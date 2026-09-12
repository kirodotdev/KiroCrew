"""Inline chat images are copied into per-session storage as a message persists.

The defect: an agent shows a picture with ``![alt](/abs/path.png)``, the dashboard
resolves that path off disk at VIEW time, and the path usually points into the
agent's per-process scratch directory -- reclaimed when the agent dies. The
transcript then renders a permanently broken image.

These tests pin the copy contract (:mod:`kiro_crew.chat_attachments`) and the two
write boundaries that use it.
"""

from __future__ import annotations

import os
import sys

import pytest

from kiro_crew.chat_attachments import (
    MAX_ATTACHMENT_BYTES,
    attachments_dir,
    persist_inline_images,
    remove_attachments,
)

# A one-pixel PNG: real magic bytes, so nothing here depends on a fake payload.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)
OTHER_PNG_BYTES = PNG_BYTES + b"\x00trailing"

STEM = "dashboard_chat-abc123"


@pytest.fixture()
def sessions(tmp_path):
    """A stand-in sessions directory, the shape ``ConversationLog`` writes into."""
    d = tmp_path / "sessions"
    d.mkdir()
    return d


def _write_png(path, data=PNG_BYTES):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _stored_files(sessions):
    target = attachments_dir(sessions, STEM)
    return sorted(p.name for p in target.iterdir()) if target.exists() else []


def test_local_png_is_copied_and_the_path_rewritten(sessions, tmp_path):
    """(1) The reference points at storage; the original file is untouched."""
    source = _write_png(tmp_path / "scratch" / "shot.png")
    text = f"Here it is:\n\n![a shot]({source})\n"

    out = persist_inline_images(text, sessions_dir=sessions, stem=STEM)

    stored = _stored_files(sessions)
    assert len(stored) == 1, stored
    assert stored[0].endswith("-shot.png")
    copied = attachments_dir(sessions, STEM) / stored[0]
    assert copied.read_bytes() == PNG_BYTES
    # The persisted text names the copy, not the scratch path.
    assert str(copied) in out
    assert str(source) not in out
    # Copy, never move: the agent's own file is exactly as it was.
    assert source.read_bytes() == PNG_BYTES
    # Everything around the reference survives verbatim.
    assert out.startswith("Here it is:\n\n![a shot](")
    assert out.endswith(")\n")


def test_same_image_twice_is_stored_once_and_both_refs_rewritten(sessions, tmp_path):
    """(2) Content-addressed: one file on disk, two rewritten references."""
    first = _write_png(tmp_path / "a" / "one.png")
    second = _write_png(tmp_path / "b" / "one.png")
    text = f"![x]({first}) and ![y]({second})"

    out = persist_inline_images(text, sessions_dir=sessions, stem=STEM)

    assert len(_stored_files(sessions)) == 1
    copied = attachments_dir(sessions, STEM) / _stored_files(sessions)[0]
    assert out == f"![x]({copied}) and ![y]({copied})"


def test_remote_and_data_destinations_are_untouched(sessions):
    """(3) Nothing remote is ours to copy."""
    text = (
        "![a](https://example.invalid/x.png)\n"
        "![b](http://example.invalid/y.png)\n"
        "![c](data:image/png;base64,iVBORw0KGgo=)\n"
        "![d](//example.invalid/z.png)\n"
    )
    assert persist_inline_images(text, sessions_dir=sessions, stem=STEM) == text
    assert not attachments_dir(sessions, STEM).exists()


def test_missing_file_is_left_alone_without_raising(sessions, tmp_path):
    """(4) A reference whose file is already gone keeps its markup."""
    gone = tmp_path / "scratch" / "vanished.png"
    text = f"![gone]({gone})"
    assert persist_inline_images(text, sessions_dir=sessions, stem=STEM) == text
    assert not attachments_dir(sessions, STEM).exists()


def test_oversize_image_is_skipped(sessions, tmp_path, monkeypatch):
    """(5) Session history is a conversation log, not a media store."""
    monkeypatch.setattr("kiro_crew.chat_attachments.MAX_ATTACHMENT_BYTES", 16)
    big = _write_png(tmp_path / "scratch" / "big.png")
    assert len(big.read_bytes()) > 16
    text = f"![big]({big})"
    assert persist_inline_images(text, sessions_dir=sessions, stem=STEM) == text
    assert not attachments_dir(sessions, STEM).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
