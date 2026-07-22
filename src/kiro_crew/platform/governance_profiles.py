"""Phase 5 — profile store + active-scope resolution (per-surface / app / task).

Level 2 of the governance model.  A *profile* is a narrow-only ceiling bound to
a surface (``cron``/``slack``/``dashboard``/``subagent``/…), an app slug, or a
task id.  At each tool call the active profile is resolved from the session key
and agent, then intersected with the policy ceiling by ``governance.resolve``.

Kept apart from the pure-data ``governance`` module (which has no I/O) so the
filesystem read + mtime hot-reload + fallback policy live in one place — the
same split the config package uses (schema/eval vs loader).

Resolution principles (from the design + the grounding analysis):

* **Single owner per surface.** The active profile is keyed on the *session
  key* taxonomy (``sel._infer_source`` is the canonical classifier — reused, not
  re-implemented) and the agent name; never on a human-supplied value.
* **Deny-by-default on unproven identity.** A surface whose identity cannot be
  established resolves to the most-restrictive built-in profile
  (``deny_all_profile``), mirroring the dashboard ``api_session_tool_policy``
  precedent — never a permissive fall-through.
* **Invalid profile → deny-all, not the ceiling** (Validation rule 5): a
  schema-invalid profile file must not silently widen to the policy ceiling.
* **Hot-reload via mtime fingerprint** (reusing the config loader's cheap
  ``st_mtime_ns + st_size`` signature idea) so an operator edit is picked up
  without a restart, while the policy ceiling stays boot-frozen.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Dict, Optional, Tuple

from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance import (
    Bind,
    Profile,
    compose_profiles,
    deny_all_profile,
    parse_profile,
)

logger = logging.getLogger(__name__)

_PROFILES_DIR = Path.home() / ".kirocrew" / "profiles"


def audit_governance_degraded(
    chokepoint: str,
    *,
    session_key: str = "",
    scope: str = "",
    app: str = "",
    log_warning: bool = True,
    failed_closed: bool = False,
) -> None:
    """Surface a governance degradation (a chokepoint that lost its opinion).

    Every governance chokepoint catches an unexpected
    (non-``PlatformCompositionError``) error.  The pure-resolution helpers
    (``governance_permits`` / ``governance_floor_ordinal``) degrade to "no
    opinion" so a latent regression cannot wedge the surface; the HARDENED
    chokepoints (subagent spawn, Slack enterprise posture, admission load) instead
    DENY.  Either way the operator's narrowing was not applied as configured, so
    this helper makes it observable: a log line, a process-global health mark (for
    the dashboard indicator), AND a file-backed ``governance_degraded`` SEL record.

    ``failed_closed=True`` marks the DENY disposition: the log is
    emitted at ERROR (vs WARNING for a fail-open degrade) and the SEL is written
    with ``critical=True`` (synchronous), matching other security-critical audits.

    ``app`` records which per-app profile was being resolved (the messaging /
    memory-writes / channels chokepoints pass ``app=_governance_app()`` into
    ``governance_permits``); without it the SEL cannot say WHICH sandboxed app's
    narrowing was bypassed — the exact per-app threat those gates exist for.

    The SEL write goes only to the on-disk audit file (never stdout), so it is
    safe inside the stdio ``kirocrew-core`` MCP server whose stray stdout/stderr
    would corrupt the JSON-RPC stream.  Those stdio call sites pass
    ``log_warning=False`` to suppress the logger call too (its stderr is shared
    with the protocol stream) while still getting the durable SEL signal.
    Best-effort: the SEL emit is guarded so the degrade path can never raise out
    of an except-branch.
    """
    disposition = (
        "FAILED CLOSED (denied)" if failed_closed else "FAILED OPEN (degraded to no-opinion)"
    )
    if log_warning:
        emit = logger.error if failed_closed else logger.warning
        emit(
            "governance chokepoint %r %s; operator narrowing not applied for "
            "scope=%r session=%r app=%r",
            chokepoint,
            disposition,
            scope,
            session_key,
            app,
            exc_info=True,
        )
    # Process-global health signal for the dashboard indicator (best-effort).
    try:
        from kiro_crew.platform.governance_health import mark_governance_incident

        mark_governance_incident(
            "failed_closed" if failed_closed else "degraded", detail=f"{chokepoint}:{scope}"
        )
    except Exception:
        logger.debug("governance health mark unavailable", exc_info=True)
    try:
        from kiro_crew.sel import sel

        sel().log_governance_degraded(
            session_key=session_key,
            chokepoint=chokepoint,
            scope=scope,
            app=app,
            failed_closed=failed_closed,
        )
    except Exception:
        # Escalate to ERROR even when log_warning=False: if the SEL write ITSELF
        # fails (disk full, read-only FS, SEL singleton crash) and we also
        # suppressed the logger, the governance trip would be COMPLETELY invisible
        # at prod log level — defeating the observability invariant this helper
        # exists to establish.  The stdio JSON-RPC concern that motivates
        # log_warning=False is the lesser risk vs. an unrecorded governance trip,
        # and this path only fires when auditing is already broken.
        logger.error(
            "governance_degraded SEL emit FAILED for chokepoint=%r scope=%r app=%r — "
            "the governance %s is otherwise UNRECORDED",
            chokepoint,
            scope,
            app,
            "denial" if failed_closed else "fail-open",
            exc_info=True,
        )


# Surfaces that run UNATTENDED with no interactive operator in the loop.  When
# such a surface has no explicitly-bound profile AND its identity is unproven,
# it resolves to deny-all rather than the (permissive) no-profile path — these
# are the high-blast-radius surfaces the profile layer exists to contain.
_UNATTENDED_SURFACES = frozenset({"cron", "subagent", "background", "heartbeat", "taskrunner"})

# Session-key sentinel for an in-process HOST action that is not driven by any
# user-facing surface (app activation, Slack workspace admission).  Classifies to
# surface ``host`` (sel._infer_source), giving operators a stable bind target
# (``bind: {type: surface, id: host}``) for host-side governance.  Used instead
# of an empty key, which classifies to ``unknown`` and matches no profile.
HOST_SESSION_KEY = "_host"


def _profiles_dir() -> Path:
    """The profiles directory (indirection so tests can monkeypatch the module)."""
    return _PROFILES_DIR


def _infer_surface(session_key: str) -> str:
    """Classify a session key to its surface.

    Delegates to ``sel._infer_source`` — the single canonical classifier — so
    governance never grows a 4th, drifting copy of the taxonomy parser.
    """
    from kiro_crew.sel import _infer_source

    return _infer_source(session_key)


def _salvage_bind(data: object) -> Optional[Bind]:
    """Extract a VALID bind from raw profile JSON, ignoring all other errors.

    Used on the invalid-profile fallback path so a profile whose controls are
    malformed but whose ``bind`` is well-formed still maps its bound surface to
    deny-all (fail-closed), instead of being dropped from the bind index and
    failing open to the policy ceiling.  Returns None when no valid bind is
    present (then the deny-all profile is simply unbound, as before).
    """
    if not isinstance(data, dict):
        return None
    raw_bind = data.get("bind")
    if not isinstance(raw_bind, dict):
        return None
    btype = str(raw_bind.get("type", "")).strip()
    if btype not in ("surface", "app", "task"):
        return None
    return Bind(type=btype, id=str(raw_bind.get("id", "")))


def _dir_fingerprint(directory: Path) -> Tuple:
    """Cheap signature of the profiles dir — busts the cache on any edit.

    Mirrors ``config.loader._config_fingerprint``: ``st_mtime_ns + st_size`` per
    file plus the set of names, so a create / edit / truncate / delete all change
    the fingerprint.  A missing directory yields a stable sentinel.
    """
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return (("<absent>", str(directory)),)
    sig: list = []
    for p in entries:
        if p.suffix != ".json":
            continue
        try:
            st = p.stat()
            sig.append((p.name, st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((p.name, None))
    return tuple(sig)


class ProfileStore:
    """Loads + caches profiles from ``~/.kirocrew/profiles`` with mtime hot-reload.

    A schema-invalid profile is recorded as a deny-all sentinel (never the
    ceiling) so a broken file fails closed.  ``extends`` is resolved by
    ``compose_profiles`` (monotonic narrowing); a cyclic/missing parent falls
    back to deny-all.
    """

    def __init__(self) -> None:
        self._fingerprint: Optional[Tuple] = None
        self._by_name: Dict[str, Profile] = {}
        # surface/app/task index → profile name, built from each profile's bind.
        self._by_bind: Dict[Tuple[str, str], str] = {}

    def _ensure_fresh(self) -> None:
        directory = _profiles_dir()
        fp = _dir_fingerprint(directory)
        if fp == self._fingerprint:
            return
        self._reload(directory)
        self._fingerprint = fp

    def _reload(self, directory: Path) -> None:
        by_name: Dict[str, Profile] = {}
        by_bind: Dict[Tuple[str, str], str] = {}
        try:
            files = [p for p in sorted(directory.iterdir()) if p.suffix == ".json"]
        except OSError:
            files = []
        # Pass 1: parse each file independently; an invalid one becomes deny-all.
        for path in files:
            stem = path.stem
            data: object = None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise PlatformCompositionError("profile is not a JSON object")
                by_name[stem] = parse_profile(data)
            except Exception:
                logger.warning(
                    "profile %s is invalid; falling back to deny-all (fail-closed)",
                    path.name,
                    exc_info=True,
                )
                # Preserve a salvageable bind so the BOUND surface still resolves
                # to deny-all (not policy-only).  Without this, an invalid profile
                # with a valid bind would be dropped from the bind index and its
                # surface would fail OPEN to the policy ceiling — defeating
                # Validation rule 5 on the binding path.
                fallback = deny_all_profile(stem)
                salvaged = _salvage_bind(data)
                if salvaged is not None:
                    fallback = replace(fallback, bind=salvaged)
                by_name[stem] = fallback
        # Pass 2: resolve ``extends`` (monotonic narrowing) now that all are parsed.
        # The "non-trivial chain" guard must read each parent's ORIGINAL ``extends``,
        # not the live dict: ``compose_profiles`` resets a composed profile's
        # ``extends`` to "", so reading ``by_name[parent].extends`` after composing
        # makes the deny-vs-compose decision depend on the (alphabetical) order
        # files happen to be processed — a 2-deep chain ``c→b→a`` would compose
        # (fail-OPEN) when ``b`` sorts before ``c`` but deny-all when it sorts
        # after.  Snapshotting the original chain depth makes the verdict
        # deterministic and order-independent.
        _orig_extends = {name: prof.extends for name, prof in by_name.items()}
        for name, profile in list(by_name.items()):
            if profile.extends:
                parent = by_name.get(profile.extends)
                # Non-trivial chain = the parent itself extends something (judged
                # from the snapshot, so a mid-parent already composed in this pass
                # is still recognised as a chain link).
                parent_is_chain = bool(_orig_extends.get(profile.extends))
                if parent is None or parent_is_chain:  # missing or non-trivial chain
                    logger.warning(
                        "profile %r extends %r which is missing/chained; deny-all",
                        name,
                        profile.extends,
                    )
                    # Preserve the original bind so the BOUND surface still
                    # resolves to deny-all (fail-CLOSED), not None.  Without this,
                    # Pass 3 would drop the profile from ``_by_bind`` (deny-all has
                    # bind=None), ``resolve_active_scope`` would return None, and
                    # the gate would fall through to the policy ceiling ALONE —
                    # bypassing the operator's narrowing (fail-OPEN).  Mirrors the
                    # Pass-1 parse-error branch's ``_salvage_bind`` invariant.
                    fallback = deny_all_profile(name)
                    if profile.bind is not None:
                        fallback = replace(fallback, bind=profile.bind)
                    by_name[name] = fallback
                else:
                    by_name[name] = compose_profiles(parent, profile)
        # Build the bind index.  Last writer wins on a duplicate bind, logged.
        for name, profile in by_name.items():
            if profile.bind is not None:
                key = (profile.bind.type, profile.bind.id)
                if key in by_bind and by_bind[key] != name:
                    logger.warning(
                        "profiles %r and %r both bind %s; using %r",
                        by_bind[key],
                        name,
                        key,
                        name,
                    )
                by_bind[key] = name
        self._by_name = by_name
        self._by_bind = by_bind

    def get(self, name: str) -> Optional[Profile]:
        self._ensure_fresh()
        return self._by_name.get(name)

    def for_bind(self, bind: Bind) -> Optional[Profile]:
        self._ensure_fresh()
        name = self._by_bind.get((bind.type, bind.id))
        return self._by_name.get(name) if name else None

    def all_profiles(self) -> "list[Profile]":
        """Every loaded profile (for the boot-time floor assertion)."""
        self._ensure_fresh()
        return list(self._by_name.values())


# Process-global store (cheap; hot-reloads itself on access).
_STORE = ProfileStore()


def reset_store() -> None:
    """Test helper — drop the cached profiles so the next access reloads."""
    global _STORE
    _STORE = ProfileStore()


def get_store_profile(name: str) -> Optional[Profile]:
    """Return a profile by file stem (read-only; used by ``policy``/``profile`` CLI)."""
    return _STORE.get(name)


def assert_profiles_within_ceiling(ceiling: "object") -> None:
    """Boot-time floor gate: every loaded profile must be ≥ as strict as the ceiling.

    Implements Validation rules 3 & 7 and the Combined-order "app/profile ≥
    ceiling for every control? no → ABORT fail-closed" step: a profile whose
    ordinal (approval_mode / sandbox.min_level) is LOOSER than the policy mark
    raises ``PlatformCompositionError`` and aborts boot, rather than being
    silently re-tightened only at runtime.  No-op when no ceiling is present
    (standalone, ungoverned).  Called once at boot from ``bootstrap_context``.
    """
    if ceiling is None:
        return
    from kiro_crew.platform.governance import GovernanceCeiling, assert_governance_floor

    if not isinstance(ceiling, GovernanceCeiling):
        return
    for profile in _STORE.all_profiles():
        assert_governance_floor(ceiling, profile)  # raises PlatformCompositionError on weakening


def governance_permits(
    scope: str,
    item: str,
    *,
    session_key: str = "",
    agent: str = "",
    app: str = "",
    log_warning: bool = True,
    fail_closed: bool = False,
) -> "object":
    """One-call chokepoint helper: is *item* permitted in *scope* right now?

    Resolves the boot-frozen ceiling (from the active context) ∩ the active
    profile for the calling surface, and returns the ``Decision``.  This is the
    single entry point every wired chokepoint calls so they share one decision
    source and audit path — no chokepoint re-implements resolution.  Wired
    chokepoints today: the PreToolUse host gate (``tools``/``mcp``/``commands``,
    plus ``filesystem.read``/``filesystem.write``/``network.egress`` via the
    tool kind + real args — see ``governance.classify_tool_args``); cron command
    authoring (``commands``) and the cron on/off gate (``capabilities.cron``);
    sub-agent spawn (``capabilities.spawn``); outbound messaging
    (``capabilities.messaging``) and per-transport ``channels``; durable memory
    writes (``capabilities.memory_writes``); script-hook execution
    (``capabilities.script_hooks``); app activation (``apps``); and the sandbox
    ordinal floor (``sandbox.min_level`` via ``governance_floor_ordinal``).  Only
    the *live* ``approval_mode`` clamp remains reserved (the ordinal is still
    boot-floor-checked) — see ``docs/system-specs/modules/governance.md`` →
    "Still-reserved in v1".

    Fail-closed discipline matches the gate: a ``PlatformCompositionError``
    propagates; any other unexpected error returns a permissive Decision (the
    chokepoint's own always-on checks still run) rather than wedging the surface.
    That unexpected error is caught HERE (not re-raised), so a stdio-MCP caller's
    own outer ``log_warning=False`` would never run — pass ``log_warning=False``
    *into this call* from a stdio site so the degrade WARNING (whose stderr is
    shared with the JSON-RPC stream) is suppressed at the point it is emitted; the
    file-backed ``governance_degraded`` SEL is still written either way.

    ``fail_closed=True`` inverts the degrade disposition for an
    AUTHORIZATION chokepoint whose wrong-permit is an exfiltration (artifact
    publish): a governance-evaluation error then returns a DENYING
    ``Decision(False, ...)`` and audits ``failed_closed=True`` (ERROR + critical
    SEL), instead of the default permissive "no opinion".  This is required
    because the degrade is caught HERE — a caller that wraps this in its own
    ``except`` to fail closed can never see the error (it is swallowed), so the
    DENY must be produced at the point the exception is actually caught.
    """
    from kiro_crew.platform.context import (
        PlatformCompositionError,
        current_context,
    )
    from kiro_crew.platform.governance import Decision, resolve

    try:
        ceiling = getattr(current_context(), "governance", None)
        profile = resolve_active_scope(session_key, agent=agent, app=app)
        if ceiling is None and profile is None:
            return Decision(True, "ungoverned", rule="default")
        return resolve(ceiling, profile, scope, item)
    except PlatformCompositionError:
        raise
    except Exception:
        audit_governance_degraded(
            "governance_permits",
            session_key=session_key,
            scope=scope,
            log_warning=log_warning,
            failed_closed=fail_closed,
        )
        from kiro_crew.platform.governance import Decision as _D

        if fail_closed:
            # Authorization chokepoint (e.g. artifact publish): a degraded
            # evaluation must DENY, not degrade-to-permit — the blast radius of a
            # wrong permit is data exfiltration.
            return _D(False, "governance error; denied (fail-closed)", rule="default")
        return _D(True, "governance error; no opinion", rule="default")


def governance_floor_ordinal(
    scope: str,
    *,
    session_key: str = "",
    agent: str = "",
    app: str = "",
    log_warning: bool = True,
) -> Optional[str]:
    """Return the effective ordinal floor value for *scope*, or ``None``.

    Used by the sandbox / approval chokepoints to clamp a requested tier up to
    at least the governed strictness (e.g. ``sandbox.min_level``).  ``None`` means
    no governance opinion (caller keeps its own default).  ``log_warning=False``
    suppresses the degrade WARNING for a stdio-MCP caller (same rationale as
    :func:`governance_permits`); the ``governance_degraded`` SEL is still written.
    """
    from kiro_crew.platform.context import (
        PlatformCompositionError,
        current_context,
    )
    from kiro_crew.platform.governance import resolve_ordinal

    try:
        ceiling = getattr(current_context(), "governance", None)
        profile = resolve_active_scope(session_key, agent=agent, app=app)
        eff = resolve_ordinal(ceiling, profile, scope)
        return eff.value if eff is not None else None
    except PlatformCompositionError:
        raise
    except Exception:
        audit_governance_degraded(
            "governance_floor_ordinal",
            session_key=session_key,
            scope=scope,
            log_warning=log_warning,
        )
        return None


def resolve_active_scope(
    session_key: str,
    *,
    agent: str = "",
    app: str = "",
    task: str = "",
) -> Optional[Profile]:
    """Resolve the active profile for a tool call, or ``None`` for policy-only.

    Precedence of bindings (most specific first):

    1. ``app`` bind — when an app is the active context, its per-app profile
       bounds the blast radius (the design's headline per-app use case).
    2. ``task`` bind — a specific spawned task's profile.
    3. ``surface`` bind — the surface inferred from the session key.

    Returns ``None`` when no profile is bound AND the surface is attended/proven
    (policy ceiling alone governs).  Returns ``deny_all_profile()`` when an
    unattended surface has no bound profile and no proven identity — fail-closed,
    never a permissive fall-through.
    """
    if app:
        prof = _STORE.for_bind(Bind(type="app", id=app))
        if prof is not None:
            return prof
    if task:
        prof = _STORE.for_bind(Bind(type="task", id=task))
        if prof is not None:
            return prof

    # An agent name may carry its own task-scoped profile (e.g. a tightly-scoped
    # "researcher" agent), checked before the broad surface binding so a spawned
    # agent's own ceiling wins over its surface's default.
    if agent:
        prof = _STORE.for_bind(Bind(type="task", id=agent))
        if prof is not None:
            return prof

    surface = _infer_surface(session_key)
    prof = _STORE.for_bind(Bind(type="surface", id=surface))
    if prof is not None:
        return prof

    # No bound profile.  Unattended + unproven identity → deny-all (fail-closed).
    # NOTE on the empty-key case: an empty/whitespace session key is the
    # documented OPT-OUT default of governance_permits/on_tool_call ("every
    # existing caller is unaffected"), and the taxonomy classifier maps it to the
    # attended "slack" surface — so it intentionally resolves to None (policy
    # ceiling alone governs), NOT deny-all.  Making it deny-all here would break
    # the gate's ungoverned no-op contract on every standalone host.  The
    # unattended sentinels (_bg/_hb) and unattended surfaces ARE contained.
    identity_proven = bool(session_key) and session_key not in ("", "_bg", "_hb")
    if surface in _UNATTENDED_SURFACES and not identity_proven:
        return deny_all_profile(f"_deny_all:{surface}")

    # Attended/proven surface with no profile → policy ceiling alone governs.
    return None
