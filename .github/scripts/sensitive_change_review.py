#!/usr/bin/env python3
"""Sensitive Change Review gate.

A PR that touches a sensitive path (the sandbox, the command floor, secret
scrubbing, redaction) needs a filled-in reasoning template for THE CURRENT
HEAD. Either of two records counts:

- the PR author's own PR comment that carries the template and names the
  current head SHA (the author did the checks, so the author writes them down);
- a human approval on the current head whose review body carries the template.

A record made for an earlier head of the PR still counts when the sensitive
files' diff is byte-identical on both heads, each measured against its own
merge base, so a rebase that changes nothing sensitive keeps the record. Any
doubt (another set of sensitive files, a changed byte, a compare the API
cannot answer in full) leaves the check red.

A bare "Approve" click does not count. The gate checks format only: it cannot
judge whether the reasoning is right, it only makes sure someone wrote it down.
Merging still needs a second person's approval; GitHub's branch rules enforce
that, not this gate.

A PR from a fork that touches a sensitive path is refused outright: a
maintainer takes it over on a branch of this repository, runs the real sandbox,
and drives it green there.

On a same-repository PR the gate also keeps one sticky PR comment up to date,
with the sensitive files and the template to paste into the approval. A fork
PR gets the job summary only: its sensitive change is refused whatever is pasted.

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
import tempfile
import time
from typing import Any, Callable, Iterator
from urllib.parse import quote

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
    "src/kiro_crew/security_posture_*.py",
    "src/kiro_crew/computer_use/gate.py",
    "src/kiro_crew/dashboard/handlers/*redaction*.py",
    ".github/workflows/sensitive-change-review*.yml",
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

# A compare lists at most this many files, all on its first page.
COMPARE_FILES_CAP = 300

# The most earlier heads one run compares against the current head, newest
# record first. Each costs one compare read.
CARRY_MAX_HEADS = 5

# Marks the one sticky comment this gate owns. Only a comment by COMMENT_AUTHOR
# carrying it is edited, so a user who pastes the marker cannot hijack it.
COMMENT_MARKER = "<!-- sensitive-change-review -->"
COMMENT_AUTHOR = "github-actions[bot]"

_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
# Markdown emphasis a reviewer may wrap a label or value in (`**Label:**`).
_EMPHASIS = "*_"
_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
# A commit SHA as a whole word: git's 7-char short form up to the full 40.
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


class Verdict:
    """The gate's answer: pass/fail, the sensitive files, and why."""

    def __init__(
        self,
        ok: bool,
        sensitive: list[str] | None = None,
        messages: list[str] | None = None,
        fork: bool = False,
        head: str = "",
    ):
        self.ok = ok
        self.sensitive = sensitive or []
        self.messages = messages or []
        self.fork = fork
        self.head = head


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


def names_head(body: str, head: str) -> bool:
    """True when the body names `head` by a 7+ character SHA prefix."""
    return bool(head) and any(head.startswith(t) for t in _SHA_RE.findall((body or "").lower()))


def _author_comment_problems(
    comment: dict, author: str, head: str, permission_of: Callable[[str], str]
) -> list[str]:
    """Why one PR-author comment does not count ([] when it does).

    The caller passes only the author's comments that carry the heading.
    """
    why: list[str] = []
    body = comment.get("body") or ""
    if not names_head(body, head):
        why.append(f"does not name the current head {head[:12]}; post it again for this head")
    why.extend(template_problems(body))
    if not why and permission_of(author) not in WRITE_PERMISSIONS:
        why.append("the author has no write access")
    return why


def _author_comments(comments: list[dict], author: str) -> list[dict]:
    """The PR author's own human comments that carry the heading, newest first."""
    picked = []
    for comment in comments:
        user = comment.get("user") or {}
        login = user.get("login") or ""
        if not author or login != author:
            continue
        if user.get("type") == "Bot" or login.endswith("[bot]"):
            continue
        if HEADING.lower() in (comment.get("body") or "").lower():
            picked.append(comment)
    return list(reversed(picked))


class CarryUnavailable(Exception):
    """A head's sensitive diff could not be read in full."""


