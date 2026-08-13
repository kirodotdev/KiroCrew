"""The reference Target Profile: any Python GitHub repo with a pytest suite.

This is the concrete profile the ported spine was missing. It assembles the six
seam fields of :mod:`...spine.profile` for a target that is *just a git repo*:

  ① :class:`SuiteRuler`       wall-clock seconds of the repo's own test suite
  ② :class:`PytestBuildGate`  ``pytest -q`` — boolean + the gated commit sha
  ②b :class:`PytestBugRunner` the RED/GREEN primitives (import smoke, lint, collect,
                              one nodeid, full suite)
  ③ :class:`RepoEditAllowlist` source globs in, tests-of-record + CI config out
  ④ :class:`RepoIsolation`    the push-disabled clone + do-not-pollute paths
  ⑤ ``GitHubPRRecipe``        reused verbatim from :mod:`.pr_recipe`
  ⑥ ``CalibrationParams``     reps + a seconds noise floor + the canary id

## Why a repo target is the *easy* case for four of the six fields

A repo has no runtime to boot, no container split, no bind mounts, and no frozen
component tree: a fresh ``pytest`` process IS the measurement. So ``single_environment``
is True (build and measure are co-located — the spine skips its cross-environment
sha assertion), ``frozen_components`` is empty, and ``measurement_boot()`` is a
documented no-op. Those are honest answers to the protocol, not stubs: see each
member's docstring for why the degenerate answer is the *correct* one here.

## Where the spine hands us ``<tree>/src`` unconditionally

Three spine call-sites build a source path by appending ``src`` with no existence
check — ``Measurer(base_src=clone/"src")``, ``calibrate_and_prove(base_src=
clone/"src")``, and ``Gate`` passing ``worktree/"src"``. A flat repo (``foo.py`` at
the root, no ``src/`` package dir) therefore arrives here as a path that does not
exist. Every adapter in this module funnels its incoming tree through
:func:`_repo_root`, which walks up to the real repo root. The driver itself already
uses that same fallback idiom (``src = clone/"src"; if not src.exists(): src =
clone``), so this is the established convention rather than a workaround.

## The canary is a LOWER BOUND here (documented limitation)

The Phase-1 canary must be a *known* win that clears the calibrated noise band.
For a purpose-built runtime you force one by flipping a disabled fast path. An
arbitrary Python repo offers no such switch: we cannot know a priori which edit
makes ITS suite measurably faster, and fabricating one (patching in a
``time.sleep`` to remove) would prove only that the ruler can see a sleep — not
that it can resolve the small deltas real candidates produce.

So :meth:`SuiteRuler.measure_canary` forces the one win that is genuinely known
and genuinely mechanical: it runs the suite with collect-only on the candidate arm,
so the candidate arm skips all test *execution* while both arms pay the same
interpreter start + collection cost. That is a real, correctly-signed delta measured
through the full ruler — but it is a *lower bound* on sensitivity, not proof that the
ruler resolves a 3% win.

A canary that does not clear even that lower bound therefore means the ruler is not
trusted on this target, and the perf run HALTS (``canary_advisory`` defaults to False —
the §7.1 contract). It ran advisory-by-default for a while on the argument that strictness
would also halt bug-track runs; it does not, because ``Driver.run`` skips ruler pre-flight
entirely for the bug track. An operator whose target genuinely cannot force a measurable
win can still opt in to warn-and-continue explicitly.

Two consequences worth stating plainly. First, the canary is sampled over
``_CANARY_REPS`` pairs and aggregated by median, because a single pair lets one
scheduling hiccup pick the sign. Second, on a repo whose suite runs in about the time
its own collection takes, skipping every test saves nothing measurable — there is no
win to force, so the canary returns ``ok=False`` with that stated in the note. It does
NOT return a delta whose sign the noise chose. Such a repo is simply not a target a
wall-clock suite ruler can prove a perf win on; configuring ``benchmarkCommand`` to
point at a real workload is the fix. Such a repo can still be used for the BUG track,
which never calibrates or consults the ruler at all.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

from kiro_crew import platform_compat
from kiro_crew.sandbox import run_limited, sandboxed_spawn_argv

from ...spine import agent_discovery
from ...spine import scope as scope_util
from ...spine.contracts import (
    TRACK_BUG,
    TRACK_PERF,
    BugReproducingTest,
    Candidate,
    DiscoveryResult,
    GateResult,
    Measurement,
    StageBreakdown,
)
from ...spine.profile import CalibrationParams, ProfileFieldAliases
from .pr_recipe import GitHubPRRecipe

logger = logging.getLogger(__name__)

#: The metric's unit. Wall-clock seconds of the repo's own suite — the one number
#: every Python repo can produce without a profile-specific harness.
UNIT_SECONDS = "s"

#: Attributable stages. A repo run decomposes into interpreter start + collection
#: (paid identically by both arms) and test execution (where a real win lands).
#: Keyed on STABLE MARKERS, never ``file:line`` — candidate diffs shift line numbers.
STAGE_SUITE = "suite_wall"
STAGE_COLLECT = "collect_wall"

#: The single guardrail: the suite must keep passing. Reported as a
#: positive-means-regression MAGNITUDE (0.0 = still green, 1.0 = broke) because the
#: keeper rejects ``value > tolerance`` and the default tolerance is 0.
GUARDRAIL_TESTS_PASS = "tests_still_pass"

#: The RH (reward-hacking) guard: a candidate must not delete tests to go faster.
#: Deleting tests is the single highest-value cheat available against a wall-clock
#: suite ruler, and the build gate cannot see it (a suite with fewer tests still
#: passes). Enforced as a collected-test COUNT comparison, base vs candidate.
RH_TEST_COUNT = "test_count_unchanged"

#: Bounded so one pathological repo cannot wedge a run. A suite slower than this is
#: not a viable measurement target anyway (a 15-minute A/B rep × 6 reps × 2 arms).
_SUITE_TIMEOUT_S = 900.0
_QUICK_TIMEOUT_S = 180.0

#: Paired reps behind :meth:`SuiteRuler.measure_canary`, aggregated by median. Three
#: is the smallest count with a true middle value — enough that one scheduling hiccup
#: cannot decide the canary's sign, cheap enough to run before every loop.
_CANARY_REPS = 3

#: ``pytest -q`` failure-line shape: ``FAILED path::case - reason`` / ``ERROR path``.
_NODEID_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)")

#: ``pytest --collect-only -q`` tail: ``N tests collected in 0.12s``.
_COLLECTED_RE = re.compile(r"(\d+)\s+tests?\s+collected")

#: SGR escape sequences. pytest colorizes its summary — including the middle of a
#: nodeid (``tests/x.py::<esc>test_name<esc>``) — whenever it thinks it has a terminal
#: or the target repo's own ``addopts`` force color. We pass ``--color=no``, but a repo
#: config can override that, so every parsed line is stripped first: a nodeid with an
#: escape sequence embedded in it is not a nodeid the gate can re-run.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

#: Paths a candidate may never touch regardless of what the repo looks like: the
#: tests-of-record (the ruler's own measurement subject — editing them is metric
#: gaming), and the build/CI/dependency config (changing the interpreter or the
#: dependency set invalidates every measurement taken before it).
_ALWAYS_OFF_LIMITS = (
    "test/**",
    "tests/**",
    "test_*.py",
    "*_test.py",
    "**/test_*.py",
    "**/conftest.py",
    "conftest.py",
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
    "tox.ini",
    "pytest.ini",
    "requirements*.txt",
    "uv.lock",
    "poetry.lock",
    "Pipfile*",
    "Makefile",
    ".github/**",
    ".gitlab-ci.yml",
    "Dockerfile*",
)

#: Files the AGENT'S OWN TOOLING drops into the worktree as a side effect of running
#: there — never part of a candidate's diff, and not the agent's doing in any
#: meaningful sense.
#:
#: These must be IGNORED rather than judged. Observed live on the first real run: the
#: agent session wrote ``.kiro/settings/cli.json`` into its worktree, the fence
#: (correctly) saw an unrecognized path and rejected the whole candidate — throwing
#: away a genuine, RED-then-GREEN-verified bug fix over an unrelated settings file.
#: Widening ``allowed`` to admit them would be wrong (they would then be legal EDIT
#: targets); dropping them from consideration is the honest fix, because they are not
#: part of the change under test at all.
_TOOLING_ARTIFACTS = (
    ".kiro/**",
    ".claude/**",
    ".aider*",
    ".DS_Store",
    "**/__pycache__/**",
    "*.pyc",
    ".pytest_cache/**",
    ".ruff_cache/**",
    ".mypy_cache/**",
)

#: The ONE new-test shape the bug track may ADD. ``author_bug_fix`` hard-codes
#: ``test/test_bug_<slug>.py`` in its prompt, and the brief also names
#: ``tests/test_*.py``; both are accepted so the fence matches what the agent is
#: actually told to write. An added test under any OTHER path is refused — that is
#: how "add your own repro" stays distinct from "edit the suite that judges you".
_ADDABLE_TEST_GLOBS = (
    "test/test_bug_*.py",
    "tests/test_bug_*.py",
    "test/test_*.py",
    "tests/test_*.py",
)


# ── shared helpers ───────────────────────────────────────────────────────────


#: Directory names that hold a pytest suite. Used to locate the RUN ROOT, which is
#: not always the directory the spine hands us — see :func:`_repo_root`.
_TEST_DIRS = ("tests", "test")


def _has_tests(tree: Path) -> bool:
    """Whether ``tree`` looks like the directory pytest should be invoked from."""
    if any((tree / name).is_dir() for name in _TEST_DIRS):
        return True
    # A repo with tests beside the code and no tests/ dir at all (``foo_test.py`` next
    # to ``foo.py``) still roots pytest here.
    return any(tree.glob("test_*.py")) or any(tree.glob("*_test.py"))


def _repo_root(tree: Path) -> Path:
    """Resolve a spine-supplied source path to the tree pytest must actually run in.

    The spine appends ``src`` to a clone/worktree unconditionally (see the module
    docstring), so ``tree`` arrives as ``<repo>/src`` whether or not that is where the
    suite lives. Two distinct layouts hide behind that one path and the difference is
    not cosmetic:

    * **flat** — no ``src/`` at all, so the path does not exist; the run root is its
      parent. (This is ``driver.py``'s own ``if not src.exists(): src = clone`` idiom.)
    * **src-layout** — ``<repo>/src`` DOES exist but holds only the package, while the
      suite sits at ``<repo>/tests``. Running pytest inside ``src/`` collects nothing
      and exits 5, which every caller here would read as a RED suite. A perfectly green
      repo would be reported broken, the build gate would refuse every candidate, and
      the failure would look like the agent's fault. So when the parent is the side
      holding the tests, the parent is the run root.

    Importability is handled separately by :func:`_measure_env`, which puts BOTH the
    run root and its ``src`` on ``PYTHONPATH`` — the run root is a pytest concern, the
    package location is an import concern, and conflating them is what caused the bug
    above.
    """
    tree = Path(tree)
    if tree.is_dir() and _has_tests(tree):
        return tree
    parent = tree.parent
    if parent.is_dir() and _has_tests(parent):
        return parent
    # Neither side declares a suite: fall back to whichever path exists, preserving the
    # spine's own resolution order.
    if tree.exists():
        return tree
    return parent if parent.exists() else tree


def _write_protected_targets() -> tuple[str, ...]:
    """Directories to bind-mount empty so agent-authored tests cannot write Kiro Crew's config.

    Derived from ``security.write_protected_home_paths()`` rather than hardcoded, so a path
    added to the platform list is covered automatically instead of drifting. Two details that
    were wrong in the first attempt, both found by measuring rather than reasoning:

    * The PARENT DIRECTORY is masked, not the file. ``sandboxed_spawn_argv``'s
      ``extra_hidden_dirs`` reaches the launcher's ``SENSITIVE_DIRS`` loop, which is guarded
      by ``os.path.isdir(target)`` — so a FILE path is silently skipped and the mask
      no-ops. (Files are masked through a separate ``SENSITIVE_FILES`` list the public helper
      does not expose.) Measured: passing the file paths left the child able to append to
      ``~/.kiro/crew/config.json`` and exit 0; passing the parent blocked it.
    * Only EXISTING directories are returned — the launcher mounts over each target, and a
      mount over a missing path is a needless failure on a fresh install.

    Masking the whole ``~/.kiro/crew`` directory is safe *for this caller* specifically: the
    sandboxed child is the TARGET REPOSITORY's pytest, which has no legitimate reason to read
    Kiro Crew's own configuration. It is a superset of the 18 per-file entries ``strict``
    already hides there, and measured not to disturb the interpreter.

    Fail-soft: if the helper is unavailable the sandbox is still applied without the extra
    masking, which beats refusing to run any test at all.
    """
    try:
        from kiro_crew.security import write_protected_home_paths

        home = Path.home()
        parents = {(home / rel).parent for rel in write_protected_home_paths()}
        return tuple(sorted(str(p) for p in parents if p.is_dir()))
    except Exception:  # noqa: BLE001 — masking is defense-in-depth, not the only control
        logger.debug("could not resolve write-protected paths to mask", exc_info=True)
        return ()


def _run(
    argv: list[str], *, cwd: Path, timeout: float, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a bounded, non-shell subprocess. Never raises on a non-zero exit — every
    caller here reads ``returncode`` as data, not as an error.

    ROUTED THROUGH THE SANDBOX CHOKEPOINT, because this is where the harness executes
    code the AGENT just wrote: every gate step runs the target repo's pytest (and its
    conftest, and the reproducing test) inside the candidate worktree. Both the command
    and the working directory are therefore agent-influenced in exactly the sense
    ``test/test_spawn_audit.py`` cares about — the same reasoning that keeps
    ``task_executor.run_tests`` routed. ``sandboxed_spawn_argv`` adds OS-level
    filesystem isolation plus a credential-scrubbed environment, so a malicious or
    simply careless conftest cannot read the operator's credentials.

    The worktree must stay VISIBLE to the sandbox or there would be nothing to test,
    and any env this module composed (``_measure_env``: PYTHONPATH, PYTHONHASHSEED,
    the addopts override) is layered ON TOP of the scrubbed base so measurement
    determinism survives the scrub.
    """
    root = str(Path(cwd).resolve())
    # STRICT, not the default "standard": "standard" deliberately leaves ~/.aws visible so
    # a test suite can use the AWS CLI. But the code running here is agent-authored — a
    # candidate's own conftest/reproducing test — which is precisely who must not see the
    # operator's credentials. Measured on the author's host: under "standard" the child saw
    # all 7 ~/.aws entries; under "strict", 0. The agent SPAWN was already switched to
    # strict; this is the gate, the other place untrusted code executes. Raised by review.
    # Also MASK Kiro Crew's own write-protected files. `mode="strict"` hides 52 credential
    # paths so agent-authored code cannot READ secrets, but it does not make the rest of the
    # filesystem read-only — measured on this host: a strict-mode child appended to
    # `~/.kiro/crew/config.json` and exited 0. Those paths are `security.
    # write_protected_home_paths()`, enforced by the platform HOOK layer, which a sandboxed
    # subprocess never passes through — so the protection was inert for exactly the code that
    # most needs it. Bind-mounting an empty dir over each makes the write fail at the kernel
    # instead. Scoped to Kiro Crew's OWN control files (config.json, config.local.json
    # under both `.kiro/crew` and `.kirocrew`), i.e. the one-way doors that
    # would corrupt the installation; broader hiding is not possible here because the
    # interpreter's own stdlib can live under `$HOME` (measured: hiding `~/.local/share`
    # broke `import platform` outright). Raised by the GPT review of this branch.
    sandboxed, scrubbed_env, cleanup = sandboxed_spawn_argv(
        list(argv),
        mode="strict",
        extra_visible_dirs=(root,),
        extra_hidden_dirs=_write_protected_targets(),
    )
    if env:
        scrubbed_env.update(env)
    # Belt and braces: strip credential-shaped names AFTER layering, so neither the
    # caller's dict nor a gap in the shared scrub list can hand a token to agent-authored
    # test code. See `_CREDENTIAL_ENV_MARKERS`.
    scrubbed_env = strip_credential_env(scrubbed_env)
    try:
        # Kernel RLIMIT ceiling (NPROC/NOFILE/CPU/AS) on top of the sandbox: a
        # runaway conftest or a fork bomb in the agent's own test cannot exhaust
        # the host running the gateway. run_limited delivers the limits after
        # exec via the spawn shim instead of in a fork child of this threaded
        # process.
        return run_limited(
            sandboxed,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            env=scrubbed_env,
        )
    finally:
        # A temp launcher/profile FILE, per sandboxed_spawn_argv's contract — unlink it,
        # matching task_executor.run_tests and source_providers._run_json.
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)


