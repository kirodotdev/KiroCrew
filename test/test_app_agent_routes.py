"""App-declared agent routes: manifest field, admission, and identity.

An installed app opens one of its own hook routes to agents by naming it in the
manifest's ``agentRoutes``. Three pieces carry that promise, and each is pinned
here against the way it could quietly widen:

- the manifest refuses every malformed entry and signs the list;
- ``token_auth`` arms a local internal-secret caller only when aiohttp selected
  the registry catch-all and no static internal set already admits the path,
  leaving every other request alone;
- ``RouteRegistry.dispatch`` decides admission once, from the route it resolved:
  it demands the caller's session, refuses an undeclared route before any handler
  runs, and publishes the session to the handler only on that arm, never for a
  cookie request.
"""

from __future__ import annotations

import ast
import json
import threading
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from app_agent_routes_helpers import (  # noqa: F401 - autouse fixture registered by import
    _isolated_token_state,
    _manifest,
)

from kiro_crew.apps.manifest import (
    AGENT_ROUTE_METHODS,
    CORE_APP_ROUTE_SEGMENTS,
    MAX_AGENT_ROUTE_ENTRY_LENGTH,
    MAX_AGENT_ROUTES_PER_APP,
    AppManifest,
    agent_route_matches,
    parse_agent_route,
)
from kiro_crew.apps.route_registry import RouteRegistry, _RegisteredRoute
from kiro_crew.constants import APP_REQUEST_HEADER, APP_REQUEST_HEADER_VALUE
from kiro_crew.dashboard.token_auth import (
    bind_token_ip,
    generate_token,
    mark_consumed,
    token_auth_middleware,
)

SECRET = "agent-route-secret"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_valid_agent_routes_load_and_round_trip() -> None:
    routes = ["GET /subscriptions", "DELETE /subscriptions/{id}", "POST /a.b/c_d-e~f"]
    manifest = AppManifest.from_dict(_manifest(agentRoutes=routes))
    assert manifest.validate() == []
    assert manifest.agentRoutes == routes
    assert manifest.to_dict()["agentRoutes"] == routes
    assert AppManifest.from_dict(manifest.to_dict()).agentRoutes == routes


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        ("GET /x", "must be a list"),
        ([7], "must be a string"),
        (["get /x"], "method must be one of"),
        (["HEAD /x"], "method must be one of"),
        (["GET"], "method must be one of"),
        (["GET x"], "must start with '/'"),
        (["GET  /x"], "must start with '/'"),
        (["GET /x?y=1"], "query or fragment"),
        (["GET /x#y"], "query or fragment"),
        (["GET /a/../b"], "'..' segment"),
        (["GET /a/./b"], "'..' segment"),
        (["GET /a//b"], "'..' segment"),
        (["GET /a/"], "'..' segment"),
        (["GET /"], "'..' segment"),
        (["GET /a%2Fb"], "is not a literal or a {param}"),
        (["GET /{1bad}"], "is not a literal or a {param}"),
        (["GET /x{id}"], "is not a literal or a {param}"),
        (["GET /x", "GET /x"], "is duplicated"),
    ],
)
def test_each_invalid_agent_route_shape_is_rejected(value: Any, fragment: str) -> None:
    errors = AppManifest.from_dict(_manifest(agentRoutes=value)).validate()
    assert any(fragment in e for e in errors), errors


def _parsed_ok(entry: str) -> bool:
    parsed, reason = parse_agent_route(entry)
    return parsed == tuple(entry.split(" ", 1)) and reason == ""


@pytest.mark.parametrize("segment", sorted(CORE_APP_ROUTE_SEGMENTS))
@pytest.mark.parametrize("method", sorted(AGENT_ROUTE_METHODS))
def test_core_reserved_first_segment_is_refused(method: str, segment: str) -> None:
    """Core mounts these under ``/api/apps/<app>/`` ahead of the app catch-all."""
    parsed, reason = parse_agent_route(f"{method} /{segment}")
    assert parsed is None
    assert "reserved by core" in reason
    nested, _ = parse_agent_route(f"{method} /{segment}/preview")
    assert nested is None
    errors = AppManifest.from_dict(_manifest(agentRoutes=[f"{method} /{segment}"])).validate()
    assert any("reserved by core" in e for e in errors), errors
    # The same name BELOW the first segment is the app's own route.
    assert _parsed_ok(f"{method} /mine/{segment}")


def test_param_first_segment_is_refused() -> None:
    """A ``{param}`` in first position would match every core-reserved name."""
    parsed, reason = parse_agent_route("GET /{id}")
    assert parsed is None
    assert "first path segment must be a literal" in reason
    assert _parsed_ok("GET /items/{id}")


@pytest.mark.asyncio
async def test_dispatch_refuses_a_core_reserved_path_even_when_the_app_registers_it() -> None:
    """An app's own routes module may register ``/dev``; the parser keeps no such
    declaration, so an armed call to it is refused before the handler runs."""
    from kiro_crew.apps.route_registry import _compile_pattern

    registry = RouteRegistry(MagicMock())
    seen: list[web.Request] = []

    async def handler(request: web.Request, _ctx: Any) -> web.Response:
        seen.append(request)
        return web.Response(text="ok")

    pattern = _RegisteredRoute(method="POST", path="/{any}", handler=handler, has_params=True)
    pattern.compiled, pattern.param_names = _compile_pattern(pattern.path)
    registry._routes["slack-poller"] = [
        _RegisteredRoute(method="POST", path="/dev", handler=handler, has_params=False),
        pattern,
    ]
    registry._contexts["slack-poller"] = MagicMock()
    retained = registry._parse_agent_routes("slack-poller", ["POST /mine", "POST /dev"])
    assert retained == []
    registry._agent_routes["slack-poller"] = retained
    for sub_path in ("dev", "enable"):
        req = make_mocked_request(
            "POST",
            f"/api/apps/slack-poller/{sub_path}",
            headers={"X-Session-Key": "cron:job-7"},
            match_info={"app_name": "slack-poller", "path": sub_path},
        )
        req["internal_auth"] = True
        req["app_agent_route"] = True
        resp = await registry.dispatch(req)
        assert resp.status == 403
    assert seen == []


def test_agent_routes_are_capped() -> None:
    routes = [f"GET /r{i}" for i in range(MAX_AGENT_ROUTES_PER_APP + 1)]
    errors = AppManifest.from_dict(_manifest(agentRoutes=routes)).validate()
    assert any("at most" in e for e in errors), errors
    at_cap = AppManifest.from_dict(_manifest(agentRoutes=routes[:-1]))
    assert at_cap.validate() == []


