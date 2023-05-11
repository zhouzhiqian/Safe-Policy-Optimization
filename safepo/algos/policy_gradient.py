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
from re import L
import numpy as np
import gym
import time
import torch
from copy import deepcopy
from safepo.common import core
from safepo.models.Constraint_Actor_Critic import ConstraintActorCritic
from safepo.common.logger import EpochLogger
import safepo.common.mpi_tools as mpi_tools
from safepo.algos.base import PolicyGradient
from safepo.common.buffer import Buffer
from safepo.common.utils import get_flat_params_from

class PG(PolicyGradient):
    def __init__(
            self,
            actor,
            ac_kwargs,
            env_id,
            epochs,
            logger_kwargs,
            adv_estimation_method='gae',
            algo='pg',
            check_freq=25,
            entropy_coef=0.01,
            gamma=0.99,
            lam=0.95,
            lam_c=0.95,
            max_ep_len=1000,
            max_grad_norm=0.5,
            num_mini_batches=16,
            optimizer='Adam',
            pi_lr=3e-4,
            vf_lr=1e-3,
            steps_per_epoch=32 * 1000,
            target_kl=0.01,
            train_pi_iterations=80,
            train_v_iterations=40,
            use_discount_cost_update_lag=False,
            use_cost_value_function=False,
            use_entropy=False,
            use_exploration_noise_anneal=False,
            use_kl_early_stopping=False,
            use_linear_lr_decay=True,
            use_max_grad_norm=False,
            use_reward_scaling=False,
            use_reward_penalty=False,
            use_shared_weights=False,
            use_standardized_reward=False,
            use_standardized_cost=False,
            use_standardized_obs=True,
            weight_initialization = 'kaiming_uniform',
            save_freq=10,
            seed=0,
            enable_eval=False,
        ):
        """
        Policy Gradient,
        Args:
            actor (string): The type of network in actor, it does not actually affect any things
                which happen in the following.

            ac_kwargs (dictionary): Information about actor and critic's net work configuration,
                it originates from {algo}.yaml file to describe [hidden layers] and [activation function].

            env_id (string): The name of environment we want to roll out.

            epochs (int): The number of epochs we want to roll out.

            logger_kwargs (dictionary): The information about logger configuration which originates
                from [runner module].

            adv_estimation_method (string): The type of advantage estimation method.

            algo (string): The name of algorithm corresponding to current class, it does not actually
                affect any things which happen in the following.

            check_freq (int): The frequency for we to check if all models own the same parameter values.
                (for mpi multi-process purpose)

            entropy_coef (float): The discount coefficient for entropy penalty, if parameters[use_entropy=True].

            gamma (float): The gamma for GAE.

            lam (float): The lambda for reward GAE.

            lam_c (float): The lambda for cost GAE.

            max_ep_len (int): The maximum timesteps of an episode.

            max_grad_norm (float): If parameters[use_max_grad_norm=True], use this parameter to normalize gradient.

            num_mini_batches (int): The number of mini batches we want to update actor and critic after one epoch.

            optimizer (string): The type of optimizer.

            pi_lr (float): The learning rate of actor network.

            vf_lr (float): The learning rate of critic network.

            steps_per_epoch (int): The number of time steps per epoch.

            target_kl (float): Roughly what KL divergence we think is appropriate
                between new and old policies after an update. This will get used
                for early stopping. (Usually small, 0.01 or 0.05.)

            train_pi_iterations (int): The number of iteration when we update actor network per mini batch.

            train_v_iterations (int): The number of iteration when we update critic network per mini batch.

            use_cost_value_function (bool): Use cost value function or not.

            use_entropy (bool): Use entropy penalty or not.

            use_exploration_noise_anneal (bool): Use exloration noise anneal or not.

            use_kl_early_stopping (bool): Use KL early stopping or not.

            use_linear_lr_decay (bool): Use linear learning rate decay or not.

            use_max_grad_norm (bool): Use maximum gradient normalization or not.

            use_reward_scaling (bool): Use reward scaling or not.

            use_reward_penalty (bool): Use cost to penalize reward or not.

            use_shared_weights (bool): Use shared weights between actor and critic network or not.

            use_standardized_advantages (bool): Use standardized advantages or not.

            use_standardized_obs (bool): Use standarized observation or not.

            weight_initialization (string): The type of weight initialization method.

            save_freq (int): How often (in terms of gap between epochs) to save
                the current policy and value function.

            seed (int): The random seed of this run.
        """
        # Create Environment
        self.env_id = env_id
        self.env = gym.make(env_id) if isinstance(env_id, str) else env_id

        # Use the environment's built_in max_episode_steps
        if hasattr(self.env, '_max_episode_steps'):
            max_ep_len = self.env._max_episode_steps

        self.gamma = gamma

        # How to calculate the advantage of reward/cost
        self.adv_estimation_method = adv_estimation_method

        self.algo = algo
        self.check_freq = check_freq
        self.entropy_coef = entropy_coef if use_entropy else 0.0
        self.epochs = epochs
        self.lam = lam
        self.local_steps_per_epoch = steps_per_epoch // mpi_tools.num_procs()
        self.logger_kwargs = logger_kwargs
        self.max_ep_len = max_ep_len
        self.max_grad_norm = max_grad_norm
        self.num_mini_batches = num_mini_batches
        self.pi_lr = pi_lr
        self.vf_lr = vf_lr
        self.save_freq = save_freq
        self.seed = seed
        self.steps_per_epoch = steps_per_epoch
        self.target_kl = target_kl
        self.train_pi_iterations = train_pi_iterations
        self.train_v_iterations = train_v_iterations
        self.use_cost_value_function = use_cost_value_function
        self.use_exploration_noise_anneal = use_exploration_noise_anneal
        self.use_kl_early_stopping = use_kl_early_stopping
        self.use_linear_lr_decay = use_linear_lr_decay
        self.use_max_grad_norm = use_max_grad_norm
        self.use_reward_penalty = use_reward_penalty
        self.use_reward_scaling = use_reward_scaling
        self.use_standardized_obs = use_standardized_obs
        self.use_standardized_reward = use_standardized_reward
        self.use_standardized_cost = use_standardized_cost
        self.use_discount_cost_update_lag = use_discount_cost_update_lag

        # Call assertions, Check if some variables are valid to experiment
        # You can add assert that you want to check
        self._init_checks()

        if not enable_eval:
            # If We want to train rather than eval
            # Set up logger and save configuration to disk
            # Get local parameters before logger instance to avoid unnecessary print
            self.params = locals()
            self.logger = self._init_logger()
            self.logger.save_config(self.params)

        # Set seed
        seed += 10000 * mpi_tools.proc_id()
        torch.manual_seed(seed)
        np.random.seed(seed)
        self.env.seed(seed=seed)

        # Setup actor-critic module
        self.ac = ConstraintActorCritic(
            actor_type=actor,
            observation_space=self.env.observation_space,
            action_space=self.env.action_space,
            use_standardized_obs=use_standardized_obs,
            use_scaled_rewards=use_reward_scaling,
            use_shared_weights=use_shared_weights,
            weight_initialization=weight_initialization,
            ac_kwargs=ac_kwargs
        )

        if not enable_eval:
            # Set PyTorch + MPI.
            self._init_mpi()

        # Set up experience buffer
        self.buf = Buffer(
            actor_critic=self.ac,
            obs_dim=self.env.observation_space.shape,
            act_dim=self.env.action_space.shape,
            size=self.local_steps_per_epoch,
            gamma=gamma,
            lam=lam,
            adv_estimation_method=adv_estimation_method,
            use_scaled_rewards=use_reward_scaling,
            standardize_env_obs=use_standardized_obs,
            use_standardized_reward=self.use_standardized_reward,
            use_standardized_cost=self.use_standardized_cost,
            lam_c=lam_c,
            use_reward_penalty=use_reward_penalty,
        )

        # Set up optimizers for policy and value function
        self.pi_optimizer = core.get_optimizer(optimizer, module=self.ac.pi, lr=pi_lr)
        self.vf_optimizer = core.get_optimizer('Adam', module=self.ac.v, lr=vf_lr)
        if use_cost_value_function:
            self.cf_optimizer = core.get_optimizer('Adam', module=self.ac.c, lr=vf_lr)

        # Set up scheduler for policy learning rate decay
        self.scheduler = self._init_learning_rate_scheduler()

        if not enable_eval:
            # Set up model saving
            self.logger.setup_torch_saver(self.ac.pi)
            self.logger.torch_save()

            # Setup statistics
            self.start_time = time.time()
            self.epoch_time = time.time()
            self.loss_pi_before = 0.0
            self.loss_v_before = 0.0
            self.loss_c_before = 0.0
            self.logger.log('Start with training.')

    def _init_learning_rate_scheduler(self):
        scheduler = None
        if self.use_linear_lr_decay:
            import torch.optim
            # Linear anneal
            def lm(epoch): return 1 - epoch / self.epochs
            scheduler = torch.optim.lr_scheduler.LambdaLR(
                optimizer=self.pi_optimizer,
                lr_lambda=lm
            )
        return scheduler

    def _init_logger(self):
        """
            Initialize Epoch logger
        """
        # Remove objects
        self.params.pop('self')
        # Combine inner parameters dictionary with outter
        if 'kwargs' in self.params:
            self.params.update(**self.params.pop('kwargs'))
        # Initialize logger
        logger = EpochLogger(**self.logger_kwargs)
        return logger

    def _init_mpi(self):
        """
            Initialize MPI specifics
        """
        if mpi_tools.num_procs() > 1:
            # Avoid slowdowns from PyTorch + MPI combo
            mpi_tools.setup_torch_for_mpi()
            dt = time.time()
            self.logger.log('INFO: Sync actor critic parameters')
            # Sync params across cores: only once necessary, grads are averaged!
            mpi_tools.sync_params(self.ac)
            self.logger.log(f'Done! (took {time.time()-dt:0.3f} sec.)')

    def _init_checks(self):
        """
            Checking feasible
        """
        # The steps in each process should be integer
        assert self.steps_per_epoch % mpi_tools.num_procs() == 0
        # Ensure local each local process can experience at least one complete eposide
        assert self.max_ep_len <= self.local_steps_per_epoch, \
            f'Reduce number of cores ({mpi_tools.num_procs()}) or increase ' \
            f'batch size {self.steps_per_epoch}.'
        # Ensure vilid number for iteration
        assert self.train_pi_iterations > 0
        assert self.train_v_iterations > 0
        # Ensure environment is consistent with gym
        assert isinstance(self.env, gym.Env), 'Env is not the expected type.'

    def algorithm_specific_logs(self):
        """
            Use this method to collect log information.
            e.g. log lagrangian for lagrangian-base , log q, r, s, c for cpo, etc
        """
        pass

    def check_distributed_parameters(self):
        """
            Check if parameters are synchronized across all processes.
        """

        if mpi_tools.num_procs() > 1:
            self.logger.log('Check if distributed parameters are synchronous..')
            modules = {'Policy': self.ac.pi.net, 'Value': self.ac.v.net}
            for key, module in modules.items():
                flat_params = get_flat_params_from(module).numpy()
                global_min = mpi_tools.mpi_min(np.sum(flat_params))
                global_max = mpi_tools.mpi_max(np.sum(flat_params))
                assert np.allclose(global_min, global_max), f'{key} not synced.'

    def compute_loss_pi(self, data: dict):
        '''
            computing pi/actor loss

            Returns:
                torch.Tensor
        '''
        # Policy loss
        dist, _log_p = self.ac.pi(data['obs'], data['act'])
        ratio = torch.exp(_log_p - data['log_p'])

        # Compute loss via ratio and advantage
        loss_pi = -(ratio * data['adv']).mean()
        loss_pi -= self.entropy_coef * dist.entropy().mean()

        # Useful extra info
        approx_kl = (0.5 * (dist.mean - data['act']) ** 2
                     / dist.stddev ** 2).mean().item()

        # Compute policy's entropy
        ent = dist.entropy().mean().item()

        pi_info = dict(kl=approx_kl, ent=ent, ratio=ratio.mean().item())

        return loss_pi, pi_info

    def compute_loss_v(self, obs, ret):
        """
        computing value loss

        Returns:
            torch.Tensor
        """
        return ((self.ac.v(obs) - ret) ** 2).mean()

    def compute_loss_c(self, obs, ret):
        """
        computing cost loss

        Returns:
            torch.Tensor
        """
        return ((self.ac.c(obs) - ret) ** 2).mean()

    def learn(self):
        '''
            This is main function for algorithm update, divided into the following steps:
                (1). self.rollout: collect interactive data from environment
                (2). self.udpate: perform actor/critic updates
                (3). log epoch/update information for visualization and terminal log print.

            Returns:
                model and environment
        '''
        # Main loop: collect experience in env and update/log each epoch
        for epoch in range(self.epochs):
            self.epoch_time = time.time()

            # Update internals of AC
            if self.use_exploration_noise_anneal:
                self.ac.update(frac=epoch / self.epochs)
            # Collect data and store
            self.roll_out()
            if self.algo == "focops":
                ep_costs = self.logger.get_stats('EpCosts')[0]
                self.update_lagrange_multiplier(ep_costs)

            # Update: actor, critic, running statistics
            self.update()

            # Log and store information
            self.log(epoch)

            # Check if all models own the same parameter values
            if epoch % self.check_freq == 0:
                self.check_distributed_parameters()
            # Save model to disk
            if epoch == (self.epochs - 1) or epoch % self.save_freq == 0:
                self.logger.save_state(state_dict={}, itr=None)
            if (epoch + 1) % 100 == 0:
                self.logger.torch_save(itr=epoch)

        # Close opened files to avoid number of open files overflow
        self.logger.close()
        return self.ac, self.env

    def log(self, epoch: int):
        # Log info about epoch
        total_env_steps = (epoch + 1) * self.steps_per_epoch
        fps = self.steps_per_epoch / (time.time() - self.epoch_time)

        # Step the actor learning rate scheduler if provided
        if self.scheduler and self.use_linear_lr_decay:
            current_lr = self.scheduler.get_last_lr()[0]
            self.scheduler.step()
        else:
            current_lr = self.pi_lr

        self.logger.log_tabular('Epoch', epoch + 1)
        self.logger.log_tabular('EpRet', min_and_max=True, std=True)
        self.logger.log_tabular('EpCosts', min_and_max=True, std=True)
        self.logger.log_tabular('EpLen', min_and_max=True)
        self.logger.log_tabular('Values/V', min_and_max=True)
        self.logger.log_tabular('Values/Adv', min_and_max=True)
        if self.use_cost_value_function:
            self.logger.log_tabular('Values/C', min_and_max=True)
        self.logger.log_tabular('Loss/Pi', std=False)
        self.logger.log_tabular('Loss/Value')
        self.logger.log_tabular('Loss/DeltaPi')
        self.logger.log_tabular('Loss/DeltaValue')
        if self.use_cost_value_function:
            self.logger.log_tabular('Loss/Cost')
            self.logger.log_tabular('Loss/DeltaCost')
        self.logger.log_tabular('Entropy')
        self.logger.log_tabular('KL')
        self.logger.log_tabular('Misc/StopIter')
        self.logger.log_tabular('Misc/Seed', self.seed)
        self.logger.log_tabular('PolicyRatio')
        self.logger.log_tabular('LR', current_lr)
        if self.use_reward_scaling:
            reward_scale_mean = self.ac.ret_oms.mean.item()
            reward_scale_stddev = self.ac.ret_oms.std.item()
            self.logger.log_tabular('Misc/RewScaleMean', reward_scale_mean)
            self.logger.log_tabular('Misc/RewScaleStddev', reward_scale_stddev)
        if self.use_exploration_noise_anneal:
            noise_std = np.exp(self.ac.pi.log_std[0].item())
            self.logger.log_tabular('Misc/ExplorationNoiseStd', noise_std)
        # Some child classes may add information to logs
        self.algorithm_specific_logs()
        self.logger.log_tabular('TotalEnvSteps', total_env_steps)
        self.logger.log_tabular('Time', int(time.time() - self.start_time))
        self.logger.log_tabular('FPS', int(fps))

        self.logger.dump_tabular()

    def pre_process_data(self, raw_data: dict):
        """
            Pre-process data, e.g. standardize observations, rescale rewards if
                enabled by arguments.

            Parameters
            ----------
            raw_data
                dictionary holding information obtain from environment interactions

            Returns
            -------
            dict
                holding pre-processed data, i.e. observations and rewards
        """
        data = deepcopy(raw_data)
        # Note: use_reward_scaling is currently applied in Buffer...
        # If self.use_reward_scaling:
        #     rew = self.ac.ret_oms(data['rew'], subtract_mean=False, clip=True)
        #     data['rew'] = rew

        if self.use_standardized_obs:
            assert 'obs' in data
            obs = data['obs']
            data['obs'] = self.ac.obs_oms(obs, clip=False)
        return data

    def roll_out(self):
        """collect data and store to experience buffer."""
        o, ep_ret, ep_costs, ep_len = self.env.reset(), 0., 0., 0

        if self.use_reward_penalty:
            # Consider reward penalty parameter in reward calculation: r' = r - c
            assert hasattr(self, 'lagrangian_multiplier')
            assert hasattr(self, 'lambda_range_projection')
            penalty_param = self.lambda_range_projection(
                self.lagrangian_multiplier)
        else:
            penalty_param = 0

        # c_gamma_step = 0
        for t in range(self.local_steps_per_epoch):
            a, v, cv, logp = self.ac.step(
                torch.as_tensor(o, dtype=torch.float32))
            next_o, r, d, info = self.env.step(a)
            if self.env_id in ['Ant-v3', "Swimmer-v3", "HalfCheetah-v3", "Hopper-v3", "Humanoid-v3", "Walker2d-v3"]:
                if 'y_velocity' not in info:
                    c = np.abs(info['x_velocity'])
                else:
                    c = np.sqrt(info['x_velocity'] ** 2 + info['y_velocity'] ** 2)
            else:
                c = info.get('cost', 0.)

            ep_ret += r
            if self.use_discount_cost_update_lag:
                ep_costs += (self.gamma ** ep_len) * c
            else:
                ep_costs += c
            ep_len += 1

            # Save and log
            # Notes:
            #   - raw observations are stored to buffer (later transformed)
            #   - reward scaling is performed in buf
            self.buf.store(
                obs=o, act=a, rew=r, val=v, logp=logp, cost=c, cost_val=cv
            )

            # Store values for statistic purpose
            if self.use_cost_value_function:
                self.logger.store(**{
                    'Values/V': v,
                    'Values/C': cv})
            else:
                self.logger.store(**{'Values/V': v})

            # Update observation
            o = next_o

            timeout = ep_len == self.max_ep_len
            terminal = d or timeout
            epoch_ended = t == self.local_steps_per_epoch - 1

            if terminal or epoch_ended:
                if timeout or epoch_ended:
                    _, v, cv, _ = self.ac(torch.as_tensor(o, dtype=torch.float32))
                else:
                    v, cv = 0., 0.

                # Automatically compute GAE in buffer
                self.buf.finish_path(v, cv, penalty_param=float(penalty_param))

                # Only save EpRet / EpLen if trajectory finished
                if terminal:
                    self.logger.store(EpRet=ep_ret, EpLen=ep_len, EpCosts=ep_costs)
                o, ep_ret, ep_costs, ep_len = self.env.reset(), 0., 0., 0

    def update_running_statistics(self, data):
        """
        Update running statistics, e.g. observation standardization,
        or reward scaling. If MPI is activated: sync across all processes.
        """
        if self.use_standardized_obs:
            self.ac.obs_oms.update(data['obs'])

        # Apply Implement Reward scaling
        if self.use_reward_scaling:
            self.ac.ret_oms.update(data['discounted_ret'])

    def update(self):
        """
            Update actor, critic, running statistics
        """
        raw_data = self.buf.get()
        # Pre-process data: standardize observations, advantage estimation, etc.
        data = self.pre_process_data(raw_data)

        # Update critic using epoch data
        self.update_value_net(data=data)
        # Update cost critic using epoch data
        if self.use_cost_value_function:
            self.update_cost_net(data=data)
        # Update actor using epoch data
        self.update_policy_net(data=data)

        # Update running statistics, e.g. observation standardization
        # Note: observations from are raw outputs from environment
        self.update_running_statistics(raw_data)

    def update_policy_net(self, data) -> None:
        # Get prob. distribution before updates: used to measure KL distance
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
                torch.nn.utils.clip_grad_norm_(
                    self.ac.pi.parameters(),
                    self.max_grad_norm)

            # Average grads across MPI processes
            mpi_tools.mpi_avg_grads(self.ac.pi.net)
            self.pi_optimizer.step()

            q_dist = self.ac.pi.dist(data['obs'])
            torch_kl = torch.distributions.kl.kl_divergence(
                self.p_dist, q_dist).mean().item()

            if self.use_kl_early_stopping:
                # Average KL for consistent early stopping across processes
                if mpi_tools.mpi_avg(torch_kl) > 2.0:
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
