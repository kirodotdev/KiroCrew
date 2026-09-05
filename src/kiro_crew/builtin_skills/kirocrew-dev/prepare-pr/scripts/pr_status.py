#!/usr/bin/env python3
"""pr_status.py - the decisive PR-readiness gate for the prepare-pr skill.

Prints PR state + every CI check + advisory unresolved-thread count and returns
an exit code that drives the poll loop. The aggregate ``PR Readiness`` status is
authoritative when present; older PRs fall back to the full check rollup.
Stdlib only; portable.

Usage:  python3 pr_status.py [pr-number] [--readiness-context NAME]
                             [--reviewers NAME1,NAME2] [--json]
        python3 pr_status.py --disposition-gate --repo OWNER/NAME --pr N
                             --head SHA
        (no number -> auto-detect the PR for the current branch;
         --readiness-context / PREPARE_PR_READINESS_CONTEXT override the
         aggregate status-context name, default "PR Readiness";
         --reviewers / PREPARE_PR_REVIEWERS pin the reviewer fleet: only the
         named stamps are evaluated AND each named reviewer must have a fresh
         stamp; by default, every ``[<NAME>-REVIEWED]`` stamp found in bot
         comments is held to freshness, and absence is not required;
         --json appends one machine-readable object as the LAST line of stdout
         and changes nothing else -- same exit codes, same prose. Its
         ``progress_key`` sub-object is the only part safe to compare between
         runs; a monitoring loop uses it to tell a stalled PR from a moving one;
         --disposition-gate evaluates ONLY the disposition rule for an
         explicitly given repo/PR/head, prints one JSON object and exits 0 --
         this is what pr-readiness.yml calls to enforce the rule server-side,
         so the rule keeps a single definition)

Exit codes:
   0  CLEAN     - open, non-draft, MERGEABLE, no CHANGES_REQUESTED, aggregate
                  PR Readiness (or the legacy full rollup) passed, every
                  reviewer stamp matches the current head, no [BLOCK-MERGE]
                  marker for the current head, and a pull_request-event run
                  exists for the current head (when the repo uses Actions)
  10  RUNNING   - a required check is still queued/in-progress, or mergeability
                  has not been computed yet
  20  BLOCKED   - failing readiness, merge conflict, draft, CHANGES_REQUESTED,
                  a terminal PR state (MERGED/CLOSED), a stale reviewer stamp,
                  a blocking review marker on the current head, no
                  pull_request-event run for the current head, a disposition
                  comment violating the one-lane / one-rationale-per-finding
                  rule, or anything that cannot be confirmed
   2  ENV ERROR - gh missing or not authenticated, or PR not found
"""

import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys


class _NoBytecodeSourceLoader(importlib.machinery.SourceFileLoader):
    """Load shipped source normally while suppressing cache writes."""

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        source = self.get_data(path)
        return self.source_to_code(source, path)

    def set_data(self, path, data, *, _mode=0o666):
        return None


