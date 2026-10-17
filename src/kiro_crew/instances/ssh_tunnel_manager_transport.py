"""How a crew is reached and its own credential minted, for
``kiro_crew.instances.ssh_tunnel_manager``.

Validating an instance's coordinates into ``_TransportParams`` (its own, or its
parent's hop for a chained crew), the per-transport timeouts, the parent lookup,
gateway-id probe and cycle check of a chain, and minting the dashboard token over the
resolved transport: directly over ssh or SSM, or through the parent's gateway, with
the parent re-mint that path needs.

Each ``SshTunnelManager`` member here is a module-level function whose first parameter
is ``self``; the class in :mod:`kiro_crew.instances.ssh_tunnel_manager` binds it by
name, so the instance state, every patch of a method and every ``__new__`` build stay
on the one class.


Instance fields read: ``_connect_timeout``, ``_mint_timeout``, ``_mint_token``,
``_parent_port``, ``_reconfiguring``, ``_registry``, ``_tunnel_epoch``, ``_tunnels``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

import aiohttp

from kiro_crew.cloud.connect import FARGATE_TURN_PATH
from kiro_crew.gateway_identity import gateway_id
from kiro_crew.instances.constants import (
    CHAINED_MINT_REPLY_MAX_BYTES as _CHAINED_MINT_REPLY_MAX_BYTES,
)
from kiro_crew.instances.constants import DEFAULT_CHAINED_MINT_TIMEOUT_SECS as _CHAINED_MINT_TIMEOUT
from kiro_crew.instances.constants import (
    DEFAULT_CONNECT_TIMEOUT_SECS as _DEFAULT_CONNECT_TIMEOUT_SECS,
)
from kiro_crew.instances.constants import DEFAULT_MINT_TIMEOUT_SECS as _DEFAULT_MINT_TIMEOUT_SECS
from kiro_crew.instances.constants import (
    DEFAULT_SSM_CONNECT_TIMEOUT_SECS as _DEFAULT_SSM_CONNECT_TIMEOUT_SECS,
)
from kiro_crew.instances.constants import (
    DEFAULT_SSM_MINT_TIMEOUT_SECS as _DEFAULT_SSM_MINT_TIMEOUT_SECS,
)
from kiro_crew.instances.constants import DEFAULT_TOKEN_PROBE_TIMEOUT_SECS as _TOKEN_PROBE_TIMEOUT
from kiro_crew.instances.registry import _ID_RE as _INSTANCE_ID_RE
from kiro_crew.instances.registry import (
    CONNECTION_METHODS,
    SSM_TRANSPORT_METHODS,
    Instance,
    ancestor_ids,
    is_headless_provisioner,
    validate_ttl,
)
from kiro_crew.instances.ssh_tunnel_manager_child import TunnelState
from kiro_crew.instances.ssh_tunnel_manager_child_text import _LOOPBACK
from kiro_crew.instances.ssh_tunnel_manager_peer import _PeerUnavailable
from kiro_crew.instances.ssm_token_mint import mint_remote_token_ssm
from kiro_crew.instances.token_mint import HopRetiredError, TokenMintError
from kiro_crew.instances.validation import (
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

if TYPE_CHECKING:
    from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

#: The manager's own logger: this code logs under the facade module's name.
logger = logging.getLogger("kiro_crew.instances.ssh_tunnel_manager")


# The parent's own codes for "this hop is not mine any more". Anything else it
# answers is a failure to retry, not a hop to retire -- the distinction is what
# keeps a network blip from tearing down a working chain.
_HOP_RETIRED_CODES = frozenset({"instance_not_connected", "instance_not_found"})


@dataclass(frozen=True)
class _Mint:
    """A mint's whole answer, carried to the caller instead of published on the way.

    For a chained crew the parent states three things at once: the credential, the
    loopback ``hop`` its own forward to that crew listens on, and the ``ttl`` it
    issued the credential under. They belong together because they describe ONE
    forward, and publishing any of them before the caller's generation fence lets a
    superseded mint overwrite a current one -- including the hop that the credential
    check itself compares against, which would leave it comparing stale to stale.

    It also carries the parent's own IDENTITY for that hop -- the id the parent
    knows the crew by, and the generation of the parent's forward to it. A port
    number cannot serve as that identity: the parent's allocator hands a just-freed
    port to the next connect, so the same number names a different forward after
    ordinary churn, and a comparison on numbers clears exactly the case it exists to
    refuse.

    ``hop``, ``hop_id`` and ``ttl`` are empty for every unchained transport: those
    mint over a key this gateway holds, so there is no second party stating anything.
    """

    token: str
    hop: int = 0
    ttl: str = ""
    hop_id: str = ""
    hop_gen: int = -1
    #: The ``gateway_id`` the parent's own forward reaches for this crew, as the
    #: parent states it in the authenticated mint reply. ``None`` when the reply
    #: carries no such field (a parent build without it); ``""`` when the parent
    #: could not tell (a crew build that reports no id).
    crew_gateway_id: str | None = None


# Named by the path callers import it from: a traceback and an error chain render a
# class as ``module.qualname``, so this module's name stays out of that text.
_Mint.__module__ = "kiro_crew.instances.ssh_tunnel_manager"


@dataclass
class _TransportParams:
    """Validated, transport-specific connection parameters for one instance.

    Resolved once by :meth:`SshTunnelManager._resolve_transport` so the
    connect / rebuild / self-heal / token-refresh paths all build their tunnel
    and mint their token from the same validated values instead of each
    re-branching on ``connection_method``.
    """

    method: str  # "ssh" | "ssm" | "fargate"
    ssh_host: str = ""
    remote_bin: str = ""
    ssm_target: str = ""
    aws_profile: str = ""
    aws_region: str = ""
    ssm_run_as: str = ""
    #: Set only for a CHAINED instance: the parent instance whose already-open hop
    #: this forward rides, and the loopback port ON THAT PARENT where the parent's
    #: own forward to this crew listens. When present, ``ssh_host`` is the PARENT's
    #: host (that is who we dial) and ``forward_remote_port`` is what we forward
    #: to, so the instance's own ``ssh_host`` / ``remote_port`` are never dialled
    #: from here — this gateway has no route to them, which is the whole reason the
    #: chain exists.
    via_instance_id: str = ""
    via_remote_port: int = 0
    #: Whether the crew at the far end serves a turn route and no dashboard, from
    #: ``registry.is_headless_provisioner`` -- that function stays the one judge,
    #: and this only CARRIES its answer to the sites where the instance row is not
    #: in scope. :meth:`turn_url` is the reason it has to be carried at all: it
    #: has ``self`` and a port and nothing else.
    headless: bool = False

    @property
    def is_chained(self) -> bool:
        """Whether this forward rides another instance's hop."""
        return bool(self.via_instance_id)

    def forward_remote_port(self, own_remote_port: int) -> int:
        """The port the forward's far end targets.

        A chained instance targets its parent's loopback port; every other
        instance targets the crew's own gateway port.
        """
        return self.via_remote_port if self.is_chained else own_remote_port

    @property
    def target(self) -> str:
        """The human-facing target (ssh host, SSM instance id or ECS task) for messages."""
        return self.ssm_target if self.method in SSM_TRANSPORT_METHODS else self.ssh_host

    @property
    def forwards_over_ssm(self) -> bool:
        """Whether the forwarder child is ``aws ssm start-session``."""
        return self.method in SSM_TRANSPORT_METHODS

    def tunnel_kwargs(self) -> dict:
        """Transport kwargs for the ``_SshTunnel`` constructor.

        The tunnel child knows two argv shapes, ssh and the SSM port-forward. A
        fargate instance's child IS the SSM port-forward (aimed at an ECS task), so
        it is handed the ``ssm`` transport; what differs for fargate lives on the
        manager (no mint, no remote ``kirocrew``), not in the child.
        """
        return {
            "transport": "ssm" if self.forwards_over_ssm else "ssh",
            "ssm_target": self.ssm_target,
            "aws_profile": self.aws_profile,
            "aws_region": self.aws_region,
        }

    def turn_url(self, local_port: int) -> str:
        """The local turn-API URL for a HEADLESS crew's forward, ``""`` otherwise.

        Keyed on the crew, like the mint is, rather than on the method being
        ``fargate``. A MicroVM crew IS headless and reaches its guest over
        ``ssm``, so while this was method-keyed its status carried no turn URL and
        two things downstream read the wrong thing from that silence:

        * the forward's liveness probe went to ``/api/health`` instead of the
          front's ``/health`` -- harmless today only because the probe accepts any
          status line, and the front answers 403 there under the lane's strict
          posture;
        * ``status.turn_url`` stayed empty, so the pane's own ``_turn_target`` fell
          through to composing the URL from the connected port. That fallback
          works, which is why the pane was not broken -- but it means the value
          the far end states about itself is unused for exactly the lane that has
          one.
        """
        if self.method != "fargate" and not self.headless:
            return ""
        return f"http://{_LOOPBACK}:{local_port}{FARGATE_TURN_PATH}"


