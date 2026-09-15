"""The agent's pod lifecycle path: gateway routes, their guards, and the tools.

An agent session cannot reach the systemd user bus, so ``kirocrew pod up`` in one
of its shells fails with ``Permission denied``. These four routes are the gateway
doing that part on the agent's behalf, and the ``pod_*`` MCP tools are how a
session calls them. What is worth pinning is therefore not the pod mechanics --
``worktree_ops`` owns those and has its own tests -- but the seams that make the
detour safe and the answer usable:

* every route is admitted for the internal secret and nothing else,
* an app the operator never enabled is not callable,
* a refusal reaches the model as an ``Error:`` string rather than a fake handle,
* the pod token survives the trip verbatim, because a redacted token is useless.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp import web

from kiro_crew.apps.builtins.dev_fleet import agent_pod_api
from kiro_crew.mcp_tools import apps as apps_tools

# ---------------------------------------------------------------------------
# Route registration + admission
# ---------------------------------------------------------------------------

_EXPECTED_ROUTES = {
    ("POST", "/api/apps/dev-fleet/pod/up"),
    ("POST", "/api/apps/dev-fleet/pod/down"),
    ("GET", "/api/apps/dev-fleet/pod/status"),
    ("GET", "/api/apps/dev-fleet/pod/list"),
}


def test_register_routes_mounts_exactly_the_agent_surface() -> None:
    """A fifth route here would be one the strict-path table has not admitted."""
    app = web.Application()
    agent_pod_api.register_routes(app)
    mounted = {
        (r.method, r.resource.canonical)
        for r in app.router.routes()
        if r.method != "HEAD"  # aiohttp adds HEAD alongside every GET
    }
    assert mounted == _EXPECTED_ROUTES


def test_package_reexports_register_routes() -> None:
    """Gateway startup looks for ``register_routes`` on the PACKAGE, not a submodule.

    The package hook composes this module's agent-facing pod routes with the
    gateway-side cutover routes (``gateway_routes.py``), so what is pinned is that a
    call through the package mounts every pod route — identity with this module's
    function alone would leave the other family unmounted.
    """
    import kiro_crew.apps.builtins.dev_fleet as pkg

    app = web.Application()
    pkg.register_routes(app)
    mounted = {(r.method, r.resource.canonical) for r in app.router.routes() if r.method != "HEAD"}
    assert _EXPECTED_ROUTES <= mounted, sorted(_EXPECTED_ROUTES - mounted)


def test_every_route_is_named_in_the_strict_internal_table() -> None:
    """An unlisted route 403s for the agent; a listed prefix admits its whole subtree.

    Both halves matter, so this asserts the paths are present AND that the bare
    ``/pod`` prefix is not -- Dev Fleet's neighbourhood includes worktree prune and
    the Make Live cutover, which must never inherit an internal-secret grant.
    """
    from kiro_crew.dashboard.server import (
        _MIXED_INTERNAL_API_PATHS,
        _STRICT_INTERNAL_API_PATHS,
    )

    for _method, path in _EXPECTED_ROUTES:
        assert path in _STRICT_INTERNAL_API_PATHS, path
    assert "/api/apps/dev-fleet/pod" not in _STRICT_INTERNAL_API_PATHS
    # Strict, not mixed: no browser calls these. The dashboard's pod buttons go
    # through the app-backend reverse proxy instead.
    assert not any(p.startswith("/api/apps/dev-fleet/pod") for p in _MIXED_INTERNAL_API_PATHS)


class _Req:
    """The request attributes the guards read."""

    def __init__(
        self,
        *,
        remote: str = "127.0.0.1",
        internal_auth: bool = True,
        body: Any = None,
        query: dict[str, str] | None = None,
        unix: bool = False,
    ) -> None:
        self.remote = remote
        self._data = {"internal_auth": internal_auth}
        self._body = body
        self.can_read_body = body is not None
        self.query = query or {}
        self.path = "/api/apps/dev-fleet/pod/up"
        self.transport = object() if unix else None

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    async def json(self) -> Any:
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body


def _payload(response: web.Response) -> dict[str, Any]:
    return json.loads(response.body.decode())


@pytest.fixture()
def granted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default posture for the tests below: the Dev Fleet app is enabled."""
    monkeypatch.setattr(
        "kiro_crew.apps.builtins.dev_fleet.agent_pod_api.is_app_enabled", lambda _n: True
    )


