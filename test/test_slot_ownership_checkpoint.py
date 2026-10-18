"""Every /api/chat/slots/{slot}/* route takes one app-ownership decision.

An app's ``permissions.api`` grant is a prefix match, so an app granted
``/api/chat`` reaches every per-slot path. ``slot_ownership_middleware`` decides
ownership once for the whole family rather than per handler. The pins
below are structural (every registered route is decided, every exception is
named and reasoned, the middleware sits in both server chains) and behavioural
(the live table, swept route by route, refuses a non-owner app before the
handler runs), followed by tests on the real handlers and on the requests that
can create the slot they name.

The route table is built inside fixtures, never at import: building it runs
``register_all``, which composes the platform context and writes under the data
home, and at collection time that happens before any per-test isolation.

Its last section pins the binders that never adopt an app-owned slot. The owner app's own
task-review session, linked slot and conflicts, resume's answers for a missing or
under-construction name, the mode route's grant re-read and the handlers mounted outside
the chain live in ``test_slot_ownership_checkpoint_owner_session.py``. Acquisition after
the setup of a send or create, the ownership rechecks after the awaits of title
generation, the session summary, regenerate, switch-variant and rewind, and the placement
of the final acquisition recheck live in ``test_slot_ownership_checkpoint_post_await.py``.
The fixtures and helpers more than one of these files reads live in
``slot_ownership_checkpoint_helpers.py``.
"""

from __future__ import annotations

import ast
import asyncio
import errno
import json
import re
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from slot_ownership_checkpoint_helpers import (  # noqa: F401 -- fixtures are requested by name
    _GRANT,
    _NOT_FOUND,
    APP,
    _client,
    _persist,
    _persist_and_close,
    _with_reply,
    chat_run_fixture,
    regen_run_fixture,
    sel_spy_fixture,
    state_fixture,
)

from kiro_crew.dashboard import slot_ownership
from kiro_crew.dashboard.slot_ownership import (
    SLOT_ROUTE_POLICIES,
    SlotRoutePolicy,
    slot_ownership_middleware,
    slot_route_param,
    slot_route_policy,
)
from kiro_crew.dashboard.state import SlotOrigin, _ChatSlot
from kiro_crew.testing.wait import LoopHop

_FAMILY_PREFIX = "/api/chat/slots/{"
#: Header the sweep's identity stand-in reads: absent is no claim at all, "-" is
#: the dashboard user's empty claim, anything else is that app's claim.
_CALLER = "X-Test-Caller"


@pytest.fixture
def family_routes() -> list[tuple[str, str]]:
    """Every (method, canonical template) the live table registers under a slot segment.

    Enumerated from the router by template prefix, NOT through the checkpoint's own
    matcher, so a route the matcher would miss still shows up here.
    """
    from kiro_crew.dashboard.routes import register_all

    app = web.Application()
    register_all(app)
    return sorted(
        {
            (route.method, route.resource.canonical)
            for route in app.router.routes()
            if route.resource is not None and route.resource.canonical.startswith(_FAMILY_PREFIX)
        }
    )


@web.middleware
async def _identity_from_header(request: web.Request, handler):
    """The sweep's token-auth stand-in: one server, the caller chosen per request."""
    caller = request.headers.get(_CALLER)
    if caller is not None:
        request["app"] = "" if caller == "-" else caller
        request["user"] = "local-app" if caller == "-" else ""
    return await handler(request)


# ── structural pins ──────────────────────────────────────────────────────────


class TestEveryPerSlotRouteIsDecided:
    """Modelled on ``TestEveryPeerDirectedOperationIsOwnerGated``."""

    def test_the_family_is_the_live_table(self, family_routes) -> None:
        """The sweep below is only as good as the set it sweeps."""
        paths = {path for _, path in family_routes}
        for expected in (
            "/api/chat/slots/{slot}",
            "/api/chat/slots/{slot}/regenerate",
            "/api/chat/slots/{slot}/switch-variant",
            "/api/chat/slots/{slot}/title",
            "/api/chat/slots/{slot}/generate-title",
            "/api/chat/slots/{slot}/color",
            "/api/chat/slots/{slot}/drop",
            "/api/chat/slots/{slot}/slack-link",
            "/api/chat/slots/{name}/mirror-link",
            "/api/chat/slots/{slot}/side/queue/{queue_id}",
        ):
            assert expected in paths, expected
        # Literal siblings are not per-slot routes.
        for literal in ("/api/chat/slots/import", "/api/chat/slots/cleanup"):
            assert slot_route_policy("POST", literal) is None

    def test_every_route_in_the_family_is_decided(self, family_routes) -> None:
        """Whatever the slot parameter is called, the checkpoint finds and decides it."""
        undecided = []
        for method, path in family_routes:
            param = path[len(_FAMILY_PREFIX) :].split("}", 1)[0]
            if slot_route_param(path) != param or slot_route_policy(method, path) is None:
                undecided.append((method, path))
        assert undecided == []

    def test_a_new_spelling_is_owner_gated_by_default(self) -> None:
        """A route added under a parameter name nobody listed is not a gap."""
        assert slot_route_param("/api/chat/slots/{slot_key}/purge") == "slot_key"
        assert slot_route_policy("POST", "/api/chat/slots/{slot_key}/purge") is (
            SlotRoutePolicy.OWNER
        )

    def test_every_exception_names_a_registered_route_with_a_reason(self, family_routes) -> None:
        """An exception names a registered route; one for a missing route is a parked gap."""
        for (method, path), (policy, reason) in SLOT_ROUTE_POLICIES.items():
            assert (method, path) in family_routes, (method, path)
            assert policy is not SlotRoutePolicy.OWNER, "OWNER is the default, not an entry"
            assert len(reason.split()) >= 8, (method, path)

    def test_the_exceptions_are_exactly_these(self) -> None:
        """Adding an exception is a reviewed edit to this pin, never a side effect."""
        assert {k: v[0] for k, v in SLOT_ROUTE_POLICIES.items()} == {
            ("POST", "/api/chat/slots/{slot}/approve"): SlotRoutePolicy.SESSION_GRANT,
            ("POST", "/api/chat/slots/{slot}/resume"): SlotRoutePolicy.HANDLER,
        }

    def test_the_checkpoint_sits_inner_to_token_auth_in_both_server_chains(self) -> None:
        """A chain without it would serve every per-slot route undecided."""
        import kiro_crew.dashboard.server as server_mod

        # Both chains are installed by a server_runtime owner server.py composes.
        owners = sorted((Path(server_mod.__file__).parent / "server_runtime").glob("[!_]*.py"))
        assert owners, "expected the server_runtime owners beside server.py"
        trees = [
            ast.parse(path.read_text(encoding="utf-8"))
            for path in (Path(server_mod.__file__), *owners)
        ]
        chains = []
        for node in (node for tree in trees for node in ast.walk(tree)):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Subscript)
                and ast.unparse(node.targets[0].value) == "app.middlewares"
                and isinstance(node.value, ast.List)
            ):
                chains.append([ast.unparse(e) for e in node.value.elts])
        assert len(chains) == 2, chains
        for chain in chains:
            names = [entry.split("(", 1)[0] for entry in chain]
            assert "slot_ownership_middleware" in names, chain
            checkpoint = names.index("slot_ownership_middleware")
            assert names.index("token_auth_middleware") < checkpoint, chain
            assert names.index("sel_audit_middleware") < checkpoint, chain

    def test_the_reserved_prefixes_are_the_ones_the_binders_mint(self) -> None:
        """The acquisition refusal and the binders must agree on the spelling."""
        from kiro_crew.dashboard import session_control

        assert slot_ownership.CRON_SLOT_PREFIX == session_control.CRON_SLOT_PREFIX
        assert slot_ownership.WORKFLOW_SLOT_PREFIX == session_control.WORKFLOW_SLOT_PREFIX


