'''
Created on March, 2016

@author: Hannes Vasyura-Bathke
'''

import numpy as np
import pymc3 as pm

import logging
import os
import shutil
import theano
import copy

from pyrocko import util, parimap
from pymc3.model import modelcontext
from pymc3.blocking import DictToArrayBijection
from pymc3.vartypes import discrete_types
from pymc3.theanof import inputvars
from pymc3.theanof import make_shared_replacements, join_nonshared_inputs
from pymc3.step_methods.metropolis import MultivariateNormalProposal as MvNPd
from numpy.random import seed

import pickle
from beat import backend, utility

__all__ = ['ATMCMC', 'ATMIP_sample']

logger = logging.getLogger('beat')
logger.setLevel(logging.INFO)


class ATMCMC(backend.ArrayStepSharedLLK):
    """
    Adaptive Transitional Markov-Chain Monte-Carlo
    following: Ching & Chen 2007: Transitional Markov chain Monte Carlo method
                for Bayesian model updating, model class selection and model
                averaging
                Journal of Engineering Mechanics 2007
                DOI:10.1016/(ASCE)0733-9399(2007)133:7(816)
    http://ascelibrary.org/doi/abs/10.1061/%28ASCE%290733-9399%282007%29133:7%28816%29

    Creates initial samples and framework around the (C)ATMIP parameters

    Parameters
    ----------
    vars : list
        List of variables for sampler
    out_vars : list
        List of output variables for trace recording. If empty unobserved_RVs
        are taken.
    n_chains : (integer) Number of chains per stage has to be a large number
               of number of n_jobs (processors to be used) on the machine.
    covariance : (n_chains x n_chains) Numpy array
                 Initial Covariance matrix for proposal distribution,
                 if None - identity matrix taken
    likelihood_name : string
                      name of the determinsitic variable that contains the
                      model likelihood - defaults to 'like'
    proposal_dist : pymc3 object
                    Type of proposal distribution, see metropolis.py for
                    options
    coef_variation : scalar, float
                     Coefficient of variation, determines the change of beta
                     from stage to stage, i.e.indirectly the number of stages,
                     low coef_variation --> slow beta change,
                                         results in many stages and vice verca
                     (default: 1.)
    check_bound : boolean
                  check if current sample lies outside of variable definition
                  speeds up computation as the forward model wont be executed
                  default: True
    model : PyMC Model
        Optional model for sampling step.
        Defaults to None (taken from context).
    """
    default_blocked = True

    def __init__(self, vars=None, out_vars=None, covariance=None, scaling=1.,
                 n_chains=100, tune=True, tune_interval=100, model=None,
                 check_bound=True, likelihood_name='like', proposal_dist=MvNPd,
                 coef_variation=1., **kwargs):

        model = modelcontext(model)

        if vars is None:
            vars = model.vars

        vars = inputvars(vars)

        if out_vars is None:
            out_vars = model.unobserved_RVs
            self.lordering = utility.TVListArrayOrdering(out_vars)
            lpoint = [var.tag.test_value for var in out_vars]
            self.lij = utility.ListToArrayBijection(
                self.lordering, lpoint)

        out_varnames = [out_var.name for out_var in out_vars]

        if covariance is None:
            self.covariance = np.eye(sum(v.dsize for v in vars))
        self.scaling = np.atleast_1d(scaling)
        self.tune = tune
        self.check_bnd = check_bound
        self.tune_interval = tune_interval
        self.steps_until_tune = tune_interval

        self.proposal_dist = proposal_dist(self.covariance)
        self.proposal_samples_array = self.proposal_dist(n_chains)

        self.stage_sample = 0
        self.accepted = 0

        self.beta = 0
        self.stage = 0
        self.chain_index = 0
        self.resampling_indexes = np.arange(n_chains)

        self.coef_variation = coef_variation
        self.n_chains = n_chains
        self.likelihoods = np.zeros(n_chains)

        self.likelihood_name = likelihood_name
        self._llk_index = out_varnames.index(likelihood_name)
        self.discrete = np.concatenate(
            [[v.dtype in discrete_types] * (v.dsize or 1) for v in vars])
        self.any_discrete = self.discrete.any()
        self.all_discrete = self.discrete.all()

        # create initial population
        self.population = []
        self.array_population = np.zeros(n_chains)
        for i in range(self.n_chains):
            dummy = pm.Point({v.name: v.random() for v in vars},
                                                            model=model)
            self.population.append(dummy)

        self.chain_previous_point = copy.deepcopy(self.population)

        shared = make_shared_replacements(vars, model)
        self.logp_forw = logp_forw(out_vars, vars, shared)
        self.check_bnd = logp_forw([model.varlogpt], vars, shared)

        super(ATMCMC, self).__init__(vars, out_vars, shared)

    def astep(self, q0):
        if self.stage == 0:
            l_new = self.logp_forw(q0)
            q_new = q0

        else:
            if not self.stage_sample:
                self.proposal_samples_array = self.proposal_dist(self.n_steps)

            if not self.steps_until_tune and self.tune:
                # Tune scaling parameter
                self.scaling = tune(self.accepted /
                                    float(self.tune_interval))
                # Reset counter
                self.steps_until_tune = self.tune_interval
                self.accepted = 0

            delta = self.proposal_samples_array[self.stage_sample, :] * \
                                                                self.scaling

            if self.any_discrete:
                if self.all_discrete:
                    delta = np.round(delta, 0)
                    q0 = q0.astype(int)
                    q = (q0 + delta).astype(int)
                else:
                    delta[self.discrete] = np.round(
                                        delta[self.discrete], 0).astype(int)
                    q = q0 + delta
                    q = q[self.discrete].astype(int)
            else:
                q = q0 + delta

            l0 = self.chain_previous_point[
                            self.resampling_indexes[self.chain_index]]

            if self.check_bnd:
                varlogp = self.check_bnd(q)

                if np.isfinite(varlogp):
                    l = self.logp_forw(q)

                    q_new = pm.metropolis.metrop_select(
                        self.beta * (l[self._llk_index] - l0[self._llk_index]),
                        q, q0)

                    if q_new is q:
                        self.accepted += 1
                        l_new = l
                        self.chain_previous_point[
                            self.resampling_indexes[self.chain_index]] = l_new
                    else:
                        l_new = l0
                else:
                    q_new = q0
                    l_new = l0
            else:
                l = self.logp_forw(q)
                q_new = pm.metropolis.metrop_select(
                    self.beta * (l[self._llk_index] - l0[self._llk_index]),
                    q, q0)

                self.chain_previous_point[
                    self.resampling_indexes[self.chain_index]] = l_new

                if q_new is q:
                    self.accepted += 1
                    l_new = l
                    self.chain_previous_point[
                        self.resampling_indexes[self.chain_index]] = l_new
                else:
                    l_new = l0

            self.steps_until_tune -= 1
            self.stage_sample += 1

            # reset sample counter
            if self.stage_sample == self.n_steps:
                self.stage_sample = 0

        return q_new, l_new

    def calc_beta(self):
        """
        Calculate next tempering beta and importance weights based on
        current beta and sample likelihoods.

        Returns
        -------
        beta(m+1) : scalar float tempering parameter of the next stage
        beta(m) : scalar float tempering parameter of the current stage
        weights : NdArray of importance weights (floats)
        """

        low_beta = self.beta
        up_beta = 2.
        old_beta = self.beta

        while up_beta - low_beta > 1e-6:
            current_beta = (low_beta + up_beta) / 2.
            temp = np.exp((current_beta - self.beta) * \
                           (self.likelihoods - self.likelihoods.max()))
            cov_temp = np.std(temp) / np.mean(temp)
            if cov_temp > self.coef_variation:
                up_beta = current_beta
            else:
                low_beta = current_beta

        beta = current_beta
        weights = temp / np.sum(temp)
        return beta, old_beta, weights

    def calc_covariance(self):
        """
        Calculate trace covariance matrix based on importance weights.

        Returns
        -------
        Ndarray of weighted covariances (NumPy > 1.10. required)
        """
        return np.cov(self.array_population,
                      aweights=self.weights,
                      bias=False,
                      rowvar=0)

    def select_end_points(self, mtrace):
        """
        Read trace results (variables and model likelihood) and take end points
        for each chain and set as start population for the next stage.
        Parameters
        -------

        mtrace : Multitrace pymc3 object

        Returns
        -------
        population : List of pymc3.Point - objects,
        array_population : Ndarray of trace end-points
        likelihoods : Ndarray of likelihoods of the trace end-points
        """

        array_population = np.zeros((self.n_chains,
                                      self.ordering.dimensions))

        n_steps = len(mtrace)

        # collect end points of each chain and put into array
        for var, slc, shp, _ in self.ordering.vmap:
            if len(shp) == 0:
                array_population[:, slc] = np.atleast_2d(
                                    mtrace.get_values(varname=var,
                                                burn=n_steps - 1,
                                                combine=True)).T
            else:
                array_population[:, slc] = mtrace.get_values(
                                                    varname=var,
                                                    burn=n_steps - 1,
                                                    combine=True)
        # get likelihoods
        likelihoods = mtrace.get_values(varname=self.likelihood_name,
                                        burn=n_steps - 1,
                                        combine=True)
        population = []

        # map end array_endpoints to dict points
        for i in range(self.n_chains):
            population.append(self.bij.rmap(array_population[i, :]))

        return population, array_population, likelihoods

    def get_chain_previous_points(self, mtrace):
        """
        Read trace results and take end points for each chain and set as
        previous chain result for comparison of metropolis select.

        Parameters
        -------

        mtrace : Multitrace pymc3 object

        Returns
        -------
        chain_previous_result - list of all unobservedRV values
        """

        array_population = np.zeros((self.n_chains,
                                      self.lordering.dimensions))

        n_steps = len(mtrace)

        for var, list_ind, slc, shp, _ in zip(mtrace.varnames,
                                                self.lordering.vmap):
            if len(shp) == 0:
                array_population[:, slc] = np.atleast_2d(
                                    mtrace.get_values(varname=var,
                                                burn=n_steps,
                                                combine=True)).T
            else:
                array_population[:, slc] = mtrace.get_values(
                                                    varname=var,
                                                    burn=n_steps - 1,
                                                    combine=True)

        chain_previous_points = []

        # map end array_endpoints to list lpoints
        for i in range(self.n_chains):
            chain_previous_points.append(
                self.lij.rmap(array_population[i, :]))

        return chain_previous_points

    def mean_end_points(self):
        '''
        Calculate mean of the end-points and return point.
        '''
        bij = DictToArrayBijection(self.ordering, self.population[0])
        return bij.rmap(self.array_population.mean(axis=0))

    def resample(self):
        """
        Resample pdf based on importance weights.
        based on Kitagawas deterministic resampling algorithm.

        Returns
        -------
        outindex : Ndarray of resampled trace indexes
        """
        parents = np.array(range(self.n_chains))
        N_childs = np.zeros(self.n_chains, dtype=int)

        cum_dist = np.cumsum(self.weights)
        aux = np.random.rand(1)
        u = parents + aux
        u /= self.n_chains
        j = 0
        for i in parents:
            while u[i] > cum_dist[j]:
                j += 1

            N_childs[j] += 1

        indx = 0
        outindx = np.zeros(self.n_chains, dtype=int)
        for i in parents:
            if N_childs[i] > 0:
                for j in range(indx, (indx + N_childs[i])):
                    outindx[j] = parents[i]

            indx += N_childs[i]

        return outindx


