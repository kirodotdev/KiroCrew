"""Operations on a crew's own gateway, for ``kiro_crew.instances.ssh_tunnel_manager``.

The diagnosis ladder (now or scheduled after a failed recovery), the shared gate a
remote verb passes, ``kirocrew restart``, and the "Update remote" job with its
version check.

Each ``SshTunnelManager`` member here is a module-level function whose first parameter
is ``self``; the class in :mod:`kiro_crew.instances.ssh_tunnel_manager` binds it by
name, so the instance state, every patch of a method and every ``__new__`` build stay
on the one class.


Instance fields read: ``_registry``, ``_remote_update_verify_interval_secs``,
``_remote_update_verify_secs``, ``_remote_updates``, ``_tunnels``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.instances.constants import (
    DIAGNOSTICS_CONNECT_TIMEOUT_CAP_SECS as _DIAGNOSTICS_CONNECT_TIMEOUT_CAP_SECS,
)
from kiro_crew.instances.diagnostics import (
    DiagnosisResult,
    diagnose_instance,
    diagnose_instance_fargate,
    diagnose_instance_ssm,
)
from kiro_crew.instances.registry import CONNECTION_METHODS, Instance
from kiro_crew.instances.remote_update import (
    PHASE_RESTARTING,
    PHASE_VERIFYING,
    REMOTE_UPDATE_TIMEOUT_SECS,
    STATE_SUCCEEDED,
    STATE_UNCHANGED,
    STATE_UNVERIFIED,
    RemoteUpdateJob,
    clean_version,
)
from kiro_crew.instances.ssh_tunnel_manager_transport import (
    _transport_method,
    _TransportParams,
    _unsupported_method_message,
)
from kiro_crew.instances.ssm_token_mint import run_remote_kirocrew_ssm
from kiro_crew.instances.token_mint import run_remote_kirocrew
from kiro_crew.instances.validation import (
    SshValidationError,
    SsmValidationError,
    UnsupportedConnectionMethodError,
)
from kiro_crew.security import redact

if TYPE_CHECKING:
    from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

#: The manager's own logger: this code logs under the facade module's name.
logger = logging.getLogger("kiro_crew.instances.ssh_tunnel_manager")


async def diagnose(self: SshTunnelManager, instance_id: str) -> dict | None:
    """Run the failure-diagnosis ladder for *instance_id*.

    Read-only ordered probes (transport reachability → remote dashboard →
    local forward); the first broken link is the diagnosis. Result is stored
    on the live tunnel's status so it surfaces in ``status()``/``to_dict()``.
    Runs WITHOUT the manager lock (the probes do network I/O). Returns the
    result dict, or None for an unknown instance.
    """
    inst = await asyncio.to_thread(self._registry.get, instance_id)
    if inst is None:
        return None
    tunnel = self._tunnels.get(instance_id)
    local_port = (tunnel.status.local_port if tunnel else 0) or inst.local_port
    method = _transport_method(inst)
    if inst.via_instance_id:
        # A chained crew is probed along the hop THIS gateway opened — the
        # parent's host and the parent's loopback port — because that is the
        # link that can be broken here. Probing the crew's own coordinates
        # would dial a host this gateway has no route to and report every
        # healthy chain as unreachable. A parent that has gone from the
        # registry leaves nothing to probe, and that IS the diagnosis.
        parent = await self._with_parent(inst)
        if parent is None:
            result = DiagnosisResult(
                code="chain_parent_missing",
                reason=(
                    f"the crew this one is reached through ({inst.via_instance_id}) is no "
                    f"longer configured. Remove this crew, or re-add it from the crew that "
                    f"reaches it."
                ),
                probes=[{"name": "chain_parent", "ok": False}],
            )
        else:
            result = await diagnose_instance(
                parent.ssh_host,
                inst.via_remote_port,
                local_port,
                connect_timeout_secs=min(
                    self._connect_timeout_for("ssh"), _DIAGNOSTICS_CONNECT_TIMEOUT_CAP_SECS
                ),
            )
    elif method == "fargate":
        result = await diagnose_instance_fargate(
            inst.ssm_target,
            local_port,
            aws_profile=inst.aws_profile,
            aws_region=inst.aws_region,
        )
    elif method == "ssm":
        result = await diagnose_instance_ssm(
            inst.ssm_target,
            inst.remote_port,
            local_port,
            aws_profile=inst.aws_profile,
            aws_region=inst.aws_region,
            ssm_run_as=inst.ssm_run_as,
        )
    elif method not in CONNECTION_METHODS:
        # Nothing to probe: this build has no transport for the method, and
        # the SSH ladder would dial whatever ssh_host the record holds.
        result = DiagnosisResult(
            code=UnsupportedConnectionMethodError.code,
            reason=_unsupported_method_message(inst),
            probes=[],
        )
    else:
        result = await diagnose_instance(
            inst.ssh_host,
            inst.remote_port,
            local_port,
            connect_timeout_secs=min(
                self._connect_timeout_for("ssh"), _DIAGNOSTICS_CONNECT_TIMEOUT_CAP_SECS
            ),
        )
    diag = result.to_dict()
    # Re-fetch the tunnel (it may have changed during the probes) and attach.
    tunnel = self._tunnels.get(instance_id)
    if tunnel is not None:
        tunnel.status.diagnosis = diag
    logger.info("Instance %s diagnosis: %s", instance_id, diag.get("code"))
    return diag


async def restart_remote(self: SshTunnelManager, instance_id: str) -> dict:
    """Restart the remote Kiro Crew gateway over the instance's transport.

    Uses the remote ``kirocrew restart`` (itself systemd/launchd-aware),
    resolved via the run-marker first (the running gateway's own launcher,
    keyed by ``remote_port``) and falling back to the bin-candidate ladder —
    so restart works even when ``~/.local/bin/kirocrew`` points at an
    uninstalled worktree. Validates the transport params first. After a
    restart the remote dashboard port bounces, so the local tunnel's health
    probe detects the drop and self-heals (Stage 2) — no manual reconnect
    needed. Returns ``{ok, message}``.
    """
    target = await self._remote_command_target(instance_id, "restart")
    if isinstance(target, dict):
        return {"ok": False, "message": target["message"]}
    inst, params = target
    rc, err = await self._run_remote_subcommand(inst, params, "restart", stdout_tail=True)
    if rc == 0:
        logger.info("Restarted remote gateway for %s", instance_id)
        return {"ok": True, "message": "remote gateway restart requested"}
    logger.warning("Remote restart for %s failed (rc=%s): %s", instance_id, rc, err)
    return {"ok": False, "message": err or f"restart exited {rc}"}


async def _remote_command_target(
    self: SshTunnelManager, instance_id: str, verb: str
) -> tuple[Instance, _TransportParams] | dict:
    """Resolve where a fixed ``kirocrew <verb>`` would run, or why it cannot.

    The ONE gate every remote CLI verb shares (restart, update), so each
    refuses the same instances for the same reasons, before any command is
    built. Returns ``(inst, params)`` or a refusal ``{code, message}`` with
    ``code`` one of ``not_found`` / ``invalid_transport`` / ``chained`` /
    ``headless``.
    """
    inst = await asyncio.to_thread(self._registry.get, instance_id)
    if inst is None:
        return {"code": "not_found", "message": "unknown instance"}
    try:
        params = self._resolve_transport(inst, await self._with_parent(inst))
    except (SshValidationError, SsmValidationError) as e:
        return {
            "code": "invalid_transport",
            "message": f"invalid {inst.connection_method} settings: {e}",
        }
    if params.is_chained:
        # A chained crew is reached through another crew's hop, so this
        # gateway has no shell on it: the verb would have to be dispatched
        # at the PARENT, acting on the wrong machine. Run it from the crew
        # that reaches it.
        return {
            "code": "chained",
            "message": (
                f"{inst.name} is reached through {params.via_instance_id}, so this "
                f"dashboard cannot run commands on it. {verb.capitalize()} it from "
                f"that crew."
            ),
        }
    if params.method == "fargate" or params.headless:
        # Refused before any command is built: the far end runs no kirocrew
        # gateway, so a verb dispatched at it would only fail remotely --
        # and on the MicroVM lane it fails as ``sudo: unknown user``, which
        # reads like a misconfigured row rather than a verb that does not
        # apply to this crew.
        return {
            "code": "headless",
            "message": (
                f"a headless crew runs no Kiro Crew gateway to {verb}; "
                "stop and relaunch it instead"
            ),
        }
    return inst, params


async def _run_remote_subcommand(
    self: SshTunnelManager,
    inst: Instance,
    params: _TransportParams,
    subcommand: str,
    *,
    timeout_secs: float | None = None,
    stdout_tail: bool = False,
) -> tuple[int, str]:
    """Run the fixed literal ``kirocrew <subcommand>`` over *params*' transport.

    *subcommand* is always a literal from this module, never caller input.
    *timeout_secs* overrides the runner's own default budget, and
    *stdout_tail* asks the runner to add a redacted stdout tail to a
    failure's message; both are passed only when set, so the default
    call is the same one restart has always made.
    """
    extra: dict[str, Any] = {}
    if timeout_secs is not None:
        extra["timeout_secs"] = timeout_secs
    if stdout_tail:
        extra["stdout_tail"] = True
    if params.method == "ssm":
        return await run_remote_kirocrew_ssm(
            params.ssm_target,
            subcommand,
            aws_profile=params.aws_profile,
            aws_region=params.aws_region,
            ssm_run_as=params.ssm_run_as,
            remote_bin=params.remote_bin,
            marker_port=inst.remote_port,
            **extra,
        )
    return await run_remote_kirocrew(
        params.ssh_host,
        subcommand,
        remote_bin=params.remote_bin,
        marker_port=inst.remote_port,
        connect_timeout_secs=self._mint_timeout_for(params.method),
        **extra,
    )


async def start_remote_update(self: SshTunnelManager, instance_id: str) -> dict:
    """Start the explicit "Update remote" job, or say why it cannot start.

    Refused exactly like :meth:`restart_remote` (unknown, invalid transport,
    chained, headless), checked HERE before anything is scheduled, so the
    caller learns of a refusal synchronously. A crew already updating is
    refused ``already_running`` rather than queued. Otherwise the job runs
    in the background (:meth:`_update_then_restart`) and this returns
    ``{ok: True, job}`` at once. Returns ``{ok: False, code, message}`` on
    a refusal.
    """
    target = await self._remote_command_target(instance_id, "update")
    if isinstance(target, dict):
        return {"ok": False, **target}
    inst, params = target
    job = self._remote_updates.start(
        instance_id, lambda j: self._update_then_restart(j, inst, params)
    )
    if job is None:
        return {
            "ok": False,
            "code": "already_running",
            "message": f"{inst.name} is already updating",
        }
    logger.info("Remote update started for %s", instance_id)
    return {"ok": True, "job": job.to_dict()}


async def _update_then_restart(
    self: SshTunnelManager, job: RemoteUpdateJob, inst: Instance, params: _TransportParams
) -> None:
    """The job body: ``kirocrew update``, ``kirocrew restart``, then a version check.

    ``update`` gets the long :data:`REMOTE_UPDATE_TIMEOUT_SECS` budget (a
    wheel download plus install) and a redacted stdout tail on failure,
    because the CLI prints its failure reasons to stdout. A failed
    ``update`` never restarts, so the remote keeps serving the build it
    has. The remote's own policy provider, install layout and updater
    decide what ``update`` does.

    A zero exit does not prove an install: ``kirocrew update`` also exits 0
    when it is already on its channel's newest build and when the install
    is managed elsewhere. The running gateway only reports a new version
    after its restart, so the outcome is read from the crew's own
    ``/api/version`` once it is back: a different version than before is
    ``succeeded``, the same one is ``unchanged``, and no answer within
    :data:`REMOTE_UPDATE_VERIFY_SECS` is ``unverified``.
    """
    iid = inst.id
    job.version_before = await self._peer_version(iid)
    rc, err = await self._run_remote_subcommand(
        inst,
        params,
        "update",
        timeout_secs=REMOTE_UPDATE_TIMEOUT_SECS,
        stdout_tail=True,
    )
    if rc != 0:
        logger.warning("Remote update for %s failed (rc=%s): %s", iid, rc, err)
        job.fail(err or f"update exited {rc}")
        return
    job.advance(PHASE_RESTARTING)
    rc, err = await self._run_remote_subcommand(inst, params, "restart", stdout_tail=True)
    if rc != 0:
        logger.warning("Remote restart after update for %s failed (rc=%s): %s", iid, rc, err)
        job.fail(err or f"restart exited {rc}")
        return
    job.advance(PHASE_VERIFYING)
    after = await self._await_peer_version_change(iid, job.version_before)
    if not after:
        outcome = STATE_UNVERIFIED
    elif job.version_before and after == job.version_before:
        outcome = STATE_UNCHANGED
    else:
        outcome = STATE_SUCCEEDED
    logger.info(
        "Remote update for %s finished: %s (%s -> %s)", iid, outcome, job.version_before, after
    )
    job.finish(outcome, version_after=after)


async def _peer_version(self: SshTunnelManager, instance_id: str) -> str:
    """The crew's own ``/api/version``, or ``""`` when it does not answer.

    A peer is untrusted input and the value is kept in the job and logged,
    so it must be version-shaped AND come through the credential redactor
    unchanged: a token that happens to fit the version shape is dropped,
    never stored.
    """
    try:
        ok, raw = await self.peer_version(instance_id)
    except Exception:
        return ""
    if not ok:
        return ""
    version = clean_version(raw)
    return version if version and redact(version) == version else ""


async def _await_peer_version_change(self: SshTunnelManager, instance_id: str, before: str) -> str:
    """Read the restarted crew's version until it differs from *before*.

    Returns the first version that differs, or, once the
    ``_remote_update_verify_secs`` window closes, whatever the crew last
    answered (``before`` itself, or ``""`` if it never answered). The old
    gateway can still answer for a moment after ``restart`` returns, so one
    read is not enough.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + self._remote_update_verify_secs
    while True:
        after = await self._peer_version(instance_id)
        if after and after != before:
            return after
        if loop.time() >= deadline:
            return after
        await asyncio.sleep(self._remote_update_verify_interval_secs)


def remote_update_job(self: SshTunnelManager, instance_id: str) -> dict | None:
    """The latest update job for *instance_id* as a dict, or ``None``."""
    job = self._remote_updates.get(instance_id)
    return job.to_dict() if job is not None else None


def _schedule_diagnosis(self: SshTunnelManager, instance_id: str) -> None:
    """Fire-and-forget a diagnosis run (tracked so it isn't GC'd)."""
    task = asyncio.create_task(self.diagnose(instance_id))
    self._track_recovery(instance_id, task)
    task.add_done_callback(
        lambda t: (
            logger.error("Diagnosis task crashed for %s: %s", instance_id, t.exception())
            if not t.cancelled() and t.exception()
            else None
        )
    )
