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
import itertools
import logging
from pathlib import Path
from typing import Callable

from kiro_crew.agent_discovery_parts.spec_fields import AGENT_SPEC_SUFFIX, spec_str
from kiro_crew.agent_discovery_parts.spec_reads import (
    ScanUnverifiable,
    _audit_denied,
    _opened_dir_identity,
    _pinned_scan_dir_fd,
    _raise_scan_overflow,
    _read_agent_spec,
    _read_agent_spec_from_descriptor,
    _warn_on_systematic_scan_failure,
)
from kiro_crew.agent_discovery_parts.spec_snapshot import (
    _PROJECT_NAMES_CACHE,
    _entries_signature,
    _ListAgentsSig,
    _signature_dir_identity,
)
from kiro_crew.agent_spec_format import (
    NATIVE_SKILL_ALIAS_PREFIX,
    spec_stem,
    split_listed_spec_paths,
)
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
    *,
    operation: str = "project_agent_files",
    source: str = "project_agent_files",
    raise_unverifiable: bool = False,
    expected_signatures: tuple[_ListAgentsSig, ...] | None = None,
    on_live_spec: Callable[[Path, int | None], None] | None = None,
) -> list[Path]:
    """The spec scan itself, on a project scope the CALLER has already admitted.

    Split out of :func:`project_agent_files` so a caller that has already run
    :func:`_project_scope_denied` scans without deciding a second time.

    Two decisions on one scope is not a stronger guard than one. Both reduce to
    ``is_sensitive_path(str(project_dir))``, which RE-RESOLVES the path on every
    call, so the two answers can differ -- a symlink component repointed between
    them, or a fail-closed resolver stall landing on only the later one. When
    they differ the caller that admitted has already carried the scope onward,
    so a denial is recorded for a tree that is read anyway: the deny-and-read
    this module's guard exists to prevent, reintroduced by the redundant check.
    One decision, taken before any access, is the whole contract.

    PRECONDITION: *project_dir* is truthy and its ROOT has been admitted by
    :func:`_project_scope_denied` in the calling frame, before any other access
    under it. This function takes no verdict on that root and records no denial
    for it; the public :func:`project_agent_files` is the guarded entry point,
    outside agent discovery wants. Never raises: an unreadable checkout yields ``[]``.

    It does still refuse a scan SUBDIRECTORY whose resolved target is sensitive:
    ``.kiro`` and ``.kiro/agents`` are resolved through :func:`_pinned_scan_dir`,
    and one landing on a protected tree is audited under *operation*/*source*
    and contributes no specs -- so a symlinked scope is never enumerated even
    though its root was admitted. Those labels reach the audit from here for
    that reason alone; the root's own refusal is the guard's to record.

    *raise_unverifiable* selects the walk, and with it whether this function can
    raise. ``False`` (the default, and every caller reading a session's
    already-established project directory) runs the uncapped by-name
    ``is_dir()`` + ``scandir`` walk and yields ``[]`` for an unreadable checkout.
    ``True`` (a caller-supplied path, which no session has vouched for) runs the
    descriptor-pinned walk and lets :class:`ScanUnverifiable` propagate, so that
    caller answers "could not be read" rather than the confident "no agents" an
    empty list would assert.

    *expected_signatures* binds a cache-miss roster to the signature computed just
    before it: each held descriptor must carry the recorded directory identity.
    A replacement raises :class:`_ProjectSnapshotChanged` before any roster path
    is consumed, and the cache owner decides whether to refuse or degrade without
    storing an answer. Public file scans omit it and retain their existing contract.
    """
    unsupported_ok = not raise_unverifiable
    specs: list[Path] = []
    try:
        if include_legacy:
            kiro_dir = project_kiro_dir(project_dir)
            with _pinned_scan_dir_fd(kiro_dir, unsupported_ok=unsupported_ok) as (
                entries,
                dir_fd,
                overflow,
            ):
                if entries is None:
                    # Audited BEFORE the identity compare below, not after: that
                    # compare RAISES on a mid-request repoint (readable when the
                    # signature was taken, sensitive now), and the raise would
                    # carry the refusal away with no denial row -- leaving a
                    # scan that WAS refused audited as allowed. The compare still
                    # runs, so the snapshot change is still refused rather than
                    # cached against a signature that describes a different
                    # directory.
                    logger.debug("Skipping sensitive .kiro scan dir: %s", kiro_dir)
                    _audit_denied(
                        operation=operation,
                        source=source,
                        resources=str(kiro_dir),
                        error="sensitive scan dir rejected",
                    )
                if expected_signatures is not None:
                    _require_signature_dir_identity(kiro_dir, dir_fd, expected_signatures[0])
                if overflow:
                    _raise_scan_overflow(kiro_dir)
                if entries is not None:
                    # Consumed inside the `with` block and, on POSIX,
                    # descriptor-relative: a swap of the NAME after the check
                    # cannot redirect what is read (see _pinned_scan_dir).
                    specs.extend(
                        kiro_dir / e.name for e in entries if e.name.endswith(AGENT_SPEC_SUFFIX)
                    )
        agents_dir = project_agents_dir(project_dir)
        with _pinned_scan_dir_fd(agents_dir, unsupported_ok=unsupported_ok) as (
            entries,
            dir_fd,
            overflow,
        ):
            if entries is None:
                # Audited before the identity compare, for the reason given on
                # the ``.kiro`` branch above.
                logger.debug("Skipping sensitive .kiro/agents scan dir: %s", agents_dir)
                _audit_denied(
                    operation=operation,
                    source=source,
                    resources=str(agents_dir),
                    error="sensitive scan dir rejected",
                )
            if expected_signatures is not None:
                _require_signature_dir_identity(agents_dir, dir_fd, expected_signatures[1])
            if overflow:
                _raise_scan_overflow(agents_dir)
            if entries is not None:
                # Both spec forms, with a ``<stem>.md`` beside its ``<stem>.json``
                # twin dropped — the same rule the user-level scan applies through
                # ``iter_agent_spec_files``, reached here by the listed-paths entry
                # point so the pinned entries are used as-is. Re-globbing
                # ``agents_dir`` by name to get that rule would reopen the
                # check-to-use window the pin closes, and so would probing one
                # twin's existence by name — hence ``dir_fd``, which puts that
                # probe through the very descriptor the entries were read from.
                # ``dir_fd`` is ``None`` on the unpinned fallback branch, which
                # is fine: that branch is only taken when ``unsupported_ok`` is
                # True, and ``split_listed_spec_paths`` accepts ``dir_fd=None``
                # for exactly the by-name-fallback case (see its own doc).
                live, _shadowed = split_listed_spec_paths(
                    agents_dir, (agents_dir / e.name for e in entries), dir_fd=dir_fd
                )
                # The twin rule is all ``split_listed_spec_paths`` carries;
                # ``iter_agent_spec_files`` drops the projection's own
                # ``kirocrew-skill-view-*`` views on top of it, and this scan
                # needs that half too. Those stems are the MACHINE namespace the
                # native-skill projection writes (``acp/skill_projection.py``),
                # so a checkout that plants one must not have it listed as an
                # agent a person can pick: the projection reads this very roster
                # to decide what to project, and feeding its own output back in
                # makes an alias of an alias on each fire. Filtered HERE rather
                # than in the shared helper because the helper's other caller,
                # ``shadowed_markdown_specs``, consumes the unfiltered
                # ``shadowed`` half, and folding a namespace rule into a
                # twin-splitting primitive would make entries vanish from a
                # function whose name says it only partitions them.
                # NOT a security boundary and not load-bearing for one:
                # ``_require_unshadowed_templates`` walks the same directory
                # unfiltered, by design, because the native CLI can still load
                # these files by declared name -- so a spec hiding a protected
                # template behind an alias stem is still refused there.
                live = [p for p in live if not p.stem.startswith(NATIVE_SKILL_ALIAS_PREFIX)]
                # A project-name cache miss must parse each candidate while the
                # validated directory descriptor remains open. The callback is
                # optional because file-list callers preserve their path-only
                # contract; only the name resolver needs the parsed document.
                if on_live_spec is not None:
                    for spec in live:
                        on_live_spec(spec, dir_fd)
                specs.extend(live)
    except _ProjectSnapshotChanged:
        raise
    except ScanUnverifiable:
        if raise_unverifiable:
            raise
        logger.debug("Agent scan for %s could not be verified; treating as no agents", project_dir)
        return []
    except OSError:
        return []
    return sorted(specs, key=lambda f: f.stem)


