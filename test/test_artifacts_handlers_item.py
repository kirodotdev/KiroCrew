"""One artifact's read, delete, reprobe-notice and version routes.

Covers ``GET /api/artifacts/{slug}`` (the content, a 404, and the ``referenced`` breadcrumb
an agent read records and audits while a browser or restricted read records nothing),
delete (restricted sessions refused, the published copy withdrawn before the local delete,
and the destination states that refuse or allow it), the publish reprobe notice, and the
version list and lookups. MagicMock requests and a real store from
``artifacts_handlers_helpers.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from artifacts_handlers_helpers import (  # noqa: F401 -- fixtures are requested by name
    _json_body,
    _request,
    isolated_store_fixture,
    patch_restricted_fixture,
)

from kiro_crew.dashboard.handlers.artifacts import (
    api_artifact_delete,
    api_artifact_detail,
    api_artifact_reprobe_notice,
    api_artifact_version_detail,
    api_artifact_versions,
)


class TestDetail:
    @pytest.mark.asyncio
    async def test_returns_content(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="<x/>", slug="x")
        resp = await api_artifact_detail(_request(match={"slug": "x"}))
        assert resp.status == 200
        body = _json_body(resp)
        assert body["content"] == "<x/>"

    @pytest.mark.asyncio
    async def test_missing_returns_404(self, isolated_store, patch_restricted) -> None:
        resp = await api_artifact_detail(_request(match={"slug": "nope"}))
        assert resp.status == 404

    # ── agent-read breadcrumbs (feeds ?touched_by=) ──────────────────────────

    @pytest.mark.asyncio
    async def test_agent_read_records_referenced_event(
        self, isolated_store, patch_restricted
    ) -> None:
        """An MCP read leaves a breadcrumb so the session can list what it consumed."""
        isolated_store.create(name="x", content="<x/>", slug="x")
        resp = await api_artifact_detail(
            _request(
                match={"slug": "x"},
                session_key="chat-7",
                extra_headers={"X-Internal-Secret": "s"},
            )
        )
        assert resp.status == 200
        events = [e for e in isolated_store.get("x").events if e.get("type") == "referenced"]
        assert len(events) == 1
        assert events[0]["session_id"] == "chat-7"
        assert events[0]["by"] == "agent"

    @pytest.mark.asyncio
    async def test_agent_read_still_returns_content(self, isolated_store, patch_restricted) -> None:
        """Regression guard: recording must not strip the body off the response.

        ``record_impression`` returns a meta loaded WITHOUT content, so assigning
        it over the artifact would make every agent read come back empty — the
        artifact would look blank to the agent that just asked for it.
        """
        isolated_store.create(name="x", content="<x/>", slug="x")
        resp = await api_artifact_detail(
            _request(
                match={"slug": "x"},
                session_key="chat-7",
                extra_headers={"X-Internal-Secret": "s"},
            )
        )
        assert _json_body(resp)["content"] == "<x/>"

    @pytest.mark.asyncio
    async def test_browser_read_records_nothing(self, isolated_store, patch_restricted) -> None:
        """A dashboard page view is not session activity and must not mutate."""
        isolated_store.create(name="x", content="<x/>", slug="x")
        resp = await api_artifact_detail(_request(match={"slug": "x"}))
        assert resp.status == 200
        assert [e for e in isolated_store.get("x").events if e.get("type") == "referenced"] == []

    @pytest.mark.asyncio
    async def test_agent_read_without_internal_secret_records_nothing(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="x", content="<x/>", slug="x")
        await api_artifact_detail(_request(match={"slug": "x"}, session_key="chat-7"))
        assert [e for e in isolated_store.get("x").events if e.get("type") == "referenced"] == []

    @pytest.mark.asyncio
    async def test_restricted_session_read_records_nothing(
        self, isolated_store, patch_restricted
    ) -> None:
        """Incognito/temporary sessions exist to leave no durable trace.

        The read itself is still served (reads are not gated here) — only the
        breadcrumb is withheld.
        """
        isolated_store.create(name="x", content="<x/>", slug="x")
        resp = await api_artifact_detail(
            _request(
                match={"slug": "x"},
                session_key="chat-7",
                restricted=True,
                extra_headers={"X-Internal-Secret": "s"},
            )
        )
        assert resp.status == 200
        assert _json_body(resp)["content"] == "<x/>"
        assert [e for e in isolated_store.get("x").events if e.get("type") == "referenced"] == []

    @pytest.mark.asyncio
    async def test_repeated_agent_reads_record_one_breadcrumb(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="x", content="<x/>", slug="x")
        req = dict(
            match={"slug": "x"},
            session_key="chat-7",
            extra_headers={"X-Internal-Secret": "s"},
        )
        await api_artifact_detail(_request(**req))
        await api_artifact_detail(_request(**req))
        events = [e for e in isolated_store.get("x").events if e.get("type") == "referenced"]
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_missing_state_records_nothing(self, isolated_store, patch_restricted) -> None:
        """Deny by default: no dashboard state means the breadcrumb is withheld.

        Guards the fail-open shape ``not (state is not None and restricted(...))``,
        which evaluates to True when ``state`` is falsy and would let an
        unclassifiable session — including an incognito one — write.
        """
        isolated_store.create(name="x", content="<x/>", slug="x")
        req = _request(
            match={"slug": "x"},
            session_key="chat-7",
            extra_headers={"X-Internal-Secret": "s"},
        )
        req.app = {"state": None, "_restricted_session": False}
        resp = await api_artifact_detail(req)
        assert resp.status == 200
        assert _json_body(resp)["content"] == "<x/>"
        assert [e for e in isolated_store.get("x").events if e.get("type") == "referenced"] == []

    @pytest.mark.asyncio
    async def test_agent_read_is_audited(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        """The write is a permission decision, so it belongs in the SEL trail."""
        from kiro_crew.dashboard.handlers import artifacts as art_handlers

        sel_stub = MagicMock()
        monkeypatch.setattr(art_handlers, "sel", lambda: sel_stub)
        isolated_store.create(name="x", content="<x/>", slug="x")
        await api_artifact_detail(
            _request(
                match={"slug": "x"},
                session_key="chat-7",
                extra_headers={"X-Internal-Secret": "s"},
            )
        )
        kwargs = sel_stub.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "artifact_reference"
        assert kwargs["outcome"] == "ok"

    @pytest.mark.asyncio
    async def test_withheld_breadcrumb_is_audited(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        """A withheld breadcrumb is still a decision — record it, mirroring
        the POST /events sibling, which audits its restricted-session denial."""
        from kiro_crew.dashboard.handlers import artifacts as art_handlers

        sel_stub = MagicMock()
        monkeypatch.setattr(art_handlers, "sel", lambda: sel_stub)
        isolated_store.create(name="x", content="<x/>", slug="x")
        await api_artifact_detail(
            _request(
                match={"slug": "x"},
                session_key="chat-7",
                restricted=True,
                extra_headers={"X-Internal-Secret": "s"},
            )
        )
        kwargs = sel_stub.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "artifact_reference"
        assert kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_browser_read_is_not_audited_as_a_reference(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        """A plain page view makes no decision, so it must not add SEL noise."""
        from kiro_crew.dashboard.handlers import artifacts as art_handlers

        sel_stub = MagicMock()
        monkeypatch.setattr(art_handlers, "sel", lambda: sel_stub)
        isolated_store.create(name="x", content="<x/>", slug="x")
        await api_artifact_detail(_request(match={"slug": "x"}))
        sel_stub.log_tool_invocation.assert_not_called()


class TestDelete:
    @pytest.mark.asyncio
    async def test_deletes(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="a", slug="x")
        resp = await api_artifact_delete(_request(match={"slug": "x"}))
        assert resp.status == 200
        assert _json_body(resp) == {"ok": True}
        assert not (isolated_store.root / "x").exists()

    @pytest.mark.asyncio
    async def test_missing_404(self, isolated_store, patch_restricted) -> None:
        resp = await api_artifact_delete(_request(match={"slug": "nope"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_restricted_denied(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="a", slug="x")
        resp = await api_artifact_delete(_request(match={"slug": "x"}, restricted=True))
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_withdraws_the_published_copy_before_deleting_locally(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        """The publication is the only handle that can withdraw the destination copy, so
        the attempt has to happen while it still exists. Die between the two steps in this
        order and the copy is withdrawn but the artifact remains -- the user deletes again.
        In the reverse order the record is gone while the content is still public."""
        from kiro_crew.artifacts import ArtifactPublication

        isolated_store.create(name="x", content="a", slug="x")
        isolated_store.set_publication(
            "x",
            ArtifactPublication(
                provider="default",
                artifact_id="uuid-1",
                view_url="https://d/x",
                visibility="PUBLIC",
            ),
        )
        order: list[str] = []

        async def _withdraw(art):
            order.append("withdraw")
            assert isolated_store.get("x") is not None, "the handle must still exist here"

        real_delete = isolated_store.delete

        def _delete(slug, **kw):
            order.append("delete")
            return real_delete(slug, **kw)

        monkeypatch.setattr("kiro_crew.publish_sync.delete_for_artifact", _withdraw)
        monkeypatch.setattr(isolated_store, "delete", _delete)
        resp = await api_artifact_delete(_request(match={"slug": "x"}))
        assert resp.status == 200
        assert order == ["withdraw", "delete"]

    @pytest.mark.asyncio
    async def test_a_recreate_during_the_withdrawal_is_not_deleted(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        """The withdrawal is a network round trip and ``delete()`` removes by SLUG, so a
        delete-and-recreate landing in that window would make the delayed request remove
        the REPLACEMENT -- an artifact the user never asked to delete and that nothing can
        restore. The ordering note above reasons that a save in that window "is included in
        the delete the user asked for", which holds for a save to the SAME artifact; a
        recreate is a different artifact wearing the same slug.
        """
        import kiro_crew.publish_sync as _ps
        from kiro_crew.artifacts import ArtifactPublication

        isolated_store.create(name="x", content="a", slug="x")
        isolated_store.set_publication(
            "x",
            ArtifactPublication(
                provider="default",
                artifact_id="uuid-1",
                view_url="https://d/x",
                visibility="PUBLIC",
            ),
        )

        async def _withdraw(art):
            # The concurrent delete + recreate completes while this await is in flight.
            isolated_store.delete("x")
            isolated_store.create(name="x-replacement", content="b", slug="x")
            return _ps.DeleteWithdrawal.WITHDRAWN

        monkeypatch.setattr("kiro_crew.publish_sync.delete_for_artifact", _withdraw)
        resp = await api_artifact_delete(_request(match={"slug": "x"}))
        assert resp.status == 409
        # The replacement must survive, and still be the replacement.
        survivor = isolated_store.get("x")
        assert survivor is not None
        assert survivor.name == "x-replacement"

    @pytest.mark.asyncio
    async def test_the_post_withdrawal_reread_runs_off_the_event_loop(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        """`store.get` returns the artifact WITH content, so on a large artifact it is a
        multi-megabyte synchronous read -- and this handler is async, so doing it inline
        stalls the gateway loop for every other task.

        Only the read AFTER the withdrawal is asserted: the capture read at the top of the
        handler is pre-existing on main and is a separate defect, not this PR's.
        """
        import threading

        import kiro_crew.publish_sync as _ps
        from kiro_crew.artifacts import ArtifactPublication

        isolated_store.create(name="x", content="a", slug="x")
        isolated_store.set_publication(
            "x",
            ArtifactPublication(
                provider="default",
                artifact_id="uuid-1",
                view_url="https://d/x",
                visibility="PUBLIC",
            ),
        )

        state = {"withdrawn": False}
        after_threads: list[str] = []
        real_get = isolated_store.get

        def _recording_get(slug, **kw):
            if state["withdrawn"]:
                after_threads.append(threading.current_thread().name)
            return real_get(slug, **kw)

        async def _withdraw(art):
            state["withdrawn"] = True
            return _ps.DeleteWithdrawal.WITHDRAWN

        monkeypatch.setattr("kiro_crew.publish_sync.delete_for_artifact", _withdraw)
        monkeypatch.setattr(isolated_store, "get", _recording_get)

        loop_thread = threading.current_thread().name
        resp = await api_artifact_delete(_request(match={"slug": "x"}))
        assert resp.status == 200
        assert after_threads, "the post-withdrawal re-read never happened"
        assert all(
            t != loop_thread for t in after_threads
        ), f"re-read ran on the event-loop thread {loop_thread!r}: {after_threads!r}"

    @pytest.mark.asyncio
    async def test_a_republish_landing_after_the_withdrawal_refuses_the_delete(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        """A publish landing between the withdrawal and the removal must not be erased.

        The pre-existing ``created_at`` guard cannot catch this one: it compares artifact
        GENERATIONS, and a republish is the SAME artifact, so its ``created_at`` is
        unchanged and that guard passes. What differs is the publication -- a new copy is
        live at the destination and the record naming it is the only handle able to take
        it down. Removing the artifact would drop that record.

        The window is reproduced where it actually exists: the republish is injected right
        after the handler clears the withdrawn record, which is the exact gap between that
        clear and the removal. The refusal has to come from inside the store's lock, so
        the assertion is that the artifact and the NEW record both survive.
        """
        import kiro_crew.publish_sync as _ps
        from kiro_crew.artifacts import ArtifactPublication

        isolated_store.create(name="x", content="a", slug="x")
        isolated_store.set_publication(
            "x",
            ArtifactPublication(
                provider="default",
                artifact_id="uuid-1",
                view_url="https://d/x",
                visibility="PUBLIC",
            ),
        )

        async def _withdraw(art):
            return _ps.DeleteWithdrawal.WITHDRAWN

        real_clear = isolated_store.clear_publication

        def _clear_then_republish(slug, **kw):
            result = real_clear(slug, **kw)
            # The concurrent publish: a NEW copy, so a NEW handle.
            isolated_store.set_publication(
                slug,
                ArtifactPublication(
                    provider="default",
                    artifact_id="uuid-2",
                    view_url="https://d/x2",
                    visibility="PUBLIC",
                ),
            )
            return result

        monkeypatch.setattr("kiro_crew.publish_sync.delete_for_artifact", _withdraw)
        monkeypatch.setattr(isolated_store, "clear_publication", _clear_then_republish)

        resp = await api_artifact_delete(_request(match={"slug": "x"}))
        assert resp.status == 409, "a republish in the window must refuse the delete"

        survivor = isolated_store.get("x")
        assert survivor is not None, "the artifact must survive so the new copy keeps a handle"
        assert survivor.publication is not None, "the new publication record must survive"
        assert (
            survivor.publication.artifact_id == "uuid-2"
        ), "the surviving record must be the NEW publication, not the withdrawn one"

    @pytest.mark.asyncio
    async def test_an_unregistered_destination_refuses_the_delete_and_the_handler_adds_no_guard(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        """Two things. First, the handler owns no guard of its own: a stub that raises
        propagates straight through it, so the decision genuinely lives in
        ``delete_for_artifact`` rather than being duplicated here.

        Second, a publication naming a destination this edition does not register
        yields UNREACHABLE, and the delete is REFUSED. Completing the delete instead
        -- on the grounds that refusing "would leave an artifact its owner could never
        delete" -- trades a recoverable annoyance for an unrecoverable one: the record
        it drops is the only handle that can ever withdraw a world-readable copy. A
        refused delete can be retried, or the owner can unpublish and accept the
        exposure deliberately.
        """
        from kiro_crew.artifacts import ArtifactPublication

        isolated_store.create(name="x", content="a", slug="x")
        isolated_store.set_publication(
            "x",
            ArtifactPublication(
                provider="default",
                artifact_id="uuid-1",
                view_url="https://d/x",
                visibility="PUBLIC",
            ),
        )

        async def _boom(art):
            raise RuntimeError("destination unreachable")

        import kiro_crew.publish_sync as _ps

        real_withdraw = _ps.delete_for_artifact
        # The decision lives in delete_for_artifact; patching past it proves the handler
        # does not carry a second copy of it.
        monkeypatch.setattr("kiro_crew.publish_sync.delete_for_artifact", _boom)
        with pytest.raises(RuntimeError):
            await api_artifact_delete(_request(match={"slug": "x"}))
        # With the real function, an unregistered destination now REFUSES. Restore only
        # THIS patch -- monkeypatch.undo() would also drop the restricted-session fixture
        # and the handler would answer 403.
        monkeypatch.setattr("kiro_crew.publish_sync.delete_for_artifact", real_withdraw)
        resp = await api_artifact_delete(_request(match={"slug": "x"}))
        assert resp.status == 502
        # The artifact and the handle that could still withdraw the copy both survive.
        assert (isolated_store.root / "x").exists()
        assert isolated_store.get("x").publication is not None

    @pytest.mark.asyncio
    async def test_a_reachable_withdrawal_failure_keeps_the_artifact_and_its_handle(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        """When the destination is REACHABLE but rejects the
        withdrawal, a retry can still succeed -- so the publication (the only handle that
        can withdraw the still-public copy) must NOT be discarded. The delete is refused
        with an error, the artifact stays, and its publication record survives."""
        import kiro_crew.publish_sync as _ps
        from kiro_crew.artifacts import ArtifactPublication

        isolated_store.create(name="x", content="a", slug="x")
        isolated_store.set_publication(
            "x",
            ArtifactPublication(
                provider="default",
                artifact_id="uuid-1",
                view_url="https://d/x",
                visibility="PUBLIC",
            ),
        )

        async def _reachable_but_rejected(art):
            return _ps.DeleteWithdrawal.FAILED

        monkeypatch.setattr("kiro_crew.publish_sync.delete_for_artifact", _reachable_but_rejected)
        resp = await api_artifact_delete(_request(match={"slug": "x"}))
        assert resp.status == 502
        # The artifact and its retry handle both survive.
        assert (isolated_store.root / "x").exists()
        assert isolated_store.get("x").publication is not None

    @pytest.mark.asyncio
    async def test_an_unreachable_destination_refuses_the_delete(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        """An unreachable destination must NOT be an "escape hatch" that lets the
        delete proceed on the reasoning that "no retry from here can reach it".

        The premise was that an unreachable destination means the copy is beyond help. It
        does not -- unreachable describes THIS PROCESS's access (revoked credentials, a
        closed account, no network), not the object, which may still be served to the whole
        internet. Deleting the record then erased the only handle that could withdraw it,
        with nothing able to recover. Refusing is recoverable: restore access and retry, or
        unpublish to accept the exposure in the open.
        """
        import kiro_crew.publish_sync as _ps
        from kiro_crew.artifacts import ArtifactPublication

        isolated_store.create(name="x", content="a", slug="x")
        isolated_store.set_publication(
            "x",
            ArtifactPublication(
                provider="default",
                artifact_id="uuid-1",
                view_url="https://d/x",
                visibility="PUBLIC",
            ),
        )

        async def _unreachable(art):
            return _ps.DeleteWithdrawal.UNREACHABLE

        monkeypatch.setattr("kiro_crew.publish_sync.delete_for_artifact", _unreachable)
        resp = await api_artifact_delete(_request(match={"slug": "x"}))
        assert resp.status == 502
        assert (isolated_store.root / "x").exists()
        assert isolated_store.get("x").publication is not None

    @pytest.mark.asyncio
    async def test_a_confirmed_gone_destination_lets_the_delete_through(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        """The one case that still deletes, and the reason the distinction has to be a TYPE.

        A destination CONFIRMED absent has nothing left to serve, so there is no copy to
        strand and no handle worth keeping -- that is `NOTHING_PUBLISHED`. It is reached
        only from a typed `DriveNotFound`; read off an error message instead, a throttled or
        unauthorized reply would land here too and delete the handle to a live copy.
        """
        import kiro_crew.publish_sync as _ps
        from kiro_crew.artifacts import ArtifactPublication

        isolated_store.create(name="x", content="a", slug="x")
        isolated_store.set_publication(
            "x",
            ArtifactPublication(
                provider="default",
                artifact_id="uuid-1",
                view_url="https://d/x",
                visibility="PUBLIC",
            ),
        )

        async def _gone(art):
            return _ps.DeleteWithdrawal.NOTHING_PUBLISHED

        monkeypatch.setattr("kiro_crew.publish_sync.delete_for_artifact", _gone)
        resp = await api_artifact_delete(_request(match={"slug": "x"}))
        assert resp.status == 200
        assert not (isolated_store.root / "x").exists()

    @pytest.mark.asyncio
    async def test_artifact_error_fallback_returns_500(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        # A base ArtifactError raised by store.delete() (e.g. a future
        # store-level sensitive-path or filesystem refusal) must be caught and
        # audited, not escape the handler and 500 silently.
        from kiro_crew.artifacts import ArtifactError

        isolated_store.create(name="x", content="a", slug="x")

        def _boom(*_a, **_kw):
            raise ArtifactError("refusing to remove sensitive path: ~/...")

        monkeypatch.setattr(isolated_store, "delete", _boom)
        resp = await api_artifact_delete(_request(match={"slug": "x"}))
        assert resp.status == 500
        assert "sensitive path" in _json_body(resp)["error"]


class TestReprobeNotice:
    """Tests for ``POST /api/artifacts/{slug}/publish/reprobe-notice`` — the
    re-probe route that brings a stale publish notice up to date."""

    @pytest.mark.asyncio
    async def test_reprobe_returns_updated_artifact(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        """The handler delegates to ``publish_sync.reprobe_notice`` and returns
        the reconciled artifact (200)."""
        import kiro_crew.publish_sync as _ps
        from kiro_crew.artifacts import ArtifactPublication

        isolated_store.create(name="x", content="a", slug="x")
        isolated_store.set_publication(
            "x",
            ArtifactPublication(
                provider="default",
                artifact_id="uuid-1",
                view_url="https://d/x",
                visibility="PUBLIC",
                notice="still rolling out",
                notice_code="rolling_out",
            ),
        )

        async def _reconcile(slug):
            # Simulate the destination having gone healthy: clear both halves.
            return isolated_store.update_publication(slug, notice="", notice_code="")

        monkeypatch.setattr(_ps, "reprobe_notice", _reconcile)
        resp = await api_artifact_reprobe_notice(_request(match={"slug": "x"}))
        assert resp.status == 200
        pub = _json_body(resp)["publication"]
        assert pub["notice"] == ""
        assert pub["notice_code"] == ""

    @pytest.mark.asyncio
    async def test_reprobe_restricted_session_is_denied(
        self, isolated_store, patch_restricted
    ) -> None:
        """A restricted session cannot reprobe (403), matching the other
        meta.json-mutating routes."""
        isolated_store.create(name="x", content="a", slug="x")
        resp = await api_artifact_reprobe_notice(_request(match={"slug": "x"}, restricted=True))
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_reprobe_missing_artifact_returns_404(
        self, isolated_store, patch_restricted
    ) -> None:
        resp = await api_artifact_reprobe_notice(_request(match={"slug": "nope"}))
        assert resp.status == 404


# ── Versions ────────────────────────────────────────────────────────────────


class TestVersions:
    @pytest.mark.asyncio
    async def test_lists_versions(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        isolated_store.update("x", content="v2", snapshot=True)
        resp = await api_artifact_versions(_request(match={"slug": "x"}))
        assert resp.status == 200
        assert _json_body(resp) == {"slug": "x", "versions": [1, 2]}

    @pytest.mark.asyncio
    async def test_specific_version(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        isolated_store.update("x", content="v2")
        resp = await api_artifact_version_detail(_request(match={"slug": "x", "version": "1"}))
        body = _json_body(resp)
        assert body["content"] == "v1"

    @pytest.mark.asyncio
    async def test_invalid_version_returns_400(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_version_detail(_request(match={"slug": "x", "version": "abc"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_out_of_range_404(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_version_detail(_request(match={"slug": "x", "version": "99"}))
        assert resp.status == 404
