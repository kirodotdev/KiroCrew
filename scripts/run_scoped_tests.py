#!/usr/bin/env python3
"""Skip the test suite a change cannot affect. Never narrow the one it can.

What this buys
--------------
The ``prepare-pr`` loop runs its gate up to ten times per PR, and the full local
suites are enormous: 62,108 collected backend tests (collection alone takes ~100s
before a single test runs) and ~1,444 frontend spec files. Most PRs touch one
surface. This replaces the OTHER surface's full suite with the cross-surface set
``ci.yml`` runs for exactly that case -- measured on this checkout, 350 backend
files instead of the whole backend suite, or 146 frontend specs instead of all
1,444.

What this deliberately does NOT do
----------------------------------
It does not narrow WITHIN the surface a change touches. An earlier revision tried,
by scanning tests for textual references to the changed module, and six review
rounds produced nine real findings -- absolute import, relative import, barrel
re-export, in-package fixture, global vitest setup, data-file read, cross-surface
parity comparison, documentation contract. They are not a defect list; they are
one impossibility: a text scan cannot enumerate the ways a test can reach a
module, and each fix shrank the allowlist further toward "escalate everything",
at which point the gate is the full suite again. Doing it soundly needs a real
import graph (Python AST plus a TS resolver that follows barrel re-exports),
tracked separately.

So the rule here is deliberately coarse and checkable:

* the diff touches this surface        -> that surface's FULL suite
* the diff touches only the OTHER one  -> this surface's cross-surface set
* a broad-impact file changed          -> full suite
* base ref missing or unresolvable     -> exit 2, run nothing

Every reduction is CI's own reduction, computed by CI's own script, so there is
no second answer invented here to be wrong.

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
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

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


class SelectionUntrustworthy(Exception):
    """Raised when a reduction cannot be justified; the caller runs everything."""


def _run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


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


def cross_surface_targets(surface: str) -> list[str]:
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
    if not out:
        raise SelectionUntrustworthy("cross-surface selector resolved no in-scope files")
    return out


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
    argv = [sys.executable, "-m", "pytest", "-q"]
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
        raise SelectionUntrustworthy(
            f"{name} is not on PATH, so the frontend suite cannot be launched"
        )
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


def plan(surface: str, base: str) -> tuple[list[str] | None, str]:
    """Return (targets, reason). ``None`` targets means run the full suite."""
    base_sha = resolve_base(base)
    try:
        paths = changed_files(base_sha)
    except SelectionUntrustworthy as exc:
        return None, f"full suite: {exc}"

    if not paths:
        return None, "full suite: diff is empty against the base"

    broad = has_broad_impact(paths)
    if broad:
        return None, f"full suite: broad-impact change {broad}"

    buckets = {surface_bucket(p) for p in paths}
    other = "frontend" if surface == "backend" else "backend"

    # ci.yml disables BOTH reductions whenever the meta bucket is touched, because
    # a `.github/` or `scripts/` path belongs to neither surface and can be read
    # by tests on both. Same veto here, for the same reason.
    if "meta" in buckets:
        return None, (
            "full suite: the diff touches CI meta paths (.github/ or scripts/), "
            "which ci.yml also treats as disabling both reductions"
        )
    if surface in buckets:
        return None, (
            "full suite: the diff touches this surface, and narrowing within a "
            "surface needs an import graph rather than a text scan"
        )
    if other not in buckets:
        return None, "full suite: the diff touches neither surface"

    try:
        cross = cross_surface_targets(surface)
    except SelectionUntrustworthy as exc:
        return None, f"full suite: {exc}"
    return cross, (
        f"cross-surface: {len(cross)} file(s) -- the diff touches only the other "
        "surface, so this is the set ci.yml runs for a single-surface diff"
    )


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

    try:
        targets, reason = plan(args.surface, os.environ.get("SCOPED_TESTS_BASE_REF", ""))
    except ValueError as exc:
        print(f"run_scoped_tests: {exc}", file=sys.stderr)
        return 2

    build = backend_argv if args.surface == "backend" else frontend_argv
    try:
        cmd = build(targets)
    except SelectionUntrustworthy as exc:
        # A target that could pass for an option is a doubt like any other.
        reason = f"full suite: {exc}"
        targets = None
        try:
            cmd = build(None)
        except SelectionUntrustworthy as fatal:
            # Nothing is runnable at all (e.g. npm absent). Say so rather than
            # dying with a FileNotFoundError from deep inside subprocess.
            print(f"run_scoped_tests: {fatal}", file=sys.stderr)
            return 2

    print(f"run_scoped_tests[{args.surface}]: {reason}")
    for target in (targets or [])[:20]:
        print(f"  - {target}")
    if targets and len(targets) > 20:
        print(f"  ... +{len(targets) - 20} more")
    print(f"run_scoped_tests[{args.surface}]: $ {' '.join(cmd)}")
    if args.dry_run:
        return 0

    cwd = REPO_ROOT / "website" if (args.surface == "frontend" and targets) else REPO_ROOT
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
    return subprocess.run(cmd, cwd=str(cwd), check=False).returncode  # noqa: E501  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args


def _self_test() -> int:
    """Prove the escalations fire. A reducer trusted without these is a guess."""
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
        frontend_argv(xs_fe)
    except SelectionUntrustworthy as exc:
        failures.append(f"cross-surface frontend list unusable: {exc}")
    try:
        xs_be = cross_surface_targets("backend")
        check("cross-surface backend list is non-empty", len(xs_be) > 0)
        backend_argv(xs_be)
    except SelectionUntrustworthy as exc:
        failures.append(f"cross-surface backend list unusable: {exc}")

    # Full-suite argv must stay CI's exact command, so a fallback is not a
    # different, weaker check than the gate it replaces. The launcher is compared
    # by BASENAME because it is resolved to an absolute path -- `subprocess.run`
    # with shell=False cannot execute a bare `npm` on native Windows, where what
    # exists on PATH is `npm.cmd`.
    check(
        "backend full argv",
        backend_argv(None) == [sys.executable, "-m", "pytest", "-q"],
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

    if failures:
        for name in failures:
            print(f"FAIL {name}", file=sys.stderr)
        print(f"run_scoped_tests self-test: {len(failures)} failure(s)", file=sys.stderr)
        return 1
    print("run_scoped_tests self-test: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
