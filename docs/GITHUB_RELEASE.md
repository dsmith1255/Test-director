# GitHub handoff for WIMNA Test Director 0.5.0

This is an **experimental release candidate**. Version 0.5.0 adds paired AI baseline and Plan C aerodynamic-proxy modes, exact prototype parameter hashes, a matched-stint analysis, and an eight-session synthetic overnight-queue rehearsal. Two manually supervised local FH26/Spa baseline sessions and one Plan C session selected correctly, automatically started AI control and recording, and completed guarded save/shutdown. A supervised automatic Spa baseline → Plan C queue saved both runs; short Spa → Monza and Imola → Silverstone GP queues then verified cross-track launching and saving on all four installed layouts. Plan C worker force calls were reported; physical effect, an eight-hour unattended run and Tyler's installation have not been verified.

## Repository scope

The destination `dsmith1255/Test-director` is **public**. The source includes case values and assumptions, but no Update6 workbook, paid RSS car, track assets, or saved live telemetry. The team should confirm that public sharing of the case-specific material is permitted. No license has been selected; do not imply that an open-source license applies.

Commit the source, configuration templates, tests, app files, build scripts, `.github/workflows/checks.yml`, and documentation. `.gitignore` excludes build products, local databases, synthetic output folders, and the `live_smoke_*` folders. In particular, `live_smoke_bridge` contains a link to a local Documents recording and must stay outside Git. The dated local result bundles are private working data. Review `git status` before every commit.

The [Tyler guide](TYLER_GUIDE.md) is the user-facing installation and driving handoff. [Delivery status](DELIVERY_STATUS.md) distinguishes implemented features from live verification. The [integration guide](INTEGRATION.md) contains developer and target-machine checks.

## Source check before publishing

On a Windows build machine with Python 3.11:

```powershell
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -q
python -m wimna replay --output build/release-replay --config campaign.template.json
.\Build-Release.ps1
```

The test-only `lupa` dependency executes the Lua app tests; Tyler does not install it. `Build-Release.ps1` uses pinned PyInstaller on the build machine, tests the extracted companion, builds both installer archives and a synthetic four-track sample, and writes `dist/release-hashes.json`. GitHub Actions runs the source tests and four-track replay on pushes and pull requests. A green CI run does not replace the FH26/CSP live check.

Before attaching files, compare SHA-256 values in `dist/release-hashes.json` with the files being uploaded. Keep the versioned copies of these release assets together:

| Asset | Use |
|---|---|
| `WIMNA-Test-Director-CM-0.5.0.zip` | Drag into Content Manager; includes the Lua app and four modes. |
| `WIMNA-Test-Director-companion-0.5.0.zip` | Extract and run the portable Windows companion. |
| `WIMNA-Test-Director-source-0.5.0.zip` | Reproducible source handoff. GitHub source checkout also contains this material. |
| `release-hashes.json` | Checksums and build time for the identically hashed versioned archives. |
| `WIMNA-Synthetic-Sample-Results.zip` | Optional example; clearly label it **SYNTHETIC**. |

Do not attach the local `live_smoke_*` folders, `WIMNA-results-*` folders, the installed RSS archive, the case workbook, or personal installation paths.

The eight generated baseline/Plan C presets span Spa, modern Monza, Imola and Silverstone GP. Their local manifest and hashes are under ignored `build/cm-presets/`; the presets are not bundled into the CM ZIP or release assets. Rebuild and inspect them on a target installation before use. Their requested weather values are not proof of observed simulator conditions.

## GitHub release text

**Title:** WIMNA Test Director 0.5.0 experimental candidate

**Summary:** Installs a CSP Lua data-collection app and four Practice-derived Content Manager modes. Two new modes pair an unchanged FH26 baseline with a small, assumed Plan C aerodynamic-force proxy. The separate Windows companion imports saved stints and reports matched AI contrasts with their evidence limits. Collection has no hard clock or lap quota.

**Changes in 0.5.0:** The paired modes stamp exact parameter identities and can start AI collection after guarded readbacks. The companion inventories every lap and pairs stints only under recorded matching conditions; any transfer to human pace remains an assumption. The portable companion can generate target-local presets, prepare a hash-bound eight-session queue, and start it only through an explicit live command after preflight. Synthetic readbacks rehearsed all eight sessions. Short supervised queues verified same-track and two cross-track transitions with exact readbacks and completed save/exit. The full unattended queue remains unarmed.

**Verification:** Automated source checks, a complete 24-block synthetic campaign replay, and an eight-session synthetic overnight-queue rehearsal passed. The earlier 0.4.0 local FH26/Spa run saved 5,042 AI-controlled samples at 19.86 Hz with no reported gaps or drops; both laps were native invalid. On October 25, three 0.5.0 Spa sessions selected their modes, automatically started AI control and recording at about 20 samples/s, and completed supervised guarded save/shutdown. Complete AI laps were 112.410 and 112.264 s baseline, and 112.672 s Plan C; all native-invalid with one cut. The repeat baseline worker counter stayed at zero; the Plan C worker reported 81,000 force-call pairs over its run. Offline ingestion accepted 780 chunks and 14,927 records without errors; a second pass added zero. The 41-file export withheld a matched Plan C result. A separate short automatic Spa baseline → Plan C queue completed in 471.56 s with two crossings, zero reported drops and a distinct physical session ID for each run. Its 470 chunks and 8,965 records imported without errors or duplicates on reimport; the 32-file report contains 8,937 samples, 26 events and two capability records. It withheld matched comparison because full laps had cuts and preset/profile identity readbacks were missing. Two supervised one-crossing queues also completed Spa → Monza in 237.62 s and Imola → Silverstone GP in 230.69 s. All four sessions saved and exited with exact native layout/mode readbacks and zero reported drops. These out-lap-only runs check switching, not pace. An earlier queue attempt stopped on Content Manager's “Settings not specified” error; the corrected launcher sends the exact preset file in both launch stages. The Update6 workbook was not included. Physical effect and any pace benefit, the eight-hour unattended queue, interrupted-recorder recovery, frame impact, and Tyler's installation remain unverified. Custom cooling and component temperatures are analysis-only. The prototype does not edit or redistribute RSS car files.

**Install:** Download the CM ZIP and companion ZIP, then follow `docs/TYLER_GUIDE.md`. Link to that page in the release description.

Mark the GitHub release **Pre-release**. After a successful target-machine smoke test, update the delivery status and publish a new build or a clearly dated verification note; do not silently relabel this archive as fully verified.
