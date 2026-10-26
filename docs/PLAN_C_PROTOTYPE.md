# Plan C simulator prototype and overnight pilot

**Live status, October 25, 2026:** The generated Spa baseline preset was
launched locally after Content Manager mode discovery was allowed to finish.
The native session selected `wimna-ai-baseline`; the WIMNA app automatically
started AI control and recording at about 20 samples/s. After two crossings,
an exact-ID guarded manual save/shutdown request received
`save_complete_shutdown_requested`; `current_state` ended with `write_ok=true`,
the final chunk `000269.jsonl` recorded `ui_end` with reason
`overnight_switch` and AI retained, and the AC process exited. The first
crossing was an out-lap flagged for pit/cut; the second was a complete
112.410 s AI lap, still native-invalid with one cut. This verifies local
baseline startup and guarded save/shutdown, not a comparable clean lap.
The Spa Plan C run was then saved after the same two-crossing pattern. Its
worker reported more than 80,000 force-call pairs over the run, but its
observed physical effect remains unverified. A second baseline run was also
saved with a complete 112.264 s AI lap, native-invalid with one cut; its
force-call counter stayed at zero, recorder ran at about 19.97 Hz with zero
reported drops, and guarded save/shutdown completed. All three raw Spa runs
are summarized below. Their offline import/export passed. A separate
**supervised automatic two-session Spa queue** subsequently launched baseline,
saved and exited, then launched Plan C, saved and exited. It took 471.56 s,
recorded two crossings and zero reported drops in each session, and used
distinct physical session IDs. Its data imported and exported successfully,
while clean matched comparison was withheld. Two further supervised short
queues saved and switched Spa → Monza and Imola → Silverstone GP, each with
one out-lap crossing and zero reported drops per session. All four selected
layouts have now launched locally. An eight-hour unattended run remains pending.
Two earlier direct Content Manager launches selected plain Practice or Quick
Race while its custom-mode list was stale or loading. Do not launch a generated
custom-mode preset directly on a fresh CM process without checking the mode.

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

## First supervised Spa runs

These are separate local FH26/Spa/default sessions: baseline runs
`20261026030103-00527736` and `20261026031105-00553153`, and Plan C run
`20261026030624-00536345`. Each supplied one complete AI lap after a pit
out-lap. The table uses native completed-lap times and the raw traces at about
20 Hz. **All three complete laps were native-invalid with one cut each.** They
are diagnostic traces, not accepted clean pace observations or a demonstrated
Plan C gain.

| Check | Baseline | Plan C prototype | Evidence limit |
| --- | ---: | ---: | --- |
| Complete AI lap | 112.410 s; repeat 112.264 s | 112.672 s | Baselines differ by 0.146 s. Plan C was 0.262 s slower than the first and 0.408 s slower than the repeat; three invalid laps cannot establish significance. |
| Full-lap starting state | C4; 47.911/47.898 L fuel; SOC 0.88076/0.88013 | C4; 47.895 L fuel; SOC 0.88047 | Air 26 °C, track 29 °C, grip 1, fuel map 2, MGU-K delivery 3/recovery 5 and AI level/aggression 1 matched in readback. Setup/profile hashes were unavailable. |
| Full-lap endpoints | 45.720/45.711 L fuel; SOC 0.88483/0.88269 | 45.707 L fuel; SOC 0.88301 | Native SOC endpoints differ; do not assume equal energy state. |
| Speed at matched distance | Peak 313.61/315.30 km/h | Peak 313.97 km/h | At 25 m intervals from 50–6900 m, Plan C speed averaged 0.58/1.00 km/h lower than the two baselines. Individual peaks do not establish a straight-line effect. |
| Acceleration proxy | First baseline: 44 shared 100 m high-throttle, low-brake windows | Plan C higher in 22 of 44 windows | This compares Plan C with baseline one only. Windows overlap and are not independent repeats; no consistent acceleration advantage is visible. |
| Prototype worker | No WIMNA force requested; repeat baseline worker counter remained zero | 37,667 additional force-call pairs during the full lap; worker-calculated downforce 7.8–134.6 N and drag 3.1–53.8 N | Calls and calculated forces confirm the worker issued requests, not that the car received the assumed force or that a pace change was caused by it. |

Against the first baseline, interpolating elapsed simulation time at the same
track distances puts Plan C about 0.02 s behind at 2 km, 0.09 s at 4 km, and
0.21 s at 6 km. The distance-aligned finish difference is about 0.26 s.
These values describe one AI line and one intervention setting; the location
of each cut and small differences in tire, fuel and battery state also limit
interpretation.

