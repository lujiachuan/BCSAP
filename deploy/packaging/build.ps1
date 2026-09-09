# Build script for the spectrum-platform executables.
#
# Usage (repo root):
#   .\deploy\packaging\build.ps1                      # build desktop client
#   .\deploy\packaging\build.ps1 -Target data-service # build data service
#   .\deploy\packaging\build.ps1 -Target instrument-service
#   .\deploy\packaging\build.ps1 -Target operator-package
#         # client + instrument-service + assembled single-folder delivery:
#         # dist\SpectrumPlatform-Operator\spectrum-client\
#         # dist\SpectrumPlatform-Operator\spectrum-instrument-service\
#         # Double-click spectrum-client.exe: it auto-starts the sibling service.
#
# Outputs:
#   dist\<name>\<name>.exe   runnable onedir build
#   build\                   PyInstaller intermediates (deletable)
#
# Requires: the repo .venv (see README section 7). PyInstaller is installed
# into .venv automatically on first run. Internet access needed once.

[CmdletBinding()]
param(
    [ValidateSet('client', 'data-service', 'instrument-service', 'operator-package')]
    [string]$Target = 'client'
)

$ErrorActionPreference = 'Stop'
# PS7+ only: keep native stderr from becoming terminating errors.
if ($PSVersionTable.PSVersion.Major -ge 7) { $PSNativeCommandUseErrorActionPreference = $false }

# deploy/packaging -> deploy -> repo root
$PackagingDir = $PSScriptRoot
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PackagingDir)

$SpecMap = @{
    'client'             = 'spectrum_client.spec'
    'data-service'       = 'spectrum_data_service.spec'
    'instrument-service' = 'spectrum_instrument_service.spec'
}

$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPython)) {
    Write-Error "Virtual environment not found: $VenvPython. Run the env setup in README section 7 first."
}

$DistPath = Join-Path $RepoRoot 'dist'
$WorkPath = Join-Path $RepoRoot 'build'

# --- ensure PyInstaller -------------------------------------------------
Write-Host "==> Checking PyInstaller in .venv"
& $VenvPython -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('PyInstaller') is not None else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "==> Installing PyInstaller into .venv"
    & $VenvPython -m pip install --disable-pip-version-check --retries 3 --timeout 30 "pyinstaller>=6.14,<7"
    if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller failed" }
} else {
    Write-Host "    PyInstaller already present"
}

function Invoke-OneBuild {
    param([string]$BuildTarget)
    $SpecFile = Join-Path $PackagingDir $SpecMap[$BuildTarget]
    Write-Host "==> Building target: $BuildTarget  (spec: $SpecFile)"
    & $VenvPython -m PyInstaller --noconfirm --clean --distpath $DistPath --workpath $WorkPath $SpecFile
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed for $BuildTarget (exit $LASTEXITCODE)" }
}

if ($Target -eq 'operator-package') {
    Invoke-OneBuild 'client'
    Invoke-OneBuild 'instrument-service'
    $AssemblyDir = Join-Path $DistPath 'SpectrumPlatform-Operator'
    $ClientSrc = Join-Path $DistPath 'spectrum-client'
    $ServiceSrc = Join-Path $DistPath 'spectrum-instrument-service'
    if (Test-Path $AssemblyDir) { Remove-Item -Recurse -Force $AssemblyDir }
    New-Item -ItemType Directory -Path $AssemblyDir | Out-Null
    Copy-Item -Recurse $ClientSrc (Join-Path $AssemblyDir 'spectrum-client')
    Copy-Item -Recurse $ServiceSrc (Join-Path $AssemblyDir 'spectrum-instrument-service')
    Write-Host ""
    Write-Host "==> Operator package assembled:"
    Write-Host "    $AssemblyDir"
    Write-Host "    Copy this folder to the operator PC and double-click"
    Write-Host "    $AssemblyDir\spectrum-client\spectrum-client.exe"
    Write-Host "    The client auto-starts the sibling instrument service (127.0.0.1:8765)."
    exit 0
}

Invoke-OneBuild $Target

$ArtifactName = "spectrum-$Target"
$ArtifactDir = Join-Path $DistPath $ArtifactName
$OutputExe = Join-Path $ArtifactDir "$ArtifactName.exe"
Write-Host ""
Write-Host "==> Build finished. Output:"
Write-Host "    $OutputExe"
Write-Host ""
Write-Host "Verification: run the exe directly, or wrap it:"
Write-Host "  client  : double-click $OutputExe (GUI)"
Write-Host "  services: register with NSSM, see deploy/README.md"
