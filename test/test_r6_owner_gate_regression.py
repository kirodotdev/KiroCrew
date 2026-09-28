"""Owner gate on the mutating routes of five dashboard handler modules.

``handlers/_shared.require_owner_dashboard_request`` is the shared owner gate, and
``handlers/hooks.py``, ``handlers/mcp_custom.py``, ``handlers/mcp.py``,
``handlers/security.py`` and ``handlers_instances.py`` reach it through their
mutating routes. This module pins BOTH directions at once, because a gate is only
correct as a pair: the wrong caller refused, and the right caller unaffected.

The route walks are what make it durable. Each is driven off the REAL registrar,
so a mutating route added to one of these modules later is picked up here without
anyone editing a list -- which is the failure the per-handler style invites. Each
walk also asserts its own enumeration is non-empty and contains the routes the
findings name, so a registrar refactor that drops routes out of the walk fails
instead of passing vacuously.

Three boundaries are asserted as controls rather than left implied:

* ``POST /api/hooks/agent`` must stay OUT. External systems (CI runners, review
  bots, deploy pipelines) post there holding a webhook token and no dashboard
  cookie, and the handler does its own bearer check -- see ``token_auth``'s note
  on that route. An owner gate there would close the documented inbound path.
* The READ routes of the same modules must stay open. The gate covers mutating
  verbs, never a whole module, so a module-wide guard or registrar middleware is
  the cheap wrong fix and shows up here.
* The standalone-local install must keep working. With no owner configured the
  predicate admits the signed local bootstrap subject, which is what lets a
  single-user gateway drive these routes at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import mcp_discovery
from kiro_crew.dashboard import handlers_instances
from kiro_crew.dashboard.handlers import _shared as shared_handlers
from kiro_crew.dashboard.handlers import mcp as mcp_handlers
from kiro_crew.dashboard.routes import agent_config as agent_config_routes
from kiro_crew.dashboard.routes import connections as connections_routes
from kiro_crew.dashboard.routes import messaging as messaging_routes
from kiro_crew.dashboard.routes import system as system_routes

pytestmark = pytest.mark.asyncio

_OWNER = "U0OWNER"
_NON_OWNER = "U0NONOWNER"
_LOCAL_SUBJECT = "local-app"

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: The five modules whose mutating routes carry the gate.
_GATED_MODULES = frozenset(
    {
        "kiro_crew.dashboard.handlers.hooks",
        "kiro_crew.dashboard.handlers.mcp_custom",
        "kiro_crew.dashboard.handlers.mcp",
        "kiro_crew.dashboard.handlers.security",
        "kiro_crew.dashboard.handlers_instances",
    }
)

#: The inbound agent webhook authenticates itself and must not acquire the gate.
_SELF_AUTHED = ("POST", "/api/hooks/agent")

#: aiohttp reports the wildcard proxy route with this method; it is covered
#: directly through ``_guard`` instead, since driving it would open a peer socket.
_WILDCARD = "*"

#: Routes the findings name, asserted present so no walk passes vacuously.
_MUST_BE_WALKED = frozenset(
    {
        ("POST", "/api/hooks"),
        ("POST", "/api/hooks/{hook_id}/test"),
        ("POST", "/api/webhooks/switch"),
        ("PATCH", "/api/webhooks/tokens/{token_id}"),
        ("DELETE", "/api/webhooks/tokens/{token_id}"),
        ("POST", "/api/webhooks/tokens"),
        ("POST", "/api/mcp/custom"),
        ("PUT", "/api/mcp/custom/{name}"),
        ("POST", "/api/mcp/apply"),
        ("PUT", "/api/mcp/servers/{name}"),
        ("DELETE", "/api/mcp/servers/{name}"),
        ("PATCH", "/api/security/denied-commands/disable-all"),
        ("PUT", "/api/security/trusted-apps/allow-all"),
        ("POST", "/api/instances"),
        ("POST", "/api/instances/{id}/refresh-token"),
    }
)

_REFUSAL = {"error": "owner authorization required", "code": "owner_only"}


#: The four modules that reach the gate through
#: ``_shared.require_owner_dashboard_request``. ``handlers_instances`` is absent on
#: purpose: it gates inside ``_guard``, which is a pure function covered directly
#: below, so it needs no route drive at all.
_SHARED_GATE_MODULES = frozenset(_GATED_MODULES - {"kiro_crew.dashboard.handlers_instances"})

#: Status the stand-in gate answers with. Outside every status any handler body
#: produces, so "the gate ran and stopped here" cannot be confused with a body.
_GATE_REACHED = 299


@pytest.fixture(autouse=True)
def no_operator_config_and_no_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Seal this module off from the operator's home and from process spawning.

    The read routes in these modules run their own bodies by design -- that is what
    "reads stay open" means, so the walk has to drive them. Two of those reads,
    ``GET /api/mcp`` and ``GET /api/mcp/probe``, arm a background re-probe when the
    probe cache is stale, and ``_mcp_probe_ts`` starts at ``0.0``, so a fresh
    process is always stale. That background task reaches
    ``mcp_discovery.probe_all``, which SPAWNS every enabled MCP server. The servers
    come from ``~/.kiro/settings/mcp.json``, and ``handlers/mcp.py`` binds that path
    from ``Path.home()`` at import time, which no environment pin rebinds -- so on a
    developer machine with a real MCP config the walk would start the operator's own
    processes, with this repository as their working directory.

    Three independent seals, because one is a single point of failure for a hazard
    of this shape:

    * ``Path.home`` itself is repointed, so a path resolved at import time or late
      lands in the tmp tree whether or not it consults the environment.
    * ``_arm_reprobe`` is the ONE place a background probe task is created (two call
      sites, both in the reads named above). The stand-in records instead of
      creating, so no task exists to spawn anything.
    * ``mcp_discovery.probe_all`` and ``probe_server`` raise. Nothing should reach
      them; if a later edit finds another route to a spawn, this fails the test
      loudly instead of starting a process.

    Returns the ``_arm_reprobe`` record so a test can assert the seal is
    LOAD-BEARING rather than decorative -- a seal the walk never reaches would prove
    nothing about the hazard.
    """
    home = tmp_path / "home"
    (home / ".kiro" / "settings").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIROCREW_HOME", str(home / ".kiro" / "crew"))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(mcp_handlers, "_GLOBAL_MCP_JSON", home / ".kiro" / "settings" / "mcp.json")
    monkeypatch.setattr(mcp_handlers, "_KIROCREW_MCP_JSON", home / "kirocrew.mcp.json")
    monkeypatch.setattr(mcp_discovery, "_MCP_JSON_PATHS", ())

    # Establish the stale precondition rather than inheriting it. ``_mcp_probe_ts``
    # and ``_mcp_probe_in_progress`` are MODULE globals, so a sibling test that
    # probed earlier in the same worker leaves the cache fresh and the re-probe
    # unarmed -- which made the load-bearing assertion below pass serially and fail
    # under xdist. Pinning both makes the premise this module's own.
    monkeypatch.setattr(mcp_handlers, "_mcp_probe_ts", 0.0)
    monkeypatch.setattr(mcp_handlers, "_mcp_probe_in_progress", False)

    armed: list[str] = []

    def _record_instead_of_arming(request) -> None:
        armed.append(request.path)

    monkeypatch.setattr(mcp_handlers, "_arm_reprobe", _record_instead_of_arming)

    async def _refuse_to_spawn(*_a, **_kw):
        raise AssertionError("a route walk must never spawn an MCP server")

    monkeypatch.setattr(mcp_discovery, "probe_all", _refuse_to_spawn)
    monkeypatch.setattr(mcp_discovery, "probe_server", _refuse_to_spawn)
    return armed


