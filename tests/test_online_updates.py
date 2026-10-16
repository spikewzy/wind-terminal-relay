"""Synthetic Gitee metadata, explicit approval, integrity and restart recovery."""
import hashlib
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error
import zipfile

from gateway_update.build_package import build
from gateway_update.publish_feed import publish
from monitor.diagnostics import Recorder, VERSION, safe_snapshot
from monitor.monitor import Monitor, make_server
from monitor.updates import Updates, UpdateError, FEED, BASE, validate_feed, extract_package, trusted_url
from monitor.management import write


class UpdatesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.app = self.root/'app'
        (self.app/'.relay').mkdir(parents=True)
        package = build(self.root/'dist')
        self.data = Path(package['archive']).read_bytes()
        self.release = {'gateway_version':'0.1.3','console_version':VERSION,'path':'updates/packages/wind-relay-gateway-upgrade-0.1.3.zip',
                        'sha256':package['sha256'],'bytes':len(self.data),'build_id':package['build_id'],
                        'published_on':'2026-10-16','notes':'离线合成测试','updater_protocol':1}
        self.feed = {'schema_version':1,'channel':'stable','releases':[self.release]}
        self.hits, self.launched = [], []
        self.recorder = Recorder()
        self.guard_error = False
        self.guards = 0
        def guard():
            self.guards += 1
            if self.guard_error: raise UpdateError('TASKS_NOT_IDLE')
        self.manager = Updates(self.app, guard, lambda:{'gateway_version':'0.1.1'}, self.recorder,
                               fetcher=self.fetch, launcher=lambda *args:self.launched.append(args),
                               running=lambda:False, windows=True)

    def fetch(self, url, maximum):
        self.assertTrue(trusted_url(url))
        self.hits.append(url)
        return json.dumps(self.feed).encode() if url == FEED else self.data

    def wait_checked(self):
        deadline=time.monotonic()+3
        while self.manager.checking and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.manager.checking)

    def checked(self):
        self.manager.check();self.wait_checked()

    def finish(self):
        self.manager.worker.join(timeout=5)
        self.assertFalse(self.manager.worker.is_alive())

    def tearDown(self):
        if self.manager.worker:self.manager.worker.join(timeout=5)
        self.recorder.close();self.temp.cleanup()

    def test_automatic_check_never_downloads_installs_or_creates_state(self):
        self.manager.tick();self.wait_checked();self.manager.tick()
        self.assertEqual(self.hits,[FEED])
        self.assertFalse(self.launched)
        self.assertFalse(self.manager.root.exists())
        state=self.manager.status()
        self.assertEqual(state['source'],'gitee')
        self.assertFalse(state['automatic_install'])
        self.assertTrue(state['releases'][0]['eligible'])

    def test_manual_confirm_download_integrity_and_persistent_completion(self):
        self.checked()
        with self.assertRaisesRegex(UpdateError,'CONFIRMATION_REQUIRED'):self.manager.install('0.1.3',False)
        self.manager.install('0.1.3',True);self.finish()
        self.assertEqual(len(self.launched),1)
        self.assertEqual(self.guards,2)
        self.assertEqual(self.hits,[FEED,BASE+self.release['path']])
        op=self.manager.status()['operation']
        self.assertEqual(op['state'],'installing')
        with self.assertRaisesRegex(UpdateError,'ALREADY_RUNNING'):self.manager.install('0.1.3',True)
        write(self.manager.root/op['operation_id']/'result.json',{'operation_id':op['operation_id'],'state':'succeeded'})
        fresh=Updates(self.app,lambda:None,lambda:{'gateway_version':'0.1.3'},self.recorder,running=lambda:False)
        self.assertEqual(fresh.status()['operation']['state'],'succeeded')
        self.assertFalse(self.launched[0][1].joinpath('package/gateway.json').exists())

    def test_busy_or_pending_config_fails_before_any_download(self):
        self.checked();self.guard_error=True
        with self.assertRaisesRegex(UpdateError,'TASKS_NOT_IDLE'):self.manager.install('0.1.3',True)
        self.assertEqual(self.hits,[FEED]);self.assertFalse(self.manager.root.exists())

    def test_hash_failure_never_launches_or_replaces_existing_application(self):
        self.checked();self.data += b'TAMPERED'
        original=self.app/'legacy.txt';original.write_text('keep')
        self.manager.install('0.1.3',True);self.finish()
        self.assertEqual(self.manager.status()['operation']['error_code'],'UPDATE_HASH_MISMATCH')
        self.assertFalse(self.launched);self.assertEqual(original.read_text(),'keep')

    def test_fresh_check_required_and_no_downgrades(self):
        with self.assertRaisesRegex(UpdateError,'CHECK_REQUIRED'):self.manager.install('0.1.3',True)
        self.checked()
        self.manager.current=lambda:{'gateway_version':'0.2.0'}
        self.assertFalse(self.manager.status()['releases'][0]['eligible'])
        with self.assertRaisesRegex(UpdateError,'NOT_AVAILABLE'):self.manager.install('0.1.3',True)
        self.manager.current=lambda:{'gateway_version':'0.1.1'}
        self.manager.checked_at-=7*3600
        with self.assertRaisesRegex(UpdateError,'CHECK_REQUIRED'):self.manager.install('0.1.3',True)

    def test_unavailable_mirror_keeps_old_service_and_no_automatic_retry(self):
        self.checked()
        self.manager.fetcher=lambda *args:(_ for _ in ()).throw(OSError('DO_NOT_EXPORT_SECRET'))
        self.manager.check();self.wait_checked()
        self.assertEqual(self.manager.status()['check_error'],'UPDATE_SOURCE_UNAVAILABLE')
        with self.assertRaisesRegex(UpdateError,'CHECK_REQUIRED'):self.manager.install('0.1.3',True)
        self.assertNotIn('DO_NOT_EXPORT',json.dumps(self.recorder.history()))

    def test_interrupted_upgrade_is_reported_and_never_replayed(self):
        self.checked();self.manager.install('0.1.3',True);self.finish()
        path=self.manager.root/'current.json'
        value=json.loads(path.read_text());value['updated_at']=time.time()-120;write(path,value)
        fresh=Updates(self.app,lambda:None,lambda:{},self.recorder,running=lambda:False)
        self.assertEqual(fresh.status()['operation']['state'],'interrupted')
        # Stop the synthetic manager's optional metadata check, not any update.
        self.assertEqual(len(self.launched),1)

    def test_feed_html_offsite_path_duplicate_and_zip_escape_are_rejected(self):
        for data in (b'<html>login</html>',b'[]'):
            with self.assertRaises(UpdateError):validate_feed(data)
        for path in ('https://github.com/a.zip','../a.zip','updates/packages/other.zip'):
            feed=json.loads(json.dumps(self.feed));feed['releases'][0]['path']=path
            with self.assertRaises(UpdateError):validate_feed(json.dumps(feed).encode())
        feed=json.loads(json.dumps(self.feed));feed['releases']*=2
        with self.assertRaises(UpdateError):validate_feed(json.dumps(feed).encode())
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w') as archive:archive.writestr('../outside','BAD')
        data=raw.getvalue();release=dict(self.release,bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        with self.assertRaisesRegex(UpdateError,'PACKAGE_INVALID'):extract_package(data,release,self.root/'extract')
        self.assertFalse((self.root/'outside').exists())
        for url in ('http://127.0.0.1/private','https://github.com/a','https://gitee.com/other/raw/main/x',BASE+'../private',BASE+'updates/stable.json?token=secret'):
            self.assertFalse(trusted_url(url))
        cdn='https://raw.giteeusercontent.com/spikewangzy/wind-terminal-relay/raw/main/updates/stable.json?metadata=abc&signature=def'
        self.assertTrue(trusted_url(cdn))
        self.assertFalse(trusted_url(cdn.replace('wind-terminal-relay','another-repository')))
        self.assertFalse(trusted_url(cdn.replace('raw.giteeusercontent.com','evil.invalid')))

    def test_diagnostic_summary_excludes_feed_notes_paths_and_private_fields(self):
        state=self.manager.status()
        state.update(token='DO_NOT_EXPORT',url='DO_NOT_EXPORT',releases=[{'notes':'DO_NOT_EXPORT'}])
        clean=safe_snapshot({'updates':state})
        self.assertNotIn('DO_NOT_EXPORT',json.dumps(clean))
        self.assertEqual(clean['updates']['source'],'gitee')
        self.assertEqual(clean,safe_snapshot(clean))
        self.assertEqual(safe_snapshot({}),safe_snapshot(safe_snapshot({})))

    def test_package_build_is_reproducible(self):
        other=build(self.root/'again')
        self.assertEqual(other['sha256'],self.release['sha256'])

    def test_committed_feed_includes_archive_and_rejects_stale_or_replaced_payload(self):
        output=self.root/'updates'
        published=publish(output,'合成更新说明','2026-10-16')
        self.assertEqual(publish(output,'','',check=True)['sha256'],published['sha256'])
        releases=validate_feed((output/'stable.json').read_bytes())
        payload=output/'packages'/Path(releases[0]['path']).name
        payload.write_bytes(b'TAMPERED')
        with self.assertRaisesRegex(ValueError,'stale'):publish(output,'','',check=True)
        with self.assertRaisesRegex(ValueError,'immutable'):publish(output,'合成说明','2026-10-16')

    def test_updates_require_local_authorization_and_explicit_confirmation(self):
        self.checked()
        monitor=Monitor(None,None,8765,10,self.recorder)
        monitor.management=object();monitor.console_key='a'*43;monitor.updates=self.manager
        server=make_server(monitor,0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url='http://127.0.0.1:'+str(server.server_port)
        try:
            for headers in ({},{'X-Console-Token':'a'*43,'Origin':'https://evil.invalid'}):
                request=urllib.request.Request(url+'/api/updates/install',data=b'{"gateway_version":"0.1.3","confirmed":true}',headers=dict(headers,**{'Content-Type':'application/json'}))
                with self.assertRaises(urllib.error.HTTPError) as raised:urllib.request.urlopen(request)
                self.assertEqual(raised.exception.code,403)
            request=urllib.request.Request(url+'/api/updates/install',data=b'{"gateway_version":"0.1.3","confirmed":false}',headers={'X-Console-Token':'a'*43,'Content-Type':'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as raised:urllib.request.urlopen(request)
            self.assertEqual(raised.exception.code,400)
            self.assertEqual(self.hits,[FEED]);self.assertFalse(self.launched)
            with urllib.request.urlopen(url+'/api/updates') as response:
                self.assertFalse(json.load(response)['automatic_install'])
        finally:
            server.shutdown();server.server_close();thread.join()
