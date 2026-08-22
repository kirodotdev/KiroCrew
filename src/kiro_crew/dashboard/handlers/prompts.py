"""Prompts (Agent SOPs) and Skills API handlers."""

from __future__ import annotations

import asyncio
import functools
import logging
import re
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.skill_trust import (
    TrustStoreUnreadable,
    canonical_key,
    grant_project_trust,
    is_project_trusted,
    list_trusted_projects,
    revoke_project_trust,
)

from ._shared import (
    _capability_manager,
    _get_skills,
    _read_session_key,
    _resolve_skill_root,
    active_project_dir,
    collect_skills_blocking,
    list_skill_tree,
    read_skill_file,
    requesting_slot_project,
)


def _list_aim_prompts():
    """Import from parent to avoid circular — cache lives in __init__.py for test compat."""
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg._list_aim_prompts()


logger = logging.getLogger(__name__)

MAX_PROMPT_BYTES = 100_000  # 100 KB — public constant, imported across dashboard + gateway + tests


def _sel():
    """Late-binding sel() — allows monkeypatching at parent package level."""
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg.sel()


# ── Prompts (Agent SOPs) ──


def _extract_sop_description(path: Path) -> str:
    """Extract description from SOP frontmatter or first heading."""
    from kiro_crew.skills import SkillsLoader

    try:
        meta = SkillsLoader._parse_frontmatter(path)
    except (OSError, ValueError):
        return ""
    if meta.get("description"):
        return meta["description"]
    # Fall back to first heading
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return re.sub(r"^#+\s*", "", stripped).strip()
    except OSError:
        pass
    return ""


def _redact_prompt(p: dict[str, Any]) -> None:
    """Redact credential patterns and exfiltration URLs from prompt metadata."""
    for field in ("description", "path"):
        p[field], _ = redact_credentials(p[field])
        p[field], _ = redact_exfiltration_urls(p[field])


async def api_prompts(request: web.Request) -> web.Response:
    """GET /api/prompts — list available prompts and agent SOPs."""
    # _list_aim_prompts() walks the edition package tree (rglob *.sop.md +
    # per-file resolve/read + frontmatter parse) on a cold cache — blocking FS
    # work that can stall the event loop on a large tree. It has a 5s TTL cache,
    # but the cold/expired build must run off the loop. (The cache lives in the
    # parent package; the executor call still benefits from it on warm builds.)
    prompts = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _list_aim_prompts
    )
    home = str(Path.home())
    for p in prompts:
        _redact_prompt(p)
        p["path"] = p["path"].replace(home, "~")
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_prompts_list",
        tool_kind="prompt",
        outcome="ok",
        metadata={"count": len(prompts)},
    )
    return web.json_response(prompts)


def _find_prompt(raw_name: str) -> dict[str, Any] | None:
    """Resolve a prompt by bare name, fullName, or ``package/name``."""
    pkg_filter = ""
    name = raw_name
    if "/" in raw_name:
        pkg_filter, name = raw_name.split("/", 1)
    for p in _list_aim_prompts():
        if pkg_filter and p["package"] != pkg_filter:
            continue
        if p["name"] == name or p["fullName"] == name:
            return p
    return None


