"""Research Lab model surfaces: the fork's model override and the withhold verdict.

The fork flow pre-selects the parent's model and may send a different one; the
campaign list/detail payloads carry the worker slot's withhold verdict and
served model so the page can say when a picked model fell back.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


class _FakeState:
    """Just enough dashboard state for the handlers' worker-slot lookup."""

    def __init__(self, slots: dict[str, object] | None = None) -> None:
        self._slots = slots or {}

    def get_slot(self, name: str):
        return self._slots.get(name)


@pytest.fixture
def make_app(tmp_path: Path):
    from aiohttp import web

    from kiro_crew.apps.builtins.auto_research.handlers import register_routes

    @web.middleware
    async def _inject_user(request, handler):
        request["user"] = "test-user"
        return await handler(request)

    with (
        patch("kiro_crew.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"),
        patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"),
    ):

        def _make(state: object | None = None):
            a = web.Application(middlewares=[_inject_user])
            if state is not None:
                a["state"] = state
            register_routes(a)
            return a

        yield _make


def _completed_parent(model: str) -> str:
    from kiro_crew.apps.builtins.auto_research import handlers as h

    parent = h.create_campaign(
        {
            "question": "Parent question padded long enough here",
            "sub_questions": [],
            "sources": ["web"],
            "max_cycles": 5,
            "model": model,
        }
    )
    pid = parent["id"]
    h.update_campaign_status(pid, h.CampaignStatus.COMPLETE)
    (h._campaign_dir(pid) / "FINDINGS.md").write_text("# Parent findings\n")
    return pid


async def _fork(app, pid: str, **extra):
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as c:
        r = await c.patch(
            f"/api/apps/auto-research/campaigns/{pid}",
            json={"action": "fork", "sub_questions": ["Follow-up one"], **extra},
        )
        return r.status, await r.json()


class TestForkModel:
    @pytest.mark.asyncio
    async def test_fork_without_a_model_keeps_the_parents_pick(self, make_app):
        from kiro_crew.apps.builtins.auto_research import handlers as h

        pid = _completed_parent("test-model-x")
        status, child = await _fork(make_app(), pid)
        assert status == 201
        assert h.get_campaign(child["id"])["model"] == "test-model-x"

    @pytest.mark.asyncio
    async def test_fork_runs_on_the_model_the_body_names(self, make_app):
        from kiro_crew.apps.builtins.auto_research import handlers as h

        pid = _completed_parent("test-model-x")
        status, child = await _fork(make_app(), pid, model="  test-model-y ")
        assert status == 201
        assert h.get_campaign(child["id"])["model"] == "test-model-y"

    @pytest.mark.asyncio
    async def test_fork_with_an_empty_model_inherits_the_default(self, make_app):
        from kiro_crew.apps.builtins.auto_research import handlers as h

        pid = _completed_parent("test-model-x")
        status, child = await _fork(make_app(), pid, model="")
        assert status == 201
        assert h.get_campaign(child["id"])["model"] == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [123, "m" * 129])
    async def test_fork_rejects_an_unusable_model_and_creates_nothing(self, make_app, bad):
        from kiro_crew.apps.builtins.auto_research import handlers as h

        pid = _completed_parent("test-model-x")
        before = len(h.list_campaigns())
        status, body = await _fork(make_app(), pid, model=bad)
        assert status == 400
        assert body["code"] == "model_invalid"
        assert body["errors"]
        assert len(h.list_campaigns()) == before


class TestWorkerModelState:
    async def _read(self, app, path: str):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as c:
            r = await c.get(path)
            assert r.status == 200
            return await r.json()

    def _campaign(self, model: str) -> str:
        from kiro_crew.apps.builtins.auto_research import handlers as h

        return h.create_campaign(
            {"question": "A research question long enough to pass", "model": model}
        )["id"]

    @pytest.mark.asyncio
    async def test_withheld_pick_carries_the_verdict_and_served_model(self, make_app):
        cid = self._campaign("test-model-x")
        slot = SimpleNamespace(model_withheld=True, served_model="test-model-default")
        app = make_app(_FakeState({f"research-{cid}": slot}))
        detail = await self._read(app, f"/api/apps/auto-research/campaigns/{cid}")
        assert detail["model"] == "test-model-x"
        assert detail["model_withheld"] is True
        assert detail["served_model"] == "test-model-default"
        listed = await self._read(app, "/api/apps/auto-research/campaigns")
        row = next(c for c in listed if c["id"] == cid)
        assert row["model_withheld"] is True
        assert row["served_model"] == "test-model-default"

    @pytest.mark.asyncio
    async def test_served_pick_reports_not_withheld(self, make_app):
        cid = self._campaign("test-model-x")
        slot = SimpleNamespace(model_withheld=False, served_model="test-model-x")
        app = make_app(_FakeState({f"research-{cid}": slot}))
        detail = await self._read(app, f"/api/apps/auto-research/campaigns/{cid}")
        assert detail["model_withheld"] is False

    @pytest.mark.asyncio
    async def test_no_worker_slot_reports_unknown(self, make_app):
        cid = self._campaign("test-model-x")
        for app in (make_app(), make_app(_FakeState())):
            detail = await self._read(app, f"/api/apps/auto-research/campaigns/{cid}")
            assert detail["model_withheld"] is None
            assert detail["served_model"] == ""
