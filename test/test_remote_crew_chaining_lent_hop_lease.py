"""How a parent keeps the hop port it lends to a hub from reaching any other crew.

When ``mint_embed_token`` hands a hub a chained credential, the port it names is leased:
withheld from allocation until the token expires, kept in the registry so it survives a restart
and the crew's own removal, never shortened by a later mint in the record or in the cache, and
pruned once it lapses. These tests pin that reservation and the hold behind it. A lease that
cannot be written refuses the mint with the forward kept and any earlier lease restored, a hop
that moves before the lease lands refuses, and a refused mint reserves nothing. The lease is
cached before its write, and a settle landing inside that write keeps both the cache entry and
the guard's hold; the exit seam, entered through ``_on_tunnel_exit`` and even when self-heal is
skipped, holds a freed lent port from the cache with no registry read and never for a lapsed
entry, and a settle reseeds that cache from the registry. A dead forward's lent port is held
rather than counted in use; the teardown reads the leases before it stops the forward; the
recovery takes the holds before its backoff, releases only in the rebuild just before the bind
and re-settles on every exit; and a failed tier-one rebuild keeps the port owned across tier
two's mint. A torn-down lent hop stays bound by the guard, so a separate process cannot take
it, and reclaim leaves an orphan holding a lent port in place. ``hop_ownership_violations`` is
asserted empty with the forward live, after an unexpected exit, after a recovery that gives up
and after a teardown: for every live lease, a live forward owns its port or the guard holds it.
The lent lifetime is capped at ``LENT_HOP_TTL_CAP`` in the mint, the lease and the answer
alike, with a shorter row keeping its own, this gateway's own pane keeping the row's and the
cap leaving a re-mint room to finish. The retained lease map is bounded on both sides:
admission refuses a new lease at the count cap but still lets a held port move its deadline,
and clamps a far deadline in the file it writes; load clamps, sheds the furthest-future
overflow with a logged count while keeping the soonest-expiring leases, and lets a clamped
deadline decay rather than slide forward on every read; and ``HOP_LEASE_DEADLINE_CAP_SECS`` is
pinned equal to ``LENT_HOP_TTL_CAP`` in seconds. A topic of the ``test_remote_crew_chaining``
family: ``test_remote_crew_chaining.py`` keeps the data model and its chain walks, the forward,
the token path and the cycle guard, and the other siblings are
``test_remote_crew_chaining_subtree_removal.py``,
``test_remote_crew_chaining_route_admission.py``,
``test_remote_crew_chaining_credential_destination.py`` and
``test_remote_crew_chaining_chained_refresh_and_repair.py``, with the shared fakes in
``remote_crew_chaining_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import pytest
from remote_crew_chaining_helpers import _FakeTunnel, _mgr

from kiro_crew.subprocess_utf8 import UTF8_TEXT


class TestTheHopOwnershipInvariantHolds:
    """For every live lease, either a live forward owns its port or the guard holds it.

    Driven as STATES rather than call sites. The same defect arrived four times by four
    routes -- an orphaned forwarder at startup, a hold that failed once, an unexpected
    `ssh` exit, and that exit past `max_recovery_attempts` -- and each time the fix was a
    point patch and the next route was found by a reviewer. A list of sites is forgotten
    by the next path added; this asks the predicate instead, so a fifth route fails here
    without anyone having remembered to add a case for it.
    """

    def _lent(self, tmp_path, monkeypatch):
        """A crew connected, its hop lent, and a REAL guard installed."""
        reg, mgr = _mgr(tmp_path, monkeypatch, real_hop_guard=True)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port
        assert asyncio.run(mgr.mint_embed_token("c", 9191))[0] is True
        assert hop in reg.live_hop_leases(), "fixture did not lend the hop"
        return reg, mgr, hop

    def test_it_holds_while_the_forward_is_live(self, tmp_path, monkeypatch):
        """The baseline: a CONNECTED forward is itself the ownership, so no hold is due."""
        reg, mgr, hop = self._lent(tmp_path, monkeypatch)
        try:
            assert mgr.hop_ownership_violations() == {}
            assert hop not in mgr._hop_guard.held_ports(), "held a port a live forward serves"
        finally:
            mgr._hop_guard.close_all()

    def test_it_holds_after_an_unexpected_exit(self, tmp_path, monkeypatch):
        """The child dies: the entry and its port stay, the state moves, the OS frees it.

        Asserts the INVARIANT, not one of its branches. The self-heal may well rebuild
        onto the same port, and then the live forward is the owner and the guard is right
        to hold nothing -- an assertion that the guard holds it would contradict the
        invariant it is meant to check. Which branch satisfies it is recorded so a reader
        can see the case really did exercise one.
        """
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr, hop = self._lent(tmp_path, monkeypatch)
        try:
            mgr._tunnels["c"].status.state = TunnelState.ERROR
            assert mgr.hop_ownership_violations(), "fixture did not reach a violating state"

            asyncio.run(mgr._recover_after("c", 0))

            assert mgr.hop_ownership_violations() == {}
            live = mgr._tunnels.get("c")
            by_forward = live is not None and live.status.state is TunnelState.CONNECTED
            by_hold = hop in mgr._hop_guard.held_ports()
            assert by_forward or by_hold, "neither branch owns the port"
        finally:
            mgr._hop_guard.close_all()

    def test_it_holds_after_the_recovery_gives_up(self, tmp_path, monkeypatch):
        """Past `max_recovery_attempts` the ERROR entry stays for good, so this is the
        state that would otherwise persist for the credential's whole lifetime."""
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr, hop = self._lent(tmp_path, monkeypatch)
        # The give-up path also schedules a diagnosis, and the real one spawns
        # `ssh c-host` on this box. That task is still in flight when `asyncio.run`
        # returns, so the runner has to cancel it on the way out -- and a cancel that
        # lands while the subprocess transport is still being set up is a wait the
        # runner cannot bound, which on the macOS lane ran past the 180s test timeout.
        # The invariant under test says nothing about the diagnosis, so record that it
        # was asked for and run none.
        diagnoses: list[str] = []
        monkeypatch.setattr(mgr, "_schedule_diagnosis", diagnoses.append)
        try:
            mgr._tunnels["c"].status.state = TunnelState.ERROR
            mgr._recover_attempts["c"] = mgr._max_recovery + 5

            asyncio.run(mgr._recover_after("c", 0))

            assert diagnoses == ["c"], "fixture did not reach the give-up path"
            assert mgr.hop_ownership_violations() == {}, (
                "the give-up path leaves the lent port owned by nothing for the rest of "
                "the credential's life"
            )
            assert hop in mgr._hop_guard.held_ports()
        finally:
            mgr._hop_guard.close_all()

    def test_it_holds_after_a_teardown(self, tmp_path, monkeypatch):
        """The explicit path, which is also the one that frees the port deliberately."""
        reg, mgr, hop = self._lent(tmp_path, monkeypatch)
        try:
            asyncio.run(mgr.disconnect("c"))

            assert mgr.hop_ownership_violations() == {}
            assert hop in mgr._hop_guard.held_ports()
        finally:
            mgr._hop_guard.close_all()

    def test_it_holds_when_an_orphan_is_left_in_place(self, tmp_path, monkeypatch):
        """Reclamation refuses a still-lent port, so the ORPHAN is the owner there.

        The predicate cannot see a foreign process, so this case asserts the decision that
        makes the invariant true rather than the invariant itself: the reclaim returns
        before touching anything, leaving the port occupied.
        """
        import kiro_crew.instances.ssh_tunnel_manager as stm

        reg, mgr, hop = self._lent(tmp_path, monkeypatch)
        try:
            reg.update(
                "c", forwarder_pid=4321, local_port=hop, forwarder_start="1.0", forwarder_sig="x"
            )
            inst = reg.get("c")

            def _never(*_a, **_k):
                raise AssertionError("reclaim freed a port whose lease still stands")

            monkeypatch.setattr(stm, "_reclaim_identity_key", _never)
            asyncio.run(mgr._reclaim_orphan_forwarder(inst, mgr._resolve_transport(inst)))
        finally:
            mgr._hop_guard.close_all()


