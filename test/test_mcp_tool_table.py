"""The MCP tool table and the dashboard port, tested through their interfaces.

``ToolTable`` is the whole tool surface of a table-built server (today
``kirocrew-dashboard``): ``list()`` answers ``tools/list`` and ``call()`` is one
``tools/call`` frame. ``DashboardClient`` is the port a tool reaches the gateway
through, with a loopback adapter (``mcp_core``'s request helpers) and an
in-memory one. Each test drives one of those interfaces and asserts what comes
out of it: the reply text, the requests the dashboard saw, the audit row.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew import mcp_core, mcp_shared
from kiro_crew.mcp_tools.dashboard_client import (
    DashboardError,
    DashboardRequest,
    InMemoryDashboardClient,
    LoopbackDashboardClient,
    UndeclaredRoute,
    UnroutedRequest,
    restrict_routes,
)
from kiro_crew.mcp_tools.table import Caller, GatewayCaller, Tool, ToolContext, ToolTable
from kiro_crew.validation import FieldSpec, ToolSchema

_SERVER = "kirocrew-test-table"
_SCHEMA = ToolSchema(
    tool_name="needs_name",
    fields=[FieldSpec(name="name", type=str, required=True, max_len=10)],
)


class _Audit:
    """Stands in for the SEL: the rows ``call_tool_with_logging`` writes."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_tool_invocation(self, **row: Any) -> None:
        self.rows.append(row)


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> _Audit:
    rec = _Audit()
    monkeypatch.setattr(mcp_shared, "sel", lambda: rec)
    return rec


def _echo(args: dict[str, Any], ctx: ToolContext) -> str:
    return f"ran with {json.dumps(args, sort_keys=True)} as {ctx.caller_key or '-'}"


def _gate(ctx: ToolContext) -> tuple[str, str]:
    return ctx.caller.require_strict_session_key("Error: who are you?")


def _table(*tools: Tool, **kw: Any) -> ToolTable:
    kw.setdefault("strict_gate", _gate)
    return ToolTable(_SERVER, tools, **kw)


def _ctx(routes: dict[str, Any] | None = None, caller: Any = None) -> ToolContext:
    return ToolContext(
        client=InMemoryDashboardClient(routes or {}),
        caller=caller if caller is not None else Caller.strict("dashboard:chat-1"),
    )


def _requests(ctx: ToolContext) -> list[DashboardRequest]:
    assert isinstance(ctx.client, InMemoryDashboardClient)
    return ctx.client.requests


