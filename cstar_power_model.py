#!/usr/bin/env python3
"""
================================================================================
C-Star Baseline Power Budget Model  --  high-latitude solar/battery simulation
================================================================================

PURPOSE
-------
Before asking "should we bolt a wind turbine or a wave harvester onto a C-Star?",
we need to know how big the problem actually is. This model answers one question:

    At latitude X, does a 50 W-peak solar array keep a 1 W average load alive
    off a 1300 Wh LiFePO4 battery, all year round?

The output is a state-of-charge (SoC) trace over time, plus the size of the
energy deficit if there is one. That deficit -- in average watts -- is the
target any alternative harvester has to hit.

WHAT IS MODELLED
----------------
  1. Solar geometry     : declination, hour angle, elevation -> day/night cycle
                          and sun height in sky (Cooper / standard astronomy)
  2. Panel-to-sun angle : deck-mounted horizontal panel, cos(incidence) = sin(elev)
  3. Atmosphere         : Kasten-Young air mass + Meinel clear-sky attenuation
  4. Weather            : stochastic cloud (AR(1), day-to-day persistent),
                          latitude-dependent mean clearness
  5. Sail shading       : constant derate factor (see ASSUMPTION A6)
  6. Temperature        : ocean-surface climatology by latitude and season,
                          driving BOTH panel efficiency and battery capacity
  7. Battery            : temperature capacity derate, sub-zero charge inhibit,
                          calendar fade (Arrhenius) + cycle fade (throughput)
  8. Load               : constant 1 W (per the brief)

USAGE
-----
    python cstar_power_model.py                     # default 60 deg N, 1 year
    python cstar_power_model.py --lat 70            # 70 deg N
    python cstar_power_model.py --lat -55           # 55 deg S (Southern Ocean)
    python cstar_power_model.py --lat 60 --years 3  # show battery fade
    python cstar_power_model.py --sweep             # latitude x season envelope

Outputs PNG figures and a CSV of daily results next to this script.

Every number that is an assumption rather than a given is tagged ASSUMPTION A#
and listed by --assumptions. All are collected in the CONFIG block below so
they can be challenged and swapped in one place.
================================================================================
"""

from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np
import matplotlib

matplotlib.use("Agg")  # headless: write PNGs, never open a window
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap

HERE = Path(__file__).resolve().parent

# ==============================================================================
# PLOT PALETTE
# Validated categorical palette (adjacent-pair CVD-safe). Slots used in order.
# ==============================================================================
BLUE = "#2a78d6"   # slot 1 -- solar / generation
ORANGE = "#eb6834" # slot 2 -- load / demand
AQUA = "#1baf7a"   # slot 3 -- state of charge
RED = "#e34948"    # status: critical (blackout)
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#8a8983"
SURFACE = "#fcfcfb"
GRID = "#e3e2de"

# Sequential blue ramp (light -> dark) for the magnitude heatmap
BLUE_RAMP = LinearSegmentedColormap.from_list(
    "blue_seq", ["#cde2fb", "#9ec5f4", "#5598e7", "#2a78d6", "#184f95", "#0d366b"]
)


def _style():
    """Recessive grid, thin marks, text in ink tokens -- never in series colour."""
    plt.rcParams.update({
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "axes.edgecolor": GRID,
        "axes.labelcolor": INK2,
        "axes.titlecolor": INK,
        "axes.titlesize": 11,
        "axes.titleweight": "bold",
        "axes.labelsize": 9,
        "axes.grid": True,
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 0.7,
        "xtick.color": INK2,
        "ytick.color": INK2,
        "xtick.labelsize": 8.5,
        "ytick.labelsize": 8.5,
        "legend.frameon": False,
        "legend.fontsize": 8.5,
        "legend.labelcolor": INK2,
        "lines.linewidth": 1.6,
        "font.size": 9,
        "savefig.facecolor": SURFACE,
        "savefig.bbox": "tight",
        "savefig.dpi": 160,
    })