def test_agent_route_entries_are_length_bounded() -> None:
    prefix = "GET /"
    at_cap = prefix + "x" * (MAX_AGENT_ROUTE_ENTRY_LENGTH - len(prefix))
    over_cap = at_cap + "x"
    assert AppManifest.from_dict(_manifest(agentRoutes=[at_cap])).validate() == []
    errors = AppManifest.from_dict(_manifest(agentRoutes=[over_cap])).validate()
    assert any("at most" in error and "characters" in error for error in errors), errors


def test_absent_agent_routes_leave_the_signed_body_unchanged() -> None:
    without = AppManifest.from_dict(_manifest())
    empty = AppManifest.from_dict(_manifest(agentRoutes=[]))
    assert b"agentRoutes" not in without.signing_payload()
    assert empty.signing_payload() == without.signing_payload()
    assert "agentRoutes" not in without.to_dict()


def test_declared_agent_routes_are_signed() -> None:
    one = AppManifest.from_dict(_manifest(agentRoutes=["GET /a"]))
    two = AppManifest.from_dict(_manifest(agentRoutes=["GET /a", "DELETE /a"]))
    assert b'"agentRoutes":["GET /a"]' in one.signing_payload()
    assert one.signing_payload() != two.signing_payload()


@pytest.mark.parametrize("value", [True, 5])
def test_non_list_agent_routes_are_signed_then_rejected_by_validation(value: Any) -> None:
    import json

    manifest = AppManifest.from_dict(_manifest(agentRoutes=value))
    payload = json.loads(manifest.signing_payload())
    assert payload["agentRoutes"] == value
    assert any("agentRoutes must be a list" in error for error in manifest.validate())


@pytest.mark.parametrize("value", [True, 5])
def test_non_list_agent_routes_survive_to_dict(value: Any) -> None:
    """An unvalidated self-registration can persist a non-list value; reading the
    manifest back must keep it as written instead of raising TypeError."""
    manifest = AppManifest.from_dict(_manifest(agentRoutes=value))
    assert manifest.to_dict()["agentRoutes"] == value


def test_list_agent_routes_round_trip_through_to_dict() -> None:
    routes = ["GET /a", "DELETE /a"]
    manifest = AppManifest.from_dict(_manifest(agentRoutes=routes))
    assert manifest.to_dict()["agentRoutes"] == routes


def test_list_agent_routes_keep_the_existing_signed_payload() -> None:
    import json

    routes = ["GET /a", "DELETE /a"]
    without_routes = AppManifest.from_dict(_manifest())
    expected_body = json.loads(without_routes.signing_payload())
    expected_body["agentRoutes"] = routes
    expected_payload = json.dumps(expected_body, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    manifest = AppManifest.from_dict(_manifest(agentRoutes=routes))
    assert manifest.signing_payload() == expected_payload


def test_signature_check_cleanly_denies_non_list_agent_routes() -> None:
    from kiro_crew.apps.admission import AppAdmissionPolicy, _signature_valid

    policy = AppAdmissionPolicy(
        mode="enforce", require_signature=True, trust_keys={"acme": "s3cr3t"}
    )
    manifest = AppManifest.from_dict(
        _manifest(agentRoutes=True, signer="acme", signature="invalid")
    )
    assert _signature_valid(manifest, policy) is False


@pytest.mark.parametrize(
    ("declared", "actual", "expected"),
    [
        ("/subscriptions", "/subscriptions", True),
        ("/subscriptions/{id}", "/subscriptions/42", True),
        ("/subscriptions/{id}", "/subscriptions", False),
        ("/subscriptions/{id}", "/subscriptions/42/x", False),
        ("/subscriptions/{id}", "/subscriptions/..", False),
        ("/subscriptions/{id}", "/subscriptions/", False),
        ("/subscriptions", "/subscriptionsx", False),
    ],
)
def test_agent_route_matching(declared: str, actual: str, expected: bool) -> None:
    assert agent_route_matches(declared, actual) is expected


# ---------------------------------------------------------------------------
# Registry: retention and the arm
# ---------------------------------------------------------------------------


def _registry(*, declares: bool = True) -> RouteRegistry:
    registry = RouteRegistry(MagicMock())
    registry._routes["slack-poller"] = [
        _RegisteredRoute(method="GET", path="/subscriptions", handler=AsyncMock(), has_params=False)
    ]
    registry._contexts["slack-poller"] = MagicMock()
    registry._agent_routes["slack-poller"] = [("GET", "/subscriptions")] if declares else []
    return registry


async def _ok_route(_request: web.Request, _ctx: Any) -> web.Response:
    return web.Response(text="ok")


async def _register(
    registry: RouteRegistry,
    monkeypatch: pytest.MonkeyPatch,
    agent_routes: list[Any],
    *,
    app_name: str = "slack-poller",
    routes: list[Any] | None = None,
) -> list[str]:
    from kiro_crew.apps import route_registry as rr

    if routes is None:
        routes = [
            rr.AppRoute("GET", "/subscriptions", _ok_route),
            rr.AppRoute("DELETE", "/subscriptions/{id}", _ok_route),
        ]
    monkeypatch.setattr(rr, "load_app_module", lambda *_a: lambda _ctx: routes)
    return await registry.register_app_routes(
        app_name, MagicMock(), "backend.routes:register", MagicMock(), agent_routes=agent_routes
    )


@pytest.mark.asyncio
async def test_register_app_routes_retains_every_declaration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = RouteRegistry(MagicMock())
    await _register(registry, monkeypatch, ["GET /subscriptions", "DELETE /subscriptions/{id}"])
    assert registry._agent_routes["slack-poller"] == [
        ("GET", "/subscriptions"),
        ("DELETE", "/subscriptions/{id}"),
    ]
    registry.deregister_app_routes("slack-poller")
    assert "slack-poller" not in registry._agent_routes


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["get /bad", 7, "GET /a/../b", "GET /dev"])
async def test_one_malformed_declaration_refuses_the_whole_list(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, bad: Any
) -> None:
    """All or none: a partial list would hide which routes went missing."""
    from kiro_crew.apps import route_registry as rr

    registry = RouteRegistry(MagicMock())
    with caplog.at_level("WARNING", logger=rr.__name__):
        descriptions = await _register(registry, monkeypatch, ["GET /subscriptions", bad])
    assert len(descriptions) == 2, "the app's routes still register"
    assert "slack-poller" not in registry._agent_routes
    refused = [
        r.getMessage()
        for r in caplog.records
        if "refusing all of its agent routes" in r.getMessage()
    ]
    assert len(refused) == 1 and repr(bad) in refused[0]


