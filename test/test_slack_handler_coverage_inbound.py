"""Privacy modifiers and dashboard-linked thread routing in :mod:`kiro_crew.slack.handler`.

Pins the ``!temporary`` and ``!incognito`` modifiers: the Slack names that delegate to
``messaging.privacy_mode``, the flags persisted on and hydrated from the session map, the
fail-closed refusal at the private-row cap and the tracker LRU caps. Also here: which
messages ``maybe_route_linked_thread`` claims for a thread linked to a dashboard slot.
The commands live in ``test_slack_handler_coverage.py``, the approval prompts in
``test_slack_handler_coverage_approvals.py``, status reactions, review drafts and the safe
update paths in ``test_slack_handler_coverage_delivery.py``, and the small pure helpers in
``test_slack_handler_coverage_pure_helpers.py``.
"""

from __future__ import annotations

from unittest.mock import ANY, MagicMock

import pytest
from slack_handler_coverage_helpers import (  # noqa: F401 -- fixtures are requested by name
    _clean_handler_state,
    _texts,
    owner_fixture,
    sessions_fixture,
    slack_fixture,
)

from kiro_crew.messaging import auto_title as auto_title_mod
from kiro_crew.messaging import privacy_mode
from kiro_crew.slack import handler as h


# ──────────────────────────────────────────────────────────────────────
# privacy modifiers
# ──────────────────────────────────────────────────────────────────────
class TestSharedPrivacyDelegation:
    """The Slack names are wrappers over ``messaging.privacy_mode``, and the two
    LRU dicts are the SAME objects.

    Identity is what the ~45 enforcement sites in this package, the dashboard
    predicates, and the autouse ``_clean_slack_thread_state`` conftest fixture all
    rely on: each reaches the tracker through the Slack spelling while the shared
    module reads its own name. A copy would leave a session restricted on one side
    and not the other, silently.
    """

    def test_the_trackers_are_the_shared_objects(self):
        """Mutation: rebind ``_thread_temporary`` to a fresh ``OrderedDict()`` —
        red, and every conftest-cleared test leaks privacy state across files."""
        assert h._thread_temporary is privacy_mode._temporary
        assert h._thread_incognito is privacy_mode._incognito
        assert h._titled_threads is auto_title_mod._titled

    def test_the_predicates_delegate(self):
        assert h.is_thread_temporary is privacy_mode.is_temporary
        assert h.is_thread_incognito is privacy_mode.is_incognito
        assert h._TEMPORARY_TOKEN_RE is privacy_mode.TEMPORARY_TOKEN_RE
        assert h._INCOGNITO_TOKEN_RE is privacy_mode.INCOGNITO_TOKEN_RE

    def test_the_slack_restricted_predicate_is_namespace_agnostic(self):
        """The dashboard's ``_is_restricted_session`` now reaches this for every
        channel, so it must answer for a non-Slack key.

        Mutation: narrow ``_is_slack_restricted`` to
        ``session_key.startswith("slack:") and privacy_mode.is_restricted(...)`` —
        red here while every Slack test stays green.
        """
        key = "telegram:kirocrew:direct:4242"
        privacy_mode.mark_incognito(key)
        assert h._is_slack_restricted(key) is True

    def test_the_shared_module_sees_a_slack_mark(self):
        h._mark_temporary("slack:1.2")
        assert privacy_mode.is_restricted("slack:1.2") is True