@pytest.fixture
def gate_recorder(monkeypatch: pytest.MonkeyPatch):
    """Replace the shared gate with a stand-in that records and short-circuits.

    Every gated handler in these four modules imports
    ``require_owner_dashboard_request`` from ``_shared`` in its own body, so one
    patch on the module attribute covers all of them.

    This is what lets the "caller gets PAST the gate" direction be asserted
    without running a single handler body. Driving the real gate with a caller it
    ADMITS means the body then executes -- and these bodies spawn MCP server
    processes from operator configuration (``POST /api/mcp/probe``), uninstall
    packages, and rewrite config files. A test must never do that, and the walk
    does not need to: reaching the gate is the whole property.

    It is also a STRONGER assertion than the previous one. A route answering
    ``_GATE_REACHED`` proves the gate was called and that it ran before any side
    effect; "the answer was not 403" proved only the second-weakest thing.
    """
    seen: list[str] = []

    async def _stand_in(request, operation: str):
        seen.append(operation)
        return web.json_response({"gate": "reached", "operation": operation}, status=_GATE_REACHED)

    monkeypatch.setattr(shared_handlers, "require_owner_dashboard_request", _stand_in)
    return seen


class _State:
    """Minimal dashboard state: only what the gate and the walks read."""

    context_builder = None

    def __init__(self, owner_id: str = _OWNER) -> None:
        self.owner_id = owner_id
        self._background_tasks: set = set()
        self._slots: dict = {}
        self.instances_registry = SimpleNamespace(get=lambda _i: None, list=lambda: [])
        self.instances_manager = None

    def push_refresh(self, _topic: str) -> None:  # pragma: no cover - UI nudge
        pass


