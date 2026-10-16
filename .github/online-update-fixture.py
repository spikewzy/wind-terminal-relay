"""Windows CI only: a synthetic mirrored ZIP; no external feed or Wind calls."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

_, original_script, app_path, archive_path = sys.argv
app = Path(app_path)
data = Path(archive_path).read_bytes()
with zipfile.ZipFile(archive_path) as archive:
    gateway = json.loads(archive.read('upgrade-manifest.json'))
    console_version = json.loads(archive.read('console-manifest.json'))['version']
sys.path.insert(0, str(Path(original_script).parent))
import monitor as console
from updates import Updates as RealUpdates, BASE, FEED

item = {'gateway_version':gateway['version'],'console_version':console_version,'updater_protocol':1,
        'path':'updates/packages/'+Path(archive_path).name,'bytes':len(data),
        'sha256':hashlib.sha256(data).hexdigest(),'build_id':gateway['build_id'],
        'published_on':'2026-10-16','notes':'Synthetic CI mirror; reinstalling the current reviewed package'}


def fetcher(url, maximum):
    if url == FEED:
        return json.dumps({'schema_version':1,'channel':'stable','releases':[item]}).encode()
    assert url == BASE+item['path']
    (app/'.relay/fixture-archive-downloaded.txt').write_text('synthetic archive requested')
    return data


class FixtureUpdates(RealUpdates):
    def __init__(self, root, guard, current, recorder):
        # Report an older version only in this synthetic console so the real
        # installer can rehearse replacing its own service with the same ZIP.
        super().__init__(root,guard,lambda:{'gateway_version':'0.1.1'},recorder,fetcher=fetcher)


original_collect = console.collect
def collect(*args, **kwargs):
    value = original_collect(*args, **kwargs)
    value['demonstration'] = True
    return value


console.Updates = FixtureUpdates
console.collect = collect
sys.argv = [original_script,'--config',str(app/'.relay/gateway.json'),
            '--client-config',str(app/'.relay/client-owner.json')]
console.main()
