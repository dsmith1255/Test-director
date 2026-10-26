param(
    [switch]$SkipTests,
    [switch]$CheckDependenciesOnly,
    [string]$PythonPath,
    [string]$OfflineSitePackages,
    [string]$TclLibrary,
    [string]$TkLibrary
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$venvPython = Join-Path $projectRoot '.buildvenv\Scripts\python.exe'
$entry = Join-Path $projectRoot 'launch_gui.py'
$template = Join-Path $projectRoot 'campaign.template.json'
$appFolder = Join-Path $projectRoot 'ac_app\apps\lua\WimnaTestDirector'
$distFolder = Join-Path $projectRoot 'dist'

if ($OfflineSitePackages -and -not $PythonPath) {
    throw '-OfflineSitePackages requires -PythonPath.'
}
if ([bool]$TclLibrary -ne [bool]$TkLibrary) {
    throw 'Supply both -TclLibrary and -TkLibrary.'
}
if ($TclLibrary) {
    $env:TCL_LIBRARY = (Resolve-Path -LiteralPath $TclLibrary -ErrorAction Stop).Path
    $env:TK_LIBRARY = (Resolve-Path -LiteralPath $TkLibrary -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath (Join-Path $env:TCL_LIBRARY 'init.tcl') -PathType Leaf)) {
        throw "Tcl init.tcl not found in $env:TCL_LIBRARY"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $env:TK_LIBRARY 'tk.tcl') -PathType Leaf)) {
        throw "Tk tk.tcl not found in $env:TK_LIBRARY"
    }
}
if ($PythonPath) {
    $buildPython = (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $buildPython -PathType Leaf)) {
        throw "Python executable not found: $buildPython"
    }
    if ($OfflineSitePackages) {
        $sitePackages = (Resolve-Path -LiteralPath $OfflineSitePackages -ErrorAction Stop).Path
        if (-not (Test-Path -LiteralPath $sitePackages -PathType Container)) {
            throw "Site-packages directory not found: $sitePackages"
        }
        $env:PYTHONPATH = if ($env:PYTHONPATH) { "$sitePackages;$env:PYTHONPATH" } else { $sitePackages }
    }
} else {
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        py -3.11 -m venv (Join-Path $projectRoot '.buildvenv')
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 is unavailable. Supply -PythonPath.' }
    }
    $buildPython = $venvPython
}

& $buildPython -c 'import sys; print(sys.version)' 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Python cannot start: $buildPython. Supply a working -PythonPath."
}
$requirementsFile = Join-Path $projectRoot 'requirements-build.txt'
$pyInstallerRequirement = @(Get-Content -LiteralPath $requirementsFile | Where-Object { $_ -match '^\s*pyinstaller==[^\s#]+' })
if ($pyInstallerRequirement.Count -ne 1) { throw 'Expected one pinned PyInstaller requirement.' }
$requiredPyInstaller = $pyInstallerRequirement[0] -replace '^\s*pyinstaller==([^\s#]+).*$', '$1'
$installedPyInstaller = & $buildPython -c "import importlib.metadata as metadata; print(metadata.version('pyinstaller'))" 2>$null
$versionExitCode = $LASTEXITCODE
if ($versionExitCode -ne 0 -or [string]$installedPyInstaller -ne $requiredPyInstaller) {
    if ($PythonPath) {
        throw "PyInstaller $requiredPyInstaller is unavailable to $buildPython (detected: '$installedPyInstaller', exit: $versionExitCode). Supply -OfflineSitePackages with compatible installed packages."
    }
    & $buildPython -m pip install --disable-pip-version-check -r $requirementsFile
    if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed.' }
}
$tkPreflight = 'import tkinter; root = tkinter.Tk(); root.withdraw(); root.destroy()'
try {
    & $buildPython -c $tkPreflight 2>$null | Out-Null
    $tkExitCode = $LASTEXITCODE
} catch {
    throw "Python Tcl/Tk initialization failed. Supply valid -TclLibrary and -TkLibrary paths. $($_.Exception.Message)"
}
if ($tkExitCode -ne 0) {
    throw 'Python Tcl/Tk initialization failed. Supply valid -TclLibrary and -TkLibrary paths.'
}
if ($CheckDependenciesOnly) {
    Write-Output "Build dependencies OK: Python $buildPython; PyInstaller $requiredPyInstaller; Tcl/Tk initialized"
    return
}
if (-not $SkipTests) {
    & $buildPython -m unittest discover -s (Join-Path $projectRoot 'tests') -q
    if ($LASTEXITCODE -ne 0) { throw 'Test suite failed.' }
}
$staleOneFileExe = Join-Path $distFolder 'wimna-director.exe'
if (Test-Path -LiteralPath $staleOneFileExe -PathType Leaf) {
    Remove-Item -LiteralPath $staleOneFileExe -Force
}
& $buildPython -m PyInstaller --noconfirm --clean --onedir --windowed --name wimna-director `
    --distpath $distFolder --workpath (Join-Path $projectRoot 'build') `
    --specpath $projectRoot --paths $projectRoot `
    --add-data "$template;." --hidden-import wimna.analysis `
    --hidden-import wimna.collection_analysis --hidden-import wimna.collection_index `
    --hidden-import wimna.case_import --hidden-import wimna.models `
    --hidden-import wimna.cm_presets --hidden-import wimna.overnight_prepare `
    --hidden-import wimna.overnight_live `
    --hidden-import wimna.report $entry
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }

$bundleFolder = Join-Path $distFolder 'wimna-director'
$exe = Join-Path $bundleFolder 'wimna-director.exe'
$internalFolder = Join-Path $bundleFolder '_internal'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Companion executable missing.' }
if (-not (Test-Path -LiteralPath (Join-Path $internalFolder '_tcl_data\init.tcl') -PathType Leaf)) {
    throw 'Companion Tcl data missing.'
}
$frozenCheck = Start-Process -FilePath $exe -ArgumentList '--self-test' -Wait -PassThru -WindowStyle Hidden
if ($frozenCheck.ExitCode -ne 0) { throw 'Frozen companion import test failed.' }
$companionArchive = Join-Path $distFolder 'WIMNA-Test-Director-companion.zip'
if (Test-Path -LiteralPath $companionArchive -PathType Leaf) { Remove-Item -LiteralPath $companionArchive -Force }
Compress-Archive -Path $exe, $internalFolder -DestinationPath $companionArchive -CompressionLevel Optimal
$smokeFolder = [System.IO.Path]::GetFullPath((Join-Path $projectRoot 'build\companion-smoke'))
if (-not $smokeFolder.StartsWith($projectRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Companion smoke folder escaped the project root.'
}
if ((Get-Item -LiteralPath (Split-Path -Parent $smokeFolder) -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
    throw 'Companion smoke parent is a reparse point.'
}
if (Test-Path -LiteralPath $smokeFolder) {
    if ((Get-Item -LiteralPath $smokeFolder -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw 'Companion smoke folder is a reparse point.'
    }
}
if (Test-Path -LiteralPath $smokeFolder) { Remove-Item -LiteralPath $smokeFolder -Recurse -Force }
Expand-Archive -LiteralPath $companionArchive -DestinationPath $smokeFolder
$smokeExe = Join-Path $smokeFolder 'wimna-director.exe'
if (-not (Test-Path -LiteralPath (Join-Path $smokeFolder '_internal\_tcl_data\init.tcl') -PathType Leaf)) {
    throw 'Extracted companion Tcl data missing.'
}
$extractedImportCheck = Start-Process -FilePath $smokeExe -ArgumentList '--self-test' -Wait -PassThru -WindowStyle Hidden
if ($extractedImportCheck.ExitCode -ne 0) { throw 'Extracted companion import test failed.' }
$savedTclLibrary = $env:TCL_LIBRARY
$savedTkLibrary = $env:TK_LIBRARY
$savedGuiTestLog = $env:WIMNA_GUI_SELF_TEST_LOG
$guiTestLog = Join-Path $projectRoot 'build\frozen-gui-self-test.txt'
Remove-Item -LiteralPath $guiTestLog -ErrorAction SilentlyContinue
try {
    Remove-Item Env:TCL_LIBRARY, Env:TK_LIBRARY -ErrorAction SilentlyContinue
    $env:WIMNA_GUI_SELF_TEST_LOG = $guiTestLog
    $frozenGuiCheck = Start-Process -FilePath $smokeExe -ArgumentList '--gui-self-test' -Wait -PassThru -WindowStyle Hidden
} finally {
    if ($savedTclLibrary) { $env:TCL_LIBRARY = $savedTclLibrary }
    if ($savedTkLibrary) { $env:TK_LIBRARY = $savedTkLibrary }
    if ($savedGuiTestLog) { $env:WIMNA_GUI_SELF_TEST_LOG = $savedGuiTestLog } else { Remove-Item Env:WIMNA_GUI_SELF_TEST_LOG -ErrorAction SilentlyContinue }
}
if ($frozenGuiCheck.ExitCode -ne 0) {
    $guiDetail = if (Test-Path -LiteralPath $guiTestLog -PathType Leaf) { Get-Content -LiteralPath $guiTestLog -Raw } else { 'No Python traceback was captured.' }
    throw "Extracted companion Tcl/Tk test failed; tkinter or its libraries were not bundled. $guiDetail"
}
$staleAppExe = Join-Path $appFolder 'wimna-director.exe'
if (Test-Path -LiteralPath $staleAppExe -PathType Leaf) { Remove-Item -LiteralPath $staleAppExe -Force }
if (Get-ChildItem -LiteralPath $appFolder -Recurse -File | Where-Object { $_.Extension -in @('.exe', '.dll', '.pyd') }) {
    throw 'Content Manager package source contains a binary; keep the companion separate.'
}
& (Join-Path $projectRoot 'ac_app\Build-AppPackage.ps1')
if ($LASTEXITCODE -ne 0) { throw 'CM archive build failed.' }
& $buildPython (Join-Path $projectRoot 'package_source.py')
if ($LASTEXITCODE -ne 0) { throw 'Source archive build failed.' }

$sampleRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot 'build\sample-replay'))
if (-not $sampleRoot.StartsWith($projectRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Synthetic sample folder escaped the project root.'
}
$sampleParent = Split-Path -Parent $sampleRoot
if ((Get-Item -LiteralPath $sampleParent -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
    throw 'Synthetic sample parent is a reparse point.'
}
if (Test-Path -LiteralPath $sampleRoot) {
    if ((Get-Item -LiteralPath $sampleRoot -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw 'Synthetic sample folder is a reparse point.'
    }
}
$sampleArchive = Join-Path $distFolder 'WIMNA-Synthetic-Sample-Results.zip'
if (Test-Path -LiteralPath $sampleArchive -PathType Leaf) { Remove-Item -LiteralPath $sampleArchive -Force }
if (Test-Path -LiteralPath $sampleRoot) { Remove-Item -LiteralPath $sampleRoot -Recurse -Force }
Push-Location $projectRoot
try {
    & $buildPython -m wimna replay --output $sampleRoot --config $template
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic full-campaign replay failed.' }
} finally {
    Pop-Location
}
$sampleStatePath = Join-Path $sampleRoot 'scheduler_state.json'
$sampleSummariesPath = Join-Path $sampleRoot 'results\run_summaries.json'
if (-not (Test-Path -LiteralPath $sampleStatePath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $sampleSummariesPath -PathType Leaf)) {
    throw 'Synthetic replay did not produce a complete results bundle.'
}
$sampleState = Get-Content -LiteralPath $sampleStatePath -Raw | ConvertFrom-Json
$sampleSummaries = Get-Content -LiteralPath $sampleSummariesPath -Raw | ConvertFrom-Json
$expectedTracks = @('spa', 'silverstone_gp', 'imola', 'monza_gp')
$actualTracks = @($sampleSummaries | ForEach-Object { $_.track } | Sort-Object -Unique)
$unexpectedTracks = @($actualTracks | Where-Object { $_ -notin $expectedTracks })
$nonSyntheticRuns = @($sampleSummaries | Where-Object { $_.evidence -ne 'SYNTHETIC' })
$sampleChecks = [ordered]@{
    phase = [string]$sampleState.phase
    block_count = [int]$sampleState.block_results.Count
    run_count = [int]$sampleSummaries.Count
    tracks = @($actualTracks)
    unexpected_tracks = @($unexpectedTracks)
    non_synthetic_count = [int]$nonSyntheticRuns.Count
}
if ($sampleChecks.phase -ne 'complete' -or $sampleChecks.block_count -ne 24 -or
    $sampleChecks.run_count -ne 24 -or $sampleChecks.unexpected_tracks.Count -ne 0 -or
    $sampleChecks.tracks.Count -ne $expectedTracks.Count -or
    $sampleChecks.non_synthetic_count -ne 0) {
    throw "Synthetic replay failed the four-track, 24-block, synthetic-only release check: $($sampleChecks | ConvertTo-Json -Compress -Depth 3)"
}
Compress-Archive -Path (Join-Path $sampleRoot 'results\*') -DestinationPath $sampleArchive -CompressionLevel Optimal

$archive = Join-Path $distFolder 'WIMNA-Test-Director-CM.zip'
$hashes = @{
    companion_sha256 = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
    companion_archive_sha256 = (Get-FileHash -LiteralPath $companionArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    cm_archive_sha256 = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    source_archive_sha256 = (Get-FileHash -LiteralPath (Join-Path $distFolder 'WIMNA-Test-Director-source.zip') -Algorithm SHA256).Hash.ToLowerInvariant()
    sample_archive_sha256 = (Get-FileHash -LiteralPath $sampleArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    built_utc = [DateTime]::UtcNow.ToString('o')
    tests_skipped = [bool]$SkipTests
}
$hashes | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $distFolder 'release-hashes.json') -Encoding utf8
Write-Output "Release archive: $archive"
