"""Inbound SSH tunnel manager for the Instances feature.

Adapts the supervised-child + state-machine design of
``kiro_crew.tunnel.manager.TunnelManager`` (which points *outward* to expose the
dashboard) to point *inward*: for each connected remote instance it supervises a
local child process that forwards a loopback port to the remote Kiro Crew's
dashboard port, over one of two transports (``Instance.connection_method``):

* ``"ssh"`` (default): ``ssh -N -L 127.0.0.1:LP:127.0.0.1:RP <ssh_host>``.
* ``"ssm"``: ``aws ssm start-session --document-name
  AWS-StartPortForwardingSession --target <ssm_target> --parameters
  portNumber=RP,localPortNumber=LP`` — no inbound SSH port or SSH key needed,
  only IAM (``ssm:StartSession``) and the SSM agent on the remote box.

Design note: a literal ``ssh -fN`` would make ssh fork into the background and
the foreground process exit immediately, which would leave the gateway unable to
supervise or kill the real forwarder. A gateway-supervised child must stay in the
foreground, so we use ``-N`` (no remote command) *without* ``-f``, mirroring how
``TunnelManager`` supervises its own child. Connection multiplexing is pinned
off in the argv (``ControlPath=none``) for the same reason: it lets a user's
``~/.ssh/config`` recreate that fork-and-exit shape from outside this module.
``ExitOnForwardFailure=yes`` ensures
ssh exits if the local forward can't be bound, so a failed connect is detected
rather than hanging. The SSM transport gets the equivalent detection from the
generic ready-poll (:meth:`_Tunnel._wait_until_ready`) plus a post-hoc ownership
recheck, since the ``session-manager-plugin`` child does not expose an
``ExitOnForwardFailure``-style flag.

Scope (Phase 1 / Stage 4): connect, disconnect, status, and shutdown-all, with
port allocation + token mint wired in. The health-probe loop and 2-tier
self-heal are Phase 3 — this module exposes clean seams (an ``on_exit`` hook and
a per-instance state machine) for that follow-up without implementing it here.
SSM support reuses every one of those seams — it is a second *transport* plugged
into the same tunnel/state-machine/self-heal/token-refresh code, not a parallel
implementation.

Security (standard practices): loopback-bound forwards only (never ``0.0.0.0``);
child spawned via argv list (no local shell) for both transports; ``ssh_host`` /
``remote_bin`` (SSH) and ``ssm_target`` / ``aws_profile`` / ``aws_region`` (SSM)
injection-validated before use; minted tokens held in memory only and never
logged.
"""

from __future__ import annotations

import asyncio
import codecs  # noqa: F401
import contextlib  # noqa: F401
import enum  # noqa: F401
import hashlib  # noqa: F401
import hmac  # noqa: F401
import importlib as _importlib
import json  # noqa: F401
import logging
import re  # noqa: F401
import sys as _sys
import time  # noqa: F401
from collections.abc import Awaitable, Callable
from dataclasses import dataclass  # noqa: F401
from dataclasses import replace as dataclass_replace  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import TYPE_CHECKING, Any, NamedTuple, TypeVar  # noqa: F401
from urllib.parse import quote  # noqa: F401

import aiohttp  # noqa: F401

from kiro_crew import _facade_mirror, platform_compat  # noqa: F401
from kiro_crew.cloud import ssm as cloud_ssm  # noqa: F401
from kiro_crew.cloud.connect import FARGATE_HEALTH_PATH, FARGATE_TURN_PATH  # noqa: F401

