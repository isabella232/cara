from dataclasses import dataclass
import typing

import numpy as np
from scipy.integrate import quad
from scipy.special import erf
import numpy.testing as npt
import pytest

import cara.monte_carlo as mc
from cara import models,data
from cara.utils import method_cache
from cara.models import _VectorisedFloat,Interval,SpecificInterval
from cara.monte_carlo.sampleable import LogNormal
from cara.monte_carlo.data import (expiration_distributions,
        expiration_BLO_factors,short_range_expiration_distributions,
        short_range_distances,virus_distributions,activity_distributions)

# TODO: seed better the random number generators
np.random.seed(2000)
SAMPLE_SIZE = 1_000_000
TOLERANCE = 0.04

sqrt2pi = np.sqrt(2.*np.pi)
sqrt2 = np.sqrt(2.)
ln2 = np.log(2)

@dataclass(frozen=True)
class SimpleConcentrationModel:
    """
    Simple model for the background (long-range) concentration, without
    all the flexibility of cara.models.ConcentrationModel.
    For independent, end-to-end testing purposes.
    This assumes no mask wearing, and the same ventilation rate at all
    times.
    """

    #: infected people presence interval
    infected_presence: Interval

    #: viral load (RNA copies  / mL)
    viral_load: _VectorisedFloat

    #: breathing rate (m^3/h)
    breathing_rate: _VectorisedFloat

    #: room volume (m^3)
    room_volume: _VectorisedFloat

    #: ventilation rate (air changes per hour) - including HEPA
    lambda_ventilation: _VectorisedFloat

    #: BLO factors
    BLO_factors: typing.Tuple[float, float, float]

    #: number of infected people
    num_infected: int = 1

    #: relative humidity RH
    humidity: float = 0.3

    #: minimum particle diameter considered (microns)
    diameter_min: float = 0.1

    #: maximum particle diameter considered (microns)
    diameter_max: float = 30.

    #: evaporation factor
    evaporation: float = 0.3
    
    #: cn (cm^-3) for resp. the B, L and O modes. Corresponds to the
    # total concentration of aerosols for each mode.
    cn: typing.Tuple[float, float, float] = (0.06, 0.2, 0.0010008)

    # mean of the underlying normal distributions (represents the log of a
    # diameter in microns), for resp. the B, L and O modes.
    mu: typing.Tuple[float, float, float] = (0.989541, 1.38629, 4.97673)

    # std deviation of the underlying normal distribution, for resp.
    # the B, L and O modes.
    sigma: typing.Tuple[float, float, float] = (0.262364, 0.506818, 0.585005)

    def removal_rate(self) -> _VectorisedFloat:
        """
        removal rate lambda in h^-1, excluding the deposition rate.
        """
        hl_calc = ((ln2/((0.16030 + 0.04018*(((293-273.15)-20.615)/10.585)
                                       +0.02176*(((self.humidity*100)-45.235)/28.665)
                                       -0.14369
                                       -0.02636*((293-273.15)-20.615)/10.585)))/60)

        return (self.lambda_ventilation
                + ln2/(np.where(hl_calc <= 0, 6.43, np.minimum(6.43, hl_calc))))

    @method_cache
    def deposition_removal_coefficient(self) -> float:
        """
        coefficient in front of gravitational deposition rate, in h^-1.microns^-2
        Note: 0.4512 = 1.88e-4 * 3600 / 1.5
        """
        return 0.4512*(self.evaporation/2.5)**2

    @method_cache
    def aerosol_volume(self,diameter: float) -> float:
        """
        particle volume in microns^3
        """
        return 4*np.pi/3. * (diameter/2.)**3

    @method_cache
    def Np(self,diameter: float,
           BLO_factors: typing.Tuple[float, float, float]) -> float:
        """
        number of emitted particles per unit volume (BLO model)
        in cm^-3.ln(micron)^-1
        """
        result = 0.
        for cn,mu,sigma,famp in zip(self.cn,self.mu,self.sigma,
                                    BLO_factors):
            result += ( (cn * famp)/sigma * 
                        np.exp(-(np.log(diameter)-mu)**2/(2*sigma**2)))
        return result/(diameter*sqrt2pi)

    def vR(self,diameter: float) -> float:
        """
        emission rate per unit diameter, in RNA copies / h / micron
        """
        return (self.Np(diameter, self.BLO_factors)
                * self.aerosol_volume(diameter) * 1e-6)

    @method_cache
    def f(self, removal_rate: _VectorisedFloat, deltat: float) -> _VectorisedFloat:
        """
        A general function to compute the main integral over diameters
        """
        def integrand(diameter):
            # function to return the integrand
            a = self.deposition_removal_coefficient()
            a_dsquare = a*diameter**2
            return (self.vR(diameter)/(a_dsquare + removal_rate)
                    * np.exp(-a_dsquare*deltat))

        return (quad(integrand,self.diameter_min,self.diameter_max,
                     epsabs=0.,limit=500)[0]
                * self.viral_load * self.breathing_rate)

    def concentration(self,t: float) -> _VectorisedFloat:
        """
        concentration at a given time t
        """
        trans_times = sorted(self.infected_presence.transition_times())
        if t==trans_times[0]:
            return 0.

        lambda_rate = self.removal_rate()
        # transition_times[i] < t <=  transition_times[i]+1
        i: int = np.searchsorted(trans_times,t) - 1 # type: ignore
        ti = trans_times[i]
        Pim1 = (False if i==0 else
            self.infected_presence.triggered((trans_times[i-1]+ti)/2.))
        Pi = self.infected_presence.triggered((ti+trans_times[i+1])/2.)

        result = (0 if not Pim1 else self.f(lambda_rate,t-ti))
        result -= (0 if not Pi else self.f(lambda_rate,t-ti))

        for k,tk in enumerate(trans_times[:i]):
            Pkm1 = (False if k==0 else
                self.infected_presence.triggered((trans_times[k-1]+tk)/2.))
            Pk = self.infected_presence.triggered((tk+trans_times[k+1])/2.)
            s = np.sum([lambda_rate*(trans_times[l]-trans_times[l-1])
                        for l in range(k+1,i+1)])
            result += ( (0 if not Pkm1 else self.f(lambda_rate,t-tk))
                       -(0 if not Pk else self.f(lambda_rate,t-tk))
                       ) * np.exp(-s)

        return ( ( (0 if not self.infected_presence.triggered(t)
                    else self.f(lambda_rate,0))
                  + result * np.exp(-lambda_rate*(t-ti)) )
                * self.num_infected/self.room_volume)


