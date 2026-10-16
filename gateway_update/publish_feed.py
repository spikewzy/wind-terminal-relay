"""Commit a reproducible upgrade ZIP and Gitee feed together; no network access."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gateway_update.build_package import build
from monitor.diagnostics import VERSION as CONSOLE_VERSION
from monitor.updates import validate_feed
from wind_relay import VERSION


def publish(destination, notes, published_on, check=False):
    destination = Path(destination)
    if destination.is_symlink() or any(p.is_symlink() for p in destination.parents):
        raise ValueError('Unsafe feed directory')
    feed = destination/'stable.json'
    releases = validate_feed(feed.read_bytes()) if feed.exists() else []
    with tempfile.TemporaryDirectory() as temporary:
        package = build(Path(temporary))
        name = Path(package['archive']).name
        data = Path(package['archive']).read_bytes()
        path = destination/'packages'/name
        current = next((r for r in releases if r['gateway_version'] == VERSION), None)
        if check:
            if (current is None or current['sha256'] != package['sha256'] or
                    current['console_version'] != CONSOLE_VERSION or current['build_id'] != package['build_id'] or
                    path.is_symlink() or not path.is_file() or path.read_bytes() != data):
                raise ValueError('Committed update package is stale: rebuild the feed before publishing')
            return {'version': VERSION, 'sha256': package['sha256'], 'checked': True}
        if path.is_symlink() or feed.is_symlink() or (destination/'packages').is_symlink():
            raise ValueError('Unsafe feed output')
        if path.exists() and path.read_bytes() != data:
            raise ValueError('Published version is immutable; bump gateway version before replacing its ZIP')
        item = {'gateway_version': VERSION, 'console_version': CONSOLE_VERSION, 'updater_protocol': 1,
                'path': 'updates/packages/'+name, 'sha256': package['sha256'], 'bytes': len(data),
                'build_id': package['build_id'], 'published_on': published_on, 'notes': notes}
        output = {'schema_version': 1, 'channel': 'stable',
                  'releases': [item]+[r for r in releases if r['gateway_version'] != VERSION][:4]}
        validate_feed(json.dumps(output).encode())
        (destination/'packages').mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        feed.write_text(json.dumps(output, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        return {'version': VERSION, 'sha256': package['sha256'], 'feed': str(feed)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'updates')
    parser.add_argument('--notes', default='Gitee 自动检查版本，手动选择并确认更新；保留接入文件与部署记录。')
    parser.add_argument('--published-on', default='2026-10-16')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    print(json.dumps(publish(args.output, args.notes, args.published_on, args.check), ensure_ascii=False))
