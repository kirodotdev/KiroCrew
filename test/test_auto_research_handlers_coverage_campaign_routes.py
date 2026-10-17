"""Coverage for the auto-research routes that act on one campaign (``handlers.py``).

The report, nudge and add-question routes; artifact export and the knowledge
library, with their 404 / 409 / 503 taxonomy; the loop, suspend, question and
findings helpers behind them; ``_handle_action``'s worker dispatch; undecodable
bodies; and emergent exploration. Isolation is the shape
``test_auto_research_handlers_coverage.py`` uses, from
``auto_research_handlers_coverage_helpers.py``: ``DB_PATH`` / ``RESEARCH_DIR`` in
``tmp_path``, every boundary patched, and handlers invoked through
``make_mocked_request``, so no network, no subprocess and no real gateway.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from auto_research_handlers_coverage_helpers import _isolate_fixture  # noqa: F401
from auto_research_handlers_coverage_helpers import _no_autonudge  # noqa: F401
from auto_research_handlers_coverage_helpers import _no_stray_sse_queues  # noqa: F401
from auto_research_handlers_coverage_helpers import sse_fixture  # noqa: F401
from auto_research_handlers_coverage_helpers import (
    _app,
    _body,
    _campaign,
    _mk,
    _running,
    _status,
    _write_finding,
)

from kiro_crew.apps.builtins.auto_research import handlers as h

# --- report / nudge / add-question ----------------------------------------


class TestReportAndNudge:
    def test_read_report_of_an_invalid_id_is_empty(self):
        assert h._read_report("../etc") == ""

    def test_read_report_of_an_unreadable_file_is_empty(self, _isolate: Path):
        cid = _campaign()
        report = h._campaign_dir(cid) / "FINDINGS.md"
        report.write_text("body")
        with mock.patch.object(Path, "read_text", side_effect=OSError("nope")):
            assert h._read_report(cid) == ""

    @pytest.mark.asyncio
    async def test_report_endpoint_serves_the_findings_file(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("# Key finding")
        resp = await h._handle_report(
            _mk("GET", f"campaigns/{cid}/report", app=_app(), match={"id": cid})
        )
        assert _body(resp)["report"] == "# Key finding"

    @pytest.mark.asyncio
    async def test_nudge_is_refused_in_workflow_mode(self, _isolate: Path):
        cid = _campaign(execution_mode="workflow")
        resp = await h._handle_nudge(
            _mk("POST", f"campaigns/{cid}/nudge", app=_app(), match={"id": cid}, body={"text": "x"})
        )
        assert resp.status == 409
        assert "workflow mode" in _body(resp)["error"]

    @pytest.mark.asyncio
    async def test_nudge_requires_text(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_nudge(
            _mk("POST", f"campaigns/{cid}/nudge", app=_app(), match={"id": cid}, body={"text": ""})
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "text required"

    @pytest.mark.asyncio
    async def test_add_question_is_refused_in_workflow_mode(self, _isolate: Path):
        cid = _campaign(execution_mode="workflow")
        resp = await h._handle_add_question(
            _mk(
                "POST",
                f"campaigns/{cid}/questions",
                app=_app(),
                match={"id": cid},
                body={"text": "q"},
            )
        )
        assert resp.status == 409

    @pytest.mark.asyncio
    async def test_add_question_requires_text(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_add_question(
            _mk(
                "POST",
                f"campaigns/{cid}/questions",
                app=_app(),
                match={"id": cid},
                body={"text": " "},
            )
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_add_question_of_a_missing_campaign_is_a_404(self, _isolate: Path):
        _campaign()
        resp = await h._handle_add_question(
            _mk(
                "POST",
                "campaigns/deadbeef/questions",
                app=_app(),
                match={"id": "deadbeef"},
                body={"text": "q"},
            )
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_add_question_appends_and_rewrites_the_brief(self, _isolate: Path, sse):
        cid = _campaign()
        resp = await h._handle_add_question(
            _mk(
                "POST",
                f"campaigns/{cid}/questions",
                app=_app(),
                match={"id": cid},
                body={"text": "What does the cap cost?"},
            )
        )
        subs = _body(resp)["sub_questions"]
        assert subs == [{"text": "What does the cap cost?", "origin": "manual", "status": "open"}]
        brief = (h._campaign_dir(cid) / "brief.md").read_text()
        assert "What does the cap cost?" in brief
        assert sse.types() == ["question_added"]


# --- artifact export ------------------------------------------------------


class _FakeArtifact:
    def __init__(self, slug: str) -> None:
        self.slug = slug


class TestArtifactRoutes:
    @pytest.mark.asyncio
    async def test_report_status_without_the_artifact_system(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(h, "_HAS_ARTIFACTS", False)
        cid = _campaign()
        resp = await h._handle_report_status(_mk("GET", "s", app=_app(), match={"id": cid}))
        assert _body(resp) == {"slug": None}

    @pytest.mark.asyncio
    async def test_report_status_of_a_missing_campaign_is_a_404(self, _isolate: Path):
        _campaign()
        resp = await h._handle_report_status(_mk("GET", "s", app=_app(), match={"id": "deadbeef"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_report_status_without_an_export_is_null(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_report_status(_mk("GET", "s", app=_app(), match={"id": cid}))
        assert _body(resp) == {"slug": None}

    @pytest.mark.asyncio
    async def test_report_status_returns_a_live_slug(self, _isolate: Path):
        cid = _campaign()
        _set_slug(cid, "slug-1")
        store = MagicMock()
        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_report_status(_mk("GET", "s", app=_app(), match={"id": cid}))
        assert _body(resp) == {"slug": "slug-1"}
        store.get.assert_called_once_with("slug-1")

    @pytest.mark.asyncio
    async def test_report_status_hides_a_deleted_artifact(self, _isolate: Path):
        cid = _campaign()
        _set_slug(cid, "slug-1")
        store = MagicMock()
        store.get.side_effect = h.ArtifactNotFoundError("gone")
        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_report_status(_mk("GET", "s", app=_app(), match={"id": cid}))
        assert _body(resp) == {"slug": None}

    @pytest.mark.asyncio
    async def test_report_status_survives_a_broken_store(self, _isolate: Path):
        cid = _campaign()
        _set_slug(cid, "slug-1")
        store = MagicMock()
        store.get.side_effect = RuntimeError("store on fire")
        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_report_status(_mk("GET", "s", app=_app(), match={"id": cid}))
        assert _body(resp) == {"slug": None}

    @pytest.mark.asyncio
    async def test_to_artifact_without_the_artifact_system_is_a_503(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(h, "_HAS_ARTIFACTS", False)
        cid = _campaign()
        resp = await h._handle_to_artifact(_mk("POST", "a", app=_app(), match={"id": cid}))
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_to_artifact_without_findings_is_a_404(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_to_artifact(_mk("POST", "a", app=_app(), match={"id": cid}))
        assert resp.status == 404
        assert _body(resp)["error"] == "No findings yet"

    @pytest.mark.asyncio
    async def test_to_artifact_falls_back_to_a_mechanical_render(self, _isolate: Path):
        cid = _campaign(sub_questions=[{"text": "Sub one", "status": "answered"}])
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("Line one\n\nLine two")
        store = MagicMock()
        store.create.return_value = _FakeArtifact("slug-new")
        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_to_artifact(_mk("POST", "a", app=_app(), match={"id": cid}))
        assert resp.status == 201
        assert _body(resp) == {
            "slug": "slug-new",
            "name": _body(resp)["name"],
            "regenerated": False,
        }
        html = store.create.call_args.kwargs["content"]
        assert "<!DOCTYPE html>" in html
        assert "Line one" in html
        assert "✅ Sub one" in html
        assert _get_slug(cid) == "slug-new"

    @pytest.mark.asyncio
    async def test_to_artifact_prefers_the_llm_authored_html(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings")
        pool = SimpleNamespace(
            send=AsyncMock(return_value="```html\n<!DOCTYPE html><p>authored</p>\n```")
        )
        store = MagicMock()
        store.create.return_value = _FakeArtifact("slug-new")
        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_to_artifact(
                _mk("POST", "a", app=_app(auto_research_llm_pool=pool), match={"id": cid})
            )
        assert resp.status == 201
        assert store.create.call_args.kwargs["content"] == "<!DOCTYPE html><p>authored</p>"

    @pytest.mark.asyncio
    async def test_to_artifact_falls_back_when_the_llm_fails(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings")
        pool = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("pool down")))
        store = MagicMock()
        store.create.return_value = _FakeArtifact("slug-new")
        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_to_artifact(
                _mk("POST", "a", app=_app(auto_research_llm_pool=pool), match={"id": cid})
            )
        assert resp.status == 201
        assert "<!DOCTYPE html>" in store.create.call_args.kwargs["content"]

    @pytest.mark.asyncio
    async def test_to_artifact_reuses_a_live_slug_as_a_new_version(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings")
        _set_slug(cid, "slug-old")
        store = MagicMock()
        store.update.return_value = _FakeArtifact("slug-old")
        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_to_artifact(_mk("POST", "a", app=_app(), match={"id": cid}))
        assert resp.status == 200
        assert _body(resp)["regenerated"] is True
        store.create.assert_not_called()
        assert store.update.call_args.kwargs["snapshot"] is True

    @pytest.mark.asyncio
    async def test_to_artifact_rebinds_when_the_stored_slug_is_dead(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings")
        _set_slug(cid, "slug-dead")
        store = MagicMock()
        store.get.side_effect = h.ArtifactNotFoundError("gone")
        store.create.return_value = _FakeArtifact("slug-fresh")
        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_to_artifact(_mk("POST", "a", app=_app(), match={"id": cid}))
        assert resp.status == 201
        assert _body(resp)["regenerated"] is False
        assert _get_slug(cid) == "slug-fresh"

    def test_render_escapes_hostile_findings_and_bare_sub_questions(self):
        html = h._render_findings_html(
            "<script>q</script>",
            ["bare one", {"text": "d", "origin": "emergent"}],
            "a\n\nb",
            3,
            "a1b2c3d4",
        )
        assert "<script>q</script>" not in html
        assert "&lt;script&gt;" in html
        assert "🔍 bare one" in html
        assert "(emergent)" in html
        assert "3 cycles" in html


def _set_slug(cid: str, slug: str) -> None:
    db = h._get_db()
    db.execute("UPDATE campaigns SET report_artifact_slug = ? WHERE id = ?", (slug, cid))
    db.commit()
    db.close()


def _get_slug(cid: str) -> str | None:
    db = h._get_db()
    row = db.execute("SELECT report_artifact_slug FROM campaigns WHERE id = ?", (cid,)).fetchone()
    db.close()
    return row["report_artifact_slug"] if row else None


# --- knowledge library ----------------------------------------------------


class TestKnowledgeRoutes:
    @pytest.mark.asyncio
    async def test_status_without_a_knowledge_store_is_false(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_knowledge_status(_mk("GET", "k", app=_app(), match={"id": cid}))
        assert _body(resp) == {"in_library": False}

    @pytest.mark.asyncio
    async def test_status_reports_an_existing_source(self, _isolate: Path):
        cid = _campaign()
        store = MagicMock()
        store.get_source_by_uri.return_value = {"id": 7}
        app = _app(state=SimpleNamespace(knowledge_store=store))
        resp = await h._handle_knowledge_status(_mk("GET", "k", app=app, match={"id": cid}))
        assert _body(resp) == {"in_library": True, "source_id": 7}
        expected = str((h._campaign_dir(cid) / "findings_for_knowledge.md").resolve())
        store.get_source_by_uri.assert_called_once_with(expected)

    @pytest.mark.asyncio
    async def test_status_reports_absence(self, _isolate: Path):
        cid = _campaign()
        store = MagicMock()
        store.get_source_by_uri.return_value = None
        app = _app(state=SimpleNamespace(knowledge_store=store))
        resp = await h._handle_knowledge_status(_mk("GET", "k", app=app, match={"id": cid}))
        assert _body(resp) == {"in_library": False}

    @pytest.mark.asyncio
    async def test_status_survives_a_broken_store(self, _isolate: Path):
        cid = _campaign()
        store = MagicMock()
        store.get_source_by_uri.side_effect = RuntimeError("index corrupt")
        app = _app(state=SimpleNamespace(knowledge_store=store))
        resp = await h._handle_knowledge_status(_mk("GET", "k", app=app, match={"id": cid}))
        assert _body(resp) == {"in_library": False}

    @pytest.mark.asyncio
    async def test_ingest_without_findings_is_a_404(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_to_knowledge(_mk("POST", "k", app=_app(), match={"id": cid}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_ingest_without_a_store_is_a_503(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings")
        resp = await h._handle_to_knowledge(_mk("POST", "k", app=_app(), match={"id": cid}))
        assert resp.status == 503
        assert "Knowledge Library unavailable" in _body(resp)["error"]

    @pytest.mark.asyncio
    async def test_ingest_without_a_pipeline_is_a_503(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings")
        app = _app(state=SimpleNamespace(knowledge_store=MagicMock()))
        resp = await h._handle_to_knowledge(_mk("POST", "k", app=app, match={"id": cid}))
        assert resp.status == 503
        assert "pipeline" in _body(resp)["error"]

    @pytest.mark.asyncio
    async def test_ingest_refuses_a_duplicate(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings")
        store = MagicMock()
        store.get_source_by_uri.return_value = {"id": 3}
        app = _app(
            state=SimpleNamespace(knowledge_store=store),
            knowledge_pipeline=SimpleNamespace(ingest_file=AsyncMock()),
        )
        resp = await h._handle_to_knowledge(_mk("POST", "k", app=app, match={"id": cid}))
        assert resp.status == 409
        assert _body(resp) == {"error": "Already in Knowledge Library", "id": 3}

    @pytest.mark.asyncio
    async def test_ingest_writes_a_sanitized_copy_and_marks_it_synced(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings body")
        store = MagicMock()
        store.get_source_by_uri.return_value = None
        store.add_source.return_value = 11
        pipeline = SimpleNamespace(ingest_file=AsyncMock())
        app = _app(state=SimpleNamespace(knowledge_store=store), knowledge_pipeline=pipeline)
        resp = await h._handle_to_knowledge(_mk("POST", "k", app=app, match={"id": cid}))
        assert resp.status == 201
        assert _body(resp) == {"id": 11, "status": "ingesting"}
        sanitized = h._campaign_dir(cid) / "findings_for_knowledge.md"
        assert sanitized.read_text() == "findings body"
        await _drain_bg_tasks(app)
        pipeline.ingest_file.assert_awaited_once()
        statuses = [c.args[0] for c in store.db.execute.call_args_list]
        assert any("'synced'" in s for s in statuses)

    @pytest.mark.asyncio
    async def test_ingest_store_statements_run_off_the_event_loop(self, _isolate: Path):
        """Every store statement for the
        to-knowledge flow must run in a worker thread, so a lock wait on the
        store's busy timeout stalls a thread instead of the event loop (which
        the watchdog would kill). Pins add_source, the syncing/synced UPDATEs,
        and their commits to non-loop threads."""
        import threading

        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings body")
        loop_thread = threading.get_ident()
        seen_threads: list[int] = []

        def _record(*_a, **_k):
            seen_threads.append(threading.get_ident())
            return MagicMock()

        store = MagicMock()
        store.get_source_by_uri.side_effect = lambda *_a: (_record(), None)[1]
        store.add_source.side_effect = lambda **_k: (_record(), 11)[1]
        store.db.execute.side_effect = _record
        store.db.commit.side_effect = _record
        pipeline = SimpleNamespace(ingest_file=AsyncMock())
        app = _app(state=SimpleNamespace(knowledge_store=store), knowledge_pipeline=pipeline)
        resp = await h._handle_to_knowledge(_mk("POST", "k", app=app, match={"id": cid}))
        assert resp.status == 201
        await _drain_bg_tasks(app)
        # get_source_by_uri + add_source + 2 UPDATEs + 2 commits all recorded,
        # none on the loop.
        assert len(seen_threads) >= 6
        assert all(t != loop_thread for t in seen_threads)

    @pytest.mark.asyncio
    async def test_ingest_failure_marks_the_source_errored(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings body")
        store = MagicMock()
        store.get_source_by_uri.return_value = None
        store.add_source.return_value = 12
        pipeline = SimpleNamespace(ingest_file=AsyncMock(side_effect=RuntimeError("boom")))
        app = _app(state=SimpleNamespace(knowledge_store=store), knowledge_pipeline=pipeline)
        await h._handle_to_knowledge(_mk("POST", "k", app=app, match={"id": cid}))
        await _drain_bg_tasks(app)
        statuses = [c.args[0] for c in store.db.execute.call_args_list]
        assert any("'error'" in s for s in statuses)


async def _drain_bg_tasks(app: web.Application) -> None:
    tasks = list(app.get("_bg_tasks") or ())
    if tasks:
        # flake-ok: pre-existing gather with no timeout of the handler's background tasks, moved verbatim; tracked for a de-flake pass
        await asyncio.gather(*tasks, return_exceptions=True)


# --- assorted helpers -----------------------------------------------------


class TestAssortedHelpers:
    @pytest.mark.asyncio
    async def test_launch_loop_of_an_unknown_campaign_stops_after_the_row_lookup(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _campaign()  # create the schema
        state = SimpleNamespace(get_or_create_slot=MagicMock())
        svc = SimpleNamespace(add=AsyncMock(), get_by_slot=MagicMock(return_value=None))
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        await h._launch_loop(_mk("PATCH", "x", app=_app(state=state)), "deadbeef")
        state.get_or_create_slot.assert_not_called()
        svc.add.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_launch_loop_arms_the_worker_without_a_conversation_log(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        cid = _campaign(name="Rate limiting study")
        slot = SimpleNamespace(key=f"research-{cid}", title="", _titled=False, _trust=False)
        state = SimpleNamespace(
            get_or_create_slot=MagicMock(return_value=slot),
            push_slot_title=MagicMock(),
            push_slots_update=MagicMock(),
            conversation_log=None,
        )
        svc = SimpleNamespace(add=AsyncMock(), get_by_slot=MagicMock(return_value=None))
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        await h._launch_loop(_mk("PATCH", "x", app=_app(state=state)), cid)
        assert slot.title == "Rate limiting study"
        assert slot._titled is True
        assert slot._trust is True
        state.push_slot_title.assert_called_once()
        svc.add.assert_awaited_once()
        assert svc.add.await_args.kwargs["slot_key"] == slot.key

    @pytest.mark.asyncio
    async def test_suspend_deactivates_research_loops_and_clears_trust(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        research = SimpleNamespace(id="l1", slot_key="research-a1b2c3d4", active=True)
        other = SimpleNamespace(id="l2", slot_key="chat-1", active=True)
        svc = SimpleNamespace(
            list_all=MagicMock(return_value=[research, other]), update=AsyncMock()
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        slot = SimpleNamespace(_trust=True)
        await h._suspend_research_loops_while_disabled(
            SimpleNamespace(_slots={"research-a1b2c3d4": slot})
        )
        svc.update.assert_awaited_once_with("l1", active=False)
        assert slot._trust is False

    @pytest.mark.asyncio
    async def test_suspend_tolerates_a_failing_deactivation(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        loop = SimpleNamespace(id="l1", slot_key="research-a1b2c3d4", active=True)
        svc = SimpleNamespace(
            list_all=MagicMock(return_value=[loop]), update=AsyncMock(side_effect=RuntimeError("x"))
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        await h._suspend_research_loops_while_disabled(None)
        svc.update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_suspend_without_a_service_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(h, "_autonudge_instance", lambda: None)
        await h._suspend_research_loops_while_disabled(None)

    @pytest.mark.asyncio
    async def test_stop_loop_without_a_service_or_loop_is_a_no_op(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        await h._stop_loop("a1b2c3d4", remove=True)  # no autonudge at all
        svc = SimpleNamespace(get_by_slot=MagicMock(return_value=None), remove=AsyncMock())
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        await h._stop_loop("a1b2c3d4", remove=True)
        svc.remove.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("remove", [True, False], ids=["remove", "pause"])
    async def test_stop_loop_removes_or_deactivates(
        self, remove: bool, monkeypatch: pytest.MonkeyPatch
    ):
        loop = SimpleNamespace(id="l1")
        svc = SimpleNamespace(
            get_by_slot=MagicMock(return_value=loop), remove=AsyncMock(), update=AsyncMock()
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        await h._stop_loop("a1b2c3d4", remove=remove, stop_reason="campaign_stopped")
        if remove:
            svc.remove.assert_awaited_once_with("l1", stop_reason="campaign_stopped")
        else:
            svc.update.assert_awaited_once_with("l1", active=False)

    def test_audit_without_the_sel_module_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(h, "sel", None)
        h._audit("campaign_created", "a1b2c3d4")  # must not raise

    def test_redaction_fails_closed_without_the_security_module(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(h, "_HAS_SECURITY", False)
        out = h._redact_finding(
            {"s": "secret", "l": ["a", {"n": "b"}], "d": {"k": "c"}, "i": 3, "none": None}
        )
        assert out == {
            "s": "[REDACTED]",
            "l": ["[REDACTED]", {"n": "[REDACTED]"}],
            "d": {"k": "[REDACTED]"},
            "i": 3,
            "none": None,
        }

    def test_update_status_rejects_an_invalid_id(self):
        assert h.update_campaign_status("../etc", h.CampaignStatus.RUNNING) == {
            "error": "invalid campaign_id"
        }

    def test_pending_question_reads_and_tolerates_junk(self, _isolate: Path):
        cid = _campaign()
        assert h._pending_question(cid) is None
        qp = h._campaign_dir(cid) / "questions.json"
        qp.write_text('{"question": "Which DB?"}')
        assert h._pending_question(cid) == "Which DB?"
        qp.write_text("{not json")
        assert h._pending_question(cid) is None

    def test_unattended_mode_discards_a_stray_question(self, _isolate: Path):
        cid = _campaign()
        qp = h._campaign_dir(cid) / "questions.json"
        qp.write_text('{"question": "Which DB?"}')
        assert h._should_pause_for_question(cid, True) is False
        assert not qp.exists()

    def test_attended_mode_pauses_on_a_question(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "questions.json").write_text('{"question": "Which DB?"}')
        assert h._should_pause_for_question(cid, False) is True

    def test_no_question_never_pauses(self, _isolate: Path):
        assert h._should_pause_for_question(_campaign(), False) is False

    def test_get_findings_skips_unreadable_files(self, _isolate: Path):
        cid = _campaign()
        _write_finding(cid, 1, summary="good")
        (h._campaign_dir(cid) / "findings" / "cycle_002.json").write_text("{broken")
        findings = h.get_findings(cid)
        assert [f["summary"] for f in findings] == ["good"]

    def test_get_findings_of_a_campaign_without_a_dir_is_empty(self, _isolate: Path):
        assert h.get_findings("a1b2c3d4") == []

    def test_delete_of_an_invalid_id_reports_an_error(self):
        assert h.delete_campaign("../etc") == {"error": "invalid campaign_id"}

    def test_fork_name_does_not_double_prefix(self):
        once = h._fork_name("Rate limiting")
        assert once == h._fork_name(once)

    def test_reserve_zone_math(self):
        assert h._reserve_cycles(30, 0.15) == 5
        assert h._reserve_cycles(0, 0.15) == 0
        assert h._in_reserve_zone(24, 30, 0.15) is False
        assert h._in_reserve_zone(25, 30, 0.15) is True
        assert h._in_reserve_zone(99, 0, 0.15) is False

    def test_cycle_files_of_a_missing_dir_is_empty(self, tmp_path: Path):
        assert h._cycle_finding_files(tmp_path / "nowhere") == []

    def test_stagnation_of_an_invalid_id_is_false(self):
        assert h.check_stagnation("../etc") is False

    def test_worker_done_of_an_invalid_id_is_absent(self):
        assert h._read_worker_done("../etc") is None

    def test_worker_done_rejects_a_non_regular_marker(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / h._WORKER_DONE_FILENAME).mkdir()
        assert h._read_worker_done(cid) is None

    def test_clear_marker_of_an_invalid_id_is_a_no_op(self):
        h._clear_worker_done_marker("../etc")  # must not raise

    def test_tree_node_redaction_passes_primitives_and_recurses_lists(self):
        assert h._redact_tree_node(7) == 7
        assert h._redact_tree_node(None) is None
        assert h._redact_tree_node(["plain", ["nested"]]) == ["plain", ["nested"]]


class TestActionDispatch:
    """``_handle_action``'s worker dispatch: agent loop vs Dynamic Workflow run."""

    @pytest.fixture
    def dispatch(self, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
        calls = SimpleNamespace(
            launch_workflow=AsyncMock(),
            stop_workflow=AsyncMock(),
            launch_loop=AsyncMock(),
            stop_loop=AsyncMock(),
        )
        monkeypatch.setattr(h, "_launch_workflow", calls.launch_workflow)
        monkeypatch.setattr(h, "_stop_workflow", calls.stop_workflow)
        monkeypatch.setattr(h, "_launch_loop", calls.launch_loop)
        monkeypatch.setattr(h, "_stop_loop", calls.stop_loop)
        return calls

    async def _act(self, cid: str, action: str) -> web.StreamResponse:
        return await h._handle_action(
            _mk("PATCH", f"campaigns/{cid}", app=_app(), match={"id": cid}, body={"action": action})
        )

    @pytest.mark.asyncio
    async def test_malformed_body_is_a_400(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_action(
            _mk("PATCH", f"campaigns/{cid}", app=_app(), match={"id": cid}, body=None)
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_action_is_a_400(self, _isolate: Path):
        resp = await self._act(_campaign(), "teleport")
        assert resp.status == 400
        assert "Unknown action" in _body(resp)["error"]

    @pytest.mark.asyncio
    async def test_action_on_a_missing_campaign_is_a_404(self, _isolate: Path):
        _campaign()
        resp = await self._act("deadbeef", "start")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_start_on_a_running_campaign_is_a_409(self, _isolate: Path, dispatch):
        cid = _campaign()
        _running(cid)
        resp = await self._act(cid, "start")
        assert resp.status == 409
        dispatch.launch_loop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_status_write_is_reported_as_a_404(
        self, _isolate: Path, dispatch, monkeypatch: pytest.MonkeyPatch
    ):
        cid = _campaign()
        monkeypatch.setattr(h, "update_campaign_status", lambda *a, **k: {"error": "vanished"})
        resp = await self._act(cid, "start")
        assert resp.status == 404
        dispatch.launch_loop.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["start", "resume"], ids=["start", "resume"])
    async def test_workflow_mode_start_launches_a_run(self, _isolate: Path, dispatch, action):
        cid = _campaign(execution_mode="workflow")
        if action == "resume":
            _running(cid)
            h.update_campaign_status(cid, h.CampaignStatus.PAUSED)
        resp = await self._act(cid, action)
        assert resp.status == 200
        dispatch.launch_workflow.assert_awaited_once()
        dispatch.launch_loop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_agent_mode_start_arms_the_loop(self, _isolate: Path, dispatch):
        cid = _campaign()
        await self._act(cid, "start")
        dispatch.launch_loop.assert_awaited_once()
        dispatch.launch_workflow.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["agent", "workflow"])
    async def test_pause_stops_the_right_worker(self, _isolate: Path, dispatch, mode):
        cid = _campaign(execution_mode=mode)
        _running(cid)
        resp = await self._act(cid, "pause")
        assert resp.status == 200
        if mode == "workflow":
            dispatch.stop_workflow.assert_awaited_once()
            dispatch.stop_loop.assert_not_awaited()
        else:
            dispatch.stop_loop.assert_awaited_once_with(cid, remove=False)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["agent", "workflow"])
    async def test_stop_tears_the_right_worker_down(self, _isolate: Path, dispatch, mode):
        cid = _campaign(execution_mode=mode)
        _running(cid)
        resp = await self._act(cid, "stop")
        assert resp.status == 200
        if mode == "workflow":
            dispatch.stop_workflow.assert_awaited_once()
        else:
            dispatch.stop_loop.assert_awaited_once_with(
                cid, remove=True, stop_reason="campaign_stopped"
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["agent", "workflow"])
    async def test_delete_tears_the_right_worker_down(self, _isolate: Path, dispatch, mode):
        cid = _campaign(execution_mode=mode)
        _running(cid)
        resp = await h._handle_delete(
            _mk("DELETE", f"campaigns/{cid}", app=_app(), match={"id": cid})
        )
        assert resp.status == 200
        if mode == "workflow":
            dispatch.stop_workflow.assert_awaited_once()
        else:
            dispatch.stop_loop.assert_awaited_once_with(
                cid, remove=True, stop_reason="campaign_deleted"
            )

    @pytest.mark.asyncio
    async def test_delete_of_a_missing_campaign_is_a_404(self, _isolate: Path, dispatch):
        _campaign()
        resp = await h._handle_delete(
            _mk("DELETE", "campaigns/deadbeef", app=_app(), match={"id": "deadbeef"})
        )
        assert resp.status == 404


