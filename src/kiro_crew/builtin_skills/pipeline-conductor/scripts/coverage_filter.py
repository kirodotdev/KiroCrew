#!/usr/bin/env python3
"""Coverage filter - the batch open-PR exclusion for the conductor's queue build.

WHY THIS EXISTS. The queue is built from a work source that SELECTS by label and
EXCLUDES by label (``select_labels`` / ``skip_signals``). A contributor who opens
a pull request carrying ``Fixes #N`` links that PR to the item for auto-close on
merge and applies no label at all, so an item whose fix is already in flight is
indistinguishable from a free one to a label-shaped selector. Measured on
kirodotdev/KiroCrew against the documented selector: of 29 label-clean
candidates, 25 were referenced by an OPEN pull request - 86% of what the queue
build emitted was work somebody was already doing.

``claim_preflight.py`` already refuses those items, one at a time, at claim time
(check 1, ``SKIP open-pr``). That script is the authority on one candidate and it
stays the authority. What it cannot also be is the QUEUE filter: its evidence is
the item's own timeline, so it costs one timeline read plus one detail read per
referencing PR, FOR EACH item. Paid across a whole backlog on every cycle, that
is the rediscovery this script removes - the coverage question moves upstream of
the queue instead of living downstream of the scanner.

So the same question is answered from the other end, ONCE for the whole batch:
read the repository's open pull requests - one paginated call, fork PRs included,
because the pulls list carries cross-repository heads - and report which
candidates any of them references.

THE PROPERTY THAT MAKES TWO EVIDENCE SOURCES SAFE: this filter only ever
SUBTRACTS.

  * ``COVERED`` is a positive finding - a CLOSING KEYWORD aimed at the item in a
    pull request's own title or body - and the item leaves the queue.
  * ``MENTIONED`` is a reference with no closing keyword. The item STAYS in the
    queue and the reference is reported. ``Refs #N`` is this repository's own
    idiom for referenced-but-deliberately-not-closed and its PR template keeps
    ``Related Issues`` apart from a closing trailer, so subtracting on a bare
    reference removed items whose referencing PR said in plain words that it was
    not fixing them. Measured over one real candidate list: of 21 (item,
    covering PR) pairs, 18 carried a closing keyword and 3 did not, and all 3 of
    those PRs disclaimed the fix in their own words.
  * ``UNCOVERED`` is NOT permission and certifies nothing. A reference made in a
    PR COMMENT appears in the item's timeline and not here, so this script's
    silence is a smaller view rather than a clean bill, and
    ``claim_preflight.py`` still runs before every claim.

``MENTIONED`` and ``UNCOVERED`` both leave the item a candidate, and they are
separate lines anyway, because a declined subtraction that printed as
``UNCOVERED`` would be exactly as silent as the subtraction it replaced. The
reference may still be work in flight whose author never spelled a keyword, and
the conductor is the one who can look.

Read the other way round - as a certificate - the cheaper evidence would widen
what gets dispatched, which is the opposite of what this change is for. Hence
``UNKNOWN`` prints no ``uncovered`` list at all: an unanswered batch must not
render as "none of these are covered".

Usage:
    python3 coverage_filter.py --repo <owner/repo> --items 10890,10849,9736
    python3 coverage_filter.py --repo <owner/repo> --items -   # numbers on stdin
                              [--json]

    --repo   ``owner/name`` of the forge repository holding the items
    --items  comma- or whitespace-separated item numbers, or ``-`` to read them
             from stdin. Reading from stdin is what lets the queue build pipe a
             selector's output straight in without a shell loop that would make
             one forge call per item and lose the whole point.
    --json   print exactly one JSON object instead of the human lines

Exit codes:

    0   answered - read the ``covered`` / ``mentioned`` / ``uncovered`` split
    2   malformed arguments
    3   UNKNOWN - the forge could not be read, so NO exclusion was computed and
        every candidate stays in the queue

Deliberately boring properties, do not weaken:

  * Forge access goes through ``gh`` and :func:`run_gh` refuses a mutating argv
    before the subprocess exists, so the no-write property is enforced rather
    than intended: this script never labels, assigns, comments or closes. The
    helpers below mirror ``claim_preflight.py`` rather than importing it - a
    skill script runs as a bare file with ``kiro_crew`` off the import path and
    with no sibling on it either, which is why ``ledger.py`` and
    ``credit_spend.py`` carry their own copies of the same shapes.
  * ONE forge call for the whole batch, up to ``MAX_ITEMS`` candidates. A
    per-item call here would reinstate the cost that keeps the check downstream.
    A larger batch is REFUSED (exit 2) rather than truncated, because a silently
    truncated batch would print unscanned items as ``uncovered``; page the queue
    build instead.
  * No user-authored PROSE reaches stdout. A pull request's title and body are
    read and never printed; what appears is identifiers - item numbers, PR
    numbers and logins - because those are the evidence a conductor needs to
    check a subtraction, and a login is chosen by its owner rather than written
    for this item.
  * A DRAFT pull request that claims closure counts as coverage, and so does a
    fork PR. Both are work in flight, and both are what ``claim_preflight.py``'s
    check 1 counts: the two rules answer the same question from different
    evidence, so a difference in what they count would be drift rather than
    nuance. The closing-keyword condition is now part of that agreement -- that
    script's rule 2 suppresses on a closing keyword and reports a bare reference,
    and this one subtracts and reports on exactly the same line, so neither
    admits an item the other refuses.
  * A CLOSED pull request is neither coverage nor a claim - merged work is
    ``claim_preflight.py``'s rule 1 (CLOSE, on ancestry), and closed-unmerged
    work is abandoned and frees the item. Only ``state=open`` is read.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any

_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

#: Pull requests per page. The maximum the endpoint allows, so a repository with
#: hundreds of open PRs still costs a handful of pages rather than dozens.
PULL_PAGE = 100

#: How many candidates one invocation will answer for. A batch larger than this
#: is a queue build that should be paged, and silently answering a truncated
#: batch would report items as uncovered that were never scanned.
MAX_ITEMS = 500

#: Standing that makes a cross-repository pull request a vouched one. The item's
#: own reporter is the other source of standing in ``claim_preflight.py``, and it
#: is deliberately not consulted here: learning it costs one forge call PER item,
#: which is the cost this script exists to avoid. The consequence is that a
#: reporter fixing their own bug from a fork annotates as unvouched - that
#: annotates MORE subtractions, never fewer, which is the safe direction for a
#: marker whose only job is to make a suppression visible.
INSIDER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

#: ``gh`` shapes this script is allowed to run. Everything else - including every
#: write verb - is refused before the subprocess starts.
_READ_SHAPES = (
    ("api",),
    ("pr", "list"),
)
_WRITE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
#: ``gh api -f/-F/--field/--raw-field`` implies POST, so a "GET" argv carrying one
#: of these is a write.
_FIELD_FLAGS = {"-f", "-F", "--field", "--raw-field", "--input"}


def run(args: list[str]) -> tuple[int, str, str]:
    """(rc, stdout, stderr) with a missing binary as rc 127, never a traceback."""
    try:
        done = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    except OSError as exc:
        return 127, "", f"{args[0]}: {exc}"
    return done.returncode, (done.stdout or "").strip(), (done.stderr or "").strip()


def is_read_only(args: list[str]) -> bool:
    """Whether ``args`` is one of the read shapes this script may run.

    The no-write rule is a property of the script rather than a promise in its
    docstring: an argv that could mutate the forge is refused here, before any
    subprocess exists, so a future edit cannot add a write without also editing
    this allowlist where the intent is obvious in review.
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
        return 126, "", "refused: coverage_filter performs no writes"
    return run(args)