async def api_prompt_detail(request: web.Request) -> web.Response:
    """GET /api/prompts/{name} — read a prompt/SOP file."""
    raw = request.match_info["name"]
    # _find_prompt() → _list_aim_prompts() does an rglob('*.sop.md') walk over the
    # (possibly large / edition-provided) prompt roots on a cold/expired cache;
    # offload it so a slow FS can't stall the event loop.
    p = await asyncio.get_running_loop().run_in_executor(discovery_executor(), _find_prompt, raw)
    if not p:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_prompt_detail",
            tool_kind="prompt",
            outcome="not_found",
            metadata={"name": raw},
        )
        return web.json_response({"error": "not found"}, status=404)
    name = raw.split("/", 1)[-1] if "/" in raw else raw
    from kiro_crew.hooks import validate_file_path  # noqa: F811

    resolved = validate_file_path(p["path"])
    if resolved is None:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_prompt_detail",
            tool_kind="prompt",
            outcome="blocked",
            metadata={"name": name, "path": p["path"]},
        )
        return web.json_response({"error": "access denied"}, status=403)
    try:
        path = Path(resolved)
        if path.stat().st_size > MAX_PROMPT_BYTES:
            _sel().log_tool_invocation(
                session_key="",
                agent="api",
                source="dashboard",
                tool_name="api_prompt_detail",
                tool_kind="prompt",
                outcome="too_large",
                metadata={"name": name, "path": p["path"]},
            )
            return web.json_response({"error": "file too large"}, status=413)
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_prompt_detail",
            tool_kind="prompt",
            outcome="error",
            metadata={"name": name, "path": p["path"]},
        )
        return web.json_response({"error": "file not readable"}, status=500)
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_prompt_detail",
        tool_kind="prompt",
        outcome="ok",
        metadata={"name": name, "path": p["path"]},
    )
    content, _ = redact_credentials(content)
    content, _ = redact_exfiltration_urls(content)
    out = dict(p)
    _redact_prompt(out)
    # Strip full filesystem path — return display-only relative path
    out["path"] = out["path"].replace(str(Path.home()), "~")
    return web.json_response({**out, "name": name, "content": content})


# ── Skills ──


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
    still be loaded via KiroCrew text-injection or an external MCP server).
    """
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)
    # Resolve the active project dir (cheap in-memory scan of slots) on the loop.
    # Scoped to the requesting chat slot: without the key, two chats on
    # different projects made this fall to None and kiro-workspace skills
    # silently vanished from the listing (#2457).
    # Strict: must match what SkillsLoader will resolve for THIS chat, or the
    # catalog advertises a skill whose $token expands to nothing.
    project_dir: Path | None = requesting_slot_project(state, _read_session_key(request))
    # Run the edition capability lookup async (on the loop, non-blocking), then offload ALL
    # blocking filesystem work — kirocrew list_skills() (os.walk + per-file
    # frontmatter reads), package path globs, kiro per-skill resolve/read, and the
    # agent annotation — onto the dedicated DISCOVERY pool in one job. This work
    # would stall the event loop past the loop-stall watchdog (~25s) on large
    # skills×agents catalogs if run on-loop. Use the discovery pool
    # (NOT maintenance_executor): this scan is browser-triggerable and can be
    # seconds-long, so the maintenance pool would let a few dashboard tabs
    # occupy the workers the orphan-reaper sweeps need to recover from a wedge
    # (see kiro_crew.executors). No result cache: the endpoint always reflects
    # current on-disk state, so freshly created/installed skills appear
    # immediately (correctness over the latency a cache would add).
    mgr = _capability_manager()
    try:
        package_skills = await mgr.list_skills() if mgr.available() else []
    except Exception:
        # The capability manager is one of three skill sources; degrade to "no
        # package skills" rather than 500 the whole /api/skills endpoint.
        package_skills = []
    result = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(),
        collect_skills_blocking,
        skills,
        package_skills,
        project_dir,
    )
    return web.json_response(result)


class _ReviewedProjectChanged(Exception):
    """The slot's project is no longer the directory the operator reviewed."""


def _grant_reviewed_project(project_dir: Path, expected: object, *, session_key: str) -> str:
    """Confirm *expected* still names *project_dir*, then grant. Blocking.

    Runs on the discovery executor: `canonical_key` realpaths both paths, which is
    one lstat per component, and `grant_project_trust` takes a lock and writes.
    Neither belongs on the event loop.

    *expected* is a CONFIRMATION, never a selector -- the directory granted is
    always the caller's *project_dir*, taken from the requesting slot. A mismatch
    raises rather than silently granting something else, and an absent *expected*
    skips the check so callers that cannot supply it are unaffected (they gain
    nothing by omitting it, since they still cannot choose the directory).

    Canonical comparison rather than string equality: the store keys on the
    canonical form, so that is the identity deciding what loads, and `/tmp/./x`
    would otherwise be a spurious mismatch.
    """
    if expected:
        want = canonical_key(str(expected))
        got = canonical_key(str(project_dir))
        if want is None or got is None or want != got:
            raise _ReviewedProjectChanged(str(expected))
    return grant_project_trust(project_dir, session_key=session_key)


