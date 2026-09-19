"""Tests for POST /api/channel-folders/backfill.

The endpoint's own decisions only -- the filing behaviour it delegates to is
covered in ``test_channel_folders.py``. What is tested here is the boundary: who
may call it, which namespaces it accepts, and that a report reaches the caller
unflattened (the panel renders the moved list, so losing it silently would make
the button look like it did nothing).

Every test that depends on the REQUEST BODY runs over a real ``TestClient``, and
asserts on the error MESSAGE rather than only the status.
``make_mocked_request(payload=...)`` gives ``await request.json()`` nothing to
read, so on a mocked request every one of these bodies -- valid,
unknown-namespace, wrong-type -- comes back as ``invalid JSON`` with status 400.
Three of these tests passed that way before the message assertions were added:
right status, wrong reason. Mocked requests are kept only for the two loopback
refusals, which return before the body is read.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


def _mod() -> Any:
    import kiro_crew.dashboard.handlers.messaging as mod

    return mod


def _mocked(payload: bytes, **headers: str) -> Any:
    from aiohttp.test_utils import make_mocked_request

    return make_mocked_request(
        "POST",
        "/api/channel-folders/backfill",
        payload=payload,
        headers={"Content-Type": "application/json", **headers},
    )


def _call(
    monkeypatch: Any,
    body: Any,
    *,
    state: object | None = None,
    with_state: bool = True,
    report: dict[str, Any] | None = None,
    raw: bytes | None = None,
) -> tuple[int, Any, list[str]]:
    """POST over a real client; return ``(status, json, namespaces seen)``.

    *report* stubs the filing call, so a boundary test never depends on the
    store. *raw* sends bytes verbatim, for a body that is not a JSON object.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    mod = _mod()
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    seen: list[str] = []

    async def _fake_backfill(_state: Any, namespace: str) -> dict[str, Any]:
        seen.append(namespace)
        return report if report is not None else {}

    monkeypatch.setattr(mod, "backfill_channel_folder", _fake_backfill)

    async def _run() -> tuple[int, Any]:
        app = web.Application()
        if with_state:
            app["state"] = state if state is not None else object()
        app.router.add_post("/api/channel-folders/backfill", mod.api_channel_folder_backfill)
        async with TestClient(TestServer(app)) as client:
            if raw is not None:
                resp = await client.post(
                    "/api/channel-folders/backfill",
                    data=raw,
                    headers={"Content-Type": "application/json"},
                )
            else:
                resp = await client.post("/api/channel-folders/backfill", json=body)
            return resp.status, await resp.json()

    status, payload = asyncio.run(_run())
    return status, payload, seen


def test_denies_non_loopback(monkeypatch: Any) -> None:
    """Loopback-only.

    Not inherited reasoning from the config saves it sits beside: this writes no
    credential, but it bulk-moves conversations with no collective undo, and a
    remote caller can neither see the sidebar it rearranges nor put anything back.
    """
    mod = _mod()
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: False)
    resp = asyncio.run(mod.api_channel_folder_backfill(_mocked(b'{"namespace": "slack"}')))
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "read_only_remote"
    # The message deliberately avoids the neighbouring panels' "read-only from
    # remote sessions": the button that provokes this refusal is labelled "File
    # existing sessions", meaning chat conversations, so that wording would put
    # one word for two different things on one card. It is also a whole sentence
    # naming the remedy, because a refusal a reader cannot act on is a dead end.
    # The `code` above is the machine-readable part and does not move.
    body = json.loads(resp.body)
    assert "remote sessions" not in body["error"], body["error"]
    assert "hosts this dashboard" in body["error"], body["error"]
    assert body["error"].rstrip().endswith("click again."), body["error"]


def test_denies_a_forwarded_loopback_request() -> None:
    """A reverse-proxied request cannot trigger a bulk move."""
    mod = _mod()
    resp = asyncio.run(
        mod.api_channel_folder_backfill(
            _mocked(b'{"namespace": "slack"}', **{"X-Forwarded-For": "203.0.113.7"})
        )
    )
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "read_only_remote"


def test_rejects_an_unknown_namespace(monkeypatch: Any) -> None:
    """Refused visibly rather than answered with a silently empty pass.

    The namespace selects a config section and stamps a folder, so an
    unrecognised one is a caller error, not "nothing to do".
    """
    status, body, seen = _call(monkeypatch, {"namespace": "../../etc"})
    assert status == 400
    assert body["error"] == "unknown channel"
    assert body["code"] == "unknown_channel"
    assert seen == [], "an unknown namespace must not reach the filing pass"


def test_rejects_a_non_string_namespace(monkeypatch: Any) -> None:
    status, body, seen = _call(monkeypatch, {"namespace": 7})
    assert status == 400
    assert body["error"] == "namespace must be text"
    assert body["code"] == "namespace_invalid"
    assert seen == []


def test_rejects_a_non_object_body(monkeypatch: Any) -> None:
    status, body, _seen = _call(monkeypatch, None, raw=b'["slack"]')
    assert status == 400
    assert body["error"] == "body must be an object"
    assert body["code"] == "invalid_body"


def test_rejects_an_unparseable_body(monkeypatch: Any) -> None:
    status, body, _seen = _call(monkeypatch, None, raw=b"{not json")
    assert status == 400
    assert body["error"] == "invalid JSON"
    assert body["code"] == "invalid_json"


def test_returns_503_without_dashboard_state(monkeypatch: Any) -> None:
    status, body, seen = _call(monkeypatch, {"namespace": "slack"}, with_state=False)
    assert status == 503
    assert body["error"] == "dashboard state unavailable"
    assert body["code"] == "state_unavailable"
    assert seen == []


def test_returns_the_report_verbatim(monkeypatch: Any) -> None:
    """The moved list is the only record of what happened, so it must survive."""
    report = {
        "folder_name": "Slack",
        "moved": [{"key": "slack:1", "title": "Standup", "label": "Slack"}],
        "reason": "",
        "remaining": 0,
    }
    status, body, seen = _call(monkeypatch, {"namespace": "slack"}, report=report)
    assert status == 200
    assert body == report
    assert seen == ["slack"]


def test_a_pass_that_moved_nothing_is_still_200(monkeypatch: Any) -> None:
    """ "Nothing to do" is a normal outcome the panel renders, not an error.

    A 4xx here would make the panel show a failure for a channel whose
    conversations are simply all placed already.
    """
    report = {
        "folder_name": "",
        "moved": [],
        "reason": "not_configured",
        "remaining": 0,
    }
    status, body, _seen = _call(monkeypatch, {"namespace": "slack"}, report=report)
    assert status == 200
    assert body["reason"] == "not_configured"


def test_the_namespace_is_normalised_before_use(monkeypatch: Any) -> None:
    """Config sections are keyed lowercase, so a cased or padded body must work."""
    status, _body, seen = _call(monkeypatch, {"namespace": " Slack "}, report={"moved": []})
    assert status == 200
    assert seen == ["slack"]
