import torch

from torch.optim import Optimizer


class SGHMC(Optimizer):
    """ Stochastic Gradient Hamiltonian Monte-Carlo Sampler that uses a burn-in
        procedure to adapt its own hyperparameters during the initial stages
        of sampling.

        See [1] for more details on Stochastic Gradient Hamiltonian Monte-Carlo.

        [1] T. Chen, E. B. Fox, C. Guestrin
            In Proceedings of Machine Learning Research 32 (2014).\n
            `Stochastic Gradient Hamiltonian Monte Carlo <https://arxiv.org/pdf/1402.4102.pdf>`_
    """
    name = "AdaptiveSGHMC"

    def __init__(self,
                 params,
                 lr: float=1e-2,
                 mdecay: float=0.01,
                 wd: float=0.00002,
                 scale_grad: float=1.) -> None:
        """ Set up a SGHMC Optimizer.

        Parameters
        ----------
        params : iterable
            Parameters serving as optimization variable.
        lr: float, optional
            Base learning rate for this optimizer.
            Must be tuned to the specific function being minimized.
            Default: `1e-2`.
        num_burn_in_steps: int, optional
            Number of burn-in steps to perform. In each burn-in step, this
            sampler will adapt its own internal parameters to decrease its error.
            Set to `0` to turn scale adaption off.
            Default: `3000`.
        noise: float, optional
            (Constant) per-parameter noise level.
            Default: `0.`.
        mdecay:float, optional
            (Constant) momentum decay per time-step.
            Default: `0.05`.
        scale_grad: float, optional
            Value that is used to scale the magnitude of the noise used
            during sampling. In a typical batches-of-data setting this usually
            corresponds to the number of examples in the entire dataset.
            Default: `1.0`.

        """
        if lr < 0.0:
            raise ValueError("Invalid learning rate: {}".format(lr))

        defaults = dict(
            lr=lr, scale_grad=float(scale_grad),
            mdecay=mdecay,
            wd=wd
        )
        super().__init__(params, defaults)

    def step(self, closure=None):
        loss = None

        if closure is not None:
            loss = closure()

        for group in self.param_groups:
            for parameter in group["params"]:

                if parameter.grad is None:
                    continue

                state = self.state[parameter]

                if len(state) == 0:
                    state["iteration"] = 0
                    state["momentum"] = torch.zeros_like(parameter)

                state["iteration"] += 1

                mdecay, lr, wd = group["mdecay"], group["lr"], group["wd"]
                scale_grad = torch.tensor(group["scale_grad"])

                momentum = state["momentum"]
                gradient = parameter.grad.data

                sigma = torch.sqrt(2 * lr * mdecay / scale_grad)
                sample_t = torch.normal(mean=0., std=sigma)
                # momentum_update = (1 - mdecay) * momentum - lr * gradient + sample_t #- lr * wd * parameter.data

                momentum_t = momentum.add_(
                    - lr * gradient - mdecay * momentum + sample_t
                )

                parameter.data.add_(momentum_t)

        return loss
