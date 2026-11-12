"""Startup-context reinjection while an abandoned idle compaction's status is owed.

A reader that drops an owed ``completed`` reaches no session, so the prompt
build asks the reader (``take_provider_context_reinjection``) as well as the
session's own flag.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_idle_compaction import (  # noqa: F401 - _no_inherited_cache_tier is an autouse fixture
    KEY,
    _managed,
    _no_inherited_cache_tier,
    _provider_factory,
    _tracked_compaction_statuses,
    _turn,
)

from kiro_crew.agent_sdk.backends import ACP_BACKEND_CODEX
from kiro_crew.config import KiroCrewConfig
from kiro_crew.testing.clock import ManualClock


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def cfg() -> KiroCrewConfig:
    c = KiroCrewConfig()
    c.session.idle_compaction = True
    c.session.idle_compaction_min_context_pct = 40.0
    c.session.autocompact_pct = 70.0
    return c


def _owed_status(status_type: str) -> Any:
    from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

    return JsonRpcMessage(
        method=METHOD_COMPACTION_STATUS, params={"status": {"type": status_type}, "summary": ""}
    )


class TestReinjectionWhileAStatusIsOwed:
    """A dropped status reaches no session, so the prompt build asks the reader."""

    @pytest.mark.asyncio
    async def test_every_prompt_reinjects_while_owed_and_once_after_the_drop(
        self, cfg, clock, tmp_path
    ):
        from kiro_crew.acp import stale_compaction

        async with _managed(cfg, clock, _provider_factory()) as mgr:
            provider = await _turn(mgr)
            reader = _tracked_compaction_statuses(provider, tmp_path, [])
            session = mgr._sessions[KEY]
            session.needs_context_reinjection = False
            stale_compaction.discard_provider_stale_compaction_status(provider, within_secs=1.0)

            # The late status can land in the pre-turn drain, after the prompt
            # is built, so every prompt built while it is owed reinjects.
            assert mgr.consume_needs_reinjection(KEY) is True
            assert mgr.consume_needs_reinjection(KEY) is True

            assert stale_compaction.consume_stale_compaction_status(
                reader, _owed_status("completed")
            )
            assert reader._unresolved_compaction_statuses in (0, None)
            # The compaction it dropped rewrote native history: one more prompt
            # carries the startup context, then the flag rules again.
            assert mgr.consume_needs_reinjection(KEY) is True
            assert mgr.consume_needs_reinjection(KEY) is False

    @pytest.mark.asyncio
    async def test_a_dropped_failed_reinjects_once_only_while_it_was_owed(
        self, cfg, clock, tmp_path
    ):
        from kiro_crew.acp import stale_compaction

        async with _managed(cfg, clock, _provider_factory()) as mgr:
            provider = await _turn(mgr)
            reader = _tracked_compaction_statuses(provider, tmp_path, [])
            session = mgr._sessions[KEY]
            session.needs_context_reinjection = False
            stale_compaction.discard_provider_stale_compaction_status(provider, within_secs=1.0)
            assert mgr.consume_needs_reinjection(KEY) is True

            assert stale_compaction.consume_stale_compaction_status(reader, _owed_status("failed"))
            # A dropped ``failed`` changed no native history.
            assert mgr.consume_needs_reinjection(KEY) is False

    @pytest.mark.asyncio
    async def test_with_nothing_owed_only_the_flag_decides(self, cfg, clock, tmp_path):
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            provider = await _turn(mgr)
            _tracked_compaction_statuses(provider, tmp_path, [])
            session = mgr._sessions[KEY]
            session.needs_context_reinjection = False
            assert mgr.consume_needs_reinjection(KEY) is False

            session.needs_context_reinjection = True
            assert mgr.consume_needs_reinjection(KEY) is True
            assert mgr.consume_needs_reinjection(KEY) is False

    @pytest.mark.asyncio
    async def test_a_provider_with_no_status_reader_is_left_alone(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory(backend=ACP_BACKEND_CODEX)) as mgr:
            await _turn(mgr)
            session = mgr._sessions[KEY]
            session.needs_context_reinjection = False
            assert mgr._compaction._record_idle_unresolved_status(session) is False
            assert mgr.consume_needs_reinjection(KEY) is False
