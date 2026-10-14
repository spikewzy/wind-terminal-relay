"""Private, durable relay aggregates. No parameters, tokens or market values.

Native reservations are conservative local preflight attempts, not Wind billing
units or a precise count of SDK invocations. Missing old/pending telemetry stays
unknown. Queue lifecycle methods use their caller's existing SQLite transaction.
"""
from __future__ import annotations

import datetime as dt
import json
import threading

from .common import RelayError


COUNTERS = ("native_preflight_reservations", "known_estimated_cells",
            "unestimated_native_reservations", "cache_hits", "observed_data_cells")
TERMINAL = {"completed", "failed", "cancelled", "expired"}


class UsageLedger:
    @staticmethod
    def initialize(con):
        con.execute("""CREATE TABLE IF NOT EXISTS relay_usage (
            job_id TEXT PRIMARY KEY, owner TEXT NOT NULL, tool TEXT,
            accepted_at REAL NOT NULL, started_at REAL, finished_at REAL,
            state TEXT NOT NULL, mode TEXT, result_ok INTEGER,
            telemetry TEXT
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS relay_usage_window ON relay_usage(owner,accepted_at)")
        # Retained pre-upgrade jobs are explicitly unmetered, including successful
        # results. Never infer attempts from a saved receipt or historical budget.
        con.execute("""INSERT OR IGNORE INTO relay_usage
            (job_id,owner,accepted_at,state,finished_at)
            SELECT id,owner,created,state,
                CASE WHEN state IN ('completed','failed','cancelled','expired')
                     THEN updated ELSE NULL END FROM jobs""")

    @staticmethod
    def submitted(con, owner, job_id, tool, stamp):
        con.execute("""INSERT OR IGNORE INTO relay_usage
            (job_id,owner,tool,accepted_at,state) VALUES(?,?,?,?,'queued')""",
                    (job_id, owner, tool, stamp))

    @staticmethod
    def started(con, job_id, mode, stamp):
        con.execute("UPDATE relay_usage SET started_at=COALESCE(started_at,?),state='running',mode=? WHERE job_id=?",
                    (stamp, mode, job_id))

    @staticmethod
    def transition(con, job_id, state, stamp):
        con.execute("UPDATE relay_usage SET state=?,finished_at=? WHERE job_id=?",
                    (state, stamp if state in TERMINAL else None, job_id))

    @staticmethod
    def completed(con, job_id, result, stamp):
        row = con.execute("SELECT mode FROM relay_usage WHERE job_id=?", (job_id,)).fetchone()
        execution = result.get("relay_execution")
        usage = execution.get("usage") if isinstance(execution, dict) else None
        telemetry = None
        if (row and isinstance(usage, dict) and usage.get("schema_version") == 1
                and execution.get("mode") == row[0]):
            clean = {"schema_version": 1}
            for key in COUNTERS:
                value = usage.get(key)
                clean[key] = value if type(value) is int and 0 <= value <= 10**12 else None
            clean["data_cells_complete"] = usage.get("data_cells_complete") is True
            if not clean["data_cells_complete"]:
                clean["observed_data_cells"] = None
            telemetry = json.dumps(clean, sort_keys=True, separators=(",", ":"))
        ok = result.get("ok")
        con.execute("""UPDATE relay_usage SET state='completed',finished_at=?,
            result_ok=?,telemetry=? WHERE job_id=?""",
                    (stamp, int(ok) if type(ok) is bool else None, telemetry, job_id))

    @staticmethod
    def snapshot(con, now, days=1, owner=None):
        if type(days) is not int or not 1 <= days <= 31:
            raise RelayError("INVALID_WINDOW", "days must be an integer in 1..31")
        cutoff = now - days * 86400
        columns = "job_id,owner,accepted_at,started_at,finished_at,state,mode,result_ok,telemetry"
        sql = "SELECT " + columns + " FROM relay_usage WHERE accepted_at>=? AND accepted_at<=?"
        parameters = [cutoff, now]
        if owner is not None:
            sql += " AND owner=?"
            parameters.append(owner)
        rows = [dict(zip(columns.split(","), row)) for row in con.execute(sql, parameters)]
        pending_sql = "SELECT owner,state,COUNT(*) FROM relay_usage WHERE state IN ('queued','running','uncertain')"
        pending_params = []
        if owner is not None:
            pending_sql += " AND owner=?"
            pending_params.append(owner)
        pending_sql += " GROUP BY owner,state"
        pending = {(row[0], row[1]): row[2] for row in con.execute(pending_sql, pending_params)}

        def timing(values):
            return {"samples": len(values), "mean": sum(values) / len(values) if values else None,
                    "max": max(values) if values else None}

        def aggregate(selected, owners):
            states = {name: sum(row["state"] == name for row in selected)
                      for name in ("queued", "running", "uncertain", "cancelled", "expired")}
            states.update(accepted=len(selected),
                          succeeded=sum(row["state"] == "completed" and row["result_ok"] == 1 for row in selected),
                          failed=sum(row["state"] == "failed" or
                                     (row["state"] == "completed" and row["result_ok"] == 0) for row in selected),
                          result_status_unknown=sum(row["state"] == "completed" and row["result_ok"] is None for row in selected))
            modes = {}
            for mode in ("native", "mock", "unassigned"):
                members = [row for row in selected if (row["mode"] or "unassigned") == mode]
                measured = [json.loads(row["telemetry"]) for row in members if row["telemetry"]]
                statistics = {key + "_observed": (sum(item[key] for item in measured if item[key] is not None)
                                                   if any(item[key] is not None for item in measured) else None)
                              for key in COUNTERS}
                modes[mode] = {"jobs": len(members), "metered_jobs": len(measured),
                               "usage_unknown_jobs": len(members) - len(measured), **statistics}
                modes[mode]["counter_complete"] = {
                    key: bool(members) and len(measured) == len(members)
                    and all(item[key] is not None for item in measured) for key in COUNTERS}
                modes[mode]["telemetry_complete"] = bool(members) and all(modes[mode]["counter_complete"].values())
                modes[mode]["exact_native_sdk_calls"] = None
            waits = [max(0, row["started_at"] - row["accepted_at"]) for row in selected if row["started_at"] is not None]
            durations = [max(0, row["finished_at"] - row["started_at"]) for row in selected
                         if row["state"] == "completed" and row["started_at"] is not None and row["finished_at"] is not None]
            return {"jobs_in_window": states,
                    "current_pending": {state: sum(pending.get((client, state), 0) for client in owners)
                                        for state in ("queued", "running", "uncertain")},
                    "by_execution_mode": modes,
                    "queue_wait_seconds": timing(waits),
                    "claim_to_result_seconds": timing(durations)}

        owners = sorted({row["owner"] for row in rows} | {client for client, _ in pending})
        if owner is not None and owner not in owners:
            owners.append(owner)
        result = {"ok": True, "scope": "own_client" if owner is not None else "administrator",
                  "window": {"timezone": "UTC", "days": days,
                             "from": dt.datetime.fromtimestamp(cutoff, dt.timezone.utc).isoformat(),
                             "through": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
                             "cohort": "jobs_accepted_in_window"},
                  "account_quota": False, "wind_account_remaining": None,
                  "http_request_count": None,
                  "notes": ["Preflight reservations include lock failures, login checks and unfinished attempts; they are not exact SDK calls or Wind billing units.",
                            "Estimated cells use local conservative output-grid estimates. Official account balance and non-relay usage are not measured here.",
                            "Old or unfinished jobs have unknown usage. Observed sums may be partial; native and synthetic mock activity are separate.",
                            "Timing is gateway acceptance to claim and claim to result acknowledgement, not pure SDK execution time.",
                            "Independent client IDs identify devices or users. Devices sharing one token cannot be distinguished."]}
        result.update(aggregate(rows, owners))
        if owner is None:
            result["clients"] = [{"client_id": client,
                                  **aggregate([row for row in rows if row["owner"] == client], [client])}
                                 for client in owners]
        return result


class JobMeter:
    """One executor thread's current job; historical result contents are ignored."""
    def __init__(self):
        self.reservations = {}
        self.cache_hits = 0
        self.data_cells = 0
        self.data_cells_complete = True

    def reserved(self, reservation):
        self.reservations[reservation["reservation_id"]] = reservation

    def returned(self, receipt):
        self.cache_hits += int(receipt.get("from_cache") is True)
        raw = receipt.get("response", {}).get("raw", {})
        if raw.get("ErrorCode") != 0:
            return
        columns = raw.get("Data")
        if isinstance(columns, list) and all(isinstance(column, list) for column in columns):
            self.data_cells += sum(len(column) for column in columns)
        else:
            self.data_cells_complete = False

    def snapshot(self):
        estimates = [reservation.get("estimate", {}).get("estimated_cells") for reservation in self.reservations.values()]
        return {"schema_version": 1, "account_quota": False,
                "native_preflight_reservations": len(estimates),
                "known_estimated_cells": sum(value for value in estimates if type(value) is int),
                "unestimated_native_reservations": sum(value is None for value in estimates),
                "cache_hits": self.cache_hits,
                "observed_data_cells": self.data_cells if self.data_cells_complete else None,
                "data_cells_complete": self.data_cells_complete,
                "exact_native_sdk_calls": None}


class MeteredBudget:
    """Preserve shared core enforcement while attributing accepted reservations."""
    def __init__(self, shared_budget, context):
        self.shared_budget, self.context = shared_budget, context

    def reserve(self, method, arguments):
        reservation = self.shared_budget.reserve(method, arguments)
        meter = getattr(self.context, "meter", None)
        if meter is not None:
            meter.reserved(reservation)
        return reservation

    def finish(self, reservation, receipt_id=None, state="returned"):
        return self.shared_budget.finish(reservation, receipt_id, state)

    def __getattr__(self, name):
        return getattr(self.shared_budget, name)


def new_context():
    return threading.local()
