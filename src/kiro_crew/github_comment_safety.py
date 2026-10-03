"""Refuse a GitHub comment whose raw markdown would start the Kiro Agent app.

**Core never rewrites comment markdown.** :func:`find_triggers` reports every
place a body holds the substring the app matches on, each with the replacement to
write instead, and :func:`assert_safe` raises with that list so the author
rewrites the body and sends it again. Nothing here returns a rewritten body.

Refusal is the whole guard because the Kiro Agent app (``kiro-agent[bot]``) starts
a Kiro Web session, and often a competing pull request, when someone whose GitHub
account is connected to Kiro Web writes an ISSUE comment whose RAW markdown holds
``/kiro`` in any case. The match is on the raw text -- link destinations, code
spans and fenced blocks included -- so a rewrite would have to change those bytes
while keeping GitHub's rendering identical, in every markdown container, and that
equivalence cannot be demonstrated. A rewrite that is wrong silently edits what a
crew said in public, where reporting the spans and letting the author pick the
wording holds by construction.

Stdlib only and free of I/O, so ``kirocrew gh-comment`` -- the command crews and
the conductor are told to write every GitHub issue comment with -- and the tests
share one implementation.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

__all__ = [
    "MAX_REPORTED",
    "TRIGGER",
    "Match",
    "UnsafeCommentError",
    "assert_safe",
    "describe",
    "find_triggers",
]

#: What the Kiro Agent app reacts to: the raw substring, in any case.
TRIGGER = re.compile(r"/kiro", re.IGNORECASE)

#: How many matches :func:`assert_safe` lists before it says how many remain. A
#: refusal is read in a terminal, so the list is a sample plus a count.
MAX_REPORTED = 5

#: What a URL or a repository path can carry. The token around a hit is grown over
#: this set so the suggestion names the whole link or path rather than five
#: characters out of the middle of one.
_TOKEN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._~:#?=&%+@-"
)
#: Trailing characters that end a sentence rather than a URL, trimmed off the token
#: the way GitHub's own autolinker trims them.
_TRAILING = ".,;:!?-"

_HOST = r"https?://(?:www\.)?github\.com/"
_SLUG = r"(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)"
#: ``.../commit/<sha>`` or ``.../pull/N/commits/<sha>``: a commit, not the pull
#: request it belongs to, so it is read before the issue/pull form.
_COMMIT_URL = re.compile(
    _HOST + _SLUG + r"/(?:commit|pull/\d+/commits)/(?P<sha>[0-9a-fA-F]{7,40})", re.IGNORECASE
)
_ITEM_URL = re.compile(_HOST + _SLUG + r"/(?:issues|pull)/(?P<number>\d+)\b", re.IGNORECASE)
_PATH_URL = re.compile(
    _HOST + _SLUG + r"/(?:blob|tree)/[^/]+/(?P<path>[^#?\s]*)(?:#L(?P<line>\d+))?", re.IGNORECASE
)
#: A source path as this repository spells it on disk; the comment form drops the
#: ``src/`` prefix. That removes the trigger from a path whose only slash before
#: ``kiro`` is the prefix itself. A path with a further ``/kiro`` segment inside it
#: keeps the substring, and :func:`_suggest` catches that and gives the advice.
_SRC_PREFIX = "src/"

_ADVICE = "write the path or URL without the slash before 'kiro'"


class UnsafeCommentError(ValueError):
    """The body holds the substring that starts the Kiro Agent app."""


@dataclass(frozen=True)
class Match:
    """One place a body would start the app, and what to write there instead.

    ``start``/``end`` are offsets into the body and span the whole URL or path
    around the hit, which is what ``token`` holds and what ``suggestion`` replaces.
    ``line`` is 1-based, so a refusal names the line the author has to edit.
    """

    start: int
    end: int
    line: int
    token: str
    suggestion: str


def _token_span(body: str, start: int, end: int) -> tuple[int, int]:
    """Grow ``body[start:end]`` out to the whole URL or path it sits inside."""
    while start > 0 and body[start - 1] in _TOKEN_CHARS:
        start -= 1
    while end < len(body) and body[end] in _TOKEN_CHARS:
        end += 1
    while end - 1 > start and body[end - 1] in _TRAILING:
        end -= 1
    return start, end


def _suggest(token: str, repo: str | None) -> str:
    """The replacement to write instead of *token*, or advice when none fits.

    Every candidate is read back through :data:`TRIGGER`. One that still holds the
    substring is no replacement at all -- a crew that wrote it would be refused
    again -- so it is dropped for the advice, which says what to do by hand.
    """
    candidate = _candidate(token, repo)
    if candidate is None or TRIGGER.search(candidate):
        return _ADVICE
    return candidate


def _candidate(token: str, repo: str | None) -> str | None:
    """The replacement *token*'s shape suggests, or ``None`` when no shape fits."""
    commit = _COMMIT_URL.match(token)
    if commit:
        return commit.group("sha")[:7]
    item = _ITEM_URL.match(token)
    if item:
        slug = f"{item.group('owner')}/{item.group('repo')}"
        if repo and slug.casefold() == repo.casefold():
            return f"#{item.group('number')}"
        # Another repository: GitHub links a cross-repository reference only in the
        # ``owner/repo#N`` form, so the whole slug carries it. A slug that holds the
        # substring itself is no replacement, and :func:`_suggest` drops it for the
        # advice.
        return f"{slug}#{item.group('number')}"
    path_url = _PATH_URL.match(token)
    if path_url:
        path = _drop_src(path_url.group("path"))
        line = path_url.group("line")
        return f"{path} line {line}" if line else path
    if token.startswith(_SRC_PREFIX):
        return _drop_src(token)
    return None