@dataclass(frozen=True)
class SimpleShortRangeModel:
    """
    Simple model for the short-range concentration, without
    all the flexibility of cara.models.ShortRangeModel.
    For independent, end-to-end testing purposes.
    This assumes no mask wearing.
    """
    
    #: time intervals in which a short-range interaction occurs
    interaction_interval: SpecificInterval

    #: tuple with interpersonal distanced from infected person (m)
    distance : _VectorisedFloat = 0.854
    
    #: breathing rate (m^3/h)
    breathing_rate: _VectorisedFloat = 0.51
    
    #: tuple with BLO factors
    BLO_factors: typing.Tuple[float, float, float] = (1,0,0)

    #: minimum diameter for integration (short-range only) (microns)
    diameter_min: float = 0.1

    #: maximum diameter for integration (short-range only) (microns)
    diameter_max: float = 100.
    
    #: mouth opening diameter (m)
    D: float = 0.02
    
    #: duration of the expiration (s)
    tstar: float = 2.
    
    #: Streamwise and radial penetration coefficients
    Cr1: float = 0.18
    Cx1: float = 2.4
    Cr2: float = 0.2
    Cx2: float = 2.2
    
    @method_cache
    def dilution_factor(self) -> _VectorisedFloat:
        """
        computes dilution factor at a certain distance x
        based on Wei JIA matlab script.
        """
        x = np.array(self.distance)
        dilution = np.empty(x.shape, dtype=np.float64)
        # expired flow rate during the expiration period, m^3/s
        Q0 = np.array(self.breathing_rate/3600)
        # the expired flow velocity at the noozle (mouth opening), m/s
        u0 = np.array(Q0/(np.pi/4. * self.D**2))
        # parameters in the jet-like stage
        # position of virtual origin
        x01 = self.D/2/self.Cr1
        # time of virtual origin
        t01 = (x01/self.Cx1)**2 * (Q0*u0)**(-0.5)
        # transition point (in m)
        xstar = np.array(self.Cx1*(Q0*u0)**0.25*(self.tstar + t01)**0.5
                         - x01)
        # dilution factor at the transition point xstar
        Sxstar = np.array(2.*self.Cr1*(xstar+x01)/self.D)

        # calculate dilution factor at the short-range distance x
        dilution[x <= xstar] = 2.*self.Cr1*(x[x <= xstar] + x01)/self.D
        dilution[x > xstar] = Sxstar[x > xstar]*(1. + self.Cr2*(x[x > xstar]
                                - xstar[x > xstar])
                                /self.Cr1/(xstar[x > xstar] + x01))**3

        return dilution

    def jet_concentration(self,conc_model: SimpleConcentrationModel) -> _VectorisedFloat:
        """
        virion concentration at the origin of the jet (close to
        the mouth of the infected person), in m^-3
        we perform the integral of Np(d)*V(d) over diameter analytically
        """
        vl = conc_model.viral_load
        dmin = self.diameter_min
        dmax = self.diameter_max
        result = 0.
        for cn,mu,sigma,famp in zip(conc_model.cn,conc_model.mu,conc_model.sigma,
                                    self.BLO_factors):
            d0 = np.exp(mu)
            ymin = (np.log(dmin)-mu)/(sqrt2*sigma)-3.*sigma/sqrt2
            ymax = (np.log(dmax)-mu)/(sqrt2*sigma)-3.*sigma/sqrt2
            result += ( (cn * famp * d0**3)/2. * np.exp(9*sigma**2/2.) *
                        (erf(ymax) - erf(ymin)) )
        return vl * 1e-6 * result * np.pi/6.

    def concentration(self, conc_model: SimpleConcentrationModel, time: float) -> _VectorisedFloat:
        """
        compute the short-range part of the concentration, and add it
        to the long-range concentration
        """
        if self.interaction_interval.triggered(time):
            lr_concentration = conc_model.concentration(time)
            S = self.dilution_factor()
            return (self.jet_concentration(conc_model)
                    - lr_concentration) / S
        else:
            return 0.