# ── behavioural sweep over the live table ────────────────────────────────────


def _concrete(path: str, slot: str) -> str:
    path = re.sub(r"\{[^}]+\}", slot, path, count=1)
    return re.sub(r"\{[^}]+\}", "x", path)


def _seed_family(state) -> None:
    state.get_or_create_slot("u1", origin=SlotOrigin.USER)
    state.get_or_create_slot("mine", app=APP)
    state.get_or_create_slot("theirs", app="other-app")
    # The app's own slot, re-linked to a person's cron session by a binder.
    state.get_or_create_slot("mine-linked", app=APP).linked_session_key = "cron:job-1"


def _sweep_client(state, routes: list[tuple[str, str]], reached: list[str]) -> TestClient:
    async def stand_in(request: web.Request) -> web.Response:
        reached.append(f"{request.method} {request.path}")
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[_identity_from_header, slot_ownership_middleware])
    app["state"] = state
    for method, path in routes:
        app.router.add_route(method, path, stand_in)
    return TestClient(TestServer(app))


async def _call(client: TestClient, method: str, url: str, caller: str | None):
    headers = {} if caller is None else {_CALLER: caller}
    resp = await client.request(method, url, headers=headers)
    body = None if method == "HEAD" else await resp.json()
    return resp.status, body


class TestTheLiveTableRefusesANonOwnerApp:
    @pytest.mark.asyncio
    async def test_every_route(self, state, sel_spy, family_routes) -> None:
        """One server for the whole table; each failing route is named."""
        _seed_family(state)
        reached: list[str] = []
        failures: list[str] = []
        with patch(_GRANT, return_value=False):
            async with _sweep_client(state, family_routes, reached) as client:
                for method, path in family_routes:
                    policy = slot_route_policy(method, path)
                    before = len(sel_spy.denials())
                    refused = ["u1", "theirs", "absent"]
                    if policy is SlotRoutePolicy.OWNER:
                        refused.append("mine-linked")
                    for slot in refused:
                        reached.clear()
                        status, body = await _call(client, method, _concrete(path, slot), APP)
                        if policy is SlotRoutePolicy.HANDLER:
                            if not reached:
                                failures.append(f"{method} {path} {slot}: handler not reached")
                        elif (status, bool(reached)) != (404, False) or body not in (
                            None,
                            _NOT_FOUND,
                        ):
                            failures.append(f"{method} {path} {slot}: {status} {body}")
                    # The owner app and the dashboard user reach the handler.
                    for slot, caller in (("mine", APP), ("u1", "-"), ("u1", None)):
                        reached.clear()
                        await _call(client, method, _concrete(path, slot), caller)
                        if not reached:
                            failures.append(f"{method} {path} {slot} as {caller}: not reached")
                    denials = sel_spy.denials()[before:]
                    if policy is SlotRoutePolicy.HANDLER:
                        continue
                    # A row per refusal of a slot that EXISTS; none for "absent".
                    expected_rows = len(refused) - 1
                    if len(denials) != expected_rows or any(
                        d["caller"] != APP or d["source"] != "app_isolation"
                        # Same-shape templates spelled with another parameter
                        # name ({slot} / {name}) resolve to whichever registered
                        # first, so the template is compared past its slot segment.
                        or not d["operation"].startswith(f"slot_route {method} ")
                        or not d["operation"].endswith(path.split("}", 1)[1])
                        or "absent" in d["resources"]
                        for d in denials
                    ):
                        failures.append(f"{method} {path}: audit rows {denials}")
        assert failures == []

    @pytest.mark.asyncio
    async def test_the_grant_reaches_approve_on_a_user_session_and_nothing_else(
        self, state, sel_spy
    ) -> None:
        _seed_family(state)
        routes = [
            ("POST", "/api/chat/slots/{slot}/approve"),
            ("POST", "/api/chat/slots/{slot}/regenerate"),
            ("PATCH", "/api/chat/slots/{slot}/title"),
        ]
        reached: list[str] = []
        with patch(_GRANT, return_value=True):
            async with _sweep_client(state, routes, reached) as client:
                await _call(client, "POST", "/api/chat/slots/u1/approve", APP)
                assert reached == ["POST /api/chat/slots/u1/approve"]
                # Another app's session is never grant-covered.
                assert await _call(client, "POST", "/api/chat/slots/theirs/approve", APP) == (
                    404,
                    _NOT_FOUND,
                )
                # Nor is a user session on any other route.
                for method, url in (
                    ("POST", "/api/chat/slots/u1/regenerate"),
                    ("PATCH", "/api/chat/slots/u1/title"),
                ):
                    assert await _call(client, method, url, APP) == (404, _NOT_FOUND)

    @pytest.mark.asyncio
    async def test_the_grant_does_not_reach_a_cron_session(self, state, sel_spy) -> None:
        state.get_or_create_slot("c1", origin=SlotOrigin.CRON)
        reached: list[str] = []
        routes = [("POST", "/api/chat/slots/{slot}/approve")]
        with patch(_GRANT, return_value=True):
            async with _sweep_client(state, routes, reached) as client:
                result = await _call(client, "POST", "/api/chat/slots/c1/approve", APP)
        assert (result, reached) == ((404, _NOT_FOUND), [])

    @pytest.mark.asyncio
    async def test_the_grant_is_read_once_whatever_the_slot_is(self, state, sel_spy) -> None:
        """The cost of a refusal must not depend on which kind of session was named."""
        state.get_or_create_slot("u1", origin=SlotOrigin.USER)
        state.get_or_create_slot("c1", origin=SlotOrigin.CRON)
        grant = MagicMock(return_value=False)
        routes = [("POST", "/api/chat/slots/{slot}/approve")]
        with patch(_GRANT, grant):
            async with _sweep_client(state, routes, []) as client:
                for name in ("absent", "c1", "u1"):
                    grant.reset_mock()
                    status, _ = await _call(client, "POST", f"/api/chat/slots/{name}/approve", APP)
                    assert (name, status, grant.call_count) == (name, 404, 1)


