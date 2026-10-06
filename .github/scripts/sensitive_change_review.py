#!/usr/bin/env python3
"""Sensitive Change Review gate.

A PR that touches a sensitive path (the sandbox, the command floor, secret
scrubbing, redaction) needs a human approval ON THE CURRENT HEAD whose review
body carries a filled-in reasoning template. A bare "Approve" click does not
count. The gate checks format only: it cannot judge whether the reasoning is
right, it only makes sure the reviewer wrote it down.

A PR from a fork that touches a sensitive path is refused outright: a
maintainer takes it over on a branch of this repository, runs the real sandbox,
and drives it green there.

On a same-repository PR the gate also keeps one sticky PR comment up to date,
with the sensitive files and the template to paste into the approval. A fork
PR's token cannot write, so a fork gets the job summary only.

Deterministic on purpose: no model, no sandbox run. It reads the PR, its file
list and its reviews through `gh api` and answers one question.

Usage (CI): REPO=owner/name PR=123 python3 sensitive_change_review.py
Exit codes: 0 pass / not applicable, 1 requirement not met, 2 API/read error.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from typing import Any, Callable

# Paths whose change can break every command, leak a secret, or lock a user
# out. `*` in fnmatch also matches `/`, so a directory glob covers subdirs.
# The gate's own files are listed so weakening the gate needs the same review.
SENSITIVE_GLOBS: tuple[str, ...] = (
    "src/kiro_crew/sandbox*.py",
    "src/kiro_crew/security/*",
    "src/kiro_crew/secrets/*",
    "src/kiro_crew/log_redaction.py",
    "src/kiro_crew/snapshot_redact.py",
    "src/kiro_crew/apps/scrub_sdk.py",
    "src/kiro_crew/hooks.py",
    "src/kiro_crew/hook_runtime/*",
    "src/kiro_crew/deny_guidance.py",
    "src/kiro_crew/platform/security_authority.py",
    "src/kiro_crew/agent_sdk/tool_gate.py",
    "src/kiro_crew/acp_tool_gate.py",
    "src/kiro_crew/builtin_skills/security-conductor/rules-of-engagement.json",
    "src/kiro_crew/builtin_skills/security-conductor/golden-paths.json",
    "src/kiro_crew/security_posture.py",
    "src/kiro_crew/computer_use/gate.py",
    "src/kiro_crew/dashboard/handlers/*redaction*.py",
    ".github/workflows/sensitive-change-review.yml",
    ".github/scripts/sensitive_change_review.py",
)

HEADING = "## Sensitive change review"

# Every field the reviewer must fill. The parent "No regression:" line only
# groups the three sub-items below it, so it carries no value of its own.
REQUIRED_FIELDS: tuple[str, ...] = (
    "What rule changed",
    "Before -> after",
    "Worst case if wrong",
    "Evidence checked",
    "Tools still work",
    "Backward compatible",
    "Existing tests",
    "How to undo",
)

# A value that says nothing. "N/A" alone is empty; "N/A because ..." is not.
_PLACEHOLDERS = {"", "n/a", "na", "none", "-", "--", "tbd", "todo", ".", "ok", "yes", "no", "lgtm"}
MIN_WORDS = 3

# Only people with write access can approve. `author_association` is not
# enough: a read-only collaborator or org member also reads as COLLABORATOR /
# MEMBER, so the repository's own permission answer is asked instead.
# (`maintain` reports as `write` in this field.)
WRITE_PERMISSIONS = {"admin", "write"}

# GitHub's PR files endpoint stops at this many rows without an error.
FILES_API_CAP = 3000

# Marks the one sticky comment this gate owns. Only a comment by COMMENT_AUTHOR
# carrying it is edited, so a user who pastes the marker cannot hijack it.
COMMENT_MARKER = "<!-- sensitive-change-review -->"
COMMENT_AUTHOR = "github-actions[bot]"

_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
# Markdown emphasis a reviewer may wrap a label or value in (`**Label:**`).
_EMPHASIS = "*_"
_HEADING_RE = re.compile(r"^\s*#{1,6}\s")


class Verdict:
    """The gate's answer: pass/fail, the sensitive files, and why."""

    def __init__(
        self,
        ok: bool,
        sensitive: list[str] | None = None,
        messages: list[str] | None = None,
        fork: bool = False,
    ):
        self.ok = ok
        self.sensitive = sensitive or []
        self.messages = messages or []
        self.fork = fork


