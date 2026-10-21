"""The conductor context cap (``docs/request-for-change/rfc-conductor-context-cap.md``).

Section 5 of the RFC asks for a test per rule and per hold, each failing first: Rule
A, Rule B, the 300-s clock read from the previous turn's release rather than its
acquire, the damping guard, an unknown reading, a slash command, a queued reset, a
pending question, attached sub-agents, a refused teardown and the off switch; and
for the seed, its budget, its note, its snapshot and the notice row its replay
skips. The check runs through ``chat_runner._cap_conductor_context`` on a real slot
and dashboard state, against a session manager that answers like one live KAS
session; the seed through ``chat_runner._session_replay_history`` on a real
transcript; the call site through one whole dashboard turn.

Invariant: a conductor turn either runs on its old session untouched (every hold) or
starts a new one whose replay is the seed, and only a turn start ever trims.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from chat_runner_coverage_helpers import _slot, _state
from turn_harness import TurnScript, run_turn

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN, AcpEvent
from kiro_crew.context import build_session_replay
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import _RESTART_MEMORY_TAIL

KEY = "dashboard:member-kirocrew-conductor"
CONDUCTOR = "kirocrew-conductor"
#: ``meta.notice`` on the trim's row: the contract the replay and the compaction card read.
NOTICE = "context_cap"
#: The block the stand-in session-ledger reader answers with.
SNAPSHOT = "[work ledger — durable state for this session; authoritative over memory of prior cycles]\ngoal: ship"


@pytest.fixture(autouse=True)
def _forget_trims():
    # Read with a default so that, with the cap reverted, each test fails at its own
    # assertion rather than every one erroring here.
    trims = getattr(chat_runner, "_CONTEXT_CAP_TRIMS", {})
    trims.clear()
    yield
    trims.clear()


class _Provider:
    """A live KAS session's context meter."""

    harness_managed_compaction_backend: Any = "kas"

    def __init__(
        self, tokens: int, *, window: int = 1_000_000, unknown: bool = False, reports_window=True
    ) -> None:
        self.pct = tokens * 100 / window
        self.window = window if reports_window else 0
        self.unknown = unknown

    def context_usage_pct(self) -> float:
        return self.pct

    def context_usage_unknown(self) -> bool:
        return self.unknown

    def context_window_tokens(self) -> int:
        return self.window


class _Sessions:
    """The session-manager calls the cap and the replay make, for one live session.

    A plain class rather than a mock: a call the cap is not expected to make is an
    AttributeError, not a truthy answer.
    """

    def __init__(
        self, provider: _Provider | None, *, idle_secs: float = 0.0, sid: str = "sid-1"
    ) -> None:
        self.provider = provider
        self.sid = sid
        self.busy = False
        self.history_owed = False
        self.suppressed = False
        self.discards: list[tuple[str, bool, bool]] = []
        self._sessions: dict[str, Any] = {}
        self.set_idle(idle_secs)

    def set_idle(self, idle_secs: float) -> None:
        self._sessions[KEY] = SimpleNamespace(last_used=time.monotonic() - idle_secs)

    def get_provider(self, key: str) -> _Provider | None:
        return self.provider if key == KEY else None

    def mapped_sid(self, key: str) -> str:
        return self.sid

    def first_turn_history_owed_pending(self, key: str) -> bool:
        return self.history_owed

    def consume_replay_suppression(self, key: str) -> bool:
        suppressed, self.suppressed = self.suppressed, False
        return suppressed

    def consume_first_turn_history_owed(self, key: str) -> bool:
        owed, self.history_owed = self.history_owed, False
        return owed

    async def discard_conversation(
        self,
        key: str,
        *,
        replay: bool = True,
        skip_if_busy: bool = False,
        refuse_only_on_active_turn: bool = False,
    ) -> bool:
        self.discards.append((key, replay, skip_if_busy))
        if skip_if_busy and self.busy:
            return False
        self.provider, self.sid = None, ""
        return True

    def start_session(self, tokens: int, *, sid: str, idle_secs: float = 0.0) -> None:
        """The session the next turn's claim starts, as it reads at a later turn start."""
        self.provider, self.sid = _Provider(tokens), sid
        self.set_idle(idle_secs)