class TestCheckpointAllowAudit:
    @pytest.fixture(autouse=True)
    def audit_window(self, monkeypatch):
        from collections import OrderedDict

        monkeypatch.setattr(slot_ownership, "_allow_audits", OrderedDict())
        clock = MagicMock(return_value=0.0)
        monkeypatch.setattr(slot_ownership, "monotonic", clock)
        return clock

    @pytest.mark.asyncio
    async def test_repeats_carry_the_suppressed_count_after_the_window(
        self, state, sel_spy, audit_window
    ) -> None:
        state.get_or_create_slot("mine", app=APP)
        async with _client(state, APP) as client:
            for now in (0.0, 1.0, slot_ownership._ALLOW_AUDIT_WINDOW_SECS - 1):
                audit_window.return_value = now
                response = await client.get("/api/chat/slots/mine")
                assert response.status == 200
            assert len(sel_spy.rows) == 1
            for now in (1, 2):
                audit_window.return_value = now * slot_ownership._ALLOW_AUDIT_WINDOW_SECS
                response = await client.get("/api/chat/slots/mine")
                assert response.status == 200
        assert [row["resources"] for row in sel_spy.rows] == [
            "slot=mine",
            "slot=mine (suppressed=2)",
            "slot=mine",
        ]
        assert all(row["outcome"] == "allowed" for row in sel_spy.rows)

    @pytest.mark.asyncio
    async def test_the_session_grant_checkpoint_also_records_allows(self, state, sel_spy) -> None:
        state.get_or_create_slot("u1", origin=SlotOrigin.USER)
        routes = [("POST", "/api/chat/slots/{slot}/approve")]
        with patch(_GRANT, return_value=True):
            async with _sweep_client(state, routes, []) as client:
                assert await _call(client, "POST", "/api/chat/slots/u1/approve", APP) == (
                    200,
                    {"ok": True},
                )
        assert sel_spy.rows == [
            {
                "caller": APP,
                "operation": "slot_route POST /api/chat/slots/{slot}/approve",
                "outcome": "allowed",
                "source": slot_ownership.APP_ISOLATION_SOURCE,
                "resources": "slot=u1",
            }
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method,path,name,caller,status",
        [
            ("GET", "/api/chat/slots/{slot}", "mine", None, 200),
            ("GET", "/api/chat/slots/{slot}", "mine", "-", 200),
            ("GET", "/api/chat/slots/{slot}", "absent", APP, 404),
            ("POST", "/api/chat/slots/{slot}/approve", "absent", APP, 404),
            ("POST", "/api/chat/slots/{slot}/resume", "mine", APP, 200),
        ],
        ids=["no-claim", "dashboard", "missing-owner", "missing-grant", "handler"],
    )
    async def test_undecided_callers_and_missing_slots_emit_no_row(
        self, state, sel_spy, method, path, name, caller, status
    ) -> None:
        state.get_or_create_slot("mine", app=APP)
        with patch(_GRANT, return_value=True):
            async with _sweep_client(state, [(method, path)], []) as client:
                result, _ = await _call(client, method, _concrete(path, name), caller)
                assert result == status
        assert sel_spy.rows == []
        assert not slot_ownership._allow_audits

    @pytest.mark.asyncio
    async def test_each_app_operation_and_slot_has_its_own_window(self, state, sel_spy) -> None:
        state.get_or_create_slot("mine", app=APP)
        state.get_or_create_slot("second", app=APP)
        routes = [
            ("GET", "/api/chat/slots/{slot}"),
            ("GET", "/api/chat/slots/{slot}/export"),
            ("DELETE", "/api/chat/slots/{slot}"),
        ]
        async with _sweep_client(state, routes, []) as client:
            for method, path in routes:
                assert (await _call(client, method, _concrete(path, "mine"), APP))[0] == 200
            assert (await _call(client, "GET", "/api/chat/slots/second", APP))[0] == 200
            state._slots["mine"]._app = "other-app"
            assert (await _call(client, "GET", "/api/chat/slots/mine", "other-app"))[0] == 200
        assert len(sel_spy.rows) == 5
        assert len({(r["caller"], r["operation"], r["resources"]) for r in sel_spy.rows}) == 5

    @pytest.mark.asyncio
    async def test_the_cache_evicts_the_oldest_emission(
        self, state, sel_spy, monkeypatch, audit_window
    ) -> None:
        monkeypatch.setattr(slot_ownership, "_ALLOW_AUDIT_MAX_ENTRIES", 2)
        for name in ("first", "second", "third"):
            state.get_or_create_slot(name, app=APP)
        routes = [("GET", "/api/chat/slots/{slot}")]
        async with _sweep_client(state, routes, []) as client:
            for now, name in enumerate(("first", "second", "first", "third", "second", "first")):
                audit_window.return_value = now
                assert (await _call(client, "GET", f"/api/chat/slots/{name}", APP))[0] == 200
                assert len(slot_ownership._allow_audits) <= 2
        assert [row["resources"] for row in sel_spy.rows] == [
            "slot=first",
            "slot=second",
            "slot=third",
            "slot=first",
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fault", ["initialization", "submission"])
    async def test_an_audit_fault_keeps_the_allow_decision(self, state, monkeypatch, fault) -> None:
        state.get_or_create_slot("mine", app=APP)
        broken = MagicMock(side_effect=OSError("audit unavailable"))
        if fault == "initialization":
            monkeypatch.setattr(slot_ownership, "sel", broken)
        else:
            monkeypatch.setattr(slot_ownership, "sel", lambda: MagicMock(log_api_access=broken))
        async with _client(state, APP) as client:
            response = await client.get("/api/chat/slots/mine")
            assert response.status == 200, await response.text()
        broken.assert_called_once()

    @pytest.mark.asyncio
    async def test_owner_transcript_read_emits_one_allowed_row(self, state, sel_spy) -> None:
        state.get_or_create_slot("mine", app=APP)
        async with _client(state, APP) as client:
            response = await client.get("/api/chat/slots/mine")
            assert response.status == 200, await response.text()
        assert sel_spy.rows == [
            {
                "caller": APP,
                "operation": "slot_route GET /api/chat/slots/{slot}",
                "outcome": "allowed",
                "source": slot_ownership.APP_ISOLATION_SOURCE,
                "resources": "slot=mine",
            }
        ]


# ── the real handlers ────────────────────────────────────────────────────────


def _foreign_slot(state, kind: str) -> _ChatSlot:
    """A slot named ``s1`` that the app ``APP`` does not own, or owns on a foreign session."""
    if kind == "user":
        return state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    if kind == "cron":
        return state.get_or_create_slot("s1", origin=SlotOrigin.CRON)
    if kind == "system":
        return state.get_or_create_slot("s1", origin=SlotOrigin.SYSTEM)
    if kind == "member":
        return state.get_or_create_slot("s1", origin=SlotOrigin.USER, mode="member")
    if kind == "cron-linked":
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        slot.linked_session_key = "cron:job-1"
        return slot
    if kind == "channel-linked":
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        slot.linked_session_key = "slack:12345.678"
        return slot
    if kind == "own-cron-linked":
        slot = state.get_or_create_slot("s1", app=APP)
        slot.linked_session_key = "cron:job-1"
        return slot
    if kind == "other-app":
        return state.get_or_create_slot("s1", app="other-app")
    if kind == "remote":
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        slot.executor = "remote"
        slot.instance_id = "peer-1"
        slot.remote_slot = "remote-s1"
        return slot
    raise AssertionError(kind)


_FOREIGN = [
    "user",
    "cron",
    "system",
    "member",
    "cron-linked",
    "channel-linked",
    "own-cron-linked",
    "other-app",
    "remote",
]


class TestRegenerateAndSwitchVariant:
    """Both truncate or overwrite a reply, so a foreign app must not reach either."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", _FOREIGN)
    async def test_a_non_owner_app_changes_nothing(self, state, regen_run, kind) -> None:
        slot = _foreign_slot(state, kind)
        _with_reply(slot)
        slot.messages[-1]["variants"] = [{"content": "AN OLDER VARIANT"}, {"content": "CURRENT"}]
        before = [dict(m) for m in slot.messages]
        with patch(_GRANT, return_value=False):
            async with _client(state, APP) as client:
                regen = await client.post("/api/chat/slots/s1/regenerate")
                switch = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
                assert (regen.status, await regen.json()) == (404, _NOT_FOUND)
                assert (switch.status, await switch.json()) == (404, _NOT_FOUND)
        assert slot.messages == before
        regen_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_owner_app_regenerates_and_is_recorded_as_itself(
        self, state, regen_run, monkeypatch
    ) -> None:
        rows: list[dict] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_regenerate.sel",
            lambda: MagicMock(log_api_access=lambda **kw: rows.append(kw)),
        )
        slot = state.get_or_create_slot("s1", app=APP)
        _with_reply(slot)
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200, await resp.text()
        regen_run.assert_called_once()
        regen = [r for r in rows if r.get("operation") == "chat.regenerate"]
        # ``source=app`` is reserved for an app's own ctx.audit reports.
        assert regen and (regen[0]["caller"], regen[0]["source"]) == (APP, "dashboard")

    @pytest.mark.asyncio
    async def test_the_owner_app_switches_variant_and_is_recorded_as_itself(
        self, state, monkeypatch
    ) -> None:
        rows: list[dict] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_regenerate.sel",
            lambda: MagicMock(log_api_access=lambda **kw: rows.append(kw)),
        )
        slot = state.get_or_create_slot("s1", app=APP)
        _with_reply(slot, "CURRENT")
        slot.messages[-1]["variants"] = [{"content": "AN OLDER VARIANT"}, {"content": "CURRENT"}]
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
            assert resp.status == 200, await resp.text()
        assert slot.messages[-1]["content"] == "AN OLDER VARIANT"
        switch = [r for r in rows if r.get("operation") == "chat.switch_variant"]
        assert switch and (switch[0]["caller"], switch[0]["source"]) == (APP, "dashboard")

    @pytest.mark.asyncio
    async def test_the_dashboard_user_regenerates_a_user_session(self, state, regen_run) -> None:
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        _with_reply(slot)
        async with _client(state, "") as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200, await resp.text()
        regen_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_slot_replaced_during_the_readiness_await_is_not_acted_on(
        self, state, regen_run, monkeypatch
    ) -> None:
        """The checkpoint judged the app's slot; the handler must not act on its successor."""
        state.get_or_create_slot("s1", app=APP)
        replacement = _ChatSlot("s1")
        replacement._origin = SlotOrigin.USER
        _with_reply(replacement)
        before = [dict(m) for m in replacement.messages]

        async def swap_during_await(_request):
            state._slots["s1"] = replacement
            return None

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_regenerate.reject_if_kiro_unverified", swap_during_await
        )
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert replacement.messages == before
        regen_run.assert_not_called()


