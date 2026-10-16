"""Offline/read-only preflight and controlled upgrade helpers. Never imports Wind."""
from __future__ import annotations
import argparse
from contextlib import closing
import datetime as dt
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from monitor.management import safe_path, read, write, protect, ManagementError

GATEWAY_FILES = {'relay_gateway.py','requirements-gateway.lock.txt','references/relay-tool-contract.json',
                 'wind_relay/__init__.py','wind_relay/common.py','wind_relay/gateway.py',
                 'wind_relay/store.py','wind_relay/usage.py','wind_relay/deployment.py'}
CONSOLE_FILES = {'monitor/monitor.py','monitor/diagnostics.py','monitor/management.py','monitor/updates.py',
                 'monitor/Launch-Upgrade.ps1','monitor/Run-OnlineUpgrade.ps1','monitor/index.html',
                 'monitor/Start.cmd','monitor/Install.ps1','monitor/README.md','monitor/Export-Diagnostics.cmd',
                 'Update.cmd','Update.ps1','START-HERE.txt','LICENSE','console-manifest.json'}
UPGRADE_FILES = {'Upgrade-Gateway.cmd','Upgrade-Gateway.ps1','gateway_update/support.py','gateway_update/README.md'}
SHA = re.compile(r'[a-f0-9]{64}\Z')
SEMVER = re.compile(r'\d{1,3}\.\d{1,3}\.\d{1,3}\Z')


class UpgradeError(Exception):
    pass


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def hash_file(path):
    return hashlib.sha256(safe_path(path).read_bytes()).hexdigest()


def verify_package(root):
    root = safe_path(root)
    value = read(root/'upgrade-manifest.json')
    expected = {'gateway/'+n for n in GATEWAY_FILES}|CONSOLE_FILES|UPGRADE_FILES|{'gateway/gateway-manifest.json'}
    if (value.get('kind')!='wind_relay_gateway_upgrade' or value.get('version')!='0.1.2'
            or set(value.get('files',{}))!=expected):
        raise UpgradeError('PACKAGE_INVALID')
    for name, hashed in value['files'].items():
        if not isinstance(hashed,str) or not SHA.fullmatch(hashed) or hash_file(root/name)!=hashed:
            raise UpgradeError('PACKAGE_HASH_MISMATCH')
    manifest = read(root/'gateway/gateway-manifest.json')
    hashes = manifest.get('files',{})
    build = hashlib.sha256(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if (set(hashes)!=GATEWAY_FILES or build!=manifest.get('build_id') or build!=value.get('build_id')
            or manifest.get('version')!=value['version']):
        raise UpgradeError('BUILD_INVALID')
    for name, hashed in hashes.items():
        if hash_file(root/'gateway'/name)!=hashed:
            raise UpgradeError('BUILD_INVALID')
    return value


def environment(root):
    missing=[]
    for line in safe_path(root/'gateway/requirements-gateway.lock.txt').read_text().splitlines():
        if not line.strip():
            continue
        name, expected=line.split('==')
        try:
            if importlib.metadata.version(name)!=expected:
                missing.append(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)
    return {'compatible':not missing,'mismatched_dependencies':missing}


def configuration(app):
    app=safe_path(app)
    config=read(app/'.relay/gateway.json')
    probe=read(app/'.relay/client-owner.json')
    if not isinstance(config.get('clients'),dict) or not config['clients']:
        raise UpgradeError('CLIENT_MAPPING_INVALID')
    if hashlib.sha256(probe.get('token','').encode()).hexdigest() not in config['clients'].values():
        raise UpgradeError('OWNER_CONFIG_MISMATCH')
    path=Path(config['database']).expanduser()
    path=safe_path(path if path.is_absolute() else app/'.relay'/path)
    return config,probe,path


def db_idle(con):
    # No maintain(), expiry, task cancellation, or other writes here.
    count=con.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running','uncertain')").fetchone()[0]
    if count:
        raise UpgradeError('TASKS_NOT_IDLE')


def inspect(app, root):
    package=verify_package(root)
    config,_,path=configuration(app)
    contract=read(root/'gateway/references/relay-tool-contract.json')
    old_contract=read(app/'references/relay-tool-contract.json')
    if any(contract.get(k)!=old_contract.get(k) for k in ('protocol_version','mcp_version','server_sha256')):
        raise UpgradeError('CORE_CONTRACT_MISMATCH')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as con:
        db_idle(con)
        if con.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise UpgradeError('DATABASE_CHECK_FAILED')
    return {'checked_at_utc':stamp(),'idle':True,'target_version':package['version'],
            'build_id':package['build_id'],'client_count':len(config['clients']),
            'environment':environment(root)}


def bounded_backup(source, destination):
    deadline=time.monotonic()+20
    def progress(status,remaining,total):
        if time.monotonic()>deadline:
            raise UpgradeError('DATABASE_BACKUP_TIMEOUT')
    source.backup(destination,pages=256,progress=progress,sleep=.05)


def backup(app, directory):
    _,_,path=configuration(app)
    directory=safe_path(directory)
    directory.mkdir(mode=0o700,exist_ok=True)
    destination=safe_path(directory/'queue.sqlite')
    with destination.open('xb'):
        protect(destination)
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as source:
            source.execute('BEGIN')
            db_idle(source)  # Final check AFTER the old listener is stopped.
            with closing(sqlite3.connect(destination)) as target:
                bounded_backup(source,target)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return {'backup_verified':True,'database_sha256':hash_file(destination)}


def restore(app, directory):
    _,_,path=configuration(app)
    source_path=safe_path(directory/'queue.sqlite')
    if not source_path.is_file():
        raise UpgradeError('BACKUP_UNAVAILABLE')
    with closing(sqlite3.connect(source_path.as_uri()+'?mode=ro',uri=True,timeout=2)) as source:
        if source.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise UpgradeError('BACKUP_CHECK_FAILED')
        # SQLite's backup API also restores a live WAL database correctly. Never
        # overwrite only queue.sqlite or blindly remove -wal/-shm files.
        with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=5)) as target:
            bounded_backup(source,target)
    return {'database_restored':True}