@dataclass(frozen=True)
class SimpleExposureModel(SimpleConcentrationModel):
    """
    Simple model for the background (long-range) exposure, without
    all the flexibility of cara.models.ExposureModel.
    For independent, end-to-end testing purposes.
    This assumes no mask wearing, identical inhalation and exhalation
    breathing rate, indentical presence for the infected and the exposed
    people, the same ventilation rate at all times, and all short-range
    interaction intervals are within presence intervals of the infected.
    """

    #: fraction of infected viruses
    finf: _VectorisedFloat = 0.5

    #: host immunity factor (0. for not immune)
    HI: _VectorisedFloat = 0.

    #: infectious dose (ID50)
    ID50: _VectorisedFloat = 50.

    #: transmissibility factor w.r.t. original strain
    # (<1 means more transmissible)
    transmissibility: _VectorisedFloat = 1.

    #: list of short-range interaction models
    sr_models: typing.Tuple[SimpleShortRangeModel, ...] = ()

    def fdep(self, diameter: float, evaporation: float) -> float:
        """
        fraction deposited
        """
        d = diameter * evaporation
        IFrac = 1 - 0.5 * (1 - (1 / (1 + (0.00076*(d**2.8)))))
        fdep = IFrac * (0.0587
                    + (0.911/(1 + np.exp(4.77 + 1.485 * np.log(d))))
                    + (0.943/(1 + np.exp(0.508 - 2.58 * np.log(d)))))
        return fdep

    @method_cache
    def F(self, removal_rate: _VectorisedFloat, deltat: float,
          evaporation: float) -> _VectorisedFloat:
        """
        A general function to compute the main integral over diameters
        """
        def integrand(diameter):
            # function to return the integrand
            a = self.deposition_removal_coefficient()
            a_dsquare = a*diameter**2
            return -(self.vR(diameter)*self.fdep(diameter,evaporation)/(
                    (a_dsquare + removal_rate)**2)
                    * np.exp(-a_dsquare*deltat))

        return (quad(integrand,self.diameter_min,self.diameter_max,
                epsabs=0.,limit=500)[0]
                * self.viral_load * self.breathing_rate)

    @method_cache
    def f_with_fdep(self, removal_rate: _VectorisedFloat, deltat: float,
                    evaporation: float) -> _VectorisedFloat:
        """
        Same as f but with fdep included in the integral.
        """
        def integrand(diameter):
            # function to return the integrand
            a = self.deposition_removal_coefficient()
            a_dsquare = a*diameter**2
            return (self.vR(diameter)*self.fdep(diameter,evaporation)
                    /(a_dsquare + removal_rate) * np.exp(-a_dsquare*deltat))

        return (quad(integrand,self.diameter_min,self.diameter_max,
                epsabs=0.,limit=500)[0]
                * self.viral_load * self.breathing_rate)

    def total_concentration(self, t: float):
        """
        total concentration at time t
        """
        res = self.concentration(t)
        for sr_mod in self.sr_models:
            res += sr_mod.concentration(self,t)
        return res

    @method_cache
    def integrated_longrange_concentration(self,t1: float,t2: float,
                            evaporation: float) -> _VectorisedFloat:
        """
        background (long-range) concentration integrated from t1 to t2
        assuming both t1 and t2 are within a single presence interval.
        This includes the deposition fraction (fdep).
        """
        trans_times = sorted(self.infected_presence.transition_times())
        if t2==trans_times[0]:
            return 0.

        lambda_rate = self.removal_rate()
        # transition_times[i] < t2 <=  transition_times[i]+1
        i: int = np.searchsorted(trans_times,t2) - 1 # type: ignore
        ti = trans_times[i]
        if np.searchsorted(trans_times,t1,side='right')-1 != i:
            raise ValueError("t1={}, t2={}, i1={}, i2={} "
                             "(i1 and i2 should be equal)".format(
                t1,t2,i,np.searchsorted(trans_times,t1,side='right')-1))

        Pim1 = (False if i==0 else
            self.infected_presence.triggered((trans_times[i-1]+ti)/2.))
        Pi = self.infected_presence.triggered((ti+trans_times[i+1])/2.)

        def primitive(time):
            result = (0 if not Pim1 else self.F(lambda_rate,time-ti,evaporation))
            result -= (0 if not Pi else self.F(lambda_rate,time-ti,evaporation))

            for k,tk in enumerate(trans_times[:i]):
                Pkm1 = (False if k==0 else
                    self.infected_presence.triggered((trans_times[k-1]+tk)/2.))
                Pk = self.infected_presence.triggered((tk+trans_times[k+1])/2.)
                s = np.sum([lambda_rate*(trans_times[l]-trans_times[l-1])
                            for l in range(k+1,i+1)])
                result += ( (0 if not Pkm1 else self.F(lambda_rate,time-tk,evaporation))
                           -(0 if not Pk else self.F(lambda_rate,time-tk,evaporation))
                           ) * np.exp(-s)
            return result

        return ( ( (0 if not self.infected_presence.triggered((t1+t2)/2.)
                    else self.f_with_fdep(lambda_rate,0,evaporation)*(t2-t1))
                  + (primitive(t2) * np.exp(-lambda_rate*(t2-ti)) -
                     primitive(t1) * np.exp(-lambda_rate*(t1-ti)) ) )
                * self.num_infected/self.room_volume)

    @method_cache
    def integrated_shortrange_concentration(self) -> _VectorisedFloat:
        """
        short-range concentration integrated over interaction times and
        diameters. This includes the deposition fraction (fdep).
        """
        result = 0.
        # evaporation set to 1 (particles do not have time to evaporate)
        evaporation = 1.
        for sr_model in self.sr_models:
            t1,t2 = sr_model.interaction_interval.boundaries()[0]
            # function to return the integrand
            integrand = lambda d: (self.aerosol_volume(d)
                    * self.Np(d,sr_model.BLO_factors)*self.fdep(d,evaporation))
            res = (quad(integrand,
                        sr_model.diameter_min,sr_model.diameter_max,
                        epsabs=0.,limit=500)[0]
                   * self.viral_load * 1e-6 * (t2-t1) )
            result += sr_model.breathing_rate * (
                        res-self.integrated_longrange_concentration(t1,t2,evaporation)
                        )/sr_model.dilution_factor()

        return result

    def dose(self) -> _VectorisedFloat:
        """
        total deposited dose (integrated over time and over particle
        diameters), including short and long range.
        """
        result = 0.
        for t1,t2 in self.infected_presence.boundaries():
            result += (self.integrated_longrange_concentration(t1,t2,self.evaporation)
                       * self.breathing_rate)

        result += self.integrated_shortrange_concentration()

        return result * self.finf * (1. - self.HI)

    def probability_infection(self):
        """
        total probability of infection
        """
        return (1. - np.exp(-self.dose() * ln2 * (1-self.HI)
                           /(self.ID50 * self.transmissibility) )) * 100.


