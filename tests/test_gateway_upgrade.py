"""Upgrade gates, legacy DB migration and deployment reports; synthetic only."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import httpx
from gateway_update.build_package import build
from gateway_update.support import verify_package, inspect, backup, restore, report, finalize, UpgradeError
from monitor.diagnostics import safe_snapshot
from wind_relay.common import digest, load_contract
from wind_relay.gateway import create_app
from wind_relay.store import Queue


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name).resolve()
        result=build(self.root/'dist')
        self.package=self.root/'package'
        with zipfile.ZipFile(result['archive']) as z:z.extractall(self.package)
        self.app=self.root/'app'
        (self.app/'.relay').mkdir(parents=True)
        (self.app/'references').mkdir()
        (self.app/'references/relay-tool-contract.json').write_text(json.dumps(load_contract()))
        self.config={'database':'queue.sqlite','clients':{'owner':digest('SYNTHETIC_OWNER')},'worker_sha256':digest('SYNTHETIC_WORKER')}
        (self.app/'.relay/gateway.json').write_text(json.dumps(self.config))
        (self.app/'.relay/client-owner.json').write_text(json.dumps({'url':'https://example.invalid','token':'SYNTHETIC_OWNER'}))
        self.db=self.app/'.relay/queue.sqlite'
        with closing(sqlite3.connect(self.db)) as con:
            con.executescript('''PRAGMA journal_mode=WAL;
              CREATE TABLE jobs(id TEXT PRIMARY KEY,owner TEXT,payload TEXT,state TEXT,created REAL,updated REAL,lease TEXT,lease_until REAL,result TEXT);
              CREATE TABLE dedupe(id TEXT PRIMARY KEY,owner TEXT,payload_hash TEXT);
              CREATE TABLE worker(id INTEGER PRIMARY KEY,seen REAL,mode TEXT);
              INSERT INTO jobs VALUES('11111111111111111111111111111111','owner','{}','completed',1,1,NULL,NULL,'{"private":"NOT_IN_REPORT"}');
              INSERT INTO dedupe VALUES('11111111111111111111111111111111','owner','preserve');''')

    def tearDown(self):self.temp.cleanup()

    def test_package_and_read_only_inspection(self):
        self.assertEqual(verify_package(self.package)['version'],'0.1.2')
        self.assertIn('双击 Upgrade-Gateway.cmd',(self.package/'START-HERE.txt').read_text(encoding='utf-8'))
        before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.app/'.relay').glob('*.json')}
        self.assertTrue(inspect(self.app,self.package)['idle'])
        after={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.app/'.relay').glob('*.json')}
        self.assertEqual(before,after)
        (self.package/'gateway/relay_gateway.py').write_text('TAMPERED')
        with self.assertRaisesRegex(UpgradeError,'HASH_MISMATCH'):verify_package(self.package)

    def test_late_job_blocks_final_backup_and_keeps_the_job(self):
        inspect(self.app,self.package)
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("UPDATE jobs SET state='running'");con.commit()
        directory=self.root/'backup'
        with self.assertRaisesRegex(UpgradeError,'TASKS_NOT_IDLE'):backup(self.app,directory)
        self.assertFalse((directory/'queue.sqlite').exists())
        with closing(sqlite3.connect(self.db)) as con:self.assertEqual(con.execute('SELECT state FROM jobs').fetchone()[0],'running')

    def test_wal_backup_and_sqlite_restore_preserve_legacy_receipts_dedupe(self):
        # Keep a reader open while WAL-backed content is backed up/restored.
        with closing(sqlite3.connect(self.db)) as reader:
            reader.execute('SELECT COUNT(*) FROM jobs').fetchone()
            backup(self.app,self.root/'backup')
            Queue(self.db)  # real new schema migration, without Wind or workers
            with closing(sqlite3.connect(self.db)) as con:
                con.execute('CREATE TABLE synthetic_failed_migration(marker TEXT)');con.commit()
            restore(self.app,self.root/'backup')
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT payload_hash FROM dedupe').fetchone()[0],'preserve')
            self.assertIn('NOT_IN_REPORT',con.execute('SELECT result FROM jobs').fetchone()[0])
            tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn('synthetic_failed_migration',tables)
            self.assertNotIn('relay_usage',tables)

    def test_report_unknown_worker_version_and_strict_no_secret_export(self):
        def fake(port,route,token=None):
            if port==8766:return {'diagnostics':{'version':'0.3.0'},'token':'DONT_EXPORT_CONSOLE'}
            return {'protocol_version':1,'worker_online':True,'worker_mode':'native','relay_version':'0.1.2',
                'mcp_version':'0.24.2','server_sha256':'b'*64,'token':'DONT_EXPORT_TOKEN','hostname':'DONT_EXPORT_HOST',
                'deployment':{'build_id':'a'*64,'scheduler':'client_round_robin','single_execution_lane':True},
                'queue_policy':{'client_pending_limit':5,'global_queued_limit':100,'password':'DONT_EXPORT_POLICY'}}
        with patch('gateway_update.support.get_local',side_effect=fake):value=report(self.app)
        self.assertEqual(value['gateway']['version'],'0.1.2')
        self.assertIsNone(value['worker']['version'])
        self.assertFalse(value['worker']['version_verified'])
        self.assertNotIn('DONT_EXPORT',json.dumps(value))
        self.assertNotIn('SYNTHETIC_',json.dumps(value))
        cleaned=safe_snapshot({'deployment':{'gateway_version':'0.1.2','core_contract_version':'0.24.2','token':'DONT_EXPORT','queue_policy':{'client_pending_limit':5,'password':'DONT_EXPORT'}}})
        self.assertEqual(cleaned['deployment']['gateway_version'],'0.1.2')
        self.assertNotIn('DONT_EXPORT',json.dumps(cleaned))

    def test_activation_checks_policy_and_does_not_fail_into_rollback_after_commit(self):
        marker=self.app/'.relay/gateway-maintenance.json'
        marker.write_text(json.dumps({'build_id':'a'*64}))
        value={'gateway':{'runtime_verified':True,'build_id':'a'*64,'version':'0.1.2',
            'maintenance_mode':True,'scheduler':'client_round_robin','queue_policy':{}},'worker':{}}
        with patch('gateway_update.support.report',return_value=value):
            with self.assertRaisesRegex(UpgradeError,'QUEUE_POLICY_VERIFICATION_FAILED'):
                finalize(self.app,'a'*64)
            self.assertTrue(marker.exists())
            value['gateway']['queue_policy']={'client_pending_limit':5,'global_queued_limit':100,'new_jobs_per_minute':30,'global_new_jobs_per_minute':None}
            from gateway_update.support import write as real_write
            calls=[]
            def fail_after_commit(*args,**kwargs):
                calls.append(1)
                if len(calls)>2:raise OSError('SYNTHETIC_REPORT_DISK_FAILURE')
                return real_write(*args,**kwargs)
            with patch('gateway_update.support.write',side_effect=fail_after_commit):
                result=finalize(self.app,'a'*64,self.root/'report.json')
            self.assertTrue(result['activated'])
            self.assertFalse(result['deployment_record_saved'])
            self.assertFalse(marker.exists())


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_maintenance_blocks_submissions_and_claims_but_allows_status(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);marker=root/'maintenance.json';marker.write_text('{}')
            config={'database':str(root/'queue.sqlite'),'clients':{'owner':digest('SYNTHETIC_OWNER')},'worker_sha256':digest('SYNTHETIC_WORKER'),'maintenance_file':str(marker)}
            app=create_app(config)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as client:
                owner={'Authorization':'Bearer SYNTHETIC_OWNER'};worker={'Authorization':'Bearer SYNTHETIC_WORKER'}
                response=await client.get('/v1/status',headers=owner)
                self.assertTrue(response.json()['maintenance_mode'])
                self.assertEqual(response.json()['relay_version'],'0.1.2')
                response=await client.post('/v1/jobs',headers=owner,json={'job_id':'2'*32,'tool':'wind_status','arguments':{'connect':False}})
                self.assertEqual(response.status_code,503)
                self.assertIsNone((await client.post('/v1/worker/claim',headers=worker,json={})).json()['job'])
                with app.state.queue.connection() as con:self.assertEqual(con.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)
                marker.unlink()
                self.assertEqual((await client.post('/v1/jobs',headers=owner,json={'job_id':'2'*32,'tool':'wind_status','arguments':{'connect':False}})).status_code,202)
                self.assertFalse((await client.get('/v1/status',headers=owner)).json()['maintenance_mode'])
            await __import__('asyncio').get_running_loop().shutdown_default_executor()


if __name__=='__main__':unittest.main()
