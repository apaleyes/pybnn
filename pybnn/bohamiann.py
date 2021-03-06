import logging
import time
import typing
from itertools import islice

import numpy as np
import torch
import torch.nn as nn
import torch.utils.data as data_utils
from pybnn.base_model import BaseModel
from pybnn.sampler import AdaptiveSGHMC, SGLD, SGHMC, PreconditionedSGLD, ConstantSGD, SGHMCHD
from pybnn.util.infinite_dataloader import infinite_dataloader
from pybnn.util.normalization import zero_mean_unit_var_unnormalization, zero_mean_unit_var_normalization
from scipy.stats import norm


def get_default_network(input_dimensionality: int) -> torch.nn.Module:
    class AppendLayer(nn.Module):
        def __init__(self, bias=True, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if bias:
                self.bias = nn.Parameter(torch.DoubleTensor(1, 1))
            else:
                self.register_parameter('bias', None)

        def forward(self, x):
            return torch.cat((x, self.bias * torch.ones_like(x)), dim=1)

    def init_weights(module):
        if type(module) == AppendLayer:
            nn.init.constant_(module.bias, val=np.log(1e-3))
        elif type(module) == nn.Linear:
            nn.init.kaiming_normal_(module.weight, mode="fan_in", nonlinearity="linear")
            nn.init.constant_(module.bias, val=0.0)

    return nn.Sequential(
        nn.Linear(input_dimensionality, 50), nn.Tanh(),
        nn.Linear(50, 50), nn.Tanh(),
        nn.Linear(50, 50), nn.Tanh(),
        nn.Linear(50, 1),
        AppendLayer()
    ).apply(init_weights)


def nll(input, target):
    batch_size = input.size(0)

    prediction_mean = input[:, 0].view((-1, 1))
    log_prediction_variance = input[:, 1].view((-1, 1))
    prediction_variance_inverse = 1. / (torch.exp(log_prediction_variance) + 1e-16)

    mean_squared_error = (target.view(-1, 1) - prediction_mean) ** 2

    log_likelihood = torch.sum(
        torch.sum(-mean_squared_error * (0.5 * prediction_variance_inverse) - 0.5 * log_prediction_variance, dim=1))

    log_likelihood = log_likelihood / batch_size

    return -log_likelihood


class Bohamiann(BaseModel):
    def __init__(self,
                 get_network=get_default_network,
                 batch_size=20,
                 normalize_input: bool = True,
                 normalize_output: bool = True,
                 sampling_method: str = "adaptive_sghmc",
                 metrics=(nn.MSELoss,)
                 ) -> None:
        """ Bayesian Neural Network for regression problems.

        Bayesian Neural Networks use Bayesian methods to estimate the posterior
        distribution of a neural network's weights. This allows to also
        predict uncertainties for test points and thus makes Bayesian Neural
        Networks suitable for Bayesian optimization.
        This module uses stochastic gradient MCMC methods to sample
        from the posterior distribution.

        See [1] for more details.

        [1] J. T. Springenberg, A. Klein, S. Falkner, F. Hutter
            Bayesian Optimization with Robust Bayesian Neural Networks.
            In Advances in Neural Information Processing Systems 29 (2016).

        Parameters
        ----------
        normalize_input: bool, optional
            Specifies if inputs should be normalized to zero mean and unit variance.
        normalize_output: bool, optional
            Specifies whether outputs should be un-normalized.
        """

        assert batch_size >= 1, "Invalid batch size. Batches must contain at least a single sample."

        self.batch_size = batch_size

        self.metrics = metrics
        self.do_normalize_input = normalize_input
        self.do_normalize_output = normalize_output
        self.get_network = get_network
        self.is_trained = False
        self.sampling_method = sampling_method
        self.sampled_weights = []  # type: typing.List[typing.Tuple[np.ndarray]]

    @property
    def network_weights(self) -> np.ndarray:
        """ Extract current network weight values as `np.ndarray`.

        Returns
        ----------
        weight_values: tuple
            Tuple containing current network weight values.

        """
        return tuple(
            np.asarray(torch.tensor(parameter.data).numpy())
            for parameter in self.model.parameters()
        )

    @network_weights.setter
    def network_weights(self, weights: typing.List[np.ndarray]) -> None:
        """ Assign new `weights` to our neural networks parameters.

        Parameters
        ----------
        weights : typing.List[np.ndarray]
            List of weight values to assign.
            Individual list elements must have shapes that match
            the network parameters with the same index in `self.network_weights`.

        Examples
        ----------
        This serves as a handy bridge between our pytorch parameters
        and corresponding values for them represented as numpy arrays:

        >>> import numpy as np
        >>> bnn = BayesianNeuralNetwork()
        >>> input_dimensionality = 1
        >>> bnn.model = bnn.network_architecture(input_dimensionality)
        >>> dummy_weights = [np.random.rand(parameter.shape) for parameter in bnn.model.parameters()]
        >>> bnn.network_weights = dummy_weights
        >>> np.allclose(bnn.network_weights, dummy_weights)
        True

        """
        logging.debug("Assigning new network weights: %s" % str(weights))
        for parameter, sample in zip(self.model.parameters(), weights):
            parameter.copy_(torch.from_numpy(sample))

    def train(self, x_train: np.ndarray, y_train: np.ndarray,
              num_steps: int = 13000,
              keep_every: int = 100,
              num_burn_in_steps: int = 3000,
              lr: float = 1e-2,
              noise: float = 0.,
              mdecay: float = 0.05,
              continue_training: bool = False,
              verbose=False):

        """ Train a BNN using input datapoints `x_train` with corresponding targets `y_train`.
        Parameters
        ----------
        x_train : numpy.ndarray (N, D)
            Input training datapoints.
        y_train : numpy.ndarray (N,)
            Input training labels.
        num_steps: int, optional
            Number of sampling steps to perform after burn-in is finished.
            In total, `num_steps // keep_every` network weights will be sampled.
            Defaults to `10000`.
        num_burn_in_steps: int, optional
            Number of burn-in steps to perform.
            This value is passed to the given `optimizer` if it supports special
            burn-in specific behavior.
            Networks sampled during burn-in are discarded.
            Defaults to `3000`.
        keep_every: int, optional
            Number of sampling steps (after burn-in) to perform before keeping a sample.
            In total, `num_steps // keep_every` network weights will be sampled.
            Defaults to `100`.
        """
        logging.debug("Training started.")
        start_time = time.time()

        num_datapoints, input_dimensionality = x_train.shape
        logging.debug(
            "Processing %d training datapoints "
            " with % dimensions each." % (num_datapoints, input_dimensionality)
        )

        if self.do_normalize_input:
            logging.debug(
                "Normalizing training datapoints to "
                " zero mean and unit variance."
            )
            x_train_, self.x_mean, self.x_std = self.normalize_input(x_train)
            x_train_ = torch.from_numpy(x_train_).double()
        else:
            x_train_ = torch.from_numpy(x_train).double()

        if self.do_normalize_output:
            logging.debug("Normalizing training labels to zero mean and unit variance.")
            y_train_, self.y_mean, self.y_std = self.normalize_output(y_train)
            y_train_ = torch.from_numpy(y_train_).double()
        else:
            y_train_ = torch.from_numpy(y_train).double()

        train_loader = infinite_dataloader(
            data_utils.DataLoader(
                data_utils.TensorDataset(x_train_, y_train_),
                batch_size=self.batch_size,
                shuffle=True
            )
        )

        if not continue_training:
            logging.debug("Clearing list of sampled weights.")

            self.sampled_weights.clear()
            self.model = self.get_network(input_dimensionality=input_dimensionality).double()

        if self.sampling_method == "adaptive_sghmc":
            sampler = AdaptiveSGHMC(self.model.parameters(),
                                    scale_grad=num_datapoints,
                                    num_burn_in_steps=num_burn_in_steps,
                                    lr=np.float64(np.sqrt(lr)),
                                    mdecay=np.float64(mdecay),
                                    noise=np.float64(noise))
        elif self.sampling_method == "sgld":
            sampler = SGLD(self.model.parameters(),
                           lr=np.float64(lr),
                           scale_grad=num_datapoints)
        elif self.sampling_method == "preconditioned_sgld":
            sampler = PreconditionedSGLD(self.model.parameters(),
                                         lr=np.float64(lr),
                                         num_train_points=num_datapoints)
        elif self.sampling_method == "sghmc":
            sampler = SGHMC(self.model.parameters(),
                            scale_grad=num_datapoints,
                            mdecay=np.float64(mdecay),
                            lr=np.float64(lr))
        elif self.sampling_method == "constant_sgd":
            sampler = ConstantSGD(self.model.parameters(),
                                  batch_size=self.batch_size,
                                  num_data_points=num_datapoints)

        elif self.sampling_method == "sghmchd":
            sampler = SGHMCHD(self.model.parameters(),
                              num_burn_in_steps=num_burn_in_steps,
                              lr=np.float64(lr), hyper_lr=1e-3,
                              scale_grad=num_datapoints)

        batch_generator = islice(enumerate(train_loader), num_steps)

        # from torch.optim.lr_scheduler import CosineAnnealingLR
        # scheduler = CosineAnnealingLR(sampler, T_max=num_steps)

        for step, (x_batch, y_batch) in batch_generator:
            sampler.zero_grad()

            loss = nll(input=self.model(x_batch), target=y_batch)
            # loss -= log_variance_prior(self.model(x_batch)[:, 1].view((-1, 1))) / num_datapoints
            # loss -= weight_prior(self.model.parameters()).double() / num_datapoints

            loss.backward()
            sampler.step()
            # scheduler.step()

            if verbose and step < num_burn_in_steps and step % 512 == 0:
                total_nll = torch.mean(nll(self.model(x_train_), y_train_)).data.numpy()
                total_err = torch.mean((self.model(x_train_)[:, 0] - y_train_) ** 2).data.numpy()
                t = time.time() - start_time
                print("Step {:8d} : NLL = {:11.4e} MSE = {:.4e} "
                      "Time = {:5.2f}".format(step, float(total_nll),
                                              float(total_err), t))

            if verbose and step > num_burn_in_steps and step % 512 == 0:
                total_nll = torch.mean(nll(self.model(x_train_), y_train_)).data.numpy()
                total_err = torch.mean((self.model(x_train_)[:, 0] - y_train_) ** 2).data.numpy()
                t = time.time() - start_time

                print("Step {:8d} : NLL = {:11.4e} MSE = {:.4e} "
                      "Samples= {} Time = {:5.2f}".format(step,
                                                          float(total_nll),
                                                          float(total_err),
                                                          len(self.sampled_weights), t))

            if step > num_burn_in_steps and (step - num_burn_in_steps) % keep_every == 0:
                logging.debug("Recording sample, step = %d " % step)
                weights = self.network_weights
                logging.debug("Sampled weights:\n%s" % str(weights))

                self.sampled_weights.append(weights)

        self.is_trained = True

    def train_and_evaluate(self, x_train: np.ndarray, y_train: np.ndarray,
                           x_valid: np.ndarray, y_valid: np.ndarray,
                           num_steps: int = 13000,
                           validate_every_n_steps=1000,
                           keep_every: int = 100,
                           num_burn_in_steps: int = 3000,
                           lr: float = 1e-2,
                           noise: float = 0.,
                           mdecay: float = 0.05,
                           verbose=False):

        # burn-in
        self.train(x_train, y_train, num_burn_in_steps=num_burn_in_steps, num_steps=num_burn_in_steps,
                   lr=lr, noise=noise, mdecay=mdecay, verbose=verbose)

        learning_curve_mse = []
        learning_curve_ll = []
        n_steps = []
        for i in range(num_steps // validate_every_n_steps):
            self.train(x_train, y_train, num_burn_in_steps=0, num_steps=validate_every_n_steps,
                       lr=lr, noise=noise, mdecay=mdecay, verbose=verbose, keep_every=keep_every,
                       continue_training=True)
            mu, var = self.predict(x_valid)

            ll = np.mean(norm.logpdf(y_valid, loc=mu, scale=np.sqrt(var)))
            mse = np.mean((y_valid - mu) ** 2)
            step = num_burn_in_steps + (i + 1) * validate_every_n_steps

            learning_curve_ll.append(ll)
            learning_curve_mse.append(mse)
            n_steps.append(step)

            if verbose:
                print("Validate : LL = {:11.4e} MSE = {:.4e}".format(ll, mse))

        return n_steps, learning_curve_ll, learning_curve_mse

    def normalize_input(self, x, m=None, s=None):
        return zero_mean_unit_var_normalization(x, m, s)

    def normalize_output(self, x, m=None, s=None):
        return zero_mean_unit_var_normalization(x, m, s)

    def predict(self, x_test: np.ndarray, return_individual_predictions: bool = False):
        x_test_ = np.asarray(x_test)

        if self.do_normalize_input:
            x_test_, *_ = self.normalize_input(x_test_, self.x_mean, self.x_std)

        def network_predict(x_test_, weights):
            with torch.no_grad():
                self.network_weights = weights
                return self.model(torch.from_numpy(x_test_).double()).numpy()

        logging.debug("Predicting with %d networks." % len(self.sampled_weights))
        network_outputs = np.array([
            network_predict(x_test_, weights=weights)
            for weights in self.sampled_weights
        ])

        mean_prediction = np.mean(network_outputs[:, :, 0], axis=0)
        variance_prediction = np.mean((network_outputs[:, :, 0] - mean_prediction) ** 2, axis=0)
        # Total variance
        # variance_prediction = np.mean(network_outputs[:, :, 0] ** 2 + np.exp(network_outputs[:, :, 1]),
        #                               axis=0) - mean_prediction ** 2

        if self.do_normalize_output:

            mean_prediction = zero_mean_unit_var_unnormalization(
                mean_prediction, self.y_mean, self.y_std
            )
            variance_prediction *= self.y_std ** 2

            for i in range(len(network_outputs)):
                network_outputs[i] = zero_mean_unit_var_unnormalization(
                    network_outputs[i], self.y_mean, self.y_std
                )

        if return_individual_predictions:
            return mean_prediction, variance_prediction, network_outputs[:, :, 0]
        return mean_prediction, variance_prediction

    def f_gradient(self, x_test, weights):
        x_test_ = np.asarray(x_test)

        with torch.no_grad():
            self.network_weights = weights

        x = torch.autograd.Variable(torch.from_numpy(x_test_[None, :]).double(), requires_grad=True)

        if self.do_normalize_input:
            x_mean = torch.autograd.Variable(torch.from_numpy(self.x_mean).double(), requires_grad=False)
            x_std = torch.autograd.Variable(torch.from_numpy(self.x_std).double(), requires_grad=False)
            x_norm = (x - x_mean) / x_std
            m = self.model(x_norm)[0][0]
        else:
            m = self.model(x)[0][0]
        if self.normalize_output:
            y_mean = torch.autograd.Variable(torch.from_numpy(np.array([self.y_mean])).double(),
                                             requires_grad=False)
            y_std = torch.autograd.Variable(torch.from_numpy(np.array([self.y_std])).double(), requires_grad=False)
            m = m * y_std + y_mean

        m.backward()

        g = x.grad.data.numpy()[0, :]
        return g

    def predictive_mean_gradient(self, x_test: np.ndarray):

        grads = np.array([self.f_gradient(x_test, weights=weights) for weights in self.sampled_weights])

        g = np.mean(grads, axis=0)

        return g

    def predictive_variance_gradient(self, x_test: np.ndarray):
        m, v, funcs = self.predict(x_test[None, :], return_individual_predictions=True)

        grads = np.array([self.f_gradient(x_test, weights=weights) for weights in self.sampled_weights])

        dmdx = self.predictive_mean_gradient(x_test)

        g = np.mean([2 * (funcs[i] - m) * (grads[i] - dmdx) for i in range(len(self.sampled_weights))], axis=0)

        return g
