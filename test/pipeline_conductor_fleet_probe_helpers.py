"""Shared fixtures and builders for the fleet probe suite.

``test_pipeline_conductor_fleet_probe.py`` and its ``test_pipeline_conductor_fleet_probe_<topic>.py``
parts all load ``fleet_probe.py`` through ``mod`` and build their sessions, ``/proc``
trees and runner command lines here.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

from conftest import make_dir_link

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "pipeline-conductor"
    / "scripts"
    / "fleet_probe.py"
)


KEY = "dashboard_chat-601-1788099254"


#: Every file of this suite: the origin, its topic parts and this helpers module. A
#: self-scan that states a rule for the suite reads all of them, so a case kept in a part
#: is held to the same rule as one in the origin.
SUITE_FILES = (
    *sorted(Path(__file__).resolve().parent.glob("test_pipeline_conductor_fleet_probe*.py")),
    Path(__file__).resolve(),
)


#: The one pytest shape the built-in rule reports: an explicit worker COUNT of two or more,
#: which bypasses the memory budget the rootdir hook applies to ``auto``. A
#: fixture that needs a reportable run carries this so the rest of its argv stays about
#: the property under test. Glued, so ``cmd=`` prints it as the bare flag name ``-n``
#: with nothing counted for the value -- the digits are withheld like every value.
UNBUDGETED = ["-n4"]


# Platform facts:
# - ``os.sysconf`` and ``os.getloadavg`` are absent on Windows and optional in the probe,
#   so cases patch them in with ``raising=False`` and run everywhere.
# - Directory links go through ``conftest.make_dir_link`` (a junction on Windows, no
#   privilege needed).
# - Real file links need ``SeCreateSymbolicLinkPrivilege``; those cases are listed by
#   node id in ``test/requires-real-symlinks.txt`` and skipped when the probe fails.


# CI's ``--cov=kiro_crew`` is a module-name boundary, so the script must load under a
# dotted ``kiro_crew`` name or it is not measured at all. The name is supplied because
# the directory is not importable (a hyphen in the skill name, no ``__init__.py``).
LOAD_NAME = "kiro_crew.builtin_skills.pipeline_conductor.scripts.fleet_probe"


@pytest.fixture(name="mod")
def mod_fixture():
    return load_skill_script(LOAD_NAME, SCRIPT)


@pytest.fixture(name="sessions")
def sessions_fixture(tmp_path, monkeypatch):
    """The gateway session store the probe derives from the data home."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    store = tmp_path / "crew" / "sessions"
    store.mkdir(parents=True)
    return store


def row(role: str, text: str) -> str:
    """One transcript row as this package's writers spell it."""
    return json.dumps({"role": role, "content": text})


def transcript(store: Path, key: str, *rows: str, age_secs: int = 0) -> Path:
    path = store / f"{key}.jsonl"
    path.write_text("".join(f"{line}\n" for line in rows), encoding="utf-8")
    if age_secs:
        # flake-ok: wall-clock backdate far past the idle windows the cases use, pre-existing shape, moved verbatim; tracked for a de-flake pass
        stamp = time.time() - age_secs
        os.utime(path, (stamp, stamp))
    return path


# Classification


def err_res(mod):
    import re

    return [re.compile(pattern) for pattern in mod.DEFAULT_ERR_RES]


def proc_pid(root: Path, pid: str, argv: list[str], *, starttime: int | None = None) -> Path:
    entry = root / pid
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(("\0".join(argv) + "\0").encode("utf-8"))
    if starttime is not None:
        (entry / "stat").write_text(f"{pid} (py test) {stat_fields(starttime)}\n", encoding="utf-8")
    return entry


def stat_fields(starttime: int) -> str:
    """Fields 3 onward of a ``/proc/<pid>/stat`` line, with field 22 set."""
    fields = ["S"] + [str(n) for n in range(4, 25)]
    fields[19] = str(starttime)
    return " ".join(fields)


