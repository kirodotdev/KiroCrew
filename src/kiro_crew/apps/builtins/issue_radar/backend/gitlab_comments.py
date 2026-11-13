"""GitLab timeline comments: discussion reads, replies and batched posting.

``gitlab_client`` re-exports :func:`reply_to_comment_thread` and
:func:`post_comment_batch` and stays the import path callers use; this module
keeps it under the file line cap. Every ``glab`` call goes through
``gitlab_client._glab_api`` looked up at call time (passed in as ``api``, or via
:func:`_client`), so a test that patches it there intercepts every request made
here.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from types import ModuleType

from . import comment_batch
from .errors import ProviderCliError
from .gitlab_normalization import _obj, _rows

GlabApi = Callable[..., object]


def _client() -> ModuleType:
    """``gitlab_client``, imported at call time because it imports this module."""
    from . import gitlab_client

    return gitlab_client


def reply_to_comment_thread(
    owner: str,
    repo: str,
    number: int,
    target: str,
    thread_id: str,
    body: str,
    *,
    host: str = "",
    timeout: float | None = None,
) -> dict:
    """Reply inside a discussion on an issue or merge request.

    ``thread_id`` is the discussion id the timeline reader put on the note. GitLab
    accepts a reply to a single, unthreaded note too and turns it into a thread,
    which is what its own web UI's Reply does. ``timeout`` defaults to
    ``gitlab_client.GL_TIMEOUT_SEC``.
    """
    gl = _client()
    discussion = comment_batch.require_thread_id(thread_id, DISCUSSION_ID_RE, "GitLab")
    text = (body or "").strip()
    if not text:
        raise ProviderCliError("a comment needs a body")
    data = _obj(
        gl._glab_api(
            f"projects/{gl.project_path(owner, repo)}/{collection(target)}/{int(number)}"
            f"/discussions/{discussion}/notes",
            host=host,
            timeout=gl.GL_TIMEOUT_SEC if timeout is None else timeout,
            method="POST",
            body={"body": text},
        )
    )
    return {"id": data.get("id"), "url": None, "created_at": data.get("created_at")}


def post_comment_batch(
    owner: str,
    repo: str,
    number: int,
    target: str,
    items: list[dict],
    *,
    host: str = "",
    timeout: float | None = None,
) -> dict:
    """Post a batch of timeline comments and replies.

    On a merge request, two or more items are published as one GitLab review (see
    :func:`post_mr_batch_as_review`). A single item is posted directly: a review of
    one comment notifies the same way, for a quarter of the API calls. Issues have
    no draft notes, so an issue batch posts its items one after another.
    ``timeout`` defaults to ``gitlab_client.GL_TIMEOUT_SEC``.
    """
    gl = _client()
    wait = gl.GL_TIMEOUT_SEC if timeout is None else timeout
    kind = collection(target)
    if kind == "merge_requests" and len(items) > 1:
        base = f"projects/{gl.project_path(owner, repo)}/merge_requests/{int(number)}"
        return post_mr_batch_as_review(gl._glab_api, base, items, host=host, timeout=wait)

    def post_one(item: dict) -> dict:
        thread = item.get("thread_id")
        if thread:
            return gl.reply_to_comment_thread(
                owner, repo, number, target, thread, item["body"], host=host, timeout=wait
            )
        post = gl.add_pr_comment if kind == "merge_requests" else gl.add_issue_comment
        return post(owner, repo, number, item["body"], host=host, timeout=wait)

    return comment_batch.post_sequentially(items, post_one)


# A GitLab discussion id is a hex SHA-1 today; the bounds leave room for a longer
# digest without accepting anything that could carry a path segment.
DISCUSSION_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")


def collection(target: str) -> str:
    """The GitLab collection (``merge_requests`` or ``issues``) for a comment target."""
    if target == comment_batch.TARGET_PULL:
        return "merge_requests"
    if target == comment_batch.TARGET_ISSUE:
        return "issues"
    raise ProviderCliError(f"invalid comment target: {target!r}")


def discussion_notes(api: GlabApi, base: str, *, host: str, timeout: float) -> list[dict]:
    """Every note on an issue or merge request, each tagged with its discussion.

    Read from ``{base}/discussions`` rather than ``{base}/notes``: the two carry the
    same notes (system notes included), but only the discussions endpoint says which
    discussion a note is in, and a reply has to be posted into that discussion. Each
    note gets its discussion's id as ``discussion_id``. The result is sorted
    oldest-first, the order the notes endpoint served.
    """
    discussions = _rows(api(f"{base}/discussions", host=host, timeout=timeout, paginate=True))
    notes: list[dict] = []
    for discussion in discussions:
        discussion_id = discussion.get("id")
        for note in _rows(discussion.get("notes")):
            notes.append({**note, "discussion_id": discussion_id})
    notes.sort(key=lambda n: str(n.get("created_at") or ""))
    return notes


def _draft_ids(api: GlabApi, base: str, *, host: str, timeout: float) -> set[int]:
    """Ids of the caller's own pending draft notes on a merge request.

    Draft notes are visible only to their author, so this is the signed-in user's
    pending review and nobody else's.
    """
    rows = _rows(api(f"{base}/draft_notes", host=host, timeout=timeout, paginate=True))
    return {int(row["id"]) for row in rows if isinstance(row.get("id"), int)}


def _discard_drafts(
    api: GlabApi,
    base: str,
    ids: list[int],
    *,
    host: str,
    timeout: float,
) -> None:
    """Delete drafts this batch created.

    Best effort, because the caller is already failing and re-raises the original
    error. A failed delete is not silent: the transport records every ``glab`` call
    in the SEL, and the draft stays visible in GitLab's own pending-review panel.
    """
    for draft_id in ids:
        with contextlib.suppress(ProviderCliError):
            api(f"{base}/draft_notes/{int(draft_id)}", host=host, timeout=timeout, method="DELETE")


def post_mr_batch_as_review(
    api: GlabApi,
    base: str,
    items: list[dict],
    *,
    host: str,
    timeout: float,
) -> dict:
    """Post a merge-request batch as ONE GitLab review: drafts, then one publish.

    ``base`` is the merge request's API path. This is GitLab's own "Start a
    review": every comment and reply is created as a draft note, then published
    together, so reviewers get the batch as one review rather than one
    notification per comment. It is all-or-nothing up to the publish step: if
    creating any draft fails, the drafts this batch already created are deleted
    and nothing is posted.

    The user may ALREADY have a pending review on this merge request, started in
    GitLab's web UI. ``bulk_publish`` publishes every pending draft, so using it
    then would post that unrelated, unfinished review too. So the bulk publish is
    used only when the pending drafts are exactly this batch's, checked both
    before the batch starts and immediately before publishing; otherwise each of
    this batch's drafts is published by id and the user's own drafts stay pending.

    When a publish fails and the pending drafts then cannot be read, the drafts
    not confirmed published are returned as ``unknown`` and left in place, never
    retried or deleted.
    """
    # Every payload is built, and every reply handle checked, before the first
    # draft exists, so a malformed item cannot leave earlier drafts behind.
    payloads: list[dict] = []
    for item in items:
        payload: dict = {"note": item["body"]}
        if item.get("thread_id"):
            payload["in_reply_to_discussion_id"] = comment_batch.require_thread_id(
                item["thread_id"], DISCUSSION_ID_RE, "GitLab"
            )
        payloads.append(payload)
    preexisting = _draft_ids(api, base, host=host, timeout=timeout)

    created: list[int] = []
    for payload in payloads:
        try:
            data = _obj(
                api(f"{base}/draft_notes", host=host, timeout=timeout, method="POST", body=payload)
            )
        except ProviderCliError:
            _discard_drafts(api, base, created, host=host, timeout=timeout)
            raise
        draft_id = data.get("id")
        if not isinstance(draft_id, int):
            _discard_drafts(api, base, created, host=host, timeout=timeout)
            raise ProviderCliError("GitLab did not return an id for a draft note")
        created.append(draft_id)

    # Drafts confirmed published one by one. Recorded as each call succeeds, so a
    # later failure that also breaks reconciliation still reports them as posted
    # and a retry never sends them twice.
    confirmed: list[int] = []
    try:
        exclusive = not preexisting and _draft_ids(api, base, host=host, timeout=timeout) == set(
            created
        )
        if exclusive:
            api(f"{base}/draft_notes/bulk_publish", host=host, timeout=timeout, method="POST")
        else:
            for draft_id in created:
                api(
                    f"{base}/draft_notes/{int(draft_id)}/publish",
                    host=host,
                    timeout=timeout,
                    method="PUT",
                )
                confirmed.append(draft_id)
    except ProviderCliError as exc:
        # A failed publish can leave some of this batch posted and the rest pending.
        # What is still pending is discarded, so a half-published batch does not
        # linger as a review the user never sees in this app, and the items that
        # did land are reported as posted so the client never sends them again.
        try:
            still_pending = _draft_ids(api, base, host=host, timeout=timeout) & set(created)
        except ProviderCliError:
            # Whether the drafts not confirmed one by one were published cannot be
            # told. Retrying them could post them twice, and deleting them could
            # lose ones that did not publish, so they are left as drafts and
            # reported as unknown: one that did not publish stays in the user's
            # pending review on GitLab, and the client sends none of them again.
            unknown = [i for i, draft_id in enumerate(created) if draft_id not in confirmed]
            return {
                "posted": [
                    {"index": i, "id": None, "url": None}
                    for i, draft_id in enumerate(created)
                    if draft_id in confirmed
                ],
                "failed": {"index": unknown[0], "error": str(exc)},
                "unknown": unknown,
            }
        _discard_drafts(api, base, sorted(still_pending), host=host, timeout=timeout)
        published = [i for i, draft_id in enumerate(created) if draft_id not in still_pending]
        if not published:
            raise
        unposted = [i for i, draft_id in enumerate(created) if draft_id in still_pending]
        return {
            "posted": [{"index": i, "id": None, "url": None} for i in published],
            # Every draft published although the call reported a failure: no failure.
            "failed": {"index": unposted[0], "error": str(exc)} if unposted else None,
        }
    return {
        "posted": [{"index": i, "id": None, "url": None} for i in range(len(items))],
        "failed": None,
    }
