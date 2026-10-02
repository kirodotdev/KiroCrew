"""Tests for the pure path-primitives leaf ``kiro_crew.config.paths``.

These pin two properties of the config-loader decoupling refactor:

1. The path primitives behave identically to their historical
   ``kiro_crew.config.loader`` definitions (back-compat).
2. ``kiro_crew.config.paths`` is a genuine leaf — importing it pulls in **no**
   ``kiro_crew`` modules (in particular not the heavy ``config.loader``), so the
   modules that only need ``config_dir()`` don't transitively load the DTOs,
   schema validation, the process-global cache, and the provider factory.
"""

from __future__ import annotations

import errno
import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import make_dir_link
from kiro_crew.config import paths


class TestConfigDir:
    """``config_dir()`` resolves ~/.kiro/crew, honoring KIROCREW_HOME."""

    @pytest.fixture(autouse=True)
    def _reset_resolved_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # config_dir() caches the resolved data home in a module global for the
        # process lifetime; reset it so each test resolves fresh against its own
        # patched Path.home / KIROCREW_HOME rather than a value another test cached.
        monkeypatch.setattr(paths, "_resolved_home", None)

    def test_default_is_home_dotkiro_crew(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        result = paths.config_dir()
        assert result == tmp_path / ".kiro" / "crew"
        assert result.is_dir()  # created on access

    def test_kirocrew_home_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        home = tmp_path / "custom-home"
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        result = paths.config_dir()
        assert result == home.resolve()
        assert result.is_dir()

    def test_kirocrew_home_system_dir_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A system directory must be refused and fall back to ~/.kiro/crew.
        # The refused location is platform-shaped: ``/usr`` is a POSIX system
        # tree, but on Windows ``Path("/usr").resolve()`` is ``C:\usr`` -- an
        # ordinary, non-existent directory the override ACCEPTED, so this test
        # both failed there and CREATED ``C:\usr`` on the developer's system
        # drive on every run. The drive root is the location ``_is_unsafe_home``
        # refuses on Windows (``p == p.parent``); it exists and is never touched.
        if sys.platform == "win32":
            system_dir = Path.cwd().anchor  # e.g. ``C:\``
        else:
            system_dir = "/usr"
        monkeypatch.setenv("KIROCREW_HOME", system_dir)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        result = paths.config_dir()
        assert result == tmp_path / ".kiro" / "crew"


class TestLedgerRoot:
    def test_link_is_refused_without_touching_its_target(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        target = tmp_path / "outside"
        target.mkdir()
        make_dir_link(home / "crew-log", target)
        restricted: list[Path] = []

        with caplog.at_level(logging.WARNING, logger=paths.__name__):
            paths._ensure_crew_log_root(home, restricted.append)

        assert restricted == [], "the owner-only callback would chmod the link target"
        assert list(target.iterdir()) == [], "the linked target was modified"
        assert any("Refusing crew log root" in record.message for record in caplog.records)


def _eperm(path: Path) -> PermissionError:
    return PermissionError(errno.EPERM, "Operation not permitted", str(path))


def _raising(exc: BaseException) -> Callable[[Path], None]:
    def restrict(_directory: Path) -> None:
        raise exc

    return restrict


class TestCannotRestrictWarnings:
    """A tightening the OS refuses is reported as a warning, with a traceback only
    when the error is one nobody expected. ``EPERM`` is expected on macOS, where a
    kernel-protected provenance attribute denies ``chmod`` and ``stat`` on the data
    home even to its owner, so a healthy startup must not print a traceback for it.
    On Linux the same errno is a chown-fixable ownership problem, so it keeps one.
    """

    @staticmethod
    def _cannot_restrict_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.message.startswith("Cannot restrict")]

    def test_log_root_eperm_on_macos_is_a_single_line_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        home = tmp_path / "home"
        home.mkdir()
        root = home / "crew-log"

        with caplog.at_level(logging.WARNING, logger=paths.__name__):
            paths._ensure_crew_log_root(home, _raising(_eperm(root)))

        (record,) = self._cannot_restrict_records(caplog)
        assert record.levelno == logging.WARNING
        assert record.exc_info is None, "an expected EPERM must not carry a traceback"
        assert "Traceback" not in caplog.text
        assert str(root) in record.message, "the warning names the path it could not tighten"
        assert "Operation not permitted" in record.message
        assert "provenance" in record.message and "chown" in record.message
        assert "continues" in record.message

    @pytest.mark.parametrize(
        ("platform", "exc"),
        [
            pytest.param(
                "darwin", OSError(errno.EROFS, "Read-only file system"), id="other-oserror"
            ),
            pytest.param("darwin", RuntimeError("no resolver"), id="runtime-error"),
            pytest.param("linux", _eperm(Path("crew-log")), id="eperm-on-linux"),
        ],
    )
    def test_log_root_other_errors_keep_the_traceback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        platform: str,
        exc: BaseException,
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        home = tmp_path / "home"
        home.mkdir()

        with caplog.at_level(logging.WARNING, logger=paths.__name__):
            paths._ensure_crew_log_root(home, _raising(exc))

        (record,) = self._cannot_restrict_records(caplog)
        assert record.exc_info is not None, "an unexpected error keeps its traceback"
        assert record.exc_info[1] is exc
        assert "Traceback" in caplog.text
        assert "provenance" not in record.message

    @staticmethod
    def _refuse_only_the_home(
        monkeypatch: pytest.MonkeyPatch, home: Path, exc: BaseException
    ) -> list[Path]:
        from kiro_crew import platform_compat

        real = platform_compat.restrict_dir_to_owner
        refused: list[Path] = []

        def restrict(directory: Path) -> None:
            if directory == home:
                refused.append(directory)
                raise exc
            real(directory)

        monkeypatch.setattr(platform_compat, "restrict_dir_to_owner", restrict)
        monkeypatch.setattr(paths, "config_dir", lambda: home)
        return refused

    def test_data_home_eperm_on_macos_is_a_single_line_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        home = tmp_path / "home"
        home.mkdir()
        refused = self._refuse_only_the_home(monkeypatch, home, _eperm(home))

        with caplog.at_level(logging.WARNING, logger=paths.__name__):
            assert paths.ensure_data_home() == home

        assert refused == [home], "the test did not exercise the failing branch"
        (record,) = self._cannot_restrict_records(caplog)
        assert record.exc_info is None, "an expected EPERM must not carry a traceback"
        assert "Traceback" not in caplog.text
        assert str(home) in record.message, "the warning names the path it could not tighten"
        assert "provenance" in record.message and "chown" in record.message
        assert "continues" in record.message
        assert (home / "crew-log").is_dir(), "the crew log root is still established"

    @pytest.mark.parametrize(
        ("platform", "exc"),
        [
            pytest.param(
                "darwin", OSError(errno.EROFS, "Read-only file system"), id="other-oserror"
            ),
            pytest.param("linux", _eperm(Path("home")), id="eperm-on-linux"),
        ],
    )
    def test_data_home_other_errors_keep_the_traceback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        platform: str,
        exc: BaseException,
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        home = tmp_path / "home"
        home.mkdir()
        refused = self._refuse_only_the_home(monkeypatch, home, exc)

        with caplog.at_level(logging.WARNING, logger=paths.__name__):
            assert paths.ensure_data_home() == home

        assert refused == [home], "the test did not exercise the failing branch"
        (record,) = self._cannot_restrict_records(caplog)
        assert record.exc_info is not None, "an unexpected error keeps its traceback"
        assert record.exc_info[1] is exc
        assert "Traceback" in caplog.text


class TestConfigPackageDir:
    """``config_package_dir()`` points at the installed ``kiro_crew/config/``."""

    def test_points_at_config_package_with_defaults_json(self) -> None:
        pkg = paths.config_package_dir()
        assert pkg.name == "config"
        # The bundled agent defaults ship in this directory.
        assert (pkg / "defaults.json").is_file()

    def test_is_paths_module_parent(self) -> None:
        assert paths.config_package_dir() == Path(paths.__file__).resolve().parent


class TestDefaultWorkspaceBase:
    def test_linux_uses_home_workplace(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert paths._default_workspace_base() == tmp_path / "workplace"

    def test_macos_prefers_volumes_then_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        # Hermetic: simulate /Volumes/workplace being ABSENT regardless of the
        # host. On a real macOS dev box /Volumes/workplace often exists, which
        # would otherwise make this assert the wrong branch. Patch is_dir to
        # report False only for that path; everything else behaves normally.
        _real_is_dir = Path.is_dir
        monkeypatch.setattr(
            Path,
            "is_dir",
            lambda self: False if str(self) == "/Volumes/workplace" else _real_is_dir(self),
        )
        assert paths._default_workspace_base() == tmp_path / "workplace"

    def test_macos_uses_volumes_when_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        # Hermetic: simulate /Volumes/workplace being PRESENT regardless of host.
        _real_is_dir = Path.is_dir
        monkeypatch.setattr(
            Path,
            "is_dir",
            lambda self: True if str(self) == "/Volumes/workplace" else _real_is_dir(self),
        )
        assert paths._default_workspace_base() == Path("/Volumes/workplace")


class TestSafeDirName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("a/b", "a_b"),
            ("a\\b", "a_b"),
            ("a:b", "a_b"),
            ("a b", "a_b"),
            ("plain", "plain"),
            ("x/y:z w", "x_y_z_w"),
        ],
    )
    def test_sanitizes_separators(self, raw: str, expected: str) -> None:
        assert paths._safe_dir_name(raw) == expected


