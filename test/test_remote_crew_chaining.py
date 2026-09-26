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
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import pathlib
import time
from pathlib import Path

import pytest

from kiro_crew.instances.registry import (
    INSTANCE_NAME_MAX,
    MAX_CHAINED_PER_PARENT,
    MAX_VIA_HOPS,
    Instance,
    InstancesRegistry,
    InvalidInstanceError,
    ancestor_ids,
    descendant_ids,
)
from kiro_crew.instances.ssh_tunnel_manager import _Mint
from kiro_crew.subprocess_utf8 import UTF8_TEXT

# ── helpers ──────────────────────────────────────────────────────────────

# Stands in for "this crew's id on its PARENT" in fixtures that do not care what
# it is. The one test that does care asserts on its own genuinely divergent pair,
# because ids that coincide on both sides hide a mint aimed at the wrong one.
VIA_ID = "peer-id"


class _FakeTunnel:
    """Minimal stand-in for ``_SshTunnel``, recording what it was asked to forward."""

    made: list[_FakeTunnel] = []

    def __init__(
        self,
        iid,
        ssh_host,
        lp,
        rp,
        *,
        connect_timeout_secs=0,
        compression=True,
        probe_failure_threshold=0,
        on_exit=None,
        transport="ssh",
        ssm_target="",
        aws_profile="",
        aws_region="",
    ):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        self.iid = iid
        self.ssh_host = ssh_host
        self.local_port = lp
        self.remote_port = rp
        self.transport = transport
        self.pid = None
        self.stopped = False
        self.start_result = True
        self._S = TunnelState
        self.status = TunnelStatus(instance_id=iid, local_port=lp, remote_port=rp)
        _FakeTunnel.made.append(self)

    async def start(self):
        self.status.state = self._S.CONNECTED if self.start_result else self._S.ERROR
        if not self.start_result:
            self.status.error = "boom"
        return self.start_result

    async def stop(self):
        self.stopped = True
        self.status.state = self._S.STOPPED


def _free_ports(monkeypatch):
    """Make both loopback port probes answer "free", so these stay hermetic."""
    from kiro_crew.instances import port_allocator
    from kiro_crew.instances import ssh_tunnel_manager as stm

    monkeypatch.setattr(port_allocator, "_is_port_free", lambda *_a, **_k: True)
    monkeypatch.setattr(stm, "_is_port_free", lambda *_a, **_k: True)


def _mgr(tmp_path, monkeypatch, *, mint=None):
    """A manager over a fresh registry, with a fake forwarder and a fake mint."""
    _free_ports(monkeypatch)
    from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

    _FakeTunnel.made = []
    reg = InstancesRegistry(path=tmp_path / "instances.json")

    async def ok_mint(
        host,
        *,
        remote_bin="",
        ttl="20h",
        remote_port=None,
        embed_parent_port=None,
        timeout_secs=None,
    ):
        return f"SSH_TOKEN_FOR_{host}_epp{embed_parent_port}"

    mgr = SshTunnelManager(
        reg,
        base_port=53700,
        mint_token=mint or ok_mint,
        tunnel_factory=_FakeTunnel,
        parent_port=4242,
    )
    # These cases run a FAKE forwarder on a fixed fake base port, so nothing here ever
    # really binds one. A real HopPortGuard would: every teardown of a lent hop would
    # take OS ownership of that fake port and keep it, because only `shutdown` releases
    # it -- so the sockets would accumulate across cases and the next manager would
    # collide with a hold left by an earlier one on the same fixed port.
    #
    # Inert here, therefore, and exercised for real in exactly two places, neither of
    # which is skipped: `test_hop_port_guard.py` pins the mechanism against a separate
    # process, and `test_a_torn_down_lent_hop_is_owned_not_merely_skipped` swaps a real
    # guard back in to pin that teardown actually arms it.
    mgr._hop_guard = _InertGuard()
    return reg, mgr


class _InertGuard:
    """Records what a real guard would have been asked to own, and binds nothing."""

    def __init__(self) -> None:
        self.synced: list[tuple[dict[int, float], set[int]]] = []

    def sync(self, leases, in_use):
        self.synced.append((dict(leases), set(in_use)))
        return set()

    def hold(self, port, until):
        return True

    def release(self, port):
        return None

    def held_ports(self):
        return set()

    def close_all(self):
        return None


class _State:
    owner_id = "owner"

    def __init__(self, registry, manager=None):
        self.instances_registry = registry
        self.instances_manager = manager


class _FakeReq:
    def __init__(self, state, *, match=None, body=None, query=None, user="owner"):
        self.app = {"state": state}
        self.headers = {}
        self.match_info = match or {}
        self.query = query or {}
        self._body = body
        self._attrs = {"app": ""}
        if user is not None:
            self._attrs["user"] = user

    def get(self, key, default=None):
        return self._attrs.get(key, default)

    def __contains__(self, key):
        return key in self._attrs

    def __getitem__(self, key):
        return self._attrs[key]

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _enable(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"instances": {"enabled": True}}))
    from kiro_crew.config import loader

    loader._invalidate_config_cache()


def _resp_body(resp):
    return json.loads(resp.body.decode())


