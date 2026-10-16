# Updates ONLY the local console. Existing gateway, worker and credentials stay in place.
param(
    [string]$AppDirectory = 'C:\WindRelay\app',
    [string]$PythonExe = '',
    [switch]$CheckOnly,
    [switch]$NoOpen
)
$ErrorActionPreference = 'Stop'
$sourceRoot = $PSScriptRoot
$expectedVersion = '0.2.1'
$taskName = 'WindRelayMonitor'
$reviewedFiles = @('monitor/monitor.py','monitor/diagnostics.py','monitor/management.py',
    'monitor/index.html','monitor/Start.cmd','monitor/Install.ps1',
    'monitor/README.md','monitor/Export-Diagnostics.cmd','Update.cmd','Update.ps1','START-HERE.txt','LICENSE')
function Assert-SafePath([string]$Path) {
    $item = [IO.Path]::GetFullPath($Path)
    while ($item) {
        if ((Test-Path -LiteralPath $item) -and
            ((Get-Item -LiteralPath $item -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Refusing linked path: $item"
        }
        $parent = Split-Path $item -Parent
        if ($parent -eq $item) { break }
        $item = $parent
    }
}
$manifestFile = Join-Path $sourceRoot 'console-manifest.json'
Assert-SafePath $manifestFile
$manifest = Get-Content -LiteralPath $manifestFile -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.kind -ne 'wind_relay_console' -or $manifest.version -ne $expectedVersion) { throw 'Wrong console package/version.' }
$members = @($manifest.files.PSObject.Properties)
if ($members.Count -ne $reviewedFiles.Count) { throw 'Unexpected package file list.' }
foreach ($member in $members) {
    if ($member.Name -cnotin $reviewedFiles) { throw 'Unreviewed package file.' }
    $source = Join-Path $sourceRoot $member.Name
    Assert-SafePath $source
    if (-not (Test-Path -LiteralPath $source -PathType Leaf) -or
        (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $member.Value) {
        throw "Package integrity check failed: $($member.Name)"
    }
}
$AppDirectory = [IO.Path]::GetFullPath($AppDirectory)
Assert-SafePath $AppDirectory
$monitorDirectory = Join-Path $AppDirectory 'monitor'
$gatewayConfig = Join-Path $AppDirectory '.relay\gateway.json'
$probeConfig = Join-Path $AppDirectory '.relay\client-owner.json'
if (-not $PythonExe) {
    $PythonExe = Join-Path $AppDirectory '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $PythonExe)) { $PythonExe = Join-Path (Split-Path $AppDirectory -Parent) 'Python\python.exe' }
}
foreach ($file in @($gatewayConfig,$probeConfig,$PythonExe)) {
    Assert-SafePath $file
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required existing deployment file missing: $file" }
}
foreach ($member in $members | Where-Object { $_.Name.StartsWith('monitor/') }) {
    $target = Join-Path $AppDirectory $member.Name
    Assert-SafePath $target
    if ([IO.Path]::GetFullPath((Join-Path $sourceRoot $member.Name)) -eq [IO.Path]::GetFullPath($target)) {
        throw 'Extract the update ZIP into a separate folder, not directly over the app directory.'
    }
}
if ($CheckOnly) {
    Write-Host 'CHECK_ONLY_OK: package verified; no tasks, files, credentials or services changed.'
    exit 0
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $quoted = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -AppDirectory "{1}" -PythonExe "{2}"' -f $PSCommandPath,$AppDirectory,$PythonExe
    $child = Start-Process powershell.exe -Verb RunAs -ArgumentList $quoted -Wait -PassThru
    exit $child.ExitCode
}
$oldTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$scriptFile = Join-Path $monitorDirectory 'monitor.py'
if ($oldTask) {
    if (@($oldTask.Actions).Count -ne 1 -or $oldTask.Actions[0].Arguments -notlike ('*"'+$scriptFile+'"*')) {
        throw 'An unrelated task uses WindRelayMonitor. Nothing changed; inspect it first.'
    }
}
$backupDirectory = Join-Path $AppDirectory ('.console-updates\'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[guid]::NewGuid().ToString('N').Substring(0,8))
Assert-SafePath $backupDirectory
function Protect-Directory([string]$Directory,[string]$ReaderSid = '') {
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true,$false)
    foreach ($sid in @('S-1-5-18','S-1-5-32-544')) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule(
            (New-Object Security.Principal.SecurityIdentifier($sid)), 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    if ($ReaderSid) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule(
            (New-Object Security.Principal.SecurityIdentifier($ReaderSid)), 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Directory -AclObject $acl
}
Protect-Directory $backupDirectory
$codeMembers = @($members | Where-Object { $_.Name.StartsWith('monitor/') })
$existed = @{}
foreach ($member in $codeMembers) {
    $target = Join-Path $AppDirectory $member.Name
    $existed[$member.Name] = Test-Path -LiteralPath $target -PathType Leaf
    if ($existed[$member.Name]) {
        $backup = Join-Path $backupDirectory $member.Name
        New-Item -ItemType Directory -Path (Split-Path $backup -Parent) -Force | Out-Null
        Copy-Item -LiteralPath $target -Destination $backup
    }
}
$oldXml = $null
$wasRunning = $false
if ($oldTask) {
    $oldXml = Export-ScheduledTask -TaskName $taskName
    $oldXml | Set-Content -LiteralPath (Join-Path $backupDirectory 'task.xml') -Encoding UTF8
    $wasRunning = $oldTask.State -eq 'Running'
}
$changed = $false
$taskTouched = $false
try {
    if ($oldTask) {
        Stop-ScheduledTask -TaskName $taskName
        for ($attempt=0; $attempt -lt 15; $attempt++) {
            if ((Get-ScheduledTask -TaskName $taskName).State -ne 'Running') { break }
            Start-Sleep -Seconds 1
        }
        if ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running') { throw 'Old monitor did not stop. No code was replaced.' }
    }
    if (Get-NetTCPConnection -State Listen -LocalPort 8766 -ErrorAction SilentlyContinue) { throw 'Port 8766 is still occupied. No second console started.' }
    $changed = $true
    foreach ($member in $codeMembers) {
        $target = Join-Path $AppDirectory $member.Name
        New-Item -ItemType Directory -Path (Split-Path $target -Parent) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $sourceRoot $member.Name) -Destination $target -Force
    }
    $authDirectory = Join-Path $monitorDirectory 'auth'
    Assert-SafePath $authDirectory
    Protect-Directory $authDirectory $identity.User.Value
    Protect-Directory (Join-Path $AppDirectory '.relay\console-backups')
    $keyFile = Join-Path $authDirectory 'console.key'
    Assert-SafePath $keyFile
    if (-not (Test-Path -LiteralPath $keyFile)) {
        $bytes = New-Object byte[] 32
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        [IO.File]::WriteAllText($keyFile,[Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_'),[Text.Encoding]::ASCII)
    }
    if (([IO.File]::ReadAllText($keyFile)).Trim() -notmatch '^[A-Za-z0-9_-]{43,128}$') { throw 'Existing console key is invalid; it was not replaced.' }
    $arguments = '"{0}" --config "{1}" --client-config "{2}"' -f $scriptFile,$gatewayConfig,$probeConfig
    $action = New-ScheduledTaskAction -Execute $PythonExe -Argument $arguments -WorkingDirectory $monitorDirectory
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = 'PT30S'
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount
    $taskTouched = $true
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $taskPrincipal -Description 'Local Wind Relay Console: status, device management and safe diagnostics' -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    $ready = $false
    for ($attempt=0; $attempt -lt 15; $attempt++) {
        try {
            $status = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/api/status' -TimeoutSec 2
            if ($status.diagnostics.version -eq $expectedVersion) { $ready = $true; break }
        } catch { }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'Console did not pass its local version check; restoring the previous console.' }
    $desktop = [Environment]::GetFolderPath('Desktop')
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut((Join-Path $desktop 'WindRelay-Console.lnk'))
    $pythonWindowless = Join-Path (Split-Path $PythonExe -Parent) 'pythonw.exe'
    $shortcut.TargetPath = $(if (Test-Path -LiteralPath $pythonWindowless) { $pythonWindowless } else { $PythonExe })
    $shortcut.Arguments = '"{0}" --open-console' -f $scriptFile
    $shortcut.WorkingDirectory = $monitorDirectory
    $shortcut.Save()
    $diagnostic = $shell.CreateShortcut((Join-Path $desktop 'WindRelay-Diagnostics.lnk'))
    $diagnostic.TargetPath = Join-Path $monitorDirectory 'Export-Diagnostics.cmd'
    $diagnostic.WorkingDirectory = $monitorDirectory
    $diagnostic.Save()
    Write-Host "Console $expectedVersion updated and local version check passed."
    Write-Host "Backup: $backupDirectory"
    Write-Host 'Gateway and worker were not restarted. Open WindRelay-Console on this desktop.'
    if (-not $NoOpen) { Start-Process -FilePath $PythonExe -ArgumentList ('"{0}" --open-console' -f $scriptFile) }
} catch {
    $failure = $_
    if ($taskTouched) { Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue }
    if ($changed) {
        foreach ($member in $codeMembers) {
            $target = Join-Path $AppDirectory $member.Name
            if ($existed[$member.Name]) { Copy-Item -LiteralPath (Join-Path $backupDirectory $member.Name) -Destination $target -Force }
            elseif (Test-Path -LiteralPath $target -PathType Leaf) { Remove-Item -LiteralPath $target -Force }
        }
    }
    if ($taskTouched) {
        if ($oldXml) { Register-ScheduledTask -TaskName $taskName -Xml $oldXml -Force | Out-Null }
        else { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue }
    }
    if ($wasRunning) { Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue }
    throw $failure
}