The three saved runs passed an offline import/export check: 780 chunks and
14,927 records ingested without errors, a repeat ingest added zero, and the
41-file report covers three measured runs. The report withheld a matched
Plan C result under its quality rules because every complete lap had a cut
and the first baseline lacked newer worker telemetry. The Update6 workbook
was not part of this check. Raw runs and the bundle remain private.

## Proposed eight-session queue

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

## Local live runner pilot

The portable companion can prepare the eight local four-track presets, write
the eight-session queue, and run the queue without a Python installation on
the target computer. Its commands are explicit and do not run when the GUI is
opened normally. In PowerShell, set the path variables to the discovered
installation and a writable folder outside Assetto Corsa, then use:

```powershell
& $companionExe --generate-presets $sourcePreset $acRoot --output $presetDir
& $companionExe --prepare-overnight --campaign --ac-root $acRoot `
  --cm-exe $cmExe --preset-dir $presetDir --bridge-root $bridgeRoot `
  --race-ini $raceIni --car-version $observedCarVersion --output $campaignDir
```

`$sourcePreset` is an existing local CM Quick Drive preset used only as a
schema source; the generator does not alter it. The generated queue is
unverified and cannot launch. After a fresh live preflight on that exact
installation and review of `runner.json`, the separate start command is:

```powershell
& $companionExe --overnight-runner --config "$campaignDir\runner.json" --activate-local-live
```

The windowed companion writes command output and failures to
`Documents/WIMNA Test Director/logs/companion-command.log`. The queue
checkpoint remains the progress record. Short supervised queues checked the
selected track layouts and two cross-track switches. The eight-session hourly
sequence has not run unattended; keep it unarmed until an explicit start and
current preflight.

`wimna.overnight_prepare` prepares a short two-session Spa baseline → Plan C
pilot from the **currently installed** car, WIMNA mode files, CM executable,
and generated Spa presets. It writes exact file hashes, a queue, two variant
manifests, a runner config, and a preflight file. Preparing files never starts
Content Manager or Assetto Corsa. Supply the actual paths on the local machine;
the reported Tyler installation path is not a default for Dixon's computer.

```powershell
python -m wimna.overnight_prepare --ac-root $acRoot --cm-exe $cmExe `
  --preset-dir $presetDir --bridge-root $bridgeRoot --race-ini $raceIni `
  --car-version $observedCarVersion --output $pilotDir
```

The generated `preflight.json` deliberately has `status: "unverified"` and all
live checks false. Review the exact queue, requested conditions, file hashes,
and configuration. Set `status: "verified_live"`, the current UTC
`verified_at_utc`, and the five smoke-test fields to true **only** when the
two-stage CM URI load/Go, recorder readback, save, process exit, and mode
readback were actually checked on this installation. The launcher also checks
that the queue hash, CM binary hash, and CM version still match, and that the
preflight is less than 24 hours old. A source fixture or a successful command
acknowledgment does not satisfy these checks.

The explicit command to start the supervised pilot is:

```powershell
python -m wimna.overnight_live run --config "$pilotDir\runner.json" --activate-local-live
```

The runner requires the simulator process to be stopped first. For each
session it loads the exact preset with `loadPreset=1`, waits three seconds for
CM custom-mode discovery, sends a separate `go=1` URI with the same exact
`presetFile`, then requires a newly written, stable `race.ini` with the exact
mode, car, track, and layout. The
bridge must independently show the matching fresh run, AI control, recording,
sample progress, and lap crossings. At the switch it requests exact-ID save
and shutdown and waits for complete chunks, the app acknowledgment, and
verified AC process exit before loading another preset. The checkpoint is
`Documents/WIMNA Test Director/overnight.checkpoint.json` by default, beside
the bridge, so local analysis can bind saved runs to the queue.

Any wrong mode, missing recorder, stalled data, failed save, or uncertain
process exit stops further launches. Where the exact active AI/recorder state
is still verified, the runner tries guarded save/shutdown. Otherwise its
checkpoint says `manual_action_required`; a person must stop and inspect the
game. A stopped or interrupted checkpoint never implicitly resumes.

The first live queue attempt stopped when Content Manager returned “Settings
not specified” for a Go URI without the exact preset path. The corrected
two-stage launch includes `presetFile` in both URI requests. In a new
supervised pilot, the runner automatically completed the Spa baseline → Spa
Plan C sequence in **471.56 s**. Both sessions recorded two crossings, zero
reported dropped samples, and about 19.97 Hz effective sampling; fresh
physical session IDs, mode/car/track/layout, AI and recorder readbacks,
completed chunks, save acknowledgments, and process exit were checked. This
verifies one same-track transition under supervision. It does not establish
unattended overnight safety.

