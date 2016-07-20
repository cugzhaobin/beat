from pyrocko import gf, trace
import numpy as num
import heart


def calculate_model_prediction_sensitivity(engine, *args, **kwargs):
    '''
    Calculate the model prediction Covariance Sensitivity Kernel.
    (numerical derivation with respect to the input source parameter(s))
    Following Duputel et al. 2014

    :Input:
    :py:class:'engine'
    source_parms = list of parameters with respect to which the kernel
                   is being calculated e.g. ['strike', 'dip', 'depth']
    !!!
    NEEDS to have seismosizer source object parameter variable name convention
    !!!
    (see seismosizer.source.keys())

    calculate_model_prediction_sensitivity(request, source_params, **kwargs)
    calculate_model_prediction_sensitivity(sources,
                                             targets, source_params, **kwargs)

    Returns traces in a list[parameter][targets] for each station and channel
    as specified in the targets. The location code of each trace is placed to
    show the respective source parameter.
    '''

    if len(args) not in (0, 1, 2, 3):
        raise gf.BadRequest('invalid arguments')

    if len(args) == 2:
        kwargs['request'] = args[0]
        kwargs['source_params'] = args[1]

    elif len(args) == 3:
        kwargs.update(gf.Request.args2kwargs(args[0:1]))
        kwargs['source_params'] = args[2]

    request = kwargs.pop('request', None)
    status_callback = kwargs.pop('status_callback', None)
    nprocs = kwargs.pop('nprocs', 1)
    source_params = kwargs.pop('source_params', None)
    h = kwargs.pop('h', None)

    if request is None:
        request = gf.Request(**kwargs)

    if h is None:
        h=num.ones(len(source_params)) * 1e-1

    # create results list
    sensitivity_param_list = []
    sensitivity_param_trcs = []

    for i in xrange(len(source_params)):
        sensitivity_param_list.append([0] * len(request.targets))
        sensitivity_param_trcs.append([0] * len(request.targets))

    for ref_source in request.sources:
        par_count = 0
        for param in source_params:
            print param, 'with h = ', h[par_count]
            calc_source_p2h = ref_source.clone()
            calc_source_ph = ref_source.clone()
            calc_source_mh = ref_source.clone()
            calc_source_m2h = ref_source.clone()

            setattr(calc_source_p2h, param,
                    ref_source[param] + (2 * h[par_count]))
            setattr(calc_source_ph, param,
                    ref_source[param] + (h[par_count]))
            setattr(calc_source_mh, param,
                    ref_source[param] - (h[par_count]))
            setattr(calc_source_m2h, param,
                    ref_source[param] - (2 * h[par_count]))

            calc_sources = [calc_source_p2h, calc_source_ph,
                            calc_source_mh, calc_source_m2h]

            response = engine.process(sources=calc_sources,
                                      targets=request.targets,
                                      nprocs=nprocs)

            for k in xrange(len(request.targets)):
                # zero padding if necessary
                trc_lengths = num.array(
                    [len(response.results_list[i][k].trace.data) for i in \
                                        range(len(response.results_list))])
                Id = num.where(trc_lengths != trc_lengths.max())

                for l in Id[0]:
                    response.results_list[l][k].trace.data = num.concatenate(
                            (response.results_list[l][k].trace.data,
                             num.zeros(trc_lengths.max() - trc_lengths[l])))

                # calculate numerical partial derivative for
                # each source and target
                sensitivity_param_list[par_count][k] = (
                        sensitivity_param_list[par_count][k] + (\
                            - response.results_list[0][k].trace.data + \
                            8 * response.results_list[1][k].trace.data - \
                            8 * response.results_list[2][k].trace.data + \
                                response.results_list[3][k].trace.data) / \
                            (12 * h[par_count])
                                                       )

            par_count = par_count + 1

    # form traces from sensitivities
    par_count = 0
    for param in source_params:
        for k in xrange(len(request.targets)):
            sensitivity_param_trcs[par_count][k] = trace.Trace(
                        network=request.targets[k].codes[0],
                        station=request.targets[k].codes[1],
                        ydata=sensitivity_param_list[par_count][k],
                        deltat=response.results_list[0][k].trace.deltat,
                        tmin=response.results_list[0][k].trace.tmin,
                        channel=request.targets[k].codes[3],
                        location=param)

        par_count = par_count + 1

    return sensitivity_param_trcs


def calc_seis_cov_velocity_models(engine, sources, crust_inds, targets,
                                  arrival_taper, corner_fs):
    '''
    Calculate model prediction uncertainty matrix with respect to uncertainties
    in the velocity model for station and channel.
    Input:
    :py:class:`BEATconfig` - wrapps and contains necessary input
    :py:class:`station` - seismic station to be processed
    channel - of the station to be processed ('T' or 'Z')
    '''

    ref_target = copy.deepcopy(targets[0])
    
    reference_taperer = get_phase_taperer(engine,
                                          sources[0],
                                          ref_target,
                                          arrival_taper)
    
    synths = seis_synthetics(engine, sources, targets,
                             arrival_taper,
                             corner_fs,
                             reference_taperer=reference_taperer)
    return num.cov(synths, rowvar=0)


def calc_geo_cov_velocity_models(store_superdir, crust_inds, dataset, sources):
    '''
    Calculate model prediction uncertainty matrix with respect to uncertainties
    in the velocity model for geodetic dateset.
    Input:
    store_superdir - geodetic GF directory
    crust_inds - List of indices for respective GF stores
    dataset - :py:class:`IFG`/`DiffIFG`
    sources - List of :py:class:`PsCmpRectangularSource`
    '''
                       
    synths = num.zeros(len(crust_inds), dataset.lons.size)
    for ind in crust_inds:
        synths[:, ind] = geo_layer_synthetics(store_superdirs, ind,
                                        lons=dataset.lons,
                                        lats=dataset.lats,
                                        sources=sources)
    
    return num.cov(synths, rowvar=0)