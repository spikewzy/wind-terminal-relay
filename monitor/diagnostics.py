"""Bounded, allowlisted diagnostics. Never copy raw logs, configs or exceptions."""
from collections import deque
import datetime as dt
import hashlib
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import math
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
import traceback
import zipfile

VERSION = "0.2.1"
LABEL = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
SEMVER = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\Z")
FRAME_FILES = {"monitor.py", "diagnostics.py", "management.py", "request.py", "error.py", "threading.py",
               "ssl.py", "socket.py", "client.py", "pathlib.py", "__init__.py"}
JOB_KEYS = {"job_id", "client_id", "tool", "state", "display_state", "created_at", "updated_at",
            "age_seconds", "result_ok", "mode", "cached", "error_code", "receipt_id", "native_data_return"}
SCALAR_FIELDS = {"collected_at", "probe_finished_at", "database_ok", "reason", "disk_free_gb", "queue_blocked", "loading", "demonstration"}
GROUPS = {
    "diagnostics": {"logging_available"},
    "worker": {"online", "age_seconds", "mode", "clock_warning"},
    "pending": {"queued", "running", "uncertain", "expired_wait"},
    "retained_24h": {"accepted", "succeeded", "failed", "result_unknown"},
    "usage_7d": {"available", "reservations", "estimated_cells", "quota_failures", "unknown_jobs", "scope",
                 "unestimated_reservations", "observed_jobs"},
    "gateway": {"ok", "reason", "worker_online", "execution_paused", "worker_mode", "relay_version", "mcp_version"},
    "public_access": {"ok", "reason", "worker_online", "execution_paused", "worker_mode", "relay_version", "mcp_version"},
}


def safe_deployment(value):
    if not isinstance(value,dict) or not value:return {}
    clean=fields(value, {'gateway_runtime_verified','worker_version_verified','maintenance_mode',
                        'single_execution_lane'})
    build=value.get('gateway_build_id')
    clean['gateway_build_id']=build if isinstance(build,str) and re.fullmatch(r'[a-f0-9]{64}',build) else None
    clean['scheduler']='client_round_robin' if value.get('scheduler')=='client_round_robin' else None
    for key in ('gateway_version','core_contract_version','worker_version'):
        version=value.get(key)
        clean[key]=version if isinstance(version,str) and SEMVER.fullmatch(version) else None
    policy=value.get('queue_policy',{})
    if isinstance(policy,dict):
        clean['queue_policy']={k:v for k,v in policy.items()
            if k in {'client_pending_limit','global_queued_limit','new_jobs_per_minute','global_new_jobs_per_minute'}
            and (v is None or type(v) is int and 0<=v<=10**9)}
    return clean


def scalar(value):
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, str) and LABEL.fullmatch(value):
        return value
    return None


def fields(value, keys):
    return {key: scalar(value[key]) for key in keys if key in value} if isinstance(value, dict) else {}


def safe_snapshot(value):
    """A second allowlist boundary; future additions cannot silently leak."""
    clean = fields(value, SCALAR_FIELDS)
    for name, keys in GROUPS.items():
        if name in value:
            clean[name] = fields(value[name], keys)
    clean["deployment"] = safe_deployment(value.get("deployment"))
    clean["latest_native_data"] = (fields(value.get("latest_native_data"), JOB_KEYS)
                                    if isinstance(value.get("latest_native_data"), dict) else None)
    for name in ("active_jobs", "recent_jobs"):
        clean[name] = [fields(item, JOB_KEYS) for item in value.get(name, [])[:100] if isinstance(item, dict)]
    clean["devices"] = [fields(item, {"client_id", "accepted", "succeeded", "failed", "pending"})
                        for item in value.get("devices", [])[:100] if isinstance(item, dict)]
    return clean


def error_details(exc):
    """Exception text, source lines, locals, paths and URLs are excluded."""
    cause = getattr(exc, "reason", None)
    result = {"exception_type": scalar(type(exc).__name__),
              "cause_type": scalar(type(cause).__name__) if isinstance(cause, BaseException) else None}
    for name in ("errno", "winerror", "sqlite_errorcode"):
        value = getattr(exc, name, None)
        if type(value) is int:
            result[name] = value
    frames = []
    for frame in traceback.extract_tb(exc.__traceback__)[-8:]:
        filename = Path(frame.filename).name
        if filename in FRAME_FILES:
            frames.append({"file": filename, "function": scalar(frame.name), "line": frame.lineno})
    result["frames"] = frames
    return result


def clean_error(value):
    result = fields(value, {"exception_type", "cause_type", "errno", "winerror", "sqlite_errorcode"})
    result["frames"] = [{"file": frame["file"], "function": scalar(frame.get("function")),
                          "line": scalar(frame.get("line"))}
                         for frame in value.get("frames", [])[:8]
                         if isinstance(frame, dict) and frame.get("file") in FRAME_FILES]
    return result


def clean_event(value):
    event = fields(value, {"timestamp", "event", "stage", "code", "version", "level"})
    version = value.get("version")
    event["version"] = version if isinstance(version, str) and SEMVER.fullmatch(version) else None
    if isinstance(value.get("error"), dict):
        event["error"] = clean_error(value["error"])
    if isinstance(value.get("snapshot"), dict):
        event["snapshot"] = safe_snapshot(value["snapshot"])
    if isinstance(value.get("job"), dict):
        event["job"] = fields(value["job"], JOB_KEYS)
    return event


