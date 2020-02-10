#
#   This file is part of do-mpc
#
#   do-mpc: An environment for the easy, modular and efficient implementation of
#        robust nonlinear model predictive control
#
#   Copyright (c) 2014-2019 Sergio Lucia, Alexandru Tatulea-Codrean
#                        TU Dortmund. All rights reserved
#
#   do-mpc is free software: you can redistribute it and/or modify
#   it under the terms of the GNU Lesser General Public License as
#   published by the Free Software Foundation, either version 3
#   of the License, or (at your option) any later version.
#
#   do-mpc is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU Lesser General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with do-mpc.  If not, see <http://www.gnu.org/licenses/>.

import numpy as np
from casadi import *
from casadi.tools import *
import pdb
import copy
from indexedproperty import IndexedProperty

import do_mpc.optimizer
import do_mpc.data


class Estimator:
    def __init__(self, model):
        self.model = model

        assert model.flags['setup'] == True, 'Model for estimator was not setup. After the complete model creation call model.setup_model().'

        self._x0 = model._x(0.0)
        self._u0 = model._u(0.0)
        self._z0 = model._z(0.0)
        self._t0 = np.array([0.0])

        self.data = do_mpc.data.Data(model)
        self.data.dtype = 'Estimator'


    def set_initial_state(self, x0, reset_history=False):
        """Set the intial state of the estimator.
        Optionally resets the history. The history is empty upon creation of the estimator.

        :param x0: Initial state
        :type x0: numpy array
        :param reset_history: Resets the history of the estimator, defaults to False
        :type reset_history: bool (,optional)

        :return: None
        :rtype: None
        """
        assert x0.size == self.model._x.size, 'Intial state cannot be set because the supplied vector has the wrong size. You have {} and the model is setup for {}'.format(x0.size, self.model._x.size)
        assert isinstance(reset_history, bool), 'reset_history parameter must be of type bool. You have {}'.format(type(reset_history))
        if isinstance(x0, (np.ndarray, casadi.DM)):
            self._x0 = self.model._x(x0)
        elif isinstance(x0, structure3.DMStruct):
            self._x0 = x0
        else:
            raise Exception('x0 must be of tpye (np.ndarray, casadi.DM, structure3.DMStruct). You have: {}'.format(type(x0)))

        if reset_history:
            self.reset_history()

    def reset_history(self):
        """Reset the history of the estimator
        """
        self.data.init_storage()


class StateFeedback(Estimator):
    def __init__(self, model):
        super().__init__(model)

    def make_step(self, y0):
        return y0

class EKF(Estimator):
    def __init__(self, model):
        raise Exception('EKF is not currently supported. This is a placeholder.')
        super().__init__(model)

    def make_step(self, y0):
        None

