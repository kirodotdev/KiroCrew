"""Unit tests for the B-minus live-mirror path.

No real browser, CDP port, or gateway is needed: ``build_frame_payload`` is a
pure normalizer, the proxy frame helpers are exercised directly, and the
``api_browser_frame`` handler is driven through an in-process aiohttp test
client with the loopback gate patched.
"""

from __future__ import annotations

import base64
import os
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.browser.screencast import build_frame_payload
from kiro_crew.dashboard.handlers.messaging import api_browser_frame


class TestBuildFramePayload:
    def test_valid_jpeg_frame(self):
        out = build_frame_payload({"data": "QUJD", "format": "jpeg"})
        assert out == {"data": "QUJD", "format": "jpeg"}

    def test_missing_data_is_rejected(self):
        assert build_frame_payload({}) is None
        assert build_frame_payload({"data": ""}) is None
        assert build_frame_payload({"data": 123}) is None

    def test_non_base64_data_is_rejected(self):
        # Charset validation rules out anything that isn't standard base64 —
        # so a URL, whitespace, or HTML can never reach the dashboard via this field.
        assert build_frame_payload({"data": "http://evil.example/x"}) is None  # ':' and '.'
        assert build_frame_payload({"data": "QUJD QUJD"}) is None  # whitespace
        assert build_frame_payload({"data": "<svg onload=alert(1)>"}) is None  # html
        assert build_frame_payload({"data": "QU=JD"}) is None  # padding mid-string
        # valid base64 (with padding) still passes
        assert build_frame_payload({"data": "QUJDRA==", "format": "png"}) == {
            "data": "QUJDRA==",
            "format": "png",
        }

    def test_unknown_format_defaults_to_jpeg(self):
        out = build_frame_payload({"data": "QUJD", "format": "tiff"})
        assert out is not None and out["format"] == "jpeg"

    def test_passes_through_integer_dimensions_only(self):
        out = build_frame_payload(
            {"data": "QUJD", "format": "png", "device_width": 1280, "device_height": "tall"}
        )
        assert out == {"data": "QUJD", "format": "png", "device_width": 1280}

    def test_drops_bool_and_out_of_range_dimensions(self):
        # bool is an int subclass, so a bare isinstance(int) check let
        # device_width=True through — the panel then treats it as 1px in JS
        # aspect math. Non-positive and absurdly large values are dropped too.
        for bad in (True, False, 0, -5, 100_001):
            out = build_frame_payload(
                {"data": "QUJD", "format": "png", "device_width": bad, "device_height": 720}
            )
            assert out is not None
            assert "device_width" not in out, f"device_width={bad!r} leaked"
            assert out["device_height"] == 720  # a valid sibling still passes

    def test_passes_through_valid_session_key(self):
        out = build_frame_payload({"data": "QUJD", "session_key": "chat-Abc_1.2:3"})
        assert out is not None and out["session_key"] == "chat-Abc_1.2:3"

    def test_drops_absent_or_malformed_session_key(self):
        # session_key is an opaque lookup id, bounded to a safe charset/length so
        # the WS payload can never carry arbitrary free text; anything off-spec is dropped.
        for body in (
            {"data": "QUJD"},  # absent
            {"data": "QUJD", "session_key": 123},  # wrong type
            {"data": "QUJD", "session_key": "has space"},  # out-of-charset
            {"data": "QUJD", "session_key": "x" * 129},  # over length
        ):
            out = build_frame_payload(body)
            assert out is not None
            assert "session_key" not in out


