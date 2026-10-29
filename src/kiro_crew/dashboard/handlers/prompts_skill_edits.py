"""One skill's detail and edits, its pin and inject toggles, and creating a skill.

``GET``, ``PUT`` and ``DELETE /api/skills/{name}``, the pin and inject-on-trigger
toggles and ``POST /api/skills``. The PUT compare-and-swap and the pin and
inject-on-trigger toggles hold the one skill write lock.

Served through :mod:`kiro_crew.dashboard.handlers.prompts`, which callers and tests import and patch; this module
never imports it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import threading
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers.prompts_authoring import MAX_PROMPT_NAME_BYTES
from kiro_crew.dashboard.handlers.prompts_caller_gates import (
    _audit_unread,
    _deny_foreign_app_skill_slot,
    _deny_non_owner_skill_operation,
    _sel,
)
from kiro_crew.dashboard.handlers.prompts_paths import _is_path_too_long_error
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink, validate_file_path

from ._shared import (
    _capability_manager,
    _get_skills,
    _read_session_key,
    _resolve_skill_root,
    read_skill_file,
)

logger = logging.getLogger("kiro_crew.dashboard.handlers.prompts")


#: Serializes the skill PUT's compare against ``base_hash`` with its write, for
#: the same reason as the prompt lock: the two are separate filesystem calls.
#: The pin and inject-on-trigger toggles and pending-candidate approval rewrite
#: the same SKILL.md, so they take it too; otherwise a write landing between a
#: PUT's compare and its write would be overwritten with both requests
#: reporting success.
_SKILL_WRITE_LOCK = threading.Lock()


def _under_skill_write_lock(fn: Any, *args: Any) -> Any:
    """Run one SKILL.md writer while holding ``_SKILL_WRITE_LOCK``."""
    with _SKILL_WRITE_LOCK:
        return fn(*args)


# ── Skills ──


# Open-standard skill-key territories whose READ path (_resolve_skill_root in
# _shared_skill_roots.py) resolves per-session / per-machine — ``kiro-user/`` against
# ``~/.kiro/skills`` and ``kiro-workspace/`` against ``<project>/.kiro/skills`` —
# while the WRITE handlers (skills.create/update/delete_skill) join the key onto
# a core root. That means the same key names a DIFFERENT file on write than the
# reader was shown. These prefixes are documented read-only in
# api_skills, so the write path refuses them rather than silently writing the
# core-root copy. The literals must match the prefixes _resolve_skill_root and
# _skill_key_roots use so read and write agree on territory. The ``package/``
# prefix is intentionally NOT listed here — that territory is handled separately;
# this guard covers the untracked kiro-user/ and kiro-workspace/ siblings.
READONLY_SKILL_KEY_PREFIXES = ("kiro-user/", "kiro-workspace/")


async def api_skill_pin(request: web.Request) -> web.Response:
    """POST /api/skills/-/pin — body {name, pinned:bool}. Pin/unpin an auto-skill
    so the lifecycle never archives it."""
    denied = _deny_non_owner_skill_operation(request, "skill_pin")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str(body.get("name", "")).strip()
    raw_pinned = body.get("pinned", True)
    if not isinstance(raw_pinned, bool):
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pin",
            tool_kind="skill",
            outcome="rejected",
            metadata={"name": name, "reason": "pinned_not_bool"},
        )
        return web.json_response({"error": "pinned must be a boolean"}, status=400)
    pinned = raw_pinned
    if not name:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pin",
            tool_kind="skill",
            outcome="rejected",
            metadata={"name": name, "reason": "name_required"},
        )
        return web.json_response({"error": "name required"}, status=400)
    try:
        ok = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), _under_skill_write_lock, skills.set_pinned, name, pinned
        )
    except Exception:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pin",
            tool_kind="skill",
            outcome="error",
            metadata={"name": name, "pinned": pinned},
        )
        return web.json_response({"error": "internal error"}, status=500)
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skill_pin",
        tool_kind="skill",
        outcome="ok" if ok else "rejected",
        metadata={"name": name, "pinned": pinned},
    )
    if not ok:
        return web.json_response({"error": "not an auto-skill or not found"}, status=400)
    return web.json_response({"name": name, "pinned": pinned})


async def api_skill_inject_on_trigger(request: web.Request) -> web.Response:
    """POST /api/skills/-/inject-on-trigger — body {name, inject:bool}.

    Opt a skill in or out of full-body injection when its triggers match. The
    edit is a targeted frontmatter line change performed server-side, not a
    round-trip through the skill editor: rebuilding the file from the structured
    form would be a wider write than this needs.

    Every outcome is audited, including the rejections. Turning ``inject`` off
    changes what the agent is guaranteed to see when the skill matches, so "who
    made this skill advisory, and when" has to be answerable.
    """
    denied = _deny_non_owner_skill_operation(request, "skill_inject_on_trigger")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)
    try:
        body = await request.json()
    except Exception:
        body = {}
    # `request.json()` yields whatever the body parsed to, and `[]` / `"x"` / `7`
    # are all valid JSON. Normalize any non-object to an empty one so validation
    # answers with a 400 and a code instead of AttributeError -> 500.
    if not isinstance(body, dict):
        body = {}
    name = str(body.get("name", "")).strip()
    raw_inject = body.get("inject")
    if not isinstance(raw_inject, bool):
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_inject_on_trigger",
            tool_kind="skill",
            outcome="rejected",
            metadata={"name": name, "reason": "inject_not_bool"},
        )
        return web.json_response(
            {"error": "inject must be a boolean", "code": "inject_not_bool"}, status=400
        )
    inject = raw_inject
    if not name:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_inject_on_trigger",
            tool_kind="skill",
            outcome="rejected",
            metadata={"name": name, "reason": "name_required"},
        )
        return web.json_response({"error": "name required", "code": "name_required"}, status=400)
    try:
        ok = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(),
            _under_skill_write_lock,
            skills.set_inject_on_trigger,
            name,
            inject,
        )
    except Exception:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_inject_on_trigger",
            tool_kind="skill",
            outcome="error",
            metadata={"name": name, "inject": inject},
        )
        return web.json_response({"error": "internal error", "code": "internal_error"}, status=500)
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skill_inject_on_trigger",
        tool_kind="skill",
        outcome="ok" if ok else "rejected",
        metadata={"name": name, "inject": inject},
    )
    if not ok:
        return web.json_response(
            {"error": "not found or has no frontmatter", "code": "skill_not_editable"},
            status=400,
        )
    return web.json_response({"name": name, "inject_on_trigger": inject})


def _match_package_row(
    rows: list[dict[str, Any]], name: str, pkg_name: str
) -> dict[str, Any] | None:
    """Pick the capability row a ``package/<...>`` skill key refers to.

    ``key`` is the exact identifier the row was listed under, so it decides
    first. Matching on ``name`` is a LEAF comparison and is only a fallback for
    an edition that keys its rows some other way — two skills can share a leaf
    under different parents (``package/shared-skill`` and ``package/SomePkg/shared-skill``),
    and picking the first leaf match would serve the wrong SKILL.md while looking
    entirely successful.

    So the leaf fallback is used only when it is UNAMBIGUOUS. An ambiguous leaf
    returns ``None`` (the caller 404s) and logs, because a reader who opened one
    skill and silently got another has no way to notice.
    """
    for row in rows:
        if row.get("key") == name:
            return row
    leaf_matches = [row for row in rows if row.get("name") == pkg_name]
    if len(leaf_matches) == 1:
        return leaf_matches[0]
    if leaf_matches:
        logger.warning(
            "skill key %r matches no row key and %d rows by leaf name (%s); "
            "refusing to guess which SKILL.md was meant",
            name,
            len(leaf_matches),
            ", ".join(sorted(str(r.get("key")) for r in leaf_matches)),
        )
    return None


async def api_skill_detail(request: web.Request) -> web.Response:
    """GET/PUT/DELETE /api/skills/{name} — get, update, or delete a skill.

    The URL name reaches the loader unbounded, and the loader joins it onto a
    skills root before its first ``exists()`` / ``is_dir()``. A name the
    filesystem refuses for its LENGTH raises there, which escaped as an uncoded
    500. It is mapped onto the same coded 400 ``POST /api/skills`` answers, through
    the same predicate, rather than a pre-flight length: a skill that is readable
    on disk must stay readable here, and only the host knows which paths fit.
    Every other ``OSError`` keeps its current handling.
    """
    try:
        return await _skill_detail_verbs(request)
    except OSError as exc:
        if not _is_path_too_long_error(exc):
            raise
        if request.method in ("PUT", "DELETE"):
            try:
                _sel().log_tool_invocation(
                    session_key="",
                    agent="api",
                    source="dashboard",
                    tool_name="api_skill_update" if request.method == "PUT" else "api_skill_delete",
                    tool_kind="skill",
                    outcome="rejected",
                    metadata={"name": request.match_info["name"], "reason": "name_too_long"},
                )
            except Exception:  # noqa: BLE001 — the refusal is decided; never 500 on an audit write
                logger.debug("Could not audit skill name_too_long refusal", exc_info=True)
        return web.json_response(
            {"error": "skill name is too long", "code": "name_too_long"}, status=400
        )


async def _skill_detail_verbs(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    name = request.match_info["name"]
    skills = _get_skills(state)

    # Authorize mutating verbs before any territory-specific refusal so every
    # denied owner decision is SEL-audited, including read-only skill names.
    if request.method in ("PUT", "DELETE"):
        operation = "skill_update" if request.method == "PUT" else "skill_delete"
        denied = _deny_non_owner_skill_operation(request, operation)
        if denied is not None:
            return denied

    # Refuse mutating verbs on the open-standard read-only territories. Their
    # READ path resolves per-session / per-machine (project or ~/.kiro/skills),
    # but update_skill/delete_skill would join the key onto a core root — so the
    # write lands in a different file than the reader was shown.
    # Guarding here, before the PUT/DELETE branches and any session-key work,
    # ensures the mutating verb never reaches skills.*; GET is untouched and keeps
    # resolving via _resolve_skill_root. The package/ territory has the same shape.
    if request.method in ("PUT", "DELETE") and name.startswith(READONLY_SKILL_KEY_PREFIXES):
        return web.json_response(
            {
                "error": (
                    f"skill '{name}' is in a read-only territory "
                    "(kiro-user/ and kiro-workspace/ skills are managed on disk, "
                    "not through this endpoint)"
                ),
                "code": "readonly_skill_prefix",
            },
            status=405,
            headers={"Allow": "GET"},
        )

    if request.method == "DELETE":
        # Off the loop: delete_skill walks a pinned parent chain and then rmtrees
        # the skill directory, and update_skill below stages a temp file, carries
        # the ACL and renames it into place. On network-backed storage either can
        # stall long enough to matter to every other session sharing this loop, so
        # both go to a thread the way discover.py already routes the same two calls.
        ok = await asyncio.to_thread(skills.delete_skill, name)
        try:
            _sel().log_tool_invocation(
                session_key="",
                agent="api",
                source="dashboard",
                tool_name="api_skill_delete",
                tool_kind="skill",
                outcome="ok" if ok else "rejected",
                metadata={"name": name},
            )
        except Exception:  # noqa: BLE001 — the mutation is committed; never 500 on an audit write
            logger.debug("Could not audit skill delete outcome", exc_info=True)
        if not ok:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"ok": True})

    if request.method == "PUT":
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        content = body.get("content", "")
        if not content:
            return web.json_response({"error": "content is required"}, status=400)
        # Optional edit base, the same contract as the prompts PUT: the GET
        # hands out ``hash`` (sha256 of SKILL.md), and an editor that presents
        # it as ``base_hash`` is refused with 409 ``content_conflict`` when the
        # file on disk differs from that base. An agent rewrites SKILL.md with
        # no push to the dashboard, so a save built on an older read would
        # otherwise silently revert that edit. A PUT without ``base_hash``
        # writes unconditionally.
        raw_hash = body.get("base_hash")
        if raw_hash is not None and (
            not isinstance(raw_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", raw_hash)
        ):
            return web.json_response(
                {
                    "error": "base_hash (sha256 of the content the edit was based on) is malformed",
                    "code": "base_hash_required",
                },
                status=400,
            )
        base_hash: str | None = raw_hash

        def _compare_and_update() -> str:
            # Serialized so two dashboard saves cannot both verify the same
            # base before either writes. A writer outside this process takes
            # no lock, so the gap between the compare and the write is narrowed
            # to one thread hop rather than closed.
            with _SKILL_WRITE_LOCK:
                if base_hash is not None:
                    current = skills.load_skill(name)
                    if current is None:
                        # An unreadable or refused file cannot be verified
                        # against the base, so it is not written.
                        return "not_found"
                    if hashlib.sha256(current.encode("utf-8")).hexdigest() != base_hash:
                        return "conflict"
                return "ok" if skills.update_skill(name, content) else "not_found"

        result = await asyncio.to_thread(_compare_and_update)
        if result == "conflict":
            try:
                _sel().log_tool_invocation(
                    session_key="",
                    agent="api",
                    source="dashboard",
                    tool_name="api_skill_update",
                    tool_kind="skill",
                    outcome="rejected",
                    metadata={"name": name, "reason": "content_conflict"},
                )
            except Exception:  # noqa: BLE001 — never 500 on an audit write
                logger.debug("Could not audit skill update conflict", exc_info=True)
            return web.json_response(
                {
                    "error": "the skill changed on disk after the edit was started",
                    "code": "content_conflict",
                },
                status=409,
            )
        ok = result == "ok"
        try:
            _sel().log_tool_invocation(
                session_key="",
                agent="api",
                source="dashboard",
                tool_name="api_skill_update",
                tool_kind="skill",
                outcome="ok" if ok else "rejected",
                metadata={"name": name},
            )
        except Exception:  # noqa: BLE001 — the mutation is committed; never 500 on an audit write
            logger.debug("Could not audit skill update outcome", exc_info=True)
        if not ok:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"ok": True})

    # GET
    if name.startswith("kiro-workspace/"):
        session_key = _read_session_key(request)
        denied = _deny_foreign_app_skill_slot(request, state, session_key, "skill_detail")
        if denied is not None:
            return denied
    content = skills.load_skill(name)
    if content is None and name.startswith("package/"):
        pkg_name = name[len("package/") :]  # strip "package/" prefix
        # The capability manager owns skill listing + path resolution; it
        # returns structured rows (no core text parsing / event-loop globbing).
        mgr = _capability_manager()
        try:
            package_skills = await mgr.list_skills() if mgr.available() else []
        except Exception:
            package_skills = []
        row = _match_package_row(package_skills, name, pkg_name)
        if row is not None and row.get("path"):
            resolved = validate_file_path(str(row["path"]))
            if resolved is None:
                return web.json_response({"error": "access denied"}, status=403)
            # Same descriptor gate as the prompt reads in prompts_authoring, for the same reason:
            # canonicalizing a path and then opening that name is two resolutions,
            # and a hardlink needs neither a race nor a link to defeat the first
            # one — it shares its target's inode, so ``realpath`` yields the
            # alias's own name and ``is_sensitive_path`` judges that instead of the
            # file whose bytes come back. The gate opens once with ``O_NOFOLLOW``
            # and refuses ``st_nlink > 1``, a non-regular inode, or an opened
            # descriptor whose real path left the canonical parent (the last of
            # which is what still holds on Windows, where ``O_NOFOLLOW`` does not
            # exist). ``row["path"]`` comes from the capability seam rather than a
            # cloned checkout, so the actor here needs write access to a package
            # skill root — a weaker requirement than the prompt sites, but the same
            # defect and the same fix.
            #
            # A refusal leaves ``content`` unset, which is the 404 an unreadable
            # skill already produced, so nothing is distinguishable from I/O
            # trouble — but it is SEL-recorded, because a 404 is also what a name
            # nobody installed produces and a refusal would otherwise be silent.
            # ``FileTooLargeError`` is not an ``OSError`` and would otherwise
            # escape as an unaudited 500; it is the one refusal whose cause IS
            # knowable, so it is recorded as itself.
            #
            # Off the loop, like the delete and update verbs above: no
            # caller-supplied cap applies here, so the gate reads up to its own
            # 50 MB default from storage that can be network-backed, and this
            # handler shares one event loop with every other session's turn.
            skill_bytes: bytes | None
            try:
                skill_bytes = await asyncio.to_thread(
                    safe_read_file_bytes_nolink, resolved, within_root=os.path.dirname(resolved)
                )
                refusal = "error"
            except FileTooLargeError:
                skill_bytes = None
                refusal = "too_large"
            if skill_bytes is not None:
                content = skill_bytes.decode("utf-8", errors="replace")
            else:
                _audit_unread(
                    "api_skill_detail", "skill", refusal, {"name": name, "path": str(row["path"])}
                )
    if content is None and (name.startswith("kiro-user/") or name.startswith("kiro-workspace/")):
        # Open-standard kiro-cli skills are read-only here — load via the
        # same path-resolution logic used by the tree/file endpoints so the
        # detail modal can fetch SKILL.md regardless of which root the
        # skill lives in.
        session_key = _read_session_key(request)

        def _resolve_and_read_md() -> str | None:
            # One filesystem transaction on the discovery pool (see api_skill_tree).
            r = _resolve_skill_root(name, state, session_key)
            if r is None:
                return None
            c, e = read_skill_file(r, "SKILL.md")
            return c if e is None else None

        content_value = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), _resolve_and_read_md
        )
        if content_value is not None:
            content = content_value
    if content is None:
        return web.json_response({"error": "not found"}, status=404)
    # ``hash`` is the edit base a PUT presents as ``base_hash``.
    return web.json_response(
        {
            "name": name,
            "content": content,
            "hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
    )


async def api_skills_create(request: web.Request) -> web.Response:
    """POST /api/skills — create a new skill."""
    denied = _deny_non_owner_skill_operation(request, "skill_create")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    name = body.get("name", "").strip()
    content = body.get("content", "").strip()
    if not name:
        return web.json_response({"error": "name is required", "code": "name_required"}, status=400)
    if not content:
        return web.json_response(
            {"error": "content is required", "code": "content_required"}, status=400
        )
    # Sanitize name: lowercase, alphanumeric + hyphens + slashes for nesting
    safe_name = re.sub(r"[^a-z0-9\-/]", "-", name.lower()).strip("-").strip("/")
    safe_name = re.sub(r"/+", "/", safe_name)  # collapse multiple slashes
    if not safe_name:
        return web.json_response(
            {"error": "invalid skill name", "code": "invalid_name"}, status=400
        )
    # Sanitizing imposes no length bound, so bound it here — on the WHOLE name,
    # since nesting (``a/b/c``) blows PATH_MAX or mkdir's recursion on short segments.
    if len(safe_name.encode("utf-8")) > MAX_PROMPT_NAME_BYTES:
        return web.json_response(
            {"error": "skill name is too long", "code": "name_too_long"}, status=400
        )
    # Refuse creating into the open-standard read-only territories. Checked on
    # the SANITISED name because that is what create_skill would write (e.g.
    # 'Kiro-Workspace/Foo' sanitises to 'kiro-workspace/foo'). create_skill joins
    # the key onto a core root, but the reader is served kiro-user/ and
    # kiro-workspace/ skills from a session/machine-scoped location — so a create
    # here would write to a different file than the reader is shown.
    if safe_name.startswith(READONLY_SKILL_KEY_PREFIXES):
        return web.json_response(
            {
                "error": (
                    f"skill name '{safe_name}' is in a reserved read-only territory "
                    "(kiro-user/ and kiro-workspace/ skills are managed on disk)"
                ),
                "code": "reserved_skill_prefix",
            },
            status=400,
        )
    skills = _get_skills(state)

    def _audit(outcome: str, **extra: str) -> None:
        try:
            _sel().log_tool_invocation(
                session_key="",
                agent="api",
                source="dashboard",
                tool_name="api_skills_create",
                tool_kind="skill",
                outcome=outcome,
                metadata={"name": safe_name, **extra},
            )
        except Exception:  # noqa: BLE001 — the outcome is decided; never 500 on an audit write
            logger.debug("Could not audit skill create outcome", exc_info=True)

    # Off the loop for the same reason api_skill_detail offloads its two calls:
    # create_skill walks a pinned parent chain and writes the SKILL.md.
    try:
        ok = await asyncio.to_thread(skills.create_skill, safe_name, content)
    except OSError as exc:
        if not _is_path_too_long_error(exc):
            raise
        # The name fit its budget but ``<root>/<name>/SKILL.md`` did not fit this
        # host: a request-shape refusal, answered like the name bound's, and
        # audited because the write reached the filesystem.
        _audit("rejected", code="name_too_long")
        return web.json_response(
            {"error": "skill name is too long for its destination path", "code": "name_too_long"},
            status=400,
        )
    _audit("ok" if ok else "rejected")
    if not ok:
        return web.json_response(
            {"error": f"skill '{safe_name}' already exists", "code": "skill_exists"}, status=409
        )
    return web.json_response({"ok": True, "name": safe_name})
