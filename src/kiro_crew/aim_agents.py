"""Agent discovery — scans ~/.kiro/agents/ for installed agents.

Provides ``list_agents()`` which returns metadata about all installed
agents, including KiroCrew's own agent and any agents shipped by
locally-installed skill packages (agent config files on disk). It also
merges project-scoped agents discovered under ``{project}/.kiro/agents/``
and recorded in the persisted registry.

Also provides CC (Claude Code) plugin discovery helpers:
- ``list_cc_plugins()`` — installed CC plugin package names (reads disk)
- ``is_cc_plugin_installed()`` — check a single package
- ``install_cc_plugin()`` — no-op in OSS (the optional plugin CLI is absent)
- ``installed_kiro_packages_missing_from_cc()`` — empty in OSS

The optional ``aim`` plugin manager is not part of the public distribution,
so the install/sync helpers degrade gracefully to no-ops when its binary is
absent. ``list_agents()`` remains fully functional — it only reads on-disk
agent config files and has no external-tool dependency.

Each agent is identified by its ``modeId`` — the value passed to
``session/set_mode`` in the ACP protocol to switch the backend's behavior.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.config.loader import config_dir  # noqa: E402 (avoid circular at module load)
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel as _sel

logger = logging.getLogger(__name__)

_KIRO_AGENTS_DIR = Path.home() / ".kiro" / "agents"

# ~/.kiro is kiro-cli's own configuration directory — it is NOT a project dir.
# We must never scan it or register anything inside it as a project agent source.
# Reason: if a user scans from ~ or ~/Documents, os.walk descends into ~/.kiro/agents/
# and finds kirocrew.json, registering it as a project agent at project_path=$HOME.
# That creates a duplicate "kirocrew" entry (global + project) with the same name but
# different project_path keys, which triggers the 409 ambiguity check and breaks the
# kirocrew switch entirely until the user manually deletes the registry entry.
_KIRO_HOME_DIR = (Path.home() / ".kiro").resolve()
_CC_PLUGINS_DIR = Path.home() / ".aim" / "cc-plugins"

_VALID_PACKAGE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9/_.-]*$")

# AIM packages whose agents are treated as kirocrew-owned (orange badge, not purple)
_KIROCREW_AIM_PACKAGES = {"KiroCrewAICapabilities"}

# ── list_agents() result cache ──
# list_agents() reads and JSON-parses every ~/.kiro/agents/*.json on each call.
# Hot callers (agent picker, per-turn agent resolution) call it repeatedly, so an
# uncached scan over 100+ AIM-installed agent files blocks the asyncio event loop.
# Cache the parsed result keyed by (dir, include_project) and reuse it while a cheap
# stat-only directory signature (file count + newest mtime + registry mtime) is
# unchanged — that signature detects adds, removals, and in-place edits.
_ListAgentsSig = tuple[tuple[int, int], int]
_LIST_AGENTS_CACHE: dict[tuple[str, bool], tuple[_ListAgentsSig, list[AimAgent]]] = {}


@dataclass
class AimAgent:
    """Metadata for an installed kiro-cli agent."""

    name: str
    filename: str
    description: str
    model: str
    skills: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    source: str = "builtin"  # "aim" | "kirocrew" | "builtin" | "project"
    package: str = ""  # AIM package name (e.g. "Customer360GenAIContext")
    project_path: str = ""  # project root for contextual launch
    project_name: str = ""  # display name (basename of project_path)
    project_state: str = "ok"  # "ok" | "not_found" — from registry state field

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _extract_skills(data: dict[str, Any]) -> list[str]:
    """Extract skill names from builder-mcp args (--skill-name-filter)."""
    mcp = data.get("mcpServers") or {}
    if not isinstance(mcp, dict):
        return []
    bm = mcp.get("builder-mcp") or {}
    if not isinstance(bm, dict):
        return []
    args = bm.get("args") or []
    skills: list[str] = []
    for i, arg in enumerate(args):
        if arg == "--skill-name-filter" and i + 1 < len(args):
            skills.extend(s.strip() for s in args[i + 1].split(",") if s.strip())
    return skills


def find_agent_file(agents_dir: Path, agent_name: str) -> Path | None:
    """Find the JSON file for *agent_name* inside *agents_dir*.

    Matches on the ``name`` field inside each JSON file — not the filename stem —
    mirroring kiro-cli's own resolution logic for ``--agent <name>``.
    Returns the first matching file path, or None if not found.
    """
    if not agents_dir.is_dir():
        return None
    for f in agents_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("name") == agent_name:
                return f
        except (OSError, ValueError):
            continue
    return None


def _registry_path() -> Path:
    """Path to the project agent registry file."""
    return config_dir() / "project_agents.json"


# ── Registry entry type ──
# Each project entry: {"name": str, "state": "ok"|"not_found", "agents": [{"file": str, "agent_name": str}]}
# agent_name is cached from the JSON name field to avoid file reads at display time.

_REGISTRY_STATE_OK = "ok"
_REGISTRY_STATE_NOT_FOUND = "not_found"


def _parse_registry_entry(raw: Any, project_path: str) -> dict | None:
    """Parse and validate a single registry entry, returning normalised dict or None."""
    if isinstance(raw, list):
        # Old format: list of filenames — migrate on read. agent_name will be
        # populated by the gateway startup pass; use "" as placeholder.
        return {
            "name": Path(project_path).name,
            "state": _REGISTRY_STATE_OK,
            "agents": [{"file": f, "agent_name": ""} for f in raw if isinstance(f, str)],
        }
    if not isinstance(raw, dict):
        return None
    agents_raw = raw.get("agents", [])
    agents: list[dict] = []
    for entry in agents_raw if isinstance(agents_raw, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("file"), str):
            agents.append({
                "file": entry["file"],
                "agent_name": entry.get("agent_name", "") if isinstance(entry.get("agent_name"), str) else "",
            })
        elif isinstance(entry, str):
            # Partial migration — file only, no agent_name yet
            agents.append({"file": entry, "agent_name": ""})
    return {
        "name": raw.get("name") or Path(project_path).name,
        "state": raw.get("state") if raw.get("state") in (_REGISTRY_STATE_OK, _REGISTRY_STATE_NOT_FOUND) else _REGISTRY_STATE_OK,
        "agents": agents,
    }


def load_registry() -> dict[str, dict]:
    """Load the project agent registry.

    Registry format:
        {"/abs/path/ProjectA": {"name": "ProjectA", "state": "ok",
                                 "agents": [{"file": "dev.json", "agent_name": "dev"}]}}

    Handles old list-of-filenames format transparently (migrates on read).
    Returns {} on corruption or missing file.
    """
    p = _registry_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.warning("Project agent registry has unexpected root type — returning empty")
            return {}
        result: dict[str, dict] = {}
        for k, v in data.items():
            if not isinstance(k, str):
                continue
            entry = _parse_registry_entry(v, k)
            if entry is not None:
                result[k] = entry
        return result
    except json.JSONDecodeError:
        logger.warning("Project agent registry is corrupted — returning empty. Rescan to restore.")
        return {}
    except OSError:
        logger.debug("Failed to read project agent registry")
        return {}


def _write_registry(registry: dict[str, dict]) -> None:
    """Write registry to disk. Caller MUST hold the registry lock."""
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="registry-", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(registry, indent=2))
        os.replace(tmp_name, p)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save_registry(data: dict) -> None:
    """Write raw registry data to disk without locking.

    Accepts both old format (``{path: [filenames]}`` list-of-strings) and new
    format (``{path: {name, state, agents}}`` dict).  Used primarily in tests to
    seed fixture data; ``load_registry`` handles format migration on read.
    """
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="registry-", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2))
        os.replace(tmp_name, p)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def update_registry(project_path: str, agent_entries: list[dict]) -> None:
    """Atomically add/update one project entry in the registry.

    *agent_entries* is a list of ``{"file": str, "agent_name": str}`` dicts.
    Lock covers the full load+modify+write sequence.
    """
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.with_suffix(".lock")
    with open(lock_path, "w") as lock_fh, platform_compat.file_lock(
        lock_fh.fileno(), exclusive=True
    ):
        registry = load_registry()
        existing = registry.get(project_path, {})
        registry[project_path] = {
            "name": Path(project_path).name,
            "state": _REGISTRY_STATE_OK,
            "agents": agent_entries,
        }
        # Preserve existing name if user customised it (future-proof)
        if isinstance(existing.get("name"), str) and existing["name"]:
            registry[project_path]["name"] = existing["name"]
        _write_registry(registry)


def remove_from_registry(project_path: str) -> None:
    """Atomically remove one project entry from the registry.

    Idempotent: no-op (and no write) when the key is absent, including on corrupt re-read.
    """
    p = _registry_path()
    if not p.is_file():
        return
    lock_path = p.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_fh, platform_compat.file_lock(
        lock_fh.fileno(), exclusive=True
    ):
        registry = load_registry()
        if project_path in registry:
            del registry[project_path]
            _write_registry(registry)


def refresh_registry_startup() -> None:
    """Gateway startup pass: stat all paths, refresh state + agent_name.

    Called once at gateway start. Combines two operations in a single pass:
    1. Stat each registered project path — set state to ok/not_found
    2. For ok entries, re-read agent files to refresh cached agent_name

    Writes back only if any entry changed.
    """
    registry = load_registry()
    if not registry:
        return

    changed = False
    updated: dict[str, dict] = {}

    for project_path, entry in registry.items():
        agents_dir = Path(project_path) / ".kiro" / "agents"
        new_state = _REGISTRY_STATE_OK if agents_dir.is_dir() else _REGISTRY_STATE_NOT_FOUND
        new_agents: list[dict] = []

        if new_state == _REGISTRY_STATE_OK:
            for agent_entry in entry.get("agents", []):
                fname = agent_entry.get("file", "")
                if not fname:
                    continue
                f = agents_dir / fname
                agent_name = agent_entry.get("agent_name", "")
                if f.is_file():
                    try:
                        data = json.loads(f.read_text(encoding="utf-8"))
                        if isinstance(data, dict) and isinstance(data.get("name"), str):
                            agent_name = data["name"]
                    except Exception:
                        pass
                if agent_name or fname:
                    new_agents.append({"file": fname, "agent_name": agent_name})
        else:
            new_agents = entry.get("agents", [])

        new_entry = {
            "name": entry.get("name") or Path(project_path).name,
            "state": new_state,
            "agents": new_agents,
        }
        if new_entry != entry:
            changed = True
        updated[project_path] = new_entry

    if changed:
        p = _registry_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        lock_path = p.with_suffix(".lock")
        with open(lock_path, "w") as lock_fh, platform_compat.file_lock(
            lock_fh.fileno(), exclusive=True
        ):
            # Re-read under lock to avoid overwriting concurrent updates
            # (e.g. update_registry called between the unlocked read above and here)
            fresh = load_registry()
            for project_path, new_entry in updated.items():
                if project_path in fresh:
                    # Only update state+agents; preserve any concurrent additions to other fields
                    fresh[project_path]["state"] = new_entry["state"]
                    fresh[project_path]["agents"] = new_entry["agents"]
                    fresh[project_path]["name"] = new_entry["name"]
                # Don't re-add entries removed by concurrent remove_from_registry()
            _write_registry(fresh)
        logger.debug("registry_startup_pass: refreshed %d entries", len(updated))


_SCAN_PRUNE = frozenset({
    "node_modules", "__pycache__", "build", "dist", ".git", ".hg",
    ".cache", "env", "venv", ".venv", ".tox", "target", ".gradle",
    ".idea", ".vs", "out", ".next", ".nuxt",
    # Large user-data / vendor trees
    "vendor", ".cargo", "Library", "Pods", "Applications", ".rustup",
})

_SCAN_MAX_DEPTH = 8
_SCAN_MAX_ENTRIES = 50_000


def scan_directory(
    root: str | Path,
    max_depth: int = _SCAN_MAX_DEPTH,
    max_entries: int = _SCAN_MAX_ENTRIES,
) -> list[AimAgent]:
    """Scan a directory tree for projects with .kiro/agents/ and register them.

    Uses os.walk with smart pruning — skips hidden dirs, node_modules, build
    artifacts, venvs, and common large user-data directories.

    Stops at *max_depth* levels below *root* and aborts with a warning if the
    cumulative directory-entry count exceeds *max_entries*, preventing runaway
    scans of very large trees.

    Returns the list of newly discovered project agents.
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        return []
    if is_sensitive_path(str(root)):
        logger.warning("Refusing to scan sensitive path: %s", root)
        _sel().log_api_access(
            caller="aim_agents",
            operation="scan_directory",
            outcome="denied",
            source="scan_directory",
            resources=str(root),
            error="sensitive path rejected",
        )
        return []

    discovered: list[AimAgent] = []
    root_depth = len(root.parts)
    entries_seen = 0

    for dirpath, dirnames, _filenames in os.walk(root):
        current_depth = len(Path(dirpath).parts) - root_depth
        if current_depth >= max_depth:
            dirnames.clear()
            continue

        # Prune hidden dirs and known dead-ends
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") or d == ".kiro"
        ]
        dirnames[:] = [d for d in dirnames if d not in _SCAN_PRUNE]

        # Count files too: os.scandir() enumerates dirs and files in one syscall,
        # so a dir with 100k files costs as much as 100k dirs. Counting both ensures
        # the abort fires on wide flat trees, not just deep ones. A name-based or
        # extension-based heuristic would miss legitimate project roots with many files.
        entries_seen += len(dirnames) + len(_filenames)
        if entries_seen > max_entries:
            logger.warning(
                "scan_directory aborted at %s: %d entries scanned (limit %d)",
                dirpath, entries_seen, max_entries,
            )
            break

        # Check if this directory IS a .kiro/agents/ dir
        p = Path(dirpath)
        if p.name == "agents" and p.parent.name == ".kiro":
            # Guard: never register ~/.kiro or anything nested inside it as a project.
            # ~/.kiro IS the global kiro-cli config dir — not a project root.
            # We explicitly allow .kiro dirs during the walk (to find project agents),
            # but we must stop before treating ~/.kiro/agents/ itself as a project.
            # is_relative_to covers subdirs too (e.g. ~/.kiro/subdir/.kiro/agents/).
            if p.parent.resolve().is_relative_to(_KIRO_HOME_DIR) or p.parent.resolve() == _KIRO_HOME_DIR:
                dirnames.clear()
                _sel().log_api_access(
                    caller="aim_agents",
                    operation="scan_directory",
                    outcome="denied",
                    source="scan_directory",
                    resources=str(p.parent.resolve()),
                    error="~/.kiro is not a project directory",
                )
                continue
            project_path = str(p.parent.parent)
            agent_files: list[str] = []
            for f in p.glob("*.json"):
                if f.name.startswith("._"):
                    continue
                try:
                    real = f.resolve(strict=True)
                except OSError:
                    continue
                if is_sensitive_path(str(real)):
                    logger.debug("Skipping sensitive project agent: %s", f)
                    _sel().log_api_access(
                        caller="aim_agents",
                        operation="scan_directory",
                        outcome="denied",
                        source="scan_directory",
                        resources=str(real),
                        error="sensitive path rejected",
                    )
                    continue
                agent_files.append(f.name)  # record filename regardless of parse
                try:
                    raw = real.read_bytes()
                    try:
                        text = raw.decode("utf-8")
                    except (UnicodeDecodeError, ValueError):
                        logger.debug("Skipping non-UTF-8 agent config: %s", f)
                        continue
                    data = json.loads(text)
                    if not isinstance(data, dict):
                        continue
                    mcp_raw = data.get("mcpServers") or {}
                    resolved_agent_name = data.get("name") or f.stem
                    agent = AimAgent(
                        name=resolved_agent_name,
                        filename=f.name,
                        description=data.get("description", ""),
                        model=data.get("model", "auto"),
                        skills=_extract_skills(data),
                        mcp_servers=list(mcp_raw.keys()) if isinstance(mcp_raw, dict) else [],
                        source="project",
                        project_path=project_path,
                    )
                    discovered.append(agent)
                    agent_files[-1] = {"file": f.name, "agent_name": resolved_agent_name}  # type: ignore
                except Exception:
                    logger.debug("Skipping invalid project agent: %s", f)
            # Filter: agent_files may contain strings (failed-parse) or dicts (successful)
            # Build proper list for registry
            registry_entries: list[dict] = []
            for item in agent_files:
                if isinstance(item, dict):
                    registry_entries.append(item)
                elif isinstance(item, str):
                    registry_entries.append({"file": item, "agent_name": ""})
            if registry_entries:
                update_registry(project_path, registry_entries)
            # Don't recurse into .kiro/agents/
            dirnames.clear()

    return discovered


