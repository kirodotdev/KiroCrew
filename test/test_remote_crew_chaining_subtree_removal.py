"""How taking a parent crew away takes the crews chained through it along.

These tests pin the cascade below a parent. Disconnecting it closes its children's forwards
while each child keeps its connect intent. ``api_instances_remove`` removes every row below the
parent and leaves an unrelated crew, decides a 404 before it deletes anything, sweeps every
captured crew deepest first both before and after the rows go, and deletes the subtree leaves
first in one registry write, so no row outlives its parent. A child whose teardown raises
refuses the removal with a 409 and every row kept, stopping at that child, and every teardown
it runs, including a grandchild's that succeeds first, is asked to keep its connect intent; a
forward that reappears after the rows are gone is logged rather than raised; and a chained add
racing the removal leaves no orphan. ``InstancesRegistry.remove_cascade`` answers deepest first
in one write and empty for a missing row, and the cloud ``unregister_instance`` path removes
the crew chained through the parent it destroys while an unrelated crew stays. ``_FakeMgr``
records each disconnect with the row count the registry holds when it lands. A topic of the
``test_remote_crew_chaining`` family: ``test_remote_crew_chaining.py`` keeps the data model and
its chain walks, the forward, the token path and the cycle guard, and the other siblings are
``test_remote_crew_chaining_route_admission.py``,
``test_remote_crew_chaining_credential_destination.py``,
``test_remote_crew_chaining_lent_hop_lease.py`` and
``test_remote_crew_chaining_chained_refresh_and_repair.py``, with the shared fakes in
``remote_crew_chaining_helpers.py``.
"""

from __future__ import annotations

import asyncio
import logging

from remote_crew_chaining_helpers import (
    VIA_ID,
    _enable,
    _FakeReq,
    _mgr,
    _relay_mint,
    _resp_body,
    _State,
)

from kiro_crew.instances.registry import InstancesRegistry


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


# ── the cascade ──────────────────────────────────────────────────────────


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
            # flake-ok: pre-existing one-tick yield, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)
            removing = asyncio.create_task(
                handlers.api_instances_remove(
                    _FakeReq(_State(reg, _FakeMgr(registry=reg)), match={"id": "b"})
                )
            )
            # flake-ok: pre-existing unbounded await on the add and remove handler tasks started above; both end in-process, moved verbatim
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
        writes: list[int] = []
        real_cascade = reg.remove_cascade
        real_write = reg._write

        def recording_cascade(target):
            removed = real_cascade(target)
            order.extend(removed)
            return removed

        def counting_write(doc):
            writes.append(len(doc.instances))
            return real_write(doc)

        monkeypatch.setattr(reg, "remove_cascade", recording_cascade)
        monkeypatch.setattr(reg, "_write", counting_write)
        resp = asyncio.run(
            handlers.api_instances_remove(_FakeReq(_State(reg, _FakeMgr()), match={"id": "b"}))
        )
        assert resp.status == 200
        assert order == ["d", "c", "b"], f"removed in {order}, so a row can outlive its parent"
        # Stronger than the ordering on its own: one write means there is no instant at
        # which a child is gone and its parent is not, so an interleaved read cannot see
        # the orphan at all rather than merely being unlikely to.
        assert writes == [0], f"the removal wrote {len(writes)} times, not once: {writes}"
        assert reg.list() == []


class TestRemovingAParentTakesItsChainedSubtree:
    """The orphan F3 names: a row that can never connect and that nothing lists.

    A chained crew rides its parent's forward, so a parent removed alone leaves a child
    whose only cure is a user noticing it. The cascade was spelled out in the dashboard
    DELETE handler while the cloud destroy path removed a single row beside it, so the rule
    held on one path and not the other. One helper now serves both.
    """

    def test_a_cloud_unregister_of_a_parent_removes_the_crew_chained_through_it(
        self, tmp_path, monkeypatch
    ):
        """Driven through `unregister_instance`, the path cloud destroy actually calls."""
        from kiro_crew.cloud import connect as cloud_connect
        from kiro_crew.instances.registry import InstancesRegistry

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="Parent", ssh_host="parent-host", instance_id="parent", ttl="2h")
        reg.add(
            name="Child",
            ssh_host="child-host",
            instance_id="child",
            ttl="2h",
            via_instance_id="parent",
            via_remote_port=53999,
            via_remote_id=VIA_ID,
        )
        reg.add(name="Bystander", ssh_host="other-host", instance_id="other", ttl="2h")
        monkeypatch.setattr(cloud_connect, "InstancesRegistry", lambda: reg, raising=False)
        monkeypatch.setattr(
            "kiro_crew.instances.registry.InstancesRegistry", lambda *a, **k: reg, raising=False
        )

        assert cloud_connect.unregister_instance("parent-host") is True

        left = {i.id for i in reg.list()}
        assert "child" not in left, (
            "destroying the parent left its chained child behind: the row rides a forward "
            "that no longer exists, so it can never connect and nothing lists it as broken"
        )
        assert "parent" not in left
        assert "other" in left, "the cascade removed an unrelated crew"

    def test_the_cascade_is_one_write_so_no_read_sees_a_child_without_its_parent(self, tmp_path):
        """Removing a row at a time left a window an interleaved read could land in."""
        from kiro_crew.instances.registry import InstancesRegistry

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="P", ssh_host="p", instance_id="p", ttl="2h")
        reg.add(
            name="C",
            ssh_host="c",
            instance_id="c",
            ttl="2h",
            via_instance_id="p",
            via_remote_port=53999,
            via_remote_id=VIA_ID,
        )

        seen: list[int] = []
        real_write = reg._write

        def counting_write(doc):
            seen.append(len(doc.instances))
            return real_write(doc)

        reg._write = counting_write  # type: ignore[method-assign]
        removed = reg.remove_cascade("p")

        # Two levels is the deepest the tree allows -- a crew chained through an already
        # chained crew is refused as `chain_too_deep` -- so this is the whole subtree.
        assert removed == ["c", "p"], (
            f"the cascade removed {removed}; it must answer deepest-first so a caller "
            f"tears down and audits in the same order"
        )
        assert seen == [0], f"the cascade wrote {len(seen)} times, not once: {seen}"
        assert reg.list() == []

    def test_removing_a_row_with_no_children_still_answers_and_a_missing_one_is_empty(
        self, tmp_path
    ):
        """The two boundary answers a caller branches on."""
        from kiro_crew.instances.registry import InstancesRegistry

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="Solo", ssh_host="solo", instance_id="solo", ttl="2h")
        assert reg.remove_cascade("solo") == ["solo"]
        assert reg.remove_cascade("solo") == [], "a missing row must answer empty, not raise"
