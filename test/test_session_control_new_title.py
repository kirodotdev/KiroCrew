"""``session_create`` and ``session_fork`` titles meet ``session_rename``'s character rules.

A model can send any code point as the title of a session it opens, and that
title is persisted and broadcast to every dashboard. The tests pin that both
verbs refuse the characters ``session_rename`` refuses (format characters,
text-direction controls, blank glyphs, an all-invisible title) before anything
is allocated, and redact a secret an invisible character splits.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_fork, create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_utils import slot_history_key


@pytest.fixture(autouse=True)
def _enabled(_floor_monkeypatch):
    """Default every test to the shipped (enabled) state without reading config."""
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _fresh_create_budget():
    """The per-caller create-rate window is process-wide module state; every
    test here creates as ``chat-1``, so it starts and ends with a fresh window."""
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


@pytest.fixture(autouse=True)
def _quiet_sel(_floor_monkeypatch):
    """Keep the SEL rows create and fork write out of the test home."""
    _floor_monkeypatch.setattr(chat_fork, "sel", lambda: MagicMock())
    _floor_monkeypatch.setattr(sc, "sel", lambda: MagicMock())


def _key(slot) -> str:
    return slot_history_key(slot)


def _caller(state):
    """A caller with one persisted turn, so a fork of it has a transcript to copy."""
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "question", "msg msg-u")
    slot.append("assistant", "answer", "msg msg-a")
    slot.drain()
    _save_slot_to_history(state, slot, closed=False)
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    return slot


def _create(state, caller, title: str) -> dict:
    return asyncio.run(sc.create_session(state, caller_session_key=_key(caller), title=title))


def _fork(state, caller, title: str) -> dict:
    return asyncio.run(sc.fork_session(state, caller_session_key=_key(caller), title=title))


_VERBS = [pytest.param(_create, id="create"), pytest.param(_fork, id="fork")]

_TAIL = "ABCDEFGHIJKLMNOP"


@pytest.mark.parametrize("verb", _VERBS)
@pytest.mark.parametrize(
    "title",
    [
        # A format character splitting a key: refused, so it is never stored.
        "deploy AKIA\u200b" + _TAIL,
        "deploy AKIA\u2060" + _TAIL,
        "deploy\u00adprod",
        "\ufeffdeploy prod",
        # Text-direction controls reorder the sidebar label.
        "\u202eevil",
        "a\u2066b",
        "report \u200f(2026) prod",
        # Nothing visible.
        "\u200b",
        "\u200b \u200c",
        "\u3164",
        "\u2800\u2800",
        "\ufe0f\ufe0e",
        # A blank glyph splits a key past the redactors.
        "deploy AKIA\u2800" + _TAIL,
        # A sidebar title is one line.
        "two\nlines",
        "esc\x1b[31m",
    ],
)
def test_a_hidden_character_title_is_refused_and_nothing_is_opened(tmp_path, verb, title):
    state = _make_state(tmp_path)
    caller = _caller(state)
    before = state.live_slot_count()

    with pytest.raises(sc.SessionControlError) as exc:
        verb(state, caller, title)

    assert exc.value.code == "invalid_title"
    assert exc.value.status == 400
    assert _TAIL not in str(exc.value)
    assert state.live_slot_count() == before


@pytest.mark.parametrize("verb", _VERBS)
def test_a_refused_title_spends_no_create_budget(tmp_path, verb, monkeypatch):
    """The title is checked before the per-caller create-rate token is taken."""
    state = _make_state(tmp_path)
    caller = _caller(state)
    spent: list[str] = []
    monkeypatch.setattr(sc, "allow_create", lambda *a: spent.append("token") or True)

    with pytest.raises(sc.SessionControlError):
        verb(state, caller, "\u202eevil")

    assert spent == []


@pytest.mark.parametrize("verb", _VERBS)
@pytest.mark.parametrize("joiner", ["\u200d", "\ufe0f", "\u034f", "\U000e0020"])
def test_a_secret_split_by_a_kept_invisible_character_is_redacted(tmp_path, verb, joiner):
    """A joiner, variation selector or tag survives the character rules, so the
    scrub must still find the key it splits before the title is stored."""
    state = _make_state(tmp_path)
    caller = _caller(state)

    out = verb(state, caller, f"deploy AKIA{joiner}{_TAIL}")

    child = state.get_slot(out["target"])
    assert child is not None
    assert _TAIL not in child.title
    assert _TAIL not in out["title"]
    assert "[REDACTED" in child.title


@pytest.mark.parametrize("verb", _VERBS)
@pytest.mark.parametrize(
    "title",
    [
        "Team \U0001f468\u200d\U0001f469\u200d\U0001f467",
        "UK \U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
        "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",
    ],
)
def test_a_title_with_a_kept_format_character_is_stored_as_given(tmp_path, verb, title):
    state = _make_state(tmp_path)
    caller = _caller(state)

    out = verb(state, caller, f"  {title}  ")

    child = state.get_slot(out["target"])
    assert child is not None
    assert child.title == title and child._titled


@pytest.mark.parametrize("verb", _VERBS)
def test_a_whitespace_title_is_no_title(tmp_path, verb):
    """Blank means "name it for me", as before: nothing is refused and the
    session keeps the name it would have had with no title at all."""
    state = _make_state(tmp_path)
    caller = _caller(state)

    blank = verb(state, caller, "   ")
    omitted = verb(state, caller, "")

    blank_child = state.get_slot(blank["target"])
    omitted_child = state.get_slot(omitted["target"])
    assert blank_child is not None and omitted_child is not None
    # An untitled create shows its own slot key, so compare with it taken out.
    assert blank_child.title.replace(blank_child.key, "") == omitted_child.title.replace(
        omitted_child.key, ""
    )
    assert blank_child._titled == omitted_child._titled


@pytest.mark.parametrize("verb", _VERBS)
def test_a_long_title_is_cut_after_the_secret_is_redacted(tmp_path, verb):
    """Cutting first would keep the head of a key the cap splits, which no
    redactor pattern matches; the scrub runs on the whole title, then the cut."""
    state = _make_state(tmp_path)
    caller = _caller(state)
    title = "x" * 190 + " AKIA" + _TAIL

    out = verb(state, caller, title)

    child = state.get_slot(out["target"])
    assert child is not None
    assert len(child.title) == 200
    assert "AKIA" not in child.title
    assert child.title.startswith("x" * 190)


@pytest.mark.parametrize("verb", _VERBS)
def test_a_title_whose_cut_shows_nothing_is_refused_and_nothing_is_opened(tmp_path, verb):
    """The whole title shows something, but its first 200 characters are kept
    joiners; the cut result would be an invisible final name, so it is refused."""
    state = _make_state(tmp_path)
    caller = _caller(state)
    before = state.live_slot_count()

    with pytest.raises(sc.SessionControlError) as exc:
        verb(state, caller, "\u200d" * 200 + "deploy")

    assert exc.value.code == "invalid_title"
    assert exc.value.status == 400
    assert "no visible character" in str(exc.value)
    assert state.live_slot_count() == before