def project_agent_files(
    project_dir: str | Path | None,
    include_legacy: bool = False,
    *,
    operation: str = "project_agent_files",
    source: str = "project_agent_files",
    raise_unverifiable: bool = False,
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

    Returns ``[]`` for a falsy or sensitive *project_dir*, and by default never
    raises: an unreadable checkout yields no agents rather than failing the
    caller's turn.

    *raise_unverifiable* switches BOTH what an unpinnable platform does and
    whether the result can raise, together, because the two describe one
    surface: caller-supplied vs. session-established.

    * ``False`` (every EXISTING caller -- per-turn resolution, ``spawn_run``
      validation, Slack, the config loader): none of them names a caller-supplied
      path, all of them read the session's already-established project
      directory, and this is the by-name ``is_dir()`` + ``scandir`` walk
      `upstream/main` runs on every platform, with no capability gate and no
      entry cap. The residual is the one that walk has always had: a name-based
      check-to-use window.
    * ``True`` (the dashboard roster endpoint's explicit ``?project_path=``
      scan): an HTTP query names a directory the caller does not already trust,
      so the scan takes the descriptor-pinned walk, and an unpinnable platform,
      a swapped ancestor or a listing past the entry cap REFUSES
      (:class:`ScanUnverifiable` propagates) rather than walking it by name or
      answering from a partial listing -- the endpoint answers 503 rather than
      an empty roster, so the picker does not read a scan it could not run as a
      project with none.

    The sensitive-path check is on the project root because that value arrives from
    a caller-supplied session field; the ``.kiro``/``.kiro/agents`` subdirs are
    additionally resolved and sensitivity-checked (:func:`_pinned_scan_dir`)
    so a symlinked scope is not even enumerated, and the per-file resolved-target
    check that catches a planted spec symlink stays with the reader
    (:func:`_read_agent_spec`).

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
    return _scan_project_agent_files(
        project_dir,
        include_legacy,
        operation=operation,
        source=source,
        raise_unverifiable=raise_unverifiable,
    )


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


def _project_signature(
    project_dir: str | Path, *, unsupported_ok: bool = True
) -> tuple[_ListAgentsSig, ...]:
    """Stat-only signature of a project's agent scopes.

    Covers both ``<project>/.kiro`` (legacy specs) and ``<project>/.kiro/agents``, so
    an add, removal, in-place edit, or directory replacement in either invalidates.
    The held directories' ``(st_dev, st_ino)`` identities and each entry's metadata
    are recorded, but no spec file is opened — which is what makes revalidating a
    warm cache cheap. Each subdir whose
    RESOLVED target is sensitive contributes :func:`_sensitive_dir_sig` rather than
    a real ``_dir_signature`` — never ``scandir``+``stat``'d, matching
    :func:`project_agent_files` so a symlinked ``.kiro``/``.kiro/agents`` scope is
    never probed even for cache validation. The sentinel must differ from the
    empty-dir signature ``()`` so a transition from empty-and-cached to
    sensitive is a cache MISS, not a hit that skips the denial audit — see
    :func:`_sensitive_dir_sig`.

    *unsupported_ok* forwards to :func:`_pinned_scan_dir_fd` and must agree with
    the same-named flag :func:`project_agent_files` uses for the scan this
    signature validates a cache entry for. With the default ``True`` (the
    pre-existing, ``raise_unverifiable=False`` caller shape) the signature is
    taken over the same by-name walk that scan uses, which never raises
    :class:`ScanUnverifiable`. Passed ``False`` (the dashboard's
    ``raise_unverifiable=True`` scan) the signature is taken over the pinned walk,
    and its :class:`ScanUnverifiable` propagates: a caller asking for the sharper
    signal on the scan must get it on the cache-miss signature computation as
    well, or a project that could not be verified would still get a signature
    computed and cached as if it had been.

    A sensitive subdir's sentinel is unaffected by this flag either way: that
    is a real, reached verdict (this directory resolves somewhere protected),
    never a stand-in for a scan that could not run at all.
    """
    kiro_dir = project_kiro_dir(project_dir)
    agents_dir = project_agents_dir(project_dir)
    with _pinned_scan_dir_fd(kiro_dir, unsupported_ok=unsupported_ok) as (
        kiro_entries,
        kiro_dir_fd,
        kiro_overflow,
    ):
        if kiro_overflow:
            _raise_scan_overflow(kiro_dir)
        # Computed from the entries the scan yielded, inside the `with` block
        # -- the same protection the spec scan gets, rather than a
        # signature-only stat pass reopening the window by name right after
        # the check released its pin.
        kiro_sig = (
            _sensitive_dir_sig()
            if kiro_entries is None
            else _entries_signature(kiro_entries, dir_fd=kiro_dir_fd)
        )
    with _pinned_scan_dir_fd(agents_dir, unsupported_ok=unsupported_ok) as (
        agents_entries,
        agents_dir_fd,
        agents_overflow,
    ):
        if agents_overflow:
            _raise_scan_overflow(agents_dir)
        agents_sig = (
            _sensitive_dir_sig()
            if agents_entries is None
            else _entries_signature(agents_entries, dir_fd=agents_dir_fd)
        )
    return (kiro_sig, agents_sig)


def project_agent_names(
    project_dir: str | Path | None,
    *,
    operation: str = "project_agent_names",
    source: str = "project_agent_names",
    raise_unverifiable: bool = False,
) -> frozenset[str]:
    """Dispatchable agent names declared by a project, cached on a stat signature.

    Only ``<project>/.kiro/agents/`` (``*.json`` and ``*.md``) contributes, because
    only those names are ones the backend can activate (see :func:`project_agent_files`).

    Cached per project directory and revalidated by :func:`_project_signature`, so a
    repeat call on an unchanged checkout costs a pair of ``scandir`` walks rather than
    re-reading and re-parsing every spec. Each signature records the held directories'
    identities; a cache-miss roster scan that pins a different agents directory is
    refused or returned empty without caching. This matters because the per-turn agent
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

    By default never raises: an unreadable checkout yields an empty set, from the
    SAME uncapped by-name scan `upstream/main` runs on every platform, with no
    capability gate, for this pre-existing caller shape (per-turn resolution,
    ``spawn_run`` validation, Slack, the config loader). Because that scan is
    never truncated, its answer is the whole directory, so a caller such as
    ``agent.require_fork_governance`` reading absence from it is reading a
    complete listing. *raise_unverifiable* switches to the dashboard roster
    endpoint's contract instead: the descriptor-pinned walk, where an
    unpinnable platform, a swapped ancestor or a listing past the entry cap
    REFUSES (:class:`ScanUnverifiable` propagates), because the caller supplying
    a value here is a request query rather than an established session project.
    This flag fences the cache rather than being bypassed by a warm entry:
    :func:`_project_signature` is computed with the SAME ``unsupported_ok`` and
    consulted as the cache key's validity check BEFORE the stored result is read,
    so under *raise_unverifiable* an unverifiable checkout's
    :class:`ScanUnverifiable` propagates from that signature computation ahead
    of the cache lookup and refuses a would-be hit exactly as it refuses a miss.
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
    signature = _project_signature(project_dir, unsupported_ok=not raise_unverifiable)
    cached = _PROJECT_NAMES_CACHE.get(key)
    if cached is not None:
        # A partial (oversized-name) entry is stored under *signature* PLUS a
        # synthetic ``_oversized_name_sig`` row (below) -- deliberately, so a
        # later STRICT scan's plain *signature* never matches it as if it were
        # a complete roster. Comparing only against *signature* makes that
        # partial entry permanently unmatchable instead: every subsequent
        # non-strict call recomputes the same fresh *signature*, which can
        # never carry a row this call never added, so it is always a miss and
        # the directory is rescanned every turn. The second comparison below
        # fixes that: a non-strict caller may ALSO match the partial form
        # (prefix-equal to *signature*, with the sentinel row appended), so an
        # unchanged oversized project gets its cache hit back, while
        # ``raise_unverifiable=True`` stays confined to the first comparison
        # and so still never serves a partial roster to the caller that asked
        # for the complete one.
        is_partial_match = (
            not raise_unverifiable
            and len(cached[0]) == len(signature) + 1
            and cached[0][:-1] == signature
            and len(cached[0][-1]) == 1
            and cached[0][-1][0][0] == "\0oversized-name"
        )
        if cached[0] == signature or is_partial_match:
            return cached[1]
    candidates = 0
    oversized_names = 0
    declared: list[str] = []

    def _collect_declared_name(spec: Path, dir_fd: int | None) -> None:
        nonlocal candidates, oversized_names
        # AppleDouble sidecars are rejected by design, not by failure — a
        # directory holding only sidecars is empty of specs, not broken.
        if not spec.name.startswith("._"):
            candidates += 1
        # Only a spec that parses contributes: a malformed or unreadable file can
        # never become a kiro-cli mode, and admitting its filename fallback here
        # would have dispatch accept a name whose session/set_mode then fails.
        if (name := _declared_project_agent_name_from_descriptor(spec, dir_fd)) is not None:
            if len(name) > _AGENT_NAME_MAX_CHARS:
                oversized_names += 1
                return
            declared.append(name)

    # The guard above already decided this scope, and the signature above was
    # taken on that verdict. Re-entering the public reader would decide a second
    # time: a verdict that disagreed would record a denial for a tree these
    # stats have already read, so the scan runs on the one decision instead.
    # ``raise_unverifiable`` travels with it, so a caller-supplied scope that
    # cannot be pinned still refuses here rather than reporting no agents.
    try:
        _scan_project_agent_files(
            project_dir,
            operation=operation,
            source=source,
            raise_unverifiable=raise_unverifiable,
            expected_signatures=signature,
            on_live_spec=_collect_declared_name,
        )
    except _ProjectSnapshotChanged as exc:
        if raise_unverifiable:
            raise ScanUnverifiable(str(exc)) from exc
        logger.debug("Agent scan for %s changed during cache validation", project_dir)
        return frozenset()
    if oversized_names:
        # One bounded row per scan: never copy the attacker-controlled name
        # into logs, and never emit one warning for each refused spec. Mirror
        # _bounded_scan_entries: a strict request scan refuses the whole
        # answer, but a pre-existing session scan consumes the surviving
        # names and reports the imposed narrowing instead of refusing the
        # roster outright -- the same split the entry cap already draws.
        logger.warning(
            (
                "agent directory scan for %s found %d spec(s) above the "
                "%d-character agent-name cap; refusing partial roster"
                if raise_unverifiable
                else "agent directory scan for %s found %d spec(s) above the "
                "%d-character agent-name cap; using bounded roster"
            ),
            project_agents_dir(project_dir),
            oversized_names,
            _AGENT_NAME_MAX_CHARS,
        )
        if raise_unverifiable:
            raise ScanUnverifiable(
                f"agent directory {project_agents_dir(project_dir)!s} contains a name above the "
                f"{_AGENT_NAME_MAX_CHARS}-character agent-name cap"
            )
        # cached_project_agent_names() must see the narrowing after an
        # off-loop warm, but a later verified scan must not hit this as a
        # complete roster. Appending a synthetic row makes the next real
        # signature mismatch even though the cached names are non-empty.
        partial_signature = (*signature, _oversized_name_sig(oversized_names))
        names: frozenset[str] = frozenset(declared)
        _store_project_agent_names(key, (partial_signature, names))
        return names
    _warn_on_systematic_scan_failure(project_agents_dir(project_dir), candidates, len(declared))
    names = frozenset(declared)
    _store_project_agent_names(key, (signature, names))
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


# Counter behind `_sensitive_dir_sig`, so each refusal gets a signature no cache
# entry can already hold. `itertools.count` is atomic under CPython, which is what
# this needs across the executor threads discovery runs on.
_sensitive_dir_seq = itertools.count()


# A project spec's declared name feeds kiro-cli dispatch as an agent name, so
# the retained length must match kiro_crew.validation._AGENT_NAME_RE, the
# grammar a dispatchable agent name is checked against: one leading character,
# up to 62 continuation characters, one trailing character (or a single bare
# character), for a maximum of 64.
_AGENT_NAME_MAX_CHARS = 64


# The dashboard roster endpoint (``GET /api/agents?project_path=<dir>``) keys this
# cache on a caller-supplied directory rather than an established session project,
# so the debounced folder picker mints one candidate key per path prefix typed. An
# entry above this many keys is cleared whole rather than evicted, mirroring
# _AGENTS_DIR_MEMO_MAX_KEYS below: clearing only costs a cache miss and a rescan,
# never a wrong answer, because the signature-first check above already fences
# every hit.
_PROJECT_NAMES_CACHE_MAX_KEYS = 256


# The count cap alone bounds how MANY keys stay resident, not how LARGE one is,
# and the key is the caller's raw ``str(project_dir)`` spelling. 256 keys of
# unbounded length is unbounded memory, so the length is bounded too, matching
# ``onboarding_plan._MAX_WORKSPACE_PATH_CHARS`` -- the same caller-supplied-path
# bound, at the same value, for the same reason. A spelling past it is answered
# without being retained: skipping the write costs a rescan on the next call,
# which is what every cache miss here already costs.
_PROJECT_NAMES_CACHE_MAX_KEY_CHARS = 4096


# Each retained snapshot has cache-owned caps for both variable-size payloads.
# Entries above either cap are still returned by project_agent_names(); skipping
# only the cache write keeps large valid projects correct while bounding resident
# memory. These are intentionally independent of the request-scan entry cap:
# request verification and cache residency have different contracts.
_PROJECT_NAMES_CACHE_MAX_NAMES = 512


_PROJECT_NAMES_CACHE_MAX_SIGNATURE_ROWS = 1024


class _ProjectSnapshotChanged(ScanUnverifiable):
    """A roster scan pinned a different directory than its cache signature."""


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
_ProjectSnapshotChanged.__module__ = "kiro_crew.agent_discovery"


def _require_signature_dir_identity(
    directory: Path,
    dir_fd: int | None,
    expected_signature: _ListAgentsSig,
) -> None:
    """Refuse when *dir_fd* does not identify the directory in *expected_signature*."""
    expected = _signature_dir_identity(expected_signature)
    observed = _opened_dir_identity(dir_fd)
    if observed != expected:
        raise _ProjectSnapshotChanged(
            f"agent scan directory {directory!s} changed after its cache signature was taken"
        )


def _sensitive_dir_sig() -> _ListAgentsSig:
    """A signature for a REFUSED scan dir that never equals any earlier one.

    Two distinct jobs, and a stable sentinel only does the first. It must differ
    from the empty-dir signature ``()``, so a cache warmed on a legitimately
    empty ``.kiro/agents`` cannot be served after that dir becomes a symlink into
    a credential home. It must ALSO differ from its own previous value: the
    refusal is what makes :func:`project_agent_files` emit the SEL denial, and a
    stable sentinel matches the cached signature on the very next lookup, so the
    cached result is served and every repeat attempt goes unaudited -- the first
    probe is recorded and an attacker's subsequent ones are silent.

    Counting per call makes the sensitive case permanently uncacheable, which is
    the intent: a refused directory must be re-checked and re-audited every time
    it is asked for. The ``\\0`` prefix keeps it outside the space of real
    filenames as well.
    """
    return (("\0sensitive", next(_sensitive_dir_seq)),)


def _oversized_name_sig(count: int) -> _ListAgentsSig:
    """Mark a name-capped roster so it cannot match a complete scan signature."""
    return (("\0oversized-name", count),)


def _declared_project_agent_name_from_descriptor(spec: Path, dir_fd: int | None) -> str | None:
    """Read *spec* through the descriptor that enumerated its parent directory."""
    data = _read_agent_spec_from_descriptor(
        spec,
        operation="resolve_project_agent_name",
        source="unknown",
        dir_fd=dir_fd,
    )
    if data is None:
        return None
    return spec_str(data, "name", _project_agent_fallback_name(spec))


def _store_project_agent_names(
    key: str, entry: tuple[tuple[_ListAgentsSig, ...], frozenset[str]]
) -> None:
    """Write a bounded *entry* into :data:`_PROJECT_NAMES_CACHE`.

    Both write sites in :func:`project_agent_names` (the ordinary answer and the
    oversized-name partial-roster answer) call this so the bound cannot drift
    between them. Clearing whole at the key-count cap, not LRU-evicting, mirrors
    :data:`_AGENTS_DIR_MEMO_MAX_KEYS`'s policy for the same caller-supplied-key
    hazard in this file.

    The key, names set, and nested signature rows are each bounded. An oversized
    snapshot is served to its off-loop caller but is not retained, so no cache
    policy can truncate a roster a caller treats as complete.
    """
    signature_rows = sum(len(scope_signature) for scope_signature in entry[0])
    if (
        len(key) > _PROJECT_NAMES_CACHE_MAX_KEY_CHARS
        or len(entry[1]) > _PROJECT_NAMES_CACHE_MAX_NAMES
        or signature_rows > _PROJECT_NAMES_CACHE_MAX_SIGNATURE_ROWS
    ):
        return
    if len(_PROJECT_NAMES_CACHE) >= _PROJECT_NAMES_CACHE_MAX_KEYS:
        _PROJECT_NAMES_CACHE.clear()
    _PROJECT_NAMES_CACHE[key] = entry
