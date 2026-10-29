"""Slack allowlist owner prompts and their ``config.json`` persistence.

:mod:`kiro_crew.slack.allowlist` decides who may talk to the agent: it DMs the
owner an Allow/Deny (or Track/Ignore) prompt and, once the owner answers, adds or
removes the entry in ``config.json``. The interaction router dispatches on the
buttons' action ids and ``partition(":")``s their value into an id and a display
name, so both are pinned here as a contract.

The Slack client is a recording fake, and persistence writes a real
``config.json`` in ``tmp_path``. No network or host config is touched.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.slack import allowlist as allowlist_mod
from kiro_crew.slack.allowlist import (
    ACTION_ALLOWLIST_APPROVE,
    ACTION_ALLOWLIST_DENY,
    ACTION_TRACK_APPROVE,
    ACTION_TRACK_DENY,
    persist_allowed_user,
    persist_tracking_channel,
    prompt_allowlist,
    prompt_track_channel,
)

OWNER = "UOWNER"


class _Slack:
    """Records the DM it opened and the blocks it posted; can fail on open."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.opened: list[str] = []
        self.posted: list[tuple[str, list[dict], str]] = []

    async def open_dm(self, user_id: str) -> str:
        if self.fail:
            raise RuntimeError("slack down")
        self.opened.append(user_id)
        return f"D-{user_id}"

    async def post_blocks(self, channel: str, blocks: list[dict], fallback: str) -> None:
        self.posted.append((channel, blocks, fallback))

    # The one prompt sent, unpacked into what the owner sees and what the buttons carry.
    def prompt(self) -> dict[str, Any]:
        assert len(self.posted) == 1, self.posted
        channel, blocks, fallback = self.posted[0]
        section, actions = blocks
        approve, deny = actions["elements"]
        assert (approve["style"], deny["style"]) == ("primary", "danger")
        return {
            "channel": channel,
            "fallback": fallback,
            "text": section["text"]["text"],
            "labels": (approve["text"]["text"], deny["text"]["text"]),
            "actions": (approve["action_id"], deny["action_id"]),
            "values": (approve["value"], deny["value"]),
        }


def _allowed(monkeypatch: pytest.MonkeyPatch, *ids: str) -> None:
    monkeypatch.setattr(allowlist_mod, "is_allowed_user", lambda uid: uid in ids)


def _tracked(monkeypatch: pytest.MonkeyPatch, *ids: str) -> None:
    monkeypatch.setattr(allowlist_mod, "is_tracked_channel", lambda cid: cid in ids)


class TestPromptAllowlist:
    @pytest.mark.asyncio
    async def test_an_empty_user_sends_nothing(self, monkeypatch) -> None:
        _allowed(monkeypatch)
        slack = _Slack()
        await prompt_allowlist(slack, OWNER, "")  # type: ignore[arg-type]
        assert (slack.opened, slack.posted) == ([], [])

    @pytest.mark.asyncio
    async def test_a_channel_join_by_an_allowed_user_sends_nothing(self, monkeypatch) -> None:
        _allowed(monkeypatch, "U1")
        slack = _Slack()
        await prompt_allowlist(slack, OWNER, "U1", channel_id="C1")  # type: ignore[arg-type]
        assert (slack.opened, slack.posted) == ([], [])

    @pytest.mark.asyncio
    async def test_a_channel_join_asks_the_owner_to_allow_or_deny(self, monkeypatch) -> None:
        _allowed(monkeypatch)
        slack = _Slack()
        await prompt_allowlist(slack, OWNER, "U1", channel_id="C1")  # type: ignore[arg-type]
        assert slack.opened == [OWNER]  # the owner's DM, never the channel
        p = slack.prompt()
        assert p["channel"] == f"D-{OWNER}"
        assert p["text"] == "👋 <@U1> just joined <#C1>.\nAdd to allowlist?"
        assert p["labels"] == ("✅ Allow", "🚫 Deny")
        assert p["actions"] == (ACTION_ALLOWLIST_APPROVE, ACTION_ALLOWLIST_DENY)
        # The router partitions on ":" into (user id, display name).
        assert p["values"] == ("U1:", "U1:")
        assert p["fallback"] == "Allowlist prompt"

    @pytest.mark.asyncio
    async def test_a_manual_request_for_a_new_user_says_requested(self, monkeypatch) -> None:
        _allowed(monkeypatch)
        slack = _Slack()
        await prompt_allowlist(slack, OWNER, "U1")  # type: ignore[arg-type]
        p = slack.prompt()
        assert p["text"] == "👋 <@U1> — allowlist requested.\nAdd to allowlist?"
        assert p["labels"] == ("✅ Allow", "🚫 Deny")

    @pytest.mark.asyncio
    async def test_a_manual_request_for_an_allowed_user_offers_keep_or_remove(
        self, monkeypatch
    ) -> None:
        _allowed(monkeypatch, "U1")
        slack = _Slack()
        await prompt_allowlist(slack, OWNER, "U1")  # type: ignore[arg-type]
        p = slack.prompt()
        assert p["text"] == "<@U1> is currently on the allowlist.\nKeep or remove?"
        assert p["labels"] == ("✅ Keep", "🚫 Remove")
        assert p["actions"] == (ACTION_ALLOWLIST_APPROVE, ACTION_ALLOWLIST_DENY)


