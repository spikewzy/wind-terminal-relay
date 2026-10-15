"""Rolling observation accounting uses event time, never calendar resets."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from wind_relay.store import Queue
from wind_relay.usage import JobMeter, MeteredBudget, UsageLedger, new_context


PAYLOAD = {"tool": "wind_status", "arguments": {"connect": False}}
WEEK = 7 * 86400


def result(events=(), mode="native", ok=True):
    events = list(events)
    return {"ok": ok, "relay_execution": {"mode": mode, "usage": {
        "schema_version": 2, "reservation_events": events,
        "reservation_events_complete": True,
        "native_preflight_reservations": len(events),
        "known_estimated_cells": sum(event["estimated_cells"] for event in events if type(event.get("estimated_cells")) is int),
        "unestimated_native_reservations": sum(event.get("estimated_cells") is None for event in events),
        "cache_hits": 0, "observed_data_cells": 0, "data_cells_complete": True,
    }}}


def event(identifier, when, cells=5):
    return {"reservation_id": identifier, "observed_at": when, "estimated_cells": cells}


class RollingWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1700000000.0
        self.queue = Queue(Path(self.temp.name) / "queue.sqlite", clock=lambda: self.now)
        self.index = 0

    def snapshot(self, owner=None, days=7):
        with self.queue.connection() as con:
            self.queue.maintain(con)
            return UsageLedger.snapshot(con, self.now, days=days, owner=owner)

    def execute(self, value=None, owner="device-a", mode="native"):
        self.index += 1
        job_id = format(self.index, "032x")
        self.queue.submit(owner, job_id, PAYLOAD)
        claim = self.queue.claim(mode)
        self.queue.complete(job_id, claim["lease_id"], value if value is not None else result([event(str(self.index), self.now)], mode))
        return claim

    def mode(self, snapshot, mode="native"):
        return snapshot["reservation_observation_window"]["by_execution_mode"][mode]

    def test_default_seven_days_and_unknown_official_limits(self):
        with self.queue.connection() as con:
            value = UsageLedger.snapshot(con, self.now)
        self.assertEqual(value["window"]["days"], 7)
        self.assertEqual(value["wind_quota_window_seconds"], WEEK)
        self.assertIsNone(value["wind_account_remaining"])
        self.assertIsNone(value["wind_official_limit"])
        self.assertEqual(value["wind_quota_reset"], "rolling_7x24_hours_no_midnight_reset")
        self.assertFalse(value["reservation_observation_window"]["full_wind_account_coverage"])

    def test_rolling_window_before_unix_epoch_formats_on_every_platform(self):
        self.now = 1000
        value = self.snapshot()
        self.assertEqual(value["window"]["from"], "1969-12-25T00:16:40+00:00")
        self.assertEqual(value["window"]["through"], "1970-01-01T00:16:40+00:00")

    def test_exact_seven_day_boundary_expires_only_after_full_duration(self):
        self.execute()
        self.now += WEEK
        self.assertEqual(self.mode(self.snapshot())["preflight_reservations_observed"], 1)
        self.now += 0.01
        self.assertIsNone(self.mode(self.snapshot())["preflight_reservations_observed"])

    def test_midnight_does_not_reset_observed_usage(self):
        self.now = dt.datetime(2026, 10, 13, 23, 59, 59, tzinfo=dt.timezone.utc).timestamp()
        self.execute()
        self.now += 2
        self.assertEqual(self.mode(self.snapshot())["preflight_reservations_observed"], 1)

    def test_job_accepted_before_window_is_attributed_by_execution_event_time(self):
        self.queue.submit("device-a", "1" * 32, PAYLOAD)
        # Preserve this synthetic queued job to model an earlier accepted job;
        # the production queue normally expires unstarted work after 30 minutes.
        self.now += WEEK + 10
        with self.queue.connection() as con:
            con.execute("UPDATE jobs SET state='running',lease='held',lease_until=? WHERE id=?", (self.now + 120, "1" * 32))
            UsageLedger.started(con, "1" * 32, "native", self.now)
        self.queue.complete("1" * 32, "held", result([event("later", self.now)]))
        value = self.snapshot()
        self.assertEqual(value["jobs_in_window"]["accepted"], 0)
        self.assertEqual(self.mode(value)["preflight_reservations_observed"], 1)
        self.assertEqual(value["clients"][0]["client_id"], "device-a")
        self.assertEqual(self.mode(value["clients"][0])["known_estimated_cells_observed"], 5)

    def test_event_outside_window_not_charged_by_recent_completion(self):
        self.execute(result([event("old", self.now - WEEK - 1)]))
        value = self.mode(self.snapshot())
        self.assertEqual(value["preflight_reservations_observed"], 0)
        self.assertTrue(value["complete_for_returned_jobs"])

    def test_completion_redelivery_and_reopening_do_not_duplicate_events(self):
        value = result([event("r", self.now)])
        claim = self.execute(value)
        self.queue.complete(claim["job_id"], claim["lease_id"], value)
        self.queue = Queue(self.queue.path, clock=lambda: self.now)
        self.assertEqual(self.mode(self.snapshot())["preflight_reservations_observed"], 1)
        with self.queue.connection() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM relay_reservation_events").fetchone()[0], 1)

    def test_empty_legacy_and_pending_stay_unknown_not_zero(self):
        self.assertIsNone(self.mode(self.snapshot())["preflight_reservations_observed"])
        self.execute({"ok": True, "relay_execution": {"mode": "native"}})
        self.assertIsNone(self.mode(self.snapshot())["preflight_reservations_observed"])
        self.queue.submit("device-a", "2" * 32, PAYLOAD)
        self.queue.claim("native")
        observed = self.mode(self.snapshot())
        self.assertEqual(observed["delivery_delayed_jobs"], 1)
        self.assertEqual(observed["coverage"], "delayed")
        self.assertFalse(observed["complete_for_returned_jobs"])

    def test_metadata_no_reservation_is_known_zero_with_schema_two(self):
        self.execute(result())
        self.assertEqual(self.mode(self.snapshot())["preflight_reservations_observed"], 0)
        self.assertTrue(self.mode(self.snapshot())["complete_for_returned_jobs"])

    def test_native_mock_and_devices_remain_isolated(self):
        self.execute(owner="device-a")
        self.execute(result([event("mock", self.now, 100)] , "mock"), owner="device-b", mode="mock")
        value = self.snapshot()
        self.assertEqual(self.mode(value)["known_estimated_cells_observed"], 5)
        self.assertEqual(self.mode(value, "mock")["known_estimated_cells_observed"], 100)
        own = self.snapshot("device-a")
        self.assertNotIn("device-b", json.dumps(own))
        self.assertIsNone(self.mode(own, "mock")["preflight_reservations_observed"])

    def test_invalid_event_timestamps_are_ignored_with_partial_coverage(self):
        for bad in ("2026-01-01", True, -1, self.now + 1, float("inf"), 10**400, None):
            # Avoid the queue's JSON finite-value rejection only for inf; call
            # the ledger directly to verify its independent validation too.
            self.index += 1
            job_id = format(self.index, "032x")
            self.queue.submit("device-a", job_id, PAYLOAD)
            self.queue.claim("native")
            with self.queue.connection() as con:
                UsageLedger.completed(con, job_id, result([event("invalid", bad)]), self.now)
                con.execute("UPDATE jobs SET state='completed' WHERE id=?", (job_id,))
        observed = self.mode(self.snapshot())
        self.assertIsNone(observed["preflight_reservations_observed"])
        self.assertFalse(observed["complete_for_returned_jobs"])
        self.assertEqual(observed["coverage"], "partial")

    def test_duplicate_or_inconsistent_events_cannot_claim_complete_coverage(self):
        value = result([event("same", self.now), event("same", self.now)])
        self.execute(value)
        observed = self.mode(self.snapshot())
        self.assertEqual(observed["preflight_reservations_observed"], 1)
        self.assertFalse(observed["complete_for_returned_jobs"])
        second = result([event("new", self.now)])
        second["relay_execution"]["usage"]["known_estimated_cells"] = 999
        self.execute(second)
        self.assertEqual(self.mode(self.snapshot())["known_estimated_cells_observed"], 10)
        self.assertFalse(self.mode(self.snapshot())["complete_for_returned_jobs"])

    def test_missing_event_field_never_breaks_result_delivery_or_claims_complete(self):
        value = result([event("incomplete", self.now)])
        del value["relay_execution"]["usage"]["reservation_events"][0]["estimated_cells"]
        self.execute(value)
        observed = self.mode(self.snapshot())
        self.assertIsNone(observed["preflight_reservations_observed"])
        self.assertEqual(observed["coverage"], "partial")
        self.assertFalse(observed["complete_for_returned_jobs"])

    def test_successful_historical_quota_evidence_is_not_a_current_failure(self):
        value = result()
        value["old_receipt"] = {"error_category": "quota_exceeded"}
        self.execute(value)
        observed = self.snapshot()["quota_observations"]
        self.assertEqual(observed["native_failure_count_observed"], 0)
        self.assertIsNone(observed["last_native_failure_at"])
        self.assertIsNone(observed["quota_recovery_at"])

    def test_nested_actual_quota_failure_is_recorded_once_without_reset_guess(self):
        value = result(ok=False)
        value["error"] = {"details": [{"error_category": "quota_exceeded"}]}
        claim = self.execute(value)
        self.queue.complete(claim["job_id"], claim["lease_id"], value)
        observed = self.snapshot()["quota_observations"]
        self.assertEqual(observed["native_failure_count_observed"], 1)
        self.assertIsNotNone(observed["last_native_failure_at"])
        self.assertIsNone(observed["quota_recovery_at"])
        self.assertFalse(observed["automatic_reset_or_retry"])

    def test_legacy_completed_without_new_failure_observation_is_explicitly_unknown(self):
        self.execute({"ok": False})
        with self.queue.connection() as con:
            con.execute("DELETE FROM relay_quota_observations")
        value = self.snapshot()["quota_observations"]
        self.assertEqual(value["native_failure_count_observed"], 0)
        self.assertEqual(value["results_without_quota_observation"], 1)

    def test_aggregate_never_exposes_reservation_ids(self):
        self.execute(result([event("private-reservation-id", self.now)]))
        self.assertNotIn("private-reservation-id", json.dumps(self.snapshot()))


class ObservationClockTests(unittest.TestCase):
    def test_shared_budget_clock_used_and_repeat_reservation_deduplicated(self):
        class Budget:
            clock = staticmethod(lambda: 1700000000.25)
            def reserve(self, method, arguments):
                return {"reservation_id": "stable", "estimate": {"estimated_cells": None}}
        context = new_context()
        context.meter = JobMeter()
        wrapped = MeteredBudget(Budget(), context)
        wrapped.reserve("edb", {})
        wrapped.reserve("edb", {})
        value = context.meter.snapshot()
        self.assertEqual(value["native_preflight_reservations"], 1)
        self.assertEqual(value["reservation_events"], [event("stable", 1700000000.25, None)])


if __name__ == "__main__":
    unittest.main()