# One sensitive file's diff on one head: (old name, status, mode on the head,
# patch text). A removed file has no mode on the head, so its mode is "".
SensitivePatches = dict[str, tuple[str, str, str, str]]

# The statuses whose whole change the patch text shows. `changed` (a mode-only
# change), `copied` and anything new are not compared, so they never carry.
CARRY_STATUSES = frozenset({"added", "modified", "removed", "renamed"})

# A regular file or an executable one. A compare row never says which, so the
# mode is read from the head's tree. A symlink (120000) or a submodule
# (160000) is refused: its patch may not show what changed.
REGULAR_MODES = frozenset({"100644", "100755"})


def _patch_line_counts(patch: str) -> tuple[int, int]:
    lines = patch.split("\n")
    return sum(ln.startswith("+") for ln in lines), sum(ln.startswith("-") for ln in lines)


def sensitive_patches(
    rows: list[dict], modes_of: Callable[[list[str]], dict[str, str]]
) -> SensitivePatches:
    """Each sensitive file's diff from one compare's file rows.

    `modes_of(paths)` answers each path's git mode on the compared head.
    Raises CarryUnavailable when the list may be cut short, or when a sensitive
    file has no patch text (binary, too large to show, or a pure rename), a
    patch whose +/- lines disagree with the row's counts (cut short), a status
    the patch cannot show in full, or a mode that cannot be read or is not a
    regular file: a diff that cannot be read cannot be shown to be unchanged.
    """
    if len(rows) >= COMPARE_FILES_CAP:
        raise CarryUnavailable(f"the compare lists {COMPARE_FILES_CAP}+ files, past what it shows")
    picked: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            raise CarryUnavailable("the compare lists a file row that is not an object")
        if not sensitive_files(changed_paths([row])):
            continue
        name = row.get("filename") or ""
        patch = row.get("patch")
        status = row.get("status") or ""
        if not name or not isinstance(patch, str) or not patch:
            raise CarryUnavailable(f"`{name or '?'}` has no patch text to compare")
        if status not in CARRY_STATUSES:
            raise CarryUnavailable(f"`{name}` has status `{status or '?'}`, which is not compared")
        if _patch_line_counts(patch) != (row.get("additions"), row.get("deletions")):
            raise CarryUnavailable(f"the patch of `{name}` disagrees with its line counts")
        picked.append(row)
    wanted = [row["filename"] for row in picked if row["status"] != "removed"]
    modes = modes_of(wanted) if wanted else {}
    patches: SensitivePatches = {}
    for row in picked:
        name = row["filename"]
        mode = ""
        if row["status"] != "removed":
            mode = modes.get(name, "")
            if mode not in REGULAR_MODES:
                raise CarryUnavailable(f"`{name}` is not a regular file (mode {mode or '?'})")
        patches[name] = (row.get("previous_filename") or "", row["status"], mode, row["patch"])
    return patches


def carry_problem(
    earlier: str, head: str, hits: list[str], patches_of: Callable[[str], SensitivePatches]
) -> str | None:
    """Why a record for `earlier` does not count for `head` (None when it does).

    The exact patch text is compared, hunk headers and context lines included,
    rather than a patch-id: a patch-id ignores line numbers and whitespace, so
    it would also match a change that moved inside a file `main` edited in
    between. Each head's diff is taken against its own merge base, so `main`
    moving under a rebase adds nothing to either side. The file's mode on the
    head is compared too, because a compare row does not show it.
    """
    try:
        before = patches_of(earlier)
        after = patches_of(head)
    except CarryUnavailable as exc:
        return f"could not compare the two heads ({exc})"
    covered = sensitive_files(list(after) + [diff[0] for diff in after.values() if diff[0]])
    if set(covered) != set(hits):
        return "the compare of the current head disagrees with the PR's file list"
    if set(before) != set(after):
        changed = sorted(set(before) ^ set(after))
        return "the set of sensitive files changed (" + ", ".join(f"`{p}`" for p in changed) + ")"
    for path in sorted(after):
        if before[path][2] != after[path][2]:
            return f"the mode of `{path}` changed ({before[path][2]} -> {after[path][2]})"
        if before[path] != after[path]:
            return f"the diff of `{path}` changed"
    return None


