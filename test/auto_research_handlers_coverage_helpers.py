"""Fixtures and request builders the auto-research handler coverage modules share.

``test_auto_research_handlers_coverage.py`` and
``test_auto_research_handlers_coverage_campaign_routes.py`` import them by name. The
three autouse fixtures pin ``DB_PATH`` / ``RESEARCH_DIR`` into ``tmp_path``, fail a
test that leaks an SSE queue, and default to no autonudge service; ``_isolate`` and
``sse`` are declared ``@pytest.fixture(name=...)`` and imported under their function
names. Requests are aiohttp's own ``make_mocked_request``, so no socket is bound.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.auto_research import handlers as h

BASE = "/api/apps/auto-research"


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True, name="_isolate")
def _isolate_fixture(tmp_path: Path):
    """Pin the DB + research dir into tmp_path (same shape as test_auto_research)."""
    with (
        mock.patch.object(h, "DB_PATH", tmp_path / "t.db"),
        mock.patch.object(h, "RESEARCH_DIR", tmp_path / "r"),
    ):
        yield tmp_path


@pytest.fixture(autouse=True)
def _no_stray_sse_queues():
    """The SSE queue registry is module-global — fail loudly if a test leaks one."""
    before = list(h._sse_queues)
    yield
    assert h._sse_queues == before, "test leaked an SSE queue"


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the auto-research coverage modules' autouse no-autonudge default, moved verbatim; tracked for a de-flake pass
def _no_autonudge(monkeypatch: pytest.MonkeyPatch):
    """Default to 'no autonudge service' so nothing touches a live loop registry."""
    monkeypatch.setattr(h, "_autonudge_instance", lambda: None)


def _app(**keys: Any) -> web.Application:
    """A real (unfrozen) Application so ``request.app.get(...)`` returns None for
    absent keys — a ``MagicMock`` app would make every ``is None`` guard false.
    """
    app = web.Application()
    for key, value in keys.items():
        app[key] = value
    return app


def _mk(
    method: str,
    path: str,
    *,
    app: web.Application | None = None,
    match: dict | None = None,
    body: Any = ...,
    authed: bool = True,
) -> web.Request:
    """A mocked aiohttp request for a handler under test.

    ``body`` is stubbed onto ``.json()`` (the pattern the issue-radar route tests
    use); pass ``None`` to model a payload that fails to decode.
    """
    req = make_mocked_request(method, f"{BASE}/{path}", app=app, match_info=match or {})
    if authed:
        req["user"] = "test-user"
    if body is not ...:
        if body is None:
            req.json = AsyncMock(side_effect=ValueError("bad json"))  # type: ignore[method-assign]
        else:
            req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _body(response: web.StreamResponse) -> dict:
    assert isinstance(response, web.Response)
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


def _campaign(**config: Any) -> str:
    cfg: dict[str, Any] = {"question": "How do teams handle API rate limiting today?"}
    cfg.update(config)
    return h.create_campaign(cfg)["id"]


def _running(cid: str, *, started_at: float | None = None, **cols: Any) -> None:
    """Force a campaign RUNNING, optionally back-dating started_at."""
    h.update_campaign_status(cid, h.CampaignStatus.RUNNING)
    if started_at is not None:
        cols["started_at"] = started_at
    if cols:
        db = h._get_db()
        sets = ", ".join(f"{k} = ?" for k in cols)
        db.execute(f"UPDATE campaigns SET {sets} WHERE id = ?", (*cols.values(), cid))
        db.commit()
        db.close()


def _status(cid: str) -> str:
    campaign = h.get_campaign(cid)
    assert campaign is not None
    return campaign["status"]


def _write_finding(cid: str, cycle: int, **fields: Any) -> Path:
    d = h._campaign_dir(cid)
    payload: dict[str, Any] = {"cycle": cycle, "summary": "s", "new_findings_count": 1}
    payload.update(fields)
    path = d / "findings" / ("cycle_%03d.json" % cycle)
    path.write_text(json.dumps(payload))
    return path


class _SSESink:
    """Captures ``_emit_sse`` payloads without a real stream consumer."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, event: dict) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.get("type") for e in self.events]


@pytest.fixture(name="sse")
def sse_fixture(monkeypatch: pytest.MonkeyPatch) -> _SSESink:
    sink = _SSESink()
    monkeypatch.setattr(h, "_emit_sse", sink)
    return sink
