# Broadcast screen setup for the SimRacing Leaderboard system.
#
# Sets up the 4th-screen "outside the car" camera view for Assetto Corsa:
#   1. Verifies OBS Studio is installed (offers winget install if not)
#   2. Downloads the official CSP OBS plugin (v1.0.0.1) from GitHub and
#      installs it into OBS's plugin folder
#   3. Installs the preconfigured "AC Broadcast" OBS scene collection
#      (chase camera source, fullscreen projector on the chosen monitor)
#   4. Prints the remaining one-time manual steps (CSP toggle, projector)
#
# Run as Administrator (needed to copy the plugin into Program Files):
#   powershell -ExecutionPolicy Bypass -File setup_broadcast.ps1
#
# Optional: -ProjectorMonitor <index> picks which monitor the broadcast view
# fills (0 = first). Default 3 = the 4th monitor.

param(
    [int]$ProjectorMonitor = 3
)

$ErrorActionPreference = 'Stop'
$PluginUrl = 'https://github.com/ac-custom-shaders-patch/acc-obs-plugin/releases/download/v1.0.0.1/acc-obs-plugin.zip'

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

# UTF-8 WITHOUT BOM: OBS's JSON/ini parsers choke on a BOM, and PowerShell
# 5.1's Out-File -Encoding utf8 always writes one.
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
function Write-TextFile([string]$path, [string]$content) {
    [System.IO.File]::WriteAllText($path, $content, $Utf8NoBom)
}

# --- 1. Locate OBS Studio ----------------------------------------------------
Step "Looking for OBS Studio"
$obsRoot = $null
foreach ($candidate in @("$env:ProgramFiles\obs-studio", "${env:ProgramFiles(x86)}\obs-studio")) {
    if (Test-Path "$candidate\bin\64bit\obs64.exe") { $obsRoot = $candidate; break }
}
if (-not $obsRoot) {
    $reg = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\OBS Studio' -ErrorAction SilentlyContinue
    if ($reg -and $reg.InstallLocation -and (Test-Path "$($reg.InstallLocation)\bin\64bit\obs64.exe")) {
        $obsRoot = $reg.InstallLocation
    }
}
if (-not $obsRoot) {
    Write-Host "OBS Studio is not installed."
    $answer = Read-Host "Install OBS Studio now via winget? (y/n)"
    if ($answer -eq 'y') {
        winget install --id OBSProject.OBSStudio --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { Fail "winget install failed - install OBS manually from https://obsproject.com and re-run this script." }
        $obsRoot = "$env:ProgramFiles\obs-studio"
        if (-not (Test-Path "$obsRoot\bin\64bit\obs64.exe")) { Fail "OBS still not found after install - re-run this script." }
    } else {
        Fail "Install OBS Studio from https://obsproject.com and re-run this script."
    }
}
Write-Host "Found OBS at: $obsRoot"

# --- 2. Install the CSP OBS plugin -------------------------------------------
Step "Installing CSP OBS plugin (official, from ac-custom-shaders-patch GitHub)"
$pluginDir = Join-Path $obsRoot 'obs-plugins\64bit'
if (-not (Test-Path $pluginDir)) { Fail "OBS plugin folder not found: $pluginDir" }

$tmp = Join-Path $env:TEMP 'acc-obs-plugin-setup'
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force -Confirm:$false }
New-Item -ItemType Directory -Force $tmp | Out-Null
$zipPath = Join-Path $tmp 'acc-obs-plugin.zip'
Invoke-WebRequest -Uri $PluginUrl -OutFile $zipPath
Expand-Archive -Path $zipPath -DestinationPath $tmp -Force
$dlls = Get-ChildItem $tmp -Recurse -Filter '*.dll'
if (-not $dlls) { Fail "No DLL found inside the plugin zip - the release layout may have changed." }
foreach ($dll in $dlls) {
    try {
        Copy-Item $dll.FullName $pluginDir -Force
    } catch {
        Fail "Could not copy $($dll.Name) into $pluginDir - run this script as Administrator."
    }
    Write-Host "Installed $($dll.Name) -> $pluginDir"
}

