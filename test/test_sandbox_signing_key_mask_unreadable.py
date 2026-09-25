"""The Linux mask over ``token_signing.key`` refuses reads instead of answering empty.

The launcher hides a sensitive file by binding an empty tmpfs file over it. For most
leaves an empty read is harmless. For the signing key it is not: a data-home copy run
from an agent shell (``rsync``, ``cp -a``, ``tar``) reads the mask and writes a 0-byte
``token_signing.key`` on the destination, which the gateway then refuses to replace and
answers with an ephemeral secret on every boot. The mask source for that leaf is mode 0,
so the copy fails with ``Permission denied`` instead.

These tests run the hiding region lifted verbatim from the shipped launcher, with the
same fake ``_libc`` as ``test_sandbox_mount_checked``: a real bind needs a user
namespace, which a nested sandbox cannot create. The fake records each mount's source,
and the source file is what the sandboxed process reads through the bind.
"""

from __future__ import annotations

import ctypes
import os
import runpy
import stat
import sys
import tempfile
from pathlib import Path

import pytest
from test_sandbox_mount_checked import _FakeLibc, _region

from kiro_crew import sandbox
from kiro_crew.sandbox import _build_launcher_script

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="_build_launcher_script uses POSIX-only os.getuid (#2041)",
)


def _mask_sources(tmp_path: Path, level: str) -> dict[str, Path]:
    """Run the hiding region; map each hidden file's basename to its mask source."""
    home = tmp_path / "home"
    crew = home / ".kiro" / "crew"
    crew.mkdir(parents=True)
    key = crew / "token_signing.key"
    key.write_bytes(os.urandom(32))
    netrc = home / ".netrc"
    netrc.write_text("machine example.com\n")
    ssh = home / ".ssh"
    ssh.mkdir()
    src_dir = tmp_path / "tmpfs"
    src_dir.mkdir()

    libc = _FakeLibc(fail_at=None)
    ns = {
        "_libc": libc,
        "_MS_BIND": 4096,
        "_MS_REC": 16384,
        "_MS_PRIVATE": 1 << 18,
        "_MS_RDONLY": 1,
        "_MS_REMOUNT": 32,
        "_MS_NOSUID": 2,
        "_MS_NODEV": 4,
        "_MS_NOEXEC": 8,
        "ctypes": ctypes,
        "os": os,
        "sys": sys,
        "tempfile": tempfile,
        "_tmpfs_src": str(src_dir),
        "_src_prefix": "kirocrew_sb_%d_" % os.getpid(),
        "expose_data": {},
        "EXPOSE_FILES": [],
        "SENSITIVE_DIRS": [],
        "PRIVATE_DIRS": [],
        "READONLY_DIRS": [],
        "WRITABLE_DIRS": [],
        "SENSITIVE_FILES": [str(key), str(netrc)],
        "SSH_DIR": str(ssh),
        "SSH_KNOWN_HOSTS": str(ssh / "known_hosts"),
        "HIDE_SSH": False,
    }
    region_file = tmp_path / "region.py"
    region_file.write_text(_region(_build_launcher_script(level)))
    runpy.run_path(str(region_file), init_globals=ns)
    return {
        os.path.basename(os.fsdecode(target)): Path(os.fsdecode(source))
        for source, target, _flags in libc.calls
        if os.fsdecode(target) in (str(key), str(netrc))
    }


@pytest.mark.parametrize("level", ["strict", "standard"])
def test_signing_key_mask_source_is_unreadable(tmp_path: Path, level: str) -> None:
    """The key's mask source carries no permission bits; other masks keep theirs."""
    sources = _mask_sources(tmp_path, level)

    assert stat.S_IMODE(sources["token_signing.key"].stat().st_mode) == 0
    # The control: an ordinary hidden file still reads as empty, unchanged.
    assert stat.S_IMODE(sources[".netrc"].stat().st_mode) == 0o600
    assert sources[".netrc"].read_bytes() == b""


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root reads a mode-0 file, so the refusal cannot be observed",
)
def test_a_copy_through_the_signing_key_mask_fails(tmp_path: Path) -> None:
    """Reading the mask raises, so a copy cannot carry zero bytes out as the key."""
    source = _mask_sources(tmp_path, "strict")["token_signing.key"]

    with pytest.raises(PermissionError):
        source.read_bytes()


def test_unreadable_set_names_only_the_signing_key() -> None:
    """Widening the set changes what sandboxed readers see, so it is pinned here."""
    assert getattr(sandbox, "_CREW_UNREADABLE_MASK_LEAVES", None) == frozenset(
        {"token_signing.key"}
    )