def auto_register_project(project_path: str) -> None:
    """Directly read {project}/.kiro/agents/ and update the registry for that project only.

    No os.walk — reads one directory. Called on project-set to populate the
    registry instantly without a full tree scan.

    Guards against ~/.kiro being passed as project_path (would register $HOME as a project
    and create duplicate kirocrew entries triggering 409 on every agent switch).
    """
    root = Path(project_path).expanduser().resolve()
    # Defense-in-depth: refuse to treat ~/.kiro (the global kiro-cli config dir) as a project.
    if root.is_relative_to(_KIRO_HOME_DIR) or root == _KIRO_HOME_DIR.parent:
        logger.debug("Refusing to register ~/.kiro parent as project: %s", root)
        _sel().log_api_access(
            caller="aim_agents",
            operation="auto_register_project",
            outcome="denied",
            source="auto_register_project",
            resources=str(root),
            error="~/.kiro is not a project directory",
        )
        return
    if is_sensitive_path(str(root)):
        _sel().log_api_access(
            caller="aim_agents",
            operation="auto_register_project",
            outcome="denied",
            source="auto_register_project",
            resources=str(root),
            error="sensitive root path rejected",
        )
        return
    agents_dir = root / ".kiro" / "agents"
    if not agents_dir.is_dir():
        return
    filenames: list[str] = []
    for f in agents_dir.glob("*.json"):
        if f.name.startswith("._"):
            continue
        try:
            real = f.resolve(strict=True)
        except OSError:
            continue
        if is_sensitive_path(str(real)):
            _sel().log_api_access(
                caller="aim_agents",
                operation="auto_register_project",
                outcome="denied",
                source="auto_register_project",
                resources=str(real),
                error="sensitive path rejected",
            )
            continue
        filenames.append(f.name)
    if filenames:
        # Read agent names for registry cache
        agent_entries: list[dict] = []
        for fname in filenames:
            f = agents_dir / fname
            agent_name = ""
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("name"), str):
                    agent_name = data["name"]
            except Exception:
                pass
            agent_entries.append({"file": fname, "agent_name": agent_name})
        update_registry(str(root), agent_entries)
        logger.debug("Auto-registered %d agent(s) for %s", len(filenames), root)


