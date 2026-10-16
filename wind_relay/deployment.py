"""Public runtime identity. No hostnames, configuration paths or credentials."""
import hashlib
import json
from pathlib import Path

from . import VERSION


def runtime_identity():
    root = Path(__file__).resolve().parents[1]
    manifest = root/'gateway-manifest.json'
    build = None
    if manifest.is_file():
        value = json.loads(manifest.read_text(encoding='utf-8-sig'))
        files = value.get('files', {})
        for name, expected in files.items():
            part = Path(name)
            if part.is_absolute() or '..' in part.parts or (root/part).is_symlink():
                raise ValueError('Unsafe deployment manifest')
            if hashlib.sha256((root/part).read_bytes()).hexdigest() != expected:
                raise ValueError('Deployment program fingerprint mismatch')
        build = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if build != value.get('build_id') or value.get('version') != VERSION:
            raise ValueError('Deployment build identity mismatch')
    return {'gateway_version': VERSION, 'build_id': build,
            'scheduler': 'client_round_robin', 'single_execution_lane': True,
            'worker_version': None, 'worker_version_verified': False,
            'metering_requires_worker_telemetry': True}
