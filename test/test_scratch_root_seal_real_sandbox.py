"""The sealed scratch root, inside the product's own Linux sandbox.

The test process plays the gateway and starts children through ``sandbox.namespace_argv``,
each with the windows a dedicated sub-agent gets: its own scratch directory and its session
tree's directory. Under a real kernel the read-only remount of the scratch root's stand-in
must refuse every create outside the windows with EROFS, leave both windows read-write and
visible to the next process and to the host, and keep the root readable. Each child reports
its mount namespace and the launcher's marker, so a run that never entered the sandbox
fails instead of passing on the host's own view.

Every launch here names no stand-in root, so the launcher takes its own temp directory, a
directory under ``tmp_path``: nothing is made in the host-wide ``/run/user/$UID`` or
``/dev/shm``, so a killed run leaves nothing outside the run's own temp root. The launcher
mounts its private stand-in stage there (``stage_stand_ins_privately``), a tmpfs of this
namespace's own, so the seal is proven on the tmpfs production seals, and the writer checks
that the sealed mount kept its nosuid/nodev.
``test_the_launch_keeps_every_stand_in_in_the_per_test_root`` pins that without a namespace,
so it runs on every Linux host. A launch whose stage is refused
on a host root keeps its stand-ins on a tmpfs whose nosuid/nodev the user namespace locks; the
seal there is ``test/test_scratch_root_seal_locked_flags_real_sandbox.py``'s nested-namespace
test, in the same CI job.

Every launch asks the launcher for its mount report and prints the launch's whole stderr,
so a failed run shows the full ``sandbox: BLOCKED`` line and which mount each sealed path
reached inside the namespace. The mount model ``test_scratch_root_seal_stage_mounts.py``
judges the launcher's order by is checked against this kernel at the end of this file.

Linux needs unprivileged user and mount namespaces, which the namespace-sandbox CI job
provides and the shard runners do not, so there the real-kernel tests skip with that reason.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_sandbox_launcher_program import rendered_payload
from test_scratch_root_seal_stage_mounts import KERNEL_ANSWERS, PRIMITIVES

from kiro_crew import sandbox, sandbox_launcher
from kiro_crew.config.loader_locations import config_path
from kiro_crew.sandbox import userns_available
from kiro_crew.testing.wait import default_timeout

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="the namespace launcher is Linux-only"
)

_REAL_KERNEL = pytest.mark.skipif(
    not userns_available(),
    reason="this host cannot build the namespace sandbox (no unprivileged user and mount "
    "namespaces)",
)

# The writer: a sub-agent with its own window and the tree's window.
_WRITER = r"""
import errno, json, os, sys, tempfile
root, own, tree, stale = sys.argv[1:5]
def attempt(step):
    try:
        return {"ok": True, "value": step()}
    except OSError as exc:
        return {"ok": False, "errno": errno.errorcode.get(exc.errno, str(exc.errno))}
def write(path, text):
    with open(path, "w") as fh:
        fh.write(text)
    return text
def make_temp():
    fd, path = tempfile.mkstemp(dir=own)
    os.close(fd)
    return os.path.dirname(path)
def readonly(path):
    return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
def top_mount(path):
    found = None
    with open("/proc/self/mountinfo") as fh:
        for line in fh:
            fields = line.split()
            if fields[4] == path:
                found = {"fstype": fields[fields.index("-") + 1],
                         "options": sorted(fields[5].split(","))}
    return found
report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "readonly": {name: attempt(lambda p=p: readonly(p)) for name, p in
                 (("root", root), ("own", own), ("tree", tree))},
    "stat": {name: attempt(lambda p=p: os.stat(p).st_ino > 0) for name, p in
             (("root", root), ("own", own), ("tree", tree))},
    "realpath": {name: attempt(lambda p=p: os.path.realpath(p, strict=True)) for name, p in
                 (("root", root), ("own", own), ("tree", tree))},
    "root_listing": attempt(lambda: sorted(os.listdir(root))),
    "root_mount": attempt(lambda: top_mount(root)),
    "own_write": attempt(lambda: write(os.path.join(own, "own.md"), "own")),
    "own_mkdir": attempt(lambda: os.mkdir(os.path.join(own, "sub")) or "made"),
    "tree_write": attempt(lambda: write(os.path.join(tree, "shared.md"), "shared")),
    "temp_dir": attempt(make_temp),
    "root_mkdir": attempt(lambda: os.mkdir(os.path.join(root, "new-dir"))),
    "root_file": attempt(lambda: write(os.path.join(root, "new-file"), "lost")),
    "stale": attempt(lambda: os.makedirs(os.path.dirname(stale))),
}
print("REPORT " + json.dumps(report), flush=True)
"""

# A reader: another process of the same tree, or the writer's own respawn.
_READER = r"""
import errno, json, os, sys
root, mine, tree, stale = sys.argv[1:5]
def attempt(step):
    try:
        return {"ok": True, "value": step()}
    except OSError as exc:
        return {"ok": False, "errno": errno.errorcode.get(exc.errno, str(exc.errno))}
def read(path):
    with open(path) as fh:
        return fh.read()
report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "root_listing": attempt(lambda: sorted(os.listdir(root))),
    "shared": attempt(lambda: read(os.path.join(tree, "shared.md"))),
    "mine": attempt(lambda: read(os.path.join(mine, "own.md"))),
    "stale": attempt(lambda: read(stale)),
}
print("REPORT " + json.dumps(report), flush=True)
"""

# An agent that inherited temp and log paths from the gateway's own environment.
_INHERITOR = r"""
import errno, json, os, sys, tempfile
scratch = os.environ.get("KIROCREW_SCRATCH", "")
def attempt(step):
    try:
        return {"ok": True, "value": step()}
    except OSError as exc:
        return {"ok": False, "errno": errno.errorcode.get(exc.errno, str(exc.errno))}
def make_temp():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    os.unlink(path)
    return os.path.dirname(path)
