# Existing Windows deployment upgrade. No Wind/worker restart, credential reset or task replay.
param(
    [string]$AppDirectory = 'C:\WindRelay\app',
    [string]$PythonExe = '',
    [switch]$CheckOnly,
    [switch]$SkipConsole,
    [switch]$NoOpen
)
$ErrorActionPreference = 'Stop'
$sourceRoot = $PSScriptRoot
$taskName = 'WindRelayGateway'
$helper = Join-Path $sourceRoot 'gateway_update\support.py'
$AppDirectory = [IO.Path]::GetFullPath($AppDirectory)
function Assert-Safe([string]$Path) {
    $item = [IO.Path]::GetFullPath($Path)
    while ($item) {
        if ((Test-Path -LiteralPath $item) -and ((Get-Item -LiteralPath $item -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'UNSAFE_LINKED_PATH' }
        $parent = Split-Path $item -Parent
        if ($parent -eq $item) { break }
        $item = $parent
    }
}
Assert-Safe $AppDirectory
Assert-Safe $helper
if (-not $PythonExe) {
    $PythonExe = Join-Path $AppDirectory '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $PythonExe)) { $PythonExe = Join-Path (Split-Path $AppDirectory -Parent) 'Python\python.exe' }
}
Assert-Safe $PythonExe
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw 'EXISTING_PYTHON_NOT_FOUND' }
function Invoke-Helper([string]$Operation, [string[]]$Extra = @()) {
    $result = & $PythonExe $helper $Operation --root $sourceRoot --app $AppDirectory @Extra
    if ($LASTEXITCODE) { throw "UPGRADE_HELPER_FAILED: $Operation" }
    return ($result | ConvertFrom-Json)
}
$package = Invoke-Helper 'verify'
$preflight = Invoke-Helper 'inspect'
if ($CheckOnly) {
    Write-Host 'CHECK_ONLY_OK: package, contract, database and queue checked; no files, services or tasks changed.'
    exit 0
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $quoted = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -AppDirectory "{1}" -PythonExe "{2}"' -f $PSCommandPath,$AppDirectory,$PythonExe
    if ($SkipConsole) { $quoted += ' -SkipConsole' }
    if ($NoOpen) { $quoted += ' -NoOpen' }
    $child = Start-Process powershell.exe -Verb RunAs -ArgumentList $quoted -Wait -PassThru
    exit $child.ExitCode
}
function Protect-Directory([string]$Directory) {
    Assert-Safe $Directory
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true,$false)
    foreach ($sid in @('S-1-5-18','S-1-5-32-544')) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier($sid)), 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Directory -AclObject $acl
}
$oldTask = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
if (-not $oldTask.Settings.Enabled -or @($oldTask.Actions).Count -ne 1) { throw 'EXISTING_GATEWAY_TASK_NOT_SUPPORTED' }
$oldAction = $oldTask.Actions[0]
$oldCommand = [string]$oldAction.Execute + ' ' + [string]$oldAction.Arguments
$recognized = $false
foreach ($name in @('start_gateway.cmd','start-gateway.cmd','relay_gateway.py')) {
    if ($oldCommand.IndexOf((Join-Path $AppDirectory $name),[StringComparison]::OrdinalIgnoreCase) -ge 0) { $recognized = $true }
}
if ($oldCommand.IndexOf((Join-Path $AppDirectory '.gateway-releases\'),[StringComparison]::OrdinalIgnoreCase) -ge 0 -and $oldCommand.Contains('relay_gateway.py')) { $recognized = $true }
if (-not $recognized) { throw 'GATEWAY_TASK_POINTS_TO_UNRECOGNIZED_PROGRAM' }
$marker = Join-Path $AppDirectory '.relay\gateway-maintenance.json'
Assert-Safe $marker
if (Test-Path -LiteralPath $marker) { throw 'PRIOR_UPGRADE_REQUIRES_INSPECTION: maintenance marker retained.' }
$version = $package.version
$build = $package.build_id
$release = Join-Path $AppDirectory ('.gateway-releases\'+$version+'-'+$build.Substring(0,12))
Protect-Directory $release
# Explicit package allowlist; never copy arbitrary extra files from extracted ZIPs.
foreach ($entry in $package.files.PSObject.Properties | Where-Object { $_.Name.StartsWith('gateway/') }) {
    $relative = $entry.Name.Substring(8)
    $target = Join-Path $release $relative
    Assert-Safe $target
    if (Test-Path -LiteralPath $target) {
        if ((Get-FileHash -LiteralPath $target).Hash.ToLowerInvariant() -ne $entry.Value) { throw 'EXISTING_RELEASE_FINGERPRINT_MISMATCH' }
    } else {
        New-Item -ItemType Directory -Path (Split-Path $target -Parent) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $sourceRoot $entry.Name) -Destination $target
    }
}
$newPython = $PythonExe
if (-not $preflight.environment.compatible) {
    Write-Host 'Preparing an isolated gateway Python environment before stopping the old gateway...'
    $venv = Join-Path $release '.venv'
    Assert-Safe $venv
    if (-not (Test-Path -LiteralPath (Join-Path $venv 'Scripts\python.exe'))) {
        & $PythonExe -m venv $venv
        if ($LASTEXITCODE) { throw 'PRIVATE_ENVIRONMENT_CREATION_FAILED' }
    }
    $newPython = Join-Path $venv 'Scripts\python.exe'
    & $newPython -m pip install --disable-pip-version-check -r (Join-Path $release 'requirements-gateway.lock.txt')
    if ($LASTEXITCODE) { throw 'DEPENDENCY_PREPARATION_FAILED: old gateway was not stopped.' }
    $checked = & $newPython $helper environment --root $sourceRoot | ConvertFrom-Json
    if ($LASTEXITCODE -or -not $checked.compatible) { throw 'PRIVATE_ENVIRONMENT_CHECK_FAILED' }
}
$operation = (Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[guid]::NewGuid().ToString('N').Substring(0,8)
$backup = Join-Path $AppDirectory ('.gateway-backups\'+$operation)
Protect-Directory $backup
$oldXml = Export-ScheduledTask -TaskName $taskName
$oldXml | Set-Content -LiteralPath (Join-Path $backup 'task.xml') -Encoding UTF8
$reportFile = Join-Path ([Environment]::GetFolderPath('UserProfile')) ('Downloads\wind-relay-deployment-'+$operation+'.json')
Assert-Safe $reportFile
New-Item -ItemType Directory -Path (Split-Path $reportFile -Parent) -Force | Out-Null
$privateReport = Join-Path $AppDirectory '.relay\deployment-current.json'
$configFile = Join-Path $AppDirectory '.relay\gateway.json'
$ownerFile = Join-Path $AppDirectory '.relay\client-owner.json'
$lockPath = $configFile+'.admin.lock'
Assert-Safe $lockPath
$lock = [IO.File]::Open($lockPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::ReadWrite)
if ($lock.Length -eq 0) { $lock.WriteByte(0); $lock.Flush() }
try { $lock.Lock(0,1) } catch { $lock.Dispose(); throw 'ANOTHER_ADMINISTRATOR_IS_CHANGING_CONFIGURATION' }
$wasRunning = $oldTask.State -eq 'Running'
$stopped = $false
$backupReady = $false
$switched = $false
$activated = $false
function Stop-Gateway {
    Disable-ScheduledTask -TaskName $taskName | Out-Null
    Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop
    for ($attempt=0; $attempt -lt 15; $attempt++) {
        $listening = Get-NetTCPConnection -State Listen -LocalPort 8765 -ErrorAction SilentlyContinue
        if (-not $listening -and (Get-ScheduledTask -TaskName $taskName).State -ne 'Running') { return }
        Start-Sleep -Seconds 1
    }
    throw 'GATEWAY_DID_NOT_STOP: no other process was killed.'
}
try {
    $configHash = (Get-FileHash -LiteralPath $configFile).Hash
    $ownerHash = (Get-FileHash -LiteralPath $ownerFile).Hash
    $checked = Invoke-Helper 'inspect'
    $before = Invoke-Helper 'report'
    $before | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $backup 'before.json') -Encoding UTF8
    Write-Host 'Pausing the gateway briefly. Wind and the physical execution worker stay running.'
    $stopped = $true
    Stop-Gateway
    # A request accepted between the preflight and stop is caught here. If any
    # task exists, restore the old listener without migrating or dropping it.
    $saved = Invoke-Helper 'backup' @('--directory',$backup)
    $backupReady = $true
    [IO.File]::WriteAllText($marker,('{"build_id":"'+$build+'"}'),[Text.Encoding]::ASCII)
    $arguments = '"{0}" --config "{1}" --maintenance-file "{2}"' -f (Join-Path $release 'relay_gateway.py'),$configFile,$marker
    $action = New-ScheduledTaskAction -Execute $newPython -Argument $arguments -WorkingDirectory $release
    # Set only Actions; retain the original task identity, stored password,
    # triggers and settings so rollback never asks for an administrator password.
    $switched = $true
    Set-ScheduledTask -TaskName $taskName -Action $action | Out-Null
    Enable-ScheduledTask -TaskName $taskName | Out-Null
    Start-ScheduledTask -TaskName $taskName
    $healthy = $false
    for ($attempt=0; $attempt -lt 15; $attempt++) {
        $snapshot = Invoke-Helper 'report'
        if ($snapshot.gateway.runtime_verified -and $snapshot.gateway.build_id -eq $build -and $snapshot.gateway.maintenance_mode) { $healthy = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $healthy) { throw 'NEW_GATEWAY_RUNTIME_CHECK_FAILED' }
    if ((Get-FileHash -LiteralPath $configFile).Hash -ne $configHash -or (Get-FileHash -LiteralPath $ownerFile).Hash -ne $ownerHash) { throw 'EXISTING_CREDENTIALS_CHANGED' }
    $ready = Invoke-Helper 'finalize' @('--build-id',$build,'--output',$reportFile)
    $activated = $ready.activated
    if (-not $activated) { throw 'GATEWAY_ACTIVATION_NOT_CONFIRMED' }
    if (-not $ready.deployment_record_saved) { Write-Warning 'Gateway activated; final report save needs inspection. Database will not be rolled back.' }
    Write-Host "Gateway $version is running. Device rotation and queue limits verified."
    Write-Host "Private rollback backup: $backup"
    Write-Host "Deployment report: $reportFile"
} catch {
    $failure = $_
    # Once maintenance is removed, clients may already have submitted new jobs.
    # Never replace the DB after that point, even if a report/window step failed.
    if ($switched -and -not (Test-Path -LiteralPath $marker)) {
        Write-Warning 'Activation may already have completed. Database was NOT rolled back. Inspect deployment report/status.'
        throw $failure
    }
    if ($switched) {
        Stop-Gateway
        if ($backupReady) { $restored = Invoke-Helper 'restore' @('--directory',$backup) }
        Set-ScheduledTask -TaskName $taskName -Action $oldTask.Actions | Out-Null
    }
    if ($stopped) {
        Enable-ScheduledTask -TaskName $taskName | Out-Null
        if ($wasRunning) { Start-ScheduledTask -TaskName $taskName }
    }
    if ($backupReady -and (Test-Path -LiteralPath $marker)) { Remove-Item -LiteralPath $marker -Force }
    Write-Host 'Old gateway task restored; existing jobs and credentials retained. Keep the error and backup folder.'
    throw $failure
} finally {
    $lock.Unlock(0,1)
    $lock.Dispose()
}
if (-not $SkipConsole) {
    try {
        if ($NoOpen) { & (Join-Path $sourceRoot 'Update.ps1') -AppDirectory $AppDirectory -PythonExe $PythonExe -NoOpen }
        else { & (Join-Path $sourceRoot 'Update.ps1') -AppDirectory $AppDirectory -PythonExe $PythonExe }
    } catch {
        Write-Warning 'Gateway upgrade succeeded, but console update needs inspection. Gateway was not rolled back.'
    }
}
# Fresh snapshot after activation/optional console update. This step cannot
# restore the database or discard newly accepted work.
try {
    $freshReport = Join-Path ([Environment]::GetFolderPath('UserProfile')) ('Downloads\wind-relay-deployment-current-'+$operation+'.json')
    $fresh = Invoke-Helper 'report' @('--output',$freshReport)
    Write-Host "Current deployment report: $freshReport"
} catch { Write-Warning 'Could not export the final report; use the console deployment report button.' }
Write-Host 'DONE. Colleagues can resume submitting tasks using their existing JSON files.'
