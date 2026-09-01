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
#   -AutoStart                     add OBS (minimized to tray) to the user's
#                                  Windows Startup so the broadcast screen
#                                  comes up on its own after boot
#
# A transcript is written next to this script (setup_broadcast.log).

param(
    [ValidateSet('plugin', 'userconfig', 'all')]
    [string]$Stage = 'all',
    [switch]$NonInteractive,
    [int]$ProjectorMonitor = 3,
    [switch]$AutoStart
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
    <#
      Finding OBS has to work from a 32-bit PowerShell, because the installer
      is 32-bit and Windows hands its children the 32-bit shell. In there
      $env:ProgramFiles is "C:\Program Files (x86)", so checking it alone
      misses a normal 64-bit OBS and the setup reinstalls it every run.
      ProgramW6432 is the real 64-bit Program Files from either bitness.
    #>
    $roots = @($env:ProgramW6432, $env:ProgramFiles, ${env:ProgramFiles(x86)}) |
        Where-Object { $_ } | Select-Object -Unique
    foreach ($root in $roots) {
        if (Test-Path "$root\obs-studio\bin\64bit\obs64.exe") { return "$root\obs-studio" }
    }

    # Registry, in both views. OBS often leaves InstallLocation empty but
    # DisplayIcon points straight at obs64.exe, so derive the root from it.
    $keys = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\OBS Studio',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\OBS Studio',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\OBS Studio'
    )
    foreach ($key in $keys) {
        $reg = Get-ItemProperty $key -ErrorAction SilentlyContinue
        if (-not $reg) { continue }
        if ($reg.InstallLocation -and (Test-Path "$($reg.InstallLocation)\bin\64bit\obs64.exe")) {
            return $reg.InstallLocation.TrimEnd('\')
        }
        $icon = ($reg.DisplayIcon -replace '^"|",?\d*$', '') -replace ',\d+$', ''
        if ($icon -and (Test-Path $icon) -and $icon -match 'obs64\.exe$') {
            # ...\obs-studio\bin\64bit\obs64.exe -> ...\obs-studio
            return (Split-Path (Split-Path (Split-Path $icon -Parent) -Parent) -Parent)
        }
    }

    # Last resort: a running instance tells us exactly where it lives
    $proc = Get-Process obs64 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($proc -and $proc.Path) {
        return (Split-Path (Split-Path (Split-Path $proc.Path -Parent) -Parent) -Parent)
    }
    return $null
}

