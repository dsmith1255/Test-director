<#
.SYNOPSIS
  Temporarily opts a track into CSP Lua-app physics for the WIMNA AI diagnostic.
.DESCRIPTION
  Changes only content/tracks/<TrackId>/data/surfaces.ini. Enable requires the
  caller to supply the file's current SHA-256. An exact backup and audit manifest
  are stored outside the game. Restore refuses to overwrite subsequent edits.
  Stop Assetto Corsa before Enable or Restore. This is never a campaign setup step.
.EXAMPLE
  .\tools\Set-WimnaDiagnosticTrackOptIn.ps1 -Action Status -AcRoot 'D:\SteamLibrary\steamapps\common\assettocorsa' -TrackId spa
.EXAMPLE
  .\tools\Set-WimnaDiagnosticTrackOptIn.ps1 -Action Enable -AcRoot 'D:\SteamLibrary\steamapps\common\assettocorsa' -TrackId spa -ExpectedOriginalSha256 '<hash printed by Status>'
.EXAMPLE
  .\tools\Set-WimnaDiagnosticTrackOptIn.ps1 -Action Restore -AcRoot 'D:\SteamLibrary\steamapps\common\assettocorsa' -TrackId spa
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Status', 'Enable', 'Restore')]
    [string] $Action,

    [Parameter(Mandatory = $true)]
    [string] $AcRoot,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9_-]+$')]
    [string] $TrackId,

    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string] $ExpectedOriginalSha256,

    [string] $BackupRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'WIMNA Test Director\diagnostic-track-optin')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-Sha256([string] $Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Write-AtomicBytes([string] $Path, [byte[]] $Bytes, [switch] $Replace) {
    $directory = Split-Path -Parent $Path
    $temporary = Join-Path $directory ('.wimna-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $rollback = Join-Path $directory ('.wimna-' + [Guid]::NewGuid().ToString('N') + '.rollback')
    try {
        [System.IO.File]::WriteAllBytes($temporary, $Bytes)
        if ($Replace) {
            [System.IO.File]::Replace($temporary, $Path, $rollback)
            [System.IO.File]::Delete($rollback)
        } else {
            [System.IO.File]::Move($temporary, $Path)
        }
    } finally {
        if ([System.IO.File]::Exists($temporary)) {
            [System.IO.File]::Delete($temporary)
        }
    }
}

function Set-IniKey([string] $Text, [string] $Section, [string] $Key, [string] $Value, [string] $Newline, [bool] $CanCreateSection) {
    $headers = [regex]::Matches($Text, '(?m)^[ \t]*\[([A-Za-z0-9_]+)\][ \t]*(?:;[^\r\n]*)?\r?$')
    $matching = @($headers | Where-Object { $_.Groups[1].Value -ieq $Section })
    if ($matching.Count -gt 1) { throw "Duplicate [$Section] sections; refusing ambiguous edit." }
    if ($matching.Count -eq 0) {
        if (-not $CanCreateSection) { throw "Missing required [$Section] section." }
        if ($Text.Length -gt 0 -and -not ($Text.EndsWith("`n") -or $Text.EndsWith("`r"))) { $Text += $Newline }
        return $Text + "[$Section]$Newline$Key=$Value$Newline"
    }

    $header = $matching[0]
    $sectionEnd = $Text.Length
    foreach ($candidate in $headers) {
        if ($candidate.Index -gt $header.Index) { $sectionEnd = $candidate.Index; break }
    }
    $sectionText = $Text.Substring($header.Index, $sectionEnd - $header.Index)
    $keyPattern = '(?im)^[ \t]*' + [regex]::Escape($Key) + '[ \t]*=[^\r\n]*'
    $keyMatches = [regex]::Matches($sectionText, $keyPattern)
    if ($keyMatches.Count -gt 1) { throw "Duplicate $Key keys in [$Section]; refusing ambiguous edit." }
    if ($keyMatches.Count -eq 1) {
        $keyMatch = $keyMatches[0]
        $sectionText = $sectionText.Substring(0, $keyMatch.Index) + "$Key=$Value" + $sectionText.Substring($keyMatch.Index + $keyMatch.Length)
    } else {
        $headerLineEnd = $sectionText.IndexOf("`n")
        if ($headerLineEnd -ge 0) {
            $insertAt = $headerLineEnd + 1
            $sectionText = $sectionText.Substring(0, $insertAt) + "$Key=$Value$Newline" + $sectionText.Substring($insertAt)
        } else {
            $sectionText += "$Newline$Key=$Value$Newline"
        }
    }
    return $Text.Substring(0, $header.Index) + $sectionText + $Text.Substring($sectionEnd)
}

$resolvedRoot = (Resolve-Path -LiteralPath $AcRoot).ProviderPath.TrimEnd('\', '/')
$rootFull = [System.IO.Path]::GetFullPath($resolvedRoot)
$target = [System.IO.Path]::GetFullPath((Join-Path $rootFull "content\tracks\$TrackId\data\surfaces.ini"))
$requiredPrefix = [System.IO.Path]::GetFullPath((Join-Path $rootFull 'content\tracks')) + [System.IO.Path]::DirectorySeparatorChar
if (-not $target.StartsWith($requiredPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Target escaped the installed tracks directory.' }
if (-not [System.IO.File]::Exists($target)) { throw "Target not found: $target" }

# Reject redirected track directories/files so the displayed target is the actual target.
$probe = Split-Path -Parent $target
while ($probe.Length -ge $rootFull.Length -and $probe.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
    if (([System.IO.File]::GetAttributes($probe) -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Reparse point in track path: $probe"
    }
    if ($probe.Equals($rootFull, [StringComparison]::OrdinalIgnoreCase)) { break }
    $probe = Split-Path -Parent $probe
}
if (([System.IO.File]::GetAttributes($target) -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Reparse point target: $target" }

$backupFull = [System.IO.Path]::GetFullPath($BackupRoot)
if ($backupFull.StartsWith($rootFull + [System.IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'BackupRoot must be outside Assetto Corsa.'
}
$pathBytes = [System.Text.Encoding]::UTF8.GetBytes($target.ToLowerInvariant())
$hasher = [System.Security.Cryptography.SHA256]::Create()
try { $targetKey = ([BitConverter]::ToString($hasher.ComputeHash($pathBytes))).Replace('-', '').ToLowerInvariant().Substring(0, 16) }
finally { $hasher.Dispose() }
$backupDirectory = Join-Path (Join-Path $backupFull $TrackId) $targetKey
$backup = Join-Path $backupDirectory 'surfaces.ini.original'
$manifestPath = Join-Path $backupDirectory 'manifest.json'
$actualHash = Get-Sha256 $target

if ($Action -eq 'Status') {
    $manifest = $null
    if ([System.IO.File]::Exists($manifestPath)) { $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json }
    [pscustomobject]@{
        track_id = $TrackId
        target = $target
        current_sha256 = $actualHash
        backup = $(if ($manifest) { $backup } else { $null })
        original_sha256 = $(if ($manifest) { $manifest.original_sha256 } else { $null })
        patched_sha256 = $(if ($manifest) { $manifest.patched_sha256 } else { $null })
        state = $(if (-not $manifest) { 'unmanaged' } elseif ($actualHash -eq $manifest.patched_sha256) { 'diagnostic_patch_present' } elseif ($actualHash -eq $manifest.original_sha256) { 'original_present' } else { 'changed_since_patch' })
    } | Format-List
    return
}

if (Get-Process -Name acs -ErrorAction SilentlyContinue) { throw 'Assetto Corsa is running. Exit the simulator before changing track data.' }

if ($Action -eq 'Enable') {
    if (-not $ExpectedOriginalSha256) { throw 'Enable requires -ExpectedOriginalSha256 from a fresh Status check.' }
    if ($actualHash -ne $ExpectedOriginalSha256.ToLowerInvariant()) { throw "Original hash mismatch: expected $ExpectedOriginalSha256, actual $actualHash" }
    $backupExists = [System.IO.File]::Exists($backup)
    $manifestExists = [System.IO.File]::Exists($manifestPath)
    if ($backupExists -ne $manifestExists) {
        throw "Incomplete backup at $backupDirectory. Inspect it before another edit."
    }
    $saved = $null
    if ($backupExists) {
        $saved = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        if ($saved.format -ne 1 -or $saved.target -cne $target -or $saved.backup -cne $backup -or $saved.track_id -cne $TrackId -or $saved.original_sha256 -ne $actualHash -or (Get-Sha256 $backup) -ne $actualHash) {
            throw 'Existing backup does not match the exact original file and target.'
        }
    }

    $originalBytes = [System.IO.File]::ReadAllBytes($target)
    if ($originalBytes.Length -ge 2 -and (($originalBytes[0] -eq 255 -and $originalBytes[1] -eq 254) -or ($originalBytes[0] -eq 254 -and $originalBytes[1] -eq 255))) {
        throw 'UTF-16 surfaces.ini is unsupported; no file was changed.'
    }
    $text = [System.Text.Encoding]::GetEncoding(28591).GetString($originalBytes)
    if ($text.Contains([char]0)) { throw 'Binary surfaces.ini is unsupported; no file was changed.' }
    $newline = $(if ($text.Contains("`r`n")) { "`r`n" } else { "`n" })
    $patched = Set-IniKey $text 'SURFACE_0' 'WAV_PITCH' 'extended-0' $newline $false
    $patched = Set-IniKey $patched '_SCRIPTING_PHYSICS' 'ALLOW_APPS' '1' $newline $true
    if ($patched -ceq $text) { throw 'Track already has the diagnostic settings; no edit is needed.' }
    $patchedBytes = [System.Text.Encoding]::GetEncoding(28591).GetBytes($patched)
    $patchedHasher = [System.Security.Cryptography.SHA256]::Create()
    try { $patchedHash = ([BitConverter]::ToString($patchedHasher.ComputeHash($patchedBytes))).Replace('-', '').ToLowerInvariant() }
    finally { $patchedHasher.Dispose() }
    $patchedTemporary = Join-Path (Split-Path -Parent $target) ('.wimna-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    if ($saved) {
        if ($saved.patched_sha256 -ne $patchedHash) { throw 'Existing manifest patch hash differs from the new patch; refusing to reuse backup.' }
    } else {
        [System.IO.Directory]::CreateDirectory($backupDirectory) | Out-Null
        Write-AtomicBytes $backup $originalBytes
        if ((Get-Sha256 $backup) -ne $actualHash) { throw 'Backup verification failed; target was not changed.' }
        $manifest = [ordered]@{
            format = 1
            purpose = 'WIMNA AI thermal diagnostic only; not for campaign laps'
            track_id = $TrackId
            target = $target
            backup = $backup
            original_sha256 = $actualHash
            patched_sha256 = $patchedHash
            created_utc = [DateTime]::UtcNow.ToString('o')
            settings = @('_SCRIPTING_PHYSICS/ALLOW_APPS=1', 'SURFACE_0/WAV_PITCH=extended-0')
        }
        $manifestBytes = [System.Text.Encoding]::UTF8.GetBytes(($manifest | ConvertTo-Json -Depth 4))
        Write-AtomicBytes $manifestPath $manifestBytes
    }
    [System.IO.File]::WriteAllBytes($patchedTemporary, $patchedBytes)
    try {
        if ((Get-Sha256 $target) -ne $actualHash) { throw 'Target changed during preparation; refusing edit.' }
        $rollbackTemporary = Join-Path (Split-Path -Parent $target) ('.wimna-' + [Guid]::NewGuid().ToString('N') + '.rollback')
        [System.IO.File]::Replace($patchedTemporary, $target, $rollbackTemporary)
        [System.IO.File]::Delete($rollbackTemporary)
    } finally {
        if ([System.IO.File]::Exists($patchedTemporary)) { [System.IO.File]::Delete($patchedTemporary) }
    }
    if ((Get-Sha256 $target) -ne $patchedHash) { throw 'Post-write hash mismatch; inspect target and backup before using the track.' }
    Write-Output "Diagnostic opt-in enabled for $target"
    Write-Output "Original SHA-256: $actualHash"
    Write-Output "Patched SHA-256:  $patchedHash"
    Write-Output "Restore with the same script and -Action Restore before any campaign driving."
    return
}

if (-not [System.IO.File]::Exists($manifestPath) -or -not [System.IO.File]::Exists($backup)) {
    throw "Restore requires both manifest and backup at $backupDirectory"
}
$saved = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($saved.format -ne 1 -or $saved.target -cne $target -or $saved.backup -cne $backup -or $saved.track_id -cne $TrackId) {
    throw 'Backup manifest does not match the exact target.'
}
if ((Get-Sha256 $backup) -ne $saved.original_sha256) { throw 'Backup hash mismatch; refusing restore.' }
if ($actualHash -eq $saved.original_sha256) {
    Write-Output "Original already present at $target; backup retained at $backup"
    return
}
if ($actualHash -ne $saved.patched_sha256) { throw 'Target changed after diagnostic patch; refusing to overwrite it.' }
$restoreBytes = [System.IO.File]::ReadAllBytes($backup)
Write-AtomicBytes $target $restoreBytes -Replace
if ((Get-Sha256 $target) -ne $saved.original_sha256) { throw 'Restored file hash mismatch; inspect target immediately.' }
Write-Output "Original restored byte-for-byte at $target"
Write-Output "SHA-256: $($saved.original_sha256)"
Write-Output "Backup retained for audit at $backup"
