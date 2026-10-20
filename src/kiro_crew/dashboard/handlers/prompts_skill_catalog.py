"""The skills catalog, project trust, and browsing a skill's files.

``GET /api/skills`` and the catalog assembly it shares between concurrent callers,
the project trust routes that decide which project skills load, and a skill's file
tree and file reads.

Served through :mod:`kiro_crew.dashboard.handlers.prompts`, which callers and tests import and patch; this module
never imports it.
"""

from __future__ import annotations

import asyncio
import functools
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.agent_discovery import (
    SkillScopeResolutionError,
    agent_skill_globs,
    served_provider_label,
    session_skill_globs,
)
from kiro_crew.dashboard.body_numbers import body_int
from kiro_crew.dashboard.handlers.prompts_caller_gates import (
    _deny_foreign_app_skill_slot,
    _deny_non_owner_skill_operation,
    _named_slot,
    _sel,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.skill_trust import ReviewedProjectChanged as _ReviewedProjectChanged
from kiro_crew.skill_trust import (
    TrustStoreFull,
    TrustStoreUnreadable,
    canonical_key,
    grant_project_trust,
    is_key_trusted,
    list_trusted_projects,
    revoke_project_trust,
)
from kiro_crew.skills import PROJECT_SKILL_BODY_CAP, SKILL_READ_CAPACITY, SkillReadRefusal
from kiro_crew.validation import MAX_SKILL_KEY_CHARS

from ._shared import (
    _capability_manager,
    _get_skills,
    _read_session_key,
    _resolve_skill_root,
    active_project_dir,
    collect_skills_blocking,
    list_skill_tree,
    read_bounded_json,
    read_skill_file,
    requesting_slot_project,
    skill_rows_for_globs,
)

# Concurrent readers of the catalog share ONE scan. Nothing is stored and nothing
# expires: the handoff below lives only while a reader is still queued for it.
# Keyed on (loader, project), so two loaders cannot collide on one entry.
_catalog_lock = threading.Lock()
_catalog_waiters: dict[tuple[Any, str], int] = {}
_catalog_handoff: dict[tuple[Any, str], list[dict[str, Any]]] = {}

# One assembly lock PER KEY, so different projects still scan in parallel. Created
# and dropped under _catalog_lock alongside the waiter count that bounds its life.
_catalog_assembly_locks: dict[tuple[Any, str], LoopBoundLock] = {}


async def _assemble_skills_catalog(skills: Any, project_dir: Path | None) -> list[dict[str, Any]]:
    """Return the catalog for *project_dir*, sharing one scan across concurrent readers.

    Shape: a fast path off the assembly lock, then the lock, then a re-check UNDER
    it. The re-check is what coalesces -- readers queued behind the leader take the
    rows it just finished instead of each scanning (0% -> 87.5% at 8-way, measured).

    NOTHING is retained past the burst, so a read that is not concurrent with
    another always scans current on-disk state. No generation or epoch check is
    needed, but NOT because scans cannot overlap: cancelling a leader mid-scan
    leaves its executor thread running (a started thread-pool task is not
    cancellable) while a replacement leader starts its own, so two scans for one key
    CAN overlap. What holds instead is that the handoff is published only after an
    await returns, so a cancelled leader publishes nothing and its rows are
    discarded; the offer is dropped when its last reader leaves.

    The staleness this admits, stated exactly: any reader that shares a scan may be
    served rows read before its own arrival -- a mid-assembly joiner, and equally a
    reader arriving while the finished rows are still draining to their waiters. The
    bound is one assembly for that key, since a reader never queues behind another
    key's scan. There is NO read-after-write guarantee under a burst.

    ONE assembly lock PER KEY, so readers of different projects still scan in
    parallel exactly as the base did: this coalesces same-key readers without making
    any reader wait on an unrelated catalog. That matters because assembly is not
    reliably sub-second -- it can run seconds long on large skills x agents
    catalogs -- so a shared lock would have handed a multi-project burst worse tail
    latency than the base. A test pins the parallelism.

    No work preservation when the leader disconnects: the assembly is awaited
    inline, so cancelling the leader makes the next waiter start over. That buys the
    removal of an in-flight task map and its cancellation bookkeeping.

    The rows are returned as-is, not copied, so a caller that mutates an entry in
    place must copy first.
    """
    key = (skills, str(project_dir) if project_dir is not None else "")
    with _catalog_lock:
        _catalog_waiters[key] = _catalog_waiters.get(key, 0) + 1
        assembly_lock = _catalog_assembly_locks.get(key)
        if assembly_lock is None:
            assembly_lock = _catalog_assembly_locks[key] = LoopBoundLock()
    try:
        with _catalog_lock:
            rows = _catalog_handoff.get(key)
        if rows is not None:
            return rows
        async with assembly_lock:
            # Re-check under the lock: the leader we queued behind may have just
            # finished this catalog. This is the join.
            with _catalog_lock:
                rows = _catalog_handoff.get(key)
            if rows is not None:
                return rows
            result = await _assemble_skills_catalog_uncached(skills, project_dir)
            with _catalog_lock:
                # Only offer the rows if someone else is queued; with no waiter
                # there is nobody to hand them to.
                if _catalog_waiters.get(key, 0) > 1:
                    _catalog_handoff[key] = result
            return result
    finally:
        with _catalog_lock:
            remaining = _catalog_waiters.get(key, 1) - 1
            if remaining > 0:
                _catalog_waiters[key] = remaining
            else:
                _catalog_waiters.pop(key, None)
                # Last reader out, so neither the offer nor this key's lock can
                # outlive the burst that created them.
                _catalog_handoff.pop(key, None)
                _catalog_assembly_locks.pop(key, None)


async def _assemble_skills_catalog_uncached(
    skills: Any,
    project_dir: Path | None,
) -> list[dict[str, Any]]:
    """Do the actual catalog assembly, with no sharing or reuse of any kind.

    Called by :func:`_assemble_skills_catalog`, which awaits it inline while
    holding the coalescing lock -- so a caller that disconnects mid-assembly
    cancels it and the next waiter reassembles.

    Runs the edition capability lookup async (on the loop, non-blocking), then
    offloads ALL blocking filesystem work — kirocrew ``list_skills()`` (os.walk +
    per-file frontmatter reads), package path globs, kiro per-skill resolve/read,
    and the agent annotation — onto the dedicated DISCOVERY pool in one job. This
    work would stall the event loop past the loop-stall watchdog (~25s) on large
    skills×agents catalogs if run on-loop. Use the discovery pool (NOT
    ``maintenance_executor``): this scan is browser-triggerable and can be
    seconds-long, so the maintenance pool would let a few dashboard tabs occupy
    the workers the orphan-reaper sweeps need to recover from a wedge (see
    :mod:`kiro_crew.executors`).

    PRESERVES THE BASE'S RECORDED DEFAULT. The base states, as a decision rather
    than an omission: "No result cache: the endpoint always reflects current
    on-disk state, so freshly created/installed skills appear immediately
    (correctness over the latency a cache would add)." That still holds. Coalescing
    stores no result and has no expiry: concurrent readers share ONE scan, and a
    read that is not part of a concurrent burst always scans current on-disk state.

    Sharing is what admits staleness rather than this function, and
    :func:`_assemble_skills_catalog` is authoritative for that contract. The
    consequence to respect here: do not build a mutation handler that returns this
    catalog expecting the just-written skill.
    """
    mgr = _capability_manager()
    try:
        package_skills = await mgr.list_skills() if mgr.available() else []
    except Exception:
        # The capability manager is one of three skill sources; degrade to "no
        # package skills" rather than 500 the whole /api/skills endpoint.
        package_skills = []
    return await asyncio.get_running_loop().run_in_executor(
        discovery_executor(),
        collect_skills_blocking,
        skills,
        package_skills,
        project_dir,
    )


async def api_skills(request: web.Request) -> web.Response:
    """GET /api/skills — list skills from all known sources.

    Sources:
    - ``kirocrew``: ``~/.kiro/crew/skills/`` (managed by SkillsLoader; editable)
    - ``package``: skills an edition contributes, if any (read-only here)
    - ``kiro-user``: ``~/.kiro/skills/`` (open-standard; read-only here)
    - ``kiro-workspace``: ``<project>/.kiro/skills/`` (open-standard; read-only here)

    Each entry carries ``loaded_by_agents`` — the names of installed agents
    whose ``resources`` would load the skill via a ``skill://`` URI. Empty
    list means no agent loads it via the kiro-cli native loader (it may
    still be loaded via Kiro Crew text-injection or an external MCP server).

    ``?agent=<name>`` scopes the listing to that agent's own ``skill://``
    mapping (matching the same globs the prompt-injection path resolves via
    :func:`agent_skill_globs`) — filtered to skills in that agent's
    ``loaded_by_agents``. An agent with no explicit skill:// resources of its
    own (``agent_skill_globs`` returns ``[]``) keeps the unfiltered, legacy
    all-or-nothing listing: an agent that never customized its skill set
    must not lose access to skills a customized agent's presence would
    otherwise imply are opt-in.

    When the agent filter is actually applied (agent given AND its globs are
    non-empty), the response is the envelope
    ``{"skills": [...], "agent_scoped": true, "agent": <name>}`` instead of
    the bare array. The flag is required wiring, not decoration: a filtered
    list — especially an EMPTY one — is byte-identical to the legacy
    unfiltered array, so without it the client cannot tell "no skills are
    mapped to this agent" apart from "no skills exist at all". Every
    unscoped path keeps the bare-array shape unchanged.
    """
    state: DashboardState = request.app["state"]
    session_key = _read_session_key(request)
    denied = _deny_foreign_app_skill_slot(request, state, session_key, "skills_list")
    if denied is not None:
        return denied
    skills = _get_skills(state)
    # Resolve the active project dir (cheap in-memory scan of slots) on the loop.
    # Scoped to the requesting chat slot: without the key, two chats on
    # different projects fall to None and kiro-workspace skills silently
    # vanish from the listing.
    # Strict: must match what SkillsLoader will resolve for THIS chat, or the
    # catalog advertises a skill whose $token expands to nothing.
    project_dir: Path | None = requesting_slot_project(state, session_key)
    params: Mapping[str, Any] = request.query
    if request.method == "POST":
        body, error = await read_bounded_json(request, max_bytes=512 * 1024)
        if error is not None:
            return error
        assert body is not None
        if body.get("scope") != "installed" or body.get("action") != "read":
            return web.json_response(
                {"error": "POST requires an installed exact read.", "code": "invalid_skill_action"},
                status=400,
            )
        params = body
    if "q" in params or request.method == "POST":
        query = params.get("q", "")
        action = params.get("action", "search")
        key = params.get("key", "")
        if not all(isinstance(value, str) for value in (query, action, key)):
            return web.json_response(
                {"error": "Skill query, action and key must be strings.", "code": "invalid_query"},
                status=400,
            )
        query = query.strip()
        if action not in {"search", "list", "read"} or (action == "read" and not key):
            return web.json_response(
                {
                    "error": "Invalid skill action or missing exact key.",
                    "code": "invalid_skill_action",
                },
                status=400,
            )
        if len(key) > MAX_SKILL_KEY_CHARS:
            return web.json_response(
                {
                    "error": f"Skill keys must be at most {MAX_SKILL_KEY_CHARS} characters.",
                    "code": "invalid_skill_key",
                },
                status=400,
            )
        if (action == "search" and not query) or len(query) > 2000:
            return web.json_response(
                {"error": "Use 1–2000 characters of short keywords.", "code": "invalid_query"},
                status=400,
            )
        try:
            offset = max(0, body_int(params.get("offset", "0")))
            # A read pages in LINES and only when asked: the whole-or-refuse contract
            # holds for a call that names neither parameter, and ``limit`` here is
            # lines rather than the result count search and list bound at 50.
            paging = action == "read" and ("offset" in params or "limit" in params)
            page_limit = max(1, body_int(params["limit"])) if paging and "limit" in params else None
            # The caller may SHRINK the capacity to what its own response framing
            # leaves for the body; the ceiling itself is not the caller's to move.
            capacity = max(
                1, min(SKILL_READ_CAPACITY, body_int(params.get("capacity", SKILL_READ_CAPACITY)))
            )
            limit = 1 if action == "read" else max(1, min(50, body_int(params.get("limit", "20"))))
        except (ValueError, TypeError):
            return web.json_response(
                {"error": "Invalid search limit.", "code": "invalid_limit"}, status=400
            )
        # The skill_search tool reads through POST. The GET read stays for
        # compatibility, and its reader may be a script or a person, so it
        # does not count as a model load.
        model_read = request.method == "POST"

        def search():
            slot = _named_slot(state, session_key)
            agent = str(getattr(slot, "agent", "") or "kirocrew")
            sessions = getattr(state, "sessions", None)
            if session_key and sessions is not None:
                active = sessions.get_agent(session_key)
                if isinstance(active, str) and active:
                    agent = active
            served = sessions.get_provider(session_key) if sessions is not None else None
            only = session_skill_globs(
                session_key,
                agent,
                project_dir=project_dir,
                provider_type=served_provider_label(served),
            )
            if action == "read":
                outcome = skills.read_scoped_skill_page(
                    key,
                    only=only,
                    project_dir=project_dir,
                    offset=offset if paging else None,
                    limit=page_limit,
                    capacity=capacity,
                )
                if isinstance(outcome, SkillReadRefusal):
                    return {"matches": [], "next_offset": None, "refusal": outcome._asdict()}
                if model_read and offset == 0:
                    # An exact read hands the model the body it asked for, the
                    # same delivery a `$name` token credits. A paged read is one
                    # load, so only the page that starts at the body's first
                    # line credits it. Search and list rows are candidates, not
                    # a chosen load, so they credit nothing, even a confined
                    # project row that carries its body.
                    skills.credit_skill_reads([key])
                match: dict[str, Any] = {
                    "key": key,
                    "name": key,
                    "description": "",
                    "content": outcome.content,
                }
                if paging:
                    match["page"] = {
                        field: value
                        for field, value in outcome._asdict().items()
                        if field != "content"
                    }
                return {"matches": [match], "next_offset": None}
            report = skills.search_skills_report(
                query,
                limit=limit + 1,
                project_dir=project_dir,
                only=only,
                offset=offset,
                browse=action == "list",
            )
            matches = report.matches
            next_offset = offset + limit if len(matches) > limit else None
            matches = matches[:limit]
            result = []
            remaining = PROJECT_SKILL_BODY_CAP
            for row in matches:
                item = {k: row[k] for k in ("key", "name", "description")}
                if row.get("confine_root"):
                    body = skills.load_skill(row["key"], project_dir, max_bytes=remaining)
                    item["content"] = (
                        body
                        or "Body unavailable or over budget; retry skill_search with this exact skill key."
                    )
                    remaining = max(0, remaining - len((body or "").encode("utf-8")))
                else:
                    item["path"] = row["path"]
                result.append(item)
            return {
                "matches": result,
                "next_offset": next_offset,
                "incomplete": report.incomplete,
                # The caller cannot see its own skill:// mapping, so a miss would
                # otherwise read as "not installed" when it means "not in scope".
                # A bit, not a count: `only` holds fnmatch globs, not skills.
                "scoped": only is not None,
            }

        try:
            result = await asyncio.to_thread(search)
        except SkillScopeResolutionError:
            return web.json_response(
                {
                    "error": "The session's bound agent skill scope is unavailable.",
                    "code": "skill_scope_unavailable",
                },
                status=409,
            )
        return web.json_response(result)
    result = await _assemble_skills_catalog(skills, project_dir)
    agent = request.query.get("agent") or None
    if agent:
        # Resolve the agent's skill globs against the SAME project the catalog
        # was assembled for. ``agent_skill_globs`` reads the winning spec
        # ``list_agents(project_dir=…)`` picks (project scope shadows user), so
        # a custom agent's workspace-relative ``skill://`` glob anchors at the
        # project exactly as ``session_skill_globs`` does on the runtime path.
        # Off-loop either way: the warm path costs one scandir signature check,
        # the cold path walks and parses ~/.kiro/agents.
        globs = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(),
            functools.partial(agent_skill_globs, agent, project_dir=project_dir),
        )
        if globs:
            # Filter by the agent's OWN resolved globs, not the catalog's
            # ``loaded_by_agents`` field: that field is annotated from the
            # user-level specs, so for a project spec that shadows a user one it
            # would disagree with the winner ``agent_skill_globs`` just resolved.
            # Matching the rows against these globs keeps ``list_agents`` the
            # single owner of which spec wins. Off-loop: ``skill_rows_for_globs``
            # delegates to ``_agents_loading_skill`` (the annotation's own
            # matcher), whose resolved-path retry and alias fallback touch the
            # filesystem per non-matching row, so on a large network-backed
            # catalog it must not run on the event loop.
            result = await asyncio.get_running_loop().run_in_executor(
                discovery_executor(),
                functools.partial(skill_rows_for_globs, result, globs, project_dir=project_dir),
            )
            return web.json_response({"skills": result, "agent_scoped": True, "agent": agent})
    return web.json_response(result)


