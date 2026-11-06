"""KAS workflow boot recovery (parity row 7, the owner's option B).

At gateway start Crew reattaches only ITS OWN interrupted runs: those in its run
ledger, not known finished, not driven by a live process here, and reported
ownerless by KAS itself (``_kiro/workflow/load`` from disk is read-only and
reconciles a run whose owner stopped beating to ``paused``). Each chat gets one
reconnect turn. KAS's ``_kiro/workflow/resumeAll`` is never called: it would
take over IDE and other clients' runs.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from kas_workflow_helpers import FakeRuntime

from kiro_crew.kas_workflows import router as router_mod
from kiro_crew.kas_workflows import wire
from kiro_crew.kas_workflows.router import STALE_RUNNING_PAUSE_REASON, KasWorkflowRouter

pytestmark = pytest.mark.asyncio


def _ledger(tmp_path, runs: dict[str, dict[str, Any]]):
    path = tmp_path / "runs.json"
    full = {
        wid: {"cwd": "/work", "createdAt": 100.0 + n, "ended": False, **entry}
        for n, (wid, entry) in enumerate(runs.items())
    }
    path.write_text(json.dumps({"version": 1, "runs": full}), encoding="utf-8")
    return path


def _router(tmp_path, runs, states: dict[str, dict]):
    """A router over *runs*, whose query process answers ``load`` from *states*."""
    reader = FakeRuntime()

    def load(params):
        wid = params["workflowId"]
        if wid not in states:
            raise RuntimeError("no such run")
        return {"workflowId": wid, "state": states[wid]}

    reader.answers[wire.METHOD_LOAD] = load

    async def factory():
        return reader

    router = KasWorkflowRouter(factory, screen=lambda _c: None, ledger_path=_ledger(tmp_path, runs))
    return router, reader


STALE = {"status": "paused", "pauseReason": STALE_RUNNING_PAUSE_REASON}
LIVE = {"status": "running"}


class _Chats:
    def __init__(self, workspaces: dict[str, str]):
        self.workspaces = workspaces
        self.sent: list[str] = []

    def workspace(self, slot_key: str) -> str | None:
        return self.workspaces.get(slot_key)

    async def reconnect(self, slot_key: str) -> bool:
        self.sent.append(slot_key)
        return True


def _outcome(outcomes, wid):
    return next(o for w, _s, o in outcomes if w == wid)


async def test_a_stale_run_is_recovered_once_per_chat(tmp_path):
    router, reader = _router(
        tmp_path,
        {"w1": {"slot": "chat-1"}, "w2": {"slot": "chat-1"}},
        {"w1": STALE, "w2": {"status": "paused", "pauseReason": "Paused by user"}},
    )
    chats = _Chats({"chat-1": "default"})
    outcomes = await router.recover_on_boot(chats.workspace, chats.reconnect)
    assert chats.sent == ["chat-1"]  # one reconnect turn for both of its runs
    assert _outcome(outcomes, "w1") == "reattached"
    assert _outcome(outcomes, "w2") == "reattached"
    # The verdict is KAS's read-only load; nothing resumes from the query process.
    assert {m for m, _p in reader.calls} == {wire.METHOD_LOAD}
    assert all(m != "_kiro/workflow/resumeAll" for m, _p in reader.calls)
    # The dashboard poll counts it as this chat's first try, so it does not double up.
    assert router._reconnect_tries["chat-1"][0] == 1


async def test_a_run_with_a_live_owner_is_left_alone(tmp_path):
    router, _reader = _router(tmp_path, {"w1": {"slot": "chat-1"}}, {"w1": LIVE})
    chats = _Chats({"chat-1": "default"})
    outcomes = await router.recover_on_boot(chats.workspace, chats.reconnect)
    assert chats.sent == []
    assert _outcome(outcomes, "w1") == "skipped: a live owner still holds it"


async def test_an_ide_run_not_in_crews_ledger_is_never_touched(tmp_path):
    # KAS knows about "ide-run" (the IDE started it); Crew's ledger does not.
    router, reader = _router(tmp_path, {"w1": {"slot": "chat-1"}}, {"w1": STALE, "ide-run": STALE})
    chats = _Chats({"chat-1": "default"})
    outcomes = await router.recover_on_boot(chats.workspace, chats.reconnect)
    assert [w for w, _s, _o in outcomes] == ["w1"]
    assert all(p.get("workflowId") != "ide-run" for _m, p in reader.calls)


async def test_finished_and_unreadable_runs_are_skipped_and_logged(tmp_path, caplog):
    router, _reader = _router(
        tmp_path,
        {
            "done": {"slot": "chat-1"},
            "gone": {"slot": "chat-1"},
            "old": {"slot": "chat-1", "ended": True},
        },
        {"done": {"status": "completed"}},
    )
    chats = _Chats({"chat-1": "default"})
    with caplog.at_level("INFO", logger=router_mod.logger.name):
        outcomes = await router.recover_on_boot(chats.workspace, chats.reconnect)
    assert chats.sent == []
    assert _outcome(outcomes, "done") == "skipped: already finished"
    assert _outcome(outcomes, "gone") == "skipped: Kiro Agent could not read it"
    assert "old" not in {w for w, _s, _o in outcomes}  # the ledger already knew it ended
    assert router._ledger["done"]["ended"] is True
    assert "run done (chat chat-1) skipped: already finished" in caplog.text


async def test_a_gone_chat_or_another_workspaces_chat_is_skipped_not_recreated(tmp_path):
    router, _reader = _router(
        tmp_path,
        {
            "w-gone": {"slot": "chat-gone"},
            "w-other": {"slot": "chat-1", "workspace": "other"},
            "w-mine": {"slot": "chat-2", "workspace": "default"},
        },
        {"w-gone": STALE, "w-other": STALE, "w-mine": STALE},
    )
    chats = _Chats({"chat-1": "default", "chat-2": "default"})
    outcomes = await router.recover_on_boot(chats.workspace, chats.reconnect)
    assert chats.sent == ["chat-2"]
    assert _outcome(outcomes, "w-gone") == "skipped: its chat no longer exists"
    assert _outcome(outcomes, "w-other") == "skipped: started in another workspace"
    assert _outcome(outcomes, "w-mine") == "reattached"


async def test_chats_are_capped_per_boot_most_recent_first(tmp_path):
    runs = {f"w{n}": {"slot": f"chat-{n}"} for n in range(4)}
    router, _reader = _router(tmp_path, runs, {w: STALE for w in runs})
    chats = _Chats({f"chat-{n}": "default" for n in range(4)})
    outcomes = await router.recover_on_boot(chats.workspace, chats.reconnect, max_chats=2)
    assert sorted(chats.sent) == ["chat-2", "chat-3"]
    assert _outcome(outcomes, "w0") == "skipped: over the 2-chat boot cap"


async def test_reconnects_run_at_most_concurrency_at_a_time(tmp_path):
    runs = {f"w{n}": {"slot": f"chat-{n}"} for n in range(5)}
    router, _reader = _router(tmp_path, runs, {w: STALE for w in runs})
    in_flight = 0
    peak = 0
    # Each reconnect holds its slot until two are in flight at once, so an
    # unbounded fan-out would start all five before any of them finishes.
    both_slots_taken = asyncio.Event()

    async def reconnect(_slot_key: str) -> bool:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        if in_flight >= 2:
            both_slots_taken.set()
        await both_slots_taken.wait()
        in_flight -= 1
        return True

    await router.recover_on_boot(lambda _s: "default", reconnect, concurrency=2)
    assert peak == 2


async def test_a_run_already_driven_here_is_not_considered(tmp_path):
    router, reader = _router(tmp_path, {"w1": {"slot": "chat-1"}}, {"w1": STALE})
    driver = FakeRuntime()
    import weakref

    router._run_runtime["w1"] = weakref.ref(driver)
    chats = _Chats({"chat-1": "default"})
    assert await router.recover_on_boot(chats.workspace, chats.reconnect) == []
    assert reader.calls == []


async def test_a_launch_stamps_the_chats_workspace_on_the_ledger(tmp_path):
    router, _reader = _router(tmp_path, {}, {})
    router.note_chat_session("chat-1", object(), workspace="team")
    await router._adopt("w9", "chat-1")
    assert router._ledger["w9"]["workspace"] == "team"


# ── The gateway side: gate and wiring ──


def _config(backend: str, switch: bool):
    return SimpleNamespace(agent=SimpleNamespace(acp_backend=backend, kas_workflows=switch))


@pytest.mark.parametrize(("backend", "switch"), [("kas", False), ("", True), ("", False)])
async def test_switch_off_or_a_non_kas_backend_does_nothing(monkeypatch, backend, switch):
    from kiro_crew.dashboard.handlers import kas_workflows as handlers
    from kiro_crew.kas_workflows.engine import ENGINE_KAS, engine_for

    if backend == "kas":
        assert engine_for(backend) == ENGINE_KAS
    slept: list[float] = []

    async def sleep(secs: float) -> None:
        slept.append(secs)

    def no_router():
        raise AssertionError("the router must not be reached")

    monkeypatch.setattr(router_mod, "get_router", no_router)
    result = await handlers.recover_runs_on_boot(
        SimpleNamespace(), sleep=sleep, load_config=lambda: _config(backend, switch)
    )
    assert result == [] and slept == []


async def test_the_gateway_waits_out_kas_abandonment_then_reconnects(monkeypatch):
    from kiro_crew.dashboard.handlers import kas_workflows as handlers

    slept: list[float] = []

    async def sleep(secs: float) -> None:
        slept.append(secs)

    seen: dict[str, Any] = {}

    class _Router:
        async def recover_on_boot(self, chat_workspace, reconnect):
            seen["ws"] = chat_workspace("chat-1"), chat_workspace("chat-x")
            seen["sent"] = await reconnect("chat-1"), await reconnect("chat-x")
            return [("w1", "chat-1", "reattached")]

        def prepare_reconnect(self, slot_key):
            return f"Reconnect workflow runs (reconnect {'0' * 32})"

    started: list[str] = []
    slot = SimpleNamespace(key="chat-1", workspace="default", running=False, task=None)
    slot.enqueue_or_run_prompt = lambda message, *_a: started.append(message) or True
    state = SimpleNamespace(
        get_slot=lambda k: slot if k == "chat-1" else None, push_slots_update=lambda: None
    )
    monkeypatch.setattr(router_mod, "get_router", lambda: _Router())
    result = await handlers.recover_runs_on_boot(
        state, sleep=sleep, load_config=lambda: _config("kas", True)
    )
    assert result == [("w1", "chat-1", "reattached")]
    assert slept == [router_mod.OWNER_ABANDON_SECS + router_mod.BOOT_RECOVERY_SLACK_SECS]
    assert router_mod.OWNER_ABANDON_SECS == 135.0  # KAS STALE_AFTER_MS, 4.5 x 30s beats
    assert seen == {"ws": ("default", None), "sent": (True, False)}
    assert len(started) == 1 and router_mod.KasWorkflowRouter.reconnect_token(started[0])


async def test_a_busy_chat_is_left_to_its_own_turn(monkeypatch):
    from kiro_crew.dashboard.handlers import kas_workflows as handlers

    class _Router:
        async def recover_on_boot(self, chat_workspace, reconnect):
            return [("w1", "chat-1", str(await reconnect("chat-1")))]

    async def sleep(_secs: float) -> None:
        return None

    slot = SimpleNamespace(key="chat-1", workspace="default", running=True)
    slot.enqueue_or_run_prompt = lambda *_a: pytest.fail("a busy chat gets no extra turn")
    state = SimpleNamespace(get_slot=lambda _k: slot, push_slots_update=lambda: None)
    monkeypatch.setattr(router_mod, "get_router", lambda: _Router())
    result = await handlers.recover_runs_on_boot(
        state, sleep=sleep, load_config=lambda: _config("kas", True)
    )
    assert result == [("w1", "chat-1", "False")]