_TransportParams.__module__ = "kiro_crew.instances.ssh_tunnel_manager"


def _transport_method(inst: Instance) -> str:
    """The transport *inst* names, normalised; an absent or blank method is ``ssh``."""
    return (inst.connection_method or "").strip().lower() or "ssh"


def _unsupported_method_message(inst: Instance) -> str:
    return (
        f"crew {inst.id!r} uses connection method {inst.connection_method!r}, which this "
        f"version of Kiro Crew cannot connect over (supported: {', '.join(CONNECTION_METHODS)})"
    )


def _connect_timeout_for(self: SshTunnelManager, method: str) -> float:
    """Readiness timeout for *method*, honoring an explicit caller override.

    SSM's ``session-manager-plugin`` has to complete a WebSocket handshake
    with the SSM service before it binds the local port, which routinely
    takes longer than a direct ssh TCP connect — so the SSM default is
    higher. A caller that passed an explicit ``connect_timeout_secs``
    (tests, tuning) wins for both transports.
    """
    if self._connect_timeout is not None:
        return self._connect_timeout  # explicit override
    if method in SSM_TRANSPORT_METHODS:
        return _DEFAULT_SSM_CONNECT_TIMEOUT_SECS
    return _DEFAULT_CONNECT_TIMEOUT_SECS


