import torch
import torch.nn.functional as F
from safepo.algos.policy_gradient import PG
class P3O(PG):
    """

    """
    def __init__(
            self, 
            algo='p3o', 
            cost_limit=25., 
            clip=0.2, 
            kappa=20.0,
            use_standardized_reward=True, 
            use_standardized_cost=True,
            use_standardized_obs=False,
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
            **kwargs
        )
        self.clip = clip
        self.cost_limit = cost_limit
        self.kappa = kappa

    def compute_loss_pi(self, data):
        dist, _log_p = self.ac.pi(data['obs'], data['act'])
        ratio = torch.exp(_log_p - data['log_p'])

        ratio_clip = torch.clamp(ratio, 1-self.clip, 1+self.clip)

        surr_adv = (torch.min(ratio * data['adv'], ratio_clip * data['adv'])).mean()
        surr_cadv = (ratio * data['cost_adv']).mean()
        ep_costs = self.logger.get_stats('EpCosts')[0]
        c = (1 - self.gamma) * (ep_costs - self.cost_limit)
        loss_pi = -surr_adv + self.kappa * F.relu(surr_cadv + c)
        loss_pi = loss_pi.mean()

        # Useful extra info
        approx_kl = (0.5 * (dist.mean - data['act']) ** 2
                     / dist.stddev ** 2).mean().item()
        ent = dist.entropy().mean().item()
        pi_info = dict(kl=approx_kl, ent=ent, ratio=ratio_clip.mean().item())

        return loss_pi, pi_info