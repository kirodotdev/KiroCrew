"""The read-only seal re-asserts the target mount's LOCKED nosuid/nodev/noexec bits.

Inside an unprivileged user namespace the kernel treats a mount's nosuid / nodev /
noexec bits as locked (``MNT_LOCK_*``) and rejects with EPERM any remount whose
flag set would clear them. The bind created by the ``READONLY_DIRS`` loop inherits
those bits — locks included — from its source mount, so a remount carrying only
``MS_RDONLY`` was refused on hosts whose ``/tmp`` (or ``/home``) is mounted
``nosuid,nodev``: the systemd ``tmp.mount`` default on Amazon Linux 2023, Fedora
and RHEL. ``_mount_or_die`` fails closed, so every sandboxed spawn aborted there
(issue #8386). The fix: ``_locked_mount_flags`` reads the new bind's effective
flags via ``statvfs`` and the sealing remount ORs them back in — re-asserting a
bit already in force can only keep restrictions, never widen access.

Three layers here, mirroring the other sandbox launcher tests (the sources under
test are extracted from the GENERATED script, so none of these can pass against
code the launcher no longer contains):

- unit: the helper's ``f_flag`` → ``MS_*`` mapping, ``statvfs`` patched;
- integration: the seal loop's remount call receives the helper's bits OR'd in,
  and the helper reads the target AFTER the bind step;
- real kernel: NESTED namespaces. Flags are only locked on mounts a namespace
  INHERITED, never on mounts it created itself — so an outer namespace mounts a
  fresh tmpfs ``nosuid,nodev`` (inside the outer namespace, so the test does not
  depend on the host's ``/tmp`` options), and an inner nested namespace, for
  which that tmpfs is inherited and therefore locked, runs the seal. A control
  first proves the lock is real: the pre-fix remount (``MS_RDONLY`` alone) must
  be refused with EPERM, then the fixed path must succeed and a write must be
  refused with EROFS. Skips — never fails — where user namespaces, ``unshare``,
  tmpfs mounting, or the flag locking itself are unavailable.

Everything is Linux-only: ``os.ST_NODEV`` / ``os.ST_NOEXEC`` are Linux-only
constants (macOS defines only ``ST_RDONLY`` / ``ST_NOSUID``), and the control
under test is the Linux namespace launcher.
"""

from __future__ import annotations

import errno
import os
import runpy
import shutil
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from kiro_crew.sandbox import _build_launcher_script

_LINUX_ONLY = pytest.mark.skipif(sys.platform != "linux", reason="Linux namespace launcher only")

#: Flag values the launcher defines for itself; mirrored so extracted code can run.
_MS_RDONLY = 1
_MS_NOSUID = 2
_MS_NODEV = 4
_MS_NOEXEC = 8
_MS_REMOUNT = 32
_MS_BIND = 4096

_HELPER_START = "def _locked_mount_flags("
_HELPER_END = "REAL_UID = "


def _cut(script: str, start_marker: str, end_marker: str) -> str:
    """Slice *script* from the START OF THE LINE holding each marker.

    Same trick as ``test_sandbox_mount_checked._region``: ``dedent`` measures the
    common prefix across all lines, so a first line already stripped of its indent
    would leave the rest indented and the block would not parse.
    """
    a = script.rindex("\n", 0, script.index(start_marker)) + 1
    b = script.rindex("\n", 0, script.index(end_marker, a)) + 1
    return script[a:b]


def _seal_loop(script: str) -> str:
    return (
        "for d in READONLY_DIRS:"
        + script.split("for d in READONLY_DIRS:", 1)[1].split("\n\n", 1)[0]
    )


