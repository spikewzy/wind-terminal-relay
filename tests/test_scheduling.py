"""Shared-account scheduling tests; no Wind SDK or network is used."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest

from wind_relay.common import RelayError
from wind_relay.store import DEFAULT_QUEUE_POLICY, Queue
from wind_relay.usage import UsageLedger


PAYLOAD = {"tool": "wind_status", "arguments": {"connect": False}}


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "queue.sqlite"
        self.now = 1000
        self.queue = Queue(self.path, clock=lambda: self.now, lease_seconds=3)

    def submit(self, owner, number, queue=None):
        return (queue or self.queue).submit(owner, f"{number:032x}", PAYLOAD)

    def finish_next(self, queue=None):
        queue = queue or self.queue
        job = queue.claim("mock")
        self.assertIsNotNone(job)
        queue.complete(job["job_id"], job["lease_id"], {"ok": True})
        return job["client_id"], job["job_id"]

    def test_backlogged_clients_take_turns_and_keep_their_fifo(self):
        for owner, numbers in [("a", (1, 2, 3)), ("b", (4, 5, 6)), ("c", (7, 8, 9))]:
            for number in numbers:
                self.submit(owner, number)
        observed = [self.finish_next() for _ in range(9)]
        self.assertEqual([owner for owner, _ in observed], list("abcabcabc"))
        self.assertEqual([int(job_id, 16) for _, job_id in observed], [1, 4, 7, 2, 5, 8, 3, 6, 9])

    def test_new_client_is_not_stuck_behind_an_existing_backlog(self):
        for number in range(1, 6):
            self.submit("a", number)
        self.assertEqual(self.finish_next()[0], "a")
        self.submit("b", 6)
        self.assertEqual(self.finish_next()[0], "b")

    def test_fair_turn_survives_a_queue_restart_with_identical_clock(self):
        for owner, number in [("a", 1), ("a", 2), ("b", 3), ("b", 4)]:
            self.submit(owner, number)
        self.assertEqual(self.finish_next()[0], "a")
        reopened = Queue(self.path, clock=lambda: self.now, lease_seconds=3)
        self.assertEqual(self.finish_next(reopened)[0], "b")
        self.assertEqual(self.finish_next(reopened)[0], "a")

    def test_multiple_gateway_calls_cannot_claim_parallel_native_jobs(self):
        for number in range(1, 9):
            self.submit(str(number), number)
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(lambda _: self.queue.claim("native"), range(8)))
        self.assertEqual(sum(job is not None for job in jobs), 1)

    def test_uncertain_started_work_still_blocks_every_client(self):
        self.submit("a", 1)
        first = self.queue.claim("native")
        self.submit("b", 2)
        self.now += 4
        self.assertIsNone(self.queue.claim("native"))
        self.assertEqual(self.queue.get("a", first["job_id"])["state"], "uncertain")
        self.queue.complete(first["job_id"], first["lease_id"], {"ok": True})
        self.assertEqual(self.finish_next()[0], "b")

    def test_default_and_configured_pending_limits_are_distinct(self):
        for number in range(20):
            self.submit("legacy", number)
        with self.assertRaises(RelayError) as caught:
            self.submit("legacy", 20)
        self.assertEqual(caught.exception.code, "QUEUE_FULL")
        shared = Queue(Path(self.temp.name) / "shared.sqlite", clock=lambda: self.now,
                       policy=DEFAULT_QUEUE_POLICY)
        for number in range(5):
            self.submit("shared", number, shared)
        with self.assertRaises(RelayError) as caught:
            self.submit("shared", 5, shared)
        self.assertEqual(caught.exception.code, "QUEUE_FULL")

    def test_global_queued_limit_applies_across_clients(self):
        queue = Queue(Path(self.temp.name) / "global.sqlite", policy={"global_queued_limit": 2})
        self.submit("a", 1, queue)
        self.submit("b", 2, queue)
        with self.assertRaises(RelayError) as caught:
            self.submit("c", 3, queue)
        self.assertEqual(caught.exception.code, "QUEUE_FULL")

    def test_new_job_rate_does_not_charge_same_id_or_status_polling(self):
        queue = Queue(Path(self.temp.name) / "rate.sqlite", clock=lambda: self.now,
                      policy={"new_jobs_per_minute": 1})
        first = self.submit("a", 1, queue)
        self.assertEqual(self.submit("a", 1, queue), first)
        queue.get("a", first["job_id"])
        queue.status("a")
        with self.assertRaises(RelayError) as caught:
            self.submit("a", 2, queue)
        self.assertEqual(caught.exception.code, "RATE_LIMITED")
        self.assertEqual(caught.exception.status, 429)
        self.submit("b", 3, queue)  # Separate authenticated clients have separate rate windows.
        self.now += 60
        self.submit("a", 2, queue)

    def test_cancellation_does_not_refund_new_job_rate(self):
        queue = Queue(Path(self.temp.name) / "cancel.sqlite", clock=lambda: self.now,
                      policy={"new_jobs_per_minute": 1})
        first = self.submit("a", 1, queue)
        queue.cancel("a", first["job_id"])
        with self.assertRaises(RelayError) as caught:
            self.submit("a", 2, queue)
        self.assertEqual(caught.exception.code, "RATE_LIMITED")

    def test_rate_log_survives_result_cleanup_and_process_restart(self):
        path = Path(self.temp.name) / "persist.sqlite"
        policy = {"new_jobs_per_minute": 1}
        queue = Queue(path, clock=lambda: self.now, retention_seconds=1, policy=policy)
        self.submit("a", 1, queue)
        self.finish_next(queue)
        self.now += 2
        reopened = Queue(path, clock=lambda: self.now, retention_seconds=1, policy=policy)
        with self.assertRaises(RelayError) as caught:
            self.submit("a", 2, reopened)
        self.assertEqual(caught.exception.code, "RATE_LIMITED")
        with self.assertRaises(RelayError) as caught:
            self.submit("a", 1, reopened)
        self.assertEqual(caught.exception.code, "JOB_EXPIRED")

    def test_global_rate_is_optional_and_can_limit_many_client_ids(self):
        queue = Queue(Path(self.temp.name) / "global-rate.sqlite", clock=lambda: self.now,
                      policy={"new_jobs_per_minute": None, "global_new_jobs_per_minute": 2})
        self.submit("a", 1, queue)
        self.submit("b", 2, queue)
        with self.assertRaises(RelayError) as caught:
            self.submit("c", 3, queue)
        self.assertEqual(caught.exception.code, "RATE_LIMITED")

    def test_rate_reservations_are_atomic_for_simultaneous_submissions(self):
        queue = Queue(Path(self.temp.name) / "atomic-rate.sqlite", clock=lambda: self.now,
                      policy={"new_jobs_per_minute": 3})

        def attempt(number):
            try:
                self.submit("a", number, queue)
                return "accepted"
            except RelayError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=8) as pool:
            states = list(pool.map(attempt, range(8)))
        self.assertEqual(states.count("accepted"), 3)
        self.assertEqual(states.count("RATE_LIMITED"), 5)
        with queue.connection() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM admission_log").fetchone()[0], 3)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM dedupe").fetchone()[0], 3)

    def test_zero_limit_blocks_new_jobs_and_null_explicitly_disables_it(self):
        path = Path(self.temp.name) / "zero.sqlite"
        queue = Queue(path, policy={"new_jobs_per_minute": 0})
        with self.assertRaises(RelayError) as caught:
            self.submit("a", 1, queue)
        self.assertEqual(caught.exception.code, "RATE_LIMITED")
        reopened = Queue(path, policy={"new_jobs_per_minute": None})
        self.submit("a", 1, reopened)

    def test_invalid_policy_is_rejected_before_initializing_a_database(self):
        for policy in [{"unknown": 1}, {"new_jobs_per_minute": True}, {"client_pending_limit": -1},
                       {"global_queued_limit": "100"}, []]:
            path = Path(self.temp.name) / "invalid.sqlite"
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                Queue(path, policy=policy)
            self.assertFalse(path.exists())

    def test_existing_schema_migrates_without_losing_pending_jobs(self):
        from contextlib import closing
        path = Path(self.temp.name) / "old.sqlite"
        with closing(sqlite3.connect(path)) as con:
            con.execute("""CREATE TABLE jobs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                lease TEXT, lease_until REAL, result TEXT)""")
            con.execute("INSERT INTO jobs VALUES(?,?,?,'queued',?,?,NULL,NULL,NULL)",
                        ("1" * 32, "a", '{"tool":"wind_status","arguments":{}}', self.now, self.now))
            con.commit()
        migrated = Queue(path, clock=lambda: self.now)
        self.assertEqual(migrated.get("a", "1" * 32)["state"], "queued")
        self.assertEqual(migrated.claim("mock")["job_id"], "1" * 32)

    def test_same_completion_after_restart_never_counts_usage_twice(self):
        self.submit("a", 1)
        job = self.queue.claim("native")
        result = {"ok": True, "relay_execution": {"mode": "native", "usage": {
            "schema_version": 1, "native_preflight_reservations": 2,
            "known_estimated_cells": 10, "unestimated_native_reservations": 0,
            "cache_hits": 0, "observed_data_cells": 8, "data_cells_complete": True}}}
        self.queue.complete(job["job_id"], job["lease_id"], result)
        self.now += 1
        reopened = Queue(self.path, clock=lambda: self.now)
        reopened.complete(job["job_id"], job["lease_id"], result)
        with reopened.connection() as con:
            usage = UsageLedger.snapshot(con, self.now, owner="a")
        self.assertEqual(usage["jobs_in_window"]["accepted"], 1)
        self.assertEqual(usage["jobs_in_window"]["succeeded"], 1)
        self.assertEqual(usage["by_execution_mode"]["native"]["native_preflight_reservations_observed"], 2)
        self.assertEqual(usage["claim_to_result_seconds"]["max"], 0)

    def test_interrupted_job_usage_stays_unknown_and_pending_after_restart(self):
        self.submit("a", 1)
        self.queue.claim("native")
        self.now += 4
        reopened = Queue(self.path, clock=lambda: self.now)
        self.assertIsNone(reopened.claim("native"))
        with reopened.connection() as con:
            usage = UsageLedger.snapshot(con, self.now, owner="a")
        native = usage["by_execution_mode"]["native"]
        self.assertEqual(usage["current_pending"]["uncertain"], 1)
        self.assertEqual(native["usage_unknown_jobs"], 1)
        self.assertIsNone(native["native_preflight_reservations_observed"])
        self.assertIsNone(native["observed_data_cells_observed"])

    def test_independent_gateway_instances_share_atomic_admission_window(self):
        path = Path(self.temp.name) / "multi-instance.sqlite"
        policy = {"new_jobs_per_minute": 3}
        queues = [Queue(path, clock=lambda: self.now, policy=policy) for _ in range(8)]

        def attempt(number):
            try:
                self.submit("a", number, queues[number])
                return "accepted"
            except RelayError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=8) as pool:
            states = list(pool.map(attempt, range(8)))
        self.assertEqual(states.count("accepted"), 3)
        self.assertEqual(states.count("RATE_LIMITED"), 5)


if __name__ == "__main__":
    unittest.main()
