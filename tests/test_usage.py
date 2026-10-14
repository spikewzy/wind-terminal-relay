"""Offline accounting, role separation and non-destructive device provisioning."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import httpx

from relay_setup import add_client, initialize, issue_admin, revoke_client
from wind_relay.admin_lock import configuration_lock
from wind_relay.common import RelayError, digest, read_json, write_private
from wind_relay.executor import ToolExecutor
from wind_relay.gateway import create_app
from wind_relay.http import Connection
from wind_relay.store import Queue
from wind_relay.usage import JobMeter, MeteredBudget, UsageLedger, new_context


PAYLOAD = {"tool": "wind_status", "arguments": {"connect": False}}
A, B, WORKER, ADMIN = (letter * 40 for letter in "abwx")


def measured_result(mode="native", ok=True, calls=1, cells=5, cache=0):
    return {"ok": ok, "values": [99999], "token": "never-expose-result-value",
            "relay_execution": {"mode": mode, "usage": {
                "schema_version": 1, "native_preflight_reservations": calls,
                "known_estimated_cells": 6 * calls,
                "unestimated_native_reservations": 0, "cache_hits": cache,
                "observed_data_cells": cells, "data_cells_complete": True}}}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1700000000.0
        self.queue = Queue(Path(self.temp.name) / "queue.sqlite", clock=lambda: self.now, lease_seconds=5)

    def snapshot(self, owner=None, days=1):
        with self.queue.connection() as con:
            self.queue.maintain(con)
            return UsageLedger.snapshot(con, self.now, days, owner)

    def completed(self, job="1" * 32, owner="a", mode="native", result=None):
        self.queue.submit(owner, job, PAYLOAD)
        self.now += 2
        claim = self.queue.claim(mode)
        self.now += 3
        self.queue.complete(job, claim["lease_id"], result or measured_result(mode))
        return claim

    def test_completed_is_accounted_once_on_duplicate_delivery_and_reopen(self):
        claim = self.completed()
        self.queue.complete(claim["job_id"], claim["lease_id"], measured_result())
        self.queue.submit("a", claim["job_id"], PAYLOAD)
        self.queue = Queue(self.queue.path, clock=lambda: self.now)
        value = self.snapshot("a")
        self.assertEqual(value["jobs_in_window"]["accepted"], 1)
        self.assertEqual(value["jobs_in_window"]["succeeded"], 1)
        self.assertEqual(value["by_execution_mode"]["native"]["native_preflight_reservations_observed"], 1)

    def test_wait_and_execution_times_ignore_heartbeat_updates(self):
        self.queue.submit("a", "1" * 32, PAYLOAD)
        self.now += 4
        claim = self.queue.claim("native")
        self.now += 2
        self.queue.heartbeat(claim["job_id"], claim["lease_id"])
        self.now += 3
        self.queue.complete(claim["job_id"], claim["lease_id"], measured_result())
        value = self.snapshot("a")
        self.assertEqual(value["queue_wait_seconds"]["mean"], 4)
        self.assertEqual(value["claim_to_result_seconds"]["mean"], 5)

    def test_pending_usage_is_unknown_not_zero(self):
        self.queue.submit("a", "1" * 32, PAYLOAD)
        self.queue.claim("native")
        value = self.snapshot("a")["by_execution_mode"]["native"]
        self.assertIsNone(value["native_preflight_reservations_observed"])
        self.assertEqual(value["usage_unknown_jobs"], 1)
        self.assertFalse(value["telemetry_complete"])

    def test_partial_observed_sum_and_unknown_jobs_are_both_reported(self):
        self.completed()
        self.queue.submit("a", "2" * 32, PAYLOAD)
        self.queue.claim("native")
        value = self.snapshot("a")["by_execution_mode"]["native"]
        self.assertEqual(value["native_preflight_reservations_observed"], 1)
        self.assertEqual(value["usage_unknown_jobs"], 1)
        self.assertFalse(value["counter_complete"]["native_preflight_reservations"])

    def test_old_completion_cannot_import_historical_budget_or_receipt_usage(self):
        result = {"ok": True, "receipt": {"local_request_budget": {"usage": {"reserved_native_calls": 888}}},
                  "relay_execution": {"mode": "native", "client_profile_isolated": True}}
        self.completed(result=result)
        value = self.snapshot("a")["by_execution_mode"]["native"]
        self.assertIsNone(value["native_preflight_reservations_observed"])
        self.assertEqual(value["usage_unknown_jobs"], 1)

    def test_completed_failure_is_failed_not_successful_transport_completion(self):
        self.completed(result=measured_result(ok=False))
        value = self.snapshot("a")["jobs_in_window"]
        self.assertEqual(value["failed"], 1)
        self.assertEqual(value["succeeded"], 0)

    def test_uncertain_manual_resolution_preserves_unknown_usage(self):
        self.queue.submit("a", "1" * 32, PAYLOAD)
        self.queue.claim("native")
        self.now += 6
        self.assertEqual(self.snapshot("a")["current_pending"]["uncertain"], 1)
        self.queue.resolve_uncertain("1" * 32)
        value = self.snapshot("a")
        self.assertEqual(value["jobs_in_window"]["failed"], 1)
        self.assertIsNone(value["by_execution_mode"]["native"]["native_preflight_reservations_observed"])

    def test_mock_and_native_usage_are_separate(self):
        self.completed(mode="mock", result=measured_result("mock", calls=9))
        self.completed(job="2" * 32, mode="native")
        value = self.snapshot("a")["by_execution_mode"]
        self.assertEqual(value["mock"]["native_preflight_reservations_observed"], 9)
        self.assertEqual(value["native"]["native_preflight_reservations_observed"], 1)

    def test_result_retention_purge_keeps_usage_for_longer_window(self):
        self.completed()
        self.now += 86401
        with self.assertRaises(RelayError):
            self.queue.get("a", "1" * 32)
        self.assertEqual(self.snapshot("a", 2)["jobs_in_window"]["succeeded"], 1)
        self.assertEqual(self.snapshot("a", 1)["jobs_in_window"]["accepted"], 0)

    def test_unknown_cells_cannot_claim_complete_telemetry(self):
        result = measured_result()
        result["relay_execution"]["usage"].update(observed_data_cells=None, data_cells_complete=False)
        self.completed(result=result)
        value = self.snapshot("a")["by_execution_mode"]["native"]
        self.assertFalse(value["telemetry_complete"])
        self.assertTrue(value["counter_complete"]["native_preflight_reservations"])
        self.assertFalse(value["counter_complete"]["observed_data_cells"])

    def test_aggregates_never_contain_result_values_parameters_job_ids(self):
        self.completed()
        text = json.dumps(self.snapshot())
        for secret in ("never-expose-result-value", "99999", "1" * 32, '"arguments"', '"values"'):
            self.assertNotIn(secret, text)

    def test_old_database_backfill_remains_unknown(self):
        with self.queue.connection() as con:
            con.execute("DELETE FROM relay_usage")
            con.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)",
                        ("1" * 32, "a", json.dumps(PAYLOAD), "completed", self.now, self.now,
                         None, None, json.dumps(measured_result())))
        self.queue = Queue(self.queue.path, clock=lambda: self.now)
        value = self.snapshot("a")
        self.assertEqual(value["jobs_in_window"]["result_status_unknown"], 1)
        self.assertEqual(value["by_execution_mode"]["unassigned"]["usage_unknown_jobs"], 1)


class RoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"database": str(Path(self.temp.name) / "queue.sqlite"),
                       "clients": {"device-a": digest(A), "device-b": digest(B)},
                       "worker_sha256": digest(WORKER), "admin_sha256": digest(ADMIN)}
        self.app = create_app(self.config)
        self.sessions = [Connection({"url": "http://127.0.0.1", "token": token},
                                    transport=httpx.ASGITransport(app=self.app))
                         for token in (A, B, WORKER, ADMIN)]
        self.a, self.b, self.worker, self.admin = self.sessions

    async def asyncTearDown(self):
        for connection in self.sessions:
            await connection.close()
        self.temp.cleanup()

    async def test_clients_cannot_read_admin_and_worker_cannot_read_either(self):
        for connection, path in ((self.a, "/v1/admin/usage"), (self.b, "/v1/admin/usage"),
                                 (self.worker, "/v1/admin/usage"), (self.worker, "/v1/usage"),
                                 (self.admin, "/v1/usage"), (self.admin, "/v1/status")):
            with self.assertRaises(RelayError) as caught:
                await connection.request("GET", path)
            self.assertEqual(caught.exception.status, 401)

    async def test_client_can_read_only_own_aggregates_admin_can_compare_devices(self):
        await self.a.request("POST", "/v1/jobs", {"job_id": "1" * 32, **PAYLOAD})
        own = await self.a.request("GET", "/v1/usage")
        other = await self.b.request("GET", "/v1/usage")
        admin = await self.admin.request("GET", "/v1/admin/usage")
        self.assertEqual(own["jobs_in_window"]["accepted"], 1)
        self.assertEqual(other["jobs_in_window"]["accepted"], 0)
        self.assertNotIn("clients", own)
        self.assertNotIn("device-a", json.dumps(other))
        self.assertEqual(admin["clients"][0]["client_id"], "device-a")

    async def test_owner_parameter_cannot_select_another_client(self):
        with self.assertRaises(RelayError) as caught:
            await self.a.request("GET", "/v1/usage?owner=device-b")
        self.assertEqual(caught.exception.code, "INVALID_WINDOW")

    async def test_window_bounded_and_rolls_in_utc(self):
        for invalid in ("0", "32", "-1", "1.5", "true"):
            with self.assertRaises(RelayError):
                await self.a.request("GET", "/v1/usage?days=" + invalid)
        value = await self.a.request("GET", "/v1/usage?days=31")
        self.assertEqual(value["window"]["timezone"], "UTC")
        self.assertIsNone(value["wind_account_remaining"])
        self.assertFalse(value["account_quota"])
        default = await self.a.request("GET", "/v1/usage")
        self.assertEqual(default["window"]["days"], 7)
        self.assertEqual(default["reservation_observation_window"]["days"], 7)

    async def test_admin_cannot_read_another_clients_job_result(self):
        await self.a.request("POST", "/v1/jobs", {"job_id": "1" * 32, **PAYLOAD})
        with self.assertRaises(RelayError) as caught:
            await self.admin.request("GET", "/v1/jobs/" + "1" * 32)
        self.assertEqual(caught.exception.status, 401)

    async def test_version_and_policy_are_distinct_from_native_core_version(self):
        result = await self.a.request("GET", "/v1/status")
        self.assertEqual(result["relay_version"], "0.1.0")
        self.assertEqual(result["mcp_version"], "0.24.2")
        self.assertEqual(result["queue_policy"]["client_pending_limit"], 5)

    async def test_reused_role_credentials_are_rejected(self):
        config = {**self.config, "admin_sha256": digest(WORKER)}
        with self.assertRaises(ValueError):
            create_app(config)


class MeterTests(unittest.TestCase):
    def budget(self):
        class FakeSharedBudget:
            def __init__(self):
                self.calls = 0
                self.finished = []
            def reserve(self, method, arguments):
                self.calls += 1
                if arguments.get("reject"):
                    raise ValueError("shared budget exceeded")
                return {"reservation_id": str(self.calls), "estimate": {"estimated_cells": arguments.get("cells")}}
            def finish(self, reservation, receipt_id=None, state="returned"):
                self.finished.append((reservation["reservation_id"], receipt_id, state))
        return FakeSharedBudget()

    def test_local_or_other_actor_shared_budget_activity_not_attributed(self):
        shared = self.budget()
        context = new_context()
        wrapped = MeteredBudget(shared, context)
        shared.reserve("edb", {"cells": 500})  # Ordinary local MCP on same profile.
        context.meter = JobMeter()
        reservation = wrapped.reserve("edb", {"cells": 6})
        wrapped.finish(reservation, "receipt", "failed_without_receipt")
        stats = context.meter.snapshot()
        self.assertEqual(stats["native_preflight_reservations"], 1)
        self.assertEqual(stats["known_estimated_cells"], 6)
        self.assertEqual(shared.finished[0][2], "failed_without_receipt")

    def test_rejected_reservation_never_counted(self):
        context = new_context()
        context.meter = JobMeter()
        with self.assertRaises(ValueError):
            MeteredBudget(self.budget(), context).reserve("edb", {"reject": True})
        self.assertEqual(context.meter.snapshot()["native_preflight_reservations"], 0)

    def test_finish_error_is_not_swallowed(self):
        shared = self.budget()
        context = new_context()
        wrapped = MeteredBudget(shared, context)
        def fail(*args):
            raise RuntimeError("failed persistent finish")
        shared.finish = fail
        with self.assertRaises(RuntimeError):
            wrapped.finish({"reservation_id": "1"})

    def test_null_zero_empty_cells_are_counted_without_exposing_values(self):
        meter = JobMeter()
        meter.returned({"from_cache": True, "response": {"raw": {"ErrorCode": 0, "Data": [[None, 0, ""]]}}})
        value = meter.snapshot()
        self.assertEqual(value["observed_data_cells"], 3)
        self.assertEqual(value["cache_hits"], 1)
        self.assertEqual(value["native_preflight_reservations"], 0)
        self.assertNotIn("Data", json.dumps(value))

    def test_unknown_output_grid_stays_unknown(self):
        meter = JobMeter()
        meter.returned({"response": {"raw": {"ErrorCode": 0, "Data": "opaque"}}})
        self.assertIsNone(meter.snapshot()["observed_data_cells"])

    def test_executor_twenty_fake_jobs_do_not_import_core_or_leak_history(self):
        shared = self.budget()
        context = new_context()
        wrapped = MeteredBudget(shared, context)
        executor = ToolExecutor.__new__(ToolExecutor)
        executor.mode = "native"
        executor.names = {"synthetic_query", "synthetic_receipt_read"}
        executor.meter_context = context
        executor.service = lambda owner: SimpleNamespace(owner=owner)

        def query(cached=False):
            if not cached:
                wrapped.reserve("edb", {"cells": 3})
            context.meter.returned({"from_cache": cached,
                                    "response": {"raw": {"ErrorCode": 0, "Data": [[None, 0, ""]]}}})
            return {"ok": True, "data_source_id": "wind_terminal_api"}
        def old_read():
            return {"ok": True, "receipt": {"local_request_budget": {"reserved_native_calls": 999}}}
        executor.module = SimpleNamespace(synthetic_query=query, synthetic_receipt_read=old_read)
        for index in range(20):
            executor.mode = "native" if index % 2 else "mock"
            result = executor({"client_id": "device-a", "tool": "synthetic_query", "arguments": {"cached": index % 3 == 0}})
            usage = result["relay_execution"]["usage"]
            self.assertEqual(usage["native_preflight_reservations"], int(index % 3 != 0))
            self.assertEqual(usage["cache_hits"], int(index % 3 == 0))
            self.assertEqual(usage["observed_data_cells"], 3)
            self.assertEqual(result["relay_execution"]["access_channel"], "wind_terminal_relay")
            receipt = executor({"client_id": "device-a", "tool": "synthetic_receipt_read", "arguments": {}})
            self.assertEqual(receipt["relay_execution"]["usage"]["native_preflight_reservations"], 0)
            self.assertEqual(receipt["relay_execution"]["usage"]["cache_hits"], 0)
            self.assertFalse(hasattr(context, "meter"))


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "private"
        initialize(self.directory, "https://relay.example", ["a", "b"])
        self.gateway = self.directory / "gateway.json"

    def test_admin_is_distinct_and_private(self):
        config = read_json(self.gateway)
        admin = read_json(self.directory / "admin.json")
        self.assertEqual(config["admin_sha256"], digest(admin["token"]))
        self.assertNotIn(admin["token"], self.gateway.read_text())
        self.assertNotIn(config["admin_sha256"], list(config["clients"].values()) + [config["worker_sha256"]])
        if os.name != "nt":
            self.assertEqual((self.directory / "admin.json").stat().st_mode & 0o777, 0o600)

    def test_add_and_revoke_one_device_preserves_all_other_credentials(self):
        before = read_json(self.gateway)
        owner_file = (self.directory / "client-a.json").read_bytes()
        result = add_client(self.gateway, "c", "https://relay.example", self.directory / "client-c.json")
        after = read_json(self.gateway)
        self.assertTrue(result["gateway_restart_required"])
        self.assertEqual(before["clients"], {key: value for key, value in after["clients"].items() if key != "c"})
        self.assertEqual(before["worker_sha256"], after["worker_sha256"])
        self.assertEqual(before["admin_sha256"], after["admin_sha256"])
        self.assertEqual(owner_file, (self.directory / "client-a.json").read_bytes())
        revoke_client(self.gateway, "c")
        revoked = read_json(self.gateway)
        self.assertEqual({key: value for key, value in revoked.items() if key != "retired_clients"}, before)
        self.assertEqual(revoked["retired_clients"], ["c"])
        self.assertNotIn(read_json(self.directory / "client-c.json")["token"], json.dumps(result))

    def test_duplicate_device_or_output_file_never_overwrites_credentials(self):
        before = self.gateway.read_bytes()
        with self.assertRaises(ValueError):
            add_client(self.gateway, "a", "https://relay.example", self.directory / "client-new.json")
        with self.assertRaises(ValueError):
            add_client(self.gateway, "c", "https://relay.example", self.directory / "client-a.json")
        self.assertEqual(self.gateway.read_bytes(), before)

    def test_revoked_id_cannot_be_reassigned_to_another_person(self):
        revoke_client(self.gateway, "a")
        before = self.gateway.read_bytes()
        with self.assertRaises(ValueError):
            add_client(self.gateway, "a", "https://relay.example", self.directory / "new-person.json")
        self.assertEqual(self.gateway.read_bytes(), before)
        self.assertFalse((self.directory / "new-person.json").exists())

    def test_old_gateway_add_admin_preserves_client_worker_budget_policy(self):
        before = read_json(self.gateway)
        del before["admin_sha256"]
        before["queue_policy"] = {"client_pending_limit": 7}
        write_private(self.gateway, before)
        output = self.directory / "new-admin.json"
        result = issue_admin(self.gateway, "https://relay.example", output)
        after = read_json(self.gateway)
        self.assertEqual({key: value for key, value in after.items() if key != "admin_sha256"}, before)
        self.assertTrue(result["gateway_restart_required"])
        self.assertNotIn(read_json(output)["token"], json.dumps(result))

    def test_admin_configuration_changes_are_exclusively_locked(self):
        before = self.gateway.read_bytes()
        with configuration_lock(self.gateway):
            with self.assertRaises(ValueError):
                add_client(self.gateway, "c", "https://relay.example", self.directory / "client-c.json")
        self.assertEqual(self.gateway.read_bytes(), before)
        self.assertFalse((self.directory / "client-c.json").exists())


if __name__ == "__main__":
    unittest.main()
