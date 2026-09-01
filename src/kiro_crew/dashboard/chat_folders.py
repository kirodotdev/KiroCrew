"""Folder management — CRUD, pin, assignment. Also hosts the shared
LLM emoji generator the artifact library uses for ITS folder icons."""

from __future__ import annotations

import asyncio
import logging
import os
import unicodedata
import uuid
import weakref
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.chat_tags import tags_write_lock, validate_folder_tag_ids
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.create_rate_limit import FOLDER_CREATE, allow_create
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.token_auth import caller_names_a_missing_slot, derive_caller_app
from kiro_crew.executors import subprocess_executor
from kiro_crew.llm_helpers import run_bg_oneliner
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.sandbox import voice_runtime_workspace_conflict
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: The ceiling on chat folders, the folder-tree counterpart to
#: :data:`~kiro_crew.dashboard.state.MAX_LIVE_SLOTS`. Folder creation had no bound
#: at all: every other create path in the dashboard tests a ceiling, so an
#: automated caller looping on this one was the single way to grow durable
#: on-disk state without limit.
#:
#: Chosen to sit far above any hand-built tree -- a person organizing chats works
#: in tens of folders, not hundreds -- so the only thing that ever reaches it is a
#: runaway loop. Tested under ``mutate_folders``, not before it: the count is only
#: authoritative while the lock is held, which is the same reason the parent is
#: re-checked and ``order`` recounted there.
MAX_CHAT_FOLDERS = 500

_folder_icon_lock = LoopBoundLock()


def _is_single_emoji(s: str) -> bool:
    """True if `s` is exactly one emoji grapheme (no letters/digits/text).

    Accepts simple emoji, variation-selector / skin-tone modified emoji, ZWJ
    sequences (families, professions), and two-codepoint flag pairs. Rejects
    empty strings, plain text, and multiple emoji.
    """
    if not s or len(s) > 16:
        return False
    modifiers = {0xFE0F, 0x200D}  # variation selector-16, zero-width joiner

    def _emoji_char(c: str) -> bool:
        o = ord(c)
        return (
            unicodedata.category(c).startswith("So")  # symbol, other
            or o > 0x1F000  # supplementary emoji planes
            or o in modifiers
            or 0x1F3FB <= o <= 0x1F3FF  # skin-tone modifiers
            or 0x1F1E6 <= o <= 0x1F1FF  # regional indicators (flags)
        )

    if not all(_emoji_char(c) for c in s):
        return False
    # Count grapheme clusters; must be exactly one.
    cps = [ord(c) for c in s]
    n = len(cps)
    clusters = 0
    i = 0
    while i < n:
        if 0x1F1E6 <= cps[i] <= 0x1F1FF:  # flag = pair of regional indicators
            clusters += 1
            i += 2 if (i + 1 < n and 0x1F1E6 <= cps[i + 1] <= 0x1F1FF) else 1
        else:
            clusters += 1  # base emoji, then absorb modifiers / ZWJ-joined emoji
            i += 1
            while i < n and (cps[i] == 0xFE0F or 0x1F3FB <= cps[i] <= 0x1F3FF):
                i += 1
            while i < n and cps[i] == 0x200D:  # ZWJ joins the following emoji
                i += 2 if i + 1 < n else 1
                while i < n and (cps[i] == 0xFE0F or 0x1F3FB <= cps[i] <= 0x1F3FF):
                    i += 1
        if clusters > 1:
            return False
    return clusters == 1


# "auto" = inherit the session's governed default (run_bg_oneliner skips the
# override for auto). A hardcoded model id 400s on accounts/partitions that do
# not serve it.
_FOLDER_ICON_MODEL = "auto"


# Folder color palette — the identity mark a user picks for a folder in the
# config modal. The frontend source of truth is FOLDER_COLOR_PALETTE in
# website/src/components/folderColorCatalog.tsx (shared by the chat-folder
# modal and the Artifacts page's folder swatches); this allowlist must match
# it, and test_folder_color_palette_matches_frontend_catalog pins the two.
_FOLDER_COLOR_PALETTE = frozenset(
    {
        "#ef4444",
        "#f97316",
        "#f59e0b",
        "#84cc16",
        "#22c55e",
        "#14b8a6",
        "#06b6d4",
        "#3b82f6",
        "#6366f1",
        "#8b5cf6",
        "#ec4899",
        "#94a3b8",
    }
)


def _is_valid_folder_color(s: str) -> bool:
    """True for a palette color value (lowercase hex, allowlisted)."""
    return s in _FOLDER_COLOR_PALETTE


def _validate_folder_tags(state: DashboardState, raw: Any) -> tuple[list[str] | None, str | None]:
    """Validate a folder ``tags`` payload against the tag vocabulary.

    Returns ``(clean_ids, None)`` on success or ``(None, error)`` on rejection.
    Only the payload SHAPE is rejected (``tags`` must be an array) — matching
    ``api_chat_slot_tags``, the sibling this endpoint mirrors, unknown ids and
    non-string entries are silently FILTERED, not 400ed. That leniency is
    load-bearing: a dangling id can legitimately exist on a folder (the
    acknowledged best-effort strip failure in ``api_chat_tag_delete``), and a
    strict endpoint would make every subsequent save of that folder fail —
    the "permanently uneditable folder" class. Filtering at the write means a
    stale reference is shed on the next save instead of bricking it.
    ``clean_ids`` is deduped preserving first-seen order, with no count cap
    (vocabulary membership plus dedupe already bounds the list). An empty list
    is valid — it means "no tags", the same way an absent ``color`` means
    "default color".

    Only the payload SHAPE is owned here (``tags`` must be an array → 400,
    matching the sibling ``api_chat_slot_tags``); everything after the shape
    check DELEGATES to ``validate_folder_tag_ids`` — the single definition of
    a usable folder tag id — so the filter-not-400 leniency, dedupe, string
    guard, and the authority-gated fail-open vocabulary intersection cannot
    drift from the inheritance paths that read these same ids back. See that
    helper's docstring for why filtering (not 400) and failing open (not
    intersecting an unknown vocabulary) are both load-bearing.
    """
    if not isinstance(raw, list):
        return None, "tags must be an array"

    return validate_folder_tag_ids(raw, state), None


async def generate_emoji_for_name(state: DashboardState, name: str) -> str:
    """Ask the cheapest model for ONE emoji representing a folder ``name``.

    Shared by chat folders and artifact-library folders. Serialized via a
    module-level lock so concurrent folder creations don't interleave streams
    on the shared BACKGROUND_KEY session. Returns ``""`` on any failure or
    when the reply isn't exactly one emoji grapheme.
    """

    prompt = (
        f'Reply with exactly ONE emoji that best represents a project folder named "{name}". '
        "No text, no explanation, just the single emoji character."
    )

    # Folder icon is a trivial single-emoji task — run on the cheapest model via
    # the shared background one-liner helper (best-effort, 30s bound, denials
    # SEL-logged). The lock serializes icon generation across folders.
    async with _folder_icon_lock:
        try:
            text = await run_bg_oneliner(
                state.sessions,
                prompt,
                model=_FOLDER_ICON_MODEL,
                sel_source="chat_folders",
                timeout=30,
            )
        except Exception:  # noqa: BLE001 — best-effort background task
            text = ""
    icon = text.strip()
    icon, _ = redact_exfiltration_urls(icon)
    icon, _ = redact_credentials(icon)
    # Validate: must be exactly one emoji (guard against stray LLM text).
    return icon if _is_single_emoji(icon) else ""


