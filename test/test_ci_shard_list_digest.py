"""One run's backend shards are held to one test-file list.

A ``--file-shard-targets`` list is placed across the shards by measured time,
so every shard of a run must hold the same list: shards holding different
lists can each leave a file to another, and that file runs nowhere on a green
run. ``ci.yml``'s ``changes`` job names each list's digest once per run, and
every shard passes that digest to pytest with its own list.

These tests execute the workflow's own steps under bash: the ``changes`` step
that computes the digests, then each shard's scope and test steps. Stand-in
programs take the place of the selectors (they print the list a scenario gives
that shard) and of pytest (it records its arguments). The recorded arguments
then go through the file-shard plugin's own option table and its
``pytest_configure``, in this process, which is where a shard is refused.
Nothing here names a step's commands, an environment variable or an output:
the steps' ``${{ ... }}`` expressions are resolved from the values the earlier
steps actually wrote.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import default_timeout
from scripts import ci_file_shards

ROOT = Path(__file__).resolve().parent.parent

# The steps are bash for a Linux runner (GNU tools, bash 4), and the Windows
# job's are the same bash under Git Bash, so the Linux shards execute them all.
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bash") is None,
    reason="executes the workflow's bash steps where its Linux jobs run",
)

# What stands in for every program a step would start that is not a shell
# tool: `git` prints the scenario's changed docs, pytest records its arguments
# (NUL-separated, one file per call), a `python -c` one-liner prints a number
# (the Windows step sizes its worker count from one), and any other Python
# script is a selector that prints the scenario's list for this shard.
_STAND_IN = r"""#!/bin/bash
name="$(basename "$0")"
if [ "$name" = git ]; then
  if [ -n "${STAND_IN_DOCS:-}" ]; then printf '%s\n' "$STAND_IN_DOCS"; fi
  exit 0
fi
if [ "$name" = pytest ] || { [ "${1:-}" = -m ] && [ "${2:-}" = pytest ]; }; then
  next="$(find "$STAND_IN_CALLS" -type f | wc -l)"
  printf '%s\0' "$@" > "$STAND_IN_CALLS/$(printf '%04d' "$next")"
  exit 0
fi
if [ "${1:-}" = -c ]; then
  echo 8192
  exit 0
fi
list="$STAND_IN_LIST"
for arg in "$@"; do
  if [ "$arg" = --for-windows ]; then list="$STAND_IN_WINDOWS_LIST"; fi