# Host posture: what counts as the fleet's problem


def host_proc(tmp_path, monkeypatch, *, mem_kb: int | None = 8_388_608) -> Path:
    root = tmp_path / "proc"
    root.mkdir()
    (root / "uptime").write_text("1000.0 900.0\n", encoding="utf-8")
    if mem_kb is not None:
        (root / "meminfo").write_text(
            f"MemTotal:       16000000 kB\nMemAvailable:   {mem_kb} kB\n", encoding="utf-8"
        )
    monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(root))
    monkeypatch.setattr(os, "sysconf", lambda name: 100, raising=False)
    return root


# Every spelling that leaves the worker pool BUDGETED or single-process, and therefore
# quiet. `auto` and `logical` hand the count to the rootdir hook, which sizes the
# pool by memory and by the host's other runs; no `-n` at all inherits `-n auto` from the
# default `addopts`; `0` and `1` run one process with xdist inactive. The last entry is
# argparse's own rule -- a repeated option is resolved last-wins -- so `-n 4 -n auto` is
# a budgeted run. Behind pytest's `--` every token is a path, so a count there is not the
# run's; and an `-o addopts=...` override is walked BEFORE the run's own tokens, where
# pytest puts it, so the run's own last `-n` still wins over the override's.
BUDGETED_SPELLINGS = {
    "absent": [],
    "auto-split": ["-n", "auto"],
    "auto-glued": ["-nauto"],
    "auto-equals": ["-n=auto"],
    "logical-split": ["-n", "logical"],
    "long-auto-split": ["--numprocesses", "auto"],
    "long-logical-equals": ["--numprocesses=logical"],
    "glued-zero": ["-n0"],
    "split-zero": ["-n", "0"],
    "equals-zero": ["-n=0"],
    "glued-one": ["-n1"],
    "split-one": ["-n", "1"],
    "split-leading-zero-one": ["-n", "01"],
    "long-equals-zero": ["--numprocesses=0"],
    "long-split-one": ["--numprocesses", "1"],
    "last-wins-auto": ["-n", "4", "-n", "auto"],
    "single-process-then-terminator": ["-n0", "--", "-n32"],
    "count-behind-terminator": ["--", "-n", "4"],
    "override-then-own-auto": ["-o", "addopts=-n 16", "-n", "auto"],
    "override-last-wins-auto": ["--override-ini=addopts=-n 16", "--override-ini=addopts=-n auto"],
    "override-supplies-none": ["-o", "addopts="],
    "override-of-another-key": ["-o", "testpaths=test", "-n", "auto"],
}


def banned_pids(mod, root: Path, fleet: Path) -> set[str]:
    """The pids ``_host_lines`` emitted a ``BANNED`` line for."""
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    return {line.split()[1].split("=", 1)[1] for line in lines if line.startswith("BANNED pid=")}


def fleet_pid(
    root: Path, fleet: Path, pid: str, argv: list[str], *, unspoofed: bool = True
) -> None:
    """A fleet-owned pid whose ``cmdline`` carries the real procfs byte shape.

    ``proc_pid`` writes ``"\\0".join(argv) + "\\0"`` as the kernel does, so the scan's
    split and re-join are exercised. ``cwd`` goes through ``make_dir_link``.

    ``unspoofed`` (the default) adds an ``exe`` link named after ``argv[0]``'s last
    component, since the ``argv[0]`` rule requires the binary to confirm the name. It is a
    directory junction: the probe reads only the target string, so no privilege is needed.
    Pass ``unspoofed=False`` to arrange the link yourself or leave it absent.
    """
    entry = proc_pid(root, pid, argv, starttime=500)
    make_dir_link(entry / "cwd", fleet)
    if unspoofed and argv:
        name = argv[0].replace("\\", "/").rpartition("/")[2] or "program"
        binary = fleet.parent / "kernel" / name
        binary.mkdir(parents=True, exist_ok=True)
        make_dir_link(entry / "exe", binary)


