"""The agent's pod API call and provision run.

``pod_api`` hands an agent one request to a pod it started, with the token minted
and spent in the gateway. What is pinned is the shape around that request: the
write switch, the pin re-read, the audit, and the cut reply. The provision run is
pinned to answer only for its own worktree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from kiro_crew.apps.builtins.dev_fleet import agent_pod_api, agent_tools
from kiro_crew.pod import runtime as rt


def _payload(response: web.Response) -> dict[str, Any]:
    return json.loads(response.body.decode())


@pytest.fixture
def ops(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A stand-in ``worktree_ops`` that records each pod call, plus the audit trail."""
    seen: dict[str, Any] = {"calls": [], "audit": []}

    async def _pod_api(name: str, method: str, path: str, data: str, *, allow_write: bool):
        seen["calls"].append((name, method, path, data, allow_write))
        return {"ok": True, "status": 200, "body": seen.get("body", '{"ok": true}')}

    async def _run_op(op: Any) -> dict[str, Any]:
        return await op()

    monkeypatch.setattr(
        agent_pod_api, "_ops", lambda: type("Ops", (), {"_pod_api": staticmethod(_pod_api)})
    )
    monkeypatch.setattr(agent_pod_api, "_run_op", _run_op)
    monkeypatch.setattr(
        agent_pod_api, "_audit", lambda op, target, outcome, **kw: seen["audit"].append(outcome)
    )
    return seen


@pytest.mark.asyncio
async def test_pod_api_sends_a_write_only_with_allow_write(ops: dict[str, Any]) -> None:
    body = {"worktree": "w", "method": "PATCH", "path": "/api/config/kirocrew", "data": "{}"}
    refused = await agent_pod_api.pod_api_response(body)
    assert refused.status == 403
    assert _payload(refused)["code"] == "pod_api_write_not_allowed"
    assert ops["calls"] == []

    sent = await agent_pod_api.pod_api_response({**body, "allow_write": True})
    assert sent.status == 200
    assert ops["calls"] == [("w", "PATCH", "/api/config/kirocrew", "{}", True)]
    assert ops["audit"] == ["denied", "granted"]


@pytest.mark.asyncio
async def test_pod_api_reads_go_through_and_the_reply_is_cut(
    ops: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_pod_api, "enabled_refusal", _no_refusal)
    ops["body"] = "x" * (agent_tools._POD_API_BODY_CHARS + 10)
    answer = await agent_tools.pod_api({"worktree": "w", "method": "get", "path": "/api/kas-login"})
    text = answer["text"]
    assert ops["calls"] == [("w", "GET", "/api/kas-login", "", False)]
    assert text.startswith("Pod `w` answered GET /api/kas-login: HTTP 200\n")
    assert f"[body cut at {agent_tools._POD_API_BODY_CHARS} of" in text
    assert len(text) < agent_tools._POD_API_BODY_CHARS + 200


@pytest.mark.asyncio
async def test_pod_api_redacts_a_credential_before_cutting_the_body(
    ops: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_pod_api, "enabled_refusal", _no_refusal)
    cap = agent_tools._POD_API_BODY_CHARS
    credential = "postgres://user:s3cretpassword@db.example:5432/x"
    prefix = "x" * (cap - len("postgres://user:s3cretpassword") - 1) + " "
    body = prefix + credential + "\n" + "x" * 100
    assert body[cap] == "@"
    ops["body"] = body

    answer = await agent_tools.pod_api({"worktree": "w", "method": "get", "path": "/api/x"})

    text = answer["text"]
    assert "s3cretpassword" not in text
    redacted = agent_tools.redact(body)
    assert len(redacted) > cap
    assert text == (
        "Pod `w` answered GET /api/x: HTTP 200\n"
        f"{redacted[:cap]}\n[body cut at {cap} of {len(redacted)} characters]"
    )


async def _no_refusal() -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"method": "TRACE", "path": "/api/x"}, "invalid_method"),
        ({"method": "GET", "path": ""}, "invalid_path"),
        ({"method": "GET", "path": "/api/x?token=abc"}, "invalid_path"),
        ({"method": "GET", "path": "/api/x", "data": 5}, "invalid_data"),
    ],
)
async def test_pod_api_refuses_a_malformed_call(
    ops: dict[str, Any], body: dict[str, Any], code: str
) -> None:
    resp = await agent_pod_api.pod_api_response({"worktree": "w", **body})
    assert resp.status == 400
    assert _payload(resp)["code"] == code
    assert ops["calls"] == []


class _no_lock:
    """``pod_name_mutex`` without the host lock file, for a test home."""

    def __init__(self, cfg: Any, name: str) -> None:
        pass

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> None:
        return None


# The provision run


@pytest.mark.asyncio
async def test_provision_status_answers_only_for_its_own_worktree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.apps.builtins.dev_fleet import fleet_state, runtime

    monkeypatch.setattr(fleet_state, "_PROVISION_INFLIGHT", {"w": "run1"})
    monkeypatch.setattr(
        runtime,
        "_RUNS",
        {"run1": {"status": "done", "exit_code": 0, "output": [f"l{i}" for i in range(60)]}},
    )
    resp = await agent_pod_api.pod_provision_status_response({"worktree": "w", "run_id": "run1"})
    body = _payload(resp)
    assert resp.status == 200 and body["status"] == "done" and body["exit_code"] == 0
    assert body["output"] == [f"l{i}" for i in range(20, 60)]

    other = await agent_pod_api.pod_provision_status_response(
        {"worktree": "other", "run_id": "run1"}
    )
    assert other.status == 404 and _payload(other)["code"] == "unknown_run"


# The host's KAS engines


def test_a_pod_is_pointed_at_the_host_engines_it_can_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saves the pod a download; an existing root and a missing one are left alone."""
    from kiro_crew.acp import kas_bundle
    from kiro_crew.constants import BUNDLED_KAS_DIR_ENV

    root = tmp_path / "engines" / "kas"
    monkeypatch.setattr(kas_bundle, "kas_engines_root", lambda home=None: root)

    env: dict[str, str] = {}
    rt._share_host_kas_engines(env)
    assert env == {}

    root.mkdir(parents=True)
    rt._share_host_kas_engines(env)
    assert env == {BUNDLED_KAS_DIR_ENV: str(root)}

    staged = {BUNDLED_KAS_DIR_ENV: "/app/kas"}
    rt._share_host_kas_engines(staged)
    assert staged == {BUNDLED_KAS_DIR_ENV: "/app/kas"}


def test_pod_api_refuses_a_pod_repinned_to_another_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin is read again under the name lock, right before the request."""
    from kiro_crew.apps.builtins.dev_fleet import worktree_ops_pods

    mine, other = tmp_path / "mine", tmp_path / "other"
    mine.mkdir()
    other.mkdir()
    sent: list[str] = []
    monkeypatch.setattr(rt, "pod_name_mutex", _no_lock)
    monkeypatch.setattr(rt, "pod_api", lambda *a, **kw: sent.append("sent") or (200, "{}"))
    monkeypatch.setattr(worktree_ops_pods, "_read_pin_strict", lambda cfg, n: (True, str(other)))

    refused = worktree_ops_pods._pod_api_locked(None, "w", str(mine), "GET", "/api/x", "", False)
    assert refused["ok"] is False and "different checkout" in refused["error"]
    assert sent == []

    monkeypatch.setattr(worktree_ops_pods, "_read_pin_strict", lambda cfg, n: (True, str(mine)))
    answered = worktree_ops_pods._pod_api_locked(None, "w", str(mine), "GET", "/api/x", "", False)
    assert answered == {"ok": True, "status": 200, "body": "{}"}
