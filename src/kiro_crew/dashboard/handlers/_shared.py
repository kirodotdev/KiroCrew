"""Shared helpers used across handler submodules."""

from __future__ import annotations

import fnmatch
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew.agent_discovery import (
    SKILL_URI_PREFIX,
    expand_skill_uri,
    skill_resource_uris,
)
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.dashboard.state import VALID_MEMORY_MODES, DashboardState
from kiro_crew.security import is_sensitive_path
from kiro_crew.skills import skills_dir
from kiro_crew.slack.handler import (
    _hydrate_conv_flags,
    is_thread_incognito,
    is_thread_temporary,
)

if TYPE_CHECKING:
    from kiro_crew.platform.interfaces import CapabilityManager

logger = logging.getLogger(__name__)


# Shared body cap for the small JSON-object endpoints that must bound the
# request BEFORE decoding (the strict-internal notification routes). Kept
# module-level and in one place so the security-relevant cap cannot drift
# between the two call sites (issue #490). 64 KB is generous — payload fields
# have their own caps.
_MAX_BODY_BYTES = 64 * 1024


async def read_bounded_json(
    request: web.Request, max_bytes: int = _MAX_BODY_BYTES
) -> tuple[dict[str, Any] | None, web.Response | None]:
    """Read and parse a JSON *object* request body, capped at *max_bytes*.

    Returns ``(body, None)`` on success, or ``(None, error_response)`` when the
    caller should return early. The cap is enforced BEFORE decoding: a
    Content-Length precheck rejects an oversized declared body, and the stream
    is then read incrementally so a chunked body (which carries no
    Content-Length) cannot buffer past ``max_bytes + one chunk`` on the
    event-loop thread. Consolidates the previously-duplicated block in
    ``messaging.api_notification_agent_push`` and
    ``notifications_push.api_push_notification`` so the cap and the 413/400
    contract stay identical across both (issue #490).
    """
    if request.content_length and request.content_length > max_bytes:
        return None, web.json_response(
            {"error": "payload too large", "code": "payload_too_large"}, status=413
        )
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.content.iter_chunked(8192):
        received += len(chunk)
        if received > max_bytes:
            return None, web.json_response(
                {"error": "payload too large", "code": "payload_too_large"}, status=413
            )
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks))
    except Exception:
        return None, web.json_response(
            {"error": "invalid JSON body", "code": "invalid_json"}, status=400
        )
    if not isinstance(body, dict):
        return None, web.json_response(
            {"error": "body must be a JSON object", "code": "body_not_object"}, status=400
        )
    return body, None


def _capability_manager() -> "CapabilityManager":
    """The edition's external capability manager (CPP seam).

    Lives in the shared layer (not a leaf handler) so every consumer —
    ``agents.py`` handlers, ``mcp.py`` uninstall, and the skill/prompt listers
    here — imports it DOWNWARD with no circular dependency. Operations-based: the
    edition owns its CLI grammar, output parsing, and error translation. Fails
    closed to an unavailable ``DefaultCapabilityManager`` so ``/api/capability/*``
    degrade to 503 rather than crashing.

    The returned manager is ALREADY LIVENESS-bounded: the context wraps every
    ``CapabilityManager`` in ``BoundedCapabilityManager`` at composition time
    (``PlatformContext.__post_init__``), so the ``asyncio.wait_for`` mutation
    bound is inherited by every reader of ``current_context().capability_manager``
    — not just callers who route through this accessor. The fallback
    ``DefaultCapabilityManager`` is bound here too so a context-lookup failure
    degrades to a wrapped (still unavailable) manager, keeping the return type
    uniform.
    """
    from kiro_crew.platform.capability_bound import bind_capability_manager
    from kiro_crew.platform.context import current_context, safe_context_call
    from kiro_crew.platform.defaults import DefaultCapabilityManager

    return safe_context_call(
        lambda: current_context().capability_manager,
        fallback_factory=lambda: bind_capability_manager(DefaultCapabilityManager()),
        log_message="capability_manager lookup failed; treating as unavailable",
    )


def _get_memory(state: DashboardState):
    """Get MemoryStore from context_builder, or create standalone."""
    if state.context_builder:
        return state.context_builder.memory
    # Fallback: create standalone MemoryStore
    if not hasattr(state, "_standalone_memory"):
        from kiro_crew.memory import MemoryStore

        mem = MemoryStore()
        mem.init()
        state._standalone_memory = mem  # type: ignore[attr-defined]
    return state._standalone_memory  # type: ignore[attr-defined]


def _get_active_workspace(state: DashboardState) -> str:
    """Return the workspace of the most recently active chat slot, or 'default'."""
    slots = getattr(state, "_slots", {})
    if slots:
        # Pick the slot with the most messages (most active)
        best = max(slots.values(), key=lambda s: s.total_messages, default=None)
        if best and best.workspace and best.workspace != "default":
            return best.workspace
    return "default"


def _get_lessons(state: DashboardState, workspace: str | None = None):
    """Get LessonStore for a workspace. Falls back to global."""
    ws = workspace or _get_active_workspace(state)
    if ws != "default" and state.context_builder:
        return state.context_builder.get_lessons_for(ws)
    return state.lessons


def _get_skills(state: DashboardState):
    """Get SkillsLoader from context_builder, or create standalone."""
    if state.context_builder:
        return state.context_builder.skills
    if not hasattr(state, "_standalone_skills"):
        from kiro_crew.skills import SkillsLoader

        skills = SkillsLoader(install_builtins=False)
        state._standalone_skills = skills  # type: ignore[attr-defined]
    return state._standalone_skills  # type: ignore[attr-defined]


def _edition_skill_roots() -> list[Path]:
    """Return edition-contributed SKILL.md source roots (CPP seam).

    Reads ``McpToolingProvider.extra_skills()`` fail-closed through
    ``safe_context_call`` (public Default: ``[]``), so on a vanilla OSS install
    there are no roots to discover and the edition skill helpers below
    return "nothing found" rather than globbing a hardcoded home-dir tree.
    Deferred import (sel.py pattern) so this module never imports the platform
    package at module load.
    """
    from kiro_crew.platform.context import current_context, safe_context_call

    roots: list[Path] = safe_context_call(
        lambda: list(current_context().mcp_tooling.extra_skills()),
        fallback_factory=list,
        log_message="extra_skills lookup failed; using none",
    )
    return [Path(r) for r in roots]


