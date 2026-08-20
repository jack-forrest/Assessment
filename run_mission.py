#!/usr/bin/env python3
"""
================================================================================
C-Star MISSION RUNNER  --  edit the settings below, then click Run
================================================================================

Runs the C-Star power-budget model several times over with different weather
realisations and plots every battery trace on one chart, so you can see the
spread rather than a single lucky or unlucky year.

WHY SEVERAL RUNS: the only random element in the model is the cloud series --
an AR(1) process, so overcast arrives in multi-day spells rather than as
independent daily noise. That persistence is what actually kills the vehicle,
and it means any single run is one draw from a wide distribution. Four runs
give you a feel for how much of the answer is physics and how much is luck.

WHAT "SAFE MISSION LENGTH" MEANS HERE
-------------------------------------
The shaded red band is the bottom 20 % of nominal battery capacity. Reaching
into it is treated as the end of safe operation, not the end of the mission --
below roughly this point you have no reserve left for a storm, a failed comms
window, or a bad run of weather, so a mission planner should already be
recovering the vehicle. Safe mission length is therefore the time from launch
until the trace FIRST touches that band.

Note the trace starts near 90 %, not 100 %. That is not an error: cold water
derates usable LiFePO4 capacity, and state of charge here is expressed as a
percentage of the 1300 Wh NOMINAL rating so the number stays comparable
between latitudes and seasons.

================================================================================
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

# ==============================================================================
#
#   >>>>>>>>>>>>>>>>  EDIT THESE, THEN CLICK RUN  <<<<<<<<<<<<<<<<
#
# ==============================================================================

LATITUDE_DEG = 65.0        # degrees. Negative for southern hemisphere (-55 = Southern Ocean)

START_DATE = "01/06"       # when the vehicle is launched. Accepts:
                           #   "1 Oct"  /  "1 October"  /  "2026-10-01"  /  274

MISSION_DAYS = 365         # how long to simulate for

N_RUNS = 3                 # how many weather realisations to overlay

UNSAFE_SOC_PCT = 20.0      # the red band: below this much of nominal capacity
                           # you have no reserve left

BATTERY_WH = 1300.0        # nominal battery capacity. The baseline C-Star is 1300 Wh;
                           # raise it to test "just add more cells" as an alternative
                           # to fitting a harvester. Roughly 110 Wh per kg of pack,
                           # so +1300 Wh is about +12 kg on a 40 kg vehicle.
                           # NOTE: cold water still derates the USABLE fraction of
                           # whatever you put here — the trace starts below 100 %
                           # for that reason, at any capacity.

# ---- less commonly changed ---------------------------------------------------
LOAD_W = 1.0               # average electrical consumption
WIND_TURBINE = False       # fit a micro wind turbine
WAVE_HARVESTER = False     # fit an inertial wave-energy harvester
WATER_TURBINE = False      # regenerate through the propeller while sailing

EXTRA_POWER_W = 0.0        # constant extra generation, W -- set this to try out
                           # a hypothetical wind turbine or wave harvester
PLOT = True                # draw the chart at all. Set False for a numbers-only
                           # run -- much quicker when sweeping settings by hand
STOP_AT_FLAT = True        # stop drawing a trace once its battery hits zero
OPEN_PLOT = True           # pop the PNG open automatically when finished
                           # (ignored when PLOT = False)
SEED_BASE = 1              # change this for a completely different set of years

# ==============================================================================
#   (nothing below here needs editing for normal use)
# ==============================================================================

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import matplotlib.pyplot as plt

from cstar_power_model import (
    Config, simulate, _style,
    MONTH_STARTS, MONTH_NAMES,
    BLUE, ORANGE, AQUA, RED, INK, INK2, MUTED, GRID,
)

YELLOW = "#eda100"                      # categorical slot 4
RUN_COLOURS = [BLUE, ORANGE, AQUA, YELLOW]

HERE = Path(__file__).resolve().parent


# ------------------------------------------------------------------------------
# Settings helpers
# ------------------------------------------------------------------------------
def parse_start_date(value) -> tuple:
    """Accept a day-of-year int or a readable date string -> (day_of_year, label)."""
    if isinstance(value, (int, float)):
        doy = int(value)
        ref = datetime.strptime(f"2001-{doy}", "%Y-%j")
        return doy, ref.strftime("%-d %b")

    text = str(value).strip()
    for fmt in ("%d %b", "%d %B", "%d %b %Y", "%d %B %Y",
                "%Y-%m-%d", "%d/%m/%Y", "%d/%m", "%b %d", "%B %d"):
        try:
            d = datetime.strptime(text, fmt)
        except ValueError:
            continue
        # Normalise onto a common non-leap year so day-of-year is consistent
        ref = datetime(2001, d.month, d.day)
        return ref.timetuple().tm_yday, ref.strftime("%-d %b")
    raise ValueError(
        f"Could not read START_DATE = {value!r}. "
        'Try something like "1 Oct", "2026-10-01", or a day number such as 274.')


def month_ticks(start_doy: int, n_days: int) -> tuple:
    """Tick positions (days into mission) at each calendar month boundary."""
    ticks, labels = [], []
    for d in range(n_days + 1):
        doy = ((start_doy - 1 + d) % 365) + 1
        if doy in MONTH_STARTS[:12]:
            ticks.append(d)
            labels.append(MONTH_NAMES[MONTH_STARTS.index(doy)])
    return ticks, labels


def date_for(start_doy: int, day_offset: float) -> str:
    """Human-readable calendar date `day_offset` days after the start."""
    doy = int(((start_doy - 1 + day_offset) % 365) + 1)
    return datetime.strptime(f"2001-{doy}", "%Y-%j").strftime("%-d %b")


# ------------------------------------------------------------------------------
# One run
# ------------------------------------------------------------------------------
def run_once(seed: int, start_doy: int) -> dict:
    """
    Simulate one weather realisation and pull out the two numbers that matter:
    when the trace first enters the unsafe band, and when (if ever) it flatlines.
    """
    cfg = Config(
        latitude_deg=LATITUDE_DEG,
        years=MISSION_DAYS / 365.0,
        batt_nominal_wh=BATTERY_WH,
        load_w=LOAD_W,
        enable_wind_turbine=WIND_TURBINE,
        enable_wave_harvester=WAVE_HARVESTER,
        enable_water_turbine=WATER_TURBINE,
        extra_power_w=EXTRA_POWER_W,
        start_day=start_doy,
        rng_seed=seed,
    )
    res = simulate(cfg)

    days = res["t_hours"] / 24.0
    soc_pct = res["soc_wh"] / cfg.batt_nominal_wh * 100.0

    # First entry into the unsafe band = end of safe operation.
    unsafe = np.flatnonzero(soc_pct <= UNSAFE_SOC_PCT)
    safe_days = float(days[unsafe[0]]) if unsafe.size else None

    # First time the battery is actually flat = mission over.
    flat = np.flatnonzero(res["soc_wh"] <= 1e-6)
    flat_days = float(days[flat[0]]) if flat.size else None

    # Optionally stop the trace at the flatline -- everything after it is
    # fictional anyway, since the vehicle has stopped operating.
    if STOP_AT_FLAT and flat.size:
        cut = flat[0] + 1
        days, soc_pct = days[:cut], soc_pct[:cut]

    return dict(seed=seed, days=days, soc_pct=soc_pct,
                safe_days=safe_days, flat_days=flat_days,
                min_soc=float(res["soc_wh"].min() / cfg.batt_nominal_wh * 100.0),
                blackout_days=res["blackout_days"], cfg=cfg)


# ------------------------------------------------------------------------------
# Plot
# ------------------------------------------------------------------------------
def plot_runs(runs: list, start_doy: int, start_label: str, out: Path) -> None:
    _style()
    fig, ax = plt.subplots(figsize=(10.5, 6.2))

    # If every trace terminates well before the horizon, zoom in on the part
    # that actually has data -- otherwise the interesting weeks get squeezed
    # into the left-hand tenth of a year-long axis.
    longest = max(float(r["days"][-1]) for r in runs)
    x_max = MISSION_DAYS if longest >= MISSION_DAYS * 0.9 \
        else min(MISSION_DAYS, max(longest * 1.22, 21.0))
    zoomed = x_max < MISSION_DAYS * 0.98

    # ---- the unsafe band -----------------------------------------------------
    ax.axhspan(0, UNSAFE_SOC_PCT, color=RED, alpha=0.10, lw=0, zorder=1)
    ax.axhline(UNSAFE_SOC_PCT, color=RED, lw=1.2, ls="--", alpha=0.65, zorder=2)
    ax.text(x_max * 0.012, UNSAFE_SOC_PCT - 2.0,
            f"unsafe  —  below {UNSAFE_SOC_PCT:.0f} % of nominal, no reserve left",
            color=RED, fontsize=8.5, ha="left", va="top", weight="bold", zorder=6)

    # ---- one trace per weather realisation -----------------------------------
    for i, r in enumerate(runs):
        col = RUN_COLOURS[i % len(RUN_COLOURS)]
        ax.plot(r["days"], r["soc_pct"], color=col, lw=1.5, zorder=4,
                label=f"Run {i+1}")

        # Mark where safe operation ends, and where the battery finally dies.
        if r["safe_days"] is not None:
            ax.plot([r["safe_days"]], [UNSAFE_SOC_PCT], marker="s", ms=6.5,
                    color=col, mec="white", mew=1.4, zorder=6)
        if r["flat_days"] is not None:
            ax.plot([r["flat_days"]], [0], marker="X", ms=8,
                    color=col, mec="white", mew=1.4, zorder=6)

    # ---- axes ----------------------------------------------------------------
    ax.set_xlim(0, x_max)
    ax.set_ylim(-3, 104)
    ax.set_xlabel(f"Days into mission   (launched {start_label})")
    ax.set_ylabel(f"Battery state of charge  (% of {BATTERY_WH:.0f} Wh nominal)")

    hemi = "N" if LATITUDE_DEG >= 0 else "S"
    extra = ""
    if abs(BATTERY_WH - 1300.0) > 1:
        extra += f", {BATTERY_WH:.0f} Wh battery"
    if EXTRA_POWER_W:
        extra += f", +{EXTRA_POWER_W:.2f} W harvester"
    ax.set_title(
        f"C-Star battery life at {abs(LATITUDE_DEG):.0f}°{hemi}, launched {start_label}"
        f"  —  {len(runs)} weather realisations{extra}",
        pad=26)

    # Calendar months along the top: days-into-mission is what you plan against,
    # but the season is what actually drives the answer.
    ticks, labels = month_ticks(start_doy, MISSION_DAYS)
    top = ax.secondary_xaxis("top")
    top.set_xticks(ticks)
    top.set_xticklabels(labels, fontsize=8, color=MUTED)
    top.tick_params(length=3, color=GRID)
    for t in ticks:
        ax.axvline(t, color=GRID, lw=0.7, zorder=0)

    # A bare colour key -- every statistic now goes to the terminal instead,
    # so the chart stays a picture of the traces and nothing else.
    ax.legend(loc="upper right", ncol=len(runs), framealpha=0.0)

    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


# ------------------------------------------------------------------------------
# Console summary -- the table view behind the chart
# ------------------------------------------------------------------------------
def print_summary(runs: list, start_doy: int, start_label: str) -> None:
    hemi = "N" if LATITUDE_DEG >= 0 else "S"
    print("=" * 76)
    print(f"  C-STAR MISSION  —  {abs(LATITUDE_DEG):.0f}° {hemi}, launched {start_label}, "
          f"{MISSION_DAYS} day horizon")
    print(f"  Battery {BATTERY_WH:.0f} Wh"
          + (f"  ({BATTERY_WH/1300.0:.2f}x baseline, "
             f"{(BATTERY_WH-1300.0)/110.0:+.1f} kg of cells)"
             if abs(BATTERY_WH - 1300.0) > 1 else "  (baseline)")
          + f"   |   load {LOAD_W:.2f} W")
    if EXTRA_POWER_W:
        print(f"  Including {EXTRA_POWER_W:.2f} W of extra generation")
    print("=" * 76)
    print(f"\n  {'Run':>4} {'Safe days':>10} {'Unsafe from':>13} "
          f"{'Flat at':>9} {'Flat on':>10} {'Min SoC':>9}")
    print("  " + "-" * 62)
    for i, r in enumerate(runs):
        safe = f"{r['safe_days']:.0f}" if r["safe_days"] is not None else "all"
        udate = date_for(start_doy, r["safe_days"]) if r["safe_days"] is not None else "—"
        flat = f"{r['flat_days']:.0f} d" if r["flat_days"] is not None else "—"
        fdate = date_for(start_doy, r["flat_days"]) if r["flat_days"] is not None else "—"
        print(f"  {i+1:>4} {safe:>10} {udate:>13} {flat:>9} {fdate:>10} "
              f"{r['min_soc']:>8.1f}%")

    safe = [r["safe_days"] for r in runs if r["safe_days"] is not None]
    censored = len(runs) - len(safe)

    print("\n  " + "-" * 62)
    if not safe:
        print(f"  VERDICT: all {len(runs)} runs stayed safe for the full "
              f"{MISSION_DAYS} days.")
        print()
        return

    a = np.array(safe, dtype=float)
    # Sample variance (ddof=1): these are N draws from the weather distribution,
    # not the whole population. Standard deviation is quoted alongside it because
    # variance is in days-squared and hard to reason about directly.
    var = float(a.var(ddof=1)) if a.size > 1 else 0.0
    sd = float(np.sqrt(var))

    print("  SAFE MISSION LENGTH  (days)")
    print(f"    mean      : {a.mean():8.1f}")
    print(f"    min       : {a.min():8.1f}")
    print(f"    max       : {a.max():8.1f}")
    print(f"    range     : {a.max()-a.min():8.1f}")
    print(f"    variance  : {var:8.2f}   (sample, ddof=1)")
    print(f"    std dev   : {sd:8.2f}")
    print(f"    median    : {np.median(a):8.1f}")
    print(f"    n         : {a.size:8d} of {len(runs)} runs")

    if censored:
        print(f"\n  NOTE: {censored} of {len(runs)} runs never became unsafe within "
              f"{MISSION_DAYS} days.\n        Those runs are EXCLUDED from the "
              f"statistics above — including them\n        at the horizon value would "
              f"bias the mean downward. The true mean\n        is therefore at least "
              f"the figure shown.")

    spread = a.max() - a.min()
    frac = spread / max(np.median(a), 1)
    if frac <= 0.05:
        print("\n  -> GEOMETRY-limited: the runs barely differ, so the answer is set")
        print("     by sun angle and day length, not by weather luck. Good news for")
        print("     planning — the endurance figure is repeatable.")
    elif frac <= 0.15:
        print("\n  -> MOSTLY GEOMETRY-limited: sun angle sets the answer, but a bad")
        print(f"     year still costs you ~{spread:.0f} days. Quote the worst case.")
    else:
        print("\n  -> WEATHER-limited: a bad run of cloud moves the answer materially.")
        print("     Size against the worst case, not the mean, and firm up the cloud")
        print("     data (assumption A9) before trusting it.")

    print(f"\n  Plan against the WORST run ({a.min():.0f} d), not the mean — a mission")
    print("  plan built on an average year fails half the time.")
    print()


def main() -> None:
    start_doy, start_label = parse_start_date(START_DATE)

    runs = [run_once(SEED_BASE + i, start_doy) for i in range(N_RUNS)]

    print_summary(runs, start_doy, start_label)

    if not PLOT:
        print("  (PLOT = False — no chart drawn)\n")
        return

    hemi = "N" if LATITUDE_DEG >= 0 else "S"
    out = HERE / f"mission_{abs(LATITUDE_DEG):.0f}{hemi}_{start_label.replace(' ','')}.png"
    plot_runs(runs, start_doy, start_label, out)
    print(f"  Plot saved to: {out}\n")

    if OPEN_PLOT:
        try:
            subprocess.Popen(["xdg-open", str(out)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass  # no desktop session -- the PNG is on disk regardless


if __name__ == "__main__":
    main()