def _mint_timeout_for(self: SshTunnelManager, method: str) -> float:
    """Token-mint timeout for *method*, honoring an explicit override.

    Mirrors :meth:`_connect_timeout_for`: the SSM mint dispatches
    ``aws ssm send-command`` and polls ``get-command-invocation``, whose
    dispatch latency (agent poll interval) makes its default higher. A
    caller that passed an explicit ``mint_timeout_secs`` (config, tests)
    wins for both transports — including a value equal to either
    transport's default.
    """
    if self._mint_timeout is not None:
        return self._mint_timeout  # explicit override
    if method == "ssm":
        return _DEFAULT_SSM_MINT_TIMEOUT_SECS
    return _DEFAULT_MINT_TIMEOUT_SECS


def _resolve_transport(
    self: SshTunnelManager, inst: Instance, parent: Instance | None = None
) -> _TransportParams:
    """Validate + resolve *inst*'s transport params immediately before use.

    Raises :class:`SshValidationError` / :class:`SsmValidationError` so each
    caller can surface a clean per-instance error. Validation happens here —
    right before a command line is built — rather than trusting the
    registry's lighter early-reject charset checks.

    A CHAINED instance is resolved first and separately: this gateway has no
    route to it, so what gets dialled is its PARENT's host and the parent's
    loopback port. The crew's own ``ssh_host`` / ``remote_port`` are left
    alone (they describe the crew on its own machine and name the row in the
    UI); dialling them from here is exactly the thing that does not work.
    """
    if inst.via_instance_id:
        return self._resolve_chained_transport(inst, parent)
    method = _transport_method(inst)
    if method not in CONNECTION_METHODS:
        raise UnsupportedConnectionMethodError(_unsupported_method_message(inst))
    if method == "fargate":
        target = validate_ssm_target(inst.ssm_target)
        # validate_ssm_target admits every SSM target shape; this method only
        # forwards to an ECS task, so an EC2 id is refused here rather than
        # handed to a forward that would reach a box with no turn API.
        if split_ecs_target(target) is None:
            raise SsmValidationError(
                f"ssm_target {target!r} must be an ECS task target "
                f"(ecs:<cluster>_<task-id>_<runtime-id>) for a fargate instance"
            )
        return _TransportParams(
            method="fargate",
            ssm_target=target,
            aws_profile=validate_aws_profile(inst.aws_profile),
            aws_region=validate_aws_region(inst.aws_region),
            headless=is_headless_provisioner(inst.provisioner_id),
        )
    if method == "ssm":
        target = validate_ssm_target(inst.ssm_target)
        # Connect-time mirror of the registry's ssm arm, for records stored
        # before the registry refused them: an ECS task has no SSM agent to
        # run ``kirocrew token`` on, so forwarding it would only fail later
        # at the mint with a generic error. Refuse here and name the method
        # that owns the target.
        if split_ecs_target(target) is not None:
            raise SsmValidationError(
                f"ssm_target {target!r} is an ECS task target; it belongs to the "
                f"fargate connection method, not ssm"
            )
        return _TransportParams(
            method="ssm",
            ssm_target=target,
            aws_profile=validate_aws_profile(inst.aws_profile),
            aws_region=validate_aws_region(inst.aws_region),
            ssm_run_as=validate_ssm_run_as(inst.ssm_run_as),
            remote_bin=validate_remote_bin(inst.remote_bin),
            headless=is_headless_provisioner(inst.provisioner_id),
        )
    return _TransportParams(
        method="ssh",
        ssh_host=validate_ssh_host(inst.ssh_host),
        remote_bin=validate_remote_bin(inst.remote_bin),
        headless=is_headless_provisioner(inst.provisioner_id),
    )