# ==============================================================================
# CONFIGURATION -- givens from the brief, and labelled assumptions
# ==============================================================================
@dataclass
class Config:
    # ---------------- GIVEN in the assessment brief ----------------
    p_solar_peak_w: float = 50.0      # GIVEN: 50 W peak solar generation
    batt_nominal_wh: float = 1300.0   # GIVEN: 1300 Wh LiFePO4
    load_w: float = 1.0               # GIVEN: ~1 W average electrical consumption

    # ---------------- ASSUMPTIONS ----------------
    # A1: The 50 W peak is a Standard Test Condition rating -- 1000 W/m^2, 25 C
    #     cell temperature, AM1.5. This lets us scale power by irradiance without
    #     needing panel area or cell efficiency separately.
    #     TO FIRM UP: read the actual module datasheet / measure Isc-Voc on a
    #     clear day at known irradiance with a reference cell.
    irradiance_stc: float = 1000.0

    # A2: Panels are deck-mounted and effectively HORIZONTAL. For a horizontal
    #     plane the panel-to-sun incidence angle equals the solar zenith angle,
    #     so cos(incidence) = sin(elevation). This is the single most punishing
    #     assumption at high latitude -- a low sun hits a flat deck at a glancing
    #     angle and the cosine loss is brutal.
    #     TO FIRM UP: get the real panel layout/curvature from CAD; consider
    #     whether any panel area is on vertical/near-vertical surfaces (hull
    #     sides or the wingsail itself), which HELPS at low sun elevation.
    panel_tilt_deg: float = 0.0       # 0 = flat on deck

    # A3: Silicon temperature coefficient -0.40 %/K referenced to 25 C.
    #     TO FIRM UP: module datasheet (typical range -0.29 to -0.45 %/K).
    panel_temp_coeff_per_k: float = -0.0040

    # A4: Cell-temperature rise above ambient. A marine deck panel is
    #     continuously wind- and spray-cooled, so it runs far cooler than a
    #     rooftop install. 0.025 K per W/m^2 ~ NOCT 40 C.
    #     TO FIRM UP: tape a thermocouple to the back of a panel on a test hull.
    panel_temp_rise_k_per_wm2: float = 0.025

    # A5: Diffuse fraction under clear sky, as a multiplier on beam-on-horizontal.
    #     Under overcast the model scales total GHI by the clearness index, which
    #     implicitly makes the residual light diffuse -- correct for a flat panel
    #     that sees the whole sky dome.
    clear_sky_diffuse_frac: float = 0.12

    # A6: SAIL SHADING. The rigid wingsail rotates continuously relative to the
    #     deck and the sun, so on average it shadows some of the array. Modelled
    #     as a flat 10 % loss.
    #     NOTE: this is optimistic at high latitude -- a low sun casts a long
    #     shadow, so real shading loss in winter is likely WORSE than 10 %.
    #     TO FIRM UP: ray-trace the CAD model over a hemisphere of sun positions
    #     and vehicle headings; or measure per-string current on a test rig.
    sail_shading_factor: float = 0.90

    # A7: Soiling / salt-spray / optical fouling losses.
    #     TO FIRM UP: measured degradation from returned vehicles vs. days at sea.
    soiling_factor: float = 0.95

    # A8: MPPT + charge-path conversion efficiency.
    #     TO FIRM UP: bench measurement of the actual charge controller.
    mppt_efficiency: float = 0.93

    # A9: Mean clearness index falls with latitude -- high latitudes are cloudier.
    #     kt_mean = a - b*|lat| gives ~0.60 at the equator, ~0.39 at 60 deg,
    #     consistent with published Southern Ocean / North Atlantic climatology.
    #     TO FIRM UP: pull real monthly irradiance for the customer's actual
    #     operating box from NASA POWER or PVGIS. THIS IS THE #1 DATA GAP.
    kt_a: float = 0.60
    kt_b: float = 0.0035
    kt_seasonal_amp: float = 0.05     # winters slightly cloudier than summers
    kt_persistence: float = 0.72      # AR(1) day-to-day: weather comes in spells
    kt_sigma: float = 0.16            # day-to-day spread
    kt_min: float = 0.08              # thickest overcast still passes ~8 %
    kt_max: float = 1.00

    # A10: Sea-surface temperature climatology, T = 28 - 0.0055*lat^2, floored at
    #      the freezing point of seawater. Seasonal swing grows with latitude but
    #      stays small (ocean thermal inertia).
    #      TO FIRM UP: real SST climatology for the operating area.
    sst_c0: float = 28.0
    sst_c2: float = 0.0055
    sst_freeze_c: float = -1.8
    sst_amp_base: float = 1.0
    sst_amp_per_deg: float = 0.035

    # A11: Battery sits inside the hull, thermally dominated by the surrounding
    #      seawater (huge thermal mass, thin hull). So T_battery ~ T_sea. Air
    #      temperature is used for the panel and is allowed to swing wider.
    #      TO FIRM UP: internal temperature logs from a deployed vehicle.
    air_temp_offset_k: float = -1.0
    air_temp_amp_mult: float = 1.5

    # A12: LiFePO4 usable-capacity derate vs temperature (discharge).
    #      TO FIRM UP: cell datasheet capacity-vs-temperature curves.
    batt_temp_points_c: tuple = (-20.0, -10.0, 0.0, 10.0, 25.0, 45.0)
    batt_temp_factor: tuple = (0.60, 0.76, 0.88, 0.96, 1.00, 1.00)

    # A13: HARD CONSTRAINT, not really an assumption -- charging a lithium cell
    #      below 0 C plates metallic lithium and permanently damages it. Every
    #      LiFePO4 BMS blocks charge below ~0 C. At high latitude this can mean
    #      the battery refuses the little solar you do get.
    batt_charge_min_c: float = 0.0

    # A14: Calendar fade 2 %/year at 25 C, sqrt(time), Arrhenius-scaled by
    #      temperature with Ea = 50 kJ/mol.
    #      TO FIRM UP: cell manufacturer calendar-life data.
    cal_fade_per_year_at_25c: float = 0.02
    cal_fade_activation_j_per_mol: float = 50_000.0

    # A15: Cycle life 3000 equivalent full cycles to 80 % capacity.
    #      TO FIRM UP: cell datasheet cycle life at the relevant depth of discharge.
    cycle_life_efc: float = 3000.0
    cycle_life_end_fade: float = 0.20

    # ---------------- Simulation controls ----------------
    latitude_deg: float = 60.0
    years: float = 1.0
    timestep_h: float = 1.0
    initial_soc_frac: float = 1.0
    rng_seed: int = 42
    # ==========================================================================
    # ALTERNATIVE GENERATION -- switch on to add to the solar array
    # ==========================================================================
    enable_wind_turbine: bool = False
    enable_wave_harvester: bool = False
    enable_water_turbine: bool = False

    # ---- Wind climatology -----------------------------------------------------
    # A17: Mean 10 m ocean wind rises with latitude and peaks in winter -- the
    #      same season the sun disappears. Trades ~7 m/s, N Atlantic winter
    #      ~13 m/s. TO FIRM UP: ERA5 reanalysis for the operating box.
    wind_mean_base: float = 6.5
    wind_mean_per_deg: float = 0.070
    wind_seasonal_frac: float = 0.28      # winter windier than summer
    wind_persistence: float = 0.80        # AR(1), daily: gales last days
    # A18: Turbine hub sits ~1 m above the sea, not at the 10 m reference height.
    #      Power-law shear over water, alpha ~ 0.11.
    wind_hub_height_m: float = 1.0
    wind_shear_alpha: float = 0.11

    # ---- Wind turbine ---------------------------------------------------------
    # A19: A micro horizontal-axis rotor sized to fit a 1 m hull. Cp is low
    #      because small rotors run at low Reynolds number.
    #      TO FIRM UP: buy a Rutland/Ampair-class unit and bench-test its curve.
    wt_diameter_m: float = 0.20
    wt_cp: float = 0.30
    wt_efficiency: float = 0.65           # generator + rectifier + MPPT
    wt_cut_in_ms: float = 3.0
    wt_rated_w: float = 12.0              # electrical limit of the machine
    wt_furl_ms: float = 25.0              # above this it must shut down to survive

    # ---- Wave climatology -----------------------------------------------------
    # A20: Significant wave height scales with latitude and season, roughly in
    #      step with the wind. TO FIRM UP: ERA5 / WaveWatch III hindcast.
    wave_hs_base: float = 1.5
    wave_hs_per_deg: float = 0.045
    wave_hs_seasonal_frac: float = 0.30

    # ---- Wave harvester -------------------------------------------------------
    # A21: A 1 m hull is far shorter than an ocean wavelength (50-150 m), so it
    #      is a WAVE FOLLOWER -- it rides the surface rather than moving relative
    #      to it. Power can therefore only come from an internal proof mass
    #      reacting against the hull's own acceleration, not from hull-to-water
    #      relative motion. Energy per cycle ~ 2*m*a*s, so power scales with the
    #      proof mass and, critically, with the available STROKE.
    #      TO FIRM UP: instrument a hull with an IMU at sea and integrate the
    #      measured acceleration spectrum -- this is a cheap, decisive test.
    wave_proof_mass_kg: float = 2.0
    wave_stroke_m: float = 0.06           # usable travel inside a 1 m hull
    wave_pto_efficiency: float = 0.50     # power take-off + conditioning
    wave_capture_factor: float = 0.60     # fraction of ideal stroke actually used

    # ---- Water turbine (propeller regeneration under sail) --------------------
    # A22: The existing propeller, freewheeling while the vehicle sails. Water is
    #      ~830x denser than air, so a small disc at 1 m/s is worth far more than
    #      the same disc in wind. Cp is poor because a propeller is not designed
    #      as a turbine. TO FIRM UP: tow-tank test, measuring both power out and
    #      the drag penalty in.
    turb_diameter_m: float = 0.10
    turb_cp: float = 0.25
    turb_efficiency: float = 0.60
    turb_cut_in_ms: float = 0.35
    # A23: Boat speed from wind speed, capped at displacement hull speed for a
    #      1 m waterline (1.34*sqrt(L_ft) kn ~ 1.25 m/s).
    boat_speed_per_wind: float = 0.15
    boat_hull_speed_ms: float = 1.25

    start_day: int = 1                # day-of-year the mission begins
    extra_power_w: float = 0.0        # constant additional generation (the thing
                                      # a wind turbine or wave harvester would add)


R_GAS = 8.314          # J/(mol K)
SOLAR_CONSTANT = 1361  # W/m^2

# A16: Pack-level LiFePO4 gravimetric energy density, including cells, BMS,
#      wiring and potting. Cell-level is ~150 Wh/kg; pack-level is lower.
#      Used to convert "just add more battery" into a mass penalty on a 40 kg
#      vehicle, so the storage option can be compared fairly against a harvester.
#      TO FIRM UP: weigh the existing C-Star pack and divide.
BATT_WH_PER_KG = 110.0


# ==============================================================================
# 1. SOLAR GEOMETRY -- day/night cycle and sun angle in the sky
# ==============================================================================
def solar_declination_deg(day_of_year: np.ndarray) -> np.ndarray:
    """
    Cooper's equation. Declination is the latitude at which the sun is directly
    overhead; it swings +/-23.45 deg over the year and is what creates seasons
    (and, at high latitude, polar night).
    """
    return 23.45 * np.sin(np.deg2rad(360.0 * (284.0 + day_of_year) / 365.0))


def solar_elevation_deg(lat_deg: float, decl_deg: np.ndarray, hour_solar: np.ndarray) -> np.ndarray:
    """
    Standard solar-position relation:

        sin(elevation) = sin(lat)sin(decl) + cos(lat)cos(decl)cos(hour_angle)

    The hour angle is 15 deg per hour from solar noon. Negative elevation = night,
    which is how the day/night cycle falls out of the model for free. At high
    latitude in winter the elevation can stay negative for weeks -- polar night.
    """
    lat = np.deg2rad(lat_deg)
    decl = np.deg2rad(decl_deg)
    hour_angle = np.deg2rad(15.0 * (hour_solar - 12.0))
    sin_elev = np.sin(lat) * np.sin(decl) + np.cos(lat) * np.cos(decl) * np.cos(hour_angle)
    return np.rad2deg(np.arcsin(np.clip(sin_elev, -1.0, 1.0)))


