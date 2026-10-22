"""Session-control HTTP routes and the ``agent.session_control`` config switch.

Covers ``api_session_control_*`` handlers driven with a stubbed request: create,
stop, close, send and read refuse a call without the internal secret with 403 and
reach the operation with it, and a ``SessionControlError`` renders as its own
status and code. The send route's ``message`` and ``steer`` checks are pinned too.

The switch defaults on, an explicit or quoted ``false`` withdraws it, and a config
load that raises disables it. Also checks that the registered session-control
routes sit in ``_STRICT_INTERNAL_API_PATHS``, and that ``server.py`` and its
``server_runtime`` owners import no session-control handler at module level.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _REAL_ENABLED,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _peer_target,
    _slot,
)

from kiro_crew.config import loader
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.handlers import session_control as handlers_sc

# ── Route authentication ─────────────────────────────────────────────────────


class TestTheRoutesRequireTheInternalSecret:
    """Strict-internal is not self-enforcing at the handler.

    With ``X-Internal-Secret`` ABSENT the middleware falls through to cookie
    auth, and a ``local_only=False`` deployment reclassifies strict paths as
    mixed. Since these routes authorize on the ``X-Session-Key`` the caller
    sends, a browser holding only a dashboard cookie could otherwise act AS any
    of the user's sessions. Mutation guard: dropping the ``internal_auth`` check
    makes every case below reach the operation.
    """

    def _request(self, tmp_path, *, internal: bool, path: str, method: str = "POST"):
        from unittest.mock import MagicMock

        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _slot(state, "chat-2")
        request = MagicMock()
        request.app = {"state": state}
        request.path = path
        request.method = method
        request.headers = {"X-Session-Key": _key(caller)}
        request.query = {"target": "chat-2"}
        # Both marks together are what an internal caller on the unix socket
        # arrives with: the secret admits it, and the kernel peer attestation is
        # what lets a session-scoped route act on the key it declares.
        request.get = lambda key, default=None: (
            True if (key in ("internal_auth", "peer_verified") and internal) else default
        )

        async def _json():
            return {"target": "chat-2", "message": "hi"}

        request.json = _json
        return request

    def _body(self, response):
        import json

        return json.loads(response.body.decode())

    def test_create_without_the_secret_is_forbidden(self, tmp_path):
        req = self._request(tmp_path, internal=False, path="/api/session-control/create")
        resp = asyncio.run(handlers_sc.api_session_control_create(req))
        assert resp.status == 403
        assert self._body(resp)["code"] == "internal_secret_required"

    def test_create_returns_a_session_it_actually_opened(self, tmp_path):
        """The create ROUTE needs its own test: a handler-only defect ships it dead.

        An earlier round proved that once -- the route was missing from the strict
        internal path list, so every call refused while the handler-level tests
        still passed.
        """
        req = self._request(tmp_path, internal=True, path="/api/session-control/create")

        async def _json():
            return {"title": "worker"}

        req.json = _json
        resp = asyncio.run(handlers_sc.api_session_control_create(req))

        assert resp.status == 200
        payload = self._body(resp)
        assert payload["ok"] is True
        state = req.app["state"]
        created = state.get_slot(payload["target"])
        assert created is not None, "the route reported a target it did not create"
        assert created.title == "worker"

    def test_create_route_forwards_the_model(self, tmp_path):
        """A route dropping model pins nothing while handler tests stay green."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/create")

        async def _json():
            return {"title": "worker", "model": "claude-sonnet-4.6"}

        req.json = _json
        resp = asyncio.run(handlers_sc.api_session_control_create(req))

        assert resp.status == 200
        payload = self._body(resp)
        assert payload["model"] == "claude-sonnet-4.6"
        created = req.app["state"].get_slot(payload["target"])
        assert created is not None
        assert created.model == "claude-sonnet-4.6"

    def test_create_route_refuses_a_bad_charset_model(self, tmp_path):
        """The route runs no schema, so create's own model bound must surface as a refusal."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/create")
        before = req.app["state"].live_slot_count()

        async def _json():
            return {"title": "worker", "model": "bad id"}

        req.json = _json
        resp = asyncio.run(handlers_sc.api_session_control_create(req))

        assert resp.status != 200
        assert resp.status < 500, "a refusal must not render as a server error"
        assert self._body(resp)["code"] == "model_rejected"
        assert req.app["state"].live_slot_count() == before

    def test_create_renders_a_refusal_as_its_status_not_a_500(self, tmp_path, monkeypatch):
        """A SessionControlError from create must come back as its own refusal."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/create")

        async def _boom(*_a, **_kw):
            raise sc.SessionControlError("nope", status=429, code="slot_cap_reached")

        monkeypatch.setattr(sc, "create_session", _boom)
        resp = asyncio.run(handlers_sc.api_session_control_create(req))

        assert resp.status == 429
        assert self._body(resp)["code"] == "slot_cap_reached"

    def test_stop_without_the_secret_is_forbidden(self, tmp_path):
        req = self._request(tmp_path, internal=False, path="/api/session-control/stop")
        resp = asyncio.run(handlers_sc.api_session_control_stop(req))
        assert resp.status == 403

    def test_a_failing_audit_still_refuses_rather_than_erroring(self, tmp_path, monkeypatch):
        """Auditing the refusal must not be able to turn it into a 500.

        The first `sel()` of a process CONSTRUCTS the log, and construction can
        raise (a trust root too short to sign the chain). An unguarded write would
        lose the denial in order to report it -- the caller would see a server
        error where it should see a clean 403.
        """

        def _boom():
            raise RuntimeError("trust root too short")

        monkeypatch.setattr(handlers_sc, "sel", _boom)
        req = self._request(tmp_path, internal=False, path="/api/session-control/stop")
        resp = asyncio.run(handlers_sc.api_session_control_stop(req))
        assert resp.status == 403, "a broken audit must not escalate a refusal to 500"
        assert self._body(resp)["code"] == "internal_secret_required"

    def test_stop_with_the_secret_reaches_the_operation(self, tmp_path, monkeypatch):
        """The stop ROUTE's success path, for the reason create's docstring gives:
        only the 403 arm was covered, so a route-level defect on the reaching path
        would ship dead."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/stop")

        async def _ok(*_a, **_kw):
            return {"ok": True, "target": "chat-2", "stopped": True}

        monkeypatch.setattr(sc, "stop_target", _ok)
        resp = asyncio.run(handlers_sc.api_session_control_stop(req))

        assert resp.status == 200
        assert self._body(resp)["target"] == "chat-2"

    def test_stop_renders_a_refusal_as_its_status_not_a_500(self, tmp_path, monkeypatch):
        """Same refusal contract the create and send routes hold."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/stop")

        async def _boom(*_a, **_kw):
            raise sc.SessionControlError(
                "not addressable", status=403, code="linked_session_target"
            )

        monkeypatch.setattr(sc, "stop_target", _boom)
        resp = asyncio.run(handlers_sc.api_session_control_stop(req))

        assert resp.status == 403
        assert self._body(resp)["code"] == "linked_session_target"

    def test_close_without_the_secret_is_forbidden(self, tmp_path):
        req = self._request(tmp_path, internal=False, path="/api/session-control/close")
        resp = asyncio.run(handlers_sc.api_session_control_close(req))
        assert resp.status == 403
        assert self._body(resp)["code"] == "internal_secret_required"

    def test_close_with_the_secret_reaches_the_operation(self, tmp_path, monkeypatch):
        """The close ROUTE's success path, for the reason create's docstring gives:
        a handler wired only at the business layer ships dead if the route itself
        refuses or never reaches the operation."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/close")

        async def _ok(*_a, **_kw):
            return {"ok": True, "target": "chat-2"}

        monkeypatch.setattr(sc, "close_target", _ok)
        resp = asyncio.run(handlers_sc.api_session_control_close(req))

        assert resp.status == 200
        assert self._body(resp)["target"] == "chat-2"

    def test_close_renders_a_refusal_as_its_status_not_a_500(self, tmp_path, monkeypatch):
        """Same refusal contract the other routes hold — including the close-path
        failure codes, which arrive as their own 500 rather than an unhandled crash."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/close")

        async def _boom(*_a, **_kw):
            raise sc.SessionControlError(
                "failed to save history", status=500, code="history_save_failed"
            )

        monkeypatch.setattr(sc, "close_target", _boom)
        resp = asyncio.run(handlers_sc.api_session_control_close(req))

        assert resp.status == 500
        assert self._body(resp)["code"] == "history_save_failed"

    @pytest.mark.parametrize("code", ["reopen_failed", "reopen_rollback_failed"])
    def test_revive_keeps_its_503_instead_of_degrading_to_400(self, tmp_path, monkeypatch, code):
        """`revive_session` promises 503 for a reopen write that could not land and
        for a refused resume whose closed marker could not be confirmed restored;
        the route must forward that status, since 400 would tell the caller it
        sent a bad request when the remedy is to retry or re-close."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/revive")

        async def _boom(*_a, **_kw):
            raise sc.SessionControlError("try again", status=503, code=code)

        monkeypatch.setattr(sc, "revive_session", _boom)
        resp = asyncio.run(handlers_sc.api_session_control_revive(req))

        assert resp.status == 503
        assert self._body(resp)["code"] == code

    def test_send_without_the_secret_is_forbidden(self, tmp_path):
        req = self._request(tmp_path, internal=False, path="/api/session-control/send")
        resp = asyncio.run(handlers_sc.api_session_control_send(req))
        assert resp.status == 403
        assert self._body(resp)["code"] == "internal_secret_required"

    def test_send_with_the_secret_delivers_to_the_target(self, tmp_path, monkeypatch):
        """The send ROUTE needs its own test for the reason create's docstring gives:
        a handler wired only in tests at the business layer ships dead if the route
        itself refuses or never reaches the operation."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/send")
        state = req.app["state"]
        caller = state.get_slot("chat-1")
        assert caller is not None
        # Make chat-2 an addressable peer of the caller through the same helper the
        # business-layer tests use, so this exercises the route rather than a
        # hand-built slot that authorize_target would refuse for unrelated reasons.
        _peer_target(state, "chat-2", caller)

        async def _fake_run_chat(_state, _slot, _prompt):
            return None

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)

        resp = asyncio.run(handlers_sc.api_session_control_send(req))

        assert resp.status == 200, self._body(resp)
        payload = self._body(resp)
        assert payload["ok"] is True
        assert payload["target"] == "chat-2"

    def test_send_hands_its_admission_fence_to_the_own_worker_revive(self, tmp_path, monkeypatch):
        """The send route's implicit revive must carry the fence admission settled,
        as every other route does; without it the revive re-derives the fence from
        config that may have moved while the body read suspended."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/send")
        seen: dict = {}

        async def _revive(_state, _caller, target, *, caller_fenced=None):
            seen["caller_fenced"] = caller_fenced
            return target

        async def _send(_state, **_kw):
            return {"ok": True, "target": "chat-2"}

        monkeypatch.setattr(handlers_sc, "_carried_fence", lambda _request: True)
        monkeypatch.setattr(sc, "revive_own_archived_worker", _revive)
        monkeypatch.setattr(sc, "send_to_target", _send)

        resp = asyncio.run(handlers_sc.api_session_control_send(req))

        assert resp.status == 200, self._body(resp)
        assert seen == {"caller_fenced": True}

    def test_send_requires_a_non_empty_message(self, tmp_path):
        """The message check lives in the ROUTE, not the business layer, so a
        whitespace-only body must be refused here rather than delivered as a
        blank turn."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/send")

        async def _json():
            return {"target": "chat-2", "message": "   "}

        req.json = _json
        resp = asyncio.run(handlers_sc.api_session_control_send(req))

        assert resp.status == 400
        assert self._body(resp)["code"] == "message_required"

    def test_send_rejects_a_steer_that_is_not_a_boolean(self, tmp_path):
        """`steer` decides whether the message cuts into a running turn, so the
        route type-checks it instead of reading truthiness: the string "false" is
        truthy, and coercing it would interrupt a turn for a caller that asked for
        the queue."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/send")

        async def _json():
            return {"target": "chat-2", "message": "hello", "steer": "false"}

        req.json = _json
        resp = asyncio.run(handlers_sc.api_session_control_send(req))

        assert resp.status == 400
        assert self._body(resp)["code"] == "invalid_steer"

    def test_send_renders_a_refusal_as_its_status_not_a_500(self, tmp_path, monkeypatch):
        """A SessionControlError from send comes back as its own refusal, the same
        contract create's route holds."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/send")

        async def _boom(*_a, **_kw):
            raise sc.SessionControlError("busy", status=409, code="target_busy")

        monkeypatch.setattr(sc, "send_to_target", _boom)
        resp = asyncio.run(handlers_sc.api_session_control_send(req))

        assert resp.status == 409
        assert self._body(resp)["code"] == "target_busy"

    def test_read_without_the_secret_is_forbidden(self, tmp_path):
        req = self._request(
            tmp_path, internal=False, path="/api/session-control/read", method="GET"
        )
        resp = asyncio.run(handlers_sc.api_session_control_read(req))
        assert resp.status == 403

    def test_read_with_the_secret_reaches_the_operation(self, tmp_path):
        """The guard must not refuse an authentic caller."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/read", method="GET")
        resp = asyncio.run(handlers_sc.api_session_control_read(req))
        assert resp.status == 200
        assert self._body(resp)["target"] == "chat-2"


# ── The config switch ────────────────────────────────────────────────────────


_ABSENT = object()


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        pytest.param(_ABSENT, True, id="absent-the-mount-is-the-grant"),
        pytest.param(True, True, id="explicit-true"),
        pytest.param(False, False, id="explicit-false-withdraws"),
        pytest.param("false", False, id="quoted-false-withdraws"),
        pytest.param(0, False, id="number-withdraws"),
    ],
)
def test_the_switch_is_on_by_default_and_an_explicit_false_still_disables(
    tmp_path, monkeypatch, stored, expected
):
    """``agent.session_control`` defaults ON; the agent config is the real grant.

    Who may reach a peer session is decided by the AGENT CONFIG, not by this
    switch: the tools come from the `kirocrew-dashboard` MCP server, so an agent
    that does not mount it never has them -- the same rule as every other MCP
    server. A second default-off gate on top of that only made the capability
    unreachable for an agent whose spec had already been given it deliberately.

    What the switch is still for is a single withdrawal: an operator who wants it
    gone from every agent at once, without editing each spec. So the one direction
    that must keep working is an EXPLICIT opt-out, including the quoted ``"false"``
    an editor that quotes values writes: ``bool("false")`` is ``True``, so a plain
    coercion would keep cross-session control on while the operator believes it off.

    Driven through the load pipeline over a document in ``tmp_path``: the document
    read normalizes a present non-bool to the safe ``False``, and the build reads
    an absent key as the field default.
    """
    path = tmp_path / "config.json"
    agent = {} if stored is _ABSENT else {"session_control": stored}
    path.write_text(json.dumps({"agent": agent}), encoding="utf-8")
    monkeypatch.setattr(loader, "config_path", lambda: path)
    monkeypatch.setattr(loader, "config_local_path", lambda: tmp_path / "config.local.json")
    monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)

    cfg = loader.build_config(loader.read_config_document())

    assert cfg.agent.session_control is expected


def test_the_switch_defaults_on_wherever_a_config_is_built_without_the_key(tmp_path):
    """A config built with no file, or from a value that is not a real bool, is ON.

    The no-config build takes the field default. A value that reaches the build
    without being a real bool (the document read normally repairs it first) reads
    as that default rather than through truthiness; a real ``false`` withdraws.
    """

    def document(data, loaded=True):
        return loader.ConfigDocument(
            ticket=0, path=tmp_path / "config.json", data=data, loaded=loaded, content_digest=None
        )

    assert loader.build_config(document({}, loaded=False)).agent.session_control is True
    none = loader.build_config(document({"agent": {"session_control": None}}))
    assert none.agent.session_control is True
    off = loader.build_config(document({"agent": {"session_control": False}}))
    assert off.agent.session_control is False


def test_a_config_read_that_raises_disables_the_feature(monkeypatch):
    """Unrelated config corruption must not undo an explicit opt-out.

    Mutation guard: returning True here means a malformed section elsewhere in
    config.json silently re-enables cross-session control.
    """

    class _Exploding:
        @staticmethod
        def load():
            raise ValueError("malformed knowledge.auto_ingest_artifact_kinds")

    monkeypatch.setattr(sc, "KiroCrewConfig", _Exploding)
    assert _REAL_ENABLED() is False


def test_safe_bool_rejects_every_non_bool():
    """The helper the wiring above depends on: only a real bool survives."""
    for bad in ("false", "true", "yes", 1, 0, [], {}, None):
        assert loader._safe_bool(bad, False) is False, bad
    assert loader._safe_bool(True, False) is True
    assert loader._safe_bool(False, True) is False


def test_session_control_routes_are_strict_internal():
    """Every registered session-control route must sit in the strict bucket.

    The route table and `_STRICT_INTERNAL_API_PATHS` are two hand-maintained
    lists, and nothing else pairs them. An unlisted path falls through
    `token_auth`'s general branch, which honors only cookie/query tokens, so the
    MCP caller's `X-Internal-Secret` is ignored and the handler's own
    `internal_auth` re-assert refuses the call -- the tool is unreachable in
    production. The handler-level suites cannot catch that: they stub
    `request["internal_auth"]` and call handlers directly, bypassing the
    middleware entirely, so they stay green while the route is dead.

    Derived from the router rather than a literal list, so a fifth route fails
    here instead of shipping unreachable.
    """
    from aiohttp import web

    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS, _register_mcp_routes

    app = web.Application()
    _register_mcp_routes(app)

    registered = {
        resource.canonical
        for resource in app.router.resources()
        if str(getattr(resource, "canonical", "")).startswith("/api/session-control/")
    }
    assert registered, "no session-control routes found -- the derivation broke, not the wiring"

    missing = sorted(registered - set(_STRICT_INTERNAL_API_PATHS))
    assert not missing, (
        "session-control routes registered but absent from _STRICT_INTERNAL_API_PATHS "
        f"(unreachable in production, X-Internal-Secret ignored): {missing}"
    )


def test_session_control_is_not_imported_on_the_gateway_boot_path():
    """A feature-flagged subsystem must not be eagerly imported by `server.py`.

    The enabled check (`session_control_enabled`) lives inside the handlers, so a
    module-level import in `server.py` is an import whose gate runs after it --
    an operator who set `agent.session_control=false` would still pay to load the
    subsystem on every launch. Route registration stays at boot; only the import
    is deferred to first request.

    Mutation guard: restoring the module-level import fails this.
    """
    from pathlib import Path

    from kiro_crew.dashboard import server as dashboard_server

    src = Path(dashboard_server.__file__).read_text(encoding="utf-8")
    # The server_runtime owners server.py composes load with it, so their module-level
    # imports are on the same boot path.
    owners = sorted((Path(dashboard_server.__file__).parent / "server_runtime").glob("[!_]*.py"))
    assert owners, "expected the server_runtime owners beside server.py"
    src += "".join(path.read_text(encoding="utf-8") for path in owners)
    for line in src.splitlines():
        if line.startswith("from kiro_crew.dashboard.handlers import"):
            assert "session_control" not in line, (
                "session_control must not be imported at server module level -- " f"found: {line!r}"
            )
