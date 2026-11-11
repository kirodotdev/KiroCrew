"""The per-slot sidebar-row memos give the frames a from-scratch pass gives.

``serialize_slots`` memoizes the transcript summary, the redacted title fields, the
bare-audience source-link chips and the channel-link rows of every slot
(``dashboard/slot_row_memo.py``). These tests drive one state through a sequence of
slot, link, session-map, transport and redaction changes and compare, after each
step, the memoized frames with frames built with every memo off, as JSON text. The
rest pins what invalidates each memo.
"""

from __future__ import annotations

import json
import types
from typing import Any
from unittest.mock import MagicMock

import pytest
from source_link_rows import append_created

from kiro_crew.dashboard import slot_row_memo
from kiro_crew.dashboard.chat_utils_display import _clear_display_redaction_cache
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.state_parts import slots_frame
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.session_map import SessionMap
from kiro_crew.session_parts import links as session_links

_LINK_ACCESSORS = (
    "clear_mirror_link",
    "clear_slack_link",
    "get_mirror_link",
    "get_slack_link",
    "is_mirror_paused",
    "is_slack_paused",
    "mirror_accepts_inbound",
    "mirror_link_nonce",
    "session_map_generation",
    "set_mirror_link",
    "set_mirror_paused",
    "set_slack_link",
    "set_slack_paused",
    "slack_link_nonce",
)


@pytest.fixture(autouse=True)
def _no_check_status(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    # Chips on, with a fixed status answer and no audit writes, so the three
    # audience views differ and stay deterministic.
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers._session_card_chips_snapshot", True
    )
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.state._cached_check_status",
        lambda url: {"ci": "passed", "state": "OPEN"},
    )
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.state._repo_is_public", lambda url: "public" in url
    )
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.state._audit_public_status_grant", lambda url: None
    )
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.state._audit_public_status_denied", lambda url: None
    )


