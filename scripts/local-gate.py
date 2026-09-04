#!/usr/bin/env python3
"""Change-scoped LOCAL test gate: run only the surfaces a diff can affect.

Why this exists
---------------
The iteration loop on this repo (review-fix rounds, babysit amends) runs the
full backend suite for every change -- roughly an hour at 16 workers -- even
when the diff touches a single frontend file. CI already narrows scope safely
via ``ci-surface-tests.py`` plus a three-bucket diff classification; this
script brings the SAME contract to the local gate, so an iteration costs what
the diff costs, not what the repo costs.

Contract (identical to CI's, fail-open everywhere)
--------------------------------------------------
Every changed file lands in exactly ONE bucket -- or, for ignored evidence
media, none -- mirroring ci.yml's ``changes`` job:

- ``frontend``: ``website/**``
- ``meta``:     ``.github/**``, ``scripts/**``
- ``backend``:  everything else except the ignored prefixes (a CATCH-ALL -- an
  unrecognised path counts as backend, so a new file can never silently ride
  along under a narrowed run)
- ignored:      ``temp-screenshots/**`` (evidence media; NO bucket, so an
  ignored-only diff runs the full gate)

Narrowing happens only when exactly one of frontend/backend changed and meta
did not:

- frontend-only diff: full frontend suite + the backend files
  ``ci-surface-tests.py --surface backend`` could NOT prove single-surface
- backend-only diff:  full backend suite + the frontend specs
  ``ci-surface-tests.py --surface frontend`` could NOT prove single-surface
- anything else (meta touched, both surfaces touched, empty/unreadable diff,
  selector error): the FULL gate on both surfaces

A heuristic miss therefore costs local time, never a skipped test. The final
pre-push gate and CI always run full regardless of this script -- this is an
iteration-loop tool, not a replacement for either.

Usage
-----
    scripts/local-gate.py                  # diff against merge-base with origin/main
    scripts/local-gate.py --base REF       # explicit base ref
    scripts/local-gate.py --dry-run        # print the plan, run nothing
    scripts/local-gate.py --full           # force the full gate (both surfaces)

Exit status is the gate's: 0 when every selected command passed, non-zero
otherwise. ``--dry-run`` exits 0 after printing the plan.
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SELECTOR = _REPO_ROOT / "scripts" / "ci-surface-tests.py"

# The selector's stdout becomes argv for pytest and vitest, so it is validated
# with the SAME helper `run_scoped_tests.py` uses rather than a second copy of
# the rule -- two spellings of one admission check drift, and this one would
# drift silently because nothing here would fail when it did.
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
from run_scoped_tests import (  # noqa: E402  (path set immediately above)
    SelectionUntrustworthy,
    validated_targets,
)

# Bucket rules -- MUST mirror ci.yml's `changes` job filters. test_local_gate.py
# pins this against the workflow file so drift fails a test instead of shipping.
_FRONTEND_PREFIXES = ("website/",)
_META_PREFIXES = (".github/", "scripts/")
# Evidence media ci.yml excludes from EVERY bucket (#8027): temp-screenshots/**
# is never packaged or imported, so it must not drag a frontend-only diff into
# the backend matrix. A path here sets NO flag; a diff that is ONLY ignored
# paths classifies all-False and build_plan() runs the full gate (fail-open),
# matching CI's full matrix for a screenshots-only PR.
_IGNORED_PREFIXES = ("temp-screenshots/",)
_NODE_LAUNCHERS = frozenset({"npm", "npx"})


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
    which the caller maps to the full gate (fail-open).

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
            ["git", "status", "--porcelain", "--no-renames"],
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


def selector_must_run(surface: str) -> list[str] | None:
    """Cross-surface must-run files for ``surface``; None means "fall back to full"."""
    try:
        proc = subprocess.run(
            [sys.executable, str(_SELECTOR), "--surface", surface],
            cwd=_REPO_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


class Plan:
    """The commands the gate will run, plus the reason it chose them."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.commands: list[tuple[str, list[str], Path]] = []

    def add(self, label: str, argv: list[str], cwd: Path) -> None:
        self.commands.append((label, argv, cwd))


def _backend_full(plan: Plan) -> None:
    plan.add(
        "backend (full)",
        [sys.executable, "-m", "pytest", "-q", "-n", "auto", "--dist", "loadgroup"],
        _REPO_ROOT,
    )


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