def _folder_history_counts(state: DashboardState) -> dict[str, int]:
    """Count on-disk (history) sessions filed in each folder, keyed by folder_id.

    Authoritative per-folder archived-session count computed from the full
    session list, NOT the paginated client history window. The sidebar uses it
    to decide whether an empty folder can be hidden (it has an archived session
    that can revive it) or must be deleted instead (nothing could revive it).
    """
    counts: dict[str, int] = {}
    if not state.conversation_log:
        return counts
    for session in state.conversation_log.list_sessions():
        fid = session.get("folder_id")
        if fid:
            counts[fid] = counts.get(fid, 0) + 1
    return counts


def _folders_with_history_counts(state: DashboardState) -> list[dict]:
    """Folders enriched with a computed, non-persisted `history_count` field."""
    counts = _folder_history_counts(state)
    return [{**f, "history_count": counts.get(f["id"], 0)} for f in state._folders]


async def _unhide_folder(state: DashboardState, folder_id: str) -> bool:
    """Clear a folder's `hidden` flag when a session re-engages it.

    Model-B semantics: reviving or moving a session into a folder un-hides it so
    it stays visible until the user hides it again. Persists on change; the
    caller is responsible for pushing the slots update.

    Returns whether the folder EXISTS. Existence is reported from inside the
    store lock, which is the only place it can be checked without a race: a
    caller that validated against ``state._folders`` beforehand and then assigned
    can have the folder deleted in between, and would persist a placement into a
    folder that is gone.
    """
    if not folder_id:
        return True

    def _clear(folders: list[dict[str, Any]]) -> tuple[bool, bool]:
        for f in folders:
            if f["id"] == folder_id:
                if f.get("hidden"):
                    f["hidden"] = False
                    return True, True
                # Present and already visible: report no change so the store is
                # not rewritten. This runs on every session move, so a needless
                # write here would be a write per move.
                return False, True
        return False, False

    return await state.mutate_folders(_clear)


# The internal callers this module recognizes on ``X-Internal-Caller``.
# Exact-listed and ratcheted in ``test_chat_folder_audit_origin.py``: adding a
# caller here must be a conscious edit paired with a test, never a silent
# widen — the point of the header is that a NEW internal caller surfaces as
# ``unknown-internal`` in the audit until someone decides what to call it,
# instead of silently inheriting another component's label (#3503).
_KNOWN_INTERNAL_CALLERS = frozenset({"kirocrew-dashboard"})


def _audit_origin(request: web.Request) -> tuple[str, str]:
    """SEL ``(source, caller)`` for a folder mutation.

    ``source`` stays in SEL's documented *interface* vocabulary (``dashboard``,
    ``mcp``, ...) so operator queries like ``source == "mcp"`` keep matching
    every MCP-driven event uniformly; the validated component identity rides
    in ``caller``, which SEL already carries for exactly this purpose.

    These endpoints are driven by BOTH the browser and the ``chat_folder_*``
    MCP tools (``/api/chat`` is a mixed-internal path). A request without
    ``X-Internal-Secret`` is the browser: ``("dashboard", "dashboard")``. An
    internal request names its component in ``X-Internal-Caller`` (attached by
    the MCP stdio servers' shared loopback request helpers — see
    ``mcp_shared.set_internal_caller``), validated against
    ``_KNOWN_INTERNAL_CALLERS``. Inferring the identity from the secret alone
    was correct only while exactly one internal caller existed, and would
    silently mislabel every write the moment a second one is added.

    Trust model: the secret is verified by the token-auth middleware before
    this handler runs, so authentication is settled here. The caller header is
    ATTRIBUTION on top of that — it grants nothing (a browser sending the
    header without the secret still audits as ``dashboard``), and an
    unrecognized or missing value on an authenticated internal request is
    recorded as ``caller="unknown-internal"`` with a warning rather than
    trusted into the audit log.
    """
    if request.headers.get("X-Internal-Secret") is None:
        return "dashboard", "dashboard"
    caller = (request.headers.get("X-Internal-Caller") or "").strip()
    if caller in _KNOWN_INTERNAL_CALLERS:
        return "mcp", caller
    logger.warning(
        "internal folder write without a recognized X-Internal-Caller (got %r) — "
        "audited as unknown-internal; a new internal caller must be added to "
        "_KNOWN_INTERNAL_CALLERS alongside its ratchet test",
        caller[:64],
    )
    return "mcp", "unknown-internal"


async def api_chat_folders(request: web.Request) -> web.Response:
    """GET /api/chat/folders — list all project folders (with archived-session counts)."""
    state: DashboardState = request.app["state"]
    # _folders_with_history_counts walks the on-disk session list (a synchronous
    # filesystem scan) that is user-triggered (every GET) and scales with the
    # archived-session count. Offload it to keep the event loop responsive, using
    # subprocess_executor (the pool for potentially-slow work) rather than
    # maintenance_executor, whose fast periodic sweeps — the orphan reaper — must
    # stay responsive and could otherwise be starved by frequent polling.
    loop = asyncio.get_running_loop()
    folders = await loop.run_in_executor(subprocess_executor(), _folders_with_history_counts, state)
    return web.json_response(folders)


def _validate_project_dir(raw: str) -> tuple[str, str | None]:
    """Validate and normalize project_dir. Returns (resolved_path, error_msg)."""
    if not raw:
        return "", None
    if not os.path.isabs(raw) and not raw.startswith("~"):
        return "", "project_dir must be an absolute path"
    resolved = os.path.realpath(os.path.expanduser(raw))
    if is_sensitive_path(resolved):
        sel().log_api_access(
            caller="dashboard",
            operation="chat.folder_project_dir",
            outcome="denied",
            resources=resolved,
            error="sensitive path",
        )
        return "", "project_dir refers to a sensitive path"
    if not os.path.isdir(resolved):
        return "", "project_dir must be an existing directory"
    return resolved, None


def _folder_project_overlap_denied(resolved: str) -> str | None:
    """Pre-flight the voice-runtime workspace guard for a folder's project_dir.

    #7392 review round 3: a folder's linked project lands on slots verbatim, so
    without this check "link a folder to ~" is refused only at agent spawn.
    Same shared scan and message as the project endpoint and set_project — the
    user-driven moments of choice agree. Returns the refusal message or None.

    Synchronous on purpose (realpath/mkdir priming on first use — round 4):
    callers on the event loop MUST run it via ``asyncio.to_thread``, exactly
    like the project endpoint does. It is deliberately NOT part of
    ``_validate_project_dir``: that validator also re-checks STORED values on
    the slot-create read path, where re-priming per read would be loop-blocking
    and where the spawn guard remains the authority.
    """
    conflict = voice_runtime_workspace_conflict(resolved)
    if conflict is None:
        return None
    sel().log_api_access(
        caller="dashboard",
        operation="chat.folder_project_dir",
        outcome="denied",
        resources=resolved,
        error="voice runtime overlap",
    )
    return conflict