def _resolve_aim_skill_path(name: str) -> Path | None:
    """Find SKILL.md for an edition-contributed skill by leaf name.

    Iterates the edition skill roots (``McpToolingProvider.extra_skills()``)
    instead of globbing an edition home-dir tree directly. Within each root a
    skill lives at
    either ``<root>/<pkg>/<name>/SKILL.md`` or ``<root>/<name>/SKILL.md``; the
    first match (roots in seam order) wins.
    """
    for root in _edition_skill_roots():
        for pattern in (f"*/{name}/SKILL.md", f"{name}/SKILL.md"):
            for p in root.glob(pattern):
                return p
    return None


def active_project_dir(state: DashboardState, session_key: str = "") -> Path | None:
    """Return the project directory that workspace-scoped resources resolve against.

    Workspace-scoped resources (``<project>/.kiro/skills``,
    ``<project>/.kiro/steering``) live under the directory the agent actually
    runs in, which the dashboard stores per chat slot as ``_ChatSlot.project``
    (set by ``PUT /api/chat/slots/{slot}/project`` and the ``set_project`` MCP
    tool).  ``project_dir`` is accepted as a fallback for slot-like objects that
    expose that name instead.

    Resolution is deterministic, in this order:

    1. the slot named by *session_key*, when it has a project;
    2. otherwise the single project shared by every slot that has one;
    3. otherwise ``None``.

    Step 3 matters for mutations: with two chats open on different projects
    there is no defensible "active" project for a settings page, and silently
    picking the first-inserted slot would create, overwrite or delete files in
    the wrong project.  Failing closed makes the caller surface the ambiguity
    instead.
    """
    slots = getattr(state, "_slots", {}) or {}

    def _project_of(slot: Any) -> Path | None:
        pd = getattr(slot, "project", None) or getattr(slot, "project_dir", None)
        if isinstance(pd, Path):
            return pd
        if isinstance(pd, str) and pd:
            return Path(pd)
        return None

    if session_key:
        slot_name = session_key.split(":", 1)[-1] if ":" in session_key else session_key
        slot = slots.get(slot_name)
        if slot is not None:
            scoped = _project_of(slot)
            if scoped is not None:
                return scoped
    distinct: dict[str, Path] = {}
    for slot in slots.values():
        proj = _project_of(slot)
        if proj is not None:
            distinct[str(proj)] = proj
    if len(distinct) == 1:
        return next(iter(distinct.values()))
    return None


# ── Kiro-cli native skills (~/.kiro/skills/, <project>/.kiro/skills/) ──


# Maximum SKILL.md content we'll read just to extract frontmatter description.
_KIRO_SKILL_FRONTMATTER_BYTES = 4096


def _kiro_skill_roots(project_dir: Path | None = None) -> list[tuple[str, Path]]:
    """Return ``(label, path)`` pairs for the open-standard skill locations.

    label is one of: ``kiro-user``, ``kiro-workspace``.  Used as the
    ``source`` field on listed skills so the UI can show provenance.
    """
    out: list[tuple[str, Path]] = []
    user_dir = Path.home() / ".kiro" / "skills"
    if user_dir.is_dir() and not is_sensitive_path(str(user_dir)):
        out.append(("kiro-user", user_dir))
    if project_dir:
        ws_dir = project_dir / ".kiro" / "skills"
        if ws_dir.is_dir() and not is_sensitive_path(str(ws_dir)):
            out.append(("kiro-workspace", ws_dir))
    return out


