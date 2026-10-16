# Isolated Windows CI only: synthetic old gateway, tasks, credentials and DB.
$ErrorActionPreference = 'Stop'
$python = (Get-Command python).Source
$root = Join-Path $env:RUNNER_TEMP ('gateway-upgrade-'+[guid]::NewGuid().ToString('N'))
$app = Join-Path $root 'app'
$package = Join-Path $root 'package'
New-Item -ItemType Directory -Path (Join-Path $app '.relay'),(Join-Path $app 'references') -Force | Out-Null
Expand-Archive -LiteralPath (Join-Path $PWD 'dist/wind-relay-gateway-upgrade-0.1.1.zip') -DestinationPath $package
Copy-Item 'references/relay-tool-contract.json' (Join-Path $app 'references/relay-tool-contract.json')
$fixture = @'
import hashlib,json,pathlib,sqlite3,sys
p=pathlib.Path(sys.argv[1]);r=p/'.relay'
config={'database':'queue.sqlite','clients':{'owner':hashlib.sha256(b'SYNTHETIC_OWNER').hexdigest(),'colleague2':hashlib.sha256(b'SYNTHETIC_COLLEAGUE').hexdigest()},'worker_sha256':hashlib.sha256(b'SYNTHETIC_WORKER').hexdigest()}
(r/'gateway.json').write_text(json.dumps(config))
for name,token in [('owner','SYNTHETIC_OWNER'),('colleague2','SYNTHETIC_COLLEAGUE')]:
 (r/('client-'+name+'.json')).write_text(json.dumps({'url':'https://example.invalid','token':token}))
con=sqlite3.connect(r/'queue.sqlite')
con.executescript("PRAGMA journal_mode=WAL; CREATE TABLE jobs(id TEXT PRIMARY KEY,owner TEXT,payload TEXT,state TEXT,created REAL,updated REAL,lease TEXT,lease_until REAL,result TEXT); CREATE TABLE dedupe(id TEXT PRIMARY KEY,owner TEXT,payload_hash TEXT); CREATE TABLE worker(id INTEGER PRIMARY KEY,seen REAL,mode TEXT); INSERT INTO dedupe VALUES('11111111111111111111111111111111','owner','PRESERVE_DEDUPE');")
con.close()
(p/'server.py').write_text('SYNTHETIC_CORE_FILE_KEEP_UNCHANGED')
'@
& $python -c $fixture $app
if ($LASTEXITCODE) { throw 'Fixture failed.' }
$legacy = @'
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
class H(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  if self.path!='/v1/status' or self.headers.get('Authorization')!='Bearer SYNTHETIC_OWNER':self.send_response(401);self.end_headers();return
  self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps({'protocol_version':1,'mcp_version':'0.24.2','worker_online':False,'worker_mode':None,'execution_paused':False,'own_pending_jobs':0}).encode())
