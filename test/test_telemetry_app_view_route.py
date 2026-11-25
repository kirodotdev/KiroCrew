"""``POST /api/telemetry/app-view``: owner only, a closed 256-byte body, nothing stored.

Driven against the real handler with a mocked request, so the gate, the body
rules, the server-side app id and version, and the 24-hour cut are the
production ones.
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

from kiro_crew.dashboard.handlers import telemetry_app_view as mod
from kiro_crew.metrics import feature_usage as fu

_PUBLIC = {"weather-app": "1.2.3"}


def _request(raw: bytes, *, content_type: str = "application/json"):
    return make_mocked_request(
        "POST",
        "/api/telemetry/app-view",
        headers=CIMultiDict({"Content-Length": str(len(raw)), "Content-Type": content_type}),
        payload=BodyStreamPayload(raw),
    )


class _Rec:
    enabled = True

    def __init__(self) -> None:
        self.counters: list[dict] = []

    def counter(self, name, value=1, *, attrs=None, **kwargs) -> None:
        self.counters.append({"name": name, "value": value, "attrs": dict(attrs or {}), **kwargs})


_HOST_OWNER = "kiro_crew.dashboard.handlers.source_providers.is_host_owner_dashboard_request"


def _call(
    body,
    *,
    owner: bool = True,
    host_owner: bool = True,
    content_type: str = "application/json",
):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    rec = _Rec()
    refusal = None if owner else web.json_response({"code": "owner_only"}, status=403)
    with (
        patch.object(mod, "require_owner_dashboard_request", AsyncMock(return_value=refusal)),
        patch("kiro_crew.metrics.provider.get_recorder", return_value=rec),
        patch.object(fu, "_public_app_ids", _PUBLIC),
        patch(_HOST_OWNER, return_value=host_owner),
    ):
        resp = asyncio.run(mod.api_app_view(_request(raw, content_type=content_type)))
    return resp, rec.counters


def _opened(app="weather-app", surface="page"):
    return {"app": app, "surface": surface, "event": "opened"}


def _foreground(seconds, app="weather-app", surface="page"):
    return {"app": app, "surface": surface, "event": "foreground", "seconds": seconds}


def test_an_open_is_counted_with_the_servers_own_id_and_version():
    resp, counts = _call(_opened())
    assert resp.status == 204
    assert counts == [
        {
            "name": fu.APP_OPENED,
            "value": 1,
            "attrs": {"app": "weather-app", "entity_version": "1.2.3", "surface": "page"},
        }
    ]


def test_an_unknown_name_is_private_and_an_unknown_surface_other():
    resp, counts = _call(_opened(app="my-secret-project", surface="iframe"))
    assert resp.status == 204
    assert counts[0]["attrs"] == {"app": "private", "entity_version": "private", "surface": "other"}


def test_a_span_adds_its_seconds_in_its_bucket():
    resp, counts = _call(_foreground(42, surface="popout"))
    assert resp.status == 204
    assert counts == [
        {
            "name": fu.APP_FOREGROUND_TIME,
            "value": 42,
            "unit": "s",
            "attrs": {
                "app": "weather-app",
                "entity_version": "1.2.3",
                "surface": "popout",
                "duration": "le_1m",
            },
        }
    ]


def test_a_desktop_surface_is_other_on_a_foreground_span():
    _, counts = _call(_foreground(5, surface="desktop"))
    assert counts[0]["attrs"]["surface"] == "other"


def test_a_span_is_cut_to_24_hours():
    resp, counts = _call(_foreground(3 * 24 * 3600))
    assert resp.status == 204
    assert counts[0]["value"] == 24 * 3600
    assert counts[0]["attrs"]["duration"] == "gt_4h"


def test_a_span_under_one_second_is_not_counted():
    resp, counts = _call(_foreground(0))
    assert resp.status == 204
    assert counts == []


def test_a_non_owner_is_refused_and_nothing_is_counted():
    resp, counts = _call(_opened(), owner=False)
    assert resp.status == 403
    assert counts == []


def test_a_shared_crew_teammate_is_answered_but_not_counted():
    """The dashboard gate admits a teammate; only the machine owner's views count."""
    for body in (_opened(), _foreground(30)):
        resp, counts = _call(body, host_owner=False)
        assert resp.status == 204
        assert counts == []


def test_the_longest_app_name_is_counted():
    resp, counts = _call(_foreground(30, app="a" * 128, surface="popout"))
    assert resp.status == 204
    assert counts[0]["attrs"]["app"] == "private"


def test_a_body_over_256_bytes_is_refused():
    resp, counts = _call(b'{"app": "' + b"x" * 300 + b'"}')
    assert resp.status == 413
    assert counts == []


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        ["opened"],
        {},
        {"app": "weather-app", "surface": "page"},
        {**_opened(), "seconds": 5},
        {**_opened(), "entity_version": "9.9.9"},
        {"app": "weather-app", "surface": "page", "event": "foreground"},
        {"app": "weather-app", "surface": "page", "event": ["opened"]},
        {"app": "weather-app", "surface": "page", "event": "closed"},
        _opened(app=7),
        _opened(app=""),
        _opened(app="x" * 129),
        _opened(surface=None),
        _foreground("10"),
        _foreground(True),
        _foreground(1.5),
        _foreground(-1),
        _foreground(10**12),
    ],
)
def test_any_other_body_is_refused_with_a_code_and_nothing_is_counted(body):
    resp, counts = _call(body)
    assert resp.status == 400
    assert json.loads(resp.body)["code"]
    assert counts == []


def test_a_body_without_a_json_content_type_is_refused():
    resp, counts = _call(_opened(), content_type="text/plain")
    assert resp.status == 415
    assert counts == []


def test_a_raising_recorder_still_answers_204():
    class _Boom:
        enabled = True

        def counter(self, *a, **k):
            raise RuntimeError("recorder down")

    raw = json.dumps(_foreground(30)).encode()
    with (
        patch.object(mod, "require_owner_dashboard_request", AsyncMock(return_value=None)),
        patch("kiro_crew.metrics.provider.get_recorder", return_value=_Boom()),
        patch(_HOST_OWNER, return_value=True),
    ):
        for body in (json.dumps(_opened()).encode(), raw):
            assert asyncio.run(mod.api_app_view(_request(body))).status == 204


def test_the_route_is_registered_beside_ui_control():
    from kiro_crew.dashboard.routes import system

    with open(system.__file__, encoding="utf-8") as fh:
        text = fh.read()
    assert 'add_post("/api/telemetry/app-view", handlers.api_app_view)' in text
    assert text.index("/api/telemetry/ui-control") < text.index("/api/telemetry/app-view")