report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "env": {key: os.environ.get(key) for key in
            ("TMPDIR", "TMP", "TEMP", "KIRO_CHAT_LOG_FILE", "KIROCREW_SCRATCH")},
    "temp_dir": attempt(make_temp),
    "scratch_write": attempt(lambda: os.makedirs(os.path.join(scratch, "briefs"))),
}
print("REPORT " + json.dumps(report), flush=True)
"""


# Loads a rendered launcher by path, without running it, and creates one stand-in the way
# the launcher does: no namespace and no mount is needed for that.
_STAND_IN_PROBE = r"""
import importlib.util, sys
spec = importlib.util.spec_from_file_location("rendered_launcher", sys.argv[1])
program = importlib.util.module_from_spec(spec)
spec.loader.exec_module(program)
launch = program.Launch(program._PLAN, None)
program.pick_stand_in_root(launch)
print(program._stand_in_mkdtemp(launch))
"""


@pytest.fixture(params=["relocated", "default"])
def home(request, tmp_path, monkeypatch) -> Path:
    """The data home, at a relocated ``KIROCREW_HOME`` or at ``$HOME/.kiro/crew``.

    The two layouts reach the scratch root through different spellings in the plan: the
    relocated home through ``_relocated_crew_targets``, the default one through the
    ``$HOME``-joined tier entry.
    """
    if request.param == "relocated":
        home = (tmp_path / "home").resolve()
    else:
        home = (tmp_path / ".kiro" / "crew").resolve()
    home.mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    # Spawn prep also sweeps the ``$HOME``-joined crew homes, so ``$HOME`` is pinned too: no
    # step of the launch may touch the developer's real data home.
    monkeypatch.setenv("HOME", str(tmp_path.resolve()))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    sandbox.reset_backend()
    yield home
    sandbox.reset_backend()


@dataclass(frozen=True)
class _StandIns:
    """Where a launch's stand-ins go, and the temp directory its children are given."""

    #: The directory every stand-in is created in: the launcher's own temp directory.
    root: Path
    #: The ``TMPDIR`` the launch environment names, under ``tmp_path``.
    temp: Path


@pytest.fixture
def stand_ins(tmp_path, monkeypatch) -> Iterator[_StandIns]:
    """Where the launcher creates its stand-ins: a directory under ``tmp_path``.

    The plan names ``/run/user/<uid>`` and ``/dev/shm`` themselves, which are host-wide and
    which a test must not fill, so here it names no stand-in root. The launcher then uses
    its own temp directory, which ``_launch_env`` names under ``tmp_path``: it mounts its
    private stand-in stage there, a tmpfs of this namespace's own, and keeps the stand-ins
    themselves there if that stage is refused. Nothing outside the run's own temp root is
    made, so a killed run leaves nothing the residue guard cannot see. The seal on a
    host-root tmpfs whose flags the user namespace locks is the locked-flags test's.
    """
    temp = (tmp_path / "stand-ins").resolve()
    temp.mkdir()
    _plan_stand_in_roots(monkeypatch, [])
    yield _StandIns(root=temp, temp=temp)


def _plan_stand_in_roots(monkeypatch: pytest.MonkeyPatch, roots: list[str]) -> None:
    """Every launch this test renders names *roots* as its stand-in roots."""
    planned = sandbox_launcher.namespace_payload

    def _with_roots(plan: Any) -> dict[str, Any]:
        payload = planned(plan)
        payload["stand_in_roots"] = list(roots)
        return payload

    monkeypatch.setattr(sandbox_launcher, "namespace_payload", _with_roots)


def _launch_env(stand_ins: Path, **extra: str) -> dict[str, str]:
    """The launch environment: the test's own, with temp pointing at *stand_ins*.

    It asks the launcher for its mount report (``report_mount_view``), so a failed run shows
    which mount each sealed path reaches from inside the namespace.
    """
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    for key in ("TMPDIR", "TMP", "TEMP", "KIRO_CHAT_LOG_FILE", "KIROCREW_SCRATCH"):
        env.pop(key, None)
    env["TMPDIR"] = str(stand_ins)
    env["KIROCREW_SANDBOX_MOUNT_REPORT"] = "1"
    env.update(extra)
    return env