def _cfg(on: bool = True) -> Any:
    return SimpleNamespace(session=SimpleNamespace(context_cap=on))


def _conductor(tmp_path, sessions: _Sessions) -> tuple[Any, Any]:
    state, slot = _state(tmp_path), _slot("member-kirocrew-conductor")
    state.sessions = sessions
    slot.agent = CONDUCTOR
    return state, slot


async def _check(state, slot, *, cfg: Any = None, is_slash: bool = False) -> str:
    return await chat_runner._cap_conductor_context(
        state, slot, KEY, _cfg() if cfg is None else cfg, is_slash=is_slash
    )


def _notices(slot) -> list[dict]:
    return [m for m in slot.messages if (m.get("meta") or {}).get("notice") == NOTICE]


# ── the rules ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tokens", "idle", "rule"),
    [
        (400_001, 0.0, "A"),
        (400_000, 0.0, None),
        (400_000, None, None),
        (200_001, 300.0, "B"),
        (200_000, 300.0, None),
        (200_001, 299.9, None),
        (200_001, None, None),
        (900_000, None, "A"),
    ],
)
def test_the_rules_fire_only_past_their_marks(tokens, idle, rule):
    """Rule A past 400K whatever the cache; Rule B past 200K once 300 s have passed."""
    assert chat_runner._context_cap_rule(tokens, idle) == rule


@pytest.mark.asyncio
async def test_rule_a_trims_a_session_over_400k_at_a_turn_start(tmp_path):
    sessions = _Sessions(_Provider(412_000), idle_secs=0.0)
    state, slot = _conductor(tmp_path, sessions)

    assert await _check(state, slot) == "trimmed"

    assert sessions.discards == [(KEY, True, True)]
    [notice] = _notices(slot)
    assert notice["role"] == "assistant"
    assert notice["meta"]["kind"] == "compaction"
    assert notice["content"] == (
        "♻️ Context reached 412K tokens, so the session was restarted on a shorter context. "
        + _RESTART_MEMORY_TAIL
    )
    assert chat_runner._take_context_cap_seed(KEY) is True


@pytest.mark.asyncio
async def test_rule_b_trims_over_200k_once_the_previous_turn_ended_a_cache_life_ago(tmp_path):
    sessions = _Sessions(_Provider(250_000), idle_secs=3_600.0)
    state, slot = _conductor(tmp_path, sessions)

    assert await _check(state, slot) == "trimmed"
    assert sessions.discards == [(KEY, True, True)]


@pytest.mark.asyncio
async def test_a_warm_session_between_the_marks_is_left_alone(tmp_path):
    sessions = _Sessions(_Provider(250_000), idle_secs=0.0)
    state, slot = _conductor(tmp_path, sessions)

    assert await _check(state, slot) == "below_marks"
    assert sessions.discards == [] and _notices(slot) == []


@pytest.mark.asyncio
async def test_the_clock_is_the_previous_turns_release_not_its_acquire(tmp_path):
    """A long turn that ended a minute ago is warm, however long ago it acquired."""
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.session import SessionManager, _Session

    mgr = SessionManager(KiroCrewConfig(), provider_factory=MagicMock())
    record = _Session(provider=_Provider(250_000))
    mgr._sessions[mgr._fold_key(KEY)] = record
    state, slot = _state(tmp_path), _slot("member-kirocrew-conductor")
    state.sessions = mgr
    slot.agent = CONDUCTOR
    # Held so a fired rule stops at a hold instead of tearing the session down.
    slot._pending_discard_conversation_key = KEY
    # The turn acquired eleven minutes ago and is still running.
    await record.semaphore.acquire()
    record.last_used = time.monotonic() - 660.0
    assert await _check(state, slot) == "reset_queued"

    mgr.release(KEY)

    assert await _check(state, slot) == "below_marks"


