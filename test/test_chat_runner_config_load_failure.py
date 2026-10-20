"""A turn whose binding capture cannot read the config still runs with a context builder.

``_run_chat`` captures the slot's agent bindings inside a ``try`` whose ``except``
swallows a failed ``KiroCrewConfig.load()`` for a slot with no memory store, so the
turn carries on without its config. ``cfg`` is bound only inside that ``try``; the
provider name the rest of the turn reads is ``provider_name``, seeded ``""`` before
it. These tests drive a real turn through ``turn_harness`` with a real
``ContextBuilder`` and pin what the turn hands ``build_message`` as
``provider_type``: ``""`` when the capture's read failed, the configured provider
when it did not.

The turn reads the config several times. The capture is the read its loop makes
while it holds its session's ``slot_switch_session_lock`` (the lock that serializes
the capture against a slot switch), so the failure is armed for exactly that read:
the reads before it (the turn ceiling, the slash-command check) and after it (the
context builder's own) succeed, the way a transient read failure leaves them. A read
another thread makes inside the same window -- the first message's early embed,
which starts on the recall pool while a cold start holds the lock -- is not the
capture and is left alone.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pytest
from turn_harness import TurnContext, TurnRecord, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.llm_helpers import slot_switch_session_lock

# Each turn builds a ContextBuilder, whose SkillsLoader opens the skill index and
# starts the catalog worker; the fixture closes both at teardown.
pytestmark = pytest.mark.usefixtures("close_skills_loaders")

_REPLY = AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)


class _UnreadableConfig(RuntimeError):
    """What the failing read raises, so no other error can stand in for it."""


async def _builder_turn(
    arm: Callable[[TurnContext], None] = lambda _ctx: None,
) -> tuple[TurnRecord, list[dict[str, Any]]]:
    """Run one turn with a real ``ContextBuilder``; return it and each build's kwargs.

    *arm* runs on the turn's loop once the builder exists, just before the turn
    starts: building the builder reads the config too.
    """
    builds: list[dict[str, Any]] = []

    def _setup(ctx: TurnContext) -> None:
        builder = ContextBuilder()
        real_build = builder.build_message

        def _recording_build(*args: Any, **kwargs: Any) -> Any:
            builds.append(dict(kwargs))
            return real_build(*args, **kwargs)

        builder.build_message = _recording_build  # type: ignore[method-assign]
        ctx.state.context_builder = builder
        arm(ctx)

    record = await run_turn(
        TurnScript(events=[_REPLY, _DONE], message="with context", setup=_setup)
    )
    return record, builds


def _error_cards(record: TurnRecord) -> list[Any]:
    return [row.get("content") for row in record.window if row.get("role") == "error"]


@pytest.mark.asyncio
async def test_a_failed_binding_capture_read_does_not_end_a_context_builder_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_load = KiroCrewConfig.load
    failed: list[str] = []

    def _arm(ctx: TurnContext) -> None:
        # The registry holds these locks weakly, so the reference kept here is what
        # makes the turn's own lookup return this same lock.
        capture_lock = slot_switch_session_lock(effective_session_key(ctx.slot))
        # Armed on the turn's loop, which is the thread the capture reads on.
        loop_thread = threading.get_ident()

        def _load() -> KiroCrewConfig:
            if capture_lock.locked() and threading.get_ident() == loop_thread:
                failed.append("binding capture")
                raise _UnreadableConfig("config.json cannot be read")
            return real_load()

        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_load))

    record, builds = await _builder_turn(_arm)

    assert failed == ["binding capture"]
    assert _error_cards(record) == []
    assert record.rows("error") == []
    assert record.stop_reason == STOP_REASON_END_TURN
    [(prompt,)] = [call.args for call in record.calls("stream")]
    assert "with context" in prompt
    assert [row["content"] for row in record.rows("assistant")] == ["the reply"]
    # The honest provider name for a config that could not be read.
    assert [build["provider_type"] for build in builds] == [""]


@pytest.mark.asyncio
async def test_a_readable_config_hands_the_builder_its_provider() -> None:
    configured = KiroCrewConfig.load().agent.provider
    assert configured

    record, builds = await _builder_turn()

    assert _error_cards(record) == []
    assert record.stop_reason == STOP_REASON_END_TURN
    assert [build["provider_type"] for build in builds] == [configured]