# --- 3. Install the OBS scene collection -------------------------------------
Step "Installing 'AC Broadcast' scene collection"
$sceneSrc = Join-Path $PSScriptRoot 'AC_Broadcast.json'
if (-not (Test-Path $sceneSrc)) { Fail "AC_Broadcast.json not found next to this script." }
$scenesDir = Join-Path $env:APPDATA 'obs-studio\basic\scenes'
New-Item -ItemType Directory -Force $scenesDir | Out-Null

# Point the saved fullscreen projector at the requested monitor
$sceneJson = Get-Content $sceneSrc -Raw
$sceneJson = $sceneJson -replace '"monitor":\s*\d+', ('"monitor": {0}' -f $ProjectorMonitor)
$sceneDest = Join-Path $scenesDir 'AC_Broadcast.json'
Write-TextFile $sceneDest $sceneJson
Write-Host "Installed scene collection -> $sceneDest (projector on monitor index $ProjectorMonitor)"

# Make it the active collection and keep projectors across restarts.
# OBS 31+ uses user.ini; older versions use global.ini. Update whichever exists
# (both if both exist); if OBS has never been launched, seed user.ini.
function Set-IniValue([string]$path, [string]$section, [string]$key, [string]$value) {
    $lines = @()
    if (Test-Path $path) { $lines = @(Get-Content $path) }
    $sectionIdx = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i].Trim() -eq "[$section]") { $sectionIdx = $i; break }
    }
    if ($sectionIdx -lt 0) {
        $lines += "[$section]"
        $lines += "$key=$value"
    } else {
        $end = $lines.Count
        for ($i = $sectionIdx + 1; $i -lt $lines.Count; $i++) {
            if ($lines[$i].Trim() -match '^\[.*\]$') { $end = $i; break }
        }
        $found = $false
        for ($i = $sectionIdx + 1; $i -lt $end; $i++) {
            if ($lines[$i] -match "^\s*$([regex]::Escape($key))\s*=") {
                $lines[$i] = "$key=$value"; $found = $true; break
            }
        }
        if (-not $found) {
            $lines = $lines[0..($end - 1)] + "$key=$value" + $(if ($end -lt $lines.Count) { $lines[$end..($lines.Count - 1)] } else { @() })
        }
    }
    Write-TextFile $path (($lines -join "`r`n") + "`r`n")
}

$obsCfgDir = Join-Path $env:APPDATA 'obs-studio'
New-Item -ItemType Directory -Force $obsCfgDir | Out-Null
$iniTargets = @('user.ini', 'global.ini') | ForEach-Object { Join-Path $obsCfgDir $_ } | Where-Object { Test-Path $_ }
if (-not $iniTargets) { $iniTargets = @(Join-Path $obsCfgDir 'user.ini') }
foreach ($ini in $iniTargets) {
    Set-IniValue $ini 'Basic' 'SceneCollection' 'AC Broadcast'
    Set-IniValue $ini 'Basic' 'SceneCollectionFile' 'AC_Broadcast'
    Set-IniValue $ini 'BasicWindow' 'SaveProjectors' 'true'
    Write-Host "Updated $ini"
}

# --- 4. Remaining manual steps -----------------------------------------------
Step "Done. Two one-time steps remain:"
Write-Host @"

1. ENABLE CSP OBS INTEGRATION (once per rig):
   Content Manager -> Settings -> Custom Shaders Patch -> Small Tweaks
   -> enable 'OBS integration'. (Requires CSP v0.1.80 or newer.)

2. VERIFY THE PROJECTOR (once):
   Start Assetto Corsa into a session, then start OBS.
   The 'AC Broadcast' scene should show the chase camera.
   If the broadcast view is not already filling the 4th screen:
   right-click the scene -> 'Fullscreen Projector (Scene)' -> pick the
   4th monitor. With 'Save projectors on exit' enabled (this script set
   it), OBS reopens it there automatically from then on.

   To use AC's trackside TV cameras instead of the chase camera:
   double-click the 'AC External Camera' source and pick
   'Camera: Track camera' from the Texture dropdown.

Tip: add OBS to Windows startup (obs64.exe --minimize-to-tray) so the
broadcast screen comes up on its own when the rig boots.
"@