def _load_review_contract():
    """Load the sibling contract without cwd, sys.path, or bytecode side effects."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_review_contract.py")
    name = "_prepare_pr_review_contract"
    loader = _NoBytecodeSourceLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:  # pragma: no cover - defensive
        raise RuntimeError("cannot import prepare-pr review contract: " + path)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


_review_contract = _load_review_contract()
REVIEWED_STAMP_RE = _review_contract.REVIEWED_STAMP_RE
BLOCK_MERGE_RE = _review_contract.BLOCK_MERGE_RE
DEFAULT_MARKER_AUTHORS = _review_contract.DEFAULT_MARKER_AUTHORS
DEFAULT_MARKER_BINDINGS = _review_contract.DEFAULT_MARKER_BINDINGS
_COMMENT_KEY_RE = _review_contract._COMMENT_KEY_RE
FINDING_RE = _review_contract.FINDING_RE
DISPOSITION_PREFIX = _review_contract.DISPOSITION_PREFIX
DISPOSITION_MARKER_RE = _review_contract.DISPOSITION_MARKER_RE
SPAN_CLAIM_RE = _review_contract.SPAN_CLAIM_RE
DISPOSITION_BULLET_RE = _review_contract.DISPOSITION_BULLET_RE
span_hash = _review_contract.span_hash
sha_matches = _review_contract.sha_matches
comment_key = _review_contract.comment_key
extract_findings = _review_contract.extract_findings
parse_disposition_record = _review_contract.parse_disposition_record


# Strip ANSI escape sequences and C0/C1 control chars from untrusted printed
# text (PR titles / check names are attacker-controllable) to prevent
# terminal/prompt injection into the agent session. The C1 range (\x80-\x9f)
# matters: U+009B is the single-byte CSI, equivalent to ESC-[.
_CTRL_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize(s):
    return _CTRL_RE.sub("", s or "")


# Explicit state classification (classify every state; fail closed).
PASS_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
# StatusContext (legacy commit statuses) use .state rather than .conclusion.
CTX_PASS = {"SUCCESS"}
CTX_RUNNING = {"PENDING", "EXPECTED"}
DEFAULT_READINESS_CONTEXT = "PR Readiness"

# A host closes an issue on merge ONLY for these verbs. "Related: #n", "Part of
# #n" and a bare "#n" render as links and close nothing, which is how finished
# work merges while its issue stays open forever.
_CLOSING_VERB = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
# A repository slug, in GitHub's own charset. Deliberately narrow so a stray
# path fragment cannot masquerade as a qualified reference.
_REPO_SLUG = r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
# The three reference targets the host actually resolves.
_ISSUE_TARGET = (
    r"(?:(?:" + _REPO_SLUG + r")?#\d+" r"|https?://[A-Za-z0-9.-]+/" + _REPO_SLUG + r"/issues/\d+)"
)
_CLOSING_REF = _CLOSING_VERB + r"[ \t]*:?[ \t]+" + _ISSUE_TARGET
# THE ACCEPTED EXPLICIT-TRAILER GRAMMAR, in full:
#
#   trailer := indent? bullet? ref (sep ref)* punct? html-comment?
#   ref     := verb ':'? sp target
#   verb    := close|closes|closed | fix|fixes|fixed | resolve|resolves|resolved
#   target  := '#123' | 'owner/repo#123' | 'https://host/owner/repo/issues/123'
#   sep     := ',' | ';' | 'and'
#
# Two properties are load-bearing.
#
# (1) The trailer must occupy the WHOLE visible line. An unanchored substring
# match also accepted prose that merely MENTIONS a past close -- "Fixed #123 in
# an earlier release; this PR only adds tests." -- and then told the author the
# keyword was fine and the NUMBER was wrong, the one reading that is never true
# for that line. A declaration is a trailer, not a mention. (Trailing
# whitespace, one sentence-ending '.'/';', a CR from a CRLF body, and a trailing
# HTML comment stay accepted, since none of them make the line prose.)
#
# (2) Qualified and URL targets are accepted. The host resolves them, so a body
# carrying one is NOT a body that forgot the verb. No reconciliation against the
# host's own repository identity is needed to accept them here: this classifier
# is only ever reached when the host resolved NOTHING, and the message it
# produces ("the verb is fine, check the reference") is correct whether the
# reference names this repository or another one.
#
# Every regex here runs against the MASKED body from
# ``_visible_markdown_prose``, never the raw one: a fenced block, an indented
# code block, an inline code span and an HTML comment are the surfaces GitHub
# itself does not resolve a closure from, and they are exactly where our own body
# template keeps its worked examples. Matching them would let a copied template
# read as a declaration the author never made.
#
# (3) INDENTATION IS CAPPED AT THREE COLUMNS, and the cap is the load-bearing
# half of that. Four columns is CommonMark's own code boundary, so this single
# bound refuses every code-indented example without the classifier needing to
# know which block type precedes it. Earlier revisions allowed any indent here
# and leaned entirely on the mask to tell code from prose, which required
# tracking whether a paragraph was open -- and that tracker was wrong for an ATX
# heading, a blockquote, a thematic break and a setext underline, each of which
# closes its block so the next indented line IS code. Enumerating block types
# converges on writing a Markdown parser; bounding the indent does not. A tab is
# refused for the same reason (it advances to the four-column stop).
#
# What the cap costs: a trailer indented four or more columns that GitHub WOULD
# resolve as lazy paragraph continuation is no longer credited as a declaration.
# That is the cheap direction -- it prints an advisory notice on an odd body,
# where the opposite error credits an EXAMPLE and silently suppresses a real
# unrelated-closure warning -- and a trailer is written as a whole line of its
# own, never as indented code.
_CLOSING_KW_RE = re.compile(
    r"^ {0,3}(?:[-*+][ \t]+)?"
    + _CLOSING_REF
    + r"(?:(?:[ \t]*[,;][ \t]*|[ \t]+and[ \t]+)"
    + _CLOSING_REF
    + r")*[ \t]*[.;]?[ \t]*(?:<!--.*?-->[ \t]*)?\r?$",
    re.IGNORECASE | re.MULTILINE,
)
# A resolved closure with no matching explicit trailer may come from prose or a
# manual link in the host UI, so every host-resolved issue number needs human
# confirmation unless the body DECLARES it. Declared numbers are read with the
# same grammar the accept path uses -- find each whole-line trailer with
# ``_CLOSING_KW_RE``, then read every reference on that line -- so one grammar
# governs both directions. Deriving them separately is what would report a
# legitimate trailer (bulleted, qualified ``owner/repo#n``, an issue URL, or
# several references on one line) as missing.
# The slug is CAPTURED, not merely matched: reconciling a host closure against
# a trailer on the number alone lets a stale ``Fixes other/repo#7`` vouch for a
# closure of THIS repository's own #7, which suppresses the very warning this
# function exists to print.
_CLOSING_REF_RE = re.compile(
    _CLOSING_VERB + r"[ \t]*:?[ \t]+"
    r"(?:(?P<slug>" + _REPO_SLUG + r")?#(?P<number>\d+)"
    r"|https?://[A-Za-z0-9.-]+/(?P<url_slug>" + _REPO_SLUG + r")"
    r"/issues/(?P<url_number>\d+))",
    re.IGNORECASE,
)
# Any issue-ish reference at all, used to tell "forgot the verb" from
# "genuinely closes nothing". Mirrors the same three targets, so a qualified
# ref or an issue URL written without a verb is reported as a missing keyword
# rather than as a body with no issue link at all.
_BARE_REF_RE = re.compile(
    r"(?<![\w/])(?:" + _REPO_SLUG + r")?#\d+\b"
    r"|https?://[A-Za-z0-9.-]+/" + _REPO_SLUG + r"/issues/\d+\b",
    re.IGNORECASE,
)
# Explicit opt-out so an issue-less PR can say so once instead of being asked
# every round. Anchored at column 0 and requires the colon, because an
# UNANCHORED substring is satisfied by any prose that merely discusses this
# check — including the instruction block of our own body template, which would
# make an author who copies the template and skips that section look like they
# declared something. A declaration is a trailer, not a mention.
# The phrasing deliberately contains no GitHub closing keyword
# (close/closes/closed, fix/fixes/fixed, resolve/resolves/resolved): a keyword
# directly before the colon would turn a '#<n>' at the start of the <why> into
# '<keyword>: #<n>', which GitHub parses as a close-on-merge trigger — the
# opt-out would then auto-close the very issue it explains not closing.
_NO_ISSUE_RE = re.compile(r"^no linked issue[ \t]*:", re.IGNORECASE | re.MULTILINE)

# Issue-link classification must see the same Markdown surface GitHub treats as
# prose. Mask ignored contexts character-for-character so MULTILINE anchors and
# word boundaries keep their source positions. Fences allow up to three leading
# spaces; a backtick fence's info string cannot itself contain a backtick.
#
# Markdown's OTHER code block -- four or more columns of indentation -- is NOT
# tracked here, deliberately. Recognising it requires knowing whether a paragraph
# is open, which is a Markdown parser's job: an approximation of that state was
# wrong for an ATX heading, a blockquote, a thematic break and a setext underline,
# each of which closes its block so the next indented line IS code. The bound in
# ``_CLOSING_KW_RE`` covers every one of those shapes without consulting block
# state, so the state machine is not needed and is not here.
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


def _fence_opening(line):
    match = _FENCE_OPEN_RE.match(line)
    if not match:
        return None
    fence = match.group("fence")
    if fence[0] == "`" and "`" in match.group("info"):
        return None
    return fence[0], len(fence)


def _is_closing_fence(line, fence_char, opening_length):
    candidate = line[:-1] if line.endswith("\r") else line
    indent = 0
    while indent < len(candidate) and candidate[indent] == " ":
        indent += 1
    if indent > 3:
        return False
    end = indent
    while end < len(candidate) and candidate[end] == fence_char:
        end += 1
    return end - indent >= opening_length and not candidate[end:].strip(" \t")


def _mask_html_comments(line, in_comment):
    masked = list(line)
    cursor = 0
    if in_comment:
        comment_end = line.find("-->")
        if comment_end < 0:
            return " " * len(line), True
        cursor = comment_end + 3
        masked[:cursor] = " " * cursor

    while True:
        comment_start = line.find("<!--", cursor)
        if comment_start < 0:
            return "".join(masked), False
        comment_end = line.find("-->", comment_start + 4)
        if comment_end < 0:
            masked[comment_start:] = " " * (len(line) - comment_start)
            return "".join(masked), True
        cursor = comment_end + 3
        masked[comment_start:cursor] = " " * (cursor - comment_start)


def _mask_inline_code(text):
    runs = []
    cursor = 0
    while cursor < len(text):
        start = text.find("`", cursor)
        if start < 0:
            break
        end = start + 1
        while end < len(text) and text[end] == "`":
            end += 1
        runs.append((start, end))
        cursor = end

    next_equal: list[int | None] = [None] * len(runs)
    next_by_length: dict[int, int] = {}
    for index in range(len(runs) - 1, -1, -1):
        run_length = runs[index][1] - runs[index][0]
        next_equal[index] = next_by_length.get(run_length)
        next_by_length[run_length] = index

    masked = list(text)
    index = 0
    while index < len(runs):
        closing_index = next_equal[index]
        if closing_index is None:
            index += 1
            continue
        start = runs[index][0]
        end = runs[closing_index][1]
        for offset in range(start, end):
            if masked[offset] not in "\r\n":
                masked[offset] = " "
        index = closing_index + 1
    return "".join(masked)


def _visible_markdown_prose(body):
    """Mask fenced/code-span/comment examples while preserving line offsets."""
    body = body or ""
    visible_lines = []
    fence_char = None
    fence_length = 0
    in_comment = False

    for line in body.split("\n"):
        if fence_char is not None:
            if _is_closing_fence(line, fence_char, fence_length):
                fence_char = None
                fence_length = 0
            visible_lines.append(" " * len(line))
            continue

        # A real fence opener wins over comment-looking text in its info
        # string. While inside a comment, masking happens first so a fence-like
        # example cannot alter fence state.
        opening = None if in_comment else _fence_opening(line)
        if opening is not None:
            fence_char, fence_length = opening
            visible_lines.append(" " * len(line))
            continue

        masked_line, in_comment = _mask_html_comments(line, in_comment)
        opening = None if in_comment else _fence_opening(masked_line)
        if opening is not None:
            fence_char, fence_length = opening
            visible_lines.append(" " * len(line))
            continue
        visible_lines.append(masked_line)

    return _mask_inline_code("\n".join(visible_lines))


# GitHub exposes ``Issue.number`` as a positive GraphQL Int. Normalize the
# host value and trailer text without converting untrusted digit strings to a
# Python int: Python 3.10 accepts arbitrarily long strings while 3.11+ raises,
# and readiness must behave identically on every supported interpreter.
_GITHUB_ISSUE_NUMBER_MAX = 2_147_483_647
_GITHUB_ISSUE_NUMBER_MAX_TEXT = "2147483647"


def _normalize_issue_number(value):
    """Return canonical decimal text for a GitHub issue number, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if not 0 < value <= _GITHUB_ISSUE_NUMBER_MAX:
            return None
        return str(value)
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or not value.isdigit()
        or len(value) > len(_GITHUB_ISSUE_NUMBER_MAX_TEXT)
    ):
        return None
    normalized = value.lstrip("0")
    if not normalized:
        return None
    if (
        len(normalized) == len(_GITHUB_ISSUE_NUMBER_MAX_TEXT)
        and normalized > _GITHUB_ISSUE_NUMBER_MAX_TEXT
    ):
        return None
    return normalized


def _issue_number_sort_key(number):
    return (len(number), number)


def _normalize_repo_key(value):
    """Return canonical case-folded ``owner/repo`` text, else None.

    ``owner/repo`` is a GitHub API slug, NOT a filesystem path: the ``/`` is the
    API's own literal separator, so ``os.path.join`` would be wrong here (it
    emits a backslash on Windows). Built with ``format`` rather than ``+`` so
    the portability scanner's path-assembly rule does not read it as one.

    GitHub owner and repository names are case-insensitive, so the comparison
    must be too: otherwise ``Fixes KiroDotDev/KiroCrew#7`` would read as naming
    a different repository from the host's own ``kirodotdev/KiroCrew`` and the
    trailer would stop covering the closure it plainly declares.
    """
    if not isinstance(value, str):
        return None
    owner, _, name = value.partition("/")
    if not owner or not name or "/" in name:
        return None
    return "{}/{}".format(owner.lower(), name.lower())