def _load_project_agents() -> list[AimAgent]:
    """Load all project agents from the persisted registry (no file reads).

    Uses cached ``agent_name`` and ``state`` from the registry.
    The ``state`` field distinguishes ok (selectable) from not_found (grayed).
    Gateway startup pass refreshes both fields.
    """
    registry = load_registry()
    agents: list[AimAgent] = []
    for project_path, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        # Guard: never surface ~/.kiro as a project (defensive read-time check)
        try:
            if Path(project_path).resolve().is_relative_to(_KIRO_HOME_DIR):
                continue
        except (OSError, ValueError):
            pass
        state = entry.get("state", _REGISTRY_STATE_OK)
        project_name = entry.get("name") or Path(project_path).name
        for agent_entry in entry.get("agents", []):
            if not isinstance(agent_entry, dict):
                continue
            fname = agent_entry.get("file", "")
            agent_name = agent_entry.get("agent_name", "") or Path(fname).stem
            if not fname or not agent_name:
                continue
            agents.append(
                AimAgent(
                    name=agent_name,
                    filename=fname,
                    description="",
                    model="auto",
                    source="project",
                    project_path=project_path,
                    project_name=project_name,
                    project_state=state,
                )
            )
    return agents


def _dir_signature(d: Path, include_project: bool) -> _ListAgentsSig:
    """Cheap stat-only signature of the agents dir (+ project registry).

    Captures the JSON file count and newest mtime — enough to detect adds,
    removals, and in-place edits without reading or parsing any file — plus
    the project-registry mtime when project agents are included. Used to
    invalidate the :func:`list_agents` result cache.
    """
    count = 0
    max_mtime = 0
    try:
        with os.scandir(d) as it:
            for entry in it:
                if not entry.name.endswith(".json"):
                    continue
                count += 1
                try:
                    m = entry.stat().st_mtime_ns
                except OSError:
                    m = 0
                if m > max_mtime:
                    max_mtime = m
    except OSError:
        pass
    reg_mtime = 0
    if include_project:
        try:
            reg_mtime = _registry_path().stat().st_mtime_ns
        except OSError:
            reg_mtime = 0
    return ((count, max_mtime), reg_mtime)