function Install-PluginStage {
    Step "Looking for OBS Studio"
    $obsRoot = Find-Obs
    if (-not $obsRoot) {
        Write-Host "OBS Studio is not installed."
        # Prefer the bundled official installer (offline, deterministic
        # location); fall back to winget, then to a manual instruction.
        $bundled = Get-ChildItem (Join-Path $PSScriptRoot 'vendor') -Filter 'OBS-Studio-*-Installer.exe' -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending | Select-Object -First 1
        if ($bundled) {
            Write-Host "Installing bundled OBS Studio silently: $($bundled.Name)"
            $proc = Start-Process $bundled.FullName -ArgumentList '/S' -Wait -PassThru
            if ($proc.ExitCode -ne 0) { Fail "Bundled OBS installer exited with code $($proc.ExitCode)." }
            $obsRoot = Find-Obs
            if (-not $obsRoot) { Fail "OBS still not found after the bundled install." }
        } else {
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
        Set-IniValue $ini 'Basic' 'Profile' 'AC Broadcast'
        Set-IniValue $ini 'Basic' 'ProfileDir' 'AC_Broadcast'
        Set-IniValue $ini 'BasicWindow' 'SaveProjectors' 'true'
        # Skip the first-launch auto-config wizard - it would fight our config
        Set-IniValue $ini 'General' 'FirstRun' 'true'
        Write-Host "Updated $ini"
    }

    # OBS WebSocket server: the Lap Time Sender drives scene switching
    # (waiting screen / replays between drivers) through it. Enable it with a
    # generated password; the Sender reads this same config file at runtime,
    # so nothing needs to be copied anywhere.
    Step "Enabling OBS WebSocket control"
    $wsDir = Join-Path $obsCfgDir 'plugin_config\obs-websocket'
    New-Item -ItemType Directory -Force $wsDir | Out-Null
    $wsCfgPath = Join-Path $wsDir 'config.json'
    $wsCfg = $null
    if (Test-Path $wsCfgPath) {
        try { $wsCfg = Get-Content $wsCfgPath -Raw | ConvertFrom-Json } catch {}
    }
    $password = if ($wsCfg -and $wsCfg.server_password) { $wsCfg.server_password } else {
        -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 20 | ForEach-Object { [char]$_ })
    }
    $port = if ($wsCfg -and $wsCfg.server_port) { [int]$wsCfg.server_port } else { 4455 }
    $wsJson = @{
        alerts_enabled  = $false
        auth_required   = $true
        first_load      = $false
        server_enabled  = $true
        server_password = $password
        server_port     = $port
    } | ConvertTo-Json
    Write-TextFile $wsCfgPath $wsJson
    Write-Host "WebSocket server enabled on port $port (password stored in config.json)"

    # Dedicated OBS profile: fixed 1920x1080 canvas (so the scene layout and
    # projector are deterministic regardless of the rig's display sizes) and
    # the replay buffer armed for Replay mode (last 60s, saved on demand).
    Step "Installing 'AC Broadcast' OBS profile"
    $profileDir = Join-Path $obsCfgDir 'basic\profiles\AC_Broadcast'
    New-Item -ItemType Directory -Force $profileDir | Out-Null
    $replayDir = Join-Path ([Environment]::GetFolderPath('MyVideos')) 'AC Broadcast'
    New-Item -ItemType Directory -Force $replayDir | Out-Null
    $profileIni = @(
        '[General]',
        'Name=AC Broadcast',
        '',
        '[Video]',
        'BaseCX=1920',
        'BaseCY=1080',
        'OutputCX=1920',
        'OutputCY=1080',
        'FPSType=0',
        'FPSCommon=30',
        '',
        '[Output]',
        'Mode=Simple',
        '',
        '[SimpleOutput]',
        'RecRB=true',
        'RecRBTime=60',
        ('FilePath={0}' -f $replayDir)
    ) -join "`r`n"
    Write-TextFile (Join-Path $profileDir 'basic.ini') ($profileIni + "`r`n")
    Write-Host "Installed profile -> $profileDir (replays saved to $replayDir)"

    # CSP only publishes broadcast cameras when this Small Tweaks flag is set
    # (also done by install_ac_app.ps1; both are idempotent)
    Step "Enabling CSP OBS integration (Small Tweaks: [OBS] DIRECT_PASS)"
    $cspCfgDir = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Assetto Corsa\cfg\extension'
    New-Item -ItemType Directory -Force $cspCfgDir | Out-Null
    Set-IniValue (Join-Path $cspCfgDir 'small_tweaks.ini') 'OBS' 'DIRECT_PASS' '1'
    Write-Host "Updated $cspCfgDir\small_tweaks.ini"

    if ($AutoStart) {
        Step "Adding OBS to Windows Startup (minimized to tray)"
        $obsRoot = Find-Obs
        if (-not $obsRoot) {
            Write-Host "OBS not found yet - skipping startup shortcut (re-run after the plugin stage)."
        } else {
            # Launch via a silent VBS wrapper that first clears OBS's
            # unclean-shutdown marker: venue rigs get power-cycled, and OBS 30+
            # otherwise shows a Safe Mode dialog that would block the
            # unattended broadcast screen (OBS 32 removed the CLI flag that
            # used to bypass it; deleting .sentinel is the supported way).
            $obsBin = Join-Path $obsRoot 'bin\64bit'
            $vbsPath = Join-Path $obsCfgDir 'launch_broadcast.vbs'
            $vbs = @(
                "' Starts the leaderboard broadcast screen (OBS) silently.",
                "' Generated by setup_broadcast.ps1 - safe to delete along with",
                "' the 'AC Broadcast (OBS)' shortcut in shell:startup.",
                'On Error Resume Next',
                'Set fso = CreateObject("Scripting.FileSystemObject")',
                'Set sh = CreateObject("WScript.Shell")',
                'appData = sh.ExpandEnvironmentStrings("%APPDATA%")',
                'fso.DeleteFolder appData & "\obs-studio\.sentinel", True',
                ('sh.CurrentDirectory = "{0}"' -f $obsBin),
                ('sh.Run """{0}\obs64.exe"" --minimize-to-tray --disable-shutdown-check", 1, False' -f $obsBin)
            ) -join "`r`n"
            Write-TextFile $vbsPath $vbs
            Write-Host "Created launcher -> $vbsPath"

            $startupDir = [Environment]::GetFolderPath('Startup')
            $shell = New-Object -ComObject WScript.Shell
            $shortcut = $shell.CreateShortcut((Join-Path $startupDir 'AC Broadcast (OBS).lnk'))
            $shortcut.TargetPath = "$env:SystemRoot\System32\wscript.exe"
            $shortcut.Arguments = """$vbsPath"""
            $shortcut.WorkingDirectory = $obsBin
            $shortcut.IconLocation = (Join-Path $obsBin 'obs64.exe') + ',0'
            $shortcut.Description = 'Starts the leaderboard broadcast screen (OBS) with Windows'
            $shortcut.Save()
            Write-Host "Created startup shortcut -> $startupDir\AC Broadcast (OBS).lnk"
        }
    }
}

if ($Stage -in @('plugin', 'all')) { Install-PluginStage }
if ($Stage -in @('userconfig', 'all')) { Install-UserConfigStage }

Step "Done. One verification pass on first use:"
Write-Host @"

Start Assetto Corsa into a session, then start OBS.
The 'AC Broadcast' scene should show the TV-style track cameras
following the car. If the broadcast view is not already filling the
4th screen: right-click the scene -> 'Fullscreen Projector (Scene)' ->
pick the 4th monitor. With 'Save projectors on exit' enabled (this
script set it), OBS reopens it there automatically from then on.

Between drivers the Lap Time Sender switches the 4th screen on its own:
pick 'Waiting screen' or 'Replay of last run' in the Sender's Advanced
settings ('4th Screen'). Customize the Waiting scene in OBS freely
(venue logo, colors) - only its name 'Waiting' matters.

(CSP's OBS integration flag, the WebSocket control server, the replay
buffer, and the OBS scene were all configured automatically;
CSP v0.1.80+ must be installed via Content Manager.)
"@

if (-not $NonInteractive) { Read-Host "Press Enter to close" | Out-Null }
try { Stop-Transcript | Out-Null } catch {}
exit 0
