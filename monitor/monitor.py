"""Loopback-only relay console. No Wind imports, queries or database writes."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hmac
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from .diagnostics import VERSION, Recorder, bundle_name
    from .management import Management, ManagementError, access_key
except ImportError:  # Running this portable file directly, outside a package.
    from diagnostics import VERSION, Recorder, bundle_name
    from management import Management, ManagementError, access_key

HERE = Path(__file__).resolve().parent
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
QUERY_TOOLS = {"query_wind_data"}


def safe_label(value):
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else None


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def database_path(config_path):
    config_path = Path(config_path).resolve()
    value = Path(read_json(config_path)["database"]).expanduser()
    return value if value.is_absolute() else config_path.parent / value


def read_snapshot(path, now=None):
    """One consistent read transaction. SQLite JSON extraction avoids raw exports.

    Read-only mode still honours live WAL state; immutable mode would hide it.
    Old retained jobs are not treated as a complete seven-day usage ledger.
    """
    now = time.time() if now is None else now
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError("Queue database not found")
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"jobs", "worker"}.issubset(tables):
            raise ValueError("Unsupported queue schema")
        row = con.execute("SELECT seen,mode FROM worker WHERE id=1").fetchone()
        age = now - row["seen"] if row and row["seen"] is not None else None
        worker = {"online": age is not None and 0 <= age < 30,
                  "age_seconds": age, "mode": safe_label(row["mode"]) if row else None,
                  "clock_warning": age is not None and age < 0}
        # Never select payload, arguments, raw market values, messages or leases.
        rows = con.execute("""SELECT id,owner,state,created,updated,lease_until,
          CASE WHEN json_valid(payload) THEN json_extract(payload,'$.tool') END AS tool,
          CASE WHEN json_valid(result) THEN json_extract(result,'$.ok') END AS ok,
          CASE WHEN json_valid(result) THEN json_extract(result,'$.relay_execution.mode') END AS mode,
          CASE WHEN json_valid(result) THEN json_extract(result,'$.from_cache') END AS cached,
          CASE WHEN json_valid(result) THEN json_extract(result,'$.code') END AS code,
          CASE WHEN json_valid(result) THEN json_extract(result,'$.receipt_id') END AS receipt,
          CASE WHEN json_valid(result) THEN json_extract(result,'$.raw.ErrorCode') END AS upstream_code,
          CASE WHEN json_valid(result) THEN json_type(result,'$.raw.Data') END AS data_type,
          CASE WHEN json_valid(result) THEN json_extract(result,'$.data_source_id') END AS source
          FROM jobs WHERE created>=? OR updated>=? OR state IN ('queued','running','uncertain')
          ORDER BY created DESC""", (now - 86400, now - 86400)).fetchall()
        jobs = []
        counts = dict.fromkeys(("queued", "running", "uncertain", "expired_wait"), 0)
        devices = {}
        hourly = [{"from": now - (24-i)*3600, "accepted": 0, "failed": 0} for i in range(24)]
        success = failed = unknown = accepted = 0
        latest_data = None
        for row in rows:
            state = row["state"]
            overdue = state == "running" and row["lease_until"] is not None and row["lease_until"] < now
            expired_wait = state == "queued" and row["created"] < now - 1800
            effective = "uncertain" if overdue else "expired_wait" if expired_wait else state
            if effective in counts:
                counts[effective] += 1
            owner = safe_label(row["owner"]) or "unknown"
            device = devices.setdefault(owner, {"client_id": owner, "accepted": 0, "succeeded": 0,
                                                "failed": 0, "pending": 0})
            if state in {"queued", "running", "uncertain"}:
                device["pending"] += 1
            in_window = now - 86400 <= row["created"] <= now
            is_failure = state == "failed" or (state == "completed" and row["ok"] == 0)
            if in_window:
                accepted += 1
                device["accepted"] += 1
                success += int(state == "completed" and row["ok"] == 1)
                failed += int(is_failure)
                unknown += int(state == "completed" and row["ok"] is None)
                device["succeeded"] += int(state == "completed" and row["ok"] == 1)
                device["failed"] += int(is_failure)
                index = min(23, int((row["created"] - (now-86400)) // 3600))
                hourly[index]["accepted"] += 1
                hourly[index]["failed"] += int(is_failure)
            source = safe_label(row["source"])
            mode = safe_label(row["mode"])
            # Source identity alone never upgrades a mock or unclassified job.
            native_data = (row["tool"] in QUERY_TOOLS and state == "completed" and row["ok"] == 1
                           and mode == "native" and source == "wind_terminal_api"
                           and row["upstream_code"] == 0 and row["data_type"] == "array"
                           and row["cached"] == 0)
            job = {"job_id": safe_label(row["id"]), "client_id": owner,
                   "tool": safe_label(row["tool"]), "state": state,
                   "display_state": effective, "created_at": row["created"],
                   "updated_at": row["updated"], "age_seconds": max(0, now-row["created"]),
                   "result_ok": None if row["ok"] is None else bool(row["ok"]),
                   "mode": mode, "cached": None if row["cached"] is None else bool(row["cached"]),
                   "error_code": safe_label(row["code"]) if is_failure else None,
                   "receipt_id": safe_label(row["receipt"]), "native_data_return": native_data}
            jobs.append(job)
            if native_data and (latest_data is None or row["updated"] > latest_data["updated_at"]):
                latest_data = job
        usage = {"available": False, "reservations": None, "estimated_cells": None,
                 "quota_failures": None, "unknown_jobs": None, "scope": "relay_only"}
        if {"relay_usage", "relay_reservation_events", "relay_quota_observations"}.issubset(tables):
            # Separate reservation-event time from task acceptance time.
            event = con.execute("""SELECT COUNT(*),COALESCE(SUM(estimated_cells),0),
                SUM(estimated_cells IS NULL) FROM relay_reservation_events
                WHERE mode='native' AND observed_at>=? AND observed_at<=?""", (now-7*86400, now)).fetchone()
            quota = con.execute("""SELECT COUNT(*) FROM relay_quota_observations
                WHERE mode='native' AND quota_failure=1 AND observed_at>=? AND observed_at<=?""",
                                (now-7*86400, now)).fetchone()[0]
            coverage = con.execute("""SELECT COUNT(*),SUM(CASE
                WHEN json_valid(telemetry) THEN
                  COALESCE(json_extract(telemetry,'$.reservation_events_complete'),0)!=1
                ELSE 1 END) FROM relay_usage
                WHERE accepted_at<=? AND (finished_at IS NULL OR finished_at>=?)""", (now, now-7*86400)).fetchone()
            usage.update(available=True, reservations=event[0], estimated_cells=event[1],
                         unestimated_reservations=event[2] or 0, quota_failures=quota,
                         unknown_jobs=coverage[1] or 0, observed_jobs=coverage[0])
        return {"collected_at": now, "database_ok": True, "worker": worker,
                "pending": counts, "queue_blocked": counts["uncertain"] > 0,
                "retained_24h": {"accepted": accepted, "succeeded": success, "failed": failed,
                                  "result_unknown": unknown},
                "latest_native_data": latest_data, "usage_7d": usage,
                "devices": sorted(devices.values(), key=lambda r: (-r["pending"], r["client_id"])),
                "active_jobs": [j for j in jobs if j["state"] in {"queued", "running", "uncertain"}],
                "recent_jobs": jobs[:30], "hourly": hourly}
    finally:
        con.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Bearer credentials never follow redirects.


def probe(url, token, recorder=None, stage="gateway_probe"):
    parts = urllib.parse.urlsplit(url)
    if (parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}
            or not parts.hostname or (parts.scheme != "https" and
               not (parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost"}))):
        return {"ok": False, "reason": "INVALID_PROBE_URL"}
    request = urllib.request.Request(url.rstrip("/") + "/v1/status",
                                     headers={"Authorization": "Bearer " + token})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=4) as response:
            value = json.loads(response.read(65537))
        if type(value.get("worker_online")) is not bool or value.get("protocol_version") != 1:
            return {"ok": False, "reason": "UNEXPECTED_SERVICE"}
        return {"ok": True, "worker_online": value["worker_online"],
                "execution_paused": value.get("execution_paused"),
                "worker_mode": safe_label(value.get("worker_mode")),
                "relay_version": safe_label(value.get("relay_version")),
                "mcp_version": safe_label(str(value.get("mcp_version", "")).replace(".", "_"))}
    except urllib.error.HTTPError as exc:
        if recorder is not None:
            recorder.record("probe_failed", stage, "HTTP_"+str(exc.code), exc=exc)
        return {"ok": False, "reason": "HTTP_" + str(exc.code)}
    except Exception as exc:
        reason = getattr(exc, "reason", exc)
        code = ("DNS_ERROR" if isinstance(reason, socket.gaierror) else
                "TIMEOUT" if isinstance(reason, TimeoutError) else
                "TLS_ERROR" if type(reason).__name__ in {"SSLError", "SSLCertVerificationError"} else
                "INVALID_STATUS_JSON" if isinstance(exc, (ValueError, UnicodeError, AttributeError)) else
                "NETWORK_ERROR")
        if recorder is not None:
            recorder.record("probe_failed", stage, code, exc=exc)
        return {"ok": False, "reason": code}


def collect(config_path, client_path, gateway_port, recorder=None):
    now = time.time()
    try:
        path = database_path(config_path)
        value = read_snapshot(path, now)
        value["disk_free_gb"] = round(shutil.disk_usage(path.parent).free / 1024**3, 1)
    except Exception as exc:
        value = {"collected_at": now, "database_ok": False, "reason": "DATABASE_UNAVAILABLE"}
        if recorder is not None:
            recorder.record("database_failed", "database", "DATABASE_UNAVAILABLE", exc=exc)
    try:
        client = read_json(client_path)
        token = client["token"]
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise ValueError("Invalid token")
        with ThreadPoolExecutor(max_workers=2) as pool:
            local = pool.submit(probe, "http://127.0.0.1:" + str(gateway_port), token, recorder, "gateway_local")
            public = pool.submit(probe, client["url"], token, recorder, "gateway_public")
            value["gateway"] = local.result()
            value["public_access"] = public.result()
    except Exception as exc:
        value["gateway"] = {"ok": None, "reason": "CLIENT_CONFIG_UNAVAILABLE"}
        value["public_access"] = {"ok": None, "reason": "CLIENT_CONFIG_UNAVAILABLE"}
        if recorder is not None:
            recorder.record("configuration_failed", "client_config", "CLIENT_CONFIG_UNAVAILABLE", exc=exc)
    value["probe_finished_at"] = time.time()
    return value


class Monitor:
    def __init__(self, config, client, port, interval, recorder=None):
        self.config, self.client, self.port, self.interval = config, client, port, interval
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.value = {"loading": True, "collected_at": time.time(), "diagnostics": {"version": VERSION}}
        self.management = None
        self.console_key = None
        self.recorder = recorder if recorder is not None else Recorder()

    def run(self):
        while not self.stop.is_set():
            try:
                value = collect(self.config, self.client, self.port, self.recorder)
                self.recorder.observe(value)
            except Exception as exc:
                self.recorder.record("collection_crashed", "monitor", "MONITOR_COLLECTION_FAILED", exc=exc)
                value = {"collected_at": time.time(), "database_ok": False, "reason": "MONITOR_COLLECTION_FAILED"}
            value["diagnostics"] = {"version": VERSION, "logging_available": self.recorder.available}
            with self.lock:
                self.value = value
            self.stop.wait(self.interval)

    def export(self):
        with self.lock:
            value = self.value
        return self.recorder.bundle(value, HERE)


def make_server(monitor, port):
    class MonitorServer(ThreadingHTTPServer):
        # Windows SO_REUSEADDR permits two listeners on one port. Use exclusive
        # ownership there; Unix retains its normal quick-restart behaviour.
        allow_reuse_address = os.name != "nt"

        def server_bind(self):
            if os.name == "nt":
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

        def handle_error(self, request, client_address):
            monitor.recorder.record("request_failed", "http_server", "MONITOR_HTTP_ERROR", exc=sys.exc_info()[1])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def allowed_origin(self):
            hosts = {"127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)}
            return (self.headers.get("Host") in hosts and
                    (not self.headers.get("Origin") or self.headers["Origin"] in {"http://" + h for h in hosts}))

        def authorized(self):
            supplied = self.headers.get("X-Console-Token", "")
            return (monitor.management is not None and bool(monitor.console_key) and
                    re.fullmatch(r"[A-Za-z0-9_-]{43,128}", supplied) is not None and hmac.compare_digest(supplied, monitor.console_key))

        def respond(self, data, mime="application/json; charset=utf-8", status=200, filename=None):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            if filename is not None:
                self.send_header("Content-Disposition", 'attachment; filename="'+filename+'"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def management_error(self, exc):
            if isinstance(exc, ManagementError):
                self.respond({"error": exc.code}, status=exc.status)
            else:
                monitor.recorder.record("management_failed", "management", "MANAGEMENT_FAILED", exc=exc)
                self.respond({"error": "MANAGEMENT_FAILED"}, status=500)

        def do_GET(self):
            if not self.allowed_origin():
                self.respond({"error": "ORIGIN_DENIED"}, status=403)
                return
            if self.path == "/":
                self.respond((HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/status":
                with monitor.lock:
                    data = dict(monitor.value)
                self.respond(data)
            elif self.path == "/api/diagnostics":
                monitor.recorder.record("diagnostics_exported", "export", "DIAGNOSTICS_EXPORTED")
                self.respond(monitor.export(), "application/zip", filename=bundle_name())
            elif self.path == "/api/manage/devices" or self.path.startswith("/api/manage/download/"):
                if not self.authorized():
                    self.respond({"error": "CONSOLE_LOCKED"}, status=403)
                    return
                try:
                    if self.path == "/api/manage/devices":
                        self.respond(monitor.management.listing())
                    else:
                        client_id = self.path.removeprefix("/api/manage/download/")
                        data = monitor.management.download(client_id)
                        self.respond(data, filename="client-"+client_id+".json")
                except Exception as exc:
                    self.management_error(exc)
            else:
                self.respond({"error": "NOT_FOUND"}, status=404)

        def do_POST(self):
            if not self.allowed_origin() or not self.authorized():
                self.respond({"error": "CONSOLE_LOCKED"}, status=403)
                return
            if self.path not in {"/api/manage/add", "/api/manage/revoke", "/api/manage/verify-reload"}:
                self.respond({"error": "NOT_FOUND"}, status=404)
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self.respond({"error": "JSON_REQUIRED"}, status=415)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 2 <= length <= 4096 or self.headers.get("Transfer-Encoding"):
                    raise ValueError()
                self.connection.settimeout(5)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or set(body) - {"client_id", "confirmation"}:
                    raise ValueError()
            except (ValueError, TimeoutError, OSError):
                self.respond({"error": "INVALID_REQUEST"}, status=400)
                return
            try:
                if self.path.endswith("verify-reload"):
                    result = monitor.management.verify_reload()
                else:
                    result = monitor.management.change(self.path.rsplit("/", 1)[-1], body.get("client_id"), body.get("confirmation"))
                self.respond(result)
            except Exception as exc:
                self.management_error(exc)

    return MonitorServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE.parent / ".relay/gateway.json")
    parser.add_argument("--client-config", type=Path, default=HERE.parent / ".relay/client-owner.json")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--gateway-port", type=int, default=8765)
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--log-dir", type=Path, default=HERE / "logs")
    parser.add_argument("--console-key", type=Path, default=HERE / "auth/console.key")
    parser.add_argument("--open-console", action="store_true", help="Open the authenticated desktop entry; never starts a second server")
    parser.add_argument("--export-diagnostics", action="store_true", help="Export without starting or requiring the monitor server")
    parser.add_argument("--output", type=Path, help="Diagnostic ZIP destination; defaults to monitor/diagnostics/")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not 1 <= args.gateway_port <= 65535 or args.interval < 5:
        parser.error("Use an unprivileged monitor port and interval >= 5 seconds")
    if args.open_console:
        try:
            import webbrowser
            key = access_key(args.console_key)
            webbrowser.open("http://127.0.0.1:"+str(args.port)+"/#key="+key)
        except Exception:
            parser.exit(1, "Console entry unavailable; run the console updater on this cloud computer.\n")
        return
    # CLI export must not open a second rotating writer while the service may
    # still be running; it reads prior logs and keeps fresh checks in memory.
    recorder = Recorder(args.log_dir, read_only=args.export_diagnostics)
    recorder.record("started", "startup", "MONITOR_STARTED")
    monitor = Monitor(args.config, args.client_config, args.gateway_port, args.interval, recorder)
    if args.export_diagnostics:
        try:
            monitor.value = collect(args.config, args.client_config, args.gateway_port, recorder)
            recorder.observe(monitor.value)
            recorder.record("diagnostics_exported", "export", "DIAGNOSTICS_EXPORTED")
            output = args.output or HERE / "diagnostics" / bundle_name()
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Never silently overwrite a previous diagnostic bundle.
            with output.open("xb") as file:
                output.chmod(0o600)
                file.write(monitor.export())
            print("Diagnostic ZIP: " + str(output.resolve()), flush=True)
        except Exception as exc:
            recorder.record("export_failed", "export", "DIAGNOSTICS_EXPORT_FAILED", exc=exc)
            parser.exit(1, "Diagnostic export failed; check the monitor logs or choose another output file.\n")
        finally:
            recorder.close()
        return
    try:
        monitor.console_key = access_key(args.console_key)
        monitor.management = Management(args.config, args.client_config,
            lambda: dict(read_snapshot(database_path(args.config)), database_ok=True), recorder, args.gateway_port)
    except Exception as exc:
        recorder.record("management_unavailable", "management", "READ_ONLY_MODE", exc=exc)
    try:
        server = make_server(monitor, args.port)
    except OSError as exc:
        recorder.record("startup_failed", "listener", "MONITOR_PORT_UNAVAILABLE", exc=exc)
        recorder.close()
        parser.exit(1, "Monitor port unavailable; check the existing monitor. No new instance started.\n")
    thread = threading.Thread(target=monitor.run, daemon=True)
    thread.start()
    print("Monitor: http://127.0.0.1:" + str(args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        recorder.record("server_crashed", "http_server", "MONITOR_SERVER_FAILED", exc=exc)
        raise SystemExit("Monitor stopped after an error; export diagnostics to inspect it.") from None
    finally:
        monitor.stop.set()
        server.server_close()
        thread.join(timeout=10)
        recorder.record("stopped", "shutdown", "MONITOR_STOPPED")
        recorder.close()


if __name__ == "__main__":
    main()
