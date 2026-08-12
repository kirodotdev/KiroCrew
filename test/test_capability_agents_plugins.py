"""Tests for the agent-package + plugin ops on the ``CapabilityManager`` seam.

``list_agents()`` was READ-only, so an edition could SHOW installed agent packages
but not manage them — the dashboard had install/uninstall for MCP servers and
skills and a dead end for agents. These tests cover the ops that close that
asymmetry (`install_agent`/`uninstall_agent`) plus the plugin trio
(`list_plugins`/`plugins_out_of_sync`/`sync_plugins`), across three layers:

  * the ``Default`` adapter (public build → 503 / empty),
  * the ``BoundedCapabilityManager`` liveness wrapper (every new async op MUST be
    bounded — an unwrapped op is a silent hole in the guarantee), and
  * the ``/api/capability/*`` handlers (availability gating, input validation,
    cache invalidation, and the combined plugins response).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

import kiro_crew.platform.context as platform_context
from kiro_crew.dashboard.handlers import agents as agents_handler
from kiro_crew.platform.capability_bound import (
    CAPABILITY_INSTALL_TIMEOUT,
    CAPABILITY_READ_TIMEOUT,
    CAPABILITY_UNINSTALL_TIMEOUT,
    BoundedCapabilityManager,
)
from kiro_crew.platform.defaults import DefaultCapabilityManager
from kiro_crew.platform.interfaces import CapabilityResult

# Ops added alongside the original set, with the bound each must inherit.
_NEW_OPS = {
    "install_agent": CAPABILITY_INSTALL_TIMEOUT,
    "uninstall_agent": CAPABILITY_UNINSTALL_TIMEOUT,
    "list_plugins": CAPABILITY_READ_TIMEOUT,
    "plugins_out_of_sync": CAPABILITY_READ_TIMEOUT,
    "sync_plugins": CAPABILITY_INSTALL_TIMEOUT,
}


class TestDefaultAdapter:
    """The public build ships no package manager: mutations fail, reads are empty."""

    @pytest.mark.parametrize("op", ["install_agent", "uninstall_agent"])
    def test_agent_mutations_fail_closed(self, op):
        result = asyncio.run(getattr(DefaultCapabilityManager(), op)("SomePackage"))
        assert result.ok is False
        assert "not available" in result.message

    def test_sync_plugins_fails_closed(self):
        result = asyncio.run(DefaultCapabilityManager().sync_plugins())
        assert result.ok is False

    def test_plugin_reads_are_empty(self):
        mgr = DefaultCapabilityManager()
        assert asyncio.run(mgr.list_plugins()) == []
        # Empty drift genuinely means "in sync" — the correct answer for an
        # edition with no plugin concept, not a fail-closed error.
        assert asyncio.run(mgr.plugins_out_of_sync()) == []

    def test_default_satisfies_the_protocol(self):
        """Structural check: the Default must implement every new op."""
        for op in _NEW_OPS:
            assert callable(getattr(DefaultCapabilityManager(), op))


class TestLivenessBound:
    """Every new async op must be wrapped — an unbounded op can wedge the loop."""

    def test_every_new_op_is_bounded(self, monkeypatch):
        recorded: List[float] = []
        real_wait_for = asyncio.wait_for

        async def spy(awaitable, timeout):
            recorded.append(timeout)
            return await real_wait_for(awaitable, timeout)

        monkeypatch.setattr(asyncio, "wait_for", spy)

        bounded = BoundedCapabilityManager(DefaultCapabilityManager())
        for op, expected in _NEW_OPS.items():
            recorded.clear()
            method = getattr(bounded, op)
            # Mutations take a package argument; reads take none.
            if op in ("install_agent", "uninstall_agent"):
                asyncio.run(method("Pkg"))
            else:
                asyncio.run(method())
            assert recorded == [expected], f"{op} not bounded at {expected}s"

    def test_bounded_proxy_is_explicit_not_getattr(self):
        """The wrapper is an explicit proxy so a new Protocol op surfaces loudly.

        If someone adds an op to the Protocol and forgets the wrapper, calling it
        on the bounded manager must raise rather than silently bypass the bound.
        """
        bounded = BoundedCapabilityManager(DefaultCapabilityManager())
        with pytest.raises(AttributeError):
            bounded.some_future_op  # noqa: B018


class _FakeManager:
    """Minimal manager recording calls, for the handler tests."""

    def __init__(self, *, available: bool = True, ok: bool = True) -> None:
        self._available = available
        self._ok = ok
        self.calls: List[str] = []
        self.plugins: List[Dict[str, Any]] = [{"name": "p", "package": "Pkg"}]
        self.drift: List[str] = ["Pkg"]

    def available(self) -> bool:
        return self._available

    def _result(self) -> CapabilityResult:
        return CapabilityResult(ok=self._ok, message="" if self._ok else "boom")

    async def install_agent(self, package: str) -> CapabilityResult:
        self.calls.append(f"install:{package}")
        return self._result()

    async def uninstall_agent(self, package: str) -> CapabilityResult:
        self.calls.append(f"uninstall:{package}")
        return self._result()

    async def list_plugins(self) -> List[Dict[str, Any]]:
        self.calls.append("list_plugins")
        return self.plugins

    async def plugins_out_of_sync(self) -> List[str]:
        self.calls.append("plugins_out_of_sync")
        return self.drift

    async def sync_plugins(self) -> CapabilityResult:
        self.calls.append("sync_plugins")
        return self._result()


class _FakeRequest:
    """Stand-in for ``web.Request`` carrying a JSON body + app state."""

    def __init__(self, body: Any, state: Any) -> None:
        self._body = body
        self.app = {"state": state}

    async def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeState:
    def __init__(self) -> None:
        self.refreshed: List[str] = []

    def push_refresh(self, what: str) -> None:
        self.refreshed.append(what)


def _install_manager(monkeypatch, manager) -> None:
    class _Ctx:
        capability_manager = manager

    monkeypatch.setattr(platform_context, "current_context", lambda: _Ctx())


def _body(response) -> Any:
    import json

    return json.loads(response.body.decode())


@pytest.fixture()
def no_agent_rebuild(monkeypatch):
    """Stub the agent-config rebuild — it is filesystem-heavy and not under test."""
    rebuilds: List[bool] = []
    monkeypatch.setattr(
        "kiro_crew.agent.install_agent", lambda *a, **k: rebuilds.append(True)
    )
    cleared: List[bool] = []
    monkeypatch.setattr(
        agents_handler, "clear_list_agents_cache", lambda: cleared.append(True)
    )
    return rebuilds, cleared


class TestAgentPackageHandlers:
    def test_install_happy_path_rebuilds_and_invalidates(self, monkeypatch, no_agent_rebuild):
        rebuilds, cleared = no_agent_rebuild
        mgr = _FakeManager()
        _install_manager(monkeypatch, mgr)
        state = _FakeState()

        resp = asyncio.run(
            agents_handler.api_capability_agents_install(
                _FakeRequest({"package": "MyPkg"}, state)
            )
        )
        assert resp.status == 200
        assert _body(resp) == {"ok": True, "package": "MyPkg"}
        assert mgr.calls == ["install:MyPkg"]
        # An agent package carries skills/prompts too, so the config must be
        # rebuilt AND the mtime-signature cache invalidated.
        assert rebuilds == [True]
        assert cleared == [True]
        assert state.refreshed == ["agents"]

    def test_uninstall_calls_the_uninstall_op(self, monkeypatch, no_agent_rebuild):
        mgr = _FakeManager()
        _install_manager(monkeypatch, mgr)
        resp = asyncio.run(
            agents_handler.api_capability_agents_uninstall(
                _FakeRequest({"package": "MyPkg"}, _FakeState())
            )
        )
        assert resp.status == 200
        assert mgr.calls == ["uninstall:MyPkg"]

    def test_503_when_manager_unavailable(self, monkeypatch):
        mgr = _FakeManager(available=False)
        _install_manager(monkeypatch, mgr)
        resp = asyncio.run(
            agents_handler.api_capability_agents_install(
                _FakeRequest({"package": "MyPkg"}, _FakeState())
            )
        )
        assert resp.status == 503
        assert mgr.calls == []  # gated before the seam op

    @pytest.mark.parametrize("body", [{}, {"package": ""}, {"package": "   "}])
    def test_400_without_a_package(self, monkeypatch, body):
        mgr = _FakeManager()
        _install_manager(monkeypatch, mgr)
        resp = asyncio.run(
            agents_handler.api_capability_agents_install(_FakeRequest(body, _FakeState()))
        )
        assert resp.status == 400
        assert mgr.calls == []

    @pytest.mark.parametrize(
        "bad",
        [
            "--force",
            "-o",
            "../etc/passwd",
            "a/../b",
            "a b",
            "a;b",
            "a|b",
            "$(x)",
            "a\nb",
            "x" * 201,
            # A leading "@" is allowed for scoped ids, but must be followed by an
            # alphanumeric: "@-evil" would hand "-evil" to an installer that
            # strips the scope prefix.
            "@-evil",
            "@",
        ],
    )
    def test_hostile_package_names_rejected_before_the_seam(self, monkeypatch, bad):
        """The name crosses into an edition that owns its own invocation grammar.

        Bound it in core rather than trusting every edition to reject a traversal
        or a flag — the same guard ``mcp_discover`` applies to ``server_id``.
        """
        mgr = _FakeManager()
        _install_manager(monkeypatch, mgr)
        resp = asyncio.run(
            agents_handler.api_capability_agents_install(
                _FakeRequest({"package": bad}, _FakeState())
            )
        )
        assert resp.status == 400
        assert mgr.calls == [], "hostile name reached the seam op"

    @pytest.mark.parametrize(
        "good",
        [
            "AlphaCaps",
            "Pkg-1.0",
            "npm:@scope/pkg",
            # A BARE scoped npm id — the shape the charset comment promises to
            # admit. A leading-alphanumeric-only anchor rejected it.
            "@scope/pkg",
            "pkg/skill",
            "a.b_c",
        ],
    )
    def test_legitimate_package_names_accepted(self, monkeypatch, good, no_agent_rebuild):
        mgr = _FakeManager()
        _install_manager(monkeypatch, mgr)
        resp = asyncio.run(
            agents_handler.api_capability_agents_install(
                _FakeRequest({"package": good}, _FakeState())
            )
        )
        assert resp.status == 200, f"{good} rejected"

    @pytest.mark.parametrize("body", [[1, 2], "x", 42, {"package": 123}, {"package": {}}])
    def test_non_object_or_non_string_body_is_400_not_500(self, monkeypatch, body):
        """Valid JSON that is not an object must not raise AttributeError -> 500."""
        _install_manager(monkeypatch, _FakeManager())
        resp = asyncio.run(
            agents_handler.api_capability_agents_install(_FakeRequest(body, _FakeState()))
        )
        assert resp.status == 400

    def test_failure_message_is_redacted_and_bounded(self, monkeypatch):
        """A manager message is edition subprocess output — redact before the UI."""

        class _Leaky(_FakeManager):
            async def install_agent(self, package):
                return CapabilityResult(
                    ok=False,
                    message="https://registry.example.com/x?token=SECRETVALUE " + "z" * 900,
                )

        _install_manager(monkeypatch, _Leaky())
        resp = asyncio.run(
            agents_handler.api_capability_agents_install(
                _FakeRequest({"package": "Pkg"}, _FakeState())
            )
        )
        assert resp.status == 500
        error = _body(resp)["error"]
        assert "SECRETVALUE" not in error
        assert len(error) <= 600  # bounded (500 + redaction markup headroom)

    def test_sync_success_message_is_redacted_and_bounded(self, monkeypatch):
        """The success path carries a message too — same treatment as failure."""

        class _Leaky(_FakeManager):
            async def sync_plugins(self):
                return CapabilityResult(
                    ok=True,
                    message="installed via https://r.example.com/a?api_key=SECRETVALUE "
                    + "z" * 900,
                )

        _install_manager(monkeypatch, _Leaky())
        resp = asyncio.run(
            agents_handler.api_capability_plugins_sync(_FakeRequest({}, _FakeState()))
        )
        assert resp.status == 200
        message = _body(resp)["message"]
        assert "SECRETVALUE" not in message
        assert len(message) <= 600

    def test_audit_names_the_package(self, monkeypatch, no_agent_rebuild):
        """The SEL middleware logs only the path; the package name is the one
        fact an incident responder needs, so it gets an explicit line."""
        emitted: List[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                emitted.append(kw)

        monkeypatch.setattr(agents_handler, "_sel", lambda: _Sel())
        _install_manager(monkeypatch, _FakeManager())
        asyncio.run(
            agents_handler.api_capability_agents_install(
                _FakeRequest({"package": "AlphaCaps"}, _FakeState())
            )
        )
        assert any(
            e.get("resources") == "capability:AlphaCaps"
            and e.get("operation") == "capability_agent_install"
            and e.get("outcome") == "ok"
            for e in emitted
        ), emitted

    def test_400_on_invalid_json(self, monkeypatch):
        _install_manager(monkeypatch, _FakeManager())
        resp = asyncio.run(
            agents_handler.api_capability_agents_install(
                _FakeRequest(ValueError("bad json"), _FakeState())
            )
        )
        assert resp.status == 400

    def test_500_when_the_op_fails_and_no_rebuild_runs(self, monkeypatch, no_agent_rebuild):
        rebuilds, cleared = no_agent_rebuild
        mgr = _FakeManager(ok=False)
        _install_manager(monkeypatch, mgr)
        resp = asyncio.run(
            agents_handler.api_capability_agents_install(
                _FakeRequest({"package": "MyPkg"}, _FakeState())
            )
        )
        assert resp.status == 500
        assert "boom" in _body(resp)["error"]
        # A failed install must not trigger a rebuild or cache clear.
        assert rebuilds == [] and cleared == []


class TestPluginHandlers:
    def test_list_returns_rows_and_drift_together(self, monkeypatch):
        mgr = _FakeManager()
        _install_manager(monkeypatch, mgr)
        resp = asyncio.run(
            agents_handler.api_capability_plugins_list(_FakeRequest({}, _FakeState()))
        )
        assert resp.status == 200
        payload = _body(resp)
        # One response, so the UI cannot show a list and a reconcile affordance
        # that disagree mid-install.
        assert payload["plugins"] == mgr.plugins
        assert payload["out_of_sync"] == mgr.drift
        assert mgr.calls == ["list_plugins", "plugins_out_of_sync"]

    def test_reads_run_concurrently_not_sequentially(self, monkeypatch):
        """The two independent reads must be gathered, not awaited in sequence.

        Each carries its own CAPABILITY_READ_TIMEOUT, and that bound is sized
        tight because the dashboard POLLS this endpoint — sequential awaits would
        let one request pend for twice the designed budget and accumulate pending
        gateway tasks per poll. Proven by observing overlap: the second read
        starts before the first finishes.
        """
        order: List[str] = []

        class _Timed(_FakeManager):
            async def list_plugins(self):
                order.append("plugins:start")
                await asyncio.sleep(0.05)
                order.append("plugins:end")
                return self.plugins

            async def plugins_out_of_sync(self):
                order.append("drift:start")
                await asyncio.sleep(0.05)
                order.append("drift:end")
                return self.drift

        _install_manager(monkeypatch, _Timed())
        resp = asyncio.run(
            agents_handler.api_capability_plugins_list(_FakeRequest({}, _FakeState()))
        )
        assert resp.status == 200
        # Sequential would be start,end,start,end. Concurrent interleaves.
        assert order.index("drift:start") < order.index("plugins:end"), (
            f"reads did not overlap: {order}"
        )

    def test_list_503_when_unavailable(self, monkeypatch):
        _install_manager(monkeypatch, _FakeManager(available=False))
        resp = asyncio.run(
            agents_handler.api_capability_plugins_list(_FakeRequest({}, _FakeState()))
        )
        assert resp.status == 503

    def test_sync_happy_path_pushes_refresh(self, monkeypatch):
        mgr = _FakeManager()
        _install_manager(monkeypatch, mgr)
        state = _FakeState()
        resp = asyncio.run(
            agents_handler.api_capability_plugins_sync(_FakeRequest({}, state))
        )
        assert resp.status == 200
        assert _body(resp)["ok"] is True
        assert mgr.calls == ["sync_plugins"]
        assert state.refreshed == ["agents"]

    def test_sync_500_on_failure(self, monkeypatch):
        _install_manager(monkeypatch, _FakeManager(ok=False))
        resp = asyncio.run(
            agents_handler.api_capability_plugins_sync(_FakeRequest({}, _FakeState()))
        )
        assert resp.status == 500

    def test_sync_503_when_unavailable(self, monkeypatch):
        mgr = _FakeManager(available=False)
        _install_manager(monkeypatch, mgr)
        resp = asyncio.run(
            agents_handler.api_capability_plugins_sync(_FakeRequest({}, _FakeState()))
        )
        assert resp.status == 503
        assert mgr.calls == []


class TestRedactExternalLayerOrder:
    """The lexical URL-parameter scrub must run LAST, not first.

    ``redact_exfiltration_urls`` classifies a URL as suspicious partly by query
    LENGTH and replaces the WHOLE url when it fires. Running the scrub first
    shortens ``?token=<210 chars>&host=…&path=…`` below that threshold, so the
    exfil scan stops firing and every other parameter — the actual payload, which
    the regex does not name — renders verbatim in the dashboard. That silently
    REMOVES a pre-existing guard, so both directions are pinned here.
    """

    _EXFIL = (
        "install failed: fetching https://collect.attacker.example/?token="
        + "A" * 210
        + "&host=corp-laptop&user=alice&path=/home/alice/.aws/credentials"
    )

    def test_long_query_exfil_url_is_still_fully_redacted(self):
        from kiro_crew.dashboard.handlers.discover import _redact_external

        out = _redact_external(self._EXFIL)
        # The whole-URL redaction must still fire...
        assert "collect.attacker.example" not in out or "[REDACTED" in out
        # ...and the non-token parameters must not survive.
        assert "corp-laptop" not in out
        assert "/home/alice/.aws/credentials" not in out

    def test_short_token_is_still_scrubbed(self):
        """The new win must survive the reorder."""
        from kiro_crew.dashboard.handlers.discover import _redact_external

        out = _redact_external("https://r.example.com/x?api_key=abc123&keep=1")
        assert "abc123" not in out
        assert "keep=1" in out  # a scrub, not a blanket drop

    def test_credential_shapes_still_caught(self):
        from kiro_crew.dashboard.handlers.discover import _redact_external

        assert "AKIAIOSFODNN7EXAMPLE" not in _redact_external("AKIAIOSFODNN7EXAMPLE")

    def test_benign_text_passes_through(self):
        from kiro_crew.dashboard.handlers.discover import _redact_external

        benign = "installed 3 packages from https://registry.example.com/v2"
        assert _redact_external(benign) == benign

    def test_already_redacted_label_is_not_re_mangled(self):
        """A credential shape inside a named param keeps the core's own label.

        `redact_credentials` turns `?token=AKIA…` into `?token=[REDACTED:
        credential]`; without a negative lookahead the scrub re-matches that value
        (its class stops at the space) and leaves `[REDACTED] credential]`. The
        secret is removed either way — this keeps the message readable.
        """
        from kiro_crew.dashboard.handlers.discover import _redact_external

        out = _redact_external("?token=AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert out == "?token=[REDACTED: credential]"

    def test_redaction_is_idempotent(self):
        """Re-redacting an already-redacted string must be a no-op."""
        from kiro_crew.dashboard.handlers.discover import _redact_external

        once = _redact_external("?token=AKIAIOSFODNN7EXAMPLE&api_key=abc123")
        assert _redact_external(once) == once


class TestUnexpectedErrorsAreSanitized:
    """A seam op raising must yield a generic 500 — never raw exception text.

    Browser-facing 5xx bodies must not echo backend exception detail (CWE-209);
    the handler returns a correlation id and logs the traceback server-side.
    """

    class _Exploding(_FakeManager):
        async def install_agent(self, package: str) -> CapabilityResult:
            raise RuntimeError("secret-internal-path /etc/shadow")

        async def list_plugins(self) -> List[Dict[str, Any]]:
            raise RuntimeError("secret-internal-path /etc/shadow")

        async def sync_plugins(self) -> CapabilityResult:
            raise RuntimeError("secret-internal-path /etc/shadow")

    @pytest.mark.parametrize(
        "handler,body",
        [
            ("api_capability_agents_install", {"package": "Pkg"}),
            ("api_capability_plugins_list", {}),
            ("api_capability_plugins_sync", {}),
        ],
    )
    def test_exception_yields_sanitized_500(self, monkeypatch, handler, body):
        _install_manager(monkeypatch, self._Exploding())
        resp = asyncio.run(
            getattr(agents_handler, handler)(_FakeRequest(body, _FakeState()))
        )
        assert resp.status == 500
        payload = _body(resp)
        assert payload["error"] == "internal error"
        assert "id" in payload  # correlation id for the server-side log
        assert "secret-internal-path" not in resp.body.decode()


class TestRouteRegistration:
    """The ops are only reachable if the routes exist and are wired to handlers."""

    @staticmethod
    def _registered_routes() -> set:
        """``(verb, path, handler_attr)`` triples parsed out of the route table.

        The registrations live in ``dashboard/routes/`` (moved out of
        ``async def start_dashboard``, which cannot be invoked from a unit test
        because it binds a port and starts services), so the AST is what is
        reachable here. This is still strictly stronger than a substring scan: it
        pins the HTTP verb and the handler attribute, so a GET registered as POST,
        or a route wired to the wrong handler, both fail.

        Both ``server`` and every slice module are scanned, so the assertions hold
        wherever a route lives and keep holding if it moves between slices.
        """
        import ast
        import importlib
        import inspect

        from kiro_crew.dashboard import routes as routes_pkg
        from kiro_crew.dashboard import server as core_server

        sources = [inspect.getsource(core_server)]
        for name in routes_pkg.REGISTRAR_NAMES:
            sources.append(
                inspect.getsource(importlib.import_module(f"kiro_crew.dashboard.routes.{name}"))
            )

        found = set()
        for src in sources:
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if not node.func.attr.startswith("add_"):
                    continue
                if len(node.args) < 2 or not isinstance(node.args[0], ast.Constant):
                    continue
                handler = node.args[1]
                if not isinstance(handler, ast.Attribute):
                    continue
                found.add(
                    (node.func.attr.removeprefix("add_").upper(), node.args[0].value, handler.attr)
                )
        return found

    def test_new_routes_are_registered_with_verb_and_handler(self):
        """A substring scan of the source could not prove this.

        ``"/api/capability/plugins" in src`` is satisfied by the LONGER string
        ``"/api/capability/plugins/sync"``, so deleting the GET registration
        entirely would leave such a test green while the endpoint 404s.
        """
        routes = self._registered_routes()
        for verb, path, handler in (
            ("POST", "/api/capability/agents/install", "api_capability_agents_install"),
            ("POST", "/api/capability/agents/uninstall", "api_capability_agents_uninstall"),
            ("GET", "/api/capability/plugins", "api_capability_plugins_list"),
            ("POST", "/api/capability/plugins/sync", "api_capability_plugins_sync"),
        ):
            assert (verb, path, handler) in routes, f"{verb} {path} -> {handler} missing"

    def test_the_plugins_list_route_is_distinct_from_sync(self):
        """Guards the exact substring-shadowing hole described above."""
        routes = self._registered_routes()
        paths = {path for _verb, path, _h in routes}
        assert "/api/capability/plugins" in paths
        assert "/api/capability/plugins/sync" in paths

    def test_handlers_are_exported(self):
        from kiro_crew.dashboard import handlers

        for name in (
            "api_capability_agents_install",
            "api_capability_agents_uninstall",
            "api_capability_plugins_list",
            "api_capability_plugins_sync",
        ):
            assert callable(getattr(handlers, name))
