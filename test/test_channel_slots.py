"""Tests for surfacing channel-originated sessions as dashboard chat slots.

Covers the eligibility rules (recency window, closed, ephemeral memory modes,
pin/folder exemption), the slot-creation/binding behaviour, and the async
reconcile pass.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import channel_slots
from kiro_crew.messaging.link import (
    channel_namespace_of,
    is_channel_session_key,
)

NOW = time.time()


@pytest.fixture
def dashboard_state(tmp_path: Any) -> Any:
    """DashboardState with mocked services and a real (empty) ConversationLog."""
    return _make_state(tmp_path)


def _session(key: str, *, modified: float | None = None, **extra: Any) -> dict[str, Any]:
    return {"key": key, "title": "", "modified": modified if modified is not None else NOW, **extra}


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

    def test_closed_on_the_channel_key_is_never_resurfaced(self) -> None:
        """A close with no known instant must stick — fail toward the dismissal.

        (With a ``closed_at`` stamp or a file mtime the close can be outrun by
        newer channel activity — see ``TestCloseReactivation``.)
        """
        sessions = [_session("slack:1.1")]
        out = channel_slots.eligible_channel_sessions(
            sessions, metadata={"slack:1.1": {"closed": True}}, cutoff=NOW - 1800
        )
        assert out == []

    def test_closed_on_the_slot_key_is_never_resurfaced(self) -> None:
        """Closing the TAB writes `closed` to the slot key, not the channel key.

        Reading only the channel key is why a closed tab would reopen on the
        next 30s pass — the exact defect this asserts against.
        """
        sessions = [_session("slack:1.1")]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"dashboard:slack_1.1": {"closed": True}},
            cutoff=NOW - 1800,
        )
        assert out == []

    def test_pinned_on_the_slot_key_survives_the_window(self) -> None:
        sessions = [_session("slack:1.1", modified=NOW - 7200)]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"dashboard:slack_1.1": {"pinned": True}},
            cutoff=NOW - 1800,
        )
        assert len(out) == 1

    def test_ephemeral_on_the_slot_key_is_skipped(self) -> None:
        sessions = [_session("slack:1.1")]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"dashboard:slack_1.1": {"memory_mode": "incognito"}},
            cutoff=None,
        )
        assert out == []

    def test_slot_history_key_derivation(self) -> None:
        assert channel_slots.channel_slot_name("slack:1.1") == "slack_1.1"
        assert channel_slots.slot_history_key("slack:1.1") == "dashboard:slack_1.1"

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
            metadata={"dashboard:slack_1.1": {"closed": True, "closed_at": NOW - 600}},
            cutoff=None,
        )
        assert [s["key"] for s in out] == ["slack:1.1"]

    def test_close_newer_than_activity_stands(self) -> None:
        """No channel activity since the close — the dismissal holds."""
        sessions = [_session("slack:1.1", modified=NOW - 600)]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"dashboard:slack_1.1": {"closed": True, "closed_at": NOW}},
            cutoff=None,
        )
        assert out == []

    def test_close_at_exactly_the_activity_instant_stands(self) -> None:
        """Strictly-newer comparison: a tie is not new activity."""
        sessions = [_session("slack:1.1", modified=NOW)]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"dashboard:slack_1.1": {"closed": True, "closed_at": NOW}},
            cutoff=None,
        )
        assert out == []

    def test_legacy_close_falls_back_to_file_mtime(self) -> None:
        """A pre-stamp `closed` flag uses the slot file's mtime as the close
        instant — the closing save is what last wrote that file."""
        sessions = [_session("slack:1.1", modified=NOW)]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"dashboard:slack_1.1": {"closed": True}},
            cutoff=None,
            mtimes={"dashboard:slack_1.1": NOW - 600},
        )
        assert [s["key"] for s in out] == ["slack:1.1"]

    def test_legacy_close_with_stale_mtime_stands(self) -> None:
        sessions = [_session("slack:1.1", modified=NOW - 600)]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"dashboard:slack_1.1": {"closed": True}},
            cutoff=None,
            mtimes={"dashboard:slack_1.1": NOW},
        )
        assert out == []

    def test_garbage_closed_at_falls_back_to_mtime(self) -> None:
        sessions = [_session("slack:1.1", modified=NOW)]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"dashboard:slack_1.1": {"closed": True, "closed_at": "not-a-number"}},
            cutoff=None,
            mtimes={"dashboard:slack_1.1": NOW - 600},
        )
        assert len(out) == 1

    def test_every_closed_side_must_be_outrun(self) -> None:
        """Slot-side close is outdated, but the channel-side close instant is
        unknown — the unknown side keeps the dismissal standing."""
        sessions = [_session("slack:1.1", modified=NOW)]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={
                "slack:1.1": {"closed": True},
                "dashboard:slack_1.1": {"closed": True, "closed_at": NOW - 600},
            },
            cutoff=None,
        )
        assert out == []

    def test_reactivated_session_still_respects_the_recency_window(self) -> None:
        """Outrunning the close does not exempt a session from the cutoff."""
        sessions = [_session("slack:1.1", modified=NOW - 7200)]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"dashboard:slack_1.1": {"closed": True, "closed_at": NOW - 99999}},
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

    def test_does_not_bind_to_the_channel_session_key(self, dashboard_state: Any) -> None:
        """One history key only.

        `_save_slot_to_history` always writes `dashboard:<slot.key>`. Binding the
        RUN path to the channel key would split the conversation across two
        transcripts and send the `closed` flag to a key the reconciler never
        reads, so a closed tab would reopen.
        """
        slot = channel_slots.surface_channel_session(dashboard_state, _session("slack:1.1"), {}, [])
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

    def test_leaves_an_existing_slot_untouched(self, dashboard_state: Any) -> None:
        """A slot restored from open_slots.json already owns its transcript —
        re-hydrating would duplicate messages on top of it."""
        existing = dashboard_state.get_or_create_slot(name="slack_1.1")
        existing.append("user", "already here", "msg msg-u", broadcast=False)
        existing.drain()
        assert (
            channel_slots.surface_channel_session(
                dashboard_state,
                _session("slack:1.1"),
                {},
                [{"role": "user", "content": "should not be duplicated"}],
            )
            is None
        )
        assert [m["content"] for m in existing.messages] == ["already here"]

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
        self.message_reads: list[str] = []
        #: key -> file mtime, consulted as the fallback close instant. Unset
        #: keys report None (file absent), which keeps a legacy close standing.
        self.mtimes: dict[str, float] = {}
        #: keys clear_closed was invoked for, in order.
        self.cleared: list[str] = []
        #: every clear_closed invocation: (key, only_if_closed_before, outcome).
        self.clear_calls: list[tuple[str, float | None, str]] = []
        #: key -> transcript. Unset keys read empty, so the reconciler's
        #: slot-key-then-channel-key preference order is observable.
        self.transcripts: dict[str, list[dict[str, Any]]] = {
            s["key"]: [{"role": "user", "content": f"msg for {s['key']}"}] for s in sessions
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        return list(self._sessions)

    def get_metadata(self, key: str) -> dict[str, Any]:
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
        """End-to-end guard for the reopen defect: `closed` lives on the SLOT key."""
        log = _FakeLog([_session("slack:1.1")], {"dashboard:slack_1.1": {"closed": True}})
        # The close instant equals the last channel activity — no new activity.
        log.mtimes["dashboard:slack_1.1"] = NOW
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 0
        assert dashboard_state._slots == {}
        assert log.cleared == []

    def test_channel_activity_after_close_reopens_and_clears_flags(
        self, dashboard_state: Any
    ) -> None:
        """New channel activity outruns the close: the tab comes back, and the
        stale `closed`/`closed_at` flags are dropped from BOTH keys so restore
        paths and future passes agree the conversation is open."""
        log = _FakeLog(
            [_session("slack:1.1", modified=NOW)],
            {"dashboard:slack_1.1": {"closed": True, "closed_at": NOW - 600}},
        )
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        assert "slack_1.1" in dashboard_state._slots
        assert set(log.cleared) == {"slack:1.1", "dashboard:slack_1.1"}
        assert "closed" not in log._meta["dashboard:slack_1.1"]

    def test_legacy_close_reopens_via_file_mtime_fallback(self, dashboard_state: Any) -> None:
        """A pre-stamp `closed` flag (no closed_at) reactivates off the slot
        file's mtime — the real-world shape of sessions closed before this fix."""
        log = _FakeLog(
            [_session("slack:1.1", modified=NOW)],
            {"dashboard:slack_1.1": {"closed": True}},
        )
        log.mtimes["dashboard:slack_1.1"] = NOW - 600
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        assert "slack_1.1" in dashboard_state._slots
        assert set(log.cleared) == {"slack:1.1", "dashboard:slack_1.1"}

    def test_stale_flags_cleared_before_the_slot_is_visible(self, dashboard_state: Any) -> None:
        """Regression (GPT round 1): clearing AFTER the slot broadcast races a
        user closing the just-reactivated tab — the deferred clear would erase
        the fresh `closed` and the next pass would reopen a tab the user just
        dismissed. The clear must complete before the slot exists."""
        log = _FakeLog(
            [_session("slack:1.1", modified=NOW)],
            {"dashboard:slack_1.1": {"closed": True, "closed_at": NOW - 600}},
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
        """Regression (GPT round 2): the reconciler must pass its snapshot
        instant as a compare-and-clear cutoff, so a `closed` written after the
        snapshot (user dismissal mid-pass, racing writer) survives the clear."""
        log = _FakeLog(
            [_session("slack:1.1", modified=NOW)],
            {"dashboard:slack_1.1": {"closed": True, "closed_at": NOW - 600}},
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
            {"dashboard:slack_1.1": {"closed": True, "closed_at": NOW - 600}},
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
        assert log._meta["dashboard:slack_1.1"].get("closed") is True, (
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
        """Regression (GPT round 3): a tab resumed from History and closed
        while this pass's executor work is in flight pops the slot, so the
        stale `pending` verdict would recreate it. The close path's synchronous
        tombstone must be honored after the pass's last await."""
        log = _FakeLog([_session("slack:1.1", modified=NOW)], {})
        orig_read = log.read_messages

        def _close_during_pass(key: str) -> list[dict[str, Any]]:
            # Runs in the _load_messages executor — after the snapshot, before
            # the surface loop. Simulates the user closing the tab right here.
            channel_slots.note_slot_closed(dashboard_state, "slack_1.1")
            return orig_read(key)

        log.read_messages = _close_during_pass  # type: ignore[method-assign]
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 0
        assert "slack_1.1" not in dashboard_state._slots

    def test_a_close_just_before_the_snapshot_still_blocks(self, dashboard_state: Any) -> None:
        """Regression (GPT round 4): the close handler pops the slot and writes
        the tombstone BEFORE its awaits (task cancellation, file lock), so a
        pass can snapshot still-open metadata after the tombstone exists. The
        tombstone must suppress by the disk flag's own rule (activity vs close
        instant), not by comparing against the pass's snapshot time."""
        # Channel activity is OLDER than the close — the dismissal stands.
        log = _FakeLog([_session("slack:1.1", modified=NOW - 60)], {})
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        # Tombstone written before the pass even starts (close save in flight,
        # disk metadata still open).
        channel_slots.note_slot_closed(dashboard_state, "slack_1.1")

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 0
        assert "slack_1.1" not in dashboard_state._slots

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

    def test_surfacing_an_open_session_clears_nothing(self, dashboard_state: Any) -> None:
        """The clear path only runs for sessions that were closed — an ordinary
        first surface must not touch metadata."""
        log = _FakeLog([_session("slack:1.1")], {})
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        assert log.cleared == []

    def test_seeds_from_both_transcripts_merged(self, dashboard_state: Any) -> None:
        """Neither side is a superset: the slot holds dashboard replies, the channel
        holds anything said on the channel after surfacing. Re-surfacing needs both."""
        log = _FakeLog([_session("slack:1.1")], {})
        log.transcripts["slack:1.1"] = [
            {"role": "user", "content": "on slack first", "ts": "2026-07-30T00:00:00Z"},
            {"role": "user", "content": "on slack later", "ts": "2026-07-30T00:03:00Z"},
        ]
        log.transcripts["dashboard:slack_1.1"] = [
            {"role": "user", "content": "on slack first", "ts": "2026-07-30T00:00:00Z"},
            {"role": "user", "content": "from the dashboard", "ts": "2026-07-30T00:01:00Z"},
        ]
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        assert [m["content"] for m in dashboard_state._slots["slack_1.1"].messages] == [
            "on slack first",
            "from the dashboard",
            "on slack later",
        ]


@contextmanager
def _tz(name: str) -> Iterator[None]:
    """Run the block with the process's local timezone set to *name*.

    ``datetime.astimezone()`` on a naive value reads the process zone, which is
    precisely what makes an un-suffixed channel timestamp ambiguous.
    """
    prev = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev
        time.tzset()


#: ``time.tzset`` is Unix-only, and CI also runs the backend suite on Windows.
_needs_tzset = pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset() is Unix-only")


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
        self._write_closed(log, "dashboard:slack_1.1", time.time() - 600)
        log.clear_closed("dashboard:slack_1.1", only_if_closed_before=time.time())
        meta = log.get_metadata("dashboard:slack_1.1")
        assert "closed" not in meta and "closed_at" not in meta

    def test_fresh_close_survives(self, dashboard_state: Any) -> None:
        """A close at/after the cutoff is spared — the caller's snapshot is
        stale with respect to it."""
        log = dashboard_state.conversation_log
        stamp = time.time() + 600
        self._write_closed(log, "dashboard:slack_1.1", stamp)
        log.clear_closed("dashboard:slack_1.1", only_if_closed_before=time.time())
        assert log.get_metadata("dashboard:slack_1.1").get("closed") is True

    def test_unconditional_clear_still_clears(self, dashboard_state: Any) -> None:
        """The resume path clears without a cutoff — unchanged behaviour."""
        log = dashboard_state.conversation_log
        self._write_closed(log, "dashboard:slack_1.1", time.time() + 600)
        log.clear_closed("dashboard:slack_1.1")
        assert "closed" not in log.get_metadata("dashboard:slack_1.1")

    def test_legacy_flag_compares_against_file_mtime(self, dashboard_state: Any) -> None:
        """A pre-stamp flag falls back to the file's mtime as its close instant."""
        log = dashboard_state.conversation_log
        self._write_closed(log, "dashboard:slack_1.1", None)
        # The write just happened, so mtime ~= now: a past cutoff spares it...
        log.clear_closed("dashboard:slack_1.1", only_if_closed_before=time.time() - 600)
        assert log.get_metadata("dashboard:slack_1.1").get("closed") is True
        # ...and a future cutoff clears it.
        log.clear_closed("dashboard:slack_1.1", only_if_closed_before=time.time() + 600)
        assert "closed" not in log.get_metadata("dashboard:slack_1.1")


class TestClosedAtStamp:
    def test_closing_save_stamps_closed_at(self, tmp_path: Any, monkeypatch: Any) -> None:
        """Closing a tab records WHEN — the instant _close_stands compares
        channel activity against. With no caller-supplied instant, the save
        falls back to save time (callers with no user gesture to anchor to)."""
        import json

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slack_1.1")
        slot.append("user", "hello")
        slot.drain()

        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        before = time.time()
        _save_slot_to_history(state, slot, closed=True)
        after = time.time()

        meta = json.loads(
            (tmp_path / "dashboard_slack_1.1.jsonl").read_text(encoding="utf-8").split("\n")[0]
        )
        assert meta["closed"] is True
        assert before <= float(meta["closed_at"]) <= after

    def test_caller_supplied_close_instant_is_persisted_verbatim(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """Regression (GPT round 5): the close handler's save runs only after
        its awaits (task cancellation, patient lock acquire). Stamping save
        time would make channel activity that landed during that teardown
        window compare as OLDER than the close, hiding a conversation the
        reactivation rule should surface. The persisted closed_at must be the
        instant the user acted — the value note_slot_closed returned — not the
        (later) save time."""
        import json

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slack_1.1")
        slot.append("user", "hello")
        slot.drain()

        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        click_instant = time.time() - 30.0  # user acted well before the save
        _save_slot_to_history(state, slot, closed=True, closed_at=click_instant)

        meta = json.loads(
            (tmp_path / "dashboard_slack_1.1.jsonl").read_text(encoding="utf-8").split("\n")[0]
        )
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
        import json

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slack_1.1")
        slot.append("user", "hello")
        slot.drain()

        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        _save_slot_to_history(state, slot, closed=True)
        # A later save of the (reopened) slot drops both fields.
        _save_slot_to_history(state, slot)

        meta = json.loads(
            (tmp_path / "dashboard_slack_1.1.jsonl").read_text(encoding="utf-8").split("\n")[0]
        )
        assert "closed" not in meta
        assert "closed_at" not in meta


class TestMergeTranscriptTimezones:
    """The two sides of a merge do not agree on timezone.

    The dashboard writes ISO-8601 UTC; a channel turn can arrive as a naive
    local-time string. Comparing those as text interleaves them wrongly by the
    host's UTC offset, so ordering has to run on absolute instants.
    """

    @_needs_tzset
    def test_naive_local_and_utc_interleave_chronologically(self) -> None:
        # 12:30 in a UTC-07:00 zone is 19:30Z — later than 19:00Z, even though
        # "2026-07-30T12:30:00" sorts BEFORE "2026-07-30T19:00:00+00:00".
        slot = [{"role": "assistant", "content": "utc reply", "ts": "2026-07-30T19:00:00+00:00"}]
        chan = [{"role": "user", "content": "naive local", "ts": "2026-07-30T12:30:00"}]
        with _tz("US/Pacific"):
            out = channel_slots.merge_transcripts(slot, chan)
        assert [m["content"] for m in out] == ["utc reply", "naive local"]

    def test_lexicographic_order_would_have_been_wrong(self) -> None:
        """Pin the regression: text order disagrees with chronological order."""
        naive, utc = "2026-07-30T12:30:00", "2026-07-30T19:00:00+00:00"
        assert naive < utc, "if this stops holding the test above proves nothing"

    def test_zulu_and_offset_spellings_of_one_instant_are_ordered_together(self) -> None:
        slot = [{"role": "user", "content": "second", "ts": "2026-07-30T00:00:01+00:00"}]
        chan = [{"role": "user", "content": "first", "ts": "2026-07-30T00:00:00Z"}]
        out = channel_slots.merge_transcripts(slot, chan)
        assert [m["content"] for m in out] == ["first", "second"]

    def test_unparseable_timestamps_sort_before_untimestamped_and_keep_order(self) -> None:
        chan = [
            {"role": "user", "content": "none"},
            {"role": "user", "content": "junk-b", "ts": "not-a-date-b"},
            {"role": "user", "content": "real", "ts": "2026-07-30T00:00:00Z"},
            {"role": "user", "content": "junk-a", "ts": "not-a-date-a"},
        ]
        out = channel_slots.merge_transcripts([], chan)
        assert [m["content"] for m in out] == ["real", "junk-a", "junk-b", "none"]


class TestFrozenPrefixAccounting:
    """A seeded slot must declare how much of its history file it did NOT load.

    ``_save_slot_to_history`` writes ``frozen prefix + serialize(window)``. Left
    at 0, the first save re-emits the omitted older lines AFTER the newer ones.
    """

    def _slot_msgs(self, n: int) -> list[dict[str, Any]]:
        return [
            {"role": "user", "content": f"m{i}", "ts": f"2026-07-30T00:{i:02d}:00Z"}
            for i in range(n)
        ]

    def test_short_transcript_has_no_prefix(self) -> None:
        msgs = self._slot_msgs(3)
        assert channel_slots.frozen_prefix_len(msgs, channel_slots.hydrate_window(msgs)) == 0

    def test_counts_only_the_lines_the_window_omits(self) -> None:
        msgs = self._slot_msgs(channel_slots._HYDRATE_LIMIT + 12)
        window = channel_slots.hydrate_window(msgs)
        assert len(window) == channel_slots._HYDRATE_LIMIT
        assert channel_slots.frozen_prefix_len(msgs, window) == 12

    def test_channel_only_window_freezes_the_whole_file(self) -> None:
        """No slot line survived into the window → every one of them is prefix."""
        msgs = self._slot_msgs(4)
        assert channel_slots.frozen_prefix_len(msgs, [{"role": "user", "content": "x"}]) == 4

    def test_empty_file_has_no_prefix(self) -> None:
        assert channel_slots.frozen_prefix_len([], [{"role": "user", "content": "x"}]) == 0

    def test_seeded_slot_reports_the_prefix_and_only_its_own_disk_lines(
        self, dashboard_state: Any
    ) -> None:
        """End-to-end: an over-long slot file plus later channel turns.

        ``_disk_window_len`` must count the slot's OWN persisted lines only —
        crediting the merged channel turns would let a trim fold turns that were
        never written into the frozen prefix.
        """
        over = 7
        slot_msgs = self._slot_msgs(channel_slots._HYDRATE_LIMIT + over)
        log = _FakeLog([_session("slack:1.1")], {})
        log.transcripts["dashboard:slack_1.1"] = slot_msgs
        log.transcripts["slack:1.1"] = [
            {"role": "user", "content": "said on slack after", "ts": "2026-07-30T23:00:00Z"}
        ]
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        slot = dashboard_state._slots["slack_1.1"]
        # The channel turn is newest, so it takes the last window seat and one
        # more slot line drops into the prefix.
        assert slot._disk_older_count == over + 1
        assert slot._disk_window_len == len(slot_msgs) - (over + 1)
        assert slot._disk_older_count + slot._disk_window_len == len(slot_msgs)
        assert len(slot.messages) == channel_slots._HYDRATE_LIMIT
        assert slot.messages[-1]["content"] == "said on slack after"

    def test_empty_content_lines_are_not_seeded_but_still_counted(self) -> None:
        """A blank on-disk line is never appended, so it belongs to the prefix."""
        msgs = [
            {"role": "user", "content": "", "ts": "2026-07-30T00:00:00Z"},
            {"role": "user", "content": "kept", "ts": "2026-07-30T00:01:00Z"},
        ]
        window = channel_slots.hydrate_window(msgs)
        assert [m["content"] for m in window] == ["kept"]
        assert channel_slots.frozen_prefix_len(msgs, window) == 1


class TestMergeTranscripts:
    def test_orders_by_timestamp_across_sources(self) -> None:
        slot = [{"role": "user", "content": "b", "ts": "2026-07-30T00:02:00Z"}]
        chan = [
            {"role": "user", "content": "a", "ts": "2026-07-30T00:01:00Z"},
            {"role": "user", "content": "c", "ts": "2026-07-30T00:03:00Z"},
        ]
        out = channel_slots.merge_transcripts(slot, chan)
        assert [m["content"] for m in out] == ["a", "b", "c"]

    def test_drops_duplicates_already_copied_into_the_slot(self) -> None:
        msg = {"role": "user", "content": "same", "ts": "2026-07-30T00:01:00Z"}
        out = channel_slots.merge_transcripts([dict(msg)], [dict(msg)])
        assert len(out) == 1

    def test_keeps_same_text_at_different_times(self) -> None:
        out = channel_slots.merge_transcripts(
            [{"role": "user", "content": "ping", "ts": "2026-07-30T00:01:00Z"}],
            [{"role": "user", "content": "ping", "ts": "2026-07-30T00:09:00Z"}],
        )
        assert len(out) == 2

    def test_untimestamped_messages_sort_last_and_keep_order(self) -> None:
        out = channel_slots.merge_transcripts(
            [{"role": "user", "content": "no ts 1"}, {"role": "user", "content": "no ts 2"}],
            [{"role": "user", "content": "has ts", "ts": "2026-07-30T00:01:00Z"}],
        )
        assert [m["content"] for m in out] == ["has ts", "no ts 1", "no ts 2"]

    def test_empty_sources(self) -> None:
        assert channel_slots.merge_transcripts([], []) == []


class TestReconcileMore:

    def test_second_pass_is_a_no_op_and_reads_no_transcripts(self, dashboard_state: Any) -> None:
        log = _FakeLog([_session("slack:1.1")], {})
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        log.message_reads.clear()
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 0
        assert log.message_reads == [], "steady state must not re-read transcripts"

    def test_works_on_stem_form_keys_as_served_by_list_sessions(self, dashboard_state: Any) -> None:
        dashboard_state.conversation_log = _FakeLog(
            [_session("slack_1785370133.085469"), _session("dashboard_chat-1-1")], {}
        )
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        slot = dashboard_state._slots["slack_1785370133.085469"]
        assert slot.linked_session_key == "", "one history key only"

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


class TestMirrorHelpers:
    """Pure-function rules for mirror-until-forked."""

    def test_is_pure_mirror_is_redaction_stable(self) -> None:
        """Slot copies are stored redacted while the channel file is raw.

        Comparing raw identities would mis-classify every redacted turn as
        dashboard-authored and permanently disarm the mirror.
        """
        raw = "creds AKIAIOSFODNN7EXAMPLE leaked"
        chan = [{"role": "user", "content": raw, "ts": "2026-07-30T00:00:00Z"}]
        slot = [
            {
                "role": "user",
                "content": channel_slots._redact(raw),
                "ts": "2026-07-30T00:00:00Z",
            }
        ]
        assert channel_slots.is_pure_mirror(slot, chan)

    def test_a_dashboard_authored_turn_breaks_purity(self) -> None:
        chan = [{"role": "user", "content": "on slack", "ts": "2026-07-30T00:00:00Z"}]
        slot = chan + [
            {"role": "user", "content": "from the dashboard", "ts": "2026-07-30T00:01:00Z"}
        ]
        assert not channel_slots.is_pure_mirror(slot, chan)

    def test_an_empty_slot_is_a_pure_mirror(self) -> None:
        assert channel_slots.is_pure_mirror([], [{"role": "user", "content": "x", "ts": "t"}])

    def test_new_messages_are_ordered_and_deduped(self) -> None:
        slot = [{"role": "user", "content": "first", "ts": "2026-07-30T00:00:00Z"}]
        chan = [
            {"role": "assistant", "content": "third", "ts": "2026-07-30T00:02:00Z"},
            {"role": "user", "content": "first", "ts": "2026-07-30T00:00:00Z"},
            {"role": "assistant", "content": "second", "ts": "2026-07-30T00:01:00Z"},
        ]
        out = channel_slots.mirror_new_messages(slot, chan)
        assert [m["content"] for m in out] == ["second", "third"]

    def test_pre_window_history_is_not_replayed(self) -> None:
        """The seed window is a capped tail; older turns must never append
        AFTER newer ones — that would scramble the visible order."""
        slot = [{"role": "user", "content": "newest", "ts": "2026-07-30T00:05:00Z"}]
        chan = [
            {"role": "user", "content": "ancient", "ts": "2026-07-30T00:00:00Z"},
            {"role": "user", "content": "newest", "ts": "2026-07-30T00:05:00Z"},
            {"role": "user", "content": "after", "ts": "2026-07-30T00:06:00Z"},
        ]
        assert [m["content"] for m in channel_slots.mirror_new_messages(slot, chan)] == ["after"]

    def test_unplaceable_and_empty_turns_are_skipped(self) -> None:
        slot = [{"role": "user", "content": "a", "ts": "2026-07-30T00:00:00Z"}]
        chan = slot + [
            {"role": "user", "content": "no ts at all"},
            {"role": "user", "content": "garbled", "ts": "not-a-timestamp"},
            {"role": "user", "content": "", "ts": "2026-07-30T00:01:00Z"},
            {"role": "user", "content": "kept", "ts": "2026-07-30T00:02:00Z"},
        ]
        assert [m["content"] for m in channel_slots.mirror_new_messages(slot, chan)] == ["kept"]


class TestMirrorUntilForked:
    """End-to-end reconcile behaviour: a surfaced slot stays current until the
    user picks the conversation up on the dashboard."""

    def _surface(self, dashboard_state: Any, log: _FakeLog) -> Any:
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        return dashboard_state._slots["slack_1.1"]

    def test_new_channel_turns_flow_into_the_surfaced_slot(self, dashboard_state: Any) -> None:
        sess = _session("slack:1.1", modified=NOW - 60)
        log = _FakeLog([sess], {})
        log.transcripts["slack:1.1"] = [
            {"role": "user", "content": "first", "ts": "2026-07-30T00:00:00Z"}
        ]
        slot = self._surface(dashboard_state, log)
        assert [m["content"] for m in slot.messages] == ["first"]
        assert slot._channel_mirror_key == "slack:1.1"

        log.transcripts["slack:1.1"].append(
            {"role": "assistant", "content": "second", "ts": "2026-07-30T00:01:00Z"}
        )
        sess["modified"] = NOW
        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert [m["content"] for m in slot.messages] == ["first", "second"]

    def test_mirroring_alone_never_marks_the_slot_dirty(self, dashboard_state: Any) -> None:
        """Same rule as the seed: a conversation the user never touches costs
        no write. The mirrored turns persist with the first real save."""
        sess = _session("slack:1.1", modified=NOW - 60)
        log = _FakeLog([sess], {})
        log.transcripts["slack:1.1"] = [
            {"role": "user", "content": "first", "ts": "2026-07-30T00:00:00Z"}
        ]
        slot = self._surface(dashboard_state, log)
        log.transcripts["slack:1.1"].append(
            {"role": "assistant", "content": "second", "ts": "2026-07-30T00:01:00Z"}
        )
        sess["modified"] = NOW
        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert len(slot.messages) == 2
        assert slot._dirty is False
        # Counted as history, not novel dashboard turns.
        assert slot._resumed_count == len(slot.messages)

    def test_mirrored_content_is_redacted(self, dashboard_state: Any) -> None:
        sess = _session("slack:1.1", modified=NOW - 60)
        log = _FakeLog([sess], {})
        log.transcripts["slack:1.1"] = [
            {"role": "user", "content": "hello", "ts": "2026-07-30T00:00:00Z"}
        ]
        slot = self._surface(dashboard_state, log)
        log.transcripts["slack:1.1"].append(
            {
                "role": "assistant",
                "content": "creds AKIAIOSFODNN7EXAMPLE leaked",
                "ts": "2026-07-30T00:01:00Z",
            }
        )
        sess["modified"] = NOW
        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert len(slot.messages) == 2
        assert "AKIAIOSFODNN7EXAMPLE" not in slot.messages[1]["content"]

    def test_a_dashboard_turn_forks_and_stops_the_mirror(self, dashboard_state: Any) -> None:
        sess = _session("slack:1.1", modified=NOW - 60)
        log = _FakeLog([sess], {})
        log.transcripts["slack:1.1"] = [
            {"role": "user", "content": "first", "ts": "2026-07-30T00:00:00Z"}
        ]
        slot = self._surface(dashboard_state, log)

        # The user picks the conversation up on the dashboard.
        slot.append("user", "picked up here", "msg msg-u")
        log.transcripts["slack:1.1"].append(
            {"role": "user", "content": "meanwhile on slack", "ts": "2026-07-30T00:02:00Z"}
        )
        sess["modified"] = NOW
        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        contents = [m["content"] for m in slot.messages]
        assert "meanwhile on slack" not in contents
        assert slot._channel_mirror_key == ""

        # And it stays off: later passes never re-arm a forked slot.
        sess["modified"] = NOW + 60
        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert "meanwhile on slack" not in [m["content"] for m in slot.messages]

    def test_a_forked_on_disk_transcript_never_arms_the_mirror(
        self, dashboard_state: Any
    ) -> None:
        """A slot re-surfaced from a fork file that holds dashboard replies is
        already diverged — seeding merges, but mirroring must not arm."""
        sess = _session("slack:1.1", modified=NOW - 60)
        log = _FakeLog([sess], {})
        log.transcripts["slack:1.1"] = [
            {"role": "user", "content": "on slack", "ts": "2026-07-30T00:00:00Z"}
        ]
        log.transcripts["dashboard:slack_1.1"] = [
            {"role": "user", "content": "on slack", "ts": "2026-07-30T00:00:00Z"},
            {"role": "user", "content": "from the dashboard", "ts": "2026-07-30T00:01:00Z"},
        ]
        slot = self._surface(dashboard_state, log)
        assert slot._channel_mirror_key == ""

        log.transcripts["slack:1.1"].append(
            {"role": "user", "content": "later on slack", "ts": "2026-07-30T00:02:00Z"}
        )
        sess["modified"] = NOW
        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert "later on slack" not in [m["content"] for m in slot.messages]

    def test_restored_pure_slot_rearms_and_catches_up(self, dashboard_state: Any) -> None:
        """After a gateway restart the restore path rebuilds the slot without
        mirror state; the reconciler settles it once and catches the tab up."""
        sess = _session("slack:1.1")
        log = _FakeLog([sess], {})
        log.transcripts["slack:1.1"] = [
            {"role": "user", "content": "first", "ts": "2026-07-30T00:00:00Z"},
            {"role": "assistant", "content": "second", "ts": "2026-07-30T00:01:00Z"},
        ]
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        slot = dashboard_state.get_or_create_slot(name="slack_1.1")
        slot.append("user", "first", "msg msg-u", ts="2026-07-30T00:00:00Z", broadcast=False)
        slot._dirty = False

        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert [m["content"] for m in slot.messages] == ["first", "second"]
        assert slot._channel_mirror_key == "slack:1.1"
        assert slot._channel_mirror_checked is True

    def test_restored_slot_with_a_frozen_prefix_never_arms(self, dashboard_state: Any) -> None:
        """Purity can only be judged when the whole file is in the window — a
        frozen prefix may hide dashboard-authored turns, so fail safe."""
        sess = _session("slack:1.1")
        log = _FakeLog([sess], {})
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        slot = dashboard_state.get_or_create_slot(name="slack_1.1")
        slot._disk_older_count = 3

        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert slot._channel_mirror_key == ""
        assert slot._channel_mirror_checked is True
        # Settled without a transcript read, and never re-read on later passes.
        assert log.message_reads == []
        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert log.message_reads == []

    def test_unchanged_channel_file_costs_no_transcript_read(self, dashboard_state: Any) -> None:
        """Steady state stays cheap: the watermark gates the disk IO."""
        sess = _session("slack:1.1", modified=NOW - 60)
        log = _FakeLog([sess], {})
        log.transcripts["slack:1.1"] = [
            {"role": "user", "content": "first", "ts": "2026-07-30T00:00:00Z"}
        ]
        self._surface(dashboard_state, log)
        reads_after_surface = len(log.message_reads)
        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert len(log.message_reads) == reads_after_surface

    def test_mirror_pass_broadcasts_the_slots_update(self, dashboard_state: Any) -> None:
        sess = _session("slack:1.1", modified=NOW - 60)
        log = _FakeLog([sess], {})
        log.transcripts["slack:1.1"] = [
            {"role": "user", "content": "first", "ts": "2026-07-30T00:00:00Z"}
        ]
        slot = self._surface(dashboard_state, log)
        pushes: list[int] = []
        dashboard_state.push_slots_update = lambda: pushes.append(1)  # type: ignore[method-assign]
        log.transcripts["slack:1.1"].append(
            {"role": "assistant", "content": "second", "ts": "2026-07-30T00:01:00Z"}
        )
        sess["modified"] = NOW
        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert len(slot.messages) == 2
        assert pushes, "a mirror-only pass must still refresh the sidebar"


class TestMirrorForkAndFailureModes:
    """Regression guards for the two review findings: net-zero edits must fork,
    and failed reads must not count as successful syncs."""

    def _surface(self, dashboard_state: Any, log: _FakeLog) -> Any:
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        return dashboard_state._slots["slack_1.1"]

    def test_a_net_zero_edit_forks_the_mirror(self, dashboard_state: Any) -> None:
        """Regenerate replaces a message WITHOUT changing the count and resets
        _resumed_count to 0 — the length check alone misses it, and mirroring
        on would interleave channel turns into the diverged branch."""
        sess = _session("slack:1.1", modified=NOW - 60)
        log = _FakeLog([sess], {})
        log.transcripts["slack:1.1"] = [
            {"role": "user", "content": "first", "ts": "2026-07-30T00:00:00Z"},
            {"role": "assistant", "content": "reply", "ts": "2026-07-30T00:00:30Z"},
        ]
        slot = self._surface(dashboard_state, log)
        assert slot._channel_mirror_key == "slack:1.1"

        # Simulate regenerate: in-place replacement, same count, resumed reset.
        slot.messages[-1]["content"] = "regenerated reply"
        slot._resumed_count = 0

        log.transcripts["slack:1.1"].append(
            {"role": "user", "content": "meanwhile on slack", "ts": "2026-07-30T00:02:00Z"}
        )
        sess["modified"] = NOW
        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert "meanwhile on slack" not in [m["content"] for m in slot.messages]
        assert slot._channel_mirror_key == ""

    def test_a_failed_mirror_read_is_retried_not_recorded(self, dashboard_state: Any) -> None:
        """A transient read failure must not advance the watermark — otherwise
        an unchanged channel file is never retried and its turns stay missing."""
        sess = _session("slack:1.1", modified=NOW - 60)
        log = _FakeLog([sess], {})
        log.transcripts["slack:1.1"] = [
            {"role": "user", "content": "first", "ts": "2026-07-30T00:00:00Z"}
        ]
        slot = self._surface(dashboard_state, log)
        log.transcripts["slack:1.1"].append(
            {"role": "assistant", "content": "second", "ts": "2026-07-30T00:01:00Z"}
        )
        sess["modified"] = NOW

        fail_once = {"armed": True}
        orig_read = log.read_messages

        def flaky(key: str) -> list[dict[str, Any]]:
            if fail_once["armed"] and key == "slack:1.1":
                fail_once["armed"] = False
                raise OSError("transient")
            return orig_read(key)

        log.read_messages = flaky  # type: ignore[method-assign]

        # Failing pass: nothing copied, mirror still armed, watermark untouched.
        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert [m["content"] for m in slot.messages] == ["first"]
        assert slot._channel_mirror_key == "slack:1.1"

        # Next pass with the SAME modified stamp must retry and catch up.
        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert [m["content"] for m in slot.messages] == ["first", "second"]

    def test_a_failed_channel_read_at_surface_does_not_arm(self, dashboard_state: Any) -> None:
        """Purity cannot be judged from a failed read — surface unarmed and let
        the rebind path settle it once the read succeeds."""
        sess = _session("slack:1.1", modified=NOW - 60)
        log = _FakeLog([sess], {})
        log.transcripts["slack:1.1"] = [
            {"role": "user", "content": "first", "ts": "2026-07-30T00:00:00Z"}
        ]
        fail_once = {"armed": True}
        orig_read = log.read_messages

        def flaky(key: str) -> list[dict[str, Any]]:
            if fail_once["armed"] and key == "slack:1.1":
                fail_once["armed"] = False
                raise OSError("transient")
            return orig_read(key)

        log.read_messages = flaky  # type: ignore[method-assign]
        slot = self._surface(dashboard_state, log)
        assert slot._channel_mirror_key == ""
        assert slot._channel_mirror_checked is False

        # Rebind pass with a working read arms the mirror and catches up.
        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert [m["content"] for m in slot.messages] == ["first"]
        assert slot._channel_mirror_key == "slack:1.1"

    def test_a_failed_rebind_read_is_retried(self, dashboard_state: Any) -> None:
        """A restored slot whose rebind read fails must stay unchecked so a
        later pass can still settle it."""
        sess = _session("slack:1.1")
        log = _FakeLog([sess], {})
        log.transcripts["slack:1.1"] = [
            {"role": "user", "content": "first", "ts": "2026-07-30T00:00:00Z"}
        ]
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        slot = dashboard_state.get_or_create_slot(name="slack_1.1")

        fail_once = {"armed": True}
        orig_read = log.read_messages

        def flaky(key: str) -> list[dict[str, Any]]:
            if fail_once["armed"] and key == "slack:1.1":
                fail_once["armed"] = False
                raise OSError("transient")
            return orig_read(key)

        log.read_messages = flaky  # type: ignore[method-assign]
        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert slot._channel_mirror_checked is False

        asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert slot._channel_mirror_key == "slack:1.1"
        assert [m["content"] for m in slot.messages] == ["first"]
