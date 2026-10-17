"""Custody of every credential after it is minted, for
``kiro_crew.instances.ssh_tunnel_manager``.

Deciding whether a mint still pairs with the live forward, storing it, the proactive
refresh loop, retiring a chained forward whose hop moved, the token and lifetime
readers, and lending a capped credential to another gateway's pane.

Each ``SshTunnelManager`` member here is a module-level function whose first parameter
is ``self``; the class in :mod:`kiro_crew.instances.ssh_tunnel_manager` binds it by
name, so the instance state, every patch of a method and every ``__new__`` build stay
on the one class.

Instance fields written: ``_chained_hop_identity``, ``_chained_hop_port``,
``_chained_ttl``, ``_last_error``, ``_lent_hops``, ``_pending_lends``,
``_refresh_tasks``, ``_retirements``, ``_token_minted_at``, ``_token_ttl_secs``,
``_tokens``.

Instance fields read: ``_lock``, ``_mint_token``, ``_reconfiguring``, ``_registry``,
``_tunnel_epoch``, ``_tunnels``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, NamedTuple

from kiro_crew.instances.constants import DEFAULT_TOKEN_REFRESH_FRACTION as _REFRESH_FRACTION
from kiro_crew.instances.constants import LENT_HOP_TTL_CAP
from kiro_crew.instances.registry import MAX_VIA_HOPS, Instance
from kiro_crew.instances.ssh_tunnel_manager_child import TunnelState
from kiro_crew.instances.ssh_tunnel_manager_transport import _Mint, _TransportParams
from kiro_crew.instances.ssm_token_mint import mint_remote_token_ssm
from kiro_crew.instances.token_mint import HopRetiredError, TokenMintError, ttl_to_seconds
from kiro_crew.instances.validation import SshValidationError, SsmValidationError

if TYPE_CHECKING:
    from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

#: The manager's own logger: this code logs under the facade module's name.
logger = logging.getLogger("kiro_crew.instances.ssh_tunnel_manager")


class _Moved(NamedTuple):
    """Why a credential was discarded, and whether the forward must go too.

    Two fields rather than one string because the caller needs an answer no string
    can carry safely: a superseded GENERATION means this gateway rebuilt its own
    forward, so tearing it down would destroy what a heal just repaired, while a
    disagreeing IDENTITY means the forward still in place is riding a hop that
    changed hands. Sniffing the reason text for the difference would make the
    prose load-bearing; this makes the decision say it.
    """

    reason: str
    retire: bool


# Named by the path callers import it from: a traceback and an error chain render a
# class as ``module.qualname``, so this module's name stays out of that text.
_Moved.__module__ = "kiro_crew.instances.ssh_tunnel_manager"


def _minted_ttl(self: SshTunnelManager, inst: Instance) -> str:
    """The TTL a freshly minted token for *inst* should be stored under.

    A CHAINED crew's token was issued by its parent, under the parent's own
    record for that crew; every other token was issued under ours. The mint
    refuses a reply that does not state a usable lifetime, so the parent's
    answer is present for every chained token that exists.

    Returns the SHORTER of the parent's answer and our own record's, which is
    what makes the refresh schedule safe in both directions: shorter than the
    token's real life only costs an early re-mint, while longer would schedule
    it after the token is already dead. Taking the minimum means that holds
    however the two numbers are configured, rather than resting on an
    assumption about which is bigger.
    """
    if not inst.via_instance_id:
        return inst.ttl
    reported = self._chained_ttl.get(inst.id)
    if not reported:
        return inst.ttl
    try:
        return reported if ttl_to_seconds(reported) < ttl_to_seconds(inst.ttl) else inst.ttl
    except Exception:
        # Both were validated where they were accepted, so this is a guard
        # rather than a path: the shorter-looking answer is the safe pick.
        return reported


def _lent_hop_ttl(inst: Instance) -> str:
    """The lifetime a credential minted for ANOTHER gateway's pane is issued for.

    The SHORTER of the crew's own row TTL and :data:`LENT_HOP_TTL_CAP`, taken as
    a minimum rather than an assignment so a row configured shorter than the cap
    keeps its own figure.

    Three things must be this one number, which is why they read it from here
    instead of from the row: the lifetime the remote crew issues the token for,
    the deadline the hop lease is recorded under, and the TTL the answer reports
    to the asking hub. The lease exists to withhold the port for exactly as long
    as the credential can be used, and the hub schedules its re-mint from the
    reported figure -- so a site reading the row while the others read the cap
    would either free the port under a live credential or schedule a re-mint
    after the token is already dead.

    This gateway's OWN pane is not lent and is not capped: it goes through
    :meth:`_mint_for`, which reads the row.
    """
    try:
        if ttl_to_seconds(inst.ttl) <= ttl_to_seconds(LENT_HOP_TTL_CAP):
            return inst.ttl
    except Exception:
        # The row was validated where it was accepted, so this is a guard rather
        # than a path. The cap is the safe pick: an unreadable row cannot be
        # shown to be shorter.
        return LENT_HOP_TTL_CAP
    return LENT_HOP_TTL_CAP


async def mint_embed_token(
    self: SshTunnelManager, instance_id: str, embed_parent_port: int
) -> tuple[bool, dict]:
    """Mint *instance_id*'s token for ANOTHER gateway's pane.

    The counterpart of :meth:`_mint_through_parent`, from this side of the hop:
    a hub that reaches this crew by riding our forward has no key for it, so we
    mint over the transport we already hold and hand the token back, carrying
    the HUB's dashboard port as the embed-parent claim rather than our own.

    No CREDENTIAL is stored. The token we keep for this crew is the one OUR pane
    loads with, and overwriting it with one scoped to someone else's page would
    break our own pane for the crew we just helped somebody else reach.

    One thing IS recorded: the hop we lent and when the token we issued against
    it expires, in the registry document's own port-keyed lease table (see
    :meth:`InstanceRegistry.lend_hop`). Until that moment the port is withheld
    from allocation -- see :meth:`_reserved_ports` -- because the hub rides the
    port NUMBER, and handing that number to another crew while the hub's token
    is still valid is what delivers one crew's credential to a different crew's
    gateway. It is kept on the DOCUMENT and not on this crew's row because
    removing the crew deletes its row while the hub's token stays live. The
    deadline is the TTL we issued, so this asks nothing of the hub and is not a
    lease.

    Returns ``(ok, payload)``. On success the payload carries the ``token``, the
    loopback ``port`` our own forward listens on, which is the hop the hub
    forwards to, and the ``ttl`` the token was issued under -- OUR record's TTL
    for this crew, which the hub cannot know and must not assume: its own row
    for the crew defaults to 20h, and scheduling a refresh from that would put
    the refresh after a shorter token has already expired. The token and the
    port are one claim, not two facts: the mint is a multi-second round trip
    taken without the manager lock, so the hop is read once before it and
    confirmed unmoved after, and a hop that moved is refused rather than
    answered. On failure the payload carries ``error`` / ``code`` plus the HTTP
    ``status`` the route should answer with, so the handler translates without
    string-matching.
    """
    inst = await asyncio.to_thread(self._registry.get, instance_id)
    if inst is None:
        return False, {"error": "not found", "code": "instance_not_found", "status": 404}
    if inst.via_instance_id:
        # The depth cap, seen from this end. The asking hub counted hops in
        # its own registry and cannot see that we reach this crew through a
        # further one; only we can, so only we can refuse it.
        return False, {
            "error": (
                f"this dashboard reaches {inst.name} through a further crew, so chaining "
                f"it once more would go past the {MAX_VIA_HOPS}-hop limit"
            ),
            "code": "chain_too_deep",
            "status": 400,
        }
    st = self.status(instance_id)
    if st is None or st.state is not TunnelState.CONNECTED or st.local_port <= 0:
        return False, {
            "error": f"{inst.name} is not connected here, so there is no hop to ride",
            "code": "instance_not_connected",
            "status": 409,
        }
    # The hop and the generation, captured together and read again after the
    # mint. `status()` hands out the tunnel's LIVE status object, and a
    # teardown pops that tunnel without zeroing the port on it, so the
    # captured value survives the forward it described; the allocator then
    # hands that exact port to the next connect first, because it takes the
    # first free port above the base and the teardown returned this one to
    # the free set. Reading the port after the await would therefore name
    # another crew's forward. Nothing between here and the mint awaits, so
    # these two readings describe one moment.
    hop = int(st.local_port)
    epoch = self._tunnel_epoch.get(instance_id, 0)
    try:
        params = self._resolve_transport(inst)
    except (SshValidationError, SsmValidationError) as e:
        return False, {
            "error": f"invalid {inst.connection_method} settings: {e}",
            "code": "instance_invalid",
            "status": 400,
        }
    # Computed ONCE, here, and threaded to all three places that must agree: the
    # lifetime the remote crew issues the token for, the lease deadline below, and
    # the `ttl` this answer reports. Three reads of the row would be three chances
    # for them to disagree.
    lent_ttl = self._lent_hop_ttl(inst)
    try:
        token = await self._mint_token_with_parent_port(
            inst, params, int(embed_parent_port), ttl=lent_ttl
        )
    except TokenMintError as e:
        return False, {
            "error": f"token mint failed: {e}",
            "code": "instance_mint_failed",
            "status": 502,
        }
    # Which gateway our own forward to this crew reaches, read with no
    # credential. The hub compares it with what answers at the far end of ITS
    # forward before it sends anything, so it learns the crew's identity from
    # us, over our credential, rather than from whatever holds the hop port.
    # The generation check under the lock below confirms the forward probed is
    # the one this answer names.
    crew_gateway_id = await self._peer_identity(hop)
    if crew_gateway_id is None:
        # Without an answer we cannot tell the hub which crew the hop reaches,
        # and an empty statement would read as "a crew too old to say", which
        # leaves the hub's forward ungated. So no credential is issued.
        return False, {
            "error": (
                f"{inst.name} did not answer the identity check over our forward, "
                "so no token was minted for another dashboard; try again"
            ),
            "code": "instance_identity_unconfirmed",
            "status": 502,
        }
    # The same decision, from the one exit that RETURNS a credential instead of
    # storing it. It passes an identity-less mint because it is not riding a hop:
    # it IS the hop, so the identity branch has nothing to compare and the two
    # readings below are its own.
    # Held from the validation through the reservation write, because those two are
    # one decision: the hop is only worth naming if it is still ours when the
    # record that protects it lands. Validating outside the lock and writing inside
    # leaves a gap the size of the acquire, and the gap is the whole hazard -- a
    # teardown and reconnect there would have us reserve a port the crew does not
    # serves while answering with the one it does. Safe to take here because the
    # only caller is the HTTP route; the hub-side mint is the one that can run under
    # this lock, and it goes through `_remint_parent_under_lock` instead.
    async with self._lock:
        return await self._answer_embed_mint(
            inst,
            instance_id,
            token,
            hop,
            epoch,
            lent_ttl=lent_ttl,
            crew_gateway_id=crew_gateway_id,
        )


async def _answer_embed_mint(
    self: SshTunnelManager,
    inst: Instance,
    instance_id: str,
    token: str,
    hop: int,
    epoch: int,
    *,
    lent_ttl: str,
    crew_gateway_id: str,
) -> tuple[bool, dict]:
    """Validate the hop and record it as lent, then answer. Caller holds the lock.

    Split out so the lock is held across exactly these two steps and nothing else:
    the mint above takes seconds and must not hold it, and the answer below must not
    be assembled from readings taken before it.

    *lent_ttl* is the lifetime the credential was ACTUALLY minted under, passed in
    rather than re-derived, because the lease deadline and the reported figure have
    to be that same number: a second derivation here could disagree with the one
    the remote crew was asked for, and the disagreement would either free the port
    under a live credential or have the hub re-mint after its token is dead.

    *crew_gateway_id* is required, with no default: an empty statement tells the
    hub "a crew too old to say" and leaves its forward ungated, so a caller that
    forgot to read the identity must fail to call this rather than fail open.
    """
    moved = self._credential_forward_moved(inst, _Mint(""), epoch, binds_forward=False).reason
    if not moved:
        # Two readings this exit adds, because they are its own: it hands back
        # the LOCAL port our forward listens on, where a store cares about the
        # remote hop the forward dials, and it answers only for a crew that is
        # serving right now -- where the self-heal stores deliberately while
        # their tunnel sits in ERROR, which is why they are separate.
        live = self._tunnels[instance_id]
        if live.status.state is not TunnelState.CONNECTED:
            moved = "this forward is no longer connected"
        elif int(live.status.local_port or 0) != hop:
            moved = f"this forward now listens on another port than the {hop} we named"
    if moved:
        # The hop this token was minted against is not the hop we would name.
        # Answering anyway hands the asking hub a credential for THIS crew
        # plus a port that a crew connected during the mint can already own,
        # and the hub forwards to whatever now listens there. The shared
        # decision above is the same one every credential STORE makes, so this
        # exit cannot drift from them.
        return False, {
            "error": (
                f"our forward to {inst.name} changed while its token was being minted, "
                "so the token is not paired with a hop we can name; try again"
            ),
            "code": "instance_hop_changed",
            "status": 409,
        }
    # The reservation is a PRECONDITION of handing the token over, not a note
    # taken on the way out. Three things make it one.
    #
    # It runs under the manager lock, together with a re-reading of the hop. The
    # checks above ran before it, and this method takes multi-second awaits, so a
    # teardown and reconnect can move the hop in between -- which would record a
    # port the crew does not serve while answering with the one it does.
    #
    # The write propagates its error instead of going through `_persist_hint`,
    # whose documented semantics are best-effort (`except Exception: pass`). That
    # is right for a hint that only has to be usually-there; it is wrong for the
    # record that keeps this port away from another crew, because a swallowed
    # failure would hand out a credential with nothing protecting its hop.
    #
    # And a reservation that cannot be written REFUSES the mint. A credential this
    # gateway cannot protect is one it does not issue.
    #
    # The deadline is the SAME ttl the payload promises, so the port is withheld for
    # exactly as long as the token can be used. `lend_hop` takes the later of this
    # and any standing lease, so a second hub's mint cannot shorten one an earlier
    # mint earned.
    #
    # It is also what bounds the window a gateway exit opens: `close_all` releases
    # every hold, because a socket cannot outlive its process, and the credential
    # stays valid because the remote crew issued it. So the exposure after an exit
    # runs to this deadline and no further, which is why the lent lifetime is
    # capped (:meth:`_lent_hop_ttl`) rather than taken from the row.
    until = time.time() + ttl_to_seconds(lent_ttl)
    # Registered BEFORE the write, not after it. `lend_hop` runs in a thread, and
    # the synchronous exit seam answers from this cache alone (`_hold_lent_port_now`)
    # precisely so it can bind with no `await`. Populating the cache only after the
    # write meant an ssh exit landing INSIDE the thread hop found no lease, bound
    # nothing, and the allocator handed the freed port to the next connect -- while
    # the credential for it had already been returned. Both values are known here,
    # before any await, so there is nothing to wait for: the port is `hop` and the
    # deadline is the ttl this payload promises.
    #
    # This is deliberately the pessimistic order. A lease recorded for a write that
    # then fails withholds a port for nothing until the unwind below puts the entry
    # back as it found it; a lease recorded too late hands out a credential with an
    # unprotected port. The first costs a port, the second costs the credential.
    #
    # Both caches take the LATER deadline, the same max-wins rule the durable record
    # and the guard's own hold already follow: a mint for a hop whose lease is still
    # live must not shorten it, because the credential that earned the longer deadline
    # stays valid until ITS deadline and the synchronous exit seam reads these tables
    # alone. Capture what is there first, so a failed write can put it back rather
    # than delete a lease this call never earned.
    prior_pending = self._pending_lends.get(hop)
    prior_lent = self._lent_hops.get(hop)
    registered = max(until, prior_pending or 0.0, prior_lent or 0.0)
    self._pending_lends[hop] = registered
    self._lent_hops[hop] = registered
    try:
        await asyncio.to_thread(self._registry.lend_hop, hop, until)
    except Exception as e:
        # Unwind the early registration, but only if it is still OURS: a concurrent
        # mint for the same hop may have replaced it, and writing blindly would
        # damage a live lease this call never owned. Putting the CAPTURED value back
        # rather than popping is what keeps an earlier lease intact -- this call may
        # have been the one that raised the deadline, and deleting the entry would
        # leave the only table the exit seam can read with no record of a hop whose
        # credential is still valid, which is the window the early registration
        # above exists to close.
        if self._pending_lends.get(hop) == registered:
            if prior_pending is None:
                self._pending_lends.pop(hop, None)
            else:
                self._pending_lends[hop] = prior_pending
        if self._lent_hops.get(hop) == registered:
            if prior_lent is None:
                self._lent_hops.pop(hop, None)
            else:
                self._lent_hops[hop] = prior_lent
        # NOT through `_persist_hint`, whose documented semantics are best-effort
        # (`except Exception: pass`). That is right for a hint that only has to be
        # usually-there and wrong for the record that keeps this port away from
        # another crew: a swallowed failure would hand out a credential with
        # nothing protecting its hop. A reservation that cannot be written refuses
        # the mint, because a credential this gateway cannot protect is one it does
        # not issue.
        logger.warning(
            "Refusing to mint for %s: could not record the lent hop (%s)",
            instance_id,
            type(e).__name__,
        )
        return False, {
            "error": (
                f"could not record that {inst.name}'s hop is lent, so the token "
                f"would not be protected against the port being reused"
            ),
            "code": "instance_hop_lease_failed",
            "status": 503,
        }
    # The write resolved, so the registry names this lease and the settle's re-seed
    # will carry it on its own. Only the in-flight marker is dropped; the cache entry
    # registered before the write stays exactly as it was. The comparison is against
    # what this call WROTE, and it puts back what it captured, so a marker a
    # concurrent mint owns is neither erased nor mistaken for this one's.
    if self._pending_lends.get(hop) == registered:
        if prior_pending is None:
            self._pending_lends.pop(hop, None)
        else:
            self._pending_lends[hop] = prior_pending
    return True, {
        "token": token,
        "port": hop,
        # The lifetime the token was actually issued for, which the hub cannot know
        # and must not assume: its own row for this crew defaults to 20h, and
        # scheduling a re-mint from that would place it long after this credential
        # is dead. The hub takes the shorter of this and its row.
        "ttl": lent_ttl,
        # OUR identity for this hop, which is what makes the asking hub's later
        # comparison an identity rather than a number. `hop_id` is the id we know
        # the crew by -- the hub asked us by it, so it is ours to confirm -- and
        # `hop_gen` is the generation of our own forward to it, verified unmoved
        # by the reading just above. A hub holding (id, generation) can tell our
        # rebuilt forward from the one it dialled even when the port is identical.
        "hop_id": inst.id,
        "hop_gen": epoch,
        # The gateway our forward to this crew reaches, ``""`` when the crew
        # reports none. The hub requires the far end of its own forward to
        # answer with this before any credential goes over it.
        "crew_gateway_id": crew_gateway_id,
    }


async def _mint_token_with_parent_port(
    self: SshTunnelManager,
    inst: Instance,
    params: _TransportParams,
    embed_parent_port: int,
    *,
    ttl: str,
) -> str:
    """Mint *inst*'s token carrying *embed_parent_port* as the frame-ancestor claim.

    The same two transport calls :meth:`_mint_for` makes, with the embed-parent
    port supplied instead of read from this gateway's own config. Kept beside
    :meth:`_mint_for` rather than folded into it because the two answer
    different questions — "a token for MY pane" and "a token for a hub's pane" —
    and a flag on one method would make every existing mint site read as
    though it had a choice about that.

    *ttl* is required rather than defaulted from ``inst.ttl``: this is the mint
    whose credential leaves for another gateway, so its lifetime is the capped
    one its caller computed (:meth:`_lent_hop_ttl`), and the same number has to
    reach the lease and the answer. A default would let a future caller mint an
    uncapped credential without saying so.
    """
    if params.method == "fargate" or params.headless:
        raise TokenMintError(
            "a headless crew has no dashboard token: the far end serves only its "
            "turn API, reached at the tunnel's turn_url"
        )
    if params.method == "ssm":
        return await mint_remote_token_ssm(
            params.ssm_target,
            aws_profile=params.aws_profile,
            aws_region=params.aws_region,
            ssm_run_as=params.ssm_run_as,
            remote_bin=params.remote_bin,
            ttl=ttl,
            remote_port=inst.remote_port,
            embed_parent_port=embed_parent_port,
            timeout_secs=self._mint_timeout_for(params.method),
        )
    return await self._mint_token(
        params.ssh_host,
        remote_bin=params.remote_bin,
        ttl=ttl,
        remote_port=inst.remote_port,
        embed_parent_port=embed_parent_port,
        timeout_secs=self._mint_timeout_for(params.method),
    )


async def _cancel_token_refresh_and_wait(self: SshTunnelManager, instance_id: str) -> None:
    """Cancel this instance's refresh loop and WAIT for it to unwind.

    ``_cancel_token_refresh`` only signals. A refresh already inside its mint
    would otherwise finish afterwards and store a token minted from the
    pre-edit coordinates against the tunnel the edit rebuilt — the embedded
    dashboard would then be handed a credential the new remote never issued.
    """
    task = self._refresh_tasks.get(instance_id)
    self._cancel_token_refresh(instance_id)
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


def get_token(self: SshTunnelManager, instance_id: str) -> str:
    """Return the in-memory token for a connected instance, or ``""``.

    Callers must not log the result. Exists so the API layer can hand the
    token to the browser for the embedded iframe's first-party cookie.
    """
    return self._tokens.get(instance_id, "")


def token_ttl_remaining(self: SshTunnelManager, instance_id: str) -> int | None:
    """Seconds until the current token reaches its TTL, or None if unknown.

    Used by the Manage panel (Stage 6) to show "token TTL remaining".
    """
    minted = self._token_minted_at.get(instance_id)
    ttl = self._token_ttl_secs.get(instance_id)
    if minted is None or ttl is None:
        return None
    return max(0, int(ttl - (time.time() - minted)))


def token_ttl_total(self: SshTunnelManager, instance_id: str) -> int | None:
    """Seconds the CURRENT token was issued for, or None if unknown.

    The companion of :meth:`token_ttl_remaining`, and the reason it exists is
    that a caller comparing the two must divide by the same number the
    remaining was derived from. A crew's row TTL is NOT that number for a
    chained crew: its token is issued by the parent, so the stored total is
    the parent's figure (or ours, whichever is shorter), and a reader that
    measured progress against the row would read a 1h token as permanently
    past a 20h row's refresh threshold and re-mint on every poll.
    """
    return self._token_ttl_secs.get(instance_id)


# ── proactive token refresh ────────────────────────────────────────────


def _mint_superseded(self: SshTunnelManager, instance_id: str, minted_at_epoch: int) -> str:
    """Why a mint's answer must be thrown away, or ``""`` -- the shared core.

    Two readings that apply to EVERYTHING a mint said, its hop and its lifetime
    as much as its credential: the forward it ran for is gone, or another
    forward took its place. Membership reads true again after any reinstall, so
    the generation counter is what tells one from the next.
    """
    if instance_id not in self._tunnels:
        return "the forward it was minted for is gone"
    if self._tunnel_epoch.get(instance_id, 0) != minted_at_epoch:
        return "another forward took its place while the mint was in flight"
    return ""


def _credential_forward_moved(
    self: SshTunnelManager,
    inst: Instance,
    mint: _Mint,
    minted_at_epoch: int,
    *,
    binds_forward: bool,
) -> _Moved:
    """Why a CREDENTIAL for *inst* must not be used, and whether to retire.

    THE one place the pairing between a credential and the forward it will
    travel over is decided. A dashboard token is a bearer credential for ONE
    crew, and it reaches that crew only over the forward it was minted against;
    deliver it over a different forward and it reaches whoever is behind that
    one instead.

    :meth:`_mint_superseded`'s two readings, then two that belong to a
    credential alone.

    The port is read FIRST and only as a cheap early-out: a forward dialling a
    different number than the mint named is plainly the wrong forward. It cannot
    be what CLEARS the check, because the parent's allocator hands a just-freed
    port to the next connect, so equal numbers are exactly what a reused port
    looks like -- and that is the case this exists to refuse.

    What clears it is the parent's own IDENTITY for the hop: the id it knows the
    crew by and the generation of its forward to it, both stated in the reply.
    Compared against the identity the forward HERE was built against, so a parent
    that tore its forward down and rebuilt it -- possibly handing the old port to
    another crew in between -- fails to match, whatever the numbers say.

    ``binds_forward`` says which question is being asked. A store that accompanies
    this gateway building or rebuilding the forward onto the hop this mint named
    is DEFINING that identity, so there is nothing yet to contradict; a store for
    a forward already up is asking whether it still rides the hop it was built
    for, and that one compares.
    """
    superseded = self._mint_superseded(inst.id, minted_at_epoch)
    if superseded:
        return _Moved(superseded, False)
    live = self._tunnels[inst.id]
    riding = int(getattr(live.status, "remote_port", 0) or 0)
    if mint.hop and riding and mint.hop != riding:
        return _Moved(
            f"the crew holding the hop now serves it on port {mint.hop} "
            f"while this forward still dials {riding}",
            # Retires for the same reason the identity branch below does, and under
            # the same condition. The parent moved this crew to a new port and can
            # hand the old one to ANOTHER crew, so the forward still dialling the
            # old number now reaches that crew instead -- and the token already in
            # `_tokens` keeps travelling over it. Refusing the new credential alone
            # left both in place with nothing to correct them: each refresh cycle
            # re-discarded and retried, so the stale pair survived indefinitely.
            #
            # Gated on `not binds_forward` for the reason that flag exists: a store
            # accompanying this gateway BUILDING the forward onto the hop the mint
            # named is defining that pairing, and retiring there would tear down the
            # forward it is in the middle of establishing.
            not binds_forward,
        )
    if mint.hop_id and not binds_forward:
        built_for = self._chained_hop_identity.get(inst.id)
        if built_for is not None and built_for != (mint.hop_id, mint.hop_gen):
            return _Moved(
                f"this forward was built against {built_for[0]!r} generation "
                f"{built_for[1]} and the crew holding the hop now answers for "
                f"{mint.hop_id!r} generation {mint.hop_gen}",
                # Retires for the same reason as the port branch above: the forward
                # already in place is riding a hop that changed hands behind an
                # unchanged port number, and refusing the new credential leaves it
                # there. This branch is already reached only when `binds_forward` is
                # false, so it needs no further gate.
                True,
            )
    return _Moved("", False)


def _store_token(
    self: SshTunnelManager,
    inst: Instance,
    mint: _Mint,
    *,
    minted_at_epoch: int,
    binds_forward: bool,
) -> bool:
    """Publish everything a mint said, or DISCARD all of it. Never logs the token.

    Answers whether the credential was stored. A discarded mint leaves the
    previous credential in place, which is the safe side: it may be stale, and a
    stale token produces a 403 the client recovers from, where a token delivered
    over another crew's forward is a disclosure nothing downstream re-checks.

    BOTH sides of the pairing pass through here, which is the point. The reply's
    hop and lifetime are written FIRST and behind the generation fence, so a
    superseded mint cannot overwrite the hop that the credential comparison
    itself reads -- fencing one side only leaves that comparison reading a stale
    value against a stale forward and passing. The credential is stored behind
    that comparison, which by then is asking whether the forward rides the hop
    THIS mint named.

    The effective lifetime is derived HERE and is not a parameter, because it
    depends on the publish this method has just done.

    ``minted_at_epoch`` and ``binds_forward`` are both required, so a caller
    cannot store without saying which forward it minted against and whether this
    store is the one DEFINING that forward's hop identity. Every credential store
    in this module goes through here, so a site added later cannot forget any of
    it.
    """
    superseded = self._mint_superseded(inst.id, minted_at_epoch)
    if superseded:
        # Worded without the word for what was minted: the SAST logging rule
        # matches that keyword in a format string, and a nothing-was-leaked log
        # line is not worth a suppression comment.
        logger.info("Discarding a mint for %s: %s", inst.id, superseded)
        return False
    # Compared BEFORE the identity is published, because the identity is what it
    # compares against: publishing first would compare this mint's answer with
    # itself. The port and lifetime are published first, as before, since the
    # port's witness is the forward's own `remote_port` rather than the record.
    if mint.hop:
        self._chained_hop_port[inst.id] = mint.hop
    if mint.ttl:
        self._chained_ttl[inst.id] = mint.ttl
    moved = self._credential_forward_moved(inst, mint, minted_at_epoch, binds_forward=binds_forward)
    if moved.reason:
        logger.info("Discarding a mint for %s: %s", inst.id, moved.reason)
        if moved.retire:
            # Refusing the new credential is not enough on its own: the forward
            # and the token already in place are the ones riding the moved hop,
            # and nothing else retires them. Both non-defining mismatch branches
            # ask for this -- the port number moved, or the identity behind an
            # unchanged number did -- and the fence above is why that is
            # sufficient: a superseded generation has already been rejected, so it
            # never reaches this line and a rebuilt forward cannot be torn down by
            # a mint that lost a race to it. Neither branch asks while
            # `binds_forward` is set, so a forward being established is never the
            # thing retired.
            self._schedule_chained_retirement(inst.id, moved.reason)
        return False
    if mint.hop_id:
        self._chained_hop_identity[inst.id] = (mint.hop_id, mint.hop_gen)
    self._tokens[inst.id] = mint.token
    self._token_minted_at[inst.id] = time.time()
    with contextlib.suppress(Exception):
        # Read HERE, after the publish above. _minted_ttl takes the shorter of
        # the parent's answer and our record's, and the parent's answer is what
        # that publish just wrote -- so a caller computing this would read the
        # dict before its own mint had reached it, store our record's default,
        # and schedule the refresh past a shorter token's expiry. Taking the
        # reading inside removes that ordering instead of asking each site to
        # respect it.
        self._token_ttl_secs[inst.id] = ttl_to_seconds(self._minted_ttl(inst))
    return True


def _cancel_token_refresh(self: SshTunnelManager, instance_id: str) -> None:
    """Cancel + drop an instance's refresh task and token metadata."""
    task = self._refresh_tasks.pop(instance_id, None)
    if task is not None and not task.done():
        task.cancel()
    self._token_minted_at.pop(instance_id, None)
    self._token_ttl_secs.pop(instance_id, None)