done
if [ -n "$list" ]; then printf '%s\n' "$list"; fi
exit "${STAND_IN_SELECTOR_RC:-0}"
"""

_EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def _lines(names) -> str:
    return "\n".join(names)


class Run:
    """One run of ``ci.yml`` on a scratch runner: its steps, executed for real."""

    def __init__(self, workflow: dict, tmp: Path) -> None:
        self.jobs = workflow["jobs"]
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for name in ("python", "python3", "pytest", "git"):
            (self.bin / name).write_text(_STAND_IN, encoding="utf-8")
            (self.bin / name).chmod(0o755)
        self.changes: dict[str, str] = {}
        self._serial = 0

    def _resolve(self, value: str, steps: dict[str, dict[str, str]], temp: Path, shard: int) -> str:
        def one(match: re.Match[str]) -> str:
            head, _, name = match.group(1).rpartition(".")
            if head == "needs.changes.outputs":
                return self.changes.get(name, "")
            if head.startswith("steps.") and head.endswith(".outputs"):
                return steps.get(head.split(".")[1], {}).get(name, "")
            if head == "matrix":
                return str(shard)
            if match.group(1) == "runner.temp":
                return str(temp)
            if match.group(1) == "github.workspace":
                return str(ROOT)
            # An event field or anything else a scenario does not set.
            return ""

        return _EXPRESSION.sub(one, str(value))

    def step(self, job: str, step: dict, steps: dict, temp: Path, shard: int, stand_in: dict):
        """Execute one step as Actions would; return (exit code, outputs, pytest calls, log)."""
        self._serial += 1
        scratch = self.tmp / f"step-{self._serial}"
        calls = scratch / "calls"
        calls.mkdir(parents=True)
        output = scratch / "output"
        output.write_text("", encoding="utf-8")
        script = scratch / "step.sh"
        script.write_text(self._resolve(step["run"], steps, temp, shard), encoding="utf-8")
        # `shell: bash` and the unprivileged test shell both add `-o pipefail`
        # to the default `bash -e`.
        flags = ["-eo", "pipefail"] if step.get("shell") else ["-e"]
        declared = {**self.jobs[job].get("env", {}), **step.get("env", {})}
        env = {name: self._resolve(value, steps, temp, shard) for name, value in declared.items()}
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", *flags, str(script)],
            cwd=ROOT,
            capture_output=True,
            # A step is a second of shell; the bound only ends a hung one.
            timeout=default_timeout(),
            env={
                **env,
                "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "RUNNER_TEMP": str(temp),
                "GITHUB_OUTPUT": str(output),
                "GITHUB_WORKSPACE": str(ROOT),
                "STAND_IN_CALLS": str(calls),
                "STAND_IN_LIST": "",
                "STAND_IN_WINDOWS_LIST": "",
                **stand_in,
            },
            **UTF8_TEXT,
        )
        outputs = dict(
            line.split("=", 1)
            for line in output.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        recorded = [
            [arg for arg in path.read_text(encoding="utf-8").split("\0") if arg]
            for path in sorted(calls.iterdir())
        ]
        return result.returncode, outputs, recorded, result.stdout + result.stderr

    def digest_lists(self, *, linux=(), windows=(), docs=(), selector_rc=0, **outputs: str) -> str:
        """Run the ``changes`` step that names the digests, after the outputs it reads."""
        self.changes = dict(outputs)
        step = next(s for s in self.jobs["changes"]["steps"] if s.get("id") == "lists")
        # The step reads its inputs from the steps before it in the same job.
        earlier = {"decide": outputs, "docs-only": outputs, "leaf": outputs}
        code, written, _calls, log = self.step(
            "changes",
            step,
            earlier,
            self.tmp,
            1,
            {
                "STAND_IN_LIST": _lines(linux or docs),
                "STAND_IN_WINDOWS_LIST": _lines(windows),
                "STAND_IN_DOCS": "docs/README.md" if docs else "",
                "STAND_IN_SELECTOR_RC": str(selector_rc),
            },
        )
        assert code == 0, log
        self.changes.update(written)
        return log

    def shard(self, job: str, index: int, names=(), *, selector_rc=0, docs=True):
        """Run one shard's scope and test steps; return (exit code, pytest calls, log)."""
        temp = self.tmp / f"{job}-{index}-{self._serial}"
        temp.mkdir()
        steps: dict[str, dict[str, str]] = {}
        calls: list[list[str]] = []
        log = ""
        stand_in = {
            "STAND_IN_LIST": _lines(names),
            "STAND_IN_DOCS": "docs/README.md" if docs else "",
            "STAND_IN_SELECTOR_RC": str(selector_rc),
        }
        for step in self.jobs[job]["steps"]:
            is_scope = step.get("id") == "scope"
            if not is_scope and "--file-shard-targets" not in step.get("run", ""):
                continue
            if "if" in step and "'true'" in step["if"]:
                # The only guard on these steps: the docs job's "a backend list exists".
                if steps.get("scope", {}).get("backend") != "true":
                    continue
            code, outputs, recorded, text = self.step(job, step, steps, temp, index, stand_in)
            log += text
            calls += recorded
            if is_scope:
                steps["scope"] = outputs
            if code != 0:
                return code, calls, log
        return 0, calls, log

    def shard_count(self, job: str) -> int:
        return int(self.jobs[job]["env"]["SHARD_COUNT"])


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))


@pytest.fixture
def run(workflow: dict, tmp_path: Path) -> Run:
    return Run(workflow, tmp_path)


def _corpus(workflow: dict, names) -> list[str]:
    """The first 400 of *names* that the Windows pin file does not list.

    A Windows leaf run that changes more than ``WINDOWS_LEAF_MAX_PINNED``
    pinned files runs the full suite instead, so a sample holding pinned files
    would pass or fail by which files sort first in ``test/``.
    """
    count = int(workflow["jobs"]["backend-test-windows"]["env"]["SHARD_COUNT"])
    pinned = ci_file_shards.read_pins(ROOT / "scripts/ci_file_shard_pins_windows.txt", count)
    return sorted(name for name in names if name not in pinned)[:400]


