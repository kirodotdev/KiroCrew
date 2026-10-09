"""Every mutating Code Review Sage route is the dashboard owner's alone.

A review runs with the owner's ``gh`` login, a post publishes comments as the
owner, and settings, namespaces, runs and pinned repos are the owner's state. So
each mutating route answers a non-owner dashboard subject, and any app token
(this app's own included: nothing in the product calls these routes with one),
with the shared ``owner_only`` 403, before it reads a body or touches a seam.

The requests run through a real aiohttp app whose middleware stamps the identity
the dashboard auth middleware would, and the gate itself is never patched. Each
request carries a body the handler refuses early, so the owner rows prove the
gate admits the owner without the handler doing real work.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

_APP_ROOT = Path(__file__).resolve().parent.parent
_ROUTES = _APP_ROOT / "backend" / "routes.py"
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

_BASE = "/api/apps/code-review-sage"
_OWNER = "sage-owner"
_UNKNOWN_RUN = "unknownrun01"
_TASKRUNNER_ACTIONS = f"/api/taskrunner/{_UNKNOWN_RUN}/review-fix/actions"

# (method, path, handler name, JSON body). Every mutating route and method pair.
_MUTATING = [
    ("POST", "/review", "_handle_review", {}),
    ("POST", "/review-repo", "_handle_review_repo", {}),
    ("POST", f"/runs/{_UNKNOWN_RUN}/post", "_handle_run_post", {}),
    ("POST", f"/runs/{_UNKNOWN_RUN}/cancel", "_handle_run_cancel", {}),
    ("POST", f"/runs/{_UNKNOWN_RUN}/archive", "_handle_run_archive", {}),
    ("DELETE", f"/runs/{_UNKNOWN_RUN}", "_handle_run_delete", {}),
    ("PUT", "/settings", "_handle_settings", {"model": "not-a-real-model"}),
    ("POST", "/namespaces", "_handle_namespaces", {}),
    ("DELETE", "/namespaces", "_handle_namespaces", {}),
    ("POST", "/repos", "_handle_repos", {}),
    ("DELETE", "/repos", "_handle_repos", {}),
    ("POST", "/learnings/consolidate", "_handle_consolidate", {"namespace": "../x"}),
    ("POST", "/followup", "_handle_followup_start", {}),
    ("POST", "/fix-tasks", "_handle_create_fix_task", {}),
    ("POST", f"/fix-tasks/{_UNKNOWN_RUN}/actions", "_handle_fix_action", {}),
    ("POST", _TASKRUNNER_ACTIONS, "_handle_fix_action", {}),
    ("POST", f"/fix-tasks/{_UNKNOWN_RUN}/review-again", "_handle_review_again", {}),
    ("GET", "/local-diff", "_handle_local_diff", {}),
    ("POST", "/local/review", "_handle_local_review", {}),
    ("POST", f"/local/{_UNKNOWN_RUN}/disposition", "_handle_local_disposition", {}),
]
_IDS = [f"{m} {p}" for m, p, _h, _b in _MUTATING]

# The seams a mutating route reaches once past its gate. A refused request must
# reach none of them.
_SEAMS = [
    ("_run_review_bg", None),
    ("_post_comments_bg", None),
    ("_consolidate_bg", None),
    ("_write_review_section", None),
    ("_save_runs", None),
    ("delete_namespace", "learning"),
    ("create_namespace", "learning"),
    ("add_repo", "discovery"),
    ("remove_repo", "discovery"),
]


def _load_routes() -> Any:
    spec = importlib.util.spec_from_file_location("sage_routes_owner_gate", str(_ROUTES))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from kiro_crew.apps.builtins.code_review_sage.backend import fix_tasks

    # Match the existing harness: enablement is not the owner-gate contract.
    fix_tasks.is_app_enabled = lambda name: True

    # The fix-task service owns three of the app's mutating handlers. Attach
    # them to this route-module fixture so every mutating Sage URL is covered
    # by one owner-gate contract instead of two divergent test harnesses.
    for handler_name in (
        "handle_create_fix_task",
        "handle_fix_action",
        "handle_review_again",
    ):
        public_name = f"_handle_{handler_name.removeprefix('handle_')}"
        setattr(mod, public_name, getattr(fix_tasks, handler_name))
    return mod


@web.middleware
async def _identity(request: web.Request, handler):
    request["user"] = request.headers.get("X-Test-User", _OWNER)
    request["app"] = request.headers.get("X-Test-App", "")
    return await handler(request)


async def _call(
    method: str, path: str, handler: str, body: dict, headers: dict[str, str]
) -> tuple[int, dict, list[str]]:
    """Send one request through the real handler; return status, JSON, seams hit."""
    routes = _load_routes()
    calls: list[str] = []
    patches: list[Any] = [mock.patch.object(routes, "is_app_enabled", lambda _name: True)]
    for name, owner in _SEAMS:
        target = getattr(routes, owner) if owner else routes

        def _record(*_a: Any, _name: str = name, **_k: Any) -> Any:
            calls.append(_name)
            return {"ok": True}

        if asyncio.iscoroutinefunction(getattr(target, name)):

            async def _arecord(*a: Any, _r: Any = _record, **k: Any) -> Any:
                return _r(*a, **k)

            patches.append(mock.patch.object(target, name, _arecord))
        else:
            patches.append(mock.patch.object(target, name, _record))
    for p in patches:
        p.start()
    try:
        app = web.Application(middlewares=[_identity])
        app["state"] = SimpleNamespace(owner_id=_OWNER)
        route = path if path.startswith("/api/") else f"{_BASE}{path}"
        app.router.add_route(method, route, getattr(routes, handler))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.request(method, route, json=body, headers=headers)
            try:
                payload = await resp.json()
            except Exception:
                payload = {}
            return resp.status, payload, calls
        finally:
            await client.close()
    finally:
        for p in reversed(patches):
            p.stop()


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


@pytest.mark.parametrize(("method", "path", "handler", "body"), _MUTATING, ids=_IDS)
@pytest.mark.asyncio
async def test_non_owner_dashboard_subject_refused(method, path, handler, body):
    """An allowlisted non-owner (``app == ""``, ``user != owner``) is refused."""
    status, payload, calls = await _call(
        method, path, handler, body, {"X-Test-User": "allowlisted-slack-user"}
    )
    assert status == 403, payload
    assert payload.get("code") == "owner_only"
    assert calls == []


@pytest.mark.parametrize(("method", "path", "handler", "body"), _MUTATING, ids=_IDS)
@pytest.mark.asyncio
async def test_own_app_token_refused(method, path, handler, body):
    """This app's own token is refused: no product caller uses one here."""
    status, payload, calls = await _call(
        method, path, handler, body, {"X-Test-App": "code-review-sage"}
    )
    assert status == 403, payload
    assert payload.get("code") == "owner_only"
    assert calls == []


@pytest.mark.parametrize(("method", "path", "handler", "body"), _MUTATING, ids=_IDS)
@pytest.mark.asyncio
async def test_owner_passes_gate(method, path, handler, body):
    """The owner reaches the handler's own validation, not the gate's 403."""
    status, payload, _calls = await _call(method, path, handler, body, {})
    assert status != 403, payload
    assert payload.get("code") != "owner_only"


@pytest.mark.parametrize(
    ("path", "handler"),
    [
        ("/settings", "_handle_settings"),
        ("/namespaces", "_handle_namespaces"),
        ("/repos", "_handle_repos"),
    ],
)
@pytest.mark.asyncio
async def test_non_owner_get_routes_unchanged(path, handler):
    """Reads stay open as before: the gate sits after each GET branch."""
    status, payload, calls = await _call(
        "GET", path, handler, {}, {"X-Test-User": "allowlisted-slack-user"}
    )
    assert status == 200, payload
    assert calls == []
