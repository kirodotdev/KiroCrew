"""Owner-only permissions: the POSIX mode bits and the Windows DACL lockdown.

``restrict_to_owner`` and ``make_owner_only_dir`` on both platforms, the Windows
DACL decision exercised on Linux by faking ``IS_WINDOWS`` and the writer, the
predicate over a re-read ``icacls`` dump, the probe-before-write that keeps the
lockdown idempotent and the conservative ``owner_only_dacl_matches``, the chmod
shims, the SID and user-id lookups that never spawn, and whether a path's
volume is remote. A topic of the ``test_platform_compat`` family; see
``test_platform_compat.py``.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import stat
import subprocess
import tempfile

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew import windows_acl

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_platform_compat family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestChmodShims:
    def test_chmod_safe_noop_on_missing_is_safe(self):
        # chmod_safe logs + swallows on failure (POSIX) and is a no-op on
        # Windows — a non-existent path must not raise either way.
        pc.chmod_safe(os.path.join(tempfile.gettempdir(), "no-such-mc-file"), 0o600)


class TestRestrictToOwnerArgvOnLinux:
    """Regression guard for the Windows owner-only DACL, exercised on Linux.

    Runs on the Linux CI fleet by monkeypatching IS_WINDOWS + the DACL writer --
    the decision of WHICH principals to grant, and whether the grants are
    inheritable, is platform-independent code, and without this it is only
    exercised on the author's manual Windows E2E (skipif-Windows tests don't run
    on AL2). A regression that drops the S-1-3-4 grant or the invoking-user grant
    silently reopens the parent-inherited-DACL gap.

    The lockdown goes through ``windows_acl.apply_owner_only`` in-process, so the
    observable is that call's
    arguments instead -- the same seam, one layer down, and still the only thing
    visible off Windows (NTFS reports 0o666 for any file regardless of its DACL,
    so no mode assertion can substitute).
    """

    @staticmethod
    def _capture(monkeypatch, sid="S-1-5-21-1-2-3-1000"):
        """Force the Windows branch and record the DACL write instead of doing it."""
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        # Reset the SID memo so the monkeypatched stub wins. The lockdown reads
        # current_user_sid, which is token-only and cannot spawn.
        monkeypatch.setattr(pc, "_TOKEN_SID_CACHE", [])
        monkeypatch.setattr(pc, "current_user_sid", lambda: sid)
        calls: list[dict] = []

        def fake_apply(path, *, inherit, sids, **_kw):
            calls.append({"path": os.fspath(path), "inherit": inherit, "sids": tuple(sids)})

        monkeypatch.setattr(pc.windows_acl, "apply_owner_only", fake_apply)
        return calls

    def test_dacl_grants_owner_rights_and_the_invoking_user(self, tmp_path, monkeypatch):
        calls = self._capture(monkeypatch)
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        pc.restrict_to_owner(f)
        assert len(calls) == 1, calls
        assert calls[0]["path"] == os.fspath(f)
        # Bare SIDs: the `*` prefix is icacls argv syntax and the API rejects it.
        assert calls[0]["sids"] == ("S-1-3-4", "S-1-5-21-1-2-3-1000"), calls[0]
        assert not any(s.startswith("*") for s in calls[0]["sids"]), calls[0]

    def test_write_failure_raises_oserror(self, tmp_path, monkeypatch):
        # With a resolvable SID, a failure to apply the DACL still raises OSError
        # so the caller's warn-and-continue handler fires. Complements the
        # None-SID early-raise test below.
        self._capture(monkeypatch, sid="S-1-5-21-9-9-9-9")

        def boom(path, *, inherit, sids, **_kw):
            raise pc.windows_acl.AclWriteFailed("SetNamedSecurityInfoW failed (error 5)")

        monkeypatch.setattr(pc.windows_acl, "apply_owner_only", boom)
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        with pytest.raises(OSError):
            pc.restrict_to_owner(f)

    def test_unreadable_platform_api_raises_oserror(self, tmp_path, monkeypatch):
        # AclUnavailable (the descriptor API could not be loaded at all) must
        # reach the caller as OSError too, not escape as a bare RuntimeError that
        # no caller's handler catches.
        self._capture(monkeypatch, sid="S-1-5-21-9-9-9-9")

        def boom(path, *, inherit, sids, **_kw):
            raise pc.windows_acl.AclUnavailable("cannot load the Windows security API")

        monkeypatch.setattr(pc.windows_acl, "apply_owner_only", boom)
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        with pytest.raises(OSError):
            pc.restrict_to_owner(f)

    def test_none_sid_raises_before_icacls_to_avoid_lockout(self, tmp_path, monkeypatch):
        # When current_user_sid() returns None (the process token read is
        # unavailable), restrict_to_owner MUST refuse to apply a lockdown —
        # granting only S-1-3-4 (Owner Rights) with inheritance stripped
        # locks non-owner users out of their own file (elevated first-run,
        # backup restore, SYSTEM-context service scenarios). Fail-loud with
        # OSError BEFORE touching the DACL; the caller's warn handler fires
        # and the pre-existing DACL is preserved unchanged.
        calls = self._capture(monkeypatch, sid=None)
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        with pytest.raises(OSError) as ei:
            pc.restrict_to_owner(f)
        assert "current user SID" in str(ei.value)
        # The DACL must NOT have been touched — the whole point is to avoid
        # applying a half-configured lockdown.
        assert calls == [], f"no DACL write may happen when the SID is unknown: {calls}"

    def test_directory_grants_are_inheritable(self, tmp_path, monkeypatch):
        # FILE-shaped restrict_to_owner grants are not inheritable: those
        # ACEs apply to the directory alone, so a file created inside an
        # "owner-only" directory gets no explicit ACE and falls back to the
        # creating token's default DACL.
        calls = self._capture(monkeypatch)
        d = tmp_path / "secrets-dir"
        d.mkdir()
        pc.restrict_dir_to_owner(d)
        assert len(calls) == 1, calls
        assert calls[0]["path"] == os.fspath(d)
        # Both grants must propagate to children, or the directory guarantee
        # covers nothing created inside it.
        assert calls[0]["inherit"] is True, calls[0]
        assert calls[0]["sids"] == ("S-1-3-4", "S-1-5-21-1-2-3-1000"), calls[0]

    def test_file_grants_stay_non_inheritable(self, tmp_path, monkeypatch):
        # The other half of the split, asserted negatively: inheritance flags are
        # meaningless on a file, so restrict_to_owner must NOT acquire them when
        # the directory shape does. Without this, "just make both inheritable"
        # reads as a passing simplification.
        calls = self._capture(monkeypatch)
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        pc.restrict_to_owner(f)
        assert len(calls) == 1, calls
        assert calls[0]["inherit"] is False, calls[0]

    def test_owner_rights_is_not_granted_twice(self, tmp_path, monkeypatch):
        # Degenerate case: when the invoking user's SID IS Owner Rights, the two
        # grants collapse to one rather than producing a duplicate ACE.
        calls = self._capture(monkeypatch, sid="S-1-3-4")
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        pc.restrict_to_owner(f)
        assert calls[0]["sids"] == ("S-1-3-4",), calls[0]

    def test_file_helper_warns_when_handed_a_directory(self, tmp_path, monkeypatch, caplog):
        # The misuse guard. The DACL-argument tests cannot see this from the call
        # site, so a directory reaching the file-shaped helper has to be caught
        # here -- it tightens the directory but leaves files created inside on the
        # creating token's default DACL. Warn, not raise: the ACE still applies
        # to the named object, so the lockdown is partial rather than absent.
        #
        # _capture stubs the DACL writer: this test is about the warning, and the
        # real writer refuses off Windows (_load raises AclUnavailable), which
        # would fail this on the POSIX CI runners while passing on Windows.
        self._capture(monkeypatch)
        d = tmp_path / "a-directory"
        d.mkdir()
        with caplog.at_level(logging.WARNING, logger=pc.logger.name):
            pc.restrict_to_owner(d)
        assert any("not inheritable" in r.getMessage() for r in caplog.records), [
            r.getMessage() for r in caplog.records
        ]

    def test_file_helper_stays_quiet_for_a_file(self, tmp_path, monkeypatch, caplog):
        # The guard must not fire on the helper's actual purpose, or every
        # secret-file lockdown would emit a spurious warning.
        self._capture(monkeypatch)
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        with caplog.at_level(logging.WARNING, logger=pc.logger.name):
            pc.restrict_to_owner(f)
        assert not [r for r in caplog.records if "not inheritable" in r.getMessage()]

    def test_directory_shape_uses_0o700_on_posix(self, tmp_path, monkeypatch):
        # The POSIX half of the split: 0o700, not the file helper's 0o600 —
        # a directory without the execute bit is not traversable at all.
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        modes: list[int] = []
        monkeypatch.setattr(pc.os, "chmod", lambda p, m: modes.append(m))
        pc.restrict_dir_to_owner(tmp_path)
        assert modes == [0o700], modes


class TestPathVolumeIsRemote:
    """The Windows half of "which kind of filesystem holds this", on Linux.

    Nothing was established is None, never False: a caller that reads a failed
    query as "local" is the case this tri-state exists to prevent.
    """

    def test_off_windows_the_answer_is_unknown(self, monkeypatch, tmp_path):
        # POSIX callers have their own mount-table source, so the answer here is
        # "nothing established". The branch is named rather than inherited from the
        # host: on Windows this same call reaches a real volume and correctly reports
        # a local one, which is a different fact from the one under test.
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc.path_volume_is_remote(tmp_path) is None

    @pytest.mark.parametrize("verdict", [True, False, None])
    def test_the_windows_volume_verdict_is_passed_through(self, monkeypatch, verdict):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.windows_acl, "volume_is_remote", lambda p: verdict)
        assert pc.path_volume_is_remote("Z:\\kiro") is verdict

    @pytest.mark.parametrize(
        "exc", [windows_acl.AclUnavailable("no api"), OSError("call failed"), ValueError("root")]
    )
    def test_a_failed_query_is_unknown(self, monkeypatch, exc):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)

        def _boom(_path):
            raise exc

        monkeypatch.setattr(pc.windows_acl, "volume_is_remote", _boom)
        assert pc.path_volume_is_remote("Z:\\kiro") is None


class TestChmodShimsApply:
    def test_fchmod_safe_applies_mode_on_posix(self, tmp_path):
        # POSIX: fchmod_safe must actually apply the mode to the open fd. Verify
        # via os.fstat (the assert is POSIX-only; Windows has no perm bits).
        f = tmp_path / "fchmod-apply.txt"
        f.write_text("x")
        fd = os.open(str(f), os.O_RDONLY)
        try:
            pc.fchmod_safe(fd, 0o600)
            if pc.IS_POSIX:
                assert os.fstat(fd).st_mode & 0o777 == 0o600
        finally:
            os.close(fd)

    def test_fchmod_safe_swallows_oserror(self, tmp_path, monkeypatch, caplog):
        # The except branch: os.fchmod raising OSError must be logged + swallowed,
        # never propagated. Force the error since a real fd would just succeed.
        if not pc.IS_POSIX:
            pytest.skip("POSIX os.fchmod branch")
        f = tmp_path / "fchmod-err.txt"
        f.write_text("x")
        fd = os.open(str(f), os.O_RDONLY)

        def boom(*args, **kwargs):
            raise OSError("forced")

        monkeypatch.setattr(pc.os, "fchmod", boom)
        try:
            with caplog.at_level(logging.WARNING, logger=pc.logger.name):
                pc.fchmod_safe(fd, 0o600)  # must NOT raise out
        finally:
            os.close(fd)
        warnings = [r.getMessage() for r in caplog.records if r.name == pc.logger.name]
        assert warnings == [f"Cannot set permissions on fd {fd}"]

    def test_chmod_safe_applies_mode_on_posix(self, tmp_path):
        # POSIX: chmod_safe must apply the mode to the path on disk.
        f = tmp_path / "chmod-apply.txt"
        f.write_text("x")
        pc.chmod_safe(str(f), 0o640)
        if pc.IS_POSIX:
            assert oct(os.stat(str(f)).st_mode & 0o777) == "0o640"

    def test_chmod_safe_swallows_oserror(self, tmp_path, monkeypatch, caplog):
        # The except branch: os.chmod raising OSError is logged + swallowed.
        if not pc.IS_POSIX:
            pytest.skip("POSIX os.chmod branch")
        f = tmp_path / "chmod-err.txt"
        f.write_text("x")

        def boom(*args, **kwargs):
            raise OSError("forced")

        monkeypatch.setattr(pc.os, "chmod", boom)
        with caplog.at_level(logging.WARNING, logger=pc.logger.name):
            pc.chmod_safe(str(f), 0o640)  # must NOT raise out
        warnings = [r.getMessage() for r in caplog.records if r.name == pc.logger.name]
        assert warnings == [f"Cannot set permissions on {f}"]


#: An ACE that icacls prints with a bare ``(I)`` flag is INHERITED, so its presence
#: means ``/inheritance:r`` did not take. Matching the flag rather than a rights token
#: keeps this locale-independent: ``(I)`` is a flag spelling, not a display name.
_INHERITED_ACE_RE = re.compile(r"\(I\)")

#: The Owner Rights principal, in either spelling icacls may print: the raw
#: ``S-1-3-4`` SID that ``restrict_to_owner`` grants, or the display name Windows
#: substitutes for it. Both are accepted because the substitution is LOCALIZED --
#: an English host prints ``OWNER RIGHTS`` and a translated one does not, so pinning
#: a single spelling turns a security assertion into a system-language assertion.
_OWNER_RIGHTS_FULL_RE = re.compile(r"(?:OWNER RIGHTS|S-1-3-4)\s*:\s*\(F\)")


def _owner_only_dacl_violations(icacls_dump: str) -> list[str]:
    """Reasons an ``icacls <path>`` dump is not the owner-only DACL we applied.

    An empty list means compliant. The predicate is factored out of the Windows
    test so it is exercised on every platform: the icacls spawn itself only runs on
    Windows, and a predicate that silently matches nothing there leaves the
    secret-at-rest posture (token signing key, per-app secrets, refresh-token state,
    snapshot tarball, cron internal-secret temp file) verified by nothing at all.
    """
    problems: list[str] = []
    if not _OWNER_RIGHTS_FULL_RE.search(icacls_dump):
        problems.append("no full-control ACE for Owner Rights (S-1-3-4)")
    if _INHERITED_ACE_RE.search(icacls_dump):
        # Any surviving inherited ACE is a finding, not just an inherited (F):
        # an inherited (RX) or (M) for Users still lets another local principal
        # read the secret.
        problems.append("an inherited ACE survived /inheritance:r")
    return problems


class TestOwnerOnlyDaclPredicate:
    """Cover the DACL predicate on the POSIX matrix, where it always executes.

    ``test_applies_owner_only_dacl_on_windows`` can only run on Windows, so without
    these the predicate it asserts through would be unverified everywhere the suite
    actually runs. Dumps are realistic ``icacls`` output shapes.
    """

    _LOCKED = (
        "C:\\Temp\\x\\secret.key OWNER RIGHTS:(F)\n"
        "                        RUNNER\\runneradmin:(F)\n"
        "\n"
        "Successfully processed 1 files; Failed processing 0 files.\n"
    )

    def test_locked_down_dump_has_no_violations(self):
        assert _owner_only_dacl_violations(self._LOCKED) == []

    def test_sid_spelling_of_owner_rights_is_accepted(self):
        # A host that does not resolve S-1-3-4 to a display name must still pass;
        # otherwise the Windows assertion fails for a correctly locked file.
        dump = self._LOCKED.replace("OWNER RIGHTS", "S-1-3-4")
        assert _owner_only_dacl_violations(dump) == []

    def test_missing_owner_rights_ace_is_flagged(self):
        dump = self._LOCKED.replace("OWNER RIGHTS:(F)", "RUNNER\\runneradmin:(RX)")
        assert any("Owner Rights" in p for p in _owner_only_dacl_violations(dump))

    def test_surviving_inherited_ace_is_flagged(self):
        dump = (
            "C:\\Temp\\x\\secret.key OWNER RIGHTS:(F)\n"
            "                        BUILTIN\\Users:(I)(RX)\n"
        )
        assert any("inherited" in p for p in _owner_only_dacl_violations(dump))

    def test_owner_rights_without_full_control_is_flagged(self):
        # A downgrade from (F) to (RX) must not read as compliant.
        dump = self._LOCKED.replace("OWNER RIGHTS:(F)", "OWNER RIGHTS:(RX)")
        assert any("Owner Rights" in p for p in _owner_only_dacl_violations(dump))


class TestRestrictToOwner:
    """Fail-loud owner-only lockdown used by every ~/.kirocrew secret writer.

    A bare ``if IS_POSIX: os.chmod(...)`` guard would leave Windows with NO per-file
    owner-only restriction on the token signing key, per-app secrets, refresh-token
    state, snapshot tarball, and cron internal-secret temp file.
    ``restrict_to_owner`` covers both: POSIX chmod 0o600,
    Windows an owner-only DACL applied via icacls (S-1-3-4 = Owner Rights).
    """

    def test_applies_owner_only_mode_on_posix(self, tmp_path):
        # POSIX path: exact 0o600 mode on disk. Verified only on POSIX because
        # NTFS has no ``st_mode`` perm bits and would report 0o666/0o444 based
        # on the read-only attribute, not the DACL.
        if not pc.IS_POSIX:
            pytest.skip("POSIX chmod branch")
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        pc.restrict_to_owner(f)
        assert os.stat(str(f)).st_mode & 0o777 == 0o600

    def test_propagates_oserror_on_posix(self, tmp_path, monkeypatch):
        # The fail-loud contract: OSError from os.chmod MUST propagate so the
        # security-warning handlers in the callers (token_secret,
        # refresh_tokens, snapshot, cron_script, server, token_auth) fire.
        # Distinct from chmod_safe (which swallows). Regression guard.
        if not pc.IS_POSIX:
            pytest.skip("POSIX chmod branch")
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)

        def boom(*args, **kwargs):
            raise OSError(errno.EPERM, "forced")

        monkeypatch.setattr(pc.os, "chmod", boom)
        with pytest.raises(OSError):
            pc.restrict_to_owner(f)

    def test_applies_owner_only_dacl_on_windows(self, tmp_path):
        # Windows path: apply the DACL in-process, then re-read it via icacls to
        # confirm the owner-only shape end-to-end. Reading through the external
        # tool is deliberate here -- it is an independent check, not the same
        # ctypes code that wrote the descriptor. Windows is the ONLY platform
        # that can execute this branch, so the node id must never be added to
        # windows-expected-failures.txt: listed there alongside this self-skip it
        # would run on no platform at all, and the DACL would be the one control
        # in the secret-at-rest posture that nothing verifies.
        if not pc.IS_WINDOWS:
            pytest.skip("Windows DACL branch")
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        pc.restrict_to_owner(f)
        out = subprocess.check_output(
            ["icacls", str(f)],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", "replace")
        assert _owner_only_dacl_violations(out) == [], out

    def test_propagates_oserror_on_windows_when_the_dacl_write_fails(self, tmp_path, monkeypatch):
        # The fail-loud contract on Windows: a DACL that cannot be applied MUST
        # raise OSError so the caller's warn-and-continue handler fires
        # (dead-code otherwise, per review-bot). Simulate at the writer seam --
        # there is no subprocess to make un-launchable, and the failure
        # this models (SetNamedSecurityInfoW returning ERROR_ACCESS_DENIED on a
        # file whose owner we cannot change) is not reproducible on demand.
        if not pc.IS_WINDOWS:
            pytest.skip("Windows DACL branch")
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)

        def boom(path, *, inherit, sids, **_kw):
            raise pc.windows_acl.AclWriteFailed("SetNamedSecurityInfoW failed (error 5)")

        monkeypatch.setattr(pc.windows_acl, "apply_owner_only", boom)
        with pytest.raises(OSError):
            pc.restrict_to_owner(f)


class TestProcessTokenSid:
    """The non-spawn SID lookup.

    ``whoami`` is the fallback, not the primary, because the primary sits on
    the gateway's bind path: a Windows CI run showed the spawn returning
    nothing under parallel test load, which made every named pipe refuse to be
    created (the DACL cannot be built without a SID).
    """

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows access tokens")
    def test_reads_a_real_sid_from_our_own_token(self) -> None:
        # The unguarded body on purpose: a ctypes prototype mistake surfaces as
        # a traceback naming the failing call instead of collapsing to None.
        sid = pc._process_token_sid_unguarded()
        assert sid is not None
        assert sid.startswith("S-1-")

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows SID lookup")
    def test_some_path_always_resolves_our_sid(self) -> None:
        """The property the gateway depends on: without a SID it cannot build
        the pipe DACL and refuses to bind at all."""
        assert pc.current_user_sid()

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows access tokens")
    def test_agrees_with_the_public_accessor(self) -> None:
        assert pc.current_user_sid() == pc._process_token_sid()

    @pytest.mark.skipif(pc.IS_WINDOWS, reason="the off-Windows guard")
    def test_returns_none_off_windows(self) -> None:
        assert pc._process_token_sid() is None


class TestLocalUserId:
    """The pool-partitioning identity. Must stay an int on every platform."""

    def test_matches_getuid_on_posix(self) -> None:
        if pc.IS_WINDOWS:
            pytest.skip("POSIX uid")
        assert pc.local_user_id() == os.getuid()

    def test_is_an_int_not_a_bool(self) -> None:
        """PoolKey type-checks this dimension and refuses to coerce, because
        bool is a subclass of int and would slip into the wrong partition."""
        value = pc.local_user_id()
        assert isinstance(value, int) and not isinstance(value, bool)

    def test_windows_derives_a_stable_int_from_the_sid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "current_user_sid", lambda: "S-1-5-21-9-8-7-1001")
        first = pc.local_user_id()
        assert isinstance(first, int) and not isinstance(first, bool)
        assert pc.local_user_id() == first  # stable across calls
        monkeypatch.setattr(pc, "current_user_sid", lambda: "S-1-5-21-9-8-7-1002")
        assert pc.local_user_id() != first  # and distinct per user

    def test_windows_without_a_sid_collapses_to_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A partition collapse, not a privilege change: the endpoint is already
        per-user, so two users cannot reach the same pool regardless."""
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "current_user_sid", lambda: None)
        assert pc.local_user_id() == 0


