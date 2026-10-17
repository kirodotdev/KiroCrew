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


# Three platform facts meet this suite, and each is handled by the mechanism the
# repository already has for it.
#
# ``os.sysconf`` and ``os.getloadavg`` are absent on Windows, and the probe's own
# source treats both as optional -- the age is uncomputable without a clock-tick
# rate, the load average sits behind a ``hasattr`` guard -- so the cases needing
# them patch them in with ``raising=False`` and run everywhere, and the answer a
# host without a clock source gets is asserted on every platform.
#
# A name meaning another DIRECTORY is what most of the ``/proc`` fixtures below
# need: a junction supplies it on Windows with no privilege at all, so they go
# through ``conftest.make_dir_link`` and keep their coverage on every host.
#
# A real link to a FILE -- the ``exe`` entries and the transcript that escapes
# its store -- needs ``SeCreateSymbolicLinkPrivilege``, which CI runners hold and
# an ordinary Windows shell does not. Those cases are inventoried by exact node
# id in ``test/requires-real-symlinks.txt``, which the root conftest skips only
# when its capability probe fails, so the inventory stays the one place that
# records what a host without the privilege loses.


# The name the script is loaded under decides whether it is measured at all. CI
# measures the backend with the package selector ``--cov=kiro_crew``, and coverage
# treats that as a module-name boundary: a file loaded under a bare top-level name
# falls outside it, so every line these cases execute is recorded against nothing
# and the script reads as untested however thoroughly it is exercised. Measured on
# this checkout with that selector: a ``fleet_probe`` load leaves the file absent
# from the coverage data entirely, a dotted load under the package records it.
# The dotted spelling mirrors where the script physically sits; its directory is
# not importable (a hyphen in the skill name, no ``__init__.py``), which is why
# the module name has to be supplied rather than derived.
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


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Host posture: what counts as the fleet's problem
# --------------------------------------------------------------------------


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

    ``proc_pid`` writes ``"\\0".join(argv) + "\\0"`` -- arguments NUL-separated and one
    NUL terminator, which is what the kernel produces. The scan is driven through that
    byte path on purpose: a pre-joined string handed straight to the rules cannot show
    that the split, the dropped terminator and the re-join preserved the argument
    boundaries the argv-side checks read.

    The ``cwd`` link goes through ``make_dir_link`` like every other directory link in
    this file: a name meaning another DIRECTORY is a junction on Windows and needs no
    privilege, where a bare ``symlink_to`` would raise on an unelevated Windows shell and
    ERROR every case built on this fixture instead of running it.

    ``unspoofed`` supplies the ``exe`` link as well, because a real fleet-owned process
    has one: it runs as the fleet's own uid, so the kernel's binary always reads, and the
    ``argv[0]`` rule requires that binary to CONFIRM the name the process claims. An
    unspoofed process is one whose binary's name IS what ``argv[0]`` says, so the link is
    built from ``argv[0]``'s own last component -- the ordinary case, and therefore the
    default. Pass ``unspoofed=False`` for a test that arranges the link itself or needs it
    absent.

    That link is a DIRECTORY junction rather than a symlink to a file, and the difference
    is free: the probe reads the link's TARGET as a string and takes its last component,
    never stating it, so a junction carries exactly the same fact while needing no
    Windows privilege and adding nothing to the real-symlink inventory.
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

    A console script is installed into the same directory as the interpreter it was
    installed for, so these tests have to BUILD that rather than name a path and rely on
    the host having one. Naming ``/usr/local/bin`` would pass or fail with whatever the
    runner happens to have installed there, which is the worst kind of test: green on one
    host and red on another for reasons the change never touched.

    The four installed prefixes are deliberately unlike each other -- a system root, a
    virtualenv inside the fleet checkout, a version-manager prefix -- because the rule
    must recognise all of them without naming any. Each one's ``bin`` gets the three
    interpreter spellings a real installation carries.

    Three counterexamples come with them, and the third is the one that matters most.
    ``checkout`` is the case GPT's finding named: a repository that merely HOLDS a ``bin``
    directory, with no interpreter installed beside its contents. Every prefix's ``sbin``
    is created EMPTY, because a python console script is never installed there -- it
    separates "a directory whose name looks like an installation's" from "a directory an
    interpreter was installed into". And ``user`` models a ``pip install --user``, which
    writes the console script into ``~/.local/bin`` and NO interpreter with it: writing
    one there would fabricate a file a real ``--user`` install never creates, and a test
    passing on a fabricated file certifies a behaviour that cannot occur. So the
    interpreter is deliberately absent and ``user`` belongs among the declining cases --
    that miss is stated in ``_is_installed_entry_point`` rather than claimed away.

    Only plain files and directories are created, so nothing here joins the real-symlink
    privilege inventory.

    Building them is host-independent; USING one is not. A prefix is only meaningful to
    ``_is_installed_entry_point`` when it is POSIX-absolute, so the cases that actually
    substitute one are guarded by ``require_resolvable_prefixes``. Merely calling this
    helper is not guarded, because most of its callers enumerate runner forms of which only
    three carry a prefix at all, and skipping the other twelve would discard real coverage
    of platform-independent logic.
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

    ``_is_installed_entry_point`` declines any path it cannot resolve, and a drive-lettered
    one is exactly that: the rule reads ``/proc/<pid>/cmdline``, which only a Linux kernel
    writes, so ``C:/...`` never reaches it from a scanned process. On a host whose own
    absolute paths are drive-lettered, every case built on a prefix therefore declines --
    which turns the cases expecting a REPORT into failures and, worse, leaves the ones
    expecting a decline passing for a reason that has nothing to do with what they pin.

    Adapting the fixture would need a real interpreter at a POSIX-absolute path outside
    ``tmp_path``, which is the host-dependent test ``install_prefixes`` exists to avoid, and
    widening the rule would add a branch no caller can reach. So the prefix-dependent cases
    state their absence here instead, and what the decline GUARANTEES on such a host is
    pinned on every host by
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

    An enumeration cannot hold the paths directly, because they only exist once a test
    has built them under its own ``tmp_path``. A token carrying no placeholder passes
    through unchanged, so a case that is deliberately relative or deliberately Windows
    stays exactly as written.

    A case that DID take a substitution now depends on the prefix being resolvable, so that
    is the point where ``require_resolvable_prefixes`` applies -- and only there. Three of
    the fifteen runner forms carry a placeholder; guarding the helper itself would skip the
    other twelve, whose logic is platform-independent and worth covering everywhere.
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
