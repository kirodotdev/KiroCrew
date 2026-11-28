"""``chat_done`` reports whether every turn and steer in a run was automation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from steer_requeue_helpers import _running_slot
from turn_harness import Do, SlotSpec, TurnScript, run_turn

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN, AcpEvent
from kiro_crew.apps.chat_slot_sdk import ChatSlotSDK
from kiro_crew.dashboard.chat_delivery import (
    SOUND_AUTOMATION_META_KEY,
    STEER_STEERED,
    TURN_ACTOR_META_KEY,
    steer_into_running_turn,
)
from kiro_crew.dashboard.chat_turn.queue_cycle import SUBAGENT_SYNTHESIS_PROMPT
from kiro_crew.dashboard.chat_utils import CRON_NOTIFICATION_KIND

_REPLY = AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)


def _chat_done_frame(record) -> dict:
    [frame] = [frame.payload for frame in record.ws_frames if frame.kind == "chat_done"]
    return frame


def _done_frame(record) -> dict:
    frame = _chat_done_frame(record)
    assert "automation_started" in frame
    return frame


def _turn(
    *,
    actor: str = "",
    message: str = "[automation wake]",
    user_row: bool = False,
    run_kwargs: dict | None = None,
    setup=None,
    then: TurnScript | None = None,
) -> TurnScript:
    kwargs = {"_turn_actor": actor, **(run_kwargs or {})}
    return TurnScript(
        message=message,
        user_row=user_row,
        run_kwargs=kwargs,
        events=[_REPLY, _DONE],
        setup=setup,
        then=then,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "actor", "run_kwargs"),
    [
        ("cron notification", "cron", {}),
        ("monitor or auto-nudge wake", "autonudge", {}),
        ("unsteered work-ledger wake", "", {"_directive_self_wake": True}),
        ("worker report to its conductor", "gateway", {"_sound_automation": True}),
        ("app turn", "app", {}),
    ],
)
async def test_an_automation_actor_finishes_quietly(source, actor, run_kwargs):
    record = await run_turn(_turn(actor=actor, message=f"[{source}]", run_kwargs=run_kwargs))
    assert _done_frame(record)["automation_started"] is True


@pytest.mark.asyncio
async def test_an_app_hidden_turn_uses_its_actor_when_the_sound_hint_is_omitted(monkeypatch):
    from kiro_crew.dashboard import chat as chat_mod
    from kiro_crew.dashboard import chat_handlers, mate_welcome, turn_dispatch

    captured: dict = {}

    def _capture(_state, _slot, _prompt, **kwargs):
        captured.update(kwargs)
        return object()

    with monkeypatch.context() as hidden:
        hidden.setattr(chat_mod, "_run_chat", _capture)
        hidden.setattr(chat_handlers, "_sweep_stale_permissions", lambda _slot: None)
        hidden.setattr(turn_dispatch, "spawn_guarded_turn", lambda _state, _slot, turn: turn)
        hidden.setattr(mate_welcome, "app_is_stopping", lambda _state: False)

        class State:
            def run_background_turn(self, _slot, turn):
                return turn

            def push_slots_update(self) -> None:
                return None

        slot = SimpleNamespace(key="member-app", _human_seen=False)
        assert mate_welcome.dispatch_hidden_turn(
            State(), slot, "[App wake]", human_seen=False, actor="app"
        )

    assert captured["_sound_automation"] is None
    record = await run_turn(
        _turn(
            actor=captured["_turn_actor"],
            run_kwargs={"_sound_automation": captured["_sound_automation"]},
        )
    )
    assert _done_frame(record)["automation_started"] is True


_APP = "slot-probe"
_APP_KEY = f"{_APP}-job-1"

#: An App Kit chat send: who sent it, and whether its run finishes quietly.
_APP_SENDS = pytest.mark.parametrize(
    ("user_origin", "typed", "expected"),
    [(True, True, False), (True, False, False), (False, True, True)],
    ids=["persons-typed-reply", "persons-composed-reply", "apps-own-send"],
)


def _app_send_kwargs(monkeypatch, *, user_origin: bool, typed: bool) -> dict:
    """The ``_run_chat`` keywords ``ctx.chat_slots.send`` starts an idle slot's turn with."""
    from kiro_crew.dashboard import chat_runner, turn_dispatch

    captured: dict = {}

    def _capture(_state, _slot, _message, **kwargs):
        captured.update(kwargs)
        return object()

    slot = SimpleNamespace(key=_APP_KEY, _app=_APP, running=False, task=None)
    state = SimpleNamespace(get_slot={_APP_KEY: slot}.get, push_slots_update=lambda: None)
    with monkeypatch.context() as direct:
        direct.setattr(chat_runner, "_run_chat", _capture)
        direct.setattr(turn_dispatch, "spawn_guarded_turn", lambda _state, _slot, turn: turn)
        ChatSlotSDK(_APP).send(
            state, slot, "my reply", append_user=False, user_origin=user_origin, typed=typed
        )
    return captured


