"""Build the reviewed console/update bundle; never include deployment state."""
import hashlib
import json
from pathlib import Path
import zipfile
try:
    from .diagnostics import VERSION
except ImportError:
    from diagnostics import VERSION
FILES = ('monitor.py', 'diagnostics.py', 'management.py', 'updates.py', 'cacert.pem', 'CA-LICENSE.txt', 'ca-provenance.json', 'Launch-Upgrade.ps1', 'Run-OnlineUpgrade.ps1', 'index.html', 'Start.cmd', 'Install.ps1', 'README.md', 'Export-Diagnostics.cmd')
ROOT_FILES = ('Update.cmd', 'Update.ps1', 'START-HERE.txt', 'LICENSE')


def write_member(archive, name, data):
    member = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    # Small source-only bundles: stored members avoid zlib-version differences
    # between the Windows/Linux/macOS builders of the mirrored checksum.
    member.compress_type = zipfile.ZIP_STORED
    member.create_system = 3
    member.external_attr = 0o100644 << 16
    archive.writestr(member, data)


def build(destination=None):
    root = Path(__file__).resolve().parent
    destination = Path(destination) if destination is not None else root.parent/'dist'
    if destination.is_symlink():
        raise ValueError('Unsafe output directory')
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination/('wind-relay-console-'+VERSION+'.zip')
    checksum = archive.with_suffix('.zip.sha256')
    if archive.is_symlink() or checksum.is_symlink():
        raise ValueError('Unsafe output file')
    manifest = {'kind': 'wind_relay_console', 'version': VERSION, 'files': {}}
    paths = {('monitor/'+name): root/name for name in FILES}
    paths.update({name: root.parent/name for name in ROOT_FILES})
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as output:
        for member, path in paths.items():
            if path.is_symlink() or not path.is_file():
                raise ValueError('Missing or unsafe console file')
            data = path.read_bytes().replace(b'\r\n', b'\n')
            if path.suffix in {'.ps1', '.cmd'}:
                data = data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
            manifest['files'][member] = hashlib.sha256(data).hexdigest()
            write_member(output, member, data)
        write_member(output, 'console-manifest.json', json.dumps(manifest, indent=2)+'\n')
    value = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum.write_text(value+'  '+archive.name+'\n', encoding='ascii')
    return {'archive': str(archive), 'sha256': value}


if __name__ == '__main__':
    print(json.dumps(build()))
