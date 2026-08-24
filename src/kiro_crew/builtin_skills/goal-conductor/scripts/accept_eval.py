#!/usr/bin/env python3
"""AcceptSpec evaluator - the deterministic half of the conductor's patrol.

The conductor NEVER judges whether a work item succeeded; this script does,
and the conductor only reads its verdicts. Script-first: the decision is an
exit code and a JSON document, not a model's impression of a transcript.

Usage:
    python3 accept_eval.py < items.json

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

Stdlib-only, Python 3.8+.
"""

import json
import subprocess
import sys
from pathlib import Path

#: Binaries this script itself invokes, from argv it builds. NOT a spec-facing
#: allowlist - nothing in a spec can name a command at all. This exists so a
#: handler that ever passes spec input into `_run` fails closed instead of
#: executing it, which is the regression the `cmd` kind's removal prevents.
_SELF_BUILT_COMMANDS = {"gh"}

TIMEOUT_SECS = 300
EVIDENCE_TAIL_CHARS = 500

#: gh pr checks exits 8 when checks are still running (gh >= 2.30).
_GH_PENDING_EXIT = 8


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text[-EVIDENCE_TAIL_CHARS:] if len(text) > EVIDENCE_TAIL_CHARS else text


def _run(argv, cwd=None):
    """Run one SCRIPT-BUILT check without a shell; return (verdict, evidence).

    Every caller must pass an argv it constructed itself. The guard below is an
    internal invariant, not a spec gate: reaching it with something outside
    ``_SELF_BUILT_COMMANDS`` means a handler leaked spec input into an exec path,
    so it refuses rather than runs.
    """
    raw = str(argv[0])
    if raw not in _SELF_BUILT_COMMANDS:
        return (
            "refused",
            f"evaluator bug: {raw!r} is not a command this script builds; "
            "no spec field may name a command",
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
        return ("error", f"timed out after {TIMEOUT_SECS}s")
    except FileNotFoundError:
        return ("error", f"{raw!r} not found on PATH")
    except OSError as exc:
        return ("error", f"could not run: {exc}")
    output = _tail(proc.stdout + "\n" + proc.stderr)
    if proc.returncode == 0:
        return ("pass", output or "exit 0")
    if raw == "gh" and proc.returncode == _GH_PENDING_EXIT:
        return ("pending", output or "checks still running")
    return ("fail", f"exit {proc.returncode}: {output}")


def _evaluate(item):
    accept = item.get("accept") or {}
    kind = accept.get("kind")
    if kind == "pr_checks":
        pr = accept.get("pr")
        # bool is an int subclass, and `{"pr": true}` must not become `gh pr
        # checks True`; reject it with the same message as any other non-int.
        if not isinstance(pr, int) or isinstance(pr, bool):
            return ("error", "pr_checks spec needs an integer pr")
        argv = ["gh", "pr", "checks", str(pr)]
        repo = accept.get("repo")
        if repo:
            argv += ["--repo", str(repo)]
        return _run(argv)
    if kind == "file":
        path = accept.get("path")
        if not isinstance(path, str) or not path:
            return ("error", "file spec needs a path")
        want = bool(accept.get("exists", True))
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


def main() -> int:
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