def get_local(port,route,token=None):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args):
            return None
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    headers={'Authorization':'Bearer '+token} if token else {}
    req=urllib.request.Request('http://127.0.0.1:'+str(port)+route,headers=headers)
    with opener.open(req,timeout=3) as response:
        body=response.read(65537)
        if len(body)>65536:
            raise UpgradeError('UNEXPECTED_STATUS')
        value=json.loads(body)
    if not isinstance(value,dict):
        raise UpgradeError('UNEXPECTED_STATUS')
    return value


def clean_version(value):
    return value if isinstance(value,str) and SEMVER.fullmatch(value) else None


def clean_sha(value):
    return value if isinstance(value,str) and SHA.fullmatch(value) else None


def report(app):
    config,probe,_=configuration(app)
    gateway={'runtime_verified':False,'version':None,'build_id':None}
    worker={'online':None,'mode':None,'version':None,'version_verified':False}
    try:
        status=get_local(8765,'/v1/status',probe['token'])
        if status.get('protocol_version')!=1 or type(status.get('worker_online')) is not bool:
            raise UpgradeError('UNEXPECTED_STATUS')
        identity=status.get('deployment',{})
        gateway.update(runtime_verified=True,version=clean_version(status.get('relay_version')),
                       build_id=clean_sha(identity.get('build_id')),
                       maintenance_mode=status.get('maintenance_mode') is True,
                       core_version=clean_version(status.get('mcp_version')),
                       core_server_sha256=clean_sha(status.get('server_sha256')),
                       scheduler='client_round_robin' if identity.get('scheduler')=='client_round_robin' else 'unreported',
                       single_execution_lane=identity.get('single_execution_lane') is True)
        policy=status.get('queue_policy')
        if isinstance(policy,dict):
            gateway['queue_policy']={key:value for key,value in policy.items()
                if key in {'client_pending_limit','global_queued_limit','new_jobs_per_minute','global_new_jobs_per_minute'}
                and (value is None or type(value) is int and 0<=value<=10**9)}
        worker.update(online=status['worker_online'],mode=status.get('worker_mode') if status.get('worker_mode') in {'mock','native'} else None)
    except Exception:
        gateway['error']='GATEWAY_STATUS_UNAVAILABLE'
    console={'runtime_verified':False,'version':None}
    try:
        value=get_local(8766,'/api/status')
        version=clean_version(value.get('diagnostics',{}).get('version'))
        if version:
            console.update(runtime_verified=True,version=version)
    except Exception:
        pass
    return {'schema_version':1,'captured_at_utc':stamp(),'gateway':gateway,'worker':worker,
            'console':console,'client_count':len(config['clients']),
            'live_wind_query_performed':False,'worker_upgrade_performed':False,
            'note':'Cloud runtime evidence only; worker version and live Wind login were not verified'}