# The local (embedding) gateway's configured port — carried into the minted
# remote token as the CSP frame-ancestor parent origin so the embedded pane can
# be framed by this desktop app on whatever KIROCREW_PORT it runs on (no
# hardcoded port, no wildcard). See server._extra_frame_ancestors.
from kiro_crew.config import live
from kiro_crew.config.loader import DASHBOARD_PORT as _LOCAL_DASHBOARD_PORT
from kiro_crew.deploy.engine import tool_spawn_env  # noqa: F401
from kiro_crew.gateway_identity import gateway_id  # noqa: F401
from kiro_crew.instances import ssh_tunnel_manager_lifecycle as _lifecycle
from kiro_crew.instances import ssh_tunnel_manager_peer as _peer
from kiro_crew.instances import ssh_tunnel_manager_ports as _ports
from kiro_crew.instances import ssh_tunnel_manager_remote_ops as _remote_ops
from kiro_crew.instances import ssh_tunnel_manager_self_heal as _self_heal
from kiro_crew.instances import ssh_tunnel_manager_tokens as _tokens
from kiro_crew.instances import ssh_tunnel_manager_transport as _transport
from kiro_crew.instances.constants import (  # noqa: F401
    CAPABILITY_REPLY_MAX_BYTES as _CAPABILITY_REPLY_MAX_BYTES,
)
from kiro_crew.instances.constants import (  # noqa: F401
    CHAINED_MINT_REPLY_MAX_BYTES as _CHAINED_MINT_REPLY_MAX_BYTES,
)
from kiro_crew.instances.constants import (  # noqa: F401
    DEFAULT_CAPABILITY_PROXY_TIMEOUT_SECS as _CAPABILITY_PROXY_TIMEOUT,
)
from kiro_crew.instances.constants import (  # noqa: F401
    DEFAULT_CHAINED_MINT_TIMEOUT_SECS as _CHAINED_MINT_TIMEOUT,
)
from kiro_crew.instances.constants import (  # noqa: F401
    DEFAULT_CONNECT_TIMEOUT_SECS as _DEFAULT_CONNECT_TIMEOUT_SECS,
)
from kiro_crew.instances.constants import DEFAULT_MAX_RECOVERY_ATTEMPTS as _MAX_RECOVERY
from kiro_crew.instances.constants import (  # noqa: F401
    DEFAULT_MINT_TIMEOUT_SECS as _DEFAULT_MINT_TIMEOUT_SECS,
)
from kiro_crew.instances.constants import (  # noqa: F401
    DEFAULT_MODELS_CAPABILITY_PROXY_TIMEOUT_SECS as _MODELS_CAPABILITY_PROXY_TIMEOUT,
)
from kiro_crew.instances.constants import DEFAULT_PROBE_FAILURE_THRESHOLD as _PROBE_FAILS
from kiro_crew.instances.constants import (  # noqa: F401
    DEFAULT_PROBE_HEALTH_TIMEOUT_SECS as _PROBE_HEALTH_TIMEOUT,
)
from kiro_crew.instances.constants import (  # noqa: F401
    DEFAULT_PROBE_INTERVAL_SECS as _PROBE_INTERVAL,
)
from kiro_crew.instances.constants import (  # noqa: F401
    DEFAULT_PROXY_CONNECT_TIMEOUT_SECS as _PROXY_CONNECT_TIMEOUT,
)
from kiro_crew.instances.constants import (  # noqa: F401
    DEFAULT_PROXY_READ_IDLE_TIMEOUT_SECS as _PROXY_READ_IDLE_TIMEOUT,
)
from kiro_crew.instances.constants import (
    DEFAULT_RECOVER_BACKOFF_MAX_SECS as _RECOVER_BACKOFF_MAX_SECS,
)
from kiro_crew.instances.constants import (  # noqa: F401
    DEFAULT_SEARCH_PROXY_TIMEOUT_SECS as _SEARCH_PROXY_TIMEOUT,
)
from kiro_crew.instances.constants import (  # noqa: F401
    DEFAULT_SESSION_TRANSFER_TIMEOUT_SECS as _TRANSFER_TIMEOUT,
)
from kiro_crew.instances.constants import (  # noqa: F401
    DEFAULT_SSM_CONNECT_TIMEOUT_SECS as _DEFAULT_SSM_CONNECT_TIMEOUT_SECS,
)
from kiro_crew.instances.constants import (  # noqa: F401
    DEFAULT_SSM_MINT_TIMEOUT_SECS as _DEFAULT_SSM_MINT_TIMEOUT_SECS,
)
from kiro_crew.instances.constants import (  # noqa: F401
    DEFAULT_TOKEN_PROBE_TIMEOUT_SECS as _TOKEN_PROBE_TIMEOUT,
)
from kiro_crew.instances.constants import (  # noqa: F401
    DEFAULT_TOKEN_REFRESH_FRACTION as _REFRESH_FRACTION,
)
from kiro_crew.instances.constants import (  # noqa: F401
    DEFAULT_TUNNEL_BASE_PORT,
)
from kiro_crew.instances.constants import (  # noqa: F401
    DIAGNOSTICS_CONNECT_TIMEOUT_CAP_SECS as _DIAGNOSTICS_CONNECT_TIMEOUT_CAP_SECS,
)
from kiro_crew.instances.constants import (  # noqa: F401
    LENT_HOP_TTL_CAP,
)
from kiro_crew.instances.constants import (  # noqa: F401
    SEARCH_REPLY_MAX_BYTES as _SEARCH_REPLY_MAX_BYTES,
)
from kiro_crew.instances.constants import (  # noqa: F401
    SESSION_IMPORT_MEMORY_WAIT_SECS as _IMPORT_MEMORY_WAIT,
)
from kiro_crew.instances.constants import (  # noqa: F401
    SESSION_TRANSFER_REPLY_MAX_BYTES as _TRANSFER_REPLY_MAX_BYTES,
)
from kiro_crew.instances.diagnostics import (  # noqa: F401
    DiagnosisResult,
    diagnose_instance,
    diagnose_instance_fargate,
    diagnose_instance_ssm,
)
from kiro_crew.instances.hop_port_guard import HopPortGuard
from kiro_crew.instances.port_allocator import (  # noqa: F401
    PortAllocator,
    _is_addr_free,
    _is_port_free,
)
from kiro_crew.instances.registry import _ID_RE as _INSTANCE_ID_RE  # noqa: F401
from kiro_crew.instances.registry import (  # noqa: F401
    _NO_FORWARDER_PID,
    _UNALLOCATED_PORT,
    CONNECTION_METHODS,
    MAX_VIA_HOPS,
    SSM_TRANSPORT_METHODS,
    Instance,
    InstanceDisabledError,
    InstancesRegistry,
    ancestor_ids,
    descendant_ids,
    is_headless_provisioner,
    validate_ttl,
)
from kiro_crew.instances.remote_update import (  # noqa: F401
    PHASE_RESTARTING,
    PHASE_VERIFYING,
    REMOTE_UPDATE_TIMEOUT_SECS,
    REMOTE_UPDATE_VERIFY_INTERVAL_SECS,
    REMOTE_UPDATE_VERIFY_SECS,
    STATE_SUCCEEDED,
    STATE_UNCHANGED,
    STATE_UNVERIFIED,
    RemoteUpdateJob,
    RemoteUpdateJobs,
    clean_version,
)
from kiro_crew.instances.ssh_tunnel_manager_child import TunnelState, _SshTunnel  # noqa: F401
from kiro_crew.instances.ssm_token_mint import (  # noqa: F401
    mint_remote_token_ssm,
    run_remote_kirocrew_ssm,
)
from kiro_crew.instances.token_mint import (  # noqa: F401
    PROXY_TOOL_MISSING_SIGNALS,
    HopRetiredError,
    TokenMintError,
    mint_remote_token,
    proxy_tool_missing_message,
    run_remote_kirocrew,
    ssh_spawn_argv_env,
    ttl_to_seconds,
)
from kiro_crew.instances.validation import (  # noqa: F401
    SshValidationError,
    SsmValidationError,
    UnsupportedConnectionMethodError,
    split_ecs_target,
    validate_aws_profile,
    validate_aws_region,
    validate_remote_bin,
    validate_ssh_host,
    validate_ssm_run_as,
    validate_ssm_target,
)
from kiro_crew.security import redact  # noqa: F401
from kiro_crew.sel import _HMAC_KEY_MIN_BYTES as _SEL_HMAC_KEY_MIN_BYTES  # noqa: F401
from kiro_crew.sel import sel, sel_hmac_key_path  # noqa: F401

