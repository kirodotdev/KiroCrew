"""One conversation, one session — the guarantees a channel tab must hold.

Each test here maps to an acceptance criterion for making a channel-born
conversation's dashboard tab BE that conversation rather than a copy of it.
Every one fails before the change: the tab used to run a separate session and
write a separate transcript.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.channel_slots import (
    channel_slot_name,
    refresh_channel_window,
    surface_channel_session,
)
from kiro_crew.dashboard.chat_utils import (
    dashboard_slot_key,
    effective_session_key,
    slot_transcript_key,
    subagent_event_slot,
)
from kiro_crew.history import ConversationLog, _safe_key
from kiro_crew.session_surface import has_dashboard_surface, set_dashboard_surfaced

SLACK_KEY = "slack:1785370133.085469"
SLACK_STEM = "slack_1785370133.085469"
DISCORD_KEY = "discord:kirocrew:direct:123456"
DISCORD_STEM = "discord_kirocrew_direct_123456"


def _write_transcript(
    dir_path: Path, stem: str, messages: list[dict], meta: dict | None = None
) -> Path:
    """Write a session JSONL the way the history layer would."""
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / f"{stem}.jsonl"
    lines = [json.dumps({"_type": "metadata", "created_at": "2026-08-01T10:00:00", **(meta or {})})]
    lines += [json.dumps(m) for m in messages]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _turns(n: int, *, start: int = 0) -> list[dict]:
    return [
        {
            "role": "user" if (i % 2 == 0) else "assistant",
            "content": f"m{i}",
            "ts": f"2026-08-01T10:{i // 60:02d}:{i % 60:02d}",
        }
        for i in range(start, start + n)
    ]


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.history.config_dir", lambda: tmp_path)
    return ConversationLog()


@pytest.fixture
def state(tmp_path, log):
    """A dashboard state real enough to hold slots and answer key questions."""
    from kiro_crew.dashboard.state import DashboardState

    st = DashboardState.__new__(DashboardState)
    st._slots = {}
    st.conversation_log = log
    st.sessions = MagicMock()
    st.sessions.channel_key_for_stem.side_effect = lambda stem: (
        SLACK_KEY if stem == SLACK_STEM else DISCORD_KEY if stem == DISCORD_STEM else ""
    )
    st.push_slots_update = MagicMock()
    st._restricted_keys = set()
    st._ephemeral_keys = set()
    st._slack_to_slot = {}
    st.get_or_create_slot = _real_get_or_create(st)
    return st


def _real_get_or_create(st):
    """A slot factory with the real _ChatSlot, minus the broadcast plumbing."""
    from kiro_crew.dashboard.state import _ChatSlot, _normalize_slot_key

    def make(name=None, agent="", linked_session_key="", **kw):
        name = _normalize_slot_key(name or "chat-1")
        if name in st._slots:
            return st._slots[name]
        slot = _ChatSlot(name, agent=agent)
        if linked_session_key:
            slot.linked_session_key = linked_session_key
        st._slots[name] = slot
        return slot

    return make


class TestA2OneSessionOneTranscript:
    """A2 — the tab is the same session and the same file as the thread."""

    def test_the_tab_is_bound_to_the_channel_session(self, state, log):
        _write_transcript(Path(log._dir), SLACK_STEM, _turns(4))
        slot = surface_channel_session(
            state, {"key": SLACK_STEM, "modified": 100.0}, {}, log.read_messages(SLACK_STEM),
            session_key=SLACK_KEY,
        )
        assert slot is not None
        assert slot.linked_session_key == SLACK_KEY
        # The one identity used for both the session and the transcript.
        assert effective_session_key(slot) == SLACK_KEY

    def test_the_tab_writes_the_channel_transcript_not_a_copy(self, state, log):
        _write_transcript(Path(log._dir), SLACK_STEM, _turns(4))
        slot = surface_channel_session(
            state, {"key": SLACK_STEM, "modified": 100.0}, {}, log.read_messages(SLACK_STEM),
            session_key=SLACK_KEY,
        )
        # _safe_key is what turns a session key into a filename; the bound key
        # and the channel's own stem must land on one file.
        assert _safe_key(effective_session_key(slot)) == SLACK_STEM
        assert not (Path(log._dir) / f"dashboard_{SLACK_STEM}.jsonl").exists()

    def test_a_discord_key_survives_the_round_trip(self, state, log):
        """A9 — multi-colon keys are the case a derived key gets wrong."""
        _write_transcript(Path(log._dir), DISCORD_STEM, _turns(2))
        slot = surface_channel_session(
            state, {"key": DISCORD_STEM, "modified": 100.0}, {}, log.read_messages(DISCORD_STEM),
            session_key=DISCORD_KEY,
        )
        assert slot is not None
        assert effective_session_key(slot) == DISCORD_KEY
        assert _safe_key(effective_session_key(slot)) == DISCORD_STEM

    def test_an_unresolvable_key_surfaces_unbound_rather_than_guessing(self, state, log):
        """Binding to a guessed key would answer the user from a dead session."""
        _write_transcript(Path(log._dir), "slack_9999.0", _turns(2))
        slot = surface_channel_session(
            state, {"key": "slack_9999.0", "modified": 100.0}, {},
            log.read_messages("slack_9999.0"),
            session_key="",
        )
        assert slot is not None
        assert slot.linked_session_key == ""


class TestA3FullHistoryIsReachable:
    """A3 — a conversation longer than the seed window is not truncated."""

    def test_a_62_message_thread_seeds_all_62(self, state, log):
        _write_transcript(Path(log._dir), SLACK_STEM, _turns(62))
        slot = surface_channel_session(
            state, {"key": SLACK_STEM, "modified": 100.0}, {}, log.read_messages(SLACK_STEM),
            session_key=SLACK_KEY,
        )
        assert slot is not None
        assert len(slot.messages) == 62
        assert slot._disk_older_count == 0

    def test_beyond_the_window_the_remainder_is_accounted_as_frozen_prefix(self, state, log):
        _write_transcript(Path(log._dir), SLACK_STEM, _turns(700))
        slot = surface_channel_session(
            state, {"key": SLACK_STEM, "modified": 100.0}, {}, log.read_messages(SLACK_STEM),
            session_key=SLACK_KEY,
        )
        assert slot is not None
        # Window + frozen prefix must equal the whole conversation, or the
        # detail endpoint cannot reassemble it.
        assert slot._disk_older_count + len(slot.messages) == 700


class TestA4TheTabStaysCurrent:
    """A4 — a channel turn after the tab opened appears in the tab."""

    def test_a_later_channel_turn_lands_in_the_window(self, state, log):
        _write_transcript(Path(log._dir), SLACK_STEM, _turns(4))
        slot = surface_channel_session(
            state, {"key": SLACK_STEM, "modified": 100.0}, {}, log.read_messages(SLACK_STEM),
            session_key=SLACK_KEY,
        )
        assert slot is not None and len(slot.messages) == 4

        # The channel writes two more turns straight to the shared file.
        log.append(SLACK_KEY, "user", "after the tab opened")
        log.append(SLACK_KEY, "assistant", "reply to that")

        added = refresh_channel_window(slot, log.read_messages(SLACK_KEY), 200.0)
        assert added == 2
        assert [m["content"] for m in slot.messages][-2:] == [
            "after the tab opened",
            "reply to that",
        ]

    def test_a_refresh_is_idempotent(self, state, log):
        _write_transcript(Path(log._dir), SLACK_STEM, _turns(4))
        slot = surface_channel_session(
            state, {"key": SLACK_STEM, "modified": 100.0}, {}, log.read_messages(SLACK_STEM),
            session_key=SLACK_KEY,
        )
        assert slot is not None
        msgs = log.read_messages(SLACK_KEY)
        assert refresh_channel_window(slot, msgs, 200.0) == 0
        assert refresh_channel_window(slot, msgs, 200.0) == 0
        assert len(slot.messages) == 4

    def test_a_refresh_never_marks_the_window_dirty(self, state, log):
        """The messages came from the file a save would write back."""
        _write_transcript(Path(log._dir), SLACK_STEM, _turns(4))
        slot = surface_channel_session(
            state, {"key": SLACK_STEM, "modified": 100.0}, {}, log.read_messages(SLACK_STEM),
            session_key=SLACK_KEY,
        )
        assert slot is not None
        log.append(SLACK_KEY, "assistant", "later")
        refresh_channel_window(slot, log.read_messages(SLACK_KEY), 200.0)
        assert slot._dirty is False
        assert slot._disk_older_count + len(slot.messages) == 5

    def test_a_rotated_transcript_re_anchors_instead_of_over_claiming(self, state, log):
        """Rotation shortens the file; stale counters would duplicate archived history."""
        _write_transcript(Path(log._dir), SLACK_STEM, _turns(700))
        slot = surface_channel_session(
            state, {"key": SLACK_STEM, "modified": 100.0}, {}, log.read_messages(SLACK_STEM),
            session_key=SLACK_KEY,
        )
        assert slot is not None
        assert slot._disk_older_count == 200

        # ConversationLog archives the head, leaving a much shorter file.
        rotated = _turns(50, start=650)
        assert refresh_channel_window(slot, rotated, 300.0) == 0
        # The accounting must never claim more lines than the file holds, or the
        # next save reads a frozen prefix past the end of it.
        assert slot._disk_older_count + len(slot.messages) == len(rotated)
        assert slot._disk_older_count == 0
        # And the WINDOW must be the file's tail, not the pre-rotation one: a
        # later save writes the window back, so a stale message here would push
        # archived turns into the active transcript.
        assert [m["content"] for m in slot.messages] == [m["content"] for m in rotated]
        assert slot._dirty is False

    def test_an_equal_length_rotation_is_still_detected(self, state, log):
        """Rotation shifts every offset; the counters can still add up."""
        _write_transcript(Path(log._dir), SLACK_STEM, _turns(20))
        slot = surface_channel_session(
            state, {"key": SLACK_STEM, "modified": 100.0}, {}, log.read_messages(SLACK_STEM),
            session_key=SLACK_KEY,
        )
        assert slot is not None
        assert slot._disk_older_count == 0 and len(slot.messages) == 20

        # Archive the head and take on new turns, landing at the SAME length —
        # a length check alone reads this as "nothing changed".
        rotated = _turns(20, start=40)
        assert len(rotated) == 20
        assert refresh_channel_window(slot, rotated, 300.0) == 0
        assert [m["content"] for m in slot.messages] == [m["content"] for m in rotated]
        assert slot._dirty is False

    def test_an_aligned_window_is_left_alone(self, state, log):
        """The alignment check must not rebuild on every pass."""
        _write_transcript(Path(log._dir), SLACK_STEM, _turns(6))
        slot = surface_channel_session(
            state, {"key": SLACK_STEM, "modified": 100.0}, {}, log.read_messages(SLACK_STEM),
            session_key=SLACK_KEY,
        )
        assert slot is not None
        before = [m["content"] for m in slot.messages]
        msgs = log.read_messages(SLACK_KEY)
        assert refresh_channel_window(slot, msgs, 200.0) == 0
        assert [m["content"] for m in slot.messages] == before

    def test_surfacing_publishes_the_tab_to_the_surface_registry(self, state, log, monkeypatch):
        """Every 'does this session have a tab?' gate reads that registry."""
        published: list[set[str]] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_utils.set_dashboard_surfaced",
            lambda keys: published.append(set(keys)),
        )
        state.sessions.set_active_dashboard_slots = MagicMock()
        _write_transcript(Path(log._dir), SLACK_STEM, _turns(2))
        slot = surface_channel_session(
            state, {"key": SLACK_STEM, "modified": 100.0}, {}, log.read_messages(SLACK_STEM),
            session_key=SLACK_KEY,
        )
        assert slot is not None
        from kiro_crew.dashboard.chat_utils import _sync_dashboard_slots

        _sync_dashboard_slots(state)
        assert published and SLACK_KEY in published[-1]


class TestA5AppearsOnce:
    """A5 — one conversation, one row; two key spellings must not split it."""

    def test_the_slot_name_is_the_transcript_stem(self):
        assert channel_slot_name(SLACK_KEY) == SLACK_STEM
        assert channel_slot_name(DISCORD_KEY) == DISCORD_STEM

    def test_slot_name_derivation_is_idempotent(self):
        """The reconciler and the restore path must produce the same name."""
        assert channel_slot_name(channel_slot_name(SLACK_KEY)) == SLACK_STEM
        assert channel_slot_name(channel_slot_name(DISCORD_KEY)) == DISCORD_STEM

    def test_the_stem_addresses_the_channel_transcript(self):
        """Why the restore path can find the file without the colon key."""
        assert _safe_key(slot_transcript_key(SLACK_STEM)) == SLACK_STEM
        assert _safe_key(slot_transcript_key(DISCORD_STEM)) == DISCORD_STEM

    def test_a_dashboard_born_slot_still_gets_the_dashboard_key(self):
        assert slot_transcript_key("chat-7-1785396512") == "dashboard:chat-7-1785396512"


class TestSurfaceRegistry:
    """The 'has a dashboard surface?' question, which a prefix test got wrong."""

    def setup_method(self):
        set_dashboard_surfaced(())

    def teardown_method(self):
        set_dashboard_surfaced(())

    def test_a_dashboard_born_session_needs_no_registry(self):
        assert has_dashboard_surface("dashboard:chat-1-99") is True

    def test_a_channel_session_has_a_surface_only_while_a_tab_is_open(self):
        assert has_dashboard_surface(SLACK_KEY) is False
        set_dashboard_surfaced({SLACK_KEY})
        assert has_dashboard_surface(SLACK_KEY) is True
        assert dashboard_slot_key(SLACK_KEY) == SLACK_STEM

    def test_a_session_owned_by_another_surface_never_resolves(self):
        set_dashboard_surfaced({SLACK_KEY})
        assert has_dashboard_surface("cron:nightly") is False
        assert dashboard_slot_key("cron:nightly") == ""

    def test_a_cron_session_resolves_to_its_actual_slot_name(self):
        """A cron-born tab is named ``cron-<id>`` (cron_inject.py), NOT the
        session key folded (``cron_<id>``). Resolving the fold sent sub-agent
        completions to a slot that never existed — "parent slot cron_<id>
        gone, notification only" — so results reached the bell icon but never
        the open conversation."""
        set_dashboard_surfaced({"cron:188f71e5"})
        assert dashboard_slot_key("cron:188f71e5") == "cron-188f71e5"

    def test_a_cron_per_run_key_resolves_to_the_job_tab(self):
        """Stateless jobs run under ``cron:<job_id>:<run_id>`` and agent
        sequences under ``cron:<job_id>:<agent>``, but the surface registry
        only ever holds the slot's linked key (``cron:<job_id>``) — the base
        key must be retried or those runs stay invisible."""
        set_dashboard_surfaced({"cron:188f71e5"})
        assert dashboard_slot_key("cron:188f71e5:a1b2c3") == "cron-188f71e5"
        assert dashboard_slot_key("cron:188f71e5:worker") == "cron-188f71e5"

    def test_a_cron_session_with_no_tab_still_resolves_to_nothing(self):
        set_dashboard_surfaced(())
        assert dashboard_slot_key("cron:188f71e5") == ""
        assert dashboard_slot_key("cron:188f71e5:a1b2c3") == ""

    def test_an_empty_registry_degrades_to_the_prefix_test(self):
        """Fail-safe: no worse than the behaviour it replaced."""
        set_dashboard_surfaced(())
        assert has_dashboard_surface("dashboard:chat-1-99") is True
        assert has_dashboard_surface(SLACK_KEY) is False


class TestSubagentEventSlotRouting:
    """The ``slot`` a per-slot WS event carries must be the TAB's key.

    The frontend routes ``subagent_spawn/tool/done`` (and the reconnect
    replay) by exact string match against the tab's slot key. A raw
    ``removeprefix("dashboard:")`` tags frames from cron/channel-born parents
    with the raw session key, which no tab uses — the Subagents panel then
    reads "No subagents running" for the entire life of every agent those
    sessions spawn.
    """

    def setup_method(self):
        set_dashboard_surfaced(())

    def teardown_method(self):
        set_dashboard_surfaced(())

    def test_dashboard_born_parent_keeps_its_slot_key(self):
        assert subagent_event_slot("dashboard:chat-3-1754") == "chat-3-1754"

    def test_cron_born_parent_routes_to_the_cron_tab(self):
        """Regression: agents spawned from a cron-born session were invisible
        in the panel (events carried ``cron:<id>``, the tab is ``cron-<id>``)."""
        set_dashboard_surfaced({"cron:188f71e5"})
        assert subagent_event_slot("cron:188f71e5") == "cron-188f71e5"

    def test_cron_per_run_parent_routes_to_the_job_tab(self):
        """A stateless run's ``cron:<job_id>:<run_id>`` parent must reach the
        job's tab too — only the linked ``cron:<job_id>`` is ever surfaced."""
        set_dashboard_surfaced({"cron:188f71e5"})
        assert subagent_event_slot("cron:188f71e5:a1b2c3") == "cron-188f71e5"

    def test_channel_born_parent_routes_to_its_transcript_stem_tab(self):
        set_dashboard_surfaced({SLACK_KEY})
        assert subagent_event_slot(SLACK_KEY) == SLACK_STEM

    def test_no_tab_falls_back_to_the_legacy_raw_key(self):
        """No tab open: nothing can route anywhere, but external WS consumers
        and log lines keep the historical payload shape."""
        assert subagent_event_slot("cron:188f71e5") == "cron:188f71e5"
        assert subagent_event_slot(SLACK_KEY) == SLACK_KEY