class TestCall:
    def test_an_unknown_name_gets_the_tables_reply(self, audit: _Audit) -> None:
        assert _table().call("x{y}", {}, _ctx()) == "Error: unknown tool 'x{y}'"
        # The "Error:" prefix is what the audit row classifies as a failure.
        assert [r["outcome"] for r in audit.rows] == ["failed"]

    def test_arguments_are_validated_before_the_body_runs(self, audit: _Audit) -> None:
        ran: list[dict] = []

        def body(args: dict[str, Any], ctx: ToolContext) -> str:
            ran.append(args)
            return "ok"

        table = _table(
            Tool("needs_name", "d", {"type": "object"}, body, "attribution"),
            validators={"needs_name": _SCHEMA},
        )
        out = table.call("needs_name", {"name": "x" * 11}, _ctx())
        assert out.startswith("Error: name: exceeds max length 10")
        assert ran == []
        assert audit.rows[-1]["error"] == "validation_failed"
        assert table.call("needs_name", {"name": "ok"}, _ctx()) == "ok"
        assert ran == [{"name": "ok"}]

    def test_a_tool_with_no_registered_schema_gets_its_arguments_as_sent(self) -> None:
        table = _table(Tool("raw", "d", {"type": "object"}, _echo, "attribution"))
        assert table.call("raw", {"anything": [1]}, _ctx()) == 'ran with {"anything": [1]} as -'
        assert table.validate("raw", {"a": 1}) == {"a": 1}

    def test_a_strict_row_runs_with_the_key_its_gate_verified(self) -> None:
        table = _table(Tool("strict", "d", {"type": "object"}, _echo, "strict"))
        out = table.call("strict", {}, _ctx(caller=Caller.strict("dashboard:chat-7")))
        assert out == "ran with {} as dashboard:chat-7"

    def test_a_strict_refusal_is_the_reply_and_nothing_runs_or_is_sent(self) -> None:
        sent: list[str] = []

        def body(args: dict[str, Any], ctx: ToolContext) -> str:
            sent.append("ran")
            ctx.client.post("/api/x")
            return "ok"

        ctx = _ctx(
            {"POST /api/x": {"ok": True}},
            caller=Caller.unverified("dashboard:parent", diagnosis=" [why]"),
        )
        table = _table(Tool("strict", "d", {"type": "object"}, body, "strict", ("POST /api/x",)))
        assert table.call("strict", {}, ctx) == "Error: who are you? [why]"
        assert sent == [] and _requests(ctx) == []

    def test_only_a_strict_row_is_gated(self) -> None:
        gated: list[str] = []

        def gate(ctx: ToolContext) -> tuple[str, str]:
            gated.append("gate")
            return "", "Error: refused"

        table = _table(
            Tool("free", "d", {"type": "object"}, _echo, "attribution"),
            strict_gate=gate,
        )
        assert table.call("free", {}, _ctx()) == "ran with {} as -"
        assert gated == []

    def test_the_audit_row_carries_the_attribution_key(self, audit: _Audit) -> None:
        """The frame's attribution key, or the server name when there is none."""
        table = _table(Tool("attributed", "d", {"type": "object"}, _echo, "attribution"))
        table.call("attributed", {}, _ctx(caller=Caller.unverified("dashboard:lenient")))
        table.call("attributed", {}, _ctx(caller=Caller.unverified("")))
        table.call("unknown", {}, _ctx(caller=Caller.unverified("dashboard:lenient")))
        assert [r["session_key"] for r in audit.rows] == [
            "dashboard:lenient",
            _SERVER,
            "dashboard:lenient",
        ]
        assert {r["downstream_service"] for r in audit.rows} == {_SERVER}

    def test_a_body_reaches_only_the_routes_its_row_declares(self) -> None:
        """Mutation proof for the declared-routes client.

        The body asks for a route its row does not list. The table's client must
        refuse it before the dashboard sees anything; dropping the restriction
        (passing ``ctx.client`` through unscoped) lets the request through and
        this fails on both asserts.
        """

        def body(args: dict[str, Any], ctx: ToolContext) -> str:
            ctx.client.get("/api/chat/folders")
            ctx.client.post("/api/chat/folders/reorder", {})
            return "ok"

        ctx = _ctx({"GET /api/chat/folders": [], "POST /api/chat/folders/reorder": {"ok": True}})
        table = _table(
            Tool("t", "d", {"type": "object"}, body, "attribution", ("GET /api/chat/folders",))
        )
        with pytest.raises(UndeclaredRoute, match="POST /api/chat/folders/reorder"):
            table.call("t", {}, ctx)
        assert [r.route for r in _requests(ctx)] == ["GET /api/chat/folders"]

    def test_the_body_runs_on_arguments_validated_twice(self) -> None:
        """The first pass strips the hidden mark only after composing; a second composes.

        Mutation proof: dropping the second pass hands the body ``cafe\u0301``.
        """
        seen: list[str] = []

        def body(args: dict[str, Any], ctx: ToolContext) -> str:
            seen.append(args["name"])
            return "ok"

        table = _table(
            Tool("needs_name", "d", {"type": "object"}, body, "strict"),
            validators={"needs_name": _SCHEMA},
        )
        assert table.call("needs_name", {"name": "cafe\u200b\u0301"}, _ctx()) == "ok"
        assert seen == ["caf\u00e9"]

    def test_a_caller_supplied_key_never_reaches_a_non_strict_body(self) -> None:
        """``caller_key`` is only ever the strict gate's verified key."""
        table = _table(Tool("t", "d", {"type": "object"}, _echo, "attribution"))
        ctx = ToolContext(InMemoryDashboardClient({}), Caller.strict("k"), caller_key="forged")
        assert table.call("t", {}, ctx) == "ran with {} as -"

    def test_the_strict_gate_reaches_only_the_rows_routes(self) -> None:
        """The gate runs inside the row's call, so it gets the row's client too.

        Mutation proof: handing the gate the unscoped ``ctx`` lets its request
        through, and this fails on both asserts.
        """

        def gate(ctx: ToolContext) -> tuple[str, str]:
            ctx.client.post("/api/session-control/zz", {})
            return "dashboard:chat-1", ""

        ctx = _ctx({"POST /api/session-control/zz": {"ok": True}})
        table = _table(Tool("t", "d", {"type": "object"}, _echo, "strict"), strict_gate=gate)
        with pytest.raises(UndeclaredRoute, match="POST /api/session-control/zz"):
            table.call("t", {}, ctx)
        assert _requests(ctx) == []

    def test_a_body_exception_reaches_the_caller(self) -> None:
        def body(args: dict[str, Any], ctx: ToolContext) -> str:
            raise RuntimeError("wire down")

        table = _table(Tool("t", "d", {"type": "object"}, body, "attribution"))
        with pytest.raises(RuntimeError, match="wire down"):
            table.call("t", {}, _ctx())

    def test_construction_refuses_an_inconsistent_table(self) -> None:
        row = Tool("t", "d", {"type": "object"}, _echo, "attribution")
        with pytest.raises(ValueError, match="declared twice"):
            ToolTable(_SERVER, (row, row))
        with pytest.raises(ValueError, match="needs a strict_gate"):
            ToolTable(_SERVER, (Tool("s", "d", {}, _echo, "strict"),))
        for identity in ("loose", "none"):
            with pytest.raises(ValueError, match="has identity"):
                ToolTable(_SERVER, (Tool("s", "d", {}, _echo, identity),))  # type: ignore[arg-type]