class TestMalformedBodies:
    @pytest.mark.asyncio
    async def test_create_rejects_an_undecodable_body(self, _isolate: Path):
        resp = await h._handle_create(_mk("POST", "campaigns", app=_app(), body=None))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_nudge_rejects_an_undecodable_body(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_nudge(_mk("POST", "n", app=_app(), match={"id": cid}, body=None))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_add_question_rejects_an_undecodable_body(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_add_question(
            _mk("POST", "q", app=_app(), match={"id": cid}, body=None)
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_nudge_clears_a_pending_question_and_resumes(self, _isolate: Path):
        cid = _campaign()
        _running(cid)
        h.update_campaign_status(cid, h.CampaignStatus.NEEDS_INPUT)
        (h._campaign_dir(cid) / "questions.json").write_text('{"question": "Which DB?"}')
        resp = await h._handle_nudge(
            _mk("POST", "n", app=_app(), match={"id": cid}, body={"text": "Use SQLite"})
        )
        assert _body(resp) == {"ok": True}
        assert not (h._campaign_dir(cid) / "questions.json").exists()
        assert _status(cid) == h.CampaignStatus.RUNNING

    @pytest.mark.asyncio
    async def test_to_artifact_of_an_orphaned_findings_dir_is_a_404(self, _isolate: Path):
        _campaign()  # create the schema
        (h._campaign_dir("deadbeef") / "FINDINGS.md").write_text("orphan")
        store = MagicMock()
        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_to_artifact(
                _mk("POST", "a", app=_app(), match={"id": "deadbeef"})
            )
        assert resp.status == 404
        store.create.assert_not_called()


class TestEmergentExploration:
    def test_ingest_of_an_invalid_id_is_empty(self):
        assert h._ingest_emergent_questions("../etc") == []

    def test_ingest_consumes_a_malformed_file(self, _isolate: Path):
        cid = _campaign()
        emergent = h._campaign_dir(cid) / h._EMERGENT_FILENAME
        emergent.write_text("{not json")
        assert h._ingest_emergent_questions(cid) == []
        assert not emergent.exists()  # consumed regardless of validity

    def test_ingest_consumes_a_non_list_payload(self, _isolate: Path):
        cid = _campaign()
        emergent = h._campaign_dir(cid) / h._EMERGENT_FILENAME
        emergent.write_text(json.dumps({"text": "not a list"}))
        assert h._ingest_emergent_questions(cid) == []
        assert not emergent.exists()

    def test_ingest_accepts_bare_string_items(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / h._EMERGENT_FILENAME).write_text(
            json.dumps(["What is the retry budget?"])
        )
        admitted = h._ingest_emergent_questions(cid)
        assert [a["text"] for a in admitted] == ["What is the retry budget?"]

    def test_ingest_discards_the_file_in_workflow_mode(self, _isolate: Path):
        cid = _campaign(execution_mode="workflow")
        emergent = h._campaign_dir(cid) / h._EMERGENT_FILENAME
        emergent.write_text(json.dumps(["ignored"]))
        assert h._ingest_emergent_questions(cid) == []
        assert not emergent.exists()

    def test_activate_of_an_invalid_id_is_empty(self):
        assert h._activate_emergent("../etc") == []

    def test_activate_is_skipped_in_workflow_mode(self, _isolate: Path):
        cid = _campaign(execution_mode="workflow")
        self._seed_pending(cid, ["queued question"])
        assert h._activate_emergent(cid) == []

    def test_activate_with_a_zero_budget_activates_nothing(self, _isolate: Path):
        cid = _campaign(max_subquestions_per_round=0)
        self._seed_pending(cid, ["queued question"])
        assert h._activate_emergent(cid) == []

    @staticmethod
    def _seed_pending(cid: str, texts: list[str]) -> None:
        from kiro_crew.apps.builtins.auto_research import subquestion_queue as sq

        d = h._campaign_dir(cid)
        queue = sq.load_queue(d)
        sq.enqueue(queue, [{"text": t, "priority": 0.9} for t in texts], depth=1, max_admit=5)
        sq.save_queue(d, queue)
        assert sq.pending_count(queue) == len(texts)

    def test_finalize_mode_is_signaled_only_once(self, _isolate: Path):
        cid = _campaign()
        assert h._enter_finalize(cid) is True
        assert h._enter_finalize(cid) is False  # flag already on disk
        assert "FINALIZE MODE" in (h._campaign_dir(cid) / "guidance.txt").read_text()

    def test_finalize_of_an_invalid_id_is_false(self):
        assert h._enter_finalize("../etc") is False

    def test_advance_never_raises(self, _isolate: Path, monkeypatch: pytest.MonkeyPatch):
        cid = _campaign()
        monkeypatch.setattr(h, "_should_finalize", MagicMock(side_effect=RuntimeError("db gone")))
        h._advance_exploration(cid)  # swallowed — the watchdog must survive
