"""Allowlisted gateway migration + console bundle. No deployment state or SDK."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wind_relay import VERSION
from monitor.build_package import FILES, ROOT_FILES

GATEWAY_FILES = ('relay_gateway.py','requirements-gateway.lock.txt','references/relay-tool-contract.json',
                 'wind_relay/__init__.py','wind_relay/common.py','wind_relay/gateway.py',
                 'wind_relay/store.py','wind_relay/usage.py','wind_relay/deployment.py')
UPDATE_FILES = ('Upgrade-Gateway.cmd','Upgrade-Gateway.ps1','gateway_update/support.py','gateway_update/README.md')


def read_file(path):
    if path.is_symlink() or not path.is_file() or any(p.is_symlink() for p in path.parents):
        raise ValueError('Unsafe package source')
    data = path.read_bytes()
    if path.suffix in {'.ps1','.cmd'}:
        data = data.replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
    return data


def build(destination=None):
    directory = Path(destination) if destination is not None else ROOT/'dist'
    if directory.is_symlink():
        raise ValueError('Unsafe package output')
    directory.mkdir(parents=True, exist_ok=True)
    data = {name: read_file(ROOT/name) for name in GATEWAY_FILES}
    hashes = {name: hashlib.sha256(raw).hexdigest() for name,raw in data.items()}
    identity = hashlib.sha256(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    manifest = {'kind':'wind_relay_gateway','version':VERSION,'build_id':identity,'files':hashes}
    payload = {'gateway/'+name: raw for name,raw in data.items()}
    payload['gateway/gateway-manifest.json'] = (json.dumps(manifest,indent=2)+'\n').encode()
    console_files = ['monitor/'+name for name in FILES]+list(ROOT_FILES)
    console_hashes = {}
    for name in console_files:
        payload[name] = read_file(ROOT/name)
        console_hashes[name] = hashlib.sha256(payload[name]).hexdigest()
    from monitor.diagnostics import VERSION as CONSOLE_VERSION
    payload['console-manifest.json'] = (json.dumps({'kind':'wind_relay_console','version':CONSOLE_VERSION,'files':console_hashes},indent=2)+'\n').encode()
    for name in UPDATE_FILES:
        payload[name] = read_file(ROOT/name)
    package_manifest = {'kind':'wind_relay_gateway_upgrade','version':VERSION,'build_id':identity,
                        'files':{name:hashlib.sha256(raw).hexdigest() for name,raw in payload.items()}}
    path = directory/('wind-relay-gateway-upgrade-'+VERSION+'.zip')
    if path.is_symlink() or path.with_suffix('.zip.sha256').is_symlink():
        raise ValueError('Unsafe package output file')
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for name,raw in payload.items():
            z.writestr(name,raw)
        z.writestr('upgrade-manifest.json',json.dumps(package_manifest,indent=2)+'\n')
    value=hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix('.zip.sha256').write_text(value+'  '+path.name+'\n',encoding='ascii')
    return {'archive':str(path),'sha256':value,'build_id':identity}


if __name__=='__main__':
    print(json.dumps(build()))