def _host_ref_repo_key(ref):
    """Return the case-folded ``owner/repo`` a host closing reference names."""
    if not isinstance(ref, dict):
        return None
    repository = ref.get("repository")
    if not isinstance(repository, dict):
        return None
    owner = repository.get("owner")
    login = owner.get("login") if isinstance(owner, dict) else None
    name = repository.get("name")
    if not isinstance(login, str) or not isinstance(name, str):
        return None
    return _normalize_repo_key("{}/{}".format(login, name))


def _undeclared_closing_numbers(declared, closing_refs):
    """Return (resolved numbers, numbers the body does not declare, well_formed).

    Accounting is per REPOSITORY, not per number, and the asymmetry between the
    two kinds of declaration is the whole point:

    * A QUALIFIED declaration (``Fixes owner/repo#7``, an issue URL, or a bare
      ``#7`` when the caller supplied the PR's own repository) covers every host
      reference naming that same repository.
    * A WILDCARD declaration -- a bare ``#7`` whose repository cannot be
      resolved because the caller passed none -- covers exactly ONE reference.
      One unqualified trailer cannot honestly vouch for two closures of the same
      number in two different repositories, so the second is reported.

    A host reference carrying no ``repository`` object is covered by any
    declaration of its number: nothing there can be reconciled in either
    direction, and inventing a mismatch would fire this advisory on a body that
    is fine.

    Deliberately absent: a "the same number resolved twice" notice. An earlier
    revision had one, and once matching became repository-aware it fired on a
    fully correct body -- ``Fixes #7`` plus ``Fixes other/repo#7`` with both
    closures resolved and both matched. Genuine ambiguity is already the
    undeclared case above, so the branch was redundant as well as wrong.
    """
    known: dict = {}
    wildcards: dict = {}
    for repo_key, number in declared:
        if repo_key is None:
            wildcards[number] = wildcards.get(number, 0) + 1
        else:
            known.setdefault(number, set()).add(repo_key)

    numbers = []
    undeclared = set()
    well_formed = True
    for ref in closing_refs:
        try:
            value = ref["number"]
        except (KeyError, TypeError):
            well_formed = False
            continue
        number = _normalize_issue_number(value)
        if number is None:
            well_formed = False
            continue
        numbers.append(number)
        host_repo = _host_ref_repo_key(ref)
        if host_repo is not None and host_repo in known.get(number, ()):
            continue
        if wildcards.get(number):
            wildcards[number] -= 1
            continue
        if host_repo is None and known.get(number):
            continue
        undeclared.add(number)
    return numbers, undeclared, well_formed


def _declared_closing_numbers(visible_body, base_repo=None):
    """Return (declared ``(repo_key, number)`` pairs, well_formed).

    ``repo_key`` is the case-folded ``owner/repo`` the trailer names explicitly,
    else *base_repo* for an unqualified ``#<n>`` (which can only mean the PR's
    own repository), else ``None`` when neither is known.

    ``well_formed`` is False when a trailer names a number the host could never
    have issued (out of GitHub's positive-Int32 range, or all zeros), which is
    reported rather than silently dropped: a body that declares a closure we
    cannot line up against the host's answer still needs a human to confirm the
    closures that DID resolve are the intended ones.
    """
    base_key = _normalize_repo_key(base_repo)
    declared = set()
    well_formed = True
    for line in _CLOSING_KW_RE.finditer(visible_body):
        for ref in _CLOSING_REF_RE.finditer(line.group(0)):
            number = _normalize_issue_number(ref.group("number") or ref.group("url_number"))
            if number is None:
                well_formed = False
                continue
            slug = ref.group("slug") or ref.group("url_slug")
            declared.add((_normalize_repo_key(slug) or base_key, number))
    return declared, well_formed


def closing_link_reason(body, closing_refs, repo=None):
    """Return an advisory issue-link reason, else None.

    ADVISORY ONLY — the caller prints this, it never changes the exit code. An
    issue-less PR is legitimate, and a green PR should not be held on
    bookkeeping.

    ``closing_refs`` is the host's OWN resolution of the body (the
    ``closingIssuesReferences`` field), so it is the truth about what will
    actually close. The body regexes classify *why* it resolved to nothing --
    which is what makes the message actionable -- and, when it DID resolve
    something, whether every resolved issue is backed by an explicit trailer or
    whether a closure needs human confirmation. The accepted explicit-trailer
    grammar is stated in full above ``_CLOSING_KW_RE``: a whole visible line
    carrying one or more ``<verb> <target>`` references, where a target may be
    ``#123``, ``owner/repo#123`` or an issue URL.

    ``repo`` is the viewed PR's own ``owner/name`` when the caller knows it. It
    is what an unqualified ``#<n>`` trailer resolves to, so supplying it lets a
    resolved closure be matched by REPOSITORY AND NUMBER rather than by number
    alone. Optional, and omitting it only widens the match.
    """
    body = body or ""
    visible_body = _visible_markdown_prose(body)
    if closing_refs:
        declared, trailers_well_formed = _declared_closing_numbers(visible_body, repo)
        _resolved, missing_numbers, refs_well_formed = _undeclared_closing_numbers(
            declared, closing_refs
        )
        if refs_well_formed and trailers_well_formed and not missing_numbers:
            return None
        if not trailers_well_formed:
            return (
                "body has a malformed explicit closing trailer "
                "(confirm every closure is intentional; use one valid "
                "'Fixes #<n>' trailer per issue)"
            )
        if not refs_well_formed:
            return (
                "host returned a malformed closing issue reference "
                "(confirm every closure is intentional)"
            )
        missing_text = (
            " matching "
            + ", ".join(
                "#{}".format(number)
                for number in sorted(missing_numbers, key=_issue_number_sort_key)
            )
            if missing_numbers
            else " matching every host-resolved closure"
        )
        return (
            "host will close an issue but the body has no explicit closing trailer"
            + missing_text
            + " (confirm every closure is intentional; add one 'Fixes #<n>' "
            "trailer per issue)"
        )
    if _NO_ISSUE_RE.search(visible_body):
        return None
    if _CLOSING_KW_RE.search(visible_body):
        # A visible verb is present but the host resolved nothing: the number,
        # repository target, or issue state does not form a live closure. A code
        # fence is no longer a candidate explanation -- fenced text is masked
        # before this runs, so it cannot reach here in the first place.
        return (
            "body has a closing keyword but the host resolved no issue "
            "(check the number, repository, and issue state)"
        )
    if _BARE_REF_RE.search(visible_body):
        return (
            "body references an issue with no closing keyword - use "
            "'Fixes #<n>' so it closes on merge, or state 'no linked issue: <why>'"
        )
    return (
        "no issue link - add 'Fixes #<n>', or state 'no linked issue: <why>' "
        "to record that the omission is deliberate"
    )


# Page cap so a pathological PR can't make us loop unbounded (100 * 50 = 5000).
_MAX_THREAD_PAGES = 50
_MAX_COMMENT_PAGES = 50

FINDING_LINE_RE = re.compile(r"^\s*FINDING\b", re.MULTILINE)


def fetch_disposition_comments(repo, number):
    return _review_contract.fetch_disposition_comments(repo, number, run)


def author_write_verdict(repo, login):
    return _review_contract.author_write_verdict(repo, login, run)


def author_is_repo_writer(repo, login):
    return _review_contract.author_is_repo_writer(repo, login, run)


def writer_disposition_records(repo, comments):
    return _review_contract.writer_disposition_records(repo, comments, run, author_write_verdict)


disposition_violations = _review_contract.disposition_violations


def resolve_marker_bindings(argv, environ):
    """Resolve the comment-key -> reviewer-name bindings (flag > env > default)."""
    raw = None
    for i, a in enumerate(argv):
        if a == "--marker-bindings" and i + 1 < len(argv):
            raw = argv[i + 1]
        elif a.startswith("--marker-bindings="):
            raw = a.split("=", 1)[1]
    if raw is None:
        raw = environ.get("PREPARE_PR_MARKER_BINDINGS")
    if not raw:
        return dict(DEFAULT_MARKER_BINDINGS)
    out = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, _, v = pair.partition("=")
            if k.strip() and v.strip():
                out[k.strip()] = v.strip().upper()
    return out or dict(DEFAULT_MARKER_BINDINGS)