def _build_app(owner_id: str = _OWNER) -> web.Application:
    """Every registrar that mounts one of the five modules, plus identity."""

    @web.middleware
    async def _identity(request, handler):
        request["user"] = request.headers.get("X-Test-User", _NON_OWNER)
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = _State(owner_id)
    messaging_routes.register(app)
    agent_config_routes.register(app)
    system_routes.register(app)
    connections_routes.register(app)
    return app


def _walk(app: web.Application, methods) -> set[tuple[str, str]]:
    """Routes of the five modules whose method is in *methods*."""
    found: set[tuple[str, str]] = set()
    for route in app.router.routes():
        if route.method not in methods:
            continue
        if getattr(route.handler, "__module__", "") not in _GATED_MODULES:
            continue
        if route.resource is None:
            continue
        found.add((route.method, route.resource.canonical))
    return found


def _concrete(path: str) -> str:
    """Fill ``{param}`` placeholders with a value no fixture has installed."""
    out = []
    for part in path.split("/"):
        out.append("nonexistent" if part.startswith("{") else part)
    return "/".join(out)


async def _drive(client: TestClient, method: str, path: str, *, user: str):
    return await client.request(
        method,
        _concrete(path),
        json={},
        headers={"X-Test-User": user},
    )


async def test_every_mutating_route_in_the_five_modules_refuses_a_non_owner() -> None:
    """The defect direction, over the whole surface rather than a sample.

    An authenticated non-owner is a real principal: an allow-listed messaging user
    running ``!dashboard`` holds an ordinary dashboard session (``app == ""``,
    ``sub != owner_id``) that token auth admits. Every mutating route in these
    modules must answer the shared 403 body -- the same one the 50-odd existing
    call sites of the predicate return -- and the body is read, not just the
    status, so a 403 raised for some other reason cannot pass as the gate.
    """
    app = _build_app()
    routes = _walk(app, _MUTATING) - {_SELF_AUTHED}
    assert _MUST_BE_WALKED <= routes, f"walk lost named routes: {sorted(_MUST_BE_WALKED - routes)}"

    async with TestClient(TestServer(app)) as client:
        ungated = []
        for method, path in sorted(routes):
            resp = await _drive(client, method, path, user=_NON_OWNER)
            if resp.status != 403 or await resp.json() != _REFUSAL:
                ungated.append((method, path, resp.status))
        assert not ungated, f"mutating routes admitted a non-owner: {ungated}"


async def test_the_inbound_agent_webhook_does_not_acquire_the_gate() -> None:
    """CONTROL: ``POST /api/hooks/agent`` keeps its own bearer check.

    Its callers hold a webhook token and nothing else, so the owner body here
    would break every CI runner and review bot pointed at this gateway. The
    assertion is on the refusal SHAPE: whatever this route answers an anonymous
    caller, it must not be the owner gate's.
    """
    app = _build_app()
    method, path = _SELF_AUTHED
    assert (method, path) in _walk(app, _MUTATING), "the self-authed route left the walk"

    async with TestClient(TestServer(app)) as client:
        resp = await _drive(client, method, path, user=_NON_OWNER)
        assert resp.status != 403 or await resp.json() != _REFUSAL


