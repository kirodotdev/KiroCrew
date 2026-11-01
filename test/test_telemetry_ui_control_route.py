"""``POST /api/telemetry/ui-control``: owner only, one closed field, nothing stored.

Driven against the real handler with a mocked request, so the gate, the body
rules and the closed value list are the production ones.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from body_stream_helpers import BodyStreamPayload
from multidict import CIMultiDict

from kiro_crew.dashboard.handlers import telemetry_ui_control as mod
from kiro_crew.metrics import feature_usage as fu


class _FragmentedPayload(BodyStreamPayload):
    """A body that arrives in pieces of *size* bytes, whatever chunk is asked for."""

    def __init__(self, raw: bytes, size: int) -> None:
        super().__init__(raw)
        self._size = size

    async def iter_chunked(self, n: int):
        for i in range(0, len(self._raw), self._size):
            yield self._raw[i : i + self._size]


def _request(raw: bytes, *, payload=None, content_type: str = "application/json"):
    return make_mocked_request(
        "POST",
        "/api/telemetry/ui-control",
        headers=CIMultiDict({"Content-Length": str(len(raw)), "Content-Type": content_type}),
        payload=payload if payload is not None else BodyStreamPayload(raw),
    )


class _Rec:
    enabled = True

    def __init__(self) -> None:
        self.counters: list[dict] = []

    def counter(self, name, value=1, *, attrs=None, **kwargs) -> None:
        self.counters.append({"name": name, "attrs": dict(attrs or {})})


def _call(raw: bytes, *, owner: bool = True, **request_kwargs):
    rec = _Rec()
    refusal = None if owner else web.json_response({"code": "owner_only"}, status=403)
    with (
        patch.object(mod, "require_owner_dashboard_request", AsyncMock(return_value=refusal)),
        patch("kiro_crew.metrics.provider.get_recorder", return_value=rec),
    ):
        resp = asyncio.run(mod.api_ui_control_clicked(_request(raw, **request_kwargs)))
    clicks = [c["attrs"] for c in rec.counters if c["name"] == fu.UI_CONTROL_CLICKED]
    return resp, clicks


def test_a_known_control_is_counted_and_answers_204():
    resp, clicks = _call(json.dumps({"control": "option_chip"}).encode())
    assert resp.status == 204
    assert clicks == [{"control": "option_chip"}]


def test_an_unknown_value_is_counted_as_other():
    resp, clicks = _call(json.dumps({"control": "drop table"}).encode())
    assert resp.status == 204
    assert clicks == [{"control": "other"}]


def test_a_non_owner_is_refused_and_nothing_is_counted():
    resp, clicks = _call(json.dumps({"control": "option_chip"}).encode(), owner=False)
    assert resp.status == 403
    assert clicks == []


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        json.dumps(["option_chip"]).encode(),
        json.dumps({"control": "option_chip", "turn": "chat-1-2"}).encode(),
        json.dumps({"control": 3}).encode(),
        json.dumps({"control": "x" * 65}).encode(),
        json.dumps({}).encode(),
    ],
)
def test_any_other_body_shape_is_refused_and_nothing_is_counted(raw):
    resp, clicks = _call(raw)
    assert resp.status == 400
    assert clicks == []


def test_a_body_that_arrives_in_pieces_is_read_whole():
    raw = json.dumps({"control": "option_chip"}).encode()
    resp, clicks = _call(raw, payload=_FragmentedPayload(raw, 5))
    assert resp.status == 204
    assert clicks == [{"control": "option_chip"}]


def test_a_body_without_a_json_content_type_is_refused():
    resp, clicks = _call(json.dumps({"control": "option_chip"}).encode(), content_type="text/plain")
    assert resp.status == 415
    assert clicks == []


def test_every_refusal_carries_a_code():
    for raw in (b"not json", json.dumps({}).encode(), json.dumps({"control": 3}).encode()):
        resp, _ = _call(raw)
        assert json.loads(resp.body)["code"]


def test_an_oversized_body_is_refused():
    resp, clicks = _call(b"{" + b" " * 400 + b"}")
    assert resp.status == 413
    assert clicks == []


def test_the_route_is_registered_as_a_post_on_the_dashboard_server():
    from kiro_crew.dashboard.routes import system

    src = system.__file__
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    assert (
        'add_post("/api/telemetry/ui-control", handlers.api_ui_control_clicked)' in text
    ), "the click route must be a POST on the existing dashboard routes"