def _schedule_chained_retirement(self: SshTunnelManager, instance_id: str, reason: str) -> None:
    """Retire a chained forward that rides a hop other than the one it was built for.

    Scheduled rather than awaited, and that is not a style choice: the callers
    differ in whether they hold the manager lock. `_store_token` runs inside
    `connect`'s lock AND outside it from the refresh, and ``asyncio.Lock`` is not
    reentrant -- awaiting a teardown from under the lock would park forever and
    wedge every later connect, disconnect and self-heal. So this hands the work
    to a task that acquires the lock itself, the same shape the self-heal uses.
    """
    task = asyncio.create_task(self._retire_chained(instance_id, reason))
    self._retirements.add(task)
    task.add_done_callback(self._retirements.discard)
    task.add_done_callback(
        lambda t: (
            logger.error("Chained retirement crashed: %s", t.exception())
            if not t.cancelled() and t.exception()
            else None
        )
    )


async def _retire_chained(self: SshTunnelManager, instance_id: str, reason: str) -> None:
    """Drop the credential and the forward, keeping the user's intent.

    ``keep_intent=True`` because the user still wants this crew: what is wrong is
    the hop, not the wish. Leaving ``was_connected`` set is what lets the ordinary
    reconnect bring it back on a hop that is actually ours -- where clearing it
    would present a security teardown as the user having turned the crew off.
    """
    logger.warning("Retiring the chained forward for %s: %s", instance_id, reason)
    async with self._lock:
        if instance_id not in self._tunnels:
            return
        await self._teardown_locked(instance_id, keep_intent=True)
    self._last_error[instance_id] = reason