def _grant_reviewed_project(project_dir: Path, expected: object, *, session_key: str) -> str:
    """Snapshot *project_dir*, confirm its reviewed canonical key, then grant.

    Runs on the discovery executor: `canonical_key` realpaths the slot path, and
    `grant_project_trust` takes a lock and writes. Neither belongs on the event
    loop.

    *expected* is the canonical key returned by the trust snapshot, never a
    selector. The current slot path is canonicalized once, then compared to that
    opaque string before the same key is persisted. Client text is never resolved,
    so a supplied UNC path cannot initiate outbound authentication. Missing and
    mismatched keys both refuse: consent without the reviewed identity is blind.
    """
    return grant_project_trust(
        project_dir,
        expected_key=expected,
        session_key=session_key,
    )


def _trust_snapshot(project_dir: Path | None) -> dict[str, Any]:
    """Blocking read of trust state for *project_dir* plus every stored grant."""
    project_key = canonical_key(project_dir) if project_dir else None
    return {
        "project": str(project_dir) if project_dir else "",
        "project_key": project_key or "",
        "trusted": is_key_trusted(project_key),
        "grants": list_trusted_projects(),
    }


async def api_skills_trust(request: web.Request) -> web.Response:
    """Report the requesting chat's project-skills trust state and all grants."""
    denied = _deny_non_owner_skill_operation(request, "skill_trust_read")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    # Strict: must match what SkillsLoader will resolve for THIS chat, or the
    # catalog advertises a skill whose $token expands to nothing.
    project_dir: Path | None = requesting_slot_project(state, _read_session_key(request))
    snapshot = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _trust_snapshot, project_dir
    )
    return web.json_response(snapshot)