def _resolve_chained_transport(
    self: SshTunnelManager, inst: Instance, parent: Instance | None
) -> _TransportParams:
    """Resolve a chained instance's forward from its PARENT's coordinates.

    The forward dialled here is ``ssh -L <local>:127.0.0.1:<via_remote_port>
    <parent host>``: a second connection to the parent, targeting the loopback
    port where the parent's own forward to this crew already listens. Nothing
    is executed on the parent, so ``remote_bin`` is deliberately left empty —
    a chained instance's token is minted by the parent's own gateway over the
    hop it owns, never by a command this gateway builds.

    Refusals here are the reasons a chain cannot be ridden at all:

    * the parent is gone from the registry (removed while the child stayed);
    * the parent is itself reached over SSM, whose forwarder takes no second
      local forward from this gateway — ssm as the parent hop is a follow-up;
    * the hop port is not a port.
    """
    if parent is None:
        raise SshValidationError(
            f"instance {inst.id!r} is reached through {inst.via_instance_id!r}, which is "
            f"no longer configured. Remove this crew, or re-add it from the crew that "
            f"reaches it."
        )
    if parent.id == inst.id:
        raise SshValidationError(
            f"instance {inst.id!r} names itself as the crew it is reached through"
        )
    parent_method = (parent.connection_method or "ssh").strip().lower()
    if parent_method != "ssh":
        raise SshValidationError(
            f"crew {parent.id!r} is reached over {parent_method}, and a further crew can "
            f"only be chained through an ssh hop"
        )
    if not 1 <= inst.via_remote_port <= 65535:
        raise SshValidationError(
            f"invalid hop port {inst.via_remote_port!r} on {parent.id!r}: expected the "
            f"loopback port where that crew's own forward listens"
        )
    return _TransportParams(
        method="ssh",
        ssh_host=validate_ssh_host(parent.ssh_host),
        via_instance_id=parent.id,
        via_remote_port=inst.via_remote_port,
        headless=is_headless_provisioner(inst.provisioner_id),
    )


async def _with_parent(self: SshTunnelManager, inst: Instance) -> Instance | None:
    """The registry record this instance is reached THROUGH, or ``None``.

    Read off the event loop, like every other registry touch in this module.
    ``None`` for a top-level instance and for a parent id naming no record;
    :meth:`_resolve_chained_transport` tells those two apart, because only the
    second is an error.
    """
    if not inst.via_instance_id:
        return None
    return await asyncio.to_thread(self._registry.get, inst.via_instance_id)


async def _peer_gateway_id(self: SshTunnelManager, local_port: int) -> str:
    """The ``gateway_id`` reported at ``127.0.0.1:<local_port>``, or ``""``.

    ``""`` covers every "cannot tell": an unreachable port, a malformed
    reply, and a crew whose build predates the field. Callers that must tell
    those apart use :meth:`_peer_identity`.
    """
    return await self._peer_identity(local_port) or ""


