# Broadcast screen setup for the SimRacing Leaderboard system.
#
# Sets up the 4th-screen "outside the car" camera view for Assetto Corsa:
#   - Verifies OBS Studio is installed (installs it via winget if missing)
#   - Downloads the official CSP OBS plugin (v1.0.0.1) from GitHub and
#     installs it into OBS's plugin folder
#   - Installs the preconfigured "AC Broadcast" OBS scene collection
#     (chase camera source, fullscreen projector on the chosen monitor)
#
# Standalone use (run as Administrator):
#   powershell -ExecutionPolicy Bypass -File setup_broadcast.ps1
#
# Installer use: the Lap Time Sender installer runs this in two stages so the
# machine-wide part gets elevation while per-user OBS config lands in the
# operator's own profile:
#   -Stage plugin      elevated: OBS check/install + plugin DLL
#   -Stage userconfig  as the logged-in user: scene collection + OBS ini
#
# Params:
#   -Stage plugin|userconfig|all   (default all)
#   -NonInteractive                no prompts; auto-installs OBS if missing
#   -ProjectorMonitor <index>      monitor for the broadcast view (0 = first,
#                                  default 3 = the 4th monitor)
#
# A transcript is written next to this script (setup_broadcast.log).

param(
    [ValidateSet('plugin', 'userconfig', 'all')]
    [string]$Stage = 'all',
    [switch]$NonInteractive,
    [int]$ProjectorMonitor = 3
)

$ErrorActionPreference = 'Stop'
$PluginUrl = 'https://github.com/ac-custom-shaders-patch/acc-obs-plugin/releases/download/v1.0.0.1/acc-obs-plugin.zip'

try { Start-Transcript -Path (Join-Path $PSScriptRoot "setup_broadcast.$Stage.log") -Force | Out-Null } catch {}

function Fail($msg) {
    Write-Host "ERROR: $msg" -ForegroundColor Red
    if (-not $NonInteractive) { Read-Host "Press Enter to close" | Out-Null }
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}
function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

# UTF-8 WITHOUT BOM: OBS's JSON/ini parsers choke on a BOM, and PowerShell
# 5.1's Out-File -Encoding utf8 always writes one.
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
function Write-TextFile([string]$path, [string]$content) {
    [System.IO.File]::WriteAllText($path, $content, $Utf8NoBom)
}

function Find-Obs {
    foreach ($candidate in @("$env:ProgramFiles\obs-studio", "${env:ProgramFiles(x86)}\obs-studio")) {
        if (Test-Path "$candidate\bin\64bit\obs64.exe") { return $candidate }
    }
    $reg = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\OBS Studio' -ErrorAction SilentlyContinue
    if ($reg -and $reg.InstallLocation -and (Test-Path "$($reg.InstallLocation)\bin\64bit\obs64.exe")) {
        return $reg.InstallLocation
    }
    return $null
}

function Install-PluginStage {
    Step "Looking for OBS Studio"
    $obsRoot = Find-Obs
    if (-not $obsRoot) {
        Write-Host "OBS Studio is not installed."
        $doInstall = $NonInteractive
        if (-not $NonInteractive) {
            $doInstall = (Read-Host "Install OBS Studio now via winget? (y/n)") -eq 'y'
        }
        if (-not $doInstall) { Fail "Install OBS Studio from https://obsproject.com and re-run this script." }
        winget install --id OBSProject.OBSStudio --accept-package-agreements --accept-source-agreements --silent
        if ($LASTEXITCODE -ne 0) { Fail "winget install failed - install OBS manually from https://obsproject.com and re-run this script (or re-run the Lap Time Sender installer with the broadcast option)." }
        $obsRoot = Find-Obs
        if (-not $obsRoot) { Fail "OBS still not found after install - re-run this script." }
    }
    Write-Host "Found OBS at: $obsRoot"

    Step "Installing CSP OBS plugin (official, from ac-custom-shaders-patch GitHub)"
    $pluginDir = Join-Path $obsRoot 'obs-plugins\64bit'
    if (-not (Test-Path $pluginDir)) { Fail "OBS plugin folder not found: $pluginDir" }

    $tmp = Join-Path $env:TEMP 'acc-obs-plugin-setup'
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force -Confirm:$false }
    New-Item -ItemType Directory -Force $tmp | Out-Null
    $zipPath = Join-Path $tmp 'acc-obs-plugin.zip'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
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
}

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

function Install-UserConfigStage {
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
}

if ($Stage -in @('plugin', 'all')) { Install-PluginStage }
if ($Stage -in @('userconfig', 'all')) { Install-UserConfigStage }

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

if (-not $NonInteractive) { Read-Host "Press Enter to close" | Out-Null }
try { Stop-Transcript | Out-Null } catch {}
exit 0
