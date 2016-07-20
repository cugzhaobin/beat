import psgrn
import pscmp
import numpy as num

from pyrocko.guts import Object, List, String, Float, Int, Tuple, Timestamp
from pyrocko.guts_array import Array

from pyrocko import crust2x2, gf, cake, orthodrome, trace, model
from pyrocko.cake import GradientLayer
from pyrocko.fomosto import qseis
from pyrocko.fomosto import qssp
#from pyrocko.fomosto import qseis2d

import utility

import time
import logging
import shutil
import copy

c = 299792458.  # [m/s]
km = 1000.
d2r = num.pi / 180.
err_depth = 0.1
err_velocities = 0.05

lambda_sensors = {
                'Envisat': 0.056,       # needs updating- no ressource file
                'ERS1': 0.05656461471698113,
                'ERS2': 0.056,          # needs updating
                'JERS': 0.23513133960784313,
                'RadarSat2': 0.055465772433
                }


class RectangularSource(Object, gf.seismosizer.Cloneable):
    '''
    Source for rectangular fault that unifies the necessary different source
    objects for teleseismic and geodetic computations.
    '''
    lon = Float.T(help='origin longitude [deg] of central upper edge',
                    default=10.0)
    lat = Float.T(help='origin latitude [deg] of central upper edge',
                    default=13.5)
    depth = Float.T(help='depth [km] of central, upper edge',
                    default=0.5)
    strike = Float.T(help='strike angle [deg] with respect to North',
                     default=90.)
    dip = Float.T(help='dip angle [deg], 0 - horizontal, 90 - vertical',
                  default=45.)
    rake = Float.T(help='rake angle [deg] 0-left lateral movement;'
                        '-90 - normal faulting; 90 - reverse faulting;'
                        '180 - right lateral movement',
                   default=0.)
    width = Float.T(help='width of the fault [km]',
                    default=2.)
    length = Float.T(help='length of the fault [km]',
                    default=2.)
    slip = Float.T(help='slip of the fault [m]',
                    default=2.)
    opening = Float.T(help='opening of the fault [m]',
                    default=2.)
    stf = gf.STF.T(optional=True)

    time = Timestamp.T(help='source origin time', default=0., )

    @property
    def dipvec(self):
        return num.array(
                [-num.cos(self.dip * d2r) * num.cos(self.strike * d2r),
                  num.cos(self.dip * d2r) * num.sin(self.strike * d2r),
                  num.sin(self.dip * d2r)])

    @property
    def strikevec(self):
        return num.array([num.sin(self.strike * d2r),
                          num.cos(self.strike * d2r),
                          0.])

    @property
    def center(self):
        return self.depth + 0.5 * self.width * self.dipvec

    def update(self, **kwargs):
        '''Change some of the source models parameters.

        Example::

          >>> from pyrocko import gf
          >>> s = gf.DCSource()
          >>> s.update(strike=66., dip=33.)
          >>> print s
          --- !pf.DCSource
          depth: 0.0
          time: 1970-01-01 00:00:00
          magnitude: 6.0
          strike: 66.0
          dip: 33.0
          rake: 0.0

        '''
        for (k, v) in kwargs.iteritems():
            self[k] = v

    def patches(self, n, m, datatype):
        '''
        Cut source into n by m sub-faults and return n times m SourceObjects.
        Discretization starts at shallow depth going row-wise deeper.
        datatype - 'geo' or 'seis' determines the :py:class to be returned.
        '''

        length = self.length / float(n)
        width = self.width / float(m)
        patches = []

        for j in range(m):
            for i in range(n):
                sub_center = self.center + \
                            self.strikevec * ((i + 0.5 - 0.5 * n) * length) + \
                            self.dipvec * ((j + 0.5 - 0.5 * m) * width)
                effective_latlon = map(float, orthodrome.ne_to_latlon(
                    self.lat, self.lon, sub_center[1] * km, sub_center[0] * km))

                if datatype == 'seis':
                    patch = gf.RectangularSource(
                        lat=float(effective_latlon[0]),
                        lon=float(effective_latlon[1]),
                        depth=float(sub_center[2]*km),
                        strike=self.strike, dip=self.dip, rake=self.rake,
                        length=length*km, width=width*km, stf=self.stf,
                        time=self.time, slip=self.slip)
                elif datatype == 'geo':
                    patch = pscmp.PsCmpRectangularSource(
                        lat=float(effective_latlon[0]),
                        lon=float(effective_latlon[1]),
                        depth=float(sub_center[2]),
                        strike=self.strike, dip=self.dip, rake=self.rake,
                        length=length, width=width, slip=self.slip,
                        opening=self.opening)
                else:
                    raise Exception("Datatype not supported either: 'seis/geo'")
                patches.append(patch)

        return patches

    