@pytest.fixture(name="state")
def _state(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    sessions = MagicMock(count=0)
    sessions._session_map = SessionMap()
    # The real link accessors over a real map, so the generation is real.
    for name in _LINK_ACCESSORS:
        setattr(sessions, name, types.MethodType(getattr(session_links, name), sessions))
    state = DashboardState(
        sessions=sessions, crons=MagicMock(), lessons=MagicMock(), start_time=0.0
    )
    monkeypatch.setattr(state, "is_yolo_active", lambda: False)
    return state


class _Transport:
    def __init__(self, proactive: bool) -> None:
        self.capabilities = types.SimpleNamespace(supports_proactive_send=proactive)


def _register(state: DashboardState, channel: str, proactive: bool) -> None:
    transports: dict[str, Any] = state.channel_transports
    transports[channel] = _Transport(proactive)


def _retitle(slot: Any, title: str) -> None:
    slot.title = title
    slot._titled = True


def _frames(state: DashboardState) -> str:
    bare, dash, owner = state.serialize_slot_views(owner=True)
    return json.dumps([bare, dash, owner])


def _unmemoized_frames(state: DashboardState) -> str:
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(slots_frame, "_row_memo_context", lambda _state: None)
        return _frames(state)


def test_the_unmemoized_reference_writes_no_memo(state: DashboardState) -> None:
    slot = state.get_or_create_slot("chat-a")
    slot.append("assistant", "see https://github.com/acme/public/pull/1", "msg")
    append_created(slot, "https://github.com/acme/public/pull/1", cls="msg")
    for name in ("_row_summary_memo", "_row_redacted_memo", "_row_links_memo", "_slot_links_memo"):
        setattr(slot, name, None)
    _unmemoized_frames(state)
    assert slot._row_summary_memo is None
    assert slot._row_redacted_memo is None
    assert slot._row_links_memo is None
    assert slot._slot_links_memo is None
    _frames(state)
    assert slot._row_summary_memo is not None
    assert slot._row_redacted_memo is not None
    assert slot._row_links_memo is not None
    assert slot._slot_links_memo is not None


def _assert_same(state: DashboardState) -> None:
    memoized = _frames(state)
    # Twice: the second memoized pass is all hits.
    assert _frames(state) == memoized
    assert memoized == _unmemoized_frames(state)


def test_memoized_frames_equal_unmemoized_frames_across_mutations(
    state: DashboardState,
) -> None:
    a = state.get_or_create_slot("chat-a")
    a.append("user", "open a PR please", "msg")
    append_created(a, "https://github.com/acme/public/pull/1", cls="msg")
    a.append("assistant", "opened https://github.com/acme/public/pull/1", "msg")
    b = state.get_or_create_slot("chat-b")
    b.append("user", "which one?", "msg")
    append_created(b, "https://github.com/acme/private/pull/2", cls="msg")
    b.append("assistant", "pick https://github.com/acme/private/pull/2 [OPTIONS: yes | no]", "msg")
    c = state.get_or_create_slot("chat-c")
    c.append("user", "hello", "msg")
    _assert_same(state)

    steps: list[tuple[str, Any]] = [
        ("append", lambda: append_created(a, "https://github.com/acme/x/issues/9", cls="msg")),
        ("in-place edit", lambda: b.messages[-1].update(content="now [OPTIONS: left | right]")),
        ("title", lambda: _retitle(c, "Renamed chat")),
        ("question", lambda: setattr(c, "_question_pending", True)),
        ("running", lambda: setattr(a, "_task", MagicMock(done=lambda: False))),
        ("prompt while running", lambda: a.append("user", "another thing", "msg")),
        ("stopped", lambda: setattr(a, "_task", None)),
        (
            "credential",
            lambda: c.append("assistant", "key AKIAIOSFODNN7EXAMPLE secret", "msg"),
        ),
        (
            "mirror",
            lambda: state.sessions.set_mirror_link(
                "dashboard:chat-a", ChannelLink("discord", "123456789", None)
            ),
        ),
        ("transport", lambda: _register(state, "discord", True)),
        (
            "transport capability",
            lambda: _register(state, "discord", False),
        ),
        ("mirror paused", lambda: state.sessions.set_mirror_paused("dashboard:chat-a", True)),
        (
            "slack link",
            lambda: state.sessions.set_slack_link("dashboard:chat-b", "1700000000.000100", "C0123"),
        ),
        ("slack paused", lambda: state.sessions.set_slack_paused("dashboard:chat-b", True)),
        (
            "unrelated map write",
            lambda: state.sessions._session_map.set_flag("dashboard:zz", "pinned", True),
        ),
        ("mirror cleared", lambda: state.sessions.clear_mirror_link("dashboard:chat-a")),
        ("in-memory slack fields", lambda: setattr(c, "_slack_thread_ts", "1700000000.000200")),
        ("display caches reset", _clear_display_redaction_cache),
        ("new slot", lambda: state.get_or_create_slot("chat-d").append("user", "hi", "msg")),
        (
            "newest row replaced",
            lambda: a._messages.__setitem__(-1, dict(a._messages[-1], content="swapped reply")),
        ),
        ("slot removed", lambda: state._slots.pop("chat-b")),
    ]
    for label, step in steps:
        step()
        try:
            _assert_same(state)
        except AssertionError:
            pytest.fail(f"memoized frames diverged after step {label!r}")
        if label in ("mirror", "slack link"):
            # The link steps really reach the rows, so equality is not vacuous.
            rows = {row["key"]: row for row in state.serialize_slots()}
            assert rows["chat-a" if label == "mirror" else "chat-b"]["links"]


def _ctx() -> slot_row_memo.RowMemoContext:
    return slot_row_memo.RowMemoContext(redaction=("stamp",), links=None)


def _counting_compute(calls: list[int]) -> Any:
    def compute(rows: Any) -> slot_row_memo.RowSummary:
        calls.append(1)
        last = next(reversed(rows))
        return slot_row_memo.RowSummary(
            last.get("ts", ""), last["content"], False, "", (), "", "", "", ""
        )

    return compute


def test_row_summary_memo_reuses_until_a_read_row_changes() -> None:
    slot = types.SimpleNamespace(_row_summary_memo=None)
    rows = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "new"}]
    calls: list[int] = []
    compute = _counting_compute(calls)
    previous = slot_row_memo.ROW_MEMO.get()
    slot_row_memo.ROW_MEMO.set(_ctx())
    try:
        first = slot_row_memo.memo_row_summary(slot, rows, compute)
        assert slot_row_memo.memo_row_summary(slot, rows, compute) is first
        assert len(calls) == 1
        # A row older than the scan read cannot change the answer: still a hit.
        rows[0]["content"] = "edited"
        slot_row_memo.memo_row_summary(slot, rows, compute)
        assert len(calls) == 1
        # An in-place edit of a row the scan read misses.
        rows[-1]["content"] = "streamed"
        assert slot_row_memo.memo_row_summary(slot, rows, compute).last_msg == "streamed"
        assert len(calls) == 2
        # So does an append.
        rows.append({"role": "assistant", "content": "more"})
        assert slot_row_memo.memo_row_summary(slot, rows, compute).last_msg == "more"
        assert len(calls) == 3
    finally:
        slot_row_memo.ROW_MEMO.set(previous)
    # A new redaction stamp misses.
    previous = slot_row_memo.ROW_MEMO.get()
    slot_row_memo.ROW_MEMO.set(slot_row_memo.RowMemoContext(("other",), None))
    try:
        slot_row_memo.memo_row_summary(slot, rows, compute)
        assert len(calls) == 4
    finally:
        slot_row_memo.ROW_MEMO.set(previous)