@pytest.mark.asyncio
@_APP_SENDS
async def test_an_app_chat_send_chimes_when_a_person_sent_it(
    monkeypatch, user_origin, typed, expected
):
    kwargs = _app_send_kwargs(monkeypatch, user_origin=user_origin, typed=typed)
    # Sound only: the actor and the person's authority stay what they were.
    assert kwargs["_turn_actor"] == "app"
    assert kwargs["_directive_user_origin"] is user_origin
    record = await run_turn(
        _turn(
            actor=kwargs["_turn_actor"],
            run_kwargs={
                "_directive_user_origin": kwargs["_directive_user_origin"],
                "_message_is_typed": kwargs["_message_is_typed"],
                "_sound_automation": kwargs.get("_sound_automation"),
            },
        )
    )
    assert _done_frame(record)["automation_started"] is expected


@pytest.mark.asyncio
@_APP_SENDS
async def test_an_app_chat_send_queued_behind_a_turn_chimes_when_a_person_sent_it(
    user_origin, typed, expected
):
    def _send_while_running(ctx) -> None:
        assert ctx.slot.running
        queued = ChatSlotSDK(_APP).send(
            ctx.state, ctx.slot, "my reply", user_origin=user_origin, typed=typed
        )
        assert queued is None

    record = await run_turn(
        TurnScript(
            message="[app wake]",
            user_row=False,
            run_kwargs={"_turn_actor": "app"},
            events=[Do(_send_while_running), _REPLY, _DONE],
            then=_turn(actor="app", run_kwargs={"_sound_automation": expected}),
        ),
        slot=SlotSpec(key=_APP_KEY, app=_APP),
    )
    [successor] = record.successors
    assert successor.args[0] == "my reply"
    assert successor.kwargs["_turn_actor"] == "app"
    assert successor.kwargs["_directive_user_origin"] is (user_origin and typed)
    assert successor.kwargs["_sound_automation"] is expected
    assert _done_frame(record.then)["automation_started"] is expected


@pytest.mark.asyncio
async def test_a_gateway_kickoff_still_chimes():
    record = await run_turn(_turn(actor="gateway", message="[Goal setup]"))
    assert _done_frame(record)["automation_started"] is False


class _Child:
    """Pending work that keeps a run open until its synthesis turn."""

    def __init__(self) -> None:
        self.pending = False

    def arm(self, ctx) -> None:
        self.pending = True
        ctx.state.workflow_service = SimpleNamespace(
            registry=SimpleNamespace(has_pending_work_for=lambda _key: self.pending)
        )

    def finish(self, _ctx) -> None:
        self.pending = False


@pytest.mark.asyncio
async def test_a_subagent_completion_and_its_synthesis_turn_finish_quietly():
    child = _Child()
    record = await run_turn(
        _turn(
            actor="subagent",
            message="[Subagent completion event]",
            setup=child.arm,
            then=_turn(
                actor="subagent",
                message=SUBAGENT_SYNTHESIS_PROMPT,
                run_kwargs={"_synthetic_payload": True},
                setup=child.finish,
            ),
        )
    )
    first = _chat_done_frame(record)
    assert first["continuing"] is True
    assert "automation_started" not in first
    assert _done_frame(record.then)["automation_started"] is True


@pytest.mark.asyncio
async def test_a_peer_session_send_runs_directly_without_a_chime():
    record = await run_turn(_turn(run_kwargs={"_sound_automation": True}))
    assert _done_frame(record)["automation_started"] is True