class TestLeafPurity:
    """The whole point of the extraction: importing the leaf is cheap.

    Importing ``kiro_crew.config.paths`` in a fresh interpreter must NOT import
    ``kiro_crew.config.loader`` (or any other ``kiro_crew`` submodule). Run in a
    subprocess so the already-warm modules in this test process don't mask a
    regression.
    """

    def test_importing_paths_pulls_no_kiro_crew_modules(self) -> None:
        code = (
            "import sys\n"
            "import kiro_crew.config.paths\n"
            "leaked = sorted(\n"
            "    m for m in sys.modules\n"
            "    if m.startswith('kiro_crew')\n"
            "    and m not in {'kiro_crew', 'kiro_crew.config', 'kiro_crew.config.paths'}\n"
            ")\n"
            "print(','.join(leaked))\n"
        )
        import os

        # Ensure kiro_crew is importable in the subprocess on local dev runs
        # where PYTHONPATH may not already include the src/ directory.
        src_dir = str(Path(__file__).resolve().parents[1] / "src")
        env = dict(os.environ)
        env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        leaked = [m for m in out.stdout.strip().split(",") if m]
        assert leaked == [], f"config.paths leaf leaked kiro_crew modules: {leaked}"


class TestBackCompatReexport:
    """All primitives remain importable from ``kiro_crew.config.loader``."""

    def test_loader_reexports_match_paths(self) -> None:
        from kiro_crew.config import loader

        for name in (
            "config_dir",
            "config_package_dir",
            "_default_workspace_base",
            "_safe_dir_name",
            "CONFIG_DIR_NAME",
            "OUTBOX_DIR_NAME",
            "_WORKSPACE_DIR_NAME",
        ):
            assert getattr(loader, name) is getattr(paths, name), name

    def test_config_package_lazy_surface(self) -> None:
        # `from kiro_crew.config import X` still resolves the public surface
        # without eagerly importing the loader at package import time.
        import kiro_crew.config as cfg

        assert cfg.config_dir is paths.config_dir
        assert cfg.KiroCrewConfig.__name__ == "KiroCrewConfig"


