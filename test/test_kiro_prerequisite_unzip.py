"""Kiro CLI prerequisite tests: naming the ``unzip`` the Linux installer needs.

The Linux one-line installer refuses a host without ``unzip`` before it downloads
anything, and a stock Ubuntu image has none. The not-found probe reports that, so
the setup screen can name the prerequisite beside the install command.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from kiro_prerequisite_helpers import _agents_dir_never_the_real_home  # noqa: F401
from kiro_prerequisite_helpers import _isolate_agent_specs  # noqa: F401
from kiro_prerequisite_helpers import _make_executable, _no_audit

from kiro_crew import kiro_prerequisite_candidate, platform_compat
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService, ProcessResult


async def _version_only(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
    return ProcessResult(ok=args == ["--version"])


@pytest.fixture(autouse=True)
def _system_bin_dirs(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Path:
    # The real /usr/bin of the machine running the tests must not decide them.
    system_bin = tmp_path / "usr" / "bin"
    system_bin.mkdir(parents=True)
    _floor_monkeypatch.setattr(kiro_prerequisite_candidate, "_SYSTEM_BIN_DIRS", (str(system_bin),))
    return system_bin


def _service(tmp_path: Path, platform_name: str, path_dir: Path) -> KiroPrerequisiteService:
    path_dir.mkdir(parents=True, exist_ok=True)
    return KiroPrerequisiteService(
        platform_name=platform_name,
        environ={"HOME": str(tmp_path), "PATH": str(path_dir)},
        home=tmp_path,
        process_runner=_version_only,
        audit_writer=_no_audit,
    )


class TestInstallerUnzipPrerequisite:
    @pytest.mark.asyncio
    async def test_linux_host_without_unzip_reports_it_missing(self, tmp_path: Path) -> None:
        service = _service(tmp_path, "linux", tmp_path / "bin")

        status = await service.snapshot(force=True)

        assert status["installed"] is False
        assert status["unzip_missing"] is True

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="Windows cannot represent POSIX execute-bit semantics",
    )
    @pytest.mark.asyncio
    async def test_linux_host_with_unzip_on_path_reports_nothing(self, tmp_path: Path) -> None:
        _make_executable(tmp_path / "bin" / "unzip")
        service = _service(tmp_path, "linux", tmp_path / "bin")

        status = await service.snapshot(force=True)

        assert status["installed"] is False
        assert status["unzip_missing"] is False

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="Windows cannot represent POSIX execute-bit semantics",
    )
    @pytest.mark.asyncio
    async def test_unzip_off_a_narrow_gateway_path_is_still_found(
        self, tmp_path: Path, _system_bin_dirs: Path
    ) -> None:
        # A gateway started by systemd or a desktop launcher can inherit a PATH
        # without the system bin directories the user's shell has. unzip there is
        # still on the PATH the installer runs with, so it is not missing.
        _make_executable(_system_bin_dirs / "unzip")
        service = _service(tmp_path, "linux", tmp_path / "bin")

        status = await service.snapshot(force=True)

        assert status["unzip_missing"] is False

    @pytest.mark.asyncio
    async def test_macos_host_is_never_asked_for_unzip(self, tmp_path: Path) -> None:
        # The installer checks for unzip on Linux only.
        service = _service(tmp_path, "darwin", tmp_path / "bin")

        status = await service.snapshot(force=True)

        assert status["installed"] is False
        assert status["unzip_missing"] is False

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="Windows cannot represent POSIX execute-bit semantics",
    )
    @pytest.mark.asyncio
    async def test_installed_cli_is_not_asked_for_unzip(self, tmp_path: Path) -> None:
        # Nothing left to install, so the installer's prerequisites do not apply.
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")
        service = _service(tmp_path, "linux", tmp_path / "bin")

        status = await service.snapshot(force=True)

        assert status["installed"] is True
        assert status["unzip_missing"] is False