def _launched(argv: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run a sandboxed launch, and print its exit status and its whole stderr.

    The stderr holds any ``sandbox: BLOCKED`` line and the launcher's mount report, uncut,
    where pytest shows a failed test's output (and ``run.sh``'s ``-s`` shows every test's).
    """
    proc = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
    )
    print("\nlaunch exit %d; its stderr:\n%s" % (proc.returncode, proc.stderr), end="")
    return proc


def _argv(script: str, args: list[str], tier: str, windows: tuple[str, ...]) -> list[str]:
    return sandbox.namespace_argv(
        [sys.executable, "-c", script, *args], tier, extra_private_dirs=windows
    )


#: What the launcher prints when the host refuses its private stand-in stage's tmpfs.
_STAGE_REFUSED = "could not stage the stand-ins"


def _run(
    script: str,
    args: list[str],
    home: Path,
    tier: str,
    windows: tuple[str, ...],
    env: dict[str, str],
    edit_launcher: Callable[[str], str] | None = None,
) -> dict:
    """Run *script* in a real sandbox and return its report.

    The report gains ``stage_refused``: whether the launcher said the host refused its
    private stand-in stage. *edit_launcher*, when given, rewrites the rendered launcher's
    text before it runs.
    """
    argv = _argv(script, args, tier, windows)
    if edit_launcher is not None:
        (launcher,) = [Path(a) for a in argv if os.path.basename(a).startswith("kirocrew_sandbox_")]
        launcher.write_text(edit_launcher(launcher.read_text(encoding="utf-8")), encoding="utf-8")
    proc = _launched(argv, home, env)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    (line,) = [ln for ln in proc.stdout.splitlines() if ln.startswith("REPORT ")]
    report = json.loads(line[len("REPORT ") :])
    assert report["marker"] == "1", output
    assert report["mnt_ns"] != os.readlink("/proc/self/ns/mnt"), output
    report["stage_refused"] = _STAGE_REFUSED in proc.stderr
    return report


def _scratch(home: Path) -> tuple[Path, Path, Path, Path]:
    root = home / "scratch"
    tree = root / "runtime-tree"
    own_a = root / "subagent-a-11111111"
    own_b = root / "subagent-b-22222222"
    for directory in (tree, own_a, own_b):
        directory.mkdir(parents=True)
    return root, tree, own_a, own_b


def _inside(path: str, parent: Path) -> bool:
    return Path(path).resolve().is_relative_to(parent.resolve())


def test_the_launch_keeps_every_stand_in_in_the_per_test_root(home, stand_ins, tmp_path):
    """No namespace needed: the payload the real tests send names only the per-test root.

    The rendered program is loaded in a child started the way the gateway starts it
    (``python -I -S``, the launch environment, a working directory of the test's own) and
    asked where its first stand-in lands, which is where its private stage is made.
    """
    root, tree, own_a, _own_b = _scratch(home)
    argv = _argv("pass", [], "standard", (str(own_a), str(tree)))
    (script,) = [a for a in argv if os.path.basename(a).startswith("kirocrew_sandbox_")]
    assert _inside(script, tmp_path)
    plan = rendered_payload(Path(script).read_text(encoding="utf-8"))
    assert plan["stand_in_roots"] == []
    assert str(root) in plan["seal_windowed_masks"]
    probe = tmp_path / "stand_in_probe.py"
    probe.write_text(_STAND_IN_PROBE, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-I", "-S", str(probe), script],
        cwd=str(tmp_path),
        env=_launch_env(stand_ins.temp),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    landed = proc.stdout.strip()
    assert Path(landed).parent == stand_ins.root, landed
    assert os.path.basename(landed).startswith("kirocrew_sb_")


@_REAL_KERNEL
@pytest.mark.parametrize("tier", ["standard", "strict", "cc"])
def test_a_write_outside_the_windows_fails_and_the_windows_stay_writable(home, stand_ins, tier):
    root, tree, own_a, own_b = _scratch(home)
    stale = root / "runtime-e748c943" / "shared" / "briefs" / "brief.md"
    args = [str(root), str(own_a), str(tree), str(stale)]
    env = _launch_env(stand_ins.temp)

    writer = _run(_WRITER, args, home, tier, (str(own_a), str(tree)), env)
    print("\nwriter:", json.dumps(writer))

    # The stand-in is read-only; the windows are not, and all three still read normally.
    assert writer["readonly"] == {
        "root": {"ok": True, "value": True},
        "own": {"ok": True, "value": False},
        "tree": {"ok": True, "value": False},
    }
    assert all(entry == {"ok": True, "value": True} for entry in writer["stat"].values())
    # The mount over the root is the sealed stand-in, read-only whatever it sits on.
    root_mount = writer["root_mount"]["value"]
    assert root_mount is not None and "ro" in root_mount["options"], writer["root_mount"]
    if not writer["stage_refused"]:
        # Made on the private stage, a nosuid,nodev tmpfs, which kept both bits through
        # the seal and was detached without taking the stand-in along.
        assert root_mount["fstype"] == "tmpfs", root_mount
        assert {"nosuid", "nodev"} <= set(root_mount["options"]), root_mount
    else:
        # Made on the stand-in root itself: the seal re-asserted that filesystem's bits.
        root_flags = os.statvfs(stand_ins.root).f_flag
        for bit, option in ((os.ST_NOSUID, "nosuid"), (os.ST_NODEV, "nodev")):
            assert bool(root_flags & bit) == (option in root_mount["options"]), root_mount
    assert writer["realpath"] == {
        "root": {"ok": True, "value": str(root)},
        "own": {"ok": True, "value": str(own_a)},
        "tree": {"ok": True, "value": str(tree)},
    }
    assert writer["root_listing"] == {"ok": True, "value": sorted([own_a.name, tree.name])}
    # Writes in each window succeed, a temp file included.
    assert writer["own_write"] == {"ok": True, "value": "own"}
    assert writer["own_mkdir"] == {"ok": True, "value": "made"}
    assert writer["tree_write"] == {"ok": True, "value": "shared"}
    assert writer["temp_dir"] == {"ok": True, "value": str(own_a)}
    # Every create outside them fails loudly.
    erofs = {"ok": False, "errno": "EROFS"}
    assert writer["root_mkdir"] == erofs
    assert writer["root_file"] == erofs
    assert writer["stale"] == erofs

    # Another process of the tree sees the shared write and none of the writer's own.
    reader_args = [str(root), str(own_a), str(tree), str(stale)]
    reader = _run(_READER, reader_args, home, tier, (str(own_b), str(tree)), env)
    print("reader:", json.dumps(reader))
    assert reader["shared"] == {"ok": True, "value": "shared"}
    assert reader["mine"] == {"ok": False, "errno": "ENOENT"}
    assert reader["stale"] == {"ok": False, "errno": "ENOENT"}
    assert reader["root_listing"] == {"ok": True, "value": sorted([own_b.name, tree.name])}

    # The writer's respawn, which mounts its own directory again, sees its own write.
    respawn = _run(_READER, reader_args, home, tier, (str(own_a), str(tree)), env)
    assert respawn["mine"] == {"ok": True, "value": "own"}
    assert respawn["shared"] == {"ok": True, "value": "shared"}

    # The host has both window writes on disk and nothing the refused creates named.
    assert (own_a / "own.md").read_text() == "own"
    assert (own_a / "sub").is_dir()
    assert (tree / "shared.md").read_text() == "shared"
    assert sorted(p.name for p in root.iterdir()) == sorted([own_a.name, own_b.name, tree.name])
    print(
        "stand-in root:",
        stand_ins.root,
        "stage refused:",
        writer["stage_refused"],
        "sealed root mount:",
        root_mount,
    )


@_REAL_KERNEL
def test_an_inherited_temp_path_under_the_root_is_dropped_and_the_tool_falls_back(home, stand_ins):
    """A temp or log path the gateway inherited, under the sealed root outside every window.

    The launcher drops it, so the tool uses its own default instead of failing with EROFS.
    A temp path inside the spawn's own window is kept, and ``KIROCREW_SCRATCH`` is kept
    so a deliberate write there fails at the path the agent named.
    """
    root, tree, own_a, _own_b = _scratch(home)
    stale = root / "runtime-e748c943"
    env = _launch_env(
        stand_ins.temp,
        TMP=str(stale),
        TEMP=str(own_a),
        KIRO_CHAT_LOG_FILE=str(stale / "kiro-log" / "kiro-chat.log"),
        KIROCREW_SCRATCH=str(stale),
    )

    report = _run(_INHERITOR, [], home, "standard", (str(own_a),), env)
    print("\ninheritor:", json.dumps(report))

    assert report["env"] == {
        "TMPDIR": str(stand_ins.temp),
        "TMP": None,
        "TEMP": str(own_a),
        "KIRO_CHAT_LOG_FILE": None,
        "KIROCREW_SCRATCH": str(stale),
    }
    assert report["temp_dir"] == {"ok": True, "value": str(stand_ins.temp)}
    assert report["scratch_write"] == {"ok": False, "errno": "EROFS"}
    assert not stale.exists()


# An agent whose own window was the launcher's only temporary directory.
_WINDOW_TEMP = r"""
import errno, json, os, sys, tempfile
root, own = sys.argv[1:3]
def attempt(step):
    try:
        return {"ok": True, "value": step()}
    except OSError as exc:
        return {"ok": False, "errno": errno.errorcode.get(exc.errno, str(exc.errno))}
def write(path, text):
    with open(path, "w") as fh:
        fh.write(text)
    return text
report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "own_listing": attempt(lambda: sorted(os.listdir(own))),
    "own_write": attempt(lambda: write(os.path.join(own, "own.md"), "own")),
    "root_file": attempt(lambda: write(os.path.join(root, "new-file"), "lost")),
}
print("REPORT " + json.dumps(report), flush=True)
"""

#: Put in front of the rendered launcher's plan line: its ``tempfile`` then knows no
#: directory but the temp variables, as on a host where ``/tmp``, ``/var/tmp``, ``/usr/tmp``
#: and the working directory are all unusable.
_ONLY_THE_TEMP_VARIABLES = (
    "tempfile._candidate_tempdir_list = lambda: "
    "[v for v in (os.environ.get(k) for k in ('TMPDIR', 'TEMP', 'TMP')) if v]\n"
)


def _only_the_temp_variables(text: str) -> str:
    assert text.count("\n_PLAN = {") == 1
    return text.replace("\n_PLAN = {", "\n" + _ONLY_THE_TEMP_VARIABLES + "_PLAN = {")


def test_the_edited_launcher_takes_the_own_window_as_its_temp(home, monkeypatch, tmp_path):
    """No namespace needed: the edit below leaves the launcher only the window to use.

    So the real-kernel test after this one exercises the window, not ``/tmp``.
    """
    root, tree, own_a, _own_b = _scratch(home)
    _plan_stand_in_roots(monkeypatch, [])
    argv = _argv("pass", [], "standard", (str(own_a), str(tree)))
    (script,) = [Path(a) for a in argv if os.path.basename(a).startswith("kirocrew_sandbox_")]
    script.write_text(_only_the_temp_variables(script.read_text(encoding="utf-8")))
    assert rendered_payload(script.read_text(encoding="utf-8"))["stand_in_roots"] == []
    probe = tmp_path / "stand_in_probe.py"
    probe.write_text(_STAND_IN_PROBE, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-I", "-S", str(probe), str(script)],
        cwd=str(tmp_path),
        env=_launch_env(own_a, TMP=str(own_a), TEMP=str(own_a)),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert Path(proc.stdout.strip()).parent == own_a, proc.stdout
    assert str(root) in rendered_payload(script.read_text(encoding="utf-8"))["seal_windowed_masks"]


@_REAL_KERNEL
@pytest.mark.parametrize("tier", ["standard", "strict"])
def test_with_no_other_temp_dir_the_own_window_holds_the_stage_and_keeps_nothing(
    home, monkeypatch, tier
):
    """No stand-in root and no system temp dir: the window ``scratch_env`` names is used.

    The launcher makes its private stage there, every stand-in on that stage, and retires
    the stage through the window bind, so the agent and the host find nothing of it in the
    window, the window stays writable, and the root stays sealed.
    """
    root, tree, own_a, _own_b = _scratch(home)
    _plan_stand_in_roots(monkeypatch, [])
    env = _launch_env(own_a, TMP=str(own_a), TEMP=str(own_a))

    report = _run(
        _WINDOW_TEMP,
        [str(root), str(own_a)],
        home,
        tier,
        (str(own_a), str(tree)),
        env,
        edit_launcher=_only_the_temp_variables,
    )
    print("\nwindow temp:", json.dumps(report))

    assert report["stage_refused"] is False, "this proof needs the private stage"
    assert report["own_listing"] == {"ok": True, "value": []}
    assert report["own_write"] == {"ok": True, "value": "own"}
    assert report["root_file"] == {"ok": False, "errno": "EROFS"}
    assert sorted(p.name for p in own_a.iterdir()) == ["own.md"]


# An agent that looks for anything of the launcher's stage in a directory the layer holds.
_HELD_TEMP = r"""
import errno, json, os, sys
root, own, held = sys.argv[1:4]
def attempt(step):
    try:
        return {"ok": True, "value": step()}
    except OSError as exc:
        return {"ok": False, "errno": errno.errorcode.get(exc.errno, str(exc.errno))}
def write(path, text):
    with open(path, "w") as fh:
        fh.write(text)
    return text
def mounts_of(path):
    with open("/proc/self/mountinfo") as fh:
        rows = [line.split() for line in fh]
    at = [{"root": r[3], "options": sorted(r[5].split(","))} for r in rows if r[4] == path]
    below = sorted(r[4] for r in rows if r[4].startswith(path + "/"))
    return {"at": at, "below": below}
report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "held_listing": attempt(lambda: sorted(os.listdir(held))),
    "held_mounts": attempt(lambda: mounts_of(held)),
    "own_write": attempt(lambda: write(os.path.join(own, "own.md"), "own")),
    "root_file": attempt(lambda: write(os.path.join(root, "new-file"), "lost")),
}
print("REPORT " + json.dumps(report), flush=True)
"""


def _system_temp_only(held: Path) -> Callable[[str], str]:
    """An edit that makes *held* the rendered launcher's only system temp directory."""
    line = (
        "tempfile._candidate_tempdir_list = lambda: "
        "[v for v in (os.environ.get(k) for k in ('TMPDIR', 'TEMP', 'TMP')) if v] + [%r]\n"
        "_TEMP_PLATFORM_DIRS = (%r,)\n" % (str(held), str(held))
    )

    def edit(text: str) -> str:
        assert text.count("\n_PLAN = {") == 1
        return text.replace("\n_PLAN = {", "\n" + line + "_PLAN = {")

    return edit