def _resolve_folder_project_dir(
    folders: list[dict[str, Any]], folder_id: str
) -> tuple[str, str | None]:
    """Return the nearest validated project directory inherited by a folder."""
    by_id = {str(folder.get("id") or ""): folder for folder in folders if isinstance(folder, dict)}
    seen: set[str] = set()
    current_id = folder_id
    while current_id and current_id not in seen:
        seen.add(current_id)
        folder = by_id.get(current_id)
        if folder is None:
            break
        raw_project = folder.get("project_dir")
        if raw_project:
            if not isinstance(raw_project, str):
                return "", "project_dir must be a string"
            return _validate_project_dir(raw_project.strip())
        current_id = str(folder.get("parent_id") or "")
    return "", None


def _refuse_unattributable_caller(
    state: DashboardState, request: web.Request
) -> web.Response | None:
    """403 when the caller NAMES a dashboard slot that is gone, else None.

    ``_effective_request_app`` answers ``""`` both for the person and for a
    caller it cannot place, and the tree-shaping rules read ``""`` as the
    person's full authority. That is sound for a caller that never had a slot --
    a Slack thread, a channel session, the person's own cron -- but not for a
    ``dashboard:`` key, which NAMES a slot: absence there is not "nothing to
    confine me to", it is "the app I would have been confined to is exactly what
    got popped". A tab closing while one of its tool calls is still in flight
    produces precisely that, because the slot is popped synchronously without
    draining in-flight MCP calls.

    So an app-owned session going through that race would otherwise arrive here
    with an empty scope and be handed the person's authority over the person's
    own folders. ``mcp_dashboard._caller_app_scope`` already refuses this class
    for its own tool set; ``caller_names_a_missing_slot`` exists so a route
    outside that set applies the same rule, and it is deliberately NOT in the
    middleware -- a popped slot no longer says whose tab it was, so refusing
    there would also refuse the person's own in-flight calls on every internal
    route at once. Each route that could not attribute a write decides for
    itself, and a write to the shared folder tree is one of those.
    """
    if caller_names_a_missing_slot(
        getattr(state, "_slots", None), request.headers.get("X-Session-Key", "")
    ):
        sel().log_api_access(
            caller="unattributable",
            operation="chat.folder_write",
            outcome="denied",
            source="app_isolation",
            resources=request.path,
            error="caller names a dashboard slot that is gone",
        )
        return web.json_response(
            {
                "error": "the calling session is gone, so this write cannot be attributed",
                "code": "caller_unattributable",
            },
            status=403,
        )
    return None


def _folder_owner_app(folder: dict[str, Any]) -> str:
    """The app that owns *folder*, or ``""`` when the person owns it.

    The single place the storage rule is expressed: a folder created by an app
    carries that app in ``owner_app``, and **an absent or empty key reads as the
    person's**. That default is what makes this a field addition rather than a
    migration — every folder written before the field existed is the person's,
    which is exactly what it was.

    Ownership decides only the tree-shaping verbs (create-into, rename,
    reparent, delete). Reads stay whole: an app sees the person's folders and
    can file its OWN sessions into one (``api_chat_slot_folder``), which is the
    case a per-app namespace would have cost.
    """
    return str(folder.get("owner_app") or "")


def _subtree_holds_foreign_folder(
    folders: list[dict[str, Any]], *, root_id: str, request_app: str
) -> bool:
    """True if anything under *root_id* belongs to someone other than *request_app*.

    Reparenting a folder relocates everything beneath it -- that is what "the
    folder moves with everything in it" means -- so the blast radius of a move is
    the whole SUBTREE, not the row being written. An app moving a folder it owns
    would otherwise relocate a folder the person nested inside it, which is the
    same violation as editing that folder directly, reached one level down.

    Used by the reparent path only. Delete asks a stricter question instead --
    whether the folder is EMPTY -- because a delete has more kinds of content to
    account for (sessions, and archived sessions a live scan cannot see), and
    emptiness answers all of them without an ownership test per content type.

    Scoped to the descendants and not the root: the caller's authority over the
    root itself is a separate question, answered separately.

    Uses the cycle-guarded walk so a pre-existing corrupt parent chain in
    folders.json cannot hang the request.
    """
    for f in folders:
        fid = str(f.get("id") or "")
        if fid == root_id:
            continue
        if _folder_owner_app(f) == request_app:
            continue
        if _is_descendant(folders, ancestor_id=root_id, folder_id=fid):
            return True
    return False


def _is_descendant(folders: list[dict], *, ancestor_id: str, folder_id: str) -> bool:
    """True if `folder_id` is `ancestor_id` or lies anywhere under it.

    Walks parent_id links upward from `folder_id` with a visited-set guard
    so pre-existing corrupt cycles in folders.json can't hang the request.
    """
    by_id = {f["id"]: f for f in folders}
    seen: set[str] = set()
    cur: str | None = folder_id
    while cur and cur not in seen:
        if cur == ancestor_id:
            return True
        seen.add(cur)
        node = by_id.get(cur)
        cur = str(node.get("parent_id") or "") if node else None
    return False