class TestMakeOwnerOnlyDir:
    def test_creates_nested_directory_owner_only_on_posix(self, tmp_path) -> None:
        if pc.IS_WINDOWS:
            pytest.skip("POSIX mode bits")
        target = tmp_path / "a" / "b" / "c"
        pc.make_owner_only_dir(target)
        assert target.is_dir()
        assert stat.S_IMODE(target.stat().st_mode) == 0o700

    def test_tightens_a_preexisting_loose_directory_on_posix(self, tmp_path) -> None:
        """The case a bare mkdir(mode=...) cannot cover: the mode argument is
        ignored entirely when the directory already exists."""
        if pc.IS_WINDOWS:
            pytest.skip("POSIX mode bits")
        loose = tmp_path / "loose"
        loose.mkdir(mode=0o755)
        pc.make_owner_only_dir(loose)
        assert stat.S_IMODE(loose.stat().st_mode) == 0o700

    def test_uses_the_dacl_helper_on_windows(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows derives access from the DACL, so the mode argument is inert
        and the DACL helper is the only thing that protects the directory.

        It must be the DIRECTORY helper. ``restrict_to_owner`` is file-shaped:
        its grants carry no ``(OI)(CI)``, so routing a directory through it
        tightened the directory itself and left every file created inside on
        the creating token's default DACL -- which is why the negative
        assertion below is the load-bearing half of this test.
        """
        calls: list[str] = []
        wrong: list[str] = []
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "restrict_dir_to_owner", lambda p, **_kw: calls.append(str(p)))
        monkeypatch.setattr(pc, "restrict_to_owner", lambda p, **_kw: wrong.append(str(p)))
        target = tmp_path / "win"
        pc.make_owner_only_dir(target)
        assert target.is_dir()
        assert calls == [str(target)]
        assert wrong == [], "a directory must not go through the file-shaped helper"

    def test_directory_still_exists_when_tightening_fails(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Best-effort on the tightening step: the caller decides whether an
        un-tightened directory is fatal, so creation must not be rolled back.

        Patches the same helper ``make_owner_only_dir`` actually calls -- when
        this named the file helper instead, the raise never fired and the test
        passed without exercising the handler at all.
        """
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(
            pc,
            "restrict_dir_to_owner",
            lambda p, **_kw: (_ for _ in ()).throw(OSError("nope")),
        )
        target = tmp_path / "partial"
        pc.make_owner_only_dir(target)
        assert target.is_dir()


class TestCurrentUserSidNeverSpawns:
    """``current_user_sid`` is called from three event-loop paths: the gatewayd
    admission check, the client-side server check, and the pipe DACL builder --
    which runs once per pipe instance and so sits on the accept path.

    None of these can spawn: a ``whoami`` subprocess fallback with a 5 s timeout
    would stall accepts for seconds at a time on a token-lookup failure, so the
    builder reads the token directly and no path here spawns a subprocess.
    """

    @staticmethod
    def _forbid_spawn(*_a, **_kw):
        raise AssertionError("current_user_sid must not spawn -- it runs on the event loop")

    def test_returns_none_without_spawning_when_the_token_read_fails(self, monkeypatch):
        monkeypatch.setattr(pc, "_TOKEN_SID_CACHE", [])
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_process_token_sid", lambda: None)
        monkeypatch.setattr(pc.subprocess, "run", self._forbid_spawn)

        # Fails closed: every caller treats None as "principal unverifiable".
        assert pc.current_user_sid() is None

    def test_returns_the_bare_token_sid_and_memoises_it(self, monkeypatch):
        monkeypatch.setattr(pc, "_TOKEN_SID_CACHE", [])
        monkeypatch.setattr(pc, "IS_POSIX", False)
        calls: list[int] = []

        def _token():
            calls.append(1)
            return "S-1-5-21-1-2-3-1001"

        monkeypatch.setattr(pc, "_process_token_sid", _token)
        monkeypatch.setattr(pc.subprocess, "run", self._forbid_spawn)

        assert pc.current_user_sid() == "S-1-5-21-1-2-3-1001"
        assert pc.current_user_sid() == "S-1-5-21-1-2-3-1001"
        assert len(calls) == 1, "the SID is constant for the process lifetime"

    def test_strips_the_icacls_star_prefix(self, monkeypatch):
        """The icacls form carries a leading ``*``; SDDL and the Win32 security
        APIs want the bare SID."""
        monkeypatch.setattr(pc, "_TOKEN_SID_CACHE", [])
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_process_token_sid", lambda: "*S-1-5-21-9-9-9-500")
        monkeypatch.setattr(pc.subprocess, "run", self._forbid_spawn)

        assert pc.current_user_sid() == "S-1-5-21-9-9-9-500"


class TestOwnerOnlyDaclIsIdempotent:
    """The lockdown must probe before writing, and must fail TOWARD writing.

    Applying an owner-only DACL to a DIRECTORY carries inheritable ACEs, which
    Windows propagates to every descendant, so the write is O(descendants) --
    measured 0.238 ms per object, i.e. 2.94 s on a 12358-object data home, paid on
    every gateway boot because ``vector_memory.init()`` locks down the whole home.
    Reading this object's own descriptor is O(1), so an unchanged DACL should cost
    a constant check.

    These run on the POSIX matrix (the seam is the ``windows_acl`` call, which is
    what the suite can observe off Windows), and they pin BOTH directions: the skip
    must happen when it is safe, and must NOT happen when anything differs.
    """

    @staticmethod
    def _capture(monkeypatch, *, matches, sid="S-1-5-21-1-2-3-1000"):
        """Force the Windows branch; record probe questions and DACL writes."""
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_TOKEN_SID_CACHE", [])
        monkeypatch.setattr(pc, "current_user_sid", lambda: sid)
        probed: list[dict] = []
        written: list[dict] = []

        def fake_probe(path, *, inherit, sids, **_kw):
            probed.append({"path": os.fspath(path), "inherit": inherit, "sids": tuple(sids)})
            if isinstance(matches, Exception):
                raise matches
            return matches

        def fake_apply(path, *, inherit, sids, **_kw):
            written.append({"path": os.fspath(path), "inherit": inherit, "sids": tuple(sids)})

        monkeypatch.setattr(pc.windows_acl, "owner_only_dacl_matches", fake_probe)
        monkeypatch.setattr(pc.windows_acl, "apply_owner_only", fake_apply)
        return probed, written

    def test_an_already_correct_dacl_is_not_rewritten(self, tmp_path, monkeypatch):
        # The whole point: the O(descendants) propagation must not run when the
        # descriptor already says what we would write.
        probed, written = self._capture(monkeypatch, matches=True)
        d = tmp_path / "home"
        d.mkdir()
        pc.restrict_dir_to_owner(d)
        assert len(probed) == 1, probed
        assert written == [], "an unchanged DACL must not be re-applied"

    def test_a_mismatched_dacl_is_still_written(self, tmp_path, monkeypatch):
        # Guards the failure mode that would make this change a security bug:
        # the skip must never swallow a write that is actually needed.
        probed, written = self._capture(monkeypatch, matches=False)
        d = tmp_path / "home"
        d.mkdir()
        pc.restrict_dir_to_owner(d)
        assert len(probed) == 1, probed
        assert len(written) == 1, written
        assert written[0]["inherit"] is True

    def test_a_probe_that_raises_is_treated_as_a_mismatch(self, tmp_path, monkeypatch):
        # Fail-safe direction: an unanswerable probe costs a redundant write,
        # never a skipped lockdown.
        probed, written = self._capture(monkeypatch, matches=OSError("descriptor unreadable"))
        d = tmp_path / "home"
        d.mkdir()
        pc.restrict_dir_to_owner(d)
        assert len(probed) == 1, probed
        assert len(written) == 1, "a probe failure must fall back to writing"

    def test_the_probe_is_asked_about_exactly_what_would_be_written(self, tmp_path, monkeypatch):
        # If the probe were asked a different question than the write answers, a
        # match could authorise skipping a DACL that does not exist yet.
        probed, written = self._capture(monkeypatch, matches=False)
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        pc.restrict_to_owner(f)
        assert len(probed) == 1 and len(written) == 1
        assert probed[0] == written[0], (probed[0], written[0])
        # File shape, so the grants must NOT be inheritable.
        assert probed[0]["inherit"] is False
        assert probed[0]["sids"] == ("S-1-3-4", "S-1-5-21-1-2-3-1000")


class TestOwnerOnlyDaclMatchesIsConservative:
    """``windows_acl.owner_only_dacl_matches`` must never answer True on doubt."""

    # (ace_type, ace_flags, mask, sid) as the ctypes half parses them.
    _PROTECTED = 0x1000
    _ALLOWED = 0
    _OI_CI = 0x01 | 0x02
    _ALL = 0x001F01FF
    _SIDS = ("S-1-3-4", "S-1-5-21-1-2-3-1000")

    def _dir_aces(self):
        return [(self._ALLOWED, self._OI_CI, self._ALL, s) for s in self._SIDS]

    def _matches(self, **over):
        kw = {
            "control": self._PROTECTED,
            "aces": self._dir_aces(),
            "inherit": True,
            "sids": self._SIDS,
        }
        kw.update(over)
        return pc.windows_acl.owner_only_dacl_matches_parsed(**kw)

    def test_the_exact_shipped_shape_matches(self):
        # The positive case. Without this, every rule below could pass by always
        # answering False and the probe would be a no-op that never skips.
        assert self._matches() is True

    def test_order_does_not_matter(self):
        # The kernel may normalise ACE order; the grant SET is the policy.
        assert self._matches(aces=list(reversed(self._dir_aces()))) is True

    def test_an_unprotected_dacl_never_matches(self):
        # Inheritance not stripped: applying PROTECTED would still change it.
        assert self._matches(control=0) is False

    def test_a_missing_grant_never_matches(self):
        assert self._matches(aces=self._dir_aces()[:1]) is False

    def test_an_extra_grant_never_matches(self):
        extra = self._dir_aces() + [(self._ALLOWED, self._OI_CI, self._ALL, "S-1-1-0")]
        assert self._matches(aces=extra) is False

    def test_a_different_principal_never_matches(self):
        aces = self._dir_aces()
        aces[1] = (self._ALLOWED, self._OI_CI, self._ALL, "S-1-1-0")
        assert self._matches(aces=aces) is False

    def test_a_deny_ace_never_matches(self):
        aces = self._dir_aces()
        aces[0] = (1, self._OI_CI, self._ALL, self._SIDS[0])  # ACCESS_DENIED
        assert self._matches(aces=aces) is False

    def test_a_narrower_mask_never_matches(self):
        aces = self._dir_aces()
        aces[0] = (self._ALLOWED, self._OI_CI, 0x120089, self._SIDS[0])  # read-ish
        assert self._matches(aces=aces) is False

    def test_directory_shape_rejects_non_inheritable_grants(self):
        # inherit=True wants (OI)(CI); a file-shaped ACE would leave children
        # uncovered, so it must not read as already correct.
        assert self._matches(aces=[(self._ALLOWED, 0, self._ALL, s) for s in self._SIDS]) is False

    def test_file_shape_rejects_inheritable_grants(self):
        # The mirror: inherit=False wants no inheritance flags.
        assert self._matches(inherit=False) is False

    def test_file_shape_matches_its_own_flags(self):
        assert (
            self._matches(
                inherit=False, aces=[(self._ALLOWED, 0, self._ALL, s) for s in self._SIDS]
            )
            is True
        )

    def test_an_unresolvable_sid_never_matches(self):
        aces = self._dir_aces()
        aces[0] = (self._ALLOWED, self._OI_CI, self._ALL, "")
        assert self._matches(aces=aces) is False

    def test_no_grants_never_matches(self, tmp_path):
        # apply_owner_only refuses an empty grant set, so "matches" is meaningless
        # here; answering True would skip a lockdown that never happened.
        assert self._matches(aces=[], sids=()) is False
        assert pc.windows_acl.owner_only_dacl_matches(tmp_path, inherit=True, sids=()) is False

    def test_an_unavailable_platform_api_never_matches(self, tmp_path):
        # Off Windows there is no descriptor to compare. This is the case the whole
        # POSIX matrix exercises, and it must read as "write", not "already correct".
        assert (
            pc.windows_acl.owner_only_dacl_matches(tmp_path, inherit=True, sids=("S-1-3-4",))
            is False
        )