# ── the damping guard ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_trimmed_session_is_not_trimmed_again_until_it_grows_50k_past_its_first_reading(
    tmp_path,
):
    sessions = _Sessions(_Provider(412_000))
    state, slot = _conductor(tmp_path, sessions)
    assert await _check(state, slot) == "trimmed"
    # The trim landed high: Rule B fires at the first cold turn start on the new session.
    sessions.start_session(215_000, sid="sid-2", idle_secs=3_600.0)
    assert await _check(state, slot) == "damped"

    sessions.start_session(264_999, sid="sid-2", idle_secs=3_600.0)
    assert await _check(state, slot) == "damped"

    sessions.start_session(265_000, sid="sid-2", idle_secs=3_600.0)
    assert await _check(state, slot) == "trimmed"
    assert len(sessions.discards) == 2


@pytest.mark.asyncio
async def test_damping_ends_with_the_session_the_trim_started(tmp_path):
    sessions = _Sessions(_Provider(412_000))
    state, slot = _conductor(tmp_path, sessions)
    assert await _check(state, slot) == "trimmed"
    sessions.start_session(150_000, sid="sid-2")
    assert await _check(state, slot) == "below_marks"
    # A reset or a recycle replaced that session: nothing a trim started is left.
    sessions.start_session(215_000, sid="sid-3", idle_secs=3_600.0)

    assert await _check(state, slot) == "trimmed"


# ── the holds ───────────────────────────────────────────────────────────────


def _unknown(sessions, state, slot) -> None:
    sessions.provider = _Provider(412_000, unknown=True)


def _zero(sessions, state, slot) -> None:
    sessions.provider = _Provider(0)


def _no_window(sessions, state, slot) -> None:
    sessions.provider = _Provider(412_000, reports_window=False)


def _no_session(sessions, state, slot) -> None:
    sessions.provider = None


def _not_kas(sessions, state, slot) -> None:
    sessions.provider.harness_managed_compaction_backend = None


def _not_conductor(sessions, state, slot) -> None:
    slot.agent = "kirocrew"


def _reset_queued(sessions, state, slot) -> None:
    slot._pending_discard_conversation_key = KEY


def _question_pending(sessions, state, slot) -> None:
    slot._question_pending = {"q-1": object()}


def _approval_pending(sessions, state, slot) -> None:
    slot._approval_futures = {"req-1": MagicMock(done=MagicMock(return_value=False))}


def _subagents_attached(sessions, state, slot) -> None:
    state.subagents = MagicMock(running_agents_for=MagicMock(return_value=["child-1"]))


@pytest.mark.parametrize(
    ("arrange", "hold"),
    [
        (_unknown, "unknown_reading"),
        (_zero, "unknown_reading"),
        (_no_window, "unknown_reading"),
        (_no_session, "no_session"),
        (_not_kas, "out_of_scope"),
        (_not_conductor, "out_of_scope"),
        (_reset_queued, "reset_queued"),
        (_question_pending, "interaction_pending"),
        (_approval_pending, "interaction_pending"),
        (_subagents_attached, "subagents_attached"),
    ],
    ids=lambda value: getattr(value, "__name__", value),
)
@pytest.mark.asyncio
async def test_a_hold_leaves_the_session_in_place(tmp_path, arrange, hold):
    sessions = _Sessions(_Provider(412_000))
    state, slot = _conductor(tmp_path, sessions)
    arrange(sessions, state, slot)

    assert await _check(state, slot) == hold

    assert sessions.discards == []
    assert _notices(slot) == []
    assert chat_runner._take_context_cap_seed(KEY) is False


@pytest.mark.asyncio
async def test_a_slash_command_is_never_trimmed(tmp_path):
    sessions = _Sessions(_Provider(412_000))
    state, slot = _conductor(tmp_path, sessions)

    assert await _check(state, slot, is_slash=True) == "slash_command"
    assert sessions.discards == []


