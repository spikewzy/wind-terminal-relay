"""Package only allowlisted relay source; never bundle the Wind core or credentials."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

from wind_relay import VERSION

ROOT = Path(__file__).resolve().parent


def payload(root=ROOT):
    root = Path(root)
    exact = [
        "README.md", "INSTALL.md", "LICENSE", "build_release.py", "Update.cmd", "Update.ps1", "START-HERE.txt",
        ".gitignore", ".gitattributes", ".github/workflows/checks.yml", ".github/windows-console-smoke.ps1",
        "requirements.in", "requirements-gateway.in", "requirements.lock.txt",
        "requirements-gateway.lock.txt", "references/relay-tool-contract.json",
        "relay_client.py", "relay_gateway.py", "relay_worker.py", "relay_setup.py",
    ]
    files = {root / name for name in exact}
    # New entry points must be reviewed here explicitly; do not glob arbitrary
    # root scripts which might contain private deployment configuration.
    if (root / "relay_admin.py").exists() or (root / "relay_admin.py").is_symlink():
        files.add(root / "relay_admin.py")
    for name in ("monitor.py", "diagnostics.py", "management.py", "index.html", "Start.cmd", "Install.ps1", "README.md", "build_package.py", "Export-Diagnostics.cmd"):
        if (root / "monitor" / name).exists() or (root / "monitor" / name).is_symlink():
            files.add(root / "monitor" / name)
    files.update((root / "wind_relay").glob("*.py"))
    files.update((root / "tests").glob("test_*.py"))
    files.update((root / "docs").rglob("*.md"))
    files.update((root / "examples").glob("*.example.json"))
    for file in files:
        relative = file.relative_to(root)
        has_symlink_parent = any((root / parent).is_symlink() for parent in relative.parents)
        if not file.is_file() or file.is_symlink() or has_symlink_parent:
            raise ValueError("Missing file or unsafe symlink: " + str(file.relative_to(root)))
    return sorted(files)


def build(root=ROOT):
    root = Path(root)
    contract = json.loads((root / "references/relay-tool-contract.json").read_text(encoding="utf-8"))
    manifest = {
        "kind": "wind_terminal_relay_source", "version": VERSION,
        "protocol_version": contract["protocol_version"],
        "required_core_version": contract["mcp_version"],
        "required_server_sha256": contract["server_sha256"],
        "wind_sdk_bundled": False, "wind_core_bundled": False,
        "deployment_acceptance": "No deployment acceptance is implied by this source package",
        "files": {},
    }
    destination = root / "dist"
    if destination.is_symlink():
        raise ValueError("Refusing unsafe symlink output directory: dist")
    destination.mkdir(exist_ok=True)
    archive = destination / f"wind-terminal-relay-{VERSION}.zip"
    checksum = archive.with_suffix(".zip.sha256")
    if archive.is_symlink() or checksum.is_symlink():
        raise ValueError("Refusing unsafe symlink output file")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for file in payload(root):
            relative = file.relative_to(root).as_posix()
            value = file.read_bytes()
            manifest["files"][relative] = {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
            output.writestr(relative, value)
        output.writestr("relay-release-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum.write_text(f"{sha256}  {archive.name}\n", encoding="utf-8")
    return {"archive": str(archive), "sha256": sha256, "files": len(manifest["files"])}


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False))