@pytest.mark.asyncio
async def test_an_over_cap_list_is_refused_whole_and_said_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The cap is an install-time error; here it only names why nothing was kept,
    so a later refused call is traceable to the cap rather than read as undeclared."""
    from kiro_crew.apps import route_registry as rr

    at_cap = [f"GET /r{index}" for index in range(MAX_AGENT_ROUTES_PER_APP)]
    registry = RouteRegistry(MagicMock())
    with caplog.at_level("WARNING", logger=rr.__name__):
        await _register(registry, monkeypatch, at_cap)
    assert len(registry._agent_routes["slack-poller"]) == MAX_AGENT_ROUTES_PER_APP
    assert not [r for r in caplog.records if "agent routes" in r.getMessage()]

    registry = RouteRegistry(MagicMock())
    with caplog.at_level("WARNING", logger=rr.__name__):
        await _register(registry, monkeypatch, at_cap + ["GET /one-more"])
    assert "slack-poller" not in registry._agent_routes
    (refused,) = [r.getMessage() for r in caplog.records if "refusing all" in r.getMessage()]
    assert f"more than the {MAX_AGENT_ROUTES_PER_APP} allowed" in refused


def _failing_loader(kind: str) -> Any:
    def _import_error(*_args: Any) -> Any:
        raise ImportError("broken hook")

    def _register_raises(*_args: Any) -> Any:
        def _fn(_ctx: Any) -> Any:
            raise RuntimeError("register blew up")

        return _fn

    def _non_list(*_args: Any) -> Any:
        return lambda _ctx: "not a list"

    return {"import": _import_error, "raises": _register_raises, "non_list": _non_list}[kind]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["import", "raises", "non_list"])
async def test_declarations_install_in_the_same_generation_as_the_routes(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """A failed load leaves NO declarations, including a previous generation's."""
    from kiro_crew.apps import route_registry as rr

    registry = RouteRegistry(MagicMock())
    registry._agent_routes["dev-fleet"] = [("POST", "/stale")]
    monkeypatch.setattr(rr, "load_app_module", _failing_loader(kind))
    descriptions = await registry.register_app_routes(
        "dev-fleet",
        MagicMock(),
        "backend.routes:register",
        MagicMock(),
        agent_routes=["POST /pod/down"],
    )
    assert descriptions == []
    assert "dev-fleet" not in registry._agent_routes


def test_the_arm_is_the_catch_all_identity_and_nothing_else() -> None:
    registry = RouteRegistry(web.Application())
    assert not registry.agent_route_arm(None)
    assert not registry.agent_route_arm(object())
    registry.ensure_catch_all()
    assert registry.agent_route_arm(registry._catch_all_route)
    assert not registry.agent_route_arm(object())
    assert not registry.agent_route_arm(None)


@pytest.mark.asyncio
async def test_a_shadowing_host_route_is_never_armed_and_never_lent_admission() -> None:
    """A declaration cannot lend admission to a host handler: aiohttp selects the
    host route, which is not the catch-all, so the arm stays closed and the
    secret-bearing call gets the ordinary cookie refusal."""
    path = "/api/apps/dev-fleet/pod/down"
    app = web.Application()
    host_handler = AsyncMock(return_value=web.Response(text="host route ran"))
    app.router.add_post(path, host_handler)

    registry = RouteRegistry(app)
    registry._routes["dev-fleet"] = [
        _RegisteredRoute(method="POST", path="/pod/down", handler=AsyncMock(), has_params=False)
    ]
    registry._contexts["dev-fleet"] = MagicMock()
    registry._agent_routes["dev-fleet"] = [("POST", "/pod/down")]
    registry.ensure_catch_all()
    app.middlewares.append(
        token_auth_middleware(internal_secret=SECRET, agent_route_arm=registry.agent_route_arm)
    )

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(path, headers={"X-Internal-Secret": SECRET})
        assert resp.status == 403
    host_handler.assert_not_awaited()


def _arm_hooks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> tuple[Any, dict[str, Any], RouteRegistry]:
    """Point the hooks system at one routes-only app declaring an agent route."""
    from types import SimpleNamespace

    import kiro_crew.apps.hooks_integration as hooks_mod

    class _StubDispatcher(SimpleNamespace):
        async def cache_shutdown_for(self, app_info: Any) -> None:
            return None

        async def dispatch_disable(self, app_info: Any) -> bool:
            return True

    app_dir = tmp_path / "apps" / "slack-poller"
    (app_dir / "backend").mkdir(parents=True)
    (app_dir / "backend" / "routes.py").write_text(
        "from kiro_crew.apps.route_registry import AppRoute\n"
        "async def _list(request, ctx):\n    return None\n"
        "def register(ctx):\n    return [AppRoute('GET', '/subscriptions', _list)]\n",
        encoding="utf-8",
    )
    info = {
        "name": "slack-poller",
        "enabled": True,
        "manifest": {
            **_manifest(agentRoutes=["GET /subscriptions"]),
            "backend": {"hooks": {"routes": "backend.routes:register"}},
        },
    }
    registry = RouteRegistry(web.Application())
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(hooks_mod, "_lifecycle_dispatcher", _StubDispatcher())
    monkeypatch.setattr(hooks_mod, "_route_registry", registry)
    monkeypatch.setattr(hooks_mod, "list_apps", lambda: [info])
    monkeypatch.setattr(hooks_mod, "_app_hook_root", lambda _name: app_dir)
    monkeypatch.setattr(hooks_mod, "app_execution_denied", lambda *a, **kw: "")
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)
    monkeypatch.setattr(hooks_mod, "_loaded_hook_signatures", {})
    monkeypatch.setattr(hooks_mod, "_loaded_hook_manifests", {})
    return hooks_mod, info, registry


@pytest.mark.asyncio
async def test_gateway_startup_hands_the_manifest_agent_routes_to_the_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    hooks_mod, _info, registry = _arm_hooks(monkeypatch, tmp_path)
    await hooks_mod.on_gateway_startup()
    assert registry._agent_routes["slack-poller"] == [("GET", "/subscriptions")]
    assert hooks_mod.agent_route_arm(registry._catch_all_route)
    assert not hooks_mod.agent_route_arm(object())


@pytest.mark.asyncio
async def test_app_enable_hands_the_manifest_agent_routes_to_the_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    hooks_mod, info, registry = _arm_hooks(monkeypatch, tmp_path)
    await hooks_mod.on_app_enable("slack-poller", info)
    assert registry._agent_routes["slack-poller"] == [("GET", "/subscriptions")]
    assert hooks_mod.agent_route_arm(registry._catch_all_route)


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["enable", "startup"])
async def test_lifecycle_resolves_agent_routes_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, entrypoint: str
) -> None:
    hooks_mod, info, _registry = _arm_hooks(monkeypatch, tmp_path)
    event_loop_thread = threading.current_thread()
    caller_threads: list[threading.Thread] = []
    original = hooks_mod._declared_agent_routes

    def record_caller_thread(app_name: str, manifest: Any) -> list[object]:
        caller_threads.append(threading.current_thread())
        return original(app_name, manifest)

    monkeypatch.setattr(hooks_mod, "_declared_agent_routes", record_caller_thread)
    if entrypoint == "enable":
        await hooks_mod.on_app_enable("slack-poller", info)
    else:
        await hooks_mod.on_gateway_startup()

    assert caller_threads
    assert event_loop_thread not in caller_threads


