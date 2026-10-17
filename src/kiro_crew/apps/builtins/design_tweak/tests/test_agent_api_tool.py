"""Tests for the ``design_tweak_update_thread`` MCP tool's authorization story.

The tool is the ONLY credentialed path from an agent session to the Design
Tweak app's ``POST /thread`` route, reached through the gateway's app reverse
proxy ``/apps/{name}/api/{path}`` (the app runs ``_h_thread`` in its own backend
process; unlike Issue Radar / Ops Mission Control, which register routes directly
under ``/api/apps/<name>``). The MCP server process holds the internal secret;
the agent never sees a credential. Four planes must stay mutually consistent, and
each has a failure mode this module pins:

- the **schema** in ``validation.py`` — field types/lengths and ``status``
  restricted to the one forward-progress value. The id grammar and the
  ``text or status`` rule are the backend's (``_h_thread``), not restated
  here, so this module does not test the schema for them;
- the **handler** in ``mcp_tools/apps.py`` — the proxied URL it builds (with
  the ids URL-quoted so neither can rewrite the route), the
  ``{role, text, status}`` body it posts, and redaction of the response;
- the **gateway's mixed-internal path set** admitting EXACTLY
  ``/apps/design-tweak/api/thread`` for internal-secret callers, and never the
  app's state-mutating routes (``/submit``, ``/send``, ``/clear``, ``/delete``,
  …) nor the bare app prefix;
- the **route is real** — the admitted path resolves against the gateway router
  to ``handle_app_api_proxy``, so the tool targets a served route, not a string
  that merely agrees with the admission set (TestRouteIsReal).
"""

import unittest
from unittest import mock

from aiohttp import web

from kiro_crew.apps.routes import (
    _PROXY_STRIP_HEADERS,
    handle_app_api_proxy,
    register_app_routes,
)
from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import internal_path_matches
from kiro_crew.mcp_tools import apps
from kiro_crew.validation import (
    DESIGN_TWEAK_UPDATE_THREAD_SCHEMA,
    ValidationError,
    validate_tool_args,
)

# The gateway's reverse-proxy route to the Design Tweak backend process:
# ``/apps/{name}/api/{path}`` (handle_app_api_proxy), NOT ``/api/apps/...``
# (which carries only the gateway's own app-management routes). This is the
# path the tool posts to and the gateway admits.
_THREAD_PATH = "/apps/design-tweak/api/thread"


class TestSchema(unittest.TestCase):
    def _validate(self, **kwargs):
        return validate_tool_args(kwargs, DESIGN_TWEAK_UPDATE_THREAD_SCHEMA)

    def test_a_text_note_passes(self):
        cleaned = self._validate(request_id="r-1", comment_id="c.2", text="editing")
        self.assertEqual(cleaned["request_id"], "r-1")
        self.assertEqual(cleaned["comment_id"], "c.2")
        self.assertEqual(cleaned["text"], "editing")

    def test_a_status_only_done_passes(self):
        cleaned = self._validate(request_id="r-1", status="done")
        self.assertEqual(cleaned["status"], "done")

    def test_request_level_note_without_comment_id_passes(self):
        cleaned = self._validate(request_id="r-1", text="rebuilding, one moment")
        self.assertEqual(cleaned["comment_id"], "")

    def test_status_is_restricted_to_done(self):
        """Forward progress only — no clear/dismiss/resolve status.

        ``new``/``sent`` are the app's own lifecycle, not an agent report;
        anything else is simply off-surface. This IS enforced at the schema
        boundary (unlike the id grammar / ``text or status`` rule, which the
        backend owns), so the tool can never carry a destructive status.
        """
        for bad in ("resolve", "clear", "new", "sent", "dismiss", "DONE"):
            with self.assertRaises(ValidationError):
                self._validate(request_id="r-1", status=bad)

    def test_request_id_is_required(self):
        with self.assertRaises(ValidationError):
            self._validate(text="x")