class Covariance(Object):
    '''
    Covariance of an observation. 
    '''
    data = Array.T(shape=(None, None),
                    dtype=num.float,
                    help='Data covariance matrix',
                    optional=True)
    pred_g = Array.T(shape=(None, None),
                    dtype=num.float,
                    help='Model prediction covariance matrix, fault geometry',
                    optional=True)
    pred_v = Array.T(shape=(None, None),
                    dtype=num.float,
                    help='Model prediction covariance matrix, velocity model',
                    optional=True)
    icov = Array.T(shape=(None, None),
                    dtype=num.float,
                    help='Inverse of all covariance matrixes used as weight'
                         'in the inversion.',
                    optional=True)

    def inverse(self):
        '''
        Add and invert different covariance Matrices. 
        '''
        return num.linalg.inv(self.data + self.pred_g + self.pred_v)

    
class TeleseismicTarget(gf.Target):
    
    covariance = Covariance.T(optional=True,
                              help=':py:class:`Covariance` that holds data'
                                   'and model prediction covariance matrixes')

    
class ArrivalTaper(trace.Taper):
    ''' Cosine arrival Taper.

    :param a: start of fading in; [s] before phase arrival
    :param b: end of fading in; [s] before phase arrival
    :param c: start of fading out; [s] after phase arrival
    :param d: end of fading out; [s] after phase arrival
    '''

    a = Float.T(default=15.)
    b = Float.T(default=10.)
    c = Float.T(default=50.)
    d = Float.T(default=55.)


class Filter(Object):
    lower_corner = Float.T(default=0.001)
    upper_corner = Float.T(default=0.1)
    order = Int.T(default=4)
    

class Parameter(Object):
    name = String.T(default='lons')
    lower = Array.T(shape=(None,), dtype=num.float,)
    upper = Array.T(shape=(None,), dtype=num.float)
    testvalue = Array.T(shape=(None,), dtype=num.float)

    def __init__(self):
        for i in range(self.lower.size):
            if self.testvalue[i] > self.upper[i] or \
               self.testvalue[i] < self.lower[i]:
               raise Exception('the testvalue has to be within the upper'
                          'and lower bounds')

    @property
    def dimension(self):
        return self.lower.size
                
    def bound_to_array(self):
        return num.array([lower, testval, upper], dtype=num.float)
    
    
class IFG(Object):
    '''
    Interferogram class as a dataset in the inversion.
    '''
    amplitude = Array.T(shape=(None,), dtype=num.float, optional=True)
    wrapped_phase = Array.T(shape=(None,), dtype=num.float, optional=True)
    incidence = Array.T(shape=(None,), dtype=num.float, optional=True)
    heading = Array.T(shape=(None,), dtype=num.float, optional=True)
    los_vec = Array.T(shape=(None,3), dtype=num.float, optional=True)
    utmn = Array.T(shape=(None,), dtype=num.float, optional=True)
    utme = Array.T(shape=(None,), dtype=num.float, optional=True)
    lats = Array.T(shape=(None,), dtype=num.float, optional=True)
    lons = Array.T(shape=(None,), dtype=num.float, optional=True)
    satellite = String.T(default='Envisat')

    @property
    def wavelength(self):
        return lambda_sensors[self.satellite]

    def look_vector(self):
        '''
        Calculate LOS vector for given incidence and heading.
        '''
        if self.incidence and self.heading is None:
            Exception('Incidence and Heading need to be provided!')
             
        Su = num.cos(num.deg2rad(self.incidence));
        Sn = - num.sin(num.deg2rad(self.incidence)) * \
             num.cos(num.deg2rad(self.heading - 270))
        Se = - num.sin(num.deg2rad(self.incidence)) * \
             num.sin(num.deg2rad(self.heading - 270))
        return num.array([Se, Sn, Su], dtype=num.float).T


