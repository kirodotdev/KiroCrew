"""``slack.show_in_session_list``: Slack-born rows stamped for the person's list filter.

On (the default, and what a missing or malformed value reads as), every slot-list
row is what it was before the setting existed. Off, only a row whose conversation
started in Slack carries ``slack_list_hidden``; a dashboard session, a code
channel's session and another channel's conversation never do. Turning it back on
drops the stamp, and neither direction closes, removes or writes anything.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.config import live
from kiro_crew.config.section_builders import _build_slack_config
from kiro_crew.dashboard import slack_session_list
from kiro_crew.dashboard.slack_session_list import (
    SLACK_LIST_HIDDEN_KEY,
    is_slack_born_slot,
    slack_rows_listed,
)

THREAD = "slack:1785370133.085469"
FLAT_DM = "slack:D0DM00001"


def _set_listed(monkeypatch: pytest.MonkeyPatch, value: Any) -> None:
    """Make the watcher's applied snapshot carry ``slack.show_in_session_list``."""
    snap = SimpleNamespace(slack=SimpleNamespace(show_in_session_list=value))
    monkeypatch.setattr(live, "snapshot", lambda: snap)


def _no_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "snapshot", lambda: None)


class TestConfigField:
    def test_default_is_on(self) -> None:
        assert _build_slack_config({}).show_in_session_list is True

    @pytest.mark.parametrize("raw", ["false", "no", 0, None, [], {}])
    def test_a_malformed_value_reads_on(self, raw: Any) -> None:
        assert _build_slack_config({"show_in_session_list": raw}).show_in_session_list is True

    def test_a_real_false_is_off(self) -> None:
        assert _build_slack_config({"show_in_session_list": False}).show_in_session_list is False


class TestSettingRead:
    def test_no_watcher_snapshot_reads_listed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _no_snapshot(monkeypatch)
        assert slack_rows_listed() is True

    def test_off_reads_unlisted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_listed(monkeypatch, False)
        assert slack_rows_listed() is False

    @pytest.mark.parametrize("value", [True, None, "false", 0])
    def test_anything_but_a_real_false_reads_listed(
        self, monkeypatch: pytest.MonkeyPatch, value: Any
    ) -> None:
        _set_listed(monkeypatch, value)
        assert slack_rows_listed() is True

    def test_a_snapshot_without_a_slack_section_reads_listed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace())
        assert slack_rows_listed() is True

    def test_a_failing_read_reads_listed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> None:
            raise RuntimeError("watcher gone")

        monkeypatch.setattr(live, "snapshot", _boom)
        assert slack_rows_listed() is True


class TestSlackBorn:
    @pytest.mark.parametrize(
        "slot,expected",
        [
            (SimpleNamespace(key="slack_1785370133.085469", channel_origin=True), True),
            (SimpleNamespace(key="slack_D0DM00001", channel_origin=True), True),
            # A dashboard session, linked to Slack or not, is never Slack-born.
            (SimpleNamespace(key="chat-1-1", channel_origin=False), False),
            # A slot key shaped like a Slack stem without channel provenance.
            (SimpleNamespace(key="slack_1785370133.085469", channel_origin=False), False),
            (SimpleNamespace(key="discord_123456", channel_origin=True), False),
            (SimpleNamespace(key="telegram_99", channel_origin=True), False),
        ],
    )
    def test_only_a_slack_channel_slot(self, slot: Any, expected: bool) -> None:
        assert is_slack_born_slot(slot) is expected


# ── Through the real slot list ────────────────────────────────────────────


@pytest.fixture
def rows_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict[str, Any]]:
    """A state holding one slot of every kind the setting must tell apart."""
    from kiro_crew.slack.code_channels import dashboard_view
    from kiro_crew.slack.code_channels.store import Binding, CodeChannelStore, set_store

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(dashboard_view, "_gate_open", lambda: True)
    state = _make_state(tmp_path)
    slots = {
        "thread": state.get_or_create_slot(
            name="slack_1785370133.085469", linked_session_key=THREAD, channel_origin=True
        ),
        "dm": state.get_or_create_slot(
            name="slack_D0DM00001", linked_session_key=FLAT_DM, channel_origin=True
        ),
        "discord": state.get_or_create_slot(
            name="discord_123456", linked_session_key="discord:123456", channel_origin=True
        ),
        "dashboard": state.get_or_create_slot(None),
        "code_channel": state.get_or_create_slot(None),
    }
    store = CodeChannelStore(tmp_path / "slack-code-channels.json")
    set_store(store)
    store.put_binding(
        Binding(
            channel_id="C0CODE1",
            team_id="T0TEAM1",
            session_key=slots["code_channel"].key,
            anchor_ts="200.000001",
            channel_name="fix-login",
        )
    )
    store.load()
    yield state, slots
    set_store(None)


