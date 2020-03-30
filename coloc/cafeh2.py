import numpy as np
from scipy.stats import norm
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

from .kls import unit_normal_kl, normal_kl, categorical_kl, bernoulli_kl
import os, sys, pickle
from scipy.optimize import minimize_scalar
from .utils import np_cache_class, gamma_logpdf
from functools import lru_cache
import time

class CAFEH2:
    from .plotting import plot_components, plot_assignment_kl, plot_credible_sets_ld, plot_decomposed_zscores, plot_pips
    from .model_queries import get_credible_sets, get_pip, check_convergence, get_expected_weights

    def __init__(self, R1, R2, Y, K, prior_activity=1.0, prior_variance=1.0, prior_pi=None, snp_ids=None, tissue_ids=None, tolerance=1e-5):
        """
        X [N x N] covariance matrix
            if X is [T x N x N] use seperate embedding for each tissue
        Y [T x N] matrix, Nans should be converted to 0s?
        """

        # precompute svd, need for computing elbo
        T, N = Y.shape

        self.R1 = R1
        self.R2 = R2
        self.Y = Y
        self.dims = {'N': N, 'T': T, 'K': K}

        # priors
        self.prior_precision = np.ones((T, K)) * prior_variance
        self.prior_component_precision = np.ones(K)
        self.prior_activity = np.ones(K) * prior_activity
        self.prior_pi = prior_pi  if (prior_pi is not None) else np.ones(N) / N

        # ids
        self.tissue_ids = tissue_ids if (tissue_ids is not None) else np.arange(T)
        self.snp_ids = snp_ids if (snp_ids is not None) else np.arange(N)

        # initialize variational parameters
        self.pi = np.ones((K, N)) / N
        self.weight_means = np.zeros((T, K, N))

        prior_variance = 1 / (self.prior_precision * self.prior_component_precision)
        self.weight_vars = (prior_variance / (prior_variance + 1))[:, :, None] * np.ones((T, K, N))
        self.active = np.ones((T, K))

        self.elbos = []
        self.tolerance = tolerance
        self.run_time = 0

        self.alpha0 = 1.0
        self.beta0 = 1e-10

        self.alpha0_component = 1.0
        self.beta0_component = 1.0

        self.R2invR1 = np.linalg.solve(self.R2, self.R1)
        self.R1R2invR1 = self.R1 @ self.R2invR1

    ################################
    # UPDATE AND FITTING FUNCTIONS #
    ################################
    def prior_variance(self):
        """
        return prior variance
        """
        return 1 / (self.prior_precision * self.prior_component_precision)

    @np_cache_class(maxsize=128)
    def _compute_prediction_component(self, active, pi, weights):
        if np.ndim(self.R1) == 2:
            return active[:, None] * (pi * weights) @ self.R1
        else:
            return active[:, None] * np.einsum(
                'tn, tnm->tm', (pi * weights), self.R1)

    @lru_cache(maxsize=128)
    def _compute_prediction_component_hash(self, k, hash):
        active= self.active[:, k]
        pi = self.pi[k]
        weights = self.weight_means[:, k]
        if np.ndim(self.R1) == 2:
            return active[:, None] * (pi * weights) @ self.R1
        else:
            return active[:, None] * np.einsum(
                'tn, tnm->tm', (pi * weights), self.R1)

    def compute_prediction_component(self, k):
        """
        active= self.active[:, k]
        pi = self.pi[k]
        weights = self.weight_means[:, k]
        return self._compute_prediction_component(active, pi, weights)
        """
        h = (self.pi[k] @ self.weight_means[:, k].T).tobytes()
        return self._compute_prediction_component_hash(k, h)

    def compute_prediction(self, k=None):
        prediction = np.zeros_like(self.Y)
        for l in range(self.dims['K']):
            prediction += self.compute_prediction_component(l)
        if k is not None:
            prediction -= self.compute_prediction_component(k)
        return prediction

    def compute_residual(self, k=None):
        """
        residual computation, works when X is 2d or 3d
        k is a component to exclude from residual computation
        """
        prediction = self.compute_prediction(k)
        residual = self.Y - prediction
        return residual

    def _update_weight_component(self, k, ARD=False, residual=None):
        if residual is None:
            r_k = self.compute_residual(k)
        else:
            r_k = residual
        if ARD:
            second_moment = (self.weight_vars[:, k] + self.weight_means[:, k] **2) @ self.pi[k]
            alpha = self.alpha0 + 0.5
            beta = self.beta0 + second_moment / 2 * self.prior_component_precision[k]
            self.prior_precision[:, k] = np.clip((alpha - 1) / beta, 1e-10, 1e5)

        for tissue in range(self.dims['T']):
            precision = np.diag(self.R1R2invR1) + (1 / self.prior_variance()[tissue, k])
            variance = 1 / precision
            mean = (r_k[tissue] @ self.R2invR1) * variance

            self.weight_vars[tissue, k] = variance
            self.weight_means[tissue, k] = mean

    def update_weights(self, components=None, ARD=False):
        """
        X is LD/Covariance Matrix
        Y is T x N
        weights  T x K matrix of weight parameters
        active T x K active[t, k] = logp(s_tk = 1)
        prior_activitiy
        """
        if components is None:
            components = np.arange(self.dims['K'])

        for k in components:
            self._update_weight_component(k, ARD=ARD)

    def _update_pi_component(self, k, ARD=False, residual=None):
        if residual is None:
            r_k = self.compute_residual(k)
        else:
            r_k = residual

        pi_k = ((r_k @ self.R2invR1) * self.weight_means[:, k]
                - 0.5 * (self.weight_means[:, k] ** 2 + self.weight_vars[:, k]) * get_diag(self.R1R2invR1)
                - normal_kl(
                    self.weight_means[:, k], self.weight_vars[:, k],
                    0, self.prior_variance()[:, k][:, None] * np.ones_like(self.weight_vars[:, k]))
                )
        pi_k = pi_k.T @ self.active[:, k]

        # normalize to probabilities
        pi_k = np.exp(pi_k - pi_k.max())
        pi_k = pi_k / pi_k.sum()
        self.pi.T[:, k] = pi_k

    def update_pi(self, components=None):
        """
        update pi
        """
        if components is None:
            components = np.arange(self.dims['K'])

        for k in components:
            self._update_pi_component(k)

    def fit(self, max_iter=1000, verbose=False, components=None, update_weights=True, update_active=True, update_pi=True, ARD_weights=False, ARD_active=False):
        """
        loop through updates until convergence
        """
        init_time = time.time()

        if components is None:
            components = np.arange(self.dims['K'])

        residual = self.compute_residual()
        for i in range(max_iter):
            for l in components:
                residual = residual + self.compute_prediction_component(l)
                if update_weights:
                    self._update_weight_component(l, ARD=ARD_weights,
                                                  residual=residual)
                if update_pi:
                    self._update_pi_component(l, residual=residual)
                # if update_active: self._update_active_component(l, ARD=ARD_active)
                residual = residual - self.compute_prediction_component(l)

            self.elbos.append(self.compute_elbo())
            if verbose: print("Iter {}: {}".format(i, self.elbos[-1]))

            cur_time = time.time()
            if self.check_convergence():
                if verbose:
                    print('ELBO converged with tolerance {} at iter: {}'.format(self.tolerance, i))
                break

        self.run_time += cur_time - init_time
        if verbose:
            print('cumulative run time: {}'.format(self.run_time))

    def compute_elbo(self, active=None):
        bound = 0 
        if active is None:
            active = self.active

        expected_conditional = 0
        KL = 0

        
        # compute expected conditional log likelihood E[ln p(Y | X, Z)]
        for tissue in range(self.dims['T']):
            p = self.active[tissue] @ (self.pi * self.weight_means[tissue])
            expected_conditional += np.inner(self.Y[tissue] @ self.R2invR1, p)

            z = self.pi * self.weight_means[tissue] #* self.active[tissue]
            z = z @ self.R1R2invR1 @ z.T
            z = z - np.diag(np.diag(z))
            expected_conditional += -0.5 * z.sum()
            expected_conditional += -0.5 * ((self.weight_means[tissue] ** 2 + self.weight_vars[tissue]) * self.pi).sum()
        """

        # compute expected conditional log likelihood E[ln p(Y | X, Z)]
        for tissue in range(self.dims['T']):
            p = self.active[tissue] @ (self.pi * self.weight_means[tissue])
            expected_conditional += np.inner(self.Y[tissue] @ self.R2invR1, p)
            expected_conditional += -0.5 * p @ self.R1R2invR1 @ p
            
            z = self.pi * self.weight_means[tissue] #* cafeh2.active[tissue]
            a = z.T @ z + np.diag(((self.weight_means[tissue]**2 + self.weight_vars[tissue]) * self.pi).sum(0))
            b = np.trace(self.R1R2invR1 @ a)
            expected_conditional += -0.5 * b
        """

        # KL(q(W | S) || p(W)) = KL(q(W | S = 1) || p(W)) q(S = 1) + KL(p(W) || p(W)) (1 - q(S = 1))
        KL += np.sum(
            normal_kl(self.weight_means, self.weight_vars, 0, self.prior_variance()[..., None])
            * (self.active[..., None] * self.pi[None])
        )

        KL += np.sum(bernoulli_kl(self.active, self.prior_activity[None]))
        KL += np.sum(
            [categorical_kl(self.pi[k], self.prior_pi) for k in range(self.dims['K'])]
        )
        # TODO ADD lnp(prior_weight_variance) + lnp(prior_slab_weights)
        # expected_conditional += gamma_logpdf(self.prior_component_precision, self.alpha0_component, self.beta0_component).sum()
        expected_conditional += gamma_logpdf(self.prior_precision, self.alpha0, self.beta0).sum()
        return expected_conditional - KL

    def get_ld(self, tissue=None, snps=None):
        """
        get ld matrix
        this function gives a common interface to
        (tisse, snp, snp) and (snp, snp) ld
        """
        cov = self.R1 # elf.get_cov(tissue=tissue, snps=snps)
        """
        ld = []
        for c in np.atleast_3d(cov):
            std = np.sqrt(np.diag(c))
            ld.append(c / np.outer(std, std))
        return np.clip(np.squeeze(ld), -1, 1)
        """
        return cov[snps][:, snps]

    def get_cov(self, tissue=None, snps=None):
        """
        get ld matrix
        this function gives a common interface to
        (tisse, snp, snp) and (snp, snp) ld
        """
        if np.ndim(self.R2) == 2:
            tissue = None
        return np.squeeze(self.R2[tissue][..., snps, :][..., snps])

    def sort_components(self):
        """
        reorder components so that components with largest weights come first
        """
        order = np.flip(np.argsort(np.abs(self.get_expected_weights()).max(0)))
        self.weight_means = self.weight_means[:, order]
        self.active = self.active[:, order]
        self.pi = self.pi[order]
        self._compute_prediction_component.cache_clear()

    def save(self, output_dir, model_name, save_data=False):
        if not os.path.isdir(output_dir):
            os.makedirs(output_dir)

        if output_dir[-1] == '/':
            output_dir = output_dir[:-1]

        if not save_data:
            R1 = self.__dict__.pop('R1')
            R2 = self.__dict__.pop('R2')
            Y = self.__dict__.pop('Y')
            
        pickle.dump(self.__dict__, open('{}/{}'.format(output_dir, model_name), 'wb'))

        if not save_data:
            self.__dict__['R1'] = R1
            self.__dict__['R2'] = R2
            self.__dict__['Y'] = Y


def get_diag(X):
    if np.ndim(X) == 2:
        return np.diag(X)[None]
    else:
        return np.array([np.diag(x) for x in X])