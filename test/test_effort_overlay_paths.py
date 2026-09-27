"""cli.json overlay tests: the paths the overlay writer follows and refuses.

Links and reparse points on ``.kiro``, ``settings`` and ``cli.json``, sensitive
targets, the lock that pins the directories, and every swap or retarget raced
against the walk, the lock check, the read and the publish: nothing is created,
read or replaced outside the workspace's own settings directory.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

import pytest
from effort_helpers import _read_cli_overlay

# O_NOFOLLOW is part of pinned_fs.supports_pinned_walk's required probe.
from conftest import make_dir_link, requires_o_nofollow, requires_symlinks
from kiro_crew import platform_compat
from kiro_crew.acp.types import ACP_BACKEND_KIRO
from kiro_crew.providers import acp as acp_provider
from kiro_crew.providers.acp import (
    _clear_cli_overlay_effort,
    _write_cli_overlay,
    _write_tool_search_overlay,
)
from kiro_crew.workspace_cli_settings import CLI_SETTINGS_LOCK_NAME, workspace_cli_settings_lock


def _tree(root: Path) -> list[str]:
    """Every path under *root*, relative and sorted, so a nested addition shows."""
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def _set_test_home(monkeypatch, home: Path) -> None:
    """Anchor sensitive-path checks to *home* without a cached prior HOME."""
    from kiro_crew import security

    monkeypatch.setenv("HOME", str(home))
    security._home_targets_cache.clear()


class TestCliOverlay:
    @pytest.mark.parametrize(
        "link_kind", [pytest.param("symlink", marks=requires_symlinks), "hardlink"]
    )
    def test_overlay_link_is_never_read_or_replaced(self, tmp_path, link_kind):
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        target = tmp_path / "outside.json"
        secret = b'{"token": "SECRET"}'
        target.write_bytes(secret)
        cli_json = settings_dir / "cli.json"
        try:
            if link_kind == "symlink":
                cli_json.symlink_to(target)
            else:
                os.link(target, cli_json)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"{link_kind} is unavailable: {exc}")

        with pytest.raises(OSError, match="link|hardlink|regular"):
            _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        assert _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7") is False

        provider = acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=tmp_path,
            model="auto",
        )
        assert provider._apply_effort_overlay() is False
        assert target.read_bytes() == secret
        assert not any(
            path != cli_json
            and path.is_file()
            and not path.is_symlink()
            and b"SECRET" in path.read_bytes()
            for path in settings_dir.iterdir()
        )

    @requires_o_nofollow
    def test_overlay_renamed_after_pinned_open_is_never_replaced(self, tmp_path, monkeypatch):
        from kiro_crew import pinned_fs

        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli_json = settings_dir / "cli.json"
        cli_json.write_bytes(b'{"chat.modelDefaults": {}}')
        replacement = settings_dir / "replacement.json"
        replacement_bytes = [b'{"replacement": "write"}', b'{"replacement": "clear"}']
        real_open = os.open

        def _replace_after_pinned_open(path, flags, mode=0o777, *, dir_fd=None):
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            if path == "cli.json" and dir_fd is not None:
                replacement.write_bytes(replacement_bytes.pop(0))
                os.replace(replacement, cli_json)
            return fd

        # Both capability probes read ``os.open in os.supports_dir_fd``, which the wrapped
        # ``os.open`` fails, so the pinned branch is pinned on explicitly.
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: True)
        monkeypatch.setattr(
            acp_provider.atomic_write_module, "pinned_parent_replace_supported", lambda: True
        )
        monkeypatch.setattr(acp_provider.os, "open", _replace_after_pinned_open)

        with pytest.raises(OSError, match="link|hardlink|regular"):
            _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        assert _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7") is False
        assert replacement_bytes == []
        assert cli_json.read_bytes() == b'{"replacement": "clear"}'

    @requires_symlinks
    def test_dangling_cli_json_link_is_refused(self, tmp_path):
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli_json = settings_dir / "cli.json"
        try:
            cli_json.symlink_to(tmp_path / "missing.json")
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")

        with pytest.raises(OSError, match="link|hardlink|regular"):
            _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        assert _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7") is False
        assert cli_json.is_symlink()

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            (PureWindowsPath(r"\\?\C:\w\x"), PureWindowsPath(r"C:\w\x")),
            (PureWindowsPath(r"\??\C:\w\x"), PureWindowsPath(r"C:\w\x")),
            (
                PureWindowsPath(r"\\?\UNC\host\share\x"),
                PureWindowsPath(r"\\host\share\x"),
            ),
            (PureWindowsPath(r"\\host\share\x"), PureWindowsPath(r"\\host\share\x")),
            (PureWindowsPath(r"\rooted"), PureWindowsPath(r"\rooted")),
            (PureWindowsPath(r"D:\other"), PureWindowsPath(r"D:\other")),
        ],
    )
    def test_windows_link_target_normalization_is_lexical(self, target, expected):
        from kiro_crew.workspace_cli_settings import _normalize_windows_link_target

        assert _normalize_windows_link_target(target) == expected

    @pytest.mark.parametrize("redirect", [".kiro", "settings"])
    @pytest.mark.parametrize("absolute", [False, True])
    def test_settings_link_target_inside_work_dir_is_resolved(self, tmp_path, redirect, absolute):
        from kiro_crew.workspace_cli_settings import _settings_dir_within_work_dir

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        if redirect == ".kiro":
            target = work_dir / "inside" / ".kiro"
            target.mkdir(parents=True)
            link = work_dir / ".kiro"
        else:
            (work_dir / ".kiro").mkdir()
            target = work_dir / "inside" / "settings"
            target.mkdir(parents=True)
            link = work_dir / ".kiro" / "settings"
        link_target = target if absolute else os.path.relpath(target, link.parent)
        platform_compat.symlink_or_junction(link_target, link)

        expected = target / "settings" if redirect == ".kiro" else target
        assert _settings_dir_within_work_dir(work_dir) == expected

    def test_relative_link_parent_component_that_stays_inside_is_resolved(self, tmp_path):
        from kiro_crew.workspace_cli_settings import _settings_dir_within_work_dir

        work_dir = tmp_path / "work"
        target = work_dir / "inside" / "settings"
        target.mkdir(parents=True)
        (work_dir / ".kiro").mkdir()
        platform_compat.symlink_or_junction(
            os.path.join("..", "inside", "settings"), work_dir / ".kiro" / "settings"
        )

        assert _settings_dir_within_work_dir(work_dir) == target

    def test_relative_link_parent_component_that_escapes_is_refused(self, tmp_path):
        from kiro_crew.workspace_cli_settings import _settings_dir_within_work_dir

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        platform_compat.symlink_or_junction(os.path.join("..", "outside"), work_dir / ".kiro")

        with pytest.raises(OSError, match="resolves outside"):
            _settings_dir_within_work_dir(work_dir)

    def test_absolute_link_outside_work_dir_is_refused(self, tmp_path):
        from kiro_crew.workspace_cli_settings import _settings_dir_within_work_dir

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        platform_compat.symlink_or_junction(outside, work_dir / ".kiro")

        with pytest.raises(OSError, match="resolves outside"):
            _settings_dir_within_work_dir(work_dir)

    def test_dangling_link_to_missing_inside_folder_is_returned_for_creation(self, tmp_path):
        from kiro_crew.workspace_cli_settings import _settings_dir_within_work_dir

        work_dir = tmp_path / "work"
        target = work_dir / "inside" / "missing"
        target.mkdir(parents=True)
        platform_compat.symlink_or_junction(target, work_dir / ".kiro")
        target.rmdir()

        assert _settings_dir_within_work_dir(work_dir) == target / "settings"

    def test_non_directory_settings_component_is_refused(self, tmp_path):
        from kiro_crew.workspace_cli_settings import _settings_dir_within_work_dir

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        (work_dir / ".kiro").write_text("not a directory", encoding="utf-8")

        with pytest.raises(OSError, match="not a directory"):
            _settings_dir_within_work_dir(work_dir)

    @pytest.mark.parametrize("redirect", [".kiro", "settings"])
    @pytest.mark.parametrize("with_existing_file", [False, True])
    def test_redirected_settings_directory_is_never_written(
        self, tmp_path, redirect, with_existing_file
    ):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        outside = tmp_path / "outside"
        try:
            if redirect == ".kiro":
                external_settings = outside / ".kiro" / "settings"
                external_settings.mkdir(parents=True)
                platform_compat.symlink_or_junction(outside / ".kiro", work_dir / ".kiro")
            else:
                external_settings = outside / "settings"
                external_settings.mkdir(parents=True)
                (work_dir / ".kiro").mkdir()
                platform_compat.symlink_or_junction(
                    external_settings, work_dir / ".kiro" / "settings"
                )
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")
        cli_json = external_settings / "cli.json"
        before = b'{"token": "SECRET"}'
        if with_existing_file:
            cli_json.write_bytes(before)

        with pytest.raises(OSError, match="resolves outside the work directory"):
            _write_cli_overlay(work_dir, "claude-opus-4.7", "max")
        with pytest.raises(OSError, match="resolves outside the work directory"):
            _write_tool_search_overlay(work_dir, True)
        # The gateway does not inspect the outside folder, so clear reports that
        # it could not change the file whether or not one exists there.
        assert _clear_cli_overlay_effort(work_dir, "claude-opus-4.7") is False

        if with_existing_file:
            assert cli_json.read_bytes() == before
        else:
            assert not cli_json.exists()
        assert not (external_settings / CLI_SETTINGS_LOCK_NAME).exists()

    @pytest.mark.parametrize("redirect", [".kiro", "settings"])
    def test_settings_directory_linked_inside_work_dir_is_used(self, tmp_path, redirect):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        try:
            if redirect == ".kiro":
                target_settings = work_dir / "shared" / ".kiro" / "settings"
                target_settings.mkdir(parents=True)
                platform_compat.symlink_or_junction(
                    work_dir / "shared" / ".kiro", work_dir / ".kiro"
                )
            else:
                target_settings = work_dir / "shared-settings"
                target_settings.mkdir()
                (work_dir / ".kiro").mkdir()
                platform_compat.symlink_or_junction(
                    target_settings, work_dir / ".kiro" / "settings"
                )
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")
        cli_json = target_settings / "cli.json"
        cli_json.write_bytes(b'{"kept": true}')

        _write_cli_overlay(work_dir, "claude-opus-4.7", "max")
        _write_tool_search_overlay(work_dir, True)

        document = json.loads(cli_json.read_bytes())
        assert document["kept"] is True
        assert document["chat.modelDefaults"]["claude-opus-4.7"]["output_config"]["effort"] == "max"
        assert document["toolSearch.enabled"] is True
        assert _read_cli_overlay(work_dir) == {"claude-opus-4.7": "max"}
        assert (target_settings / CLI_SETTINGS_LOCK_NAME).is_file()

        assert _clear_cli_overlay_effort(work_dir, "claude-opus-4.7") is True
        assert _read_cli_overlay(work_dir) == {}
        assert json.loads(cli_json.read_bytes())["kept"] is True
        assert not (tmp_path / ".kiro").exists()

    @requires_o_nofollow
    @pytest.mark.parametrize("pinned_walk", [True, False], ids=["pinned", "by-name"])
    @pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
    def test_sensitive_settings_link_is_refused_before_lock_or_overlay_read(
        self, tmp_path, monkeypatch, pinned_walk, absolute
    ):
        from kiro_crew import pinned_fs, workspace_cli_settings

        work_dir = tmp_path / "work"
        sensitive_dir = work_dir / ".aws"
        sensitive_dir.mkdir(parents=True)
        cli_json = sensitive_dir / "cli.json"
        sentinel = b'{"token": "NEVER-READ"}'
        cli_json.write_bytes(sentinel)
        (work_dir / ".kiro").mkdir()
        target = sensitive_dir if absolute else Path("..") / ".aws"
        platform_compat.symlink_or_junction(target, work_dir / ".kiro" / "settings")
        _set_test_home(monkeypatch, work_dir)
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: pinned_walk)

        pin_attempts = []
        read_attempts = []
        real_pinned_walk = workspace_cli_settings._pinned_settings_dir_fd
        real_by_name_pin = platform_compat.pin_directory

        def _record_pinned_walk(*args, **kwargs):
            pin_attempts.append("pinned")
            return real_pinned_walk(*args, **kwargs)

        def _record_by_name_pin(*args, **kwargs):
            pin_attempts.append("by-name")
            return real_by_name_pin(*args, **kwargs)

        def _unexpected_read(*_args, **_kwargs):
            read_attempts.append("cli.json")
            raise AssertionError("sensitive cli.json bytes must not be read")

        monkeypatch.setattr(workspace_cli_settings, "_pinned_settings_dir_fd", _record_pinned_walk)
        monkeypatch.setattr(platform_compat, "pin_directory", _record_by_name_pin)
        monkeypatch.setattr(acp_provider, "_read_pinned_cli_json", _unexpected_read)
        before = _tree(sensitive_dir)

        with pytest.raises(OSError, match="sensitive path"):
            _write_cli_overlay(work_dir, "claude-opus-4.7", "max")
        with pytest.raises(OSError, match="sensitive path"):
            _write_tool_search_overlay(work_dir, True)
        assert _clear_cli_overlay_effort(work_dir, "claude-opus-4.7") is False

        assert pin_attempts == []
        assert read_attempts == []
        assert cli_json.read_bytes() == sentinel
        assert _tree(sensitive_dir) == before
        assert not (sensitive_dir / CLI_SETTINGS_LOCK_NAME).exists()

    @requires_o_nofollow
    @pytest.mark.parametrize("pinned_walk", [True, False], ids=["pinned", "by-name"])
    def test_sensitive_settings_link_with_no_overlay_creates_nothing(
        self, tmp_path, monkeypatch, pinned_walk
    ):
        from kiro_crew import pinned_fs

        work_dir = tmp_path / "work"
        sensitive_dir = work_dir / ".aws"
        sensitive_dir.mkdir(parents=True)
        (work_dir / ".kiro").mkdir()
        platform_compat.symlink_or_junction(Path("..") / ".aws", work_dir / ".kiro" / "settings")
        _set_test_home(monkeypatch, work_dir)
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: pinned_walk)

        with pytest.raises(OSError, match="sensitive path"):
            _write_cli_overlay(work_dir, "claude-opus-4.7", "max")
        with pytest.raises(OSError, match="sensitive path"):
            _write_tool_search_overlay(work_dir, True)
        assert _clear_cli_overlay_effort(work_dir, "claude-opus-4.7") is True

        assert _tree(sensitive_dir) == []

    @requires_o_nofollow
    def test_pinned_cli_json_refuses_sensitive_descriptor_before_read(self, tmp_path, monkeypatch):
        from kiro_crew import pinned_fs

        work_dir = tmp_path / "work"
        sensitive_dir = work_dir / ".aws"
        sensitive_dir.mkdir(parents=True)
        cli_json = sensitive_dir / "cli.json"
        sentinel = b'{"token": "NEVER-READ"}'
        cli_json.write_bytes(sentinel)
        _set_test_home(monkeypatch, work_dir)
        read_attempts = []

        def _unexpected_fdopen(*_args, **_kwargs):
            read_attempts.append("cli.json")
            raise AssertionError("sensitive cli.json descriptor must not become a reader")

        monkeypatch.setattr(acp_provider.os, "fdopen", _unexpected_fdopen)
        settings_fd = os.open(sensitive_dir, pinned_fs.dir_flags())
        try:
            with pytest.raises(OSError, match="left unchanged"):
                acp_provider._read_pinned_cli_json(settings_fd, cli_json)
        finally:
            os.close(settings_fd)

        assert read_attempts == []
        assert cli_json.read_bytes() == sentinel

    def test_settings_directory_loop_is_refused(self, tmp_path):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        try:
            platform_compat.symlink_or_junction(work_dir / ".kiro", work_dir / ".kiro")
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")
        before = _tree(tmp_path)

        with pytest.raises(OSError, match="resolves outside the work directory or loops"):
            _write_cli_overlay(work_dir, "claude-opus-4.7", "max")
        with pytest.raises(OSError, match="resolves outside the work directory or loops"):
            _write_tool_search_overlay(work_dir, True)
        # The gateway cannot prove that no file exists without following the loop,
        # so the clear reports that it could not change the overlay.
        assert _clear_cli_overlay_effort(work_dir, "claude-opus-4.7") is False

        assert _tree(tmp_path) == before

    def test_work_dir_resolving_elsewhere_after_the_check_is_refused(self, tmp_path, monkeypatch):
        """A work dir that resolves elsewhere between the two resolutions is refused, not crashed."""
        from kiro_crew import workspace_cli_settings

        work_dir = tmp_path / "work"
        (work_dir / ".kiro" / "settings").mkdir(parents=True)
        moved = tmp_path / "moved"
        moved.mkdir()
        real_resolved_work_dir = workspace_cli_settings._resolved_work_dir
        calls = 0

        def _elsewhere_on_the_second_call(path):
            nonlocal calls
            calls += 1
            return moved if calls == 2 else real_resolved_work_dir(path)

        monkeypatch.setattr(
            workspace_cli_settings, "_resolved_work_dir", _elsewhere_on_the_second_call
        )
        before = _tree(tmp_path)

        with pytest.raises(OSError, match="changed while it was opened"):
            with workspace_cli_settings_lock(work_dir):
                pytest.fail("a work dir that moved must not be locked")

        assert calls == 2
        assert _tree(tmp_path) == before

    @pytest.mark.parametrize("pinned_walk", [True, False])
    @pytest.mark.parametrize(
        ("swap", "swap_at_check"), [("link-target", 1), ("resolved-folder", 2)]
    )
    def test_inside_link_swapped_outside_after_resolution_is_refused(
        self, tmp_path, monkeypatch, pinned_walk, swap, swap_at_check
    ):
        """A chain resolved inside the work dir is refused once a part of it points outside.

        ``link-target`` re-points the ``.kiro`` link after the first resolution, which the
        second resolution refuses. ``resolved-folder`` replaces the resolved ``inside-kiro``
        folder with a link after the second resolution, which the walk refuses because it
        opens the resolved names and never follows a link. On Windows the walk's pin on
        ``inside-kiro`` refuses that delete, so the swap never happens and the lock holds the
        folder inside the work dir.
        """
        from kiro_crew import pinned_fs, workspace_cli_settings

        if pinned_walk and not pinned_fs.supports_pinned_walk():
            pytest.skip("descriptor-relative directory walks are unavailable here")
        work_dir = tmp_path / "work"
        inside_kiro = work_dir / "inside-kiro"
        (inside_kiro / "settings").mkdir(parents=True)
        outside = tmp_path / "outside"
        (outside / "settings").mkdir(parents=True)
        secret = b'{"token": "SECRET"}'
        (outside / "settings" / "cli.json").write_bytes(secret)
        try:
            platform_compat.symlink_or_junction(inside_kiro, work_dir / ".kiro")
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")
        real_settings_dir = workspace_cli_settings._settings_dir_within_work_dir
        checks = 0
        swap_blocked = False

        def _swap_after_check(path):
            nonlocal checks, swap_blocked
            settings_dir = real_settings_dir(path)
            checks += 1
            if checks == swap_at_check:
                try:
                    if swap == "link-target":
                        (work_dir / ".kiro").unlink()
                        platform_compat.symlink_or_junction(outside, work_dir / ".kiro")
                    else:
                        (inside_kiro / "settings").rmdir()
                        try:
                            inside_kiro.rmdir()
                        except PermissionError:
                            swap_blocked = True
                        else:
                            platform_compat.symlink_or_junction(outside, inside_kiro)
                except (OSError, NotImplementedError) as exc:
                    pytest.fail(f"symlink is unavailable: {exc}")
            return settings_dir

        before = _tree(outside)
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: pinned_walk)
        monkeypatch.setattr(
            workspace_cli_settings, "_settings_dir_within_work_dir", _swap_after_check
        )

        failure = None
        yielded = None
        try:
            with workspace_cli_settings_lock(work_dir) as cli_json:
                yielded = cli_json
                if not swap_blocked:
                    pytest.fail("a settings chain swapped outside the work dir must not be locked")
        except OSError as exc:
            failure = exc

        assert checks >= swap_at_check
        if swap_blocked:
            assert failure is None
            assert yielded == work_dir.resolve() / "inside-kiro" / "settings" / "cli.json"
        else:
            assert failure is not None
            assert "settings directory" in str(failure)
        assert _tree(outside) == before
        assert (outside / "settings" / "cli.json").read_bytes() == secret

    def test_settings_swap_after_lock_check_cannot_read_outside_overlay(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew import workspace_cli_settings
        from kiro_crew.providers.acp import _write_tool_search_overlay

        work_dir = tmp_path / "work"
        settings_dir = work_dir / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        outside_cli_json = outside / "cli.json"
        secret = b'{"token": "SECRET"}'
        outside_cli_json.write_bytes(secret)
        settings_dir.rmdir()
        try:
            platform_compat.symlink_or_junction(outside, settings_dir)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")

        with monkeypatch.context() as temporary_patches:
            temporary_patches.setattr(
                workspace_cli_settings,
                "_settings_dir_within_work_dir",
                lambda _work_dir: settings_dir,
            )
            with pytest.raises(OSError, match="settings directory"):
                _write_cli_overlay(work_dir, "claude-opus-4.7", "max")
            with pytest.raises(OSError, match="settings directory"):
                _write_tool_search_overlay(work_dir, True)
            assert _clear_cli_overlay_effort(work_dir, "claude-opus-4.7") is False

        assert outside_cli_json.read_bytes() == secret
        assert not (outside / CLI_SETTINGS_LOCK_NAME).exists()
        assert not any(
            path.is_file() and not path.is_symlink() and b"SECRET" in path.read_bytes()
            for path in work_dir.rglob("*")
        )

    @pytest.mark.parametrize("redirect", [".kiro", "settings"])
    def test_by_name_directory_swap_after_second_check_never_creates_outside_lock(
        self, tmp_path, monkeypatch, redirect
    ):
        from kiro_crew import pinned_fs, workspace_cli_settings

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        if redirect == ".kiro":
            (outside / "settings").mkdir()
        else:
            (work_dir / ".kiro").mkdir()
        real_settings_dir = workspace_cli_settings._settings_dir_within_work_dir
        checks = 0
        swap_blocked = False

        def _swap_after_second_check(path):
            nonlocal checks, swap_blocked
            settings_dir = real_settings_dir(path)
            checks += 1
            if checks == 2:
                try:
                    settings_dir.mkdir(parents=True, exist_ok=True)
                    if redirect == ".kiro":
                        settings_dir.rmdir()
                        kiro_dir = work_dir / ".kiro"
                        try:
                            kiro_dir.rmdir()
                        except PermissionError:
                            swap_blocked = True
                        else:
                            platform_compat.symlink_or_junction(outside, kiro_dir)
                    else:
                        settings_dir.rmdir()
                        platform_compat.symlink_or_junction(outside, settings_dir)
                except (OSError, NotImplementedError) as exc:
                    pytest.fail(f"symlink is unavailable: {exc}")
            return settings_dir

        before = _tree(outside)
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(
            workspace_cli_settings, "_settings_dir_within_work_dir", _swap_after_second_check
        )

        failure = None
        yielded = None
        try:
            with workspace_cli_settings_lock(work_dir) as cli_json:
                yielded = cli_json
                if not swap_blocked:
                    pytest.fail("a swapped settings directory must not be locked")
        except OSError as exc:
            failure = exc

        if swap_blocked:
            assert failure is None
            assert yielded == work_dir.resolve() / ".kiro" / "settings" / "cli.json"
            assert _tree(outside) == before
        else:
            assert failure is not None
            assert "settings directory" in str(failure)

    def test_by_name_first_check_swap_never_creates_outside_settings(self, tmp_path, monkeypatch):
        from kiro_crew import pinned_fs, workspace_cli_settings

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        real_settings_dir = workspace_cli_settings._settings_dir_within_work_dir
        checks = 0

        def _swap_after_first_check(path):
            nonlocal checks
            settings_dir = real_settings_dir(path)
            checks += 1
            if checks == 1:
                try:
                    platform_compat.symlink_or_junction(outside, work_dir / ".kiro")
                except (OSError, NotImplementedError) as exc:
                    pytest.fail(f"symlink is unavailable: {exc}")
            return settings_dir

        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(
            workspace_cli_settings, "_settings_dir_within_work_dir", _swap_after_first_check
        )

        with pytest.raises(OSError, match="settings directory"):
            with workspace_cli_settings_lock(work_dir):
                pytest.fail("a swapped .kiro directory must not be locked")

        assert not (outside / "settings").exists()

    def test_by_name_work_dir_alias_retarget_never_reaches_a_sibling_workspace(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew import pinned_fs

        first = tmp_path / "first-workspace"
        second = tmp_path / "second-workspace"
        first.mkdir()
        (second / ".kiro" / "settings").mkdir(parents=True)
        sibling_cli_json = second / ".kiro" / "settings" / "cli.json"
        operator_document = b'{"chat.modelDefaults": {"m": {"output_config": {"effort": "max"}}}}'
        sibling_cli_json.write_bytes(operator_document)
        alias = tmp_path / "alias"
        try:
            make_dir_link(alias, first)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")

        real_pin_directory = platform_compat.pin_directory
        pins = 0

        def _retarget_alias_on_first_pin(path):
            nonlocal pins
            pinned = real_pin_directory(path)
            pins += 1
            if pins == 1:
                try:
                    platform_compat.unlink_link_or_junction(alias)
                    make_dir_link(alias, second)
                except (OSError, NotImplementedError) as exc:
                    pytest.fail(f"symlink is unavailable: {exc}")
            return pinned

        before = _tree(second)
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(platform_compat, "pin_directory", _retarget_alias_on_first_pin)

        yielded = None
        try:
            with workspace_cli_settings_lock(alias) as cli_json:
                yielded = cli_json
                cli_json.write_bytes(b"{}")
        except OSError as exc:
            assert "settings directory" in str(exc)

        assert pins >= 1
        if yielded is not None:
            assert yielded == first.resolve() / ".kiro" / "settings" / "cli.json"
        assert sibling_cli_json.read_bytes() == operator_document
        assert _tree(second) == before

    def test_work_dir_alias_retarget_after_the_second_resolution_stays_in_the_first_workspace(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew import workspace_cli_settings

        first = tmp_path / "first-workspace"
        second = tmp_path / "second-workspace"
        first.mkdir()
        second_cli_json = second / ".kiro" / "settings" / "cli.json"
        second_cli_json.parent.mkdir(parents=True)
        operator_document = b'{"chat.modelDefaults": {"m": {"output_config": {"effort": "max"}}}}'
        second_cli_json.write_bytes(operator_document)
        alias = tmp_path / "alias"
        try:
            make_dir_link(alias, first)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")

        real_settings_dir = workspace_cli_settings._settings_dir_within_work_dir
        resolutions = 0

        def _retarget_after_second_resolution(path):
            nonlocal resolutions
            settings_dir = real_settings_dir(path)
            resolutions += 1
            if resolutions == 2:
                try:
                    platform_compat.unlink_link_or_junction(alias)
                    make_dir_link(alias, second)
                except (OSError, NotImplementedError) as exc:
                    pytest.fail(f"symlink is unavailable: {exc}")
            return settings_dir

        before = _tree(second)
        monkeypatch.setattr(
            workspace_cli_settings,
            "_settings_dir_within_work_dir",
            _retarget_after_second_resolution,
        )

        with workspace_cli_settings_lock(alias) as cli_json:
            assert cli_json == first.resolve() / ".kiro" / "settings" / "cli.json"
            cli_json.write_bytes(b"{}")

        assert resolutions == 2
        assert second_cli_json.read_bytes() == operator_document
        assert _tree(second) == before
        assert (first / ".kiro" / "settings" / "cli.json").read_bytes() == b"{}"

    def test_by_name_fallback_creates_and_pins_fresh_settings(self, tmp_path, monkeypatch):
        from kiro_crew import pinned_fs

        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)

        with workspace_cli_settings_lock(tmp_path) as cli_json:
            assert cli_json == tmp_path / ".kiro" / "settings" / "cli.json"
            assert cli_json.parent.is_dir()
            assert not (tmp_path / ".kiro").is_symlink()
            assert not cli_json.parent.is_symlink()

    def test_by_name_lock_pins_directories_until_after_overlay_publish(self, tmp_path, monkeypatch):
        from kiro_crew import pinned_fs, platform_compat

        pins = []
        checked = []
        real_pin_directory = platform_compat.pin_directory
        real_open_lock_file = platform_compat.open_lock_file
        real_atomic_write = acp_provider.atomic_write

        def _record_pin_directory(path):
            descriptor = real_pin_directory(path)
            info = os.fstat(descriptor)
            pins.append((Path(path).name, descriptor, (info.st_dev, info.st_ino)))
            return descriptor

        def _assert_lock_pins_held(when):
            assert [name for name, _, _ in pins[:2]] == [".kiro", "settings"]
            for _, descriptor, identity in pins[:2]:
                info = os.fstat(descriptor)
                assert (info.st_dev, info.st_ino) == identity
            checked.append(when)

        @contextmanager
        def _opening_lock(path):
            if Path(path).name == CLI_SETTINGS_LOCK_NAME:
                _assert_lock_pins_held("lock")
            with real_open_lock_file(path) as lock_file:
                yield lock_file

        def _publishing(path, content, **kwargs):
            if Path(path).name == "cli.json":
                _assert_lock_pins_held("publish")
            return real_atomic_write(path, content, **kwargs)

        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(platform_compat, "pin_directory", _record_pin_directory)
        monkeypatch.setattr(platform_compat, "open_lock_file", _opening_lock)
        monkeypatch.setattr(acp_provider, "atomic_write", _publishing)

        _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")

        assert checked == ["lock", "publish"]
        for _, descriptor, identity in pins[:2]:
            try:
                info = os.fstat(descriptor)
            except OSError:
                continue
            # A released number can be reused by a later open; the pinned folder cannot.
            assert (info.st_dev, info.st_ino) != identity

    @pytest.mark.parametrize("redirect", [".kiro", "settings"])
    def test_by_name_pinned_directory_identity_rejects_a_swap_before_lock_open(
        self, tmp_path, monkeypatch, redirect
    ):
        from kiro_crew import pinned_fs, platform_compat

        work_dir = tmp_path / "work"
        settings_dir = work_dir / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        if redirect == ".kiro":
            (outside / "settings").mkdir()
        real_open_lock_file = platform_compat.open_lock_file
        swap_blocked = False

        @contextmanager
        def _swap_before_lock_open(path):
            nonlocal swap_blocked
            if redirect == ".kiro":
                try:
                    settings_dir.rmdir()
                    kiro_dir = work_dir / ".kiro"
                    kiro_dir.rmdir()
                except PermissionError:
                    swap_blocked = True
                else:
                    platform_compat.symlink_or_junction(outside, kiro_dir)
            else:
                try:
                    settings_dir.rmdir()
                except PermissionError:
                    swap_blocked = True
                else:
                    platform_compat.symlink_or_junction(outside, settings_dir)
            with real_open_lock_file(path) as lock_file:
                yield lock_file

        before = _tree(outside)
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(platform_compat, "open_lock_file", _swap_before_lock_open)

        failure = None
        try:
            with workspace_cli_settings_lock(work_dir) as cli_json:
                if not swap_blocked:
                    pytest.fail("a swapped settings directory must not be locked")
                assert cli_json == work_dir / ".kiro" / "settings" / "cli.json"
        except OSError as exc:
            failure = exc

        if swap_blocked:
            assert failure is None
            assert _tree(outside) == before
        else:
            assert failure is not None
            assert "settings directory changed while it was opened" in str(failure)

    @pytest.mark.parametrize("when", ["at_its_pin", "after_settings_pin"])
    def test_by_name_reparse_point_on_held_kiro_is_refused(self, tmp_path, monkeypatch, when):
        import stat
        from types import SimpleNamespace

        from kiro_crew import pinned_fs, platform_compat

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        real_pin_directory = platform_compat.pin_directory
        real_lstat_by_name = pinned_fs.lstat_by_name
        reparsed = []
        lock_opens = []

        def _pin_directory(path):
            descriptor = real_pin_directory(path)
            pinned = Path(path).name
            if (when, pinned) in {("at_its_pin", ".kiro"), ("after_settings_pin", "settings")}:
                reparsed.append(path)
            return descriptor

        @contextmanager
        def _open_lock_file(path):
            lock_opens.append(path)
            yield None

        # The held .kiro reports what Windows reports for an empty folder that took a reparse
        # point in place: the same file id, the reparse attribute.
        def _lstat_by_name(target):
            info = real_lstat_by_name(target)
            if reparsed and info is not None and Path(target).name == ".kiro":
                return SimpleNamespace(
                    st_mode=info.st_mode,
                    st_dev=info.st_dev,
                    st_ino=info.st_ino,
                    st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                )
            return info

        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(platform_compat, "pin_directory", _pin_directory)
        monkeypatch.setattr(platform_compat, "open_lock_file", _open_lock_file)
        monkeypatch.setattr(pinned_fs, "lstat_by_name", _lstat_by_name)

        with pytest.raises(OSError, match="settings directory changed while it was opened"):
            with workspace_cli_settings_lock(work_dir):
                pytest.fail("a .kiro folder that took a reparse point must not be locked")

        assert lock_opens == []
        # Refused at its own check, nothing is created through a .kiro that is a reparse point.
        assert (work_dir / ".kiro" / "settings").exists() is (when == "after_settings_pin")

    @pytest.mark.parametrize("when", ["after_pins", "at_lock_open"])
    def test_by_name_reparse_point_on_held_settings_is_refused(self, tmp_path, monkeypatch, when):
        import stat
        from types import SimpleNamespace

        from kiro_crew import pinned_fs, platform_compat

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        real_pin_directory = platform_compat.pin_directory
        real_open_lock_file = platform_compat.open_lock_file
        real_lstat_by_name = pinned_fs.lstat_by_name
        reparsed = []
        lock_opens = []

        def _pin_directory(path):
            descriptor = real_pin_directory(path)
            if when == "after_pins" and Path(path).name == "settings":
                reparsed.append(path)
            return descriptor

        @contextmanager
        def _open_lock_file(path):
            lock_opens.append(path)
            if when == "at_lock_open":
                reparsed.append(path)
            with real_open_lock_file(path) as lock_file:
                yield lock_file

        # Linux cannot set a reparse point, so the held folder reports what Windows reports
        # for an empty folder that took one in place: the same file id, the reparse attribute.
        def _lstat_by_name(target):
            info = real_lstat_by_name(target)
            if reparsed and info is not None and Path(target).name == "settings":
                return SimpleNamespace(
                    st_mode=info.st_mode,
                    st_dev=info.st_dev,
                    st_ino=info.st_ino,
                    st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                )
            return info

        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(platform_compat, "pin_directory", _pin_directory)
        monkeypatch.setattr(platform_compat, "open_lock_file", _open_lock_file)
        monkeypatch.setattr(pinned_fs, "lstat_by_name", _lstat_by_name)

        with pytest.raises(OSError, match="settings directory changed while it was opened"):
            with workspace_cli_settings_lock(work_dir):
                pytest.fail("a settings folder that took a reparse point must not be locked")

        assert len(lock_opens) == (0 if when == "after_pins" else 1)

    @pytest.mark.timeout(30)
    @requires_o_nofollow
    @pytest.mark.parametrize("redirect", [".kiro", "settings"])
    def test_directory_swap_after_lock_check_never_creates_outside_lock(
        self, tmp_path, monkeypatch, redirect
    ):
        from kiro_crew import workspace_cli_settings

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        if redirect == ".kiro":
            (outside / "settings").mkdir()
        else:
            (work_dir / ".kiro").mkdir()
        real_settings_dir = workspace_cli_settings._settings_dir_within_work_dir
        checks = 0

        def _swap_after_check(path):
            nonlocal checks
            settings_dir = real_settings_dir(path)
            checks += 1
            if checks == 2:
                try:
                    if redirect == ".kiro":
                        settings = work_dir / ".kiro" / "settings"
                        if settings.exists():
                            settings.rmdir()
                        kiro_dir = work_dir / ".kiro"
                        if kiro_dir.exists():
                            kiro_dir.rmdir()
                        platform_compat.symlink_or_junction(outside, kiro_dir)
                    else:
                        settings = work_dir / ".kiro" / "settings"
                        if settings.exists():
                            settings.rmdir()
                        platform_compat.symlink_or_junction(outside, settings)
                except (OSError, NotImplementedError) as exc:
                    pytest.fail(f"symlink is unavailable: {exc}")
            return settings_dir

        before = _tree(outside)
        monkeypatch.setattr(
            workspace_cli_settings, "_settings_dir_within_work_dir", _swap_after_check
        )

        with pytest.raises(OSError, match="settings directory"):
            with workspace_cli_settings_lock(work_dir):
                pytest.fail("a swapped settings directory must not be locked")

        assert _tree(outside) == before

    @requires_o_nofollow
    @pytest.mark.parametrize("swap", [".kiro", "settings", "removed"])
    def test_directory_swap_after_pinned_walk_never_creates_outside_lock(
        self, tmp_path, monkeypatch, swap
    ):
        from kiro_crew import workspace_cli_settings

        work_dir = tmp_path / "work"
        (work_dir / ".kiro" / "settings").mkdir(parents=True)
        outside = tmp_path / "outside"
        (outside / "settings").mkdir(parents=True)
        real_pinned_settings_dir_fd = workspace_cli_settings._pinned_settings_dir_fd

        def _swap_after_walk(resolved_work_dir, settings_dir):
            settings_fd = real_pinned_settings_dir_fd(resolved_work_dir, settings_dir)
            try:
                if swap == ".kiro":
                    (work_dir / ".kiro").rename(work_dir / ".kiro-real")
                    platform_compat.symlink_or_junction(outside, work_dir / ".kiro")
                elif swap == "settings":
                    settings = work_dir / ".kiro" / "settings"
                    settings.rename(work_dir / ".kiro" / "settings-real")
                    platform_compat.symlink_or_junction(outside / "settings", settings)
                else:
                    (work_dir / ".kiro" / "settings").rmdir()
            except (OSError, NotImplementedError) as exc:
                os.close(settings_fd)
                pytest.fail(f"symlink is unavailable: {exc}")
            return settings_fd

        before = _tree(outside)
        monkeypatch.setattr(workspace_cli_settings, "_pinned_settings_dir_fd", _swap_after_walk)

        expected = "was removed" if swap == "removed" else "changed while it was opened"
        with pytest.raises(OSError, match=expected):
            with workspace_cli_settings_lock(work_dir):
                pytest.fail("a swapped settings directory must not be locked")

        assert _tree(outside) == before

    @requires_o_nofollow
    def test_pinned_lock_create_lost_to_a_sibling_opens_the_siblings_lock(
        self, tmp_path, monkeypatch
    ):
        """A taker that loses the lock's create to a sibling holds the sibling's file.

        Darwin answers a nonexclusive ``O_CREAT`` open of a name two callers race to
        create with ``ENOENT``; the settings lock is taken through the exclusive-then-reopen
        helper, so a warm-pool fill and a chat start racing on one work dir both lock.
        """
        import errno

        from kiro_crew import pinned_fs, platform_compat

        work_dir = tmp_path / "work"
        settings = work_dir / ".kiro" / "settings"
        settings.mkdir(parents=True)
        (settings / CLI_SETTINGS_LOCK_NAME).write_bytes(b"")
        real_open = os.open
        racing_creates = []

        def _darwin_racing_open(path, flags, mode=0o777, *, dir_fd=None):
            if (
                os.fspath(path) == CLI_SETTINGS_LOCK_NAME
                and flags & os.O_CREAT
                and not flags & os.O_EXCL
            ):
                racing_creates.append(flags)
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        # ``supports_pinned_walk`` reads ``os.open in os.supports_dir_fd``, which the patched
        # ``os.open`` fails, so the pinned branch is pinned on explicitly.
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: True)
        monkeypatch.setattr(os, "open", _darwin_racing_open)

        with workspace_cli_settings_lock(work_dir) as cli_json:
            assert cli_json == settings / "cli.json"
            twin_fd = real_open(settings / CLI_SETTINGS_LOCK_NAME, os.O_RDWR)
            try:
                assert not platform_compat.try_acquire_lock(twin_fd, exclusive=True)
            finally:
                os.close(twin_fd)

        assert racing_creates == []

    @requires_o_nofollow
    def test_hardlinked_lock_twin_cannot_carry_a_swapped_settings_directory(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew import workspace_cli_settings

        work_dir = tmp_path / "work"
        settings = work_dir / ".kiro" / "settings"
        settings.mkdir(parents=True)
        (settings / CLI_SETTINGS_LOCK_NAME).write_bytes(b"")
        outside = tmp_path / "outside"
        (outside / "settings").mkdir(parents=True)
        try:
            os.link(
                settings / CLI_SETTINGS_LOCK_NAME, outside / "settings" / CLI_SETTINGS_LOCK_NAME
            )
        except OSError as exc:
            pytest.fail(f"hardlink is unavailable: {exc}")
        real_pinned_settings_dir_fd = workspace_cli_settings._pinned_settings_dir_fd

        def _swap_after_walk(resolved_work_dir, settings_dir):
            settings_fd = real_pinned_settings_dir_fd(resolved_work_dir, settings_dir)
            try:
                (work_dir / ".kiro").rename(work_dir / ".kiro-real")
                platform_compat.symlink_or_junction(outside, work_dir / ".kiro")
            except (OSError, NotImplementedError) as exc:
                os.close(settings_fd)
                pytest.fail(f"symlink is unavailable: {exc}")
            return settings_fd

        before = _tree(outside)
        monkeypatch.setattr(workspace_cli_settings, "_pinned_settings_dir_fd", _swap_after_walk)

        with pytest.raises(OSError, match="settings directory changed while it was opened"):
            with workspace_cli_settings_lock(work_dir):
                pytest.fail("a lock reached through a swapped settings directory is not held")

        assert _tree(outside) == before

    @pytest.mark.parametrize("redirect", [".kiro", "settings"])
    @pytest.mark.parametrize("floor", ["pinned", "by_name"])
    def test_hardlinked_lock_twin_swapped_in_while_acquiring_is_refused(
        self, tmp_path, monkeypatch, floor, redirect
    ):
        from kiro_crew import pinned_fs, platform_compat

        work_dir = tmp_path / "work"
        settings = work_dir / ".kiro" / "settings"
        settings.mkdir(parents=True)
        (settings / CLI_SETTINGS_LOCK_NAME).write_bytes(b"")
        outside = tmp_path / "outside"
        (outside / "settings").mkdir(parents=True)
        os.link(settings / CLI_SETTINGS_LOCK_NAME, outside / "settings" / CLI_SETTINGS_LOCK_NAME)
        moved = work_dir / ".kiro" if redirect == ".kiro" else settings
        target = outside if redirect == ".kiro" else outside / "settings"
        real_file_lock = platform_compat.file_lock
        swap_blocked = False

        @contextmanager
        def _swap_then_lock(fd, **kwargs):
            nonlocal swap_blocked
            try:
                moved.rename(moved.with_name(moved.name + "-real"))
            except PermissionError:
                swap_blocked = True
            else:
                platform_compat.symlink_or_junction(target, moved)
            with real_file_lock(fd, **kwargs):
                yield

        before = _tree(outside)
        if floor == "by_name":
            monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(platform_compat, "file_lock", _swap_then_lock)

        failure = None
        try:
            with workspace_cli_settings_lock(work_dir):
                pass
        except OSError as exc:
            failure = exc

        if swap_blocked:
            assert failure is None
        else:
            assert failure is not None
            assert "settings directory changed while it was acquired" in str(failure)
        assert _tree(outside) == before

    @requires_o_nofollow
    @pytest.mark.parametrize("writer", ["effort", "tool_search", "clear"])
    @pytest.mark.parametrize("swap", ["link", "directory"])
    def test_settings_swap_after_read_never_redirects_publish(
        self, tmp_path, monkeypatch, writer, swap
    ):
        from kiro_crew.providers.acp import _write_tool_search_overlay

        work_dir = tmp_path / "work"
        settings_dir = work_dir / ".kiro" / "settings"
        _write_cli_overlay(work_dir, "claude-opus-4.7", "max")
        held_settings_dir = work_dir / ".kiro" / "settings-held"
        replacement = tmp_path / "outside" if swap == "link" else settings_dir
        if swap == "link":
            replacement.mkdir()
        replacement_cli_json = replacement / "cli.json"
        sentinel = b'{"token": "SECRET"}'
        if swap == "link":
            replacement_cli_json.write_bytes(sentinel)
            before_names = {path.name for path in replacement.iterdir()}
        else:
            before_names = {"cli.json"}
        held_before = (settings_dir / "cli.json").read_bytes()
        real_read = acp_provider._read_cli_overlay_document

        def _swap_after_read(settings):
            document = real_read(settings)
            settings_dir.rename(held_settings_dir)
            try:
                if swap == "link":
                    platform_compat.symlink_or_junction(replacement, settings_dir)
                else:
                    settings_dir.mkdir()
            except (OSError, NotImplementedError) as exc:
                pytest.fail(f"settings replacement is unavailable: {exc}")
            if swap == "directory":
                replacement_cli_json.write_bytes(sentinel)
            return document

        with monkeypatch.context() as temporary_patches:
            temporary_patches.setattr(acp_provider, "_read_cli_overlay_document", _swap_after_read)
            if writer == "effort":
                with pytest.raises(OSError, match="workspace cli.json overlay|settings directory"):
                    _write_cli_overlay(work_dir, "claude-opus-4.7", "low")
            elif writer == "tool_search":
                with pytest.raises(OSError, match="workspace cli.json overlay|settings directory"):
                    _write_tool_search_overlay(work_dir, True)
            else:
                assert _clear_cli_overlay_effort(work_dir, "claude-opus-4.7") is False

        assert replacement_cli_json.read_bytes() == sentinel
        assert {path.name for path in replacement.iterdir()} == before_names
        assert (held_settings_dir / "cli.json").read_bytes() == held_before

    @requires_o_nofollow
    @pytest.mark.parametrize("writer", ["effort", "tool_search"])
    @pytest.mark.parametrize("path", ["pinned", "by-name"])
    @pytest.mark.parametrize("outside_write", ["replace", "in-place"])
    def test_a_projection_that_writes_nothing_still_refuses_a_cli_json_changed_after_read(
        self, tmp_path, monkeypatch, writer, path, outside_write
    ):
        work_dir = tmp_path / "work"
        _write_cli_overlay(work_dir, "claude-opus-4.7", "max")
        if writer == "tool_search":
            _write_tool_search_overlay(work_dir, True)
        cli_json = work_dir / ".kiro" / "settings" / "cli.json"
        outside_bytes = b'{"operator.sentinel": true, "padding": "outside-write"}'
        outside_write_landed = False
        real_read = acp_provider._read_cli_overlay_document

        def _outside_write_after_read(settings):
            nonlocal outside_write_landed
            document = real_read(settings)
            outside_write_landed = True
            if outside_write == "replace":
                replacement = cli_json.with_name("replacement.json")
                replacement.write_bytes(outside_bytes)
                os.replace(replacement, cli_json)
            else:
                cli_json.write_bytes(outside_bytes)
            return document

        with monkeypatch.context() as temporary_patches:
            temporary_patches.setattr(
                acp_provider.atomic_write_module,
                "pinned_parent_replace_supported",
                lambda: path == "pinned",
            )
            temporary_patches.setattr(
                acp_provider, "_read_cli_overlay_document", _outside_write_after_read
            )
            if writer == "effort":
                with pytest.raises(OSError, match="changed while the overlay was written"):
                    _write_cli_overlay(work_dir, "claude-opus-4.7", "max")
            else:
                with pytest.raises(OSError, match="changed while the overlay was written"):
                    _write_tool_search_overlay(work_dir, True)

        assert outside_write_landed
        assert cli_json.read_bytes() == outside_bytes

    @requires_o_nofollow
    @pytest.mark.parametrize("writer", ["effort", "tool_search", "clear"])
    @pytest.mark.parametrize("path", ["pinned", "by-name"])
    @pytest.mark.parametrize("outside_write", ["replace", "in-place"])
    def test_cli_json_changed_after_read_is_not_overwritten(
        self, tmp_path, monkeypatch, writer, path, outside_write
    ):
        work_dir = tmp_path / "work"
        _write_cli_overlay(work_dir, "claude-opus-4.7", "max")
        cli_json = work_dir / ".kiro" / "settings" / "cli.json"
        outside_bytes = b'{"operator.sentinel": true, "padding": "outside-write"}'
        real_read = acp_provider._read_cli_overlay_document

        def _outside_write_after_read(settings):
            document = real_read(settings)
            if outside_write == "replace":
                replacement = cli_json.with_name("replacement.json")
                replacement.write_bytes(outside_bytes)
                os.replace(replacement, cli_json)
            else:
                cli_json.write_bytes(outside_bytes)
            return document

        with monkeypatch.context() as temporary_patches:
            temporary_patches.setattr(
                acp_provider.atomic_write_module,
                "pinned_parent_replace_supported",
                lambda: path == "pinned",
            )
            temporary_patches.setattr(
                acp_provider, "_read_cli_overlay_document", _outside_write_after_read
            )
            if writer == "effort":
                with pytest.raises(OSError, match="changed while the overlay was written"):
                    _write_cli_overlay(work_dir, "claude-opus-4.7", "low")
            elif writer == "tool_search":
                with pytest.raises(OSError, match="changed while the overlay was written"):
                    _write_tool_search_overlay(work_dir, True)
            else:
                assert _clear_cli_overlay_effort(work_dir, "claude-opus-4.7") is False

        assert cli_json.read_bytes() == outside_bytes

    @requires_o_nofollow
    @pytest.mark.parametrize("writer", ["effort", "tool_search", "clear"])
    @pytest.mark.parametrize("path", ["pinned", "by-name"])
    @pytest.mark.parametrize("outside_write", ["replace", "in-place"])
    def test_cli_json_changed_just_after_its_bytes_are_read_is_not_overwritten(
        self, tmp_path, monkeypatch, writer, path, outside_write
    ):
        work_dir = tmp_path / "work"
        _write_cli_overlay(work_dir, "claude-opus-4.7", "max")
        cli_json = work_dir / ".kiro" / "settings" / "cli.json"
        read_file = os.stat(cli_json, follow_symlinks=False)
        outside_bytes = b'{"operator.sentinel": true, "padding": "outside-write"}'
        outside_write_landed = False

        def _write_outside():
            nonlocal outside_write_landed
            outside_write_landed = True
            if outside_write == "replace":
                replacement = cli_json.with_name("replacement.json")
                replacement.write_bytes(outside_bytes)
                os.replace(replacement, cli_json)
            else:
                cli_json.write_bytes(outside_bytes)

        real_fstat = os.fstat
        real_read_by_name = acp_provider.safe_read_file_bytes_nolink

        def _fstat_writing_outside_once_the_bytes_are_read(fd, *args, **kwargs):
            if not outside_write_landed:
                metadata = real_fstat(fd)
                same_file = (metadata.st_dev, metadata.st_ino) == (
                    read_file.st_dev,
                    read_file.st_ino,
                )
                # Only the pinned reader's fstat after its read finds the offset past 0.
                if same_file and os.lseek(fd, 0, os.SEEK_CUR) > 0:
                    _write_outside()
            return real_fstat(fd, *args, **kwargs)

        def _read_by_name_then_write_outside(*args, **kwargs):
            raw = real_read_by_name(*args, **kwargs)
            _write_outside()
            return raw

        with monkeypatch.context() as temporary_patches:
            temporary_patches.setattr(
                acp_provider.atomic_write_module,
                "pinned_parent_replace_supported",
                lambda: path == "pinned",
            )
            if path == "pinned":
                temporary_patches.setattr(
                    acp_provider.os, "fstat", _fstat_writing_outside_once_the_bytes_are_read
                )
            else:
                temporary_patches.setattr(
                    acp_provider, "safe_read_file_bytes_nolink", _read_by_name_then_write_outside
                )
            if writer == "effort":
                with pytest.raises(OSError, match="changed while the overlay was written"):
                    _write_cli_overlay(work_dir, "claude-opus-4.7", "low")
            elif writer == "tool_search":
                with pytest.raises(OSError, match="changed while the overlay was written"):
                    _write_tool_search_overlay(work_dir, True)
            else:
                assert _clear_cli_overlay_effort(work_dir, "claude-opus-4.7") is False

        assert outside_write_landed
        assert cli_json.read_bytes() == outside_bytes

    @requires_o_nofollow
    @pytest.mark.parametrize("writer", ["effort", "tool_search", "clear"])
    def test_settings_directory_replaced_after_the_publish_check_keeps_its_cli_json(
        self, tmp_path, monkeypatch, writer
    ):
        from kiro_crew.providers.acp import _write_tool_search_overlay
        from kiro_crew.workspace_cli_settings import LockedCliSettings

        work_dir = tmp_path / "work"
        settings_dir = work_dir / ".kiro" / "settings"
        _write_cli_overlay(work_dir, "claude-opus-4.7", "max")
        held_settings_dir = work_dir / ".kiro" / "settings-held"
        replacement_cli_json = settings_dir / "cli.json"
        sentinel = b'{"operator.sentinel": true}'
        real_check = LockedCliSettings.holds_named_settings_dir
        checks = 0

        def _replace_after_first_check(settings):
            nonlocal checks
            holds = real_check(settings)
            checks += 1
            if checks == 1:
                settings_dir.rename(held_settings_dir)
                settings_dir.mkdir()
                replacement_cli_json.write_bytes(sentinel)
            return holds

        with monkeypatch.context() as temporary_patches:
            temporary_patches.setattr(
                LockedCliSettings, "holds_named_settings_dir", _replace_after_first_check
            )
            if writer == "effort":
                with pytest.raises(OSError, match="settings directory changed while"):
                    _write_cli_overlay(work_dir, "claude-opus-4.7", "low")
            elif writer == "tool_search":
                with pytest.raises(OSError, match="settings directory changed while"):
                    _write_tool_search_overlay(work_dir, True)
            else:
                assert _clear_cli_overlay_effort(work_dir, "claude-opus-4.7") is False

        assert checks == 2
        assert replacement_cli_json.read_bytes() == sentinel
        assert {path.name for path in settings_dir.iterdir()} == {"cli.json"}

    @requires_o_nofollow
    def test_settings_directory_replacement_before_read_cannot_receive_publish(
        self, tmp_path, monkeypatch
    ):

        work_dir = tmp_path / "work"
        settings_dir = work_dir / ".kiro" / "settings"
        _write_cli_overlay(work_dir, "claude-opus-4.7", "max")
        held_settings_dir = work_dir / ".kiro" / "settings-held"
        replacement_cli_json = settings_dir / "cli.json"
        sentinel = (
            b'{"operator.sentinel": true, "chat.modelDefaults": {"claude-opus-4.7": '
            b'{"output_config": {"effort": "max"}}}}'
        )
        real_read = acp_provider._read_cli_overlay_document
        documents_read = []

        def _replace_before_read(settings):
            settings_dir.rename(held_settings_dir)
            settings_dir.mkdir()
            replacement_cli_json.write_bytes(sentinel)
            document = real_read(settings)
            documents_read.append(document)
            return document

        with monkeypatch.context() as temporary_patches:
            temporary_patches.setattr(
                acp_provider, "_read_cli_overlay_document", _replace_before_read
            )
            with pytest.raises(OSError, match="settings directory"):
                _write_cli_overlay(work_dir, "claude-opus-4.7", "low")

        assert documents_read and "operator.sentinel" not in documents_read[0]
        assert replacement_cli_json.read_bytes() == sentinel
        assert {path.name for path in settings_dir.iterdir()} == {"cli.json"}

    @pytest.mark.timeout(30)
    @pytest.mark.parametrize("kind", ["fifo", "directory"])
    def test_non_regular_cli_json_is_refused_without_blocking_or_leaking(
        self, tmp_path, monkeypatch, kind
    ):
        import stat

        from kiro_crew.providers.acp import _write_tool_search_overlay

        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli_json = settings_dir / "cli.json"
        simulated_fifo = kind == "fifo" and not hasattr(os, "mkfifo")
        if kind == "fifo" and not simulated_fifo:
            os.mkfifo(cli_json)
        elif kind == "fifo":
            cli_json.write_bytes(b"{}")
            cli_json_stat = os.stat(cli_json)
            real_fstat = os.fstat

            def _fstat_as_fifo(fd):
                info = real_fstat(fd)
                if (info.st_dev, info.st_ino) == (cli_json_stat.st_dev, cli_json_stat.st_ino):
                    return os.stat_result(
                        (
                            stat.S_IFIFO | (info.st_mode & 0o7777),
                            *info[1 : os.stat_result.n_sequence_fields],
                        ),
                        {name: getattr(info, name) for name in dir(info) if name.startswith("st_")},
                    )
                return info

            monkeypatch.setattr(os, "fstat", _fstat_as_fifo)
        else:
            cli_json.mkdir()
        open_descriptors = Path("/proc/self/fd")

        def _refused_round():
            with pytest.raises(OSError, match="workspace cli.json overlay"):
                _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
            with pytest.raises(OSError, match="workspace cli.json overlay"):
                _write_tool_search_overlay(tmp_path, True)
            assert _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7") is False

        _refused_round()
        open_before = len(os.listdir(open_descriptors)) if open_descriptors.is_dir() else None

        for _ in range(5):
            _refused_round()

        if open_before is not None:
            assert len(os.listdir(open_descriptors)) == open_before
        if kind == "fifo" and not simulated_fifo:
            assert cli_json.is_fifo()
        elif kind == "fifo":
            assert cli_json.is_file()
        else:
            assert cli_json.is_dir()

    @requires_symlinks
    def test_leaf_link_inside_settings_is_never_read_or_replaced(self, tmp_path):
        from kiro_crew.providers.acp import _write_tool_search_overlay

        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli_json = settings_dir / "cli.json"
        other_json = settings_dir / "other.json"
        secret = b'{"token": "SECRET"}'
        other_json.write_bytes(secret)
        try:
            cli_json.symlink_to(other_json)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")

        with pytest.raises(OSError, match="is a link"):
            _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        with pytest.raises(OSError, match="is a link"):
            _write_tool_search_overlay(tmp_path, True)
        assert _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7") is False
        assert cli_json.is_symlink()
        assert other_json.read_bytes() == secret