def named_head(body: str) -> str:
    """The one full SHA on the template's `Head commit:` line, or ""."""
    lower = (body or "").lower()
    if HEADING.lower() not in lower:
        return ""
    lines = body[lower.index(HEADING.lower()) + len(HEADING) :].splitlines()
    shas = set(_SHA_RE.findall((_field_value(lines, "Head commit") or "").lower()))
    if len(shas) != 1:
        return ""
    (sha,) = shas
    return sha if len(sha) == 40 else ""


def _carry_candidates(
    owned: list[dict],
    standing: dict[str, list[dict]],
    author: str,
    head: str,
    permission_of: Callable[[str], str],
) -> Iterator[tuple[str, str]]:
    """(earlier head, who and what) for each record complete for another head.

    Newest author comment first, then each reviewer's newest approval first;
    one entry per earlier head, at most CARRY_MAX_HEADS of them. Lazy, so a
    record that carries spends no reads on the ones after it.
    """
    records: list[tuple[str, Callable[[str], list[str]], str]] = [
        (
            named_head(comment.get("body") or ""),
            lambda earlier, c=comment: _author_comment_problems(c, author, earlier, permission_of),
            f"@{author}'s template comment",
        )
        for comment in owned
    ]
    for login, approvals in sorted(standing.items()):
        records += [
            (
                review.get("commit_id") or "",
                lambda earlier, r=review, who=login: _approval_problems(
                    r, who, author, earlier, permission_of
                ),
                f"@{login}'s template approval",
            )
            for review in reversed(approvals)
        ]
    picked: set[str] = set()
    for earlier, problems_for, who in records:
        if len(picked) >= CARRY_MAX_HEADS:
            return
        if not earlier or earlier == head or earlier in picked:
            continue
        # Judged as if `earlier` were the head, so only a record that was
        # complete for its own head can carry.
        if not problems_for(earlier):
            picked.add(earlier)
            yield earlier, who


def evaluate(
    pr: dict,
    files: list[str],
    reviews: list[dict],
    permission_of: Callable[[str], str],
    head: str | None = None,
    comments: list[dict] | None = None,
    patches_of: Callable[[str], SensitivePatches] | None = None,
) -> Verdict:
    """Decide the gate for `head` (defaults to the PR's head).

    `permission_of(login)` returns the repo permission. Any standing approval
    on `head` with a complete template passes, not just the reviewer's latest.
    So does any PR-author comment with a complete template that names `head`.
    With `patches_of(sha)` (a head's sensitive diff), a record complete for an
    earlier head also passes when that head's sensitive diff equals `head`'s.
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

    owned = _author_comments(comments or [], author)
    for comment in owned:
        if not _author_comment_problems(comment, author, head, permission_of):
            return Verdict(
                ok=True,
                sensitive=hits,
                head=head,
                messages=[f"@{author} posted a complete reasoning template for {head[:12]}."],
            )
    if owned:
        latest = _author_comment_problems(owned[0], author, head, permission_of)
        note = f" ({len(owned)} comments checked, none complete)" if len(owned) > 1 else ""
        messages.append(f"@{author} comment{note}: " + "; ".join(latest))

    standing = _standing_approvals(reviews)
    for login, approvals in sorted(standing.items()):
        latest_why: list[str] = []
        for review in reversed(approvals):
            why = _approval_problems(review, login, author, head, permission_of)
            if not why:
                return Verdict(
                    ok=True,
                    sensitive=hits,
                    head=head,
                    messages=[f"@{login} approved {head[:12]} with a complete reasoning template."],
                )
            latest_why = latest_why or why
        note = f" ({len(approvals)} approvals checked, none complete)" if len(approvals) > 1 else ""
        messages.append(f"@{login}{note}: " + "; ".join(latest_why))

    if patches_of is not None:
        for earlier, who in _carry_candidates(owned, standing, author, head, permission_of):
            problem = carry_problem(earlier, head, hits, patches_of)
            if problem is None:
                return Verdict(
                    ok=True,
                    sensitive=hits,
                    head=head,
                    messages=[
                        f"{who} for {earlier[:12]} carries to {head[:12]}: the diff of every "
                        "sensitive file is byte-identical on both heads, each against its own "
                        "merge base."
                    ],
                )
            messages.append(f"{who} for {earlier[:12]} does not carry to {head[:12]}: {problem}.")

    if not messages:
        messages.append(f"No reasoning template for {head[:12]} yet.")
    return Verdict(ok=False, sensitive=hits, messages=messages, head=head)


class PermissionReadError(RuntimeError):
    """The permission API did not answer; distinct from answering "no"."""


# Waits between attempts of one API read. A read that still fails after the
# last one raises, so the caller fails closed exactly as it would have.
READ_RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0)
_sleep: Callable[[float], None] = time.sleep


def _is_transient(exc: subprocess.CalledProcessError) -> bool:
    """A server error, a rate limit or no HTTP status at all (a dropped
    connection) may pass on a second try. Any other 4xx is the API's answer,
    and a 404 is one that permission_of reads as "not a collaborator"."""
    err = str(exc.stderr or "")
    match = re.search(r"\bHTTP (\d{3})\b", err)
    if match is None:
        return True
    status = int(match.group(1))
    return status >= 500 or status == 429 or "rate limit" in err.lower()


def _gh_run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8").stdout


def _gh_read(cmd: list[str]) -> str:
    for delay in READ_RETRY_DELAYS:
        try:
            return _gh_run(cmd)
        except subprocess.CalledProcessError as exc:
            if not _is_transient(exc):
                raise
            print(f"::warning::{' '.join(cmd[:3])} failed ({_first_line(exc)}); retrying.")
            _sleep(delay)
    return _gh_run(cmd)


def _first_line(exc: BaseException) -> str:
    """The first line of a failed command's stderr, capped, or the error type."""
    err = str(getattr(exc, "stderr", "") or "").strip()
    return err.splitlines()[0][:200] if err else type(exc).__name__