presence = models.SpecificInterval(present_times=((8.5, 12), (13, 17.5)))
interaction_intervals = (models.SpecificInterval(present_times=((10.5, 11.0),)),
                         models.SpecificInterval(present_times=((14.5, 15.0),))
                         )


@pytest.fixture
def c_model() -> mc.ConcentrationModel:
    return mc.ConcentrationModel(
        room=models.Room(volume=50, inside_temp=models.PiecewiseConstant((0., 24.), (293,)), humidity=0.3),
        ventilation=models.AirChange(active=models.PeriodicInterval(period=120, duration=120), air_exch=1.),
        infected=mc.InfectedPopulation(
            number=1,
            presence=presence,
            virus=models.Virus.types['SARS_CoV_2_DELTA'],
            mask=models.Mask.types['No mask'],
            activity=models.Activity.types['Seated'],
            expiration=expiration_distributions['Breathing'],
            host_immunity=0.,
        ),
        evaporation_factor=0.3,
    ).build_model(SAMPLE_SIZE)


@pytest.fixture
def c_model_distr() -> mc.ConcentrationModel:
    return mc.ConcentrationModel(
        room=models.Room(volume=50, humidity=0.3),
        ventilation=models.AirChange(active=models.PeriodicInterval(
                            period=120, duration=120), air_exch=1.),
        infected=mc.InfectedPopulation(
            number=1,
            presence=presence,
            virus=virus_distributions['SARS_CoV_2_DELTA'],
            mask=models.Mask.types['No mask'],
            activity=activity_distributions['Seated'],
            expiration=expiration_distributions['Breathing'],
            host_immunity=0.,
        ),
        evaporation_factor=0.3,
    ).build_model(SAMPLE_SIZE)


