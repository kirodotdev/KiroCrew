"""The import-source registry, and the explicit adapters it dispatches to.

Every foreign agent the engine can read is an adapter module in this package --
``codex``, ``claude_code``, ``gemini``, ``openclaw``, ``hermes`` -- plus
``lineage``, the reader for an install of Kiro Crew's own layout that an
edition registers through the ``ImportSourceProvider`` CPP seam. An adapter
knows ONE layout: where its files are, which configs outrank which, and which
of its directories have no destination. It reads through
:mod:`kiro_crew.onboarding_scan` and projects through
:mod:`kiro_crew.onboarding_plan`, so every content gate applies to every
source alike.

This module is the registry over them: the builtin descriptors, the single
normalization boundary every descriptor crosses (:func:`_normalize_source`),
the per-context registry cache, root and context resolution, the
failure-isolated dispatch of one adapter (:func:`_scan_source`), and the
managed / superseded MCP name sets ``mcp_cleanup`` reads through the
``onboarding_import`` facade.
See docs/system-specs/modules/onboarding-import.md, "Registering a source".
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The module, not its names: the patch seams read from it here (``_is_link_like``)
# live only in that module, so each is read off it at call time and a patch on
# ``onboarding_import.<name>`` reaches this module too.
from kiro_crew import onboarding_scan, platform_compat
from kiro_crew.onboarding_plan import _MAX_WORKSPACE_COMPONENT_CHARS, _deduplicate_items
from kiro_crew.onboarding_scan import _expand_root, _Scan
from kiro_crew.onboarding_sources import lineage
from kiro_crew.onboarding_sources.claude_code import _scan_claude
from kiro_crew.onboarding_sources.codex import _scan_codex
from kiro_crew.onboarding_sources.gemini import _scan_gemini
from kiro_crew.onboarding_sources.hermes import _hermes_windows_root, _scan_hermes
from kiro_crew.onboarding_sources.openclaw import _openclaw_context, _openclaw_root, _scan_openclaw
from kiro_crew.platform.context import current_context, safe_context_call

# The facade's logger, not ``__name__``: operators and tests filter import
# warnings on ``kiro_crew.onboarding_import``, whichever owner emits them.
logger = logging.getLogger("kiro_crew.onboarding_import")


_CORE_MANAGED_MCP_NAMES = frozenset(
    {
        "kirocrew-core",
        "kirocrew-cron",
        "kirocrew-computer",
        "kirocrew-dashboard",
        # The three opt-in sets. They are managed names like the four above, so an
        # entry under any of them must never be carried over from a runtime the
        # user is migrating away from -- it would name a binary that cannot start.
        # The work-ledger name was missing here while this list already held every
        # always-on server; adding the crew-log one without it would have left the
        # same gap open next to a test that closes it.
        "kirocrew-work",
        "kirocrew-crew-log",
        "kirocrew-debug",
        "kirocrew-panel",
        "openclaw-core",
        "openclaw-cron",
        "openclaw-computer",
    }
)


def _home_from(home: Path | None, env: Mapping[str, str]) -> Path:
    if home is not None:
        return Path(home)
    home_keys = ("USERPROFILE", "HOME") if platform_compat.IS_WINDOWS else ("HOME", "USERPROFILE")
    for key in home_keys:
        value = env.get(key, "").strip()
        if value:
            return Path(value)
    drive = env.get("HOMEDRIVE", "")
    tail = env.get("HOMEPATH", "")
    if drive and tail:
        return Path(drive + tail)
    return Path.home()


def _source_roots(
    home: Path | None,
    env: Mapping[str, str] | None,
    *,
    sources: dict[str, _Source] | None = None,
) -> tuple[Path, dict[str, Path]]:
    """Resolve a root per source.

    *sources* lets a caller pass a registry snapshot it has already resolved. A
    caller that validates ids against one read and then resolves roots against a
    second can disagree with itself: the registry is read fail-closed, so a
    transient adapter failure between the two degrades the second to the builtins
    and leaves an accepted id with no root.
    """
    env_map = os.environ if env is None else env
    base_home = _home_from(home, env_map)
    roots: dict[str, Path] = {}
    for source_id, source in (sources if sources is not None else _sources()).items():
        if source_id == "openclaw":
            roots[source_id] = _openclaw_root(env_map, base_home)
            continue
        env_names, default_name = source.env_vars, source.home_dir
        override = next(
            (env_map.get(name, "").strip() for name in env_names if env_map.get(name, "").strip()),
            "",
        )
        if override:
            roots[source_id] = _expand_root(override, base_home)
            continue
        if source_id == "hermes":
            windows_root = _hermes_windows_root(env_map)
            if windows_root is not None:
                roots[source_id] = windows_root
                continue
        if not default_name:
            # env-only source with none of its variables set. There is no
            # directory name to fall back to, and `base_home / ""` is the user's
            # ENTIRE home — scanning that would walk every file they own. Leave it
            # unresolved; callers treat an absent root as "not installed".
            continue
        roots[source_id] = base_home / default_name
    return base_home, roots


def _source_context(
    source_id: str,
    root: Path,
    home: Path,
    env: Mapping[str, str],
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Config files and workspaces the plan must carry for a source to be rescanned.

    Only OpenClaw has any: env vars and a profile can point its config file and
    its workspaces away from its root, so they travel in the plan as
    ``_config_paths`` / ``_workspace_paths``. Every other adapter derives what it
    reads from its root and the configs it finds there.
    """
    if source_id == "openclaw":
        return _openclaw_context(root, home, env)
    return (), ()