def _gh_json(path: str, paginate: bool = False) -> Any:
    cmd = ["gh", "api", path]
    if paginate:
        # --slurp wraps each page's array into one outer array.
        cmd += ["--paginate", "--slurp"]
    out = _gh_read(cmd)
    data = json.loads(out)
    if paginate:
        return [item for page in data for item in page]
    return data


_TREE_MODES_QUERY = """
query($owner: String!, $name: String!, $expression: String!) {
  repository(owner: $owner, name: $name) {
    object(expression: $expression) { ... on Tree { entries { name mode } } }
  }
}
"""


def tree_modes(repo: str, sha: str, paths: list[str]) -> dict[str, str]:
    """Each path's git mode at `sha` (octal text, `100644`), read from its
    parent tree. Raises CarryUnavailable on any read that does not answer."""
    owner, _, name = repo.partition("/")
    by_dir: dict[str, list[str]] = {}
    for path in paths:
        parent, _, base = path.rpartition("/")
        by_dir.setdefault(parent, []).append(base)
    modes: dict[str, str] = {}
    for parent, names in sorted(by_dir.items()):
        cmd = ["gh", "api", "graphql", "-f", f"query={_TREE_MODES_QUERY}"]
        cmd += ["-f", f"owner={owner}", "-f", f"name={name}", "-f", f"expression={sha}:{parent}"]
        try:
            data = json.loads(_gh_read(cmd))
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            raise CarryUnavailable(
                f"reading modes at {sha[:12]} failed: {_first_line(exc)}"
            ) from exc
        tree = (((data or {}).get("data") or {}).get("repository") or {}).get("object")
        entries = tree.get("entries") if isinstance(tree, dict) else None
        if not isinstance(entries, list):
            raise CarryUnavailable(f"no tree for `{parent or '.'}` at {sha[:12]}")
        found = {e.get("name"): e.get("mode") for e in entries if isinstance(e, dict)}
        for base in names:
            mode = found.get(base)
            if not isinstance(mode, int):
                raise CarryUnavailable(f"no mode for `{parent}/{base}` at {sha[:12]}")
            modes[f"{parent}/{base}" if parent else base] = f"{mode:o}"
    return modes