#: Host variables the measurement subprocess genuinely needs. An ALLOWLIST, so adding a
#: name is a visible decision rather than a side effect of inheriting the whole
#: environment. Deliberately excludes every credential-shaped family (AWS_*, GITHUB_*,
#: *_TOKEN, SSH_AUTH_SOCK, …): the sandbox scrubs those, and `_run` layers this dict on
#: top of the scrubbed env, so anything listed here defeats that scrub.
_MEASURE_ENV_PASSTHROUGH = (
    "PATH",  # without it a subprocess cannot find python/pytest at all
    "HOME",  # pytest/pip cache locations; a missing HOME breaks many suites
    "TMPDIR",  # tmp_path fixtures
    "LANG",  # text decoding in test output
    "LC_ALL",
    "LC_CTYPE",
    "TZ",  # date-sensitive assertions must not shift between arms
    "TERM",  # some suites probe it; absent TERM makes output differ between arms
    "SYSTEMROOT",  # Windows: CPython needs it to initialize
    "COMSPEC",  # Windows
    # Memory-aware cap for xdist's ``-n auto`` (_XDIST_ARGV below adds it
    # unconditionally when xdist is importable). Seeded at the agent spawn
    # boundary (see resource_status.inject_xdist_auto_cap); without this
    # passthrough the allowlist would strip it and the suite would size to the
    # CPU count regardless of memory. Same value for both A/B arms, so arm
    # fairness is preserved. Not credential-shaped.
    "PYTEST_XDIST_AUTO_NUM_WORKERS",
)


def _measure_env(tree: Path) -> dict[str, str]:
    """The measurement environment, held byte-identical across both A/B arms.

    ``PYTHONHASHSEED=0`` and ``PYTHONDONTWRITEBYTECODE=1`` are the two conditions
    that otherwise silently differ between arms: a random hash seed changes dict/set
    iteration order (and with it any order-sensitive timing), and bytecode caching
    makes whichever arm runs FIRST pay compilation the other one skips. Pinning both
    is what makes the interleaved A/B a fair comparison rather than a coin flip.
    """
    # ONLY the explicit measurement variables — never a copy of `os.environ`. The caller
    # (`_run`) layers this dict ON TOP of the sandbox's credential-scrubbed environment,
    # so anything inherited here is put straight back after the sandbox removed it.
    # Measured before fixing: an `AWS_SECRET_ACCESS_KEY` scrubbed by the sandbox
    # reappeared in the child's env once this dict was applied — handing the operator's
    # credentials to agent-authored test code. Raised by review of this branch.
    #
    # A few HOST variables are still required for a subprocess to run at all (PATH) or to
    # behave like a normal user session (HOME, TMPDIR, locale). Those are matched against
    # the named allowlist above via the shared convention (exact on POSIX, case-folded on
    # Windows — see platform_compat.env_key_allowed), so the set is auditable and cannot
    # silently grow to include a credential.
    env: dict[str, str] = {
        name: value
        for name, value in os.environ.items()
        if platform_compat.env_key_allowed(name, _MEASURE_ENV_PASSTHROUGH)
    }
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Prepend the tree so the candidate's OWN source is imported, not an installed copy
    # of the same package that happens to be on the path. ``<tree>/src`` goes on too
    # when it exists: in a src-layout repo the run root is the repo root (that is where
    # the suite is) but the package is one level down, and an uninstalled src-layout
    # repo is not importable without it.
    roots = [str(tree)]
    inner = Path(tree) / "src"
    if inner.is_dir():
        roots.append(str(inner))
    existing = env.get("PYTHONPATH", "")
    if existing:
        roots.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(roots)
    # Drop any PYTEST_ADDOPTS INHERITED from the gateway's environment, so our runs do
    # not silently pick up flags the operator's shell happened to export (the arms must
    # be byte-identical and repo-independent). This does NOT neutralize the target
    # repo's ini ``addopts`` — an env value is APPENDED to the ini one, not a
    # replacement — that is done with ``-o addopts=`` in :func:`_pytest_argv`.
    env["PYTEST_ADDOPTS"] = ""
    return env