def ATMIP_sample(n_steps, step=None, start=None, trace=None, chain=0,
                  stage=None, n_jobs=1, tune=None, progressbar=False,
                  model=None, update=None, random_seed=None):
    """
    (C)ATMIP sampling algorithm from Minson et al. 2013:
    Bayesian inversion for finite fault earthquake source models I-
        Theory and algorithm
    (without cascading- C)
    https://gji.oxfordjournals.org/content/194/3/1701.full
    Samples the solution space with n_chains of Metropolis chains, where each
    chain has n_steps iterations. Once finished, the sampled traces are
    evaluated:
    (1) Based on the likelihoods of the final samples, chains are weighted
    (2) the weighted covariance of the ensemble is calculated and set as new
        proposal distribution
    (3) the variation in the ensemble is calculated and the next tempering
        parameter (beta) calculated
    (4) New n_chains Metropolis chains are seeded on the traces with high
        weight for n_steps iterations
    (5) Repeat until beta > 1.

    Parameters
    ----------

    n_steps : int
        The number of samples to draw for each Markov-chain per stage
    step : function from TMCMC initialisation
    start : List of dicts with length(n_chains)
        Starting points in parameter space (or partial point)
        Defaults to random draws from variables (defaults to empty dict)
    trace : backend
        This should be a backend instance.
        Passing either "text" or "sqlite" is taken as a shortcut to set
        up the corresponding backend (with "mcmc" used as the base
        name).
    chain : int
        Chain number used to store sample in backend. If `n_jobs` is
        greater than one, chain numbers will start here.
    stage : int
        Stage where to start or continue the calculation. It is possible to
        continue after completed stages (stage should be the number of the
        completed stage + 1). If None the start will be at stage = 0.
    n_jobs : int
        The number of cores to be used in parallel. Be aware that theano has
        internal parallelisation. Sometimes this is more efficient especially
        for simple models.
        step.n_chains / n_jobs has to be an integer number!
    tune : int
        Number of iterations to tune, if applicable (defaults to None)
    trace : result_folder for storing stages, will be created if not existing
    progressbar : bool
        Flag for progress bar
    model : Model (optional if in `with` context) has to contain deterministic
            variable 'name defined under step.likelihood_name' that contains
            model likelihood
    update : :py:class:`Project` contains all data and
             covariances to be updated each transition step
    random_seed : int or list of ints
        A list is accepted if more if `n_jobs` is greater than one.

    Returns
    -------
    MultiTrace object with access to sampling values
    """

    model = pm.modelcontext(model)
    step.n_steps = int(n_steps)
    seed(random_seed)

    if n_steps < 1:
        logger.error('Argument `n_steps` should be above 0.', exc_info=1)

    if step is None:
        logger.error('Argument `step` has to be a TMCMC step object.',
            exc_info=1)

    if trace is None:
        logger.error('Argument `trace` should be path to result_directory.',
            exc_info=1)

    if n_jobs > 1:
        if not (step.n_chains / float(n_jobs)).is_integer():
            logger.error('n_chains / n_jobs has to be a whole number!',
                exc_info=1)

    if start is not None:
        if len(start) != step.n_chains:
            logger.error('Argument `start` should have dicts equal the '
                            'number of chains (step.N-chains)', exc_info=1)
        else:
            step.population = start

    if not any(
            step.likelihood_name in var.name for var in model.deterministics):
            logger.error('Model (deterministic) variables need to contain '
                            'a variable `' + step.likelihood_name + '` as '
                            'defined in `step`.', exc_info=1)

    homepath = trace

    util.ensuredir(homepath)

    if stage is not None:
        if stage > 0:
            logger.info('Loading completed stage_%i' % (stage - 1))
            project_dir = os.path.dirname(homepath)
            mode = os.path.basename(homepath)
            step, update = utility.load_atmip_params(
                project_dir, stage - 1, mode)
            step.stage += 1
            # remove following (inclomplete) stage results
            stage_path = os.path.join(homepath, 'stage_%i' % step.stage)
            util.ensuredir(stage_path)
            shutil.rmtree(stage_path)
        else:
            step.stage = stage

    with model:
        while step.beta < 1.:
            logger.info('Beta: %f Stage: %i' % (step.beta, step.stage))
            if step.stage == 0:
                # Initial stage
                logger.info('Sample initial stage: ...')
                draws = 1

            else:
                draws = n_steps

            if progressbar and n_jobs > 1:
                progressbar = False

            # Metropolis sampling intermediate stages
            stage_path = os.path.join(homepath, 'stage_%i' % step.stage)

            sample_args = {
                    'draws': draws,
                    'step': step,
                    'stage_path': stage_path,
                    'progressbar': progressbar,
                    'model': model,
                    'n_jobs': n_jobs}

            mtrace = _iter_parallel_chains(**sample_args)

            step.population, step.array_population, step.likelihoods = \
                                    step.select_end_points(mtrace)
            step.beta, step.old_beta, step.weights = step.calc_beta()

            if step.stage == 0:
                step.chain_previous_point = step.get_chain_previous_points(
                                                              mtrace)

            if update is not None:
                logger.info('Updating Covariances ...')
                mean_pt = step.mean_end_points()
                update.update_weights(mean_pt)

                if step.stage == 0:
                    update.update_target_weights(mtrace, mode='adaptive')

            if step.beta > 1.:
                logger.info('Beta > 1.: %f' % step.beta)
                step.beta = 1.
                break

            step.covariance = step.calc_covariance()
            step.proposal_dist = MvNPd(step.covariance)
            step.resampling_indexes = step.resample()

            outpath = os.path.join(stage_path, 'atmip.params')
            outparam_list = [step, update]
            dump_params(outpath, outparam_list)

            step.stage += 1

            del(mtrace)

        # Metropolis sampling final stage
        logger.info('Sample final stage')
        stage_path = os.path.join(homepath, 'stage_final')
        temp = np.exp((1 - step.old_beta) * \
                           (step.likelihoods - step.likelihoods.max()))
        step.weights = temp / np.sum(temp)
        step.covariance = step.calc_covariance()
        step.proposal_dist = MvNPd(step.covariance)
        step.resampling_indexes = step.resample()

        sample_args['step'] = step
        sample_args['stage_path'] = stage_path
        mtrace = _iter_parallel_chains(**sample_args)

        outpath = os.path.join(stage_path, 'atmip.params')
        outparam_list = [step, update]
        dump_params(outpath, outparam_list)

        return mtrace


