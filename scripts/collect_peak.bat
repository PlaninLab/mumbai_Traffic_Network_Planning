@echo off
REM Collect a TomTom Flow snapshot along the WEH corridor.
REM Usage:  collect_peak.bat <label>       e.g.  collect_peak.bat pm_peak
REM Runs the cached collector with the repo venv; writes to data/raw/tomtom/collected/.

set LABEL=%1
if "%LABEL%"=="" set LABEL=run

cd /d "%~dp0.."
".venv\Scripts\python.exe" -m src.data.collect_flow --n 50 --label %LABEL%