#: Ids that are never an import source, whatever a descriptor claims. ``quick`` is
#: the first-run setup MODE — the spec requires it never appear as an import
#: option — so accepting it here would register a source the dashboard is obliged
#: to hide, i.e. one that imports nothing and cannot be diagnosed.
_RESERVED_SOURCE_IDS = frozenset({"quick"})


#: A source id must match this to be registered. The id is not just a lookup key:
#: it becomes a PATH SEGMENT under the data home (imported skills land in
#: ``skills/imported/<source_id>/``, and the pre-overwrite restore copies are
#: scoped by it), so an id carrying a separator or a parent reference would place
#: imported content outside the tree it is meant to be namespaced into.
_SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


#: Trailing version on a launcher basename (``python3.12``, ``node20``,
#: ``node-22.1``), including any separator that introduces it. Stripped before the
#: shared-runtime refusal so a versioned spelling cannot walk past it.
_RUNTIME_VERSION_SUFFIX_RE = re.compile(r"[-._]?[0-9][0-9._-]*$")


#: Launcher basenames a descriptor may NOT claim as a superseded agent's own.
#: ``mcp_cleanup`` deletes an entry from the user's global provider config when its
#: command basename matches, so claiming a shared runtime would reclaim every MCP
#: server that happens to run on it — including ones the user wrote themselves.
#: An agent's launcher is its own name, never the interpreter it starts.
#:
#: This list is **mistake-mitigation, not a security boundary.** It cannot be
#: complete — every language ships an interpreter, and a descriptor naming one
#: that is absent here is still refused only by review, not by this guard. What
#: makes that acceptable is the trust level of the input: descriptors are edition
#: code, shipped and reviewed like the rest of the core, never user- or
#: network-supplied. Treat an addition here as fixing one instance of a mistake
#: class, not as closing a hole.
_SHARED_RUNTIME_BINARIES = frozenset(
    {
        # Shells. ``busybox`` is a multi-call binary that IS the shell on many
        # minimal images, and ``env`` is how a shebang reaches an interpreter
        # (``/usr/bin/env node``), so both name someone else's runtime just as
        # surely as ``bash`` does.
        "ash",
        "bash",
        "busybox",
        "dash",
        "env",
        "fish",
        "ksh",
        "sh",
        "zsh",
        # Windows shells and script hosts.
        "cmd",
        "cscript",
        "powershell",
        "pwsh",
        "wscript",
        # Language runtimes and their package runners. ``nodejs`` is Debian's and
        # Ubuntu's name for ``node`` — version stripping cannot collapse it, since
        # the suffix is letters, so it needs its own entry or a descriptor naming
        # it slips the guard and reclaims a user's Node-based server.
        "bun",
        "bunx",
        "deno",
        "docker",
        "dotnet",
        "go",
        "java",
        "julia",
        "lua",
        "node",
        "nodejs",
        "npm",
        "npx",
        "perl",
        "php",
        "pip",
        "pipx",
        "pnpm",
        "py",
        "pyw",
        "python",
        "python3",
        "rscript",
        "ruby",
        "uv",
        "uvx",
        "yarn",
    }
)


