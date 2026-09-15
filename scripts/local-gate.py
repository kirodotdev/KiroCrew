#!/usr/bin/env python3
"""Change-scoped LOCAL test gate: run the tests a diff is related to, on both surfaces.

Why this exists
---------------
The iteration loop on this repo (review-fix rounds, babysit amends) used to run
the full backend suite -- 62,108 tests, roughly an hour, and with ``-n auto`` one
xdist worker per core -- for every change whose diff touched more than one
surface or any ``scripts/`` file. On a shared 48-core box that is 32 workers
owning the machine while every other session crawls, to produce a signal CI
produces anyway on ``refs/pull/<N>/merge``.

This gate never does that on its own. It runs the RELATED set on each surface,
computed by ``scripts/run_scoped_tests.py`` (the same module the ``gates[]`` in
the prepare-pr profile call one surface at a time), with a bounded worker count.
The full suite is CI's job.

Contract
--------
- Every changed file (committed since the merge-base, staged, unstaged or
  untracked) lands in exactly one of CI's buckets -- ``frontend``
  (``website/**``), ``meta`` (``.github/**``, ``scripts/**``), ``backend``
  (everything else) -- or, for ``temp-screenshots/**`` evidence, none. The
  buckets are pinned to ``ci.yml`` by ``test/test_local_gate.py``; they are
  reported, and they decide whether the cross-surface set joins the plan.
- For EACH surface the plan is that surface's related set: test files in the
  diff, ``test_<module>*.py`` / ``<Stem>.*.test.ts`` beside changed modules,
  tests that textually reference a changed file, and -- when the OTHER surface
  changed -- the cross-surface set ``ci-surface-tests.py`` computes for CI.
  A surface with nothing related is skipped and says so.
- There is NO automatic path to a full suite. Not for a meta change, not for a
  diff touching both surfaces, not for a large related set (its size is printed
  and it runs). ``--full`` exists for a human who asks for one.
- A diff the gate cannot read (git failure, unresolvable base, a selector target
  that could pass for an option) fails CLOSED: exit 2, run nothing, say why.
  The old fallback of "then run everything" is the behaviour this script exists
  to remove.

Usage
-----
    scripts/local-gate.py                  # diff against merge-base with origin/main
    scripts/local-gate.py --base REF       # explicit base ref
    scripts/local-gate.py --dry-run        # print the plan, run nothing
    scripts/local-gate.py --full           # both surfaces' FULL suites (human-requested)

Exit status is the gate's: 0 when every selected command passed (or nothing was
related), 2 when the diff could not be read, non-zero otherwise. ``--dry-run``
exits 0 after printing the plan. pytest runs as ``-n auto`` with
``PYTEST_XDIST_AUTO_NUM_WORKERS`` capped at ``min(max(cpu_count // 3, 2), 12)``,
so the root ``xdist_budget.py`` still applies its memory clamp and shared slots
under that cap; a human who wants the machine saturated runs a bare
``python -m pytest`` instead.
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# One selection module for the whole local gate. The per-surface `gates[]`
# entries in the prepare-pr profile call `run_scoped_tests.py --surface X`
# directly; this driver runs the same selection for both surfaces in one go so
# an iteration pass has one command to type and one verdict to read.
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
from run_scoped_tests import (  # noqa: E402  (path set immediately above)
    LauncherMissing,
    SelectionUntrustworthy,
    argv_batches,
    backend_argv,
    frontend_argv,
    pytest_worker_env,
    related_targets,
)

# Bucket rules -- MUST mirror ci.yml's `changes` job filters. test_local_gate.py
# pins this against the workflow file so drift fails a test instead of shipping.
_FRONTEND_PREFIXES = ("website/",)
_META_PREFIXES = (".github/", "scripts/")
# Evidence media ci.yml excludes from EVERY bucket (#8027): temp-screenshots/**
# is never packaged or imported. A path here sets NO flag.
_IGNORED_PREFIXES = ("temp-screenshots/",)
_NODE_LAUNCHERS = frozenset({"npm", "npx"})


class GateCannotSee(Exception):
    """The gate cannot read what it is meant to select on. Exit 2, run nothing."""


def classify(paths: list[str]) -> tuple[bool, bool, bool]:
    """Return (frontend, meta, backend) touched-flags for a changed-file list.

    Backend is the catch-all: any path that is neither frontend nor meta counts
    as backend, including paths that do not exist yet (adds) or any unexpected
    shape. There is deliberately NO "unknown" outcome. The one carve-out is
    ``_IGNORED_PREFIXES`` (screenshot evidence), which sets no flag at all --
    mirroring ci.yml, where those paths match no bucket.
    """
    frontend = meta = backend = False
    for raw in paths:
        p = raw.strip().replace("\\", "/")
        if not p:
            continue
        if p.startswith(_IGNORED_PREFIXES):
            continue
        if p.startswith(_FRONTEND_PREFIXES):
            frontend = True
        elif p.startswith(_META_PREFIXES):
            meta = True
        else:
            backend = True
    return frontend, meta, backend


def changed_files(base: str) -> list[str] | None:
    """Changed paths vs the merge-base with ``base``; None means "cannot tell".

    Includes uncommitted work (staged + unstaged + untracked) -- the local gate
    verifies the working tree, not just commits. Any git failure returns None,
    which ``build_plan`` maps to ``GateCannotSee`` -- exit 2, run nothing. Never
    to a full run.

    BOTH endpoints of a rename are collected: ``--no-renames`` makes git report
    a rename as a delete plus an add (the same contract run_scoped_tests.py
    documents, and the same one dorny/paths-filter applies in CI), so renaming
    a real file INTO an ignored evidence path cannot hide the old path's
    bucket from classification.
    """
    try:
        merge_base = subprocess.run(
            ["git", "merge-base", "HEAD", base],
            cwd=_REPO_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        if merge_base.returncode != 0:
            return None
        committed = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames",
             merge_base.stdout.strip(), "HEAD"],
            cwd=_REPO_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        working = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all", "--no-renames"],
            cwd=_REPO_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        if committed.returncode != 0 or working.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    paths = [line for line in committed.stdout.splitlines() if line.strip()]
    for line in working.stdout.splitlines():
        # porcelain: "XY path". Renames cannot appear (--no-renames above), but
        # parse the "old -> new" arrow defensively and keep BOTH sides -- the
        # old path's bucket must not vanish just because the file moved.
        for part in line[3:].split(" -> "):
            entry = part.strip().strip('"')
            if entry:
                paths.append(entry)
    return paths


class Plan:
    """The commands the gate will run, plus the reason it chose them."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.commands: list[tuple[str, list[str], Path]] = []
        self.notes: list[str] = []
        # Surfaces whose related set exists but has no runner on this box.
        self.skipped: list[str] = []

    def add(self, label: str, argv: list[str], cwd: Path) -> None:
        self.commands.append((label, argv, cwd))