async def _peer_identity(self: SshTunnelManager, local_port: int) -> str | None:
    """What ``127.0.0.1:<local_port>`` says about its own ``gateway_id``.

    ``/api/health`` needs no credential, and it reveals identity only to a
    direct-local caller — which is what a request through the loopback end of
    our own forward is. Three answers, kept apart because a decision that
    fails open for one of them must not fail open for the others:

    * the id, when the reply names one;
    * ``""`` when a well-formed health reply names none -- a crew whose
      build predates the field;
    * ``None`` when there is no such reply at all: unreachable, timed out,
      not 200, oversized or malformed.
    """
    url = f"http://{_LOOPBACK}:{int(local_port)}/api/health"
    try:
        timeout = aiohttp.ClientTimeout(total=_TOKEN_PROBE_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=False) as resp:
                if resp.status != 200:
                    return None
                raw = await resp.content.read(_CHAINED_MINT_REPLY_MAX_BYTES + 1)
                if len(raw) > _CHAINED_MINT_REPLY_MAX_BYTES:
                    return None
                payload = json.loads(raw)
    except Exception as e:
        logger.debug("gateway-id probe on port %d failed (%s)", local_port, type(e).__name__)
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    reported = payload.get("gateway_id")
    if reported is None:
        return ""
    return reported if isinstance(reported, str) and reported else None


async def _chain_cycle_reason(self: SshTunnelManager, inst: Instance, local_port: int) -> str:
    """Why *inst*'s freshly-opened chained forward closes a loop, or ``""``.

    Compares GATEWAY IDS, never host strings: one machine answers to many
    spellings (an ssh alias, an FQDN, an IP, ``localhost``), so a string
    comparison would refuse unrelated crews and still admit a real loop.

    Only a CHAINED forward is checked. A top-level instance that happens to
    point back at this gateway is what today's product already allows, and
    turning that into a refusal here would break a working setup over an
    arrangement this feature does not introduce.

    Fail-open on a crew that reports no id (a build older than the field):
    the loop it cannot rule out is a nested pane, not an escape from any
    boundary, and :data:`kiro_crew.instances.registry.MAX_VIA_HOPS` already
    bounds the arrangement to three levels. Refusing instead would make
    chaining unusable against every crew that has not been updated yet.
    """
    if not inst.via_instance_id:
        return ""
    reported = await self._peer_gateway_id(local_port)
    if not reported:
        logger.info("Crew %s reports no gateway id; chaining it without a cycle check", inst.id)
        return ""
    own = await asyncio.to_thread(gateway_id)
    if own and reported == own:
        return (
            "that crew is this dashboard reached through a loop. Chaining a crew back to "
            "the gateway showing it would nest this dashboard inside itself."
        )
    instances = await asyncio.to_thread(self._registry.list)
    for ancestor_id in ancestor_ids(instances, inst.id):
        st = self.status(ancestor_id)
        if st is None or st.state is not TunnelState.CONNECTED or st.local_port <= 0:
            continue
        if await self._peer_gateway_id(st.local_port) == reported:
            return (
                f"that crew is {ancestor_id!r} reached through a loop. A crew cannot be "
                f"chained back to one already carrying the hop."
            )
    return ""


async def _remint_parent_under_lock(self: SshTunnelManager, parent_id: str) -> bool:
    """Re-mint *parent_id*'s own credential WITHOUT reaching for the lock.

    :meth:`_mint_through_parent` can run inside :meth:`connect`'s
    ``async with self._lock``, so it cannot reach for :meth:`refresh_token`:
    that stores under the same lock, ``asyncio.Lock`` is not reentrant, and the
    acquire would never complete while the caller's own frame holds it -- a
    connect that hangs forever holding the lock, wedging every later connect,
    disconnect and self-heal.

    So this stores directly, and must be correct whether or not a lock is
    held, because its three callers differ: :meth:`connect` holds the lock
    across the mint, while :meth:`_refresh_token_once` and the self-heal's
    tier-2 mint deliberately do not. What makes the store safe in all three is
    the tunnel GENERATION, captured before the mint and compared after: an
    unheld lock lets the parent be disconnected and reconnected during a mint
    budget measured in tens of seconds, and membership alone cannot see that
    -- the new tunnel satisfies it, so the fresh credential would be
    overwritten by one minted against the generation before it.

    Returns ``False`` for every "cannot", leaving the 401 to be raised as a
    mint failure: a parent mid-reconfiguration, one that is not connected,
    coordinates that do not validate, a mint the parent's own remote refused,
    and a parent whose tunnel was replaced while this mint was in flight.
    """
    if parent_id in self._reconfiguring:
        return False
    inst = await asyncio.to_thread(self._registry.get, parent_id)
    if inst is None or parent_id not in self._tunnels:
        return False
    # Which tunnel generation this credential is being minted FOR. Captured
    # before the first await, compared before the store.
    epoch = self._tunnel_epoch.get(parent_id, 0)
    try:
        params = self._resolve_transport(inst, await self._with_parent(inst))
    except (SshValidationError, SsmValidationError) as e:
        logger.warning("Parent-hop re-mint aborted for %s: %s", parent_id, e)
        return False
    try:
        mint = await self._mint_for(inst, params)
    except TokenMintError as e:
        logger.warning("Parent-hop re-mint failed for %s: %s", parent_id, e)
        return False
    if not self._store_token(inst, mint, minted_at_epoch=epoch, binds_forward=False):
        return False
    logger.info("Parent-hop re-mint succeeded for %s", parent_id)  # value never logged
    return True


