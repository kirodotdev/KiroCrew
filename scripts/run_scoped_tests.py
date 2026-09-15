#!/usr/bin/env python3
"""Run the tests a change is related to. The full suite is CI's job, not this box's.

What this buys
--------------
The ``prepare-pr`` loop runs its gate up to ten times per PR, and the full local
suites are enormous: 62,108 collected backend tests (collection alone takes ~100s
before a single test runs) and ~1,444 frontend spec files. CI runs the touched
surface's full suite on ``refs/pull/<N>/merge`` regardless of what ran here, and
for the other surface the same cross-surface set this runner includes (``ci.yml``
``changes`` job; the only other narrowing is ``leaf_test_scope.py``, which is sound
by construction). Paying for the suite serially on a workstation -- once per
iteration of a ten-round inner loop -- buys a signal CI produces anyway.

So this runner NEVER escalates to a full suite on its own. Every branch that used
to escalate runs the RELATED set instead and says so:

    related: N test file(s) (full suite deferred to CI)

This runner has no full-suite mode at all. A human who wants one has two
spellings already -- ``scripts/local-gate.py --full`` for both surfaces with the
bounded worker count, or a bare ``python -m pytest`` / ``npm test`` -- and a third
here would be a duplicate. No environment variable and no heuristic reaches a
full suite through this script.

Why a heuristic miss is affordable now
--------------------------------------
An earlier revision refused to narrow WITHIN the surface a change touches, and it
was right about the mechanism: six review rounds produced nine real findings --
absolute import, relative import, barrel re-export, in-package fixture, global
vitest setup, data-file read, cross-surface parity comparison, documentation
contract -- and they are not a defect list but one impossibility, that a text scan
cannot enumerate the ways a test can reach a module.

What changed is not that scan's soundness; it is what rides on it. Local green is
no longer asserted to stand in for the suite. CI runs the touched surface's full
suite on the merge ref regardless, so a miss here costs one CI round trip, not a
skipped test -- while
the full local suite cost roughly an hour per iteration and starved every other
session on this shared box.

The related set
---------------
Backend, as the union of:

* test files the diff itself touches
* ``test_<module>.py`` and ``test_<module>_*.py`` under a testpath, per changed
  module
* test files that textually reference a changed module -- an import, a quoted
  string for ``importlib.import_module``, a fixture name. Best-effort, and
  deliberately over-broad in the safe direction
* the cross-surface set ``ci-surface-tests.py`` computes, when the OTHER surface
  changed. That one is CI's own reduction rather than a second answer invented here

Frontend is the same shape over vitest specs, re-rooted to ``cwd=website``.

The set is never silently swapped for the full suite because it came out large:
the count is printed and the set is what runs. Two things still fail closed,
because both mean the gate cannot see what it is reducing:

* base ref missing or unresolvable -> exit 2, run nothing
* the diff or the cross-surface selector unreadable -> exit 2, run nothing

A target that could reach a runner as an OPTION is refused outright
(:func:`validated_targets`).

Parallelism is bounded rather than ``-n auto``: this box is shared, and one gate
taking every core starves every other session on it. See
:func:`local_gate_workers`.

Usage
-----
    SCOPED_TESTS_BASE_REF="$(git merge-base HEAD origin/main)" \
        python3 scripts/run_scoped_tests.py --surface backend

    python3 scripts/run_scoped_tests.py --surface frontend --dry-run
    python3 scripts/run_scoped_tests.py --test

Exit codes: 0 green, 1 tests failed, 2 usage/environment error.
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent

# A change to any of these can affect anything, so no reduction is defensible.
# Matched on the FILE NAME or on a PATH PREFIX, never as a bare substring:
# `clone_setup.py` contains "setup.py" and is an ordinary module.
BROAD_IMPACT_NAMES = frozenset(
    {
        "conftest.py",
        "setup.cfg",
        "setup.py",
        "pyproject.toml",
        "pytest.ini",
        "tox.ini",
        "uv.lock",
        "package.json",
        "package-lock.json",
    }
)

# Config files whose name varies by suffix (tsconfig.app.json, vite.config.ts,
# requirements-dev.txt), so the NAME is matched by prefix rather than in full.
BROAD_IMPACT_NAME_PREFIXES = (
    "tsconfig",
    "vite.config",
    "vitest.config",
    "vitest.workspace",
    "requirements",
)

# Every entry is asserted to exist by the self-test. An earlier revision carried
# `website/src/test/setup`, which resolves to nothing, so the real vitest setup
# graph was never treated as broad-impact and the gap sat undetected for four
# review rounds -- a dead path looks exactly like a working one.
BROAD_IMPACT_PATH_PREFIXES = (
    "src/kiro_crew/testing/",
    # The vitest setup graph, per vite.config.ts `setupFiles: './integration/setup.ts'`.
    # Every integration spec inherits it, and `mocks/server.ts` installs the global
    # MSW handlers they all rely on, so a change there can fail a spec that never
    # names it.
    "website/integration/setup.ts",
    "website/integration/mocks/",
    ".github/workflows/",
    "scripts/run_scoped_tests.py",
)

_SAFE_TARGET = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]*$")

# Bounded parallelism, shared by every pytest call the gate scripts make. The
# bound is expressed through xdist's own knob rather than an explicit `-n <N>`,
# so the root `xdist_budget.py` hook still runs and its protections compose:
# the live free-memory clamp, the per-host flock slots shared between concurrent
# runs, and any tighter cap the agent's spawn boundary already seeded.
XDIST_CAP_ENV = "PYTEST_XDIST_AUTO_NUM_WORKERS"
_WORKER_FLOOR = 2
_WORKER_CEILING = 12

# `setup.cfg`'s `testpaths`, and the shapes pytest and vitest each collect. Every
# root is asserted to exist by the self-test: a root that resolves to nothing
# looks exactly like a root with no matching tests, which is how a dead
# broad-impact prefix survived four review rounds here.
_BACKEND_TEST_ROOTS = ("test", "src/kiro_crew/apps/builtins")
_BACKEND_TEST_NAME = re.compile(r"^test_[A-Za-z0-9_]+\.py$")
# `website/electron/` is deliberately absent: those specs belong to the
# `node --test` lane and vitest cannot collect them.
_FRONTEND_SPEC_ROOTS = ("website/src", "website/integration")
_FRONTEND_SPEC_NAME = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$")
_SCAN_EXCLUDE = ("node_modules", "__pycache__", "_vendor", "build", "dist", ".venv")

# The canonical verdict. Pinned by the self-test and by test/test_local_gate.py so
# that a rename cannot quietly turn "we ran a subset" back into a claim of a full
# local run.
RELATED_VERDICT = "related: {count} test file(s) (full suite deferred to CI)"


def local_gate_workers() -> int:
    """The gate's worker CAP: ``min(max(cpu_count // 3, 2), 12)``.

    `xdist_budget.py` alone would let ``-n auto`` resolve to one worker per core
    on a host with the memory for it -- 32 on this 48-core shared box -- because
    its default cap (``KIROCREW_MAX_TEST_WORKERS``, 32) is tuned for the full
    suite's throughput, not for a gate that runs beside other sessions.
    ``cpu_count() // 3`` leaves room for those sessions, and the ceiling holds
    because past roughly a dozen workers a related-set run is dominated by
    per-worker startup rather than by test time.

    There is deliberately no override here: this is sized for the one caller the
    gate scripts have, the agent iterating on a shared box. A human who wants the
    machine saturated runs a bare ``python -m pytest`` and gets the budget's own
    default; a tighter ``PYTEST_XDIST_AUTO_NUM_WORKERS`` already in the
    environment is honoured over this cap (see :func:`pytest_worker_env`).
    """
    return min(max((os.cpu_count() or _WORKER_FLOOR) // 3, _WORKER_FLOOR), _WORKER_CEILING)


def pytest_parallel_args() -> list[str]:
    """``-n auto --dist loadgroup``: the budgeted form, never an explicit ``-n``.

    An explicit ``-n <N>`` would bypass `xdist_budget.py`'s ``firstresult`` hook
    entirely -- no live free-memory clamp, no flock slots shared with a concurrent
    run, no respect for a cap the agent's spawn boundary seeded -- so the bound
    goes through ``PYTEST_XDIST_AUTO_NUM_WORKERS`` (:func:`pytest_worker_env`)
    and ``auto`` is what the hook then budgets. ``--dist loadgroup`` keeps
    ``xdist_group``-marked modules serialized on one worker.
    """
    return ["-n", "auto", "--dist", "loadgroup"]


def pytest_worker_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a gate pytest run: the budget's cap knob set to this
    gate's bound, unless a tighter one is already there.

    A positive ``PYTEST_XDIST_AUTO_NUM_WORKERS`` in the environment is a
    deliberate cap -- Kiro Crew seeds a memory-aware one at every agent spawn --
    and `xdist_budget.py`'s rule is that an inherited cap must never be raised,
    so the result is the MIN of that and :func:`local_gate_workers`. An unset,
    empty or non-positive value is replaced by the gate's bound.
    """
    env = dict(os.environ if base is None else base)
    cap = local_gate_workers()
    raw = (env.get(XDIST_CAP_ENV) or "").strip()
    try:
        inherited = int(raw)
    except ValueError:
        inherited = 0
    if inherited > 0:
        cap = min(cap, inherited)
    env[XDIST_CAP_ENV] = str(cap)
    return env


