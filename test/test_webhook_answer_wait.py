"""``wait`` on ``POST /api/hooks/agent``: a caller may hold its request for the answer.

Opt-in behind ``hooks.return_answer`` (off by default). With it off, or with a
value that cannot be honoured, the call keeps the plain accept receipt and the
response names what was dropped. With it on, a run that ends inside the wait
answers with its outcome and the text delivery sends, after the same redaction.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from webhooks_api_helpers import wired_fixture  # noqa: F401
from webhooks_api_helpers import _payload, _req

from kiro_crew import webhooks
from kiro_crew.dashboard.handlers import hooks as H
from kiro_crew.testing.synthetic_credentials import github_classic

pytestmark = pytest.mark.asyncio

KEY = "hook:ci:build-42"


def _post(body: dict, raw: str):
    return _req("POST", "/api/hooks/agent", body=body, headers={"Authorization": f"Bearer {raw}"})


def _bearer_source() -> str:
    raw, _secret, _entry = webhooks.token_store().create("CI", require_signature=False)
    return raw


def _hook_state(tmp_path, monkeypatch, answer: str):
    state = _make_state(tmp_path)
    state.sessions.record_failure = AsyncMock()
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.notify = MagicMock()
    state.slack_client = None
    state.memory_startup_task = None
    state._background_tasks = set()

    async def inner(*_a, **_k):
        return answer

    monkeypatch.setattr(H, "_run_hook_inner", inner)
    monkeypatch.setattr(H, "_load_hook_execution", lambda _key: None)
    return state


async def _drain(state) -> None:
    await asyncio.gather(*state._background_tasks)


async def test_a_call_without_wait_keeps_the_accept_receipt(wired, tmp_path, monkeypatch):
    state = _hook_state(tmp_path, monkeypatch, "done")
    req = _post({"message": "build 42 failed", "sessionKey": KEY}, _bearer_source())
    req.app["state"] = state

    resp = await H.api_hooks_agent(req)
    await _drain(state)

    assert resp.status == 200
    assert await _payload(resp) == {"status": "accepted", "sessionKey": KEY}


async def test_wait_is_dropped_with_a_warning_while_the_setting_is_off(
    wired, tmp_path, monkeypatch
):
    state = _hook_state(tmp_path, monkeypatch, "done")
    monkeypatch.setattr(H, "_answer_return_enabled", lambda: False)
    req = _post({"message": "build 42 failed", "sessionKey": KEY, "wait": 30}, _bearer_source())
    req.app["state"] = state

    resp = await H.api_hooks_agent(req)
    await _drain(state)

    assert resp.status == 200
    assert await _payload(resp) == {
        "status": "accepted",
        "sessionKey": KEY,
        "warning": "wait ignored: hooks.return_answer is off",
    }


async def test_wait_returns_the_runs_redacted_answer(wired, tmp_path, monkeypatch):
    secret = github_classic()
    state = _hook_state(tmp_path, monkeypatch, f"Build 42 fixed. Token was {secret}")
    monkeypatch.setattr(H, "_answer_return_enabled", lambda: True)
    body = {"message": "build 42 failed", "sessionKey": KEY, "wait": 30, "deliver": False}
    req = _post(body, _bearer_source())
    req.app["state"] = state

    resp = await H.api_hooks_agent(req)
    payload = await _payload(resp)

    assert resp.status == 200
    assert (payload["status"], payload["outcome"]) == ("completed", "completed")
    assert payload["answer"].startswith("Build 42 fixed. Token was ")
    assert secret not in payload["answer"]


async def test_a_run_that_failed_answers_its_outcome_without_the_error_text(
    wired, tmp_path, monkeypatch
):
    state = _hook_state(tmp_path, monkeypatch, "unused")

    async def inner(*_a, **_k):
        raise RuntimeError("memory store unavailable: owner detail")

    monkeypatch.setattr(H, "_run_hook_inner", inner)
    monkeypatch.setattr(H, "_answer_return_enabled", lambda: True)
    body = {"message": "build 42 failed", "sessionKey": KEY, "wait": 30, "deliver": False}
    req = _post(body, _bearer_source())
    req.app["state"] = state

    resp = await H.api_hooks_agent(req)
    payload = await _payload(resp)

    assert resp.status == 200
    assert payload == {"status": "completed", "sessionKey": KEY, "outcome": "error"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("soon", (0, "wait ignored: not a whole number of seconds")),
        (True, (0, "wait ignored: not a whole number of seconds")),
        (0, (0, "")),
        (10**9, (300, "wait capped at 300 seconds")),
        (45, (45, "")),
    ],
)
async def test_wait_values_are_bounded_and_never_refused(raw, expected):
    assert H._answer_wait(raw, True) == expected


async def test_a_run_still_going_is_left_running():
    release = asyncio.Event()

    async def run() -> tuple[str, str]:
        await release.wait()
        return "completed", "late"

    task = asyncio.create_task(run())
    assert await H._wait_for_answer(task, 0) is None
    assert not task.done()
    release.set()
    assert await task == ("completed", "late")