class Diff_IFG(IFG):
    '''
    Differential Interferogram class as geodetic target for the calculation
    of synthetics.
    '''
    unwrapped_phase = Array.T(shape=(None,), dtype=num.float, optional=True)
    coherence = Array.T(shape=(None,), dtype=num.float, optional=True)
    reference_point = Tuple.T(2, Float.T(), optional=True)
    reference_value = Float.T(optional=True, default=0.0)
    displacement = Array.T(shape=(None,), dtype=num.float, optional=True)
    covariance = Covariance.T(optional=True,
                              help=':py:class:`Covariance` that holds data'
                                   'and model prediction covariance matrixes')
    odw = Array.T(
            shape=(None,),
            dtype=num.float,
            help='Overlapping data weights, additional weight factor to the'
                 'dataset for overlaps with other datasets',
            optional=True)


class BEATconfig(Object):
    '''
    BEATconfig class is the overarching class, providing all the configurations
    for seismic data and geodetic data being used. Define directory structure
    here for Greens functions geodetic and seismic.
    '''
    store_superdirs = List.T(String.T(default='./'))

    event = model.Event.T()
    main_source = RectangularSource.T()
    sub_sources = List.T(default=RectangularSource.T(), optional=True)
    
    bounds = List.T(default=Parameter.T())

    geodetic_data_dir = String.T(default='./')
    gtargets = List.T(optional=True, default=Diff_IFG.T())

    seismic_data_dir = String.T(default='./')
    stargets = List.T(optional=True, default=TeleseismicTarget.T())
    stations = List.T(default=model.Station.T())
    channels = List.T(default=[String.T(default='Z'), String.T(default='T')])
    
    sample_rate = Float.T(default=1.0,
                          help='Sample rate of GFs to be calculated')
    arrival_taper = trace.Taper.T(default=ArrivalTaper.D())
    filterer = Filter.T()


def init_targets(stations, channels=['T', 'Z'], sample_rate=1.0, crust_inds=[0],
                 interpolation='multilinear'):
    '''
    Initiate a list of target objects given a list of indexes to the
    respective GF store velocity model variation index (crust_inds).
    '''
    targets = [TeleseismicTarget(
        quantity='displacement',
        codes=(stations[sta_num].network,
                 stations[sta_num].station,
                 '%i' % crust_ind, channel), #network, statio, location, channel
        lat=stations[sta_num].lat,
        lon=stations[sta_num].lon,
        azimuth=stations[sta_num].get_channel(channel).azimuth,
        dip=stations[sta_num].get_channel(channel).dip,
        interpolation=interpolation,
        store_id='%s_ak135_%.3fHz_%s' % (stations[sta_num].station,
                                                sample_rate, crust_ind))
        for sta_num in range(len(stations))
            for crust_ind in crust_inds
                for channel in channels]

    return targets

    
