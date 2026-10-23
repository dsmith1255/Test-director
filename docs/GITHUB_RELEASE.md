# GitHub handoff for WIMNA Test Director 0.4.2

This is a **release candidate**. Version 0.4.2 makes unrestricted recording the primary in-game workflow and shows raw lap crossings independently of optional guided acceptance. It passed source tests and packaging checks but has not yet been checked in Assetto Corsa. Version 0.4.0 recorded one local FH26/Spa AI stint; 0.4.1 introduced compact recording and safe AI end. Do not describe 0.4.2 as live verified on Tyler's installation.

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
| `WIMNA-Test-Director-CM-0.4.2.zip` | Drag into Content Manager; includes the Lua app and two modes. |
| `WIMNA-Test-Director-companion-0.4.2.zip` | Extract and run the portable Windows companion. |
| `WIMNA-Test-Director-source-0.4.2.zip` | Reproducible source handoff. GitHub source checkout also contains this material. |
| `release-0.4.2-hashes.json` | Checksums and build time. |
| `WIMNA-Synthetic-Sample-Results.zip` | Optional example; clearly label it **SYNTHETIC**. |

Do not attach the local `live_smoke_*` folders, `WIMNA-results-*` folders, the installed RSS archive, the case workbook, or personal installation paths.

## GitHub release text

**Title:** WIMNA Test Director 0.4.2 release candidate

**Summary:** Installs a CSP Lua data-collection app and two Practice-derived Content Manager modes. The separate Windows companion imports and exports labeled tire stints and assumption-labeled model results. AI and human controller readback are retained with simulator telemetry. Collection has no hard clock or lap quota.

**Changes in 0.4.2:** Start Recording and Start AI Recording are the primary actions and collect every lap crossing without a settings checklist or acceptance quota. The C/R/F/E/M/V protocol is clearly optional. The overlay separates raw crossings from guided acceptance, and the companion shows the crossing count. The 0.4.1 safe AI stop and compact recorder remain included.

**Verification:** 124 automated checks and a complete 24-block synthetic replay passed at packaging. The earlier 0.4.0 local FH26/Spa run saved 5,042 AI-controlled samples at 19.86 Hz with no reported gaps or drops; both laps were native invalid and there was no tire comparison. The 0.4.2 entry flow and crossing count, newer compact writer, AI end action, tire handoff, restart recovery, and Tyler's installation remain unverified live. Custom thermal temperatures are analysis-only; direct RSS electrical power and native component temperatures were unavailable in the local test.

**Install:** Download the CM ZIP and companion ZIP, then follow `docs/TYLER_GUIDE.md`. Link to that page in the release description.

Mark the GitHub release **Pre-release**. After a successful target-machine smoke test, update the delivery status and publish a new build or a clearly dated verification note; do not silently relabel this archive as fully verified.