class TestLentHopReservation:
    """The PREVENTION layer: a lent hop is not handed to another crew.

    This is the layer that removes the window rather than bounding it. While a
    chained credential this gateway issued is still valid, the port it named is
    withheld from allocation -- so there is no moment at which the hub's forward,
    which holds only the NUMBER, reaches a different crew.
    """

    def test_a_lent_hop_is_withheld_from_allocation_until_the_token_expires(
        self, tmp_path, monkeypatch
    ):
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port
        assert hop > 0

        ok, payload = asyncio.run(mgr.mint_embed_token("c", 9191))
        assert ok is True and payload["port"] == hop

        assert hop in reg.live_hop_leases(), "the parent kept no record of the hop it lent"

        # The crew behind the hop goes away, which is what frees the port today and
        # is exactly when the hub's forward is still pointed at the number.
        asyncio.run(mgr.disconnect("c"))
        assert reg.get("c").local_port == 0, "fixture no longer frees the port"

        assert hop in mgr._reserved_ports(), "handed the lent hop back to the allocator"

    def test_the_reservation_lapses_once_the_credential_it_protects_is_dead(
        self, tmp_path, monkeypatch
    ):
        """Bounded, not permanent. After the TTL the hub's token cannot be used, so
        withholding the port any longer would leak ports for no benefit.
        """
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port
        assert asyncio.run(mgr.mint_embed_token("c", 9191))[0] is True
        asyncio.run(mgr.disconnect("c"))
        assert hop in mgr._reserved_ports()

        # Time passes beyond the 2h the token was issued under. Moved by replacing the
        # registry's clock, because `lend_hop` takes the LATER of the deadlines -- so a
        # shorter one cannot fake expiry, which is the property that stops a
        # second mint from shortening a reservation an earlier one earned.
        from kiro_crew.instances import registry as reg_mod

        later = time.time() + 3 * 3600
        # flake-ok: pre-existing rebind of the registry's stdlib time.time for the whole worker, scoped by monkeypatch, moved verbatim
        # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing rebind of the registry's stdlib clock moved verbatim; scoped by monkeypatch
        monkeypatch.setattr(reg_mod.time, "time", lambda: later)
        assert hop not in mgr._reserved_ports(), "withheld the port past the token's life"

    def test_the_reservation_survives_a_restart(self, tmp_path, monkeypatch):
        """Persisted on the row, not held in memory. A restart drops `_tunnels`, and
        an in-memory reservation would go with it while the hub's token is still
        valid -- handing the port to the next crew that connects.
        """
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port
        assert asyncio.run(mgr.mint_embed_token("c", 9191))[0] is True
        asyncio.run(mgr.disconnect("c"))

        # A second manager over the SAME registry file is the restart.
        _, restarted = _mgr(tmp_path, monkeypatch)
        assert hop in restarted._reserved_ports(), "a restart dropped the reservation"

    def test_a_reservation_that_cannot_be_written_refuses_the_mint(self, tmp_path, monkeypatch):
        """No reservation, no token. The record is what keeps this port away from
        another crew, so handing over a credential without it would be issuing one
        with nothing protecting its hop -- and a swallowed write error is exactly how
        that happens quietly. The registry hint helpers are deliberately best-effort
        (`except Exception: pass`), which is right for a hint and wrong for this.
        """
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port

        def refuse_lease(_port, _until):
            raise OSError("disk full")

        monkeypatch.setattr(reg, "lend_hop", refuse_lease)

        ok, payload = asyncio.run(mgr.mint_embed_token("c", 9191))
        assert ok is False, "handed over a credential it could not protect"
        assert payload["code"] == "instance_hop_lease_failed"
        assert payload["status"] == 503
        assert "token" not in payload, "leaked the token in the refusal"
        assert reg.live_hop_leases() == set()
        assert mgr.status("c").local_port == hop, "tore the forward down over a write error"
        # The cache is registered BEFORE the write, so a refused write must unwind it.
        # Left behind, it would withhold a port for a lease that does not exist and let
        # the exit seam bind one nothing names.
        assert hop not in mgr._lent_hops, "a refused lend left its cache entry behind"
        assert hop not in mgr._pending_lends, "a refused lend left its in-flight marker"

    def test_a_hop_that_moves_before_the_reservation_lands_refuses(self, tmp_path, monkeypatch):
        """The window the lock closes. The hop is read and verified before a mint that
        takes seconds, so a teardown and reconnect can move it in between -- and a
        reservation written then would name a port the crew does not serve while the
        reply named the one it does.
        """
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))

        real_mint = mgr._mint_token_with_parent_port

        async def mint_then_move(inst, params, parent_port, *, ttl):
            out = await real_mint(inst, params, parent_port, ttl=ttl)
            # The generation ends exactly as a real teardown ends it, while the mint
            # this gateway already committed to is still in flight.
            mgr._tunnel_epoch["c"] = mgr._tunnel_epoch.get("c", 0) + 1
            return out

        monkeypatch.setattr(mgr, "_mint_token_with_parent_port", mint_then_move)

        ok, payload = asyncio.run(mgr.mint_embed_token("c", 9191))
        assert ok is False, "named a hop that had already moved"
        assert payload["code"] == "instance_hop_changed"
        assert reg.live_hop_leases() == set(), "reserved a port for a refused mint"

    def test_removing_the_crew_does_not_release_its_lent_hop(self, tmp_path, monkeypatch):
        """The hole a per-row reservation had, and the reason the lease is not on the row.

        A hub holding this crew's credential forwards to the PORT. Removing the crew
        here deletes its row -- and a reservation stored on that row went with it, so
        the port was handed to the next crew that connected while the hub's token was
        still valid. That is exactly the disclosure the reservation exists to prevent,
        reached through the one path that deletes a row.
        """
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port
        assert asyncio.run(mgr.mint_embed_token("c", 9191))[0] is True

        assert reg.remove("c") is True
        assert reg.get("c") is None, "fixture no longer deletes the row"

        assert hop in reg.live_hop_leases(), "the row took the reservation with it"
        assert hop in mgr._reserved_ports(), "handed a lent hop back after a removal"

    def test_a_dead_forwards_lent_port_is_held_not_counted_as_in_use(self, tmp_path, monkeypatch):
        """The invariant, behaviourally: in use means LISTENING, not remembered.

        An unexpected `ssh` exit leaves its tunnel in `_tunnels` with `local_port`
        intact and the state ERROR, and past `max_recovery_attempts` it stays there for
        good. Counting that as in use skipped the hold for a port the OS had already
        freed while the credential naming it stayed valid -- the exposure itself, reached
        without any orphan, reclaim or restart.
        """
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = _mgr(tmp_path, monkeypatch, real_hop_guard=True)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port
        assert asyncio.run(mgr.mint_embed_token("c", 9191))[0] is True

        try:
            # While it is CONNECTED the forward's own listener is the ownership, so the
            # guard must NOT try to bind it.
            mgr._apply_hop_holds(reg.live_hop_lease_deadlines())
            assert hop not in mgr._hop_guard.held_ports(), "held a port a live forward serves"

            # The child dies unexpectedly: state moves, the entry and its port remain.
            mgr._tunnels["c"].status.state = TunnelState.ERROR
            assert mgr._tunnels["c"].status.local_port == hop, "fixture cleared the port"

            mgr._apply_hop_holds(reg.live_hop_lease_deadlines())
            assert hop in mgr._hop_guard.held_ports(), (
                "a dead forward's lent port was counted as in use, so nothing owns it "
                "while its credential is still valid"
            )
        finally:
            mgr._hop_guard.close_all()

    def test_a_second_mint_cannot_shorten_a_live_lease_in_the_cache(self, tmp_path, monkeypatch):
        """Max wins in the cache, the same rule the record and the guard already state.

        The durable record maxes (`InstancesRegistry.lend_hop`) and a hold is never
        shortened (`HopPortGuard.hold`), because the credential that earned the longer
        deadline stays valid until ITS deadline. The in-memory cache is the only table
        the synchronous exit seam can read, so a shorter deadline written over a live one
        there lets a tunnel exit past the short deadline find the lease lapsed, bind
        nothing, and leave the port free while the earlier credential still names it.
        """
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port

        # What an earlier mint for a longer-lived credential leaves behind.
        standing = time.time() + 30 * 3600
        mgr._lent_hops[hop] = standing
        reg.lend_hop(hop, standing)

        ok, _ = asyncio.run(mgr.mint_embed_token("c", 9191))
        assert ok is True, "the mint itself failed -- re-derive this pin"
        assert mgr._lent_hops[hop] == standing, (
            "a later mint shortened a live lease in the cache, so an exit after the "
            "shorter deadline reads it as lapsed and leaves the port unheld while the "
            "earlier credential is still valid"
        )
        assert hop not in mgr._pending_lends, "left an in-flight marker behind on success"

    def test_a_refused_lend_restores_an_earlier_lease_instead_of_deleting_it(
        self, tmp_path, monkeypatch
    ):
        """Unwinding this call's write must not unwind someone else's lease.

        The early registration is unwound when the record cannot be written, because a
        lease the registry does not name withholds a port for nothing. But popping the
        entry outright also erases a STANDING lease this call merely raised the deadline
        of, and that one protects a credential still in the hub's hands -- so the unwind
        would open the very unbound-port window the early registration exists to close.
        """
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port

        standing = time.time() + 30 * 3600
        mgr._lent_hops[hop] = standing

        def refuse_lease(_port, _until):
            raise OSError("disk full")

        monkeypatch.setattr(reg, "lend_hop", refuse_lease)

        ok, payload = asyncio.run(mgr.mint_embed_token("c", 9191))
        assert ok is False, "handed over a credential it could not protect"
        assert payload["code"] == "instance_hop_lease_failed"
        assert mgr._lent_hops.get(hop) == standing, (
            "a refused lend deleted a standing lease it did not create, leaving the only "
            "table the exit seam reads with no record of a hop whose credential is live"
        )
        assert hop not in mgr._pending_lends, "a refused lend left its in-flight marker"

    def test_the_recovery_releases_before_rebinding_and_resettles_on_every_exit(self):
        """Structural, because both orderings end with the port held.

        The recovery holds the port the moment the child exits, so the rebuild's bind
        would lose to our own hold unless it is released first -- and releasing without
        re-arming would trade a blocked self-heal for the exposure. The re-settle is in a
        `finally` rather than at each failure branch so a later `return` cannot skip it.
        """
        import ast
        import inspect
        import textwrap

        from kiro_crew.instances import ssh_tunnel_manager as stm

        after = ast.parse(
            textwrap.dedent(inspect.getsource(stm.SshTunnelManager._recover_after))
        ).body[0]
        # The arm must come BEFORE the backoff. The child is already gone, so waiting
        # for the sleep would leave the port unheld for the whole backoff on every flap
        # -- and for good once attempts run out.
        armed = [
            n.lineno
            for n in ast.walk(after)
            if isinstance(n, ast.Call) and "_apply_hop_holds" in ast.unparse(n)
        ]
        slept = [
            n.lineno
            for n in ast.walk(after)
            if isinstance(n, ast.Call) and "asyncio.sleep" in ast.unparse(n)
        ]
        assert armed, "the exit path never takes the holds"
        assert slept, "no backoff found -- re-derive this pin"
        assert min(armed) < min(slept), (
            "the holds are taken only after the backoff, so the lent port sits unheld "
            "for the whole delay while its credential is valid"
        )
        tries = [n for n in ast.walk(after) if isinstance(n, ast.Try) and n.finalbody]
        assert tries, "the re-settle is not in a finally, so a new return can skip it"
        settle = [
            n
            for t in tries
            for n in ast.walk(ast.Module(body=t.finalbody, type_ignores=[]))
            if isinstance(n, ast.Call) and "_apply_hop_holds" in ast.unparse(n)
        ]
        assert settle, "the finally does not re-settle the holds"

        recover = ast.parse(textwrap.dedent(inspect.getsource(stm.SshTunnelManager._recover))).body[
            0
        ]
        # `_recover` must release NOTHING. The only re-arm outside `_rebuild` is
        # `_recover_after`'s `finally` -- which runs after the WHOLE two-tier recovery --
        # so a release in this method leaves the port unheld across tier 2's mint whenever
        # tier 1's rebuild fails, bounded by the mint timeout rather than by a bind, with
        # the chained credential still naming it.
        stray = [
            n.lineno
            for n in ast.walk(recover)
            if isinstance(n, ast.Call) and "_hop_guard.release" in ast.unparse(n)
        ]
        assert stray == [], (
            f"the recovery releases the hold at line(s) {stray}; every slow step after it "
            f"-- the tier-2 mint above all -- then runs with the lent port unowned"
        )

        # The release belongs to the BIND, so it sits in `_rebuild` immediately before
        # `start()` with nothing awaited in between, and is taken straight back when the
        # bind does not produce a live forward.
        rebuild = ast.parse(textwrap.dedent(inspect.getsource(stm.SshTunnelManager._rebuild))).body[
            0
        ]
        rel = [
            n.lineno
            for n in ast.walk(rebuild)
            if isinstance(n, ast.Call) and "_hop_guard.release" in ast.unparse(n)
        ]
        start = [
            n.lineno
            for n in ast.walk(rebuild)
            if isinstance(n, ast.Call) and ast.unparse(n).endswith("tunnel.start()")
        ]
        assert rel, "the rebuild never releases, so its own bind loses to our hold"
        assert start, "no tunnel.start() found in _rebuild -- re-derive this pin"
        assert min(rel) < min(start), "the hold is released AFTER the bind it blocks"
        # The corrected criterion. Checking only that a re-arm EXISTS is what let the old
        # placement pass: what matters is the re-arm's POSITION against every await between
        # the release and the bind, because each one widens the unheld window.
        between = [
            n.lineno
            for n in ast.walk(rebuild)
            if isinstance(n, ast.Await) and min(rel) < n.lineno < min(start)
        ]
        assert between == [], (
            f"await(s) at {between} sit between the release and the bind, so the unheld "
            f"window is no longer just the bind"
        )
        rebuild_tries = [n for n in ast.walk(rebuild) if isinstance(n, ast.Try) and n.finalbody]
        rearm = [
            n
            for t in rebuild_tries
            for n in ast.walk(ast.Module(body=t.finalbody, type_ignores=[]))
            if isinstance(n, ast.Call) and "_hold_lent_port_by_number" in ast.unparse(n)
        ]
        assert rearm, (
            "the rebuild does not re-arm in a finally, so a failed or raising bind leaves "
            "the lent port unheld for whatever the caller does next"
        )

    def test_a_failed_tier_1_rebuild_owns_the_lent_port_across_tier_2s_mint(
        self, tmp_path, monkeypatch
    ):
        """The window itself, behaviourally: who owns the port while tier 2 mints.

        Taken from a peer session's independent fix for the same finding, because it is
        strictly stronger than the structural pin above: that one reads the source and
        this one drives the real two-tier recovery, so it would still fail if the release
        and the re-arm were arranged correctly and the port still ended up unowned.

        A lent hop's port is held the moment the child exits. Releasing it in the
        recovery's locked phase meant a FAILED tier-1 rebuild dropped straight into tier
        2's `_mint_for` -- bounded only by the mint timeout -- with no forward bound and
        no hold taken, while the hub kept forwarding the crew's bearer token to that
        loopback port. Any other local binder that took it was handed the credential.
        """
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        held_during_mint: list[bool] = []
        # `connect` and `mint_embed_token` mint too, and both run before the port is
        # known, so the probe arms only once this list carries the lent port.
        watching: list[int] = []

        class _RecordingGuard:
            """An inert guard that remembers what it was asked to own."""

            def __init__(self) -> None:
                self.held: set[int] = set()

            def sync(self, leases, in_use):
                return set()

            def hold(self, port, until):
                self.held.add(port)
                return True

            def release(self, port):
                self.held.discard(port)
                return None

            def held_ports(self):
                return set(self.held)

            def close_all(self):
                return None

        guard = _RecordingGuard()

        async def watching_mint(host, **kwargs):
            # Tier 2's re-mint. This is the span the finding is about.
            if watching:
                held_during_mint.append(watching[0] in guard.held_ports())
            return f"SSH_TOKEN_FOR_{host}"

        reg, mgr = _mgr(tmp_path, monkeypatch, mint=watching_mint)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port
        assert asyncio.run(mgr.mint_embed_token("c", 9191))[0] is True
        assert mgr._lent_hops.get(hop), "fixture no longer records the lend"

        mgr._hop_guard = guard
        # The child dies: the exit seam takes the port back, which is the state every
        # recovery starts from.
        mgr._tunnels["c"].status.state = TunnelState.ERROR
        mgr._hold_lent_port_now("c")
        assert hop in guard.held_ports(), "fixture did not arm the hold"
        watching.append(hop)

        # Tier 1 fails to bind; tier 2 then re-mints and rebuilds.
        made: list = []

        def failing_then_ok(*a, **kw):
            t = _FakeTunnel(*a, **kw)
            t.start_result = len(made) > 0
            made.append(t)
            return t

        mgr._tunnel_factory = failing_then_ok
        asyncio.run(mgr._recover("c"))

        assert held_during_mint, "tier 2 never re-minted -- re-derive this pin"
        assert all(held_during_mint), (
            "the lent port was unowned while tier 2 minted: no forward was bound and no "
            "hold was taken, so another local binder could take it and be handed the "
            "credential the hub is still forwarding there"
        )

    def test_the_lease_is_cached_before_its_write_not_after(self, tmp_path, monkeypatch):
        """The window between deciding to lend and the record saying so.

        `lend_hop` runs in a thread, and the synchronous exit seam answers from the cache
        alone precisely so it can bind with no await. Caching only AFTER the write meant
        an ssh exit landing inside that thread hop found no lease, bound nothing, and the
        allocator handed the freed port to the next connect -- while the credential naming
        it had already been returned. Both values are known before the await, so nothing
        forces the late order.

        Probed from inside the write itself, which is exactly what the seam would see if
        the child died at that instant.
        """
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port

        real_lend = reg.lend_hop
        seen: dict[str, object] = {}

        def probe(port, until):
            seen["cached_during_write"] = mgr._lent_hops.get(port)
            # A settle landing in this same window must not wipe it either. The registry
            # does not name the lease yet, so a plain re-seed from the record would drop
            # it and reopen the gap -- `_pending_lends` is what survives that.
            mgr._apply_hop_holds(reg.live_hop_lease_deadlines())
            seen["cached_after_settle"] = mgr._lent_hops.get(port)
            return real_lend(port, until)

        monkeypatch.setattr(reg, "lend_hop", probe)
        assert asyncio.run(mgr.mint_embed_token("c", 9191))[0] is True

        assert seen.get("cached_during_write"), (
            "the lease was absent from the cache while its own write was in flight, so an "
            "exit landing in the thread hop would bind nothing and free the port"
        )
        assert seen.get("cached_after_settle"), (
            "a settle inside the write wiped the pre-registered lease, which reopens the "
            "window the early registration exists to close"
        )
        # The marker is transient: it exists only while a write is unresolved.
        assert hop not in mgr._pending_lends, "an in-flight marker outlived its write"
        assert mgr._lent_hops.get(hop), "the lease left the cache once its write landed"

    def test_a_settle_inside_an_in_flight_lend_keeps_the_hold_not_just_the_cache(
        self, tmp_path, monkeypatch
    ):
        """The other half of the same window, and the half that owns the socket.

        Registering the lease early is only useful if the settle that follows does not
        undo it. `sync` releases every held or owed port absent from the set it is handed,
        so handing it the registry-only snapshot while a lend was still in flight released
        the hold the exit seam had just taken -- and nothing re-takes it until after the
        recovery backoff, so the port sat unowned while the credential naming it stayed
        valid. Asserted on the GUARD, because the cache surviving proves nothing about who
        owns the port.
        """
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = _mgr(tmp_path, monkeypatch, real_hop_guard=True)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))

        real_lend = reg.lend_hop
        seen: dict[str, object] = {}

        def probe(port, until):
            # The real sequence, inside the window: the child dies, the synchronous seam
            # takes the hold, and only then does a settle land.
            mgr._tunnels["c"].status.state = TunnelState.ERROR
            mgr._hold_lent_port_now("c")
            seen["held_before_settle"] = port in mgr._hop_guard.held_ports()
            mgr._apply_hop_holds(reg.live_hop_lease_deadlines())
            seen["held_after_settle"] = port in mgr._hop_guard.held_ports()
            return real_lend(port, until)

        monkeypatch.setattr(reg, "lend_hop", probe)
        try:
            assert asyncio.run(mgr.mint_embed_token("c", 9191))[0] is True
            assert seen.get("held_before_settle"), "the exit seam did not take the hold"
            assert seen.get("held_after_settle"), (
                "a settle inside the in-flight lend RELEASED the hold, leaving the lent "
                "port unowned while its credential is still valid"
            )
        finally:
            mgr._hop_guard.close_all()

    def test_the_exit_seam_holds_from_the_cache_without_reading_the_registry(
        self, tmp_path, monkeypatch
    ):
        """The invariant restored SYNCHRONOUSLY, with no await between free and bind.

        The child is gone by the time the seam runs, so the lent port is free from that
        instant; any await before the bind -- the thread hop of a registry read included
        -- is a window another local process can take it in. So the seam answers from the
        in-memory cache, and this case proves it by making every registry read raise: if
        the seam consults the record at all, it fails here.
        """
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = _mgr(tmp_path, monkeypatch, real_hop_guard=True)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port
        assert asyncio.run(mgr.mint_embed_token("c", 9191))[0] is True
        assert mgr._lent_hops.get(hop), "the mint did not cache the lent hop"

        try:
            # The child dies; the entry and its port stay, the state moves.
            mgr._tunnels["c"].status.state = TunnelState.ERROR

            def _no_reads(*_a, **_k):
                raise AssertionError("the exit seam read the registry instead of the cache")

            monkeypatch.setattr(reg, "live_hop_lease_deadlines", _no_reads)
            monkeypatch.setattr(reg, "live_hop_leases", _no_reads)

            mgr._hold_lent_port_now("c")

            assert hop in mgr._hop_guard.held_ports(), "the freed lent port was not taken"
            assert mgr.hop_ownership_violations({hop: mgr._lent_hops[hop]}) == {}
        finally:
            mgr._hop_guard.close_all()

    def test_the_exit_seam_itself_takes_the_port_back(self, tmp_path, monkeypatch):
        """Driven through ``_on_tunnel_exit``, not through the helper it calls.

        The helper being correct is worth nothing if the seam does not call it, and that
        is a separate property no other case here covers: every other cache test invokes
        ``_hold_lent_port_now`` directly, so deleting the call from the seam leaves them
        all green. This drives the real entry point the monitor uses.
        """
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = _mgr(tmp_path, monkeypatch, real_hop_guard=True)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port
        assert asyncio.run(mgr.mint_embed_token("c", 9191))[0] is True

        async def _drive() -> bool:
            mgr._tunnels["c"].status.state = TunnelState.ERROR
            mgr._on_tunnel_exit("c")
            # Read the holds BEFORE letting the scheduled recovery run: the property
            # under test is that the seam binds on its way out, not that something
            # later reconciles it.
            held = hop in mgr._hop_guard.held_ports()
            for task in list(mgr._recovery_tasks):
                task.cancel()
            # flake-ok: pre-existing one-tick yield, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)
            return held

        try:
            assert asyncio.run(_drive()), "the exit seam did not take its lent port back"
        finally:
            mgr._hop_guard.close_all()

    def test_the_port_is_taken_back_even_when_self_heal_is_skipped(self, tmp_path, monkeypatch):
        """Ordering: the hold precedes every early return in the seam.

        A reconfiguration makes the seam return before it schedules any recovery, so
        this is the path on which a hold placed after that branch would never happen at
        all -- the port would stay free for the rest of the lease with no later pass to
        notice. It pins the hold as the seam's FIRST act rather than one of its outcomes.
        """
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = _mgr(tmp_path, monkeypatch, real_hop_guard=True)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port
        assert asyncio.run(mgr.mint_embed_token("c", 9191))[0] is True

        try:
            mgr._tunnels["c"].status.state = TunnelState.ERROR
            mgr._reconfiguring.add("c")
            mgr._on_tunnel_exit("c")
            assert not mgr._recovery_tasks, "self-heal was scheduled -- re-derive this pin"
            assert (
                hop in mgr._hop_guard.held_ports()
            ), "the seam skipped the hold along with the self-heal"
        finally:
            mgr._hop_guard.close_all()

    def test_a_lapsed_cache_entry_is_not_held(self, tmp_path, monkeypatch):
        """The cache mirrors a deadline, so it must not resurrect a dead credential."""
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = _mgr(tmp_path, monkeypatch, real_hop_guard=True)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port
        assert asyncio.run(mgr.mint_embed_token("c", 9191))[0] is True

        try:
            mgr._lent_hops[hop] = time.time() - 1  # the credential has already died
            mgr._tunnels["c"].status.state = TunnelState.ERROR

            mgr._hold_lent_port_now("c")

            assert (
                hop not in mgr._hop_guard.held_ports()
            ), "squatted a port whose credential had already expired"
        finally:
            mgr._hop_guard.close_all()

    def test_a_settle_reseeds_the_cache_from_the_registry(self, tmp_path, monkeypatch):
        """The record is the source of truth, so memory never outlives it."""
        reg, mgr = _mgr(tmp_path, monkeypatch, real_hop_guard=True)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        assert asyncio.run(mgr.mint_embed_token("c", 9191))[0] is True

        try:
            live_ports = reg.live_hop_leases()
            cache_only_port = next(
                port for port in range(1, len(live_ports) + 2) if port not in live_ports
            )
            mgr._lent_hops[cache_only_port] = time.time() + 999
            mgr._apply_hop_holds(reg.live_hop_lease_deadlines())
            assert (
                cache_only_port not in mgr._lent_hops
            ), "a cache entry the registry never had survived"
            assert set(mgr._lent_hops) == set(reg.live_hop_leases())
        finally:
            mgr._hop_guard.close_all()

    def test_the_lease_read_happens_before_the_port_is_released(self):
        """Structural, because this is about the WIDTH of a window, not its existence.

        The forward must release the port before the guard can bind it, so a gap is
        unavoidable; what matters is that nothing is awaited inside it. Reading the
        lease table is the slow half and has to leave the event loop, but doing that
        read BETWEEN the stop and the bind would stretch the gap from two adjacent
        statements to however long the SHARED default executor takes to schedule --
        unbounded when it is busy, and it is the same executor the identity read uses.
        So the read is taken ahead of the stop and only the bind stays in line.

        No behavioural test can see this: both orderings end with the port held, and
        they differ only in how long it was free in between.
        """
        import ast
        import inspect
        import textwrap

        from kiro_crew.instances import ssh_tunnel_manager as stm

        fn = ast.parse(
            textwrap.dedent(inspect.getsource(stm.SshTunnelManager._teardown_locked))
        ).body[0]
        lines = {}
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                # The read is an ARGUMENT to `to_thread`, not the call target, so this
                # one has to look at the whole call rather than just its func.
                if "live_hop_lease_deadlines" in ast.unparse(node):
                    lines["read"] = node.lineno
                elif name.endswith("tunnel.stop"):
                    lines["stop"] = node.lineno
                elif "_apply_hop_holds" in name:
                    lines["bind"] = node.lineno
        assert set(lines) == {"read", "stop", "bind"}, f"shape changed: {lines}"
        assert lines["read"] < lines["stop"], (
            "the lease read moved into the window between the port's release and its "
            "hold, so the gap is now as long as the shared executor takes to schedule"
        )
        assert lines["stop"] < lines["bind"], "the hold is taken before the port is free"

    def test_reclaim_will_not_free_a_port_whose_lease_still_stands(self, tmp_path, monkeypatch):
        """Fail closed: an orphan holding a LENT port is left alone.

        Reclaiming exists to give a crew its recorded port back, and a lent port is one
        `allocate` deliberately routes around -- so freeing it recovers nothing and
        converts a port safely occupied by our own dead forwarder into a free one a
        stranger can bind while a hub still forwards a bearer token to it. The orphan is
        the ownership here, and a stronger one than a held socket.

        Pinned by ordering: the identity-key read is the reclaim's first real step, so
        making it raise proves the lease check returned BEFORE anything was inspected or
        signalled.
        """
        import kiro_crew.instances.ssh_tunnel_manager as stm

        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port
        assert asyncio.run(mgr.mint_embed_token("c", 9191))[0] is True
        assert hop in reg.live_hop_leases(), "fixture did not lend the hop"

        # Enough recorded identity to get past the "nothing we may touch" gate.
        reg.update(
            "c", forwarder_pid=4321, local_port=hop, forwarder_start="1.0", forwarder_sig="x"
        )
        inst = reg.get("c")

        def _never(*_a, **_k):
            raise AssertionError("reclaim inspected an orphan on a port that is still lent")

        monkeypatch.setattr(stm, "_reclaim_identity_key", _never)

        params = mgr._resolve_transport(inst)
        asyncio.run(mgr._reclaim_orphan_forwarder(inst, params))

    def test_a_torn_down_lent_hop_is_owned_not_merely_skipped(self, tmp_path, monkeypatch):
        """The reservation's teeth, measured from OUTSIDE this process.

        Every assertion above is about OUR allocator, and that is the whole gap: an
        excluded port is still free at the OS level, so any other local process --
        including a second gateway with its own registry, which cannot see this lease
        at all -- could bind it and be handed the chained crew's bearer credential by a
        pane that has noticed nothing. Asserting `_reserved_ports` again would re-pin
        the defect, so this binds from a separate process instead.
        """
        import subprocess
        import sys

        reg, mgr = _mgr(tmp_path, monkeypatch, real_hop_guard=True)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port
        assert asyncio.run(mgr.mint_embed_token("c", 9191))[0] is True

        asyncio.run(mgr.disconnect("c"))

        try:
            assert (
                hop in mgr._hop_guard.held_ports()
            ), "teardown freed a lent hop port without taking ownership of it"
            probe = (
                "import socket,sys\n"
                "s=socket.socket()\n"
                "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
                "try:\n"
                "    s.bind(('127.0.0.1', int(sys.argv[1])))\n"
                "except OSError:\n"
                "    print('REFUSED')\n"
                "else:\n"
                "    print('BOUND')\n"
                "finally:\n"
                "    s.close()\n"
            )
            out = subprocess.run(
                [sys.executable, "-c", probe, str(hop)],
                capture_output=True,
                timeout=60,
                # Pinned, not inherited: text mode without it decodes with the
                # Windows ANSI code page.
                **UTF8_TEXT,
            )
            assert (
                out.stdout.strip() == "REFUSED"
            ), f"another process took the lent hop port {hop} while its lease stands"
        finally:
            mgr._hop_guard.close_all()

    def test_the_lease_survives_a_restart_after_the_crew_is_gone(self, tmp_path, monkeypatch):
        """Both losses at once: the row deleted AND the process restarted. Nothing in
        memory and nothing on the crew's row remains to carry it, so only a record kept
        at the document level answers.
        """
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port
        assert asyncio.run(mgr.mint_embed_token("c", 9191))[0] is True
        reg.remove("c")

        _, restarted = _mgr(tmp_path, monkeypatch)
        assert hop in restarted._reserved_ports(), "lost the lease across removal + restart"

    def test_a_later_mint_cannot_shorten_a_standing_lease(self, tmp_path, monkeypatch):
        """Two hubs can hold credentials against the same hop, and the later mint is
        not necessarily the longer one. Taking the newest deadline would release the
        port while the FIRST hub's token was still valid -- so the later of the two
        wins, and a short lease never overwrites a long one.

        Both deadlines sit INSIDE :data:`HOP_LEASE_DEADLINE_CAP_SECS` and the longer one is
        derived from it, because admission clamps to that cap: a fixture past it would be
        measuring the clamp rather than max-wins, and the real writer cannot exceed it
        either (it takes the shorter of the crew's row TTL and ``LENT_HOP_TTL_CAP``).
        """
        from kiro_crew.instances.constants import HOP_LEASE_DEADLINE_CAP_SECS

        reg, _ = _mgr(tmp_path, monkeypatch)
        far = time.time() + HOP_LEASE_DEADLINE_CAP_SECS - 60
        reg.lend_hop(53993, far)
        reg.lend_hop(53993, time.time() + 30)

        doc = json.loads((tmp_path / "instances.json").read_text())
        assert float(doc["hop_leases"]["53993"]) == far, "a later mint shortened the lease"

    def test_a_lapsed_lease_is_pruned_rather_than_kept_forever(self, tmp_path, monkeypatch):
        """Bounded storage without a timer: the only writer prunes on its way through,
        so a gateway that lends many hops over a long life does not accumulate them.
        """
        reg, _ = _mgr(tmp_path, monkeypatch)
        reg.lend_hop(53991, time.time() - 1)
        reg.lend_hop(53992, time.time() + 3600)
        assert reg.live_hop_leases() == {53992}
        # The lapsed one is gone from the record, not merely filtered on read.
        doc = json.loads((tmp_path / "instances.json").read_text())
        assert sorted(doc["hop_leases"]) == ["53992"], "kept a lapsed lease on disk"

    def test_a_refused_mint_reserves_nothing(self, tmp_path, monkeypatch):
        """No token handed over, no port to protect. Reserving on a refusal would
        withhold one of our own ports for the TTL over a credential that never left.
        """
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        # Never connected here, so there is no hop to ride and the mint refuses.
        ok, payload = asyncio.run(mgr.mint_embed_token("c", 9191))
        assert ok is False and payload["code"] == "instance_not_connected"
        assert reg.live_hop_leases() == set(), "reserved a port for a token it never issued"


