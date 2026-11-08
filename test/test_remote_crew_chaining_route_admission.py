"""What the dashboard's instance routes accept and answer for a chained crew.

These tests pin ``api_instances_add`` at the add boundary: a second hop is admitted, and a
third hop (``chain_too_deep``), an unknown or ssm parent, a parent already holding
``MAX_CHAINED_PER_PARENT`` crews, a second row for the same (parent, remote id) pair and a name
past ``INSTANCE_NAME_MAX`` are each refused with a 400. The third hop's refusal states the
distance in hops; it and the width, duplicate and name refusals leave no row behind; the depth,
unknown-parent, ssm-parent and duplicate refusals each read as a whole sentence naming the
crew; the duplicate check is scoped to the (parent, remote id) pair; the one ``_chain_refusal``
call, which decides the chain refusals, sits inside the one ``_CHAIN_MUTATION_LOCK`` block, so
two racing notices leave one row; the width cap counts per parent and never refuses a top-level
crew; and a name exactly at the cap is admitted. The name cap matches the relay's
``CHAINED_NAME_MAX``, and a stored name past it still lets disconnect persist its reset, while
a rename past it is refused and a short rename lands. A PATCH edits the hop port and never
re-parents a crew. ``token_ttl_total`` counts from the token's own stored lifetime rather than
the row's and is absent for a crew with no token, and ``_status_for`` carries it beside
``token_ttl_remaining`` or leaves it out. ``api_instances_embed_token`` hands back the token
and the hop port, refuses a malformed port before any mint and a non-owner caller outright,
answers a refusal with the manager's status while keeping that status out of the payload, and
is registered ahead of the catch-all proxy. A topic of the ``test_remote_crew_chaining``
family: ``test_remote_crew_chaining.py`` keeps the data model and its chain walks, the forward,
the token path and the cycle guard, and the other siblings are
``test_remote_crew_chaining_subtree_removal.py``,
``test_remote_crew_chaining_credential_destination.py``,
``test_remote_crew_chaining_lent_hop_lease.py`` and
``test_remote_crew_chaining_chained_refresh_and_repair.py``, with the shared fakes in
``remote_crew_chaining_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import pytest
from remote_crew_chaining_helpers import (
    VIA_ID,
    _enable,
    _FakeReq,
    _FakeTunnel,
    _mgr,
    _resp_body,
    _State,
)

from kiro_crew.instances.registry import (
    INSTANCE_NAME_MAX,
    MAX_CHAINED_PER_PARENT,
    MAX_VIA_HOPS,
    InstancesRegistry,
    InvalidInstanceError,
)
from kiro_crew.instances.ssh_tunnel_manager import _Mint

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
            # flake-ok: pre-existing unbounded await on the two in-process add calls this gather races, moved verbatim
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

    def test_a_pre_cap_name_still_lets_disconnect_persist_its_reset(self, tmp_path):
        """Nothing truncates a name on the way in, so a stored name can be longer than
        the cap. ``update()`` revalidates the WHOLE record, and disconnect's reset rides
        through ``update()`` as a hint-only patch whose failure is swallowed as
        best-effort -- so capping in ``Instance.validate`` loses that reset silently and
        the next start revives a crew the user disconnected, holding a port that is free
        for anything else to bind. The cap belongs at the write sites, which is where
        ``ttl`` already puts it for this same reason.
        """
        from kiro_crew.instances.registry import validate_instance_name

        path = tmp_path / "instances.json"
        reg = InstancesRegistry(path=path)
        reg.add(name="B", ssh_host="b-host", instance_id="b")

        # A stored row over the cap, mid-session, so the reset below is the write that
        # must not be lost.
        legacy = "x" * (INSTANCE_NAME_MAX + 50)
        doc = json.loads(path.read_text())
        doc["instances"][0]["name"] = legacy
        doc["instances"][0]["was_connected"] = True
        doc["instances"][0]["local_port"] = 51234
        path.write_text(json.dumps(doc))

        # Exactly the hint set `disconnect` sends (ssh_tunnel_manager `_disconnect`).
        InstancesRegistry(path=path).update(
            "b",
            local_port=0,
            forwarder_pid=0,
            forwarder_start="",
            forwarder_sig="",
            was_connected=False,
        )

        persisted = json.loads(path.read_text())["instances"][0]
        assert persisted["was_connected"] is False, "the disconnect reset was lost"
        assert persisted["local_port"] == 0, "the freed port stayed pinned"
        assert persisted["name"] == legacy, "the untouched name was rewritten"

        # The one write this rule must still refuse is a name CHANGE, at any layer.
        with pytest.raises(InvalidInstanceError):
            InstancesRegistry(path=path).update("b", name="y" * (INSTANCE_NAME_MAX + 1))
        with pytest.raises(InvalidInstanceError):
            validate_instance_name("y" * (INSTANCE_NAME_MAX + 1))

        # And renaming is that row's only exit, so it has to work.
        InstancesRegistry(path=path).update("b", name="short")
        assert json.loads(path.read_text())["instances"][0]["name"] == "short"

    def test_the_backend_name_cap_matches_the_relays(self):
        """The relay slices an announced name before sending it, so frame code can
        never exceed the cap; the backend's copy is what makes the bound a property
        of the STORE, which is the only place it holds for a caller that does not go
        through the relay. Two numbers that must agree, pinned so they cannot drift.
        """
        import re

        ts = (
            pathlib.Path(__file__).resolve().parents[1] / "website/src/lib/chainAnnounce.ts"
        ).read_text(encoding="utf-8")
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
