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
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, Optional, Tuple

from kiro_crew.config.paths import config_dir
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance import (
    Bind,
    Profile,
    compose_profiles,
    deny_all_profile,
    parse_profile,
)


@dataclass(frozen=True)
class _Snapshot:
    """One IMMUTABLE, atomically-published view of the loaded profiles.

    ``by_name`` (stem → Profile), ``by_bind`` (surface/app/task → stem), and
    ``unrecoverable`` are published together as a SINGLE object reference so a
    lock-free reader can never observe a torn state — e.g. the new ``by_name``
    with the old ``by_bind`` after a rename, which would make ``for_bind`` miss
    the new binding and fail OPEN to policy-only. A reader grabs ``self._snap``
    once (an atomic attribute read) and reads all three from that consistent
    object; a reload swaps in a brand-new ``_Snapshot`` in one assignment.

    ``loaded`` distinguishes "we have read the profiles dir and this is the
    answer" from the INITIAL, never-loaded snapshot. Without it an empty
    never-loaded snapshot is indistinguishable from a genuine "no profiles
    configured" host, so a caller that declined to reload would authorize against
    ``profile=None`` and ``governance_permits`` would return its
    ``ungoverned`` default-permit — a fail-OPEN on a host that does have
    restrictive profiles. ``_ensure_fresh`` uses this to decide that the FIRST
    load may not be skipped.

    ``fallback_profiles`` records file stems where the deny-all fallback was
    substituted because the file could not be read or parsed. This is distinct
    from ``unrecoverable`` (which tracks bind-unrecoverable files for boot-abort).
    A profile in ``fallback_profiles`` is still governed (deny-all), so
    enforcement is correct — but the operator cannot tell a deliberate lockdown
    from a broken file without this signal.
    """

    by_name: Dict[str, Profile] = field(default_factory=dict)
    by_bind: Dict[Tuple[str, str], str] = field(default_factory=dict)
    unrecoverable: Tuple[str, ...] = ()
    fallback_profiles: "frozenset[str]" = field(default_factory=frozenset)
    loaded: bool = False


logger = logging.getLogger(__name__)

# Optional override slot for the profiles dir. Left ``None`` at import — NOT a
# module-level ``config_dir()`` capture — so importing this trust-root module
# never fires ``config_dir()`` (and thus the one-time data-home migration) as an
# import side effect; the migration must run only at the single chosen point
# (``ensure_data_home()`` in the CLI prologue). ``_profiles_dir()`` resolves the
# real path lazily; tests set this attribute to redirect it.
_PROFILES_DIR: "Path | None" = None

