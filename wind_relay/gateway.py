from __future__ import annotations

import asyncio
import hmac
import json
import re

import jsonschema
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import PROTOCOL_VERSION, VERSION
from .common import IDENTIFIER, MAX_REQUEST_BYTES, MAX_RESULT_BYTES, RelayError, digest, load_contract
from .store import DEFAULT_QUEUE_POLICY, Queue
from .usage import UsageLedger
from .deployment import runtime_identity


def create_app(config, queue=None):
    for name, value in config["clients"].items():
        if not IDENTIFIER.fullmatch(name) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("Invalid client ID or token hash")
    if not re.fullmatch(r"[0-9a-f]{64}", config["worker_sha256"]):
        raise ValueError("Invalid worker token hash")
    if config.get("admin_sha256") is not None and not re.fullmatch(r"[0-9a-f]{64}", config["admin_sha256"]):
        raise ValueError("Invalid administrator token hash")
    hashes = list(config["clients"].values()) + [config["worker_sha256"]]
    if config.get("admin_sha256"):
        hashes.append(config["admin_sha256"])
    if len(set(hashes)) != len(hashes):
        raise ValueError("Client, worker and administrator credentials must be distinct")
    deployment = runtime_identity()
    maintenance_file = config.get("maintenance_file")
    def maintenance():
        from pathlib import Path
        return bool(maintenance_file and Path(maintenance_file).exists())

    contract = load_contract()
    tools = {tool["name"]: tool for tool in contract["tools"]}
    supplied_policy = config.get("queue_policy", {})
    if not isinstance(supplied_policy, dict):
        raise ValueError("queue_policy must be a mapping")
    policy = {**DEFAULT_QUEUE_POLICY, **supplied_policy}
    queue = queue or Queue(config["database"], policy=policy)

    def authenticate(request, role):
        value = request.headers.get("authorization", "")
        if not value.startswith("Bearer "):
            raise RelayError("UNAUTHORIZED", "Bearer token required", 401)
        token = digest(value[7:])
        if role == "worker" and hmac.compare_digest(token, config["worker_sha256"]):
            return "worker"
        if role == "admin" and config.get("admin_sha256") and hmac.compare_digest(token, config["admin_sha256"]):
            return "administrator"
        if role == "client":
            for owner, expected in config["clients"].items():
                if hmac.compare_digest(token, expected):
                    return owner
        raise RelayError("UNAUTHORIZED", "Invalid token or role", 401)

    async def body(request, maximum):
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > maximum:
                raise RelayError("BODY_TOO_LARGE", "Payload exceeds relay limit", 413)
            chunks.append(chunk)
        try:
            value = json.loads(b"".join(chunks), parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
        except (ValueError, UnicodeError):
            raise RelayError("INVALID_JSON", "Body must contain finite JSON values") from None
        if not isinstance(value, dict):
            raise RelayError("INVALID_JSON", "Body must be a JSON object")
        return value

    async def status(request):
        owner = authenticate(request, "client")
        result = await asyncio.to_thread(queue.status, owner)
        return JSONResponse({"protocol_version": PROTOCOL_VERSION, "relay_version": VERSION,
                             "mcp_version": contract["mcp_version"], "queue_policy": queue.policy,
                             "server_sha256": contract["server_sha256"],
                             "deployment": deployment, "maintenance_mode": maintenance(), **result})

    async def usage(request):
        role = "admin" if request.url.path == "/v1/admin/usage" else "client"
        owner = authenticate(request, role)
        if set(request.query_params) - {"days"}:
            raise RelayError("INVALID_WINDOW", "Only the days query parameter is supported")
        value = request.query_params.get("days", "7")
        if not re.fullmatch(r"[1-9][0-9]?", value) or int(value) > 31:
            raise RelayError("INVALID_WINDOW", "days must be an integer in 1..31")

        def snapshot():
            with queue.connection() as con:
                queue.maintain(con)
                return UsageLedger.snapshot(con, queue.clock(), int(value), None if role == "admin" else owner)
        return JSONResponse(await asyncio.to_thread(snapshot))

    async def submit(request):
        owner = authenticate(request, "client")
        if maintenance():
            raise RelayError("GATEWAY_MAINTENANCE", "Gateway upgrade is not activated; no job was accepted", 503)
        value = await body(request, MAX_REQUEST_BYTES)
        if (set(value) != {"job_id", "tool", "arguments"}
                or not isinstance(value.get("tool"), str) or value["tool"] not in tools):
            raise RelayError("UNKNOWN_TOOL", "Submit job_id, an allowlisted tool and its arguments")
        try:
            jsonschema.validate(value["arguments"], tools[value["tool"]]["inputSchema"])
        except jsonschema.ValidationError:
            raise RelayError("INVALID_PARAMS", "Arguments do not match the tool schema") from None
        payload = {"tool": value["tool"], "arguments": value["arguments"]}
        result = await asyncio.to_thread(queue.submit, owner, value["job_id"], payload)
        return JSONResponse(result, status_code=202 if result["state"] == "queued" else 200)

    async def get(request):
        owner = authenticate(request, "client")
        return JSONResponse(await asyncio.to_thread(queue.get, owner, request.path_params["job_id"]))

    async def cancel(request):
        owner = authenticate(request, "client")
        return JSONResponse(await asyncio.to_thread(queue.cancel, owner, request.path_params["job_id"]))

    async def claim(request):
        authenticate(request, "worker")
        if maintenance():
            return JSONResponse({"job": None, "maintenance_mode": True})
        value = await body(request, 4096)
        if (not isinstance(value.get("mode"), str) or value["mode"] not in {"mock", "native"}
                or value.get("server_sha256") != contract["server_sha256"]):
            raise RelayError("CONTRACT_MISMATCH", "Worker mode or source contract does not match", 409)
        job = await asyncio.to_thread(queue.claim, value["mode"])
        return JSONResponse({"job": job})

    async def heartbeat(request):
        authenticate(request, "worker")
        value = await body(request, 4096)
        if not isinstance(value.get("lease_id"), str):
            raise RelayError("INVALID_LEASE", "lease_id must be a string")
        await asyncio.to_thread(queue.heartbeat, request.path_params["job_id"], value.get("lease_id"))
        return JSONResponse({"ok": True})

    async def complete(request):
        authenticate(request, "worker")
        value = await body(request, MAX_RESULT_BYTES)
        if not isinstance(value.get("result"), dict) or not isinstance(value.get("lease_id"), str):
            raise RelayError("INVALID_RESULT", "Result must be an object")
        await asyncio.to_thread(queue.complete, request.path_params["job_id"], value.get("lease_id"), value["result"])
        return JSONResponse({"ok": True})

    async def handle_error(request, exc):
        return JSONResponse({"ok": False, "code": exc.code, "message": str(exc)}, status_code=exc.status)

    app = Starlette(routes=[
        Route("/v1/status", status, methods=["GET"]),
        Route("/v1/usage", usage, methods=["GET"]),
        Route("/v1/admin/usage", usage, methods=["GET"]),
        Route("/v1/jobs", submit, methods=["POST"]),
        Route("/v1/jobs/{job_id}", get, methods=["GET"]),
        Route("/v1/jobs/{job_id}/cancel", cancel, methods=["POST"]),
        Route("/v1/worker/claim", claim, methods=["POST"]),
        Route("/v1/worker/{job_id}/heartbeat", heartbeat, methods=["POST"]),
        Route("/v1/worker/{job_id}/complete", complete, methods=["POST"]),
    ], exception_handlers={RelayError: handle_error})
    app.state.queue = queue
    return app
