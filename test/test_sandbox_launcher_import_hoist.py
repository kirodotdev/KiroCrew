"""The namespace launcher must import everything BEFORE entering isolation.

A first-time stdlib import reads module files off disk. The launcher's capability
drop and seccomp install run in the child AFTER ``unshare(NEWUSER)`` +
``unshare(NEWNS)`` + the mount masking, and on a host whose LSM restricts
unprivileged user namespaces that post-unshare read is denied: Ubuntu 24.04 with
``apparmor_restrict_unprivileged_userns=1`` killed ``import platform`` at
seccomp-install time with ``ModuleNotFoundError``, so every sandboxed spawn died
inside the launcher. The isolation probe in Auto-Improvement then read that crash
as "push is not disabled".

The launcher is ``kiro_crew.sandbox_launcher_program``; a spawn runs that module's
source with one plan line substituted. These tests run it:

* in a fresh isolated interpreter with every further import DENIED once the program
  has loaded -- what that LSM does to a post-unshare import -- and drive the child's
  stages to the exec over a real tree, with a stand-in libc;
* as a script from a directory holding a shadow copy of every module it imports,
  the way the gateway's ``run/`` directory can hold a stray ``struct.py``, so the
  ``sys.path`` purge is seen to run before anything resolves from the filesystem;
* to each refusal its classifier keys on, so the probe's launcher-failure prefixes
  are prefixes of lines the launcher really emits.

One structural rule stays read from the parsed source: every ``import`` is
MODULE-LEVEL. Module scope runs before ``main`` forks, so a module-level import can
never hit the post-isolation denial; a run covers only the branches its tree takes,
and the rule is what keeps a lazy import out of the others.
"""

from __future__ import annotations

import ast
import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_sandbox_launcher_program import RecordingLibc, launch, payload, refusal

from kiro_crew import sandbox_launcher, sandbox_launcher_program, sandbox_plan

program = sandbox_launcher_program

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="the namespace launcher is Linux-only: its stages pin through O_PATH and "
    "/proc/self/fd, and its payload carries os.getuid() (absent on Windows)",
)

#: Every module the launcher imports at module level. ``sys`` is the builtin the purge
#: itself needs, so it is the one that cannot be shadowed and is not listed.
_LAUNCHER_IMPORTS = ("ctypes", "errno", "os", "platform", "stat", "struct", "tempfile")


def _render(tmp_path: Path, plan: sandbox_plan.ConfinementPlan, name: str) -> Path:
    """Write the launcher rendered for *plan* where a spawn would run it from."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    script = run_dir / name
    script.write_text(sandbox_launcher.render_namespace_launcher(plan), encoding="utf-8")
    return script


def test_every_launcher_import_is_module_level() -> None:
    """No import may execute after namespace/mount isolation.

    The only sanctioned position is a direct child of the module body, which runs
    before ``fork()`` and therefore before either ``unshare()``. Imports nested in
    module-level ``if``/``try`` blocks are refused too: they would pass a naive
    indentation check while still being conditional.
    """
    tree = ast.parse(sandbox_launcher.launcher_program_source())
    module_level = {id(node) for node in tree.body}
    offenders = [
        f"line {node.lineno}: {ast.dump(node)[:120]}"
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in module_level
    ]
    assert not offenders, (
        "the launcher program contains non-module-level import(s); a first-time import "
        "after unshare()+mount isolation is denied on LSM-restricted hosts -- hoist them "
        "to module level:\n" + "\n".join(offenders)
    )


#: Loads the rendered launcher under a name other than ``__main__`` (so it defines its
#: stages and does not start), then denies every import, as the LSM does once the child
#: has unshared, and runs the child's stages to the exec with a libc whose binds hide
#: their target the way a real mount does. Prints ``exec <argv0>`` on reaching it.
_DENIED_IMPORT_DRIVER = """
import importlib.util, os, sys