def _rel_posix(path: Path, root: Path) -> str:
    """Root-relative path with forward slashes on every platform.

    ``str(Path.relative_to(...))`` yields ``test\\test_x.py`` on Windows, which
    `_SAFE_TARGET` does not admit, so every target would be refused as "not a
    plain relative path". Git speaks POSIX form on all platforms, which is also
    what the diff side of every comparison here carries.
    """
    return path.relative_to(root).as_posix()


@functools.lru_cache(maxsize=None)
def _tree_files(root: Path, rel_roots: tuple[str, ...], name_re: re.Pattern[str]) -> tuple[str, ...]:
    """Every collectable test file under ``rel_roots``, relative to ``root``.

    Cached because one ``related_targets`` call asks the same question for
    several token sets and the self-test asks it dozens of times; neither the set
    of files nor their contents change inside one process, so the walk is paid
    once rather than approximated.
    """
    out: list[str] = []
    for base in rel_roots:
        start = root / base
        if not start.is_dir():
            continue
        for path in start.rglob("*"):
            if any(part in _SCAN_EXCLUDE for part in path.parts):
                continue
            if name_re.search(path.name) and path.is_file():
                out.append(_rel_posix(path, root))
    return tuple(sorted(out))


@functools.lru_cache(maxsize=None)
def _read_text(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        # A file that cannot be read cannot be cleared of referencing the diff,
        # and silently skipping it would be exactly the wrong direction.
        raise SelectionUntrustworthy(f"cannot read {rel} while selecting related tests") from None


def reference_tokens(path: str) -> set[str]:
    """Strings a test would contain if it reaches ``path``.

    For a PYTHON module the bare stem is included, because that is what a reach
    is spelled as: ``import session``, ``from kiro_crew.session import ...``, a
    ``"session"`` handed to ``importlib.import_module``, or a fixture named after
    the module. It is over-broad on purpose -- ``session`` occurs in most of this
    tree -- and that over-breadth is now affordable, because it costs a longer
    local run rather than a skipped test.

    For a NON-Python file the bare stem is dropped and the last two path
    components are used instead. ``SKILL.md`` or ``index.ts`` as a bare name
    matches nearly every file that merely mentions a skill or a barrel, which is
    noise rather than reach; a test that really reads such a file names its path.
    """
    p = PurePosixPath(path)
    tokens = {path}
    if len(p.parts) >= 2:
        tokens.add("/".join(p.parts[-2:]))
    if p.suffix == ".py":
        tokens.add(p.stem)
        parts = list(p.parts)
        if parts and parts[0] == "src":
            parts = parts[1:]
        tokens.add(".".join([*parts[:-1], p.stem]))
    return {t for t in tokens if t}


@functools.lru_cache(maxsize=None)
def _word_bounded(token: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(token)}\b")


class _ReferenceMatcher:
    """Does a text reference any of a token set? A bare name is word-bounded, a
    path is not.

    Word-bounding a bare stem keeps ``session`` from matching ``sessions_view``
    while still matching ``import session`` and ``"session"``. A path or dotted
    module carries its own separators, so a plain substring is already specific.

    Shaped for the scan it serves: this runs over every test file in the tree
    (~4,600 files, ~2,100 of them read for a single surface). A single alternation
    regex ``\\ba\\b|b/c|d.e`` cannot use a literal fast path and cost ~0.4ms per
    file -- 20s of a self-test whose CI budget is 120s. A C-level ``in`` check
    per token rejects almost every file in microseconds, and the word-boundary
    regex (single literal, so ``re`` scans for it directly) runs only on the few
    files that contain the bare name at all. Same answer, two orders of magnitude
    cheaper.
    """

    def __init__(self, tokens: set[str]) -> None:
        self._paths = tuple(sorted(t for t in tokens if "/" in t))
        self._names = tuple(sorted(t for t in tokens if "/" not in t))

    def search(self, text: str) -> bool:
        for token in self._paths:
            if token in text:
                return True
        for token in self._names:
            if token in text and _word_bounded(token).search(text):
                return True
        return False


def _reference_matcher(tokens: set[str]) -> _ReferenceMatcher | None:
    if not tokens:
        return None
    return _ReferenceMatcher(tokens)


class SelectionUntrustworthy(Exception):
    """Raised when the gate cannot see what it is selecting.

    Callers exit 2 and run NOTHING. The earlier contract ("the caller runs
    everything") is gone on purpose: a gate that cannot read its own diff has no
    business spending an hour of a shared machine to hide that.
    """


class LauncherMissing(SelectionUntrustworthy):
    """A surface's test runner is not installed here (``npm``/``npx`` off PATH).

    A subclass so a caller driving BOTH surfaces can tell "this box cannot run
    vitest at all" from "this selection is not to be trusted": the first is an
    environment gap that should not discard the other surface's runnable set.
    """


# A selector or git call that has not returned in this long is not going to:
# `ci-surface-tests.py` is a tree walk that takes ~12s here, and every git call is
# local. Expiry becomes SelectionUntrustworthy (exit 2, run nothing) rather than
# a gate that never returns.
_SUBPROCESS_TIMEOUT_SECS = 120


def _run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd or REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired as exc:
        raise SelectionUntrustworthy(
            f"{' '.join(argv[:3])} did not return within {_SUBPROCESS_TIMEOUT_SECS}s"
        ) from exc


def resolve_base(base: str) -> str:
    """Return the resolved base sha, or raise ValueError. Never fails open."""
    base = (base or "").strip()
    if not base:
        raise ValueError(
            "SCOPED_TESTS_BASE_REF is empty. Without a base ref this cannot know "
            "what changed, and guessing one would reduce the wrong suite. Set it "
            "to `git merge-base HEAD origin/<base>`."
        )
    proc = _run(["git", "rev-parse", "--verify", "--quiet", base])
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        raise ValueError(
            f"base ref {base!r} is not present in this checkout. Fetch it first "
            "(`git fetch origin`) -- an unresolvable base must fail closed, not "
            "degrade to an empty diff that reduces everything."
        )
    return sha


def _parse_diff_z(text: str) -> set[str]:
    """Paths from ``git diff --name-only -z`` output (NUL-separated, unquoted)."""
    return {p for p in text.split("\0") if p.strip()}


def _parse_status_z(text: str) -> set[str]:
    """Paths from ``git status --porcelain -z`` output.

    Each record is ``XY <path>`` NUL-terminated. With ``-z`` git emits the path
    VERBATIM instead of C-quoting it, which is the whole point: without it a name
    carrying a non-ASCII byte, a quote or a newline comes back as
    ``"website/src/f\\303\\251e.tsx"`` -- leading double-quote included -- so a
    ``startswith("website/")`` test says "not frontend" and the frontend full
    suite is skipped for a frontend change.
    """
    paths: set[str] = set()
    for record in text.split("\0"):
        if len(record) > 3:
            path = record[3:]
            if path:
                paths.add(path)
    return paths


def changed_files(base_sha: str) -> list[str]:
    """Committed diff against the base PLUS uncommitted work.

    The gate runs before the push but sometimes before the commit too, so a
    committed-only diff would miss the very edit under review.

    BOTH endpoints of a rename are collected: ``--no-renames`` makes git report a
    rename as a delete plus an add, so the old path's surface ownership is not
    lost. Output is read NUL-delimited (``-z``) so a filename git would otherwise
    C-quote cannot be misclassified.
    """
    proc = _run(["git", "diff", "--name-only", "--no-renames", "-z", f"{base_sha}...HEAD"])
    if proc.returncode != 0:
        raise SelectionUntrustworthy(f"git diff failed: {proc.stderr.strip()}")
    paths = _parse_diff_z(proc.stdout)

    dirty = _run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--no-renames", "-z"]
    )
    if dirty.returncode != 0:
        raise SelectionUntrustworthy(f"git status failed: {dirty.stderr.strip()}")
    paths |= _parse_status_z(dirty.stdout)
    return sorted(paths)


