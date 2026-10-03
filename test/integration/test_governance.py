"""Spawn admission through the running gateway: the seams behind bugs 14 and 20.

Every request here goes through ``POST /api/spawn`` on a booted gateway, with
the credentials a real MCP caller presents (``gw.mcp_headers``). Nothing is
patched inside the process: the memory bar is set through the operator's own
``config.local.json``, the agent specs through the kiro agents directory.

* bug 20 -- a spawn the memory guard holds back was reported as ``queued behind
  concurrency limit``. The contract now: a deferral names its kind
  (``low_memory``) and its figures, durable or not, and never the cap.
* bug 14 -- the parent agent spec's spawn allow-list. kiro-cli's contract
  (``docs/reference/kiro-cli/chat/subagents.md``) puts it at
  ``toolsSettings.subagent.availableAgents`` (``trustedAgents`` beside it only
  waives permission prompts). The spawn gate honours it: a spawn of an installed
  agent outside the list is refused with ``agent_not_available`` and a message
  that names the agent, the parent and the list. Pinned as the positive contract.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from kiro_crew.subagent_wait_reasons import (
    QUEUED_REASON_CONCURRENCY_LIMIT,
    QUEUED_REASON_LOW_MEMORY,
)

pytestmark = pytest.mark.integration

#: Higher than any host has: forces the memory guard's "not enough" branch
#: without touching the guard itself.
UNREACHABLE_MEMORY_BAR_GB = 1_000_000.0


def _pin_memory_bar(home: Path) -> None:
    """Raise the bar in the operator's override file, keeping whatever else the
    harness or an earlier step wrote there."""
    path = home / "config.local.json"
    doc = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    doc.setdefault("agent", {})["spawn_min_memory_gb"] = UNREACHABLE_MEMORY_BAR_GB
    path.write_text(json.dumps(doc), encoding="utf-8")


async def _slot_session(gw, memory_mode: str, **extra) -> str:
    body = {"memory_mode": memory_mode, **extra}
    slot = (await gw.post_json("/api/chat/slots", body))["key"]
    return f"dashboard:{slot}"


async def _spawn(gw, session: str, **body):
    payload = {"task": "say hello and stop", "parent_session": session, "max_turns": 1}
    payload.update(body)
    return await gw.post("/api/spawn", payload, headers=gw.mcp_headers(session), auth=False)


@pytest.mark.asyncio
async def test_spawn_is_guarded(gateway_boot) -> None:
    async with gateway_boot() as gw:
        resp = await gw.post("/api/spawn", {"task": "x"}, auth=False)
        assert resp.status in (401, 403), await resp.text()


# ---------------------------------------------------------------- bug 20 ---


@pytest.mark.asyncio
async def test_a_memory_deferral_is_named_as_one(gateway_boot, integration_home) -> None:
    """A persistent parent: the row is durable, so the guard DEFERS it. The
    answer must say ``queued`` for a memory reason with the figures -- not
    ``spawned``, and not the capacity queue (bug 20).
    """
    _pin_memory_bar(integration_home)
    async with gateway_boot() as gw:
        session = await _slot_session(gw, "persistent")
        resp = await _spawn(gw, session)
        body = await resp.json()
        assert resp.status == 200, body
        assert body["status"] == "queued", body
        assert body["reason"] == QUEUED_REASON_LOW_MEMORY, body
        assert body["reason"] != QUEUED_REASON_CONCURRENCY_LIMIT
        detail = body["reason_detail"]
        assert "low memory" in detail and "GB" in detail, detail
        assert "concurrency" not in detail.lower(), detail


@pytest.mark.asyncio
async def test_a_non_durable_memory_wait_is_named_as_one(gateway_boot, integration_home) -> None:
    """An incognito parent has no durable row to park, but a capacity verdict
    is never a refusal: the start waits in the in-memory window, and the answer
    names the memory kind and figures, never the capacity queue (bug 20).
    """
    _pin_memory_bar(integration_home)
    async with gateway_boot() as gw:
        session = await _slot_session(gw, "incognito")
        resp = await _spawn(gw, session)
        body = await resp.json()
        assert resp.status == 200, body
        assert body["status"] == "queued", body
        assert body["reason"] == QUEUED_REASON_LOW_MEMORY, body
        detail = body["reason_detail"]
        assert "low memory" in detail and "GB" in detail, detail
        assert "concurrency" not in detail.lower(), detail


# ---------------------------------------------------------------- bug 14 ---


def _agents_dir(home: Path) -> Path:
    """Where boot materializes the agent specs (``KIRO_HOME`` is ``<home>/kiro``).

    Written BEFORE the boot: the spawn path answers agent names from a snapshot
    the boot scans once (``_scan_materialized_agents``); a spec dropped in
    afterwards is not dispatchable until the next registration or boot, which
    the loader documents as accepted staleness.
    """
    directory = home / "kiro" / "agents"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_spec(directory: Path, name: str, **extra) -> None:
    spec = {"name": name, "description": f"{name} for the spawn admission test", "prompt": "x"}
    spec.update(extra)
    (directory / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


async def _settled(gw, session: str, spawn_id: str, *, secs: float = 60.0) -> dict:
    """Wait until the run is done so no child outlives the test body."""
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        listing = await gw.get_json("/api/spawn", headers=gw.mcp_headers(session), auth=False)
        for entry in listing["agents"]:
            if entry["id"] == spawn_id and entry["done"]:
                return entry
        await asyncio.sleep(0.2)
    pytest.fail(f"spawn {spawn_id} did not settle in {secs}s")


#: Where kiro-cli reads the spawn allow-list from an agent spec. A glob list;
#: omitting it allows every agent. ``trustedAgents`` beside it is a different
#: knob (no permission prompts), which is why it is not the one asserted.
SPAWN_ALLOW_LIST = ("toolsSettings", "subagent", "availableAgents")


@pytest.mark.asyncio
async def test_a_spawn_outside_the_allow_list_is_refused(gateway_boot, integration_home) -> None:
    """The parent session runs a spec whose allow-list names ``ally`` alone. A
    spawn of ``stranger`` -- installed, but not listed -- is refused before
    any child starts, with the allow-list code, and the message names the
    agent, the parent and the allowed agent (bug 14). No child is started,
    so there is nothing to await.
    """
    agents = _agents_dir(integration_home)
    _write_spec(agents, "gatekeeper", toolsSettings={"subagent": {"availableAgents": ["ally"]}})
    _write_spec(agents, "ally")
    _write_spec(agents, "stranger")
    async with gateway_boot() as gw:
        session = await _slot_session(gw, "persistent", agent="gatekeeper", agent_kind="template")

        resp = await _spawn(gw, session, agent="stranger")
        body = await resp.json()
        if resp.status == 200 and body.get("status") in ("spawned", "queued"):
            # Admitted: let the child finish so nothing outlives the boot, then
            # fail on the contract below.
            await _settled(gw, session, body["id"])
        assert resp.status == 400, body
        assert body["code"] == "agent_not_available", body
        # The code is the contract; of the prose only the three names are
        # pinned (the refused agent, the parent, the allowed one), so a
        # rewording that keeps them still passes.
        message = body["error"]
        for name in ("stranger", "gatekeeper", "ally"):
            assert name in message, (name, message)


# ---------------------------------------------------------------- bug 15 ---

SECURITY_SNAPSHOTS = (
    "/api/security/denied-commands",
    "/api/security/posture",
    "/api/security/stats",
)


@pytest.mark.asyncio
async def test_security_snapshots_are_guarded(gateway_boot) -> None:
    async with gateway_boot() as gw:
        for path in SECURITY_SNAPSHOTS:
            resp = await gw.get(path, auth=False)
            assert resp.status in (401, 403), (path, await resp.text())


@pytest.mark.asyncio
async def test_a_fresh_home_ships_the_credential_ceiling_intact(gateway_boot) -> None:
    """What the security surface reports on a home nobody has configured: no
    opt-out is recorded, every built-in denied rule is on, and the AWS
    credential directory is among the paths the gate refuses. A report that
    the agent read ``~/.aws/credentials`` (bug 15) is triaged against this
    baseline first: if these hold on the reporter's home, the read did not go
    through the gate this snapshot describes.
    """
    async with gateway_boot() as gw:
        denied = await gw.get_json("/api/security/denied-commands")
        assert denied["disable_all"] is False, denied
        assert denied["user_added"] == [], denied
        assert denied["governance_locked"] is False, denied
        builtins = denied["builtins"]
        assert builtins and all(rule["enabled"] for rule in builtins), [
            rule["id"] for rule in builtins if not rule["enabled"]
        ]
        assert denied["effective_count"] == len(builtins), denied["effective_count"]

        posture = await gw.get_json("/api/security/posture")
        controls = {c["key"]: c for c in posture["controls"]}
        sensitive = controls["sensitive_paths"]
        labels = {item["label"] for item in sensitive["items"]}
        assert "~/.aws" in labels, sorted(labels)[:40]
        assert sensitive["count"] == len(sensitive["items"]), sensitive["count"]
        assert posture["counts"]["denied_commands"] == denied["effective_count"], posture["counts"]

        stats = await gw.get_json("/api/security/stats")
        assert stats["denied_commands"] == denied["effective_count"], stats
