"""Tests for the ``design_tweak_update_thread`` agent tool, which this app serves itself.

The tool is the agent's ONLY path to this app's ``POST /thread`` route. The app declares
it in ``app.json`` (``agentTools``) and serves it from its own backend process; the core
lists it in ``kirocrew-core`` and forwards each call (``apps.app_agent_tools``). Four
planes must stay consistent, and each has a failure mode this module pins:

- the **declaration**: the tool name, title, answer cap and the schema the core validates
  every call against -- ``status`` restricted to the one forward-progress value and each
  string bounded. The id grammar and the ``text or status`` rule are the backend's
  (``_h_thread``), not restated in the schema;
- the **backend**: ``POST /api/agent-tools`` turns the call into exactly the ``/thread``
  write the tool always made (``id`` and, when given, ``cid`` in the query; the agent's
  note and status in a ``role: agent`` body), so ``_h_thread`` stays the one place that
  validates ids and redacts the note;
- the **gateway**: no route of this app is admitted to internal-secret callers; a call
  reaches the backend only through the core's one forwarding route and the signed proxy;
- the **proxy** strips the gateway's own credentials before the app process sees them.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from kiro_crew.apps import app_agent_tools
from kiro_crew.apps.app_agent_tools import DeclaredTool, build_tool_schema
from kiro_crew.apps.builtins.design_tweak.backend import server
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.routes import _PROXY_STRIP_HEADERS
from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS, _STRICT_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import internal_path_matches
from kiro_crew.validation import ValidationError, validate_tool_args

_TOOL = "design_tweak_update_thread"
_MANIFEST = AppManifest.from_json_file(Path(__file__).parents[1] / "app.json")


def _declared() -> DeclaredTool:
    (decl,) = [t for t in _MANIFEST.agentTools.tools if t.name == _TOOL]
    return DeclaredTool(app=_MANIFEST.name, route=_MANIFEST.agentTools.route, decl=decl)


class TestDeclaration(unittest.TestCase):
    def test_the_manifest_declares_the_tool_cleanly(self):
        self.assertEqual(_MANIFEST.agentTools.validate(_MANIFEST.name), [])
        self.assertEqual([t.name for t in _MANIFEST.agentTools.tools], [_TOOL])
        self.assertEqual(_MANIFEST.agentTools.route, "agent-tools")
        tool = _declared().decl
        self.assertEqual(tool.title, "Update design-tweak thread")
        self.assertEqual(tool.max_result_chars, 20_000)
        self.assertEqual(tool.input_schema["required"], ["request_id"])

    def test_the_shipped_declaration_is_read_as_first_party(self):
        """The reader takes the block from this shipped ``app.json``, for a first-party slot only."""
        rows = ("apps", (_MANIFEST.name, 0, 0))
        tools = app_agent_tools._scan(rows, frozenset({_MANIFEST.name}))
        self.assertEqual(
            [(t.app, t.route, t.name) for t in tools], [(_MANIFEST.name, "agent-tools", _TOOL)]
        )
        self.assertEqual(app_agent_tools._scan(rows, frozenset()), ())

    def test_the_deny_universe_names_the_tool_before_the_app_is_installed(self):
        """A scoped app agent's exclude list is fixed when it is built, so it must name this tool."""
        tools = app_agent_tools.shipped_tools.__wrapped__()
        self.assertIn((_MANIFEST.name, _TOOL), [(t.app, t.name) for t in tools])

    def _validate(self, **kwargs: Any) -> dict[str, Any]:
        return validate_tool_args(kwargs, build_tool_schema(_declared()))

    def test_a_text_note_passes(self):
        cleaned = self._validate(request_id="r-1", comment_id="c.2", text="editing")
        self.assertEqual(cleaned, {"request_id": "r-1", "comment_id": "c.2", "text": "editing"})

    def test_a_status_only_done_passes(self):
        self.assertEqual(self._validate(request_id="r-1", status="done")["status"], "done")

    def test_status_is_restricted_to_done(self):
        """Forward progress only: the tool can never carry a destructive status."""
        for bad in ("resolve", "clear", "new", "sent", "dismiss", "DONE"):
            with self.assertRaises(ValidationError):
                self._validate(request_id="r-1", status=bad)

    def test_request_id_is_required(self):
        with self.assertRaises(ValidationError):
            self._validate(text="x")

    def test_each_string_is_bounded(self):
        for field, cap in (("request_id", 200), ("comment_id", 200), ("text", 2_048)):
            args = {"request_id": "r", field: "a" * (cap + 1)}
            with self.assertRaises(ValidationError, msg=field):
                self._validate(**args)

    def test_an_undeclared_argument_is_refused(self):
        with self.assertRaises(ValidationError):
            self._validate(request_id="r", role="user")