def has_broad_impact(paths: list[str]) -> str | None:
    for path in paths:
        name = Path(path).name
        if name in BROAD_IMPACT_NAMES:
            return f"{path} (name {name!r})"
        if name.startswith(BROAD_IMPACT_NAME_PREFIXES):
            return f"{path} (config file {name!r})"
        if path.startswith(BROAD_IMPACT_PATH_PREFIXES):
            return f"{path} (under a broad-impact path)"
    return None


def surface_bucket(path: str) -> str:
    """Which of CI's buckets a changed path falls in (or ``ignored``).

    Transcribed from `ci.yml`'s `changes` job, which is the authority for this
    question and computes it once for the whole workflow:

        frontend: website/**
        meta:     .github/**  scripts/**
        ignored:  temp-screenshots/**  (evidence media; matches NO bucket)
        backend:  **  minus the three above

    An earlier revision folded `meta` into `backend`, which is wrong in a way that
    is invisible until it bites: `.github/scripts/frontend-blob-reconcile.mjs` is
    asserted on by `website/src/test/frontendBlobReconcile.wireFormat.test.ts`, and
    `scripts/` and `docs/` are read by several i18n and settings specs too. Meta
    paths belong to neither surface and can be read by both.

    ``ignored`` is safe in `plan()` by construction: it is never `meta`, never
    the surface under test, and never the *other* surface, so an ignored path
    can only ever leave the decision to the real files in the diff -- and an
    ignored-ONLY diff has no `other` in its buckets, which returns the full
    suite (fail-open), matching ci.yml where such a diff sets no flag.
    """
    if path.startswith("website/"):
        return "frontend"
    if path.startswith((".github/", "scripts/")):
        return "meta"
    if path.startswith("temp-screenshots/"):
        # Mirrors ci.yml's '!temp-screenshots/**' backend negation (#8027):
        # committed screenshot evidence must not drag a frontend-only diff
        # into the full backend matrix.
        return "ignored"
    # Catch-all, exactly as ci.yml comments it: "an unrecognised path counts as
    # backend and cannot ride along under a narrowed suite".
    return "backend"


