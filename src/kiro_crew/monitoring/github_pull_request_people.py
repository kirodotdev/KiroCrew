"""Tell a person from a bot and from the author on a GitHub pull request's comment read.

A structured watch on a pull request that waits for review wakes when a PERSON
other than the pull request's author writes a comment or a review. The comment
read in ``github_pull_request`` therefore selects each body's author, and this
module turns one page of that read into fixed-width fingerprints of the bodies a
person wrote, and those into the digest the canonical observation carries.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from kiro_crew.monitoring.pull_request import comment_body_digest, comment_body_fingerprint

_NOBODY = hashlib.sha256(b"[]").hexdigest()


def person_fingerprints(node: Mapping[str, Any], *, with_reviews: bool) -> tuple[list[str], bool]:
    """Fingerprint each non-empty body on this page that a person other than the author wrote.

    A person is a ``User`` account other than the pull request's author, compared
    without case because GitHub logins are. A ``Bot`` is not. Neither is a row
    whose author is null: that is a deleted account, and reading it as malformed
    would silence every person on the pull request for as long as it exists.

    ``with_reviews`` reads the reviews connection too, which the first comment page
    carries alone and unpaged; a second page of reviews makes the read incomplete.
    The second value is False when the page cannot be trusted, so no digest is
    built from it.
    """
    author = node.get("author")
    login = author.get("login") if isinstance(author, Mapping) else None
    pr_author = login.casefold() if isinstance(login, str) else ""
    fingerprints: list[str] = []
    complete = True
    for name in ("comments", "reviews") if with_reviews else ("comments",):
        connection = node.get(name)
        if not isinstance(connection, Mapping) or not isinstance(connection.get("nodes"), list):
            return [], False
        rows = connection["nodes"]
        page_info = connection.get("pageInfo")
        if name == "reviews" and (
            not isinstance(page_info, Mapping) or page_info.get("hasNextPage") is not False
        ):
            complete = False
        for row in rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("body"), str):
                complete = False
                continue
            writer = row.get("author")
            if writer is None or not row["body"]:
                continue
            if not isinstance(writer, Mapping) or not isinstance(writer.get("login"), str):
                complete = False
                continue
            if writer.get("__typename") == "User" and writer["login"].casefold() != pr_author:
                fingerprints.append(comment_body_fingerprint(row["body"]))
    return fingerprints, complete


def person_comment_digest(fingerprints: list[str]) -> str:
    """``comment_body_digest``, but never empty.

    A read that found no person still yields one, so the first comment a person
    writes differs from it.
    """
    return comment_body_digest(fingerprints) or _NOBODY
