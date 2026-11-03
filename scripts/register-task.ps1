# Register a scheduled task that runs `python -m localdots tick` every N minutes (no admin rights needed).
#   powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 [-Minutes 15]
# Remove it with: Unregister-ScheduledTask -TaskName local-dots-tick
# (ASCII only: Windows PowerShell 5.1 misreads UTF-8 files without a BOM.)
param(
    [int]$Minutes = 15,
    [string]$TaskName = 'local-dots-tick'
)
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python -ErrorAction Stop).Source
# pythonw runs without a console window
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (Test-Path $pythonw) { $python = $pythonw }

$action = New-ScheduledTaskAction -Execute $python -Argument '-m localdots tick' -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "local-dots: run the dots that are due ($root)" -Force | Out-Null
Write-Host "Registered $TaskName (every $Minutes min, working folder $root). Log: $root\state\localdots.log"
