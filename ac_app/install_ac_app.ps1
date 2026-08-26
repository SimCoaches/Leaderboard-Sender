# Installs the LeaderboardSector CSP companion app into Assetto Corsa.
#
# - Locates the AC install via the Steam registry + library folders
# - Copies LeaderboardSector into <assettocorsa>\apps\lua\
# - Enables CSP's OBS integration flag ([OBS] DIRECT_PASS=1 in the user's
#   Small Tweaks config) so the broadcast screen works without touching
#   Content Manager
# - Warns (without failing the install) if AC or CSP aren't present
#
# Run as the normal user - Steam's folder is user-writable, no admin needed:
#   powershell -ExecutionPolicy Bypass -File install_ac_app.ps1
#
# -NonInteractive suppresses the closing prompt (used by the installer).
# A transcript is written next to this script (install_ac_app.log).

param(
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
try { Start-Transcript -Path (Join-Path $PSScriptRoot 'install_ac_app.log') -Force | Out-Null } catch {}

function Done([int]$code) {
    if (-not $NonInteractive) { Read-Host "Press Enter to close" | Out-Null }
    try { Stop-Transcript | Out-Null } catch {}
    exit $code
}
function Warn($msg) { Write-Host "WARNING: $msg" -ForegroundColor Yellow }

# UTF-8 without BOM (CSP ini files should stay BOM-less)
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
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
    [System.IO.File]::WriteAllText($path, (($lines -join "`r`n") + "`r`n"), $Utf8NoBom)
}

# --- Locate Assetto Corsa -----------------------------------------------------
Write-Host "== Looking for Assetto Corsa"
$acRoot = $null
$steam = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).SteamPath
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
        if (Test-Path (Join-Path $candidate 'AssettoCorsa.exe')) { $acRoot = $candidate; break }
        if ((-not $acRoot) -and (Test-Path $candidate)) { $acRoot = $candidate }
    }
}
if (-not $acRoot) {
    Warn "Assetto Corsa not found on this PC - skipping companion app install."
    Warn "If AC is installed later, re-run this script from the Lap Time Sender folder."
    Done 0
}
Write-Host "Found Assetto Corsa at: $acRoot"

# --- Check CSP ----------------------------------------------------------------
if (-not (Test-Path (Join-Path $acRoot 'extension'))) {
    Warn "Custom Shaders Patch does not appear to be installed (no 'extension' folder)."
    Warn "Install CSP via Content Manager - the companion app and broadcast camera need it."
    # Continue anyway: the files are harmless and start working once CSP arrives.
}

# --- Copy the companion app ---------------------------------------------------
Write-Host "== Installing LeaderboardSector app"
$src = Join-Path $PSScriptRoot 'LeaderboardSector'
if (-not (Test-Path (Join-Path $src 'LeaderboardSector.lua'))) {
    Warn "LeaderboardSector folder not found next to this script - nothing to install."
    Done 1
}
$destDir = Join-Path $acRoot 'apps\lua'
try {
    New-Item -ItemType Directory -Force $destDir | Out-Null
    Copy-Item $src (Join-Path $destDir 'LeaderboardSector') -Recurse -Force
    Write-Host "Installed -> $destDir\LeaderboardSector"
} catch {
    Warn "Could not copy into $destDir ($($_.Exception.Message))."
    Warn "Re-run this script as Administrator."
    Done 1
}

# --- Enable CSP OBS integration ----------------------------------------------
# The Small Tweaks module only publishes broadcast cameras when
# [OBS] DIRECT_PASS is set in the user's CSP config override.
Write-Host "== Enabling CSP OBS integration (Small Tweaks: [OBS] DIRECT_PASS)"
$cfgDir = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Assetto Corsa\cfg\extension'
New-Item -ItemType Directory -Force $cfgDir | Out-Null
Set-IniValue (Join-Path $cfgDir 'small_tweaks.ini') 'OBS' 'DIRECT_PASS' '1'
Write-Host "Updated $cfgDir\small_tweaks.ini"

Write-Host "`nDone. The companion app loads automatically next time AC starts."
Done 0
