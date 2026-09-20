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

  * ``COVERED`` is a positive finding - a reference to the item in a pull
    request's own title or body - and the item leaves the queue.
  * ``UNCOVERED`` is NOT permission and certifies nothing. A reference made in a
    PR COMMENT appears in the item's timeline and not here, so this script's
    silence is a smaller view rather than a clean bill, and
    ``claim_preflight.py`` still runs before every claim.

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

    0   answered - read the ``covered`` / ``uncovered`` split
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
  * A DRAFT pull request counts as coverage, and so does a fork PR. Both are
    work in flight, and both are what ``claim_preflight.py``'s check 1 counts:
    the two rules answer the same question from different evidence, so a
    difference in what they count would be drift rather than nuance.
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


def item_reference_re(repo: str, item: int) -> re.Pattern[str]:
    """A pattern matching a reference to THIS item, keyword or not.

    The three spellings GitHub itself links on: ``#N``, ``owner/repo#N``, and the
    full issue URL. ``\\b`` after the number stops ``#12`` matching ``#123``.

    Deliberately NOT keyed on a closing keyword, and that is the whole difference
    from ``claim_preflight.py``'s :func:`closing_reference_re`. There, a closing
    keyword is required because the verdict it feeds is CLOSE - the strongest
    answer that script has - and a PR that merely mentions an item has not
    claimed to finish it. Here the verdict is a queue subtraction, and check 2 of
    that same script already SKIPs on any open PR the item's timeline references,
    keyword or not. Requiring a keyword would make this filter admit an item that
    the preflight then refuses, which is the rediscovery it exists to remove.

    The bare ``#N`` form additionally declines a number carrying a QUALIFIER -
    ``otherowner/otherrepo#N``, ``v1.2#N`` - because the only way a subtractive
    filter can be wrong is a FALSE subtraction, and that is a silent denial of
    work on an item nobody is fixing. ``owner/repo#N`` for THIS repository is
    still matched, by its own alternative, and that alternative carries the SAME
    left boundary: without it a lookalike owner whose name merely ENDS in this
    one (``fakeowner/repo#N``) would match by substring, which is the same false
    subtraction arriving through the other door. The lookbehind is one character
    wide, which is what Python's ``re`` allows.
    """
    owner_repo = re.escape(repo)
    return re.compile(
        rf"(?:(?<![\w./-])#{item}\b"
        rf"|(?<![\w./-]){owner_repo}#{item}\b"
        rf"|https?://github\.com/{owner_repo}/issues/{item}\b)",
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
    the searchable text. The text is read here and never printed.
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
                "text": f"{entry.get('title') or ''}\n{entry.get('body') or ''}",
            }
        )
    return pulls, None


def coverage(repo: str, items: list[int], pulls: list[dict]) -> dict[int, list[dict]]:
    """item -> the open PRs referencing it, in PR order. Pure: no forge, no git.

    Every item gets a key, so a caller reading this mapping cannot mistake an
    absent key for an unscanned item.
    """
    found: dict[int, list[dict]] = {item: [] for item in items}
    patterns = [(item, item_reference_re(repo, item)) for item in items]
    for pull in pulls:
        text = str(pull.get("text") or "")
        if not text:
            continue
        for item, pattern in patterns:
            if not pattern.search(text):
                continue
            association = str(pull.get("author_association") or "")
            found[item].append(
                {
                    "pr": pull.get("number"),
                    "fork": bool(pull.get("is_cross_repository")),
                    "author": pull.get("author"),
                    # Annotation only: the subtraction happens either way, for
                    # the reason INSIDER_ASSOCIATIONS gives. What this buys is
                    # that the one suppression worth a look does not read
                    # identically to the hundred that are routine.
                    "unvouched": bool(pull.get("is_cross_repository"))
                    and association not in INSIDER_ASSOCIATIONS,
                }
            )
    return found


def human_lines(items: list[int], found: dict[int, list[dict]], pulls_read: int) -> list[str]:
    """The human form: one line per item, then one summary line.

    Field names are the contract's and values are metadata only - never
    user-authored text.
    """
    lines: list[str] = []
    covered = 0
    for item in items:
        hits = found.get(item) or []
        if not hits:
            lines.append(f"UNCOVERED {item}")
            continue
        covered += 1
        hit = hits[0]
        line = (
            f"COVERED {item} open-pr=#{hit.get('pr')} "
            f"fork={'true' if hit.get('fork') else 'false'} author={hit.get('author')}"
        )
        if hit.get("unvouched"):
            line += " unvouched=true"
        if len(hits) > 1:
            line += f" also={','.join('#%s' % other.get('pr') for other in hits[1:])}"
        lines.append(line)
    lines.append(
        f"summary items={len(items)} covered={covered} "
        f"uncovered={len(items) - covered} open-prs-read={pulls_read}"
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
    if args.as_json:
        print(
            json.dumps(
                {
                    "repo": args.repo,
                    "items": items,
                    "verdict": "OK",
                    "covered": {str(item): hits for item, hits in found.items() if hits},
                    "uncovered": [item for item in items if not found.get(item)],
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