@pytest.mark.asyncio
async def test_the_unix_socket_transport_is_admitted(
    monkeypatch: pytest.MonkeyPatch, granted: None
) -> None:
    """mcp_core PREFERS the gateway's AF_UNIX socket, where request.remote is empty.

    Testing only the loopback half would refuse every pod tool on POSIX -- the only
    platform where systemd --user pods exist -- so this pins the union the
    middleware itself uses (`_unix_sock is not None or is_loopback(...)`).
    """
    monkeypatch.setattr("kiro_crew.dashboard.origin.request_is_unix_socket", lambda _r: True)
    monkeypatch.setattr(
        agent_pod_api, "_ops", lambda: type("Ops", (), {"_pod_ls": staticmethod(_ok_ls)})
    )
    resp = await agent_pod_api.handle_pod_list(_Req(remote="", unix=True))  # type: ignore[arg-type]
    assert resp.status == 200


async def _ok_ls() -> dict[str, Any]:
    return {"ok": True, "pods": []}


@pytest.mark.asyncio
async def test_a_non_local_caller_is_refused(granted: None) -> None:
    """A `local_only=False` deployment reclassifies strict paths as mixed."""
    resp = await agent_pod_api.handle_pod_list(_Req(remote="10.0.0.7"))  # type: ignore[arg-type]
    assert resp.status == 403
    assert _payload(resp)["code"] == "loopback_only"


@pytest.mark.asyncio
async def test_cookie_only_caller_is_refused(granted: None) -> None:
    """Listed-as-strict does not prove the secret was checked; the handler re-checks."""
    resp = await agent_pod_api.handle_pod_list(_Req(internal_auth=False))  # type: ignore[arg-type]
    assert resp.status == 403
    assert _payload(resp)["code"] == "internal_secret_required"


