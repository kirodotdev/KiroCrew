"""Session-identity resolution and fallback for the ``browser`` MCP tool.

Guards the two fixes for the default-install 403:

* an UNRESOLVED session must NOT fabricate a placeholder key and fire a
  request the strict route is guaranteed to 403 — it must degrade to the
  playwright-cli fallback text, exactly like the no-native-panel (503) case;
* a RESOLVED session must POST the dashboard:-namespaced key in the
  ``X-Session-Key`` header (what the gateway peer check compares against) and
  the BARE slot id in the request body (what the native panel is keyed under).
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from kiro_crew.mcp_tools import browser as mod


@pytest.fixture(autouse=True)
def _browsing_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # Presence of the CLI is the browse consent gate; force it on so the tool
    # runs its body instead of the "not set up" early return.
    monkeypatch.setattr(mod, "_browsing_available", lambda: True)
    # The header encodability guard is orthogonal to what these tests exercise.
    monkeypatch.setattr(mod.mcp_core, "_session_key_header_error", lambda sk: None)
    # Default: governance permits browsing (its own deny is covered separately).
    monkeypatch.setattr(mod.mcp_core, "_vet_browse_governance", lambda s: None)
    # Default: the built-in-browser preference is ON (its OFF path is covered
    # separately); otherwise every test would route to the playwright fallback.
    monkeypatch.setattr(mod, "_use_builtin_browser", lambda: True)


def _capture_post(recorder: list[tuple[str, str, dict, str, int]], status: int, payload: dict):
    def _fake(
        bus_key: str, op: str, args: dict, session_header: str, timeout_ms: int
    ) -> tuple[int, dict]:
        recorder.append((bus_key, op, args, session_header, timeout_ms))
        return status, payload

    return _fake


def test_unresolved_session_falls_back_without_posting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod.mcp_core, "_resolve_session_key", lambda: "")

    def _must_not_post(*_a: Any, **_k: Any) -> tuple[int, dict]:
        raise AssertionError("must not POST when the session is unresolved")

    monkeypatch.setattr(mod, "_post_command", _must_not_post)

    out = mod.browser("browser", {"op": "navigate", "args": {"url": "https://example.com"}})
    assert out == mod._FALLBACK_TEXT


def test_resolved_session_posts_namespaced_header_and_bare_bus_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod.mcp_core, "_resolve_session_key", lambda: "dashboard:chat-7-1786752973")
    calls: list[tuple[str, str, dict, str]] = []
    monkeypatch.setattr(mod, "_post_command", _capture_post(calls, 200, {"ok": True, "result": "ok"}))

    out = mod.browser("browser", {"op": "navigate", "args": {"url": "https://example.com"}})

    assert len(calls) == 1
    bus_key, op, args, session_header, timeout_ms = calls[0]
    assert session_header == "dashboard:chat-7-1786752973"  # namespaced -> header
    assert bus_key == "chat-7-1786752973"  # bare slot id -> body
    assert op == "navigate"
    assert args == {"url": "https://example.com"}
    assert timeout_ms == 60000  # navigate is a slow op -> longer ceiling
    assert out.startswith("Browser navigate:")


def test_bus_key_left_as_is_when_not_namespaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod.mcp_core, "_resolve_session_key", lambda: "chat-9-1786000000")
    calls: list[tuple[str, str, dict, str]] = []
    monkeypatch.setattr(mod, "_post_command", _capture_post(calls, 200, {"ok": True, "result": "ok"}))

    mod.browser("browser", {"op": "snapshot"})

    bus_key, _op, _args, session_header, timeout_ms = calls[0]
    assert bus_key == "chat-9-1786000000"
    assert session_header == "chat-9-1786000000"
    assert timeout_ms == 15000  # non-slow op -> default ceiling


def test_no_native_panel_503_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.mcp_core, "_resolve_session_key", lambda: "dashboard:chat-7-1786752973")
    monkeypatch.setattr(
        mod, "_post_command", _capture_post([], 503, {"code": "no_native_panel"})
    )

    out = mod.browser("browser", {"op": "navigate", "args": {"url": "https://example.com"}})
    assert out == mod._FALLBACK_TEXT


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:5476/api/spawn",  # loopback control plane
        "http://localhost:8080/",           # loopback name
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata (IMDS)
        "http://10.0.0.5/",                 # RFC1918 private
        "http://192.168.1.1/",              # RFC1918 private
        "file:///etc/passwd",               # non-http scheme
        "http://[::1]/",                    # IPv6 loopback
    ],
)
def test_navigate_to_non_public_target_is_refused_without_posting(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setattr(mod.mcp_core, "_resolve_session_key", lambda: "dashboard:chat-7-1")

    def _must_not_post(*_a: Any, **_k: Any) -> tuple[int, dict]:
        raise AssertionError("must not POST a non-public navigate target")

    monkeypatch.setattr(mod, "_post_command", _must_not_post)

    out = mod.browser("browser", {"op": "navigate", "args": {"url": url}})
    assert out.startswith("Error: the browser tool only opens public http(s) URLs")


def test_navigate_classifier_accepts_public_and_rejects_local() -> None:
    assert mod._navigate_target_is_public("https://example.com/path?q=1") is True
    assert mod._navigate_target_is_public("http://8.8.8.8/") is True
    assert mod._navigate_target_is_public("http://127.0.0.1/") is False
    assert mod._navigate_target_is_public("http://169.254.169.254/") is False
    assert mod._navigate_target_is_public("http://localhost/") is False
    assert mod._navigate_target_is_public("ftp://example.com/") is False


def test_navigate_classifier_rejects_alternate_ip_encodings() -> None:
    # 2852039166 == 0xa9fea9fe == 169.254.169.254 (IMDS); Chromium would
    # normalize these, so the gate must canonicalize before classifying.
    assert mod._navigate_target_is_public("http://2852039166/latest/meta-data/") is False
    assert mod._navigate_target_is_public("http://0xa9fea9fe/") is False
    assert mod._navigate_target_is_public("http://0x7f000001/") is False  # 127.0.0.1
    assert mod._navigate_target_is_public("http://2130706433/") is False  # 127.0.0.1


def test_governance_deny_refuses_without_fallback_or_posting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A governance deny must refuse outright — NOT fall back to playwright-cli
    # (that would let browsing continue) and NOT POST to the bus.
    monkeypatch.setattr(mod.mcp_core, "_resolve_session_key", lambda: "dashboard:chat-7-1")
    monkeypatch.setattr(
        mod.mcp_core, "_vet_browse_governance", lambda s: "web browsing is disabled by governance policy"
    )

    def _must_not_post(*_a: Any, **_k: Any) -> tuple[int, dict]:
        raise AssertionError("must not POST when browsing is denied by policy")

    monkeypatch.setattr(mod, "_post_command", _must_not_post)

    out = mod.browser("browser", {"op": "navigate", "args": {"url": "https://example.com"}})
    assert out.startswith("Error: web browsing is disabled by governance policy")
    assert out != mod._FALLBACK_TEXT  # not the playwright downgrade


def test_builtin_off_falls_back_to_playwright_without_posting(monkeypatch: pytest.MonkeyPatch) -> None:
    # With the built-in-browser preference OFF, browsing is still allowed (unlike
    # a governance deny) -- the tool returns the dedicated built-in-off message
    # (naming the setting, not a missing panel) and never touches the native panel.
    monkeypatch.setattr(mod.mcp_core, "_resolve_session_key", lambda: "dashboard:chat-7-1")
    monkeypatch.setattr(mod, "_use_builtin_browser", lambda: False)

    def _must_not_post(*_a: Any, **_k: Any) -> tuple[int, dict]:
        raise AssertionError("must not POST when the built-in browser is off")

    monkeypatch.setattr(mod, "_post_command", _must_not_post)

    out = mod.browser("browser", {"op": "navigate", "args": {"url": "https://example.com"}})
    assert out == mod._DISABLED_TEXT
    assert "turned off in Settings" in out


def test_governance_deny_wins_over_builtin_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pins the ordering invariant: a governance DENY must win even when the
    # built-in-browser preference is OFF. If the OFF short-circuit ever moved
    # ahead of the governance gate, a denied session would get _FALLBACK_TEXT and
    # keep browsing via playwright -- exactly the control the ordering protects.
    monkeypatch.setattr(mod.mcp_core, "_resolve_session_key", lambda: "dashboard:chat-7-1")
    monkeypatch.setattr(
        mod.mcp_core, "_vet_browse_governance", lambda s: "web browsing is disabled by governance policy"
    )
    monkeypatch.setattr(mod, "_use_builtin_browser", lambda: False)

    def _deny_post(*_a: Any, **_k: Any) -> tuple[int, dict]:
        raise AssertionError("must not POST when browsing is governance-denied")

    monkeypatch.setattr(mod, "_post_command", _deny_post)

    out = mod.browser("browser", {"op": "navigate", "args": {"url": "https://example.com"}})
    assert out.startswith("Error: web browsing is disabled by governance policy")
    assert out != mod._FALLBACK_TEXT


def test_navigate_classifier_rejects_parser_differential_hosts() -> None:
    # urlsplit vs Chromium disagree on the authority when a backslash / control
    # char is present; reject before parsing.
    assert mod._navigate_target_is_public("http://169.254.169.254\\@example.com/") is False
    assert mod._navigate_target_is_public("http://example.com\\@169.254.169.254/") is False
    assert mod._navigate_target_is_public("http://example.com\t@169.254.169.254/") is False
    assert mod._navigate_target_is_public("http://exa mple.com/") is False
    assert mod._navigate_target_is_public("https://example.com/") is True


def test_object_valued_arg_is_rejected_without_posting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.mcp_core, "_resolve_session_key", lambda: "dashboard:chat-7-1")

    def _must_not_post(*_a: Any, **_k: Any) -> tuple[int, dict]:
        raise AssertionError("must not POST a non-scalar arg")

    monkeypatch.setattr(mod, "_post_command", _must_not_post)

    out = mod.browser("browser", {"op": "type", "args": {"ref": "e5", "text": {"nested": 1}}})
    assert out.startswith("Error: args values must be")


def test_screenshot_result_is_not_a_truncated_blob() -> None:
    # A base64 image would be corrupted by the length cap; screenshot returns a
    # safe confirmation instead, never a sliced blob.
    out = mod._result_text("screenshot", "data:image/png;base64," + "A" * 5000)
    assert "captured" in out
    assert "AAAA" not in out


def test_result_text_renders_and_redacts_non_screenshot() -> None:
    assert mod._result_text("snapshot", None) == "Browser snapshot: ok"
    assert mod._result_text("snapshot", "hello") == "Browser snapshot: hello"


def test_schemas_always_advertises_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    # Always advertised (registry invariant): even when browsing is not set up,
    # the descriptor is present; browser() degrades at call time instead.
    monkeypatch.setattr(mod, "_browsing_available", lambda: True)
    assert mod.schemas()[0]["name"] == "browser"
    monkeypatch.setattr(mod, "_browsing_available", lambda: False)
    assert mod.schemas()[0]["name"] == "browser"


def test_browsing_not_set_up_degrades_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_browsing_available", lambda: False)
    out = mod.browser("browser", {"op": "snapshot"})
    assert out.startswith("Error: browsing is not set up")


def test_op_ran_but_failed_surfaces_the_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.mcp_core, "_resolve_session_key", lambda: "dashboard:chat-7-1")
    monkeypatch.setattr(mod, "_post_command", _capture_post([], 200, {"ok": False, "error": "boom"}))
    out = mod.browser("browser", {"op": "snapshot"})
    assert out == "Error: boom"


def test_hard_non_200_is_surfaced_not_masked_as_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.mcp_core, "_resolve_session_key", lambda: "dashboard:chat-7-1")
    monkeypatch.setattr(mod, "_post_command", _capture_post([], 429, {"code": "queue_full", "error": "queue-full"}))
    out = mod.browser("browser", {"op": "snapshot"})
    assert out.startswith("Error: browser command failed (HTTP 429, queue_full)")


class _FakeResp:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self) -> bytes:
        return self._body


def test_post_command_parses_success_and_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.mcp_core, "_api_base", lambda: "http://127.0.0.1:9")
    monkeypatch.setattr(mod.mcp_core, "_internal_secret", lambda: "sekret")

    # 200 success -> (status, parsed body)
    monkeypatch.setattr(
        mod.mcp_core, "_api_urlopen", lambda req, timeout: _FakeResp(200, b'{"ok": true}')
    )
    status, payload = mod._post_command("chat-1", "snapshot", {}, "dashboard:chat-1", 15000)
    assert status == 200 and payload == {"ok": True}

    # HTTPError -> (code, parsed-or-empty)
    def _raise_http(req, timeout):
        raise mod.urllib.error.HTTPError("u", 503, "x", None, io.BytesIO(b'{"code":"no_native_panel"}'))

    monkeypatch.setattr(mod.mcp_core, "_api_urlopen", _raise_http)
    status, payload = mod._post_command("chat-1", "snapshot", {}, "dashboard:chat-1", 15000)
    assert status == 503 and payload.get("code") == "no_native_panel"

    # URLError -> (None, {})  [connection failure == no native panel here]
    def _raise_url(req, timeout):
        raise mod.urllib.error.URLError("refused")

    monkeypatch.setattr(mod.mcp_core, "_api_urlopen", _raise_url)
    status, payload = mod._post_command("chat-1", "snapshot", {}, "dashboard:chat-1", 15000)
    assert status is None and payload == {}
