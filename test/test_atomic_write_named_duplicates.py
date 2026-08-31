"""Regression tests for the three named atomic-write duplicates now folded into
the shared helper.

The sites are ``dev_fleet.gateway_service.atomic_write_text``,
``dashboard.handlers.themes._atomic_write_theme_json``, and BOTH writers in
``service.macos`` (``_write_plist_atomic`` and ``write_live_program``). Each had
its own hand-rolled temp-write-and-rename, so each independently missed the
Windows sharing-violation rename retry that ``atomic_write`` applies, and the two
macOS writers cleaned up their temp file under ``except Exception`` — which does
not catch a ``KeyboardInterrupt``, so a Ctrl-C at the rename left a scratch file
in the directory launchd scans.

Every assertion here is chosen to FAIL if its site is reverted to the
hand-rolled write, which is what stops the file from passing by accident:

* the routing assertions record calls through the module-level ``atomic_write``
  seam, so a reverted site never records one;
* the missing-parent assertions exercise the ``mkdir(parents=True)`` the shared
  helper owns — the hand-rolled ``Path.write_text`` and ``mkstemp(dir=...)``
  both raise on an absent parent;
* the Ctrl-C assertions raise ``KeyboardInterrupt`` from ``os.replace``, the one
  seam every form of this write reaches, so the reverted site is interrupted at
  the same point and its ``except Exception`` demonstrably fails to reclaim the
  temp file.

The mode assertions are the ones that caught a real defect while this migration
was being written, so they are load-bearing in BOTH directions. ``mkstemp``
creates its file owner-only, and neither ``_atomic_write_theme_json`` nor
``_write_plist_atomic`` chmod'd it afterwards, so both published at ``0o600``
— while ``atomic_write`` with no *mode* applies the umask default. Migrating
them without an explicit ``mode=0o600`` silently widened two previously
owner-only files to ``0o644``. ``write_live_program`` keeps its explicit
``0o700`` (drop it and launchd cannot exec the agent), and the Dev Fleet drop-in
keeps the umask default its ``Path.write_text`` always produced. No site ever
asked for ``fsync``, so none may acquire one.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.dev_fleet import gateway_service
from kiro_crew.dashboard.handlers import themes as themes_mod
from kiro_crew.service import macos as svc_macos

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")


def _umask_default_mode() -> int:
    """The mode ``open(path, "w")`` would have produced under this umask."""
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _mode_of(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _recorder(monkeypatch: pytest.MonkeyPatch, module: object) -> list[tuple[str, dict]]:
    """Record every ``atomic_write`` call *module* makes, still performing it."""
    calls: list[tuple[str, dict]] = []
    original = module.atomic_write  # type: ignore[attr-defined]

    def recording(path, content, **kwargs):
        calls.append((str(path), dict(kwargs)))
        original(path, content, **kwargs)

    monkeypatch.setattr(module, "atomic_write", recording)
    return calls


def _interrupt_the_rename_of(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """Raise ``KeyboardInterrupt`` when the write renames its temp onto *target*.

    ``os.replace`` is the seam EVERY form of this write reaches — the shared
    helper through ``replace_with_retry``, each hand-rolled site directly — so a
    reverted site is interrupted at exactly the same point rather than at a
    different one, which is what makes the cleanup assertion a real comparison.
    Interrupting there also means the content is already fully in the temp file,
    so what the assertion measures is purely whether the temp is reclaimed.

    Keyed on the destination path, so pytest's own renames and the ``tmp_path``
    teardown that runs after the exception are untouched.
    """
    real_replace = os.replace
    needle = str(target)

    def guarded(src, dst, **kwargs):  # noqa: ANN001 - mirrors os.replace
        if str(dst) == needle:
            raise KeyboardInterrupt("simulated Ctrl-C at the atomic rename")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", guarded)


class TestDevFleetDropInWrite:
    """``gateway_service.atomic_write_text`` stages and rolls back the systemd
    drop-in a Dev Fleet make-live rewrites under the operator."""

    def test_routes_through_the_shared_helper_with_no_fsync_or_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls = _recorder(monkeypatch, gateway_service)
        target = tmp_path / "make-live.conf"

        gateway_service.atomic_write_text(target, "[Service]\n")

        assert calls, "the drop-in write did not go through atomic_write"
        assert calls[0][0] == str(target)
        # This site wrote through ``Path.write_text``, so it had neither: an
        # fsync would add a disk barrier to a staging write, and an explicit
        # mode would stop honouring the operator's umask.
        assert calls[0][1] == {}
        assert target.read_text(encoding="utf-8") == "[Service]\n"

    def test_creates_a_missing_parent_directory(self, tmp_path: Path) -> None:
        """``rollback`` never mkdir'd, so the helper owning it is the fix.

        ``SystemdBackend.stage`` mkdirs the drop-in directory itself, but
        ``rollback`` calls straight into this writer — on a host where that
        directory went away between the two, the hand-rolled
        ``Path.write_text`` raised ``FileNotFoundError`` and the rollback
        reported failure to the dashboard.
        """
        target = tmp_path / "kirocrew.service.d" / "make-live.conf"
        assert not target.parent.exists()

        gateway_service.atomic_write_text(target, "prior\n")

        assert target.read_text(encoding="utf-8") == "prior\n"

    def test_writes_utf8_and_leaves_no_temp_sibling(self, tmp_path: Path) -> None:
        """Encoding is pinned rather than locale-derived, and nothing residual.

        systemd reads unit files as UTF-8. ``Path.write_text`` with no encoding
        follows the locale, so a non-UTF-8 locale wrote a drop-in with a
        non-ASCII ``WorkingDirectory`` that systemd could not parse.
        ``atomic_write`` encodes UTF-8 unconditionally.

        The line terminator is ``os.linesep``, not a hard-coded ``\n``: both this
        writer's old ``Path.write_text`` and ``atomic_write`` default to
        ``newline=None``, which translates. Pinning ``\n`` would assert a
        BEHAVIOUR CHANGE on Windows rather than the preservation this test is
        for — so the expectation is built from the same translation the replaced
        ``open()`` applied.
        """
        target = tmp_path / "make-live.conf"

        gateway_service.atomic_write_text(target, "WorkingDirectory=/home/tést\n")

        expected = f"WorkingDirectory=/home/tést{os.linesep}".encode("utf-8")
        assert target.read_bytes() == expected
        assert b"t\xc3\xa9st" in expected, "the fixture must exercise a non-ASCII byte"
        assert [p.name for p in tmp_path.iterdir()] == ["make-live.conf"]

    @POSIX_ONLY
    def test_keeps_the_umask_default_mode(self, tmp_path: Path) -> None:
        """Unlike the two mkstemp sites, this one published at the umask default
        (``Path.write_text`` goes through ``open``), so it must NOT gain a mode."""
        target = tmp_path / "make-live.conf"

        gateway_service.atomic_write_text(target, "[Service]\n")

        assert _mode_of(target) == _umask_default_mode()

    def test_a_ctrl_c_at_the_rename_leaves_no_temp_sibling(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A preservation assertion, not a gap being closed.

        This site is the one of the three that already reclaimed its temp file
        on a ``KeyboardInterrupt``, because it cleaned up in a ``finally`` rather
        than an ``except Exception``. It therefore passes on revert by design;
        it is here so the migration cannot LOSE that behaviour, which the two
        macOS writers' versions of this test do catch.
        """
        target = tmp_path / "make-live.conf"
        _interrupt_the_rename_of(monkeypatch, target)

        with pytest.raises(KeyboardInterrupt):
            gateway_service.atomic_write_text(target, "[Service]\n")

        assert not target.exists(), "no partial drop-in may be published"
        assert list(tmp_path.iterdir()) == [], "the temp sibling must be reclaimed"

    def test_rollback_still_restores_prior_content_through_the_helper(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The migration must not change what the rollback path publishes."""
        from unittest.mock import AsyncMock

        calls = _recorder(monkeypatch, gateway_service)
        dropin = tmp_path / "make-live.conf"
        dropin.write_text("staged\n", encoding="utf-8")
        backend = gateway_service.SystemdBackend(
            AsyncMock(return_value=(0, "", "")),
            lambda: "kirocrew.service",
            platform="linux",
            which=lambda _name: "/usr/bin/systemctl",
            dropin_path=lambda: dropin,
            dropin_content=lambda _wt, _kcbin: "",
        )

        assert backend.rollback("prior\n") is True

        assert dropin.read_text(encoding="utf-8") == "prior\n"
        assert calls and calls[0][0] == str(dropin)


class TestThemeJsonWrite:
    """``themes._atomic_write_theme_json`` publishes editor-authored theme packs
    the dashboard reads back immediately."""

    def test_routes_through_the_shared_helper_carrying_mode_0o600(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls = _recorder(monkeypatch, themes_mod)
        target = tmp_path / "sunset.json"

        themes_mod._atomic_write_theme_json(target, json.dumps({"slug": "sunset"}) + "\n")

        assert calls, "the theme write did not go through atomic_write"
        assert calls[0][0] == str(target)
        assert calls[0][1] == {"mode": 0o600}, (
            "mkstemp already published this file owner-only, so the mode must be "
            "passed explicitly or the helper's umask default widens it; and no "
            "fsync may be introduced"
        )
        assert json.loads(target.read_text(encoding="utf-8")) == {"slug": "sunset"}

    def test_creates_a_missing_parent_directory(self, tmp_path: Path) -> None:
        """The hand-rolled ``mkstemp(dir=target.parent)`` raised on an absent
        parent; the helper's ``mkdir(parents=True)`` now owns it."""
        target = tmp_path / "themes" / "sunset.json"
        assert not target.parent.exists()

        themes_mod._atomic_write_theme_json(target, json.dumps({"v": 1}) + "\n")

        assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1}

    def test_leaves_the_directory_holding_only_the_target(self, tmp_path: Path) -> None:
        """Stronger than globbing the old ``.<stem>-*.tmp`` shape.

        The temp name is now ``mkstemp``'s own, so a glob for the previous
        prefix would pass vacuously. Assert the directory's exact contents
        instead, so a leaked temp under ANY name fails.
        """
        target = tmp_path / "sunset.json"

        themes_mod._atomic_write_theme_json(target, json.dumps({"v": 1}) + "\n")
        themes_mod._atomic_write_theme_json(target, json.dumps({"v": 2}) + "\n")

        assert json.loads(target.read_text(encoding="utf-8")) == {"v": 2}
        assert [p.name for p in tmp_path.iterdir()] == ["sunset.json"]

    @POSIX_ONLY
    def test_publishes_the_theme_owner_only(self, tmp_path: Path) -> None:
        """``0o600``, matching what mkstemp-without-chmod always produced."""
        target = tmp_path / "sunset.json"

        themes_mod._atomic_write_theme_json(target, json.dumps({"v": 1}) + "\n")

        assert _mode_of(target) == 0o600

    def test_a_non_text_payload_still_raises_typeerror_and_publishes_nothing(
        self, tmp_path: Path
    ) -> None:
        """The failure contract the install route relies on is unchanged."""
        target = tmp_path / "sunset.json"

        with pytest.raises(TypeError):
            themes_mod._atomic_write_theme_json(target, 123)  # type: ignore[arg-type]

        assert not target.exists()
        assert list(tmp_path.iterdir()) == []