async def api_chat_folder_create(request: web.Request) -> web.Response:
    """POST /api/chat/folders — create a project folder."""
    state: DashboardState = request.app["state"]
    if (refusal := _refuse_unattributable_caller(state, request)) is not None:
        return refusal
    # Rate-limit INTERNAL callers only. This endpoint is mixed-path: the browser's
    # own "new folder" control posts here too, and a person organizing their chats
    # can legitimately create a dozen in one sitting, so throttling them would be a
    # regression with no security value. The threat is an automated loop on an
    # auto-approved verb, and `_audit_origin` already tells the two apart -- a
    # request without the internal secret is the browser.
    #
    # Keyed on the VALIDATED caller name, not the session key. The session key would
    # give finer granularity but is partly caller-supplied:
    # `_refuse_unattributable_caller` only refuses a `dashboard:` key naming a dead
    # slot, so a caller could present rotating non-dashboard keys and earn a fresh
    # budget for each. The caller name is checked against `_KNOWN_INTERNAL_CALLERS`,
    # so it cannot be varied to escape the bucket. The cost is that internal callers
    # share one folder budget, which is acceptable when a goal needs exactly one.
    rl_source, rl_caller = _audit_origin(request)
    if rl_source != "dashboard" and not allow_create(FOLDER_CREATE, rl_caller):
        return web.json_response(
            {
                "error": "too many folders created recently; retry shortly",
                "code": "create_rate_limited",
            },
            status=429,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    name = (body.get("name") or "").strip()[:100]
    if not name:
        return web.json_response({"error": "name required"}, status=400)
    parent_id = str(body.get("parent_id") or "")
    if parent_id and not any(f["id"] == parent_id for f in state._folders):
        return web.json_response({"error": "parent folder not found"}, status=400)
    project_dir = str(body.get("project_dir") or "").strip()
    project_dir, err = _validate_project_dir(project_dir)
    if err:
        return web.json_response({"error": err}, status=400)
    if project_dir:
        # Off-loop: the shared scan primes runtime paths on first use (round 4).
        conflict = await asyncio.to_thread(_folder_project_overlap_denied, project_dir)
        if conflict is not None:
            return web.json_response(
                {"error": conflict, "code": "workspace_overlaps_data_home"}, status=400
            )
    default_agent = str(body.get("default_agent") or "").strip()
    color = str(body.get("color") or "").strip().lower()
    if color and not _is_valid_folder_color(color):
        # `code` is the contract, `error` is advisory prose (RFC 9457 3.1.3) —
        # the dashboard renders `error` verbatim into a localized UI, so a new
        # error response without an id is untranslatable by construction.
        return web.json_response(
            {"error": "color must be one of the folder palette values", "code": "color_invalid"},
            status=400,
        )
    # Organizational tags copied onto every new chat filed into this folder.
    # Validated exactly like the slot-tags endpoint (ids from the live
    # vocabulary), and included in the folder dict only when non-empty — the
    # same optional-key shape as `color`, so a tagless folder keeps the record
    # it has on disk today.
    folder_tags: list[str] = []
    if "tags" in body:
        clean_tags, tags_err = _validate_folder_tags(state, body.get("tags"))
        if tags_err or clean_tags is None:
            return web.json_response(
                {"error": tags_err or "tags invalid", "code": "tags_invalid"}, status=400
            )
        folder_tags = clean_tags
    folder = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "order": len(state._folders),
        "collapsed": False,
        "hidden": False,
        "parent_id": parent_id,
        "project_dir": project_dir,
        "default_agent": default_agent,
    }
    if color:
        folder["color"] = color
    if folder_tags:
        folder["tags"] = folder_tags
    # Never from the body: a caller that could name its own owner could name
    # someone else's. Written only when an app is calling, so the person's rows
    # keep the shape they have on disk today and "absent means the person"
    # stays the one representation (see _folder_owner_app).
    request_app = _effective_request_app(state, request)
    if request_app:
        folder["owner_app"] = request_app

    def _append(folders: list[dict[str, Any]]) -> tuple[bool, str]:
        # Re-check the parent under the lock. Its existence was validated before
        # the lock was taken, so a concurrent delete of that parent would
        # otherwise land this folder with a dangling parent_id — the same
        # pre-lock/post-lock gap the reparent path re-tests.
        parent = next((f for f in folders if f["id"] == parent_id), None) if parent_id else None
        if parent_id and parent is None:
            return False, "parent_not_found"
        # The ceiling is tested here, under the lock, for the same reason the parent
        # is re-checked here: `len(folders)` is only authoritative while the lock is
        # held, so a pre-lock test lets concurrent creators each pass a cap that is
        # already full.
        if len(folders) >= MAX_CHAT_FOLDERS:
            return False, "folder_cap_reached"
        # Nesting into a folder writes to THAT folder's child list, so an app may
        # only nest under one of its own. The top level is not a folder row and
        # so has no owner to violate — that is where an app's own tree starts.
        # Decided here rather than pre-lock because a reparent racing this
        # request can change who the parent belongs to.
        if request_app and parent is not None and _folder_owner_app(parent) != request_app:
            return False, "forbidden_parent"
        folder["order"] = len(folders)  # recount under the lock
        folders.append(folder)
        return True, ""

    if folder_tags:
        # The AUTHORITATIVE intersection runs here, at the point of application,
        # under ``tags_write_lock`` — the invariant every tag consumer follows
        # (see api_chat_slot_tags / the channel filing): a tag deletion
        # committing between the early shape check and this write must not be
        # persisted onto the new folder, and the strip pass a deletion runs
        # cannot see a folder that is not yet in the store.

        async with tags_write_lock(state):
            refreshed, _ = _validate_folder_tags(state, folder_tags)
            if refreshed:
                folder["tags"] = refreshed
            else:
                folder.pop("tags", None)
            create_err = await state.mutate_folders(_append)
    else:
        create_err = await state.mutate_folders(_append)
    if create_err == "folder_cap_reached":
        return web.json_response(
            {
                "error": f"folder cap reached ({MAX_CHAT_FOLDERS})",
                "code": "folder_cap_reached",
            },
            status=429,
        )
    if create_err == "parent_not_found":
        # The parent was deleted while this request waited for the lock.
        return web.json_response(
            {"error": "parent folder not found", "code": "folder_parent_not_found"},
            status=400,
        )
    if create_err == "forbidden_parent":
        sel().log_api_access(
            caller=request_app,
            operation="chat.folder_create",
            outcome="denied",
            source="app_isolation",
            resources=f"parent={parent_id}",
            error="app cannot create inside a folder it does not own",
        )
        return web.json_response(
            {
                "error": "cannot create a folder inside one this app does not own",
                "code": "folder_not_owned",
            },
            status=403,
        )
    state.push_slots_update()
    source, caller = _audit_origin(request)
    sel().log_api_access(
        caller=caller,
        operation="chat.folder_create",
        outcome="allowed",
        source=source,
        resources=str(folder["id"]),
    )
    return web.json_response(folder, status=201)


