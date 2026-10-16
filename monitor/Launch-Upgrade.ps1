# Called only after a local authenticated administrator requests an update.
param(
    [Parameter(Mandatory=$true)][string]$AppDirectory,
    [Parameter(Mandatory=$true)][string]$OperationDirectory,
    [Parameter(Mandatory=$true)][string]$OperationId,
    [Parameter(Mandatory=$true)][string]$PythonExe
)
$ErrorActionPreference = 'Stop'
if ($OperationId -notmatch '^[a-f0-9]{32}$') { throw 'Invalid operation id.' }
$expected = [IO.Path]::GetFullPath((Join-Path $AppDirectory ('.relay\online-updates\'+$OperationId)))
if ([IO.Path]::GetFullPath($OperationDirectory) -ne $expected) { throw 'Invalid operation directory.' }
$runner = Join-Path $OperationDirectory 'Run-OnlineUpgrade.ps1'
$taskName = 'WindRelayUpgrade'
$old = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($old) {
    if ($old.State -eq 'Running') { throw 'An update is already running.' }
    $prefix = Join-Path $AppDirectory '.relay\online-updates\'
    if (@($old.Actions).Count -ne 1 -or $old.Actions[0].Arguments.IndexOf($prefix,[StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw 'An unrelated task uses the update task name.'
    }
}
$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -AppDirectory "{1}" -OperationDirectory "{2}" -OperationId "{3}" -PythonExe "{4}"' -f $runner,$AppDirectory,$OperationDirectory,$OperationId,$PythonExe
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $OperationDirectory
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount
# No automatic retries: a partial/uncertain upgrade must be inspected first.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Settings $settings -Principal $principal -Description 'Explicitly requested Wind relay upgrade; independent of the console service' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host 'UPDATE_TASK_STARTED'