async def _mint_through_parent(
    self: SshTunnelManager, inst: Instance, params: _TransportParams
) -> _Mint:
    """Ask the parent crew to mint *inst*'s token with OUR embed parent port.

    A NARROW carrier, like :meth:`peer_capability`: the path is built here from
    the crew's id IN THE PARENT's registry, never supplied by a caller, and the
    only body field is the port this gateway serves its own dashboard on. It
    runs over the parent's already-open forward with the parent's port-scoped
    cookie, so:

    * the parent's credential never leaves this object and never reaches the
      browser — the pane's postMessage relay only ever carries "crew X is up
      on my port N", which is why a chained pane can be announced by untrusted
      frame code at all;
    * the minted token is the CHILD's, issued by the child's own gateway over
      the hop the parent owns, and it carries our embed parent port so the
      child's CSP admits this gateway's page as the pane's frame ancestor. The
      parent's own token for that child is untouched, so the parent's pane for
      it keeps working.

    A 401/403 gets exactly one transparent re-mint of the PARENT's credential
    and one retry, matching every other peer call here. That re-mint goes
    through :meth:`_remint_parent_under_lock` rather than the public refresh,
    because one of the two callers DOES hold the manager lock: ``connect``
    holds it across the mint, while the self-heal's tier-2 mint deliberately
    runs without it (see the "slow remote I/O WITHOUT the lock" phase). A
    lock-taking refresh would deadlock the first caller, so the store is
    gated on the tunnel generation instead, which is correct either way.
    That unheld lock is also why this method re-reads the parent's forward
    before spending the credential; see :meth:`_require_peer_forward`.
    Raises :class:`TokenMintError` on anything else, so the caller's existing
    mint-failure handling applies unchanged.
    """
    parent_id = params.via_instance_id
    # The id the PARENT knows this crew by. It is the only one the parent can
    # look up: our own id is derived from the name HERE and equals the parent's
    # only by luck, so a name collision, a rename there, or an explicitly
    # assigned id makes them differ and the parent answers 404 for a crew it
    # holds. A STORED id reaching a request path has also never been through
    # `validate` -- `Instance.from_dict` is deliberately tolerant, so a registry
    # file written by hand or by an agent can carry any string. Unchecked, an id
    # like `victim/disconnect?x=` would interpolate into a DIFFERENT
    # authenticated route on the parent and spend our credential for it there.
    # Refuse it, then still encode as exactly one segment.
    child_id = inst.via_remote_id
    if not _INSTANCE_ID_RE.match(child_id):
        raise TokenMintError(
            f"crew {inst.id!r} carries no usable id for that crew on {parent_id!r}, so the "
            f"parent cannot be asked to mint a token for it"
        )
    path = f"/api/instances/{quote(child_id, safe='')}/embed-token"
    body = json.dumps({"embed_parent_port": int(self._parent_port)}).encode("utf-8")
    # ONE budget for the whole call, retry included, so that when the caller
    # DOES hold the manager lock (``connect``) the ceiling a concurrent
    # connect or disconnect waits on is the ceiling of the CALL; a full budget
    # per attempt would double it.
    deadline = time.monotonic() + _CHAINED_MINT_TIMEOUT
    reminted = False
    for _attempt in range(2):
        try:
            url, cookie_name = self._peer_target(parent_id, path)
            stamp = self._peer_forward_stamp(parent_id)
            headers = await self._peer_headers_for(parent_id, url, cookie_name, stamp)
        except _PeerUnavailable as e:
            raise TokenMintError(
                f"crew {parent_id!r} is not connected, so it cannot mint a token for "
                f"{inst.id!r} ({e.message})"
            ) from None
        headers["Content-Type"] = "application/json"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TokenMintError(f"crew {parent_id!r} did not answer the mint in time")
        timeout = aiohttp.ClientTimeout(total=remaining)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url,
                    data=body,
                    headers=headers,
                    # Same SSRF reasoning as every other peer call: the
                    # loopback end of our own forward is the only legitimate
                    # target, so a compromised parent answering 30x must not
                    # redirect this mint anywhere.
                    allow_redirects=False,
                ) as resp:
                    if resp.status in (401, 403):
                        if not reminted and await self._remint_parent_under_lock(parent_id):
                            reminted = True
                            continue
                        raise TokenMintError(f"crew {parent_id!r} rejected our credential")
                    if not 200 <= resp.status < 300:
                        # Read BEFORE any status branch, because the status alone
                        # cannot tell the two 404s apart: a build with no chaining
                        # route and a parent that does not hold this crew both
                        # answer 404, and only the second means the hop is retired.
                        # Taken from the reply's `code`, never its sentence, which
                        # is prose and is allowed to change.
                        code = ""
                        with contextlib.suppress(Exception):
                            # Capped like the success read on this same wire: the
                            # reply comes from another gateway, so an error body
                            # must not be the one place that reads without a bound.
                            raw = await resp.content.read(_CHAINED_MINT_REPLY_MAX_BYTES + 1)
                            if len(raw) <= _CHAINED_MINT_REPLY_MAX_BYTES:
                                body = json.loads(raw)
                                if isinstance(body, dict):
                                    code = str(body.get("code") or "")
                        if code in _HOP_RETIRED_CODES:
                            # The parent still answers; what it answers is that the
                            # hop is not ours. Retrying cannot recover it. With the
                            # reservation at the document level the removed crew's
                            # port stays withheld, so this is not what keeps the
                            # credential out of another gateway -- prevention is.
                            # What it does is retire a forward that cannot work
                            # again, because the crew it reaches is gone from the
                            # parent, and the port it dials becomes the parent's to
                            # reassign once the reservation lapses.
                            raise HopRetiredError(
                                f"crew {parent_id!r} does not hold the hop to "
                                f"{inst.id!r} ({code})"
                            )
                        if resp.status in (404, 405):
                            raise TokenMintError(
                                f"crew {parent_id!r} did not mint for {child_id!r}: its "
                                f"build has no endpoint for chaining through it"
                            )
                        raise TokenMintError(
                            f"crew {parent_id!r} refused the mint (HTTP {resp.status})"
                        )
                    raw = await resp.content.read(_CHAINED_MINT_REPLY_MAX_BYTES + 1)
                    if len(raw) > _CHAINED_MINT_REPLY_MAX_BYTES:
                        raise TokenMintError(f"crew {parent_id!r} returned an oversized mint reply")
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        raise TokenMintError(
                            f"crew {parent_id!r} returned a malformed mint reply"
                        ) from None
                    token = payload.get("token") if isinstance(payload, dict) else None
                    if not isinstance(token, str) or not token:
                        raise TokenMintError(
                            f"crew {parent_id!r} returned no token for {inst.id!r}"
                        )
                    reported_ttl = payload.get("ttl")
                    # Untrusted like every other field, but REQUIRED: the
                    # parent issued this token, so it is the only party that
                    # knows when it dies. Our own record's TTL is not a safe
                    # stand-in -- it is a separate number, defaulting to 20h,
                    # and nothing holds it below the parent's, so scheduling
                    # the refresh from it can land after the token has already
                    # expired. A token we cannot describe is refused here, the
                    # same as one we cannot read: every gateway that answers
                    # this endpoint at all reports the TTL it issued.
                    if not isinstance(reported_ttl, str) or not reported_ttl:
                        raise TokenMintError(
                            f"crew {parent_id!r} minted a token for {inst.id!r} without "
                            f"saying how long it lasts"
                        )
                    try:
                        validate_ttl(reported_ttl)
                    except Exception:
                        raise TokenMintError(
                            f"crew {parent_id!r} reported an unusable lifetime for the "
                            f"token it minted for {inst.id!r}"
                        ) from None
                    # The hop port, from the one party entitled to name it. The
                    # row's copy arrived over a pane's postMessage and is what
                    # `ssh -L` aims at on the parent's machine, so a forged
                    # notice would otherwise choose which loopback-only service
                    # there this gateway forwards and then renders as a crew.
                    # This answer comes over the credential we hold for the
                    # parent, so it is the parent's own statement about its own
                    # forward. Shape-checked and dropped if unusable -- the
                    # caller refuses the dial rather than falling back to the
                    # pane's value.
                    #
                    # Nothing is published from here. All three values go back
                    # as one answer, and only the caller's fenced store writes
                    # them, because the hop this names is the very value the
                    # credential check reads.
                    # REQUIRED, with ttl's strictness rather than port's. A
                    # port that cannot be read is dropped and the caller refuses
                    # to dial; an identity that cannot be read is worse, because
                    # the dial would succeed and only the comparison guarding the
                    # credential would be weakened. The endpoint and this field
                    # ship in the same change, so no build can answer this route
                    # and omit them: a parent without the field is a parent
                    # without the route, which the 404 branch above already names.
                    reported_id = payload.get("hop_id")
                    if not isinstance(reported_id, str) or not reported_id:
                        raise TokenMintError(
                            f"crew {parent_id!r} minted a token for {inst.id!r} without "
                            f"naming which of its own crews the hop reaches"
                        )
                    reported_gen = payload.get("hop_gen")
                    if (
                        not isinstance(reported_gen, int)
                        or isinstance(reported_gen, bool)
                        or reported_gen < 0
                    ):
                        raise TokenMintError(
                            f"crew {parent_id!r} minted a token for {inst.id!r} without "
                            f"naming which generation of its forward the hop is"
                        )
                    reported_port = payload.get("port")
                    usable_port = (
                        reported_port
                        if (
                            isinstance(reported_port, int)
                            and not isinstance(reported_port, bool)
                            and 1 <= reported_port <= 65535
                        )
                        else 0
                    )
                    # Optional, because a parent build without the field still
                    # answers this route. A value that is not a string is
                    # treated as absent rather than as an identity.
                    stated_crew = payload.get("crew_gateway_id")
                    return _Mint(
                        token=token,
                        hop=usable_port,
                        ttl=reported_ttl,
                        hop_id=reported_id,
                        hop_gen=reported_gen,
                        crew_gateway_id=(stated_crew if isinstance(stated_crew, str) else None),
                    )
        except TokenMintError:
            raise
        except Exception as e:
            logger.info(
                "Chained mint for %s through %s failed (%s)",
                inst.id,
                parent_id,
                type(e).__name__,  # never the credential, never the token
            )
            raise TokenMintError(
                f"crew {parent_id!r} did not answer the mint ({type(e).__name__})"
            ) from None
    raise TokenMintError(f"crew {parent_id!r} rejected our credential")