async def api_chat_folder_update(request: web.Request) -> web.Response:
    """PATCH /api/chat/folders/{id} — rename or reorder a folder."""
    state: DashboardState = request.app["state"]
    if (refusal := _refuse_unattributable_caller(state, request)) is not None:
        return refusal
    fid = request.match_info["id"]
    folder = next((f for f in state._folders if f["id"] == fid), None)
    if not folder:
        return web.json_response({"error": "not found"}, status=404)
    request_app = _effective_request_app(state, request)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # Validate ALL submitted fields into a pending-changes dict BEFORE mutating
    # ``folder`` — otherwise an early field (e.g. name) is persisted while a later
    # field (e.g. an invalid/cyclic parent_id) returns 400, leaving the rejected
    # request's partial mutation live for the next successful save.
    #
    # ``owner_app`` is deliberately absent from the fields below: ownership is
    # stamped once at create from the authenticated caller and is not a field a
    # request can hand over, take, or clear.
    changes: dict[str, object] = {}
    if "name" in body:
        new_name = str(body["name"]).strip()[:100]
        if not new_name:
            return web.json_response({"error": "name required"}, status=400)
        changes["name"] = new_name
    if "collapsed" in body:
        changes["collapsed"] = bool(body["collapsed"])
    if "hidden" in body:
        changes["hidden"] = bool(body["hidden"])
    if "order" in body:
        # A non-numeric, null, or non-finite order is caller error, not a server
        # fault: int() would raise and surface as a 500 (no middleware maps
        # handler exceptions). OverflowError covers JSON infinities such as
        # 1e309, which int() rejects with neither TypeError nor ValueError.
        # Skip the field instead, matching api_chat_tag_update.
        try:
            changes["order"] = int(body["order"])
        except (TypeError, ValueError, OverflowError):
            pass
    if "default_agent" in body:
        val = body["default_agent"]
        changes["default_agent"] = str(val).strip() if val is not None else ""
    reparenting = "parent_id" in body
    new_parent = ""
    if reparenting:
        # Re-parent: move this folder into another folder, or to the top
        # level ("" / null). Reject self-parenting and cycles (the new
        # parent must not be the folder itself or any of its descendants).
        #
        # Self-parenting is state-independent, so it is decided here. The other
        # two conditions depend on the CURRENT tree, and this check runs before
        # the store lock is taken — so it is only a fast reject. The
        # authoritative parent-exists / cycle test is repeated inside ``_apply``
        # under the lock: two opposite reparents (A into B, B into A) can both
        # pass here against the same pre-state and would otherwise both apply,
        # persisting a cycle that makes both folders unreachable in the tree.
        new_parent = str(body["parent_id"] or "")
        if new_parent:
            if new_parent == fid:
                return web.json_response({"error": "folder cannot be its own parent"}, status=400)
            if not any(f["id"] == new_parent for f in state._folders):
                return web.json_response({"error": "parent folder not found"}, status=400)
            if _is_descendant(state._folders, ancestor_id=fid, folder_id=new_parent):
                return web.json_response(
                    {"error": "cannot move a folder into its own descendant"},
                    status=400,
                )
        changes["parent_id"] = new_parent
    if "project_dir" in body:
        pd, err = _validate_project_dir(str(body["project_dir"] or "").strip())
        if err:
            return web.json_response({"error": err}, status=400)
        if pd:
            # Off-loop: the shared scan primes runtime paths on first use (round 4).
            conflict = await asyncio.to_thread(_folder_project_overlap_denied, pd)
            if conflict is not None:
                return web.json_response(
                    {"error": conflict, "code": "workspace_overlaps_data_home"}, status=400
                )
        changes["project_dir"] = pd
    if "color" in body:
        # Palette color for the folder glyph. None or empty string clears back
        # to the default gray; anything else must be an allowlisted value.
        raw_color = body["color"]
        color_val = str(raw_color).strip().lower() if raw_color is not None else ""
        if color_val and not _is_valid_folder_color(color_val):
            return web.json_response(
                {
                    "error": "color must be one of the folder palette values",
                    "code": "color_invalid",
                },
                status=400,
            )
        changes["color"] = color_val
    if "tags" in body:
        # Vocabulary-constrained tag list. An empty list clears the folder's
        # tags; anything else must be ids that exist in the tag vocabulary.
        clean_tags, tags_err = _validate_folder_tags(state, body["tags"])
        if tags_err:
            return web.json_response({"error": tags_err, "code": "tags_invalid"}, status=400)
        changes["tags"] = clean_tags
    # All fields validated — apply atomically under the store lock, re-finding
    # the folder there so a concurrent delete cannot resurrect it, and
    # re-deciding the tree-shape rules there so two concurrent reparents cannot
    # each validate against the pre-state and persist a cycle between them.

    def _apply(folders: list[dict[str, Any]]) -> tuple[bool, str]:
        target = next((f for f in folders if f["id"] == fid), None)
        if target is None:
            return False, "not_found"
        # Ownership, decided here for the same reason the cycle rule is: a
        # concurrent reparent can change who the target or the destination
        # belongs to between validation and the write.
        if request_app and _folder_owner_app(target) != request_app:
            return False, "not_owned"
        if reparenting and new_parent:
            if not any(f["id"] == new_parent for f in folders):
                return False, "parent_not_found"
            if _is_descendant(folders, ancestor_id=fid, folder_id=new_parent):
                return False, "cycle"
            dest = next((f for f in folders if f["id"] == new_parent), None)
            if request_app and dest is not None and _folder_owner_app(dest) != request_app:
                return False, "forbidden_parent"
        if (
            reparenting
            and request_app
            and _subtree_holds_foreign_folder(folders, root_id=fid, request_app=request_app)
        ):
            # A move takes the whole subtree with it, so a folder the person
            # nested inside this one would be relocated by an app's write. Only
            # the reparent is gated: a rename, a colour or a collapse changes
            # nothing about where the descendants sit. Checked for a move to the
            # top level too -- "" is still a move.
            return False, "foreign_descendant"
        target.update(changes)
        if not target.get("color"):
            target.pop("color", None)
        if not target.get("tags"):
            # Empty list clears the key entirely, so "absent means no tags"
            # stays the single on-disk representation (mirrors color above).
            target.pop("tags", None)
        return True, ""

    if "tags" in changes:
        # Same point-of-application rule as create: the authoritative
        # intersection and the store write are one critical section under
        # ``tags_write_lock``, so a concurrent tag deletion cannot slip a
        # just-deleted id past the strip pass and back onto this folder.

        async with tags_write_lock(state):
            refreshed, _ = _validate_folder_tags(state, changes["tags"])
            changes["tags"] = refreshed if refreshed is not None else []
            err = await state.mutate_folders(_apply)
    else:
        err = await state.mutate_folders(_apply)
    if err == "not_found":
        # Deleted between the validation above and acquiring the store lock.
        return web.json_response({"error": "not found", "code": "folder_not_found"}, status=404)
    if err in ("not_owned", "forbidden_parent", "foreign_descendant"):
        # Distinguished in the audit, not to the caller: one code for all three
        # keeps the response from reporting which folder was foreign.
        _reason = {
            "not_owned": "app cannot change a folder it does not own",
            "forbidden_parent": "app cannot move a folder into one it does not own",
            "foreign_descendant": "app cannot move a folder holding one it does not own",
        }[err]
        sel().log_api_access(
            caller=request_app,
            operation="chat.folder_update",
            outcome="denied",
            source="app_isolation",
            resources=(f"parent={new_parent}" if err == "forbidden_parent" else fid),
            error=_reason,
        )
        return web.json_response(
            {
                "error": "this app does not own that folder",
                "code": "folder_not_owned",
            },
            status=403,
        )
    if err == "parent_not_found":
        # The parent was deleted while this request waited for the lock.
        return web.json_response(
            {"error": "parent folder not found", "code": "folder_parent_not_found"},
            status=400,
        )
    if err == "cycle":
        # A concurrent reparent moved the target under this folder while this
        # request waited for the lock; applying it now would persist a cycle.
        return web.json_response(
            {
                "error": "cannot move a folder into its own descendant",
                "code": "folder_cycle",
            },
            status=409,
        )
    state.push_slots_update()
    source, caller = _audit_origin(request)
    sel().log_api_access(
        caller=caller,
        operation="chat.folder_update",
        outcome="allowed",
        source=source,
        resources=fid,
    )
    return web.json_response(folder)