# Marker prefix on a fail-closed / degraded evaluation Decision's ``reason``. A
# transient governance-evaluation error yields a Decision whose ``reason`` STARTS
# with this and whose ``rule`` is ``"default"`` — distinct from a real policy deny.
# Consumers that must tell "eval error" apart from "explicitly denied" (e.g. the
# dashboard channels endpoint, which renders an eval error as "unavailable" rather
# than "Off by admin") match on THIS constant, not a hand-copied substring, so a
# reword here can't silently drift the two matchers apart.
GOVERNANCE_ERROR_REASON = "governance error"


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
    """The profiles directory, resolved lazily (honors a test-set ``_PROFILES_DIR``).

    Resolving ``config_dir()`` here rather than at module scope keeps importing
    this module free of the data-home-migration import side effect (see
    ``_PROFILES_DIR`` above). Tests that need a fixed dir set ``_PROFILES_DIR``.
    """
    if _PROFILES_DIR is not None:
        return _PROFILES_DIR
    return config_dir() / "profiles"


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

    Per file: ``st_mtime_ns + st_size + st_ctime_ns`` plus the name set, so a
    create / edit / truncate / delete all change the fingerprint. ``st_ctime_ns``
    is included specifically so a ``chmod`` that makes a previously-UNREADABLE
    profile readable busts the cache: chmod/chown/rename-in-place bump ctime but
    NOT mtime/size, so without ctime the unreadable fallback would stay cached
    forever after a permission fix and the profile's restrictions would remain
    bypassed indefinitely. A missing directory and an UNREADABLE directory yield
    DISTINCT sentinels: otherwise a reload that PRESERVED profiles on an
    enumeration error would see the identical fingerprint after the dir is later
    DELETED and never rescan — leaving stale profiles active. Distinct sentinels
    make the delete a fingerprint change that forces a fresh reload.
    """
    try:
        entries = sorted(directory.iterdir())
    except FileNotFoundError:
        return (("<absent>", str(directory)),)
    except OSError:
        # Includes NotADirectoryError (a non-dir at the path) + EACCES/EIO — all
        # treated as "present but unreadable", distinct from the absent sentinel,
        # so the reload's fail-closed/preserve handling (not the empty-absent path)
        # runs and a later fix/delete busts the cache.
        return (("<unreadable>", str(directory)),)
    sig: list = []
    for p in entries:
        if p.suffix != ".json":
            continue
        try:
            st = p.stat()
            # ctime included so a chmod (perms fix on an unreadable file) — which
            # changes ctime but not mtime/size — still busts the cache and forces
            # a re-read, so a fixed-permissions profile recovers its restrictions.
            sig.append((p.name, st.st_mtime_ns, st.st_size, st.st_ctime_ns))
        except OSError:
            sig.append((p.name, None))
    return tuple(sig)


def _fallback_profile(name: str) -> Profile:
    """The profile substituted for a stem whose FILE is unusable.

    Defaults to the most-restrictive :func:`deny_all_profile` (Validation rule 5):
    an unreadable / unparseable / broken-``extends`` profile denies its surface
    rather than silently widening to the ceiling.

    An enterprise ceiling MAY declare a looser fallback via the policy's top-level
    ``fallback`` key (``GovernanceCeiling.fallback_profile``). When present it is
    used instead — e.g. deny only the ``channels`` and ``apps`` planes while
    leaving the basic operational surfaces (subagent/cron/heartbeat/taskrunner,
    tools, commands, filesystem, network) to the ceiling. That trades strict
    fail-closed for keeping the background planes running when a profile file
    cannot be loaded; the declared fallback is still intersected with the ceiling,
    so it can only narrow it. Absent the declaration the public default is
    unchanged (deny-all).

    Read best-effort through ``current_context()``: if the context or ceiling is
    not composed yet (early boot, tests), fall back to deny-all — the safe
    direction. The lookup mirrors the runtime-unrecoverable escalation below,
    which reads ``current_context().governance`` the same way.
    """
    try:
        from kiro_crew.platform.context import current_context

        ceiling = getattr(current_context(), "governance", None)
        declared = getattr(ceiling, "fallback_profile", None) if ceiling is not None else None
        if declared is not None:
            return replace(declared, name=name)
    except Exception:
        logger.debug("fallback profile lookup unavailable; using deny-all", exc_info=True)
    return deny_all_profile(name)


def _fallback_token() -> bool:
    """Whether a declared fallback profile is currently available on the ceiling.

    Folded into the profile-store freshness key (:meth:`ProfileStore._ensure_fresh`)
    so a snapshot baked BEFORE governance composed — when :func:`_fallback_profile`
    could only return deny-all — is reloaded ONCE the declared fallback becomes
    available. Without this, a store first-touched on the pre-governance boot path
    would bake deny-all for an unusable profile and keep serving it until a file
    mtime changed, silently ignoring the operator's declared fallback (a boot-order
    race that reproduces the very background-plane outage the fallback prevents).
    The ceiling is boot-frozen, so this transitions False→True at most once and
    never thrashes; when no fallback is declared it stays False and deny-all (the
    correct answer) needs no reload.
    """
    try:
        from kiro_crew.platform.context import current_context

        ceiling = getattr(current_context(), "governance", None)
        return ceiling is not None and getattr(ceiling, "fallback_profile", None) is not None
    except Exception:
        return False


class ProfileStore:
    """Loads + caches profiles from ``~/.kiro/crew/profiles`` with mtime hot-reload.

    A schema-invalid profile is recorded as a deny-all sentinel (never the
    ceiling) so a broken file fails closed.  ``extends`` is resolved by
    ``compose_profiles`` (monotonic narrowing); a cyclic/missing parent falls
    back to deny-all.
    """

    def __init__(self) -> None:
        # Gives the reload transaction (re-stat → reload → publish → commit) a
        # single owner. The inbound channels gate resolves profiles OFF the event
        # loop — all five transport dispatchers can call into the store from mc-gov
        # worker threads concurrently — so without this several threads would each
        # run the full iterdir + read_text walk and publish competing snapshots for
        # the same directory state. The lock makes one thread do that work while the
        # others either wait (unprimed store) or serve the current snapshot (warm);
        # the fast path (loaded + fingerprint unchanged) never contends.
        self._lock = threading.Lock()
        self._fingerprint: Optional[Tuple] = None
        # The current profiles view, published ATOMICALLY as one immutable object
        # (see ``_Snapshot``). A reader grabs this reference once; a reload swaps a
        # fresh ``_Snapshot`` in a single assignment, so name/bind/unrecoverable are
        # never observed torn. ``unrecoverable`` records files that were PRESENT but
        # whose bind could not be recovered — a GOVERNED fleet turns a non-empty set
        # into a boot-abort via ``assert_profiles_within_ceiling``; a standalone host
        # tolerates it (lenient).
        self._snap: _Snapshot = _Snapshot()

    def _ensure_fresh(self) -> bool:
        """Reload the profiles if the directory fingerprint changed.

        Returns whether the snapshot is RESOLVED — i.e. safe to authorize against.
        ``False`` means "this store has never loaded and another thread is loading
        it right now", so the caller must fail CLOSED rather than treat the empty
        snapshot as "no profiles configured" (see ``_Snapshot.loaded``). Callers
        that only READ profiles (the CLI, the boot floor) can ignore the result;
        the authorization path (:func:`resolve_active_scope`) must not.

        This NEVER blocks. The path is reachable on the event loop (the
        synchronous PreToolUse gate), where waiting on another thread's filesystem
        I/O would wedge the gateway — a slow first profile load in a worker plus a
        concurrent dashboard tool approval is exactly that stall. So a caller that
        can't take the reload lock does not wait:

        * **Warm** (already loaded): serve the current immutable snapshot and
          return ``True``. Safe because ``_snap`` is only ever REPLACED wholesale
          (an atomic ref swap), so a concurrent reader sees a coherent
          prior-or-next snapshot; since a prior snapshot exists the worst case is
          authorizing against the last committed state for ONE call, and the next
          access self-heals.
        * **Unprimed**: return ``False`` — there is no safe snapshot to serve, so
          the decision fails closed instead of failing open.

        mtime hot-reload still works exactly as documented: an operator edit is
        picked up without a restart. What this deliberately does NOT have is a
        per-thread "always block" discipline for off-loop callers. Its only benefit
        on a warm store is closing a staleness window one call wide while a reload
        is in flight, which is not worth a thread-local plus a dual code path — and
        it invites someone to later reach for the blocking path from the loop,
        reintroducing the wedge.

        There is likewise NO same-metadata retry: an unreadable/malformed profile
        fails CLOSED (its surface is a bind-preserving deny-all, see ``_reload``),
        so there is nothing STALE being served that a retry would need to refresh.
        """
        directory = _profiles_dir()
        fp: Optional[Tuple] = None
        if self._snap.loaded:
            fp = (_dir_fingerprint(directory), _fallback_token())
            if fp == self._fingerprint:
                return True
        # NEVER block: this is reachable on the event loop (the synchronous PreToolUse
        # gate), and waiting on another thread's filesystem I/O there would wedge the
        # gateway — a first profile load in a worker on a slow FS, plus a concurrent
        # dashboard tool approval, is exactly that stall.
        if not self._lock.acquire(blocking=False):
            # Another thread owns this reload. If we are WARM we serve the current
            # immutable snapshot (worst case: the last committed state for one call).
            # If we are UNPRIMED there is nothing safe to serve — an empty
            # never-loaded snapshot is indistinguishable from a genuine "no profiles
            # configured" host, so the caller would resolve ``profile=None`` and
            # ``governance_permits`` would hand back its ``ungoverned`` default-PERMIT:
            # a fail-OPEN that ``fail_closed=True`` cannot catch (the default-permit is
            # a normal return, not an exception). So report UNRESOLVED and let the
            # caller fail closed. Concurrent first-touch is the EXPECTED case, not an
            # exotic one: nothing primes the store on the ungoverned / profile-only
            # boot path, so a startup burst across the transports puts several threads
            # here at once.
            return self._snap.loaded
        try:
            # Re-stat under the lock only when we did not already do it above (an
            # unprimed caller has no pre-lock fingerprint). A warm caller reuses its
            # pre-lock value: it acquired without waiting, so that value is still
            # current, and re-statting would run a SECOND iterdir+stat walk on the
            # event loop (a slow-FS stall) for no freshness gain. Either way the value
            # used for the freshness test is the one committed below, so the committed
            # fingerprint always describes the snapshot actually published.
            if fp is None:
                fp = (_dir_fingerprint(directory), _fallback_token())
            if self._snap.loaded and fp == self._fingerprint:
                return True  # already fresh (another thread reloaded, or unchanged)
            self._reload(directory)
            # Commit the fingerprint. An unreadable/malformed file is a
            # bind-preserving deny-all (fail-closed), so the cached state is the SAFE
            # (denying) state; a metadata change (fix/delete/chmod — all bump the
            # ctime-inclusive fingerprint) busts the cache and reloads normally.
            self._fingerprint = fp
        finally:
            self._lock.release()
        return True

    def _reload(self, directory: Path) -> None:
        # Snapshot the last-known-good index BEFORE mutating, so a per-file blip can
        # recover the prior BIND (to keep the surface bound to a fail-closed deny-all
        # rather than dropping it to policy-only). NOTE: a present-but-unreadable or
        # malformed file does NOT carry its prior PERMISSIONS forward — it fails
        # CLOSED (bind-preserving deny-all) so a tightened-then-unreadable profile
        # can never leave newly-denied operations authorized.
        prior_by_name = self._snap.by_name
        by_name: Dict[str, Profile] = {}
        by_bind: Dict[Tuple[str, str], str] = {}
        unrecoverable: list[str] = []
        fallback_stems: set[str] = set()
        try:
            files = [p for p in sorted(directory.iterdir()) if p.suffix == ".json"]
        except FileNotFoundError:
            # The directory does not exist — the NORMAL "no profiles configured"
            # case (a fresh KIROCREW_HOME has no profiles/ dir). This is ABSENT,
            # not an enumeration failure: publish an EMPTY index (no profiles bind
            # anything → every surface is policy-only), exactly as before, and do
            # NOT warn. Falls through to the normal publish below with no files.
            # (NotADirectoryError is deliberately NOT caught here — a non-directory
            # at the profiles path is a MISCONFIG where Level-2 restrictions would
            # silently vanish, so it must fail closed via the OSError branch below,
            # NOT be treated as benign absence.)
            files = []
        except OSError:
            # The directory EXISTS but could not be enumerated: EACCES/EIO, OR a
            # NON-directory at the profiles path (NotADirectoryError, a subclass of
            # OSError — a misconfig where honouring "empty" would silently drop all
            # Level-2 narrowing). A genuinely MISSING dir was already handled as
            # absent above, so this never fires on the common no-profiles host.
            # Two cases:
            #   * WARM (we hold a prior index): preserve it untouched and return —
            #     clearing here would drop every active profile to policy-only
            #     (fail-OPEN). The prior IS the safe state, so we do NOT mark it
            #     unrecoverable (no boot-abort for a transient blip on a running,
            #     already-validated host). The ``<absent>`` fingerprint sentinel
            #     differs from the real one, so a later successful re-enumeration
            #     reloads.
            #   * COLD (no prior — e.g. an unreadable dir at BOOT): there is nothing
            #     to preserve AND we cannot prove the fleet is within its ceiling.
            #     Record a sentinel in ``unrecoverable`` so a GOVERNED fleet
            #     boot-aborts via ``assert_profiles_within_ceiling`` rather than
            #     starting with zero (fail-open) profiles; a standalone host
            #     tolerates it (empty index → policy-only, no crash).
            logger.warning(
                "profiles dir %s could not be enumerated; %s",
                directory,
                (
                    "preserving last-known-good profiles"
                    if prior_by_name
                    else "no prior snapshot — a governed fleet fails closed at boot"
                ),
                exc_info=True,
            )
            if prior_by_name:
                # WARM: keep the existing snapshot untouched (do not clear the
                # unrecoverable flag on the live snapshot — it already reflects a
                # clean prior load). Nothing to publish.
                return
            # COLD: publish an empty index but flag it (one atomic snapshot) so a
            # governed boot aborts rather than run with zero profiles.
            self._snap = _Snapshot(
                by_name={},
                by_bind={},
                unrecoverable=(f"<dir:{directory}>",),
                loaded=True,
            )
            return
        # Pass 1: parse each file independently; an invalid one becomes deny-all.
        for path in files:
            stem = path.stem
            data: object = None
            # Read the bytes FIRST, separately from parsing, so a present-but-
            # UNREADABLE file is distinguished from a genuine parse error. The two
            # fail closed the same way but recover their BIND differently: a parse
            # error has parsed content, so ``_salvage_bind`` can often read the real
            # bind out of it; an unreadable file has none, so it can only fall back
            # to a prior entry's bind. See the two except-branches below.
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                # Vanished between iterdir() and read (TOCTOU) — the file is now
                # ABSENT, not a present policy. A missing file is not a deny, so
                # skip it (don't manufacture a fail-closed for a surface that has
                # no profile at all).
                continue
            except (OSError, UnicodeError):
                # PRESENT but UNREADABLE — an IO/permission error (OSError) or an
                # invalid encoding (UnicodeDecodeError, whose base is UnicodeError):
                # either way the content, and thus the intended bind, cannot be read.
                # Catching UnicodeError alongside OSError is REQUIRED — it is not an
                # OSError, so without it a corrupt-encoding profile escapes both
                # handlers and crashes boot inside assert_profiles_within_ceiling.
                #
                # Fail CLOSED. The prior PERMISSIONS are NOT carried forward: a
                # profile that was just tightened and then became unreadable would
                # otherwise keep its newly-DENIED operations authorized (a real
                # fail-open, especially for a composed child whose parent changed).
                # The surface resolves to a deny-all instead. Two sub-cases:
                #   * PRIOR entry exists → reuse only its BIND, so the surface stays
                #     BOUND to that deny-all rather than dropping to policy-only
                #     (which would fail OPEN). A metadata change (fix / delete /
                #     chmod — all bump the ctime-inclusive fingerprint) transitions
                #     it out.
                #   * NO prior (first-ever load of an unreadable file) → the bind
                #     cannot be recovered, so it is an UNBOUND deny-all recorded as
                #     unrecoverable. We do NOT guess a bind from the filename: the
                #     stem does not reliably encode it (an unreadable file that binds
                #     app:file-explorer is NOT surface:<stem>), so a guess would
                #     mis-bind and STILL fail open on the real lookup. A governed
                #     fleet boot-aborts via assert_profiles_within_ceiling; a
                #     standalone host tolerates it (lenient, no crash).
                prior = prior_by_name.get(stem)
                fallback = _fallback_profile(stem)
                if prior is not None and prior.bind is not None:
                    logger.warning(
                        "profile %s is present but unreadable; denying its surface "
                        "(bind-preserving deny-all) until the file is fixed.",
                        path.name,
                        exc_info=True,
                    )
                    by_name[stem] = replace(fallback, bind=prior.bind)
                else:
                    logger.warning(
                        "profile %s is present but unreadable (no recoverable bind); "
                        "deny-all fallback (unbound). A governed fleet fails closed "
                        "at boot.",
                        path.name,
                        exc_info=True,
                    )
                    by_name[stem] = fallback
                    unrecoverable.append(path.name)
                fallback_stems.add(stem)
                continue
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise PlatformCompositionError("profile is not a JSON object")
                by_name[stem] = parse_profile(data)
            except Exception:
                logger.warning(
                    "profile %s is invalid; falling back to deny-all (fail-closed)",
                    path.name,
                    exc_info=True,
                )
                # Fail CLOSED (deny-all), preserving a salvageable bind so the BOUND
                # surface still resolves to that deny-all rather than dropping to
                # policy-only — which would fail OPEN to the ceiling and defeat
                # Validation rule 5 on the binding path. The prior PERMISSIONS are
                # NOT preserved: a just-tightened-then-malformed profile must not keep
                # its newly-denied operations authorized. Bind recovery order: the
                # parsed dict first (a parse error means the JSON parsed, or partially
                # did, so ``_salvage_bind`` can often read the real bind out of it),
                # else the prior entry's bind. Only when NEITHER yields a bind is it an
                # UNBOUND deny-all recorded as unrecoverable (governed fleet
                # boot-aborts; standalone tolerates).
                fallback = _fallback_profile(stem)
                salvaged = _salvage_bind(data)
                if salvaged is None:
                    prior = prior_by_name.get(stem)
                    salvaged = prior.bind if prior is not None else None
                if salvaged is not None:
                    by_name[stem] = replace(fallback, bind=salvaged)
                else:
                    by_name[stem] = fallback
                    unrecoverable.append(path.name)
                fallback_stems.add(stem)
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
                    fallback = _fallback_profile(name)
                    if profile.bind is not None:
                        fallback = replace(fallback, bind=profile.bind)
                    by_name[name] = fallback
                    # Third substitution site, and the least obvious: the file
                    # itself parsed fine, so only the broken ``extends`` chain
                    # makes it deny-all. Recording it here is what stops a deleted
                    # or mis-chained PARENT from rendering as a deliberate
                    # lockdown, which is the same symptom as an unparseable file.
                    fallback_stems.add(name)
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
        unrec = tuple(unrecoverable)
        # Publish the freshly-built index as ONE immutable snapshot (single atomic
        # assignment) so a lock-free reader never sees new names with old bindings.
        # Preservation is PER-PROFILE, handled inline in Pass 1: an unreadable file
        # whose profile was parsed in a prior reload carried its last-known-good
        # entry into ``by_name`` above, so valid updates to OTHER profiles in this
        # same reload are still published (no whole-store rollback that would
        # discard them). A never-before-seen unreadable file has no prior entry, so
        # it stays an UNBOUND deny-all and is recorded in ``unrecoverable`` — a
        # governed fleet boot-aborts via ``assert_profiles_within_ceiling``; a
        # standalone host tolerates it. A directory that could not be enumerated
        # already returned early above, leaving the prior snapshot fully intact.
        self._snap = _Snapshot(
            by_name=by_name,
            by_bind=by_bind,
            unrecoverable=unrec,
            fallback_profiles=frozenset(fallback_stems),
            loaded=True,
        )
        # Runtime observability for a POST-BOOT unrecoverable file. The boot floor
        # (``assert_profiles_within_ceiling``) only runs once; a governed RUNNING
        # host that hot-loads a NEW unreadable profile (no prior entry to preserve)
        # gets an unbound deny-all that never matches its intended surface, so that
        # surface silently falls to policy-only until the file is fixed. We do NOT
        # lock the whole fleet down on one bad file (that would let a single stray
        # unreadable profile DoS every surface); instead we make it LOUD +
        # observable so an operator/monitor catches the fail-open window: an ERROR
        # log and a governance-health incident (the dashboard indicator). Only when
        # a ceiling is actually present (governed) — an ungoverned standalone host
        # has no narrowing to lose.
        if unrec:
            try:
                from kiro_crew.platform.context import current_context

                if getattr(current_context(), "governance", None) is not None:
                    logger.error(
                        "governed host has unrecoverable profile(s) %s at runtime; their "
                        "bound surfaces fall to policy-only until fixed (boot floor does "
                        "not re-run) — resolve the file(s) to restore narrowing",
                        unrec,
                    )
                    try:
                        from kiro_crew.platform.governance_health import (
                            mark_governance_incident,
                        )

                        mark_governance_incident(
                            "unrecoverable_profile",
                            detail=",".join(unrec),
                        )
                    except Exception:
                        logger.debug("governance health mark unavailable", exc_info=True)
            except Exception:
                # current_context() unavailable (pre-boot / tests) — the boot floor
                # covers boot; nothing to escalate here.
                logger.debug("runtime unrecoverable escalation skipped", exc_info=True)

    def unrecoverable_files(self) -> Tuple[str, ...]:
        """File names that were PRESENT but whose bind could not be recovered.

        Populated by the last reload (unreadable content / invalid encoding, or a
        parse error with no salvageable bind). A governed fleet turns a non-empty
        result into a boot-abort via :func:`assert_profiles_within_ceiling`; a
        standalone host tolerates it (lenient). Reads through ``_ensure_fresh`` so
        the value reflects the current on-disk state.
        """
        self._ensure_fresh()
        return self._snap.unrecoverable

    def get(self, name: str) -> Optional[Profile]:
        self._ensure_fresh()
        return self._snap.by_name.get(name)

    def for_bind(self, bind: Bind) -> Optional[Profile]:
        self._ensure_fresh()
        # Grab the snapshot ONCE so the two lookups read a consistent view — a
        # concurrent reload swaps in a whole new _Snapshot, never mutates this one.
        snap = self._snap
        name = snap.by_bind.get((bind.type, bind.id))
        return snap.by_name.get(name) if name else None

    def resolved(self) -> bool:
        """True when the snapshot is safe to AUTHORIZE against.

        False only while this store has never loaded and another thread is doing
        that first load: the empty never-loaded snapshot would otherwise read as
        "no profiles configured" and fail OPEN. Refreshes first, so a caller that
        wins the race simply gets ``True``.
        """
        return self._ensure_fresh()

    def snapshot(self) -> _Snapshot:
        """The current immutable snapshot, WITHOUT refreshing.

        For a caller that already called :meth:`resolved` and wants every lookup in
        one resolution to read ONE consistent state — a concurrent reload swaps in a
        whole new ``_Snapshot``, so re-reading per lookup could mix bindings from two
        different states (e.g. an app bind from before a reload and a surface bind
        from after). Pin it once, read it many times.
        """
        return self._snap

    def all_profiles(self) -> "list[Profile]":
        """Every loaded profile (for the boot-time floor assertion)."""
        self._ensure_fresh()
        return list(self._snap.by_name.values())


# Process-global store (cheap; hot-reloads itself on access).
_STORE = ProfileStore()


def bound_surfaces() -> Tuple[str, ...]:
    """Surface ids that have a profile bound to them, sorted.

    Names only — no control, count, or rule contents — so a read-only display
    surface can say WHICH surfaces are separately governed without widening what
    it may expose. The Security page needs exactly this: a host-surface row that
    pins ``capabilities.cron`` off is the host's posture, and knowing that ``cron``
    carries its own profile is what stops that row reading as install-wide.

    Returns ``()`` when the store cannot be trusted yet (never-loaded, mid
    first-load), matching the fail-quiet contract a display caller needs: an
    empty list renders as "no other bound surfaces", never as a wrong name.
    """
    if not _STORE.resolved():
        return ()
    snap = _STORE.snapshot()
    return tuple(
        sorted({bind_id for (bind_type, bind_id) in snap.by_bind if bind_type == "surface"})
    )


def fallback_profile_names() -> "frozenset[str]":
    """Profile stems currently substituted by the deny-all fallback.

    A non-empty result means at least one profile is present on disk but could not
    be USED by this build — it was unreadable, it failed to parse, or its
    ``extends`` parent is missing or chained. Its surface is correctly denied
    (enforcement is intact), but the operator cannot tell a deliberate lockdown
    from a broken file. The Security page uses this to render a warning banner.

    All three substitution sites in ``_reload`` record here, so "substituted" means
    substituted for any reason rather than "failed to parse" specifically — the
    caller must not narrate a cause it has not established.

    Returns ``frozenset()`` when the store cannot be trusted yet (never-loaded,
    mid first-load), matching the fail-quiet contract a display caller needs.
    """
    if not _STORE.resolved():
        return frozenset()
    return _STORE.snapshot().fallback_profiles


def any_configured_profile_governs(ref: str) -> bool:
    """True if ANY loaded profile has an opinion on *ref*.

    The static ``allowedTools`` writers decide auto-approve at config-BUILD time,
    where there is no surface/session to resolve the ONE profile a tool call
    would bind. A profile-only deny (a per-app/per-surface profile denying
    ``@srv/delete`` on a host with no POLICY ceiling) would then be bypassed by a
    blanket auto-approve, because ``may_skip_gate`` alone only consults the
    ceiling. So the writer must withhold if ANY configured profile COULD govern
    the ref; the runtime gate applies the specific one.

    Reuses the one predicate: ``Profile`` and ``GovernanceCeiling`` both expose
    ``.get(scope)``, so ``may_skip_gate(ref, profile)`` answers "does THIS profile
    govern the ref" without duplicating the ref-parsing. Fail-closed: an
    unresolved store (never-loaded, mid first-load) answers True — "cannot confirm
    none governs" must not read as "none governs".
    """
    from kiro_crew.platform.governance import may_skip_gate

    if not _STORE.resolved():
        return True
    # may_skip_gate is typed for a ceiling, but it only reads `.get(scope)`, which
    # Profile and GovernanceCeiling share — passing a profile answers "does THIS
    # profile govern the ref" (see resolve, which is scope-map-agnostic).
    return any(
        not may_skip_gate(ref, profile)  # type: ignore[arg-type]
        for profile in _STORE.all_profiles()
    )


def reset_store() -> None:
    """Test helper — drop the cached profiles so the next access reloads."""
    global _STORE
    _STORE = ProfileStore()


def get_store_profile(name: str) -> Optional[Profile]:
    """Return a profile by file stem (read-only; used by ``policy``/``profile`` CLI)."""
    return _STORE.get(name)


def all_profile_pinned_commands() -> "tuple[str, ...]":
    """Union of ``commands``-scope deny patterns across ALL loaded profiles.

    The Settings > Security snapshot is surface-agnostic (it has no session /
    agent / app to resolve a single *active* profile), so it must treat a rule
    as governance-locked if ANY profile pins it — otherwise a profile-pinned
    rule would render as freely disableable, the user "disables" it, and the
    per-session gate (which DOES resolve the bound profile) still denies the
    command: a confusing no-op opt-out. Conservative by design (over-locks
    rather than under-locks). Empty on an ungoverned standalone host.
    """
    pins: list[str] = []
    for profile in _STORE.all_profiles():
        pins.extend(profile.pinned_command_patterns())
    # Order-preserving de-dup.
    return tuple(dict.fromkeys(pins))


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
    # Governed fleet + a PRESENT profile whose bind could not be recovered
    # (unreadable content / invalid encoding / unsalvageable parse error) →
    # fail closed to a boot-abort. Such a file may carry a restrictive bind that
    # was silently dropped from the bind index; a governed fleet must refuse to
    # run rather than run with the operator's narrowing missing. (No-op for a
    # standalone host — ``ceiling is None`` returned above — so a profile blip on
    # an ungoverned install never crashes boot.) There is no transient-vs-permanent
    # distinction to make here: this gate runs ONCE at boot against whatever the
    # bundle shipped, and the store commits the fingerprint for an unrecoverable
    # reload too (the fail-closed deny-all IS the safe cached state), so a file
    # that is unreadable at boot aborts boot. Recovery is an operator fix +
    # restart, which is the correct response to a broken bundle.
    unrecoverable = _STORE.unrecoverable_files()
    if unrecoverable:
        raise PlatformCompositionError(
            "governed fleet: profile file(s) present but unrecoverable "
            f"(bind cannot be resolved): {', '.join(sorted(unrecoverable))}. "
            "Refusing to boot with a silently-dropped restrictive profile "
            "(fail-closed). Fix or remove the file(s)."
        )
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
            return _D(False, f"{GOVERNANCE_ERROR_REASON}; denied (fail-closed)", rule="default")
        return _D(True, f"{GOVERNANCE_ERROR_REASON}; no opinion", rule="default")


def vet_and_audit(
    scope: str,
    item: str,
    *,
    session_key: str,
    tool_name: str,
    app: str = "",
    fail_closed: bool = False,
    log_warning: bool = True,
) -> "object":
    """Evaluate one permission decision AND write its SEL audit record.

    The single audited-decision seam for governed outbound messaging
    (``mcp_core._vet_messaging_governance``, shared by ``send_message`` and
    ``send_notification``).  Every permission decision — grant or denial —
    lands in the SEL trail with the same record shape from ONE code path,
    so fail-closed semantics, scope additions, and audit shapes cannot
    drift between callers.  Callers own only their posture (which scopes to
    check, what a denial means); the evaluate+audit pairing lives here.
    A NEW governed outbound-messaging caller MUST use this seam rather than
    pairing ``governance_permits`` with hand-rolled SEL writes.

    Exceptions from :func:`governance_permits` propagate unchanged (it
    already converts unexpected internal errors into a Decision per its
    ``fail_closed`` contract; ``PlatformCompositionError`` still raises) so
    each caller keeps its documented degrade posture.  SEL write failures
    never raise — a best-effort audit must not block or unblock the send.
    """
    decision = governance_permits(
        scope,
        item,
        session_key=session_key,
        app=app,
        log_warning=log_warning,
        fail_closed=fail_closed,
    )
    outcome = "allowed" if getattr(decision, "permitted", False) else "denied"
    try:
        # Local import is DELIBERATE (matches this module's other SEL sites):
        # the test suite's convention is patch("kiro_crew.sel.sel"), which a
        # load-time ``from ... import`` binding would bypass. Keep the lazy
        # lookup so audits stay intercept-able at the source module.
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key=session_key,
            tool_name=tool_name,
            scope=scope,
            item=item,
            outcome=outcome,
            rule=getattr(decision, "rule", ""),
            layer=getattr(decision, "layer", ""),
            reason=getattr(decision, "reason", "") or "",
        )
    except Exception:
        # File-backed SEL write; stdio-silent callers rely on this never
        # touching stdout/stderr and never raising.
        pass
    return decision


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

    Freshness is resolved ONCE, up front, and every binding lookup then reads a
    SINGLE pinned snapshot. Both properties matter for correctness:

    * **Resolve before looking up.** Checking resolution only *after* a lookup
      missed would be a check-after-use: the lookup could read the empty
      never-loaded snapshot, the first load could complete, and the late check
      would then report "resolved" — so the miss (really "not loaded yet") would
      be reported as the authoritative "no profile bound", i.e. policy-only.
    * **One pinned snapshot.** Re-reading ``_STORE`` per lookup lets a concurrent
      reload swap the snapshot mid-resolution, so the app/task/surface precedence
      chain could mix bindings from two different states.
    """
    surface = _infer_surface(session_key)
    if not _STORE.resolved():
        # The store has never loaded and another thread owns that first load. An
        # empty snapshot is indistinguishable from a genuine no-profiles host, so
        # reporting "no profile bound" here would let governance_permits answer
        # with its ``ungoverned`` default-PERMIT (a normal return, so fail_closed
        # cannot catch it). Deny this ONE call instead; the next access resolves.
        logger.warning(
            "profile store not yet loaded (concurrent first load); denying %r this "
            "call rather than treating an unloaded store as ungoverned",
            surface,
        )
        return deny_all_profile(f"_deny_all_unloaded:{surface}")
    snap = _STORE.snapshot()

    def _for_bind(bind: Bind) -> Optional[Profile]:
        name = snap.by_bind.get((bind.type, bind.id))
        return snap.by_name.get(name) if name else None

    if app:
        prof = _for_bind(Bind(type="app", id=app))
        if prof is not None:
            return prof
    if task:
        prof = _for_bind(Bind(type="task", id=task))
        if prof is not None:
            return prof

    # An agent name may carry its own task-scoped profile (e.g. a tightly-scoped
    # "researcher" agent), checked before the broad surface binding so a spawned
    # agent's own ceiling wins over its surface's default.
    if agent:
        prof = _for_bind(Bind(type="task", id=agent))
        if prof is not None:
            return prof

    prof = _for_bind(Bind(type="surface", id=surface))
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

    # Attended/proven surface with no profile → policy ceiling alone governs. This
    # "no profile bound" verdict is trustworthy because resolution was confirmed
    # BEFORE the lookups above (see the docstring).
    return None
