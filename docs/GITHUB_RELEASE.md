# GitHub handoff for WIMNA Test Director 0.5.0

This is an **experimental release candidate**. Version 0.5.0 adds paired AI baseline and Plan C aerodynamic-proxy modes, exact prototype parameter hashes, a matched-stint analysis, and a synthetic overnight-queue rehearsal. The Plan C force and automatic track switching have not been checked live. The earlier 0.4.0 build recorded one local FH26/Spa AI stint; do not describe 0.5.0 as live verified on Tyler's installation.

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

## GitHub release text

**Title:** WIMNA Test Director 0.5.0 experimental candidate

**Summary:** Installs a CSP Lua data-collection app and four Practice-derived Content Manager modes. Two new modes pair an unchanged FH26 baseline with a small, assumed Plan C aerodynamic-force proxy. The separate Windows companion imports saved stints and reports matched AI contrasts with their evidence limits. Collection has no hard clock or lap quota.

**Changes in 0.5.0:** The paired modes stamp exact parameter identities and can start AI collection after guarded readbacks. The companion inventories every lap and pairs stints only under recorded matching conditions; any transfer to human pace remains an assumption. A local queue can rehearse eight hourly sessions with synthetic readbacks. Its live launch/close adapters are disabled until a two-session smoke test verifies them.

**Verification:** 157 automated checks, a complete 24-block synthetic campaign replay, and an eight-session synthetic overnight-queue rehearsal passed. The earlier 0.4.0 local FH26/Spa run saved 5,042 AI-controlled samples at 19.86 Hz with no reported gaps or drops; both laps were native invalid. The 0.5.0 force, automatic start, paired recordings, session switching, restart recovery, and Tyler's installation remain unverified live. Custom cooling and component temperatures are analysis-only. The prototype does not edit or redistribute RSS car files.

**Install:** Download the CM ZIP and companion ZIP, then follow `docs/TYLER_GUIDE.md`. Link to that page in the release description.

Mark the GitHub release **Pre-release**. After a successful target-machine smoke test, update the delivery status and publish a new build or a clearly dated verification note; do not silently relabel this archive as fully verified.