def install_prefixes(tmp_path: Path) -> dict[str, str]:
    """Real prefixes for the entry-point tests, keyed for ``str.format`` substitution.

    A console script sits beside its interpreter, so the prefixes are built rather than
    named (``/usr/local/bin`` would depend on the host). The installed prefixes differ (a
    system root, a venv in the checkout, a version-manager prefix), each ``bin`` with
    three interpreter spellings.

    Counterexamples: ``checkout`` holds a ``bin`` with no interpreter; every ``sbin`` is
    empty; ``user`` models ``pip install --user`` (no interpreter in ``~/.local/bin``), a
    miss stated in ``_is_installed_entry_point``. Only plain files and directories are
    made. Cases that substitute a prefix call ``require_resolvable_prefixes``.
    """
    made: dict[str, str] = {}
    for key, relative in (
        ("sys", "usr"),
        ("local", "usr/local"),
        ("venv", "wt/.venv"),
        ("shim", "opt/pythons/3.12.13"),
    ):
        prefix = tmp_path / relative
        (prefix / "bin").mkdir(parents=True, exist_ok=True)
        (prefix / "sbin").mkdir(parents=True, exist_ok=True)
        (prefix / "tools").mkdir(parents=True, exist_ok=True)
        for interpreter in ("python", "python3", "python3.12"):
            (prefix / "bin" / interpreter).write_text("#!/bin/sh\n", encoding="utf-8")
        made[key] = str(prefix)
    (tmp_path / "checkout" / "bin").mkdir(parents=True, exist_ok=True)
    made["checkout"] = str(tmp_path / "checkout")
    # A --user install: the console script, and pointedly no interpreter beside it.
    (tmp_path / "home" / "u" / ".local" / "bin").mkdir(parents=True, exist_ok=True)
    made["user"] = str(tmp_path / "home" / "u" / ".local")
    return made


def require_resolvable_prefixes(prefixes: dict[str, str]) -> None:
    """Skip a case that needs an installed prefix the running host cannot express.

    ``_is_installed_entry_point`` declines a drive-lettered path, which ``/proc`` never
    holds, so on such a host every prefix case declines and would pass or fail for the
    wrong reason. The decline itself is pinned on every host by
    ``test_an_installed_entry_point_needs_a_posix_absolute_path_on_any_host``.
    """
    unresolvable = sorted(p for p in prefixes.values() if not p.replace("\\", "/").startswith("/"))
    if unresolvable:
        pytest.skip(
            "an installed entry point is a real interpreter beside a real script at a "
            f"POSIX-absolute path, and this host builds {unresolvable[0]!r}; the decline "
            "that replaces these cases is pinned on every host by "
            "test_an_installed_entry_point_needs_a_posix_absolute_path_on_any_host"
        )


def with_prefixes(tokens: list[str], prefixes: dict[str, str]) -> list[str]:
    """Substitute ``install_prefixes`` keys into an argv enumeration's tokens.

    A token with no placeholder passes through unchanged. Only a case that took a
    substitution needs ``require_resolvable_prefixes``, so the twelve of fifteen runner
    forms without one still run everywhere.
    """
    substituted = [token.format(**prefixes) for token in tokens]
    if substituted != tokens:
        require_resolvable_prefixes(prefixes)
    return substituted


# What a launcher consumes as part of its own grammar. A runner standing after only these
# is still the program, so every one of them must keep qualifying.
LAUNCHER_OWN_GRAMMAR = {
    "nothing": ([], []),
    "numeric-operand": (["timeout"], ["900"]),
    "flag-and-numeric": (["nice"], ["-n", "10"]),
    "bare-flag": (["xvfb-run"], ["-a"]),
    "env-assignment": (["env"], ["CI=1"]),
    "two-assignments": (["env"], ["CI=1", "TERM=dumb"]),
    "flag-then-numeric-then-flag": (["timeout"], ["-k", "5", "900"]),
}
