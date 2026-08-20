#!/usr/bin/env python3
"""
Side-by-side latitude sweeps for all three generation options.

Produces one wide figure with three panels sharing a y-axis, so the options can
be compared visually at a glance. Each panel shows the min/mean/max safe mission
duration across N weather runs, with the solar-only baseline drawn behind it as
a reference so the improvement (or lack of one) is immediately visible.

    python sweep_compare.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

START_DATE = "01/06"
LAT_START, LAT_END, LAT_STEP = 0, 74, 2
N_RUNS = 10
MISSION_DAYS = 365
UNSAFE_SOC_PCT = 20.0
OPEN_PLOT = True
SEED_BASE = 1

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import matplotlib.pyplot as plt

import run_mission as rm
from cstar_power_model import _style, BLUE, ORANGE, INK, INK2, MUTED, GRID

HERE = Path(__file__).resolve().parent

OPTIONS = [
    ("Micro wind turbine", dict(WIND_TURBINE=True)),
    ("Wave harvester", dict(WAVE_HARVESTER=True)),
    ("Propeller regeneration", dict(WATER_TURBINE=True)),
]


def run_config(start_doy, **flags):
    """Sweep latitude for one hardware configuration."""
    rm.PLOT = False
    rm.OPEN_PLOT = False
    rm.MISSION_DAYS = MISSION_DAYS
    rm.UNSAFE_SOC_PCT = UNSAFE_SOC_PCT
    rm.BATTERY_WH = 1300.0
    rm.LOAD_W = 1.0
    rm.EXTRA_POWER_W = 0.0
    rm.STOP_AT_FLAT = True
    rm.WIND_TURBINE = flags.get("WIND_TURBINE", False)
    rm.WAVE_HARVESTER = flags.get("WAVE_HARVESTER", False)
    rm.WATER_TURBINE = flags.get("WATER_TURBINE", False)

    lats = list(range(LAT_START, LAT_END + 1, LAT_STEP))
    mins, means, maxs = [], [], []
    for lat in lats:
        rm.LATITUDE_DEG = float(lat)
        d = []
        for i in range(N_RUNS):
            r = rm.run_once(SEED_BASE + i, start_doy)
            d.append(float(MISSION_DAYS) if r["safe_days"] is None
                     else float(r["safe_days"]))
        a = np.array(d)
        mins.append(a.min()); means.append(a.mean()); maxs.append(a.max())
    return (np.array(lats, dtype=float), np.array(mins),
            np.array(means), np.array(maxs))


def main() -> None:
    start_doy, start_label = rm.parse_start_date(START_DATE)
    _style()

    print("  baseline (solar only) ...")
    lats, _, base_mean, _ = run_config(start_doy)

    fig, axes = plt.subplots(1, 3, figsize=(13.2, 4.3), sharey=True)

    for ax, (name, flags) in zip(axes, OPTIONS):
        print(f"  {name} ...")
        _, mn, mu, mx = run_config(start_doy, **flags)

        ax.axhline(MISSION_DAYS, color=ORANGE, lw=1.2, ls="--", zorder=3)
        # Solar-only reference, so each panel shows its own improvement.
        ax.plot(lats, base_mean, color="#3d3d3a", lw=1.4, ls=(0, (4, 2)),
                zorder=8, label="Solar only (baseline)")
        ax.fill_between(lats, mn, mx, color=BLUE, alpha=0.16, lw=0, zorder=5,
                        label=f"Spread of {N_RUNS} runs")
        ax.plot(lats, mu, color=BLUE, lw=2.4, marker="o", ms=3.8,
                mec="white", mew=0.8, zorder=6, label="Mean with option fitted")

        # Where does this option stop working?
        fails = [la for la, m in zip(lats, mn) if m < MISSION_DAYS]
        limit = (fails[0] - LAT_STEP) if fails else lats[-1]
        ax.set_title(f"{name}\nalways survives to {limit:.0f}°N",
                     fontsize=10, pad=6)
        ax.set_xlabel("Latitude (°N)")
        ax.set_xlim(lats[0], lats[-1])
        ax.set_xticks(np.arange(0, 75, 15))

    axes[0].set_ylabel("Safe mission duration (days)")
    axes[0].set_ylim(0, MISSION_DAYS * 1.10)
    axes[0].legend(loc="lower left", fontsize=7.6, framealpha=0.0)
    axes[0].text(2, MISSION_DAYS + 6, f"{MISSION_DAYS} d horizon",
                 color=ORANGE, fontsize=7.6, weight="bold", va="bottom")

    fig.suptitle(f"Safe mission duration vs latitude for each option  —  launched "
                 f"{start_label}, {N_RUNS} weather runs per point",
                 fontsize=11, weight="bold", y=1.02)
    fig.tight_layout()
    out = HERE / "sweep_compare_1Jun.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out}")

    if OPEN_PLOT:
        try:
            subprocess.Popen(["xdg-open", str(out)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


if __name__ == "__main__":
    main()