def test_the_edited_launcher_with_a_held_system_temp_takes_the_own_window(
    home, monkeypatch, tmp_path
):
    """No namespace needed: with the edit below and the layer set, the window holds the stage.

    So the real-kernel test after this one exercises the window, not the held directory.
    """
    root, tree, own_a, _own_b = _scratch(home)
    _plan_stand_in_roots(monkeypatch, [])
    held = (tmp_path / "system-tmp").resolve()
    held.mkdir()
    _configure_readonly_layer([held], [])
    argv = _argv("pass", [], "standard", (str(own_a), str(tree)))
    (script,) = [Path(a) for a in argv if os.path.basename(a).startswith("kirocrew_sandbox_")]
    script.write_text(_system_temp_only(held)(script.read_text(encoding="utf-8")))
    assert rendered_payload(script.read_text(encoding="utf-8"))["operator_readonly_dirs"] == [
        str(held)
    ]
    probe = tmp_path / "stand_in_probe.py"
    probe.write_text(_STAND_IN_PROBE, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-I", "-S", str(probe), str(script)],
        cwd=str(tmp_path),
        env=_launch_env(own_a, TMP=str(own_a), TEMP=str(own_a)),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert Path(proc.stdout.strip()).parent == own_a, proc.stdout
    assert list(held.iterdir()) == []
    assert str(root) in rendered_payload(script.read_text(encoding="utf-8"))["seal_windowed_masks"]


@_REAL_KERNEL
def test_a_default_temp_dir_inside_an_operator_entry_leaves_the_stage_to_the_own_window(
    home, monkeypatch, tmp_path
):
    """``agent.sandbox_readonly_paths`` holds the system temp dir; ``scratch_env`` names the window.

    Made in the system temp dir, the stage would be copied read-only by the layer, and the
    copy, a read-only view of every window's real tree, would stay mounted there once the
    stage is detached. The launcher takes the window instead, so nothing of the stage is in
    that directory or mounted below it. The one mount at the directory itself is the layer's
    own read-only bind of it: its root is the directory, where a copy of the stage would be
    the root of the stage's tmpfs.
    """
    root, tree, own_a, _own_b = _scratch(home)
    _plan_stand_in_roots(monkeypatch, [])
    held = (tmp_path / "system-tmp").resolve()
    held.mkdir()
    _configure_readonly_layer([held], [])
    env = _launch_env(own_a, TMP=str(own_a), TEMP=str(own_a))
    report = _run(
        _HELD_TEMP,
        [str(root), str(own_a), str(held)],
        home,
        "standard",
        (str(own_a), str(tree)),
        env,
        edit_launcher=_system_temp_only(held),
    )
    print("\nheld temp:", json.dumps(report))
    assert report["stage_refused"] is False, "this proof needs the private stage"
    assert report["held_listing"] == {"ok": True, "value": []}
    assert report["held_mounts"]["ok"], report["held_mounts"]
    mounts = report["held_mounts"]["value"]
    assert mounts["below"] == [], "a copy of the stage is mounted below the held directory"
    (layer,) = mounts["at"]
    assert "ro" in layer["options"], layer
    assert layer["root"].endswith("/" + held.name), layer
    assert report["own_write"] == {"ok": True, "value": "own"}
    assert report["root_file"] == {"ok": False, "errno": "EROFS"}
    assert list(held.iterdir()) == []


# An agent whose data home sits inside a credential directory another mask hides.
_CONTAINED_WRITER = r"""
import errno, json, os, sys
root, own, tree, aws = sys.argv[1:5]
def attempt(step):
    try:
        return {"ok": True, "value": step()}
    except OSError as exc:
        return {"ok": False, "errno": errno.errorcode.get(exc.errno, str(exc.errno))}
def write(path, text):
    with open(path, "w") as fh:
        fh.write(text)
    return text
report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "root_readonly": attempt(lambda: bool(os.statvfs(root).f_flag & os.ST_RDONLY)),
    "root_listing": attempt(lambda: sorted(os.listdir(root))),
    "own_write": attempt(lambda: write(os.path.join(own, "own.md"), "own")),
    "tree_write": attempt(lambda: write(os.path.join(tree, "shared.md"), "shared")),
    "root_file": attempt(lambda: write(os.path.join(root, "new-file"), "lost")),
    "stale": attempt(lambda: os.makedirs(os.path.join(root, "runtime-stale", "briefs"))),
    "aws_listing": attempt(lambda: sorted(os.listdir(aws))),
}
print("REPORT " + json.dumps(report), flush=True)
"""

#: ``(tier, the credential home as a caller mask, the containing mask placed before the
#: root)``: a Codex session masks ``~/.aws`` after every tier mask; the strict and cc tiers
#: list it themselves, ahead of the relocated scratch root.
_CONTAINERS = [("standard", True, False), ("strict", False, True), ("cc", False, True)]
_CONTAINER_IDS = ["codex mask after the root", "strict tier before it", "cc tier before it"]


@pytest.fixture
def aws_home(tmp_path, monkeypatch) -> Path:
    """``KIROCREW_HOME`` at ``~/.aws/crew``, with a credential beside it."""
    aws = (tmp_path / ".aws").resolve()
    home = aws / "crew"
    home.mkdir(parents=True)
    (aws / "credentials").write_text("[default]\n", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path.resolve()))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    sandbox.reset_backend()
    yield home
    sandbox.reset_backend()


