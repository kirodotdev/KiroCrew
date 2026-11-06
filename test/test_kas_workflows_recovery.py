"""The run ledger, KAS's owner verdict, and boot recovery of this gateway's own runs."""

from __future__ import annotations

import json

import pytest
from kas_workflow_helpers import FakeRuntime

from kiro_crew.kas_workflows import router_base, wire
from kiro_crew.kas_workflows.router import KasWorkflowRouter


def _router(tmp_path, reader: FakeRuntime | None = None) -> KasWorkflowRouter:
    query = reader or FakeRuntime()

    async def factory():
        return query

    return KasWorkflowRouter(
        factory, screen=lambda _c: None, ledger_path=tmp_path / "runs.json", clock=lambda: 1000.0
    )


def _entry(slot: str, created: float, *, ended: bool = False, workspace: str = "") -> dict:
    return {"slot": slot, "cwd": "/w", "createdAt": created, "ended": ended, "workspace": workspace}


# ── The ledger ──


@pytest.mark.asyncio
async def test_the_ledger_keeps_well_formed_entries_and_what_this_run_wrote(tmp_path):
    (tmp_path / "runs.json").write_text(
        json.dumps(
            {
                "runs": {
                    "w1": {"slot": "chat-1", "cwd": "/a", "createdAt": 5, "ended": True},
                    "bad-slot": {"slot": 3},
                    "w2": "not an entry",
                    "w3": {"slot": "chat-3"},
                }
            }
        ),
        encoding="utf-8",
    )
    router = _router(tmp_path)
    router._ledger["w3"] = _entry("chat-new", 9.0)
    await router.ensure_ledger()
    assert set(router._ledger) == {"w1", "w3"}
    assert router._ledger["w1"] == {
        "slot": "chat-1",
        "cwd": "/a",
        "createdAt": 5.0,
        "ended": True,
        "workspace": "",
    }
    assert router._ledger["w3"]["slot"] == "chat-new", "what this run wrote wins"
    await router.ensure_ledger()  # read once


@pytest.mark.parametrize("content", [None, "not json", '["a", "list"]'])
@pytest.mark.asyncio
async def test_a_missing_or_unreadable_ledger_starts_empty(tmp_path, content):
    if content is not None:
        (tmp_path / "runs.json").write_text(content, encoding="utf-8")
    router = _router(tmp_path)
    await router.ensure_ledger()
    assert router._ledger == {}