async def api_skills_trust_grant(request: web.Request) -> web.Response:
    """Grant project-skills trust to the REQUESTING CHAT's own project.

    The directory is taken from the requesting slot, never from the request
    body: a caller-supplied path would let anything that can reach this
    endpoint consent on the operator's behalf for a directory they never
    opened. The operator can only trust the project they actually have open.

    The body carries ``expected_key`` — the canonical identity returned with the
    consent dialog's snapshot. It is a required confirmation, never a selector:
    the directory still comes from the slot, and a missing or mismatched key is
    refused. This covers slot changes and mutable aliases between review and click.
    """
    denied = _deny_non_owner_skill_operation(request, "skill_trust_grant")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    session_key = _read_session_key(request)
    # Strict: consent is recorded for the directory THIS chat is bound to.
    # The shared fallback would grant trust to another chat's project.
    project_dir: Path | None = requesting_slot_project(state, session_key)
    if project_dir is None:
        return web.json_response(
            {
                "error": "no project is set for this chat, so there is no directory to trust",
                "code": "skill_trust_no_project",
                "reason": "no_project",
            },
            status=400,
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed/missing confirmation is refused below
        body = {}
    expected = (body or {}).get("expected_key") if isinstance(body, dict) else None
    loop = asyncio.get_running_loop()
    try:
        # Confirmation AND grant in one offloaded call. Both halves touch the
        # filesystem (canonical_key does realpath + isdir, i.e. one lstat per path
        # component), and this handler offloads every other filesystem step -- doing
        # it inline stalled the event loop. Combining them also removes the window
        # a second await would open between confirming a directory and recording
        # consent for it, so what was reviewed is what gets written.
        await loop.run_in_executor(
            discovery_executor(),
            functools.partial(
                _grant_reviewed_project,
                project_dir,
                expected,
                session_key=session_key,
            ),
        )
    except _ReviewedProjectChanged:
        return web.json_response(
            {
                "error": (
                    "this chat's project is no longer the directory shown for "
                    "review, so consent was not recorded"
                ),
                "code": "skill_trust_project_changed",
                "reviewed": str(expected),
                "current": str(project_dir),
            },
            status=409,
        )
    except ValueError as exc:
        return web.json_response(
            {"error": str(exc), "code": "skill_trust_unusable_project"}, status=400
        )
    except TrustStoreFull as exc:
        return web.json_response({"error": str(exc), "code": "skill_trust_store_full"}, status=409)
    except TrustStoreUnreadable as exc:
        # Refusing beats overwriting: the store may hold grants this build
        # cannot read, and appending to an empty list would destroy them.
        return web.json_response(
            {"error": str(exc), "code": "skill_trust_store_unreadable"}, status=409
        )
    snapshot = await loop.run_in_executor(discovery_executor(), _trust_snapshot, project_dir)
    return web.json_response(snapshot)


async def api_skills_trust_revoke(request: web.Request) -> web.Response:
    """Withdraw a project-skills trust grant.

    Unlike granting, this accepts an explicit ``path`` so the operator can
    revoke a grant for a directory they do not have open (or have deleted)
    from the settings list. Removing trust only ever narrows what loads, so a
    caller-supplied path is safe here.
    """
    denied = _deny_non_owner_skill_operation(request, "skill_trust_revoke")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    session_key = _read_session_key(request)
    target = request.query.get("path", "").strip()
    if not target:
        project_dir = active_project_dir(state, session_key)
        if project_dir is None:
            return web.json_response(
                {
                    "error": "no path given and no project is set for this chat",
                    "code": "skill_trust_no_target",
                },
                status=400,
            )
        target = str(project_dir)
    loop = asyncio.get_running_loop()
    try:
        removed = await loop.run_in_executor(
            discovery_executor(),
            functools.partial(revoke_project_trust, target, session_key=session_key),
        )
    except TrustStoreUnreadable as exc:
        # A revoke rewrites the survivors, so an unreadable store would lose the
        # grants it could not read -- refuse rather than narrow destructively.
        return web.json_response(
            {"error": str(exc), "code": "skill_trust_store_unreadable"}, status=409
        )
    project_dir = active_project_dir(state, session_key)
    snapshot = await loop.run_in_executor(discovery_executor(), _trust_snapshot, project_dir)
    snapshot["removed"] = removed
    return web.json_response(snapshot)


async def api_skill_tree(request: web.Request) -> web.Response:
    """GET /api/skills/{name}/tree — list files within a skill folder.

    Capped at SKILL_TREE_MAX_ENTRIES; sensitive paths and symlinks
    escaping the skill root are omitted.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["name"]
    session_key = _read_session_key(request)
    if name.startswith("kiro-workspace/"):
        denied = _deny_foreign_app_skill_slot(request, state, session_key, "skill_tree")
        if denied is not None:
            return denied

    def _resolve_and_list() -> tuple["Path | None", list]:
        # Resolve (stat/realpath) and the tree walk are one filesystem
        # transaction; both run on the discovery pool so a network-backed
        # project cannot stall the event loop.
        r = _resolve_skill_root(name, state, session_key)
        return (r, list_skill_tree(r) if r is not None else [])

    root, entries = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _resolve_and_list
    )
    if root is None:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_tree",
            tool_kind="skill",
            outcome="not_found",
            metadata={"name": name},
        )
        return web.json_response({"error": "not found"}, status=404)
    # Sanitize the absolute path — never expose the server's real home to the
    # client.  ``root`` is already resolved (symlinks followed), so compare
    # against the *resolved* home too; otherwise a symlinked home (e.g. macOS
    # ``/var`` → ``/private/var``) would mismatch and leak the real path.
    display_root = str(root)
    for home in {str(Path.home()), str(Path.home().resolve())}:
        display_root = display_root.replace(home, "~")
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skill_tree",
        tool_kind="skill",
        outcome="ok",
        metadata={"name": name, "root": display_root, "count": len(entries)},
    )
    return web.json_response({"name": name, "root": display_root, "entries": entries})


async def api_skill_file(request: web.Request) -> web.Response:
    """GET /api/skills/{name}/file?path=<rel> — read a single file inside a skill folder.

    Capped at SKILL_FILE_MAX_BYTES.  Returns 400 on path-escape attempts,
    403 on sensitive paths, 413 when over the size cap, 404 otherwise.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["name"]
    rel_path = request.query.get("path", "")
    session_key = _read_session_key(request)
    if name.startswith("kiro-workspace/"):
        denied = _deny_foreign_app_skill_slot(request, state, session_key, "skill_file")
        if denied is not None:
            return denied

    def _audit(outcome: str) -> None:
        # Audit every access — including failed ones (traversal rejections,
        # sensitive-path blocks), which can indicate filesystem probing.
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_file",
            tool_kind="skill",
            outcome=outcome,
            metadata={"name": name, "path": rel_path},
        )

    if not rel_path:
        _audit("bad_request")
        return web.json_response({"error": "path query param required"}, status=400)

    def _resolve_and_read() -> tuple["Path | None", str | None, str | None]:
        # One filesystem transaction on the discovery pool (see api_skill_tree).
        r = _resolve_skill_root(name, state, session_key)
        if r is None:
            return None, None, None
        c, e = read_skill_file(r, rel_path)
        return r, c, e

    root, content, err = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _resolve_and_read
    )
    if root is None:
        _audit("not_found")
        return web.json_response({"error": "not found"}, status=404)
    if err:
        if err == "access denied":
            _audit("blocked")
            return web.json_response({"error": err}, status=403)
        if err.startswith("file too large"):
            _audit("too_large")
            return web.json_response({"error": err}, status=413)
        if err == "invalid path":
            _audit("blocked")
            return web.json_response({"error": err}, status=400)
        _audit("not_found")
        return web.json_response({"error": err}, status=404)
    _audit("ok")
    return web.json_response({"name": name, "path": rel_path, "content": content})
