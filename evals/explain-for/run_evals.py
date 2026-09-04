#!/usr/bin/env python3
"""Evaluate the ``explain-for`` skill.

Two modes, because only one of them can run in CI:

``--check`` (default)
    Deterministic. No model, no network. Validates that the case set and the
    skill agree with each other, and -- the load-bearing part -- that every
    prompt actually *reaches* the skill through Crew's word-overlap trigger
    matching. A well-written skill nobody triggers is worth nothing, and that
    failure is invisible to a prose review. Exits non-zero on any problem, so a
    test can gate on it.

``--run``
    The A/B measurement: each prompt answered twice, once with the skill body
    prepended and once bare, then both graded against the case's assertions by
    the same CLI acting as judge. Needs a working agent CLI and costs tokens, so
    it is a local command, not a CI gate. Results land in ``iteration-N/``.

Stdlib only, Python 3.8+, subprocess called with argument lists.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
SKILL_FILE = REPO_ROOT / "src" / "kiro_crew" / "builtin_skills" / "explain-for" / "SKILL.md"
CASES_FILE = HERE / "cases.json"

# Crew's own trigger threshold. Imported when the package is importable so the
# gate cannot silently drift from the loader; the literal is the fallback for a
# bare checkout.
_FALLBACK_MIN_OVERLAP = 0.7

DEFAULT_CLI = ["kiro-cli", "chat", "--no-interactive", "--trust-tools="]
DEFAULT_TIMEOUT = 300


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def min_trigger_overlap() -> float:
    try:
        from kiro_crew.skills import _MIN_TRIGGER_OVERLAP  # type: ignore

        return float(_MIN_TRIGGER_OVERLAP)
    except Exception:
        return _FALLBACK_MIN_OVERLAP


def read_skill() -> Tuple[Dict[str, str], str]:
    """Return (frontmatter, body) for the skill under test."""
    text = SKILL_FILE.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise SystemExit(f"{SKILL_FILE}: no YAML frontmatter")
    end = text.find("\n---", 3)
    if end == -1:
        raise SystemExit(f"{SKILL_FILE}: unterminated frontmatter")
    meta: Dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    body = text[end + 4 :].lstrip("\n")
    return meta, body


def audience_labels(body: str) -> List[str]:
    """First-column labels of the skill's AUDIENCE tables.

    Scoped to tables whose header row's first cell is literally ``Audience``.
    An earlier version collected the first column of *every* markdown table in
    the body, which silently absorbed any other table the skill grew -- the
    diagram "does this have a shape?" table, for instance -- so a case could
    declare an audience like "A definition, a single fact" and pass the
    coherence check against a row that is not an audience at all.
    """
    labels: List[str] = []
    in_audience_table = False
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            in_audience_table = False  # any non-table line ends the current table
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or not cells[0]:
            continue
        first = cells[0]
        if set(first) <= set("-: "):
            continue  # separator row, keeps whatever scope the header set
        if first.lower() == "audience":
            in_audience_table = True
            continue
        if first.lower().startswith("the thing") or not in_audience_table:
            in_audience_table = False
            continue
        labels.append(first)
    return labels


def parse_triggers(meta: Dict[str, str]) -> List[str]:
    raw = meta.get("triggers", "")
    return [t.strip().lower() for t in raw.split(",") if t.strip()]


def worked_shape_prompts(body: str) -> List[str]:
    """The example prompts the skill spells out in its own "Worked shapes" section.

    An eval prompt that duplicates one of these measures recall, not skill: the
    skill body is injected ahead of the prompt, so the answer is already in the
    context and the with-skill lane wins for a reason the skill would not
    reproduce on unseen phrasing.
    """
    section: List[str] = []
    inside = False
    for line in body.splitlines():
        if line.strip().startswith("## "):
            inside = "worked shape" in line.lower()
            continue
        if inside:
            section.append(line)
    return [m.strip().lower() for m in re.findall(r'\*\*"([^"]+)"\*\*', "\n".join(section))]


def best_overlap(prompt: str, triggers: Sequence[str]) -> Tuple[float, Optional[str]]:
    """Crew's scoring: per-phrase word-set overlap, best phrase wins.

    Mirrors ``SkillsLoader.get_triggered_skills``. Negative (``!``) triggers are
    honoured as an immediate exclusion.
    """
    text_words = set(re.findall(r"\w+", prompt.lower()))
    best = 0.0
    winner: Optional[str] = None
    for trigger in triggers:
        if trigger.startswith("!"):
            neg = set(re.findall(r"\w+", trigger[1:]))
            if neg and neg <= text_words:
                return 0.0, None
            continue
        words = set(re.findall(r"\w+", trigger))
        if not words:
            continue
        overlap = len(words & text_words) / len(words)
        if overlap > best:
            best, winner = overlap, trigger
    return best, winner


# --------------------------------------------------------------------------- #
# --check
# --------------------------------------------------------------------------- #


def run_check(verbose: bool) -> int:
    meta, body = read_skill()
    spec = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    cases = spec.get("cases", [])
    triggers = parse_triggers(meta)
    labels = audience_labels(body)
    shapes = worked_shape_prompts(body)
    threshold = min_trigger_overlap()
    problems: List[str] = []

    if not cases:
        problems.append("cases.json defines no cases")
    if not triggers:
        problems.append("skill declares no triggers, so nothing can auto-load it")
    if not shapes:
        # The duplicate-prompt rule below is only as good as this list. An empty
        # list makes it pass every case without checking anything, and that
        # silence is indistinguishable from success -- so an empty parse is a
        # failure, not a skipped rule. It goes empty if the "Worked shapes"
        # heading is renamed or the examples stop being **"bold-quoted"**.
        problems.append(
            "no worked-shape examples parsed out of the skill, so the duplicate-prompt "
            "rule would pass vacuously -- check the 'Worked shapes' heading and that its "
            'examples are still **"bold-quoted"**'
        )
    for field in ("name", "description"):
        if not meta.get(field):
            problems.append(f"skill frontmatter missing required '{field}'")

    seen_ids, seen_names = set(), set()
    exercised: set = set()
    for case in cases:
        cid = case.get("id")
        name = case.get("name", "?")
        prompt = case.get("prompt", "")
        tag = f"case {cid} ({name})"

        if cid in seen_ids:
            problems.append(f"{tag}: duplicate id")
        seen_ids.add(cid)
        if name in seen_names:
            problems.append(f"{tag}: duplicate name")
        seen_names.add(name)
        if not prompt:
            problems.append(f"{tag}: empty prompt")
            continue

        assertions = case.get("assertions") or []
        if len(assertions) < 2:
            problems.append(f"{tag}: needs at least 2 assertions, has {len(assertions)}")

        expect = bool(case.get("expect_trigger", True))
        score, winner = best_overlap(prompt, triggers)
        fires = score >= threshold
        if expect and fires and winner:
            exercised.add(winner)

        if expect and not fires:
            problems.append(
                f"{tag}: prompt does NOT trigger the skill "
                f"(best overlap {score:.2f} < {threshold}) -- the skill would never load "
                f"for this prompt, so the case cannot measure it"
            )
        elif not expect and fires:
            problems.append(
                f"{tag}: control prompt WRONGLY triggers on '{winner}' "
                f"(overlap {score:.2f} >= {threshold}) -- the trigger set is too loose"
            )

        audience = case.get("audience")
        if expect and not audience:
            problems.append(f"{tag}: expected to trigger but names no audience")
        if audience and audience not in labels:
            problems.append(
                f"{tag}: audience '{audience}' is not a row in the skill's tables "
                f"(known: {', '.join(labels)})"
            )

        if prompt.strip().lower() in shapes:
            problems.append(
                f"{tag}: prompt duplicates one of the skill's own worked-shape examples, "
                f"so the with-skill lane would be reciting an answer already in its context "
                f"instead of generalising -- pick phrasing the skill does not spell out"
            )

        if verbose:
            verdict = "fires" if fires else "silent"
            print(f"  {tag}: {verdict} ({score:.2f}) via {winner or '-'}")

    # Every declared trigger needs a case that actually fires on it. A trigger no
    # case exercises is untested surface: it can be too loose, or dead, and the
    # suite would stay green either way -- which is the same shape of gap as a
    # gate that passes vacuously.
    unexercised = [t for t in triggers if not t.startswith("!") and t not in exercised]
    if unexercised:
        problems.append(
            "trigger(s) no passing case fires on: "
            + ", ".join(repr(t) for t in unexercised)
            + " -- add a case whose prompt matches, or drop the trigger"
        )

    print()
    if problems:
        print(f"CHECK FAILED -- {len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"CHECK PASSED -- {len(cases)} cases, {len(triggers)} triggers, {len(labels)} audiences")
    return 0


# --------------------------------------------------------------------------- #
# --run
# --------------------------------------------------------------------------- #


def cli_argv() -> List[str]:
    override = os.environ.get("EXPLAIN_FOR_EVAL_CLI")
    return override.split() if override else list(DEFAULT_CLI)


def ask(argv: Sequence[str], prompt: str, timeout: int) -> Optional[str]:
    """Run one CLI call. Returns the answer text, or None if the call FAILED.

    None means the harness never got an answer -- a timeout, a non-zero exit, an
    empty stdout. That is deliberately NOT a string: an earlier version returned
    a ``"<TIMEOUT>"`` sentinel, which then flowed into the grader and came back
    as every criterion FAILing. A transport failure was being recorded as the
    model failing the content criteria, so the pass rate -- the entire output of
    ``--run`` -- silently moved for a reason that had nothing to do with the
    skill. Callers must branch on None instead of grading it.
    """
    try:
        proc = subprocess.run(
            list(argv) + [prompt],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"    ! CLI call timed out after {timeout}s", flush=True)
        return None

    # THE GATE, stated once and exhaustively: a result is an answer only if the
    # process exited 0 AND produced non-empty stdout. Anything else is an error,
    # never content.
    #
    # This is deliberately a closed enumeration rather than a list of observed
    # failures, because the observed-failure approach was tried and cost three
    # review rounds on this one function -- a "<TIMEOUT>" sentinel that got
    # graded, a partially-parsed grader reply back-filled as FAILs, and a
    # non-zero exit whose stdout was returned as the model's answer. A
    # subprocess can only fail three ways: it never finished, it finished
    # badly, or it finished with nothing to say. All three are covered here and
    # there is no fourth axis, so this closes the family instead of the instance.
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        why = "exited non-zero" if proc.returncode != 0 else "produced no output"
        err = (proc.stderr or "").strip()[:400]
        print(f"    ! CLI call {why} (rc={proc.returncode}) {err}", flush=True)
        return None
    return out


#: The ONLY shape a grader line may take. Anchored at both ends, so the whole
#: line is the claim -- there is no leading or trailing room for prose. An
#: optional ``.`` after the index is the one tolerance (a grader writing
#: ``1.|PASS|...``); casing and padding around the token are the others.
_VERDICT_LINE_RE = re.compile(r"^\s*(\d+)\.?\s*\|\s*(PASS|FAIL)\s*\|(.*)$", re.IGNORECASE)


def parse_grading(raw: str, count: int) -> Optional[List[Tuple[bool, str]]]:
    """Parse a grader reply into one verdict per criterion, or None if unusable.

    The invariant is a **positive grammar**: the reply is a grading only if every
    non-blank line matches one exact shape, and the indices those lines carry
    are precisely 1..count with each appearing **exactly once**. Anything else
    is an errored grading, not a set of failures.

    Stated positively on purpose. The earlier version scavenged recognisable
    lines out of arbitrary text and skipped the rest, then bolted on a check per
    malformation as each was found -- a sentinel string graded as an answer, a
    partial reply back-filled as FAILs, a non-zero exit returned as content, and
    duplicate verdicts for one criterion overwriting each other. Each fix was
    right for its own case and none of them bounded the next, because the ways a
    model can reply wrongly are not enumerable in advance. Matching one accepted
    shape inverts that: novel malformations fail by default instead of needing to
    be predicted, so "unparseable" and "judged" can never be confused again.

    Consequences worth naming, since they are the point rather than side effects:

    - A repeated index is refused outright. A grader that contradicts itself has
      not judged that criterion, and silently keeping either verdict invents an
      answer the grader did not give.
    - An index outside 1..count is refused, so renumbered or over-long replies
      cannot shift verdicts onto the wrong criterion.
    - A stray prose line -- even alongside a full set of valid verdicts --
      makes the whole reply unusable. A grader that ignores "output nothing
      else" has already departed from its instructions, so its verdicts have
      lost the claim to be read as verdicts.

    Blank lines are the sole exception: they carry no claim, so they are skipped
    rather than treated as a violation.

    Pure and model-free on purpose: this is the part of ``--run`` that CI can
    actually test.
    """

    def _unusable(reason: str) -> None:
        print(f"    ! grader reply unusable: {reason}", flush=True)

    seen: Dict[int, Tuple[bool, str]] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        match = _VERDICT_LINE_RE.match(line)
        if match is None:
            _unusable(f"line is not a verdict: {line.strip()[:60]!r}")
            return None
        idx = int(match.group(1))
        if not 1 <= idx <= count:
            _unusable(f"criterion {idx} is outside the expected 1..{count}")
            return None
        if idx in seen:
            _unusable(f"criterion {idx} received more than one verdict")
            return None
        seen[idx] = (match.group(2).upper() == "PASS", match.group(3).strip())

    missing = [i for i in range(1, count + 1) if i not in seen]
    if missing:
        _unusable(f"no verdict for criterion {', '.join(str(i) for i in missing)} of {count}")
        return None
    return [seen[i] for i in range(1, count + 1)]


def grade(
    argv: Sequence[str], answer: str, assertions: Sequence[str], timeout: int
) -> Optional[List[Tuple[bool, str]]]:
    """Ask the CLI to judge one answer against its assertions.

    Returns None when the grading is unusable -- either the grader call itself
    failed, or it replied and the reply did not carry a verdict for every
    criterion. In both cases the answer was never actually judged, so it must
    not reach the tally. See ``parse_grading`` for the invariant.
    """
    numbered = "\n".join(f"{i}. {a}" for i, a in enumerate(assertions, 1))
    prompt = (
        "You are grading one answer against explicit criteria. Do not be generous.\n"
        "For EACH numbered criterion output exactly one line:\n"
        "  <number>|PASS or FAIL|one short sentence of evidence quoted from the answer\n"
        "Output nothing else.\n\n"
        f"CRITERIA:\n{numbered}\n\n"
        f"ANSWER TO GRADE:\n{answer}\n"
    )
    raw = ask(argv, prompt, timeout)
    if raw is None:
        return None
    return parse_grading(raw, len(assertions))


def next_iteration_dir() -> Path:
    n = 1
    while (HERE / f"iteration-{n}").exists():
        n += 1
        if n > 999:
            raise SystemExit("too many iteration directories")
    out = HERE / f"iteration-{n}"
    out.mkdir(parents=True)
    return out


def run_evals(only: Optional[int], with_skill_only: bool, timeout: int) -> int:
    argv = cli_argv()
    if not shutil.which(argv[0]):
        print(f"agent CLI '{argv[0]}' not on PATH.")
        print("Set EXPLAIN_FOR_EVAL_CLI to the non-interactive command to use.")
        return 2

    _, body = read_skill()
    spec = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    cases = [c for c in spec.get("cases", []) if only is None or c.get("id") == only]
    if not cases:
        print(f"no case matched --test={only}")
        return 2

    out_root = next_iteration_dir()
    lanes = ["with-skill"] if with_skill_only else ["with-skill", "baseline"]
    tally: Dict[str, List[int]] = {lane: [0, 0] for lane in lanes}
    # Calls that never produced a gradeable answer. Counted separately and kept
    # OUT of the tally: folding a transport failure in as FAILs would move the
    # pass rate for a reason unrelated to the skill, in whichever lane happened
    # to time out, and nothing in the output would say so.
    errors: Dict[str, int] = {lane: 0 for lane in lanes}

    for case in cases:
        name = case["name"]
        prompt = case["prompt"]
        assertions = case.get("assertions") or []
        case_dir = out_root / name
        case_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n--- Case {case['id']}: {name} ---")

        for lane in lanes:
            if lane == "with-skill":
                # What the trigger-injection path delivers: the skill body ahead
                # of the user's message.
                full = f"{body}\n\n---\n\nUser request: {prompt}"
            else:
                full = prompt
            print(f"  [{lane}]")
            answer = ask(argv, full, timeout)
            if answer is None:
                errors[lane] += 1
                (case_dir / f"{lane}.txt").write_text("", encoding="utf-8")
                (case_dir / f"{lane}.grading.txt").write_text(
                    "ERROR no answer: the CLI call failed, so this lane was not graded\n",
                    encoding="utf-8",
                )
                print("    ERROR -- not graded, excluded from the pass rate")
                continue
            (case_dir / f"{lane}.txt").write_text(answer, encoding="utf-8")

            graded = grade(argv, answer, assertions, timeout)
            if graded is None:
                errors[lane] += 1
                (case_dir / f"{lane}.grading.txt").write_text(
                    "ERROR grader failed: the answer exists but was not graded\n",
                    encoding="utf-8",
                )
                print("    ERROR -- grader failed, excluded from the pass rate")
                continue
            lines = []
            for i, (ok, evidence) in enumerate(graded, 1):
                verdict = "PASS" if ok else "FAIL"
                print(f"    {verdict}  #{i} -- {evidence}")
                lines.append(f"{verdict} #{i} {assertions[i - 1]}\n    {evidence}")
                tally[lane][0] += 1 if ok else 0
                tally[lane][1] += 1
            (case_dir / f"{lane}.grading.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n" + "=" * 41)
    print(f"  PASS RATE -- {out_root.name}")
    print("=" * 41)
    rates = {}
    for lane in lanes:
        passed, total = tally[lane]
        pct = (passed / total * 100) if total else 0.0
        rates[lane] = pct
        suffix = f"  [{errors[lane]} errored, ungraded]" if errors[lane] else ""
        print(f"  {lane:<12} {passed}/{total} ({pct:.1f}%){suffix}")
    # A delta between lanes graded on different numbers of cases is not a
    # comparison, so say that rather than printing a number that looks like one.
    if len(lanes) == 2:
        if errors["with-skill"] or errors["baseline"]:
            print(f"  {'delta':<12} not comparable -- the lanes graded different case sets")
        else:
            print(f"  {'delta':<12} {rates['with-skill'] - rates['baseline']:+.1f}%")
    if any(errors.values()):
        print(
            f"  {'errors':<12} {sum(errors.values())} call(s) never answered -- rerun those cases"
        )
    print("=" * 41)
    print(f"  results: {out_root}")
    return 1 if any(errors.values()) else 0


# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check", action="store_true", help="deterministic validation only (default)"
    )
    parser.add_argument(
        "--run", action="store_true", help="run the A/B measurement through an agent CLI"
    )
    parser.add_argument("--test", type=int, metavar="ID", help="restrict --run to one case id")
    parser.add_argument("--with-skill-only", action="store_true", help="skip the baseline lane")
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT, help="per-CLI-call timeout in seconds"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="print per-case trigger scores in --check"
    )
    args = parser.parse_args()

    if args.run:
        rc = run_check(args.verbose)
        if rc != 0:
            print("\nrefusing to spend tokens on an inconsistent case set.")
            return rc
        return run_evals(args.test, args.with_skill_only, args.timeout)
    return run_check(args.verbose)


if __name__ == "__main__":
    sys.exit(main())