@pytest.fixture(scope="module")
def corpus(workflow: dict) -> list[str]:
    """Real test files of this checkout: a targets list must name files that exist."""
    names = [path.relative_to(ROOT).as_posix() for path in (ROOT / "test").glob("test_*.py")]
    return _corpus(workflow, names)


@pytest.fixture(scope="module")
def options() -> argparse.ArgumentParser:
    """The file-shard plugin's own option table, as an argument parser."""
    parser = argparse.ArgumentParser(add_help=False)

    class Group:
        @staticmethod
        def addoption(*names, **spec):
            spec.pop("help", None)
            parser.add_argument(*names, **spec)

    class Declared:
        @staticmethod
        def getgroup(_name):
            return Group

    ci_file_shards.pytest_addoption(Declared)
    return parser


class _Config:
    """As much of pytest's Config as the plugin's ``pytest_configure`` reads."""

    def __init__(self, values: argparse.Namespace) -> None:
        self.values = values
        self.rootpath = ROOT
        self.pluginmanager = self
        self.plugin = None

    def getoption(self, name: str, default=None):
        return getattr(self.values, name, default)

    def register(self, plugin, _name) -> None:
        self.plugin = plugin


def _admit(options: argparse.ArgumentParser, call: list[str]):
    """Give one recorded pytest call to the plugin; return what it registered.

    Raises ``pytest.UsageError`` where pytest would stop before collecting.
    """
    values, _rest = options.parse_known_args(call)
    config = _Config(values)
    ci_file_shards.pytest_configure(config)
    return config.plugin


def _sharded(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if any(a.startswith("--file-shard-targets=") for a in call)]


def _has_digest(call: list[str]) -> bool:
    return any(arg.startswith("--file-shard-targets-sha256") for arg in call)


def _owned(plugin, index: int) -> set[str]:
    return {
        path.relative_to(ROOT).as_posix()
        for path, owner in plugin.balanced.items()
        if owner == index
    }


# Each way a run gives its shards a list: the job, the outputs of the `changes`
# job that put the shards on that path, and which list the scenario's names are.
_REDUCED = {"only_frontend": "true", "windows_paths": "false"}
_LEAF = {"leaf_tests": "true", "windows_paths": "false"}
_DOCS = {"docs_only": "true"}
PATHS = {
    "linux reduced": ("backend-test", _REDUCED, "linux"),
    "windows reduced": ("backend-test-windows", _REDUCED, "windows"),
    "linux leaf gates": ("backend-test", _LEAF, "gates"),
    "windows leaf gates": ("backend-test-windows", _LEAF, "gates"),
    "docs readers": ("docs-contract-tests", _DOCS, "docs"),
}


def _start(run: Run, path: str, names: list[str], **more) -> str:
    """Run the ``changes`` step for *path* with *names* as the run's list; return the job."""
    job, outputs, kind = PATHS[path]
    outputs = dict(outputs)
    lists: dict = {}
    if kind == "gates":
        # A leaf run: one changed file (not a gate) and the corpus gates.
        outputs["leaf_gates"] = _lines(names)
        outputs["leaf_targets"] = _lines(["test/conftest.py", *names])
    elif kind == "docs":
        lists["docs"] = names
    else:
        # The two platforms' lists differ, as they do on a real run.
        lists["linux"] = names if kind == "linux" else [*names, "test/conftest.py"]
        lists["windows"] = names if kind == "windows" else names[1:]
    run.digest_lists(**lists, **outputs, **more)
    return job


def _shard(run: Run, path: str, index: int, names: list[str], **more):
    job, _outputs, kind = PATHS[path]
    # A leaf shard's lists arrive as outputs of `changes`, not from a selector.
    return run.shard(job, index, () if kind == "gates" else names, **more)


