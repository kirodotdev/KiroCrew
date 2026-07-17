"""Prompts (Agent SOPs) and Skills API handlers."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

from ._shared import (
    _aim_list_stdout,
    _get_skills,
    _parse_aim_skills,
    _resolve_skill_root,
    collect_skills_blocking,
    list_skill_tree,
    read_skill_file,
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


def _latest_aim_event_dir(pkg_dir: Path) -> Path | None:
    """Return the current eventId-* directory for an AIM package.

    Uses ``.aim/.version-manifest.json`` (authoritative) with a fallback
    to the highest numeric eventId if the manifest is missing.
    """
    # Authoritative: read currentEventId from manifest (same as agent.py)
    manifest = pkg_dir / ".aim" / ".version-manifest.json"
    if manifest.is_file():
        try:
            current = json.loads(manifest.read_text(encoding="utf-8")).get("currentEventId", "")
            if current:
                candidate = pkg_dir / f"eventId-{current}"
                if candidate.is_dir():
                    return candidate
        except (json.JSONDecodeError, OSError):
            pass
    # Fallback: highest numeric eventId
    events = [d for d in pkg_dir.iterdir() if d.is_dir() and d.name.startswith("eventId-")]
    if not events:
        return None

    def _sort_key(d: Path) -> int:
        try:
            return int(d.name.split("-", 1)[1])
        except (IndexError, ValueError):
            return 0

    return max(events, key=_sort_key)


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
    # _list_aim_prompts() walks ~/.aim/packages (rglob *.sop.md + per-file
    # resolve/read + frontmatter parse) on a cold cache — blocking FS work that
    # can stall the event loop on a large ~/.aim tree. It has a 5s TTL cache,
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
        session_key='', agent='api', source='dashboard',
        tool_name='api_prompts_list', tool_kind='prompt', outcome='ok',
        metadata={'count': len(prompts)},
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
    p = _find_prompt(raw)
    if not p:
        _sel().log_tool_invocation(
            session_key='', agent='api', source='dashboard',
            tool_name='api_prompt_detail', tool_kind='prompt', outcome='not_found',
            metadata={'name': raw},
        )
        return web.json_response({"error": "not found"}, status=404)
    name = raw.split("/", 1)[-1] if "/" in raw else raw
    from kiro_crew.hooks import validate_file_path  # noqa: F811
    resolved = validate_file_path(p["path"])
    if resolved is None:
        _sel().log_tool_invocation(
            session_key='', agent='api', source='dashboard',
            tool_name='api_prompt_detail', tool_kind='prompt', outcome='blocked',
            metadata={'name': name, 'path': p['path']},
        )
        return web.json_response({"error": "access denied"}, status=403)
    try:
        path = Path(resolved)
        if path.stat().st_size > MAX_PROMPT_BYTES:
            _sel().log_tool_invocation(
                session_key='', agent='api', source='dashboard',
                tool_name='api_prompt_detail', tool_kind='prompt', outcome='too_large',
                metadata={'name': name, 'path': p['path']},
            )
            return web.json_response({"error": "file too large"}, status=413)
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        _sel().log_tool_invocation(
            session_key='', agent='api', source='dashboard',
            tool_name='api_prompt_detail', tool_kind='prompt', outcome='error',
            metadata={'name': name, 'path': p['path']},
        )
        return web.json_response({"error": "file not readable"}, status=500)
    _sel().log_tool_invocation(
        session_key='', agent='api', source='dashboard',
        tool_name='api_prompt_detail', tool_kind='prompt', outcome='ok',
        metadata={'name': name, 'path': p['path']},
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
    - ``kirocrew``: ``~/.kirocrew/skills/`` (managed by SkillsLoader; editable)
    - ``aim``: skills from an optional ``aim`` CLI, if present (read-only here)
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
    project_dir: Path | None = None
    for slot in getattr(state, "_slots", {}).values():
        pd = getattr(slot, "project_dir", None)
        if pd:
            project_dir = Path(pd)
            break
    # Run the AIM subprocess async (on the loop, non-blocking), then offload ALL
    # blocking filesystem work — kirocrew list_skills() (os.walk + per-file
    # frontmatter reads), AIM path globs, kiro per-skill resolve/read, and the
    # agent annotation — onto the dedicated DISCOVERY pool in one job. This work
    # stalled the event loop past the loop-stall watchdog (~25s) on large
    # skills×agents catalogs before it moved off-loop. Use the discovery pool
    # (NOT maintenance_executor): this scan is browser-triggerable and can be
    # seconds-long, so the maintenance pool would let a few dashboard tabs
    # occupy the workers the orphan-reaper sweeps need to recover from a wedge
    # (see kiro_crew.executors). No result cache: the endpoint always reflects
    # current on-disk state, so freshly created/installed skills appear
    # immediately (correctness over the latency a cache would add).
    aim_stdout = await _aim_list_stdout()
    result = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(),
        collect_skills_blocking,
        skills,
        aim_stdout,
        project_dir,
    )
    return web.json_response(result)


async def api_skill_tree(request: web.Request) -> web.Response:
    """GET /api/skills/{name}/tree — list files within a skill folder.

    Capped at SKILL_TREE_MAX_ENTRIES; sensitive paths and symlinks
    escaping the skill root are omitted.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["name"]
    root = _resolve_skill_root(name, state)
    if root is None:
        _sel().log_tool_invocation(
            session_key='', agent='api', source='dashboard',
            tool_name='api_skill_tree', tool_kind='skill', outcome='not_found',
            metadata={'name': name},
        )
        return web.json_response({"error": "not found"}, status=404)
    entries = list_skill_tree(root)
    # Sanitize the absolute path — never expose the server's real home to the
    # client.  ``root`` is already resolved (symlinks followed), so compare
    # against the *resolved* home too; otherwise a symlinked home (e.g. macOS
    # ``/var`` → ``/private/var``) would mismatch and leak the real path.
    display_root = str(root)
    for home in {str(Path.home()), str(Path.home().resolve())}:
        display_root = display_root.replace(home, "~")
    _sel().log_tool_invocation(
        session_key='', agent='api', source='dashboard',
        tool_name='api_skill_tree', tool_kind='skill', outcome='ok',
        metadata={'name': name, 'root': display_root, 'count': len(entries)},
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
            session_key='', agent='api', source='dashboard',
            tool_name='api_skill_file', tool_kind='skill', outcome=outcome,
            metadata={'name': name, 'path': rel_path},
        )

    if not rel_path:
        _audit('bad_request')
        return web.json_response({"error": "path query param required"}, status=400)
    root = _resolve_skill_root(name, state)
    if root is None:
        _audit('not_found')
        return web.json_response({"error": "not found"}, status=404)
    content, err = read_skill_file(root, rel_path)
    if err:
        if err == "access denied":
            _audit('blocked')
            return web.json_response({"error": err}, status=403)
        if err.startswith("file too large"):
            _audit('too_large')
            return web.json_response({"error": err}, status=413)
        if err == "invalid path":
            _audit('blocked')
            return web.json_response({"error": err}, status=400)
        _audit('not_found')
        return web.json_response({"error": err}, status=404)
    _audit('ok')
    return web.json_response({"name": name, "path": rel_path, "content": content})


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
    if content is None and name.startswith("aim/"):
        aim_name = name[4:]  # strip "aim/" prefix
        # Run the aim subprocess on the loop (async), parse off-loop: the parse
        # does per-skill ~/.aim globs and must not block the event loop.
        aim_stdout = await _aim_list_stdout()
        aim_skills = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), _parse_aim_skills, aim_stdout
        )
        for s in aim_skills:
            if s["name"] == aim_name or s["key"] == name:
                if s["path"]:
                    from kiro_crew.hooks import validate_file_path  # noqa: F811
                    resolved = validate_file_path(s["path"])
                    if resolved is None:
                        return web.json_response({"error": "access denied"}, status=403)
                    try:
                        content = Path(resolved).read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        pass
                break
    if content is None and (name.startswith("kiro-user/") or name.startswith("kiro-workspace/")):
        # Open-standard kiro-cli skills are read-only here — load via the
        # same path-resolution logic used by the tree/file endpoints so the
        # detail modal can fetch SKILL.md regardless of which root the
        # skill lives in.
        root = _resolve_skill_root(name, state)
        if root is not None:
            content_value, err = read_skill_file(root, "SKILL.md")
            if err is None:
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