class TestDefaultWorkDir:
    """``default_work_dir()`` is the ONE spelling of where a cwd-less session runs.

    The ACP runtime and client root a cwd-less process there, and the session
    manager's ``default_conversation_roots`` must recognise a conversation
    rooted there as "at the gateway default" -- a cleared-project cron job is
    judged against it every wake. Two spellings that drift would end that
    job's conversation on every wake.
    """

    def test_is_the_workspace_under_the_data_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        monkeypatch.setattr(paths, "_resolved_home", None)
        monkeypatch.setattr(paths, "_config_dir_memo", None)
        assert paths.default_work_dir() == paths.config_dir() / "workspace"

    def test_a_caller_may_hand_in_the_home_it_already_resolved(self, tmp_path: Path) -> None:
        # A CLI verb resolves config_dir() through its own module seam (which
        # its tests point at a fixture home); the spelling beneath stays here.
        assert paths.default_work_dir(tmp_path) == tmp_path / "workspace"

    def test_no_module_spells_the_default_work_dir_itself(self) -> None:
        # Tree-wide: the only ``config_dir() / "workspace"`` in the package is
        # the helper's own body, so a change to it moves every consumer (the
        # ACP runtime and client, the session manager's default roots, the
        # deploy allowed-roots, member essential context, the knowledge db
        # paths) together instead of leaving one pointing at the old place.
        import re

        pkg = Path(paths.__file__).resolve().parents[1]
        literal = re.compile(r'config_dir\(\)\s*/\s*"workspace"')
        offenders: list[str] = []
        for py in pkg.rglob("*.py"):
            if py.name == "paths.py" and py.parent.name == "config":
                continue
            for line in py.read_text(encoding="utf-8").splitlines():
                stripped = line.lstrip()
                # A comment or a backticked docstring mention describes the
                # directory; only CODE that computes it is a second spelling.
                if stripped.startswith("#") or "``" in line:
                    continue
                if literal.search(line):
                    offenders.append(f"{py.relative_to(pkg)}: {line.strip()}")
        assert offenders == [], offenders
