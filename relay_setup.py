"""Create private relay credentials; never edits an existing MCP configuration."""
import argparse
from functools import wraps
import json
import os
from pathlib import Path
import secrets
import sys

from wind_relay.common import IDENTIFIER, ROOT, digest, read_config, read_json, validate_base_url, write_private
from wind_relay.admin_lock import configuration_lock


def serialized_change(function):
    @wraps(function)
    def run(gateway_path, *args, **kwargs):
        with configuration_lock(gateway_path):
            return function(gateway_path, *args, **kwargs)
    return run


def initialize(directory, url, clients, allow_private_http=False):
    directory = Path(directory).expanduser().absolute()
    url = validate_base_url(url, allow_private_http)
    if not clients or len(set(clients)) != len(clients) or any(not IDENTIFIER.fullmatch(x) for x in clients):
        raise ValueError("Use unique simple client IDs")
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("Output directory must be empty; existing credentials are never overwritten")
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    worker_token = secrets.token_urlsafe(32)
    admin_token = secrets.token_urlsafe(32)
    tokens = {name: secrets.token_urlsafe(32) for name in clients}
    write_private(directory / "gateway.json", {"database": "queue.sqlite",
                  "clients": {name: digest(token) for name, token in tokens.items()},
                  "worker_sha256": digest(worker_token), "admin_sha256": digest(admin_token)})
    write_private(directory / "admin.json", {"url": url, "token": admin_token,
                  "allow_private_http": allow_private_http})
    write_private(directory / "worker.json", {"url": url, "token": worker_token,
                  "data_directory": "worker-state", "allow_private_http": allow_private_http})
    for name, token in tokens.items():
        write_private(directory / ("client-" + name + ".json"),
                      {"url": url, "token": token, "wait_seconds": 2, "allow_private_http": allow_private_http})
    return {"directory": str(directory), "clients": clients, "tokens_printed": False}


@serialized_change
def add_client(gateway_path, client_id, url, output, allow_private_http=False):
    """Issue one new credential without rotating existing clients/worker/admin."""
    if not isinstance(client_id, str) or not IDENTIFIER.fullmatch(client_id):
        raise ValueError("Use a simple unique client ID")
    gateway_path = Path(gateway_path).expanduser().absolute()
    output = Path(output).expanduser().absolute()
    if output.exists() or output == gateway_path:
        raise ValueError("Output file must not already exist; credentials are never overwritten")
    gateway = read_json(gateway_path)
    if not isinstance(gateway.get("clients"), dict):
        raise ValueError("Gateway configuration requires a clients mapping")
    if client_id in gateway["clients"]:
        raise ValueError("Client ID already exists; revoke explicitly before replacing it")
    if client_id in gateway.get("retired_clients", []):
        raise ValueError("Revoked client IDs retain historical ownership and cannot be assigned again; use a new client ID")
    url = validate_base_url(url, allow_private_http)
    token = secrets.token_urlsafe(32)
    write_private(output, {"url": url, "token": token, "wait_seconds": 2,
                           "allow_private_http": allow_private_http})
    gateway["clients"][client_id] = digest(token)
    write_private(gateway_path, gateway)
    return {"client_id": client_id, "configuration_file": str(output), "tokens_printed": False,
            "gateway_restart_required": True, "existing_credentials_rotated": False}


@serialized_change
def revoke_client(gateway_path, client_id):
    gateway_path = Path(gateway_path).expanduser().absolute()
    gateway = read_json(gateway_path)
    if not isinstance(gateway.get("clients"), dict) or client_id not in gateway["clients"]:
        raise ValueError("Client ID does not exist")
    del gateway["clients"][client_id]
    retired = gateway.setdefault("retired_clients", [])
    if client_id not in retired:
        retired.append(client_id)
    write_private(gateway_path, gateway)
    return {"client_id": client_id, "revoked": True, "tokens_printed": False,
            "gateway_restart_required": True,
            "note": "Revocation blocks future authentication after gateway restart; already-started work is not killed or replayed. Retired client IDs cannot be reassigned."}