def air_mass(elev_deg: np.ndarray) -> np.ndarray:
    """
    Kasten & Young (1989). Air mass is how much atmosphere the beam traverses
    relative to straight overhead. At 60 deg latitude in December the noon sun is
    only ~6 deg up, giving air mass ~9 -- the beam crosses nine atmospheres and
    most of it is absorbed. This is a big part of why high-latitude winter solar
    is so poor, on top of the short day and the cosine loss.
    """
    e = np.maximum(elev_deg, 0.0)
    denom = np.sin(np.deg2rad(e)) + 0.50572 * np.power(e + 6.07995, -1.6364)
    am = np.where(elev_deg > 0.0, 1.0 / np.maximum(denom, 1e-9), np.inf)
    return np.minimum(am, 40.0)


def clear_sky_ghi(elev_deg: np.ndarray, cfg: Config) -> np.ndarray:
    """
    Clear-sky global horizontal irradiance on the deck, W/m^2.

    Three separate penalties stack up at high latitude, and it is worth being
    explicit that they are three DIFFERENT effects, all driven by low sun:
      (1) atmospheric attenuation via air mass    -> the 0.7^(AM^0.678) term
      (2) cosine loss onto a horizontal panel     -> the sin(elev) term
      (3) short or zero day length                -> elevation <= 0 for most hours
    """
    am = air_mass(elev_deg)
    # Meinel & Meinel clear-sky beam attenuation
    dni = np.where(np.isfinite(am), SOLAR_CONSTANT * np.power(0.7, np.power(am, 0.678)), 0.0)
    sin_elev = np.maximum(np.sin(np.deg2rad(elev_deg)), 0.0)
    beam_horizontal = dni * sin_elev                       # panel-to-sun cosine term
    return beam_horizontal * (1.0 + cfg.clear_sky_diffuse_frac)


# ==============================================================================
# 2. WEATHER -- persistent stochastic cloud
# ==============================================================================
def clearness_series(n_days: int, lat_deg: float, cfg: Config, rng: np.random.Generator) -> np.ndarray:
    """
    Daily clear-sky index kt in [0,1]: the fraction of clear-sky irradiance that
    actually reaches the deck.

    Modelled as a mean-reverting AR(1) process so that weather is PERSISTENT --
    a week of overcast is a real, and for this problem a decisive, event. Drawing
    kt independently each day would badly understate the risk, because the thing
    that kills the vehicle is a long cloudy spell in a low-sun season, not the
    annual average.
    """
    lat_abs = abs(lat_deg)
    kt_mean_annual = cfg.kt_a - cfg.kt_b * lat_abs

    day = np.arange(n_days)
    doy = day % 365
    # Winter cloudier than summer; flip the phase in the southern hemisphere.
    phase = 0.0 if lat_deg >= 0 else math.pi
    seasonal = -cfg.kt_seasonal_amp * np.cos(2 * math.pi * (doy - 172) / 365.0 + phase)
    kt_mean = kt_mean_annual + seasonal

    # AR(1) latent process, zero mean, unit-ish variance
    z = np.zeros(n_days)
    innov_sd = cfg.kt_sigma * math.sqrt(1.0 - cfg.kt_persistence ** 2)
    z[0] = rng.normal(0.0, cfg.kt_sigma)
    for i in range(1, n_days):
        z[i] = cfg.kt_persistence * z[i - 1] + rng.normal(0.0, innov_sd)

    return np.clip(kt_mean + z, cfg.kt_min, cfg.kt_max)


# ==============================================================================
# 3. TEMPERATURE -- drives panel efficiency AND battery capacity
# ==============================================================================
def sea_temperature_c(lat_deg: float, day_of_year: np.ndarray, cfg: Config) -> np.ndarray:
    """
    Simple ocean-surface climatology. Quadratic falloff with latitude, small
    seasonal swing (water has enormous thermal inertia), floored at the freezing
    point of seawater. Peak lags the solstice by ~2 months.
    """
    lat_abs = abs(lat_deg)
    t_mean = cfg.sst_c0 - cfg.sst_c2 * lat_abs ** 2
    amp = cfg.sst_amp_base + cfg.sst_amp_per_deg * lat_abs
    peak_doy = 240.0 if lat_deg >= 0 else 240.0 - 182.5   # late summer, hemisphere-aware
    seasonal = amp * np.cos(2 * math.pi * (day_of_year - peak_doy) / 365.0)
    return np.maximum(t_mean + seasonal, cfg.sst_freeze_c)


def air_temperature_c(lat_deg: float, day_of_year: np.ndarray, cfg: Config) -> np.ndarray:
    """Air over open ocean tracks the sea closely but swings a little wider."""
    lat_abs = abs(lat_deg)
    t_mean = cfg.sst_c0 - cfg.sst_c2 * lat_abs ** 2
    amp = (cfg.sst_amp_base + cfg.sst_amp_per_deg * lat_abs) * cfg.air_temp_amp_mult
    peak_doy = 240.0 if lat_deg >= 0 else 240.0 - 182.5
    seasonal = amp * np.cos(2 * math.pi * (day_of_year - peak_doy) / 365.0)
    return np.maximum(t_mean + seasonal + cfg.air_temp_offset_k, cfg.sst_freeze_c - 8.0)


# ==============================================================================
# 4. PANEL POWER
# ==============================================================================
def panel_power_w(ghi: np.ndarray, t_air: np.ndarray, cfg: Config) -> tuple:
    """
    Scale the 50 W STC rating by actual irradiance, then correct for cell
    temperature, sail shading and soiling.

    A counter-intuitive but real result: at high latitude the panel runs COLD
    (sea ~2-8 C, wind- and spray-cooled), and silicon is MORE efficient when
    cold. The temperature term is a small gain of a few percent, not a loss.
    It is nowhere near enough to offset the geometry, but it is worth knowing
    that the panel itself is not the problem -- the sun angle is.
    """
    t_cell = t_air + cfg.panel_temp_rise_k_per_wm2 * ghi
    eta_temp = 1.0 + cfg.panel_temp_coeff_per_k * (t_cell - 25.0)

    p = (cfg.p_solar_peak_w
         * (ghi / cfg.irradiance_stc)
         * eta_temp
         * cfg.sail_shading_factor
         * cfg.soiling_factor)

    # Converter cannot pass more than the array rating (small headroom for cold-boost)
    p = np.clip(p, 0.0, cfg.p_solar_peak_w * 1.10)
    return p, t_cell, eta_temp


# ==============================================================================
# 4b. ALTERNATIVE GENERATION -- wind, wave, water
# ==============================================================================
def wind_speed_series(n_days: int, lat_deg: float, cfg: Config,
                      rng: np.random.Generator) -> np.ndarray:
    """
    Daily-mean 10 m wind speed, then corrected to hub height.

    Two things matter for this problem. First, the mean rises with latitude, so
    the windiest places are the darkest. Second, the seasonal peak is in WINTER,
    exactly in antiphase with the solar resource -- which is the whole physical
    argument for wind as a complement to solar on this vehicle.
    """
    lat_abs = abs(lat_deg)
    u_mean = cfg.wind_mean_base + cfg.wind_mean_per_deg * lat_abs

    doy = np.arange(n_days) % 365
    phase = 0.0 if lat_deg >= 0 else math.pi
    # Peak in midwinter: cos peaks at the solstice (day 355 N / 172 S).
    seasonal = 1.0 + cfg.wind_seasonal_frac * np.cos(
        2 * math.pi * (doy - 355) / 365.0 + phase)
    daily_mean = u_mean * seasonal

    # AR(1) multiplicative anomaly -- gales and calms both persist for days.
    z = np.zeros(n_days)
    sd = 0.28
    innov = sd * math.sqrt(1.0 - cfg.wind_persistence ** 2)
    z[0] = rng.normal(0.0, sd)
    for i in range(1, n_days):
        z[i] = cfg.wind_persistence * z[i - 1] + rng.normal(0.0, innov)

    u10 = np.clip(daily_mean * np.exp(z), 0.0, 40.0)
    # Power-law shear down to hub height: a masthead rotor on a 1 m boat sees
    # noticeably less wind than the 10 m reference.
    shear = (cfg.wind_hub_height_m / 10.0) ** cfg.wind_shear_alpha
    return u10 * shear


