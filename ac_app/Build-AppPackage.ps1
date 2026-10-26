param(
    [string]$OutputPath = (Join-Path $PSScriptRoot '..\dist\WIMNA-Test-Director-CM.zip')
)

$ErrorActionPreference = 'Stop'
$appRoot = $PSScriptRoot
$entry = Join-Path $appRoot 'apps\lua\WimnaTestDirector\WimnaTestDirector.lua'
$appManifest = Join-Path $appRoot 'apps\lua\WimnaTestDirector\manifest.ini'
$modeManifest = Join-Path $appRoot 'extension\lua\new-modes\wimna-test\manifest.ini'
$aiModeManifest = Join-Path $appRoot 'extension\lua\new-modes\wimna-ai-thermal\manifest.ini'
$baselineModeManifest = Join-Path $appRoot 'extension\lua\new-modes\wimna-ai-baseline\manifest.ini'
$planCModeManifest = Join-Path $appRoot 'extension\lua\new-modes\wimna-ai-plan-c\manifest.ini'
foreach ($required in @($entry, $appManifest, $modeManifest, $aiModeManifest, $baselineModeManifest, $planCModeManifest)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Package source missing: $required"
    }
}

$resolvedOutput = [System.IO.Path]::GetFullPath($OutputPath)
$resolvedRoot = [System.IO.Path]::GetFullPath($appRoot)
if ($resolvedOutput.StartsWith($resolvedRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Output archive must be outside ac_app so it cannot include itself.'
}

$destinationDir = Split-Path -Parent $resolvedOutput
New-Item -ItemType Directory -Force -Path $destinationDir | Out-Null
if (Test-Path -LiteralPath $resolvedOutput) {
    Remove-Item -LiteralPath $resolvedOutput -Force
}

# Archive root must be the Assetto Corsa root: apps/ and extension/ at top level.
Compress-Archive -Path (Join-Path $appRoot 'apps'), (Join-Path $appRoot 'extension') -DestinationPath $resolvedOutput -CompressionLevel Optimal
Write-Output $resolvedOutput
