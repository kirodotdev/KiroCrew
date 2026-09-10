#!/usr/bin/env python3
"""scope_candidates — the deterministic half of the security-scope review.

The scope reviewer asks one question about a security-tightening change: which
legitimate operations does it newly refuse? A model cannot answer that by reading
the matcher -- it would have to simulate four checks and thousands of lines, and a
model that reports a refusal it did not observe produces a confident finding about
nothing. So the lane splits the work: the model PROPOSES candidate legitimate
operations, and ``scripts/deny_diff.py`` DECIDES, by classifying each candidate
with the real code at the base ref and at the head ref.

This script is the two seams around that decision.

``validate``
    Takes the model's ``candidates.json``, proves it is a corpus ``deny_diff`` can
    consume, and writes the normalized file the differential is pointed at.

``verdict``
    Takes the per-leg ``deny_diff --json`` reports, each under the label its
    caller gave it, folds them into one verdict, and renders the review body plus
    the paste-ready golden-path rows. The caller also declares which legs it
    REQUIRES, because the fold sees only the reports it was handed: a leg whose
    job died uploads nothing, and folding the survivors would publish a clean
    verdict for a platform that never reported.

``conclude``
    Takes the fold result, the model's ``Scope-Verdict`` header, whether the
    review marked THIS head, and the two platform-gap signals, and folds them into
    ONE lane conclusion -- the mapping both review lanes share instead of
    hand-copying a ladder into each workflow. It is a pure downstream mapping: it
    prints its answer and exits 0, and spends none of the exit-1/exit-2 contract
    below on its own account.

Why a script and not shell in the workflow: every rule below is a way the lane
could report a false green, and a false green here is worse than no lane at all --
it stands as evidence the question was asked. The rules are testable here and
untestable in a heredoc.

Exit codes, ``validate``: ``0`` a corpus was written, ``2`` the file cannot be
trusted, ``3`` valid but nothing left to adjudicate. Exit codes, ``verdict``: ``0``
no confirmed regression, ``1`` at least one, ``2`` the question could not be
settled. Both fail CLOSED -- a check that could not run never exits 0, because
"could not run" and "found nothing" are the same badge to a reader and must not be
the same exit code.

``verdict``'s exit ``1`` is a factual claim -- the classifier confirmed a
newly-refused operation -- so ONLY that finding may produce it. Every other way
this script can end, an unreadable report and an internal defect alike, is exit
``2``. ``conclude`` does not touch that contract: it maps
already-settled signals to a conclusion word and exits ``0``; only a malformed
flag is argparse's exit ``2``.

A candidate is DATA, never authorization. ``deny_diff`` classifies a corpus row's
string; it never executes one. That property is what makes it safe to let a model
write the corpus this lane classifies, and nothing in this script may weaken it --
neither subcommand runs, resolves, or expands anything a candidate names.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

#: Row ceiling. A capped file is a bounded blast radius for a prompt-injected or
#: runaway generator, and the cap doubles as a quality floor the prompt states in
#: its own terms: fifteen boundary-probing rows beat sixty restatements. Counted
#: BEFORE dedupe, so padding a file with duplicates cannot buy room.
MAX_ROWS = 60

#: Per-command ceiling. The corpus holds operations, not payloads; a multi-kilobyte
#: "command" is either a generated blob or an attempt to smuggle prose through the
#: classifier, and neither is a golden path.
MAX_COMMAND_CHARS = 512


class CandidateError(Exception):
    """A candidate file that cannot be trusted. Always exit 2, never a pass."""


def _load_deny_diff() -> Any:
    """Import ``deny_diff`` beside this file, for its row schema and nothing else.

    The schema lives in ONE place on purpose. A second validator here would drift
    from the classifier's, and the drift would show up as a row this script
    accepted and the differential then rejected -- which surfaces as a gate that
    errored rather than as the malformed row it is.
    """
    path = Path(__file__).resolve().parent / "deny_diff.py"
    spec = importlib.util.spec_from_file_location("_scope_deny_diff", path)
    if spec is None or spec.loader is None:
        raise CandidateError(f"cannot load the row schema from {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: a script's dataclasses resolve their own annotations
    # through `sys.modules[cls.__module__]`, so a module executed without a
    # registration raises at class-creation time rather than at use.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _read_json(path: Path, what: str) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CandidateError(f"cannot read {what} {path}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CandidateError(f"{what} {path} is not valid JSON: {exc}") from exc


def _rows_of(payload: Any, what: str) -> list[dict[str, Any]]:
    """The ``golden_paths`` array out of a corpus-shaped payload.

    Accepts the wrapper object and a bare list, matching ``deny_diff.load_corpus``
    so a candidate file and the committed corpus are the same shape.
    """
    if isinstance(payload, dict):
        if "golden_paths" not in payload:
            raise CandidateError(f"{what} is an object without a 'golden_paths' key")
        payload = payload["golden_paths"]
    if not isinstance(payload, list):
        raise CandidateError(f"{what} must hold a list of rows, got {type(payload).__name__}")
    return payload


def _key(row: dict[str, Any]) -> tuple[str, str, str]:
    """The identity of a row: kind, command, platform.

    The same triple ``ledger.py import-golden-paths`` is idempotent on, so a
    candidate the corpus already carries is recognised as the duplicate it is
    rather than probed a second time.
    """
    return (
        str(row.get("kind", "")).strip(),
        str(row.get("command_or_flow", "")).strip(),
        str(row.get("platform", "any")).strip(),
    )


def validate(
    candidates_path: Path,
    corpus_path: Path | None,
    out_path: Path,
    max_rows: int = MAX_ROWS,
    max_command_chars: int = MAX_COMMAND_CHARS,
) -> int:
    """Normalize the model's candidates into a corpus the differential can read.

    Four rules, each closing a way this lane could report a false green:

    *Schema by the classifier's own validator.* Every row goes through
    ``deny_diff``'s row parser, which rejects an unknown ``kind``, an empty
    command, and an unknown ``platform`` by position. A dropped-and-continued row
    would mean the differential ran over a subset nobody chose.

    *Only a kind the classifier settles.* ``deny_diff`` classifies ``shell``
    rows and counts every other kind as skipped, classifying nothing for it. A
    ``flow`` or ``cron`` candidate would therefore spend a slot, reach the
    submitted corpus, and come back with no verdict while its leg still reports
    rows classified -- an unmeasured row inside a measured run. This is narrower
    than the classifier's schema on purpose, and it is the only seam where the
    corpus this lane submits is still this script's to choose.

    *Caps before dedupe.* :data:`MAX_ROWS` and :data:`MAX_COMMAND_CHARS` bound a
    generator that ran away or was steered, and counting before dedupe means a
    padded file cannot buy itself room. They are the caps on every CLI path: the
    keyword arguments exist so a test can drive a small cap directly, because a
    second spelling of a ceiling is a ceiling that can disagree with itself.

    *Dedupe against the BASE corpus.* A candidate the committed corpus already
    holds is already gated by the denial differential; re-probing it spends a slot
    and reports a finding the other lane owns. Dropped, and counted in the report.

    *An empty result is exit 3, not exit 0.* ``deny_diff`` refuses an empty corpus,
    so writing one would surface as a gate that errored. "Every candidate was
    already covered" is a real and honest outcome, and it needs its own code so
    the workflow can say so instead of failing.
    """
    deny_diff = _load_deny_diff()

    payload = _read_json(candidates_path, "candidate file")
    raw_rows = _rows_of(payload, f"candidate file {candidates_path}")
    if not raw_rows:
        raise CandidateError(f"candidate file {candidates_path} holds no rows")
    if len(raw_rows) > max_rows:
        raise CandidateError(
            f"candidate file {candidates_path} holds {len(raw_rows)} rows, cap is {max_rows}"
        )

    for position, row in enumerate(raw_rows):
        if not isinstance(row, dict):
            raise CandidateError(f"candidate row {position} is not an object")
        kind = row.get("kind")
        if kind != "shell":
            raise CandidateError(
                f"candidate row {position} has kind {kind!r}, and only 'shell' is "
                "adjudicated -- the differential classifies nothing for another kind, "
                "so the row would be submitted and never settled"
            )
        command = row.get("command_or_flow")
        if isinstance(command, str) and len(command) > max_command_chars:
            raise CandidateError(
                f"candidate row {position} has a {len(command)}-char command, "
                f"cap is {max_command_chars}"
            )

    # The classifier's own parser is the schema. It raises DenyDiffError naming the
    # offending position, which is the message a reviewer needs, so let it through
    # as a CandidateError rather than restating it.
    try:
        deny_diff.load_corpus(candidates_path)
    except Exception as exc:  # DenyDiffError, by any other import path
        raise CandidateError(f"{candidates_path} is not a valid corpus: {exc}") from exc

    known: set[tuple[str, str, str]] = set()
    if corpus_path is not None:
        for row in _rows_of(_read_json(corpus_path, "base corpus"), f"base corpus {corpus_path}"):
            if isinstance(row, dict):
                known.add(_key(row))

    kept: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    already_covered = 0
    self_duplicates = 0
    for row in raw_rows:
        key = _key(row)
        if key in known:
            already_covered += 1
            continue
        if key in seen:
            self_duplicates += 1
            continue
        seen.add(key)
        kept.append(row)

    summary = {
        "proposed": len(raw_rows),
        "already_in_corpus": already_covered,
        "duplicate_candidates": self_duplicates,
        "to_adjudicate": len(kept),
    }

    if not kept:
        print(json.dumps(summary, indent=2))
        print(
            "Every candidate is already a committed golden path -- nothing new to "
            "adjudicate. The denial differential already gates these rows.",
            file=sys.stderr,
        )
        return 3

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"golden_paths": kept}, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


#: Count fields the summary folds arithmetically. Each is typed on the way in
#: because the fold happens in the middle of rendering: a field of the wrong type
#: raises there, and an exception escaping ``verdict`` lands on Python's own exit
#: 1, which is this lane's claim that a regression was confirmed.
_NUMERIC_COUNTS = (
    "total_rows",
    "classified",
    "skipped_kind",
    "skipped_platform",
    "regressions",
    "unchanged_allowed",
)

#: List-valued count fields. ``base_absent_tiers`` holds tier NAMES, and that is
#: the specific drift this check closes: the differential reports names, and a
#: reader that takes them for a count turns a change which merely ADDS a deny
#: check into a confirmed regression.
_LIST_COUNTS = ("base_absent_tiers",)


def _report_leg(path: Path) -> dict[str, Any]:
    """One ``deny_diff --json`` report, validated enough to be summarized.

    A report whose shape is not what this reader expects is exit 2, not an empty
    leg: a leg silently read as "no regressions" is the exact false green the
    fail-closed rule exists for.

    Every field the summary READS is typed here, not only the containers holding
    them. The alternative is a type error raised while rendering, which is an
    unsettled question wearing the exit code of a finding.
    """
    payload = _read_json(path, "differential report")
    if not isinstance(payload, dict):
        raise CandidateError(f"differential report {path} is not an object")
    for field in ("platform", "counts", "regressions"):
        if field not in payload:
            raise CandidateError(f"differential report {path} has no '{field}'")
    if not isinstance(payload["counts"], dict) or not isinstance(payload["regressions"], list):
        raise CandidateError(f"differential report {path} has a malformed 'counts'/'regressions'")
    counts = payload["counts"]
    for field in _NUMERIC_COUNTS:
        value = counts.get(field)
        if value is None:
            continue
        # ``bool`` is an ``int`` to isinstance, and a true/false where a tally
        # belongs is a report this reader must not fold into a number.
        if isinstance(value, bool) or not isinstance(value, int):
            raise CandidateError(
                f"differential report {path} has a non-numeric '{field}' in 'counts': "
                f"{type(value).__name__}"
            )
    for field in _LIST_COUNTS:
        value = counts.get(field)
        if value is not None and not isinstance(value, list):
            raise CandidateError(
                f"differential report {path} has a '{field}' in 'counts' that is not a list: "
                f"{type(value).__name__}"
            )
    return payload


def _split_leg_arg(value: str) -> tuple[str, Path]:
    """``LABEL=PATH`` into its two halves. The label is REQUIRED.

    The label is what tells two legs apart, and it has to, because more than one
    host maps to a single corpus ``platform``: two lines reading ``posix`` leave a
    reader unable to say which OS reported which, and unable to tell a duplicated
    leg from an absent one.

    Both callers always pass one (``--report "$leg=$f"`` in each lane), so
    requiring it costs no invocation and needs no disambiguation rule. A bare
    form has to decide whether a prefix holding a path separator means "this
    ``=`` belongs to a path, not a label" -- a rule a caller must know before
    they can predict what their own argument means. One accepted shape needs no
    such rule.
    """
    label, separator, rest = value.partition("=")
    if not separator or not label or not rest:
        raise CandidateError(
            f"--report {value!r} must be LABEL=PATH -- the label is what names this "
            "leg, and two hosts that share one corpus platform are told apart by "
            "nothing else"
        )
    return label, Path(rest)


#: The corpus platform values a row can be pinned to, `any` excluded: a row marked
#: `any` is eligible on every leg, so it cannot be the row nobody covered. Mirrors
#: `deny_diff._PLATFORMS`; a new platform there needs a leg here.
_CONCRETE_PLATFORMS = ("posix", "windows")


def verdict(
    report_args: list[str],
    expect_legs: list[str],
    out_md: Path | None,
    out_rows: Path | None,
) -> int:
    """Fold the per-leg reports into one verdict and one review body.

    Four properties earn their place here:

    *No report is exit 2.* A lane with nothing to read has measured nothing. The
    caller must not be able to reach a green by failing to produce a report.

    *A leg the caller required and did not hand over is exit 2, by name.* This
    fold sees only the reports it was given, so absence is invisible to it unless
    it is declared: a leg whose job died uploads nothing, one surviving leg
    classifies rows, and a platform that never reported reads as clean. The
    ``--expect-leg`` labels are that declaration.

    *A row nothing could classify is exit 2.* ``deny_diff`` counts a row it cannot
    classify into ``skipped_kind`` and settles nothing for it, while the leg still
    reports its other rows as classified. Folding that leg as measured publishes a
    verdict over a candidate that never got one, so the unsettled rows stop the
    fold instead of being averaged away.

    *A row NO reporting leg was eligible for is exit 2.* A row whose platform no leg
    reported for is skipped by every leg and classified nowhere, while the per-leg
    check above is satisfied by the rows the other legs did classify. The
    counterpart matters as much: a leg that classified nothing because the corpus
    holds no row for its platform is honest, so this is per ROW and never "exit 2
    when a leg classified zero".

    *A leg that classified nothing is reported as NO VERDICT*, never as a pass.
    Every row skipped on platform grounds means this host was asked about rows that
    do not apply to it, and the platform the diff actually touched may be the one
    with no verdict at all.
    """
    if not report_args:
        raise CandidateError("no differential report given -- nothing was measured")

    legs: list[tuple[str, dict[str, Any]]] = []
    for value in report_args:
        label, path = _split_leg_arg(value)
        legs.append((label, _report_leg(path)))

    handed_over = {label for label, _ in legs}
    missing = [label for label in expect_legs if label not in handed_over]
    if missing:
        raise CandidateError(
            "no differential report for required leg(s) "
            f"{', '.join(missing)} -- handed over: "
            f"{', '.join(sorted(handed_over)) or 'none'}. A leg that did not report "
            "has measured nothing, and folding the rest would pass its platform "
            "without asking it"
        )

    unsettled = [
        f"{label}: {int(report['counts'].get('skipped_kind', 0) or 0)} row(s)"
        for label, report in legs
        if int(report["counts"].get("skipped_kind", 0) or 0)
    ]
    if unsettled:
        raise CandidateError(
            "a candidate the classifier could not classify was submitted "
            f"({'; '.join(unsettled)}) -- the differential settles nothing for such "
            "a row, so it is an open question and not a row that passed"
        )

    # Fail closed on a run where NOTHING was classified. Every leg reporting zero
    # classified rows means no host was asked a question it could answer -- the
    # markdown says NO VERDICT, and an exit 0 beside it would publish a green badge
    # over an unmeasured change, which is this lane's worst failure mode.
    if not any(int(report["counts"].get("classified", 0) or 0) for _, report in legs):
        raise CandidateError(
            "no leg classified a single candidate -- nothing was measured, so there "
            "is nothing to pass"
        )

    # Fail closed on a corpus ROW that no reporting leg was eligible for. The check
    # above is per LEG and `any()` is satisfied by the rows the other legs did
    # classify, so a row on a platform nobody reported for is skipped by every leg,
    # classified nowhere, and folded into a green badge -- a verdict over a row no
    # host was ever asked about.
    #
    # It is deliberately NOT "exit 2 when a leg classified zero". A leg reaching
    # here with `classified == 0` had every row skipped as OTHER-PLATFORM (a
    # `skipped_kind` row already raised above), which is the honest report for a
    # single-platform corpus -- refusing on it would red every run whose corpus
    # holds no row for one of the legs.
    #
    # The corpus itself does not travel to the fold (the legs read it, the fold
    # reads only their reports), so coverage is derived from the counts: with
    # `skipped_kind == 0` every row is a shell row, so a leg on platform `p`
    # skipped exactly the rows whose platform is a concrete one other than `p`.
    # If no leg reported for that platform, those rows are the uncovered ones.
    covered = {
        str(report.get("platform", "")).strip()
        for _, report in legs
        if str(report.get("platform", "")).strip() in _CONCRETE_PLATFORMS
    }
    for label, report in legs:
        skipped = int(report["counts"].get("skipped_platform", 0) or 0)
        if not skipped:
            continue
        platform = str(report.get("platform", "")).strip()
        if platform in _CONCRETE_PLATFORMS:
            # This leg classified the rows pinned to its own platform plus the `any`
            # rows, so what it skipped is exactly the rows pinned to the OTHER
            # concrete platforms. Each of those needs a leg that reported for it.
            uncovered = sorted(p for p in _CONCRETE_PLATFORMS if p != platform and p not in covered)
        elif covered:
            # A leg that reported for no concrete platform (`deny_diff --platform
            # any`) skipped rows pinned to concrete platforms, but its aggregate
            # count cannot say WHICH one. Attributing it would refuse a corpus whose
            # rows are in fact all covered: three posix rows, a posix leg that
            # classifies them and an `any` leg that skips all three reads as an
            # uncovered WINDOWS row that does not exist. So such a leg can only
            # prove an uncovered row when no concrete leg reported at all.
            continue
        else:
            uncovered = sorted(_CONCRETE_PLATFORMS)
        if not uncovered:
            continue
        raise CandidateError(
            f"leg {label} (platform {platform or '?'}) skipped {skipped} corpus row(s) on "
            f"platform grounds and no leg reported for {', '.join(uncovered)} -- those "
            "rows are classified nowhere, so folding this run would publish a verdict "
            "over a candidate no host was asked about. Add a reporting leg for "
            f"{', '.join(uncovered)}, or drop the row(s) from the corpus"
        )

    lines: list[str] = ["### Security scope review -- adjudicated candidates", ""]
    confirmed: list[dict[str, Any]] = []
    for label, report in legs:
        counts = report["counts"]
        platform = str(report.get("platform", "?"))
        # The label says which host reported; the corpus platform says which rows
        # that host was eligible to classify. Both, because a label alone hides
        # what the leg could see, and a platform alone renders two hosts as one
        # line -- and an absent leg then looks like a duplicated one.
        named = label if label == platform else f"{label} (platform {platform})"
        classified = int(counts.get("classified", 0) or 0)
        regressions = report["regressions"]
        if classified == 0:
            lines.append(
                f"- **{named}: NO VERDICT** -- {counts.get('total_rows', 0)} rows in the "
                f"corpus, none classified on this host "
                f"(skipped on platform: {counts.get('skipped_platform', 0)}, "
                f"on kind: {counts.get('skipped_kind', 0)}). "
                "Not a pass for this platform."
            )
        else:
            lines.append(
                f"- **{named}**: {classified} classified, "
                f"**{len(regressions)} newly refused**, "
                f"{counts.get('unchanged_allowed', 0)} still allowed."
            )
        # The names, not a tally: the name is what tells a reader WHICH check is
        # new, and a tier absent at the base ref is why a whole tier's worth of
        # rows can turn up refused at once.
        absent_tiers = [str(tier) for tier in counts.get("base_absent_tiers") or []]
        if absent_tiers:
            lines.append(
                f"    - Deny check(s) absent at the base ref: {', '.join(absent_tiers)} -- "
                "this change introduces them, so their refusals are new by construction."
            )
        for row in regressions:
            entry = dict(row)
            entry["leg_label"] = label
            confirmed.append(entry)

    if confirmed:
        lines += ["", "#### Newly refused (confirmed by the real classifier at both refs)", ""]
        for row in confirmed:
            lines += [
                f"- `{row.get('command')}` ({row.get('platform')}, leg {row.get('leg_label')})",
                f"    - Who loses it: {row.get('why_legitimate')}",
                f"    - Refused by: **{row.get('head_tier') or 'unreported tier'}** -- "
                f"{row.get('head_refusal')}",
            ]
        lines += [
            "",
            "Each row above is an operation that the base ref ALLOWS and this change "
            "REFUSES. Narrow the rule at the tier named. Withdrawing a row is not the "
            "way to green: that is its own pull request, on its own merits.",
        ]
    else:
        lines += ["", "No candidate flipped from allowed to refused."]

    body = "\n".join(lines) + "\n"
    print(body, end="")
    if out_md is not None:
        out_md.write_text(body, encoding="utf-8")

    if out_rows is not None:
        # Paste-ready, in the committed corpus's own shape: a confirmed regression is
        # exactly the row the corpus was missing. Proposed only -- adding it is a
        # reviewed edit with an approver, never this script's to make.
        #
        # De-duplicated by the corpus's own identity triple (kind, command,
        # platform). A `platform: any` regression is confirmed by EVERY reporting
        # leg, so `confirmed` holds one entry per leg for it, while the committed
        # corpus holds each row once, keyed on that triple. Emitting one row per leg
        # writes a corpus that is a multiple of itself -- three legs turn a single
        # `any` regression into three identical rows -- which no human can paste as
        # a corpus, and which inflates any row count taken off this file to a
        # function of leg count rather than of distinct operations.
        rows: list[dict[str, Any]] = []
        seen_rows: set[tuple[str, str, str]] = set()
        for row in confirmed:
            entry = {
                "kind": row.get("kind", "shell"),
                "command_or_flow": row.get("command"),
                "platform": row.get("platform", "any"),
                "reason": row.get("why_legitimate", ""),
            }
            identity = (
                str(entry["kind"]),
                str(entry["command_or_flow"]),
                str(entry["platform"]),
            )
            if identity in seen_rows:
                continue
            seen_rows.add(identity)
            rows.append(entry)
        out_rows.write_text(json.dumps({"golden_paths": rows}, indent=2), encoding="utf-8")

    return 1 if confirmed else 0


# --------------------------------------------------------------------------- #
# conclude -- THE conclusion table, shared by BOTH review lanes.
# --------------------------------------------------------------------------- #
#
# The same-repo lane (.github/workflows/security-scope-review.yml) and the fork
# lane (.github/workflows/fork-security-scope-review.yml) both fold the same four
# signals -- the differential's fold result, the model's ``Scope-Verdict``
# header, whether the review marked THIS head, and whether a platform gap was
# demonstrated -- into one lane conclusion. This function is the ONE table both
# lanes call, so neither lane can drift and a fork can never be the more
# permissive one. A per-lane ``case``/``if`` ladder in each ``run:`` body cannot
# hold that: two copies drift on the platform-gap signal, one reading the
# SCRIPT's body alone while the other also honours a model-authored
# ``UNADJUDICATED:`` marker in the review prose, and the identical unadjudicable
# tightening then publishes ``neutral`` on a fork while it BLOCKS same-repo --
# the softer verdict against the more hostile source.
#
# It returns a ``(conclusion, why)`` pair. ``conclusion`` is one of a fixed
# vocabulary each lane maps to its own surface (a badge + exit code on same-repo,
# a check-run conclusion on the fork); ``why`` is a human line for the comment or
# annotation. This is a PURE DOWNSTREAM MAPPING: it never confirms a regression
# and never spends this script's exit-1/exit-2 contract on its own account. A
# regression is already exit 1 from ``verdict``; ``conclude`` exits 0 having
# printed its answer, and only a bad flag is argparse's exit 2 -- which is the
# correct fail-closed "could not settle".

#: The conclusion for a run that could not produce a SETTLED verdict -- a fold
#: that errored, a row that had to be redacted, a leg that never reported, or a
#: review that left no ``[SCOPE-REVIEWED]`` marker for this head.
#:
#: THIS ONE CONSTANT IS THE FLIP POINT for the question of whether the lane
#: should fail CLOSED or resolve NEUTRAL when the run errors across a Bedrock call
#: plus a 3-OS matrix. It is ``"error"``: fail CLOSED, chosen deliberately and on
#: three grounds, not left at a default nobody ruled on. (1) It is the answer this
#: lane exists for -- an unmeasured tightening that reads "nothing newly refused"
#: is the exact failure the lane was built to catch, so the lane must not commit
#: it about itself. (2) The population that pays the noise cost is bounded to the
#: population that needs the strictness: ``generate``'s "Resolve review scope"
#: step writes ``in_scope=false`` for a change outside the security surface, and
#: an off-surface PR therefore skips this lane without spending a Bedrock call, so
#: an outage or a flaky matrix leg cannot red a PR the lane would not have judged.
#: (3) It matches the two line-level reviewer lanes, which ``docs/ci/
#: ci-and-reviews.md`` already documents as fail-closed; a security lane that
#: resolved softer than them would be the weakest link in the same rollup.
#: Each lane maps ``error`` to a FAILING check
#: (exit 1 / ``failure``) and to a leak-safe "could not settle" notice rather than
#: a "confirmed regression" badge. To make EVERY unsettled run resolve neutral
#: instead -- the other candidate answer -- change this single value to
#: ``"concerns"``; both lanes already map ``concerns`` to a non-blocking neutral.
#: Nothing else moves. Note the breadth: this governs every could-not-settle
#: outcome below, marker-absent included, not only a Bedrock/matrix crash.
_UNSETTLED_CONCLUSION: str = "error"

#: The blocking severity of a lane conclusion -- ``pass`` the softest, the two
#: reds the hardest. This is the ONE ranking of "harder": the model-gap property
#: in ``conclude_lane`` and the per-(base, head) monotonic guard both read it, so
#: neither drifts onto a second ranking that disagrees with the other.
_CONCLUSION_SEVERITY: "dict[str, int]" = {
    "pass": 0,
    "nothing-new": 1,
    "concerns": 2,
    "error": 3,
    "block": 3,
}

#: A published check-run conclusion, named as the lane token whose blocking
#: severity it carries. ``neutral`` is a ``concerns`` -- the stricter of the two
#: tokens that surface as neutral -- so the monotonic guard reads a neutral as no
#: softer than it stands. A conclusion absent here (``cancelled``, ``timed_out``,
#: ``skipped``) is not a lane verdict and sets no floor.
_CHECKRUN_CONCLUSION_TOKEN: "dict[str, str]" = {
    "success": "pass",
    "neutral": "concerns",
    "failure": "block",
}

#: The ``external_id`` prefix the fork lane stamps onto the check-run it POSTs,
#: with the pull-request number appended. It carries BOTH dimensions the floor
#: needs: the pull request a row belongs to, and the lane that wrote it. A row
#: carrying it is a fork-lane row; a row without it, under this same check name,
#: is the same-repo lane's own job conclusion.
_FORK_EXTERNAL_ID_PREFIX = "scope-pr-"

#: The base-authored marker the fork publish job prefixes onto its check-run's
#: ``output.title`` when the run COULD NOT MEASURE -- a Bedrock outage, an absent
#: harness, a fold that errored, a review with no ``[SCOPE-REVIEWED]`` marker, or
#: a floor computation that itself died. Such a run still publishes ``failure`` to
#: red ITS OWN run (``_UNSETTLED_CONCLUSION`` is unchanged), but it measured no
#: regression, so it must set no per-head floor: without this distinction one
#: transient flake reds the head permanently and a re-run cannot clear it, because
#: the flake becomes the floor. The fork publish job holds ``checks: write`` and
#: writes this title from committed base code, so the marker is a signal model
#: prose cannot forge; the reader anchors it to the START of the title, the bytes
#: the publish step writes before any model-derived text. Only the fork lane can
#: stamp it -- the same-repo lane's prior IS the publish job's own conclusion,
#: created by the API, so it carries no settable output and stays strict.
_SCOPE_FLOOR_UNSETTLED_MARKER = "[scope-floor:unsettled]"

#: The check-run conclusion each lane surfaces a token as. Both lanes map these
#: the same way; the monotonic guard fails closed by surfacing
#: ``_UNSETTLED_CONCLUSION`` through this map, so there is no second fail-closed
#: knob to keep in step with the constant above.
_TOKEN_CHECKRUN_CONCLUSION: "dict[str, str]" = {
    "pass": "success",
    "nothing-new": "neutral",
    "concerns": "neutral",
    "block": "failure",
    "error": "failure",
}

#: The fold-result vocabulary ``conclude`` accepts. Each lane normalizes its own
#: native fold encoding (same-repo's ``$FOLDED`` word, the fork's ``$FOLD_RC``)
#: down to one of these before calling, so the table sees one shape from both.
_FOLD_CHOICES = ("clean", "regression", "redacted", "unscrubbable", "error", "no-report")


def _truthy(value: "str | None") -> bool:
    """A lenient boolean for shell-supplied flags.

    ``present`` is accepted alongside ``true`` so the marker flag can pass its own
    word through unchanged.
    """
    return str(value or "").strip().lower() in {"true", "1", "yes", "on", "present"}


def _normalize_model(value: "str | None") -> str:
    """Fold the raw ``Scope-Verdict`` header to one of PASS/CONCERNS/BLOCK/UNKNOWN.

    Anything the contract does not define -- an empty header, a typo, a value the
    lane could not parse -- is UNKNOWN, which the table treats as unsettled. This
    does NOT decide attribution: whether the header describes THIS head is the
    marker's job, kept separate so a present-but-unmarked header is not silently
    honoured.
    """
    header = str(value or "").strip().upper()
    return header if header in {"PASS", "CONCERNS", "BLOCK"} else "UNKNOWN"


def conclude_lane(
    *,
    lane: str,
    fold: str,
    nothing_new: bool,
    marker_present: bool,
    model: str,
    gap_script: bool,
    gap_model: bool,
) -> "tuple[str, str]":
    """Fold the lane signals into one ``(conclusion, why)`` -- the shared table.

    ``lane`` is accepted and validated but DELIBERATELY not branched on: the
    ruling that produced this table resolved the one asymmetry to the stricter
    side, so there is no row where a fork and a same-repo run of the same inputs
    reach different conclusions. The parameter stays because the two lanes must
    pass it (it names the caller and reserves the seam), and because the contract
    is that any future lane difference is a ROW here selected by ``lane`` -- never
    a branch back in a YAML ``run:`` body.
    """
    if lane not in ("fork", "same-repo"):
        raise CandidateError(f"unknown lane {lane!r} -- expected 'fork' or 'same-repo'")

    # gap_model is MODEL-AUTHORED input to a merge-blocking gate, so it is
    # UNTRUSTED. It is OR-ed in here and read ONLY inside the BLOCK branch below,
    # where a gap can do exactly one thing: upgrade concerns -> block. It can make
    # the verdict STRICTER, never clear it -- so honouring it costs no safety while
    # closing the asymmetry (same-repo already honoured it; the fork did not, and a
    # fork is the more hostile source). Both sources feed both lanes now.
    gap = gap_script or gap_model
    if gap_script:
        gap_src = "a platform obtained no verdict"
    elif gap_model:
        gap_src = "the review marked a guard unadjudicated"
    else:
        gap_src = ""

    # 1. A run that produced no settle-able verdict fails closed. Order matches
    #    both shell ladders: a redacted row is read FIRST, because it names no
    #    operation and neither a green nor a regression can be published off it.
    if fold == "redacted":
        return _UNSETTLED_CONCLUSION, "a confirmed row was redacted, so it names no operation"
    if fold == "unscrubbable":
        return _UNSETTLED_CONCLUSION, "the folded verdict could not be scrubbed, so it was not published"
    if fold == "error":
        return _UNSETTLED_CONCLUSION, "the per-platform reports could not be folded"

    # 2. THE HARD RULE: a script-confirmed regression reds the lane whatever the
    #    model wrote, on either lane. Never governed by the flip point above -- a
    #    confirmed newly-refused operation always blocks.
    if fold == "regression":
        return "block", "a script-confirmed newly-refused operation"

    # 3. No leg reported. The ONE green reading is the exit-3 short circuit: every
    #    candidate is already a committed golden path, so the denial differential
    #    already gates them. That is green only when the review marked THIS head,
    #    so "nothing new" is attributable to this revision -- the same bar
    #    same-repo already held and the fork did not. Every other no-report is an
    #    unmeasured run.
    if fold == "no-report":
        if nothing_new and marker_present:
            return "nothing-new", "every candidate is already a committed golden path"
        if nothing_new:
            return _UNSETTLED_CONCLUSION, (
                "nothing new to adjudicate, but no [SCOPE-REVIEWED] marker attributes it to this head"
            )
        return _UNSETTLED_CONCLUSION, (
            "no leg reported, and this was not the nothing-to-adjudicate short circuit"
        )

    # 4. fold == "clean": the legs folded to no confirmed regression. The model's
    #    header decides -- but only once the review is attributable to this head.
    if not marker_present:
        return _UNSETTLED_CONCLUSION, "no [SCOPE-REVIEWED] marker for this head"
    if model == "PASS":
        return "pass", "adjudicated, zero confirmed regressions"
    if model == "CONCERNS":
        return "concerns", "the reviewer's own judgement; no confirmed regression"
    if model == "BLOCK":
        if gap:
            return "block", f"the reviewer's BLOCK stands on a demonstrated platform gap ({gap_src})"
        return "concerns", "the reviewer wrote BLOCK with no confirmed regression and no demonstrated gap"
    return _UNSETTLED_CONCLUSION, "no parseable Scope-Verdict header carrying a marker for this head"


def _checkrun_severity(conclusion: "str | None") -> "int | None":
    """The blocking severity of a published check-run conclusion, or ``None`` for
    one that is not a lane verdict. Read THROUGH ``_CONCLUSION_SEVERITY`` so the
    guard shares the one ranking rather than carrying a parallel one.
    """
    token = _CHECKRUN_CONCLUSION_TOKEN.get(str(conclusion or "").strip().lower())
    return None if token is None else _CONCLUSION_SEVERITY[token]


class PriorReadError(Exception):
    """The prior check-run response cannot be trusted to say what priors exist."""


def _row_belongs_to(row: "dict[str, Any]", *, lane: str, pr: str) -> bool:
    """Is this check-run row THIS pull request's, on THIS lane?

    Two pull requests can share a head SHA, and both lanes publish under one
    check name, so a row is only a prior for this run when it matches on both
    dimensions.

    The fork lane stamps its own ``external_id`` (``scope-pr-<pr>-<run>-<attempt>``),
    which names the pull request and the lane in one field. The same-repo lane's
    check-run is the JOB's own conclusion, which the API creates rather than this
    code, so no ``external_id`` is settable there. Its pull-request binding comes
    from the API's own ``pull_requests`` array -- populated for a ``pull_request``
    event -- and its lane identity is the ABSENCE of the fork stamp, since the only
    other writer of this check name is the fork lane. That pair is sufficient: a
    row is admitted only when the API itself attributes it to this pull request,
    and a fork row is excluded by its own stamp.

    Raises ``PriorReadError`` when a same-repo row carries NO attribution, rather
    than reading it as another pull request's. See the comment at that raise: the
    silent reading is the fail-open mirror of this module's central defect.
    """
    external_id = str(row.get("external_id") or "")
    is_fork_row = external_id.startswith(_FORK_EXTERNAL_ID_PREFIX)
    if lane == "fork":
        return external_id.startswith(f"{_FORK_EXTERNAL_ID_PREFIX}{pr}-")
    if is_fork_row:
        return False
    numbers = row.get("pull_requests")
    if not isinstance(numbers, list) or not numbers:
        # NOT "belongs to another pull request" -- this row cannot be attributed at
        # all, and the two readings have opposite consequences. Dropping it means
        # "this head has no prior", which silently makes the floor INERT and lets a
        # re-run publish clean over a verdict the lane already stands behind: the
        # fail-OPEN mirror of the conflation this module exists to avoid. The caller
        # raises instead, so an unattributable row is a could-not-settle outcome.
        #
        # Observed behaviour today is that the API populates this array for a
        # `pull_request`-event check-run (captured for this lane: `pull_requests`
        # `[9984]`, `external_id` a UUID). This guard is why that observation does
        # not have to keep holding.
        raise PriorReadError(
            "a completed check-run for this head names no pull request, so whether "
            "it is this pull request's prior verdict cannot be decided"
        )
    return any(str((entry or {}).get("number")) == str(pr) for entry in numbers if isinstance(entry, dict))


def _fork_row_is_unsettled(row: "dict[str, Any]") -> bool:
    """Does this fork-lane check-run mark ITSELF as a run that could not measure?

    The fork publish job authors its own check-run and writes ``output.title``
    from committed base code, so it can carry one bit model prose cannot forge:
    a run that could not settle prefixes the title with
    ``_SCOPE_FLOOR_UNSETTLED_MARKER``. That row published ``failure`` to red its
    OWN run, but it confirmed no regression, so it must set NO floor -- otherwise
    one flake reds the head forever and a re-run cannot clear it. The match is
    anchored to the START of the title, the bytes the publish step writes before
    any model-derived ``why``, so the same marker echoed later in model prose
    cannot forge it. Absence reads as SETTLED, so an untagged row -- an older
    run, or a response that omitted ``output`` -- keeps its floor (fail closed).
    """
    output = row.get("output")
    title = str(output.get("title") or "") if isinstance(output, dict) else ""
    return title.lstrip().startswith(_SCOPE_FLOOR_UNSETTLED_MARKER)


def select_prior_conclusions(payload_text: str, *, lane: str, pr: str) -> "list[str]":
    """The completed conclusions this run stands behind, read from ONE API response.

    ``payload_text`` is the raw ``GET /repos/{repo}/commits/{sha}/check-runs``
    body. The envelope is validated before anything is read out of it: a
    well-formed "no check-runs yet" answer (an object whose ``total_count`` is a
    number and whose ``check_runs`` is a list) is the ONLY way an empty prior set
    is reached. An absent body, a truncated body, a body that is not an object,
    or one missing either field raises ``PriorReadError`` -- the caller routes that
    through the one fail-closed constant, because "the response said nothing" and
    "the response said there is nothing" are different answers and only the second
    may publish clean.

    ``total_count`` is reconciled against the number of rows in hand, so a page
    that holds fewer rows than the listing claims raises rather than ranking a
    partial set. A truncated page reads as a SOFTER prior set than the head
    really carries, which is the one direction the floor may not be wrong in:
    the row naming a block can be the row that fell off the page. This is why
    the caller asks for one page and reconciles instead of paginating -- ``gh api
    --paginate`` concatenates one envelope per page, which is not a single
    envelope any of the checks above can read.
    """
    if lane not in ("fork", "same-repo"):
        raise CandidateError(f"unknown lane {lane!r} -- expected 'fork' or 'same-repo'")
    if not payload_text.strip():
        raise PriorReadError("the prior check-run response is empty, so it names no check-runs either way")
    try:
        payload = json.loads(payload_text)
    except (ValueError, TypeError) as exc:
        raise PriorReadError(f"the prior check-run response is not readable JSON ({exc})") from exc
    if not isinstance(payload, dict):
        raise PriorReadError("the prior check-run response is not an object, so its envelope cannot be read")
    if not isinstance(payload.get("total_count"), int) or isinstance(payload.get("total_count"), bool):
        raise PriorReadError("the prior check-run response carries no numeric total_count, so it is not a check-run listing")
    rows = payload.get("check_runs")
    if not isinstance(rows, list):
        raise PriorReadError("the prior check-run response carries no check_runs list, so it is not a check-run listing")
    total = payload["total_count"]
    if total != len(rows):
        raise PriorReadError(
            f"the prior check-run listing claims {total} check-run(s) and carries {len(rows)}, "
            "so the page is partial and a prior block can be missing from it"
        )
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").strip().lower() != "completed":
            continue
        if not _row_belongs_to(row, lane=lane, pr=pr):
            continue
        if lane == "fork" and _fork_row_is_unsettled(row):
            # A fork run that could not measure reds its own run but stands
            # behind no regression, so it is not a prior the floor holds to.
            continue
        conclusion = row.get("conclusion")
        if conclusion is not None:
            out.append(str(conclusion))
    return out


def monotonic_conclusion(
    *, current: str, priors: "list[str]", prior_readable: bool
) -> "tuple[str, str]":
    """Raise a run's conclusion to the per-(base, head) floor.

    ``current`` is the check-run conclusion THIS run computed. ``priors`` are the
    lane's OWN completed check-run conclusions for the same head. The lane may
    never publish a conclusion softer than one it already stands behind for this
    head, so the answer is the harder of ``current`` and the hardest prior.

    Check-runs are per-SHA, so a new head carries no priors and starts clean --
    new code earns a fresh measurement. A failure to READ the priors is a
    could-not-settle outcome and routes through ``_UNSETTLED_CONCLUSION``, the one
    fail-closed constant, rather than passing on an unknown.
    """
    unsettled = _TOKEN_CHECKRUN_CONCLUSION[_UNSETTLED_CONCLUSION]
    cur = str(current or "").strip().lower()
    cur_sev = _checkrun_severity(cur)
    if cur_sev is None:
        return unsettled, f"this run computed no ranked conclusion ({current!r}), so it fails closed"
    if not prior_readable:
        harder = unsettled if (_checkrun_severity(unsettled) or 0) > cur_sev else cur
        return harder, "the prior conclusion for this head could not be read, so the lane fails closed"
    hardest = ""
    hardest_sev = -1
    for candidate in priors:
        sev = _checkrun_severity(candidate)
        if sev is not None and sev > hardest_sev:
            hardest = str(candidate).strip().lower()
            hardest_sev = sev
    if hardest_sev > cur_sev:
        return hardest, f"a prior run of this head stands at {hardest!r}, harder than this run's {cur!r}"
    return cur, f"this run's {cur!r} is at least as hard as any prior conclusion for this head"


def _cmd_conclude(args: argparse.Namespace) -> int:
    conclusion, why = conclude_lane(
        lane=args.lane,
        fold=args.fold,
        nothing_new=_truthy(args.nothing_new),
        marker_present=_truthy(args.marker),
        model=_normalize_model(args.model),
        gap_script=_truthy(args.gap_script),
        gap_model=_truthy(args.gap_model),
    )
    # Printed as GitHub ``key=value`` lines so the caller can redirect stdout
    # straight into ``$GITHUB_OUTPUT``. Both the vocabulary and the why are
    # authored HERE, never interpolated from caller input, so neither line can
    # carry a newline or a fork-controlled value into that file.
    print(f"conclusion={conclusion}")
    print(f"why={why}")
    # Whether this run MEASURED its verdict, answered HERE rather than inferred by
    # a caller from the token. The fork lane needs the answer to decide whether to
    # stamp `_SCOPE_FLOOR_UNSETTLED_MARKER` on its check-run, and it read the token
    # to get it -- which silently breaks the "flip one constant" contract on
    # `_UNSETTLED_CONCLUSION`: set that to `"concerns"` and an unsettled run starts
    # arriving as a token the caller's `concerns` arm treats as settled, so flakes
    # would floor the head again with no other edit in sight. The comparison lives
    # next to the constant instead.
    print(f"settled={'no' if conclusion == _UNSETTLED_CONCLUSION else 'yes'}")
    return 0


def _cmd_monotonic(args: argparse.Namespace) -> int:
    # The RAW check-run listing for this head arrives on stdin, exactly as the
    # Checks API returns it. Envelope validation, per-pull-request and per-lane
    # selection, and the ranking that picks the hardest all live HERE, so the
    # caller's shell hands over a response rather than a verdict and the two lanes
    # cannot disagree about which rows count.
    readable = _truthy(args.prior_readable)
    priors: list[str] = []
    detail = ""
    if readable:
        try:
            priors = select_prior_conclusions(sys.stdin.read(), lane=args.lane, pr=args.pr)
        except PriorReadError as exc:
            readable = False
            detail = str(exc)
    conclusion, why = monotonic_conclusion(
        current=args.current,
        priors=priors,
        prior_readable=readable,
    )
    if detail:
        why = f"{why} ({detail})"
    print(f"conclusion={conclusion}")
    print(f"why={why}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="normalize a candidate file into a corpus")
    v.add_argument("--candidates", required=True, type=Path, help="the model's candidate JSON")
    v.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="the BASE golden-paths corpus, to drop candidates it already holds",
    )
    v.add_argument("--out", required=True, type=Path, help="where to write the normalized corpus")

    d = sub.add_parser("verdict", help="fold the per-leg differential reports into one verdict")
    d.add_argument(
        "--report",
        action="append",
        default=[],
        metavar="LABEL=PATH",
        help=(
            "a deny_diff --json report under its leg LABEL; repeat once per leg. The "
            "LABEL is required: two hosts can share one corpus 'platform', and the "
            "label is the only thing that tells those two legs apart"
        ),
    )
    d.add_argument(
        "--expect-leg",
        action="append",
        default=[],
        metavar="LABEL",
        help=(
            "a leg label that MUST be among the reports; a missing one is exit 2, "
            "because a leg that never reported cannot be folded into a pass"
        ),
    )
    d.add_argument("--out-md", type=Path, default=None, help="write the review body here")
    d.add_argument(
        "--out-rows",
        type=Path,
        default=None,
        help="write the confirmed rows as a paste-ready golden_paths corpus",
    )

    c = sub.add_parser("conclude", help="map the fold + model signals to ONE lane conclusion")
    c.add_argument("--lane", required=True, choices=["fork", "same-repo"])
    c.add_argument(
        "--fold",
        required=True,
        choices=list(_FOLD_CHOICES),
        help="the fold result, normalized to the table's vocabulary by the caller",
    )
    c.add_argument(
        "--model",
        default="",
        help="the parsed Scope-Verdict header (PASS/CONCERNS/BLOCK); anything else reads as UNKNOWN",
    )
    c.add_argument(
        "--marker",
        default="absent",
        help="'present' when the review left a [SCOPE-REVIEWED] marker for THIS head",
    )
    c.add_argument(
        "--nothing-new",
        dest="nothing_new",
        default="false",
        help="true when validate short-circuited (exit 3): every candidate is already a committed golden path",
    )
    c.add_argument(
        "--gap-script",
        dest="gap_script",
        default="false",
        help="true when a leg reported NO VERDICT -- the script's OWN demonstrated platform gap",
    )
    c.add_argument(
        "--gap-model",
        dest="gap_model",
        default="false",
        help=(
            "true when the review prose carries an UNADJUDICATED: marker -- model-authored, "
            "untrusted, and honoured ONLY where it makes the verdict stricter"
        ),
    )

    m = sub.add_parser(
        "monotonic",
        help="raise a run's check-run conclusion to the per-(base, head) floor",
    )
    m.add_argument(
        "--current",
        required=True,
        help="the check-run conclusion THIS run computed (success/neutral/failure)",
    )
    m.add_argument("--lane", required=True, choices=["fork", "same-repo"])
    m.add_argument(
        "--pr",
        required=True,
        help="the pull-request number this run belongs to; a row naming another PR is not a prior",
    )
    m.add_argument(
        "--prior-readable",
        dest="prior_readable",
        default="true",
        help=(
            "false when the API call for this head's check-runs did not answer at "
            "all; the guard then fails closed rather than passing on an unknown"
        ),
    )

    parsed = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if parsed.command == "validate":
            return validate(parsed.candidates, parsed.corpus, parsed.out)
        if parsed.command == "conclude":
            return _cmd_conclude(parsed)
        if parsed.command == "monotonic":
            return _cmd_monotonic(parsed)
        return verdict(parsed.report, parsed.expect_leg, parsed.out_md, parsed.out_rows)
    except CandidateError as exc:
        print(f"scope_candidates: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 -- the breadth IS the rule, see below
        # An exit code is a statement, and only one of them is a finding: exit 1
        # says the classifier confirmed a newly-refused operation. An exception
        # left to escape gets Python's own exit 1 and makes that claim about a
        # change nobody measured -- a false BLOCK from the lane whose whole
        # purpose is preventing false refusals. So every failure that is not that
        # finding, defects in this script included, ends as exit 2: a crash is an
        # unsettled question, never evidence.
        print(f"scope_candidates: internal error, nothing was settled: {exc!r}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
