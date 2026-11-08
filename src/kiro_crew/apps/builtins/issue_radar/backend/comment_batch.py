"""Provider-neutral pieces of the timeline comment surface.

The timeline lets a maintainer write several comments and replies, hold them as a
pending batch, and submit them together, the way a forge's own "Start a review"
does. A batch item is ``{"body": str, "thread_id": str | None}``. ``thread_id`` is
the opaque reply handle the timeline reader put on a comment event:

* GitHub: the ROOT inline review comment's id. Only inline review comments thread
  on GitHub; a conversation comment has no thread and carries ``None``.
* GitLab: the note's discussion id, which every non-system note has.
* Azure DevOps: the pull-request thread id. Work item comments do not thread.

An item with no ``thread_id`` is a new top-level comment.

Each client decides how a batch reaches its forge. GitLab merge requests have a
native pending review (draft notes), so the whole batch publishes in one step. No
other surface has an equivalent this app can drive, and there :func:`post_sequentially`
posts the items in order. It stops at the first failure, because the items are
prose written to be read in order, and continuing past a failure would post the
later ones above a gap.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from .errors import (
    ProviderCliError,
    ProviderInvalidInputError,
    ProviderNotFoundError,
    ProviderPermissionError,
    ProviderSetupError,
)

# The two collections a timeline belongs to. Spelled out rather than inferred from
# the number because GitLab and Azure DevOps number issues and change requests
# independently, so the number alone does not say which item is meant.
TARGET_ISSUE = "issue"
TARGET_PULL = "pull"
TARGETS = (TARGET_ISSUE, TARGET_PULL)

# A batch is a review's worth of prose, not a bulk operation. The cap bounds how
# long one request can hold the provider's rate limit.
MAX_BATCH_ITEMS = 25

# The shape every provider's handle fits: GitHub and Azure ids are decimal, a
# GitLab discussion id is hex. Each client checks its own narrower form too.
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")


def thread_id_or_none(raw: object) -> str | None:
    """A normalized reply handle from a timeline row, or ``None``.

    The readers emit ``str`` ids so the frontend never compares a number from one
    provider with a string from another.
    """
    if raw is None or raw == "":
        return None
    text = str(raw)
    return text if THREAD_ID_RE.match(text) else None


def require_thread_id(thread_id: str, pattern: re.Pattern[str], provider_name: str) -> str:
    """``thread_id`` checked against one provider's own id form."""
    text = (thread_id or "").strip()
    if not pattern.match(text):
        raise ProviderCliError(f"not a {provider_name} reply thread: {thread_id!r}")
    return text


# Errors that are the forge's own answer that it refused the write, so nothing
# was posted. Any other failure (a timeout, a 5xx, a dropped connection) may have
# reached the forge after all.
_DEFINITE_REFUSALS = (
    ProviderSetupError,
    ProviderPermissionError,
    ProviderNotFoundError,
    ProviderInvalidInputError,
)
_CLIENT_ERROR_STATUS_RE = re.compile(r"\bHTTP 4\d\d\b")


def delivery_unknown(exc: ProviderCliError) -> bool:
    """Whether a failed write may still have been posted.

    Only a refusal the forge sent back proves it was not: a typed refusal, or an
    HTTP 4xx status in the CLI's error. A timeout, a 5xx or an error with no status
    can follow a write the forge already accepted.
    """
    if isinstance(exc, _DEFINITE_REFUSALS):
        return False
    return not _CLIENT_ERROR_STATUS_RE.search(str(exc))


def post_sequentially(
    items: list[dict],
    post_one: Callable[[dict], dict],
) -> dict:
    """Post ``items`` one at a time, in order, stopping at the first failure.

    A failure the forge refused (see ``delivery_unknown``) on the FIRST item means
    nothing was posted, and the exception is re-raised so the route maps it like
    any single-comment failure. A later refusal returns what did post plus the
    failure, because those comments are already on the forge and the caller must
    not resend them. A failure that may have been delivered returns that item as
    ``unknown``, wherever it falls, so the caller does not send it again either.
    """
    posted: list[dict] = []
    for index, item in enumerate(items):
        try:
            result = post_one(item)
        except ProviderCliError as exc:
            unknown = delivery_unknown(exc)
            if not posted and not unknown:
                raise
            return {
                "posted": posted,
                "failed": {"index": index, "error": str(exc)},
                "unknown": [index] if unknown else [],
            }
        posted.append({"index": index, "id": result.get("id"), "url": result.get("url")})
    return {"posted": posted, "failed": None}
