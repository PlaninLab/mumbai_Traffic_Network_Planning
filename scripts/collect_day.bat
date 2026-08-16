@echo off
REM Full-day WEH flow collection at segment-dependent intervals
REM (10 min in peak windows, 15 min otherwise). Writes to the SQLite store and
REM refreshes the dashboard summary after each reading.
REM
REM Usage:
REM     collect_day.bat                 run now until 23:59 IST, n=25
REM     collect_day.bat 30              same, 30 points per reading
REM     collect_day.bat 25 22:00        run until 22:00 IST, n=25
REM
REM Preview the schedule + daily API-call estimate without collecting:
REM     ".venv\Scripts\python.exe" -m src.data.collect_day --dry-run

set N=%1
if "%N%"=="" set N=25
set UNTIL=%2

cd /d "%~dp0.."
if "%UNTIL%"=="" (
  ".venv\Scripts\python.exe" -m src.data.collect_day --n %N%
) else (
  ".venv\Scripts\python.exe" -m src.data.collect_day --n %N% --until %UNTIL%
)
