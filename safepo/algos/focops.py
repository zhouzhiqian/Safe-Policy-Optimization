# Copyright 2023 OmniSafeAI Team. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
from turtle import pen
import torch
from safepo.algos.policy_gradient import PG
from safepo.algos.lagrangian_base import Lagrangian
import safepo.common.mpi_tools as mpi_tools
import numpy as np

class FOCOPS(PG,Lagrangian):
    def __init__(
            self,
            algo='focops',
            eta=0.02,
            lam=1.5,
            cost_limit=25.,
            lagrangian_multiplier_init=0,
            lambda_lr=0.01,
            lambda_optimizer='Adam',
            use_standardized_reward=True,
            use_standardized_cost=True,
            use_standardized_obs=True,
            use_cost_value_function=True,
            use_kl_early_stopping=True,
            **kwargs
        ):

        PG.__init__(
            self,
            algo=algo,
            use_cost_value_function=use_cost_value_function,
            use_kl_early_stopping=use_kl_early_stopping,
            use_standardized_reward=use_standardized_reward,
            use_standardized_cost=use_standardized_cost,
            use_standardized_obs=use_standardized_obs,
            **kwargs)

        Lagrangian.__init__(
                             self,
                             cost_limit=cost_limit,
                             lagrangian_multiplier_init=lagrangian_multiplier_init,
                             lambda_lr=lambda_lr,
                             lambda_optimizer=lambda_optimizer)
        # replace
        self.lagrangian_multiplier = 0.0
        self.lam = lam
        self.eta = eta

    def algorithm_specific_logs(self):
        super().algorithm_specific_logs()
        self.logger.log_tabular('LagrangeMultiplier', self.lagrangian_multiplier)

    def update_lagrange_multiplier(self, ep_costs):
        self.lagrangian_multiplier += self.lambda_lr * (ep_costs - self.cost_limit)
        if self.lagrangian_multiplier < 0.0:
            self.lagrangian_multiplier = 0.0
        elif self.lagrangian_multiplier > 2.0:
            self.lagrangian_multiplier = 2.0

    def compute_loss_pi(self, data: dict):
        # Policy loss
        dist, _log_p = self.ac.pi(data['obs'], data['act'])
        ratio = torch.exp(_log_p - data['log_p'])

        kl_new_old = torch.distributions.kl.kl_divergence(dist, self.p_dist).sum(-1, keepdim=True)
        loss_pi = (kl_new_old - (1 / self.lam) * ratio *(data['adv'] - self.lagrangian_multiplier * data['cost_adv'])) \
         * (kl_new_old.detach() <= self.eta).type(torch.float32)
        loss_pi = loss_pi.mean()
        loss_pi -= self.entropy_coef * dist.entropy().mean()
        # loss_pi -= 0.01 * dist.entropy().mean()

        # Useful extra info
        approx_kl = (0.5 * (dist.mean - data['act']) ** 2
                     / dist.stddev ** 2).mean().item()
        ent = dist.entropy().mean().item()
        pi_info = dict(kl=approx_kl, ent=ent, ratio=ratio.mean().item())

        return loss_pi, pi_info

    def update(self):
        raw_data = self.buf.get()
        data = self.pre_process_data(raw_data)
        self.fvp_obs = data['obs'][::4]
        self.update_policy_net(data=data)
        self.update_value_net(data=data)
        self.update_cost_net(data=data)
        self.update_running_statistics(raw_data)

    def update_policy_net(self, data) -> None:
        with torch.no_grad():
            self.p_dist = self.ac.pi.detach_dist(data['obs'])

        # Get loss and info values before update
        pi_l_old, pi_info_old = self.compute_loss_pi(data)
        self.loss_pi_before = pi_l_old.item()

        # Train policy with multiple steps of gradient descent
        for i in range(self.train_pi_iterations):
            self.pi_optimizer.zero_grad()
            loss_pi, pi_info = self.compute_loss_pi(data=data)
            loss_pi.backward()
            # Apply L2 norm
            if self.use_max_grad_norm:
                torch.nn.utils.clip_grad_norm_(self.ac.pi.parameters(),self.max_grad_norm)

            # Average grads across MPI processes
            mpi_tools.mpi_avg_grads(self.ac.pi.net)
            self.pi_optimizer.step()

            q_dist = self.ac.pi.dist(data['obs'])
            torch_kl = torch.distributions.kl.kl_divergence(q_dist,self.p_dist).mean().item()

            if self.use_kl_early_stopping:
                # Average KL for consistent early stopping across processes
                if mpi_tools.mpi_avg(torch_kl) > 0.02:
                    self.logger.log(f'Reached ES criterion after {i+1} steps.')
                    break

        # Track when policy iteration is stopped; Log changes from update
        self.logger.store(**{
            'Loss/Pi': self.loss_pi_before,
            'Loss/DeltaPi': loss_pi.item() - self.loss_pi_before,
            'Misc/StopIter': i + 1,
            'Values/Adv': data['adv'].numpy(),
            'Entropy': pi_info['ent'],
            'KL': torch_kl,
            'PolicyRatio': pi_info['ratio']
        })

    def update_value_net(self, data: dict) -> None:
        # Divide whole local epoch data into mini_batches which is mbs size
        mbs = self.local_steps_per_epoch // self.num_mini_batches
        assert mbs >= 16, f'Batch size {mbs}<16'

        loss_v = self.compute_loss_v(data['obs'], data['target_v'])
        self.loss_v_before = loss_v.item()

        indices = np.arange(self.local_steps_per_epoch)
        val_losses = []
        for _ in range(self.train_v_iterations):
            # Shuffle for mini-batch updates
            np.random.shuffle(indices)
            # 0 to mini_batch_size with batch_train_size step
            for start in range(0, self.local_steps_per_epoch, mbs):
                end = start + mbs  # iterate mini batch times
                mb_indices = indices[start:end]
                self.vf_optimizer.zero_grad()
                loss_v = self.compute_loss_v(
                    obs=data['obs'][mb_indices],
                    ret=data['target_v'][mb_indices])
                loss_v.backward()
                val_losses.append(loss_v.item())
                # Average grads across MPI processes
                mpi_tools.mpi_avg_grads(self.ac.v)
                self.vf_optimizer.step()

        self.logger.store(**{
            'Loss/DeltaValue': np.mean(val_losses) - self.loss_v_before,
            'Loss/Value': self.loss_v_before,
        })

    def update_cost_net(self, data: dict) -> None:
        """Some child classes require additional updates,
        e.g. Lagrangian-PPO needs Lagrange multiplier parameter."""
        # Ensure we have some key components
        assert self.use_cost_value_function
        assert hasattr(self, 'cf_optimizer')
        assert 'target_c' in data, f'provided keys: {data.keys()}'

        if self.use_cost_value_function:
            self.loss_c_before = self.compute_loss_c(data['obs'],
                                                     data['target_c']).item()

        # Divide whole local epoch data into mini_batches which is mbs size
        mbs = self.local_steps_per_epoch // self.num_mini_batches
        assert mbs >= 16, f'Batch size {mbs}<16'

        indices = np.arange(self.local_steps_per_epoch)
        losses = []

        # Train cost value network
        for _ in range(self.train_v_iterations):
            # Shuffle for mini-batch updates
            np.random.shuffle(indices)
            # 0 to mini_batch_size with batch_train_size step
            for start in range(0, self.local_steps_per_epoch, mbs):
                # Iterate mini batch times
                end = start + mbs
                mb_indices = indices[start:end]

                self.cf_optimizer.zero_grad()
                loss_c = self.compute_loss_c(obs=data['obs'][mb_indices],
                                             ret=data['target_c'][mb_indices])
                loss_c.backward()
                losses.append(loss_c.item())
                # Average grads across MPI processes
                mpi_tools.mpi_avg_grads(self.ac.c)
                self.cf_optimizer.step()

        self.logger.store(**{
            'Loss/DeltaCost': np.mean(losses) - self.loss_c_before,
            'Loss/Cost': self.loss_c_before,
        })
