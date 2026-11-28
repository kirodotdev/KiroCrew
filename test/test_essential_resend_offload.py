"""What a warm re-send of a member's essentials leaves out, and what it keeps.

Three pieces of the snapshot's wire traffic, each pinned at the delivery and
through the real builder: the briefing is acknowledged apart from the rest, a
warm re-send blanks the sources the engine already holds, and a cancelled turn on
a harness that keeps its history leaves the earlier receipts standing.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_essential_delivery import HEADER, Wire, build, provider, send
from test_member_essential_context import env as _essential_env

from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.essential_delivery import EssentialDelivery
from kiro_crew.members import member_briefing_path, member_briefing_supported, slug_for_name

env = _essential_env

_DONE = [
    AcpEvent(kind=EVENT_TEXT_CHUNK, text="Done"),
    AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
]
_CANCELLED = [
    AcpEvent(kind=EVENT_TEXT_CHUNK, text="partial"),
    AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED),
]
_SYNTHETIC_CANCEL = [
    AcpEvent(kind=EVENT_TEXT_CHUNK, text="partial"),
    AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED, synthetic_completion=True),
]


class _Recorder:
    """A send that records each message and answers with the queued events."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.events = list(_DONE)

    async def __call__(self, message):
        self.sent.append(message)
        for event in self.events:
            yield event


async def _drain(delivery, message, wire, **kwargs):
    return [e async for e in delivery.stream(message, wire, lambda: "conv", **kwargs)]


def _bind(delivery, briefing, *, volatile=True, rest="REST"):
    envelope = f"HEAD|{briefing}|{rest}"
    delivery.bind(envelope, scope=("s",), incarnation="conv", volatile=briefing if volatile else "")
    return f"before {envelope} after"


# ── the briefing is acknowledged apart from the rest ────────────────────────


@pytest.mark.asyncio
async def test_a_changed_briefing_alone_goes_out_while_the_rest_is_held():
    delivery, wire = EssentialDelivery(), _Recorder()
    await _drain(delivery, _bind(delivery, "[NOTES one]"), wire)
    await _drain(delivery, _bind(delivery, "[NOTES two]"), wire)
    await _drain(delivery, _bind(delivery, "[NOTES two]"), wire)
    assert wire.sent == [
        "before HEAD|[NOTES one]|REST after",
        "before [NOTES two] after",
        "before  after",
    ]


@pytest.mark.asyncio
async def test_a_change_outside_the_briefing_sends_the_whole_envelope():
    delivery, wire = EssentialDelivery(), _Recorder()
    await _drain(delivery, _bind(delivery, "[NOTES one]"), wire)
    await _drain(delivery, _bind(delivery, "[NOTES one]", rest="REST2"), wire)
    assert wire.sent[1] == "before HEAD|[NOTES one]|REST2 after"


@pytest.mark.asyncio
async def test_a_briefing_only_send_that_fails_sends_the_whole_envelope_next():
    delivery, wire = EssentialDelivery(), _Recorder()
    await _drain(delivery, _bind(delivery, "[NOTES one]"), wire)
    wire.events = [AcpEvent(kind=EVENT_COMPLETE, stop_reason="error")]
    await _drain(delivery, _bind(delivery, "[NOTES two]"), wire)
    wire.events = list(_DONE)
    await _drain(delivery, _bind(delivery, "[NOTES two]"), wire)
    assert wire.sent[1:] == ["before [NOTES two] after", "before HEAD|[NOTES two]|REST after"]


@pytest.mark.asyncio
@pytest.mark.parametrize("volatile", ["", "absent", "HEAD"])
async def test_a_span_that_does_not_occur_once_is_not_split(volatile):
    """No span, one the envelope lacks, or one it holds twice: the bind ignores it."""
    delivery, wire = EssentialDelivery(), _Recorder()
    for briefing in ("[NOTES one]", "[NOTES two]"):
        envelope = f"HEAD|{briefing}|HEAD"
        delivery.bind(envelope, scope=("s",), incarnation="conv", volatile=volatile)
        await _drain(delivery, f"<{envelope}>", wire)
    assert wire.sent == ["<HEAD|[NOTES one]|HEAD>", "<HEAD|[NOTES two]|HEAD>"]


@pytest.mark.asyncio
async def test_a_forced_bind_sends_the_whole_envelope_with_the_briefing():
    """A compaction forces the bind: the model lost the snapshot, so all of it goes."""
    delivery, wire = EssentialDelivery(), _Recorder()
    await _drain(delivery, _bind(delivery, "[NOTES one]"), wire)
    delivery.bind("HEAD|[NOTES one]|REST", scope=("s",), incarnation="conv", force=True)
    await _drain(delivery, "before HEAD|[NOTES one]|REST after", wire)
    assert wire.sent[1] == "before HEAD|[NOTES one]|REST after"