def _ship_builtin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, shipped_manifest: str | None
) -> tuple[Any, dict[str, Any], RouteRegistry]:
    """Make ``slack-poller`` a shipped builtin whose package manifest is *shipped_manifest*.

    The persisted copy (``info["manifest"]``) still declares ``GET /subscriptions``,
    as an agent shell editing ``apps/slack-poller/app.json`` would leave it; the
    hook code loads from the shipped root, as it does for a real builtin.
    """
    hooks_mod, info, registry = _arm_hooks(monkeypatch, tmp_path)
    shipped = tmp_path / "shipped" / "slack_poller"
    (shipped / "backend").mkdir(parents=True)
    (shipped / "backend" / "routes.py").write_text(
        (tmp_path / "apps" / "slack-poller" / "backend" / "routes.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    if shipped_manifest is not None:
        (shipped / "app.json").write_text(shipped_manifest, encoding="utf-8")
    monkeypatch.setattr(hooks_mod, "shipped_builtin_app_root", lambda _name: shipped)
    monkeypatch.setattr(hooks_mod, "_app_hook_root", lambda _name: shipped)
    return hooks_mod, info, registry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shipped_manifest",
    [
        pytest.param(json.dumps(_manifest()), id="shipped-declares-none"),
        pytest.param(None, id="shipped-missing"),
        pytest.param("{not json", id="shipped-invalid"),
        pytest.param(json.dumps(["GET /subscriptions"]), id="shipped-not-an-object"),
    ],
)
async def test_a_persisted_copy_cannot_add_agent_routes_to_a_builtin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, shipped_manifest: str | None
) -> None:
    hooks_mod, info, registry = _ship_builtin(monkeypatch, tmp_path, shipped_manifest)
    await hooks_mod.on_app_enable("slack-poller", info)
    assert registry._agent_routes.get("slack-poller") is None
    assert registry._routes["slack-poller"]  # the builtin's routes still load
    assert hooks_mod._declared_agent_routes("slack-poller", info["manifest"]) == []


def test_agent_routes_marker_retains_valid_routes_unchanged() -> None:
    import kiro_crew.apps.hooks_integration as hooks_mod

    routes = ["POST /z/{id}", "GET /a"]
    assert hooks_mod._agent_routes_marker("slack-poller", {"agentRoutes": routes}) == (
        "GET /a",
        "POST /z/{id}",
    )


@pytest.mark.parametrize(
    "routes",
    [
        pytest.param(
            [f"GET /r{index}" for index in range(MAX_AGENT_ROUTES_PER_APP + 1)],
            id="over-cap",
        ),
        pytest.param(
            ["GET /" + "x" * MAX_AGENT_ROUTE_ENTRY_LENGTH],
            id="over-length",
        ),
        pytest.param(["GET /a/../b"], id="malformed"),
    ],
)
def test_agent_routes_marker_bounds_invalid_declarations(routes: list[str]) -> None:
    import kiro_crew.apps.hooks_integration as hooks_mod

    assert hooks_mod._agent_routes_marker("slack-poller", {"agentRoutes": routes}) == ("<invalid>",)


def test_a_persisted_agent_route_edit_leaves_a_builtin_hook_signature_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    hooks_mod, info, _registry = _ship_builtin(monkeypatch, tmp_path, json.dumps(_manifest()))
    untouched = {
        **info,
        "manifest": {k: v for k, v in info["manifest"].items() if k != "agentRoutes"},
    }
    assert hooks_mod.hook_signature(info) == hooks_mod.hook_signature(untouched)


@pytest.mark.asyncio
async def test_a_builtin_takes_its_agent_routes_from_the_shipped_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    shipped = json.dumps(_manifest(agentRoutes=["GET /subscriptions"]))
    hooks_mod, info, registry = _ship_builtin(monkeypatch, tmp_path, shipped)
    info["manifest"].pop("agentRoutes")
    await hooks_mod.on_app_enable("slack-poller", info)
    assert registry._agent_routes["slack-poller"] == [("GET", "/subscriptions")]


def test_nothing_arms_before_the_hooks_system_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    import kiro_crew.apps.hooks_integration as hooks_mod

    monkeypatch.setattr(hooks_mod, "_route_registry", None)
    assert not hooks_mod.agent_route_arm(object())
    assert not hooks_mod.agent_route_arm(None)


def test_both_server_entrypoints_wire_the_agent_route_arm() -> None:
    """A dropped kwarg would disable the arm with every other test green."""
    server_path = (
        Path(__file__).resolve().parents[1]
        / "src/kiro_crew/dashboard/server_runtime/middleware_chain.py"
    )
    module = ast.parse(server_path.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "token_auth_middleware"
    ]
    assert len(calls) == 2
    for call in calls:
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        assert ast.dump(keywords["agent_route_arm"]) == ast.dump(
            ast.Name(id="agent_route_arm", ctx=ast.Load())
        )


# ---------------------------------------------------------------------------
# token_auth
# ---------------------------------------------------------------------------


def _request(
    path: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    remote: str = "127.0.0.1",
) -> MagicMock:
    req = MagicMock(spec=web.Request)
    req.path = path
    req.method = method
    req.query = {}
    req.cookies = cookies or {}
    req.remote = remote
    req.headers = headers or {}
    store: dict[str, Any] = {}
    req.__setitem__.side_effect = store.__setitem__
    req.__getitem__.side_effect = store.__getitem__
    req.get.side_effect = store.get
    req.store = store
    return req