@pytest.fixture
def sr_models() -> typing.Tuple[mc.ShortRangeModel, ...]:
    return (
        mc.ShortRangeModel(
            expiration = short_range_expiration_distributions['Speaking'],
            activity = models.Activity.types['Seated'],
            presence = interaction_intervals[0],
            distance = 0.854,
        ).build_model(SAMPLE_SIZE),
        mc.ShortRangeModel(
            expiration = short_range_expiration_distributions['Breathing'],
            activity = models.Activity.types['Heavy exercise'],
            presence = interaction_intervals[1],
            distance = 0.854,
        ).build_model(SAMPLE_SIZE),
    )


@pytest.fixture
def simple_c_model() -> SimpleConcentrationModel:
    return SimpleConcentrationModel(
        infected_presence = presence,
        viral_load        = models.Virus.types['SARS_CoV_2_DELTA'].viral_load_in_sputum,
        breathing_rate    = models.Activity.types['Seated'].exhalation_rate,
        room_volume       = 50.,
        lambda_ventilation= 1.,
        BLO_factors       = expiration_BLO_factors['Breathing'],
    )


@pytest.fixture
def simple_sr_models() -> typing.Tuple[SimpleShortRangeModel, ...]:
    return (
        SimpleShortRangeModel(
            interaction_interval = interaction_intervals[0],
            distance = 0.854,
            breathing_rate = models.Activity.types['Seated'].exhalation_rate,
            BLO_factors = expiration_BLO_factors['Speaking'],
        ),
        SimpleShortRangeModel(
            interaction_interval = interaction_intervals[1],
            distance = 0.854,
            breathing_rate = models.Activity.types['Heavy exercise'].exhalation_rate,
            BLO_factors = expiration_BLO_factors['Breathing'],
        ),
    )


