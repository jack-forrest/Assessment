#!/usr/bin/env python3
"""
================================================================================
C-Star BATTERY SWEEP  --  safe mission duration vs battery capacity
================================================================================

The first option any harvester proposal has to beat is the boring one: fit more
cells. This script sweeps nominal battery capacity at a fixed latitude and launch
date, and plots how much extra safe mission duration each additional watt-hour
actually buys.

Same structure as sweep_latitude.py: the model is driven with PLOT = False, N
weather realisations are run at every point, and the min / mean / max of the
resulting safe durations are plotted as a band around a bold mean.

WHY THIS IS THE RIGHT COMPARISON
--------------------------------
A bigger battery is not free. On a 40 kg vehicle, cells are paid for in mass, and
mass on a small sailing hull costs waterline, righting moment and payload. The
chart therefore carries a second scale along the top showing the added pack mass,
so the reader can see immediately what a given endurance gain costs. That is the
same axis in different units, not a second data series.

WHAT THE CURVE ACTUALLY DOES
----------------------------
Below the survival threshold the extra days bought scale LINEARLY with added
capacity -- each watt-hour simply buys proportionally more winter to burn
through. Note this is not the same as endurance scaling with capacity: doubling
the pack does not double the mission, because most of the summer half never
touches the battery at all. Two effects also work against the added cells --
the 20 % "safe" floor is a fraction of NOMINAL capacity, so a bigger pack locks
away a bigger absolute reserve, and cold water derates whatever is installed.

The important feature is not a gradient but a STEP: capacity buys extra days,
until suddenly it buys an indefinite mission.

CENSORING
---------
Once the pack is large enough to carry the vehicle through to spring, it survives
the whole horizon and the true duration becomes unbounded -- it would then run
indefinitely, because it returns to the surplus conditions it launched in. Those
points are recorded at the horizon and flagged; the minimum capacity that first
achieves this is reported separately, and is the number that matters.

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

LATITUDE_DEG = 60.0        # fixed latitude for the sweep
START_DATE = "01/06"       # launch date, 1 June (see report section 2)

BATT_START_WH = 1300.0     # baseline C-Star pack
BATT_END_WH = 7800.0       # 6x baseline -- deliberately past the plausible limit,
                           # so the survival threshold and the plateau beyond it
                           # are both visible
BATT_STEP_WH = 260.0       # 0.2x baseline per step

N_RUNS = 10                # weather realisations per capacity
MISSION_DAYS = 365         # simulation horizon (also the censoring ceiling)

# ---- platform ----------------------------------------------------------------
UNSAFE_SOC_PCT = 20.0      # reserve floor defining "safe"
LOAD_W = 1.0               # average electrical consumption
EXTRA_POWER_W = 0.0        # constant extra generation, W

WH_PER_KG = 110.0          # ASSUMPTION A16: pack-level LiFePO4 energy density,
                           # including cells, BMS, wiring and potting. Cell-level
                           # is ~150 Wh/kg. TO FIRM UP: weigh the existing pack.
VEHICLE_KG = 40.0          # GIVEN: vehicle mass, for the mass-fraction figure

OPEN_PLOT = True
SEED_BASE = 1

# ==============================================================================
#   (nothing below here needs editing for normal use)
# ==============================================================================

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import matplotlib.pyplot as plt

import run_mission as rm
from cstar_power_model import _style, BLUE, ORANGE, INK, INK2, MUTED

HERE = Path(__file__).resolve().parent


def configure_model() -> None:
    """Push settings into run_mission and force PLOT off -- numbers only."""
    rm.PLOT = False
    rm.OPEN_PLOT = False
    rm.LATITUDE_DEG = LATITUDE_DEG
    rm.MISSION_DAYS = MISSION_DAYS
    rm.UNSAFE_SOC_PCT = UNSAFE_SOC_PCT
    rm.LOAD_W = LOAD_W
    rm.EXTRA_POWER_W = EXTRA_POWER_W
    rm.STOP_AT_FLAT = True


def sweep(start_doy: int) -> list:
    caps = np.arange(BATT_START_WH, BATT_END_WH + 1e-6, BATT_STEP_WH)
    rows = []

    print(f"  Sweeping {len(caps)} capacities x {N_RUNS} runs "
          f"= {len(caps)*N_RUNS} simulations at {LATITUDE_DEG:.0f}°N ...\n")

    for cap in caps:
        rm.BATTERY_WH = float(cap)
        durations, censored = [], 0

        for i in range(N_RUNS):
            r = rm.run_once(SEED_BASE + i, start_doy)
            if r["safe_days"] is None:
                durations.append(float(MISSION_DAYS))
                censored += 1
            else:
                durations.append(float(r["safe_days"]))

        a = np.array(durations)
        added_kg = (cap - BATT_START_WH) / WH_PER_KG
        rows.append(dict(
            wh=float(cap), added_kg=added_kg,
            min=float(a.min()), mean=float(a.mean()), max=float(a.max()),
            var=float(a.var(ddof=1)) if a.size > 1 else 0.0,
            sd=float(a.std(ddof=1)) if a.size > 1 else 0.0,
            censored=censored, n=a.size,
        ))
        flag = f"  [{censored}/{N_RUNS} censored]" if censored else ""
        print(f"    {cap:6.0f} Wh ({cap/BATT_START_WH:4.1f}x, +{added_kg:5.1f} kg)   "
              f"mean {a.mean():6.1f} d   min {a.min():6.1f}   "
              f"max {a.max():6.1f}{flag}")

    return rows


def print_table(rows: list, start_label: str) -> None:
    base = rows[0]

    print("\n" + "=" * 80)
    print(f"  SAFE MISSION DURATION vs BATTERY CAPACITY   —   {LATITUDE_DEG:.0f}°N, "
          f"launched {start_label}")
    print("=" * 80)
    print(f"\n  {'Capacity':>9} {'Mult':>6} {'Added':>8} {'Min':>7} {'Mean':>7} "
          f"{'Max':>7} {'Gain':>7} {'d/kg':>7} {'Cens':>6}")
    print(f"  {'(Wh)':>9} {'':>6} {'(kg)':>8} {'(d)':>7} {'(d)':>7} "
          f"{'(d)':>7} {'(d)':>7} {'':>7} {'':>6}")
    print("  " + "-" * 74)
    for r in rows:
        gain = r["mean"] - base["mean"]
        # Marginal efficiency: extra days bought per extra Wh installed.
        per = (f"{gain/r['added_kg']:7.1f}"
               if r["added_kg"] > 0 and r["censored"] == 0 else f"{'—':>7}")
        cen = f"{r['censored']}/{r['n']}" if r["censored"] else "—"
        print(f"  {r['wh']:>9.0f} {r['wh']/base['wh']:>5.1f}x {r['added_kg']:>8.1f} "
              f"{r['min']:>7.1f} {r['mean']:>7.1f} {r['max']:>7.1f} "
              f"{gain:>+7.1f} {per} {cen:>6}")

    print("\n  " + "-" * 74)
    print(f"  Baseline ({base['wh']:.0f} Wh): {base['mean']:.0f} d mean "
          f"({base['min']:.0f}–{base['max']:.0f} d)")

    # The number that actually decides the option: smallest pack that survives.
    survivors = [r for r in rows if r["censored"] == r["n"]]
    partial = [r for r in rows if 0 < r["censored"] < r["n"]]

    if survivors:
        s = survivors[0]
        print(f"\n  SMALLEST PACK THAT SURVIVES THE FULL {MISSION_DAYS} DAYS IN EVERY RUN:")
        print(f"    {s['wh']:.0f} Wh  ({s['wh']/base['wh']:.1f}x baseline)")
        print(f"    = +{s['added_kg']:.1f} kg of cells, "
              f"{s['added_kg']/VEHICLE_KG*100:.0f} % of the {VEHICLE_KG:.0f} kg vehicle")
        print(f"    Surviving the first winter implies indefinite operation — the")
        print(f"    vehicle recovers over the following summer.")
    else:
        print(f"\n  NO pack in the swept range ({BATT_START_WH:.0f}–{BATT_END_WH:.0f} Wh)")
        print(f"  survives the full {MISSION_DAYS} days in every run.")

    if partial:
        print(f"\n  Marginal band (some runs survive, some do not): "
              f"{partial[0]['wh']:.0f}–{partial[-1]['wh']:.0f} Wh")
        print("    Sizing anywhere in this band is a coin toss on the weather.")

    # Marginal value of capacity, measured ONLY over the uncensored region.
    # Beyond the survival threshold the mean is pinned at the horizon, so any
    # rate computed across it would be an artefact of where the run was stopped
    # rather than a property of the vehicle.
    live = [r for r in rows if r["censored"] == 0]
    if len(live) >= 2:
        x = np.array([r["wh"] for r in live])
        y = np.array([r["mean"] for r in live])
        slope = float(np.polyfit(x, y, 1)[0])          # days per Wh
        resid = y - np.polyval(np.polyfit(x, y, 1), x)
        r2 = 1.0 - resid.var() / y.var() if y.var() > 0 else 1.0

        print(f"\n  MARGINAL VALUE OF CAPACITY  (below the survival threshold)")
        print(f"    {slope*1000:.1f} days per 1000 Wh   =   "
              f"{slope*WH_PER_KG:.1f} days per kg of cells")
        print(f"    Linear fit over {live[0]['wh']:.0f}–{live[-1]['wh']:.0f} Wh, "
              f"R² = {r2:.4f}")
        print("    The relationship is LINEAR, not diminishing: below the threshold")
        print("    every extra watt-hour simply buys proportionally more winter to")
        print("    burn through. There is no efficiency argument against a bigger")
        print("    pack — the argument against it is mass, and the fact that buying")
        print("    days is not the same as solving the problem.")
        print("\n    Note this is a step change, not a gradient: capacity buys extra")
        print("    days until it suddenly buys an indefinite mission. Sizing just")
        print("    below the threshold delivers a vehicle that still dies, only later.")
    print()


def plot_sweep(rows: list, start_label: str, out: Path) -> None:
    _style()
    fig, ax = plt.subplots(figsize=(10.0, 6.4))

    wh = np.array([r["wh"] for r in rows])
    mins = np.array([r["min"] for r in rows])
    means = np.array([r["mean"] for r in rows])
    maxs = np.array([r["max"] for r in rows])

    ax.fill_between(wh, mins, maxs, color=MUTED, alpha=0.14, lw=0, zorder=2,
                    label=f"Spread of {N_RUNS} runs")
    ax.plot(wh, maxs, color=MUTED, lw=1.4, ls=(0, (5, 2)), zorder=3,
            label="Max (best weather)")
    ax.plot(wh, mins, color=MUTED, lw=1.4, ls=(0, (1.5, 2)), zorder=3,
            label="Min (worst weather)")
    ax.plot(wh, means, color=BLUE, lw=2.6, zorder=5, marker="o", ms=4.5,
            mec="white", mew=1.0, label=f"Mean of {N_RUNS} runs")

    # ---- censoring ceiling ---------------------------------------------------
    if any(r["censored"] for r in rows):
        ax.axhline(MISSION_DAYS, color=ORANGE, lw=1.5, ls="--", zorder=4)
        # Sit the caption in the empty strip ABOVE the horizon line -- the rising
        # edge below it is where the max curve lives.
        ax.text(wh[-1], MISSION_DAYS + 6,
                f"simulation horizon ({MISSION_DAYS} d)  —  packs reaching this line "
                f"survive the winter and would then run indefinitely",
                color=ORANGE, fontsize=8.5, va="bottom", ha="right",
                weight="bold", zorder=7)

    # ---- the decision point --------------------------------------------------
    survivors = [r for r in rows if r["censored"] == r["n"]]
    if survivors:
        s = survivors[0]
        ax.axvline(s["wh"], color=INK2, ls=":", lw=1.2, zorder=3)
        ax.annotate(
            f"smallest pack that always\nsurvives: {s['wh']:.0f} Wh "
            f"(+{s['added_kg']:.0f} kg,\n{s['added_kg']/VEHICLE_KG*100:.0f} % of vehicle mass)",
            xy=(s["wh"], MISSION_DAYS * 0.30), xytext=(10, 0),
            textcoords="offset points", color=INK2, fontsize=8.5,
            weight="bold", va="center", zorder=7)

    # ---- axes ----------------------------------------------------------------
    ax.set_xlim(wh[0], wh[-1])
    ax.set_ylim(0, MISSION_DAYS * 1.10)
    ax.set_xlabel("Nominal battery capacity  (Wh)")
    ax.set_ylabel("Safe mission duration  (days)")
    ax.set_title(f"C-Star safe mission duration vs battery capacity  —  "
                 f"{LATITUDE_DEG:.0f}°N, launched {start_label}, "
                 f"{N_RUNS} weather runs per point")

    # Added pack mass along the top. This is the SAME axis in different units,
    # not a second scale -- it converts capacity into what it actually costs.
    top = ax.secondary_xaxis(
        "top",
        functions=(lambda w: (w - BATT_START_WH) / WH_PER_KG,
                   lambda k: k * WH_PER_KG + BATT_START_WH))
    top.set_xlabel("Added pack mass above baseline  (kg)", fontsize=9, color=INK2)
    top.tick_params(labelsize=8.5, colors=INK2)

    ax.legend(loc="lower right", ncol=1, framealpha=0.0)

    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def main() -> None:
    start_doy, start_label = rm.parse_start_date(START_DATE)
    configure_model()

    rows = sweep(start_doy)
    print_table(rows, start_label)

    out = HERE / (f"sweep_battery_{LATITUDE_DEG:.0f}N_"
                  f"{start_label.replace(' ', '')}.png")
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