async def api_chat_folder_delete(request: web.Request) -> web.Response:
    """DELETE /api/chat/folders/{id} — delete a folder, ungroup its slots."""

    state: DashboardState = request.app["state"]
    if (refusal := _refuse_unattributable_caller(state, request)) is not None:
        return refusal
    fid = request.match_info["id"]
    target = next((f for f in state._folders if f["id"] == fid), None)
    if target is None:
        return web.json_response({"error": "not found"}, status=404)
    request_app = _effective_request_app(state, request)
    # Answered before a single slot is unfiled, so the common refusal costs no
    # rollback. Sound pre-lock because ``owner_app`` is stamped at create and no
    # route can reassign it — unlike the child test in ``_remove``, this answer
    # cannot go stale while the request runs.
    if request_app and _folder_owner_app(target) != request_app:
        sel().log_api_access(
            caller=request_app,
            operation="chat.folder_delete",
            outcome="denied",
            source="app_isolation",
            resources=fid,
            error="app cannot delete a folder it does not own",
        )
        return web.json_response(
            {"error": "this app does not own that folder", "code": "folder_not_owned"},
            status=403,
        )
    # An app may not delete a folder at all -- not even an empty one it owns.
    #
    # This is the smallest rule that is actually enforceable. A delete relocates
    # everything the folder contains, and a folder's contents live in a DIFFERENT
    # store from the folder: sessions are in the slot table and the session
    # archive, neither of which shares a lock with the folder store. So "is this
    # folder empty?" cannot be answered atomically with the removal, and every
    # narrower rule leaked through a different seam -- a session filed while the
    # archive scan awaited, a child created while the lock was acquired, a
    # session closing after the scan and writing its folder_id on the way out.
    # Each was closable in isolation; the class was not.
    #
    # Nothing shipped loses a capability: no MCP tool exposes folder deletion
    # (the set is chat_folder_tree / chat_folder_create / chat_folder_move /
    # chat_folder_move_session), and the only client of this route is the
    # dashboard UI, which is the person. An app organizes its own work by
    # creating, renaming and reparenting its folders and filing its sessions --
    # cleanup is the person's, who can delete a full folder as they always could.
    if request_app:
        sel().log_api_access(
            caller=request_app,
            operation="chat.folder_delete",
            outcome="denied",
            source="app_isolation",
            resources=fid,
            error="app cannot delete folders",
        )
        return web.json_response(
            {
                "error": (
                    "an app cannot delete folders - ask the person, or move your "
                    "sessions out and leave the folder"
                ),
                "code": "folder_delete_forbidden",
            },
            status=403,
        )
    # Unfile the folder's slots first, then commit the folder removal. If that
    # commit fails, put the slots back: otherwise the delete half-lands —
    # conversations persistently unfiled while the folder they came from is
    # still there. Restoring is order-neutral, which matters because either
    # ordering leaves a partial-commit window on its own (folder-first strands a
    # dangling folder_id; slots-first strands unfiled conversations), and only
    # undoing the half that did land closes both.
    unfiled: list[tuple[Any, str]] = []
    for slot in state._slots.values():
        if slot.folder_id == fid:
            unfiled.append((slot, slot.folder_id))
            # Pin the write to the transcript this iteration's membership
            # check covered: the save awaits inside the loop, so a rebind can
            # land mid-persist and the save would otherwise resolve its
            # target from the moved routing at write time. No await between
            # this capture and the unfile below.
            authorized_history_key = slot_history_key(slot)
            slot.folder_id = ""
            if not await save_slot_off_loop(
                state, slot, force=True, expected_history_key=authorized_history_key
            ):
                # Refused without writing (session permanently deleted or
                # rebound mid-persist). The in-memory unfile stands — the
                # folder is being removed — so mark dirty and let the
                # periodic flush persist wherever the slot now routes; a
                # dangling folder_id left on the old transcript is ignored
                # on the next load.
                slot._dirty = True
                logger.warning(
                    "folder delete: unfile save refused for %s "
                    "(session deleted or rebound); marked dirty for "
                    "periodic-flush retry",
                    getattr(slot, "key", "?"),
                )

    async def _restore_unfiled() -> None:
        for slot, previous in unfiled:
            # Only put back a slot that is STILL unfiled. Between the unfile
            # above and this rollback the user can move that conversation
            # somewhere else, and their move is the newer intent — restoring
            # `previous` unconditionally would discard it and, worse, file the
            # slot back into the folder this request was trying to delete.
            if slot.folder_id:
                continue
            # Same pin as the unfile: no await between this capture and the
            # restore below, so the rollback write cannot land on a
            # transcript this slot was rebound to mid-restore.
            authorized_history_key = slot_history_key(slot)
            slot.folder_id = previous
            try:
                applied = await save_slot_off_loop(
                    state,
                    slot,
                    force=True,
                    expected_history_key=authorized_history_key,
                )
            except Exception:
                # Best-effort restore; a slot left unfiled renders at the top
                # level, which the sidebar handles, so keep restoring the rest.
                logger.warning(
                    "folder delete rollback: could not restore slot %s to folder %s",
                    slot.key,
                    previous,
                    exc_info=True,
                )
            else:
                if not applied:
                    # Refused without writing (session deleted or rebound).
                    # Keep the restored live field and mark dirty so the
                    # periodic flush persists it wherever the slot now routes.
                    slot._dirty = True
                    logger.warning(
                        "folder delete rollback: restore save refused for %s "
                        "(session deleted or rebound); marked dirty for "
                        "periodic-flush retry",
                        getattr(slot, "key", "?"),
                    )
        state.push_slots_update()

    def _remove(folders: list[dict[str, Any]]) -> tuple[bool, None]:
        for f in folders:
            if f.get("parent_id") == fid:
                f["parent_id"] = ""
        # In place, not a rebind: mutate_folders snapshots the list object it
        # was given, and other holders of state._folders must see the removal.
        folders[:] = [f for f in folders if f["id"] != fid]
        return True, None

    try:
        await state.mutate_folders(_remove)
    except Exception:
        await _restore_unfiled()
        raise
    state.push_slots_update()
    source, caller = _audit_origin(request)
    sel().log_api_access(
        caller=caller,
        operation="chat.folder_delete",
        outcome="allowed",
        source=source,
        resources=fid,
    )
    return web.json_response({"ok": True})


def _effective_request_app(state: DashboardState, request: web.Request) -> str:
    """App identity to enforce ownership against, or "" for the dashboard user.

    Reads the claim ``token_auth_middleware`` publishes, and re-derives through
    the SAME shared rule (``token_auth.derive_caller_app``) when it is absent.

    This route used to carry its own copy of the derivation, because the
    internal-secret transport (the managed MCP set) carries no app claim of its
    own and this was the only route compensating. The middleware now derives it
    once for every route on that transport (issue #3690); the re-derivation here
    is defense-in-depth for a caller that reaches the handler without having
    passed that branch, and it calls the shared function rather than restating
    the rule so the two can never disagree.

    Never read from request BODY or tool arguments — a caller that could name
    its own scope could name someone else's.
    """
    declared = request.get("app", "")
    if declared:
        return str(declared)
    app_name = derive_caller_app(
        getattr(state, "_slots", None),
        request.headers.get("X-Session-Key", ""),
    )
    return app_name


# Per-STATE metadata-write transaction lock for the slot metadata PATCH
# endpoints (folder / pin / mode), same rationale as the autocompact txn lock:
# with awaits inside a mutate/save/rollback span, a second concurrent request
# would otherwise capture the first one's value as its rollback snapshot, and
# value-based rollback cannot tell "my write survived" from "someone else
# wrote the same value" — so a refused request could erase an equal,
# acknowledged concurrent commit. Under the lock exactly one request is inside
# the span, so a rollback can only undo its own write.
#
# Keyed by the STATE, not the transcript: a transcript-keyed lock changes
# identity when the slot is rebound mid-request (review-caught), so a second
# request entering after the rebind would acquire a DIFFERENT lock and the
# spans would interleave anyway. The state key is stable across rebinds and
# covers alias slots resolving onto one file too — the same identity
# chat_tags._TAGS_WRITE_LOCKS uses for the tags writers. These are rare,
# human-driven sidebar operations, so one lock per state does not contend.
_SLOT_META_TXN_LOCKS: "weakref.WeakKeyDictionary[Any, LoopBoundLock]" = weakref.WeakKeyDictionary()


def _slot_meta_txn_lock(state: Any) -> LoopBoundLock:
    lock = _SLOT_META_TXN_LOCKS.get(state)
    if lock is None:
        lock = LoopBoundLock()
        _SLOT_META_TXN_LOCKS[state] = lock
    return lock