@functools.lru_cache(maxsize=None)
def cross_surface_targets(surface: str) -> tuple[str, ...]:
    """The cross-surface set, as ``ci.yml`` computes it.

    `ci.yml` runs `ci-surface-tests.py` for a single-surface diff and executes the
    files the selector could NOT prove single-surface (parity guards and anything
    unclassified) -- a frontend-only change really can break a backend test that
    reads a frontend module, so a plain skip would be unsafe. Reusing that script
    keeps this gate at parity with CI instead of inventing a second answer.
    """
    proc = _run([sys.executable, "scripts/ci-surface-tests.py", "--surface", surface])
    if proc.returncode != 0:
        raise SelectionUntrustworthy(
            f"cross-surface selector failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}"
        )
    out = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if surface == "frontend":
        # Mirror ci.yml's own post-processing: vitest runs with cwd=website and
        # only covers website/**, and the Electron specs belong to the
        # `node --test` lane, which is always-on. Skipping this handed vitest
        # repo-relative paths for files that are not its specs.
        out = [p[len("website/") :] for p in out if p.startswith("website/")]
        out = [p for p in out if not p.startswith("electron/")]
    # An empty set is a fact, not a failure: the selector proved every file on
    # this surface single-surface. The old "raise, then run everything" reflex
    # would turn that best case into the most expensive one.
    #
    # Cached (hence the tuple): the selector walks both trees and costs ~12s for
    # the backend surface. Within one process the answer cannot change, and the
    # self-test asks for it from half a dozen diff shapes -- uncached, that alone
    # pushed `test_scoped_runner_self_test_passes` past its 120s CI timeout.
    return tuple(out)


def validated_targets(targets: list[str], root: Path) -> list[str]:
    """Reject any target that could act as an option or escape the tree.

    Targets come from a selector's stdout, so a file committed as
    ``--config=evil.ini`` would otherwise reach pytest as an OPTION rather than a
    path. There is no shell involved (argv is always a list, never
    ``shell=True``), so the exposure is ARGUMENT injection rather than command
    injection -- but a test runner's own flags are quite enough to do damage, and
    a bare ``--`` does not protect a runner that inspects argv before it.
    """
    root_resolved = root.resolve()
    safe: list[str] = []
    for target in targets:
        if not _SAFE_TARGET.match(target) or ".." in Path(target).parts:
            raise SelectionUntrustworthy(
                f"refusing a target that is not a plain relative path: {target!r}"
            )
        resolved = (root / target).resolve()
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            raise SelectionUntrustworthy(f"refusing a target outside the tree: {target!r}") from None
        if not resolved.is_file():
            raise SelectionUntrustworthy(f"target vanished before the run: {target!r}")
        safe.append(target)
    return safe


def backend_argv(targets: list[str] | None) -> list[str]:
    """pytest argv for a related set, or (``None``) for `local-gate.py --full`.

    Parallelism is stated explicitly in BOTH shapes. `setup.cfg` supplies
    ``-n auto``, and an addopts value is only overridden by a LATER one on the
    command line, so a bounded ``-n`` has to be passed here rather than inherited.
    """
    argv = [sys.executable, "-m", "pytest", "-q", *pytest_parallel_args()]
    if targets:
        # `--` ends option parsing so nothing after it can be read as a flag.
        # Coverage is off: a subset's coverage is not comparable to the repo
        # floor, which is why CI skips the coverage lane for its reduced runs.
        argv += ["--no-cov", "--", *validated_targets(targets, REPO_ROOT)]
    return argv


def _node_launcher(name: str) -> str:
    """Absolute path to ``npm``/``npx``, resolved the way this repo already does.

    ``subprocess.run([...], shell=False)`` cannot execute a bare ``npm`` on native
    Windows, because what exists on PATH is ``npm.cmd`` -- the gate would die with
    FileNotFoundError before running a single spec. Hardcoding ``.cmd`` would work
    for that one case; ``shutil.which`` is what `mcp_gateway/resolve_once.py:641`
    already uses and additionally covers ``npm.exe``, nvm shims, and the
    genuinely-missing case, which becomes a named error instead of a crash.
    """
    found = shutil.which(name)
    if not found:
        raise LauncherMissing(f"{name} is not on PATH, so the frontend suite cannot be launched")
    return found


def frontend_argv(targets: list[str] | None) -> list[str]:
    if not targets:
        return [_node_launcher("npm"), "--prefix", "website", "test"]
    # NO `--` here, unlike the pytest builder: `vitest run -- <paths>` silently
    # stops treating the positionals as filters and runs the WHOLE suite. That was
    # measured -- 1,474 spec files and 22,939 tests -- while the gate still
    # reported a narrow scope, so the report disagreed with the run.
    # `validated_targets` is the real protection and is strictly stronger anyway:
    # it rejects a leading `-` outright rather than asking the runner to stop
    # parsing.
    return [
        _node_launcher("npx"),
        "vitest",
        "run",
        *validated_targets(targets, REPO_ROOT / "website"),
    ]


# Windows caps a CreateProcess command line at 32,767 characters; a related set
# for a widely-referenced module (1,045 files for `session.py`, ~36K chars of
# argv) blows through it and the gate dies with WinError 206 before pytest
# starts. POSIX ARG_MAX is ~2MB and never hits this, but the gate has to work on
# a Windows checkout too, so every runner invocation is split into batches whose
# joined argv stays under this bound. The margin below 32,767 covers the
# per-argument quoting Windows adds that a plain `" ".join` does not see.
ARGV_CHAR_LIMIT = 24_000


