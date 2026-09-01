# Installs Custom Shaders Patch (CSP) into Assetto Corsa.
#
# CSP is what makes the sector challenge teleport the car back to the line
# (instead of restarting the session into the pits) and what provides the
# broadcast cameras. Content Manager can install it, but this does it without
# anyone having to click through CM.
#
#   powershell -ExecutionPolicy Bypass -File install_csp.ps1
#   ... -Version 0.2.11        install a specific public build
#   ... -Force                 reinstall even if CSP is already present
#   ... -ZipPath <file>        install from an already downloaded archive
#
# CSP ships as dwrite.dll plus an extension folder that sit in the Assetto
# Corsa root. This downloads the official public build from acstuff.club (the
# same source Content Manager uses) and extracts it there.
#
# -NonInteractive suppresses the closing prompt (used by the installer).

param(
    [string]$Version = '0.2.11',
    [switch]$Force,
    [string]$ZipPath,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
try { Start-Transcript -Path (Join-Path $PSScriptRoot 'install_csp.log') -Force | Out-Null } catch {}

function Done([int]$code) {
    if (-not $NonInteractive) { Read-Host "Press Enter to close" | Out-Null }
    try { Stop-Transcript | Out-Null } catch {}
    exit $code
}
function Warn($m) { Write-Host "WARNING: $m" -ForegroundColor Yellow }
function Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }

# --- Locate Assetto Corsa -----------------------------------------------------
Step "Looking for Assetto Corsa"
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
    Warn "Assetto Corsa not found - install AC first, then re-run this script."
    Done 1
}
Write-Host "Found Assetto Corsa at: $acRoot"

if (Get-Process -Name 'acs', 'AssettoCorsa' -ErrorAction SilentlyContinue) {
    Warn "Assetto Corsa is running - close it first so its files aren't locked."
    Done 1
}

# --- Already installed? -------------------------------------------------------
$dwrite = Join-Path $acRoot 'dwrite.dll'
$extension = Join-Path $acRoot 'extension'
if ((Test-Path $dwrite) -and (Test-Path $extension) -and -not $Force) {
    Write-Host "`nCSP is already installed (dwrite.dll and extension folder present)."
    Write-Host "Nothing to do. Use -Force to reinstall version $Version."
    Done 0
}

# --- Get the archive ----------------------------------------------------------
$tempDir = Join-Path $env:TEMP 'csp-install'
if (Test-Path $tempDir) { Remove-Item $tempDir -Recurse -Force -Confirm:$false }
New-Item -ItemType Directory -Force $tempDir | Out-Null

if ($ZipPath) {
    if (-not (Test-Path $ZipPath)) { Warn "Archive not found: $ZipPath"; Done 1 }
    $zip = $ZipPath
    Write-Host "Using supplied archive: $zip"
} else {
    # Same public build source Content Manager downloads from
    $bundled = Join-Path $PSScriptRoot "vendor\lights-patch-v$Version.zip"
    if (Test-Path $bundled) {
        $zip = $bundled
        Write-Host "Using bundled archive: $zip"
    } else {
        Step "Downloading Custom Shaders Patch $Version (~120 MB)"
        $zip = Join-Path $tempDir "lights-patch-v$Version.zip"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        try {
            Invoke-WebRequest -Uri "https://acstuff.club/patch/?get=$Version" -OutFile $zip -UseBasicParsing
        } catch {
            Warn "Download failed ($($_.Exception.Message)). Install CSP through Content Manager instead."
            Done 1
        }
        Write-Host "Downloaded $([math]::Round((Get-Item $zip).Length / 1MB)) MB"
    }
}

# --- Sanity check the archive before touching the game ------------------------
Step "Checking the archive"
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::OpenRead($zip)
try {
    $entries = $archive.Entries | ForEach-Object { $_.FullName }
    $hasDwrite = $entries -contains 'dwrite.dll'
    $hasExtension = ($entries | Where-Object { $_ -like 'extension/*' }).Count -gt 0
} finally {
    $archive.Dispose()
}
if (-not ($hasDwrite -and $hasExtension)) {
    Warn "That archive doesn't look like CSP (expected dwrite.dll and an extension folder)."
    Done 1
}
Write-Host "Archive looks correct (dwrite.dll + extension)"

# --- Install ------------------------------------------------------------------
# Move the previous install aside rather than merging over it: CSP's own
# update guidance is to replace, not overlay.
if ($Force -and (Test-Path $extension)) {
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    Step "Backing up the existing CSP install"
    $backup = Join-Path $acRoot "extension_backup_$stamp"
    Move-Item $extension $backup
    Write-Host "Previous extension folder -> $backup"
    if (Test-Path $dwrite) { Move-Item $dwrite (Join-Path $acRoot "dwrite_backup_$stamp.dll") -Force }
}

Step "Installing into Assetto Corsa"
try {
    Expand-Archive -Path $zip -DestinationPath $acRoot -Force
} catch {
    Warn "Could not extract into $acRoot ($($_.Exception.Message))."
    Warn "Try running this script as Administrator."
    Done 1
}

# --- Verify -------------------------------------------------------------------
Step "Verifying"
$ok = (Test-Path $dwrite) -and (Test-Path $extension)
if (-not $ok) {
    Warn "CSP files are not where they should be - install via Content Manager instead."
    Done 1
}
Write-Host "  dwrite.dll   : present"
Write-Host "  extension\   : present"
Write-Host "`nCustom Shaders Patch $Version installed." -ForegroundColor Green
Write-Host "Start Assetto Corsa once so CSP initialises, then run the Preflight Check."
Done 0
