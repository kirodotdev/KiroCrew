"""Agents a checkout declares under its own ``.kiro/agents``.

The project-scope fence, the scan of the declared spec files, each file's declared name,
and the dispatchable name index the per-turn resolver reads, with its cached read, its
clear and its off-loop warm.

:mod:`kiro_crew.agent_discovery` stays the import path and the patch surface; see
:mod:`kiro_crew.agent_discovery_parts`.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from pathlib import Path

from kiro_crew.agent_discovery_parts.spec_fields import AGENT_SPEC_SUFFIX, spec_str
from kiro_crew.agent_discovery_parts.spec_reads import (
    _audit_denied,
    _read_agent_spec,
    _warn_on_systematic_scan_failure,
)
from kiro_crew.agent_discovery_parts.spec_snapshot import (
    _PROJECT_NAMES_CACHE,
    _dir_signature,
    _ListAgentsSig,
)
from kiro_crew.agent_spec_format import iter_agent_spec_files, spec_stem
from kiro_crew.config.paths import project_agents_dir, project_kiro_dir
from kiro_crew.executors import discovery_executor
from kiro_crew.security import is_sensitive_path

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.agent_discovery")


def _project_scope_denied(
    project_dir: str | Path,
    *,
    operation: str,
    source: str,
) -> bool:
    """``True`` when *project_dir* is a protected tree, recorded as a denial.

    The one guard every reader of a caller-supplied project scope shares, so the
    decision and the audited refusal cannot drift apart between them. Call it
    BEFORE any filesystem access under *project_dir* -- a ``scandir`` or a
    ``stat`` pair under a protected root is already a read of that tree, so a
    guard placed after one records a denial for a read that happened.

    *operation*/*source* label the SEL denial event, exactly as on
    :func:`_read_agent_spec`: the calling surface names itself so the security
    trail attributes the refusal to the request that triggered it.

    The audit is best-effort by :func:`_audit_denied` -- the caller's refusal
    stands either way, so a lost row costs the trail, never the guard (see the
    denial-audit rule in ``docs/architecture/security-deep-dive.md``).
    """
    if not is_sensitive_path(str(project_dir)):
        return False
    logger.debug("Skipping sensitive project dir for agent discovery: %s", project_dir)
    _audit_denied(
        operation=operation,
        source=source,
        resources=str(project_dir),
        error="sensitive project dir rejected",
    )
    return True


def _scan_project_agent_files(
    project_dir: str | Path,
    include_legacy: bool = False,
) -> list[Path]:
    """The spec scan itself, on a scope the CALLER has already admitted.

    Split out of :func:`project_agent_files` so a caller that has already run
    :func:`_project_scope_denied` can scan without deciding a second time.

    Two decisions on one scope is not a stronger guard than one. Both reduce to
    ``is_sensitive_path(str(project_dir))``, which RE-RESOLVES the path on every
    call, so the two answers can differ -- a symlink component repointed between
    them, or a fail-closed resolver stall landing on only the later one. When
    they differ the caller that admitted has already carried the scope onward,
    so a denial is recorded for a tree that is read anyway: the deny-and-read
    this module's guard exists to prevent, reintroduced by the redundant check.
    One decision, taken before any access, is the whole contract.

    PRECONDITION: *project_dir* is truthy and has been admitted by
    :func:`_project_scope_denied` in the calling frame, before any other access
    under it. This function does not guard and records no denial -- the public
    :func:`project_agent_files` is the guarded entry point, and is what a caller
    outside agent discovery wants. Never raises: an unreadable checkout yields ``[]``.
    """
    specs: list[Path] = []
    try:
        if include_legacy:
            kiro_dir = project_kiro_dir(project_dir)
            if kiro_dir.is_dir():
                specs.extend(kiro_dir.glob(f"*{AGENT_SPEC_SUFFIX}"))
        agents_dir = project_agents_dir(project_dir)
        if agents_dir.is_dir():
            specs.extend(iter_agent_spec_files(agents_dir))
    except OSError:
        return []
    return sorted(specs, key=lambda f: f.stem)


def project_agent_files(
    project_dir: str | Path | None,
    include_legacy: bool = False,
    *,
    operation: str = "project_agent_files",
    source: str = "project_agent_files",
) -> list[Path]:
    """Agent config files declared by a project checkout, sorted by stem.

    Returns the kiro-cli-native ``<project>/.kiro/agents/*.json`` and the markdown
    ``*.md`` form beside it — the only project location the backends resolve
    ``--agent`` against, and therefore the only one whose names are dispatchable.
    Dispatchable by the kiro-cli backend, which reads the checkout itself, and by
    the KAS projection (:func:`kiro_crew.acp.harness.kas.resolve_projected_spec`),
    which projects a project-only agent's prompt and visible tools and nothing it
    could grant, and reads the user-level spec for a name both levels declare.
    Whether a checkout's spec may be projected at all is a governance question (a
    checkout can shadow a managed agent), not a question of which form is
    scanned, and the two forms are treated alike here.

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

    *operation*/*source* label the SEL denial event emitted on a sensitive
    project directory, exactly as on :func:`_read_agent_spec` and
    :func:`project_agent_names`: the calling surface names itself so the security
    trail attributes the refusal to the request that triggered it. ``source`` is
    the interface channel (``SecurityEvent.source`` vocabulary: dashboard, cli,
    slack, cron, ...; ``"unknown"`` when the caller serves multiple channels) --
    every call site passes it explicitly, enforced by the call-site ratchet test.
    Both defaults exist ONLY so a bare call still records the refusal under a
    label that names this function; they are not for new call sites.
    """
    if not project_dir:
        return []
    # Audited like every other deny in agent discovery: the path arrives from a
    # caller-supplied session, spawn or channel field, so a scan of a protected
    # tree is a probe an operator must be able to see.
    if _project_scope_denied(project_dir, operation=operation, source=source):
        return []
    return _scan_project_agent_files(project_dir, include_legacy)


# Reports the path callers import it from, as it did before it moved: an import
# table that pins each name's defining module reads this.
project_agent_files.__module__ = "kiro_crew.agent_discovery"


def _project_agent_fallback_name(spec: Path) -> str:
    """The filename-derived name for *spec*, with the spec suffixes stripped."""
    fallback = spec.name
    if fallback.endswith(AGENT_SPEC_SUFFIX):
        return fallback[: -len(AGENT_SPEC_SUFFIX)]
    return spec_stem(fallback)


def _declared_project_agent_name(spec: Path) -> str | None:
    """The dispatchable name *spec* declares, or ``None`` when it does not parse.

    ``None`` (malformed JSON, unreadable file, sensitive symlink target) means the
    file cannot become a kiro-cli mode, so its name must not enter any dispatch
    allowlist: offering the filename of a broken spec has the session accept the
    agent and then fail at ``session/set_mode``.
    """
    data = _read_agent_spec(spec, operation="resolve_project_agent_name", source="unknown")
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


# Reports the path callers import it from, as it did before it moved: an import
# table that pins each name's defining module reads this.
project_agent_name.__module__ = "kiro_crew.agent_discovery"


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


def project_agent_names(
    project_dir: str | Path | None,
    *,
    operation: str = "project_agent_names",
    source: str = "project_agent_names",
) -> frozenset[str]:
    """Dispatchable agent names declared by a project, cached on a stat signature.

    Only ``<project>/.kiro/agents/`` (``*.json`` and ``*.md``) contributes, because
    only those names are ones the backend can activate (see :func:`project_agent_files`).

    Cached per project directory and revalidated by :func:`_project_signature`, so a
    repeat call on an unchanged checkout costs a pair of ``scandir`` walks rather than
    re-reading and re-parsing every spec. This matters because the per-turn agent
    resolver calls this on EVERY turn of a project-agent-bound session: bounding the
    file count is not enough on its own, since the cost that stalls a caller is the
    reads, not the count.

    *operation*/*source* label the SEL denial event emitted on a sensitive
    project directory, exactly as on :func:`_read_agent_spec`: the calling
    surface names itself so the security trail attributes the refusal to the
    request that triggered it. ``source`` is the interface
    channel (``SecurityEvent.source`` vocabulary: dashboard, cli, slack, cron,
    ...; ``"unknown"`` when the caller serves multiple channels) — every call
    site passes it explicitly, enforced by the call-site ratchet test. Both
    defaults exist ONLY so a bare call reproduces the established event
    byte-for-byte (a forgotten future call site degrades to exactly today's
    trail); they are not for new call sites.

    Never raises; an unreadable checkout yields an empty set.
    """
    if not project_dir:
        return frozenset()
    key = str(project_dir)
    # Sensitivity is decided BEFORE any filesystem access, signature stats
    # included: this path arrives from caller-supplied session/spawn fields, so
    # even a stat pair under ~/.aws etc. is probing a protected tree. Denied
    # loudly — the SEL record is what lets an operator see a spawn_run/cwd probe
    # at a protected path, matching every other deny in agent discovery.
    if _project_scope_denied(key, operation=operation, source=source):
        return frozenset()
    signature = _project_signature(project_dir)
    cached = _PROJECT_NAMES_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    candidates = 0
    declared: list[str] = []
    # The guard above already decided this scope, and the signature above was
    # taken on that verdict. Re-entering the public reader would decide a second
    # time: a verdict that disagreed would record a denial for a tree these
    # stats have already read, so the scan runs on the one decision instead.
    for f in _scan_project_agent_files(project_dir):
        # AppleDouble sidecars are rejected by design, not by failure — a
        # directory holding only sidecars is empty of specs, not broken.
        if not f.name.startswith("._"):
            candidates += 1
        # Only a spec that parses contributes: a malformed or unreadable file can
        # never become a kiro-cli mode, and admitting its filename fallback here
        # would have dispatch accept a name whose session/set_mode then fails.
        if (name := _declared_project_agent_name(f)) is not None:
            declared.append(name)
    _warn_on_systematic_scan_failure(project_agents_dir(project_dir), candidates, len(declared))
    names = frozenset(declared)
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


async def warm_project_agent_names(
    project_dir: str | Path | None,
    *,
    operation: str = "warm_project_agent_names",
    source: str = "unknown",
) -> None:
    """Populate the project name cache from the discovery pool, off the event loop.

    The counterpart to :func:`cached_project_agent_names`: an async caller runs this
    first so the synchronous, on-loop resolution that follows is a cache HIT rather
    than a fallback to the default agent.

    Only the scan is offloaded. Resolution itself stays inline on purpose — moving a
    synchronous resolver into an executor changes its exception semantics, and
    ``StopIteration`` in particular cannot be delivered through a ``Future``, which
    hangs the awaiting caller instead of surfacing the error.

    *operation*/*source* forward to :func:`project_agent_names` so a denial hit
    during the warm names the surface that requested it rather than echoing the
    helper's own name. The defaults name this hop truthfully: the warm
    itself is the operation, and the helper serves several channels (dashboard
    chat, spawn admission), so its channel is ``"unknown"`` unless the caller
    says otherwise.

    A no-op without a *project_dir*. Best-effort and never raises: failing to warm
    costs one turn's fallback, and must not break turn handling.
    """
    if not project_dir:
        return
    try:
        await asyncio.get_running_loop().run_in_executor(
            discovery_executor(),
            functools.partial(project_agent_names, project_dir, operation=operation, source=source),
        )
    except Exception:  # noqa: BLE001 — a warm failure only costs a fallback
        logger.debug("Failed to warm project agent names for %s", project_dir, exc_info=True)