async def test_reads_in_the_same_modules_stay_open(gate_recorder) -> None:
    """CONTROL: the gate covers verbs, not modules.

    A guard mounted over a module, or middleware on the registrar, would close the
    reads the dashboard renders from. Every route the walk finds is driven -- none
    is dropped -- and a read counts as still open when the gate stand-in records no
    call for it. Reading the recorder rather than the status is what makes this
    specific: a read could answer 403 for a reason of its own, and that must not be
    mistaken for the owner gate.

    Bodies do run here, by design. ``no_operator_config_and_no_spawn`` is what makes
    that safe.
    """
    app = _build_app()
    reads = _walk(app, frozenset({"GET"}))
    assert reads, "the read walk found nothing, so it proves nothing"

    async with TestClient(TestServer(app)) as client:
        gated = []
        for method, path in sorted(reads):
            before = len(gate_recorder)
            resp = await _drive(client, method, path, user=_NON_OWNER)
            reached_gate = len(gate_recorder) > before
            if reached_gate or (resp.status == 403 and await resp.json() == _REFUSAL):
                gated.append((method, path))
    # The instances control plane is owner-only by its own contract, so its reads
    # legitimately refuse; every other module's reads must not. Instances gates
    # inside ``_guard``, which the stand-in does not replace, so those surface by
    # status rather than through the recorder.
    unexpected = [r for r in gated if not r[1].startswith("/api/instances")]
    assert not unexpected, f"reads closed by the gate: {unexpected}"


async def test_the_probe_seal_is_load_bearing(no_operator_config_and_no_spawn) -> None:
    """The spawn seal is reached, so it is not decorative.

    ``GET /api/mcp`` and ``GET /api/mcp/probe`` arm a background re-probe whenever
    the probe cache is stale, and a fresh process is always stale, so driving them
    reaches the one seam that creates that task. A non-empty record proves the
    hazard this module seals is real and live: without the seal these same drives
    would spawn the operator's own MCP servers.
    """
    app = _build_app()
    async with TestClient(TestServer(app)) as client:
        for path in ("/api/mcp", "/api/mcp/probe"):
            await _drive(client, "GET", path, user=_NON_OWNER)

    assert no_operator_config_and_no_spawn, (
        "the re-probe seam was never reached, so the seal proves nothing -- check "
        "whether the stale-cache path still arms a background probe"
    )


async def test_every_mutating_route_reaches_the_gate_before_any_side_effect(
    gate_recorder,
) -> None:
    """Each mutating route calls the shared gate, and calls it FIRST.

    Driven through the recording stand-in rather than the real predicate, so no
    handler body runs: these bodies spawn MCP servers from operator configuration,
    uninstall packages and rewrite config files, and a route walk must not do that
    on anyone's machine.

    Stopping at the gate is also what proves placement. A body that ran first and
    only then consulted the gate would answer its own status here, not
    ``_GATE_REACHED``.
    """
    app = _build_app()
    mutating = _walk(app, _MUTATING) - {_SELF_AUTHED}
    shared = {
        (r.method, r.resource.canonical)
        for r in app.router.routes()
        if getattr(r.handler, "__module__", "") in _SHARED_GATE_MODULES
        and r.resource is not None
        and r.method in _MUTATING
    } - {_SELF_AUTHED}
    assert shared, "the shared-gate walk found nothing, so it proves nothing"

    async with TestClient(TestServer(app)) as client:
        missed = []
        for method, path in sorted(shared):
            resp = await _drive(client, method, path, user=_NON_OWNER)
            if resp.status != _GATE_REACHED:
                missed.append((method, path, resp.status))
        assert not missed, f"mutating routes that did not reach the gate first: {missed}"

    assert len(gate_recorder) == len(
        shared
    ), f"{len(shared)} routes driven but the gate recorded {len(gate_recorder)} call(s)"
    # The instances module is covered on its own seam, not here.
    assert mutating - shared, "the instances routes vanished from the mutating walk"


