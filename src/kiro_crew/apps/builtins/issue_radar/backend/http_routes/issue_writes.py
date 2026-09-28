"""Writes on one issue: labels (``/labels/apply``), state (``/issue/state``) and assignees.

Each is gated on confirmed triage/push access and SEL-audited. A label change and
an assignee replacement each hold the per-issue write lock across the provider
calls and the cache patch, so two writers to one issue cannot land out of order.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend import provider, store

logger = logging.getLogger("kirocrew.app.issue-radar")


def _apply_label_change(
    key: provider.RepoKey, number: int, add: list[str], remove: list[str]
) -> list[dict] | None:
    """Apply one issue's whole label change and patch the caches, serialized.

    Runs in a worker thread with the per-issue write lock held across EVERY step:
    the removals, the additions, and the cache patch. Splitting them lets two
    concurrent changes to the same issue land their authoritative responses out of
    order — so a removal that started before an addition can patch its older label
    set over the addition's, and the added label disappears from the cache until
    the next refresh papers over it.

    Returns the issue's authoritative label set, or ``None`` when every operation
    was a no-op removal (GitHub 404s a label that is not on the issue and the
    remaining set is then unknown) so the caller can re-read it. GitLab always
    reports the authoritative set, so that path is GitHub-only in practice.

    Cache failures are logged, never raised: the change is already applied on the
    provider, and reporting the apply as failed would send the user to redo it."""
    from .. import routes  # circular import: backend.routes imports this module

    owner, repo = key.owner, key.repo
    client = provider.client_for(key)
    pkw = provider.call_kwargs(key)
    scope = routes._scope(key)
    with store.issue_write_lock(owner, repo, number, scope):
        final_labels: list[dict] | None = None
        for name in remove:
            result = client.remove_issue_label(owner, repo, number, name, **pkw)
            if result is not None:
                final_labels = result
        if add:
            final_labels = client.add_issue_labels(owner, repo, number, add, **pkw)
        if final_labels is None:
            # Every removal 404'd (the labels were already absent), so GitHub told
            # us nothing about the remaining set. Read it authoritatively HERE,
            # inside the lock, so the cache is repaired too — doing it after the
            # lock released left stale labels surviving reloads.
            try:
                final_labels = client.get_issue_detail(owner, repo, number).get("labels", [])
            except routes.GhCliError:
                logger.warning(
                    "tagging: could not re-read labels for %s#%s after a no-op removal",
                    f"{owner}/{repo}",
                    number,
                    exc_info=True,
                )
                return None
        try:
            store.apply_label_change_to_caches(owner, repo, number, final_labels, root=scope)
        except Exception:
            logger.warning(
                "tagging: cache patch failed after a label change on %s#%s",
                f"{owner}/{repo}",
                number,
                exc_info=True,
            )
        return final_labels


def _reread_labels_and_patch(key: provider.RepoKey, number: int) -> list[dict]:
    """Re-read one issue's authoritative labels AND patch the caches, under the lock.

    The retry for the one case `_apply_label_change` cannot resolve itself: every
    removal was a no-op and the in-lock re-read failed, so it returns ``None``
    knowing nothing about the label set. Reading here without patching would leave
    the caller holding fresh labels while the cache still carried the removed one —
    the response looked right and the next reload put the label back.

    Read and patch happen inside the same per-issue lock, so the value written is
    the value read: a concurrent writer either finished before us (we read its
    result) or waits for us (it overwrites with its own newer read)."""
    from .. import routes  # circular import: backend.routes imports this module

    owner, repo = key.owner, key.repo
    client = provider.client_for(key)
    pkw = provider.call_kwargs(key)
    scope = routes._scope(key)
    with store.issue_write_lock(owner, repo, number, scope):
        try:
            labels = client.get_issue_detail(owner, repo, number, **pkw).get("labels", [])
        except routes.GhCliError:
            logger.warning(
                "tagging: could not re-read labels for %s#%s",
                f"{owner}/{repo}",
                number,
                exc_info=True,
            )
            return []
        try:
            store.apply_label_change_to_caches(owner, repo, number, labels, root=scope)
        except Exception:
            logger.warning(
                "tagging: cache patch failed after re-reading labels for %s#%s",
                f"{owner}/{repo}",
                number,
                exc_info=True,
            )
        return labels


async def _handle_labels_apply(request: web.Request) -> web.Response:
    """POST /labels/apply {"owner","repo","number","add":[],"remove":[]} — apply
    a label change to an issue.

    The confirm half of the suggest->confirm loop: used both to accept an
    AI-suggested label and to hand-pick from the repo's existing labels. Gated on
    triage/push access (read-only repos get 403). Added labels MUST already exist
    on the repo — Issue Radar never creates labels (that is repo settings, out of
    scope). Returns the issue's authoritative label set after the change."""
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

    add = body.get("add") or []
    remove = body.get("remove") or []
    if not isinstance(add, list) or not isinstance(remove, list):
        return web.json_response({"error": "'add'/'remove' must be arrays"}, status=400)
    add = [s.strip() for s in add if isinstance(s, str) and s.strip()]
    remove = [s.strip() for s in remove if isinstance(s, str) and s.strip()]
    if not add and not remove:
        return web.json_response({"error": "nothing to change (empty add/remove)"}, status=400)

    if not await asyncio.to_thread(routes._connected, key):
        return web.json_response(
            {"error": f"{owner}/{repo} is not connected — call /connect first"},
            status=404,
        )

    target = f"{owner}/{repo}#{number}"
    if (await asyncio.to_thread(routes._repo_can_write, key)) is not True:
        routes._audit("apply_labels", target, "denied", error="no confirmed write access")
        return web.json_response(
            {
                "error": "This repo is connected read-only — you need triage or push access to edit labels."
            },
            status=403,
        )

    # Guard: only labels that exist on the repo may be ADDED (no label creation).
    try:
        repo_labels = await routes._load_labels_for_ai(key)
    except routes.GhCliError as exc:
        return web.json_response({"error": str(exc)}, status=502)
    known = {lab.get("name") for lab in repo_labels}
    unknown = [n for n in add if n not in known]
    if unknown:
        return web.json_response(
            {"error": f"unknown label(s) for this repo: {', '.join(unknown)}"},
            status=400,
        )

    try:
        final_labels = await asyncio.to_thread(
            partial(routes._apply_label_change, key, number, add, remove)
        )
    except routes.GhPermissionError as exc:
        routes._audit("apply_labels", target, "denied", error=str(exc))
        return web.json_response({"error": str(exc)}, status=403)
    except routes.GhCliError as exc:
        routes._audit("apply_labels", target, "failure", error=str(exc))
        return web.json_response({"error": str(exc)}, status=502)

    if final_labels is None:
        # Only removes, all of which 404'd (labels already absent), AND the in-lock
        # re-read failed. Retry through the locked helper so the caches are repaired
        # too: returning a read the cache never saw is how a removed label came back
        # on the next reload.
        final_labels = await asyncio.to_thread(
            partial(routes._reread_labels_and_patch, key, number)
        )

    # The cache was patched inside the locked step above. Pruning the Tagging queue
    # is a SEPARATE try: sharing one with the patch meant a failed patch skipped the
    # prune, leaving a successfully labelled issue sitting in the queue.
    # The issue is tagged, so its Tagging-queue proposal is spent —
    # drop it here too (not just on the bulk path) so accepting a suggestion from
    # the detail pane also clears it from the dashboard.
    if final_labels:
        try:
            await routes._st(key, store.drop_tagging_suggestions, owner, repo, [number])
        except Exception:
            logger.warning(
                "tagging: could not prune the suggestion for %s#%s",
                f"{owner}/{repo}",
                number,
                exc_info=True,
            )
    routes._audit("apply_labels", target, "ok")
    return web.json_response(
        {"owner": owner, "repo": repo, "number": number, "labels": final_labels}
    )


