"""Admission rejection is distinct from an ambiguous delivery acknowledgement."""
from pathlib import Path
import tempfile
import unittest

import httpx

from wind_relay.common import RelayError, digest
from wind_relay.gateway import create_app
from wind_relay.http import Connection
from wind_relay.proxy import Proxy

PAYLOAD = {"method": "edb", "arguments": {"codes": "S0033227", "beginTime": "2026-01-01",
                                          "endTime": "2026-01-04", "options": ""}}


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"clients": {"a": digest("a" * 40)}, "worker_sha256": digest("w" * 40),
                       "database": str(Path(self.temp.name) / "queue.sqlite"),
                       "queue_policy": {"new_jobs_per_minute": 1}}
        self.app = create_app(self.config)
        self.connection = Connection({"url": "http://127.0.0.1", "token": "a" * 40},
                                     transport=httpx.ASGITransport(app=self.app))
        self.proxy = Proxy({"wait_seconds": 0}, connection=self.connection)

    async def asyncTearDown(self):
        await self.connection.close()
        self.temp.cleanup()

    async def test_rate_rejection_has_no_nonexistent_job_to_poll(self):
        first = await self.proxy.call("query_wind_data", PAYLOAD)
        rejected = await self.proxy.call("query_wind_data", PAYLOAD)
        self.assertFalse(first["result_ready"])
        self.assertEqual(rejected["code"], "RATE_LIMITED")
        self.assertFalse(rejected["accepted"])
        self.assertTrue(rejected["retry_safe"])
        self.assertFalse(rejected["do_not_resubmit"])
        self.assertNotIn("job_id", rejected["relay"])
        self.assertNotIn("poll_tool", rejected["relay"])
        with self.app.state.queue.connection() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    async def test_queue_rejection_does_not_return_poll_instruction(self):
        self.app.state.queue.policy["client_pending_limit"] = 0
        rejected = await self.proxy.call("query_wind_data", PAYLOAD)
        self.assertEqual(rejected["code"], "QUEUE_FULL")
        self.assertFalse(rejected["accepted"])
        self.assertTrue(rejected["retry_safe"])
        self.assertNotIn("poll_tool", rejected["relay"])

    async def test_lost_submission_ack_preserves_id_and_never_creates_second_job(self):
        real = self.connection

        class LostAck:
            async def request(inner, method, path, payload=None):
                result = await real.request(method, path, payload)
                if method == "POST" and path == "/v1/jobs":
                    raise RelayError("RELAY_UNREACHABLE", "synthetic lost acknowledgement", 503)
                return result

        result = await Proxy({"wait_seconds": 0}, connection=LostAck()).call("query_wind_data", PAYLOAD)
        self.assertEqual(result["code"], "RELAY_UNREACHABLE")
        self.assertTrue(result["relay"]["do_not_resubmit"])
        job_id = result["relay"]["job_id"]
        existing = await self.proxy.get(job_id)
        self.assertEqual(existing["relay"]["job_id"], job_id)
        with self.app.state.queue.connection() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    async def test_read_error_after_acceptance_is_not_a_safe_new_submission(self):
        real = self.connection

        class GetRejected:
            async def request(inner, method, path, payload=None):
                if method == "GET":
                    raise RelayError("RATE_LIMITED", "synthetic proxy read rejection", 429)
                return await real.request(method, path, payload)

        result = await Proxy({"wait_seconds": 0}, connection=GetRejected()).call("query_wind_data", PAYLOAD)
        self.assertTrue(result["relay"]["do_not_resubmit"])
        self.assertNotIn("retry_safe", result)
        self.assertIsNotNone(result["relay"]["job_id"])


if __name__ == "__main__":
    unittest.main()
