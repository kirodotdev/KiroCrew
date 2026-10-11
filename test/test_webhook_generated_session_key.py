"""A webhook call without ``sessionKey`` gets a session key of its own.

``api_hooks_agent`` generates the key for a call that names none, and the code
describes that key as per-request. It runs one turn per key at a time, so two
DIFFERENT deliveries that shared a generated key would have the second refused
with ``409 session_busy`` and its event lost. A key the caller supplies keeps its
meaning: the same key is the same session, and a second call while that turn runs
is still refused.

The real handler is served by an aiohttp test server at its real path on a
tmp_path home; the agent run is a recorder that releases the session claim and
the permit as the real runner does in its ``finally``. Wall clocks are frozen.
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
from kiro_crew.testing.clock import ManualClock

TS = 1_700_000_000


@pytest.fixture
def home(tmp_path, monkeypatch):
    for var, sub in (("KIROCREW_HOME", "home"), ("KIROCREW_WORKSPACE", "workspace")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.setattr(webhooks, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(hooks_handlers, "_HOOK_STORE_PATH", tmp_path / "hooks.json")
    monkeypatch.setattr(hooks_handlers, "_sel", MagicMock())
    ManualClock(start=float(TS)).install(monkeypatch, webhooks)
    ManualClock(start=float(TS) + 0.25).install(monkeypatch, hooks_handlers)
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
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.release = asyncio.Event()

    async def run(self, state, session_key, message, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.keys.append(session_key)
        try:
            await asyncio.wait_for(self.release.wait(), 10)
        finally:
            hooks_handlers._hook_inflight_sessions.discard(session_key)
            hooks_handlers._hook_semaphore.release()


async def _until_runs(runs: _Runs, n: int) -> None:
    async def _wait() -> None:
        while len(runs.keys) < n:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait(), 5)


async def _until_idle(session_key: str) -> None:
    """Wait until no turn holds *session_key*, the check a new call is refused on."""

    async def _wait() -> None:
        while session_key in hooks_handlers._hook_inflight_sessions:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait(), 5)


def _app() -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(_background_tasks=set())
    app.router.add_post("/api/hooks/agent", hooks_handlers.api_hooks_agent)
    return app


def _headers(home) -> dict[str, str]:
    raw, _secret, _entry = webhooks.WebhookTokenStore(home).create("ci", require_signature=False)
    return {"Authorization": f"Bearer {raw}", "Content-Type": "application/json"}


@pytest.mark.asyncio
async def test_two_deliveries_without_a_session_key_in_one_second_both_run(home, monkeypatch):
    runs = _Runs()
    monkeypatch.setattr(hooks_handlers, "_run_hook_agent", runs.run)
    headers = _headers(home)
    async with TestClient(TestServer(_app())) as http:
        first = await http.post(
            "/api/hooks/agent", data=json.dumps({"message": "deploy 8 failed"}), headers=headers
        )
        await _until_runs(runs, 1)
        second = await http.post(
            "/api/hooks/agent", data=json.dumps({"message": "deploy 9 failed"}), headers=headers
        )
        second_body = await second.json()
        if second.status == 200:
            await _until_runs(runs, 2)
        runs.release.set()
    refused = f"the second delivery was refused: {second.status} {second_body}"
    assert (first.status, second.status) == (200, 200), refused
    assert len(set(runs.keys)) == 2
    assert all(key.startswith("hook:default:") for key in runs.keys)
    assert second_body["sessionKey"] == runs.keys[1]


@pytest.mark.asyncio
async def test_a_supplied_session_key_names_the_same_session_each_time(home, monkeypatch):
    runs = _Runs()
    runs.release.set()
    monkeypatch.setattr(hooks_handlers, "_run_hook_agent", runs.run)
    headers = _headers(home)
    body = json.dumps({"message": "nightly check", "sessionKey": "hook:nightly"})
    async with TestClient(TestServer(_app())) as http:
        assert (await http.post("/api/hooks/agent", data=body, headers=headers)).status == 200
        await _until_runs(runs, 1)
        await _until_idle("hook:nightly")
        assert (await http.post("/api/hooks/agent", data=body, headers=headers)).status == 200
        await _until_runs(runs, 2)
    assert runs.keys == ["hook:nightly", "hook:nightly"]


@pytest.mark.asyncio
async def test_a_second_call_on_a_running_supplied_key_is_still_refused(home, monkeypatch):
    runs = _Runs()
    monkeypatch.setattr(hooks_handlers, "_run_hook_agent", runs.run)
    headers = _headers(home)
    body = json.dumps({"message": "build 42 failed", "sessionKey": "hook:ci-42"})
    async with TestClient(TestServer(_app())) as http:
        assert (await http.post("/api/hooks/agent", data=body, headers=headers)).status == 200
        await _until_runs(runs, 1)
        second = await http.post("/api/hooks/agent", data=body, headers=headers)
        code = (await second.json()).get("code")
        runs.release.set()
    assert (second.status, code) == (409, "session_busy")
    assert runs.keys == ["hook:ci-42"]