class TestALentCredentialIsShortLived:
    """The BOUND on the window a gateway exit opens.

    A lent port is held for the life of its lease, and a socket cannot outlive the
    process holding it: the exit releases every hold while the credential naming
    that port stays valid, because the remote crew issued it and nothing here can
    invalidate it. The length of that window is therefore the one part of it this
    gateway chooses, and it chooses it by capping the lifetime it lends under.

    Three values have to BE that one number -- the lifetime the remote crew issues
    the token for, the deadline the lease is recorded under, and the figure the
    answer reports -- so each case checks all three rather than one.
    """

    @staticmethod
    def _recording_mint(seen: list[str]):
        async def mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            seen.append(ttl)
            return f"SSH_TOKEN_FOR_{host}_epp{embed_parent_port}"

        return mint

    def test_the_lent_lifetime_the_lease_and_the_answer_are_one_capped_number(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.instances.constants import LENT_HOP_TTL_CAP
        from kiro_crew.instances.token_mint import ttl_to_seconds

        asked: list[str] = []
        reg, mgr = _mgr(tmp_path, monkeypatch, mint=self._recording_mint(asked))
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="20h")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port
        assert hop > 0
        asked.clear()  # the connect's own mint is this gateway's pane, not the lent one

        before = time.time()
        ok, payload = asyncio.run(mgr.mint_embed_token("c", 9191))
        assert ok is True

        cap = ttl_to_seconds(LENT_HOP_TTL_CAP)
        assert cap < ttl_to_seconds("20h"), "the cap is not shorter than the row it bounds"

        assert asked == [LENT_HOP_TTL_CAP], f"the remote crew was asked for {asked}"
        assert payload["ttl"] == LENT_HOP_TTL_CAP, "reported a lifetime it did not mint"
        deadline = reg.live_hop_lease_deadlines()[hop]
        # flake-ok: pre-existing wall-clock window around one in-process mint, moved verbatim; 30 s of slack
        assert deadline - before == pytest.approx(
            cap, abs=30
        ), f"the lease runs {round(deadline - before)}s, not the {cap}s it lent under"

    def test_a_row_shorter_than_the_cap_keeps_its_own_lifetime(self, tmp_path, monkeypatch):
        """A minimum, not an assignment. An operator who wants a tighter window than
        the cap gets it; raising the cap to their row would widen the exposure they
        asked to narrow.
        """
        from kiro_crew.instances.constants import LENT_HOP_TTL_CAP
        from kiro_crew.instances.token_mint import ttl_to_seconds

        assert ttl_to_seconds("5m") < ttl_to_seconds(LENT_HOP_TTL_CAP), "5m is not the shorter one"

        asked: list[str] = []
        reg, mgr = _mgr(tmp_path, monkeypatch, mint=self._recording_mint(asked))
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="5m")
        asyncio.run(mgr.connect("c"))
        hop = mgr.status("c").local_port
        asked.clear()

        before = time.time()
        assert asyncio.run(mgr.mint_embed_token("c", 9191))[0] is True

        assert asked == ["5m"], f"lent under {asked} instead of the shorter row"
        deadline = reg.live_hop_lease_deadlines()[hop]
        # flake-ok: pre-existing wall-clock window around one in-process mint, moved verbatim; 30 s of slack
        assert deadline - before == pytest.approx(ttl_to_seconds("5m"), abs=30)

    def test_this_gateways_own_pane_keeps_the_rows_lifetime(self, tmp_path, monkeypatch):
        """The cap is on the credential that LEAVES. This gateway's own pane is not
        lent to anyone, nothing releases a hold on its behalf, and capping it would
        re-mint every half hour for no window closed.
        """
        from kiro_crew.instances.constants import LENT_HOP_TTL_CAP

        asked: list[str] = []
        reg, mgr = _mgr(tmp_path, monkeypatch, mint=self._recording_mint(asked))
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="20h")
        asyncio.run(mgr.connect("c"))

        assert asked == ["20h"], f"capped this gateway's own mint: {asked}"
        assert LENT_HOP_TTL_CAP not in asked

    def test_the_cap_leaves_a_re_mint_room_to_finish(self):
        """Computed from the same constants the scheduler uses, so lowering the cap
        without re-checking the arithmetic fails here rather than in production.

        The refresh fires at a fraction of the lifetime, leaving the remainder as the
        margin a re-mint has to complete inside. A cap small enough to eat that margin
        expires the token mid-mint, and the hub's pane reloads every cycle.
        """
        from kiro_crew.instances.constants import (
            DEFAULT_SSM_MINT_TIMEOUT_SECS,
            DEFAULT_TOKEN_REFRESH_FRACTION,
            LENT_HOP_TTL_CAP,
            MINT_TIMEOUT_CEILING_SECS,
        )
        from kiro_crew.instances.token_mint import ttl_to_seconds

        margin = ttl_to_seconds(LENT_HOP_TTL_CAP) * (1.0 - DEFAULT_TOKEN_REFRESH_FRACTION)
        slowest = max(MINT_TIMEOUT_CEILING_SECS + 15.0, DEFAULT_SSM_MINT_TIMEOUT_SECS)
        assert margin > slowest, (
            f"a {LENT_HOP_TTL_CAP} cap leaves {margin:.0f}s to re-mint, under the "
            f"{slowest:.0f}s the slowest mint is allowed"
        )