@dataclass(frozen=True)
class _Builtin:
    """A built-in source's descriptor — engine code, so it names its own reader.

    Deliberately NOT the public ``ImportSource``: only the engine may pair a
    source with an arbitrary reader, and keeping that field off the public type is
    what stops out-of-tree code reaching the scan accumulator. Builtins still go
    through :func:`_normalize_source`, so they cannot drift onto a laxer path than
    the rule a contribution must satisfy.
    """

    id: str
    display_name: str
    scan: Callable[[Any], None]
    env_vars: tuple[str, ...] = ()
    home_dir: str = ""
    managed_mcp_names: tuple[str, ...] = ()
    superseded: bool = False
    stale_mcp_binaries: tuple[str, ...] = ()


def _core_sources() -> tuple[_Builtin, ...]:
    """The foreign agents the public edition knows how to read.

    Built on call rather than at import, so a test can monkeypatch one of the
    adapter readers imported above.
    """
    return (
        _Builtin(
            id="codex",
            display_name="Codex",
            scan=_scan_codex,
            env_vars=("CODEX_HOME",),
            home_dir=".codex",
        ),
        _Builtin(
            id="claude_code",
            display_name="Claude Code",
            scan=_scan_claude,
            env_vars=("CLAUDE_CONFIG_DIR", "CLAUDE_HOME"),
            home_dir=".claude",
        ),
        _Builtin(
            id="gemini",
            display_name="Gemini CLI / Antigravity",
            scan=_scan_gemini,
            env_vars=("GEMINI_HOME", "ANTIGRAVITY_HOME"),
            home_dir=".gemini",
        ),
        # OpenClaw's root additionally depends on a profile and a state-dir
        # override, so ``_source_roots`` resolves it bespoke and returns before the
        # generic path. The declared default below is still its real one, and
        # declaring it keeps this descriptor honest against the normalizer rather
        # than exempt from it.
        _Builtin(
            id="openclaw",
            display_name="OpenClaw",
            scan=_scan_openclaw,
            env_vars=("OPENCLAW_STATE_DIR", "OPENCLAW_HOME"),
            home_dir=".openclaw",
            managed_mcp_names=("openclaw-core", "openclaw-cron", "openclaw-computer"),
        ),
        _Builtin(
            id="hermes",
            display_name="Hermes Agent",
            scan=_scan_hermes,
            env_vars=("HERMES_HOME", "HERMES_AGENT_HOME", "HERMES_CONFIG_DIR"),
            home_dir=".hermes",
        ),
    )


@dataclass(frozen=True)
class _Source:
    """A registered import source, normalized.

    The public ``ImportSource`` descriptor is what an edition writes; this is what
    the engine reads. Everything questionable about a descriptor — id shape, a
    resolvable root, name casing, a launcher that is really a shared runtime — is
    settled ONCE in :func:`_normalize_source`, so no consumer re-derives it and no
    two consumers can disagree about the same field.
    """

    id: str
    display_name: str
    scan: Callable[[Any], None]
    env_vars: tuple[str, ...]
    #: Directory name under the user's home. Empty means this source is ONLY
    #: locatable through ``env_vars`` — it must never fall back to the home root.
    home_dir: str
    managed_mcp_names: frozenset[str]
    superseded: bool
    stale_mcp_binaries: frozenset[str]