def test_no_memo_outside_a_serialize_pass() -> None:
    slot = types.SimpleNamespace(_row_summary_memo=None, _row_redacted_memo=None)
    rows = [{"role": "assistant", "content": "x"}]
    calls: list[int] = []
    slot_row_memo.memo_row_summary(slot, rows, _counting_compute(calls))
    slot_row_memo.memo_row_summary(slot, rows, _counting_compute(calls))
    assert len(calls) == 2
    assert slot._row_summary_memo is None
    assert slot_row_memo.memo_redacted(slot, ("t",), str.upper) == ("T",)
    assert slot._row_redacted_memo is None


def test_redacted_memo_keys_on_text_and_stamp() -> None:
    slot = types.SimpleNamespace(_row_redacted_memo=None)
    seen: list[str] = []

    def redact(text: str) -> str:
        seen.append(text)
        return text.upper()

    previous = slot_row_memo.ROW_MEMO.get()
    slot_row_memo.ROW_MEMO.set(_ctx())
    try:
        assert slot_row_memo.memo_redacted(slot, ("a", "b"), redact) == ("A", "B")
        assert slot_row_memo.memo_redacted(slot, ("a", "b"), redact) == ("A", "B")
        assert seen == ["a", "b"]
        assert slot_row_memo.memo_redacted(slot, ("c", "b"), redact) == ("C", "B")
        assert seen == ["a", "b", "c", "b"]
    finally:
        slot_row_memo.ROW_MEMO.set(previous)


def test_every_link_mutator_moves_the_map_generation(state: DashboardState) -> None:
    sessions = state.sessions
    key = "dashboard:chat-a"
    mutations = [
        lambda: sessions.set_mirror_link(key, ChannelLink("discord", "42", None)),
        lambda: sessions.set_mirror_paused(key, True),
        lambda: sessions.set_mirror_paused(key, True, origin=True),
        lambda: sessions.clear_mirror_link(key),
        lambda: sessions.set_slack_link(key, "1700000000.000300", "C9"),
        lambda: sessions.set_slack_paused(key, True),
        lambda: sessions.clear_slack_link(key),
    ]
    for mutate in mutations:
        before = sessions.session_map_generation()
        mutate()
        after = sessions.session_map_generation()
        assert after[0] == before[0]
        assert after[1] > before[1]