def _trust_snapshot(project_dir: Path | None) -> dict[str, Any]:
    """Blocking read of trust state for *project_dir* plus every stored grant."""
    return {
        "project": str(project_dir) if project_dir else "",
        "trusted": is_project_trusted(project_dir) if project_dir else False,
        "grants": list_trusted_projects(),
    }


async def api_skills_trust(request: web.Request) -> web.Response:
    """Report the requesting chat's project-skills trust state and all grants."""
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

    The body MAY carry ``expected_path`` — the directory the consent dialog
    actually showed. It is a confirmation, never a selector: the directory still
    comes from the slot, and a mismatch is REFUSED rather than honoured. Without
    it, a slot retargeted between rendering the dialog and clicking Trust
    (``set_project``, a worktree switch, another agent turn) would record consent
    for a directory whose name and skills the operator never read.
    """
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
    except Exception:  # noqa: BLE001 — no body is fine; the field is optional
        body = {}
    expected = (body or {}).get("expected_path") if isinstance(body, dict) else None
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
    revoke a grant for a directory they no longer have open (or have deleted)
    from the settings list. Removing trust only ever narrows what loads, so a
    caller-supplied path is safe here.
    """
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
    session_key = _read_session_key(request)

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


# ── Auto-skill pending-approval queue (v2) ──


def _pending_slug_ok(slug: str) -> bool:
    return (
        bool(slug)
        and slug not in (".", "..")
        and not slug.startswith(".")
        and "/" not in slug
        and "\\" not in slug
        and ".." not in slug
    )


async def api_skills_pending(request: web.Request) -> web.Response:
    """GET /api/skills/-/pending — list staged auto-skill candidates."""
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)

    def _prune_and_list() -> list:
        # Opportunistic TTL cleanup on read — gives prune_pending a real caller
        # so stale candidates don't accumulate unbounded.
        try:
            ttl = KiroCrewConfig.load().skills.pending_ttl_days
            skills.prune_pending(ttl)
        except Exception:
            pass
        return skills.list_pending_skills()

    try:
        items = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), _prune_and_list
        )
    except Exception:
        items = []
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skills_pending",
        tool_kind="skill",
        outcome="ok",
        metadata={"count": len(items)},
    )
    return web.json_response({"pending": items})


async def api_skill_pending_detail(request: web.Request) -> web.Response:
    """GET /api/skills/-/pending/{slug} — full candidate incl. body + scripts."""
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)
    slug = request.match_info["slug"]
    if not _pending_slug_ok(slug):
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_detail",
            tool_kind="skill",
            outcome="bad_request",
            metadata={"slug": slug},
        )
        return web.json_response({"error": "invalid slug"}, status=400)
    try:
        detail = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), skills.get_pending_skill, slug
        )
    except Exception:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_detail",
            tool_kind="skill",
            outcome="error",
            metadata={"slug": slug},
        )
        return web.json_response({"error": "internal error"}, status=500)
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skill_pending_detail",
        tool_kind="skill",
        outcome="ok" if detail is not None else "not_found",
        metadata={"slug": slug},
    )
    if detail is None:
        return web.json_response({"error": "not found"}, status=404)
    # Update candidates carry an approval PREVIEW so the UI can show exactly what
    # approving would change: the target's current live body, the proposed
    # post-approval content, and a unified diff between them (computed
    # server-side with difflib so the frontend needs no diff dependency).
    # kind/target may be exposed at the top level or nested under ``meta`` — read
    # defensively. All preview fields are null if the target skill was removed
    # since the candidate was staged.
    _meta = detail.get("meta") if isinstance(detail.get("meta"), dict) else {}
    kind = detail.get("kind") or _meta.get("kind")
    if kind == "update":

        def _preview() -> dict | None:
            try:
                return skills.preview_pending_update(slug)
            except Exception:
                return None

        try:
            pv = await asyncio.get_running_loop().run_in_executor(discovery_executor(), _preview)
        except Exception:
            pv = None
        detail["live_body"] = (pv or {}).get("live_body")
        detail["proposed_body"] = (pv or {}).get("proposed_body")
        detail["diff"] = (pv or {}).get("diff")
        detail["from_version"] = (pv or {}).get("from_version")
        detail["to_version"] = (pv or {}).get("to_version")
        detail["stale_base"] = bool((pv or {}).get("stale_base"))
    return web.json_response(detail)