def resolve_marker_authors(argv, environ):
    """Resolve the comment-author allowlist for marker evaluation.

    Precedence mirrors the other seams: ``--marker-authors`` CLI flag >
    ``PREPARE_PR_MARKER_AUTHORS`` env var > DEFAULT_MARKER_AUTHORS. Logins are
    compared case-insensitively.
    """
    raw = None
    for i, a in enumerate(argv):
        if a == "--marker-authors" and i + 1 < len(argv):
            raw = argv[i + 1]
        elif a.startswith("--marker-authors="):
            raw = a.split("=", 1)[1]
    if raw is None:
        raw = environ.get("PREPARE_PR_MARKER_AUTHORS")
    if not raw:
        return {a.lower() for a in DEFAULT_MARKER_AUTHORS}
    return {n.strip().lower() for n in raw.split(",") if n.strip()} or {
        a.lower() for a in DEFAULT_MARKER_AUTHORS
    }


def resolve_readiness_context(argv, environ):
    """Resolve the aggregate-readiness status-context name.

    Precedence: ``--readiness-context`` CLI flag > ``PREPARE_PR_READINESS_CONTEXT``
    env var > the KiroCrew default. Lets a project profile name a non-default
    aggregate status; when unset, behavior is identical to before (the default
    context, with the full-rollup fallback when that context is absent).
    """
    for i, a in enumerate(argv):
        if a == "--readiness-context" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--readiness-context="):
            return a.split("=", 1)[1]
    env = environ.get("PREPARE_PR_READINESS_CONTEXT")
    return env if env else DEFAULT_READINESS_CONTEXT


def resolve_reviewers(argv, environ):
    """Resolve an optional reviewer-name filter for marker evaluation.

    Precedence mirrors ``resolve_readiness_context``: ``--reviewers`` CLI flag >
    ``PREPARE_PR_REVIEWERS`` env var > None (discovery mode). Discovery mode
    evaluates every ``[<NAME>-REVIEWED]`` stamp found, which self-configures on
    any repo whose reviewers emit the stamp contract. Naming reviewers both
    scopes freshness to those names AND requires each to have a fresh stamp --
    a pinned reviewer that never posted reads as stale, so an emitter drift or
    a bot that fails to post cannot make the gate silently vacuous.
    """
    raw = None
    for i, a in enumerate(argv):
        if a == "--reviewers" and i + 1 < len(argv):
            raw = argv[i + 1]
        elif a.startswith("--reviewers="):
            raw = a.split("=", 1)[1]
    if raw is None:
        raw = environ.get("PREPARE_PR_REVIEWERS")
    if not raw:
        return None
    names = {n.strip().upper() for n in raw.split(",") if n.strip()}
    return names or None


def head_run_check_enabled(argv, environ):
    """Whether the pull_request-run-for-head assertion is enabled.

    ``--head-run-check=off`` / ``PREPARE_PR_HEAD_RUN_CHECK=off`` disable it --
    the escape hatch for a repo shape the event heuristic misreads, degrading
    that one gate to pre-existing behavior instead of a permanent block. Any
    other value (or unset) keeps it on.
    """
    val = None
    for i, a in enumerate(argv):
        if a == "--head-run-check" and i + 1 < len(argv):
            val = argv[i + 1]
        elif a.startswith("--head-run-check="):
            val = a.split("=", 1)[1]
    if val is None:
        val = environ.get("PREPARE_PR_HEAD_RUN_CHECK")
    return (val or "").strip().lower() not in ("off", "0", "false", "no")


_VALUE_FLAGS = (
    "--readiness-context",
    "--reviewers",
    "--head-run-check",
    "--marker-authors",
    "--marker-bindings",
)

# Flags that take NO value. positional_args must drop these too: a bare
# --json left in the positional list is read as the PR number, which silently
# resolves the wrong PR (or fails env-check) instead of erroring.
_BOOL_FLAGS = ("--json",)


def positional_args(argv):
    """Return argv with the flags (and any values they take) removed."""
    out = []
    skip = False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in _BOOL_FLAGS:
            continue
        if a in _VALUE_FLAGS:
            skip = True
            continue
        if a.startswith(tuple(f + "=" for f in _VALUE_FLAGS)):
            continue
        out.append(a)
    return out


def run(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except OSError as exc:
        return 127, "", "{}: {}".format(args[0], exc)


def err(msg):
    sys.stderr.write(msg + "\n")


# statusCheckRollup needs Checks read access, which a fine-grained PAT
# structurally cannot grant, and gh resolves every field of one --json request
# atomically -- so bundling the rollup with the core fields makes the WHOLE
# read fail for those tokens. The rollup is therefore fetched in its own call
# (fetch_check_rollup) and degrades softly: the caller keeps the core metadata
# and reports CI as unknown instead of aborting. The second read re-fetches
# headRefOid and is discarded on a mismatch with the core read's head, so a
# push landing between the two reads can never pair one head's metadata with
# another head's checks. The parity-pinned copy in pr_findings.py keeps each
# command's check-rollup path explicit.
ROLLUP_UNAVAILABLE_NOTICE = (
    "CI check status UNAVAILABLE - the statusCheckRollup fetch failed (a token "
    "without Checks read access, e.g. any fine-grained PAT, cannot fetch it); "
    "treat CI as UNKNOWN, not as 'no checks yet'"
)
ROLLUP_HEAD_MOVED_NOTICE = (
    "CI check status DISCARDED - the PR head changed between the core read and "
    "the rollup read (concurrent push); treat CI as UNKNOWN and re-run for a "
    "consistent snapshot"
)


def fetch_check_rollup(pr, expected_head):
    """Return (rollup entries, notice); the notice is non-empty when degraded."""
    rc, out, _ = run(["gh", "pr", "view", pr, "--json", "headRefOid,statusCheckRollup"])
    if rc == 0 and out.strip():
        try:
            d = json.loads(out)
        except ValueError:
            d = None
        if isinstance(d, dict):
            if expected_head and (d.get("headRefOid") or "").strip() != expected_head:
                return [], ROLLUP_HEAD_MOVED_NOTICE
            return d.get("statusCheckRollup") or [], ""
    return [], ROLLUP_UNAVAILABLE_NOTICE


def classify_check(entry):
    """Return 'pass' | 'running' | 'fail' for one statusCheckRollup entry.

    Fail-closed: any unrecognized COMPLETED conclusion or unknown shape counts
    as 'fail' rather than silently passing.
    """
    status = (entry.get("status") or "").upper()
    conclusion = (entry.get("conclusion") or "").upper()
    state = (entry.get("state") or "").upper()
    if status:  # CheckRun
        if status != "COMPLETED":
            return "running"  # queued/in-progress/any non-terminal state
        return "pass" if conclusion in PASS_CONCLUSIONS else "fail"
    if state:  # StatusContext
        if state in CTX_PASS:
            return "pass"
        if state in CTX_RUNNING:
            return "running"
        return "fail"
    return "fail"  # unknown shape -> fail closed


def failing_check_identity(entry):
    """Workflow-qualified label for a failing check, for ``progress_key``.

    Mirrors ``collapse_superseded()``'s identity notion -- a StatusContext is
    keyed by its context name, a CheckRun by (workflow, name) -- because a
    display name alone is not an identity: two workflows can publish the same
    check name. If one workflow's copy starts failing while the other's stops,
    a name-only list is byte-identical across that change, and a stall streak
    would run straight through a PR whose blocking check actually moved.
    """
    context = entry.get("context")
    if context:
        return sanitize(context)
    workflow = sanitize(entry.get("workflowName") or "")
    name = sanitize(entry.get("name") or "check")
    return "{} / {}".format(workflow, name) if workflow else name


def collapse_superseded(rollup):
    """Collapse re-run check attempts to the newest run per check identity.

    GitHub keeps superseded attempts (typically CANCELLED) in the rollup next
    to the run that replaced them; counting them inflates the failure count
    with entries that are no longer live. Identity is the workflow-qualified
    check name for CheckRuns and the context name for StatusContexts; newest
    is decided by startedAt (ISO-8601, so string comparison orders correctly).
    Entries that cannot be strictly ordered against the current winner are all
    kept -- when in doubt, over-report rather than hide a live failure.
    """
    winners = {}
    order = []
    undecidable = []
    for e in rollup:
        context = e.get("context")
        if context:
            key = ("ctx", context, "")
        else:
            key = ("run", e.get("workflowName") or "", e.get("name") or "")
        started = e.get("startedAt") or ""
        if key not in winners:
            winners[key] = (started, e)
            order.append(key)
            continue
        prev_started, prev = winners[key]
        if started and prev_started:
            if started > prev_started:
                winners[key] = (started, e)
        else:
            undecidable.append(e)  # no ordering evidence -> keep both
    return [winners[k][1] for k in order] + undecidable


def unresolved_thread_count(number):
    """Count unresolved review threads across all pages for advisory output."""
    rc, repo, _ = run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    if rc != 0 or "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    query = (
        "query($o:String!,$r:String!,$n:Int!,$c:String){repository(owner:$o,"
        "name:$r){pullRequest(number:$n){reviewThreads(first:100,after:$c)"
        "{pageInfo{hasNextPage endCursor} nodes{isResolved}}}}}"
    )
    cursor = None
    count = 0
    for _ in range(_MAX_THREAD_PAGES):
        args = [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=" + query,
            "-F",
            "o=" + owner,
            "-F",
            "r=" + name,
            "-F",
            "n=" + str(number),
        ]
        if cursor:
            args += ["-F", "c=" + cursor]
        rc, out, _ = run(args)
        if rc != 0 or not out:
            return None
        try:
            rt = json.loads(out)["data"]["repository"]["pullRequest"]["reviewThreads"]
        except (ValueError, KeyError, TypeError):
            return None
        count += sum(1 for t in (rt.get("nodes") or []) if not t.get("isResolved", False))
        page = rt.get("pageInfo") or {}
        if not page.get("hasNextPage") or not page.get("endCursor"):
            return count
        cursor = page["endCursor"]
    return None  # hit the page cap with more pages left -> uncertain (fail-closed)