class TestList:
    def test_descriptors_come_in_row_order_and_fresh_each_call(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        table = _table(
            Tool("b_tool", "B", schema, _echo, "attribution"),
            Tool("a_tool", "A", {"type": "object"}, _echo, "strict"),
        )
        first = table.list()
        assert [t["name"] for t in first] == ["b_tool", "a_tool"]
        assert list(first[0]) == ["name", "description", "inputSchema"]
        first[0]["inputSchema"]["properties"]["a"]["type"] = "integer"
        assert table.list()[0]["inputSchema"] == schema
        assert table.names() == ("b_tool", "a_tool")
        assert table.names("strict") == ("a_tool",)

    def test_a_declared_title_is_added(self) -> None:
        table = ToolTable(
            "kirocrew-dashboard",
            (Tool("chat_folder_tree", "d", {"type": "object"}, _echo, "attribution"),),
        )
        (listed,) = table.list()
        assert listed["title"] and list(listed)[-1] == "title"


class TestCaller:
    def test_a_strict_caller_passes_every_strict_check(self) -> None:
        caller = Caller.strict("dashboard:chat-1")
        assert caller.require_strict_session_key("Error: no") == ("dashboard:chat-1", "")
        assert caller.attribution() == "dashboard:chat-1"

    def test_an_unverified_caller_is_refused_with_its_diagnosis(self) -> None:
        caller = Caller.unverified("dashboard:parent", diagnosis=" (no channel)")
        assert caller.require_strict_session_key("Error: no") == ("", "Error: no (no channel)")
        assert caller.attribution() == "dashboard:parent"
        assert Caller.unverified().require_strict_session_key("Error: no") == ("", "Error: no")

    def test_the_gateway_caller_asks_mcp_core_at_call_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        caller = GatewayCaller("kirocrew-dashboard")
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-9")
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:walked")
        assert caller.require_strict_session_key("Error: no") == ("dashboard:chat-9", "")
        assert caller.attribution() == "dashboard:walked"
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
        monkeypatch.setattr(
            mcp_core, "strict_identity_diagnosis", lambda server="kirocrew-core": f" [{server}]"
        )
        assert caller.require_strict_session_key("Error: no") == (
            "",
            "Error: no [kirocrew-dashboard]",
        )

    def test_the_production_frame_context(self) -> None:
        ctx = ToolContext.for_frame("kirocrew-dashboard")
        assert isinstance(ctx.client, LoopbackDashboardClient)
        assert isinstance(ctx.caller, GatewayCaller) and ctx.caller.server == "kirocrew-dashboard"
        assert ctx.caller_key == ""


class _Helpers:
    """``mcp_core``'s five request helpers, recorded instead of sent."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reply: Any = {"ok": True}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        for name in ("_get", "_post", "_patch", "_put", "_delete"):
            monkeypatch.setattr(mcp_core, name, self._recorder(name))

    def _recorder(self, name: str) -> Any:
        def helper(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            return self.reply

        return helper


@pytest.fixture
def helpers(monkeypatch: pytest.MonkeyPatch) -> _Helpers:
    return _Helpers(monkeypatch)


class TestLoopbackClient:
    def test_each_verb_is_the_matching_mcp_core_helper(self, helpers: _Helpers) -> None:
        """Read at call time, so a rebinding in ``mcp_core`` reaches it, and the
        transport's own defaults apply wherever the caller passes nothing."""
        client = LoopbackDashboardClient()
        client.get("/api/a")
        client.get("/api/a?x=1", session_key="dashboard:v", timeout=3)
        client.post("/api/b", {"k": 1})
        client.post("/api/b", None, session_key="dashboard:v", timeout=99)
        client.patch("/api/c", {"k": 2}, session_key="dashboard:v")
        client.put("/api/d", {"k": 3})
        client.delete("/api/e?if_empty=true", session_key="dashboard:v")
        assert helpers.calls == [
            ("_get", ("/api/a", None), {}),
            ("_get", ("/api/a?x=1", "dashboard:v"), {"timeout": 3}),
            ("_post", ("/api/b", {"k": 1}), {"session_key": None}),
            ("_post", ("/api/b", None), {"timeout": 99, "session_key": "dashboard:v"}),
            ("_patch", ("/api/c", {"k": 2}), {"session_key": "dashboard:v"}),
            ("_put", ("/api/d", {"k": 3}), {"session_key": None}),
            ("_delete", ("/api/e?if_empty=true", None), {"session_key": "dashboard:v"}),
        ]

    @pytest.mark.parametrize(
        "reply",
        [{"ok": True}, {"error": ""}, {"error": None, "code": "x"}, [], [{"id": 1}], "text"],
    )
    def test_a_reply_without_a_truthy_error_is_returned(
        self, helpers: _Helpers, reply: Any
    ) -> None:
        helpers.reply = reply
        assert LoopbackDashboardClient().get("/api/a") == reply

    def test_a_refusal_is_raised_with_its_code_and_body(self, helpers: _Helpers) -> None:
        helpers.reply = {"error": "stale", "code": "stale_base", "extra": 1}
        with pytest.raises(DashboardError) as raised:
            LoopbackDashboardClient().put("/api/d", {})
        assert raised.value.error == "stale"
        assert raised.value.code == "stale_base"
        assert raised.value.body == {"error": "stale", "code": "stale_base", "extra": 1}
        assert str(raised.value) == "stale"

    def test_a_non_string_code_reads_as_none(self, helpers: _Helpers) -> None:
        helpers.reply = {"error": {"nested": 1}, "code": 7}
        with pytest.raises(DashboardError) as raised:
            LoopbackDashboardClient().get("/api/a")
        assert raised.value.code == "" and raised.value.error == {"nested": 1}


class TestInMemoryClient:
    def test_it_answers_from_its_routes_and_records_each_request(self) -> None:
        rows = [{"id": "a"}]
        client = InMemoryDashboardClient(
            {
                "GET /api/rows": rows,
                "PATCH /api/rows/{id}/pin": lambda req: {"pinned": req.body["pinned"]},
                "POST /api/boom": RuntimeError("down"),
                "PUT /api/refuse": {"error": "no", "code": "app_forbidden"},
            }
        )
        got = client.get("/api/rows?x=1")
        got.append({"id": "mutated"})
        assert client.get("/api/rows") == [{"id": "a"}]
        assert client.patch("/api/rows/r%2F1/pin", {"pinned": True}, session_key="k") == {
            "pinned": True
        }
        with pytest.raises(RuntimeError, match="down"):
            client.post("/api/boom")
        with pytest.raises(DashboardError) as raised:
            client.put("/api/refuse", {"a": 1})
        assert raised.value.code == "app_forbidden"
        with pytest.raises(UnroutedRequest, match="DELETE /api/rows"):
            client.delete("/api/rows")
        assert [r.route for r in client.requests] == [
            "GET /api/rows",
            "GET /api/rows",
            "PATCH /api/rows/r%2F1/pin",
            "POST /api/boom",
            "PUT /api/refuse",
            "DELETE /api/rows",
        ]
        assert client.requests[2].session_key == "k"
        assert [r.path for r in client.sent("GET /api/rows")] == ["/api/rows?x=1", "/api/rows"]

    def test_the_body_it_records_is_a_copy(self) -> None:
        client = InMemoryDashboardClient({"POST /api/x": {"ok": True}})
        body = {"tags": ["a"]}
        client.post("/api/x", body, timeout=5)
        body["tags"].append("b")
        assert client.requests[0].body == {"tags": ["a"]}
        assert client.requests[0].timeout == 5


class TestRestrictRoutes:
    def test_an_undeclared_route_never_reaches_the_inner_client(self) -> None:
        inner = InMemoryDashboardClient({"GET /api/a": {}, "DELETE /api/a/{id}": {}})
        scoped = restrict_routes(inner, ("GET /api/a", "DELETE /api/a/{id}"))
        scoped.get("/api/a?q=1")
        scoped.delete("/api/a/x/y?if_empty=true")
        for call in (
            lambda: scoped.post("/api/a"),
            lambda: scoped.get("/api/a/b"),
            lambda: scoped.delete("/api/a/"),
            lambda: scoped.put("/api/a", {}),
            lambda: scoped.patch("/api/a", {}),
        ):
            with pytest.raises(UndeclaredRoute):
                call()
        assert [r.route for r in inner.requests] == ["GET /api/a", "DELETE /api/a/x/y"]
