"""The artifact action routes beside create, read, update and delete.

Covers ``POST /api/artifacts/{slug}/events`` (the ``referenced`` impression a widget logs,
and the content-mutating types it refuses), the SEL audit on denials of the materialize,
pin and session-doc routes, relocate's confinement of ``source_path`` to the allowed
roots, teardown's restricted-session deny and confirm gate, and the leave-time ``settle``
that deletes an abandoned blank, saves a draft or keeps an invested document. MagicMock
requests and a real store from ``artifacts_handlers_helpers.py``.
"""

from __future__ import annotations

import json

import pytest
from artifacts_handlers_helpers import (  # noqa: F401 -- fixtures are requested by name
    _json_body,
    _request,
    isolated_store_fixture,
    patch_restricted_fixture,
)

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.dashboard.handlers.artifacts import (
    api_artifact_materialize,
    api_artifact_relocate,
    api_artifact_session_docs,
    api_artifact_set_pinned,
    api_artifact_settle_blank,
)

# ── Record events (referenced) ─────────────────────────────────────────────


class TestRecordEvent:
    """Tests for ``POST /api/artifacts/<slug>/events``.

    This endpoint exists so ``WidgetFrame`` can log a ``referenced`` event
    each time a chat impression of a saved artifact mounts. It
    deliberately rejects content-mutating event types —
    those have to come through create/update so version-bump bookkeeping
    stays coupled to actual content changes.
    """

    @pytest.mark.asyncio
    async def test_referenced_event_recorded_with_metadata(
        self, isolated_store, patch_restricted
    ) -> None:
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        resp = await api_artifact_record_event(
            _request(
                match={"slug": "x"},
                body={
                    "type": "referenced",
                    "metadata": {
                        "message_ts": "1779995123.456789",
                        "widget_index": 0,
                    },
                },
            )
        )
        assert resp.status == 200
        body = _json_body(resp)
        assert body["slug"] == "x"
        assert body["event"]["type"] == "referenced"
        assert body["event"]["by"] == "user"  # X-Internal-Secret absent
        assert body["event"]["metadata"]["message_ts"] == "1779995123.456789"
        assert body["event"]["metadata"]["widget_index"] == 0
        # Verify the event is persisted in the artifact's event log.
        art = isolated_store.get("x")
        ref_events = [e for e in art.events if e.get("type") == "referenced"]
        assert len(ref_events) == 1

    @pytest.mark.asyncio
    async def test_mcp_actor_inferred_from_internal_secret(
        self, isolated_store, patch_restricted
    ) -> None:
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        resp = await api_artifact_record_event(
            _request(
                match={"slug": "x"},
                body={"type": "referenced"},
                extra_headers={"X-Internal-Secret": "anything-non-empty"},
            )
        )
        assert resp.status == 200
        assert _json_body(resp)["event"]["by"] == "agent"

    @pytest.mark.asyncio
    async def test_dashboard_ui_session_dropped(self, isolated_store, patch_restricted) -> None:
        # Browser dashboard sends X-Session-Key="dashboard:ui" as a default
        # for non-chat-scoped requests. That literal isn't a real slot key
        # and would mislead the activity timeline if recorded — the handler
        # explicitly drops it. Same rule as the create/update endpoints.
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        resp = await api_artifact_record_event(
            _request(
                match={"slug": "x"},
                body={"type": "referenced"},
                session_key="dashboard:ui",
            )
        )
        assert resp.status == 200
        # No session_id key in the event.
        assert "session_id" not in _json_body(resp)["event"]

    @pytest.mark.asyncio
    async def test_real_session_key_recorded(self, isolated_store, patch_restricted) -> None:
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        resp = await api_artifact_record_event(
            _request(
                match={"slug": "x"},
                body={"type": "referenced"},
                session_key="chat-3-1779995123",
            )
        )
        assert resp.status == 200
        assert _json_body(resp)["event"]["session_id"] == "chat-3-1779995123"

    @pytest.mark.asyncio
    async def test_rejects_content_mutating_event_types(
        self, isolated_store, patch_restricted
    ) -> None:
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        for bad_type in ("created", "edited", "iterated", "reverted"):
            resp = await api_artifact_record_event(
                _request(
                    match={"slug": "x"},
                    body={"type": bad_type},
                )
            )
            assert resp.status == 400, f"expected 400 for type={bad_type!r}, got {resp.status}"

    @pytest.mark.asyncio
    async def test_unknown_slug_returns_404(self, isolated_store, patch_restricted) -> None:
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        resp = await api_artifact_record_event(
            _request(
                match={"slug": "does-not-exist"},
                body={"type": "referenced"},
            )
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_restricted_session_forbidden(self, isolated_store, patch_restricted) -> None:
        # Appending events mutates meta.json, so a restricted session must
        # be rejected with 403 like the other mutation endpoints — it must
        # not be able to flood an artifact's event log.
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        resp = await api_artifact_record_event(
            _request(
                match={"slug": "x"},
                body={"type": "referenced"},
                restricted=True,
            )
        )
        assert resp.status == 403
        # No event recorded.
        art = isolated_store.get("x")
        assert [e for e in art.events if e.get("type") == "referenced"] == []

    @pytest.mark.asyncio
    async def test_suppressed_response_when_session_has_cud(
        self, isolated_store, patch_restricted
    ) -> None:
        # When the session already has a CUD event, the impression is
        # suppressed: the handler must return suppressed:true with a null
        # event, NOT a stale prior event echoed as if it were recorded.
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        isolated_store.update(
            "x",
            content="<div>v2</div>",
            session_id="chat-9-1779995123",
            actor="agent",
            snapshot=True,
        )
        resp = await api_artifact_record_event(
            _request(
                match={"slug": "x"},
                body={"type": "referenced"},
                session_key="chat-9-1779995123",
            )
        )
        assert resp.status == 200
        body = _json_body(resp)
        assert body["suppressed"] is True
        assert body["event"] is None
        assert [e for e in isolated_store.get("x").events if e.get("type") == "referenced"] == []

    @pytest.mark.asyncio
    async def test_same_impression_in_session_recorded_once(
        self, isolated_store, patch_restricted
    ) -> None:
        # The reported bug: reloading / revisiting a tab clears
        # the frontend sessionStorage debounce, so the same chat session
        # re-POSTs a `referenced` for the same impression. The store must
        # record it once — the second POST returns suppressed:true.
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        body = {"type": "referenced", "metadata": {"message_ts": "1780036091.1", "widget_index": 0}}
        first = await api_artifact_record_event(
            _request(match={"slug": "x"}, body=body, session_key="chat-2-1780036091")
        )
        second = await api_artifact_record_event(
            _request(match={"slug": "x"}, body=body, session_key="chat-2-1780036091")
        )
        assert _json_body(first)["event"]["type"] == "referenced"
        assert _json_body(second)["suppressed"] is True
        ref = [e for e in isolated_store.get("x").events if e.get("type") == "referenced"]
        assert len(ref) == 1

    @pytest.mark.asyncio
    async def test_does_not_bump_version_or_change_content(
        self, isolated_store, patch_restricted
    ) -> None:
        # A referenced event is pure observability — it must not touch
        # the artifact's content or version. Regression guard so a
        # future refactor that accidentally routes referenced events
        # through update() (which DOES bump on content change) doesn't
        # silently turn impression-logging into a version-churn engine.
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        art = isolated_store.create(name="X", content="<div>orig</div>", slug="x", kind="widget")
        original_version = art.version
        original_content = isolated_store.get("x").content

        await api_artifact_record_event(
            _request(match={"slug": "x"}, body={"type": "referenced"}, session_key="chat-a-1")
        )
        await api_artifact_record_event(
            _request(match={"slug": "x"}, body={"type": "referenced"}, session_key="chat-b-2")
        )
        await api_artifact_record_event(
            _request(match={"slug": "x"}, body={"type": "referenced"}, session_key="chat-c-3")
        )

        post = isolated_store.get("x")
        assert post.version == original_version
        assert post.content == original_content
        ref_events = [e for e in post.events if e.get("type") == "referenced"]
        assert len(ref_events) == 3


# ── Denial audit (SEL) for new pin / materialize / session-doc routes ─────────


class TestDenialAudit:
    """Every denial/error exit on the new routes must emit a SEL audit event."""

    def _capture_sel(self, monkeypatch):
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.handlers import artifacts as art_handlers

        sel_stub = MagicMock()
        monkeypatch.setattr(art_handlers, "sel", lambda: sel_stub)
        return sel_stub

    @pytest.mark.asyncio
    async def test_materialize_non_string_path_is_audited(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        sel_stub = self._capture_sel(monkeypatch)
        resp = await api_artifact_materialize(_request(body={"path": 123}))
        assert resp.status == 400
        sel_stub.log_tool_invocation.assert_called_once()
        kwargs = sel_stub.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "artifact_materialize"
        assert kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_materialize_restricted_is_audited(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        sel_stub = self._capture_sel(monkeypatch)
        resp = await api_artifact_materialize(_request(body={"path": "/x.md"}, restricted=True))
        assert resp.status == 403
        assert sel_stub.log_tool_invocation.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_set_pinned_non_bool_is_audited(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        sel_stub = self._capture_sel(monkeypatch)
        resp = await api_artifact_set_pinned(_request(match={"slug": "x"}, body={"pinned": "yes"}))
        assert resp.status == 400
        kwargs = sel_stub.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "artifact_set_pinned"
        assert kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_session_docs_restricted_is_audited(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        sel_stub = self._capture_sel(monkeypatch)
        resp = await api_artifact_session_docs(_request(restricted=True))
        assert resp.status == 403
        kwargs = sel_stub.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "artifact_session_docs"
        assert kwargs["outcome"] == "denied"


# ── Relocate (fixed-root containment) ─────────────────────────────────────────


class TestRelocate:
    """api_artifact_relocate confines source_path to $HOME (+ configured roots),
    so an agent cannot aim an artifact at /etc/passwd or another user's files
    and exfiltrate them via a later GET (PR #14 alice + CodeQL py/path-injection)."""

    @pytest.mark.asyncio
    async def test_home_file_allowed(self, isolated_store, patch_restricted, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
        target = home / "notes.md"
        target.write_text("# hi")
        isolated_store.create(name="Doc", content="x", slug="doc", kind="markdown")
        resp = await api_artifact_relocate(
            _request(match={"slug": "doc"}, body={"source_path": str(target)})
        )
        assert resp.status == 200, _json_body(resp)
        assert isolated_store.get("doc").source_path == str(target.resolve())

    @pytest.mark.asyncio
    async def test_outside_home_denied(
        self, isolated_store, patch_restricted, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
        # The barrier now shares the store's root set, which includes the DATA
        # HOME (the store root's parent). Re-root the store under the fake home
        # — mirroring production's ~/.kiro/crew/artifacts — so "outside home"
        # is genuinely outside every allowed root and the denial still means
        # something.
        store = ArtifactStore(root=home / ".kiro" / "crew" / "artifacts")
        monkeypatch.setattr(art_mod, "_default_store", store)
        # A file OUTSIDE home (sibling tmp dir) must be refused with 403.
        outside = tmp_path / "outside" / "secret.txt"
        outside.parent.mkdir()
        outside.write_text("secret")
        store.create(name="Doc", content="x", slug="doc", kind="markdown")
        resp = await api_artifact_relocate(
            _request(match={"slug": "doc"}, body={"source_path": str(outside)})
        )
        assert resp.status == 403
        assert "home" in _json_body(resp)["error"].lower()

    @pytest.mark.asyncio
    async def test_data_home_root_allowed_third_site_pin(
        self, isolated_store, patch_restricted, tmp_path, monkeypatch
    ):
        """The relocate barrier is the THIRD consumer of the allowed-roots set.

        Its private copy omitted the data-home root, so it refused paths the
        store's own read/write barriers accept. Sharing
        ``ArtifactStore.allowed_source_roots()`` closes that gap — pinned here
        because the two sites disagreeing is invisible until a user hits it.
        """
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
        # isolated_store is rooted at tmp_path/artifacts → data home == tmp_path.
        target = tmp_path / "in-data-home.md"
        target.write_text("# hi", encoding="utf-8")
        isolated_store.create(name="Doc", content="x", slug="doc", kind="markdown")
        resp = await api_artifact_relocate(
            _request(match={"slug": "doc"}, body={"source_path": str(target)})
        )
        assert resp.status == 200, _json_body(resp)
        # …and the store agrees, which is the whole point of one producer.
        assert isolated_store._try_read_source_path(str(target)) == "# hi"
        assert isolated_store._try_write_source_path(str(target), "# edited") is True

    @pytest.mark.asyncio
    async def test_configured_extra_root_allowed(
        self, isolated_store, patch_restricted, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
        extra = tmp_path / "shared"
        extra.mkdir()
        target = extra / "doc.md"
        target.write_text("# shared")
        # Configure the extra root via publish.relocate_roots.
        from kiro_crew.config.loader import KiroCrewConfig, PublishConfig

        cfg = KiroCrewConfig()
        cfg.publish = PublishConfig(relocate_roots=[str(extra)])
        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))
        isolated_store.create(name="Doc", content="x", slug="doc", kind="markdown")
        resp = await api_artifact_relocate(
            _request(match={"slug": "doc"}, body={"source_path": str(target)})
        )
        assert resp.status == 200, _json_body(resp)

    @pytest.mark.asyncio
    async def test_traversal_denied(self, isolated_store, patch_restricted, monkeypatch):
        isolated_store.create(name="Doc", content="x", slug="doc", kind="markdown")
        resp = await api_artifact_relocate(
            _request(match={"slug": "doc"}, body={"source_path": "../../etc/passwd"})
        )
        assert resp.status == 403
        assert "traversal" in _json_body(resp)["error"].lower()


# ── Teardown ────────────────────────────────────────────────────────────────


class TestTeardown:
    """Tests for _handle_teardown restricted-session deny + confirm gate."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse class-scoped restricted-session patch moved verbatim; no test in this class calls monkeypatch.undo()
    def patch_deploy_restricted(self, monkeypatch):
        """Patch _is_restricted_session at the import point used by deploy handlers."""
        from kiro_crew.dashboard.handlers import _shared

        def _stub(_state, req) -> bool:
            return req.app.get("_restricted_session", False)

        monkeypatch.setattr(_shared, "_is_restricted_session", _stub)

    @pytest.mark.asyncio
    async def test_restricted_session_denied(self, isolated_store, patch_restricted) -> None:
        from kiro_crew.deploy.handlers import _handle_teardown

        req = _request(match={"slug": "x"}, body={"confirm": True}, restricted=True)
        resp = await _handle_teardown(req)
        assert resp.status == 403
        assert "restricted session" in json.loads(resp.body)["error"]

    @pytest.mark.asyncio
    async def test_missing_confirm_returns_400(self, isolated_store, patch_restricted) -> None:
        from kiro_crew.deploy.handlers import _handle_teardown

        req = _request(match={"slug": "x"}, body={})
        resp = await _handle_teardown(req)
        assert resp.status == 400
        assert "confirm" in json.loads(resp.body)["error"]

    @pytest.mark.asyncio
    async def test_confirm_false_returns_400(self, isolated_store, patch_restricted) -> None:
        from kiro_crew.deploy.handlers import _handle_teardown

        req = _request(match={"slug": "x"}, body={"confirm": False})
        resp = await _handle_teardown(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_not_found_returns_404(self, isolated_store, patch_restricted) -> None:
        from kiro_crew.deploy.handlers import _handle_teardown

        req = _request(match={"slug": "nonexistent"}, body={"confirm": True})
        resp = await _handle_teardown(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_success_includes_infra_note(self, isolated_store, patch_restricted) -> None:
        from kiro_crew.deploy.handlers import _handle_teardown

        # Create a webapp artifact to tear down
        isolated_store.create(
            name="My App",
            content="<h1>App</h1>",
            slug="my-app",
            kind="webapp",
            webapp_metadata={
                "lifecycle": {"status": "deployed"},
                "deploy_target": {"public_url": "https://x.cloudfront.net/my-app/"},
            },
        )
        req = _request(match={"slug": "my-app"}, body={"confirm": True})
        resp = await _handle_teardown(req)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["ok"] is True
        # R39 F2: this fixture records no deploy profile, so there is no
        # manifest to expire — "skipped" (a true write failure is now a
        # retryable 502, never a silent success).
        assert body["manifest"] == "skipped"
        assert "reaper" in body["note"]

    @pytest.mark.asyncio
    async def test_manifest_expiry_success(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        """When engine.run_aws succeeds, manifest field should be 'expired-now'."""
        from kiro_crew.deploy import engine
        from kiro_crew.deploy import profiles as profiles_mod
        from kiro_crew.deploy.handlers import _handle_teardown

        # Register the profile so _expire_manifest_best_effort passes the registry check.
        reg = profiles_mod.load_registry()
        reg["profiles"].append(profiles_mod.make_entry("test-profile", "us-west-2"))
        reg["default"] = "test-profile"
        profiles_mod.save_registry(reg)

        isolated_store.create(
            name="Expire App",
            content="<h1>App</h1>",
            slug="expire-app",
            kind="webapp",
            webapp_metadata={
                "lifecycle": {"status": "deployed", "created_at": "2026-01-01T00:00:00Z"},
                # R17 F1: fail-closed identity check requires BOTH the metadata
                # and the existing manifest to carry a matching distribution_id.
                "deploy_target": {
                    "profile": "test-profile",
                    "region": "us-west-2",
                    "distribution_id": "EDEPLOY1",
                },
                "slug": "expire-app",
            },
        )

        calls: list[tuple] = []

        def mock_run_aws(args, profile, timeout=30):
            calls.append((args, profile))
            import json as _j

            if "describe-stacks" in args:
                outputs = [
                    {"OutputKey": "BucketName", "OutputValue": "test-bucket"},
                    {"OutputKey": "DistributionId", "OutputValue": "E123"},
                    {"OutputKey": "DistributionDomainName", "OutputValue": "d.cloudfront.net"},
                ]
                return (0, _j.dumps(outputs), "")
            if "s3" in args and "cp" in args:
                if "-" in args:  # manifest READ (download to stdout)
                    return (
                        0,
                        _j.dumps(
                            {"slug": "expire-app", "distribution_id": "EDEPLOY1", "arch": "engine"}
                        ),
                        "",
                    )
                return (0, "", "")
            return (0, "", "")

        monkeypatch.setattr(engine, "run_aws", mock_run_aws)
        req = _request(match={"slug": "expire-app"}, body={"confirm": True})
        resp = await _handle_teardown(req)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["manifest"] == "expired-now"
        # R12 F2: manifest expiry now READS the existing manifest first, then
        # WRITES the patched one — two s3 cp calls, both on the same key.
        s3_calls = [c for c in calls if "s3" in c[0] and "cp" in c[0]]
        assert len(s3_calls) == 2
        for call in s3_calls:
            assert any("expire-app/.kirocrew-deploy.json" in a for a in call[0])

    @pytest.mark.asyncio
    async def test_teardown_fail_closed_when_aws_unreachable(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        """When engine.run_aws fails, the reaper check cannot be confirmed —
        teardown must fail closed (409, NO tombstone) rather than expiring the
        manifest into the void (round-18: reaper-not-installed guard)."""
        from kiro_crew.deploy import engine
        from kiro_crew.deploy.handlers import _handle_teardown

        isolated_store.create(
            name="Fail App",
            content="<h1>App</h1>",
            slug="fail-app",
            kind="webapp",
            webapp_metadata={
                "lifecycle": {"status": "deployed"},
                "deploy_target": {"profile": "bad-profile", "region": "us-west-2"},
                "slug": "fail-app",
            },
        )

        def mock_run_aws(args, profile, timeout=30):
            return (1, "", "AccessDenied")

        monkeypatch.setattr(engine, "run_aws", mock_run_aws)
        # F2 (r4): register the profile so this test exercises the
        # AWS-unreachable path, not the unregistered-profile 409.
        from kiro_crew.deploy import profiles as _profiles_mod

        monkeypatch.setattr(
            _profiles_mod,
            "load_registry",
            lambda: {
                "version": 2,
                "profiles": [{"name": "bad-profile", "region": "us-west-2"}],
                "default": "bad-profile",
            },
        )
        req = _request(match={"slug": "fail-app"}, body={"confirm": True})
        resp = await _handle_teardown(req)
        assert resp.status == 409
        body = json.loads(resp.body)
        assert "reaper" in body.get("error", "").lower()
        # Artifact must NOT be tombstoned — teardown did not actually happen.
        art = isolated_store.get("fail-app")
        assert art.webapp_metadata.lifecycle.status != "expired"


class TestSettleBlankEndpoint:
    """``POST /api/artifacts/{slug}/settle`` -- the atomic leave-time resolution."""

    UNTITLED = "Untitled"

    @pytest.mark.asyncio
    async def test_deletes_an_abandoned_blank(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name=self.UNTITLED, content="", slug="u")
        resp = await api_artifact_settle_blank(
            _request(body={"untitled_name": self.UNTITLED, "draft": ""}, match={"slug": "u"})
        )
        assert resp.status == 200
        assert _json_body(resp)["outcome"] == "deleted"

    @pytest.mark.asyncio
    async def test_saves_a_draft(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name=self.UNTITLED, content="", slug="u")
        resp = await api_artifact_settle_blank(
            _request(body={"untitled_name": self.UNTITLED, "draft": "# notes"}, match={"slug": "u"})
        )
        assert _json_body(resp)["outcome"] == "saved"
        assert isolated_store.get("u").content == "# notes"

    @pytest.mark.asyncio
    async def test_keeps_an_invested_document(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="Release plan", content="# real", slug="u")
        resp = await api_artifact_settle_blank(
            _request(body={"untitled_name": self.UNTITLED, "draft": "# stale"}, match={"slug": "u"})
        )
        assert _json_body(resp)["outcome"] == "kept"
        assert isolated_store.get("u").content == "# real"

    @pytest.mark.asyncio
    async def test_requires_the_untitled_placeholder(
        self, isolated_store, patch_restricted
    ) -> None:
        """Without it the store cannot tell an unnamed document from a named one,
        and guessing would risk deleting a named document."""
        isolated_store.create(name=self.UNTITLED, content="", slug="u")
        resp = await api_artifact_settle_blank(_request(body={"draft": ""}, match={"slug": "u"}))
        assert resp.status == 400
        assert isolated_store.get("u") is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("draft", [[], {}, 7])
    async def test_refuses_a_non_string_draft(
        self, isolated_store, patch_restricted, draft
    ) -> None:
        isolated_store.create(name=self.UNTITLED, content="", slug="u")
        resp = await api_artifact_settle_blank(
            _request(body={"untitled_name": self.UNTITLED, "draft": draft}, match={"slug": "u"})
        )
        assert resp.status == 400
        assert isolated_store.get("u") is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body,reason",
        [
            pytest.param({"draft": ""}, "untitled_name is required", id="missing-placeholder"),
            pytest.param(
                {"untitled_name": "  ", "draft": ""},
                "untitled_name is required",
                id="blank-placeholder",
            ),
            pytest.param(
                {"untitled_name": "Untitled", "draft": []},
                "draft must be a string",
                id="non-string-draft",
            ),
        ],
    )
    async def test_every_rejection_is_audited(
        self, isolated_store, patch_restricted, monkeypatch, body, reason
    ) -> None:
        """This endpoint can DELETE a document, so a refused attempt is as
        interesting to an auditor as a successful one -- a silent 400 would leave
        no trace of it."""
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.handlers import artifacts as art_handlers

        sel_stub = MagicMock()
        monkeypatch.setattr(art_handlers, "sel", lambda: sel_stub)
        isolated_store.create(name="Untitled", content="", slug="u")
        resp = await api_artifact_settle_blank(_request(body=body, match={"slug": "u"}))
        assert resp.status == 400
        kwargs = sel_stub.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "artifact_settle_blank"
        assert kwargs["outcome"] == "denied"
        assert reason in kwargs["error"]

    @pytest.mark.asyncio
    async def test_allow_delete_false_rescues_the_draft_without_deleting(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name=self.UNTITLED, content="", slug="u")
        resp = await api_artifact_settle_blank(
            _request(
                body={"untitled_name": self.UNTITLED, "draft": "# typed", "allow_delete": False},
                match={"slug": "u"},
            )
        )
        assert _json_body(resp)["outcome"] == "saved"
        assert isolated_store.get("u").content == "# typed"

    @pytest.mark.asyncio
    async def test_allow_delete_false_keeps_an_empty_shell(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name=self.UNTITLED, content="", slug="u")
        resp = await api_artifact_settle_blank(
            _request(
                body={"untitled_name": self.UNTITLED, "draft": "", "allow_delete": False},
                match={"slug": "u"},
            )
        )
        assert _json_body(resp)["outcome"] == "kept"
        assert isolated_store.get("u") is not None

    @pytest.mark.asyncio
    async def test_refuses_a_non_boolean_allow_delete(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name=self.UNTITLED, content="", slug="u")
        resp = await api_artifact_settle_blank(
            _request(
                body={"untitled_name": self.UNTITLED, "draft": "", "allow_delete": "no"},
                match={"slug": "u"},
            )
        )
        assert resp.status == 400
        assert isolated_store.get("u") is not None

    @pytest.mark.asyncio
    async def test_an_already_gone_document_is_not_an_error(
        self, isolated_store, patch_restricted
    ) -> None:
        """Double navigation, or deleted in another window -- nothing to settle."""
        resp = await api_artifact_settle_blank(
            _request(body={"untitled_name": self.UNTITLED, "draft": ""}, match={"slug": "gone"})
        )
        assert resp.status == 200
        assert _json_body(resp)["outcome"] == "kept"