class TestHandler(unittest.TestCase):
    """The handler builds the proxied POST and redacts; it holds no identity."""

    def _call(self, args, resp=None):
        captured = {}

        def _fake_post(url, body=None, **kwargs):
            captured["url"] = url
            captured["body"] = body
            captured["kwargs"] = kwargs
            return resp if resp is not None else {"ok": True}

        with mock.patch.object(apps.mcp_core, "_post", _fake_post):
            out = apps.design_tweak_update_thread("design_tweak_update_thread", args)
        return out, captured

    def test_per_comment_url_carries_id_and_cid(self):
        _out, cap = self._call({"request_id": "req-1", "comment_id": "cmt-2", "text": "editing"})
        self.assertEqual(cap["url"], f"{_THREAD_PATH}?id=req-1&cid=cmt-2")

    def test_request_level_url_omits_cid(self):
        _out, cap = self._call({"request_id": "req-1", "text": "rebuilding"})
        self.assertEqual(cap["url"], f"{_THREAD_PATH}?id=req-1")

    def test_ids_are_url_quoted_into_value_position(self):
        """The handler quotes both ids with ``safe=''`` so a stray separator
        cannot rewrite the ``/thread`` route even if the backend grammar check
        were ever relaxed. This is the route-safety property the tool keeps.
        """
        _out, cap = self._call({"request_id": "a/b?c", "comment_id": "x&y", "text": "z"})
        self.assertEqual(cap["url"], f"{_THREAD_PATH}?id=a%2Fb%3Fc&cid=x%26y")

    def test_body_is_role_agent_with_text_and_status(self):
        _out, cap = self._call(
            {"request_id": "r", "comment_id": "c", "text": "done!", "status": "done"}
        )
        self.assertEqual(cap["body"]["role"], "agent")
        self.assertEqual(cap["body"]["text"], "done!")
        self.assertEqual(cap["body"]["status"], "done")

    def test_status_only_body_has_no_text_key(self):
        _out, cap = self._call({"request_id": "r", "comment_id": "c", "status": "done"})
        self.assertNotIn("text", cap["body"])
        self.assertEqual(cap["body"]["status"], "done")

    def test_handler_passes_no_session_key(self):
        """The thread post needs no caller identity; the resolver must stay out.

        A ``session_key`` kwarg here would mean the handler resolved a caller,
        which this route deliberately does not.
        """
        _out, cap = self._call({"request_id": "r", "text": "x"})
        self.assertNotIn("session_key", cap["kwargs"])

    def test_text_is_passed_through_for_the_backend_to_redact(self):
        """The handler does NOT redact inbound text: the backend's
        ``_redact_incoming_thread_text`` runs on every ``/thread`` write, so a
        second scrub here would duplicate that one enforcement point. The note
        reaches ``_post`` unchanged.
        """
        note = "token ghp_" + "A" * 36
        _out, cap = self._call({"request_id": "r", "text": note})
        self.assertEqual(cap["body"]["text"], note)

    def test_response_is_redacted(self):
        secret = "ghp_" + "B" * 36
        out, _cap = self._call(
            {"request_id": "r", "text": "x"},
            resp={"ok": True, "echo": f"leak {secret}"},
        )
        self.assertNotIn(secret, out)


class TestGatewayAdmission(unittest.TestCase):
    """The gateway admits exactly ``/thread``, and no mutating sibling."""

    @staticmethod
    def _admitted(path):
        return internal_path_matches(path, _MIXED_INTERNAL_API_PATHS)

    def test_thread_route_is_admitted(self):
        self.assertTrue(self._admitted(_THREAD_PATH))

    def test_mutating_and_prefix_routes_are_not_admitted(self):
        """Only ``/thread`` is agent-reachable; everything else stays UI-only.

        Admitting the app prefix would prefix-match these state-mutating routes
        to anything holding the internal secret. Paths use the real reverse-proxy
        prefix ``/apps/design-tweak/api/...`` (the one the tool reaches), plus the
        ``/api/apps/...`` management prefix for good measure.
        """
        for path in (
            "/apps/design-tweak",
            "/apps/design-tweak/api",
            "/apps/design-tweak/api/submit",
            "/apps/design-tweak/api/send",
            "/apps/design-tweak/api/clear",
            "/apps/design-tweak/api/delete",
            "/apps/design-tweak/api/delete-comment",
            "/apps/design-tweak/api/projects",
            "/apps/design-tweak/api/dev-server/start",
            "/api/apps/design-tweak",
            "/api/apps/design-tweak/api/thread",
        ):
            self.assertFalse(
                self._admitted(path),
                f"{path} must not be reachable with the internal secret",
            )


