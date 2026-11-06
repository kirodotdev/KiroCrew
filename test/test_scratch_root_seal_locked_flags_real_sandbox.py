"""The scratch root's seal around its windows holds on a flag-locked tmpfs, on a real kernel.

Since the launcher stages its stand-ins on a private tmpfs, a real launch seals a stand-in whose
nosuid/nodev it mounted itself, so their locks are never tested there. A launch whose stage is
refused keeps its stand-ins on the host root, ``/run/user/$UID`` or ``/dev/shm``, whose
nosuid/nodev the user namespace locks. This test drives that case: an outer namespace mounts a
``nosuid,nodev`` tmpfs, and in a nested one, for which that tmpfs is inherited and so locked,
``mask_sensitive`` binds a stand-in from it over a mask listed in ``seal_windowed_masks``, binds
the window back and seals the stand-in. A create beside the window must fail with EROFS while
the window, a mount of its own, keeps its contents and stays writable. A control first proves
the lock is real (``test/test_sandbox_seal_locked_flags.py``, whose stages this reuses, explains
it).

Linux needs unprivileged user and mount namespaces, which the namespace-sandbox CI job provides
and the shard runners do not, so there the test skips with that reason. It skips, never fails,
where ``unshare``, nesting, a tmpfs mount or the flag lock is unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap

import pytest
from test_sandbox_launcher_program import payload
from test_sandbox_seal_locked_flags import _SHARED_PREAMBLE, _outer_script

from kiro_crew import sandbox_launcher
from kiro_crew.sandbox import userns_available
from kiro_crew.testing.wait import default_timeout

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="Linux namespace launcher only"),
    pytest.mark.skipif(
        not userns_available(),
        reason="this host cannot build the namespace sandbox (no unprivileged user and mount "
        "namespaces)",
    ),
]


def _inner_windowed_mask_script() -> str:
    """Stage 2 for a mask sealed AROUND its window (the scratch root), on a real kernel.

    The stand-in comes from the inherited tmpfs, whose nosuid/nodev are locked, as the
    scratch root's does from ``/run/user/$UID`` when the launcher's private stand-in stage
    is refused. The control proves the lock (else skip 44); then the window is staged and
    ``mask_sensitive`` binds the stand-in, binds the window back and seals the stand-in
    read-only.
    """
    body = textwrap.dedent("""\
        import importlib.util
        import json

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

        masked = os.path.join(mnt, b"masked")
        window = os.path.join(masked, b"win")
        os.mkdir(window)
        with open(os.path.join(window, b"kept"), "wb") as fh:
            fh.write(b"kept")

        spec = importlib.util.spec_from_file_location("launcher_program", sys.argv[2])
        program = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(program)
        with open(sys.argv[3], encoding="utf-8") as fh:
            plan = json.load(fh)
        run = program.Launch(plan, program._load_libc(), environ={})
        run.tmpfs_src = sys.argv[1]
        run.src_prefix = "kirocrew_sb_test_"
        program.stage_private_windows(run)
        program.mask_sensitive(run)

        try:
            with open(os.path.join(masked, b"outside"), "wb") as fh:
                fh.write(b"x")
        except OSError as exc:
            if exc.errno != errno.EROFS:
                raise
        else:
            sys.stderr.write("a create beside the window succeeded; no seal\\n")
            sys.exit(1)
        with open(os.path.join(window, b"kept"), "rb") as fh:
            if fh.read() != b"kept":
                sys.stderr.write("the window lost its contents\\n")
                sys.exit(2)
        with open(os.path.join(window, b"new"), "wb") as fh:
            fh.write(b"y")
        if os.statvfs(window).f_flag & os.ST_RDONLY:
            sys.stderr.write("the window was remounted read-only\\n")
            sys.exit(3)
        print("OK")
        """)
    return _SHARED_PREAMBLE + "\n" + body


def test_the_stand_in_seals_and_its_window_stays_writable(tmp_path):
    """The scratch root's seal on a locked tmpfs: EROFS beside the window, rw inside.

    Every mount and stand-in is under ``tmp_path``, in namespaces that end with the run.
    """
    unshare = shutil.which("unshare")
    if unshare is None:
        pytest.skip("unshare not available on this host")
    probe = subprocess.run(
        [unshare, "-Urm", "--propagation", "private", "true"],
        capture_output=True,
        cwd=str(tmp_path),
        timeout=default_timeout(),
    )
    if probe.returncode != 0:
        pytest.skip(
            "user namespaces unavailable: %s" % probe.stderr.decode(errors="replace").strip()
        )

    outer = tmp_path / "outer_stage.py"
    outer.write_text(_outer_script())
    inner = tmp_path / "inner_windowed_stage.py"
    inner.write_text(_inner_windowed_mask_script())
    mnt = tmp_path / "mnt"
    mnt.mkdir()
    masked = str(mnt / "masked")
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            payload(
                sensitive_dirs=[masked],
                private_dirs=[masked + "/win"],
                seal_windowed_masks=[masked],
            )
        )
    )
    program_file = tmp_path / "launcher_program.py"
    program_file.write_text(sandbox_launcher.launcher_program_source(), encoding="utf-8")
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
            str(program_file),
            str(plan),
        ],
        capture_output=True,
        cwd=str(tmp_path),
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
    )
    if result.returncode == 42:
        pytest.skip("tmpfs mount unavailable inside the namespace: %s" % result.stderr)
    if result.returncode == 43:
        pytest.skip("nested user namespaces unavailable: %s" % result.stderr)
    if result.returncode == 44:
        pytest.skip("kernel did not lock the flags here: %s" % result.stderr)

    assert result.returncode == 0, (
        f"the windowed mask did not seal around its window inside the nested "
        f"namespace: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "OK" in result.stdout
