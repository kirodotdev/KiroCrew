#!/usr/bin/env python3
"""Claim preflight — the deterministic claim predicate for the pipeline conductor.

One invocation answers every cheap question about one candidate work item and
returns ONE verdict, so a worker dispatch is never spent discovering that the
work does not exist.

WHY FIVE QUESTIONS AND NOT ONE. The predicate this replaces was a single prose
line, ``gh pr list --search``, and it was blind in three directions at once.
Each blind spot cost a whole dispatch to discover:

  * an item already fixed by a MERGED PR was claimed and dispatched days later
    — an ``--state open`` query structurally cannot see a merged PR — with the
    reporter's own "happy to have it closed" sitting on the thread;
  * four dispatches went to items that each had an OPEN PR carrying
    ``Fixes #N``, because the predicate read one field
    (``closedByPullRequestsReferences``) that came back empty for all of them.
    It still does: measured on this repo, two items closed by merged PRs both
    answer ``[]``. That is why this script never asks that question at all — it
    reads the item's TIMELINE, which sees both the fork PRs and the merged ones;
  * three items said "I am claiming this issue" / "Ownership claimed by @X" in
    PROSE, in the body, which no label or field query sees.

The lesson those three share, and the design rule of this script: **one
question with an empty answer is not permission.** So it asks all five, and an
unanswerable question yields UNKNOWN — never CLAIM.

Usage:
    python3 claim_preflight.py --repo <owner/repo> --item <N>
                              [--default-branch main]
                              [--repo-dir <path to a clone of the base>]
                              [--json]

    --repo            ``owner/name`` of the forge repository holding the item
    --item            the issue number
    --default-branch  the git rev a worker would branch from, as it is spelled
                      in ``--repo-dir`` (``main``, ``origin/main``, …)
    --repo-dir        a clone of the base. Needed only for the two questions
                      git can answer: whether a merged PR actually LANDED on
                      that branch, and whether a symbol the item names exists
                      there. Omitting it is not an error — but if the item has
                      a merged PR or names a symbol, the answer becomes
                      UNKNOWN rather than a guess. The clone is read as-is and
                      never fetched: keeping it current is the caller's job,
                      and a merge commit missing from a stale clone reports
                      UNKNOWN, never "did not land".
    --json            print exactly one JSON object instead of the human line

Exit codes (the conductor branches on these):

    0   CLAIM    — dispatch it
   10   SKIP     — covered or not workable; do not dispatch
   11   CLOSE    — triage debt: already fixed, or the reporter asked to close
    2            — malformed arguments or config
    3   UNKNOWN  — a check could not be answered (forge unreachable, rate
                   limited, no clone for a question only git can answer)

The five checks, all of them, every call:

  1. ``open_prs``      open PRs referencing the item, FORK PRs included, with
                       ``is_cross_repository`` and author per hit, plus
                       ``untrusted_fork`` when a cross-repository PR's author
                       has no standing. That last one changes no verdict: it
                       makes the suppression LOUD, because opening a fork PR
                       needs no permission and a silent SKIP anybody can cause
                       is how an item leaves the queue with nobody told.
  2. ``merged_prs``    MERGED PRs referencing the item, each annotated
                       ``landed`` by ``git merge-base --is-ancestor`` and
                       ``closes_item`` by a closing keyword aimed at this item.
                       BOTH are load-bearing: a PR merged somewhere other than
                       the branch a worker would start from is not coverage, and
                       a PR that merely MENTIONS the item is not closure.
  3. ``prose_claim``   the body and the NEWEST human comment, scanned for
                       self-claim phrases and for closure requests — over what
                       the author SAYS, with code fences, backtick spans,
                       blockquotes and quoted spans removed first, because a
                       phrase inside them is being cited. BOTH phrase sets need
                       standing (the reporter, or a repository insider), for
                       opposite reasons: CLOSE acts on live work, and a SKIP any
                       commenter can cast is a denial-of-work channel.
  4. ``symbol_on_base``every symbol the item names, by ``git grep`` on the
                       default branch. Absent means the target code may live
                       only on an unmerged branch — but that reading holds only
                       for a BUG-class item, so absence vetoes only when the
                       item's own metadata corroborates that class. Otherwise it
                       downgrades to ``CLAIM risk=high``: a feature request
                       names the symbol it PROPOSES to add, and parking it would
                       be a permanent false veto on a whole item class.
  5. ``recency``       age and ``authorAssociation``. A freshly opened item
                       from an active contributor is a high self-claim risk —
                       surfaced as ``risk=high``, never a veto on its own. The
                       consumer is the skill: ``risk=high`` means the item is
                       not batched, it gets a live re-check immediately before
                       the atomic claim.

Verdict precedence, first match wins (see :func:`verdict`, a pure function of
the checks dict so every branch is unit-testable with no forge access):

  1. ``merged_prs`` LANDED and CLOSES it       → CLOSE ``already-fixed``
  2. any ``open_prs`` entry                  → SKIP  ``open-pr``, marked
                                               ``untrusted-fork`` at
                                               ``risk=high`` when the PR is an
                                               unvouched fork
  3. ``prose_claim.closure_requested``       → CLOSE ``reporter-asked-close``
  4. ``prose_claim.claimed_by_other``        → SKIP  ``prose-claim``
  5. ``symbol_on_base.missing`` AND bug-class→ SKIP  ``symbol-absent``
  6. any check errored                       → UNKNOWN
  7. otherwise                               → CLAIM, annotated with ``risk``,
                                               which an UNCORROBORATED absent
                                               symbol or an UNAUTHORIZED claim
                                               forces to ``high``

Rules 2 and 4 both suppress work on evidence anybody can manufacture, and both
answer it the same way rather than by refusing to look: the finding stands, and
the doubt is published alongside it. A verdict that acts while doubting has to
say so, or the doubt is only in this docstring.

Note that 6 sits BELOW the positive findings on purpose: a definite answer to
one question outranks a partial view of another, and no error path can reach
CLAIM.

Deliberately boring properties, do not weaken:

  * Forge access goes through ``gh``; no hand-rolled HTTP and no token is ever
    read by this script. ``run_gh`` refuses a mutating argv outright, so the
    no-write property is enforced rather than merely intended: this script
    never labels, assigns, comments, or closes.
  * At most one forge call per question. The timeline is fetched once and
    answers checks 1 and 2 together; each referencing PR is then detailed once,
    because fork-ness and the merge commit exist only on the pull object.
  * No user-authored PROSE reaches stdout. Failures are reported as SLUGS
    rather than forge stderr, a prose match reports the PATTERN that fired
    rather than the sentence, and a bug-class match reports this module's own
    TERM rather than the label's text — this output lands in an agent's context.
    What DOES appear is identifiers: logins, PR numbers, commit prefixes,
    comment ids and symbol names extracted from the item. Those are the evidence
    a conductor needs to check the verdict, and a login is chosen by its owner
    rather than written for this item. The claim is deliberately narrower than
    "nothing user-authored": the wider version was in this docstring first, and
    an invariant that overclaims is how a leak of user-written LABEL text once
    shipped past it.
  * A closed-unmerged PR is neither coverage nor a claim. It is abandoned work
    and it frees the item, so it appears in neither list.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

#: What a closure request has to be ABOUT. Measured: `\bplease\s+close\b` alone
#: fired on "Please close the file after reading it" and "Remember to please
#: close the connection in the finally block" -- ordinary sentences in a bug
#: report about this kind of software -- and returned CLOSE, exit 11, on a live
#: item. The verb is not the signal; its OBJECT is. So the close-family patterns
#: require an issue-shaped object or a clause end, and anything unrecognised is
#: NOT a closure request: that direction costs one dispatch, the other closes
#: work in flight.
#:
#: Bare ``it`` and bare ``that`` are deliberately NOT objects, which cost a
#: round to learn: the first version of this list accepted them, and "The
#: connection leaks. Please close it." closed the item. A bare pronoun resolves
#: to whatever was last mentioned, and in a bug report that is usually a socket,
#: a file or a handle. ``this`` is kept because it points at the thread's topic
#: rather than at the previous noun, and ``that issue`` still works with the noun
#: present. The noun alternatives come FIRST so "this issue" is not consumed by
#: bare ``this`` and then failed on the clause end.
_ISSUE_OBJECT = (
    r"(?:this|that|the)\s+(?:issue|ticket|item|bug|report|one)"
    r"|(?:the\s+)?(?:issue|ticket|item|bug|report)"
    r"|this"
    r"|#\d+"
)
_CLAUSE_END = r"(?:\s*(?:[.,;:!?)\]\"]|$)|\s+(?:as|since|because|please))"
_CLOSE_OBJECT = rf"(?:\s+(?:{_ISSUE_OBJECT}))?{_CLAUSE_END}"

#: What may FOLLOW "this is resolved/fixed" for it to be a statement about the
#: item's state. An allowlist, not a list of exclusions, because two exclusions
#: were already not enough here. Measured misses it fixes: "This is fixed-width
#: layout" closed live items, because ``\b`` matches happily before a hyphen and
#: the word is an ADJECTIVE there, not a verdict. ``by`` is absent on purpose --
#: "this is resolved by upgrading it in your own fork" hands over a workaround
#: and leaves the item open -- while ``in`` is present, so "fixed in the 0.7
#: release" still reads as closure.
_STATE_END = r"(?:\s*(?:[.,;:!?)\]\"]|$)|\s+(?:in|on|at|since|as|now|already|and|so|--))"

#: Self-claim prose, from the three items that were dispatched on top of
#: someone else's declared ownership. Patterns, not the matched text, are what
#: gets reported.
#: Negation and hedging that INVERT a phrase this scanner would otherwise read
#: as a verdict. Measured: "I don't think this is resolved", "I am not sure this
#: is fixed" and "nobody said this is resolved" all closed live items, because
#: the phrase patterns match a SUBSTRING while English puts the negation earlier
#: in the sentence. Python's ``re`` has no variable-width lookbehind, so this is
#: applied as a window check over the text PRECEDING each match rather than as
#: part of the pattern.
#:
#: This is the OPPOSITE choice from :func:`closing_reference_re`, deliberately.
#: There, negation-blindness matches GitHub's own parser, which closes an item
#: for "does not close #N" anyway, so agreeing with the forge keeps the two
#: readings identical. Here nothing else is doing the reading and the verdict
#: writes to somebody's live work.
_NEGATOR_RE = re.compile(
    r"\b(?:not|never|nobody|none|cannot|doubt|unsure|unclear|unless|whether)\b|n't\b",
    re.IGNORECASE,
)

#: How far back a negation reaches. Wide enough for "I don't think this is
#: resolved", short enough that an unrelated "no" two sentences earlier does not
#: veto a real request. A false veto costs one dispatch, which is the side to
#: err on.
_NEGATION_WINDOW = 60

SELF_CLAIM_RES: tuple[str, ...] = (
    r"\bI(?:'m| am)\s+claiming\b",
    r"\bclaiming\s+(?:this|it)\b",
    r"\b(?:I|we)\s+(?:have\s+)?claimed\s+(?:this|it)\b",
    r"\bownership\s+claimed\s+by\b",
    r"\bI(?:'m| am)\s+working\s+on\s+(?:this|it)\b",
    r"\bworking\s+on\s+(?:this|it)\s+(?:now|already)\b",
    r"\bI(?:'ll| will)\s+(?:take|pick\s+up|handle|fix)\s+(?:this|it)\b",
    r"\b(?:taking|picking)\s+(?:this|it)\s+up\b",
    r"\bassigned\s+(?:this\s+)?to\s+myself\b",
)

#: A claim being GIVEN UP. Recovering the newest standing claim must not step
#: over one of these: an owner who says "dropping this" has released the item,
#: and treating their earlier claim as live parks it on nobody.
WITHDRAWAL_RES: tuple[str, ...] = (
    r"\b(?:dropping|unclaiming|abandoning)\s+(?:this|it)\b",
    r"\bno\s+longer\s+working\s+on\s+(?:this|it)\b",
    r"\bnot\s+working\s+on\s+(?:this|it)\b",
    r"\bI(?:'m| am)\s+off\s+(?:this|it)\b",
    r"\bunassigning\s+myself\b",
)

#: Closure requests, from the item whose reporter had already said it was done.
#: Every pattern that can produce CLOSE carries a continuation guard, because
#: CLOSE is the one verdict that writes to somebody else's live work and this
#: list has now produced four separate false positives without one.
CLOSURE_RES: tuple[str, ...] = (
    rf"\bthis\s+(?:is|was)\s+(?:already\s+)?(?:resolved|fixed)\b{_STATE_END}",
    rf"\bhappy\s+to\s+have\s+(?:{_ISSUE_OBJECT})\s+closed\b",
    rf"\bplease\s+close\b{_CLOSE_OBJECT}",
    rf"\b(?:can|could|should)\s+(?:we|you|this)\s+(?:be\s+)?close[d]?\b{_CLOSE_OBJECT}",
    rf"\bthis\s+can\s+be\s+closed\b{_STATE_END}",
    # "no longer an issue" and "no longer reproducible" are about the ITEM by
    # construction. "needed" and "relevant" are not: "the workaround is no
    # longer needed" describes a workaround and closed live items, so those two
    # require the item as their subject.
    r"\bno\s+longer\s+(?:an\s+issue|reproducible)\b",
    r"\bthis\s+(?:is\s+)?no\s+longer\s+(?:needed|relevant)\b",
)

#: Patterns above that name the item in their own words rather than through
#: ``_ISSUE_OBJECT`` or a guard. The ratchet test uses this: every closure
#: pattern must either carry a guard or be listed here WITH a reason, so the next
#: phrase added to the list cannot quietly ship without one. Six separate
#: false-CLOSE defects reached review because a pattern was added without a
#: guard and nothing checked.
ITEM_SCOPED_CLOSURE_RES: frozenset[str] = frozenset(
    {
        # "an issue" and "reproducible" can only describe the item itself.
        r"\bno\s+longer\s+(?:an\s+issue|reproducible)\b",
        # Subject is a literal "this ... no longer", so the object is inherent.
        r"\bthis\s+(?:is\s+)?no\s+longer\s+(?:needed|relevant)\b",
    }
)

#: An association that makes a fresh item a plausible self-claim in flight.
ACTIVE_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"})

#: Standing to ask for closure on somebody else's item. Narrower than
#: ACTIVE_ASSOCIATIONS on purpose: CONTRIBUTOR means "has had a PR merged here
#: once", which is not authority to close another person's report, while CLOSE
#: is the one verdict that acts on live work.
INSIDER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

#: GitHub's closing keywords. A merged PR is coverage only if it CLAIMS to close
#: the item; a bare cross-reference ("see #123", "related to #123") is a mention
#: and decides nothing. Deliberately negation-blind, matching GitHub's own
#: parser: "does not close #123" links and closes #123 there too, so treating it
#: as a claim keeps this script's reading and the forge's reading identical.
_CLOSING_WORDS = "close[sd]?|fix(?:e[sd])?|resolve[sd]?"


def closing_reference_re(repo: str, item: int) -> re.Pattern[str]:
    """A pattern matching a closing keyword aimed at THIS item.

    Covers the three spellings GitHub honours: ``#N``, ``owner/repo#N``, and the
    full issue URL. ``\\b`` after the number stops ``#12`` matching ``#123``.
    """
    owner_repo = re.escape(repo)
    target = (
        rf"(?:#{item}\b"
        rf"|{owner_repo}#{item}\b"
        rf"|https?://github\.com/{owner_repo}/issues/{item}\b)"
    )
    return re.compile(rf"\b(?:{_CLOSING_WORDS})\s*:?\s+{target}", re.IGNORECASE)


RECENT_DAYS = 14
MAX_SYMBOLS = 8
MAX_PR_DETAILS = 20

#: Comments per page. The maximum the endpoint allows, so the common item costs
#: exactly one call and only a genuinely chatty thread pays for a second --
#: needed because the endpoint has no usable sort and the NEWEST comment is the
#: one this check wants (see :func:`last_human_comment`). ``gh --paginate``
#: merges JSON array pages into one document, measured across four pages on
#: gh 2.96.0, so a paginated read stays a single parse.
COMMENT_PAGE = 100

CHECK_NAMES = (
    "open_prs",
    "merged_prs",
    "prose_claim",
    "symbol_on_base",
    "recency",
)

EXIT_CODES = {"CLAIM": 0, "SKIP": 10, "CLOSE": 11, "UNKNOWN": 3}

#: ``gh`` shapes this script is allowed to run. Everything else — including
#: every write verb — is refused before the subprocess starts.
_READ_SHAPES = (
    ("api",),
    ("issue", "view"),
    ("issue", "list"),
    ("pr", "view"),
    ("pr", "list"),
)
_WRITE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
#: ``gh api -f/-F/--field/--raw-field`` implies POST, so a "GET" argv carrying
#: one of these is a write.
_FIELD_FLAGS = {"-f", "-F", "--field", "--raw-field", "--input"}


def run(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """(rc, stdout, stderr) with a missing binary as rc 127, never a traceback."""
    try:
        done = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd
        )
    except OSError as exc:
        return 127, "", f"{args[0]}: {exc}"
    return done.returncode, (done.stdout or "").strip(), (done.stderr or "").strip()


def is_read_only(args: list[str]) -> bool:
    """Whether ``args`` is one of the read shapes this script may run.

    The no-write rule is a property of the script, not a promise in its
    docstring: an argv that could mutate the forge is refused here, before any
    subprocess exists. That also means a future edit cannot add a write without
    also editing this allowlist, where the intent is obvious in review.
    """
    if len(args) < 2 or args[0] != "gh":
        return False
    if not any(tuple(args[1 : 1 + len(shape)]) == shape for shape in _READ_SHAPES):
        return False
    if args[1] != "api":
        return True
    for index, token in enumerate(args):
        if token in _FIELD_FLAGS:
            return False
        if token in ("-X", "--method"):
            following = args[index + 1] if index + 1 < len(args) else ""
            if following.upper() in _WRITE_METHODS:
                return False
        if token.startswith("--method=") and token.split("=", 1)[1].upper() in _WRITE_METHODS:
            return False
    return True


def run_gh(args: list[str]) -> tuple[int, str, str]:
    """``run`` for ``gh``, refusing anything that is not a read."""
    if not is_read_only(args):
        return 126, "", "refused: claim_preflight performs no writes"
    return run(args)


def error_slug(rc: int, err: str) -> str:
    """A slug for a failed forge call. Never the stderr text.

    The caller prints this into an agent's context, and forge stderr can carry
    a URL with a token in it. A slug plus the exit code is enough to act on.
    """
    low = err.lower()
    if rc == 126:
        return "refused-write"
    if rc == 127:
        return "gh-missing"
    if "rate limit" in low or "429" in low:
        return "rate-limited"
    if "401" in low or "gh auth login" in low or "authentication" in low:
        return "not-authenticated"
    if "404" in low or "not found" in low:
        return "not-found"
    for token in ("could not resolve", "dial tcp", "timeout", "timed out", "connection refused"):
        if token in low:
            return "forge-unreachable"
    return f"gh-error-rc{rc}"


def parse_pages(out: str) -> Any:
    """Parse ``gh`` output that is ONE JSON document or several concatenated.

    ``gh api --paginate`` MERGES JSON array pages into a single document:
    measured on gh 2.96.0, the timeline of a real item at ``per_page=8`` came
    back as one parseable array of 33 events across 4 pages, and the comments
    endpoint at ``per_page=5`` as one array of 12 across 3. So the
    single-document path is the one that runs, and no ``--slurp`` is needed --
    which is just as well, since gh refuses ``--slurp`` together with ``--jq``.

    The concatenated shape is tolerated anyway, and deliberately: this script
    pins no gh version, the failure mode if a version does split pages is that a
    covered item silently reports UNKNOWN, and being right about today's gh is a
    worse guarantee than not caring which gh it is.
    """
    try:
        return json.loads(out)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    merged: list[Any] = []
    index, length = 0, len(out)
    while index < length:
        while index < length and out[index].isspace():
            index += 1
        if index >= length:
            break
        # A ValueError here is a genuinely unparseable payload; it propagates to
        # gh_json, which reports it as a failed answer rather than as no data.
        value, index = decoder.raw_decode(out, index)
        merged.extend(value) if isinstance(value, list) else merged.append(value)
    return merged


def gh_json(args: list[str]) -> tuple[Any, str | None]:
    """(parsed, None) or (None, slug). Unparseable output is a failed answer."""
    rc, out, err = run_gh(args)
    if rc != 0:
        return None, error_slug(rc, err)
    try:
        return parse_pages(out or "null"), None
    except ValueError:
        return None, "unparseable-json"


def git(repo_dir: str, args: list[str]) -> tuple[int, str, str]:
    return run(["git", "-C", repo_dir, *args])


# --------------------------------------------------------------------------- #
# checks 1 + 2 — referencing PRs, and whether a merged one actually landed
# --------------------------------------------------------------------------- #


_API_REPO_RE = re.compile(r"/repos/([^/]+/[^/]+)/(?:issues|pulls)/\d+")


def repo_from_api_url(url: Any) -> str | None:
    """``owner/repo`` out of a forge API URL, or None.

    The timeline's nested ``repository`` object is not guaranteed to be present,
    and the fallback that treated its absence as "same repository" turned a
    reference from ANOTHER repo into a fetch of this repo's PR with the same
    NUMBER -- a different pull request entirely, which could SKIP a live item.
    Returning None here is the honest answer and the caller skips the reference.
    """
    if not isinstance(url, str):
        return None
    found = _API_REPO_RE.search(url)
    return found.group(1) if found else None


def referencing_prs(repo: str, item: int) -> tuple[list[dict], list[dict], str | None]:
    """(open, merged, error slug) for PRs that reference ``item``.

    ONE timeline call finds every cross-reference — fork PRs included, which is
    the half the old ``--state open`` search could not see — and one detail call
    per referenced PR supplies the two fields the timeline omits: the merge
    commit, and the head repository that makes a PR a fork PR.

    A reference to a PR in ANOTHER repository is skipped: it cannot land code
    on this repo's default branch, so it is not coverage of this item.
    """
    data, error = gh_json(["gh", "api", f"repos/{repo}/issues/{item}/timeline", "--paginate"])
    if error:
        return [], [], error
    numbers: list[int] = []
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict) or entry.get("event") != "cross-referenced":
            continue
        source = entry.get("source")
        issue = source.get("issue") if isinstance(source, dict) else None
        if not isinstance(issue, dict) or not isinstance(issue.get("pull_request"), dict):
            continue
        where = issue.get("repository")
        full_name = where.get("full_name") if isinstance(where, dict) else None
        if full_name is None:
            # The nested repository object is not guaranteed. Falling back to
            # "assume local" fetched repos/<this repo>/pulls/<N> and could pull
            # an UNRELATED local PR that merely shares the number, which then
            # SKIPped a live item. The URL carries the owner and repo, so read
            # it from there and skip the reference when neither source can
            # establish it: an unidentifiable reference is not coverage.
            full_name = repo_from_api_url(issue.get("url"))
        if full_name != repo:
            continue
        number = issue.get("number")
        if isinstance(number, int) and number not in numbers:
            numbers.append(number)
    if len(numbers) > MAX_PR_DETAILS:
        # A scan that would drop references cannot claim completeness, and an
        # incomplete coverage answer must not read as "no coverage".
        return [], [], "too-many-references"
    open_prs: list[dict] = []
    merged_prs: list[dict] = []
    closing_re = closing_reference_re(repo, item)
    for number in numbers:
        detail, error = gh_json(["gh", "api", f"repos/{repo}/pulls/{number}"])
        if error or not isinstance(detail, dict):
            # An OPEN PR already confirmed is a definite answer, and the module's
            # precedence puts a definite answer above a partial view: discarding
            # it here turned a SKIP into UNKNOWN over a later PR that could not
            # have changed it. The accumulated hits travel with the error, and
            # the caller keeps the finding while marking the merged side
            # unanswered. Rule 1 outranks rule 2, so a CLOSE can still be missed
            # this way -- that costs an item left open, never a false close.
            return open_prs, merged_prs, (error or "unparseable-json")
        head = detail.get("head") or {}
        base = detail.get("base") or {}
        head_repo = (head.get("repo") or {}).get("full_name") if isinstance(head, dict) else None
        base_repo = (base.get("repo") or {}).get("full_name") if isinstance(base, dict) else None
        user = detail.get("user") or {}
        hit = {
            "number": number,
            "author": user.get("login") if isinstance(user, dict) else None,
            # A deleted head repo reads as cross-repository, the safe side.
            "is_cross_repository": head_repo != base_repo,
            # Read by annotate_untrusted_forks, never printed: an association is
            # a forge enum, but the trust decision belongs in one place.
            "author_association": detail.get("author_association"),
        }
        if detail.get("merged"):
            hit["merge_commit_sha"] = detail.get("merge_commit_sha")
            # A merged PR is coverage only if it CLAIMS to close the item. A
            # bare cross-reference is a mention, and a mention decides nothing:
            # reading it as coverage would CLOSE live work, and reading it as a
            # claim would starve an item whose fix was only partial. So it is
            # recorded and left to fall through to the remaining checks.
            claimed = f"{detail.get('title') or ''}\n{detail.get('body') or ''}"
            hit["closes_item"] = bool(closing_re.search(claimed))
            merged_prs.append(hit)
        elif detail.get("state") == "open":
            open_prs.append(hit)
        # A closed-unmerged PR is abandoned work: neither coverage nor a claim.
    return open_prs, merged_prs, None


def annotate_untrusted_forks(open_prs: list[dict], reporter: str | None) -> None:
    """Set ``untrusted_fork`` on each open hit. Annotation only, by design.

    Opening a fork PR needs no permission, so a cross-repository PR that merely
    MENTIONS an item is a suppression channel available to anybody: the item
    SKIPs and leaves the queue. The tempting fix -- stop trusting fork PRs -- is
    the wrong trade. In a public repository most genuine coverage arrives as a
    fork PR, and dropping those reinstates the duplicate-dispatch class this
    whole script was built from, repeatedly, to close a channel that costs an
    outsider one throwaway PR.

    So the verdict does not move: an open PR still SKIPs, fork PRs included, as
    the skill's check list requires. What changes is that the suppression stops
    being SILENT -- it carries ``risk=high`` and an ``untrusted-fork`` marker, so
    the conductor reviews it as a triage signal instead of watching the item
    vanish. The objection worth answering was never the detection; it was a
    response that was both unconditional and unreported.

    Standing is an insider association or the item's own reporter, since a
    reporter fixing their own bug from a fork is the ordinary case, not an
    attack. An unknown reporter annotates MORE items, never fewer.
    """
    for hit in open_prs:
        if not hit.get("is_cross_repository"):
            hit["untrusted_fork"] = False
            continue
        association = str(hit.get("author_association") or "")
        author = hit.get("author")
        trusted = association in INSIDER_ASSOCIATIONS or (
            reporter is not None and author == reporter
        )
        hit["untrusted_fork"] = not trusted


def annotate_landed(merged: list[dict], repo_dir: str | None, default_branch: str) -> str | None:
    """Set ``landed`` on each merged hit that CLAIMS closure; error slug or None.

    ``landed`` is the difference between coverage and a merge that went
    somewhere else. Only three answers are possible and the third is not
    ``False``: an ancestry question git cannot answer (a merge commit absent
    from a stale clone) must degrade to UNKNOWN, because reading it as "did not
    land" is exactly how an already-fixed item got dispatched.

    Only closure-claiming entries are asked, and that scoping is the point: a
    merged PR that merely MENTIONS the item cannot reach rule 1 whatever its
    ancestry, so letting its unanswerable ancestry return an error made the
    whole check UNKNOWN over a fact that could not have changed the verdict. An
    error is reserved for a question whose answer matters.
    """
    claiming = [hit for hit in merged if hit.get("closes_item")]
    if not claiming:
        return None
    if repo_dir is None:
        return "no-repo-dir"
    if git(repo_dir, ["rev-parse", "--verify", "--quiet", f"{default_branch}^{{commit}}"])[0] != 0:
        return "unknown-default-branch"
    for hit in claiming:
        sha = hit.get("merge_commit_sha")
        if not isinstance(sha, str) or not sha:
            return "no-merge-commit"
        rc = git(repo_dir, ["merge-base", "--is-ancestor", sha, default_branch])[0]
        if rc == 0:
            hit["landed"] = True
        elif rc == 1:
            hit["landed"] = False
        else:
            return "ancestry-unknown"
    return None


# --------------------------------------------------------------------------- #
# check 3 — prose
# --------------------------------------------------------------------------- #


#: Markdown shapes that CITE text rather than say it. Stripped before any prose
#: match, because a phrase in a code fence, a backtick span, a blockquote or a
#: pair of quotation marks belongs to whoever is being quoted. The span patterns
#: also exclude newlines, which :func:`plain_prose` has already collapsed by the
#: time they run — belt and braces, so each pattern is correct read alone.
#: HTML comments, stripped BEFORE everything else. A comment is invisible to
#: every human reader of the item, so nothing inside one is a statement its
#: author is making -- and this repository's issue templates ship instructional
#: comments, so "<!-- please close this issue when done -->" arrives in the body
#: of real items and closed them. First in the order because a comment can
#: contain a fence, a quote or a backtick span and must not be parsed as one.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
#: Paired backtick RUNS of equal length, not single backticks. Markdown lets an
#: author write ``this can be closed`` with doubled delimiters, and the
#: single-backtick pattern consumed each `` pair as an empty span, leaving the
#: quoted phrase behind as prose -- so a citation of the phrases this scanner
#: looks for closed a live item. The backreference makes the closing run match
#: the opening one, which is the rule Markdown itself uses.
_INLINE_CODE_RE = re.compile(r"(`+)[^`]*?\1")
_BLOCKQUOTE_RE = re.compile(r"^[ \t]*>.*$", re.MULTILINE)
#: Deliberately UNBOUNDED between the delimiters. A length cap here is a
#: false-CLOSE channel: a reporter who quotes "please close" inside a quotation
#: longer than the cap has the phrase read as their OWN prose, and this verdict
#: performs a write on live work. Linear despite the ``*`` because the class is
#: negated -- there is no nested quantifier to backtrack through.
_QUOTED_RE = re.compile('["\u201c\u201d][^"\u201c\u201d\n]*["\u201c\u201d]')
#: An UNMATCHED opening delimiter is the same channel by the other door: a
#: citation whose closing quote the author forgot (or that a smart-quote pair
#: broke) matches nothing above, so the phrase survives as the author's own
#: words. Everything from a leftover delimiter to the end is therefore dropped
#: too. This is the deliberately lossy direction -- prose after an unbalanced
#: quote can hide a REAL closure request, which costs one dispatch, where the
#: other reading closes somebody's live work.
_UNCLOSED_QUOTE_RE = re.compile('["\u201c\u201d].*$', re.DOTALL)


def plain_prose(text: str) -> str:
    """What the author SAYS, with what they QUOTE removed.

    Measured on the item that specified this script: its body quotes the very
    closure phrases the check looks for ("this is resolved / happy to have it
    closed", as a description of what to detect), and scanning it raw produced
    CLOSE on a live item. A false CLOSE closes work in flight; a missed one only
    costs the dispatch that discovers the work is done — so citations come out
    before matching, and the asymmetry runs the cheap way.

    Line structure first, then whitespace, then spans. Fences and blockquotes
    are line-shaped, so they have to go while the newlines are still there.
    Quotation marks are not, and markdown hard-wraps prose, so a quoted phrase
    routinely straddles a line break: collapsing whitespace before matching the
    spans is what makes the stripper as newline-tolerant as the ``\\s+`` in the
    phrases it defends. Without that step it missed the very body that found
    this bug, where the quote broke mid-phrase.

    Balanced spans go first, then any LEFTOVER delimiter takes the rest of the
    text with it. Both halves matter and the second was missing: an unclosed
    citation matches no pair, so the phrase inside it used to survive as the
    author's own words and returned CLOSE on a live item. Every span rule here
    is therefore allowed to strip too MUCH, never too little -- over-stripping
    can hide a real closure request and cost one dispatch, while under-stripping
    closes somebody's work in flight.
    """
    text = _HTML_COMMENT_RE.sub(" ", text)
    text = _FENCE_RE.sub(" ", text)
    text = _BLOCKQUOTE_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    text = _QUOTED_RE.sub(" ", text)
    return _UNCLOSED_QUOTE_RE.sub(" ", text)


def _first_match(patterns: tuple[str, ...], text: str) -> str | None:
    """The first pattern that fires UNNEGATED on ``text``, or None.

    Returns the PATTERN: the matched text is user-authored and never leaves this
    function. A match preceded by a negator inside the window is skipped rather
    than returned, and the scan continues -- "I don't think this is resolved, but
    please close this issue" should still be read as a request.
    """
    for pattern in patterns:
        for found in re.finditer(pattern, text, re.IGNORECASE):
            window = text[max(0, found.start() - _NEGATION_WINDOW) : found.start()]
            if _NEGATOR_RE.search(window):
                continue
            return pattern
    return None


def _is_bot(user: Any) -> bool:
    if not isinstance(user, dict):
        return False
    login = str(user.get("login") or "")
    return user.get("type") == "Bot" or login.endswith("[bot]")


def withdrawals_by_author(comments: Any) -> dict[str, str]:
    """login -> newest timestamp at which that login GAVE UP the item.

    Shared by the two places that need it, so the rule cannot drift between
    them: the claim recovery, and the check that retires a claim written in the
    issue BODY.
    """
    found: dict[str, str] = {}
    if not isinstance(comments, list):
        return found
    for comment in comments:
        if not isinstance(comment, dict) or _is_bot(comment.get("user")):
            continue
        if _first_match(WITHDRAWAL_RES, plain_prose(str(comment.get("body") or ""))) is None:
            continue
        user = comment.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        if not isinstance(login, str):
            continue
        when = str(comment.get("created_at") or "")
        if login not in found or when > found[login]:
            found[login] = when
    return found


def body_claim_withdrawn(comments: Any, reporter: str | None) -> bool:
    """Has the BODY's author since given the item up?

    The body predates every comment by construction, so any withdrawal from its
    author retires the claim it contains -- no timestamp comparison needed. This
    closes a permanent suppression: a reporter who wrote "Ownership claimed by
    me" and later commented "dropping this" left the item SKIPping forever,
    which is the indefinite-suppression harm rather than a wasted dispatch.
    """
    if reporter is None:
        return False
    return reporter in withdrawals_by_author(comments)


def newest_authorized_claim(comments: Any, reporter: str | None) -> dict | None:
    """The newest non-bot comment bearing a self-claim from someone with standing.

    :func:`last_human_comment` answers "what is the latest word", which is the
    right question for closure but the wrong one for ownership: measured, an
    insider comment "I am claiming this one" followed by a passer-by's "any
    update on this?" left the claim unscanned and the verdict CLAIM, dispatching
    a second worker onto work already in flight -- the exact class this script
    exists to remove.

    So ownership gets its own selector. A claim is a statement that stays true
    until withdrawn, not a line in a transcript that a later remark overwrites.
    "Until withdrawn" is enforced rather than assumed: a newer comment giving the
    item up ("dropping this", "no longer working on this") retires that AUTHOR's
    older claims, so recovery cannot park an item on somebody who already walked
    away.

    A withdrawal only ever releases its OWN author's claim. The first version
    applied any withdrawal to every earlier claim, which handed a stranger the
    power to erase a maintainer's claim by typing "dropping this" -- the same
    denial-of-work shape as an unauthorized claim, running the other direction
    and costing a duplicate dispatch instead of a suppression. You can only give
    up what you hold.
    """
    if not isinstance(comments, list):
        return None
    withdrawn_by = withdrawals_by_author(comments)
    best: dict | None = None
    best_key: tuple[str, int] | None = None
    for index, comment in enumerate(comments):
        if not isinstance(comment, dict) or _is_bot(comment.get("user")):
            continue
        body = plain_prose(str(comment.get("body") or ""))
        if _first_match(SELF_CLAIM_RES, body) is None:
            continue
        when = str(comment.get("created_at") or "")
        user = comment.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        released = withdrawn_by.get(login) if isinstance(login, str) else None
        if released is not None and when <= released:
            continue
        association = str(comment.get("author_association") or "")
        if association not in INSIDER_ASSOCIATIONS and not (
            reporter is not None and login == reporter
        ):
            continue
        key = (str(comment.get("created_at") or ""), index)
        if best_key is None or key > best_key:
            best, best_key = comment, key
    return best


def last_human_comment(comments: Any) -> dict | None:
    """The NEWEST non-bot comment, chosen by timestamp rather than by position.

    Two measurements shaped this. First, the per-issue comments endpoint
    documents only ``since``, ``per_page`` and ``page``: it silently IGNORES
    ``sort`` and ``direction`` and answers oldest-first. An earlier version of
    this function asked for ``direction=desc`` and took the first element, and
    on a real item that returned the OLDEST of twelve comments (2026-09-01
    10:40) against a newest of 2026-09-03 00:56 — so a reporter's later "please
    close" was invisible to the check that exists to find it. Second, this
    repository's triage bot comments on issues, and its summary was the OLDEST
    entry rather than the newest, so skipping bots is right but reading from
    either end is not.

    Hence selection by ``max(created_at)`` over non-bot comments and never by
    position: an endpoint that changes its order, or a client that merges pages
    in another sequence, cannot bring the bug back. Position breaks ties only
    when a timestamp is missing.
    """
    if not isinstance(comments, list):
        return None
    newest: dict | None = None
    best: tuple[str, int] | None = None
    for index, comment in enumerate(comments):
        if not isinstance(comment, dict) or _is_bot(comment.get("user")):
            continue
        stamp = comment.get("created_at")
        # ISO-8601 Z timestamps sort lexicographically exactly as they sort
        # chronologically. A missing one sorts lowest, so any timestamped
        # comment outranks it, and among timestampless comments the latest
        # POSITION wins.
        key = (stamp if isinstance(stamp, str) else "", index)
        if best is None or key > best:
            newest, best = comment, key
    return newest


def scan_prose(
    issue: dict,
    comment: dict | None,
    me: str | None,
    body_withdrawn: bool = False,
) -> dict:
    """The ``prose_claim`` check value. Pure: no forge access.

    ``me`` is the authenticated login. When it is unknown, a self-claim counts
    as somebody else's — the fail-safe direction is SKIP, never CLAIM.

    BOTH phrase sets need STANDING (the item's own author, always true of the
    body, or a repository insider by ``author_association``) but for opposite
    reasons, and the asymmetry is the point:

    * a closure request produces CLOSE, which acts on live work, so "please
      close" from a passer-by must not fire it;
    * a self-claim produces SKIP, and a veto anyone can cast is a
      denial-of-work channel -- one comment would suppress a queued item
      indefinitely with nothing downstream reporting the suppression.

    So an unauthorized claim is not discarded and not obeyed: it sets
    ``claim_without_standing``, which annotates ``risk=high`` and sends the item
    to the live recheck instead of parking it.
    """
    body = plain_prose(str(issue.get("body") or ""))
    body_author = (
        (issue.get("user") or {}).get("login") if isinstance(issue.get("user"), dict) else None
    )
    comment_body = plain_prose(str(comment.get("body") or "")) if comment else ""
    comment_author = None
    comment_id = None
    comment_standing = False
    if comment:
        user = comment.get("user")
        comment_author = user.get("login") if isinstance(user, dict) else None
        comment_id = comment.get("id")
        association = str(comment.get("author_association") or "")
        comment_standing = (
            comment_author is not None and comment_author == body_author
        ) or association in INSIDER_ASSOCIATIONS

    result: dict[str, Any] = {
        "closure_requested": False,
        "claimed_by_other": False,
        "claim_without_standing": False,
        "claimed_by": None,
        "closure_by": None,
        "where": None,
        "comment_id": None,
        "pattern": None,
    }

    # The comment is the fresher statement, so it is consulted first for both
    # phrase sets and its evidence wins when both sources match. The body's
    # author is the reporter by definition, so the body always has standing.
    #
    # For CLOSURE the body is consulted only when there is no newer human
    # comment at all. The body is the OLDEST text on the item, and a reporter
    # who wrote "this is resolved" and later commented "still broken on 0.8" has
    # reopened it -- reading the body then closed live work. A later comment is
    # the item's current state whether or not it happens to contain a phrase
    # this scanner knows, so its mere existence retires the body's request. The
    # CLAIM loop below still reads the body, because a claim is a statement of
    # ownership that holds until withdrawn rather than a status.
    closure_sources = [
        ("comment", comment_body, comment_author, comment_id, comment_standing),
    ]
    if comment is None:
        closure_sources.append(("body", body, body_author, None, True))
    for where, text, author, ident, standing in closure_sources:
        if not text or result["closure_requested"]:
            continue
        pattern = _first_match(CLOSURE_RES, text)
        if pattern and standing:
            result.update(
                closure_requested=True,
                closure_by=author,
                where=where,
                comment_id=ident,
                pattern=pattern,
            )
    claim_sources = [
        ("comment", comment_body, comment_author, comment_id, comment_standing),
    ]
    if not body_withdrawn:
        # A claim in the body outlives later chatter, but not its own author's
        # withdrawal: a reporter who claimed the item and later commented
        # "dropping this" left it SKIPping forever, which is indefinite
        # suppression rather than a wasted dispatch.
        claim_sources.append(("body", body, body_author, None, True))
    for where, text, author, ident, standing in claim_sources:
        if not text or result["claimed_by_other"]:
            continue
        pattern = _first_match(SELF_CLAIM_RES, text)
        if not pattern or (me is not None and author == me):
            continue
        if standing:
            result.update(claimed_by_other=True, claimed_by=author, claimed_by_where=where)
            if not result["closure_requested"]:
                result.update(where=where, comment_id=ident, pattern=pattern)
        elif not result["claim_without_standing"]:
            # A veto anyone can cast is a denial-of-work channel: one comment
            # from a passer-by would suppress a queued item indefinitely and
            # nothing downstream would report that it had been suppressed. So an
            # unauthorized claim annotates risk=high and takes the live recheck
            # instead of parking the item.
            result.update(claim_without_standing=True, claimed_by=author)
    return result


# --------------------------------------------------------------------------- #
# check 4 — symbols on the base
# --------------------------------------------------------------------------- #


_SYMBOL_TOKEN_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)(?:\(\))?`")


def looks_like_symbol(token: str) -> bool:
    """Whether a backticked token is plausibly an identifier and not English.

    Deliberately narrow: a false symbol would park a workable item. An
    underscore or a camel hump is the signal; ``main`` and ``true`` are not
    symbols however they are quoted.
    """
    if len(token) < 4:
        return False
    if "_" in token:
        return True
    return bool(re.search(r"[a-z][A-Z]", token))


def named_symbols(text: str, limit: int = MAX_SYMBOLS) -> list[str]:
    """Identifiers the item names, in first-appearance order, capped."""
    found: list[str] = []
    for token in _SYMBOL_TOKEN_RE.findall(text or ""):
        if looks_like_symbol(token) and token not in found:
            found.append(token)
            if len(found) >= limit:
                break
    return found


#: Bug-class metadata. Matched against LABEL names and the issue type only --
#: never prose. A label is a deliberate act by a human triaging the item, which
#: is what makes it corroboration; guessing the class from wording would put an
#: unmeasured heuristic in front of a veto, and the veto is the thing that was
#: over-applied in the first place.
BUG_CLASS_RE = re.compile(r"\b(?:bug|defect|regression|crash)\b", re.IGNORECASE)


def bug_class_of(issue: dict) -> tuple[bool, str | None]:
    """Whether the item is corroborated as bug-class, and by what.

    Only a bug item supports the inference "this symbol is absent, so the target
    code lives on an unmerged branch". A FEATURE REQUEST names the symbol it
    proposes to ADD, so absence is expected and vetoing on it would park that
    whole item class permanently -- it would still be absent on the next pass,
    and the one after.

    Corroboration is explicit metadata, so an item nobody has triaged is simply
    not corroborated. That direction is the cheap one: the item is dispatched
    with ``risk=high`` and a worker may find the code is not on the base, which
    costs one dispatch, against a park that costs the item.

    What comes back is the MATCHED TERM, never the label's own text: a label
    name is user-authored, and this value is printed. The issue type
    ``type: Bug (urgent!)`` reports ``type:bug`` and a label ``Bug (urgent!)``
    reports ``label:bug`` -- the vocabulary is this module's, which is the same
    rule the prose scan follows when it reports a pattern rather than a
    sentence.
    """
    kind = issue.get("type")
    if isinstance(kind, dict):
        name = kind.get("name")
        if isinstance(name, str):
            hit = BUG_CLASS_RE.search(name)
            if hit:
                return True, f"type:{hit.group(0).lower()}"
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            hit = BUG_CLASS_RE.search(name)
            if hit:
                return True, f"label:{hit.group(0).lower()}"
    return False, None


def symbols_on_base(
    symbols: list[str],
    repo_dir: str | None,
    default_branch: str,
    *,
    bug_class: bool = False,
    bug_class_by: str | None = None,
) -> dict:
    """The ``symbol_on_base`` check value: which named symbols exist on base.

    ``bug_class`` rides along rather than being consulted here, because this
    function answers a question of fact and :func:`verdict` decides what the
    fact is worth.
    """
    corroboration = {"bug_class": bug_class, "bug_class_by": bug_class_by}
    if not symbols:
        return {"symbols": [], "present": [], "missing": [], **corroboration}
    if repo_dir is None:
        return {"error": "no-repo-dir", "symbols": symbols, **corroboration}
    if git(repo_dir, ["rev-parse", "--verify", "--quiet", f"{default_branch}^{{commit}}"])[0] != 0:
        return {"error": "unknown-default-branch", "symbols": symbols, **corroboration}
    present: list[str] = []
    missing: list[str] = []
    for symbol in symbols:
        rc = git(repo_dir, ["grep", "-l", "-F", "-e", symbol, default_branch, "--"])[0]
        if rc == 0:
            present.append(symbol)
        elif rc == 1:
            missing.append(symbol)
        else:
            # git could not answer; an unsearchable tree is not an absent
            # symbol, and absence is what parks the item.
            return {"error": "grep-failed", "symbols": symbols, **corroboration}
    return {"symbols": symbols, "present": present, "missing": missing, **corroboration}


# --------------------------------------------------------------------------- #
# check 5 — recency and self-claim risk
# --------------------------------------------------------------------------- #


def _age_days(created_at: Any, now: datetime) -> int | None:
    if not isinstance(created_at, str) or not created_at:
        return None
    try:
        stamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(int((now - stamp).total_seconds() // 86400), 0)


def scan_recency(issue: dict, now: datetime | None = None) -> dict:
    """The ``recency`` check value. Pure: age, association, and the risk it implies."""
    now = now or datetime.now(timezone.utc)
    age = _age_days(issue.get("created_at"), now)
    association = str(issue.get("author_association") or "") or None
    if age is None:
        # An unparseable timestamp cannot veto anything, but it also cannot
        # certify low risk. Say so rather than defaulting to reassurance.
        return {"age_days": None, "author_association": association, "risk": "high"}
    fresh = age <= RECENT_DAYS
    active = (association or "") in ACTIVE_ASSOCIATIONS
    return {
        "age_days": age,
        "author_association": association,
        "risk": "high" if (fresh and active) else "low",
    }


# --------------------------------------------------------------------------- #
# the verdict — pure function of the checks
# --------------------------------------------------------------------------- #


def entries(value: Any) -> list[dict]:
    """The hits in a list-shaped check, or none when the check errored."""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def errored(value: Any) -> str | None:
    """The error slug of a check that could not be answered, or None."""
    if isinstance(value, dict) and value.get("error"):
        return str(value["error"])
    return None


def uncorroborated_absent_symbols(checks: dict) -> list[str]:
    """Symbols absent from the base on an item NOT corroborated as bug-class.

    Not a veto (see :func:`bug_class_of`) but not nothing either: the item may
    target code that is not on the base, so it is dispatched with ``risk=high``
    rather than parked or waved through as routine.
    """
    symbols = checks.get("symbol_on_base")
    if not isinstance(symbols, dict) or errored(symbols) or symbols.get("bug_class"):
        return []
    missing = symbols.get("missing")
    return [item for item in missing if isinstance(item, str)] if isinstance(missing, list) else []


def unauthorized_claim(checks: dict) -> str | None:
    """The login of someone who claimed the item without standing, or None.

    Not a veto (see :func:`scan_prose`) but not nothing: the claim may be real,
    so the item is dispatched at ``risk=high`` for the live recheck rather than
    parked on an unauthorized commenter's word.
    """
    prose = checks.get("prose_claim")
    if not isinstance(prose, dict) or errored(prose):
        return None
    if not prose.get("claim_without_standing"):
        return None
    who = prose.get("claimed_by")
    return str(who) if who else "unknown"


def untrusted_fork_skip(checks: dict) -> int | None:
    """The number of the first open PR whose SKIP is an untrusted fork, or None.

    The counterpart to :func:`unauthorized_claim`: both name a finding this
    script acts on while doubting, so the doubt has to travel with the verdict.
    """
    for hit in entries(checks.get("open_prs")):
        if hit.get("untrusted_fork"):
            number = hit.get("number")
            return number if isinstance(number, int) else None
    return None


def risk_of(checks: dict) -> str:
    """``low`` or ``high``, from check 5, from an uncorroborated absent symbol,
    from an unauthorized self-claim, and from an untrusted-fork suppression,
    defaulting to the cautious side."""
    if uncorroborated_absent_symbols(checks) or unauthorized_claim(checks):
        return "high"
    if untrusted_fork_skip(checks) is not None:
        return "high"
    recency = checks.get("recency")
    if not isinstance(recency, dict) or errored(recency):
        return "high"
    return "high" if recency.get("risk") == "high" else "low"


def verdict(checks: dict) -> tuple[str, str, dict]:
    """(verdict, reason, evidence) — the whole decision, first match wins.

    Pure: a dict of fabricated checks in, a verdict out, no forge and no git.
    Every branch below is a dispatch the old single-question predicate would
    have made.
    """
    for hit in entries(checks.get("merged_prs")):
        if hit.get("landed") is True and hit.get("closes_item") is True:
            # BOTH conditions are load-bearing: a PR merged somewhere other than
            # the base is not coverage, and a PR that merely MENTIONS the item is
            # not closure. A mention falls through to the remaining checks.
            return (
                "CLOSE",
                "already-fixed",
                {
                    "pr": hit.get("number"),
                    "sha": (str(hit.get("merge_commit_sha") or ""))[:10],
                    "landed": True,
                },
            )
    for hit in entries(checks.get("open_prs")):
        return (
            "SKIP",
            "open-pr",
            {
                "pr": hit.get("number"),
                "fork": bool(hit.get("is_cross_repository")),
                "author": hit.get("author"),
                "untrusted_fork": bool(hit.get("untrusted_fork")),
            },
        )
    prose = checks.get("prose_claim")
    if isinstance(prose, dict) and not errored(prose):
        if prose.get("closure_requested"):
            return (
                "CLOSE",
                "reporter-asked-close",
                {"comment_id": prose.get("comment_id"), "where": prose.get("where")},
            )
        if prose.get("claimed_by_other"):
            return (
                "SKIP",
                "prose-claim",
                {
                    "claimed_by": prose.get("claimed_by"),
                    "where": prose.get("claimed_by_where") or prose.get("where"),
                },
            )
    symbols = checks.get("symbol_on_base")
    if isinstance(symbols, dict) and not errored(symbols):
        missing = symbols.get("missing")
        if isinstance(missing, list) and missing and symbols.get("bug_class"):
            # Corroborated bug item: absence really does mean the target span
            # lives somewhere other than the base. An UNCORROBORATED item falls
            # through instead, and risk_of() turns its absence into risk=high.
            return (
                "SKIP",
                "symbol-absent",
                {
                    "symbol": missing[0],
                    "missing": list(missing),
                    "bug_class_by": symbols.get("bug_class_by"),
                },
            )
    # Only now: a definite finding outranks a partial view, and no error path
    # may reach CLAIM.
    for name in CHECK_NAMES:
        slug = errored(checks.get(name))
        if slug:
            return "UNKNOWN", slug, {"check": name, "reason": slug}
    evidence: dict[str, Any] = {"risk": risk_of(checks)}
    uncorroborated = uncorroborated_absent_symbols(checks)
    if uncorroborated:
        # Say WHY the risk is high. The human line is one field wide by
        # contract, so the reason lives in --json rather than being dropped.
        evidence["symbol_absent_uncorroborated"] = uncorroborated
    claimed = unauthorized_claim(checks)
    if claimed:
        evidence["claim_without_standing"] = claimed
    return "CLAIM", "clean", evidence


def human_line(item: int, name: str, reason: str, evidence: dict, risk: str) -> str:
    """The one-line human form. Field names are the contract's, values are
    metadata only — never user-authored text."""
    if name == "CLAIM":
        return f"CLAIM {item} risk={risk}"
    if name == "UNKNOWN":
        return f"UNKNOWN {item} check={evidence.get('check')} reason={evidence.get('reason')}"
    if reason == "already-fixed":
        return (
            f"CLOSE {item} merged-pr=#{evidence.get('pr')} "
            f"sha={evidence.get('sha')} landed=true"
        )
    if reason == "open-pr":
        fork = "true" if evidence.get("fork") else "false"
        line = (
            f"SKIP {item} open-pr=#{evidence.get('pr')} fork={fork} author={evidence.get('author')}"
        )
        if evidence.get("untrusted_fork"):
            # Only the doubtful case is annotated: a marker on every routine
            # SKIP is noise, and this exists so the one that needs a look does
            # not read identically to the hundred that do not.
            line += f" untrusted-fork=true risk={risk}"
        return line
    if reason == "reporter-asked-close":
        ident = evidence.get("comment_id")
        tail = f"comment-id={ident}" if ident is not None else f"where={evidence.get('where')}"
        return f"CLOSE {item} reporter-asked-close {tail}"
    if reason == "prose-claim":
        return (
            f"SKIP {item} prose-claim claimed-by={evidence.get('claimed_by')} "
            f"where={evidence.get('where')}"
        )
    if reason == "symbol-absent":
        return f"SKIP {item} symbol-absent={evidence.get('symbol')}"
    return f"{name} {item} {reason}"  # pragma: no cover - every reason above is covered


# --------------------------------------------------------------------------- #
# collection — thin IO around the pure parts
# --------------------------------------------------------------------------- #


def whoami() -> str | None:
    """The authenticated login, or None. Called only when a self-claim phrase
    already matched, so the common path pays nothing for it."""
    data, error = gh_json(["gh", "api", "user"])
    if error or not isinstance(data, dict):
        return None
    login = data.get("login")
    return login if isinstance(login, str) and login else None


def collect(repo: str, item: int, default_branch: str, repo_dir: str | None) -> dict:
    """Run all five checks. A check that cannot be answered carries ``error``."""
    checks: dict[str, Any] = {}

    open_prs, merged_prs, prs_error = referencing_prs(repo, item)
    if prs_error:
        # A confirmed open PR survives the error: the precedence rule that puts
        # a definite finding above a partial view applies to a half-finished
        # scan too, so the SKIP is kept and only the merged side reads unknown.
        checks["open_prs"] = open_prs if open_prs else {"error": prs_error}
        checks["merged_prs"] = {"error": prs_error}
    else:
        landed_error = annotate_landed(merged_prs, repo_dir, default_branch)
        checks["open_prs"] = open_prs
        checks["merged_prs"] = (
            {"error": landed_error, "count": len(merged_prs)} if landed_error else merged_prs
        )

    issue, issue_error = gh_json(["gh", "api", f"repos/{repo}/issues/{item}"])
    if issue_error or not isinstance(issue, dict):
        slug = issue_error or "unparseable-json"
        checks["prose_claim"] = {"error": slug}
        checks["recency"] = {"error": slug}
        checks["symbol_on_base"] = {"error": slug}
        # An open PR still SKIPs when the item itself cannot be read, so the
        # suppression still needs its marker. Nobody is known to be the
        # reporter here, which annotates more forks rather than fewer.
        #
        # Keyed on the HITS, not on the absence of an error: a half-finished scan
        # can now return a confirmed open PR alongside its error, and gating on
        # `not prs_error` emitted that SKIP as a routine low-risk one. The loud
        # suppression has to survive the degraded path, or it is missing in
        # exactly the conditions that make a suppression hard to notice.
        annotate_untrusted_forks(open_prs, None)
    else:
        reporter = (issue.get("user") or {}).get("login") if isinstance(issue, dict) else None
        annotate_untrusted_forks(open_prs, reporter if isinstance(reporter, str) else None)
        comments, comments_error = gh_json(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/{item}/comments?per_page={COMMENT_PAGE}",
                "--paginate",
            ]
        )
        if comments_error:
            # The body alone is half the question; half an answer to a question
            # about ownership is not permission.
            checks["prose_claim"] = {"error": comments_error}
        else:
            comment = last_human_comment(comments)
            withdrawn = body_claim_withdrawn(comments, reporter)
            prose = scan_prose(issue, comment, None, withdrawn)
            if not prose["claimed_by_other"] and not prose["closure_requested"]:
                # The latest word answers closure; ownership needs its own
                # lookup, because any later remark would otherwise bury a
                # standing claim and send a second worker onto live work.
                claimed = newest_authorized_claim(comments, reporter)
                if claimed is not None and claimed is not comment:
                    recovered = scan_prose(issue, claimed, None, withdrawn)
                    if recovered["claimed_by_other"]:
                        comment, prose = claimed, recovered
            if prose["claimed_by_other"]:
                # Re-scan knowing who we are: our own claim is not somebody
                # else's. One extra call, only when it can change the verdict.
                prose = scan_prose(issue, comment, whoami(), withdrawn)
            checks["prose_claim"] = prose
        checks["recency"] = scan_recency(issue)
        text = f"{issue.get('title') or ''}\n{issue.get('body') or ''}"
        bug_class, bug_class_by = bug_class_of(issue)
        checks["symbol_on_base"] = symbols_on_base(
            named_symbols(text),
            repo_dir,
            default_branch,
            bug_class=bug_class,
            bug_class_by=bug_class_by,
        )
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claim preflight for one work item.")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--item", required=True, type=int, help="issue number")
    parser.add_argument("--default-branch", default="main")
    parser.add_argument("--repo-dir", default=None, help="a clone of the base, read-only")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    if not _REPO_RE.match(args.repo):
        print(f"malformed --repo {args.repo!r}: expected owner/name", file=sys.stderr)
        return 2
    if args.item <= 0:
        print(f"malformed --item {args.item}: expected a positive issue number", file=sys.stderr)
        return 2
    if not args.default_branch.strip():
        print("malformed --default-branch: expected a git rev", file=sys.stderr)
        return 2
    if args.repo_dir is not None:
        # A path the caller passed that is not a git work tree is malformed
        # config (exit 2), unlike an OMITTED clone, which is a question this run
        # simply cannot answer (UNKNOWN).
        if run(["git", "-C", args.repo_dir, "rev-parse", "--git-dir"])[0] != 0:
            print(f"malformed --repo-dir {args.repo_dir!r}: not a git repository", file=sys.stderr)
            return 2

    checks = collect(args.repo, args.item, args.default_branch, args.repo_dir)
    name, reason, evidence = verdict(checks)
    risk = risk_of(checks)
    if args.as_json:
        print(
            json.dumps(
                {
                    "item": args.item,
                    "verdict": name,
                    "reason": reason,
                    "risk": risk,
                    "checks": checks,
                    "evidence": evidence,
                },
                sort_keys=True,
            )
        )
    else:
        print(human_line(args.item, name, reason, evidence, risk))
    return EXIT_CODES[name]


if __name__ == "__main__":
    sys.exit(main())
