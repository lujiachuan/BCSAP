# Build script for the spectrum-platform executables.
#
# Double-click entry point: build-all.cmd (runs -Target release).
#
# Usage (repo root):
#   .\deploy\packaging\build.ps1                      # build desktop client
#   .\deploy\packaging\build.ps1 -Target data-service
#   .\deploy\packaging\build.ps1 -Target instrument-service
#   .\deploy\packaging\build.ps1 -Target operator-package
#         # client + instrument-service + assembled single-folder delivery:
#         # dist\SpectrumPlatform-Operator\spectrum-client\
#         # dist\SpectrumPlatform-Operator\spectrum-instrument-service\
#   .\deploy\packaging\build.ps1 -Target release
#         # everything above + data-service + zip archives for distribution
#
# Notes:
#   * Missing .venv is created automatically (Python 3.12 required) and the
#     client+service dependencies are installed on first run.
#   * PyInstaller is installed into .venv on first run (needs network once).
#   * Outputs: dist\<name>\<name>.exe, dist\SpectrumPlatform-Operator\,
#     dist\release\*.zip ; intermediates live in build\ (deletable).

[CmdletBinding()]
param(
    [ValidateSet('client', 'data-service', 'instrument-service', 'operator-package', 'release')]
    [string]$Target = 'client',

    # release only: skip creating the zip archives
    [switch]$SkipZip,

    # release only: do not open the output folder in Explorer at the end
    [switch]$NoOpen,

    # print the planned steps and exit without building anything
    [switch]$DryRun
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

$VenvDir = Join-Path $RepoRoot '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$DistPath = Join-Path $RepoRoot 'dist'
$WorkPath = Join-Path $RepoRoot 'build'

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Pip {
    param([string[]]$PipArgs)
    Push-Location $RepoRoot
    try {
        & $VenvPython -m pip --disable-pip-version-check --retries 3 --timeout 30 @PipArgs
        if ($LASTEXITCODE -ne 0) { throw "pip failed: pip $($PipArgs -join ' ')" }
    } finally {
        Pop-Location
    }
}

# --- 1. virtual environment ------------------------------------------------
if (-not (Test-Path $VenvPython)) {
    Write-Step "No .venv found - creating one (Python 3.12)"
    $PyLauncher = $null
    & py -3.12 --version *> $null
    if ($LASTEXITCODE -eq 0) { $PyLauncher = 'py -3.12' }
    if ($null -eq $PyLauncher) {
        & python --version *> $null
        if ($LASTEXITCODE -eq 0) { $PyLauncher = 'python' }
    }
    if ($null -eq $PyLauncher) {
        throw "Python 3.12 not found. Install it from python.org (check 'py launcher'), then re-run this script."
    }
    if ($PyLauncher -eq 'py -3.12') {
        & py -3.12 -m venv $VenvDir
    } else {
        & python -m venv $VenvDir
    }
    if ($LASTEXITCODE -ne 0) { throw "Failed to create virtual environment at $VenvDir" }

    Invoke-Pip @('install', '--upgrade', 'pip')
    Write-Step "Installing project dependencies (.[client,service])"
    Invoke-Pip @('install', '-e', '.[client,service]')
} else {
    Write-Host "    .venv found: $VenvPython"
}

# --- 2. PyInstaller --------------------------------------------------------
& $VenvPython -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('PyInstaller') is not None else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Step "Installing PyInstaller into .venv"
    Invoke-Pip @('install', 'pyinstaller>=6.14,<7')
} else {
    Write-Host "    PyInstaller already present"
}

function Invoke-OneBuild {
    param([string]$BuildTarget)
    $SpecFile = Join-Path $PackagingDir $SpecMap[$BuildTarget]
    Write-Step "Building target: $BuildTarget"
    Write-Host "    spec: $SpecFile"
    & $VenvPython -m PyInstaller --noconfirm --clean --distpath $DistPath --workpath $WorkPath $SpecFile
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed for $BuildTarget (exit $LASTEXITCODE)" }
}

