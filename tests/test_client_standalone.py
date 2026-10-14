"""Real stdio tool discovery with no core, Wind SDK or gateway requests."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from wind_relay.common import ROOT


class StandaloneClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_exposes_thirty_tools_without_importing_core_or_wind(self):
        with tempfile.TemporaryDirectory(prefix="relay-client-only-") as temporary:
            config = Path(temporary) / "client.json"
            config.write_text(json.dumps({"url": "https://relay.example.com", "token": "fixture-" + "x" * 40}), encoding="utf-8")
            bootstrap = """
import importlib.abc
import runpy
import sys
class ForbidCoreAndWind(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'WindPy' or fullname.startswith('WindPy.') or fullname == 'wind_bridge' or fullname.startswith('wind_bridge.'):
            raise RuntimeError('Standalone client must not import the Wind core or SDK')
sys.meta_path.insert(0, ForbidCoreAndWind())
script, config = sys.argv[1:]
sys.argv = [script, '--config', config]
runpy.run_path(script, run_name='__main__')
"""
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-c", bootstrap, str(ROOT / "relay_client.py"), str(config)],
                cwd=str(ROOT),
            )
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {tool.name for tool in tools.tools}
                    self.assertEqual(len(tools.tools), 30)
                    self.assertTrue({"relay_status", "relay_usage", "relay_job_status", "relay_cancel_job"} <= names)
                    self.assertFalse((ROOT / "server.py").exists())
                    self.assertFalse((ROOT / "wind_bridge").exists())


if __name__ == "__main__":
    unittest.main()

