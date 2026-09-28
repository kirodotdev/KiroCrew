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
from pathlib import Path

import pytest

from kiro_crew.chat_attachments import (
    MAX_ATTACHMENT_BYTES,
    _encode_destination,
    attachments_dir,
    persist_inline_images,
    purge_staged_attachments,
    restore_staged_attachments,
    same_text_modulo_images,
    stage_attachments_removal,
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


def _dest(path):
    """A stored path as this module now spells it in a markdown destination.

    Identity on POSIX. On Windows the separators are forward slashes, because
    the native spelling is not a fixed point of the parser that reads the row
    back -- see ``_commonmark_unescape`` and the cases at the end of this file.
    """
    return str(path).replace(os.sep, "/")


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
    assert _dest(copied) in out
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
    assert out == f"![x]({_dest(copied)}) and ![y]({_dest(copied)})"


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


def test_staging_moves_the_directory_aside_in_one_step(sessions, tmp_path):
    """(8) Session content dies with the session -- in three all-or-nothing steps."""
    source = _write_png(tmp_path / "scratch" / "shot.png")
    persist_inline_images(f"![s]({source})", sessions_dir=sessions, stem=STEM)
    target = attachments_dir(sessions, STEM)
    before = _stored_files(sessions)

    staged = stage_attachments_removal(sessions, STEM)

    assert staged is not None and staged.is_dir()
    assert staged.parent == target.parent and staged != target
    assert not target.exists()
    # Every byte is still there, under the staged name: nothing is destroyed until
    # the transcript is gone.
    assert sorted(f.name for f in staged.iterdir()) == before
    assert purge_staged_attachments(staged) is True
    assert not staged.exists()
    # A session that never had attachments has nothing to stage.
    assert stage_attachments_removal(sessions, STEM) is None


def test_a_foreign_entry_rides_along_and_is_purged_after_the_transcript(sessions, tmp_path):
    """The one-rename stage cannot half-fail on a foreign entry: the whole
    directory moves, so a retained transcript never points at pictures already
    gone, by construction."""
    sources = [
        _write_png(tmp_path / "scratch" / f"s{i}.png", PNG_BYTES + bytes([i])) for i in range(3)
    ]
    text = " ".join(f"![s{i}]({p})" for i, p in enumerate(sources))
    persist_inline_images(text, sessions_dir=sessions, stem=STEM)
    (attachments_dir(sessions, STEM) / "zzz-unexpected").mkdir()

    staged = stage_attachments_removal(sessions, STEM)

    assert staged is not None
    assert len(list(staged.iterdir())) == 4  # three images and the foreign dir, all intact
    assert purge_staged_attachments(staged) is True


def test_a_planted_link_is_refused_before_anything_moves(sessions, tmp_path):
    """A link where the directory should be must not be renamed (which would
    rename the link, not what it points at) nor followed."""
    if sys.platform == "win32":
        pytest.skip("symlink creation needs a privilege on Windows")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.png").write_bytes(PNG_BYTES)
    attachments_dir(sessions, STEM).symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(OSError):
        stage_attachments_removal(sessions, STEM)

    assert attachments_dir(sessions, STEM).is_symlink()
    assert (elsewhere / "keep.png").exists()


def test_restore_puts_the_directory_back_under_its_served_name(sessions, tmp_path):
    """When the transcript could not be deleted, its references must resolve again."""
    source = _write_png(tmp_path / "scratch" / "shot.png")
    persist_inline_images(f"![s]({source})", sessions_dir=sessions, stem=STEM)
    before = _stored_files(sessions)
    staged = stage_attachments_removal(sessions, STEM)
    assert staged is not None

    restore_staged_attachments(staged, sessions, STEM)

    assert not staged.exists()
    assert _stored_files(sessions) == before


def test_a_purge_residue_is_reported_not_raised(sessions, tmp_path, monkeypatch):
    """A file the OS will not release (Windows: open in a viewer) leaves an orphan
    the operator is told about; the delete itself already succeeded."""
    import kiro_crew.chat_attachments as mod

    source = _write_png(tmp_path / "scratch" / "shot.png")
    persist_inline_images(f"![s]({source})", sessions_dir=sessions, stem=STEM)
    staged = stage_attachments_removal(sessions, STEM)
    assert staged is not None
    monkeypatch.setattr(mod.shutil, "rmtree", lambda *_a, **_k: None)

    assert purge_staged_attachments(staged) is False
    assert staged.is_dir()


def test_a_title_repeating_the_path_does_not_hide_the_destination(sessions, tmp_path):
    """`![x](/a.png "/a.png")`: the destination is the first occurrence after
    `](`, not the last in the markup -- or the title is rewritten and the
    persisted reference keeps naming the scratch file."""
    source = _write_png(tmp_path / "scratch" / "shot.png")
    out = persist_inline_images(f'![x]({source} "{source}")', sessions_dir=sessions, stem=STEM)
    stored = _stored_files(sessions)
    assert len(stored) == 1
    copied = attachments_dir(sessions, STEM) / stored[0]
    assert out == f'![x]({_dest(copied)} "{source}")'


def test_a_destination_that_cannot_be_stored_still_counts_against_the_cap(
    sessions, tmp_path, monkeypatch
):
    """The cap bounds work under the lock, so an oversize or missing destination
    is charged too: a row of unstorable pictures cannot read without limit."""
    import kiro_crew.chat_attachments as mod

    monkeypatch.setattr(mod, "MAX_IMAGES_PER_MESSAGE", 2)
    looked_at: list[str] = []
    real = mod._store_one

    def counting(raw_dest, target_dir, budget):
        looked_at.append(raw_dest)
        return real(raw_dest, target_dir, budget)

    monkeypatch.setattr(mod, "_store_one", counting)
    missing = [tmp_path / "scratch" / f"gone{i}.png" for i in range(3)]
    good = _write_png(tmp_path / "scratch" / "good.png")
    text = " ".join(f"![m]({p})" for p in missing) + f" ![g]({good})"

    out = persist_inline_images(text, sessions_dir=sessions, stem=STEM)

    assert looked_at == [str(missing[0]), str(missing[1])]
    assert _stored_files(sessions) == []
    assert str(good) in out


def test_an_escaped_destination_is_rewritten_not_orphaned(sessions, tmp_path):
    """`![x](/scratch/shot\\(1\\).png)`: the scanner parses `shot(1).png`, which
    is not a substring of the markup. The destination must be located as a span,
    or the copy lands and the reference keeps naming the scratch file."""
    source = _write_png(tmp_path / "scratch" / "shot(1).png")
    escaped = str(source).replace("(", "\\(").replace(")", "\\)")

    out = persist_inline_images(f"![x]({escaped})", sessions_dir=sessions, stem=STEM)

    stored = _stored_files(sessions)
    assert len(stored) == 1
    copied = attachments_dir(sessions, STEM) / stored[0]
    assert out == f"![x]({_dest(copied)})"


def test_a_repeat_after_the_cap_is_still_rewritten(sessions, tmp_path, monkeypatch):
    """The cap bounds distinct destinations. A repeat of one already stored costs
    nothing and must not be left naming the scratch file while its twin names storage."""
    monkeypatch.setattr("kiro_crew.chat_attachments.MAX_IMAGES_PER_MESSAGE", 1)
    first = _write_png(tmp_path / "scratch" / "a.png")
    second = _write_png(tmp_path / "scratch" / "b.png", OTHER_PNG_BYTES)

    out = persist_inline_images(
        f"![a]({first}) ![b]({second}) ![a2]({first})", sessions_dir=sessions, stem=STEM
    )

    stored = _stored_files(sessions)
    assert len(stored) == 1
    copied = attachments_dir(sessions, STEM) / stored[0]
    assert out == f"![a]({_dest(copied)}) ![b]({second}) ![a2]({_dest(copied)})"


def test_a_repeated_reference_is_read_once_per_row(sessions, tmp_path, monkeypatch):
    """The copy hashes the whole file before it can see it is already stored, so a
    row repeating one reference must not pay that read per repeat."""
    import kiro_crew.chat_attachments as mod

    source = _write_png(tmp_path / "scratch" / "shot.png")
    reads: list[str] = []
    real = mod.safe_read_file_bytes_nolink

    def counting(path, **kw):
        reads.append(path)
        return real(path, **kw)

    monkeypatch.setattr(mod, "safe_read_file_bytes_nolink", counting)
    out = persist_inline_images(
        " ".join(f"![x{i}]({source})" for i in range(8)), sessions_dir=sessions, stem=STEM
    )

    assert reads == [str(source)]
    stored = _stored_files(sessions)
    assert len(stored) == 1
    assert str(source) not in out
    assert out.count(stored[0]) == 8


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
    assert out == f"![x]({_dest(copied)}) ![y]({_dest(copied)})"


def test_a_path_needing_markdown_quoting_is_angle_wrapped(tmp_path):
    """A sessions directory holding a space still yields renderable markup."""
    sessions = tmp_path / "My Sessions"
    sessions.mkdir()
    source = _write_png(tmp_path / "scratch" / "shot.png")

    out = persist_inline_images(f"![s]({source})", sessions_dir=sessions, stem=STEM)

    stored = next(attachments_dir(sessions, STEM).iterdir())
    assert out == f"![s](<{_dest(stored)}>)"


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


def test_same_text_modulo_images_pairs_a_preserved_copy_with_its_original(sessions, tmp_path):
    """The corroboration the two write boundaries need when they met one message
    at different times: equal text, one image destination stored, the other the
    (now vanished) scratch path. Angle wrapping and the digest prefix are both
    noise; the stored name is compared by the basename the copy kept.
    """
    stored = attachments_dir(sessions, STEM) / "0123456789abcdef-my_shot_1_.png"
    scratch = tmp_path / "scratch dir" / "my shot(1).png"  # never written
    left = f"see ![s](<{scratch}>) done"
    right = f"see ![s]({stored}) done"
    assert same_text_modulo_images(left, right, sessions_dir=sessions, stem=STEM)
    assert same_text_modulo_images(right, left, sessions_dir=sessions, stem=STEM)
    assert same_text_modulo_images(right, right, sessions_dir=sessions, stem=STEM)


def test_same_text_modulo_images_pairs_a_copy_stored_on_a_unc_data_home(monkeypatch, tmp_path):
    r"""The roaming-profile case: the stored copy's own destination is ``//...``.

    A markdown destination cannot carry ``\\fileserver\...`` -- the CommonMark
    parser reading it drops the backslashes -- so ``_posix_separators`` spells a
    stored Windows path with forward slashes, and on a roaming profile the data
    home is itself a share, which makes that spelling ``//fileserver/...``. Read
    as a protocol-relative URL, the stored side contributes no reference at all:
    ``stored_seen`` stays false, the two id-matched rows fail to corroborate, and
    the pair the two write boundaries exist to collapse becomes two history rows.

    Purely lexical throughout (no path here is written), and every spelling is
    forward-slash with ``peek_data_home`` patched, so the gate answers the same on
    the Linux CI box as on Windows -- the reason the UNC-gate tests give.
    """
    from kiro_crew.messaging import outbound_files as module

    monkeypatch.setattr(module, "os", type("OS", (), {"name": "nt"})(), raising=False)
    unc_home = "//fileserver/home/me/.kiro/crew"
    monkeypatch.setattr("kiro_crew.config.paths.peek_data_home", lambda: Path(unc_home))

    unc_sessions = f"{unc_home}/sessions"
    stored = f"{unc_sessions}/{STEM}.attachments/0123456789abcdef-shot.png"
    scratch = tmp_path / "scratch" / "shot.png"  # never written; the agent's own copy
    left = f"see ![s]({scratch}) done"
    right = f"see ![s]({stored}) done"
    assert same_text_modulo_images(left, right, sessions_dir=Path(unc_sessions), stem=STEM)
    assert same_text_modulo_images(right, left, sessions_dir=Path(unc_sessions), stem=STEM)
    # A share this gateway does not write to is still not a local path, so it
    # corroborates nothing -- the allowlist, not the `//`, is what decides.
    foreign = f"//evil/share/{STEM}.attachments/0123456789abcdef-shot.png"
    assert not same_text_modulo_images(
        left, f"see ![s]({foreign}) done", sessions_dir=Path(unc_sessions), stem=STEM
    )


def test_same_text_modulo_images_does_not_pair_different_text_or_unstored_paths(sessions, tmp_path):
    stored = attachments_dir(sessions, STEM) / "0123456789abcdef-shot.png"
    scratch = tmp_path / "scratch" / "shot.png"
    other = tmp_path / "elsewhere" / "shot.png"
    # Different prose around the same image: not the same message.
    assert not same_text_modulo_images(
        f"a ![s]({scratch})", f"b ![s]({stored})", sessions_dir=sessions, stem=STEM
    )
    # Two different images, neither preserved here: nothing corroborates.
    assert not same_text_modulo_images(
        f"a ![s]({scratch})", f"a ![s]({other})", sessions_dir=sessions, stem=STEM
    )
    # A copy stored under ANOTHER session's directory is not this session's.
    foreign = attachments_dir(sessions, "other-session") / "0123456789abcdef-shot.png"
    assert not same_text_modulo_images(
        f"a ![s]({scratch})", f"a ![s]({foreign})", sessions_dir=sessions, stem=STEM
    )
    # No images at all and unequal: plainly different.
    assert not same_text_modulo_images("a", "b", sessions_dir=sessions, stem=STEM)


def test_an_escaped_bracket_in_the_alt_text_does_not_end_it(sessions, tmp_path):
    """The rewrite locates the destination past the alt text by the scanner's own
    escape rule, so an alt holding ``\\]`` is still one alt and the path after it
    is still found and rewritten.
    """
    png = _write_png(tmp_path / "scratch" / "shot.png")
    text = f"![a \\] b]({png})"
    out = persist_inline_images(text, sessions_dir=sessions, stem=STEM)
    assert out.startswith("![a \\] b](")
    assert str(png) not in out
    assert _stored_files(sessions)


def test_a_long_run_of_image_openers_is_left_alone_quickly(sessions, tmp_path):
    """Bounded on hostile markup: the scanner is quadratic in the number of ``![``
    openers, and this runs under the session lock, so a row past the opener bound
    is left as written -- in milliseconds, not the tens of seconds the scan costs.
    """
    import time

    png = _write_png(tmp_path / "scratch" / "shot.png")
    text = "![" * 20000 + f"![shot]({png})"
    started = time.monotonic()
    assert persist_inline_images(text, sessions_dir=sessions, stem=STEM) == text
    assert time.monotonic() - started < 2.0
    assert not _stored_files(sessions)
    # And the corroboration helper takes the same exit.
    assert not same_text_modulo_images(text, text + " ", sessions_dir=sessions, stem=STEM)


# ---------------------------------------------------------------------------
# A destination has to survive the parser that reads it back.
# ---------------------------------------------------------------------------

#: CommonMark's backslash rule, mirrored here so the cases below can state the
#: property directly: inside a link destination a backslash before ASCII
#: punctuation is an ESCAPE and is dropped; before anything else it is literal.
#: The dashboard's markdown parser applies this to every destination this module
#: writes, so a destination resolves to the file it names only when it is a FIXED
#: POINT of the rule.
_ASCII_PUNCT = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"


def _commonmark_unescape(dest):
    """*dest* as a CommonMark parser resolves it. Mirrors :data:`_ASCII_PUNCT`."""
    out = []
    i = 0
    while i < len(dest):
        if dest[i] == "\\" and i + 1 < len(dest) and dest[i + 1] in _ASCII_PUNCT:
            out.append(dest[i + 1])
            i += 2
            continue
        out.append(dest[i])
        i += 1
    return "".join(out)


@pytest.mark.parametrize(
    "stored",
    [
        # The default data home is ~/.kiro/crew, so a native destination always
        # carries a backslash-then-dot sequence. This is the ordinary case.
        r"C:\Users\me\.kiro\crew\sessions\chat.attachments\0123456789abcdef-shot.png",
        r"\\nas\team\.kiro\crew\sessions\chat.attachments\0123456789abcdef-shot.png",
    ],
    ids=["drive", "unc"],
)
def test_a_windows_destination_is_written_so_the_parser_reads_it_back(stored):
    """Separators are written as ``/`` -- the spelling the reader resolves.

    Driven through the encoder rather than :func:`persist_inline_images` so the
    case runs on every platform: a drive-letter path is not absolute off Windows,
    so the end-to-end route below cannot reach the rewrite there at all.
    """
    # Guard the guard: the native spelling is NOT a fixed point of the parser's
    # own rule, so the assertion after it cannot pass vacuously.
    assert _commonmark_unescape(stored) != stored

    encoded = _encode_destination(stored, angle_wrapped=False)

    assert _commonmark_unescape(encoded) == encoded
    assert encoded == stored.replace("\\", "/")


def test_a_posix_destination_is_returned_by_identity():
    """Nothing changes off Windows: there are no separators to rewrite."""
    posix = "/home/me/.kiro/crew/sessions/chat.attachments/0123456789abcdef-shot.png"
    assert _encode_destination(posix, angle_wrapped=False) == posix
    assert _commonmark_unescape(posix) == posix


@pytest.mark.skipif(os.name != "nt", reason="a drive-letter path is absolute on Windows only")
def test_a_persisted_windows_reference_resolves_to_the_file_it_stored(tmp_path):
    """End to end on a real Windows filesystem, with the home shaped as shipped.

    The defect this pins: the rewritten destination named the attachment under
    ``.kiro``, the parser read that separator-then-dot as an escape, and the
    dashboard asked ``/api/file-raw`` for a path one directory level short of the
    file -- so every persisted image rendered broken.
    """
    sessions = tmp_path / ".kiro" / "crew" / "sessions"
    sessions.mkdir(parents=True)
    source = _write_png(tmp_path / "scratch" / "shot.png")

    out = persist_inline_images(f"![s]({source})", sessions_dir=sessions, stem=STEM)

    stored = next(attachments_dir(sessions, STEM).iterdir())
    dest = out[out.index("](") + 2 : -1]
    assert "\\" not in dest
    # What the reader resolves is the file that was actually written.
    assert Path(_commonmark_unescape(dest)) == stored


def test_persisting_an_already_rewritten_row_changes_nothing(sessions, tmp_path):
    """The fixed point, on every platform: a second pass re-encodes to itself.

    This is what lets the slot save re-serialize its whole window on every flush,
    and what makes the repair below safe to run on every persisted row.
    """
    source = _write_png(tmp_path / "scratch" / "shot.png")

    once = persist_inline_images(f"![s]({source})", sessions_dir=sessions, stem=STEM)
    twice = persist_inline_images(once, sessions_dir=sessions, stem=STEM)

    assert twice == once
    assert len(_stored_files(sessions)) == 1


@pytest.mark.skipif(os.name != "nt", reason="a drive-letter path is absolute on Windows only")
def test_a_row_holding_the_pre_repair_spelling_is_repaired_in_place(tmp_path):
    """Rows already on disk are reached, because this is where they are rewritten.

    A build that wrote the destination natively left a row whose reader resolves
    a DIFFERENT path. Such a row names a file that is already stored, so nothing
    is copied -- but the destination is re-encoded, and the row heals the next
    time it is persisted.

    The home is shaped as shipped (``~/.kiro/crew``): it is the dot-led segment
    that makes the native spelling unreadable, so a sessions directory without
    one could not express the defect at all.
    """
    sessions = tmp_path / ".kiro" / "crew" / "sessions"
    sessions.mkdir(parents=True)
    source = _write_png(tmp_path / "scratch" / "shot.png")
    persist_inline_images(f"![s]({source})", sessions_dir=sessions, stem=STEM)
    stored = next(attachments_dir(sessions, STEM).iterdir())

    legacy = f"![s]({stored})"  # str(Path) -- exactly what the old encoder emitted
    # Guard the guard: the legacy row really is unreadable as written.
    assert Path(_commonmark_unescape(legacy[legacy.index("](") + 2 : -1])) != stored

    out = persist_inline_images(legacy, sessions_dir=sessions, stem=STEM)

    dest = out[out.index("](") + 2 : -1]
    assert Path(_commonmark_unescape(dest)) == stored
    # Repair only: no second copy of bytes that were already stored.
    assert len(_stored_files(sessions)) == 1