def _backend_full(plan: Plan) -> None:
    plan.add("backend (full)", backend_argv(None), _REPO_ROOT)


def _frontend_full(plan: Plan) -> None:
    plan.add("frontend (full)", ["npm", "test"], _REPO_ROOT / "website")


def _resolve_command(cmd: list[str]) -> list[str]:
    """Resolve Node's platform launcher before passing argv to subprocess.

    npm installs ``npm.cmd`` / ``npx.cmd`` on Windows.  ``subprocess.run`` with
    ``shell=False`` does not apply the shell's PATHEXT lookup, so a bare
    ``"npx"`` raises ``FileNotFoundError`` even though the same command works
    at an interactive prompt.  ``shutil.which`` performs the portable lookup
    and still preserves list argv / no-shell execution.
    """
    if not cmd or cmd[0] not in _NODE_LAUNCHERS:
        return cmd
    launcher = shutil.which(cmd[0])
    if launcher is None:
        raise FileNotFoundError(f"required launcher {cmd[0]!r} was not found on PATH")
    return [launcher, *cmd[1:]]


def _add_related(plan: Plan, surface: str, paths: list[str]) -> None:
    """Append ``surface``'s related set to the plan, or a note that it is empty.

    Raises ``SelectionUntrustworthy`` (via ``related_targets`` /
    ``validated_targets``) when a selected target could act as an option or has
    vanished. One bad target condemns the whole selection: dropping it and
    running the rest would be a selection the gate cannot explain.
    """
    targets, verdict = related_targets(surface, paths)
    if not targets:
        plan.notes.append(f"{surface}: {verdict}")
        return
    if surface == "backend":
        plan.notes.append(f"{surface}: {verdict}")
        _add_batches(plan, "backend (related)", argv_batches(backend_argv, targets), _REPO_ROOT)
        return
    try:
        # `frontend_argv` resolves npx to an absolute launcher itself, so
        # `_resolve_command` sees a non-launcher argv[0] and leaves it alone.
        batches = argv_batches(frontend_argv, targets)
    except LauncherMissing as exc:
        # No Node on this box. That is an environment gap, not a reason to
        # discard the backend set already in the plan: say so, run what can run,
        # and leave these specs to CI, which runs them regardless.
        plan.notes.append(f"{surface}: {verdict} -- NOT RUN HERE: {exc}; CI runs them")
        plan.skipped.append(surface)
        return
    plan.notes.append(f"{surface}: {verdict}")
    _add_batches(plan, "frontend (related)", batches, _REPO_ROOT / "website")