def vary_model(earthmod, err_depth=0.1, err_velocities=0.1,
               depth_limit=600 * km):
    '''
    Vary depth and velocities in the given source model by Gaussians with given
    2-sigma errors [percent]. Ensures increasing velocity with depth. Stops at
    the given depth_limit [m].
    Mantle discontinuity uncertainties are hardcoded.
    Returns: Varied Earthmodel
             Cost - Counts repetitions of cycles to ensure increasing layer
                    velocity, if high - unlikely velocities are too high
                    cost up to 10 are ok for crustal profiles.
    '''

    new_earthmod = copy.deepcopy(earthmod)
    layers = new_earthmod.layers()

    last_l = None
    cost = 0
    deltaz = 0

    # uncertainties in discontinuity depth after Shearer 1991
    discont_unc = {'410': 3 * km,
                   '520': 4 * km,
                   '660': 8 * km}

    # uncertainties in velocity for upper and lower mantle from Woodward 1991
    mantle_vel_unc = {'200': 0.02,     # above 200
                      '400': 0.01}     # above 400
    
    for layer in layers:
        # stop if depth_limit is reached
        if depth_limit:
            if layer.ztop >= depth_limit:
                layer.ztop = last_l.zbot
                # assign large cost if previous layer has higher velocity
                if layer.mtop.vp < last_l.mtop.vp or \
                   layer.mtop.vp > layer.mbot.vp:
                    cost = 1000
                # assign large cost if layer bottom depth smaller than top
                if layer.zbot < layer.ztop:
                    cost = 1000
                break
        repeat = 1
        count = 0
        while repeat:
            if count > 1000:
                break

            # vary layer velocity
            # check for layer depth and use hardcoded uncertainties
            for l_depth, vel_unc in mantle_vel_unc.items():
                if float(l_depth) * km < layer.ztop:
                    err_velocities = vel_unc
                    print err_velocities
                    
            deltavp = float(num.random.normal(
                        0, layer.mtop.vp * err_velocities / 3., 1))

            if layer.ztop == 0:
                layer.mtop.vp += deltavp
            
            # ensure increasing velocity with depth
            if last_l:
                # gradient layer without interface
                if layer.mtop.vp == last_l.mbot.vp:
                    if layer.mbot.vp + deltavp < layer.mtop.vp:
                        count += 1
                    else:
                        layer.mbot.vp += deltavp
                        layer.mbot.vs += (deltavp /
                                                layer.mbot.vp_vs_ratio())
                        repeat = 0
                        cost += count
                elif layer.mtop.vp + deltavp < last_l.mbot.vp:
                    count += 1
                else:
                    layer.mtop.vp += deltavp
                    layer.mtop.vs += (deltavp / layer.mtop.vp_vs_ratio())
                    if isinstance(layer, GradientLayer):
                        layer.mbot.vp += deltavp
                        layer.mbot.vs += (deltavp / layer.mbot.vp_vs_ratio())
                    repeat = 0
                    cost += count
            else:
                repeat = 0

        # vary layer depth
        layer.ztop += deltaz
        repeat = 1

        # use hard coded uncertainties for mantle discontinuities
        if '%i' % (layer.zbot / km) in discont_unc:
            factor_d = discont_unc['%i' % (layer.zbot / km)] / layer.zbot
        else:
            factor_d = err_depth

        while repeat:
            # ensure that bottom of layer is not shallower than the top
            deltaz = float(num.random.normal(
                       0, layer.zbot * factor_d / 3., 1))  # 3 sigma
            layer.zbot += deltaz
            if layer.zbot < layer.ztop:
                layer.zbot -= deltaz
                count += 1
            else:
                repeat = 0
                cost += count

        last_l = copy.deepcopy(layer)

    return new_earthmod, cost


def ensemble_earthmodel(ref_earthmod, num_vary=10, err_depth=0.1,
                        err_velocities=0.1, depth_limit=600 * km):
    '''
    Create ensemble of earthmodels (num_vary) that vary around a given input
    pyrocko cake earth model by a Gaussian of std_depth (in Percent 0.1 = 10%)
    for the depth layers and std_velocities (in Percent) for the p and s wave
    velocities.
    '''

    earthmods = []
    i = 0
    while i < num_vary:
        new_model, cost = vary_model(ref_earthmod,
                                     err_depth,
                                     err_velocities,
                                     depth_limit)
        if cost > 20:
            print 'Skipped unlikely model', cost
        else:
            i += 1
            earthmods.append(new_model)

    return earthmods