class TestPrivacyModifiers:
    @pytest.mark.asyncio
    async def test_temporary_only_returns_early(self, slack, sessions, owner):
        text, cmd, only = await h.maybe_apply_privacy_modifiers(
            "!temporary", "!temporary", "t1", "U1", "C1", slack, sessions, "t1"
        )
        assert only is True and cmd == ""
        assert h.is_thread_temporary("t1") is True
        assert "Temporary mode ON" in _texts(slack)
        sessions.set_slack_link.assert_called_once_with("t1", "t1", "C1")

    @pytest.mark.asyncio
    async def test_incognito_only_returns_early(self, slack, sessions, owner):
        _, cmd, only = await h.maybe_apply_privacy_modifiers(
            "!incognito", "!incognito", "t1", "U1", "C1", slack, sessions, "t1"
        )
        assert only is True and cmd == ""
        assert h.is_thread_incognito("t1") is True
        assert h._is_slack_restricted("t1") is True

    @pytest.mark.asyncio
    async def test_both_modifiers_with_remaining_text(self, slack, sessions, owner):
        text, cmd, only = await h.maybe_apply_privacy_modifiers(
            "!temporary !incognito summarize",
            "!temporary !incognito summarize",
            "t1",
            "U1",
            "C1",
            slack,
            sessions,
            "t1",
        )
        assert only is False
        assert cmd == "summarize" and text == "summarize"
        assert h.is_thread_temporary("t1") and h.is_thread_incognito("t1")

    @pytest.mark.asyncio
    async def test_no_modifier_is_passthrough(self, slack, sessions, owner):
        out = await h.maybe_apply_privacy_modifiers(
            "hello", "hello", "t1", "U1", "C1", slack, sessions, "t1"
        )
        assert out == ("hello", "hello", False)
        assert not slack.actions

    @pytest.mark.asyncio
    async def test_flags_are_persisted_on_the_session_map(
        self, slack, sessions, owner, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
        from kiro_crew.session_map import SessionMap

        sessions._session_map = SessionMap()
        await h._apply_privacy_mode(
            privacy_mode.MODE_TEMPORARY, "t1", "U1", "C1", slack, sessions, "t1"
        )
        await h._apply_privacy_mode(
            privacy_mode.MODE_INCOGNITO, "t1", "U1", "C1", slack, sessions, "t1"
        )
        # Assert real durability rather than that set_flag was called: a FRESH
        # map must read both flags back off disk, which is the property the
        # restart path actually depends on. Loop-side mutations defer their
        # disk write, so force the flush — the deterministic durability point.
        sessions._session_map.flush()
        fresh = SessionMap()
        assert fresh.get_flag("t1", "temporary") is True
        assert fresh.get_flag("t1", "incognito") is True

    @pytest.mark.asyncio
    async def test_a_refused_modifier_leaves_nothing_to_run(
        self, slack, sessions, owner, tmp_path, monkeypatch
    ):
        """The map is at its private-conversation cap: ``!incognito summarize``
        is refused fail-closed. The user is told the message was NOT processed,
        one SEL denial is written, no flag or mark lands, and the shared applier
        answers ``only_modifier=True`` -- the contract both Slack callers already
        honour by returning without a turn -- so the message is never run with
        the mode dropped. Mutation: drop the gate in ``SessionMap.set_flag`` --
        the flag is written, ``only_modifier`` is False and the turn would run."""
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
        import kiro_crew.session_map as session_map_mod
        from kiro_crew.messaging import privacy_mode
        from kiro_crew.session_map import SessionMap

        monkeypatch.setattr(session_map_mod, "PRIVACY_ROW_CAP", 1)
        sessions._session_map = SessionMap()
        sessions._session_map.set_flag("slack:other-thread", "incognito", True)
        events: list[dict] = []
        fake = MagicMock()
        fake.log_api_access = lambda **kw: events.append(kw)
        monkeypatch.setattr(privacy_mode, "sel", lambda: fake)

        text, cmd, only = await h.maybe_apply_privacy_modifiers(
            "!incognito summarize", "!incognito summarize", "t1", "U1", "C1", slack, sessions, "t1"
        )
        assert only is True, "a refused modifier must leave nothing to run"
        assert h.is_thread_incognito("t1") is False
        assert sessions._session_map.get_flag("t1", "incognito") is False
        sessions.set_slack_link.assert_not_called()
        posted = _texts(slack)
        assert "NOT processed" in posted and "Incognito mode ON" not in posted, posted
        assert [e["outcome"] for e in events] == ["denied"]
        assert events[0]["resources"] == "private_session_refused:limit:C1:t1"

    def test_hydrate_conv_flags_without_session_map(self, sessions):
        h._hydrate_conv_flags(sessions, "t1")
        assert not h.is_thread_temporary("t1")

    def test_hydrate_ignores_an_auto_attribute_stub(self, sessions):
        """An auto-attribute stub must NOT be mistaken for a real SessionMap.

        ``MagicMock().get_flag(...)`` returns a truthy mock, so accepting one
        here would mark every session both temporary and incognito.
        """
        sessions._session_map = MagicMock()
        h._hydrate_conv_flags(sessions, "t1")
        assert not h.is_thread_temporary("t1")
        assert not h.is_thread_incognito("t1")

    def test_hydrate_conv_flags_restores_both(self, sessions, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
        from kiro_crew.session_map import SessionMap

        sm = SessionMap()
        sm.set_flag("t1", "temporary", True)
        sm.set_flag("t1", "incognito", True)
        sessions._session_map = sm
        h._hydrate_conv_flags(sessions, "t1")
        assert h.is_thread_temporary("t1") and h.is_thread_incognito("t1")

    # The caps live on the shared modules the Slack names now delegate to, so the
    # monkeypatch has to land where the eviction check reads it.
    def test_temporary_lru_evicts_oldest(self, monkeypatch):
        monkeypatch.setattr(privacy_mode, "PRIVACY_LRU_MAX", 2)
        for key in ("a", "b", "c"):
            h._mark_temporary(key)
        assert not h.is_thread_temporary("a")
        assert h.is_thread_temporary("c")

    def test_incognito_lru_evicts_oldest(self, monkeypatch):
        monkeypatch.setattr(privacy_mode, "PRIVACY_LRU_MAX", 1)
        h._mark_incognito("a")
        h._mark_incognito("b")
        assert not h.is_thread_incognito("a")
        assert h.is_thread_incognito("b")

    def test_titled_lru_evicts_oldest(self, monkeypatch):
        monkeypatch.setattr(auto_title_mod, "TITLE_LRU_MAX", 1)
        h._mark_titled("a", "auto")
        h._mark_titled("b", "manual")
        assert "a" not in h._titled_threads


# ──────────────────────────────────────────────────────────────────────
# maybe_route_linked_thread
# ──────────────────────────────────────────────────────────────────────
class TestRouteLinkedThread:
    @pytest.mark.asyncio
    async def test_no_dashboard_state(self, slack):
        assert await h.maybe_route_linked_thread("hi", "t1", "U1", "C1", slack, "t1") is False

    @pytest.mark.asyncio
    async def test_no_linked_slot(self, slack, monkeypatch):
        state = MagicMock()
        state.get_linked_slot.return_value = None
        monkeypatch.setattr(h, "_dashboard_state", state)
        assert await h.maybe_route_linked_thread("hi", "t1", "U1", "C1", slack, "t1") is False

    @pytest.mark.asyncio
    async def test_unauthorized_user_denied(self, slack, owner, monkeypatch):
        state = MagicMock()
        state.get_linked_slot.return_value = MagicMock(key="slot-1")
        monkeypatch.setattr(h, "_dashboard_state", state)
        assert await h.maybe_route_linked_thread("hi", "t1", "UZZZ", "C1", slack, "t1") is True
        assert "Not authorized." in _texts(slack)

    @pytest.mark.asyncio
    async def test_bang_command_falls_through(self, slack, owner, monkeypatch):
        state = MagicMock()
        state.get_linked_slot.return_value = MagicMock(key="slot-1")
        monkeypatch.setattr(h, "_dashboard_state", state)
        assert await h.maybe_route_linked_thread("!yolo on", "t1", "U1", "C1", slack, "t1") is False

    @pytest.mark.asyncio
    async def test_running_slot_queues_message(self, slack, owner, monkeypatch):
        slot = MagicMock(key="slot-1", running=True)
        state = MagicMock()
        state.get_linked_slot.return_value = slot
        monkeypatch.setattr(h, "_dashboard_state", state)
        assert await h.maybe_route_linked_thread("do it", "t1", "U1", "C1", slack, "t1") is True
        # meta carries the admission-time containment snapshot.
        slot.queue_append.assert_called_once_with(
            "do it",
            meta=ANY,
            directive_user_origin=True,
            directive_channel_origin=True,
        )
        slot.append.assert_called_once()
        state.push_slots_update.assert_called_once()