def wind_turbine_power(u: np.ndarray, cfg: Config) -> np.ndarray:
    """
    P = 1/2 rho A Cp eta v^3, with cut-in, rated clip and furling.

    Applied to an hourly wind series, so the cubic is evaluated on the actual
    distribution rather than on the mean -- which matters enormously, because
    E[v^3] is roughly twice E[v]^3 for a realistic wind distribution.
    """
    rho = 1.225
    area = math.pi * (cfg.wt_diameter_m / 2.0) ** 2
    p = 0.5 * rho * area * cfg.wt_cp * cfg.wt_efficiency * np.power(u, 3)
    p = np.where(u < cfg.wt_cut_in_ms, 0.0, p)      # too slow to start
    p = np.where(u > cfg.wt_furl_ms, 0.0, p)        # furled for survival
    return np.clip(p, 0.0, cfg.wt_rated_w)


def wave_height_series(u10: np.ndarray, lat_deg: float, cfg: Config) -> tuple:
    """
    Significant wave height and energy period, tied to the wind that raises it.

    Hs is anchored to a latitude/season climatology and modulated by the local
    wind anomaly, so a gale brings both wind AND waves -- the two harvesters are
    strongly correlated, which matters when judging whether fitting both adds
    genuine redundancy.
    """
    lat_abs = abs(lat_deg)
    hs_mean = cfg.wave_hs_base + cfg.wave_hs_per_deg * lat_abs
    ref = cfg.wind_mean_base + cfg.wind_mean_per_deg * lat_abs
    # Hs grows roughly with wind speed squared in a developing sea; damped here
    # because fetch and duration limit the response.
    hs = hs_mean * np.power(np.maximum(u10, 0.1) / ref, 1.2)
    hs = np.clip(hs, 0.2, 14.0)
    # Energy period from Hs -- standard engineering approximation for open ocean.
    te = 4.0 * np.sqrt(hs)
    return hs, te


def wave_harvester_power(hs: np.ndarray, te: np.ndarray, cfg: Config) -> np.ndarray:
    """
    Inertial (proof-mass) harvester inside a wave-following hull.

    The hull heaves with the surface, so there is no hull-to-water relative
    motion to exploit. What is left is the hull's own vertical acceleration
    acting on an internal mass:

        omega = 2*pi/Te
        a     = omega^2 * Hs/2          vertical acceleration amplitude
        E     ~ 2 * m * a * s           energy per half-cycle, mass x force x stroke
        P     = E * (2/Te) * eta * k

    The brutal term is the stroke `s`. Inside a 1 m hull there is very little of
    it, and power is directly proportional -- which is why this concept struggles
    at this scale rather than for any subtler reason.
    """
    omega = 2.0 * math.pi / np.maximum(te, 1e-3)
    accel = np.power(omega, 2) * (hs / 2.0)
    energy_per_half_cycle = 2.0 * cfg.wave_proof_mass_kg * accel * cfg.wave_stroke_m
    p = (energy_per_half_cycle * (2.0 / np.maximum(te, 1e-3))
         * cfg.wave_pto_efficiency * cfg.wave_capture_factor)
    return np.clip(p, 0.0, 50.0)


def boat_speed(u10: np.ndarray, cfg: Config) -> np.ndarray:
    """Speed through the water, capped at displacement hull speed."""
    return np.minimum(cfg.boat_speed_per_wind * u10, cfg.boat_hull_speed_ms)


def water_turbine_power(v_boat: np.ndarray, cfg: Config) -> np.ndarray:
    """
    P = 1/2 rho A Cp eta v^3 in WATER -- rho 1025 rather than 1.225.

    That density ratio is the entire attraction: a 0.1 m disc at 1 m/s beats a
    0.2 m rotor in 5 m/s of wind. The cost is drag. Extracting P watts at speed v
    demands at least P/v newtons of thrust deficit, which slows the vehicle --
    modelled in the report rather than here, because it couples back into
    passage planning rather than into the energy balance.
    """
    rho = 1025.0
    area = math.pi * (cfg.turb_diameter_m / 2.0) ** 2
    p = 0.5 * rho * area * cfg.turb_cp * cfg.turb_efficiency * np.power(v_boat, 3)
    return np.where(v_boat < cfg.turb_cut_in_ms, 0.0, p)


# ==============================================================================
# 5. BATTERY -- temperature derate, charge inhibit, fade
# ==============================================================================
def battery_temp_factor(t_c: float | np.ndarray, cfg: Config) -> float | np.ndarray:
    """Usable-capacity multiplier vs temperature, linearly interpolated."""
    return np.interp(t_c, cfg.batt_temp_points_c, cfg.batt_temp_factor)


def calendar_fade_rate(t_c: float, cfg: Config) -> float:
    """
    Arrhenius-scaled sqrt(time) calendar fade coefficient, per sqrt(day).

    Reference: 2 %/year at 25 C  ->  k_ref = 0.02 / sqrt(365).
    Scaling to lower temperature slows ageing sharply: at 5 C the rate is roughly
    23 % of the 25 C rate. Cold ocean water is, ironically, excellent for
    battery calendar life -- it is only bad for usable capacity and charging.
    """
    k_ref = cfg.cal_fade_per_year_at_25c / math.sqrt(365.0)
    t_k = t_c + 273.15
    t_ref_k = 298.15
    scale = math.exp(-cfg.cal_fade_activation_j_per_mol / R_GAS * (1.0 / t_k - 1.0 / t_ref_k))
    return k_ref * scale