def detect_repo(pr_url=""):
    """Return "owner/name" for the VIEWED PR, or "" when undetectable.

    Prefers the PR's own URL: the positional argument may be a full PR URL for
    a different repository than the cwd's checkout, and querying the checkout's
    repo for that PR's comments/runs would silently evaluate the wrong data
    (markers invisible, gates vacuous). Falls back to the cwd's repo only when
    no URL is available.
    """
    m = re.match(r"https?://[^/]+/([^/]+)/([^/]+)/pull/\d+", pr_url or "")
    if m:
        return "{}/{}".format(m.group(1), m.group(2))
    rc, repo, _ = run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    return repo.strip() if rc == 0 and "/" in repo else ""


def fetch_bot_comments(repo, number, trusted_authors):
    """Trusted marker-source comments on the PR, across pages; None on error.

    Paginated by hand (PRs here routinely carry 50+ bot comments; a single
    unpaginated read silently truncates). A comment counts only when its
    author is a Bot AND its login is in ``trusted_authors``: the Bot-type
    check alone is spoofable -- any third-party app that echoes PR-controlled
    text would post an attacker-chosen marker and forge freshness. Returns
    None (uncertain, the caller fails closed) on any API/parse error or when
    the page cap is hit with more pages left.
    """
    if not repo:
        return None
    comments: list = []
    for page in range(1, _MAX_COMMENT_PAGES + 1):
        rc, out, _ = run(
            [
                "gh",
                "api",
                "repos/{}/issues/{}/comments?per_page=100&page={}".format(repo, number, page),
            ]
        )
        if rc != 0 or not out:
            return None
        try:
            batch = json.loads(out)
        except ValueError:
            return None
        if not isinstance(batch, list):
            return None
        for c in batch:
            if not isinstance(c, dict):
                continue
            user = c.get("user") or {}
            if user.get("type") != "Bot":
                continue
            if (user.get("login") or "").lower() not in trusted_authors:
                continue
            comments.append(c)
        if len(batch) < 100:
            return comments
    return None


def evaluate_reviewer_markers(comments, head_sha, bindings, only=None):
    """Evaluate reviewer stamps and blocking markers against the current head.

    Returns a dict:
      ok        -- False when the comments could not be read (fail-closed)
      stale     -- sorted reviewer names with no fresh stamp for the head
      blocking  -- sorted reviewer names with [BLOCK-MERGE] <current head>
      findings  -- {name: advisory FINDING-line count} for fresh comments
      pinned    -- whether ``only`` named the fleet. Empty ``stale`` means
                   "every REQUIRED lane stamped this head" only when pinned;
                   in discovery mode it means "every lane that POSTED is
                   fresh", which cannot see a lane that has not spoken yet.

    STRUCTURAL INVARIANT -- reviewer identity comes from WORKFLOW-AUTHORED
    bytes, never from model output. ``bindings`` maps each lane's comment
    upsert key (the leading ``<!-- key -->`` the workflow template writes
    before any model text) to its reviewer name. A stamp counts only when a
    bound comment's OWN name matches it: stamps for other names inside a
    lane's body are injected model output and are ignored, so no lane can
    forge another reviewer's freshness regardless of what its model emits.
    Comments without a bound leading key contribute no freshness. Fail-closed
    asymmetry: a [BLOCK-MERGE] for the current head gates from ANY trusted
    comment, bound or not -- injection can deny a review, never forge one.

    Reviewers required: names in ``only`` when set (a pinned fleet -- absence
    reads as stale, so emitter drift cannot silently un-gate), else every
    BOUND reviewer that posted a comment (discovery mode; a lane that never
    posted is not required, its CI gate covers absence).
    """
    if comments is None or not head_sha:
        return {
            "ok": False,
            "stale": [],
            "blocking": [],
            "findings": {},
            "elided": [],
            "pinned": only is not None,
        }
    fresh_by_name: dict = {name: False for name in (only or ())}
    findings: dict = {}
    blocking = set()
    # Reviewers whose freshness rests on an ELIDED stamp (see sha_matches). The
    # gate accepts those, but silently swallowing them would hide the emitter
    # defect for good: nobody would learn a lane is mangling the SHA it was
    # handed. Reported as an advisory note, never as a blocking reason -- and
    # deliberately absent from progress_key, which a polling loop diffs.
    elided = set()
    for c in comments:
        body = c.get("body") or ""
        name = bindings.get(comment_key(body))
        stamps = REVIEWED_STAMP_RE.findall(body)
        if name and (only is None or name in only):
            own_stamps = [sha for stamp_name, sha in stamps if stamp_name == name]
            # A bound lane is held to freshness in DISCOVERY mode only when
            # its comment carries at least one of its own stamps: the UX and
            # Design workflows rewrite their keyed comment to a stampless
            # "skipped"/"could not complete" notice by design (advisory lanes
            # must not block), and enrolling those would pin exit 20 on a
            # green PR forever. A PINNED lane stays required regardless --
            # that is what pinning means.
            if only is not None or own_stamps:
                fresh = any(sha_matches(sha, head_sha) for sha in own_stamps)
                fresh_by_name[name] = fresh_by_name.get(name, False) or fresh
                if fresh:
                    findings[name] = len(FINDING_LINE_RE.findall(body))
                    if not any(head_sha.startswith(sha) for sha in own_stamps):
                        elided.add(name)
        for sha in BLOCK_MERGE_RE.findall(body):
            if sha_matches(sha, head_sha):
                blocking.add(name or "(unattributed)")
    stale = sorted(n for n, fresh in fresh_by_name.items() if not fresh)
    return {
        "ok": True,
        "stale": stale,
        "blocking": sorted(blocking),
        "findings": findings,
        "elided": sorted(elided),
        "pinned": only is not None,
    }


def reviewer_round_settled(marker_eval):
    """Whether the AI-review round is decided, regardless of the other checks.

    True only when the fleet was PINNED (``--reviewers`` / the loop's own
    profile names), the comments were readable, every pinned lane carries a
    fresh ``[<NAME>-REVIEWED]`` stamp for this head, and at least one posted
    ``[BLOCK-MERGE]``. That combination is terminal for the head: the diff has
    to change, so the tests, packaging and lint runs still in flight are
    running on a commit that is already condemned.

    Deliberately narrow in three ways. It needs a pinned fleet, because in
    discovery mode an empty ``stale`` only says every lane that has SPOKEN is
    fresh -- a lane still composing its review is invisible, and acting on the
    first blocker would throw its verdict away. It needs a blocker, because a
    settled round with no blocker has nothing to act on and must fall through
    to the normal running/clean path. And it says nothing about failing
    non-reviewer checks: a red test while a lane is still pending stays a
    wait, so one round still fixes one full set of findings.
    """
    if not marker_eval:
        return False
    return bool(
        marker_eval.get("pinned")
        and marker_eval.get("ok")
        and not marker_eval.get("stale")
        and marker_eval.get("blocking")
    )