function Get-ProjectVersion {
    $pyproject = Join-Path $RepoRoot 'pyproject.toml'
    $match = Select-String -Path $pyproject -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($null -eq $match) { return '0.0.0' }
    return $match.Matches[0].Groups[1].Value
}

function New-ZipArchive {
    param([string]$SourceDir, [string]$ZipPath)
    if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $SourceDir, $ZipPath, [System.IO.Compression.CompressionLevel]::Optimal, $true)
    $size = (Get-Item $ZipPath).Length
    Write-Host ("    {0} ({1:N1} MB)" -f (Split-Path -Leaf $ZipPath), ($size / 1MB))
}

function Compress-Directory {
    param([string]$SourceDir)
    if ($SkipZip) {
        Write-Host "    (zip skipped by -SkipZip)"
        return
    }
    # Wait for file handles to settle before archiving large trees.
    Start-Sleep -Milliseconds 400
    New-ZipArchive -SourceDir $SourceDir -ZipPath ($SourceDir + '.zip')
}

function New-OperatorPackage {
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
    Write-Host "    operator package: $AssemblyDir"
    return $AssemblyDir
}

# --- 3. build --------------------------------------------------------------
if ($DryRun) {
    $version = Get-ProjectVersion
    Write-Step "Dry run - nothing will be built"
    Write-Host "    target      : $Target"
    Write-Host "    version     : $version"
    Write-Host "    repo root   : $RepoRoot"
    Write-Host "    venv python : $VenvPython"
    Write-Host "    dist        : $DistPath"
    Write-Host "    work        : $WorkPath"
    if ($Target -eq 'release') {
        Write-Host "    steps       : client -> instrument-service -> operator package -> data-service -> zips"
    } elseif ($Target -eq 'operator-package') {
        Write-Host "    steps       : client -> instrument-service -> operator package"
    } else {
        Write-Host "    steps       : $Target ($($SpecMap[$Target]))"
    }
    Write-Host "    zip         : $(-not $SkipZip)"
    exit 0
}

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$SummaryDirs = @()

if ($Target -eq 'release') {
    $version = Get-ProjectVersion
    $stamp = Get-Date -Format 'yyyyMMdd'
    Write-Host ""
    Write-Host "==== release build  version=$version  date=$stamp ====" -ForegroundColor Green

    $AssemblyDir = New-OperatorPackage
    Invoke-OneBuild 'data-service'

    Write-Step "Creating distribution archives"
    Compress-Directory $AssemblyDir
    Compress-Directory (Join-Path $DistPath 'spectrum-data-service')
    Compress-Directory (Join-Path $DistPath 'spectrum-client')

    $SummaryDirs += $AssemblyDir
    $SummaryDirs += (Join-Path $DistPath 'spectrum-data-service')

    Write-Step "Release summary"
    Get-ChildItem $DistPath -Filter '*.zip' | ForEach-Object {
        Write-Host ("    dist\{0}   {1:N1} MB" -f $_.Name, ($_.Length / 1MB))
    }
} elseif ($Target -eq 'operator-package') {
    $AssemblyDir = New-OperatorPackage
    $SummaryDirs += $AssemblyDir
} else {
    Invoke-OneBuild $Target
    $ArtifactName = "spectrum-$Target"
    $ArtifactDir = Join-Path $DistPath $ArtifactName
    $SummaryDirs += $ArtifactDir
    Write-Host ""
    Write-Host "    output: $(Join-Path $ArtifactDir "$ArtifactName.exe")"
}

$sw.Stop()
Write-Step "Done in $([int]$sw.Elapsed.TotalMinutes) min $($sw.Elapsed.Seconds) s"
Write-Host "    dist: $DistPath" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host "  operator PC : copy dist\SpectrumPlatform-Operator (or its .zip), run spectrum-client\spectrum-client.exe"
Write-Host "  server      : run dist\spectrum-data-service with PostgreSQL, see deploy/README.md"
Write-Host "  analysis PC : the same operator folder works as-is"

if ($Target -eq 'release' -and -not $NoOpen) {
    Start-Process explorer.exe $DistPath | Out-Null
}