def _contained_argv(script, args, tier, windows, codex_mask, aws):
    return sandbox.namespace_argv(
        [sys.executable, "-c", script, *args],
        tier,
        extra_private_dirs=windows,
        extra_hidden_dirs=(str(aws),) if codex_mask else (),
    )


@pytest.mark.parametrize(("tier", "codex_mask", "container_first"), _CONTAINERS, ids=_CONTAINER_IDS)
def test_a_data_home_under_aws_renders_a_mask_that_contains_the_root(
    aws_home, stand_ins, tier, codex_mask, container_first
):
    """No namespace needed: the launcher the real test below runs has that mask in its list."""
    aws = aws_home.parent
    root, tree, own_a, _own_b = _scratch(aws_home)
    argv = _contained_argv("pass", [], tier, (str(own_a), str(tree)), codex_mask, aws)
    (script,) = [a for a in argv if os.path.basename(a).startswith("kirocrew_sandbox_")]
    plan = rendered_payload(Path(script).read_text(encoding="utf-8"))
    dirs = plan["sensitive_dirs"]
    assert str(root) in plan["seal_windowed_masks"]
    assert (dirs.index(str(aws)) < dirs.index(str(root))) == container_first


@_REAL_KERNEL
@pytest.mark.parametrize(("tier", "codex_mask", "container_first"), _CONTAINERS, ids=_CONTAINER_IDS)
def test_a_mask_that_contains_the_root_leaves_its_seal_and_windows_in_place(
    aws_home, stand_ins, tier, codex_mask, container_first
):
    """Under a real kernel the root stays sealed on top of the ``.aws`` mask that contains it."""
    aws = aws_home.parent
    root, tree, own_a, _own_b = _scratch(aws_home)
    env = _launch_env(stand_ins.temp)
    argv = _contained_argv(
        _CONTAINED_WRITER,
        [str(root), str(own_a), str(tree), str(aws)],
        tier,
        (str(own_a), str(tree)),
        codex_mask,
        aws,
    )
    proc = _launched(argv, aws_home, env)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    (line,) = [ln for ln in proc.stdout.splitlines() if ln.startswith("REPORT ")]
    report = json.loads(line[len("REPORT ") :])
    print("\ncontained:", json.dumps(report))
    assert report["marker"] == "1", output
    assert report["mnt_ns"] != os.readlink("/proc/self/ns/mnt"), output
    assert report["root_readonly"] == {"ok": True, "value": True}
    assert report["root_listing"] == {"ok": True, "value": sorted([own_a.name, tree.name])}
    assert report["own_write"] == {"ok": True, "value": "own"}
    assert report["tree_write"] == {"ok": True, "value": "shared"}
    erofs = {"ok": False, "errno": "EROFS"}
    assert report["root_file"] == erofs
    assert report["stale"] == erofs
    assert report["aws_listing"]["ok"] and "credentials" not in report["aws_listing"]["value"]
    assert (own_a / "own.md").read_text() == "own"
    assert (tree / "shared.md").read_text() == "shared"
    assert not (root / "runtime-stale").exists()


#: The line of the rendered launcher that mounts a sealed root back over a mask containing it.
_REATTACH = "        if _reattach:\n            _reattach_sealed_masks(launch, d)\n"


