#!/usr/bin/env python
"""
Orbit fitting code. The run function is the console entry point,
accessed by calling fit_orbit from the command line.
"""

from __future__ import print_function
import numpy as np
import os
import time
import emcee
from configparser import ConfigParser
from astropy.io import fits
from htof.main import Astrometry
from astropy.time import Time

from orbit3d import orbit
from orbit3d.config import parse_args

_loglkwargs = {}


def set_initial_parameters(start_file, ntemps, nplanets, nwalkers):
    if start_file.lower() == 'none':
        mpri = 1.85
        jit = 0.5
        sau = 10
        esino = 0.1
        ecoso = 0.30
        inc = 88.8*np.pi/180
        asc = 212*np.pi/180
        lam = -35*np.pi/180
        msec = 0.00764

        par0 = np.ones((ntemps, nwalkers, 2 + 7 * nplanets))
        init = [jit, mpri]
        for i in range(nplanets):
            init += [msec, sau, esino, ecoso, inc, asc, lam]
        par0 *= np.asarray(init)
        par0 *= 2 ** (np.random.rand(np.prod(par0.shape)).reshape(par0.shape) - 0.5)

    else:

        #################################################################
        # read in the starting positions for the walkers. The next four
        # lines remove parallax and RV zero point from the optimization,
        # change semimajor axis from arcseconds to AU, and bring the
        # number of temperatures used for parallel tempering down to
        # ntemps.
        #################################################################

        par0 = fits.open(start_file)[0].data
        par0[:, :, 8] = par0[:, :, 9]
        par0[:, :, 9] = par0[:, :, 10]
        par0[:, :, 0] /= par0[:, :, 9]
        par0 = par0[:ntemps, :, :-2]
    return par0


def initialize_data(config):
    # load in items from the ConfigParser object
    HipID = config.getint('data_paths', 'HipID', fallback=0)
    RVFile = config.get('data_paths', 'RVFile')
    relRVFile = config.get('data_paths', 'relRVFile', fallback=None)
    AstrometryFile = config.get('data_paths', 'AstrometryFile')
    GaiaDataDir = config.get('data_paths', 'GaiaDataDir', fallback=None)
    Hip2DataDir = config.get('data_paths', 'Hip2DataDir', fallback=None)
    Hip1DataDir = config.get('data_paths', 'Hip1DataDir', fallback=None)
    use_epoch_astrometry = config.getboolean('mcmc_settings', 'use_epoch_astrometry', fallback=False)
    #

    data = orbit.Data(HipID, RVFile, AstrometryFile, relRVfile=relRVFile)
    if use_epoch_astrometry:
        to_jd = lambda x: Time(x, format='decimalyear').jd
        Gaia_fitter = Astrometry('GaiaDR2', '%06d' % (HipID), GaiaDataDir,
                                 central_epoch_ra=to_jd(data.epRA_G),
                                 central_epoch_dec=to_jd(data.epDec_G),
                                 format='jd', normed=False)
        Hip2_fitter = Astrometry('Hip2', '%06d' % (HipID), Hip2DataDir,
                                 central_epoch_ra=to_jd(data.epRA_H),
                                 central_epoch_dec=to_jd(data.epDec_H),
                                 format='jd', normed=False)
        Hip1_fitter = Astrometry('Hip1', '%06d' % (HipID), Hip1DataDir,
                                 central_epoch_ra=to_jd(data.epRA_H),
                                 central_epoch_dec=to_jd(data.epDec_H),
                                 format='jd', normed=False)
        # instantiate C versions of the astrometric fitter which are must faster than HTOF's Astrometry
        hip1_fast_fitter = orbit.AstrometricFitter(Hip1_fitter)
        hip2_fast_fitter = orbit.AstrometricFitter(Hip2_fitter)
        gaia_fast_fitter = orbit.AstrometricFitter(Gaia_fitter)

        data = orbit.Data(HipID, RVFile, AstrometryFile,
                          relRVfile=relRVFile,
                          use_epoch_astrometry=use_epoch_astrometry,
                          epochs_Hip1=Hip1_fitter.data.julian_day_epoch(),
                          epochs_Hip2=Hip2_fitter.data.julian_day_epoch(),
                          epochs_Gaia=Gaia_fitter.data.julian_day_epoch())
    else:
        hip1_fast_fitter, hip2_fast_fitter, gaia_fast_fitter = None, None, None

    return data, hip1_fast_fitter, hip2_fast_fitter, gaia_fast_fitter


