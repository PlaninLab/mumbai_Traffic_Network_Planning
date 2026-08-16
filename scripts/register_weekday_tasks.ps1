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

    Usage:
        powershell -ExecutionPolicy Bypass -File scripts\register_weekday_tasks.ps1
        powershell -ExecutionPolicy Bypass -File scripts\register_weekday_tasks.ps1 -Remove
#>
param([switch]$Remove)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Bat = Join-Path $RepoRoot "scripts\collect_segment.bat"

$Jobs = @(
    @{ Name = "MumbaiTraffic-Peak-AM"; Seg = "peak"; At = "09:00" },
    @{ Name = "MumbaiTraffic-Peak-PM"; Seg = "peak"; At = "18:45" },
    @{ Name = "MumbaiTraffic-Avg";     Seg = "avg";  At = "14:00" }
)

$Weekdays = @("Monday","Tuesday","Wednesday","Thursday","Friday")

foreach ($j in $Jobs) {
    if (Get-ScheduledTask -TaskName $j.Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $j.Name -Confirm:$false
        Write-Host "Removed existing task $($j.Name)"
    }
    if ($Remove) { continue }

    $action  = New-ScheduledTaskAction -Execute "cmd.exe" `
                   -Argument "/c `"$Bat`" $($j.Seg)" -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Weekdays -At $j.At
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                   -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
    Register-ScheduledTask -TaskName $j.Name -Action $action -Trigger $trigger `
        -Settings $settings -Description "Mumbai Traffic weekday $($j.Seg) collection" | Out-Null
    Write-Host "Registered $($j.Name): $($j.Seg) at $($j.At) Mon-Fri"
}

if ($Remove) {
    Write-Host "`nAll Mumbai Traffic weekday tasks removed."
} else {
    Write-Host "`nDone. Inspect with:  Get-ScheduledTask -TaskName 'MumbaiTraffic-*'"
    Write-Host "Run one now with:     Start-ScheduledTask -TaskName 'MumbaiTraffic-Avg'"
}