@pytest.mark.asyncio
async def test_disabled_app_is_not_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Routes are registered at startup, before any app is known to be enabled."""
    monkeypatch.setattr(
        "kiro_crew.apps.builtins.dev_fleet.agent_pod_api.is_app_enabled", lambda _n: False
    )
    resp = await agent_pod_api.handle_pod_list(_Req())  # type: ignore[arg-type]
    assert resp.status == 403
    assert _payload(resp)["code"] == "app_not_enabled"


def test_the_capability_is_not_gated_on_an_operator_grant() -> None:
    """Agent pod control is an intended capability, so it carries no operator gate.

    Pinned as a test because the reverse is what a reader would assume from the
    security note in the module docstring -- and because adding a grant would
    silently break the unattended QA loop these routes exist to restore. The
    residual such a grant would appear to close is Kiro Crew's documented posture
    (security.md, "Scoped user-bus locator forward"), reachable by other paths
    regardless, so a gate here buys nothing and costs the loop.
    """
    assert not hasattr(agent_pod_api, "_operator_granted")
    assert not hasattr(agent_pod_api, "_require_operator_grant")

    from kiro_crew.config.sections import AgentConfig

    assert not hasattr(AgentConfig(), "allow_agent_pod_control")


@pytest.mark.asyncio
async def test_missing_worktree_is_a_400_with_a_code(granted: None) -> None:
    resp = await agent_pod_api.handle_pod_up(_Req(body={}))  # type: ignore[arg-type]
    assert resp.status == 400
    assert _payload(resp)["code"] == "invalid_worktree"


@pytest.mark.asyncio
async def test_provisioning_is_not_reachable_from_the_agent_surface(granted: None) -> None:
    """A minutes-long blocking call dies to any timeout with no way to learn the outcome.

    The schema rejects the key, so a model that asks for it is told rather than
    silently given a 16-minute call. The dashboard's Provision button streams the
    same work under a run id, which is the shape that survives a restart.
    """
    from kiro_crew.validation import POD_UP_SCHEMA

    assert [f.name for f in POD_UP_SCHEMA.fields] == ["worktree"]
    assert "provision" not in {
        k
        for t in apps_tools.schemas()
        if t["name"] == "pod_up"
        for k in t["inputSchema"]["properties"]
    }


@pytest.mark.asyncio
async def test_pod_up_returns_the_handle(monkeypatch: pytest.MonkeyPatch, granted: None) -> None:
    seen: dict[str, Any] = {}

    async def _fake_up(name: str) -> dict[str, Any]:
        seen["name"] = name
        return {
            "ok": True,
            "name": name,
            "status": "up",
            "port": 7913,
            "base_url": "http://127.0.0.1:7913",
            "token": "tok-abc",
            "ttl": "2h",
        }

    monkeypatch.setattr(
        agent_pod_api, "_ops", lambda: type("Ops", (), {"_pod_up": staticmethod(_fake_up)})
    )
    resp = await agent_pod_api.handle_pod_up(
        _Req(body={"worktree": "kc-wt-1"})  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert seen == {"name": "kc-wt-1"}
    assert _payload(resp)["token"] == "tok-abc"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "req"),
    [
        ("handle_pod_up", {"body": {"worktree": "w"}}),
        ("handle_pod_down", {"body": {"worktree": "w"}}),
        ("handle_pod_status", {"query": {"worktree": "w"}}),
        ("handle_pod_list", {}),
    ],
)
async def test_every_verb_answers_an_unusable_checkout(
    monkeypatch: pytest.MonkeyPatch, handler: str, req: dict[str, Any], granted: None
) -> None:
    """``repository._repo()`` RAISES, and it sits on more than one branch per verb.

    The four-verb table is the point. Guarding only the subprocess cwd leaves the
    discovery chain inside ``_pod_checkout_guard`` (``_find_worktree`` ->
    ``_discover_worktrees`` -> ``_repo()``, repository.py:581) reaching the handler as
    an unhandled exception, so three of the four verbs would still answer a setup
    problem with a 500. Catching once around the whole op covers every branch,
    whichever one raises, which is why this asserts on the verbs rather than on the
    raise sites.
    """
    from kiro_crew.apps.builtins.dev_fleet import repository

    async def _aboom(*_a: Any, **_k: Any) -> Any:
        raise repository.RepoNotConfigured("no Kiro Crew checkout found to manage")

    class _Ops:
        _pod_up = staticmethod(_aboom)
        _pod_down = staticmethod(_aboom)
        _pod_status = staticmethod(_aboom)
        _pod_ls = staticmethod(_aboom)

    monkeypatch.setattr(agent_pod_api, "_ops", lambda: _Ops)
    resp = await getattr(agent_pod_api, handler)(_Req(**req))  # type: ignore[arg-type]
    assert resp.status == 409, handler
    payload = _payload(resp)
    assert payload["code"] == "repo_unavailable"
    assert "checkout" in payload["error"]


@pytest.mark.asyncio
async def test_a_refused_pod_op_is_409_not_500(
    monkeypatch: pytest.MonkeyPatch, granted: None
) -> None:
    """The request was well-formed and authorized; the HOST could not carry it out."""

    async def _fake_down(name: str) -> dict[str, Any]:
        return {"ok": False, "error": "unknown worktree: 'nope'"}

    monkeypatch.setattr(
        agent_pod_api, "_ops", lambda: type("Ops", (), {"_pod_down": staticmethod(_fake_down)})
    )
    resp = await agent_pod_api.handle_pod_down(_Req(body={"worktree": "nope"}))  # type: ignore[arg-type]
    assert resp.status == 409
    assert _payload(resp)["code"] == "pod_down_failed"


@pytest.mark.asyncio
async def test_an_overlong_worktree_name_is_refused(granted: None) -> None:
    """The name reaches worktree matching and a filesystem path before validate_name."""
    resp = await agent_pod_api.handle_pod_up(
        _Req(body={"worktree": "x" * 5000})  # type: ignore[arg-type]
    )
    assert resp.status == 400
    assert _payload(resp)["code"] == "invalid_worktree"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["{not json", "[1, 2]"])
async def test_a_non_object_body_is_refused(body: str, granted: None) -> None:
    resp = await agent_pod_api.handle_pod_down(_Req(body=body))  # type: ignore[arg-type]
    assert resp.status == 400
    assert _payload(resp)["code"] == "invalid_body"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "op", "code", "req"),
    [
        ("handle_pod_up", "_pod_up", "pod_up_failed", {"body": {"worktree": "w"}}),
        ("handle_pod_status", "_pod_status", "pod_status_failed", {"query": {"worktree": "w"}}),
        ("handle_pod_list", "_pod_ls", "pod_list_failed", {}),
    ],
)
async def test_each_verb_reports_its_own_refusal_code(
    monkeypatch: pytest.MonkeyPatch,
    handler: str,
    op: str,
    code: str,
    req: dict[str, Any],
    granted: None,
) -> None:
    """One code per verb: an agent branching on the answer must know which call failed."""

    async def _fake(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"ok": False}

    monkeypatch.setattr(agent_pod_api, "_ops", lambda: type("Ops", (), {op: staticmethod(_fake)}))
    resp = await getattr(agent_pod_api, handler)(_Req(**req))  # type: ignore[arg-type]
    assert resp.status == 409
    payload = _payload(resp)
    assert payload["code"] == code
    # An empty in-band error must not become an empty sentence.
    assert payload["error"]


def test_ops_resolves_the_real_worktree_ops_module() -> None:
    """The lazy import is the seam every handler depends on; a typo there is silent."""
    from kiro_crew.apps.builtins.dev_fleet import worktree_ops

    assert agent_pod_api._ops() is worktree_ops
    assert callable(worktree_ops._pod_status)
    assert callable(worktree_ops._pod_ls)


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def test_pod_tools_are_advertised_with_handlers() -> None:
    """A descriptor with no handler advertises a tool that cannot run."""
    advertised = {t["name"] for t in apps_tools.schemas()}
    for name in ("pod_up", "pod_down", "pod_status", "pod_ls"):
        assert name in advertised
        assert name in apps_tools.HANDLERS


def test_pod_tools_are_registered_for_argument_validation() -> None:
    """A tool absent from MCP_CORE_SCHEMAS has its arguments passed through raw."""
    from kiro_crew.validation import MCP_CORE_SCHEMAS

    for name in ("pod_up", "pod_down", "pod_status", "pod_ls"):
        assert name in MCP_CORE_SCHEMAS


def test_pod_up_tool_returns_the_token_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token IS the deliverable -- a redacted one is a handle that cannot be used."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_post(path: str, body: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        calls.append((path, body or {}))
        return {
            "ok": True,
            "name": "kc-wt-1",
            "status": "up",
            "port": 7913,
            "base_url": "http://127.0.0.1:7913",
            "token": "tok-abc123",
            "ttl": "2h",
        }

    monkeypatch.setattr(apps_tools.mcp_core, "_post", _fake_post)
    out = apps_tools.pod_up("pod_up", {"worktree": "kc-wt-1"})
    assert "tok-abc123" in out
    assert "http://127.0.0.1:7913" in out
    assert calls[0][0] == "/api/apps/dev-fleet/pod/up"
    assert calls[0][1] == {"worktree": "kc-wt-1"}


def test_pod_up_tool_says_so_when_the_cli_withheld_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty token is a deliberate refusal to certify the port, not an oversight."""
    monkeypatch.setattr(
        apps_tools.mcp_core,
        "_post",
        lambda *_a, **_k: {"ok": True, "name": "kc-wt-1", "port": 7913, "token": ""},
    )
    out = apps_tools.pod_up("pod_up", {"worktree": "kc-wt-1"})
    assert "withheld" in out
    assert not out.startswith("Error:")