def test_relinking_the_same_slack_thread_leaves_the_generation(state: DashboardState) -> None:
    """The Slack inbound path re-records its link every turn; that alone must not miss every memo."""
    sessions = state.sessions
    sessions.set_slack_link("dashboard:chat-a", "1700000000.000300", "C9")
    before = sessions.session_map_generation()
    sessions.set_slack_link("dashboard:chat-a", "1700000000.000300", "C9")
    assert sessions.session_map_generation() == before


def test_slot_links_memo_hits_until_an_input_moves(
    state: DashboardState, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = state.get_or_create_slot("chat-a")
    builds: list[str] = []
    real_build = slots_frame._build_slot_links

    def counting_build(self: Any, s: Any, session_key: str) -> Any:
        builds.append(session_key)
        return real_build(self, s, session_key)

    monkeypatch.setattr(slots_frame, "_build_slot_links", counting_build)
    # Creating the slot already published a frame; start from no memo.
    slot._slot_links_memo = None
    state.serialize_slots()
    state.serialize_slots()
    assert len(builds) == 1
    # A session-map write misses, even one for another key.
    state.sessions._session_map.set_flag("dashboard:other", "pinned", True)
    state.serialize_slots()
    assert len(builds) == 2
    # A transport change misses.
    _register(state, "telegram", True)
    state.serialize_slots()
    assert len(builds) == 3
    # The slot's own legacy Slack fields miss.
    slot._slack_channel = "C1"
    state.serialize_slots()
    assert len(builds) == 4
    state.serialize_slots()
    assert len(builds) == 4


def test_links_unmemoized_without_a_real_generation(state: DashboardState) -> None:
    setattr(state.sessions, "session_map_generation", MagicMock(return_value=MagicMock()))
    ctx = slots_frame._row_memo_context(state)
    assert ctx is not None
    assert ctx.links is None


def test_bare_chip_memo_follows_a_new_extraction(state: DashboardState) -> None:
    slot = state.get_or_create_slot("chat-a")
    slot.append("assistant", "see https://github.com/acme/public/pull/1", "msg")
    append_created(slot, "https://github.com/acme/public/pull/1", cls="msg")
    first = state.serialize_slots()[0]["source_links"]
    memo = slot._row_links_memo
    assert memo is not None
    assert state.serialize_slots()[0]["source_links"] == first
    assert slot._row_links_memo is memo
    slot.append("assistant", "and https://github.com/acme/public/pull/3", "msg")
    append_created(slot, "https://github.com/acme/public/pull/3", cls="msg")
    second = state.serialize_slots()[0]["source_links"]
    assert [link["number"] for link in second] == [3, 1]
    assert slot._row_links_memo is not memo


def test_summary_memo_tail_is_bounded(state: DashboardState) -> None:
    slot = state.get_or_create_slot("chat-tools")
    slot.append("user", "run the tools", "msg")
    slot.append("assistant", "starting", "msg")
    for index in range(300):
        slot.append("tool_result", f"output {index}", "msg")
    slot.append("assistant", "done", "msg")
    state.serialize_slots()
    memo = slot._row_summary_memo
    assert memo is not None
    assert len(memo[2]) <= 4


def test_summary_memo_skips_a_streaming_tail(state: DashboardState) -> None:
    # A turn still running its tools has no assistant row after the prompt, so
    # the scan reads back to the prompt: too far to keep a copy of.
    slot = state.get_or_create_slot("chat-busy")
    slot.append("user", "run the tools", "msg")
    for index in range(300):
        slot.append("tool_call", f"call {index}", "msg")
    state.serialize_slots()
    assert slot._row_summary_memo is None
    assert len(slot.messages) - 1 > slot_row_memo.MAX_MEMO_TAIL


def test_owner_view_pass_memoizes_nothing(state: DashboardState) -> None:
    from kiro_crew.security.redaction_switch import owner_view

    slot = state.get_or_create_slot("chat-owner")
    slot.append("assistant", "see https://github.com/acme/public/pull/1", "msg")
    append_created(slot, "https://github.com/acme/public/pull/1", cls="msg")
    # Creating and appending already ran unscoped passes.
    for name in ("_row_summary_memo", "_row_redacted_memo", "_row_links_memo", "_slot_links_memo"):
        setattr(slot, name, None)
    with owner_view():
        state.serialize_slots()
    assert slot._row_summary_memo is None
    assert slot._row_redacted_memo is None
    assert slot._row_links_memo is None
    assert slot._slot_links_memo is None


def test_warm_pass_recomputes_nothing(
    state: DashboardState, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard import slot_projection
    from kiro_crew.dashboard import state as state_facade

    slots = []
    for index in range(200):
        slot = state.get_or_create_slot(f"chat-{index:03d}")
        slot.append("user", f"question {index}", "msg")
        slot.append(
            "assistant", f"answer {index} https://github.com/acme/public/pull/{index}", "msg"
        )
        slots.append(slot)
    state.serialize_slots()

    counts = {"summary": 0, "links": 0, "redact": 0}

    def counting(name: str, real: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            counts[name] += 1
            return real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(
        slot_projection, "summarize_rows", counting("summary", slot_projection.summarize_rows)
    )
    monkeypatch.setattr(
        slots_frame, "_build_slot_links", counting("links", slots_frame._build_slot_links)
    )
    monkeypatch.setattr(state_facade, "_redact", counting("redact", state_facade._redact))
    state.serialize_slots()
    assert counts == {"summary": 0, "links": 0, "redact": 0}

    slots[7].append("assistant", "one more thing", "msg")
    state.serialize_slots()
    assert counts["summary"] == 1

    # The redaction counter is live: a retitle misses the title memo and redacts.
    _retitle(slots[8], "Renamed")
    state.serialize_slots()
    assert counts["redact"] >= 1


def test_summary_memo_is_not_stored_past_the_text_budget(state: DashboardState) -> None:
    # One reply ending in an [OPTIONS:] ask whose label alone is past the
    # budget: the row reads it, but the slot keeps no copy of it.
    label = "x" * (slot_row_memo.MAX_MEMO_TEXT + 1)
    slot = state.get_or_create_slot("chat-huge")
    slot.append("user", "pick one", "msg")
    slot.append("assistant", f"which? [OPTIONS: {label} | no]", "msg")
    row = next(r for r in state.serialize_slots() if r["key"] == "chat-huge")
    assert slot._row_summary_memo is None
    assert row["has_options"] is True
    assert len(row["options"]) == 2
    _assert_same(state)

    # A reply back under the budget is memoized again.
    slot.append("assistant", "short [OPTIONS: yes | no]", "msg")
    state.serialize_slots()
    assert slot._row_summary_memo is not None


def test_redacted_memo_is_not_stored_past_the_text_budget(state: DashboardState) -> None:
    slot = state.get_or_create_slot("chat-long-title")
    slot.append("user", "hello", "msg")
    _retitle(slot, "t" * (slot_row_memo.MAX_MEMO_TEXT + 1))
    row = next(r for r in state.serialize_slots() if r["key"] == "chat-long-title")
    assert slot._row_redacted_memo is None
    assert len(row["title"]) > slot_row_memo.MAX_MEMO_TEXT
    _assert_same(state)


def test_budget_walk_counts_nested_containers_and_stops_early() -> None:
    within = slot_row_memo._within_budget
    assert within(["abc", {"k": ["de"]}], 8)
    # 3 + dict 1 + key 1 + list 1 + 2 = 8; one unit less is over.
    assert not within(["abc", {"k": ["de"]}], 7)
    # A container far larger than the budget is rejected without a full walk.
    assert not within([list(range(10**6))], 10)
    # An empty string still counts one, so many of them cannot hide.
    assert within([""] * 10, 10)
    assert not within([""] * 11, 10)


def test_summary_memo_is_not_stored_for_a_meta_of_empty_strings(state: DashboardState) -> None:
    slot = state.get_or_create_slot("chat-empties")
    slot.append("user", "hi", "msg")
    slot.append(
        "assistant", "hello", "msg", meta={"payload": [""] * (slot_row_memo.MAX_MEMO_TEXT + 1)}
    )
    state.serialize_slots()
    assert slot._row_summary_memo is None
    _assert_same(state)