class TestTheRetainedLeaseMapIsBounded:
    """`a-bound-bounds-every-field-it-retains`, on both sides of the map's lifetime.

    Startup binds one listening socket per non-in-use lease, so an unbounded map is a
    descriptor burst as well as unbounded storage. The bound is enforced twice because the
    two sides can do different things about a violation: ADMISSION can refuse, and does,
    since a refused mint is a credential never issued; LOAD cannot refuse -- the write is
    already on disk and may be foreign -- so it clamps and says so.
    """

    def test_the_deadline_cap_constant_matches_the_ttl_cap_it_mirrors(self):
        """Two spellings of one bound, and nothing computes one from the other.

        The registry must not import `ttl_to_seconds` (it sits below the mint in the
        dependency order), so the seconds form is a literal. A literal can drift from the
        string it mirrors, and drift would silently widen or narrow the load clamp, so the
        equality is pinned rather than trusted.
        """
        from kiro_crew.instances.constants import HOP_LEASE_DEADLINE_CAP_SECS, LENT_HOP_TTL_CAP
        from kiro_crew.instances.token_mint import ttl_to_seconds

        assert HOP_LEASE_DEADLINE_CAP_SECS == ttl_to_seconds(LENT_HOP_TTL_CAP), (
            "the seconds-valued cap the registry clamps to has drifted from the TTL cap "
            "the writer enforces, so one of the two bounds is no longer the bound"
        )

    def test_admission_refuses_a_new_lease_at_the_cap_but_still_refreshes_a_live_one(
        self, tmp_path
    ):
        """Refuse, do not trim. Every entry names a credential that is still valid.

        Trimming to admit a new lease would hand out a loopback port some live token still
        points at -- the exact misdirection the guard exists to remove. A refusal costs one
        connect instead. Refreshing a port already held cannot grow the map, so it must
        still be allowed at the cap, or a chained crew would stop being able to re-mint
        precisely when the map is full.
        """
        from kiro_crew.instances.constants import HOP_LEASE_MAX
        from kiro_crew.instances.registry import InstancesRegistry, InvalidInstanceError

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        soon = time.time() + 600
        for i in range(HOP_LEASE_MAX):
            reg.lend_hop(20000 + i, soon)
        assert len(reg.live_hop_leases()) == HOP_LEASE_MAX

        with pytest.raises(InvalidInstanceError) as excinfo:
            reg.lend_hop(30000, soon)
        assert "cap" in str(excinfo.value), "the refusal does not say what bound it hit"
        assert 30000 not in reg.live_hop_leases(), "the refused lease was recorded anyway"
        assert len(reg.live_hop_leases()) == HOP_LEASE_MAX, "the refusal changed the map"

        # The control: at the cap, a port already held can still move its deadline.
        reg.lend_hop(20000, soon + 300)
        assert reg.live_hop_lease_deadlines()[20000] == pytest.approx(soon + 300)

    def test_admission_clamps_a_deadline_past_the_cap(self, tmp_path):
        """The writer already clamps; this is the bound holding for EVERY writer.

        Read from the RAW FILE, not through the registry: the loader clamps too, so a read
        through `live_hop_lease_deadlines` answers the same either way and would pass with
        admission's clamp deleted. Mutation M40 proved exactly that -- it survived a
        through-the-registry assertion -- so the two bounds have to be measured apart.
        """
        from kiro_crew.instances.constants import HOP_LEASE_DEADLINE_CAP_SECS
        from kiro_crew.instances.registry import InstancesRegistry

        path = tmp_path / "instances.json"
        reg = InstancesRegistry(path=path)
        reg.lend_hop(20001, time.time() + 86400 * 30)
        stored = float(json.loads(path.read_text())["hop_leases"]["20001"])
        # flake-ok: pre-existing wall-clock ceiling with 5 s of slack, moved verbatim
        assert stored <= time.time() + HOP_LEASE_DEADLINE_CAP_SECS + 5, (
            f"a month-long deadline was WRITTEN as {stored}, so one write can withhold a "
            f"port far past the cap the writer respects -- the loader hides this on read"
        )

    def test_the_loader_clamps_a_far_future_deadline_that_no_overflow_shed_would_remove(
        self, tmp_path
    ):
        """The load clamp measured ALONE, with the map under the count cap.

        Kept separate from the overflow case because the two bounds mask each other: in an
        OVER-cap map the furthest-future entry is the one the shed drops, so the shed
        removes the very lease that would prove the clamp missing. Mutation M42 survived
        the combined assertion for that reason.
        """
        from kiro_crew.instances.constants import HOP_LEASE_DEADLINE_CAP_SECS, HOP_LEASE_MAX
        from kiro_crew.instances.registry import InstancesRegistry

        now = time.time()
        path = tmp_path / "instances.json"
        path.write_text(
            json.dumps({"instances": [], "hop_leases": {"25000": now + 86400 * 365}}) + "\n"
        )
        reg = InstancesRegistry(path=path)
        live = reg.live_hop_lease_deadlines()

        assert len(live) == 1 <= HOP_LEASE_MAX, "fixture is not under the count cap"
        # flake-ok: pre-existing wall-clock ceiling with 5 s of slack, moved verbatim
        assert live[25000] <= now + HOP_LEASE_DEADLINE_CAP_SECS + 5, (
            f"a deadline a year out survived the load as {live[25000]}, so a foreign write "
            f"withholds that port for a year across every restart"
        )

    def test_the_loader_clamps_a_stored_deadline_and_sheds_the_furthest_future_overflow(
        self, tmp_path, caplog
    ):
        """A foreign write is already on disk, so the loader clamps instead of refusing.

        The SOONEST-expiring entries are kept: dropping those would let one oversized write
        pin the cap's worth of ports for the full horizon. Loud with a count, because every
        shed lease makes a port allocatable while a credential may still name it.
        """
        from kiro_crew.instances.constants import HOP_LEASE_DEADLINE_CAP_SECS, HOP_LEASE_MAX
        from kiro_crew.instances.registry import InstancesRegistry

        now = time.time()
        # Written directly, not through `lend_hop`: admission would refuse this map, which
        # is exactly why the loader is the only place it can appear.
        over = HOP_LEASE_MAX + 20
        leases = {str(21000 + i): now + 300 + i for i in range(over)}
        leases["25000"] = now + 86400 * 365  # and one deadline a year out
        discarded = len(leases) - HOP_LEASE_MAX
        path = tmp_path / "instances.json"
        path.write_text(json.dumps({"instances": [], "hop_leases": leases}) + "\n")

        with caplog.at_level(logging.ERROR):
            reg = InstancesRegistry(path=path)
            live = reg.live_hop_lease_deadlines()

        assert (
            len(live) == HOP_LEASE_MAX
        ), f"the loader kept {len(live)} leases, so the retained map is still unbounded"
        # flake-ok: pre-existing wall-clock ceiling with 5 s of slack, moved verbatim
        assert max(live.values()) <= now + HOP_LEASE_DEADLINE_CAP_SECS + 5, (
            "a stored deadline past the cap survived the load, so a foreign write can "
            "withhold a port for as long as it likes"
        )
        # Soonest-expiring kept: the lowest ports carry the nearest deadlines here.
        assert 21000 in live, "the soonest-expiring lease was shed instead of kept"
        assert str(discarded) in caplog.text, (
            "the overflow was shed without logging how many, so nothing says which ports "
            "became allocatable"
        )

    def test_a_clamped_deadline_decays_instead_of_sliding_forward_on_every_read(self, tmp_path):
        """The clamp must bound the lease's LIFETIME, not just the number it reports.

        A horizon taken from the clock is recomputed per read, so an over-cap deadline comes
        back as a fresh `now + cap` every time and never satisfies `u <= now`: the port is
        withheld for as long as the process lives and `sync_hop_holds` keeps a socket bound
        on it. Anchoring to the file's mtime makes the horizon a fixed point, so the lease
        lapses one cap after the last write.

        The two halves are what make this fail in both directions. Rather than patching the
        clock, the file's own mtime is moved into the past -- equivalent, and it needs no
        process-wide patch of `time.time`, which every other clock reader in the run shares.

        Note what the sibling clamp test CANNOT see: it asserts the reported deadline is
        `<= now + cap`, which is true under both anchors, so mutation M42 is about the clamp
        EXISTING. This case is about it decaying.
        """
        from kiro_crew.instances.constants import HOP_LEASE_DEADLINE_CAP_SECS
        from kiro_crew.instances.registry import InstancesRegistry

        now = time.time()
        path = tmp_path / "instances.json"
        path.write_text(
            json.dumps({"instances": [], "hop_leases": {"25001": now + 86400 * 365}}) + "\n"
        )

        # Half one: nothing has written this file for longer than a cap, so whatever the
        # stored deadline says, the lease it names cannot still be protected.
        stale = now - HOP_LEASE_DEADLINE_CAP_SECS - 3600
        os.utime(path, (stale, stale))
        reg = InstancesRegistry(path=path)
        assert 25001 not in reg.live_hop_leases(), (
            "a deadline a year out is still live a full cap after the last write, so the "
            "clamp slides with the clock and that port is withheld for this process's life"
        )

        # Half two, the control: the same file written just now keeps the lease. Without
        # this, deleting the clamp's lower bound -- or the lease map itself -- would pass.
        os.utime(path, (now, now))
        assert 25001 in InstancesRegistry(path=path).live_hop_leases(), (
            "a lease written this second was dropped, so the bound is discarding leases "
            "whose credential is still valid instead of bounding them"
        )