def build_plan(args: argparse.Namespace) -> Plan:
    if args.full:
        plan = Plan("--full requested")
        _backend_full(plan)
        _frontend_full(plan)
        return plan

    paths = changed_files(args.base)
    if paths is None:
        plan = Plan("could not read the diff -- full gate (fail-open)")
        _backend_full(plan)
        _frontend_full(plan)
        return plan
    if not paths:
        plan = Plan("no changes vs merge-base -- full gate (fail-open)")
        _backend_full(plan)
        _frontend_full(plan)
        return plan

    frontend, meta, backend = classify(paths)

    if not (frontend or meta or backend):
        plan = Plan("only ignored evidence paths changed -- full gate (fail-open)")
        _backend_full(plan)
        _frontend_full(plan)
        return plan

    if meta or (frontend and backend):
        plan = Plan(
            "meta or both surfaces touched -- full gate"
            f" (frontend={frontend} meta={meta} backend={backend})"
        )
        _backend_full(plan)
        _frontend_full(plan)
        return plan

    if frontend and not backend:
        must_run = selector_must_run("backend")
        if must_run is None:
            plan = Plan("selector unusable -- full gate (fail-open)")
            _backend_full(plan)
            _frontend_full(plan)
            return plan
        try:
            must_run = validated_targets(must_run, _REPO_ROOT)
        except SelectionUntrustworthy as exc:
            plan = Plan(f"selector target refused -- full gate (fail-open): {exc}")
            _backend_full(plan)
            _frontend_full(plan)
            return plan
        plan = Plan(f"frontend-only diff -- full frontend + {len(must_run)} backend guard file(s)")
        _frontend_full(plan)
        if must_run:
            plan.add(
                "backend (cross-surface guards)",
                # `--` ends option parsing, matching `run_scoped_tests.backend_argv`.
                # Belt and braces: `validated_targets` already refuses a leading `-`.
                [sys.executable, "-m", "pytest", "-q", "-n", "auto", "--dist", "loadgroup",
                 "--", *must_run],
                _REPO_ROOT,
            )
        return plan

    # backend-only diff
    must_run = selector_must_run("frontend")
    if must_run is None:
        plan = Plan("selector unusable -- full gate (fail-open)")
        _backend_full(plan)
        _frontend_full(plan)
        return plan
    # The frontend-surface selector also lists website/electron guards; those
    # belong to the electron job and vitest cannot collect them -- filter, as
    # CI's frontend-test scope step does.
    vitest_targets = [p for p in must_run if not p.startswith("website/electron/")]
    vitest_rel = [p.removeprefix("website/") for p in vitest_targets]
    try:
        vitest_rel = validated_targets(vitest_rel, _REPO_ROOT / "website")
    except SelectionUntrustworthy as exc:
        plan = Plan(f"selector target refused -- full gate (fail-open): {exc}")
        _backend_full(plan)
        _frontend_full(plan)
        return plan
    plan = Plan(f"backend-only diff -- full backend + {len(vitest_rel)} frontend guard spec(s)")
    _backend_full(plan)
    if vitest_rel:
        plan.add(
            "frontend (cross-surface guards)",
            # Deliberately NO `--` here: `vitest run -- <paths>` stops treating the
            # positionals as filters and runs the whole suite, which would report a
            # narrow scope while running everything. `run_scoped_tests.frontend_argv`
            # carries the measurement; `validated_targets` is the real protection.
            ["npx", "vitest", "run", *vitest_rel],
            _REPO_ROOT / "website",
        )
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main",
                        help="base ref for the diff (default: origin/main)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan without running anything")
    parser.add_argument("--full", action="store_true",
                        help="force the full gate on both surfaces")
    args = parser.parse_args(argv)

    plan = build_plan(args)
    print(f"local-gate: {plan.reason}", file=sys.stderr)
    for label, cmd, cwd in plan.commands:
        print(f"  [{label}] (cwd={cwd.relative_to(_REPO_ROOT) if cwd != _REPO_ROOT else '.'}) "
              f"{shlex.join(cmd[:8])}{' ...' if len(cmd) > 8 else ''}", file=sys.stderr)
    if args.dry_run:
        return 0

    for label, cmd, cwd in plan.commands:
        print(f"local-gate: running [{label}]", file=sys.stderr)
        try:
            proc = subprocess.run(_resolve_command(cmd), cwd=cwd)
        except OSError as exc:
            print(f"local-gate: [{label}] FAILED to start: {exc}", file=sys.stderr)
            return 127
        if proc.returncode != 0:
            print(f"local-gate: [{label}] FAILED (rc={proc.returncode})", file=sys.stderr)
            return proc.returncode
    print("local-gate: all selected gates passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
