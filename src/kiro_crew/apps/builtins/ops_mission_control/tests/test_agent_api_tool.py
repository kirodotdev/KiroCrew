"""The ``ops_mission_control_api`` agent tool, declared in ``app.json`` and served by this app.

The tool is the ONLY credentialed path from an agent session to the app's HTTP surface.
The declaration (``agentTools``) is what the gateway lists in ``kirocrew-core`` and checks
a call against; ``POST /agent-tools`` (``backend/http_routes/agent_tool.py``, registered
with the route registry by ``backend/agent_tool_hook.py``) is what decides the call.
Four planes must stay consistent, and each has a failure mode this module pins:

- the **declaration**: the tool's name, title, description, arguments, enums and bounds,
  which are the ones the core tool of the same name carried, so an agent sees the same
  tool; the path enum is the allowlist;
- the **(method, path) allowlist** in ``agent_tool.ALLOWED_CALLS``: widening it is an
  authorization change and must look like one in review;
- the **route**: every refusal lands before any handler runs, a channel agent cannot
  arm the rotation, and an allowed call is answered by the handler its own route serves,
  with the decoded body sanitized and redacted first;
- the **gateway**: a call validated by the host's field engine, posted to the host's
  ``/api/app-tools/call`` and dispatched by the host's route registry from the hook this
  ``app.json`` names arrives here unchanged, and no route of this app is admitted to
  internal-secret callers any more.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import app_agent_tools
from kiro_crew.apps.app_agent_tools import DeclaredTool, build_tool_schema
from kiro_crew.apps.builtins.ops_mission_control.backend import agent_tool_hook, ledger
from kiro_crew.apps.builtins.ops_mission_control.backend import registry as omc_registry
from kiro_crew.apps.builtins.ops_mission_control.backend import routes
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes import agent_tool
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes._shared import _json_body
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.route_registry import RouteRegistry
from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS, _STRICT_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import internal_path_matches
from kiro_crew.validation import ValidationError, validate_tool_args

APP_ROOT = Path(__file__).resolve().parents[1]
PREFIX = "/api/apps/ops-mission-control"
URL = f"{PREFIX}/agent-tools"
_MANIFEST = AppManifest.from_json_file(APP_ROOT / "app.json")


def _raw_manifest() -> dict[str, Any]:
    return json.loads((APP_ROOT / "app.json").read_text(encoding="utf-8"))


def _declared() -> DeclaredTool:
    (decl,) = _MANIFEST.agentTools.tools
    return DeclaredTool(app=_MANIFEST.name, route=_MANIFEST.agentTools.route, decl=decl)


def _call(**arguments: str) -> dict[str, Any]:
    return {"tool": agent_tool.TOOL_NAME, "arguments": arguments}


class TestTheDeclaration(unittest.TestCase):
    def test_the_manifest_declares_the_tool_cleanly(self) -> None:
        self.assertEqual(_MANIFEST.agentTools.validate(_MANIFEST.name), [])
        self.assertEqual(_MANIFEST.agentTools.route, "agent-tools")
        tool = _declared().decl
        self.assertEqual(tool.name, agent_tool.TOOL_NAME)
        self.assertEqual(tool.title, "Call Ops Mission Control")
        self.assertEqual(tool.max_result_chars, 60_000)
        self.assertEqual(tool.input_schema["required"], ["method", "path"])

    def test_the_shipped_declaration_is_read_as_first_party(self) -> None:
        rows = ("apps", (_MANIFEST.name, 0, 0))
        tools = app_agent_tools._scan(rows, frozenset({_MANIFEST.name}))
        self.assertEqual(
            [(t.app, t.route, t.name) for t in tools],
            [(_MANIFEST.name, "agent-tools", agent_tool.TOOL_NAME)],
        )
        self.assertEqual(app_agent_tools._scan(rows, frozenset()), ())

    def test_the_tool_is_the_seams_and_no_core_tool_has_its_name(self) -> None:
        """The core declares no tool of this name, so the manifest seam lists it."""
        from kiro_crew.mcp_tools import app_tools, build_tool_names

        self.assertNotIn(agent_tool.TOOL_NAME, app_tools._core_tool_names())
        (listed,) = [d for d in app_tools.schemas() if d["name"] == agent_tool.TOOL_NAME]
        self.assertEqual(listed, _declared().decl.descriptor())
        self.assertEqual(build_tool_names().count(agent_tool.TOOL_NAME), 1)

    def test_the_title_is_the_title_table_row(self) -> None:
        """The chat label and the remote-session grant read the title table by name."""
        import kiro_crew

        titles = json.loads(
            (Path(kiro_crew.__file__).parent / "data" / "mcp_tool_titles.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(_declared().decl.title, titles["kirocrew-core"][agent_tool.TOOL_NAME])

    def test_the_tool_is_granted_by_the_manifest(self) -> None:
        self.assertIn(agent_tool.TOOL_NAME, _raw_manifest()["permissions"]["mcpTools"])

    def test_the_path_enum_is_the_allowlist(self) -> None:
        props = _declared().decl.input_schema["properties"]
        self.assertEqual(set(props["path"]["enum"]), {p for _, p in agent_tool.ALLOWED_CALLS})
        self.assertEqual(props["path"]["enum"], sorted(props["path"]["enum"]))
        self.assertEqual(set(props["method"]["enum"]), {m for m, _ in agent_tool.ALLOWED_CALLS})

    def test_the_declared_bounds_are_the_routes(self) -> None:
        props = _declared().decl.input_schema["properties"]
        self.assertEqual(props["query"]["maxLength"], agent_tool.MAX_QUERY_CHARS)
        self.assertEqual(props["body_json"]["maxLength"], agent_tool.MAX_BODY_CHARS)

    def test_the_agent_surface_is_exactly_what_the_sops_use(self) -> None:
        """Every path an SOP instructs, nothing more.

        A new entry here must come WITH an SOP that needs it -- this test is the review
        speed bump for widening the agent surface.
        """
        self.assertEqual(
            agent_tool.ALLOWED_CALLS,
            frozenset(
                {
                    ("GET", "/state"),
                    ("GET", "/signals"),
                    ("GET", "/incidents"),
                    ("GET", "/handover"),
                    ("GET", "/rotation"),
                    ("GET", "/ledger"),
                    ("GET", "/ledger/contradictions"),
                    ("POST", "/dispatch"),
                    ("POST", "/incident/transition"),
                    ("POST", "/incident/claim"),
                    ("POST", "/incident/action"),
                    ("POST", "/rotation/arm"),
                    ("POST", "/ledger"),
                    ("POST", "/ledger/hygiene"),
                }
            ),
        )
        self.assertEqual(agent_tool.CHANNEL_HELD_OPERATIONS, frozenset({("POST", "/rotation/arm")}))

    def test_excluded_routes_stay_excluded(self) -> None:
        """``/incident/proposal/decide`` is the human approval gate for provider actions,
        the provider routes hold credentials, ``/webhook`` is signed external ingest."""
        allowed_paths = {p for _, p in agent_tool.ALLOWED_CALLS}
        for excluded in (
            "/incident",
            "/incident/propose",
            "/incident/proposal/decide",
            "/proposals",
            "/providers",
            "/settings",
            "/webhook",
            "/agent-tools",
        ):
            self.assertNotIn(excluded, allowed_paths)

    def test_every_allowed_call_has_the_handler_its_route_serves(self) -> None:
        """``routes._served_call`` answers a call with the object the router serves there."""
        self.assertEqual(set(routes._AGENT_TOOL_HANDLERS), agent_tool.ALLOWED_CALLS)
        app = web.Application()
        routes.register_routes(app)
        self.addCleanup(omc_registry.reset_registry)
        served = {
            (route.method, route.resource.canonical): getattr(route.handler, "__wrapped__")
            for route in app.router.routes()
            if route.resource is not None
        }
        for (method, path), name in routes._AGENT_TOOL_HANDLERS.items():
            with self.subTest(call=f"{method} {path}"):
                self.assertIs(served[(method, PREFIX + path)], getattr(routes, name))

    def test_the_hook_registers_the_declared_route(self) -> None:
        self.assertEqual(
            _raw_manifest()["backend"]["hooks"]["routes"],
            "backend.agent_tool_hook:register_agent_tool_routes",
        )
        (route,) = agent_tool_hook.register_agent_tool_routes(None)
        self.assertEqual((route.method, route.path), ("POST", "/" + _MANIFEST.agentTools.route))


class TestTheDeclaredSchema(unittest.TestCase):
    """What the gateway's field engine refuses before the app is called at all."""

    def _validate(self, **kwargs: Any) -> dict[str, Any]:
        return validate_tool_args(kwargs, build_tool_schema(_declared()))

    def test_a_valid_get_and_post_pass(self) -> None:
        self.assertEqual(self._validate(method="GET", path="/state")["path"], "/state")
        cleaned = self._validate(
            method="POST", path="/incident/transition", body_json='{"id": "INV-7"}'
        )
        self.assertEqual(cleaned["body_json"], '{"id": "INV-7"}')

    def test_off_surface_paths_and_full_urls_are_refused(self) -> None:
        for path in (
            "/incident/proposal/decide",
            "/settings",
            "/webhook",
            "/incident",
            "/api/apps/ops-mission-control/state",
        ):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                self._validate(method="POST", path=path)

    def test_an_unknown_method_or_argument_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            self._validate(method="DELETE", path="/ledger")
        with self.assertRaises(ValidationError):
            self._validate(method="GET", path="/state", headers="x")

    def test_each_string_is_bounded(self) -> None:
        with self.assertRaises(ValidationError):
            self._validate(method="GET", path="/incidents", query="x" * 513)
        with self.assertRaises(ValidationError):
            self._validate(method="POST", path="/ledger", body_json="x" * 32_769)


