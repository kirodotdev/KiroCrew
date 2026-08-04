"""Native-routing contract for the Playwright MCP proxy.

The proxy may hand a ``browser_*`` tool call to the NATIVE embedded browser panel
instead of the Playwright subprocess. Two properties of that decision are
security-relevant and pinned here:

1. A panel that ANSWERS and refuses must NOT fall back to Playwright. Falling
   back would convert a deny into an allow by another route -- when the user
   revokes "let the agent act", the control plane refuses, and forwarding the same
   op to Playwright would execute exactly what authorization just withheld.
2. Only TRANSPORT unavailability (no panel, timeout, connection error) may fall
   back, because then nothing native can answer at all.
"""

import json
from unittest.mock import patch

from kiro_crew import mcp_playwright_proxy as proxy


def _call(op_name="browser_navigate", args=None):
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": op_name, "arguments": args or {"url": "https://example.com/"}},
    }


def test_native_ops_is_empty_so_no_op_is_intercepted_yet() -> None:
    """Interception is wired but deliberately disabled.

    Routing only SOME ops natively would split the agent across two browsers: the
    ref-based tools (snapshot/click/type/evaluate) address Playwright's own page,
    so a natively-navigated view plus a Playwright snapshot is confidently wrong.
    """
    assert proxy._NATIVE_OPS == {}
    # With the map empty, nothing is intercepted regardless of the tool name.
    assert proxy._try_native_tool_call(_call()) is None


def test_non_tool_call_is_never_intercepted() -> None:
    assert proxy._try_native_tool_call({"method": "initialize", "id": 1}) is None


def test_refusal_returns_an_mcp_error_and_does_not_fall_back() -> None:
    """``ok:false`` is an ANSWER, not an absent transport -- never fall back."""
    payload = json.dumps(
        {"id": "c1", "ok": False, "error": "browser control refused: agent-act-not-authorized"}
    ).encode()

    class _Resp:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch.dict(proxy._NATIVE_OPS, {"browser_navigate": "navigate"}, clear=False), patch.object(
        proxy, "_SESSION_KEY", "dashboard:chat-1"
    ), patch.object(proxy.urllib.request, "urlopen", return_value=_Resp()):
        out = proxy._try_native_tool_call(_call())

    assert out is not None, "a refusal must NOT fall through to Playwright"
    assert "error" in out and "result" not in out
    assert "agent-act-not-authorized" in out["error"]["message"]
    assert out["id"] == 7


def test_transport_failure_does_fall_back() -> None:
    """No panel / timeout / connection error -> Playwright is the right target."""
    with patch.dict(proxy._NATIVE_OPS, {"browser_navigate": "navigate"}, clear=False), patch.object(
        proxy, "_SESSION_KEY", "dashboard:chat-1"
    ), patch.object(proxy.urllib.request, "urlopen", side_effect=OSError("no panel")):
        assert proxy._try_native_tool_call(_call()) is None


def test_success_is_returned_as_an_mcp_result() -> None:
    payload = json.dumps({"id": "c1", "ok": True, "result": "navigated"}).encode()

    class _Resp:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch.dict(proxy._NATIVE_OPS, {"browser_navigate": "navigate"}, clear=False), patch.object(
        proxy, "_SESSION_KEY", "dashboard:chat-1"
    ), patch.object(proxy.urllib.request, "urlopen", return_value=_Resp()):
        out = proxy._try_native_tool_call(_call())

    assert out is not None
    assert out["result"]["isError"] is False
    assert out["result"]["content"][0]["text"] == "navigated"


def test_missing_session_key_falls_back_rather_than_guessing() -> None:
    with patch.dict(proxy._NATIVE_OPS, {"browser_navigate": "navigate"}, clear=False), patch.object(
        proxy, "_SESSION_KEY", ""
    ):
        assert proxy._try_native_tool_call(_call()) is None
