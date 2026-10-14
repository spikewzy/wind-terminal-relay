"""Cross-platform MCP configuration generation; never contacts Wind or a gateway."""
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

from relay_setup import client_config
from wind_relay.common import ROOT, write_private


class ClientConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="relay-config-")
        self.directory = Path(self.temporary.name) / "含 空格"
        self.directory.mkdir()
        self.config = self.directory / "client-test.json"
        self.token = "test-token-" + "x" * 40
        write_private(self.config, {"url": "https://relay.example.com", "token": self.token})
        self.python = self.directory / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")

    def tearDown(self):
        self.temporary.cleanup()

    def test_workbuddy_json_preserves_target_machine_absolute_paths(self):
        text = client_config(self.config, self.python, "workbuddy")
        entry = json.loads(text)["mcpServers"]["wind_terminal_relay"]
        self.assertEqual(entry["command"], str(self.python.absolute()))
        self.assertEqual(entry["args"], [str(ROOT / "relay_client.py"), "--config", str(self.config.absolute())])
        self.assertEqual(entry["env"]["PYTHONUTF8"], "1")
        self.assertNotIn(self.token, text)
        self.assertNotIn("server.py", text)

    def test_codex_toml_preserves_spaces_unicode_and_private_config_reference(self):
        text = client_config(self.config, self.python, "codex")
        entry = tomllib.loads(text)["mcp_servers"]["wind_terminal_relay"]
        self.assertEqual(entry["command"], str(self.python.absolute()))
        self.assertEqual(entry["args"], [str(ROOT / "relay_client.py"), "--config", str(self.config.absolute())])
        self.assertEqual(entry["startup_timeout_sec"], 30)
        self.assertEqual(entry["tool_timeout_sec"], 30)
        self.assertEqual(entry["env"], {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        self.assertNotIn(self.token, text)

    @unittest.skipIf(sys.platform == "win32", "Windows symlink creation can require elevated permissions")
    def test_venv_python_symlink_is_preserved(self):
        self.python.parent.mkdir(parents=True)
        self.python.symlink_to(sys.executable)
        entry = json.loads(client_config(self.config, self.python, "workbuddy"))["mcpServers"]["wind_terminal_relay"]
        self.assertEqual(entry["command"], str(self.python.absolute()))
        self.assertNotEqual(entry["command"], str(self.python.resolve()))

    def test_missing_private_config_is_rejected(self):
        with self.assertRaises(FileNotFoundError):
            client_config(self.directory / "missing.json", self.python, "codex")


if __name__ == "__main__":
    unittest.main()

