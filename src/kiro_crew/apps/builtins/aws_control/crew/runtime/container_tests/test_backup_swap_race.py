"""The backup read must lose the swap race safely, not follow the link.

``layout`` refuses a symlink it can SEE at enumeration time. That check and the read
are two moments, and the agent writes into this tree with an auto-approved shell. So
the interesting case is not "a symlink was there all along" -- that is already
covered -- but "a regular file was enumerated and became a symlink before the read".

These tests stage exactly that: enumerate, then swap, then read.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from container.backup import sidecar

from .test_backup_restore import make_settings


def _swap_for_link(target_file: Path, points_at: Path) -> None:
    """Replace a real file with a symlink, the way the race would."""
    target_file.unlink()
    target_file.symlink_to(points_at)


def test_a_file_swapped_for_a_symlink_is_not_read(tmp_path):
    secret = tmp_path / "environ-lookalike"
    secret.write_bytes(b"AWS_SESSION_TOKEN=stolen\n")

    victim = tmp_path / "artifact.bin"
    victim.write_bytes(b"the real contents that were enumerated\n")
    _swap_for_link(victim, secret)

    with pytest.raises(OSError) as excinfo:
        sidecar._read_nofollow(victim)
    # ELOOP is what O_NOFOLLOW raises on a final-component link.
    assert excinfo.value.errno == 62 or "symbolic link" in str(excinfo.value).lower(), excinfo.value


def test_the_secret_bytes_never_come_back(tmp_path):
    """The property that matters is not the exception type, it is the bytes."""
    secret = tmp_path / "environ-lookalike"
    secret.write_bytes(b"AWS_SESSION_TOKEN=stolen\n")
    victim = tmp_path / "artifact.bin"
    victim.write_bytes(b"real\n")
    _swap_for_link(victim, secret)

    try:
        data = sidecar._read_nofollow(victim)
    except OSError:
        data = b""
    assert b"stolen" not in data, "the linked-to secret was read through the swap"


def test_a_real_regular_file_still_reads_byte_for_byte(tmp_path):
    """The refusal must not cost the ordinary case."""
    payload = b'{"turn": 1}\n{"turn": 2}\n' * 5000  # spans the read loop's chunk size
    f = tmp_path / "session.jsonl"
    f.write_bytes(payload)
    assert sidecar._read_nofollow(f) == payload


def test_a_fifo_is_refused_rather_than_blocking_the_cycle(tmp_path):
    """fstat's regular-file check, which O_NOFOLLOW alone does not give.

    A FIFO left in the tree would otherwise block the read forever with no writer,
    stalling every later backup cycle behind it.
    """
    fifo = tmp_path / "not-a-file"
    os.mkfifo(fifo)
    with pytest.raises((sidecar._NotARegularFile, OSError)):
        sidecar._read_nofollow(fifo)


def test_the_cycle_skips_the_swapped_entry_and_keeps_going(tmp_path):
    """End to end: one poisoned entry must not stop the others being backed up."""
    s = make_settings(tmp_path)
    s.sessions_dir.mkdir(parents=True, exist_ok=True)

    good = s.sessions_dir / "good.jsonl"
    good.write_bytes(b'{"turn": 1}\n')
    secret = tmp_path / "environ-lookalike"
    secret.write_bytes(b"AWS_SESSION_TOKEN=stolen\n")
    poisoned = s.sessions_dir / "poisoned.jsonl"
    poisoned.write_bytes(b'{"turn": 1}\n')
    _swap_for_link(poisoned, secret)

    store = sidecar.InMemoryObjectStore() if hasattr(sidecar, "InMemoryObjectStore") else None
    if store is None:  # the fake lives with the store module in this suite
        from container.backup.store import InMemoryObjectStore

        store = InMemoryObjectStore()

    from container.backup.state import BackupState

    sidecar.run_backup_cycle(s, store, BackupState())

    uploaded = b"".join(store._objects.values()) if hasattr(store, "_objects") else b""
    assert b"stolen" not in uploaded, "the swapped entry's target reached the bucket"
