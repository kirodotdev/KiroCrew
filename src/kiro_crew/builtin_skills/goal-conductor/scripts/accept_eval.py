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
        {"id": "item-2", "accept": {"kind": "pr_merged", "pr": 123,
                                     "repo": "owner/name"}},
        {"id": "item-3", "accept": {"kind": "file", "path": "/abs/path",
                                     "exists": true}},
        {"id": "item-4", "accept": {"kind": "human_approval"}},
        {"id": "item-5", "accept": {"kind": "edition_check",
                                     "check": "name", "ref": "ID-1"}}
    ]}

Kinds:
    pr_checks       every check on a GitHub pull request is green
                    (``gh pr checks``).
    pr_merged       a GitHub pull request is merged (``gh pr view``; an open
                    one is pending, a closed-unmerged one fails). ``repo`` is
                    required here.
    file            a path exists (or, with ``"exists": false``, does not).
    human_approval  never machine-evaluated; always ``pending``.
    edition_check   an installed edition answers a named check (below).

``edition_check`` asks an installed edition. It runs
``kirocrew acceptance-check --json <check> <ref>``, a command the public build
does not ship, so here it answers ``error`` ("no edition provides the
acceptance check"). An edition that provides the check prints one JSON object
naming the same ``check``, ``ref`` and ``verdict`` as its exit code: 0 pass,
8 pending, 1 fail, 3 needs a person (read as ``refused``), 4 could not answer
(read as ``error``). Exit 2 means only that no edition provides that check.
The check names are the edition's; the core knows none of them. The edition
contract, and how an edition provides the command, is specified in
``docs/system-specs/modules/platform-context.md`` ("Acceptance check command").

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
import re
import shutil
import subprocess
import sys
from pathlib import Path

#: Binaries this script itself invokes, from argv it builds. NOT a spec-facing
#: allowlist - nothing in a spec can name a command at all. This exists so a
#: handler that ever passes spec input into `_exec` fails closed instead of
#: executing it, which is the regression the `cmd` kind's removal prevents.
#: `gh` serves `pr_checks`, its draft probe and `pr_merged`'s `pr view`;
#: `kirocrew` serves `edition_check` and runs exactly one fixed subcommand,
#: `acceptance-check --json <check> <ref>`.
_SELF_BUILT_COMMANDS = {"gh", "kirocrew"}

TIMEOUT_SECS = 300
EVIDENCE_TAIL_CHARS = 500

#: gh pr checks exits 8 when checks are still running (gh >= 2.30).
_GH_PENDING_EXIT = 8

#: The GitHub ``owner/name`` ``pr_merged`` accepts. Applied with ``fullmatch``:
#: a ``$`` anchor also matches before a trailing newline. ASCII classes only,
#: and an owner cannot start with ``-``, so the value can never read as a flag.
_GH_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")

#: ``edition_check``'s two fields, applied with ``fullmatch``: a ``$`` anchor
#: also matches before a trailing newline. ASCII classes only, and both must
#: start with a letter or digit, so neither can ever read as a flag.
_CHECK_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
_CHECK_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

#: ``kirocrew acceptance-check`` exit codes and the report verdict each must
#: carry. The report has to agree with the exit code before either is
#: believed: a crash also exits 1, and a crash is not a failed check. Exit 4 is
#: the edition's own "this check exists but could not answer" (a ref the check
#: does not accept, or its system unreachable); it reads as ``error``, with the
#: edition's summary as the evidence.
_EDITION_EXIT_VERDICTS = {0: "pass", 1: "fail", 3: "needs_person", 4: "error", 8: "pending"}

#: argparse's usage exit, and it has one meaning: no installed edition provides
#: the check. The evaluator validated both fields and always passes both, so
#: argparse can only be refusing the command (the public ``kirocrew`` has no
#: ``acceptance-check``) or a check name the edition does not know. A check
#: that exists but rejects a ref answers exit 4 instead.
_EDITION_USAGE_EXIT = 2


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


def _pr_merged(accept):
    """Has this GitHub pull request merged? ``MERGED`` passes.

    ``OPEN`` is ``pending``, because it can still merge; ``CLOSED`` fails,
    because a closed pull request that did not merge carries no merged code. A
    ``gh`` that cannot answer (no such PR, no auth) is an ``error``, never a
    ``fail``: it says nothing about the work.

    ``repo`` is required, unlike ``pr_checks``: without it ``gh`` resolves the
    number against whatever checkout the evaluator runs in.
    """
    pr = accept.get("pr")
    # bool is an int subclass; a non-positive number would reach gh as a flag.
    if not isinstance(pr, int) or isinstance(pr, bool) or pr < 1:
        return ("error", "pr_merged spec needs a positive integer pr")
    repo = accept.get("repo")
    if not isinstance(repo, str) or not _GH_REPO.fullmatch(repo):
        return ("error", "pr_merged spec needs a repo of the form owner/name")
    argv = ["gh", "pr", "view", str(pr), "--repo", repo, "--json", "state", "-q", ".state"]
    problem, proc = _exec(argv)
    if proc is None:
        return problem or ("error", "could not run")
    output = _tail(proc.stdout + "\n" + proc.stderr)
    if proc.returncode != 0:
        return ("error", f"could not read PR #{pr} in {repo}: exit {proc.returncode}: {output}")
    state = proc.stdout.strip().upper()
    if state == "MERGED":
        return ("pass", f"PR #{pr} in {repo} is merged")
    if state == "OPEN":
        return ("pending", f"PR #{pr} in {repo} is open and not merged yet")
    if state == "CLOSED":
        return ("fail", f"PR #{pr} in {repo} was closed without merging")
    return ("error", f"PR #{pr} in {repo} reported an unknown state: {output}")


def _kirocrew_on_path():
    """Name the ``kirocrew`` this evaluator runs, for evidence only.

    The argv stays the bare name, resolved from ``PATH`` by the operating
    system, so this never picks the binary; it only says which one ``PATH``
    holds. That one need not be the install the gateway runs from (a shim
    beside a virtual environment, say), and an answer of ``error`` is easier
    to act on when it names it.
    """
    found = shutil.which("kirocrew")
    return f"kirocrew on PATH is {found}" if found else "no kirocrew on PATH"


def _edition_report(stdout):
    """The one JSON object ``kirocrew acceptance-check --json`` prints, or ``None``."""
    try:
        report = json.loads(stdout or "")
    except (ValueError, RecursionError):
        return None
    return report if isinstance(report, dict) else None


def _edition_check(accept):
    """Has an edition's own check on this reference passed?

    The edition does the reading and judging; this handler validates the two
    fields, builds the one fixed argv, and maps the answer. The exit code and the
    report's ``verdict`` must agree, and the report must name the check and the
    reference that were asked about, before either is believed. Anything else is
    an ``error``: an unexpected exit is a broken environment, not a verdict on the
    work, so it must never read as ``fail`` - or, worse, as ``pass``.

    ``needs_person`` (exit 3) becomes ``refused``: the check says waiting cannot
    change its answer and a person decides, and the conductor surfaces a
    ``refused`` verdict to the user rather than polling it. ``error`` (exit 4)
    stays ``error``, carrying the edition's own reason: the check exists but
    could not answer, which is never a verdict on the work.
    """
    check = accept.get("check")
    if not isinstance(check, str) or not _CHECK_NAME.fullmatch(check):
        return (
            "error",
            "edition_check spec needs a check name: a lower-case letter, then up to 31 "
            "lower-case letters, digits or hyphens",
        )
    ref = accept.get("ref")
    if not isinstance(ref, str) or not _CHECK_REF.fullmatch(ref):
        return (
            "error",
            "edition_check spec needs a ref: a letter or digit, then up to 127 letters, "
            "digits, dots, underscores or hyphens",
        )
    problem, proc = _exec(["kirocrew", "acceptance-check", "--json", check, ref])
    if proc is None:
        return problem or ("error", "could not run")
    output = _tail(proc.stdout + "\n" + proc.stderr)
    if proc.returncode == _EDITION_USAGE_EXIT:
        return (
            "error",
            f"no installed edition provides the acceptance check {check!r}: "
            f"kirocrew acceptance-check exited 2 ({output or 'no output'}); "
            f"{_kirocrew_on_path()}",
        )
    expected = _EDITION_EXIT_VERDICTS.get(proc.returncode)
    report = _edition_report(proc.stdout)
    if (
        expected is None
        or report is None
        or report.get("verdict") != expected
        or report.get("check") != check
        or report.get("ref") != ref
    ):
        return (
            "error",
            f"kirocrew acceptance-check exit {proc.returncode} came without a matching "
            f"report for {check} {ref}: {output}; {_kirocrew_on_path()}",
        )
    summary = _tail(str(report.get("summary") or "")) or f"{check} {ref}: {expected}"
    if expected == "error":
        return ("error", f"{summary} -- the edition's check could not answer")
    if expected == "needs_person":
        return ("refused", f"{summary} -- waiting will not change this; a person decides")
    return (expected, summary)


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
    if kind == "pr_merged":
        return _pr_merged(accept)
    if kind == "edition_check":
        return _edition_check(accept)
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