class TestTitleAndColour:
    """generate-title is a READ as well as a write: its reply is built from the transcript."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["user", "cron", "member", "other-app", "own-cron-linked"])
    async def test_a_non_owner_app_reads_and_changes_nothing(self, state, kind) -> None:
        slot = _foreign_slot(state, kind)
        slot.append("user", "my private plan for the merger")
        slot.drain()
        slot.title = "User's own title"
        slot._titled = True
        slot._title_origin = "user"
        slot.color_index = 2
        llm = AsyncMock(return_value="A summary of the private plan")
        with (
            patch(_GRANT, return_value=False),
            patch("kiro_crew.dashboard.chat_title._generate_title_via_kiro", new=llm),
        ):
            async with _client(state, APP) as client:
                gen = await client.post("/api/chat/slots/s1/generate-title")
                ren = await client.patch("/api/chat/slots/s1/title", json={"title": "renamed"})
                col = await client.patch("/api/chat/slots/s1/color", json={"color_hex": "#ff0000"})
                for resp in (gen, ren, col):
                    assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        llm.assert_not_called()
        assert (slot.title, slot._title_origin) == ("User's own title", "user")
        assert (slot.color_index, slot.color_hex) == (2, None)

    @pytest.mark.asyncio
    async def test_the_owner_app_renames_and_recolours_its_own_slot(self, state) -> None:
        slot = state.get_or_create_slot("s1", app=APP)
        async with _client(state, APP) as client:
            ren = await client.patch("/api/chat/slots/s1/title", json={"title": "mine"})
            col = await client.patch("/api/chat/slots/s1/color", json={"color_index": 3})
            assert ren.status == 200 and col.status == 200
        assert (slot.title, slot.color_index) == ("mine", 3)

    @pytest.mark.asyncio
    async def test_the_dashboard_user_renames_and_recolours(self, state) -> None:
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        async with _client(state, "") as client:
            ren = await client.patch("/api/chat/slots/s1/title", json={"title": "mine"})
            col = await client.patch("/api/chat/slots/s1/color", json={"color_index": 4})
            assert ren.status == 200 and col.status == 200
        assert (slot.title, slot.color_index) == ("mine", 4)


class TestChannelLinksAndLanes:
    """Routes whose handlers carry no app check of their own: a refusal changes nothing."""

    @pytest.mark.asyncio
    async def test_an_app_cannot_move_a_users_session_into_a_lane(self, state) -> None:
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        slot.tags = ["todo"]
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/s1/drop", json={"column_id": "done-lane"})
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert slot.tags == ["todo"]

    @pytest.mark.asyncio
    async def test_an_app_cannot_link_a_users_session_to_a_channel(self, state) -> None:
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        async with _client(state, APP) as client:
            resp = await client.post(
                "/api/chat/slots/s1/slack-link", json={"channel_id": "C_SOMEWHERE"}
            )
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert not slot._slack_linked and not slot._slack_channel

    @pytest.mark.asyncio
    async def test_mirror_unlink_does_not_act_on_a_slot_replaced_during_the_body_read(
        self, state, monkeypatch
    ) -> None:
        state.get_or_create_slot("s1", app=APP)
        replacement = _ChatSlot("s1")
        replacement._origin = SlotOrigin.USER

        async def swap_during_read(_request):
            state._slots["s1"] = replacement
            return None

        monkeypatch.setattr("kiro_crew.dashboard.chat_mirror._expected_binding", swap_during_read)
        cleared = MagicMock()
        state.sessions.clear_mirror_link = cleared
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-unlink")
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        cleared.assert_not_called()


class TestExport:
    @pytest.mark.asyncio
    async def test_absent_foreign_and_own_linked_slots_answer_one_body(self, state) -> None:
        """A distinguishable body would tell an app which of its slots carry a channel link."""
        state.get_or_create_slot("theirs", origin=SlotOrigin.USER)
        state.get_or_create_slot("linked", app=APP).linked_session_key = "slack:1700000000.000100"
        bodies = []
        async with _client(state, APP) as client:
            for name in ("absent", "theirs", "linked"):
                resp = await client.get(f"/api/chat/slots/{name}/export")
                bodies.append((resp.status, await resp.read()))
        assert len(set(bodies)) == 1, bodies
        assert bodies[0][0] == 404


# ── requests that can create the slot they name ──────────────────────────────


class TestTranscriptOwnershipWithoutALiveSlot:
    """A closed session has no slot for the checkpoint; its transcript records the owner."""

    @pytest.mark.asyncio
    async def test_an_app_cannot_resume_a_users_closed_session(self, state) -> None:
        await _persist_and_close(state, state.get_or_create_slot("s1", origin=SlotOrigin.USER))
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
            # Naming the user's transcript as the history key of a fresh name too.
            resp = await client.post("/api/chat/slots/fresh/resume", json={"key": "dashboard:s1"})
            assert resp.status == 404
        assert "s1" not in state._slots and "fresh" not in state._slots
        assert state.conversation_log.get_metadata("dashboard:s1").get("closed")

    @pytest.mark.asyncio
    async def test_an_app_cannot_publish_its_own_transcript_under_a_users_session(
        self, state
    ) -> None:
        """The slot a resume builds writes ``dashboard:<name>``, so that key must be the app's too."""
        await _persist_and_close(state, state.get_or_create_slot("a1", app=APP))
        victim = state.get_or_create_slot("victim", origin=SlotOrigin.USER)
        await _persist_and_close(state, victim)
        before = state.conversation_log.read_messages("dashboard:victim")
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/victim/resume", json={"key": "dashboard:a1"})
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert "victim" not in state._slots
        meta = state.conversation_log.get_metadata("dashboard:victim")
        assert meta.get("closed") and not meta.get("app")
        assert state.conversation_log.read_messages("dashboard:victim") == before

    @pytest.mark.asyncio
    @pytest.mark.parametrize("caller", [APP, ""], ids=["app", "dashboard"])
    async def test_resume_rechecks_a_destination_created_and_closed_during_setup(
        self, state, monkeypatch, caller
    ) -> None:
        """The source's owner cannot authorize a destination written during the reads."""
        from kiro_crew.dashboard import chat_handlers

        log = state.conversation_log
        source_key, destination_key = "dashboard:a1", "dashboard:b1"
        await asyncio.to_thread(
            log.update_metadata, source_key, {"app": APP, "closed": True, "closed_at": 1.0}
        )
        assert await asyncio.to_thread(log.read_messages, source_key) == []
        assert not await asyncio.to_thread(log.has_log, destination_key)
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        hop = LoopHop(loop)
        before = {}
        load_cfg = chat_handlers._load_restore_cfg

        async def create_and_close_destination():
            assert "b1" not in state._slots_under_construction
            # The reopen write runs after construction, so the source is still closed.
            assert (await asyncio.to_thread(log.get_metadata, source_key)).get("closed")
            hop.note("source still closed")
            await _persist_and_close(state, state.get_or_create_slot("b1", origin=SlotOrigin.USER))
            hop.note("destination persisted and closed")
            before["meta"] = await asyncio.to_thread(log.get_metadata, destination_key)
            before["rows"] = await asyncio.to_thread(log.read_messages, destination_key)

        def restore_cfg():
            assert threading.get_ident() != loop_thread
            hop.run(create_and_close_destination(), "the destination's create-and-close")
            return load_cfg()

        monkeypatch.setattr(chat_handlers, "_load_restore_cfg", restore_cfg)
        async with _client(state, caller) as client:
            resp = await client.post("/api/chat/slots/b1/resume", json={"key": source_key})
            hop.check()
            assert before["meta"]["closed"], "the destination interleaving did not occur"
            assert before["rows"], "the destination must contain the person's persisted rows"
            if caller:
                assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
                detail = await client.get("/api/chat/slots/b1")
                assert (detail.status, await detail.json()) == (404, _NOT_FOUND)
                assert "b1" not in state._slots
                source = await asyncio.to_thread(log.get_metadata, source_key)
                assert source["closed"] and source["closed_at"] == 1.0
            else:
                assert resp.status == 200, await resp.text()
                assert state._slots["b1"]._app == ""
        assert "b1" not in state._slots_under_construction
        assert await asyncio.to_thread(log.get_metadata, destination_key) == before["meta"]
        assert await asyncio.to_thread(log.read_messages, destination_key) == before["rows"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "source_change",
        ["", "owner", "deleted", "identity"],
        ids=["owned-source", "foreign-source", "deleted-source", "recreated-source"],
    )
    async def test_resume_reserves_the_destination_and_rechecks_the_source(
        self, state, monkeypatch, source_change
    ) -> None:
        from kiro_crew.dashboard import chat_handlers

        log = state.conversation_log
        source_key = "dashboard:a1"
        await asyncio.to_thread(
            log.update_metadata, source_key, {"app": APP, "closed": True, "closed_at": 1.0}
        )
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        hop = LoopHop(loop)
        reservations = []
        read_owner = chat_handlers.transcript_acquisition_reason

        async def while_destination_is_reserved():
            with pytest.raises(ValueError, match="still being built"):
                state.get_or_create_slot("b1", origin=SlotOrigin.USER)
            hop.note("create of the reserved destination refused")
            if source_change == "owner":
                await asyncio.to_thread(log.update_metadata, source_key, {"app": ""})
            elif source_change:
                await asyncio.to_thread(log.delete_session, source_key)
                hop.note("source deleted")
                if source_change == "identity":
                    await asyncio.to_thread(
                        log.update_metadata, source_key, {"app": APP, "created_at": "replacement"}
                    )

        def inspect_destination(log_arg, key, app):
            assert threading.get_ident() != loop_thread
            reason = read_owner(log_arg, key, app)
            if key == "dashboard:b1":
                reserved = "b1" in state._slots_under_construction
                reservations.append(reserved)
                if reserved:
                    hop.run(while_destination_is_reserved(), f"the source change {source_change!r}")
            return reason

        monkeypatch.setattr(chat_handlers, "transcript_acquisition_reason", inspect_destination)
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/b1/resume", json={"key": source_key})
            hop.check()
            assert reservations == [False, True]
            if source_change == "owner":
                assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
                assert "b1" not in state._slots
                assert (await asyncio.to_thread(log.get_metadata, source_key))["closed"]
            elif source_change:
                assert resp.status == 409, await resp.text()
                assert (await resp.json())["code"] == "resume_session_deleted"
                assert "b1" not in state._slots
            else:
                assert resp.status == 200, await resp.text()
                assert state._slots["b1"]._app == APP
        assert "b1" not in state._slots_under_construction

    @pytest.mark.asyncio
    async def test_resume_rechecks_app_ownership_after_the_reopen_write(
        self, state, monkeypatch
    ) -> None:
        """A transcript replaced under another app inside the reopen window never publishes.

        A closed session's resume clears ``closed`` after construction, in an
        awaited worker call. The fixture's line carries no ``created_at`` (the
        legacy shape), so the identity arm cannot see a delete and same-key
        recreate there; ownership is what tells the two transcripts apart. The
        clear is parked, the session is deleted and recreated as another app's,
        and the resume must refuse with the app's uniform 404, publish nothing,
        and leave the other app's transcript as that app wrote it.
        """
        log = state.conversation_log
        key = "dashboard:a1"

        def write_legacy_line(fields: dict) -> None:
            # Written by hand: every store writer stamps ``created_at``.
            path = log._path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"_type": "metadata", **fields}) + "\n", encoding="utf-8")

        await asyncio.to_thread(write_legacy_line, {"app": APP, "closed": True, "closed_at": 1.0})
        assert "created_at" not in await asyncio.to_thread(log.get_metadata, key)
        original_clear = log.clear_closed
        replaced = []

        def replace_then_clear(*args, **kwargs):
            assert log.delete_session(key), "the fixture did not delete the source"
            write_legacy_line({"app": "other-app"})
            replaced.append(True)
            return original_clear(*args, **kwargs)

        monkeypatch.setattr(log, "clear_closed", replace_then_clear)
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/a1/resume", json={"key": key})
            body = await resp.json()
        assert replaced, "the resume never reached its reopen write"
        assert (resp.status, body) == (404, _NOT_FOUND), (
            f"an app resume published a transcript another app recreated during the "
            f"reopen write (status {resp.status})"
        )
        assert "a1" not in state._slots
        assert "a1" not in state._slots_under_construction
        meta = await asyncio.to_thread(log.get_metadata, key)
        assert meta.get("app") == "other-app"
        assert "closed" not in meta, "the rollback closed another app's transcript"

    @pytest.mark.asyncio
    async def test_resume_dedups_a_session_published_during_the_destination_read(
        self, state, monkeypatch
    ) -> None:
        from kiro_crew.dashboard import chat_handlers

        log = state.conversation_log
        source_key = "dashboard:a1"
        await asyncio.to_thread(
            log.update_metadata, source_key, {"app": APP, "closed": True, "closed_at": 1.0}
        )
        hop = LoopHop(asyncio.get_running_loop())
        read_owner = chat_handlers.transcript_acquisition_reason
        published = []

        async def publish_same_transcript():
            published.append(state.get_or_create_slot("a1", app=APP))

        def inspect_destination(log_arg, key, app):
            reason = read_owner(log_arg, key, app)
            if key == "dashboard:b1" and "b1" in state._slots_under_construction:
                hop.run(publish_same_transcript(), "the same-transcript publish")
            return reason

        monkeypatch.setattr(chat_handlers, "transcript_acquisition_reason", inspect_destination)
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/b1/resume", json={"key": source_key})
            hop.check()
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["key"] == "a1"
        assert published and state._slots["a1"] is published[0]
        assert "b1" not in state._slots
        assert "b1" not in state._slots_under_construction

    @pytest.mark.asyncio
    async def test_an_app_resumes_its_own_closed_session(self, state) -> None:
        await _persist_and_close(state, state.get_or_create_slot("a1", app=APP))
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/a1/resume", json={"key": "dashboard:a1"})
            assert resp.status == 200, await resp.text()
        assert state._slots["a1"]._app == APP

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [{}, {"key": "a1"}], ids=["no-key", "bare-key"])
    async def test_no_key_or_the_bare_name_means_the_slots_own_transcript(
        self, state, body
    ) -> None:
        await _persist_and_close(state, state.get_or_create_slot("a1", app=APP))
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/a1/resume", json=body)
            assert resp.status == 200, await resp.text()
        assert state._slots["a1"]._app == APP

    @pytest.mark.asyncio
    @pytest.mark.parametrize("clears", [True, False], ids=["transient-clears", "persistent-locks"])
    async def test_a_sharing_violation_on_the_final_reread_retries_then_decides(
        self, state, monkeypatch, clears
    ) -> None:
        """The identity re-reads survive a transient file lock on the resume's own line.

        A just-rewritten session file is briefly unopenable while an indexer or AV
        scanner holds it on Windows (ERROR_SHARING_VIOLATION). The off-loop read's
        bounded retries ride over a lock that clears within the budget, so the
        resume publishes; a lock that outlasts every attempt refuses and publishes
        nothing. Count-based rather than clock-based so it is deterministic on a
        loaded runner.
        """
        import builtins
        import threading

        from kiro_crew.history import _METADATA_READ_ATTEMPTS

        await _persist_and_close(state, state.get_or_create_slot("a1", app=APP))
        log = state.conversation_log
        target = str(log._path("dashboard:a1"))
        loop_thread = threading.get_ident()
        real_open = builtins.open
        real_clear = log.clear_closed
        real_status = log.get_metadata_status
        armed = {"on": False}
        reread_threads: list[int] = []
        # A clearing lock faults every attempt but the last within one read's
        # budget; a persistent lock faults every attempt of every read.
        fail_budget = {"n": (_METADATA_READ_ATTEMPTS - 1) if clears else 10_000_000}

        def clear_closed(key, **kw):
            out = real_clear(key, **kw)
            # Model the just-rewritten file being briefly unopenable: arm only
            # for the re-reads after the reopen write.
            armed["on"] = True
            return out

        def status(key):
            if armed["on"]:
                reread_threads.append(threading.get_ident())
            return real_status(key)

        def flaky_open(file, *args, **kwargs):
            if armed["on"] and str(file) == target and fail_budget["n"] > 0:
                fail_budget["n"] -= 1
                raise PermissionError("ERROR_SHARING_VIOLATION (simulated)")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(log, "clear_closed", clear_closed)
        monkeypatch.setattr(log, "get_metadata_status", status)
        monkeypatch.setattr(builtins, "open", flaky_open)
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/a1/resume", json={"key": "dashboard:a1"})
            body = await resp.text()
        if clears:
            assert resp.status == 200, body
            assert state._slots["a1"]._app == APP
            # An identity re-read runs off the loop, so its paused retries outlast
            # the hold the on-loop reads cannot.
            assert any(t != loop_thread for t in reread_threads)
        else:
            assert resp.status != 200, body
            assert "a1" not in state._slots
            assert "a1" not in state._slots_under_construction

    @pytest.mark.asyncio
    async def test_a_lock_outlasting_one_reads_budget_but_not_the_handlers_publishes(
        self, state, monkeypatch
    ) -> None:
        """A reopen re-read waits out a lock longer than one read's own retry budget.

        The post-reopen re-reads open a file this resume just rewrote. On a loaded
        Windows runner the transient hold can outlast ``get_metadata_status``'s own
        bounded retry, which then reports unreadable and would refuse. The handler
        re-reads a few more times off the loop, so a hold that clears within that
        wider window lets the read succeed and the resume publishes. The fault
        budget here exceeds one read's attempts but not the handler's, so a read
        with no extra retries would refuse and the extra retries are what carry it.
        Not run on a real Windows host.
        """
        import builtins

        from kiro_crew.history import _METADATA_READ_ATTEMPTS

        await _persist_and_close(state, state.get_or_create_slot("a1", app=APP))
        log = state.conversation_log
        target = str(log._path("dashboard:a1"))
        real_open = builtins.open
        real_clear = log.clear_closed
        armed = {"on": False}
        # More faults than one read's internal budget (so a single read reports
        # unreadable), but few enough that the handler's extra off-loop re-reads
        # clear the hold.
        fail_budget = {"n": _METADATA_READ_ATTEMPTS + 2}

        def clear_closed(key, **kw):
            out = real_clear(key, **kw)
            armed["on"] = True
            return out

        def flaky_open(file, *args, **kwargs):
            if armed["on"] and str(file) == target and fail_budget["n"] > 0:
                fail_budget["n"] -= 1
                raise PermissionError("ERROR_SHARING_VIOLATION (simulated)")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(log, "clear_closed", clear_closed)
        monkeypatch.setattr(builtins, "open", flaky_open)
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/a1/resume", json={})
            body = await resp.text()
        assert fail_budget["n"] == 0, "the simulated transient did not fully clear"
        assert resp.status == 200, body
        assert state._slots["a1"]._app == APP
        assert "a1" not in state._slots_under_construction

    @pytest.mark.asyncio
    async def test_a_delete_finishing_inside_the_final_offloop_read_refuses(
        self, state, monkeypatch
    ) -> None:
        """A delete that begins and ends during the off-loop final read is caught.

        The final identity re-read is off the loop, so a whole process-local
        delete can run and release inside it: its in-flight marker clears before
        ``_identity_refusal`` reads it. The invalidation generation outlives the
        marker — every delete bumps it — so the synchronous generation re-check
        before the publish refuses, and no slot is published over the deleted
        session.
        """
        await _persist_and_close(state, state.get_or_create_slot("a1", app=APP))
        log = state.conversation_log
        real_status = log.get_metadata_status
        real_clear = log.clear_closed
        fired = {"on": False}
        state_box = {"after_reopen": False, "rereads": 0}

        def arm(key, **kw):
            state_box["after_reopen"] = True
            return real_clear(key, **kw)

        def status(key):
            # The final re-read is the second re-read after the reopen write
            # (the clear's own verification read is the first). The generation is
            # snapshotted just before that final read, so model the delete
            # landing THERE: it bumps the generation and clears its in-flight
            # marker inside the off-loop window.
            if state_box["after_reopen"]:
                state_box["rereads"] += 1
                if state_box["rereads"] == 2 and not fired["on"]:
                    fired["on"] = True
                    log._invalidate_cache("dashboard:a1")
            return real_status(key)

        monkeypatch.setattr(log, "clear_closed", arm)
        monkeypatch.setattr(log, "get_metadata_status", status)
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/a1/resume", json={"key": "dashboard:a1"})
            payload = await resp.json()
        assert fired["on"], "the modelled delete did not land in the read window"
        assert (resp.status, payload["code"]) == (409, "resume_conflict")
        assert "a1" not in state._slots
        assert "a1" not in state._slots_under_construction

    @pytest.mark.asyncio
    async def test_a_late_refusal_leaves_the_closed_marker_untouched(
        self, state, monkeypatch
    ) -> None:
        """Refused at the late ownership barrier, the transcript is left closed.

        The reopen write runs only after construction, so the late barrier refuses
        before any durable write: the clear is never attempted.
        """
        await _persist_and_close(state, state.get_or_create_slot("a1", app=APP))
        log = state.conversation_log
        cleared: list[str] = []
        reads: list[str] = []
        real_clear, real_get = log.clear_closed, log.get_metadata

        def clear_closed(key, **kw):
            cleared.append(key)
            return real_clear(key, **kw)

        def get_metadata(key):
            reads.append(key)
            meta = real_get(key)
            # Every snapshot after the first records no app: the post-read
            # snapshot is not the app's.
            return {k: v for k, v in meta.items() if k != "app"} if len(reads) > 1 else meta

        monkeypatch.setattr(log, "clear_closed", clear_closed)
        monkeypatch.setattr(log, "get_metadata", get_metadata)
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/a1/resume", json={"key": "dashboard:a1"})
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert len(reads) > 1, "the late barrier's re-read did not happen"
        assert cleared == []
        assert "a1" not in state._slots
        assert "a1" not in state._slots_under_construction
        meta, readable = log.get_metadata_status("dashboard:a1")
        assert readable and meta.get("closed")

    @pytest.mark.asyncio
    async def test_an_app_cannot_recreate_a_users_closed_session_by_sending(
        self, state, chat_run
    ) -> None:
        await _persist_and_close(state, state.get_or_create_slot("s1", origin=SlotOrigin.USER))
        with patch(_GRANT, return_value=True):
            async with _client(state, APP) as client:
                send = await client.post("/api/chat?ws=1", json={"slot": "s1", "message": "hi"})
                create = await client.post("/api/chat/slots", json={"name": "s1"})
                assert (send.status, await send.json()) == (404, _NOT_FOUND)
                assert (create.status, await create.json()) == (404, _NOT_FOUND)
        chat_run.assert_not_called()
        assert "s1" not in state._slots
        meta = state.conversation_log.get_metadata("dashboard:s1")
        assert meta.get("closed") and not meta.get("app")

    @pytest.mark.asyncio
    async def test_an_unreadable_transcript_is_refused_under_its_own_reason(
        self, state, sel_spy, monkeypatch
    ) -> None:
        """A read fault is not filed as an isolation breach."""
        log = state.conversation_log
        monkeypatch.setattr(log, "has_log", lambda key: True)
        monkeypatch.setattr(log, "get_metadata_status", lambda key: ({}, False))
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots", json={"name": "s1"})
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert [d["error"] for d in sel_spy.denials()] == ["transcript metadata unreadable"]

    @pytest.mark.asyncio
    async def test_a_name_the_filesystem_rejects_is_a_404_not_a_500(
        self, state, chat_run, sel_spy, monkeypatch
    ) -> None:
        """The stat raising (an over-long name on Linux and macOS) refuses instead of raising.

        The fault is injected: whether a host raises for an over-long name or answers
        "no such file" depends on the platform, and only the raising half is under test.
        """

        def has_log(key):
            raise OSError(errno.ENAMETOOLONG, "File name too long", key)

        monkeypatch.setattr(state.conversation_log, "has_log", has_log)
        async with _client(state, APP) as client:
            send = await client.post("/api/chat?ws=1", json={"slot": "s1", "message": "hi"})
            create = await client.post("/api/chat/slots", json={"name": "s1"})
            assert (send.status, await send.json()) == (404, _NOT_FOUND)
            assert (create.status, await create.json()) == (404, _NOT_FOUND)
        chat_run.assert_not_called()
        assert "s1" not in state._slots
        assert [d["error"] for d in sel_spy.denials()] == ["transcript key cannot be read"] * 2


