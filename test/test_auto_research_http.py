"""auto_research campaign HTTP routes, driven through aiohttp's TestClient.

Campaign validate, create, list and get; the start, stop, resume and fork actions with
their 409, 400 and 404 replies; nudges and questions; the knowledge and artifact routes;
the redacted grill-tree read; the 401 auth refusal; and two watchdog-settlement races
that run through the resume route. ``test_auto_research.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from auto_research_helpers import isolate_fixture  # noqa: F401 - fixture

from kiro_crew.apps.builtins.auto_research.handlers import (
    CampaignStatus,
    create_campaign,
    get_campaign,
    update_campaign_status,
)
from kiro_crew.testing.fixtures import one_pool_thread  # noqa: F401 (fixture)
from kiro_crew.testing.wait import async_wait_until, default_timeout

# --- HTTP Handler tests ---


class TestHTTPHandlers:
    @pytest.fixture
    def app(self, tmp_path: Path):
        from aiohttp import web

        from kiro_crew.apps.builtins.auto_research.handlers import register_routes

        @web.middleware
        async def _inject_user(request, handler):
            request["user"] = "test-user"
            return await handler(request)

        with (
            patch(
                "kiro_crew.apps.builtins.auto_research.handlers.DB_PATH",
                tmp_path / "t.db",
            ),
            patch(
                "kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR",
                tmp_path / "r",
            ),
        ):
            a = web.Application(middlewares=[_inject_user])
            register_routes(a)
            yield a

    @pytest.mark.asyncio
    async def test_validate(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/validate",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                assert r.status == 200
                assert (await r.json())["can_start"] is True

    @pytest.mark.asyncio
    async def test_nudge_resumes_needs_input(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                cr = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await cr.json())["id"]
                (h._campaign_dir(cid) / "questions.json").write_text('{"question": "Which DB?"}')
                r = await c.post(
                    f"/api/apps/auto-research/campaigns/{cid}/nudge", json={"text": "Use SQLite"}
                )
                assert r.status == 200
                # Answering clears the question and writes the guidance.
                assert not (h._campaign_dir(cid) / "questions.json").exists()
                assert (h._campaign_dir(cid) / "guidance.txt").read_text(
                    encoding="utf-8"
                ) == "Use SQLite"

    @pytest.mark.asyncio
    async def test_report_endpoint(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                cr = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await cr.json())["id"]
                (h._campaign_dir(cid) / "FINDINGS.md").write_text("# Report\nKey finding.")
                r = await c.get(f"/api/apps/auto-research/campaigns/{cid}/report")
                assert r.status == 200
                assert "Key finding." in (await r.json())["report"]

    @pytest.mark.asyncio
    async def test_create_list_get(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                assert r.status == 201
                cid = (await r.json())["id"]
                r = await c.get("/api/apps/auto-research/campaigns")
                assert r.status == 200
                assert len(await r.json()) == 1
                r = await c.get(f"/api/apps/auto-research/campaigns/{cid}")
                assert r.status == 200

    @pytest.mark.asyncio
    async def test_action_start_stop(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await r.json())["id"]
                r = await c.patch(
                    f"/api/apps/auto-research/campaigns/{cid}", json={"action": "start"}
                )
                assert (await r.json())["status"] == "running"
                r = await c.patch(
                    f"/api/apps/auto-research/campaigns/{cid}", json={"action": "stop"}
                )
                assert (await r.json())["status"] == "stopped"

    @pytest.mark.asyncio
    async def test_delete_campaign(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                cr = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await cr.json())["id"]
                (h._campaign_dir(cid) / "FINDINGS.md").write_text("# Report")
                r = await c.delete(f"/api/apps/auto-research/campaigns/{cid}")
                assert r.status == 200
                assert (await r.json())["deleted"] is True
                assert await (await c.get("/api/apps/auto-research/campaigns")).json() == []
                assert (await c.delete(f"/api/apps/auto-research/campaigns/{cid}")).status == 404

    @pytest.mark.asyncio
    async def test_action_start_on_running_returns_409(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                cr = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await cr.json())["id"]
                await c.patch(f"/api/apps/auto-research/campaigns/{cid}", json={"action": "start"})
                # Re-issuing start on a running campaign must be rejected, not relaunch.
                r = await c.patch(
                    f"/api/apps/auto-research/campaigns/{cid}", json={"action": "start"}
                )
                assert r.status == 409

    @pytest.mark.asyncio
    async def test_action_resume_from_failed_clears_error(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                cr = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await cr.json())["id"]
                update_campaign_status(cid, CampaignStatus.FAILED, error_message="stalled")
                # A failed campaign is recoverable: resume must succeed (not 409)
                # and clear the stale failure message.
                r = await c.patch(
                    f"/api/apps/auto-research/campaigns/{cid}", json={"action": "resume"}
                )
                assert r.status == 200
                camp = await (await c.get(f"/api/apps/auto-research/campaigns/{cid}")).json()
                assert camp["status"] == "running"
                assert not camp["error_message"]

    @pytest.mark.asyncio
    async def test_action_prepares_resume_before_publishing_running(self, app, tmp_path: Path):
        """Old stop evidence is removed before the watchdog can see RUNNING."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h

        observed = []

        async def _prepare(cid: str) -> None:
            observed.append(("prepare", get_campaign(cid)["status"]))
            # flake-ok: one yield inside the stub prepare, moved verbatim; nothing waits on it
            await asyncio.sleep(0)

        async def _launch(_request, cid: str, *, prepared: bool = False) -> None:
            observed.append(("launch", prepared, get_campaign(cid)["status"]))

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
            patch.object(h, "_prepare_loop_launch", side_effect=_prepare),
            patch.object(h, "_launch_loop", side_effect=_launch),
        ):
            async with TestClient(TestServer(app)) as c:
                cr = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await cr.json())["id"]
                update_campaign_status(cid, CampaignStatus.FAILED, error_message="stalled")
                r = await c.patch(
                    f"/api/apps/auto-research/campaigns/{cid}", json={"action": "resume"}
                )

        assert r.status == 200
        assert observed == [
            ("prepare", CampaignStatus.FAILED),
            ("launch", True, CampaignStatus.RUNNING),
        ]

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("one_pool_thread")
    async def test_resume_cannot_be_overwritten_by_slow_terminal_sidecar(
        self, app, tmp_path: Path, monkeypatch
    ):
        """Terminal settlement finishes before Resume publishes its new run."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h
        from kiro_crew.autonudge import AUTONUDGE_STOP_REASON, AutoNudgeService

        svc = AutoNudgeService(base_dir=tmp_path / "autonudge")
        await svc.start()
        state = MagicMock()
        state.conversation_log = None
        app["state"] = state
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        monkeypatch.setattr(h, "is_app_enabled", lambda _name: False)
        monkeypatch.setattr(
            h,
            "_stalled_campaign_verdict",
            lambda _cid, _files, *, stopped_reason="": (CampaignStatus.STOPPED, None),
        )

        terminal_sidecar_started = threading.Event()
        release_terminal_sidecar = threading.Event()
        real_write_status = h.write_status

        def _slow_terminal_sidecar(cid: str, status: str, **extra):
            if status == CampaignStatus.STOPPED:
                terminal_sidecar_started.set()
                assert release_terminal_sidecar.wait(
                    timeout=default_timeout()
                ), "the test never released the terminal sidecar"
            real_write_status(cid, status, **extra)

        monkeypatch.setattr(h, "write_status", _slow_terminal_sidecar)

        try:
            campaign = create_campaign(
                {
                    "question": "Research a sufficiently detailed question here",
                    "sources": ["web"],
                }
            )
            cid = campaign["id"]
            update_campaign_status(cid, CampaignStatus.RUNNING)
            observed_started_at = get_campaign(cid)["started_at"]
            slot_key = f"research-{cid}"
            slot = SimpleNamespace(key=slot_key)
            state.get_or_create_slot.return_value = slot
            state.get_slot.return_value = slot
            old_loop = await svc.add(slot_key=slot_key, message="old run", idle_secs=60)
            await svc.update(
                old_loop.id,
                active=False,
                stopped_reason=AUTONUDGE_STOP_REASON,
            )

            settlement = asyncio.create_task(
                h._settle_campaign_from_watchdog(
                    cid,
                    [],
                    {},
                    {},
                    observed_started_at=observed_started_at,
                    stopped_reason=AUTONUDGE_STOP_REASON,
                )
            )
            await async_wait_until(
                terminal_sidecar_started.is_set,
                describe=lambda: "the terminal status sidecar write never started",
            )

            async with TestClient(TestServer(app)) as client:
                resume = asyncio.create_task(
                    client.patch(
                        f"/api/apps/auto-research/campaigns/{cid}",
                        json={"action": "resume"},
                    )
                )
                # flake-ok: ten-turn yield barrier before a negative assert, pre-existing shape, moved verbatim; tracked for a de-flake pass
                for _ in range(10):
                    await asyncio.sleep(0)
                assert not resume.done()

                release_terminal_sidecar.set()
                # flake-ok: unbounded await of a task this test just released, pre-existing shape, moved verbatim; tracked for a de-flake pass
                await settlement
                # flake-ok: unbounded await of a request this test just released, pre-existing shape, moved verbatim; tracked for a de-flake pass
                response = await resume
                assert response.status == 200

            assert get_campaign(cid)["status"] == CampaignStatus.RUNNING
            status_payload = json.loads((h._campaign_dir(cid) / "status.json").read_text())
            assert status_payload["status"] == CampaignStatus.RUNNING
            replacement = svc.get_by_slot(slot_key)
            assert replacement is not None
            assert replacement.id != old_loop.id
        finally:
            release_terminal_sidecar.set()
            svc.stop()

    @pytest.mark.asyncio
    async def test_stale_settlement_skips_replacement_run_effects(
        self, _isolate: Path, monkeypatch
    ):
        """A watchdog observation cannot settle a later run generation."""
        from kiro_crew.apps.builtins.auto_research import handlers as h
        from kiro_crew.autonudge import AutoNudgeService

        campaign = create_campaign(
            {
                "question": "Research a sufficiently detailed question here",
                "sources": ["web"],
            }
        )
        cid = campaign["id"]
        update_campaign_status(cid, CampaignStatus.RUNNING)
        old_started_at = get_campaign(cid)["started_at"]
        update_campaign_status(cid, CampaignStatus.STOPPED)
        update_campaign_status(cid, CampaignStatus.RUNNING)
        # flake-ok: two product time.time() run stamps compared for inequality, pre-existing shape, moved verbatim; tracked for a de-flake pass
        assert get_campaign(cid)["started_at"] != old_started_at

        svc = AutoNudgeService(base_dir=_isolate / "autonudge")
        await svc.start()
        replacement = await svc.add(
            slot_key=f"research-{cid}",
            message="replacement run",
            idle_secs=60,
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        verdict = MagicMock(side_effect=AssertionError("stale run was classified"))
        monkeypatch.setattr(h, "_stalled_campaign_verdict", verdict)
        events = []
        monkeypatch.setattr(h, "_emit_sse", events.append)
        last_counts = {cid: 1}
        last_ts = {cid: 2.0}

        try:
            await h._settle_campaign_from_watchdog(
                cid,
                [],
                last_counts,
                last_ts,
                observed_started_at=old_started_at,
            )

            assert get_campaign(cid)["status"] == CampaignStatus.RUNNING
            assert svc.get_by_slot(f"research-{cid}") is replacement
            assert last_counts == {cid: 1}
            assert last_ts == {cid: 2.0}
            assert events == []
            verdict.assert_not_called()
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_action_unknown(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await r.json())["id"]
                r = await c.patch(
                    f"/api/apps/auto-research/campaigns/{cid}", json={"action": "boom"}
                )
                assert r.status == 400

    @pytest.mark.asyncio
    async def test_nudge(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await r.json())["id"]
                r = await c.post(
                    f"/api/apps/auto-research/campaigns/{cid}/nudge", json={"text": "focus"}
                )
                assert r.status == 200

    @pytest.mark.asyncio
    async def test_nudge_empty(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await r.json())["id"]
                r = await c.post(
                    f"/api/apps/auto-research/campaigns/{cid}/nudge", json={"text": ""}
                )
                assert r.status == 400

    @pytest.mark.asyncio
    async def test_add_question(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={
                        "question": "How do teams handle rate limiting?",
                        "sources": ["web"],
                        "sub_questions": [{"text": "What patterns exist?", "origin": "grill"}],
                    },
                )
                cid = (await r.json())["id"]
                r = await c.post(
                    f"/api/apps/auto-research/campaigns/{cid}/questions",
                    json={"text": "What about token buckets?"},
                )
                assert r.status == 200
                body = await r.json()
                assert body["ok"] is True
                assert len(body["sub_questions"]) == 2
                assert body["sub_questions"][1]["text"] == "What about token buckets?"
                assert body["sub_questions"][1]["origin"] == "manual"
                assert body["sub_questions"][1]["status"] == "open"
                # Verify brief.md was regenerated with the new question.
                brief = (h._campaign_dir(cid) / "brief.md").read_text(encoding="utf-8")
                assert "What about token buckets?" in brief
                assert "_(user guidance)_" in brief

    @pytest.mark.asyncio
    async def test_add_question_empty(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await r.json())["id"]
                r = await c.post(
                    f"/api/apps/auto-research/campaigns/{cid}/questions", json={"text": ""}
                )
                assert r.status == 400

    @pytest.mark.asyncio
    async def test_add_question_not_found(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns/deadbeef/questions",
                    json={"text": "Something"},
                )
                assert r.status == 404

    @pytest.mark.asyncio
    async def test_to_knowledge_no_findings(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await r.json())["id"]
                r = await c.post(f"/api/apps/auto-research/campaigns/{cid}/to-knowledge", json={})
                assert r.status == 404
                assert "No findings" in (await r.json())["error"]

    @pytest.mark.asyncio
    async def test_to_knowledge_no_pipeline(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await r.json())["id"]
                d = h._campaign_dir(cid)
                (d / "FINDINGS.md").write_text("# Summary\nSomething.")
                # No knowledge store/pipeline in test app → 503
                r = await c.post(f"/api/apps/auto-research/campaigns/{cid}/to-knowledge", json={})
                assert r.status == 503

    @pytest.mark.asyncio
    async def test_to_knowledge_redacts_before_ingest(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h

        # Mock knowledge store + pipeline so the handler reaches the ingest path.
        added: dict = {}

        class _FakeStore:
            def __init__(self) -> None:
                self.db = MagicMock()

            def get_source_by_uri(self, uri):
                return None

            def add_source(self, *, name, source_type, uri, properties):
                added["uri"] = uri
                added["name"] = name
                return "sid1"

        app["state"] = SimpleNamespace(knowledge_store=_FakeStore())
        app["knowledge_pipeline"] = SimpleNamespace(ingest_file=AsyncMock())
        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await r.json())["id"]
                d = h._campaign_dir(cid)
                (d / "FINDINGS.md").write_text(
                    "# Summary\nFound key aws_secret=AKIAIOSFODNN7EXAMPLE in the config."
                )
                r = await c.post(f"/api/apps/auto-research/campaigns/{cid}/to-knowledge", json={})
                assert r.status == 201
                # A sanitized copy is created and the raw credential is gone.
                sanitized = d / "findings_for_knowledge.md"
                assert sanitized.exists()
                assert "AKIAIOSFODNN7EXAMPLE" not in sanitized.read_text(encoding="utf-8")
                # The source ingested is the sanitized file, never raw FINDINGS.md.
                assert added["uri"] == str(sanitized.resolve())

    @pytest.mark.asyncio
    async def test_knowledge_status_unavailable(self, app, tmp_path: Path):
        # No knowledge store in the test app -> graceful {in_library: false},
        # never a 503 for a status probe.
        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                cr = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await cr.json())["id"]
                r = await c.get(f"/api/apps/auto-research/campaigns/{cid}/knowledge-status")
                assert r.status == 200
                assert (await r.json())["in_library"] is False

    @pytest.mark.asyncio
    async def test_knowledge_status_true_and_false(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h

        present = {"v": False}

        class _FakeStore:
            def get_source_by_uri(self, uri):
                return {"id": "sid9"} if present["v"] else None

        app["state"] = SimpleNamespace(knowledge_store=_FakeStore())
        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                cr = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await cr.json())["id"]
                # Not yet in the library.
                r = await c.get(f"/api/apps/auto-research/campaigns/{cid}/knowledge-status")
                assert (await r.json()) == {"in_library": False}
                # Now the dedup key resolves to an existing source.
                present["v"] = True
                r = await c.get(f"/api/apps/auto-research/campaigns/{cid}/knowledge-status")
                body = await r.json()
                assert body["in_library"] is True
                assert body["source_id"] == "sid9"
                # The probe must not write the sanitized file as a side effect.
                assert not (h._campaign_dir(cid) / "findings_for_knowledge.md").exists()

    @pytest.mark.asyncio
    async def test_to_artifact(self, app, tmp_path: Path):
        # Fallback path: when the LLM pool errors, the mechanical render is used
        # (HTML-escaped + redacted). Patch LLMPool so the startup hook creates a
        # pool whose send() raises, deterministically exercising the fallback.
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h
        from kiro_crew.artifacts import ArtifactStore

        class _RaisingPool:
            async def send(self, *a: object, **k: object) -> str:
                raise RuntimeError("no llm in test")

            async def shutdown(self) -> None:
                pass

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
            patch(
                "kiro_crew.apps.builtins.auto_research.handlers.LLMPool",
                lambda *a, **k: _RaisingPool(),
            ),
            patch(
                "kiro_crew.apps.builtins.auto_research.handlers.ArtifactStore",
                return_value=ArtifactStore(root=tmp_path / "artifacts"),
            ),
        ):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={
                        "question": "How do teams handle rate limiting?",
                        "sources": ["web"],
                        "sub_questions": [
                            {"text": "Token bucket?", "origin": "grill"},
                            {"text": "XSS?", "origin": "<script>alert(1)</script>"},
                        ],
                    },
                )
                cid = (await r.json())["id"]
                d = h._campaign_dir(cid)
                # Findings containing a credential-shaped token must be redacted
                # before it lands in the shareable artifact.
                (d / "FINDINGS.md").write_text(
                    "# Summary\nToken bucket is common. aws_secret=AKIAIOSFODNN7EXAMPLE"
                )
                r = await c.post(f"/api/apps/auto-research/campaigns/{cid}/to-artifact", json={})
                assert r.status == 201
                body = await r.json()
                assert "slug" in body
                assert body["slug"]
                # The stored artifact must not contain the raw credential.
                art = ArtifactStore(root=tmp_path / "artifacts").get(body["slug"])
                content = art.content or ""
                assert "AKIAIOSFODNN7EXAMPLE" not in content
                # The malicious origin must be HTML-escaped, not raw.
                assert "<script>alert(1)</script>" not in content
                assert "&lt;script&gt;" in content

    @pytest.mark.asyncio
    async def test_to_artifact_llm_authored(self, app, tmp_path: Path):
        # When the LLM pool is available, the report is LLM-authored (and the
        # ```html code fence the model adds is stripped). Credentials are still
        # redacted on this path.
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h
        from kiro_crew.artifacts import ArtifactStore

        class _FakePool:
            async def send(self, prompt: str, timeout: float = 0) -> str:
                # Include a credential in the LLM's own output so the redaction
                # step on this path is actually exercised (not vacuously true).
                return (
                    "```html\n<!DOCTYPE html><html><body><h1>Nice Report</h1>"
                    "<p>Found aws_secret=AKIAIOSFODNN7EXAMPLE</p>"
                    "</body></html>\n```"
                )

            async def shutdown(self) -> None:
                pass

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
            patch(
                "kiro_crew.apps.builtins.auto_research.handlers.LLMPool",
                lambda *a, **k: _FakePool(),
            ),
            patch(
                "kiro_crew.apps.builtins.auto_research.handlers.ArtifactStore",
                return_value=ArtifactStore(root=tmp_path / "artifacts"),
            ),
        ):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await r.json())["id"]
                d = h._campaign_dir(cid)
                (d / "FINDINGS.md").write_text(
                    "# Summary\nToken bucket. aws_secret=AKIAIOSFODNN7EXAMPLE"
                )
                r = await c.post(f"/api/apps/auto-research/campaigns/{cid}/to-artifact", json={})
                assert r.status == 201
                slug = (await r.json())["slug"]
                content = ArtifactStore(root=tmp_path / "artifacts").get(slug).content or ""
                assert "Nice Report" in content  # LLM-authored HTML was used
                assert "```" not in content  # code fence stripped
                assert "AKIAIOSFODNN7EXAMPLE" not in content  # still redacted

    @pytest.mark.asyncio
    async def test_to_artifact_reuse_regenerates(self, app, tmp_path: Path):
        # Repeated exports update ONE artifact (a new version) instead of
        # spawning a duplicate on every click; the persisted slug is reused.
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h
        from kiro_crew.artifacts import ArtifactStore

        store = ArtifactStore(root=tmp_path / "artifacts")

        class _RaisingPool:
            async def send(self, *a: object, **k: object) -> str:
                raise RuntimeError("no llm in test")

            async def shutdown(self) -> None:
                pass

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
            patch(
                "kiro_crew.apps.builtins.auto_research.handlers.LLMPool",
                lambda *a, **k: _RaisingPool(),
            ),
            patch(
                "kiro_crew.apps.builtins.auto_research.handlers.ArtifactStore", return_value=store
            ),
        ):
            async with TestClient(TestServer(app)) as c:
                cr = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await cr.json())["id"]
                d = h._campaign_dir(cid)
                (d / "FINDINGS.md").write_text("# Summary\nFinding one.")
                r1 = await c.post(f"/api/apps/auto-research/campaigns/{cid}/to-artifact", json={})
                assert r1.status == 201
                b1 = await r1.json()
                assert b1["regenerated"] is False
                slug = b1["slug"]
                # Second export regenerates the SAME artifact (new version).
                (d / "FINDINGS.md").write_text("# Summary\nFinding two, updated.")
                r2 = await c.post(f"/api/apps/auto-research/campaigns/{cid}/to-artifact", json={})
                assert r2.status == 200
                b2 = await r2.json()
                assert b2["regenerated"] is True
                assert b2["slug"] == slug  # reused, not a duplicate
                art = store.get(slug)
                assert art.version >= 2  # regeneration bumped the version
                assert "Finding two, updated." in (art.content or "")

    @pytest.mark.asyncio
    async def test_report_status(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h
        from kiro_crew.artifacts import ArtifactStore

        store = ArtifactStore(root=tmp_path / "artifacts")

        class _RaisingPool:
            async def send(self, *a: object, **k: object) -> str:
                raise RuntimeError("no llm in test")

            async def shutdown(self) -> None:
                pass

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
            patch(
                "kiro_crew.apps.builtins.auto_research.handlers.LLMPool",
                lambda *a, **k: _RaisingPool(),
            ),
            patch(
                "kiro_crew.apps.builtins.auto_research.handlers.ArtifactStore", return_value=store
            ),
        ):
            async with TestClient(TestServer(app)) as c:
                cr = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await cr.json())["id"]
                # No export yet -> slug is null.
                r = await c.get(f"/api/apps/auto-research/campaigns/{cid}/report-status")
                assert r.status == 200
                assert (await r.json())["slug"] is None
                # After exporting, report-status returns the live slug.
                (h._campaign_dir(cid) / "FINDINGS.md").write_text("# Summary\nFinding.")
                er = await c.post(f"/api/apps/auto-research/campaigns/{cid}/to-artifact", json={})
                slug = (await er.json())["slug"]
                r = await c.get(f"/api/apps/auto-research/campaigns/{cid}/report-status")
                assert (await r.json())["slug"] == slug

    @pytest.mark.asyncio
    async def test_to_artifact_no_findings(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await r.json())["id"]
                r = await c.post(f"/api/apps/auto-research/campaigns/{cid}/to-artifact", json={})
                assert r.status == 404

    @pytest.mark.asyncio
    async def test_auth_rejected(self, tmp_path: Path):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research.handlers import register_routes

        # No middleware → no user set → handlers must reject with 401
        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            no_auth_app = web.Application()
            register_routes(no_auth_app)
            async with TestClient(TestServer(no_auth_app)) as c:
                r = await c.get("/api/apps/auto-research/campaigns")
                assert r.status == 401

    @pytest.mark.asyncio
    async def test_get_invalid_id(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                r = await c.get("/api/apps/auto-research/campaigns/ZZZZZZZZ")
                assert r.status == 400

    @pytest.mark.asyncio
    async def test_action_nonexistent_404(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
            patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
        ):
            async with TestClient(TestServer(app)) as c:
                r = await c.patch(
                    "/api/apps/auto-research/campaigns/deadbeef", json={"action": "start"}
                )
                assert r.status == 404


class TestForkAndGrillTreeHTTP:
    @pytest.fixture
    def app(self, tmp_path: Path):
        from aiohttp import web

        from kiro_crew.apps.builtins.auto_research.handlers import register_routes

        @web.middleware
        async def _inject_user(request, handler):
            request["user"] = "test-user"
            return await handler(request)

        with (
            patch(
                "kiro_crew.apps.builtins.auto_research.handlers.DB_PATH",
                tmp_path / "t.db",
            ),
            patch(
                "kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR",
                tmp_path / "r",
            ),
        ):
            a = web.Application(middlewares=[_inject_user])
            register_routes(a)
            yield a

    @pytest.mark.asyncio
    async def test_fork_creates_child_with_parent_link(self, app):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h

        parent = h.create_campaign(
            {
                "question": "Parent question padded long enough here",
                "sub_questions": [],
                "sources": ["web"],
                "max_cycles": 5,
                "model": "test-model-x",  # parent created with an explicit pick
            }
        )
        pid = parent["id"]
        h.update_campaign_status(pid, h.CampaignStatus.COMPLETE)
        (h._campaign_dir(pid) / "FINDINGS.md").write_text("# Parent findings\nsome evidence")

        async with TestClient(TestServer(app)) as c:
            r = await c.patch(
                f"/api/apps/auto-research/campaigns/{pid}",
                json={
                    "action": "fork",
                    "sub_questions": ["Follow-up sub-question one"],
                    "scope_constraints": ["stay on topic"],
                    "max_cycles": 7,
                },
            )
            assert r.status == 201
            child = await r.json()
        child_id = child["id"]
        assert child_id != pid
        assert child["name"].startswith("Forked: ")
        row = h.get_campaign(child_id)
        assert row is not None and row["parent_id"] == pid
        assert row["name"].startswith("Forked: ")
        assert row["model"] == "test-model-x"  # fork continues on the parent's pick
        assert (h._campaign_dir(child_id) / "parent_findings.md").read_text(encoding="utf-8") == (
            "# Parent findings\nsome evidence"
        )

    def test_fork_name_helper(self):
        from kiro_crew.apps.builtins.auto_research.handlers import _fork_name

        # Prefixes, caps at 50 chars, and never double-prefixes a re-fork.
        assert _fork_name("Migrate auth").startswith("Forked: ")
        assert len(_fork_name("x" * 200)) <= 50
        assert _fork_name("Forked: already a fork") == "Forked: already a fork"

    @pytest.mark.asyncio
    async def test_fork_missing_parent_404(self, app):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as c:
            r = await c.patch("/api/apps/auto-research/campaigns/deadbeef", json={"action": "fork"})
            assert r.status == 404

    @pytest.mark.asyncio
    async def test_fork_incomplete_parent_409(self, app):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h

        parent = h.create_campaign(
            {
                "question": "Running parent question padded enough",
                "sub_questions": [],
                "sources": ["web"],
                "max_cycles": 5,
            }
        )
        pid = parent["id"]
        h.update_campaign_status(pid, h.CampaignStatus.RUNNING)
        async with TestClient(TestServer(app)) as c:
            r = await c.patch(f"/api/apps/auto-research/campaigns/{pid}", json={"action": "fork"})
            assert r.status == 409

    @pytest.mark.asyncio
    async def test_grill_tree_returns_persisted_tree(self, app):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h

        tree = [{"id": "n1", "kind": "research", "text": "sub q", "origin": "grill"}]
        camp = h.create_campaign(
            {
                "question": "Persisted tree question padded enough",
                "sub_questions": [],
                "sources": ["web"],
                "max_cycles": 5,
                "grill_tree": tree,
            }
        )
        cid = camp["id"]
        async with TestClient(TestServer(app)) as c:
            r = await c.get(f"/api/apps/auto-research/campaigns/{cid}/grill-tree")
            assert r.status == 200
            body = await r.json()
        assert body["tree"] == tree

    @pytest.mark.asyncio
    async def test_grill_tree_redacts_node_text(self, app):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h

        tree = [{"id": "n1", "kind": "research", "text": "leaked AKIAIOSFODNN7EXAMPLE in node"}]
        camp = h.create_campaign(
            {
                "question": "Redacted tree question padded enough",
                "sub_questions": [],
                "sources": ["web"],
                "max_cycles": 5,
                "grill_tree": tree,
            }
        )
        cid = camp["id"]
        async with TestClient(TestServer(app)) as c:
            r = await c.get(f"/api/apps/auto-research/campaigns/{cid}/grill-tree")
            assert r.status == 200
            body = await r.text()
        assert "AKIAIOSFODNN7EXAMPLE" not in body

    @pytest.mark.asyncio
    async def test_grill_tree_redacts_string_elements(self, app):
        """Non-dict (string) elements are LLM-generated too: a stray string
        from a malformed/drifted model response must be scanned, not served
        unredacted."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h

        camp = h.create_campaign(
            {
                "question": "String node question padded enough",
                "sub_questions": [],
                "sources": ["web"],
                "max_cycles": 5,
            }
        )
        cid = camp["id"]
        # Simulate a malformed tree mixing a dict node with a bare string.
        d = h._safe_campaign_dir(cid)
        assert d is not None
        (d / "grill_tree.json").write_text(
            json.dumps([{"id": "n1", "text": "ok"}, "leaked AKIAIOSFODNN7EXAMPLE here"])
        )
        async with TestClient(TestServer(app)) as c:
            r = await c.get(f"/api/apps/auto-research/campaigns/{cid}/grill-tree")
            assert r.status == 200
            body = await r.text()
        assert "AKIAIOSFODNN7EXAMPLE" not in body

    @pytest.mark.asyncio
    async def test_grill_tree_redacts_nested_list_elements(self, app):
        """A nested list element (schema drift) is scanned recursively — a
        secret buried inside a nested list must not be served unredacted."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h

        camp = h.create_campaign(
            {
                "question": "Nested list question padded enough",
                "sub_questions": [],
                "sources": ["web"],
                "max_cycles": 5,
            }
        )
        cid = camp["id"]
        # A list element nested inside the tree, carrying a secret string.
        d = h._safe_campaign_dir(cid)
        assert d is not None
        (d / "grill_tree.json").write_text(
            json.dumps([{"id": "n1", "text": "ok"}, ["benign", "leaked AKIAIOSFODNN7EXAMPLE here"]])
        )
        async with TestClient(TestServer(app)) as c:
            r = await c.get(f"/api/apps/auto-research/campaigns/{cid}/grill-tree")
            assert r.status == 200
            body = await r.text()
        assert "AKIAIOSFODNN7EXAMPLE" not in body

    @pytest.mark.asyncio
    async def test_grill_tree_non_list_fails_closed(self, app):
        """A non-list payload (file corruption/tampering) is dropped to [] —
        never served unredacted (fail-closed)."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h

        camp = h.create_campaign(
            {
                "question": "Non list question padded long enough",
                "sub_questions": [],
                "sources": ["web"],
                "max_cycles": 5,
            }
        )
        cid = camp["id"]
        # A dict (not a list) with an embedded secret simulates tampering.
        d = h._safe_campaign_dir(cid)
        assert d is not None
        (d / "grill_tree.json").write_text(json.dumps({"text": "leaked AKIAIOSFODNN7EXAMPLE here"}))
        async with TestClient(TestServer(app)) as c:
            r = await c.get(f"/api/apps/auto-research/campaigns/{cid}/grill-tree")
            assert r.status == 200
            body = await r.json()
            text = json.dumps(body)
        assert body["tree"] == []
        assert "AKIAIOSFODNN7EXAMPLE" not in text

    @pytest.mark.asyncio
    async def test_grill_tree_empty_when_absent(self, app):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.auto_research import handlers as h

        camp = h.create_campaign(
            {
                "question": "No tree question padded long enough",
                "sub_questions": [],
                "sources": ["web"],
                "max_cycles": 5,
            }
        )
        cid = camp["id"]
        async with TestClient(TestServer(app)) as c:
            r = await c.get(f"/api/apps/auto-research/campaigns/{cid}/grill-tree")
            assert r.status == 200
            body = await r.json()
        assert body["tree"] == []

    @pytest.mark.asyncio
    async def test_grill_tree_invalid_id_400(self, app):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as c:
            # Non-hex id fails _safe_campaign_dir -> 400 (no traversal/leak).
            r = await c.get("/api/apps/auto-research/campaigns/ZZZZZZZZ/grill-tree")
            assert r.status == 400