async def api_skill_pending_approve(request: web.Request) -> web.Response:
    """POST /api/skills/-/pending/{slug}/approve — promote candidate to live."""
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)
    slug = request.match_info["slug"]
    if not _pending_slug_ok(slug):
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_approve",
            tool_kind="skill",
            outcome="rejected",
            metadata={"slug": slug, "reason": "invalid_slug"},
        )
        return web.json_response({"error": "invalid slug"}, status=400)

    def _approve_and_bound() -> str | None:
        # Route on candidate kind: an UPDATE candidate rewrites an existing live
        # skill (approve_pending_update); a NEW candidate is promoted fresh
        # (approve_pending_skill). kind is read from the candidate detail
        # (top-level or nested ``meta``), defaulting to the new path.
        kind = None
        try:
            _detail = skills.get_pending_skill(slug)
        except Exception:
            _detail = None
        if isinstance(_detail, dict):
            _meta_raw = _detail.get("meta")
            _meta: dict = _meta_raw if isinstance(_meta_raw, dict) else {}
            kind = _detail.get("kind") or _meta.get("kind")
        if kind == "update":
            nm = skills.approve_pending_update(slug)
        else:
            nm = skills.approve_pending_skill(slug)
        if nm:
            # Approving consumes a slot — enforce the bound (archive, never
            # delete). Best-effort; runs in the same off-loop executor job.
            # Exempt the just-approved skill so a full-cap pass can't archive the
            # very skill this request promoted (brand-new + zero-hit, it would
            # otherwise rank lowest in the max-N backstop).
            try:
                cfg = KiroCrewConfig.load().skills
                skills.run_skill_lifecycle(
                    max_auto_skills=cfg.max_auto_skills,
                    stale_after_days=cfg.stale_after_days,
                    archive_after_days=cfg.archive_after_days,
                    exempt={nm},
                )
            except Exception:
                pass
        return nm

    try:
        name = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), _approve_and_bound
        )
    except Exception:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_approve",
            tool_kind="skill",
            outcome="error",
            metadata={"slug": slug},
        )
        return web.json_response({"error": "internal error"}, status=500)
    outcome = "ok" if name else "not_found"
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skill_pending_approve",
        tool_kind="skill",
        outcome=outcome,
        metadata={"slug": slug, "name": name or ""},
    )
    if not name:
        return web.json_response(
            {"error": "not found, a live skill already exists, or script validation failed"},
            status=409,
        )
    return web.json_response({"approved": name})


async def api_skill_pending_dismiss(request: web.Request) -> web.Response:
    """POST /api/skills/-/pending/{slug}/dismiss — delete a candidate."""
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)
    slug = request.match_info["slug"]
    if not _pending_slug_ok(slug):
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_dismiss",
            tool_kind="skill",
            outcome="rejected",
            metadata={"slug": slug, "reason": "invalid_slug"},
        )
        return web.json_response({"error": "invalid slug"}, status=400)
    try:
        ok = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), skills.dismiss_pending_skill, slug
        )
    except Exception:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_dismiss",
            tool_kind="skill",
            outcome="error",
            metadata={"slug": slug},
        )
        return web.json_response({"error": "internal error"}, status=500)
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skill_pending_dismiss",
        tool_kind="skill",
        outcome="ok" if ok else "not_found",
        metadata={"slug": slug},
    )
    if not ok:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"dismissed": slug})


