#!/usr/bin/env python3
"""release_record.py -- the one definition of an override's class and a deferral's issue test.

Two workflows decide things this file defines, and before it they each carried their own copy:

* ``ai-review-human-override.yml`` reads ``/ai-review override <lane> <sha> <class>[ #N]: <reason>``
  and records the decision.
* ``disposition-deferral-check.yml`` asks whether the issue a deferral names is a real promise.

``parse_command`` owns the grammar and the per-class requirements. ``issue_problems`` owns the four
tests a deferral issue must pass: open, labelled ``deferred-finding``, assigned, dated.
``render_record`` owns the hidden second line of the bot comment.

Values reach this file through the environment or stdin, never through interpolation into a
command line, so a comment body cannot become shell text. The module is stdlib only because the
handler fetches it from the default branch into a bare runner.

Command line (each reads its input from the environment or stdin and prints on stdout):

    parse             COMMENT_BODY [RELEASE_RECORD_TODAY=YYYY-MM-DD]  -> one JSON object
    issue-problems    an issue as JSON on stdin                       -> one problem per line
    render            CLASS [ISSUE] AUTHOR_IS_ACTOR                   -> the hidden record line
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timezone
from typing import NamedTuple

LANES = ("fable", "gpt", "design", "ux", "first-principles", "scope", "all")
CLASSES = (
    "false-positive",
    "not-applicable",
    "pre-existing",
    "evidence-supplied",
    "infra-failure",
    "reissue",
    "deferred",
    "owner-decision",
)
# The class a record gets when the command used the old grammar.
LEGACY_CLASS = "legacy"
# The old grammar (`/ai-review override <lane> <sha>: <reason>`) is accepted, with a warning,
# before this date and refused from it. Six weeks after the class record landed.
LEGACY_CUTOVER = date(2026, 12, 9)

MIN_REASON = 20
MAX_REASON = 500
DEFERRED_LABEL = "deferred-finding"

DOCS_ANCHOR = "docs/ci/ci-and-reviews.md#risk-release-records"
_LANE_ALT = "|".join(LANES)
_NEW_RE = re.compile(
    rf"/ai-review override (?P<lane>{_LANE_ALT}) (?P<sha>[0-9a-fA-F]{{7,40}})"
    r" (?P<cls>[a-z][a-z-]*)(?: #(?P<issue>[0-9]{1,9}))?:\s*(?P<reason>.+)"
)
_OLD_RE = re.compile(
    rf"/ai-review override (?P<lane>{_LANE_ALT}) (?P<sha>[0-9a-fA-F]{{7,40}}):\s*(?P<reason>.+)"
)
_URL_RE = re.compile(r"https?://\S+")
_RUN_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/actions/runs/[0-9]+")
# A `reissue` points at the earlier record by link or by its `source=<id>`.
_COMMENT_REF_RE = re.compile(r"issuecomment-[0-9]+|source=[0-9]+")
# An `owner-decision` points at the owner's decision comment, and only a link to a comment is that:
# a bare `source=<id>` names a record the handler wrote, not a decision a person made.
_DECISION_URL_RE = re.compile(
    r"https://github\.com/[\w.-]+/[\w.-]+/(?:pull|issues)/[0-9]+#issuecomment-[0-9]+"
)
_HEX_WORD_RE = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])")
_DUE_RE = re.compile(r"^ *Due: *[0-9]{4}-[0-9]{2}-[0-9]{2}", re.MULTILINE)

USAGE = (
    "Use `/ai-review override <fable|gpt|design|ux|first-principles|scope|all> <current-sha> "
    f"<class>[ #N]: <reason, {MIN_REASON} to {MAX_REASON} characters>`, where `<class>` is one of "
    + ", ".join(f"`{c}`" for c in CLASSES)
    + f". What each class needs: {DOCS_ANCHOR}."
)

# What a class needs beyond the reason length, in words the refusal can print.
_NEEDS = {
    "pre-existing": "an issue (`#N`) or a commit sha in the reason that shows it on `main`",
    "evidence-supplied": "a URL in the reason that points at the evidence",
    "infra-failure": "the failed workflow run's URL "
    "(`https://github.com/<owner>/<repo>/actions/runs/<id>`) in the reason",
    "reissue": "the earlier record's comment link (`...#issuecomment-<id>`) or `source=<id>` "
    "in the reason",
    "deferred": "the follow-up issue as `#N` in the command; it must be open, labelled "
    f"`{DEFERRED_LABEL}`, assigned and dated (`Due: YYYY-MM-DD`)",
    "owner-decision": "a link to the owner's decision comment "
    "(`https://github.com/<owner>/<repo>/pull/<n>#issuecomment-<id>`) in the reason",
}
# Classes that accept `#N` in the command at all.
_TAKES_ISSUE = ("deferred", "pre-existing")


class Command(NamedTuple):
    """A parsed override command, or the reason it was refused."""

    ok: bool
    notice: str = ""
    lane: str = ""
    sha: str = ""
    cls: str = ""
    issue: str = ""
    reason: str = ""
    legacy: bool = False
    warning: str = ""

    def as_dict(self) -> dict:
        data = self._asdict()
        data["class"] = data.pop("cls")
        return data


def _refuse(message: str) -> Command:
    return Command(ok=False, notice=f"AI-review override not recorded: {message} {USAGE}")


def _has_commit_sha(reason: str) -> bool:
    # A bare run of hex letters is a word ("defaced"), so a sha must carry a digit.
    return any(re.search(r"[0-9]", word) for word in _HEX_WORD_RE.findall(reason))


def _missing_requirement(cls: str, issue: str, reason: str) -> str:
    """The unmet extra requirement of ``cls``, or ``""``."""
    if cls == "deferred":
        met = bool(issue)
    elif cls == "pre-existing":
        met = bool(issue) or _has_commit_sha(reason)
    elif cls == "evidence-supplied":
        met = bool(_URL_RE.search(reason))
    elif cls == "infra-failure":
        met = bool(_RUN_URL_RE.search(reason))
    elif cls == "reissue":
        met = bool(_COMMENT_REF_RE.search(reason))
    elif cls == "owner-decision":
        met = bool(_DECISION_URL_RE.search(reason))
    else:
        met = True
    return "" if met else _NEEDS[cls]


def parse_command(body: str, today: date | None = None) -> Command:
    """Parse the FIRST line of a comment body as an override command."""
    today = today or datetime.now(timezone.utc).date()
    first_line = (body or "").split("\n", 1)[0].rstrip("\r")
    match = _NEW_RE.fullmatch(first_line)
    if match is None:
        old = _OLD_RE.fullmatch(first_line)
        if old is None:
            return _refuse("the command shape is not recognised.")
        return _parse_legacy(old, today)
    cls = match["cls"]
    if cls not in CLASSES:
        return _refuse(f"`{cls}` is not a release class.")
    issue = match["issue"] or ""
    reason = match["reason"].strip()
    if issue and cls not in _TAKES_ISSUE:
        return _refuse(f"class `{cls}` does not take an issue number.")
    if not MIN_REASON <= len(reason) <= MAX_REASON:
        return _refuse(f"the reason must be {MIN_REASON} to {MAX_REASON} characters.")
    missing = _missing_requirement(cls, issue, reason)
    if missing:
        return _refuse(f"class `{cls}` needs {missing}.")
    return Command(
        ok=True,
        lane=match["lane"],
        sha=match["sha"].lower(),
        cls=cls,
        issue=issue,
        reason=reason,
    )


def _parse_legacy(match: re.Match, today: date) -> Command:
    if today >= LEGACY_CUTOVER:
        return _refuse(
            f"the old command without a class was retired on {LEGACY_CUTOVER.isoformat()}."
        )
    reason = match["reason"].strip()
    if not reason:
        return _refuse("provide a reason.")
    if len(reason) > MAX_REASON:
        return _refuse(f"keep the reason to {MAX_REASON} characters or fewer.")
    warning = (
        "This override used the old command without a class. It was recorded as class "
        f"`{LEGACY_CLASS}`, and the old command stops working on {LEGACY_CUTOVER.isoformat()}. "
        "Name a class from now on. " + USAGE
    )
    return Command(
        ok=True,
        lane=match["lane"],
        sha=match["sha"].lower(),
        cls=LEGACY_CLASS,
        reason=reason,
        legacy=True,
        warning=warning,
    )


def issue_problems(issue: dict) -> list[str]:
    """Why ``issue`` is not a tracked deferral. Empty means it is.

    The four tests: open, labelled ``deferred-finding``, assigned, dated (a ``Due: YYYY-MM-DD``
    line in the body, or a milestone with a due date). The strings are the ones the deferral
    check has always printed.
    """
    if issue.get("pull_request"):
        return ["is a pull request, not an issue"]
    problems = []
    if issue.get("state") != "open":
        problems.append("closed")
    labels = issue.get("labels") or []
    names = {(label.get("name") if isinstance(label, dict) else label) for label in labels}
    if DEFERRED_LABEL not in names:
        problems.append(f"no {DEFERRED_LABEL} label")
    if not (issue.get("assignees") or []):
        problems.append("no assignee (owner)")
    milestone = issue.get("milestone") or {}
    if not _DUE_RE.search(issue.get("body") or "") and not milestone.get("due_on"):
        problems.append(
            "no due date (add a 'Due: YYYY-MM-DD' line to the issue body, "
            "or a milestone with a due date)"
        )
    return problems


def render_record(cls: str, issue: str, author_is_actor: bool) -> str:
    """The hidden second line of the bot comment. Every field comes from a closed set."""
    if cls not in CLASSES and cls != LEGACY_CLASS:
        raise ValueError(f"unknown class {cls!r}")
    if issue and not issue.isdigit():
        raise ValueError(f"issue must be digits, got {issue!r}")
    parts = [
        f"class={cls}",
        f"issue={issue or 'none'}",
        f"author_is_actor={'true' if author_is_actor else 'false'}",
    ]
    return "<!-- ai-review-override-record " + " ".join(parts) + " -->"


def _today_from_env() -> date | None:
    raw = os.environ.get("RELEASE_RECORD_TODAY", "")
    return date.fromisoformat(raw) if raw else None


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command == "parse":
        parsed = parse_command(os.environ.get("COMMENT_BODY", ""), _today_from_env())
        print(json.dumps(parsed.as_dict()))
        return 0
    if command == "issue-problems":
        try:
            issue = json.load(sys.stdin)
        except ValueError:
            print("issue-problems: stdin is not JSON", file=sys.stderr)
            return 2
        if not isinstance(issue, dict):
            print("issue-problems: stdin is not an issue object", file=sys.stderr)
            return 2
        for problem in issue_problems(issue):
            print(problem)
        return 0
    if command == "render":
        try:
            print(
                render_record(
                    os.environ.get("CLASS", ""),
                    os.environ.get("ISSUE", ""),
                    os.environ.get("AUTHOR_IS_ACTOR", "") == "true",
                )
            )
        except ValueError as exc:
            print(f"render: {exc}", file=sys.stderr)
            return 2
        return 0
    print("usage: release_record.py parse|issue-problems|render", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