def _schedule_token_refresh(self: SshTunnelManager, instance_id: str) -> None:
    """(Re)start the proactive refresh loop for *instance_id*.

    Refuses while a reconfiguration holds the barrier: a refresh mints against
    the record it read, so one started here would carry the pre-edit
    coordinates and could store that token against the rebuilt tunnel.
    """
    if instance_id in self._reconfiguring:
        logger.info(
            "Skipping proactive refresh for %s: reconfiguration in progress",
            instance_id,
        )
        return
    existing = self._refresh_tasks.get(instance_id)
    if existing is not None and not existing.done():
        existing.cancel()
    task = asyncio.create_task(self._token_refresh_loop(instance_id))
    self._refresh_tasks[instance_id] = task
    task.add_done_callback(
        # False positive (below): only the instance id + exception are logged,
        # never the token. The message contains the word "Token" (the task's
        # name), which trips the heuristic; this module never logs token values
        # (a documented invariant — mint/refresh keep tokens off stderr/logs).
        lambda t: (
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logger.error("Token refresh task crashed for %s: %s", instance_id, t.exception())
            if not t.cancelled() and t.exception()
            else None
        )
    )


def _refresh_delay(self: SshTunnelManager, instance_id: str) -> float | None:
    """How long to wait before the next proactive re-mint, or None if unknown.

    Derived from the lifetime the CURRENT token was issued under, read at the
    moment it is needed rather than once for the life of the loop. That stored
    lifetime is rewritten by every store (:meth:`_store_token`), and for a
    chained crew it is the SHORTER of the parent's answer and our own record
    (:meth:`_minted_ttl`) -- so lowering the TTL on either side changes it while
    the loop is running. Neither edit is a transport field, so neither tears the
    tunnel down and restarts this loop; a delay computed once would keep
    sleeping the old, longer interval, and the shorter token would die before
    the next re-mint. The pane then reloads and whatever was unsaved in it is
    gone, on every cycle rather than once.
    """
    ttl_secs = self._token_ttl_secs.get(instance_id)
    if not ttl_secs:
        return None
    return max(1.0, ttl_secs * _REFRESH_FRACTION)


