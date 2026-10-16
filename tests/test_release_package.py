"""Release allowlist and hashes, excluding private state and external core files."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import zipfile

from build_release import ROOT, build, payload


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="relay-release-")
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        for file in payload(ROOT):
            target = self.root / file.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)

    def tearDown(self):
        self.temporary.cleanup()

    def test_source_package_excludes_private_and_core_files_and_verifies_hashes(self):
        private = [
            ".relay/worker.json", ".relay/client-person.json", ".git/config",
            ".venv/bin/python", "runtime/native.lock", "worker-state/inflight.json",
            "server.py", "wind_bridge/backend.py", "WindSDK/WindPy.py",
            "client-owner.json", "gateway.json", "queue.sqlite",
            "relay_private_credentials.py", "dist/old-package.zip",
            "monitor/auth/console.key", "monitor/logs/monitor.log",
            ".console-updates/backup/task.xml", ".relay/console-backups/gateway.json",
            ".gateway-backups/old/queue.sqlite", ".gateway-releases/old/.venv/private.py",
            ".relay/deployment-current.json",
        ]
        marker = hashlib.sha256(str(self.root).encode()).hexdigest()
        for name in private:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(marker, encoding="utf-8")
        result = build(self.root)
        archive = Path(result["archive"])
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), result["sha256"])
        self.assertEqual(archive.with_suffix(".zip.sha256").read_text().split()[0], result["sha256"])
        with zipfile.ZipFile(archive) as package:
            self.assertIsNone(package.testzip())
            names = set(package.namelist())
            self.assertTrue(names.isdisjoint(private))
            manifest = json.loads(package.read("relay-release-manifest.json"))
            self.assertEqual(names, set(manifest["files"]) | {"relay-release-manifest.json"})
            self.assertFalse(manifest["wind_sdk_bundled"])
            self.assertFalse(manifest["wind_core_bundled"])
            self.assertNotEqual(manifest["version"], manifest["required_core_version"])
            for name, info in manifest["files"].items():
                data = package.read(name)
                self.assertNotIn(marker.encode(), data)
                self.assertEqual(hashlib.sha256(data).hexdigest(), info["sha256"])
                self.assertEqual(len(data), info["bytes"])

    @unittest.skipIf(sys.platform == "win32", "Creating Windows symlinks can require elevated permissions")
    def test_file_symlink_in_source_allowlist_is_rejected(self):
        outside = Path(self.temporary.name) / "private.py"
        outside.write_text("private")
        (self.root / "wind_relay/leak.py").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "unsafe symlink"):
            build(self.root)

    @unittest.skipIf(sys.platform == "win32", "Creating Windows symlinks can require elevated permissions")
    def test_allowlisted_directory_symlink_is_rejected(self):
        outside = Path(self.temporary.name) / "outside"
        shutil.move(str(self.root / "wind_relay"), outside)
        (self.root / "wind_relay").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe symlink"):
            build(self.root)

    @unittest.skipIf(sys.platform == "win32", "Creating Windows symlinks can require elevated permissions")
    def test_output_directory_symlink_is_rejected(self):
        outside = Path(self.temporary.name) / "outside-output"
        outside.mkdir()
        (self.root / "dist").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe symlink"):
            build(self.root)
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipIf(sys.platform == "win32", "Creating Windows symlinks can require elevated permissions")
    def test_output_archive_or_checksum_symlink_does_not_overwrite_external_file(self):
        from wind_relay import VERSION
        destination = self.root / "dist"
        destination.mkdir()
        outside = Path(self.temporary.name) / "preserve.txt"
        outside.write_text("preserve this file")
        for suffix in (".zip", ".zip.sha256"):
            with self.subTest(suffix=suffix):
                target = destination / ("wind-terminal-relay-" + VERSION + suffix)
                target.symlink_to(outside)
                try:
                    with self.assertRaisesRegex(ValueError, "unsafe symlink"):
                        build(self.root)
                    self.assertEqual(outside.read_text(), "preserve this file")
                    self.assertEqual(list(destination.iterdir()), [target])
                finally:
                    target.unlink()


if __name__ == "__main__":
    unittest.main()