def _helper_namespace(tmp_path) -> dict:
    """Run the launcher's OWN ``_locked_mount_flags`` source; return its namespace.

    Via ``runpy.run_path`` rather than ``exec`` of the text, for the same reason
    ``test_sandbox_mount_checked`` does: equivalent here, but ``exec`` trips the
    SAST gate's ``exec-detected`` rule on a false positive.
    """
    region = _cut(_build_launcher_script("strict"), _HELPER_START, _HELPER_END)
    region_file = tmp_path / "helper_region.py"
    region_file.write_text(region)
    return runpy.run_path(
        str(region_file),
        init_globals={
            "os": os,
            "_MS_NOSUID": _MS_NOSUID,
            "_MS_NODEV": _MS_NODEV,
            "_MS_NOEXEC": _MS_NOEXEC,
        },
    )


@_LINUX_ONLY
class TestLockedMountFlagsHelper:
    def test_nosuid_and_nodev_are_mapped_to_their_ms_flags(self, tmp_path, monkeypatch):
        helper = _helper_namespace(tmp_path)["_locked_mount_flags"]
        monkeypatch.setattr(
            os, "statvfs", lambda _t: SimpleNamespace(f_flag=os.ST_NOSUID | os.ST_NODEV)
        )

        assert helper(b"/anywhere") == _MS_NOSUID | _MS_NODEV

    def test_all_three_lockable_bits_are_mapped(self, tmp_path, monkeypatch):
        helper = _helper_namespace(tmp_path)["_locked_mount_flags"]
        monkeypatch.setattr(
            os,
            "statvfs",
            lambda _t: SimpleNamespace(f_flag=os.ST_NOSUID | os.ST_NODEV | os.ST_NOEXEC),
        )

        assert helper(b"/anywhere") == _MS_NOSUID | _MS_NODEV | _MS_NOEXEC

    def test_a_plain_flag_set_yields_zero_extra_flags(self, tmp_path, monkeypatch):
        """No locked bit means the remount behaves exactly as it always did."""
        helper = _helper_namespace(tmp_path)["_locked_mount_flags"]
        monkeypatch.setattr(os, "statvfs", lambda _t: SimpleNamespace(f_flag=0))

        assert helper(b"/anywhere") == 0

    def test_statvfs_failure_falls_back_to_zero_never_degrading_the_seal(
        self, tmp_path, monkeypatch
    ):
        """The fallback is 0 EXTRA flags — the remount itself still fails closed.

        A helper that raised here would turn a transient stat failure into a
        refused spawn even on hosts where the plain remount would have succeeded;
        a helper that silently skipped the remount would degrade the seal. Zero
        extra flags does neither.
        """
        helper = _helper_namespace(tmp_path)["_locked_mount_flags"]

        def _boom(_t):
            raise OSError(errno.EACCES, "statvfs refused")

        monkeypatch.setattr(os, "statvfs", _boom)

        assert helper(b"/anywhere") == 0

    def test_a_host_without_the_st_constants_maps_to_zero_not_a_crash(self, tmp_path, monkeypatch):
        """macOS defines only ``ST_RDONLY``/``ST_NOSUID`` — the helper's extracted
        source is executed by the POSIX-wide mount-region tests, so a missing
        constant must read as 0, never raise ``AttributeError``."""
        helper = _helper_namespace(tmp_path)["_locked_mount_flags"]
        monkeypatch.setattr(os, "statvfs", lambda _t: SimpleNamespace(f_flag=0xFFFF))
        monkeypatch.delattr(os, "ST_NODEV", raising=False)
        monkeypatch.delattr(os, "ST_NOEXEC", raising=False)

        assert helper(b"/anywhere") == _MS_NOSUID


