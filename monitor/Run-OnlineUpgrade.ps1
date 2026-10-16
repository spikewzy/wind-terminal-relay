# Independent task survives the old monitor stopping during its own update.
param(
    [Parameter(Mandatory=$true)][string]$AppDirectory,
    [Parameter(Mandatory=$true)][string]$OperationDirectory,
    [Parameter(Mandatory=$true)][string]$OperationId,
    [Parameter(Mandatory=$true)][string]$PythonExe
)
$ErrorActionPreference = 'Stop'
if ($OperationId -notmatch '^[a-f0-9]{32}$') { throw 'Invalid operation id.' }
$selection = Get-Content -LiteralPath (Join-Path $OperationDirectory 'selection.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($selection.operation_id -ne $OperationId -or [IO.Path]::GetFullPath($selection.app_directory) -ne [IO.Path]::GetFullPath($AppDirectory)) { throw 'Selection mismatch.' }
$resultFile = Join-Path $OperationDirectory 'result.json'
$result = @{ operation_id=$OperationId; state='failed'; error_code='UPDATE_RUNTIME_CHECK_FAILED' }
try {
    $updater = Join-Path $OperationDirectory 'package\Upgrade-Gateway.ps1'
    # A separate PowerShell process gives a reliable exit code even for exit 1.
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $updater -AppDirectory $AppDirectory -PythonExe $PythonExe -NoOpen *> (Join-Path $OperationDirectory 'installer.log')
    if ($LASTEXITCODE) { throw 'Gateway updater failed; inspect installer.log and the retained backups.' }
    $verified = $false
    for ($attempt=0; $attempt -lt 30; $attempt++) {
        try {
            $status = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/api/status' -TimeoutSec 2
            if ($status.diagnostics.version -eq $selection.console_version -and
                $status.deployment.gateway_version -eq $selection.gateway_version -and
                $status.deployment.gateway_build_id -eq $selection.build_id -and
                $status.deployment.gateway_runtime_verified -and -not $status.deployment.maintenance_mode) {
                $verified = $true
                break
            }
        } catch { }
        Start-Sleep -Seconds 2
    }
    if (-not $verified) { throw 'Running versions were not fully verified. Do not replay an upgrade automatically.' }
    $result.state = 'succeeded'
    $result.error_code = $null
} catch {
    # Never serialize raw exceptions, paths or tokens into the public result.
    # The private installer log and gateway backup remain for local inspection.
} finally {
    $result.completed_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $temporary = $resultFile+'.tmp'
    $result | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $resultFile -Force
}
if ($result.state -ne 'succeeded') { exit 1 }