class Recorder:
    def __init__(self, directory=None, *, max_bytes=1024*1024, backups=3, read_only=False):
        self.directory = Path(directory) if directory is not None else None
        self.started = time.time()
        self.lock = threading.RLock()
        self.events = deque(maxlen=500)
        self.handler = None
        self.read_only = read_only
        self.available = None if read_only else False
        self.last_signature = None
        self.last_status = 0
        self.failed_ids = deque(maxlen=1000)
        if self.directory is not None and not read_only:
            try:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = self.directory / "monitor.log"
                if self.directory.is_symlink() or path.is_symlink():
                    raise OSError("Unsafe log location")
                self.handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups,
                                                   encoding="utf-8", delay=False)
                self.handler.setFormatter(logging.Formatter("%(message)s"))
                path.chmod(0o600)
                self.available = True
            except Exception as exc:
                self.record("logging_unavailable", "logging", "LOGGING_UNAVAILABLE", exc=exc)

    def record(self, event, stage, code, *, exc=None, snapshot=None, job=None):
        value = {"timestamp": time.time(), "event": event, "stage": stage, "code": code, "version": VERSION}
        if exc is not None:
            value["error"] = error_details(exc)
        if snapshot is not None:
            value["snapshot"] = snapshot
        if job is not None:
            value["job"] = job
        value = clean_event(value)
        text = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        with self.lock:
            self.events.append(value)
            if self.handler is not None:
                try:
                    # Call the handler implementation directly so logging's own
                    # stderr error reporter cannot print unsanitized exceptions.
                    if self.handler.shouldRollover(logging.makeLogRecord({"msg": text})):
                        self.handler.doRollover()
                        (self.directory / "monitor.log").chmod(0o600)
                    self.handler.stream.write(text + "\n")
                    self.handler.flush()
                except Exception:
                    self.available = False

    def observe(self, value):
        signature = json.dumps({key: value.get(key) for key in
                               ("database_ok", "reason", "gateway", "public_access", "queue_blocked")}, sort_keys=True)
        worker = value.get("worker", {})
        signature += str((worker.get("online"), worker.get("mode"), worker.get("clock_warning")))
        now = time.time()
        if signature != self.last_signature or now-self.last_status >= 300:
            self.record("status_change" if signature != self.last_signature else "status_checkpoint",
                        "monitor", "STATUS_SNAPSHOT", snapshot=value)
            self.last_signature, self.last_status = signature, now
        for job in value.get("recent_jobs", []):
            if (job.get("state") == "failed" or job.get("result_ok") is False or
                    job.get("display_state") == "uncertain") and job.get("job_id") not in self.failed_ids:
                self.record("task_problem", "relay_task", "TASK_REQUIRES_INSPECTION", job=job)
                self.failed_ids.append(job.get("job_id"))

    def history(self):
        with self.lock:
            records = deque(maxlen=1000)
            if self.directory is not None:
                for suffix in (".3", ".2", ".1", ""):
                    path = self.directory / ("monitor.log"+suffix)
                    try:
                        if path.is_symlink() or path.stat().st_size > 2*1024*1024:
                            continue
                        for line in path.read_text(encoding="utf-8").splitlines():
                            try:
                                value = json.loads(line)
                                if isinstance(value, dict):
                                    records.append(clean_event(value))
                            except (ValueError, TypeError, AttributeError):
                                continue
                    except (OSError, UnicodeError):
                        continue
            # In-memory events also preserve errors when filesystem logging fails.
            seen = {json.dumps(row, sort_keys=True) for row in records}
            for event in self.events:
                if json.dumps(event, sort_keys=True) not in seen:
                    records.append(event)
            return list(records)

    def bundle(self, snapshot, source_dir):
        now = time.time()
        hashes = {}
        for name in ("monitor.py", "diagnostics.py", "management.py", "index.html", "Install.ps1"):
            try:
                hashes[name] = hashlib.sha256((Path(source_dir)/name).read_bytes()).hexdigest()
            except OSError:
                hashes[name] = None
        summary = {"monitor_version": VERSION, "exported_at_utc": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
                   "python_version": ".".join(map(str, sys.version_info[:3])), "platform": sys.platform,
                   "sqlite_version": sqlite3.sqlite_version, "logging_available": self.available,
                   "recorder_started_at": self.started, "snapshot": safe_snapshot(snapshot), "source_sha256": hashes,
                   "scope": "cloud_monitor_only", "raw_gateway_worker_logs_included": False,
                   "synthetic_fixture": snapshot.get("demonstration") is True,
                   "export_mode": "read_only_log_reader" if self.read_only else "running_monitor"}
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
            archive.writestr("events.jsonl", "\n".join(json.dumps(row, ensure_ascii=False) for row in self.history())+"\n")
            archive.writestr("README.txt", "这是云端监控诊断包：包含已过滤的状态、错误类型、代码位置和任务编号。\n"
                              "不包含 token、配置全文、数据库、查询参数、行情正文或未过滤的日志。\n"
                              "请同时说明故障发生的大致时间及当时的操作；执行端原始日志需另行在取数电脑核查。\n")
        return stream.getvalue()

    def close(self):
        if self.handler is not None:
            self.handler.close()


def bundle_name():
    return "wind-relay-diagnostics-"+dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")+"Z.zip"