def test_symlink_is_not_followed_or_copied(sessions, tmp_path):
    """(6) A link planted where a screenshot was expected pulls in nothing.

    The hazard is not a broken picture: the copy lands in session storage, which
    the dashboard serves. Following a link would let LLM-authored markup name any
    readable file and have it republished under the session's own directory.
    """
    secret = tmp_path / "outside" / "private.png"
    _write_png(secret, OTHER_PNG_BYTES)
    link = tmp_path / "scratch" / "shot.png"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(secret, link)

    text = f"![shot]({link})"
    out = persist_inline_images(text, sessions_dir=sessions, stem=STEM)

    assert out == text
    assert not attachments_dir(sessions, STEM).exists()


def test_a_destination_already_in_attachments_is_not_recopied(sessions, tmp_path):
    """(7) Idempotent, which is what lets the two write boundaries compose."""
    source = _write_png(tmp_path / "scratch" / "shot.png")
    once = persist_inline_images(f"![s]({source})", sessions_dir=sessions, stem=STEM)
    stored_after_first = _stored_files(sessions)
    mtimes = {
        name: (attachments_dir(sessions, STEM) / name).stat().st_mtime_ns
        for name in stored_after_first
    }

    twice = persist_inline_images(once, sessions_dir=sessions, stem=STEM)

    assert twice == once
    assert _stored_files(sessions) == stored_after_first
    for name, was in mtimes.items():
        assert (attachments_dir(sessions, STEM) / name).stat().st_mtime_ns == was


def test_remove_attachments_reclaims_the_directory(sessions, tmp_path):
    """(8) Session content dies with the session."""
    source = _write_png(tmp_path / "scratch" / "shot.png")
    persist_inline_images(f"![s]({source})", sessions_dir=sessions, stem=STEM)
    target = attachments_dir(sessions, STEM)
    assert target.is_dir()

    assert remove_attachments(sessions, STEM) is True
    assert not target.exists()
    # The answer is "is it gone", not "did I unlink something", so a session that
    # never had attachments is already in the state a deleter needs.
    assert remove_attachments(sessions, STEM) is True


def test_a_residue_reports_not_gone(sessions, tmp_path):
    """A leftover must be visible to the caller, which fails the delete closed."""
    source = _write_png(tmp_path / "scratch" / "shot.png")
    persist_inline_images(f"![s]({source})", sessions_dir=sessions, stem=STEM)
    # A subdirectory is deliberately not recursed into: this code never writes
    # one, so something else did and a human should look.
    (attachments_dir(sessions, STEM) / "unexpected").mkdir()

    assert remove_attachments(sessions, STEM) is False
    assert attachments_dir(sessions, STEM).exists()


def test_a_row_stops_after_the_per_message_image_cap(sessions, tmp_path, monkeypatch):
    """The copy runs under the session lock, so one row's work has a ceiling."""
    monkeypatch.setattr("kiro_crew.chat_attachments.MAX_IMAGES_PER_MESSAGE", 2)
    sources = [
        _write_png(tmp_path / "scratch" / f"s{i}.png", PNG_BYTES + bytes([i])) for i in range(4)
    ]
    text = " ".join(f"![s{i}]({p})" for i, p in enumerate(sources))

    out = persist_inline_images(text, sessions_dir=sessions, stem=STEM)

    assert len(_stored_files(sessions)) == 2
    # Budget spent in reading order: the first two are preserved, the rest keep
    # their original markup.
    assert str(sources[0]) not in out and str(sources[1]) not in out
    assert str(sources[2]) in out and str(sources[3]) in out


def test_a_row_stops_after_the_per_message_byte_cap(sessions, tmp_path, monkeypatch):
    """A byte ceiling as well, since one image may be far larger than another."""
    monkeypatch.setattr("kiro_crew.chat_attachments.MAX_BYTES_PER_MESSAGE", len(PNG_BYTES))
    first = _write_png(tmp_path / "scratch" / "a.png", PNG_BYTES)
    second = _write_png(tmp_path / "scratch" / "b.png", OTHER_PNG_BYTES)
    out = persist_inline_images(f"![a]({first}) ![b]({second})", sessions_dir=sessions, stem=STEM)

    assert len(_stored_files(sessions)) == 1
    assert str(first) not in out
    assert str(second) in out