def dump_params(outpath, outparam_list):
    '''
    Dump parameters in outparam_list into pickle file.
    '''
    with open(outpath, 'w') as f:
        pickle.dump(outparam_list, f)


def _iter_initial(step, chain=0, strace=None, model=None):
    """
    Modified from pymc3 to work with ATMCMC.
    Yields generator for Iteration over initial stage similar to
    _iter_sample, just different input to loop over.
    """

    # check if trace file already exists before setup
    filename = os.path.join(strace.name, 'chain-{}.csv'.format(chain))
    if os.path.exists(filename):
        strace.setup(step.n_chains, chain=0)
        l_tr = len(strace)
    else:
        strace.setup(step.n_chains, chain=0)
        l_tr = 0

    if l_tr == step.n_chains:
        # only return strace
        for i in range(1):
            print('Using results of previous run!')
            yield strace
    else:
        for i in range(l_tr, step.n_chains):
            _, out_list = step.step(step.population[i])
            strace.record(out_list)
            yield strace
        else:
            strace.close()


def _sample(draws, step=None, start=None, trace=None, chain=0, tune=None,
            progressbar=True, model=None, random_seed=None):

    sampling = _iter_sample(draws, step, start, trace, chain,
                            tune, model, random_seed)
    progress = pm.progressbar.progress_bar(draws)
    for i, strace in enumerate(sampling):
        step.chain_index = chain
        try:
            if progressbar:
                progress.update(i)
        except KeyboardInterrupt:
            strace.close()
    return pm.backends.base.MultiTrace([strace]), chain