def _without_the_reattach(text: str) -> str:
    assert text.count(_REATTACH) == 1
    return text.replace(_REATTACH, "")


def test_the_rendered_launcher_confirms_its_sealed_roots_and_can_lose_the_reattach(
    aws_home, stand_ins
):
    """No namespace needed: the real test below edits exactly one line of the launcher it runs.

    The rendered launcher checks its sealed roots once every mask is placed, and holds the
    re-attach line once, so taking it out leaves the root under the ``.aws`` stand-in.
    """
    aws = aws_home.parent
    root, tree, own_a, _own_b = _scratch(aws_home)
    argv = _contained_argv("pass", [], "standard", (str(own_a), str(tree)), True, aws)
    (script,) = [a for a in argv if os.path.basename(a).startswith("kirocrew_sandbox_")]
    text = Path(script).read_text(encoding="utf-8")
    assert text.count("\n        confirm_sealed_roots(launch)\n") == 1
    assert _REATTACH not in _without_the_reattach(text)


@_REAL_KERNEL
@pytest.mark.parametrize("reattach", [True, False], ids=["as rendered", "re-attach removed"])
def test_the_last_check_passes_a_sealed_root_and_refuses_one_a_mask_covered(
    aws_home, stand_ins, reattach
):
    """Under a real kernel, with a Codex ``.aws`` mask placed after the root it contains.

    As rendered, the last check passes and the agent runs. With the re-attach taken out the
    ``.aws`` stand-in covers the root, and the check refuses before anything runs, naming it.
    """
    aws = aws_home.parent
    root, tree, own_a, _own_b = _scratch(aws_home)
    argv = _contained_argv(
        _CONTAINED_WRITER,
        [str(root), str(own_a), str(tree), str(aws)],
        "standard",
        (str(own_a), str(tree)),
        True,
        aws,
    )
    if not reattach:
        (launcher,) = [Path(a) for a in argv if os.path.basename(a).startswith("kirocrew_sandbox_")]
        launcher.write_text(
            _without_the_reattach(launcher.read_text(encoding="utf-8")), encoding="utf-8"
        )
    proc = _launched(argv, aws_home, _launch_env(stand_ins.temp))
    output = proc.stdout + proc.stderr
    reports = [ln for ln in proc.stdout.splitlines() if ln.startswith("REPORT ")]
    if reattach:
        assert proc.returncode == 0, output
        (line,) = reports
        report = json.loads(line[len("REPORT ") :])
        assert report["marker"] == "1", output
        assert report["root_readonly"] == {"ok": True, "value": True}
        assert report["root_file"] == {"ok": False, "errno": "EROFS"}
    else:
        assert proc.returncode != 0, output
        assert not reports, output
        # The root's name reaches the directory the ``.aws`` stand-in was given for it.
        assert (
            "BLOCKED -- the sealed root %s is not the sealed stand-in this spawn mounted" % root
            in proc.stderr
        ), output
    assert not (root / "runtime-stale").exists()


@pytest.fixture
def linked_aws_home(tmp_path, monkeypatch) -> Path:
    """``KIROCREW_HOME=~/.aws/crew`` where ``~/.aws`` is a link to ``data-aws``.

    Yields the data home as the gateway resolves it, ``data-aws/crew``: the scratch root is
    then spelled by its real path, and the ``.aws`` mask by the link, so only the resolved
    spellings show that the mask contains the root.
    """
    data = (tmp_path / "data-aws").resolve()
    (data / "crew").mkdir(parents=True)
    (data / "credentials").write_text("[default]\n", encoding="utf-8")
    (tmp_path / ".aws").symlink_to(data)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path.resolve() / ".aws" / "crew"))
    monkeypatch.setenv("HOME", str(tmp_path.resolve()))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    sandbox.reset_backend()
    yield data / "crew"
    sandbox.reset_backend()


@pytest.mark.parametrize(("tier", "codex_mask", "container_first"), _CONTAINERS, ids=_CONTAINER_IDS)
def test_a_data_home_under_a_linked_aws_renders_a_mask_that_holds_the_root_only_through_it(
    linked_aws_home, stand_ins, tmp_path, tier, codex_mask, container_first
):
    """No namespace needed: the launcher the real test below runs spells the two apart."""
    link = tmp_path.resolve() / ".aws"
    root, tree, own_a, _own_b = _scratch(linked_aws_home)
    argv = _contained_argv("pass", [], tier, (str(own_a), str(tree)), codex_mask, link)
    (script,) = [a for a in argv if os.path.basename(a).startswith("kirocrew_sandbox_")]
    plan = rendered_payload(Path(script).read_text(encoding="utf-8"))
    dirs = plan["sensitive_dirs"]
    assert str(root) in plan["seal_windowed_masks"]
    assert not any(str(root).startswith(d.rstrip("/") + "/") for d in dirs if d != str(root))
    assert (dirs.index(str(link)) < dirs.index(str(root))) == container_first


def _run_linked(script, args, home, tier, windows, codex_mask, link, env):
    return _launched(_contained_argv(script, args, tier, windows, codex_mask, link), home, env)