def _runtime_stem(name: str) -> str:
    """Strip a trailing version from a launcher basename.

    ``python3.12`` and ``node20`` are the same launcher as ``python`` and ``node``
    for the purpose of refusing a shared runtime; comparing the raw string lets a
    versioned spelling walk straight past the refusal.
    """
    return _RUNTIME_VERSION_SUFFIX_RE.sub("", name.casefold())


def _name_tuple(candidate: Any, attribute: str) -> tuple[str, ...] | None:
    """Read a tuple-of-names attribute off a descriptor, or None if unreadable.

    A descriptor is out-of-tree data, so an attribute can be any object — a bare
    int is not iterable and a property can raise. Reading defensively is what
    keeps one malformed contribution from taking down discovery for every source.

    Only a concrete collection is accepted. A SCALAR is refused rather than
    iterated: a string is a sequence of characters, so the natural authoring slip
    of ``env_vars="PREDECESSOR_HOME"`` (instead of a one-tuple) would otherwise
    become sixteen single-character names, and ``stale_mcp_binaries="node"`` would
    become four names that each sail past the shared-runtime refusal. A mapping is
    refused for the same reason — iterating it yields its keys, which look like
    names but were never offered as any.

    Unreadable returns None so the caller DROPS the source, rather than an empty
    tuple: for ``managed_mcp_names`` and ``stale_mcp_binaries`` an empty set is the
    permissive answer, so silently substituting one would import an agent's own
    MCP servers precisely when the descriptor could not be trusted. Junk entries
    inside an accepted collection are filtered — that is a value the engine can
    interpret, not a descriptor it cannot read.
    """
    try:
        raw = getattr(candidate, attribute, ())
    except Exception:
        logger.warning("import source attribute %r is unreadable", attribute)
        return None
    if not isinstance(raw, (list, tuple, set, frozenset)):
        logger.warning(
            "import source attribute %r must be a list/tuple/set of names, got %s",
            attribute,
            type(raw).__name__,
        )
        return None
    return tuple(name for name in raw if isinstance(name, str) and name)


