from collections import namedtuple
import numpy as np
import torch
from torch import optim
import torch.nn as nn
from utilities.util import *
from utilities.replay_buffer import *
from learning_algorithms.actor_critic import *
from learning_algorithms.reinforce import *
from learning_algorithms.ddpg import *
from utilities.inspector import *



# define a transition of an episode
Transition = namedtuple('Transition', ('state', 'action', 'reward', 'next_state', 'done', 'last_step'))



class Trainer(object):

    def __init__(self, args, model, env):
        self.args = args
        self.cuda_ = self.args.cuda and torch.cuda.is_available()
        inspector(self.args)
        if self.args.target:
            target_net = model(self.args).cuda() if self.cuda_ else model(self.args)
            self.behaviour_net = model(self.args, target_net).cuda() if self.cuda_ else model(self.args, target_net)
        else:
            self.behaviour_net = model(self.args).cuda() if self.cuda_ else model(self.args)
        if self.args.replay:
            self.replay_buffer = ReplayBuffer(int(self.args.replay_buffer_size))
        self.env = env
        self.action_optimizer = optim.Adam(self.behaviour_net.action_dict.parameters(), lr = args.policy_lrate)
        self.value_optimizer = optim.Adam(self.behaviour_net.value_dict.parameters(), lr = args.value_lrate)

    def get_episode(self, stat):
        episode = []
        state = self.env.reset()
        mean_reward = []
        info = {}
        if self.args.epsilon_softmax:
            info['softmax_eps'] = self.behaviour_net.eps
        for t in range(self.args.max_steps):
            start_step = True if t == 0 else False
            state_ = cuda_wrapper(prep_obs(state).contiguous().view(1, self.args.agent_num, self.args.obs_size), self.cuda_)
            action_out = self.behaviour_net.policy(state_, info=info, stat=stat)
            action = select_action(self.args, action_out, status='train', info=info)
            # return the rescaled (clipped) actions
            _, actual = translate_action(self.args, action, self.env)
            if self.args.model_name == 'coma':
                info['last_action'] = action
            next_state, reward, done, _ = self.env.step(actual)
            if isinstance(done, list): done = np.sum(done)
            done_ = done or t==self.args.max_steps-1
            mean_reward.append(reward)
            trans = Transition(state, action.cpu().numpy(), np.array(reward), next_state, done, done_)
            episode.append(trans)
            if done_:
                if self.args.model_name == 'coma': self.behaviour_net.init_hidden(batch_size=1)
                break
            state = next_state
        mean_reward = np.mean(mean_reward)
        num_steps = t+1
        if self.args.epsilon_softmax:
            self.behaviour_net.update_eps()
        return episode, mean_reward, num_steps

    def get_batch_loss(self, batch):
        action_loss, value_loss, log_p_a = self.behaviour_net.get_loss(batch)
        return action_loss, value_loss, log_p_a

    def action_compute_grad(self, stat, batch_loss):
        action_loss, log_p_a = batch_loss
        if not self.args.continuous:
            if self.args.entr > 0:
                entropy = multinomial_entropy(log_p_a)
                action_loss -= self.args.entr * entropy
                stat['entropy'] = entropy.item()
        action_loss.backward()

    def value_compute_grad(self, batch_loss):
        value_loss = batch_loss
        value_loss.backward()

    def grad_clip(self, module):
        for name, param in module.named_parameters():
            param.grad.data.clamp_(-1, 1)

    def replay_process(self, stat):
        action_loss_ = 0
        value_loss_ = 0
        policy_grad_norm = 0
        value_grad_norm = 0
        for i in range(self.args.replay_iters):
            batch = self.replay_buffer.get_batch_episodes(\
                                    self.args.epoch_size*self.args.max_steps)
            batch = Transition(*zip(*batch))
            action_loss, value_loss, log_p_a = self.get_batch_loss(batch)
            action_loss_ += action_loss.item()
            value_loss_ += value_loss.item()
            self.value_optimizer.zero_grad()
            self.value_compute_grad(value_loss)
            if self.args.grad_clip:
                self.grad_clip(self.behaviour_net.value_dict)
            value_grad_norm += get_grad_norm(self.behaviour_net.value_dict)
            self.value_optimizer.step()
            self.action_optimizer.zero_grad()
            self.action_compute_grad(stat, (action_loss, log_p_a))
            if self.args.grad_clip:
                self.grad_clip(self.behaviour_net.action_dict)
            policy_grad_norm += get_grad_norm(self.behaviour_net.action_dict)
            self.action_optimizer.step()
        merge_dict(stat, 'action_loss', action_loss_ / self.args.replay_iters)
        merge_dict(stat, 'value_loss', value_loss_ / self.args.replay_iters)
        merge_dict(stat, 'policy_grad_norm', policy_grad_norm / self.args.replay_iters)
        merge_dict(stat, 'value_grad_norm', value_grad_norm / self.args.replay_iters)

    def online_process(self, stat, batch):
        action_loss, value_loss, log_p_a = self.get_batch_loss(batch)
        self.value_optimizer.zero_grad()
        self.value_compute_grad(value_loss)
        if self.args.grad_clip:
            self.grad_clip(self.behaviour_net.value_dict)
        stat['value_grad_norm'] = get_grad_norm(self.behaviour_net.value_dict)
        self.value_optimizer.step()
        self.action_optimizer.zero_grad()
        self.action_compute_grad(stat, (action_loss, log_p_a))
        if self.args.grad_clip:
            self.grad_clip(self.behaviour_net.action_dict)
        stat['policy_grad_norm'] = get_grad_norm(self.behaviour_net.action_dict)
        self.action_optimizer.step()
        stat['action_loss'] = action_loss.item()
        stat['value_loss'] = value_loss.item()

    def run_batch(self):
        batch = []
        stats = dict()
        num_episodes = 0
        average_mean_reward = 0
        average_num_steps = 0
        while num_episodes < self.args.epoch_size:
            episode, mean_reward, num_steps = self.get_episode(stats)
            average_mean_reward += mean_reward
            average_num_steps += num_steps
            num_episodes += 1
            batch += episode
            if self.args.replay:
                self.replay_buffer.add_experience(episode)
        stats['batch_finish_steps'] = len(batch)
        stats['mean_reward'] = average_mean_reward / self.args.epoch_size
        stats['average_episode_steps'] = average_num_steps / self.args.epoch_size
        batch = Transition(*zip(*batch))
        return batch, stats

    def train_batch(self, t, batch, stat):
        if self.args.model_name in ['maddpg']:
            self.replay_process(stat)
        else:
            self.online_process(stat, batch)
            if self.args.replay:
                self.replay_process(stat)
        if t%self.args.target_update_freq == self.args.target_update_freq-1:
            self.behaviour_net.update_target()
        return stat