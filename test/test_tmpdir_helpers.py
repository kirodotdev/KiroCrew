"""The temp base fixtures use must be usable on every OS.

The Windows half is what regressed: `mkdtemp(dir="/tmp")` resolves against the
current drive and does not create its `dir`, so it raised FileNotFoundError
unless something unrelated had already made that directory. These tests pin the
branch itself, which is verifiable from any host, rather than the shard outcome.
"""

import shutil
import tempfile
from pathlib import Path

from tmpdir_helpers import short_tmp_base

from kiro_crew import platform_compat


class TestShortTmpBase:
    def test_posix_keeps_the_low_entropy_tmp(self, monkeypatch):
        """`/tmp` is what satisfies the redaction and sun_path constraints."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        assert short_tmp_base() == "/tmp"

    def test_windows_defers_to_the_platform_base(self, monkeypatch):
        """`None` makes mkdtemp use gettempdir(), which exists by construction --
        unlike `<drive>\\tmp`, which mkdtemp will not create."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        assert short_tmp_base() is None

    def test_the_resolved_base_is_usable_on_this_host(self):
        """The regression itself: whatever branch THIS platform takes must yield a
        directory `mkdtemp` can actually create.

        Deliberately not parametrized over both branches. Forcing the POSIX branch
        while running on Windows asks for `/tmp` -- precisely the path that does
        not exist there -- so a both-branches loop would reproduce the very
        runner-state dependency the rest of this change removes.
        """
        base = Path(tempfile.mkdtemp(dir=short_tmp_base()))
        try:
            assert base.is_dir()
            probe = base / "probe.txt"
            probe.write_text("ok", encoding="utf-8")
            assert probe.read_text(encoding="utf-8") == "ok"
        finally:
            shutil.rmtree(base, ignore_errors=True)
