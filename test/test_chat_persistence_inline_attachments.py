"""The dashboard slot save preserves the images its rows reference.

A dashboard chat's assistant rows do NOT go through ``ConversationLog.append`` --
``_save_slot_to_history`` re-serializes the whole in-memory window through
``_build_message_entry`` -- so the write boundary has to hold on that path too.
"""

from __future__ import annotations

import os

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


def _dest(path):
    """A stored path as a persisted markdown destination spells it.

    Identity on POSIX; forward slashes on Windows, because the native spelling
    is not a fixed point of the markdown parser that reads the row back. The
    contract and its cases live in ``test_chat_attachments.py``.
    """
    return str(path).replace(os.sep, "/")


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
    assert _dest(stored) in entry["content"]
    assert str(png) not in entry["content"]
    # The live row now names the durable copy too, so the next flush of this
    # window has nothing to resolve from scratch.
    assert message["content"] == entry["content"]


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
    assert _dest(next(attachments_dir(sessions, "tab1").iterdir())) in first["content"]
    assert _dest(next(attachments_dir(sessions, "tab2").iterdir())) in second["content"]


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
    assert _dest(stored) in entry["variants"][0]["content"]
    assert str(png) not in entry["variants"][0]["content"]
    # The live variant is updated too, like the primary content.
    assert message["variants"][0]["content"] == entry["variants"][0]["content"]


def test_variants_share_the_primary_content_budget(sessions, png, tmp_path, monkeypatch):
    """Content plus variants are ONE row under ONE lock, so they draw on one
    image cap: a row with many variants cannot copy many times the ceiling."""
    monkeypatch.setattr("kiro_crew.chat_attachments.MAX_IMAGES_PER_MESSAGE", 2)
    others = []
    for i in range(3):
        q = tmp_path / f"v{i}.png"
        q.write_bytes(png.read_bytes() + bytes([i]))
        others.append(q)
    message = {
        "role": "assistant",
        "content": f"![p]({png})",
        "ts": "t0",
        "variants": [{"content": f"![v]({q})"} for q in others],
        "variant_idx": 0,
    }

    entry = _build_message_entry_uncached(message, attachments=(sessions, "tab1"))

    # Two distinct destinations in all: the primary picture and the first variant's.
    assert len(list(attachments_dir(sessions, "tab1").iterdir())) == 2
    assert str(png) not in entry["content"]
    assert str(others[0]) not in entry["variants"][0]["content"]
    assert str(others[1]) in entry["variants"][1]["content"]
    assert str(others[2]) in entry["variants"][2]["content"]


def test_a_reflush_after_the_scratch_file_is_gone_keeps_the_stored_path(sessions, png):
    """The defining case: the agent's scratch is reclaimed between two flushes of
    the same window. The second flush must not resolve the row from scratch
    (which now fails) and overwrite the good persisted path with the dead one."""
    message = {"role": "assistant", "content": f"![shot]({png})", "ts": "t0"}
    first = _build_message_entry_uncached(message, attachments=(sessions, "tab1"))
    png.unlink()

    second = _build_message_entry_uncached(message, attachments=(sessions, "tab1"))

    assert second["content"] == first["content"]
    assert str(png) not in second["content"]


def test_a_row_that_moved_on_during_the_copy_is_not_overwritten(sessions, png, monkeypatch):
    """The save runs in a worker thread over row dicts the event loop still owns.
    A variant switch between the read and the write-back must win: the rewrite
    of the OLD text is persisted for this flush, but the live row keeps the
    reply the user chose, and the next flush rewrites that one."""
    import kiro_crew.dashboard.chat_persistence as cp

    message = {"role": "assistant", "content": f"![shot]({png})", "ts": "t0"}
    real = cp.persist_inline_images

    def switching(content, **kw):
        out = real(content, **kw)
        message["content"] = "the reply the user switched to"
        return out

    monkeypatch.setattr(cp, "persist_inline_images", switching)
    entry = _build_message_entry_uncached(message, attachments=(sessions, "tab1"))

    assert str(png) not in entry["content"]  # this flush persisted the rewrite
    assert message["content"] == "the reply the user switched to"  # the switch won


def test_transient_roles_are_still_not_persisted(sessions):
    assert (
        _build_message_entry_uncached(
            {"role": "chunk", "content": "x"}, attachments=(sessions, "tab1")
        )
        is None
    )