class _Handler(server.Handler):
    """A socketless backend handler that records the ``/thread`` call it would make."""

    def __init__(self, envelope: dict[str, Any]) -> None:
        self.path = "/api/agent-tools"
        self._cached_body = json.dumps(envelope).encode("utf-8")
        self.thread_calls: list[tuple[dict[str, list[str]], dict[str, Any]]] = []
        self.json_sent: list[tuple[int, Any]] = []

    def _h_thread(self, query: dict[str, list[str]]) -> None:
        self.thread_calls.append((query, self._read_body()))

    def _json(self, code: int, payload: Any) -> None:
        self.json_sent.append((code, payload))


def _serve(arguments: dict[str, Any], tool: str = _TOOL) -> _Handler:
    handler = _Handler({"tool": tool, "arguments": arguments})
    handler._h_agent_tools()
    return handler


class TestBackendServesTheTool(unittest.TestCase):
    """``/agent-tools`` is the ``/thread`` write the tool always made."""

    def test_per_comment_call_carries_id_and_cid(self):
        h = _serve({"request_id": "req-1", "comment_id": "cmt-2", "text": "editing"})
        self.assertEqual(
            h.thread_calls,
            [({"id": ["req-1"], "cid": ["cmt-2"]}, {"role": "agent", "text": "editing"})],
        )

    def test_request_level_call_omits_cid(self):
        h = _serve({"request_id": "req-1", "text": "rebuilding"})
        self.assertEqual(h.thread_calls[0][0], {"id": ["req-1"]})

    def test_body_is_role_agent_with_text_and_status(self):
        h = _serve({"request_id": "r", "comment_id": "c", "text": "done!", "status": "done"})
        self.assertEqual(h.thread_calls[0][1], {"role": "agent", "text": "done!", "status": "done"})

    def test_status_only_body_has_no_text_key(self):
        h = _serve({"request_id": "r", "comment_id": "c", "status": "done"})
        self.assertEqual(h.thread_calls[0][1], {"role": "agent", "status": "done"})

    def test_the_note_reaches_the_thread_write_unchanged(self):
        """``_h_thread`` redacts every note before it persists; a scrub here would be a second one."""
        note = "token ghp_" + "A" * 36
        self.assertEqual(_serve({"request_id": "r", "text": note}).thread_calls[0][1]["text"], note)

    def test_an_undeclared_tool_is_not_served(self):
        h = _serve({"request_id": "r", "text": "x"}, tool="design_tweak_clear")
        self.assertEqual(h.thread_calls, [])
        self.assertEqual(h.json_sent[0][0], 404)

    def test_the_route_is_dispatched_by_post(self):
        seen: list[str] = []

        class _Routed(_Handler):
            def _authorized(self, *_a: Any, **_k: Any) -> bool:
                return True

            def _read_raw_body(self) -> bytes:
                return self._cached_body

            def _h_agent_tools(self) -> None:
                seen.append(self.path)

        _Routed({"tool": _TOOL, "arguments": {}}).do_POST()
        self.assertEqual(seen, ["/api/agent-tools"])


class TestGatewayAdmission(unittest.TestCase):
    """No route of this app is reachable with the internal secret."""

    def test_no_app_route_is_admitted(self):
        for path in (
            "/apps/design-tweak/api/thread",
            "/apps/design-tweak/api/agent-tools",
            "/apps/design-tweak/api/submit",
            "/apps/design-tweak/api/clear",
            "/api/apps/design-tweak",
        ):
            self.assertFalse(internal_path_matches(path, _MIXED_INTERNAL_API_PATHS), path)
            self.assertFalse(internal_path_matches(path, _STRICT_INTERNAL_API_PATHS), path)


class TestProxyStripsGatewayAuth(unittest.TestCase):
    """The app process never receives the gateway-wide secret or a session identity."""

    def test_gateway_auth_headers_are_in_the_strip_set(self):
        for h in (
            "x-internal-secret",
            "x-session-token",
            "x-session-key",
            "x-internal-caller",
            "cookie",
            "authorization",
        ):
            self.assertIn(h, _PROXY_STRIP_HEADERS, h)


if __name__ == "__main__":
    unittest.main()