def _all_views(state: Any) -> list[list[dict[str, Any]]]:
    bare, user, owner = state.serialize_slot_views(owner=True)
    return [bare, user, owner or [], state.serialize_slots(dashboard_user=True)]


def _stamped(rows: list[dict[str, Any]]) -> set[str]:
    return {r["key"] for r in rows if r.get(SLACK_LIST_HIDDEN_KEY)}


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


class TestSerializedRows:
    def test_on_by_default_no_row_carries_the_stamp(
        self, rows_state: tuple[Any, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state, _slots = rows_state
        _no_snapshot(monkeypatch)
        for rows in _all_views(state):
            assert all(SLACK_LIST_HIDDEN_KEY not in r for r in rows)

    def test_explicit_on_matches_the_default(
        self, rows_state: tuple[Any, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state, _slots = rows_state
        _no_snapshot(monkeypatch)
        default = state.serialize_slots()
        _set_listed(monkeypatch, True)
        assert state.serialize_slots() == default

    def test_off_stamps_only_slack_born_rows_in_every_view(
        self, rows_state: tuple[Any, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state, slots = rows_state
        _set_listed(monkeypatch, False)
        want = {slots["thread"].key, slots["dm"].key}
        for rows in _all_views(state):
            assert _stamped(rows) == want

    def test_a_code_channel_session_keeps_its_row_and_its_badge(
        self, rows_state: tuple[Any, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state, slots = rows_state
        _set_listed(monkeypatch, False)
        rows = state.serialize_slots(dashboard_user=True)
        row = next(r for r in rows if r["key"] == slots["code_channel"].key)
        assert row["code_channel"]["channel_name"] == "fix-login"
        assert SLACK_LIST_HIDDEN_KEY not in row

    def test_turning_it_back_on_drops_the_stamp(
        self, rows_state: tuple[Any, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state, _slots = rows_state
        _set_listed(monkeypatch, False)
        assert _stamped(state.serialize_slots())
        _set_listed(monkeypatch, True)
        for rows in _all_views(state):
            assert not _stamped(rows)

    def test_neither_direction_closes_removes_or_writes_anything(
        self,
        rows_state: tuple[Any, dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        state, slots = rows_state
        before_slots = dict(state._slots)
        before_tree = _tree(tmp_path)
        _set_listed(monkeypatch, False)
        _all_views(state)
        _set_listed(monkeypatch, True)
        _all_views(state)
        assert state._slots == before_slots
        assert all(not getattr(s, "is_closing", False) for s in slots.values())
        assert _tree(tmp_path) == before_tree

    def test_a_hidden_conversation_s_approval_still_rides_its_row(
        self, rows_state: tuple[Any, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The person's list keeps a stamped row that waits on them (``slackRowHidden``),
        so the server must still report the approval on that row."""
        state, slots = rows_state
        slots["thread"]._approval_futures["r1"] = SimpleNamespace(done=lambda: False)
        _set_listed(monkeypatch, False)
        for rows in _all_views(state):
            row = next(r for r in rows if r["key"] == slots["thread"].key)
            assert row[SLACK_LIST_HIDDEN_KEY] is True
            assert row["pending_approval"] is True


# ── The Slack settings endpoints ──────────────────────────────────────────


def _config_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path]:
    import kiro_crew.dashboard.handlers.messaging as mod
    from kiro_crew.config import loader

    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text('{"slack": {"command": "kirocrew"}}', encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    monkeypatch.delenv("KIROCREW_OWNER_ID", raising=False)
    return mod, cfg


def _put(mod: Any, payload: dict[str, Any], state: Any = None) -> tuple[int, dict[str, Any]]:
    async def _run() -> tuple[int, dict[str, Any]]:
        app = web.Application()
        if state is not None:
            app["state"] = state
        app.router.add_put("/api/slack/config", mod.api_slack_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/slack/config", json=payload)
            return resp.status, await resp.json()

    return asyncio.run(_run())


class TestSettingsEndpoint:
    def test_get_reports_on_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod, _cfg = _config_app(tmp_path, monkeypatch)

        async def _get() -> dict[str, Any]:
            app = web.Application()
            app["state"] = MagicMock()
            app.router.add_get("/api/slack/config", mod.api_slack_config_get)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/slack/config")
                return await resp.json()

        assert asyncio.run(_get())["show_in_session_list"] is True

    def test_save_round_trips_live_and_pushes_the_slot_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod, cfg = _config_app(tmp_path, monkeypatch)
        state = SimpleNamespace(owner_id="", push_slots_update=MagicMock())
        status, body = _put(mod, {"show_in_session_list": False}, state)
        assert status == 200
        assert body["restart_required"] is False
        assert json.loads(cfg.read_text(encoding="utf-8"))["slack"]["show_in_session_list"] is False
        state.push_slots_update.assert_called_once_with()
        status, _ = _put(mod, {"show_in_session_list": True}, state)
        assert status == 200
        assert json.loads(cfg.read_text(encoding="utf-8"))["slack"]["show_in_session_list"] is True

    def test_a_save_without_the_field_pushes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod, _cfg = _config_app(tmp_path, monkeypatch)
        state = SimpleNamespace(owner_id="", push_slots_update=MagicMock())
        status, _ = _put(mod, {"show_thinking": False}, state)
        assert status == 200
        state.push_slots_update.assert_not_called()

    @pytest.mark.parametrize("raw", ["false", 0, None])
    def test_save_refuses_a_non_bool(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: Any
    ) -> None:
        mod, cfg = _config_app(tmp_path, monkeypatch)
        status, _ = _put(mod, {"show_in_session_list": raw})
        assert status == 400
        assert "show_in_session_list" not in cfg.read_text(encoding="utf-8")


def test_the_module_reads_no_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The setting read runs on every slot-list serialization on the event loop."""
    from kiro_crew.config import KiroCrewConfig

    def _no_load(*_a: Any, **_k: Any) -> None:
        raise AssertionError("config read from disk on the slot-list path")

    monkeypatch.setattr(KiroCrewConfig, "load", _no_load)
    _no_snapshot(monkeypatch)
    assert slack_session_list.slack_rows_listed() is True


def test_a_peer_row_keeps_only_a_real_hidden_stamp() -> None:
    """A connected crew's stamp crosses the peer boundary so this dashboard's list
    honours it; anything but a literal ``True`` is dropped, and absent stays absent."""
    from kiro_crew.dashboard.handlers_instances_peer import _clean_peer_slot

    hidden = _clean_peer_slot({"key": "slack_1.1", "slack_list_hidden": True})
    forged = _clean_peer_slot({"key": "slack_2.2", "slack_list_hidden": "yes"})
    plain = _clean_peer_slot({"key": "chat-1"})
    assert hidden is not None and hidden[SLACK_LIST_HIDDEN_KEY] is True
    assert forged is not None and SLACK_LIST_HIDDEN_KEY not in forged
    assert plain is not None and SLACK_LIST_HIDDEN_KEY not in plain


def test_a_pass_reads_the_setting_once_and_stamps_only_slack_born_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard.slack_session_list import slack_row_stamper

    _set_listed(monkeypatch, True)
    assert slack_row_stamper() is None
    _set_listed(monkeypatch, False)
    stamp = slack_row_stamper()
    assert stamp is not None
    slack_row: dict[str, Any] = {}
    plain_row: dict[str, Any] = {}
    stamp(SimpleNamespace(key="slack_1.1", channel_origin=True), slack_row)
    stamp(SimpleNamespace(key="chat-1", channel_origin=False), plain_row)
    assert slack_row == {SLACK_LIST_HIDDEN_KEY: True}
    assert plain_row == {}