async def api_chat_slot_folder(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/folder — assign slot to a folder."""

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    # App ownership (App Kit §5.2) — the same deny-by-default rule
    # api_chat_slot_mode applies, and it matters HERE because filing is a write
    # to a session's own state: refiling moves a foreign session in the sidebar
    # and re-injects its folder breadcrumb on that session's next turn, so an
    # app holding this route could reach a session it does not own. Reported as
    # the same 404 for both reasons on purpose — a distinct code per reason
    # would turn it into an existence oracle for slots the caller cannot see.
    request_app = _effective_request_app(state, request)
    if request_app and getattr(slot, "_app", "") != request_app:
        sel().log_api_access(
            caller=request_app,
            operation="chat.slot_folder",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot.key}",
            error=(
                "app cannot access unscoped slots"
                if not getattr(slot, "_app", "")
                else "app does not own this slot"
            ),
        )
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    # Capture the transcript key the ownership decision above just covered,
    # BEFORE the body-parse await: ``linked_session_key`` is rebound on
    # already-live slots with no ``running`` gate (cron completions, workflow
    # injections), so a slow caller can be authorized against its own session
    # and land on somebody else's conversation. The re-check below and the
    # save's expected_history_key pin together keep this request's write on
    # the transcript it was authorized against.
    authorized_history_key = slot_history_key(slot)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    folder_id = str(body.get("folder_id") or "")
    if folder_id and not any(f["id"] == folder_id for f in state._folders):
        return web.json_response({"error": "folder not found"}, status=400)
    # Serialize the whole re-check/mutate/persist/rollback span under the
    # state-wide metadata txn lock (rebind-stable; see _slot_meta_txn_lock):
    # with awaits inside the span, a second concurrent request would capture
    # this one's value as its rollback snapshot, and value-based rollback
    # cannot tell "my write survived" from "someone else wrote the same
    # value". Under the lock a rollback can only undo its own write; the
    # compare-and-set below stays as defense for the non-endpoint writers
    # (the folder-delete unfile loop) that do not take this lock.
    async with _slot_meta_txn_lock(state):
        # Re-authorize after the awaits above (body parse, lock acquisition):
        # same slot OBJECT still registered under the name, routing still on
        # the transcript captured before the first await. No await between
        # this check and the mutation below; the _unhide_folder and persist
        # awaits after it are covered by the save's pin.
        if state._slots.get(name) is not slot or slot_history_key(slot) != authorized_history_key:
            source, caller = _audit_origin(request)
            sel().log_api_access(
                caller=caller,
                operation="chat.slot_folder",
                outcome="denied",
                source=source,
                resources=name,
                error="session was deleted or rebound",
            )
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
            )
        previous = slot.folder_id
        previous_changed = slot._folder_changed
        if folder_id != slot.folder_id:
            slot._folder_changed = True  # re-inject [FOLDER] breadcrumb on next turn
        slot.folder_id = folder_id
        # The check above reads the store unlocked, so a delete can land between it
        # and here. _unhide_folder re-checks existence under the store lock, which
        # is the only place the answer cannot go stale — reject rather than persist a
        # placement into a folder that no longer exists.
        if not await _unhide_folder(state, folder_id):
            slot.folder_id = previous
            slot._folder_changed = previous_changed
            return web.json_response(
                {"error": "folder not found", "code": "folder_not_found"}, status=400
            )
        if not await save_slot_off_loop(
            state, slot, force=True, expected_history_key=authorized_history_key
        ):
            # Refused without writing: the session was permanently deleted or
            # rebound mid-persist. Roll back the live fields — but only while
            # they still hold THIS request's value: a non-endpoint writer may
            # have committed a newer placement that an unconditional restore
            # would erase (the same guard _restore_unfiled applies).
            if slot.folder_id == folder_id:
                slot.folder_id = previous
                slot._folder_changed = previous_changed
            # The UNPINNED periodic flush may have persisted the provisional
            # value while this save awaited (review-caught): mark dirty so the
            # next flush reconverges the durable record to the live state.
            slot._dirty = True
            source, caller = _audit_origin(request)
            sel().log_api_access(
                caller=caller,
                operation="chat.slot_folder",
                outcome="denied",
                source=source,
                resources=name,
                error="session was deleted or rebound",
            )
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
            )
    state.push_slots_update()
    source, caller = _audit_origin(request)
    sel().log_api_access(
        caller=caller,
        operation="chat.slot_folder",
        outcome="allowed",
        source=source,
        resources=name,
    )
    return web.json_response({"ok": True, "folder_id": slot.folder_id})


async def api_chat_slot_pin(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/pin — toggle pinned state."""

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    # Capture the transcript key the lookup above just covered, BEFORE the
    # body-parse await — the same rebind window api_chat_slot_folder
    # documents. The re-check below and the save's expected_history_key pin
    # together keep this request's write on the transcript it was authorized
    # against.
    authorized_history_key = slot_history_key(slot)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # Serialize the re-check/mutate/persist/rollback span under the
    # state-wide metadata txn lock — same rationale as api_chat_slot_folder.
    async with _slot_meta_txn_lock(state):
        # Re-authorize after the awaits above (body parse, lock acquisition):
        # same slot OBJECT still registered under the name, routing still on
        # the transcript captured before the first await. No await between
        # this check and the save dispatch.
        if state._slots.get(name) is not slot or slot_history_key(slot) != authorized_history_key:
            sel().log_api_access(
                caller="dashboard",
                operation="chat.slot_pin",
                outcome="denied",
                source="dashboard",
                resources=name,
                error="session was deleted or rebound",
            )
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
            )
        prior_pinned = slot.pinned
        new_pinned = body.get("pinned", False)
        # Do not use Python truthiness for API booleans: JSON strings such as
        # "false" are non-empty and therefore truthy.  The sibling metadata
        # fields validate their types before mutating; pin must do the same.
        if not isinstance(new_pinned, bool):
            return web.json_response(
                {"error": "pinned must be a boolean", "code": "pinned_not_bool"}, status=400
            )
        slot.pinned = new_pinned
        if not await save_slot_off_loop(
            state, slot, force=True, expected_history_key=authorized_history_key
        ):
            # Refused without writing: the session was permanently deleted or
            # rebound mid-persist. Roll back the live field — but only while
            # it still holds THIS request's value, so a non-endpoint writer's
            # newer commit is not erased.
            if slot.pinned == new_pinned:
                slot.pinned = prior_pinned
            # The UNPINNED periodic flush may have persisted the provisional
            # value while this save awaited (review-caught): mark dirty so the
            # next flush reconverges the durable record to the live state.
            slot._dirty = True
            sel().log_api_access(
                caller="dashboard",
                operation="chat.slot_pin",
                outcome="denied",
                source="dashboard",
                resources=name,
                error="session was deleted or rebound",
            )
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
            )
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.slot_pin",
        outcome="allowed",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True, "pinned": slot.pinned})


_VALID_MODES = ("", "orchestrator", "crew")


