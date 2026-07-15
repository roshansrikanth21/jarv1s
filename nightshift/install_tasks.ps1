<#
  Night Shift - scheduler installer (Windows Task Scheduler).

  Creates two tasks:
    JarvisNightShift        run_hour.ps1  hourly, 10:00 PM..6:00 AM, wakes the machine
    JarvisNightShiftReport  report.py     daily at 6:45 AM (after the final 6 AM slot)

  Run this ONCE from an elevated PowerShell (Task Scheduler registration needs admin):
      powershell -ExecutionPolicy Bypass -File .\install_tasks.ps1
  Uninstall:
      powershell -ExecutionPolicy Bypass -File .\install_tasks.ps1 -Remove

  Idempotent: re-running replaces the tasks. Do NOT let the schedule fire until you have run
  the trust test in README.md.
#>
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$NsDir  = $PSScriptRoot
$Python = if ($env:NS_PYTHON) { $env:NS_PYTHON } else { 'C:\Users\rosha\venv\Scripts\python.exe' }
$RunTask    = 'JarvisNightShift'
$ReportTask = 'JarvisNightShiftReport'

if ($Remove) {
  foreach ($t in @($RunTask, $ReportTask)) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
      Unregister-ScheduledTask -TaskName $t -Confirm:$false
      Write-Host "removed $t"
    }
  }
  return
}

# -- hourly runner: 10 PM..6 AM ------------------------------------------------------
$runAction = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument ("-NonInteractive -ExecutionPolicy Bypass -File `"{0}`"" -f (Join-Path $NsDir 'run_hour.ps1'))

# Fire at 10 PM and repeat every hour for 8 hours (10,11,12,1,2,3,4,5,6 AM => the 6 AM slot is the last).
$runTrigger = New-ScheduledTaskTrigger -Daily -At 10:00PM
$runTrigger.Repetition = (New-ScheduledTaskTrigger -Once -At 10:00PM `
  -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Hours 8)).Repetition

# WakeToRun + start-when-available so a sleeping machine still works the night; never run two at once.
$runSettings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 55) `
  -DontStopOnIdleEnd -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $RunTask -Action $runAction -Trigger $runTrigger `
  -Settings $runSettings -Description 'Night Shift hourly worker (10 PM-6 AM)' -Force | Out-Null
Write-Host "installed $RunTask (hourly 10 PM-6 AM, wakes machine)"

# -- morning report: 6:45 AM daily ----------------------------------------------------
$repAction = New-ScheduledTaskAction -Execute $Python `
  -Argument ("`"{0}`"" -f (Join-Path $NsDir 'report.py')) -WorkingDirectory $NsDir
$repTrigger  = New-ScheduledTaskTrigger -Daily -At 6:45AM
$repSettings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $ReportTask -Action $repAction -Trigger $repTrigger `
  -Settings $repSettings -Description 'Night Shift morning report (6:45 AM)' -Force | Out-Null
Write-Host "installed $ReportTask (daily 6:45 AM)"

Write-Host ""
Write-Host "Done. Load work into PROJECTS.md, then run the trust test in README.md before"
Write-Host "trusting the first real night. Remove with:  install_tasks.ps1 -Remove"