# ==============================================================================
# 6. SIMULATION
# ==============================================================================
def simulate(cfg: Config) -> dict:
    """
    Hourly energy balance. Returns time series and summary statistics.

    The battery integration has to be a sequential loop (SoC depends on the
    previous step), but everything upstream of it is vectorised.
    """
    rng = np.random.default_rng(cfg.rng_seed)

    n_days = int(round(365 * cfg.years))
    steps_per_day = int(round(24 / cfg.timestep_h))
    n = n_days * steps_per_day

    t_hours = np.arange(n) * cfg.timestep_h
    day_index = (t_hours // 24).astype(int)
    # start_day lets us test worst-case deployment timing -- launching in autumn
    # into a darkening winter is far harsher than launching in spring.
    doy = ((day_index + cfg.start_day - 1) % 365) + 1
    hour_solar = t_hours % 24

    # --- Solar resource -------------------------------------------------------
    decl = solar_declination_deg(doy)
    elev = solar_elevation_deg(cfg.latitude_deg, decl, hour_solar)
    ghi_clear = clear_sky_ghi(elev, cfg)

    kt_daily = clearness_series(n_days, cfg.latitude_deg, cfg, rng)
    kt = np.repeat(kt_daily, steps_per_day)
    ghi = ghi_clear * kt

    # --- Temperatures ---------------------------------------------------------
    t_air = air_temperature_c(cfg.latitude_deg, doy, cfg)
    t_sea = sea_temperature_c(cfg.latitude_deg, doy, cfg)   # ~ battery temperature

    # --- Generation -----------------------------------------------------------
    p_panel, t_cell, eta_temp = panel_power_w(ghi, t_air, cfg)
    p_in = p_panel * cfg.mppt_efficiency

    # --- Alternative generation ----------------------------------------------
    # Wind drives all three: the turbine directly, the waves it raises, and the
    # boat speed that spins the water turbine. One shared series keeps them
    # correctly correlated -- a calm kills all three at once.
    u_hub = np.repeat(wind_speed_series(n_days, cfg.latitude_deg, cfg, rng),
                      steps_per_day)
    u10 = u_hub / ((cfg.wind_hub_height_m / 10.0) ** cfg.wind_shear_alpha)

    p_wind = wind_turbine_power(u_hub, cfg) if cfg.enable_wind_turbine \
        else np.zeros(n)

    hs, te = wave_height_series(u10, cfg.latitude_deg, cfg)
    p_wave = wave_harvester_power(hs, te, cfg) if cfg.enable_wave_harvester \
        else np.zeros(n)

    v_boat = boat_speed(u10, cfg)
    p_turb = water_turbine_power(v_boat, cfg) if cfg.enable_water_turbine \
        else np.zeros(n)

    p_alt = (p_wind + p_wave + p_turb) * cfg.mppt_efficiency
    p_in = p_in + p_alt

    # --- Battery integration --------------------------------------------------
    soc_wh = np.zeros(n)
    capacity_wh = np.zeros(n)
    unmet_w = np.zeros(n)
    dumped_wh = np.zeros(n)
    charge_blocked = np.zeros(n, dtype=bool)

    fade_cal = 0.0                 # fractional capacity lost to calendar ageing
    fade_cyc = 0.0                 # fractional capacity lost to cycling
    discharge_wh_total = 0.0
    cal_accum = 0.0                # accumulated Arrhenius-weighted "effective days"

    dt = cfg.timestep_h
    soc = cfg.batt_nominal_wh * cfg.initial_soc_frac

    for i in range(n):
        # Capacity available right now = nominal x fade x temperature derate
        f_temp = float(battery_temp_factor(t_sea[i], cfg))
        fade_total = min(fade_cal + fade_cyc, 0.95)
        cap = cfg.batt_nominal_wh * (1.0 - fade_total) * f_temp
        capacity_wh[i] = cap

        # extra_power_w is the constant contribution a wind turbine or wave
        # harvester would make. Zero for the baseline solar-only case.
        net_w = p_in[i] + cfg.extra_power_w - cfg.load_w

        # HARD CONSTRAINT: no charging below 0 C (lithium plating). The BMS
        # blocks it, so any surplus in cold water is simply thrown away.
        if net_w > 0 and t_sea[i] < cfg.batt_charge_min_c:
            charge_blocked[i] = True
            dumped_wh[i] += net_w * dt
            net_w = 0.0

        soc_new = soc + net_w * dt

        if soc_new > cap:                      # full: MPPT backs off, surplus dumped
            dumped_wh[i] += soc_new - cap
            soc_new = cap
        elif soc_new < 0.0:                    # flat: load cannot be met -> blackout
            unmet_w[i] = -soc_new / dt
            soc_new = 0.0

        if soc_new < soc:
            discharge_wh_total += (soc - soc_new)

        soc = soc_new
        soc_wh[i] = soc

        # --- ageing -----------------------------------------------------------
        # Calendar: dQ/dt of a sqrt law, evaluated on Arrhenius-effective time.
        k_cal = calendar_fade_rate(float(t_sea[i]), cfg)
        cal_accum += dt / 24.0
        fade_cal = k_cal * math.sqrt(cal_accum)
        # Cycle: linear in equivalent full cycles of throughput.
        efc = discharge_wh_total / cfg.batt_nominal_wh
        fade_cyc = (efc / cfg.cycle_life_efc) * cfg.cycle_life_end_fade

    # --- Daily aggregation ----------------------------------------------------
    def daily(arr, how="sum"):
        r = arr.reshape(n_days, steps_per_day)
        return r.sum(axis=1) * dt if how == "sum" else r.mean(axis=1)

    gen_wh_day = daily(p_in)
    load_wh_day = np.full(n_days, cfg.load_w * 24.0)
    soc_end_day = soc_wh.reshape(n_days, steps_per_day)[:, -1]
    soc_min_day = soc_wh.reshape(n_days, steps_per_day).min(axis=1)
    unmet_wh_day = daily(unmet_w)
    elev_max_day = elev.reshape(n_days, steps_per_day).max(axis=1)

    # --- Summary --------------------------------------------------------------
    blackout_hours = int((unmet_w > 0).sum())
    blackout_days = int((unmet_wh_day > 0).sum())
    annual_gen_wh = gen_wh_day[:365].sum() if n_days >= 365 else gen_wh_day.sum()
    annual_load_wh = cfg.load_w * 24 * min(n_days, 365)

    # Deficit: the average extra power needed to survive the worst continuous
    # stretch. This is the number an alternative harvester has to beat.
    worst_window = _worst_deficit_window(gen_wh_day, cfg.load_w * 24.0, cfg.batt_nominal_wh)

    return dict(
        cfg=cfg, n_days=n_days, steps_per_day=steps_per_day,
        t_hours=t_hours, doy=doy, elev=elev, ghi=ghi, ghi_clear=ghi_clear, kt=kt,
        p_in=p_in, t_air=t_air, t_sea=t_sea, t_cell=t_cell, eta_temp=eta_temp,
        p_wind=p_wind, p_wave=p_wave, p_turb=p_turb, p_alt=p_alt,
        u10=u10, hs=hs, v_boat=v_boat,
        soc_wh=soc_wh, capacity_wh=capacity_wh, unmet_w=unmet_w,
        charge_blocked=charge_blocked, dumped_wh=dumped_wh,
        gen_wh_day=gen_wh_day, load_wh_day=load_wh_day,
        soc_end_day=soc_end_day, soc_min_day=soc_min_day,
        unmet_wh_day=unmet_wh_day, elev_max_day=elev_max_day,
        fade_cal=fade_cal, fade_cyc=fade_cyc,
        discharge_wh_total=discharge_wh_total,
        blackout_hours=blackout_hours, blackout_days=blackout_days,
        annual_gen_wh=annual_gen_wh, annual_load_wh=annual_load_wh,
        worst_window=worst_window,
    )


def _worst_deficit_window(gen_wh_day: np.ndarray, load_wh_day: float, batt_wh: float) -> dict:
    """
    Find the continuous stretch of days with the largest cumulative energy
    shortfall. That cumulative shortfall, minus whatever the battery can supply,
    is the energy an alternative source must provide; divided by the window
    length it gives the average watts needed.
    """
    deficit = load_wh_day - gen_wh_day          # positive = short
    best_sum, best_start, best_end = 0.0, 0, 0
    cur_sum, cur_start = 0.0, 0
    for i, d in enumerate(deficit):
        if cur_sum <= 0:
            cur_sum, cur_start = d, i
        else:
            cur_sum += d
        if cur_sum > best_sum:
            best_sum, best_start, best_end = cur_sum, cur_start, i
    length = max(best_end - best_start + 1, 1)
    shortfall_after_batt = max(best_sum - batt_wh, 0.0)
    return dict(
        start_day=best_start + 1, end_day=best_end + 1, length_days=length,
        deficit_wh=best_sum,
        shortfall_after_battery_wh=shortfall_after_batt,
        avg_extra_w=shortfall_after_batt / (length * 24.0) if length else 0.0,
        avg_gross_deficit_w=best_sum / (length * 24.0) if length else 0.0,
    )


def _sim_fails(cfg: Config) -> bool:
    """True if the vehicle blacks out at any point during the run."""
    return simulate(cfg)["blackout_days"] > 0


def solve_required_extra_power(cfg: Config, hi: float = 6.0, tol: float = 0.01) -> float:
    """
    THE HEADLINE NUMBER.

    Bisect on a constant additional generation term to find the minimum extra
    average power that eliminates every blackout. This is the requirement any
    alternative harvester -- wind or wave -- has to meet.

    Note this is deliberately a CONSTANT power. A real turbine or wave harvester
    delivers intermittent power correlated with weather, so meeting this figure
    on an annual-average basis is NOT sufficient; it has to deliver during the
    dark months specifically. Treat the answer as a lower bound on the
    requirement, and see the note for the seasonality argument.
    """
    if not _sim_fails(Config(**{**asdict(cfg), "extra_power_w": 0.0})):
        return 0.0

    lo = 0.0
    # Expand the bracket if even `hi` is not enough (very high latitude).
    while _sim_fails(Config(**{**asdict(cfg), "extra_power_w": hi})):
        lo = hi
        hi *= 2.0
        if hi > 200.0:
            return float("inf")

    while hi - lo > tol:
        mid = 0.5 * (lo + hi)
        if _sim_fails(Config(**{**asdict(cfg), "extra_power_w": mid})):
            lo = mid
        else:
            hi = mid
    return hi


def solve_required_battery_wh(cfg: Config, hi: float = 200_000.0, tol: float = 10.0) -> float:
    """
    The alternative to generating more: store more.

    Bisect on nominal battery capacity to find the smallest battery that rides
    out the dark season on solar alone. Comparing this against the extra-power
    figure tells you whether the cheaper engineering answer is "add a harvester"
    or "add cells" -- a fair comparison the brief does not explicitly ask for,
    but which any recommendation has to survive.
    """
    base = Config(**{**asdict(cfg), "extra_power_w": 0.0})
    if not _sim_fails(base):
        return cfg.batt_nominal_wh

    lo = cfg.batt_nominal_wh
    if _sim_fails(Config(**{**asdict(base), "batt_nominal_wh": hi})):
        return float("inf")

    while hi - lo > tol:
        mid = 0.5 * (lo + hi)
        if _sim_fails(Config(**{**asdict(base), "batt_nominal_wh": mid})):
            lo = mid
        else:
            hi = mid
    return hi


# ==============================================================================
# 7. REPORTING
# ==============================================================================
MONTH_STARTS = [1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335, 366]
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def monthly_table(res: dict) -> list:
    """Monthly means -- the table view that accompanies the charts."""
    cfg = res["cfg"]
    rows = []
    for m in range(12):
        a, b = MONTH_STARTS[m] - 1, MONTH_STARTS[m + 1] - 1
        b = min(b, res["n_days"])
        if a >= b:
            continue
        gen = res["gen_wh_day"][a:b]
        soc = res["soc_min_day"][a:b]
        rows.append(dict(
            month=MONTH_NAMES[m],
            gen_wh_day=float(gen.mean()),
            load_wh_day=cfg.load_w * 24.0,
            margin_wh_day=float(gen.mean() - cfg.load_w * 24.0),
            min_soc_pct=float(soc.min() / cfg.batt_nominal_wh * 100.0),
            peak_sun_elev=float(res["elev_max_day"][a:b].mean()),
            blackout_days=int((res["unmet_wh_day"][a:b] > 0).sum()),
        ))
    return rows


def print_report(res: dict) -> None:
    cfg = res["cfg"]
    lat = cfg.latitude_deg
    hemi = "N" if lat >= 0 else "S"

    print("=" * 78)
    print(f"  C-STAR BASELINE POWER BUDGET  --  {abs(lat):.0f} deg {hemi}, "
          f"{cfg.years:g} year(s)")
    print("=" * 78)

    print("\n-- ANNUAL ENERGY BALANCE " + "-" * 52)
    gen, load = res["annual_gen_wh"], res["annual_load_wh"]
    print(f"  Solar generated (yr 1)     : {gen:9.0f} Wh   ({gen/8760:.2f} W avg)")
    print(f"  Load consumed   (yr 1)     : {load:9.0f} Wh   ({cfg.load_w:.2f} W avg)")
    print(f"  Net                        : {gen-load:+9.0f} Wh   "
          f"({(gen-load)/8760:+.2f} W avg)")
    print(f"  Ratio generation/load      : {gen/load:9.2f} x")
    print(f"  Energy dumped (batt full)  : {res['dumped_wh'].sum():9.0f} Wh   "
          f"<- surplus that cannot be stored")

    print("\n-- MISSION OUTCOME " + "-" * 58)
    print(f"  Blackout days (SoC = 0)    : {res['blackout_days']:4d} of {res['n_days']}")
    print(f"  Blackout hours             : {res['blackout_hours']:4d}")
    min_soc = res["soc_wh"].min() / cfg.batt_nominal_wh * 100
    print(f"  Minimum state of charge    : {min_soc:6.1f} %")
    ch_blocked = int(res["charge_blocked"].sum())
    print(f"  Hours charging BLOCKED     : {ch_blocked:4d}   "
          f"<- battery below {cfg.batt_charge_min_c:.0f} C, lithium-plating limit")
    verdict = "SURVIVES" if res["blackout_days"] == 0 else "FAILS"
    print(f"  VERDICT                    : {verdict}")

    print("\n-- WORST DEFICIT WINDOW " + "-" * 53)
    w = res["worst_window"]
    print(f"  Longest continuous deficit : days {w['start_day']}-{w['end_day']}  "
          f"({w['length_days']} days)")
    print(f"  Cumulative energy deficit  : {w['deficit_wh']:9.0f} Wh  "
          f"vs {cfg.batt_nominal_wh:.0f} Wh battery")
    print(f"  Mean deficit over window   : {w['avg_gross_deficit_w']:9.2f} W")

    print("\n-- SIZING THE GAP  (what would actually fix it) " + "-" * 29)
    req_w = solve_required_extra_power(cfg)
    req_wh = solve_required_battery_wh(cfg)
    if req_w == 0.0:
        print("  Extra power required       :      none -- solar alone is sufficient")
    elif math.isinf(req_w):
        print("  Extra power required       :  >200 W -- no plausible harvester closes this")
    else:
        print(f"  Extra CONSTANT power req'd : {req_w:9.2f} W  "
              f"<== the target any harvester must hit")
        print(f"     as a fraction of load    : {req_w/cfg.load_w:9.2f} x the 1 W load")
    if math.isinf(req_wh):
        print("  Battery-only alternative   :  infeasible at any capacity")
    elif req_wh <= cfg.batt_nominal_wh * 1.001:
        print("  Battery-only alternative   :   current battery already sufficient")
    else:
        extra_kg = (req_wh - cfg.batt_nominal_wh) / BATT_WH_PER_KG
        print(f"  Battery-only alternative   : {req_wh:9.0f} Wh "
              f"({req_wh/cfg.batt_nominal_wh:.1f}x current)")
        print(f"     extra cells needed       : {req_wh-cfg.batt_nominal_wh:9.0f} Wh "
              f"~ {extra_kg:.1f} kg  ({extra_kg/40*100:.0f} % of the 40 kg vehicle)")

    print("\n-- BATTERY AGEING " + "-" * 59)
    print(f"  Calendar fade over run     : {res['fade_cal']*100:6.2f} %")
    print(f"  Cycle fade over run        : {res['fade_cyc']*100:6.2f} %")
    efc = res["discharge_wh_total"] / cfg.batt_nominal_wh
    print(f"  Equivalent full cycles     : {efc:6.1f}  "
          f"({efc/cfg.years:.1f} EFC/yr vs {cfg.cycle_life_efc:.0f} rated)")
    yrs_to_cycle_eol = cfg.cycle_life_efc / max(efc / cfg.years, 1e-9)
    print(f"  Years to cycle end-of-life : {yrs_to_cycle_eol:6.0f}  "
          f"<- cycle ageing is NEGLIGIBLE; calendar ageing dominates")
    mean_t = res["t_sea"].mean()
    print(f"  Mean battery temperature   : {mean_t:6.1f} C  "
          f"(cold water slows calendar fade ~{calendar_fade_rate(mean_t,cfg)/calendar_fade_rate(25.0,cfg)*100:.0f} % of 25 C rate)")

    print("\n-- TEMPERATURE EFFECTS " + "-" * 54)
    day_mask = res["ghi"] > 20
    if day_mask.any():
        print(f"  Mean cell temp (daylight)  : {res['t_cell'][day_mask].mean():6.1f} C")
        print(f"  Panel temp efficiency gain : {(res['eta_temp'][day_mask].mean()-1)*100:+6.2f} %  "
              f"<- cold panels are MORE efficient")
    print(f"  Mean battery capacity derate: "
          f"{(1-res['capacity_wh'].mean()/cfg.batt_nominal_wh)*100:6.2f} %")

    print("\n-- MONTHLY BREAKDOWN " + "-" * 56)
    print(f"  {'Mon':>4} {'Gen Wh/d':>9} {'Load Wh/d':>10} {'Margin':>8} "
          f"{'MinSoC%':>8} {'SunElev':>8} {'Blackout':>9}")
    for r in monthly_table(res):
        print(f"  {r['month']:>4} {r['gen_wh_day']:9.1f} {r['load_wh_day']:10.1f} "
              f"{r['margin_wh_day']:+8.1f} {r['min_soc_pct']:8.1f} "
              f"{r['peak_sun_elev']:7.1f}d {r['blackout_days']:9d}")
    print()


def print_assumptions(cfg: Config) -> None:
    print("=" * 78)
    print("  ASSUMPTIONS  (everything not given in the brief)")
    print("=" * 78)
    for line in __doc__.splitlines():
        pass
    src = Path(__file__).read_text().splitlines()
    inside = False
    for ln in src:
        s = ln.strip()
        if s.startswith("# A") and ":" in s:
            inside = True
            print("  " + s[2:])
        elif inside and s.startswith("#"):
            print("      " + s[1:].strip())
        elif inside and not s.startswith("#"):
            inside = False
    print()


# ==============================================================================
# 8. PLOTS
# ==============================================================================
def plot_main(res: dict, out: Path) -> None:
    """
    Three-panel view of one latitude: what the battery does, why, and when.
    """
    _style()
    cfg = res["cfg"]
    lat = cfg.latitude_deg
    hemi = "N" if lat >= 0 else "S"
    days = np.arange(res["n_days"]) + 1

    # Panels (a) and (b) are both indexed by day-of-mission and share an x axis.
    # Panel (c) is indexed by month, so it must NOT share -- otherwise the 12
    # bars get squashed into the left edge of a 365-day axis.
    fig = plt.figure(figsize=(9.5, 10.4))
    gs = fig.add_gridspec(3, 1, hspace=0.42)
    ax0 = fig.add_subplot(gs[0])
    ax1 = fig.add_subplot(gs[1], sharex=ax0)
    ax2 = fig.add_subplot(gs[2])
    axes = [ax0, ax1, ax2]

    # ---- (a) State of charge -- THE headline chart ----------------------------
    ax = axes[0]
    soc_pct = res["soc_wh"] / cfg.batt_nominal_wh * 100.0
    t_days = res["t_hours"] / 24.0 + 1

    # Shade blackout stretches in status-critical red, labelled -- never colour alone
    blackout = res["unmet_wh_day"] > 0
    if blackout.any():
        _shade_runs(ax, days, blackout, RED, 0.13)

    ax.plot(t_days, soc_pct, color=AQUA, lw=1.3)
    ax.axhline(0, color=RED, lw=1.0, ls="--", alpha=0.7)
    ax.set_ylabel("State of charge  (%)")
    ax.set_ylim(-4, 108)
    ax.set_title(f"C-Star battery state of charge at {abs(lat):.0f}°{hemi} "
                 f"— 50 W array, 1 W load, 1300 Wh battery")

    n_black = int(blackout.sum())
    if n_black:
        ax.text(0.015, 0.10, f"{n_black} blackout days\n(load unmet, mission lost)",
                transform=ax.transAxes, color=RED, fontsize=8.5, va="bottom", weight="bold")
    else:
        ax.text(0.015, 0.06, f"No blackout — min SoC {soc_pct.min():.0f}%",
                transform=ax.transAxes, color=INK2, fontsize=8.5, va="bottom", weight="bold")

    # ---- (b) Daily energy in vs out ------------------------------------------
    ax = axes[1]
    ax.plot(days, res["gen_wh_day"], color=BLUE, lw=1.2, label="Solar generated")
    ax.plot(days, res["load_wh_day"], color=ORANGE, lw=1.6, label="Load consumed")
    ax.fill_between(days, res["gen_wh_day"], res["load_wh_day"],
                    where=res["gen_wh_day"] < res["load_wh_day"],
                    color=ORANGE, alpha=0.12, linewidth=0)
    ax.set_ylabel("Daily energy  (Wh/day)")
    ax.set_yscale("symlog", linthresh=10)
    ax.legend(loc="upper right", ncol=2)
    ax.set_title("Daily energy balance  —  shaded where solar cannot cover the load")

    # Direct label rather than relying on the legend alone
    ax.annotate("24 Wh/day load", xy=(days[-1] * 0.55, 24), xytext=(0, 7),
                textcoords="offset points", color=ORANGE, fontsize=8, weight="bold")

    # ---- (c) Monthly generation vs load --------------------------------------
    ax = axes[2]
    rows = monthly_table(res)
    xs = np.arange(len(rows))
    gen = [r["gen_wh_day"] for r in rows]
    cols = [BLUE if g >= cfg.load_w * 24 else RED for g in gen]
    ax.bar(xs, gen, color=cols, width=0.62, zorder=3)
    ax.axhline(cfg.load_w * 24.0, color=ORANGE, lw=1.6, zorder=4)
    ax.annotate("load 24 Wh/day", xy=(len(rows) - 0.4, cfg.load_w * 24), xytext=(0, 6),
                textcoords="offset points", color=ORANGE, fontsize=8,
                ha="right", weight="bold")
    for x, g in zip(xs, gen):
        ax.text(x, g, f"{g:.0f}", ha="center", va="bottom", fontsize=7.5, color=INK2)
    ax.set_xticks(xs)
    ax.set_xticklabels([r["month"] for r in rows])
    ax.set_ylabel("Mean daily generation  (Wh/day)")
    ax.set_xlabel("Month")
    ax.set_title("Monthly mean solar generation  —  red bars fall below the load")

    for a in axes[:2]:
        a.set_xlim(1, res["n_days"])
    axes[0].set_xlabel("")
    axes[0].tick_params(labelbottom=False)
    axes[1].set_xlabel("Day of mission")

    fig.savefig(out)
    plt.close(fig)
    print(f"  wrote {out}")


def _shade_runs(ax, x, mask, color, alpha):
    """Shade contiguous True runs of `mask`."""
    in_run, start = False, 0
    for i, v in enumerate(mask):
        if v and not in_run:
            in_run, start = True, i
        elif not v and in_run:
            ax.axvspan(x[start], x[i], color=color, alpha=alpha, lw=0, zorder=1)
            in_run = False
    if in_run:
        ax.axvspan(x[start], x[-1], color=color, alpha=alpha, lw=0, zorder=1)


def plot_sweep(cfg: Config, out: Path, lat_min=0, lat_max=75, step=5) -> list:
    """
    Latitude x season envelope: where and when does the baseline vehicle work?

    Panel (a) is a sequential single-hue heatmap of mean daily generation, with
    the break-even contour drawn on it. Panel (b) reduces the whole thing to the
    one number a decision-maker needs: blackout days per year vs latitude.
    """
    _style()
    lats = list(range(lat_min, lat_max + 1, step))
    grid = np.zeros((len(lats), 12))
    blackout_days, min_soc, extra_w, req_batt = [], [], [], []

    print("\n  Running latitude sweep (bisecting for required power at each) ...")
    for i, lat in enumerate(lats):
        c = Config(**{**asdict(cfg), "latitude_deg": float(lat), "years": 1.0,
                      "extra_power_w": 0.0})
        r = simulate(c)
        for m in range(12):
            a, b = MONTH_STARTS[m] - 1, MONTH_STARTS[m + 1] - 1
            grid[i, m] = r["gen_wh_day"][a:b].mean()
        blackout_days.append(r["blackout_days"])
        min_soc.append(r["soc_wh"].min() / c.batt_nominal_wh * 100)
        # Use the bisection solver, not the naive window estimate, so the
        # requirement curve is consistent with the single-latitude report.
        extra_w.append(solve_required_extra_power(c))
        req_batt.append(solve_required_battery_wh(c))
        print(f"    {lat:3d}°  blackout {r['blackout_days']:3d} d/yr   "
              f"min SoC {min_soc[-1]:5.1f} %   needs {extra_w[-1]:5.2f} W extra"
              f"   or {req_batt[-1]:6.0f} Wh battery")

    fig, axes = plt.subplots(3, 1, figsize=(9.5, 11.4),
                             gridspec_kw={"height_ratios": [1.35, 1, 1]})

    # ---- (a) heatmap ---------------------------------------------------------
    ax = axes[0]
    im = ax.imshow(grid, aspect="auto", origin="lower", cmap=BLUE_RAMP,
                   extent=[-0.5, 11.5, lats[0] - step / 2, lats[-1] + step / 2],
                   norm=matplotlib.colors.LogNorm(vmin=max(grid.min(), 0.5),
                                                  vmax=grid.max()))
    cs = ax.contour(np.arange(12), lats, grid, levels=[cfg.load_w * 24.0],
                    colors=[ORANGE], linewidths=1.8)
    ax.clabel(cs, fmt={cfg.load_w * 24.0: " break-even 24 Wh/day "}, fontsize=8)
    ax.set_xticks(np.arange(12))
    ax.set_xticklabels(MONTH_NAMES)
    ax.set_yticks(lats)
    ax.set_ylabel("Latitude  (°N)")
    ax.set_title("Mean daily solar generation  —  above the orange break-even "
                 "line the vehicle is in energy deficit")
    ax.grid(False)
    cb = fig.colorbar(im, ax=ax, pad=0.015)
    cb.set_label("Wh/day  (log scale)", color=INK2, fontsize=8.5)
    cb.ax.tick_params(labelsize=8, color=INK2)
    cb.outline.set_edgecolor(GRID)

    # ---- (b) blackout days vs latitude --------------------------------------
    ax = axes[1]
    cols = [BLUE if b == 0 else RED for b in blackout_days]
    ax.bar(lats, blackout_days, width=step * 0.62, color=cols, zorder=3)
    for la, b in zip(lats, blackout_days):
        if b > 0:
            ax.text(la, b, f"{b}", ha="center", va="bottom", fontsize=7.5, color=INK2)
    ax.set_xlabel("Latitude  (°N)")
    ax.set_ylabel("Blackout days per year")
    # Zoom to the interesting band: below ~45° nothing ever happens.
    zoom = [la for la in lats if la >= min(45, max(lats) - 30)]
    ax.set_xticks(zoom)
    ax.set_xlim(zoom[0] - step, zoom[-1] + step * 0.8)
    ax.set_title("Days per year the battery reaches zero  —  "
                 "blue = survives, red = mission lost")

    first_fail = next((la for la, b in zip(lats, blackout_days) if b > 0), None)
    if first_fail is not None:
        ax.axvline(first_fail - step / 2, color=MUTED, ls="--", lw=1.0, zorder=2)
        ax.text(first_fail - step / 2 + 0.4, ax.get_ylim()[1] * 0.85,
                f"solar-only limit\n≈ {first_fail - step/2:.0f}°",
                color=INK2, fontsize=8.5, weight="bold")

    # ---- (c) required extra power -- THE requirement curve -------------------
    ax = axes[2]
    finite = [e if math.isfinite(e) else np.nan for e in extra_w]
    ax.bar(lats, finite, width=step * 0.62, color=BLUE, zorder=3)
    for la, e in zip(lats, finite):
        if e and e > 0:
            ax.text(la, e, f"{e:.2f}", ha="center", va="bottom",
                    fontsize=7.5, color=INK2)
    ax.axhline(cfg.load_w, color=ORANGE, lw=1.6, zorder=4)
    ax.annotate("the 1 W load, for scale", xy=(lats[-1], cfg.load_w), xytext=(0, 6),
                textcoords="offset points", color=ORANGE, fontsize=8,
                ha="right", weight="bold")
    ax.set_xlabel("Latitude  (°N)")
    ax.set_ylabel("Extra constant power (W)")
    ax.set_xticks(zoom)
    ax.set_xlim(zoom[0] - step, zoom[-1] + step * 0.8)
    ax.set_ylim(0, max(max([e for e in finite if e == e] + [0]) * 1.45, cfg.load_w * 1.25))
    ax.set_title("Extra continuous power needed to eliminate all blackouts  —  "
                 "the target for any harvester")

    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(f"  wrote {out}")

    return [dict(lat=la, blackout_days=b, min_soc_pct=s, extra_w_needed=e,
                 required_battery_wh=rb,
                 extra_battery_kg=(rb - cfg.batt_nominal_wh) / BATT_WH_PER_KG
                 if math.isfinite(rb) else float("inf"))
            for la, b, s, e, rb in zip(lats, blackout_days, min_soc, extra_w, req_batt)]


def write_csv(res: dict, out: Path) -> None:
    """Daily results -- the table view behind the charts."""
    cfg = res["cfg"]
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["day", "day_of_year", "solar_wh", "load_wh", "margin_wh",
                    "soc_end_wh", "soc_end_pct", "soc_min_pct",
                    "peak_sun_elev_deg", "unmet_wh", "sea_temp_c"])
        spd = res["steps_per_day"]
        t_sea_day = res["t_sea"].reshape(res["n_days"], spd).mean(axis=1)
        for d in range(res["n_days"]):
            w.writerow([
                d + 1, int(res["doy"][d * spd]),
                f"{res['gen_wh_day'][d]:.3f}", f"{res['load_wh_day'][d]:.3f}",
                f"{res['gen_wh_day'][d]-res['load_wh_day'][d]:.3f}",
                f"{res['soc_end_day'][d]:.1f}",
                f"{res['soc_end_day'][d]/cfg.batt_nominal_wh*100:.2f}",
                f"{res['soc_min_day'][d]/cfg.batt_nominal_wh*100:.2f}",
                f"{res['elev_max_day'][d]:.2f}",
                f"{res['unmet_wh_day'][d]:.3f}", f"{t_sea_day[d]:.2f}",
            ])
    print(f"  wrote {out}")