def test_a_route_refusal_reaches_the_model_as_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Error:`` is load-bearing: call_tool_with_logging derives the outcome from it."""
    monkeypatch.setattr(
        apps_tools.mcp_core,
        "_post",
        lambda *_a, **_k: {"ok": False, "code": "pod_up_failed", "error": "no built dist"},
    )
    out = apps_tools.pod_up("pod_up", {"worktree": "kc-wt-1"})
    assert out.startswith("Error:")
    assert "pod_up_failed" in out
    assert "no built dist" in out


def test_a_transport_failure_is_not_reported_as_a_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gateway-helper ``{"error": ...}`` carries no ``ok`` at all."""
    monkeypatch.setattr(
        apps_tools.mcp_core, "_post", lambda *_a, **_k: {"error": "connection refused"}
    )
    assert apps_tools.pod_down("pod_down", {"worktree": "kc-wt-1"}).startswith("Error:")


def test_pod_status_url_encodes_the_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    """The name reaches a query string, so a caller cannot smuggle a second param."""
    seen: list[str] = []

    def _fake_get(path: str, session_key: str | None = None, **kw: Any) -> dict[str, Any]:
        seen.append(path)
        return {"ok": True, "name": "a b", "status": "down", "port": 7999, "health": 0}

    monkeypatch.setattr(apps_tools.mcp_core, "_get", _fake_get)
    apps_tools.pod_status("pod_status", {"worktree": "a b&x=1"})
    assert seen[0] == "/api/apps/dev-fleet/pod/status?worktree=a%20b%26x%3D1"


def test_pod_ls_reports_an_empty_host_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apps_tools.mcp_core, "_get", lambda *_a, **_k: {"ok": True, "pods": []})
    assert "No pods" in apps_tools.pod_ls("pod_ls", {})


def test_pod_ls_lists_each_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        apps_tools.mcp_core,
        "_get",
        lambda *_a, **_k: {
            "ok": True,
            "pods": [
                {"name": "kc-wt-1", "port": 7913, "health": 200},
                {"name": "kc-wt-2", "port": 7940, "health": 0},
            ],
        },
    )
    out = apps_tools.pod_ls("pod_ls", {})
    assert "kc-wt-1" in out
    assert "kc-wt-2" in out
    assert "2 pod(s) active" in out


def test_read_tools_ask_for_more_than_the_telemetry_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pod read spawns a python subprocess in the gateway; 10s cannot cover it."""
    seen: dict[str, Any] = {}

    def _fake_get(path: str, session_key: str | None = None, **kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {"ok": True, "pods": []}

    monkeypatch.setattr(apps_tools.mcp_core, "_get", _fake_get)
    apps_tools.pod_ls("pod_ls", {})
    assert seen["timeout"] > 10
