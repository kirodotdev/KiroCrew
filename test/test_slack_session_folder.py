"""The zero-config "Slack" folder for listed Slack conversations.

With ``slack.show_in_session_list`` on and ``slack.session_folder`` empty, a newly
surfaced Slack conversation is filed into a top-level "Slack" folder, created on
first use in one folder-store transaction (``slack_session_list.auto_folder_id``).
Unset or off, nothing is created and conversations land unfiled. A configured
``session_folder`` always wins, even when that folder is missing. Two top-level
"Slack" folders, the folder cap or a failed write leave the conversation unfiled.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from channel_folders_helpers import (  # noqa: F401 -- the fixture is requested by name
    _FakeLog,
    _write_config,
    dashboard_state_fixture,
)

from kiro_crew.config import live
from kiro_crew.dashboard import channel_folders, channel_slots, slack_session_list
from kiro_crew.dashboard.chat_folders_records import MAX_CHAT_FOLDERS
from kiro_crew.dashboard.mate_folders import MATE_HOME_KEY
from kiro_crew.dashboard.slack_session_list import AUTO_FOLDER_NAME, auto_folder_id

SLACK_KEY = "slack:1785370133.085469"
DISCORD_KEY = "discord:kirocrew:direct:U1"


def _snapshot(monkeypatch: pytest.MonkeyPatch, listed: Any, folder: str = "") -> None:
    snap = SimpleNamespace(
        slack=SimpleNamespace(show_in_session_list=listed, session_folder=folder)
    )
    monkeypatch.setattr(live, "snapshot", lambda: snap)


def _no_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "snapshot", lambda: None)


def _folder(fid: str, name: str, **extra: Any) -> dict[str, Any]:
    return {"id": fid, "name": name, "order": 0, "parent_id": "", **extra}


@pytest.fixture
def state(dashboard_state: Any) -> Any:
    dashboard_state.push_slots_update = MagicMock()
    return dashboard_state


def _slack_folders(state: Any) -> list[dict[str, Any]]:
    return [f for f in state._folders if f.get("name") == AUTO_FOLDER_NAME]


class TestAutoFolderId:
    def test_listed_with_no_folder_set_creates_it_once(self, state: Any, monkeypatch: Any) -> None:
        _snapshot(monkeypatch, True)
        first = asyncio.run(auto_folder_id(state))
        second = asyncio.run(auto_folder_id(state))
        (folder,) = state._folders
        assert first and first == second == folder["id"]
        assert folder["name"] == "Slack"
        assert folder["parent_id"] == ""
        # Unstamped, so a later configured ``session_folder`` never relabels it.
        assert "channel" not in folder
        state.push_slots_update.assert_called_once_with()

    def test_the_folder_is_on_disk(self, state: Any, monkeypatch: Any) -> None:
        _snapshot(monkeypatch, True)
        fid = asyncio.run(auto_folder_id(state))
        state._folders.clear()
        state.load_folders()
        assert [f["id"] for f in state._folders] == [fid]

    def test_unset_creates_nothing(self, state: Any, monkeypatch: Any) -> None:
        _no_snapshot(monkeypatch)
        assert asyncio.run(auto_folder_id(state)) == ""
        assert state._folders == []

    @pytest.mark.parametrize("listed", [False, None, "true", 1])
    def test_anything_but_a_real_true_creates_nothing(
        self, state: Any, monkeypatch: Any, listed: Any
    ) -> None:
        _snapshot(monkeypatch, listed)
        assert asyncio.run(auto_folder_id(state)) == ""
        assert state._folders == []

    def test_a_degraded_section_creates_nothing(self, state: Any, monkeypatch: Any) -> None:
        """A degraded ``slack`` section holds the field defaults."""
        from kiro_crew.config.sections_slack import SlackConfig

        monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(slack=SlackConfig()))
        assert asyncio.run(auto_folder_id(state)) == ""
        assert state._folders == []

    def test_a_failing_read_creates_nothing(self, state: Any, monkeypatch: Any) -> None:
        def _boom() -> None:
            raise RuntimeError("watcher gone")

        monkeypatch.setattr(live, "snapshot", _boom)
        assert asyncio.run(auto_folder_id(state)) == ""
        assert state._folders == []

    def test_an_explicit_folder_wins(self, state: Any, monkeypatch: Any) -> None:
        _snapshot(monkeypatch, True, folder="Work")
        assert asyncio.run(auto_folder_id(state)) == ""
        assert state._folders == []

    def test_an_existing_top_level_folder_is_used_as_is(self, state: Any, monkeypatch: Any) -> None:
        state._folders.append(_folder("mine", "slack ", hidden=True))
        _snapshot(monkeypatch, True)
        assert asyncio.run(auto_folder_id(state)) == "mine"
        (folder,) = state._folders
        assert "channel" not in folder
        assert folder["hidden"] is True
        state.push_slots_update.assert_not_called()

    def test_a_nested_folder_of_that_name_does_not_answer(
        self, state: Any, monkeypatch: Any
    ) -> None:
        state._folders.extend([_folder("p", "Work"), _folder("n", "Slack", parent_id="p")])
        _snapshot(monkeypatch, True)
        fid = asyncio.run(auto_folder_id(state))
        assert fid not in {"", "p", "n"}
        assert len(_slack_folders(state)) == 2

    def test_a_mate_home_of_that_name_does_not_answer(self, state: Any, monkeypatch: Any) -> None:
        state._folders.append(
            _folder("home", "Slack", **{MATE_HOME_KEY: True, "owner_app": "member:x"})
        )
        _snapshot(monkeypatch, True)
        fid = asyncio.run(auto_folder_id(state))
        assert fid not in {"", "home"}

    def test_two_top_level_folders_of_that_name_leave_it_unfiled(
        self, state: Any, monkeypatch: Any, caplog: Any
    ) -> None:
        state._folders.extend([_folder("a", "Slack"), _folder("b", "SLACK")])
        _snapshot(monkeypatch, True)
        with caplog.at_level(logging.INFO, logger=slack_session_list.__name__):
            assert asyncio.run(auto_folder_id(state)) == ""
        assert len(state._folders) == 2
        assert "2 top-level folders" in caplog.text

    def test_the_folder_cap_leaves_it_unfiled(self, state: Any, monkeypatch: Any) -> None:
        state._folders.extend(_folder(f"f{i}", f"F{i}") for i in range(MAX_CHAT_FOLDERS))
        _snapshot(monkeypatch, True)
        assert asyncio.run(auto_folder_id(state)) == ""
        assert len(state._folders) == MAX_CHAT_FOLDERS

    def test_a_failed_write_leaves_it_unfiled(
        self, state: Any, monkeypatch: Any, caplog: Any
    ) -> None:
        async def _fail(_mutate: Any, *_a: Any, **_k: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(state, "mutate_folders", _fail)
        _snapshot(monkeypatch, True)
        with caplog.at_level(logging.WARNING, logger=slack_session_list.__name__):
            assert asyncio.run(auto_folder_id(state)) == ""
        assert "could not be created" in caplog.text
        state.push_slots_update.assert_not_called()

    def test_racing_passes_create_one_folder(self, state: Any, monkeypatch: Any) -> None:
        _snapshot(monkeypatch, True)

        async def _both() -> list[str]:
            return list(await asyncio.gather(auto_folder_id(state), auto_folder_id(state)))

        first, second = asyncio.run(_both())
        assert first == second
        assert len(_slack_folders(state)) == 1


def _reconcile(state: Any, keys: list[str]) -> int:
    state.conversation_log = _FakeLog(keys)
    return asyncio.run(channel_slots.reconcile_channel_slots(state, 0))


def _slot(state: Any, key: str) -> Any:
    return state._slots[channel_slots.channel_slot_name(key)]


class TestReconcileFiling:
    def test_a_listed_slack_conversation_is_filed_in_the_slack_folder(
        self, state: Any, monkeypatch: Any
    ) -> None:
        _snapshot(monkeypatch, True)
        assert _reconcile(state, [SLACK_KEY]) == 1
        (folder,) = _slack_folders(state)
        assert _slot(state, SLACK_KEY).folder_id == folder["id"]

    def test_unset_leaves_it_unfiled_and_creates_nothing(
        self, state: Any, monkeypatch: Any
    ) -> None:
        _no_snapshot(monkeypatch)
        assert _reconcile(state, [SLACK_KEY]) == 1
        assert _slot(state, SLACK_KEY).folder_id == ""
        assert state._folders == []

    def test_a_configured_folder_wins(self, state: Any, monkeypatch: Any) -> None:
        _write_config("slack", "Work")
        _snapshot(monkeypatch, True, folder="Work")
        fid = asyncio.run(channel_folders.ensure_channel_folder(state, "slack", "Work"))
        assert _reconcile(state, [SLACK_KEY]) == 1
        assert _slot(state, SLACK_KEY).folder_id == fid
        assert _slack_folders(state) == []

    def test_a_configured_folder_that_is_missing_leaves_it_unfiled(
        self, state: Any, monkeypatch: Any
    ) -> None:
        _write_config("slack", "Work")
        _snapshot(monkeypatch, True, folder="Work")
        assert _reconcile(state, [SLACK_KEY]) == 1
        assert _slot(state, SLACK_KEY).folder_id == ""
        assert state._folders == []

    def test_a_folder_saved_before_the_snapshot_caught_up_still_wins(
        self, state: Any, monkeypatch: Any
    ) -> None:
        """Disk names a folder the watcher snapshot has not applied yet."""
        _write_config("slack", "Work")
        _snapshot(monkeypatch, True, folder="")
        assert _reconcile(state, [SLACK_KEY]) == 1
        assert _slot(state, SLACK_KEY).folder_id == ""
        assert state._folders == []

    def test_another_channel_is_never_filed_there(self, state: Any, monkeypatch: Any) -> None:
        _snapshot(monkeypatch, True)
        assert _reconcile(state, [DISCORD_KEY]) == 1
        assert _slot(state, DISCORD_KEY).folder_id == ""
        assert state._folders == []