@pytest.mark.asyncio
async def test_a_refused_teardown_changes_nothing(tmp_path):
    """Another turn holds the session: the discard refuses under its lock."""
    sessions = _Sessions(_Provider(412_000))
    sessions.busy = True
    state, slot = _conductor(tmp_path, sessions)

    assert await _check(state, slot) == "busy"

    assert sessions.discards == [(KEY, True, True)]
    assert sessions.provider is not None
    assert _notices(slot) == []
    assert chat_runner._take_context_cap_seed(KEY) is False


@pytest.mark.parametrize("cfg", [_cfg(on=False), None], ids=["off", "unreadable"])
@pytest.mark.asyncio
async def test_the_off_switch_leaves_every_session_alone(tmp_path, cfg):
    sessions = _Sessions(_Provider(900_000), idle_secs=3_600.0)
    state, slot = _conductor(tmp_path, sessions)

    result = await chat_runner._cap_conductor_context(state, slot, KEY, cfg, is_slash=False)

    assert result == "cap_off"
    assert sessions.discards == [] and _notices(slot) == []


def test_the_cap_is_on_by_default_and_config_json_turns_it_off():
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.config.loader_builders import _build_session_config

    assert getattr(KiroCrewConfig().session, "context_cap", None) is True
    assert _build_session_config({}).context_cap is True
    assert _build_session_config({"context_cap": False}).context_cap is False


def test_the_setting_is_published_with_its_default():
    """The settings registry the dashboard renders lists the key, on by default."""
    baseline = Path(__file__).resolve().parents[1] / "config-baseline.json"
    entries = {e["path"]: e for e in json.loads(baseline.read_text(encoding="utf-8"))["entries"]}

    assert entries["session"]["defaultValue"].get("context_cap") is True
    assert entries.get("session.context_cap", {}).get("defaultValue") is True
    assert entries["session.context_cap"]["type"] == "boolean"


# ── the seed ────────────────────────────────────────────────────────────────


def _transcript(state, rows: int = 700, size: int = 1_000) -> None:
    for i in range(rows):
        role = "user" if i % 2 == 0 else "assistant"
        state.conversation_log.append(KEY, role, f"{i:04d} " + "x" * size)


async def _replay(state, slot, provider: _Provider | None = None) -> str:
    return await chat_runner._session_replay_history(
        state,
        SimpleNamespace(conversation_log=state.conversation_log),
        slot,
        provider or _Provider(150_000),
        KEY,
        _context_is_new=True,
        _provider_has_history=False,
        _current_replay_message=None,
    )


@pytest.fixture
def snapshot_reads(monkeypatch) -> list[str]:
    from kiro_crew import session_ledger

    reads: list[str] = []

    def _render(key: str) -> str:
        reads.append(key)
        return SNAPSHOT

    monkeypatch.setattr(session_ledger, "render_snapshot", _render)
    return reads


@pytest.mark.asyncio
async def test_the_trimmed_cold_start_replays_the_note_the_snapshot_and_a_270k_tail(
    tmp_path, snapshot_reads
):
    sessions = _Sessions(_Provider(412_000))
    state, slot = _conductor(tmp_path, sessions)
    _transcript(state)
    assert await _check(state, slot) == "trimmed"

    seed = await _replay(state, slot)

    head = chat_runner.CONTEXT_CAP_TRIM_NOTE + "\n\n" + SNAPSHOT + "\n\n"
    assert seed.startswith(head)
    assert snapshot_reads == ["member-kirocrew-conductor"]
    tail = seed[len(head) :]
    assert 260_000 < len(tail) <= 270_000
    assert tail.endswith("0699 " + "x" * 1_000)
    assert "search_chat_history" in chat_runner.CONTEXT_CAP_TRIM_NOTE


