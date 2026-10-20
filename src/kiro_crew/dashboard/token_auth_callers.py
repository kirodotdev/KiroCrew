"""Who is calling: the peer on the dashboard's Unix socket, and the app or slot a request names.

Kernel peer-credential verification for a request on the Unix socket, the live and bound
session keys of the caller's process chain, and attributing a request to the app, cron
job, sub-agent or member it acts for, including refusing a caller whose slot record is
missing.

Served through :mod:`kiro_crew.dashboard.token_auth`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from functools import partial
from typing import Any, Sequence, cast

from aiohttp import web

from kiro_crew.dashboard.origin import is_proxied_request, request_is_unix_socket
from kiro_crew.dashboard.token_auth_paths import _deny, _log_auth
from kiro_crew.executors import subprocess_executor
from kiro_crew.mcp_gateway.socketsec import PeerCredResult, check_peer_is_self, get_peer_pid
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.peer_resolve import resolve_peer_tenancy
from kiro_crew.runtime_ownership import session_keys_bound_to_pid
from kiro_crew.sel import sel as _sel_fn
from kiro_crew.session_token_sig import verify_session_token

logger = logging.getLogger("kiro_crew.dashboard.token_auth")


def _unix_request_socket(request: web.Request) -> Any:
    """Return the request's underlying socket iff it is ``AF_UNIX``.

    Thin socket-returning wrapper over the shared
    :func:`~kiro_crew.dashboard.origin.request_is_unix_socket` discriminator
    (one definition of "arrived on the dashboard's unix socket" for the CSRF
    and token-auth layers). The peer-verification branch needs the SOCKET (to
    read kernel peer credentials), not just the boolean. Returns ``None`` for
    TCP requests, mocked/absent transports, platforms without ``AF_UNIX``,
    and any error — never raises.
    """
    if not request_is_unix_socket(request):
        return None
    transport = getattr(request, "transport", None)
    if transport is None:  # pragma: no cover — excluded by the check above
        return None
    try:
        return transport.get_extra_info("socket")
    except Exception:
        return None


def _live_session_keys_on_chain(request: web.Request, chain: Sequence[int]) -> frozenset[str]:
    """WHICH sessions the LIVE session manager reports on *chain*'s nearest hit.

    The keys, not a count. The manager already holds them, and a count throws
    away the one fact that decides whether a declared key belongs here at all:
    an arm that learns "one session lives on this pid" and not "which" has to
    accept whatever key it is handed.

    The published mapping is the same answer written to disk, and disk is not a
    trustworthy place to ask this question: the pair sits in the data-home root,
    which is writable inside the agent sandbox, so a co-tenant can simply UNLINK
    it. Absence then reads as "nothing resolved", which is the permissive arm --
    a plain unlink would otherwise buy the attacker the attestation-free path
    that publishing the roster exists to close. Per-turn republication bounds
    that window but does not close it, because the deleter picks its moment.

    So for the case where nothing resolved on disk, the manager is asked
    directly. It answers from memory, in this same process, about runtimes it
    owns, and an agent cannot edit it. The chain is walked peer-first for the
    same reason the disk walk is: the peer is an MCP server whose kiro-cli
    ANCESTOR is the pid sessions are keyed by.

    Returns an EMPTY set when the manager knows none of these pids, which is the
    ordinary answer for the callers the permissive arm exists for -- a cron
    script, a warm-pool runtime before claim, a pooled backend outside this
    manager. Empty is therefore "no evidence", never "not shared", and the caller
    treats it as such. Every failure path answers empty for the same reason: a
    probe that could not ask must not become a verdict.

    The set is NOT a closed account of the pid's tenancy. A ``spawn_run`` shared
    subagent's session lives on its parent's ``_shared_provider`` and is never
    registered with the manager, so its key can be missing from a set that names
    its parent. That is why a declared key absent from a NON-EMPTY set takes the
    attestation arm rather than a flat denial: absent-from-roster and
    not-a-tenant are different facts, and only a token tells them apart.
    """
    try:
        sessions = request.app["state"].sessions
        rows = sessions.runtime_pids()
    except Exception:
        # No manager reachable from this request (a unit test's bare app, a
        # non-dashboard host). No evidence, not a verdict.
        return frozenset()
    try:
        on_pid: dict[int, set[str]] = {}
        for row in rows:
            # Sessions only. The snapshot also appends one row per companion
            # RUNTIME whose key is display text, and a subagent runtime often
            # repeats the pid of a session it serves -- counting those would
            # report two sessions for a pid hosting one. Same filter, and same
            # reason, as the publisher's.
            if "sid" not in row:
                continue
            pid, key = row.get("pid"), row.get("key")
            if isinstance(pid, int) and isinstance(key, str) and key:
                on_pid.setdefault(pid, set()).add(key)
        for pid in chain:
            if pid in on_pid:
                return frozenset(on_pid[pid])
    except Exception:
        return frozenset()
    return frozenset()


def _bound_session_keys_on_chain(chain: Sequence[int]) -> frozenset[str]:
    """WHICH sessions the OWNERSHIP TABLES place on *chain*'s nearest hit.

    A token proves which session a caller IS. It does not prove that session is
    on the process the call arrived from, and those are different facts: the token
    is a bearer credential sitting in the runtime's environment, so a same-uid
    co-tenant that can read ``/proc/<pid>/environ`` can present a sibling's token
    and satisfy the name check with it. What it cannot do is put that sibling onto
    a process the gateway never placed it on. So the declared key is additionally
    required to hold a LIVE entry in one of the two tables for this pid -- the
    lease table for a session that owns its runtime, the tenancy table for a
    sharing sub-agent or task run that does not.

    Both halves are needed and :func:`session_keys_bound_to_pid` unions them: a
    sub-agent holds no lease, and a chat session holds no tenancy.

    The chain is walked peer-first, exactly as the disk walk and the live-manager
    reader walk it, and the NEAREST pid either table knows anything about decides.
    The peer is an MCP stub whose kiro-cli ANCESTOR is the process sessions are
    placed on, so the answer is never on the peer pid itself.

    Returns an EMPTY set when no pid on the chain appears in either table, which
    is not a verdict and must not be read as one. The tables describe runtimes
    THIS gateway placed sessions on; a warm-pool runtime before its claim, a
    pooled MCP backend, a cron script and anything belonging to another install
    are all absent from them while being entirely legitimate. Every failure path
    answers empty for the same reason a probe that could not ask must not become a
    verdict.
    """
    try:
        for pid in chain:
            if not isinstance(pid, int):
                continue
            bound = session_keys_bound_to_pid(pid)
            if bound:
                return bound
    except Exception:
        # The tables failing to answer is an "unresolvable" outcome, never a
        # denial -- the same rule the resolver failure above follows.
        logger.debug("ownership tables unreadable while verifying a peer", exc_info=True)
        return frozenset()
    return frozenset()


async def _verify_unix_peer(
    request: web.Request, sock: Any, path: str
) -> web.StreamResponse | None:
    """Kernel-verify the declared ``X-Session-Key`` of an AF_UNIX peer.

    Verify-when-resolvable, deny-on-mismatch, degrade-to-status-quo when
    unresolvable — strictly monotonic hardening over the TCP-era behavior
    (where the header was accepted entirely on the caller's word):

    * peer uid positively ≠ ours (``MISMATCH``) → deny. Cannot normally
      happen (the socket sits in the 0700 data home), so a hit means the
      directory gate failed — exactly when denying matters most.
    * the declared key is one of the sessions the peer's process is attested to
      host → proceed with ``request["peer_verified"] = True``. On a 1:1 runtime
      that is the pid's single mapped key, and the kernel's process attestation
      settles it: one session lives there, so there is no other identity the
      caller could be mistaken for.
    * on a SHARED runtime the pid names several sessions, and membership is
      necessary but NOT sufficient. The mapping's MAC stops an agent ADDING
      itself to a pid's membership, but every co-tenant can READ the list, so
      one of them could declare a sibling's recorded key — and the key admitted
      here is what ``derive_caller_app`` derives app confinement from. So a
      shared pid additionally requires an ``X-Session-Token`` that verifies to
      the declared key: it is MAC'd under the agent-unreadable SEL trust root,
      names ONE session, and every caller that declares a key already sends it.
      Admitted here the call is SEL-recorded rather than debug-logged, because
      this is the arm where a cross-session declaration would have landed.
      The token is required on EVERY shared-pid arm, including the one whose
      roster the mapping file's size bound truncated: truncation is a normal
      publisher outcome, so a roster short of the declared key withholds
      membership evidence without withdrawing the demand for attestation.
    * the peer's full membership is known and the declared key is NOT in it →
      deny 403 + SEL ``dashboard.peer-identity-mismatch`` (the impersonation
      this check exists to close: a same-uid process declaring another
      session's identity).
    * anything that leaves the pid's tenancy unknown ALTOGETHER → proceed under
      today's semantics: no new denial. No peer pid mechanism, no
      ``session_pid_<pid>`` file in the ancestry (warm-pool runtimes before
      claim, cron scripts, pooled MCP backends), or a pid proven recycled.
      A truncated roster is NOT one of these: it carries positive evidence of
      sharing, so it takes the token arm above.

    Returns a deny response, or ``None`` to proceed. The /proc ancestry walk
    is blocking I/O and runs on the subprocess executor (mirroring gatewayd's
    register-path offload).
    """
    declared = request.headers.get("X-Session-Key", "")
    if not declared:
        # Nothing session-scoped is being claimed — nothing to verify.
        return None
    verdict = check_peer_is_self(sock)
    if verdict is not PeerCredResult.MATCH:
        # Deny-by-default, mirroring gatewayd's register-path policy: a peer
        # whose principal cannot be POSITIVELY confirmed as ours is refused.
        # On the supported POSIX platforms (Linux SO_PEERCRED, macOS
        # LOCAL_PEERCRED) an accepted AF_UNIX connection always yields peer
        # credentials, so UNVERIFIABLE here means the mechanism itself failed
        # — exactly when trusting the claim is least justified. TCP callers
        # are unaffected (this branch is AF_UNIX-only) and the client falls
        # back to TCP transparently if the socket ever refuses connects.
        _reason = (
            "unix peer uid differs from server uid"
            if verdict is PeerCredResult.MISMATCH
            else "unix peer credentials unverifiable"
        )
        _sel_fn().log_api_access(
            caller="unknown",
            forwarded=is_proxied_request(request),
            operation="dashboard.peer-identity-mismatch",
            outcome="denied",
            source="token_auth",
            resources=path,
            error=_reason,
        )
        _log_auth(request, "internal", "denied", _reason)
        return _deny(request, "Forbidden", "unix_peer_unverified")
    peer_pid = get_peer_pid(sock)
    if peer_pid is None:
        return None
    try:
        # signed_only: authorization decisions must not trust the bare
        # same-uid-writable .txt mapping — require the HMAC sidecar (pid
        # bound into the MAC, keyed by the agent-unreadable SEL trust root),
        # or the walk yields nothing and this check degrades to status quo.
        # That covers the tenant list too: without the MAC an agent could add
        # its declared key to a pid's membership and be admitted by it.
        tenancy = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            partial(resolve_peer_tenancy, peer_pid, signed_only=True),
        )
    except Exception:
        # Resolution machinery failing is an "unresolvable" outcome, not a
        # denial — the change must never be weaker OR stricter than intended.
        logger.debug("unix peer identity resolution failed", exc_info=True)
        return None

    async def _attest_shared(shape: str, *, hosts: int | None) -> web.StreamResponse | None:
        """Admit *declared* on a shared pid only against its per-session token.

        The one place a shared-runtime declaration is decided, because every
        arm that reaches a shared pid owes the same attestation: membership is
        NECESSARY but not SUFFICIENT when the roster enumerates the key, and it
        is not even available when the size bound truncated the roster. Both
        arms hold the same two facts — the pid hosts several sessions, and the
        kernel cannot say which one holds this socket — so both need the token,
        which is MAC'd under the agent-unreadable SEL trust root and names ONE
        session.

        *shape* describes the roster the decision was made against and is
        recorded on both outcomes, so an investigation can tell a declaration
        checked against a full membership from one checked against a short one.

        Two things are asked, because a token answers only one of them. It proves
        WHICH session the caller is; it does not prove that session is on the
        process this call arrived from, and it cannot, because it is a bearer
        credential living in the runtime's environment where a same-uid co-tenant
        reading ``/proc/<pid>/environ`` can lift it. So the declared key must ALSO
        hold a live entry in one of the two ownership tables for this pid --
        :func:`_bound_session_keys_on_chain` -- and a declaration the tables place
        elsewhere is refused ``peer_session_unbound``. Stolen-token reach shrinks
        from "any session whose env is readable" to "a session this process
        actually hosts", which is the set the roster already admits. Tables that
        cannot speak about the pid answer empty and change nothing.

        *hosts* is the tenancy the denial was actually decided against, or
        ``None`` on the one arm that measured no count at all. It is
        keyword-only with NO default on purpose: a default resolves to
        ``tenancy.tenant_count``, which is ``0`` on every arm reached without a
        resolved mapping, so a denial taken BECAUSE the live manager reported
        several sessions would record ``hosts 0 sessions`` — a trail
        contradicting the decision it explains and indistinguishable from a pid
        that genuinely hosts none. An unmeasured tenancy therefore carries its
        own wording rather than a zero, and a new arm cannot inherit a silent
        count.

        Returns a deny response, or ``None`` having marked the request verified.
        """
        attested = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            partial(verify_session_token, request.headers.get("X-Session-Token", "")),
        )
        if attested != declared:
            # Three states, not two: a measured count, and a tenancy nothing
            # measured — which must NOT arrive as the zero that reads as "hosts
            # none".
            tenancy_note = (
                f"hosts {hosts} sessions" if hosts is not None else "tenancy not measured"
            )
            _sel_fn().log_api_access(
                caller=declared,
                forwarded=is_proxied_request(request),
                operation="dashboard.peer-identity-unattested",
                outcome="denied",
                source="token_auth",
                resources=path,
                error=(
                    f"peer_pid={peer_pid} {tenancy_note} ({shape}); "
                    "the declared X-Session-Key needs a per-session token naming it"
                ),
            )
            _log_auth(
                request,
                "internal",
                "denied",
                f"co-tenant declaration unattested (peer_pid={peer_pid})",
            )
            return _deny(request, "Forbidden", "peer_session_unattested")
        bound = _bound_session_keys_on_chain(tenancy.chain)
        if bound and declared not in bound:
            # The token named this session, and the gateway's own tables say this
            # session is not on this process. A token is a BEARER credential in
            # the runtime's environment, so the actor that can read a sibling's
            # env can present its token and pass the name check above -- which is
            # exactly the gap this arm closes. It cannot also make the gateway
            # place that sibling on a process it was never placed on, and the
            # tables are in this process's memory rather than in a file the
            # sandbox can write.
            #
            # A non-empty set is the whole precondition. Empty means the tables
            # cannot speak about the pid, which is no evidence and takes the arm
            # below unchanged; only a set that names OTHER sessions and not this
            # one is positive evidence that the declaration does not belong here.
            _sel_fn().log_api_access(
                caller=declared,
                forwarded=is_proxied_request(request),
                operation="dashboard.peer-session-unbound",
                outcome="denied",
                source="token_auth",
                resources=path,
                error=(
                    f"peer_pid={peer_pid} ({shape}); the token names this session but "
                    f"neither ownership table places it on this process "
                    f"({len(bound)} bound there)"
                ),
            )
            _log_auth(
                request,
                "internal",
                "denied",
                f"declared session is not bound to this process (peer_pid={peer_pid})",
            )
            return _deny(request, "Forbidden", "peer_session_unbound")
        # Recorded, not debug-logged: this is the arm where a cross-session
        # declaration would have succeeded, so it is the one an investigation
        # needs a trail for. The 1:1 arm stays at debug because a pid hosting
        # one session has no other identity to be mistaken for.
        _sel_fn().log_api_access(
            caller=declared,
            forwarded=is_proxied_request(request),
            operation="dashboard.peer-identity-co-tenant",
            outcome="allowed",
            source="token_auth",
            resources=path,
            # The roster judged AND whether the tables confirmed the binding, so
            # the trail separates an admission that passed both checks from one
            # the tables could not speak about -- which is the difference between
            # the hardened path and the one that fell open.
            error=f"{shape}; binding={'confirmed' if bound else 'no evidence'}",
        )
        request["peer_verified"] = True
        return None

    if not tenancy.admits(declared):
        if not tenancy.membership_complete:
            if tenancy.shared:
                # Two different unknowns reach this arm, and ``shared`` is the
                # positive evidence that separates them. Here the pid IS known
                # to host several sessions and only the roster is short, so the
                # declared key's absence is not grounds to deny — but it is not
                # licence to proceed on the caller's word either, which is the
                # weakest point of the whole check: truncation is a normal
                # publisher outcome under the size bound, so a caller that can
                # overflow the roster would otherwise skip attestation
                # altogether. The token does not depend on the roster, so it
                # still decides.
                return await _attest_shared(
                    "roster truncated by the size bound",
                    hosts=tenancy.tenant_count,
                )
            if tenancy.unverifiable:
                # A mapping EXISTS on this ancestry and would not VERIFY, which
                # is not the same as none existing. The pair is replaced in two
                # steps -- `.txt` then `.sig` -- so a body-changing
                # republication is briefly visible as a MAC mismatch, and a
                # tenancy-changing claim is exactly when the body changes. Left
                # on the arm below, that window is a bypass rather than a
                # degradation: a co-tenant can read both files, watch `.txt`
                # move ahead of `.sig`, and drive its declaration into the gap
                # to skip an attestation the resolved mapping would have
                # demanded. There is no roster to check a declaration against
                # here, so the token is the whole decision -- and the caller
                # that legitimately owns the declared key holds one, while a
                # sibling reading that key out of the roster does not.
                #
                # This is the MAC mismatch ALONE. A proven recycle verifies, so
                # it is attested knowledge that the pid moved on rather than
                # ambiguity about who is calling, and one sits beside every live
                # recycled pid until the next sweep -- demanding a token there would deny
                # the tokenless callers the arm below exists for. The CLIENT-side
                # walk in `mcp_caller` keeps the wider set on purpose: it reads
                # without requiring a signature, so a recycled-looking file
                # there may simply have been planted, and what it withholds is
                # the caller's own name rather than someone else's access.
                # No count reaches this arm: the mapping that would carry one is
                # the very thing that would not verify, so the record says the
                # tenancy was not measured rather than reporting none.
                return await _attest_shared("mapping present but unverifiable", hosts=None)
            live_keys = _live_session_keys_on_chain(request, tenancy.chain)
            live_sessions = len(live_keys)
            if live_sessions == 1 and declared in live_keys:
                # The manager names exactly one session on this ancestry and it
                # IS the declared key, so the manager itself attests the 1:1
                # identity the missing mapping would have carried. Nothing is
                # taken on the caller's word: the key came from the manager's
                # own memory, which an agent cannot edit. Admitted without a
                # token for the same reason a 1:1 roster is -- one session lives
                # there, so there is no other identity to be mistaken for.
                logger.debug(
                    "unix peer identity verified from the live manager for %s (peer_pid=%d)",
                    path,
                    peer_pid,
                )
                request["peer_verified"] = True
                return None
            if live_sessions == 1:
                # One session, and it is NOT the declared key. A COUNT cannot see
                # this: one reads as "1:1, no other identity to be mistaken for",
                # so the declared key is taken on the caller's word -- which is
                # exactly what a plain unlink buys, because an agent that deletes
                # its own mapping has already read the roster and knows a sibling
                # key to name.
                #
                # Attestation rather than denial, because the set is not a closed
                # account of the tenancy: a `spawn_run` shared subagent's session
                # is never registered with the manager, so a legitimate caller's
                # key can be absent from a set that names its parent. Denying on
                # absence would 403 exactly that caller in the window this arm
                # exists for. A token names ONE session under the agent-unreadable
                # trust root, so the legitimate holder passes and the unlinker --
                # which has the sibling key but not its token -- does not.
                return await _attest_shared(
                    "mapping absent, manager hosts a different session", hosts=live_sessions
                )
            if live_sessions > 1:
                # Nothing resolved ON DISK, but the live session manager says
                # this ancestry hosts several sessions. Disk absence is not
                # evidence of anything here: the pair sits in the data-home
                # root, which is writable inside the agent sandbox, so reaching
                # this arm costs a co-tenant one unlink -- and the roster it read
                # before deleting tells it which sibling key to declare.
                # Per-turn republication bounds that window without closing it,
                # because the deleter picks its moment. The manager answers from
                # memory in this process about runtimes it owns, so it is the one
                # account of the pid's tenancy an agent cannot edit, and where it
                # says several the token decides exactly as it would have from a
                # readable mapping.
                return await _attest_shared(
                    "mapping absent, manager reports several", hosts=live_sessions
                )
            # Nothing resolved at all: no mapping in the ancestry, and the manager
            # knows of no sharing either. Absence is not evidence, and with no
            # positive evidence of sharing to attest against this stays the
            # unresolvable arm and today's semantics hold -- a warm-pool runtime
            # before claim, a cron script, a pooled MCP backend outside this
            # manager.
            return None
        _sel_fn().log_api_access(
            caller=declared,
            forwarded=is_proxied_request(request),
            operation="dashboard.peer-identity-mismatch",
            outcome="denied",
            source="token_auth",
            resources=path,
            error=f"peer_pid={peer_pid} hosts no session matching the declared X-Session-Key",
        )
        _log_auth(
            request,
            "internal",
            "denied",
            f"peer identity mismatch (peer_pid={peer_pid})",
        )
        return _deny(request, "Forbidden", "peer_session_mismatch")
    if tenancy.shared:
        # Membership is NECESSARY but not SUFFICIENT here. The tenant list is
        # MAC-covered, so it cannot be forged — but it is published in a file
        # the agent can READ, and every session on the pid shares that file, so
        # one co-tenant can read a sibling's key and declare it. The kernel
        # attests the PROCESS; on a shared runtime that does not pick out which
        # of its sessions is speaking on this socket, and the key admitted here
        # is the same one ``derive_caller_app`` derives app confinement from.
        #
        # On a 1:1 pid the mapping's single key already identifies the only
        # possible speaker, so that path stays exactly as it was and needs no
        # token.
        return await _attest_shared("roster complete", hosts=tenancy.tenant_count)
    # Positive kernel attestation. Debug-level on purpose — this fires on
    # every internal call from a claimed session; the SEL trail records the
    # deny arm, which is the permission decision that changes anything.
    logger.debug("unix peer identity verified for %s (peer_pid=%d)", path, peer_pid)
    request["peer_verified"] = True
    return None


def derive_caller_app(
    slots: object, session_key: str, jobs: object = None, subagents: object = None
) -> str:
    """The app that owns the CALLING session, or ``""`` for the dashboard user.

    **Why this exists.** App-ownership checks gate on ``request["app"]``, which
    the app-token branch publishes. The internal-secret branch (the managed MCP
    set) carries no app claim at all: the secret proves the call came from
    inside, not who made it. Without this, every ownership check is a no-op on
    that transport, and an app agent granted ``@kirocrew-dashboard`` arrives
    indistinguishable from the dashboard user.

    The identity comes from the authenticated CALLING SESSION, resolved against
    server-side registries in four steps -- one per way a session can be owned:
    the slot the key names, the slot RUNNING under that key
    (``linked_session_key``), for a cron key the job's ``created_by`` owner tag,
    and for a subagent key the child record's spawning ``app``. The agent cannot
    forge the key — it does not build the request; the in-process MCP broker sets
    it from the calling session's own context and never from tool arguments
    (``mcp_core`` ``_resolve_session_key_strict``), and on an AF_UNIX transport
    ``_verify_unix_peer`` has already kernel-attested it against ``/proc``
    ancestry before this runs.

    ``""`` means "no app owns this caller" and is returned both when the person
    is the caller and when the caller cannot be placed at all. The two are not
    distinguished because no site acts differently on them; the one route that
    needs the difference for a ``dashboard:`` key asks
    :func:`caller_names_a_missing_slot` instead.

    Deliberately PURE — every parameter is a server-side registry or the
    caller's own key, never a request — so the middleware and the one route that
    also re-derives for defense-in-depth share a single implementation that
    cannot drift. Never derive from a request BODY or from tool arguments: a
    caller that could name its own scope could name someone else's.
    """
    sk = (session_key or "").strip()
    # No key at all: the gateway's own internal calls, the CLI, a loopback
    # curl. Nothing to place and no app could have been attached — unscoped,
    # exactly as before this derivation existed.
    if not sk or sk == "dashboard:ui":
        return ""
    # Each registry below answers only "yes, THIS app owns the caller". An
    # ownerless answer FALLS THROUGH to the next one rather than ending the
    # search, because a session can be recorded in more than one place and only
    # some of those records carry the owner. The case that forces this: an
    # app-created cron with a cron-born tab is present in the slot registry with
    # no ``_app`` (a channel/cron-born slot is created without one) AND in the
    # cron registry WITH its ``created_by`` owner -- so short-circuiting on the
    # slot would hand an app the dashboard user's reach. Returning "" only after
    # every registry has been asked also makes the search monotonic: adding a
    # registry can find an owner that was missed, never lose one.
    lookup = getattr(slots, "get", None) if slots is not None else None
    if lookup is not None:
        slot = lookup(sk.split(":", 1)[-1] if ":" in sk else sk)
        owner = str(getattr(slot, "_app", "") or "") if slot is not None else ""
        if owner:
            return owner
        # Second lookup, by LINKED key. A slot bound to a channel or cron
        # session runs its turns under ``linked_session_key`` rather than under
        # its own name ("when set, _run_chat uses this as session key" --
        # ``DashboardState``), so the caller presents that key and the lookup
        # above cannot find it. Without this the slot's ``_app`` is invisible
        # and the caller reads as the person: the very shape this fix exists to
        # end, since the slot IS there and may carry an owner.
        linked = _slot_by_linked_key(slots, sk)
        owner = str(getattr(linked, "_app", "") or "") if linked is not None else ""
        if owner:
            return owner
    # Third lookup, for a cron key, in the CRON registry. A cron created by an
    # app is tagged ``created_by="app:<name>"`` (``CronSDK``), so the owner is a
    # matter of record rather than a guess -- and a cron often has no dashboard
    # slot at all, which is why the lookups above cannot see it. Resolving it
    # POSITIVELY is what lets an app-owned cron be confined while the person's
    # own crons keep working; refusing every unplaceable delegated caller
    # instead would take cron and subagent tools from the person too.
    if sk.startswith("cron:"):
        owner = _cron_job_owner(jobs, _key_segment(sk))
        if owner:
            return owner
    # Fourth lookup, for a subagent key, in the SUBAGENT registry. A child
    # spawned by an app carries it in ``SubagentInfo.app``, persisted for exactly
    # this purpose -- the field's own note says it is kept so "the child's
    # per-tool-call gate can resolve the app's Level-2 profile", because
    # otherwise "the child's ongoing tool calls run unconstrained by the app
    # scope". A tool call arriving here IS one of those ongoing calls.
    if sk.startswith("subagent:"):
        owner = _subagent_owner(subagents, _key_segment(sk))
        if owner:
            return owner
    # Every registry asked, none names an owner: the person, or a Slack thread
    # or channel session that never had an app.
    return ""


def _key_segment(session_key: str) -> str:
    """The id segment of a delegated caller key, ignoring anything after it.

    A cron session key has TWO forms -- ``cron:<job_id>`` for a persistent job
    and ``cron:<job_id>:<run_id>`` for a stateless one (``cron._build_prompt``).
    Taking the whole remainder after the first colon yields ``<job_id>:<run_id>``
    for the stateless form, which matches no job, so an app-owned stateless cron
    would resolve to no owner and be handed the dashboard user's reach.
    """
    parts = session_key.split(":")
    return parts[1] if len(parts) > 1 else ""


def _subagent_owner(subagents: object, agent_id: str) -> str:
    """The app that spawned a subagent, or ``""`` (person-spawned, or no record).

    Uses the same canonical conversation lookup as the missing-record gate.
    """
    info = _subagent_caller_record(subagents, agent_id)
    return str(getattr(info, "app", "") or "") if info is not None else ""


def _subagent_caller_record(subagents: object, agent_id: str) -> object | None:
    """Resolve a run or its unique active continuation from the live registry.

    A continuation registers under a new run id but calls tools under its
    original conversation key. After eviction or restart that original run
    can be absent. Only an executing continuation with the exact canonical key
    can establish the caller; retained files and queued work confer no authority.
    The original record, when present, keeps its ownership precedence.
    """
    if subagents is None or not agent_id:
        return None
    lookup = getattr(subagents, "get", None)
    if lookup is None:
        return None
    try:
        info = lookup(agent_id)
        if info is not None:
            return info
        conversation_key = f"subagent:{agent_id}"
        matches = [
            candidate
            for candidate in getattr(subagents, "values")()
            if getattr(candidate, "conversation_key", "") == conversation_key
            and getattr(candidate, "done", None) is False
            and getattr(candidate, "queued", None) is False
        ]
        return matches[0] if len(matches) == 1 else None
    except Exception:  # noqa: BLE001 - an auth path must never 500 on this
        return None


def caller_record_is_missing(
    session_key: str, jobs: object = None, subagents: object = None
) -> bool:
    """True when a DELEGATED key's own registry is present but holds no record.

    A ``cron:`` or ``subagent:`` key is not like a Slack thread: the work it names
    is recorded, and that record is where its owner lives. So absence here is not
    "nothing to confine" -- it is "the record that would have told me who this
    runs for is gone", which is the same thing
    :func:`caller_names_a_missing_slot` says about a ``dashboard:`` key.

    A live subagent is registered by run id; an active continuation can establish
    its canonical conversation key when the original run was evicted or predates
    this gateway process. A cron job stays until it is removed, and its deleted
    record's app reach must not survive removal.

    Requires the registry to be PRESENT. A surface wired without one (the
    ``--slack-only`` API server) must not have every delegated caller refused
    because of a missing dependency, so an absent registry answers False.
    """
    sk = (session_key or "").strip()
    if sk.startswith("cron:"):
        registry: object = jobs
    elif sk.startswith("subagent:"):
        registry = subagents
    else:
        return False
    if registry is None:
        return False
    ident = _key_segment(sk)
    if not ident:
        # A key with no id names nothing; it cannot be confirmed against a
        # record either, so treat it as unresolvable rather than as present.
        return True
    if sk.startswith("cron:"):
        return not _cron_job_exists(registry, ident)
    return not _subagent_record_exists(registry, ident)


def _cron_job_exists(jobs: object, job_id: str) -> bool:
    """Whether a cron job id is in the registry (cache-only read, see ``_cron_job_owner``).

    Fails CLOSED: an unreadable/erroring registry returns ``False`` ("does not
    exist") so the ``_internal_caller_record_missing`` DENY branch fires and the
    delegated caller is refused rather than admitted as the unscoped dashboard
    user. Per SAX-04 outcome_3, an auth decision must fail closed when the source
    it depends on is unavailable; the in-memory registry makes this rare, but the
    branch must not silently escalate a delegated caller on a torn read.
    """
    try:
        snapshot = list(cast("Iterable[Any]", jobs))
    except Exception:  # noqa: BLE001 - an auth path must never 500 on this
        return False
    return any(str(getattr(j, "id", "") or "") == job_id for j in snapshot)


def _subagent_record_exists(subagents: object, agent_id: str) -> bool:
    """Whether the live registry establishes this canonical subagent caller.

    The owner and existence decisions share a resolver, including its refusal
    of ambiguous continuations and unreadable registries.
    """
    return _subagent_caller_record(subagents, agent_id) is not None


def caller_names_a_missing_slot(slots: object, session_key: str) -> bool:
    """True when the key NAMES a dashboard slot that is not in the registry.

    Distinct from "no app owns this caller" (:func:`derive_caller_app` returning
    ``""``), which covers callers that never had a slot at all -- a Slack thread,
    a channel session, the CLI. A ``dashboard:`` key is different in kind: it
    names a specific slot, so absence is not "nothing to confine" but "the slot
    I would have been confined against is gone". That happens when a tab is
    closed while one of its calls is still in flight (the slot is popped
    synchronously, without draining in-flight MCP calls) or when the key is
    simply wrong.

    An app-owned session going through that race would otherwise reach a route
    that refuses apps and be admitted, because the app it should have been
    confined to is exactly what got popped. ``mcp_dashboard._caller_app_scope``
    already refuses this class for its own tool set on that reasoning; this
    predicate lets a route outside that set apply the same rule.

    Deliberately NOT applied in the middleware: a popped slot does not say
    whose tab it was, so refusing there would also refuse the person's own
    in-flight calls, on every internal route at once. Each route that publishes
    something it could not attribute decides for itself.
    """
    sk = (session_key or "").strip()
    if not sk.startswith("dashboard:") or sk == "dashboard:ui":
        return False
    lookup = getattr(slots, "get", None) if slots is not None else None
    if lookup is None:
        return False
    if lookup(sk.split(":", 1)[1]) is not None:
        return False
    return _slot_by_linked_key(slots, sk) is None


#: The internal callers the dashboard routes recognize on ``X-Internal-Caller``.
#: Exact-listed and ratcheted in ``test_chat_folder_audit_origin.py``: adding a
#: caller here must be a conscious edit paired with a test, never a silent
#: widen — the point of the header is that a NEW internal caller surfaces as
#: ``unknown-internal`` in the audit until someone decides what to call it,
#: instead of silently inheriting another component's label.
KNOWN_INTERNAL_CALLERS = frozenset({"kirocrew-dashboard", "kirocrew-crew-log", "kirocrew-debug"})


def request_origin(
    request: web.Request, *, what: str = "write", log: logging.Logger | None = None
) -> tuple[str, str]:
    """SEL ``(source, caller)`` for a route driven by both the browser and MCP.

    ``source`` stays in SEL's documented *interface* vocabulary (``dashboard``,
    ``mcp``, ...) so operator queries like ``source == "mcp"`` keep matching
    every MCP-driven event uniformly; the validated component identity rides
    in ``caller``, which SEL already carries for exactly this purpose.

    A request without ``X-Internal-Secret`` is the browser:
    ``("dashboard", "dashboard")``. An internal request names its component in
    ``X-Internal-Caller`` (attached by the MCP stdio servers' shared loopback
    request helpers — see ``mcp_shared.set_internal_caller``), validated against
    :data:`KNOWN_INTERNAL_CALLERS`. Inferring the identity from the secret alone
    was correct only while exactly one internal caller existed, and would
    silently mislabel every write the moment a second one is added.

    Trust model: the secret is verified by the token-auth middleware before the
    handler runs, so authentication is settled here. The caller header is
    ATTRIBUTION on top of that — it grants nothing (a browser sending the header
    without the secret still audits as ``dashboard``), and an unrecognized or
    missing value on an authenticated internal request is recorded as
    ``caller="unknown-internal"`` with a warning rather than trusted into the
    audit log. ``what`` names the route class in that warning and ``log`` is the
    logger it is emitted under (the calling route module's, so its tests can
    listen for it).
    """
    if request.headers.get("X-Internal-Secret") is None:
        return "dashboard", "dashboard"
    caller = (request.headers.get("X-Internal-Caller") or "").strip()
    if caller in KNOWN_INTERNAL_CALLERS:
        return "mcp", caller
    (log or logger).warning(
        "internal %s without a recognized X-Internal-Caller (got %r) — audited as "
        "unknown-internal; a new internal caller must be added to "
        "KNOWN_INTERNAL_CALLERS alongside its ratchet test",
        what,
        caller[:64],
    )
    return "mcp", "unknown-internal"


def refuse_unattributable_caller(
    state: object, request: web.Request, operation: str
) -> web.Response | None:
    """403 when the caller NAMES a dashboard slot that is gone, else ``None``.

    ``effective_request_app`` answers ``""`` both for the person and for a
    caller it cannot place, and every app-isolation rule reads ``""`` as the
    person's full authority. That is sound for a caller that never had a slot —
    a Slack thread, a channel session, the person's own cron — but not for a
    ``dashboard:`` key, which NAMES a slot: absence there is not "nothing to
    confine me to", it is "the app I would have been confined to is exactly what
    got popped". A tab closing while one of its tool calls is still in flight
    produces precisely that, because the slot is popped synchronously without
    draining in-flight MCP calls.

    Deliberately NOT in the middleware: a popped slot cannot say whose tab
    it was, so refusing there would also refuse the person's own in-flight calls
    on every internal route at once. Each route that could not attribute a write
    decides for itself and names its ``operation`` for the audit line.
    """
    if caller_names_a_missing_slot(
        getattr(state, "_slots", None), request.headers.get("X-Session-Key", "")
    ):
        _sel_fn().log_api_access(
            caller="unattributable",
            forwarded=is_proxied_request(request),
            operation=operation,
            outcome="denied",
            source="app_isolation",
            resources=request.path,
            error="caller names a dashboard slot that is gone",
        )
        return web.json_response(
            {
                "error": "the calling session is gone, so this write cannot be attributed",
                "code": "caller_unattributable",
            },
            status=403,
        )
    return None


def app_owns_transcript(slots: object, request_app: str, history_key: str) -> bool:
    """Whether *request_app* owns the TRANSCRIPT a slot write would land on.

    A slot carries two identities: ``_app``, stamped at creation, and the
    transcript it currently writes to (``slot_history_key`` — its own
    ``dashboard:<key>`` file, or the session named by ``linked_session_key``).
    An ownership check on ``_app`` alone answers "does this app own the slot
    object", not "does it own the conversation the write persists into"; a slot
    linked to another owner's session would pass the first and fail the second.

    So a per-slot write on behalf of an app checks both: the slot's ``_app``
    (at the route) and, here, that EVERY slot whose conversation is that
    transcript — its own ``dashboard:<key>`` file, or a slot linked to that
    session — belongs to the same app. Unanimity is the point: two slots can be
    bound to one session, and resolving the key to "whichever slot matches
    first" would let the caller's own slot vouch for a transcript another
    owner's slot also writes. A transcript NO slot claims is refused too: that
    is the unbound channel-origin slot, whose ``slot_history_key`` is a channel
    transcript nothing in the registry is bound to, and an app is never granted
    reach into a conversation it cannot be shown to own. A CHANNEL transcript is
    refused outright: a Slack, Discord or other channel thread is the person's
    conversation, so no set of app-owned slots bound to it makes it the app's —
    the claim would be the app's own slots vouching for themselves. Pure and
    registry-only, like :func:`derive_caller_app`.
    """
    if not request_app:
        return True
    key = (history_key or "").strip()
    if not key or is_channel_session_key(key):
        return False
    owners: set[str] = set()
    values = getattr(slots, "values", None) if slots is not None else None
    for slot in values() if values is not None else ():
        own_file = f"dashboard:{getattr(slot, 'key', '')}"
        linked = str(getattr(slot, "linked_session_key", "") or "")
        if key == own_file or (linked and key == linked):
            owners.add(str(getattr(slot, "_app", "") or ""))
    return owners == {request_app}


def effective_request_app(state: object, request: web.Request) -> str:
    """App identity to enforce ownership against, or "" for the dashboard user.

    Reads the claim ``token_auth_middleware`` publishes, and re-derives through
    the SAME shared rule (:func:`derive_caller_app`) when it is absent.

    The internal-secret transport (the managed MCP set) carries no app claim of
    its own, so the middleware derives one for every route on that transport.
    The re-derivation here is defense-in-depth for a caller that reaches the
    handler without having passed that branch, and it calls the shared function
    rather than restating the rule so the two can never disagree.

    Never read from request BODY or tool arguments — a caller that could name
    its own scope could name someone else's.

    Lives here, beside the rule it wraps, so every route module (folders, tags)
    imports one authorization-identity helper instead of one feature module
    re-exporting another's.
    """
    declared = request.get("app", "")
    if declared:
        return str(declared)
    return derive_caller_app(
        getattr(state, "_slots", None),
        request.headers.get("X-Session-Key", ""),
    )


#: Request key under which the chat folder/tag gate
#: (``handlers/_shared_route_guards.py``'s ``private_chat_route_refusal``) stamps the VERIFIED
#: member principal (``member:<store>``) when it admits a member caller. The
#: constant lives HERE, the lowest layer, so the gate that writes it and
#: :func:`folder_principal` that reads it share one key and cannot drift.
MEMBER_CHAT_PRINCIPAL_KEY = "member_chat_principal"


def folder_principal(state: object, request: web.Request) -> str:
    """The principal that owns a folder written by *request*, or ``""``.

    The generalisation of :func:`effective_request_app` from "which app" to
    "which non-person principal", so the chat-folder tree fence
    (``chat_folders``' ``owner_app`` comparisons) can be one uniform check
    across app AND crew-member callers instead of two:

    * an APP caller -> its bare app name, EXACTLY what
      :func:`effective_request_app` returns and what ``owner_app`` has always
      stored, so every folder written before members existed keeps its meaning
      and no migration is needed;
    * an admitted crew MEMBER caller -> ``"member:<store>"``, read from the
      principal the gate already stamped on the VERIFIED scope (never a second
      config read on the event loop, never a body value). App names are
      validated identifiers that never begin ``member:``, so the two principal
      spaces cannot collide;
    * the person -> ``""`` (absent/empty ``owner_app``), unchanged.

    Ordering matters: the app claim is checked FIRST. A member never carries an
    app claim (``request["app"]`` is set only for a resolved app), so the two
    arms are mutually exclusive, but checking the app first keeps an app's
    principal byte-identical to what it was.
    """
    app = effective_request_app(state, request)
    if app:
        return app
    return str(request.get(MEMBER_CHAT_PRINCIPAL_KEY) or "")


def _cron_job_owner(jobs: object, job_id: str) -> str:
    """The app that created a cron job, or ``""`` (person-created, or no such job).

    Reads the in-memory job list CACHE-ONLY: no store lock, no ``_sync``, no
    disk I/O, because this runs ON the event loop and the cron store's
    synchronous readers deliberately refuse to be called there. The list is
    replaced by atomic reference assignment, so iterating one snapshot can never
    see a half-rebuilt list -- the same rationale the cron reaper's own
    lock-free read relies on.
    """
    if jobs is None or not job_id:
        # An empty id identifies nothing; matching a record that also happens to
        # carry an empty id would resolve a malformed key to somebody's app.
        return ""
    try:
        snapshot = list(cast("Iterable[Any]", jobs))  # one coherent list; never torn
    except Exception:  # noqa: BLE001 - an auth path must never 500 on this
        return ""
    for job in snapshot:
        if str(getattr(job, "id", "") or "") != job_id:
            continue
        created_by = str(getattr(job, "created_by", "") or "")
        if created_by.startswith("app:"):
            return created_by[len("app:") :]
        # A job the person created.
        return ""
    return ""


def _slot_by_linked_key(slots: object, session_key: str) -> object | None:
    """The slot whose ``linked_session_key`` is ``session_key``, or ``None``.

    Scans rather than indexes because the registry is keyed by slot name; the
    live slot count is small (one per open tab) and this runs only after the
    direct lookup missed. Defensive against a registry that is not a mapping and
    against a slot object without the attribute, because a raise here would turn
    an authenticated request into a 500.
    """
    try:
        candidates = list(getattr(slots, "values", lambda: [])())
    except Exception:  # noqa: BLE001 - a hostile/partial registry must not 500
        return None
    for slot in candidates:
        if str(getattr(slot, "linked_session_key", "") or "") == session_key:
            return slot
    return None


def _derive_internal_caller_app(request: web.Request) -> str:
    """:func:`derive_caller_app` bound to an aiohttp request's own context."""
    state = request.app.get("state")
    if state is None:
        return derive_caller_app(None, request.headers.get("X-Session-Key", ""))
    # ``_jobs`` / ``_agents`` directly, not store readers: this runs on the event
    # loop and the cron store's sync readers refuse that on purpose (they would
    # park the loop for the lock window). See ``_cron_job_owner``.
    jobs = getattr(getattr(state, "crons", None), "_jobs", None)
    subagents = getattr(getattr(state, "subagents", None), "_agents", None)
    return derive_caller_app(
        getattr(state, "_slots", None),
        request.headers.get("X-Session-Key", ""),
        jobs,
        subagents,
    )


def _internal_caller_record_missing(request: web.Request) -> bool:
    """:func:`caller_record_is_missing` bound to an aiohttp request's context."""
    state = request.app.get("state")
    if state is None:
        return False
    return caller_record_is_missing(
        request.headers.get("X-Session-Key", ""),
        getattr(getattr(state, "crons", None), "_jobs", None),
        getattr(getattr(state, "subagents", None), "_agents", None),
    )