# The credential-env strip lives in ``spine/push_policy`` — ONE definition shared with the
# agent spawn. It was duplicated here and there, and a duplicated security decision is how
# the empty-allowlist inversion survived in one copy after being fixed in the other.
from ...spine.push_policy import strip_credential_env  # noqa: E402


def _xdist_argv() -> tuple[str, ...]:
    """``-n auto`` when pytest-xdist is importable, else empty.

    Probed once at import: the full-suite STAYGREEN run needs parallelism (see
    :meth:`PytestBugRunner.run_suite`), but a target repo without xdist installed
    must still run — passing ``-n`` there is a usage error that would look like a
    red suite.
    """
    try:
        import xdist  # noqa: F401
    except Exception:  # noqa: BLE001 — absent/broken plugin → run serially
        return ()
    return ("-n", "auto")


_XDIST_ARGV: tuple[str, ...] = _xdist_argv()

#: Substrings that mean xdist itself failed to run, as opposed to tests failing.
#: Used to retry the suite serially so the gate still gets a real verdict.
_XDIST_FAILURE_MARKERS = (
    "unrecognized arguments: -n",
    "error: unrecognized arguments",
    "Different tests were collected",
    "node down",
    "worker crashed",
    "Replacing crashed worker",
)


def _looks_like_xdist_failure(stdout: str, stderr: str) -> bool:
    """True when the run failed because of xdist, not because tests failed."""
    blob = f"{stdout}\n{stderr}"
    return any(m in blob for m in _XDIST_FAILURE_MARKERS)


def _suite_scope_for_globs(clone: Path, globs: list[str] | None) -> list[str]:
    """The test PATHS the gate suite should run, given a NARROWED edit allowlist.

    Empty (→ run the whole tree) unless the operator confined edits to a subtree. When
    they did, gating a one-subtree change against the ENTIRE repo suite is both
    infeasible and pointless: this monorepo collects 21,901 tests that cannot finish
    inside the suite timeout even in parallel, while the edit fence already guarantees
    the change touches only the subtree — so a regression it can cause is one its own
    subtree's tests would catch. We therefore scope the STAYGREEN/build suite to the
    ``tests``/``test`` directory nearest the edit region (28s vs a 900s timeout here).

    Derivation: take the common ANCESTOR directory of the edit globs (strip the glob
    tail), then use the first of ``<ancestor>/tests`` / ``<ancestor>/test`` that exists.
    Returns [] — meaning "no scope, run everything" — when no allowlist is set, the
    globs share no ancestor, or no test dir is found there (fail OPEN to the safe,
    whole-tree behavior rather than silently gating nothing).
    """
    if not globs:
        return []
    # Directory part of each glob (drop the ``*.py`` / ``**`` tail).
    dirs: list[tuple[str, ...]] = []
    for g in globs:
        head = g.split("*", 1)[0]  # everything before the first glob metachar
        parts = tuple(p for p in head.split("/") if p and p != ".")
        # Drop a trailing partial filename fragment (a glob like ``foo/bar_*.py`` →
        # head ``foo/bar_`` → the ``bar_`` piece is not a directory).
        if parts and not (clone / Path(*parts)).is_dir():
            parts = parts[:-1]
        if parts:
            dirs.append(parts)
    if not dirs:
        return []
    # Common ancestor of all edit dirs.
    ancestor = dirs[0]
    for d in dirs[1:]:
        common: list[str] = []
        for a, b in zip(ancestor, d):
            if a != b:
                break
            common.append(a)
        ancestor = tuple(common)
        if not ancestor:
            return []
    # Walk UP from the common ancestor to the NEAREST enclosing test dir. A single
    # glob (``.../backend/*.py``) has the edited dir itself as its ancestor, and test
    # dirs sit beside a package rather than inside it — without the walk that common
    # case would fail open and re-run the whole repo. Walking up is safe by
    # construction: each step widens the suite, and the last candidate is the repo's
    # own top-level test dir (i.e. the original whole-tree behavior, just explicit).
    walk: list[str] = list(ancestor)
    while True:
        base = clone / Path(*walk) if walk else clone
        for name in ("tests", "test"):
            if (base / name).is_dir():
                # REPO-RELATIVE so it composes with cwd=_repo_root regardless of the
                # src-layout ``src`` prepend.
                return [str(Path(*walk) / name) if walk else name]
        if not walk:  # checked the repo root — nothing above it
            return []
        walk.pop()


def _pytest_argv(*args: str) -> list[str]:
    """``<this interpreter> -m pytest`` plus ``args``.

    Deliberately the RUNNING interpreter rather than a ``pytest`` found on PATH: the
    interpreter executing the gateway is the one with the app's dependencies
    installed, and a PATH ``pytest`` from a different environment is the classic
    source of "collection error" verdicts that look like a real RED but are not.
    ``-p no:cacheprovider`` keeps pytest from writing ``.pytest_cache`` into the tree
    (a write the do-not-pollute discipline would rightly flag). ``--color=no`` keeps
    the summary machine-readable: pytest colorizes ``FAILED``/``ERROR`` lines — and the
    test name INSIDE the nodeid — whenever color is on, and a nodeid carrying escape
    sequences cannot be handed back to pytest to re-run.

    ``-o addopts=`` is load-bearing: the TARGET repo's own ini ``addopts`` otherwise
    apply to every invocation we make, and a repo that ships coverage + ``-n auto``
    makes each gate step pay whole-package instrumentation and full xdist worker
    startup. Measured on this repo, collecting ONE trivial test: **73.9s with the
    repo's addopts, 0.17s with them overridden** — a ~430x difference, and it also
    wrote a coverage report into the tree. That cost is charged FOUR times per
    candidate (T2 collect, RED, GREEN, STAYGREEN), so a valid candidate could blow the
    180s quick timeout and be recorded as "reproducing test does not collect" — a
    harness artifact reported as a defect in the agent's test. It also protects
    MEASUREMENT VALIDITY: coverage instrumentation and xdist scheduling are exactly
    the variance the A/B noise band exists to exclude, so paying them inside a timed
    arm measures the harness, not the change.

    Note ``-o addopts=`` and NOT ``PYTEST_ADDOPTS=""``: an env ``PYTEST_ADDOPTS`` is
    APPENDED to the ini value rather than replacing it, so the empty string is inert
    (verified — coverage still ran). Overriding the ini key is what actually drops them.
    """
    return [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "--color=no",
        "-o",
        "addopts=",
        *args,
    ]


def _time_suite(
    tree: Path, *, extra: tuple[str, ...] = (), timeout: float = _SUITE_TIMEOUT_S
) -> tuple[float, bool]:
    """Wall-clock one full suite run in ``tree``. Returns ``(seconds, passed)``.

    ``time.perf_counter`` around the subprocess, not pytest's own reported duration:
    interpreter start and collection are real costs a candidate can improve (or
    regress), and pytest's summary excludes them.
    """
    root = _repo_root(tree)
    argv = _pytest_argv("-q", *extra)
    t0 = time.perf_counter()
    try:
        proc = _run(argv, cwd=root, timeout=timeout, env=_measure_env(root))
    except (OSError, subprocess.SubprocessError):
        # A timeout/spawn failure is not a slow run — it is no measurement at all.
        return float("nan"), False
    elapsed = time.perf_counter() - t0
    # pytest exit 0 = all passed, 5 = no tests collected. Treat 5 as "not passed":
    # an empty suite provides no correctness signal to guardrail on.
    return elapsed, proc.returncode == 0


def _collected_count(tree: Path) -> int:
    """Number of tests pytest collects in ``tree``, or -1 when it cannot be read.

    This is the RH guard's raw material: a candidate that made the suite faster by
    deleting tests changes this number, and nothing else in the pipeline would see it.
    """
    root = _repo_root(tree)
    try:
        proc = _run(
            _pytest_argv("-q", "--collect-only"),
            cwd=root,
            timeout=_QUICK_TIMEOUT_S,
            env=_measure_env(root),
        )
    except (OSError, subprocess.SubprocessError):
        return -1
    stdout = _ANSI_RE.sub("", proc.stdout or "")
    match = _COLLECTED_RE.search(stdout)
    if match:
        return int(match.group(1))
    # Older/quieter pytest output: count the collected nodeids instead.
    ids = [ln for ln in stdout.splitlines() if "::" in ln]
    return len(ids) if ids else -1


def _failing_nodeids(stdout: str) -> list[str]:
    """Pull ``FAILED``/``ERROR`` nodeids out of ``pytest -q`` output.

    Exact nodeids matter: the spine's STAYGREEN check re-runs them against BASE to
    decide whether a failure is pre-existing (not the candidate's fault) or a real
    regression. When nothing parses, the caller must NOT report an empty list —
    see :meth:`PytestBugRunner.run_suite`.

    Escape sequences are stripped before matching. ``--color=no`` normally prevents
    them, but the target repo's own ``addopts``/``force_color`` can put them back, and
    pytest colors the test name in the MIDDLE of the nodeid — so an unstripped line
    yields no match at all, and STAYGREEN would then read "no pre-existing failures"
    on a repo that has them.
    """
    out: list[str] = []
    for line in (stdout or "").splitlines():
        match = _NODEID_RE.match(_ANSI_RE.sub("", line).strip())
        if match:
            out.append(match.group(1))
    return out


# ── ① the ruler: wall-clock seconds of the repo's own test suite ─────────────