@_REAL_KERNEL
def test_a_codex_mask_that_holds_the_root_through_a_link_refuses_at_a_crew_file_leaf(
    linked_aws_home, stand_ins, tmp_path
):
    """A Codex ``.aws`` mask, a link, is placed after every crew leaf and holds them all.

    Every directory mask is placed before it, the sealed root among them. The single-file
    masks come after every directory mask and are offered each crew leaf again, so the first
    one a pass established (``policy_cache``) is gone beneath the ``.aws`` stand-in, and the
    base launcher's check for a leaf its own mask covers goes by spelling. The spawn refuses
    at that leaf before anything runs, as the base launcher refuses this layout; it never
    stops at the root.
    """
    link = tmp_path.resolve() / ".aws"
    root, tree, own_a, _own_b = _scratch(linked_aws_home)
    proc = _run_linked(
        _CONTAINED_WRITER,
        [str(root), str(own_a), str(tree), str(link)],
        linked_aws_home,
        "standard",
        (str(own_a), str(tree)),
        True,
        link,
        _launch_env(stand_ins.temp),
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert not any(ln.startswith("REPORT ") for ln in proc.stdout.splitlines()), output
    refused = [ln for ln in proc.stderr.splitlines() if "BLOCKED -- cannot pin" in ln]
    assert refused, output
    assert all(str(linked_aws_home) + "/" in ln and str(root) not in ln for ln in refused), output
    assert not (root / "runtime-stale").exists()


@_REAL_KERNEL
@pytest.mark.parametrize("tier", ["strict", "cc"])
def test_a_linked_aws_listed_before_the_crew_leaves_refuses_at_a_leaf_not_at_the_root(
    linked_aws_home, stand_ins, tmp_path, tier
):
    """The tier's own ``.aws`` mask, a link, is placed before the crew leaves it holds.

    The sealed root is found inside it and placed first. The other crew leaves listed after
    ``.aws`` are gone beneath it, and the base launcher's check for a leaf its own mask
    covers goes by spelling, so the spawn refuses at the first of them before anything
    runs, as the base launcher refuses this layout. It never stops at the root.
    """
    link = tmp_path.resolve() / ".aws"
    root, tree, own_a, _own_b = _scratch(linked_aws_home)
    proc = _run_linked(
        _CONTAINED_WRITER,
        [str(root), str(own_a), str(tree), str(link)],
        linked_aws_home,
        tier,
        (str(own_a), str(tree)),
        False,
        link,
        _launch_env(stand_ins.temp),
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert not any(ln.startswith("REPORT ") for ln in proc.stdout.splitlines()), output
    refused = [ln for ln in proc.stderr.splitlines() if "BLOCKED -- cannot pin" in ln]
    assert refused, output
    assert all(str(linked_aws_home) + "/" in ln and str(root) not in ln for ln in refused), output
    assert not (root / "runtime-stale").exists()


# An agent that looks for the sealed root's stand-in among the launcher's own entries, and
# one level below them, where a private stand-in stage that stayed mounted holds them.
_SOURCE_WRITER = r"""
import errno, json, os, sys
root, own, stand_ins = sys.argv[1:4]
def attempt(step):
    try:
        return {"ok": True, "value": step()}
    except OSError as exc:
        return {"ok": False, "errno": errno.errorcode.get(exc.errno, str(exc.errno))}
def write(path, text):
    with open(path, "w") as fh:
        fh.write(text)
    return text
want = os.stat(root)
def same(path):
    st = os.stat(path)
    return (st.st_dev, st.st_ino) == (want.st_dev, want.st_ino)
def ours(parent):
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        return []
    return [os.path.join(parent, name) for name in names if name.startswith("kirocrew_sb_")]
entries = ours(stand_ins)
entries += [p for parent in entries if os.path.isdir(parent) for p in ours(parent)]
sources = [p for p in entries if os.path.isdir(p) and same(p)]
report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "sources": len(sources),
    "source_writes": [attempt(lambda p=p: write(os.path.join(p, "lost"), "lost"))
                      for p in sources],
    "root_file": attempt(lambda: write(os.path.join(root, "new-file"), "lost")),
    "own_write": attempt(lambda: write(os.path.join(own, "own.md"), "own")),
}
print("REPORT " + json.dumps(report), flush=True)
"""

#: The rendered launcher's private-stage mount, and the line that detaches the stage.
_STAGE_MOUNT = (
    'if launch.libc.mount(b"tmpfs", target, b"tmpfs", _MS_NOSUID | _MS_NODEV, b"mode=0700") != 0:'
)
_STAGE_DETACH = (
    'detached = launch.libc.umount2(("/proc/self/fd/%d" % root_fd).encode(), _MNT_DETACH) == 0'
)


def _refuse_the_stage(text: str) -> str:
    """The launcher with its private stand-in stage refused, as on a host that denies its tmpfs."""
    assert text.count(_STAGE_MOUNT) == 1
    return text.replace(_STAGE_MOUNT, "if True:")


def _keep_the_stage(text: str) -> str:
    """The launcher with its private stand-in stage left mounted, as when it will not detach."""
    assert text.count(_STAGE_DETACH) == 1
    return text.replace(_STAGE_DETACH, "detached = False")


def test_the_stage_edits_apply_to_the_rendered_launcher(home, stand_ins):
    """No namespace needed: each edit the real tests below make changes exactly one line."""
    root, tree, own_a, _own_b = _scratch(home)
    argv = _argv("pass", [], "standard", (str(own_a), str(tree)))
    (script,) = [a for a in argv if os.path.basename(a).startswith("kirocrew_sandbox_")]
    text = Path(script).read_text(encoding="utf-8")
    assert _STAGE_MOUNT not in _refuse_the_stage(text)
    assert _STAGE_DETACH not in _keep_the_stage(text)
    assert _STAGE_DETACH not in _keep_the_stage(_only_the_temp_variables(text))
    assert str(root) in rendered_payload(text)["seal_windowed_masks"]


@_REAL_KERNEL
def test_without_the_private_stage_the_stand_ins_own_entry_is_read_only(home, stand_ins):
    """The stage is refused, so the stand-ins are made in the launcher's temp directory.

    The agent finds the sealed root's stand-in there by its identity, and a write through
    that entry fails with EROFS, as one under the root does; its window stays writable.
    """
    root, tree, own_a, _own_b = _scratch(home)
    report = _run(
        _SOURCE_WRITER,
        [str(root), str(own_a), str(stand_ins.temp)],
        home,
        "standard",
        (str(own_a), str(tree)),
        _launch_env(stand_ins.temp),
        edit_launcher=_refuse_the_stage,
    )
    print("\nsource writer:", json.dumps(report))
    assert report["stage_refused"] is True, "this proof needs the stage refused"
    assert report["sources"] == 1, report
    assert report["source_writes"] == [{"ok": False, "errno": "EROFS"}]
    assert report["root_file"] == {"ok": False, "errno": "EROFS"}
    assert report["own_write"] == {"ok": True, "value": "own"}
    assert not any((p / "lost").exists() for p in stand_ins.temp.glob("kirocrew_sb_*"))


@_REAL_KERNEL
def test_a_private_stage_that_does_not_detach_leaves_its_sealed_entry_read_only(home, stand_ins):
    """The stage stays mounted in the launcher's temp directory, so the agent can reach it.

    The spawn runs, as on main; the sealed root's stand-in, found on the stage by its
    identity, takes a write with EROFS, as the root does, and the window stays writable.
    """
    root, tree, own_a, _own_b = _scratch(home)
    report = _run(
        _SOURCE_WRITER,
        [str(root), str(own_a), str(stand_ins.temp)],
        home,
        "standard",
        (str(own_a), str(tree)),
        _launch_env(stand_ins.temp),
        edit_launcher=_keep_the_stage,
    )
    print("\nkept stage:", json.dumps(report))
    assert report["stage_refused"] is False, "this proof needs the stage mounted"
    assert report["sources"] == 1, report
    assert report["source_writes"] == [{"ok": False, "errno": "EROFS"}]
    assert report["root_file"] == {"ok": False, "errno": "EROFS"}
    assert report["own_write"] == {"ok": True, "value": "own"}


@_REAL_KERNEL
def test_a_stage_in_the_own_window_that_does_not_detach_stays_out_of_reach(home, monkeypatch):
    """No other temp directory: the own window holds the stage, and the stage stays mounted.

    The window is bound back without the stage inside it, so no path the agent has reaches
    the sealed root's stand-in; the spawn runs, as on main, and the root stays sealed.
    """
    root, tree, own_a, _own_b = _scratch(home)
    _plan_stand_in_roots(monkeypatch, [])
    report = _run(
        _SOURCE_WRITER,
        [str(root), str(own_a), str(own_a)],
        home,
        "standard",
        (str(own_a), str(tree)),
        _launch_env(own_a, TMP=str(own_a), TEMP=str(own_a)),
        edit_launcher=lambda text: _keep_the_stage(_only_the_temp_variables(text)),
    )
    print("\nkept stage in the window:", json.dumps(report))
    assert report["stage_refused"] is False, "this proof needs the stage mounted"
    assert report["sources"] == 0, report
    assert report["root_file"] == {"ok": False, "errno": "EROFS"}
    assert report["own_write"] == {"ok": True, "value": "own"}


def _configure_readonly_layer(paths: list[Path], exceptions: list[Path]) -> None:
    """Set ``agent.sandbox_readonly_paths`` and ``agent.sandbox_readonly_exceptions``."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    agent = {
        "sandbox_readonly_paths": [str(p) for p in paths],
        "sandbox_readonly_exceptions": [str(p) for p in exceptions],
    }
    path.write_text(json.dumps({"agent": agent}), encoding="utf-8")


@_REAL_KERNEL
@pytest.mark.parametrize("holder", ["data home", "test root"])
def test_an_operator_read_only_entry_over_the_scratch_root_keeps_the_windows_writable(
    home, stand_ins, tmp_path, holder
):
    """An operator read-only directory holds the scratch root: the data home, or ``tmp_path``.

    ``tmp_path`` holds the launcher's temp directory too, so the layer copies the stand-in
    stage read-only. The windows stay writable and shared with the host, every create outside
    them fails with EROFS, inside the scratch root and elsewhere in the read-only entry.
    """
    root, tree, own_a, _own_b = _scratch(home)
    entry = home if holder == "data home" else tmp_path.resolve()
    _configure_readonly_layer([entry], [])
    elsewhere = entry / "outside-the-scratch-root" / "brief.md"
    args = [str(root), str(own_a), str(tree), str(elsewhere)]
    writer = _run(
        _WRITER, args, home, "standard", (str(own_a), str(tree)), _launch_env(stand_ins.temp)
    )
    print("\nwriter:", json.dumps(writer))
    assert writer["readonly"] == {
        "root": {"ok": True, "value": True},
        "own": {"ok": True, "value": False},
        "tree": {"ok": True, "value": False},
    }
    assert writer["own_write"] == {"ok": True, "value": "own"}
    assert writer["tree_write"] == {"ok": True, "value": "shared"}
    assert writer["temp_dir"] == {"ok": True, "value": str(own_a)}
    erofs = {"ok": False, "errno": "EROFS"}
    assert (writer["root_mkdir"], writer["root_file"], writer["stale"]) == (erofs,) * 3
    assert (own_a / "own.md").read_text() == "own"
    assert (tree / "shared.md").read_text() == "shared"
    assert not elsewhere.parent.exists()


_LAYER_WRITER = r"""
import errno, json, os, sys
def attempt(path):
    try:
        with open(path, "w") as fh:
            fh.write("written")
        return "ok"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))