def _relay_mint(
    mgr, *, token="CHAINED_TOKEN", port=None, seen=None, ttl=None, hop_id=None, hop_gen=1
):
    """A stand-in for the chained mint that also reports the hop port.

    The real method reads `port` and `ttl` out of the parent's authenticated reply
    and returns all three together, publishing nothing: the hop it names is the
    very value the credential check compares against, so only the caller's fenced
    store may write it. A double that returns a bare token therefore describes a
    DIFFERENT contract, not a simpler one.

    Defaults to the row's port, which is what a parent in agreement with the row
    would answer; pass *port* to make it disagree.
    """

    async def fake(inst, _params):
        if seen is not None:
            seen.append(inst.id)
        from kiro_crew.instances.ssh_tunnel_manager import _Mint

        return _Mint(
            token=token,
            hop=inst.via_remote_port if port is None else port,
            ttl=inst.ttl if ttl is None else ttl,
            hop_id=inst.via_remote_id if hop_id is None else hop_id,
            hop_gen=hop_gen,
        )

    return fake


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
        monkeypatch.setattr(hub, "_peer_cookie_header", lambda _pid, _name: {})

        params = hub._resolve_chained_transport(child, hub_reg.get("b"))
        mint = asyncio.run(hub._mint_through_parent(child, params))

        assert mint.hop_id == payload["hop_id"]
        assert mint.hop_gen == payload["hop_gen"]
        assert mint.hop == payload["port"]
        assert mint.ttl == payload["ttl"]

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
        monkeypatch.setattr(mgr, "_peer_cookie_header", lambda _pid, _name: {})

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
        assert (
            mgr._store_token(inst, mint, minted_at_epoch=1, binds_forward=False) is False
        ), "stored against a forward dialling 5432 while the parent named 4242"
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
        monkeypatch.setattr(mgr, "_peer_cookie_header", lambda _pid, _name: {})

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
        monkeypatch.setattr(mgr, "_peer_cookie_header", lambda _pid, _name: {})

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
            monkeypatch.setattr(mgr, "_peer_cookie_header", lambda _pid, _name: {})
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
        reaches for on a rejected parent credential has to work THERE. Bounded by
        `wait_for` so a re-entrant acquire fails the test instead of hanging it."""
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", instance_id="b")

        async def scenario():
            await mgr.connect("b")
            async with mgr._lock:
                return await asyncio.wait_for(mgr._remint_parent_under_lock("b"), timeout=5)

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


# ── the cascade ──────────────────────────────────────────────────────────


class _FakeMintReader:
    def __init__(self, raw: bytes):
        self._raw = raw

    async def read(self, _n: int) -> bytes:
        return self._raw


class _FakeMintResp:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self.content = _FakeMintReader(json.dumps(payload).encode("utf-8"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeMintSession:
    """Records the URL each chained mint is aimed at and answers with a canned reply.

    The URL is what the assertion is really about: the path carries the crew's id
    as the PARENT knows it, and a fake that forgets the URL cannot tell the right
    id from the wrong one.
    """

    posted: list[str] = []
    reply: dict = {"token": "CHILD_TOKEN", "port": 4242, "ttl": "2h", "hop_id": "c-2", "hop_gen": 3}
    reply_status: int = 200

    def __init__(self, *_a, **_kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def post(self, url, **_kw):
        # Read off `type(self)`, so a subclass can stage a refusal. Naming the base
        # class here meant every subclass still answered the base's 200 reply, which
        # is why no test could reach the non-2xx branches at all.
        cls = type(self)
        _FakeMintSession.posted.append(url)
        return _FakeMintResp(cls.reply_status, cls.reply)


class _FakeMgr:
    """Records every disconnect, and what the registry still held when it landed.

    The rows are what ``disconnect`` reads to find a crew's children, so a sweep
    that runs after they are gone can only reach the ids its caller captured
    beforehand. Recording the row count alongside each call is what lets a test
    assert that, rather than assert a call order that would also hold by luck.
    """

    def __init__(self, registry=None):
        self._registry = registry
        self.disconnected: list[str] = []
        self.rows_at_disconnect: list[int] = []

    async def disconnect(self, instance_id, **_kw):
        self.disconnected.append(instance_id)
        self.rows_at_disconnect.append(len(self._registry.list()) if self._registry else -1)
        return True

    def status(self, _instance_id):
        # Nothing is connected in these tests; the add handler renders an instance
        # view, and a view asks the manager what each row's tunnel is doing.
        return None


class TestChainCascade:
    def test_disconnecting_a_parent_closes_its_childrens_forwards(self, tmp_path, monkeypatch):
        """A child's forward targets a port on a machine this gateway can no
        longer reach once the parent's hop is gone: a pane that looks connected
        and answers nothing."""
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

        async def no_cycle(_inst, _port):
            return ""

        monkeypatch.setattr(mgr, "_chain_cycle_reason", no_cycle)
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr))
        asyncio.run(mgr.connect("b"))
        asyncio.run(mgr.connect("c"))
        assert mgr.status("c") is not None

        asyncio.run(mgr.disconnect("b"))

        assert mgr.status("b") is None
        assert mgr.status("c") is None, "left a child riding a hop that is gone"
        # The child keeps its intent: the user turned off the PARENT, so
        # reconnecting the parent must be able to bring its crews back.
        assert reg.get("c").was_connected is True
        assert reg.get("b").was_connected is False

    def test_removing_a_parent_removes_the_rows_below_it(self, tmp_path, monkeypatch):
        """A row left behind describes a forward that can never be opened again."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=1,
            via_remote_id=VIA_ID,
        )
        reg.add(
            name="D",
            ssh_host="d-host",
            instance_id="d",
            via_instance_id="c",
            via_remote_port=2,
            via_remote_id=VIA_ID,
        )
        reg.add(name="Other", ssh_host="other-host", instance_id="other")

        resp = asyncio.run(handlers.api_instances_remove(_FakeReq(_State(reg), match={"id": "b"})))
        assert resp.status == 200
        assert _resp_body(resp)["removed"] == "b"
        # The rows themselves are the proof the cascade ran: c and d are gone and
        # the unrelated crew is not.
        assert [i.id for i in reg.list()] == ["other"]

    def test_removing_an_id_with_no_row_deletes_nothing(self, tmp_path, monkeypatch):
        """A 404 has to be decided BEFORE anything is deleted. Reading it off the
        parent's own removal meant the descendants were already gone by the time
        the missing parent answered "not found" -- so a DELETE of an orphan's
        former parent id reported a failure having just deleted the crews under
        it, and dropped the list of which ones they were with it.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        # `c` and `d` still name `b` as their hop, but b's own row is gone -- the
        # shape an unregister leaves behind, since it removes one row and cascades
        # nothing.
        reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=1,
            via_remote_id=VIA_ID,
        )
        reg.add(
            name="D",
            ssh_host="d-host",
            instance_id="d",
            via_instance_id="c",
            via_remote_port=2,
            via_remote_id=VIA_ID,
        )

        resp = asyncio.run(handlers.api_instances_remove(_FakeReq(_State(reg), match={"id": "b"})))
        assert resp.status == 404
        assert sorted(i.id for i in reg.list()) == ["c", "d"], "404'd after deleting rows"

    def test_a_child_that_will_not_stop_refuses_the_removal_with_nothing_deleted(
        self, tmp_path, monkeypatch
    ):
        """Rows deleted over a live forwarder strand it together with its minted
        token, and the row that goes takes the `forwarder_pid` reclaim hint with it,
        so nothing can find it again while the gateway runs. An EXCEPTION out of the
        teardown is unambiguous -- unlike a status reading, which cannot tell a
        failed stop from a reconnect that landed after a successful one -- so it is
        safe to refuse on, and every row stays.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=1,
            via_remote_id="c-on-b",
        )

        class _StuckChild:
            def __init__(self):
                self.calls: list[str] = []
                self.intent_kept: list[bool] = []

            async def disconnect(self, instance_id, *, keep_intent=False):
                self.calls.append(instance_id)
                self.intent_kept.append(keep_intent)
                if instance_id == "c":
                    raise OSError("terminate refused")
                return True

        mgr = _StuckChild()
        resp = asyncio.run(
            handlers.api_instances_remove(_FakeReq(_State(reg, mgr), match={"id": "b"}))
        )
        assert resp.status == 409
        assert _resp_body(resp)["code"] == "remove_teardown_failed"
        assert sorted(i.id for i in reg.list()) == ["b", "c"], "deleted rows over a live forward"
        # Deepest first, and it stopped AT the failure rather than carrying on to
        # the parent -- the parent's forward is the child's route out.
        assert mgr.calls == ["c"]
        # And the teardown kept the sticky connect intent. The response says nothing
        # was removed, so nothing may be left changed either: clearing it would drop
        # the descendants' tabs (`was_connected || connected || warm`) and a reconnect
        # of the parent would not bring them back, which contradicts the sentence.
        assert mgr.intent_kept == [True], "cleared connect intent under a 'nothing was removed'"

    def test_a_descendant_torn_down_before_the_failure_keeps_its_intent(
        self, tmp_path, monkeypatch
    ):
        """The case the abort path actually damaged, which needs THREE levels.

        With only a parent and one child the loop fails on its first candidate, so
        nothing was torn down yet and no intent could be lost. Give the parent a
        grandchild and the loop tears the grandchild down SUCCESSFULLY first, then
        fails on the child -- and the response still says nothing was removed. Under
        the default that succeeded teardown had already cleared the grandchild's
        sticky intent, so its tab was gone and reconnecting the parent would not have
        brought it back: a row left changed under a sentence promising otherwise.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=1,
            via_remote_id="c-on-b",
        )
        reg.add(
            name="D",
            ssh_host="d-host",
            instance_id="d",
            via_instance_id="c",
            via_remote_port=2,
            via_remote_id="d-on-c",
        )

        class _StuckMiddle:
            """D comes down cleanly; C will not. Deepest first, so D goes first."""

            def __init__(self):
                self.seen: list[tuple[str, bool]] = []

            async def disconnect(self, instance_id, *, keep_intent=False):
                self.seen.append((instance_id, keep_intent))
                if instance_id == "c":
                    raise OSError("terminate refused")
                return True

        mgr = _StuckMiddle()
        resp = asyncio.run(
            handlers.api_instances_remove(_FakeReq(_State(reg, mgr), match={"id": "b"}))
        )
        assert resp.status == 409
        assert _resp_body(resp)["code"] == "remove_teardown_failed"
        assert sorted(i.id for i in reg.list()) == ["b", "c", "d"], "deleted a row it refused"
        # D really was torn down first, and it was torn down WITHOUT losing intent.
        assert [i for i, _ in mgr.seen] == ["d", "c"], f"wrong order: {mgr.seen}"
        assert all(kept for _, kept in mgr.seen), f"a teardown cleared intent: {mgr.seen}"
        # The flag itself is the handler's whole contribution here -- a stubbed manager
        # performs no registry write, so asserting `was_connected` off these rows would
        # pass whatever the handler passed. That the manager HONOURS the flag is
        # `_teardown_locked`'s own unit, tested where it lives.

    def test_a_forward_that_reappears_after_the_rows_go_is_reported_not_raised(
        self, tmp_path, monkeypatch, caplog
    ):
        """The second pass cannot refuse: the rows are already gone, so raising
        would report failure for a removal that happened. The crew it could not stop
        is logged rather than hidden -- the response cannot carry it, because the
        removal succeeded and nothing at the dashboard reads a residual.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="B", ssh_host="b-host", instance_id="b")

        class _RacingStuck:
            def __init__(self):
                self.calls = 0

            async def disconnect(self, _instance_id, *, keep_intent=False):
                self.calls += 1
                # Down cleanly before the rows go; a reconnect slips in and then
                # refuses to stop on the second pass.
                if self.calls > 1:
                    raise OSError("reappeared and will not stop")
                return True

        stuck = _RacingStuck()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers_instances"):
            resp = asyncio.run(
                handlers.api_instances_remove(_FakeReq(_State(reg, stuck), match={"id": "b"}))
            )
        assert resp.status == 200, "raised over rows that were already gone"
        assert _resp_body(resp)["removed"] == "b"
        assert [i.id for i in reg.list()] == [], "kept the row it reported removed"
        # Two witnesses that the pass RAN and did not swallow the failure: it called
        # disconnect a second time, and it said so where an operator reads it.
        assert stuck.calls == 2, "never made the second pass"
        assert any(
            "could not stop a forward that reappeared" in r.message and "b" in r.message
            for r in caplog.records
        ), "hid a forward it could not stop"

    def test_the_sweep_reaches_every_captured_crew_once_the_rows_are_gone(
        self, tmp_path, monkeypatch
    ):
        """A connect racing the removal can re-establish a child's forward. The
        sweep afterwards has to name that child itself: `disconnect` finds children
        by reading the registry, and the rows are gone by then, so sweeping only
        the crew that was asked for tears down nothing below it and leaves a
        forward holding a port with no row left to surface it."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=1,
            via_remote_id=VIA_ID,
        )
        reg.add(
            name="D",
            ssh_host="d-host",
            instance_id="d",
            via_instance_id="c",
            via_remote_port=2,
            via_remote_id=VIA_ID,
        )

        mgr = _FakeMgr(registry=reg)
        resp = asyncio.run(
            handlers.api_instances_remove(_FakeReq(_State(reg, mgr), match={"id": "b"}))
        )
        assert resp.status == 200
        # Two passes, each deepest-first over the captured crews. The FIRST runs
        # before any row is deleted, so a stop that raises can still refuse with
        # nothing removed; the SECOND runs after, for a forward a racing connect
        # re-established between the teardown and the offloaded deletion.
        assert mgr.disconnected == [
            "d",
            "c",
            "b",
            "d",
            "c",
            "b",
        ], f"swept {mgr.disconnected}, so a racing connect below 'b' survives"
        # The first pass runs WITH the rows present -- that is what lets it refuse
        # before anything is deleted. The second runs with the registry empty, which
        # is exactly why its ids cannot come from it: only the captured list still
        # names 'c' and 'd'.
        assert mgr.rows_at_disconnect[:3] == [3, 3, 3], "tore down after deleting rows"
        assert mgr.rows_at_disconnect[3:] == [0, 0, 0], "swept while rows still existed"

    def test_a_chained_add_cannot_land_inside_a_parent_removal(self, tmp_path, monkeypatch):
        """Validating the parent and inserting the child are two awaits. A removal
        between them snapshots the subtree without the new row and then deletes the
        parent under it, leaving a crew whose only route is a hop that is gone."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="B", ssh_host="b-host", instance_id="b")

        async def interleave():
            # The add reaches its parent check first; the removal then runs to
            # completion while the add is still between validation and insert --
            # which it can only do if the two are not one critical section.
            adding = asyncio.create_task(
                handlers.api_instances_add(
                    _FakeReq(
                        _State(reg),
                        body={
                            "name": "C",
                            "ssh_host": "c-host",
                            "via_instance_id": "b",
                            "via_remote_port": 53999,
                            "via_remote_id": VIA_ID,
                        },
                    )
                )
            )
            await asyncio.sleep(0)
            removing = asyncio.create_task(
                handlers.api_instances_remove(
                    _FakeReq(_State(reg, _FakeMgr(registry=reg)), match={"id": "b"})
                )
            )
            return await asyncio.gather(adding, removing)

        added, removed = asyncio.run(interleave())
        assert removed.status == 200
        orphans = [i.id for i in reg.list() if i.via_instance_id and not reg.get(i.via_instance_id)]
        assert (
            orphans == []
        ), f"{orphans} name a parent that is gone, so they can never be connected again"
        # Either order is correct. What must not happen is a row surviving its
        # parent: the add either lands before the snapshot and goes with it, or
        # lands after the parent is gone and is refused.
        assert added.status in (201, 400)

    def test_the_rows_go_leaves_first_so_no_row_outlives_its_parent(self, tmp_path, monkeypatch):
        """An interrupted sweep may leave a parent with fewer children, which still
        connects; a row whose parent is already gone can never be opened again."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=1,
            via_remote_id=VIA_ID,
        )
        reg.add(
            name="D",
            ssh_host="d-host",
            instance_id="d",
            via_instance_id="c",
            via_remote_port=2,
            via_remote_id=VIA_ID,
        )

        order: list[str] = []
        real_remove = reg.remove

        def recording_remove(target):
            order.append(target)
            return real_remove(target)

        monkeypatch.setattr(reg, "remove", recording_remove)
        resp = asyncio.run(
            handlers.api_instances_remove(_FakeReq(_State(reg, _FakeMgr()), match={"id": "b"}))
        )
        assert resp.status == 200
        assert order == ["d", "c", "b"], f"removed in {order}, so a row can outlive its parent"


# ── the depth cap, at the add boundary ───────────────────────────────────


class TestDepthCap:
    def _reg(self, tmp_path):
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        return reg

    def test_a_second_hop_is_allowed(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        body = {
            "name": "C",
            "ssh_host": "c-host",
            "id": "c",
            "via_instance_id": "b",
            "via_remote_id": VIA_ID,
            "via_remote_port": 53999,
        }
        resp = asyncio.run(handlers.api_instances_add(_FakeReq(_State(reg), body=body)))
        assert resp.status == 201
        assert reg.get("c").via_remote_port == 53999

    def test_a_third_hop_is_refused_before_anything_is_written(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=1,
            via_remote_id=VIA_ID,
        )
        body = {
            "name": "D",
            "ssh_host": "d-host",
            "id": "d",
            "via_instance_id": "c",
            "via_remote_id": VIA_ID,
            "via_remote_port": 2,
        }
        resp = asyncio.run(handlers.api_instances_add(_FakeReq(_State(reg), body=body)))
        assert resp.status == 400
        assert _resp_body(resp)["code"] == "chain_too_deep"
        assert reg.get("d") is None, "wrote the record it refused"
        # The number is HOPS, and it counts the link to the refused crew itself, so
        # reporting it as machines in between overstates the chain by one: a refused
        # 3 is two intermediary machines. The reader is being told how far away the
        # crew is, so the unit has to be the one the cap is expressed in.
        detail = _resp_body(resp)["error"]
        assert "3 hops" in detail, f"the refusal does not state the distance in hops: {detail}"
        assert "machines" not in detail, f"reported hops as machines in between: {detail}"
        # Bound to the constant, not to the number 2: raising the cap moves this
        # test's own expectation with it, instead of leaving it to assert a limit
        # the product does not have.
        assert MAX_VIA_HOPS == 2, "the refusal above assumes a third hop is one too many"

    def test_every_chain_refusal_is_a_sentence_naming_the_crew(self, tmp_path, monkeypatch):
        """The panel renders this text raw, and it can arrive after the connect
        already looked like it succeeded -- so a clause opening lowercase with no
        subject left the reader unable to tell what was refused or what had been
        doing it. Each refusal names the crew and reads as a whole sentence.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=1,
            via_remote_id=VIA_ID,
        )
        reg.add(
            name="Ssm",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            instance_id="ssm",
        )

        def add(name, **over):
            body = {
                "name": name,
                "ssh_host": "x-host",
                "via_remote_id": "some-remote",
                "via_remote_port": 7,
                **over,
            }
            return asyncio.run(handlers.api_instances_add(_FakeReq(_State(reg), body=body)))

        cases = [
            # Deeper than the cap: D behind C, which is already behind B.
            ("chain_too_deep", "D Box", add("D Box", via_instance_id="c")),
            ("chain_parent_unknown", "E Box", add("E Box", via_instance_id="nobody")),
            ("chain_parent_not_ssh", "F Box", add("F Box", via_instance_id="ssm")),
            (
                "chain_duplicate",
                "G Box",
                add("G Box", via_instance_id="b", via_remote_id=VIA_ID),
            ),
        ]
        for code, name, resp in cases:
            body = _resp_body(resp)
            assert resp.status == 400, f"{code} did not refuse"
            assert body["code"] == code, f"got {body['code']} for {code}"
            error = body["error"]
            assert error[:1].isupper(), f"{code} opens mid-sentence: {error!r}"
            assert error.rstrip().endswith("."), f"{code} is not a full sentence: {error!r}"
            # The crew being connected is named, which is what the reader is looking
            # for and what an id cannot tell them. Spelled exactly as given: a
            # `capitalize()` on the whole subject lowercased the name's own letters.
            assert name in error, f"{code} does not name {name!r}: {error!r}"

    def test_an_unknown_parent_is_refused(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        body = {
            "name": "C",
            "ssh_host": "c-host",
            "id": "c",
            "via_instance_id": "nope",
            "via_remote_id": VIA_ID,
            "via_remote_port": 1,
        }
        resp = asyncio.run(handlers.api_instances_add(_FakeReq(_State(reg), body=body)))
        assert resp.status == 400
        assert _resp_body(resp)["code"] == "chain_parent_unknown"

    def test_a_second_row_for_the_same_remote_crew_is_refused(self, tmp_path, monkeypatch):
        """One row per remote crew, settled HERE rather than by the announcing pane.
        The pane decides from a list it refreshes only after the add and the connect
        that add triggers, so a second announcement inside that window reads a list
        without the first row and asks for a duplicate -- which the registry would
        take under a suffixed id, giving one remote crew two rows, two forwards, two
        tabs and two charges against the width cap.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="B", ssh_host="b-host", instance_id="b")

        body = {
            "name": "C",
            "ssh_host": "c-host",
            "id": "c",
            "via_instance_id": "b",
            "via_remote_id": "c-on-b",
            "via_remote_port": 53999,
        }
        first = asyncio.run(handlers.api_instances_add(_FakeReq(_State(reg), body=body)))
        assert first.status == 201

        again = asyncio.run(handlers.api_instances_add(_FakeReq(_State(reg), body=body)))
        assert again.status == 400
        assert _resp_body(again)["code"] == "chain_duplicate"
        assert len([i for i in reg.list() if i.via_remote_id == "c-on-b"]) == 1

    def test_the_uniqueness_guard_is_scoped_to_the_pair(self, tmp_path, monkeypatch):
        """It must not refuse a DIFFERENT crew behind the same parent, nor the same
        remote id behind a different parent -- two parents can each hold a crew whose
        id on that parent happens to read the same.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        reg.add(name="E", ssh_host="e-host", instance_id="e")

        def add(name, parent, remote_id):
            return asyncio.run(
                handlers.api_instances_add(
                    _FakeReq(
                        _State(reg),
                        body={
                            "name": name,
                            "ssh_host": f"{name.lower()}-host",
                            "via_instance_id": parent,
                            "via_remote_id": remote_id,
                            "via_remote_port": 1,
                        },
                    )
                )
            )

        assert add("C", "b", "shared-id").status == 201
        assert add("D", "b", "other-id").status == 201, "refused a different crew on b"
        assert add("F", "e", "shared-id").status == 201, "refused the same id on another parent"
        assert add("G", "b", "shared-id").status == 400, "admitted the duplicate pair"

    def test_two_racing_notices_for_one_crew_leave_one_row(self, tmp_path, monkeypatch):
        """The check is only decisive INSIDE the lock. Outside it both notices read a
        list without the other's row, both pass, and both add -- the same bug in a new
        place. A retry from the announcing pane produces exactly this pair, so it is
        the ordinary case rather than an error path.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="B", ssh_host="b-host", instance_id="b")

        body = {
            "name": "C",
            "ssh_host": "c-host",
            "via_instance_id": "b",
            "via_remote_id": "c-on-b",
            "via_remote_port": 53999,
        }

        async def race():
            return await asyncio.gather(
                handlers.api_instances_add(_FakeReq(_State(reg), body=dict(body))),
                handlers.api_instances_add(_FakeReq(_State(reg), body=dict(body))),
            )

        first, second = asyncio.run(race())
        assert sorted([first.status, second.status]) == [201, 400], "both notices were accepted"
        refused = [r for r in (first, second) if r.status == 400]
        assert [_resp_body(r)["code"] for r in refused] == ["chain_duplicate"]
        assert len([i for i in reg.list() if i.via_remote_id == "c-on-b"]) == 1
        # One row means one forward and one tab: both follow the row, and a second
        # row is the only way one remote crew acquires a second of either.
        assert len([i for i in reg.list() if i.via_instance_id == "b"]) == 1

    def test_the_uniqueness_check_sits_inside_the_mutation_lock(self):
        """Structural, because the behaviour test above cannot see WHY it passed: a
        check-then-add outside the lock is the same defect one step along, and no
        single-threaded assertion distinguishes the two placements.
        """
        import ast
        import inspect

        from kiro_crew.dashboard import handlers_instances as handlers

        tree = ast.parse(inspect.getsource(handlers.api_instances_add))
        fn = tree.body[0]

        def guarded_blocks(node):
            for sub in ast.walk(node):
                if isinstance(sub, ast.AsyncWith) and any(
                    isinstance(item.context_expr, ast.Name)
                    and item.context_expr.id == "_CHAIN_MUTATION_LOCK"
                    for item in sub.items
                ):
                    yield sub

        blocks = list(guarded_blocks(fn))
        assert len(blocks) == 1, f"expected one guarded block, found {len(blocks)}"

        def refusal_calls(node):
            return [
                n
                for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "_chain_refusal"
            ]

        inside = sum(len(refusal_calls(stmt)) for stmt in blocks[0].body)
        total = len(refusal_calls(fn))
        assert inside == 1, f"the refusal is called {inside} times inside the lock, expected 1"
        assert total == inside, f"{total - inside} call(s) sit outside the lock"

    def test_a_name_past_the_cap_is_refused_before_the_row_is_written(self, tmp_path, monkeypatch):
        """A bound bounds every field it RETAINS. The name is persisted per row and
        echoed in every list reply, so leaving it unbounded makes the row caps no
        bound at all: eight chained rows of arbitrary-length names are still an
        arbitrary-size registry and an arbitrary-size response.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        body = {
            "name": "x" * (INSTANCE_NAME_MAX + 1),
            "ssh_host": "d-host",
            "id": "d",
            "via_instance_id": "b",
            "via_remote_port": 53999,
            "via_remote_id": "d-2",
        }
        resp = asyncio.run(handlers.api_instances_add(_FakeReq(_State(reg), body=body)))

        assert resp.status == 400, "retained a name past the cap"
        assert not [i for i in reg.list() if i.id == "d"], "wrote the row anyway"

        # Exactly at the cap is accepted: the bound is a cap, not a smaller limit.
        body["name"] = "x" * INSTANCE_NAME_MAX
        resp = asyncio.run(handlers.api_instances_add(_FakeReq(_State(reg), body=body)))
        assert resp.status == 201, "refused a name exactly at the cap"

    def test_the_backend_name_cap_matches_the_relays(self):
        """The relay slices an announced name before sending it, so frame code can
        never exceed the cap; the backend's copy is what makes the bound a property
        of the STORE, which is the only place it holds for a caller that does not go
        through the relay. Two numbers that must agree, pinned so they cannot drift.
        """
        import re

        ts = (
            pathlib.Path(__file__).resolve().parents[1] / "website/src/lib/chainAnnounce.ts"
        ).read_text()
        m = re.search(r"export const CHAINED_NAME_MAX = (\d+)", ts)
        assert m, "the relay's cap is no longer declared where this test reads it"
        assert int(m.group(1)) == INSTANCE_NAME_MAX, (
            f"the relay slices at {m.group(1)} while the store keeps " f"{INSTANCE_NAME_MAX}"
        )

    def test_a_parent_already_at_the_width_cap_is_refused(self, tmp_path, monkeypatch):
        """Depth and width are independent bounds. Every crew here is two hops, which
        the depth cap allows without limit, so the population behind one parent needs
        its own cap -- these rows are created by that parent's pane announcing crews,
        not by anyone at this dashboard, and each one is a forward and a mint here.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="B", ssh_host="b-host", instance_id="b")

        def _add(child_id: str, parent: str = "b"):
            body = {
                "name": child_id.upper(),
                "ssh_host": f"{child_id}-host",
                "id": child_id,
                "via_instance_id": parent,
                # Its own id on the parent: eight crews behind one hop are eight
                # DIFFERENT crews, so sharing one id here would exercise the
                # uniqueness guard instead of the cap this test is about.
                "via_remote_id": f"{child_id}-on-{parent}",
                "via_remote_port": 1,
            }
            return asyncio.run(handlers.api_instances_add(_FakeReq(_State(reg), body=body)))

        for n in range(MAX_CHAINED_PER_PARENT):
            resp = _add(f"kid{n}")
            assert resp.status == 201, f"refused child {n}, below the cap"

        resp = _add("overflow")
        assert resp.status == 400
        assert _resp_body(resp)["code"] == "chain_parent_full"
        assert reg.get("overflow") is None, "wrote the record it refused"
        # Bound to the constant, so raising the cap moves this expectation with it.
        assert (
            sum(1 for i in reg.list() if i.via_instance_id == "b") == MAX_CHAINED_PER_PARENT
        ), "the refusal must land exactly at the cap, not before or after it"

    def test_the_width_cap_is_counted_per_parent(self, tmp_path, monkeypatch):
        """A full parent must not refuse crews riding a DIFFERENT one, or one parent's
        pane could stop every other parent from being used.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        reg.add(name="E", ssh_host="e-host", instance_id="e")
        for n in range(MAX_CHAINED_PER_PARENT):
            reg.add(
                name=f"KID{n}",
                ssh_host=f"kid{n}-host",
                instance_id=f"kid{n}",
                via_instance_id="b",
                via_remote_port=1,
                via_remote_id=f"kid{n}-on-b",
            )

        body = {
            "name": "F",
            "ssh_host": "f-host",
            "id": "f",
            "via_instance_id": "e",
            "via_remote_id": VIA_ID,
            "via_remote_port": 1,
        }
        resp = asyncio.run(handlers.api_instances_add(_FakeReq(_State(reg), body=body)))
        assert resp.status == 201, "a full parent blocked a crew riding another parent"

        top = {"name": "G", "ssh_host": "g-host", "id": "g"}
        resp = asyncio.run(handlers.api_instances_add(_FakeReq(_State(reg), body=top)))
        assert resp.status == 201, "the chained cap refused a top-level crew"

    def test_the_published_total_is_the_token_s_own_not_the_row_s(self, tmp_path, monkeypatch):
        """`token_ttl_remaining` counts down from the STORED total, so a reader
        measuring how far a token has run must divide by that same number. For a
        chained crew the row's TTL is a different figure -- the parent issues the
        token -- and dividing by the row puts a short token permanently past the
        refresh threshold, re-minting on every poll and remounting the pane.
        """
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        inst = reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            ttl="20h",
            via_instance_id="b",
            via_remote_port=1,
            via_remote_id=VIA_ID,
        )

        # The parent issued an hour; our row says twenty. The stored total is the
        # shorter, so that is the number the status must publish. A store needs a
        # live forward to be stored against, which every real store has.
        mgr._chained_ttl["c"] = "1h"
        mgr._tunnels["c"] = _FakeTunnel("c", "b-host", 53700, 1)
        mgr._tunnel_epoch["c"] = 1
        assert (
            mgr._store_token(inst, _Mint("TOKEN", ttl="1h"), minted_at_epoch=1, binds_forward=True)
            is True
        )
        assert mgr.token_ttl_total("c") == 3600, "published our row's TTL, not the token's"

        remaining = mgr.token_ttl_remaining("c")
        assert remaining is not None and remaining <= 3600
        # No token, no total -- absent rather than zero, because a zero total
        # would read as a fully elapsed token and send readers to a refresh.
        assert mgr.token_ttl_total("nope") is None

    def test_the_status_dict_carries_the_total_beside_the_remaining(self, tmp_path, monkeypatch):
        """Both or neither: a consumer that gets a remaining without the total it
        counts down from has to invent a denominator, which is the defect.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        class _St:
            def to_dict(self):
                return {"instance_id": "c", "state": "connected"}

        class _Mgr:
            def __init__(self, total):
                self._total = total

            def status(self, _iid):
                return _St()

            def token_ttl_remaining(self, _iid):
                return 3500

            def token_ttl_total(self, _iid):
                return self._total

        reg = self._reg(tmp_path)
        d = handlers._status_for(_State(reg, manager=_Mgr(3600)), "c")
        assert d["token_ttl_remaining"] == 3500
        assert d["token_ttl_total"] == 3600

        d2 = handlers._status_for(_State(reg, manager=_Mgr(None)), "c")
        assert d2["token_ttl_remaining"] == 3500
        assert "token_ttl_total" not in d2

    def test_an_ssm_parent_is_refused(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(
            name="B",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            instance_id="b",
        )
        body = {
            "name": "C",
            "ssh_host": "c-host",
            "id": "c",
            "via_instance_id": "b",
            "via_remote_id": VIA_ID,
            "via_remote_port": 1,
        }
        resp = asyncio.run(handlers.api_instances_add(_FakeReq(_State(reg), body=body)))
        assert resp.status == 400
        assert _resp_body(resp)["code"] == "chain_parent_not_ssh"

    def test_a_patch_cannot_re_parent_a_crew(self, tmp_path, monkeypatch):
        """Re-parenting would move a crew onto a hop whose depth was never
        checked, so only the hop PORT is editable."""
        from kiro_crew.dashboard import handlers_instances as handlers

        assert "via_instance_id" not in handlers._PATCH_FIELD_TYPES
        assert handlers._PATCH_FIELD_TYPES["via_remote_port"] is int

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=1,
            via_remote_id=VIA_ID,
        )
        resp = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(
                    _State(reg),
                    match={"id": "c"},
                    body={"via_instance_id": "other", "via_remote_port": 2},
                )
            )
        )
        assert resp.status == 200
        updated = reg.get("c")
        assert updated.via_instance_id == "b", "a PATCH re-parented the crew"
        assert updated.via_remote_port == 2


# ── the embed-token endpoint ─────────────────────────────────────────────


class TestEmbedTokenEndpoint:
    class _Mgr:
        def __init__(self, result):
            self.result = result
            self.calls: list[tuple[str, int]] = []

        async def mint_embed_token(self, iid, port):
            self.calls.append((iid, port))
            return self.result

    def test_it_hands_back_the_token_and_the_hop_port(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        mgr = self._Mgr((True, {"token": "TOK", "port": 53999}))
        resp = asyncio.run(
            handlers.api_instances_embed_token(
                _FakeReq(_State(reg, mgr), match={"id": "c"}, body={"embed_parent_port": 9191})
            )
        )
        assert resp.status == 200
        assert _resp_body(resp) == {"token": "TOK", "port": 53999}
        assert mgr.calls == [("c", 9191)]

    def test_a_malformed_port_is_refused_before_any_mint(self, tmp_path, monkeypatch):
        """`isinstance(True, int)` is True, so a bool would otherwise be accepted
        as port 1 and mint a token no page can use."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        for bad in (True, 0, -1, 70000, "9191", None):
            mgr = self._Mgr((True, {"token": "TOK", "port": 1}))
            resp = asyncio.run(
                handlers.api_instances_embed_token(
                    _FakeReq(_State(reg, mgr), match={"id": "c"}, body={"embed_parent_port": bad})
                )
            )
            assert resp.status == 400, f"accepted embed_parent_port={bad!r}"
            assert mgr.calls == []

    def test_a_refusal_carries_the_managers_status(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        mgr = self._Mgr((False, {"error": "too deep", "code": "chain_too_deep", "status": 400}))
        resp = asyncio.run(
            handlers.api_instances_embed_token(
                _FakeReq(_State(reg, mgr), match={"id": "c"}, body={"embed_parent_port": 9191})
            )
        )
        assert resp.status == 400
        body = _resp_body(resp)
        assert body["code"] == "chain_too_deep"
        assert "status" not in body, "leaked the transport hint into the payload"

    def test_a_non_owner_caller_is_refused(self, tmp_path, monkeypatch):
        """The route mints a credential for a machine, so it carries the same
        owner-only gate as every other route in this control plane."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        mgr = self._Mgr((True, {"token": "TOK", "port": 1}))
        resp = asyncio.run(
            handlers.api_instances_embed_token(
                _FakeReq(
                    _State(reg, mgr),
                    match={"id": "c"},
                    body={"embed_parent_port": 9191},
                    user=None,
                )
            )
        )
        assert resp.status == 401
        assert mgr.calls == []

    def test_the_route_is_registered_ahead_of_the_catch_all_proxy(self):
        """A `{path:.*}` route registered first would swallow this one.

        Every verb is recorded, not just POST: the catch-all is an ``add_route("*",
        ...)``, so a POST-only stub would see no catch-all at all and the assertion
        would pass without checking anything.
        """
        from kiro_crew.dashboard.routes import connections

        paths: list[str] = []

        class _Router:
            """Records the path of every registration, whatever the verb.

            Generic rather than one method per verb: an enumerated stub goes stale
            the moment a route family uses a verb it does not list, and the failure
            then looks like a routing bug rather than a stale double.
            """

            def __getattr__(self, name):
                def record(*args, **_kw):
                    # add_route("*", path, handler) puts the path second; every
                    # add_<verb>(path, handler) puts it first.
                    for arg in args:
                        if isinstance(arg, str) and arg.startswith("/"):
                            paths.append(arg)
                            break
                    return None

                if name.startswith("add_"):
                    return record
                raise AttributeError(name)

        class _App(dict):
            router = _Router()

        connections.register(_App())  # type: ignore[arg-type]

        embed = next(i for i, p in enumerate(paths) if p.endswith("/embed-token"))
        catch_all = [i for i, p in enumerate(paths) if "{path:.*}" in p]
        assert catch_all, "no catch-all route recorded — the stub missed a verb"
        assert embed < min(catch_all)


# ── gateway identity ─────────────────────────────────────────────────────


class TestForgedHostIsNeverDialled:
    """A readiness notice is untrusted payload, and its `ssh_host` is a RECORD.

    The sender is a warm pane, so the origin is the one the owner deliberately
    connected to -- an origin check cannot help here, because in this threat model
    the attacker holds the correct origin. What makes the forgery harmless is that a
    chained row's own host is never dialled: the forward rides the PARENT, so the
    only coordinates this gateway acts on come from the parent's record, which the
    pane never gets to write.
    """

    def _pair(self, tmp_path, monkeypatch):
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        return reg, mgr

    def test_a_forged_host_does_not_become_the_dial_target(self, tmp_path, monkeypatch):
        reg, mgr = self._pair(tmp_path, monkeypatch)
        # Exactly what a compromised pane can put on the wire: a host it chose.
        child = reg.add(
            name="Prod DB",
            ssh_host="prod-db.internal.example",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=53999,
            via_remote_id=VIA_ID,
        )

        params = mgr._resolve_transport(child, reg.get("b"))
        assert params.ssh_host == "b-host", "dialled the host the payload named"
        assert "prod-db" not in params.ssh_host
        # And what it DOES dial: the parent, at the hop the parent advertised.
        assert params.forward_remote_port(child.remote_port) == 53999

    def test_the_forward_is_built_from_the_parent_not_the_row(self, tmp_path, monkeypatch):
        """Asserted on the spawned forward, not only on the params: the argv is what
        actually reaches ssh, so a later change that passes the row's host through
        some other route is caught here rather than only at the resolver.
        """
        reg, mgr = self._pair(tmp_path, monkeypatch)
        reg.add(
            name="Prod DB",
            ssh_host="prod-db.internal.example",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=53999,
            via_remote_id=VIA_ID,
        )
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr))
        assert asyncio.run(mgr.connect("c")).state.value == "connected"

        made = [x for x in _FakeTunnel.made if x.iid == "c"]
        assert made, "no forward was built for the chained crew"
        for spec in made:
            assert spec.ssh_host == "b-host", f"the forward dialled {spec.ssh_host!r}"
            assert spec.remote_port == 53999, "forwarded to something other than the hop"

    def test_a_chained_row_never_reaches_the_unchained_host_branch(self):
        """Structural, because the guarantee is a branch and not a value: the chained
        resolver returns before the branch that reads the row's own `ssh_host`. A
        future edit that moves the read above the chain check would be invisible to a
        value assertion on today's inputs.
        """
        import ast
        import pathlib as _p

        src = (
            _p.Path(__file__).resolve().parents[1] / "src/kiro_crew/instances/ssh_tunnel_manager.py"
        ).read_text()
        fn = next(
            n
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)
            and n.name == "_resolve_transport"
        )
        # The chain check is the FIRST branch, and it returns.
        first = fn.body[1] if isinstance(fn.body[0], ast.Expr) else fn.body[0]
        assert isinstance(first, ast.If), "the chained branch is no longer first"
        assert any(
            isinstance(n, ast.Attribute) and n.attr == "via_instance_id"
            for n in ast.walk(first.test)
        ), "the first branch no longer tests via_instance_id"
        assert any(
            isinstance(n, ast.Return) for n in first.body
        ), "the chained branch falls through"


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
        from kiro_crew.instances.ssh_tunnel_manager import HopPortGuard

        reg, mgr = _mgr(tmp_path, monkeypatch)
        mgr._hop_guard = HopPortGuard()
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
        try:
            mgr._tunnels["c"].status.state = TunnelState.ERROR
            mgr._recover_attempts["c"] = mgr._max_recovery + 5

            asyncio.run(mgr._recover_after("c", 0))

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

        async def mint_then_move(inst, params, parent_port):
            out = await real_mint(inst, params, parent_port)
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
        from kiro_crew.instances.ssh_tunnel_manager import HopPortGuard, TunnelState

        reg, mgr = _mgr(tmp_path, monkeypatch)
        mgr._hop_guard = HopPortGuard()
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
        release = [
            n.lineno
            for n in ast.walk(recover)
            if isinstance(n, ast.Call) and "_hop_guard.release" in ast.unparse(n)
        ]
        rebuild = [
            n.lineno
            for n in ast.walk(recover)
            if isinstance(n, ast.Call) and "_rebuild" in ast.unparse(n)
        ]
        assert release, "the recovery never releases its own hold, so the rebuild binds against it"
        assert rebuild, "no rebuild call found -- re-derive this pin"
        assert min(release) < min(rebuild), "the hold is released AFTER the rebuild tries to bind"

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
        from kiro_crew.instances.ssh_tunnel_manager import HopPortGuard, TunnelState

        reg, mgr = _mgr(tmp_path, monkeypatch)
        mgr._hop_guard = HopPortGuard()
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
        from kiro_crew.instances.ssh_tunnel_manager import HopPortGuard, TunnelState

        reg, mgr = _mgr(tmp_path, monkeypatch)
        mgr._hop_guard = HopPortGuard()
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
        from kiro_crew.instances.ssh_tunnel_manager import HopPortGuard, TunnelState

        reg, mgr = _mgr(tmp_path, monkeypatch)
        mgr._hop_guard = HopPortGuard()
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
        from kiro_crew.instances.ssh_tunnel_manager import HopPortGuard, TunnelState

        reg, mgr = _mgr(tmp_path, monkeypatch)
        mgr._hop_guard = HopPortGuard()
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
        from kiro_crew.instances.ssh_tunnel_manager import HopPortGuard

        reg, mgr = _mgr(tmp_path, monkeypatch)
        mgr._hop_guard = HopPortGuard()
        reg.add(name="C", ssh_host="c-host", instance_id="c", ttl="2h")
        asyncio.run(mgr.connect("c"))
        assert asyncio.run(mgr.mint_embed_token("c", 9191))[0] is True

        try:
            mgr._lent_hops[65000] = time.time() + 999  # never in the record
            mgr._apply_hop_holds(reg.live_hop_lease_deadlines())
            assert 65000 not in mgr._lent_hops, "a cache entry the registry never had survived"
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

        from kiro_crew.instances.hop_port_guard import HopPortGuard

        reg, mgr = _mgr(tmp_path, monkeypatch)
        # The fixture installs an inert guard so fake-port cases do not leak listening
        # sockets; this case is precisely about the real one, so put it back.
        mgr._hop_guard = HopPortGuard()
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
        """
        reg, _ = _mgr(tmp_path, monkeypatch)
        far = time.time() + 4 * 3600
        reg.lend_hop(53993, far)
        reg.lend_hop(53993, time.time() + 60)

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


class TestHopRetiredDetection:
    """The DETECTION layer on the parent's own answer.

    Terminal ONLY when the parent says the hop is gone. A transport error means try
    again, and the refresh loop's non-terminal retry exists for exactly that -- so
    the two must not be inferred from one another.
    """

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
        return reg, mgr

    def _refresh(self, mgr):
        async def run():
            out = await mgr._refresh_token_once("c")
            if mgr._retirements:
                await asyncio.gather(*list(mgr._retirements))
            return out

        return asyncio.run(run())

    def test_the_parent_saying_the_hop_is_gone_retires_the_forward(self, tmp_path, monkeypatch):
        from kiro_crew.instances.token_mint import HopRetiredError

        reg, mgr = self._chained(tmp_path, monkeypatch)
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr))
        assert asyncio.run(mgr.connect("c")).state.value == "connected"
        assert mgr.get_token("c")

        async def gone(_inst, _params):
            raise HopRetiredError("crew 'b' no longer holds the hop (instance_not_connected)")

        monkeypatch.setattr(mgr, "_mint_through_parent", gone)
        assert self._refresh(mgr) is False

        # Retrying cannot recover a hop the parent says is not ours, and the forward
        # still points at a port the parent may now give to another crew.
        assert "c" not in mgr._tunnels, "kept a forward riding a hop the parent disowned"
        assert not mgr.get_token("c"), "kept the credential that forward carries"
        assert reg.get("c").was_connected is True, "cleared the user's own intent"

    def test_a_transport_error_keeps_retrying_instead(self, tmp_path, monkeypatch):
        """The distinction that makes the layer above safe. A timeout is a blip, and
        tearing a working chain down on one would turn every network hiccup into a
        disconnect the user has to undo.
        """
        from kiro_crew.instances.token_mint import TokenMintError

        reg, mgr = self._chained(tmp_path, monkeypatch)
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr))
        assert asyncio.run(mgr.connect("c")).state.value == "connected"
        before = mgr.get_token("c")

        async def blip(_inst, _params):
            raise TokenMintError("crew 'b' did not answer the mint in time")

        monkeypatch.setattr(mgr, "_mint_through_parent", blip)
        assert self._refresh(mgr) is False

        assert "c" in mgr._tunnels, "tore a working chain down over a transport error"
        assert mgr.get_token("c") == before, "dropped a credential that is still valid"

    @pytest.mark.parametrize(
        "status,code",
        [
            # The parent answers 409 for a crew it holds but is not connected to,
            # and 404 for one whose row is gone. BOTH mean the same thing and are
            # read the same way: the hop is not ours, so retrying cannot recover it.
            # Neither is the route prevention cannot cover -- that one is an identity
            # mismatch on a DIFFERENT port, and a removed crew's port stays withheld
            # because the reservation lives on the registry document. What these two
            # retire is a forward that cannot work again.
            (409, "instance_not_connected"),
            (404, "instance_not_found"),
        ],
    )
    def test_a_parent_that_disowns_the_hop_is_read_over_the_wire(
        self, tmp_path, monkeypatch, status, code
    ):
        """Driven through a real reply, not by raising the exception directly.

        Asserting the CODE SET's contents is what let `instance_not_found` sit in it
        while being unreachable: the status branch for 404 answered first and folded a
        removed crew in with "maybe this build has no chaining endpoint". A test that
        reads the set cannot see that; one that answers a 404 can.
        """
        from kiro_crew.instances import ssh_tunnel_manager as stm
        from kiro_crew.instances.token_mint import HopRetiredError

        reg, mgr = self._chained(tmp_path, monkeypatch)
        inst = reg.get("c")
        params = mgr._resolve_chained_transport(inst, reg.get("b"))

        class _Disowning(_FakeMintSession):
            reply_status = status
            reply = {"error": "no", "code": code}

        monkeypatch.setattr(stm.aiohttp, "ClientSession", _Disowning)
        monkeypatch.setattr(
            mgr, "_peer_target", lambda _pid, path: (f"http://127.0.0.1:1{path}", "kc")
        )
        monkeypatch.setattr(mgr, "_peer_cookie_header", lambda _pid, _name: {})

        with pytest.raises(HopRetiredError):
            asyncio.run(mgr._mint_through_parent(inst, params))

    def test_an_oversized_refusal_body_is_not_read(self, tmp_path, monkeypatch):
        """The bound on the refusal read, which is a read from ANOTHER gateway.

        The success reply on this same wire is capped, and an error body has no
        business being the one unbounded read on it. Past the cap the body is not
        parsed at all, so no `code` is found and the refusal stays an ordinary
        retryable failure rather than being trusted -- failing closed on a reply too
        big to vet.
        """
        from kiro_crew.instances import ssh_tunnel_manager as stm
        from kiro_crew.instances.token_mint import HopRetiredError, TokenMintError

        reg, mgr = self._chained(tmp_path, monkeypatch)
        inst = reg.get("c")
        params = mgr._resolve_chained_transport(inst, reg.get("b"))

        class _Bloated(_FakeMintSession):
            reply_status = 409
            # A real `code`, buried in a body past the cap. Trusting it would mean
            # reading an unbounded amount from the far gateway first.
            reply = {
                "code": "instance_not_connected",
                "pad": "x" * (stm._CHAINED_MINT_REPLY_MAX_BYTES + 64),
            }

        monkeypatch.setattr(stm.aiohttp, "ClientSession", _Bloated)
        monkeypatch.setattr(
            mgr, "_peer_target", lambda _pid, path: (f"http://127.0.0.1:1{path}", "kc")
        )
        monkeypatch.setattr(mgr, "_peer_cookie_header", lambda _pid, _name: {})

        with pytest.raises(TokenMintError) as caught:
            asyncio.run(mgr._mint_through_parent(inst, params))
        assert not isinstance(
            caught.value, HopRetiredError
        ), "read an oversized body to find a code"

    def test_a_parent_with_no_chaining_route_is_not_treated_as_a_retired_hop(
        self, tmp_path, monkeypatch
    ):
        """The other 404, and the reason the code is read rather than the status. A
        build without the endpoint answers 404 too, and retiring the forward over it
        would tear a chain down because the PARENT is old rather than because the hop
        moved, which is the confusion reading the status alone creates.
        """
        from kiro_crew.instances import ssh_tunnel_manager as stm
        from kiro_crew.instances.token_mint import HopRetiredError, TokenMintError

        reg, mgr = self._chained(tmp_path, monkeypatch)
        inst = reg.get("c")
        params = mgr._resolve_chained_transport(inst, reg.get("b"))

        class _NoRoute(_FakeMintSession):
            reply_status = 404
            reply = {"error": "Not Found"}

        monkeypatch.setattr(stm.aiohttp, "ClientSession", _NoRoute)
        monkeypatch.setattr(
            mgr, "_peer_target", lambda _pid, path: (f"http://127.0.0.1:1{path}", "kc")
        )
        monkeypatch.setattr(mgr, "_peer_cookie_header", lambda _pid, _name: {})

        with pytest.raises(TokenMintError) as caught:
            asyncio.run(mgr._mint_through_parent(inst, params))
        assert not isinstance(caught.value, HopRetiredError), "retired a hop over an old build"
        assert "no endpoint for chaining" in str(caught.value)