class SuiteRuler:
    """Field ① — the primary metric is the suite's wall-clock time, minimized.

    Why the test suite and not a microbenchmark: it is the one workload every Python
    repo already has, it exercises the code paths the repo's authors consider load
    bearing, and it needs no per-repo harness authoring. A repo that ships a real
    benchmark command is better served by it, so ``benchmark_cmd`` overrides the
    suite when configured.

    The A/B discipline (serial, pinned, warmups discarded, median-not-mean,
    interleaved) belongs to the spine's :class:`~...spine.measurer.Measurer`; this
    class supplies ONE paired base-vs-candidate sample per call and nothing else.
    """

    primary_name = "suite_wall_seconds"
    unit = UNIT_SECONDS
    direction = "minimize"
    substages = [STAGE_SUITE, STAGE_COLLECT]
    guardrails = [GUARDRAIL_TESTS_PASS]
    rh_guards = [RH_TEST_COUNT]

    def __init__(self, *, benchmark_cmd: str = "") -> None:
        #: Optional repo-supplied benchmark command (config ``benchmarkCommand``).
        #: When set it replaces the suite as the timed workload; it is split on
        #: whitespace and run WITHOUT a shell, so no shell metacharacters are honored.
        self.benchmark_cmd = (benchmark_cmd or "").strip()
        #: The byte-identical incidental conditions. Off-limits to the agent and
        #: recorded so a later run can tell whether it is comparable to this one.
        self.measurement_constants: dict[str, str] = {
            "interpreter": sys.executable,
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "runner": self.benchmark_cmd or "python -m pytest -q",
            "timer": "time.perf_counter around the subprocess",
        }
        #: Baseline medians captured during calibration, used to DERIVE the guardrail
        #: tolerance the driver adopts (see :meth:`guardrail_tolerances`).
        self._baseline_median: float | None = None
        #: Duck-typed by the driver so a Stop click interrupts the calibration loop
        #: between reps instead of waiting out ~30 full suite runs.
        self.stop_check: Callable[[], bool] | None = None

    # ── the timed workload ───────────────────────────────────────────────────

    def _time_once(self, tree: Path, *, collect_only: bool = False) -> tuple[float, bool]:
        """One timed run of the workload in ``tree``. Returns ``(seconds, passed)``."""
        if self.benchmark_cmd and not collect_only:
            root = _repo_root(tree)
            t0 = time.perf_counter()
            try:
                proc = _run(
                    self.benchmark_cmd.split(),
                    cwd=root,
                    timeout=_SUITE_TIMEOUT_S,
                    env=_measure_env(root),
                )
            except (OSError, subprocess.SubprocessError):
                return float("nan"), False
            return time.perf_counter() - t0, proc.returncode == 0
        extra = ("--collect-only",) if collect_only else ()
        return _time_suite(tree, extra=extra)

    def _sample(self, tree: Path) -> tuple[float, bool, float]:
        """One arm of an A/B: ``(wall_seconds, passed, collect_seconds)``.

        The collection sub-time is measured separately so a win can be ATTRIBUTED to a
        named stage (the spine requires a win be pinned to a substage). Collection is
        the cheap part, so timing it costs little relative to the suite itself.
        """
        wall, passed = self._time_once(tree)
        collect, _ = self._time_once(tree, collect_only=True)
        return wall, passed, collect

    # ── the seam ─────────────────────────────────────────────────────────────

    def measure(
        self, *, base_src: Path, cand_src: Path, commit_sha: str, scenario: str
    ) -> Measurement:
        """One paired base-vs-candidate sample, measured base-arm-first.

        ``commit_sha`` is the sha Phase C gated. We do not re-assert it against the
        tree: :class:`PytestBuildGate` declares ``single_environment = True``, so the
        gate and this measurement run in the SAME environment on the SAME tree and
        the spine skips its cross-environment sha assertion (07_*.md §1.2). The sha is
        recorded in the note so a result row is still traceable to an artifact.
        """
        base_wall, base_pass, base_collect = self._sample(base_src)
        cand_wall, cand_pass, cand_collect = self._sample(cand_src)

        if base_wall != base_wall or cand_wall != cand_wall:  # NaN check
            return Measurement(ok=False, note="workload did not complete (timeout or spawn error)")

        # RH-A: a candidate must not have deleted tests to go faster. -1 means the
        # count could not be read on one side, which we treat as "cannot verify" →
        # NOT ok, because an unverifiable RH guard is indistinguishable from a
        # defeated one and this is the highest-value cheat against a suite ruler.
        base_n, cand_n = _collected_count(base_src), _collected_count(cand_src)
        rh_capability_ok = base_n >= 0 and cand_n >= 0 and cand_n >= base_n

        return Measurement(
            ok=True,
            primary_delta=cand_wall - base_wall,  # negative == faster == better
            primary_base=base_wall,
            primary_cand=cand_wall,
            stages=StageBreakdown(
                stages={
                    STAGE_SUITE: (cand_wall - cand_collect) - (base_wall - base_collect),
                    STAGE_COLLECT: cand_collect - base_collect,
                }
            ),
            # BLOCKING, positive == regression magnitude: 0.0 while the suite is green,
            # 1.0 the moment it is not (the keeper's default tolerance is 0).
            guardrails={GUARDRAIL_TESTS_PASS: 0.0 if cand_pass else 1.0},
            # NON-BLOCKING observability the archive surfaces per candidate.
            secondary={
                "base_tests_collected": float(base_n),
                "cand_tests_collected": float(cand_n),
                "base_pass": 1.0 if base_pass else 0.0,
            },
            rh_capability_ok=rh_capability_ok,
            # RH-B: the functional probe IS the suite — it ran and it passed.
            rh_functional_ok=cand_pass,
            note=f"sha={commit_sha[:10]} scenario={scenario or 'suite'} tests={base_n}->{cand_n}",
        )

    def baseline_samples(self, *, base_src: Path, reps: int) -> list[float]:
        """Phase-1 calibration: time the UNTOUCHED baseline ``reps`` times.

        Honors the driver's duck-wired ``stop_check`` between reps — a Stop click
        during a 10-rep calibration of a two-minute suite should not take 20 minutes
        to take effect. A stop mid-calibration returns the partial list, which the
        spine surfaces as a :class:`CalibrationError` when it is too short to have a
        spread; that is the correct outcome (a stopped run has no proven ruler).
        """
        out: list[float] = []
        for _ in range(max(int(reps), 2)):
            check = self.stop_check
            if callable(check) and check():
                break
            wall, _passed = self._time_once(base_src)
            if wall == wall:  # skip NaN (a failed/timed-out rep is not a sample)
                out.append(wall)
        if out:
            out_sorted = sorted(out)
            self._baseline_median = out_sorted[len(out_sorted) // 2]
        return out

    def measure_canary(self, *, base_src: Path) -> Measurement:
        """Phase-1 canary: the forced, mechanically-known win (see module docstring).

        The candidate arm runs ``--collect-only`` — every test is collected but none
        executed — while the base arm runs the suite normally. Both arms pay identical
        interpreter start + collection cost, so the delta is exactly the suite's
        execution time: correctly-signed (negative for minimize) and requiring no edit
        to the repo. It proves the ruler can resolve a win of that size, which is a
        LOWER BOUND on sensitivity, not proof it resolves a 3% win — which is precisely
        why the supervisor runs the canary advisory.

        Both arms are sampled ``_CANARY_REPS`` times and aggregated by MEDIAN for the
        same reason the spine's measurer does it: a single pair lets one scheduling
        hiccup decide the sign, and a canary whose sign is noise-determined reports a
        broken ruler on a working one (or worse, the reverse). Interleaved base-first
        so a monotonic host drift (thermal, a background build starting) biases both
        arms the same way instead of loading it all onto the second arm.

        The forced win is ``--collect-only`` vs a full pytest run — which only means
        anything when the workload IS pytest. With a custom ``benchmarkCommand`` configured,
        ``_time_once`` runs the benchmark for the base arm but STILL runs ``pytest
        --collect-only`` for the candidate arm, so ``delta`` would compare a benchmark against
        pytest collection — two unrelated workloads. Any benchmark slower than collection
        yields ``delta < 0`` and clears the sensitivity check without the ruler ever being
        exercised. There is no mechanically-known win for an arbitrary command, so refuse to
        certify: ``ok=False`` -> preflight reports the canary did not clear and the run halts
        rather than optimizing an unproven ruler. Raised by the Opus review.
        """
        if self.benchmark_cmd:
            return Measurement(
                ok=False,
                note="no mechanically-known win exists for a custom benchmarkCommand — "
                "the --collect-only canary only proves sensitivity for a pytest workload",
            )
        base_samples: list[float] = []
        cand_samples: list[float] = []
        base_pass = True
        for _ in range(_CANARY_REPS):
            b_wall, b_pass = self._time_once(base_src)
            c_wall, _ = self._time_once(base_src, collect_only=True)
            if b_wall != b_wall or c_wall != c_wall:  # NaN: no measurement at all
                return Measurement(ok=False, note="canary workload did not complete")
            base_samples.append(b_wall)
            cand_samples.append(c_wall)
            base_pass = base_pass and b_pass
        base_wall = statistics.median(base_samples)
        cand_wall = statistics.median(cand_samples)
        delta = cand_wall - base_wall
        # A suite whose execution time is indistinguishable from its collection time has
        # NO win available to force — the canary cannot prove anything about the ruler on
        # such a repo. Say so (``ok=False`` -> preflight reports "canary did not clear")
        # rather than returning a delta whose sign the noise chose.
        if delta >= 0.0:
            return Measurement(
                ok=False,
                primary_delta=delta,
                primary_base=base_wall,
                primary_cand=cand_wall,
                note=(
                    "canary inconclusive: skipping all test execution did not measurably "
                    f"beat the full suite (base={base_wall:.3f}s cand={cand_wall:.3f}s) — "
                    "this suite is too fast for a wall-clock ruler to resolve a win in"
                ),
            )
        return Measurement(
            ok=True,
            primary_delta=delta,
            primary_base=base_wall,
            primary_cand=cand_wall,
            stages=StageBreakdown(stages={STAGE_SUITE: delta}),
            guardrails={GUARDRAIL_TESTS_PASS: 0.0 if base_pass else 1.0},
            note=(
                f"canary: collect-only vs full suite over {_CANARY_REPS} reps (median; "
                "forced known win, see module docstring)"
            ),
        )

    # ── optional companions the spine probes via getattr ─────────────────────

    def guardrail_tolerances(self) -> dict[str, float]:
        """Derived allowances the driver adopts after calibration.

        ``tests_still_pass`` is a strict 0: the suite going red is never within
        tolerance. Exposed anyway so the intent is explicit in the run log rather
        than implied by the keeper's default.
        """
        return {GUARDRAIL_TESTS_PASS: 0.0}

    def guardrail_baselines(self) -> dict[str, float]:
        """Baseline medians for the UI's measurement battery (display only)."""
        return {"suite_wall_seconds": self._baseline_median or 0.0}


# ── ② the build gate: the repo's own pytest run ──────────────────────────────


class PytestBuildGate:
    """Field ② — ``pytest -q`` in the candidate worktree. Boolean + the gated sha.

    ``single_environment = True``: for a repo target the gate and the measurement run
    in the same environment on the same tree, so the spine skips its cross-environment
    same-sha assertion (07_*.md §1.2 calls that split "a capability of the schema, not
    a spine assumption"). Setting it False would make the spine assert a sha equality
    that is trivially true here while adding a failure mode.
    """

    single_environment = True

    def __init__(self, *, suite_scope: list[str] | None = None) -> None:
        #: Repo-relative test paths to run instead of the whole tree. Set when the
        #: operator narrowed the edit allowlist — see _suite_scope_for_globs.
        self.suite_scope: list[str] = list(suite_scope or [])

    def build_and_test(self, *, worktree: Path, src: Path) -> GateResult:
        """Run the repo's unmodified suite. ``passed`` iff every test passed.

        There is no separate "build" step: import-time errors surface as pytest
        collection errors, which is the Python equivalent of a compile failure. The
        commit sha is left empty — :class:`~...spine.gate.Gate` snapshots it from the
        worktree and carries it forward onto this result.
        """
        root = _repo_root(src if Path(src).exists() else worktree)
        try:
            # PARALLEL: this is the full suite (T0 build/import smoke), which on a large
            # repo — 21,901 tests here — cannot finish serially inside the timeout. See
            # run_suite for the same reasoning; the targeted RED/GREEN runs stay serial.
            proc = _run(
                _pytest_argv("-q", *_XDIST_ARGV, *self.suite_scope),
                cwd=root,
                timeout=_SUITE_TIMEOUT_S,
                env=_measure_env(root),
            )
        except subprocess.TimeoutExpired:
            return GateResult(passed=False, detail=f"suite timed out after {_SUITE_TIMEOUT_S:.0f}s")
        except (OSError, subprocess.SubprocessError) as exc:
            return GateResult(passed=False, detail=f"could not run the suite: {exc}")
        # Retry serially if xdist itself failed to start, so a missing/broken plugin
        # reads as "run serially", not "suite red".
        if (
            proc.returncode != 0
            and _XDIST_ARGV
            and _looks_like_xdist_failure(proc.stdout or "", proc.stderr or "")
        ):
            try:
                proc = _run(
                    _pytest_argv("-q", *self.suite_scope),
                    cwd=root,
                    timeout=_SUITE_TIMEOUT_S,
                    env=_measure_env(root),
                )
            except subprocess.TimeoutExpired:
                return GateResult(
                    passed=False, detail=f"suite timed out after {_SUITE_TIMEOUT_S:.0f}s"
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return GateResult(passed=False, detail=f"could not run the suite: {exc}")
        failing = _failing_nodeids(proc.stdout or "")
        if proc.returncode == 0:
            return GateResult(passed=True, detail="suite green")
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-1:] or [""]
        return GateResult(
            passed=False,
            detail=f"suite red (exit {proc.returncode}): {tail[0][:160]}",
            failing_tests=failing,
        )


# ── ②b the bug runner: deterministic RED/GREEN primitives ───────────────────


class PytestBugRunner:
    """Field ②b — the deterministic primitives the spine's RED/GREEN gate composes.

    The gate discipline (static-triage ladder → doubled RED → GREEN → STAYGREEN) is
    entirely in :class:`~...spine.bug_gate.BugGate`; nothing here decides anything.
    Each method is a bounded subprocess whose result the gate cannot argue past.
    """

    def __init__(self, *, suite_scope: list[str] | None = None) -> None:
        #: Repo-relative test paths the STAYGREEN full-suite run is confined to, when
        #: the operator narrowed the edit allowlist. See _suite_scope_for_globs.
        self.suite_scope: list[str] = list(suite_scope or [])

    def build_imports_ok(self, *, src: Path) -> bool:
        """T0: every Python module in the tree imports (the compile-equivalent smoke).

        Byte-compiling with ``compileall`` rather than importing each module: importing
        executes module-level code, which for an arbitrary repo can open sockets, read
        credentials, or block. Compilation catches the same class of defect (syntax and
        obvious structural breakage) with no side effects.
        """
        root = _repo_root(src)
        try:
            proc = _run(
                [sys.executable, "-m", "compileall", "-q", "-x", r"(\.venv|node_modules)", "."],
                cwd=root,
                timeout=_QUICK_TIMEOUT_S,
                env=_measure_env(root),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def _lint_findings(self, tree: Path) -> set[str] | None:
        """Lint findings in ``tree`` as ``file:code`` tokens, or None when no linter.

        Deliberately drops the line number: a candidate's edit shifts every later line
        in the file, so a line-keyed comparison would report the whole tail of a file as
        "new violations". ``file:code`` is stable under a shift while still catching a
        genuinely new violation kind or a new file.
        """
        root = _repo_root(tree)
        # Tried in order of fidelity; the first linter that actually RUNS wins. ruff
        # and pyflakes both emit ``path:line:col: CODE message``, so one parser serves.
        # ``--color=never``: ruff colorizes even when piped, and an SGR sequence lands
        # INSIDE the parsed fields — the path becomes "\x1b[1m<path>\x1b[0m" and the
        # rule code parses as EMPTY, so every finding in a file collapsed to one
        # malformed token and T1's base-vs-candidate set difference was unreliable
        # (measured: F401/F841/F403 all became ""). The env belt-and-braces plus the
        # _ANSI_RE strip below cover a repo config that forces color back on.
        for argv in (
            ["ruff", "check", "--output-format=concise", "--color=never", "."],
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--output-format=concise",
                "--color=never",
                ".",
            ],
            [sys.executable, "-m", "pyflakes", "."],
        ):
            if argv[0] == "ruff" and shutil.which("ruff") is None:
                continue
            try:
                proc = _run(argv, cwd=root, timeout=_QUICK_TIMEOUT_S, env=_measure_env(root))
            except (OSError, subprocess.SubprocessError):
                continue
            # A missing module exits non-zero with an import error and no findings —
            # distinguish that from "linter ran and found problems".
            if "No module named" in (proc.stderr or ""):
                continue
            findings: set[str] = set()
            for raw_line in (proc.stdout or "").splitlines():
                # Strip SGR sequences BEFORE splitting: they sit inside the path and
                # the code fields (see the --color=never note above), so parsing a
                # colorized line yields a mangled path and an empty rule code.
                line = _ANSI_RE.sub("", raw_line)
                # ruff also emits `warning: ...` diagnostics about its own config on
                # stdout; they are not findings and have no path:line:col shape.
                if line.startswith(("warning:", "error:")):
                    continue
                parts = line.split(":", 3)
                if len(parts) < 3:
                    continue
                path = parts[0].strip()
                rest = parts[3] if len(parts) > 3 else ""
                code = (rest.strip().split(" ", 1)[0] or "?").rstrip(":")
                findings.add(f"{path}:{code}")
            return findings
        return None

    def lint_clean(self, *, base_src: Path, cand_src: Path) -> bool:
        """T1: the candidate introduces no NEW lint violation vs base.

        Degrades gracefully in two documented steps. With ruff or pyflakes available we
        compare finding SETS and pass iff the candidate adds none — a repo with
        pre-existing violations is not punished for them. With no linter installed we
        fall back to :meth:`build_imports_ok` (byte-compilation), which is a weaker but
        honest signal rather than a fabricated pass.
        """
        cand = self._lint_findings(cand_src)
        if cand is None:
            return self.build_imports_ok(src=cand_src)
        base = self._lint_findings(base_src) or set()
        return not (cand - base)

    def test_collects(self, *, src: Path, test_path: str) -> bool:
        """T2: the reproducing test file collects. A non-collecting test cannot be RED."""
        root = _repo_root(src)
        target = (test_path or "").strip()
        if not target:
            return False
        try:
            proc = _run(
                _pytest_argv("-q", "--collect-only", target),
                cwd=root,
                timeout=_QUICK_TIMEOUT_S,
                env=_measure_env(root),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def run_reproducing_test(self, *, src: Path, test_id: str, test_only: bool) -> bool | None:
        """Run ONE nodeid. True = passed, False = failed, None = errored.

        The three-way return is load-bearing and the distinction is exactly pytest's:
        exit 1 means a test ran and asserted false (a clean FAIL — a valid RED), while
        exit 2/3/4 (usage, internal, interrupted) and exit 5 (nothing collected) mean
        the test never really ran. The gate maps None to ``BUG_TEST_INVALID`` rather
        than accepting it as RED, which is what stops a test that merely fails to
        import from masquerading as a reproduction.
        """
        root = _repo_root(src)
        nodeid = (test_id or "").strip()
        if not nodeid:
            return None
        try:
            proc = _run(
                _pytest_argv("-q", nodeid),
                cwd=root,
                timeout=_QUICK_TIMEOUT_S,
                env=_measure_env(root),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            # Exit 1 covers both an assertion failure and a collection ERROR. Only the
            # former is a valid RED, so read the summary: an ``ERROR`` line means the
            # test could not run (invalid), a ``FAILED`` line means it ran and failed.
            out = _ANSI_RE.sub("", proc.stdout or "")
            if re.search(r"^ERROR\s", out, re.MULTILINE) or "errors during collection" in out:
                return None
            return False
        return None

    def run_suite(self, *, src: Path) -> tuple[bool, list[str]]:
        """STAYGREEN: the full suite. Returns ``(all_green, failing_nodeids)``.

        When the suite is red but no nodeid parses, we return the sentinel the spine
        recognizes (``<unparsed-suite-failure>``) instead of an empty list. An empty
        list alongside ``False`` would read as "red, but nothing failing" and let the
        gate subtract zero pre-existing failures — admitting a regression. The
        sentinel makes :class:`BugGate` fall back to its conservative verdict.
        """
        root = _repo_root(src)
        try:
            proc = _run(
                # PARALLEL, unlike the single-test runs. ``-o addopts=`` drops the repo's
                # coverage (which we do not want) but also its ``-n auto`` (which we DO,
                # here only). This repo collects 21,901 tests: serial they cannot finish
                # inside _SUITE_TIMEOUT_S, and a timeout returns the unparseable sentinel
                # below, which the gate must read as "regressed" — so STAYGREEN failed for
                # every candidate with "suite is NOT green but reported no identifiable
                # failing test". Restoring xdist for the FULL suite fixes that; the
                # targeted RED/GREEN/collect runs stay serial, where worker startup would
                # be pure overhead on one test. ``-p xdist`` is not assumed: fall back to a
                # serial run when the plugin is missing (a bare target repo).
                _pytest_argv("-q", *_XDIST_ARGV, *self.suite_scope),
                cwd=root,
                timeout=_SUITE_TIMEOUT_S,
                env=_measure_env(root),
            )
        except (OSError, subprocess.SubprocessError):
            return False, ["<unparsed-suite-failure>"]
        if proc.returncode == 0:
            return True, []
        # An xdist-side startup failure (plugin absent, worker crash) is not a test
        # failure: retry serially so a real verdict is still produced rather than the
        # conservative "regressed" the sentinel would force.
        if _XDIST_ARGV and _looks_like_xdist_failure(proc.stdout or "", proc.stderr or ""):
            try:
                proc = _run(
                    _pytest_argv("-q", *self.suite_scope),
                    cwd=root,
                    timeout=_SUITE_TIMEOUT_S,
                    env=_measure_env(root),
                )
            except (OSError, subprocess.SubprocessError):
                return False, ["<unparsed-suite-failure>"]
            if proc.returncode == 0:
                return True, []
        failing = _failing_nodeids(proc.stdout or "")
        return False, failing or ["<unparsed-suite-failure>"]

    # ── optional companions the spine probes via getattr ─────────────────────

    def run_named_tests(self, *, src: Path, test_ids: list[str]) -> set[str]:
        """Run ONLY ``test_ids`` and return the subset that FAIL or ERROR.

        The gate calls this on BASE to subtract pre-existing failures from a STAYGREEN
        result — a suite failure that also fails on base is not a regression the fix
        caused. Without it the gate conservatively treats every suite failure as a
        regression, which rejects a valid fix in any repo with a flaky test.
        """
        ids = [t for t in (test_ids or []) if t and t != "<unparsed-suite-failure>"]
        if not ids:
            return set()
        root = _repo_root(src)
        failed: set[str] = set()
        for nodeid in ids:
            verdict = self.run_reproducing_test(src=root, test_id=nodeid, test_only=False)
            if verdict is not True:  # False (failed) or None (errored) both count
                failed.add(nodeid)
        return failed

    def agent_test_hint(self, worktree: Path) -> str:
        """The exact test command the gate itself uses, handed to the fix-authoring
        agent. Without it the agent burns ~20 minutes per candidate hunting for an
        interpreter that has the dependencies — this one already does."""
        return (
            f"cd {_repo_root(Path(worktree) / 'src')} && {sys.executable} -m pytest -q <test_path>"
        )


# ── ③ the edit allowlist: source in, tests-of-record out ────────────────────


class RepoEditAllowlist:
    """Field ③ — the mechanical path fence. Source may be edited; the suite may not.

    The asymmetry that matters: the perf track may not touch ``tests/**`` AT ALL,
    because the suite is the ruler's measurement subject and editing it is metric
    gaming the build gate cannot see. The bug track must be able to ADD a new
    reproducing test, because a bug fix without one cannot be proven RED→GREEN.

    Both are served by exposing the protocol's ``allows(paths)`` AND the optional
    status-aware ``allows_changes(status_paths)`` that :meth:`Gate.check_allowlist`
    prefers via ``getattr``. The status-aware form sees ``A`` (added) vs ``M``
    (modified), so "add ``tests/test_bug_x.py``" and "edit ``tests/test_core.py``"
    stop being the same event.
    """

    def __init__(
        self,
        *,
        allowed: list[str] | None = None,
        scope: set[str] | None = None,
        track: str = TRACK_BUG,
    ) -> None:
        #: Source globs the agent MAY edit. Defaults cover both repo layouts
        #: (``src/`` package and flat module) since we cannot know which we have.
        self.allowed: list[str] = list(allowed or ["src/**/*.py", "*.py", "**/*.py", "lib/**/*.py"])
        #: Extends — never relaxes — the spine's default-deny categories.
        self.off_limits: list[str] = list(_ALWAYS_OFF_LIMITS)
        #: Which track this fence serves. Only the BUG track may ADD a reproducing test; the
        #: perf track may not touch the suite at all, because the suite is the ruler's own
        #: measurement subject. Defaults to the bug track so an omitted argument is the
        #: RESTRICTIVE-for-perf choice rather than a silent carve-out.
        self.track: str = track
        #: Optional diff-scope tightening (config ``scopeDiffBase``): when set, an
        #: edit must ALSO be inside the change set the branch introduced. A newly
        #: ADDED reproducing test is exempt — it is by definition not in the base diff.
        self.scope = scope

    # ── predicates ───────────────────────────────────────────────────────────

    @staticmethod
    def _matches(path: str, globs: list[str] | tuple[str, ...]) -> bool:
        """Glob match on both the path and its basename.

        Matching the basename too is what makes ``test_*.py`` catch
        ``pkg/sub/test_thing.py``: ``fnmatch`` treats ``*`` as matching separators
        inconsistently across patterns, and relying on that would leave a hole in the
        fence. Two cheap checks are better than a subtle one.
        """
        name = Path(path).name
        return any(fnmatch(path, g) or fnmatch(name, g) for g in globs)

    def _is_addable_test(self, path: str) -> bool:
        """True iff ``path`` is the ONE new-test shape the bug track may add."""
        return self._matches(path, _ADDABLE_TEST_GLOBS)

    def is_tooling_artifact(self, path: str) -> bool:
        """True iff ``path`` is agent-tooling debris to be ignored, not judged.

        Kept public so the gate can filter these OUT of the changed-path list before
        the fence sees them — see :data:`_TOOLING_ARTIFACTS` for why judging them
        rejected a verified fix on the first live run.
        """
        return self._matches(path, _TOOLING_ARTIFACTS)

    def _reject(self, path: str, *, added: bool) -> bool:
        """Core fence decision for one path. True == reject."""
        if not path:
            return True
        # Path traversal / absolute escapes: a changed path is always repo-relative.
        # Checked BEFORE the artifact ignore so a crafted ``.kiro/../../etc`` cannot
        # slip through by matching an ignore glob.
        if path.startswith("/") or ".." in Path(path).parts:
            return True
        # Agent-tooling debris is not part of the change under test — ignore it
        # instead of failing the candidate over it.
        if self.is_tooling_artifact(path):
            return False
        # An ADDED reproducing test of the sanctioned shape is allowed through the test
        # denylist — the BUG track's one carve-out, and only for additions. Modifying an
        # existing test always falls through to off_limits.
        #
        # Gated on the track, which it previously was not: the class docstring promised "the
        # perf track may not touch tests/** AT ALL" while the code granted the carve-out to
        # both. That is the gaming the fence exists to stop — the RH guard compares collected
        # test COUNTS, so adding one cheap test while an expensive one stops being collected
        # keeps the count equal while measured suite time drops, and a purely artifactual
        # "win" gets drafted as a real perf PR. Raised by the GPT review.
        if added and self.track == TRACK_BUG and self._is_addable_test(path):
            return False
        if self._matches(path, self.off_limits):
            return True
        if not self._matches(path, self.allowed):
            return True
        # Diff-scope tightening, when configured. Additions are exempt (a new file is
        # never part of the base diff), so this narrows EDITS to the branch's own files.
        if self.scope is not None and not added and not scope_util.in_scope(path, self.scope):
            return True
        return False

    # ── the seam ─────────────────────────────────────────────────────────────

    def allows(self, changed_paths: list[str]) -> tuple[bool, list[str]]:
        """Protocol form: name-only. Every path is judged as a MODIFICATION.

        Judging an unknown change as a modification is the fail-closed reading: it
        cannot accidentally admit a test edit by assuming it was an addition. The
        bug track's added-test carve-out therefore requires the status-aware form,
        which the spine's gate always prefers when present.
        """
        offending = [p for p in (changed_paths or []) if self._reject(p, added=False)]
        return (not offending), offending

    def allows_changes(self, status_paths: list[tuple[str, str]]) -> tuple[bool, list[str]]:
        """Status-aware form the spine's gate prefers: ``[(git_status, path), ...]``.

        ``status`` is a git name-status letter (``A`` added, ``M`` modified, ``R``
        renamed, ``D`` deleted…). Only a leading ``A`` earns the added-test carve-out;
        a rename INTO a sanctioned test path does not, because a rename's source side
        is also reported and a renamed-away test is a deleted test.
        """
        offending: list[str] = []
        for status, path in status_paths or []:
            added = (status or "").upper().startswith("A")
            if self._reject(path, added=added):
                offending.append(path)
        return (not offending), offending


# ── ④ the isolation recipe: the push-disabled clone ─────────────────────────


class RepoIsolation:
    """Field ④ — the push-disabled clone, the pinned base ref, the pollute path set.

    ``frozen_components`` is empty and :meth:`measurement_boot` is a no-op. Both are
    correct answers for a repo target rather than omissions: there is no separate
    layer to hold byte-identical (the whole repo is the layer under optimization,
    fenced by the edit allowlist instead), and there is no runtime to boot — a fresh
    ``pytest`` process IS the measurement, spawned per rep inside the worktree.
    """

    frozen_components: list[str] = []

    def __init__(self, *, clone_path: Path, base_ref: str = "origin/main") -> None:
        self.clone_path = Path(clone_path)
        self.base_ref = base_ref or "origin/main"

    def push_disabled(self) -> bool:
        """True iff BOTH of origin's urls are mechanically neutralized.

        Mirrors ``clone_setup._ok()``'s sentinel test exactly — the same predicate that
        gated recording the clone in the first place, so the setup-time check and this
        run-time check cannot drift apart and disagree. Fails CLOSED: any git error,
        timeout, or unreadable url reads as "push is NOT disabled" and the driver
        refuses to start.

        BOTH urls, not just the push url: a live FETCH url is a live push target
        (``git push "$(git remote get-url origin)" HEAD`` ignores the push url entirely
        and writes to the fetch url — see ``clone_setup._disable_push``). Checking only
        the push url reported "disabled" for a clone that could still write to the remote;
        `_ok` checks both, and this drifted from it. Raised by the GPT review of this branch.
        """

        def _neutral(args: list[str]) -> bool:
            try:
                proc = _run(
                    ["git", "-C", str(self.clone_path), *args],
                    cwd=self.clone_path if self.clone_path.exists() else Path.cwd(),
                    timeout=30,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            if proc.returncode != 0:
                return False
            url = (proc.stdout or "").strip()
            return (not url) or ("DISABLED" in url.upper()) or ("NO_PUSH" in url.upper())

        return _neutral(["remote", "get-url", "--push", "origin"]) and _neutral(
            ["remote", "get-url", "origin"]
        )

    def do_not_pollute_paths(self) -> list[Path]:
        """Host paths the spine snapshots around the (no-op) measurement boot.

        The app's own data dir and the user's Kiro Crew home: the two places a leak
        would actually land if the measured workload wrote outside its worktree. We do
        NOT snapshot ``$HOME`` wholesale — hashing a developer's entire home directory
        would take minutes per run and flag every unrelated background write as a leak,
        producing a gate that is always red and therefore always ignored.
        """
        from ...backend import store

        paths: list[Path] = []
        try:
            paths.append(store.data_dir())
        except Exception:  # noqa: BLE001 — a missing data dir is not a leak
            pass
        try:
            from kiro_crew.config.loader import config_dir

            paths.append(Path(config_dir()))
        except Exception:  # noqa: BLE001 — config home is best-effort
            pass
        return paths

    def do_not_pollute_excludes(self) -> list[Path]:
        """Subpaths to ignore inside the snapshot roots (optional spine hook).

        The app's data dir lives UNDER the Kiro Crew config home, and the orchestrator
        writes its own ledger/logs/activity there DURING the boot window by design.
        Without this exclude those writes register as a phantom leak and block every
        run. Everything else under the config home is still hashed, so a real write by
        the measured workload is still caught.
        """
        from ...backend import store

        try:
            return [store.data_dir()]
        except Exception:  # noqa: BLE001
            return []

    def measurement_boot(self) -> Callable[[], None]:
        """A documented no-op: a repo target has no measurement runtime to boot.

        The protocol explicitly blesses this ("A profile with no measurement runtime
        to boot … returns a no-op callable (``lambda: None``); the test then degenerates
        to 'the spine touched nothing', which is still a true zero-diff"). The reason it
        is TRUE here and not a dodge: the measured workload is a fresh ``pytest``
        subprocess spawned per rep inside the throwaway worktree, with
        ``-p no:cacheprovider`` and ``PYTHONDONTWRITEBYTECODE=1`` so it writes neither
        a pytest cache nor ``.pyc`` files. There is no long-lived runtime whose startup
        could touch a host path, so there is nothing for a boot callable to bracket.
        """
        return lambda: None


# ── the assembled profile ───────────────────────────────────────────────────


class GitHubRepoProfile(ProfileFieldAliases):
    """The reference Target Profile: any Python GitHub repo with a pytest suite.

    Mixes in :class:`ProfileFieldAliases` so ``isolation_recipe`` /
    ``calibration_params`` resolve to ``isolation`` / ``calibration`` (both spellings
    appear in the design docs).
    """

    id = "github-repo"

    def __init__(
        self,
        *,
        clone_path: Path,
        pr_queue_dir: Path,
        user: str = "",
        base_ref: str = "origin/main",
        # The real remote, from CONFIG rather than from the clone: both of the clone's
        # urls are neutralized so agent-run Bash inside it cannot find a push target.
        # Only the pr_recipe (a trusted publisher) receives it.
        origin_url: str = "",
        track: str = TRACK_BUG,
        benchmark_cmd: str = "",
        scope_base: str = "",
        allowed_globs: list[str] | None = None,
        baseline_reps: int = 5,
        noise_floor_s: float = 0.25,
        log_dir: Path | None = None,
    ) -> None:
        self.clone_path = Path(clone_path)
        self.track = track
        #: The ``scopeDiffBase`` ref: when set, discovery and the edit fence are both
        #: narrowed to the change set this branch introduced.
        self.scope_base = (scope_base or "").strip()
        self._scope = scope_util.scoped_relpaths(self.clone_path, self.scope_base)
        # `scoped_relpaths` used to return None for THREE different situations — a blank ref, a
        # git FAILURE (bad/unresolvable ref), and a valid-but-EMPTY diff (base == HEAD) — and
        # the caller could not tell them apart. Treating all three as "no scope" means a
        # misconfigured `scopeDiffBase` silently widens the edit fence from "what this branch
        # changed" to the WHOLE REPOSITORY, the opposite of what setting a scope is for.
        #
        # Two fixes, in two review rounds. First: an unresolvable ref REFUSES here rather than
        # running unscoped. Then the remaining hole — `scopeDiffBase=HEAD` RESOLVES fine and
        # yields an empty diff, so it still fell through to unscoped. `scoped_relpaths` now
        # returns `set()` for a successful-but-empty diff (None only for blank/error), and the
        # allowlist checks `scope is not None`, so an empty scope enforces "no file may be
        # edited" — a run that keeps nothing beats a run that may edit anything.
        # The condition is simply "a scope was configured but could not be computed". The
        # REASON is deliberately not consulted: this guard was previously gated on
        # a ref-resolvability check, which only covered a base that does not exist. A base
        # that RESOLVES but whose diff fails left `_scope is None` and nothing refused —
        # reproduced with two unrelated histories, where `rev-parse --verify` succeeds while
        # `diff <ref>...HEAD` exits 128 "no merge base". Third variant of one bug on this
        # branch (unresolvable ref, empty diff, git error), so the guard now keys on the
        # CONSEQUENCE rather than enumerating causes. Raised by the GPT review.
        if self.scope_base and self._scope is None:
            raise ValueError(
                f"scopeDiffBase {self.scope_base!r} could not be resolved to a file set in "
                "this clone — refusing to run, because an uncomputable scope silently widens "
                "the edit fence to the whole repository"
            )
        self._log_dir = Path(log_dir) if log_dir else None

        self.ruler = SuiteRuler(benchmark_cmd=benchmark_cmd)  # ①
        # Confine the gate's FULL-suite runs (T0 build smoke + STAYGREEN) to the test
        # dir nearest a NARROWED edit allowlist. On a monorepo the whole-repo suite
        # cannot finish inside the timeout (21,901 tests here, vs 28s for the app's own
        # subtree), and a timeout is reported as an unidentifiable failure -> every
        # candidate "regressed". Empty when no allowlist is set: unchanged whole-tree
        # behavior. Logged below so a narrowed gate is never silent.
        suite_scope = _suite_scope_for_globs(self.clone_path, allowed_globs)
        if suite_scope:
            logger.info(
                "%s: gate suite scoped to %s (edit allowlist is narrowed); "
                "the whole-repo suite is not run",
                self.id,
                ", ".join(suite_scope),
            )
        self.build_gate = PytestBuildGate(suite_scope=suite_scope)  # ②
        self.bug_runner = PytestBugRunner(suite_scope=suite_scope)  # ②b
        # ``allowed_globs`` narrows WHICH files the agent may edit — e.g. confine a
        # run to one subdirectory so it cannot touch the rest of a large repo (the
        # blast-radius control used when dogfooding against a repo the app itself
        # lives in). The off-limits fence (tests/config/CI) still applies on top.
        self.edit_allowlist = RepoEditAllowlist(
            allowed=allowed_globs, scope=self._scope, track=track
        )  # ③
        #: The USER-SUPPLIED edit globs, or None when unset. Distinct from
        #: ``edit_allowlist.allowed``, which fills in a repo-wide default when unset —
        #: focusing discovery on THAT would enumerate the whole tree and mislabel it as
        #: "the only fixable region". Only a genuinely NARROWED allowlist should steer
        #: reads, so discovery keys off this raw value (see ``discover``).
        self._user_edit_globs = list(allowed_globs) if allowed_globs else None
        self.isolation = RepoIsolation(clone_path=self.clone_path, base_ref=base_ref)  # ④
        self.pr_recipe = GitHubPRRecipe(  # ⑤ — reused verbatim
            user=user,
            clone_path=self.clone_path,
            pr_queue_dir=Path(pr_queue_dir),
            base_ref=base_ref,
            fetch_url=origin_url or None,
        )
        self.calibration = CalibrationParams(  # ⑥
            # 5–10 reps, not the protocol's 30: each rep is a FULL suite run, so 30
            # reps of a two-minute suite is an hour of calibration before the loop
            # starts. The floor below carries the anti-noise weight instead.
            baseline_reps=max(2, min(int(baseline_reps), 10)),
            noise_band=0.0,  # the driver overwrites this with the calibrated band
            # An absolute floor in SECONDS. Sub-quarter-second "wins" on a suite that
            # takes seconds are host jitter, not optimization, and a deceptively quiet
            # calibration window would otherwise let them through.
            floor=float(noise_floor_s),
            canary_id="collect_only_vs_full_suite",
            anchors=[],
            drift_reanchor_cycles=5,
            heldout=[],
        )
        #: Duck-set by the driver each cycle: loci already terminal in the ledger, so
        #: discovery does not spend its read budget re-proposing them.
        self._skip_targets: list[str] = []
        #: Duck-set by the driver each cycle: rotates discovery's focus ordering so a
        #: bounded per-cycle read budget samples a different slice each cycle.
        self._discovery_rotate = 0

    # ── Phase A: discovery ───────────────────────────────────────────────────

    def discover(
        self,
        *,
        base_sha: str,
        top_k: list[dict],
        known_loci: list[str],
        agent_runner=None,
    ) -> DiscoveryResult:
        """Phase A — the agent reads the repo and names candidate surfaces.

        There is no repo-specific static analyzer to seed from, so the agent IS the
        discovery source (:func:`~...spine.agent_discovery.discover_surfaces_via_agent`
        is target-agnostic and never raises). Offline — no runner, or the runner
        unavailable — this returns nothing rather than a fabricated candidate list:
        the loop then quiesces honestly instead of burning cycles on invented targets.
        """
        if agent_runner is None:
            return DiscoveryResult(candidates=[], notes="no agent runner wired — offline, no seeds")
        surfaces = agent_discovery.discover_surfaces_via_agent(
            agent_runner,
            clone=self.clone_path,
            scope_base=self.scope_base,
            scope=self._scope,
            log_dir=self._log_dir,
            logger=logger,
            skip_targets=list(self._skip_targets or []),
            rotate=int(self._discovery_rotate or 0),
            # Focus UNSCOPED reads on the fixable region: without a diff scope, discovery
            # would read the whole tree while the fence confines fixes to these globs, so it
            # finds nothing fixable and returns []. Only a USER-NARROWED allowlist steers
            # reads (the repo-wide default is None here → no-op, agent reads the whole tree
            # as before). See allowlisted_py_files.
            edit_globs=self._user_edit_globs,
        )
        candidates = [self._candidate_from(s) for s in surfaces]
        return DiscoveryResult(
            candidates=candidates,
            notes=(
                f"{len(candidates)} agent surface(s); "
                # `is None` — an EMPTY set is a real scope ("nothing"), not "repo". Truthiness
                # here would have relabelled the empty-diff case as unscoped in the log while
                # the gate correctly enforced a fence of zero files.
                f"scope={'repo' if self._scope is None else 'diff'}"
            ),
        )

    def _test_dir(self) -> str:
        """This repo's DOMINANT test directory — ``tests`` or ``test``.

        Not just "the first one that exists": a repo can have BOTH (Kiro Crew has 776
        ``test_*.py`` under ``test/`` and 9 under ``tests/``), and writing the reproducing
        test into the minor one puts it outside the suite the gate actually runs. Chooses
        by file count, so the answer follows where the tests really are; falls back to
        ``test`` when neither exists.
        """
        counts = {
            name: len(list((self.clone_path / name).glob("test_*.py")))
            for name in ("test", "tests")
            if (self.clone_path / name).is_dir()
        }
        if not counts:
            return "test"
        # max() on (count, name) — a deterministic tie-break, never dict order.
        return max(counts, key=lambda n: (counts[n], n))

    def _candidate_from(self, surface: dict) -> Candidate:
        """Turn one discovery surface dict into a track-appropriate candidate.

        For the bug track we do NOT invent a nodeid: the reproducing test does not
        exist yet, and naming a file the agent has not written would fail T2 collection
        and be recorded as ``test_invalid``. We name only the SHAPE the agent is
        instructed to produce (``<testdir>/test_bug_<slug>.py``), which is the same shape
        the edit fence permits — so what discovery asks for, the agent is told to
        write, and the fence allows are all one thing.

        ``<testdir>`` is THIS repo's actual test directory (``tests`` or ``test``). It was
        hard-coded to ``test``, so a repo using ``tests/`` had the declared path point at a
        directory that does not exist and T2 could never collect — every candidate failed
        ``test_invalid``. Found running the integration plan against Zedmor/chess_test.
        """
        target = str(surface.get("target") or surface.get("file") or "unknown")
        hypothesis = str(surface.get("hypothesis") or surface.get("message") or "")
        if self.track != TRACK_BUG:
            return Candidate(
                kind=TRACK_PERF,
                target=target,
                signature=str(surface.get("message") or "")[:200],
                hypothesis=hypothesis,
                evidence=str(surface.get("rule") or ""),
                scenario="suite",
                confidence=0.5,
            )
        slug = re.sub(r"[^a-z0-9]+", "_", target.lower()).strip("_")[:40] or "surface"
        test_path = f"{self._test_dir()}/test_bug_{slug}.py"
        return Candidate(
            kind=TRACK_BUG,
            target=target,
            signature=str(surface.get("message") or "")[:200],
            hypothesis=hypothesis,
            evidence=str(surface.get("rule") or ""),
            confidence=0.5,
            reproducing_test=BugReproducingTest(
                test_id=test_path,
                test_path=test_path,
                added_by_candidate=True,
            ),
        )

    # ── Phase B: proposal ────────────────────────────────────────────────────

    def propose(self, *, candidate: Candidate, base_sha: str, worktree: Path, tier: str) -> bool:
        """Phase B — realize one candidate's edit. Returns False, always, by design.

        There is no mechanical transformation to apply: a repo candidate is a
        hypothesis about code the agent must read and change. Returning False is the
        contract's way of saying "no mechanical seed" — the proposer then escalates to
        the model, calling :func:`~...spine.agent_runner.author_bug_fix` for a bug
        candidate and :func:`~...spine.agent_runner.author_perf_fix` for a perf one when
        a runner is wired, and drops the candidate as an honest ``no_defect`` when one is
        not. Fabricating an edit here would produce a diff the gate would have to reject.

        Upstream shipped ~24 hand-written mechanical perf seeds PER TARGET (its two
        profiles optimized one specific service's own code). A target-agnostic profile
        cannot ship those for an arbitrary repo, so the perf edit is authored by the
        model and judged by the spine's A/B measurement against the calibrated noise
        band — the same "model proposes, deterministic gate disposes" split the bug
        track uses.
        """
        return False

    # ── observability: the profiler data path ────────────────────────────────

    def capture_profile(self, *, fp: str, worktree: Path) -> dict | None:
        """Profile the suite in ``worktree`` and normalize it under ``fp``.

        Feeds ``GET /profiles`` and ``GET /profile/{fp}``. Both endpoints and the whole
        pstats/.cpuprofile normalizer already existed, but NOTHING ever called a capture,
        so the profiler views were permanently empty — a shipped surface with no data
        path. The driver calls this per perf candidate, deliberately OUTSIDE any timed
        arm (cProfile's overhead is exactly the variance the noise band excludes).

        Runs ``python -m cProfile -o <out>.pstats -m pytest`` over the SAME suite scope
        the gate uses, so a monorepo profiles its edit region rather than 21k unrelated
        tests. Returns the normalized tree, or None on any failure — profiling is
        observability and must never fail a run or block a measured candidate.
        """
        from ...backend import profile_normalize as PN
        from ...backend import store

        # ``worktree`` is the worktree ROOT (the driver passes proposal.worktree), so
        # hand _repo_root the ``<tree>/src`` shape it expects — but only when that dir
        # exists. Appending "src" unconditionally produced ``<wt>/src/src`` for a repo
        # that already IS the run root, and pytest then found nothing to profile.
        inner = Path(worktree) / "src"
        root = _repo_root(inner if inner.is_dir() else Path(worktree))
        try:
            raw = store.profiles_dir() / f"{fp}.pstats"
            raw.parent.mkdir(parents=True, exist_ok=True)
            argv = [
                sys.executable,
                "-m",
                "cProfile",
                "-o",
                str(raw),
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "--color=no",
                "-o",
                "addopts=",
                "-q",
                # NO xdist: workers profile their own subprocesses, so the parent's
                # pstats file would be near-empty. Serial is correct here, and the
                # scope keeps it affordable.
                *self.suite_scope_for_profiling,
            ]
            _run(argv, cwd=root, timeout=_SUITE_TIMEOUT_S, env=_measure_env(root))
            if not raw.is_file() or raw.stat().st_size == 0:
                return None
            return PN.capture_profile(fp, raw, scenario="suite")
        except (OSError, subprocess.SubprocessError):
            return None

    @property
    def suite_scope_for_profiling(self) -> list[str]:
        """The gate's suite scope, reused so profiling covers the same tests."""
        return list(getattr(self.bug_runner, "suite_scope", []) or [])


# ── the factory ─────────────────────────────────────────────────────────────


def _resolve_origin_url(cfg: dict) -> str:
    """The real remote for the PR recipe, migration-safe. See
    ``backend/clone_setup.resolve_origin_url`` for why the legacy fallback re-validates."""
    from ...backend.clone_setup import resolve_origin_url

    return resolve_origin_url(cfg)


def build_profile(config: dict) -> GitHubRepoProfile:
    """Assemble a :class:`GitHubRepoProfile` from the app's on-disk config.

    Reads the same keys the routes write (``clone``, ``branch``, ``scopeDiffBase``,
    ``benchmarkCommand``, ``calibrationReps``, ``noiseFloorSeconds``, ``track``) and
    resolves the queue/log dirs from :mod:`...backend.store`, so a caller only has to
    hand over the config dict. Raises :class:`ValueError` when no clone is configured
    — a profile pointed at nothing would fail later and less legibly.
    """
    from ...backend import store

    cfg = config or {}
    clone = str(cfg.get("clone") or "").strip()
    if not clone:
        raise ValueError("no repository configured — run setup-clone first")

    branch = str(cfg.get("branch") or "").strip()
    # ``branch`` names the BASE the run forks off. A bare name is normalized to the
    # remote-tracking ref so a fresh clone (which has only origin/* refs for branches
    # it did not check out) can still resolve it.
    if not branch:
        base_ref = "origin/main"
    elif "/" in branch:
        base_ref = branch
    else:
        base_ref = f"origin/{branch}"

    return GitHubRepoProfile(
        clone_path=Path(clone),
        pr_queue_dir=store.pr_queue_dir(),
        user=str(cfg.get("prUser") or cfg.get("user") or ""),
        base_ref=base_ref,
        track=str(cfg.get("track") or TRACK_BUG),
        benchmark_cmd=str(cfg.get("benchmarkCommand") or ""),
        scope_base=str(cfg.get("scopeDiffBase") or ""),
        origin_url=_resolve_origin_url(cfg),
        allowed_globs=(
            [str(g) for g in cfg["editAllowlist"]]
            if isinstance(cfg.get("editAllowlist"), list) and cfg["editAllowlist"]
            else None
        ),
        baseline_reps=int(cfg.get("calibrationReps") or 5),
        noise_floor_s=float(cfg.get("noiseFloorSeconds") or 0.25),
        log_dir=store.logs_dir(),
    )