Two short supervised cross-track queues then completed: Spa/default baseline →
Monza/default Plan C in **237.62 s**, and Imola/default baseline →
`ks_silverstone/gp` Plan C in **230.69 s**. Each session had one out-lap
crossing, zero reported drops, a distinct physical session ID, exact
track/layout/mode and AI/recorder readbacks, completed chunks, a save
acknowledgment and verified simulator exit. These are integration checks,
not clean pace evidence or a full eight-session run.

The two pilot runs passed offline import/export: **470 chunks and 8,965
records** imported with zero errors; a second import added nothing. The
**32-file** report contains 8,937 measured samples, 26 events and two
capability records. It withheld a matched Plan C pair because complete
zero-cut AI laps were unavailable and preset, variant-hash and energy-profile
identity readbacks were missing. These missing fields are comparison limits,
not grounds to discard raw telemetry. The pilot data and export are local and
private; the Update6 workbook was not used for this check.

The proposed eight-session, four-track queue has completed only with
**synthetic** readbacks. Short supervised live pilots verified same-track and
two cross-track switches. The local builder generated eight baseline/Plan C presets
for `spa`, `monza`, `imola`, and `ks_silverstone/gp`, with matched requested
26 °C ambient air and 29 °C road temperature. Their manifest and SHA-256
hashes are under ignored `build/cm-presets/`; they are not part of the CM ZIP,
and requested temperature is not an observed simulator temperature. All four
selected layout launches and guarded save paths passed short local live checks.
The hourly Codex monitor remains paused until the user starts the full queue.
Do not use the fixture queue or short pilots to claim unattended track
switching.

## Prepare the four-track overnight queue

The source preparation command writes the eight-session order shown above.
Build the matched presets from a local Quick Drive source preset, then prepare
the queue against the **installed** FH26 car, WIMNA app/modes, Content Manager,
weather and exact track layouts. Use paths discovered on the target machine.
This reads game files and writes local manifests; it does not start either
Content Manager or Assetto Corsa.

```powershell
python -m wimna.cm_presets $sourcePreset $acRoot --output $presetDir
python -m wimna.overnight_prepare --campaign `
  --ac-root $acRoot --cm-exe $cmExe --preset-dir $presetDir `
  --bridge-root $bridgeRoot --race-ini $raceIni `
  --car-version $observedCarVersion --output $campaignDir `
  --expected-tire C4 --expected-fuel-l 50 `
  --expected-energy-profile unverified-readback --max-dropped-samples 100
```

The output includes `four-track-eight-session.queue.json`, `runner.json`, two
variant manifests and `preflight.json`. The variant manifests hash the installed
car physics archive, WIMNA app and modes, selected weather, track identity and
surfaces files, plus the KN5 models referenced by the four exact layouts. No
paid car or track file is copied into the preparation output. If any installed
file changes after preparation, create a fresh output directory and prepare
again. An existing `overnight.checkpoint.json` is never overwritten; archive
the prior checkpoint before preparing a new queue.

All eight presets request the same 26 °C ambient and 29 °C road temperature,
clear weather, car and assists. The generated preset does not apply tire, fuel,
battery or RSS energy-profile state; those are requested comparison targets
that the recorder checks where readable. Varying heat, fuel or tire together
with Plan C would confound this first paired comparison. Separate paired
sweeps can test those inputs later.

The queue arms a switch after 3,600 recording seconds, then waits for a lap
crossing. It stops at a 3,960-second session bound if a crossing never arrives;
the hour is a switching cadence, not a lap acceptance rule. The default stop
threshold is 100 reported dropped samples per session, about 0.14% of an hour
at 20 Hz. Per-lap gaps and coverage remain visible in the report, and a
12-second stale-source watchdog still stops the runner. The generated
preflight is **unverified** with all live checks false. The full four-track
runner cannot start until its installation-specific live checks are recorded
and the user explicitly starts it. The short live pilots and synthetic
eight-session replay do not prove an unattended eight-hour operation.

For each track, compare the prototype and baseline AI pace after accounting
for starting fuel, tire age, battery state, environment, and session order.
Laps within one stint are correlated, so they are not counted as independent
replications. Tyler's 38 clean human laps anchor absolute baseline pace. Adding
an AI prototype-minus-baseline delta to that human anchor is an `ASSUMED`
transfer until a held-out human prototype run checks it. Do not add the case's
expected Plan C gain again.

## Release and live gates

Before an unattended run, verify package identity and observed force effect
independently. Short supervised switches have checked the selected track
layouts and close/save/new-session sequence. Check sustained disk rate and
frame time before a full overnight run.
The installed CSP build reported force calls, but that alone does not establish
that the prototype affected FH26. Keep the original car and user settings
unchanged. The local queue must fail closed on any missing readback before an
unattended transition.

If force application or readback fails, preserve the telemetry as baseline or
analysis-only evidence. Do not label the run a measured Plan C gain.
