#!/usr/bin/env python3
"""PR Size: flag a very wide or long-open pull request, and record an owner's approval.

``.github/workflows/pr-size.yml`` (job ``PR Size``) calls ``check``;
``.github/workflows/pr-size-override.yml`` calls ``parse-approve``, ``authorize``
and ``render-comment``. The rules and numbers live here so a test can run them.

Why files and hours, not lines: of the 106 pull requests with 5,000 or more lines
that merged fast with at most one loop, 94 are splits, so lines flag work that is
fine. Files or days open flag the tail that loops: ``files >= 80 or open >= 168 h``
is 3.3% of the 1,980 studied pull requests and holds 26.9% of their rebase loops.
(docs/request-for-change/rfc-review-loop-and-escaped-bug-reduction.md, D6.)

The levels:

* **Hint** at ``FILES_HINT`` files: a notice only. The local twin is
  ``diff_signals.py`` in the kirocrew-prepare-pr skill, which prints ``! SIZE``.
* **Stop** at ``FILES_STOP`` files: red once ``PR_SIZE_ENFORCED`` is true and no owner
  has approved. Until then it prints a warning that says what would be red.
* **Age** at ``HOURS_AGING`` hours open: a notice. Never red, because waiting for a
  review is not the author's fault.

The owner override is a PR comment whose first line is
``/pr-size approve: <reason, 12 to 500 characters>``. The override workflow checks
the commenter's collaborator permission and posts a bot comment whose first line is
a hidden marker. This check trusts only a marker in a comment written by
``github-actions[bot]``; a marker typed by anyone else is ignored. An approval holds
while the file count stays at or below the recorded count plus 25% plus 10, so a PR
that doubles needs a new decision without every push asking again.

Subcommands (untrusted text arrives in the environment, never in argv or a shell word):

    check            reads REPO and PR from the environment and GH_TOKEN for ``gh``;
                     exit 0, or 1 when the stop level is red.
    parse-approve    reads COMMENT_BODY; prints the reason (exit 0) or the refusal
                     text for the PR (exit 3).
    authorize        --permission --actor --author --files; prints nothing (exit 0)
                     or the refusal text for the PR (exit 3).
    render-comment   --actor --files --head --source, reads REASON; prints the bot
                     comment body, marker first.

The module is stdlib only and carries no workflow expression.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, NamedTuple

# Shadow switch. False: the stop level prints a warning and the check passes.
# True: the stop level is red until an owner approves. Flipping it is the one-line
# change that ends the shadow period; the red path is tested with it both ways.
PR_SIZE_ENFORCED = False

# At this many changed files the check prints a hint. Never red.
FILES_HINT = 50
# At this many changed files the check is red (when enforced) until an owner approves.
FILES_STOP = 80
# After this many hours open the check prints an age notice. Never red.
HOURS_AGING = 168

# An approval recorded at N files covers up to N + N * GROWTH_PERCENT / 100 + GROWTH_FILES.
GROWTH_PERCENT = 25
GROWTH_FILES = 10

REASON_MIN = 12
REASON_MAX = 500

APPROVE_COMMAND = "/pr-size approve"
MARKER_NAME = "pr-size-override"
# The login that the workflow's GITHUB_TOKEN comments as; the only author a marker is read from.
BOT_LOGIN = "github-actions[bot]"
# Collaborator permissions that may approve (the bar ai-review-human-override.yml uses).
OWNER_PERMISSIONS = ("admin", "maintain", "write")
# The only permission that may approve a pull request it authored.
SELF_APPROVAL_PERMISSIONS = ("admin",)

GH_ATTEMPTS = 3
GH_TIMEOUT_SECONDS = 60

EXIT_OK = 0
EXIT_RED = 1
EXIT_REFUSED = 3

_APPROVE_RE = re.compile(r"^" + re.escape(APPROVE_COMMAND) + r":[ \t]*(.*)$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_MARKER_RE = re.compile(
    r"^<!-- " + re.escape(MARKER_NAME) + r" actor=(?P<actor>[A-Za-z0-9\[\]-]+)"
    r" files=(?P<files>\d+) head=(?P<head>[0-9a-f]{7,40})"
    r" source=(?P<source>\d+) at=(?P<at>\S+) -->$"
)
_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


class ReadError(Exception):
    """A GitHub API read that did not succeed after its retries."""


class Approval(NamedTuple):
    actor: str
    files: int
    url: str


class Verdict(NamedTuple):
    red: bool
    annotations: list[str]
    summary: list[str]


def parse_time(value: str) -> datetime:
    """An ISO-8601 timestamp as GitHub writes it (``Z`` suffix) -> aware datetime."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def hours_open(created_at: datetime, now: datetime) -> int:
    """Whole hours between creation and ``now``; never negative."""
    return max(0, int((now - created_at).total_seconds() // 3600))


def covered_up_to(recorded: int) -> int:
    """The largest file count an approval recorded at ``recorded`` files still covers."""
    return recorded + recorded * GROWTH_PERCENT // 100 + GROWTH_FILES


def parse_approve(body: str) -> tuple[str | None, str]:
    """Read ``/pr-size approve: <reason>`` from a comment's first line.

    Returns ``(reason, "")`` or ``(None, refusal text)``. Only the first line counts.
    """
    usage = f"Use `{APPROVE_COMMAND}: <reason, {REASON_MIN} to {REASON_MAX} characters>` as the first line."
    first = body.split("\n", 1)[0].rstrip("\r")
    match = _APPROVE_RE.match(first)
    if not match:
        return None, usage
    reason = _CONTROL_RE.sub(" ", match.group(1)).strip()
    if len(reason) < REASON_MIN:
        return (
            None,
            f"The reason is {len(reason)} characters; give {REASON_MIN} to {REASON_MAX}. {usage}",
        )
    if len(reason) > REASON_MAX:
        return None, f"The reason is {len(reason)} characters; keep it to {REASON_MAX} or fewer."
    return reason, ""


def authorize(permission: str, actor: str, author: str, files: int) -> tuple[bool, str]:
    """May ``actor`` (holding ``permission``) approve this pull request?

    A writer who is not the author may; the author may only if an admin; a
    pull request under the stop level needs no approval.
    """
    if permission not in OWNER_PERMISSIONS:
        return (
            False,
            "only a repository writer (write, maintain or admin) may approve a large pull request.",
        )
    if actor.lower() == author.lower() and permission not in SELF_APPROVAL_PERMISSIONS:
        return (
            False,
            "the author cannot approve their own pull request unless they are an admin. Ask another writer.",
        )
    if files < FILES_STOP:
        return (
            False,
            f"PR Size is not blocking at {files} files (the limit is {FILES_STOP}), so there is nothing to approve.",
        )
    return True, ""


def render_marker(actor: str, files: int, head: str, source: str, at: str) -> str:
    """The hidden first line of the bot comment this check later reads back."""
    if not _LOGIN_RE.match(actor):
        raise ValueError("actor is not a GitHub login")
    if not _SHA_RE.match(head):
        raise ValueError("head is not a commit sha")
    if not source.isdigit():
        raise ValueError("source is not a comment id")
    if not re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", at):
        raise ValueError("at is not a UTC timestamp")
    return f"<!-- {MARKER_NAME} actor={actor} files={files} head={head} source={source} at={at} -->"


def render_comment(actor: str, files: int, head: str, source: str, reason: str, at: str) -> str:
    marker = render_marker(actor, files, head, source, at)
    quoted = _CONTROL_RE.sub(" ", reason).strip()
    return (
        f"{marker}\n## PR size approved\n\n"
        f"@{actor} approved this pull request at {files} files (the limit is {FILES_STOP}).\n\n"
        f"> {quoted}\n\n"
        f"_The approval holds while the file count stays at or below {covered_up_to(files)}. "
        "A larger change needs a new decision._"
    )


def parse_marker(body: str) -> dict[str, str] | None:
    first = body.split("\n", 1)[0].rstrip("\r")
    match = _MARKER_RE.match(first)
    return match.groupdict() if match else None


def best_approval(comments: list[Any]) -> Approval | None:
    """The approval covering the most files, from markers written by the bot only."""
    best: Approval | None = None
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        user = comment.get("user")
        if not isinstance(user, dict) or user.get("login") != BOT_LOGIN:
            continue
        body = comment.get("body")
        marker = parse_marker(body) if isinstance(body, str) else None
        if marker is None:
            continue
        files = int(marker["files"])
        if best is None or files > best.files:
            best = Approval(marker["actor"], files, str(comment.get("html_url") or ""))
    return best


def evaluate(files: int, hours: int, approval: Approval | None, enforced: bool) -> Verdict:
    """Fold the numbers and the approval into annotations, a step summary and a verdict."""
    notes: list[str] = []
    red = False
    approved = approval is not None and files <= covered_up_to(approval.files)
    if files >= FILES_STOP:
        if approved and approval is not None:
            where = f" ({approval.url})" if approval.url else ""
            notes.append(
                f"::warning::PR Size: {files} files is over the limit of {FILES_STOP}; "
                f"approved by @{approval.actor} at {approval.files} files{where}."
            )
        else:
            if approval is not None:
                why = (
                    f"The approval by @{approval.actor} was recorded at {approval.files} files and "
                    f"covers up to {covered_up_to(approval.files)}; this pull request has {files}. "
                )
            else:
                why = ""
            ask = (
                f"An owner approves with `{APPROVE_COMMAND}: <reason, {REASON_MIN} to {REASON_MAX} "
                "characters>`. Or split it along a seam, or finish it today."
            )
            if enforced:
                red = True
                notes.append(
                    f"::error::PR Size: {files} files is over the limit of {FILES_STOP}. {why}{ask}"
                )
            else:
                notes.append(
                    f"::warning::PR Size (shadow, not enforced): {files} files is over the limit of "
                    f"{FILES_STOP}. This would be red. {why}{ask}"
                )
    elif files >= FILES_HINT:
        notes.append(
            f"::notice::PR Size: {files} files (hint at {FILES_HINT}, limit {FILES_STOP}). "
            "Consider splitting along a seam or landing it today."
        )
    if hours >= HOURS_AGING:
        notes.append(
            f"::notice::PR Size: open {hours} h ({HOURS_AGING // 24}+ days): merge, split or close. "
            "An owner decides; waiting for a review is not counted against the author, and age is never red."
        )
    if not notes:
        notes.append(
            f"PR Size: {files} files, open {hours} h; under the hint at {FILES_HINT} files."
        )
    if red:
        state = "red: no valid owner approval"
    elif files >= FILES_STOP and approved:
        state = "approved by an owner"
    elif files >= FILES_STOP:
        state = "over the limit, shadow: not red yet"
    else:
        state = "ok"
    summary = [
        "### PR Size",
        "",
        "| metric | value | threshold |",
        "|---|---|---|",
        f"| changed files | {files} | hint {FILES_HINT}, limit {FILES_STOP} |",
        f"| hours open | {hours} | notice at {HOURS_AGING} |",
        f"| enforced | {'yes' if enforced else 'no (shadow)'} | |",
        "",
        f"Result: {state}.",
    ]
    return Verdict(red, notes, summary)


def _run_gh(args: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


Runner = Callable[[list[str]], "tuple[int, str, str]"]


def api_read(
    path: str,
    *,
    paginate: bool = False,
    runner: Runner = _run_gh,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """One ``gh api`` read with a bounded retry; raises ReadError when it never succeeds."""
    args = ["api", path] + (["--paginate"] if paginate else [])
    last = ""
    for attempt in range(1, GH_ATTEMPTS + 1):
        code, out, err = runner(args)
        if code == 0:
            return out
        last = err.strip().replace("\n", " ")[:200]
        if attempt < GH_ATTEMPTS:
            sleep(attempt)
    raise ReadError(f"reading {path} failed after {GH_ATTEMPTS} attempts: {last}")


def decode_stream(text: str) -> list[Any]:
    """Concatenated JSON arrays (what ``gh api --paginate`` prints) -> one flat list."""
    decoder = json.JSONDecoder()
    items: list[Any] = []
    pos = 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        value, pos = decoder.raw_decode(text, pos)
        if isinstance(value, list):
            items.extend(value)
        else:
            items.append(value)
    return items


def run_check(
    repo: str,
    pr: str,
    *,
    now: datetime,
    enforced: bool,
    runner: Runner = _run_gh,
    sleep: Callable[[float], None] = time.sleep,
    summary_path: str | None = None,
) -> int:
    """Read the pull request, judge it, print annotations. Returns the exit code."""
    if not _REPO_RE.match(repo) or not pr.isdigit():
        print(
            f"::error::PR Size: REPO and PR must be owner/name and a number, got {repo!r} and {pr!r}."
        )
        return EXIT_RED if enforced else EXIT_OK
    try:
        pull = json.loads(api_read(f"repos/{repo}/pulls/{pr}", runner=runner, sleep=sleep))
        files = int(pull["changed_files"])
        created = parse_time(str(pull["created_at"]))
        approval = None
        if files >= FILES_STOP:
            comments = decode_stream(
                api_read(
                    f"repos/{repo}/issues/{pr}/comments?per_page=100",
                    paginate=True,
                    runner=runner,
                    sleep=sleep,
                )
            )
            approval = best_approval(comments)
    except (ReadError, ValueError, KeyError, TypeError) as exc:
        # A read that failed is not a size that passed. Shadow: say so and pass.
        # Enforced: fail closed, naming the read, so a re-run is the remedy.
        level = "error" if enforced else "warning"
        print(f"::{level}::PR Size could not measure this pull request ({exc}). Re-run this job.")
        return EXIT_RED if enforced else EXIT_OK
    verdict = evaluate(files, hours_open(created, now), approval, enforced)
    for line in verdict.annotations:
        print(line)
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(verdict.summary) + "\n")
    return EXIT_RED if verdict.red else EXIT_OK


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pr_size.py")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("parse-approve")
    auth = sub.add_parser("authorize")
    auth.add_argument("--permission", required=True)
    auth.add_argument("--actor", required=True)
    auth.add_argument("--author", required=True)
    auth.add_argument("--files", type=int, required=True)
    render = sub.add_parser("render-comment")
    render.add_argument("--actor", required=True)
    render.add_argument("--files", type=int, required=True)
    render.add_argument("--head", required=True)
    render.add_argument("--source", required=True)
    args = parser.parse_args(argv[1:])

    if args.command == "check":
        return run_check(
            os.environ.get("REPO", ""),
            os.environ.get("PR", ""),
            now=_utc_now(),
            enforced=PR_SIZE_ENFORCED,
            summary_path=os.environ.get("GITHUB_STEP_SUMMARY") or None,
        )
    if args.command == "parse-approve":
        reason, refusal = parse_approve(os.environ.get("COMMENT_BODY", ""))
        print(reason if reason is not None else refusal)
        return EXIT_OK if reason is not None else EXIT_REFUSED
    if args.command == "authorize":
        ok, refusal = authorize(args.permission, args.actor, args.author, args.files)
        if not ok:
            print(refusal)
        return EXIT_OK if ok else EXIT_REFUSED
    try:
        print(
            render_comment(
                args.actor,
                args.files,
                args.head,
                args.source,
                os.environ.get("REASON", ""),
                _stamp(_utc_now()),
            )
        )
    except ValueError as exc:
        print(f"pr_size: {exc}", file=sys.stderr)
        return 2
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