def _queue_batch(ctx, kinds: tuple[str, ...]) -> None:
    for index, kind in enumerate(kinds):
        meta = {}
        directive_user_origin = False
        if kind == "peer":
            meta[SOUND_AUTOMATION_META_KEY] = True
        elif kind == "cron":
            meta[TURN_ACTOR_META_KEY] = "cron"
        elif kind == "person":
            directive_user_origin = True
        else:  # pragma: no cover - test data is closed above
            raise AssertionError(kind)
        ctx.slot.queue_append(
            f"{kind} queued message {index}",
            meta=meta,
            directive_user_origin=directive_user_origin,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kinds", "expected", "merged"),
    [
        (("peer", "peer"), True, True),
        (("cron", "cron"), True, True),
        (("person", "peer"), False, True),
        (("person", "cron"), False, False),
    ],
    ids=["peer-only", "cron-only", "person-and-peer", "person-and-cron"],
)
async def test_a_merged_batch_is_quiet_only_when_every_entry_is_automation(kinds, expected, merged):
    def _queue(ctx) -> None:
        _queue_batch(ctx, kinds)

    record = await run_turn(
        _turn(
            actor="cron",
            setup=_queue,
            then=_turn(run_kwargs={"_sound_automation": expected}),
        ),
        config={"dashboard": {"merge_queued_messages": True}},
    )
    [successor] = record.successors
    assert successor.args[0].startswith("[2 queued messages merged]") is merged
    assert successor.kwargs.get("_sound_automation", False) is expected
    completed = (
        record.then if merged else await run_turn(_turn(run_kwargs={"_sound_automation": expected}))
    )
    assert _done_frame(completed)["automation_started"] is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "actor", "kind", "edited"),
    [
        ("cron-edited", "cron", CRON_NOTIFICATION_KIND, True),
        ("crew-edited", "crew", "", True),
        ("cron-untouched", "cron", CRON_NOTIFICATION_KIND, False),
        ("crew-untouched", "crew", "", False),
    ],
)
async def test_a_persons_queue_edit_overrides_an_automation_actor_for_sound(
    source, actor, kind, edited
):
    expected = not edited

    def _queue(ctx) -> None:
        meta = {TURN_ACTOR_META_KEY: actor} if not kind else None
        qid = ctx.slot.queue_append(f"{source} words", kind=kind, meta=meta)
        if edited:
            assert ctx.slot.queue_edit_by_id(
                qid,
                "the person's rewrite",
                directive_user_origin=True,
            )

    record = await run_turn(
        _turn(
            actor="cron",
            setup=_queue,
            then=_turn(actor=actor, run_kwargs={"_sound_automation": expected}),
        )
    )
    [successor] = record.successors
    assert successor.kwargs["_turn_actor"] == actor
    assert successor.kwargs["_sound_automation"] is expected
    assert _done_frame(record.then)["automation_started"] is expected


@pytest.mark.asyncio
async def test_an_explicit_mixed_queue_verdict_overrides_its_automation_actor():
    record = await run_turn(_turn(actor="cron", run_kwargs={"_sound_automation": False}))
    assert _done_frame(record)["automation_started"] is False


@pytest.mark.asyncio
async def test_a_persons_message_chimes():
    record = await run_turn(_turn(message="hello", user_row=True))
    assert _done_frame(record)["automation_started"] is False


@pytest.mark.asyncio
async def test_prompts_get_from_a_person_chimes(monkeypatch):
    from kiro_crew.dashboard import chat_runner

    monkeypatch.setattr(
        chat_runner,
        "_resolve_prompt_mention",
        lambda *_args, **_kwargs: ("expanded prompt", "ok", ""),
    )
    record = await run_turn(
        _turn(message="/prompts get demo", user_row=True, run_kwargs={"_turn_actor": ""})
    )
    assert _done_frame(record)["automation_started"] is False


@pytest.mark.asyncio
async def test_prompts_get_from_a_dashboard_card_chimes(monkeypatch):
    from kiro_crew.dashboard import chat_runner

    monkeypatch.setattr(
        chat_runner,
        "_resolve_prompt_mention",
        lambda *_args, **_kwargs: ("expanded prompt", "ok", ""),
    )
    record = await run_turn(
        _turn(
            message="/prompts get demo",
            run_kwargs={"_card_origin": True},
        )
    )
    assert _done_frame(record)["automation_started"] is False


