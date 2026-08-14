"""Agent discovery — scans ~/.kiro/agents/ for installed agents.

Provides ``list_agents()`` which returns metadata about all installed
agents, including KiroCrew's own agent and any agents shipped by
locally-installed skill packages (agent config files on disk). It only
reads on-disk agent config files and has no external-tool dependency.

Each agent is identified by its ``modeId`` — the value passed to
``session/set_mode`` in the ACP protocol to switch the backend's behavior.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.agent_files import AGENT_FILENAME, LITE_AGENT_FILENAME
from kiro_crew.config.paths import kiro_agents_dir, project_agents_dir, project_kiro_dir
from kiro_crew.executors import discovery_executor
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes
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


# Discovery scopes. A ``project`` agent comes from the session's own checkout and
# SHADOWS a ``global`` agent of the same name, mirroring kiro-cli: it resolves
# ``--agent`` against ``$PWD/.kiro/agents`` before ``~/.kiro/agents``. Kiro Crew
# spawns kiro-cli with the session's project dir as cwd, so the shadowing is a
# property of the backend rather than a policy choice here — surfacing the losing
# entry as separately selectable would advertise an agent that cannot be reached.
SCOPE_GLOBAL = "global"
SCOPE_PROJECT = "project"

# ── list_agents() result cache ──
# list_agents() reads and JSON-parses every ~/.kiro/agents/*.json on each call.
# Hot callers (agent picker, per-turn agent resolution) call it repeatedly, so an
# uncached scan over 100+ AIM-installed agent files blocks the asyncio event loop.
# Cache the parsed result keyed by directory and reuse it while a cheap stat-only
# directory signature (file count + newest mtime) is unchanged — that signature
# detects adds, removals, and in-place edits.
#
# The key carries BOTH scopes because the project scope varies per session: two
# sessions on different checkouts must not serve each other's agents from one
# entry. The signature is the pair of per-directory signatures, so an edit in
# either scope invalidates.
_ListAgentsSig = tuple[int, int]
_LIST_AGENTS_KEY = tuple[str, str]
_LIST_AGENTS_CACHE: dict[_LIST_AGENTS_KEY, tuple[tuple[_ListAgentsSig, ...], list[AgentInfo]]] = {}

# Dispatchable project agent NAMES, keyed by project dir and revalidated by the same
# stat-only signature idea. Separate from the cache above because the per-turn
# resolver needs only the name set: building full AgentInfo rows (and scanning the
# user-level dir alongside) on every turn is the cost this index exists to avoid.
_PROJECT_NAMES_CACHE: dict[str, tuple[tuple[_ListAgentsSig, ...], frozenset[str]]] = {}


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
    scope: str = SCOPE_GLOBAL  # "global" | "project"

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
            ("scope", SCOPE_GLOBAL),
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

# Kiro Crew-only spec convention, predating ``.kiro/agents/`` and still used by
# projects driven from Slack. kiro-cli does NOT read this location, so a name
# declared only here is NOT dispatchable: offering it as an agent would hand
# kiro-cli a mode it cannot activate. It is therefore excluded from discovery by
# default and included only for Slack's own listing/resolution, which predates
# this scope — see :func:`project_agent_files`.
AGENT_SPEC_SUFFIX = ".agent-spec.json"


def _read_agent_spec(path: Path) -> dict[str, Any] | None:
    """Parse an agent config file, or ``None`` when it is not usable.

    The one reader for both scopes, so every guard applies uniformly: AppleDouble
    sidecars, a symlink whose RESOLVED target is sensitive (``evil.json`` ->
    ``~/.aws/credentials``), non-UTF-8 bytes, JSON that is not an object, and
    oversized files are all rejected. The read itself goes through
    :func:`kiro_crew.hooks.safe_read_file_bytes` — the hardened gate every other
    dashboard file read uses — so a multi-gigabyte "agent config" is refused at
    the size cap instead of being slurped into memory during a cache warm. The
    agents directories are user-writable and shared with other tools, so none of
    these are hypothetical.
    """
    if path.name.startswith("._"):
        return None
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        # OSError: broken link / permission; RuntimeError: pathlib's signal for
        # a symlink LOOP (a self-referential agent symlink is one `ln -s` away
        # in a user-writable dir). Both mean "not a readable spec", and an
        # uncaught loop here crashes whichever surface asked — e.g. Slack's
        # `!agent` handler exits without replying.
        return None
    if is_sensitive_path(str(real)):
        logger.debug("Skipping sensitive agent config: %s", path)
        _sel().log_api_access(
            caller="agent_discovery",
            operation="list_agents",
            outcome="denied",
            source="list_agents",
            resources=str(real),
            error="sensitive path rejected",
        )
        return None
    try:
        raw = safe_read_file_bytes(str(real))
    except FileTooLargeError:
        logger.debug("Skipping oversized agent config: %s", path)
        return None
    if raw is None:
        logger.debug("Skipping unreadable agent config: %s", path)
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.debug("Skipping unreadable agent config: %s", path)
        return None
    if not isinstance(data, dict):
        logger.debug("Skipping non-object agent config: %s", path)
        return None
    return data


def project_agent_files(
    project_dir: str | Path | None,
    include_legacy: bool = False,
) -> list[Path]:
    """Agent config files declared by a project checkout, sorted by stem.

    Returns the kiro-cli-native ``<project>/.kiro/agents/*.json`` — the only project
    location kiro-cli itself resolves ``--agent`` against, and therefore the only
    one whose names are dispatchable.

    *include_legacy* additionally returns ``<project>/.kiro/*.agent-spec.json``, Kiro
    Crew's own older convention. It defaults to ``False`` because every dispatch
    surface (the agent picker, ``spawn_run`` validation, per-turn resolution) must
    offer only agents the backend can actually activate; a legacy-only name would be
    accepted here and then fail at ``session/set_mode``. Slack passes ``True`` to
    keep its pre-existing listing and resolution behavior.

    Returns ``[]`` for a falsy or sensitive *project_dir*, and never raises: an
    unreadable checkout yields no agents rather than failing the caller's scan.

    The sensitive-path check is on the project root because that value arrives from
    a caller-supplied session field; the per-file resolved-target check that
    catches a planted symlink stays with the reader (:func:`_read_agent_spec`).
    """
    if not project_dir:
        return []
    if is_sensitive_path(str(project_dir)):
        logger.debug("Skipping sensitive project dir for agent discovery: %s", project_dir)
        return []
    specs: list[Path] = []
    try:
        if include_legacy:
            kiro_dir = project_kiro_dir(project_dir)
            if kiro_dir.is_dir():
                specs.extend(kiro_dir.glob(f"*{AGENT_SPEC_SUFFIX}"))
        agents_dir = project_agents_dir(project_dir)
        if agents_dir.is_dir():
            specs.extend(agents_dir.glob("*.json"))
    except OSError:
        return []
    return sorted(specs, key=lambda f: f.stem)


def _project_agent_fallback_name(spec: Path) -> str:
    """The filename-derived name for *spec*, with the spec suffixes stripped."""
    fallback = spec.name
    for suffix in (AGENT_SPEC_SUFFIX, ".json"):
        if fallback.endswith(suffix):
            fallback = fallback[: -len(suffix)]
            break
    return fallback


def _declared_project_agent_name(spec: Path) -> str | None:
    """The dispatchable name *spec* declares, or ``None`` when it does not parse.

    ``None`` (malformed JSON, unreadable file, sensitive symlink target) means the
    file cannot become a kiro-cli mode, so its name must not enter any dispatch
    allowlist: offering the filename of a broken spec has the session accept the
    agent and then fail at ``session/set_mode``.
    """
    data = _read_agent_spec(spec)
    if data is None:
        return None
    return spec_str(data, "name", _project_agent_fallback_name(spec))


def project_agent_name(spec: Path) -> str:
    """The dispatchable name a project agent file declares.

    The declared ``name`` wins over the filename, matching what kiro-cli lists and
    accepts for ``--agent``. The stem is the fallback, with
    :data:`AGENT_SPEC_SUFFIX` stripped — a raw ``<name>.agent-spec`` stem is not a
    name anything downstream resolves.
    """
    return _declared_project_agent_name(spec) or _project_agent_fallback_name(spec)


def _project_signature(project_dir: str | Path) -> tuple[_ListAgentsSig, ...]:
    """Stat-only signature of a project's agent scopes.

    Covers both ``<project>/.kiro`` (legacy specs) and ``<project>/.kiro/agents``, so
    an add, removal, or in-place edit in either invalidates. Stats only — no file is
    opened — which is what makes revalidating a warm cache cheap.
    """
    return (
        _dir_signature(project_kiro_dir(project_dir)),
        _dir_signature(project_agents_dir(project_dir)),
    )


def project_agent_names(project_dir: str | Path | None) -> frozenset[str]:
    """Dispatchable agent names declared by a project, cached on a stat signature.

    Only ``<project>/.kiro/agents/*.json`` contributes, because only those names are
    ones kiro-cli can activate (see :func:`project_agent_files`).

    Cached per project directory and revalidated by :func:`_project_signature`, so a
    repeat call on an unchanged checkout costs a pair of ``scandir`` walks rather than
    re-reading and re-parsing every spec. This matters because the per-turn agent
    resolver calls this on EVERY turn of a project-agent-bound session: bounding the
    file count is not enough on its own, since the cost that stalls a caller is the
    reads, not the count.

    Never raises; an unreadable checkout yields an empty set.
    """
    if not project_dir:
        return frozenset()
    key = str(project_dir)
    # Sensitivity is decided BEFORE any filesystem access, signature stats
    # included: this path arrives from caller-supplied session/spawn fields, so
    # even a stat pair under ~/.aws etc. is probing a protected tree. Denied
    # loudly — the SEL record is what lets an operator see a spawn_run/cwd probe
    # at a protected path, matching every other deny in this module.
    if is_sensitive_path(key):
        logger.debug("Skipping sensitive project dir for agent discovery: %s", project_dir)
        _sel().log_api_access(
            caller="agent_discovery",
            operation="project_agent_names",
            outcome="denied",
            source="project_agent_names",
            resources=key,
            error="sensitive project dir rejected",
        )
        return frozenset()
    signature = _project_signature(project_dir)
    cached = _PROJECT_NAMES_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    names = frozenset(
        name
        for f in project_agent_files(project_dir)
        # Only a spec that parses contributes: a malformed or unreadable file can
        # never become a kiro-cli mode, and admitting its filename fallback here
        # would have dispatch accept a name whose session/set_mode then fails.
        if (name := _declared_project_agent_name(f)) is not None
    )
    _PROJECT_NAMES_CACHE[key] = (signature, names)
    return names


def cached_project_agent_names(project_dir: str | Path | None) -> frozenset[str] | None:
    """Cached names for *project_dir*, or ``None`` when nothing is cached yet.

    Performs **no syscalls at all** — not even the stat pair
    :func:`project_agent_names` uses to revalidate. That is the point: this is the
    read the per-turn resolver makes while an event loop is running, where any
    filesystem access is a potential gateway stall. A caller that needs a fresh
    answer warms the cache off-loop first (see :func:`project_agent_names`); this
    then serves it from memory.

    Returning the possibly-stale cached value is deliberate. The alternative on the
    loop is not "a fresher answer", it is "no answer" — and one turn resolved
    against a snapshot taken moments earlier is strictly better than a stall or a
    wrong fallback to the default agent.
    """
    if not project_dir:
        return None
    cached = _PROJECT_NAMES_CACHE.get(str(project_dir))
    return None if cached is None else cached[1]


def clear_project_agent_cache() -> None:
    """Drop all cached :func:`project_agent_names` results.

    Invalidation is normally automatic via the stat signature; call this only to
    force an immediate refresh (tests, or right after writing a project spec).
    """
    _PROJECT_NAMES_CACHE.clear()


async def warm_project_agent_names(project_dir: str | Path | None) -> None:
    """Populate the project name cache from the discovery pool, off the event loop.

    The counterpart to :func:`cached_project_agent_names`: an async caller runs this
    first so the synchronous, on-loop resolution that follows is a cache HIT rather
    than a fallback to the default agent.

    Only the scan is offloaded. Resolution itself stays inline on purpose — moving a
    synchronous resolver into an executor changes its exception semantics, and
    ``StopIteration`` in particular cannot be delivered through a ``Future``, which
    hangs the awaiting caller instead of surfacing the error.

    A no-op without a *project_dir*. Best-effort and never raises: failing to warm
    costs one turn's fallback, and must not break turn handling.
    """
    if not project_dir:
        return
    try:
        await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), project_agent_names, project_dir
        )
    except Exception:  # noqa: BLE001 — a warm failure only costs a fallback
        logger.debug("Failed to warm project agent names for %s", project_dir, exc_info=True)


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


def _global_agent_info(f: Path, data: dict[str, Any]) -> AgentInfo:
    """Build an :class:`AgentInfo` for a user-level (``~/.kiro/agents``) config."""
    # Coerced BEFORE the package-detection below, which does
    # ``stem.endswith(agent_name)``: a non-string name raised TypeError there, and
    # the broad ``except`` around the caller's loop turned that into a silently
    # DROPPED agent rather than a degraded one. Falling back to the filename stem
    # keeps the row selectable under the name its file already implies.
    agent_name = spec_str(data, "name", f.stem)
    stem = f.stem

    package = ""
    # Package-installed agents follow the "{package}-{name}.json" filename
    # convention (a generic package-manager convention, not tied to any specific
    # tool). A plain "{name}.json" is built-in.
    is_package_filename = agent_name and stem.endswith(agent_name) and stem != agent_name
    if is_package_filename:
        pkg_stem = f.stem
        if pkg_stem.startswith("local-"):
            pkg_stem = pkg_stem[len("local-") :]
        package = pkg_stem[: -(len(agent_name) + 1)]

    if f.name in (AGENT_FILENAME, LITE_AGENT_FILENAME):
        source = "kirocrew"
    elif is_package_filename:
        source = "package"
    else:
        source = "builtin"

    return AgentInfo(
        name=agent_name,
        filename=f.name,
        description=spec_str(data, "description"),
        model=spec_model(data),
        skills=_extract_skills(data),
        mcp_servers=_mcp_server_names(data),
        source=source,
        package=package,
        scope=SCOPE_GLOBAL,
    )


def _project_agent_info(f: Path, data: dict[str, Any]) -> AgentInfo:
    """Build an :class:`AgentInfo` for a project-level (``<project>/.kiro``) config.

    The ``{package}-{name}`` filename convention is deliberately NOT applied here:
    a project checkout has no package manager installing into it, so a hyphenated
    filename is just a filename and reading a package out of it would invent one.
    """
    return AgentInfo(
        name=project_agent_name(f),
        filename=f.name,
        description=spec_str(data, "description"),
        model=spec_model(data),
        skills=_extract_skills(data),
        mcp_servers=_mcp_server_names(data),
        source="builtin",
        package="",
        scope=SCOPE_PROJECT,
    )


def _mcp_server_names(data: dict[str, Any]) -> list[str]:
    """The ``mcpServers`` keys of a spec, or ``[]`` when the field is not a mapping."""
    mcp = data.get("mcpServers") or {}
    return list(mcp.keys()) if isinstance(mcp, dict) else []


def list_agents(
    agents_dir: Path | None = None,
    project_dir: str | Path | None = None,
) -> list[AgentInfo]:
    """Scan the agent directories for all installed agents.

    Returns a list of ``AgentInfo`` objects. Each agent corresponds to a kiro-cli
    agent config file that can be selected via ``session/set_mode`` in the ACP
    protocol.

    Two scopes are searched when *project_dir* is given: the user-level directory
    (``~/.kiro/agents``, or *agents_dir*) and the project's own
    (``<project>/.kiro/agents`` plus ``<project>/.kiro/*.agent-spec.json``). A
    project agent SHADOWS a user-level agent of the same name and the shadowing is
    logged, mirroring kiro-cli — which resolves ``--agent`` against its cwd first,
    and is spawned by Kiro Crew with the session's project dir as that cwd. The
    losing entry is not returned: it is unreachable for this session, so listing it
    would offer an agent that cannot run.

    Omitting *project_dir* preserves the user-level-only behavior, which is what
    callers with no session context (and therefore no project) want.

    Results are cached per scope pair and reused while both directory signatures
    are unchanged, so repeated calls avoid re-reading and re-parsing every agent
    JSON on the event loop.
    """
    d = agents_dir or _kiro_agents_dir()
    project_files = project_agent_files(project_dir)
    cache_key = (str(d), str(project_dir or ""))
    signature: tuple[_ListAgentsSig, ...] = (
        _dir_signature(d),
        _dir_signature(project_kiro_dir(project_dir)) if project_dir else (0, 0),
        _dir_signature(project_agents_dir(project_dir)) if project_dir else (0, 0),
    )
    cached = _LIST_AGENTS_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        return _with_edition_agents(list(cached[1]))

    agents: list[AgentInfo] = []

    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            try:
                data = _read_agent_spec(f)
                if data is None:
                    continue
                agents.append(_global_agent_info(f, data))
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

    # Project scope LAST so it shadows: kiro-cli resolves --agent against its cwd
    # before the user-level dir, and Kiro Crew spawns it with the session's project
    # dir as cwd, so the project entry is what would actually run. The warning
    # mirrors kiro-cli's own conflict notice — shadowing is correct here, silent
    # shadowing is not, because the two configs can differ in tools and permissions.
    for pf in project_files:
        try:
            data = _read_agent_spec(pf)
            if data is None:
                continue
            info = _project_agent_info(pf, data)
            shadowed = seen.get(info.name)
            if shadowed is not None and shadowed.scope == SCOPE_GLOBAL:
                logger.warning(
                    "Project agent '%s' (%s) shadows the user-level agent in %s",
                    info.name,
                    pf,
                    shadowed.filename,
                )
            seen[info.name] = info
        except Exception:
            logger.debug("Skipping invalid project agent config: %s", pf)
            continue

    result = list(seen.values())
    _LIST_AGENTS_CACHE[cache_key] = (signature, result)
    return _with_edition_agents(result)