def _add_batches(plan: Plan, label: str, batches: list[list[str]], cwd: Path) -> None:
    """One plan command per argv batch; a single batch keeps the bare label.

    `argv_batches` keeps every runner invocation under `ARGV_CHAR_LIMIT` (the
    Windows command-line limit with margin), so a large related set becomes
    several sequential runs rather than a CreateProcess failure before the first
    test.
    """
    if len(batches) == 1:
        plan.add(label, batches[0], cwd)
        return
    for index, argv in enumerate(batches, start=1):
        plan.add(f"{label} {index}/{len(batches)}", argv, cwd)


def build_plan(args: argparse.Namespace) -> Plan:
    """Decide what to run. Every path out of here is related-only or ``--full``."""
    if args.full:
        plan = Plan("--full requested explicitly -- both surfaces' full suites")
        _backend_full(plan)
        _frontend_full(plan)
        return plan

    paths = changed_files(args.base)
    if paths is None:
        raise GateCannotSee(
            "could not read the diff against the base -- running nothing (exit 2). "
            "Fetch the base (`git fetch origin`) or pass --base; the full suite is "
            "not a substitute for a diff the gate cannot see."
        )
    if not paths:
        return Plan("no changes vs merge-base -- nothing related to run (the full suite runs in CI)")

    frontend, meta, backend = classify(paths)
    if not (frontend or meta or backend):
        return Plan(
            "only ignored evidence paths changed -- nothing related to run "
            "(the full suite runs in CI)"
        )

    plan = Plan(
        "related tests only, full suite deferred to CI"
        f" (frontend={frontend} meta={meta} backend={backend})"
    )
    try:
        _add_related(plan, "backend", paths)
        _add_related(plan, "frontend", paths)
    except SelectionUntrustworthy as exc:
        raise GateCannotSee(f"cannot select related tests -- running nothing (exit 2): {exc}") from exc
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main",
                        help="base ref for the diff (default: origin/main)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan without running anything")
    parser.add_argument("--full", action="store_true",
                        help="run both surfaces' FULL suites (for a human who asks; "
                             "nothing else selects this)")
    args = parser.parse_args(argv)

    try:
        plan = build_plan(args)
    except GateCannotSee as exc:
        print(f"local-gate: {exc}", file=sys.stderr)
        return 2

    print(f"local-gate: {plan.reason}", file=sys.stderr)
    for note in plan.notes:
        print(f"  {note}", file=sys.stderr)
    for label, cmd, cwd in plan.commands:
        print(f"  [{label}] (cwd={cwd.relative_to(_REPO_ROOT) if cwd != _REPO_ROOT else '.'}) "
              f"{shlex.join(cmd[:8])}{' ...' if len(cmd) > 8 else ''}", file=sys.stderr)
    if not plan.commands:
        print("local-gate: nothing to run locally", file=sys.stderr)
        return 0
    if args.dry_run:
        return 0

    for label, cmd, cwd in plan.commands:
        print(f"local-gate: running [{label}]", file=sys.stderr)
        try:
            # The env carries the gate's worker cap through xdist_budget's own
            # knob (harmless to npm/vitest); see run_scoped_tests.pytest_worker_env.
            proc = subprocess.run(_resolve_command(cmd), cwd=cwd, env=pytest_worker_env())
        except OSError as exc:
            print(f"local-gate: [{label}] FAILED to start: {exc}", file=sys.stderr)
            return 127
        if proc.returncode != 0:
            print(f"local-gate: [{label}] FAILED (rc={proc.returncode})", file=sys.stderr)
            return proc.returncode
    if plan.skipped:
        print(
            "local-gate: every runnable gate passed; "
            f"{', '.join(plan.skipped)} related set NOT run here (no runner on this box) "
            "-- CI runs it (full suite deferred to CI)",
            file=sys.stderr,
        )
        return 0
    print("local-gate: all selected gates passed (full suite deferred to CI)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
