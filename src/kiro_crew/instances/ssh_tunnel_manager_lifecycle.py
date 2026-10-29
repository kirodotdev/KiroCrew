"""The manager-lock lifecycle of a tunnel, for ``kiro_crew.instances.ssh_tunnel_manager``.

Connect (allocate, reclaim, spawn, gate, mint, persist), disconnect and teardown
(chained crews first), the reconfiguration barrier, shutdown, the status readers,
the live-config apply and the cancel-safe registry hint write.

Each ``SshTunnelManager`` member here is a module-level function whose first parameter
is ``self``; the class in :mod:`kiro_crew.instances.ssh_tunnel_manager` binds it by
name, so the instance state, every patch of a method and every ``__new__`` build stay
on the one class.

Instance fields written: ``_chained_far_end``, ``_chained_hop_identity``,
``_chained_hop_port``, ``_chained_ttl``, ``_connect_timeout``, ``_last_error``,
``_max_recovery``, ``_mint_timeout``, ``_peer_sessions``, ``_probe_fails``,
``_reconfiguring``, ``_recover_attempts``, ``_recover_backoff_max``,
``_ssh_compression``, ``_tokens``, ``_tunnel_epoch``, ``_tunnels``.

Instance fields read: ``_allocator``, ``_hop_guard``, ``_lock``, ``_recovery_tasks``,
``_refresh_tasks``, ``_registry``, ``_remote_updates``, ``_tunnel_factory``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import replace as dataclass_replace
from typing import TYPE_CHECKING, Any, TypeVar

from kiro_crew.cloud import ssm as cloud_ssm
from kiro_crew.instances.port_allocator import _is_port_free
from kiro_crew.instances.registry import (
    _NO_FORWARDER_PID,
    _UNALLOCATED_PORT,
    Instance,
    InstanceDisabledError,
    ancestor_ids,
    descendant_ids,
    is_headless_provisioner,
)
from kiro_crew.instances.ssh_tunnel_manager_child import TunnelState, TunnelStatus
from kiro_crew.instances.ssh_tunnel_manager_transport import _Mint
from kiro_crew.instances.token_mint import TokenMintError
from kiro_crew.instances.validation import (
    SshValidationError,
    SsmValidationError,
    UnsupportedConnectionMethodError,
)

if TYPE_CHECKING:
    from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

#: The manager's own logger: this code logs under the facade module's name.
logger = logging.getLogger("kiro_crew.instances.ssh_tunnel_manager")


_T = TypeVar("_T")


def apply_config(self: SshTunnelManager, instances_cfg: object) -> None:
    """Adopt new ``instances.*`` transport tunables.

    Every value here is consulted per operation -- per connect, per mint, per
    recovery attempt -- so pushing it onto the manager is a genuine hot apply
    rather than a value that only matters at construction. The probe threshold
    is additionally propagated into the tunnels ALREADY running, since each one
    copied it when it was built and would otherwise keep tearing itself down on
    the old count.

    ``tunnel_base_port`` is deliberately left alone: the allocator has already
    handed out ports from the old base and live tunnels hold them, so moving the
    base mid-flight would only fragment the range. It applies to a manager built
    after the change.
    """
    self._connect_timeout = getattr(instances_cfg, "connect_timeout_secs")
    self._mint_timeout = getattr(instances_cfg, "mint_timeout_secs")
    self._ssh_compression = bool(getattr(instances_cfg, "ssh_compression"))
    self._max_recovery = int(getattr(instances_cfg, "max_recovery_attempts"))
    self._recover_backoff_max = float(getattr(instances_cfg, "recover_backoff_max_secs"))
    self._probe_fails = int(getattr(instances_cfg, "probe_failure_threshold"))
    for tunnel in self._tunnels.values():
        # Attribute-set on the live tunnel rather than a restart: the threshold
        # is compared against a running counter, so the new value takes effect on
        # the next probe without dropping a healthy forward.
        tunnel._probe_fails = self._probe_fails


async def _persist_hint(
    self: SshTunnelManager, fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> None:
    """Run a registry hint write in a worker thread; return only when it is DONE.

    A cancelled ``await asyncio.to_thread(...)`` abandons only the await —
    the already-submitted worker thread keeps running, and its
    read-modify-rewrite of ``instances.json`` can land AFTER the caller's
    ``async with self._lock`` block has unwound. That late write races the
    next locked write (e.g. a cancelled connect's ``was_connected=True``
    overtaking a disconnect's reset and reviving an instance the user
    disconnected). So on cancellation this helper keeps waiting for the
    worker to finish, then re-raises the cancellation — the caller's lock
    is not released until the write has durably completed. Write FAILURES
    are swallowed: hint persistence is best-effort, matching the
    pre-offload ``contextlib.suppress(Exception)`` semantics.
    """
    task: asyncio.Task[Any] = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as e:
            if task.done():
                raise  # write already completed; propagate the cancel as-is
            cancelled = e
            continue  # keep waiting: the worker write is still in flight
        except Exception:
            pass  # best-effort hint write
        break
    if cancelled is not None:
        raise cancelled


async def connect(
    self: SshTunnelManager,
    instance_id: str,
    *,
    rebuild: bool = False,
    only_if_connected: bool = False,
) -> TunnelStatus:
    """Open a tunnel + mint a token for *instance_id*; return its status.

    Idempotent: connecting an already-connected instance returns its current
    status. Raises :class:`KeyError` for an unknown instance, or surfaces a
    validation / mint / spawn error via the returned status (state ERROR).
    Works for either ``connection_method`` — the transport is resolved by
    :meth:`_resolve_transport`.

    ``rebuild=True`` breaks the idempotence on purpose: a CONNECTED tunnel is
    torn down first (``keep_intent`` — the user is asking for the crew, not
    turning it off) and a fresh forwarder is spawned on a DIFFERENT local
    port: the port just freed is excluded from the allocation, so both a
    stalled stream on the old forwarder and a cause bound to the old port
    itself are escaped by one Retry. This is the pane's Retry after a load watchdog
    fired on a document that DID navigate: the transport is up by every
    probe the manager runs (``/api/health`` answers, the credential
    validates), yet one stream inside it stalled and the pane's module
    graph will wait on it forever. Nothing short of a new TCP path clears
    that, and the plain connect — which sees CONNECTED and returns — would
    hand the same stalled tunnel back.

    A teardown that fails (the stop raises) is reported as an ERROR status,
    the same way every other connect failure is: the live tunnel is left
    exactly as :meth:`_teardown_locked` leaves it (intact — nothing is
    removed unless the stop succeeded), the reason is retained for
    :meth:`last_error`, and the caller gets a 502 with a message instead of
    a propagated exception turned into an unexplained 500.

    ``only_if_connected=True`` is the opposite restriction: answer a
    CONNECTED tunnel exactly like the plain connect (its status, its cached
    token) but, when the tunnel is not up, spawn nothing and touch nothing
    -- return a DISCONNECTED status and leave ``was_connected`` as the user
    last set it. This is the viewport's auto-warm, whose job is to pre-mount
    panes for tunnels that are ALREADY up, never to bring one up. The check
    runs under the manager lock, the same lock :meth:`disconnect` holds
    while it stops a tunnel, so an auto-warm racing a disconnect either sees
    the tunnel still CONNECTED (and warms a pane the disconnect's
    ``removeWarm`` then drops) or sees it gone and stands down; it can never
    re-open a tunnel the user just closed, which a probe-then-connect from
    the browser could.

    A disabled crew raises :class:`InstanceDisabledError` and spawns nothing,
    and so does a chained crew with a disabled crew anywhere up its chain.
    The check reads the record under the same lock the disabling write is
    made under (see :meth:`reconfigure`), so a connect racing a disable either
    finishes first and is torn down by it, or starts after and is refused.
    """
    if rebuild and only_if_connected:
        raise ValueError("rebuild and only_if_connected are mutually exclusive")
    async with self._lock:
        inst = await asyncio.to_thread(self._registry.get, instance_id)
        if inst is None:
            raise KeyError(f"no instance with id {instance_id!r}")

        existing = self._tunnels.get(instance_id)
        # The port a rebuild tears down. Kept out of the allocation below so
        # the new forwarder lands on a DIFFERENT local port: the field
        # evidence has every stall on the first allocated port, so a cause
        # bound to the port itself (a stale listener, a local firewall or
        # proxy rule) is a live hypothesis alongside the stalled stream. A
        # first-free allocator would hand the just-freed port straight back
        # and Retry could loop on it forever with no in-product escape.
        rebuild_freed_port: int | None = None
        if only_if_connected and (
            existing is None or existing.status.state != TunnelState.CONNECTED
        ):
            logger.info(
                "Connected-only connect for %s declined: tunnel is %s",
                instance_id,
                existing.status.state.value if existing is not None else "absent",
            )
            return TunnelStatus(
                instance_id=inst.id,
                state=TunnelState.DISCONNECTED,
                local_port=inst.local_port,
                remote_port=inst.remote_port,
            )
        if inst.disabled:
            raise InstanceDisabledError(f"crew {instance_id!r} is disabled")
        if inst.via_instance_id:
            # A chained crew's forward dials its parent's host, so a disabled
            # crew anywhere up the chain keeps this one closed as well.
            rows = await asyncio.to_thread(self._registry.list)
            by_id = {row.id: row for row in rows}
            off = [a for a in ancestor_ids(rows, instance_id) if a in by_id and by_id[a].disabled]
            if off:
                raise InstanceDisabledError(
                    f"crew {off[0]!r}, which {instance_id!r} rides, is disabled"
                )
        if rebuild and existing is not None:
            logger.info(
                "Rebuilding tunnel for %s on request (was %s on 127.0.0.1:%s)",
                instance_id,
                existing.status.state.value,
                existing.status.local_port,
            )
            try:
                await self._teardown_locked(instance_id, keep_intent=True)
            except Exception as e:  # noqa: BLE001 - reported, not swallowed
                logger.warning("Rebuild of %s could not stop the old tunnel: %s", instance_id, e)
                return self._error_status(
                    inst,
                    f"could not stop the existing tunnel to rebuild it: {e}. "
                    f"Disconnect and connect again, or retry.",
                )
            if existing.status.local_port:
                rebuild_freed_port = existing.status.local_port
            existing = None
            # The registry row was just rewritten (local_port reset); re-read
            # so the allocation below skips nothing stale and records fresh.
            inst = await asyncio.to_thread(self._registry.get, instance_id)
            if inst is None:
                raise KeyError(f"no instance with id {instance_id!r}")
        if existing is not None and existing.status.state == TunnelState.CONNECTED:
            return existing.status
        if existing is not None:
            # Tracked but not CONNECTED: stop it first so its child is
            # terminated and the local forward freed before we spawn a
            # replacement. Otherwise the old child orphans (dropped from
            # _tunnels below, never killed) and keeps the port — every
            # replacement then hits ExitOnForwardFailure while _port_reachable
            # is still satisfied by the orphan -> tight respawn loop.
            with contextlib.suppress(Exception):
                await existing.stop()

        # Injection-safe validation immediately before building command lines.
        try:
            params = self._resolve_transport(inst, await self._with_parent(inst))
        except UnsupportedConnectionMethodError:
            # Raised, like a disabled crew, so the route answers with its typed
            # code; nothing has been spawned for this crew.
            raise
        except (SshValidationError, SsmValidationError) as e:
            return self._error_status(inst, f"invalid {inst.connection_method} settings: {e}")

        # A CHAINED crew is minted for BEFORE its forward opens, which is the
        # opposite order from every other method here, for one reason: the
        # forward's target port must come from the parent and not from our row.
        # `ssh -L <local>:127.0.0.1:<hop>` aims at the parent's own loopback,
        # and our row's copy of `hop` arrived over a pane's postMessage -- so a
        # forged notice would otherwise choose which loopback-only service on
        # that machine this gateway forwards, and the pane would then render
        # whatever answers as a crew's dashboard inside the owner's page. The
        # mint reply names the port the parent's own forward listens on, over
        # the credential we already hold for it. It can run first because it
        # rides the PARENT's hop, which is already up -- this crew's forward is
        # not involved in it at all.
        chained_mint: _Mint | None = None
        if params.is_chained:
            try:
                chained_mint = await self._mint_for(inst, params)
            except TokenMintError as e:
                return self._error_status(inst, f"token mint failed: {e}")
            hop = chained_mint.hop
            if not hop:
                return self._error_status(
                    inst,
                    f"crew {params.via_instance_id!r} did not name the port its forward to "
                    f"this crew listens on, so there is no hop to ride. Reconnect that crew.",
                )
            if hop != params.via_remote_port:
                # The parent disagrees with the row. Its answer wins and is
                # persisted, so a later reconnect and the rebuild path dial the
                # same port this one does.
                await self._persist_hint(self._registry.update, instance_id, via_remote_port=hop)
                inst = dataclass_replace(inst, via_remote_port=hop)
                params = dataclass_replace(params, via_remote_port=hop)

        # SSM needs the local session-manager-plugin; fail with an actionable
        # message rather than letting the child exit with a cryptic error.
        #
        # Probed in a worker thread: the probe resolves the plugin through the
        # deploy engine's shared resolver, which scans PATH, then the
        # well-known install dirs, then routes a fallback-dir hit through
        # executable-provenance validation — filesystem work that must not run
        # on the gateway event loop, where a stalled network mount would freeze
        # every request and heartbeat. Same reason _build_argv is offloaded
        # below, and the same thing the dashboard's own cloud handler does with
        # this exact call.
        if params.forwards_over_ssm:
            if not await asyncio.to_thread(cloud_ssm.session_manager_plugin_installed):
                return self._error_status(
                    inst,
                    await asyncio.to_thread(cloud_ssm.session_manager_plugin_unavailable_message),
                )

        # Reclaim our own forwarder if a prior gateway hard-kill leaked it
        # still holding this instance's recorded port. Keyed on the recorded
        # pid behind a strict exact-argv identity check — see the method for
        # why nothing else is ever signalled. Best-effort: allocation below
        # skips the recorded port whether or not the reclaim succeeded.
        await self._reclaim_orphan_forwarder(inst, params)

        # Allocate a free loopback port for the forward. It deliberately does
        # NOT have to equal ``inst.remote_port``. The embedded dashboard runs
        # in an iframe at http://127.0.0.1:<local_port>, and the remote
        # gateway accepts that because:
        #   * ``check_origin`` has a same-origin loopback branch — a loopback
        #     Origin equal to the request's own Host is trusted at ANY port,
        #     which is exactly the shape the iframe produces (it is served at
        #     127.0.0.1:<local_port> and calls that same location.host); and
        #   * ``build_allowed_hosts`` compares hostname only, so the Host
        #     header matches regardless of port; and
        #   * the session cookie is named from the browser-facing port
        #     (``_cookie_port_from_host``), so distinct local ports get
        #     distinct cookies instead of colliding in the shared 127.0.0.1
        #     jar — that helper exists precisely for tunnels whose local port
        #     differs from the remote's.
        # This does not reopen CSE SEC-016: a malicious local page on an
        # arbitrary port sends its own Origin while the Host stays the
        # gateway's, so the two differ and the same-origin branch rejects it.
        # Browsers forbid scripts from forging either header.
        #
        # Mirroring the remote port instead would make the shipped defaults
        # self-contradictory: a stock gateway binds the same default port on
        # both ends, so a stock hub would already hold the port a stock remote
        # reports and two stock installs could never connect.
        #
        # Every instance's recorded port stays reserved, and the allocator
        # probes each candidate, so a port anything still holds — including a
        # leftover forwarder of our own — is skipped rather than fought over.
        # That skip is why no orphan-reaping step is needed for connect to
        # make PROGRESS: nothing has to be killed to get a working tunnel.
        #
        # The cost that skip alone would carry: an ``ssh -N -L`` child
        # orphaned by a gateway hard-kill keeps its loopback port and its
        # session to the remote until the OS reaps it. That leak is now
        # reclaimed by ``_reclaim_orphan_forwarder`` above — by the child's
        # RECORDED pid behind a strict exact-argv identity check, never by
        # scanning the process table. Scanning it by argv pattern could
        # SIGTERM a forward the operator opened themselves; an unrecorded or
        # unverified process is therefore left alone, and allocation simply
        # skips its port.
        #
        # The recorded ``local_port`` is PREFERRED, not merely skipped: the
        # loopback port is the browser ORIGIN of this pane's iframe
        # (``http://<host>:<local_port>``), and origin-keyed client state —
        # ``localStorage`` UI preferences above all — is lost the moment that
        # origin moves. ``disconnect`` zeroes the port, but ``shutdown``
        # documents that it "Leaves registry hints intact", so the recorded
        # port survives a gateway RESTART, and the auto-revive on the next
        # start reconnects through here. A first-free-only allocator lets the
        # crew land on a DIFFERENT port after that restart whenever another
        # instance claimed the lower port first, silently resetting the user's
        # pane settings — re-minting the token and reloading the pane does
        # nothing for state the browser keys by origin. So the recorded port
        # is passed as ``preferred`` and returned unchanged when it is still
        # free; only if something else now holds it does allocation fall
        # through to first-free. The recorded port is
        # dropped from ``reserved`` first (``_reserved_ports`` adds every row's
        # own ``local_port``), or the preference could never be honoured. A
        # rebuild deliberately wants a different port — the field evidence puts
        # every stall on the first-allocated port — so it passes no preference
        # and keeps the recorded port excluded, gated on the rebuild flag
        # rather than on there being a freed port to add back.
        #
        # Everything here runs off the event loop: ``_reserved_ports`` reads
        # the registry from disk under its own lock, and the port probe binds
        # a socket. Under the mirror neither happened on this path -- the port
        # was a fixed field read and one probe -- whereas this reads a file
        # and can walk upward past every occupied candidate, all inside the
        # manager lock on the gateway's loop, where a synchronous scan would
        # stall unrelated requests and heartbeats. This matches how the rest
        # of the module already reaches the registry (``asyncio.to_thread``).
        reserved = await asyncio.to_thread(self._reserved_ports)
        # Prefer this crew's own recorded port for origin stability (above),
        # but not on a rebuild, which wants a fresh port. ``_reserved_ports``
        # adds every row's ``local_port`` including this crew's, so the
        # preference is unreachable unless its own port is dropped from the
        # exclude set first.
        #
        # SECURITY: the drop must NOT expose a port another crew still claims.
        # Two rows can record the SAME port number (a duplicate hint from an
        # earlier allocation), and a LIVE HOP LEASE means a chained credential
        # this gateway minted still routes a bearer token to that port number,
        # so ``_reserved_ports`` withholds it whoever's row records it. Dropping
        # it here to satisfy the origin preference — while another row records
        # it or a lease covers it — would bind this crew's forward under a token
        # minted for a different crew, the exact confused-deputy the reservation
        # exists to close. A shared or leased recorded port therefore stays
        # reserved and unpreferred: the crew takes a fresh port this cycle, and
        # the next reconnect once it is this crew's alone restores the stable
        # origin. The lease is re-armed on a failed forward exit by
        # ``_on_tunnel_exit`` / ``_recover_after``.
        preferred_port = 0
        if rebuild:
            # A rebuild wants a fresh port, so the recorded port stays
            # excluded — whether or not the torn-down forwarder had bound
            # one to free (``rebuild_freed_port`` can be None when the old
            # tunnel never bound a port). Gating on the flag rather than the
            # freed port keeps the recorded port reserved in every rebuild.
            reserved = set(reserved)
            if rebuild_freed_port is not None:
                reserved |= {rebuild_freed_port}
        elif inst.local_port:
            leased = await asyncio.to_thread(self._registry.live_hop_leases)
            shared = await asyncio.to_thread(
                self._port_recorded_by_another_row, inst.local_port, inst.id
            )
            if inst.local_port not in leased and not shared:
                preferred_port = inst.local_port
                reserved = set(reserved) - {inst.local_port}
        try:
            local_port = await asyncio.to_thread(
                self._allocator.allocate, exclude=reserved, preferred=preferred_port
            )
        except RuntimeError as e:
            return self._error_status(inst, str(e))

        # If WE are holding this port, let go before probing it. The allocator
        # excludes live leases, so a port it hands back is one whose lease has
        # lapsed and which we therefore have no business still owning -- and a
        # hold the reaper has not swept yet would otherwise fail the check below
        # against ourselves, turning a legitimate reconnect into a port conflict.
        self._hop_guard.release(local_port)
        # The probe above is advisory — there is an inherent TOCTOU window
        # between probing and ssh actually binding — so re-check immediately
        # before spawning and fail with an actionable message rather than
        # letting the child exit on ExitOnForwardFailure.
        if not await asyncio.to_thread(_is_port_free, local_port):
            return self._error_status(
                inst,
                f"local port {local_port} was taken while connecting. Retry; "
                f"if it keeps happening, disconnect whatever is holding port "
                f"{local_port} or move instances.tunnel_base_port to a "
                f"quieter range.",
            )

        # Open the tunnel first so the forward is live.
        tunnel = self._tunnel_factory(
            inst.id,
            params.ssh_host,
            local_port,
            params.forward_remote_port(inst.remote_port),
            connect_timeout_secs=self._connect_timeout_for(params.method),
            compression=self._ssh_compression,
            probe_failure_threshold=self._probe_fails,
            on_exit=self._on_tunnel_exit,
            **params.tunnel_kwargs(),
        )
        self._tunnels[instance_id] = tunnel
        self._tunnel_epoch[instance_id] = self._tunnel_epoch.get(instance_id, 0) + 1
        tunnel.status.turn_url = params.turn_url(local_port)
        tunnel._on_healthy = self._on_tunnel_healthy
        ok = await tunnel.start()
        if not ok:
            self._last_error[instance_id] = tunnel.status.error or "tunnel failed to start"
            # Drop the failed tunnel (matching the mint-failure path below) so
            # status() returns None and _status_for surfaces the error via the
            # last_error() fallback, rather than leaving a stale ERROR tunnel
            # lingering in _tunnels (its process never started, so _on_tunnel_exit
            # never fires to clean it up).
            self._tunnels.pop(instance_id, None)
            return tunnel.status

        # Cycle guard, for a chained forward only. Which gateway is at the far
        # end of a hop cannot be known until the hop is open, so the check has
        # to happen here rather than at add time: the chain names ports on
        # other machines, and only the far end can say who it is. A loop that
        # closes back on this gateway (or on a crew already carrying the hop)
        # would nest a dashboard inside itself, and every pane in the loop
        # would fight over the same tab bar.
        cycle = await self._chain_cycle_reason(inst, local_port)
        if cycle:
            with contextlib.suppress(Exception):
                await tunnel.stop()
            self._tunnels.pop(instance_id, None)
            return self._error_status(inst, cycle)
        # Who answers at the far end of this hop right now: the crew the
        # credential below is for. Recorded only once that credential is
        # stored, so a refused connect leaves nothing behind.
        far_end: str | None = await self._peer_identity(local_port) if params.is_chained else ""
        stated = chained_mint.crew_gateway_id if chained_mint is not None else None
        refusal = ""
        if far_end is None:
            # No answer cannot be told apart from a listener that stays silent,
            # and treating it as "a crew too old to say" would leave this
            # forward ungated for its whole life.
            refusal = (
                "the crew at this hop did not answer the identity check, so its "
                "token was withheld. Connect again."
            )
        elif stated is not None and far_end != stated:
            # The parent named the crew this credential is for -- or said the
            # crew reports no id. Anything else answering at the hop, including
            # a listener that took the port after the parent let it go, gets
            # nothing.
            refusal = (
                "the crew answering at this hop is not the one its parent minted "
                "the token for, so the token was withheld. Connect again."
            )
        if refusal:
            with contextlib.suppress(Exception):
                await tunnel.stop()
            self._tunnels.pop(instance_id, None)
            return self._error_status(inst, refusal)

        # Mint a per-instance token over the same transport (never logged).
        # A HEADLESS crew's forward reaches a turn API, not a dashboard: there
        # is no token to mint and none to refresh, so the forward alone is the
        # connection and the status carries the turn URL instead.
        #
        # Keyed on the CREW, not on the transport. A headless crew reached
        # over SSM is as headless as one reached over an ECS exec, and asking
        # the transport instead mints a dashboard token against a crew that
        # serves no dashboard: the mint fails, this method returns an error
        # status, and the forward it already installed is torn down -- so the
        # pane reports a crew that will not answer about a forward that is
        # fine. This seam is owned elsewhere, so the change here is one
        # condition.
        if params.method != "fargate" and not is_headless_provisioner(inst.provisioner_id):
            try:
                # Already minted above for a chained crew, because its reply is
                # what named the port this forward was allowed to dial.
                mint = chained_mint or await self._mint_for(inst, params)
            except TokenMintError as e:
                await tunnel.stop()
                self._tunnels.pop(instance_id, None)
                return self._error_status(inst, f"token mint failed: {e}")
            if not self._store_token(
                inst,
                mint,
                minted_at_epoch=self._tunnel_epoch.get(instance_id, 0),
                binds_forward=True,
            ):
                # This site installed the tunnel itself and has held the lock
                # ever since, so the only way the store refuses is that the
                # forward does not ride the hop the mint named -- a CONNECTED
                # tab with no credential, which the pane cannot recover from.
                # Treat it as the mint failure it effectively is.
                await tunnel.stop()
                self._tunnels.pop(instance_id, None)
                return self._error_status(
                    inst,
                    "the crew holding the hop moved this crew to another port while its "
                    "token was being minted. Connect again.",
                )
            if far_end:
                self._chained_far_end[instance_id] = far_end
            else:
                # A crew that answered but whose build predates the field, as
                # both ends saw it: nothing to compare against, the same
                # arrangement the cycle guard admits.
                self._chained_far_end.pop(instance_id, None)
            self._schedule_token_refresh(instance_id)
            await self._prime_peer_session(instance_id)

        # Persist hints: the forwarder identity record built by
        # _forwarder_identity_hints (port, pid, start time, signature,
        # was_connected — the same record _mark_recovered writes) plus
        # last-active, which is this site's alone — ONE
        # read-modify-rewrite of instances.json (fsync), so the set is
        # durable together and the manager lock is held for a single fsync
        # round-trip. The identity is what a later connect uses to
        # reclaim this child if a gateway hard-kill orphans it.
        # _persist_hint runs it off the loop and does not
        # return — even under cancellation — until the write completes, so
        # the lock cannot release while the worker write is still in
        # flight (a late hint write would race a subsequent disconnect).
        await self._persist_hint(
            self._registry.update,
            instance_id,
            mark_last_active=True,
            **await self._forwarder_identity_hints(instance_id, tunnel),
        )
        # A successful (re)connect clears any stale give-up counter so the next
        # unexpected drop gets a full fresh recovery budget instead of tripping
        # the cap immediately.
        self._recover_attempts.pop(instance_id, None)
        # Connected cleanly — drop any retained failure reason from a prior
        # attempt so status() does not report a stale error.
        self._last_error.pop(instance_id, None)
        return tunnel.status


async def disconnect(
    self: SshTunnelManager, instance_id: str, *, keep_intent: bool = False
) -> bool:
    """Tear down *instance_id*'s tunnel, drop its token, clear its port hint.

    Returns whether a live tunnel existed.

    ``keep_intent`` distinguishes a RECONFIGURATION from a user disconnect.
    ``was_connected`` records that the user wants this instance connected, so
    only an explicit disconnect may clear it; a caller tearing a tunnel down
    in order to rebuild it (an edit that changes the host or port) passes
    ``keep_intent=True`` and leaves that flag alone. Restoring the flag
    afterwards instead would race a real disconnect arriving mid-edit and
    silently revive the instance the user just turned off.

    The persisted ``local_port`` is reset to the unallocated sentinel here —
    symmetric with :meth:`connect` setting it — so a disconnected instance
    never leaves a stale port recorded. Without this the freed port reads as
    perpetually reserved (``_reserved_ports`` / the ``local_port == 0``
    "unallocated" contract), and the instance can't be reconnected. The
    registry cleanup runs even when no live tunnel is tracked, so a port left
    behind by an unclean prior exit can still be cleared by a disconnect.

    A crew that other crews are CHAINED behind takes them down with it, and
    this is where that happens: their forwards ride this one's hop, so leaving
    them up would leave a forward pointing at a port on a machine this gateway
    has no route to — a pane that looks connected and answers nothing.
    Children are torn down first, deepest last, before the parent's own
    forward closes under them.
    """
    async with self._lock:
        for child_id in await self._chained_below(instance_id):
            # keep_intent on the children: the user turned off the PARENT.
            # Clearing a child's intent as well would silently un-remember it,
            # so reconnecting the parent would not bring its crews back.
            with contextlib.suppress(Exception):
                await self._teardown_locked(child_id, keep_intent=True)
        return await self._teardown_locked(instance_id, keep_intent=keep_intent)


async def _chained_below(self: SshTunnelManager, instance_id: str) -> list[str]:
    """Ids whose forward rides *instance_id*'s hop, nearest first.

    Read off the event loop, like every other registry touch here. Empty for a
    gateway with no chained crews, which is the only shape that existed before
    chaining — so this adds one registry read to a disconnect and changes
    nothing else about it.
    """
    instances = await asyncio.to_thread(self._registry.list)
    return descendant_ids(instances, instance_id)


async def _teardown_locked(self: SshTunnelManager, instance_id: str, *, keep_intent: bool) -> bool:
    """The body of :meth:`disconnect`, for callers already holding the lock.

    Reconfiguration needs the teardown and the coordinate rewrite to happen
    inside ONE critical section, so it cannot call the public method without
    deadlocking on our own non-reentrant lock.
    """
    # Stop FIRST, discard after. A stop that raises leaves the forward alive,
    # and everything below is what makes that forward usable: its token, its
    # refresh task, its place in `_tunnels`. Clearing any of it before the
    # process is really down leaves a live tunnel with no credential (session
    # transfer then reports `transfer_no_credential`) or, worse, an untracked
    # process holding the port. Nothing is removed unless the stop succeeded.
    tunnel = self._tunnels.get(instance_id)
    # Read the lease table BEFORE the stop, off the event loop. The read is the
    # slow part -- a file read plus a parse of every row -- and it is why a
    # synchronous `sync_hop_holds` here broke `no-blocking-call-on-event-loop`.
    # It is taken ahead of the stop rather than after it on purpose: `await`ing
    # anything BETWEEN the stop and the bind is what would widen the one window
    # this mechanism cannot close, from two adjacent statements to however long
    # the SHARED default executor takes to schedule -- unbounded when it is busy,
    # and it is the same executor the identity read runs on. So the slow half
    # moves ahead of the release and only the bind, which is a non-blocking
    # loopback syscall and one port rather than one per lease, stays in line.
    leases = await asyncio.to_thread(self._registry.live_hop_lease_deadlines)
    if tunnel is not None:
        await tunnel.stop()
    self._tunnels.pop(instance_id, None)
    self._tokens.pop(instance_id, None)
    self._peer_sessions.pop(instance_id, None)
    self._chained_ttl.pop(instance_id, None)
    self._chained_hop_port.pop(instance_id, None)
    self._chained_hop_identity.pop(instance_id, None)
    self._chained_far_end.pop(instance_id, None)
    # The moment the hole opens: the forward above has just released the port, and
    # a chained credential naming it is still valid for the rest of its lease.
    # Taking OS ownership here is what stops any other local process -- including a
    # second gateway, which cannot see our lease -- from binding it and being handed
    # that credential by a pane that has not noticed anything.
    self._apply_hop_holds(leases)
    self._recover_attempts.pop(instance_id, None)
    self._last_error.pop(instance_id, None)
    # A teardown ends the generation: a slow unlocked mint or rebuild in
    # flight captured the pre-teardown stamp, and this bump is what lets
    # the epoch compares at their store/record sites refuse the result —
    # membership alone reads true again as soon as anything reinstalls.
    self._tunnel_epoch[instance_id] = self._tunnel_epoch.get(instance_id, 0) + 1
    # Awaited, not just signalled: a refresh already inside its mint would
    # otherwise finish afterwards and store a token for a tunnel this teardown
    # has already removed. Ordered after the stop for the same reason as the
    # token itself — a rejected edit must leave the live tunnel intact.
    await self._cancel_token_refresh_and_wait(instance_id)
    # A parked self-heal is deliberately NOT drained here, although it has
    # the same shape of hazard. Draining would await _cancel_recovery under
    # the lock we hold, and unlike the refresh loop a recovery's
    # cancellation can be swallowed — _SshTunnel.stop() suppresses
    # CancelledError around its child-task awaits — after which the
    # survivor parks on THIS lock and the gather never returns. The
    # surviving recovery is harmless instead: the epoch bump above makes
    # its generation stamp stale, so tier 2's store refuses the token and
    # _mark_recovered unwinds the rebuild instead of recording it.
    # Clear the lazy-reconnect hint AND the recorded local port together
    # (one atomic write). local_port must return to the unallocated
    # sentinel so the now-free port is not treated as reserved forever,
    # and the forwarder pid goes with it: the child was just stopped, so a
    # retained pid would eventually be recycled by the OS and point a later
    # reclaim at a stranger (the exact-argv guard would refuse it, but a
    # cleared hint never even asks). _persist_hint runs the
    # read-modify-rewrite off the loop and does
    # not return — even if this handler is cancelled (e.g. aiohttp
    # aborting at shutdown) — until the write completes: the in-memory
    # teardown above is already done, so abandoning the persisted reset
    # would leave was_connected=True plus a stale local_port, reviving
    # an instance the user disconnected and pinning the freed port.
    hints: dict[str, object] = {
        "local_port": _UNALLOCATED_PORT,
        "forwarder_pid": _NO_FORWARDER_PID,
        "forwarder_start": "",
        "forwarder_sig": "",
        "forwarder_argv_sig": "",
    }
    if not keep_intent:
        hints["was_connected"] = False
    await self._persist_hint(self._registry.update, instance_id, **hints)
    return tunnel is not None


async def reconfigure(self: SshTunnelManager, instance_id: str, apply: Callable[[], _T]) -> _T:
    """Tear the tunnel down and rewrite its coordinates as ONE operation.

    Editing the host/port/transport of a live instance is two steps that must
    not be observed apart: with the lock released between them, a ``connect``
    can read the OLD record, and whether its tunnel is already CONNECTED or
    still CONNECTING when the write lands decides whether any after-the-fact
    sweep notices it. Holding the lock across both removes the window instead
    of narrowing it — a racing ``connect`` either completes before this (and
    is torn down here) or starts after (and reads the new coordinates).

    ``apply`` performs the persistence and returns whatever the caller needs;
    it runs on a worker thread because the registry write is blocking, so the
    event loop is not held while the lock is.
    """
    # The barrier goes up FIRST, with no await before it, so no recovery can
    # be scheduled for this instance from here on (see `_reconfiguring`).
    # Cancellation then runs OUTSIDE the lock, because a recovery task may
    # itself be waiting for that lock and awaiting it while holding the lock
    # would deadlock.
    self._reconfiguring.add(instance_id)
    try:
        # Self-heal rebuilds a tunnel from the record it read, so it is
        # stopped and unwound before the coordinates move. The refresh loop is
        # unwound by the teardown instead — AFTER the stop succeeds — because a
        # rejected edit must leave the live tunnel with its credential and its
        # refresh intact. The barrier already prevents either from restarting.
        await self._cancel_recovery(instance_id)
        return await self._reconfigure_locked(instance_id, apply)
    finally:
        self._reconfiguring.discard(instance_id)


async def _reconfigure_locked(
    self: SshTunnelManager, instance_id: str, apply: Callable[[], _T]
) -> _T:
    """The locked half of :meth:`reconfigure` (barrier already raised)."""
    async with self._lock:
        # Children first, and NOT tolerantly: their forwards were built from
        # this crew's coordinates, so an edit that moves this crew to another
        # machine leaves every `ssh -L ... <old host>` below it live and still
        # reporting CONNECTED -- serving the machine the user just left while
        # the record names the new one. `disconnect` suppresses a child's
        # teardown failure because it is shutting everything down anyway; here
        # a failure must abort the edit, for the same reason the parent's does.
        for child_id in await self._chained_below(instance_id):
            # keep_intent on the children: the user edited the PARENT. Clearing
            # a child's intent would silently un-remember it, so reconnecting
            # would not bring its crews back.
            await self._teardown_locked(child_id, keep_intent=True)
        # Deliberately NOT tolerant: a stop that failed leaves the old forward
        # live, so persisting the new coordinates would leave the record
        # pointing at one machine while the still-open tunnel serves another —
        # and that tunnel is the one the user would reach. The edit aborts,
        # the tunnel stays tracked (see _teardown_locked), and the caller is
        # told to disconnect and retry.
        await self._teardown_locked(instance_id, keep_intent=True)
        # The write is shielded: if this request's task is cancelled (the
        # client hung up), the worker thread keeps going regardless, and an
        # unshielded await would unwind the `async with` and release the lock
        # while that write was still in flight — letting a concurrent connect
        # read the pre-edit coordinates. Awaiting it out under the lock keeps
        # the critical section honest, then the cancellation propagates.
        write = asyncio.ensure_future(asyncio.to_thread(apply))
        try:
            return await asyncio.shield(write)
        except asyncio.CancelledError:
            await asyncio.wait({write})
            raise


async def shutdown(self: SshTunnelManager) -> None:
    """Tear down all tunnels (gateway shutdown). Leaves registry hints intact
    so lazy reconnect can revive the last-active instance next startup."""
    async with self._lock:
        # End every generation FIRST, before any await below: cancellation
        # of an in-flight self-heal can be swallowed inside
        # _SshTunnel.stop(), and a survivor that still saw its expected
        # stamp would reinstall a child after this cleanup. With the stamp
        # moved, its gated install and record refuse instead.
        for instance_id in list(self._tunnels):
            self._tunnel_epoch[instance_id] = self._tunnel_epoch.get(instance_id, 0) + 1
        # Cancel any in-flight self-heal so it can't resurrect a tunnel
        # after shutdown.
        for task in list(self._recovery_tasks):
            if not task.done():
                task.cancel()
        self._remote_updates.cancel_all()
        self._recover_attempts.clear()
        for instance_id in list(self._refresh_tasks):
            self._cancel_token_refresh(instance_id)
        ids = list(self._tunnels)
        for instance_id in ids:
            tunnel = self._tunnels.pop(instance_id, None)
            self._tokens.pop(instance_id, None)
            self._peer_sessions.pop(instance_id, None)
            self._chained_far_end.pop(instance_id, None)
            if tunnel is not None:
                with contextlib.suppress(Exception):
                    await tunnel.stop()
        # Every lent hop port goes with the process, so nothing stays bound by a
        # guard whose gateway is gone. The LEASE survives this, and
        # `sync_hop_holds` at the next startup is what re-takes the ports.
        #
        # Off the loop: `close_all` joins the reaper thread with a timeout, and a
        # join is a blocking wait -- on the event loop it stalls every other task
        # for as long as the thread takes to notice.
        await asyncio.to_thread(self._hop_guard.close_all)
        logger.info("All instance tunnels shut down (%d)", len(ids))


def status(self: SshTunnelManager, instance_id: str) -> TunnelStatus | None:
    """Return the live tunnel status for *instance_id*, or None if not live."""
    tunnel = self._tunnels.get(instance_id)
    return tunnel.status if tunnel is not None else None


def last_error(self: SshTunnelManager, instance_id: str) -> str | None:
    """Return the retained connect/reconnect failure reason, or None.

    Set by the connect path when an attempt fails (validation, port
    conflict, tunnel spawn, or token mint) and the failed tunnel is not
    retained as a live ERROR status; cleared on a successful connect or an
    explicit disconnect. Lets a sticky tab whose tunnel is down report *why*
    even though there is no live tunnel object to query.
    """
    return self._last_error.get(instance_id)


def status_all(self: SshTunnelManager) -> dict[str, TunnelStatus]:
    """Return live tunnel statuses keyed by instance id."""
    return {iid: t.status for iid, t in self._tunnels.items()}


def _error_status(self: SshTunnelManager, inst: Instance, message: str) -> TunnelStatus:
    """Build (and remember) an ERROR status for *inst* without a live tunnel.

    The message is retained in ``_last_error`` so a later :meth:`status`
    lookup — after the failed-connect tunnel has been popped — can still
    report *why* the instance is down. This is what lets a sticky tab whose
    tunnel never came up show its error instead of a bare "disconnected".
    """
    logger.warning("Instance %s connect error: %s", inst.id, message)
    self._last_error[inst.id] = message
    return TunnelStatus(
        instance_id=inst.id,
        state=TunnelState.ERROR,
        local_port=inst.local_port,
        remote_port=inst.remote_port,
        error=message,
    )