def seis_construct_gf(station, event, superdir, code='QSSP',
                source_depth_min=0., source_depth_max=10., source_spacing=1.,
                sample_rate=2., source_range=100., depth_limit=600 * km,
                earth_model='ak135-f-average.m', crust_ind=0,
                execute=False, rm_gfs=True, nworkers=1):
    '''Create a GF store for a station with respect to an event for a given
       Phase [P or S] and a distance range around the event.'''

    # calculate distance to station
    distance = orthodrome.distance_accurate50m(event, station)
    print 'Station', station.station
    print '---------------------'

    # load velocity profile from CRUST2x2 and check for water layer
    profile_station = crust2x2.get_profile(station.lat, station.lon)
    thickness_lwater = profile_station.get_layer(crust2x2.LWATER)[0]
    if thickness_lwater > 0.0:
        print 'Water layer', str(thickness_lwater), 'in CRUST model! \
                remove and add to lower crust'
        thickness_llowercrust = profile_station.get_layer(
                                        crust2x2.LLOWERCRUST)[0]
        thickness_lsoftsed = profile_station.get_layer(crust2x2.LSOFTSED)[0]

        profile_station.set_layer_thickness(crust2x2.LWATER, 0.0)
        profile_station.set_layer_thickness(crust2x2.LSOFTSED,
                num.ceil(thickness_lsoftsed / 3))
        profile_station.set_layer_thickness(crust2x2.LLOWERCRUST,
                thickness_llowercrust + \
                thickness_lwater + \
                (thickness_lsoftsed - num.ceil(thickness_lsoftsed / 3))
                )
        profile_station._elevation = 0.0
        print 'New Lower crust layer thickness', \
                str(profile_station.get_layer(crust2x2.LLOWERCRUST)[0])

    profile_event = crust2x2.get_profile(event.lat, event.lon)

    #extract model for source region
    source_model = cake.load_model(
        earth_model, crust2_profile=profile_event)

    # extract model for receiver stations,
    # lowest layer has to be as well in source layer structure!
    receiver_model = cake.load_model(
        earth_model, crust2_profile=profile_station)

    # randomly vary receiver site crustal model
    if crust_ind > 0:
        #moho_depth = receiver_model.discontinuity('moho').z
        receiver_model = ensemble_earthmodel(
                                        receiver_model,
                                        num_vary=1,
                                        err_depth=err_depth,
                                        err_velocities=err_velocities,
                                        depth_limit=depth_limit)[0]

    # define phases
    tabulated_phases = [gf.TPDef(
                            id='any_P',
                            definition='p,P,p\\,P\\'),
                        gf.TPDef(
                            id='any_S',
                            definition='s,S,s\\,S\\')]

    # fill config files for fomosto
    fom_conf = gf.ConfigTypeA(
        id='%s_%s_%.3fHz_%s' % (station.station,
                        earth_model.split('-')[0].split('.')[0],
                        sample_rate,
                        crust_ind),
        ncomponents=10,
        sample_rate=sample_rate,
        receiver_depth=0. * km,
        source_depth_min=source_depth_min * km,
        source_depth_max=source_depth_max * km,
        source_depth_delta=source_spacing * km,
        distance_min=distance - source_range * km,
        distance_max=distance + source_range * km,
        distance_delta=source_spacing * km,
        tabulated_phases=tabulated_phases)

   # slowness taper
    phases = [
        fom_conf.tabulated_phases[i].phases
        for i in range(len(
            fom_conf.tabulated_phases))]

    all_phases = []
    map(all_phases.extend, phases)

    mean_source_depth = num.mean((source_depth_min, source_depth_max))
    distances = num.linspace(fom_conf.distance_min,
                             fom_conf.distance_max,
                             100) * cake.m2d

    arrivals = receiver_model.arrivals(
                            phases=all_phases,
                            distances=distances,
                            zstart=mean_source_depth)

    ps = num.array(
        [arrivals[i].p for i in range(len(arrivals))])

    slownesses = ps / (cake.r2d * cake.d2m / km)

    slowness_taper = (0.0,
                      0.0,
                      1.1 * float(slownesses.max()),
                      1.3 * float(slownesses.max()))

    if code == 'QSEIS':
        from pyrocko.fomosto.qseis import build
        receiver_model = receiver_model.extract(depth_max=200 * km)
        model_code_id = 'qseis'
        version = '2006a'
        conf = qseis.QSeisConfig(
            filter_shallow_paths=0,
            slowness_window=slowness_taper,
            wavelet_duration_samples=0.001,
            sw_flat_earth_transform=1,
            sw_algorithm=1,
            qseis_version=version)

    elif code == 'QSSP':
        from pyrocko.fomosto.qssp import build
        source_model = copy.deepcopy(receiver_model)
        receiver_model = None
        model_code_id = 'qssp'
        version = '2010beta'
        conf = qssp.QSSPConfig(
            qssp_version=version,
            slowness_max=float(num.max(slowness_taper)),
            toroidal_modes=True,
            spheroidal_modes=True,
            source_patch_radius=(fom_conf.distance_delta - \
                                 fom_conf.distance_delta * 0.05) / km)

    ## elif code == 'QSEIS2d':
    ##     from pyrocko.fomosto.qseis2d import build
    ##     model_code_id = 'qseis2d'
    ##     version = '2014'
    ##     conf = qseis2d.QSeis2dConfig()
    ##     conf.qseis_s_config.slowness_window = slowness_taper
    ##     conf.qseis_s_config.calc_slowness_window = 0
    ##     conf.qseis_s_config.receiver_max_distance = 11000.
    ##     conf.qseis_s_config.receiver_basement_depth = 35.
    ##     conf.qseis_s_config.sw_flat_earth_transform = 1
    ##     # extract method still buggy!!!
    ##     receiver_model = receiver_model.extract(
    ##             depth_max=conf.qseis_s_config.receiver_basement_depth * km)

    # fill remaining fomosto params
    fom_conf.earthmodel_1d = source_model.extract(depth_max='cmb')
    fom_conf.earthmodel_receiver_1d = receiver_model
    fom_conf.modelling_code_id = model_code_id + '.' + version

    window_extension = 60.   # [s]

    fom_conf.time_region = (
        gf.Timing(tabulated_phases[0].id + '-%s' % (1.1 * window_extension)),
        gf.Timing(tabulated_phases[1].id + '+%s' % (1.6 * window_extension)))
    fom_conf.cut = (
        gf.Timing(tabulated_phases[0].id + '-%s' % window_extension),
        gf.Timing(tabulated_phases[1].id + '+%s' % (1.5 * window_extension)))
    fom_conf.relevel_with_fade_in = True
    fom_conf.fade = (
        gf.Timing(tabulated_phases[0].id + '-%s' % (1.1 * window_extension)),
        gf.Timing(tabulated_phases[0].id + '-%s' % window_extension),
        gf.Timing(tabulated_phases[1].id + '+%s' % (1.5 * window_extension)),
        gf.Timing(tabulated_phases[1].id + '+%s' % (1.6 * window_extension)))

    fom_conf.validate()
    conf.validate()

    store_dir = superdir + fom_conf.id
    print 'create Store at ', store_dir
    gf.Store.create_editables(store_dir,
                              config=fom_conf,
                              extra={model_code_id: conf})
    if execute:
        store = gf.Store(store_dir, 'r')
        store.make_ttt()
        store.close()
        build(store_dir, nworkers=nworkers)
        gf_dir = store_dir + '/qssp_green'
        if rm_gfs:
            logger.info('Removing QSSP Greens Functions!')
            shutil.rmtree(gf_dir)


