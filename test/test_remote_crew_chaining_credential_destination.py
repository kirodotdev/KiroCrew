"""Where a chained crew's credential may go: only to the crew its forward is built for.

A forged ``ssh_host`` on a chained row is never dialled: the resolved transport and the spawned
forward both carry the parent's host and the hop port, and ``_resolve_transport`` returns from
its chained branch before it reads the row's own host. ``gateway_id`` is stable and persisted,
answered from memory after its first read, replaced when its file is corrupt, not minted by a
read without create, and distinct per data home. A peer request revalidates the forward it
resolved once the credential await returns, so a forward that moves, or is rebuilt onto the
same port, is not spent on while one that stays put is; source pins keep every peer-target
resolver revalidating and every ``_peer_cookie_header`` call awaited. The parent states the id
its own forward reaches when it mints, and a stated id that is not a string reads as absent.
The far end's id is recorded when a chained forward is built, never for a top-level one, which
is not probed, and dropped on disconnect and shutdown; it must match before the browser
handout, the token-carrying liveness probe, a cached session or the peer link exchange goes
out. A listener holding the hop before the build, a squatter on a freed port, a far end
answering an id where the parent sees none or none where it names one, a dead forward and a
forwarder that exits during the probe get nothing, and a squatter found at the handout retires
the forward with the user's intent kept; an unreachable far end withholds without retiring, a
forward replaced during the probe is not retired, a matching far end lets the credential
through, and a crew that reports no id at both ends stays ungated. The id is read over real
loopback relays standing in for two ``ssh -L`` hops; a probe with no answer is refused at
connect rather than read as a crew with no id; a parent whose own probe gets none refuses to
mint, its locked answer taking the identity as a required argument; and the health reply is
read three ways, as an id, as a crew that reports no id, or as no answer. A topic of the
``test_remote_crew_chaining`` family: ``test_remote_crew_chaining.py`` keeps the data model and
its chain walks, the forward, the token path and the cycle guard, and the other siblings are
``test_remote_crew_chaining_subtree_removal.py``,
``test_remote_crew_chaining_route_admission.py``,
``test_remote_crew_chaining_lent_hop_lease.py`` and
``test_remote_crew_chaining_chained_refresh_and_repair.py``, with the shared fakes in
``remote_crew_chaining_helpers.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket

import pytest
from remote_crew_chaining_helpers import (
    VIA_ID,
    _fake_base_port,
    _FakeMintReader,
    _FakeMintSession,
    _FakeTunnel,
    _mgr,
    _no_cookie_header,
    _relay_mint,
)

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
            _p.Path(__file__).resolve().parents[1]
            / "src/kiro_crew/instances/ssh_tunnel_manager_transport.py"
        ).read_text(encoding="utf-8")
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


class TestEveryPeerCallAwaitsItsCredential:
    """Pinned over the SOURCE, because the doubles in this file cannot see it.

    The credential header builder is a coroutine function, and this file replaces it
    with a stand-in at nine sites. A call that forgot to await it would therefore pass
    every behavioural case here while assigning into a coroutine at run time, failing
    the first real chained mint. The type checker catches that too; this makes it fail
    in the suite as well, where a reader of the mint path will see it.
    """

    @staticmethod
    def _manager_source() -> str:
        import inspect
        import sys

        from kiro_crew.instances import ssh_tunnel_manager

        return "".join(
            inspect.getsource(sys.modules[name])
            for name in (ssh_tunnel_manager.__name__, *ssh_tunnel_manager._OWNERS)
        )

    def test_the_credential_builder_is_a_coroutine_function(self):
        import inspect

        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        assert inspect.iscoroutinefunction(
            SshTunnelManager._peer_cookie_header
        ), "the builder is no longer async, so the await pin below tests nothing"

    def test_no_call_site_reads_it_without_awaiting(self):
        # Needle assembled from parts so this assertion's own source cannot be the
        # first offender it reports.
        call = "self." + "_peer_cookie_header" + "("
        awaited = "await " + call
        lines = [
            (n, line.strip())
            for n, line in enumerate(self._manager_source().splitlines(), 1)
            if call in line
        ]
        assert lines, "found no call sites at all, so this pin measures nothing"
        unawaited = [(n, text) for n, text in lines if awaited not in text]
        assert not unawaited, f"these call sites read the credential without awaiting: {unawaited}"


class TestAPeerRequestRefusesAForwardThatMovedUnderIt:
    """The window between resolving a peer target and spending the credential on it.

    Every peer call bakes a port into its url AND its port-scoped cookie name, then
    awaits before the request goes out. A teardown inside that window frees the port
    without zeroing it on the status object the tunnel handed out, and the allocator
    gives that exact port to the next connect first -- so the credential can land on
    another crew's forward. Both readings are pinned, because the port alone is
    satisfied by a peer disconnected and reconnected onto the same port.
    """

    @staticmethod
    def _chained(tmp_path, monkeypatch):
        """A connected parent ``b`` on a known port, plus the chained child ``c``."""
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        inst = reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=4242,
            via_remote_id="c-2",
        )
        hop = _fake_base_port() + 7
        tunnel = _FakeTunnel("b", "b-host", hop, 7777)
        tunnel.status.state = TunnelState.CONNECTED
        mgr._tunnels["b"] = tunnel
        mgr._tunnel_epoch["b"] = 4
        mgr._tokens["b"] = "PARENT_LINK"
        assert mgr._peer_target("b", "/x")[1] == f"mc_token_{hop}", (
            "the cookie name must carry the port, or a moved forward would be "
            "indistinguishable from a live one"
        )
        return reg, mgr, inst, tunnel, hop

    def _mint(self, mgr, inst, reg, monkeypatch, during):
        """Run the chained mint with *during* executing inside the credential await."""
        from kiro_crew.instances import ssh_tunnel_manager as stm

        _FakeMintSession.posted = []
        # Set, not inherited: `reply` is a CLASS attribute several cases here rewrite,
        # so the control below would read whichever token ran last in the file.
        _FakeMintSession.reply = {
            "token": "CHILD_TOKEN",
            "port": 4242,
            "ttl": "2h",
            "hop_id": "c-2",
            "hop_gen": 3,
        }
        _FakeMintSession.reply_status = 200
        monkeypatch.setattr(stm.aiohttp, "ClientSession", _FakeMintSession)

        async def moving_header(_pid, _url, _name):
            during()
            return {}

        monkeypatch.setattr(mgr, "_peer_cookie_header", moving_header)
        params = mgr._resolve_chained_transport(inst, reg.get("b"))
        return params

    def test_a_forward_that_moved_during_the_credential_await_is_not_spent_on(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.instances.ssh_tunnel_manager import TokenMintError

        reg, mgr, inst, tunnel, hop = self._chained(tmp_path, monkeypatch)
        params = self._mint(
            mgr,
            inst,
            reg,
            monkeypatch,
            # What a teardown plus the next connect does: this crew's forward now
            # listens somewhere else, and the port the url names belongs to whoever
            # the allocator handed it to.
            during=lambda: setattr(tunnel.status, "local_port", hop + 1),
        )
        with pytest.raises(TokenMintError):
            asyncio.run(mgr._mint_through_parent(inst, params))
        assert _FakeMintSession.posted == [], (
            f"the mint sent the parent's credential to {_FakeMintSession.posted}, a url "
            f"built from port {hop} that this crew's forward had already left"
        )

    def test_a_forward_rebuilt_onto_the_same_port_is_still_refused(self, tmp_path, monkeypatch):
        """The epoch half: the port alone cannot see a rebuild.

        A teardown returns the port to the free set and the allocator hands out the
        first free port above the base, so the reconnect lands on the SAME number.
        Port equality holds while the forward is a different generation, which is the
        reading ``_remint_parent_under_lock`` already relies on.
        """
        from kiro_crew.instances.ssh_tunnel_manager import TokenMintError

        reg, mgr, inst, tunnel, hop = self._chained(tmp_path, monkeypatch)
        params = self._mint(
            mgr,
            inst,
            reg,
            monkeypatch,
            during=lambda: mgr._tunnel_epoch.__setitem__("b", 6),
        )
        assert tunnel.status.local_port == hop, "this case must move only the generation"
        with pytest.raises(TokenMintError):
            asyncio.run(mgr._mint_through_parent(inst, params))
        assert _FakeMintSession.posted == [], (
            "the mint spent the credential on a forward torn down and rebuilt while it "
            "was being resolved; the port matching is what makes that invisible"
        )

    def test_a_forward_that_stayed_put_is_used(self, tmp_path, monkeypatch):
        """The control, so neither case above can pass by refusing everything."""
        reg, mgr, inst, tunnel, hop = self._chained(tmp_path, monkeypatch)
        params = self._mint(mgr, inst, reg, monkeypatch, during=lambda: None)
        mint = asyncio.run(mgr._mint_through_parent(inst, params))
        assert mint.token == "CHILD_TOKEN"
        assert len(_FakeMintSession.posted) == 1, "an unmoved forward must be spent on"
        assert f"127.0.0.1:{hop}/" in _FakeMintSession.posted[0]

    def test_every_peer_call_revalidates_the_forward_it_resolved(self):
        """Over the SOURCE, because four of the five carriers are public API whose
        doubles in this file replace the credential builder outright.

        The five sites resolve a target with ``_peer_target`` and then await. A new
        carrier added without the revalidation is the failure this catches -- it would
        pass every behavioural case here, since nothing in them moves a forward.
        """
        import inspect
        import sys

        from kiro_crew.instances import ssh_tunnel_manager

        source = "".join(
            inspect.getsource(sys.modules[name])
            for name in (ssh_tunnel_manager.__name__, *ssh_tunnel_manager._OWNERS)
        )
        helper = "self." + "_peer_headers_for" + "("
        resolve = "self." + "_peer_target" + "("
        lines = source.splitlines()
        resolvers = [n for n, line in enumerate(lines, 1) if resolve in line]
        guarded = [n for n, line in enumerate(lines, 1) if helper in line]
        # Six resolvers, and every one awaits before spending a credential: the
        # five carriers, and `_prime_peer_session`, whose header build awaits the
        # far-end probe ahead of the exchange.
        assert len(resolvers) == 6, (
            f"the peer-target resolvers moved ({resolvers}); re-derive which of them "
            f"awaits before spending a credential rather than editing this number"
        )
        assert len(guarded) == 6, (
            f"{len(guarded)} of the six peer-target resolvers revalidate the forward "
            f"they resolved: {guarded}"
        )


# ── a chained credential travels only to the crew it was built for ─────────


class TestAChainedCredentialGoesOnlyToTheCrewItWasBuiltFor:
    """A chained forward's local end is a loopback port on the PARENT's host.

    When the parent gateway exits, that port is released while the credential
    for the crew behind it stays valid, and whatever binds the port next answers
    over the same ``ssh -L``. The far end's ``gateway_id`` is recorded when the
    forward is built and must match, exactly, before any credential goes out:
    the browser handout, the liveness probe that carries the token, and the
    peer link exchange.
    """

    def _chained(self, tmp_path, monkeypatch, *, far_end="CREW-C"):
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

        async def no_cycle(_inst, _port):
            return ""

        monkeypatch.setattr(mgr, "_chain_cycle_reason", no_cycle)
        answer = {"id": far_end}
        probed: list[int] = []

        async def peer_id(port):
            probed.append(port)
            return answer["id"]

        monkeypatch.setattr(mgr, "_peer_identity", peer_id)
        exchanged: list[str] = []

        async def exchange(_url, link, _name):
            exchanged.append(link)
            return "SESSION"

        monkeypatch.setattr(mgr, "_exchange_link", exchange)
        return reg, mgr, answer, probed, exchanged

    @staticmethod
    async def _settle(mgr):
        while mgr._retirements:
            # flake-ok: pre-existing unbounded await on the retirement tasks the manager scheduled; each ends in-process, moved verbatim
            await asyncio.gather(*list(mgr._retirements), return_exceptions=True)

    @staticmethod
    def _no_token_probe(monkeypatch):
        """Fail the test if the token-carrying liveness probe is ever sent."""
        from kiro_crew.instances import ssh_tunnel_manager as stm

        sent: list[str] = []

        class _Refuse:
            def __init__(self, *_a, **_k):
                sent.append("session")

            async def __aenter__(self):
                raise AssertionError("the token probe went out to an unverified far end")

            async def __aexit__(self, *_exc):
                return False

        monkeypatch.setattr(stm.aiohttp, "ClientSession", _Refuse)
        return sent

    def test_the_far_end_is_recorded_when_the_forward_is_built(self, tmp_path, monkeypatch):
        _reg, mgr, _answer, _probed, _ex = self._chained(tmp_path, monkeypatch)
        st = asyncio.run(mgr.connect("c"))
        assert st.state.value == "connected"
        assert mgr._chained_far_end == {"c": "CREW-C"}

    def test_a_top_level_forward_records_nothing_and_is_not_probed(self, tmp_path, monkeypatch):
        reg, mgr, _answer, probed, _ex = self._chained(tmp_path, monkeypatch)

        async def go():
            await mgr.connect("b")
            return await mgr.refresh_token("b")

        assert asyncio.run(go())
        assert "b" not in mgr._chained_far_end
        assert probed == [], "probed a forward that rides no hop"
        assert reg is not None

    def test_a_squatter_on_the_freed_port_gets_no_token_from_the_handout(
        self, tmp_path, monkeypatch
    ):
        reg, mgr, answer, _probed, _ex = self._chained(tmp_path, monkeypatch)

        async def go():
            await mgr.connect("c")
            answer["id"] = "SOMEONE-ELSE"
            got = await mgr.refresh_token("c")
            await self._settle(mgr)
            return got

        assert asyncio.run(go()) is None
        assert mgr.status("c") is None, "the forward to the wrong far end was left up"
        assert "c" not in mgr._tokens
        assert "c" not in mgr._chained_far_end
        # Intent kept: the user still wants this crew, on a hop that is ours.
        assert reg.get("c").was_connected is True
        assert "withheld" in (mgr.last_error("c") or "")

    def test_an_unreachable_far_end_withholds_without_retiring(self, tmp_path, monkeypatch):
        """No answer (a probe timeout on a slow link) withholds that one send and
        leaves the forward up for the next attempt."""
        _reg, mgr, answer, _probed, _ex = self._chained(tmp_path, monkeypatch)

        async def go():
            await mgr.connect("c")
            answer["id"] = ""
            got = await mgr.refresh_token("c")
            retirements = len(mgr._retirements)
            await self._settle(mgr)
            answer["id"] = "CREW-C"
            again = await mgr.refresh_token("c")
            return got, retirements, again

        got, retirements, again = asyncio.run(go())
        assert got is None
        assert retirements == 0
        assert mgr.status("c") is not None
        assert again == "CHAINED_TOKEN", "the forward did not recover once the far end answered"

    def test_the_liveness_probe_never_carries_the_token_to_a_squatter(self, tmp_path, monkeypatch):
        _reg, mgr, answer, _probed, _ex = self._chained(tmp_path, monkeypatch)

        async def go():
            await mgr.connect("c")
            port = mgr.status("c").local_port
            token = mgr.get_token("c")
            assert token
            sent = self._no_token_probe(monkeypatch)
            answer["id"] = "SOMEONE-ELSE"
            ok = await mgr.token_validates(port, token)
            await self._settle(mgr)
            return ok, sent

        ok, sent = asyncio.run(go())
        assert ok is False
        assert sent == [], "opened a session to send the token to the squatter"

    def test_the_peer_link_is_not_exchanged_with_a_squatter(self, tmp_path, monkeypatch):
        from kiro_crew.instances.ssh_tunnel_manager import _PeerUnavailable

        _reg, mgr, answer, _probed, exchanged = self._chained(tmp_path, monkeypatch)

        async def go():
            await mgr.connect("c")
            exchanged.clear()
            mgr._peer_sessions.pop("c", None)
            answer["id"] = "SOMEONE-ELSE"
            url, name = mgr._peer_target("c", "api/status")
            with pytest.raises(_PeerUnavailable):
                await mgr._peer_cookie_header("c", url, name)
            await self._settle(mgr)

        asyncio.run(go())
        assert exchanged == [], "the link went to a far end that is not the crew"

    def test_a_cached_session_is_not_sent_to_a_squatter(self, tmp_path, monkeypatch):
        from kiro_crew.instances.ssh_tunnel_manager import _PeerUnavailable

        _reg, mgr, answer, _probed, _ex = self._chained(tmp_path, monkeypatch)

        async def go():
            await mgr.connect("c")
            assert mgr._peer_sessions.get("c"), "the connect did not prime a session"
            answer["id"] = "SOMEONE-ELSE"
            url, name = mgr._peer_target("c", "api/status")
            with pytest.raises(_PeerUnavailable):
                await mgr._peer_cookie_header("c", url, name)
            await self._settle(mgr)

        asyncio.run(go())
        assert "c" not in mgr._peer_sessions

    def test_a_matching_far_end_lets_the_credential_through(self, tmp_path, monkeypatch):
        _reg, mgr, _answer, _probed, exchanged = self._chained(tmp_path, monkeypatch)

        async def go():
            await mgr.connect("c")
            return await mgr.refresh_token("c")

        assert asyncio.run(go()) == "CHAINED_TOKEN"
        assert mgr.status("c") is not None
        assert exchanged, "a verified far end never received the link"

    def test_a_crew_that_reported_no_id_at_build_is_not_gated(self, tmp_path, monkeypatch):
        """A build older than the field cannot be told apart from a squatter, the
        same arrangement the cycle guard already admits."""
        _reg, mgr, answer, _probed, _ex = self._chained(tmp_path, monkeypatch, far_end="")

        async def go():
            await mgr.connect("c")
            answer["id"] = "SOMEONE-ELSE"
            return await mgr.refresh_token("c")

        assert asyncio.run(go()) == "CHAINED_TOKEN"
        assert "c" not in mgr._chained_far_end

    def test_a_dead_forward_withholds_the_credential(self, tmp_path, monkeypatch):
        """The abnormal exit: the forwarder child is gone and the tunnel reads
        ERROR before any teardown has run. Nothing goes out over it."""
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _PeerUnavailable

        _reg, mgr, _answer, _probed, exchanged = self._chained(tmp_path, monkeypatch)

        async def go():
            await mgr.connect("c")
            exchanged.clear()
            mgr._peer_sessions.pop("c", None)
            url, name = mgr._peer_target("c", "api/status")
            mgr._tunnels["c"].status.state = TunnelState.ERROR
            with pytest.raises(_PeerUnavailable):
                await mgr._peer_cookie_header("c", url, name)
            await self._settle(mgr)

        asyncio.run(go())
        assert exchanged == []

    def test_every_exit_path_drops_the_record(self, tmp_path, monkeypatch):
        _reg, mgr, _answer, _probed, _ex = self._chained(tmp_path, monkeypatch)

        async def go():
            await mgr.connect("c")
            await mgr.disconnect("c")
            after_disconnect = dict(mgr._chained_far_end)
            await mgr.connect("c")
            await mgr.shutdown()
            return after_disconnect, dict(mgr._chained_far_end)

        after_disconnect, after_shutdown = asyncio.run(go())
        assert after_disconnect == {}
        assert after_shutdown == {}

    def test_a_forward_replaced_during_the_probe_is_not_retired(self, tmp_path, monkeypatch):
        """The probe awaits; a reconnect landing inside it owns the forward now,
        and an answer read against the old one must not tear the new one down."""
        _reg, mgr, _answer, _probed, _ex = self._chained(tmp_path, monkeypatch)

        async def go():
            await mgr.connect("c")

            async def moved_under_us(_port):
                mgr._tunnel_epoch["c"] = mgr._tunnel_epoch.get("c", 0) + 1
                return "SOMEONE-ELSE"

            monkeypatch.setattr(mgr, "_peer_gateway_id", moved_under_us)
            held = await mgr._chained_far_end_holds("c")
            return held, len(mgr._retirements)

        held, retirements = asyncio.run(go())
        assert held is False
        assert retirements == 0
        assert mgr.status("c") is not None

    def test_the_parent_states_the_gateway_its_own_forward_reaches(self, tmp_path, monkeypatch):
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="C", ssh_host="c-host", instance_id="c")
        asyncio.run(mgr.connect("c"))
        port = mgr.status("c").local_port
        asked: list[int] = []

        async def peer_id(p):
            asked.append(p)
            return "CREW-C"

        monkeypatch.setattr(mgr, "_peer_identity", peer_id)
        ok, payload = asyncio.run(mgr.mint_embed_token("c", 9191))
        assert ok is True
        assert payload["crew_gateway_id"] == "CREW-C"
        assert asked == [port], "probed something other than our own forward to the crew"

    @pytest.mark.parametrize("bad", [None, 7, ["CREW-C"], {"id": "x"}])
    def test_a_non_string_stated_id_is_treated_as_absent(self, tmp_path, monkeypatch, bad):
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
        reply = dict(_FakeMintSession.reply)
        reply["crew_gateway_id"] = bad
        monkeypatch.setattr(_FakeMintSession, "reply", reply)
        monkeypatch.setattr(stm.aiohttp, "ClientSession", _FakeMintSession)
        monkeypatch.setattr(
            mgr, "_peer_target", lambda _pid, path: (f"http://127.0.0.1:1{path}", "kc")
        )
        monkeypatch.setattr(mgr, "_peer_cookie_header", _no_cookie_header)
        params = mgr._resolve_chained_transport(inst, reg.get("b"))
        mint = asyncio.run(mgr._mint_through_parent(inst, params))
        assert mint.crew_gateway_id is None

    def _stated(self, tmp_path, monkeypatch, *, stated, far_end):
        reg, mgr, answer, _probed, exchanged = self._chained(tmp_path, monkeypatch, far_end=far_end)
        relay = _relay_mint(mgr)

        async def stating(inst, params):
            import dataclasses

            return dataclasses.replace(await relay(inst, params), crew_gateway_id=stated)

        monkeypatch.setattr(mgr, "_mint_through_parent", stating)
        return reg, mgr, answer, exchanged

    def test_a_listener_that_took_the_hop_before_the_build_gets_nothing(
        self, tmp_path, monkeypatch
    ):
        """The parent exits between its mint reply and our build: whatever holds
        the port now answers with its own id, which is not the one the parent
        stated, so the forward is refused before anything is primed."""
        _reg, mgr, _answer, exchanged = self._stated(
            tmp_path, monkeypatch, stated="CREW-C", far_end="SQUATTER"
        )
        st = asyncio.run(mgr.connect("c"))
        assert st.state.value == "error"
        assert "withheld" in st.error
        assert mgr.status("c") is None
        assert "c" not in mgr._tokens
        assert "c" not in mgr._chained_far_end
        assert exchanged == [], "the link was primed against the squatter"
        built = [t for t in _FakeTunnel.made if t.iid == "c"]
        assert built and built[0].stopped

    def test_a_silent_far_end_is_refused_when_the_parent_named_one(self, tmp_path, monkeypatch):
        _reg, mgr, _answer, exchanged = self._stated(
            tmp_path, monkeypatch, stated="CREW-C", far_end=""
        )
        st = asyncio.run(mgr.connect("c"))
        assert st.state.value == "error"
        assert exchanged == []

    def test_the_parents_statement_is_what_later_sends_compare_against(self, tmp_path, monkeypatch):
        _reg, mgr, _answer, _ex = self._stated(
            tmp_path, monkeypatch, stated="CREW-C", far_end="CREW-C"
        )
        st = asyncio.run(mgr.connect("c"))
        assert st.state.value == "connected"
        assert mgr._chained_far_end == {"c": "CREW-C"}

    def test_a_parent_that_cannot_tell_leaves_the_crew_ungated(self, tmp_path, monkeypatch):
        """A crew build that reports no id: the parent says so with an empty
        statement, and the arrangement stays what the cycle guard admits."""
        _reg, mgr, _answer, _ex = self._stated(tmp_path, monkeypatch, stated="", far_end="")
        st = asyncio.run(mgr.connect("c"))
        assert st.state.value == "connected"
        assert "c" not in mgr._chained_far_end

    def test_a_forwarder_that_exits_during_the_probe_gets_nothing(self, tmp_path, monkeypatch):
        """The child dies while the identity probe is in flight: the state flips
        to ERROR with the generation unchanged, and whatever then answers on the
        port must not receive the credential on the strength of that answer."""
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        _reg, mgr, _answer, _probed, _ex = self._chained(tmp_path, monkeypatch)

        async def go():
            await mgr.connect("c")

            async def exits_mid_probe(_port):
                mgr._tunnels["c"].status.state = TunnelState.ERROR
                return "CREW-C"

            monkeypatch.setattr(mgr, "_peer_gateway_id", exits_mid_probe)
            return await mgr._chained_far_end_holds("c")

        assert asyncio.run(go()) is False


class TestTheIdentityProbeCrossesARelayedChain:
    """The far-end id is read over real loopback sockets through two byte relays.

    Each relay stands in for one ``ssh -L``: it copies bytes and adds nothing, so
    the crew sees a loopback peer, no forwarding header, and the ``Host`` the hub
    sent. That is the request the crew's ``/api/health`` gate admits, and it is
    the same read the cycle guard already makes over a chained forward.
    """

    @staticmethod
    async def _relay(target_port: int):
        async def pipe(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass
            finally:
                with contextlib.suppress(Exception):
                    writer.close()

        async def handle(reader, writer):
            up_r, up_w = await asyncio.open_connection("127.0.0.1", target_port)
            # flake-ok: pre-existing relay pump that ends when either socket closes, moved verbatim
            await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer))

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        return server, server.sockets[0].getsockname()[1]

    def test_the_crews_id_comes_back_through_two_hops(self, tmp_path, monkeypatch):
        from aiohttp import web

        from kiro_crew.dashboard.handlers import core as core_mod

        monkeypatch.setattr(core_mod, "gateway_id", lambda *_a, **_k: "CREW-C")
        _reg, mgr = _mgr(tmp_path, monkeypatch, real_identity=True)

        async def go():
            app = web.Application()
            app["allowed_origins"] = {"http://localhost:5476", "http://127.0.0.1:5476"}
            app.router.add_get("/api/health", core_mod.api_health)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            crew_port = site._server.sockets[0].getsockname()[1]
            parent_hop, parent_port = await self._relay(crew_port)
            hub_fwd, hub_port = await self._relay(parent_port)
            try:
                return await mgr._peer_gateway_id(hub_port)
            finally:
                hub_fwd.close()
                parent_hop.close()
                await runner.cleanup()

        assert asyncio.run(go()) == "CREW-C"


class TestAProbeWithNoAnswerIsNotACrewWithNoId:
    """A crew too old to report an id answers ``/api/health`` with ``ok`` and
    nothing else; a probe that times out, or a listener that is not a crew,
    gives no such answer. Only the first may leave a chained forward ungated."""

    def _chained(self, tmp_path, monkeypatch, *, far_end, stated):
        import dataclasses

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
        relay = _relay_mint(mgr)

        async def stating(inst, params):
            return dataclasses.replace(await relay(inst, params), crew_gateway_id=stated)

        monkeypatch.setattr(mgr, "_mint_through_parent", stating)

        async def no_cycle(_inst, _port):
            return ""

        monkeypatch.setattr(mgr, "_chain_cycle_reason", no_cycle)

        async def identity(_port):
            return far_end

        monkeypatch.setattr(mgr, "_peer_identity", identity)
        primed: list[str] = []

        async def exchange(_url, link, _name):
            primed.append(link)
            return "SESSION"

        monkeypatch.setattr(mgr, "_exchange_link", exchange)
        return mgr, primed

    @pytest.mark.parametrize("stated", [None, "", "CREW-C"])
    def test_a_far_end_that_does_not_answer_is_refused_at_connect(
        self, tmp_path, monkeypatch, stated
    ):
        mgr, primed = self._chained(tmp_path, monkeypatch, far_end=None, stated=stated)
        st = asyncio.run(mgr.connect("c"))
        assert st.state.value == "error"
        assert "did not answer" in st.error
        assert "c" not in mgr._tokens
        assert primed == []

    def test_an_id_where_the_parent_saw_none_is_refused(self, tmp_path, monkeypatch):
        mgr, primed = self._chained(tmp_path, monkeypatch, far_end="SOMEONE", stated="")
        st = asyncio.run(mgr.connect("c"))
        assert st.state.value == "error"
        assert primed == []

    def test_a_crew_both_ends_saw_without_an_id_connects_ungated(self, tmp_path, monkeypatch):
        mgr, _primed = self._chained(tmp_path, monkeypatch, far_end="", stated="")
        st = asyncio.run(mgr.connect("c"))
        assert st.state.value == "connected"
        assert "c" not in mgr._chained_far_end

    def test_the_parent_refuses_to_mint_when_its_own_probe_gets_no_answer(
        self, tmp_path, monkeypatch
    ):
        reg, mgr = _mgr(tmp_path, monkeypatch)
        reg.add(name="C", ssh_host="c-host", instance_id="c")
        asyncio.run(mgr.connect("c"))

        async def silent(_port):
            return None

        monkeypatch.setattr(mgr, "_peer_identity", silent)
        ok, payload = asyncio.run(mgr.mint_embed_token("c", 9191))
        assert ok is False
        assert payload["code"] == "instance_identity_unconfirmed"
        assert "token" not in payload
        assert not mgr._lent_hops, "lent a hop it refused to mint for"

    def test_the_locked_answer_cannot_be_called_without_an_identity(self, tmp_path, monkeypatch):
        """An empty statement leaves the hub's forward ungated, so the identity is a
        required argument of the locked answer: a caller that forgot to read it gets
        a TypeError instead of quietly answering ``""``."""
        import inspect

        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        param = inspect.signature(SshTunnelManager._answer_embed_mint).parameters["crew_gateway_id"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is inspect.Parameter.empty, "the identity has a fail-open default"

        reg, mgr = _mgr(tmp_path, monkeypatch)
        inst = reg.add(name="C", ssh_host="c-host", instance_id="c")
        asyncio.run(mgr.connect("c"))
        with pytest.raises(TypeError, match="crew_gateway_id"):
            asyncio.run(
                mgr._answer_embed_mint(
                    inst, "c", "tok", mgr.status("c").local_port, 0, lent_ttl="1h"
                )
            )
        assert not mgr._lent_hops, "lent a hop with no identity stated"


class TestTheIdentityReplyIsReadThreeWays:
    @staticmethod
    def _answer(monkeypatch, status, body):
        from kiro_crew.instances import ssh_tunnel_manager as stm

        raw = body if isinstance(body, bytes) else json.dumps(body).encode()

        class _Resp:
            def __init__(self):
                self.status = status
                self.content = _FakeMintReader(raw)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

        class _Session:
            def __init__(self, *_a, **_k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            def get(self, *_a, **_k):
                return _Resp()

        monkeypatch.setattr(stm.aiohttp, "ClientSession", _Session)

    @pytest.mark.parametrize(
        ("status", "body", "expected"),
        [
            (200, {"ok": True, "gateway_id": "CREW-C"}, "CREW-C"),
            (200, {"ok": True}, ""),
            (200, {"ok": True, "gateway_id": ""}, None),
            (200, {"ok": True, "gateway_id": 7}, None),
            (200, {"gateway_id": "CREW-C"}, None),
            (200, ["ok"], None),
            (200, b"not json", None),
            (503, {"ok": True, "gateway_id": "CREW-C"}, None),
        ],
    )
    def test_each_reply_shape(self, tmp_path, monkeypatch, status, body, expected):
        _reg, mgr = _mgr(tmp_path, monkeypatch, real_identity=True)
        self._answer(monkeypatch, status, body)
        assert asyncio.run(mgr._peer_identity(1)) == expected
        assert asyncio.run(mgr._peer_gateway_id(1)) == (expected or "")

    def test_an_unreachable_port_is_no_answer(self, tmp_path, monkeypatch):
        _reg, mgr = _mgr(tmp_path, monkeypatch, real_identity=True)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        assert asyncio.run(mgr._peer_identity(free)) is None