def _parse_skill_description(skill_md: Path) -> tuple[str, bool]:
    """Cheap frontmatter parse — return (description, always)."""
    # Gate on the resolved target before reading: a SKILL.md inside an
    # otherwise-trusted skills root may itself be a symlink to a sensitive
    # credential file (e.g. ~/.kiro/skills/evil/SKILL.md → ~/.aws/credentials).
    # Checking the root dir is not enough — individual files must be checked.
    try:
        resolved_md = skill_md.resolve(strict=True)
    except OSError:
        return "", False
    if is_sensitive_path(str(resolved_md)):
        return "", False
    try:
        with resolved_md.open("rb") as f:
            head = f.read(_KIRO_SKILL_FRONTMATTER_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return "", False
    if not head.startswith("---"):
        return "", False
    end = head.find("\n---", 3)
    if end < 0:
        return "", False
    desc = ""
    always = False
    for line in head[3:end].splitlines():
        line = line.strip()
        if line.startswith("description:"):
            desc = line.split(":", 1)[1].strip().strip('"').strip("'")
        elif line.startswith("always:"):
            val = line.split(":", 1)[1].strip().lower()
            always = val == "true"
    return desc, always


def list_kiro_skills(project_dir: Path | None = None) -> list[dict[str, Any]]:
    """List skills from kiro-cli's open-standard locations.

    Each entry has the same shape as a SkillsLoader entry plus a
    ``source`` of ``kiro-user`` or ``kiro-workspace``.  Read-only —
    edits are not routed back here (kiro-cli owns these directories).
    """
    out: list[dict[str, Any]] = []
    for source, root in _kiro_skill_roots(project_dir):
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            desc, always = _parse_skill_description(skill_md)
            out.append({
                "key": f"{source}/{entry.name}",
                "name": entry.name,
                "description": desc,
                "path": str(skill_md),
                "dir": str(entry),
                "always": always,
                "source": source,
            })
    return out


# ── loaded_by_agents resolution ──


def _agent_dirs() -> list[Path]:
    """Return existing agent JSON directories (global + workspace)."""
    out: list[Path] = []
    user = kiro_agents_dir()
    if user.is_dir():
        out.append(user)
    return out


def _expand_resource_uri(uri: str, agent_path: Path) -> str | None:
    """Strip ``skill://`` and resolve ``~`` / workspace-relative paths.

    Thin alias for :func:`kiro_crew.agent_discovery.expand_skill_uri` — the
    single implementation, shared with the session-context skill filter so the
    dashboard's ``loaded_by_agents`` annotation and the runtime injection agree
    on what a given URI matches.

    Returns a glob pattern usable with fnmatch, or None if not a skill URI.
    """
    return expand_skill_uri(uri, agent_path)


def _agent_loads_skill(agent_json: dict[str, Any], agent_path: Path, skill_md: Path) -> bool:
    """Return True if *agent_json*'s ``resources`` would load *skill_md*.

    One-off helper (single skill vs single agent). For annotating *many*
    skills against *many* agents, prefer :func:`_expand_agent_globs` +
    :func:`_agents_loading_skill` so each agent's globs are expanded once
    instead of once per skill.
    """
    resources = agent_json.get("resources") or []
    if not isinstance(resources, list):
        return False
    target = str(skill_md)
    for res in resources:
        if not isinstance(res, str):
            continue
        glob = _expand_resource_uri(res, agent_path)
        if glob and fnmatch.fnmatch(target, glob):
            return True
    return False


def _expand_agent_globs(
    parsed_agents: list[tuple[str, dict[str, Any], Path]],
) -> list[tuple[str, list[str]]]:
    """Pre-expand every agent's ``skill://`` resources into fnmatch globs ONCE.

    Returns ``(agent_name, [glob, ...])`` pairs. The glob for a resource
    depends only on ``(uri, agent_path)`` — NOT on the skill being matched —
    so expanding here (O(agents × resources)) and reusing the result across
    all skills avoids re-running :func:`_expand_resource_uri` once per
    (skill, agent, resource), which on a large catalog is the dominant cost.
    Agents with no skill:// resources are dropped (they can match nothing).
    """
    expanded: list[tuple[str, list[str]]] = []
    for name, data, agent_path in parsed_agents:
        resources = data.get("resources") or []
        if not isinstance(resources, list):
            continue
        globs = [
            g
            for res in resources
            if isinstance(res, str)
            for g in (_expand_resource_uri(res, agent_path),)
            if g
        ]
        if globs:
            expanded.append((name, globs))
    return expanded


def _agents_loading_skill(
    skill_md: Path, expanded_agents: list[tuple[str, list[str]]]
) -> list[str]:
    """Return names of agents whose pre-expanded globs match *skill_md*."""
    target = str(skill_md)
    return [
        name
        for name, globs in expanded_agents
        if any(fnmatch.fnmatch(target, g) for g in globs)
    ]


def _load_parsed_agents() -> list[tuple[str, dict[str, Any], Path]]:
    """Read every agent JSON ONCE, returning ``(name, data, agent_path)``.

    Hoisted out of the per-skill loop so ``api_skills`` parses each agent
    file exactly once per request instead of once per skill — turning an
    O(skills × agents) read/parse blowup into O(agents). Best-effort: macOS
    AppleDouble sidecars ("._foo.json"), unreadable/invalid agents, and
    sensitive-path symlinks are skipped (a symlink under ~/.kiro/agents/
    could otherwise point at a credential file renamed ``*.json``).
    """
    parsed: list[tuple[str, dict[str, Any], Path]] = []
    for agents_dir in _agent_dirs():
        try:
            agent_files = sorted(agents_dir.glob("*.json"))
        except OSError:
            # An unreadable agents dir (e.g. PermissionError) must degrade to
            # "no agents" rather than propagate and 500 the whole response.
            continue
        for agent_path in agent_files:
            if agent_path.name.startswith("._"):
                continue
            try:
                resolved = agent_path.resolve(strict=True)
            except OSError:
                continue
            if is_sensitive_path(str(resolved)):
                continue
            try:
                data = json.loads(resolved.read_text(encoding="utf-8"))
            # ValueError covers both json.JSONDecodeError and
            # UnicodeDecodeError (a non-UTF-8 file must not 500 the API).
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            name = data.get("name") or agent_path.stem
            parsed.append((str(name), data, agent_path))
    return parsed


def _resolve_loaded_by_agents(
    skill_md: Path,
    parsed_agents: list[tuple[str, dict[str, Any], Path]] | None = None,
) -> list[str]:
    """Return list of agent names whose ``resources`` glob matches *skill_md*.

    Pass *parsed_agents* (from :func:`_load_parsed_agents`) to reuse a single
    agent parse across many skills; omit it for a one-off lookup (parses
    agents inline). Empty list means no agent loads this skill via
    ``skill://`` URIs (it may still be loaded via KiroCrew text-injection or
    an external MCP server).
    """
    agents = parsed_agents if parsed_agents is not None else _load_parsed_agents()
    out: list[str] = []
    for name, data, agent_path in agents:
        if _agent_loads_skill(data, agent_path, skill_md):
            out.append(name)
    return out


def annotate_skills_with_agents(skills: list[dict[str, Any]]) -> None:
    """Annotate each skill dict in-place with ``loaded_by_agents``.

    Parses the agent JSONs ONCE and pre-expands each agent's ``skill://``
    globs ONCE, then matches every skill against that in-memory set —
    O(agents × resources) expansion + O(skills × globs) matching, instead of
    re-expanding every agent glob per skill. Synchronous and filesystem-heavy
    (the parse walks ~/.kiro/agents) — callers on the asyncio event loop MUST
    run this off the loop. Per-skill failures isolate to an empty list (the
    documented default) rather than blanking the whole response.
    """
    expanded = _expand_agent_globs(_load_parsed_agents())
    for s in skills:
        path = s.get("path") or ""
        if not path:
            s["loaded_by_agents"] = []
            continue
        try:
            s["loaded_by_agents"] = _agents_loading_skill(Path(path), expanded)
        except Exception:
            s["loaded_by_agents"] = []


def collect_skills_blocking(
    skills_loader: Any,
    package_skills: list[dict[str, Any]],
    project_dir: Path | None,
) -> list[dict[str, Any]]:
    """Gather + annotate the full skill catalog. Runs ALL blocking FS work.

    This is the synchronous core behind ``GET /api/skills``. It performs
    every filesystem-heavy step in one call so the caller can offload the
    whole thing to a thread via ``run_in_executor``. ``list_skills()`` (os.walk +
    per-file frontmatter reads) and ``list_kiro_skills()`` (per-skill resolve +
    read) are filesystem-heavy enough to stall the event loop past the
    loop-stall watchdog on large catalogs, so they run in the thread too rather
    than inline.

    Steps, in the same order the handler used inline:

    1. ``skills_loader.list_skills()`` — kirocrew skills (default source).
    2. ``package_skills`` — edition/package skills already fetched (structured
       rows) from ``CapabilityManager.list_skills()``; the manager owns their
       parsing, so nothing is parsed here.
    3. ``list_kiro_skills(project_dir)`` — open-standard kiro-cli skills.
    4. ``annotate_skills_with_agents(...)`` — ``loaded_by_agents`` per skill.

    The capability-manager fetch is intentionally NOT done here (it is async);
    the caller awaits it and hands us the structured rows.
    """
    result: list[dict[str, Any]] = skills_loader.list_skills()
    for s in result:
        s.setdefault("source", "kirocrew")
    _warn_skills_outside_roots(package_skills)
    result.extend(package_skills)
    result.extend(list_kiro_skills(project_dir))
    annotate_skills_with_agents(result)
    return result


def _warn_skills_outside_roots(package_skills: list[dict[str, Any]]) -> None:
    """Log loudly for any ``CapabilityManager.list_skills()`` row whose path
    falls outside every ``McpToolingProvider.extra_skills()`` root.

    Enforces (at runtime, not just in the interface docstring) the containment
    invariant the two Protocols share: the skill browser
    (``/api/skills/package/<name>/tree`` + detail) resolves a skill's on-disk
    path by searching those roots, so a listed row outside them lists in
    ``/api/skills`` but 404s on tree/detail. An edition that satisfies both
    seams independently can violate this; a loud warning turns an otherwise
    silent, hard-to-diagnose 404 into an actionable log line. No-op in OSS
    (``list_skills()`` returns ``[]``, so ``package_skills`` is empty).
    """
    if not package_skills:
        return
    roots = _edition_skill_roots()
    if not roots:
        return
    resolved_roots = []
    for r in roots:
        try:
            resolved_roots.append(r.resolve())
        except OSError:
            continue
    for row in package_skills:
        raw = row.get("dir") or row.get("path")
        if not raw:
            continue
        try:
            p = Path(raw).resolve()
        except OSError:
            continue
        if not any(p == root or root in p.parents for root in resolved_roots):
            logger.warning(
                "skill %r (path %s) is outside every extra_skills() root %s — it "
                "will list in /api/skills but 404 on tree/detail (CapabilityManager."
                "list_skills / McpToolingProvider.extra_skills containment invariant)",
                row.get("name") or row.get("key"),
                raw,
                [str(r) for r in resolved_roots],
            )


# ── Skill directory browser (tree + file content) ──


# Hard caps to keep the API responsive and bounded.
SKILL_TREE_MAX_ENTRIES = 500
SKILL_FILE_MAX_BYTES = 1_048_576  # 1 MiB


def _resolve_skill_root(name: str, state: DashboardState) -> Path | None:
    """Return the absolute skill directory for *name*, or None.

    Accepts the same nested-name scheme used by the existing skill API:
    - ``foo`` → ``~/.kiro/crew/skills/foo``
    - ``utils/tiny-url`` → ``~/.kiro/crew/skills/utils/tiny-url``
    - ``package/<skill>`` → resolved via _resolve_aim_skill_path() lookup
    - ``kiro-user/<skill>`` → ``~/.kiro/skills/<skill>``
    - ``kiro-workspace/<skill>`` → ``<project>/.kiro/skills/<skill>``

    The returned path is always under one of the allowed roots — paths
    that try to escape via ``..`` or symlinks are rejected.
    """
    if not name or ".." in name or name.startswith("/"):
        return None
    if name.startswith("kiro-user/"):
        rel = name[len("kiro-user/") :]
        root = Path.home() / ".kiro" / "skills"
    elif name.startswith("kiro-workspace/"):
        rel = name[len("kiro-workspace/") :]
        # We cannot reliably resolve project dir here — gate this to
        # paths under any active slot's project directory.
        proj = active_project_dir(state)
        if proj is None:
            return None
        root = proj / ".kiro" / "skills"
    elif name.startswith("package/"):
        # Locate via existing helper (sync version).
        aim_name = name[len("package/") :]
        path = _resolve_aim_skill_path(aim_name)
        if not path:
            return None
        candidate = path.parent
        if is_sensitive_path(str(candidate)):
            return None
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        # Re-check the *resolved* target — a symlink within the AIM path could
        # point at a sensitive location that the unresolved check missed
        # (consistent with the kirocrew/kiro branches below).
        if is_sensitive_path(str(resolved)):
            return None
        return resolved
    else:
        # ``kirocrew`` skills live under the active config home, which honors
        # KIROCREW_HOME (e.g. isolated dev gateways).  Hardcoding
        # ``~/.kirocrew`` here would 404 every skill in a KIROCREW_HOME-isolated
        # deployment even though SkillsLoader (the GET /api/skills source)
        # resolves them correctly.
        rel = name
        # Reject empty, traversal, absolute, and home-expansion inputs before
        # any filesystem probing. pathlib collapses ``Path(root) / "/etc"`` to
        # ``/etc`` (absolute RHS overrides the base), so an un-rejected absolute
        # or ``~`` prefix would let _probe() run is_dir() on arbitrary paths
        # before the containment check.
        if not rel or ".." in rel or rel.startswith("/") or rel.startswith("~"):
            return None
        # Root precedence must match SkillsLoader.load_skill(): kirocrew ->
        # user extra_paths -> edition skill roots (lowest). Otherwise the tree
        # endpoint could display a different directory than load_skill() reads.
        roots = [skills_dir()]
        try:
            roots.extend(Path(p).expanduser() for p in KiroCrewConfig.load().skills.extra_paths)
        except Exception:
            logger.debug("failed to load extra skill paths from config", exc_info=True)
        roots.extend(_edition_skill_roots())

        def _probe(r: Path) -> bool:
            try:
                return (r / rel).is_dir()
            except OSError:
                return False

        root = next((r for r in roots if _probe(r)), skills_dir())
    candidate = root / rel
    if not candidate.is_dir():
        return None
    if is_sensitive_path(str(candidate)):
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    # Containment + symlink policy.  Skills can be nested under category
    # directories (``utils/multi-badger`` → ``<root>/utils/multi-badger``),
    # and a skill directory itself may be a symlink (AIM ``--local`` installs
    # symlink ``~/.kiro/skills/<name>`` to ``~/.agents/skills/<name>``).  We
    # therefore require the candidate's *parent* directory to resolve to a
    # location at or under the trusted root — which permits the leaf to be a
    # symlink while still rejecting a symlinked *intermediate* directory that
    # would let ``a/b`` escape the tree.  The resolved target is then checked
    # against the sensitive-path list as a final guard.
    try:
        parent_resolved = candidate.parent.resolve(strict=True)
        root_resolved = root.resolve(strict=True)
    except OSError:
        return None
    if parent_resolved != root_resolved and root_resolved not in parent_resolved.parents:
        return None
    if is_sensitive_path(str(resolved)):
        return None
    return resolved


# ── Agent-template skill mapping (skill:// resources <-> catalog keys) ──


# Upper bound on how many skills one agent template may map. Each mapped skill
# is a full SKILL.md that kiro-cli loads into the agent's context, so an
# unbounded list is a context-exhaustion footgun, not a feature.
MAX_AGENT_SKILLS = 100

# fnmatch metacharacters. A URI containing any of these matches a SET of skills
# ("every skill in this root"), which has no single catalog key — such entries
# are surfaced read-only and preserved verbatim across edits.
_GLOB_CHARS = ("*", "?", "[")


def _skill_key_roots(state: DashboardState) -> list[tuple[str, Path]]:
    """``(key_prefix, root)`` pairs for every location skills are keyed from.

    Mirrors :func:`_resolve_skill_root`'s roots, in the same precedence order,
    so an enumerated key names the same directory that function would resolve.
    Roots that cannot exist in this deployment (no active project dir, no
    edition roots) are omitted.
    """
    out: list[tuple[str, Path]] = [("kiro-user/", Path.home() / ".kiro" / "skills")]
    proj = active_project_dir(state)
    if proj is not None:
        out.append(("kiro-workspace/", proj / ".kiro" / "skills"))
    out.append(("", skills_dir()))
    try:
        # ``resolve()``, not just ``expanduser()``: a RELATIVE extra_paths entry
        # would otherwise key the catalog by a relative root, and the persisted
        # ``skill://`` URI would then resolve against whatever cwd the next
        # kiro-cli session starts in — silently loading a different skill, or
        # none. A skill root must be a stable absolute location.
        out.extend(
            ("", Path(p).expanduser().resolve())
            for p in KiroCrewConfig.load().skills.extra_paths
        )
    except Exception:
        logger.debug("failed to load extra skill paths from config", exc_info=True)
    out.extend(("package/", r) for r in _edition_skill_roots())
    return out


# Skills may sit under category directories (``utils/tiny-url``). Bound the
# enumeration walk so a deep or pathological tree cannot turn one PATCH into an
# unbounded filesystem crawl. Three levels covers every layout in use.
_SKILL_NEST_DEPTH = 3


def _collect_skills_under(
    directory: Path,
    root: Path,
    root_resolved: Path,
    prefix: str,
    out: dict[str, Path],
    depth: int,
) -> None:
    """Add every ``<dir>/SKILL.md`` at or under *directory* to *out*.

    Containment mirrors :func:`_resolve_skill_root`: a candidate's *parent* must
    resolve at or under the trusted root, which permits the skill directory
    itself to be a symlink (AIM ``--local`` installs symlink
    ``~/.kiro/skills/<name>`` to elsewhere) while still rejecting a symlinked
    *intermediate* directory that would let ``a/b`` escape the tree. Sensitive
    paths are rejected before and after symlink resolution.
    """
    if depth <= 0:
        return
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        if is_sensitive_path(str(entry)):
            continue
        try:
            parent_resolved = entry.parent.resolve(strict=True)
        except OSError:
            continue
        if parent_resolved != root_resolved and root_resolved not in parent_resolved.parents:
            continue
        skill_md = entry / "SKILL.md"
        if skill_md.is_file():
            try:
                target = skill_md.resolve(strict=True)
            except OSError:
                continue
            if is_sensitive_path(str(target)):
                continue
            # First root wins, matching _skill_key_roots precedence.
            out.setdefault(prefix + entry.relative_to(root).as_posix(), skill_md)
        else:
            _collect_skills_under(entry, root, root_resolved, prefix, out, depth - 1)


def enumerate_skill_catalog(state: DashboardState) -> dict[str, Path]:
    """Map every discoverable catalog key to its ``SKILL.md`` path.

    Built by **enumerating** the skill roots, never by joining a caller-supplied
    string onto one. That is the security property this function exists for: the
    only paths the agent-template editor can ever hand to the filesystem or
    write into an agent spec are paths this walk discovered, so a hostile or
    traversing key (``../../.ssh``, an absolute path, a ``~`` prefix) can do
    nothing but miss a dict lookup. Allowlist by enumeration rather than
    validate-then-join — it also removes the tainted-path dataflow that
    validate-then-join leaves for static analysis to flag.

    It is additionally the single source of truth for BOTH directions of the
    key <-> URI mapping, so they cannot disagree: a mapping written against a
    symlinked skill directory inverts back to the same key it was written from.
    """
    catalog: dict[str, Path] = {}
    for prefix, root in _skill_key_roots(state):
        if is_sensitive_path(str(root)) or not root.is_dir():
            continue
        try:
            root_resolved = root.resolve(strict=True)
        except OSError:
            continue
        _collect_skills_under(root, root, root_resolved, prefix, catalog, _SKILL_NEST_DEPTH)
    return catalog


def _skill_uri_for_path(skill_md: Path) -> str:
    """Render a discovered ``SKILL.md`` path as a ``skill://`` resource URI.

    Paths under ``$HOME`` are emitted in ``~/`` form: kiro-cli expands it, and
    it keeps the written agent spec portable across machines and home dirs.
    """
    try:
        rel_home = skill_md.relative_to(Path.home())
    except ValueError:
        return f"{SKILL_URI_PREFIX}{skill_md.as_posix()}"
    return f"{SKILL_URI_PREFIX}~/{rel_home.as_posix()}"


def skill_key_for_uri(
    uri: str,
    agent_path: Path,
    state: DashboardState,
    catalog: dict[str, Path] | None = None,
) -> str | None:
    """Invert a ``skill://`` resource URI back to a catalog key, or ``None``.

    ``None`` means "not editable through the catalog" — a wildcard pattern, or a
    path that no enumerated skill accounts for (a hand-authored URI, or a skill
    that has since been deleted). Callers preserve those verbatim instead of
    rewriting or dropping them.

    Pass *catalog* (from :func:`enumerate_skill_catalog`) to reuse one walk
    across many URIs.
    """
    if any(c in uri for c in _GLOB_CHARS):
        return None
    expanded = expand_skill_uri(uri, agent_path)
    if not expanded:
        return None
    entries = catalog if catalog is not None else enumerate_skill_catalog(state)
    wanted = Path(expanded)
    for key, path in entries.items():
        if path == wanted:
            return key
    # Fall back to comparing resolved targets so a URI written against a
    # symlinked skill directory (or against its target) still inverts.
    try:
        target = wanted.resolve(strict=True)
    except OSError:
        return None
    for key, path in entries.items():
        try:
            if path.resolve(strict=True) == target:
                return key
        except OSError:
            continue
    return None


def skill_uri_for_key(
    key: str, state: DashboardState, catalog: dict[str, Path] | None = None
) -> str | None:
    """Resolve a catalog key to the ``skill://`` URI for its ``SKILL.md``.

    A miss returns ``None`` — the key names no discoverable skill. Because the
    lookup goes through :func:`enumerate_skill_catalog` rather than joining
    *key* onto a root, an arbitrary caller-supplied key can never widen an
    agent's resources beyond the enumerated skill trees.

    Pass *catalog* to reuse one walk across many keys.
    """
    entries = catalog if catalog is not None else enumerate_skill_catalog(state)
    skill_md = entries.get(key)
    if skill_md is None:
        return None
    return _skill_uri_for_path(skill_md)


def agent_skill_views(
    data: dict[str, Any], agent_path: Path, state: DashboardState
) -> tuple[list[str], list[str]]:
    """``(catalog_keys, unmanaged_uris)`` for *data*, from ONE catalog walk.

    The two views partition the agent's ``skill://`` resources: keys the editor
    owns and can rewrite, and URIs it cannot express (wildcards, or paths no
    enumerated skill accounts for) which are shown read-only and preserved on
    every write. Both are order-preserving; keys are de-duplicated.

    Filesystem-heavy (it enumerates the skill roots) — callers on the asyncio
    event loop MUST run this off the loop.
    """
    catalog = enumerate_skill_catalog(state)
    keys: list[str] = []
    unmanaged: list[str] = []
    seen: set[str] = set()
    for uri in skill_resource_uris(data):
        key = skill_key_for_uri(uri, agent_path, state, catalog)
        if key is None:
            unmanaged.append(uri)
        elif key not in seen:
            seen.add(key)
            keys.append(key)
    return keys, unmanaged


def agent_skill_keys(
    data: dict[str, Any], agent_path: Path, state: DashboardState
) -> list[str]:
    """Catalog keys for the skills *data* maps, de-duplicated, order-preserving.

    Only catalog-resolvable entries are returned — this is the set the Agent
    Templates editor owns and can rewrite. Wildcard / hand-authored URIs are
    excluded here and reported separately by :func:`agent_unmanaged_skill_uris`.
    """
    return agent_skill_views(data, agent_path, state)[0]


def agent_unmanaged_skill_uris(
    data: dict[str, Any], agent_path: Path, state: DashboardState
) -> list[str]:
    """``skill://`` URIs that the catalog editor cannot express, in order.

    Wildcards, and paths no enumerated skill accounts for. Surfaced read-only in
    the UI and preserved on every write so editing an agent through the dashboard
    never silently drops a hand-authored mapping.
    """
    return agent_skill_views(data, agent_path, state)[1]


def apply_skill_mapping(
    data: dict[str, Any],
    agent_path: Path,
    state: DashboardState,
    keys: list[str],
) -> tuple[list[str], list[str]]:
    """Rewrite *data*'s ``skill://`` resources to *keys*, in place.

    Returns ``(applied_keys, unknown_keys)``. Nothing is written when
    *unknown_keys* is non-empty — the caller rejects the whole request so a
    typo'd key can never partially apply.

    Invariants:

    * Non-``skill://`` resources (``file://`` steering globs) keep their
      original relative order and are never touched.
    * Unmanaged ``skill://`` URIs (wildcards, hand-authored paths) are preserved.
    * The managed set is fully replaced, so removing a key removes the mapping.
    """
    applied: list[str] = []
    unknown: list[str] = []
    uris: list[str] = []
    seen: set[str] = set()
    # One enumeration for the whole write: every key resolved and every existing
    # URI inverted against the SAME snapshot, so a concurrent skill add/remove
    # cannot make the two halves disagree mid-request.
    catalog = enumerate_skill_catalog(state)
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        uri = skill_uri_for_key(key, state, catalog)
        if uri is None:
            unknown.append(key)
            continue
        applied.append(key)
        uris.append(uri)
    if unknown:
        return applied, unknown

    resources = data.get("resources") or []
    if not isinstance(resources, list):
        resources = []
    kept = [
        r
        for r in resources
        if not (isinstance(r, str) and r.startswith(SKILL_URI_PREFIX))
        or skill_key_for_uri(r, agent_path, state, catalog) is None
    ]
    merged = kept + [u for u in uris if u not in kept]
    if merged:
        data["resources"] = merged
    else:
        # An empty list is meaningful to kiro-cli (it suppresses the shipped
        # steering defaults that _refresh_dynamic_fields only re-seeds when the
        # key is absent/empty), and an agent with nothing mapped should fall
        # back to those defaults — so drop the key instead of writing [].
        data.pop("resources", None)
    return applied, unknown


def list_skill_tree(skill_root: Path) -> list[dict[str, Any]]:
    """Return a flat list of files under *skill_root*, capped at SKILL_TREE_MAX_ENTRIES.

    Each entry: ``{path: relative-from-root, type: "file"|"dir", size: int}``.
    Sensitive paths are filtered out.  Symlinks are resolved; entries whose
    real path escapes *skill_root* are omitted.
    """
    out: list[dict[str, Any]] = []
    skipped = 0
    for dirpath, dirnames, filenames in os.walk(skill_root, followlinks=False):
        # Stable order — reproducible across runs / tests.
        dirnames.sort()
        filenames.sort()
        for d in list(dirnames):
            full = Path(dirpath) / d
            if is_sensitive_path(str(full)):
                dirnames.remove(d)
                continue
            rel = full.relative_to(skill_root).as_posix()
            out.append({"path": rel, "type": "dir", "size": 0})
            if len(out) >= SKILL_TREE_MAX_ENTRIES:
                return out
        for f in filenames:
            full = Path(dirpath) / f
            if is_sensitive_path(str(full)):
                skipped += 1
                continue
            try:
                if full.is_symlink():
                    real = full.resolve(strict=True)
                    real.relative_to(skill_root.resolve(strict=True))
                    if is_sensitive_path(str(real)):
                        skipped += 1
                        continue
                stat = full.stat()
            except (OSError, ValueError):
                skipped += 1
                continue
            rel = full.relative_to(skill_root).as_posix()
            out.append({"path": rel, "type": "file", "size": int(stat.st_size)})
            if len(out) >= SKILL_TREE_MAX_ENTRIES:
                return out
    return out


def read_skill_file(skill_root: Path, rel_path: str) -> tuple[str, str | None]:
    """Read ``skill_root/rel_path`` with safety + size guards.

    Returns ``(content, error)``.  ``error`` is non-empty when access is
    denied, the file is too big, or it doesn't exist.
    """
    if not rel_path or ".." in rel_path.split("/") or rel_path.startswith("/"):
        return "", "invalid path"
    target = skill_root / rel_path
    try:
        resolved = target.resolve(strict=True)
        skill_resolved = skill_root.resolve(strict=True)
        resolved.relative_to(skill_resolved)
    except (OSError, ValueError):
        return "", "not found"
    if is_sensitive_path(str(resolved)):
        return "", "access denied"
    if not resolved.is_file():
        return "", "not a file"
    try:
        size = resolved.stat().st_size
    except OSError:
        return "", "stat failed"
    if size > SKILL_FILE_MAX_BYTES:
        return "", f"file too large ({size} bytes; cap {SKILL_FILE_MAX_BYTES})"
    try:
        return resolved.read_text(encoding="utf-8", errors="replace"), None
    except OSError:
        return "", "read failed"


def _read_session_key(request: "Any") -> str:
    """Read and normalize the ``X-Session-Key`` header for authz comparisons.

    Strips surrounding whitespace so the authorization gate matches the
    canonical stored key form and the routing endpoints (which already
    ``.strip()``). A trailing space / stray whitespace must not let a
    restricted or read-blocked session slip past the restricted-key set or the
    slot lookup (CWE-178/180 — inconsistent normalization in an auth context).
    """
    return request.headers.get("X-Session-Key", "").strip()


def _is_restricted_session(state: DashboardState, request: "Any") -> bool:
    """Check if request comes from an ephemeral (incognito) or temporary (guest) session.

    Reads X-Session-Key header (set by browser and MCP subprocesses).
    Returns True if the session should be blocked from memory operations.
    """
    sk = _read_session_key(request)
    if not sk:
        return False
    if sk == "dashboard:ui":
        return False
    if sk in state._restricted_keys:
        return True
    slot_name = sk.split(":", 1)[-1] if ":" in sk else sk
    slot = state._slots.get(slot_name)
    if slot and slot.is_restricted:
        return True
    if sk.startswith("slack:"):
        # Restore the DURABLE flags before consulting the in-memory maps.
        # ``_thread_incognito``/``_thread_temporary`` are process-local and are
        # only populated by ``_hydrate_conv_flags`` on an INBOUND Slack message,
        # so a turn that no inbound message drove — a cron with
        # session="origin", a webhook-resumed session, a monitor/autonudge
        # re-injection, a subagent — reaches this gate with empty maps after a
        # gateway restart even though the user's !incognito is on disk. Calling
        # the canonical restore (rather than reading the SessionMap directly)
        # keeps one source of truth and self-heals the process-local view.
        # Idempotent and allocation-free for unflagged keys.
        _hydrate_conv_flags(state.sessions, sk)
        if is_thread_temporary(sk) or is_thread_incognito(sk):
            return True
    # NOTE: deliberately no disk fallback for an absent slot. This predicate is
    # a SYNC helper with ~49 call sites reachable from async handlers, so reading
    # the persisted mode here would put blocking file I/O on the event loop
    # (AUTOSDE ``no-blocking-call-on-event-loop``). The archived-session recovery
    # is done off-loop instead, by the one caller that needs it —
    # ``api_lessons_create`` — via ``_probe_persisted_session``.
    return False


def _blocks_reads_session(state: DashboardState, request: "Any") -> bool:
    """Check if request comes from a temporary session that blocks memory reads."""
    sk = _read_session_key(request)
    if not sk or sk == "dashboard:ui":
        return False
    slot_name = sk.split(":", 1)[-1] if ":" in sk else sk
    slot = state._slots.get(slot_name)
    if slot and slot.blocks_reads:
        return True
    if sk.startswith("slack:"):
        # Same durable-flag restore as _is_restricted_session: a temporary
        # thread whose flags this process never hydrated must not serve reads.
        _hydrate_conv_flags(state.sessions, sk)
        if is_thread_temporary(sk):
            return True
    # NOTE: deliberately no disk fallback for an absent slot. This predicate is
    # a SYNC helper with ~49 call sites reachable from async handlers, so reading
    # the persisted mode here would put blocking file I/O on the event loop
    # (AUTOSDE ``no-blocking-call-on-event-loop``). The archived-session recovery
    # is done off-loop instead, by the one caller that needs it —
    # ``api_lessons_create`` — via ``_probe_persisted_session``.
    return False


# Byte ceiling for the session-metadata head read. The metadata line is a small
# JSON object (a few hundred bytes); 64 KiB is generous headroom while keeping an
# enormous or adversarial first line from being pulled into memory.
_METADATA_HEAD_MAX_BYTES = 64 * 1024


def _persisted_session_paths(slot_name: str) -> list["Path"]:
    """Every existing session transcript that *slot_name* could name.

    Returns more than one entry only when the key is genuinely ambiguous — see
    :func:`_probe_persisted_session`, which treats that as unknown rather than
    picking a winner.
    """
    if (
        not slot_name
        or "/" in slot_name
        or "\\" in slot_name
        or "\x00" in slot_name
        or ":" in slot_name
        or slot_name.startswith(".")
    ):
        # Defence-in-depth against path traversal; ``KIROCREW_SESSION_KEY``
        # normally has no path separators, but ``X-Session-Key`` is
        # attacker-controlled in principle even behind the secret
        # middleware. Reject forward slash (Linux/macOS) and backslash
        # (Windows) path separators, null bytes that can truncate C-level
        # path parsing, and leading dots that could target hidden
        # per-directory files outside the intended session namespace.
        #
        # The colon is rejected for Windows, where it is not an ordinary
        # character: ``WindowsPath("…/sessions") / "D:foo.jsonl"`` evaluates to
        # ``D:foo.jsonl`` — a DRIVE-RELATIVE path that silently escapes the
        # sessions directory entirely (verified; POSIX joins it literally and is
        # unaffected). It also spells an NTFS alternate data stream
        # (``file:stream``). A dashboard slot key never contains a colon: the
        # transport prefix is stripped by the caller before this point, and
        # ``_normalize_slot_key`` folds the key to ``[\\w\\-.]`` anyway.
        return []
    sess_dir = config_dir() / "sessions"
    if not sess_dir.exists():
        return []
    # Match the resolution order used by slack/interactions.py when
    # linking Slack threads to existing sessions: bare stem first, then
    # the ``dashboard_`` prefix fallback for dashboard slots. Cron sessions
    # persist under different names: ``history._safe_key`` folds ``:`` to
    # ``_``, so ``cron:{id}`` writes ``cron_{id}.jsonl`` and its linked
    # dashboard slot ``dashboard:cron-{id}`` writes ``dashboard_cron-{id}.jsonl``.
    # Probe those too so an idle-evicted cron session is recognised rather
    # than misclassified as forged.
    candidates = [sess_dir / f"{slot_name}.jsonl"]
    if not slot_name.startswith("dashboard_"):
        candidates.append(sess_dir / f"dashboard_{slot_name}.jsonl")
    candidates.append(sess_dir / f"cron_{slot_name}.jsonl")
    candidates.append(sess_dir / f"dashboard_cron-{slot_name}.jsonl")
    return [p for p in candidates if p.exists()]


def _persisted_session_path(slot_name: str) -> "Path | None":
    """First existing transcript for *slot_name*, or None.

    Existence only. When more than one candidate exists the answer is still
    "yes, a session exists" — which is all the establish-vs-forged check needs.
    Anything making an AUTHORIZATION decision must use
    :func:`_probe_persisted_session`, which refuses to guess between them.
    """
    matches = _persisted_session_paths(slot_name)
    return matches[0] if matches else None


def _session_has_persisted_history(slot_name: str) -> bool:
    """Return True iff the slot has a JSONL file in the data home's sessions/.

    A positive signal that the session was previously **established** — i.e.
    that the key belongs to a real session rather than being forged or stale.
    It says nothing about the session's ``memory_mode``: every mode writes its
    transcript to disk (``_save_slot_to_history`` has no ``memory_mode`` gate,
    by design, so incognito/temporary tabs still survive a reload). Callers
    gating *memory writes* must therefore consult
    :func:`_persisted_session_memory_mode` as well — file existence alone is
    not evidence that writes are permitted.

    Used by ``api_lessons_create`` (in ``handlers/cron.py``) to distinguish
    between:

    * A legitimate MCP subprocess whose in-memory slot was evicted by the
      idle-sweep loop (``session.py``'s 30-minute timeout) or archived by a
      tab close. The subprocess still holds the original
      ``KIROCREW_SESSION_KEY`` env var, so it keeps sending the same
      ``X-Session-Key``, but ``state._slots`` has moved on. Without this
      check such calls return HTTP 400 ``unknown session`` even though the
      user is actively typing in the thread.

    * A forged or stale key from a context that never had a real session
      backing it — which should continue to be rejected.

    Only checks existence, not contents. Authentication of the caller is
    still enforced by the ``X-Internal-Secret`` middleware upstream; this
    check only governs the *established vs forged* distinction.
    """
    return _persisted_session_path(slot_name) is not None


def _persisted_session_memory_mode(slot_name: str) -> str | None:
    """Return the ``memory_mode`` recorded in *slot_name*'s session metadata.

    Three distinct outcomes, and the distinction IS the security property:

    * ``"persistent"`` / ``"incognito"`` / ``"temporary"`` — read from the
      metadata line. A metadata line that parses but carries no ``memory_mode``
      is reported as ``"persistent"``: the field postdates the feature, so a
      valid header without it is genuinely a legacy persistent session.
    * ``None`` — **unknown**. No file, or no parseable metadata object as the
      first line. Callers gating memory writes MUST deny on ``None`` rather
      than treat it as persistent. Denying is safe: ``ConversationLog.append``
      writes the metadata line when it creates the file, before any message is
      appended, so a session file whose first line is not metadata was not
      produced by a normal session and is no evidence that writes are allowed.

    This is the recovery path for a session whose in-memory state is gone but
    whose transcript is still on disk. Both in-memory signals a restricted
    session normally carries are dropped when a tab is archived
    (``api_chat_slot_close`` removes the slot from ``state._slots`` *and*
    discards its key from ``state._restricted_keys``), while the transcript —
    including its ``memory_mode`` marker — persists. Without reading that
    marker back, an archived incognito session whose MCP subprocess is still
    alive presents as an ordinary established session and its memory writes
    are allowed.

    Only the FIRST line is consulted, and only ``_METADATA_HEAD_MAX_BYTES`` of
    it: a later ``_type: metadata`` object is message content, not the header,
    and must not be able to redefine the mode. Byte-bounding keeps an enormous
    or adversarial first line from pinning memory.

    Blocking file I/O — call from a worker thread, never on the event loop
    (AUTOSDE ``no-blocking-call-on-event-loop``); prefer
    :func:`_probe_persisted_session`. Deliberately uncached: a cache would need
    invalidation on every mode change and could itself go stale, which is the
    exact failure class this closes.
    """
    path = _persisted_session_path(slot_name)
    if path is None:
        return None
    return _read_memory_mode(path)


def _read_memory_mode(path: "Path") -> str | None:
    """Read the ``memory_mode`` out of *path*'s metadata line. See above."""
    try:
        with open(path, "rb") as f:
            head = f.read(_METADATA_HEAD_MAX_BYTES)
    except OSError:
        return None
    first, _sep, _rest = head.partition(b"\n")
    try:
        d = json.loads(first.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("_type") != "metadata":
        return None
    mode = d.get("memory_mode")
    if mode is None:
        # Valid header, field absent -> legacy persistent session.
        return "persistent"
    if not isinstance(mode, str):
        return None
    # Allowlist, not normalize-and-hope: an unrecognised value must read as
    # unknown so the caller fails closed. Case/whitespace matter because the
    # comparison downstream is set membership — `"incognito "` would lower() to
    # itself, miss INCOGNITO_MEMORY_MODES, and be treated as unrestricted. The
    # API validates this field on the way in, but a hand-edited or partially
    # written transcript is not bound by that.
    normalized = mode.strip().lower()
    if normalized not in VALID_MEMORY_MODES:
        return None
    return normalized


def _probe_persisted_session(slot_name: str) -> tuple[bool, str | None]:
    """``(file_exists, memory_mode_or_None)`` for *slot_name*.

    Refuses to guess when the key is **ambiguous**. ``slot_name`` reaches this
    function with its transport namespace already stripped
    (``sk.split(":", 1)[-1]``), so one stem can match several real transcripts —
    e.g. a legacy Slack thread at ``<ts>.jsonl`` and an archived dashboard slot
    named after that same ts at ``dashboard_<ts>.jsonl``. Taking the first
    candidate would let a *persistent* file answer for an *incognito* session and
    permit the write. Existence stays true (a session really does exist), but the
    mode is reported as ``None`` = unknown, which the caller denies on.

    Blocking I/O: hand this to a worker thread from an async caller
    (``await asyncio.to_thread(_probe_persisted_session, slot_name)``). It is a
    single composed call so one thread hop covers the whole probe.
    """
    matches = _persisted_session_paths(slot_name)
    if not matches:
        return False, None
    if len(matches) > 1:
        return True, None
    return True, _read_memory_mode(matches[0])