class TestProxyFrameHelpers:
    def test_post_frame_to_gateway_never_raises_when_gateway_down(self, monkeypatch):
        # Point at a port nothing is listening on; the threaded POST must swallow
        # the connection error so the agent's screenshot is never affected.
        import kiro_crew.mcp_playwright_proxy as proxy

        monkeypatch.setenv("KIROCREW_PORT", "1")  # unroutable
        # Must return immediately (spawns a daemon thread) and not raise.
        proxy._post_frame_to_gateway(b"\xff\xd8\xff", "jpeg")

    def test_post_frame_suppressed_in_extension_mode(self, monkeypatch):
        # Extension mode attaches to the user's own visible Chrome — the mirror is
        # redundant, so no frame POST should be spawned at all.
        import kiro_crew.mcp_playwright_proxy as proxy

        spawned = []

        class _FakeThread:
            def __init__(self, *a, **k):
                spawned.append(k.get("target"))

            def start(self):
                spawned.append("start")

        monkeypatch.setattr(proxy.threading, "Thread", _FakeThread)

        monkeypatch.setattr(proxy, "_EXTENSION_MODE", True)
        proxy._post_frame_to_gateway(b"\xff\xd8\xff", "jpeg")
        assert spawned == []  # no POST thread in extension mode

        monkeypatch.setattr(proxy, "_EXTENSION_MODE", False)
        proxy._post_frame_to_gateway(b"\xff\xd8\xff", "jpeg")
        assert spawned  # headless mode still mirrors

    def test_prune_keeps_newest(self, monkeypatch, tmp_path):
        import kiro_crew.mcp_playwright_proxy as proxy

        d = tmp_path / "shots"
        d.mkdir()
        monkeypatch.setattr(proxy, "_SCREENSHOT_DIR", str(d))
        monkeypatch.setattr(proxy, "_SCREENSHOT_KEEP", 3)
        for i in range(6):
            p = d / f"screenshot-{i}.jpeg"
            p.write_bytes(b"x")
            os.utime(p, (i, i))  # ascending mtime
        proxy._prune_screenshot_dir()
        remaining = sorted(os.listdir(d))
        assert remaining == ["screenshot-3.jpeg", "screenshot-4.jpeg", "screenshot-5.jpeg"]

    def test_encode_frame_returns_bytes_and_ext(self):
        import kiro_crew.mcp_playwright_proxy as proxy

        # 1x1 transparent PNG.
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMCAQAB"
            "GqQ4QAAAAABJRU5ErkJggg=="
        )
        img_bytes, ext = proxy._encode_frame(png_b64, "image/png")
        assert isinstance(img_bytes, bytes) and img_bytes
        # With PIL present it re-encodes to JPEG; without, it stays png.
        assert ext in ("jpeg", "png")
        # Round-trips as valid base64 input.
        assert base64.b64decode(png_b64)

    def test_internal_secret_read_from_kirocrew_home(self, monkeypatch, tmp_path):
        import kiro_crew.mcp_playwright_proxy as proxy

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        assert proxy._internal_secret() == ""  # absent file -> empty (POST then dropped)
        (tmp_path / ".local_secret").write_text("s3cr3t\n")
        assert proxy._internal_secret() == "s3cr3t"  # stripped


def _make_frame_app(state) -> web.Application:
    app = web.Application()
    app.router.add_post("/api/browser/frame", api_browser_frame)
    app["state"] = state
    # ws_client_count() must return an int for json_response serialization.
    if not hasattr(state.ws_client_count, "return_value") or not isinstance(
        state.ws_client_count.return_value, int
    ):
        state.ws_client_count.return_value = 0
    return app


_VALID_FRAME = {"data": "QUJD", "format": "jpeg"}


