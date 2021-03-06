import torch
import numpy as np
from torch.optim import Optimizer


# Pytorch Port of a previous tensorflow implementation in `tensorflow_probability`:
# https://github.com/tensorflow/probability/blob/master/tensorflow_probability/g3doc/api_docs/python/tfp/optimizer/StochasticGradientLangevinDynamics.md
class PreconditionedSGLD(Optimizer):
    """ Stochastic Gradient Langevin Dynamics Sampler with preconditioning.
        Optimization variable is viewed as a posterior sample under Stochastic
        Gradient Langevin Dynamics with noise rescaled in eaach dimension
        according to RMSProp.
    """
    def __init__(self,
                 params,
                 lr=np.float64(1e-2),
                 num_train_points=1,
                 precondition_decay_rate=np.float64(0.99),
                 diagonal_bias=np.float64(1e-5)) -> None:
        """ Set up a SGLD Optimizer.

        Parameters
        ----------
        params : iterable
            Parameters serving as optimization variable.
        lr : float, optional
            Base learning rate for this optimizer.
            Must be tuned to the specific function being minimized.
            Default: `1e-2`.
        precondition_decay_rate : float, optional
            Exponential decay rate of the rescaling of the preconditioner (RMSprop).
            Should be smaller than but nearly `1` to approximate sampling from the posterior.
            Default: `0.95`
        num_pseudo_batches : int, optional
            Effective number of minibatches in the data set.
            Trades off noise and prior with the SGD likelihood term.
            Note: Assumes loss is taken as mean over a minibatch.
            Otherwise, if the sum was taken, divide this number by the batch size.
            Default: `1`.
        num_burn_in_steps : int, optional
            Number of iterations to collect gradient statistics to update the
            preconditioner before starting to draw noisy samples.
            Default: `3000`.
        diagonal_bias : float, optional
            Term added to the diagonal of the preconditioner to prevent it from
            degenerating.
            Default: `1e-5`.

        """
        if lr < 0.0:
            raise ValueError("Invalid learning rate: {}".format(lr))

        defaults = dict(
            lr=lr, precondition_decay_rate=precondition_decay_rate,
            diagonal_bias=diagonal_bias,
            num_train_points=num_train_points
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
                lr = group["lr"]
                num_train_points = group["num_train_points"]
                precondition_decay_rate = group["precondition_decay_rate"]  # alpha
                diagonal_bias = group["diagonal_bias"]  # lambda
                gradient = parameter.grad.data

                #  state initialization
                if len(state) == 0:
                    state["iteration"] = 0
                    state["momentum"] = torch.ones_like(parameter).double()

                state["iteration"] += 1

                #  momentum update
                momentum = state["momentum"]
                momentum_t = momentum * precondition_decay_rate + (1.0 - precondition_decay_rate) * (gradient ** 2)
                state["momentum"] = momentum_t  # V(theta_t+1)

                # compute preconditioner
                preconditioner = (1. / (torch.sqrt(momentum_t) + diagonal_bias))  # G(theta_t+1)

                # standard deviation of the injected noise
                sigma = torch.sqrt(torch.tensor(lr).double()) * torch.sqrt(preconditioner)

                mean = 0.5 * lr * (preconditioner * gradient * num_train_points)
                delta = (mean + torch.normal(mean=torch.zeros_like(gradient), std=torch.ones_like(gradient)) * sigma)

                parameter.data.add_(-delta)

        return loss