async def api_chat_slot_mode(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/mode — switch session mode."""

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    # Capture the transcript key the lookup above just covered, BEFORE the
    # body-parse and busy-check awaits — the same rebind window
    # api_chat_slot_folder documents. The re-check before the mutation and
    # the save's expected_history_key pin together keep this request's write
    # on the transcript it was authorized against.
    authorized_history_key = slot_history_key(slot)
    # App ownership (App Kit §5.2) — the same deny-by-default rule api_chat_send
    # and api_chat_slot_create apply, and it matters HERE because the mode
    # decides which execution model a session runs under: an app holding
    # `/api/chat` could otherwise list a foreign slot and PATCH it into (or out
    # of) crew mode, changing a session it does not own. One code for both
    # reasons on purpose — a distinct code per reason would turn this 404 into an
    # existence oracle for slots the caller may not know about.
    request_app = request.get("app", "")
    if request_app and getattr(slot, "_app", "") != request_app:
        sel().log_api_access(
            caller=request_app,
            operation="chat.slot_mode",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot.key}",
            error=(
                "app cannot access unscoped slots"
                if not getattr(slot, "_app", "")
                else "app does not own this slot"
            ),
        )
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    mode = body.get("mode", "")
    if mode not in _VALID_MODES:
        return web.json_response({"error": "invalid mode"}, status=400)
    # Member DM threads (mode="member") are pinned to their crew, and every
    # pin guard is conditioned on this very field — so the mode writer is the
    # one door that would unlock all of them at once (PATCH mode -> "", then
    # the agent switch endpoint passes its guard). "member" is deliberately
    # absent from _VALID_MODES (mode cannot be SET here), and here it cannot
    # be UNSET either: member slots are born and retired only through the
    # member-thread endpoint.
    if slot.mode == "member":
        return web.json_response(
            {"error": "member thread mode is locked", "code": "member_mode_locked"},
            status=409,
        )
    # A crew-bound (remote) session runs PLAIN chat only — the same rule
    # api_chat_slot_create enforces at birth, applied here to the post-create
    # switch that would otherwise reopen it. A non-plain mode (crew,
    # orchestrator, design-critique) is consumed by an earlier dispatch branch in
    # api_chat that runs its tools and filesystem work on THIS machine, not on the
    # peer the session is bound to (finding F3). Keyed on ``executor`` rather than
    # ``is_remote`` so even a half-bound slot can never be switched into one.
    if slot.executor == "remote" and mode:
        return web.json_response(
            {
                "error": "a crew-bound session runs plain chat only; mode-specific work runs on the crew, not here",
                "code": "remote_mode_unsupported",
            },
            status=409,
        )
    # Crew keeps its durable queue in a directory named after the slot, and a
    # key that folds to nothing but dots has no such directory (see
    # `CrewStore`). That refusal would otherwise land on the first crew MESSAGE
    # — an unhandled 500 on a tab the switch had already reported as crew, and
    # on every message after it. Refuse the switch instead, while it is still a
    # request with an answer.
    # Deferred import: this module is reachable from the gateway's boot path
    # (gateway -> kiro_crew.dashboard -> chat_folders), and crew is a
    # dashboard-only subsystem, so importing it at module scope made
    # `--no-dashboard` pay for it before the API was ready to serve. Inside a
    # mode-switch handler the cost is a sys.modules hit.
    from kiro_crew.crew_chat import CrewOrchestrator, is_crew_capable_slot_key

    if mode == "crew" and not is_crew_capable_slot_key(slot.key):
        return web.json_response(
            {"error": "this session name cannot run crew mode", "code": "crew_unsupported_slot"},
            status=400,
        )
    # Serialize the busy-check/re-check/mutate/persist/rollback span under
    # the state-wide metadata txn lock — same rationale as
    # api_chat_slot_folder. The busy guard runs INSIDE the lock: waiting on a
    # concurrent metadata save can take long enough for a turn to start, so a
    # guard evaluated before the acquisition would be stale by the time the
    # mutation runs (review-caught).
    async with _slot_meta_txn_lock(state):
        # Re-authorize after the awaits above (body parse, lock acquisition):
        # same slot OBJECT still registered under the name, routing still on
        # the transcript captured before the first await.
        if state._slots.get(name) is not slot or slot_history_key(slot) != authorized_history_key:
            sel().log_api_access(
                caller="dashboard",
                operation="chat.slot_mode",
                outcome="denied",
                source="dashboard",
                resources=name,
                error="session was deleted or rebound",
            )
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
            )
        # Work in SUBAGENTS keeps `slot.running` false the whole time, so that
        # flag alone lets the mode flip mid-flight and interleave two execution
        # models in one session. Two separate questions are needed, because the
        # risk is not symmetric:
        #  * ANY direction — a plain-chat subagent may be running on this slot
        #    right now, and its completion follows the default `_run_chat`
        #    path, so ENTERING crew mode has to be refused for that too, not
        #    just leaving it. (Gating the whole check on `slot.mode == "crew"`
        #    missed exactly this.)
        #  * LEAVING crew — the orchestrator may still hold crew topics or a
        #    live queue, which only it can answer for.
        busy = False
        subs = getattr(state, "subagents", None)
        if subs is not None:
            try:
                # The key the SPAWN ran under, which for a channel-linked slot
                # is the channel session, not `dashboard:<tab>` —
                # `has_pending_work_for` matches `parent_session_key` exactly,
                # so deriving it differently here reports "idle" while that
                # slot's subagents are still running and flips the execution
                # model out from under them.
                busy = bool(subs.has_pending_work_for(effective_session_key(slot)))
            except Exception:
                busy = True  # fail closed: refuse rather than risk the flip
        if not busy and slot.mode == "crew":
            # isinstance, not `is not None` — matching gateway.py's own check
            # on this attribute. A stand-in object passes an identity check and
            # then answers `has_live_work` with something truthy, refusing a
            # switch that is fine.
            crew = getattr(state, "crew", None)
            if isinstance(crew, CrewOrchestrator):
                try:
                    busy = bool(await crew.has_live_work(name))
                except Exception:
                    busy = True
        if slot.running or busy:
            sel().log_api_access(
                caller="dashboard",
                operation="chat.slot_mode",
                outcome="denied",
                source="dashboard",
                resources=name,
            )
            return web.json_response(
                {"error": "cannot switch mode while session is running"}, status=409
            )
        prior_mode = slot.mode
        prior_auto_run = getattr(slot, "_auto_run", False)
        slot.mode = mode
        # Clear orchestrator auto-run flag when leaving orchestrator mode to
        # prevent stale "Go All" state from triggering on re-entry.
        if mode != "orchestrator" and getattr(slot, "_auto_run", False):
            slot._auto_run = False
        if not await save_slot_off_loop(
            state, slot, force=True, expected_history_key=authorized_history_key
        ):
            # Refused without writing: the session was permanently deleted or
            # rebound mid-persist. Roll back the live fields — but only while
            # the mode still holds THIS request's value, so a non-endpoint
            # writer's newer commit is not erased.
            if slot.mode == mode:
                slot.mode = prior_mode
                slot._auto_run = prior_auto_run
            # The UNPINNED periodic flush may have persisted the provisional
            # value while this save awaited (review-caught): mark dirty so the
            # next flush reconverges the durable record to the live state.
            slot._dirty = True
            sel().log_api_access(
                caller="dashboard",
                operation="chat.slot_mode",
                outcome="denied",
                source="dashboard",
                resources=name,
                error="session was deleted or rebound",
            )
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
            )
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.slot_mode",
        outcome="allowed",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True, "mode": slot.mode})
