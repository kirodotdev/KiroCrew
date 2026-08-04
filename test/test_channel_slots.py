"""Tests for surfacing channel-originated sessions as dashboard chat slots.

Covers the eligibility rules (recency window, closed, ephemeral memory modes,
pin/folder exemption), the slot-creation/binding behaviour, and the async
reconcile pass.

One conversation has one session key and one transcript. A channel session's
dashboard tab is BOUND to that key (``linked_session_key``), so metadata — a
``closed`` flag, a pin, a memory mode — is written once and read once. Fixtures
therefore key metadata on the channel session key, never on the slot name
derived from it.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import channel_slots
from kiro_crew.history import _safe_key
from kiro_crew.messaging.link import (
    channel_namespace_of,
    is_channel_session_key,
)

NOW = time.time()


@pytest.fixture
def dashboard_state(tmp_path: Any) -> Any:
    """DashboardState with mocked services and a real (empty) ConversationLog.

    ``sessions`` is a MagicMock whose auto-created attributes are truthy, so the
    channel-key resolver is pinned to the empty-session-map answer (``""``) —
    otherwise a slot could silently bind to a Mock. Call :func:`_map_stems` in
    tests that need the resolution to succeed.
    """
    state = _make_state(tmp_path)
    state.sessions.channel_key_for_stem = lambda stem: ""
    return state


def _session(key: str, *, modified: float | None = None, **extra: Any) -> dict[str, Any]:
    return {"key": key, "title": "", "modified": modified if modified is not None else NOW, **extra}


def _map_stems(state: Any, *keys: str) -> None:
    """Make *state*'s session manager resolve *keys* from their filename stems.

    Mirrors ``SessionManager.channel_key_for_stem``: the reconciler can only bind
    a tab to a key the session map actually holds, because ``history._safe_key``
    folds every ``:`` to ``_`` and that fold is not reversible.
    """
    mapping = {_safe_key(k): k for k in keys}
    state.sessions.channel_key_for_stem = lambda stem: mapping.get(stem, "")


class TestChannelKeyPredicates:
    def test_recognizes_every_channel_namespace(self) -> None:
        for key in (
            "slack:1785370133.085469",
            "discord:kirocrew:direct:U1",
            "telegram:kirocrew:direct:U1",
            "whatsapp:kirocrew:direct:U1",
            "webex:kirocrew:direct:U1",
            "wecom:kirocrew:direct:U1",
            "teams:kirocrew:direct:U1",
            "weixin:kirocrew:direct:U1",
            "unified:kirocrew",
        ):
            assert is_channel_session_key(key), key

    def test_recognizes_the_persisted_filename_stem_form(self) -> None:
        """list_sessions() reports the stem, where _safe_key folded ':' -> '_'.

        Missing this is why the reconciler saw zero channel sessions in a real
        instance while every synthetic ``slack:`` fixture passed.
        """
        assert is_channel_session_key("slack_1785370133.085469")
        assert is_channel_session_key("discord_kirocrew_direct_U1")
        assert is_channel_session_key("unified_kirocrew")
        assert channel_namespace_of("slack_1.1") == "slack"
        assert channel_slots.channel_label("slack_1.1") == "Slack"

    def test_rejects_non_channel_namespaces(self) -> None:
        for key in (
            "dashboard:chat-1-123",
            "cron:abc123",
            "hook:default:1",
            "subagent:xyz",
            "channel:general",
            "dashboard_chat-1-123",
            "cron_abc123",
            "",
            "slackish:1.2",
            "slackish_1.2",
        ):
            assert not is_channel_session_key(key), key

    def test_namespace_of(self) -> None:
        assert channel_namespace_of("slack:1.2") == "slack"
        assert channel_namespace_of("teams:a:direct:b") == "teams"
        assert channel_namespace_of("cron:x") == ""

    def test_labels(self) -> None:
        assert channel_slots.channel_label("slack:1.2") == "Slack"
        assert channel_slots.channel_label("wecom:a:direct:b") == "WeCom"
        assert channel_slots.channel_label("dashboard:chat-1") == "Channel"


class TestEligibility:
    def test_dashboard_and_cron_sessions_are_never_eligible(self) -> None:
        sessions = [_session("dashboard:chat-1-1"), _session("cron:abc")]
        out = channel_slots.eligible_channel_sessions(sessions, metadata={}, cutoff=None)
        assert out == []

    def test_recent_channel_session_is_eligible(self) -> None:
        sessions = [_session("slack:1785370133.085469")]
        out = channel_slots.eligible_channel_sessions(sessions, metadata={}, cutoff=NOW - 1800)
        assert [s["key"] for s in out] == ["slack:1785370133.085469"]

    def test_stale_channel_session_is_filtered(self) -> None:
        sessions = [_session("slack:1.1", modified=NOW - 7200)]
        out = channel_slots.eligible_channel_sessions(sessions, metadata={}, cutoff=NOW - 1800)
        assert out == []

    def test_zero_window_disables_recency_filter(self) -> None:
        sessions = [_session("slack:1.1", modified=NOW - 999999)]
        out = channel_slots.eligible_channel_sessions(sessions, metadata={}, cutoff=None)
        assert len(out) == 1

    def test_pinned_survives_the_window(self) -> None:
        sessions = [_session("slack:1.1", modified=NOW - 7200)]
        out = channel_slots.eligible_channel_sessions(
            sessions, metadata={"slack:1.1": {"pinned": True}}, cutoff=NOW - 1800
        )
        assert len(out) == 1

    def test_foldered_survives_the_window(self) -> None:
        sessions = [_session("slack:1.1", modified=NOW - 7200)]
        out = channel_slots.eligible_channel_sessions(
            sessions, metadata={"slack:1.1": {"folder_id": "f1"}}, cutoff=NOW - 1800
        )
        assert len(out) == 1

    def test_a_closed_session_with_no_known_close_instant_is_never_resurfaced(self) -> None:
        """A close with no known instant must stick — fail toward the dismissal.

        (With a ``closed_at`` stamp or a file mtime the close can be outrun by
        newer channel activity — see ``TestCloseReactivation``.)
        """
        sessions = [_session("slack:1.1")]
        out = channel_slots.eligible_channel_sessions(
            sessions, metadata={"slack:1.1": {"closed": True}}, cutoff=NOW - 1800
        )
        assert out == []

    def test_metadata_under_the_derived_slot_key_is_ignored(self) -> None:
        """One key per conversation.

        The tab and the channel thread share the conversation's own session key,
        so nothing is written under the slot name derived from it. A stray flag
        there belongs to no conversation and must not hide this one.
        """
        sessions = [_session("slack:1.1")]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"dashboard:slack_1.1": {"closed": True}},
            cutoff=NOW - 1800,
        )
        assert [s["key"] for s in out] == ["slack:1.1"]

    def test_slot_name_is_the_channel_key_folded_to_the_filename_charset(self) -> None:
        assert channel_slots.channel_slot_name("slack:1.1") == "slack_1.1"
        assert channel_slots.channel_slot_name("discord:kirocrew:direct:U1") == (
            "discord_kirocrew_direct_U1"
        )

    def test_closed_beats_pinned(self) -> None:
        sessions = [_session("slack:1.1")]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"slack:1.1": {"closed": True, "pinned": True}},
            cutoff=None,
        )
        assert out == []

    @pytest.mark.parametrize("mode", ["incognito", "temporary", "INCOGNITO"])
    def test_ephemeral_threads_are_skipped(self, mode: str) -> None:
        sessions = [_session("slack:1.1")]
        out = channel_slots.eligible_channel_sessions(
            sessions, metadata={"slack:1.1": {"memory_mode": mode}}, cutoff=None
        )
        assert out == []

    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    def test_ephemeral_detected_from_listing_too(self, mode: str) -> None:
        """The listing carries memory_mode as well; either source disqualifies."""
        sessions = [_session("slack:1.1", memory_mode=mode)]
        out = channel_slots.eligible_channel_sessions(sessions, metadata={}, cutoff=None)
        assert out == []


class TestCloseReactivation:
    """A close stands only until channel-side activity outruns it."""

    def test_activity_after_close_resurfaces(self) -> None:
        """The person kept talking on the channel after the tab was closed."""
        sessions = [_session("slack:1.1", modified=NOW)]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"slack:1.1": {"closed": True, "closed_at": NOW - 600}},
            cutoff=None,
        )
        assert [s["key"] for s in out] == ["slack:1.1"]

    def test_close_newer_than_activity_stands(self) -> None:
        """No channel activity since the close — the dismissal holds."""
        sessions = [_session("slack:1.1", modified=NOW - 600)]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"slack:1.1": {"closed": True, "closed_at": NOW}},
            cutoff=None,
        )
        assert out == []

    def test_close_at_exactly_the_activity_instant_stands(self) -> None:
        """Strictly-newer comparison: a tie is not new activity."""
        sessions = [_session("slack:1.1", modified=NOW)]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"slack:1.1": {"closed": True, "closed_at": NOW}},
            cutoff=None,
        )
        assert out == []

    def test_legacy_close_falls_back_to_file_mtime(self) -> None:
        """A pre-stamp `closed` flag uses the transcript's mtime as the close
        instant — the closing save is what last wrote that file."""
        sessions = [_session("slack:1.1", modified=NOW)]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"slack:1.1": {"closed": True}},
            cutoff=None,
            mtimes={"slack:1.1": NOW - 600},
        )
        assert [s["key"] for s in out] == ["slack:1.1"]

    def test_legacy_close_with_stale_mtime_stands(self) -> None:
        sessions = [_session("slack:1.1", modified=NOW - 600)]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"slack:1.1": {"closed": True}},
            cutoff=None,
            mtimes={"slack:1.1": NOW},
        )
        assert out == []

    def test_garbage_closed_at_falls_back_to_mtime(self) -> None:
        sessions = [_session("slack:1.1", modified=NOW)]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"slack:1.1": {"closed": True, "closed_at": "not-a-number"}},
            cutoff=None,
            mtimes={"slack:1.1": NOW - 600},
        )
        assert len(out) == 1

    def test_reactivated_session_still_respects_the_recency_window(self) -> None:
        """Outrunning the close does not exempt a session from the cutoff."""
        sessions = [_session("slack:1.1", modified=NOW - 7200)]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"slack:1.1": {"closed": True, "closed_at": NOW - 99999}},
            cutoff=NOW - 1800,
        )
        assert out == []


class TestSurfaceChannelSession:
    def test_creates_slot_seeded_with_the_conversation(self, dashboard_state: Any) -> None:
        slot = channel_slots.surface_channel_session(
            dashboard_state,
            _session("slack:1785370133.085469", title="Ship the thing"),
            {},
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        )
        assert slot is not None
        # Deterministic name = the session key folded to the filename charset.
        assert slot.key == "slack_1785370133.085469"
        assert slot.title == "Ship the thing"
        assert [m["content"] for m in slot.messages] == ["hi", "hello"]

    def test_binds_the_slot_to_the_real_channel_session_key(self, dashboard_state: Any) -> None:
        """The tab IS the conversation, not a picture of it: a reply typed in it
        runs on the channel's own session and lands in the channel transcript,
        which only holds while the slot carries that key.
        """
        slot = channel_slots.surface_channel_session(
            dashboard_state,
            _session("slack_1.1"),
            {},
            [],
            session_key="slack:1.1",
        )
        assert slot is not None
        assert slot.key == "slack_1.1"
        assert slot.linked_session_key == "slack:1.1"

    def test_an_unresolvable_session_key_surfaces_the_slot_unbound(
        self, dashboard_state: Any
    ) -> None:
        """The filename fold is not reversible, so a caller that cannot resolve
        the key must not invent one: the tab shows the history without claiming
        to be two-way, rather than routing replies to a session the channel
        never reads."""
        slot = channel_slots.surface_channel_session(
            dashboard_state, _session("slack_1.1"), {}, []
        )
        assert slot is not None
        assert slot.linked_session_key == ""

    def test_refuses_to_bind_to_a_non_channel_session_key(self, dashboard_state: Any) -> None:
        """Only a channel key can be the far side of a channel tab — anything
        else would answer the user from an unrelated conversation."""
        slot = channel_slots.surface_channel_session(
            dashboard_state,
            _session("slack:1.1"),
            {},
            [],
            session_key="dashboard:chat-1-1",
        )
        assert slot is not None
        assert slot.linked_session_key == ""

    def test_untitled_session_falls_back_to_the_channel_label(self, dashboard_state: Any) -> None:
        slot = channel_slots.surface_channel_session(
            dashboard_state, _session("teams:a:direct:b"), {}, []
        )
        assert slot is not None
        assert slot.title == "Teams"
        assert slot._titled is False

    def test_is_idempotent(self, dashboard_state: Any) -> None:
        info = _session("slack:1.1", title="T")
        first = channel_slots.surface_channel_session(dashboard_state, info, {}, [])
        second = channel_slots.surface_channel_session(dashboard_state, info, {}, [])
        assert first is not None
        assert second is None, "second pass must be a no-op"
        assert len(dashboard_state._slots) == 1

    def test_an_existing_slot_is_never_re_seeded(self, dashboard_state: Any) -> None:
        """A slot the restore path already rebuilt owns its window — seeding a
        second copy of the transcript on top of it would duplicate every turn."""
        existing = dashboard_state.get_or_create_slot(name="slack_1.1")
        existing.append("user", "already here", "msg msg-u", broadcast=False)
        existing.drain()
        assert (
            channel_slots.surface_channel_session(
                dashboard_state,
                _session("slack:1.1"),
                {},
                [{"role": "user", "content": "should not be duplicated"}],
                session_key="slack:1.1",
            )
            is None
        )
        assert [m["content"] for m in existing.messages] == ["already here"]
        assert list(dashboard_state._slots) == ["slack_1.1"]

    def test_ignores_non_channel_keys(self, dashboard_state: Any) -> None:
        assert (
            channel_slots.surface_channel_session(
                dashboard_state, _session("dashboard:chat-1-1"), {}, []
            )
            is None
        )
        assert dashboard_state._slots == {}

    def test_applies_metadata(self, dashboard_state: Any) -> None:
        slot = channel_slots.surface_channel_session(
            dashboard_state,
            _session("slack:1.1"),
            {
                "agent": "kirocrew",
                "model": "claude-opus-5",
                "workspace": "default",
                "project": "p1",
                "folder_id": "f1",
                "pinned": True,
                "created_at": "2026-07-30T00:00:00Z",
            },
            [],
        )
        assert slot is not None
        assert slot.agent == "kirocrew"
        assert slot.model == "claude-opus-5"
        assert slot.project == "p1"
        assert slot.folder_id == "f1"
        assert slot.pinned is True
        assert slot.created_at == "2026-07-30T00:00:00Z"

    def test_redacts_titles_and_messages(self, dashboard_state: Any) -> None:
        slot = channel_slots.surface_channel_session(
            dashboard_state,
            _session("slack:1.1", title="key AKIAIOSFODNN7EXAMPLE"),
            {},
            [{"role": "assistant", "content": "token AKIAIOSFODNN7EXAMPLE"}],
        )
        assert slot is not None
        assert "AKIAIOSFODNN7EXAMPLE" not in slot.title
        assert "AKIAIOSFODNN7EXAMPLE" not in slot.messages[0]["content"]


class _FakeLog:
    def __init__(self, sessions: list[dict[str, Any]], meta: dict[str, dict[str, Any]]) -> None:
        self._sessions = sessions
        self._meta = meta
        #: keys get_metadata was invoked for, in order.
        self.meta_reads: list[str] = []
        self.message_reads: list[str] = []
        #: key -> file mtime, consulted as the fallback close instant. Unset
        #: keys report None (file absent), which keeps a legacy close standing.
        self.mtimes: dict[str, float] = {}
        #: keys clear_closed was invoked for, in order.
        self.cleared: list[str] = []
        #: every clear_closed invocation: (key, only_if_closed_before, outcome).
        self.clear_calls: list[tuple[str, float | None, str]] = []
        #: key -> transcript. Unset keys read empty.
        self.transcripts: dict[str, list[dict[str, Any]]] = {
            s["key"]: [{"role": "user", "content": f"msg for {s['key']}"}] for s in sessions
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        return list(self._sessions)

    def get_metadata(self, key: str) -> dict[str, Any]:
        self.meta_reads.append(key)
        return dict(self._meta.get(key, {}))

    def mtime_of(self, key: str) -> float | None:
        return self.mtimes.get(key)

    def clear_closed(self, key: str, *, only_if_closed_before: float | None = None) -> None:
        meta = self._meta.get(key, {})
        if only_if_closed_before is not None and "closed" in meta:
            raw = meta.get("closed_at")
            try:
                close_time = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                close_time = None
            if close_time is None:
                close_time = self.mtimes.get(key)
            if close_time is not None and close_time >= only_if_closed_before:
                self.clear_calls.append((key, only_if_closed_before, "spared"))
                return
        self.cleared.append(key)
        self.clear_calls.append((key, only_if_closed_before, "cleared"))
        meta.pop("closed", None)
        meta.pop("closed_at", None)

    def read_messages(self, key: str) -> list[dict[str, Any]]:
        self.message_reads.append(key)
        return list(self.transcripts.get(key, []))


class TestReconcilePass:
    def test_surfaces_eligible_and_pushes_once(self, dashboard_state: Any) -> None:
        dashboard_state.conversation_log = _FakeLog(
            [
                _session("slack:1.1"),
                _session("discord:a:direct:b"),
                _session("dashboard:chat-1-1"),
                _session("slack:2.2", modified=NOW - 99999),
            ],
            {},
        )
        pushes: list[int] = []
        dashboard_state.push_slots_update = lambda: pushes.append(1)  # type: ignore[method-assign]

        n = asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert n == 2
        assert set(dashboard_state._slots) == {"slack_1.1", "discord_a_direct_b"}
        # get_or_create_slot broadcasts on create; the pass adds a final push so
        # a rebind-only pass (no create) still reaches connected clients.
        assert pushes, "the pass must broadcast the new slots"

    def test_a_closed_tab_is_not_reopened_by_the_next_pass(self, dashboard_state: Any) -> None:
        """Closing the tab is a statement about the conversation, and the next
        30s pass must respect it."""
        log = _FakeLog([_session("slack:1.1")], {"slack:1.1": {"closed": True}})
        # The close instant equals the last channel activity — no new activity.
        log.mtimes["slack:1.1"] = NOW
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 0
        assert dashboard_state._slots == {}
        assert log.cleared == []

    def test_channel_activity_after_close_reopens_and_clears_the_flag(
        self, dashboard_state: Any
    ) -> None:
        """New channel activity outruns the close: the tab comes back, and the
        stale `closed`/`closed_at` flags are dropped so every restore path
        agrees the conversation is open."""
        log = _FakeLog(
            [_session("slack:1.1", modified=NOW)],
            {"slack:1.1": {"closed": True, "closed_at": NOW - 600}},
        )
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        assert "slack_1.1" in dashboard_state._slots
        assert log.cleared == ["slack:1.1"]
        assert "closed" not in log._meta["slack:1.1"]

    def test_legacy_close_reopens_via_file_mtime_fallback(self, dashboard_state: Any) -> None:
        """A pre-stamp `closed` flag (no closed_at) reactivates off the
        transcript's mtime — the shape of sessions closed before the stamp."""
        log = _FakeLog(
            [_session("slack:1.1", modified=NOW)],
            {"slack:1.1": {"closed": True}},
        )
        log.mtimes["slack:1.1"] = NOW - 600
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        assert "slack_1.1" in dashboard_state._slots
        assert log.cleared == ["slack:1.1"]

    def test_stale_flags_cleared_before_the_slot_is_visible(self, dashboard_state: Any) -> None:
        """Clearing AFTER the slot broadcast races a user closing the
        just-reactivated tab — the deferred clear would erase the fresh `closed`
        and the next pass would reopen a tab the user just dismissed. The clear
        must complete before the slot exists."""
        log = _FakeLog(
            [_session("slack:1.1", modified=NOW)],
            {"slack:1.1": {"closed": True, "closed_at": NOW - 600}},
        )
        slot_present_at_clear: list[bool] = []
        orig_clear = log.clear_closed

        def _recording_clear(key: str, **kwargs: Any) -> None:
            slot_present_at_clear.append("slack_1.1" in dashboard_state._slots)
            orig_clear(key, **kwargs)

        log.clear_closed = _recording_clear  # type: ignore[method-assign]
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        assert slot_present_at_clear, "clear_closed must have been invoked"
        assert not any(slot_present_at_clear), "flags must be cleared before the slot is surfaced"

    def test_clears_are_scoped_to_the_snapshot_instant(self, dashboard_state: Any) -> None:
        """The reconciler must pass its snapshot instant as a compare-and-clear
        cutoff, so a `closed` written after the snapshot (user dismissal
        mid-pass, racing writer) survives the clear."""
        log = _FakeLog(
            [_session("slack:1.1", modified=NOW)],
            {"slack:1.1": {"closed": True, "closed_at": NOW - 600}},
        )
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        before = time.time()
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        after = time.time()
        assert log.clear_calls, "clear_closed must have been invoked"
        for _key, cutoff_arg, _outcome in log.clear_calls:
            assert cutoff_arg is not None, "clear must carry the snapshot cutoff"
            assert before <= cutoff_arg <= after

    def test_a_close_fresher_than_the_snapshot_survives_the_clear(
        self, dashboard_state: Any
    ) -> None:
        """A dismissal recorded after the pass's snapshot is not erased: the
        compare-and-clear spares it, and the fresh close keeps standing."""
        log = _FakeLog(
            [_session("slack:1.1", modified=NOW)],
            # Stale in the snapshot the reconciler reads...
            {"slack:1.1": {"closed": True, "closed_at": NOW - 600}},
        )
        # ...but by clear time the user has re-closed: simulate the racing
        # write by bumping closed_at to the future before delegating.
        orig_clear = log.clear_closed

        def _racing_clear(key: str, **kwargs: Any) -> None:
            meta = log._meta.get(key)
            if meta and "closed" in meta:
                meta["closed_at"] = time.time() + 3600
            orig_clear(key, **kwargs)

        log.clear_closed = _racing_clear  # type: ignore[method-assign]
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert log._meta["slack:1.1"].get("closed") is True, (
            "a close written after the snapshot must survive the stale clear"
        )

    def test_overlapping_reconciles_are_serialized(self, dashboard_state: Any) -> None:
        """The periodic loop and a dispatcher-triggered immediate pass must not
        interleave — overlapping passes could clear flags from stale snapshots."""
        log = _FakeLog([_session("slack:1.1")], {})
        active = {"n": 0, "max": 0}
        orig_list = log.list_sessions

        def _tracking_list() -> list[dict[str, Any]]:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            try:
                time.sleep(0.02)
                return orig_list()
            finally:
                active["n"] -= 1

        log.list_sessions = _tracking_list  # type: ignore[method-assign]
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        async def _run_two() -> None:
            await asyncio.gather(
                channel_slots.reconcile_channel_slots(dashboard_state, 30),
                channel_slots.reconcile_channel_slots(dashboard_state, 30),
            )

        asyncio.run(_run_two())
        assert active["max"] == 1, "reconcile passes must not overlap"

    def test_a_tab_closed_mid_pass_is_not_resurrected(self, dashboard_state: Any) -> None:
        """A tab resumed from History and closed while this pass's executor work
        is in flight pops the slot, so the stale `pending` verdict would recreate
        it. The close path's synchronous tombstone must be honored after the
        pass's last await."""
        log = _FakeLog([_session("slack:1.1", modified=NOW)], {})
        orig_meta = log.get_metadata

        def _close_during_pass(key: str) -> dict[str, Any]:
            # Runs in the metadata executor — after the snapshot instant, before
            # the surface loop. Simulates the user closing the tab right here.
            channel_slots.note_slot_closed(dashboard_state, "slack_1.1")
            return orig_meta(key)

        log.get_metadata = _close_during_pass  # type: ignore[method-assign]
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 0
        assert "slack_1.1" not in dashboard_state._slots

    def test_a_close_just_before_the_snapshot_still_blocks(self, dashboard_state: Any) -> None:
        """The close handler pops the slot and writes the tombstone BEFORE its
        awaits (task cancellation, file lock), so a pass can snapshot still-open
        metadata after the tombstone exists. The tombstone must suppress by the
        disk flag's own rule (activity vs close instant), not by comparing
        against the pass's snapshot time."""
        # Channel activity is OLDER than the close — the dismissal stands.
        log = _FakeLog([_session("slack:1.1", modified=NOW - 60)], {})
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        # Tombstone written before the pass even starts (close save in flight,
        # disk metadata still open).
        channel_slots.note_slot_closed(dashboard_state, "slack_1.1")

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 0
        assert "slack_1.1" not in dashboard_state._slots

    def test_a_close_and_its_tombstone_check_agree_on_the_slot_key(
        self, dashboard_state: Any
    ) -> None:
        """The close path tombstones the key it popped from ``state._slots``;
        the surface loop re-derives that key from the channel session key. Both
        derivations must land on the same string or a dismissed tab silently
        reopens on the next pass. Driven through real keys on both sides — a
        hardcoded slot name on each side would agree by construction."""
        session = _session("discord:kirocrew:direct:U1", modified=NOW - 60)
        _map_stems(dashboard_state, "discord:kirocrew:direct:U1")
        dashboard_state.conversation_log = _FakeLog([session], {})
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        # Close it the way the tab-close handlers do: pop the slot, then
        # tombstone the key that was popped.
        (slot_key,) = list(dashboard_state._slots)
        dashboard_state._slots.pop(slot_key)
        channel_slots.note_slot_closed(dashboard_state, slot_key)

        assert channel_slots._tombstone_blocks(dashboard_state, session) is True
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 0
        assert dashboard_state._slots == {}

    def test_channel_activity_newer_than_the_tombstone_resurfaces(
        self, dashboard_state: Any
    ) -> None:
        """A tombstone follows the same outrun rule as the disk flag: channel
        activity strictly newer than the close re-surfaces the conversation."""
        channel_slots.note_slot_closed(dashboard_state, "slack_1.1")
        time.sleep(0.01)
        # Activity AFTER the close: the person kept talking on the channel.
        log = _FakeLog([_session("slack:1.1", modified=time.time() + 1)], {})
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        assert "slack_1.1" in dashboard_state._slots

    def test_close_tombstones_are_pruned(self, dashboard_state: Any) -> None:
        closes = channel_slots._RECENT_CLOSES.setdefault(dashboard_state, {})
        closes["ancient"] = time.time() - channel_slots._CLOSE_TOMBSTONE_TTL_SECS - 1
        channel_slots.note_slot_closed(dashboard_state, "fresh")
        assert "ancient" not in channel_slots._RECENT_CLOSES[dashboard_state]
        assert "fresh" in channel_slots._RECENT_CLOSES[dashboard_state]

    def test_surfacing_an_open_session_never_clears_a_flag(self, dashboard_state: Any) -> None:
        """The clear path only runs for sessions that were closed — an ordinary
        first surface must not invoke it at all, not even to spare a flag."""
        log = _FakeLog([_session("slack:1.1")], {})
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        assert log.clear_calls == []


