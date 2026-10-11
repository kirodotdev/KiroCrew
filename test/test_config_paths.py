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
import os
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

    def test_the_home_is_created_with_no_mode_argument_on_windows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A ``0o700`` mkdir is a protected DACL on Windows; POSIX gets owner-only."""
        seen: list[int] = []
        real_mkdir = Path.mkdir

        def recording_mkdir(self: Path, mode: int = 0o777, **kwargs: object) -> None:
            seen.append(mode)
            real_mkdir(self, mode, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "mkdir", recording_mkdir)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "fresh-home"))
        paths.config_dir()

        assert seen == [0o777 if sys.platform == "win32" else 0o700]

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


class TestScratchRootOverride:
    """``valid_scratch_root_override()`` validates ``KIROCREW_SCRATCH_ROOT`` with
    the same ``_is_unsafe_home`` predicate as ``KIROCREW_HOME``."""

    @pytest.fixture(autouse=True)
    def _home_off_the_real_tree(self, _floor_monkeypatch, tmp_path: Path) -> None:
        # pytest's tmp_path resolves UNDER the real ``~/.kiro/crew``, so a tmp_path-based
        # "off-drive" override would land inside the real default home, which the validator
        # now refuses (an override inside ANY crew-home spelling is masked and refused). Pin
        # HOME to an isolated dir so ``_default_home()``/``_legacy_home()`` resolve under it
        # and a tmp_path relocation is genuinely off every crew home, as the real case is.
        # Uses _floor_monkeypatch (undone independently, D11), not the shared monkeypatch,
        # so a test calling monkeypatch.undo() cannot lift this autouse isolation.
        realhome = tmp_path / "realhome"
        _floor_monkeypatch.setenv("HOME", str(realhome))
        # Patch Path.home() too, not only HOME: on Windows Path.home() ignores HOME, so a
        # Windows runner launched from the real crew home would still resolve the default
        # home there and refuse the temp override as in-home.
        _floor_monkeypatch.setattr(Path, "home", classmethod(lambda cls: realhome))
        # The validator reads an EXISTING override root's owner (Windows only) and refuses a
        # foreign-owned one. The Windows ACL layer is unavailable on the test runner, so stub
        # the owner read to "current user owns it" by default; the owner-refusal path is
        # exercised explicitly in test_agent_scratch. ``_floor_monkeypatch`` keeps it from
        # being lifted by a test's own ``monkeypatch.undo()``.
        from kiro_crew import platform_compat as _pc
        from kiro_crew import windows_acl as _wacl

        _floor_monkeypatch.setattr(_pc, "current_user_sid", lambda: "S-1-5-21-TEST-ME")

        class _Owned:
            owner_sid = "S-1-5-21-TEST-ME"

        _floor_monkeypatch.setattr(_wacl, "describe", lambda p: _Owned())

    def test_unset_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KIROCREW_SCRATCH_ROOT", raising=False)
        assert paths.valid_scratch_root_override() is None

    def test_valid_override_is_resolved(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The override applies on Windows only, so this "is honoured" assertion pins
        # ``win32``; every other platform refuses it (see
        # ``test_override_is_refused_off_windows``).
        monkeypatch.setattr(sys, "platform", "win32")
        paths._scratch_root_override_pin = paths._UNSET
        target = tmp_path / "scratch-elsewhere"
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(target))
        assert paths.valid_scratch_root_override() == target.resolve()

    @pytest.mark.parametrize("platform", ["linux", "darwin"])
    def test_override_is_refused_off_windows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str
    ) -> None:
        # The relocated scratch root is masked by a pathname-anchored rule that protects
        # the tree it is bound over, not a later allocation created after the root's own
        # ancestor is renamed and recreated. On a non-Windows platform, under an
        # operator-writable parent, an agent could rename an ancestor of the relocated root
        # and recreate it, so a later session's scratch is built at the original pathname
        # while the existing mask stays beneath the renamed-aside tree -- leaving those
        # later trees reachable from another session. The override is therefore honoured on
        # Windows only; on every other platform it is refused with reason "unsupported
        # platform" and ``scratch_root()`` falls back to the guarded default. A valid
        # off-home target that WOULD be accepted on Windows is used, so only the platform
        # decides the refusal.
        target = tmp_path / "data-drive" / "scratch"
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(target))
        monkeypatch.setattr(sys, "platform", platform)
        paths._scratch_root_override_pin = paths._UNSET
        assert paths.valid_scratch_root_override() is None
        assert paths.scratch_root_override_refusal_reason() == "unsupported platform"
        import kiro_crew.agent_scratch as agent_scratch

        assert agent_scratch.scratch_root() == paths.config_dir() / "scratch"

    def test_valid_override_is_honoured_on_windows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Windows honours a valid off-home override: its scratch is a per-session private
        # window the launcher opens directly, not a pathname mask over an operator-writable
        # tree, so the ancestor-replacement exposure that refuses the override elsewhere
        # does not apply.
        target = tmp_path / "data-drive" / "scratch"
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(target))
        paths._scratch_root_override_pin = paths._UNSET
        assert paths.valid_scratch_root_override() == target.resolve()

    def test_system_dir_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Same refusal shape as ``KIROCREW_HOME``: a drive/filesystem root is
        # refused on every OS (``p == p.parent``) without being created.
        if sys.platform == "win32":
            system_dir = Path.cwd().anchor
        else:
            system_dir = "/usr"
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", system_dir)
        assert paths.valid_scratch_root_override() is None

    def test_ancestor_of_config_dir_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Masking a path that equals or contains the data home would hide the policy
        # ceiling and the operator's files below it, so it is refused.
        home = tmp_path / "home" / ".kiro" / "crew"
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        # config_dir() itself.
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(paths.config_dir()))
        assert paths.valid_scratch_root_override() is None
        # An ancestor of config_dir().
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(tmp_path / "home"))
        assert paths.valid_scratch_root_override() is None

    def test_sibling_of_config_dir_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        paths._scratch_root_override_pin = paths._UNSET
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home" / ".kiro" / "crew"))
        sibling = tmp_path / "data-drive" / "scratch"
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(sibling))
        assert paths.valid_scratch_root_override() == sibling.resolve()

    def test_path_inside_data_home_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A scratch root INSIDE the data home would be masked as a hidden tree,
        # hiding whatever governance tree sits there -- e.g. ``<data home>/profiles``
        # drops the read-only profile seal, so a profile-bound policy silently falls
        # back to the permissive default. Any descendant of config_dir() is refused.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home" / ".kiro" / "crew"))
        home = paths.config_dir()
        for inside in (home / "profiles", home / "scratch" / "moved", home / "deep" / "nested"):
            monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(inside))
            paths._scratch_root_override_pin = paths._UNSET
            assert paths.valid_scratch_root_override() is None, inside

    def test_path_inside_legacy_home_is_refused_even_with_active_home_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # With a non-default KIROCREW_HOME active, an override inside the pre-move legacy
        # ``~/.kirocrew`` home is refused as a crew home. The override must be refused at ANY
        # crew-home spelling (active config_dir, default ~/.kiro/crew, legacy ~/.kirocrew),
        # not only the active one. Pin ``win32`` so the override passes the Windows-only
        # platform gate and the crew-home refusal is actually reached (on a non-Windows
        # runner the validator returns at the unsupported-platform guard first).
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "active" / ".kiro" / "crew"))
        legacy = paths._legacy_home()
        for inside in (legacy / "scratch" / "moved", legacy, paths._default_home() / "x"):
            monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(inside))
            paths._scratch_root_override_pin = paths._UNSET
            assert paths.valid_scratch_root_override() is None, inside

    def test_symlinked_override_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A symlink AT the override name could be repointed to another session's root
        # between the first resolve and a later allocation, so a symlinked override is
        # refused outright (fall back to the default), and scratch_root() stays pinned.
        # On Windows a directory SYMLINK needs a privilege the CI runner lacks, so the
        # alias is staged with ``make_dir_link`` -- a JUNCTION there, a symlink on POSIX.
        # Both are reparse points whose ``realpath`` differs from the lexical path, which
        # is exactly what the validator refuses, so the refusal/pinning asserts run on
        # every OS. If a runner cannot create the reparse point at all, only that creation
        # is skipped (with a reason) -- never the assertions.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home" / ".kiro" / "crew"))
        real_a = tmp_path / "scratch-a"
        real_a.mkdir()
        real_b = tmp_path / "scratch-b"
        real_b.mkdir()
        alias = tmp_path / "current"

        def _repoint(dest: Path) -> None:
            # A junction is a directory reparse point and must be removed with rmdir()
            # on Windows; a POSIX symlink is removed with unlink().
            if alias.exists() or alias.is_symlink():
                if sys.platform == "win32":
                    alias.rmdir()
                else:
                    alias.unlink()
            make_dir_link(alias, dest)

        try:
            make_dir_link(alias, real_a)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"cannot create a directory link on this runner: {exc}")
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(alias))
        paths._scratch_root_override_pin = paths._UNSET
        import kiro_crew.agent_scratch as agent_scratch

        before = agent_scratch.scratch_root()
        # The override is a reparse point, so it is refused and scratch_root() is the default.
        assert paths.valid_scratch_root_override() is None
        assert before == paths.config_dir() / "scratch"
        # Repoint the alias; the root must not change (refused + pinned, no re-resolve).
        _repoint(real_b)
        after = agent_scratch.scratch_root()
        assert after == before

    def test_cyclic_symlink_override_is_refused_without_raising(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A self-referential symlink override makes ``Path.resolve()`` raise ``RuntimeError``
        # on CPython 3.12. ``allocate_scratch``'s fallback catches only ``OSError`` /
        # ``ScratchBoundaryError``, so an uncaught resolution error would propagate out of
        # ``scratch_root()`` and abort agent startup. The override must instead be refused
        # fail-safe (fall back to the guarded default root) and ``scratch_root()`` must not
        # raise. On a runner that cannot create the loop, only the creation is skipped.
        import kiro_crew.agent_scratch as agent_scratch

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home" / ".kiro" / "crew"))
        loop = tmp_path / "loop"
        try:
            loop.symlink_to(loop)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"cannot create a self-referential symlink on this runner: {exc}")
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(loop))
        monkeypatch.setattr(paths, "_scratch_root_override_pin", paths._UNSET)
        # Refused fail-safe (not raised), and scratch_root() falls back to the default.
        assert paths.valid_scratch_root_override() is None
        assert agent_scratch.scratch_root() == paths.config_dir() / "scratch"

    def test_resolved_override_is_pinned_for_the_process(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A valid real-directory override is resolved ONCE and pinned: later calls read
        # the pin, so even repointing a symlink that sits BELOW the override name cannot
        # change the resolved root mid-process. A changed env string recomputes.
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home" / ".kiro" / "crew"))
        target = tmp_path / "data-drive" / "scratch"
        target.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(target))
        paths._scratch_root_override_pin = paths._UNSET
        first = paths.valid_scratch_root_override()
        assert first == target.resolve()
        # Same env string -> pinned value returned, not recomputed.
        assert paths.valid_scratch_root_override() is first
        # A genuinely CHANGED variable is honoured (different relocation).
        other = tmp_path / "other-drive" / "scratch"
        other.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(other))
        assert paths.valid_scratch_root_override() == other.resolve()

    def test_different_case_real_dir_is_accepted_when_realpath_renormalises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A plain REAL directory typed in a spelling that ``realpath`` renormalises
        # (Windows upper-cases the drive letter and expands short 8.3 names, and
        # ``ntpath.realpath`` returns the on-disk casing) is ACCEPTED: the link guard tests
        # the override NAME with ``is_link_or_junction``, and a real directory is not a
        # link whatever its casing. A lexical ``realpath`` vs ``abspath`` comparison would
        # instead refuse ``d:\scratch`` because it resolves to ``D:\scratch``, silently
        # keeping scratch on the system drive on the one platform the override exists for.
        #
        # Drive the user path: the directory genuinely exists at ``target`` and the operator
        # types exactly that path, while ``realpath`` is forced to return a renormalised
        # spelling (upper-cased leaf) of the SAME real directory, reproducing the Windows
        # on-disk-casing behaviour on any runner. The name-only link check accepts it.
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home" / ".kiro" / "crew"))
        target = tmp_path / "data-drive" / "scratch"
        target.mkdir(parents=True)
        typed = os.path.abspath(str(target))
        renormalised = os.path.join(os.path.dirname(typed), "SCRATCH")  # same dir, on-disk casing

        def _casing_realpath(path: str, *a: object, **k: object) -> str:
            if os.path.abspath(path) == typed:
                return renormalised
            return os.path.abspath(path)

        monkeypatch.setattr(os.path, "realpath", _casing_realpath)
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", typed)
        paths._scratch_root_override_pin = paths._UNSET
        resolved, reason = paths._resolve_scratch_root_override()
        # The real directory is accepted (resolved, no refusal reason), not dropped as "a link".
        assert resolved is not None, f"a real directory was refused as {reason!r}"
        assert reason is None

    @pytest.mark.parametrize(
        ("make_env", "platform", "expected_reason"),
        [
            ("crew_home", "win32", "a crew home"),
            ("system_dir", "win32", "a system directory"),
            ("off_home", "linux", "unsupported platform"),
        ],
    )
    def test_refusal_reason_is_logged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        make_env: str,
        platform: str,
        expected_reason: str,
    ) -> None:
        # The validator returns WHY it refused, and the override pin logs that reason once
        # per distinct value when it is computed, so an operator can tell a crew-home or
        # platform refusal apart from a real system-directory one, and ``scratch_root()``
        # still falls back to the default.
        import kiro_crew.agent_scratch as agent_scratch

        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home" / ".kiro" / "crew"))
        if make_env == "crew_home":
            value = str(paths.config_dir())  # the data home itself
        elif make_env == "system_dir":
            # A filesystem/drive root is refused on every platform (``p == p.parent``).
            # Chosen independently of the mocked ``sys.platform`` so the case gives the
            # same verdict on every runner.
            value = Path.cwd().anchor
        else:  # off_home, refused only by the platform rule (Windows-only override)
            off = tmp_path / "data-drive" / "scratch"
            off.mkdir(parents=True)
            value = str(off)
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", value)
        monkeypatch.setattr(paths, "_scratch_root_override_pin", paths._UNSET)

        # The warning fires once when the pin is computed (in config.paths).
        with caplog.at_level(logging.WARNING, logger=paths.__name__):
            assert paths.scratch_root_override_refusal_reason() == expected_reason
        assert agent_scratch.scratch_root() == paths.config_dir() / "scratch"  # fell back
        assert any(
            f"is {expected_reason}, ignoring" in record.message for record in caplog.records
        ), [r.message for r in caplog.records]


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