async def _mint_for(self: SshTunnelManager, inst: Instance, params: _TransportParams) -> _Mint:
    """Mint a dashboard token for *inst* over its configured transport.

    The SSH path goes through the injectable ``self._mint_token`` seam (kept
    so the existing tests can substitute a fake mint); the SSM path calls
    :func:`mint_remote_token_ssm`. Never logs the token.

    A fargate instance has nothing to mint: the task runs no ``kirocrew`` and
    serves no dashboard. Every mint path (connect, self-heal, proactive and
    on-demand refresh) funnels through here, so refusing here is what keeps a
    later caller from dispatching ``kirocrew token`` at an ECS task.

    A CHAINED instance is minted by its PARENT, over the credential this
    gateway already holds for that parent — this gateway has no key for the
    crew itself, which is the whole reason the chain exists. The parent mints
    with OUR embed parent port, so the pane this gateway serves is the frame
    ancestor the crew's own CSP admits.
    """
    if params.is_chained:
        return await self._mint_through_parent(inst, params)
    if params.method == "fargate" or params.headless:
        raise TokenMintError(
            "a headless crew has no dashboard token: the far end serves only its "
            "turn API, reached at the tunnel's turn_url"
        )
    if params.method == "ssm":
        return _Mint(
            await mint_remote_token_ssm(
                params.ssm_target,
                aws_profile=params.aws_profile,
                aws_region=params.aws_region,
                ssm_run_as=params.ssm_run_as,
                remote_bin=params.remote_bin,
                ttl=inst.ttl,
                remote_port=inst.remote_port,
                embed_parent_port=self._parent_port,
                timeout_secs=self._mint_timeout_for(params.method),
            )
        )
    return _Mint(
        await self._mint_token(
            params.ssh_host,
            remote_bin=params.remote_bin,
            ttl=inst.ttl,
            remote_port=inst.remote_port,
            embed_parent_port=self._parent_port,
            timeout_secs=self._mint_timeout_for(params.method),
        )
    )