print("REPORT " + json.dumps({
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "writes": {path: attempt(path) for path in sys.argv[1:]},
}), flush=True)
"""


@_REAL_KERNEL
@pytest.mark.parametrize("spelling", ["two levels inside", "through a link inside"])
def test_a_seal_parent_inside_an_operator_exception_keeps_its_write_access(
    home, stand_ins, tmp_path, spelling
):
    """A read-only drive, its ``Documents`` writable, an ``Archive`` below them read-only again.

    The gateway pins the parents of ``Archive`` this uid could rename: ``Documents/Work``, or
    the link ``Documents/link`` the entry is spelled through. Those pins land inside the
    exception, so they must keep its write access.
    """
    _root, tree, own_a, _own_b = _scratch(home)
    drive = tmp_path.resolve() / "drive"
    docs = drive / "Users" / "Documents"
    work = docs / "Work"
    (work / "Archive").mkdir(parents=True)
    spelled = work / "Archive"
    if spelling == "through a link inside":
        (docs / "link").symlink_to(work)
        spelled = docs / "link" / "Archive"
    _configure_readonly_layer([drive, spelled], [docs])
    targets = [drive / "file", docs / "file", work / "file", work / "Archive" / "file"]
    report = _run(
        _LAYER_WRITER,
        [str(t) for t in targets],
        home,
        "standard",
        (str(own_a), str(tree)),
        _launch_env(stand_ins.temp),
    )
    assert report["writes"] == dict(zip(map(str, targets), ["EROFS", "ok", "ok", "EROFS"]))
    assert (work / "file").read_text() == "written"


#: Runs :data:`PRIMITIVES` with the C library, in a user and mount namespace of its own.
_SEQUENCE_PRELUDE = """\
import ctypes, json, sys
libc = ctypes.CDLL(None, use_errno=True)
libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong,
                       ctypes.c_char_p]
libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
D, pin_first = sys.argv[1], sys.argv[2] == "1"
"""


@_REAL_KERNEL
@pytest.mark.parametrize("pin_first", [True, False], ids=["pinned first", "pinned after"])
def test_this_kernel_answers_the_mount_sequence_as_the_model_does(tmp_path, pin_first):
    """The mount model the in-process stage tests judge the launcher by, against this kernel.

    The sequence mounts a tmpfs stage, pins the directory above it before or after that,
    covers an entry on the stage and detaches the stage through descriptors, as the launcher
    does. This kernel must answer it as ``KERNEL_ANSWERS`` says, which is what the model
    answers in ``test_scratch_root_seal_stage_mounts.py``; pinned after, it is the round-13
    failure: the entry stays writable and the stage's directory busy.
    """
    unshare = shutil.which("unshare")
    if unshare is None:
        pytest.skip("unshare not available on this host")
    scratch = tmp_path / "sequence"
    scratch.mkdir()
    proc = subprocess.run(
        [
            unshare,
            "-Urm",
            "--propagation",
            "private",
            sys.executable,
            "-I",
            "-c",
            _SEQUENCE_PRELUDE + PRIMITIVES + "print('RESULT ' + json.dumps(result))\n",
            str(scratch),
            "1" if pin_first else "0",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
    )
    print("\nsequence exit %d:\n%s%s" % (proc.returncode, proc.stdout, proc.stderr), end="")
    assert proc.returncode == 0, proc.stderr
    (line,) = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert json.loads(line[len("RESULT ") :]) == KERNEL_ANSWERS[pin_first]
