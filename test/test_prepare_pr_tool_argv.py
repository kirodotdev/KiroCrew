"""``_tool_argv.py`` and the command runners of the kirocrew-prepare-pr scripts that use it.

The module resolves a runner's argv[0] from PATH's absolute entries only (so a
checkout's own ``gh.exe`` and relative PATH entries never win the lookup) and
refuses a ``.cmd``/``.bat`` launch whose arguments carry a cmd.exe metacharacter.
The second half pins that every ``run()``-style helper goes through it before a
child is spawned.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from skill_script_helpers import load_skill_script

SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-prepare-pr"
    / "scripts"
)


def _tool_argv() -> ModuleType:
    return load_skill_script("prepare_pr_tool_argv_under_test", SCRIPTS / "_tool_argv.py")


def _plant_executable(directory: Path, stem: str) -> Path:
    """An executable named ``stem`` that ``shutil.which`` finds on this platform."""
    directory.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        path = directory / f"{stem}.exe"
        path.write_bytes(b"")
    else:
        path = directory / stem
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class TestResolve:
    def test_a_bare_name_resolves_from_an_absolute_path_entry(self, tmp_path, monkeypatch):
        mod = _tool_argv()
        planted = _plant_executable(tmp_path / "bin", "fake-tool")
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))
        argv, refused = mod.resolve(["fake-tool", "pr", "view"])
        assert refused is None
        assert os.path.normcase(argv[0]) == os.path.normcase(str(planted))
        assert argv[1:] == ["pr", "view"]

    def test_the_current_directory_and_relative_entries_are_never_searched(
        self, tmp_path, monkeypatch
    ):
        """The executable sits in the cwd and is reachable through a relative entry,
        so a lookup of the bare name -- what CreateProcess does on Windows -- would
        find it. Only an absolute entry counts, and the empty one holds nothing."""
        mod = _tool_argv()
        _plant_executable(tmp_path / "checkout", "fake-tool")
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(tmp_path / "checkout")
        monkeypatch.setenv("PATH", os.pathsep.join([".", "", str(empty)]))
        argv, refused = mod.resolve(["fake-tool", "pr", "view"])
        assert argv is None
        assert refused == (127, "", "fake-tool: not found on PATH")

    def test_an_argv0_with_a_directory_part_is_used_as_given(self, monkeypatch):
        mod = _tool_argv()
        monkeypatch.setenv("PATH", "")
        argv, refused = mod.resolve([sys.executable, "-c", "pass"])
        assert refused is None
        assert argv == [sys.executable, "-c", "pass"]

    @pytest.mark.parametrize("launcher", ["C:\\tools\\gh.cmd", "C:\\tools\\GH.BAT"])
    @pytest.mark.parametrize("meta", list('^&|<>%!"\r\n'))
    def test_a_batch_launcher_refuses_every_cmd_metacharacter(self, monkeypatch, launcher, meta):
        mod = _tool_argv()
        monkeypatch.setattr(mod, "which", lambda name: launcher)
        argv, refused = mod.resolve(["gh", "api", "--jq", "a{}b".format(meta)])
        assert argv is None
        assert refused is not None and refused[0] == 126
        assert "metacharacters" in refused[2]

    def test_a_batch_launcher_with_plain_arguments_is_run(self, monkeypatch):
        mod = _tool_argv()
        monkeypatch.setattr(mod, "which", lambda name: "C:\\tools\\gh.cmd")
        argv, refused = mod.resolve(["gh", "pr", "view", "--json", "number"])
        assert refused is None
        assert argv == ["C:\\tools\\gh.cmd", "pr", "view", "--json", "number"]

    def test_an_exe_launcher_passes_metacharacters_through(self, monkeypatch):
        """Only cmd.exe re-parses arguments; a real executable gets them verbatim."""
        mod = _tool_argv()
        monkeypatch.setattr(mod, "which", lambda name: "C:\\tools\\gh.exe")
        argv, refused = mod.resolve(["gh", "api", "--jq", ".[] | .name"])
        assert refused is None
        assert argv == ["C:\\tools\\gh.exe", "api", "--jq", ".[] | .name"]

    @pytest.mark.skipif(sys.platform == "win32", reason="emulates the Windows lookup on POSIX")
    def test_windows_lookup_applies_pathext_without_shutil_help(self, tmp_path, monkeypatch):
        """Python <=3.11 ``shutil.which`` adds no PATHEXT suffix to a name with a
        directory part, so ``which`` must try the suffixes itself."""
        mod = _tool_argv()
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "git.EXE").write_bytes(b"")
        (bindir / "gh.CMD").write_bytes(b"")
        (bindir / "plain").write_bytes(b"")
        monkeypatch.setenv("PATH", str(bindir))
        monkeypatch.delenv("PATHEXT", raising=False)
        monkeypatch.setattr(mod.os, "name", "nt")
        monkeypatch.setattr(
            mod.shutil, "which", lambda cmd, *a, **k: cmd if os.path.isfile(cmd) else None
        )
        assert mod.which("git") == str(bindir / "git.EXE")
        assert mod.which("gh") == str(bindir / "gh.CMD")
        assert mod.which("git.EXE") == str(bindir / "git.EXE")
        # A file without a PATHEXT suffix is not a Windows executable.
        assert mod.which("plain") is None


def test_the_lookup_and_the_metacharacter_set_are_spelled_only_here():
    """push_guard.py binds both from this module, so no other script carries a copy
    that could drift from this one."""
    spelled = {
        path.name
        for path in SCRIPTS.glob("*.py")
        for needle in ("<>%!", "os.path.isabs(entry)")
        if needle in path.read_text(encoding="utf-8")
    }
    assert spelled == {"_tool_argv.py"}


# (script, runner attribute, how the script reaches its _tool_argv module)
RUNNERS = [
    ("diff_signals.py", "run", lambda m: m._tool_argv()),
    ("enable_automerge.py", "run", lambda m: m._tool_argv),
    ("pr_findings.py", "run", lambda m: m._tool_argv),
    ("pr_status.py", "run", lambda m: m._tool_argv),
    ("pr_status.py", "bounded_run", lambda m: m._tool_argv),
    ("resolve_profile.py", "run", lambda m: m._tool_argv),
    ("local_review.py", "run", lambda m: m._tool_argv),
    ("green_age.py", "run", lambda m: m._tool_argv),
    ("regen_shared.py", "run", lambda m: m._tool_argv),
]


@pytest.mark.parametrize(
    ("script", "runner", "guard_of"), RUNNERS, ids=[f"{s}:{r}" for s, r, _ in RUNNERS]
)
def test_every_runner_vets_its_command_before_spawning(monkeypatch, script, runner, guard_of):
    """A refusal is returned as the runner's own result and no child is started; an
    accepted command is spawned with the RESOLVED argv, not the bare name."""
    mod = load_skill_script("prepare_pr_runner_" + script[:-3], SCRIPTS / script)
    guard = guard_of(mod)
    spawned: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = "out"
        stderr = ""

    def fake_spawn(argv, **kwargs):
        spawned.append(list(argv))
        return _Done()

    monkeypatch.setattr(mod.subprocess, "run", fake_spawn)

    monkeypatch.setattr(guard, "resolve", lambda args: (None, (126, "", "refused")))
    assert getattr(mod, runner)(["gh", "pr", "view"]) == (126, "", "refused")
    assert spawned == []

    monkeypatch.setattr(guard, "resolve", lambda args: (["/resolved/gh"] + list(args[1:]), None))
    rc, out, _ = getattr(mod, runner)(["gh", "pr", "view"])
    assert (rc, out) == (0, "out")
    assert spawned == [["/resolved/gh", "pr", "view"]]


@pytest.mark.parametrize(
    ("script", "builder"), [("prove.py", "_git_argv"), ("pre_push_gate.py", "git_argv")]
)
def test_every_git_argv_builder_launches_the_git_on_an_absolute_path_entry(
    tmp_path, monkeypatch, script, builder
):
    """A git in the checkout (the cwd, reachable through ``.``) never wins over the one
    on an absolute PATH entry, and with none there the builder raises instead of handing
    the bare name to the OS."""
    mod = load_skill_script("prepare_pr_builder_" + script[:-3], SCRIPTS / script)
    _plant_executable(tmp_path / "checkout", "git")
    real = _plant_executable(tmp_path / "bin", "git")
    monkeypatch.chdir(tmp_path / "checkout")
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(tmp_path / "bin")]))
    argv = getattr(mod, builder)(["status"])
    assert os.path.normcase(argv[0]) == os.path.normcase(str(real))
    assert argv[-1] == "status"

    monkeypatch.setenv("PATH", ".")
    with pytest.raises(OSError, match="git: not found on PATH"):
        getattr(mod, builder)(["status"])


@pytest.mark.parametrize(
    ("script", "ask"),
    [
        ("prove.py", lambda m: m.pin_commit(Path("."))),
        ("pre_push_gate.py", lambda m: m.head_tree()),
    ],
    ids=["prove.py:pin_commit", "pre_push_gate.py:head_tree"],
)
def test_a_batch_git_launcher_can_read_the_commit_and_tree_ids(monkeypatch, script, ask):
    """The ids a proof names are asked for with no cmd.exe metacharacter, so a
    ``git.cmd`` launcher is not refused on every run."""
    mod = load_skill_script("prepare_pr_ids_" + script[:-3], SCRIPTS / script)
    monkeypatch.setattr(mod._tool_argv, "which", lambda name: "C:\\tools\\git.cmd")
    spawned: list[list[str]] = []

    def fake_spawn(argv, **kwargs):
        spawned.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"0" * 40 + b"\n", b"")

    monkeypatch.setattr(mod.subprocess, "run", fake_spawn)
    ask(mod)
    assert spawned and all(argv[0] == "C:\\tools\\git.cmd" for argv in spawned)