@pytest.mark.parametrize("path", sorted(PATHS))
def test_every_shard_of_a_run_holding_the_runs_list_is_admitted_and_splits_it(
    run: Run, options, corpus, path: str
) -> None:
    rng = random.Random(f"identical {path}")
    names = rng.sample(corpus, 60)
    job = _start(run, path, names)
    count = run.shard_count(job)
    seen: list[str] = []
    for index in range(1, count + 1):
        # Every shard works its list out again; the order it comes in is its own.
        mine = rng.sample(names, len(names))
        code, calls, log = _shard(run, path, index, mine)
        assert code == 0, log
        sharded = _sharded(calls)
        assert len(sharded) == 1, calls
        # A list never goes to pytest without the run's digest, and no call
        # without a list carries one.
        assert [_has_digest(call) for call in calls] == [call in sharded for call in calls]
        plugin = _admit(options, sharded[0])
        assert plugin.digest == ci_file_shards.targets_digest(names)
        seen += sorted(_owned(plugin, index))
    # Every listed file has exactly one owner among the shards.
    assert sorted(seen) == sorted(names)


@pytest.mark.parametrize("path", sorted(PATHS))
def test_a_shard_holding_another_list_is_refused_and_the_others_are_not(
    run: Run, options, corpus, path: str
) -> None:
    rng = random.Random(f"differing {path}")
    names = rng.sample(corpus, 40)
    extra = next(name for name in corpus if name not in names)
    job = _start(run, path, names)
    count = run.shard_count(job)
    odd = rng.randrange(1, count + 1)
    # One file fewer, one more, and one swapped: each is another list.
    others = [names[1:], [*names, extra], [extra, *names[1:]]]
    for index in range(1, count + 1):
        mine = others[index % len(others)] if index == odd else names
        if PATHS[path][2] == "gates" and index == odd:
            # A leaf shard's gate list is an output of `changes`; this shard's
            # copy of it is what differs.
            kept = run.changes["leaf_gates"]
            run.changes["leaf_gates"] = _lines(mine)
            code, calls, log = _shard(run, path, index, mine)
            run.changes["leaf_gates"] = kept
        else:
            code, calls, log = _shard(run, path, index, mine)
        assert code == 0, log
        (call,) = _sharded(calls)
        if index == odd:
            with pytest.raises(pytest.UsageError, match="different list from the rest of the run"):
                _admit(options, call)
        else:
            assert _admit(options, call).digest == ci_file_shards.targets_digest(names)


@pytest.mark.parametrize("path", sorted(PATHS))
def test_a_run_that_named_no_digest_admits_no_shard_holding_a_list(
    run: Run, options, corpus, path: str
) -> None:
    names = random.Random(f"unnamed {path}").sample(corpus, 12)
    # The run's own selector failed, or found nothing, so it vouches for no list.
    _job, outputs, kind = PATHS[path]
    if kind == "gates":
        run.digest_lists(**{**outputs, "leaf_targets": "test/conftest.py"})
        run.changes["leaf_gates"] = _lines(names)
    elif kind == "docs":
        run.digest_lists(**outputs)
    else:
        _start(run, path, names, selector_rc=2)
    assert not any(value for name, value in run.changes.items() if name.endswith("sha256"))
    code, calls, log = _shard(run, path, 1, names)
    assert code == 0, log
    (call,) = _sharded(calls)
    assert _has_digest(call)
    with pytest.raises(pytest.UsageError, match="not a SHA-256 digest"):
        _admit(options, call)


@pytest.mark.parametrize("path", sorted(PATHS))
def test_a_shard_holding_no_list_fails_when_the_run_has_one(run: Run, corpus, path: str) -> None:
    names = random.Random(f"absent {path}").sample(corpus, 12)
    job = _start(run, path, names)
    kind = PATHS[path][2]
    if kind == "gates":
        run.changes["leaf_gates"] = ""
    # Once with a selector that fails and once with one that finds nothing.
    for selector_rc in (2, 0):
        code, calls, log = run.shard(job, 1, (), selector_rc=selector_rc, docs=False)
        assert code != 0, log
        # A selector that finds nothing is refused by name; one that fails may
        # already have ended the step with its own exit code.
        assert selector_rc != 0 or "::error::" in log, log
        # Nothing ran in place of the list: not the full suite, not a part of it.
        assert _sharded(calls) == []
        assert not any("--file-shard-pins" in arg for call in calls for arg in call)


