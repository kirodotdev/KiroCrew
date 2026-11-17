"""``POST /api/hooks/agent`` reads a string ``deliver`` as the bool it names.

The handler tested ``deliver`` for truth, so the JSON string ``"false"`` was
truthy and the result was delivered (dashboard notification and Slack DM)
although the caller asked not to. The shared ``_coerce_bool`` reader maps the
usual spellings; a JSON bool, an absent key and any other value read as before.

The real handler is served at its real path on a tmp_path home; the agent run
is a recorder that releases the session claim and the permit as the real runner
does in its ``finally``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import webhooks
from kiro_crew.dashboard.handlers import hooks as hooks_handlers
from kiro_crew.testing.wait import async_wait_until


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
    """Stands in for ``_run_hook_agent``; records the ``deliver`` each run got."""

    def __init__(self) -> None:
        self.delivers: list[object] = []

    async def run(  # type: ignore[no-untyped-def]
        self, state, session_key, message, name, agent, deliver, timeout_secs, **kwargs
    ):
        self.delivers.append(deliver)
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


_ABSENT = object()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deliver", "expected"),
    [
        ("false", False),
        ("False", False),
        (" off ", False),
        ("0", False),
        ("true", True),
        ("", False),
        ("maybe", True),
        (False, False),
        (True, True),
        (_ABSENT, True),
    ],
    ids=[
        "false",
        "False",
        "off",
        "zero",
        "true",
        "empty",
        "other",
        "json-false",
        "json-true",
        "absent",
    ],
)
async def test_deliver_is_read_as_the_bool_it_names(home, monkeypatch, deliver, expected):
    runs = _Runs()
    monkeypatch.setattr(hooks_handlers, "_run_hook_agent", runs.run)
    body: dict[str, object] = {"message": "deploy failed"}
    if deliver is not _ABSENT:
        body["deliver"] = deliver
    async with TestClient(TestServer(_app())) as http:
        resp = await http.post("/api/hooks/agent", data=json.dumps(body), headers=_headers(home))
        status = resp.status
        await async_wait_until(
            lambda: len(runs.delivers) >= 1, describe=lambda: f"delivers={runs.delivers}"
        )
    assert status == 200
    assert runs.delivers == [expected]