def _iter_sample(draws, step, start=None, trace=None, chain=0, tune=None,
                 model=None, random_seed=None):
    '''
    Modified from pymc3.sampling._iter_sample until they make
    _choose_backends more flexible.
    '''

    model = modelcontext(model)

    draws = int(draws)
    seed(random_seed)
    if draws < 1:
        raise ValueError('Argument `draws` should be above 0.')

    if start is None:
        start = {}

#    if len(strace) > 0:
#        pm.sampling._soft_update(start, strace.point(-1))
#    else:
#        pm.sampling._soft_update(start, model.test_point)

    try:
        step = pm.step_methods.CompoundStep(step)
    except TypeError:
        pass

    point = pm.Point(start, model=model)

    trace.setup(draws, chain)
    for i in range(draws):
        if i == tune:
            step = pm.sampling.stop_tuning(step)

        point, out_list = step.step(point)
        trace.record(out_list)

        yield trace
    else:
        trace.close()


def work_chain(work, pshared=None):

    if pshared is not None:
        draws = pshared['draws']
        progressbars = pshared['progressbars']
        tune = pshared['tune']
        trace_list = pshared['trace_list']
        model = pshared['model']

    step, chain, start = work

    progressbar = progressbars[chain]
    trace = trace_list[chain]

    return _sample(draws, step, start, trace, chain, tune, progressbar, model)