ThreadingHTTPServer(('127.0.0.1',8765),H).serve_forever()
'@
[IO.File]::WriteAllText((Join-Path $app 'relay_gateway.py'),$legacy,[Text.Encoding]::UTF8)
$oldAction=New-ScheduledTaskAction -Execute $python -Argument ('"'+(Join-Path $app 'relay_gateway.py')+'"') -WorkingDirectory $app
$trigger=New-ScheduledTaskTrigger -AtStartup
$principal=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount
$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName 'WindRelayGateway' -Action $oldAction -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
$headers=@{Authorization='Bearer SYNTHETIC_OWNER'}
$workerHeaders=@{Authorization='Bearer SYNTHETIC_WORKER'}
$colleagueHeaders=@{Authorization='Bearer SYNTHETIC_COLLEAGUE'}
$updater=Join-Path $package 'Upgrade-Gateway.ps1'
$configHash=(Get-FileHash (Join-Path $app '.relay/gateway.json')).Hash
$ownerHash=(Get-FileHash (Join-Path $app '.relay/client-owner.json')).Hash
$coreHash=(Get-FileHash (Join-Path $app 'server.py')).Hash
function Wait-Gateway {
 for ($try=0;$try -lt 15;$try++) {
  try { return (Invoke-RestMethod 'http://127.0.0.1:8765/v1/status' -Headers $headers -TimeoutSec 2) } catch { }
  Start-Sleep -Seconds 1
 }
 throw 'Synthetic gateway did not start.'
}
try {
 Start-ScheduledTask -TaskName 'WindRelayGateway'
 $before=Wait-Gateway
 & $updater -AppDirectory $app -PythonExe $python -CheckOnly
 if (Test-Path (Join-Path $app '.gateway-releases')) { throw 'CheckOnly wrote files.' }
 & $updater -AppDirectory $app -PythonExe $python -NoOpen
 $after=Wait-Gateway
 if ($after.relay_version -ne '0.1.1' -or $after.deployment.scheduler -ne 'client_round_robin' -or $after.maintenance_mode -or $after.queue_policy.client_pending_limit -ne 5) { throw 'Runtime upgrade not verified.' }
 if ((Get-FileHash (Join-Path $app '.relay/gateway.json')).Hash -ne $configHash -or (Get-FileHash (Join-Path $app '.relay/client-owner.json')).Hash -ne $ownerHash -or (Get-FileHash (Join-Path $app 'server.py')).Hash -ne $coreHash) { throw 'Credentials or core changed.' }
 $report=Get-Content -Raw (Join-Path $app '.relay/deployment-current.json') | ConvertFrom-Json
 if ($report.upgrade.outcome -ne 'activated' -or $report.gateway.version -ne '0.1.1' -or $report.worker.version_verified) { throw 'Deployment report invalid.' }
 $consoleReport=Invoke-RestMethod 'http://127.0.0.1:8766/api/deployment-report'
 if ($consoleReport.console_version -ne '0.2.1' -or $consoleReport.deployment.gateway_version -ne '0.1.1') { throw 'Console deployment report invalid.' }
 # Prove actual rotation with HTTP jobs and synthetic completions. No Wind SDK.
 foreach ($entry in @(@('2',$headers),@('3',$headers),@('4',$colleagueHeaders))) {
  $body=@{job_id=($entry[0]*32);tool='wind_status';arguments=@{connect=$false}} | ConvertTo-Json -Depth 4 -Compress
  Invoke-RestMethod 'http://127.0.0.1:8765/v1/jobs' -Method Post -Headers $entry[1] -ContentType 'application/json' -Body $body | Out-Null
 }
 $order=@()
 for ($i=0;$i -lt 3;$i++) {
  $body=@{mode='mock';server_sha256=$after.server_sha256} | ConvertTo-Json -Compress
  $claim=Invoke-RestMethod 'http://127.0.0.1:8765/v1/worker/claim' -Method Post -Headers $workerHeaders -ContentType 'application/json' -Body $body
  $order += $claim.job.client_id
  $complete=@{lease_id=$claim.job.lease_id;result=@{ok=$true;relay_execution=@{mode='mock'};synthetic_fixture=$true}} | ConvertTo-Json -Depth 5 -Compress
  Invoke-RestMethod ('http://127.0.0.1:8765/v1/worker/'+$claim.job.job_id+'/complete') -Method Post -Headers $workerHeaders -ContentType 'application/json' -Body $complete | Out-Null
 }
 if (($order -join ',') -ne 'owner,colleague2,owner') { throw 'Device rotation failed.' }
 # Rebuild a valid package whose program intentionally fails after a DB change.
 $badRoot=Join-Path $root 'bad-package'
 Copy-Item -LiteralPath $package -Destination $badRoot -Recurse
 $breakPackage=@'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]);script=p/'gateway/relay_gateway.py'
s=script.read_text();needle='    uvicorn.run(create_app(config), host=args.host, port=args.port, access_log=False)'
replacement="    app = create_app(config)\n    with app.state.queue.connection() as con:\n        con.execute('CREATE TABLE synthetic_failed_upgrade(marker TEXT)')\n    raise SystemExit('SYNTHETIC_STARTUP_FAILURE')"
assert needle in s;script.write_text(s.replace(needle,replacement))
m=json.loads((p/'gateway/gateway-manifest.json').read_text());m['files']['relay_gateway.py']=hashlib.sha256(script.read_bytes()).hexdigest();m['build_id']=hashlib.sha256(json.dumps(m['files'],sort_keys=True,separators=(',',':')).encode()).hexdigest();(p/'gateway/gateway-manifest.json').write_text(json.dumps(m))
u=json.loads((p/'upgrade-manifest.json').read_text());u['build_id']=m['build_id']
for name in u['files']:u['files'][name]=hashlib.sha256((p/name).read_bytes()).hexdigest()
(p/'upgrade-manifest.json').write_text(json.dumps(u))
'@
 & $python -c $breakPackage $badRoot
 if ($LASTEXITCODE) { throw 'Broken package fixture failed.' }
 $goodAction=(Get-ScheduledTask -TaskName 'WindRelayGateway').Actions[0].Arguments
 $failed=$false
 try { & (Join-Path $badRoot 'Upgrade-Gateway.ps1') -AppDirectory $app -PythonExe $python -SkipConsole -NoOpen } catch { $failed=$true }
 if (-not $failed) { throw 'Broken gateway upgrade unexpectedly activated.' }
 $recovered=Wait-Gateway
 if ($recovered.deployment.build_id -ne $after.deployment.build_id -or (Get-ScheduledTask -TaskName 'WindRelayGateway').Actions[0].Arguments -ne $goodAction) { throw 'Old task not restored.' }
 $verifyDatabase=@'
import pathlib,sqlite3,sys
con=sqlite3.connect(pathlib.Path(sys.argv[1])/'.relay/queue.sqlite')
assert con.execute("SELECT payload_hash FROM dedupe WHERE id=?",('1'*32,)).fetchone()[0]=='PRESERVE_DEDUPE'
assert con.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==3
assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='synthetic_failed_upgrade'").fetchone()
con.close()
'@
 & $python -c $verifyDatabase $app
 if ($LASTEXITCODE) { throw 'Rollback lost tasks/dedupe or left a failed migration.' }
 if (Test-Path (Join-Path $app '.relay/gateway-maintenance.json')) { throw 'Own failed-upgrade marker not removed after verified rollback.' }
 Write-Host 'WINDOWS_GATEWAY_LEGACY_UPGRADE_ROTATION_REPORT_AND_ROLLBACK_OK; NO WIND WAS STARTED.'
} finally {
 foreach ($task in @('WindRelayGateway','WindRelayMonitor')) {
  Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
 }
}