# ==============================================================================
# MAIN
# ==============================================================================
def main() -> None:
    p = argparse.ArgumentParser(
        description="C-Star high-latitude solar/battery power budget model",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--lat", type=float, default=60.0,
                   help="latitude in degrees (negative = southern hemisphere)")
    p.add_argument("--years", type=float, default=1.0, help="mission duration")
    p.add_argument("--load", type=float, default=1.0, help="average electrical load, W")
    p.add_argument("--seed", type=int, default=42, help="RNG seed for the weather series")
    p.add_argument("--start-day", type=int, default=1, dest="start_day",
                   help="day-of-year the mission starts (274 = 1 Oct, worst case)")
    p.add_argument("--sweep", action="store_true",
                   help="also run the latitude x season envelope sweep")
    p.add_argument("--assumptions", action="store_true",
                   help="print all labelled assumptions and exit")
    p.add_argument("--outdir", type=str, default=str(HERE))
    args = p.parse_args()

    cfg = Config(latitude_deg=args.lat, years=args.years,
                 load_w=args.load, rng_seed=args.seed, start_day=args.start_day)

    if args.assumptions:
        print_assumptions(cfg)
        return

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    res = simulate(cfg)
    print_report(res)

    tag = f"{abs(cfg.latitude_deg):.0f}{'N' if cfg.latitude_deg >= 0 else 'S'}"
    print("-- OUTPUT FILES " + "-" * 61)
    plot_main(res, outdir / f"fig1_baseline_{tag}.png")
    write_csv(res, outdir / f"daily_results_{tag}.csv")

    if args.sweep:
        rows = plot_sweep(cfg, outdir / "fig2_latitude_envelope.png")
        with (outdir / "latitude_sweep.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"  wrote {outdir/'latitude_sweep.csv'}")
    print()


if __name__ == "__main__":
    main()
