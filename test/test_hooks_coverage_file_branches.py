"""Coverage of the remaining branches in ``kiro_crew.hooks``' file helpers.

A symlink swapped in after canonicalization, the opened inode's real path
re-screened, a failed stage write, access controls carried across the atomic
replace, the by-name replace where dir-fd APIs are absent, the dir-fd checks,
the verified compare-and-swap replace, and the refusal of a path the platform
cannot represent. Shared helpers live in ``hooks_coverage_helpers.py``.
"""

from __future__ import annotations

import os
import stat as _stat
import sys

import pytest
from hooks_coverage_helpers import (
    _IS_WINDOWS,
    _same,
    _staged_sibling,
    _write,
)

from kiro_crew import hooks as hooks_mod
from kiro_crew.hooks import (
    safe_copy_file_nolink,
    safe_read_file,
    safe_read_file_bytes_nolink,
    safe_write_file_nolink,
    validate_file_path,
    verified_replace_file_nolink,
)

# ── extra branches in the file helpers ──


class TestSafeReadFileSymlinkRace:
    def test_eloop_after_canonicalization_is_refused(self, tmp_path, monkeypatch):
        import errno as _errno

        from kiro_crew import platform_compat as _pc

        f = _write(tmp_path / "a.txt", "x")
        real_no_reparse = _pc.open_file_no_reparse

        # The seam is open_file_no_reparse, which is what carries the
        # final-component refusal on both platforms; os.open is only its POSIX arm.
        def _eloop(path, *args, **kwargs):
            if isinstance(path, str) and _same(path, str(f)):
                raise OSError(_errno.ELOOP, "swapped for a symlink")
            return real_no_reparse(path, *args, **kwargs)

        monkeypatch.setattr(_pc, "open_file_no_reparse", _eloop)
        with pytest.raises(PermissionError, match="refusing to follow symlink"):
            safe_read_file(str(f))