@serialized_change
def issue_admin(gateway_path, url, output, allow_private_http=False):
    """Add the new read-only role to an old gateway without resetting credentials."""
    gateway_path = Path(gateway_path).expanduser().absolute()
    output = Path(output).expanduser().absolute()
    gateway = read_json(gateway_path)
    if gateway.get("admin_sha256") or output.exists() or output == gateway_path:
        raise ValueError("Administrator credential already exists; existing credentials are never overwritten")
    url = validate_base_url(url, allow_private_http)
    token = secrets.token_urlsafe(32)
    write_private(output, {"url": url, "token": token, "allow_private_http": allow_private_http})
    gateway["admin_sha256"] = digest(token)
    write_private(gateway_path, gateway)
    return {"configuration_file": str(output), "tokens_printed": False,
            "gateway_restart_required": True, "existing_credentials_rotated": False}


def client_config(path, python, format):
    config = Path(path).expanduser().absolute()
    read_config(config)  # Check that the private file exists; do not print its token.
    python = Path(python).expanduser().absolute()  # Preserve the venv symlink.
    entry = {"command": str(python), "args": [str(ROOT / "relay_client.py"), "--config", str(config)],
             "env": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}}
    if format == "workbuddy":
        return json.dumps({"mcpServers": {"wind_terminal_relay": entry}}, ensure_ascii=False, indent=2)
    lines = ["[mcp_servers.wind_terminal_relay]"]
    lines += [f"{key} = {json.dumps(entry[key], ensure_ascii=False)}" for key in ("command", "args")]
    lines += ["startup_timeout_sec = 30", "tool_timeout_sec = 30", "", "[mcp_servers.wind_terminal_relay.env]"]
    lines += [f"{key} = {json.dumps(value)}" for key, value in entry["env"].items()]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init")
    init.add_argument("--output", default=".relay")
    init.add_argument("--url", default="http://127.0.0.1:8765")
    init.add_argument("--clients", nargs="+", default=["researcher1", "researcher2"])
    init.add_argument("--allow-private-http", action="store_true")
    client = sub.add_parser("client-config")
    client.add_argument("--config", required=True)
    client.add_argument("--python", default=sys.executable)
    client.add_argument("--format", choices=["codex", "workbuddy"], required=True)
    add = sub.add_parser("add-client", help="Issue one device/user token without resetting others")
    add.add_argument("--gateway", required=True)
    add.add_argument("--client-id", required=True)
    add.add_argument("--url", required=True)
    add.add_argument("--output", required=True)
    add.add_argument("--allow-private-http", action="store_true")
    revoke = sub.add_parser("revoke-client", help="Revoke one client, effective after gateway restart")
    revoke.add_argument("--gateway", required=True)
    revoke.add_argument("--client-id", required=True)
    admin = sub.add_parser("issue-admin", help="Add a read-only administrator to an old gateway")
    admin.add_argument("--gateway", required=True)
    admin.add_argument("--url", required=True)
    admin.add_argument("--output", required=True)
    admin.add_argument("--allow-private-http", action="store_true")
    args = parser.parse_args()
    if args.action == "init":
        print(json.dumps(initialize(args.output, args.url, args.clients, args.allow_private_http), ensure_ascii=False))
    elif args.action == "client-config":
        print(client_config(args.config, args.python, args.format))
    elif args.action == "add-client":
        print(json.dumps(add_client(args.gateway, args.client_id, args.url, args.output, args.allow_private_http), ensure_ascii=False))
    elif args.action == "revoke-client":
        print(json.dumps(revoke_client(args.gateway, args.client_id), ensure_ascii=False))
    else:
        print(json.dumps(issue_admin(args.gateway, args.url, args.output, args.allow_private_http), ensure_ascii=False))


if __name__ == "__main__":
    main()
