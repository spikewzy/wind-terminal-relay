# Run only on an isolated GitHub Windows runner. No Wind or real credentials.
$ErrorActionPreference = 'Stop'
$python = (Get-Command python).Source
$fixtureRoot = Join-Path $env:RUNNER_TEMP ('console-smoke-'+[guid]::NewGuid().ToString('N'))
$packageRoot = Join-Path $fixtureRoot 'package'
$app = Join-Path $fixtureRoot 'app'
New-Item -ItemType Directory -Path (Join-Path $app '.relay') -Force | Out-Null
Expand-Archive -LiteralPath (Join-Path $PWD 'dist/wind-relay-console-0.2.0.zip') -DestinationPath $packageRoot
$setup = @'
import hashlib,json,pathlib,sqlite3,sys
root=pathlib.Path(sys.argv[1])/'.relay'
token='SYNTHETIC_WINDOWS_OWNER'
(root/'gateway.json').write_text(json.dumps({'database':'queue.sqlite','clients':{'owner':hashlib.sha256(token.encode()).hexdigest()},'worker_sha256':'a'*64}))
(root/'client-owner.json').write_text(json.dumps({'url':'https://example.invalid','token':token}))
con=sqlite3.connect(root/'queue.sqlite')
con.executescript('CREATE TABLE jobs(id TEXT PRIMARY KEY,owner TEXT,payload TEXT,state TEXT,created REAL,updated REAL,lease TEXT,lease_until REAL,result TEXT); CREATE TABLE worker(id INTEGER PRIMARY KEY,seen REAL,mode TEXT);')
con.close()
'@
& $python -c $setup $app
if ($LASTEXITCODE) { throw 'Synthetic fixture failed.' }
$ownerHash = (Get-FileHash (Join-Path $app '.relay/client-owner.json')).Hash
$gatewayHash = (Get-FileHash (Join-Path $app '.relay/gateway.json')).Hash
$updater = Join-Path $packageRoot 'Update.ps1'
& $updater -AppDirectory $app -PythonExe $python -CheckOnly
if (Test-Path (Join-Path $app 'monitor')) { throw 'CheckOnly wrote files.' }
try {
    & $updater -AppDirectory $app -PythonExe $python -NoOpen
    $keyFile = Join-Path $app 'monitor/auth/console.key'
    $key = (Get-Content -Raw $keyFile).Trim()
    $headers = @{ 'X-Console-Token' = $key }
    $status = Invoke-RestMethod 'http://127.0.0.1:8766/api/status'
    if ($status.diagnostics.version -ne '0.2.0') { throw 'Wrong installed console version.' }
    $devices = Invoke-RestMethod 'http://127.0.0.1:8766/api/manage/devices' -Headers $headers
    if ($devices.clients[0].client_id -ne 'owner') { throw 'Management endpoint did not initialize.' }
    & $updater -AppDirectory $app -PythonExe $python -NoOpen
    if ((Get-Content -Raw $keyFile).Trim() -ne $key) { throw 'Upgrade changed console key.' }
    if ((Get-FileHash (Join-Path $app '.relay/client-owner.json')).Hash -ne $ownerHash -or
        (Get-FileHash (Join-Path $app '.relay/gateway.json')).Hash -ne $gatewayHash) { throw 'Update changed existing relay credentials.' }
    $created = Invoke-RestMethod 'http://127.0.0.1:8766/api/manage/add' -Method Post -Headers $headers -ContentType 'application/json' -Body '{"client_id":"smoke-win"}'
    if (-not $created.restart_required) { throw 'Missing reload warning.' }
    $downloaded = Invoke-RestMethod 'http://127.0.0.1:8766/api/manage/download/smoke-win' -Headers $headers
    if (-not $downloaded.token) { throw 'Missing synthetic credential download.' }
    Write-Host 'WINDOWS_CONSOLE_INSTALL_AND_UPGRADE_OK; no real gateway or Wind was used.'
} finally {
    Stop-ScheduledTask -TaskName 'WindRelayMonitor' -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName 'WindRelayMonitor' -Confirm:$false -ErrorAction SilentlyContinue
    foreach ($name in @('WindRelay-Console.lnk','WindRelay-Diagnostics.lnk')) {
        Remove-Item -LiteralPath (Join-Path ([Environment]::GetFolderPath('Desktop')) $name) -Force -ErrorAction SilentlyContinue
    }
}
