# Rig health check for the SimRacing Leaderboard AC integration.
# Verifies every piece of the sector-challenge + broadcast-screen chain and
# prints PASS/FAIL per item. Safe to run any time; changes nothing.
#
#   powershell -ExecutionPolicy Bypass -File check_rig.ps1

$ErrorActionPreference = 'SilentlyContinue'

function Check($label, $ok, $hint) {
    $tag = if ($ok) { 'PASS' } else { 'FAIL' }
    $color = if ($ok) { 'Green' } else { 'Red' }
    Write-Host ("[{0}] {1}" -f $tag, $label) -ForegroundColor $color
    if (-not $ok -and $hint) { Write-Host ("       -> {0}" -f $hint) -ForegroundColor Yellow }
    return [bool]$ok
}

Write-Host "=== Leaderboard rig check ===" -ForegroundColor Cyan

# Assetto Corsa
$acRoot = $null
$steam = (Get-ItemProperty 'HKCU:\Software\Valve\Steam').SteamPath
if ($steam) {
    $steam = $steam -replace '/', '\'
    $libraries = @($steam)
    $vdf = Join-Path $steam 'steamapps\libraryfolders.vdf'
    if (Test-Path $vdf) {
        foreach ($m in (Select-String -Path $vdf -Pattern '"path"\s+"([^"]+)"' -AllMatches).Matches) {
            $libraries += ($m.Groups[1].Value -replace '\\\\', '\')
        }
    }
    foreach ($lib in ($libraries | Select-Object -Unique)) {
        $candidate = Join-Path $lib 'steamapps\common\assettocorsa'
        if (Test-Path $candidate) { $acRoot = $candidate; break }
    }
}
$null = Check "Assetto Corsa installed" $acRoot "Install AC via Steam."
if ($acRoot) { Write-Host "       at $acRoot" }

# CSP
$cspOk = $acRoot -and (Test-Path (Join-Path $acRoot 'extension'))
$null = Check "Custom Shaders Patch installed" $cspOk "Install CSP via Content Manager (v0.1.80+)."

# Companion app
$appOk = $acRoot -and (Test-Path (Join-Path $acRoot 'apps\lua\LeaderboardSector\LeaderboardSector.lua'))
$null = Check "LeaderboardSector companion app installed" $appOk "Run install_ac_app.ps1 from the ac_app folder."

# CSP OBS integration flag
$cspIni = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Assetto Corsa\cfg\extension\small_tweaks.ini'
$flagOk = (Test-Path $cspIni) -and ((Get-Content $cspIni -Raw) -match '(?ms)^\[OBS\].*?^\s*DIRECT_PASS\s*=\s*1')
$null = Check "CSP OBS integration enabled ([OBS] DIRECT_PASS=1)" $flagOk "Run install_ac_app.ps1, or enable OBS integration in CSP Small Tweaks."

# OBS Studio
$obsRoot = $null
foreach ($candidate in @("$env:ProgramFiles\obs-studio", "${env:ProgramFiles(x86)}\obs-studio")) {
    if (Test-Path "$candidate\bin\64bit\obs64.exe") { $obsRoot = $candidate; break }
}
if (-not $obsRoot) {
    $reg = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\OBS Studio'
    if ($reg -and $reg.InstallLocation -and (Test-Path "$($reg.InstallLocation)\bin\64bit\obs64.exe")) { $obsRoot = $reg.InstallLocation }
}
$null = Check "OBS Studio installed" $obsRoot "Run setup_broadcast.ps1 (or the Lap Time Sender installer's broadcast task)."

# CSP OBS plugin DLL
$pluginOk = $obsRoot -and (Get-ChildItem (Join-Path $obsRoot 'obs-plugins\64bit') -Filter '*acc*obs*.dll').Count -gt 0
$null = Check "CSP OBS plugin installed" $pluginOk "Run setup_broadcast.ps1 -Stage plugin as Administrator."

# OBS scene collection
$sceneOk = Test-Path (Join-Path $env:APPDATA 'obs-studio\basic\scenes\AC_Broadcast.json')
$null = Check "AC Broadcast scene collection installed" $sceneOk "Run setup_broadcast.ps1 -Stage userconfig."

# OBS WebSocket control (scene switching / replays between drivers)
$wsCfgPath = Join-Path $env:APPDATA 'obs-studio\plugin_config\obs-websocket\config.json'
$wsOk = $false
if (Test-Path $wsCfgPath) {
    try { $wsOk = [bool](Get-Content $wsCfgPath -Raw | ConvertFrom-Json).server_enabled } catch {}
}
$null = Check "OBS WebSocket control enabled" $wsOk "Run setup_broadcast.ps1 -Stage userconfig."

# OBS broadcast profile (canvas + replay buffer)
$profOk = Test-Path (Join-Path $env:APPDATA 'obs-studio\basic\profiles\AC_Broadcast\basic.ini')
$null = Check "AC Broadcast OBS profile installed (replay buffer)" $profOk "Run setup_broadcast.ps1 -Stage userconfig."

# OBS autostart shortcut (optional)
$autoOk = Test-Path (Join-Path ([Environment]::GetFolderPath('Startup')) 'AC Broadcast (OBS).lnk')
Write-Host ("[{0}] OBS auto-start shortcut (optional)" -f $(if ($autoOk) { 'PASS' } else { ' -- ' })) -ForegroundColor $(if ($autoOk) { 'Green' } else { 'DarkGray' })

# Lap Time Sender reachable (only meaningful while it is running)
try {
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:5001/health' -UseBasicParsing -TimeoutSec 2
    $null = Check "Lap Time Sender running (port 5001)" ($r.StatusCode -eq 200) $null
} catch {
    Write-Host "[ -- ] Lap Time Sender not currently running (start it to complete the chain)" -ForegroundColor DarkGray
}

Write-Host "`nIn-game check: open the 'Leaderboard Sector' app from AC's taskbar -" -ForegroundColor Cyan
Write-Host "it should read 'Connected to Lap Time Sender' while the Sender runs." -ForegroundColor Cyan
Read-Host "`nPress Enter to close" | Out-Null
