"""Loopback-only, read-only monitor, compatible with the original relay gateway.

Standard library only. Never imports the queue, Wind core, SDK or executor.
No query submission, database migration, retry, recovery or deletion operations.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

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


def probe(url, token):
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
        return {"ok": False, "reason": "HTTP_" + str(exc.code)}
    except Exception:
        return {"ok": False, "reason": "NETWORK_OR_TLS_ERROR"}


def collect(config_path, client_path, gateway_port):
    now = time.time()
    try:
        path = database_path(config_path)
        value = read_snapshot(path, now)
        value["disk_free_gb"] = round(shutil.disk_usage(path.parent).free / 1024**3, 1)
    except Exception:
        value = {"collected_at": now, "database_ok": False, "reason": "DATABASE_UNAVAILABLE"}
    try:
        client = read_json(client_path)
        token = client["token"]
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise ValueError("Invalid token")
        with ThreadPoolExecutor(max_workers=2) as pool:
            local = pool.submit(probe, "http://127.0.0.1:" + str(gateway_port), token)
            public = pool.submit(probe, client["url"], token)
            value["gateway"] = local.result()
            value["public_access"] = public.result()
    except Exception:
        value["gateway"] = {"ok": None, "reason": "CLIENT_CONFIG_UNAVAILABLE"}
        value["public_access"] = {"ok": None, "reason": "CLIENT_CONFIG_UNAVAILABLE"}
    value["probe_finished_at"] = time.time()
    return value


class Monitor:
    def __init__(self, config, client, port, interval):
        self.config, self.client, self.port, self.interval = config, client, port, interval
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.value = {"loading": True, "collected_at": time.time()}

    def run(self):
        while not self.stop.is_set():
            value = collect(self.config, self.client, self.port)
            with self.lock:
                self.value = value
            self.stop.wait(self.interval)


def make_server(monitor, port):
    class MonitorServer(ThreadingHTTPServer):
        # Windows SO_REUSEADDR permits two listeners on one port. Use exclusive
        # ownership there; Unix retains its normal quick-restart behaviour.
        allow_reuse_address = os.name != "nt"

        def server_bind(self):
            if os.name == "nt":
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            hosts = {"127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)}
            if self.headers.get("Host") not in hosts or (self.headers.get("Origin") and
                    self.headers["Origin"] not in {"http://" + h for h in hosts}):
                self.send_error(403)
                return
            if self.path == "/":
                data, mime = (HERE / "index.html").read_bytes(), "text/html; charset=utf-8"
            elif self.path == "/api/status":
                with monitor.lock:
                    data = json.dumps(monitor.value, ensure_ascii=False, allow_nan=False).encode()
                mime = "application/json; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

    return MonitorServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE.parent / ".relay/gateway.json")
    parser.add_argument("--client-config", type=Path, default=HERE.parent / ".relay/client-owner.json")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--gateway-port", type=int, default=8765)
    parser.add_argument("--interval", type=int, default=10)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not 1 <= args.gateway_port <= 65535 or args.interval < 5:
        parser.error("Use an unprivileged monitor port and interval >= 5 seconds")
    monitor = Monitor(args.config, args.client_config, args.gateway_port, args.interval)
    try:
        server = make_server(monitor, args.port)
    except OSError:
        parser.exit(1, "Monitor port unavailable; check the existing monitor. No new instance started.\n")
    thread = threading.Thread(target=monitor.run, daemon=True)
    thread.start()
    print("Monitor: http://127.0.0.1:" + str(args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop.set()
        server.server_close()
        thread.join(timeout=10)


if __name__ == "__main__":
    main()
