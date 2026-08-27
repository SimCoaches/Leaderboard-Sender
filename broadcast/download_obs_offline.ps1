# Build-time helper: fetch the official OBS Studio installer into
# broadcast\vendor\ so LapTimeSenderSetup.exe can install OBS fully offline
# with a known, deterministic install location (C:\Program Files\obs-studio).
#
# Run this BEFORE compiling the installer whenever you want an offline bundle:
#   powershell -ExecutionPolicy Bypass -File download_obs_offline.ps1
#
# The vendor exe is gitignored (~160 MB); without it the installer still
# works, falling back to winget at install time. OBS Studio is GPLv2 -
# redistributing the unmodified official installer is permitted; the notice
# file written alongside points to the source.

$ErrorActionPreference = 'Stop'
$Version = '32.2.2'
$Url = "https://github.com/obsproject/obs-studio/releases/download/$Version/OBS-Studio-$Version-Windows-x64-Installer.exe"

$vendorDir = Join-Path $PSScriptRoot 'vendor'
New-Item -ItemType Directory -Force $vendorDir | Out-Null
$dest = Join-Path $vendorDir "OBS-Studio-$Version-Windows-x64-Installer.exe"

if (Test-Path $dest) {
    Write-Host "Already present: $dest"
} else {
    Write-Host "Downloading OBS Studio $Version (~160 MB) from the official GitHub release..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $Url -OutFile $dest
    Write-Host "Saved -> $dest ($([math]::Round((Get-Item $dest).Length / 1MB)) MB)"
}

$notice = @(
    "OBS Studio $Version - official unmodified installer, bundled for offline",
    "rig setup. OBS Studio is free software under the GNU GPL v2;",
    "source code: https://github.com/obsproject/obs-studio",
    "Project site: https://obsproject.com"
) -join "`r`n"
[System.IO.File]::WriteAllText((Join-Path $vendorDir 'OBS-LICENSE-NOTICE.txt'), $notice + "`r`n")
Write-Host "License notice written."