@pytest.mark.asyncio
async def test_a_saved_ledger_drops_the_oldest_finished_runs_first(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.kas_workflows.router_recovery.LEDGER_MAX_RUNS", 2)
    router = _router(tmp_path)
    router._ledger_loaded = True
    router._ledger.update(
        {
            "old-ended": _entry("c", 1.0, ended=True),
            "new-ended": _entry("c", 5.0, ended=True),
            "old-live": _entry("c", 2.0),
        }
    )
    await router._save_ledger()
    assert set(router._ledger) == {"new-ended", "old-live"}
    saved = json.loads((tmp_path / "runs.json").read_text(encoding="utf-8"))
    assert set(saved["runs"]) == {"new-ended", "old-live"}


# ── KAS's owner verdict ──


@pytest.mark.parametrize(
    "answer, verdict",
    [
        ({"workflowId": "w1", "state": {"status": "paused"}}, "recover"),
        ({"workflowId": "w1", "state": {"status": "running"}}, "live"),
        ({"workflowId": "w1", "state": {"status": "completed"}}, "finished"),
        ({"workflowId": "w1", "state": {}}, "other:unknown"),
        (RuntimeError("no such run"), "unreadable"),
    ],
)
@pytest.mark.asyncio
async def test_kas_says_whether_a_run_has_a_live_owner(tmp_path, answer, verdict):
    reader = FakeRuntime()
    reader.answers[wire.METHOD_LOAD] = answer
    router = _router(tmp_path, reader)
    router._ledger["w1"] = _entry("chat-1", 1.0)
    assert await router._owner_verdict("w1") == verdict
    if verdict == "finished":
        assert router._ledger["w1"]["ended"] is True


# ── Boot recovery ──


@pytest.mark.asyncio
async def test_boot_recovery_reconnects_each_ownerless_chat_once(tmp_path):
    statuses = {
        "a1": "paused",
        "a2": "paused",
        "live": "running",
        "done": "completed",
        "odd": "",
        "gone": "paused",
        "elsewhere": "paused",
        "fails": "paused",
    }
    reader = FakeRuntime()

    def load(params):
        wid = params["workflowId"]
        if wid == "broken":
            raise RuntimeError("unreadable")
        return {"workflowId": wid, "state": {"status": statuses[wid]}}

    reader.answers[wire.METHOD_LOAD] = load
    router = _router(tmp_path, reader)
    router._ledger_loaded = True
    router._ledger.update(
        {
            "a1": _entry("chat-a", 10.0, workspace="ws"),
            "a2": _entry("chat-a", 11.0),
            "live": _entry("chat-b", 1.0),
            "done": _entry("chat-b", 1.0),
            "odd": _entry("chat-b", 1.0),
            "broken": _entry("chat-b", 1.0),
            "gone": _entry("chat-gone", 3.0),
            "elsewhere": _entry("chat-e", 4.0, workspace="other-ws"),
            "fails": _entry("chat-f", 5.0),
            "already-ended": _entry("chat-a", 1.0, ended=True),
        }
    )
    reconnected: list[str] = []

    async def reconnect(slot_key: str) -> bool:
        reconnected.append(slot_key)
        if slot_key == "chat-f":
            raise RuntimeError("chat busy")
        return True

    def workspace(slot_key: str) -> str | None:
        return None if slot_key == "chat-gone" else "ws"

    outcomes = dict(
        (wid, outcome) for wid, _slot, outcome in await router.recover_on_boot(workspace, reconnect)
    )
    assert sorted(reconnected) == ["chat-a", "chat-f"]
    assert outcomes == {
        "a1": "reattached",
        "a2": "reattached",
        "live": "skipped: a live owner still holds it",
        "done": "skipped: already finished",
        "odd": "skipped: not paused (unknown)",
        "broken": "skipped: Kiro Agent could not read it",
        "gone": "skipped: its chat no longer exists",
        "elsewhere": "skipped: started in another workspace",
        "fails": "skipped: its chat could not take the turn",
    }
    # The boot reconnect counts as each chat's first automatic try.
    assert router._reconnect_tries["chat-a"] == (1, 1000.0)


@pytest.mark.asyncio
async def test_boot_recovery_reconnects_at_most_the_cap_newest_first(tmp_path):
    reader = FakeRuntime()
    reader.answers[wire.METHOD_LOAD] = lambda p: {
        "workflowId": p["workflowId"],
        "state": {"status": "paused"},
    }
    router = _router(tmp_path, reader)
    router._ledger_loaded = True
    for n in range(3):
        router._ledger[f"w{n}"] = _entry(f"chat-{n}", float(n))
    reconnected: list[str] = []

    async def reconnect(slot_key: str) -> bool:
        reconnected.append(slot_key)
        return True

    outcomes = await router.recover_on_boot(lambda _s: "", reconnect, max_chats=2)
    assert sorted(reconnected) == ["chat-1", "chat-2"]
    assert ("w0", "chat-0", "skipped: over the 2-chat boot cap") in outcomes


def test_automatic_reconnects_stop_after_their_tries(tmp_path, monkeypatch):
    now = [1000.0]
    router = _router(tmp_path)
    router._clock = lambda: now[0]
    router._ledger["w1"] = _entry("chat-1", 1.0)
    assert router.due_reconnects() == ["chat-1"]
    assert router.due_reconnects() == [], "not again within the interval"
    for _ in range(router_base.RECONNECT_MAX_TRIES):
        now[0] += router_base.RECONNECT_INTERVAL_SECS
        router.due_reconnects()
    now[0] += router_base.RECONNECT_INTERVAL_SECS
    assert router.due_reconnects() == [], "tries spent"
    router.reset_reconnect_tries("chat-1")
    assert router.due_reconnects() == ["chat-1"]


def test_a_step_session_loads_in_its_runs_project(tmp_path):
    router = _router(tmp_path)
    runtime = FakeRuntime()
    runtime._work_dir = "/process"
    router._ledger["w1"] = _entry("chat-1", 1.0)
    assert router._step_cwd("w1", runtime) == "/w"
    assert router._step_cwd("unknown", runtime) == "/process"
