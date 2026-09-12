"""The dashboard slot save preserves the images its rows reference.

A dashboard chat's assistant rows do NOT go through ``ConversationLog.append`` --
``_save_slot_to_history`` re-serializes the whole in-memory window through
``_build_message_entry`` -- so the write boundary has to hold on that path too.
"""

from __future__ import annotations

import pytest

from kiro_crew.chat_attachments import attachments_dir
from kiro_crew.dashboard.chat_persistence import (
    _build_message_entry,
    _build_message_entry_uncached,
)

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


@pytest.fixture()
def png(tmp_path):
    source = tmp_path / "scratch" / "shot.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(PNG_BYTES)
    return source


@pytest.fixture()
def sessions(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    return d


def test_entry_names_the_stored_copy(sessions, png):
    message = {"role": "assistant", "content": f"![shot]({png})", "ts": "t0"}

    entry = _build_message_entry_uncached(message, attachments=(sessions, "tab1"))

    stored = next(attachments_dir(sessions, "tab1").iterdir())
    assert str(stored) in entry["content"]
    assert str(png) not in entry["content"]
    # The in-memory row is untouched: the live UI keeps the path it streamed.
    assert message["content"] == f"![shot]({png})"


def test_without_a_session_target_nothing_is_copied(sessions, png):
    """A caller with no session context (a preview, a test) opts out."""
    message = {"role": "assistant", "content": f"![shot]({png})", "ts": "t0"}

    entry = _build_message_entry_uncached(message)

    assert entry["content"] == f"![shot]({png})"
    assert not attachments_dir(sessions, "tab1").exists()


def test_user_rows_are_left_alone(sessions, png):
    message = {"role": "user", "content": f"![mine]({png})", "ts": "t0"}

    entry = _build_message_entry_uncached(message, attachments=(sessions, "tab1"))

    assert entry["content"] == f"![mine]({png})"
    assert not attachments_dir(sessions, "tab1").exists()


def test_the_memo_does_not_share_an_entry_across_sessions(sessions, png):
    """Two sessions get two copies -- one session's delete must not blank the other.

    The cached entry names a path inside ONE session's attachment directory, so
    the session target has to be part of the cache key.
    """
    message = {"role": "assistant", "content": f"![shot]({png})", "ts": "t0"}

    first = _build_message_entry(message, attachments=(sessions, "tab1"))
    second = _build_message_entry(message, attachments=(sessions, "tab2"))

    assert first["content"] != second["content"]
    assert str(next(attachments_dir(sessions, "tab1").iterdir())) in first["content"]
    assert str(next(attachments_dir(sessions, "tab2").iterdir())) in second["content"]


def test_reserializing_the_same_row_does_not_recopy(sessions, png):
    """The save re-serializes its whole window on every flush."""
    message = {"role": "assistant", "content": f"![shot]({png})", "ts": "t0"}

    first = _build_message_entry_uncached(message, attachments=(sessions, "tab1"))
    stored = sorted(p.name for p in attachments_dir(sessions, "tab1").iterdir())
    second = _build_message_entry_uncached(message, attachments=(sessions, "tab1"))

    assert first["content"] == second["content"]
    assert sorted(p.name for p in attachments_dir(sessions, "tab1").iterdir()) == stored


def test_an_already_rewritten_row_is_stable(sessions, png):
    """Re-persisting a row that already names storage changes nothing."""
    first = _build_message_entry_uncached(
        {"role": "assistant", "content": f"![shot]({png})", "ts": "t0"},
        attachments=(sessions, "tab1"),
    )
    again = _build_message_entry_uncached(
        {"role": "assistant", "content": first["content"], "ts": "t0"},
        attachments=(sessions, "tab1"),
    )

    assert again["content"] == first["content"]
    assert len(list(attachments_dir(sessions, "tab1").iterdir())) == 1


def test_variant_content_is_rewritten_too(sessions, png):
    """A variant is an alternate reply the user can switch BACK to.

    It is persisted and redacted on this path, so leaving its images alone would
    show the same missing-file chip the primary content is free of.
    """
    message = {
        "role": "assistant",
        "content": "primary",
        "ts": "t0",
        "variants": [{"content": f"![shot]({png})"}],
        "variant_idx": 0,
    }

    entry = _build_message_entry_uncached(message, attachments=(sessions, "tab1"))

    stored = next(attachments_dir(sessions, "tab1").iterdir())
    assert str(stored) in entry["variants"][0]["content"]
    assert str(png) not in entry["variants"][0]["content"]
    # The in-memory variant is untouched, like the primary content.
    assert message["variants"][0]["content"] == f"![shot]({png})"


def test_transient_roles_are_still_not_persisted(sessions):
    assert (
        _build_message_entry_uncached(
            {"role": "chunk", "content": "x"}, attachments=(sessions, "tab1")
        )
        is None
    )
