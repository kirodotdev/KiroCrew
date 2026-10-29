"""Detect the sandbox backend, cache it, and wrap a child argv with it.

The backend cache and its warm-up, and :func:`wrap_argv`, which applies the effective
tier or refuses the spawn. One of the owners ``kiro_crew.sandbox`` is carved into: every
name here stays readable and patchable as ``kiro_crew.sandbox.<name>``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time

from kiro_crew.sandbox_argv import (
    _ENV_BINARY_CANDIDATES,
    _launcher_script_of,
    _pinned_env_bin,
    _sandbox_env_unset_args,
    _unset_env_argv,
    namespace_argv,
    sandbox_exec_argv,
)
from kiro_crew.sandbox_delegation import (
    _command_log_label,
    _delegate_to_kiro_internal_sandbox,
    _spawns_kiro_cli,
    kiro_internal_sandbox_enabled,
)
from kiro_crew.sandbox_probe import (
    _PROBE_TRANSIENT_RETRY_DELAY_SECS,
    _WARM_JOIN_TIMEOUT_SECS,
    REMEDY_MOUNT_DENIED,
    _inside_kirocrew_sandbox,
    _inside_macos_sandbox,
    _linux_remedy_guidance,
    _macos_sandbox_state,
    _probe_sandbox_exec,
    _probe_unshare_once,
    is_docker_container,
)
from kiro_crew.sandbox_seals import _warn_aliased_strict_leaves
from kiro_crew.sandbox_tiers import (
    _IN_SANDBOX_LEVEL_VAR,
    _IN_SANDBOX_MARKER,
    _TIER_ORDINALS,
    _clamp_sandbox_mode_to_floor,
    _floor_mandates_sandbox,
    _governance_sandbox_floor,
    _mode_to_level,
    enterprise_policy_cache_view,
)
from kiro_crew.sandbox_unavailable import (
    UNSANDBOXED_BY_OPERATOR,
    UNSANDBOXED_BY_PLATFORM,
    SandboxUnavailableError,
    _allow_no_isolation,
    _allow_unsandboxed_exec,
    _classify_unavailable,
    _first_party_no_backend_passthrough,
    _forward_for_effective_tier,
    _no_backend_guidance,
    _unsandboxed_exec_key_declared,
    _unsandboxed_grant_source,
    _warn_mode_off_unconfined,
    refuse_unaudited_on_dead_fs,
)

# Logged under the sandbox's name, which operator log filters, level settings
# and the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.sandbox")


# Detail of the most recent failed userns probe: (transient, reason, remedy).
# ``None`` means the last probe succeeded (or none has run yet). Consumed by
# detect_backend() for cache policy and by wrap_argv() for error reporting.
#
# One value, swapped atomically, so a reader always gets a reason and the remedy
# from the SAME probe. Holding the remedy in a second global would let a
# concurrent re-probe land between the two reads and pair one probe's failure
# with another's mechanism, which is the wrong fix presented as the right one.
_last_unshare_failure: tuple[bool, str, str] | None = None


def unavailable_remedy() -> str:
    """Public: remedy token for the most recent sandbox probe failure.

    ``""`` when the last probe succeeded, when none has run, or when the failure
    identifies no host mechanism. Pair it with :func:`unavailable_kind` — a
    ``"transient"`` failure is momentary resource pressure and must never be
    presented as something the operator should reconfigure.
    """
    if _last_unshare_failure is None:
        return ""
    return _last_unshare_failure[2]


def unavailable_reason() -> str:
    """Public: technical reason for the most recent sandbox probe failure.

    Names the failing step verbatim (e.g. ``"unshare(CLONE_NEWNS) failed with
    errno 1 (EPERM)"``), so a diagnostic surface can show the kernel's answer
    rather than a paraphrase. ``""`` when the last probe succeeded or none has
    run. Sibling accessor to :func:`unavailable_remedy`, reading the same
    recorded failure so the two can never describe different probes.
    """
    if _last_unshare_failure is None:
        return ""
    return _last_unshare_failure[1]


def remedy_guidance(remedy: str) -> str:
    """Public: mechanism-specific guidance for a ``REMEDY_*`` token (``""`` if none).

    The stable cross-module entry point for :data:`_LINUX_REMEDY_GUIDANCE`, so
    diagnostic surfaces (doctor, dashboard) render the one shared remedy text
    for a mechanism instead of maintaining a drifting copy.
    """
    return _linux_remedy_guidance(remedy)


# ── Background warm thread (never-block-on-loop policy) ──
# The event loop NEVER executes fork/waitpid/sleep for the probe. On-loop
# callers with a cold cache get an immediate transient "none" (fail-closed,
# self-heals in ms) and fire a background daemon thread that populates the
# cache off-loop. Boot sites call warm_backend() (via asyncio.to_thread) to fill
# the cache before any on-loop caller ever reaches detect_backend(), so the
# transient path is typically never hit in production.

_warm_thread: threading.Thread | None = None


def _record_probe_failure(transient: bool, reason: str, remedy: str = "") -> None:
    """Record a probe failure and its remedy token together.

    Sole writer of the pair, so a token can never outlive the failure it
    describes: a caller that records a failure without probing omits `remedy` and
    thereby clears any earlier one, instead of having to remember a separate line.
    That matters because the token is surfaced to the user even for a transient
    verdict, so a stale one would name the wrong host mechanism.
    """
    global _last_unshare_failure
    _last_unshare_failure = (transient, reason, remedy)


def _background_warm() -> None:
    """Run the probe off-loop and populate the cache. Thread target."""
    global _backend, _last_unshare_failure
    for attempt in (1, 2):
        ok, transient, reason, remedy = _probe_unshare_once()
        if ok:
            _last_unshare_failure = None
            _backend = "namespace"
            logger.info("Background warm: sandbox backend = namespace")
            return
        _record_probe_failure(transient, reason, remedy)
        if not transient:
            logger.warning("Background warm: probe permanent failure: %s", reason)
            _backend = "none"
            return
        logger.warning("Background warm: probe transient (attempt %d/2): %s", attempt, reason)
        if attempt == 1:
            time.sleep(_PROBE_TRANSIENT_RETRY_DELAY_SECS)
    # Both attempts transient — leave cache uncached (None) so next call re-tries
    logger.warning("Background warm: both attempts transient, cache stays cold")


def _kick_background_warm() -> None:
    """Start the background warm thread if not already running.

    Thread-start failure (RuntimeError under thread exhaustion) is swallowed:
    the cache stays cold and the pre-existing self-healing transient path
    applies on the next spawn.  This keeps gateway boot stable even when the
    host is resource-constrained.
    """
    global _warm_thread
    if _warm_thread is not None and _warm_thread.is_alive():
        return  # dedupe: warm already in progress
    _warm_thread = threading.Thread(target=_background_warm, name="sandbox-probe-warm", daemon=True)
    try:
        _warm_thread.start()
    except RuntimeError:
        _warm_thread = None
        logger.debug("sandbox warm thread start failed; cache stays cold")


def prewarm_backend() -> None:
    """Fire-and-forget boot hook: start background probe to fill the cache.

    The gateway boot sites (slack/gateway_runtime/service_boot.py,
    mcp_gateway/daemon/cli.py::_amain)
    call the blocking ``warm_backend`` instead, which waits for the probe so the
    cache is warm before any on-loop spawn path reaches detect_backend().
    """
    if sys.platform != "linux":
        return  # probes are Linux-only
    _kick_background_warm()


def warm_backend(timeout: float = _WARM_JOIN_TIMEOUT_SECS) -> None:
    """Blocking boot hook: fill the probe cache BEFORE returning.

    ``prewarm_backend`` only *starts* the probe, so a caller that reaches
    ``detect_backend`` in the same tick still races the warm thread and gets the
    synthetic-transient answer — a cold-cache false negative that reads exactly
    like "this host has no sandbox backend". This variant waits for the probe to
    land, so the next ``detect_backend`` sees a warm cache and the transient path
    is not reachable from a warmed boot.

    The wait is bounded: ``_background_warm`` makes at most two attempts with a
    single short delay between them, and a join timeout caps the total. A timeout
    is not an error — the cache simply stays cold and the pre-existing
    self-healing transient path applies, exactly as with ``prewarm_backend``.

    **Never-block-on-loop invariant**: this blocks on a thread join, so it MUST
    NOT be called from a running event loop. On-loop callers use
    ``await asyncio.to_thread(warm_backend)``; synchronous callers (CLI paths)
    may call it directly.
    """
    if sys.platform != "linux":
        return  # probes are Linux-only
    _kick_background_warm()
    thread = _warm_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout)


def _probe_unshare() -> bool:
    """Return True if user + mount namespaces work (Linux).

    Failures are logged with their errno and classified transient vs
    permanent in :data:`_last_unshare_failure`; a transient failure gets one
    immediate retry (off-loop only).

    **Never-block-on-loop invariant**: when called from a running asyncio
    event loop with a cold cache, this function does NOT probe — it fires
    ``_kick_background_warm()`` and returns False with a transient reason.
    The background thread populates the cache in ms; the next spawn re-checks
    and finds a warm cache. Boot prewarm ensures this path is rarely hit.

    Callers deciding cache policy (detect_backend) MUST consult the
    classification — a transient result is not evidence that the host lacks
    a sandbox backend.
    """
    global _last_unshare_failure
    if sys.platform != "linux":
        _record_probe_failure(False, "not Linux")
        return False

    # Fast path: the cache already proved user namespaces work -- no probe
    # needed. Keeps on-loop callers correct after prewarm instead of
    # deferring and returning False.
    if _backend == "namespace":
        return True

    # Detect running event loop — governs whether we probe directly or defer.
    on_loop = False
    try:
        asyncio.get_running_loop()
        on_loop = True
    except RuntimeError:
        pass

    if on_loop:
        # NEVER probe on the event loop. Kick background warm and fail transient.
        _kick_background_warm()
        # No probe ran, so the omitted remedy clears any older failure's token.
        _record_probe_failure(
            True,
            "probe deferred to background thread (cold cache on event loop); "
            "cache warms in ms — retry",
        )
        return False

    # Off-loop: direct probe with one retry on transient failure.
    for attempt in (1, 2):
        ok, transient, reason, remedy = _probe_unshare_once()
        if ok:
            _last_unshare_failure = None
            return True
        _record_probe_failure(transient, reason, remedy)
        if not transient:
            logger.warning("userns probe failed (permanent): %s", reason)
            return False
        logger.warning("userns probe failed (transient, attempt %d/2): %s", attempt, reason)
        if attempt == 1:
            time.sleep(_PROBE_TRANSIENT_RETRY_DELAY_SECS)
    return False


def userns_available() -> bool:
    """Public: True if this host can build the launcher's namespace sandbox.

    "Build" means the whole handshake the launcher performs before it hides
    anything: an unprivileged user namespace, a mount namespace inside it, and
    a private propagation mount on ``/`` inside that. A host that grants the
    namespaces but refuses the mount (a container under its runtime's default
    AppArmor profile) is NOT sandbox-capable, and reporting it as such made
    every real spawn die in the launcher instead.

    Stable cross-module entry point for the namespace-support probe, shared by
    the OS-level sandbox here and the JailProvider extension point
    (``platform/interfaces_runtime.py``), so consumers do not depend on the private
    ``_probe_unshare`` name.
    """
    return _probe_unshare()


# ── Public API ──

_backend: str | None = None  # "namespace", "sandbox-exec", "none"


def unavailable_kind() -> str:
    """Classify a backend-less host for callers that offer a PERSISTENT opt-in.

    Returns ``""`` when a backend IS available, otherwise the same value
    ``SandboxUnavailableError.kind`` would carry.

    A caller that writes ``sandbox_allow_unsandboxed_exec`` to disk must act
    ONLY on ``"no_backend"``. ``detect_backend()`` alone is not enough: it also
    reports ``"none"`` for a momentary fork/resource failure, which self-heals on
    the next spawn and must never buy a permanent bypass — and for a foreign
    outer sandbox, where the host's own sandbox is fine and the remedy is to hand
    isolation back to Kiro Crew rather than disable it.
    """
    if detect_backend() != "none":
        return ""
    transient, _reason, _remedy = _last_unshare_failure or (False, "none", "")
    return _classify_unavailable(transient)


def _warn_no_isolation(mode: str, granted_by: str = "") -> None:
    """Loudly surface that the agent subprocess is running WITHOUT OS-level
    isolation, so the fallback is never silent.

    When no sandbox backend is available the credential paths (``~/.aws``,
    ``~/.ssh``, ...) are visible to the (untrusted) agent subprocess and only
    the bypassable app-level ``security.py`` checks remain. This is a real
    degradation of the security posture, so it is logged as a WARNING unless
    the operator has explicitly acknowledged it via
    ``agent.sandbox_allow_no_isolation``. Emitted once per process.

    ``granted_by`` names which permission let the spawn through
    (``UNSANDBOXED_BY_OPERATOR`` / ``UNSANDBOXED_BY_PLATFORM``) and changes the
    REMEDY, not the severity. "Install a supported sandbox" is unactionable on a
    platform that has none to install, and a warning whose only suggestion is
    impossible trains readers to ignore it — so a platform-default grant is told
    how to LOCK THE HOST DOWN instead. The default is empty so the existing
    ``mode="off"`` callers keep the operator-shaped text.
    """
    if getattr(wrap_argv, "_warned", False):
        return
    wrap_argv._warned = True  # type: ignore[attr-defined]
    if _allow_no_isolation():
        logger.info(
            "OS-level sandbox unavailable (mode=%s, permitted by %s); running "
            "WITHOUT credential isolation. Operator opted in via "
            "agent.sandbox_allow_no_isolation; app-level checks are the only "
            "remaining boundary.",
            mode,
            granted_by or "config",
        )
        return
    if granted_by == UNSANDBOXED_BY_PLATFORM:
        logger.warning(
            "SECURITY: this platform offers no OS-level sandbox backend for Kiro "
            "Crew to apply (mode=%s), so the agent subprocess runs WITHOUT "
            "credential isolation — ~/.aws, ~/.ssh and other secrets are readable "
            "by it and only the bypassable app-level security.py checks remain. "
            "This is the documented default for such a host, not a failure: no "
            "backend can be installed here. To refuse these spawns instead, set "
            "agent.sandbox_allow_unsandboxed_exec=false in ~/.kiro/crew/config.json "
            "(or pin a governance sandbox.min_level, which overrides it fleet-wide).",
            mode,
        )
        return
    logger.warning(
        "SECURITY: no OS-level sandbox backend is available on this host "
        "(mode=%s), so the agent subprocess runs WITHOUT credential isolation — "
        "~/.aws, ~/.ssh and other secrets are readable by it and only the "
        "bypassable app-level security.py checks remain. Install a supported "
        "sandbox (Linux user namespaces, or macOS sandbox-exec), or set "
        "agent.sandbox_allow_no_isolation=true in ~/.kiro/crew/config.json to "
        "acknowledge the risk and silence this warning.",
        mode,
    )


def detect_backend(config_mode: str = "auto") -> str:
    """Detect the best available sandbox backend.

    Cache policy (a single transient fork failure must not poison the cache and
    fail-close every spawn until restart):

    - A positive result (``"namespace"``/``"sandbox-exec"``) is cached for the
      process lifetime — kernel capability does not change while running.
    - ``"none"`` is cached ONLY when the userns probe failure looks permanent
      (kernel refuses user namespaces: EPERM/EINVAL/ENOSYS). A transient
      resource failure (fork EAGAIN, EMFILE, ...) is never cached — the next
      spawn re-probes and self-heals.
    - ``config_mode="off"`` short-circuits to ``"none"`` without probing and
      without touching the cache. All other modes share one cache entry:
      backend capability is mode-independent, so mode alternation does not
      force pointless re-probes.
    """
    global _backend
    if config_mode == "off":
        return "none"
    if _backend is not None:
        return _backend
    if userns_available():
        _backend = "namespace"
    elif _probe_sandbox_exec():
        _backend = "sandbox-exec"
    else:
        transient, reason, _remedy = _last_unshare_failure or (False, "none", "")
        if transient:
            logger.warning(
                "Sandbox backend probe failed transiently (%s); result NOT cached — "
                "the next spawn re-probes",
                reason,
            )
            return "none"
        _backend = "none"
    logger.info("Sandbox backend: %s (config_mode=%s)", _backend, config_mode)
    return _backend


def reset_backend() -> None:
    """Reset cached backend (for testing or config change)."""
    global _backend, _last_unshare_failure
    _backend = None
    _last_unshare_failure = None


def wrap_argv(
    argv: list[str],
    mode: str = "auto",
    *,
    strip_python_env: bool = False,
    forward_ssh_auth_sock: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    seal_hidden_dirs: bool = False,
    extra_hidden_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_alias_credential_ids: tuple[tuple[int, int], ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_private_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    extra_expose_files: tuple[str, ...] = (),
    exec_cwd: str = "",
    is_kiro_cli: bool | None = None,
    first_party_fixed_argv: bool = False,
) -> tuple[list[str], str | None]:
    """Wrap a command argv with OS-level sandbox if available.

    Args:
        argv: Original command + args.
        mode: ``"auto"``/``"standard"`` (expose .aws/.ssh/.kube),
              ``"cc"`` (hide .aws but expose .aws/config for Bedrock auth),
              ``"strict"`` (hide everything), ``"off"`` (no sandbox).
        extra_hidden_dirs: Additional absolute directory trees to deny.
        extra_visible_dirs: Trusted paths that must remain visible when an
            otherwise-hidden parent contains them (the whole parent's mask is lifted).
        extra_private_dirs: The spawn's OWN directories inside a hidden tree
            (its ``agent_scratch`` dir under the masked scratch root). Re-exposed
            read-write as a window; the parent's mask and every sibling stay hidden.
        extra_expose_files: Absolute files to keep READABLE inside dirs that
            ``extra_hidden_dirs`` hides. Linux restores a read-only COPY via
            the launcher's ``expose_files`` primitive (cc mode's mechanism
            for ``.aws/config``); Seatbelt carves a ``require-not (literal)``
            exception out of the hidden dir's read deny (the shape it uses
            for ``.ssh/known_hosts``). Writes and hardlinks stay denied on
            both.
        extra_writable_dirs: Self-derived scratch directories INSIDE the sealed
            runtime parent (``<data home>/run``) that the child must be able to
            write — e.g. the MCP probe's private ``TMPDIR``. Validated
            inside the plan by :func:`kiro_crew.sandbox_plan.writable_carveouts`;
            a candidate that would re-open any other seal is refused with a
            security warning. Inert on
            backends with no path seal to carve (Windows, the no-backend
            fail-open path): there the directory is already writable.
        exec_cwd: A directory the Linux namespace launcher enters after its seals
            and before it execs, failing closed unless it is read-only and empty
            (an app agent's launch copy). Ignored by every other backend.
        is_kiro_cli: Explicit executable classification for descriptor-backed
            Kiro snapshots whose launch path has no ``kiro-cli``
            basename. ``None`` retains basename detection for other callers.
            Windows internal-sandbox delegation requires this to be exactly
            ``True``; basename inference can never grant that exception.
        first_party_fixed_argv: True ONLY for spawns whose full argv is derived
            inside this package with zero agent/repo/user-config influence;
            every passing site must be allowlisted in
            ``test/test_spawn_audit.py::FIRST_PARTY_SPAWNS``. On a host with
            genuinely no sandbox backend (``no_backend`` — never a transient
            probe failure or a foreign outer sandbox) and no governance
            ``sandbox.min_level`` floor, such a spawn proceeds unconfined
            (env-scrubbed, loudly warned, SEL ``outcome="unconfined"``) instead
            of fail-closing. Inert whenever a backend exists or
            ``sandbox_allow_unsandboxed_exec`` is set.

    Returns:
        (wrapped_argv, cleanup_path_or_None).
        *cleanup_path* is a temp file to delete after the child exits
        (macOS seatbelt profile or Linux launcher script).
        ``None`` when no cleanup is needed.

    Raises:
        RuntimeError: When no sandbox backend is available, mode is not "off",
            ``agent.sandbox_allow_unsandboxed_exec`` is False (default), and
            neither the first-party carve-out nor the explicitly classified
            Windows Kiro internal-sandbox delegation applies.
            This is the fail-closed behavior — the agent subprocess is NOT
            allowed to run without OS-level isolation unless explicitly opted in.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "wrap_argv() performs blocking sandbox preparation and cannot run on "
            "an event loop; await wrap_argv_async() instead"
        )

    # Governance ordinal floor: a policy/profile may require a MINIMUM sandbox
    # tier (off < standard < cc < strict).  Clamp the requested mode up to that
    # floor before resolving the level — so an enterprise "min_level: cc" makes
    # even a mode="off" call run confined.  Cheap no-op when ungoverned.
    #
    # ONE read per wrap_argv call, reused by the first-party carve-out below:
    # the (potentially profile-walking) resolve runs once, and the clamp and
    # the carve-out condition can never disagree about the same host.
    governance_floor = _governance_sandbox_floor()
    mode = _clamp_sandbox_mode_to_floor(mode, governance_floor)
    # The caller resolved the SSH forward against its own floor read. Re-judge
    # it against the tier this wrap applies, so a floor raised in between can
    # only drop the socket. Never turns a False into True.
    if forward_ssh_auth_sock:
        try:
            forward_ssh_auth_sock = _forward_for_effective_tier(mode, extra_hidden_dirs)
        except Exception:
            forward_ssh_auth_sock = False

    if mode == "off":
        # Fix #2: verify kiro-cli delegation before honoring "off". The
        # documented invariant (beside ``_KIRO_INTERNAL_SETTINGS_PATH``) requires that when
        # Kiro Crew's seatbelt is off, kiro-cli's internal sandbox is ON —
        # but the old early return never checked. Now we verify the delegation
        # on macOS kiro-cli spawns; on Linux (where kiro's internal sandbox
        # doesn't apply) or non-kiro spawns, "off" means genuinely unconfined.
        kiro_spawn_off = _spawns_kiro_cli(argv) if is_kiro_cli is None else is_kiro_cli
        if sys.platform == "darwin" and kiro_spawn_off and kiro_internal_sandbox_enabled():
            # Delegation is valid: kiro-cli's sandbox IS active. Apply env scrub
            # (same as _delegate_to_kiro_internal_sandbox) but WITHOUT the
            # seatbelt fallback on SEL failure — mode="off" must never produce a
            # nested seatbelt wrap (the exact EPERM case the design prevents).
            # SEL audit-or-degrade: record the delegation with critical=True
            # (synchronous write for tamper-evident log), but on failure degrade
            # to unconfined passthrough rather than seatbelt wrap (which would
            # EPERM inside kiro-cli's already-active sandbox).
            try:
                from kiro_crew.sel import sel

                sel().log_tool_invocation(
                    session_key="sandbox",
                    agent="system",
                    source="sandbox.wrap_argv",
                    tool_name=_command_log_label(argv),
                    tool_kind="subprocess",
                    outcome="delegated",
                    resources=(
                        "mode=off: kiro internal sandbox on -> env scrub only "
                        "(no seatbelt, no seatbelt-fallback)"
                    ),
                    critical=True,  # synchronous write for audit integrity
                )
            except Exception as exc:
                refuse_unaudited_on_dead_fs(exc, "mode=off delegation audit")
                # Fail OPEN (not to seatbelt): an unaudited delegation with
                # mode=off still applies env scrub but returns without seatbelt.
                # This is deliberately different from _delegate_to_kiro_internal_sandbox
                # which falls back to seatbelt — here that fallback would EPERM.
                logger.warning(
                    "SECURITY: SEL audit failed for mode=off delegation; "
                    "proceeding with env scrub but no seatbelt. Command: %s",
                    _command_log_label(argv),
                    exc_info=True,
                )
            unset_args = _sandbox_env_unset_args(
                "standard", strip_python_env, forward_ssh_auth_sock
            )
            if unset_args:
                return [_pinned_env_bin(), *unset_args, *argv], None
            return list(argv), None
        # Fix #3: Make the degradation loud — both layers are inactive.
        _warn_mode_off_unconfined(argv, kiro_spawn_off)
        return argv, None

    # Already inside a Kiro Crew sandbox (script cron, sandboxed agent child, app
    # backend, pooled MCP server): the outer sandbox confines every descendant,
    # and a nested wrap is impossible by design — Linux seccomp denies the
    # unshare, macOS Seatbelt refuses sandbox_apply with EPERM. Pass through
    # within the existing isolation boundary.
    #
    # On macOS the marker must agree with the kernel, and the two cover each
    # other's blind spot: the marker proves Kiro Crew built the outer sandbox and
    # scrubbed the credential env on the way in, but an env var alone could be
    # forged; the kernel independently confirms a sandbox IS active, but cannot
    # say whose profile it is, so it can never grant this on its own. A definite
    # kernel "not sandboxed" therefore vetoes the marker. An *unanswerable* probe
    # does not: that says nothing, and must not invalidate a marker the Linux
    # path honours unconditionally.
    if _inside_kirocrew_sandbox() and _macos_sandbox_state() is not False:
        if not getattr(wrap_argv, "_nested_passthrough_logged", False):
            wrap_argv._nested_passthrough_logged = True  # type: ignore[attr-defined]
            logger.info(
                "wrap_argv: already inside a Kiro Crew sandbox — nested OS "
                "sandboxing is impossible by design (Linux seccomp denies "
                "unshare; macOS Seatbelt refuses sandbox_apply with EPERM); "
                "spawning within the existing isolation boundary rather than "
                "fail-closing on a nesting artifact"
            )
        # Compare the tier the caller asked for against the tier the OUTER
        # sandbox was built at. The passthrough is unavoidable (a nested
        # re-wrap is denied by design on both platforms), but a tier
        # downgrade must be visible, not silent. ``unknown`` = launcher
        # predates the level export (or the value is unrecognized); it has no
        # ordinal, so no downgrade can be proven against it.
        requested_level = _mode_to_level(mode)
        active_level = os.environ.get(_IN_SANDBOX_LEVEL_VAR) or "unknown"
        if active_level not in _TIER_ORDINALS:
            active_level = "unknown"
        tier_downgrade = (
            active_level in _TIER_ORDINALS
            and _TIER_ORDINALS[requested_level] > _TIER_ORDINALS[active_level]
        )
        if tier_downgrade:
            # Loud and per-call (not once-only like the info log above): every
            # downgraded spawn is a distinct security-relevant event.
            logger.warning(
                "SECURITY: nested-sandbox passthrough tier downgrade — caller "
                "requested %r but the outer sandbox runs at %r, so %s executes "
                "at the weaker tier (a nested re-wrap is impossible by design). "
                "Applying the stricter tier's env scrub to the passthrough.",
                requested_level,
                active_level,
                _command_log_label(argv),
            )
        # Emit an SEL audit event for this security-relevant passthrough so the
        # decision to spawn without a *fresh* wrap is tamper-evidently recorded,
        # mirroring the ``denied`` event on the fail-closed path. Outcome is
        # ``allowed`` (a permission grant, not a denial). Fires on EVERY
        # passthrough so the audit trail is complete.
        #
        # critical=True gives this the same write reliability as the fail-closed
        # ``denied`` audit and the ``delegated`` audit: the event is written
        # SYNCHRONOUSLY after draining the async backlog (sel.log), so a
        # slow/wedged background writer can NOT silently drop passthrough
        # records. What it does NOT do is re-raise into a *deny*: unlike
        # _delegate_to_kiro_internal_sandbox — which on audit failure falls back
        # to Kiro Crew's own seatbelt, an equally-safe audited layer — a nested
        # passthrough has no safe alternative (seccomp denies the re-wrap by
        # design). Failing the spawn on a SEL filesystem error would couple every
        # in-sandbox MCP call to SEL health and reintroduce a prior in-sandbox
        # spawn outage. The child is confined by the outer namespace + seccomp
        # whether or not the record lands, so on a hard write failure we log
        # loudly and proceed: availability of the confinement over a best-effort
        # audit gap during an already-degraded FS.
        try:
            # circular import (see the fail-closed branch below for the full
            # rationale): sandbox.py is a low-level leaf; defer the sel import.
            from kiro_crew.sel import sel

            sel().log_tool_invocation(
                session_key="sandbox",
                agent="system",
                source="sandbox.wrap_argv",
                tool_name=_command_log_label(argv),
                tool_kind="subprocess",
                outcome="allowed",
                metadata={
                    "reason": "nested_sandbox_passthrough",
                    "mode": mode,
                    "requested_tier": requested_level,
                    "active_tier": active_level,
                    # tier_known separates "proven no downgrade" from
                    # "unprovable": tier_downgrade=False alone cannot tell a
                    # consumer which of the two it is looking at.
                    "tier_known": active_level in _TIER_ORDINALS,
                    "tier_downgrade": tier_downgrade,
                },
                critical=True,
            )
        except Exception as exc:
            refuse_unaudited_on_dead_fs(exc, "nested-sandbox passthrough audit")
            logger.warning(
                "SEL audit failed for nested-sandbox passthrough — proceeding "
                "unaudited: the outer namespace + seccomp still confine this "
                "spawn, and denying it would brick in-sandbox MCP calls whenever "
                "SEL is down",
                exc_info=True,
            )
        if tier_downgrade:
            # The one slice of the stricter tier that IS enforceable without a
            # nested wrap: its env scrub. A standard outer sandbox scrubbed
            # only _SENSITIVE_ENV_PREFIXES, so agent-denied credential keys
            # (Slack tokens, owner id) are still in this environment; prefix
            # the child with the requested tier's ``env -u`` scrub so it does
            # not inherit them (a delta in practice — the outer launcher
            # already removed the shared prefixes). File-level hides still run
            # at the outer tier — that residual gap is exactly what the audit
            # above records. The ``env`` binary is resolved at a trusted
            # absolute path only (:func:`_unset_env_argv`): this environment
            # can carry a PATH that leads with user-writable directories, and
            # a planted ``env`` there would receive exactly the credentials
            # this scrub exists to withhold. No trusted binary → keep the
            # plain passthrough (never fail closed) and say so.
            unset_args = _sandbox_env_unset_args(
                requested_level, strip_python_env, forward_ssh_auth_sock
            )
            if unset_args:
                scrub_keys = tuple(unset_args[1::2])
                env_prefix = _unset_env_argv(scrub_keys)
                if env_prefix is not None:
                    return [*env_prefix, *argv], None
                logger.warning(
                    "SECURITY: no trusted env binary (%s) — the requested "
                    "tier's env scrub cannot be applied to this passthrough; "
                    "spawning without it",
                    ", ".join(_ENV_BINARY_CANDIDATES),
                )
        return argv, None
    if _inside_kirocrew_sandbox():
        # Marker present, kernel says NOT sandboxed: the marker can only have been
        # forged or inherited into an unconfined process. Refuse the passthrough
        # and fall through to a normal wrap.
        logger.warning(
            "SECURITY: %s is set but the kernel reports this process is NOT "
            "sandboxed — refusing the nested-sandbox passthrough and falling back "
            "to a normal wrap.",
            _IN_SANDBOX_MARKER,
        )

    # "auto"/"standard" allows git-over-SSH, AWS CLI, kubectl.
    # "cc" hides .aws (exposes only .aws/config for Bedrock credential_process).
    # "strict" hides everything.
    sandbox_level = _mode_to_level(mode)

    # The ONE place an aliased strict leaf is REPORTED, covering every spawn this function can
    # produce: the Linux namespace wrap, the macOS Seatbelt wrap, the delegated kiro-cli spawn,
    # and the Windows no-backend path. A rule stated once per platform branch is a rule each
    # branch can be edited out of independently, and this one belongs to none of them: it is a
    # fact about a NAME, settled before any mechanism is chosen. What stays platform-specific
    # below is the mechanism that enforces a seal.
    #
    # It WARNS and lets the spawn through. The refusal lives where the file is CONSUMED --
    # `provisioners.engine_for` for `cloud.json`, `LaunchState.load` for the launch record --
    # because refusing here refused every sandboxed spawn on the host, a chat turn, a cron job,
    # a subagent, whenever a leaf carried a second name, and an install laid down by stow,
    # chezmoi or `rsync --link-dest` has that shape for reasons that have nothing to do with
    # Fargate. The blast radius was the whole box; the exposure is one command. The alias harm
    # is a refused command either way, and nothing else stops working.
    #
    # The warning matters most on the path that applies no seal of ours. A delegated spawn is
    # confined by kiro-cli's own sandbox and Crew wraps nothing, so a write reaching
    # `cloud.json` through an alias whose target sits outside the data home chooses the
    # container image a Fargate launch runs, and the task's execution role hands the model
    # credential to it. The check reads no file contents, so it needs no knowledge of which
    # layer owns isolation and there is no encoding to bypass. Absent leaves cost nothing:
    # `_warn_if_alias_backed` returns when the lstat fails.
    #
    # The passthrough tiers above return before this point, which is deliberate: with no
    # sandbox at all Crew claims no seal, so there is nothing here to be bypassed.
    _warn_aliased_strict_leaves()

    # macOS sandbox mutual exclusion: kiro-cli >= 2.13's internal sandbox cannot
    # initialize nested inside Kiro Crew's seatbelt (kernel EPERM even under an
    # allow-all outer profile), so exactly one layer can own isolation. When
    # kiro's internal sandbox is enabled, it is that layer for kiro-cli spawns;
    # Kiro Crew's sandbox stays on for everything else and whenever kiro's is off.
    # Windows has no Kiro Crew OS sandbox backend. Official Kiro ACP spawns are
    # positively classified by their reviewed callers and delegate to Kiro's
    # built-in sandbox by default; basename inference is deliberately
    # insufficient to grant this exception. All other Windows spawns retain the
    # no-backend fail-closed path. Checked before backend detection so this is a
    # deterministic capability decision, never a fallback after a probe failure.
    # Linux namespace isolation is unaffected.
    kiro_spawn = _spawns_kiro_cli(argv) if is_kiro_cli is None else is_kiro_cli
    delegate_to_kiro = (
        sys.platform == "darwin" and kiro_spawn and kiro_internal_sandbox_enabled()
    ) or (sys.platform == "win32" and is_kiro_cli is True)
    if delegate_to_kiro:
        # ``extra_private_dirs`` is deliberately NOT in this test: a private
        # window only RELAXES a mask owned by Kiro Crew (the scratch root) for the
        # spawn's own directory. A delegated sandbox applies none of those
        # masks, so the window is moot there and must not cost the delegation
        # (on Windows that would send every session to the no-backend path).
        if (
            extra_hidden_dirs
            or extra_hidden_dir_ids
            or extra_visible_dirs
            or extra_writable_dirs
            or extra_expose_files
        ):
            # A delegated sandbox cannot enforce the path hides specific to Kiro Crew.
            # macOS keeps the outer seatbelt. Windows falls through to its
            # no-backend policy and fail-closes unless explicitly opted in.
            if sys.platform == "darwin":
                return sandbox_exec_argv(
                    argv,
                    sandbox_level,
                    strip_python_env=strip_python_env,
                    forward_ssh_auth_sock=forward_ssh_auth_sock,
                    extra_hidden_dirs=extra_hidden_dirs,
                    extra_hidden_dir_ids=extra_hidden_dir_ids,
                    extra_visible_dirs=extra_visible_dirs,
                    extra_private_dirs=extra_private_dirs,
                    extra_private_dir_ids=extra_private_dir_ids,
                    extra_writable_dirs=extra_writable_dirs,
                    extra_expose_files=extra_expose_files,
                )
        else:
            delegated = _delegate_to_kiro_internal_sandbox(
                argv,
                sandbox_level,
                strip_python_env=strip_python_env,
                forward_ssh_auth_sock=forward_ssh_auth_sock,
            )
            if delegated is not None:
                return delegated
            if sys.platform == "darwin":
                # Preserve macOS's audit-failure fallback: once delegation is
                # refused, Kiro Crew's own seatbelt remains the safe owner.
                return sandbox_exec_argv(
                    argv,
                    sandbox_level,
                    strip_python_env=strip_python_env,
                    forward_ssh_auth_sock=forward_ssh_auth_sock,
                )

    backend = detect_backend(config_mode=mode)

    # An enterprise build's sandboxed processes resolve the ceiling from the governance
    # cache, so it is shown to them READ-ONLY (the plan seals a lifted cache). After the
    # delegation branch on purpose: a delegated sandbox applies none of Kiro Crew's masks,
    # and adding a path here would cost the delegation.
    cache_view = tuple(
        path
        for path in enterprise_policy_cache_view(mode, extra_hidden_dirs)
        if path not in extra_visible_dirs
    )
    if cache_view:
        extra_visible_dirs = (*extra_visible_dirs, *cache_view)

    if backend == "namespace":
        if (
            extra_hidden_dirs
            or extra_visible_dirs
            or extra_private_dirs
            or extra_writable_dirs
            or extra_expose_files
            or exec_cwd
        ):
            wrapped = namespace_argv(
                argv,
                sandbox_level,
                strip_python_env=strip_python_env,
                forward_ssh_auth_sock=forward_ssh_auth_sock,
                extra_hidden_dirs=extra_hidden_dirs,
                seal_hidden_dirs=seal_hidden_dirs,
                extra_hidden_dir_ids=extra_hidden_dir_ids,
                extra_alias_credential_ids=extra_alias_credential_ids,
                extra_visible_dirs=extra_visible_dirs,
                extra_private_dirs=extra_private_dirs,
                extra_private_dir_ids=extra_private_dir_ids,
                extra_writable_dirs=extra_writable_dirs,
                extra_expose_files=extra_expose_files,
                exec_cwd=exec_cwd,
            )
        else:
            wrapped = namespace_argv(
                argv,
                sandbox_level,
                strip_python_env=strip_python_env,
                forward_ssh_auth_sock=forward_ssh_auth_sock,
            )
        # Caller deletes the generated launcher script. Its position is
        # ``1 + len(flags)``, NOT a hardcoded 1: the interpreter flags sit between
        # the executable and the script, so hardcoding leaks the tempfile (and
        # hands the caller a flag to unlink) the moment that list changes.
        return wrapped, _launcher_script_of(wrapped)
    if backend == "sandbox-exec":
        if (
            extra_hidden_dirs
            or extra_hidden_dir_ids
            or extra_visible_dirs
            or extra_private_dirs
            or extra_writable_dirs
            or extra_expose_files
        ):
            return sandbox_exec_argv(
                argv,
                sandbox_level,
                strip_python_env=strip_python_env,
                forward_ssh_auth_sock=forward_ssh_auth_sock,
                extra_hidden_dirs=extra_hidden_dirs,
                extra_hidden_dir_ids=extra_hidden_dir_ids,
                extra_visible_dirs=extra_visible_dirs,
                extra_private_dirs=extra_private_dirs,
                extra_private_dir_ids=extra_private_dir_ids,
                extra_writable_dirs=extra_writable_dirs,
                extra_expose_files=extra_expose_files,
            )
        return sandbox_exec_argv(
            argv,
            sandbox_level,
            strip_python_env=strip_python_env,
            forward_ssh_auth_sock=forward_ssh_auth_sock,
        )

    if backend == "none":
        # Reaching here while the kernel says this process IS sandboxed means the
        # outer sandbox is NOT one Kiro Crew built: one Kiro Crew built carries
        # KIROCREW_SANDBOX_ACTIVE and was already passed through above. The
        # remaining nested case is a foreign confiner — kiro-cli's own internal
        # seatbelt, or an operator-wrapped gateway — whose profile macOS gives us
        # no supported way to identify, and whose environment our scrub never
        # touched. We therefore do NOT pass through. What we DO fix is the
        # diagnosis: the probe's EPERM is a nesting artifact, not a host verdict,
        # so the error must not claim this host lacks a sandbox backend.
        #
        # FAIL-CLOSED: refuse to execute without sandbox unless explicitly opted in.
        # Returning unmodified argv would let the agent subprocess reach every
        # credential path with no OS-level isolation.
        #
        # ONE read of the gate: the branch below, the warning on the permitted path
        # and the message that explains a refusal must describe the same state, and
        # a concurrent config reload must not let them disagree about the same
        # spawn. ``granted_by`` is DERIVED from that one verdict rather than
        # resolved again, so it cannot contradict it.
        opted_in = _allow_unsandboxed_exec()
        granted_by = _unsandboxed_grant_source(opted_in)
        # A governance ``sandbox.min_level`` floor OVERRIDES the config opt-in.
        # The floor must not do the opposite of what pinning it implies:
        # disabling the audited first-party carve-out below while leaving this
        # broad opt-in untouched would leave a governed fleet without the
        # constrained path and with the unconstrained one.  ``config.json`` is not
        # policy — the floor is — so the flag cannot re-open this on a governed
        # host.  Derived from the ONE floor read taken at the top of this call,
        # and via ``_floor_mandates_sandbox`` rather than raw truthiness, because
        # a pinned floor of the loosest tier requires nothing and must not deny.
        floor_mandates_sandbox = _floor_mandates_sandbox(governance_floor)
        if floor_mandates_sandbox or not opted_in:
            # ONE read of the pair: a concurrent re-probe swaps the whole tuple,
            # so failure and remedy can never come from different probes.
            transient, probe_reason, probe_remedy = _last_unshare_failure or (
                False,
                "no probe detail recorded",
                "",
            )
            # First-party carve-out: a spawn whose full argv is
            # derived inside this package (never agent/repo/user-config text)
            # may proceed unconfined on a host that GENUINELY has no backend.
            # All three preconditions, structurally:
            #   * the caller vouched via ``first_party_fixed_argv`` — a reviewed
            #     property, ratcheted by test_spawn_audit.py::FIRST_PARTY_SPAWNS;
            #   * the unavailability class is ``no_backend``: a ``transient``
            #     failure still raises (it self-heals on the next spawn and must
            #     not buy a bypass) and ``foreign_sandbox`` still raises (the
            #     host's sandbox is fine; the remedy is config, not bypass);
            #   * no governance ``sandbox.min_level`` floor is active — reuses
            #     the ONE floor read taken at the top of this call (the same
            #     value the clamp used), so no second profile walk runs and the
            #     two checks cannot disagree; a governed host keeps fail-closing
            #     for first-party spawns too.
            if (
                first_party_fixed_argv
                and _classify_unavailable(transient) == "no_backend"
                and not governance_floor
            ):
                return _first_party_no_backend_passthrough(argv, sandbox_level, strip_python_env)
            if transient:
                # The mechanism follows the retry advice rather than leading it: the
                # cap case is permanently reported transient, so withholding it here
                # would leave doctor and the logs unable to name the one sysctl that
                # fixes the host, while leading with it would read as "reconfigure"
                # to someone whose host is merely busy.
                guidance = (
                    "This probe failure looks TRANSIENT (momentary resource "
                    "pressure) — it is not cached and the next spawn re-probes "
                    "automatically. Do NOT disable the sandbox for this; retry "
                    "instead. "
                ) + _linux_remedy_guidance(probe_remedy)
            elif _inside_macos_sandbox():
                # Nesting under a FOREIGN sandbox — say so, and point at the
                # config-level fix that hands isolation back to Kiro Crew's own
                # profile, not at sandbox_allow_unsandboxed_exec (which would
                # disable isolation everywhere to fix a case where a sandbox
                # demonstrably exists).
                guidance = (
                    "This host's sandbox is NOT broken: the kernel reports this "
                    "process is already inside a macOS Seatbelt sandbox that "
                    "Kiro Crew did not create, and Seatbelt cannot nest, so "
                    "sandbox-exec fails with EPERM. Spawns under Kiro Crew's OWN "
                    "sandbox are unaffected — they carry an isolation marker and "
                    "pass through. The usual cause is kiro-cli's internal "
                    'sandbox: set {"sandbox": false} in '
                    "~/.kiro/settings/amazon-internal.json so Kiro Crew's own "
                    "profile owns isolation (that profile is the one that hides "
                    "the credential directories, so this keeps isolation rather "
                    "than weakening it), then restart the gateway. Other outer "
                    "sandboxes (e.g. an operator-wrapped gateway) hit the same "
                    "nesting limit — see docs/system-specs/modules/security.md "
                    '("macOS marker site and the kernel cross-check"). '
                )
            elif is_docker_container():
                # Inside a Docker/OCI container the runtime's seccomp or
                # AppArmor policy blocked a step of the namespace handshake.
                # This is a container-policy restriction, NOT a kernel-level
                # limitation on the host — the correct fix is at the container
                # level, not disabling the sandbox everywhere.
                #
                # WHICH step decides the advice. A refused unshare is seccomp
                # (Docker's default profile gates it on CAP_SYS_ADMIN) and the
                # shipped seccomp profile fixes it. A refused propagation MOUNT
                # after both unshares succeeded is the runtime's default
                # AppArmor profile (`deny mount`) — the Kubernetes default on an
                # AppArmor node, where no seccomp profile is applied at all — and
                # a seccomp profile cannot fix that, so prescribing one would
                # send the operator through a change that leaves the probe
                # failing exactly as before.
                if probe_remedy == REMEDY_MOUNT_DENIED:
                    guidance = (
                        "Running inside a Docker/OCI container whose runtime "
                        "policy permits user namespaces but refuses mount(2) "
                        f"inside them (probe: {probe_reason}). This is the "
                        "runtime's default AppArmor profile (`deny mount`) or a "
                        "seccomp filter, not a host kernel limitation, and it "
                        "needs no root or CAP_SYS_ADMIN to fix. Choose one of:\n"
                        "  (a) Run the container with AppArmor unconfined, and a "
                        "seccomp profile that permits unshare and mount:\n"
                        "        docker run --security-opt apparmor=unconfined "
                        "--security-opt seccomp=kirocrew-seccomp.json ...\n"
                        "        # Kubernetes 1.30+: securityContext.appArmorProfile: "
                        "{type: Unconfined}\n"
                        "        # earlier Kubernetes: annotation "
                        "container.apparmor.security.beta.kubernetes.io/<container>: "
                        "unconfined\n"
                        "  (b) Restart with explicit unsandboxed consent "
                        "(the container is then the only isolation boundary):\n"
                        "        docker run -e KIROCREW_ALLOW_UNSANDBOXED=1 ...\n"
                        "  (c) Manually set agent.sandbox_allow_unsandboxed_exec=true "
                        "in ~/.kiro/crew/config.json inside the container.\n"
                        "See docs/guides/docker.md for the full sandbox troubleshooting guide."
                    )
                else:
                    guidance = (
                        "Running inside a Docker/OCI container where the runtime's "
                        "seccomp or AppArmor policy blocks user namespace creation "
                        f"(probe: {probe_reason}). "
                        "This is a container policy restriction, not a host kernel "
                        "limitation. To resolve, choose one of:\n"
                        "  (a) Use the Kiro Crew custom seccomp profile (adds "
                        "unconditional unshare/clone/mount/umount2 allows to the Docker "
                        "default — less permissive than seccomp=unconfined):\n"
                        "        # With a repo checkout:\n"
                        "        docker run --security-opt "
                        "seccomp=docker/seccomp/kirocrew-seccomp.json ...\n"
                        "        # Without a checkout (image-only):\n"
                        "        curl -fsSL https://raw.githubusercontent.com/"
                        "kirodotdev/KiroCrew/main/docker/seccomp/kirocrew-seccomp.json"
                        " -o kirocrew-seccomp.json\n"
                        "        docker run --security-opt seccomp=kirocrew-seccomp.json ...\n"
                        "  (b) Restart with explicit unsandboxed consent "
                        "(the container is then the only isolation boundary):\n"
                        "        docker run -e KIROCREW_ALLOW_UNSANDBOXED=1 ...\n"
                        "  (c) Manually set agent.sandbox_allow_unsandboxed_exec=true "
                        "in ~/.kiro/crew/config.json inside the container.\n"
                        "See docs/guides/docker.md for the full sandbox troubleshooting guide."
                    )
            else:
                guidance = _no_backend_guidance()
            # When the policy floor is what refused, every guidance above points
            # at the wrong lever: the operator HAS set the opt-in and the flag is
            # deliberately powerless here, so naming it would send them down a
            # dead end.  Replace the remedy rather than appending to it.
            policy_overrode_opt_in = floor_mandates_sandbox and opted_in
            if policy_overrode_opt_in:
                guidance = (
                    "This host is GOVERNED: an enterprise policy pins "
                    f"sandbox.min_level={governance_floor!r}, which forbids "
                    "unsandboxed execution regardless of "
                    "agent.sandbox_allow_unsandboxed_exec — that flag is set on "
                    "this host and is deliberately powerless against the policy, "
                    "so editing config.json cannot resolve this. A governed host "
                    "also withholds the first-party carve-out, so Kiro Crew's own "
                    "built-in spawns are refused here too: this host runs no "
                    "agent subprocess until it has a working sandbox backend "
                    "(see docs/system-specs/modules/security.md) or the policy "
                    "owner relaxes sandbox.min_level."
                )
                sel_reason = (
                    "No sandbox backend available and a governance "
                    f"sandbox.min_level={governance_floor!r} floor forbids "
                    "unsandboxed exec (the config opt-in is set but overridden)"
                )
                refusal = (
                    "Sandbox backend unavailable and a governance policy forbids "
                    "unsandboxed execution. "
                )
            elif _unsandboxed_exec_key_declared():
                # The operator DID decide, and decided to keep this host
                # fail-closed. Telling them the flag "is not set" would send them
                # to set a key they already set, so name their own decision as
                # the thing to revisit. Diagnostic-only, so reading it here rather
                # than alongside the gate costs at worst a stale WORDING under a
                # concurrent config reload, never a wrong allow/deny.
                sel_reason = (
                    "No sandbox backend available and allow_unsandboxed_exec is declared false"
                )
                refusal = (
                    "Sandbox backend unavailable and agent.sandbox_allow_unsandboxed_exec "
                    "is set to false on this host, so unsandboxed execution stays "
                    "refused. Change that key to true (or remove it to accept this "
                    "platform's default) if you want these spawns to run unconfined. "
                )
            else:
                sel_reason = "No sandbox backend available and allow_unsandboxed_exec is not set"
                refusal = "Sandbox backend unavailable and allow_unsandboxed_exec is not set. "
            # Emit SEL audit event for this security-relevant denial so it
            # appears in the tamper-evident audit log (security-review requirement).
            try:
                from kiro_crew.sel import sel  # circular import: sandbox is low-level

                sel().log_tool_invocation(
                    session_key="sandbox",
                    agent="system",
                    source="sandbox.wrap_argv",
                    tool_name=_command_log_label(argv),
                    tool_kind="subprocess",
                    outcome="denied",
                    error=(f"{sel_reason} (probe: {probe_reason})"),
                )
            except Exception:
                logger.warning("Failed to emit SEL audit event for sandbox denial", exc_info=True)
            raise SandboxUnavailableError(
                refusal + "No OS-level sandbox backend is available on this host, and the "
                "agent subprocess cannot be safely isolated. "
                f"Probe detail: {probe_reason}. " + guidance,
                kind=_classify_unavailable(transient),
                detail=probe_reason,
                # A transient verdict is never cached, so the host is free to
                # recover on the next call — but it can still name a mechanism.
                # `user.max_user_namespaces` exhaustion surfaces as ENOSPC, which
                # is indistinguishable from momentary fd/disk pressure, so a
                # configured cap of 0 is permanently reported as transient. Withholding
                # the remedy there leaves the one host this token exists for with
                # no way out; the steps are framed as "if this keeps happening" so
                # they never read as advice to reconfigure a merely busy host.
                remedy=probe_remedy,
            )
        # Permitted: audit, warn (or info), and return unmodified argv.
        #
        # The audit is not optional now that a PLATFORM DEFAULT can reach here.
        # Before, everything on this path had an operator declaration behind it
        # and the config file was itself the record; a spawn permitted because of
        # the platform has no such record, so without this event an unconfined
        # spawn would leave no trace at all. ``outcome="unconfined"`` matches the
        # first-party carve-out's third outcome — neither ``denied`` (nothing was
        # refused) nor ``allowed`` (nothing confines this spawn) — and
        # ``resources`` names WHICH permission applied, so the log distinguishes
        # an accepted risk from a default.
        #
        # Best-effort, like the carve-out and unlike the fail-closed ``denied``
        # audit: this fires for every spawn on a backend-less host, and draining
        # SEL synchronously on each one would put a flush on the gateway event
        # loop. Denying the spawn on an audit hiccup would also brick every MCP
        # server and app backend on Windows, which is the platform this path
        # exists to serve.
        try:
            from kiro_crew.sel import sel  # circular import: sandbox is low-level

            sel().log_tool_invocation(
                session_key="sandbox",
                agent="system",
                source="sandbox.wrap_argv",
                tool_name=_command_log_label(argv),
                tool_kind="subprocess",
                outcome="unconfined",
                resources=(
                    "operator declared agent.sandbox_allow_unsandboxed_exec=true"
                    if granted_by == UNSANDBOXED_BY_OPERATOR
                    else "platform default for a host with no sandbox backend"
                ),
            )
        except Exception:
            logger.warning(
                "SEL audit failed for an unconfined spawn (%s permission) — "
                "proceeding unaudited rather than bricking every agent "
                "subprocess on a host that has no sandbox backend to fall back "
                "on. Command: %s",
                granted_by,
                _command_log_label(argv),
                exc_info=True,
            )
        _warn_no_isolation(mode, granted_by)
    return argv, None