def clear_list_agents_cache() -> None:
    """Drop all cached :func:`list_agents` results (forces a fresh scan next call).

    Invalidation is normally automatic via the directory signature; call this
    only to force an immediate refresh (e.g. right after writing an agent file).
    """
    _LIST_AGENTS_CACHE.clear()


def list_agents(
    agents_dir: Path | None = None,
    include_project: bool = True,
) -> list[AimAgent]:
    """Scan ~/.kiro/agents/*.json for all installed agents.

    When *include_project* is True (default), also includes project-scoped
    agents from the persisted registry (populated by ``scan_directory``).

    Returns a list of ``AimAgent`` objects sorted by name. Each agent
    corresponds to a kiro-cli agent config file that can be selected
    via ``session/set_mode`` in the ACP protocol.

    Results are cached per (directory, include_project) and reused while the
    directory signature is unchanged, so repeated calls avoid re-reading and
    re-parsing every agent JSON on the event loop.
    """
    d = agents_dir or _KIRO_AGENTS_DIR
    cache_key = (str(d), include_project)
    signature = _dir_signature(d, include_project)
    cached = _LIST_AGENTS_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        return list(cached[1])

    agents: list[AimAgent] = []

    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            try:
                # Skip AppleDouble sidecars and sensitive symlink targets
                if f.name.startswith("._"):
                    continue
                try:
                    real = f.resolve(strict=True)
                except OSError:
                    continue
                if is_sensitive_path(str(real)):
                    logger.debug("Skipping sensitive agent config: %s", f)
                    _sel().log_api_access(
                        caller="aim_agents",
                        operation="list_agents",
                        outcome="denied",
                        source="list_agents",
                        resources=str(real),
                        error="sensitive path rejected",
                    )
                    continue
                raw = real.read_bytes()
                try:
                    text = raw.decode("utf-8")
                except (UnicodeDecodeError, ValueError):
                    logger.debug("Skipping non-UTF-8 agent config: %s", f)
                    continue
                data = json.loads(text)
                if not isinstance(data, dict):
                    logger.debug("Skipping non-object agent config: %s", f)
                    continue
                agent_name = data.get("name", "")
                stem = f.stem

                package = ""
                is_aim_filename = (
                    agent_name and stem.endswith(agent_name) and stem != agent_name
                )
                if is_aim_filename:
                    pkg_stem = f.stem
                    if pkg_stem.startswith("local-"):
                        pkg_stem = pkg_stem[len("local-") :]
                    package = pkg_stem[: -(len(agent_name) + 1)]

                if f.name in ("kirocrew.json", "kirocrew-lite.json"):
                    source = "kirocrew"
                elif is_aim_filename:
                    source = "kirocrew" if package in _KIROCREW_AIM_PACKAGES else "aim"
                else:
                    source = "builtin"

                agents.append(
                    AimAgent(
                        name=data.get("name", f.stem),
                        filename=f.name,
                        description=data.get("description", ""),
                        model=data.get("model", "auto"),
                        skills=_extract_skills(data),
                        mcp_servers=list((data.get("mcpServers") or {}).keys())
                        if isinstance(data.get("mcpServers") or {}, dict)
                        else [],
                        source=source,
                        package=package,
                    )
                )
            except Exception:
                logger.debug("Skipping invalid agent config: %s", f)
                continue

    # Merge project agents from registry
    if include_project:
        try:
            agents.extend(_load_project_agents())
        except Exception:
            logger.warning("Failed to load project agents", exc_info=True)

    # Deduplicate: project agents with a project_path are keyed by name+path
    # (allows same-name agents in different projects). A project agent also
    # shadows any global agent with the same name.
    seen: dict[str, AimAgent] = {}
    for a in agents:
        key = f"{a.name}:{a.project_path}" if a.project_path else a.name
        existing = seen.get(key)
        if existing is None:
            seen[key] = a
        elif a.package and not existing.package:
            seen[key] = a
        elif a.package and existing.package:
            logger.warning(
                "Duplicate agent name '%s' from packages '%s' and '%s'; keeping '%s'",
                a.name,
                existing.package,
                a.package,
                existing.package,
            )
    result = list(seen.values())
    _LIST_AGENTS_CACHE[cache_key] = (signature, result)
    return list(result)


