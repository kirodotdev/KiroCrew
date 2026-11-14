"""The Spend table's outcome columns are additive.

A window in which no session delivered anything must answer exactly as it did
before the columns existed: no ``outcome`` on a row and no ``outcome_scan`` on
the block. These tests do not import the outcome module, so they pin main's
answer and pass on main as well as on the branch.
"""

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.chat_utils import slot_transcript_key
from kiro_crew.dashboard.handlers import telemetry as h
from kiro_crew.history import ConversationLog


class TestStartupRouteWithoutDeliveries:
    """A window in which no session delivered anything answers as it always did.

    The Spend table's outcome columns are additive: a transcript that holds no
    commit and no pull request must leave the ``cost`` block exactly as the
    title step shapes it, with no ``outcome`` on a row and no ``outcome_scan``.
    """

    @pytest.mark.asyncio
    async def test_the_cost_block_gains_no_outcome_keys(self, tmp_path, _floor_monkeypatch):
        stem = slot_transcript_key("chat-1-1").replace(":", "_")
        talk = {
            "role": "tool",
            "content": "\U0001f527 ls -la",
            "ts": "2026-11-10T12:00:00+00:00",
            "meta": {"output": "total 0\n"},
        }
        (tmp_path / f"{stem}.jsonl").write_text(
            json.dumps({"_type": "metadata", "created_at": "2026-11-10T12:00:00+00:00"})
            + "\n"
            + json.dumps(talk)
            + "\n",
            encoding="utf-8",
        )
        cost = {
            "turns": 1,
            "unit": "credits",
            "conversations": [{"slot": "chat-1-1", "credits": 10.0}],
            "transcripts": {},
        }
        _floor_monkeypatch.setattr(
            h,
            "_telemetry_cfg",
            lambda: h._TelemetryState(
                enabled=True,
                directory=tmp_path,
                env_pinned=False,
                env_var=h.TELEMETRY_ENV_VAR,
                otlp_configured=False,
                retention_days=0,
            ),
        )
        _floor_monkeypatch.setattr(h, "_parse_startup_metrics", lambda _w, **_kw: {"other": []})
        _floor_monkeypatch.setattr(h, "_context_block", lambda _w: None)
        _floor_monkeypatch.setattr(h, "_cost_block", lambda _w: cost)
        app = web.Application()
        app["state"] = SimpleNamespace(
            conversation_log=ConversationLog(tmp_path),
            get_slot=lambda key: SimpleNamespace(display_title="Billing audit"),
        )
        request = make_mocked_request(
            "GET", "/api/telemetry/startup?since=1794225600&until=1794398400", app=app
        )
        body = json.loads((await h.api_telemetry_startup(request)).body)["cost"]
        assert body["conversations"] == [
            {"slot": "chat-1-1", "credits": 10.0, "title": "Billing audit"}
        ]
        assert "outcome_scan" not in body