def geo_construct_gf(event, superdir,
                     source_distance_min=0., source_distance_max=100.,
                     source_depth_min=0., source_depth_max=40.,
                     source_spacing=0.5, earth_model='ak135-f-average.m',
                     crust_ind=0, execute=True):
    '''
    Given a :py:class:`Event` the crustal model :py:class:`LayeredModel` from
    :py:class:`Crust2Profile` at the event location is extracted and the
    geodetic greens functions are calculated with the given grid resolution.
    '''
    conf = psgrn.PsGrnConfigFull()

    n_steps_depth = (source_depth_max - source_depth_min) / source_spacing
    n_steps_distance = (source_distance_max - source_distance_min) \
                            / source_spacing

    conf.distance_grid = psgrn.PsGrnSpatialSampling(
                                n_steps=n_steps_distance,
                                start_distance=source_distance_min,
                                end_distance=source_distance_max)
    conf.depth_grid = psgrn.PsGrnSpatialSampling(
                                n_steps=n_steps_depth,
                                start_distance=source_depth_min,
                                end_distance=source_depth_max)

    # extract source crustal profile and check for water layer
    source_profile = crust2x2.get_profile(event.lat, event.lon)
    thickness_lwater = source_profile.get_layer(crust2x2.LWATER)[0]

    if thickness_lwater > 0.0:
        print 'Water layer', str(thickness_lwater), 'in CRUST model! \
                remove and add to lower crust'
        thickness_llowercrust = source_profile.get_layer(
                                        crust2x2.LLOWERCRUST)[0]

        source_profile.set_layer_thickness(crust2x2.LWATER, 0.0)
        source_profile.set_layer_thickness(crust2x2.LLOWERCRUST,
                thickness_llowercrust + \
                thickness_lwater)
        source_profile._elevation = 0.0
        print 'New Lower crust layer thickness', \
                str(source_profile.get_layer(crust2x2.LLOWERCRUST)[0])

    source_model = cake.load_model(earth_model,
                                   crust2_profile=source_profile).extract(
                                   depth_max=source_depth_max * km)

    # potentially vary source model
    if crust_ind > 0:
        source_model = ensemble_earthmodel(source_model,
                                           num_vary=1,
                                           err_depth=err_depth,
                                           err_velocities=err_velocities,
                                           depth_limit=None)

    conf.earthmodel_1d = source_model
    conf.psgrn_outdir = superdir + 'psgrn_green_%i/' % (crust_ind)
    conf.validate()

    print 'Creating Geo GFs in directory:', conf.psgrn_outdir

    runner = psgrn.PsGrnRunner(outdir=conf.psgrn_outdir)
    if execute:
        runner.run(conf)