@pytest.mark.asyncio
async def test_every_other_cold_start_keeps_the_80k_replay(tmp_path, snapshot_reads):
    sessions = _Sessions(_Provider(150_000))
    state, slot = _conductor(tmp_path, sessions)
    _transcript(state)

    replay = await _replay(state, slot)

    assert not replay.startswith(chat_runner.CONTEXT_CAP_TRIM_NOTE)
    assert 70_000 < len(replay) <= 80_000
    assert snapshot_reads == []


def test_the_replay_budget_is_the_one_asked_for(tmp_path):
    state = _state(tmp_path)
    _transcript(state)

    default = build_session_replay(state.conversation_log, KEY)
    trimmed = build_session_replay(state.conversation_log, KEY, budget_chars=270_000)

    assert len(default) <= 80_000 < len(trimmed) <= 270_000


@pytest.mark.asyncio
async def test_the_trim_notice_row_is_not_replayed(tmp_path, snapshot_reads):
    sessions = _Sessions(_Provider(412_000))
    state, slot = _conductor(tmp_path, sessions)
    _transcript(state, rows=4, size=10)
    assert await _check(state, slot) == "trimmed"
    [notice] = _notices(slot)
    state.conversation_log.append(KEY, "assistant", notice["content"], extra_meta=notice["meta"])
    state.conversation_log.append(KEY, "user", "after the trim")

    seed = await _replay(state, slot)

    assert "after the trim" in seed
    assert "Context reached" not in seed
    later = build_session_replay(state.conversation_log, KEY)
    assert later is not None and "after the trim" in later
    assert "Context reached" not in later


@pytest.mark.asyncio
async def test_the_seed_outlives_a_first_turn_that_did_not_deliver_it(tmp_path, snapshot_reads):
    """A cold start that fails before the provider keeps its prompt is rebuilt from the seed."""
    sessions = _Sessions(_Provider(412_000))
    state, slot = _conductor(tmp_path, sessions)
    _transcript(state, rows=4, size=10)
    assert await _check(state, slot) == "trimmed"
    assert (await _replay(state, slot)).startswith(chat_runner.CONTEXT_CAP_TRIM_NOTE)
    # The first turn died before the provider kept its history; the session is live.
    sessions.start_session(0, sid="sid-2")
    sessions.history_owed = True
    await _check(state, slot)
    assert (await _replay(state, slot)).startswith(chat_runner.CONTEXT_CAP_TRIM_NOTE)
    # Delivered: the next turn start spends the seed, and a later cold start is ordinary.
    sessions.history_owed = False
    await _check(state, slot)

    assert not (await _replay(state, slot)).startswith(chat_runner.CONTEXT_CAP_TRIM_NOTE)


@pytest.mark.asyncio
async def test_the_seed_outlives_a_session_that_died_before_delivering_it(tmp_path, snapshot_reads):
    """KAS died before keeping the seeded prompt, and the recovery reset removed it."""
    sessions = _Sessions(_Provider(412_000))
    state, slot = _conductor(tmp_path, sessions)
    _transcript(state, rows=4, size=10)
    assert await _check(state, slot) == "trimmed"
    assert (await _replay(state, slot)).startswith(chat_runner.CONTEXT_CAP_TRIM_NOTE)
    # The recovery turn starts with no session: nothing shows a provider kept the seed.
    sessions.provider = None
    assert await _check(state, slot) == "no_session"

    assert (await _replay(state, slot)).startswith(chat_runner.CONTEXT_CAP_TRIM_NOTE)


class _Debt:
    """The session manager's FRESH first-turn history debt, tracked for real."""

    def __init__(self) -> None:
        self.owed = False

    def first_turn_history_owed_pending(self, _key: str) -> bool:
        return self.owed

    def mark_first_turn_history_owed(self, _key: str) -> bool:
        self.owed = True
        return True

    def consume_first_turn_history_owed(self, _key: str) -> bool:
        owed, self.owed = self.owed, False
        return owed

    def provider_switch_replay_pending(self, _key: str) -> bool:
        return False

    def consume_replay_suppression(self, _key: str) -> bool:
        return False


