# Isolated Windows CI only: synthetic old gateway, tasks, credentials and DB.
param([switch]$PasswordLauncher)
$ErrorActionPreference = 'Stop'
$python = (Get-Command python).Source
$root = Join-Path $env:RUNNER_TEMP ('gateway-upgrade-'+[guid]::NewGuid().ToString('N'))
$app = Join-Path $root 'app'
$package = Join-Path $root 'package'
New-Item -ItemType Directory -Path (Join-Path $app '.relay'),(Join-Path $app 'references') -Force | Out-Null
Expand-Archive -LiteralPath (Join-Path $PWD 'dist/wind-relay-gateway-upgrade-0.1.4.zip') -DestinationPath $package
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
$fixtureFile=Join-Path $root 'create-fixture.py'
[IO.File]::WriteAllText($fixtureFile,$fixture,[Text.Encoding]::UTF8)
& $python $fixtureFile $app
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
$syntheticAccount = $null
$launcherPath = Join-Path $app 'start_gateway.cmd'
if ($PasswordLauncher) {
 $legacyBatch = '@echo off'+"`r`n"+'"'+$python+'" "'+(Join-Path $app 'relay_gateway.py')+'"'+"`r`n"+'exit /b %errorlevel%'+"`r`n"
 [IO.File]::WriteAllText($launcherPath,$legacyBatch,[Text.Encoding]::ASCII)
 $oldAction=New-ScheduledTaskAction -Execute $launcherPath -WorkingDirectory $app
 # Exact known 0.1.3 marker after the old launcher has been recovered; no DB restore.
 [IO.File]::WriteAllText((Join-Path $app '.relay/gateway-maintenance.json'),'{"build_id":"f3067a01d873cbe09fb95589a9f22d764b18f85b960d06a3e00b918ca101bfe6"}',[Text.Encoding]::ASCII)
}
$trigger=New-ScheduledTaskTrigger -AtStartup
$principal=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount
$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
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
 if ($PasswordLauncher) {
  $syntheticAccount='RelayCI'+[guid]::NewGuid().ToString('N').Substring(0,8)
  $syntheticPassword='aA1!'+[guid]::NewGuid().ToString('N')
  $securePassword=ConvertTo-SecureString $syntheticPassword -AsPlainText -Force
  New-LocalUser -Name $syntheticAccount -Password $securePassword -PasswordNeverExpires | Out-Null
  $administrators=(Get-LocalGroup -SID 'S-1-5-32-544').Name
  Add-LocalGroupMember -Group $administrators -Member $syntheticAccount
  Register-ScheduledTask -TaskName 'WindRelayGateway' -Action $oldAction -Trigger $trigger -User ($env:COMPUTERNAME+'\'+$syntheticAccount) -Password $syntheticPassword -RunLevel Highest -Settings $settings | Out-Null
  $syntheticPassword=$null;$securePassword=$null
  $passwordTaskBefore=Get-ScheduledTask -TaskName 'WindRelayGateway'
  if ([string]$passwordTaskBefore.Principal.LogonType -ne 'Password') { throw 'Password-logon fixture was not actually registered.' }
 } else {
  Register-ScheduledTask -TaskName 'WindRelayGateway' -Action $oldAction -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
 }
 Start-ScheduledTask -TaskName 'WindRelayGateway'
 $before=Wait-Gateway
 & $updater -AppDirectory $app -PythonExe $python -CheckOnly
 if (Test-Path (Join-Path $app '.gateway-releases')) { throw 'CheckOnly wrote files.' }
 if (-not $PasswordLauncher) {
  # A rejected action update must restart the unchanged original task and keep
  # the first failure visible; it must not try the same forbidden action again.
  function Set-ScheduledTask { throw 'SYNTHETIC_TASK_REGISTRATION_DENIED' }
  $denied=$false
  try { & $updater -AppDirectory $app -PythonExe $python -SkipConsole -NoOpen } catch { $denied=$_.Exception.Message -like '*SYNTHETIC_TASK_REGISTRATION_DENIED*' }
  finally { Remove-Item Function:\Set-ScheduledTask }
  if (-not $denied) { throw 'The original task-registration failure was hidden.' }
  $stillOld=Wait-Gateway
  if ($stillOld.relay_version -or (Test-Path (Join-Path $app '.relay/gateway-maintenance.json'))) { throw 'Unchanged original task was not restored after rejected registration.' }
 }
 & $updater -AppDirectory $app -PythonExe $python -NoOpen
 $after=Wait-Gateway
 if ($PasswordLauncher) {
  $taskAfter=Get-ScheduledTask -TaskName 'WindRelayGateway'
  # Task Scheduler may canonicalize the registered account name to a SID.
  # Compare the actual registered identity/action before and after the upgrade.
  if ([string]$taskAfter.Actions[0].Execute -cne [string]$passwordTaskBefore.Actions[0].Execute -or [string]$taskAfter.Actions[0].Arguments -cne [string]$passwordTaskBefore.Actions[0].Arguments -or [string]$taskAfter.Actions[0].WorkingDirectory -cne [string]$passwordTaskBefore.Actions[0].WorkingDirectory -or [string]$taskAfter.Principal.LogonType -cne [string]$passwordTaskBefore.Principal.LogonType -or [string]$taskAfter.Principal.UserId -cne [string]$passwordTaskBefore.Principal.UserId -or [string]$taskAfter.Principal.RunLevel -cne [string]$passwordTaskBefore.Principal.RunLevel) { throw 'Password task identity/action changed.' }
  if (@(Get-ChildItem -LiteralPath (Join-Path $app '.relay/archive') -Filter 'gateway-maintenance-0.1.3-*.json').Count -ne 1) { throw 'Known legacy marker was not preserved.' }
 }
 if ($after.relay_version -ne '0.1.4' -or $after.deployment.scheduler -ne 'client_round_robin' -or $after.maintenance_mode -or $after.queue_policy.client_pending_limit -ne 5) { throw 'Runtime upgrade not verified.' }
 if ((Get-FileHash (Join-Path $app '.relay/gateway.json')).Hash -ne $configHash -or (Get-FileHash (Join-Path $app '.relay/client-owner.json')).Hash -ne $ownerHash -or (Get-FileHash (Join-Path $app 'server.py')).Hash -ne $coreHash) { throw 'Credentials or core changed.' }
 $report=Get-Content -Raw (Join-Path $app '.relay/deployment-current.json') | ConvertFrom-Json
 if ($report.upgrade.outcome -ne 'activated' -or $report.gateway.version -ne '0.1.4' -or $report.worker.version_verified) { throw 'Deployment report invalid.' }
 $consoleReport=Invoke-RestMethod 'http://127.0.0.1:8766/api/deployment-report'
 if ($consoleReport.console_version -ne '0.3.2' -or $consoleReport.deployment.gateway_version -ne '0.1.4') { throw 'Console deployment report invalid.' }
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
 $breakFile=Join-Path $root 'break-package.py'
 [IO.File]::WriteAllText($breakFile,$breakPackage,[Text.Encoding]::UTF8)
 & $python $breakFile $badRoot
 if ($LASTEXITCODE) { throw 'Broken package fixture failed.' }
 $goodAction=(Get-ScheduledTask -TaskName 'WindRelayGateway').Actions[0].Arguments
 $goodLauncherHash=if ($PasswordLauncher) {(Get-FileHash -LiteralPath $launcherPath).Hash} else {''}
 $failed=$false
 try { & (Join-Path $badRoot 'Upgrade-Gateway.ps1') -AppDirectory $app -PythonExe $python -SkipConsole -NoOpen } catch { $failed=$true }
 if (-not $failed) { throw 'Broken gateway upgrade unexpectedly activated.' }
 $recovered=Wait-Gateway
 if ($recovered.deployment.build_id -ne $after.deployment.build_id -or (Get-ScheduledTask -TaskName 'WindRelayGateway').Actions[0].Arguments -ne $goodAction) { throw 'Old task not restored.' }
 if ($PasswordLauncher -and (Get-FileHash -LiteralPath $launcherPath).Hash -ne $goodLauncherHash) { throw 'Stable launcher bytes were not restored.' }
 $verifyDatabase=@'
import pathlib,sqlite3,sys
con=sqlite3.connect(pathlib.Path(sys.argv[1])/'.relay/queue.sqlite')
assert con.execute("SELECT payload_hash FROM dedupe WHERE id=?",('1'*32,)).fetchone()[0]=='PRESERVE_DEDUPE'
assert con.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==3
assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='synthetic_failed_upgrade'").fetchone()
con.close()
'@
 $verifyFile=Join-Path $root 'verify-database.py'
 [IO.File]::WriteAllText($verifyFile,$verifyDatabase,[Text.Encoding]::UTF8)
 & $python $verifyFile $app
 if ($LASTEXITCODE) { throw 'Rollback lost tasks/dedupe or left a failed migration.' }
 if (Test-Path (Join-Path $app '.relay/gateway-maintenance.json')) { throw 'Own failed-upgrade marker not removed after verified rollback.' }
 Write-Host 'WINDOWS_GATEWAY_LEGACY_UPGRADE_ROTATION_REPORT_AND_ROLLBACK_OK; NO WIND WAS STARTED.'
 # Rehearse a manual online update through the installed HTTP API. Only the
 # metadata/archive fetch is replaced by a local synthetic Gitee mirror.
 # The actual downloader, verifier, separate SYSTEM task and installers run.
 Stop-ScheduledTask -TaskName 'WindRelayMonitor'
 for ($attempt=0;$attempt -lt 15;$attempt++) {
  if (-not (Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue)) { break }
  Start-Sleep -Seconds 1
 }
 $fixtureConsole=Join-Path $PWD '.github/online-update-fixture.py'
 $originalConsole=Join-Path $app 'monitor/monitor.py'
 $zip=Join-Path $PWD 'dist/wind-relay-gateway-upgrade-0.1.4.zip'
 $fixtureAction=New-ScheduledTaskAction -Execute $python -Argument ('"{0}" "{1}" "{2}" "{3}"' -f $fixtureConsole,$originalConsole,$app,$zip) -WorkingDirectory (Join-Path $app 'monitor')
 Set-ScheduledTask -TaskName 'WindRelayMonitor' -Action $fixtureAction | Out-Null
 Start-ScheduledTask -TaskName 'WindRelayMonitor'
 $fixtureReady=$false
 for ($attempt=0;$attempt -lt 25;$attempt++) {
  try {
   $state=Invoke-RestMethod 'http://127.0.0.1:8766/api/status' -TimeoutSec 2
   $updates=Invoke-RestMethod 'http://127.0.0.1:8766/api/updates' -TimeoutSec 2
   if ($state.demonstration -and $updates.checked_at -and $updates.releases[0].eligible) { $fixtureReady=$true;break }
  } catch { }
  Start-Sleep -Seconds 1
 }
 if (-not $fixtureReady) { throw 'Synthetic online console did not initialize.' }
 if ((Test-Path (Join-Path $app '.relay/fixture-archive-downloaded.txt')) -or (Get-ScheduledTask -TaskName 'WindRelayUpgrade' -ErrorAction SilentlyContinue)) { throw 'Version check automatically installed/downloaded an archive.' }
 $consoleKey=(Get-Content -Raw (Join-Path $app 'monitor/auth/console.key')).Trim()
 $desktopAccessBefore=(Get-Acl -LiteralPath (Join-Path $app 'monitor/auth')).Sddl
 $consoleHeaders=@{'X-Console-Token'=$consoleKey}
 $requested=Invoke-RestMethod 'http://127.0.0.1:8766/api/updates/install' -Method Post -Headers $consoleHeaders -ContentType 'application/json' -Body '{"gateway_version":"0.1.4","confirmed":true}'
 if (-not $requested.operation_id) { throw 'No durable update operation.' }
 $onlineCompleted=$false
 for ($attempt=0;$attempt -lt 90;$attempt++) {
  try {
   $updates=Invoke-RestMethod 'http://127.0.0.1:8766/api/updates' -TimeoutSec 2
   if ($updates.operation.state -eq 'succeeded') { $onlineCompleted=$true;break }
   if ($updates.operation.state -eq 'failed' -or $updates.operation.state -eq 'interrupted') { throw ('Online upgrade ended: '+$updates.operation.error_code) }
  } catch {
   # Service restart briefly removes the old console listener.
   if ($_.Exception.Message -like 'Online upgrade ended:*') { throw }
  }
  Start-Sleep -Seconds 1
 }
 if (-not $onlineCompleted) {
  Get-Content -LiteralPath (Join-Path $app ('.relay/online-updates/'+$requested.operation_id+'/installer.log')) -Tail 80 -ErrorAction SilentlyContinue
  throw 'Independent online upgrade task did not finish across console restart.'
 }
 $current=Invoke-RestMethod 'http://127.0.0.1:8766/api/status'
 if ($current.demonstration -or $current.diagnostics.version -ne '0.3.2' -or $current.deployment.gateway_version -ne '0.1.4') { throw 'New real console/runtime versions were not verified.' }
 if ((Get-FileHash (Join-Path $app '.relay/gateway.json')).Hash -ne $configHash -or (Get-FileHash (Join-Path $app '.relay/client-owner.json')).Hash -ne $ownerHash -or (Get-Content -Raw (Join-Path $app 'monitor/auth/console.key')).Trim() -ne $consoleKey) { throw 'Online update changed credentials.' }
 if ((Get-Acl -LiteralPath (Join-Path $app 'monitor/auth')).Sddl -ne $desktopAccessBefore) { throw 'SYSTEM update changed the desktop user access rules.' }
 Write-Host 'WINDOWS_MANUAL_ONLINE_UPDATE_SURVIVED_CONSOLE_RESTART_OK; SYNTHETIC MIRROR, NO WIND.'
 if ($PasswordLauncher) { Write-Host 'WINDOWS_PASSWORD_TASK_LAUNCHER_UPGRADE_ROLLBACK_AND_ONLINE_UPDATE_OK; NO PASSWORD WAS PASSED TO THE UPDATER.' }
} finally {
 foreach ($task in @('WindRelayUpgrade','WindRelayGateway','WindRelayMonitor')) {
  Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
 }
 if ($syntheticAccount) {
  Remove-LocalGroupMember -Group $administrators -Member $syntheticAccount -ErrorAction SilentlyContinue
  Remove-LocalUser -Name $syntheticAccount -ErrorAction SilentlyContinue
 }
}