def _drop_src(path: str) -> str:
    return path[len(_SRC_PREFIX) :] if path.startswith(_SRC_PREFIX) else path


def find_triggers(body: str, repo: str | None = None) -> list[Match]:
    """Every place *body* would start the Kiro Agent app, in the order they appear.

    *repo* (``owner/name``) is the item's own repository, which lets an issue or
    pull-request URL into it be reported as the ``#N`` GitHub renders that URL as.
    A URL into any other repository is reported as ``owner/repo#N``, the one form
    GitHub links across repositories; without *repo* every such URL is read that
    way, and one whose slug holds the substring gets the advice instead.

    One :class:`Match` per URL or path, not per occurrence of the substring: a
    single link can hold it twice (its owner and a source path inside it), and a
    reader fixing that link needs one instruction for it.
    """
    if not TRIGGER.search(body):
        return []
    newlines = [m.start() for m in re.finditer(r"\n", body)]
    found: list[Match] = []
    covered = 0
    for hit in TRIGGER.finditer(body):
        if hit.start() < covered:
            continue
        start, end = _token_span(body, hit.start(), hit.end())
        covered = end
        token = body[start:end]
        found.append(
            Match(
                start=start,
                end=end,
                line=bisect.bisect_right(newlines, start) + 1,
                token=token,
                suggestion=_suggest(token, repo),
            )
        )
    return found


def describe(matches: list[Match]) -> str:
    """The refusal text for *matches*: one line each, capped, then the remainder."""
    head = (
        f"comment body would start the Kiro Agent GitHub app: {len(matches)} "
        + ("place holds" if len(matches) == 1 else "places hold")
        + " the trigger"
    )
    lines = [f"  line {m.line}: {m.token} -> {m.suggestion}" for m in matches[:MAX_REPORTED]]
    rest = len(matches) - len(lines)
    if rest:
        lines.append(f"  ... and {rest} more")
    return "\n".join([head, *lines])


def assert_safe(body: str, repo: str | None = None) -> None:
    """Raise :class:`UnsafeCommentError` if *body* would start the Kiro Agent app.

    The message lists each match with its line number, the URL or path, and the
    replacement to write instead, so the caller can rewrite the body itself. This
    module changes no byte of *body*.
    """
    matches = find_triggers(body, repo)
    if matches:
        raise UnsafeCommentError(describe(matches))
