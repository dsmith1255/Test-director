# Plan C simulator prototype and overnight pilot

## What is changed

WIMNA 0.5.0 adds two opt-in AI test modes that use the same installed FH26 car and
extended-physics setting. The baseline mode applies no WIMNA force. The Plan C
prototype applies a small external aerodynamic force through CSP's new-mode
Physics API. It leaves RSS car files, `data.acd`, scripts, and profiles untouched.
The test modes are an engineering experiment, not an official RSS car update or
the complete competition package.

The prototype adds equivalent drag area **0.012 m²** and equivalent downforce
area **0.030 m²**. At each physics step, requested force is proportional to
`0.5 × air_density × speed² × area`. Air density uses measured ambient
temperature and an assumed 101,325 Pa pressure, with 20 °C as a labeled
fallback if ambient is unavailable. Speed is capped at 100 m/s for this
calculation, and requested force is bounded at 100 N drag and 250 N downforce.
These values were chosen before the A/B data and are `ASSUMED`, not fitted to
the case packet's 0.06–0.12 s/lap estimate. More downforce might help corners;
more drag might cost speed on straights. A favorable result is not guaranteed.

The existing custom thermal analysis may test a Plan C cooling-airflow
multiplier of 1.10, with sensitivity across its declared range. That multiplier
does not change the installed car's native component temperature or electric
power. No native brake-cooling, battery-life, or reliability improvement is
claimed. The prototype does not alter RSS energy maps.

## What the laps can establish

CSP documents that `physics.addForce()` invalidates the native current lap.
Therefore a prototype lap can still contain measured simulator speed, fuel,
tire state, and a complete crossing time, but must retain `native_valid=false`
and the known intervention as context. Cuts, spins, resets, contact and gaps
remain separate exclusion reasons. The comparison is an exploratory **simulated
intervention**, not an official clean-lap record. Requested force alone is not
proof of a physical effect; matched distance speed and sector behavior must be
checked against the baseline mode.

Use the same tire compound, starting fuel, energy target/map, setup, assists,
weather, grip, progression, and mode family within each track pair. Record
requested and observed values independently. Each track change is a new
physical session; fuel, tire, and battery state cannot carry over by run ID.

| Hour | Track/layout | Variant |
| --- | --- | --- |
| 1 | Spa/default | Baseline |
| 2 | Monza/default | Plan C prototype |
| 3 | Imola/default | Baseline |
| 4 | Silverstone GP | Plan C prototype |
| 5 | Spa/default | Plan C prototype |
| 6 | Monza/default | Baseline |
| 7 | Imola/default | Plan C prototype |
| 8 | Silverstone GP | Baseline |

At the hourly boundary, finish the current lap, close and save the run, then
launch the next exact preset. Warmup and all invalid laps stay in raw telemetry.
Comparable complete crossings are used for the pace estimate. The hour is a
switching cadence, not a recording gate or accepted-lap quota. A local runner
must stop if it cannot verify the next track, variant, AI control, or recorder.
An hourly Codex task can summarize progress and failures, but must not be the
timing-critical controller for game sessions.

The shipped queue currently completes this matrix only with **synthetic**
readbacks. Its live Content Manager launcher is dormant until a matching
two-session preflight is recorded, and no verified game-session close/readback
adapter is bundled. The generated Spa preset pair in the local `build` folder
has not been launched. The hourly Codex monitor is paused until a live runner
and the user's start signal are both ready. Do not use the fixture queue to
claim unattended track switching.

For each track, compare the prototype and baseline AI pace after accounting
for starting fuel, tire age, battery state, environment, and session order.
Laps within one stint are correlated, so they are not counted as independent
replications. Tyler's 38 clean human laps anchor absolute baseline pace. Adding
an AI prototype-minus-baseline delta to that human anchor is an `ASSUMED`
transfer until a held-out human prototype run checks it. Do not add the case's
expected Plan C gain again.

## Release and live gates

Before an unattended run, complete a short local two-session smoke test:
install the new modes, record AI laps in both, verify package identity and
actual force request, measure the expected native invalidity, check complete
crossings and raw sample continuity, save/restart, and confirm the next exact
track and variant. Check disk rate and frame time. Verify the installed CSP
build's Physics API behavior; source tests alone do not establish that the
prototype affected FH26. Keep the original car and user settings unchanged.

If force application or readback fails, preserve the telemetry as baseline or
analysis-only evidence. Do not label the run a measured Plan C gain.