async def _handle_issue_state(request: web.Request) -> web.Response:
    """POST /issue/state {"owner","repo","number","state","state_reason"?} —
    close or reopen an issue.

    A triage decision, gated on triage/push access. ``state`` is "open" or
    "closed"; on close, ``state_reason`` may be "completed" (default) or
    "not_planned". Returns the issue's state after the change."""
    from .. import routes  # circular import: backend.routes imports this module

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "request body must be JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "request body must be a JSON object"}, status=400)

    key = routes._key_from_body(body)
    owner, repo = key.owner, key.repo
    client = provider.client_for(key)
    pkw = provider.call_kwargs(key)
    number = body.get("number")
    if not owner or not repo:
        return web.json_response({"error": "missing 'owner'/'repo'"}, status=400)
    # bool is a subclass of int: JSON `true` would otherwise validate as #1.
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return web.json_response({"error": "'number' must be a positive integer"}, status=400)

    state = (body.get("state") or "").strip().lower()
    if state not in ("open", "closed"):
        return web.json_response({"error": "state must be 'open' or 'closed'"}, status=400)
    state_reason = body.get("state_reason")
    if state == "closed":
        if state_reason not in (None, "completed", "not_planned"):
            return web.json_response(
                {"error": "state_reason must be 'completed' or 'not_planned'"},
                status=400,
            )
        state_reason = state_reason or "completed"
    else:
        state_reason = None

    if not await asyncio.to_thread(routes._connected, key):
        return web.json_response(
            {"error": f"{owner}/{repo} is not connected — call /connect first"},
            status=404,
        )

    target = f"{owner}/{repo}#{number}"
    if (await asyncio.to_thread(routes._repo_can_write, key)) is not True:
        routes._audit("issue_state", target, "denied", error="no confirmed write access")
        return web.json_response(
            {
                "error": "This repo is connected read-only — you need triage or push access to close/reopen issues."
            },
            status=403,
        )

    try:
        result = await asyncio.to_thread(
            partial(client.set_issue_state, owner, repo, number, state, state_reason, **pkw)
        )
    except routes.GhPermissionError as exc:
        routes._audit("issue_state", target, "denied", error=str(exc))
        return web.json_response({"error": str(exc)}, status=403)
    except routes.GhCliError as exc:
        routes._audit("issue_state", target, "failure", error=str(exc))
        return web.json_response({"error": str(exc)}, status=502)

    await routes._st(
        key,
        store.apply_state_change_to_caches,
        owner,
        repo,
        number,
        result.get("state", state),
        result.get("state_reason"),
    )
    routes._audit("issue_state", f"{target}->{result.get('state', state)}", "ok")
    return web.json_response(
        {
            "owner": owner,
            "repo": repo,
            "number": number,
            "state": result.get("state", state),
            "state_reason": result.get("state_reason"),
        }
    )


