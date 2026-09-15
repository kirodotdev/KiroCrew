"""A transcript entry that is not a file must be refused, not opened.

`ensure_local_transcript` decides whether this slot's conversation is already on disk and,
if not, restores it from the backup bucket. Both halves handle names and files that
came from OUTSIDE the container: boot restores nothing, so what is on disk was put
there by an earlier turn or by the restore, and the restore's bytes and keys come from
the bucket.

So the shape of an entry is not something this process gets to assume. A directory, a
FIFO, a socket, a symlink or a hard-linked file where a transcript should be is refused
by name, before the backend is handed a path it will open and append to. Crashing
mid-turn on whatever `open()` raises is the worst of the three outcomes, because a
refusal is auditable and a crash is not.

Shape is decided on the descriptor, never on the name: `Path.exists()` follows a link,
answers true for a directory, and is a separate resolution from the open that follows
it.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest
from container.front import transcript as t

from ._settings_helper import make_settings


def _path(settings, stem: str = "dashboard_slot-1") -> Path:
    path = t.local_transcript_path(settings, stem)
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_a_regular_file_reads_as_present(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    path = _path(settings)
    path.write_bytes(b'{"role":"user"}\n')

    assert t._probe_local_entry(path) is True


def test_nothing_there_reads_as_absent(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    assert t._probe_local_entry(_path(settings)) is False


def test_a_directory_is_refused(tmp_path: Path) -> None:
    """The shape `Path.exists()` gets wrong most usefully: it answers true."""
    settings = make_settings(tmp_path)
    path = _path(settings)
    path.mkdir()

    with pytest.raises(t.TranscriptUnavailable, match="not a regular file"):
        t._probe_local_entry(path)


def test_a_symlink_is_refused_and_names_the_path(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    path = _path(settings)
    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_bytes(b"not this file\n")
    path.symlink_to(elsewhere)

    with pytest.raises(t.TranscriptUnavailable, match="symlink") as exc:
        t._probe_local_entry(path)
    assert str(path) in str(exc.value), "an operator needs the path in the message"
    assert elsewhere.read_bytes() == b"not this file\n"


def test_a_fifo_is_refused_rather_than_hanging(tmp_path: Path) -> None:
    """Refused, and refused WITHOUT blocking.

    Opening a FIFO for reading waits for a writer, so the probe would hang forever
    rather than answer. ``O_NONBLOCK`` is what makes this a refusal; the timeout on
    this test is what would catch its removal.
    """
    settings = make_settings(tmp_path)
    path = _path(settings)
    os.mkfifo(path)

    with pytest.raises(t.TranscriptUnavailable, match="not a regular file"):
        t._probe_local_entry(path)


def test_a_hard_linked_transcript_is_refused(tmp_path: Path) -> None:
    """A regular file, and still not safe: the backend appends to this path.

    A second name for the same inode receives everything the backend writes, which is
    a customer's conversation going somewhere nobody asked for.
    """
    settings = make_settings(tmp_path)
    path = _path(settings)
    other = tmp_path / "other-name"
    other.write_bytes(b"seed\n")
    os.link(other, path)

    with pytest.raises(t.TranscriptUnavailable, match="links"):
        t._probe_local_entry(path)


def test_a_refused_shape_fails_the_turn_rather_than_fetching(tmp_path: Path) -> None:
    """End to end through the caller: the refusal is the turn's answer.

    ``ensure_local_transcript``'s contract is that ``TranscriptUnavailable`` fails the
    turn and every other outcome lets it proceed. A bad shape must take the first
    path -- a turn served on top of one would have the backend create a fresh history
    and, once a durability writer exists, overwrite the customer's real one.
    """
    settings = make_settings(tmp_path)
    path = _path(settings)
    path.mkdir()

    class _Reader:
        def get(self, key):  # pragma: no cover - must never be reached
            raise AssertionError("fetched despite a refused local entry")

    with pytest.raises(t.TranscriptUnavailable):
        asyncio.run(t.ensure_local_transcript(settings, "dashboard_slot-1", reader=_Reader()))


@pytest.mark.parametrize("shape", ["directory", "symlink", "fifo", "hardlink"])
def test_every_bad_shape_is_refused_through_the_public_entry_point(
    tmp_path: Path, shape: str
) -> None:
    """Each shape pinned at the CALL SITE, not only against the private probe.

    The unit tests above would keep passing if the probe were written and never
    called, which is the failure mode this whole series is about. These reach it the
    way a turn does.
    """
    settings = make_settings(tmp_path)
    path = _path(settings)
    if shape == "directory":
        path.mkdir()
    elif shape == "symlink":
        target = tmp_path / "target.jsonl"
        target.write_bytes(b"x\n")
        path.symlink_to(target)
    elif shape == "fifo":
        os.mkfifo(path)
    else:
        other = tmp_path / "other-name"
        other.write_bytes(b"seed\n")
        os.link(other, path)

    class _Reader:
        def get(self, key):  # pragma: no cover - must never be reached
            raise AssertionError("fetched despite a refused local entry")

    with pytest.raises(t.TranscriptUnavailable):
        asyncio.run(t.ensure_local_transcript(settings, "dashboard_slot-1", reader=_Reader()))


def test_the_write_refuses_a_symlinked_sessions_directory(tmp_path: Path) -> None:
    """``mkdir(exist_ok=True)`` succeeds on a link to a directory, so it is checked."""
    settings = make_settings(tmp_path)
    real = tmp_path / "real-sessions"
    real.mkdir()
    sessions = settings.sessions_dir
    shutil.rmtree(sessions)
    sessions.symlink_to(real, target_is_directory=True)

    with pytest.raises(t.TranscriptUnavailable, match="sessions directory is a symlink"):
        t._write_without_clobbering(sessions / "dashboard_slot-1.jsonl", b"x")

    assert not any(real.iterdir()), "nothing was written through the link"


def test_the_write_still_refuses_to_clobber(tmp_path: Path) -> None:
    """Non-vacuity for the guard above: the ordinary paths still behave.

    A guard that refused every write would make the test above pass while breaking
    restore entirely.
    """
    settings = make_settings(tmp_path)
    path = _path(settings)

    t._write_without_clobbering(path, b"first\n")
    assert path.read_bytes() == b"first\n"

    t._write_without_clobbering(path, b"second\n")
    assert path.read_bytes() == b"first\n", "the copy on disk is the newer one and wins"
    leftovers = [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, leftovers


@pytest.mark.parametrize("shape", ["directory", "symlink", "fifo", "hardlink"])
def test_a_raced_publish_validates_the_entry_that_won(tmp_path: Path, shape: str) -> None:
    """A collision means the file the turn will use is not the one just written.

    The pre-fetch probe answered about a different entry, so the winner has had none of
    this module's checks applied to it. Keeping it is still right when it is a
    transcript -- whoever wrote it has the newer history -- but it has to BE one, and
    the same helper decides that, so the collision path cannot drift from the path it
    mirrors.
    """
    settings = make_settings(tmp_path)
    path = _path(settings)
    if shape == "directory":
        path.mkdir()
    elif shape == "symlink":
        target = tmp_path / "raced.jsonl"
        target.write_bytes(b"x\n")
        path.symlink_to(target)
    elif shape == "fifo":
        os.mkfifo(path)
    else:
        other = tmp_path / "raced-other"
        other.write_bytes(b"seed\n")
        os.link(other, path)

    with pytest.raises(t.TranscriptUnavailable):
        t._write_without_clobbering(path, b"fetched\n")


def test_a_raced_publish_with_a_real_transcript_keeps_it(tmp_path: Path) -> None:
    """The ordinary collision is not an error, and must not become one.

    Two turns for the same conversation can race, and the loser's job is to leave the
    winner's bytes alone rather than fail a turn that has a perfectly good transcript.
    """
    settings = make_settings(tmp_path)
    path = _path(settings)
    path.write_bytes(b"arrived first\n")

    t._write_without_clobbering(path, b"fetched\n")

    assert path.read_bytes() == b"arrived first\n"
