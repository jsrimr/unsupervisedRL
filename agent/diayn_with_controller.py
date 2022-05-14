import math
from collections import OrderedDict

import hydra
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import utils
from dm_env import specs
from torch.distributions import Categorical

from agent.ddpg import DDPGAgent


class DIAYN(nn.Module):
    def __init__(self, obs_dim, skill_dim, hidden_dim):
        super().__init__()
        self.skill_pred_net = nn.Sequential(nn.Linear(obs_dim, hidden_dim),
                                            nn.ReLU(),
                                            nn.Linear(hidden_dim, hidden_dim),
                                            nn.ReLU(),
                                            nn.Linear(hidden_dim, skill_dim))

        self.apply(utils.weight_init)

    def forward(self, obs):
        skill_pred = self.skill_pred_net(obs)
        return skill_pred


class DIAYNwithController(DDPGAgent):
    def __init__(self, update_skill_every_step, skill_dim, diayn_scale,
                 update_encoder, **kwargs):
        self.skill_dim = skill_dim
        self.update_skill_every_step = update_skill_every_step
        self.diayn_scale = diayn_scale
        self.update_encoder = update_encoder
        # increase obs shape to include skill dim
        kwargs["meta_dim"] = self.skill_dim
        self.current_meta = None

        # create actor and critic
        super().__init__(**kwargs)

        # create diayn
        self.diayn = DIAYN(self.obs_dim - self.skill_dim, self.skill_dim,
                           kwargs['hidden_dim']).to(kwargs['device'])
        self.softmax = nn.Softmax(dim=1)

        # optimizers
        self.diayn_opt = torch.optim.Adam(self.diayn.parameters(), lr=self.lr)

        self.diayn.train()

    def act(self, obs, meta, step, eval_mode):
        """
        meta from passed parameter is useless
        """
        with torch.no_grad():
            obs = torch.as_tensor(obs, device=self.device).unsqueeze(0)
            h = self.encoder(obs)
            x = self.diayn(obs)  # (B, skill_dim)
            skill_dist = Categorical(self.softmax(x))
            skill = skill_dist.sample()  # Or, skill = torch.argmax(x, dim=1) # (B,1)
            meta = torch.zeros(self.skill_dim, device=self.device)
            meta[skill] = 1.0
            self.current_meta = meta

        inpt = torch.cat([h, meta.unsqueeze(0)], dim=-1)
        #assert obs.shape[-1] == self.obs_shape[-1]
        stddev = utils.schedule(self.stddev_schedule, step)

        dist = self.actor(inpt, stddev)
        if eval_mode:
            action = dist.mean
        else:
            action = dist.sample(clip=None)
            if step < self.num_expl_steps:
                action.uniform_(-1.0, 1.0)

        return action.cpu().numpy()[0]

    def init_from(self, other):
        # copy parameters over
        utils.hard_update_params(other.encoder, self.encoder)
        utils.hard_update_params(other.actor, self.actor)
        utils.hard_update_params(other.diayn, self.diayn)
        if self.init_critic:
            utils.hard_update_params(other.critic.trunk, self.critic.trunk)

    def get_meta_specs(self):
        return (specs.Array((self.skill_dim,), np.float32, 'skill'),)

    def get_current_meta(self):
        meta = OrderedDict()
        meta['skill'] = self.current_meta.cpu().numpy()
        return meta

    def update_actor_and_controller(self, obs, step):
        metrics = dict()
        # update controller
        stddev = utils.schedule(self.stddev_schedule, step)
        dist = self.actor(obs, stddev)
        action = dist.sample(clip=self.stddev_clip)
        log_prob = dist.log_prob(action).sum(-1, keepdim=True)
        Q1, Q2 = self.critic(obs, action)
        Q = torch.min(Q1, Q2)

        actor_loss = -Q.mean()

        # optimize actor
        self.actor_opt.zero_grad(set_to_none=True)
        actor_loss.backward()
        # actor_loss.backward(retain_graph=True)
        self.actor_opt.step()

        if self.use_tb or self.use_wandb:
            metrics['actor_loss'] = actor_loss.item()
            metrics['actor_logprob'] = log_prob.mean().item()
            metrics['actor_ent'] = dist.entropy().sum(dim=-1).mean().item()

        # update controller

        skill_prob = self.softmax(self.diayn(obs[:, :-self.skill_dim]))
        skill_dist = Categorical(skill_prob)

        skill_used = obs[:, -self.skill_dim:].argmax(dim=1)
        skill_dist.log_prob(torch.tensor(1, device="cuda:0")).shape
        # loss = - Q.mean().detach() * skill_pred.gather(1, skill_used.unsqueeze(0)).log_prob()
        advantage = (Q - Q.mean(dim=1)).detach()
        loss = -(advantage * skill_dist.log_prob(skill_used).unsqueeze(1)).mean()

        self.diayn_opt.zero_grad()
        if self.encoder_opt is not None:
            self.encoder_opt.zero_grad(set_to_none=True)
        loss.backward()
        self.diayn_opt.step()
        if self.encoder_opt is not None:
            self.encoder_opt.step()

        if self.use_tb or self.use_wandb:
            metrics['diayn_loss'] = loss.item()

        return metrics

    def update(self, replay_iter, step):
        """
        only ext_reward logic
        update controller

        """
        metrics = dict()

        if step % self.update_every_steps != 0:
            return metrics

        batch = next(replay_iter)

        obs, action, extr_reward, discount, next_obs, skill = utils.to_torch(
            batch, self.device)

        # augment and encode
        obs = self.aug_and_encode(obs)
        next_obs = self.aug_and_encode(next_obs)
        reward = extr_reward

        if self.use_tb or self.use_wandb:
            metrics['extr_reward'] = extr_reward.mean().item()
            metrics['batch_reward'] = reward.mean().item()

        if not self.update_encoder:
            obs = obs.detach()
            next_obs = next_obs.detach()

        # extend observations with skill
        obs = torch.cat([obs, skill], dim=1)
        next_obs = torch.cat([next_obs, skill], dim=1)

        # update critic
        metrics.update(
            self.update_critic(obs.detach(), action, reward, discount,
                               next_obs.detach(), step))

        # update actor and controller
        metrics.update(self.update_actor_and_controller(obs.detach(), step))

        # update critic target
        utils.soft_update_params(self.critic, self.critic_target,
                                 self.critic_target_tau)

        return metrics