class TestApiBrowserFrameHandler:
    """Direct handler coverage for the four branches of ``api_browser_frame``.

    The loopback gate is the load-bearing auth control on this ingress, so it
    gets a direct test rather than only the indirect ``build_frame_payload``
    coverage. ``_sel`` is patched out so the assertions are about HTTP/broadcast
    behavior, not the SEL audit sink.
    """

    @pytest.mark.asyncio
    async def test_non_loopback_is_denied(self):
        state = MagicMock()
        app = _make_frame_app(state)
        with (
            patch("kiro_crew.dashboard.handlers.messaging.is_loopback", return_value=False),
            patch("kiro_crew.dashboard.handlers.messaging._sel"),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/browser/frame", json=_VALID_FRAME)
                assert resp.status == 403
        state.broadcast_ws.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_frame_broadcasts(self):
        state = MagicMock()
        app = _make_frame_app(state)
        with (
            patch("kiro_crew.dashboard.handlers.messaging.is_loopback", return_value=True),
            patch("kiro_crew.dashboard.handlers.messaging._sel"),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/browser/frame", json=_VALID_FRAME)
                assert resp.status == 200
        state.broadcast_ws.assert_called_once()
        event, payload = state.broadcast_ws.call_args[0]
        assert event == "browser_frame"
        assert payload["data"] == "QUJD" and payload["format"] == "jpeg"

    @pytest.mark.asyncio
    async def test_invalid_json_is_rejected(self):
        state = MagicMock()
        app = _make_frame_app(state)
        with (
            patch("kiro_crew.dashboard.handlers.messaging.is_loopback", return_value=True),
            patch("kiro_crew.dashboard.handlers.messaging._sel"),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/browser/frame",
                    data=b"not json",
                    headers={"Content-Type": "application/json"},
                )
                assert resp.status == 400
        state.broadcast_ws.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_frame_data_is_rejected(self):
        state = MagicMock()
        app = _make_frame_app(state)
        with (
            patch("kiro_crew.dashboard.handlers.messaging.is_loopback", return_value=True),
            patch("kiro_crew.dashboard.handlers.messaging._sel"),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/browser/frame", json={"format": "jpeg"})
                assert resp.status == 400
        state.broadcast_ws.assert_not_called()


class TestBrowseSessionKeyResolution:
    """Warm-pool fix: the proxy sends its host pid, and the gateway resolves the
    authoritative session key by walking process ancestors + verifying the
    signed session_pid sidecar — overriding the (empty on warm pool) env key."""

    def test_post_frame_body_includes_host_pid_and_env_key(self, monkeypatch):
        import json as _json

        import kiro_crew.mcp_playwright_proxy as proxy

        captured = {}

        class _Resp:
            def read(self):
                return b'{"ok": true, "subscribers": 1}'

            def close(self):
                pass

        def _fake_urlopen(req, timeout=2):
            captured["body"] = req.data
            return _Resp()

        class _Thread:  # run the daemon send synchronously
            def __init__(self, *a, **k):
                self._t = k.get("target")

            def start(self):
                self._t()

        monkeypatch.setattr(proxy, "_EXTENSION_MODE", False)
        monkeypatch.setattr(proxy.threading, "Thread", _Thread)
        monkeypatch.setattr(proxy, "loopback_urlopen", _fake_urlopen)
        monkeypatch.setattr(proxy, "_internal_secret", lambda: "secret")
        monkeypatch.setattr(proxy, "_SESSION_KEY", "env-key")

        proxy._post_frame_to_gateway(b"\xff\xd8\xff", "jpeg")

        payload = _json.loads(captured["body"])
        assert payload["host_pid"] == os.getpid()
        assert payload["session_key"] == "env-key"  # fallback still sent

    def test_resolver_walks_ancestors_and_verifies(self, monkeypatch):
        from kiro_crew.dashboard.handlers.messaging import _resolve_browse_session_key

        # ppid chain: 100 -> 200 -> 300 -> 1; only 300 has a verifiable sidecar.
        ppids = {100: 200, 200: 300, 300: 1}
        verified = {300: "sess-live"}
        monkeypatch.setattr("kiro_crew.platform_compat.get_ppid", lambda pid: ppids.get(pid, -1))
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.messaging.verify_session_pid",
            lambda pid: verified.get(int(pid), ""),
        )
        assert _resolve_browse_session_key(100) == "sess-live"
        assert _resolve_browse_session_key("100") == "sess-live"

    def test_resolver_returns_empty_on_bad_or_unmapped_pid(self, monkeypatch):
        from kiro_crew.dashboard.handlers.messaging import _resolve_browse_session_key

        monkeypatch.setattr("kiro_crew.platform_compat.get_ppid", lambda pid: 1)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.messaging.verify_session_pid", lambda pid: ""
        )
        assert _resolve_browse_session_key(None) == ""
        assert _resolve_browse_session_key("not-an-int") == ""
        assert _resolve_browse_session_key(4242) == ""  # no ancestor mapping

    @pytest.mark.asyncio
    async def test_resolved_key_overrides_payload(self):
        state = MagicMock()
        app = _make_frame_app(state)
        with (
            patch("kiro_crew.dashboard.handlers.messaging.is_loopback", return_value=True),
            patch("kiro_crew.dashboard.handlers.messaging._sel"),
            patch(
                "kiro_crew.dashboard.handlers.messaging._resolve_browse_session_key",
                return_value="sess-authoritative",
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/browser/frame", json={**_VALID_FRAME, "host_pid": 12345}
                )
                assert resp.status == 200
        _event, payload = state.broadcast_ws.call_args[0]
        assert payload["session_key"] == "sess-authoritative"

    @pytest.mark.asyncio
    async def test_resolved_key_strips_dashboard_prefix(self):
        # verify_session_pid returns the full namespaced key ("dashboard:<slot>"),
        # but the client panel filters by the BARE slot key. The handler must strip
        # the "dashboard:" prefix so the frame matches the on-screen slot; otherwise
        # every frame is dropped on the mismatch and the mirror never renders.
        state = MagicMock()
        app = _make_frame_app(state)
        with (
            patch("kiro_crew.dashboard.handlers.messaging.is_loopback", return_value=True),
            patch("kiro_crew.dashboard.handlers.messaging._sel"),
            patch(
                "kiro_crew.dashboard.handlers.messaging._resolve_browse_session_key",
                return_value="dashboard:chat-70-1785264224",
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/browser/frame", json={**_VALID_FRAME, "host_pid": 12345}
                )
                assert resp.status == 200
        _event, payload = state.broadcast_ws.call_args[0]
        assert payload["session_key"] == "chat-70-1785264224"


class TestActivePump:
    """B′ active pump: idle-gated self-issued screenshots that keep the mirror current."""

    def _reset(
        self,
        proxy,
        monkeypatch,
        *,
        enabled=True,
        pending=None,
        inflight=None,
        sent_at=0.0,
        activity=None,
        subs=1,
    ):
        now = 1000.0
        monkeypatch.setattr(proxy, "_pump_enabled", enabled)
        monkeypatch.setattr(proxy, "_PENDING_REQUESTS", pending if pending is not None else {})
        monkeypatch.setattr(proxy, "_pump_inflight_id", inflight)
        monkeypatch.setattr(proxy, "_pump_sent_at", sent_at)
        monkeypatch.setattr(proxy, "_last_browse_activity", now if activity is None else activity)
        monkeypatch.setattr(proxy, "_last_subscriber_count", subs)
        return now

    def test_is_pump_id(self):
        import kiro_crew.mcp_playwright_proxy as proxy

        assert proxy._is_pump_id("__mc_pump_7")
        assert not proxy._is_pump_id(7)
        assert not proxy._is_pump_id("7")
        assert not proxy._is_pump_id(None)

    def test_should_pump_all_gates_pass(self, monkeypatch):
        import kiro_crew.mcp_playwright_proxy as proxy

        now = self._reset(proxy, monkeypatch)
        assert proxy._should_pump(now) is True

    def test_should_pump_blocked_when_disabled(self, monkeypatch):
        import kiro_crew.mcp_playwright_proxy as proxy

        now = self._reset(proxy, monkeypatch, enabled=False)
        assert proxy._should_pump(now) is False

    def test_should_pump_blocked_when_agent_request_in_flight(self, monkeypatch):
        import kiro_crew.mcp_playwright_proxy as proxy

        now = self._reset(proxy, monkeypatch, pending={1: {"id": 1}})
        assert proxy._should_pump(now) is False

    def test_should_pump_blocked_when_pump_in_flight_within_timeout(self, monkeypatch):
        import kiro_crew.mcp_playwright_proxy as proxy

        now = self._reset(proxy, monkeypatch, inflight="__mc_pump_1", sent_at=1000.0)
        assert proxy._should_pump(now) is False  # just sent, still in flight

    def test_should_pump_allowed_after_inflight_timeout(self, monkeypatch):
        import kiro_crew.mcp_playwright_proxy as proxy

        # stuck pump older than _PUMP_TIMEOUT no longer blocks
        now = self._reset(
            proxy, monkeypatch, inflight="__mc_pump_1", sent_at=1000.0 - proxy._PUMP_TIMEOUT - 1
        )
        assert proxy._should_pump(now) is True

    def test_should_pump_blocked_when_session_idle_cold(self, monkeypatch):
        import kiro_crew.mcp_playwright_proxy as proxy

        # last browse activity older than the active window
        now = self._reset(proxy, monkeypatch, activity=1000.0 - proxy._PUMP_ACTIVE_WINDOW - 1)
        assert proxy._should_pump(now) is False

    def test_should_pump_blocked_when_no_subscribers(self, monkeypatch):
        import kiro_crew.mcp_playwright_proxy as proxy

        now = self._reset(proxy, monkeypatch, subs=0)
        assert proxy._should_pump(now) is False

    def test_note_browse_activity_only_for_browser_tools(self, monkeypatch):
        import kiro_crew.mcp_playwright_proxy as proxy

        monkeypatch.setattr(proxy, "_last_browse_activity", 0.0)
        proxy._note_browse_activity(
            {"method": "tools/call", "params": {"name": "browser_navigate"}}
        )
        assert proxy._last_browse_activity > 0.0  # browser_* updates it

        monkeypatch.setattr(proxy, "_last_browse_activity", 0.0)
        proxy._note_browse_activity({"method": "tools/call", "params": {"name": "list_files"}})
        assert proxy._last_browse_activity == 0.0  # non-browser tool ignored
        proxy._note_browse_activity(None)  # must not raise
        proxy._note_browse_activity({"method": "initialize"})  # not a tools/call

    def test_relay_pump_frame_posts_decoded_image(self, monkeypatch):
        import kiro_crew.mcp_playwright_proxy as proxy

        posted = []
        monkeypatch.setattr(
            proxy,
            "_post_frame_to_gateway",
            lambda b, fmt, source="agent": posted.append((b, fmt, source)),
        )
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMCAQAB"
            "GqQ4QAAAAABJRU5ErkJggg=="
        )
        msg = {
            "id": "__mc_pump_3",
            "result": {"content": [{"type": "image", "data": png_b64, "mimeType": "image/png"}]},
        }
        proxy._relay_pump_frame(msg)
        # pump frames are tagged source="pump" so the gateway can label them in SEL
        assert len(posted) == 1 and isinstance(posted[0][0], bytes) and posted[0][0]
        assert posted[0][2] == "pump"

    def test_relay_pump_frame_ignores_non_image(self, monkeypatch):
        import kiro_crew.mcp_playwright_proxy as proxy

        posted = []
        monkeypatch.setattr(
            proxy, "_post_frame_to_gateway", lambda b, fmt, source="agent": posted.append(b)
        )
        proxy._relay_pump_frame(
            {"id": "__mc_pump_4", "result": {"content": [{"type": "text", "text": "x"}]}}
        )
        proxy._relay_pump_frame({"id": "__mc_pump_5", "error": {"code": -32000}})  # error response
        assert posted == []

    def test_relay_pump_frame_swallows_bad_base64(self, monkeypatch):
        import kiro_crew.mcp_playwright_proxy as proxy

        posted = []
        monkeypatch.setattr(
            proxy, "_post_frame_to_gateway", lambda b, fmt, source="agent": posted.append(b)
        )
        # malformed base64 makes _encode_frame raise; the main relay loop must not crash
        msg = {
            "id": "__mc_pump_6",
            "result": {
                "content": [{"type": "image", "data": "!!!not base64!!!", "mimeType": "image/png"}]
            },
        }
        proxy._relay_pump_frame(msg)  # must not raise
        assert posted == []

    def test_clear_pump_inflight(self, monkeypatch):
        import kiro_crew.mcp_playwright_proxy as proxy

        monkeypatch.setattr(proxy, "_pump_inflight_id", "__mc_pump_9")
        proxy._clear_pump_inflight("__mc_pump_8")  # different id: no-op
        assert proxy._pump_inflight_id == "__mc_pump_9"
        proxy._clear_pump_inflight("__mc_pump_9")  # matching id: cleared
        assert proxy._pump_inflight_id is None

    def test_record_subscriber_count(self, monkeypatch):
        import kiro_crew.mcp_playwright_proxy as proxy

        monkeypatch.setattr(proxy, "_last_subscriber_count", 1)
        proxy._record_subscriber_count(b'{"ok": true, "subscribers": 3}')
        assert proxy._last_subscriber_count == 3
        proxy._record_subscriber_count(b'{"ok": true, "subscribers": 0}')
        assert proxy._last_subscriber_count == 0
        proxy._record_subscriber_count(b"not json")  # must not raise or change
        assert proxy._last_subscriber_count == 0


class TestApiBrowserFrameSelSource:
    """The frame ingress labels its SEL audit event by frame origin (agent vs pump),
    bounded to a known set so the audit field can't carry arbitrary caller text."""

    def _run(self, body, monkeypatch):
        import asyncio

        from kiro_crew.dashboard.handlers import messaging

        captured: dict = {}

        class _Sel:
            def log_tool_invocation(self, **kw):
                captured.update(kw)

        class _State:
            def broadcast_ws(self, *_a):
                pass

            def ws_client_count(self):
                return 1

        class _Req:
            app = {"state": _State()}
            remote = "127.0.0.1"

            def __init__(self, b):
                self._b = b

            async def json(self):
                return self._b

        monkeypatch.setattr(messaging, "is_loopback", lambda _r: True)
        monkeypatch.setattr(messaging, "_sel", lambda: _Sel())
        asyncio.run(messaging.api_browser_frame(_Req(body)))
        return captured

    def test_pump_source_labeled_in_sel(self, monkeypatch):
        cap = self._run({"data": "QUJDRA==", "format": "jpeg", "source": "pump"}, monkeypatch)
        assert cap.get("outcome") == "completed"
        assert cap.get("source") == "pump"

    def test_default_source_is_agent(self, monkeypatch):
        cap = self._run({"data": "QUJDRA==", "format": "jpeg"}, monkeypatch)
        assert cap.get("source") == "agent"

    def test_unknown_source_falls_back_to_agent(self, monkeypatch):
        cap = self._run({"data": "QUJDRA==", "format": "jpeg", "source": "../evil"}, monkeypatch)
        assert cap.get("source") == "agent"


class TestPumpAudit:
    """Proxy reports each pump injection to the gateway so it can emit the SEL
    tool-invocation event (the proxy is stdlib-only and can't reach sel.py). The
    POST is synchronous and gates the injection: no 2xx ack -> no screenshot, so
    a pump-injected tool call is never executed unaudited."""

    @staticmethod
    def _resp(status):
        class _Resp:
            def getcode(self):
                return status

            def close(self):
                pass

        return _Resp()

    def test_post_pump_audit_skipped_in_extension_mode(self, monkeypatch):
        import kiro_crew.mcp_playwright_proxy as proxy

        calls = []
        monkeypatch.setattr(proxy, "_EXTENSION_MODE", True)
        monkeypatch.setattr(proxy, "loopback_urlopen", lambda *a, **k: calls.append("post"))
        # Extension mode: the user sees their own Chrome; no audit POST, and the
        # caller treats the False return as "do not inject".
        assert proxy._post_pump_audit() is False
        assert calls == []

    def test_post_pump_audit_true_on_2xx(self, monkeypatch):
        import kiro_crew.mcp_playwright_proxy as proxy

        monkeypatch.setattr(proxy, "_EXTENSION_MODE", False)
        monkeypatch.setattr(proxy, "loopback_urlopen", lambda *a, **k: self._resp(200))
        assert proxy._post_pump_audit() is True  # gateway acked -> injection allowed

    def test_post_pump_audit_false_on_non_2xx(self, monkeypatch):
        import kiro_crew.mcp_playwright_proxy as proxy

        monkeypatch.setattr(proxy, "_EXTENSION_MODE", False)
        monkeypatch.setattr(proxy, "loopback_urlopen", lambda *a, **k: self._resp(500))
        assert proxy._post_pump_audit() is False  # not acked -> caller skips the injection

    def test_post_pump_audit_false_on_failure(self, monkeypatch):
        import kiro_crew.mcp_playwright_proxy as proxy

        def _boom(*a, **k):
            raise OSError("gateway down")

        monkeypatch.setattr(proxy, "_EXTENSION_MODE", False)
        monkeypatch.setattr(proxy, "loopback_urlopen", _boom)
        # A failed audit must report False so the pump skips the injection rather
        # than run an unaudited browser_take_screenshot.
        assert proxy._post_pump_audit() is False


class TestApiBrowserPumpAudit:
    """The pump-audit ingress logs the browser_take_screenshot tool invocation
    (source=pump) on the proxy's behalf, loopback-gated."""

    def _run(self, monkeypatch, *, loopback):
        import asyncio

        from kiro_crew.dashboard.handlers import messaging

        captured: dict = {}

        class _Sel:
            def log_tool_invocation(self, **kw):
                captured.update(kw)

        class _Req:
            remote = "127.0.0.1" if loopback else "10.0.0.5"

        monkeypatch.setattr(messaging, "is_loopback", lambda _r: loopback)
        monkeypatch.setattr(messaging, "_sel", lambda: _Sel())
        resp = asyncio.run(messaging.api_browser_pump_audit(_Req()))
        return captured, resp

    def test_logs_tool_invocation(self, monkeypatch):
        cap, resp = self._run(monkeypatch, loopback=True)
        assert resp.status == 200
        assert cap.get("tool_name") == "browser_take_screenshot"
        assert cap.get("source") == "pump"
        assert cap.get("outcome") == "injected"

    def test_denies_non_loopback(self, monkeypatch):
        cap, resp = self._run(monkeypatch, loopback=False)
        assert resp.status == 403
        assert cap.get("tool_name") == "browser_take_screenshot"
        assert cap.get("outcome") == "denied"