def argv_batches(
    build: Callable[[list[str] | None], list[str]],
    targets: list[str],
    limit: int = ARGV_CHAR_LIMIT,
) -> list[list[str]]:
    """Split ``targets`` into runner argvs whose joined length stays under ``limit``.

    Order is preserved and every target lands in exactly one batch. Each batch
    goes through ``build`` (and so through ``validated_targets``) once; the
    fixed cost of the argv is measured from one real build rather than guessed,
    so a change to the runner's flags cannot silently push a batch over the
    limit.
    """
    if not targets:
        return []
    probe = build([targets[0]])
    fixed = len(" ".join(probe)) - len(targets[0])
    batches: list[list[str]] = []
    current: list[str] = []
    length = fixed
    for target in targets:
        cost = len(target) + 1
        if current and length + cost > limit:
            batches.append(current)
            current = []
            length = fixed
        current.append(target)
        length += cost
    if current:
        batches.append(current)
    return [build(batch) for batch in batches]


def _diff_tests(surface: str, paths: list[str]) -> list[str]:
    """(a) Test files the diff itself touches, in the runner's own path space."""
    out: list[str] = []
    for path in paths:
        name = PurePosixPath(path).name
        if surface == "backend":
            if path.startswith(tuple(f"{r}/" for r in _BACKEND_TEST_ROOTS)) and _BACKEND_TEST_NAME.search(name):
                out.append(path)
        elif path.startswith("website/") and not path.startswith("website/electron/"):
            if _FRONTEND_SPEC_NAME.search(name):
                out.append(path[len("website/") :])
    return out


def _name_matched_tests(surface: str, paths: list[str], tree: tuple[str, ...]) -> list[str]:
    """(b) ``test_<module>.py`` / ``test_<module>_*.py`` for each changed module.

    The frontend mirror is vitest's own convention -- ``Foo.test.ts`` and
    ``Foo.<aspect>.test.ts`` beside ``Foo.tsx`` -- so the join is on the stem
    followed by a dot, not on a bare substring.
    """
    stems = {PurePosixPath(p).stem for p in paths}
    stems = {s for s in stems if s}
    if not stems:
        return []
    out: list[str] = []
    for rel in tree:
        name = PurePosixPath(rel).name
        for stem in stems:
            if surface == "backend":
                if name == f"test_{stem}.py" or name.startswith(f"test_{stem}_"):
                    out.append(rel)
                    break
            elif name.startswith(f"{stem}."):
                out.append(rel)
                break
    return out


def _referencing_tests(paths: list[str], tree: tuple[str, ...], root: Path) -> list[str]:
    """(c) Test files that textually reference a changed file.

    The heuristic an earlier revision removed, restored deliberately and with a
    different job: it no longer decides whether a test may be SKIPPED (CI runs
    everything either way), only which tests are worth running first, locally.
    """
    tokens: set[str] = set()
    for path in paths:
        tokens |= reference_tokens(path)
    matcher = _reference_matcher(tokens)
    if matcher is None:
        return []
    changed = set(paths)
    return [rel for rel in tree if rel not in changed and matcher.search(_read_text(root, rel))]


def related_targets(
    surface: str, paths: list[str], root: Path = REPO_ROOT
) -> tuple[list[str], str]:
    """The related set for ``surface``, plus the verdict line describing it.

    Never returns "run everything": there is no code path from a diff to a full
    local suite. A large set stays a large set, and its size is printed.
    """
    tree_root = root if surface == "backend" else root / "website"
    if surface == "backend":
        tree = _tree_files(root, _BACKEND_TEST_ROOTS, _BACKEND_TEST_NAME)
    else:
        tree = tuple(
            p[len("website/") :]
            for p in _tree_files(root, _FRONTEND_SPEC_ROOTS, _FRONTEND_SPEC_NAME)
        )

    selected: set[str] = set()
    notes: list[str] = []

    for label, found in (
        ("in the diff", _diff_tests(surface, paths)),
        ("name-matched", _name_matched_tests(surface, paths, tree)),
        ("referencing a changed file", _referencing_tests(paths, tree, tree_root)),
    ):
        fresh = {f for f in found if f not in selected}
        if fresh:
            notes.append(f"{len(fresh)} {label}")
            selected |= fresh

    other = "frontend" if surface == "backend" else "backend"
    if other in {surface_bucket(p) for p in paths}:
        # CI's own cross-surface reduction, computed by CI's own script, so there
        # is no second answer invented here to be wrong.
        cross = {c for c in cross_surface_targets(surface) if c not in selected}
        if cross:
            notes.append(f"{len(cross)} cross-surface (ci-surface-tests.py)")
            selected |= cross

    # A path named by the diff can be one the diff DELETED. Dropping it here keeps
    # a deletion from tripping validated_targets' "target vanished" refusal, which
    # would otherwise fail a gate over a file that is correctly gone.
    targets = sorted(t for t in selected if (tree_root / t).is_file())
    verdict = RELATED_VERDICT.format(count=len(targets))
    if notes:
        verdict = f"{verdict}: {'; '.join(notes)}"
    return targets, verdict


def plan(surface: str, base: str) -> tuple[list[str], str]:
    """Return (targets, verdict). Raises rather than ever answering "everything".

    ``ValueError`` (no usable base ref) and ``SelectionUntrustworthy`` (the diff
    or CI's selector could not be read) both mean the same thing: the gate cannot
    see what it is reducing. That fails closed at exit 2 -- run nothing, say why --
    because the old fallback of "then run everything" is the behaviour this script
    exists to remove.
    """
    base_sha = resolve_base(base)
    paths = changed_files(base_sha)
    if not paths:
        return [], RELATED_VERDICT.format(count=0) + ": the diff is empty against the base"
    return related_targets(surface, paths)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", choices=("backend", "frontend"))
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    parser.add_argument("--test", action="store_true", help="run this script's self-test")
    args = parser.parse_args(argv)

    if args.test:
        return _self_test()
    if not args.surface:
        parser.error("--surface is required (or pass --test)")

    build = backend_argv if args.surface == "backend" else frontend_argv

    try:
        targets, reason = plan(args.surface, os.environ.get("SCOPED_TESTS_BASE_REF", ""))
    except ValueError as exc:
        print(f"run_scoped_tests: {exc}", file=sys.stderr)
        return 2
    except SelectionUntrustworthy as exc:
        # Fails closed, and deliberately NOT by running everything: a gate
        # that cannot read the diff cannot know what is related to it.
        print(f"run_scoped_tests: cannot select related tests: {exc}", file=sys.stderr)
        return 2

    try:
        cmds = argv_batches(build, targets)
    except SelectionUntrustworthy as exc:
        # A target that could pass for an option, or a missing runner. Either way
        # this is an environment problem to report, not a reason to run the suite.
        print(f"run_scoped_tests: {exc}", file=sys.stderr)
        return 2

    print(f"run_scoped_tests[{args.surface}]: {reason}")
    for target in targets[:20]:
        print(f"  - {target}")
    if len(targets) > 20:
        print(f"  ... +{len(targets) - 20} more")
    if not targets:
        print(
            f"run_scoped_tests[{args.surface}]: nothing related to run -- "
            "the full suite runs in CI on refs/pull/<N>/merge"
        )
        return 0
    if len(cmds) > 1:
        print(
            f"run_scoped_tests[{args.surface}]: {len(targets)} target(s) in {len(cmds)} "
            f"batch(es) so no argv exceeds {ARGV_CHAR_LIMIT} chars"
        )
    for cmd in cmds:
        print(f"run_scoped_tests[{args.surface}]: $ {' '.join(cmd[:12])}{' ...' if len(cmd) > 12 else ''}")
    if args.dry_run:
        return 0

    cwd = REPO_ROOT / "website" if args.surface == "frontend" else REPO_ROOT
    for cmd in cmds:
        rc = _run_batch(cmd, cwd)
        if rc != 0:
            return rc
    return 0