class TestNoAnswerBeforeOwnership:
    """A 409 from slot acquisition would tell an app about a session it may not see."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {"slot": "s1", "message": "x", "memory_mode": "incognito"},
            {"slot": "uc1", "message": "x"},
            {"slot": "member-foo", "message": "x"},
            {"slot": "dashboard:dashboard:member-foo", "message": "x"},
            {"slot": "dashboard:dashboard:s9", "message": "x", "memory_mode": "incognito"},
            {"slot": "cron-job-1", "message": "x"},
            {"slot": "workflow-run-1", "message": "x"},
        ],
        ids=[
            "memory-mode-mismatch",
            "under-construction",
            "member-key",
            "member-key-doubled-prefix",
            "memory-mode-doubled-prefix",
            "cron-key",
            "workflow-key",
        ],
    )
    async def test_send(self, state, chat_run, body) -> None:
        state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        state.get_or_create_slot("dashboard_s9", origin=SlotOrigin.USER)
        state.get_or_create_slot("uc1", origin=SlotOrigin.USER)
        state._slots_under_construction.add("uc1")
        live = set(state._slots)
        with patch(_GRANT, return_value=False):
            async with _client(state, APP) as client:
                resp = await client.post("/api/chat?ws=1", json=body)
                assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        chat_run.assert_not_called()
        assert set(state._slots) == live

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {"name": "s1", "memory_mode": "incognito"},
            {"name": "cron-job-1"},
            {"name": "workflow-run-1"},
            {"name": "dashboard:dashboard:member-foo"},
        ],
        ids=["memory-mode-mismatch", "cron-key", "workflow-key", "member-key-doubled-prefix"],
    )
    async def test_create(self, state, body) -> None:
        state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        live = set(state._slots)
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots", json=body)
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert set(state._slots) == live

    @pytest.mark.asyncio
    async def test_a_session_being_imported_is_nobodys_to_name(
        self, state, chat_run, sel_spy
    ) -> None:
        """The import tail retracts the slot but keeps its construction mark."""
        state._slots_under_construction.add("chat-3-1700000000")
        async with _client(state, APP) as client:
            send = await client.post(
                "/api/chat?ws=1", json={"slot": "chat-3-1700000000", "message": "x"}
            )
            create = await client.post("/api/chat/slots", json={"name": "chat-3-1700000000"})
            assert (send.status, await send.json()) == (404, _NOT_FOUND)
            assert (create.status, await create.json()) == (404, _NOT_FOUND)
        async with _client(state, "") as client:
            resp = await client.post("/api/chat/slots", json={"name": "chat-3-1700000000"})
            # The person still gets the retryable conflict.
            assert resp.status == 409
        assert "chat-3-1700000000" not in state._slots
        assert sel_spy.denials() == []

    @pytest.mark.asyncio
    async def test_a_conflict_raised_at_acquisition_is_still_a_404_for_an_app(
        self, state, chat_run, monkeypatch
    ) -> None:
        """A slot created after the pre-check decided must not be answered with a 409."""

        async def nothing_live_yet(*_a, **_kw):
            return None, None

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._app_slot_acquisition_denial", nothing_live_yet
        )
        state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        async with _client(state, APP) as client:
            send = await client.post(
                "/api/chat?ws=1", json={"slot": "s1", "message": "x", "memory_mode": "incognito"}
            )
            create = await client.post(
                "/api/chat/slots", json={"name": "s1", "memory_mode": "incognito"}
            )
            assert (send.status, await send.json()) == (404, _NOT_FOUND)
            assert (create.status, await create.json()) == (404, _NOT_FOUND)
        chat_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_granted_session_closed_before_acquisition_is_not_minted_afresh(
        self, state, chat_run, monkeypatch
    ) -> None:
        """The slot the grant approved must be the slot the request runs on."""
        user = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        await _persist(state, user)

        def close_meanwhile(_agent):
            state._slots.pop("s1", None)
            return True

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.members_mod.is_configured_dispatchable_member",
            close_meanwhile,
        )
        with patch(_GRANT, return_value=True):
            async with _client(state, APP) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"slot": "s1", "message": "x", "agent": "Someone Configured"},
                )
                assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        chat_run.assert_not_called()
        assert "s1" not in state._slots
        assert not state.conversation_log.get_metadata("dashboard:s1").get("app")

    @pytest.mark.asyncio
    async def test_a_new_app_slot_is_still_created(self, state) -> None:
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots", json={"name": "brand-new"})
            assert resp.status == 200, await resp.text()
        assert state._slots["brand-new"]._app == APP


@pytest.fixture
def case_insensitive_fs(monkeypatch):
    """Transcript paths that fold letter case, as the macOS and Windows defaults do."""
    from kiro_crew.history import ConversationLog

    real_path = ConversationLog._path

    def folded(self, key):
        path = real_path(self, key)
        return path.with_name(path.name.lower())

    monkeypatch.setattr(ConversationLog, "_path", folded)


class TestALetterCaseTwinIsNotANewSession:
    """A key one live slot holds in another letter case names that slot's transcript."""

    PERSON = "chat-1-1790951058"

    @pytest.mark.asyncio
    async def test_send_create_and_resume(
        self, state, chat_run, sel_spy, case_insensitive_fs
    ) -> None:
        from kiro_crew.dashboard.slot_ownership import CASE_ALIAS_DENIED

        await _persist_and_close(state, state.get_or_create_slot("appconv", app=APP))
        # The person's new tab: live, with nothing written to its transcript yet.
        state.get_or_create_slot(self.PERSON, origin=SlotOrigin.USER)
        assert not state.conversation_log.has_log(f"dashboard:{self.PERSON}")
        twin = self.PERSON.upper()
        live = set(state._slots)
        with patch(_GRANT, return_value=True):
            async with _client(state, APP) as client:
                send = await client.post("/api/chat?ws=1", json={"slot": twin, "message": "x"})
                create = await client.post("/api/chat/slots", json={"name": twin})
                resume = await client.post(
                    f"/api/chat/slots/{twin}/resume", json={"key": "dashboard:appconv"}
                )
                for resp in (send, create, resume):
                    assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        chat_run.assert_not_called()
        assert set(state._slots) == live
        assert [d["error"] for d in sel_spy.denials()] == [CASE_ALIAS_DENIED] * 3
        assert state.conversation_log.get_metadata("dashboard:appconv").get("closed")

    @pytest.mark.asyncio
    async def test_a_twin_of_a_key_under_construction(self, state, sel_spy) -> None:
        state._slots_under_construction.add("uc-1")
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots", json={"name": "UC-1"})
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert "UC-1" not in state._slots
        assert sel_spy.denials() == []

    @pytest.mark.asyncio
    async def test_the_apps_exact_key_and_an_unrelated_key_are_unaffected(self, state) -> None:
        state.get_or_create_slot(self.PERSON, origin=SlotOrigin.USER)
        state.get_or_create_slot("mine", app=APP)
        async with _client(state, APP) as client:
            for name in ("mine", "chat-2-1790951058"):
                resp = await client.post("/api/chat/slots", json={"name": name})
                assert resp.status == 200, await resp.text()