def head_run_exists(repo, head_sha):
    """Whether a pull_request-event workflow run exists for the current head.

    A conflicted or stale PR dispatches no pull_request workflows at all, so
    every check visible belongs to an older head and a status-only loop reports
    'nothing new' forever. The decision reads the CURRENT HEAD's own runs and
    their events -- never repo-wide history, which is wrong in both directions
    (a repo that switched to push-only triggers retains historical
    pull_request runs; a pull_request_target/workflow_run fork-safe repo never
    dispatches the event at all). Returns:
      True   -- a pull_request run exists for this head
      "skip" -- the head has runs, but its CI is driven by other events
                (push / pull_request_target / workflow_run); the PR is not
                held to an event its repo does not use for it
      False  -- an Actions-shaped rollup with NO runs for this head at all:
                the visible checks cannot belong to this head (stale)
      None   -- API error (the caller fails closed with an explicit reason)
    """
    if not repo or not head_sha:
        return None
    rc, out, _ = run(
        [
            "gh",
            "api",
            "repos/{}/actions/runs?head_sha={}&per_page=100".format(repo, head_sha),
        ]
    )
    if rc != 0 or not out:
        return None
    try:
        runs = json.loads(out).get("workflow_runs") or []
        events = {r.get("event") for r in runs if isinstance(r, dict)}
    except (ValueError, TypeError, AttributeError):
        return None
    if "pull_request" in events:
        return True
    if events:
        return "skip"
    return False


def build_report(
    *,
    number,
    url,
    head_sha,
    readiness_kind,
    failing_checks,
    n_fail,
    n_unresolved,
    marker_eval,
    code,
    status,
):
    """Build the --json report.

    Every field here has a named consumer in the babysit skill; nothing is
    emitted speculatively. The shape is split because the whole object is NOT
    safe to compare between runs:

    ``progress_key`` holds only fields that change when real progress happens,
    so a polling loop can compare it byte-for-byte to tell "still stuck" from
    "something moved". Everything unstable lives under ``advisory``, which the
    loop reads when checking its exit conditions but never includes in the
    comparison:

    * a finding a writer rebutted or deferred is re-raised every round, so a key
      including finding counts would never stabilise and a stall would never be
      recognised -- and the count also moves when a bot merely re-words a comment;
    * ``unresolved_threads`` degrades to null on any API error or page cap, so a
      transient blip would read as progress.

    Ambient PR state (mergeable, merge_state, review_decision, check totals) is
    deliberately NOT emitted: the prose above already prints all of it, the loop
    reads it there, and ``pr_findings.py`` owns the exit-20 drill-in. A second
    machine-readable copy with no reader is a surface to keep in sync for nothing.
    """
    return {
        "exit_code": code,
        "pr": number,
        "status": status,
        "url": url,
        # Compare THIS between cycles -- nothing else.
        "progress_key": {
            "checks_failing": n_fail,
            "exit_code": code,
            "failing_checks": sorted(failing_checks),
            "head_sha": head_sha,
            "readiness_kind": readiness_kind,
            # The verdict reason, so a CHANGED blocker cannot read as an
            # unchanged one. exit_code and the failing-check set do not
            # distinguish "blocked by a conflict" from "blocked by a review
            # marker": both are exit 20 and can carry an identical check set, so
            # without this a real change in what blocks the PR would extend a
            # stall streak instead of resetting it. Deterministic by
            # construction -- decide() joins its reasons in a fixed order from
            # sorted inputs.
            "status": status,
        },
        "advisory": {
            "blocking_reviewers": sorted(marker_eval.get("blocking") or []),
            "bot_comments_readable": bool(marker_eval.get("ok")),
            "elided_stamp_reviewers": sorted(marker_eval.get("elided") or []),
            "findings": dict(marker_eval.get("findings") or {}),
            "stale_reviewers": sorted(marker_eval.get("stale") or []),
            "unresolved_threads": n_unresolved,
        },
    }


def decide(
    state,
    mergeable,
    merge_state,
    decision,
    draft,
    readiness_kind,
    n_running,
    n_fail,
    n_checks,
    readiness_context,
    marker_eval=None,
    head_run="skip",
    rollup_notice="",
    disposition_eval=None,
):
    """Resolve PR state to (exit_code, status line). Fail-closed.

    Exit codes: 0 = clean, 10 = wait (nothing to do yet), 20 = act.

    Precedence is the load-bearing part, and it is ordered by "can waiting
    change this answer?" rather than by how the fields arrive:

    1. A non-open PR is terminal, and must be decided BEFORE any wait: GitHub
       reports mergeable=UNKNOWN for merged/closed PRs forever, so waiting on
       it returns 10 on every poll and a loop never stops.
    2. Conditions waiting CANNOT fix outrank "still running". A conflicted PR is
       the case that matters: the host cannot build a merge ref for it, so it
       dispatches no pull_request workflows at all and every check visible
       belongs to the old head. Ranking in-flight checks first reports "running"
       forever while nothing can complete -- a stall only a human notices.
       BEHIND, draft and CHANGES_REQUESTED behave the same way: each survives
       any amount of waiting and needs the author to act. A SETTLED reviewer
       round (``reviewer_round_settled``) joins them: once every pinned lane
       has stamped this head and one blocks, the edit is required, so the
       backend/frontend runs still in flight are spending minutes on a commit
       already condemned -- Phase 3 cancels them instead of waiting them out.
       Only the AI-review lanes gate this; a non-reviewer check still running
       does not hold the decision open, and a red one does not force it. The disposition
       evaluation (``disposition_eval``, built from disposition_violations)
       belongs here too, in BOTH its states: a violation is cleared only by
       the author editing or deleting the offending comment, and an
       UNREADABLE evaluation is not "no violations" -- while either waits
       behind an in-flight round, the reviewer bots rewrite their stamped
       comments in place, so the judged-head span mapping the verdict needs
       disappears with the old stamps. It gates because a record that claims
       several findings, another lane's finding, or no finding at all is
       exactly the blanket ruling the one-lane / one-rationale rule forbids
       -- and the adjudication ledger would consume it as written.
    3. Only then is "still running" a wait, and an uncomputed mergeability too.
    4. Everything left is a check-result verdict -- including the reviewer-side
       conditions: ``marker_eval`` (from evaluate_reviewer_markers; None skips
       the gate) and ``head_run`` (True/False/None from head_run_exists; the
       string "skip" skips the gate). Both are evaluated ONLY here, after the
       running gate: while a round is in flight the reviewer bots may simply
       not have posted yet, and a stale stamp mid-round is expected, not a
       defect. Both fail closed on "could not read".
       Advisory FINDING counts never gate -- whether a non-blocking finding
       should hold the loop open is a judgment call the exit code deliberately
       does not make.
       ``rollup_notice`` -- the non-empty NOTICE string when the rollup read
       degraded (fetch failure, or a concurrent push between the two reads) --
       never changes the exit code either, only which reason an empty rollup
       earns: a degraded read names its environment cause, so the reason (and
       ``progress_key.status``, which carries it) distinguishes an environment
       gap from a genuinely check-less PR.
    """
    if state != "OPEN":
        return 20, "STATUS: BLOCKED - PR state is {} (not OPEN; terminal)".format(state or "?")

    blocked_now = []
    if mergeable == "CONFLICTING" or merge_state in ("DIRTY", "CONFLICTING"):
        blocked_now.append("merge conflict / not mergeable")
    if merge_state == "BEHIND":
        blocked_now.append("branch is BEHIND base - re-sync onto the latest base")
    if draft:
        blocked_now.append("PR is a draft")
    if decision == "CHANGES_REQUESTED":
        blocked_now.append("review decision is CHANGES_REQUESTED")
    if reviewer_round_settled(marker_eval):
        # The reviewer round is DONE even though the rollup is not: every
        # pinned lane stamped this head and at least one blocks. Waiting for
        # the remaining lanes (backend/frontend tests, packaging) cannot
        # change that the diff must be edited, and their verdicts do not
        # survive the edit anyway -- the push re-runs them on the new head.
        # So this belongs with the other "waiting cannot fix this" conditions
        # ABOVE the running gate. Requires a PINNED fleet: discovery mode
        # cannot tell "all lanes reported" from "one lane reported first",
        # and acting there would drop a verdict that was still coming.
        blocked_now.append(
            "reviewer round complete with blocking marker [BLOCK-MERGE] on "
            "current head from: " + ", ".join(sorted(marker_eval["blocking"]))
        )
    if disposition_eval is not None:
        # A disposition violation is a condition waiting cannot fix -- only the
        # AUTHOR editing or deleting the comment clears it -- so it belongs
        # with the other author-action conditions ABOVE the running gate.
        # Deferring it behind an in-flight round loses the evidence: the
        # reviewer bots rewrite their stamped comments in place when the round
        # completes, and the judged-head span mapping the violation was
        # computed from disappears with the old stamps. An UNREADABLE
        # evaluation (records or the finding map could not be fetched) gates
        # here for the same reason: "could not read" is not "no violations",
        # and waiting through the round destroys the very evidence a re-read
        # would need. Sorted by construction (disposition_violations returns a
        # sorted list), so the joined reason -- which travels in
        # ``progress_key.status`` -- is deterministic.
        if not disposition_eval.get("ok"):
            blocked_now.append("disposition records could not be established (fail-closed)")
        for v in disposition_eval.get("violations") or []:
            blocked_now.append("disposition rule: " + v)
    if blocked_now:
        return 20, "STATUS: BLOCKED - " + "; ".join(blocked_now)

    # Once published, the aggregate is authoritative over stale duplicate
    # checks in the rollup. Legacy PRs without it still use the full rollup.
    if readiness_kind == "running" or (readiness_kind is None and n_running > 0):
        return 10, "STATUS: RUNNING (round not complete)"
    if mergeable not in ("MERGEABLE", "CONFLICTING"):
        return 10, "STATUS: RUNNING (mergeability not yet computed: {})".format(
            mergeable or "UNKNOWN"
        )

    reasons = []
    if readiness_kind == "fail":
        reasons.append("{} reported action required".format(readiness_context))
    elif readiness_kind is None and n_fail > 0:
        reasons.append("{} check(s) failed".format(n_fail))
    if n_checks == 0:
        # An empty rollup has two very different causes, and the reason chosen
        # here travels in ``progress_key.status``, which a polling loop
        # compares byte-for-byte across runs. A degraded rollup read (the
        # NOTICE the caller printed) is an environment gap -- token scope, an
        # API error, a concurrent push -- while an empty rollup from a healthy
        # read means the host truly reports no checks for this head. One
        # shared reason would make those indistinguishable, so a loop would
        # re-poll a token problem forever instead of escalating it.
        if rollup_notice == ROLLUP_HEAD_MOVED_NOTICE:
            reasons.append(
                "CI status unreadable - the PR head moved between reads "
                "(concurrent push) - transient environment, re-run for a "
                "consistent snapshot (fail-closed)"
            )
        elif rollup_notice:
            reasons.append(
                "CI status unreadable - the rollup fetch failed (a token "
                "without Checks read access, a 403, or a rate limit) - "
                "environment gap, not a code defect (fail-closed)"
            )
        else:
            reasons.append("no CI checks reported - cannot confirm CI (fail-closed)")
    if marker_eval is not None:
        if not marker_eval.get("ok"):
            reasons.append("reviewer comments could not be read (fail-closed)")
        else:
            if marker_eval.get("blocking"):
                # sorted(): ``blocking`` is a set, and joining a set of strings
                # is only order-stable WITHIN one process. This status string is
                # part of progress_key, which a polling loop compares
                # byte-for-byte across separate runs, so an unsorted join would
                # make two identical states differ whenever more than one
                # reviewer blocks.
                reasons.append(
                    "blocking review marker [BLOCK-MERGE] on current head from: "
                    + ", ".join(sorted(marker_eval["blocking"]))
                )
            if marker_eval.get("stale"):
                reasons.append(
                    "stale reviewer stamp(s) - no [<NAME>-REVIEWED] for current head: "
                    + ", ".join(marker_eval["stale"])
                )
    if head_run is False:
        reasons.append(
            "no pull_request-event workflow run for the current head - the "
            "checks shown may belong to an older head (stale or conflicted PR)"
        )
    elif head_run is None:
        reasons.append(
            "could not confirm a pull_request-event run for the current head (fail-closed)"
        )
    if merge_state and merge_state not in (
        "CLEAN",
        "HAS_HOOKS",
        "UNSTABLE",
        "BLOCKED",
        "DIRTY",
        "CONFLICTING",
        "DRAFT",
        "BEHIND",
    ):
        # BLOCKED = pending required review (expected for a review-ready PR);
        # anything unrecognized is fail-closed.
        reasons.append("unrecognized merge state '{}' (fail-closed)".format(merge_state))

    if reasons:
        return 20, "STATUS: BLOCKED - " + "; ".join(reasons)
    return 0, "STATUS: CLEAN (readiness passed, mergeable, no blocking review decision)"


