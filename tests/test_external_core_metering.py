"""Optional external-core integration using synthetic responses, never WindPy."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

CORE_ROOT = Path(os.environ["WIND_RELAY_TEST_CORE_ROOT"]).resolve() if os.environ.get("WIND_RELAY_TEST_CORE_ROOT") else None
if CORE_ROOT:
    sys.path.append(str(CORE_ROOT))


@unittest.skipUnless(CORE_ROOT, "Set WIND_RELAY_TEST_CORE_ROOT for metering against an external core; responses are synthetic")
class ExternalCoreMeteringTests(unittest.TestCase):
    def setUp(self):
        from wind_relay.executor import ToolExecutor
        self.environment = patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.executor = ToolExecutor({"mcp_root": str(CORE_ROOT), "data_directory": str(root / "state"),
                                      "shared_profile": str(root / "production-placeholder")}, "mock")
        self.job = {"client_id": "device-a", "tool": "query_wind_data", "arguments": {
            "method": "edb", "arguments": {"codes": "S0033227", "beginTime": "2026-01-01",
                                             "endTime": "2026-01-04", "options": ""}}}

    def test_fresh_query_cache_and_second_device_are_metered_separately(self):
        first = self.executor(self.job)
        usage = first["relay_execution"]["usage"]
        self.assertEqual(usage["native_preflight_reservations"], 1)
        self.assertEqual(usage["observed_data_cells"], 4)
        self.assertEqual(usage["cache_hits"], 0)
        cached_job = copy.deepcopy(self.job)
        cached_job["arguments"]["max_age_seconds"] = 86400
        cached = self.executor(cached_job)
        self.assertEqual(cached["relay_execution"]["usage"]["native_preflight_reservations"], 0)
        self.assertEqual(cached["relay_execution"]["usage"]["cache_hits"], 1)
        other_job = copy.deepcopy(cached_job)
        other_job["client_id"] = "device-b"
        other = self.executor(other_job)
        self.assertEqual(other["relay_execution"]["usage"]["native_preflight_reservations"], 1)
        self.assertEqual(other["relay_execution"]["usage"]["cache_hits"], 0)
        self.assertEqual(self.executor.budget.inspect()["usage"]["reserved_native_calls"], 2)

    def test_reading_old_receipt_does_not_recount_its_query(self):
        result = self.executor(self.job)
        read = self.executor({"client_id": "device-a", "tool": "read_wind_receipt",
                              "arguments": {"receipt_id": result["receipt_id"]}})
        self.assertEqual(read["relay_execution"]["usage"]["native_preflight_reservations"], 0)
        self.assertEqual(read["relay_execution"]["usage"]["observed_data_cells"], 0)
        self.assertEqual(read["relay_execution"]["usage"]["cache_hits"], 0)
        self.assertEqual(self.executor.budget.inspect()["usage"]["reserved_native_calls"], 1)

    def test_metadata_read_and_login_check_use_different_reservation_counts(self):
        local = self.executor({"client_id": "device-a", "tool": "wind_status", "arguments": {"connect": False}})
        checked = self.executor({"client_id": "device-a", "tool": "wind_status", "arguments": {"connect": True}})
        self.assertEqual(local["relay_execution"]["usage"]["native_preflight_reservations"], 0)
        self.assertEqual(checked["relay_execution"]["usage"]["native_preflight_reservations"], 1)
        self.assertEqual(checked["relay_execution"]["usage"]["known_estimated_cells"], 0)
        self.assertIsNone(checked["relay_execution"]["usage"]["exact_native_sdk_calls"])

    def test_synthetic_execution_preserves_mock_identity_and_never_imports_windpy(self):
        result = self.executor(self.job)
        self.assertEqual(result["data_source_id"], "relay_mock")
        self.assertTrue(result["synthetic_fixture"])
        self.assertFalse(result["live_wind_query"])
        self.assertEqual(result["relay_execution"]["mode"], "mock")
        self.assertNotIn("WindPy", sys.modules)


if __name__ == "__main__":
    unittest.main()