class TestMtimeOf:
    def test_reports_the_session_file_mtime(self, dashboard_state: Any) -> None:
        log = dashboard_state.conversation_log
        assert log.mtime_of("slack:9.9") is None
        log.append("slack:9.9", "user", "hi")
        stamp = log.mtime_of("slack:9.9")
        assert stamp is not None and abs(stamp - time.time()) < 60


class TestCompareAndClear:
    """ConversationLog.clear_closed(only_if_closed_before=...) semantics."""

    def _write_closed(self, log: Any, key: str, closed_at: float | None) -> None:
        log.append(key, "user", "hi")
        meta: dict[str, Any] = {"closed": True}
        if closed_at is not None:
            meta["closed_at"] = closed_at
        log.update_metadata(key, meta)

    def test_stale_close_is_cleared(self, dashboard_state: Any) -> None:
        log = dashboard_state.conversation_log
        self._write_closed(log, "slack:1.1", time.time() - 600)
        log.clear_closed("slack:1.1", only_if_closed_before=time.time())
        meta = log.get_metadata("slack:1.1")
        assert "closed" not in meta and "closed_at" not in meta

    def test_fresh_close_survives(self, dashboard_state: Any) -> None:
        """A close at/after the cutoff is spared — the caller's snapshot is
        stale with respect to it."""
        log = dashboard_state.conversation_log
        stamp = time.time() + 600
        self._write_closed(log, "slack:1.1", stamp)
        log.clear_closed("slack:1.1", only_if_closed_before=time.time())
        assert log.get_metadata("slack:1.1").get("closed") is True

    def test_unconditional_clear_still_clears(self, dashboard_state: Any) -> None:
        """The resume path clears without a cutoff — unchanged behaviour."""
        log = dashboard_state.conversation_log
        self._write_closed(log, "slack:1.1", time.time() + 600)
        log.clear_closed("slack:1.1")
        assert "closed" not in log.get_metadata("slack:1.1")

    def test_legacy_flag_compares_against_file_mtime(self, dashboard_state: Any) -> None:
        """A pre-stamp flag falls back to the file's mtime as its close instant."""
        log = dashboard_state.conversation_log
        self._write_closed(log, "slack:1.1", None)
        # The write just happened, so mtime ~= now: a past cutoff spares it...
        log.clear_closed("slack:1.1", only_if_closed_before=time.time() - 600)
        assert log.get_metadata("slack:1.1").get("closed") is True
        # ...and a future cutoff clears it.
        log.clear_closed("slack:1.1", only_if_closed_before=time.time() + 600)
        assert "closed" not in log.get_metadata("slack:1.1")