@pytest.mark.parametrize("added", range(6))
def test_new_test_files_sorting_first_leave_the_windows_leaf_sample_failing(
    run: Run, workflow: dict, corpus, added: int
) -> None:
    # A PR adding test files that sort before the rest shifts every sample.
    names = [*corpus, *(f"test/test_0{index}.py" for index in range(added))]
    shifted = _corpus(workflow, names)
    test_a_shard_holding_no_list_fails_when_the_run_has_one(run, shifted, "windows leaf gates")


@pytest.mark.parametrize("job", ["backend-test", "backend-test-windows"])
def test_a_full_suite_run_passes_no_list_and_no_digest(run: Run, corpus, job: str) -> None:
    run.digest_lists(only_frontend="false", leaf_tests="false", windows_paths="true")
    assert not any(value for name, value in run.changes.items() if name.endswith("sha256"))
    code, calls, log = run.shard(job, 1, corpus[:5])
    assert code == 0, log
    (call,) = calls
    assert not any(arg.startswith("--file-shard-targets") for arg in call)
    assert any(arg == "--file-shard-pins" for arg in call)


def test_the_selector_failing_everywhere_still_runs_the_full_suite(run: Run) -> None:
    for job in ("backend-test", "backend-test-windows"):
        run.digest_lists(selector_rc=2, **_REDUCED)
        for index in (1, run.shard_count(job)):
            code, calls, log = run.shard(job, index, (), selector_rc=2)
            assert code == 0, log
            (call,) = calls
            assert not any(arg.startswith("--file-shard-targets") for arg in call)
            assert any(arg == "--file-shard-pins" for arg in call)


def test_the_runs_digests_name_the_set_of_files_and_only_the_lists_the_run_has(
    run: Run, corpus
) -> None:
    for seed in range(4):
        rng = random.Random(seed)
        linux = rng.sample(corpus, rng.randrange(1, 80))
        # Names whose order depends on how they are compared: the shell sorts
        # bytes, the plugin sorts code points, and the two must agree.
        linux += [
            "test/Test_Upper.py",
            "Test/test_z.py",
            "test/a b/test_x.py",
            "test/t\u00e9st_\u00e9.py",
        ]
        windows = linux[: len(linux) // 2] or linux
        noisy = [*linux, *linux[:3], "", "   "]
        rng.shuffle(noisy)
        run.digest_lists(linux=noisy, windows=windows, only_frontend="true")
        found = {name: value for name, value in run.changes.items() if name.endswith("sha256")}
        named = sorted(value for value in found.values() if value)
        assert named == sorted(
            {ci_file_shards.targets_digest(linux), ci_file_shards.targets_digest(windows)}
        )
        # A diff that is not frontend-only has no reduced list to name.
        run.digest_lists(linux=linux, windows=windows, only_frontend="false")
        assert not any(value for name, value in run.changes.items() if name.endswith("sha256"))
    # Docs readers under website/ are frontend specs: no backend list, no digest.
    run.digest_lists(docs=["website/src/a.test.ts", "website/src/b.test.ts"], docs_only="true")
    assert not any(value for name, value in run.changes.items() if name.endswith("sha256"))
    mixed = ["website/src/a.test.ts", *corpus[:7]]
    run.digest_lists(docs=mixed, docs_only="true")
    named = [value for name, value in run.changes.items() if name.endswith("sha256") and value]
    assert named == [ci_file_shards.targets_digest(corpus[:7])]


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "0" * 63,
        "0" * 65,
        "g" * 64,
        "sha256:" + "0" * 57,
        "0" * 64 + "\n--file-shard=2",
        "$(id)",
    ],
)
def test_a_value_that_is_not_a_digest_is_refused_whatever_the_list(
    options, corpus, tmp_path, value
):
    listed = tmp_path / "targets.txt"
    listed.write_text(_lines(corpus[:3]), encoding="utf-8")
    call = ["--file-shards=2", "--file-shard=1", f"--file-shard-targets={listed}"]
    with pytest.raises(pytest.UsageError, match="not a SHA-256 digest"):
        _admit(options, [*call, f"--file-shard-targets-sha256={value}"])
    # The digest itself is admitted in either case and with stray whitespace.
    digest = ci_file_shards.targets_digest(corpus[:3])
    for spelling in (digest, digest.upper(), f" {digest}\n"):
        assert _admit(options, [*call, f"--file-shard-targets-sha256={spelling}"]).digest == digest
