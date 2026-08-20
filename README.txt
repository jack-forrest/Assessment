================================================================================
OSHEN — GENERAL ENGINEER ASSESSMENT
Alternative Power for C-Star at High Latitude
================================================================================

WHAT THIS IS
------------
A three-hour take-home assessment for the General Engineer role at Oshen.

The brief: a customer wants to operate the C-Star — a 1 m, 40 kg autonomous
sailing robot — at high latitudes, where solar power may be limited. They have
asked whether adding a small wind turbine or a wave-energy harvester could
meaningfully improve endurance. The task is to assess whether either concept is
technically worthwhile, identify the main blockers, propose how to integrate and
test it, and recommend what Oshen should do next.

The deliverable is a note of no more than three pages, plus supporting work.
The assessment is explicitly about how an unfamiliar engineering problem is
researched, reasoned about and de-risked — not about producing a finished design.


APPROACH
--------
Rather than compare harvester concepts in the abstract, I built a power-budget
model of the baseline vehicle first, to establish how large the shortfall is,
when in the year it occurs, and above what latitude it starts to matter. That
gives a requirement in watts that any candidate solution has to beat, and a
common basis on which to compare very different options.

The work is therefore in two halves:
  1. Quantify the baseline problem     (model + latitude envelope)
  2. Assess candidate solutions        against the requirement it produces

Where information was unavailable I have made an assumption, labelled it, and
noted what it would take to firm it up. All model assumptions are tagged A1-A16
in cstar_power_model.py with a "TO FIRM UP" line each.


PLATFORM ASSUMED (given in the brief)
-------------------------------------
  1 m long, 40 kg                     Rigid wingsail, ~0.6 m^2
  50 W peak solar generation          1300 Wh LiFePO4 battery
  ~1 W average electrical consumption
  Onboard computing, comms, navigation, control and sensors


FILES
-----
  report.md                 The note. Written in Markdown, exported to PDF for
                            submission.

  cstar_power_model.py      The model. Hourly energy balance over a year:
                            solar geometry, air mass, panel-to-sun angle,
                            stochastic cloud, sail shading, sea-temperature
                            climatology, and a battery with temperature
                            derating, sub-zero charge inhibit and ageing.
                            Run directly for a single-latitude report, or with
                            --sweep for the latitude/season envelope.

  run_mission.py            Single-mission runner. Edit latitude, launch date,
                            battery size and load at the top and run. Overlays
                            several weather realisations and reports safe
                            mission duration.

  sweep_latitude.py         Safe mission duration vs latitude, with min / mean /
                            max across repeated weather runs.

  sweep_battery.py          Safe mission duration vs battery capacity, for the
                            "just fit more cells" option.

  requirements.txt          numpy, matplotlib.


RUNNING IT
----------
  python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
  ./venv/bin/python run_mission.py

Each of the three scripts has a clearly marked settings block at the top; edit
the values and run. Figures are written alongside the scripts as PNG.


CONVENTIONS
-----------
"Safe mission duration" means the time from launch until the battery first falls
below 20 % of nominal capacity. That is not the point of failure but the point at
which no reserve remains for a storm, a missed comms window or a bad run of
weather — a mission planner should already be recovering the vehicle.

State of charge is expressed as a percentage of NOMINAL capacity, not of the
capacity available at the current temperature. Traces therefore start below
100 %, because cold water genuinely derates usable LiFePO4 capacity. This is
deliberate: hiding that derate would flatter the design.


MY FEELING ON HOW IT WENT
-------------------------
I spent a fairly substantive time (~30 minutes) at the beginnning considering how
I would approach the problem. Once I decided, I got very excited about building my 
model and arguably over-estimated what was possible in the 3 hours. The brief
encourages the use of AI but I feel I ended up letting it run unchecked to a much
greater degree than I would have liked due to the limitted time and the scale of 
what I comitted to building. This was particularly true when it came to 
implementing the models of the new power generating systems, I would have liked a 
more hands on approach with the writing of them. Finally, when it came to writing
the report, I did not have the time I would have liked to fully handcraft this 
either and had to resort to proof-reading an AI built version. This is the biggest
shame as that is the work that is being assessed and it is absolutely not how I 
would approach real work. Those are the corners you have to cut when trying to 
solve a problem like this in 3 hours without having thought about it before though
I guess!