def _run_batch(cmd: list[str], cwd: Path) -> int:
    # Suppressed rather than fixed, and the reasoning is worth stating: argv is
    # always a list and shell=True is never used, so there is no shell to inject
    # into. The ARGUMENT-injection risk that remains -- a selector path posing as
    # an option -- is closed by validated_targets(), which rejects anything not
    # matching ^[A-Za-z0-9_][A-Za-z0-9._/-]*$, refuses traversal, and requires the
    # target to resolve to a real file inside the runner's root. Same rule and same
    # reasoning as pod-playwright.py:266 and narrate.py:237.
    #
    # The annotation must sit on the line Semgrep REPORTS. Splitting this call
    # across lines moved the report onto the argument line, where a comment on the
    # preceding line no longer suppressed it.
    return subprocess.run(cmd, cwd=str(cwd), env=pytest_worker_env(), check=False).returncode  # noqa: E501  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args


def _self_test() -> int:
    """Prove no diff shape reaches a full local suite, and that the caps hold.

    The old contract was the mirror of this one -- "prove the escalations fire" --
    so these checks are what keeps a future edit from restoring escalation by
    accident: every diff shape that used to answer "run everything" is asserted
    here to answer with a list and a verdict that names CI as the full suite's
    owner. A reducer trusted without that is a guess.
    """
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            failures.append(name)

    # An absent or unresolvable base must fail closed, never report an empty diff.
    for bad in ("", "   ", "definitely-not-a-ref-zzz"):
        try:
            resolve_base(bad)
            failures.append(f"resolve_base accepted {bad!r}")
        except ValueError:
            pass

    # EVERY hardcoded path must be asserted to exist. `website/src/test/setup`
    # resolved to nothing and the gap survived four review rounds because a dead
    # path is indistinguishable from a working one.
    for prefix in BROAD_IMPACT_PATH_PREFIXES:
        check(f"broad-impact prefix resolves: {prefix}", (REPO_ROOT / prefix.rstrip("/")).exists())

    check("broad impact: conftest", has_broad_impact(["test/conftest.py"]) is not None)
    check("broad impact: workflow", has_broad_impact([".github/workflows/ci.yml"]) is not None)
    check("broad impact: package.json", has_broad_impact(["website/package.json"]) is not None)
    check("broad impact: self", has_broad_impact(["scripts/run_scoped_tests.py"]) is not None)
    check("broad impact: tsconfig variant", has_broad_impact(["website/tsconfig.app.json"]) is not None)
    check("broad impact: requirements variant", has_broad_impact(["requirements-dev.txt"]) is not None)
    check("broad impact: vitest setup graph", has_broad_impact(["website/integration/setup.ts"]) is not None)
    check("broad impact: global MSW handlers", has_broad_impact(["website/integration/mocks/server.ts"]) is not None)
    check("broad impact: ordinary file is not broad", has_broad_impact(["src/kiro_crew/session.py"]) is None)
    # Regression trap: substring matching escalated any path merely CONTAINING a
    # marker, so `clone_setup.py` was treated as `setup.py`.
    check(
        "broad impact: a name merely containing a marker is NOT broad",
        has_broad_impact(["src/kiro_crew/apps/builtins/auto_improvement/backend/clone_setup.py"]) is None,
    )

    # Git C-quotes a path carrying a non-ASCII byte, a quote or a newline unless
    # asked for NUL-delimited output, and a quoted `"website/src/..."` fails a
    # startswith("website/") test -- so a frontend change would have been
    # classified backend and its full suite skipped. Parsing is separated from the
    # git call so the hostile shapes can be asserted without creating such files.
    hostile = "website/src/fée.tsx"
    newlined = "website/src/we ird\nname.tsx"
    assert "\0" not in hostile
    diff_out = f"src/kiro_crew/a.py\0{hostile}\0{newlined}\0"
    parsed = _parse_diff_z(diff_out)
    check("diff -z keeps a non-ASCII path verbatim", hostile in parsed)
    check("diff -z keeps a newline-bearing path whole", newlined in parsed)
    check("a non-ASCII website path is classified frontend", surface_bucket(hostile) == "frontend")
    check(
        "a newline-bearing website path is classified frontend",
        surface_bucket(newlined) == "frontend",
    )
    status_out = f" M src/kiro_crew/a.py\0?? {hostile}\0 M {newlined}\0"
    sparsed = _parse_status_z(status_out)
    check("status -z strips only the 3-char prefix", "src/kiro_crew/a.py" in sparsed)
    check("status -z keeps a non-ASCII path verbatim", hostile in sparsed)
    check("status -z keeps a newline-bearing path whole", newlined in sparsed)
    check(
        "status -z does not strip a leading quote it never added",
        not any(p.startswith('"') for p in sparsed),
    )

    # CI's three buckets, transcribed from ci.yml's `changes` job. Regression trap
    # for folding `meta` into `backend`: `.github/scripts/frontend-blob-reconcile.mjs`
    # is asserted on by a FRONTEND spec, so treating it as backend let a reduced
    # frontend run drop that spec.
    check("bucket: website is frontend", surface_bucket("website/src/App.tsx") == "frontend")
    check("bucket: src is backend", surface_bucket("src/kiro_crew/session.py") == "backend")
    check("bucket: test is backend", surface_bucket("test/test_x.py") == "backend")
    check("bucket: docs are backend (ci.yml catch-all)", surface_bucket("docs/guides/install.md") == "backend")
    check("bucket: root files are backend", surface_bucket("README.md") == "backend")
    check("bucket: .github is meta, NOT backend", surface_bucket(".github/scripts/frontend-blob-reconcile.mjs") == "meta")
    check("bucket: workflows are meta", surface_bucket(".github/workflows/ci.yml") == "meta")
    check("bucket: scripts are meta, NOT backend", surface_bucket("scripts/ci-surface-tests.py") == "meta")
    check("bucket: the runner itself is meta", surface_bucket("scripts/run_scoped_tests.py") == "meta")
    check("bucket: temp-screenshots is ignored, NOT backend (#8027)", surface_bucket("temp-screenshots/feature/shot.png") == "ignored")
    check("bucket: ignored is a prefix, not a substring", surface_bucket("temp-screenshotsx/evil.py") == "backend")

    # The cross-surface list feeds a runner directly, so it must arrive in that
    # runner's path space. Regression trap: unprocessed, it handed vitest
    # `website/electron/test/*.test.js` -- repo-relative, and not vitest specs.
    try:
        xs_fe = cross_surface_targets("frontend")
        check("cross-surface frontend list is website-relative", all(not p.startswith("website/") for p in xs_fe))
        check("cross-surface frontend list excludes the electron lane", all(not p.startswith("electron/") for p in xs_fe))
        check("cross-surface frontend list is non-empty", len(xs_fe) > 0)
        frontend_argv(list(xs_fe))
    except SelectionUntrustworthy as exc:
        failures.append(f"cross-surface frontend list unusable: {exc}")
    try:
        xs_be = cross_surface_targets("backend")
        check("cross-surface backend list is non-empty", len(xs_be) > 0)
        backend_argv(list(xs_be))
    except SelectionUntrustworthy as exc:
        failures.append(f"cross-surface backend list unusable: {exc}")

    # Full-suite argv must stay CI's exact command, so a fallback is not a
    # different, weaker check than the gate it replaces. The launcher is compared
    # by BASENAME because it is resolved to an absolute path -- `subprocess.run`
    # with shell=False cannot execute a bare `npm` on native Windows, where what
    # exists on PATH is `npm.cmd`.
    check(
        "backend full argv",
        backend_argv(None) == [sys.executable, "-m", "pytest", "-q", *pytest_parallel_args()],
    )
    try:
        fe_full = frontend_argv(None)
        check("frontend full argv launcher is npm", Path(fe_full[0]).stem == "npm")
        check("frontend full argv tail", fe_full[1:] == ["--prefix", "website", "test"])
        check("frontend launcher is resolved, not a bare name", Path(fe_full[0]).is_absolute())
    except SelectionUntrustworthy as exc:
        failures.append(f"frontend full argv unbuildable: {exc}")

    real = "test/test_prepare_pr_profiles.py"
    be = backend_argv([real])
    check("backend reduced argv has --no-cov", "--no-cov" in be)
    check("backend reduced argv separates positionals with --", "--" in be and be[-1] == real)
    for hostile in ("--config=evil.ini", "-p no:randomly", "../outside.py", "/etc/passwd"):
        try:
            backend_argv([hostile])
            failures.append(f"backend_argv accepted a hostile target: {hostile!r}")
        except SelectionUntrustworthy:
            pass
    try:
        backend_argv(["test/this_file_does_not_exist_zz.py"])
        failures.append("backend_argv accepted a target that is not a real file")
    except SelectionUntrustworthy:
        pass
    fe = frontend_argv(["src/test/i18nGateTable.test.ts"])
    check("frontend reduced argv is vitest run", [Path(fe[0]).stem, *fe[1:3]] == ["npx", "vitest", "run"])
    # Regression trap: `vitest run -- <paths>` silently stops filtering and runs
    # the WHOLE suite (measured: 1,474 files / 22,939 tests) while the gate still
    # reports a narrow scope.
    check("frontend reduced argv must NOT carry a -- separator", "--" not in fe)
    for hostile in ("--reporter=evil", "../outside.test.ts"):
        try:
            frontend_argv([hostile])
            failures.append(f"frontend_argv accepted a hostile target: {hostile!r}")
        except SelectionUntrustworthy:
            pass

    # Bounded parallelism, expressed through xdist_budget's knob so its memory
    # clamp and shared slots still apply. The argv says `auto`; the ENV carries
    # the cap, and an inherited tighter cap is never raised.
    cap = local_gate_workers()
    check("worker cap is at least the floor", cap >= _WORKER_FLOOR)
    check("worker cap is bounded", cap <= _WORKER_CEILING)
    args = pytest_parallel_args()
    check("parallel args are the budgeted -n auto", args[:2] == ["-n", "auto"])
    check("parallel args keep loadgroup", args[-2:] == ["--dist", "loadgroup"])
    check("full argv is the budgeted form too", backend_argv(None)[-4:] == args)
    check("env carries the cap when unset", pytest_worker_env({})[XDIST_CAP_ENV] == str(cap))
    check("env replaces an empty cap", pytest_worker_env({XDIST_CAP_ENV: ""})[XDIST_CAP_ENV] == str(cap))
    check("env replaces a non-numeric cap", pytest_worker_env({XDIST_CAP_ENV: "many"})[XDIST_CAP_ENV] == str(cap))
    check("env replaces a non-positive cap", pytest_worker_env({XDIST_CAP_ENV: "0"})[XDIST_CAP_ENV] == str(cap))
    check("a tighter inherited cap is kept", pytest_worker_env({XDIST_CAP_ENV: "1"})[XDIST_CAP_ENV] == "1")
    check("a looser inherited cap is lowered", pytest_worker_env({XDIST_CAP_ENV: "64"})[XDIST_CAP_ENV] == str(cap))
    check("other env is passed through", pytest_worker_env({"KEEP": "x"})["KEEP"] == "x")

    # Every test root must resolve. An empty root and a missing one are
    # indistinguishable from the selection they produce.
    for root_rel in _BACKEND_TEST_ROOTS + _FRONTEND_SPEC_ROOTS:
        check(f"test root resolves: {root_rel}", (REPO_ROOT / root_rel).is_dir())

    # The related set, and the invariant that replaced escalation: NO diff shape
    # yields a full local suite. Each of these used to return `None`.
    for label, diff in (
        ("this surface", ["src/kiro_crew/session.py"]),
        ("broad-impact", ["setup.cfg"]),
        ("meta", [".github/workflows/ci.yml"]),
        ("both surfaces", ["src/kiro_crew/session.py", "website/src/App.tsx"]),
        ("neither surface", ["temp-screenshots/feature/shot.png"]),
        ("the runner itself", ["scripts/run_scoped_tests.py"]),
    ):
        for surface in ("backend", "frontend"):
            targets, verdict = related_targets(surface, diff)
            check(
                f"{label} diff returns a related list, not the full suite ({surface})",
                isinstance(targets, list),
            )
            check(
                f"{label} diff verdict names the deferral ({surface})",
                "full suite deferred to CI" in verdict,
            )
            check(
                f"{label} diff verdict never claims a full local run ({surface})",
                not verdict.startswith("full suite"),
            )

    be_targets, be_verdict = related_targets("backend", ["src/kiro_crew/session.py"])
    check("a changed module selects its name-matched tests", "test/test_session.py" in be_targets)
    check("the backend related set is non-empty for a real module", len(be_targets) > 1)
    check("the verdict counts what it selected", f"related: {len(be_targets)} test file(s)" in be_verdict)
    backend_argv(be_targets)

    diff_test = "test/test_prepare_pr_profiles.py"
    picked, _ = related_targets("backend", [diff_test])
    check("a changed test file is itself selected", diff_test in picked)

    fe_targets, _ = related_targets("frontend", ["website/src/App.tsx"])
    check("the frontend related set is website-relative", all(not p.startswith("website/") for p in fe_targets))
    check("the frontend related set excludes the electron lane", all(not p.startswith("electron/") for p in fe_targets))
    check("the frontend related set is non-empty for a real module", bool(fe_targets))
    frontend_argv(fe_targets)

    fe_spec = "website/src/test/AcpAdapter.defaults.test.ts"
    fe_picked, _ = related_targets("frontend", [fe_spec])
    check(
        "a changed spec is itself selected, re-rooted for vitest",
        fe_spec[len("website/") :] in fe_picked,
    )

    # A backend-only diff must still pick up CI's cross-surface frontend set, and
    # vice versa -- that component is the one reduction CI itself computes.
    fe_from_backend, fe_reason = related_targets("frontend", ["src/kiro_crew/session.py"])
    check("a backend diff pulls the cross-surface frontend set", "cross-surface" in fe_reason)
    check("cross-surface frontend targets are website-relative", all(not p.startswith("website/") for p in fe_from_backend))
    _be_from_frontend, be_reason = related_targets("backend", ["website/src/App.tsx"])
    check("a frontend diff pulls the cross-surface backend set", "cross-surface" in be_reason)

    # Argv batching: a related set for a widely-referenced module is ~1,000 files,
    # and Windows caps a command line at 32,767 chars. Every batch must stay under
    # the limit, every target must land in exactly one batch, order preserved.
    tree_targets = list(_tree_files(REPO_ROOT, _BACKEND_TEST_ROOTS, _BACKEND_TEST_NAME))
    check("enough real test files to exercise batching", len(tree_targets) > 200)
    small_limit = 2_000
    batches = argv_batches(backend_argv, tree_targets, limit=small_limit)
    check("a large set is split into several batches", len(batches) > 1)
    check(
        "every batch's joined argv stays under the limit",
        all(len(" ".join(b)) <= small_limit for b in batches),
    )
    flattened = [t for b in batches for t in b[b.index("--") + 1 :]]
    check("batches cover every target exactly once, in order", flattened == tree_targets)
    check("every batch carries --no-cov and the budgeted -n", all("--no-cov" in b and "auto" in b for b in batches))
    check("the default limit leaves margin under Windows' 32,767", ARGV_CHAR_LIMIT < 32_000)
    check("a small set is one batch", len(argv_batches(backend_argv, [real])) == 1)
    check("no targets, no batches", argv_batches(backend_argv, []) == [])
    fe_batches = argv_batches(frontend_argv, ["src/test/i18nGateTable.test.ts"])
    check("frontend batching goes through frontend_argv", len(fe_batches) == 1 and "vitest" in fe_batches[0])

    # Reference tokens: a python module contributes its stem (that is what an
    # import or an importlib string spells), a non-python file does not, because a
    # bare `SKILL.md` matches nearly everything that mentions a skill.
    py_tokens = reference_tokens("src/kiro_crew/session.py")
    check("python tokens carry the stem", "session" in py_tokens)
    check("python tokens carry the dotted path", "kiro_crew.session" in py_tokens)
    check("python tokens carry the repo path", "src/kiro_crew/session.py" in py_tokens)
    md_tokens = reference_tokens("src/kiro_crew/builtin_skills/kirocrew-dev/prepare-pr/SKILL.md")
    check("a non-python file does not contribute a bare basename", "SKILL.md" not in md_tokens)
    check("a non-python file contributes its last two components", "prepare-pr/SKILL.md" in md_tokens)
    check(
        "a bare stem is word-bounded, not a substring",
        not _reference_matcher({"session"}).search("sessions_view"),
    )
    check(
        "a path token is a plain substring",
        _reference_matcher({"kiro_crew/session.py"}).search("reads src/kiro_crew/session.py here"),
    )
    check(
        "a dotted module is a plain substring",
        _reference_matcher({"kiro_crew.session"}).search("from kiro_crew.session import x"),
    )
    check("no tokens, no matcher", _reference_matcher(set()) is None)
    check(
        "a bare stem still matches an import and a quoted string",
        bool(_reference_matcher({"session"}).search('import session; x = "session"')),
    )

    if failures:
        for name in failures:
            print(f"FAIL {name}", file=sys.stderr)
        print(f"run_scoped_tests self-test: {len(failures)} failure(s)", file=sys.stderr)
        return 1
    print("run_scoped_tests self-test: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