async def _seeded_first_turn(tmp_path, monkeypatch, events) -> tuple[Any, str]:
    """One whole ``_run_chat`` first turn on a cold start a trim began; returns its prompt."""
    from recovery_l1_chat_runner_helpers import _l1_state, _RecordingSlot

    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.providers.base import LLMEvent

    state, client = _l1_state(tmp_path)
    client.last_infra_error = None
    debt = _Debt()
    for name in (
        "first_turn_history_owed_pending",
        "mark_first_turn_history_owed",
        "consume_first_turn_history_owed",
        "provider_switch_replay_pending",
        "consume_replay_suppression",
    ):
        setattr(state.sessions, name, getattr(debt, name))
    monkeypatch.setattr("kiro_crew.context.build_session_replay", lambda *a, **k: "User: earlier")

    def _stream(*_a, **_kw):
        async def _gen():
            for kind, payload in events:
                if kind == "raise":
                    raise payload
                yield LLMEvent(kind=kind, **payload)

        return _gen()

    client.stream = MagicMock(side_effect=_stream)
    slot = _RecordingSlot("chat-cap-seed")
    key = effective_session_key(slot)
    chat_runner._remember_context_cap_trim(key)
    with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
        mock_sel.return_value = MagicMock()
        await _run_chat(state, slot, "hello")
        if slot.task:
            # flake-ok: awaits the turn task _run_chat itself started, on a scripted client that never blocks
            await slot.task
    built = state.context_builder.build_message.call_args_list[0]
    return chat_runner._CONTEXT_CAP_TRIMS[key], built.kwargs["compressed_history"]


@pytest.mark.asyncio
async def test_a_landed_first_turn_spends_the_seed_it_carried(
    tmp_path, monkeypatch, snapshot_reads
):
    trim, prompt = await _seeded_first_turn(
        tmp_path,
        monkeypatch,
        [
            (EVENT_TEXT_CHUNK, {"text": "done"}),
            (EVENT_COMPLETE, {"stop_reason": STOP_REASON_END_TURN}),
        ],
    )

    assert prompt.startswith(chat_runner.CONTEXT_CAP_TRIM_NOTE)
    assert prompt.endswith("User: earlier")
    assert trim.seed_pending is False


@pytest.mark.asyncio
async def test_a_first_turn_that_fails_before_output_keeps_the_seed(
    tmp_path, monkeypatch, snapshot_reads
):
    from kiro_crew.acp.client import AcpError

    trim, prompt = await _seeded_first_turn(
        tmp_path, monkeypatch, [("raise", AcpError("the backend refused the prompt"))]
    )

    assert prompt.startswith(chat_runner.CONTEXT_CAP_TRIM_NOTE)
    assert trim.seeded is True and trim.seed_pending is True


@pytest.mark.asyncio
async def test_a_reset_that_forgets_drops_the_seed(tmp_path, snapshot_reads):
    sessions = _Sessions(_Provider(412_000))
    state, slot = _conductor(tmp_path, sessions)
    _transcript(state, rows=4, size=10)
    assert await _check(state, slot) == "trimmed"
    sessions.suppressed = True
    assert await _replay(state, slot) == ""

    assert not (await _replay(state, slot)).startswith(chat_runner.CONTEXT_CAP_TRIM_NOTE)


# ── the call site ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_dashboard_turn_checks_the_cap_before_it_claims_its_session(monkeypatch):
    seen: list[tuple[str, bool, bool, int]] = []

    async def _record(state, slot, session_key, cfg, *, is_slash):
        seen.append(
            (session_key, is_slash, cfg is not None, state.sessions.get_or_create.call_count)
        )
        return "out_of_scope"

    monkeypatch.setattr(chat_runner, "_cap_conductor_context", _record, raising=False)

    record = await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )
    )

    assert seen == [("dashboard:chat-1", False, True, 0)]
    assert record.stop_reason == STOP_REASON_END_TURN