def lnprob(theta, returninfo=False, use_epoch_astrometry=False,
           data=None, nplanets=1, H1f=None, H2f=None, Gf=None, max_jitter=20):
    """
    Log likelyhood function for the joint parameters
    :param theta: array-like.
                  [j, mass_primary, mass_secondary, semi-major axis, sqrt(e)sin(w),
                  sqrt(e)cos(w), inclination (rad), ascending node, lam]
                  Note: j is such that sqrt(10^j) = RV jitter in meters per second.
    :param returninfo:
    :param use_epoch_astrometry:
    :param data:
    :param nplanets:
    :param H1f:
    :param H2f:
    :param Gf:
    :param max_jitter: parameter j such that the maximum radial velocity jitter is sqrt(10**j)
    :return:
    """
    model = orbit.Model(data)
    # theta for beta pic c
    # 1220 day period for beta pic c. If refep in Data is set to the periastron time of 2455117 JD
    #theta = np.array([0, 1.84, 0.00852, 2.6948, np.sqrt(0.243) * np.sin(-95.2 * np.pi / 180),
    #                  np.sqrt(0.243) * np.cos(-95.2 * np.pi / 180), 88.9 * np.pi / 180, 211.7 * np.pi / 180,
    #                  -95.2 * np.pi / 180])
    for i in range(nplanets):
        params = orbit.Params(theta, i, nplanets)
        if not np.isfinite(orbit.lnprior(params)):
            model.free()
            return -np.inf

        orbit.calc_EA_RPP(data, params, model)
        orbit.calc_RV(data, params, model)
        orbit.calc_offsets(data, params, model, i)
        #pubmodel = orbit.PublicModel(model, data)
        #import pdb
        #pdb.set_trace()
    
    if use_epoch_astrometry:
        orbit.calc_PMs_epoch_astrometry(data, model, H1f, H2f, Gf)
    else:
        orbit.calc_PMs_no_epoch_astrometry(data, model)

    if returninfo:
        return orbit.calcL(data, params, model, chisq_resids=True)
        
    return orbit.lnprior(params, max_jitter=max_jitter) + orbit.calcL(data, params, model)


def return_one(theta):
    return 1.


def avoid_pickle_lnprob(theta):
    global _loglkwargs
    return lnprob(theta, **_loglkwargs)


def run():
    """
    Initialize and run the MCMC sampler.
    """
    global _loglkwargs
    start_time = time.time()

    args = parse_args()
    config = ConfigParser()
    config.read(args.config_file)

    # set the mcmc parameters
    nwalkers = config.getint('mcmc_settings', 'nwalkers')
    ntemps = config.getint('mcmc_settings', 'ntemps')
    nplanets = config.getint('mcmc_settings', 'nplanets')
    nstep = config.getint('mcmc_settings', 'nstep')
    nthreads = config.getint('mcmc_settings', 'nthreads')
    use_epoch_astrometry = config.getboolean('mcmc_settings', 'use_epoch_astrometry', fallback=False)
    HipID = config.getint('data_paths', 'HipID', fallback=0)
    start_file = config.get('data_paths', 'start_file', fallback='none')
    max_jitter = np.log10(float(config.get('mcmc_settings', 'max_jitter', fallback=1E10)))**2
    # jitter param used by the code is such that the RV jitter is sqrt(10**j)

    # set initial conditions
    par0 = set_initial_parameters(start_file, ntemps, nplanets, nwalkers)
    ndim = par0[0, 0, :].size
    data, H1f, H2f, Gf = initialize_data(config)

    # set arguments for emcee PTSampler and the log-likelyhood (lnprob)
    samplekwargs = {'thin': 50}
    loglkwargs = {'returninfo': False, 'use_epoch_astrometry': use_epoch_astrometry,
                  'data': data, 'nplanets': nplanets, 'H1f': H1f, 'H2f': H2f, 'Gf': Gf,
                  'max_jitter': max_jitter}
    _loglkwargs = loglkwargs
    # run sampler without feeding it loglkwargs directly, since loglkwargs contains non-picklable C objects.
    print('Running MCMC.')
    sample0 = emcee.PTSampler(ntemps, nwalkers, ndim, avoid_pickle_lnprob, return_one, threads=nthreads)
    sample0.run_mcmc(par0, nstep, **samplekwargs)

    print('Total Time: %.2f' % (time.time() - start_time))
    print("Mean acceptance fraction (cold chain): {0:.6f}".format(np.mean(sample0.acceptance_fraction[0, :])))
    # save data
    shape = sample0.lnprobability[0].shape
    parfit = np.zeros((shape[0], shape[1], 8))
    loglkwargs['returninfo'] = True
    for i in range(shape[0]):
        for j in range(shape[1]):
            res = lnprob(sample0.chain[0][i, j], **loglkwargs)
            parfit[i, j] = [res.plx_best, res.pmra_best, res.pmdec_best,
                            res.chisq_sep, res.chisq_PA,
                            res.chisq_H, res.chisq_HG, res.chisq_G]

    out = fits.HDUList(fits.PrimaryHDU(sample0.chain[0].astype(np.float32)))
    out.append(fits.PrimaryHDU(sample0.lnprobability[0].astype(np.float32)))
    out.append(fits.PrimaryHDU(parfit.astype(np.float32)))
    for i in range(1000):
        filename = os.path.join(args.output_dir, 'HIP%d_chain%03d.fits' % (HipID, i))
        if not os.path.isfile(filename):
            print('Writing output to {0}'.format(filename))
            out.writeto(filename, overwrite=False)
            break


if __name__ == "__main__":
    run()