class TestGatewayIdentity:
    def test_it_is_stable_and_persisted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.config import loader

        loader._invalidate_config_cache()
        from kiro_crew import gateway_identity
        from kiro_crew.gateway_identity import GATEWAY_ID_FILE, gateway_id

        first = gateway_id()
        assert len(first) == 32
        # Drop the memo so the second call has to answer from the FILE. That is
        # what "persisted" claims, and a memo hit would assert nothing about disk.
        gateway_identity._CACHED_IDS.clear()
        assert gateway_id() == first, "minted a second id for one gateway"
        stored = (loader.config_dir() / GATEWAY_ID_FILE).read_text().strip()
        assert stored == first

    def test_a_repeat_read_does_not_touch_the_disk(self, tmp_path, monkeypatch):
        """`/api/health` is the most polled endpoint there is and the id cannot
        change, so only the first resolve may pay for the file."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.config import loader

        loader._invalidate_config_cache()
        from kiro_crew import gateway_identity

        gateway_identity._CACHED_IDS.clear()
        first = gateway_identity.gateway_id()
        assert len(first) == 32

        reads: list[str] = []

        def counting_read(path):
            reads.append(str(path))
            return ""

        monkeypatch.setattr(gateway_identity, "_read_id", counting_read)
        assert gateway_identity.gateway_id() == first
        assert reads == [], "re-read the id file for a value that cannot change"

    def test_a_corrupt_file_is_replaced(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.config import loader

        loader._invalidate_config_cache()
        from kiro_crew.gateway_identity import GATEWAY_ID_FILE, gateway_id

        path = loader.config_dir()
        path.mkdir(parents=True, exist_ok=True)
        (path / GATEWAY_ID_FILE).write_text("not-a-uuid\n")
        fresh = gateway_id()
        assert len(fresh) == 32 and fresh != "not-a-uuid"

    def test_reading_without_create_does_not_mint(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.config import loader

        loader._invalidate_config_cache()
        from kiro_crew.gateway_identity import GATEWAY_ID_FILE, gateway_id

        assert gateway_id(create=False) == ""
        assert not (loader.config_dir() / GATEWAY_ID_FILE).exists()

    def test_two_homes_get_different_ids(self, tmp_path, monkeypatch):
        """Two gateways sharing a machine but not a data home are two gateways,
        which is precisely the distinction the cycle guard needs."""
        from kiro_crew.config import loader
        from kiro_crew.gateway_identity import gateway_id

        ids = []
        for name in ("home-a", "home-b"):
            monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / name))
            loader._invalidate_config_cache()
            ids.append(gateway_id())
        assert ids[0] != ids[1]


class TestChainedRepairPaths:
    """The three paths that share `connect`'s repairs and did not have them."""

    def _pair(self, tmp_path, monkeypatch, *, hop=53999):
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", remote_port=5476, instance_id="b")
        reg.add(
            name="C",
            ssh_host="c-host",
            remote_port=5476,
            instance_id="c",
            via_instance_id="b",
            via_remote_port=hop,
            via_remote_id=VIA_ID,
        )

        async def no_cycle(_inst, _port):
            return ""

        monkeypatch.setattr(mgr, "_chain_cycle_reason", no_cycle)
        return reg, mgr

    def test_self_heal_mints_before_tier_one_so_a_succeeding_rebuild_dials_the_reported_hop(
        self, tmp_path, monkeypatch
    ):
        """A parent that came back on a different loopback port is the commonest
        reason a chained crew needs healing at all -- and `ssh -L` binds the LOCAL
        side whatever answers remotely, so a rebuild against the row's stale hop
        starts fine and `start()` reports success. Tier 1 therefore marks the crew
        recovered and returns without ever asking the parent where its forward
        listens, leaving it healthy on the board and unreachable in fact. The mint
        rides the parent's own hop, so it runs first.
        """
        reg, mgr = self._pair(tmp_path, monkeypatch)
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr))
        assert asyncio.run(mgr.connect("c")).state.value == "connected"

        # The parent reconnected and serves this crew on a different port.
        minted: list[str] = []
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr, port=54321, seen=minted))

        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        mgr._tunnels["c"].status.state = TunnelState.ERROR
        _FakeTunnel.made = []
        asyncio.run(mgr._recover("c"))

        rebuilt = [t for t in _FakeTunnel.made if t.iid == "c"]
        assert len(rebuilt) == 1, "tier 1's rebuild is the one that has to dial the new hop"
        assert rebuilt[0].remote_port == 54321, "tier 1 healed onto the stale hop port"
        assert minted == ["c"], "tier 1 ran without asking the parent for its port"
        # And the parent's answer is persisted, so a later reconnect agrees.
        assert reg.get("c").via_remote_port == 54321
        # The credential is stored only AFTER the forward rides the hop it was
        # minted for -- deferred, not dropped.
        assert mgr.get_token("c") == "CHAINED_TOKEN"

    def test_self_heal_tier_two_rebuilds_without_minting_again(self, tmp_path, monkeypatch):
        """A chained crew's token is minted at the top of the recovery, so tier 2
        is the rebuild alone -- the same shape fargate's tier 2 has, for the
        opposite reason. Both of its rebuilds dial the port the parent named.
        """
        reg, mgr = self._pair(tmp_path, monkeypatch)
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr))
        assert asyncio.run(mgr.connect("c")).state.value == "connected"

        minted: list[str] = []
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr, port=54321, seen=minted))

        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        fails = {"n": 1}
        real_start = _FakeTunnel.start

        async def start_failing_once(self):
            if fails["n"] > 0:
                fails["n"] -= 1
                self.start_result = False
            return await real_start(self)

        monkeypatch.setattr(_FakeTunnel, "start", start_failing_once)

        mgr._tunnels["c"].status.state = TunnelState.ERROR
        _FakeTunnel.made = []
        asyncio.run(mgr._recover("c"))

        rebuilt = [t for t in _FakeTunnel.made if t.iid == "c"]
        assert len(rebuilt) == 2, "did not reach tier 2 (tier 1's rebuild succeeded)"
        assert [t.remote_port for t in rebuilt] == [54321, 54321]
        assert minted == ["c"], "tier 2 minted a second time for one recovery"
        assert reg.get("c").via_remote_port == 54321

    def test_self_heal_stands_down_when_the_parent_names_no_hop(self, tmp_path, monkeypatch):
        """Dialling the row's port when the parent names none is the exposure
        `connect` refuses: that copy arrived over a pane's postMessage, so it is
        not allowed to choose which loopback-only service on the parent this
        gateway forwards and renders as a crew.
        """
        reg, mgr = self._pair(tmp_path, monkeypatch)
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr))
        assert asyncio.run(mgr.connect("c")).state.value == "connected"

        async def mint_naming_no_port(inst, _params):
            from kiro_crew.instances.ssh_tunnel_manager import _Mint

            return _Mint(token="CHAINED_TOKEN", hop=0, ttl=inst.ttl)

        monkeypatch.setattr(mgr, "_mint_through_parent", mint_naming_no_port)

        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        mgr._tunnels["c"].status.state = TunnelState.ERROR
        _FakeTunnel.made = []
        asyncio.run(mgr._recover("c"))

        assert [t for t in _FakeTunnel.made if t.iid == "c"] == [], "dialled an unnamed hop"
        assert reg.get("c").via_remote_port == 53999

    def test_a_refresh_for_a_hop_the_parent_has_moved_off_is_discarded(self, tmp_path, monkeypatch):
        """The exit this invariant was found on. The crew holding the hop moves this
        crew to another loopback port and is free to give the old one to a different
        crew. Our forward still dials the old port, so storing the credential the
        refresh just minted would deliver THIS crew's bearer token over a forward
        that now reaches that other crew -- and nothing downstream re-checks it.
        Neither the generation nor membership can see it: our own tunnel was never
        touched, which is what made the earlier guards miss it.
        """
        reg, mgr = self._pair(tmp_path, monkeypatch)
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr))
        assert asyncio.run(mgr.connect("c")).state.value == "connected"
        first = mgr.get_token("c")
        assert first and mgr.status("c").remote_port == 53999

        # The parent now serves this crew on a different port, and the refresh's
        # mint is where it says so. Our forward is untouched and still dials 53999.
        monkeypatch.setattr(
            mgr, "_mint_through_parent", _relay_mint(mgr, token="MOVED", port=54321)
        )

        assert asyncio.run(mgr._refresh_token_once("c")) is False, "stored a moved-hop mint"
        assert mgr.get_token("c") == first, "overwrote the valid credential with the moved one"
        assert mgr.status("c").remote_port == 53999, "the forward was never rebuilt here"

    def test_a_first_chained_connect_stores_the_parents_shorter_lifetime(
        self, tmp_path, monkeypatch
    ):
        """`_chained_ttl` is written only by the store and cleared on every teardown,
        so it is empty on every connect and every reconnect. A caller that computed
        the effective lifetime for the store would therefore read it before its own
        mint had reached it, take our row's 20h default, and schedule the refresh
        past a shorter parent-issued expiry -- which re-mints, re-derives the iframe
        src and remounts the pane, losing whatever was unsaved in it.
        """
        reg, mgr = self._pair(tmp_path, monkeypatch)
        # The parent issues an hour; our row says twenty.
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr, ttl="1h"))
        assert reg.get("c").ttl == "20h", "fixture no longer contrasts the two numbers"

        assert asyncio.run(mgr.connect("c")).state.value == "connected"

        assert mgr.token_ttl_total("c") == 3600, "stored our row's lifetime, not the token's"

        # And again after a teardown, because the dict is cleared with the tunnel.
        asyncio.run(mgr.disconnect("c"))
        assert "c" not in mgr._chained_ttl
        assert asyncio.run(mgr.connect("c")).state.value == "connected"
        assert mgr.token_ttl_total("c") == 3600, "the reconnect took our row's lifetime"

    def test_the_store_is_the_one_place_a_moved_hop_is_refused(self, tmp_path, monkeypatch):
        """The enforcement point itself, at the unit. Every credential store goes
        through `_store_token`, so the comparison lives there once instead of at
        each exit -- an exit added later cannot forget it, and `minted_at_epoch` is
        required so a caller cannot store without saying what it minted against.
        """
        reg, mgr = self._pair(tmp_path, monkeypatch)
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr))
        assert asyncio.run(mgr.connect("c")).state.value == "connected"
        inst = reg.get("c")
        epoch = mgr._tunnel_epoch["c"]

        # Agreeing: stored.
        assert (
            mgr._store_token(inst, _Mint("AGREE"), minted_at_epoch=epoch, binds_forward=False)
            is True
        )
        assert mgr.get_token("c") == "AGREE"

        # The parent reports a port this forward does not dial: discarded, and the
        # credential already held survives -- a stale token yields a 403 the client
        # recovers from, where a delivered one is a disclosure that is not undone.
        assert (
            mgr._store_token(
                inst, _Mint("MOVED", hop=54321), minted_at_epoch=epoch, binds_forward=False
            )
            is False
        )
        # Discarded, and that is ALL. A port-only disagreement is the parent naming a
        # hop we do not dial, which the connect path also produces while a forward is
        # being built -- so retiring here would tear down the very tunnel being set
        # up. Only the identity branch, which can only mean a hop that changed hands
        # behind an unchanged number, asks for the forward to go.
        assert "c" in mgr._tunnels, "retired a forward over a port-only disagreement"
        assert mgr.get_token("c") == "AGREE"

        # A generation that moved, with the hop agreeing again.
        assert (
            mgr._store_token(inst, _Mint("OLD"), minted_at_epoch=epoch - 1, binds_forward=False)
            is False
        )
        assert mgr.get_token("c") == "AGREE"

    def test_every_credential_store_routes_through_the_one_decision(self):
        """Structural, because behaviour cannot see it: a later exit must not be able
        to store a credential, or publish the hop the check reads, without the
        comparison. Three properties -- every `_store_token` call names the
        generation it minted against; the decision is reached only from the store
        and from the parent-side mint, which is the one exit that RETURNS a
        credential; and the two published values are WRITTEN only inside the store,
        so the mint cannot put a superseded hop where the comparison will read it.
        """
        import ast
        import pathlib

        src = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src/kiro_crew/instances/ssh_tunnel_manager.py"
        ).read_text()
        tree = ast.parse(src)

        stores, deciders = [], []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "_store_token":
                stores.append((node.lineno, {k.arg for k in node.keywords}))
            if node.func.attr == "_credential_forward_moved":
                deciders.append(node.lineno)

        assert len(stores) >= 5, f"expected every store site, found {len(stores)}"
        missing = [ln for ln, kw in stores if "minted_at_epoch" not in kw]
        assert not missing, f"_store_token called without minted_at_epoch at lines {missing}"

        def owners_of(pred):
            found = set()
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if pred(node):
                        found.add(fn.name)
            return found

        # `_answer_embed_mint` IS the embed mint's second half: the mint takes seconds
        # and must not hold the manager lock, so the validation and the hop reservation
        # run there, under it, as one step. Two names, still one reader per exit.
        assert owners_of(
            lambda n: isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_credential_forward_moved"
        ) == {"_store_token", "_answer_embed_mint"}, "the one decision is reached from elsewhere"

        # And the mint's locked half is only ever ENTERED under the lock. A unit test
        # cannot observe a missing lock -- nothing here runs concurrently -- so the
        # structure is what holds it: validating the hop and recording it as lent are
        # one critical section, and a call from outside one would put the gap back.
        locked_calls = 0
        loose_calls = []
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]:
            for node in ast.walk(fn):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_answer_embed_mint"
                ):
                    continue
                under_lock = any(
                    isinstance(outer, ast.AsyncWith)
                    and any(
                        isinstance(item.context_expr, ast.Attribute)
                        and item.context_expr.attr == "_lock"
                        for item in outer.items
                    )
                    and node in list(ast.walk(outer))
                    for outer in ast.walk(fn)
                )
                if under_lock:
                    locked_calls += 1
                else:
                    loose_calls.append(fn.name)
        assert locked_calls == 1, f"expected one locked call, found {locked_calls}"
        assert not loose_calls, f"_answer_embed_mint called outside the lock in {loose_calls}"

        # The WRITE side. A subscript store into either published dict, anywhere but
        # the fenced store, is how a superseded mint poisoned the comparison's own
        # reference; `pop` is excluded because the teardown must still clear them.
        published = {"_chained_hop_port", "_chained_ttl", "_chained_hop_identity"}

        def is_publish(n):
            if not isinstance(n, ast.Assign):
                return False
            for tgt in n.targets:
                if (
                    isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Attribute)
                    and tgt.value.attr in published
                ):
                    return True
            return False

        writers = owners_of(is_publish)
        assert writers == {"_store_token"}, f"the published hop is written from {sorted(writers)}"

        # And the ORDERING: the effective lifetime depends on the publish above, so a
        # caller reading it would read the dict before its own mint reached it.
        readers = owners_of(
            lambda n: isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_minted_ttl"
        )
        assert readers == {"_store_token"}, f"the lifetime is computed in {sorted(readers)}"

        # The IDENTITY may only be read where the decision is made, so no site can
        # reach a credential by consulting it somewhere the comparison does not run.
        id_readers = owners_of(
            lambda n: isinstance(n, ast.Attribute) and n.attr == "_chained_hop_identity"
        )
        assert id_readers == {
            "__init__",
            "_store_token",
            "_credential_forward_moved",
            "_teardown_locked",
        }, f"the hop identity is touched in {sorted(id_readers)}"

        # And every store declares which question it asks.
        no_binds = [ln for ln, kw in stores if "binds_forward" not in kw]
        assert not no_binds, f"_store_token called without binds_forward at {no_binds}"

    def test_a_heal_that_never_lands_stores_no_credential_for_the_new_hop(
        self, tmp_path, monkeypatch
    ):
        """The ordering, which no other case can see. The mint learns the parent's
        new port, but if neither rebuild ever gets a forward onto that port then
        nothing here ever rides the hop this credential was minted for -- so it must
        not be stored at all, and the credential already held stays. A store placed
        before the rebuild looks identical in every test where the rebuild succeeds.
        """
        reg, mgr = self._pair(tmp_path, monkeypatch)
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr))
        assert asyncio.run(mgr.connect("c")).state.value == "connected"
        first = mgr.get_token("c")
        assert first == "CHAINED_TOKEN"

        monkeypatch.setattr(
            mgr, "_mint_through_parent", _relay_mint(mgr, token="MOVED", port=54321)
        )

        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        # Every rebuild fails, so no forward ever dials the port the parent named.
        async def never_starts(self):
            self.status.state = TunnelState.ERROR
            self.status.error = "boom"
            return False

        monkeypatch.setattr(_FakeTunnel, "start", never_starts)
        mgr._tunnels["c"].status.state = TunnelState.ERROR
        asyncio.run(mgr._recover("c"))

        assert mgr.get_token("c") == first, "stored a credential no forward ever rode"

    def test_the_same_port_naming_a_different_hop_identity_is_discarded(
        self, tmp_path, monkeypatch
    ):
        """The case a port-number comparison cannot see, and the one the finding is
        about. The crew holding the hop tears its forward down and rebuilds it, and
        its allocator hands out the SAME loopback number -- which is what allocators
        do, first free above the base. Every number agrees; the forward on the far
        side is not the one this crew's credential was minted against.
        """
        reg, mgr = self._pair(tmp_path, monkeypatch)
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr, hop_gen=1))
        assert asyncio.run(mgr.connect("c")).state.value == "connected"
        first = mgr.get_token("c")
        assert mgr._chained_hop_identity["c"] == (VIA_ID, 1)

        # Same port, same crew id, NEW generation of the parent's own forward.
        monkeypatch.setattr(
            mgr, "_mint_through_parent", _relay_mint(mgr, token="REUSED", hop_gen=2)
        )

        async def refresh_then_retire():
            refreshed = await mgr._refresh_token_once("c")
            # The retirement is SCHEDULED, because the store runs both inside and
            # outside the manager lock and awaiting a teardown from under it would
            # park forever. Drained here so the assertion is deterministic rather
            # than a race with the loop closing.
            if mgr._retirements:
                await asyncio.gather(*list(mgr._retirements))
            return refreshed

        assert (
            asyncio.run(refresh_then_retire()) is False
        ), "stored a credential for a forward rebuilt behind the same port number"
        assert first and first != "REUSED", "fixture no longer contrasts the two tokens"

        # Refusing the new credential is not enough: the forward and the token
        # ALREADY in place are the ones riding the reused port, so both go. Leaving
        # them is what let one crew's token keep reaching another crew's gateway.
        # `get_token` answers with an empty string, not None, for a crew it has no
        # credential for.
        assert not mgr.get_token("c"), "kept a credential for a hop that changed hands"
        assert "c" not in mgr._tunnels, "kept the forward riding the reused port"
        assert "c" not in mgr._chained_hop_identity, "kept the retired hop's identity"

        # The user still WANTS the crew, so the intent survives and an ordinary
        # reconnect can bring it back on a hop that is actually ours. A security
        # teardown must not read as the user having turned the crew off.
        assert reg.get("c").was_connected is True, "cleared the user's own intent"

    def test_a_stale_mint_arriving_after_a_rebuild_does_not_retire_it(self, tmp_path, monkeypatch):
        """The boundary on retirement, and the reason only the identity branch sets it.

        A mint taken without the lock can finish AFTER this gateway tore its own
        forward down and rebuilt it -- that is what the generation stamp exists to
        catch, and its credential is correctly discarded. But discarding is all that
        may happen: the forward now up is the rebuilt one, which is healthy and is
        NOT riding a hop that changed hands. Retiring on a superseded generation
        would tear down exactly what a self-heal had just repaired, and would do it
        every time a slow mint lost a race.
        """
        reg, mgr = self._pair(tmp_path, monkeypatch)
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr, hop_gen=1))
        assert asyncio.run(mgr.connect("c")).state.value == "connected"
        healthy = mgr.get_token("c")
        stale_epoch = mgr._tunnel_epoch.get("c", 0)

        # The rebuild: a teardown ends the generation, and something reinstalls.
        mgr._tunnel_epoch["c"] = stale_epoch + 1

        async def store_stale():
            # The mint that was in flight across the rebuild, named for the OLD
            # generation and carrying the identity it was built against.
            stored = mgr._store_token(
                reg.get("c"),
                _Mint("STALE", hop=reg.get("c").via_remote_port, hop_id=VIA_ID, hop_gen=1),
                minted_at_epoch=stale_epoch,
                binds_forward=False,
            )
            if mgr._retirements:
                await asyncio.gather(*list(mgr._retirements))
            return stored

        assert asyncio.run(store_stale()) is False, "stored a credential for a dead generation"
        assert "c" in mgr._tunnels, "retired the forward a rebuild had just put up"
        assert mgr.get_token("c") == healthy, "dropped the rebuilt forward's own credential"

    def test_a_rebuild_onto_the_reported_hop_rebinds_the_identity(self, tmp_path, monkeypatch):
        """The other direction, so the comparison is not simply a refusal. A self-heal
        rebuilds this gateway's forward onto the hop the mint named, so that store IS
        the one defining the identity and must land -- otherwise a parent that ever
        restarts could never be healed onto again.
        """
        reg, mgr = self._pair(tmp_path, monkeypatch)
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr, hop_gen=1))
        assert asyncio.run(mgr.connect("c")).state.value == "connected"

        monkeypatch.setattr(
            mgr,
            "_mint_through_parent",
            _relay_mint(mgr, token="HEALED", port=54321, hop_gen=7),
        )

        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        mgr._tunnels["c"].status.state = TunnelState.ERROR
        _FakeTunnel.made = []
        asyncio.run(mgr._recover("c"))

        assert mgr.get_token("c") == "HEALED", "the rebuild's own store was refused"
        assert mgr._chained_hop_identity["c"] == (VIA_ID, 7), "the identity did not rebind"

    def test_a_reply_that_cannot_identify_the_hop_is_refused(self, tmp_path, monkeypatch):
        """Required with the lifetime's strictness, not dropped with the port's. A port
        that cannot be read is dropped and the caller refuses to dial; an identity that
        cannot be read would let the dial succeed and weaken only the comparison
        guarding the credential -- which is the whole finding. The endpoint and these
        fields ship in one change, so a parent that answers this route at all has them.
        """
        from kiro_crew.instances import ssh_tunnel_manager as stm
        from kiro_crew.instances.token_mint import TokenMintError

        reg, mgr = self._pair(tmp_path, monkeypatch)
        inst = reg.get("c")
        params = mgr._resolve_chained_transport(inst, reg.get("b"))
        monkeypatch.setattr(stm.aiohttp, "ClientSession", _FakeMintSession)
        monkeypatch.setattr(
            mgr, "_peer_target", lambda _pid, path: (f"http://127.0.0.1:1{path}", "kc")
        )
        monkeypatch.setattr(mgr, "_peer_cookie_header", lambda _pid, _name: {})

        base = {"token": "T", "port": 4242, "ttl": "2h", "hop_id": "c-2", "hop_gen": 3}
        for missing in ("hop_id", "hop_gen"):
            bad = dict(base)
            del bad[missing]
            _FakeMintSession.reply = bad
            with pytest.raises(TokenMintError):
                asyncio.run(mgr._mint_through_parent(inst, params))

        for field, value in (
            ("hop_id", ""),
            ("hop_id", 7),
            ("hop_gen", "3"),
            ("hop_gen", True),
            ("hop_gen", -1),
            ("hop_gen", 1.5),
        ):
            bad = dict(base)
            bad[field] = value
            _FakeMintSession.reply = bad
            with pytest.raises(TokenMintError):
                asyncio.run(mgr._mint_through_parent(inst, params))

        _FakeMintSession.reply = dict(base)
        assert asyncio.run(mgr._mint_through_parent(inst, params)).hop_gen == 3

    def test_editing_a_parent_tears_down_the_crews_riding_it(self, tmp_path, monkeypatch):
        """A child's forward was built from the parent's coordinates. Moving the
        parent to another machine while the child's `ssh -L ... <old host>` stays up
        leaves that child reporting CONNECTED and serving the machine the user just
        left, while its row names the new one.
        """
        reg, mgr = self._pair(tmp_path, monkeypatch)
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr))
        assert asyncio.run(mgr.connect("b")).state.value == "connected"
        assert asyncio.run(mgr.connect("c")).state.value == "connected"
        assert "c" in mgr._tunnels and "b" in mgr._tunnels

        asyncio.run(mgr.reconfigure("b", lambda: reg.update("b", ssh_host="b-moved")))

        assert "b" not in mgr._tunnels, "kept the edited crew's own forward"
        assert "c" not in mgr._tunnels, "left a chained crew forwarding to the old host"
        # The child keeps its intent: the user edited the PARENT, and forgetting the
        # child would mean reconnecting the parent does not bring its crews back.
        assert reg.get("c").was_connected is True

    def test_a_child_that_will_not_stop_aborts_the_parent_s_edit(self, tmp_path, monkeypatch):
        """Same reason the parent's own failed stop aborts it: persisting new
        coordinates while a forward built from the old ones is still live leaves the
        record describing one machine and the reachable tunnel serving another.
        """
        reg, mgr = self._pair(tmp_path, monkeypatch)
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr))
        asyncio.run(mgr.connect("b"))
        asyncio.run(mgr.connect("c"))

        async def wont_stop():
            raise RuntimeError("stop refused")

        mgr._tunnels["c"].stop = wont_stop

        with pytest.raises(Exception):
            asyncio.run(mgr.reconfigure("b", lambda: reg.update("b", ssh_host="b-moved")))

        assert reg.get("b").ssh_host == "b-host", "persisted the edit over a live child"

    def test_a_superseded_parent_hop_remint_is_discarded(self, tmp_path, monkeypatch):
        """Two of the three callers reach this without the manager lock, and the
        mint budget is tens of seconds, so the parent can be disconnected and
        reconnected inside it. Membership cannot see that -- the NEW tunnel
        satisfies it -- so the fresh credential would be overwritten by one minted
        against the generation before it. The tunnel generation is what sees it.
        """
        reg, mgr = self._pair(tmp_path, monkeypatch)
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr))
        assert asyncio.run(mgr.connect("b")).state.value == "connected"
        live = mgr.get_token("b")

        async def mint_then_replace(inst, _params):
            # Stand in for a reconnect landing while the mint is in flight.
            mgr._tunnel_epoch["b"] = mgr._tunnel_epoch.get("b", 0) + 1
            return "STALE_GENERATION_TOKEN"

        monkeypatch.setattr(mgr, "_mint_for", mint_then_replace)
        assert asyncio.run(mgr._remint_parent_under_lock("b")) is False
        assert mgr.get_token("b") == live, "overwrote a live credential with a stale one"

    def test_an_undisturbed_parent_hop_remint_still_stores(self, tmp_path, monkeypatch):
        """The guard must not be over-eager: with the generation unchanged the
        re-mint is the whole point of the 401 branch and must land.
        """
        reg, mgr = self._pair(tmp_path, monkeypatch)
        monkeypatch.setattr(mgr, "_mint_through_parent", _relay_mint(mgr))
        asyncio.run(mgr.connect("b"))
        first = mgr.get_token("b")

        async def quiet_mint(_inst, _params):
            return _Mint("FRESH_TOKEN")

        monkeypatch.setattr(mgr, "_mint_for", quiet_mint)
        assert asyncio.run(mgr._remint_parent_under_lock("b")) is True
        assert mgr.get_token("b") == "FRESH_TOKEN" != first


