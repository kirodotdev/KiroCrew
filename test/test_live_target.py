"""Tests for ``kiro_crew.service.live_target`` — the live-target pointer.

Pins the security and correctness properties of the mechanism that decides which
checkout the gateway executes:

- The pointer is validated before use and before write.
- Resolution is fail-safe, never fail-open: any unusable pointer is ignored.
- The loop guard is airtight: two independent mechanisms (env marker + realpath
  comparison) prevent infinite exec chains.
- The env set up by the exec carries the right identity into the child.
- The ``live_target.json`` leaf is keystone-fenced under both crew-home prefixes
  in the security module (a ratchet test that fails loudly if dropped).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.service.live_target import (
    _FILENAME,
    _MODE,
    EXEC_MARKER,
    InvalidTarget,
    maybe_reexec,
    pointer_path,
    read_target,
    read_target_reason,
    restore,
    snapshot,
    target_bin,
    validate,
    write_target,
)


@pytest.fixture(autouse=True)
def _isolate_pointer(tmp_path, monkeypatch):
    """Route pointer_path() to tmp_path so no test touches the real data home."""
    monkeypatch.setattr(
        "kiro_crew.config.loader.config_dir", lambda: tmp_path
    )


def _place_entry_point(checkout: Path, mode: int = 0o755) -> Path:
    """Create the checkout's venv entry point where production looks for it.

    Routed through ``target_bin`` so the fixture tracks the platform layout
    (``.venv/bin/kirocrew`` vs ``.venv/Scripts/kirocrew.exe``) instead of pinning
    one and failing validation everywhere else.
    """
    kcbin = target_bin(checkout)
    kcbin.parent.mkdir(parents=True, exist_ok=True)
    kcbin.write_text("#!/bin/sh\n")
    kcbin.chmod(mode)
    return kcbin


def _make_valid_checkout(tmp_path: Path) -> Path:
    """Build a minimal valid checkout tree under tmp_path."""
    checkout = tmp_path / "my-checkout"
    checkout.mkdir()
    (checkout / "src" / "kiro_crew").mkdir(parents=True)
    _place_entry_point(checkout)
    return checkout


# ─── validate() ────────────────────────────────────────────────────────────


class TestValidate:
    """Each rejection reason produces InvalidTarget with a distinct message."""

    def test_rejects_empty_string(self):
        with pytest.raises(InvalidTarget, match="empty"):
            validate("")

    def test_rejects_blank_only(self):
        with pytest.raises(InvalidTarget, match="empty"):
            validate("   \t  ")

    def test_rejects_newline_control_character(self):
        with pytest.raises(InvalidTarget, match="control characters"):
            validate("/some/path\n/injected")

    def test_rejects_del_control_character(self):
        with pytest.raises(InvalidTarget, match="control characters"):
            validate("/some/path\x7f")

    def test_rejects_null_control_character(self):
        with pytest.raises(InvalidTarget, match="control characters"):
            validate("/some\x00path")

    def test_rejects_nonexistent_path(self, tmp_path):
        with pytest.raises(InvalidTarget, match="not a directory"):
            validate(str(tmp_path / "does-not-exist"))

    def test_rejects_file_not_directory(self, tmp_path):
        f = tmp_path / "afile"
        f.write_text("x")
        with pytest.raises(InvalidTarget, match="not a directory"):
            validate(str(f))

    def test_rejects_missing_venv_entry_point(self, tmp_path):
        checkout = tmp_path / "co"
        checkout.mkdir()
        (checkout / "src" / "kiro_crew").mkdir(parents=True)
        with pytest.raises(InvalidTarget, match="no .* in its .venv"):
            validate(str(checkout))

    def test_rejects_non_executable_entry_point(self, tmp_path):
        checkout = tmp_path / "co"
        checkout.mkdir()
        (checkout / "src" / "kiro_crew").mkdir(parents=True)
        kcbin = _place_entry_point(checkout, mode=0o644)
        if os.access(kcbin, os.X_OK):
            # Windows reports every existing file as executable, so the state
            # this asserts on cannot be constructed there.
            pytest.skip("platform cannot produce a non-executable file")
        with pytest.raises(InvalidTarget, match="not executable"):
            validate(str(checkout))

    def test_rejects_directory_lacking_src_kiro_crew(self, tmp_path):
        checkout = tmp_path / "co"
        checkout.mkdir()
        _place_entry_point(checkout)
        with pytest.raises(InvalidTarget, match="no src/kiro_crew"):
            validate(str(checkout))

    def test_happy_path_resolves_symlink(self, tmp_path):
        """A valid checkout with a symlink or '..' is returned resolved."""
        checkout = _make_valid_checkout(tmp_path)
        # Reference through a symlink
        link = tmp_path / "link-to-co"
        try:
            link.symlink_to(checkout)
        except OSError:
            pytest.skip("platform refuses symlink creation for this user")
        result = validate(str(link))
        assert result == checkout.resolve()
        assert result.is_absolute()
        # No symlink component remains
        assert not result.is_symlink()

    def test_happy_path_resolves_dotdot(self, tmp_path):
        """A path with '..' segments is normalised."""
        checkout = _make_valid_checkout(tmp_path)
        ref_via_dotdot = str(checkout) + "/src/../src/.."
        result = validate(ref_via_dotdot)
        assert result == checkout.resolve()

    def test_rejection_messages_are_distinct(self, tmp_path):
        """Every rejection reason produces a different message substring."""
        cases: list[tuple[str, str]] = [
            ("", "empty"),
            ("/x\n", "control characters"),
            (str(tmp_path / "nope"), "not a directory"),
        ]
        messages = set()
        for raw, _ in cases:
            try:
                validate(raw)
            except InvalidTarget as exc:
                messages.add(str(exc))
        # All messages are unique
        assert len(messages) == len(cases)


# ─── read_target_reason() / read_target() ──────────────────────────────────


class TestReadTargetReason:
    def test_absent_file_returns_none_none(self, tmp_path):
        target, reason = read_target_reason()
        assert target is None
        assert reason is None

    def test_unreadable_file_returns_none_with_reason(self, tmp_path):
        path = pointer_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        path.chmod(0o000)
        if os.access(path, os.R_OK):
            # Running as root or on a filesystem ignoring perms
            pytest.skip("platform cannot produce an unreadable file")
        target, reason = read_target_reason()
        assert target is None
        assert reason is not None
        assert "could not be read" in reason

    def test_undecodable_bytes_return_none_with_reason(self, tmp_path):
        """Undecodable bytes are ignored, not raised.

        UnicodeDecodeError is a ValueError, not an OSError, so an except-OSError
        arm alone lets it escape the startup bootstrap and crash the gateway on
        every boot for as long as the pointer sits there.
        """
        path = pointer_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe not utf-8 \x80")
        target, reason = read_target_reason()
        assert target is None
        assert reason is not None
        assert "could not be read" in reason

    def test_undecodable_bytes_do_not_break_the_bootstrap(self, tmp_path, monkeypatch):
        """The startup path stays fail-safe: it returns instead of raising."""
        path = pointer_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe not utf-8 \x80")
        execs: list = []
        monkeypatch.setattr(os, "execve", lambda *a, **k: execs.append(a))
        maybe_reexec(["gateway"])  # must not raise
        assert execs == []

    def test_invalid_json_returns_none_with_reason(self, tmp_path):
        path = pointer_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json {{{")
        target, reason = read_target_reason()
        assert target is None
        assert reason is not None
        assert "not valid JSON" in reason

    def test_json_list_returns_none_with_reason(self, tmp_path):
        path = pointer_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('["a", "b"]')
        target, reason = read_target_reason()
        assert target is None
        assert reason is not None
        assert "not a JSON object" in reason

    def test_object_missing_checkout_key(self, tmp_path):
        path = pointer_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"other": "value"}')
        target, reason = read_target_reason()
        assert target is None
        assert "no 'checkout' string" in reason

    def test_object_with_non_string_checkout(self, tmp_path):
        path = pointer_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"checkout": 42}')
        target, reason = read_target_reason()
        assert target is None
        assert "no 'checkout' string" in reason

    def test_valid_pointer_returns_path(self, tmp_path):
        checkout = _make_valid_checkout(tmp_path)
        path = pointer_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checkout": str(checkout)}))
        target, reason = read_target_reason()
        assert target == checkout.resolve()
        assert reason is None

    def test_read_target_mirrors_path_only(self, tmp_path):
        checkout = _make_valid_checkout(tmp_path)
        path = pointer_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checkout": str(checkout)}))
        assert read_target() == checkout.resolve()

    def test_read_target_returns_none_when_absent(self):
        assert read_target() is None


# ─── write_target() ────────────────────────────────────────────────────────


class TestWriteTarget:
    def test_validates_before_writing(self, tmp_path):
        """An invalid target raises AND leaves no file behind."""
        with pytest.raises(InvalidTarget):
            write_target("/nonexistent/path")
        assert not pointer_path().exists()

    def test_writes_owner_only_permissions(self, tmp_path):
        """The pointer is owner-only however the platform expresses that."""
        checkout = _make_valid_checkout(tmp_path)
        write_target(checkout)
        if os.name != "posix":
            # Windows has no POSIX bits: write_target applies an owner-only DACL
            # through restrict_to_owner, which the next test pins directly.
            pytest.skip("POSIX permission bits are not meaningful here")
        assert pointer_path().stat().st_mode & 0o777 == _MODE

    def test_applies_owner_only_lockdown(self, tmp_path, monkeypatch):
        """restrict_to_owner is called on the written pointer.

        This is the only owner-only mechanism on Windows, where atomic_write's
        mode argument is a no-op, so it must be asserted independently of the
        POSIX bit check above.
        """
        seen: list = []
        monkeypatch.setattr(
            "kiro_crew.service.live_target.platform_compat.restrict_to_owner",
            lambda path: seen.append(Path(path)),
        )
        checkout = _make_valid_checkout(tmp_path)
        write_target(checkout)
        assert seen == [pointer_path()]

    def test_round_trips_through_read_target(self, tmp_path):
        checkout = _make_valid_checkout(tmp_path)
        resolved = write_target(checkout)
        assert read_target() == resolved

    def test_returns_resolved_path(self, tmp_path):
        checkout = _make_valid_checkout(tmp_path)
        link = tmp_path / "symlink-co"
        try:
            link.symlink_to(checkout)
        except OSError:
            pytest.skip("platform refuses symlink creation for this user")
        resolved = write_target(link)
        assert resolved == checkout.resolve()


# ─── snapshot() / restore() ────────────────────────────────────────────────


class TestSnapshotRestore:
    def test_absent_file_returns_none(self):
        assert snapshot() is None

    def test_restore_none_deletes_file(self, tmp_path):
        path = pointer_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("anything")
        result = restore(None)
        assert result is True
        assert not path.exists()

    def test_restore_text_rewrites_byte_for_byte(self, tmp_path):
        prior = '{"checkout": "/old/path"}\n'
        path = pointer_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        result = restore(prior)
        assert result is True
        assert path.read_text() == prior

    def test_snapshot_of_unreadable_file_propagates_oserror(self, tmp_path):
        """Load-bearing distinction: unreadable != absent."""
        path = pointer_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content")
        path.chmod(0o000)
        if os.access(path, os.R_OK):
            pytest.skip("platform cannot produce an unreadable file")
        with pytest.raises(OSError):
            snapshot()

    def test_restore_reapplies_the_owner_only_dacl(self, tmp_path, monkeypatch):
        """A rollback must not be the step that widens access to the pointer.

        atomic_write's mode is a POSIX bit and a no-op on Windows, so without an
        explicit restrict_to_owner the restored pointer -- a code-execution input
        read at every startup -- comes back inheriting the directory ACL, and on
        a shared data home another local account could redirect it.
        """
        hardened: list = []
        monkeypatch.setattr(
            "kiro_crew.service.live_target.platform_compat.restrict_to_owner", lambda path: hardened.append(path))
        path = pointer_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        assert restore('{"checkout": "/old/path"}\n') is True

        assert hardened == [path], "restore must harden the file it wrote"

    def test_restore_none_does_not_harden_a_deleted_pointer(self, tmp_path, monkeypatch):
        """Deleting leaves no file, so there is nothing to apply a DACL to."""
        hardened: list = []
        monkeypatch.setattr(
            "kiro_crew.service.live_target.platform_compat.restrict_to_owner", lambda path: hardened.append(path))
        path = pointer_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("anything")

        assert restore(None) is True

        assert not path.exists()
        assert hardened == []

    def test_restore_returns_false_when_hardening_fails(self, tmp_path, monkeypatch):
        """A partial rollback reports False so the caller can warn the operator."""
        def boom(_path):
            raise OSError(5, "icacls failed")

        monkeypatch.setattr("kiro_crew.service.live_target.platform_compat.restrict_to_owner", boom)
        path = pointer_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        assert restore('{"checkout": "/old/path"}\n') is False

    def test_restore_returns_false_on_oserror(self, tmp_path, monkeypatch):
        """Best-effort: returns False rather than raising."""
        # Parent is a FILE, so both the mkdir and the write fail on every
        # platform — an absolute path like /proc/... is creatable on Windows.
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")
        monkeypatch.setattr(
            "kiro_crew.service.live_target.pointer_path",
            lambda: blocker / "sub" / "file",
        )
        result = restore("some content")
        assert result is False

    def test_snapshot_round_trips(self, tmp_path):
        checkout = _make_valid_checkout(tmp_path)
        write_target(checkout)
        prior = snapshot()
        assert prior is not None
        # Clobber
        pointer_path().write_text("junk")
        restore(prior)
        assert pointer_path().read_text() == prior


# ─── maybe_reexec() ───────────────────────────────────────────────────────


class TestMaybeReexec:
    """The core exec logic. os.execve is always monkeypatched (never actually exec)."""

    def test_returns_without_exec_when_marker_set(self, tmp_path, monkeypatch):
        """EXEC_MARKER in env terminates the chain unconditionally."""
        checkout = _make_valid_checkout(tmp_path)
        write_target(checkout)
        monkeypatch.setenv(EXEC_MARKER, "1")
        mock_execve = MagicMock()
        monkeypatch.setattr(os, "execve", mock_execve)
        maybe_reexec(["gateway"])
        mock_execve.assert_not_called()

    def test_returns_without_exec_when_no_pointer(self, monkeypatch):
        """No pointer file -> stay here."""
        mock_execve = MagicMock()
        monkeypatch.setattr(os, "execve", mock_execve)
        monkeypatch.delenv(EXEC_MARKER, raising=False)
        maybe_reexec(["gateway"])
        mock_execve.assert_not_called()

    def test_returns_without_exec_when_pointer_invalid_and_warns(
        self, tmp_path, monkeypatch
    ):
        """An unusable pointer is ignored with a warning."""
        path = pointer_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checkout": "/nonexistent/path"}))
        monkeypatch.delenv(EXEC_MARKER, raising=False)
        mock_execve = MagicMock()
        monkeypatch.setattr(os, "execve", mock_execve)
        log = MagicMock()
        maybe_reexec(["gateway"], log=log)
        mock_execve.assert_not_called()
        log.warning.assert_called()
        # The warning mentions "ignoring"
        args = log.warning.call_args[0]
        assert "ignoring" in args[0] % args[1:]

    def test_loop_guard_same_image(self, tmp_path, monkeypatch):
        """Returns without exec when the pointer names the running image."""
        checkout = _make_valid_checkout(tmp_path)
        write_target(checkout)
        kcbin = target_bin(checkout.resolve())
        # Arrange sys.argv[0] to be the target's entry point (same realpath)
        monkeypatch.setattr(sys, "argv", [str(kcbin), "gateway"])
        monkeypatch.delenv(EXEC_MARKER, raising=False)
        mock_execve = MagicMock()
        monkeypatch.setattr(os, "execve", mock_execve)
        maybe_reexec(["gateway"])
        mock_execve.assert_not_called()

    def test_exec_path_argv_and_env(self, tmp_path, monkeypatch):
        """On exec: argv is [kcbin, *argv], env has EXEC_MARKER + PROJECT_DIR + PATH."""
        checkout = _make_valid_checkout(tmp_path)
        write_target(checkout)
        resolved = checkout.resolve()
        kcbin = target_bin(resolved)
        # argv[0] must differ from kcbin for the loop guard to pass
        monkeypatch.setattr(sys, "argv", ["/usr/bin/kirocrew", "gateway", "--port", "5476"])
        monkeypatch.delenv(EXEC_MARKER, raising=False)
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        mock_execve = MagicMock()
        monkeypatch.setattr(os, "execve", mock_execve)
        # Also patch chdir to avoid side effects
        monkeypatch.setattr(os, "chdir", MagicMock())
        maybe_reexec(["gateway", "--port", "5476"])
        mock_execve.assert_called_once()
        call_args = mock_execve.call_args[0]
        exec_path = call_args[0]
        exec_argv = call_args[1]
        exec_env = call_args[2]
        assert exec_path == str(kcbin)
        assert exec_argv == [str(kcbin), "gateway", "--port", "5476"]
        assert exec_env[EXEC_MARKER] == "1"
        assert exec_env["KIROCREW_PROJECT_DIR"] == str(resolved)
        # Target venv bin is FIRST on PATH
        path_entries = exec_env["PATH"].split(os.pathsep)
        assert path_entries[0] == str(kcbin.parent)

    def test_execve_oserror_is_swallowed(self, tmp_path, monkeypatch):
        """Fail-safe: OSError from execve does not propagate."""
        checkout = _make_valid_checkout(tmp_path)
        write_target(checkout)
        monkeypatch.setattr(sys, "argv", ["/other/bin/kirocrew", "gateway"])
        monkeypatch.delenv(EXEC_MARKER, raising=False)

        def raise_oserror(*args, **kwargs):
            raise OSError("exec failed")

        monkeypatch.setattr(os, "execve", raise_oserror)
        monkeypatch.setattr(os, "chdir", MagicMock())
        # Must NOT raise — the caller keeps booting
        maybe_reexec(["gateway"])

    def test_chdir_failure_warned_but_exec_still_happens(
        self, tmp_path, monkeypatch
    ):
        """A cwd we cannot enter is warned, not fatal — the exec still fires."""
        checkout = _make_valid_checkout(tmp_path)
        write_target(checkout)
        monkeypatch.setattr(sys, "argv", ["/other/bin/kirocrew", "gateway"])
        monkeypatch.delenv(EXEC_MARKER, raising=False)

        def chdir_fail(path):
            raise OSError("permission denied")

        monkeypatch.setattr(os, "chdir", chdir_fail)
        mock_execve = MagicMock()
        monkeypatch.setattr(os, "execve", mock_execve)
        log = MagicMock()
        maybe_reexec(["gateway"], log=log)
        # The exec was still attempted despite chdir failure
        mock_execve.assert_called_once()
        # A warning was emitted about chdir
        warn_calls = [
            c for c in log.warning.call_args_list
            if "chdir" in (c[0][0] % c[0][1:])
        ]
        assert len(warn_calls) >= 1


# ─── Security ratchet ──────────────────────────────────────────────────────


class TestSecurityRatchet:
    """The pointer file is keystone-fenced — a security property, not an impl detail."""

    def test_live_target_json_in_sensitive_home_dirs_both_prefixes(self):
        """live_target.json must be protected under both crew-home prefixes."""
        from kiro_crew import security

        dirs = security.sensitive_home_dirs()
        prefixes = security.crew_home_prefixes()
        # Must be at least .kiro/crew and .kirocrew
        assert len(prefixes) >= 2
        for prefix in prefixes:
            expected = f"{prefix}/{_FILENAME}"
            assert expected in dirs, (
                f"{expected!r} missing from sensitive_home_dirs() — "
                f"the live-target pointer is unprotected under the {prefix!r} prefix"
            )
