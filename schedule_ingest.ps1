<#
  Registers a twice-daily Windows scheduled task for Second Source ingestion.

  Runs 07:00 and 19:00 local. Twice daily because the shallowest feeds
  (Florida's Voice, NSF) only expose ~2 days of items -- a longer outage
  loses their coverage permanently.

  Runs only while you are logged on, so no stored password is required.
  StartWhenAvailable catches up a run missed while the machine was off.

  Usage:  powershell -ExecutionPolicy Bypass -File .\schedule_ingest.ps1
  Remove: Unregister-ScheduledTask -TaskName 'SecondSource Ingest' -Confirm:$false
#>

$ErrorActionPreference = 'Stop'

$base   = Split-Path -Parent $MyInvocation.MyCommand.Path
$py     = Join-Path $base '.venv\Scripts\python.exe'
$script = Join-Path $base 'ingest.py'
$log    = Join-Path $base 'data\ingest.log'

if (-not (Test-Path $py))     { throw "venv python not found at $py - create it with: py -3 -m venv .venv" }
if (-not (Test-Path $script)) { throw "ingest.py not found at $script" }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $log) | Out-Null

# Out-File -Encoding utf8 rather than *>> : PowerShell 5.1 redirection writes
# UTF-16, which makes the log unreadable to grep and to anything parsing it later.
$inner = "Add-Content -Path '$log' -Encoding utf8 -Value ('--- run ' + (Get-Date -Format s)); & '$py' '$script' 2>&1 | Out-File -FilePath '$log' -Append -Encoding utf8"
$arg   = "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"$inner`""

$action   = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg -WorkingDirectory $base
$triggers = @(
    (New-ScheduledTaskTrigger -Daily -At 7:00AM),
    (New-ScheduledTaskTrigger -Daily -At 7:00PM)
)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName    'SecondSource Ingest' `
    -Action      $action `
    -Trigger     $triggers `
    -Settings    $settings `
    -Description 'Second Source: twice-daily RSS ingestion into local SQLite.' `
    -Force | Out-Null

Write-Host "Registered 'SecondSource Ingest'."
Get-ScheduledTask -TaskName 'SecondSource Ingest' | Select-Object TaskName, State | Format-Table -AutoSize
Write-Host "Log: $log"
Write-Host "Run once now to verify:  Start-ScheduledTask -TaskName 'SecondSource Ingest'"
