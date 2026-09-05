"""A swapped PARENT directory must not be traversed either.

``O_NOFOLLOW`` on a whole path constrains only the final component. This module said
so in its own docstring and left the gap open: an agent that controls a nested path
can replace a PARENT with a link to ``/proc/self`` after enumeration, and a
final-component-only check then opens ``environ`` happily and uploads it.

The reader now walks each component under the enumerated root with ``O_NOFOLLOW``, so
the open fails at the component that was swapped. These tests stage that directly --
the parent is replaced, not the file.
"""

from __future__ import annotations

import os
import shutil

import pytest
from container.backup import sidecar

_needs_dir_fd = pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_DIRECTORY"),
    reason="component-wise opening needs dir_fd support; the image is Linux",
)


@_needs_dir_fd
def test_a_swapped_parent_directory_is_refused(tmp_path):
    root = tmp_path / "unit"
    (root / "sessions" / "sub").mkdir(parents=True)
    victim = root / "sessions" / "sub" / "a.jsonl"
    victim.write_bytes(b'{"turn": 1}\n')

    # Intact first, so the test proves the walk WORKS before proving it refuses.
    assert sidecar._read_nofollow(victim, root) == b'{"turn": 1}\n'

    shutil.rmtree(root / "sessions" / "sub")
    (root / "sessions" / "sub").symlink_to("/proc/self")

    with pytest.raises(OSError):
        sidecar._read_nofollow(victim, root)


@_needs_dir_fd
def test_the_process_environment_never_comes_back(tmp_path):
    """The property is the bytes, not the exception."""
    root = tmp_path / "unit"
    (root / "d").mkdir(parents=True)
    victim = root / "d" / "environ"
    victim.write_bytes(b"harmless\n")
    shutil.rmtree(root / "d")
    (root / "d").symlink_to("/proc/self")

    try:
        data = sidecar._read_nofollow(victim, root)
    except OSError:
        data = b""
    # A real /proc/self/environ carries NUL-separated KEY=VALUE pairs.
    assert b"PATH=" not in data and b"HOME=" not in data, "the task environment was read"


@_needs_dir_fd
def test_a_deeply_nested_real_file_still_reads(tmp_path):
    """The refusal must not cost legitimate nesting, which the archive dir uses."""
    root = tmp_path / "unit"
    deep = root / "sessions" / "archive" / "2026" / "09"
    deep.mkdir(parents=True)
    f = deep / "old.jsonl"
    payload = b'{"turn": 1}\n' * 3000
    f.write_bytes(payload)
    assert sidecar._read_nofollow(f, root) == payload


@_needs_dir_fd
def test_a_swapped_final_component_is_still_refused(tmp_path):
    """The original final-component protection must survive the rewrite."""
    root = tmp_path / "unit"
    root.mkdir()
    secret = tmp_path / "secret"
    secret.write_bytes(b"AWS_SESSION_TOKEN=stolen\n")
    f = root / "a.jsonl"
    f.write_bytes(b"real\n")
    f.unlink()
    f.symlink_to(secret)

    try:
        data = sidecar._read_nofollow(f, root)
    except OSError:
        data = b""
    assert b"stolen" not in data


def test_the_enumeration_reports_the_root_it_used(tmp_path):
    """The reader cannot guess the root, so the enumeration has to say.

    There are several roots plus the config files' own parent, and passing the wrong
    one would either raise on ``relative_to`` or check the wrong prefix.
    """
    from container.backup import layout

    from .test_backup_restore import make_settings

    s = make_settings(tmp_path)
    s.sessions_dir.mkdir(parents=True, exist_ok=True)
    (s.sessions_dir / "a.jsonl").write_bytes(b'{"turn": 1}\n')

    seen = list(layout.iter_backup_files(s))
    assert seen, "nothing enumerated, so this proves nothing"
    for local, _rel, root in seen:
        # The contract the reader relies on: every entry is under its reported root.
        assert local.is_relative_to(root), f"{local} is not under its reported root {root}"
