# Temporary track opt-in for the AI thermal diagnostic

WIMNA 0.3.4 enabled and disabled native AI on stock, unmodified Spa during the local smoke test. The track edit below is unnecessary for that verified installation and should not be used as routine setup.

This was a reversible track-data experiment for an offline AI thermal check, not part of the four-track driver campaign. It did **not** enable AI control in the local Spa test with WIMNA 0.3.3, so do not repeat it as a routine setup step. The helper changes `content/tracks/<track-id>/data/surfaces.ini` and no other game file. It adds or sets `[_SCRIPTING_PHYSICS] ALLOW_APPS=1` and `[SURFACE_0] WAV_PITCH=extended-0`. The AI mode still needs its own `ALLOW_PHYSICS_ALTERATIONS=1` and `FORCE_EXTENDED_PHYSICS=1` settings. These changes may alter the simulation; do not compare diagnostic laps directly with ordinary campaign laps.

Close Assetto Corsa first. In PowerShell, from the repository root, use the **actual** local installation root and track ID:

```powershell
$acRoot = 'D:\SteamLibrary\steamapps\common\assettocorsa'
& .\tools\Set-WimnaDiagnosticTrackOptIn.ps1 -Action Status -AcRoot $acRoot -TrackId spa
```

Inspect the printed absolute `target`, and copy the current SHA-256. The script supports only a track-level `data/surfaces.ini` path; it refuses missing or redirected track files. If the printed path is not the track and layout you intend to use, stop. Enable only with that exact hash:

```powershell
& .\tools\Set-WimnaDiagnosticTrackOptIn.ps1 -Action Enable -AcRoot $acRoot -TrackId spa -ExpectedOriginalSha256 '<current_sha256 from Status>'
```

The helper makes an exact backup and manifest under `%LOCALAPPDATA%\WIMNA Test Director\diagnostic-track-optin\spa\...` before replacing the track file atomically. It prints original and patched hashes. Keep that backup. The helper refuses to write while `acs.exe` is running, if the input hash changed, or if an existing backup does not match the exact original and target. A later diagnostic can reuse a verified backup after restoration. It rejects ambiguous duplicate INI sections or keys.

Run one short offline diagnostic in **WIMNA AI Thermal** mode only when investigating a specific permission result. Treat AI takeover as proven only if the overlay and raw run data show `car.isAIControlled=true`; movement or a command acknowledgement alone is insufficient. WIMNA 0.3.3 reported a failed *combined* capability check, which did not identify the value of `physics.allowed()` separately. The 0.3.4 probe records the gates independently. Do not try to solve a failed gate with repeated timed driving laps.

After exiting the simulator, restore the exact original bytes:

```powershell
& .\tools\Set-WimnaDiagnosticTrackOptIn.ps1 -Action Restore -AcRoot $acRoot -TrackId spa
& .\tools\Set-WimnaDiagnosticTrackOptIn.ps1 -Action Status -AcRoot $acRoot -TrackId spa
```

Restore verifies that the backup hash matches the original and that the live file still matches the helper's patched hash. If another tool or person changed the file meanwhile, it refuses to overwrite those edits; inspect them before manual recovery. `Status` should report `original_present` and the original SHA-256. The backup and manifest remain as a local audit trail. Confirm restoration before any normal Spa campaign laps. An AI diagnostic lap never satisfies a campaign lap requirement.

The local 0.3.3 Spa test applied this helper, observed zero AI-controlled samples, then restored the original `surfaces.ini` byte-for-byte (SHA-256 `c5b4af8a1d822e23da3d81a6f33a28b76027cae4754771e54b14837740c430cd`). The current Spa track file is unmodified. This helper is for a local, readable track file only. It does not bypass protected track data, add AI fast lanes, guarantee CSP Physics API access, or verify native AI control on Tyler's separate installation. The released Content Manager archive does not include a modified track.
