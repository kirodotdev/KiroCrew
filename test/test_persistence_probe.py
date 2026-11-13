"""Tests for the gateway persistence preflight.

``probe_file_persistence`` guards against an environment where basic file
creation or advisory locking fails (observed: a gateway spawned from inside a
sandboxed agent session inherits a seccomp filter, so ``mkstemp``/``flock``
raise ``OSError(ENOSYS)`` while writes to already-open fds keep working). The
gateway must refuse to boot in that state instead of limping along silently
losing every save.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.platform_compat import probe_file_persistence


class TestProbeFilePersistence:
    def test_healthy_directory_passes(self, tmp_path: Path) -> None:
        assert probe_file_persistence(tmp_path) is None

    def test_healthy_directory_leaves_no_probe_file(self, tmp_path: Path) -> None:
        probe_file_persistence(tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_creates_missing_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "not" / "yet" / "there"
        assert probe_file_persistence(target) is None
        assert target.is_dir()

    def test_mkstemp_enosys_reports_create_step_and_sandbox_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _enosys(*args: object, **kwargs: object) -> tuple[int, str]:
            raise OSError(errno.ENOSYS, "Function not implemented")

        monkeypatch.setattr(platform_compat.tempfile, "mkstemp", _enosys)
        msg = probe_file_persistence(tmp_path)
        assert msg is not None
        assert "cannot create files in" in msg
        assert str(tmp_path) in msg
        assert "seccomp" in msg

    def test_lock_enosys_reports_lock_step_and_sandbox_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import contextlib

        @contextlib.contextmanager
        def _enosys_lock(fd: int, **kwargs: object):  # type: ignore[no-untyped-def]
            raise OSError(errno.ENOSYS, "Function not implemented")
            yield

        monkeypatch.setattr(platform_compat, "file_lock", _enosys_lock)
        msg = probe_file_persistence(tmp_path)
        assert msg is not None
        assert "cannot lock files in" in msg
        assert "seccomp" in msg

    def test_lock_failure_still_removes_probe_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import contextlib

        @contextlib.contextmanager
        def _enosys_lock(fd: int, **kwargs: object):  # type: ignore[no-untyped-def]
            raise OSError(errno.ENOSYS, "Function not implemented")
            yield

        monkeypatch.setattr(platform_compat, "file_lock", _enosys_lock)
        probe_file_persistence(tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_non_enosys_error_gets_no_sandbox_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _eacces(*args: object, **kwargs: object) -> tuple[int, str]:
            raise OSError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(platform_compat.tempfile, "mkstemp", _eacces)
        msg = probe_file_persistence(tmp_path)
        assert msg is not None
        assert "cannot create files in" in msg
        assert "seccomp" not in msg

    def test_unlink_failure_reports_remove_step(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Create-allowed/delete-denied environments break atomic renames, so
        a failed probe-file removal must be a preflight FAILURE, not success."""
        real_unlink = os.unlink

        def _deny_probe_unlink(path: str | os.PathLike[str], **kwargs: object) -> None:
            if ".persistence-probe-" in str(path):
                raise OSError(errno.EACCES, "Operation not permitted")
            real_unlink(path, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(platform_compat.os, "unlink", _deny_probe_unlink)
        msg = probe_file_persistence(tmp_path)
        assert msg is not None
        assert "cannot remove files from" in msg

    def test_write_failure_reports_write_step(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A byte quota (EDQUOT/ENOSPC) that allows creating empty files but
        not writing to them must fail the probe."""

        def _no_space(fd: int, data: bytes, **kwargs: object) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(platform_compat.os, "write", _no_space)
        msg = probe_file_persistence(tmp_path)
        assert msg is not None
        assert "cannot write files in" in msg

    def test_fsync_failure_reports_flush_step(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Chat history commits with atomic_write(fsync=True): a buffered
        write can succeed while the flush fails (EIO), so fsync is part of
        the probed contract."""

        def _eio(fd: int, **kwargs: object) -> None:
            raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr(platform_compat.os, "fsync", _eio)
        msg = probe_file_persistence(tmp_path)
        assert msg is not None
        assert "cannot flush files in" in msg

    def test_replace_failure_reports_replace_step(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``replace_with_retry`` is what atomic_write commits with; if it is
        broken, the probe must fail even though create/write/lock succeeded."""

        def _no_replace(
            src: str | os.PathLike[str], dst: str | os.PathLike[str], **kwargs: object
        ) -> None:
            raise OSError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr("kiro_crew.atomic_write.replace_with_retry", _no_replace)
        msg = probe_file_persistence(tmp_path)
        assert msg is not None
        assert "cannot atomically replace files in" in msg

    def test_replace_failure_still_cleans_up_probe_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_replace(
            src: str | os.PathLike[str], dst: str | os.PathLike[str], **kwargs: object
        ) -> None:
            raise OSError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr("kiro_crew.atomic_write.replace_with_retry", _no_replace)
        probe_file_persistence(tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_probe_uses_the_production_replace_primitive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The probe must go through atomic_write.replace_with_retry (which
        absorbs the transient Windows sharing-violation window), not raw
        os.replace — otherwise a healthy Windows data home can fail preflight
        over an AV/indexer touch."""
        calls: list[tuple[str, str]] = []
        from kiro_crew import atomic_write

        real = atomic_write.replace_with_retry

        def _spy(
            src: str | os.PathLike[str], dst: str | os.PathLike[str], **kwargs: object
        ) -> None:
            calls.append((str(src), str(dst)))
            real(src, dst, **kwargs)

        monkeypatch.setattr("kiro_crew.atomic_write.replace_with_retry", _spy)
        assert probe_file_persistence(tmp_path) is None
        assert len(calls) == 1
        assert ".persistence-probe-" in calls[0][0]

    def test_uncreatable_directory_reports_create_step(self, tmp_path: Path) -> None:
        if os.name != "posix" or os.geteuid() == 0:
            pytest.skip("needs a POSIX non-root user for chmod-based denial")
        parent = tmp_path / "sealed"
        parent.mkdir()
        parent.chmod(0o500)
        try:
            msg = probe_file_persistence(parent / "child")
            assert msg is not None
            assert "cannot create files in" in msg
        finally:
            parent.chmod(0o700)


class TestGatewayCommandPreflight:
    """``kirocrew gateway`` must refuse an unwritable data home with one line."""

    @pytest.mark.parametrize("missing", [False, True], ids=["read-only home", "missing home"])
    def test_unwritable_home_exits_before_the_first_write(
        self,
        missing: bool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Project-dir detection and the live-target read both resolve the home
        first; on an unwritable one neither may raise before the one-line refusal."""
        if os.name != "posix" or os.geteuid() == 0:
            pytest.skip("needs a POSIX non-root user for chmod-based denial")
        from kiro_crew import cli

        sealed = tmp_path / "sealed"
        sealed.mkdir()
        home = (sealed / "home" if missing else sealed).resolve()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        # A service starts outside a checkout, so project-dir detection reads the
        # saved path from the home: pin a marker-free cwd, never assume tmp_path's.
        monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: cls("/kc-no-markers/cwd")))
        monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "gateway"])
        monkeypatch.setattr(
            cli.platform_compat, "limit_malloc_arenas", cli.platform_compat.MallocArenaCap
        )
        monkeypatch.setattr(cli, "maybe_reexec", lambda argv: pytest.fail("reached maybe_reexec"))
        sealed.chmod(0o500)
        try:
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        finally:
            sealed.chmod(0o700)
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert f"❌ Cannot persist state: cannot create files in {home}: " in err
        assert "Traceback" not in err
