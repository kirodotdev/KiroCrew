"""Native-routing contract for the Playwright MCP proxy.

The proxy hands a ``browser_*`` tool call to the NATIVE embedded browser panel
instead of the Playwright subprocess. The properties pinned here are the
security- and correctness-relevant ones from the shared agent-ops contract:

1. Every MAPPED op (``_NATIVE_OPS``) routes to the native command bus.
2. No-split-brain: once native routing is active for a session (session key
   known), a ``browser_*`` tool with NO native mapping is refused with an MCP
   error naming the tool -- it must NOT reach Playwright, or it would act on a
   different page than the native view the agent is driving.
3. A panel that ANSWERS and refuses (``ok:false``) is surfaced as an MCP error
   and must NOT fall back -- falling back would convert a revoked "let the agent
   act" into an allow by another route.
4. Only TRANSPORT unavailability (no panel / timeout / connection error) may fall
   back, because then nothing native can answer at all.
5. Element addressing uses Playwright's ``target`` param (a snapshot ref OR a
   selector). Only our ``eN`` refs resolve natively; a selector is refused rather
   than silently mis-targeted.
6. Screenshot results (base64 from Electron) are saved to a file so the agent
   receives a PATH, not inline image bytes.
7. The screenshot-mirror pump is disabled in native mode.
"""

import base64
import json
from unittest.mock import patch

import pytest

from kiro_crew import mcp_playwright_proxy as proxy


@pytest.fixture(autouse=True)
def _isolate_native_panel_seen():
    """Keep `_native_panel_seen` from leaking out of this module.

    It is a process-global by design (one proxy process serves one session), and
    several tests here drive the real code path that sets it. Without this reset a
    success case would latch it True for the whole pytest worker, and any later
    module that exercises the screenshot-mirror pump would then see the mirror
    correctly suppressed and fail -- a cross-file false failure.
    """
    original = proxy._native_panel_seen
    try:
        yield
    finally:
        proxy._native_panel_seen = original


def _call(op_name="browser_navigate", args=None, req_id=7):
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": op_name, "arguments": args or {"url": "https://example.com/"}},
    }