@pytest.fixture
def expo_sr_model(c_model,sr_models) -> mc.ExposureModel:
    return mc.ExposureModel(
        concentration_model=c_model,
        short_range=sr_models,
        exposed=mc.Population(
            number=1,
            presence=presence,
            mask=models.Mask.types['No mask'],
            activity=models.Activity.types['Seated'],
            host_immunity=0.,
        ),
    ).build_model(SAMPLE_SIZE)


@pytest.fixture
def simple_expo_sr_model(simple_sr_models) -> SimpleExposureModel:
    return SimpleExposureModel(
        infected_presence = presence,
        viral_load        = models.Virus.types['SARS_CoV_2_DELTA'].viral_load_in_sputum,
        breathing_rate    = models.Activity.types['Seated'].exhalation_rate,
        room_volume       = 50.,
        lambda_ventilation= 1.,
        BLO_factors       = expiration_BLO_factors['Breathing'],
        finf              = models.Virus.types['SARS_CoV_2_DELTA'].viable_to_RNA_ratio,
        HI                = 0.,
        ID50              = models.Virus.types['SARS_CoV_2_DELTA'].infectious_dose,
        transmissibility  = models.Virus.types['SARS_CoV_2_DELTA'].transmissibility_factor,
        sr_models         = simple_sr_models,
    )


@pytest.fixture
def expo_sr_model_distr(c_model_distr) -> mc.ExposureModel:
    return mc.ExposureModel(
        concentration_model=c_model_distr,
        short_range=(
            mc.ShortRangeModel(
                expiration = short_range_expiration_distributions['Breathing'],
                activity = activity_distributions['Seated'],
                presence = interaction_intervals[0],
                distance = short_range_distances,
            ).build_model(SAMPLE_SIZE),
            mc.ShortRangeModel(
                expiration = short_range_expiration_distributions['Speaking'],
                activity = activity_distributions['Seated'],
                presence = interaction_intervals[1],
                distance = short_range_distances,
            ).build_model(SAMPLE_SIZE),
        ),
        exposed=mc.Population(
            number=1,
            presence=presence,
            mask=models.Mask.types['No mask'],
            activity=models.Activity.types['Seated'],
            host_immunity=0.,
        ),
    ).build_model(SAMPLE_SIZE)