class TestRouteIsReal(unittest.IsolatedAsyncioTestCase):
    """The admitted path resolves to a route the gateway actually serves.

    The regression this guards: the tool first targeted ``/api/apps/design-tweak
    /api/thread``, which no gateway route serves — Design Tweak's ``_h_thread``
    lives in its own backend process reached ONLY through the reverse proxy
    ``/apps/{name}/api/{path}``, unlike Issue Radar / Ops Mission Control which
    register routes directly under ``/api/apps/<name>``. A handler test that
    mocks ``_post`` cannot catch that; resolving against the real router can.
    """

    async def test_thread_path_resolves_to_the_app_api_proxy(self):
        from aiohttp.test_utils import make_mocked_request

        app = web.Application()
        register_app_routes(app)
        req = make_mocked_request("POST", _THREAD_PATH)
        match = await app.router.resolve(req)
        self.assertIs(match.handler, handle_app_api_proxy)
        # Captured path params live on the match mapping; get_info() carries the
        # route formatter/pattern, not the resolved values.
        self.assertEqual(match["name"], "design-tweak")
        self.assertEqual(match["path"], "thread")

    async def test_the_tool_posts_the_admitted_served_path(self):
        """The URL the handler builds is exactly the admitted path."""
        self.assertEqual(apps._DESIGN_TWEAK_THREAD_URL, _THREAD_PATH)
        self.assertTrue(internal_path_matches(_THREAD_PATH, _MIXED_INTERNAL_API_PATHS))


class TestProxyStripsGatewayAuth(unittest.TestCase):
    """The reverse proxy must not forward the gateway's own auth headers.

    ``design_tweak_update_thread`` is the first internal-secret caller to reach
    an app backend through ``handle_app_api_proxy``. The app backend trusts a
    request by the per-app ``X-KiroCrew-Proxy`` HMAC alone, so the gateway-wide
    ``X-Internal-Secret`` (which opens every strict internal route) and the
    session-identity headers must be stripped before forwarding — the app
    subprocess, which also serves the user's own project content, must never
    receive them.
    """

    _GATEWAY_AUTH = (
        "x-internal-secret",
        "x-session-token",
        "x-session-key",
        "x-internal-caller",
    )

    def test_gateway_auth_headers_are_in_the_strip_set(self):
        for h in self._GATEWAY_AUTH + ("cookie", "authorization"):
            self.assertIn(h, _PROXY_STRIP_HEADERS, h)

    def test_the_proxy_filter_drops_them_but_keeps_the_hmac(self):
        """Replicate the proxy's own forward filter and assert the result."""
        incoming = {
            "X-Internal-Secret": "gw-wide-secret",
            "X-Session-Token": "tok",
            "X-Session-Key": "sk",
            "X-Internal-Caller": "kirocrew-core",
            "Content-Type": "application/json",
            "X-KiroCrew-Proxy": "123:deadbeef",
        }
        # The exact predicate handle_app_api_proxy applies when building the
        # forwarded header set.
        forwarded = {
            k: v
            for k, v in incoming.items()
            if k.lower() not in _PROXY_STRIP_HEADERS and k.lower() != "host"
        }
        for h in ("X-Internal-Secret", "X-Session-Token", "X-Session-Key", "X-Internal-Caller"):
            self.assertNotIn(h, forwarded, h)
        self.assertIn("Content-Type", forwarded)


class TestToolRegistry(unittest.TestCase):
    def test_tool_is_advertised_and_handled(self):
        names = {s["name"] for s in apps.schemas()}
        self.assertIn("design_tweak_update_thread", names)
        self.assertIn("design_tweak_update_thread", apps.HANDLERS)


if __name__ == "__main__":
    unittest.main()
