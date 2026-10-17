"""``dashboard.title_ticket_prefix``: a ticket key leads an AUTO title.

The model keeps writing only the summary; the key is copied from the opening
message and wrapped on deterministically, on the initial, fallback, refresh and
manual-regenerate paths. Off by default, so every existing title is unchanged.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_title
from kiro_crew.dashboard.chat_title import (
    _TITLE_ORIGIN_AUTO,
    _TITLE_REFRESH_MILESTONES,
    _maybe_auto_title,
    _split_ticket_prefix,
    _ticket_key_from_messages,
    _with_ticket_prefix,
    maybe_refresh_title,
)
from kiro_crew.dashboard.state import _ChatSlot


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _state(*slots):
    state = MagicMock()
    state.conversation_log = MagicMock()
    state._slots = {s.key: s for s in slots}
    return state


def _enable(monkeypatch, on: bool = True) -> None:
    monkeypatch.setattr(chat_title, "_title_ticket_prefix_enabled", lambda: on)


async def _no_reveal(*_a, **_k):
    return None


# ── key detection: every shape the matcher is meant to cover, and the misses ──
@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("Please look at PROJ-1234 today", "PROJ-1234"),
        ("AB-12 is flaky", "AB-12"),
        ("P2-345: shard the suite", "P2-345"),
        ("see https://jira.example.com/browse/OPS-77 now", "OPS-77"),
        ("Fix #123 please", "#123"),
        ("(#42) needs a look", "#42"),
        ("first PROJ-11 then PROJ-22", "PROJ-11"),
        ("#9 then PROJ-1234", "#9"),
        # Residual pinned on purpose: a standard name with a long number has the
        # tracker-key shape. This is why the setting is opt-in.
        ("compare SHA-256 digests", "SHA-256"),
        # Misses: below the two-digit floor, lower case, single-letter project,
        # embedded in a longer token, URL fragment, HTML entity, heading marks.
        ("decode as UTF-8", ""),
        ("try GPT-4", ""),
        ("proj-1234 lower case", ""),
        ("A-1234 single letter", ""),
        ("XPROJ-1234-extra", ""),
        ("page.html#123 anchor", ""),
        ("an &#123; entity", ""),
        ("## 12 heading", ""),
        ("issue/#5 path", ""),
        ("nothing here", ""),
    ],
)
def test_ticket_key_shapes(text, key):
    assert _ticket_key_from_messages([_user(text)]) == key


def test_only_the_opening_user_message_is_read():
    messages = [
        {"role": "assistant", "content": "Hello PROJ-99"},
        _user("rename the sidebar button"),
        _user("also PROJ-1234"),
    ]
    assert _ticket_key_from_messages(messages) == ""


def test_attachment_path_key_is_not_the_sessions_key():
    message = _user("summarise this /tmp/up/PROJ-1234-notes.txt")
    message["meta"] = {"files": ["/tmp/up/PROJ-1234-notes.txt"]}
    assert _ticket_key_from_messages([message]) == ""


def test_wrap_and_split_round_trip():
    assert _with_ticket_prefix("Increase regression shards", "PROJ-1234") == (
        "PROJ-1234: Increase regression shards"
    )
    assert _with_ticket_prefix("Research PROJ-1234 crash", "PROJ-1234") == (
        "Research PROJ-1234 crash"
    )
    assert _with_ticket_prefix("Some summary", "") == "Some summary"
    assert _split_ticket_prefix("PROJ-1234: Increase shards") == ("PROJ-1234", "Increase shards")
    assert _split_ticket_prefix("#12: Fix sidebar") == ("#12", "Fix sidebar")
    assert _split_ticket_prefix("Note: not a key") == ("", "Note: not a key")


def test_setting_defaults_off():
    assert KiroCrewConfig().dashboard.title_ticket_prefix is False


# ── initial auto-title ───────────────────────────────────────────────────────
def _fresh_slot(text: str) -> _ChatSlot:
    slot = _ChatSlot("chat-1-1")
    slot.messages = [_user(text), {"role": "assistant", "content": "On it."}]
    return slot


def _patch_initial(monkeypatch, reply: str):
    async def _fake(_state, _messages, *, session_key: str = ""):
        return reply

    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _fake)
    monkeypatch.setattr(chat_title, "_reveal_title", _no_reveal)
    monkeypatch.setattr(chat_title, "maybe_suggest_folder", _no_reveal)


@pytest.mark.asyncio
async def test_initial_title_is_led_by_the_key(monkeypatch):
    _enable(monkeypatch)
    _patch_initial(monkeypatch, "Increase regression test shards")
    slot = _fresh_slot("PROJ-1234 the regression suite times out, add shards")
    await _maybe_auto_title(_state(slot), slot)
    assert slot.title == "PROJ-1234: Increase regression test shards"
    assert slot._title_origin == _TITLE_ORIGIN_AUTO
    assert slot._title_low_signal is False


@pytest.mark.asyncio
async def test_setting_off_leaves_the_title_unchanged(monkeypatch):
    _enable(monkeypatch, False)
    _patch_initial(monkeypatch, "Increase regression test shards")
    slot = _fresh_slot("PROJ-1234 the regression suite times out, add shards")
    await _maybe_auto_title(_state(slot), slot)
    assert slot.title == "Increase regression test shards"


@pytest.mark.asyncio
async def test_no_key_leaves_the_title_unchanged(monkeypatch):
    _enable(monkeypatch)
    _patch_initial(monkeypatch, "Increase regression test shards")
    slot = _fresh_slot("the regression suite times out, add shards")
    await _maybe_auto_title(_state(slot), slot)
    assert slot.title == "Increase regression test shards"


@pytest.mark.asyncio
async def test_skip_fallback_is_wrapped_when_it_lacks_the_key(monkeypatch):
    _enable(monkeypatch)
    _patch_initial(monkeypatch, "")
    slot = _fresh_slot("hello there, about #77")
    await _maybe_auto_title(_state(slot), slot)
    # The truncated fallback already contains the key, so it is not repeated.
    assert slot.title.count("#77") == 1


# ── refresh ──────────────────────────────────────────────────────────────────
def _titled(title: str, first: str) -> _ChatSlot:
    slot = _ChatSlot("chat-1-1")
    slot.messages = [_user(first), {"role": "assistant", "content": "ok"}]
    for i in range(_TITLE_REFRESH_MILESTONES[0] - 1):
        slot.messages.append(_user(f"more {i}"))
        slot.messages.append({"role": "assistant", "content": f"reply {i}"})
    slot.title = title
    slot._titled = True
    slot._title_origin = _TITLE_ORIGIN_AUTO
    return slot


def _patch_refresh(monkeypatch, reply: str) -> list[str]:
    seen: list[str] = []

    async def _fake(_state, _messages, current_title, *, session_key: str = ""):
        seen.append(current_title)
        return reply

    monkeypatch.setattr(chat_title, "_generate_refreshed_title", _fake)
    return seen


@pytest.mark.asyncio
async def test_refresh_judges_the_summary_and_keeps_the_key(monkeypatch):
    _enable(monkeypatch)
    seen = _patch_refresh(monkeypatch, "Tune shard timeouts")
    # The opening message names no key (e.g. it is outside the window): the
    # key comes from the title itself.
    slot = _titled("PROJ-1234: Increase shards", "unrelated opener")
    await maybe_refresh_title(_state(slot), slot)
    assert seen == ["Increase shards"]
    assert slot.title == "PROJ-1234: Tune shard timeouts"


@pytest.mark.asyncio
async def test_refresh_adds_the_key_when_the_setting_was_turned_on_later(monkeypatch):
    _enable(monkeypatch)
    _patch_refresh(monkeypatch, "Tune shard timeouts")
    slot = _titled("Increase shards", "OPS-42 shards are slow")
    await maybe_refresh_title(_state(slot), slot)
    assert slot.title == "OPS-42: Tune shard timeouts"


@pytest.mark.asyncio
async def test_refresh_with_setting_off_passes_the_title_through(monkeypatch):
    _enable(monkeypatch, False)
    seen = _patch_refresh(monkeypatch, "Tune shard timeouts")
    slot = _titled("PROJ-1234: Increase shards", "PROJ-1234 shards")
    await maybe_refresh_title(_state(slot), slot)
    assert seen == ["PROJ-1234: Increase shards"]
    assert slot.title == "Tune shard timeouts"


# ── manual regenerate ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_manual_regenerate_keeps_the_key(monkeypatch):
    _enable(monkeypatch)
    _patch_initial(monkeypatch, "Tune shard timeouts")
    slot = _titled("PROJ-1234: Increase shards", "unrelated opener")
    state = _state(slot)
    request = MagicMock()
    request.app = {"state": state}
    request.match_info = {"slot": slot.key}
    request.get = lambda key, default=None: default
    resp = await chat_title.api_chat_slot_generate_title(request)
    assert resp.status == 200
    assert slot.title == "PROJ-1234: Tune shard timeouts"