async def _token_refresh_loop(self: SshTunnelManager, instance_id: str) -> None:
    """Sleep to ~80% of TTL, re-mint, repeat — until cancelled.

    Keeps the in-memory token valid ahead of the 20h cap so reconnects /
    fresh iframe loads always have a usable token. Re-mint failures are
    logged and retried on the next cycle (self-heal also covers tunnel-side
    breakage). Cancelled by disconnect/shutdown.
    """
    delay = self._refresh_delay(instance_id)
    if delay is None:
        return
    try:
        while True:
            await asyncio.sleep(delay)
            # A failed re-mint is only terminal once the instance is not
            # connected (dropped from _tunnels).
            if not await self._refresh_token_once(instance_id) and instance_id not in self._tunnels:
                return
            # Re-derive rather than keep the value this loop started with: every
            # store rewrites the stored lifetime, and for a chained crew it is
            # the shorter of the parent's answer and our record, so a TTL lowered
            # on either side has to move the schedule. A FAILED refresh stored
            # nothing, so this reads back the same number and the deliberate
            # retry-at-the-same-interval behaviour is unchanged.
            delay = self._refresh_delay(instance_id) or delay
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # never let the refresh loop crash silently
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- the format names a token, its arguments never carry one
        logger.exception("Token refresh loop crashed for %s: %s", instance_id, exc)