class TestA6NoLostOrOutOfOrderTurn:
    """A6 — a message arriving while the other surface holds the turn.

    The guarantee is ordering and delivery, not steering. A steer RPC is
    fire-and-forget: it reports that the request was WRITTEN, and only a
    ``steering_consumed`` echo proves it was taken up. The dashboard makes that
    safe with pending-steer accounting that requeues on turn exit; a caller
    without that accounting would silently drop a message whose turn ended
    mid-flight. So a cross-surface message is serialized on the shared semaphore
    instead, which loses nothing and cannot reorder.
    """

    def test_a_busy_session_is_reported_busy_from_either_spelling(self):
        """One conversation, one busy answer, whichever key spelling asks.

        Slack holds the bare ``thread_ts`` while the dashboard holds the
        canonical ``slack:<ts>``; without the fold a busy conversation reads
        idle to half its callers.
        """
        import asyncio

        from kiro_crew.session import SessionManager, _Session

        async def _check():
            mgr = SessionManager.__new__(SessionManager)
            sess = _Session(provider=MagicMock())
            mgr._sessions = {SLACK_KEY: sess}
            mgr._fold_key = lambda k: SLACK_KEY if k in (SLACK_KEY, "1785370133.085469") else k
            assert mgr.is_busy(SLACK_KEY) is False
            await sess.semaphore.acquire()
            assert mgr.is_busy(SLACK_KEY) is True
            assert mgr.is_busy("1785370133.085469") is True

        asyncio.new_event_loop().run_until_complete(_check())

    def test_no_steer_primitive_is_exposed(self):
        """Withdrawn deliberately: an unaccounted steer can drop a message."""
        from kiro_crew.session import SessionManager

        assert not hasattr(SessionManager, "steer")