class TestModeChanges:
    @pytest.mark.asyncio
    async def test_an_unknown_slot_is_the_same_404_as_a_foreign_one(self, state) -> None:
        state.get_or_create_slot("c1", origin=SlotOrigin.CRON)
        with patch(_GRANT, return_value=True):
            async with _client(state, APP) as client:
                bodies = []
                for name in ("absent", "c1"):
                    resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": name})
                    bodies.append((resp.status, await resp.json()))
        assert bodies == [(404, _NOT_FOUND), (404, _NOT_FOUND)]


# ── binders never adopt an app-owned slot ────────────────────────────────────


class TestBindersDoNotAdoptAnAppSlot:
    def test_a_cron_result_is_not_bound_into_an_app_slot(self, state, sel_spy) -> None:
        from kiro_crew.cron import CronJob
        from kiro_crew.dashboard.cron_inject import inject_cron_result_to_dashboard

        squatter = state.get_or_create_slot("cron-j1", app=APP)
        job = CronJob(id="j1", name="nightly", message="report", agent_id="kirocrew")
        inject_cron_result_to_dashboard(state, job, "THE PERSON'S CRON RESULT", history=None)
        assert squatter.linked_session_key == ""
        assert not any("CRON RESULT" in str(m.get("content")) for m in squatter.messages)
        # Filed under the scheduler that asked for the bind, never the holder app.
        assert [(d["caller"], d["resources"]) for d in sel_spy.denials()] == [
            ("gateway", f"slot=cron-j1 holder={APP}")
        ]

    def test_a_workflow_result_is_not_bound_into_an_app_slot(self, state, sel_spy) -> None:
        from kiro_crew.dashboard.workflow_inject import inject_workflow_result

        squatter = state.get_or_create_slot("workflow-r1", app=APP)
        snapshot = {
            "name": "release",
            "run_id": "r1",
            "status": "finished",
            "session_key": "dashboard:gone",
            "result": {"ok": True},
        }
        assert inject_workflow_result(state, "r1", snapshot) is False
        assert squatter.linked_session_key == ""
        assert squatter.messages == []
        assert [(d["caller"], d["resources"]) for d in sel_spy.denials()] == [
            ("gateway", f"slot=workflow-r1 holder={APP}")
        ]

    @pytest.mark.asyncio
    async def test_to_chat_files_the_refusal_under_the_person(self, state, sel_spy) -> None:
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.handlers.cron import api_cron_to_chat

        state.get_or_create_slot("cron-j1", app=APP)
        app = web.Application()
        app["state"] = state
        request = make_mocked_request(
            "POST", "/api/crons/j1/to-chat", match_info={"job_id": "j1"}, app=app
        )
        resp = await api_cron_to_chat(request)
        assert resp.status == 409
        assert [(d["caller"], d["operation"]) for d in sel_spy.denials()] == [
            ("dashboard", "cron.to_chat")
        ]

    def test_a_scheduled_run_reads_nothing_for_an_app_slot_under_the_jobs_key(
        self, state, sel_spy, monkeypatch
    ) -> None:
        """The run-start pre-create stands down before any transcript read or audit row."""
        import asyncio

        from kiro_crew.cron import CronJob
        from kiro_crew.dashboard.cron_inject import (
            ensure_cron_slot,
            prefetch_cron_dismissed,
            prefetch_cron_history,
        )

        squatter = state.get_or_create_slot("cron-j1", app=APP)
        log = state.conversation_log
        reads: list[str] = []

        def read_messages(key, *a, **kw):
            reads.append(key)
            return []

        def get_metadata_status(key):
            reads.append(key)
            return {}, True

        monkeypatch.setattr(log, "read_messages", read_messages)
        monkeypatch.setattr(log, "get_metadata_status", get_metadata_status)
        job = CronJob(
            id="j1", name="nightly", message="report", agent_id="kirocrew", persistent_session=True
        )

        async def run() -> None:
            for _ in range(3):
                await ensure_cron_slot(state, job)
            assert await prefetch_cron_history(state, "j1") is None
            await prefetch_cron_dismissed(state, "j1")

        asyncio.run(run())
        assert reads == []
        assert sel_spy.denials() == []
        assert squatter.linked_session_key == ""