def _summary(verdict: Verdict) -> str:
    lines = ["### Sensitive Change Review", ""]
    if verdict.sensitive:
        lines.append("Sensitive files changed:")
        lines += [f"- `{f}`" for f in verdict.sensitive]
        lines.append("")
    lines += [f"- {m}" for m in verdict.messages]
    if not verdict.ok and not verdict.fork:
        head_line = f"- Head commit: {verdict.head}" if verdict.head else "- Head commit:"
        lines += [
            "",
            "How to clear this check:",
            "",
            "1. The PR author posts the template below as a PR comment, with",
            "   every field filled (`N/A` needs a reason). Keep the",
            "   `Head commit` line: it must name the current head SHA.",
            "2. The check turns green within a minute, with no push.",
            "3. A new push resets this check: post it again for the new head.",
            "   A push that leaves every sensitive file's diff byte-identical",
            "   (a plain rebase) keeps the record for the earlier head.",
            "",
            "Only the author's comment counts this way. A reviewer can instead",
            'paste the filled template into an "Approve" review on this head.',
            "Merging still needs a reviewer's approval.",
            "",
            "```",
            HEADING,
            head_line,
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
    # A file, not stdin: the job's rate-limit wrapper re-runs a rate-limited
    # call, and a retry fed on stdin would send an empty body.
    # The file is closed before gh opens it, which Windows requires.
    with tempfile.TemporaryDirectory() as tmp:
        payload = os.path.join(tmp, "body.json")
        with open(payload, "w", encoding="utf-8") as out:
            json.dump({"body": body}, out)
        subprocess.run(
            ["gh", "api", "-X", method, path, "--input", payload],
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


def upsert_sticky(
    repo: str, pr_number: str, body: str, comments: list[dict], create: bool = True
) -> None:
    """Create (when `create`) or update the sticky comment. Never changes the verdict."""
    try:
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
        # The author's template comment can clear the gate, so an unread
        # comment list fails closed like the other reads.
        comments = _gh_json(f"repos/{repo}/issues/{pr_number}/comments?per_page=100", paginate=True)
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError) as exc:
        # A read failure is not a passing PR: fail closed and say why.
        detail = type(exc).__name__
        if isinstance(exc, subprocess.CalledProcessError):
            detail += f": {_first_line(exc)}"
        print(f"::error::Could not read the PR from the API ({detail}); re-run this job.")
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

    base_ref = ((pr.get("base") or {}).get("ref")) or ""
    compared: dict[str, SensitivePatches | CarryUnavailable] = {}

    def patches_of(sha: str) -> SensitivePatches:
        # A three-dot compare diffs `sha` against its merge base with the base
        # branch, which is what the PR's own file list shows for its head.
        if sha not in compared:
            try:
                if not base_ref:
                    raise CarryUnavailable("the PR names no base branch")
                path = f"repos/{repo}/compare/{quote(base_ref, safe='')}...{sha}?per_page=1"
                try:
                    data = _gh_json(path)
                except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
                    raise CarryUnavailable(
                        f"GET compare for {sha[:12]} failed: {_first_line(exc)}"
                    ) from exc
                compare_rows = data.get("files") if isinstance(data, dict) else None
                if not isinstance(compare_rows, list):
                    raise CarryUnavailable(f"the compare for {sha[:12]} lists no files")
                compared[sha] = sensitive_patches(
                    compare_rows, lambda paths: tree_modes(repo, sha, paths)
                )
            except CarryUnavailable as exc:
                compared[sha] = exc
        result = compared[sha]
        if isinstance(result, CarryUnavailable):
            raise result
        return result

    try:
        verdict = evaluate(
            pr,
            changed_paths(rows),
            reviews,
            permission_of,
            head=event_head,
            comments=comments,
            patches_of=patches_of,
        )
    except PermissionReadError as exc:
        print(f"::error::Could not read a reviewer's permission ({exc}); re-run this job.")
        return 2
    text = _summary(verdict)
    print(text)
    # Only a same-repository PR gets the comment: a fork is refused regardless.
    # With no sensitive file left, an existing comment is refreshed (so it stops
    # asking for an approval) but no new one is created.
    if not is_fork(pr):
        upsert_sticky(
            repo, pr_number, comment_body(verdict), comments, create=bool(verdict.sensitive)
        )
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(text)
    if not verdict.ok:
        print("::error::Sensitive path changed without a reasoning template for this head.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