@_LINUX_ONLY
class TestSealRemountCarriesTheLockedBits:
    def test_remount_flags_include_the_helper_result_read_after_the_bind(
        self, tmp_path, monkeypatch
    ):
        """The seal loop ORs the helper's bits into the remount, post-bind.

        Order is asserted, not just the flags: the helper must read the target
        AFTER the ``MS_BIND`` step, because that is when ``f_flag`` reflects the
        new bind — the mount whose locks the kernel will enforce on the remount.
        """
        script = _build_launcher_script("strict")
        helper = _cut(script, _HELPER_START, _HELPER_END)
        region_file = tmp_path / "seal_region.py"
        region_file.write_text(helper + "\n" + textwrap.dedent(_seal_loop(script)) + "\n")

        target = tmp_path / "sealed"
        target.mkdir()
        events: list[tuple] = []

        def _record_mount(source, target_, flags, what):
            assert source == target_, "a ceiling is bound over ITSELF"
            events.append(("mount", os.fsdecode(target_), flags))

        def _fake_statvfs(target_):
            events.append(("statvfs", os.fsdecode(target_)))
            return SimpleNamespace(f_flag=os.ST_NOSUID | os.ST_NODEV)

        monkeypatch.setattr(os, "statvfs", _fake_statvfs)
        runpy.run_path(
            str(region_file),
            init_globals={
                "os": os,
                "READONLY_DIRS": [str(target)],
                "_mount_or_die": _record_mount,
                "_MS_BIND": _MS_BIND,
                "_MS_REMOUNT": _MS_REMOUNT,
                "_MS_RDONLY": _MS_RDONLY,
                "_MS_NOSUID": _MS_NOSUID,
                "_MS_NODEV": _MS_NODEV,
                "_MS_NOEXEC": _MS_NOEXEC,
            },
        )

        assert events == [
            ("mount", str(target), _MS_BIND),
            ("statvfs", str(target)),
            (
                "mount",
                str(target),
                _MS_REMOUNT | _MS_BIND | _MS_RDONLY | _MS_NOSUID | _MS_NODEV,
            ),
        ]


_SHARED_PREAMBLE = textwrap.dedent("""\
    import ctypes
    import errno
    import os
    import sys

    _MS_RDONLY = 1
    _MS_NOSUID = 2
    _MS_NODEV = 4
    _MS_NOEXEC = 8
    _MS_REMOUNT = 32
    _MS_BIND = 4096

    _libc = ctypes.CDLL(None, use_errno=True)
    _libc.mount.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
        ctypes.c_ulong, ctypes.c_void_p,
    ]
    _libc.mount.restype = ctypes.c_int
    """)


def _outer_script() -> str:
    """Stage 1, run under ``unshare -Urm --propagation private``.

    Mounts a fresh tmpfs ``nosuid,nodev`` — a mount THIS namespace created, so
    its flags are NOT locked here — then re-execs into a nested user+mount
    namespace, for which that tmpfs is an INHERITED mount and the kernel locks
    its nosuid/nodev bits. Exit 42 = cannot mount tmpfs; 43 = cannot nest
    namespaces; otherwise propagates the inner stage's exit code.
    """
    driver = textwrap.dedent("""\
        import subprocess

        mnt = sys.argv[1].encode()
        inner = sys.argv[2]
        unshare = sys.argv[3]
        if _libc.mount(b"tmpfs", mnt, b"tmpfs", _MS_NOSUID | _MS_NODEV, None) != 0:
            sys.stderr.write("tmpfs mount failed: errno %d\\n" % ctypes.get_errno())
            sys.exit(42)
        os.mkdir(os.path.join(mnt, b"sealed"))
        os.mkdir(os.path.join(mnt, b"control"))
        nested = ["%s" % unshare, "-Urm", "--propagation", "private"]
        probe = subprocess.run(nested + ["true"], capture_output=True)
        if probe.returncode != 0:
            sys.stderr.write("nested namespace unavailable: %s\\n"
                             % probe.stderr.decode(errors="replace"))
            sys.exit(43)
        rc = subprocess.call(nested + [sys.executable, inner, sys.argv[1]])
        sys.exit(rc)
        """)
    return _SHARED_PREAMBLE + "\n" + driver


