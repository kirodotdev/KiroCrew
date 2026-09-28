"""``/investigation``: the local per-item record behind the Investigate button.

Purely local triage state -- the linked chat session, status and findings --
so nothing is written to the provider and there is no permission gate. The PUT is
also the write behind the ``issue_radar_record_investigation`` MCP tool.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend import provider, store

# ── investigation records (the "Investigate" button) ────────────────────────
#
# "Investigate" opens a Kiro Crew chat session (seeded with an investigation
# prompt, filed into the per-repo "Issue Radar - <repo>" chat folder) entirely
# from the frontend — those are core chat routes, not this app's. These two
# routes only persist the LOCAL per-issue record that links the session so a
# repeat click resumes it, badges status, and retains findings. No shared
# ledger, no GitHub write.


def _item_kind(raw: object) -> str | None:
    """Validate an item-kind field (``issue`` / ``pull``), defaulting to ``issue``.

    ``None`` means invalid, so the caller answers 400 rather than silently reading
    the wrong record: on GitLab the kind is part of an item's identity, and
    quietly falling back to "issue" would resume the wrong session.
    """
    if raw is None or raw == "":
        return "issue"
    return raw if isinstance(raw, str) and raw in provider.ITEM_KINDS else None


async def _handle_get_investigation(request: web.Request) -> web.Response:
    """GET /investigation?owner=<o>&repo=<r>&number=<n>[&kind=issue|pull] — the
    local investigation record for one item (session link + status + findings), or
    ``null`` when it has never been investigated. Read-only, no permission gate.

    ``kind`` defaults to ``issue`` and only changes the answer on GitLab, where
    issues and merge requests have independent number sequences."""
    from .. import routes  # circular import: backend.routes imports this module

    key = routes._key_from_request(request)
    owner, repo = key.owner, key.repo
    number_raw = (request.query.get("number") or "").strip()
    if not owner or not repo or not number_raw:
        return web.json_response({"error": "missing ?owner=, ?repo= and ?number="}, status=400)
    number, number_error = routes._parse_item_number(number_raw)
    if number_error is not None:
        return number_error

    if not await asyncio.to_thread(routes._connected, key):
        return web.json_response(
            {"error": f"{owner}/{repo} is not connected — call /connect first"},
            status=404,
        )

    item_kind = _item_kind(request.query.get("kind"))
    if item_kind is None:
        return web.json_response({"error": "'kind' must be 'issue' or 'pull'"}, status=400)

    record = await routes._st(
        key,
        store.read_investigation,
        owner,
        repo,
        number,
        kind=provider.investigation_kind(key, item_kind),
    )
    return web.json_response(
        {
            **routes._identity(key),
            "number": number,
            "kind": item_kind,
            "investigation": record,
        }
    )


async def _handle_put_investigation(request: web.Request) -> web.Response:
    """PUT /investigation {"owner","repo","number", kind?, slot_key?, folder_id?,
    status?, findings?} — upsert one item's investigation record.

    ``kind`` (``issue`` / ``pull``, default ``issue``) is part of the record's
    identity on GitLab, where a merge request's number is drawn from a different
    sequence than an issue's. An agent PUT that omits it therefore addresses the
    ISSUE with that number -- which is why the seed prompts emit it (see
    ``lib/links.ts:recordIdentityJson``).

    Called by the Investigate button to link the freshly-created chat session
    (``slot_key`` + ``folder_id``), and again on resume to bump the "last opened"
    stamp; the investigating agent (or the user) may also PUT a ``findings``
    summary when a conclusion is reached. The body is MERGED into any existing
    record and normalized server-side (unknown keys dropped, ``status``
    constrained, ``findings`` coerced), so a partial patch — even ``{}`` — is
    valid. Purely local triage state; nothing is written to GitHub."""
    from .. import routes  # circular import: backend.routes imports this module

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "request body must be JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "request body must be a JSON object"}, status=400)

    key = routes._key_from_body(body)
    owner, repo = key.owner, key.repo
    number = body.get("number")
    if not owner or not repo:
        return web.json_response({"error": "missing 'owner'/'repo'"}, status=400)
    # bool is a subclass of int: JSON `true` would otherwise validate as #1.
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return web.json_response({"error": "'number' must be a positive integer"}, status=400)
    # Same upper bound the ?number= routes enforce via _parse_item_number. It
    # matters more on this write than on a read: the number becomes part of the
    # record's FILENAME (investigation-<n>.json), so an absurd value is an
    # ENAMETOOLONG write rather than just a miss.
    if number > routes.MAX_ITEM_NUMBER:
        return web.json_response(
            {
                "error": f"number must be at most {routes.MAX_ITEM_NUMBER}",
                "code": "item_number_out_of_range",
            },
            status=400,
        )

    if not await asyncio.to_thread(routes._connected, key):
        return web.json_response(
            {"error": f"{owner}/{repo} is not connected — call /connect first"},
            status=404,
        )

    item_kind = _item_kind(body.get("kind"))
    if item_kind is None:
        return web.json_response({"error": "'kind' must be 'issue' or 'pull'"}, status=400)

    patch = {k: body[k] for k in ("slot_key", "folder_id", "status", "findings") if k in body}
    saved = await routes._st(
        key,
        store.write_investigation,
        owner,
        repo,
        number,
        patch,
        kind=provider.investigation_kind(key, item_kind),
    )
    return web.json_response(
        {
            **routes._identity(key),
            "number": number,
            "kind": item_kind,
            "investigation": saved,
        }
    )
