"""The ``KiroPrerequisiteService`` class: its fields, status surface and bindings.

The class keeps its constructor, the readiness latch and the dashboard snapshot it
serves; each other method is bound from the collaborator that owns its responsibility
(``_probe = _probing._probe``), so the class attribute IS that function, and the
instance keeps every field.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import weakref
from collections.abc import Callable, MutableMapping
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from kiro_crew import kiro_prerequisite_agent_specs as _agent_specs
from kiro_crew import kiro_prerequisite_identity_tracking as _identity_tracking
from kiro_crew import kiro_prerequisite_probe as _probing
from kiro_crew import sandbox
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.kiro_prerequisite_agent_specs import _default_spec_lister
from kiro_crew.kiro_prerequisite_candidate import register_process_start_override_attestation
from kiro_crew.kiro_prerequisite_identity import _AUTH_FINGERPRINT_ABSENT
from kiro_crew.kiro_prerequisite_identity_store import (
    _auth_store_mappings,
    _ensure_auth_staging_parent,
)
from kiro_crew.kiro_prerequisite_probe import AuditWriter, _write_audit
from kiro_crew.kiro_prerequisite_process import ProcessRunner, _run_process
from kiro_crew.kiro_prerequisite_spawn_identity import identity_component_dropped
from kiro_crew.kiro_prerequisite_status import (
    PrerequisiteStatus,
    _platform_label,
    config_set_hints_for_launcher,
    legacy_idle_operation,
)

logger = logging.getLogger("kiro_crew.kiro_prerequisite")


# Floor between HOST probes driven by the status endpoint, however many callers
# ask. The first-run gate polls with ``refresh=1`` every 5s while it blocks the
# dashboard (it has to: readiness is latched at boot, so a latch read can never
# see the CLI the user just installed). Each browser tab polls independently and
# they cannot coordinate, so without a floor N open tabs mean N times the probes,
# each of them two ``kiro-cli`` spawns. Collapsing them costs a user-driven Check
# again nothing observable: the worst case is an answer a few seconds old, which
# is indistinguishable from a fresh one at human speed.
_FORCED_PROBE_FLOOR_SECS = 4.0
# Yield before the boot-time readiness probe so its kiro-cli spawn does not
# contend with the concurrent app-backend spawns on the boot-critical path.
_WARM_UP_DELAY_SECS = 3.0


_SETUP_COMPLETE_FILENAME = ".kiro_cli_setup_complete"


def _established_installation(data_home: Path) -> bool:
    """Recognize an existing Kiro Crew home when migrating onto the setup marker."""

    marker = data_home / _SETUP_COMPLETE_FILENAME
    if marker.is_file():
        return True
    for path in (data_home / "sessions", data_home / "history"):
        try:
            if path.is_file() and path.stat().st_size > 0:
                return True
            if path.is_dir():
                for child in path.rglob("*"):
                    if child.is_file() and child.stat().st_size > 0:
                        return True
        except OSError:
            continue
    return False


def _resolve_config_set_hints() -> dict[str, Any] | None:
    """The shell prefixes for ``kirocrew config set``, or None when no launcher resolves.

    Zero arguments, so a test replaces this one seam instead of the resolver behind it.
    ``agent`` is imported here because it loads the whole agent-spec graph, which the
    status path should not pay for until a snapshot asks.
    """
    from kiro_crew.agent import _resolve_kirocrew_bin

    try:
        launcher = _resolve_kirocrew_bin()
    except Exception:
        logger.debug("Could not resolve the kirocrew launcher for config hints", exc_info=True)
        return None
    return config_set_hints_for_launcher(launcher, sys.platform)


_OWNER_PREFIX = "kiro_crew.kiro_prerequisite_"


def _own_methods(cls: type) -> None:
    """Make each member *cls* binds from a collaborator read as a method of *cls*.

    A bound function annotates ``self`` so mypy checks its body as a method body;
    this drops that annotation from the runtime object and names the function after
    the class attribute (``KiroPrerequisiteService._probe``), so its
    ``inspect.signature`` and ``__qualname__`` are what the one-module class had. Only
    ``__module__`` moves: it names the collaborator that holds the body.
    """
    for name, member in vars(cls).items():
        function = getattr(member, "__func__", member)
        if getattr(function, "__module__", "").startswith(_OWNER_PREFIX):
            function.__annotations__.pop("self", None)
            function.__qualname__ = f"{cls.__qualname__}.{name}"


class KiroPrerequisiteService:
    """Single-gateway coordinator for prerequisite probes and setup operations."""

    # kiro_prerequisite_agent_specs.py
    _missing_agent_specs = staticmethod(_agent_specs._missing_agent_specs)
    _agent_spec_overlay = _agent_specs._agent_spec_overlay
    _repair_agent_specs = _agent_specs._repair_agent_specs
    _repair_auxiliary_specs = _agent_specs._repair_auxiliary_specs
    repair_agent_specs = _agent_specs.repair_agent_specs
    _probe_spec_acceptance = _agent_specs._probe_spec_acceptance
    _probe_kas_spec_loading = _agent_specs._probe_kas_spec_loading

    # kiro_prerequisite_identity_tracking.py
    current_identity_fingerprint = _identity_tracking.current_identity_fingerprint
    note_absence_swept = _identity_tracking.note_absence_swept
    _maybe_latch_interim_identity = _identity_tracking._maybe_latch_interim_identity
    identity_changed_since_probe = _identity_tracking.identity_changed_since_probe
    identity_changed_since_sessions = _identity_tracking.identity_changed_since_sessions
    note_sessions_reconciled = _identity_tracking.note_sessions_reconciled
    seed_sessions_baseline = _identity_tracking.seed_sessions_baseline
    read_spawn_identity = _identity_tracking.read_spawn_identity

    # kiro_prerequisite_probe.py
    signed_out_confirmed = _probing.signed_out_confirmed
    _stamp_probe = _probing._stamp_probe
    _probe = _probing._probe
    _probe_acp_support = _probing._probe_acp_support
    update_cli = _probing.update_cli
    _audited_probe = _probing._audited_probe
    _run_auth_command = _probing._run_auth_command
    _audited_identity_probe = _probing._audited_identity_probe
    _set_terminal_audit = _probing._set_terminal_audit

    def __init__(
        self,
        *,
        platform_name: str | None = None,
        environ: MutableMapping[str, str] | None = None,
        home: Path | None = None,
        data_home: Path | None = None,
        process_runner: ProcessRunner | None = None,
        audit_writer: AuditWriter | None = None,
        clock: Callable[[], float] | None = None,
        spec_lister: Callable[[], list[tuple[str, Path]]] | None = None,
        assume_ready: bool = False,
        warm_up_delay: float = _WARM_UP_DELAY_SECS,
    ) -> None:
        self._platform = platform_name or sys.platform
        self._environ = environ if environ is not None else os.environ
        self._home = home or Path.home()
        if data_home is not None:
            self._data_home = data_home
        elif home is not None:
            configured_home = self._environ.get("KIROCREW_HOME", "")
            self._data_home = (
                Path(configured_home).expanduser()
                if configured_home
                else self._home / ".kiro" / "crew"
            )
        else:
            self._data_home = config_dir()
        self._auth_staging_parent = _ensure_auth_staging_parent(self._home)
        self._setup_marker = self._data_home / _SETUP_COMPLETE_FILENAME
        (
            self._initial_override_path,
            self._initial_override_sha256,
        ) = register_process_start_override_attestation(
            platform_name=self._platform,
            environ=self._environ,
        )
        self._initial_setup_complete = _established_installation(self._data_home)
        auth_store_dirs = [
            mapping.source
            for mapping in _auth_store_mappings(self._platform, self._home, self._environ)
        ]
        # Kiro Crew's own secret home is always hidden from a probed CLI. The
        # credential-minimal probe additionally hides the identity stores; the
        # real-home callers must leave those visible — the readiness probe so a
        # CLI whose valid session lives outside the staged files (an external
        # auth helper resolved from the real home) can read its own credentials,
        # and device login so kiro-cli can WRITE its own credential store there.
        self._crew_hidden_dirs = tuple(
            dict.fromkeys(
                str(path)
                for path in (
                    self._data_home,
                    self._home / ".kiro" / "crew",
                    self._home / ".kirocrew",
                )
            )
        )
        self._hidden_probe_dirs = tuple(
            dict.fromkeys((*self._crew_hidden_dirs, *(str(path) for path in auth_store_dirs)))
        )
        self._probe_environment: dict[str, str] = {}
        self._run = process_runner or _run_process
        self._audit = audit_writer or _write_audit
        # Injected like the runner and clock above so a test states which specs
        # exist instead of inheriting whatever is in the developer's real agents
        # dir — otherwise spawn-count assertions vary by machine.
        self._spec_lister = spec_lister or _default_spec_lister
        self._clock = clock or time.monotonic
        # An enterprise build never assumes readiness: its login gate reads this
        # service's verdict, and an assumed one (`--test-mode`) would open the
        # gate with no kiro login at all.
        from kiro_crew.edition import is_enterprise

        self._assume_ready = assume_ready and not is_enterprise()
        assume_ready = self._assume_ready
        # `assume_ready` must hold from CONSTRUCTION, not from the first probe:
        # readiness is now a latch that `session_ready()` reads without probing,
        # so an offline/test gateway that never probes would otherwise report
        # not-ready and its blocking gates (e.g. /api/models) would 503.
        self._status = (
            PrerequisiteStatus(
                platform=_platform_label(self._platform),
                installed=True,
                authenticated=True,
                ready=True,
                initial_setup_complete=True,
            )
            if assume_ready
            else PrerequisiteStatus(
                platform=_platform_label(self._platform),
                initial_setup_complete=self._initial_setup_complete,
            )
        )
        self._warm_up_task: asyncio.Task[None] | None = None
        # Injectable so tests need not sleep out the real boot-contention delay.
        self._warm_up_delay = warm_up_delay
        self._probe_lock = asyncio.Lock()
        # Serializes spec repair. `operation_running` does NOT cover it: that
        # tracks `_task`, which only install/login set, so two concurrent owner
        # repair POSTs would both pass it. Without this lock the second rebuild
        # runs over the spec the first just wrote -- the exact
        # rebuild-over-an-existing-file case the main-spec gate exists to avoid,
        # which can drop a concurrent api_mcp_toggle write.
        self._repair_lock = asyncio.Lock()
        self._last_probe_at = 0.0
        self._has_probed = False
        self._viable_binary = ""
        # The failed-turn sign-in check (:meth:`signed_out_confirmed`) keeps its
        # own ``whoami`` verdict, apart from ``_status``: it must never rewrite
        # the readiness latch, or a network blip during a token refresh would
        # flip the whole gateway to not-ready. ``None`` = never asked.
        self._turn_auth_lock = asyncio.Lock()
        self._turn_auth_checked_at: float | None = None
        self._turn_auth_signed_out = False
        # Which account the store named when the latch was last written.
        # Comparing it against a fresh read is how an out-of-band `kiro-cli
        # logout` (or a switch to another account) is noticed at all: nothing
        # else on the host reports it, and the store's mtime is useless because
        # ordinary chat traffic rewrites the database every few seconds.
        self._probe_identity = _AUTH_FINGERPRINT_ABSENT
        # The retirement consumer's own baseline: which account the RUNNING
        # children were started under. Separate from _probe_identity because two
        # consumers sharing one baseline means the first to observe a change
        # advances it and the second never sees it (see
        # identity_changed_since_sessions). None = never reconciled, which reports
        # CHANGED: an unknown baseline must not resolve to "the children match".
        self._session_identity: str | None = None
        # Real-time cache bounding the store reads and their SEL audit events.
        self._identity_cache = _AUTH_FINGERPRINT_ABSENT
        self._identity_cache_at = 0.0
        # Whether the read behind _identity_cache proved nobody is signed in; see
        # identity_absence_is_definitive.
        self._identity_cache_absence_definitive = False
        # Unbroken runs of proven sign-out reads, numbered from 1, and the run a
        # complete sweep covered; see identity_absence_already_swept.
        self._identity_absence_run = 0
        self._identity_absence_run_swept = 0
        # Whether the relocation refusal has been logged. The relocated arm runs
        # on every identity poll, so the diagnostic logs once per service rather
        # than flooding; see current_identity_fingerprint.
        self._relocation_logged = False
        # Sticky: some fresh read observed a DIFFERENT signed-in account than
        # the recorded baseline. Forces the retirement sweep at the next turn
        # gate even after the store switches back (the A->B->A round trip a
        # bare baseline comparison cannot see), and clears only when a sweep
        # COMPLETES (note_sessions_reconciled). Latched exclusively by
        # _maybe_latch_interim_identity, which refuses component-loss
        # observations so a transient unreadable store can never arm it.
        self._interim_identity_observed = False
        # Monotonic count of qualifying interim observations. Every arm-worthy
        # read bumps it -- including reads that land while the latch is already
        # armed -- so a sweep can tell observations it covered from ones that
        # arrived AFTER its own initiating read. note_sessions_reconciled
        # clears the latch only when no observation postdates the generation
        # its caller captured at the gate read; a newer observation names an
        # account that sweep never compared against, and clearing it would
        # erase the only record that the account existed (a child spawned
        # under it may be unstamped and otherwise invisible).
        self._interim_identity_gen = 0

    @property
    def initial_setup_complete(self) -> bool:
        """Whether this data home has already completed first-run setup.

        Derived from the data home at construction, so it is available BEFORE
        any CLI probe runs. Callers use it to classify a returning user without
        waiting on the probe, whose cold path spawns two sandboxed subprocesses
        (``--version``, then ``whoami``) and takes long enough for the dashboard
        to flash first-run setup chrome at someone who has used the app for
        months.
        """

        return self._initial_setup_complete

    @staticmethod
    def _kas_bundle_route() -> bool:
        """Whether ``agent.kas_bundle`` is on, read from the config at call time."""
        from kiro_crew.config import KiroCrewConfig

        try:
            return bool(KiroCrewConfig.load().agent.kas_bundle)
        except Exception:
            return False

    async def record_independent_backend_setup(self, backend: str) -> bool:
        """Persist first-run completion after the configured harness is usable.

        A browser-local completion bit cannot admit a later browser or a
        non-owner dashboard user.  Re-check the server's selectable backend,
        installation, and sandbox facts before writing the owner-only marker.
        A probe marked UNKNOWN is not proof of installation or sign-in.

        KAS is the one backend that completes here without being an independent
        harness: it is kiro-cli's relay, so it needs a kiro-cli that speaks ACP
        (``acp_supported``) rather than the independent-setup bypass, and it does
        NOT need a kiro-cli login (its own account gate replaces that). This is
        the SAME rule the onboarding gate admits KAS on, kept in lock-step here
        so a KAS first run actually records the marker instead of leaving
        ``initial_setup_complete`` false forever.

        On the BUNDLE route (``agent.kas_bundle`` on) KAS runs Crew's own installed
        engine and never kiro-cli, so none of the kiro-cli checks apply: the
        prerequisite is the installed engine (``installed_bundle()``), and a host
        with no kiro-cli at all completes here once ``kirocrew kas install`` has
        run. The install probe below answers from the same reader on that route.
        """
        from kiro_crew.acp_backends import selectable_backend_values
        from kiro_crew.agent_sdk.backend_install import INSTALLED, probe_backend
        from kiro_crew.agent_sdk.backends import (
            ACP_BACKEND_KAS,
            ACP_BACKENDS_INDEPENDENT_SETUP,
        )

        if backend == ACP_BACKEND_KAS and self._kas_bundle_route():
            from kiro_crew.agent_sdk import kas_bundle

            if await asyncio.to_thread(kas_bundle.installed_bundle) is None:
                return False
        elif backend == ACP_BACKEND_KAS:
            # KAS runs ON kiro-cli, so it is deliberately absent from
            # ACP_BACKENDS_INDEPENDENT_SETUP. Its prerequisite is a kiro-cli new
            # enough to expose the ``acp`` subcommand; only a clean "too old"
            # verdict (acp_supported False) blocks it, matching the client.
            if self._status.acp_supported is False:
                return False
            # The marker is permanent, so ask the binary again rather than trust
            # a verdict from an earlier probe: kiro-cli may have been replaced by
            # an older build since. A too-old answer also updates the served
            # status, so the gate shows its update card.
            if not self._viable_binary and not self._assume_ready:
                # No candidate is resolved yet: the boot probe may still be in its
                # warm-up delay, and ``acp_supported`` reads True until a probe
                # answers. Run the probe now so the version check covers the binary
                # a KAS session launches; with no runnable kiro-cli, KAS cannot start.
                await self._probe(force=True)
                if not self._viable_binary or self._status.acp_supported is False:
                    return False
            elif self._viable_binary and not await self._probe_acp_support(self._viable_binary):
                self._status = replace(self._status, acp_supported=False, ready=False)
                return False
        elif backend not in ACP_BACKENDS_INDEPENDENT_SETUP:
            return False
        if backend not in selectable_backend_values():
            return False
        state = await asyncio.to_thread(probe_backend, backend)
        if state.installed != INSTALLED or state.restart_required:
            return False
        status = await self._dashboard_snapshot()
        kas_load_rejected: list[str] = []
        if backend == ACP_BACKEND_KAS and not status.get("authenticated"):
            # No Kiro CLI validation ran, so re-check KAS's local loading contract
            # now: the probe's answer may predate an edit to the spec.
            kas_load_rejected, _ = await self._probe_kas_spec_loading()
        if backend == ACP_BACKEND_KAS and (
            status.get("missing_agent_specs")
            or status.get("rejected_agent_specs")
            or kas_load_rejected
        ):
            # KAS loads Kiro Crew's agent specs from kiro-cli's agents dir, so a
            # missing or rejected spec must stay on the repair card, not finish.
            return False
        if (
            status["sandbox_unavailable"]
            or not (status["sandbox_backend_available"] or status.get("unsandboxed_exec_permitted"))
            or backend in status["sandbox_blocked_backends"]
        ):
            return False
        await asyncio.to_thread(self._mark_setup_complete)
        # Snapshots are served from ``_status``, and this path (unlike the probe,
        # which rebuilds ``_status`` right after its own marker write) runs no
        # probe. Without this a non-owner, whose poll cannot force a probe, keeps
        # reading ``initial_setup_complete: false`` until a restart.
        self._status = replace(self._status, initial_setup_complete=True)
        return True

    def _snapshot_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = asdict(self._status)
        # See _LEGACY_IDLE_OPERATION: a pre-upgrade tab crashes without this key.
        result["operation"] = legacy_idle_operation()
        return result

    async def _dashboard_snapshot(self) -> dict[str, Any]:
        """Apply host facts to every dashboard response, including repair/update."""
        result = await self._agent_spec_overlay(self._snapshot_dict())
        # Resolved per snapshot, off-loop: the resolver stats files and walks PATH, and
        # it re-validates its own answer because a gateway outlives the install it
        # started from. Only this owner-side snapshot carries it; the handler's
        # non-owner payload is built field by field and never copies it.
        hints = await asyncio.to_thread(_resolve_config_set_hints)
        if hints is not None:
            result["config_set_hints"] = hints
        # A missing Kiro candidate cannot produce a sandbox_unavailable verdict,
        # and Kiro's delegated sandbox does not establish a Crew OS backend for
        # other harnesses. Use the runtime detector independently, off-loop because
        # its cold/transient path can probe the host. Test mode never probes it.
        if self._assume_ready:
            result["sandbox_backend_available"] = True
            return result

        def sandbox_facts() -> tuple[bool, bool, list[str]]:
            from kiro_crew.agent_sdk.backends import registered_backends
            from kiro_crew.agent_sdk.tool_gate import is_enforced

            available = sandbox.detect_backend() != "none"
            # Whether a session could START with no OS backend: the platform
            # default (native Windows) or an operator opt-in, the same verdict
            # wrap_argv gates a backend-less spawn on. A non-enforced harness
            # needs no Crew OS mask, so this lets it finish setup on such a host.
            # A governed isolation floor overrides either grant, just as it
            # does in wrap_argv's no-backend branch.
            unsandboxed_permitted = bool(
                sandbox.unsandboxed_exec_permitted_by()
            ) and not sandbox._floor_mandates_sandbox(sandbox._governance_sandbox_floor())
            mask_applies = sandbox.credential_mask_applies(sandbox.configured_sandbox_mode())
            blocked = (
                []
                if mask_applies
                else sorted(backend for backend in registered_backends() if is_enforced(backend))
            )
            return available, unsandboxed_permitted, blocked

        (
            result["sandbox_backend_available"],
            result["unsandboxed_exec_permitted"],
            result["sandbox_blocked_backends"],
        ) = await asyncio.to_thread(sandbox_facts)
        return result

    def warm_up(self) -> asyncio.Task[None] | None:
        """Resolve readiness in the background shortly after gateway start.

        The probe spawns two ``kiro-cli`` subprocesses, so it yields
        ``_WARM_UP_DELAY_SECS`` first rather than running inline: racing those
        spawns against the concurrent app-backend spawns measurably lengthened and
        destabilized boot (~2.7s → 2.8-5.6s on one real home). Running it at all
        means the answer is usually already there when a session is first started.

        Returns the task so callers (and tests) can await it; startup itself does
        NOT await it, and failures are contained. A warm-up is strictly an
        optimization: the dashboard renders without waiting for it (readiness
        gates nothing in the UI), so it must never delay boot.
        """

        if self._warm_up_task is not None:
            return self._warm_up_task

        async def _warm() -> None:
            try:
                if self._warm_up_delay > 0:
                    await asyncio.sleep(self._warm_up_delay)
                await self._probe()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The lazy probe path stays authoritative and will retry, so a
                # failed warm-up must not fail gateway startup.
                logger.warning("Kiro readiness warm-up failed", exc_info=True)

        self._warm_up_task = asyncio.create_task(_warm())
        return self._warm_up_task

    async def snapshot(
        self,
        *,
        force: bool = False,
        coalesce: bool = False,
    ) -> dict[str, Any]:
        """Return the latched status, probing only on an explicit ``force``.

        Probing is boot-and-explicit-action only (see :meth:`session_ready`), so
        an ordinary poll reads latched CLI state. The independent OS sandbox
        capability check uses the shared detector/cache. ``force=True``
        (the gate's Check again, and its blocking-state auto-poll) is the
        supported way to re-probe on demand.

        ``coalesce=True`` additionally floors the probe at
        :data:`_FORCED_PROBE_FLOOR_SECS`, and serves the latched answer without
        waiting when a probe is already in flight. It is for the MACHINE-driven
        force — the
        blocking gate's auto-poll, which every open tab runs independently — so N
        tabs collapse onto one probe per interval instead of multiplying the
        spawns. A human-driven force (Check again) passes ``coalesce=False`` and
        always probes: a button that silently returns a cached answer is a button
        that looks broken. The floor is likewise NOT applied in
        :meth:`verified_ready`, whose callers act irreversibly and carry their own
        freshness bound.
        """

        if force and coalesce and self._clock() - self._last_probe_at < _FORCED_PROBE_FLOOR_SECS:
            force = False
        if force and coalesce and self._has_probed and self._probe_lock.locked():
            # A probe is already in flight and a latched answer exists. The
            # machine poll must not queue behind ``_probe_lock`` here: the
            # identity probe can legitimately run for
            # _IDENTITY_PROBE_TIMEOUT_SECS (an IdP token refresh), and every
            # open tab polls every few seconds, so queueing would hang each
            # poll for the probe's whole remaining duration — a frozen gate,
            # exactly what the floor exists to prevent. Serving the latched
            # answer keeps the pane live; the in-flight probe refreshes it for
            # the next poll. A human Check again keeps queueing: it promised a
            # fresh answer, and it runs its own probe after the winner.
            force = False
        if force:
            # ``force=not coalesce`` is what actually coalesces a BURST. The floor
            # above is read outside ``_probe_lock``, so several tabs polling in the
            # same instant all see the same stale ``_last_probe_at``, all pass it,
            # and would each run a full probe in turn behind the lock — N tabs, N
            # probes, exactly what the floor exists to prevent. Handing the machine
            # poll ``force=False`` lets ``_probe``'s own cache recheck, which runs
            # INSIDE the lock after the winner refreshed the timestamp, drop the
            # queued callers. A human Check again keeps ``force=True`` and still
            # bypasses that cache, so the button never returns a stale answer.
            await self._probe(force=not coalesce)
        elif not self._has_probed:
            # Nothing has resolved yet (warm-up still pending or it failed).
            # One probe here is the boot probe, just arriving late.
            await self._probe()
        elif await self.identity_changed_since_probe():
            # The store now names a different account (or none) than the latch
            # describes. This is the one condition under which an ORDINARY poll
            # re-probes: without it the card keeps reporting the account the user
            # signed out of until they happen to press Check again, and reports
            # "not signed in" after a terminal sign-in for just as long. It stays
            # cheap because the trigger is a local read, not a spawn, and it
            # cannot loop -- the probe stamps the new identity, so the next poll
            # compares equal.
            await self._probe(force=True)
        result = await self._dashboard_snapshot()

        # The repair arm deliberately does NOT live here. This is an ``add_get``
        # route, and both dashboard barriers are method-scoped (csrf_middleware
        # skips check_origin for GET; sel_audit_middleware logs only
        # POST/PUT/DELETE/PATCH), so a write reached from a status read would be
        # cross-site triggerable and unaudited. It is a POST route instead:
        # ``repair_agent_specs`` / ``POST /api/kiro-prerequisite/repair-specs``.
        return result

    def mark_signed_out(self) -> None:
        """Latch readiness to signed-out after an observed ACP auth failure.

        The ACP attempt — not a probe — is what discovers a mid-session logout
        now that probing is boot-and-explicit-action only. Recording it here is
        what keeps the fail-closed gates (poll-driven spawn sites + destructive
        reruns) honest with no timer re-probe and no subprocess.

        Only ever *narrows* readiness (never grants it), and never touches
        ``initial_setup_complete``, so a returning user is not demoted to
        first-run setup. A running install/login operation owns the status, so
        this defers to it rather than racing its outcome.
        """

        if self._assume_ready:
            return
        if not self._status.ready and not self._status.authenticated:
            return
        self._status = replace(self._status, authenticated=False, ready=False)
        # Force the next explicit probe (Refresh / login) to actually run rather
        # than being served by the short cache off this synthetic transition.
        self._last_probe_at = 0.0
        logger.info("Kiro readiness latched to signed-out after an ACP auth failure")

    @property
    def identity_absence_is_definitive(self) -> bool:
        """Whether the last identity read PROVED that no account is signed in.

        An empty fingerprint has two causes this distinguishes. Nobody is signed in:
        the store holds no credential (or has no file at all), no API key is set in
        the environment or a cleanly read ``.env``, and the Crew vault answered with no
        identity. Or the reader cannot tell who is: a social login whose row carries
        no stable claim, an unreadable or env-relocated store, a store or vault it may
        not look at, an audit that could not be recorded, a ``.env`` or a vault that
        would not read. Every source reports those failures as ``""`` as well, so this
        is True only when each of them answered. It describes the value the last
        :meth:`current_identity_fingerprint` read returned (cached reads reuse it).
        A caller with no person behind it reads this to tell a sign-out, after which
        a child still holding the old credential must be retired, from a store it
        merely cannot identify, where retiring on every call would recycle
        healthy sessions forever.
        """

        return self._identity_cache_absence_definitive

    @property
    def identity_absence_run(self) -> int:
        """Which unbroken run of proven sign-out reads the last read belongs to.

        A gate captures it with its read and hands it to :meth:`note_absence_swept`
        once its sweep completes, so a read that lands while the sweep runs cannot
        stand in for the one that started it.
        """

        return self._identity_absence_run

    @property
    def identity_absence_already_swept(self) -> bool:
        """Whether a complete sweep already covered the sign-out the last read proved.

        A sign-out is never reconciled (:meth:`note_sessions_reconciled` takes only a
        real account), so every gate call under it sweeps again. After one complete
        sweep nothing that held the old account is left. A session started since holds
        no account, or brings its own ``KIRO_API_KEY`` (a cron job's ``env`` block),
        which no sweep spares, so sweeping again would only cancel its sub-agents.
        True while every read since that sweep has proved the same sign-out: any other
        reading starts a new run.
        """

        return (
            self._identity_cache_absence_definitive
            and self._identity_absence_run == self._identity_absence_run_swept
        )

    def identity_component_dropped(self, live: str) -> bool:
        """Whether a credential source present in the baseline is now EMPTY in *live*.

        The retire path spares an idle parent's live children only while its
        credential is still valid. ``bool(live)`` alone cannot tell a switch
        between two real accounts from a partial credential LOSS: an external
        ``kiro-cli logout`` while the Crew vault stays populated leaves the
        combined fingerprint nonempty (``_combine_identity_fingerprints`` appends
        each present source), so the children would be spared and keep running on
        the logged-out store's in-memory credential. A dropped component is that
        loss, and the sweep must then retire the parent and cancel the children,
        exactly as the sign-out path does.

        Pure and side-effect-free: it compares the *live* fingerprint the caller
        already read against the retire baseline ``self._session_identity`` per
        component (store, API key, Crew vault), so it adds no second store read.
        A component that is PRESENT in the baseline and EMPTY in *live* dropped; a
        component that merely appeared or changed is a switch, not a loss, and does
        not count. An unset baseline answers False -- nothing was there to drop,
        and that case already sweeps once on its own.

        This catches the simple logout where the baseline is current. The sweep
        ALSO checks each holder's own spawn stamp (see
        ``_identity_change_sweep``), because the baseline advances only on a
        complete sweep and so lags a source added-then-removed inside an
        incomplete one.
        """
        if self._session_identity is None:
            return False
        return identity_component_dropped(self._session_identity, live)

    @property
    def identity_observation_generation(self) -> int:
        """Count of qualifying interim-identity observations so far.

        Captured by the turn gate immediately after its
        :meth:`identity_changed_since_sessions` read and handed back to
        :meth:`note_sessions_reconciled`, so the reconcile can tell
        observations its sweep covered from ones that arrived while the sweep
        ran. An instance-level "generation at last gate read" would race:
        two overlapping turn gates would overwrite each other's capture, and
        the later writer's value could mask an observation the earlier
        sweep never saw.
        """

        return self._interim_identity_gen

    async def verified_ready(self, *, max_age_secs: float) -> bool:
        """Return readiness backed by a probe no older than *max_age_secs*.

        For callers that act IRREVERSIBLY on the answer — the destructive reruns
        (which rewrite persisted history) and the poll-driven ``kiro-cli`` spawn
        sites (which would open an interactive browser login) — the latch is not
        a sufficient authority in EITHER direction. It is written at boot and
        narrowed only when a chat turn observes an auth failure, so an external
        logout with no chat turn in between would leave it ``ready=True``
        indefinitely.

        Re-probing here is bounded: only these paths call it, never the message
        hot path, and ``_PROBE_CACHE_SECS`` collapses a burst of callers onto one
        probe. A running install/login operation owns the status, so defer to its
        current value rather than racing it.
        """

        if self._assume_ready:
            return bool(self._status.ready)
        if not self._has_probed or self._clock() - self._last_probe_at >= max_age_secs:
            try:
                await self._probe(force=True)
            except Exception:
                # A probe that cannot run is not evidence of readiness. Fail
                # closed: these callers must never act on a guess.
                logger.warning("Kiro verification probe failed; denying", exc_info=True)
                return False
        return bool(self._status.ready)

    @property
    def authenticated(self) -> bool:
        """The latched sign-in verdict. Never probes.

        The enterprise login gate reads this on every request, so it must stay a
        field read. False before the first probe answers, which is what keeps an
        enterprise gateway closed until a probe has seen a login.
        """

        return bool(self._status.authenticated)

    async def login_verified(self, *, max_age_secs: float) -> bool:
        """Return the sign-in verdict, re-probing when it may be stale.

        For the enterprise login gate's background check (docs/request-for-change/
        rfc-crew-enterprise-edition.md, "Enterprise gate"), which must notice an
        expired or lost login with no chat turn to surface it. The probe re-runs
        when the last one is older than *max_age_secs*, or when the identity store
        now names a different account (or none) than the latch: that read is
        local, so a sign-out or a fresh sign-in shows up without waiting out the
        age. This is the same ``whoami`` probe the setup gate uses, not a second
        login check. Fails closed: a probe that cannot run answers False.
        """

        if self._assume_ready:
            return bool(self._status.authenticated)
        try:
            stale = not self._has_probed or self._clock() - self._last_probe_at >= max_age_secs
            if stale or await self.identity_changed_since_probe():
                await self._probe(force=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Kiro login check failed; treating as signed out", exc_info=True)
            return False
        return bool(self._status.authenticated)

    async def boot_login_status(self) -> PrerequisiteStatus:
        """Probe ``--version`` and ``whoami`` only, and return the latched status.

        For the enterprise boot check, which runs before the gateway binds: the
        spec and ``acp`` checks of a full probe run after the bind instead (a
        probe that reached ``whoami`` is left not done, so the next read runs the
        full one). ``authenticated`` is the verdict; it fails closed.
        """

        return await self._probe(force=True, login_only=True)

    async def session_ready(self) -> bool:
        """Return the latched readiness. Never probes; never blocks a turn.

        Readiness is resolved ONCE at gateway start (:meth:`warm_up`) and then
        refreshed only by an explicit user action — the dashboard's Refresh, or
        an install/login operation. There is deliberately no timer re-probe and
        no probe on the send path: a signed-out CLI is discovered by the ACP
        attempt itself, which raises ``AcpAuthRequired`` and surfaces an
        actionable sign-in error in the chat transcript. The one exception runs
        AFTER a turn has failed, not before it: :meth:`signed_out_confirmed`
        asks ``whoami`` when a prompt fails with a bare connection error, and it
        never writes this latch.

        Because this value can therefore be arbitrarily stale, turn-starting
        callers treat it as ADVISORY and let the real ACP attempt be the
        authority (see ``dashboard/kiro_readiness.py``). Poll-driven
        ``kiro-cli`` spawn sites still gate on it — they have no turn to carry
        the failure, and an unauthenticated spawn opens a browser window on
        every poll.
        """

        return bool(self._status.ready)

    def _mark_setup_complete(self) -> None:
        if self._initial_setup_complete:
            return
        # restrict_to_owner=True locks the staged temp file down before any
        # content reaches it (0o600 on POSIX, an owner-only DACL on Windows), so
        # the published marker is owner-only from the instant the rename makes
        # it visible. The default restrict_on_error="raise" is what makes that
        # unconditional: a lockdown that fails aborts before the rename instead
        # of publishing the marker under the parent's inherited DACL.
        atomic_write(
            self._setup_marker,
            "complete\n",
            fsync=True,
            restrict_to_owner=True,
        )
        self._initial_setup_complete = True

    async def close(self) -> None:
        global _turn_auth_service
        if _turn_auth_service is not None and _turn_auth_service() is self:
            _turn_auth_service = None
        tasks: list[asyncio.Task[Any]] = []
        for task in (self._warm_up_task,):
            if task is not None and not task.done() and task not in tasks:
                task.cancel()
                tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


_own_methods(KiroPrerequisiteService)


# The gateway's service, for the ACP layer's failed-prompt check. Set by the
# dashboard at start (:func:`register_turn_auth_service`) and cleared by that
# service's ``close``. ``None`` everywhere else (the CLI, tests that do not
# register one), where :func:`kiro_signed_out_confirmed` answers False and a
# failed prompt keeps its ordinary retry. A weak reference, so a gateway that
# is dropped without ``close`` does not keep its service alive here.
_turn_auth_service: weakref.ref[KiroPrerequisiteService] | None = None


def register_turn_auth_service(service: KiroPrerequisiteService | None) -> None:
    """Name the service whose ``whoami`` verdict failed prompts may consult."""
    global _turn_auth_service
    _turn_auth_service = weakref.ref(service) if service is not None else None


async def kiro_signed_out_confirmed() -> bool:
    """Whether the registered service confirms kiro-cli is signed out.

    Never raises: any failure answers False, which leaves the caller's own
    handling unchanged.
    """
    ref = _turn_auth_service
    service = ref() if ref is not None else None
    if service is None:
        return False
    try:
        return await service.signed_out_confirmed()
    except Exception:
        logger.debug("Kiro sign-in check failed", exc_info=True)
        return False