class TestPromptTrackChannel:
    @pytest.mark.asyncio
    async def test_an_empty_channel_sends_nothing(self, monkeypatch) -> None:
        _tracked(monkeypatch)
        slack = _Slack()
        await prompt_track_channel(slack, OWNER, "")  # type: ignore[arg-type]
        assert (slack.opened, slack.posted) == ([], [])

    @pytest.mark.asyncio
    async def test_an_untracked_channel_offers_track_or_ignore(self, monkeypatch) -> None:
        _tracked(monkeypatch)
        slack = _Slack()
        await prompt_track_channel(slack, OWNER, "C1", "eng")  # type: ignore[arg-type]
        assert slack.opened == [OWNER]
        p = slack.prompt()
        assert p["text"] == "📡 Track <#C1> for new member allowlist prompts?"
        assert p["labels"] == ("✅ Track", "🚫 Ignore")
        assert p["actions"] == (ACTION_TRACK_APPROVE, ACTION_TRACK_DENY)
        assert p["values"] == ("C1:eng", "C1:eng")
        assert p["fallback"] == "Track channel — prompt"

    @pytest.mark.asyncio
    async def test_a_tracked_channel_offers_keep_or_remove(self, monkeypatch) -> None:
        _tracked(monkeypatch, "C1")
        slack = _Slack()
        await prompt_track_channel(slack, OWNER, "C1")  # type: ignore[arg-type]
        p = slack.prompt()
        assert p["text"] == "📡 <#C1> is currently tracked.\nKeep tracking or remove?"
        assert p["labels"] == ("✅ Keep", "🚫 Remove")
        assert p["values"] == ("C1:", "C1:")


@pytest.mark.asyncio
async def test_a_failed_dm_is_logged_not_raised(monkeypatch, caplog) -> None:
    _allowed(monkeypatch)
    caplog.set_level(logging.ERROR, logger=allowlist_mod.logger.name)
    slack = _Slack(fail=True)
    await prompt_allowlist(slack, OWNER, "U1")  # type: ignore[arg-type]
    assert slack.posted == []
    assert [r.getMessage() for r in caplog.records] == ["Failed to send prompt: Allowlist prompt"]


@pytest.fixture()
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real ``config.json`` the persistence helpers read and write."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"slack": {"bot_token": "keep-me"}}))
    monkeypatch.setattr(allowlist_mod, "config_path", lambda: path)
    return path


def _slack_section(path: Path) -> dict:
    return json.loads(path.read_text())["slack"]


class TestPersistence:
    def test_an_allowed_user_is_added_with_its_name(self, config_file: Path) -> None:
        persist_allowed_user("U1", "Ada")
        persist_allowed_user("U2")
        slack = _slack_section(config_file)
        assert slack["allowed_users"] == [{"slack_id": "U1", "name": "Ada"}, {"slack_id": "U2"}]
        assert slack["bot_token"] == "keep-me"  # the rest of the section is untouched

    def test_a_user_is_removed(self, config_file: Path) -> None:
        persist_allowed_user("U1")
        persist_allowed_user("U2")
        persist_allowed_user("U1", remove=True)
        assert _slack_section(config_file)["allowed_users"] == [{"slack_id": "U2"}]

    def test_a_tracking_channel_is_added_and_removed(self, config_file: Path) -> None:
        persist_tracking_channel("C1", "eng")
        persist_allowed_user("U1")
        assert _slack_section(config_file)["tracking_channels"] == [
            {"channel_id": "C1", "name": "eng"}
        ]
        persist_tracking_channel("C1", remove=True)
        slack = _slack_section(config_file)
        assert slack["tracking_channels"] == []
        assert slack["allowed_users"] == [{"slack_id": "U1"}]  # the other list is untouched

    @pytest.mark.parametrize(
        "write",
        [
            lambda: persist_allowed_user("U1", "Renamed"),  # duplicate add
            lambda: persist_allowed_user("U9", remove=True),  # missing remove
            lambda: persist_tracking_channel("C9", remove=True),
        ],
        ids=["duplicate-add", "missing-user-remove", "missing-channel-remove"],
    )
    def test_a_no_op_does_not_rewrite_the_file(self, config_file: Path, write) -> None:
        persist_allowed_user("U1", "Ada")
        before = config_file.read_bytes()
        inode = config_file.stat().st_ino
        write()
        # The locked writer replaces the file by rename, so a write would change the inode.
        assert config_file.stat().st_ino == inode
        assert config_file.read_bytes() == before