class TestTheRefreshScheduleFollowsTheLifetimeItStored:
    """The proactive refresh interval is re-derived, not remembered.

    ``_token_ttl_secs`` is rewritten by every store, and for a chained crew it holds
    the SHORTER of the parent's answer and our own row — so the parent lowering its
    TTL changes it underneath a running loop. ``ttl`` is not a transport field, so
    that edit tears no tunnel down and restarts nothing. An interval computed once
    would therefore keep sleeping the old, longer gap while the token it protects
    died early, reloading the crew's pane and discarding whatever was unsaved in it —
    on every cycle from then on, not once.
    """

    def _chained(self, tmp_path, monkeypatch, *, row_ttl="20h"):
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=53999,
            via_remote_id=VIA_ID,
            ttl=row_ttl,
        )
        return reg, mgr

    @staticmethod
    def _parent_answering(mgr, answer: dict[str, str]):
        """A parent whose TTL answer can be changed between cycles."""

        async def fake(inst, _params):
            return _Mint(
                token="CHAINED_TOKEN",
                hop=inst.via_remote_port,
                ttl=answer["ttl"],
                hop_id=inst.via_remote_id,
                hop_gen=1,
            )

        return fake

    @staticmethod
    def _intervals(mgr, monkeypatch, *, cycles: int, on_cycle=None) -> list[float]:
        """Run the REAL loop and return the interval it asked to sleep each cycle.

        Sleeps are recorded and skipped rather than waited out, and the loop is
        stopped by cancelling it once it has asked ``cycles`` times — the same way
        disconnect stops it in production.
        """
        seen: list[float] = []
        real_sleep = asyncio.sleep

        async def record(delay, *args, **kwargs):
            seen.append(delay)
            if len(seen) >= cycles:
                raise asyncio.CancelledError
            if on_cycle is not None:
                on_cycle(len(seen))
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", record)

        async def run():
            with contextlib.suppress(asyncio.CancelledError):
                await mgr._token_refresh_loop("c")

        asyncio.run(run())
        return seen

    def test_a_parent_lowering_its_ttl_shortens_the_next_interval(self, tmp_path, monkeypatch):
        """The finding's own case: 20h becomes 1h, and the next wake must follow it."""
        reg, mgr = self._chained(tmp_path, monkeypatch)
        answer = {"ttl": "20h"}
        monkeypatch.setattr(mgr, "_mint_through_parent", self._parent_answering(mgr, answer))
        assert asyncio.run(mgr.connect("c")).state.value == "connected"
        assert mgr.token_ttl_total("c") == 20 * 3600

        # The operator lowers the parent's record for this crew after the first wake.
        def lower(cycle):
            if cycle == 1:
                answer["ttl"] = "1h"

        seen = self._intervals(mgr, monkeypatch, cycles=2, on_cycle=lower)

        assert len(seen) == 2, f"loop did not reach a second cycle: {seen}"
        assert seen[0] == pytest.approx(20 * 3600 * 0.8), "first interval was not the 20h one"
        assert mgr.token_ttl_total("c") == 3600, "the refresh did not store the shorter life"
        assert seen[1] == pytest.approx(3600 * 0.8), (
            f"second wake kept the 20h schedule ({seen[1]}s) after the token's life "
            f"fell to 1h -- the token dies {seen[1] - 3600:.0f}s before it"
        )

    def test_the_interval_never_outlives_the_token_it_protects(self, tmp_path, monkeypatch):
        """Stated as the property rather than the numbers, so a changed refresh
        fraction or a different pair of TTLs cannot make it vacuously true."""
        reg, mgr = self._chained(tmp_path, monkeypatch)
        answer = {"ttl": "20h"}
        monkeypatch.setattr(mgr, "_mint_through_parent", self._parent_answering(mgr, answer))
        assert asyncio.run(mgr.connect("c")).state.value == "connected"

        def lower(cycle):
            answer["ttl"] = "1h" if cycle == 1 else "30m"

        seen = self._intervals(mgr, monkeypatch, cycles=3, on_cycle=lower)

        assert len(seen) == 3, f"loop did not reach a third cycle: {seen}"
        # Each wake after the first must land inside the life of the token the
        # previous wake minted, or the pane reloads before the refresh arrives.
        assert seen[1] < 3600, f"wake at {seen[1]}s is past a 1h token"
        assert seen[2] < 1800, f"wake at {seen[2]}s is past a 30m token"

    def test_a_failed_refresh_keeps_its_interval(self, tmp_path, monkeypatch):
        """The distinction the re-derivation must not break. A failed mint stores no
        new lifetime, so the retry stays on the interval it was already using; making
        failure reschedule would turn a network blip into a changed schedule.
        """
        from kiro_crew.instances.token_mint import TokenMintError

        reg, mgr = self._chained(tmp_path, monkeypatch)
        answer = {"ttl": "20h"}
        monkeypatch.setattr(mgr, "_mint_through_parent", self._parent_answering(mgr, answer))
        assert asyncio.run(mgr.connect("c")).state.value == "connected"

        async def blip(_inst, _params):
            raise TokenMintError("timed out")

        def fail_from_now(cycle):
            if cycle == 1:
                monkeypatch.setattr(mgr, "_mint_through_parent", blip)

        seen = self._intervals(mgr, monkeypatch, cycles=2, on_cycle=fail_from_now)

        assert len(seen) == 2, f"a transport error ended the loop: {seen}"
        assert seen[1] == pytest.approx(seen[0]), "a failed refresh moved the schedule"
