"""Eager spawn's agent-binding config load must not run on the gateway loop.

``_eager_spawn`` is speculative: it exists only to make the slot's FIRST real
message faster. Before the handshake it resolves the slot's agent bindings, and
that read goes through ``KiroCrewConfig.load()``, which stats and reads
``config.json`` plus any ``config.local.json`` overlay, deep-merges them and runs
the full jsonschema validation — synchronously, on the single event loop the
gateway shares with every other session. So an optional latency optimisation
stalls unrelated foreground work; that is the inversion this fixes.

Only the load crosses to a worker. The bindings are resolved, the session
acquired and the slot read back on the loop, so no slot or session object is
handed to another thread.

The hop widens an existing race window rather than opening a new one. The load
sits INSIDE the envelope ``_eager_spawn`` already maintains:

    _bound = (slot.agent, slot.model, slot.project, slot.reasoning_effort)
        ↓
    await asyncio.to_thread(KiroCrewConfig.load)   <- the new suspension
        ↓
    resolve bindings
        ↓
    await sessions.get_or_create(...)
        ↓
    not is_new              -> leave the winning session alone
    slot identity changed   -> remove the session this task created
    bindings != _bound      -> remove the session this task created

Every action newly able to land during the load — slot deletion or replacement,
an agent/model/project/effort switch, another creator winning the same key — is
therefore already revalidated after the handshake by guards that predate this
change. The two tests below pin that: they hold the loader suspended and fire the
lifecycle action while it is suspended. They are preservation coverage for the
new await, not proof of an old defect, so they do not need to fail on a pristine
tree — only ``test_config_load_runs_off_the_event_loop`` does that.

A worker left running by a cancelled eager task is harmless: the load is
read-only and its result is simply abandoned, so no cancellation plumbing is
needed for it.
"""

from __future__ import annotations

import asyncio
import json
import threading
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache, config_dir
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _eager_spawn
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

#: Upper bound on the cross-thread handoffs below. Purely a hang guard — every
#: wait in the happy path returns the moment the other side signals, so raising
#: this can never mask a failure, and no assertion depends on elapsed time.
_GUARD_SECS = 30.0


@pytest.fixture(autouse=True)
def _no_debounce(monkeypatch: Any) -> None:
    """Zero the debounce so the body runs without waiting on wall-clock time."""
    monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)


def _state(slot: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state.get_slot = MagicMock(return_value=slot)
    state.sessions = MagicMock()
    state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    state.sessions.release = MagicMock()
    state.sessions.remove = AsyncMock()
    state.sessions.resumable_hint = MagicMock(return_value=True)
    return state


def _write_real_config() -> None:
    """Put a real config.json in the per-test KIROCREW_HOME.

    The root conftest pins that home to a tmp dir for every testpath, so the real
    loader runs against a controlled file rather than the developer's own
    configuration.
    """
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(
        json.dumps({"session": {"eager_spawn": True}}), encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_config_load_runs_off_the_event_loop() -> None:
    """The thread that stats, reads, merges and validates must not be the loop's.

    The wrapper delegates to the real classmethod, so the recorded thread is the
    one that genuinely does the filesystem and schema work — not merely a thread
    that reached a call site. It reads the same whether the production call is
    direct or routed through ``asyncio.to_thread``, so the thread comparison is
    what fails on an unfixed tree.
    """
    _write_real_config()
    # Force the disk path: a warm fingerprint cache would let the recorded thread
    # skip the read, merge and validation this test is about.
    _invalidate_config_cache()

    load_threads: list[int] = []

    def loader() -> KiroCrewConfig:
        load_threads.append(threading.get_ident())
        return KiroCrewConfig.load()

    slot = _ChatSlot("cfg-load-slot")
    state = _state(slot)
    loop_thread = threading.get_ident()

    with patch.object(chat_runner, "KiroCrewConfig", types.SimpleNamespace(load=loader)):
        await _eager_spawn(state, slot)

    assert len(load_threads) == 1, f"expected exactly one config load, got {len(load_threads)}"
    assert load_threads[0] != loop_thread, (
        "KiroCrewConfig.load() ran on the event loop thread "
        f"({loop_thread}); the gateway is blocked for the whole read, "
        "merge and jsonschema validation"
    )
    # The handshake must still happen — a load moved off-loop that also skipped
    # the spawn would satisfy the thread assertion while breaking eager spawn.
    state.sessions.get_or_create.assert_awaited_once()


async def _suspend_load_and(state: DashboardState, slot: _ChatSlot, action: Any) -> str:
    """Run ``_eager_spawn`` with the config load held, firing *action* meanwhile.

    *action* runs on the event loop thread, standing in for the request handler
    that would really mutate the slot, and it runs while the loader is parked —
    so it lands strictly inside the window the new await opens. Returns the
    session key the handshake used.
    """
    entered = threading.Event()
    release = threading.Event()

    def loader() -> MagicMock:
        entered.set()
        # Bounded only so an unfixed tree fails instead of wedging the suite: on
        # a fixed tree the loop is free to run *action* and set this at once.
        release.wait(_GUARD_SECS)
        cfg = MagicMock()
        cfg.session.eager_spawn = True
        return cfg

    bindings = MagicMock()
    bindings.kiro_agent = "kirocrew"
    bindings.resolved_alias = "kirocrew"
    bindings.model = ""

    with (
        patch.object(chat_runner, "KiroCrewConfig", types.SimpleNamespace(load=loader)),
        patch.object(chat_runner, "resolve_agent_bindings", return_value=bindings),
    ):
        task = asyncio.ensure_future(_eager_spawn(state, slot))
        # Waiting off-loop keeps the loop free, which is exactly what makes the
        # loader's thread observable here; no sleep is involved.
        await asyncio.to_thread(entered.wait, _GUARD_SECS)
        assert entered.is_set(), "the config load never started"
        action()
        release.set()
        await task

    assert state.sessions.get_or_create.await_args is not None
    return str(state.sessions.get_or_create.await_args.args[0])


@pytest.mark.asyncio
async def test_slot_replaced_during_the_load_leaves_no_orphan_session() -> None:
    """A delete+recreate while the load is suspended must not strand a session.

    The recreated slot shares the key, so an orphan registered under THIS slot's
    bindings would be silently reused by it. The post-handshake identity guard
    already covers the wider ``get_or_create`` window; this pins that it also
    covers the load.
    """
    slot = _ChatSlot("t1")
    live: dict[str, _ChatSlot] = {"slot": slot}
    state = _state(slot)
    state.get_slot = MagicMock(side_effect=lambda _key: live["slot"])

    key = await _suspend_load_and(
        state, slot, lambda: live.__setitem__("slot", _ChatSlot("t1"))
    )

    state.sessions.remove.assert_awaited_once_with(key)


@pytest.mark.asyncio
async def test_binding_switch_during_the_load_removes_the_stale_session() -> None:
    """An agent switch while the load is suspended must discard the session.

    The switch handler's own reset finds nothing to reset (nothing is registered
    yet), so the session this task goes on to register would carry the bindings
    the user just moved away from. ``_bound`` was snapshotted before the load, so
    the existing equality guard sees the change and tears the session down.
    """
    slot = _ChatSlot("t1")
    slot.agent = "wfe-oncall"
    state = _state(slot)

    def _switch_agent() -> None:
        slot.agent = "sre-oncall"

    key = await _suspend_load_and(state, slot, _switch_agent)

    state.sessions.remove.assert_awaited_once_with(key)