def error_slug(rc: int, err: str) -> str:
    """A slug for a failed forge call. Never the stderr text.

    The caller prints this into an agent's context, and forge stderr can carry a
    URL with a token in it. A slug plus the exit code is enough to act on.
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

    ``gh api --paginate`` merges JSON array pages into a single document, so the
    single-document path is the one that runs. The concatenated shape is
    tolerated anyway and deliberately: this script pins no gh version, and being
    right about today's gh is a worse guarantee than not caring which gh it is.
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


#: GitHub's closing keywords, spelled exactly as ``claim_preflight.py``'s
#: ``_CLOSING_WORDS``. The two scripts answer one question from different
#: evidence, so what must never drift is the vocabulary: a keyword one of them
#: honours and the other does not is a queue that admits items the preflight
#: refuses, or subtracts items it would have dispatched.
_CLOSING_WORDS = "close[sd]?|fix(?:e[sd])?|resolve[sd]?"


def _item_target(repo: str, item: int) -> str:
    """The three spellings GitHub links on, as one alternation.

    ``#N``, ``owner/repo#N``, and the full issue URL. ``\\b`` after the number
    stops ``#12`` matching ``#123``.

    Shared by :func:`item_reference_re` and :func:`closing_reference_re` rather
    than written twice, because the difference between them is the KEYWORD and
    nothing else. Two copies of the target would let the spellings drift while
    both patterns still looked right.

    The bare ``#N`` form declines a number carrying a QUALIFIER --
    ``otherowner/otherrepo#N``, ``v1.2#N`` -- because the only way a subtractive
    filter can be wrong is a FALSE subtraction, and that is a silent denial of
    work on an item nobody is fixing. ``owner/repo#N`` for THIS repository is
    still matched, by its own alternative, and that alternative carries the SAME
    left boundary: without it a lookalike owner whose name merely ENDS in this
    one (``fakeowner/repo#N``) would match by substring, which is the same false
    subtraction arriving through the other door. The lookbehind is one character
    wide, which is what Python's ``re`` allows.
    """
    owner_repo = re.escape(repo)
    return (
        rf"(?:(?<![\w./-])#{item}\b"
        rf"|(?<![\w./-]){owner_repo}#{item}\b"
        rf"|https?://github\.com/{owner_repo}/issues/{item}\b)"
    )


def item_reference_re(repo: str, item: int) -> re.Pattern[str]:
    """A pattern matching a reference to THIS item, keyword or not.

    This is the WEAKER of the two readings and it decides no subtraction. What it
    finds is that a pull request names the item at all, which is reported as
    ``MENTIONED`` and leaves the item in the queue;
    :func:`closing_reference_re` is what subtracts.

    The split exists because a bare reference is not a claim to fix anything.
    ``Refs #N`` is this repository's own idiom for
    referenced-but-deliberately-not-closed and its PR template keeps
    ``Related Issues`` apart from a closing trailer, so subtracting on a bare
    reference removes items whose referencing PR says in plain words that it is
    not fixing them -- and removes them silently, which is the worse half.
    """
    return re.compile(_item_target(repo, item), re.IGNORECASE)


def closing_reference_re(repo: str, item: int) -> re.Pattern[str]:
    """A pattern matching a closing keyword aimed at THIS item.

    The subtracting reading, and the same one ``claim_preflight.py`` applies to
    both its open and its merged PRs. A closing keyword is a pull request
    claiming to finish the item, which is what makes it coverage; a mention is a
    pointer and decides nothing.

    Deliberately negation-blind, matching GitHub's own parser and the preflight's:
    "does not close #N" links and closes #N on the forge too, so treating it as a
    claim keeps all three readings identical.
    """
    return re.compile(
        rf"\b(?:{_CLOSING_WORDS})\s*:?\s+{_item_target(repo, item)}",
        re.IGNORECASE,
    )


def parse_items(raw: str, stdin_text: str) -> tuple[list[int], str | None]:
    """(items, error message) from ``--items``, in first-appearance order.

    Commas and whitespace both separate, because the two callers spell a list
    differently: a human types ``--items 1,2,3`` and a selector pipes one number
    per line.
    """
    text = stdin_text if raw.strip() == "-" else raw
    tokens = [token for token in re.split(r"[,\s]+", text) if token]
    if not tokens:
        return [], "no items: expected at least one issue number"
    items: list[int] = []
    for token in tokens:
        if not token.isdigit() or int(token) <= 0:
            return [], f"malformed item {token!r}: expected a positive issue number"
        number = int(token)
        if number not in items:
            items.append(number)
    if len(items) > MAX_ITEMS:
        return [], f"too many items: {len(items)} exceeds {MAX_ITEMS}"
    return items, None


def open_pull_requests(repo: str) -> tuple[list[dict], str | None]:
    """(pulls, error slug) for every OPEN pull request on ``repo``.

    One paginated call. Each entry keeps only what the answer needs: the number,
    the author and their association, whether the head is cross-repository, and
    the title and body as SEPARATE fields. Both are read here and never printed.
    """
    data, error = gh_json(
        [
            "gh",
            "api",
            f"repos/{repo}/pulls?state=open&per_page={PULL_PAGE}",
            "--paginate",
        ]
    )
    if error:
        return [], error
    if not isinstance(data, list):
        return [], "unparseable-json"
    pulls: list[dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        number = entry.get("number")
        if not isinstance(number, int):
            continue
        head = entry.get("head") or {}
        base = entry.get("base") or {}
        head_repo = (head.get("repo") or {}).get("full_name") if isinstance(head, dict) else None
        base_repo = (base.get("repo") or {}).get("full_name") if isinstance(base, dict) else None
        user = entry.get("user") or {}
        pulls.append(
            {
                "number": number,
                "author": user.get("login") if isinstance(user, dict) else None,
                # A deleted head repo reads as cross-repository, the safe side.
                "is_cross_repository": head_repo != base_repo,
                "author_association": entry.get("author_association"),
                # Kept SEPARATE and never joined. A closing reference is honoured
                # by the forge only within one field, and the closing pattern's
                # ``\s+`` matches a newline, so a joined field lets a title ending
                # in a closing word glue to a body opening with a bare ``#N`` and
                # match a reference neither field carries -- fabricated coverage,
                # which subtracts an item nobody is fixing. Both are read here and
                # neither is printed.
                "title": str(entry.get("title") or ""),
                "body": str(entry.get("body") or ""),
            }
        )
    return pulls, None


def coverage(repo: str, items: list[int], pulls: list[dict]) -> dict[int, list[dict]]:
    """item -> the open PRs referencing it, in PR order. Pure: no forge, no git.

    Every item gets a key, so a caller reading this mapping cannot mistake an
    absent key for an unscanned item.

    Every hit carries ``closes``: whether the reference was introduced by a
    closing keyword WITHIN one field. The title and the body are searched
    separately and never joined, because the closing pattern's ``\\s+`` spans a
    newline and a joined field would match a reference neither field carries.
    This function reports the FACT and decides nothing -- which of the two classes
    subtracts is :func:`split_hits` and the caller's business, the same separation
    ``claim_preflight.py`` keeps between its detectors and its verdict.
    """
    found: dict[int, list[dict]] = {item: [] for item in items}
    patterns = [
        (item, item_reference_re(repo, item), closing_reference_re(repo, item)) for item in items
    ]
    for pull in pulls:
        # Each field on its own, never concatenated. See open_pull_requests: a
        # joined field lets a closing word at the end of the title glue to a bare
        # ``#N`` at the start of the body and fabricate coverage that subtracts an
        # item nobody is fixing.
        fields = [str(pull.get("title") or ""), str(pull.get("body") or "")]
        if not any(fields):
            continue
        for item, reference, closing in patterns:
            if not any(reference.search(field) for field in fields):
                continue
            association = str(pull.get("author_association") or "")
            found[item].append(
                {
                    "pr": pull.get("number"),
                    "fork": bool(pull.get("is_cross_repository")),
                    "author": pull.get("author"),
                    # The subtraction turns on this and nothing else.
                    "closes": any(closing.search(field) for field in fields),
                    # Annotation only: the subtraction happens either way, for
                    # the reason INSIDER_ASSOCIATIONS gives. What this buys is
                    # that the one suppression worth a look does not read
                    # identically to the hundred that are routine.
                    "unvouched": bool(pull.get("is_cross_repository"))
                    and association not in INSIDER_ASSOCIATIONS,
                }
            )
    return found


def split_hits(hits: list[dict]) -> tuple[list[dict], list[dict]]:
    """(claims closure, mentions only) out of one item's hits, in PR order.

    The policy, in one place: only the first list subtracts. An item with a
    closing hit is COVERED whatever else references it, because a pull request
    has claimed to finish it; an item with references but no closing hit is
    MENTIONED, which is reported and stays in the queue.
    """
    closing = [hit for hit in hits if hit.get("closes")]
    return closing, [hit for hit in hits if not hit.get("closes")]


def human_lines(items: list[int], found: dict[int, list[dict]], pulls_read: int) -> list[str]:
    """The human form: one line per item, then one summary line.

    Three line kinds, because there are three answers. ``COVERED`` subtracts,
    ``MENTIONED`` reports a reference that is not a claim to fix and leaves the
    item in the queue, ``UNCOVERED`` saw no reference at all. ``MENTIONED`` and
    ``UNCOVERED`` both mean "stays a candidate" and differ in what the conductor
    is told, which is the whole point of separating them: a declined subtraction
    that printed as ``UNCOVERED`` would be exactly as silent as the subtraction
    it replaced.

    Field names are the contract's and values are metadata only - never
    user-authored text.
    """
    lines: list[str] = []
    covered = 0
    mentioned = 0
    for item in items:
        closing, mentions = split_hits(found.get(item) or [])
        if closing:
            covered += 1
            hits, label = closing, "COVERED"
        elif mentions:
            mentioned += 1
            hits, label = mentions, "MENTIONED"
        else:
            lines.append(f"UNCOVERED {item}")
            continue
        hit = hits[0]
        line = (
            f"{label} {item} open-pr=#{hit.get('pr')} "
            f"fork={'true' if hit.get('fork') else 'false'} author={hit.get('author')}"
        )
        if hit.get("unvouched"):
            line += " unvouched=true"
        if len(hits) > 1:
            line += f" also={','.join('#%s' % other.get('pr') for other in hits[1:])}"
        lines.append(line)
    lines.append(
        f"summary items={len(items)} covered={covered} mentioned={mentioned} "
        f"uncovered={len(items) - covered - mentioned} open-prs-read={pulls_read}"
    )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Batch open-PR coverage exclusion for a queue of work items."
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--items", required=True, help="comma/space separated numbers, or -")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    if not _REPO_RE.match(args.repo):
        print(f"malformed --repo {args.repo!r}: expected owner/name", file=sys.stderr)
        return 2
    stdin_text = sys.stdin.read() if args.items.strip() == "-" else ""
    items, items_error = parse_items(args.items, stdin_text)
    if items_error:
        print(items_error, file=sys.stderr)
        return 2

    pulls, error = open_pull_requests(args.repo)
    if error:
        # No exclusion was computed, so every candidate stays in the queue. The
        # `uncovered` list is deliberately ABSENT rather than empty or complete:
        # either shape would let an unanswered batch read as a finding about the
        # items, and this script's only finding is COVERED.
        if args.as_json:
            print(
                json.dumps(
                    {
                        "repo": args.repo,
                        "items": items,
                        "verdict": "UNKNOWN",
                        "reason": error,
                        "covered": {},
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"UNKNOWN items={len(items)} reason={error}")
        return 3

    found = coverage(args.repo, items, pulls)
    covered: dict[str, list[dict]] = {}
    mentioned: dict[str, list[dict]] = {}
    for item in items:
        closing, mentions = split_hits(found.get(item) or [])
        if closing:
            covered[str(item)] = closing
        elif mentions:
            mentioned[str(item)] = mentions
    if args.as_json:
        print(
            json.dumps(
                {
                    "repo": args.repo,
                    "items": items,
                    "verdict": "OK",
                    "covered": covered,
                    "mentioned": mentioned,
                    # Every item that is not COVERED, so a mention-only item is
                    # LISTED HERE as well as under `mentioned`. `uncovered` keeps
                    # its contract meaning -- "no coverage was found, it stays a
                    # candidate" -- which is what makes a consumer that subtracts
                    # `covered` inherit this fix without knowing `mentioned`
                    # exists. Redefining it as "no reference at all" would leave
                    # mention-only items out of both lists and silently restore
                    # the old suppression for every caller not updated in step.
                    "uncovered": [item for item in items if str(item) not in covered],
                    "open_prs_read": len(pulls),
                },
                sort_keys=True,
            )
        )
    else:
        for line in human_lines(items, found, len(pulls)):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
