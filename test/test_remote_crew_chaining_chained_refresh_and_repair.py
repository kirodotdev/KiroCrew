"""How a chained crew's credential is refreshed and its forward repaired as the hop shifts.

A refresh retires the forward, dropping it and its credential while keeping the user's intent,
when the parent says the hop is gone or when the mint names the same port under a different hop
identity. The parent says so with a ``HopRetiredError``, read off the ``code`` of a real 409 or
404 reply; a transport error, a refusal body past the reply cap and a parent with no chaining
route stay retryable failures. The self-heal mints before tier one, so its rebuild dials and
persists the hop the parent reports; tier two rebuilds without minting again; and the heal
stands down when the parent names no hop. ``_store_token`` is the one place a credential is
refused for a moved hop: a refresh for a hop the parent has moved off is discarded and the
credential already held stays, a store that is not building the forward asks for the forward on
the moved port to be retired while one that is building it does not, and a mint for a
generation a rebuild has superseded is discarded without retiring the rebuilt forward; a
structural pin keeps every store routed through that decision, with the published hop, lifetime
and identity written only inside it. A heal whose rebuilds never land stores nothing for the
new hop, because the store follows the rebuild; a first chained connect and its reconnect store
the parent's shorter lifetime; a rebuild onto the reported hop rebinds the identity; and a mint
reply that cannot identify the hop is refused outright. Editing a parent tears down the crews
riding it with their intent kept, or aborts the edit when a child will not stop, and a
parent-hop re-mint is discarded when a reconnect supersedes it and stored when nothing does.
The proactive refresh interval is re-derived from the lifetime each store records, so a hub
schedules from the capped lent lifetime, follows a parent lowering its TTL, never sleeps past
the token it protects and keeps its interval after a failed refresh. A topic of the
``test_remote_crew_chaining`` family: ``test_remote_crew_chaining.py`` keeps the data model and
its chain walks, the forward, the token path and the cycle guard, and the other siblings are
``test_remote_crew_chaining_subtree_removal.py``,
``test_remote_crew_chaining_route_admission.py``,
``test_remote_crew_chaining_credential_destination.py`` and
``test_remote_crew_chaining_lent_hop_lease.py``, with the shared fakes in
``remote_crew_chaining_helpers.py``.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from remote_crew_chaining_helpers import (
    VIA_ID,
    _FakeMintSession,
    _FakeTunnel,
    _mgr,
    _no_cookie_header,
    _relay_mint,
)

from kiro_crew.instances.ssh_tunnel_manager import _Mint


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
                # flake-ok: pre-existing unbounded await on the retirement tasks the manager scheduled; each ends in-process, moved verbatim
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
        monkeypatch.setattr(mgr, "_peer_cookie_header", _no_cookie_header)

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
        monkeypatch.setattr(mgr, "_peer_cookie_header", _no_cookie_header)

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
        monkeypatch.setattr(mgr, "_peer_cookie_header", _no_cookie_header)

        with pytest.raises(TokenMintError) as caught:
            asyncio.run(mgr._mint_through_parent(inst, params))
        assert not isinstance(caught.value, HopRetiredError), "retired a hop over an old build"
        assert "no endpoint for chaining" in str(caught.value)


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
        #
        # It ALSO asks for the forward to go, which this case originally asserted it
        # must not. That was wrong, and the mechanism is the one GPT named: the parent
        # moved this crew to a new port and can hand the old one to ANOTHER crew, so the
        # forward still dialling the old number reaches that crew, and the token already
        # in `_tokens` keeps travelling over it. Discarding the new mint corrected
        # neither, and each refresh cycle re-discarded and retried, so the stale pair
        # survived for as long as the crew was connected. The reason the original
        # assertion gave -- that a forward being BUILT also names a hop it does not yet
        # dial -- is real, and it is what `binds_forward` is for; it is not a reason to
        # leave this case alone.
        asked: list[str] = []
        monkeypatch.setattr(
            mgr, "_schedule_chained_retirement", lambda _id, reason: asked.append(reason)
        )
        assert (
            mgr._store_token(
                inst, _Mint("MOVED", hop=54321), minted_at_epoch=epoch, binds_forward=False
            )
            is False
        )
        assert asked, "a moved port discarded the mint and left the stale forward running"
        assert "54321" in asked[0], f"retired for some other reason: {asked[0]!r}"
        assert mgr.get_token("c") == "AGREE"

        # And NOT while this store is the one defining the pairing: a forward being
        # built names a hop it does not dial yet, and retiring there would tear down
        # the tunnel being set up. The mint is still DISCARDED -- only the retirement
        # is gated, which is the whole of this change.
        asked.clear()
        assert (
            mgr._store_token(
                inst, _Mint("BUILDING", hop=54321), minted_at_epoch=epoch, binds_forward=True
            )
            is False
        )
        assert asked == [], "retired a forward this very store was establishing"

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

        from kiro_crew.instances import ssh_tunnel_manager as mod

        src = "".join(
            (
                pathlib.Path(__file__).resolve().parents[1]
                / "src/kiro_crew/instances"
                / f"{name.rpartition('.')[2]}.py"
            ).read_text()
            for name in (mod.__name__, *mod._OWNERS)
        )
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
                # flake-ok: pre-existing unbounded await on the retirement tasks the manager scheduled; each ends in-process, moved verbatim
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
                # flake-ok: pre-existing unbounded await on the retirement tasks the manager scheduled; each ends in-process, moved verbatim
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
        monkeypatch.setattr(mgr, "_peer_cookie_header", _no_cookie_header)

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

        # flake-ok: pre-existing worker-wide asyncio.sleep rebind that records each interval the refresh loop asks for, scoped by monkeypatch, moved verbatim
        monkeypatch.setattr(asyncio, "sleep", record)

        async def run():
            with contextlib.suppress(asyncio.CancelledError):
                await mgr._token_refresh_loop("c")

        asyncio.run(run())
        return seen

    def test_a_hub_schedules_its_re_mint_from_the_capped_lifetime(self, tmp_path, monkeypatch):
        """The other end of the cap. A parent lends under a capped lifetime and says
        so; the hub's own row for that crew is the 20h default, and scheduling from
        the row would put the re-mint long after the credential is dead. The pane
        would then reload on every cycle, which is what makes the cap safe or not.
        """
        from kiro_crew.instances.constants import (
            DEFAULT_TOKEN_REFRESH_FRACTION,
            LENT_HOP_TTL_CAP,
        )
        from kiro_crew.instances.token_mint import ttl_to_seconds

        cap = ttl_to_seconds(LENT_HOP_TTL_CAP)
        reg, mgr = self._chained(tmp_path, monkeypatch, row_ttl="20h")
        monkeypatch.setattr(
            mgr, "_mint_through_parent", self._parent_answering(mgr, {"ttl": LENT_HOP_TTL_CAP})
        )
        assert asyncio.run(mgr.connect("c")).state.value == "connected"

        assert mgr.token_ttl_total("c") == cap, "stored its own row over the lent lifetime"
        seen = self._intervals(mgr, monkeypatch, cycles=1)
        assert seen[0] == pytest.approx(
            cap * DEFAULT_TOKEN_REFRESH_FRACTION
        ), f"slept {seen[0]:.0f}s against a {cap}s credential"

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
