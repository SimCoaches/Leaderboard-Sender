# Signed release build for Lap Time Sender.
#
# Signs with the SIM COACHES LLC EV certificate exactly the way TrackPro
# releases are signed: Jsign + the EV private key held in Google Cloud
# KMS/HSM (project dill-486809, keyring simcoaches-code-signing, key
# ev-signing-key). No key material exists on this machine; gcloud
# application-default credentials authorize the signing.
#
# Delegates to TrackPro's production signing script
# (TrackProV2\scripts\sign-artifact-google-kms.ps1), which also handles
# RFC3161 timestamping and Windows Defender's transient lock on freshly
# built installers.
#
#   powershell -ExecutionPolicy Bypass -File sign_release.ps1            # sign existing build
#   powershell -ExecutionPolicy Bypass -File sign_release.ps1 -Rebuild   # PyInstaller first
#
# Order matters: the app exe is signed BEFORE the installer is compiled so
# the packaged exe is the signed one; then the installer itself is signed.

param(
    [switch]$Rebuild,
    [string]$TrackProRepo = "$env:USERPROFILE\Documents\TrackProV2"
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$appExe = Join-Path $root 'dist\Lap Time Sender\Lap Time Sender.exe'
$installerExe = Join-Path $root 'installer\LapTimeSenderSetup.exe'

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

$signScript = Join-Path $TrackProRepo 'scripts\sign-artifact-google-kms.ps1'
if (-not (Test-Path $signScript)) { Fail "TrackPro signing script not found: $signScript" }

# Resolve cert + jsign the same way TrackPro does, with the shared
# TrackProSigning folder as an extra fallback for the jar
$certFile = $env:TRACKPRO_EV_CERT_FILE
if (-not $certFile) { $certFile = Join-Path $TrackProRepo 'certs\sim-coaches-ev-code-signing-2027.pem' }
if (-not (Test-Path $certFile)) { Fail "EV certificate file not found: $certFile" }
$jsignJar = $env:TRACKPRO_JSIGN_JAR
if (-not $jsignJar) {
    foreach ($candidate in @("$env:LOCALAPPDATA\TrackProSigning\jsign.jar",
                             (Join-Path $TrackProRepo 'jsign.jar'),
                             "$env:USERPROFILE\Downloads\jsign.jar")) {
        if (Test-Path $candidate) { $jsignJar = $candidate; break }
    }
}
if (-not $jsignJar) { Fail "jsign.jar not found (TrackProSigning folder, TrackPro repo, or Downloads)." }

function Sign-File([string]$path) {
    Write-Host "`n== Signing $(Split-Path $path -Leaf)" -ForegroundColor Cyan
    & powershell -NoProfile -ExecutionPolicy Bypass -File $signScript `
        -ArtifactPath $path -CertFile $certFile -JsignJar $jsignJar
    if ($LASTEXITCODE -ne 0) { Fail "Signing failed for $path" }
}

if ($Rebuild) {
    Write-Host "== Rebuilding executable (PyInstaller)"
    if (Get-Process 'Lap Time Sender' -ErrorAction SilentlyContinue) { Fail "Close the running Lap Time Sender first - it locks the build folder." }
    Push-Location $root
    pyinstaller 'Lap Time Sender.spec' --noconfirm
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { Fail "PyInstaller failed." }
}
if (-not (Test-Path $appExe)) { Fail "App executable not found ($appExe) - run with -Rebuild." }

# 1. Sign the application exe so the installer packages a signed binary
Sign-File $appExe

# 2. Compile the installer around the signed exe
Write-Host "`n== Compiling installer" -ForegroundColor Cyan
$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { Fail "Inno Setup 6 not found." }
& $iscc (Join-Path $root 'setup.iss') | Select-Object -Last 2
if ($LASTEXITCODE -ne 0) { Fail "Installer compile failed." }

# 3. Sign the installer itself (this is what downloads/SmartScreen sees)
Sign-File $installerExe

Write-Host "`n== Final verification" -ForegroundColor Cyan
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