class _Served(unittest.IsolatedAsyncioTestCase):
    """The app's router plus the registry route its hook registers, on an isolated home."""

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.enterContext(mock.patch.dict(os.environ, {"KIROCREW_HOME": self.home}))
        omc_registry.reset_registry()
        self.addCleanup(omc_registry.reset_registry)
        self.enterContext(mock.patch.object(routes, "is_app_enabled", return_value=True))

    async def _app(self) -> web.Application:
        app = web.Application()
        routes.register_routes(app)
        hook_registry = RouteRegistry(app)
        await hook_registry.register_app_routes(
            _MANIFEST.name, APP_ROOT, _raw_manifest()["backend"]["hooks"]["routes"], mock.Mock()
        )
        self.addCleanup(hook_registry.deregister_app_routes, _MANIFEST.name)
        self.assertEqual(hook_registry.get_registered_apps(), [_MANIFEST.name])
        return app

    async def _client(self) -> TestClient:
        client = TestClient(TestServer(await self._app()))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def _post(self, body: object, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        client = await self._client()
        resp = await client.post(URL, json=body, headers=headers or {})
        return resp.status, await resp.json()

    def _recording(self, name: str) -> tuple[Any, list[dict[str, Any]]]:
        seen: list[dict[str, Any]] = []

        async def handler(request: web.Request) -> web.StreamResponse:
            seen.append(
                {
                    "method": request.method,
                    "path": request.path,
                    "query": dict(request.query),
                    "body": await _json_body(request) if request.method == "POST" else None,
                }
            )
            return web.json_response({"ok": True, "served": name})

        return mock.patch.object(routes, name, handler), seen


class TestTheRoute(_Served):
    async def test_a_body_that_is_not_one_call_is_refused(self) -> None:
        bodies: list[Any] = [
            [],
            {"tool": agent_tool.TOOL_NAME},
            {"tool": "x", "arguments": {}, "y": 1},
        ]
        for body in bodies:
            with self.subTest(body=body):
                status, payload = await self._post(body)
                self.assertEqual((status, payload["code"]), (400, "invalid_json"))

    async def test_another_tool_name_is_unknown(self) -> None:
        status, payload = await self._post({"tool": "design_tweak_update_thread", "arguments": {}})
        self.assertEqual((status, payload["code"]), (404, "unknown_tool"))

    async def test_arguments_outside_the_declaration_are_refused(self) -> None:
        for args in ({"method": "GET", "path": "/state", "extra": "x"}, {"method": 1, "path": "/"}):
            with self.subTest(args=args):
                status, payload = await self._post(
                    {"tool": agent_tool.TOOL_NAME, "arguments": args}
                )
                self.assertEqual((status, payload["code"]), (400, "invalid_arguments"))

    async def test_calls_off_the_surface_are_refused_before_any_handler(self) -> None:
        refused = [
            _call(method="POST", path="/state"),
            _call(method="GET", path="/incident/claim"),
            _call(method="POST", path="/incident/proposal/decide"),
            _call(method="POST", path="/settings"),
            _call(method="POST", path="/webhook"),
            _call(method="GET", path="/incident"),
            _call(method="GET", path="/api/apps/ops-mission-control/state"),
            _call(method="POST", path="/agent-tools"),
            _call(method="POST", path="/ledger", query="a=1"),
            _call(method="GET", path="/state", body_json='{"a": 1}'),
            _call(method="POST", path="/ledger", body_json="x" * (agent_tool.MAX_BODY_CHARS + 1)),
            _call(method="GET", path="/incidents", query="x" * (agent_tool.MAX_QUERY_CHARS + 1)),
        ] + [
            _call(method="GET", path="/incidents", query=bad)
            for bad in ("../incident/propose", "a=1?b=2", "x=1#frag", "a=/etc/passwd")
        ]
        served = sorted(set(routes._AGENT_TOOL_HANDLERS.values()))
        for name in served:
            self.enterContext(mock.patch.object(routes, name, mock.AsyncMock()))
        for body in refused:
            with self.subTest(arguments=body["arguments"]):
                status, payload = await self._post(body)
                self.assertEqual((status, payload["code"]), (400, "invalid_arguments"))
        for name in served:
            getattr(routes, name).assert_not_awaited()

    async def test_a_body_that_is_not_a_json_object_is_refused(self) -> None:
        for raw in ("{not json", "[1, 2]", '"text"'):
            with self.subTest(body_json=raw):
                status, payload = await self._post(
                    _call(method="POST", path="/ledger", body_json=raw)
                )
                self.assertEqual((status, payload["code"]), (400, "invalid_arguments"))

    async def test_a_get_reaches_its_handler_with_its_query(self) -> None:
        patch, seen = self._recording("_handle_incidents")
        with patch:
            status, payload = await self._post(
                _call(method="GET", path="/incidents", query="status=investigating&id=INV-42")
            )
        self.assertEqual((status, payload), (200, {"ok": True, "served": "_handle_incidents"}))
        self.assertEqual(seen[0]["method"], "GET")
        self.assertEqual(seen[0]["path"], f"{PREFIX}/incidents")
        self.assertEqual(seen[0]["query"], {"status": "investigating", "id": "INV-42"})

    async def test_a_post_reaches_its_handler_with_the_decoded_body(self) -> None:
        patch, seen = self._recording("_handle_transition")
        with patch:
            status, _ = await self._post(
                _call(
                    method="POST",
                    path="/incident/transition",
                    body_json='{"id": "INV-42", "status": "resolved", "n": 3, "l": ["a"]}',
                )
            )
        self.assertEqual(status, 200)
        self.assertEqual(seen[0]["method"], "POST")
        self.assertEqual(
            seen[0]["body"], {"id": "INV-42", "status": "resolved", "n": 3, "l": ["a"]}
        )

    async def test_a_post_with_no_body_gets_an_empty_object(self) -> None:
        patch, seen = self._recording("_handle_dispatch")
        with patch:
            status, _ = await self._post(_call(method="POST", path="/dispatch"))
        self.assertEqual(status, 200)
        self.assertEqual(seen[0]["body"], {})

    async def test_hidden_characters_and_credentials_are_scrubbed_on_the_way_in(self) -> None:
        """The schema sees ``body_json`` as one string, where ``\\u200b`` is plain ASCII."""
        secret = "AKIA" + "IOSFODNN7EXAMPLE"
        raw = json.dumps({"pat\u200btern": "x", "fix": f"key {secret[:4]}\u200b{secret[4:]}"})
        self.assertNotIn("\u200b", raw)  # escaped: invisible to a string-level sanitizer
        patch, seen = self._recording("_handle_post_ledger")
        with patch:
            status, _ = await self._post(_call(method="POST", path="/ledger", body_json=raw))
        self.assertEqual(status, 200)
        body = seen[0]["body"]
        self.assertIn("pattern", body)
        self.assertNotIn(secret, body["fix"])
        self.assertNotIn("\u200b", json.dumps(body, ensure_ascii=False))

    async def test_a_ledger_post_through_the_tool_writes_no_credential(self) -> None:
        secret = "glpat-" + "IOSFODNN7EXAMPLE1234"
        body = {"pattern": "assume-role denied", "fix": f"aws sts assume-role --key {secret}"}
        status, payload = await self._post(
            _call(method="POST", path="/ledger", body_json=json.dumps(body))
        )
        self.assertEqual(status, 200, payload)
        self.assertNotIn(secret, ledger.ledger_path().read_text(encoding="utf-8"))

    async def test_a_channel_agent_cannot_arm_the_rotation(self) -> None:
        patch, seen = self._recording("_handle_rotation_arm")
        with patch, mock.patch.object(routes, "_audit") as audit:
            status, payload = await self._post(
                _call(method="POST", path="/rotation/arm"),
                headers={agent_tool.SESSION_KEY_HEADER: "channel:slack:C1:1.2"},
            )
        self.assertEqual((status, payload["code"]), (403, "channel_agent_refused"))
        self.assertIn("POST /rotation/arm on ops_mission_control_api", payload["error"])
        self.assertIn("channel agents", payload["error"])
        self.assertEqual(seen, [])
        audit.assert_called_once_with(
            "agent_tool", "POST /rotation/arm", "denied", error="channel agent"
        )

    async def test_a_channel_agent_keeps_the_read_surface(self) -> None:
        """Only the arming operation is held: ``GET /rotation`` reads what it writes."""
        for method, path, name in (
            ("GET", "/rotation", "_handle_rotation"),
            ("GET", "/state", "_handle_state"),
            ("POST", "/ledger", "_handle_post_ledger"),
            ("POST", "/incident/claim", "_handle_claim"),
        ):
            with self.subTest(call=f"{method} {path}"):
                patch, seen = self._recording(name)
                with patch:
                    status, _ = await self._post(
                        _call(method=method, path=path),
                        headers={agent_tool.SESSION_KEY_HEADER: "channel:slack:C1:1.2"},
                    )
                self.assertEqual(status, 200)
                self.assertEqual(len(seen), 1)

    async def test_any_other_session_arms_the_rotation(self) -> None:
        for key in ("cron:ops-mission-control-dispatch", "dashboard:abc", "subagent:x", None):
            with self.subTest(session=key):
                patch, seen = self._recording("_handle_rotation_arm")
                headers = {agent_tool.SESSION_KEY_HEADER: key} if key else {}
                with patch:
                    status, _ = await self._post(
                        _call(method="POST", path="/rotation/arm"), headers=headers
                    )
                self.assertEqual(status, 200)
                self.assertEqual(len(seen), 1)

    async def test_a_disabled_app_answers_its_routes_refusal(self) -> None:
        with mock.patch.object(routes, "is_app_enabled", return_value=False):
            status, payload = await self._post(_call(method="GET", path="/state"))
        self.assertEqual((status, payload["code"]), (403, "app_disabled"))

    async def test_every_allowed_call_answers_as_the_direct_route_does(self) -> None:
        """The real handlers, through :class:`agent_tool.AgentCall` and directly.

        A handler that reads a request attribute the call view lacks would answer 502
        (the registry's boundary) or 500 here, while its direct route answered.
        """
        client = await self._client()
        for method, path in sorted(agent_tool.ALLOWED_CALLS):
            with self.subTest(call=f"{method} {path}"):
                if method == "GET":
                    direct = await client.get(PREFIX + path)
                    args = {"method": method, "path": path}
                else:
                    direct = await client.post(PREFIX + path, json={})
                    args = {"method": method, "path": path, "body_json": "{}"}
                tool = await client.post(
                    URL, json={"tool": agent_tool.TOOL_NAME, "arguments": args}
                )
                self.assertNotIn(tool.status, (500, 502), await tool.text())
                self.assertEqual(tool.status, direct.status, await tool.text())
                self.assertEqual(tool.content_type, direct.content_type)


class TestTheGatewaysOwnPath(_Served):
    """Field engine, ``POST /api/app-tools/call`` and the route registry: the host's code."""

    async def test_a_validated_call_reaches_the_route_through_the_registry(self) -> None:
        from kiro_crew.dashboard.handlers import app_agent_tools as handler_mod

        declared = _declared()
        arguments = validate_tool_args(
            {"method": "GET", "path": "/rotation"}, build_tool_schema(declared)
        )

        @web.middleware
        async def internal(request: web.Request, handler: Any) -> web.StreamResponse:
            request["internal_auth"] = True
            return await handler(request)

        app = web.Application(middlewares=[internal])
        routes.register_routes(app)
        hook_registry = RouteRegistry(app)
        await hook_registry.register_app_routes(
            _MANIFEST.name, APP_ROOT, _raw_manifest()["backend"]["hooks"]["routes"], mock.Mock()
        )
        self.addCleanup(hook_registry.deregister_app_routes, _MANIFEST.name)
        app.router.add_post("/api/app-tools/call", handler_mod.api_app_agent_tool_call)
        self.enterContext(
            mock.patch.object(
                app_agent_tools,
                "declared_tool",
                lambda name: declared if name == declared.name else None,
            )
        )
        self.enterContext(mock.patch.object(handler_mod, "get_app_manifest", lambda _n: _MANIFEST))
        self.enterContext(mock.patch.object(handler_mod, "is_app_enabled", lambda _n: True))
        self.enterContext(
            mock.patch("kiro_crew.apps.hooks_integration.get_route_registry", lambda: hook_registry)
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        ok = await client.post(
            "/api/app-tools/call", json={"tool": declared.name, "arguments": arguments}
        )
        ok_body = await ok.json()
        refused = await client.post(
            "/api/app-tools/call",
            json={"tool": declared.name, "arguments": {"method": "POST", "path": "/rotation/arm"}},
            headers={agent_tool.SESSION_KEY_HEADER: "channel:slack:C1:1.2"},
        )
        refused_body = await refused.json()
        self.assertEqual(ok.status, 200, ok_body)
        direct = await client.get(PREFIX + "/rotation")
        self.assertEqual(ok_body, await direct.json())
        self.assertEqual((refused.status, refused_body["code"]), (403, "channel_agent_refused"))


class TestGatewayAdmission(unittest.TestCase):
    """No route of this app is reachable with the internal secret: the tool is the path in."""

    def test_no_app_route_is_admitted(self) -> None:
        paths = [PREFIX, URL, *(PREFIX + p for _, p in agent_tool.ALLOWED_CALLS)]
        paths += [
            f"{PREFIX}/incident/proposal/decide",
            f"{PREFIX}/providers/aws/secret",
            f"{PREFIX}/settings",
            f"{PREFIX}/webhook",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertFalse(internal_path_matches(path, _MIXED_INTERNAL_API_PATHS))
                self.assertFalse(internal_path_matches(path, _STRICT_INTERNAL_API_PATHS))


if __name__ == "__main__":
    unittest.main()