class _Resp:
    """Minimal context-manager stand-in for ``loopback_urlopen``."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok(result):
    return _Resp(json.dumps({"id": "c1", "ok": True, "result": result}).encode())


def _session(key="dashboard:chat-1"):
    return patch.object(proxy, "_SESSION_KEY", key)


def _panel_seen(value=True):
    """Pin whether a native panel has demonstrably answered for this session.

    The no-split-brain refusal is gated on this: an unmapped ``browser_*`` tool is
    only refused once a panel has PROVEN to exist, because only then can a
    Playwright-served read contradict the page the agent is driving.
    """
    return patch.object(proxy, "_native_panel_seen", value)


# ── mapped ops all route native ───────────────────────────────────────────


def test_every_mapped_op_routes_native() -> None:
    """Each tool in ``_NATIVE_OPS`` reaches the gateway command bus with its verb."""
    assert proxy._NATIVE_OPS, "the map must be populated"
    # Minimal valid args per op so translation succeeds.
    args_for = {
        "browser_click": {"target": "e5"},
        "browser_type": {"target": "e5", "text": "hi"},
        "browser_hover": {"target": "e5"},
        "browser_select_option": {"target": "e5", "values": ["a"]},
        "browser_press_key": {"key": "Enter"},
        "browser_evaluate": {"function": "() => 1"},
        "browser_wait_for": {"time": 1},
        "browser_navigate": {"url": "https://x/"},
        "browser_navigate_back": {},
        "browser_snapshot": {},
        "browser_console_messages": {"level": "error"},
        "browser_take_screenshot": {"type": "png", "scale": "css"},
    }
    for tool, op in proxy._NATIVE_OPS.items():
        seen = {}

        def _capture(req, timeout=None):
            seen["body"] = json.loads(req.data.decode())
            return _ok("done")

        with _session(), patch.object(proxy, "loopback_urlopen", side_effect=_capture):
            out = proxy._try_native_tool_call(_call(tool, args_for[tool]))
        assert out is not None, f"{tool} must be intercepted, not forwarded"
        assert "result" in out, f"{tool} should return an MCP result"
        assert seen["body"]["op"] == op, f"{tool} must send wire-op {op!r}"


def test_success_is_returned_as_an_mcp_result() -> None:
    with _session(), patch.object(proxy, "loopback_urlopen", return_value=_ok("navigated")):
        out = proxy._try_native_tool_call(_call())
    assert out is not None
    assert out["result"]["isError"] is False
    assert out["result"]["content"][0]["text"] == "navigated"
    assert out["id"] == 7


# ── no-split-brain: unmapped browser_* is refused, never forwarded ─────────


def test_unmapped_browser_tool_errors_and_does_not_fall_back() -> None:
    """A ``browser_*`` tool with no native mapping must not reach Playwright.

    Precondition: a panel has already answered, so a native page IS being driven.
    """
    assert "browser_drag" not in proxy._NATIVE_OPS
    with _session(), _panel_seen(), patch.object(proxy, "loopback_urlopen") as urlopen:
        out = proxy._try_native_tool_call(_call("browser_drag", {"startElement": "a"}))
    assert out is not None, "an unmapped browser_* tool must NOT fall through to Playwright"
    assert "error" in out and "result" not in out
    assert "browser_drag" in out["error"]["message"]
    assert "not supported" in out["error"]["message"].lower()
    urlopen.assert_not_called()  # never even reached the gateway


def test_unmapped_browser_tool_falls_back_when_no_panel_has_answered() -> None:
    """Remote gateway / non-Electron host: session key present, but NO native panel.

    Split brain is unreachable until a panel actually drives a page, so every
    unmapped ``browser_*`` tool must keep working on Playwright. Refusing here
    would strip cookie/storage/tab/network tooling from every remote user.
    """
    assert "browser_drag" not in proxy._NATIVE_OPS
    with _session(), _panel_seen(False):
        assert proxy._try_native_tool_call(_call("browser_drag", {"startElement": "a"})) is None


def test_a_panel_answer_marks_presence_and_arms_the_refusal() -> None:
    """One answered native op flips presence, which then arms the refusal."""
    with _session(), _panel_seen(False):
        with patch.object(proxy, "loopback_urlopen", return_value=_ok({"url": "x"})):
            assert proxy._try_native_tool_call(_call()) is not None
        assert proxy._native_panel_seen is True
        # Now that a native page is proven, an unmapped tool is refused.
        out = proxy._try_native_tool_call(_call("browser_drag", {"startElement": "a"}))
    assert out is not None and "error" in out


def test_transport_failure_does_not_mark_presence() -> None:
    """A 503/timeout must NOT arm the refusal -- that is the remote-host case."""
    with _session(), _panel_seen(False):
        with patch.object(proxy, "loopback_urlopen", side_effect=OSError("no panel")):
            assert proxy._try_native_tool_call(_call()) is None
        assert proxy._native_panel_seen is False


def test_non_browser_tool_is_never_intercepted() -> None:
    with _session():
        assert proxy._try_native_tool_call(_call("some_other_tool")) is None


def test_non_tool_call_is_never_intercepted() -> None:
    assert proxy._try_native_tool_call({"method": "initialize", "id": 1}) is None


# ── refusal vs transport ───────────────────────────────────────────────────


def test_refusal_returns_an_mcp_error_and_does_not_fall_back() -> None:
    """``ok:false`` is an ANSWER, not an absent transport -- never fall back."""
    payload = json.dumps(
        {"id": "c1", "ok": False, "error": "browser control refused: agent-act-not-authorized"}
    ).encode()
    with _session(), patch.object(proxy, "loopback_urlopen", return_value=_Resp(payload)):
        out = proxy._try_native_tool_call(_call())
    assert out is not None, "a refusal must NOT fall through to Playwright"
    assert "error" in out and "result" not in out
    assert "agent-act-not-authorized" in out["error"]["message"]
    assert out["id"] == 7


def test_only_no_panel_http_statuses_fall_back() -> None:
    """A REACHABLE gateway that answers with a status is not absent transport.

    503/504 mean "no panel to drive" -> Playwright is correct. Every other status
    (403 secret mismatch, 429, 500) comes from a gateway that IS reachable, so
    falling back would re-run the op elsewhere and turn a refusal into an allow
    by another route.
    """
    import urllib.error

    def _http(code: int):
        return urllib.error.HTTPError(
            "http://127.0.0.1/api/browser/command", code, "err", {}, None  # type: ignore[arg-type]
        )

    for code in (503, 504):
        with _session(), patch.object(proxy, "loopback_urlopen", side_effect=_http(code)):
            assert proxy._try_native_tool_call(_call()) is None, f"{code} must fall back"

    for code in (403, 429, 500):
        with _session(), patch.object(proxy, "loopback_urlopen", side_effect=_http(code)):
            out = proxy._try_native_tool_call(_call())
        assert out is not None, f"HTTP {code} must NOT fall back to Playwright"
        assert "error" in out and "result" not in out
        assert str(code) in out["error"]["message"]


def test_undecodable_panel_response_does_not_fall_back() -> None:
    """The panel is reachable but answered garbage -- surface it, never re-route."""
    with _session(), patch.object(proxy, "loopback_urlopen", return_value=_Resp(b"not json")):
        out = proxy._try_native_tool_call(_call())
    assert out is not None and "error" in out


def test_transport_failure_does_fall_back() -> None:
    """No panel / timeout / connection error -> Playwright is the right target."""
    with _session(), patch.object(
        proxy, "loopback_urlopen", side_effect=OSError("no panel")
    ):
        assert proxy._try_native_tool_call(_call()) is None


def test_empty_session_key_delegates_resolution_to_gateway_via_host_pid() -> None:
    """Warm pool: the frozen-env key is empty, so the proxy does NOT bail. It
    sends its ``host_pid`` and lets the GATEWAY resolve the authoritative session
    key (signed session_pid sidecar) -- the same mechanism the frame path uses.
    A mapped op therefore still ATTEMPTS the native POST; only a transport gap
    (503 / timeout) sends it to Playwright."""
    import os as _os

    seen: dict = {}

    def _capture(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        return _ok("navigated")

    with patch.object(proxy, "_SESSION_KEY", ""), patch.object(
        proxy, "loopback_urlopen", side_effect=_capture
    ):
        out = proxy._try_native_tool_call(_call())
    assert out is not None and "result" in out, "an empty key must not short-circuit to Playwright"
    assert seen["body"]["host_pid"] == _os.getpid(), "the proxy's pid is sent for gateway resolution"
    assert seen["body"]["session_key"] == "", "the frozen-env key rides along as an empty fallback"


def test_host_pid_is_sent_even_when_session_key_is_known() -> None:
    """Per-session spawns also carry host_pid, so the gateway can prefer the
    authoritative resolution and the two paths behave identically."""
    seen: dict = {}

    def _capture(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        return _ok("navigated")

    with _session("dashboard:chat-1"), patch.object(
        proxy, "loopback_urlopen", side_effect=_capture
    ):
        assert proxy._try_native_tool_call(_call()) is not None
    assert "host_pid" in seen["body"] and isinstance(seen["body"]["host_pid"], int)
    assert seen["body"]["session_key"] == "dashboard:chat-1"


def test_empty_session_key_still_falls_back_on_no_panel() -> None:
    """When the gateway cannot resolve a panel it answers 503, and THAT (a
    transport gap) is what routes the op to Playwright -- never a client guess."""
    import urllib.error

    err = urllib.error.HTTPError(
        "http://127.0.0.1/api/browser/command", 503, "no-native-panel", {}, None  # type: ignore[arg-type]
    )
    with patch.object(proxy, "_SESSION_KEY", ""), patch.object(
        proxy, "loopback_urlopen", side_effect=err
    ):
        assert proxy._try_native_tool_call(_call()) is None


def test_unmapped_tool_falls_back_until_a_panel_is_proven() -> None:
    """An unmapped ``browser_*`` tool still falls back while no native panel has
    answered, regardless of the session key -- the split-brain refusal only
    engages once a panel is proven live."""
    with patch.object(proxy, "_SESSION_KEY", ""), _panel_seen(False):
        assert proxy._try_native_tool_call(_call("browser_drag", {})) is None


# ── argument translation: target ref vs selector ──────────────────────────


def test_ref_target_is_translated_to_ref_wire_field() -> None:
    seen = {}

    def _capture(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        return _ok("clicked")

    with _session(), patch.object(proxy, "loopback_urlopen", side_effect=_capture):
        out = proxy._try_native_tool_call(
            _call("browser_click", {"target": "e12", "element": "the Submit button"})
        )
    assert out is not None and "result" in out
    assert seen["body"]["args"]["ref"] == "e12"
    assert "target" not in seen["body"]["args"]
    assert "element" not in seen["body"]["args"], "human 'element' description is dropped"


def test_selector_target_is_refused_not_mis_targeted() -> None:
    with _session(), patch.object(proxy, "loopback_urlopen") as urlopen:
        out = proxy._try_native_tool_call(_call("browser_click", {"target": "#submit-btn"}))
    assert out is not None and "error" in out
    assert "e5" in out["error"]["message"] or "ref" in out["error"]["message"].lower()
    urlopen.assert_not_called()


def test_missing_required_target_is_refused() -> None:
    with _session(), patch.object(proxy, "loopback_urlopen") as urlopen:
        out = proxy._try_native_tool_call(_call("browser_type", {"text": "hi"}))
    assert out is not None and "error" in out
    urlopen.assert_not_called()


def test_stray_target_on_non_element_op_is_refused() -> None:
    with _session(), patch.object(proxy, "loopback_urlopen") as urlopen:
        out = proxy._try_native_tool_call(_call("browser_navigate", {"url": "x", "target": "e1"}))
    assert out is not None and "error" in out
    urlopen.assert_not_called()


# ── screenshot -> saved to a path ──────────────────────────────────────────


def test_screenshot_result_is_saved_to_a_path() -> None:
    data = base64.b64encode(b"\x89PNGfakebytes").decode()
    payload = _ok({"data": data, "mimeType": "image/png"})
    with _session(), patch.object(
        proxy, "loopback_urlopen", return_value=payload
    ), patch.object(proxy, "_save_screenshot", return_value="/tmp/kirocrew-screenshots/s.png") as save:
        out = proxy._try_native_tool_call(_call("browser_take_screenshot", {"type": "png", "scale": "css"}))
    save.assert_called_once()
    assert save.call_args[0][0] == data, "the base64 payload is passed to _save_screenshot"
    assert out is not None and "result" in out
    text = out["result"]["content"][0]["text"]
    assert "Screenshot saved:" in text
    assert "/tmp/kirocrew-screenshots/s.png" in text


# ── pump disabled in native mode ───────────────────────────────────────────


def test_mirror_is_suppressed_only_once_native_routing_is_live() -> None:
    """The mirror must keep working until a native panel actually proves out.

    Suppressing it statically (because the op map is non-empty) blanked the panel
    on every host that still falls back to Playwright -- a remote gateway, where
    the proxy is the ONLY producer of `/api/browser/frame`, would render nothing.
    """
    # Pump thread stays enabled; suppression is decided per frame at call time.
    assert proxy._pump_enabled is True

    # Satisfy the pump's other gates (recent activity + a live subscriber) so the
    # only variable under test is native liveness.
    now = 1_000_000.0
    with (
        patch.object(proxy, "_last_browse_activity", now),
        patch.object(proxy, "_last_subscriber_count", 1),
        patch.object(proxy, "_pump_inflight_id", None),
    ):
        with patch.object(proxy, "_native_panel_seen", False):
            assert proxy._should_pump(now) is True, "mirror must work before native proves out"
        with patch.object(proxy, "_native_panel_seen", True):
            assert proxy._should_pump(now) is False, "mirror must stop once native is live"

    with patch.object(proxy, "_native_panel_seen", True):
        assert proxy._post_pump_audit() is False


def test_native_mode_suppresses_the_dashboard_mirror_frame() -> None:
    """Once native is live the embedded view is visible; frame POSTs are a no-op."""
    with patch.object(proxy, "_native_panel_seen", True):
        with patch.object(proxy.threading, "Thread") as thread:
            proxy._post_frame_to_gateway(b"bytes", "png")
    thread.assert_not_called()
