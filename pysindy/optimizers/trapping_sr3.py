# import time
import warnings

import cvxpy as cp
import numpy as np
from scipy.linalg import cho_factor
from scipy.linalg import cho_solve
from sklearn.exceptions import ConvergenceWarning

from ..utils import get_prox
from ..utils import get_regularization
from ..utils import reorder_constraints
from .sr3 import SR3


class TrappingSR3(SR3):
    """
    Trapping variant of sparse relaxed regularized regression.

    Attempts to minimize the objective function

    .. math::

        0.5\\|y-Xw\\|^2_2 + \\lambda \\times R(w)
        + 0.5\\|Pw-A\\|^2_2/\\eta + \\delta_0(Cw-d)
        + \\delta_{\\Lambda}(A)}

    where :math:`R(w)` is a regularization function.
    See the following references for more details:

        Kaptanoglu, Alan A., et al. "Promoting global stability in
        data-driven models of quadratic nonlinear dynamics."
        arXiv preprint arXiv:2105.01843 (2021).

        Zheng, Peng, et al. "A unified framework for sparse relaxed
        regularized regression: Sr3." IEEE Access 7 (2018): 1404-1423.

        Champion, Kathleen, et al. "A unified sparse optimization framework
        to learn parsimonious physics-informed models from data."
        arXiv preprint arXiv:1906.10612 (2019).

    Parameters
    ----------
    evolve_w : bool, optional (default True)
        If false, don't update w and just minimize over (m, A)

    threshold : float, optional (default 0.1)
        Determines the strength of the regularization. When the
        regularization function R is the L0 norm, the regularization
        is equivalent to performing hard thresholding, and lambda
        is chosen to threshold at the value given by this parameter.
        This is equivalent to choosing lambda = threshold^2 / (2 * nu).

    eta : float, optional (default 1.0e20)
        Determines the strength of the stability term ||Pw-A||^2 in the
        optimization. The default value is to ignore the stability term.

    alpha_m : float, optional (default 5e19)
        Determines the step size in the prox-gradient descent over m.
        For convergence, need alpha_m <= eta / ||w^T * PQ^T * PQ * w||.
        Typically 0.01 * eta <= alpha_m <= 0.1 * eta.

    alpha_A : float, optional (default 1.0e20)
        Determines the step size in the prox-gradient descent over A.
        For convergence, need alpha_A <= eta, so typically
        alpha_A = eta is used.

    gamma : float, optional (default 0.1)
        Determines the negative interval that matrix A is projected onto.
        For most applications gamma = 0.1 - 1.0 works pretty well.

    tol : float, optional (default 1e-5)
        Tolerance used for determining convergence of the optimization
        algorithm over w.

    tol_m : float, optional (default 1e-5)
        Tolerance used for determining convergence of the optimization
        algorithm over m.

    thresholder : string, optional (default 'L1')
        Regularization function to use. For current trapping SINDy,
        only the L1 norm is implemented. Note that other convex norms
        could be straightforwardly implemented, but L0 requires
        reformulation because of nonconvexity.

    eps_solver : float, optional
        If threshold != 0.0, this specifies the error tolerance in the
        CVXPY (OSQP) solve. Default is 1.0e-3 in OSQP.

    relax_optim : bool, optional
        If relax_optim = True, use the relax-and-split method. If False,
        try a direct minimization on the largest eigenvalue.

    max_iter : int, optional (default 30)
        Maximum iterations of the optimization algorithm.

    accel : bool, optional (default False)
        Whether or not to use accelerated prox-gradient descent for (m, A).

    initial_guess : np.ndarray, shape (n_features) or (n_targets, n_features),
            optional (default None)
        Initial guess for coefficients ``coef_``.
        If None, least-squares is used to obtain an initial guess.

    m0 : np.ndarray, shape (n_targets), optional (default None)
        Initial guess for vector m in the optimization. Otherwise
        each component of m is randomly initialized in [-1, 1].

    A0 : np.ndarray, shape (n_targets, n_targets), optional (default None)
        Initial guess for vector A in the optimization. Otherwise
        A is initialized as A = diag(gamma).

    PL : np.ndarray, shape (n_targets, n_targets, n_targets, n_features)
        Linear coefficient part of the P matrix in ||Pw - A||^2

    PQ : np.ndarray, shape (n_targets, n_targets, n_targets, n_targets, n_features)
        Quadratic coefficient part of the P matrix in ||Pw - A||^2

    fit_intercept : boolean, optional (default False)
        Whether to calculate the intercept for this model. If set to false, no
        intercept will be used in calculations.

    normalize : boolean, optional (default False)
        This parameter is ignored when fit_intercept is set to False. If True,
        the regressors X will be normalized before regression by subtracting
        the mean and dividing by the L2-norm.

    copy_X : boolean, optional (default True)
        If True, X will be copied; else, it may be overwritten.

    Attributes
    ----------
    coef_ : array, shape (n_features,) or (n_targets, n_features)
        Regularized weight vector(s). This is the v in the objective
        function.

    history_ : list
        History of sparse coefficients. ``history_[k]`` contains the
        sparse coefficients (v in the optimization objective function)
        at iteration k.

    Examples
    --------
    >>> import numpy as np
    >>> from scipy.integrate import odeint
    >>> from pysindy import SINDy
    >>> from pysindy.optimizers import TrappingSR3
    >>> lorenz = lambda z,t : [10*(z[1] - z[0]),
    >>>                        z[0]*(28 - z[2]) - z[1],
    >>>                        z[0]*z[1] - 8/3*z[2]]
    >>> t = np.arange(0,2,.002)
    >>> x = odeint(lorenz, [-8,8,27], t)
    >>> opt = TrappingSR3(threshold=0.1)
    >>> model = SINDy(optimizer=opt)
    >>> model.fit(x, t=t[1]-t[0])
    >>> model.print()
    x0' = -10.004 1 + 10.004 x0
    x1' = 27.994 1 + -0.993 x0 + -1.000 1 x1
    x2' = -2.662 x1 + 1.000 1 x0
    """

    def __init__(
        self,
        evolve_w=True,
        threshold=0.1,
        eps_solver=1.0e-7,
        relax_optim=True,
        eta=1.0e20,
        alpha_A=1.0e20,
        alpha_m=5e19,
        gamma=-0.1,
        tol=1e-5,
        tol_m=1e-5,
        thresholder="l1",
        max_iter=30,
        accel=False,
        normalize=False,
        fit_intercept=False,
        copy_X=True,
        initial_guess=None,
        m0=None,
        A0=None,
        PL=None,
        PQ=None,
        thresholds=None,
        objective_history=None,
        constraint_lhs=None,
        constraint_rhs=None,
        constraint_order="target",
    ):
        super(TrappingSR3, self).__init__(
            max_iter=max_iter,
            initial_guess=initial_guess,
            normalize=normalize,
            fit_intercept=fit_intercept,
            copy_X=copy_X,
        )

        if threshold < 0:
            raise ValueError("threshold cannot be negative")
        if eta <= 0:
            raise ValueError("eta must be positive")
        if alpha_m < 0:
            raise ValueError("alpha_m must be positive")
        if alpha_A < 0:
            raise ValueError("alpha_A must be positive")
        if gamma >= 0:
            raise ValueError("gamma must be negative")
        if tol <= 0 or tol_m <= 0 or eps_solver <= 0:
            raise ValueError("tol and tol_m must be positive")

        self.evolve_w = evolve_w
        self.threshold = threshold
        self.eps_solver = eps_solver
        self.relax_optim = relax_optim
        self.m0 = m0
        self.A0 = A0
        self.thresholds = thresholds
        self.alpha_A = alpha_A
        self.alpha_m = alpha_m
        self.eta = eta
        self.gamma = gamma
        self.PL = PL
        self.PQ = PQ
        self.tol = tol
        self.tol_m = tol_m
        self.accel = accel
        self.thresholder = thresholder
        self.reg = get_regularization(thresholder)
        self.prox = get_prox(thresholder)
        self.A_history_ = []
        self.m_history_ = []
        self.PW_history_ = []
        self.PWeigs_history_ = []
        self.history_ = []
        self.objective_history = objective_history
        self.unbias = False
        self.use_constraints = (constraint_lhs is not None) and (
            constraint_rhs is not None
        )

        if self.use_constraints:
            if constraint_order not in ("feature", "target"):
                raise ValueError(
                    "constraint_order must be either 'feature' or 'target'"
                )

            self.constraint_lhs = constraint_lhs
            self.constraint_rhs = constraint_rhs
            self.unbias = False
            self.constraint_order = constraint_order

    def _update_coef_constraints(self, H, x_transpose_y, P_transpose_A):
        g = x_transpose_y + P_transpose_A / self.eta
        inv1 = np.linalg.pinv(H, rcond=1e-15)
        inv2 = np.linalg.pinv(
            self.constraint_lhs.dot(inv1).dot(self.constraint_lhs.T), rcond=1e-15
        )

        rhs = g.flatten() + self.constraint_lhs.T.dot(inv2).dot(
            self.constraint_rhs - self.constraint_lhs.dot(inv1).dot(g.flatten())
        )
        rhs = rhs.reshape(g.shape)
        return inv1.dot(rhs)

    def _update_A(self, A_old, PW):
        """Update the symmetrized A matrix"""
        eigvals, eigvecs = np.linalg.eigh(A_old)
        eigPW, eigvecsPW = np.linalg.eigh(PW)
        r = A_old.shape[0]
        A = np.diag(eigvals)
        for i in range(r):
            if eigvals[i] > self.gamma:
                A[i, i] = self.gamma
        return eigvecsPW @ A @ np.linalg.inv(eigvecsPW)

    def _convergence_criterion(self):
        """Calculate the convergence criterion for the optimization over w"""
        this_coef = self.history_[-1]
        if len(self.history_) > 1:
            last_coef = self.history_[-2]
        else:
            last_coef = np.zeros_like(this_coef)
        err_coef = np.sqrt(np.sum((this_coef - last_coef) ** 2))
        return err_coef

    def _m_convergence_criterion(self):
        """Calculate the convergence criterion for the optimization over m"""
        return np.sum(np.abs(self.m_history_[-2] - self.m_history_[-1]))

    def _objective(self, x, y, coef_sparse, A, PW, q):
        """Objective function"""

        # Compute the errors
        R2 = (y - np.dot(x, coef_sparse)) ** 2
        A2 = (A - PW) ** 2
        L1 = self.threshold * np.sum(np.abs(coef_sparse.flatten()))

        # convoluted way to print every max_iter / 10 iterations
        if q % max(int(self.max_iter / 10.0), 1) == 0 or self.threshold != 0.0:
            row = [q, 0.5 * np.sum(R2), 0.5 * np.sum(A2) / self.eta, L1]
            print("{0:12d} {1:12.5e} {2:12.5e} {3:12.5e}".format(*row))
        return 0.5 * np.sum(R2) + 0.5 * np.sum(A2) / self.eta + L1

    def _reduce(self, x, y):
        """
        Perform at most ``self.max_iter`` iterations of the
        TrappingSR3 algorithm.
        Assumes initial guess for coefficients is stored in ``self.coef_``.
        """

        n_samples, n_features = x.shape
        r = (self.PL).shape[0]
        N = n_features

        # Set initial coefficients
        if self.initial_guess is not None:
            self.coef_ = self.initial_guess
        if self.use_constraints and self.constraint_order.lower() == "target":
            self.constraint_lhs = reorder_constraints(self.constraint_lhs, n_features)

        coef_sparse = self.coef_.T

        # Print initial values for each term in the optimization
        row = ["Iteration", "Data Error", "Stability Error", "L1 Error"]
        print("{: >10} | {: >10} | {: >10} | {: >10}".format(*row))

        # initial A
        if self.A0 is not None:
            A = self.A0
        else:
            A = np.diag(self.gamma * np.ones(r))
        self.A_history_.append(A)

        # initial guess for m
        if self.m0 is not None:
            m = self.m0
        else:
            np.random.seed(1)
            m = (np.random.rand(r) - np.ones(r)) * 2
        self.m_history_.append(m)

        # Precompute some objects for optimization
        PL = self.PL
        PQ = self.PQ
        mPQ = np.tensordot(m, PQ, axes=([0], [0]))
        p = PL - mPQ
        x_expanded = np.zeros((n_samples, r, n_features, r))
        for i in range(r):
            x_expanded[:, i, :, i] = x
        x_expanded = np.reshape(x_expanded, (n_samples * r, r * n_features))
        xTx = np.dot(x_expanded.T, x_expanded)
        xTy = np.dot(x_expanded.T, y.flatten())
        Pmatrix = p.reshape(r * r, r * N)

        # if using acceleration
        tk_prev = 1
        m_prev = m

        # Begin optimization loop
        objective_history = []
        for k in range(self.max_iter):

            # update P tensor from the newest m
            mPQ = np.tensordot(m, PQ, axes=([0], [0]))
            p = PL - mPQ
            Pmatrix = p.reshape(r * r, r * N)

            # update w
            if self.evolve_w:
                # Define and solve the CVXPY problem if threshold is nonzero.
                if self.relax_optim:
                    if self.threshold > 0.0:
                        xi = cp.Variable(N * r)
                        cost = cp.sum_squares(
                            x_expanded @ xi - y.flatten()
                        ) + self.threshold * cp.norm1(xi)
                        cost = (
                            cost + cp.sum_squares(Pmatrix @ xi - A.flatten()) / self.eta
                        )
                        if self.use_constraints:
                            prob = cp.Problem(
                                cp.Minimize(cost),
                                [self.constraint_lhs @ xi == self.constraint_rhs],
                            )
                        else:
                            prob = cp.Problem(cp.Minimize(cost))

                        # default solver is OSQP here
                        prob.solve(eps_abs=self.eps_solver, eps_rel=self.eps_solver)

                        if xi.value is None:
                            warnings.warn(
                                "Infeasible solve, increase/decrease eta",
                                ConvergenceWarning
                            )
                            break
                        coef_sparse = (xi.value).reshape(coef_sparse.shape)
                    else:
                        pTp = np.dot(Pmatrix.T, Pmatrix)
                        H = xTx + pTp / self.eta
                        P_transpose_A = np.dot(Pmatrix.T, A.flatten())
                        if self.use_constraints:
                            coef_sparse = self._update_coef_constraints(
                                H, xTy, P_transpose_A
                            ).reshape(coef_sparse.shape)
                        else:
                            cho = cho_factor(H)
                            coef_sparse = cho_solve(
                                cho, xTy + P_transpose_A / self.eta
                            ).reshape(coef_sparse.shape)
                else:
                    xi = cp.Variable(N * r)
                    cost = cp.sum_squares(
                        x_expanded @ xi - y.flatten()
                    ) + self.threshold * cp.norm1(xi)
                    cost = (
                        cost
                        + cp.lambda_max(cp.reshape(Pmatrix @ xi, (r, r))) / self.eta
                    )
                    if self.use_constraints:
                        prob = cp.Problem(
                            cp.Minimize(cost),
                            [self.constraint_lhs @ xi == self.constraint_rhs],
                        )
                    else:
                        prob = cp.Problem(cp.Minimize(cost))

                    # default solver is SCS here I think
                    prob.solve(eps=self.eps_solver)

                    if xi.value is None:
                        print("Infeasible solve, increase/decrease eta")
                        break
                    coef_sparse = (xi.value).reshape(coef_sparse.shape)

                    m_cp = cp.Variable(r)
                    L = np.tensordot(PL, coef_sparse, axes=([3, 2], [0, 1]))
                    Q = np.reshape(
                        np.tensordot(PQ, coef_sparse, axes=([4, 3], [0, 1])), (r, r * r)
                    )
                    Ls = 0.5 * (L + L.T).flatten()
                    cost_m = cp.lambda_max(cp.reshape(Ls - m_cp @ Q, (r, r)))
                    prob_m = cp.Problem(cp.Minimize(cost_m))

                    # default solver is SCS here
                    prob_m.solve(eps=self.eps_solver)

                    m = m_cp.value
                    if m is None:
                        print("Infeasible solve over m, increase/decrease eta")
                        break
            self.history_.append(coef_sparse.T)

            if self.relax_optim:
                # prox-grad for (A, m)
                # Accelerated prox gradient descent
                if self.accel:
                    tk = (1 + np.sqrt(1 + 4 * tk_prev ** 2)) / 2.0
                    m_partial = m + (tk_prev - 1.0) / tk * (m - m_prev)
                    tk_prev = tk
                    mPQ = np.tensordot(m_partial, PQ, axes=([0], [0]))
                    p = PL - mPQ
                    Pmatrix = p.reshape(r * r, r * N)
                PW = np.tensordot(p, coef_sparse, axes=([3, 2], [0, 1]))
                PQW = np.tensordot(PQ, coef_sparse, axes=([4, 3], [0, 1]))
                A_b = (A - PW) / self.eta
                PQWT_PW = np.tensordot(PQW, A_b, axes=([2, 1], [0, 1]))
                if self.accel:
                    m = m_partial - self.alpha_m * PQWT_PW
                else:
                    m = m_prev - self.alpha_m * PQWT_PW
                m_prev = m

                # Update A
                A = self._update_A(A - self.alpha_A * A_b, PW)
            else:
                PW = np.tensordot(p, coef_sparse, axes=([3, 2], [0, 1]))

            # (m,A) update finished, append the result
            self.m_history_.append(m)
            self.A_history_.append(A)
            eigvals, eigvecs = np.linalg.eig(PW)
            self.PW_history_.append(PW)
            self.PWeigs_history_.append(np.sort(eigvals))

            # update objective
            objective_history.append(self._objective(x, y, coef_sparse, A, PW, k))

            if (
                self._m_convergence_criterion() < self.tol_m
                and self._convergence_criterion() < self.tol
            ):
                # Could not (further) select important features
                break
        else:
            warnings.warn(
                "TrappingSR3._reduce did not converge after {} iterations.".format(
                    self.max_iter
                ),
                ConvergenceWarning,
            )

        if self.use_constraints and self.constraint_order.lower() == "target":
            self.constraint_lhs = reorder_constraints(
                self.constraint_lhs, n_features, output_order="target"
            )

        self.coef_ = coef_sparse.T
        self.objective_history = objective_history