@pytest.fixture
def simple_expo_sr_model_distr(c_model_distr) -> SimpleExposureModel:
    return SimpleExposureModel(
        infected_presence = presence,
        viral_load        = virus_distributions['SARS_CoV_2_DELTA'
                        ].build_model(SAMPLE_SIZE).viral_load_in_sputum,
        breathing_rate    = activity_distributions['Seated'].build_model(
                                            SAMPLE_SIZE).exhalation_rate,
        room_volume       = 50.,
        lambda_ventilation= 1.,
        BLO_factors       = expiration_BLO_factors['Breathing'],
        finf              = virus_distributions['SARS_CoV_2_DELTA'
                        ].build_model(SAMPLE_SIZE).viable_to_RNA_ratio,
        HI                = 0.,
        ID50              = virus_distributions['SARS_CoV_2_DELTA'
                        ].build_model(SAMPLE_SIZE).infectious_dose,
        transmissibility  = virus_distributions['SARS_CoV_2_DELTA'
                        ].transmissibility_factor,
        sr_models         = (
            SimpleShortRangeModel(
                interaction_interval = interaction_intervals[0],
                distance = short_range_distances.generate_samples(SAMPLE_SIZE),
                breathing_rate = activity_distributions['Seated'].build_model(
                                            SAMPLE_SIZE).exhalation_rate,
                BLO_factors = expiration_BLO_factors['Breathing'],
            ),
            SimpleShortRangeModel(
                interaction_interval = interaction_intervals[1],
                distance = short_range_distances.generate_samples(SAMPLE_SIZE),
                breathing_rate = activity_distributions['Seated'].build_model(
                                            SAMPLE_SIZE).exhalation_rate,
                BLO_factors = expiration_BLO_factors['Speaking'],
            )
        ),
    )


@pytest.mark.parametrize(
    "time", np.linspace(8.5,17.5,12),
)
def test_longrange_concentration(time,c_model,simple_c_model):
    npt.assert_allclose(
        c_model.concentration(time).mean(),
        simple_c_model.concentration(time), rtol=TOLERANCE
        )


@pytest.mark.parametrize(
    "time", [10, 10.7, 11., 12.5, 14.75, 14.9, 17]
)
def test_shortrange_concentration(time,c_model,simple_c_model,
                                  sr_models,simple_sr_models):
    result_sr_model = np.sum([np.array(
            sr_mod.short_range_concentration(c_model,time)).mean()
        for sr_mod in sr_models])
    result_simple_sr_model = np.sum([np.array(
            sr_mod.concentration(simple_c_model,time)).mean()
        for sr_mod in simple_sr_models])
    npt.assert_allclose(
        result_sr_model,result_simple_sr_model,rtol=TOLERANCE
        )


def test_longrange_exposure(c_model):
    simple_expo_model = SimpleExposureModel(
        infected_presence = presence,
        viral_load        = models.Virus.types['SARS_CoV_2_DELTA'].viral_load_in_sputum,
        breathing_rate    = models.Activity.types['Seated'].exhalation_rate,
        room_volume       = 50.,
        lambda_ventilation= 1.,
        BLO_factors       = expiration_BLO_factors['Breathing'],
        finf              = models.Virus.types['SARS_CoV_2_DELTA'].viable_to_RNA_ratio,
        HI                = 0.,
        ID50              = models.Virus.types['SARS_CoV_2_DELTA'].infectious_dose,
        transmissibility  = models.Virus.types['SARS_CoV_2_DELTA'].transmissibility_factor,
        sr_models         = (),
    )
    expo_model = mc.ExposureModel(
            concentration_model=c_model,
            short_range=(),
            exposed=mc.Population(
                number=1,
                presence=presence,
                mask=models.Mask.types['No mask'],
                activity=models.Activity.types['Seated'],
                host_immunity=0.,
            ),
    ).build_model(SAMPLE_SIZE)
    npt.assert_allclose(
        expo_model.deposited_exposure().mean(),
        simple_expo_model.dose().mean(), rtol=TOLERANCE
        )
    npt.assert_allclose(
        expo_model.infection_probability().mean(),
        simple_expo_model.probability_infection().mean(), rtol=TOLERANCE
        )


@pytest.mark.parametrize(
    "time", [11., 12.5, 17.]
)
def test_longrange_concentration_with_distributions(c_model_distr,time):
    simple_expo_model = SimpleConcentrationModel(
        infected_presence = presence,
        viral_load        = virus_distributions['SARS_CoV_2_DELTA'
                        ].build_model(SAMPLE_SIZE).viral_load_in_sputum,
        breathing_rate    = activity_distributions['Seated'].build_model(
                                            SAMPLE_SIZE).exhalation_rate,
        room_volume       = 50.,
        lambda_ventilation= 1.,
        BLO_factors       = expiration_BLO_factors['Breathing'],
    )
    npt.assert_allclose(
        c_model_distr.concentration(time).mean(),
        simple_expo_model.concentration(time).mean(), rtol=TOLERANCE
        )


