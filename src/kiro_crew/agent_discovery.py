"""Agent discovery — scans ~/.kiro/agents/ for installed agents.

Provides ``list_agents()`` which returns metadata about all installed
agents, including KiroCrew's own agent and any agents shipped by
locally-installed skill packages (agent config files on disk). It only
reads on-disk agent config files and has no external-tool dependency.

Each agent is identified by its ``modeId`` — the value passed to
``session/set_mode`` in the ACP protocol to switch the backend's behavior.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel as _sel

logger = logging.getLogger(__name__)

# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage.py is the reference implementation.
_KIRO_AGENTS_DIR: Path | None = None


def _kiro_agents_dir() -> Path:
    """The kiro-cli agents directory, resolved against the live data home."""
    return _KIRO_AGENTS_DIR if _KIRO_AGENTS_DIR is not None else kiro_agents_dir()


# ── list_agents() result cache ──
# list_agents() reads and JSON-parses every ~/.kiro/agents/*.json on each call.
# Hot callers (agent picker, per-turn agent resolution) call it repeatedly, so an
# uncached scan over 100+ AIM-installed agent files blocks the asyncio event loop.
# Cache the parsed result keyed by directory and reuse it while a cheap stat-only
# directory signature (file count + newest mtime) is unchanged — that signature
# detects adds, removals, and in-place edits.
_ListAgentsSig = tuple[int, int]
_LIST_AGENTS_CACHE: dict[str, tuple[_ListAgentsSig, list[AgentInfo]]] = {}


@dataclass
class AgentInfo:
    """Metadata for an installed kiro-cli agent."""

    name: str
    filename: str
    description: str
    model: str
    skills: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    source: str = "builtin"  # "kirocrew" | "package" | "builtin"
    package: str = ""  # AIM package name (e.g. "Customer360GenAIContext")

    def __post_init__(self) -> None:
        """Make the annotations above TRUE, at every construction site.

        ``~/.kiro/agents`` is a SHARED directory: ACP adapters and IDE plugins
        drop their own specs there and do not all spell every field as a plain
        string (observed: ``"model": {"id": "anthropic:claude-opus-4-8"}``, and
        bare ``null``). ``to_dict()`` is what ``/api/agents/installed``
        serialises, so any such value reached the dashboard verbatim; rendered as
        a JSX child it threw React error #31 ("Objects are not valid as a React
        child") and put the WHOLE Agent Templates tab into the error boundary —
        every other agent's row with it.

        The enforcement lives HERE rather than in per-field calls at each caller
        because the fields are rendered bare in several places (`{a.name}`,
        `{a.package}`, `<SourceBadge source={a.source}>`, `a.filename.startsWith`)
        and there are two construction sites, one of them an out-of-tree edition
        seam. A per-field fix at one caller only *looks* complete: the next
        foreign spelling, or the next field someone renders, reopens the same
        whole-tab crash. A constructor invariant cannot be forgotten.

        Fallbacks are per field because they are not interchangeable: ``model``
        defers to ``"auto"`` (see :func:`spec_model` — the same "non-string means
        no pin" rule the execution path applies), ``source`` to its
        ``"builtin"`` default, and the free-text fields to empty.
        """
        for name, fallback in (
            ("name", ""),
            ("filename", ""),
            ("description", ""),
            ("model", _DEFER_MODEL),
            ("source", "builtin"),
            ("package", ""),
        ):
            if not isinstance(getattr(self, name), str):
                setattr(self, name, fallback)
        # ``list[str]`` is equally load-bearing: these elements are rendered as
        # skill/server chips. Drop the unusable ones rather than the whole list,
        # so a spec with one bad entry keeps the rest.
        self.skills = [s for s in self.skills if isinstance(s, str)]
        self.mcp_servers = [s for s in self.mcp_servers if isinstance(s, str)]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SKILL_URI_PREFIX = "skill://"

# The "no pin, defer to the tier below" spelling. Mirrors
# ``config.loader.DEFAULT_MODEL``; duplicated as a literal rather than imported
# so this module keeps its leaf-level import graph (``config.paths`` only).
_DEFER_MODEL = "auto"


def spec_str(data: dict[str, Any], key: str, default: str = "") -> str:
    """Read a raw spec field as a ``str``, for callers that are NOT an AgentInfo.

    ``AgentInfo.__post_init__`` is what enforces the type contract for the
    dataclass; this is its counterpart for the two places that hand spec fields
    to the dashboard WITHOUT going through it:

    - ``api_agent_detail``, which returns ``{**data, ...}`` — the raw on-disk spec
      — so the detail panel receives whatever the file happened to contain.
    - ``list_agents``, for ``name``, where the useful fallback is the file's own
      stem rather than the generic empty string.

    ``~/.kiro/agents`` is a SHARED directory: other tools (ACP adapters, IDE
    plugins) drop their own specs there and do not all spell every field as a
    plain string. Observed in the wild: ``"model": {"id":
    "anthropic:claude-opus-4-8"}``, and a bare ``null``. Rendered as a React
    child, either throws error #31 and blanks the whole Agent Templates tab.
    """
    value = data.get(key, default)
    return value if isinstance(value, str) else default


def spec_model(data: dict[str, Any]) -> str:
    """The ``model`` of an agent spec, coerced to the ``str`` AgentInfo declares.

    A non-string is treated as "no pin" (``"auto"``), which is exactly the rule
    :func:`config.loader.normalize_agent_model` already applies to the same file
    on the EXECUTION path. Keeping both sides on that rule is deliberate: the
    resolver would collapse a structured model to "defer" regardless, so
    extracting ``id`` here would make the displayed chip disagree with the model
    actually used — and ``anthropic:claude-opus-4-8`` is a provider-prefixed id
    kiro-cli would reject anyway, turning a display bug into a spawn failure.

    It also leaked into ``subagent.py``'s spawn kwargs as a ``--model`` argument.
    """
    return spec_str(data, "model", _DEFER_MODEL)


def _builder_mcp_skills(data: dict[str, Any]) -> list[str]:
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


def skill_resource_uris(data: dict[str, Any]) -> list[str]:
    """Return the ``skill://`` entries of an agent spec's ``resources``, in order.

    These are the kiro-cli-native mapping of skills to an agent: kiro-cli loads
    every ``SKILL.md`` matched by these URIs when the agent is spawned with
    ``--agent``. Non-``skill://`` resources (``file://`` steering globs and
    friends) are user-owned and deliberately excluded.
    """
    resources = data.get("resources") or []
    if not isinstance(resources, list):
        return []
    return [r for r in resources if isinstance(r, str) and r.startswith(SKILL_URI_PREFIX)]


def _skills_from_resources(data: dict[str, Any]) -> list[str]:
    """Derive display names for the skills an agent maps via ``skill://``.

    Lexical only (no filesystem access) so :func:`list_agents` stays a
    stat+parse operation: a skill directory is named by the path segment
    directly above ``SKILL.md``, which is the skill's name in every supported
    layout. A wildcard segment (``skill://~/.kiro/skills/*/SKILL.md`` — "every
    skill in this root") has no single name, so the pattern is surfaced
    verbatim rather than silently dropped.
    """
    names: list[str] = []
    for uri in skill_resource_uris(data):
        parts = [p for p in uri[len(SKILL_URI_PREFIX) :].split("/") if p]
        if not parts:
            continue
        # Trailing SKILL.md (or any *.md) is the file, not the skill name.
        if parts[-1].lower().endswith(".md"):
            parts.pop()
        if parts:
            names.append(parts[-1])
    return names


def _extract_skills(data: dict[str, Any]) -> list[str]:
    """All skills mapped to an agent, de-duplicated, order-preserving.

    Two independent mapping mechanisms are unioned:

    1. ``skill://`` entries in ``resources`` — the kiro-cli-native mapping
       (what the dashboard's Agent Templates editor writes).
    2. ``builder-mcp --skill-name-filter`` args — an edition-specific
       convention that predates the ``resources`` support.
    """
    seen: set[str] = set()
    out: list[str] = []
    for name in (*_skills_from_resources(data), *_builder_mcp_skills(data)):
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def expand_skill_uri(uri: str, agent_path: Path) -> str | None:
    """Expand a ``skill://`` resource URI into an fnmatch glob over real paths.

    kiro-cli accepts ``skill://~/.kiro/skills/*/SKILL.md`` (global),
    ``skill:///abs/path/SKILL.md`` (absolute), and
    ``skill://.kiro/skills/*/SKILL.md`` (workspace-relative to the cwd at
    session start). Workspace-relative URIs are resolved against the project
    root inferred from *agent_path* — for the ``<project>/.kiro/agents/foo.json``
    layout that is three levels up (``foo.json`` -> ``agents`` -> ``.kiro`` ->
    ``<project>``), so appending the ``.kiro/``-prefixed glob yields
    ``<project>/.kiro/...`` without doubling the ``.kiro`` segment. Best-effort:
    the cwd kiro-cli actually uses may differ.

    Returns ``None`` for anything that is not a ``skill://`` URI.
    """
    if not uri.startswith(SKILL_URI_PREFIX):
        return None
    raw = uri[len(SKILL_URI_PREFIX) :]
    if raw.startswith("~/"):
        return str(Path.home() / raw[2:])
    if raw.startswith("/"):
        return raw
    return str(agent_path.parent.parent.parent / raw)


def agent_skill_globs(agent: str, agents_dir: Path | None = None) -> list[str]:
    """Return fnmatch globs for the skills mapped to *agent*, or ``[]``.

    An empty list means "this agent has no explicit skill mapping" — callers
    treat that as the legacy all-or-nothing default rather than as "no skills".
    Best-effort and never raises: an unreadable, invalid, or sensitive-path
    agent file yields ``[]``.
    """
    d = agents_dir or _kiro_agents_dir()
    if not agent or not d.is_dir():
        return []
    try:
        candidates = sorted(d.glob("*.json"))
    except OSError:
        return []
    for f in candidates:
        if f.name.startswith("._"):
            continue
        try:
            real = f.resolve(strict=True)
        except OSError:
            continue
        if is_sensitive_path(str(real)):
            continue
        try:
            data = json.loads(real.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("name") != agent and f.stem != agent:
            continue
        globs = [g for uri in skill_resource_uris(data) if (g := expand_skill_uri(uri, f))]
        return globs
    return []


def _dir_signature(d: Path) -> _ListAgentsSig:
    """Cheap stat-only signature of the agents dir.

    Captures the JSON file count and newest mtime — enough to detect adds,
    removals, and in-place edits without reading or parsing any file. Used to
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
    return (count, max_mtime)


def clear_list_agents_cache() -> None:
    """Drop all cached :func:`list_agents` results (forces a fresh scan next call).

    Invalidation is normally automatic via the directory signature; call this
    only to force an immediate refresh (e.g. right after writing an agent file).
    """
    _LIST_AGENTS_CACHE.clear()


def _with_edition_agents(disk_agents: list[AgentInfo]) -> list[AgentInfo]:
    """Merge edition-contributed agent-catalog rows onto the on-disk scan.

    Reads ``AgentCatalogProvider.builtin_agents()`` through the platform context
    (deferred import so this module never imports the platform package at load
    time; fails closed to no extra agents). ADD-only and de-duped by name — an
    on-disk agent of the same name wins. The public Default returns ``[]`` so
    this is a no-op for the standalone edition.
    """
    from kiro_crew.platform.context import current_context, safe_context_call

    rows: list[dict[str, Any]] = safe_context_call(
        lambda: list(current_context().agent_catalog.builtin_agents()),
        fallback_factory=list,
        log_message="builtin_agents lookup failed; using none",
    )
    if not rows:
        return list(disk_agents)
    by_name: dict[str, AgentInfo] = {a.name: a for a in disk_agents}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        # A row keyed by a non-string name has no usable identity: the name IS
        # the dedup key, the React list key, and the argument every mutation
        # (agentDetail / agentPatch / setDefaultAgent) is addressed by. Blanking
        # it via __post_init__ would yield an unselectable row that also collides
        # with any other nameless row, so such a row is skipped outright — unlike
        # the cosmetic fields, which degrade.
        if not isinstance(name, str) or not name or name in by_name:
            continue
        try:
            by_name[name] = AgentInfo(
                name=name,
                filename=row.get("filename", ""),
                # Every other field is passed through as-is: this seam is
                # out-of-tree, so ``__post_init__`` — not this call site — is what
                # guarantees the declared ``str`` / ``list[str]`` types hold.
                # ``model`` still goes through spec_model() because its "defer"
                # spelling is domain knowledge, not a generic type fallback.
                description=row.get("description", ""),
                model=spec_model(row),
                skills=list(row.get("skills") or []),
                mcp_servers=list(row.get("mcp_servers") or []),
                source=row.get("source", "builtin"),
                package=row.get("package", ""),
            )
        except Exception:
            logger.debug("Skipping malformed edition agent row: %r", row)
    return list(by_name.values())


def list_agents(agents_dir: Path | None = None) -> list[AgentInfo]:
    """Scan ~/.kiro/agents/*.json for all installed agents.

    Returns a list of ``AgentInfo`` objects sorted by name. Each agent
    corresponds to a kiro-cli agent config file that can be selected
    via ``session/set_mode`` in the ACP protocol.

    Results are cached per directory and reused while the directory signature
    is unchanged, so repeated calls avoid re-reading and re-parsing every agent
    JSON on the event loop.
    """
    d = agents_dir or _kiro_agents_dir()
    cache_key = str(d)
    signature = _dir_signature(d)
    cached = _LIST_AGENTS_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        return _with_edition_agents(list(cached[1]))

    agents: list[AgentInfo] = []

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
                        caller="agent_discovery",
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
                # Coerced BEFORE the package-detection below, which does
                # ``stem.endswith(agent_name)``: a non-string name raised
                # TypeError there, and the broad ``except`` around this loop
                # turned that into a silently DROPPED agent rather than a
                # degraded one. Falling back to the filename stem keeps the row
                # selectable under the name its file already implies.
                agent_name = spec_str(data, "name", f.stem)
                stem = f.stem

                package = ""
                # Package-installed agents follow the "{package}-{name}.json"
                # filename convention (a generic package-manager convention, not
                # tied to any specific tool). A plain "{name}.json" is built-in.
                is_package_filename = (
                    agent_name and stem.endswith(agent_name) and stem != agent_name
                )
                if is_package_filename:
                    pkg_stem = f.stem
                    if pkg_stem.startswith("local-"):
                        pkg_stem = pkg_stem[len("local-") :]
                    package = pkg_stem[: -(len(agent_name) + 1)]

                if f.name in ("kirocrew.json", "kirocrew-lite.json"):
                    source = "kirocrew"
                elif is_package_filename:
                    source = "package"
                else:
                    source = "builtin"

                agents.append(
                    AgentInfo(
                        name=agent_name,
                        filename=f.name,
                        description=spec_str(data, "description"),
                        model=spec_model(data),
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

    # Deduplicate by name — prefer package-installed (has package) over fallback
    seen: dict[str, AgentInfo] = {}
    for a in agents:
        existing = seen.get(a.name)
        if existing is None:
            seen[a.name] = a
        elif a.package and not existing.package:
            seen[a.name] = a
        elif a.package and existing.package:
            if a.package == existing.package:
                # Package managers publish a locally-built package as BOTH
                # "{package}-{name}.json" AND "local-{package}-{name}.json";
                # stripping the "local-" prefix above makes the twin files
                # collide on the same (name, package). That is the EXPECTED
                # on-disk shape for every locally published package — warning
                # on it produced a self-contradictory "from packages 'X' and
                # 'X'" line per agent per scan (dozens per startup), drowning
                # real signals. Keep the first-seen file (unchanged first-wins
                # policy; sort order decides which twin that is) and note the
                # twin at debug.
                logger.debug(
                    "Agent '%s': keeping '%s' over same-package twin '%s'",
                    a.name,
                    existing.filename,
                    a.filename,
                )
            else:
                logger.warning(
                    "Duplicate agent name '%s' from packages '%s' and '%s'; keeping '%s'",
                    a.name,
                    existing.package,
                    a.package,
                    existing.package,
                )
    result = list(seen.values())
    _LIST_AGENTS_CACHE[cache_key] = (signature, result)
    return _with_edition_agents(result)
