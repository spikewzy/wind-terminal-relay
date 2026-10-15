# Independent monitor only; never modifies or restarts the gateway or worker.
# Run from an elevated PowerShell. Task runs after boot without a visible window.
$ErrorActionPreference = 'Stop'
$monitorDir = $PSScriptRoot
$appDir = Split-Path $monitorDir -Parent
$pythonExe = Join-Path $appDir '.venv\Scripts\python.exe'
if (-not (Test-Path $pythonExe)) {
    $pythonExe = Join-Path (Split-Path $appDir -Parent) 'Python\python.exe'
}
$gatewayConfig = Join-Path $appDir '.relay\gateway.json'
$probeConfig = Join-Path $appDir '.relay\client-owner.json'
$monitorScript = Join-Path $monitorDir 'monitor.py'
$monitorHtml = Join-Path $monitorDir 'index.html'
$diagnosticLauncher = Join-Path $monitorDir 'Export-Diagnostics.cmd'
$diagnosticModule = Join-Path $monitorDir 'diagnostics.py'
$taskName = 'WindRelayMonitor'
foreach ($file in @($pythonExe, $gatewayConfig, $probeConfig, $monitorScript, $monitorHtml, $diagnosticLauncher, $diagnosticModule)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw "Required file missing: $file. Extract monitor into the existing app directory."
    }
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open PowerShell as Administrator, then run this installer again.'
}
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw 'WindRelayMonitor already exists. Open Task Scheduler to check or run it; no second task was installed.'
}
if (Get-NetTCPConnection -State Listen -LocalPort 8766 -ErrorAction SilentlyContinue) {
    throw 'Port 8766 is already in use. Check the existing monitor first; nothing was changed.'
}
$arguments = '"{0}" --config "{1}" --client-config "{2}"' -f $monitorScript, $gatewayConfig, $probeConfig
$action = New-ScheduledTaskAction -Execute $pythonExe -Argument $arguments -WorkingDirectory $monitorDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = 'PT30S'
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$taskPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $taskPrincipal -Description 'Read-only local Wind relay status monitor' | Out-Null
Start-ScheduledTask -TaskName $taskName
$shortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) 'WindRelay-Monitor.url'
Set-Content -LiteralPath $shortcut -Encoding Ascii -Value "[InternetShortcut]`r`nURL=http://127.0.0.1:8766/"
$diagnosticShortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'WindRelay-Diagnostics.lnk'
$shortcutShell = New-Object -ComObject WScript.Shell
$diagnosticShortcut = $shortcutShell.CreateShortcut($diagnosticShortcutPath)
$diagnosticShortcut.TargetPath = $diagnosticLauncher
$diagnosticShortcut.WorkingDirectory = $monitorDir
$diagnosticShortcut.Save()
Write-Host 'Monitor task installed and start requested. Gateway and worker were not restarted.'
Write-Host 'Open http://127.0.0.1:8766/ in the browser ON THIS CLOUD COMPUTER.'
Write-Host 'Allow a few seconds, then confirm the page updates. A start request alone is not health verification.'
Start-Process 'http://127.0.0.1:8766/'
