"""Paging and fingerprint readers for the GitHub pull-request monitor.

Moved whole out of ``github_pull_request`` to hold that adapter under the
2,000-line cap (AGENTS.md). The original path stays the import path callers
use: it re-imports the names its own code calls, and tests reach for the rest
here, at the defining module.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def _page_cursor(page_info: object) -> tuple[bool, str | None]:
    """Read one page's continuation, refusing a shape that cannot be trusted."""
    if not isinstance(page_info, Mapping):
        raise ValueError("GitHub pagination is malformed")
    has_next = page_info.get("hasNextPage")
    if has_next is False:
        return False, None
    if has_next is not True:
        raise ValueError("GitHub pagination is malformed")
    cursor = page_info.get("endCursor")
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("GitHub pagination is malformed")
    return True, cursor


def _body_fingerprint(body: str) -> str:
    """A FIXED-LENGTH stand-in for one externally-authored comment body.

    This is where the bound lives. A comment body is unbounded third-party text
    and a paged read accumulates one entry per comment across every page, so
    retaining the body itself would let a single large comment, or many of them,
    size the probe's own memory. Hashing at the moment of retention makes what is
    kept 64 characters wide whatever arrives, and nothing downstream ever sees
    the body again -- neither the aggregate digest nor the condition key.

    Only the digest of the body is ever needed: the conditions built from these
    answer "did this change", never "what does it say".
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _review_thread_page(
    node: Mapping[str, Any],
) -> tuple[int, bool, bool, str | None]:
    """Count one page's unresolved, non-outdated review threads.

    A malformed thread marks the page incomplete so the caller does not trust a
    count built from a partial read.
    """
    threads = node["reviewThreads"]
    if not isinstance(threads, Mapping):
        raise ValueError("GitHub review threads are malformed")
    nodes = threads.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("GitHub review threads are malformed")
    unresolved = 0
    nodes_complete = True
    for thread in nodes:
        if (
            not isinstance(thread, Mapping)
            or not isinstance(thread.get("isResolved"), bool)
            or not isinstance(thread.get("isOutdated"), bool)
        ):
            nodes_complete = False
            continue
        if thread["isResolved"] or thread["isOutdated"]:
            continue
        unresolved += 1
    has_next, cursor = _page_cursor(threads.get("pageInfo"))
    return unresolved, nodes_complete, has_next, cursor


def _pr_comment_activity_page(
    node: Mapping[str, Any],
    pr_author_login: str,
) -> tuple[list[str], list[str], bool, bool, bool, str | None, str | None]:
    """Read body and human-activity fingerprints from one issue-comment page."""
    comments = node["comments"]
    nodes, has_next, cursor, end = _activity_connection_page(comments, "comments")
    bodies: list[str] = []
    humans: list[str] = []
    nodes_complete = True
    authors_complete = True
    for comment in nodes:
        if not isinstance(comment, Mapping) or not isinstance(comment.get("body"), str):
            nodes_complete = False
            authors_complete = False
            continue
        body = comment["body"]
        if body:
            bodies.append(_body_fingerprint(body))
        fingerprint, actor_complete = _human_activity_fingerprint(
            comment.get("author"), pr_author_login, kind="comment", body=body
        )
        authors_complete &= actor_complete
        if fingerprint is not None:
            humans.append(fingerprint)
    return bodies, humans, nodes_complete, authors_complete, has_next, cursor, end


def _pr_review_activity_page(
    node: Mapping[str, Any],
    pr_author_login: str,
) -> tuple[list[str], bool, bool, bool, str | None, str | None]:
    """Read one page of review submissions, including body edits and verdicts."""
    reviews = node["reviews"]
    nodes, has_next, cursor, end = _activity_connection_page(reviews, "reviews")
    humans: list[str] = []
    nodes_complete = True
    authors_complete = True
    for review in nodes:
        if (
            not isinstance(review, Mapping)
            or not isinstance(review.get("body"), str)
            or not isinstance(review.get("state"), str)
        ):
            nodes_complete = False
            authors_complete = False
            continue
        fingerprint, actor_complete = _human_activity_fingerprint(
            review.get("author"),
            pr_author_login,
            kind="review",
            body=review["body"],
            state=review["state"],
        )
        authors_complete &= actor_complete
        if fingerprint is not None:
            humans.append(fingerprint)
    return humans, nodes_complete, authors_complete, has_next, cursor, end


def _activity_connection_page(
    raw: object,
    label: str,
) -> tuple[list[object], bool, str | None, str | None]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"GitHub pull request {label} are malformed")
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError(f"GitHub pull request {label} are malformed")
    has_next, cursor = _page_cursor(raw.get("pageInfo"))
    page_info = raw.get("pageInfo")
    end_cursor = page_info.get("endCursor") if isinstance(page_info, Mapping) else None
    if end_cursor is not None and (not isinstance(end_cursor, str) or not end_cursor):
        raise ValueError("GitHub pagination is malformed")
    return nodes, has_next, cursor, end_cursor


def _human_activity_fingerprint(
    actor: object,
    pr_author_login: str,
    *,
    kind: str,
    body: str,
    state: str = "",
) -> tuple[str | None, bool]:
    """Fingerprint eligible human activity without retaining provider prose."""
    login = actor.get("login") if isinstance(actor, Mapping) else ""
    actor_type = actor.get("__typename") if isinstance(actor, Mapping) else ""
    if (
        not isinstance(login, str)
        or not login
        or not isinstance(actor_type, str)
        or not actor_type
    ):
        # A deleted (or otherwise unreadable) account is still activity somebody
        # left on the pull request. Fingerprint it UNATTRIBUTED rather than
        # failing the whole page: returning incomplete here would blank the
        # activity digest on every later read, so one deleted account would
        # silence person wakes for the pull request's lifetime. The fingerprint
        # stays fixed-width and keyed by an empty login, so a real person's
        # later comment still moves the digest and wakes.
        payload = json.dumps(
            [kind, "", state, _body_fingerprint(body)],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest(), True
    if not pr_author_login:
        # The PR author's own login is unreadable, so self-activity cannot be
        # told from other people's: fail closed for the attributed digests.
        return None, False
    if (
        actor_type != "User"
        or login.casefold().endswith("[bot]")
        or login.casefold() == pr_author_login.casefold()
    ):
        return None, True
    payload = json.dumps(
        [kind, login.casefold(), state, _body_fingerprint(body)],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest(), True


def _pr_comment_body_digest(fingerprints: list[str]) -> str:
    """A stable digest over a pull request's PR-level comment bodies.

    Takes per-comment FINGERPRINTS, never bodies, so nothing on the path from
    read to condition key scales with how much a reviewer wrote.

    Empty when no readable non-empty body was seen, so a pull request with no
    comments emits no condition. Fingerprints are SORTED before hashing so the
    page order they arrived in cannot move the digest, mirroring the thread
    digest.
    """
    kept = sorted(fingerprints)
    if not kept:
        return ""
    encoded = json.dumps(kept, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