def geo_layer_synthetics(store_superdir, crust_ind, lons, lats, sources):
    '''
    Input: Greensfunction store path, index of potentialy varied model store
           List of observation points Latitude and Longitude,
           List of rectangular fault sources.
    Output: NumpyArray(nobservations; ux, uy, uz)
    '''
    conf = pscmp.PsCmpConfigFull()
    conf.observation = pscmp.PsCmpScatter(lats=lats, lons=lons)
    conf.psgrn_outdir = store_superdir + 'psgrn_green_%i/' % (crust_ind)
    # only coseismic displacement
    conf.times_snapshots = [0]
    conf.rectangular_source_patches = sources

    runner = pscmp.PsCmpRunner(keep_tmp=True)
    runner.run(conf)
    # returns list of displacements for each snapshot
    return runner.get_results(component='displ', flip_z=True)


def get_phase_taperer(engine, source, target, arrival_taper):
    '''
    Get arrival time from store for respective :py:class:`target`
    and :py:class:`source` pair and taper return :py:class:`CosTaper`
    according to defined arrival_taper times.
    '''
    store = engine.get_store(target.store_id)
    dist = target.distance_to(source)
    depth = source.depth
    if target.codes[3] == 'T':
        wave = 'any_S'
    else:
        wave = 'any_P'

    arrival_time = store.t(wave, (depth, dist)) + source.time
    taperer = trace.CosTaper(arrival_time - arrival_taper.a,
                             arrival_time - arrival_taper.b,
                             arrival_time + arrival_taper.c,
                             arrival_time + arrival_taper.d)
    return taperer


def seis_synthetics(engine, sources, targets, arrival_taper, filterer,
                    reference_taperer=None, plot=False):
    '''
    Calculate synthetic seismograms of combination of targets and sources,
    filtering and tapering afterwards (filterer)
    tapering according to arrival_taper around P -or S wave.
    If reference_taper the given taper is always used.
    Returns: Array with data each row-one target
    plot - flag for looking at traces
    '''
    
    response = engine.process(sources = sources,
                              targets = targets)

    synt_trcs = []
    for source, target, tr in response.iter_results():
        # extract interpolated travel times of phases which have been defined
        # in the store's config file and cut data/synth traces
        
        if reference_taperer is None:
            taperer = get_phase_taperer(engine, sources[0], target, arrival_taper)
        else:
            taperer = reference_taperer

        # cut traces
        tr.taper(taperer, inplace=True, chop=True)

        # filter traces
        tr.bandpass(corner_hp = filterer.lower_corner,
                                  corner_lp = filterer.upper_corner,
                                  order=filterer.order)
        tr.location = str(source.magnitude)

        synt_trcs.append(tr)

    if plot:
        trace.snuffle(synt_trcs)

    nt = len(targets)
    ns = len(sources)
    synths = num.vstack([synt_trcs[i].ydata for i in range(len(synt_trcs))])
    tmins = num.vstack([synt_trcs[i].tmin for i in range(nt)]).flatten()

    # stack traces for all sources
    if ns > 1 :
        for k in range(ns):
            outstack = num.zeros([nt, synths.shape[1]])
            outstack += synths[(k*nt):(k+1)*nt, :]

        synths = outstack

    print tmins.shape
    return synths, tmins

    
def taper_filter_traces(traces, arrival_taper, filterer, tmins, plot=False):
    cut_traces = []
    nt = len(traces)
    print tmins, tmins.shape
    for i in range(nt):
        print tmins[i], tmins[i].shape
        taperer = trace.CosTaper(
            float(tmins[i]),
            float(tmins[i] + arrival_taper.b),
            float(tmins[i] + arrival_taper.a + arrival_taper.c),
            float(tmins[i] + arrival_taper.a + arrival_taper.d))
        cut_trace = traces[i].copy()
        # cut traces
        cut_trace.taper(taperer, inplace=True, chop=True)

        # filter traces
        cut_trace.bandpass(corner_hp = filterer.lower_corner,
                           corner_lp = filterer.upper_corner,
                           order=filterer.order)
        cut_traces.append(cut_trace)

        if plot:
            trace.snuffle(cut_traces)
        
    return num.vstack([cut_traces[i].ydata for i in range(nt)])