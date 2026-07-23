"""Shared helpers used across handler submodules."""

from __future__ import annotations

import fnmatch
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import is_sensitive_path
from kiro_crew.skills import skills_dir

if TYPE_CHECKING:
    from kiro_crew.platform.interfaces import CapabilityManager

logger = logging.getLogger(__name__)


def _capability_manager() -> "CapabilityManager":
    """The edition's external capability manager (CPP seam).

    Lives in the shared layer (not a leaf handler) so every consumer —
    ``agents.py`` handlers, ``mcp.py`` uninstall, and the skill/prompt listers
    here — imports it DOWNWARD with no circular dependency. Operations-based: the
    edition owns its CLI grammar, output parsing, and error translation. Fails closed to
    an unavailable ``DefaultCapabilityManager`` so ``/api/capability/*`` degrade
    to 503 rather than crashing.
    """
    from kiro_crew.platform.context import current_context, safe_context_call
    from kiro_crew.platform.defaults import DefaultCapabilityManager

    return safe_context_call(
        lambda: current_context().capability_manager,
        fallback_factory=DefaultCapabilityManager,
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
    there are no roots to discover and the AIM-flavored skill helpers below
    return "nothing found" rather than globbing a hardcoded ``~/.aim``.
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
    instead of globbing ``~/.aim`` directly. Within each root a skill lives at
    either ``<root>/<pkg>/<name>/SKILL.md`` or ``<root>/<name>/SKILL.md``; the
    first match (roots in seam order) wins.
    """
    for root in _edition_skill_roots():
        for pattern in (f"*/{name}/SKILL.md", f"{name}/SKILL.md"):
            for p in root.glob(pattern):
                return p
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
    user = Path.home() / ".kiro" / "agents"
    if user.is_dir():
        out.append(user)
    return out


def _expand_resource_uri(uri: str, agent_path: Path) -> str | None:
    """Strip ``skill://`` prefix and resolve ``~`` and workspace-relative paths.

    kiro-cli accepts both ``skill://~/.kiro/skills/*/SKILL.md`` (global)
    and ``skill://.kiro/skills/*/SKILL.md`` (workspace, relative to the
    process cwd at session start).  We resolve workspace-relative globs
    relative to the agent file's directory's ancestor chain — best-effort
    since we don't know the exact cwd kiro-cli will use.

    Returns a glob pattern usable with fnmatch, or None if not a skill URI.
    """
    if not uri.startswith("skill://"):
        return None
    raw = uri[len("skill://") :]
    if raw.startswith("~/"):
        raw = str(Path.home() / raw[2:])
    elif raw.startswith("/"):
        pass  # absolute
    else:
        # Workspace-relative (e.g. ``.kiro/skills/*/SKILL.md``): resolve
        # against the project root.  For the typical layout
        # ``<project>/.kiro/agents/foo.json`` the project root is three
        # levels up (foo.json → agents → .kiro → <project>); appending the
        # ``.kiro/...``-prefixed glob then yields ``<project>/.kiro/...``.
        # Going only two levels up would double the ``.kiro`` segment.
        # Best-effort — the cwd kiro-cli uses at session start may differ.
        candidate = agent_path.parent.parent.parent / raw
        raw = str(candidate)
    return raw


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
    whole thing to a thread via ``run_in_executor`` — previously only the
    agent annotation was offloaded while ``list_skills()`` (os.walk +
    per-file frontmatter reads) and ``list_kiro_skills()`` (per-skill resolve +
    read) still ran on the event loop and could stall it past the loop-stall
    watchdog on large catalogs.

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
    - ``foo`` → ``~/.kirocrew/skills/foo``
    - ``utils/tiny-url`` → ``~/.kirocrew/skills/utils/tiny-url``
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
        # paths under any active slot's project_dir.
        proj: Path | None = None
        for slot in getattr(state, "_slots", {}).values():
            pd = getattr(slot, "project_dir", None)
            if pd:
                proj = Path(pd)
                break
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
        from kiro_crew.slack.handler import is_thread_incognito, is_thread_temporary

        if is_thread_temporary(sk) or is_thread_incognito(sk):
            return True
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
        from kiro_crew.slack.handler import is_thread_temporary

        if is_thread_temporary(sk):
            return True
    return False


def _session_has_persisted_history(slot_name: str) -> bool:
    """Return True iff the slot has a JSONL file in ~/.kirocrew/sessions/.

    This is a positive signal that the session was previously established
    as non-ephemeral: ephemeral (incognito/temporary) sessions never write
    to disk, so a persisted JSONL can only come from a real user session.

    Used by ``api_lessons_create`` to distinguish between:

    * A legitimate MCP subprocess whose in-memory slot was evicted by the
      idle-sweep loop (``session.py``'s 30-minute timeout). The subprocess
      still holds the original ``KIROCREW_SESSION_KEY`` env var, so it
      keeps sending the same ``X-Session-Key``, but ``state._slots`` has
      moved on. Without this check such calls return HTTP 400 ``unknown
      session`` even though the user is actively typing in the thread.

    * A forged or stale key from a context that never had a real session
      backing it — which should continue to be rejected.

    Only checks existence, not contents. Authentication of the caller is
    still enforced by the ``X-Internal-Secret`` middleware upstream; this
    check only governs the *ephemeral vs non-ephemeral* distinction.
    """
    if (
        not slot_name
        or "/" in slot_name
        or "\\" in slot_name
        or "\x00" in slot_name
        or slot_name.startswith(".")
    ):
        # Defence-in-depth against path traversal; ``KIROCREW_SESSION_KEY``
        # normally has no path separators, but ``X-Session-Key`` is
        # attacker-controlled in principle even behind the secret
        # middleware. Reject forward slash (Linux/macOS) and backslash
        # (Windows) path separators, null bytes that can truncate C-level
        # path parsing, and leading dots that could target hidden
        # per-directory files outside the intended session namespace.
        return False
    sess_dir = Path.home() / ".kirocrew" / "sessions"
    if not sess_dir.exists():
        return False
    # Match the resolution order used by slack/interactions.py when
    # linking Slack threads to existing sessions: bare stem first, then
    # the ``dashboard_`` prefix fallback for dashboard slots. Cron sessions
    # persist under different names: ``history._safe_key`` folds ``:`` to
    # ``_``, so ``cron:{id}`` writes ``cron_{id}.jsonl`` and its linked
    # dashboard slot ``dashboard:cron-{id}`` writes ``dashboard_cron-{id}.jsonl``.
    # Probe those too so an idle-evicted cron session is recognised rather
    # than misclassified as forged.
    if (sess_dir / f"{slot_name}.jsonl").exists():
        return True
    if not slot_name.startswith("dashboard_") and (
        sess_dir / f"dashboard_{slot_name}.jsonl"
    ).exists():
        return True
    if (sess_dir / f"cron_{slot_name}.jsonl").exists():
        return True
    if (sess_dir / f"dashboard_cron-{slot_name}.jsonl").exists():
        return True
    return False