# GitHub caps an issue at 10 assignees (its documented limit); reject a longer
# list at the door rather than sending a request the forge will refuse. GitLab
# Free allows one and truncates silently, which is why the returned set is always
# read back from the write rather than echoed.
MAX_ASSIGNEES = 10


def _replace_assignees_checked(
    key: provider.RepoKey, number: int, expected: list[str], desired: list[str]
) -> tuple[list[str] | None, list[str]]:
    """Replace an issue's assignees, but only if the forge still holds ``expected``.

    Replace semantics have a lost-update hazard that a delta does not: two people
    who each start from ``{A}`` and add one name send ``{A,B}`` and ``{A,C}``, and
    the later write silently erases the earlier one's addition. A delta would
    commute -- but a delta is not implementable across both providers, because
    GitLab has no add/remove assignee endpoint at all (only whole-set
    ``assignee_ids``), so an add/remove API would have to be emulated there by the
    same read-modify-write and would carry the identical race while hiding it.

    So the write keeps replace semantics and gains a PRECONDITION instead, which is
    this repo's established answer to exactly this problem: the PR merge pins the
    reviewed ``head_sha`` and the settings PUT echoes the ``revision`` it read, and
    both answer 409 rather than clobbering. Here the client echoes the assignee set
    it actually rendered; if the forge has moved since, nobody's edit is lost --
    the second writer is told.

    Returns ``(final_assignees, current)``. ``final_assignees`` is ``None`` when the
    precondition failed, and ``current`` is then the set the forge actually holds so
    the caller can hand it back for a re-read.

    Runs in a worker thread with the per-issue write lock held across the read, the
    compare and the write, so two writers inside THIS process serialize rather than
    interleave; the precondition is what covers writers outside it.
    """
    from .. import routes  # circular import: backend.routes imports this module

    owner, repo = key.owner, key.repo
    client = provider.client_for(key)
    pkw = provider.call_kwargs(key)
    scope = routes._scope(key)

    def _fold(logins: list[str]) -> set[str]:
        # Order does not matter and the forge is case-preserving but not
        # case-sensitive, so compare as a case-folded set.
        return {s.strip().lower() for s in logins if isinstance(s, str) and s.strip()}

    with store.issue_write_lock(owner, repo, number, scope):
        detail = client.get_issue_detail(owner, repo, number, **pkw)
        current = [a for a in (detail.get("assignees") or []) if isinstance(a, str) and a]
        if _fold(current) != _fold(expected):
            return None, current
        final = client.set_issue_assignees(owner, repo, number, desired, **pkw)
        try:
            store.apply_assignees_change_to_caches(owner, repo, number, final, root=scope)
        except Exception:
            logger.warning(
                "issue-radar: cache patch failed after an assignee change on %s#%s",
                f"{owner}/{repo}",
                number,
                exc_info=True,
            )
        return final, current


