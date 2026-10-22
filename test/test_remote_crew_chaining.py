"""Remote Crew chaining — a crew reached by riding another crew's hop.

Covers the three things the arrangement adds, each of which is a decision that
has to be made SERVER-SIDE because the request can originate inside an embedded
pane whose code the hub does not control:

* the data model (``via_instance_id`` / ``via_remote_port``), including that a
  registry file written before chaining existed still loads;
* the forward: a chained crew dials its PARENT's host and the parent's loopback
  port, never its own coordinates, which this gateway has no route to;
* the two guards — the depth cap, checked before anything is dialled, and the
  cycle guard, which can only be checked once the hop is open because only the
  far end can say which gateway it is.

The token path is covered from both ends: the hub asks the parent to mint
(``_mint_through_parent``), and the parent mints for the hub without disturbing
its own stored credential (``mint_embed_token``).

Of the two guards listed above, this file keeps the cycle guard and the depth cap as a parent
applies it when asked to mint; the depth cap checked before anything is dialled, at the add
boundary, is in ``test_remote_crew_chaining_route_admission.py``. It also keeps the chain walks
beside the data model. The rest of the family is split by behaviour:
``test_remote_crew_chaining_subtree_removal.py`` holds the cascade below a parent that is
disconnected or removed; ``test_remote_crew_chaining_route_admission.py`` the add boundary's
depth, width, duplicate, name and parent refusals, the PATCH, the published token lifetime and
the embed-token route; ``test_remote_crew_chaining_credential_destination.py`` the dial target,
the gateway identity and the checks that send a chained credential only to the crew it is built
for; ``test_remote_crew_chaining_lent_hop_lease.py`` the lease and the hold a parent keeps on a
hop it lends; and ``test_remote_crew_chaining_chained_refresh_and_repair.py`` the refresh,
retirement and self-heal of a chained forward. The fakes and builders these files share live in
``remote_crew_chaining_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from remote_crew_chaining_helpers import (
    VIA_ID,
    _FakeMintSession,
    _FakeTunnel,
    _mgr,
    _no_cookie_header,
    _relay_mint,
)

from kiro_crew.instances.registry import (
    Instance,
    InstancesRegistry,
    InvalidInstanceError,
    ancestor_ids,
    descendant_ids,
)
from kiro_crew.testing.wait import finished

# ── the data model ───────────────────────────────────────────────────────


class TestChainFields:
    def test_the_pair_travels_together(self):
        """Either half alone reads as "top level" to every consumer while the
        record plainly means something else, so the half-pair is refused rather
        than silently reinterpreted."""
        with pytest.raises(InvalidInstanceError, match="via_remote_port"):
            Instance(id="c", name="C", ssh_host="c-host", via_instance_id="b").validate()
        with pytest.raises(InvalidInstanceError, match="only a chained instance"):
            Instance(id="c", name="C", ssh_host="c-host", via_remote_port=5476).validate()
        with pytest.raises(InvalidInstanceError, match="only a chained instance"):
            Instance(id="c", name="C", ssh_host="c-host", via_remote_id=VIA_ID).validate()
        # A chained record also needs the id its PARENT knows it by: without that
        # the parent cannot be asked to mint, so the row could never connect.
        with pytest.raises(InvalidInstanceError, match="needs via_remote_id"):
            Instance(
                id="c", name="C", ssh_host="c-host", via_instance_id="b", via_remote_port=5476
            ).validate()
        # All three set is the chained record, and it validates.
        Instance(
            id="c",
            name="C",
            ssh_host="c-host",
            via_instance_id="b",
            via_remote_port=5476,
            via_remote_id=VIA_ID,
        ).validate()
        # None set is every record written before chaining existed.
        Instance(id="b", name="B", ssh_host="b-host").validate()

    def test_a_record_cannot_be_reached_through_itself(self):
        with pytest.raises(InvalidInstanceError, match="through itself"):
            Instance(
                id="c",
                name="C",
                ssh_host="c-host",
                via_instance_id="c",
                via_remote_port=5476,
                via_remote_id=VIA_ID,
            ).validate()

    def test_a_malformed_hop_port_is_refused(self):
        for bad in (0, -1, 70000, True):
            with pytest.raises(InvalidInstanceError):
                Instance(
                    id="c",
                    name="C",
                    ssh_host="c-host",
                    via_instance_id="b",
                    via_remote_port=bad,  # type: ignore[arg-type]
                    via_remote_id=VIA_ID,
                ).validate()

    def test_a_parents_side_id_outside_the_grammar_is_refused(self):
        """It ends up in a request path on the parent, so its shape is checked at
        the boundary rather than trusted because it arrived on a record."""
        with pytest.raises(InvalidInstanceError, match="via_remote_id"):
            Instance(
                id="c",
                name="C",
                ssh_host="c-host",
                via_instance_id="b",
                via_remote_port=5476,
                via_remote_id="victim/disconnect?x=",
            ).validate()

    def test_a_registry_file_written_before_chaining_loads_unchanged(self, tmp_path):
        """The feature must not make an existing instances.json unreadable."""
        path = tmp_path / "instances.json"
        path.write_text(
            json.dumps(
                {
                    "instances": [
                        {"id": "b", "name": "B", "ssh_host": "b-host", "remote_port": 5476}
                    ],
                    "last_active_id": "b",
                }
            )
        )
        loaded = InstancesRegistry(path=path).get("b")
        assert loaded is not None
        assert loaded.via_instance_id == ""
        assert loaded.via_remote_port == 0
        # Including the parent-side id, which a file written before chaining
        # cannot carry: the loader defaults it rather than refusing the record.
        assert loaded.via_remote_id == ""
        # And it is still writable: an update must not fail on a field the
        # caller never touched.
        InstancesRegistry(path=path).update("b", was_connected=True)

    def test_the_pair_round_trips_through_the_stored_shape(self, tmp_path):
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=53999,
            via_remote_id=VIA_ID,
        )
        reloaded = InstancesRegistry(path=tmp_path / "instances.json").get("c")
        assert reloaded is not None
        assert (reloaded.via_instance_id, reloaded.via_remote_port) == ("b", 53999)


class TestChainWalks:
    def _chain(self) -> list[Instance]:
        return [
            Instance(id="b", name="B", ssh_host="b-host"),
            Instance(
                id="c",
                name="C",
                ssh_host="c-host",
                via_instance_id="b",
                via_remote_port=1,
                via_remote_id=VIA_ID,
            ),
            Instance(
                id="d",
                name="D",
                ssh_host="d-host",
                via_instance_id="c",
                via_remote_port=2,
                via_remote_id=VIA_ID,
            ),
        ]

    def test_ancestors_are_nearest_first(self):
        assert ancestor_ids(self._chain(), "d") == ["c", "b"]
        assert ancestor_ids(self._chain(), "b") == []

    def test_an_absent_parent_ends_the_walk(self):
        rows = [
            Instance(
                id="c",
                name="C",
                ssh_host="c-host",
                via_instance_id="gone",
                via_remote_port=1,
                via_remote_id=VIA_ID,
            )
        ]
        assert ancestor_ids(rows, "c") == ["gone"]

    def test_a_looped_registry_terminates(self):
        """A hand edit can write a loop. Both walks must end, because the guards
        that refuse the loop are the CALLERS of these functions.

        The exact chain matters, not just its length: the walk stops AT the id
        that repeats, so the caller sees the loop closing. Without the visit
        guard the bounded range still terminates, but it pads the chain with the
        same two ids over and over and the closure is unreadable."""
        rows = [
            Instance(
                id="a",
                name="A",
                ssh_host="a-host",
                via_instance_id="b",
                via_remote_port=1,
                via_remote_id=VIA_ID,
            ),
            Instance(
                id="b",
                name="B",
                ssh_host="b-host",
                via_instance_id="a",
                via_remote_port=2,
                via_remote_id=VIA_ID,
            ),
        ]
        assert ancestor_ids(rows, "a") == ["b", "a"]
        assert ancestor_ids(rows, "b") == ["a", "b"]
        assert descendant_ids(rows, "a") == ["b"]

    def test_descendants_list_parents_before_their_children(self):
        assert descendant_ids(self._chain(), "b") == ["c", "d"]
        assert descendant_ids(self._chain(), "d") == []


# ── the forward ──────────────────────────────────────────────────────────


class TestChainedForward:
    def test_it_dials_the_parents_host_and_the_hop_port(self, tmp_path, monkeypatch):
        """The crew's own ssh_host/remote_port describe it on ITS machine. This
        gateway has no route to them; the whole point of the chain is that the
        parent does."""
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", remote_port=5476, instance_id="b")
        reg.add(
            name="C",
            ssh_host="c-host",
            remote_port=5476,
            instance_id="c",
            via_instance_id="b",
            via_remote_port=53999,
            via_remote_id=VIA_ID,
        )

        async def no_cycle(_inst, _port):
            return ""

        monkeypatch.setattr(mgr, "_chain_cycle_reason", no_cycle)

        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr))
        st = asyncio.run(mgr.connect("c"))

        assert st.state.value == "connected"
        built = [t for t in _FakeTunnel.made if t.iid == "c"]
        assert len(built) == 1
        assert built[0].ssh_host == "b-host", "dialled the crew instead of its parent"
        assert built[0].remote_port == 53999, "forwarded to the crew's own port, not the hop"

    def test_the_hop_port_comes_from_the_parent_not_from_the_row(self, tmp_path, monkeypatch):
        """The row's copy of the hop port arrived over a pane's postMessage, and it
        is what `ssh -L` aims at on the parent's machine. Unchecked, a forged notice
        chooses which loopback-only service there this gateway forwards and then
        renders as a crew. The parent's own mint reply names the port its forward
        listens on, over the credential we already hold for it."""
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", remote_port=5476, instance_id="b")
        reg.add(
            name="C",
            ssh_host="c-host",
            remote_port=5476,
            instance_id="c",
            via_instance_id="b",
            # What a forged notice put on the row: the parent's own loopback
            # postgres, say, rather than the port its forward to C listens on.
            via_remote_port=5432,
            via_remote_id=VIA_ID,
        )

        async def no_cycle(_inst, _port):
            return ""

        monkeypatch.setattr(mgr, "_chain_cycle_reason", no_cycle)
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr, port=53999))
        st = asyncio.run(mgr.connect("c"))

        assert st.state.value == "connected"
        built = [t for t in _FakeTunnel.made if t.iid == "c"]
        assert len(built) == 1
        assert built[0].remote_port == 53999, (
            f"dialled {built[0].remote_port}, the value on the row, so a forged notice "
            f"still picks the service"
        )
        assert 5432 not in [t.remote_port for t in built], "dialled the pane's port at all"
        # Persisted, so a reconnect and the rebuild path dial the same port.
        assert reg.get("c").via_remote_port == 53999

    def test_a_parent_that_names_no_hop_port_is_refused_before_dialling(
        self, tmp_path, monkeypatch
    ):
        """Falling back to the row's value here is the whole hazard, so there is no
        fallback. Every build that answers this endpoint at all reports the port."""
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=5432,
            via_remote_id=VIA_ID,
        )

        async def silent_mint(_inst, _params):
            from kiro_crew.instances.ssh_tunnel_manager import _Mint

            return _Mint(token="CHAINED_TOKEN", hop=0, ttl="20h")  # reports no port

        monkeypatch.setattr(mgr, "_mint_through_parent", silent_mint)
        before = len(_FakeTunnel.made)
        st = asyncio.run(mgr.connect("c"))

        assert st.state.value == "error"
        assert "port" in (st.error or ""), st.error
        assert len(_FakeTunnel.made) == before, "opened a forward before the parent named the port"

    def test_a_missing_parent_is_an_error_status_naming_it(self, tmp_path, monkeypatch):
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="gone",
            via_remote_port=53999,
            via_remote_id=VIA_ID,
        )
        st = asyncio.run(mgr.connect("c"))
        assert st.state.value == "error"
        assert "gone" in st.error
        assert not _FakeTunnel.made, "opened a forward with no hop to ride"

    def test_an_ssm_parent_is_refused(self, tmp_path, monkeypatch):
        """ssm as the parent hop is a follow-up: its forwarder takes no second
        local forward from this gateway."""
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(
            name="B",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            instance_id="b",
        )
        reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=53999,
            via_remote_id=VIA_ID,
        )
        st = asyncio.run(mgr.connect("c"))
        assert st.state.value == "error"
        assert "ssh hop" in st.error
        assert not _FakeTunnel.made

    def test_the_orphan_reclaim_argv_uses_the_hop_port(self, tmp_path, monkeypatch):
        """The reclaim compares the argv it EXPECTS against the leaked child's
        real one. Built from the crew's own port it could never match, and a
        mismatch reads as "not our child" — the forwarder would keep the port."""
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        inst = reg.add(
            name="C",
            ssh_host="c-host",
            remote_port=5476,
            instance_id="c",
            via_instance_id="b",
            via_remote_port=53999,
            via_remote_id=VIA_ID,
        )
        params = mgr._resolve_chained_transport(inst, reg.get("b"))
        assert params.forward_remote_port(inst.remote_port) == 53999
        assert params.ssh_host == "b-host"

    def test_a_chained_crew_cannot_be_restarted_from_here(self, tmp_path, monkeypatch):
        """`kirocrew restart` would be dispatched at the PARENT's shell, which
        restarts the wrong machine."""
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=53999,
            via_remote_id=VIA_ID,
        )
        out = asyncio.run(mgr.restart_remote("c"))
        assert out["ok"] is False
        assert "cannot run commands" in out["message"]


# ── the token ────────────────────────────────────────────────────────────


class TestChainedToken:
    def test_a_chained_mint_goes_through_the_parent_not_over_ssh(self, tmp_path, monkeypatch):
        """This gateway holds no key for a chained crew, so an ssh mint aimed at
        it cannot work — and aimed at the parent it would mint the PARENT's
        token, which the crew's own CSP would reject as a frame ancestor."""
        ssh_mints: list[str] = []

        async def recording_mint(host, **_kw):
            ssh_mints.append(host)
            return "SSH_TOKEN"

        reg, mgr = _mgr(tmp_path, monkeypatch, mint=recording_mint)
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        inst = reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=53999,
            via_remote_id=VIA_ID,
        )
        relayed: list[str] = []

        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr, seen=relayed))
        params = mgr._resolve_chained_transport(inst, reg.get("b"))
        mint = asyncio.run(mgr._mint_for(inst, params))

        assert mint.token == "CHAINED_TOKEN"
        assert mint.hop == 53999, "the dispatcher dropped the hop the parent named"
        assert relayed == ["c"]
        assert ssh_mints == [], "dispatched an ssh mint for a crew it has no key for"

    def test_the_parent_mints_with_the_hubs_port_and_keeps_its_own_token(
        self, tmp_path, monkeypatch
    ):
        """The token the parent hands back is scoped to the HUB's page. Storing it
        would replace the parent's own credential for that crew and break the
        parent's pane for it."""
        seen_ports: list[int | None] = []

        async def recording_mint(host, *, embed_parent_port=None, **_kw):
            seen_ports.append(embed_parent_port)
            return f"TOKEN_epp{embed_parent_port}"

        reg, mgr = _mgr(tmp_path, monkeypatch, mint=recording_mint)
        reg.add(name="C", ssh_host="c-host", instance_id="c")
        asyncio.run(mgr.connect("c"))
        own = mgr.get_token("c")
        assert own == "TOKEN_epp4242", "our own mint did not carry our parent port"

        ok, payload = asyncio.run(mgr.mint_embed_token("c", 9191))
        assert ok is True
        assert payload["token"] == "TOKEN_epp9191"
        assert payload["port"] == mgr.status("c").local_port, "named a hop it did not verify"
        assert seen_ports == [4242, 9191]
        assert mgr.get_token("c") == own, "overwrote our own credential for the crew"

    def test_the_parents_reply_carries_what_the_hubs_parser_requires(self, tmp_path, monkeypatch):
        """The two ends of one wire, pinned against each other. Each side had its own
        tests and neither could see a field the OTHER side stopped sending: the parent
        builds a payload, the hub parses one, and nothing compared them. So this drives
        the parent for real and then feeds its own payload to the hub's parser.
        """
        from kiro_crew.instances import ssh_tunnel_manager as stm

        # The parent end: mint for a crew IT holds, for a hub on port 9191.
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="C", ssh_host="c-host", instance_id="c")
        asyncio.run(mgr.connect("c"))
        ok, payload = asyncio.run(mgr.mint_embed_token("c", 9191))
        assert ok is True

        assert payload["hop_id"] == "c", "the parent did not name which crew the hop reaches"
        assert payload["hop_gen"] == mgr._tunnel_epoch["c"], "the parent named a stale generation"

        # The hub end: a DIFFERENT gateway parsing exactly that payload.
        hub_reg, hub = _mgr(tmp_path / "hub", monkeypatch)
        hub_reg.add(name="B", ssh_host="b-host", instance_id="b")
        child = hub_reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=payload["port"],
            via_remote_id="c",
        )
        # Set through monkeypatch: this is a CLASS attribute other tests in this file
        # rely on the default of, so mutating it directly leaks into them.
        monkeypatch.setattr(_FakeMintSession, "reply", dict(payload))
        monkeypatch.setattr(stm.aiohttp, "ClientSession", _FakeMintSession)
        monkeypatch.setattr(
            hub, "_peer_target", lambda _pid, path: (f"http://127.0.0.1:1{path}", "kc")
        )
        monkeypatch.setattr(hub, "_peer_cookie_header", _no_cookie_header)

        params = hub._resolve_chained_transport(child, hub_reg.get("b"))
        mint = asyncio.run(hub._mint_through_parent(child, params))

        assert mint.hop_id == payload["hop_id"]
        assert mint.hop_gen == payload["hop_gen"]
        assert mint.hop == payload["port"]
        assert mint.ttl == payload["ttl"]
        assert "crew_gateway_id" in payload, "the parent did not say which gateway the hop reaches"
        assert mint.crew_gateway_id == payload["crew_gateway_id"]

    def test_the_parent_refuses_to_mint_for_a_crew_it_reaches_through_a_hop(
        self, tmp_path, monkeypatch
    ):
        """The depth cap seen from the far end, and the ONLY place it can be seen:
        the asking hub counts hops in its own registry and cannot know that ours
        adds another one."""
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=53999,
            via_remote_id=VIA_ID,
        )
        ok, payload = asyncio.run(mgr.mint_embed_token("c", 9191))
        assert ok is False
        assert payload["code"] == "chain_too_deep"
        assert payload["status"] == 400

    def test_the_parent_refuses_to_mint_for_a_crew_that_is_not_connected(
        self, tmp_path, monkeypatch
    ):
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="C", ssh_host="c-host", instance_id="c")
        ok, payload = asyncio.run(mgr.mint_embed_token("c", 9191))
        assert ok is False
        assert payload["code"] == "instance_not_connected"

    def test_a_hop_freed_during_the_mint_is_refused_not_handed_to_its_new_owner(
        self, tmp_path, monkeypatch
    ):
        """The token and the port are one claim. `status()` hands out the tunnel's
        LIVE status object and a teardown pops that tunnel without zeroing the port
        on it, while the allocator gives a just-freed port to the next connect
        first -- so a crew disconnected inside the mint's round trip, plus any crew
        connected before it returns, would pair C's token with D's forward and the
        asking hub would forward to whatever now answers there.
        """
        holder: list = []

        async def mint_that_loses_the_hop(host, *, embed_parent_port=None, **_kw):
            mgr_ = holder[0]
            # Only the EMBED mint, which runs with no manager lock held. The
            # connect-time mint (embed_parent_port 4242) runs inside `connect`'s
            # own critical section, so re-entering the manager there would park
            # this test on the lock rather than exercise anything.
            if host == "c-host" and embed_parent_port == 9191:
                await mgr_.disconnect("c")
                await mgr_.connect("d")
            return f"TOKEN_FOR_{host}"

        reg, mgr = _mgr(tmp_path, monkeypatch, mint=mint_that_loses_the_hop)
        holder.append(mgr)
        reg.add(name="C", ssh_host="c-host", instance_id="c")
        reg.add(name="D", ssh_host="d-host", instance_id="d")
        assert asyncio.run(mgr.connect("c")).state.value == "connected"
        hop = mgr.status("c").local_port
        assert hop > 0

        ok, payload = asyncio.run(mgr.mint_embed_token("c", 9191))

        assert ok is False, "answered with a token paired to a hop it no longer owns"
        assert payload["code"] == "instance_hop_changed"
        assert payload["status"] == 409
        # The port a stale read would have named belongs to the other crew now,
        # which is what makes this a credential crossing rather than a dead port.
        assert mgr.status("d").local_port == hop

    def test_a_reconnect_during_the_mint_is_refused_although_the_crew_is_live_again(
        self, tmp_path, monkeypatch
    ):
        """Membership cannot see this: the crew is back in `_tunnels` and CONNECTED
        by the time the mint returns. Only the generation stamp says the forward the
        token was minted against is not the forward we would name.
        """
        holder: list = []

        async def mint_that_reconnects(host, *, embed_parent_port=None, **_kw):
            mgr_ = holder[0]
            if host == "c-host" and embed_parent_port == 9191:
                await mgr_.disconnect("c")
                await mgr_.connect("c")
            return f"TOKEN_FOR_{host}"

        reg, mgr = _mgr(tmp_path, monkeypatch, mint=mint_that_reconnects)
        holder.append(mgr)
        reg.add(name="C", ssh_host="c-host", instance_id="c")
        assert asyncio.run(mgr.connect("c")).state.value == "connected"

        ok, payload = asyncio.run(mgr.mint_embed_token("c", 9191))

        assert ok is False, "a reconnect satisfied membership and the mint was answered"
        assert payload["code"] == "instance_hop_changed"
        assert mgr.status("c").state.value == "connected", "the crew really is live again"

    def test_a_forward_that_died_during_the_mint_is_refused(self, tmp_path, monkeypatch):
        """The third reading, and the one a teardown never reaches: a probe marks the
        live status ERROR without popping the tunnel or moving the generation, so
        membership and the stamp both still agree. The hop is dead all the same, and
        answering would hand the asking hub a token plus a port nothing listens on.
        """
        holder: list = []

        async def mint_that_loses_the_forward(host, *, embed_parent_port=None, **_kw):
            mgr_ = holder[0]
            if host == "c-host" and embed_parent_port == 9191:
                from kiro_crew.instances.ssh_tunnel_manager import TunnelState

                mgr_._tunnels["c"].status.state = TunnelState.ERROR
            return f"TOKEN_FOR_{host}"

        reg, mgr = _mgr(tmp_path, monkeypatch, mint=mint_that_loses_the_forward)
        holder.append(mgr)
        reg.add(name="C", ssh_host="c-host", instance_id="c")
        assert asyncio.run(mgr.connect("c")).state.value == "connected"
        epoch_before = mgr._tunnel_epoch.get("c", 0)

        ok, payload = asyncio.run(mgr.mint_embed_token("c", 9191))

        assert ok is False, "answered with a token for a forward that had died"
        assert payload["code"] == "instance_hop_changed"
        assert "c" in mgr._tunnels, "the tunnel was never popped, so membership still agrees"
        assert mgr._tunnel_epoch.get("c", 0) == epoch_before, "the generation never moved"

    def test_an_unknown_crew_is_a_404(self, tmp_path, monkeypatch):
        _reg, mgr = _mgr(tmp_path, monkeypatch)
        ok, payload = asyncio.run(mgr.mint_embed_token("nope", 9191))
        assert ok is False
        assert payload["status"] == 404

    # ── the cycle guard ──────────────────────────────────────────────────────

    def test_the_mint_asks_the_parent_by_the_parents_own_id_not_ours(self, tmp_path, monkeypatch):
        """The two ids genuinely DIFFER here, which is the case a harness using
        matching names cannot see: our row is `c` (derived from the name) while the
        parent knows the crew as `c-2`. The parent looks it up by its own id, so
        asking with ours makes it answer 404 for a crew it holds.

        Also the TTL: the parent issues the token under ITS record for the crew, so
        a refresh scheduled from our row's 20h default would run after a shorter
        token has already expired.
        """
        from kiro_crew.instances import ssh_tunnel_manager as stm

        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        inst = reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=53999,
            via_remote_id="c-2",
        )
        assert inst.id == "c" and inst.via_remote_id == "c-2", "the ids must differ here"

        _FakeMintSession.posted = []
        monkeypatch.setattr(stm.aiohttp, "ClientSession", _FakeMintSession)
        monkeypatch.setattr(
            mgr, "_peer_target", lambda _pid, path: (f"http://127.0.0.1:1{path}", "kc")
        )
        monkeypatch.setattr(mgr, "_peer_cookie_header", _no_cookie_header)

        params = mgr._resolve_chained_transport(inst, reg.get("b"))
        mint = asyncio.run(mgr._mint_through_parent(inst, params))

        assert mint.token == "CHILD_TOKEN"
        assert len(_FakeMintSession.posted) == 1
        assert "/api/instances/c-2/embed-token" in _FakeMintSession.posted[0]
        assert "/api/instances/c/embed-token" not in _FakeMintSession.posted[0]
        # REPORTED, not published: the parent's TTL comes back in the reply and is
        # written only by the fenced store, so nothing is in the dict yet.
        assert mint.ttl == "2h", "dropped the TTL the parent issued"
        assert "c" not in mgr._chained_ttl, "published the TTL ahead of the caller's fence"
        mgr._tunnels["c"] = _FakeTunnel("c", "b-host", 53700, 5432)
        mgr._tunnel_epoch["c"] = 1
        # The store below is a port MISMATCH with `binds_forward` false, which is the
        # case that now asks for the stale forward to be retired. That is deliberate and
        # is asserted in `test_the_store_is_the_one_place_a_moved_hop_is_refused`; here it
        # is incidental, and the real `_schedule_chained_retirement` would need a running
        # loop for its task. Spied so this case keeps testing the ids and the TTL.
        retired: list[str] = []
        monkeypatch.setattr(
            mgr, "_schedule_chained_retirement", lambda _id, reason: retired.append(reason)
        )
        assert (
            mgr._store_token(inst, mint, minted_at_epoch=1, binds_forward=False) is False
        ), "stored against a forward dialling 5432 while the parent named 4242"
        assert retired, "a moved port no longer retires -- see the store test"
        assert mgr._chained_ttl["c"] == "2h", "the fenced store did not publish the TTL"
        assert mgr._minted_ttl(inst) == "2h"

    def test_the_real_mint_records_the_hop_port_the_parent_reports(self, tmp_path, monkeypatch):
        """Read at THIS level because every connect test doubles the mint, so the
        reply-parsing itself is only reachable here -- a mutation removing it stays
        green against those tests."""
        from kiro_crew.instances import ssh_tunnel_manager as stm

        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        inst = reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=5432,
            via_remote_id="c-2",
        )

        _FakeMintSession.posted = []
        _FakeMintSession.reply = {
            "token": "CHILD_TOKEN",
            "port": 4242,
            "ttl": "2h",
            "hop_id": "c-2",
            "hop_gen": 3,
        }
        monkeypatch.setattr(stm.aiohttp, "ClientSession", _FakeMintSession)
        monkeypatch.setattr(
            mgr, "_peer_target", lambda _pid, path: (f"http://127.0.0.1:1{path}", "kc")
        )
        monkeypatch.setattr(mgr, "_peer_cookie_header", _no_cookie_header)

        params = mgr._resolve_chained_transport(inst, reg.get("b"))
        mint = asyncio.run(mgr._mint_through_parent(inst, params))
        assert mint.hop == 4242, "took the row's port over the parent's"
        assert "c" not in mgr._chained_hop_port, "published the hop ahead of the caller's fence"

    @pytest.mark.parametrize("bad", [None, 0, 65536, -1, True, "53999", 1.5])
    def test_an_unusable_reported_port_is_dropped_not_adopted(self, tmp_path, monkeypatch, bad):
        """Dropped, so the caller refuses the dial. Adopting a malformed value would
        put it straight into an `ssh -L` target, and `True` is in the list because
        `isinstance(True, int)` is True and it would otherwise read as port 1."""
        from kiro_crew.instances import ssh_tunnel_manager as stm

        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        inst = reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=53999,
            via_remote_id="c-2",
        )

        reply = {"token": "CHILD_TOKEN", "ttl": "2h", "hop_id": "c-2", "hop_gen": 3}
        if bad is not None:
            reply["port"] = bad
        _FakeMintSession.posted = []
        _FakeMintSession.reply = reply
        monkeypatch.setattr(stm.aiohttp, "ClientSession", _FakeMintSession)
        monkeypatch.setattr(
            mgr, "_peer_target", lambda _pid, path: (f"http://127.0.0.1:1{path}", "kc")
        )
        monkeypatch.setattr(mgr, "_peer_cookie_header", _no_cookie_header)

        params = mgr._resolve_chained_transport(inst, reg.get("b"))
        try:
            asyncio.run(mgr._mint_through_parent(inst, params))
        finally:
            _FakeMintSession.reply = {
                "token": "CHILD_TOKEN",
                "port": 4242,
                "ttl": "2h",
                "hop_id": "c-2",
                "hop_gen": 3,
            }
        assert "c" not in mgr._chained_hop_port, f"adopted {bad!r} as a hop port"

    @pytest.mark.parametrize("bad", ["not-a-ttl", "", None, 7200, True, "20"])
    def test_a_mint_reply_that_cannot_state_the_lifetime_is_refused(
        self, tmp_path, monkeypatch, bad
    ):
        """The parent issued this token, so it is the only party that knows when it
        dies. Our row's TTL is a separate number with its own default and nothing
        holds it below the parent's, so falling back to it can schedule the refresh
        AFTER the token is already dead. Refused instead, the same as a reply we
        cannot read -- every gateway that answers this endpoint reports its TTL.
        """
        from kiro_crew.instances import ssh_tunnel_manager as stm
        from kiro_crew.instances.ssh_tunnel_manager import TokenMintError

        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        inst = reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            ttl="9h",
            via_instance_id="b",
            via_remote_port=53999,
            via_remote_id="c-2",
        )

        reply = {"token": "T", "port": 1}
        if bad is not None:
            reply["ttl"] = bad
        _FakeMintSession.posted = []
        _FakeMintSession.reply = reply
        try:
            monkeypatch.setattr(stm.aiohttp, "ClientSession", _FakeMintSession)
            monkeypatch.setattr(
                mgr, "_peer_target", lambda _pid, path: (f"http://127.0.0.1:1{path}", "kc")
            )
            monkeypatch.setattr(mgr, "_peer_cookie_header", _no_cookie_header)
            params = mgr._resolve_chained_transport(inst, reg.get("b"))
            with pytest.raises(TokenMintError):
                asyncio.run(mgr._mint_through_parent(inst, params))
        finally:
            _FakeMintSession.reply = {
                "token": "CHILD_TOKEN",
                "port": 4242,
                "ttl": "2h",
                "hop_id": "c-2",
                "hop_gen": 3,
            }

        assert "c" not in mgr._chained_ttl, f"recorded {bad!r} as a token lifetime"

    def test_the_stored_lifetime_is_the_shorter_of_the_two(self, tmp_path, monkeypatch):
        """Both directions, because the safe one is not symmetric. Shorter than the
        token's real life costs an early re-mint; longer schedules the refresh after
        it is dead. Taking the minimum makes that hold however the two are
        configured, rather than assuming the parent's is always the smaller.
        """
        reg, _mgr_unused = _mgr(tmp_path, monkeypatch)
        _reg2, mgr = _mgr(tmp_path / "second", monkeypatch)
        inst = reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            ttl="9h",
            via_instance_id="b",
            via_remote_port=53999,
            via_remote_id="c-2",
        )

        mgr._chained_ttl["c"] = "2h"
        assert mgr._minted_ttl(inst) == "2h", "kept our longer TTL over the parent's shorter one"
        mgr._chained_ttl["c"] = "40h"
        assert mgr._minted_ttl(inst) == "9h", "took a TTL longer than ours, scheduling late"

        top = reg.add(name="D", ssh_host="d-host", instance_id="d", ttl="5h")
        mgr._chained_ttl["d"] = "1h"
        assert mgr._minted_ttl(top) == "5h", "a top-level token is ours, not a parent's"

    def test_a_stored_id_cannot_inject_a_parent_control_plane_path(self, tmp_path, monkeypatch):
        """`Instance.from_dict` is deliberately tolerant, so a registry file written
        by hand or by an agent can carry any id string. Unchecked, an id like
        `victim/disconnect?x=` interpolates into a DIFFERENT authenticated route on
        the parent and spends our credential for it there."""
        from kiro_crew.instances.registry import Instance
        from kiro_crew.instances.ssh_tunnel_manager import TokenMintError

        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        valid = reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=53999,
            via_remote_id=VIA_ID,
        )
        params = mgr._resolve_chained_transport(valid, reg.get("b"))

        hostile = Instance.from_dict(
            {
                "id": "c",
                "name": "C",
                "ssh_host": "c-host",
                "via_instance_id": "b",
                "via_remote_port": 53999,
                "via_remote_id": "victim/disconnect?x=",
            }
        )
        assert (
            hostile.via_remote_id == "victim/disconnect?x="
        ), "from_dict rejected it, so the risk is elsewhere"

        dialled: list[str] = []

        def record_target(*_a, **_k):
            dialled.append("built a request")
            return "http://127.0.0.1:1/x", "cookie"

        monkeypatch.setattr(mgr, "_peer_target", record_target)
        with pytest.raises(TokenMintError):
            asyncio.run(mgr._mint_through_parent(hostile, params))
        assert dialled == [], "built a parent request for an id outside the grammar"

    def test_a_parent_remint_completes_with_the_lock_already_held(self, tmp_path, monkeypatch):
        """The chained mint runs inside `connect`'s lock hold, so the re-mint it
        reaches for on a rejected parent credential has to work THERE. Bounded so a
        re-entrant acquire fails the test by name instead of hanging it."""
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", instance_id="b")

        async def scenario():
            await mgr.connect("b")
            async with mgr._lock:
                return await finished(
                    mgr._remint_parent_under_lock("b"), "the re-mint under the held lock"
                )

        assert asyncio.run(scenario()) is True
        assert mgr.get_token("b"), "re-minted nothing for the parent"

    def test_the_rejected_credential_path_never_re_enters_the_manager_lock(self):
        """`asyncio.Lock` is not reentrant and the holder here is the same task, so
        a 401 reaching the lock-taking public refresh would hang the connect while
        it still holds the lock, wedging every later connect and disconnect."""
        import ast
        import inspect
        import textwrap

        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        def calls_in(fn):
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            return {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}

        relay_calls = calls_in(SshTunnelManager._mint_through_parent)
        assert "self.refresh_token" not in relay_calls, "re-enters the lock via the public refresh"
        assert "self._remint_parent_under_lock" in relay_calls

        helper = textwrap.dedent(inspect.getsource(SshTunnelManager._remint_parent_under_lock))
        acquired = [
            ast.unparse(item.context_expr)
            for node in ast.walk(ast.parse(helper))
            if isinstance(node, ast.AsyncWith)
            for item in node.items
        ]
        assert (
            "self._lock" not in acquired
        ), "the caller already holds it; taking it again deadlocks"