async def _ok(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def _middleware(*, arm: bool = True, **extra: Any) -> Any:
    """The gate with its arm answered for it; dispatch's own checks are below."""
    return token_auth_middleware(
        internal_secret=SECRET, agent_route_arm=lambda _resolved: arm, **extra
    )


_DECLARED = "/api/apps/slack-poller/subscriptions"


@pytest.mark.asyncio
async def test_internal_secret_call_on_the_catch_all_is_armed() -> None:
    req = _request(_DECLARED, headers={"X-Internal-Secret": SECRET})
    resp = await _middleware()(req, _ok)
    assert resp.status == 200
    assert req.store.get("internal_auth") is True
    assert req.store.get("app_agent_route") is True


@pytest.mark.asyncio
async def test_the_gate_hands_the_arm_the_route_aiohttp_resolved() -> None:
    """The arm is an identity check on the resolved route, nothing path-based."""
    registry = RouteRegistry(web.Application())
    registry.ensure_catch_all()
    req = _request(_DECLARED, headers={"X-Internal-Secret": SECRET})
    req.match_info.route = registry._catch_all_route
    resp = await token_auth_middleware(
        internal_secret=SECRET, agent_route_arm=registry.agent_route_arm
    )(req, _ok)
    assert resp.status == 200 and req.store.get("app_agent_route") is True

    req = _request(_DECLARED, headers={"X-Internal-Secret": SECRET})
    req.match_info.route = object()
    resp = await token_auth_middleware(
        internal_secret=SECRET, agent_route_arm=registry.agent_route_arm
    )(req, _ok)
    assert resp.status == 403 and "app_agent_route" not in req.store


@pytest.mark.asyncio
async def test_a_failing_arm_predicate_arms_nothing() -> None:
    def _boom(_resolved: object) -> bool:
        raise RuntimeError("registry unavailable")

    req = _request(_DECLARED, headers={"X-Internal-Secret": SECRET})
    resp = await token_auth_middleware(internal_secret=SECRET, agent_route_arm=_boom)(req, _ok)
    assert resp.status == 403
    assert "app_agent_route" not in req.store


class _RecordingSel:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_api_access(self, **row: Any) -> None:
        self.rows.append(row)


def _owned_by(monkeypatch: pytest.MonkeyPatch, app: str) -> _RecordingSel:
    """Make every internal caller resolve to *app* (``""`` = the person)."""
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr(_ta, "_derive_internal_caller_app", lambda _request: app)
    monkeypatch.setattr(_ta, "_internal_caller_record_missing", lambda _request: False)
    monkeypatch.setattr(_ta, "_app_api_allowlist", lambda _name: ())
    recording = _RecordingSel()
    monkeypatch.setattr(_ta, "_sel_fn", lambda: recording)
    return recording


def _grant_rows(recording: _RecordingSel) -> list[dict[str, Any]]:
    return [
        row
        for row in recording.rows
        if row.get("operation") == "app_agent_route" and row.get("outcome") == "granted"
    ]


_AGENT_HEADERS = {"X-Internal-Secret": SECRET, "X-Session-Key": "cron:job-7"}


@pytest.mark.asyncio
async def test_an_agent_route_refuses_a_caller_naming_a_removed_dashboard_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = _owned_by(monkeypatch, "")
    handler = AsyncMock(return_value=web.Response(text="ok"))
    state = MagicMock()
    state._slots = {}
    req = _request(
        _DECLARED,
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:closed-tab"},
    )
    req.app = {"state": state}

    resp = await _middleware()(req, handler)

    assert resp.status == 403
    assert '"code": "caller_unattributable"' in resp.text
    handler.assert_not_awaited()
    assert _grant_rows(recording) == []


@pytest.mark.asyncio
async def test_an_agent_route_admits_a_caller_naming_a_live_dashboard_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _owned_by(monkeypatch, "")
    handler = AsyncMock(return_value=web.Response(text="ok"))
    state = MagicMock()
    state._slots = {"live-tab": MagicMock()}
    req = _request(
        _DECLARED,
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:live-tab"},
    )
    req.app = {"state": state}

    resp = await _middleware()(req, handler)

    assert resp.status == 200
    handler.assert_awaited_once_with(req)


@pytest.mark.asyncio
async def test_a_static_internal_path_does_not_refuse_a_removed_dashboard_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = _owned_by(monkeypatch, "")
    static = "/api/apps/issue-radar/investigation"
    handler = AsyncMock(return_value=web.Response(text="ok"))
    state = MagicMock()
    state._slots = {}
    middleware = token_auth_middleware(
        mixed_internal_paths=frozenset({static}), internal_secret=SECRET
    )
    req = _request(
        static,
        method="PUT",
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:closed-tab"},
    )
    req.app = {"state": state}

    resp = await middleware(req, handler)

    assert resp.status == 200
    handler.assert_awaited_once_with(req)
    assert not [row for row in recording.rows if row.get("operation") == "app_agent_route"]


@pytest.mark.asyncio
async def test_a_session_owned_by_another_app_cannot_call_a_declared_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``app_request`` checks only the TARGET app's declaration, so the arm must
    confine an app-owned caller to its own namespace as the cookie arm does."""
    recording = _owned_by(monkeypatch, "other-app")
    req = _request(_DECLARED, headers=_AGENT_HEADERS)
    resp = await _middleware()(req, _ok)
    assert resp.status == 403
    assert req.store.get("app") == "other-app"
    # The grant row is written only after the scope check passes.
    assert _grant_rows(recording) == []


@pytest.mark.asyncio
async def test_an_app_allowlist_is_primed_before_agent_route_scope_is_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kiro_crew.dashboard.token_auth as _ta

    _owned_by(monkeypatch, "other-app")
    calls: list[tuple[str, str]] = []
    prime = AsyncMock(side_effect=lambda app: calls.append(("prime", app)))

    def enforce_scope(_request: web.Request, app: str, _path: str) -> web.Response:
        calls.append(("scope", app))
        return web.Response(status=403)

    monkeypatch.setattr(_ta, "_prime_app_api_allowlist", prime)
    monkeypatch.setattr(_ta, "_enforce_app_scope", enforce_scope)
    req = _request(_DECLARED, headers=_AGENT_HEADERS)

    resp = await _middleware()(req, _ok)

    assert resp.status == 403
    prime.assert_awaited_once_with("other-app")
    assert calls == [("prime", "other-app"), ("scope", "other-app")]


@pytest.mark.asyncio
async def test_a_session_owned_by_the_declaring_app_is_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _owned_by(monkeypatch, "slack-poller")
    req = _request(_DECLARED, headers=_AGENT_HEADERS)
    resp = await _middleware()(req, _ok)
    assert resp.status == 200
    assert req.store.get("app") == "slack-poller"
    assert req.store.get("app_agent_route") is True


@pytest.mark.asyncio
async def test_an_unconfined_session_is_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    _owned_by(monkeypatch, "")
    req = _request(_DECLARED, headers=_AGENT_HEADERS)
    resp = await _middleware()(req, _ok)
    assert resp.status == 200
    assert "app" not in req.store
    assert req.store.get("app_agent_route") is True


@pytest.mark.asyncio
async def test_the_grant_row_names_the_calling_session(monkeypatch: pytest.MonkeyPatch) -> None:
    recording = _owned_by(monkeypatch, "")
    req = _request(_DECLARED, headers=_AGENT_HEADERS)
    resp = await _middleware()(req, _ok)
    assert resp.status == 200
    (granted,) = _grant_rows(recording)
    assert granted["caller"] == "cron:job-7"
    assert granted["resources"] == f"GET {_DECLARED}"
    assert granted["source"] == "token_auth"


@pytest.mark.asyncio
async def test_no_grant_row_is_emitted_off_the_agent_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    recording = _owned_by(monkeypatch, "")
    req = _request(_DECLARED, headers=_AGENT_HEADERS)
    resp = await _middleware(arm=False)(req, _ok)
    assert resp.status == 403
    assert not [row for row in recording.rows if row.get("operation") == "app_agent_route"]


@pytest.mark.asyncio
async def test_no_grant_row_is_emitted_for_a_caller_whose_record_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kiro_crew.dashboard.token_auth as _ta

    recording = _owned_by(monkeypatch, "")
    monkeypatch.setattr(_ta, "_internal_caller_record_missing", lambda _request: True)
    req = _request(_DECLARED, headers=_AGENT_HEADERS)
    resp = await _middleware()(req, _ok)
    assert resp.status == 403
    assert _grant_rows(recording) == []


@pytest.mark.asyncio
async def test_a_static_internal_path_under_api_apps_never_takes_the_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path the static sets already admit keeps its branch byte-for-byte: the
    arm is not even consulted, so dispatch sees no agent mark to check."""
    recording = _owned_by(monkeypatch, "")
    static = "/api/apps/issue-radar/investigation"
    asked: list[object] = []

    def _arm(resolved: object) -> bool:
        asked.append(resolved)
        return True

    middleware = token_auth_middleware(
        mixed_internal_paths=frozenset({static}), internal_secret=SECRET, agent_route_arm=_arm
    )
    req = _request(static, method="PUT", headers=_AGENT_HEADERS)
    resp = await middleware(req, _ok)
    assert resp.status == 200
    assert req.store.get("internal_auth") is True
    assert "app_agent_route" not in req.store
    assert asked == []
    assert not [row for row in recording.rows if row.get("operation") == "app_agent_route"]


@pytest.mark.asyncio
async def test_app_request_marker_refuses_a_static_internal_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kiro_crew.dashboard.token_auth as _ta

    static = "/api/apps/issue-radar/investigation"
    handler = AsyncMock(return_value=web.Response(text="ok"))
    auth_log = MagicMock()
    monkeypatch.setattr(_ta, "_log_auth", auth_log)
    middleware = token_auth_middleware(
        mixed_internal_paths=frozenset({static}), internal_secret=SECRET
    )
    marked = {
        **_AGENT_HEADERS,
        APP_REQUEST_HEADER: APP_REQUEST_HEADER_VALUE,
    }
    request = _request(static, method="PUT", headers=marked)

    resp = await middleware(request, handler)

    assert resp.status == 403
    assert '"code": "app_request_static_route_refused"' in resp.text
    handler.assert_not_awaited()
    auth_log.assert_called_once_with(
        request, "internal", "denied", "app_request_static_route_refused"
    )


@pytest.mark.asyncio
async def test_app_request_marker_refuses_an_undeclared_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kiro_crew.dashboard.token_auth as _ta

    handler = AsyncMock(return_value=web.Response(text="ok"))
    auth_log = MagicMock()
    monkeypatch.setattr(_ta, "_log_auth", auth_log)
    request = _request(
        _DECLARED,
        headers={APP_REQUEST_HEADER: APP_REQUEST_HEADER_VALUE},
    )

    resp = await _middleware()(request, handler)

    assert resp.status == 403
    assert '"code": "app_request_route_refused"' in resp.text
    handler.assert_not_awaited()
    auth_log.assert_called_once_with(request, "internal", "denied", "app_request_route_refused")


@pytest.mark.asyncio
async def test_unmarked_dedicated_tool_still_admits_a_static_internal_path() -> None:
    static = "/api/apps/issue-radar/investigation"
    handler = AsyncMock(return_value=web.Response(text="ok"))
    middleware = token_auth_middleware(
        mixed_internal_paths=frozenset({static}), internal_secret=SECRET
    )

    resp = await middleware(_request(static, method="PUT", headers=_AGENT_HEADERS), handler)

    assert resp.status == 200
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_internal_secret_call_the_arm_declines_is_refused() -> None:
    req = _request("/api/status", headers={"X-Internal-Secret": SECRET})
    resp = await _middleware(arm=False)(req, _ok)
    assert resp.status == 403
    assert "app_agent_route" not in req.store
    assert "internal_auth" not in req.store


@pytest.mark.asyncio
async def test_a_non_local_internal_secret_call_is_not_armed() -> None:
    asked: list[object] = []

    def _arm(resolved: object) -> bool:
        asked.append(resolved)
        return True

    req = _request(_DECLARED, headers={"X-Internal-Secret": SECRET}, remote="10.0.0.5")
    resp = await token_auth_middleware(internal_secret=SECRET, agent_route_arm=_arm)(req, _ok)
    assert resp.status == 403
    assert "internal_auth" not in req.store
    assert asked == []


@pytest.mark.asyncio
async def test_a_query_token_call_without_the_secret_stays_on_the_cookie_flow() -> None:
    """Only a caller PRESENTING the secret is moved onto the internal branch.

    The ordinary flow exchanges a ``?token=`` for a session cookie; the internal
    branch never does. Seeing the cookie proves the request took the same path it
    takes on any other app route.
    """
    token = generate_token("testuser", ttl_seconds=300)
    req = _request(_DECLARED)
    req.query = {"token": token}
    resp = await _middleware()(req, _ok)
    assert resp.status == 200
    assert resp.cookies.get("mc_token_5476") is not None
    assert "app_agent_route" not in req.store


@pytest.mark.asyncio
async def test_a_non_local_wrong_secret_with_a_cookie_stays_on_the_cookie_flow() -> None:
    """A remote request carrying the header is not an agent call, so the secret is
    not judged: the cookie decides, as on any other app route."""
    token = generate_token("testuser", ttl_seconds=300)
    bind_token_ip(token, "10.0.0.5")
    mark_consumed(token)
    req = _request(
        _DECLARED,
        headers={"X-Internal-Secret": "wrong"},
        cookies={"mc_token_5476": token},
        remote="10.0.0.5",
    )
    resp = await _middleware()(req, _ok)
    assert resp.status == 200
    assert "internal_auth" not in req.store


@pytest.mark.asyncio
async def test_a_cookie_call_on_a_declared_route_is_unchanged() -> None:
    token = generate_token("testuser", ttl_seconds=300)
    bind_token_ip(token, "127.0.0.1")
    mark_consumed(token)
    req = _request(_DECLARED, cookies={"mc_token_5476": token})
    resp = await _middleware()(req, _ok)
    assert resp.status == 200
    assert "internal_auth" not in req.store
    assert "app_agent_route" not in req.store


# ---------------------------------------------------------------------------
# Dispatch: the one admission decision, and the identity it publishes
# ---------------------------------------------------------------------------


def _dispatch_request(
    headers: dict[str, str],
    *,
    agent_arm: bool,
    sub_path: str = "subscriptions",
    method: str = "GET",
    app_name: str = "slack-poller",
) -> web.Request:
    req = make_mocked_request(
        method,
        f"/api/apps/{app_name}/{sub_path}",
        headers=headers,
        match_info={"app_name": app_name, "path": sub_path},
    )
    if agent_arm:
        req["internal_auth"] = True
        req["app_agent_route"] = True
    return req


def _capturing_registry(*, declares: bool = True) -> tuple[RouteRegistry, list[web.Request]]:
    seen: list[web.Request] = []

    async def handler(request: web.Request, _ctx: Any) -> web.Response:
        seen.append(request)
        return web.Response(text="ok")

    registry = RouteRegistry(MagicMock())
    registry._routes["slack-poller"] = [
        _RegisteredRoute(method="GET", path="/subscriptions", handler=handler, has_params=False)
    ]
    registry._contexts["slack-poller"] = MagicMock()
    registry._agent_routes["slack-poller"] = [("GET", "/subscriptions")] if declares else []
    return registry, seen


_SESSION = {"X-Session-Key": "dashboard:chat-1"}


@pytest.mark.asyncio
async def test_agent_arm_without_a_session_key_is_refused() -> None:
    registry, seen = _capturing_registry()
    resp = await registry.dispatch(_dispatch_request({}, agent_arm=True))
    assert resp.status == 403
    assert seen == []


@pytest.mark.asyncio
async def test_agent_arm_publishes_the_session_key_to_the_handler() -> None:
    registry, seen = _capturing_registry()
    resp = await registry.dispatch(_dispatch_request(_SESSION, agent_arm=True))
    assert resp.status == 200
    assert seen[0]["kirocrew_agent_session"] == "dashboard:chat-1"


_CORE_CREDENTIALS = {
    "X-Internal-Secret": SECRET,
    "X-Session-Token": "session-token-value",
    "X-Internal-Caller": "mcp",
    "Authorization": "Bearer dashboard-token-value",
    "Cookie": "kirocrew_token=dashboard-cookie-value",
}


@pytest.mark.asyncio
async def test_agent_arm_hands_the_handler_no_core_credential() -> None:
    registry, seen = _capturing_registry()
    resp = await registry.dispatch(
        _dispatch_request({**_SESSION, **_CORE_CREDENTIALS}, agent_arm=True)
    )
    assert resp.status == 200
    handed = seen[0]
    for name in _CORE_CREDENTIALS:
        assert name not in handed.headers
    raw = b"".join(key + value for key, value in handed.raw_headers)
    for value in _CORE_CREDENTIALS.values():
        assert value.encode() not in raw
    assert handed.headers["X-Session-Key"] == "dashboard:chat-1"
    assert handed["kirocrew_agent_session"] == "dashboard:chat-1"


@pytest.mark.asyncio
async def test_a_handler_replaying_its_request_headers_is_refused_by_the_gate() -> None:
    registry, seen = _capturing_registry()
    await registry.dispatch(
        _dispatch_request({**_SESSION, "X-Internal-Secret": SECRET}, agent_arm=True)
    )
    replay = _request(_DECLARED, headers=dict(seen[0].headers))
    resp = await _middleware()(replay, _ok)
    assert resp.status in (401, 403)
    assert replay.store.get("internal_auth") is None


@pytest.mark.asyncio
async def test_agent_arm_on_an_undeclared_route_is_refused_before_the_handler() -> None:
    """The declaration is checked here, once, against the route that will run."""
    registry, seen = _capturing_registry(declares=False)
    resp = await registry.dispatch(_dispatch_request(_SESSION, agent_arm=True))
    assert resp.status == 403
    assert "not a declared agent route" in cast(str, resp.text)
    assert seen == []


@pytest.mark.asyncio
async def test_a_cookie_request_on_an_undeclared_route_still_reaches_the_handler() -> None:
    registry, seen = _capturing_registry(declares=False)
    resp = await registry.dispatch(_dispatch_request(_SESSION, agent_arm=False))
    assert resp.status == 200
    assert len(seen) == 1 and "kirocrew_agent_session" not in seen[0]


@pytest.mark.asyncio
async def test_the_declaration_is_scoped_to_the_declaring_app() -> None:
    """Another app's identical path is its own: undeclared there, refused there."""
    registry, seen = _capturing_registry()
    registry._routes["other-app"] = list(registry._routes["slack-poller"])
    registry._contexts["other-app"] = MagicMock()
    resp = await registry.dispatch(
        _dispatch_request(_SESSION, agent_arm=True, app_name="other-app")
    )
    assert resp.status == 403
    assert seen == []
    resp = await registry.dispatch(_dispatch_request(_SESSION, agent_arm=True, method="POST"))
    assert resp.status == 404
    assert seen == []


@pytest.mark.asyncio
async def test_a_deregistered_app_admits_nothing_on_the_arm() -> None:
    registry, seen = _capturing_registry()
    registry.deregister_app_routes("slack-poller")
    resp = await registry.dispatch(_dispatch_request(_SESSION, agent_arm=True))
    assert resp.status == 404
    assert seen == []


@pytest.mark.asyncio
async def test_exact_undeclared_route_wins_over_declared_pattern_on_the_arm() -> None:
    """Admission follows the resolver's exact-first precedence, so a declared
    pattern cannot lend admission to an undeclared exact route, and a ``{param}``
    never carries a traversal token to the handler."""
    from kiro_crew.apps.route_registry import _compile_pattern

    registry, seen = _capturing_registry()
    handler = registry._routes["slack-poller"][0].handler
    pattern = _RegisteredRoute(method="GET", path="/items/{id}", handler=handler, has_params=True)
    pattern.compiled, pattern.param_names = _compile_pattern(pattern.path)
    registry._routes["slack-poller"] = [
        pattern,
        _RegisteredRoute(method="GET", path="/items/admin", handler=handler, has_params=False),
    ]
    registry._agent_routes["slack-poller"] = [("GET", "/items/{id}")]

    resp = await registry.dispatch(_dispatch_request(_SESSION, agent_arm=True, sub_path="items/42"))
    assert resp.status == 200 and seen[-1].match_info["id"] == "42"
    for sub_path, status in (("items/admin", 403), ("items/..", 403), ("missing", 404)):
        resp = await registry.dispatch(
            _dispatch_request(_SESSION, agent_arm=True, sub_path=sub_path)
        )
        assert resp.status == status, sub_path
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_a_cookie_request_with_a_session_key_header_gets_no_agent_session() -> None:
    registry, seen = _capturing_registry()
    resp = await registry.dispatch(_dispatch_request(_SESSION, agent_arm=False))
    assert resp.status == 200
    assert "kirocrew_agent_session" not in seen[0]


@pytest.mark.asyncio
async def test_an_internal_call_off_the_agent_arm_gets_no_agent_session() -> None:
    registry, seen = _capturing_registry()
    req = _dispatch_request(_SESSION, agent_arm=False)
    req["internal_auth"] = True
    resp = await registry.dispatch(req)
    assert resp.status == 200
    assert "kirocrew_agent_session" not in seen[0]


@pytest.mark.asyncio
async def test_an_agent_mark_without_an_internal_grant_gets_no_agent_session() -> None:
    """Both marks are required: ``app_agent_route`` alone proves no secret check ran."""
    registry, seen = _capturing_registry()
    req = _dispatch_request(_SESSION, agent_arm=False)
    req["app_agent_route"] = True
    resp = await registry.dispatch(req)
    assert resp.status == 200
    assert "kirocrew_agent_session" not in seen[0]


def _recording_dispatch_sel(monkeypatch: pytest.MonkeyPatch) -> _RecordingSel:
    from kiro_crew.apps import route_registry as rr

    recording = _RecordingSel()
    monkeypatch.setattr(rr, "sel", lambda: recording)
    return recording


@pytest.mark.asyncio
async def test_the_agent_arm_dispatch_row_names_the_session_and_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = _recording_dispatch_sel(monkeypatch)
    registry, _seen = _capturing_registry()
    resp = await registry.dispatch(_dispatch_request(_SESSION, agent_arm=True))
    assert resp.status == 200
    (row,) = [r for r in recording.rows if r.get("outcome") == "ok"]
    assert row["caller"] == "dashboard:chat-1"
    assert row["resources"] == "app:slack-poller GET /subscriptions"


@pytest.mark.asyncio
async def test_the_undeclared_refusal_row_names_the_session_and_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = _recording_dispatch_sel(monkeypatch)
    registry, _seen = _capturing_registry(declares=False)
    resp = await registry.dispatch(_dispatch_request(_SESSION, agent_arm=True))
    assert resp.status == 403
    (row,) = [r for r in recording.rows if r.get("outcome") == "denied"]
    assert row["caller"] == "dashboard:chat-1"
    assert row["resources"] == "app:slack-poller GET /subscriptions"
    assert row["error"] == "route is not a declared agent route"


@pytest.mark.asyncio
async def test_a_cookie_dispatch_row_keeps_the_app_as_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = _recording_dispatch_sel(monkeypatch)
    registry, _seen = _capturing_registry()
    resp = await registry.dispatch(_dispatch_request(_SESSION, agent_arm=False))
    assert resp.status == 200
    (row,) = [r for r in recording.rows if r.get("outcome") == "ok"]
    assert row["caller"] == "app:slack-poller"
    assert row["resources"] == "GET /subscriptions"


# ---------------------------------------------------------------------------
# The whole chain: token_auth gate, catch-all, dispatch
# ---------------------------------------------------------------------------

_STATIC_INTERNAL = "/api/apps/issue-radar/investigation"


def _chain(monkeypatch: pytest.MonkeyPatch) -> tuple[web.Application, list[web.Request]]:
    """Two catch-all apps behind the real middleware: ``slack-poller`` declares one
    agent route; ``issue-radar`` declares none and is reached, as on main, through
    a static mixed-internal entry."""
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr(_ta, "_derive_internal_caller_app", lambda _request: "")
    monkeypatch.setattr(_ta, "_internal_caller_record_missing", lambda _request: False)
    seen: list[web.Request] = []

    async def handler(request: web.Request, _ctx: Any) -> web.Response:
        seen.append(request)
        return web.json_response({"ran": request.path})

    app = web.Application()
    registry = RouteRegistry(app)
    registry._routes["slack-poller"] = [
        _RegisteredRoute(method="GET", path="/subscriptions", handler=handler, has_params=False),
        _RegisteredRoute(method="GET", path="/settings", handler=handler, has_params=False),
    ]
    registry._contexts["slack-poller"] = MagicMock()
    registry._agent_routes["slack-poller"] = [("GET", "/subscriptions")]
    registry._routes["issue-radar"] = [
        _RegisteredRoute(method="PUT", path="/investigation", handler=handler, has_params=False)
    ]
    registry._contexts["issue-radar"] = MagicMock()
    registry.ensure_catch_all()
    app.middlewares.append(
        token_auth_middleware(
            mixed_internal_paths=frozenset({_STATIC_INTERNAL}),
            internal_secret=SECRET,
            agent_route_arm=registry.agent_route_arm,
        )
    )
    return app, seen


@pytest.mark.asyncio
async def test_chain_admits_a_declared_route_and_publishes_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, seen = _chain(monkeypatch)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(_DECLARED, headers=_AGENT_HEADERS)
        assert resp.status == 200
    (request,) = seen
    assert request["kirocrew_agent_session"] == "cron:job-7"
    assert request["app_agent_route"] is True


@pytest.mark.asyncio
async def test_chain_refuses_an_undeclared_route_under_an_armed_app_without_any_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The arm sets ``internal_auth`` on every catch-all request; dispatch is what
    keeps an undeclared route from running on it."""
    app, seen = _chain(monkeypatch)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/apps/slack-poller/settings", headers=_AGENT_HEADERS)
        assert resp.status == 403
        assert "not a declared agent route" in await resp.text()
    assert seen == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/apps/issue-radar%2F..%2Fslack-poller/subscriptions",
        "/api/apps/slack-poller%2F../issue-radar/subscriptions",
    ],
)
@pytest.mark.asyncio
async def test_chain_runs_no_handler_for_an_encoded_traversal_out_of_the_caller_app(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """An ``issue-radar`` session cannot reach ``slack-poller``'s declared route by
    hiding a separator or dot segment in the app-name segment."""
    app, seen = _chain(monkeypatch)
    _owned_by(monkeypatch, "issue-radar")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(path, headers=_AGENT_HEADERS)
        assert resp.status in (403, 404)
    assert seen == []


@pytest.mark.asyncio
async def test_chain_leaves_a_static_internal_path_under_api_apps_exactly_as_on_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue Radar's investigation route declares nothing and must still reach its
    handler through the static entry, unmarked and with no agent session."""
    app, seen = _chain(monkeypatch)
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(_STATIC_INTERNAL, headers=_AGENT_HEADERS)
        assert resp.status == 200
        assert await resp.json() == {"ran": _STATIC_INTERNAL}
    (request,) = seen
    assert request["internal_auth"] is True
    assert "app_agent_route" not in request
    assert "kirocrew_agent_session" not in request


@pytest.mark.asyncio
async def test_chain_refuses_a_cookieless_browser_style_call_on_a_declared_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, seen = _chain(monkeypatch)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(_DECLARED)
        assert resp.status == 403
    assert seen == []