async def test_the_standalone_local_install_is_admitted_by_the_predicate() -> None:
    """CONTROL: the deployment an over-strict gate would break.

    With no owner configured the predicate admits the signed local bootstrap
    subjects, which is what a single-user gateway runs as. Asserted on the
    predicate itself rather than by driving routes, because a caller the gate
    ADMITS is exactly the caller whose handler body would then execute.
    """
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    for subject in ("local-app", "local-startup"):
        assert is_owner_dashboard_request(_req(subject, owner_id="")) is True, subject
    assert is_owner_dashboard_request(_req(_NON_OWNER, owner_id="")) is False
    assert is_owner_dashboard_request(_req(_OWNER, owner_id=_OWNER)) is True
    assert is_owner_dashboard_request(_req(_OWNER, owner_id="U0SOMEONEELSE")) is False


async def test_an_app_token_never_passes_the_gate() -> None:
    """An app token is not the owner, on every one of these routes.

    No shipped app manifest declares any of these paths, so this is a boundary
    rather than a behaviour change -- but it is the boundary that keeps a future
    manifest from silently inheriting the owner's authority.
    """
    app = _build_app()
    routes = sorted(_walk(app, _MUTATING) - {_SELF_AUTHED})

    async with TestClient(TestServer(app)) as client:
        admitted = []
        for method, path in routes:
            resp = await client.request(
                method,
                _concrete(path),
                json={},
                headers={"X-Test-User": _OWNER, "X-Test-App": "some-app"},
            )
            if resp.status != 403 or await resp.json() != _REFUSAL:
                admitted.append((method, path, resp.status))
        assert not admitted, f"an app token reached these routes: {admitted}"


def _req(user: str | None, *, owner_id: str = _OWNER, app_token: str = ""):
    """Request double carrying the claims ``_guard``'s predicate reads."""
    claims = {"app": app_token}
    if user is not None:
        claims["user"] = user

    class _Req:
        def __init__(self) -> None:
            self.app = {"state": _State(owner_id)}
            self.headers: dict[str, str] = {}
            self.match_info: dict[str, str] = {}
            self.query: dict[str, str] = {}

        def get(self, key, default=None):
            return claims.get(key, default)

        def __contains__(self, key):
            return key in claims

        def __getitem__(self, key):
            return claims[key]

    return _Req()


