"""Timeline comments: post a batch of comments and replies on one issue or PR.

``POST /api/apps/issue-radar/comments``::

    {"owner", "repo", "provider"?, "host"?, "number",
     "target": "issue" | "pull",
     "items": [{"body": str, "thread_id": str | null}, ...]}

    -> {owner, repo, provider, host, number, target,
        posted: [{index, id, url}], failed: {index, error} | null}

The detail pane's timeline composer is the only caller. A single comment is a batch
of one, so posting now and submitting a pending batch are the same request.

A forge write, so it runs the same gate chain as every pull-request mutation
(``_pr_action_preamble``: dashboard owner, JSON, owner/repo, connected repo,
confirmed triage/push access). How a batch reaches the forge is each client's
``post_comment_batch``: a GitLab merge request publishes it as one review, every
other surface posts the items in order and stops at the first failure. A failure
after some items posted is a 200 that names the failed index, because the earlier
items are already on the forge and the caller must not send them again.
"""

from __future__ import annotations

import asyncio
import contextlib
from functools import partial

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend import comment_batch, provider, store


def _items_field(body: dict) -> tuple[list[dict], web.Response | None]:
    """The validated batch from the request: 1..MAX items, each a bounded body and
    an optional reply handle."""
    from .. import routes  # circular import: backend.routes imports this module

    def bad(message: str, code: str) -> tuple[list[dict], web.Response]:
        return [], web.json_response({"error": message, "code": code}, status=400)

    raw = body.get("items")
    if not isinstance(raw, list) or not raw:
        return bad("'items' must be a non-empty list", "items_required")
    if len(raw) > comment_batch.MAX_BATCH_ITEMS:
        return bad(
            f"too many comments in one batch (max {comment_batch.MAX_BATCH_ITEMS})",
            "too_many_items",
        )
    items: list[dict] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            return bad(f"items[{index}] must be an object", "invalid_item")
        text, too_long = routes._pr_body_field(entry)
        if too_long is not None:
            return [], too_long
        if not text.strip():
            return bad(f"items[{index}] needs a body", "body_required")
        thread = entry.get("thread_id")
        if thread is not None and not (
            isinstance(thread, str) and comment_batch.THREAD_ID_RE.match(thread)
        ):
            return bad(f"items[{index}] has an invalid 'thread_id'", "invalid_thread_id")
        items.append({"body": text.strip(), "thread_id": thread or None})
    return items, None


def _drop_detail_cache(key: provider.RepoKey, target: str, number: int) -> None:
    """Invalidate the item's cached detail so the next read shows the new comments.

    OSError is suppressed: failing to invalidate a cache must not turn a write that
    succeeded into an error.
    """
    from .. import routes  # circular import: backend.routes imports this module

    root = routes._scope(key)
    with contextlib.suppress(OSError):
        if target == comment_batch.TARGET_PULL:
            store.drop_pr_detail_cache(key.owner, key.repo, number, root=root)
        else:
            store.issue_detail_cache_path(key.owner, key.repo, number, root).unlink(missing_ok=True)


async def _handle_comments(request: web.Request) -> web.Response:
    """POST /comments — see the module docstring."""
    from .. import routes  # circular import: backend.routes imports this module

    body, key, early = await routes._pr_action_preamble(request, "comments")
    if early is not None:
        return early

    number, number_error = routes._pr_number_field(body)
    if number_error is not None:
        return number_error
    target = body.get("target")
    if target not in comment_batch.TARGETS:
        return web.json_response(
            {"error": "'target' must be 'issue' or 'pull'", "code": "invalid_target"},
            status=400,
        )
    items, items_error = _items_field(body)
    if items_error is not None:
        return items_error

    ref = f"{key.slug}#{number}"
    client = provider.client_for(key)
    try:
        result = await asyncio.to_thread(
            partial(
                client.post_comment_batch,
                key.owner,
                key.repo,
                number,
                target,
                items,
                **provider.call_kwargs(key),
            )
        )
    except routes.GhCliError as exc:
        return routes._pr_action_error("comments", ref, exc)

    await asyncio.to_thread(partial(_drop_detail_cache, key, target, number))
    failed = result.get("failed")
    posted = result.get("posted") or []
    # A partial batch is audited as a failure that names how far it got, so a query
    # for failed writes finds it; the posted count is in the resource either way.
    routes._audit(
        "comments",
        f"{ref}:{len(posted)}/{len(items)}",
        "failure" if failed else "ok",
        error=str(failed.get("error") or "") if isinstance(failed, dict) else "",
    )
    return web.json_response(
        {
            **routes._identity(key),
            "number": number,
            "target": target,
            "posted": posted,
            "failed": failed,
            # Items whose delivery could not be confirmed either way: the client
            # drops them from the batch rather than risk posting them twice.
            "unknown": result.get("unknown") or [],
        }
    )