def _normalize_source(candidate: Any, *, taken: set[str], core: bool) -> _Source | None:
    """Validate and canonicalize one descriptor, or explain why it is dropped.

    The single boundary every source crosses — builtin and edition alike, so the
    builtins cannot drift onto a laxer path than the rule they model.

    *core* is the trust distinction, and it governs exactly one thing: where the
    reader comes from. A builtin brings its own (it IS engine code); for a
    contribution the engine supplies the lineage reader itself, so no
    out-of-tree code ever writes into the scan accumulator and the content gates
    inside the engine's readers cannot be bypassed.
    """
    source_id = getattr(candidate, "id", "")
    if not isinstance(source_id, str) or not source_id:
        logger.warning("ignoring import source with no id")
        return None
    if not _SOURCE_ID_RE.match(source_id):
        logger.warning(
            "ignoring import source %r: id must match %s — it becomes a path segment "
            "under the data home",
            source_id,
            _SOURCE_ID_RE.pattern,
        )
        return None
    if source_id in _RESERVED_SOURCE_IDS:
        logger.warning(
            "ignoring import source %r: that id is reserved and is never an import source",
            source_id,
        )
        return None
    if source_id in taken:
        logger.warning("ignoring import source %r: id is already registered", source_id)
        return None

    if core:
        scan = getattr(candidate, "scan", None)
        if not callable(scan):
            logger.warning("ignoring builtin import source %r: no reader", source_id)
            return None
    else:
        # A REGISTERED source is read by engine code, never by code it supplies.
        # That is the boundary keeping out-of-tree code away from the scan
        # accumulator and the ~10 gated read helpers (credential redaction,
        # injection screening, sensitive-path refusal, size caps, symlink
        # rejection) that every contributed byte must pass through. There is one
        # reader because there is one thing to read: an agent that writes THIS
        # product's own layout — a predecessor, a rename, or a fork. A second
        # reader becomes an additive, default-valued field on this same seam when
        # a second layout actually exists.
        # Read off ``lineage`` as the descriptor is normalized, never bound here:
        # that module is the reader's one storage location, so a patch on
        # ``onboarding_import._scan_lineage_install`` is the reader a registry
        # built afterwards dispatches to.
        scan = lineage._scan_lineage_install

    env_vars = _name_tuple(candidate, "env_vars")
    managed = _name_tuple(candidate, "managed_mcp_names")
    stale_raw = _name_tuple(candidate, "stale_mcp_binaries")
    if env_vars is None or managed is None or stale_raw is None:
        logger.warning(
            "ignoring import source %r: an attribute could not be read as a list of names",
            source_id,
        )
        return None
    home_dir = str(getattr(candidate, "home_dir", "") or "")
    if not env_vars and not home_dir:
        logger.warning("ignoring import source %r: no root to scan", source_id)
        return None
    if home_dir and (
        "\x00" in home_dir
        or Path(home_dir).is_absolute()
        or len(Path(home_dir).parts) != 1
        or home_dir in (".", "..")
    ):
        logger.warning(
            "ignoring import source %r: home_dir must be a single directory name under "
            "the user's home, not a path",
            source_id,
        )
        return None
    if len(home_dir) > _MAX_WORKSPACE_COMPONENT_CHARS:
        # A single component longer than the filesystem allows makes every stat
        # of this root raise ENAMETOOLONG rather than answer "absent". The
        # existence probe now survives that (see ``_stat_kind``), but a source
        # that can never resolve is a descriptor bug, so refuse it HERE where the
        # warning names the offender instead of letting it masquerade as an agent
        # the user has not installed.
        logger.warning(
            "ignoring import source %r: home_dir exceeds %d characters",
            source_id,
            _MAX_WORKSPACE_COMPONENT_CHARS,
        )
        return None

    superseded = bool(getattr(candidate, "superseded", False))
    stale = frozenset(name.casefold() for name in stale_raw)
    shared = sorted(name for name in stale if _runtime_stem(name) in _SHARED_RUNTIME_BINARIES)
    if shared:
        logger.warning(
            "ignoring import source %r: stale_mcp_binaries claims shared runtime(s) %s, "
            "which would reclaim unrelated MCP servers",
            source_id,
            ", ".join(shared),
        )
        return None

    return _Source(
        id=source_id,
        display_name=str(getattr(candidate, "display_name", "") or source_id),
        scan=scan,
        env_vars=env_vars,
        home_dir=home_dir,
        # Casefolded here so every consumer compares the same way. One consumer
        # casefolding its lookup and another not is how a contributed name
        # silently stopped matching.
        managed_mcp_names=frozenset(name.casefold() for name in managed),
        superseded=superseded,
        stale_mcp_binaries=stale if superseded else frozenset(),
    )


#: Last fully-resolved registry, paired with the context it came from. A
#: ``PlatformContext`` is built once at boot and immutable, so a complete resolve
#: stays valid for that context's lifetime. Caching it is what makes preview and
#: apply agree: without it, a provider that answered during discovery but failed
#: later left apply with no reader for a source the user had selected, and apply
#: reported success having imported nothing for it.
#:
#: Only a COMPLETE resolve is cached. Caching a degraded one would turn a transient
#: provider failure into a permanent loss of that edition's sources for the rest of
#: the process.
_SOURCES_CACHE: tuple[Any, dict[str, "_Source"]] | None = None


#: Distinguishes "the provider returned nothing" from "the read failed".
_READ_FAILED = object()


