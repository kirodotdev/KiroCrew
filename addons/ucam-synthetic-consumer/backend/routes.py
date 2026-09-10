from __future__ import annotations

import asyncio
import json

from aiohttp import web

from kiro_crew.apps.route_registry import AppRoute
from kiro_crew.ucam_consumer import (
    AGENT_NAME,
    APP_NAME,
    IDENTIFIER,
    MAX_TASK_BYTES,
    ConsumerError,
    RunStore,
    load_binding,
)


async def consume(request, ctx):
    if request.get("app") != APP_NAME or ctx.name != APP_NAME:
        return web.json_response({"code": "ucam_app_binding"}, status=403)
    try:
        binding = await load_binding()
    except ConsumerError as error:
        return web.json_response({"code": error.code}, status=503)
    if request.get("user") != APP_NAME:
        return web.json_response({"code": "ucam_owner_binding"}, status=403)
    try:
        if request.content_length is None or request.content_length > MAX_TASK_BYTES:
            raise ValueError()
        body = await asyncio.wait_for(request.json(), timeout=2.5)
        if not isinstance(body, dict) or body.keys() != {"task", "request_id"}:
            raise ValueError()
        task = body["task"]
        request_id = body["request_id"]
        if not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id):
            raise ValueError()
        if not isinstance(task, str) or not task.strip() or len(task.encode()) > MAX_TASK_BYTES:
            raise ValueError()
    except (ValueError, UnicodeError, asyncio.TimeoutError):
        return web.json_response({"code": "ucam_task_body"}, status=400)
    if ctx.spawn is None:
        return web.json_response({"code": "ucam_spawn_unavailable"}, status=503)
    try:
        store = RunStore(binding)
        row, fresh = await store.call("reserve", request_id, task)
        if not fresh:
            if not row["run_id"]:
                return web.json_response({"code": "ucam_dispatch_ambiguous"}, status=409)
            return web.json_response({"run_id": row["run_id"], "phase": row["phase"]}, status=202)
        run_id = await ctx.spawn.run(task, agent=AGENT_NAME, silent=True)
        await store.call("bind", request_id, run_id)
    except ConsumerError as error:
        return web.json_response({"code": error.code}, status=409)
    except Exception:
        return web.json_response({"code": "ucam_spawn_refused"}, status=503)
    return web.json_response({"run_id": run_id, "phase": "queued"}, status=202)


async def result(request, ctx):
    if request.get("app") != APP_NAME or request.get("user") != APP_NAME or ctx.name != APP_NAME:
        return web.json_response({"code": "ucam_app_binding"}, status=403)
    run_id = request.match_info.get("run_id", "")
    if not IDENTIFIER.fullmatch(run_id):
        return web.json_response({"code": "ucam_run_unknown"}, status=404)
    try:
        binding = await load_binding()
        row = await RunStore(binding).call("get", run_id)
    except ConsumerError as error:
        return web.json_response({"code": error.code}, status=503)
    if row is None:
        return web.json_response({"code": "ucam_run_unknown"}, status=404)
    spawn = getattr(ctx, "spawn", None)
    if row["phase"] in ("queued", "running") and spawn is not None and spawn.is_done(run_id):
        row.update(phase="failed", outcome="ucam_native_result_missing")
        try:
            await RunStore(binding).call(
                "finish",
                run_id,
                row["phase"],
                row["text"],
                row["outcome"],
                json.loads(row["evidence"]),
            )
        except ConsumerError as error:
            return web.json_response({"code": error.code}, status=503)
    return web.json_response(
        {
            **{name: row[name] for name in ("run_id", "phase", "text", "outcome")},
            "evidence": json.loads(row["evidence"]),
        }
    )


def register_routes(ctx):
    return [AppRoute("POST", "/consume", consume), AppRoute("GET", "/runs/{run_id}", result)]
