"""Fixtures and builders shared by the ``test_eager_spawn*`` modules.

The two autouse fixtures every one of them runs under (the armed-prefetch registry cleared
around each test, the host-derived prewarm allowance pinned), the mocked dashboard state, the
eager-spawn config and live-watch primers, and the resolved-binding builder.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, ResolvedBindings
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef


@pytest.fixture(autouse=True)
def _isolate_armed_registry():
    """Every successful ``_eager_spawn`` registers in the module-global
    ``_armed_prefetches``; clear it around every test so registrations made
    by one test can never trigger a spurious over-cap eviction in another."""
    chat_runner._armed_prefetches.clear()
    yield
    chat_runner._armed_prefetches.clear()


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_eager_spawn*.py modules that import it calls monkeypatch.undo()
def _pin_prewarm_allowance(monkeypatch):
    """The live-population cap is host-derived (``resource_status.prewarm_allowance``
    reads available memory). Pin it to the fixed ceiling so no test here depends
    on the memory of the machine running it; the admission tests override it."""
    monkeypatch.setattr(
        chat_runner, "_prewarm_allowance", lambda: chat_runner._RESUME_PREFETCH_MAX_LIVE
    )


def _mock_state(slot: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state.get_slot = MagicMock(return_value=slot)
    state.sessions = MagicMock()
    state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.remove = AsyncMock()
    state.sessions.remove_if_unclaimed = AsyncMock(return_value=True)
    state.sessions.resumable_hint = MagicMock(return_value=True)
    return state


def _cfg(enabled: bool) -> MagicMock:
    cfg = KiroCrewConfig(agents={"default": KiroCrewAgentConfig()})
    cfg.session.eager_spawn = enabled
    # Pin the harness: the tool-search replay gate is kiro-cli only, and a
    # fresh config defaults to KAS.
    cfg.agent.acp_backend = ACP_BACKEND_KIRO
    cfg_loader = MagicMock(return_value=cfg)
    return cfg_loader


def _native_resume_cfg() -> MagicMock:
    """A config under which a dashboard resume stays a native ``session/load``.

    ``agent.tool_search`` defaults on, and with it the kiro backend replaces the
    load of a direct dashboard session with a fresh session plus replay -- a
    prefetch that can only be refused, so ``_eager_spawn`` leaves it to the
    first turn before anything spawns. The tests of the speculative LOAD itself
    turn the toggle off so the path still reaches ``get_or_create``.
    """
    loader = _cfg(True)
    loader.return_value.agent.tool_search = False
    return loader


def _prime(enabled: bool) -> None:
    """Adopt a config carrying *enabled* on the process config watcher.

    ``schedule_eager_spawn`` reads ``session.eager_spawn`` from the watcher's
    snapshot, because it runs on the event loop and a snapshot read touches no
    file. ``_eager_spawn`` still loads from disk, so the two are stubbed
    differently and :func:`_cfg` stays the helper for the latter.

    The autouse ``_drop_live_config_snapshot`` fixture in ``test/conftest.py``
    resets the watcher around every test, so this leaves nothing behind.
    """
    cfg = KiroCrewConfig(agents={"default": KiroCrewAgentConfig()})
    cfg.session.eager_spawn = enabled
    live.watch().prime(cfg)


def _bindings(
    *,
    agent: str = "kirocrew",
    alias: str = "default",
    memory_store: str = "default",
) -> ResolvedBindings:
    """Member bindings with provenance; session resolution captures the revision."""
    return ResolvedBindings(
        workspace_dir=Path("workspace"),
        effective_memory_config={},
        kiro_agent=agent,
        model="",
        resolved_alias=alias,
        requested_resolved=True,
        memory_store_name=memory_store,
        selection_kind="member",
        execution_context=(
            ExecutionContext(
                None, MemoryStoreRef(memory_store), "member", agent, selection_name=alias
            )
            if memory_store in ("default", "legacy-v1")
            else None
        ),
    )


def _stub_factory():
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.context_usage_pct = lambda: 0.0
        m.is_alive.return_value = True
        m.is_process_alive = lambda: True
        return m

    return factory