def is_fork(pr: dict) -> bool:
    """True unless the PR's head and base are the same repository.

    A head with no repository (a deleted fork) counts as a fork: fail closed.
    """
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
    return not head_repo or head_repo != base_repo


def sensitive_files(files: list[str]) -> list[str]:
    """The changed files that match a sensitive glob, in input order."""
    return [f for f in files if any(fnmatch.fnmatchcase(f, g) for g in SENSITIVE_GLOBS)]


def _field_value(lines: list[str], label: str) -> str | None:
    """Text after `label ...:` on its bullet, plus non-bullet continuation lines.

    Returns None when no bullet starts with the label.
    """
    want = label.lower()
    for i, line in enumerate(lines):
        m = _BULLET_RE.match(line)
        if not m:
            continue
        # `- **What rule changed:** ...` is the same field as the plain form.
        text = m.group(1).strip().lstrip(_EMPHASIS).strip()
        if not text.lower().startswith(want):
            continue
        colon = text.find(":", len(label))
        if colon < 0:
            return ""
        parts = [text[colon + 1 :].strip().lstrip(_EMPHASIS).strip()]
        for nxt in lines[i + 1 :]:
            if not nxt.strip() or _BULLET_RE.match(nxt) or _HEADING_RE.match(nxt):
                break
            parts.append(nxt.strip())
        return " ".join(p for p in parts if p).strip()
    return None


def _is_empty(value: str) -> bool:
    stripped = value.strip().strip("`*_").strip()
    if stripped.lower() in _PLACEHOLDERS:
        return True
    return len(stripped.split()) < MIN_WORDS


def template_problems(body: str) -> list[str]:
    """Why a review body does not satisfy the template ([] when it does)."""
    if HEADING.lower() not in (body or "").lower():
        return [f"missing the `{HEADING}` heading"]
    section = body[body.lower().index(HEADING.lower()) + len(HEADING) :]
    lines = section.splitlines()
    problems: list[str] = []
    for label in REQUIRED_FIELDS:
        value = _field_value(lines, label)
        if value is None:
            problems.append(f"missing field `{label}`")
        elif _is_empty(value):
            problems.append(
                f"field `{label}` is empty or too short (need {MIN_WORDS}+ words; "
                "`N/A` needs a reason)"
            )
    return problems


def _standing_approvals(reviews: list[dict]) -> dict[str, list[dict]]:
    """Each reviewer's approvals that still stand, oldest first.

    A CHANGES_REQUESTED or DISMISSED review withdraws every earlier approval by
    that reviewer. A later approval does NOT withdraw an earlier one: a complete
    template approval followed by a second bare "Approve" on the same head
    still counts. A plain COMMENTED review changes nothing.
    """
    standing: dict[str, list[dict]] = {}
    for review in reviews:
        state = review.get("state")
        login = (review.get("user") or {}).get("login") or ""
        if not login:
            continue
        if state in {"CHANGES_REQUESTED", "DISMISSED"}:
            standing.pop(login, None)
        elif state == "APPROVED":
            standing.setdefault(login, []).append(review)
    return standing


def changed_paths(rows: list[dict]) -> list[str]:
    """Every path a PR touches, including the OLD name of a renamed file.

    Without the old name, moving a sensitive file outside its glob in the same
    PR that edits it would hide the edit.
    """
    paths: list[str] = []
    for row in rows:
        for key in ("filename", "previous_filename"):
            if row.get(key):
                paths.append(row[key])
    return paths


def file_list_problem(pr: dict, rows: list[dict]) -> str | None:
    """Why the file list cannot be trusted as complete (None when it can)."""
    expected = pr.get("changed_files")
    if len(rows) >= FILES_API_CAP:
        return f"the PR changes {FILES_API_CAP}+ files, past what the API lists"
    if not isinstance(expected, int) or len(rows) != expected:
        return f"the API listed {len(rows)} files but the PR reports {expected}"
    return None


def head_problem(event_head: str, api_head: str) -> str | None:
    """Why the run cannot judge a head (None when event and API agree).

    The check run lands on the commit that triggered the event. Judging the
    API's head instead would let an approval of one commit turn a different
    commit green whenever the API lags or leads a push, so the two must match.
    """
    if not event_head:
        return "the event carried no head SHA"
    if event_head != api_head:
        return (
            f"the event head {event_head[:12]} differs from the API head "
            f"{(api_head or '?')[:12]}; the run for the newer push decides"
        )
    return None


