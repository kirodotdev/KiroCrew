"""The audited kiro-cli probes, the failed-turn sign-in check and the self-update.

Bound into :class:`KiroPrerequisiteService`. Reads ``self._run``, ``self._audit``,
``self._clock``, ``self._platform``, ``self._home``, ``self._environ``,
``self._data_home``, ``self._assume_ready``, ``self._initial_setup_complete``,
``self._crew_hidden_dirs`` and ``self._hidden_probe_dirs``; writes ``self._status``,
``self._viable_binary``, ``self._probe_environment``, ``self._last_probe_at``,
``self._has_probed``, ``self._probe_identity``, ``self._turn_auth_checked_at`` and
``self._turn_auth_signed_out``, under ``self._probe_lock`` / ``self._turn_auth_lock``.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from kiro_crew.config.loader import CRED_KIRO_API_KEY, read_env_file_credential
from kiro_crew.kiro_cli import is_bundled_kiro_cli
from kiro_crew.kiro_prerequisite_candidate import (
    _IDENTITY_PROXY_ENV_KEYS,
    _allowlisted_env,
    _identity_probe_env,
    _is_runnable_executable,
    _probe_filesystem_state,
)
from kiro_crew.kiro_prerequisite_identity import _AUTH_FINGERPRINT_ABSENT
from kiro_crew.kiro_prerequisite_identity_store import _prepare_auth_workspace
from kiro_crew.kiro_prerequisite_process import _UNVERIFIED_SANDBOX_MODE, ProcessResult
from kiro_crew.kiro_prerequisite_status import (
    BUNDLED_CLI_UPDATE_REFUSAL,
    KIRO_CLI_UPDATE_COMMAND,
    PrerequisiteStatus,
    _platform_label,
    _sanitize_detail,
    login_commands_for,
)
from kiro_crew.security import redact

if TYPE_CHECKING:
    from kiro_crew.kiro_prerequisite_service import KiroPrerequisiteService

logger = logging.getLogger("kiro_crew.kiro_prerequisite")


# The subcommand Kiro Crew launches every ACP session through (see
# ``acp.client.KIRO_CLI_SUBCMD`` / ``acp.runtime.KIRO_CLI_SUBCMD``). Probed by
# name so the readiness check learns of a rename the same way a spawn would fail.
_ACP_SUBCOMMAND = "acp"
# How long the self-update is allowed to run before the probe gives up on it.
# ``kiro-cli update`` downloads and swaps a binary, so it is far slower than the
# read-only probes; sized to match the auto-update path's own 120s budget in
# ``slack/gateway_runtime/update_apply.py`` rather than the 10s probe ceiling.
_UPDATE_TIMEOUT_SECS = 120


_PROBE_TIMEOUT_SECS = 10


# Substrings that identify an "unknown subcommand" rejection in the captured
# output of an ``acp --help`` probe. A kiro-cli that HAS the ``acp`` subcommand
# exits 0 and prints its help; one too old to have it exits nonzero and (being a
# clap CLI) prints one of these. Matched case-insensitively.
#
# This is deliberately a POSITIVE match on the rejection wording rather than
# "the probe printed something" or "exit was nonzero", for the same reason the
# spec probe matches its rejection marker: a probe that failed for an unrelated
# reason (a sandbox denial, a hung binary) also produces a nonzero exit and
# captured text, and reporting THAT as "your CLI is too old" would push the user
# to run an update that cannot fix it. Anything unrecognized is therefore treated
# as supported — the module's rule is to report nothing rather than block a
# working install behind a card it cannot clear (see :meth:`_probe_acp_support`).
_ACP_UNSUPPORTED_MARKERS = (
    "unrecognized subcommand",
    "unknown subcommand",
    "unrecognized command",
    "unknown command",
    "invalid subcommand",
    "no such subcommand",
)
# The identity probe's own budget, deliberately separate from
# _PROBE_TIMEOUT_SECS. ``whoami`` is not a local read: when the cached token has
# expired, Kiro CLI refreshes an OIDC token against the organization's IdP —
# for IAM Identity Center users that network round trip measures 12–20 seconds,
# so a probe killed at the shared 10-second ceiling reports a fully signed-in
# CLI as ``authenticated=False`` and wedges the first-run gate. The budget is
# NOT applied to ``--version``: that probe is the FIRST execution of an unknown
# candidate and must stay on a short leash, so a genuinely missing or hung
# binary is still reported quickly rather than blocking the gate for the full
# identity budget. Sized to cover the observed refresh with headroom, while the
# in-flight latch in :meth:`KiroPrerequisiteService.snapshot` keeps the gate's
# machine polls answering from cached state instead of queueing behind a probe
# this long.
_IDENTITY_PROBE_TIMEOUT_SECS = 30
_PROBE_CACHE_SECS = 2.0
# How old a ``whoami`` verdict may be and still turn a failed prompt into a
# sign-in error (:meth:`KiroPrerequisiteService.signed_out_confirmed`). Short, so
# a sign-in from a terminal is seen on the next failure; the same bound the
# destructive reruns use (``dashboard/kiro_readiness._VERIFY_MAX_AGE_SECS``).
_TURN_AUTH_MAX_AGE_SECS = 30.0
# What ``kiro-cli whoami`` prints when there is no sign-in (measured on kiro-cli
# 2.31.0: "Not logged in", exit 1). The launcher's refusal says the same words.
_WHOAMI_SIGNED_OUT_MARKER = "not logged in"


_KIRO_AUTH_SANDBOX_MODE = "standard"


def _whoami_says_signed_out(result: ProcessResult) -> bool:
    """Whether a ``whoami`` run answered, by itself, that nobody is signed in.

    Needs all of: it exited (no timeout, no spawn failure), non-zero, and printed
    the signed-out banner. Anything less is "unknown", never "signed out".
    """
    return (
        not result.ok
        and not result.timed_out
        and result.returncode not in (None, 0)
        and _WHOAMI_SIGNED_OUT_MARKER in result.output.casefold()
    )


AuditWriter = Callable[..., Awaitable[None]]


def _terminal_audit_detail(result: ProcessResult, succeeded: bool) -> str:
    """The terminal audit event's error label for a run that finished or timed out.

    *succeeded* is the verdict the caller already reached, not ``result.ok``, so
    the label always follows that verdict and can never contradict the
    ``outcome`` recorded beside it. The update path spells the verdict
    ``update.ok and not error``, where ``error`` is itself set from this same
    run, so the two agree there.
    """

    if succeeded:
        return ""
    if result.timed_out:
        return "timeout"
    return "nonzero exit"


#: Longest ``probe_error`` served. The value is a diagnostic line for a status
#: screen, not a log: the tail of a failing ``--version`` is where the CLI names
#: its own complaint, and anything longer is a stack trace the screen cannot use.
_PROBE_ERROR_MAX_CHARS = 400


def _probe_failure_text(result: ProcessResult | None) -> str:
    """What a failed version probe has to say for itself, bounded.

    The typed failures (sandbox refusal, timeout) are reported through their own
    fields and never reach here. What remains is a probe that RAN and did not
    verify. The CLI's own output is the text: that is where a launcher wrapper
    or a broken install names its complaint, and it is what the operator can act
    on. The spawn layer's ``error`` is only a fallback for a probe that printed
    nothing -- for an ordinary non-zero exit it is just the generic exit code,
    which ``probe_status`` already carries and the gate already renders as
    ``(exit N)``, so appending it here would say the exit code twice. Empty
    when there is nothing to say (no probe ran, or it passed).
    """

    if result is None or result.ok:
        return ""
    text = (result.output or "").strip() or (result.error or "").strip()
    # The probe's stdout/stderr is untrusted text that can echo a token or an
    # authority-bearing URL (a launcher wrapper printing the environment it
    # sees), and this string travels to the status payload and the setup
    # screen, so it is redacted BEFORE the cut: truncating first could leave
    # the recognisable half of a secret in the kept tail.
    text = redact(text)
    if len(text) > _PROBE_ERROR_MAX_CHARS:
        text = text[-_PROBE_ERROR_MAX_CHARS:]
    return text


async def _write_audit(
    *,
    action: str,
    outcome: str,
    caller: str,
    error: str = "",
    critical: bool = False,
) -> None:
    from kiro_crew.sel import sel

    def _write() -> None:
        sel().log_tool_invocation(
            session_key="dashboard:kiro-prerequisite",
            source="dashboard",
            tool_name=f"kiro_prerequisite_{action}",
            tool_kind="system_setup",
            outcome=outcome,
            error=error,
            metadata={"caller": caller[:100]},
            critical=critical,
        )

    await asyncio.to_thread(_write)


async def signed_out_confirmed(self: KiroPrerequisiteService) -> bool:
    """Return True only when a recent ``whoami`` said "not logged in".

    For a prompt that failed with a connection-class error before any output.
    A signed-out bundled ``kiro-cli-chat`` answers ``initialize`` and
    ``session/new`` normally and fails ``session/prompt`` with a bare
    ``dispatch failure``, with nothing on stderr, so the ACP layer alone
    cannot tell it from a network outage. This is the tie-breaker, and the
    one probe the send path makes (see :meth:`session_ready`): it runs only
    after a turn has already failed, never before one.

    Runs ``whoami`` alone, on the binary the boot probe verified, and keeps
    the verdict apart from ``_status``, so the readiness latch is untouched.
    The verdict is reused for :data:`_TURN_AUTH_MAX_AGE_SECS`, so a sign-in
    from a terminal is seen on a later failure, and the lock makes a burst
    of failing turns wait on one ``whoami``. No verified binary, a
    ``whoami`` that cannot run, times out or prints anything else answers
    False: the caller then keeps its ordinary retry.
    """

    if self._assume_ready:
        return False
    async with self._turn_auth_lock:
        checked_at = self._turn_auth_checked_at
        if checked_at is not None and self._clock() - checked_at < _TURN_AUTH_MAX_AGE_SECS:
            return self._turn_auth_signed_out
        executable = self._viable_binary
        if not executable:
            return False
        try:
            result = await self._audited_identity_probe(executable, isolate_home=False)
            verdict = _whoami_says_signed_out(result)
        except Exception:
            logger.warning("Kiro sign-in check failed; keeping the retry", exc_info=True)
            verdict = False
        self._turn_auth_signed_out = verdict
        self._turn_auth_checked_at = self._clock()
        return verdict


def _stamp_probe(self: KiroPrerequisiteService, identity: str) -> None:
    """Record that the latch was just written, and for which account.

    Every latch write in :meth:`_probe` goes through here so none can record
    a verdict without the identity it belongs to -- a latch stamped with no
    identity would read as "account changed" on the very next comparison and
    retire healthy sessions forever.

    Deliberately does NOT touch the retirement baseline. Seeding it here would
    look like it anchors that baseline to the identity the running children
    loaded, but the probe is delayed a few seconds after boot and a session can
    be spawned eagerly before it, so a logout in that gap would be seeded as the
    starting point and the pre-logout child would never be retired. That
    baseline is advanced only by :meth:`note_sessions_reconciled`, after a
    sweep -- and an unset one reports changed rather than assuming a match.
    """

    self._last_probe_at = self._clock()
    self._has_probed = True
    self._probe_identity = identity


async def _probe(self: KiroPrerequisiteService, *, force: bool = False) -> PrerequisiteStatus:
    async with self._probe_lock:
        # Read the identity BEFORE running whoami, and stamp that value on
        # whatever verdict this probe reaches. The order matters: if the store
        # changes between this read and whoami, the latch records the OLDER
        # identity, so the next comparison sees a change and re-probes -- one
        # wasted probe. Reading afterwards would stamp the NEW identity onto a
        # verdict describing the OLD one, which hides the change instead.
        probe_identity = (
            _AUTH_FINGERPRINT_ABSENT
            if self._assume_ready
            else await self.current_identity_fingerprint(allow_cached=False)
        )
        if self._assume_ready:
            self._status = PrerequisiteStatus(
                platform=_platform_label(self._platform),
                installed=True,
                authenticated=True,
                ready=True,
                initial_setup_complete=True,
            )
            self._stamp_probe(probe_identity)
            return self._status
        now = self._clock()
        if self._has_probed and not force and now - self._last_probe_at < _PROBE_CACHE_SECS:
            return self._status
        self._viable_binary = ""
        (
            self._probe_environment,
            candidates,
        ) = await asyncio.to_thread(
            _probe_filesystem_state,
            self._platform,
            self._home,
            self._environ,
        )
        # ACP resolves the first executable candidate. Probe that exact
        # candidate instead of skipping a broken entry and approving a
        # different binary than the session launcher will use.
        version_probe: ProcessResult | None = None
        for executable in candidates[:1]:
            result = await self._audited_probe(
                "probe_version",
                executable,
                ["--version"],
            )
            version_probe = result
            if result.ok:
                # Keep the discovered path AS RESOLVED (not realpath'd): a
                # multiplexer launcher like ``~/.toolbox/bin/kiro-cli``
                # dispatches on its argv[0] basename, so resolving the
                # symlink to ``toolbox-exec`` would make whoami/login fail
                # with "Command doesn't appear to be associated with any
                # tool". This is the exact path ``--version`` just succeeded
                # with and the one ACP launches.
                self._viable_binary = executable
                break

        if not self._viable_binary:
            # Was the probe refused BY THE SANDBOX, as opposed to failing for
            # any other reason? Verification runs the candidate inside the
            # sandbox (see _UNVERIFIED_SANDBOX_MODE), so on a host that
            # cannot build one a perfectly good, already-authenticated CLI
            # fails verification and must not be reported as missing.
            #
            # This keys on what the spawn itself reported, NOT on whether the
            # host has a backend: the typed SandboxUnavailableError when the
            # sandbox could not be built before the spawn, or — when the
            # launcher refused after it (a container that grants the
            # namespaces but denies the launcher's first mount can pass a
            # cached boot probe and fail here) — the sandbox's own fresh
            # probe, which the launcher's stderr line only triggers and
            # which a candidate's output can therefore never forge. Host
            # capability is not evidence: _run_process skips the wrap on
            # Windows, and the allow_unsandboxed_exec opt-in bypasses it, so
            # a broken CLI on either would otherwise be blamed on the sandbox
            # and lose the repair actions that would genuinely help.
            sandbox_failure = version_probe.sandbox_failure if version_probe else None
            first_candidate = candidates[0] if candidates else ""
            # Off-loop: _is_runnable_executable realpath()s and stat()s the
            # candidate, and a stalled NFS/autofs mount would block those
            # syscalls indefinitely — on the event loop that freezes the whole
            # gateway, not just this probe.
            candidate_runnable = bool(first_candidate) and await asyncio.to_thread(
                _is_runnable_executable, first_candidate, self._platform
            )
            if sandbox_failure is not None and candidate_runnable:
                kind, detail, remedy = sandbox_failure
                logger.warning(
                    "Kiro CLI at %s is present and executable but could not be "
                    "verified: the sandbox refused the probe (%s: %s)",
                    first_candidate,
                    kind,
                    detail,
                )
                login_cmd, sso_cmd, bundled = login_commands_for(first_candidate, self._environ)
                self._status = PrerequisiteStatus(
                    platform=_platform_label(self._platform),
                    # Present and executable on disk. Verification is what
                    # failed, and it failed for an unrelated reason.
                    installed=True,
                    # Unknown, not false: whoami also runs through the probe
                    # path, so we cannot claim either way.
                    authenticated=False,
                    ready=False,
                    repair_required=False,
                    initial_setup_complete=self._initial_setup_complete,
                    login_command=login_cmd,
                    sso_login_command=sso_cmd,
                    bundled_cli=bundled,
                    sandbox_unavailable=True,
                    sandbox_failure_kind=kind,
                    sandbox_detail=detail,
                    sandbox_remedy=remedy,
                )
                self._stamp_probe(probe_identity)
                return self._status
            if version_probe is not None and version_probe.timed_out and candidate_runnable:
                # A probe that never answered is not evidence of absence. The
                # spawn was accepted and raised no typed failure, so the
                # sandbox branch above cannot claim it, and falling through to
                # the bare default below asserts installed=False about a binary
                # this function just confirmed is present and executable.
                #
                # Reported as the SAME shape as a sandbox refusal, for the same
                # reason: present on disk, verification is what failed. The
                # timeout gets its own flag rather than reusing sandbox_* --
                # nothing about the sandbox failed here, and a caller that keys
                # a userns remedy off sandbox_unavailable must not be handed a
                # slow filesystem to fix with an AppArmor profile.
                #
                # WARNING on the TRANSITION into the condition, DEBUG while it
                # persists. This branch runs on EVERY probe, and on an affected
                # host that is one probe per ~40s (the readiness gate's 30s
                # staleness window plus this probe's own 10s timeout), so ~90
                # lines/hour into the dashboard's 1000-entry log ring. A line that
                # reports a slow host must not be the thing that evicts the rest of
                # the evidence about it.
                #
                # The latched status IS the transition record, so this needs no
                # timestamp and no re-warn floor: every probe rewrites
                # ``self._status``, so a recovery re-arms the warning by itself and
                # the next timeout speaks up. The dashboard gate needs the extra
                # floor only because it has no object to hang state on and its
                # clear path depends on a caller happening to observe the recovery.
                if self._status.probe_timed_out:
                    logger.debug(
                        "Kiro CLI at %s still unverified: probe timed out again "
                        "after %ss (already reported)",
                        first_candidate,
                        _PROBE_TIMEOUT_SECS,
                    )
                else:
                    logger.warning(
                        "Kiro CLI at %s is present and executable but could not be "
                        "verified: the probe timed out after %ss. The CLI may be "
                        "installed and signed in; this is not evidence it is "
                        "missing. Further timeouts log at DEBUG until it recovers.",
                        first_candidate,
                        _PROBE_TIMEOUT_SECS,
                    )
                login_cmd, sso_cmd, bundled = login_commands_for(first_candidate, self._environ)
                self._status = PrerequisiteStatus(
                    platform=_platform_label(self._platform),
                    installed=True,
                    # Unknown, not false: whoami runs through the same probe
                    # path, and on this host it is never even reached.
                    authenticated=False,
                    ready=False,
                    repair_required=False,
                    initial_setup_complete=self._initial_setup_complete,
                    login_command=login_cmd,
                    sso_login_command=sso_cmd,
                    bundled_cli=bundled,
                    probe_timed_out=True,
                )
                self._last_probe_at = self._clock()
                self._has_probed = True
                return self._status
            # Neither typed condition explains the failure, so carry the
            # probe's own account of it: without this the bare default below
            # says only installed=False and the gate has no diagnostic to
            # show (the desktop "Setup Check Unavailable" dead end).
            self._status = PrerequisiteStatus(
                platform=_platform_label(self._platform),
                initial_setup_complete=self._initial_setup_complete,
                probe_error=_probe_failure_text(version_probe),
                probe_status=version_probe.returncode if version_probe else None,
            )
            self._stamp_probe(probe_identity)
            return self._status

        # A viable binary answered ``--version``, so it can be signed into
        # (trust is "it runs"); ``whoami`` decides whether it is already
        # authenticated. No provenance gate: source/owner/path do not block
        # sign-in, so a runnable CLI never needs an unreachable "repair".
        # ``whoami`` decides whether the CLI is already signed in. Run it the
        # same way a real ACP session runs the CLI (see acp/runtime.py):
        # against the real environment/home, NOT a credential-minimal
        # rewritten HOME. A rewritten HOME breaks any CLI whose session or
        # tool registry lives in the real home — e.g. a multiplexer launcher
        # cannot even resolve itself without its real-home registry — so the
        # isolated probe reported such CLIs signed-out even though a real
        # session authenticates fine.
        whoami = await self._audited_identity_probe(self._viable_binary, isolate_home=False)
        if whoami.ok:
            await asyncio.to_thread(self._mark_setup_complete)
        # Kiro CLI acceptance is checked here, on the probe path, because it
        # costs a spawn per spec — the every-read overlay stays stat-only. It
        # needs a successful identity probe: ``agent validate`` refuses to run
        # signed out (measured on kiro-cli 2.21.1), so asking would only fail.
        rejected: list[str] = []
        rejection_detail = ""
        if whoami.ok:
            rejected, rejection_detail = await self._probe_spec_acceptance(self._viable_binary)
        else:
            # Kiro CLI validation needs sign-in, but KAS runs signed out. Apply
            # KAS's local loading contract instead so a present spec KAS cannot
            # load still reaches repair_required and the KAS setup gate.
            rejected, rejection_detail = await self._probe_kas_spec_loading()
        # KAS needs ACP support but not a kiro-cli login, so the probe must
        # check for a too-old relay binary even when signed out.
        acp_supported = await self._probe_acp_support(self._viable_binary)
        login_cmd, sso_cmd, bundled = login_commands_for(self._viable_binary, self._environ)
        self._status = PrerequisiteStatus(
            platform=_platform_label(self._platform),
            installed=True,
            authenticated=whoami.ok,
            # A required spec the CLI refuses fails every turn, exactly like
            # one that is absent, so it narrows readiness the same way. A CLI
            # without the ``acp`` subcommand cannot start ANY session, so it
            # narrows readiness too — but its remedy is an update, not a spec
            # rewrite, so it is tracked separately from ``repair_required``.
            ready=whoami.ok and not rejected and acp_supported,
            repair_required=bool(rejected),
            initial_setup_complete=self._initial_setup_complete,
            login_command=login_cmd,
            sso_login_command=sso_cmd,
            bundled_cli=bundled,
            rejected_agent_specs=rejected,
            agent_spec_rejection_detail=rejection_detail,
            acp_supported=acp_supported,
        )
        self._stamp_probe(probe_identity)
        return self._status


async def _probe_acp_support(self: KiroPrerequisiteService, executable: str) -> bool:
    """Ask kiro-cli whether it exposes the ``acp`` subcommand Kiro Crew uses.

    Returns ``True`` when the subcommand is present OR when the probe could
    not establish otherwise; ``False`` only on a CLEAN "unknown subcommand"
    rejection.

    Kiro Crew launches every session as ``kiro-cli acp ...`` (see
    ``acp.client`` / ``acp.runtime``). A CLI too old to have that subcommand
    runs fine and signs in fine, then fails at session-create with an opaque
    ``process exited (rc=None)`` — the same class of silent, hard-to-place
    failure the rest of this module exists to turn into an actionable card.
    ``acp --help`` is the read-only way to ask: a CLI that HAS the subcommand
    exits 0 and prints its help, one that lacks it exits nonzero with an
    "unknown subcommand" line (kiro-cli is a clap CLI).

    The verdict is deliberately conservative in ONE direction. A timeout, a
    sandbox refusal, or any nonzero exit whose text is not a recognized
    rejection is treated as SUPPORTED, so a probe that merely failed to run
    never blocks a working, up-to-date install behind an update card it does
    not need. The cost is that a genuinely-too-old CLI whose rejection wording
    is unrecognized would slip through here — but that install then fails at
    session-create with the pre-existing error, i.e. no worse than before this
    check existed, whereas a false positive would strand a healthy install.

    A spawn, so it belongs to the probe's boot-and-explicit-action budget.
    The caller probes every viable binary independently of ``whoami``:
    KAS needs ACP support without requiring a kiro-cli login.
    """

    result = await self._audited_probe(
        "probe_acp_support",
        executable,
        [_ACP_SUBCOMMAND, "--help"],
    )
    if result.ok:
        return True
    # A probe that could not even run (sandbox refusal, timeout, spawn error)
    # is not evidence the subcommand is missing. Only a clean rejection counts.
    if result.timed_out or result.sandbox_failure is not None:
        return True
    haystack = (result.output or "").lower()
    return not any(marker in haystack for marker in _ACP_UNSUPPORTED_MARKERS)


async def update_cli(self: KiroPrerequisiteService, caller: str = "") -> dict[str, Any]:
    """Run the CLI's own in-place self-update, then return a fresh snapshot.

    The Update button's action, behind an owner-gated POST so the spawn is
    origin-checked and audited. This is the ONE place this module runs a Kiro
    CLI subcommand that is not a read-only probe — justified because
    ``kiro-cli update`` is the CLI updating ITSELF in place (the same command
    the auto-update path in ``slack/gateway_runtime/update_apply.py`` already
    runs unattended), not a remote installer script or a credential-writing
    flow, so it adds no new privileged surface. It is offered only to remedy a
    too-old CLI that lacks the ``acp`` subcommand.

    Returns a snapshot with ``cli_update_error`` set — empty on success. The
    error is returned rather than raised so the gate can render it in place,
    the same contract as ``repair_agent_specs``. Runs to completion within the
    request (bounded by :data:`_UPDATE_TIMEOUT_SECS`); a re-probe follows so a
    successful update clears the card instead of leaving stale state up.
    """

    del caller  # the SEL record is written by the route's audit middleware
    if self._assume_ready:
        # A test / offline gateway asserts its own readiness and has no real
        # CLI to update; running an update there is meaningless.
        result = await self._dashboard_snapshot()
        result["cli_update_error"] = ""
        return result
    # Resolve the binary the way the probe does, off-loop.
    probe_environment, candidates = await asyncio.to_thread(
        _probe_filesystem_state,
        self._platform,
        self._home,
        self._environ,
    )
    executable = candidates[0] if candidates else ""
    error = ""
    if not executable:
        error = "Kiro CLI could not be found to update."
    elif is_bundled_kiro_cli(executable, self._environ):
        # The bundled copy lives inside the signed app bundle: writing there
        # breaks the codesign seal (Gatekeeper then calls the app "damaged"),
        # and it is pinned to the version the app was built against anyway.
        # Refused BEFORE the audited spawn so no "invoked" record is written
        # for a step that never runs.
        error = BUNDLED_CLI_UPDATE_REFUSAL
    else:
        # The resolved binary is UNVERIFIED (candidates[0] is whatever sits
        # first on PATH; an agent that can plant ~/.local/bin/kiro-cli would
        # otherwise have it run against the real home). Run it under the
        # strict sandbox — the same posture verification uses for an
        # unverified candidate — so ~/.aws / ~/.ssh stay hidden even though
        # the owner clicked Update. Network reach for the self-update comes
        # from the proxy keys (which govern egress), NOT from the standard
        # sandbox's real-home exposure; the identity credential is
        # deliberately omitted because `update` fetches a binary, it does
        # not authenticate.
        update_environment = dict(probe_environment)
        update_environment.update(_allowlisted_env(self._environ, _IDENTITY_PROXY_ENV_KEYS))
        await self._audit(
            action="update_cli",
            outcome="invoked",
            caller="gateway-setup",
            critical=True,
        )
        try:
            update = await self._run(
                executable,
                ["update"],
                env=update_environment,
                timeout_secs=_UPDATE_TIMEOUT_SECS,
                sandbox_mode=_UNVERIFIED_SANDBOX_MODE,
                # _hidden_probe_dirs (not _crew_hidden_dirs) so the unverified
                # binary cannot read the Kiro identity token store either — the
                # same isolation the read-only probe applies. `update` fetches a
                # binary; it has no need for the on-disk identity credential.
                extra_hidden_dirs=self._hidden_probe_dirs,
            )
        except asyncio.CancelledError:
            await self._set_terminal_audit("update_cli", "failed", "gateway-setup", "cancelled")
            raise
        except Exception as exc:
            logger.warning("kiro-cli update failed to run", exc_info=True)
            await self._set_terminal_audit(
                "update_cli", "failed", "gateway-setup", "update execution failed"
            )
            error = _sanitize_detail(f"{type(exc).__name__}: {exc}")
        else:
            if update.timed_out:
                error = (
                    "kiro-cli update did not finish in time. Run "
                    f"`{KIRO_CLI_UPDATE_COMMAND}` on the gateway host directly."
                )
            elif not update.ok:
                error = _sanitize_detail(
                    (update.output or "").strip()
                    or f"kiro-cli update exited with code {update.returncode}"
                )
            updated = update.ok and not error
            await self._set_terminal_audit(
                "update_cli",
                "completed" if updated else "failed",
                "gateway-setup",
                _terminal_audit_detail(update, updated),
            )
    if not error:
        # Re-probe so a successful update flips ``acp_supported`` / ``ready``
        # and the card clears. Explicit-action half of the probe budget.
        try:
            await self._probe(force=True)
        except Exception:  # noqa: BLE001 — stale state beats a 500
            logger.warning("Re-probe after kiro-cli update failed", exc_info=True)
    result = await self._dashboard_snapshot()
    if not error and not result.get("acp_supported", True):
        error = (
            "The update ran but this kiro-cli still has no `acp` command. "
            f"Update it manually with `{KIRO_CLI_UPDATE_COMMAND}` on the "
            "gateway host, or reinstall from the setup page."
        )
    result["cli_update_error"] = error
    return result


async def _audited_probe(
    self: KiroPrerequisiteService,
    action: str,
    executable: str,
    args: list[str],
) -> ProcessResult:
    """Run one credential-free status probe with paired SEL lifecycle events."""

    await self._audit(
        action=action,
        outcome="invoked",
        caller="gateway-status",
        critical=True,
    )
    try:
        result = await self._run(
            executable,
            args,
            env=self._probe_environment,
            timeout_secs=_PROBE_TIMEOUT_SECS,
            sandbox_mode=_UNVERIFIED_SANDBOX_MODE,
            extra_hidden_dirs=self._hidden_probe_dirs,
        )
    except asyncio.CancelledError:
        await self._set_terminal_audit(action, "failed", "gateway-status", "cancelled")
        raise
    except Exception:
        # A probe that cannot even run means the candidate is not viable,
        # not that the gateway is broken. Degrade to a not-ok result so the
        # status endpoint stays a retryable "not ready" instead of a 500.
        logger.warning("Kiro %s probe failed to run", action, exc_info=True)
        await self._set_terminal_audit(
            action,
            "failed",
            "gateway-status",
            "probe execution failed",
        )
        return ProcessResult(ok=False, error="Kiro CLI probe could not run")
    await self._set_terminal_audit(
        action,
        "completed" if result.ok else "failed",
        "gateway-status",
        _terminal_audit_detail(result, result.ok),
    )
    return result


async def _run_auth_command(
    self: KiroPrerequisiteService,
    executable: str,
    args: list[str],
    *,
    base_env: dict[str, str],
    timeout_secs: float,
    isolate_home: bool = True,
) -> ProcessResult:
    """Run one Kiro auth command against a sandboxed, trusted executable.

    The CLI is trusted because it runs (its install source, owner, and path
    do not gate sign-in); Kiro Crew is not the authority on where Kiro CLI is
    installed. The user's installed binary is executed IN PLACE — never a
    private copy: Kiro CLI 2.15+ is a multi-call binary that exec's a sibling
    ``kiro-cli-chat`` resolved relative to its own path, so a copy into a
    flat staging dir fails with ENOENT.

    ``isolate_home=False`` runs against the user's real home (like an ACP
    session), so a CLI whose session/registry lives in the real home is
    detected and device login writes its own credential store where the CLI
    normally keeps it. ``isolate_home=True`` keeps a credential-minimal
    temporary HOME holding only Kiro identity files, for read-only probes
    that must never see the real ``~/.aws`` / ``~/.ssh``.
    """

    if not isolate_home:
        # Run the way a real ACP session runs the CLI (acp/runtime.py):
        # against the real environment/home under the standard OS sandbox. A
        # rewritten HOME breaks CLIs whose session or tool registry lives in
        # the real home (e.g. a toolbox multiplexer that resolves itself via
        # its real-home registry), so this is what actually detects them, and
        # it is where the CLI's own credential store belongs.
        #
        # SECURITY: this matches the accepted ACP launch posture, not a new
        # surface — ACP already runs the resolved kiro-cli with the full real
        # environment on every session (the standard sandbox intentionally
        # exposes AWS/SSH to it). Device sign-in writes kiro-cli's OWN
        # credential store in its own home; that write is the entire point of
        # delegating sign-in to the CLI, and it targets the same store an ACP
        # session already reads. Kiro Crew stages nothing and publishes
        # nothing. Only Kiro Crew's own secret home is hidden, and the user's
        # binary runs IN PLACE, exactly as ACP runs it, so a multi-call CLI
        # can still reach its sibling subcommand executable.
        return await self._run(
            executable,
            args,
            env=base_env,
            timeout_secs=timeout_secs,
            sandbox_mode=_KIRO_AUTH_SANDBOX_MODE,
            extra_hidden_dirs=self._crew_hidden_dirs,
        )

    workspace = await asyncio.to_thread(
        _prepare_auth_workspace,
        self._platform,
        self._home,
        self._environ,
        base_env,
    )
    try:
        # The user's binary runs IN PLACE — never a copy staged under the
        # workspace. Kiro CLI 2.15+ dispatches subcommands by exec'ing a
        # sibling executable resolved relative to its own path, which a flat
        # copy destroys (ENOENT). Only the credential workspace is staged.
        return await self._run(
            executable,
            args,
            env=workspace.env,
            timeout_secs=timeout_secs,
            sandbox_mode=_KIRO_AUTH_SANDBOX_MODE,
            extra_hidden_dirs=self._hidden_probe_dirs,
            extra_visible_dirs=(str(workspace.root),),
        )
    finally:
        # Read-only probe home: nothing to publish, just remove it.
        await asyncio.to_thread(shutil.rmtree, str(workspace.root), ignore_errors=True)


async def _audited_identity_probe(
    self: KiroPrerequisiteService, executable: str, *, isolate_home: bool = True
) -> ProcessResult:
    """Run an identity probe with paired SEL events.

    The readiness check calls this with ``isolate_home=False`` so ``whoami``
    runs against the real home (like an ACP session) and detects CLIs whose
    session or tool registry lives there. ``isolate_home=True`` keeps the
    credential-minimal temporary home for callers that need it.

    Either way the environment carries :data:`_IDENTITY_PROBE_ENV_KEYS`, so a
    host authenticated by Kiro CLI's own API-key variable is detected as
    signed in rather than reported "Not logged in".
    """

    action = "probe_identity"
    await self._audit(
        action=action,
        outcome="invoked",
        caller="gateway-status",
        critical=True,
    )
    base_env = _identity_probe_env(self._environ, self._probe_environment)
    if not base_env.get(CRED_KIRO_API_KEY):
        # Post-scrub Docker: the entrypoint moved the CLI's own credential
        # from the process environ into the data home's .env (mode 600), so
        # the environ allowlist above found nothing. Read it back from the
        # file — otherwise an API-key container reads as "Not logged in"
        # while ACP sessions (which get the same re-injection at spawn)
        # authenticate fine. Off-loop: file read.
        fallback_key = await asyncio.to_thread(
            read_env_file_credential, CRED_KIRO_API_KEY, self._data_home / ".env"
        )
        if fallback_key:
            base_env[CRED_KIRO_API_KEY] = fallback_key
    try:
        result = await self._run_auth_command(
            executable,
            ["whoami"],
            base_env=base_env,
            # The identity budget, not the shared probe ceiling: ``whoami``
            # may refresh an OIDC token against an organization IdP (see
            # _IDENTITY_PROBE_TIMEOUT_SECS). By the time this runs, the same
            # binary already answered ``--version`` inside the short budget,
            # so the extra headroom is never spent on a missing or hung
            # candidate.
            timeout_secs=_IDENTITY_PROBE_TIMEOUT_SECS,
            isolate_home=isolate_home,
        )
    except asyncio.CancelledError:
        await self._set_terminal_audit(action, "failed", "gateway-status", "cancelled")
        raise
    except Exception:
        # A whoami that cannot even run means "not signed in", not a broken
        # gateway. Degrade to a not-ok result so the status endpoint reports
        # authenticated=False (retryable) instead of surfacing a 500 that
        # flashes the full-screen "could not check Kiro CLI" error.
        logger.warning("Kiro identity probe failed to run", exc_info=True)
        await self._set_terminal_audit(
            action,
            "failed",
            "gateway-status",
            "probe execution failed",
        )
        return ProcessResult(ok=False, error="Kiro identity probe could not run")
    await self._set_terminal_audit(
        action,
        "completed" if result.ok else "failed",
        "gateway-status",
        _terminal_audit_detail(result, result.ok),
    )
    return result


async def _set_terminal_audit(
    self: KiroPrerequisiteService,
    action: str,
    outcome: str,
    caller: str,
    error: str = "",
) -> None:
    try:
        await self._audit(
            action=action,
            outcome=outcome,
            caller=caller,
            error=error,
            critical=False,
        )
    except Exception:
        logger.warning("Could not write terminal Kiro setup audit event", exc_info=True)