class TestClosedAtStamp:
    """Closing a channel tab stamps WHEN, on the conversation's own transcript.

    The instant is what ``_close_stands`` compares channel activity against, and
    the file is the one ``eligible_channel_sessions`` reads its metadata from —
    a close written anywhere else would be invisible to the next pass.
    """

    def _bound_slot(self, tmp_path: Any, monkeypatch: Any) -> tuple[Any, Any]:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slack_1.1", linked_session_key="slack:1.1")
        slot.append("user", "hello")
        slot.drain()
        return state, slot

    def _channel_meta(self, tmp_path: Any) -> dict[str, Any]:
        """First line of the CHANNEL transcript — the file the tab writes to."""
        text = (tmp_path / "slack_1.1.jsonl").read_text(encoding="utf-8")
        return dict(json.loads(text.split("\n")[0]))

    def test_closing_save_stamps_closed_at_on_the_channel_transcript(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """With no caller-supplied instant the save falls back to save time
        (callers with no user gesture to anchor to)."""
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        state, slot = self._bound_slot(tmp_path, monkeypatch)

        before = time.time()
        _save_slot_to_history(state, slot, closed=True)
        after = time.time()

        meta = self._channel_meta(tmp_path)
        assert meta["closed"] is True
        assert before <= float(meta["closed_at"]) <= after

    def test_the_close_write_does_not_outrun_its_own_close_instant(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """Closing a tab must not immediately reopen it.

        The reconciler decides whether a close still stands by comparing the
        transcript's mtime against ``closed_at`` — activity newer than the close
        means the conversation moved on. Writing the close flag IS a write to the
        shared transcript, so without preserving the pre-close mtime the close
        outruns itself and the tab reopens on the next pass.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        state, slot = self._bound_slot(tmp_path, monkeypatch)
        path = tmp_path / "slack_1.1.jsonl"
        _save_slot_to_history(state, slot)  # a normal save: real activity
        mtime_before = path.stat().st_mtime

        click_instant = time.time()
        _save_slot_to_history(state, slot, closed=True, closed_at=click_instant)

        meta = self._channel_meta(tmp_path)
        assert meta["closed"] is True
        assert path.stat().st_mtime == pytest.approx(mtime_before)
        # The rule the reconciler applies: the close stands.
        assert channel_slots._close_stands(
            _session("slack_1.1", modified=path.stat().st_mtime),
            meta,
            {},
        )

    def test_caller_supplied_close_instant_is_persisted_verbatim(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """The close handler's save runs only after its awaits (task
        cancellation, patient lock acquire). Stamping save time would make
        channel activity that landed during that teardown window compare as
        OLDER than the close, hiding a conversation the reactivation rule should
        surface. The persisted closed_at must be the instant the user acted —
        the value note_slot_closed returned — not the (later) save time."""
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        state, slot = self._bound_slot(tmp_path, monkeypatch)

        click_instant = time.time() - 30.0  # user acted well before the save
        _save_slot_to_history(state, slot, closed=True, closed_at=click_instant)

        meta = self._channel_meta(tmp_path)
        assert meta["closed"] is True
        assert float(meta["closed_at"]) == click_instant

    def test_note_slot_closed_returns_the_recorded_instant(self, dashboard_state: Any) -> None:
        """The tombstone and the persisted closed_at must be the SAME instant —
        callers persist the return value, so the in-memory and on-disk close
        records cannot disagree about when the user acted."""
        before = time.time()
        returned = channel_slots.note_slot_closed(dashboard_state, "slack_1.1")
        after = time.time()
        assert before <= returned <= after
        assert channel_slots._RECENT_CLOSES[dashboard_state]["slack_1.1"] == returned

    def test_open_save_carries_no_close_fields(self, tmp_path: Any, monkeypatch: Any) -> None:
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        state, slot = self._bound_slot(tmp_path, monkeypatch)

        _save_slot_to_history(state, slot, closed=True)
        # A later save of the (reopened) slot drops both fields.
        _save_slot_to_history(state, slot)

        meta = self._channel_meta(tmp_path)
        assert "closed" not in meta
        assert "closed_at" not in meta


class TestReconcileMore:

    def test_a_steady_state_pass_re_reads_metadata_but_no_transcripts(
        self, dashboard_state: Any
    ) -> None:
        """A pass whose sessions all own slots still re-evaluates eligibility —
        so a close or a pin lands within one interval — but costs no transcript
        IO: the expensive read is scoped to sessions being surfaced."""
        log = _FakeLog([_session("slack:1.1")], {})
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        log.meta_reads.clear()
        log.message_reads.clear()
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 0
        assert log.meta_reads == ["slack:1.1"], "eligibility must be re-evaluated every pass"
        assert log.message_reads == [], "steady state must not re-read transcripts"

    def test_works_on_stem_form_keys_as_served_by_list_sessions(self, dashboard_state: Any) -> None:
        """list_sessions reports filename stems, where every ':' was folded to
        '_'. The tab binds to the key the session map resolves that stem back
        to, because the fold cannot be undone by guessing."""
        _map_stems(dashboard_state, "slack:1785370133.085469")
        dashboard_state.conversation_log = _FakeLog(
            [_session("slack_1785370133.085469"), _session("dashboard_chat-1-1")], {}
        )
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        slot = dashboard_state._slots["slack_1785370133.085469"]
        assert slot.linked_session_key == "slack:1785370133.085469"

    def test_no_conversation_log_is_a_no_op(self, dashboard_state: Any) -> None:
        dashboard_state.conversation_log = None
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 0

    def test_list_sessions_failure_is_swallowed(self, dashboard_state: Any) -> None:
        class Boom:
            def list_sessions(self) -> list[dict[str, Any]]:
                raise OSError("disk gone")

        dashboard_state.conversation_log = Boom()
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 0

    def test_one_bad_session_does_not_block_the_others(
        self, dashboard_state: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dashboard_state.conversation_log = _FakeLog(
            [_session("slack:1.1"), _session("slack:2.2")], {}
        )
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        real = channel_slots.surface_channel_session

        def flaky(state: Any, info: dict[str, Any], meta: Any, msgs: Any, **kw: Any) -> Any:
            if info["key"] == "slack:1.1":
                raise RuntimeError("boom")
            return real(state, info, meta, msgs, **kw)

        monkeypatch.setattr(channel_slots, "surface_channel_session", flaky)
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        assert "slack_2.2" in dashboard_state._slots


class TestImmediateDispatcherSurface:
    def test_reconciles_with_the_configured_restore_window(
        self, dashboard_state: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[Any, int]] = []

        async def fake_reconcile(state: Any, window_minutes: int) -> int:
            calls.append((state, window_minutes))
            return 1

        monkeypatch.setattr(channel_slots, "reconcile_channel_slots", fake_reconcile)
        dispatcher = SimpleNamespace(
            cfg=SimpleNamespace(
                dashboard=SimpleNamespace(
                    surface_channel_sessions=True,
                    restore_window_minutes=47,
                )
            ),
            dashboard_state=dashboard_state,
        )

        asyncio.run(channel_slots.surface_dispatcher_session(dispatcher))

        assert calls == [(dashboard_state, 47)]

    def test_respects_the_surface_channel_sessions_gate(
        self, dashboard_state: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = False

        async def fake_reconcile(state: Any, window_minutes: int) -> int:
            nonlocal called
            called = True
            return 1

        monkeypatch.setattr(channel_slots, "reconcile_channel_slots", fake_reconcile)
        dispatcher = SimpleNamespace(
            cfg=SimpleNamespace(
                dashboard=SimpleNamespace(
                    surface_channel_sessions=False,
                    restore_window_minutes=30,
                )
            ),
            dashboard_state=dashboard_state,
        )

        asyncio.run(channel_slots.surface_dispatcher_session(dispatcher))

        assert not called


class TestFailedTranscriptReadDefers:
    """A read failure must not look like an empty conversation.

    Surfacing an empty window gives the slot a zero frozen-prefix count, so the
    next dashboard reply writes its turns ahead of the history still on disk.
    ``_load_messages`` omits the key on failure precisely so the surface loop can
    tell the two apart.
    """

    @pytest.mark.asyncio
    async def test_a_failed_read_defers_surfacing(self, dashboard_state: Any) -> None:
        state = dashboard_state
        _map_stems(state, "slack:1.1")
        state.conversation_log.list_sessions = lambda: [_session("slack_1.1")]
        state.conversation_log.get_metadata = lambda key: {}

        def _boom(key: str) -> Any:
            raise OSError("transient read failure")

        state.conversation_log.read_messages = _boom
        assert await channel_slots.reconcile_channel_slots(state, 60) == 0
        assert "slack_1.1" not in state._slots

    @pytest.mark.asyncio
    async def test_a_genuinely_empty_transcript_still_surfaces(
        self, dashboard_state: Any
    ) -> None:
        state = dashboard_state
        _map_stems(state, "slack:1.1")
        state.conversation_log.list_sessions = lambda: [_session("slack_1.1")]
        state.conversation_log.get_metadata = lambda key: {}
        state.conversation_log.read_messages = lambda key: []
        assert await channel_slots.reconcile_channel_slots(state, 60) == 1
        assert "slack_1.1" in state._slots