async def api_skills_pending_dismiss_all(request: web.Request) -> web.Response:
    """POST /api/skills/-/pending/-/dismiss-all — dismiss pending candidates.

    Accepts an optional JSON body ``{"slugs": ["slug1", ...]}``.  When present,
    only those slugs are dismissed (the client passes the set it displayed to the
    user, so a candidate staged *after* the confirmation dialog is never silently
    deleted).  When the body is absent or ``slugs`` is empty, ALL pending
    candidates are dismissed (back-compat / fallback).
    """
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )
    raw_slugs = body.get("slugs")
    if raw_slugs is not None and (
        not isinstance(raw_slugs, list) or not all(isinstance(s, str) for s in raw_slugs)
    ):
        return web.json_response(
            {"error": "slugs must be an array of strings", "code": "invalid_slugs"}, status=400
        )
    slugs: list[str] = raw_slugs if isinstance(raw_slugs, list) else []
    try:
        if slugs:
            count = await asyncio.get_running_loop().run_in_executor(
                discovery_executor(),
                lambda: skills.dismiss_pending_slugs(slugs),
            )
        else:
            return web.json_response(
                {
                    "error": "slugs array is required and must not be empty",
                    "code": "slugs_required",
                },
                status=400,
            )
    except Exception:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skills_pending_dismiss_all",
            tool_kind="skill",
            outcome="error",
            metadata={},
        )
        return web.json_response({"error": "internal error", "code": "internal_error"}, status=500)
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skills_pending_dismiss_all",
        tool_kind="skill",
        outcome="ok",
        metadata={"count": count},
    )
    return web.json_response({"dismissed_count": count})


async def api_skill_pin(request: web.Request) -> web.Response:
    """POST /api/skills/-/pin — body {name, pinned:bool}. Pin/unpin an auto-skill
    so the lifecycle never archives it."""
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
            discovery_executor(), skills.set_pinned, name, pinned
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
            discovery_executor(), skills.set_inject_on_trigger, name, inject
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
    """GET/PUT/DELETE /api/skills/{name} — get, update, or delete a skill."""
    state: DashboardState = request.app["state"]
    name = request.match_info["name"]
    skills = _get_skills(state)

    if request.method == "DELETE":
        ok = skills.delete_skill(name)
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
        ok = skills.update_skill(name, content)
        if not ok:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"ok": True})

    # GET
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
            from kiro_crew.hooks import validate_file_path  # noqa: F811

            resolved = validate_file_path(str(row["path"]))
            if resolved is None:
                return web.json_response({"error": "access denied"}, status=403)
            try:
                content = Path(resolved).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
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
    return web.json_response({"name": name, "content": content})


async def api_skills_create(request: web.Request) -> web.Response:
    """POST /api/skills — create a new skill."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    name = body.get("name", "").strip()
    content = body.get("content", "").strip()
    if not name:
        return web.json_response({"error": "name is required"}, status=400)
    if not content:
        return web.json_response({"error": "content is required"}, status=400)
    # Sanitize name: lowercase, alphanumeric + hyphens + slashes for nesting
    safe_name = re.sub(r"[^a-z0-9\-/]", "-", name.lower()).strip("-").strip("/")
    safe_name = re.sub(r"/+", "/", safe_name)  # collapse multiple slashes
    if not safe_name:
        return web.json_response({"error": "invalid skill name"}, status=400)
    skills = _get_skills(state)
    ok = skills.create_skill(safe_name, content)
    if not ok:
        return web.json_response({"error": f"skill '{safe_name}' already exists"}, status=409)
    return web.json_response({"ok": True, "name": safe_name})
