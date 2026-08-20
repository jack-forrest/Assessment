#!/usr/bin/env python3
"""
================================================================================
C-Star LATITUDE SWEEP  --  safe mission duration vs latitude
================================================================================

Sits on top of run_mission.py. For every latitude from the equator northwards it
runs the power-budget model N times with different weather, extracts the safe
mission duration from each, and plots the min / mean / max of that spread
against latitude.

The underlying model is run with PLOT = False throughout -- this script only
wants the numbers, and skipping the per-run chart makes the sweep far quicker.

WHAT "SAFE MISSION DURATION" MEANS
----------------------------------
Time from launch until the battery first falls into the bottom UNSAFE_SOC_PCT
of nominal capacity. Below that you have no reserve left for a storm or a bad
run of weather, so a mission planner should already be recovering the vehicle.
Same definition as run_mission.py -- this script imports that logic rather than
restating it, so the two can never drift apart.

READING THE CHART
-----------------
The bold blue line is the mean of N runs. The grey band and its dashed/dotted
edges are the best and worst run at each latitude, so the width of the band is
how much of your endurance is weather luck rather than orbital geometry.

CENSORING -- IMPORTANT
----------------------
At low latitudes the vehicle never becomes unsafe: it cycles seasonally and
would run indefinitely. Those runs have no "safe duration" to measure, so they
are recorded at the simulation horizon and the curve flattens against a dashed
line at the top of the chart. Points sitting on that line mean "survived the
whole horizon", NOT "failed at day 365" -- the true value is unbounded. The
terminal output reports exactly how many runs were censored at each latitude.

================================================================================
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# ==============================================================================
#
#   >>>>>>>>>>>>>>>>  EDIT THESE, THEN CLICK RUN  <<<<<<<<<<<<<<<<
#
# ==============================================================================

START_DATE = "01/06"       # launch date, 1 June. Accepts "01/06", "1 Jun",
                           # "2026-06-01", or a day number

LAT_START = 0              # equator
LAT_END = 75               # highest latitude to test
LAT_STEP = 2               # degrees between samples

N_RUNS = 10                # weather realisations per latitude

MISSION_DAYS = 365         # simulation horizon (also the censoring ceiling)

# ---- platform, matching run_mission.py --------------------------------------
UNSAFE_SOC_PCT = 20.0      # the reserve floor that defines "safe"
BATTERY_WH = 1300.0        # nominal battery capacity
LOAD_W = 1.0               # average electrical consumption
EXTRA_POWER_W = 0.0        # constant extra generation, W -- set this to see how
                           # a harvester moves the whole curve

OPEN_PLOT = True           # pop the PNG open when finished
SEED_BASE = 1              # change for a different set of weather years

# ==============================================================================
#   (nothing below here needs editing for normal use)
# ==============================================================================

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import matplotlib.pyplot as plt

import run_mission as rm
from cstar_power_model import _style, BLUE, ORANGE, INK, INK2, MUTED, GRID

HERE = Path(__file__).resolve().parent


def configure_model() -> None:
    """
    Push this script's settings into run_mission's module-level configuration.

    run_mission is written to be edited by hand and run, so its settings live as
    module globals. Rather than duplicate its safe-duration logic here (which
    would eventually drift out of step), we set those globals once and then call
    its run_once() directly. PLOT is forced off: this sweep wants numbers only.
    """
    rm.PLOT = False
    rm.OPEN_PLOT = False
    rm.MISSION_DAYS = MISSION_DAYS
    rm.UNSAFE_SOC_PCT = UNSAFE_SOC_PCT
    rm.BATTERY_WH = BATTERY_WH
    rm.LOAD_W = LOAD_W
    rm.EXTRA_POWER_W = EXTRA_POWER_W
    rm.STOP_AT_FLAT = True


def sweep(start_doy: int) -> list:
    """Run every latitude and reduce each to min / mean / max / variance."""
    lats = list(range(LAT_START, LAT_END + 1, LAT_STEP))
    rows = []

    print(f"  Sweeping {len(lats)} latitudes x {N_RUNS} runs "
          f"= {len(lats)*N_RUNS} simulations ...\n")

    for lat in lats:
        rm.LATITUDE_DEG = float(lat)
        durations, censored = [], 0

        for i in range(N_RUNS):
            r = rm.run_once(SEED_BASE + i, start_doy)
            if r["safe_days"] is None:
                # Never became unsafe within the horizon. Record the horizon as
                # a LOWER BOUND -- the real duration is longer, possibly forever.
                durations.append(float(MISSION_DAYS))
                censored += 1
            else:
                durations.append(float(r["safe_days"]))

        a = np.array(durations)
        rows.append(dict(
            lat=lat,
            min=float(a.min()), mean=float(a.mean()), max=float(a.max()),
            var=float(a.var(ddof=1)) if a.size > 1 else 0.0,
            sd=float(a.std(ddof=1)) if a.size > 1 else 0.0,
            censored=censored, n=a.size,
        ))
        flag = f"  [{censored}/{N_RUNS} censored]" if censored else ""
        print(f"    {lat:3d}°   mean {a.mean():6.1f} d   "
              f"min {a.min():6.1f}   max {a.max():6.1f}   "
              f"var {rows[-1]['var']:8.2f}{flag}")

    return rows


def print_table(rows: list, start_label: str) -> None:
    print("\n" + "=" * 78)
    print(f"  SAFE MISSION DURATION vs LATITUDE   —   launched {start_label}, "
          f"{N_RUNS} runs each")
    print("=" * 78)
    print(f"\n  {'Lat':>5} {'Min':>8} {'Mean':>8} {'Max':>8} {'Range':>8} "
          f"{'Variance':>10} {'Std dev':>9} {'Censored':>9}")
    print("  " + "-" * 72)
    for r in rows:
        cen = f"{r['censored']}/{r['n']}" if r["censored"] else "—"
        print(f"  {r['lat']:>4}° {r['min']:>8.1f} {r['mean']:>8.1f} "
              f"{r['max']:>8.1f} {r['max']-r['min']:>8.1f} "
              f"{r['var']:>10.2f} {r['sd']:>9.2f} {cen:>9}")

    fully = [r for r in rows if r["censored"] == r["n"]]
    partly = [r for r in rows if 0 < r["censored"] < r["n"]]
    failing = [r for r in rows if r["censored"] == 0]

    print("\n  " + "-" * 72)
    if fully:
        print(f"  Survives the full {MISSION_DAYS} days in every run up to "
              f"{max(r['lat'] for r in fully)}°N.")
        print(f"    Those rows are CENSORED at the horizon — the true duration is")
        print(f"    longer than {MISSION_DAYS} d and probably unbounded, so treat their")
        print(f"    mean as a lower bound, not a measurement.")
    if partly:
        band = ", ".join(f"{r['lat']}°" for r in partly)
        print(f"  Partly censored (some runs survive, some do not): {band}")
        print("    This is the knife-edge — the same design either makes it through")
        print("    the winter or does not, depending only on the weather it draws.")
    if failing:
        first = min(r["lat"] for r in failing)
        print(f"  Every run becomes unsafe at and above {first}°N.")
        worst = failing[-1]
        print(f"    At {worst['lat']}°N: {worst['mean']:.0f} d mean "
              f"({worst['min']:.0f}–{worst['max']:.0f} d spread).")

    if failing:
        widest = max(failing, key=lambda r: r["max"] - r["min"])
        print(f"\n  Widest weather spread among fully-failing latitudes: "
              f"{widest['lat']}°N, {widest['max']-widest['min']:.0f} d "
              f"(sd {widest['sd']:.1f}).")
        print("    A narrow band means endurance is set by sun geometry and is")
        print("    repeatable; a wide one means you must size against a bad year.")
    print()


def plot_sweep(rows: list, start_label: str, out: Path) -> None:
    _style()
    fig, ax = plt.subplots(figsize=(10.0, 6.4))

    lats = np.array([r["lat"] for r in rows], dtype=float)
    mins = np.array([r["min"] for r in rows])
    means = np.array([r["mean"] for r in rows])
    maxs = np.array([r["max"] for r in rows])

    # ---- spread band ---------------------------------------------------------
    ax.fill_between(lats, mins, maxs, color=MUTED, alpha=0.14, lw=0, zorder=2,
                    label=f"Spread of {N_RUNS} runs")

    # ---- min and max, de-emphasised but legible ------------------------------
    ax.plot(lats, maxs, color=MUTED, lw=1.4, ls=(0, (5, 2)), zorder=3,
            label="Max (best weather)")
    ax.plot(lats, mins, color=MUTED, lw=1.4, ls=(0, (1.5, 2)), zorder=3,
            label="Min (worst weather)")

    # ---- the mean, carrying the emphasis -------------------------------------
    ax.plot(lats, means, color=BLUE, lw=2.6, zorder=5,
            marker="o", ms=5.5, mec="white", mew=1.2,
            label=f"Mean of {N_RUNS} runs")

    # ---- censoring ceiling ---------------------------------------------------
    censored_any = [r for r in rows if r["censored"] > 0]
    if censored_any:
        ax.axhline(MISSION_DAYS, color=ORANGE, lw=1.5, ls="--", zorder=4)
        ax.text(lats[0] + 0.4, MISSION_DAYS - 6,
                f"simulation horizon ({MISSION_DAYS} d)  —  points on this line "
                f"never became unsafe;\ntheir true endurance is longer, and at low "
                f"latitude effectively unlimited",
                color=ORANGE, fontsize=8.5, va="top", ha="left",
                weight="bold", zorder=7)

    # Mark where the design starts to fail in at least one run.
    first_fail = next((r["lat"] for r in rows if r["censored"] < r["n"]), None)
    if first_fail is not None:
        ax.axvline(first_fail, color=INK2, ls=":", lw=1.2, zorder=3)
        # Park the label low, where the curves never go -- the region under the
        # falling edge is always empty.
        ax.annotate(f"first failures\nappear at {first_fail}°N",
                    xy=(first_fail, MISSION_DAYS * 0.26),
                    xytext=(9, 0), textcoords="offset points",
                    color=INK2, fontsize=8.5, weight="bold", va="center", zorder=7)

    # ---- axes ----------------------------------------------------------------
    ax.set_xlim(lats[0] - 1, lats[-1] + 1)
    ax.set_ylim(0, MISSION_DAYS * 1.10)
    ax.set_xticks(lats)
    ax.set_xlabel("Latitude  (°N)")
    ax.set_ylabel("Safe mission duration  (days)")

    extra = ""
    if abs(BATTERY_WH - 1300.0) > 1:
        extra += f", {BATTERY_WH:.0f} Wh battery"
    if EXTRA_POWER_W:
        extra += f", +{EXTRA_POWER_W:.2f} W harvester"
    ax.set_title(f"C-Star safe mission duration vs latitude  —  launched "
                 f"{start_label}, {N_RUNS} weather runs per point{extra}")

    ax.legend(loc="lower left", ncol=1, framealpha=0.0)

    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def main() -> None:
    start_doy, start_label = rm.parse_start_date(START_DATE)
    configure_model()

    rows = sweep(start_doy)
    print_table(rows, start_label)

    out = HERE / f"sweep_latitude_{start_label.replace(' ', '')}.png"
    plot_sweep(rows, start_label, out)
    print(f"  Plot saved to: {out}\n")

    if OPEN_PLOT:
        try:
            subprocess.Popen(["xdg-open", str(out)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


if __name__ == "__main__":
    main()