def finalize(app,build,output=None):
    value=report(app)
    gateway=value['gateway']
    if (not gateway['runtime_verified'] or gateway.get('build_id')!=build or gateway.get('version')!='0.1.2'
            or not gateway.get('maintenance_mode') or gateway.get('scheduler')!='client_round_robin'):
        raise UpgradeError('GATEWAY_VERIFICATION_FAILED')
    config,_,_=configuration(app)
    expected={'client_pending_limit':5,'global_queued_limit':100,'new_jobs_per_minute':30,'global_new_jobs_per_minute':None}
    expected.update(config.get('queue_policy',{}))
    if gateway.get('queue_policy')!=expected:
        raise UpgradeError('QUEUE_POLICY_VERIFICATION_FAILED')
    marker=safe_path(app/'.relay/gateway-maintenance.json')
    if read(marker).get('build_id')!=build:
        raise UpgradeError('MAINTENANCE_MARKER_MISMATCH')
    value['upgrade']={'outcome':'verified_before_activation','target_version':'0.1.2','build_id':build}
    # Reports must be writable before admission is resumed. No rollback of the
    # DB is permitted once this marker is removed and new jobs can be accepted.
    write(app/'.relay/deployment-current.json',value)
    if output:
        write(output,value,exclusive=True)
    marker.unlink()
    value['upgrade']['outcome']='activated'
    value['gateway']['maintenance_mode']=False
    record_saved=True
    try:
        write(app/'.relay/deployment-current.json',value)
        if output:write(output,value)
    except Exception:
        record_saved=False
    return {'activated':True,'build_id':build,'deployment_record_saved':record_saved}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation',choices=['verify','inspect','environment','backup','restore','report','finalize'])
    p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('--app',type=Path)
    p.add_argument('--directory',type=Path)
    p.add_argument('--build-id')
    p.add_argument('--output',type=Path)
    a=p.parse_args()
    try:
        if a.operation=='verify':result=verify_package(a.root)
        elif a.operation=='environment':result=environment(a.root)
        elif a.operation=='inspect':result=inspect(a.app,a.root)
        elif a.operation=='backup':result=backup(a.app,a.directory)
        elif a.operation=='restore':result=restore(a.app,a.directory)
        elif a.operation=='finalize':result=finalize(a.app,a.build_id,a.output)
        else:
            result=report(a.app)
            if a.output:write(a.output,result,exclusive=True)
        print(json.dumps(result,ensure_ascii=True))
    except (Exception,) as exc:
        # No raw exceptions/configs/HTTP headers are printed by this utility.
        code=str(exc) if isinstance(exc,(UpgradeError,ManagementError)) else 'UPGRADE_HELPER_FAILED'
        print(json.dumps({'error':code}),file=sys.stderr)
        raise SystemExit(1) from None


if __name__=='__main__':main()