class MHE(do_mpc.optimizer.Optimizer, Estimator):
    def __init__(self, model, p_est_list=[]):
        Estimator.__init__(self, model)
        do_mpc.optimizer.Optimizer.__init__(self, model)

            # Parameters that can be set for the MHE:
        self.data_fields = [
            'n_horizon',
            't_step',
            'meas_from_data',
            'state_discretization',
            'collocation_type',
            'collocation_deg',
            'collocation_ni',
            'store_full_solution',
            'store_lagr_multiplier',
            'store_solver_stats',
            'nlpsol_opts'
        ]

        # Default Parameters:
        self.meas_from_data = False
        self.state_discretization = 'collocation'
        self.collocation_type = 'radau'
        self.collocation_deg = 2
        self.collocation_ni = 1
        self.store_full_solution = False
        self.store_lagr_multiplier = True
        self.store_solver_stats = [
            'success',
            't_wall_S',
            't_wall_S',
        ]
        self.nlpsol_opts = {} # Will update default options with this dict.


        # Create seperate structs for the estimated and the set parameters (the union of both are all parameters of the model.)
        _p = model._p
        self._p_est  = struct_symSX(
            [entry('default', shape=(0,0))]+
            [entry(p_i, sym=_p[p_i]) for p_i in _p.keys() if p_i in p_est_list]
        )
        self._p_set  = struct_symSX(
            [entry(p_i, sym=_p[p_i]) for p_i in _p.keys() if p_i not in p_est_list]
        )
        # Function to obtain full set of parameters from the seperate structs (while obeying the order):
        self._p_cat_fun = Function('p_cat_fun', [self._p_est, self._p_set], [_p])

        # Initialize structures for bounds, scaling, initial values by calling the symbolic structures defined above
        # with the default numerical value.
        # This returns an identical numerical structure with all values set to the passed value.
        self._p_est_scaling = self._p_est(1.0)

        self._p_est_lb = self._p_est(-np.inf)
        self._p_est_ub = self._p_est(np.inf)

        self._p_est0 = self._p_est(0.0)


        # Introduce aliases / new variables to smoothly and intuitively formulate
        # the MHE objective function.
        self._y_meas = self.model._y
        self._y_calc = self.model._y_expression

        self._x_prev = copy.copy(self.model._x)
        self._x = self.model._x

        self._p_prev = copy.copy(self._p_est)
        self._p_est = self._p_est

        # Flags are checked when calling .setup.
        self.flags = {
            'setup': False,
            'set_tvp_fun': False,
            'set_p_fun': False,
            'set_y_fun': False,
            'set_objective': False,
        }

    @IndexedProperty
    def vars(self, ind):
        if isinstance(ind, tuple):
            assert ind[0] in self.__dict__.keys(), '{} is not a MHE variable.'.format(ind[0])
            rval = self.__dict__[ind[0]][ind[1:]]
        elif isinstance(ind, str):
            assert ind in self.__dict__.keys(), '{} is not a MHE variable.'.format(ind)
            rval = self.__dict__[ind]
        else:
            raise Exception('Index {} is not valid.'.format(ind))
        return rval

    @vars.setter
    def vars(self, ind, val):
        raise Exception('Setting MHE variables is not allowed.')


    def set_param(self, **kwargs):
        """Method to set the parameters of the mhe class. Parameters must be passed as pairs of valid keywords and respective argument.
        For example:
        ::
            mhe.set_param(n_horizon = 20)

        It is also possible and convenient to pass a dictionary with multiple parameters simultaneously as shown in the following example:
        ::
            setup_optimizer = {
                'n_horizon': 20,
                't_step': 0.5,
            }
            mhe.set_param(**setup_optimizer)

        .. note:: :py:func:`mhe.set_param` can be called multiple times. Previously passed arguments are overwritten by successive calls.

        The following parameters are available:

        :param n_horizon: Prediction horizon of the optimal control problem. Parameter must be set by user.
        :type n_horizon: int

        :param t_step: Timestep of the mhe.
        :type t_step: float

        :param meas_from_data: Default option to retrieve past measurements for the MHE optimization problem.
        :type meas_from_data: bool

        :param state_discretization: Choose the state discretization for continuous models. Currently only ``'collocation'`` is available. Defaults to ``'collocation'``.
        :type state_discretization: str

        :param collocation_type: Choose the collocation type for continuous models with collocation as state discretization. Currently only ``'radau'`` is available. Defaults to ``'radau'``.
        :type collocation_type: str

        :param collocation_deg: Choose the collocation degree for continuous models with collocation as state discretization. Defaults to ``2``.
        :type collocation_deg: int

        :param collocation_ni: Choose the collocation ni for continuous models with collocation as state discretization. Defaults to ``1``.
        :type collocation_ni: int

        :param store_full_solution: Choose whether to store the full solution of the optimization problem. This is required for animating the predictions in post processing. However, it drastically increases the required storage. Defaults to False.
        :type store_full_solution: bool

        :param store_lagr_multiplier: Choose whether to store the lagrange multipliers of the optimization problem. Increases the required storage. Defaults to ``True``.
        :type store_lagr_multiplier: bool

        :param store_solver_stats: Choose which solver statistics to store. Must be a list of valid statistics. Defaults to ``['success','t_wall_S','t_wall_S']``.
        :type store_solver_stats: list

        :param nlpsol_opts: Dictionary with options for the CasADi solver call ``nlpsol`` with plugin ``ipopt``. All options are listed `here <http://casadi.sourceforge.net/api/internal/d4/d89/group__nlpsol.html>`_.
        :type store_solver_stats: dict

        .. note:: We highly suggest to change the linear solver for IPOPT from `mumps` to `MA27`. In many cases this will drastically boost the speed of **do mpc**. Change the linear solver with:
            ::
                optimizer.set_param(nlpsol_opts = {'ipopt.linear_solver': 'MA27'})
        .. note:: To surpress the output of IPOPT, please use:
            ::
                surpress_ipopt = {'ipopt.print_level':0, 'ipopt.sb': 'yes', 'print_time':0}
                optimizer.set_param(nlpsol_opts = surpress_ipopt)

        """
        assert self.flags['setup'] == False, 'Setting parameters after setup is prohibited.'

        for key, value in kwargs.items():
            if not (key in self.data_fields):
                print('Warning: Key {} does not exist for optimizer.'.format(key))
            else:
                setattr(self, key, value)

    def set_objective(self, obj, arrival_cost):
        assert obj.shape == (1,1), 'obj must have shape=(1,1). You have {}'.format(obj.shape)
        assert arrival_cost.shape == (1,1), 'arrival_cost must have shape=(1,1). You have {}'.format(arrival_cost.shape)
        assert self.flags['setup'] == False, 'Cannot call .set_objective after .setup.'


        obj_input = self.model._x, self.model._u, self.model._z, self.model._tvp, self.model._p, self._y_meas
        assert set(symvar(obj)).issubset(set(symvar(vertcat(*obj_input)))), 'objective cost equation must be solely depending on x, u, z, p, tvp, y_meas.'
        self.obj_fun = Function('obj_fun', [*obj_input], [obj])

        arrival_cost_input = self._x, self._x_prev, self._p_est, self._p_prev
        assert set(symvar(arrival_cost)).issubset(set(symvar(vertcat(*arrival_cost_input)))), 'Arrival cost equation must be solely depending on x_0, x_prev, p_0, p_prev.'
        self.arrival_cost_fun = Function('arrival_cost_fun', arrival_cost_input, [arrival_cost])

        self.flags['set_objective'] = True

    def get_p_template(self):
        """docstring
        """
        return self._p_set(0)

    def set_p_fun(self, p_fun):
        """docstring
        """
        assert self.get_p_template().labels() == p_fun(0).labels(), 'Incorrect output of p_fun. Use get_p_template to obtain the required structure.'
        self.p_fun = p_fun
        self.flags['set_p_fun'] = True

    def get_y_template(self):
        y_template = struct_symSX([
            entry('y_meas', repeat=self.n_horizon, struct=self._y_meas)
        ])
        return y_template(0)

    def set_y_fun(self, y_fun):
        assert self.get_y_template().labels() == y_fun(0).labels(), 'Incorrect output of y_fun. Use get_y_template to obtain the required structure.'
        self.y_fun = y_fun
        self.flags['set_y_fun'] = True


    def check_validity(self):
        # Objective mus be defined.
        if self.flags['set_objective'] == False:
            raise Exception('Objective is undefined. Please call .set_objective() prior to .setup().')

        # tvp_fun must be set, if tvp are defined in model.
        if self.flags['set_tvp_fun'] == False and self.model._tvp.size > 0:
            raise Exception('You have not supplied a function to obtain the time varying parameters defined in model. Use .set_tvp_fun() prior to setup.')
        # p_fun must be set, if p are defined in model.
        if self.flags['set_p_fun'] == False and self._p_set.size > 0:
            raise Exception('You have not supplied a function to obtain the parameters defined in model. Use .set_p_fun() (low-level API) or .set_uncertainty_values() (high-level API) prior to setup.')


        # Lower bounds should be lower than upper bounds:
        for lb, ub in zip([self._x_lb, self._u_lb, self._z_lb], [self._x_ub, self._u_ub, self._z_ub]):
            bound_check = lb.cat > ub.cat
            bound_fail = [label_i for i,label_i in enumerate(lb.labels()) if bound_check[i]]
            if np.any(bound_check):
                raise Exception('Your bounds are inconsistent. For {} you have lower bound > upper bound.'.format(bound_fail))

        # Set dummy functions for tvp and p in case these parameters are unused.
        if 'tvp_fun' not in self.__dict__:
            _tvp = self.get_tvp_template()

            def tvp_fun(t): return _tvp
            self.set_tvp_fun(tvp_fun)

        if 'p_fun' not in self.__dict__:
            _p = self.get_p_template()

            def p_fun(t): return _p
            self.set_p_fun(p_fun)

        if self.flags['set_y_fun'] == False and self.meas_from_data:
            y_template = self.get_y_template()

            def y_fun(t_now):
                n_steps = min(self.data._y.shape[0], self.n_horizon)
                for k in range(-n_steps,0):
                    y_template['y_meas',k] = self.data._y[k]
                try:
                    for k in range(self.n_horizon-n_steps):
                        y_template['y_meas',k] = self.data._y[-n_steps]
                except:
                    None
                return y_template
            self.set_y_fun(y_fun)
        else:
            raise Exception('You have not suppplied a measurement function. Use .set_y_fun or set parameter meas_from_data to True for default function.')


    def set_initial_guess(self):
        """Uses the current class attributes _x0, _z0 and _u0, _p_est0 to create an initial guess for the mhe.
        The initial guess is simply the initial values for all instances of x, u and z, p_est. The method is automatically
        evoked when calling the .setup() method.
        However, if no initial values for x, u and z were supplied during setup, these default to zero.
        """
        assert self.flags['setup'] == True, 'mhe was not setup yet. Please call mhe.setup().'

        self.opt_x_num['_x'] = self._x0.cat/self._x_scaling
        self.opt_x_num['_u'] = self._u0.cat/self._u_scaling
        self.opt_x_num['_z'] = self._z0.cat/self._z_scaling
        self.opt_x_num['_p_est'] = self._p_est0.cat/self._p_est_scaling

    def setup(self):
        # Create struct for _nl_cons:
        # Use the previously defined SX.sym variables to declare shape and symbolic variable.
        self._nl_cons = struct_SX([
            entry(expr_i['expr_name'], expr=expr_i['expr']) for expr_i in self.nl_cons_list
        ])
        # Make function from these expressions:
        _x, _u, _z, _tvp, _p, _aux, _y, _ = self.model.get_variables()
        self._nl_cons_fun = Function('nl_cons_fun', [_x, _u, _z, _tvp, _p], [self._nl_cons])
        # Create bounds:
        self._nl_cons_ub = self._nl_cons(0)
        self._nl_cons_lb = self._nl_cons(-np.inf)
        # Set bounds:
        for nl_cons_i in self.nl_cons_list:
            self._nl_cons_lb[nl_cons_i['expr_name']] = nl_cons_i['lb']

        # Gather meta information:
        meta_data = {key: getattr(self, key) for key in self.data_fields}
        self.data.set_meta(**meta_data)

        self.check_validity()
        self._setup_mhe_optim_problem()
        self.flags['setup'] = True

        self.set_initial_guess()
        self.prepare_data()

    def make_step(self, y0):

        self.data.update(_y = y0)


        p_est0 = self._p_est0
        x0 = self._x0

        t0 = self._t0
        tvp0 = self.tvp_fun(t0)
        p_set0 = self.p_fun(t0)

        y_traj = self.y_fun(t0)

        self.opt_p_num['_x_prev'] = x0
        self.opt_p_num['_p_prev'] = p_est0
        self.opt_p_num['_p_set'] = p_set0
        self.opt_p_num['_tvp'] = tvp0['_tvp']
        self.opt_p_num['_y_meas'] = y_traj['y_meas']

        self.solve()

        # Extract solution:
        x_next = self.opt_x_num['_x', -1, -1]*self._x_scaling
        p_est_next = self._p_est0 = self.opt_x_num['_p_est']*self._p_est_scaling
        u0 = self.opt_x_num['_u', -1]*self._u_scaling
        z0  = self.opt_x_num['_z', -1, -1]*self._z_scaling
        aux0 = self.opt_aux_num['_aux', -1]
        p0 = self._p_cat_fun(p_est0, p_set0)

        # Update data object:
        self.data.update(_x = x0)
        self.data.update(_u = u0)
        self.data.update(_z = z0)
        self.data.update(_p = p0)
        self.data.update(_time = t0)
        self.data.update(_aux_expression = aux0)

        # Update initial
        self._t0 = self._t0 + self.t_step
        self._x0.master = x_next
        self._p_est0.master = p_est_next
        self._u0.master = u0
        self._z0.master = z0

        return x_next.full()

    def _setup_mhe_optim_problem(self):
        # Obtain an integrator (collocation, discrete-time) and the amount of intermediate (collocation) points
        ifcn, n_total_coll_points = self._setup_discretization()
        # Create struct for optimization variables:
        self.opt_x = opt_x = struct_symSX([
            entry('_x', repeat=[self.n_horizon+1, 1+n_total_coll_points], struct=self.model._x),
            entry('_z', repeat=[self.n_horizon,   1+n_total_coll_points], struct=self.model._z),
            entry('_u', repeat=[self.n_horizon], struct=self.model._u),
            entry('_p_est', struct=self._p_est),
        ])
        self.n_opt_x = self.opt_x.shape[0]
        # NOTE: The entry _x[k,:] starts with the collocation points from s to b at time k
        #       and the last point contains the child node
        # NOTE: Currently there exist dummy collocation points for the initial state (for each branch)

        # Create scaling struct as assign values for _x, _u, _z.
        self.opt_x_scaling = opt_x_scaling = opt_x(1)
        opt_x_scaling['_x'] = self._x_scaling
        opt_x_scaling['_z'] = self._z_scaling
        opt_x_scaling['_u'] = self._u_scaling
        opt_x_scaling['_p_est'] = self._p_est_scaling
        # opt_x are unphysical (scaled) variables. opt_x_unscaled are physical (unscaled) variables.
        self.opt_x_unscaled = opt_x_unscaled = opt_x(opt_x.cat * opt_x_scaling)


        # Create struct for optimization parameters:
        self.opt_p = opt_p = struct_symSX([
            entry('_x_prev', struct=self.model._x),
            entry('_p_prev', struct=self._p_prev),
            entry('_p_set', struct=self._p_set),
            entry('_tvp', repeat=self.n_horizon, struct=self.model._tvp),
            entry('_y_meas', repeat=self.n_horizon, struct=self.model._y),
        ])

        # Dummy struct with symbolic variables
        self.aux_struct = struct_symSX([
            entry('_aux', repeat=[self.n_horizon], struct=self.model._aux_expression)
        ])
        # Create mutable symbolic expression from the struct defined above.
        self.opt_aux = opt_aux = struct_SX(self.aux_struct)

        self.n_opt_aux = self.opt_aux.shape[0]

        self.lb_opt_x = opt_x(-np.inf)
        self.ub_opt_x = opt_x(np.inf)

        # Initialize objective function and constraints
        obj = 0
        cons = []
        cons_lb = []
        cons_ub = []

        # Arrival cost:
        arrival_cost = self.arrival_cost_fun(
            opt_x['_x', 0, -1],
            opt_p['_x_prev']/self._x_scaling,
            opt_x['_p_est'],
            opt_p['_p_prev']/self._p_est_scaling
            )

        obj += arrival_cost

        # Get concatenated parameters vector containing the estimated and fixed parameters.
        _p = self._p_cat_fun(self.opt_x['_p_est'], self.opt_p['_p_set'])

        # For all control intervals
        for k in range(self.n_horizon):
            # Compute constraints and predicted next state of the discretization scheme
            [g_ksb, xf_ksb] = ifcn(opt_x['_x', k, -1], vertcat(*opt_x['_x', k+1, :-1]),
                                   opt_x['_u', k], vertcat(*opt_x['_z', k, :]), opt_p['_tvp', k], _p)

            # Add the collocation equations
            cons.append(g_ksb)
            cons_lb.append(np.zeros(g_ksb.shape[0]))
            cons_ub.append(np.zeros(g_ksb.shape[0]))

            # Add continuity constraints
            cons.append(xf_ksb - opt_x['_x', k+1, -1])
            cons_lb.append(np.zeros((self.model.n_x, 1)))
            cons_ub.append(np.zeros((self.model.n_x, 1)))

            # Add nonlinear constraints only on each control step
            nl_cons_k = self._nl_cons_fun(
                opt_x_unscaled['_x', k, -1], opt_x_unscaled['_u', k], opt_x_unscaled['_z', k, -1], opt_p['_tvp', k], _p)
            cons.append(nl_cons_k)
            cons_lb.append(self._nl_cons_lb)
            cons_ub.append(self._nl_cons_ub)


            obj += self.obj_fun(
                opt_x_unscaled['_x', k+1, -1], opt_x_unscaled['_u', k], opt_x_unscaled['_z', k, -1],
                opt_p['_tvp', k], _p, opt_p['_y_meas', k]
            )


            # Calculate the auxiliary expressions for the current scenario:
            opt_aux['_aux', k] = self.model._aux_expression_fun(
                opt_x_unscaled['_x', k, -1], opt_x_unscaled['_u', k], opt_x_unscaled['_z', k, -1], opt_p['_tvp', k], _p)

            # Bounds for the states on all discretize values along the horizon
            self.lb_opt_x['_x', k] = self._x_lb.cat/self._x_scaling
            self.ub_opt_x['_x', k] = self._x_ub.cat/self._x_scaling

            # Bounds for the inputs along the horizon
            self.lb_opt_x['_u', k] = self._u_lb.cat/self._u_scaling
            self.ub_opt_x['_u', k] = self._u_ub.cat/self._u_scaling

        # Bounds for the inputs along the horizon
        self.lb_opt_x['_p_est'] = self._p_est_lb.cat/self._p_est_scaling
        self.ub_opt_x['_p_est'] = self._p_est_ub.cat/self._p_est_scaling


        cons = vertcat(*cons)
        self.cons_lb = vertcat(*cons_lb)
        self.cons_ub = vertcat(*cons_ub)

        self.n_opt_lagr = cons.shape[0]
        # Create casadi optimization object:
        nlpsol_opts = {
            'expand': False,
            'ipopt.linear_solver': 'mumps',
        }.update(self.nlpsol_opts)
        nlp = {'x': vertcat(opt_x), 'f': obj, 'g': cons, 'p': vertcat(opt_p)}
        self.S = nlpsol('S', 'ipopt', nlp, self.nlpsol_opts)

        # Create copies of these structures with numerical values (all zero):
        self.opt_x_num = self.opt_x(0)
        self.opt_x_num_unscaled = self.opt_x(0)
        self.opt_p_num = self.opt_p(0)
        self.opt_aux_num = self.opt_aux(0)

        # Create function to caculate all auxiliary expressions:
        self.opt_aux_expression_fun = Function('opt_aux_expression_fun', [opt_x, opt_p], [opt_aux])