class TestLaunchdPlistWrite:
    """``macos._write_plist_atomic`` rewrites the LaunchAgent plist launchctl
    loads. Its own docstring promises a SIGINT mid-write leaves no partial
    file — under ``except Exception`` it still left the temp one."""

    @pytest.fixture(autouse=True)
    def _isolated_plist(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._dir = tmp_path / "LaunchAgents"
        self._dir.mkdir()
        self._path = self._dir / "dev.kirocrew.gateway.plist"
        monkeypatch.setattr(svc_macos, "PLIST_DIR", self._dir)
        monkeypatch.setattr(svc_macos, "PLIST_PATH", self._path)

    def test_routes_through_the_shared_helper_carrying_mode_0o600(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _recorder(monkeypatch, svc_macos)

        svc_macos._write_plist_atomic("<plist/>\n")

        assert calls, "the plist write did not go through atomic_write"
        assert calls[0][0] == str(self._path)
        assert calls[0][1] == {"mode": 0o600}, (
            "mkstemp already published the plist owner-only, so the mode must be "
            "passed explicitly or the helper's umask default widens it; and no "
            "fsync may be introduced"
        )
        assert self._path.read_text(encoding="utf-8") == "<plist/>\n"

    def test_a_ctrl_c_at_the_rename_leaves_no_temp_sibling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``except BaseException``, not ``except Exception``.

        The hand-rolled clause did not catch ``KeyboardInterrupt``, so an
        operator pressing Ctrl-C during ``service install`` left a
        ``dev.kirocrew.gateway.plist.*.tmp`` file directly in the directory
        launchd scans.
        """
        _interrupt_the_rename_of(monkeypatch, self._path)

        with pytest.raises(KeyboardInterrupt):
            svc_macos._write_plist_atomic("<plist/>\n")

        assert not self._path.exists(), "no partial plist may be published"
        assert list(self._dir.iterdir()) == [], "the temp sibling must be reclaimed"

    @POSIX_ONLY
    def test_publishes_the_plist_owner_only(self) -> None:
        """``0o600``, matching what mkstemp-without-chmod always produced."""
        svc_macos._write_plist_atomic("<plist/>\n")

        assert _mode_of(self._path) == 0o600


class TestLaunchdLiveProgramWrite:
    """``macos.write_live_program`` rewrites the shell launcher the LaunchAgent
    EXECUTES, so its explicit ``0o700`` is load-bearing in both directions:
    drop it and launchd cannot exec the agent, widen it and an owner-only
    directory starts publishing a world-readable executable."""

    @pytest.fixture(autouse=True)
    def _isolated_launcher(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._dir = tmp_path / "support"
        self._path = self._dir / "live-gateway"
        monkeypatch.setattr(svc_macos, "LIVE_PROGRAM", self._path)

    def test_routes_through_the_shared_helper_carrying_mode_0o700(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _recorder(monkeypatch, svc_macos)

        svc_macos.write_live_program("#!/bin/sh\nexec /bin/true\n")

        assert calls, "the launcher write did not go through atomic_write"
        assert calls[0][0] == str(self._path)
        assert calls[0][1] == {"mode": 0o700}, (
            "the launcher's explicit owner-only exec mode must survive the "
            "migration, and no fsync may be introduced"
        )

    @POSIX_ONLY
    def test_publishes_the_launcher_owner_only_and_executable(self) -> None:
        svc_macos.write_live_program("#!/bin/sh\nexec /bin/true\n")

        assert _mode_of(self._path) == 0o700
        assert os.access(self._path, os.X_OK), "launchd must be able to exec it"

    def test_a_ctrl_c_at_the_rename_leaves_no_temp_sibling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same ``except Exception`` gap as the plist writer, on the file the
        agent execs: a leftover ``live-gateway.*.tmp`` is an orphaned
        owner-executable script in the user's application-support directory."""
        self._dir.mkdir(parents=True, exist_ok=True)
        _interrupt_the_rename_of(monkeypatch, self._path)

        with pytest.raises(KeyboardInterrupt):
            svc_macos.write_live_program("#!/bin/sh\nexec /bin/true\n")

        assert not self._path.exists(), "no partial launcher may be published"
        assert list(self._dir.iterdir()) == [], "the temp sibling must be reclaimed"

    def test_an_explicit_path_argument_still_wins_over_the_module_default(
        self, tmp_path: Path
    ) -> None:
        """Dev Fleet passes the path it reports as live; the writer must use it."""
        explicit = tmp_path / "elsewhere" / "live-gateway"

        svc_macos.write_live_program("#!/bin/sh\nexec /bin/true\n", explicit)

        assert explicit.is_file()
        assert not self._path.exists()
