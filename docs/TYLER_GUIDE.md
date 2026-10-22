# Tyler's WIMNA Test Director guide

**Version 0.4.1 — release candidate.** Start with a short FH26/Spa check before collecting long stints. Version 0.4.1 has passed software and saved-data checks, but its new recorder and safer AI stop have **not yet been tested in game**. The earlier 0.4.0 app recorded an AI-driven Spa stint on the development computer; Tyler's installation, tire changes, and AI-to-human pit handoff still need checking.

## Install once

You need Assetto Corsa with your FH26 car, Content Manager and CSP already installed. WIMNA does not include a car or track.

1. Download both **WIMNA-Test-Director-CM-0.4.1.zip** and **WIMNA-Test-Director-companion-0.4.1.zip** from the release. They serve different purposes.
2. Drag the **CM ZIP** into Content Manager. In the installation preview, confirm that it adds **WimnaTestDirector** and the two session modes **WIMNA Test** and **WIMNA AI Thermal** to your selected Assetto Corsa installation. Install those items. If Content Manager asks, enable **WIMNA Test Director** in its app list.
3. Extract the **companion ZIP** to a folder you can keep. Leave `wimna-director.exe` beside its `_internal` folder. Double-click the EXE before driving; it needs no Python installation or terminal. If it cannot find Assetto Corsa, use **Browse** to select the game's actual folder, then **Detect car and track IDs**. Do not assume another computer's installation path applies to yours.
4. Keep the companion open while driving. It should show a live recorder status after you start a run. In Assetto Corsa, open **WIMNA Test Director** from the in-game app bar if its window does not appear automatically.

If **WIMNA Test** does not appear in Content Manager, ordinary **Practice** with the in-game WIMNA app is the manual-driving fallback. Automatic AI takeover needs the **WIMNA AI Thermal** mode and a successful on-screen permission and control check.

## First FH26/Spa check

1. In Content Manager, choose the installed **FH26** car and the modern **Spa** layout. Choose the actual tire, fuel, weather and track conditions you want to record. Select **WIMNA AI Thermal** for AI collection, or **WIMNA Test** for human collection.
2. In the WIMNA window, enter **X** as the tire test label, the tire compound you selected, a condition label such as `baseline` or `hot`, and an optional setup/profile label. The labels are your notes; check the **Observed tire** and air/track readbacks separately.
3. For AI driving, press **Start AI Data Collection**. Wait for **AI control readback: ON** and confirm the car is being driven by Assetto Corsa's AI. If it stays OFF or is unavailable, do not count the lap as AI-driven. For manual driving, press **Start Collection**.
4. Drive a short stint. Check that the WIMNA window says **Recorder: ON**, that its sample count increases, and that the companion shows new records. The first crossing after Start is a partial out-lap. You can collect as many useful laps as needed; collection mode has no timer or lap quota.
5. End with **End Collection Run** and export a test result before planning longer stints. Ending recording does **not** brake the car or switch AI off. If AI is ON, let it continue driving and leave the Assetto Corsa session through the game menu. Use **Stop AI Driver at Rest** only when the car has come to a safe stop, its native speed reads **2 km/h or less**, and you are ready to steer; wait for **AI control readback: OFF** before taking over. If the car will not stop safely, exit the session with AI still in control.

## Collect X, Y and Z tire stints

Keep the car, track/layout, setup, assists, fuel and energy settings as consistent as practical. Record any deliberate change. The AI mode forces extended physics, so compare tires under the same mode and conditions where possible.

1. Start a collection run with tire label **X**, the requested compound and the actual condition label. Use AI or drive yourself. The app records the controller and native tire/environment observations with each stint.
2. After useful X laps, bring the car toward the pits. Press **Mark pit / control handoff** to save the transition; this button does not steer, brake, direct AI into the pits or change control. If AI is driving, wait for a safe stop with a human ready before using **Stop AI Driver at Rest**. Confirm the OFF readback, then take over and complete the pit entry. If no safe stop is available, finish the recording and exit instead of releasing AI at speed.
3. Press **End Stint / Box**. Change tires through Assetto Corsa's normal pit controls. Enter **Y** and the new requested compound, check the observed compound, then press **Start Next Stint**. Repeat for **Z** and any other heat condition. Pit travel and the tire change remain in the raw recording, but are kept separate from comparable flying laps.
4. Finish with **End Collection Run**. If AI is still ON, follow the at-rest stop or game-menu exit procedure above. A different weather session creates a new physical run; label it separately and prepare the tires and fuel again. Aim for at least one competitive human stint under recorded conditions so your pace can anchor comparisons with the more repeatable AI runs.

A native tire change or session reset can close a stint automatically; start a newly labeled stint after it. **Pause Collection** marks the experiment only: the simulator can keep driving and telemetry can keep recording.

## Save and share results

In the companion, click **Export Results** and choose a parent folder. It creates a dated **WIMNA-results-...** folder with the report, stint and lap tables, raw telemetry, model outputs and file hashes. Keep the folder intact when sharing it. The raw 0.4.1 telemetry is compressed as `.jsonl.gz`; the readable report and CSV tables are included too.

Your ongoing recordings and companion data live under **Documents/WIMNA Test Director**. Keep that folder as the master copy. You can close and reopen the companion and import saved chunks again without intentionally duplicating records. Restarting Assetto Corsa creates a **new physical run**: its tires, fuel, battery and temperatures must be checked again. The **Resume** button belongs to the optional guided campaign; it does not claim that a new game session continues the same physical tire stint.

## If something looks wrong

| What you see | What to do |
|---|---|
| WIMNA window missing | Check that the app is enabled in Content Manager, then open it from the Assetto Corsa app bar. |
| Custom WIMNA session missing | Use Practice plus the WIMNA app for manual collection. Record AI mode as unavailable until its installation is checked. |
| AI readback stays OFF or unavailable | Do not label the stint AI-driven. Continue manually or save the diagnostic and stop. |
| Recorder unavailable, sample count stuck, or companion sees no new records | End the run, keep the existing files, and check the selected Assetto Corsa folder and free space. Do a short recording check before trying again. |
| Requested and observed tires differ | Correct the pit selection or label. Keep both values in the record; do not silently relabel the stint. |
| Spin, contact, cut, unexpected setting or interruption | Use **Report Issue**. Keep the raw lap; the report can flag it without hiding the data. |

## What the results can establish

The earlier **0.4.0** local FH26/Spa test saved 5,042 AI-controlled samples at 19.86 Hz with no reported drops. Both laps were invalid, and no tire swap or human handoff was tested. **0.4.1 has not yet been live checked**, so verify its recording rate, disk use, AI end behavior, pit handoff, observed second compound, restart recovery and game performance on your computer before long sessions.

The custom thermal temperatures are **model calculations**, not readings from hidden FH26 components. Direct RSS electrical power, battery energy and native thermal derating were unavailable in the local test. Tire contrasts from matched AI stints are useful observations; applying them to human race pace remains an assumption until checked with human laps. The optional guided four-track program covers **Spa, Silverstone GP, Imola and modern Monza GP**; it is separate from open-ended collection and advances on accepted laps and confirmations, not a hard clock.
