#!/usr/bin/env python3
"""AcceptSpec evaluator - the deterministic half of the conductor's patrol.

The conductor NEVER judges whether a work item succeeded; this script does,
and the conductor only reads its verdicts. Script-first: the decision is an
exit code and a JSON document, not a model's impression of a transcript.

Usage:
    python3 accept_eval.py < items.json
    python3 accept_eval.py --help      # this block, on stderr, exit 2

The input arrives on STDIN. Invoked with nothing piped in (a terminal) or with
``-h``/``--help``, this script prints this block and exits 2 instead of
blocking on a read that would look like a hang.

stdin (JSON):
    {"items": [
        {"id": "item-1", "accept": {"kind": "pr_checks", "pr": 123,
                                     "repo": "owner/name"}},
        {"id": "item-2", "accept": {"kind": "file", "path": "/abs/path",
                                     "exists": true}},
        {"id": "item-3", "accept": {"kind": "human_approval"}}
    ]}

stdout (JSON):
    {"results": [{"id": "...", "verdict": "pass|fail|pending|refused|error",
                  "evidence": "..."}]}

Exit code: 0 when evaluation ran (verdicts carry the outcome); 2 on malformed
input. A per-item problem is a verdict, never a crash - one bad spec must not
hide the others' results.

THE SECURITY INVARIANT (deliberate, do not weaken):

    No model-authored argv ever reaches subprocess.

A spec is written by a model, and this script runs as an approved wrapper, so a
spec that could name a command would make this script a general way to run one.
That is not a hypothetical: an earlier revision accepted a generic ``cmd`` kind
with an allowlist, and it took three review rounds to establish that no
constraint on ``argv`` fixes the shape. In order, the allowlist was defeated by
(1) bare interpreters on it, so ``python -c <payload>`` ran anything;
(2) a basename-only check, so ``/tmp/git`` executed an arbitrary binary under an
allowlisted name; and (3) plain ``git reset --hard`` - allowlisted, and
destructive. (3) is the one that settles it. Kiro Crew's denied-command floor
inspects the ``execute_bash`` command string, which here reads
``python3 .../accept_eval.py``; the real argv arrives on stdin and is executed
with ``shell=False``, so a denied command never appears in any string the hook
examines. A generic executor behind an approved wrapper therefore REMOVES the
deny floor for whatever it accepts, and the bypass is the wrapper rather than
the list - which is why the list was dropped and not tightened again.

So every argv this script runs is built HERE, from a fixed template, out of
narrowly-typed spec fields. ``_SELF_BUILT_COMMANDS`` is the internal assertion
that this stayed true: it is not a model-facing allowlist, it is a fail-closed
check that a handler did not start passing through something it was given.

HOW TO WIDEN IT (the supported path):

    Add a new ``kind`` whose handler builds its own argv. Never re-introduce a
    kind that takes a command, an argv array, or a shell string from the spec.

A ``pytest`` kind would take test paths and build ``["pytest", *paths]``; an
``npm_script`` kind would take a script name and build ``["npm", "run", name]``.
Each such addition is a deliberate, reviewable decision about one more binary,
and it keeps the invariant above intact by construction. Start narrow and add
kinds as real work items need them.

Other properties:
- No shell. argv arrays only, subprocess.run(shell=False).
- Per-check timeout (TIMEOUT_SECS). A hung check is an "error" verdict.
- Evidence is tail-capped so a chatty check cannot flood the conductor's turn.
- A ``refused`` verdict is one the conductor must surface to the user, never
  retry around.
- ``pr_checks`` that comes back PENDING on a DRAFT pull request is ``refused``,
  not waited on. A draft is the author's own statement that the PR is not ready
  for review, so an unfinished check run on one is a person's turn, not a
  machine's. This deliberately covers a draft whose checks are merely still
  running: the evaluator holds no state across cycles and cannot tell that apart
  from a readiness check gated on the draft flag, which never resolves - and the
  remedy is the same either way, and one the author owes anyway. A draft whose
  checks have RESOLVED is judged on them like any other PR (a green draft
  passes).

Stdlib-only, Python 3.8+.
"""

import json
import subprocess
import sys
from pathlib import Path

#: Binaries this script itself invokes, from argv it builds. NOT a spec-facing
#: allowlist - nothing in a spec can name a command at all. This exists so a
#: handler that ever passes spec input into `_exec` fails closed instead of
#: executing it, which is the regression the `cmd` kind's removal prevents.
#: Both invocations here are `gh` (`pr checks` and the `pr view` draft probe),
#: so a second entry would widen the surface for nothing.
_SELF_BUILT_COMMANDS = {"gh"}