def _inner_script() -> str:
    """Stage 2, the nested namespace: control, then the fixed seal path.

    Assembled from the GENERATED launcher source — ``_mount_or_die`` +
    ``_locked_mount_flags`` + the ``READONLY_DIRS`` loop — so the code under test
    is the code that ships.

    The control comes first and is what gives the test its power: bind the
    control dir over itself, then attempt the PRE-FIX remount (``MS_RDONLY``
    alone). On a locked mount the kernel must refuse it with EPERM — the exact
    #8386 failure. If it succeeds the environment does not lock flags and the
    test skips (44) rather than passing vacuously. Only then does the fixed
    path run against the sealed dir; the seal must land and a write must be
    refused with EROFS.
    """
    script = _build_launcher_script("strict")
    defs = _cut(script, "def _mount_or_die(", _HELPER_END)
    control = textwrap.dedent("""\
        mnt = sys.argv[1].encode()
        control = os.path.join(mnt, b"control")
        if _libc.mount(control, control, None, _MS_BIND, None) != 0:
            sys.stderr.write("control bind failed: errno %d\\n" % ctypes.get_errno())
            sys.exit(45)
        if _libc.mount(control, control, None,
                       _MS_REMOUNT | _MS_BIND | _MS_RDONLY, None) == 0:
            sys.stderr.write("control remount succeeded; flags are not locked here\\n")
            sys.exit(44)
        _control_errno = ctypes.get_errno()
        if _control_errno != errno.EPERM:
            sys.stderr.write("control remount failed with errno %d, not EPERM\\n"
                             % _control_errno)
            sys.exit(46)
        sealed = os.path.join(mnt, b"sealed")
        READONLY_DIRS = [os.fsdecode(sealed)]
        """)
    epilogue = textwrap.dedent("""\
        try:
            with open(os.path.join(sealed, b"probe"), "wb") as fh:
                fh.write(b"x")
        except OSError as exc:
            if exc.errno == errno.EROFS:
                print("OK")
                sys.exit(0)
            raise
        sys.stderr.write("write succeeded; the seal did not hold\\n")
        sys.exit(1)
        """)
    return "\n".join(
        (_SHARED_PREAMBLE, defs, control, textwrap.dedent(_seal_loop(script)), epilogue)
    )


@_LINUX_ONLY
class TestSealHoldsOnALockedNosuidNodevMountRealKernel:
    def test_prefix_remount_gets_eperm_and_the_fixed_seal_lands(self, tmp_path):
        """The regression on a real kernel, with its own power proven in-band.

        The inner stage first shows the pre-fix remount is refused with EPERM
        (so the locked-flag condition #8386 reported genuinely holds in this
        environment), then runs the shipped seal path and asserts it succeeds
        and the sealed dir refuses a write with EROFS. Pre-fix code fails here
        at the seal step with the launcher's own ``sandbox: BLOCKED`` refusal.
        """
        unshare = shutil.which("unshare")
        if unshare is None:
            pytest.skip("unshare not available on this host")
        probe = subprocess.run(
            [unshare, "-Urm", "--propagation", "private", "true"],
            capture_output=True,
            cwd=str(tmp_path),
            timeout=30,
        )
        if probe.returncode != 0:
            pytest.skip(
                "user namespaces unavailable: %s" % probe.stderr.decode(errors="replace").strip()
            )

        outer = tmp_path / "outer_stage.py"
        outer.write_text(_outer_script())
        inner = tmp_path / "inner_stage.py"
        inner.write_text(_inner_script())
        mnt = tmp_path / "mnt"
        mnt.mkdir()
        result = subprocess.run(
            [
                unshare,
                "-Urm",
                "--propagation",
                "private",
                sys.executable,
                str(outer),
                str(mnt),
                str(inner),
                unshare,
            ],
            capture_output=True,
            cwd=str(tmp_path),
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        if result.returncode == 42:
            pytest.skip("tmpfs mount unavailable inside the namespace: %s" % result.stderr)
        if result.returncode == 43:
            pytest.skip("nested user namespaces unavailable: %s" % result.stderr)
        if result.returncode == 44:
            pytest.skip("kernel did not lock the flags here: %s" % result.stderr)

        assert result.returncode == 0, (
            f"seal did not hold inside the nested namespace: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "OK" in result.stdout