class TestFdPathSensitivityChecks:
    """The opened inode's real path is re-screened, not just the input name."""

    @staticmethod
    def _sensitive_on_second_call(monkeypatch):
        calls = {"n": 0}

        def _probe(path, *args, **kwargs):
            calls["n"] += 1
            # First call is validate_file_path's screening of the input name;
            # the next is the check against the OPENED descriptor's real path.
            return calls["n"] >= 2

        monkeypatch.setattr(hooks_mod, "is_sensitive_path", _probe)
        return calls

    def test_read_nolink_refuses_a_sensitive_opened_inode(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        root.mkdir()
        f = _write(root / "a.txt", "x")
        self._sensitive_on_second_call(monkeypatch)
        assert safe_read_file_bytes_nolink(str(f), within_root=str(root)) is None

    def test_copy_refuses_a_sensitive_opened_inode(self, tmp_path, monkeypatch):
        src = _write(tmp_path / "src.bin", "x")
        dest = tmp_path / "dest"
        dest.mkdir()
        self._sensitive_on_second_call(monkeypatch)
        assert safe_copy_file_nolink(str(src), str(dest)) is None
        assert list(dest.iterdir()) == []

    def test_write_refuses_a_sensitive_opened_inode(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        root.mkdir()
        f = _write(root / "a.txt", "old")
        self._sensitive_on_second_call(monkeypatch)
        assert safe_write_file_nolink(str(f), "new", within_root=str(root)) is False
        assert f.read_text(encoding="utf-8") == "old"


class TestSafeCopyFileNolinkStagingFailure:
    def test_a_failed_stage_write_cleans_up_and_returns_none(self, tmp_path, monkeypatch):
        import tempfile as _tempfile

        src = _write(tmp_path / "src.bin", "payload")
        dest = tmp_path / "dest"
        dest.mkdir()
        real_mkstemp = _tempfile.mkstemp
        staged: list[str] = []

        def _stale_fd(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            staged.append(path)
            # A closed descriptor makes the very next os.write raise EBADF,
            # which is the OSError the cleanup branch exists for.
            os.close(fd)
            return fd, path

        monkeypatch.setattr(_tempfile, "mkstemp", _stale_fd)
        assert safe_copy_file_nolink(str(src), str(dest)) is None
        assert staged, "the staging file was never created"
        # The partial copy must not be left behind for a downstream reader.
        assert list(dest.iterdir()) == []


class TestSafeWriteFileNolinkXattrs:
    """Access controls must survive the atomic replace, or the write refuses."""

    def _require_xattrs(self):
        if not all(hasattr(os, a) for a in ("listxattr", "getxattr", "setxattr")):
            pytest.skip("this platform has no extended-attribute API")

    def test_an_unreadable_xattr_set_refuses_the_write(self, tmp_path, monkeypatch):
        self._require_xattrs()
        f = _write(tmp_path / "a.txt", "old")

        def _eperm(fd):
            raise OSError(1, "operation not permitted")

        monkeypatch.setattr(os, "listxattr", _eperm)
        # Not knowing what would be dropped is a refusal, not a best effort.
        assert safe_write_file_nolink(str(f), "new") is False
        assert f.read_text(encoding="utf-8") == "old"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]

    def test_a_filesystem_without_xattrs_is_not_an_error(self, tmp_path, monkeypatch):
        self._require_xattrs()
        import errno as _errno

        f = _write(tmp_path / "a.txt", "old")

        def _unsupported(fd):
            raise OSError(_errno.ENOTSUP, "not supported")

        monkeypatch.setattr(os, "listxattr", _unsupported)
        # Nothing on the source to lose, so the write proceeds.
        assert safe_write_file_nolink(str(f), "new") is True
        assert f.read_text(encoding="utf-8") == "new"

    def test_an_uncopyable_access_control_attr_refuses_the_write(self, tmp_path, monkeypatch):
        self._require_xattrs()
        f = _write(tmp_path / "a.txt", "old")
        monkeypatch.setattr(os, "listxattr", lambda fd: ["system.posix_acl_access"])
        monkeypatch.setattr(os, "getxattr", lambda fd, attr: b"acl")

        def _refuse(fd, attr, value):
            raise OSError(1, "cannot set")

        monkeypatch.setattr(os, "setxattr", _refuse)
        assert safe_write_file_nolink(str(f), "new") is False
        assert f.read_text(encoding="utf-8") == "old"

    def test_a_privileged_attr_is_not_carried_and_does_not_refuse(self, tmp_path, monkeypatch):
        """File capabilities and integrity signatures must not reach the copy.

        ``safe_write_file_nolink`` also installs a fresh inode holding
        caller-supplied content, so it shares ``atomic_write``'s allowlist:
        replaying ``security.capability`` there would attach the old file's
        privileges to the new bytes, and ``security.ima``/``security.evm`` are
        signatures over bytes that are gone. The ACL beside them is still
        carried, so this is a filter and not a blanket stop.
        """
        self._require_xattrs()
        f = _write(tmp_path / "a.txt", "old")
        present = [
            "security.capability",
            "security.ima",
            "security.evm",
            "security.selinux",
            "system.posix_acl_access",
        ]
        monkeypatch.setattr(os, "listxattr", lambda fd: list(present))
        monkeypatch.setattr(os, "getxattr", lambda fd, attr: attr.encode())
        written: list[str] = []
        monkeypatch.setattr(os, "setxattr", lambda fd, attr, value: written.append(attr))

        assert safe_write_file_nolink(str(f), "new") is True
        assert f.read_text(encoding="utf-8") == "new"
        assert written == ["system.posix_acl_access"]

    def test_an_uncopyable_informational_attr_is_best_effort(self, tmp_path, monkeypatch):
        self._require_xattrs()
        f = _write(tmp_path / "a.txt", "old")
        monkeypatch.setattr(os, "listxattr", lambda fd: ["user.comment"])
        monkeypatch.setattr(os, "getxattr", lambda fd, attr: b"tag")

        def _refuse(fd, attr, value):
            raise OSError(1, "cannot set")

        monkeypatch.setattr(os, "setxattr", _refuse)
        # Losing a tag must not fail every save on a filesystem that cannot
        # store one.
        assert safe_write_file_nolink(str(f), "new") is True
        assert f.read_text(encoding="utf-8") == "new"


class TestSafeWriteFileNolinkWithoutDirFd:
    """The by-name replace branch taken where the POSIX dir-fd APIs are absent.

    Windows has no ``O_DIRECTORY`` / ``dir_fd`` support, so this is the branch it
    always uses. Clearing ``os.supports_dir_fd`` exercises the same code here.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture, moved verbatim; the one undo() in its class runs after the write the patch is for
    def _no_dir_fd(self, monkeypatch):
        monkeypatch.setattr(os, "supports_dir_fd", set())

    def test_the_staged_payload_is_still_renamed_over_the_target(self, tmp_path):
        f = _write(tmp_path / "a.txt", "old")
        assert safe_write_file_nolink(str(f), "new") is True
        assert f.read_text(encoding="utf-8") == "new"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]

    def test_within_root_still_refuses_an_escaping_parent(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = _write(tmp_path / "outside.txt", "old")
        assert safe_write_file_nolink(str(outside), "new", within_root=str(root)) is False
        assert outside.read_text(encoding="utf-8") == "old"

    def test_within_root_accepts_a_contained_parent(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        f = _write(root / "a.txt", "old")
        assert safe_write_file_nolink(str(f), "new", within_root=str(root)) is True
        assert f.read_text(encoding="utf-8") == "new"

    def test_a_vanished_target_refuses_before_the_rename(self, tmp_path, monkeypatch):
        f = _write(tmp_path / "a.txt", "old")
        real_stat = os.stat
        state = {"fired": False}

        def _vanish_on_the_recheck(path, *args, **kwargs):
            if (
                isinstance(path, str)
                and os.path.basename(path) == f.name
                and _staged_sibling(tmp_path, f.name)
            ):
                state["fired"] = True
                raise FileNotFoundError("target moved")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", _vanish_on_the_recheck)
        try:
            assert safe_write_file_nolink(str(f), "new") is False
        finally:
            # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo that restores os.stat before the file is read back, moved verbatim
            monkeypatch.undo()
        assert state["fired"] is True
        assert f.read_text(encoding="utf-8") == "old"
        # The staged sibling is unlinked by name on the cleanup path.
        assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]


class TestSafeWriteFileNolinkDirFdChecks:
    def test_an_unverifiable_parent_fails_closed(self, tmp_path, monkeypatch):
        if not (
            getattr(os, "O_DIRECTORY", 0)
            and os.open in getattr(os, "supports_dir_fd", set())
            and os.rename in getattr(os, "supports_dir_fd", set())
        ):
            pytest.skip("this platform has no directory-fd pinning")
        root = tmp_path / "root"
        root.mkdir()
        f = _write(root / "a.txt", "old")
        real_fd_path = hooks_mod._fd_real_path

        def _files_only(fd):
            got = real_fd_path(fd)
            # The file's own check must still pass; only the DIRECTORY handle
            # becomes unverifiable.
            if got and os.path.isdir(got):
                return None
            return got

        monkeypatch.setattr(hooks_mod, "_fd_real_path", _files_only)
        assert safe_write_file_nolink(str(f), "new", within_root=str(root)) is False
        assert f.read_text(encoding="utf-8") == "old"

    def test_an_unstattable_pinned_target_refuses(self, tmp_path, monkeypatch):
        if not (
            getattr(os, "O_DIRECTORY", 0)
            and os.open in getattr(os, "supports_dir_fd", set())
            and os.rename in getattr(os, "supports_dir_fd", set())
        ):
            pytest.skip("this platform has no directory-fd pinning")
        f = _write(tmp_path / "a.txt", "old")
        real_stat = os.stat

        def _no_stat_through_dir_fd(path, *args, **kwargs):
            if "dir_fd" in kwargs and path == f.name:
                raise FileNotFoundError("gone from the pinned parent")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", _no_stat_through_dir_fd)
        try:
            assert safe_write_file_nolink(str(f), "new") is False
        finally:
            # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo that restores os.stat before the file is read back, moved verbatim
            monkeypatch.undo()
        assert f.read_text(encoding="utf-8") == "old"


class TestVerifiedReplaceFileNolink:
    """Compare-and-swap through one descriptor: verify and replace share a
    single name resolution, and every post-verify concurrency change answers
    conflict — the newer file wins, never the stale edit."""

    @staticmethod
    def _sha(text: str) -> str:
        import hashlib

        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def test_matching_base_replaces(self, tmp_path):
        f = _write(tmp_path / "a.txt", "BEFORE")
        assert (
            verified_replace_file_nolink(str(f), "AFTER", self._sha("BEFORE"), max_bytes=1_000_000)
            == "ok"
        )
        assert f.read_text(encoding="utf-8") == "AFTER"

    def test_stale_base_is_a_conflict_and_leaves_the_file(self, tmp_path):
        f = _write(tmp_path / "a.txt", "THEIRS")
        out = verified_replace_file_nolink(
            str(f), "MINE", self._sha("WHAT I SAW"), max_bytes=1_000_000
        )
        assert out == "conflict"
        assert f.read_text(encoding="utf-8") == "THEIRS"

    def test_file_grown_past_cap_is_too_large(self, tmp_path):
        f = _write(tmp_path / "a.txt", "x" * 100)
        out = verified_replace_file_nolink(str(f), "new", self._sha("x" * 100), max_bytes=50)
        assert out == "too_large"
        assert f.read_text(encoding="utf-8") == "x" * 100

    def test_missing_file_is_refused(self, tmp_path):
        out = verified_replace_file_nolink(
            str(tmp_path / "gone.txt"), "x", self._sha("anything"), max_bytes=1_000_000
        )
        assert out == "refused"
        assert not (tmp_path / "gone.txt").exists()

    def test_within_root_containment_still_applies(self, tmp_path):
        outside = _write(tmp_path / "outside.txt", "SECRET")
        root = tmp_path / "root"
        root.mkdir()
        out = verified_replace_file_nolink(
            str(outside), "clobber", self._sha("SECRET"), within_root=str(root), max_bytes=1_000_000
        )
        assert out == "refused"
        assert outside.read_text(encoding="utf-8") == "SECRET"

    def test_external_atomic_save_after_verify_is_a_conflict(self, tmp_path, monkeypatch):
        """THE finding class: an external editor completes its own atomic save
        (new inode) in the window between hash verification and the rename.
        The identity re-checks anchored to the verified descriptor detect the
        swap and answer conflict; the external editor's newer content survives.
        The injection point is the staging write (os.fsync), which sits
        strictly after verification and strictly before the rename."""
        f = _write(tmp_path / "a.txt", "BASE")
        real_fsync = os.fsync
        fired = {"done": False}

        def racing_fsync(fd):
            if not fired["done"]:
                fired["done"] = True
                # An editor's atomic save: stage + replace = NEW inode.
                side = tmp_path / ".editor-save.tmp"
                side.write_text("NEWER FROM OUTSIDE", encoding="utf-8")
                os.replace(side, f)
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", racing_fsync)
        out = verified_replace_file_nolink(
            str(f), "STALE EDIT", self._sha("BASE"), max_bytes=1_000_000
        )
        # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo that restores os.fsync before the outcome is checked, moved verbatim
        monkeypatch.undo()
        assert fired["done"], "the race never ran — the test would be vacuous"
        assert out == "conflict"
        assert f.read_text(encoding="utf-8") == "NEWER FROM OUTSIDE"

    def test_external_in_place_write_after_verify_is_a_conflict(self, tmp_path, monkeypatch):
        """Same window, same-inode variant: an in-place rewrite keeps (dev,ino)
        so the identity check cannot see it — the mtime/size re-check does."""
        f = _write(tmp_path / "a.txt", "BASE")
        real_fsync = os.fsync
        fired = {"done": False}

        def racing_fsync(fd):
            if not fired["done"]:
                fired["done"] = True
                # In-place write through the SAME inode, different length.
                with open(f, "r+b") as fh:
                    fh.write(b"NEWER IN PLACE, LONGER THAN BASE")
                    fh.truncate()
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", racing_fsync)
        out = verified_replace_file_nolink(
            str(f), "STALE EDIT", self._sha("BASE"), max_bytes=1_000_000
        )
        # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo that restores os.fsync before the outcome is checked, moved verbatim
        monkeypatch.undo()
        assert fired["done"], "the race never ran — the test would be vacuous"
        assert out == "conflict"
        assert f.read_text(encoding="utf-8") == "NEWER IN PLACE, LONGER THAN BASE"

    def test_same_length_in_place_rewrite_is_still_a_conflict(self, tmp_path, monkeypatch):
        """The mtime half of the freshness pair, exercised alone: a rewrite of
        the SAME length keeps st_size, so only st_mtime_ns can catch it. The
        mtime is advanced explicitly because a sub-timestamp-tick rewrite is
        stat-invisible — the documented residue this check cannot close."""
        f = _write(tmp_path / "a.txt", "BASE")
        real_fsync = os.fsync
        fired = {"done": False}

        def racing_fsync(fd):
            if not fired["done"]:
                fired["done"] = True
                with open(f, "r+b") as fh:
                    fh.write(b"EGAB")  # same length as BASE
                    fh.truncate()
                # An in-place rewrite landing within one filesystem timestamp
                # tick leaves NO stat-visible trace (same inode, size, and
                # mtime_ns) — that sub-tick case is the documented unclosable
                # residue, verified empirically on this filesystem. Advance
                # the mtime explicitly so this test pins the mtime half of
                # the comparison, which real editor saves (milliseconds to
                # minutes later) always trip.
                st = os.stat(f)
                os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", racing_fsync)
        out = verified_replace_file_nolink(
            str(f), "STALE EDIT", self._sha("BASE"), max_bytes=1_000_000
        )
        # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo that restores os.fsync before the outcome is checked, moved verbatim
        monkeypatch.undo()
        assert fired["done"], "the race never ran — the test would be vacuous"
        assert out == "conflict"
        assert f.read_text(encoding="utf-8") == "EGAB"

    def test_mode_survives_a_verified_replace(self, tmp_path):
        if _IS_WINDOWS:
            pytest.skip("POSIX mode bits are not meaningful on Windows")
        f = _write(tmp_path / "a.txt", "BEFORE")
        os.chmod(f, 0o600)
        assert (
            verified_replace_file_nolink(str(f), "AFTER", self._sha("BEFORE"), max_bytes=1_000_000)
            == "ok"
        )
        assert _stat.S_IMODE(os.stat(f).st_mode) == 0o600

    def test_a_malformed_base_hash_is_refused_not_skipped(self, tmp_path):
        """Deny-by-default: an unverifiable base refuses — it must never skip
        verification and proceed (fail-open is the lost-update class itself)."""
        f = _write(tmp_path / "a.txt", "BASE")
        for bad in ("", "not-hex", "ABCDEF" + "0" * 58, None):
            out = verified_replace_file_nolink(
                str(f), "NEW", bad, max_bytes=1_000_000  # type: ignore[arg-type]
            )
            assert out == "refused", f"base_hash={bad!r}"
            assert f.read_text(encoding="utf-8") == "BASE"

    def test_wrapper_contract_unchanged(self, tmp_path):
        """safe_write_file_nolink is the engine's no-verify entry point: no
        base hash, no conflict vocabulary, bool contract preserved."""
        f = _write(tmp_path / "a.txt", "old")
        assert safe_write_file_nolink(str(f), "new") is True
        assert f.read_text(encoding="utf-8") == "new"


class TestValidateFilePathRepresentability:
    """Only an UNREPRESENTABLE path is refused here -- deliberately not more.

    This is a shared chokepoint: `safe_read_file_bytes_nolink` routes through it,
    and its callers include diagnostics that enumerate a file whose name holds a
    control character in order to report on it. A broader refusal here turns such
    a report into "could not be compared" and suppresses the finding, so the
    control-character class belongs to the boundary that receives the path
    (`_validate_dashboard_path`) rather than to this function.

    Every caller treats None as the refusal, so a string that got past this point
    surfaced from the dashboard handlers as an uncaught HTTP 500 rather than a 400.
    """

    def test_refuses_an_embedded_nul(self):
        # realpath raises ValueError on it, and no file can be named with one, so
        # refusing it costs no real name.
        assert validate_file_path("/tmp/a\x00b") is None

    @pytest.mark.parametrize(
        "raw",
        [
            "/tmp/(a\x1b[2Jb)",
            "/tmp/a\x9bb",
            "/tmp/a\rb",
            "/tmp/a\tb",
        ],
    )
    def test_does_not_refuse_another_control_character(self, raw):
        # An agent-writeable directory can hold such a name, and a diagnostic
        # enumerates it to report divergence, escaping the name for display. If
        # this function refused it, that report would degrade to "could not be
        # compared" and the divergence would go unreported -- a suppressed
        # finding, which is worse than the display hazard it would be guarding.
        assert validate_file_path(raw) is not None

    def test_refuses_a_path_the_platform_cannot_encode(self):
        """A lone surrogate: refused where the platform's own encoder refuses it.

        Which answer is correct here is a PLATFORM FACT, not a policy choice, and
        that is the point of asking the encoder rather than listing characters.
        On POSIX the handler is surrogateescape, which cannot carry U+D800, so
        realpath would raise and the path is refused. On Windows it is
        surrogatepass, which carries it -- and an unpaired surrogate can appear in
        a legal NTFS name, so refusing it there would reject a real file, which is
        the harm this gate exists to avoid.

        So the expectation is derived from the same encoder the code consults,
        rather than hardcoded: a fixed answer here would assert POSIX behaviour on
        Windows and fail against correct code.
        """
        raw = "/tmp/\ud800x"
        try:
            raw.encode(sys.getfilesystemencoding(), sys.getfilesystemencodeerrors())
        except (UnicodeError, ValueError):
            assert validate_file_path(raw) is None
        else:
            assert validate_file_path(raw) is not None

    @pytest.mark.parametrize(
        "name",
        [
            # Canonically composed and canonically DECOMPOSED spellings of the
            # same name. macOS stores the decomposed form, so refusing every
            # string a sanitizer would alter would reject real files there.
            "caf\u00e9.md",
            "cafe\u0301.md",
            # A name may legally end in a space on POSIX.
            "trailing ",
            # The reserved-character shape the schema gate was widened for.
            "One on one (2026) #1.md",
        ],
    )
    def test_accepts_a_representable_name(self, tmp_path, name):
        target = tmp_path / name
        target.write_text("x", encoding="utf-8")
        assert validate_file_path(str(target)) is not None

    def test_accepts_a_surrogate_escaped_raw_byte_name(self, tmp_path):
        # A filename holding bytes that are not valid UTF-8 arrives
        # surrogate-escaped. The platform's own filesystem error handler
        # round-trips that range, so it is a real file rather than a crash, and
        # must not be refused alongside the lone surrogate above. Reading the
        # handler from sys rather than hardcoding one is what keeps this true on
        # Windows, where it is surrogatepass and an unpaired surrogate can appear
        # in a legal NTFS name.
        target = tmp_path / "\udc80raw.md"
        try:
            target.write_text("x", encoding="utf-8")
        except (OSError, UnicodeEncodeError):
            pytest.skip("filesystem refuses non-UTF-8 names")
        assert validate_file_path(str(target)) is not None
