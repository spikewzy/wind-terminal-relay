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
$launcherPath = ''
foreach ($name in @('start_gateway.cmd','start-gateway.cmd')) {
    if ([string]::Equals([string]$oldAction.Execute,(Join-Path $AppDirectory $name),[StringComparison]::OrdinalIgnoreCase) -and -not $oldAction.Arguments) {
        $launcherPath = Join-Path $AppDirectory $name
    }
}
if ($launcherPath) {
    Assert-Safe $launcherPath
    if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) { throw 'EXISTING_LAUNCHER_NOT_FOUND' }
    Write-Host 'Keeping the existing scheduled task and its stored credentials; switching only its launcher file.'
} elseif ([string]$oldTask.Principal.LogonType -in @('Password','InteractiveOrPassword','1','6')) {
    # Updating a password-logon task requires resupplying Windows credentials.
    # Never discover that after stopping the working gateway or change its identity.
    throw 'PASSWORD_TASK_NEEDS_STABLE_LAUNCHER: no service was stopped; retain the task and use a stable start_gateway.cmd launcher.'
}
$marker = Join-Path $AppDirectory '.relay\gateway-maintenance.json'
Assert-Safe $marker
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
$launcherBackup = Join-Path $backup 'launcher.cmd'
$oldLauncherHash = ''
if ($launcherPath) {
    $oldLauncherHash = (Get-FileHash -LiteralPath $launcherPath).Hash
    Copy-Item -LiteralPath $launcherPath -Destination $launcherBackup
}
function Replace-Launcher([byte[]]$Bytes) {
    $temporary = $launcherPath+'.relay-'+[guid]::NewGuid().ToString('N')+'.tmp'
    Assert-Safe $temporary
    try {
        [IO.File]::WriteAllBytes($temporary,$Bytes)
        [IO.File]::Replace($temporary,$launcherPath,$null)
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}
function Same-Action($Left,$Right) {
    return ([string]$Left.Execute -ceq [string]$Right.Execute -and [string]$Left.Arguments -ceq [string]$Right.Arguments -and [string]$Left.WorkingDirectory -ceq [string]$Right.WorkingDirectory)
}
function Quote-Literal([string]$Value) { return "'"+$Value.Replace("'","''")+"'" }
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
$switchAttempted = $false
$newLauncherHash = ''
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
    if (Test-Path -LiteralPath $marker) {
        if (-not $launcherPath) { throw 'PRIOR_UPGRADE_REQUIRES_INSPECTION: maintenance marker retained.' }
        # Only the known 0.1.3 failure, with a running unversioned gateway and
        # an idle DB, may be archived. No database rollback or task change here.
        $recovery = Invoke-Helper 'recover-marker' @('--launcher',$launcherPath)
        Write-Host ('Prior 0.1.3 marker preserved in archive: '+$recovery.archive_name)
    }
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
    if ($launcherPath) {
        if ((Get-FileHash -LiteralPath $launcherPath).Hash -ne $oldLauncherHash) { throw 'EXISTING_LAUNCHER_CHANGED' }
        $command = 'Set-Location -LiteralPath '+(Quote-Literal $release)+'; & '+(Quote-Literal $newPython)+' '+(Quote-Literal (Join-Path $release 'relay_gateway.py'))+' --config '+(Quote-Literal $configFile)+' --maintenance-file '+(Quote-Literal $marker)+'; exit $LASTEXITCODE'
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
        $batch = '@echo off'+"`r`n"+'@rem WindRelay stable gateway launcher; scheduled task credentials retained.'+"`r`n"+'"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -EncodedCommand '+$encoded+"`r`n"+'exit /b %errorlevel%'+"`r`n"
        $launcherBytes = [Text.Encoding]::ASCII.GetBytes($batch)
        $hasher = [Security.Cryptography.SHA256]::Create()
        try { $newLauncherHash = ([BitConverter]::ToString($hasher.ComputeHash($launcherBytes))).Replace('-','') } finally { $hasher.Dispose() }
        $switchAttempted = $true
        Replace-Launcher $launcherBytes
    } else {
        # Only non-password tasks reach this path; password tasks above retain
        # their existing action and use an atomic launcher-file replacement.
        $switchAttempted = $true
        Set-ScheduledTask -TaskName $taskName -Action $action | Out-Null
    }
    $switched = $true
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
    Write-Warning ('Original upgrade failure: '+$failure.Exception.Message)
    if (-not $stopped) { throw $failure }
    # Once maintenance is removed, clients may already have submitted new jobs.
    # Never replace the DB after that point, even if a report/window step failed.
    if ($switched -and -not (Test-Path -LiteralPath $marker)) {
        Write-Warning 'Activation may already have completed. Database was NOT rolled back. Inspect deployment report/status.'
        throw $failure
    }
    try {
        if ($switchAttempted) {
            Stop-Gateway
            if ($switched -and $backupReady) { $restored = Invoke-Helper 'restore' @('--directory',$backup) }
            if ($launcherPath) {
                $currentLauncherHash = (Get-FileHash -LiteralPath $launcherPath).Hash
                if ($currentLauncherHash -eq $newLauncherHash) {
                    Replace-Launcher ([IO.File]::ReadAllBytes($launcherBackup))
                } elseif ($currentLauncherHash -ne $oldLauncherHash) { throw 'LAUNCHER_RECOVERY_CONFLICT' }
            } elseif (-not (Same-Action (Get-ScheduledTask -TaskName $taskName).Actions[0] $oldAction)) {
                Set-ScheduledTask -TaskName $taskName -Action $oldTask.Actions | Out-Null
            }
        }
        if ($backupReady -and (Test-Path -LiteralPath $marker)) {
            $failedMarker = Get-Content -Raw -LiteralPath $marker | ConvertFrom-Json
            if ($failedMarker.build_id -ne $build) { throw 'MAINTENANCE_MARKER_MISMATCH' }
            Move-Item -LiteralPath $marker -Destination (Join-Path $backup 'failed-maintenance.json') -ErrorAction Stop
        }
        if ($stopped) {
            Enable-ScheduledTask -TaskName $taskName | Out-Null
            if ($wasRunning) { Start-ScheduledTask -TaskName $taskName }
        }
        $restoredVerified = -not $wasRunning
        if ($wasRunning) {
            for ($attempt=0; $attempt -lt 15; $attempt++) {
                $restoredReport = Invoke-Helper 'report'
                if ($restoredReport.gateway.runtime_verified -and $restoredReport.gateway.version -eq $before.gateway.version -and $restoredReport.gateway.build_id -eq $before.gateway.build_id -and -not $restoredReport.gateway.maintenance_mode) { $restoredVerified=$true; break }
                Start-Sleep -Seconds 1
            }
        }
        if (-not $restoredVerified) { throw 'OLD_GATEWAY_RECOVERY_NOT_VERIFIED' }
        Write-Host 'Old gateway restored and checked; existing jobs and credentials retained. Keep the original error and backup folder.'
    } catch {
        Write-Warning ('Recovery also needs inspection: '+$_.Exception.Message+'. Original failure was printed above; all recovery files are retained.')
    }
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