def _iter_serial_chains(draws, step=None, stage_path=None,
                        progressbar=True, model=None):
    """
    Do Metropolis sampling over all the chains with each chain being
    sampled 'draws' times. Serial execution one after another.
    """
    mtraces = []
    progress = pm.progressbar.progress_bar(step.n_chains)
    for chain in range(step.n_chains):
        if progressbar:
            progress.update(chain)
        trace = backend.Text(stage_path, model=model)
        strace, chain = _sample(
                draws=draws,
                step=step,
                chain=chain,
                trace=trace,
                model=model,
                progressbar=False,
                start=step.population[step.resampling_indexes[chain]])
        mtraces.append(strace)

    return pm.sampling.merge_traces(mtraces)


def _iter_parallel_chains(draws, step, stage_path, progressbar, model, n_jobs):
    """
    Do Metropolis sampling over all the chains with each chain being
    sampled 'draws' times. Parallel execution according to n_jobs.
    """
    chains = list(range(step.n_chains))
    trace_list = []
    result_traces = []

    if step.stage == 0:
        display = False
    else:
        display = True

    pack_pb = [progressbar for i in range(n_jobs - 1)] + [display]
    block_pb = []
    list_pb = []

    for i in range(int(step.n_chains / n_jobs)):
        block_pb.append(pack_pb)

    map(list_pb.extend, block_pb)

    logger.info('Initialising chain traces ...')
    for chain in chains:
        trace_list.append(backend.Text(stage_path, model=model))
        result_traces.append(None)

    logger.info('Sampling ...')

    pshared = dict(
        draws=draws,
        trace_list=trace_list,
        progressbars=list_pb,
        tune=None,
        model=model)

    work = [(step, chain, step.population[step.resampling_indexes[chain]])
             for chain in chains]

    progress = pm.progressbar.progress_bar(step.n_chains)
    chain_done = 0

    for strace, chain in parimap.parimap(
                    work_chain, work, pshared=pshared, nprocs=n_jobs):
        result_traces[chain] = strace
        chain_done += 1
        progress.update(chain_done)

    return pm.sampling.merge_traces(result_traces)


def tune(acc_rate):
    """
    Tune adaptively based on the acceptance rate.
    Parameters
    ----------
    acc_rate: scalar float
              Acceptance rate of the Metropolis sampling

    Returns
    -------
    scaling: scalar float
    """

    # a and b after Muto & Beck 2008 .
    a = 1. / 9
    b = 8. / 9
    return np.power((a + (b * acc_rate)), 2)


def logp_forw(out_vars, vars, shared):
    out_list, inarray0 = join_nonshared_inputs(out_vars, vars, shared)
    f = theano.function([inarray0], out_list)
    f.trust_input = True
    return f