# ---------------------------------------------------------------------------
# Claude Code plugin discovery and installation
# ---------------------------------------------------------------------------


def list_cc_plugins() -> list[str]:
    """Return package names of installed CC plugins from the AIM marketplace.

    Reads ``~/.aim/cc-plugins/.claude-plugin/marketplace.json`` if it exists.
    Returns an empty list if AIM is not installed or the file is missing.
    """
    marketplace = _CC_PLUGINS_DIR / ".claude-plugin" / "marketplace.json"
    if not marketplace.is_file():
        return []
    try:
        data = json.loads(marketplace.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.debug("Cannot read CC plugins marketplace.json")
        return []
    # marketplace.json is an array of objects with a "packageName" field
    if isinstance(data, list):
        return [
            entry["packageName"]
            for entry in data
            if isinstance(entry, dict) and entry.get("packageName")
        ]
    # Alternate format: dict with "plugins" key
    if isinstance(data, dict):
        plugins = data.get("plugins", [])
        if isinstance(plugins, list):
            return [
                entry["packageName"]
                for entry in plugins
                if isinstance(entry, dict) and entry.get("packageName")
            ]
    return []


def is_cc_plugin_installed(pkg: str) -> bool:
    """Check if a specific AIM package is installed as a CC plugin."""
    return pkg in list_cc_plugins()


def _ensure_standalone_mode() -> bool:
    """No-op in OSS — the optional plugin manager config is not managed here.

    Preserved for API compatibility. Always returns True; writes nothing.
    """
    return True


def install_cc_plugin(pkg: str, *, standalone: bool = True) -> tuple[bool, str]:
    """Install a package as a CC plugin (no-op in this distribution).

    The optional plugin manager used to perform installs is not part of the
    public distribution, so this degrades to a graceful no-op rather than
    shelling out to an absent binary.

    Args:
        pkg: Package name (validated for safety, otherwise unused).
        standalone: Accepted for API compatibility; ignored.

    Returns:
        (success, message) tuple. ``success`` is always False here.
    """
    if not _VALID_PACKAGE_RE.match(pkg) or ".." in pkg:
        return False, f"Invalid package name: {pkg!r}"
    return False, "Plugin install is not available in this distribution"


def _list_kiro_packages() -> set[str]:
    """Return installed plugin-manager package names — empty in OSS.

    The optional plugin manager CLI is absent in the public distribution, so
    there are no externally-tracked packages to enumerate. Returns an empty
    set (no subprocess spawned).
    """
    return set()


def installed_kiro_packages_missing_from_cc() -> list[str]:
    """Return packages installed for the agent backend but missing from CC.

    With no external plugin manager in the public distribution there is
    nothing to diff, so this always returns an empty list.
    """
    return []