class TestCycleGuard:
    def _chained(self, tmp_path, monkeypatch):
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=53999,
            via_remote_id=VIA_ID,
        )

        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr))
        return reg, mgr

    def test_a_hop_that_lands_back_on_us_is_refused_and_torn_down(self, tmp_path, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as stm

        reg, mgr = self._chained(tmp_path, monkeypatch)
        monkeypatch.setattr(stm, "gateway_id", lambda *_a, **_k: "OURS")

        async def far_end_is_us(_port):
            return "OURS"

        monkeypatch.setattr(mgr, "_peer_gateway_id", far_end_is_us)
        st = asyncio.run(mgr.connect("c"))

        assert st.state.value == "error"
        assert "loop" in st.error
        built = [t for t in _FakeTunnel.made if t.iid == "c"]
        assert built and built[0].stopped, "left the looping forward open"
        assert mgr.status("c") is None
        assert reg is not None

    def test_a_hop_that_lands_on_an_ancestor_is_refused(self, tmp_path, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as stm

        _reg, mgr = self._chained(tmp_path, monkeypatch)
        monkeypatch.setattr(stm, "gateway_id", lambda *_a, **_k: "OURS")
        # Bring the parent up so the guard has an ancestor tunnel to read.
        asyncio.run(mgr.connect("b"))
        parent_port = mgr.status("b").local_port

        async def ports(port):
            # The parent and the far end of the new hop are the same gateway.
            return "PARENT" if port in (parent_port, mgr.status("c").local_port) else "OTHER"

        monkeypatch.setattr(mgr, "_peer_gateway_id", ports)
        st = asyncio.run(mgr.connect("c"))

        assert st.state.value == "error"
        assert "'b'" in st.error and "loop" in st.error

    def test_a_crew_that_reports_no_id_is_allowed(self, tmp_path, monkeypatch):
        """Fail-open on a crew older than the field: the loop it cannot rule out
        is a nested pane, not an escape from a boundary, and the depth cap
        already bounds the arrangement."""
        _reg, mgr = self._chained(tmp_path, monkeypatch)

        async def silent(_port):
            return ""

        monkeypatch.setattr(mgr, "_peer_gateway_id", silent)
        st = asyncio.run(mgr.connect("c"))
        assert st.state.value == "connected"

    def test_a_top_level_crew_is_not_cycle_checked(self, tmp_path, monkeypatch):
        """A top-level crew pointing back at this gateway is what the product
        already allows. Refusing it here would break a working setup over an
        arrangement chaining does not introduce."""
        from kiro_crew.instances import ssh_tunnel_manager as stm

        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="Self", ssh_host="localhost", instance_id="self")
        monkeypatch.setattr(stm, "gateway_id", lambda *_a, **_k: "OURS")
        probed: list[int] = []

        async def far_end_is_us(port):
            probed.append(port)
            return "OURS"

        monkeypatch.setattr(mgr, "_peer_gateway_id", far_end_is_us)
        st = asyncio.run(mgr.connect("self"))
        assert st.state.value == "connected"
        assert probed == [], "probed a crew that rides no hop"