def _flag_value(argv, name):
    """Value of ``--name VALUE`` or ``--name=VALUE`` in argv, else ""."""
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return ""


def disposition_gate(argv, environ):
    """Evaluate ONLY the disposition rule and print one JSON object; exit 0.

    This is the server-side entry point (issue #6658): pr-readiness.yml calls
    it so a disposition record violating the one-lane / one-rationale-per-
    finding rule fails the repository's required status for EVERY writer, not
    only for a writer running the prepare-pr loop. It exists as a mode of this
    script rather than as a workflow-side reimplementation so the rule keeps a
    single definition -- the same ``disposition_violations`` the local gate
    calls, over the same records the adjudication ledger admits.

    Usage: --disposition-gate --repo OWNER/NAME --pr N --head SHA
    (--marker-bindings / --marker-authors and their env forms apply as usual.)

    Prints ``{"ok", "violations", "comments", "records", "unverified",
    "error"}``. ``ok`` is False when the record set could not be established,
    which the caller must treat as UNKNOWN (pending) rather than as a red: a
    transient API failure red-lighting the required status is the #2753 class
    of bug. Exit status is 0 for both outcomes -- the JSON carries the verdict,
    so a non-zero exit means only that this script itself failed to run, and
    the caller can tell the two apart. Enforcement scope is deliberately
    identical to the ledger's admission scope: an author the collaborators
    permission API does not confirm as a writer is DROPPED here exactly as
    codex-review.yml drops them, so this gate never blocks on a record that
    holds no downgrade power (``unverified`` counts those, for observability).
    """
    repo = _flag_value(argv, "--repo").strip()
    number = _flag_value(argv, "--pr").strip()
    head_sha = _flag_value(argv, "--head").strip()
    result = {
        "ok": False,
        "violations": [],
        "comments": 0,
        "records": 0,
        "unverified": 0,
        "error": "",
    }
    try:
        if not repo or not number or not head_sha:
            result["error"] = "--repo, --pr and --head are all required"
        else:
            bindings = resolve_marker_bindings(argv, environ)
            comments = fetch_disposition_comments(repo, number)
            bot_comments = fetch_bot_comments(repo, number, resolve_marker_authors(argv, environ))
            records = writer_disposition_records(repo, comments)
            if comments is None or bot_comments is None or records is None:
                result["error"] = "disposition or marker comments could not be read"
            else:
                result["comments"] = len(comments)
                result["records"] = len(records)
                result["unverified"] = len(comments) - len(records)
                # One violation per line downstream, so a newline inside one
                # would forge an extra blocker line. Nothing in the strings can
                # carry one today (logins, span ids and target= are all charset-
                # limited), which is exactly why flattening here is free.
                result["violations"] = [
                    " ".join(sanitize(v).split())
                    for v in disposition_violations(records, bot_comments, head_sha, bindings)
                ]
                result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - any failure is "unknown", never red
        result["error"] = "{}: {}".format(type(exc).__name__, exc)
    print(json.dumps(result, sort_keys=True))
    return 0