def _approval_problems(
    review: dict, login: str, author: str, head: str, permission_of: Callable[[str], str]
) -> list[str]:
    """Why one approval does not count ([] when it does)."""
    user = review.get("user") or {}
    why: list[str] = []
    if user.get("type") == "Bot" or login.endswith("[bot]"):
        why.append("bots do not count")
    if login == author:
        why.append("the PR author cannot approve their own change")
    if review.get("commit_id") != head:
        why.append(
            f"approved an older commit ({(review.get('commit_id') or '?')[:12]}); re-approve on {head[:12]}"
        )
    why.extend(template_problems(review.get("body") or ""))
    # Ask for the permission last, and only for an otherwise-good approval, so
    # a bare click never costs an API read.
    if not why and permission_of(login) not in WRITE_PERMISSIONS:
        why.append("reviewer has no write access")
    return why


def evaluate(
    pr: dict,
    files: list[str],
    reviews: list[dict],
    permission_of: Callable[[str], str],
    head: str | None = None,
) -> Verdict:
    """Decide the gate for `head` (defaults to the PR's head).

    `permission_of(login)` returns the repo permission. Any standing approval
    on `head` with a complete template passes, not just the reviewer's latest.
    """
    hits = sensitive_files(files)
    if not hits:
        return Verdict(ok=True, messages=["No sensitive path changed; nothing to check."])

    if is_fork(pr):
        # No approval can clear this: the fork's code must come in through a
        # maintainer, who becomes accountable for the sandbox evidence.
        return Verdict(
            ok=False,
            sensitive=hits,
            fork=True,
            messages=[
                "This PR comes from a fork and changes a sensitive path. A fork PR "
                "cannot pass this gate. A maintainer takes it over on a branch of "
                "this repository (keep the author with `Co-authored-by:` and write "
                "`Supersedes #<n>`), runs the real sandbox, and drives that PR green."
            ],
        )

    head = head or (pr.get("head") or {}).get("sha") or ""
    author = (pr.get("user") or {}).get("login") or ""
    messages: list[str] = []
    for login, approvals in sorted(_standing_approvals(reviews).items()):
        latest_why: list[str] = []
        for review in reversed(approvals):
            why = _approval_problems(review, login, author, head, permission_of)
            if not why:
                return Verdict(
                    ok=True,
                    sensitive=hits,
                    messages=[f"@{login} approved {head[:12]} with a complete reasoning template."],
                )
            latest_why = latest_why or why
        note = f" ({len(approvals)} approvals checked, none complete)" if len(approvals) > 1 else ""
        messages.append(f"@{login}{note}: " + "; ".join(latest_why))

    if not messages:
        messages.append(f"No approval on {head[:12]} yet.")
    return Verdict(ok=False, sensitive=hits, messages=messages)


class PermissionReadError(RuntimeError):
    """The permission API did not answer; distinct from answering "no"."""


def _gh_json(path: str, paginate: bool = False) -> Any:
    cmd = ["gh", "api", path]
    if paginate:
        # --slurp wraps each page's array into one outer array.
        cmd += ["--paginate", "--slurp"]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8").stdout
    data = json.loads(out)
    if paginate:
        return [item for page in data for item in page]
    return data


def _summary(verdict: Verdict) -> str:
    lines = ["### Sensitive Change Review", ""]
    if verdict.sensitive:
        lines.append("Sensitive files changed:")
        lines += [f"- `{f}`" for f in verdict.sensitive]
        lines.append("")
    lines += [f"- {m}" for m in verdict.messages]
    if not verdict.ok and not verdict.fork:
        lines += [
            "",
            "A human reviewer with write access must approve the CURRENT head and",
            "paste this template into the approval, every field filled",
            "(`N/A` needs a reason):",
            "",
            "```",
            HEADING,
            "- What rule changed:",
            "- Before -> after (what was allowed/blocked, now):",
            "- Worst case if wrong (breaks commands / leaks secret / locks user out):",
            "- Evidence checked (which sandbox result proves it):",
            "- No regression:",
            "  - Tools still work (which tools/commands were run, result):",
            "  - Backward compatible (old config / old data / old callers still work, how checked):",
            "  - Existing tests (which suites passed on this head):",
            "- How to undo:",
            "```",
        ]
    return "\n".join(lines) + "\n"


