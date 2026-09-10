@echo off
rem ============================================================
rem  One-click packaging for the spectrum platform.
rem  Double-click this file. It builds the client, the instrument
rem  service and the data service, assembles the operator package
rem  and creates the distribution zip archives under dist\.
rem
rem  Requirements: Python 3.12 installed (py launcher recommended).
rem  A missing .venv is created automatically; the first run needs
rem  network access to install dependencies.
rem
rem  Useful flags:  build-all.cmd /nopause   (no pause at the end)
rem                 build-all.cmd /nozip     (skip zip archives)
rem ============================================================

setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..\dist") do set "DIST_DIR=%%~fI"
set "PS_ARGS=-NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%build.ps1" -Target release"
set "NOPAUSE="

:parse
if "%~1"=="" goto run
if /i "%~1"=="/nopause" set "NOPAUSE=1"
if /i "%~1"=="/nozip" set "PS_ARGS=%PS_ARGS% -SkipZip"
if /i "%~1"=="/dry" set "PS_ARGS=%PS_ARGS% -DryRun"
shift
goto parse

:run
echo.
echo === spectrum-platform one-click release build ===
echo.

powershell %PS_ARGS%
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [FAILED] packaging failed with exit code %RC%
    echo See the messages above; the build log lists the failing step.
) else (
    echo [OK] packaging finished. Output folder: "%DIST_DIR%"
)

if not defined NOPAUSE (
    echo.
    pause
)
exit /b %RC%