async def test_the_instances_guard_refuses_a_non_owner_and_admits_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_guard`` is the seam all 14 instances routes share.

    The routes hand back peer dashboard credentials minted with the OWNER's
    manager-held credential, which is why ``_guard``'s own docstring calls the
    control plane owner-only. Both directions are asserted on the seam itself,
    because the wildcard proxy route cannot be driven through a client without
    opening a peer socket.
    """
    monkeypatch.setattr(
        handlers_instances.KiroCrewConfig,
        "load",
        classmethod(lambda _cls: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )

    denied = handlers_instances._guard(_req(_NON_OWNER), "proxy")
    assert denied is not None, "_guard admitted an authenticated non-owner"
    assert denied.status == 403
    assert json.loads(denied.body) == _REFUSAL

    assert handlers_instances._guard(_req(_OWNER), "proxy") is None
    assert handlers_instances._guard(_req(_LOCAL_SUBJECT, owner_id=""), "proxy") is None
    assert handlers_instances._guard(_req(_OWNER, app_token="some-app"), "proxy") is not None


async def test_the_instances_guard_still_refuses_an_unauthenticated_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 401 branch keeps its own label.

    An absent subject and a non-owner subject are different failures, and the
    control plane reports them differently so a client can tell "sign in" from
    "not yours". Folding one into the other would lose that.
    """
    monkeypatch.setattr(
        handlers_instances.KiroCrewConfig,
        "load",
        classmethod(lambda _cls: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )

    denied = handlers_instances._guard(_req(None), "list")
    assert denied is not None
    assert denied.status == 401


async def test_every_instances_route_runs_the_guard() -> None:
    """A route added to the instances module later must not skip the seam.

    The count is read off the real registrar and compared with the number of
    ``_guard`` call sites in the module, so a fourteenth-plus route that forgets
    the gate fails here rather than shipping open.
    """
    import inspect

    app = _build_app()
    # Keyed on method AND path: four verbs share two resources here
    # (``/api/instances`` and ``/api/instances/{id}``), so a resource count would
    # under-report the surface by exactly those. HEAD is dropped because
    # ``add_get`` mints one per GET and it dispatches to the same handler, so
    # counting it would inflate the surface against a fixed call-site count.
    routes = {
        (r.method, r.resource.canonical)
        for r in app.router.routes()
        if getattr(r.handler, "__module__", "") == "kiro_crew.dashboard.handlers_instances"
        and r.resource is not None
        and r.method != "HEAD"
    }
    assert len(routes) >= 14, f"the instances walk found only {len(routes)} routes"

    source = inspect.getsource(handlers_instances)
    assert source.count("= _guard(request,") == len(routes), (
        f"{len(routes)} instances routes but "
        f"{source.count('= _guard(request,')} _guard call sites"
    )


async def test_the_internal_secret_caller_still_reaches_the_mcp_server_routes() -> None:
    """CONTROL: the one gated route whose designed caller is not the owner.

    ``/api/mcp/servers`` is listed in ``server._STRICT_INTERNAL_API_PATHS``, so its
    caller is an internal loopback process presenting ``X-Internal-Secret`` -- the
    App Kit SDK's ``register_mcp_server`` / ``remove_mcp_server`` in
    ``packages/kirocrew-client-py``. ``token_auth`` grants that request, marks it
    ``internal_auth`` and deliberately leaves ``request["app"]`` ABSENT, which the
    owner predicate reads as not-the-owner. An owner gate applied to it as well
    would answer 403 to the one caller the route exists for.

    A blank name is the first thing the handler body checks, so its own 400 is the
    proof the request got PAST the gate -- and it is also why this test writes
    nothing: no config file is touched on that branch.
    """

    @web.middleware
    async def _internal(request, handler):
        request["internal_auth"] = True
        return await handler(request)

    app = web.Application(middlewares=[_internal])
    app["state"] = _State()
    agent_config_routes.register(app)

    async with TestClient(TestServer(app)) as client:
        for method in ("PUT", "DELETE"):
            resp = await client.request(method, "/api/mcp/servers/%20", json={})
            assert resp.status == 400, f"{method} did not reach the body: {resp.status}"
            assert (await resp.json())["error"] == "server name is required"


async def test_the_mcp_server_routes_still_refuse_a_cookie_non_owner() -> None:
    """The other half of that scoping: a browser session must still be the owner.

    A ``local_only=False`` deployment reclassifies strict paths as mixed, so a
    cookie-authenticated session can arrive here, and a PUT writes a command later
    agent sessions execute. The route therefore refuses a non-owner cookie caller
    even though it admits the internal secret.
    """
    app = _build_app()
    async with TestClient(TestServer(app)) as client:
        for method in ("PUT", "DELETE"):
            resp = await _drive(client, method, "/api/mcp/servers/{name}", user=_NON_OWNER)
            assert resp.status == 403
            assert await resp.json() == _REFUSAL


async def test_no_other_gated_route_belongs_to_the_strict_internal_set() -> None:
    """The gate must not be applied to a route whose caller is a machine.

    A path in ``server._STRICT_INTERNAL_API_PATHS`` is authenticated by loopback
    plus ``X-Internal-Secret``, never by a dashboard cookie, so the owner predicate
    refuses its only legitimate caller. Two such paths touch these modules:
    ``/api/hooks/agent``, which keeps its own bearer check, and
    ``/api/mcp/servers``, whose gate is scoped to the cookie caller. The pair is
    asserted exactly -- a third one appearing later is a route that has to be
    scoped or excluded before the gate goes on it.
    """
    import re

    from kiro_crew.dashboard import server as server_mod

    with open(server_mod.__file__, encoding="utf-8") as fh:
        source = fh.read()
    start = source.index("_STRICT_INTERNAL_API_PATHS = frozenset(")
    end = source.index("\n)\n", start)
    strict = sorted(set(re.findall(r'"(/api/[^"]*)"', source[start:end])))
    assert "/api/mcp/servers" in strict, "the strict set no longer names the route this pins"

    app = _build_app()
    overlap = set()
    for method, path in _walk(app, _MUTATING | {_WILDCARD}):
        for entry in strict:
            if path == entry or path.startswith(entry.rstrip("/") + "/"):
                overlap.add((method, path))
    assert overlap == {
        ("POST", "/api/hooks/agent"),
        ("PUT", "/api/mcp/servers/{name}"),
        ("DELETE", "/api/mcp/servers/{name}"),
    }, f"strict-internal routes in these modules changed: {sorted(overlap)}"