def _gh_write(method: str, path: str, body: str) -> None:
    payload = json.dumps({"body": body})
    subprocess.run(
        ["gh", "api", "-X", method, path, "--input", "-"],
        input=payload,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def comment_body(verdict: Verdict) -> str:
    """The sticky comment: the marker, then the same text as the job summary."""
    return f"{COMMENT_MARKER}\n{_summary(verdict)}"


def find_sticky(comments: list[dict]) -> dict | None:
    """The gate's own sticky comment, or None."""
    for comment in comments:
        login = (comment.get("user") or {}).get("login")
        if login == COMMENT_AUTHOR and COMMENT_MARKER in (comment.get("body") or ""):
            return comment
    return None


def upsert_sticky(repo: str, pr_number: str, body: str, create: bool = True) -> None:
    """Create (when `create`) or update the sticky comment. Never changes the verdict."""
    try:
        comments = _gh_json(f"repos/{repo}/issues/{pr_number}/comments?per_page=100", paginate=True)
        existing = find_sticky(comments)
        if existing is None:
            if not create:
                return
            _gh_write("POST", f"repos/{repo}/issues/{pr_number}/comments", body)
        elif (existing.get("body") or "") != body:
            _gh_write("PATCH", f"repos/{repo}/issues/comments/{existing['id']}", body)
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError) as exc:
        detail = getattr(exc, "stderr", "") or type(exc).__name__
        print(f"::warning::Could not update the sticky comment ({str(detail).strip()}).")


def main() -> int:
    repo = os.environ.get("REPO", "")
    pr_number = os.environ.get("PR", "")
    if not repo or not pr_number.isdigit():
        print("::error::REPO and a numeric PR must be set.")
        return 2
    base = f"repos/{repo}/pulls/{pr_number}"
    event_head = os.environ.get("EVENT_HEAD_SHA", "")
    try:
        pr = _gh_json(base)
        rows = _gh_json(f"{base}/files?per_page=100", paginate=True)
        reviews = _gh_json(f"{base}/reviews?per_page=100", paginate=True)
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError) as exc:
        # A read failure is not a passing PR: fail closed and say why.
        print(
            f"::error::Could not read the PR from the API ({type(exc).__name__}); re-run this job."
        )
        return 2

    # An incomplete file list is not a list with no sensitive file in it.
    problem = file_list_problem(pr, rows)
    if problem:
        print(f"::error::Cannot see every changed file ({problem}); failing closed.")
        return 1

    # Judge the commit the check run lands on, never a head the API picked.
    problem = head_problem(event_head, (pr.get("head") or {}).get("sha") or "")
    if problem:
        print(f"::error::Cannot judge this head ({problem}); failing closed.")
        return 1

    def permission_of(login: str) -> str:
        # A 404 is the API answering "not a collaborator". Any other failure
        # is unknown: raise so the job fails as a read error and logs why,
        # instead of silently reading as "no write access" forever.
        path = f"repos/{repo}/collaborators/{login}/permission"
        try:
            data = _gh_json(path)
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or "").strip()
            if "HTTP 404" in err or "Not Found" in err:
                print(f"Permission for @{login}: not a collaborator (HTTP 404).")
                return "none"
            raise PermissionReadError(f"GET {path} failed: {err or exc}") from exc
        except json.JSONDecodeError as exc:
            raise PermissionReadError(f"GET {path} returned non-JSON") from exc
        permission = str((data or {}).get("permission") or "none")
        print(f"Permission for @{login}: {permission}.")
        return permission

    try:
        verdict = evaluate(pr, changed_paths(rows), reviews, permission_of, head=event_head)
    except PermissionReadError as exc:
        print(f"::error::Could not read a reviewer's permission ({exc}); re-run this job.")
        return 2
    text = _summary(verdict)
    print(text)
    # A fork's token is read-only, so only a same-repository PR gets the comment.
    # With no sensitive file left, an existing comment is refreshed (so it stops
    # asking for an approval) but no new one is created.
    if not is_fork(pr):
        upsert_sticky(repo, pr_number, comment_body(verdict), create=bool(verdict.sensitive))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(text)
    if not verdict.ok:
        print("::error::Sensitive path changed without a reasoned human approval on this head.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
