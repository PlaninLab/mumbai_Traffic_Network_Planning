<#
    register_weekday_tasks.ps1 — schedule automatic weekday segment collection.

    Registers three Windows Task Scheduler jobs that run Mon–Fri and call
    collect_segment.bat, so the two planning segments fill in on their own:

        MumbaiTraffic-Peak-AM   09:00  ->  peak  (morning office peak)
        MumbaiTraffic-Peak-PM   18:45  ->  peak  (evening office peak)
        MumbaiTraffic-Avg       14:00  ->  avg   (inter-peak average delay)

    Each run also refreshes data/processed/segment_overview.json.

    Times are local machine time — set the machine clock / times to IST so the
    windows in src/data/segments.py line up. Run this script from an ADMIN
    PowerShell once. Remove the jobs with -Remove.

    Modes:
        (default)  three fixed segment readings/day  (peak AM, peak PM, avg)
        -FullDay   one 06:00 launch of the full-day adaptive loop (10-min peak /
                   15-min off-peak sampling until 23:00) — finer resolution

    Usage:
        powershell -ExecutionPolicy Bypass -File scripts\register_weekday_tasks.ps1
        powershell -ExecutionPolicy Bypass -File scripts\register_weekday_tasks.ps1 -FullDay
        powershell -ExecutionPolicy Bypass -File scripts\register_weekday_tasks.ps1 -Remove
#>
param([switch]$Remove, [switch]$FullDay)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$SegBat = Join-Path $RepoRoot "scripts\collect_segment.bat"
$DayBat = Join-Path $RepoRoot "scripts\collect_day.bat"
$Weekdays = @("Monday","Tuesday","Wednesday","Thursday","Friday")

# Job set depends on the mode.
if ($FullDay) {
    $Jobs = @(
        @{ Name = "MumbaiTraffic-FullDay"; Bat = $DayBat; Arg = "25 23:00"; At = "06:00";
           Limit = 18; Desc = "full-day adaptive 10/15-min collection" }
    )
} else {
    $Jobs = @(
        @{ Name = "MumbaiTraffic-Peak-AM"; Bat = $SegBat; Arg = "peak"; At = "09:00"; Limit = 0.25; Desc = "peak collection" },
        @{ Name = "MumbaiTraffic-Peak-PM"; Bat = $SegBat; Arg = "peak"; At = "18:45"; Limit = 0.25; Desc = "peak collection" },
        @{ Name = "MumbaiTraffic-Avg";     Bat = $SegBat; Arg = "avg";  At = "14:00"; Limit = 0.25; Desc = "avg collection" }
    )
}

# Always clear ALL of our task names first (so switching modes is clean).
$AllNames = @("MumbaiTraffic-Peak-AM","MumbaiTraffic-Peak-PM","MumbaiTraffic-Avg","MumbaiTraffic-FullDay")
foreach ($n in $AllNames) {
    if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $n -Confirm:$false
        Write-Host "Removed existing task $n"
    }
}
if ($Remove) { Write-Host "`nAll Mumbai Traffic weekday tasks removed."; return }

foreach ($j in $Jobs) {
    $action  = New-ScheduledTaskAction -Execute "cmd.exe" `
                   -Argument "/c `"$($j.Bat)`" $($j.Arg)" -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Weekdays -At $j.At
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                   -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours $j.Limit)
    Register-ScheduledTask -TaskName $j.Name -Action $action -Trigger $trigger `
        -Settings $settings -Description "Mumbai Traffic weekday $($j.Desc)" | Out-Null
    Write-Host "Registered $($j.Name): '$($j.Arg)' at $($j.At) Mon-Fri"
}

Write-Host "`nDone. Inspect with:  Get-ScheduledTask -TaskName 'MumbaiTraffic-*'"
if ($FullDay) {
    Write-Host "Preview the day plan: .venv\Scripts\python.exe -m src.data.collect_day --dry-run"
} else {
    Write-Host "Run one now with:     Start-ScheduledTask -TaskName 'MumbaiTraffic-Avg'"
}
