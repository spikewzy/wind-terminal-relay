"""Optional public connectivity evidence, independent of offline acceptance."""
import json
from pathlib import Path
import sys
import urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from monitor.updates import BASE,fetch,failure_code

url=BASE+'README.md'
for name,loader in (
    ('system_trust',lambda:urllib.request.urlopen(url,timeout=15).read(128)),
    ('system_and_bundled_mozilla',lambda:fetch(url,128*1024))):
    try:
        data=loader()
        print(json.dumps({'probe':name,'python':sys.version.split()[0],'ok':True,'bytes':len(data)}))
    except Exception as exc:
        cause=getattr(exc,'reason',exc)
        print(json.dumps({'probe':name,'python':sys.version.split()[0],'ok':False,
                          'code':failure_code(exc),'verify_code':getattr(cause,'verify_code',None)}))
