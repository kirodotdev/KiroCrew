#!/usr/bin/env python3
"""Change-scoped LOCAL test gate: run the tests a diff is related to, on both surfaces.

Why this exists
---------------
The iteration loop on this repo (review-fix rounds, babysit amends) used to run
the full backend suite -- far too large to run per change, and with ``-n auto`` one
xdist worker per core -- for every change whose diff touched more than one
surface or any ``scripts/`` file. On a shared 48-core box that is 32 workers
owning the machine while every other session crawls, to produce a signal CI
produces anyway on ``refs/pull/<N>/merge``.

This gate never does that on its own. It runs the RELATED set on each surface,
computed by ``scripts/run_scoped_tests.py`` (the same module the ``gates[]`` in
the kirocrew-prepare-pr profile call one surface at a time), with a bounded worker count.
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
- A command that already passed on this exact tree is not run twice. The tree is the
  working tree as git sees it (HEAD plus staged, unstaged and untracked edits; ignored
  files are outside it), and a proof is keyed by that tree, the argv, the cwd, a digest
  of the whole command environment and the tools behind the argv: this interpreter and
  every package installed in it, and for npm and npx also the node they resolve to and
  the lockfile npm wrote for ``node_modules``. Only a pass is recorded, only when the
  tree did not change while the command ran, and a proof is ignored once it is
  unreadable, mismatched, over an hour old or dated in the future. A command whose tools
  cannot be named is always run. Each command it skips is reported as ``reused``.
  After changing an ignored input that no lockfile names (a built dist, a hand edit under
  node_modules) pass ``--no-reuse``. Proofs are files under ``<git-dir>/local-gate-proofs/``.

Usage
-----
    scripts/local-gate.py                  # diff against merge-base with origin/main
    scripts/local-gate.py --base REF       # explicit base ref
    scripts/local-gate.py --dry-run        # print the plan, run nothing
    scripts/local-gate.py --full           # both surfaces' FULL suites (human-requested)
    scripts/local-gate.py --no-reuse       # run every command, even one that passed on this tree

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
import contextlib
import hashlib
import importlib.metadata
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# One selection module for the whole local gate. The per-surface `gates[]`
# entries in the kirocrew-prepare-pr profile call `run_scoped_tests.py --surface X`
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
# How long a passing proof stays reusable. Files .gitignore hides that no package list or
# lockfile names (a built dist, a hand edit under node_modules) are outside the identity,
# so the bound caps how stale one of those can be behind a reused pass.
_PROOF_MAX_AGE = 3600.0


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


def gate_env(base: str) -> dict[str, str]:
    """The environment every gate command runs under.

    The worker cap from :func:`pytest_worker_env`, plus ``I18N_BASE_REF`` set to
    the merge-base with ``base`` when the caller did not set one. The website's
    ``[changed-values]`` style tests and the other diff-scoped i18n gates skip
    themselves green without that variable, so a local run passed a catalog
    value CI then failed (bn আপনি, fr plain space before ``:``). An explicit
    value is kept as given. An unresolvable merge-base leaves it unset, so a
    gate that needs it reports that it skipped.
    """
    env = pytest_worker_env()
    if env.get("I18N_BASE_REF"):
        return env
    try:
        proc = subprocess.run(
            ["git", "merge-base", "HEAD", base],
            cwd=_REPO_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return env
    sha = (proc.stdout or "").strip()
    if proc.returncode == 0 and sha:
        env["I18N_BASE_REF"] = sha
    return env


def _output(argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> str | None:
    """Stripped stdout of ``argv`` run in ``cwd``, or None when it cannot answer."""
    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (proc.stdout or "").strip() if proc.returncode == 0 else None


def _git(*args: str, env: dict[str, str] | None = None) -> str | None:
    """Stripped stdout of ``git <args>`` run in the checkout, or None when git cannot answer."""
    return _output(["git", *args], _REPO_ROOT, env)


def tree_id() -> str | None:
    """The working tree as one git tree id: HEAD plus every staged, unstaged and untracked edit.

    Built in a scratch copy of the index, so the checkout's own index is left alone and
    only edited files are hashed again. The copy keeps the index's mtime, which git's
    check for entries written in the same clock tick as the index depends on. Files
    .gitignore hides are not part of it. None when git cannot say, and then nothing is
    reused.
    """
    index = _git("rev-parse", "--git-path", "index")
    if not index:
        return None
    with tempfile.TemporaryDirectory(prefix="local-gate-") as scratch:
        scratch_index = os.path.join(scratch, "index")
        try:
            shutil.copy2(_REPO_ROOT / index, scratch_index)
        except OSError:
            return None
        env = {**os.environ, "GIT_INDEX_FILE": scratch_index}
        if _git("add", "-A", env=env) is None:
            return None
        return _git("write-tree", env=env) or None


def _digest(value: object) -> str:
    """sha256 of ``value`` as canonical JSON, so key order never changes it."""
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def tool_versions(cmd: list[str], cwd: Path, env: dict[str, str]) -> dict[str, str] | None:
    """What will run ``cmd``, named: a path alone does not say which version sits behind it.

    This interpreter and every package installed in it (pytest and its plugins) always.
    For npm and npx also the launcher, the node that ``env`` and ``cwd`` resolve to (a version
    manager's shim picks one per directory) and the lockfile npm wrote for ``node_modules``
    (vitest and the rest). None when any of it cannot be read, and then nothing is reused.
    """
    try:
        installed = [
            [str(d.metadata.get("Name")), str(d.metadata.get("Version"))]
            for d in importlib.metadata.distributions()
        ]
        tools = {"python": sys.version, "packages": _digest(sorted(installed))}
        if Path(cmd[0]).stem.lower() in _NODE_LAUNCHERS:
            node = shutil.which("node", path=env.get("PATH"))
            tools["launcher"] = _output([cmd[0], "--version"], cwd, env) or ""
            tools["node"] = (node and _output([node, "--version"], cwd, env)) or ""
            tools["node_modules"] = hashlib.sha256(
                (cwd / "node_modules" / ".package-lock.json").read_bytes()
            ).hexdigest()
    except Exception:  # whatever stops the tools being named, the answer is: run the command
        return None
    return None if "" in tools.values() else tools


class Proofs:
    """Gate commands that passed, kept per exact tree so an unchanged tree is not retested.

    One file per (argv, cwd) holds the identity that passed and when; a newer pass
    overwrites it. A file that is unreadable, names another identity, is older than
    ``_PROOF_MAX_AGE`` or is dated in the future is no proof, and a failure is never one.
    The identity is the tree, the argv, the cwd, a digest of the whole command environment
    and :func:`tool_versions`; when the tools cannot be named the command runs and nothing
    is kept. The tools are read once per command, before it first runs.
    """

    def __init__(
        self, root: Path, tree: str, env: dict[str, str], clock: Callable[[], float] = time.time
    ) -> None:
        self.root, self.tree, self.env, self.clock = root, tree, env, clock
        self._tools: dict[tuple[str, str], dict[str, str] | None] = {}

    def _path(self, cmd: list[str], cwd: Path) -> Path:
        return self.root / f"{_digest([cmd, str(cwd)])}.json"

    def _identity(self, cmd: list[str], cwd: Path) -> dict[str, object] | None:
        key = (cmd[0], str(cwd))
        if key not in self._tools:
            self._tools[key] = tool_versions(cmd, cwd, self.env)
        tools = self._tools[key]
        if tools is None:
            return None
        return {
            "tree": self.tree, "argv": cmd, "cwd": str(cwd), "env": _digest(self.env),
            "tools": tools,
        }

    def age(self, cmd: list[str], cwd: Path) -> float | None:
        """Seconds since ``cmd`` passed on this exact tree, or None when it has to run."""
        identity = self._identity(cmd, cwd)
        if identity is None:
            return None
        try:
            proof = json.loads(self._path(cmd, cwd).read_text(encoding="utf-8"))
            age = self.clock() - proof["at"]
            same = proof["identity"] == identity
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return age if same and 0 <= age <= _PROOF_MAX_AGE else None

    def record(self, cmd: list[str], cwd: Path) -> None:
        """Keep a pass, but only when the tree is still the one the command started on."""
        identity = self._identity(cmd, cwd)
        if identity is None or tree_id() != self.tree:
            return
        path = self._path(cmd, cwd)
        staged = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            staged.write_text(json.dumps({"identity": identity, "at": self.clock()}), encoding="utf-8")
            os.replace(staged, path)
        except OSError:
            with contextlib.suppress(OSError):
                staged.unlink(missing_ok=True)

    def forget(self, cmd: list[str], cwd: Path) -> None:
        """Drop the pass: a failing run contradicts it."""
        with contextlib.suppress(OSError):
            self._path(cmd, cwd).unlink(missing_ok=True)


def open_proofs(env: dict[str, str]) -> Proofs | None:
    """This checkout's proofs for its current tree, or None when it cannot be identified."""
    git_dir = _git("rev-parse", "--absolute-git-dir")
    tree = tree_id()
    if not git_dir or not tree:
        return None
    return Proofs(Path(git_dir) / "local-gate-proofs", tree, env)


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
        return Plan(
            "no changes vs merge-base -- nothing related to run (full suite deferred to CI)"
        )

    frontend, meta, backend = classify(paths)
    if not (frontend or meta or backend):
        return Plan(
            "only ignored evidence paths changed -- nothing related to run "
            "(full suite deferred to CI)"
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
    parser.add_argument("--no-reuse", action="store_true",
                        help="run every selected command even when it already passed on "
                             "this exact tree")
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

    # The env carries the gate's worker cap through xdist_budget's own knob
    # (harmless to npm/vitest) and the i18n base ref; see gate_env.
    env = gate_env(args.base)
    proofs = open_proofs(env)
    for label, cmd, cwd in plan.commands:
        age = None if proofs is None else proofs.age(cmd, cwd)
        if age is not None and not args.no_reuse:
            print(f"local-gate: [{label}] reused, not run: it passed on this exact tree "
                  f"{age:.0f}s ago (--no-reuse runs it again)", file=sys.stderr)
            continue
        print(f"local-gate: running [{label}]", file=sys.stderr)
        try:
            proc = subprocess.run(_resolve_command(cmd), cwd=cwd, env=env)
        except OSError as exc:
            print(f"local-gate: [{label}] FAILED to start: {exc}", file=sys.stderr)
            return 127
        if proc.returncode != 0:
            print(f"local-gate: [{label}] FAILED (rc={proc.returncode})", file=sys.stderr)
            if proofs is not None:
                proofs.forget(cmd, cwd)
            return proc.returncode
        if proofs is not None:
            proofs.record(cmd, cwd)
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
