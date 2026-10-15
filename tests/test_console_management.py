"""Synthetic administration tests: no real relay, Wind SDK or accounts."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import zipfile

from monitor.management import Management, ManagementError, digest, write, config_lock
from monitor.monitor import Monitor, make_server
from monitor.diagnostics import Recorder


class ConsoleFixture:

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config = self.root/'gateway.json'
        self.client = self.root/'client-owner.json'
        self.original = {'database': 'queue.sqlite', 'clients': {'owner': digest('SYNTHETIC_OWNER')},
                         'worker_sha256': digest('SYNTHETIC_WORKER'), 'legacy_option': True}
        write(self.config, self.original)
        write(self.client, {'url': 'https://example.invalid', 'token': 'SYNTHETIC_OWNER'})
        self.snapshot = {'database_ok': True, 'pending': {}}
        self.recorder = Recorder()
        self.manager = Management(self.config, self.client, lambda: self.snapshot, self.recorder)

    def tearDown(self):
        self.recorder.close()
        self.temp.cleanup()


class ManagementTests(ConsoleFixture, unittest.TestCase):
    def test_add_download_revoke_preserves_credentials_and_backups(self):
        result = self.manager.change('add', 'colleague2-mac')
        self.assertTrue(result['restart_required'])
        config = json.loads(self.config.read_text())
        for key, value in self.original.items():
            if key != 'clients':
                self.assertEqual(config[key], value)
        self.assertEqual(config['clients']['owner'], self.original['clients']['owner'])
        self.assertEqual(len(list((self.root/'console-backups').glob('*.json'))), 1)
        credential = json.loads(self.manager.download('colleague2-mac'))
        self.assertEqual(config['clients']['colleague2-mac'], digest(credential['token']))
        listing = self.manager.listing()
        self.assertNotIn('token', json.dumps(listing))
        self.assertTrue(next(row for row in listing['clients'] if row['client_id']=='owner')['protected'])
        self.manager.change('revoke', 'colleague2-mac', 'colleague2-mac')
        self.assertIn('colleague2-mac', json.loads(self.config.read_text())['retired_clients'])
        self.assertTrue((self.root/'client-colleague2-mac.json').exists())
        with self.assertRaisesRegex(ManagementError, 'CLIENT_ID_USED'):
            self.manager.change('add', 'colleague2-mac')
        with self.assertRaisesRegex(ManagementError, 'CLIENT_NOT_FOUND'):
            self.manager.download('colleague2-mac')

    def test_busy_unknown_protected_invalid_no_mutation(self):
        original = self.config.read_bytes()
        for name in ('queued','running','uncertain','expired_wait'):
            self.snapshot['pending'] = {name: 1}
            with self.assertRaisesRegex(ManagementError, 'TASKS_NOT_IDLE'):
                self.manager.change('add', 'new-device')
        self.snapshot = {'database_ok': False}
        with self.assertRaisesRegex(ManagementError, 'DATABASE_UNAVAILABLE'):
            self.manager.change('add', 'new-device')
        self.snapshot = {'database_ok': True, 'pending': {}}
        with self.assertRaisesRegex(ManagementError, 'PROTECTED_CLIENT'):
            self.manager.change('revoke', 'owner', 'owner')
        for bad in ('../escape', 'a/b', '', '-bad', '中文', 'a'*65, None):
            with self.assertRaises(ManagementError):
                self.manager.change('add', bad)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse((self.root/'console-backups').exists())

    def test_duplicate_confirm_mismatch_and_credential_export_allowlist(self):
        self.manager.change('add', 'new-device')
        with self.assertRaisesRegex(ManagementError, 'CONFIRMATION_REQUIRED'):
            self.manager.change('revoke', 'new-device', 'wrong')
        path = self.root/'client-new-device.json'
        data = json.loads(path.read_text())
        data['private_future_field'] = 'DO_NOT_EXPORT'
        write(path, data)
        self.assertNotIn('DO_NOT_EXPORT', self.manager.download('new-device').decode())
        data['token'] = 'MISMATCH'
        write(path, data)
        with self.assertRaisesRegex(ManagementError, 'CLIENT_FILE_MISMATCH'):
            self.manager.download('new-device')
        self.assertFalse(next(row for row in self.manager.listing()['clients'] if row['client_id']=='new-device')['download_available'])
        self.assertFalse(any('SYNTHETIC_' in json.dumps(event) for event in self.recorder.events))

    @unittest.skipIf(__import__('os').name == 'nt', 'Windows symlink creation requires a separate privilege')
    def test_symlink_new_credential_does_not_escape_or_overwrite(self):
        outside = self.root/'outside'
        outside.write_text('KEEP')
        (self.root/'client-new.json').symlink_to(outside)
        with self.assertRaisesRegex(ManagementError, 'UNSAFE_PATH'):
            self.manager.change('add', 'new')
        self.assertEqual(outside.read_text(), 'KEEP')

    def test_shared_lock_rejects_competing_admin(self):
        with config_lock(self.config):
            with self.assertRaisesRegex(ManagementError, 'CONFIG_BUSY'):
                self.manager.change('add', 'new')

    def test_reload_only_checks_authentication_no_jobs(self):
        manager = self.manager
        loaded = dict(self.original['clients'])
        hits = []
        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                hits.append(self.path)
                token = self.headers.get('Authorization', '').removeprefix('Bearer ')
                self.send_response(200 if digest(token) in loaded.values() else 401)
                self.end_headers()
                self.wfile.write(b'{}')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        manager.port = server.server_port
        try:
            manager.change('add', 'new')
            with self.assertRaisesRegex(ManagementError, 'RELOAD_NOT_CONFIRMED'):
                manager.verify_reload()
            self.assertTrue(manager.listing()['restart_required'])
            loaded.update(json.loads(self.config.read_text())['clients'])
            self.assertTrue(manager.verify_reload()['authentication_verified'])
            self.assertFalse(manager.listing()['restart_required'])
            manager.change('revoke', 'new', 'new')
            with self.assertRaisesRegex(ManagementError, 'RELOAD_NOT_CONFIRMED'):
                manager.verify_reload()
            del loaded['new']
            manager.verify_reload()
            self.assertEqual(set(hits), {'/v1/status'})
        finally:
            server.shutdown();server.server_close();thread.join()


class ConsoleHttpTests(ConsoleFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.monitor = Monitor(None, None, 8765, 10, self.recorder)
        self.monitor.management = self.manager
        self.monitor.console_key = 'a'*43
        self.server = make_server(self.monitor, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
        super().tearDown()

    def request(self, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        return urllib.request.urlopen(urllib.request.Request(self.url+path, data=data, headers=headers or {}))

    def test_write_and_download_require_local_admin_key_and_json(self):
        original = self.config.read_bytes()
        auth = {'X-Console-Token': self.monitor.console_key, 'Content-Type': 'application/json'}
        for headers in ({}, {'Content-Type': 'application/json'},
                        dict(auth, Origin='https://hostile.invalid'), dict(auth, Host='hostile.invalid')):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/manage/add', {'client_id':'new'}, headers)
            self.assertEqual(error.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/manage/add', {'client_id':'new'}, {'X-Console-Token': self.monitor.console_key})
        self.assertEqual(error.exception.code, 415)
        self.assertEqual(original, self.config.read_bytes())
        with self.request('/api/manage/add', {'client_id':'new'}, auth) as response:
            self.assertTrue(json.load(response)['saved'])
        for route in ('/api/manage/devices','/api/manage/download/new'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(route)
            self.assertEqual(error.exception.code, 403)
        with self.request('/api/manage/download/new', headers=auth) as response:
            self.assertIn('client-new.json', response.headers['Content-Disposition'])
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertIn('token', json.load(response))
        with self.request('/api/diagnostics') as response:
            with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                for name in archive.namelist():
                    text = archive.read(name).decode()
                    self.assertNotIn('SYNTHETIC_OWNER', text)
                    self.assertNotIn(self.monitor.console_key, text)
                    self.assertNotIn(json.loads((self.root/'client-new.json').read_text())['token'], text)

    def test_invalid_body_and_download_path_do_not_expose_files(self):
        auth = {'X-Console-Token': self.monitor.console_key, 'Content-Type':'application/json'}
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/manage/add', {'client_id':'new','url':'https://evil.invalid'}, auth)
        self.assertEqual(error.exception.code, 400)
        for path in ('../gateway.json','%2e%2e','owner?token=x'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/manage/download/'+path, headers=auth)
            self.assertEqual(error.exception.code, 400)


if __name__ == '__main__':
    unittest.main()