def main(argv):
    # Before the auth probe and PR detection below: this mode is given its
    # repo/PR/head explicitly and must stay usable from a workflow runner,
    # where `gh auth status` prose is noise and the JSON is the whole output.
    if "--disposition-gate" in argv[1:]:
        return disposition_gate(argv, os.environ)

    if run(["gh", "auth", "status"])[0] != 0:
        err("ERROR: gh not found or not authenticated. Run: gh auth login")
        return 2

    readiness_context = resolve_readiness_context(argv, os.environ)
    reviewers_filter = resolve_reviewers(argv, os.environ)
    pos = positional_args(argv[1:])
    pr = pos[0] if pos else ""
    if not pr:
        pr = run(["gh", "pr", "view", "--json", "number", "-q", ".number"])[1]
    if not pr:
        err("ERROR: no PR number given and none found for the current branch.")
        return 2

    fields = (
        "number,title,state,isDraft,mergeable,mergeStateStatus,"
        "reviewDecision,url,headRefName,headRefOid,"
        "body,closingIssuesReferences"
    )
    rc, out, _ = run(["gh", "pr", "view", pr, "--json", fields])
    if rc != 0 or not out:
        err("ERROR: could not read PR #" + str(pr))
        return 2
    d = json.loads(out)

    state = (d.get("state") or "").upper()
    draft = bool(d.get("isDraft"))
    mergeable = (d.get("mergeable") or "").upper()
    merge_state = (d.get("mergeStateStatus") or "").upper()
    decision = (d.get("reviewDecision") or "NONE").upper()
    head_sha = (d.get("headRefOid") or "").strip()
    rollup_entries, rollup_notice = fetch_check_rollup(pr, head_sha)
    rollup = collapse_superseded(rollup_entries)

    print("=" * 54)
    print("PR #{}  [{}{}]".format(d.get("number"), state, " draft" if draft else ""))
    print("title (untrusted): " + sanitize(d.get("title") or ""))
    print("branch: " + sanitize(d.get("headRefName") or ""))
    print("url:    " + (d.get("url") or ""))
    print(
        "mergeable={}  mergeState={}  reviewDecision={}".format(
            mergeable or "?", merge_state or "?", decision
        )
    )

    print("-- CI checks " + "-" * 40)
    if rollup_notice:
        print("  NOTICE: " + rollup_notice)
    n_running = n_fail = 0
    failing_checks = []
    readiness_kind = None
    for e in rollup:
        kind = classify_check(e)
        if kind == "running":
            n_running += 1
        elif kind == "fail":
            n_fail += 1
        name = sanitize(e.get("name") or e.get("context") or "check")
        if kind == "fail":
            failing_checks.append(failing_check_identity(e))
        # Only the legacy StatusContext we publish is authoritative. A CheckRun
        # can share the display name but is a different, independently writable
        # namespace and must remain part of the ordinary rollup.
        if e.get("context") == readiness_context:
            readiness_kind = kind
        shown = (e.get("status") or "-") + "/" + (e.get("conclusion") or e.get("state") or "-")
        print("  - {}: {}  [{}]".format(name, shown, kind))
    print("  rollup: total={} running={} failing={}".format(len(rollup), n_running, n_fail))
    print("  aggregate readiness: {}".format(readiness_kind or "not published"))
    _closes = d.get("closingIssuesReferences") or []
    print(
        "  closes on merge: {}".format(
            ", ".join("#{}".format(i.get("number")) for i in _closes) if _closes else "nothing"
        )
    )
    # Advisory, never a gate. The measured failure was that nobody was ever
    # ASKED for a trailer, not that authors refuse to write one: across 600
    # merged PRs the host's auto-close worked every time it had a keyword to
    # act on. So report the gap where the author will see it and let them
    # decide -- blocking a green PR on bookkeeping costs more than it saves,
    # and an issue-less PR is legitimate.
    repo = detect_repo(d.get("url") or "")
    _closing = closing_link_reason(d.get("body"), _closes, repo)
    if _closing:
        print("  NOTICE: " + _closing)

    n_unresolved = unresolved_thread_count(d.get("number"))
    print("-- Review threads " + "-" * 35)
    print(
        "  unresolved threads (advisory): " + ("?" if n_unresolved is None else str(n_unresolved))
    )

    # Reviewer-side conditions (issue #2550): the stamp and the comment body
    # are the signal -- never the review workflow's run conclusion, which is
    # unreliable in both directions on this repo.
    marker_authors = resolve_marker_authors(argv, os.environ)
    marker_bindings = resolve_marker_bindings(argv, os.environ)
    bot_comments = fetch_bot_comments(repo, d.get("number"), marker_authors)
    marker_eval = evaluate_reviewer_markers(
        bot_comments,
        head_sha,
        marker_bindings,
        only=reviewers_filter,
    )
    print("-- Reviewer markers (head {}) ".format(sanitize(head_sha[:12]) or "?") + "-" * 20)
    if not marker_eval["ok"]:
        print("  ERROR: bot comments could not be read (fail-closed)")
    elif not marker_eval["findings"] and not marker_eval["stale"]:
        if reviewers_filter:
            print(
                "  (no [<NAME>-REVIEWED] stamps found for filter: "
                + ", ".join(sorted(reviewers_filter))
                + ")"
            )
        else:
            print("  (no [<NAME>-REVIEWED] stamps found in bot comments)")
    else:
        for name in sorted(marker_eval["findings"]):
            print(
                "  - {}: fresh{}{}{}".format(
                    sanitize(name),
                    "  [BLOCK-MERGE]" if name in marker_eval["blocking"] else "",
                    (
                        "  ({} advisory FINDING line(s))".format(marker_eval["findings"][name])
                        if marker_eval["findings"][name]
                        else ""
                    ),
                    (
                        "  [stamp elided the head's middle - emitter transcription "
                        "artifact, verified against this head]"
                        if name in (marker_eval.get("elided") or ())
                        else ""
                    ),
                )
            )
        for name in marker_eval["stale"]:
            print("  - {}: STALE (stamp names an older head)".format(sanitize(name)))

    # Disposition-rule gate (issue #4187): a repository writer's disposition
    # comment must claim exactly one span= finding identity from its own
    # target= lane. Each record is validated against the findings stamped for
    # the head its head= says it judged (in the ordinary fix-then-push round
    # that is the PRIOR head) and the current one, so the rule "one comment
    # covers one lane, one rationale covers one finding" is mechanical rather
    # than prose.
    disposition_comments = fetch_disposition_comments(repo, d.get("number"))
    disposition_records = writer_disposition_records(repo, disposition_comments)
    disposition_violation_list: list = []
    # BOTH inputs must be readable: the records (the rulings) and the trusted
    # reviewer comments (the finding map the rulings are validated against).
    # An unreadable finding map is not "no findings" -- computing with an
    # empty map would silently drop every evidence-bearing class while the
    # round runs, and the judged-head evidence is rewritten in place when it
    # completes.
    disposition_ok = disposition_records is not None and bot_comments is not None
    if disposition_ok:
        disposition_violation_list = disposition_violations(
            disposition_records, bot_comments, head_sha, marker_bindings
        )
    disposition_eval = {"ok": disposition_ok, "violations": disposition_violation_list}
    print("-- Disposition records (one lane, one rationale per finding) " + "-" * 6)
    if not disposition_ok:
        print("  ERROR: disposition records could not be established (fail-closed)")
    elif not disposition_comments:
        print("  (no disposition comments)")
    else:
        # Both counts, so a degraded state is visible instead of reading like
        # an empty PR: zero verified records above a non-zero comment count
        # means the check is INERT for those comments -- non-writer authors,
        # or a token that cannot read collaborator permissions (the endpoint
        # needs push access).
        print(
            "  disposition-marked comment(s): {}  from verified writers: {}".format(
                len(disposition_comments), len(disposition_records)
            )
        )
        if not disposition_records:
            print(
                "  NOTICE: none verified as a repository writer's - the "
                "disposition check is inert for these comments"
            )
        unknown_targets = sorted(
            {
                r["target"]
                for r in disposition_records
                if not r["malformed"]
                and r["target"] not in {n.lower() for n in marker_bindings.values()}
            }
        )
        if unknown_targets:
            # Advisory only: an unbound lane (e.g. first-principles) has no
            # extractable finding identity, but a typo'd target= looks the
            # same and silently escapes the claim requirement -- say so.
            print(
                "  NOTICE: target lane(s) outside the marker bindings "
                "(claim checks do not apply): " + ", ".join(sanitize(t) for t in unknown_targets)
            )
        for v in disposition_violation_list:
            print("  VIOLATION: " + sanitize(v))
        if not disposition_violation_list:
            print("  (no disposition-rule violations)")

    # Assert a pull_request-event run exists for the current head, but only on
    # a PR that demonstrably uses Actions (a rollup entry with a workflowName);
    # a repo reporting only legacy statuses must not be blocked forever by an
    # assertion about workflows it does not run.
    head_run = "skip"
    if (
        head_sha
        and any(e.get("workflowName") for e in rollup)
        and head_run_check_enabled(argv, os.environ)
    ):
        head_run = head_run_exists(repo, head_sha)
        if head_run is True:
            run_shown = "yes"
        elif head_run is False:
            run_shown = "NO (stale or conflicted?)"
        elif head_run == "skip":
            run_shown = "n/a (this head's CI is driven by other events)"
        else:
            run_shown = "? (could not confirm)"
        print("  pull_request run for current head: " + run_shown)
    print("=" * 54)

    code, status = decide(
        state=state,
        mergeable=mergeable,
        merge_state=merge_state,
        decision=decision,
        draft=draft,
        readiness_kind=readiness_kind,
        n_running=n_running,
        n_fail=n_fail,
        n_checks=len(rollup),
        readiness_context=readiness_context,
        marker_eval=marker_eval,
        head_run=head_run,
        rollup_notice=rollup_notice,
        disposition_eval=disposition_eval,
    )
    print(status)
    if "--json" in argv[1:]:
        # Last line of stdout, one compact line, keys sorted: a polling loop
        # compares consecutive runs byte-for-byte, so the serialization has to
        # be stable for an unchanged PR.
        print(
            json.dumps(
                build_report(
                    number=d.get("number"),
                    url=d.get("url") or "",
                    head_sha=head_sha,
                    readiness_kind=readiness_kind,
                    failing_checks=failing_checks,
                    n_fail=n_fail,
                    n_unresolved=n_unresolved,
                    marker_eval=marker_eval,
                    code=code,
                    status=status,
                ),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