class TestOneRedactionRule:
    """§3.6 — one transcript cannot have two redaction policies."""

    def test_model_authored_content_is_scrubbed_on_the_channel_path(self, log):
        log.append(SLACK_KEY, "assistant", "token is AKIAIOSFODNN7EXAMPLE ok")
        stored = log.read_messages(SLACK_KEY)[0]["content"]
        assert "AKIAIOSFODNN7EXAMPLE" not in stored

    def test_user_typed_content_is_stored_verbatim(self, log):
        """Matches the dashboard's own gate; the user's words are not rewritten."""
        log.append(SLACK_KEY, "user", "my key is AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" in log.read_messages(SLACK_KEY)[0]["content"]

    def test_append_if_absent_stays_idempotent_under_redaction(self, log):
        """Dedup compares the stored form, so a scrubbed message is recognised."""
        content = "leaked AKIAIOSFODNN7EXAMPLE here"
        assert log.append_if_absent(SLACK_KEY, "assistant", content) is True
        assert log.append_if_absent(SLACK_KEY, "assistant", content) is False
        assert len(log.read_messages(SLACK_KEY)) == 1


class TestForeignAppendsInterleaveChronologically:
    """A channel turn that lands mid-window is filed where it happened.

    The save rewrites ``meta + frozen prefix + window``. Foreign lines (another
    writer's acknowledged appends) used to be concatenated after the window,
    which parked a channel reply that arrived BEFORE the user's next dashboard
    message after it. Once the tab and the thread share one transcript that
    reordering is the conversation the next turn reads back.
    """

    @staticmethod
    def _line(role, content, ts):
        import json as _json

        return _json.dumps({"role": role, "content": content, "ts": ts}) + "\n"

    def test_a_channel_line_from_between_two_window_turns_lands_between_them(self):
        from kiro_crew.dashboard.chat_persistence import _interleave_foreign_lines

        window_entries = [
            {"role": "user", "content": "first", "ts": "2026-08-01T10:00:00+00:00"},
            {"role": "user", "content": "third", "ts": "2026-08-01T10:02:00+00:00"},
        ]
        window_lines = [self._line(e["role"], e["content"], e["ts"]) for e in window_entries]
        foreign = [self._line("assistant", "second", "2026-08-01T10:01:00+00:00")]

        merged = _interleave_foreign_lines(window_entries, window_lines, foreign)
        assert [__import__("json").loads(m)["content"] for m in merged] == [
            "first",
            "second",
            "third",
        ]

    def test_a_genuinely_newer_channel_line_still_lands_last(self):
        from kiro_crew.dashboard.chat_persistence import _interleave_foreign_lines

        window_entries = [
            {"role": "user", "content": "first", "ts": "2026-08-01T10:00:00+00:00"},
        ]
        window_lines = [self._line("user", "first", "2026-08-01T10:00:00+00:00")]
        foreign = [self._line("assistant", "later", "2026-08-01T10:05:00+00:00")]

        merged = _interleave_foreign_lines(window_entries, window_lines, foreign)
        assert [__import__("json").loads(m)["content"] for m in merged] == ["first", "later"]

    def test_no_foreign_lines_returns_the_window_untouched(self):
        from kiro_crew.dashboard.chat_persistence import _interleave_foreign_lines

        window_lines = [self._line("user", "only", "2026-08-01T10:00:00+00:00")]
        entries = [{"role": "user", "content": "only", "ts": "2026-08-01T10:00:00+00:00"}]
        assert _interleave_foreign_lines(entries, window_lines, []) is window_lines

    def test_an_unparseable_timestamp_stays_beside_its_neighbour(self):
        from kiro_crew.dashboard.chat_persistence import _interleave_foreign_lines

        window_entries = [
            {"role": "user", "content": "first", "ts": "2026-08-01T10:00:00+00:00"},
            {"role": "assistant", "content": "no-ts", "ts": ""},
        ]
        window_lines = [self._line(e["role"], e["content"], e["ts"]) for e in window_entries]
        foreign = [self._line("user", "channel", "2026-08-01T10:09:00+00:00")]

        merged = _interleave_foreign_lines(window_entries, window_lines, foreign)
        contents = [__import__("json").loads(m)["content"] for m in merged]
        # The un-stamped line inherits "first"'s instant, so it cannot jump the
        # later channel line.
        assert contents == ["first", "no-ts", "channel"]
