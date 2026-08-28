# Signed release build for Lap Time Sender.
#
# Uses the SIM COACHES LLC EV code-signing certificate (the same one that
# signs TrackPro). The EV private key lives on the hardware USB token /
# signing device - PLUG IT IN FIRST (with its middleware installed, e.g.
# SafeNet Authentication Client); each signing step pops the token's PIN
# prompt.
#
#   powershell -ExecutionPolicy Bypass -File sign_release.ps1            # sign existing build
#   powershell -ExecutionPolicy Bypass -File sign_release.ps1 -Rebuild   # PyInstaller first
#
# What it does, in order:
#   1. Signs "dist\Lap Time Sender\Lap Time Sender.exe" (SHA256 +
#      Sectigo RFC3161 timestamp, so signatures outlive the cert)
#   2. Compiles the installer with Inno's sign tool wired up, which makes
#      Inno sign BOTH the final LapTimeSenderSetup.exe and the embedded
#      uninstaller
#   3. Verifies every signature and prints the result

param(
    [string]$CertSubject = 'SIM COACHES LLC',
    [switch]$Rebuild
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$appExe = Join-Path $root 'dist\Lap Time Sender\Lap Time Sender.exe'
$setupIss = Join-Path $root 'setup.iss'
$installerExe = Join-Path $root 'installer\LapTimeSenderSetup.exe'
$timestampUrl = 'http://timestamp.sectigo.com'

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# Locate signtool (newest x64 from the Windows SDK)
$signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
    Where-Object FullName -match '\\x64\\' | Sort-Object FullName | Select-Object -Last 1
if (-not $signtool) { Fail "signtool.exe not found - install the Windows 10/11 SDK." }
$signtool = $signtool.FullName
Write-Host "signtool: $signtool"

# The EV cert only appears in the store while the token is plugged in
$cert = Get-ChildItem Cert:\CurrentUser\My -ErrorAction SilentlyContinue |
    Where-Object { $_.Subject -match [regex]::Escape($CertSubject) -and $_.HasPrivateKey }
if (-not $cert) {
    $cert = Get-ChildItem Cert:\LocalMachine\My -ErrorAction SilentlyContinue |
        Where-Object { $_.Subject -match [regex]::Escape($CertSubject) -and $_.HasPrivateKey }
}
if (-not $cert) {
    Fail ("No '$CertSubject' certificate with a private key is available. " +
          "Plug in the TrackPro EV signing token (and install its middleware, e.g. SafeNet Authentication Client), then re-run.")
}
Write-Host "Certificate found: $(($cert | Select-Object -First 1).Subject)"

if ($Rebuild) {
    Write-Host "`n== Rebuilding executable (PyInstaller)"
    if (Get-Process 'Lap Time Sender' -ErrorAction SilentlyContinue) { Fail "Close the running Lap Time Sender first - it locks the build folder." }
    Push-Location $root
    pyinstaller 'Lap Time Sender.spec' --noconfirm
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { Fail "PyInstaller failed." }
}
if (-not (Test-Path $appExe)) { Fail "App executable not found ($appExe) - run with -Rebuild." }

Write-Host "`n== Signing application executable"
& $signtool sign /n $CertSubject /fd SHA256 /td SHA256 /tr $timestampUrl "$appExe"
if ($LASTEXITCODE -ne 0) { Fail "Signing the app executable failed." }

Write-Host "`n== Compiling installer with signing enabled"
$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { Fail "Inno Setup 6 not found." }
# $q = quote, $f = file to sign (Inno Setup sign tool placeholders)
$signCmd = "signtool=$signtool sign /n `$q$CertSubject`$q /fd SHA256 /td SHA256 /tr $timestampUrl `$f"
& $iscc "/DSIGN" "/S$signCmd" "$setupIss"
if ($LASTEXITCODE -ne 0) { Fail "Installer compile failed." }

Write-Host "`n== Verifying signatures"
$allOk = $true
foreach ($file in @($appExe, $installerExe)) {
    $sig = Get-AuthenticodeSignature $file
    $name = Split-Path $file -Leaf
    if ($sig.Status -eq 'Valid') {
        Write-Host "  [OK]   $name  ($($sig.SignerCertificate.Subject.Split(',')[0]))" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] $name  ($($sig.Status))" -ForegroundColor Red
        $allOk = $false
    }
}
if (-not $allOk) { Fail "Signature verification failed." }

Write-Host "`nSigned release ready: $installerExe" -ForegroundColor Green
Write-Host "EV-signed installers get immediate SmartScreen reputation - no download warning."