async def _handle_issue_assignees(request: web.Request) -> web.Response:
    """POST /issue/assignees {"owner","repo","number","assignees":[...],"expected":[...]} —
    REPLACE an issue's assignees with the given set.

    The confirm half of the assignee editor: the client sends the FINAL set of
    logins (not an add/remove delta) plus ``expected``, the set it last read. The
    write only lands if the forge still holds ``expected``; otherwise it is a 409
    carrying the current set, so a concurrent edit is reported rather than silently
    overwritten (see :func:`_replace_assignees_checked`). An empty ``assignees``
    array clears everyone -- but a junk entry is a 400, never a silent clear.

    Gated on triage/push access (read-only repos get 403). A login the forge will
    not assign is a 400 (``invalid_assignees`` names them), not a 502: GitHub
    answers 422 and applies none of the write, and GitLab is pre-checked against the
    project roster for the same reason. The response otherwise carries the set read
    back from the write rather than the request, because a success is not required
    to be an exact echo (GitLab Free keeps only the first assignee)."""
    from .. import routes  # circular import: backend.routes imports this module

    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": "request body must be JSON", "code": "invalid_json"}, status=400
        )
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "invalid_json"},
            status=400,
        )

    key = routes._key_from_body(body)
    owner, repo = key.owner, key.repo
    # No client/pkw here: every provider call for this route happens inside
    # _replace_assignees_checked, which needs them under the same lock as the write.
    number = body.get("number")
    if not owner or not repo:
        return web.json_response(
            {"error": "missing 'owner'/'repo'", "code": "missing_repo"}, status=400
        )
    # bool is a subclass of int: JSON `true` would otherwise validate as #1.
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return web.json_response(
            {"error": "'number' must be a positive integer", "code": "invalid_number"},
            status=400,
        )
    # An unbounded int reaches the FILESYSTEM: issue_write_lock names its lock file
    # after the number, so a several-hundred-digit value raises ENAMETOOLONG and
    # answers 500 on input that should simply be a 400. Same bound and code the
    # investigation route uses.
    if number > routes.MAX_ITEM_NUMBER:
        return web.json_response(
            {
                "error": f"number must be at most {routes.MAX_ITEM_NUMBER}",
                "code": "item_number_out_of_range",
            },
            status=400,
        )

    assignees = body.get("assignees")
    if not isinstance(assignees, list):
        return web.json_response(
            {"error": "'assignees' must be an array", "code": "assignees_not_array"},
            status=400,
        )
    # REJECT a junk entry rather than dropping it. Silently filtering was a
    # destructive bug on a replace endpoint: `[null]` normalized to `[]`, which is
    # the wire form for "clear everyone", so a malformed request unassigned the
    # whole issue instead of failing. An empty array still means clear -- but only
    # when the caller actually sent an empty array.
    cleaned: list[str] = []
    for entry in assignees:
        if not isinstance(entry, str) or not entry.strip():
            return web.json_response(
                {
                    "error": "each entry in 'assignees' must be a non-empty string",
                    "code": "invalid_assignee_entry",
                },
                status=400,
            )
        cleaned.append(entry.strip())
    assignees = cleaned
    # Dedupe while preserving order — a repeated login is a no-op to the provider
    # but would inflate the count against the cap.
    seen: set[str] = set()
    deduped: list[str] = []
    for login in assignees:
        low = login.lower()
        if low not in seen:
            seen.add(low)
            deduped.append(login)
    assignees = deduped

    # The set the client RENDERED, echoed back so the write carries a precondition
    # (see _replace_assignees_checked). Required, and fail-closed: without it the
    # endpoint would silently overwrite a concurrent edit, which is the whole
    # hazard replace semantics carry.
    expected = body.get("expected")
    if not isinstance(expected, list) or not all(isinstance(s, str) for s in expected):
        return web.json_response(
            {
                "error": "'expected' must be an array of the assignee logins you last read",
                "code": "expected_required",
            },
            status=400,
        )
    if len(assignees) > MAX_ASSIGNEES:
        return web.json_response(
            {
                "error": f"at most {MAX_ASSIGNEES} assignees",
                "code": "too_many_assignees",
            },
            status=400,
        )

    if not await asyncio.to_thread(routes._connected, key):
        return web.json_response(
            {
                "error": f"{owner}/{repo} is not connected — call /connect first",
                "code": "repo_not_connected",
            },
            status=404,
        )

    target = f"{owner}/{repo}#{number}"
    if (await asyncio.to_thread(routes._repo_can_write, key)) is not True:
        routes._audit("issue_assignees", target, "denied", error="no confirmed write access")
        return web.json_response(
            {
                "error": "This repo is connected read-only — you need triage or push access to edit assignees.",
                "code": "repo_read_only",
            },
            status=403,
        )

    try:
        final_assignees, current = await asyncio.to_thread(
            partial(routes._replace_assignees_checked, key, number, expected, assignees)
        )
    except routes.GhPermissionError as exc:
        routes._audit("issue_assignees", target, "denied", error=str(exc))
        return web.json_response({"error": str(exc), "code": "provider_forbidden"}, status=403)
    except routes.GhInvalidInputError as exc:
        # The forge refused a LOGIN, not the caller: 400, naming who was refused.
        # A 502 here would report the forge as broken and invite a retry that can
        # only fail the same way. Neither provider applies a partial write in this
        # case, so nothing changed on the issue.
        routes._audit("issue_assignees", target, "failure", error=str(exc))
        return web.json_response(
            {
                "error": str(exc),
                "code": "invalid_assignees",
                "invalid_assignees": exc.values,
            },
            status=400,
        )
    except routes.GhCliError as exc:
        routes._audit("issue_assignees", target, "failure", error=str(exc))
        return web.json_response(
            {"error": "upstream provider error", "code": "provider_error"}, status=502
        )

    if final_assignees is None:
        # Somebody else changed the assignees between the read this client rendered
        # and this write. Nothing was written; hand back what the forge holds so the
        # client can re-render and let the user redo the edit on current state.
        routes._audit("issue_assignees", target, "failure", error="assignees changed elsewhere")
        return web.json_response(
            {
                "error": "The assignees changed elsewhere since you loaded this issue.",
                "code": "assignees_conflict",
                "assignees": current,
            },
            status=409,
        )

    routes._audit("issue_assignees", target, "ok")
    return web.json_response(
        {"owner": owner, "repo": repo, "number": number, "assignees": final_assignees}
    )
