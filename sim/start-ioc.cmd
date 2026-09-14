@echo off
rem ===========================================================================
rem  Start the local simulated EPICS IOC (development / wiring checks only).
rem  It never talks to real beamline hardware.
rem
rem  Double-click this file. Keep the window open while the IOC runs;
rem  type "exit" and press Enter to stop it.
rem
rem  The PV list lives in ioc.db next to this file and covers the full demo
rem  ledger: 128 mapped PVs + 22 sim-only extras (BD Reset/Error, JM:01/:07
rem  set/output, MSScan and MS:Scanz). See README.md (Chinese) for details.
rem
rem  NOTE: this file is deliberately ASCII-only. cmd.exe parses .cmd files with
rem  the OEM code page, so non-ASCII bytes here corrupt command parsing.
rem ===========================================================================
setlocal

if "%EPICS_BASE%"=="" (
    echo [ERROR] EPICS_BASE is not set, cannot locate softIoc.exe.
    echo         Example: set EPICS_BASE=D:\EPICS\base
    pause
    exit /b 1
)

set "SOFTIOC="
if not "%EPICS_HOST_ARCH%"=="" (
    if exist "%EPICS_BASE%\bin\%EPICS_HOST_ARCH%\softIoc.exe" (
        set "SOFTIOC=%EPICS_BASE%\bin\%EPICS_HOST_ARCH%\softIoc.exe"
    )
)
if not defined SOFTIOC (
    rem Fall back to any host-arch subdirectory, e.g. windows-x64-mingw.
    for /d %%D in ("%EPICS_BASE%\bin\*") do (
        if exist "%%D\softIoc.exe" set "SOFTIOC=%%D\softIoc.exe"
    )
)
if not defined SOFTIOC (
    if exist "%EPICS_BASE%\bin\softIoc.exe" set "SOFTIOC=%EPICS_BASE%\bin\softIoc.exe"
)
if not defined SOFTIOC (
    echo [ERROR] softIoc.exe not found under %EPICS_BASE%\bin
    pause
    exit /b 1
)

rem Pin CA to localhost so caget/caput run from THIS window can never reach a
rem real beamline IOC. The instrument service does not need these variables.
set "EPICS_CA_ADDR_LIST=127.0.0.1"
set "EPICS_CA_AUTO_ADDR_LIST=NO"

echo ============================================================
echo  Simulated IOC starting
echo  executable : %SOFTIOC%
echo  database   : %~dp0ioc.db
echo  CA address : %EPICS_CA_ADDR_LIST% (AUTO_ADDR_LIST=NO)
echo ------------------------------------------------------------
echo  Verify from another window : caget Part1:Flow_W:CS200A:Setpoint
echo  Stop                       : type "exit" and press Enter
echo ============================================================
echo.

"%SOFTIOC%" -d "%~dp0ioc.db"

endlocal
