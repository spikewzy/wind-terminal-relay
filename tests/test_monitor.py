"""Read-only monitor tests. Synthetic local databases; no Wind or public service."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import hashlib
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile

from monitor.monitor import Monitor, collect, database_path, make_server, probe, read_snapshot
from monitor.build_package import build as build_monitor, FILES, ROOT_FILES


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "queue space.sqlite"
        self.now = 1800000000.0
        self.con = sqlite3.connect(self.path)
        self.con.executescript("""PRAGMA journal_mode=WAL;
          CREATE TABLE jobs(id TEXT PRIMARY KEY,owner TEXT,payload TEXT,state TEXT,
            created REAL,updated REAL,lease TEXT,lease_until REAL,result TEXT);
          CREATE TABLE worker(id INTEGER PRIMARY KEY,seen REAL,mode TEXT);
        """)
        self.con.execute("INSERT INTO worker VALUES(1,?, 'native')", (self.now-5,))
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def job(self, identifier="a"*32, *, state="completed", age=100, owner="owner",
            mode="native", cached=False, ok=True, tool="query_wind_data", code=None, result_override=None):
        result = {"ok": ok, "raw": {"ErrorCode": 0, "Data": [[123456.789]]},
                  "arguments": {"secret": "DO_NOT_EXPORT_ARGUMENT"},
                  "message": "DO_NOT_EXPORT_MESSAGE", "receipt_id": "d"*32,
                  "data_source_id": "wind_terminal_api", "relay_execution": {"mode": mode},
                  "from_cache": cached, "code": code}
        if result_override is not None:
            result = result_override
        self.con.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)", (
            identifier, owner, json.dumps({"tool": tool, "arguments": {"token": "DO_NOT_EXPORT_TOKEN"}}),
            state, self.now-age, self.now-5, "DO_NOT_EXPORT_LEASE", self.now+60, json.dumps(result)))
        self.con.commit()

    def test_legacy_read_is_non_mutating_and_has_no_fabricated_quota(self):
        self.job(state="running")
        self.con.execute("UPDATE jobs SET lease_until=?", (self.now-1,))
        self.con.commit()
        before = list(self.con.execute("SELECT * FROM jobs"))
        tables_before = list(self.con.execute("SELECT sql FROM sqlite_master ORDER BY name"))
        data = read_snapshot(self.path, self.now)
        self.assertTrue(data["worker"]["online"])
        self.assertTrue(data["queue_blocked"])
        self.assertEqual(data["pending"]["uncertain"], 1)
        self.assertEqual(data["active_jobs"][0]["state"], "running")
        self.assertIsNone(data["usage_7d"]["reservations"])
        self.assertEqual(before, list(self.con.execute("SELECT * FROM jobs")))
        self.assertEqual(tables_before, list(self.con.execute("SELECT sql FROM sqlite_master ORDER BY name")))

    def test_no_market_values_tokens_arguments_messages_or_leases_exported(self):
        self.job()
        text = json.dumps(read_snapshot(self.path, self.now))
        for private in ("DO_NOT_EXPORT", "123456.789", '"arguments"', '"raw"', '"lease"'):
            self.assertNotIn(private, text)

    def test_latest_native_data_excludes_metadata_mock_cache_and_unknown_mode(self):
        self.job(identifier="a"*32)
        self.job(identifier="b"*32, tool="wind_status")
        self.job(identifier="c"*32, mode="mock")
        self.job(identifier="e"*32, cached=True)
        self.job(identifier="f"*32, mode=None)
        data = read_snapshot(self.path, self.now)
        self.assertEqual(data["latest_native_data"]["job_id"], "a"*32)
        self.assertEqual(sum(j["native_data_return"] for j in data["recent_jobs"]), 1)

    def test_recent_return_of_old_job_is_visible_but_not_added_to_acceptance_count(self):
        self.job(age=90000)
        data = read_snapshot(self.path, self.now)
        self.assertEqual(data["latest_native_data"]["job_id"], "a"*32)
        self.assertEqual(data["retained_24h"]["accepted"], 0)

    def test_failed_results_are_not_successes_even_when_queue_state_completed(self):
        self.job(ok=False, code="WIND_QUOTA_EXCEEDED")
        self.job(identifier="b"*32, result_override={})
        data = read_snapshot(self.path, self.now)
        self.assertEqual(data["retained_24h"], {"accepted": 2, "succeeded": 0, "failed": 1, "result_unknown": 1})
        self.assertIsNone(data["latest_native_data"])

    def test_counts_devices_hourly_and_old_pending_reconcile(self):
        self.job(identifier="a"*32, owner="mac")
        self.job(identifier="b"*32, owner="win", state="queued", age=90000)
        self.job(identifier="c"*32, owner="win", state="failed", age=5000)
        self.job(identifier="d"*32, owner="win", state="completed", age=90001)
        data = read_snapshot(self.path, self.now)
        self.assertEqual(data["retained_24h"]["accepted"], 2)
        self.assertEqual(sum(h["accepted"] for h in data["hourly"]), 2)
        self.assertEqual(sum(h["failed"] for h in data["hourly"]), 1)
        self.assertEqual(sum(d["accepted"] for d in data["devices"]), 2)
        self.assertEqual(data["pending"]["expired_wait"], 1)
        self.assertEqual(len(data["active_jobs"]), 1)

    def test_stale_and_future_worker_heartbeat_are_not_online(self):
        for age in (30, 60, -10):
            self.con.execute("UPDATE worker SET seen=?", (self.now-age,))
            self.con.commit()
            data = read_snapshot(self.path, self.now)
            self.assertFalse(data["worker"]["online"])
            self.assertEqual(data["worker"]["clock_warning"], age < 0)

    def test_invalid_json_does_not_take_down_monitor(self):
        self.job(result_override="not-an-object")
        self.con.execute("UPDATE jobs SET result='broken-json',payload='broken-json'")
        self.con.commit()
        self.assertIsNone(read_snapshot(self.path, self.now)["latest_native_data"])

    def test_missing_database_is_not_created(self):
        missing = self.root / "not-created.sqlite"
        with self.assertRaises(FileNotFoundError):
            read_snapshot(missing)
        self.assertFalse(missing.exists())

    def test_relative_config_path_and_graceful_missing_probe(self):
        config = self.root / "gateway.json"
        config.write_text(json.dumps({"database": self.path.name, "clients": {"not-a-token": "private-hash"}}))
        self.assertEqual(database_path(config), self.path.resolve())
        result = collect(config, self.root / "absent.json", 1)
        self.assertTrue(result["database_ok"])
        self.assertIsNone(result["gateway"]["ok"])
        self.assertNotIn("private-hash", json.dumps(result))

    def test_new_usage_counts_execution_event_window_without_claiming_balance(self):
        self.con.executescript("""CREATE TABLE relay_usage(accepted_at REAL,finished_at REAL,telemetry TEXT);
            CREATE TABLE relay_reservation_events(mode TEXT,observed_at REAL,estimated_cells INTEGER);
            CREATE TABLE relay_quota_observations(mode TEXT,observed_at REAL,quota_failure INTEGER);""")
        for mode, age, cells in (("native", 1, 10), ("native", 2, None), ("mock", 1, 900), ("native", 8*86400, 999)):
            self.con.execute("INSERT INTO relay_reservation_events VALUES(?,?,?)", (mode, self.now-age, cells))
        self.con.execute("INSERT INTO relay_quota_observations VALUES('native',?,1)", (self.now-1,))
        self.con.execute("INSERT INTO relay_usage VALUES(?,?,NULL)", (self.now-10, self.now-2))
        self.con.execute("INSERT INTO relay_usage VALUES(?,?,?)", (self.now-10, self.now-2, '{"reservation_events_complete":false}'))
        self.con.commit()
        data = read_snapshot(self.path, self.now)["usage_7d"]
        self.assertEqual(data["reservations"], 2)
        self.assertEqual(data["estimated_cells"], 10)
        self.assertEqual(data["quota_failures"], 1)
        self.assertEqual(data["unknown_jobs"], 2)
        self.assertEqual(data["unestimated_reservations"], 1)
        self.assertNotIn("remaining", data)

    def test_wal_snapshot_sees_commits_without_blocking_writer(self):
        self.job()
        first = read_snapshot(self.path, self.now)
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(read_snapshot, self.path, self.now)
            self.con.execute("UPDATE worker SET mode='mock'")
            self.con.commit()
            self.assertTrue(future.result()["database_ok"])
        self.assertEqual(first["worker"]["mode"], "native")
        self.assertEqual(read_snapshot(self.path, self.now)["worker"]["mode"], "mock")


class MonitorHttpTests(unittest.TestCase):
    def setUp(self):
        self.monitor = Monitor(None, None, 8765, 10)
        self.monitor.value = {"database_ok": True, "collected_at": 1}
        self.server = make_server(self.monitor, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_local_json_and_page_no_cache_or_cross_origin_access(self):
        with urllib.request.urlopen(self.url + "/api/status") as response:
            self.assertEqual(json.load(response), self.monitor.value)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
        with urllib.request.urlopen(self.url) as response:
            self.assertIn("Wind 中继管理台", response.read().decode())
        for headers in ({"Host": "hostile.example"}, {"Origin": "https://hostile.example"}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(self.url+"/api/status", headers=headers))
            self.assertEqual(error.exception.code, 403)

    def test_no_mutation_or_file_routes_and_no_second_listener(self):
        for path in ("/.relay/client-owner.json", "/../monitor.py", "/v1/jobs"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(self.url + path)
            self.assertEqual(error.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(self.url+"/api/status", data=b"{}"))
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(OSError):
            make_server(self.monitor, self.server.server_port)

    def test_probe_never_follows_redirects_or_exports_credentials(self):
        hits = []
        target = self.url
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                hits.append(self.path)
                self.send_response(302)
                self.send_header("Location", target + "/api/status")
                self.end_headers()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = probe("http://127.0.0.1:"+str(server.server_port), "SECRET_PROBE_TOKEN")
            self.assertEqual(result, {"ok": False, "reason": "HTTP_302"})
            self.assertEqual(hits, ["/v1/status"])
            self.assertNotIn("SECRET", json.dumps(result))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_probe_rejects_unsafe_urls(self):
        for url in ("http://example.com", "https://user:secret@example.com", "https://example.com/?token=abc"):
            self.assertEqual(probe(url, "x")["reason"], "INVALID_PROBE_URL")


class MonitorPackageTests(unittest.TestCase):
    def test_package_has_only_reviewed_files_with_verified_hashes_and_windows_line_endings(self):
        with tempfile.TemporaryDirectory() as directory:
            result = build_monitor(directory)
            archive = Path(result["archive"])
            self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), result["sha256"])
            with zipfile.ZipFile(archive) as package:
                self.assertIsNone(package.testzip())
                self.assertEqual(set(package.namelist()), {"monitor/"+name for name in FILES} | set(ROOT_FILES) | {"console-manifest.json"})
                manifest = json.loads(package.read("console-manifest.json"))
                for name, digest in manifest["files"].items():
                    data = package.read(name)
                    self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
                    if name.endswith((".cmd", ".ps1")):
                        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))


if __name__ == "__main__":
    unittest.main()