TIMEOUT_SECS = 300
EVIDENCE_TAIL_CHARS = 500

#: gh pr checks exits 8 when checks are still running (gh >= 2.30).
_GH_PENDING_EXIT = 8


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text[-EVIDENCE_TAIL_CHARS:] if len(text) > EVIDENCE_TAIL_CHARS else text


def _exec(argv, cwd=None):
    """Run one SCRIPT-BUILT argv without a shell; return ``(problem, proc)``.

    Exactly one half is not ``None``. ``problem`` is a ready
    ``(verdict, evidence)`` pair for the cases where no process ran at all.

    Every caller must pass an argv it constructed itself. The guard below is an
    internal invariant, not a spec gate: reaching it with something outside
    ``_SELF_BUILT_COMMANDS`` means a handler leaked spec input into an exec path,
    so it refuses rather than runs. It lives HERE, in the one exec seam, which
    is why adding a second invocation did not add a second way past it.
    """
    raw = str(argv[0])
    if raw not in _SELF_BUILT_COMMANDS:
        return (
            (
                "refused",
                f"evaluator bug: {raw!r} is not a command this script builds; "
                "no spec field may name a command",
            ),
            None,
        )
    try:
        proc = subprocess.run(  # noqa: S603 - argv array, no shell, script-built
            [str(a) for a in argv],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            # Pin the decode instead of inheriting the locale: on Windows the
            # default is the ANSI code page, which mangles a check's UTF-8
            # output, and `errors="replace"` keeps genuinely undecodable bytes
            # from raising - a check's OUTPUT must never turn its verdict into a
            # crash, since the evidence is only ever read by a human.
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        return (("error", f"timed out after {TIMEOUT_SECS}s"), None)
    except FileNotFoundError:
        return (("error", f"{raw!r} not found on PATH"), None)
    except OSError as exc:
        return (("error", f"could not run: {exc}"), None)
    return (None, proc)


def _run(argv, cwd=None):
    """Run one SCRIPT-BUILT check and map its exit into a verdict."""
    raw = str(argv[0])
    problem, proc = _exec(argv, cwd)
    if proc is None:
        return problem or ("error", "could not run")
    output = _tail(proc.stdout + "\n" + proc.stderr)
    if proc.returncode == 0:
        return ("pass", output or "exit 0")
    if raw == "gh" and proc.returncode == _GH_PENDING_EXIT:
        return ("pending", output or "checks still running")
    return ("fail", f"exit {proc.returncode}: {output}")


def _pr_is_draft(pr, repo=None):
    """Is this pull request still a draft? ``True`` only on an unambiguous yes.

    Asked ONLY to explain a check run that came back pending - see the
    ``pr_checks`` handler for why the order matters.

    The probe is deliberately one-sided. Anything other than a clean ``true`` -
    gh absent, no such PR, an auth failure, a forge without the field - returns
    ``False``, and the pending verdict then stands exactly as it did before, so
    a probe that cannot answer never invents a refusal.
    """
    argv = ["gh", "pr", "view", str(pr)]
    if repo:
        argv += ["--repo", str(repo)]
    argv += ["--json", "isDraft", "-q", ".isDraft"]
    problem, proc = _exec(argv)
    if problem is not None or proc is None or proc.returncode != 0:
        return False
    return proc.stdout.strip().lower() == "true"


def _evaluate(item):
    accept = item.get("accept") or {}
    kind = accept.get("kind")
    if kind == "pr_checks":
        pr = accept.get("pr")
        # bool is an int subclass, and `{"pr": true}` must not become `gh pr
        # checks True`; reject it with the same message as any other non-int.
        if not isinstance(pr, int) or isinstance(pr, bool):
            return ("error", "pr_checks spec needs an integer pr")
        repo = accept.get("repo")
        argv = ["gh", "pr", "checks", str(pr)]
        if repo:
            argv += ["--repo", str(repo)]
        verdict, evidence = _run(argv)
        # The draft probe runs HERE, behind a pending verdict, and nowhere else.
        #
        # A draft PR is not unconditionally unsatisfiable, so the probe must not
        # pre-empt the check: where a repo has no aggregate check gated on the
        # draft flag, a draft's checks resolve and `gh pr checks` exits 0 - a
        # `pass` this script keeps returning, since this skill ships to
        # arbitrary repos. Probing only a pending run also costs nothing on the
        # pass and fail paths, which is every cycle that was never going to
        # loop.
        #
        # A PENDING draft is refused on the spec, not on a prediction about CI.
        # The two ways a draft's checks stay pending - a readiness check gated
        # on the draft flag, which never resolves, and ordinary checks still
        # running, which do - are indistinguishable to a stateless evaluator,
        # and this returns the same answer for both ON PURPOSE. A draft is the
        # author's own "not ready for review", so an unfinished check run on one
        # is a person's turn; "mark it ready for review" is a step they owe
        # regardless, and it is bounded. Waiting instead is not: guessing wrong
        # in that direction is the reported defect, an hour of polling a
        # condition no cycle could change.
        if verdict == "pending" and _pr_is_draft(pr, repo):
            return (
                "refused",
                f"PR #{pr} is a draft and its checks have not finished; a draft "
                "is not a review-ready PR, and where readiness is gated on the "
                "draft flag no further polling resolves it -- mark it ready for "
                "review or change the acceptance kind",
            )
        return (verdict, evidence)
    if kind == "file":
        path = accept.get("path")
        if not isinstance(path, str) or not path:
            return ("error", "file spec needs a path")
        # bool() would coerce, and a truthy `{"exists": "false"}` must not
        # invert an absence check into a presence check; reject non-booleans
        # (including 1/0 — the pr guard already refuses bool as an int).
        exists = accept.get("exists", True)
        if not isinstance(exists, bool):
            return ("error", "file spec needs a boolean exists")
        want = exists
        have = Path(path).exists()
        verdict = "pass" if have == want else "fail"
        return (verdict, f"{path} {'exists' if have else 'does not exist'}")
    if kind == "human_approval":
        # Never machine-evaluated; the conductor asks the person.
        return ("pending", "awaiting human approval - not machine-checkable")
    if kind == "cmd":
        # Named explicitly so the removal reads as a decision rather than a gap:
        # a conductor carrying an older skill gets a message that tells it what
        # to do instead, not a bare "unknown kind".
        return (
            "refused",
            "the 'cmd' kind was removed: a spec may not name a command to run. "
            "Use 'pr_checks' for CI-backed acceptance (it covers 'the tests "
            "pass', since CI runs them), or ask for a new purpose-built kind",
        )
    return ("error", f"unknown accept kind {kind!r}")


def _usage_text() -> str:
    """The Usage..exit-code part of the module docstring, verbatim.

    Sliced out of ``__doc__`` instead of duplicated, so help a caller reads can
    never drift from the contract documented above it. ``python -OO`` strips
    docstrings, hence the one-line fallback.
    """
    doc = __doc__ or ""
    start = doc.find("Usage:")
    end = doc.find("THE SECURITY INVARIANT")
    if start < 0 or end <= start:
        return "Usage: python3 accept_eval.py < items.json"
    return doc[start:end].rstrip()


def _stdin_is_a_tty() -> bool:
    """Is stdin a terminal? A closed or detached stdin counts as not one."""
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def main() -> int:
    args = sys.argv[1:]
    if "-h" in args or "--help" in args or _stdin_is_a_tty():
        # Run with nothing piped in, the json.load below blocks on a read that
        # never completes: from a caller's side that is a tool timeout, an
        # approval spent, and no output - not "you forgot the input". Say what
        # the input is and stop. Exit 2 is the code malformed input already
        # uses, so nothing that pipes real input sees a new outcome.
        print(_usage_text(), file=sys.stderr)
        return 2
    try:
        payload = json.load(sys.stdin)
        items = payload["items"]
        assert isinstance(items, list)
    except Exception:
        print(json.dumps({"error": 'stdin must be JSON: {"items": [...]}'}))
        return 2
    results = []
    for position, item in enumerate(items):
        # ID extraction happens INSIDE the guard. A non-object entry
        # (``{"items": [1, {"id": "valid"}]}``) raises on ``.get`` before the
        # handler runs, and an uncaught raise here would abort the whole
        # evaluation and hide every sibling verdict - the exact failure the
        # per-item try exists to prevent. The positional fallback keeps a
        # malformed entry identifiable in the output.
        item_id = f"#{position}"
        try:
            if not isinstance(item, dict):
                raise TypeError(f"item must be a JSON object, got {type(item).__name__}")
            item_id = str(item.get("id", item_id))
            verdict, evidence = _evaluate(item)
        except Exception as exc:  # one bad spec must not hide the rest
            verdict, evidence = "error", f"evaluator bug on this item: {exc}"
        results.append({"id": item_id, "verdict": verdict, "evidence": evidence})
    print(json.dumps({"results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
