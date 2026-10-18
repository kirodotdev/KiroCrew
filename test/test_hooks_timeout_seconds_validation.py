"""A malformed ``timeoutSeconds`` on ``POST /api/hooks/agent`` gets the handler's 400.

``json.loads`` reads ``1e999`` as ``float('inf')``, and ``int(inf)`` raises
``OverflowError``, which is neither ``ValueError`` nor ``TypeError``. The
handler catches those two to answer a malformed value with
``400 timeout_not_an_integer``, so an infinity must land in the same answer.

The malformed cases are an infinity of either sign, a string, a list and
``null``. The valid cases keep their clamped meaning: missing (the default), a
plain number, a negative (floored at 60) and a huge finite value (capped).

The real handler is served by an aiohttp test server at its real path on a
tmp_path home; the agent run is a recorder that releases the session claim and
the permit as the real runner does in its ``finally``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import webhooks
from kiro_crew.dashboard.handlers import hooks as hooks_handlers
from kiro_crew.testing.wait import async_wait_until, await_until


@pytest.fixture
def home(tmp_path, monkeypatch):
    for var, sub in (("KIROCREW_HOME", "home"), ("KIROCREW_WORKSPACE", "workspace")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.setattr(webhooks, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(hooks_handlers, "_HOOK_STORE_PATH", tmp_path / "hooks.json")
    monkeypatch.setattr(hooks_handlers, "_sel", MagicMock())
    webhooks._reset_signature_replay()
    webhooks._reset_auth_throttle()
    inherited = hooks_handlers._hook_semaphore._value
    hooks_handlers._reset_hook_inflight()
    yield tmp_path
    webhooks._reset_signature_replay()
    webhooks._reset_auth_throttle()
    hooks_handlers._reset_hook_inflight()
    while hooks_handlers._hook_semaphore._value < inherited:
        hooks_handlers._hook_semaphore.release()


class _Runs:
    """Stands in for ``_run_hook_agent``; records the timeout each run got."""

    def __init__(self) -> None:
        self.timeouts: list[int] = []
        self.release = asyncio.Event()
        self.release.set()

    async def run(  # type: ignore[no-untyped-def]
        self, state, session_key, message, name, agent, deliver, timeout_secs, **kwargs
    ):
        self.timeouts.append(timeout_secs)
        try:
            await await_until(self.release)
        finally:
            hooks_handlers._hook_inflight_sessions.discard(session_key)
            hooks_handlers._hook_semaphore.release()


def _app() -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(_background_tasks=set())
    app.router.add_post("/api/hooks/agent", hooks_handlers.api_hooks_agent)
    return app


def _headers(home) -> dict[str, str]:
    raw, _secret, _entry = webhooks.WebhookTokenStore(home).create("ci", require_signature=False)
    return {"Authorization": f"Bearer {raw}", "Content-Type": "application/json"}


def _hook_body(timeout_json: str | None) -> str:
    """A raw JSON body; *timeout_json* is spliced in verbatim so ``1e999`` stays a literal."""
    if timeout_json is None:
        return json.dumps({"message": "deploy failed"})
    return '{"message": "deploy failed", "timeoutSeconds": ' + timeout_json + "}"


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_json", ["1e999", "-1e999", '"ten"', "[60]", "null"])
async def test_malformed_timeout_seconds_is_a_400(home, monkeypatch, timeout_json):
    runs = _Runs()
    monkeypatch.setattr(hooks_handlers, "_run_hook_agent", runs.run)
    async with TestClient(TestServer(_app())) as http:
        resp = await http.post(
            "/api/hooks/agent", data=_hook_body(timeout_json), headers=_headers(home)
        )
        status = resp.status
        body = await resp.json()
    assert (status, body.get("code")) == (400, "timeout_not_an_integer"), body
    assert runs.timeouts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeout_json", "expected"),
    [
        (None, hooks_handlers._HOOK_TIMEOUT_DEFAULT),
        ("120", 120),
        ("-5", 60),
        ("1e300", hooks_handlers._HOOK_TIMEOUT_MAX),
        ("99999999999999999999999", hooks_handlers._HOOK_TIMEOUT_MAX),
    ],
)
async def test_valid_timeout_seconds_keeps_its_clamped_value(
    home, monkeypatch, timeout_json, expected
):
    runs = _Runs()
    monkeypatch.setattr(hooks_handlers, "_run_hook_agent", runs.run)
    async with TestClient(TestServer(_app())) as http:
        resp = await http.post(
            "/api/hooks/agent", data=_hook_body(timeout_json), headers=_headers(home)
        )
        status = resp.status
        await async_wait_until(
            lambda: len(runs.timeouts) >= 1, describe=lambda: f"timeouts={runs.timeouts}"
        )
    assert status == 200
    assert runs.timeouts == [expected]
