"""A failed staging setup must not leave a snapshot-shaped file behind.

The download claims its final name up front, atomically, so two concurrent restores cannot
pick the same one. That placeholder is a zero-byte file named exactly like a snapshot, in
the directory `--keep` prunes -- so leaking one does not merely litter: it is the NEWEST
entry by mtime, survives the prune, and a real bundle is deleted in its place.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from kiro_crew import snapshot_remote as remote

URL = "s3://bucket/prefix/kirocrew-snapshot-20260101T000000Z.tar.gz"
GLOB = "kirocrew-snapshot-*.tar.gz"


class TestAFailedStagingSetupLeavesNoPlaceholder:
    @pytest.fixture(autouse=True)
    def _aws_present(self, monkeypatch):
        monkeypatch.setattr(remote.shutil, "which", lambda _name: "/usr/bin/aws")

    def test_the_claimed_name_is_released_when_mkdtemp_fails(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        into = tmp_path / "snapshots"

        def _boom(*_a, **_k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(tempfile, "mkdtemp", _boom)

        with pytest.raises(OSError):
            remote.download(URL, into, "default")

        assert list(into.glob(GLOB)) == [], "a snapshot-shaped placeholder was left behind"

    def test_the_original_failure_is_what_surfaces(self, tmp_path: Path, monkeypatch) -> None:
        """Not an UnboundLocalError from cleaning up a directory that was never made."""
        into = tmp_path / "snapshots"

        def _boom(*_a, **_k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(tempfile, "mkdtemp", _boom)

        with pytest.raises(OSError) as e:
            remote.download(URL, into, "default")

        assert "No space left on device" in str(e.value), str(e.value)

    def test_a_prune_after_the_failure_still_sees_only_real_bundles(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The consequence, stated directly: the leak would have displaced this bundle."""
        into = tmp_path / "snapshots"
        into.mkdir()
        real = into / "kirocrew-snapshot-20250101T000000Z.tar.gz"
        real.write_bytes(b"a real bundle")

        def _boom(*_a, **_k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(tempfile, "mkdtemp", _boom)

        with pytest.raises(OSError):
            remote.download(URL, into, "default")

        assert [p.name for p in into.glob(GLOB)] == [real.name]