logger = logging.getLogger(__name__)


class SshTunnelManager:
    """Manages per-instance tunnels (SSH or SSM) keyed by instance id.

    Holds the live tunnels, allocates loopback ports, mints per-instance tokens,
    and keeps the registry's ``was_connected`` / ``last_active`` hints in sync.
    Tokens are kept in memory only (never persisted, never logged) and handed to
    the API layer via :meth:`get_token`.

    The class name is retained (rather than renamed to a transport-neutral one)
    because it is referenced by ``dashboard/server.py`` and the existing test
    suite; it now supervises whichever transport each instance's
    ``connection_method`` selects.
    """

    # Connect, teardown, reconfigure and status: ``ssh_tunnel_manager_lifecycle``.
    apply_config = _lifecycle.apply_config
    _persist_hint = _lifecycle._persist_hint
    connect = _lifecycle.connect
    disconnect = _lifecycle.disconnect
    _chained_below = _lifecycle._chained_below
    _teardown_locked = _lifecycle._teardown_locked
    reconfigure = _lifecycle.reconfigure
    _reconfigure_locked = _lifecycle._reconfigure_locked
    shutdown = _lifecycle.shutdown
    status = _lifecycle.status
    last_error = _lifecycle.last_error
    status_all = _lifecycle.status_all
    _error_status = _lifecycle._error_status
    # Local forward ports, lent-hop holds and orphan reclaim: ``ssh_tunnel_manager_ports``.
    _forwarder_identity_hints = _ports._forwarder_identity_hints
    _reserved_ports = _ports._reserved_ports
    _port_recorded_by_another_row = _ports._port_recorded_by_another_row
    sync_hop_holds = _ports.sync_hop_holds
    hop_ownership_violations = _ports.hop_ownership_violations
    _apply_hop_holds = _ports._apply_hop_holds
    _reclaim_orphan_forwarder = _ports._reclaim_orphan_forwarder
    _hold_lent_port_now = _ports._hold_lent_port_now
    _hold_lent_port_by_number = _ports._hold_lent_port_by_number
    # Reaching a crew and minting its credential: ``ssh_tunnel_manager_transport``.
    _connect_timeout_for = _transport._connect_timeout_for
    _mint_timeout_for = _transport._mint_timeout_for
    _resolve_transport = _transport._resolve_transport
    _resolve_chained_transport = _transport._resolve_chained_transport
    _with_parent = _transport._with_parent
    _peer_gateway_id = _transport._peer_gateway_id
    _peer_identity = _transport._peer_identity
    _chain_cycle_reason = _transport._chain_cycle_reason
    _remint_parent_under_lock = _transport._remint_parent_under_lock
    _mint_through_parent = _transport._mint_through_parent
    _mint_for = _transport._mint_for
    # Credential custody, refresh and lending: ``ssh_tunnel_manager_tokens``.
    _minted_ttl = _tokens._minted_ttl
    _lent_hop_ttl = staticmethod(_tokens._lent_hop_ttl)
    mint_embed_token = _tokens.mint_embed_token
    _answer_embed_mint = _tokens._answer_embed_mint
    _mint_token_with_parent_port = _tokens._mint_token_with_parent_port
    _cancel_token_refresh_and_wait = _tokens._cancel_token_refresh_and_wait
    get_token = _tokens.get_token
    token_ttl_remaining = _tokens.token_ttl_remaining
    token_ttl_total = _tokens.token_ttl_total
    _mint_superseded = _tokens._mint_superseded
    _credential_forward_moved = _tokens._credential_forward_moved
    _store_token = _tokens._store_token
    _cancel_token_refresh = _tokens._cancel_token_refresh
    _schedule_chained_retirement = _tokens._schedule_chained_retirement
    _retire_chained = _tokens._retire_chained
    _schedule_token_refresh = _tokens._schedule_token_refresh
    _refresh_delay = _tokens._refresh_delay
    _token_refresh_loop = _tokens._token_refresh_loop
    _refresh_token_once = _tokens._refresh_token_once
    refresh_token = _tokens.refresh_token
    # Self-heal after a child exits: ``ssh_tunnel_manager_self_heal``.
    _track_recovery = _self_heal._track_recovery
    _cancel_recovery = _self_heal._cancel_recovery
    _on_tunnel_healthy = _self_heal._on_tunnel_healthy
    _on_tunnel_exit = _self_heal._on_tunnel_exit
    _recover_after = _self_heal._recover_after
    _rebuild = _self_heal._rebuild
    _mark_recovered = _self_heal._mark_recovered
    _recover = _self_heal._recover
    # Operations on a crew's own gateway: ``ssh_tunnel_manager_remote_ops``.
    diagnose = _remote_ops.diagnose
    restart_remote = _remote_ops.restart_remote
    _remote_command_target = _remote_ops._remote_command_target
    _run_remote_subcommand = _remote_ops._run_remote_subcommand
    start_remote_update = _remote_ops.start_remote_update
    _update_then_restart = _remote_ops._update_then_restart
    _peer_version = _remote_ops._peer_version
    _await_peer_version_change = _remote_ops._await_peer_version_change
    remote_update_job = _remote_ops.remote_update_job
    _schedule_diagnosis = _remote_ops._schedule_diagnosis
    # Requests over a connected crew's forward: ``ssh_tunnel_manager_peer``.
    _chained_far_end_holds = _peer._chained_far_end_holds
    _instance_on_port = _peer._instance_on_port
    token_validates = _peer.token_validates
    _peer_target = _peer._peer_target
    _peer_forward_stamp = _peer._peer_forward_stamp
    _require_peer_forward = _peer._require_peer_forward
    _peer_headers_for = _peer._peer_headers_for
    _exchange_link = _peer._exchange_link
    _peer_cookie_header = _peer._peer_cookie_header
    _prime_peer_session = _peer._prime_peer_session
    proxy_request = _peer.proxy_request
    send_session_bundle = _peer.send_session_bundle
    peer_capability = _peer.peer_capability
    peer_version = _peer.peer_version
    search_sessions_remote = _peer.search_sessions_remote

    def __init__(
        self,
        registry: InstancesRegistry,
        *,
        base_port: int = DEFAULT_TUNNEL_BASE_PORT,
        connect_timeout_secs: float | None = None,
        ssh_compression: bool = True,
        max_recovery_attempts: int = _MAX_RECOVERY,
        recover_backoff_max_secs: float = _RECOVER_BACKOFF_MAX_SECS,
        probe_failure_threshold: int = _PROBE_FAILS,
        mint_timeout_secs: float | None = None,
        mint_token: Callable[..., Awaitable[str]] = mint_remote_token,
        tunnel_factory: Callable[..., _SshTunnel] | None = None,
        parent_port: int | None = None,
    ) -> None:
        self._registry = registry
        # The port the embedding dashboard ACTUALLY bound, carried into every
        # minted remote token as the CSP frame-ancestor parent origin.
        #
        # Falls back to the configured value only when the caller cannot supply
        # the real one. The distinction matters: ``DASHBOARD_PORT`` is derived
        # from env/config at import time, so in the desktop app — which resolves
        # its own port but spawns the backend without passing it through — the
        # two disagree, the claim names a port the parent is not served on, and
        # the remote's ``frame-ancestors`` then blocks the iframe ("Pane failed
        # to load"). The gateway already knows its real port (``app["port"]``,
        # passed to ``_register_instances_hooks``), so it is threaded in here
        # rather than re-derived.
        self._parent_port = parent_port if parent_port else _LOCAL_DASHBOARD_PORT
        self._allocator = PortAllocator(base_port=base_port)
        self._connect_timeout = connect_timeout_secs
        self._ssh_compression = ssh_compression
        # Self-heal tunables (config-tunable via instances.*): max consecutive
        # recovery attempts before give-up, the cap on the per-attempt backoff,
        # and the per-tunnel consecutive-probe-failure teardown threshold.
        self._max_recovery = max_recovery_attempts
        self._recover_backoff_max = recover_backoff_max_secs
        self._probe_fails = probe_failure_threshold
        self._mint_timeout = mint_timeout_secs
        self._mint_token = mint_token
        self._tunnel_factory = tunnel_factory or _SshTunnel
        self._tunnels: dict[str, _SshTunnel] = {}
        self._tokens: dict[str, str] = {}
        #: instance_id -> (link token it was exchanged from, session cookie value).
        #: Keyed on the link so a re-mint silently retires the old session.
        self._peer_sessions: dict[str, tuple[str, str]] = {}
        # Last connect/reconnect failure reason per instance, retained after the
        # failed tunnel is popped so a sticky tab whose tunnel is down can still
        # report *why* (e.g. a startup auto-revive that couldn't reach the host).
        # Cleared on a successful connect or an explicit disconnect.
        self._last_error: dict[str, str] = {}
        # The TTL a CHAINED crew's current token was actually issued under, keyed
        # by our id for that crew. A chained token is minted by the parent under
        # the PARENT's record for the crew, which this gateway cannot know: our own
        # row defaults to 20h, so scheduling the refresh from it would place the
        # refresh after a shorter token has already expired. Written by the chained
        # mint that produced the token, so it always describes the token held.
        self._chained_ttl: dict[str, str] = {}
        # The hop port the PARENT reported for a chained crew, from the same mint
        # reply. Kept separate from the row because the row's copy came over a
        # pane's postMessage: this one is the parent's own statement, and it is
        # what the forward is allowed to aim at.
        self._chained_hop_port: dict[str, int] = {}
        #: The parent's own identity -- (its id for the crew, its forward's
        #: generation) -- for the hop each chained forward here was BUILT against.
        #: Written only by ``_store_token``, read only by
        #: ``_credential_forward_moved``: it is what makes that comparison an
        #: identity rather than a port number, which a reused port defeats.
        self._chained_hop_identity: dict[str, tuple[str, int]] = {}
        #: The ``gateway_id`` of the crew behind each chained forward: the one its
        #: parent states in the mint reply, which the far end must also report when
        #: the forward is built (or, for a parent build that states none, what the
        #: far end reported then). The forward's local end is a loopback port on the
        #: PARENT's host, which that port's owner can release (a parent gateway that
        #: exits frees it) while the credential for the crew behind it stays valid;
        #: whatever binds it next answers over the same ``ssh -L``. It is compared
        #: before any credential travels (:meth:`_chained_far_end_holds`), which
        #: stops a listener that merely took the port. The id is public to a
        #: direct-local caller, so a process that read it from the hop earlier and
        #: replays it passes this check; the lent lifetime cap still bounds that.
        self._chained_far_end: dict[str, str] = {}
        # Retirement tasks, held so they are not garbage-collected mid-teardown.
        self._retirements: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        #: OS-level ownership of ports we have lent as chained hops. The lease in the
        #: registry keeps a lent port out of THIS allocator; this keeps it out of every
        #: other process on the host, which is what the lease alone could not do.
        #: Deliberately NOT armed here -- constructing a manager must not bind ports.
        #: Armed by :meth:`sync_hop_holds`, which the gateway calls at startup, and by
        #: every teardown that frees a lent port.
        self._hop_guard = HopPortGuard()
        #: port -> lease deadline, mirrored from the registry at the moment
        #: ``lend_hop`` succeeds. Exists so the SYNCHRONOUS exit seam can bind a
        #: freed lent port with zero ``await``; the registry remains authoritative
        #: and this is rebuilt from it at startup and pruned on every settle.
        self._lent_hops: dict[int, float] = {}
        # Lends whose registry write is STILL IN FLIGHT. `_lent_hops` is re-seeded
        # wholesale from the registry on every settle, so an entry registered before
        # its write completes would be wiped by a settle landing in that window --
        # reopening the very gap registering early closes. These are unioned back in
        # there, and each is removed the moment its write resolves either way.
        self._pending_lends: dict[int, float] = {}
        # Self-heal: consecutive recovery attempts per instance (reset on a
        # successful rebuild) + live recovery task refs (stored so they aren't
        # GC'd mid-flight; cancelled on shutdown).
        self._recover_attempts: dict[str, int] = {}
        self._recovery_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        # Same tasks, indexed by instance: a reconfiguration must cancel the
        # recovery in flight for ITS instance only, and a recovery that
        # captured the pre-edit record cannot be allowed to reinstall it.
        self._recovery_by_instance: dict[str, set[asyncio.Task]] = {}  # type: ignore[type-arg]
        # The explicit "Update remote" jobs: the latest one per instance plus
        # the task refs that keep each running job alive until it finishes.
        self._remote_updates = RemoteUpdateJobs()
        # The post-restart version check's window and cadence; attributes so a
        # test can close the window without waiting it out.
        self._remote_update_verify_secs = REMOTE_UPDATE_VERIFY_SECS
        self._remote_update_verify_interval_secs = REMOTE_UPDATE_VERIFY_INTERVAL_SECS
        # Instances whose coordinates are being rewritten right now. Self-heal
        # reads the record before it takes the lock, so cancelling the recoveries
        # in flight is not enough on its own: a tunnel exiting mid-edit schedules
        # a FRESH recovery that would read the pre-edit record. This barrier is
        # set before the first await of a reconfiguration and cleared after the
        # write, and recovery refuses to run for an instance named in it — which
        # closes the window instead of racing it with a retry loop.
        self._reconfiguring: set[str] = set()
        # Generation counter per instance, bumped every time a tunnel is
        # INSTALLED and every time one is torn down (_teardown_locked): both
        # events end the generation a slow unlocked mint or rebuild ran for.
        # A mint runs without the lock, so the tunnel it was minted for can be torn
        # down and replaced while it is in flight; `instance_id in self._tunnels` is
        # then true again and cannot tell the generations apart. The stamp can:
        # a token is stored only if the tunnel it belongs to is still the current
        # one. Coverage, mint path by mint path: connect() mints inside its
        # critical section, so nothing can replace the tunnel mid-mint and it
        # needs no stamp; _refresh_token_once — and refresh_token(), the
        # request-driven path the embedded dashboard calls, which delegates to
        # it and is not a task in `_refresh_tasks`, so it cannot be cancelled by
        # name — compares the stamp under the lock before its store; the
        # self-heal tier-2 re-mint does the same before its store.
        self._tunnel_epoch: dict[str, int] = {}
        # Proactive token refresh: per-instance refresh task + the mint timestamp
        # / ttl so the TTL-remaining can be surfaced (Stage 6).
        self._refresh_tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        self._token_minted_at: dict[str, float] = {}
        self._token_ttl_secs: dict[str, int] = {}
        # The transport tunables above are copies of instances.*, so a config write
        # reaches them only through apply_config(). Held on self because the watcher
        # holds the owner weakly. ``fail_closed=False``: the section carries no
        # authorization, so a degraded document's defaults are the right answer.
        self._config_sub = live.watch_section(
            self,
            "instances",
            method="apply_config",
            fail_closed=False,
            name="SshTunnelManager",
        )


