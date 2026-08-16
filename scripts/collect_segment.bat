@echo off
REM Collect a weekday planning-segment reading, then refresh the segment summary.
REM
REM Usage:
REM     collect_segment.bat peak            (AM/PM office-peak reading)
REM     collect_segment.bat avg             (inter-peak average-delay reading)
REM     collect_segment.bat peak --force    (record even outside the window)
REM
REM Writes data/raw/tomtom/collected/flow_<segment>_<timestamp>.csv and rebuilds
REM data/processed/segment_overview.json (which the web dashboard reads).

set SEG=%1
if "%SEG%"=="" (
  echo Usage: collect_segment.bat ^<peak^|avg^> [--force]
  exit /b 1
)
set EXTRA=%2

cd /d "%~dp0.."
".venv\Scripts\python.exe" -m src.data.collect_flow --n 50 --segment %SEG% %EXTRA%
if errorlevel 1 exit /b %errorlevel%
".venv\Scripts\python.exe" -m src.data.segment_summary
