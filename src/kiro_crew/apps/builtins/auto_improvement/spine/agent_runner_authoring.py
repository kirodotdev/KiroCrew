"""The spine's fix-authoring step: drive the agent to write a bug or perf fix.

``author_bug_fix`` and ``author_perf_fix`` give the agent one bounded prompt for a
candidate, run it through the runner the spine passes (anything with
``run(prompt, ...)``), and then read the worktree, never the agent's prose, to decide
whether a change is left for the deterministic gate. ``_adopt_authored_test`` points the
candidate at the reproducing test the agent actually wrote, and ``_repro_test_dir`` names
the directory the repository keeps its tests in. The host-side ``git status`` presence
check runs under ``_GIT_SAFE_CONFIG``; a run the backend cut off begins its error with
``_UNFINISHED_TURN_ERROR`` and is harvested like a timeout.

``kiro_crew.apps.builtins.auto_improvement.spine.agent_runner`` serves every name here and
is the import path callers use; a patch through it reaches this module
(``kiro_crew._facade_mirror``).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from kiro_crew.subprocess_utf8 import UTF8_TEXT

from .git_safety import GIT_SAFE_CONFIG, hook_off_args, require_pinned

#: The facade's logger, named from this package so the app loader's copy shares it too.
logger = logging.getLogger(f"{__package__}.agent_runner")

#: Trusted git config for host-side git run over the agent-writable worktree — identical to the
#: driver/gate/commit `_GIT_SAFE_CONFIG`. The POST-AGENT presence checks below run `git status`
#: on the HOST as the gateway user, in the tree the sandboxed agent just wrote to. `git status`
#: consults `core.fsmonitor` and can SPAWN it, so an agent that set `core.fsmonitor` to a program
#: (or planted a hook via `core.hooksPath`) would get host-side, out-of-sandbox execution on the
#: next status. `-c` overrides on OUR argv beat the repo config.
_GIT_SAFE_CONFIG = GIT_SAFE_CONFIG

#: How the error of a turn the backend ended unfinished begins (see ``_run_async``). Bug and
#: perf authoring read it as a bounded exit, like ``timeout after Ns``: the run was cut off,
#: so an edit already on disk still goes to the deterministic gate.
_UNFINISHED_TURN_ERROR = "the backend did not finish the turn"


def _repro_test_dir(worktree: Path) -> str:
    """The directory this repo actually keeps tests in — ``tests`` or ``test``.

    Hard-coding ``test/`` in the prompt breaks a repo that keeps its tests in ``tests/``
    (plural): the reproducing test lands in a directory that does not exist, so T2 cannot
    collect it and EVERY candidate fails ``test_invalid`` regardless of fix quality.

    The edit fence permits both (``_ADDABLE_TEST_GLOBS``), so the directory only has to be
    named correctly here. Prefers an EXISTING directory; falls back to ``test``.
    """
    counts = {
        name: len(list((Path(worktree) / name).glob("test_*.py")))
        for name in ("test", "tests")
        if (Path(worktree) / name).is_dir()
    }
    if not counts:
        return "test"
    # By file count, not alphabetical: a repo can have BOTH, and the minor one is
    # outside the suite the gate runs. Deterministic tie-break on (count, name).
    return max(counts, key=lambda n: (counts[n], n))


def author_bug_fix(
    # ``Any``, not ``AgentRunner``: the runner is DUCK-TYPED — anything exposing
    # ``run(prompt, ...) -> AgentResult`` qualifies, and the in-process
    # ``SessionAgentRunner`` (the default path) is deliberately NOT a subclass. The
    # narrower annotation was simply wrong about what the spine passes.
    runner: Any,
    *,
    candidate,
    worktree: Path,
    test_cmd_hint: str | None = None,
) -> bool:
    """Drive the agent to author a bug fix (reproducing test + minimal source edit) in
    ``worktree``. Returns True iff the agent left a real diff. Mirrors the original
    framework's ``implement()`` prompt shape (05_*.md §1.3: the agent writes the RED
    test + the fix; the spine's RED→GREEN gate then verifies the boolean transition).

    SAFETY: the agent edits inside the throwaway worktree only; the spine's edit
    allowlist + RED→GREEN gate + push-disabled clone contain the blast radius. We do
    NOT trust the agent's word that it fixed anything — the deterministic gate decides.

    ``test_cmd_hint`` is the exact, ready-to-run test command the gate itself uses
    (same interpreter + flags + PYTHONPATH). Handing it to the agent removes the
    ~20-minute environment hunt seen in practice — the agent should NOT rediscover
    which interpreter has the deps / why a plugin won't load; the gate already knows.
    """
    tdir = _repro_test_dir(worktree)
    rt = getattr(candidate, "reproducing_test", None)
    test_hint = ""
    if rt is not None:
        test_hint = (
            f"\n  suggested reproducing test id: {getattr(rt, 'test_id', '')}"
            f"\n  it must FAIL on the current (buggy) code and PASS after your fix."
        )
    # The known-good test invocation. Without this the agent burns ~20 min per candidate
    # hunting for a working interpreter; with it, it runs tests in seconds.
    run_block = ""
    if test_cmd_hint:
        run_block = (
            "\nHOW TO RUN TESTS (use EXACTLY this — do not hunt for another interpreter, "
            "do not install plugins, do not switch Python; this command already has every "
            "dependency the suite needs):\n"
            f"  {test_cmd_hint}\n"
            "Replace <test_path> with the test file (or file::case) you are running.\n"
        )
    prompt = (
        "You are the implementation step of an autonomous Kiro Crew BUG-fix loop.\n"
        f"Working dir is an isolated git worktree: {worktree}\n"
        "Investigate this candidate defect surface and, ONLY IF a real defect exists, "
        "fix it:\n"
        f"  defect surface: {candidate.target}\n"
        f"  signature: {candidate.signature}\n"
        f"  hypothesis: {candidate.hypothesis}{test_hint}\n"
        f"{run_block}\n"
        "Procedure (keep it BOUNDED — do not exceed a few investigation steps):\n"
        "  1. Read the named surface and write a MINIMAL deterministic test that would "
        "reproduce the hypothesised defect.\n"
        f"     NAME THE TEST FILE ``{tdir}/test_bug_<short_slug>.py`` (it MUST start with "
        f"``{tdir}/test_bug_`` — e.g. ``{tdir}/test_bug_format_schedule_tz.py``). This "
        "exact naming is REQUIRED: the edit fence permits a NEW reproducing test only "
        f"under ``{tdir}/test_bug_*.py``, and ``{tdir}`` is the directory THIS repo "
        "actually keeps its tests in — writing to any other directory means the gate "
        "cannot collect your test and the whole fix is rejected unmeasured. Any other "
        "path is treated as editing the existing suite. Do NOT add to or modify an "
        "existing test file — create this new one.\n"
        "  2. RUN that test against the CURRENT (unmodified) code FIRST, BEFORE editing "
        "any source.\n"
        "     - If it FAILS (defect confirmed): apply the smallest source fix that makes "
        "it pass, preserving all other behavior, then re-run to confirm GREEN.\n"
        "     - If it PASSES on the current code (the hypothesis does NOT reproduce — the "
        "code already handles this case): the surface has NO real defect. STOP "
        "IMMEDIATELY. Make NO source or test edits, revert anything you wrote, and reply "
        "with exactly: NO DEFECT FOUND — <one-line reason>.\n"
        "CRITICAL — the reproducing test is re-run by a downstream gate against the "
        "ORIGINAL un-fixed code, where it MUST COLLECT and FAIL (this is what proves the "
        "bug is real). So the test must NOT depend on ANYTHING your fix introduces:\n"
        "  • do not import, reference, monkeypatch, or attribute-access any name/symbol/"
        "import that only EXISTS after your fix (e.g. patching ``module.datetime`` when "
        "the buggy code has no ``datetime`` import yet — that is a COLLECTION error on the "
        "original code, not a failure, and the gate will reject your fix as 'cannot be "
        "RED').\n"
        "  • patch/observe only symbols ALREADY present in the unmodified module; assert "
        "on OUTPUT/behavior, not on the fix's internal helpers.\n"
        "  • if you used ``monkeypatch.setattr(mod, 'X', ...)`` make sure ``mod.X`` exists "
        "BEFORE your edit (use ``raising=True``, the default — if it errors on the base "
        "tree your test is collection-coupled to the fix and is INVALID).\n"
        "  After writing the test, VERIFY this yourself: it must import/collect and FAIL "
        "on the current code with NO edits applied.\n"
        "Do NOT fabricate a fix or a passing test for a defect you could not reproduce — "
        "an honest 'NO DEFECT FOUND' is a correct, valued outcome and costs nothing "
        "downstream (the candidate is simply skipped). A made-up fix wastes a human "
        "reviewer's time and is worse than no fix.\n"
        "Constraints: edit only what a confirmed fix + its test require; keep it minimal "
        "and reviewable; do NOT touch build config or auth/security code.\n"
        "BEFORE YOU FINISH — the T1 gate rejects the whole candidate if your diff adds ANY "
        "new lint finding, INCLUDING one in the test you just wrote. This is the single "
        "most common way a correct, fully-verified fix gets thrown away (observed live: a "
        "real fix lost to one unused ``import pytest``). So lint what you changed and "
        "clean it up:\n"
        "  • run ``ruff check --output-format=concise <files you touched>`` if ruff is "
        "available, else ``python -m pyflakes <files>``;\n"
        "  • remove unused imports and unused local variables — do NOT silence them with "
        "``# noqa``, which reads as gaming the gate;\n"
        "  • only findings YOUR diff introduced matter; the repo's pre-existing ones are "
        "not yours to fix and must be left alone.\n"
        "When you DO fix, reply with a one-line summary of the edit."
    )
    role_runner = runner.for_role("implementation") if hasattr(runner, "for_role") else runner
    res = role_runner.run(
        prompt,
        cwd=str(worktree),
        allowed_tools=["Bash", "Read", "Edit", "Write", "Grep", "Glob"],
        # 10-min cap (was 1800s/30min). In practice the agent finishes the real work —
        # write reproducing test, apply minimal fix, confirm GREEN — in well under 10
        # minutes, then tends to OVER-iterate (re-verifying a fix that is already GREEN)
        # instead of returning. The worktree already holds the finished fix when the cap
        # fires, so the deterministic RED→GREEN gate (which is the real verdict) still
        # evaluates the completed work — a shorter cap just stops paying for the agent's
        # redundant self-checking and keeps cycle throughput up.
        max_turns=40,
        timeout_s=600,
    )
    # The worktree git state — NOT res.ok — is the source of truth. A TIMEOUT or a
    # MAX_TURNS cap is expected here (the agent often finishes the fix, then over-iterates
    # until the cap fires): the completed fix is already on disk, so we still harvest it and
    # let the deterministic RED→GREEN gate decide. Both are the runner's bounded "expected"
    # exits — the session path returns ``max_turns (N) reached`` for the ACP turn cap, the
    # subprocess path returns ``timeout after Ns`` — so they are treated identically. A turn
    # the backend ended unfinished (its own prompt timeout, a stale_recover, a stream that
    # ended first) is cut off the same way, so it is harvested too. Only a
    # genuine failure (provider died, agent binary missing, explicit stop) means there's
    # nothing trustworthy to harvest — bail so we don't gate a half-written tree.
    err = (res.error or "").lower()
    is_bounded_exit = (
        "timeout after" in err or "max_turns" in err or err.startswith(_UNFINISHED_TURN_ERROR)
    )
    if not res.ok and not is_bounded_exit:
        return False
    # Require an ACTUAL change — the agent's prose is not trusted; the worktree state is.
    # A "NO DEFECT FOUND" exit leaves the tree clean → no change → candidate skipped.
    #
    # We must detect ALL of: modified tracked files, NEW untracked files (the reproducing
    # test is always a new file), AND staged changes (the agent may ``git add`` its work
    # while running base-relative verification — observed live: an agent stashed/staged
    # during its own STAYGREEN check, leaving the fix staged). A bare ``git diff --stat``
    # sees ONLY unstaged tracked modifications and silently MISSED both the new test and
    # any staged fix → a real fix was dropped as "no diff produced". ``git status
    # --porcelain`` reports every change kind (modified, added, staged, untracked), so it
    # is the correct presence check.
    require_pinned(worktree)
    where = str(Path(worktree).absolute())
    st = subprocess.run(
        ["git", "-C", where, *_GIT_SAFE_CONFIG, *hook_off_args(where), "status", "--porcelain"],
        capture_output=True,
        cwd=where,
        **UTF8_TEXT,
    )
    if not st.stdout.strip():
        return False
    # RECONCILE the candidate's reproducing-test path to the file the agent ACTUALLY
    # wrote. The candidate arrives carrying a path the spine INVENTED from the target
    # slug (profile._candidate_from), but the prompt tells the agent to choose its own
    # ``test/test_bug_<short_slug>.py``. Those names almost never match, and the invented
    # one also COLLIDES: the slug is the target path lowercased and truncated to 40 chars,
    # so two loci sharing a long directory prefix produce the SAME filename. The gate then
    # ran ``pytest --collect-only`` on a path that does not exist, so T2 failed
    # ("reproducing test does not collect") for every candidate no matter how good the fix
    # — observed live: two independent, verified RED→GREEN fixes both rejected. Point the
    # candidate at the real file so the gate verifies the test that was actually authored.
    _adopt_authored_test(candidate, worktree, st.stdout)
    return True


def _adopt_authored_test(candidate, worktree: Path, porcelain: str) -> None:
    """Retarget ``candidate.reproducing_test`` at the repro test the agent added.

    Best-effort and non-fatal: if no added ``test_bug_*.py`` is found we leave the
    candidate untouched and let the gate judge what is there. Never raises — a
    reconciliation failure must not lose an otherwise-good fix.
    """
    rt = getattr(candidate, "reproducing_test", None)
    if rt is None:
        return
    try:
        # Porcelain fields: XY <path>, with '-> ' for renames and quoting when needed.
        added: list[str] = []
        for line in porcelain.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if " -> " in path:  # rename/copy: take the destination
                path = path.split(" -> ", 1)[1].strip()
            path = path.strip('"')
            name = Path(path).name
            if name.startswith("test_bug_") and name.endswith(".py"):
                added.append(path)
        if not added:
            return
        # Deterministic pick when the agent wrote more than one: the shortest path,
        # then lexicographic — stable across runs so a re-run gates the same file.
        chosen = sorted(added, key=lambda p: (len(p), p))[0]
        if not (worktree / chosen).is_file():
            return
        current = str(getattr(rt, "test_path", "") or "")
        if current == chosen:
            return
        # test_id doubles as the pytest nodeid for the RED/GREEN runs; keep any
        # ``::case`` selector the spine already had only if it named THIS file.
        for attr, value in (("test_path", chosen), ("test_id", chosen)):
            try:
                setattr(rt, attr, value)
            except Exception:  # noqa: BLE001 — frozen/slotted dataclass: nothing to do
                return
        logger.info(
            "author_bug_fix: adopted authored repro test %s (was %s)", chosen, current or "<unset>"
        )
    except Exception:  # noqa: BLE001 — never lose a fix over path reconciliation
        logger.warning("author_bug_fix: could not reconcile repro test path", exc_info=True)


def author_perf_fix(
    runner: Any,
    *,
    candidate,
    worktree: Path,
    test_cmd_hint: str | None = None,
) -> bool:
    """Drive the agent to author a BEHAVIOR-PRESERVING speedup in ``worktree``.

    The perf-track counterpart of :func:`author_bug_fix`, and the reason the track was
    dead: :meth:`GitHubRepoProfile.propose` returns False for every candidate ("no
    mechanical seed"), and the proposer only escalated to an agent for ``TRACK_BUG`` —
    so a perf candidate produced no diff, was recorded ``no_defect``, and the loop could
    never keep, measure, or file a perf win. Upstream had 24 hand-written mechanical
    seeds per target; a target-agnostic profile cannot ship those, so the agent authors
    the edit and the spine's A/B measurement decides — the same division of labour the
    bug track already uses (the model proposes, the deterministic gate disposes).

    Returns True iff the agent left a real diff. Never trusts the agent's claim of a
    speedup: the keeper requires the measured delta to clear the calibrated noise band,
    reproduced by a SECOND independent A/B, with the suite-still-passes guardrail and the
    test-count reward-hack guard both holding. So an imagined win costs one bounded
    cycle, never a fabricated keep.

    SAFETY: edits land only in the throwaway worktree; the edit allowlist forbids the
    tests-of-record (the ruler's own measurement subject) and the build/CI config, and
    the clone cannot push.
    """
    run_block = ""
    if test_cmd_hint:
        run_block = (
            "\nHOW TO RUN TESTS (use EXACTLY this — it already has every dependency; do "
            "not hunt for another interpreter):\n"
            f"  {test_cmd_hint}\n"
        )
    prompt = (
        "You are the implementation step of an autonomous PERFORMANCE loop.\n"
        f"Working dir is an isolated git worktree: {worktree}\n"
        "Make ONE behavior-preserving change that makes this locus measurably faster:\n"
        f"  target: {candidate.target}\n"
        f"  observation: {candidate.signature}\n"
        f"  hypothesis: {candidate.hypothesis}\n"
        f"{run_block}\n"
        "WHAT COUNTS as behavior-preserving: identical observable outputs, return "
        "shapes, exceptions, and side effects for every input the code already handles. "
        "Good moves: hoist repeated work out of a loop, avoid re-reading/re-parsing the "
        "same data, replace an O(n^2) scan with a dict/set lookup, cache a pure "
        "computation, drop a redundant copy, avoid building a large intermediate list. "
        "Do NOT change an algorithm's RESULT, relax a check, or trade correctness for "
        "speed.\n\n"
        "HARD CONSTRAINTS — a violation wastes the whole cycle because the gate rejects "
        "it mechanically:\n"
        "  • Do NOT touch any test file, conftest, or the build/CI/dependency config. "
        "The test suite is the RULER that measures you; editing it is metric gaming and "
        "is auto-rejected. A test-count change trips the reward-hack guard.\n"
        "  • Do NOT delete, skip, or weaken tests to go faster.\n"
        "  • Keep the diff MINIMAL and reviewable — one locus, not a refactor.\n"
        "  • Run the suite before you finish; it MUST still pass (that is a guardrail).\n"
        "  • LINT what you changed before finishing: the T1 gate rejects the candidate for ANY new lint finding your diff adds (``ruff check --output-format=concise <files>``, else ``python -m pyflakes``). Remove unused imports/variables rather than silencing them with ``# noqa``; the repo's pre-existing findings are not yours to fix.\n\n"
        "You do NOT decide whether this is kept. The spine measures your edit A/B against "
        "the unmodified base, requires the win to clear a calibrated noise band, and "
        "reproduces it independently. So do NOT report an estimated speedup as a fact — "
        "state what you changed and why it should be faster; the measurement is not "
        "yours to make.\n"
        "If, after reading the code, there is NO behavior-preserving win here, make NO "
        "edits and reply with exactly: NO WIN FOUND — <one-line reason>. That is a "
        "correct, valued outcome and costs nothing downstream; a fabricated 'optimization' "
        "wastes a reviewer's time and pollutes the measurement record.\n"
        "When you DO change something, reply with a one-line summary of the edit."
    )
    role_runner = runner.for_role("implementation") if hasattr(runner, "for_role") else runner
    res = role_runner.run(
        prompt,
        cwd=str(worktree),
        allowed_tools=["Bash", "Read", "Edit", "Write", "Grep", "Glob"],
        max_turns=40,
        timeout_s=600,
    )
    # Same harvest rule as the bug track: the WORKTREE, not the agent's prose, is the
    # source of truth, and a bounded exit (timeout / max_turns / a turn the backend ended
    # unfinished) is an EXPECTED outcome whose finished work is already on disk. Only a
    # genuine runner failure means there is nothing trustworthy to measure.
    err = (res.error or "").lower()
    is_bounded_exit = (
        "timeout after" in err or "max_turns" in err or err.startswith(_UNFINISHED_TURN_ERROR)
    )
    if not res.ok and not is_bounded_exit:
        return False
    require_pinned(worktree)
    # ``cwd=`` beside ``-C``, the same absolute path for both: the child's working
    # directory must be the worktree, not whatever the gateway inherited.
    where = str(Path(worktree).absolute())
    st = subprocess.run(
        ["git", "-C", where, *_GIT_SAFE_CONFIG, *hook_off_args(where), "status", "--porcelain"],
        capture_output=True,
        cwd=where,
        **UTF8_TEXT,
    )
    return bool(st.stdout.strip())