@pytest.mark.asyncio
async def test_a_persons_queued_local_command_after_a_cron_turn_chimes(monkeypatch):
    from kiro_crew.dashboard import chat_runner

    # Nothing to list: `/prompts` answers locally and ends at the slash gate.
    monkeypatch.setattr(chat_runner, "_list_aim_prompts", lambda _project_dir: [])

    def _queue(ctx) -> None:
        ctx.slot.queue_append("/prompts", directive_user_origin=True)

    record = await run_turn(
        _turn(
            actor="cron",
            setup=_queue,
            then=_turn(
                message="/prompts",
                run_kwargs={"_directive_user_origin": True, "_sound_automation": False},
            ),
        )
    )
    [successor] = record.successors
    assert successor.args[0] == "/prompts"
    assert successor.kwargs["_sound_automation"] is False
    assert not record.then.provider_calls
    assert _done_frame(record.then)["automation_started"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("then_kwargs", "expected"),
    [
        ({"message": "hello", "user_row": True}, False),
        ({"actor": "cron"}, True),
    ],
    ids=["person", "cron"],
)
async def test_a_turn_refused_at_admission_keeps_its_own_sound_verdict(
    monkeypatch, then_kwargs, expected
):
    from kiro_crew.slack.code_channels import binding

    child = _Child()

    async def _worktree_not_ready(_state, _slot) -> bool:
        return False

    def _refuse_at_admission(ctx) -> None:
        child.finish(ctx)
        monkeypatch.setattr(binding, "notify_turn_prepare", _worktree_not_ready)

    record = await run_turn(
        _turn(
            actor="cron",
            setup=child.arm,
            then=_turn(setup=_refuse_at_admission, **then_kwargs),
        )
    )
    assert _chat_done_frame(record)["continuing"] is True
    assert not record.then.provider_calls
    assert _done_frame(record.then)["automation_started"] is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "run_kwargs"),
    [
        ("dashboard-card Resume", {"_card_origin": True, "_synthetic_recovery_turn": True}),
        ("restored message", {"_turn_provenance_restored": True}),
        ("Continue", {"_synthetic_recovery_turn": True}),
    ],
)
async def test_a_persons_resume_restored_send_and_continue_chime(name, run_kwargs):
    record = await run_turn(_turn(message=name, run_kwargs=run_kwargs))
    assert _done_frame(record)["automation_started"] is False


@pytest.mark.asyncio
async def test_a_run_a_person_opened_stays_theirs_when_a_subagent_ends_it():
    child = _Child()
    record = await run_turn(
        _turn(
            message="spawn the workers",
            user_row=True,
            setup=child.arm,
            then=_turn(actor="subagent", setup=child.finish),
        )
    )
    first = _chat_done_frame(record)
    assert first["continuing"] is True
    assert "automation_started" not in first
    assert _done_frame(record.then)["automation_started"] is False


class TestSteer:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"automation_origin": True},
            {"nudge_meta": {"cycle": 1, "loop_id": "loop-1"}},
            {"automation_origin": True},
        ],
        ids=["peer", "work-ledger", "cron-script"],
    )
    async def test_automation_steer_keeps_an_automation_run_quiet(
        self, tmp_path, monkeypatch, kwargs
    ):
        slot = await self._steer(tmp_path, monkeypatch, **kwargs)
        assert slot._run_automation_started is True

    @pytest.mark.asyncio
    async def test_a_persons_steer_makes_an_automation_run_chime(self, tmp_path, monkeypatch):
        slot = await self._steer(tmp_path, monkeypatch, user_origin=True)
        assert slot._run_automation_started is False

    @staticmethod
    async def _steer(tmp_path, monkeypatch, **kwargs):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        slot._run_automation_started = True
        client = MagicMock()
        client.supports_steer = True
        client.steer = AsyncMock(return_value=True)
        slot._acp_client = client
        outcome = await steer_into_running_turn(
            state, slot, "and the tests?", admission={}, **kwargs
        )
        assert outcome == STEER_STEERED
        return slot


def test_restore_drops_a_quiet_mark_but_keeps_a_chime_override():
    from kiro_crew.dashboard.slot_queue_repository import sanitize_restored_queue

    restored_true, restored_false = sanitize_restored_queue(
        [
            {
                "id": "q-true",
                "content": "untrusted quiet mark",
                "meta": {SOUND_AUTOMATION_META_KEY: True},
            },
            {
                "id": "q-false",
                "content": "safe chime override",
                "meta": {SOUND_AUTOMATION_META_KEY: False},
            },
        ]
    )
    assert SOUND_AUTOMATION_META_KEY not in restored_true.get("meta", {})
    assert restored_false["meta"][SOUND_AUTOMATION_META_KEY] is False


@pytest.mark.asyncio
async def test_the_next_run_starts_clean():
    record = await run_turn(_turn(actor="cron", then=_turn(message="hello")))
    assert _done_frame(record)["automation_started"] is True
    assert _done_frame(record.then)["automation_started"] is False