async def _refresh_token_once(self: SshTunnelManager, instance_id: str) -> bool:
    """Re-mint the token once. Returns True on success.

    The remote mint runs WITHOUT holding the manager lock (so a slow mint
    can't block connect/disconnect); the result is stored under the lock only
    if the instance is still connected (guards a disconnect mid-mint). Uses
    whichever transport the instance is configured for.
    """
    # A reconfiguration is about to move the coordinates this mint would read,
    # so starting one now can only produce a token for a machine the user is
    # leaving. The caller reports "no token" and the client retries.
    if instance_id in self._reconfiguring:
        return False
    inst = await asyncio.to_thread(self._registry.get, instance_id)
    if inst is None or instance_id not in self._tunnels:
        return False
    # Which tunnel generation this token is being minted FOR. Captured before
    # the await, compared after.
    epoch = self._tunnel_epoch.get(instance_id, 0)
    try:
        params = self._resolve_transport(inst, await self._with_parent(inst))
    except (SshValidationError, SsmValidationError) as e:
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- the format names a token, its arguments never carry one
        logger.warning("Token refresh aborted for %s: %s", instance_id, e)
        return False
    try:
        mint = await self._mint_for(inst, params)
    except HopRetiredError as e:
        # The parent answered that the hop is gone, so retrying cannot recover
        # it -- and the forward we still hold points at a port the parent may
        # now hand to a different crew, which would carry this crew's token
        # there. Terminal for the forward, unlike every other mint failure.
        self._schedule_chained_retirement(instance_id, str(e))
        return False
    except TokenMintError as e:
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- the format names a token, its arguments never carry one
        logger.warning("Proactive token refresh failed for %s: %s", instance_id, e)
        return False
    async with self._lock:
        if instance_id not in self._tunnels:
            return False  # disconnected while minting — discard
        if not self._store_token(inst, mint, minted_at_epoch=epoch, binds_forward=False):
            return False
    await self._prime_peer_session(instance_id)
    # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- the format names a token, its arguments never carry one
    logger.info("Proactively refreshed token for %s", instance_id)  # no token in logs
    return True


async def refresh_token(self: SshTunnelManager, instance_id: str) -> str | None:
    """Force a fresh token mint for a connected instance and return it.

    Drives the owner's client-side refresh loop: re-mints over SSH, stores
    the new token, and returns it so the browser can reload the embedded
    iframe with a valid token — either proactively (before the TTL cap) or
    reactively (the embedded dashboard reported an expired session). Returns
    ``None`` if the instance isn't connected or the mint failed. The token
    is never logged.
    """
    if not await self._refresh_token_once(instance_id):
        return None
    # The caller hands this token to the browser, whose pane loads it over
    # the same forward.
    if not await self._chained_far_end_holds(instance_id):
        return None
    return self.get_token(instance_id) or None