@pytest.mark.asyncio
async def test_a_saved_note_through_the_builder_sends_only_the_assignment(env):
    if not member_briefing_supported():
        pytest.skip("briefing reads fail closed on this platform")
    target = provider(env.project)
    path = member_briefing_path(slug_for_name("writer"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("NOTE_ONE", encoding="utf-8")
    await send(target, build(env, target, fresh=True))
    path.write_text("NOTE_TWO", encoding="utf-8")
    await send(target, build(env, target))
    await send(target, build(env, target))
    first, second, third = target.client.messages
    assert first.count(HEADER) == 1 and "NOTE_ONE" in first
    assert HEADER not in second and second.count("NOTE_TWO") == 1
    assert "[CURRENT ASSIGNMENT" in second
    assert "You are writer." not in second
    assert "NOTE_TWO" not in third and HEADER not in third


# ── a warm re-send leaves out what the engine already holds ────────────────

_GUIDE = "Declared guide: examples must be reproducible."


def _shared(env, backend, native):
    wire = Wire(env.project, backend)
    handle = SimpleNamespace(
        prompt=wire.stream_events,
        session_id="shared",
        cwd=str(env.project),
        served_model="",
        native_context_documents=native,
        is_turn_active=True,
        cancel=AsyncMock(),
    )
    runtime = SimpleNamespace(process_instance="runtime", acp_backend=backend)
    return AcpSessionProvider(handle, runtime), wire


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KAS, ACP_BACKEND_KIRO])
@pytest.mark.parametrize("why", ["changed", "compaction"])
async def test_a_warm_resend_blanks_the_sources_the_engine_holds(env, backend, why):
    target, wire = _shared(env, backend, {str(env.project / "declared-guide.md"): _GUIDE})
    await send(target, build(env, target, fresh=True, project=str(env.project)))
    options = {}
    if why == "changed":
        env.memory.write_preferences("PREFERENCE_CHANGED")
    else:
        wire.events = [AcpEvent(kind=EVENT_COMPACTION_STATUS, text="completed"), *_DONE]
        await send(target, build(env, target, project=str(env.project)))
        wire.events = list(_DONE)
        options = {"needs_reinjection": True}
    await send(target, build(env, target, project=str(env.project), **options))
    resend = wire.messages[-1]
    assert resend.count(HEADER) == 1
    assert _GUIDE not in resend
    assert "declared-guide.md" in resend
    assert all(_GUIDE not in message for message in wire.messages)


@pytest.mark.asyncio
async def test_a_harness_that_holds_nothing_gets_every_body_on_a_resend(env):
    target = provider(env.project, ACP_BACKEND_CLAUDE)
    await send(target, build(env, target, fresh=True))
    env.memory.write_preferences("PREFERENCE_CHANGED")
    await send(target, build(env, target))
    assert [m.count(_GUIDE) for m in target.client.messages] == [1, 1]


@pytest.mark.asyncio
async def test_a_source_changed_since_launch_goes_out_whole(env):
    target, wire = _shared(env, ACP_BACKEND_KAS, {str(env.project / "declared-guide.md"): _GUIDE})
    await send(target, build(env, target, fresh=True, project=str(env.project)))
    (env.project / "declared-guide.md").write_text("GUIDE_EDITED", encoding="utf-8")
    await send(target, build(env, target, project=str(env.project)))
    assert "GUIDE_EDITED" in wire.messages[-1]


# ── a cancel on a harness that keeps its history ───────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "keeps,events,resent",
    [(True, _CANCELLED, False), (False, _CANCELLED, True), (True, _SYNTHETIC_CANCEL, True)],
)
async def test_a_cancelled_turn_keeps_earlier_receipts_only_where_history_survives(
    keeps, events, resent
):
    delivery, wire = EssentialDelivery(), _Recorder()
    await _drain(delivery, _bind(delivery, "[N]"), wire, cancel_keeps_history=lambda: keeps)
    wire.events = events
    await _drain(delivery, _bind(delivery, "[N]"), wire, cancel_keeps_history=lambda: keeps)
    wire.events = list(_DONE)
    await _drain(delivery, _bind(delivery, "[N]"), wire, cancel_keeps_history=lambda: keeps)
    assert ("HEAD" in wire.sent[2]) is resent


@pytest.mark.asyncio
async def test_what_a_cancelled_turn_carried_is_still_sent_again():
    delivery, wire = EssentialDelivery(), _Recorder()
    wire.events = _CANCELLED
    await _drain(delivery, _bind(delivery, "[N]"), wire, cancel_keeps_history=lambda: True)
    wire.events = list(_DONE)
    await _drain(delivery, _bind(delivery, "[N]"), wire, cancel_keeps_history=lambda: True)
    assert wire.sent[1] == "before HEAD|[N]|REST after"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,resent", [(ACP_BACKEND_KAS, False), (ACP_BACKEND_KIRO, True)])
async def test_a_cancel_resends_the_snapshot_only_where_history_may_not_survive(
    env, backend, resent
):
    target, wire = _shared(env, backend, {})
    await send(target, build(env, target, fresh=True, project=str(env.project)))
    assert await target.cancel() == "acked"
    wire.events = list(_CANCELLED)
    await send(target, build(env, target, project=str(env.project)))
    wire.events = list(_DONE)
    await send(target, build(env, target, project=str(env.project)))
    assert [m.count(HEADER) for m in wire.messages] == ([1, 1, 1] if resent else [1, 0, 0])