def test_a_repeated_image_is_not_charged_twice_to_the_budget(sessions, tmp_path, monkeypatch):
    """Content-addressing means a repeat costs no disk, so it costs no budget."""
    monkeypatch.setattr("kiro_crew.chat_attachments.MAX_BYTES_PER_MESSAGE", len(PNG_BYTES))
    first = _write_png(tmp_path / "a" / "one.png")
    second = _write_png(tmp_path / "b" / "one.png")

    out = persist_inline_images(f"![x]({first}) ![y]({second})", sessions_dir=sessions, stem=STEM)

    stored = _stored_files(sessions)
    assert len(stored) == 1
    copied = attachments_dir(sessions, STEM) / stored[0]
    assert out == f"![x]({copied}) ![y]({copied})"


def test_a_path_needing_markdown_quoting_is_angle_wrapped(tmp_path):
    """A sessions directory holding a space still yields renderable markup."""
    sessions = tmp_path / "My Sessions"
    sessions.mkdir()
    source = _write_png(tmp_path / "scratch" / "shot.png")

    out = persist_inline_images(f"![s]({source})", sessions_dir=sessions, stem=STEM)

    stored = next(attachments_dir(sessions, STEM).iterdir())
    assert out == f"![s](<{stored}>)"


def test_fenced_and_escaped_references_are_left_as_written(sessions, tmp_path):
    """Literal text, not markup -- the shared scanner already knows the difference."""
    source = _write_png(tmp_path / "scratch" / "shot.png")
    text = f"```\n![s]({source})\n```\n\n\\![s]({source})\n"
    assert persist_inline_images(text, sessions_dir=sessions, stem=STEM) == text
    assert not attachments_dir(sessions, STEM).exists()


def test_non_image_extension_is_skipped(sessions, tmp_path):
    """The viewer would refuse it, so copying the bytes buys nothing."""
    doc = tmp_path / "scratch" / "notes.txt"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("not an image", encoding="utf-8")
    text = f"![n]({doc})"
    assert persist_inline_images(text, sessions_dir=sessions, stem=STEM) == text


def test_relative_destination_is_skipped(sessions):
    """A relative path has no stable meaning off the agent's working directory."""
    text = "![r](./shot.png)"
    assert persist_inline_images(text, sessions_dir=sessions, stem=STEM) == text


def test_the_read_goes_through_the_house_chokepoint(sessions, tmp_path, monkeypatch):
    """The security decision on the read belongs to one shared seam.

    Pinned because a hand-rolled ``os.open`` here would silently lose what that
    helper adds: the reparse-point refusal on Windows (there is no
    ``O_NOFOLLOW``), the hardlink refusal, and validation of the descriptor
    actually opened rather than of the path.
    """
    import kiro_crew.chat_attachments as mod

    real = mod.safe_read_file_bytes_nolink
    calls: list[tuple[str, object]] = []

    def spy(path, *args, **kwargs):
        calls.append((path, kwargs.get("max_bytes")))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(mod, "safe_read_file_bytes_nolink", spy)
    source = _write_png(tmp_path / "scratch" / "shot.png")

    persist_inline_images(f"![s]({source})", sessions_dir=sessions, stem=STEM)

    assert calls == [(str(source), MAX_ATTACHMENT_BYTES)]


def test_an_oversize_refusal_from_the_chokepoint_is_not_an_error(sessions, tmp_path, monkeypatch):
    """The chokepoint RAISES on oversize where the rest of it returns None."""
    import kiro_crew.chat_attachments as mod

    def boom(path, *args, **kwargs):
        raise mod.FileTooLargeError("too big")

    monkeypatch.setattr(mod, "safe_read_file_bytes_nolink", boom)
    source = _write_png(tmp_path / "scratch" / "shot.png")
    text = f"![s]({source})"

    assert persist_inline_images(text, sessions_dir=sessions, stem=STEM) == text
    assert not attachments_dir(sessions, STEM).exists()


def test_default_ceiling_is_the_documented_one():
    """The cap is a stated part of the contract, not an implementation detail."""
    assert MAX_ATTACHMENT_BYTES == 25 * 1024 * 1024