def test_longrange_exposure_with_distributions(c_model_distr):
    simple_expo_model = SimpleExposureModel(
        infected_presence = presence,
        viral_load        = virus_distributions['SARS_CoV_2_DELTA'
                        ].build_model(SAMPLE_SIZE).viral_load_in_sputum,
        breathing_rate    = activity_distributions['Seated'].build_model(
                                            SAMPLE_SIZE).exhalation_rate,
        room_volume       = 50.,
        lambda_ventilation= 1.,
        BLO_factors       = expiration_BLO_factors['Breathing'],
        finf              = virus_distributions['SARS_CoV_2_DELTA'
                        ].build_model(SAMPLE_SIZE).viable_to_RNA_ratio,
        HI                = 0.,
        ID50              = virus_distributions['SARS_CoV_2_DELTA'
                        ].build_model(SAMPLE_SIZE).infectious_dose,
        transmissibility  = virus_distributions['SARS_CoV_2_DELTA'
                        ].transmissibility_factor,
        sr_models         = (),
    )
    expo_model = mc.ExposureModel(
            concentration_model=c_model_distr,
            short_range=(),
            exposed=mc.Population(
                number=1,
                presence=presence,
                mask=models.Mask.types['No mask'],
                activity=activity_distributions['Seated'],
                host_immunity=0.,
            ),
    ).build_model(SAMPLE_SIZE)
    npt.assert_allclose(
        expo_model.deposited_exposure().mean(),
        simple_expo_model.dose().mean(), rtol=TOLERANCE
        )
    npt.assert_allclose(
        expo_model.infection_probability().mean(),
        simple_expo_model.probability_infection().mean(), rtol=TOLERANCE
        )


# tests on the concentration with short-range should be skipped until
# one finds a way to avoid the large variability of the concentration
# with short-range 'Speaking' or 'Shouting' interactions
@pytest.mark.skip
@pytest.mark.parametrize(
    "time", [10.75, 14.75, 16.]
)
def test_concentration_with_shortrange(expo_sr_model,simple_expo_sr_model,time):
    npt.assert_allclose(
        expo_sr_model.concentration(time).mean(),
        simple_expo_sr_model.total_concentration(time).mean(), rtol=TOLERANCE
        )


def test_exposure_with_shortrange(expo_sr_model,simple_expo_sr_model):
    npt.assert_allclose(
        expo_sr_model.deposited_exposure().mean(),
        simple_expo_sr_model.dose().mean(), rtol=TOLERANCE
        )
    npt.assert_allclose(
        expo_sr_model.infection_probability().mean(),
        simple_expo_sr_model.probability_infection().mean(), rtol=TOLERANCE
        )


@pytest.mark.skip
@pytest.mark.parametrize(
    "time", [10.75, 14.75, 16.]
)
def test_concentration_with_shortrange_and_distributions(
                    expo_sr_model_distr,simple_expo_sr_model_distr,time):
    npt.assert_allclose(
        expo_sr_model_distr.concentration(time).mean(),
        simple_expo_sr_model_distr.total_concentration(time).mean(),
        rtol=TOLERANCE
        )


def test_exposure_with_shortrange_and_distributions(expo_sr_model_distr,
                                            simple_expo_sr_model_distr):
    npt.assert_allclose(
        expo_sr_model_distr.deposited_exposure().mean(),
        simple_expo_sr_model_distr.dose().mean(), rtol=0.05
        )
    npt.assert_allclose(
        expo_sr_model_distr.infection_probability().mean(),
        simple_expo_sr_model_distr.probability_infection().mean(),
        rtol=0.03
        )