# --------------------------------------------------------------------------- #
# Facade. ``SshTunnelManager`` binds every member but ``__init__`` from the owner module
# that holds its body (one header per owner above). The module-level helpers, constants and
# classes live in those owners too, and every name this module bound stays readable here:
# an import above stays bound (``# noqa: F401`` where only owners read it), ``_SshTunnel``
# and ``TunnelState`` are bound because ``__init__`` and ``TunnelStatus``'s annotations read
# them here, and every other definition that moved is served by ``_facade_mirror`` from the
# owner that defines it. A write or delete through this module reaches every owner binding
# the same object, which is every reader the one namespace had. No owner imports this
# module at run time. ``test/test_ssh_tunnel_manager_bindings.py`` pins the members, the
# binding and the reach.
# --------------------------------------------------------------------------- #
#: Owner modules, lowest layer first: an owner imports only owners listed before it,
#: and the first holder of a served name answers its reads.
_OWNERS: tuple[str, ...] = (
    "kiro_crew.instances.ssh_tunnel_manager_child_text",
    "kiro_crew.instances.ssh_tunnel_manager_child",
    "kiro_crew.instances.ssh_tunnel_manager_peer",
    "kiro_crew.instances.ssh_tunnel_manager_transport",
    "kiro_crew.instances.ssh_tunnel_manager_ports",
    "kiro_crew.instances.ssh_tunnel_manager_tokens",
    "kiro_crew.instances.ssh_tunnel_manager_lifecycle",
    "kiro_crew.instances.ssh_tunnel_manager_self_heal",
    "kiro_crew.instances.ssh_tunnel_manager_remote_ops",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Every definition that left this module for an owner: still read here, it is served
#: from the owner that defines it. Names an owner adds beside them are not part of this
#: module.
_SERVED = frozenset(
    {
        "ProxyRequestError",
        "TunnelStatus",
        "_ANSI_CSI_RE",
        "_BANNER_DETAIL_MAX_CHARS",
        "_BENIGN_SSH_STDERR_MARKERS",
        "_C0_CONTROL_RE",
        "_FIRST_CONNECT_DROP_HINT",
        "_HOP_RETIRED_CODES",
        "_LOOPBACK",
        "_MAX_STDERR_CHARS",
        "_MAX_STDOUT_CHARS",
        "_Mint",
        "_Moved",
        "_PEER_CAPABILITY_PATHS",
        "_PEER_SIZE_REFUSALS",
        "_PeerUnavailable",
        "_READY_POLL_INTERVAL_SECS",
        "_RECLAIM_KILL_GRACE_SECS",
        "_RECLAIM_POLL_INTERVAL_SECS",
        "_RECLAIM_SIG_DOMAIN",
        "_RECLAIM_TERM_GRACE_SECS",
        "_RECOVER_BACKOFF_BASE_SECS",
        "_RecoverySuperseded",
        "_SSH_AUTH_SIGNALS",
        "_SSH_BIND_SIGNALS",
        "_SSH_TRANSPORT_DROP_SIGNALS",
        "_SSM_BIND_SIGNALS",
        "_SSM_CLOSED_NO_REASON_ANCHOR",
        "_SSM_CLOSED_WITH_REASON_ANCHOR",
        "_SSM_CREDENTIAL_SIGNALS",
        "_SSM_DENIAL_SIGNALS",
        "_SSM_IDLE_CLOSE_SIGNALS",
        "_SSM_PLUGIN_SIGNALS",
        "_SSM_REASON_SEPARATOR",
        "_SSM_RESUME_TIMED_OUT_ANCHOR",
        "_SSM_START_FAILED_ANCHOR",
        "_SSM_TARGET_SIGNALS",
        "_STDERR_DRAIN_GRACE_SECS",
        "_STDOUT_READ_CHUNK",
        "_T",
        "_TREE_KILL_UNDELIVERED",
        "_TransportParams",
        "_UPLOAD_CHUNK_BYTES",
        "_WINDOWS_SSH_TUNNEL_UNSUPPORTED",
        "_build_ssh_tunnel_argv",
        "_build_ssm_tunnel_argv",
        "_first_hit",
        "_forwarder_identity_sig",
        "_forwarder_orphan_state",
        "_read_capability_body",
        "_read_transfer_reply",
        "_reclaim_identity_key",
        "_recover_backoff_secs",
        "_sanitize_banner",
        "_sanitize_ssm_stdout",
        "_ssm_close_reason",
        "_strip_benign_ssh_noise",
        "_transport_method",
        "_unsupported_method_message",
        "_upload_chunks",
        "_verify_and_reclaim_forwarder",
    }
)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(
    _facade_mirror.install(
        _sys.modules[__name__], _OWNERS, excluded=(), surface=frozenset(globals()) | _SERVED
    ).served
)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: Bound here for the ``if TYPE_CHECKING:`` blocks alone: a star import binds the names it
#: bound before the owners existed.
_NOT_EXPORTED = frozenset({"TYPE_CHECKING"})
__all__ = sorted(
    n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_") and n not in _NOT_EXPORTED
)
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.instances.ssh_tunnel_manager_child import (  # noqa: F401
        _MAX_STDERR_CHARS,
        _MAX_STDOUT_CHARS,
        _READY_POLL_INTERVAL_SECS,
        _STDERR_DRAIN_GRACE_SECS,
        _STDOUT_READ_CHUNK,
        _TREE_KILL_UNDELIVERED,
        _WINDOWS_SSH_TUNNEL_UNSUPPORTED,
        TunnelStatus,
    )
    from kiro_crew.instances.ssh_tunnel_manager_child_text import (  # noqa: F401
        _ANSI_CSI_RE,
        _BANNER_DETAIL_MAX_CHARS,
        _BENIGN_SSH_STDERR_MARKERS,
        _C0_CONTROL_RE,
        _FIRST_CONNECT_DROP_HINT,
        _LOOPBACK,
        _SSH_AUTH_SIGNALS,
        _SSH_BIND_SIGNALS,
        _SSH_TRANSPORT_DROP_SIGNALS,
        _SSM_BIND_SIGNALS,
        _SSM_CLOSED_NO_REASON_ANCHOR,
        _SSM_CLOSED_WITH_REASON_ANCHOR,
        _SSM_CREDENTIAL_SIGNALS,
        _SSM_DENIAL_SIGNALS,
        _SSM_IDLE_CLOSE_SIGNALS,
        _SSM_PLUGIN_SIGNALS,
        _SSM_REASON_SEPARATOR,
        _SSM_RESUME_TIMED_OUT_ANCHOR,
        _SSM_START_FAILED_ANCHOR,
        _SSM_TARGET_SIGNALS,
        _build_ssh_tunnel_argv,
        _build_ssm_tunnel_argv,
        _first_hit,
        _sanitize_banner,
        _sanitize_ssm_stdout,
        _ssm_close_reason,
        _strip_benign_ssh_noise,
    )
    from kiro_crew.instances.ssh_tunnel_manager_lifecycle import (  # noqa: F401
        _T,
    )
    from kiro_crew.instances.ssh_tunnel_manager_peer import (  # noqa: F401
        _PEER_CAPABILITY_PATHS,
        _PEER_SIZE_REFUSALS,
        _UPLOAD_CHUNK_BYTES,
        ProxyRequestError,
        _PeerUnavailable,
        _read_capability_body,
        _read_transfer_reply,
        _upload_chunks,
    )
    from kiro_crew.instances.ssh_tunnel_manager_ports import (  # noqa: F401
        _RECLAIM_KILL_GRACE_SECS,
        _RECLAIM_POLL_INTERVAL_SECS,
        _RECLAIM_SIG_DOMAIN,
        _RECLAIM_TERM_GRACE_SECS,
        _forwarder_identity_sig,
        _forwarder_orphan_state,
        _reclaim_identity_key,
        _verify_and_reclaim_forwarder,
    )
    from kiro_crew.instances.ssh_tunnel_manager_self_heal import (  # noqa: F401
        _RECOVER_BACKOFF_BASE_SECS,
        _recover_backoff_secs,
        _RecoverySuperseded,
    )
    from kiro_crew.instances.ssh_tunnel_manager_tokens import (  # noqa: F401
        _Moved,
    )
    from kiro_crew.instances.ssh_tunnel_manager_transport import (  # noqa: F401
        _HOP_RETIRED_CODES,
        _Mint,
        _transport_method,
        _TransportParams,
        _unsupported_method_message,
    )
