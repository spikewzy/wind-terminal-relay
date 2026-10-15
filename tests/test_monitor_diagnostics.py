"""Diagnostics stay bounded and cannot package raw credentials or market data."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile

from monitor.diagnostics import Recorder, VERSION, clean_event, error_details
from monitor.monitor import HERE, Monitor, collect, make_server, probe


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.recorder = Recorder(self.root / "logs")

    def tearDown(self):
        self.recorder.close()
        self.temp.cleanup()

    def test_errors_have_type_but_no_exception_text_or_paths(self):
        try:
            raise FileNotFoundError(2, "Bearer DO_NOT_EXPORT_ERROR", "/private/DO_NOT_EXPORT_PATH")
        except FileNotFoundError as exc:
            self.recorder.record("database_failed", "database", "DATABASE_UNAVAILABLE", exc=exc)
            details = error_details(exc)
        self.assertEqual(details["exception_type"], "FileNotFoundError")
        self.assertEqual(details["errno"], 2)
        text = (self.root/"logs/monitor.log").read_text()
        self.assertNotIn("DO_NOT_EXPORT", text)
        self.assertNotIn("/private/", text)

    def test_history_preserves_valid_old_versions_without_arbitrary_version_text(self):
        self.assertEqual(clean_event({"version": "0.1.0"})["version"], "0.1.0")
        self.assertIsNone(clean_event({"version": "DO_NOT_EXPORT_VERSION"})["version"])

    def test_rotation_is_bounded_and_events_remain_valid_json(self):
        self.recorder.close()
        self.recorder = Recorder(self.root/"rotate", max_bytes=400, backups=3)
        for _ in range(100):
            self.recorder.record("test_event", "monitor", "TEST_EVENT")
        files = list((self.root/"rotate").iterdir())
        self.assertLessEqual(len(files), 4)
        for file in files:
            self.assertLess(file.stat().st_size, 800)
            for line in file.read_text().splitlines():
                self.assertEqual(json.loads(line)["code"], "TEST_EVENT")

    def test_bundle_reapplies_allowlists_even_to_manually_tainted_log(self):
        marker = "DO_NOT_EXPORT_PRIVATE"
        value = {"database_ok": True, "collected_at": 1, "token": marker,
                 "raw": {"Data": [[marker]]}, "arguments": {"secret": marker},
                 "gateway": {"ok": False, "reason": "DNS_ERROR", "token": marker},
                 "recent_jobs": [{"job_id": "a"*32, "error_code": "WIND_BUSY", "message": marker}],
                 "active_jobs": [], "devices": []}
        (self.root/"logs/monitor.log").write_text(json.dumps({
            "event": "old_event", "message": marker, "snapshot": value,
            "error": {"exception_type": "ValueError", "message": marker,
                      "frames": [{"file": marker+".py", "function": marker, "line": 1}]}})+"\n")
        self.recorder.record("task_problem", "relay_task", "TEST_EVENT", snapshot=value)
        package = self.recorder.bundle(value, HERE)
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            self.assertEqual(set(archive.namelist()), {"summary.json", "events.jsonl", "README.txt"})
            text = "\n".join(archive.read(name).decode() for name in archive.namelist())
            self.assertNotIn(marker, text)
            self.assertNotIn('"arguments"', text)
            self.assertNotIn('"raw"', text)
            self.assertIn("WIND_BUSY", text)
            summary = json.loads(archive.read("summary.json"))
            self.assertEqual(summary["monitor_version"], VERSION)
            self.assertFalse(summary["raw_gateway_worker_logs_included"])

    def test_unwritable_logging_falls_back_to_memory(self):
        blocked = self.root/"not-a-directory"
        blocked.write_text("file")
        recorder = Recorder(blocked)
        try:
            self.assertFalse(recorder.available)
            self.assertEqual(recorder.history()[0]["code"], "LOGGING_UNAVAILABLE")
            with zipfile.ZipFile(io.BytesIO(recorder.bundle({"database_ok": False}, HERE))) as archive:
                self.assertFalse(json.loads(archive.read("summary.json"))["logging_available"])
        finally:
            recorder.close()

    def test_safe_corrupt_log_is_skipped_without_exporting_arbitrary_files(self):
        (self.root/"logs/monitor.log.1").write_text('broken-json\n'+json.dumps(["PRIVATE"])+"\n")
        (self.root/"logs/gateway.log").write_text("DO_NOT_EXPORT_RAW_GATEWAY")
        self.recorder.record("test_event", "monitor", "TEST_EVENT")
        with zipfile.ZipFile(io.BytesIO(self.recorder.bundle({}, HERE))) as archive:
            text=archive.read("events.jsonl").decode()
            self.assertNotIn("DO_NOT_EXPORT", text)
            self.assertIn("TEST_EVENT", text)

    def test_state_changes_and_failed_jobs_are_not_logged_on_every_refresh(self):
        value = {"database_ok": True, "gateway": {"ok": True}, "worker": {"online": True, "mode": "native"},
                 "recent_jobs": [{"job_id": "a"*32, "state": "completed", "result_ok": False}]}
        self.recorder.observe(value)
        self.recorder.observe(value)
        events = self.recorder.history()
        self.assertEqual(sum(row.get("event")=="status_change" for row in events), 1)
        self.assertEqual(sum(row.get("event")=="task_problem" for row in events), 1)

    def test_detailed_collect_errors_do_not_expose_private_config(self):
        value = collect(self.root/"missing-config.json", self.root/"missing-client.json", 1, self.recorder)
        self.assertFalse(value["database_ok"])
        events = self.recorder.history()
        self.assertEqual({row.get("stage") for row in events}, {"database", "client_config"})
        self.assertTrue(all(row["error"]["exception_type"]=="FileNotFoundError" for row in events))

    def test_dns_error_category_is_visible_without_raw_exception_message(self):
        import socket
        class Opener:
            def open(self, request, timeout):
                raise urllib.error.URLError(socket.gaierror(1, "DO_NOT_EXPORT_HOST"))
        with patch("monitor.monitor.urllib.request.build_opener", return_value=Opener()):
            result = probe("https://relay.example.com", "DO_NOT_EXPORT_TOKEN", self.recorder)
        self.assertEqual(result["reason"], "DNS_ERROR")
        text = json.dumps(self.recorder.history())
        self.assertIn("gaierror", text)
        self.assertNotIn("DO_NOT_EXPORT", text)

    def test_cli_exports_when_monitor_and_gateway_are_not_running(self):
        output = self.root/"report.zip"
        self.recorder.record("old_service_event", "monitor", "EXISTING_SERVICE_LOG")
        saved_log = (self.root/"logs/monitor.log").read_bytes()
        command = [sys.executable, str(HERE/"monitor.py"), "--export-diagnostics", "--output", str(output),
                   "--config", str(self.root/"missing.json"), "--client-config", str(self.root/"missing-client.json"),
                   "--log-dir", str(self.root/"logs")]
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(output) as archive:
            summary = json.loads(archive.read("summary.json"))
            self.assertFalse(summary["snapshot"]["database_ok"])
            self.assertEqual(summary["export_mode"], "read_only_log_reader")
            self.assertIn("EXISTING_SERVICE_LOG", archive.read("events.jsonl").decode())
        self.assertEqual((self.root/"logs/monitor.log").read_bytes(), saved_log)
        before = output.read_bytes()
        repeated = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(repeated.returncode, 0)
        self.assertEqual(output.read_bytes(), before)

    def test_http_export_does_not_collect_submit_or_modify_tasks(self):
        monitor = Monitor(None, None, 8765, 10, self.recorder)
        monitor.value = {"collected_at": 1, "database_ok": False, "reason": "DATABASE_UNAVAILABLE"}
        server = make_server(monitor, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:"+str(server.server_port)+"/api/diagnostics"
        try:
            with patch("monitor.monitor.collect", side_effect=AssertionError("Export should use its cached snapshot")):
                with urllib.request.urlopen(url) as response:
                    self.assertEqual(response.headers["Content-Type"], "application/zip")
                    self.assertIn("attachment", response.headers["Content-Disposition"])
                    with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                        self.assertIn("summary.json", archive.namelist())
            with self.assertRaises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(urllib.request.Request(url, headers={"Origin": "https://hostile.example"}))
            self.assertEqual(exc.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_unexpected_collector_error_is_recorded_and_loop_stays_controlled(self):
        monitor = Monitor(None, None, 8765, 10, self.recorder)
        def fail(*args):
            monitor.stop.set()
            raise RuntimeError("DO_NOT_EXPORT_CRASH")
        with patch("monitor.monitor.collect", side_effect=fail):
            monitor.run()
        self.assertEqual(monitor.value["reason"], "MONITOR_COLLECTION_FAILED")
        self.assertNotIn("DO_NOT_EXPORT", json.dumps(self.recorder.history()))
        self.assertIn("MONITOR_COLLECTION_FAILED", json.dumps(self.recorder.history()))


if __name__ == "__main__":
    unittest.main()