def _sources() -> dict[str, _Source]:
    """Every import source, core builtins first then edition contributions.

    Read fail-closed: a broken adapter costs the edition's sources, not the page.
    Both groups pass through :func:`_normalize_source`, so a malformed descriptor
    is dropped with a reason rather than shadowing a builtin or reaching a
    consumer in a shape it does not check.
    """
    global _SOURCES_CACHE

    ctx = safe_context_call(
        current_context,
        fallback=None,
        log_message="platform context unavailable; using builtin import sources only",
    )
    cached = _SOURCES_CACHE
    if ctx is not None and cached is not None and cached[0] is ctx:
        return cached[1]

    sources: dict[str, _Source] = {}
    for builtin in _core_sources():
        normalized = _normalize_source(builtin, taken=set(sources), core=True)
        if normalized is not None:
            sources[normalized.id] = normalized

    extra: Any = _READ_FAILED
    if ctx is not None:
        extra = safe_context_call(
            lambda: list(ctx.import_sources.import_sources()),
            fallback=_READ_FAILED,
            log_message="edition import sources lookup failed; using builtins only",
        )
    complete = extra is not _READ_FAILED
    for candidate in extra if complete else ():
        # Each contribution is isolated: a descriptor is out-of-tree data, and one
        # whose attribute access raises must cost that source alone, not discovery
        # for every source including the builtins.
        try:
            normalized = _normalize_source(candidate, taken=set(sources), core=False)
        except Exception:
            logger.warning("ignoring unreadable edition import source", exc_info=True)
            continue
        if normalized is not None:
            sources[normalized.id] = normalized

    if complete and ctx is not None:
        _SOURCES_CACHE = (ctx, sources)
    return sources


def _managed_mcp_names() -> frozenset[str]:
    """MCP server names owned by Kiro Crew or by a known foreign agent, casefolded.

    Never imported: the entry points at a runtime the user is migrating away
    from, so carrying it over hands them a server that cannot start. Callers MUST
    casefold their lookup — the set is canonical, the input is not.
    """
    contributed: set[str] = set()
    for source in _sources().values():
        contributed |= source.managed_mcp_names
    return _CORE_MANAGED_MCP_NAMES | frozenset(contributed)


def stale_mcp_binaries() -> frozenset[str]:
    """Launcher basenames whose leftover MCP entries are purgeable, casefolded.

    An edition that supersedes a predecessor registers the predecessor's launcher
    name, which is what lets ``mcp_cleanup`` reclaim entries that agent wrote into
    the user's global provider config without the core naming it. Empty unless a
    registered source declares itself ``superseded``.
    """
    names: set[str] = set()
    for source in _sources().values():
        names |= source.stale_mcp_binaries
    return frozenset(names)


def predecessor_mcp_names() -> frozenset[str]:
    """Managed server names belonging to an agent this product REPLACES, casefolded.

    Restricted to superseded agents: a live foreign agent's managed servers are
    skipped on import but must never be reclaimed from the user's global config,
    because that agent is still running them.
    """
    names: set[str] = set()
    for source in _sources().values():
        if source.superseded:
            names |= source.managed_mcp_names
    return frozenset(names)


def _scan_source(
    source_id: str,
    root: Path,
    user_home: Path,
    *,
    config_paths: tuple[Path, ...] = (),
    workspace_paths: tuple[Path, ...] = (),
    source: _Source | None = None,
) -> _Scan:
    scan = _Scan(
        source_id=source_id,
        root=root,
        user_home=user_home,
        config_paths=config_paths,
        workspace_paths=workspace_paths,
        managed_mcp_names=_managed_mcp_names,
    )
    if onboarding_scan._is_link_like(root):
        scan.diagnostic("settings", "symlink_rejected")
        return scan
    source = source if source is not None else _sources().get(source_id)
    if source is None:
        scan.diagnostic("", "unknown_source")
        return scan
    try:
        source.scan(scan)
    except Exception:
        # Discovery is a best-effort read of installs this product does not own,
        # and it reports every other unreadable input as a diagnostic rather than
        # raising. A scanner that dies must not be the one input that denies the
        # user every OTHER source too, so it is reported the same way.
        logger.warning("import scanner for %r failed", source_id, exc_info=True)
        # Whatever it managed to add before dying is a PARTIAL read of a source we
        # now know we cannot read correctly. Offering half of it as importable
        # would present that partial state as the user's data.
        for category in scan.items:
            scan.items[category].clear()
        # A source-level failure, NOT a Settings one: filing it under a category
        # told the user "Codex: Settings — scanner_failed" while the source itself
        # vanished from the picker, which misdescribes what happened and points
        # them at the wrong thing. An empty category marks it as whole-source.
        scan.diagnostic("", "source_unreadable", unsupported=True)
        return scan
    _deduplicate_items(scan)
    return scan