spec = importlib.util.spec_from_file_location("launcher_under_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
stand_ins, aside, home = sys.argv[2], sys.argv[3], sys.argv[4]


class _Denied:
    def find_spec(self, name, path=None, target=None):
        raise ImportError("import of %s after isolation" % name)


class _CoveringLibc:
    covered = 0

    def mount(self, source, target, fstype, flags, data):
        if source is None or not flags & 4096 or flags & 32:
            return 0
        src, tgt = (
            os.readlink(p) if p.startswith("/proc/self/fd/") else p
            for p in (os.fsdecode(source), os.fsdecode(target))
        )
        if src == tgt:
            return 0
        self.covered += 1
        os.rename(tgt, os.path.join(aside, str(self.covered)))
        os.rename(src, tgt)
        return 0

    def umount2(self, target, flags):
        return 0

    def unshare(self, flags):
        return 0

    def prctl(self, option, a2, a3, a4, a5):
        return 0


plan = dict(module._PLAN, stand_in_roots=[])
run = module.Launch(
    plan,
    _CoveringLibc(),
    environ={"HOME": home},
    execvp=lambda file, argv: print("exec", file),
)
run.tmpfs_src = stand_ins
sys.meta_path.insert(0, _Denied())
module.run_child(run, ["/bin/agent"])
"""


def test_no_stage_imports_once_the_launcher_has_loaded(tmp_path: Path) -> None:
    """The child's stages reach the exec with every first-time import refused.

    The tree gives each kind of mask something to cover -- a credential directory, a
    secret file, ``~/.ssh`` with its known_hosts, a sealed ceiling -- so the hiding
    stages run their pin, mount and read-back, and the capability drop and seccomp
    install run after them, all after the denial is in place.
    """
    home = tmp_path / "home"
    keys = home / ".aws"
    keys.mkdir(parents=True)
    (keys / "credentials").write_text("[default]\n", encoding="utf-8")
    (home / ".netrc").write_text("machine x\n", encoding="utf-8")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "known_hosts").write_text("host ssh-ed25519 AAAA\n", encoding="utf-8")
    ceiling = home / ".kiro" / "crew" / "security_policy.json"
    ceiling.parent.mkdir(parents=True)
    ceiling.write_text("{}", encoding="utf-8")
    host = sandbox_plan.PlanHost(
        home=str(home),
        tier_dirs=(".aws",),
        cc_files=(".netrc",),
        crew_readonly_targets=(".kiro/crew/security_policy.json",),
        uid=os.getuid(),
        gid=os.getgid(),
    )
    plan = sandbox_plan.plan_confinement(sandbox_plan.SandboxRequest(tier="strict"), host)
    script = _render(tmp_path, plan, "kirocrew_sandbox_imports.py")
    stand_ins = tmp_path / "stand-ins"
    aside = tmp_path / "under-masks"
    stand_ins.mkdir()
    aside.mkdir()

    done = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            _DENIED_IMPORT_DRIVER,
            str(script),
            str(stand_ins),
            str(aside),
            str(home),
        ],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
        cwd=str(tmp_path),
    )

    assert done.returncode == 0 and done.stdout == "exec /bin/agent\n", (
        "a launcher stage imported a module after the program loaded; on an "
        "LSM-restricted host that import is denied after unshare and the spawn dies:\n"
        + done.stderr
    )
    # The run did reach the masks: every hidden name now shows its empty stand-in.
    assert os.listdir(keys) == [] and (home / ".netrc").read_text(encoding="utf-8") == ""
    assert os.listdir(home / ".ssh") == ["known_hosts"]


def test_syspath_purge_precedes_every_filesystem_import(tmp_path: Path) -> None:
    """The stdlib-shadowing hardening must stay ahead of every import it protects.

    Run as a script, CPython puts the script's own directory first on ``sys.path``,
    so a sibling ``struct.py`` left in the gateway's ``run/`` directory would shadow
    the real stdlib (seen as "cannot import name 'calcsize' from '/tmp/struct.py'").
    Every module the launcher imports is shadowed here by one that announces itself
    and exits; ``-I`` is left off, so nothing but the launcher's own purge keeps them
    out. Started with no agent command, the launcher loads libc and refuses before it
    forks, which is the whole of what runs.
    """
    plan = sandbox_plan.plan_confinement(
        sandbox_plan.SandboxRequest(tier="strict"), sandbox_plan.PlanHost(home=str(tmp_path))
    )
    script = _render(tmp_path, plan, "kirocrew_sandbox_shadowed.py")
    for name in _LAUNCHER_IMPORTS:
        (script.parent / f"{name}.py").write_text(
            "import sys\n"
            f"sys.stderr.write('SHADOW {name} imported\\n')\n"
            "raise SystemExit(97)\n",
            encoding="utf-8",
        )

    done = subprocess.run(
        [sys.executable, "-E", "-S", str(script)],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
        cwd=script.parent,
    )

    assert "SHADOW" not in done.stderr, (
        "a module in the launcher's own directory shadowed the stdlib: an import runs "
        "BEFORE the sys.path purge\n" + done.stderr
    )
    assert (done.returncode, done.stderr) == (1, "sandbox_launcher: no command given\n")


def _blocked(tmp_path: Path, _capfd: pytest.CaptureFixture[str]) -> str | None:
    """A ``sandbox: BLOCKED`` refusal: the propagation mount on ``/`` failing."""
    run = launch(tmp_path, libc=RecordingLibc(fail_at=1))
    c2p_r, c2p_w = os.pipe()
    p2c_r, p2c_w = os.pipe()
    os.write(p2c_w, b"x")
    try:
        # The stage closes its own two ends once the parent has answered, before the
        # mount it refuses on.
        return refusal(program.enter_namespaces, run, c2p_w, p2c_r)
    finally:
        os.close(c2p_r)
        os.close(p2c_w)


def _unshare(tmp_path: Path, _capfd: pytest.CaptureFixture[str]) -> str | None:
    """A ``sandbox: unshare(`` refusal: the user namespace cannot be created."""

    class _Refusing(RecordingLibc):
        def unshare(self, flags):  # noqa: ANN001, ANN201
            self.unshared.append(flags)
            return -1

    run = launch(tmp_path, libc=_Refusing())
    c2p_r, c2p_w = os.pipe()
    p2c_r, p2c_w = os.pipe()
    try:
        # Refused before the parent is signalled, so every end is still open here.
        return refusal(program.enter_namespaces, run, c2p_w, p2c_r)
    finally:
        for fd in (c2p_r, c2p_w, p2c_r, p2c_w):
            os.close(fd)


def _fatal(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> str | None:
    """A ``sandbox: FATAL`` line: ``~/.ssh/known_hosts`` exists and cannot be read."""
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    known = ssh / "known_hosts"
    known.write_text("h", encoding="utf-8")
    known.chmod(0)
    try:
        if os.access(known, os.R_OK):
            pytest.skip("this host can read a mode-0 file")
        run = launch(tmp_path, payload(hide_ssh=1, ssh_dir=str(ssh), ssh_known_hosts=str(known)))
        with pytest.raises(PermissionError):
            program.mask_ssh_keys(run)
    finally:
        known.chmod(0o600)
    return capfd.readouterr().err


def _no_command(tmp_path: Path, _capfd: pytest.CaptureFixture[str]) -> str | None:
    """A ``sandbox_launcher:`` refusal: the launcher started with no agent command."""
    return refusal(program.main, payload(), RecordingLibc(), [])


#: Each prefix the probe classifies on, and a launcher failure that emits it.
_EMITTERS = {
    "sandbox: BLOCKED": _blocked,
    "sandbox: FATAL": _fatal,
    "sandbox: unshare(": _unshare,
    "sandbox_launcher:": _no_command,
}


def test_every_probe_failure_marker_has_a_launcher_failure_behind_it() -> None:
    """The prefix list and the failures below are one set, so a new prefix gets one too."""
    from kiro_crew.apps.builtins.auto_improvement.backend.clone_setup import (
        _LAUNCHER_EXIT_PREFIXES,
    )

    assert set(_EMITTERS) == set(_LAUNCHER_EXIT_PREFIXES)


@pytest.mark.parametrize("prefix", sorted(_EMITTERS))
def test_probe_failure_markers_round_trip_against_the_launcher(
    prefix: str, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The probe's launcher-failure signature must track what the launcher emits.

    ``clone_setup._LAUNCHER_EXIT_PREFIXES`` classifies a nonzero probe exit as a
    launcher failure by the line it starts with; each prefix must start a line a real
    launcher failure writes, or the list has drifted and real launcher deaths fall back
    to the misleading push-isolation refusal this pairing exists to prevent.
    """
    emit = _EMITTERS[prefix]
    output = emit(tmp_path, capfd)
    assert output is not None, f"the launcher failure behind {prefix!r} did not happen"
    lines = output.splitlines()
    assert lines and lines[0].startswith(prefix), (
        f"no launcher failure line starts with the probe marker {prefix!r}: {output!r} -- "
        "update clone_setup._LAUNCHER_EXIT_PREFIXES together with the launcher"
    )
    if prefix == "sandbox: BLOCKED":
        assert "errno %d" % errno.EPERM in output


def test_the_traceback_marker_matches_the_launcher_filename() -> None:
    """The traceback-frame marker keys on the launcher's on-disk filename.

    A ROUND TRIP against a frame built from the two constants ``namespace_argv``'s
    ``mkstemp`` is passed -- never a pin on how ``sandbox.py`` spells that call, which
    would fail on any refactor of the writer while real drift went unnoticed.
    """
    from kiro_crew.apps.builtins.auto_improvement.backend.clone_setup import (
        _LAUNCHER_TRACEBACK_RE,
    )
    from kiro_crew.sandbox import (
        _LAUNCHER_SCRIPT_SUFFIX,
        _SANDBOX_ARTIFACT_PREFIX,
        namespace_launcher_script_dir,
    )

    launcher_path = os.path.join(
        namespace_launcher_script_dir(),
        f"{_SANDBOX_ARTIFACT_PREFIX}4242_ab12cd{_LAUNCHER_SCRIPT_SUFFIX}",
    )
    frame = f'  File "{launcher_path}", line 1, in <module>'
    assert _LAUNCHER_TRACEBACK_RE.search(frame) is not None, (
        "the launcher's tempfile name does not match the traceback regex in "
        f"clone_setup (_LAUNCHER_TRACEBACK_RE); frame was {frame!r}"
    )
    # And it is still a TRACEBACK-frame match, not a bare substring: a repository may
    # legally be named after the prefix, which puts it in a clone path git echoes.
    assert _LAUNCHER_TRACEBACK_RE.search(f"fatal: could not read {launcher_path}") is None
    # The regex is keyed on THAT prefix and not on any crew-looking name, so a frame from
    # a differently-named file is not classified as a launcher death.
    other = launcher_path.replace(_SANDBOX_ARTIFACT_PREFIX, "kirocrew_other_")
    assert _LAUNCHER_TRACEBACK_RE.search(f'  File "{other}", line 1, in <module>') is None
