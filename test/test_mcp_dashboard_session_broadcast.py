"""The tool layer for `session_broadcast` and `session_status`: what it forwards,
and what it reports back.

Both tools return PROSE the model reads and acts on, so the report is the contract.
Two shapes matter most and neither is visible from the API's own tests.

For a broadcast: partial delivery is the normal outcome, so a report that reads as
all-or-nothing is the defect — a model told "queued for 2 sessions" when three were
asked will not go looking for the third. Every row's outcome is asserted on its
own, including the steer that quietly fell back to a queue, for the reason
`session_send`'s own report separates them.

For a status listing: a short list means opposite things depending on how good the
durable read was, so the `tree` flag must reach the prose. A list rendered without
it lets a model conclude "you created one worker" from an answer that could not see
the other seven.
"""

from __future__ import annotations

import pytest

from kiro_crew import mcp_dashboard as md
from kiro_crew import validation
from kiro_crew.dashboard import session_control as sc
from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest, InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext
from kiro_crew.validation import (
    SESSION_BROADCAST_SCHEMA,
    ValidationError,
    validate_tool_args,
)


def _frame(tool: str, args: dict, route: str, resp: dict) -> tuple[str, DashboardRequest]:
    """One frame of ``tool``; the reply text and the one request ``route`` got."""
    dash = InMemoryDashboardClient({route: resp})
    out = TABLE.call(tool, args, ToolContext(dash, Caller.strict("dashboard:chat-1-100")))
    (request,) = dash.requests
    return out, request


def _bc(args: dict, resp: dict) -> tuple[str, DashboardRequest]:
    return _frame("session_broadcast", args, "POST /api/session-control/broadcast", resp)


def _st(resp: dict) -> tuple[str, DashboardRequest]:
    return _frame("session_status", {}, "GET /api/session-control/status", resp)


def _row(target: str, **kw) -> dict:
    return {"target": target, "ok": True, "started": False, "steered": False, **kw}


class TestBroadcastForwarding:
    def test_the_mode_and_message_go_to_the_broadcast_route(self) -> None:
        _, sent = _bc(
            {"message": "rebase first", "mode": "queue", "all": True},
            {"ok": True, "mode": "queue", "requested": 1, "delivered": 1, "results": []},
        )
        assert sent.path == "/api/session-control/broadcast"
        assert sent.body == {"message": "rebase first", "mode": "queue", "all": True}
        assert sent.session_key == "dashboard:chat-1-100"

    def test_targets_are_forwarded_when_named(self) -> None:
        _, sent = _bc(
            {"message": "stop", "mode": "steer", "targets": ["chat-2", "chat-3"]},
            {"ok": True, "mode": "steer", "requested": 2, "delivered": 2, "results": []},
        )
        assert sent.body["targets"] == ["chat-2", "chat-3"]

    def test_the_request_timeout_grows_with_the_target_cap(self) -> None:
        """The client budget must exceed the backend's worst delivery path.

        The backend delivers SEQUENTIALLY, so the 30-second default expires
        mid-fleet and the tool then reports a failure for a broadcast the server
        delivered — discarding the per-target report, and inviting a retry that
        delivers the whole message twice.

        Asserted from the named cap, per-target allowance, and response margin so
        a change to any one moves the expected timeout with it. The strict
        inequality is the contract: equality leaves no time for the per-target
        gate and audit work, the broadcast audit write, or the HTTP response.
        """
        _, sent = _bc(
            {"message": "stop", "mode": "steer", "all": True},
            {"ok": True, "mode": "steer", "requested": 0, "delivered": 0, "results": []},
        )
        timeout = sent.timeout
        assert timeout is not None
        worst_case_delivery = md.MAX_BROADCAST_TARGETS * md.BROADCAST_TARGET_ALLOWANCE_SECS
        assert timeout == worst_case_delivery + md.BROADCAST_RESPONSE_MARGIN_SECS
        assert timeout > worst_case_delivery
        assert timeout > 30, "the default would expire mid-fleet"

    def test_all_true_is_forwarded_and_no_targets_key_is_invented(self) -> None:
        """`all: true` is the opt-in to every created session; it travels as a bare
        boolean and the client adds no `targets` key beside it."""
        _, sent = _bc(
            {"message": "rebase", "mode": "queue", "all": True},
            {"ok": True, "mode": "queue", "requested": 0, "delivered": 0, "results": []},
        )
        assert sent.body == {"message": "rebase", "mode": "queue", "all": True}

    def test_an_explicit_empty_targets_list_is_forwarded_as_given(self) -> None:
        """A gateway older than this tool refuses an explicit empty list but reads a
        bare body as "every session you created". Dropping `[]` here would turn an
        empty audience into a fleet-wide send when the two versions meet."""
        resp = {"ok": True, "mode": "queue", "requested": 0, "delivered": 0, "results": []}
        _, sent = _bc({"message": "go", "mode": "queue", "targets": []}, resp)
        assert sent.body == {"message": "go", "mode": "queue", "targets": []}

    def test_an_empty_targets_list_beside_all_is_forwarded_with_it(self) -> None:
        resp = {"ok": True, "mode": "queue", "requested": 0, "delivered": 0, "results": []}
        _, sent = _bc({"message": "go", "mode": "queue", "all": True, "targets": []}, resp)
        assert sent.body == {"message": "go", "mode": "queue", "all": True, "targets": []}

    def test_a_null_targets_is_not_forwarded(self) -> None:
        """`null` is the validator's default for an absent field, not a list."""
        resp = {"ok": True, "mode": "queue", "requested": 0, "delivered": 0, "results": []}
        out, sent = _bc({"message": "go", "mode": "queue", "all": True, "targets": None}, resp)
        assert "NoneType" not in out
        assert sent.body == {"message": "go", "mode": "queue", "all": True}

    @pytest.mark.parametrize("args", [{}, {"targets": None}, {"all": False}, {"all": None}])
    def test_a_call_that_names_no_audience_sends_no_request(self, args) -> None:
        """Kiro CLI drops `targets: []`, so "no audience" arrives as a missing key.
        A body without `targets` is read by a gateway older than this tool as every
        session the caller created, so the tool refuses locally and sends nothing."""
        dash = InMemoryDashboardClient({"POST /api/session-control/broadcast": {"ok": True}})
        ctx = ToolContext(dash, Caller.strict("dashboard:chat-1-100"))
        out = TABLE.call("session_broadcast", {"message": "stop", "mode": "steer", **args}, ctx)
        assert dash.requests == []
        assert out.startswith("Error:")
        assert "all: true" in out and "target_required" in out
        assert "Nothing was sent" in out

    def test_an_explicit_empty_targets_list_alone_reaches_the_backend_to_be_refused(self) -> None:
        """An explicit `[]` is forwarded: a gateway older than this tool refuses it
        and the new backend refuses it as `target_required`."""
        refusal = {
            "error": "no audience selected: name the sessions in targets, or pass all: true",
            "code": "target_required",
        }
        out, sent = _bc({"message": "stop", "mode": "steer", "targets": []}, refusal)
        assert sent.body == {"message": "stop", "mode": "steer", "targets": []}
        assert out.startswith("Error:") and "all: true" in out

    def test_targets_beside_all_are_both_forwarded_for_the_backend_to_refuse(self) -> None:
        refusal = {"error": "pass targets or all: true, not both", "code": "audience_conflict"}
        out, sent = _bc(
            {"message": "stop", "mode": "queue", "all": True, "targets": ["chat-2"]}, refusal
        )
        assert sent.body["all"] is True and sent.body["targets"] == ["chat-2"]
        assert out.startswith("Error:") and "not both" in out

    def test_a_non_boolean_all_is_refused_without_a_request(self) -> None:
        """A string such as "false" is truthy in Python; it must never select the fleet."""
        dash = InMemoryDashboardClient({"POST /api/session-control/broadcast": {"ok": True}})
        ctx = ToolContext(dash, Caller.strict("dashboard:chat-1-100"))
        out = TABLE.call(
            "session_broadcast", {"message": "stop", "mode": "queue", "all": "false"}, ctx
        )
        assert out.startswith("Error:")
        assert dash.requests == []


class TestBroadcastReports:
    def test_a_queue_broadcast_reports_each_target_and_the_tally(self) -> None:
        out, _ = _bc(
            {"message": "rebase", "mode": "queue", "all": True},
            {
                "ok": True,
                "mode": "queue",
                "requested": 2,
                "delivered": 2,
                "results": [_row("chat-2"), _row("chat-3", started=True)],
            },
        )
        assert "2/2" in out
        assert "chat-2" in out and "chat-3" in out
        assert "queued until its turn ends" in out
        assert "started a turn on it" in out

    def test_a_steer_broadcast_says_it_cut_into_the_running_turns(self) -> None:
        out, _ = _bc(
            {"message": "stop", "mode": "steer", "all": True},
            {
                "ok": True,
                "mode": "steer",
                "requested": 1,
                "delivered": 1,
                "results": [_row("chat-2", steered=True)],
            },
        )
        assert "Steered" in out
        assert "cut into its running turn" in out

    def test_a_steer_that_fell_back_to_a_queue_says_so_for_that_target(self) -> None:
        """The caller asked for an interruption and one target did not get one.
        Reporting a plain queue would leave it believing every target was cut into."""
        out, _ = _bc(
            {"message": "stop", "mode": "steer", "all": True},
            {
                "ok": True,
                "mode": "steer",
                "requested": 2,
                "delivered": 2,
                "results": [_row("chat-2", steered=True), _row("chat-3")],
            },
        )
        assert "cut into its running turn" in out
        assert "steer fell back to the queue" in out

    def test_a_queue_broadcast_never_mentions_steering(self) -> None:
        out, _ = _bc(
            {"message": "rebase", "mode": "queue", "all": True},
            {
                "ok": True,
                "mode": "queue",
                "requested": 1,
                "delivered": 1,
                "results": [_row("chat-2")],
            },
        )
        assert "steer" not in out.lower()

    def test_a_refused_target_is_a_visible_row_with_its_code(self) -> None:
        """A model that cannot see WHICH target was missed cannot recover the
        delivery, and nothing retries it."""
        out, _ = _bc(
            {"message": "rebase", "mode": "queue", "all": True},
            {
                "ok": True,
                "mode": "queue",
                "requested": 2,
                "delivered": 1,
                "results": [
                    _row("chat-2"),
                    {
                        "target": "chat-9",
                        "ok": False,
                        "code": "target_not_found",
                        "error": "no open session matches 'chat-9'",
                    },
                ],
            },
        )
        assert "1/2" in out
        assert "chat-9" in out and "target_not_found" in out
        assert "Some targets were not reached" in out
        assert "Nothing\nretries them" in out or "Nothing retries them" in out

    def test_a_fully_delivered_broadcast_does_not_warn_about_missed_targets(self) -> None:
        """A warning on every call is a warning nobody reads."""
        out, _ = _bc(
            {"message": "rebase", "mode": "queue", "all": True},
            {
                "ok": True,
                "mode": "queue",
                "requested": 1,
                "delivered": 1,
                "results": [_row("chat-2")],
            },
        )
        assert "were not reached" not in out

    def test_an_empty_audience_is_reported_as_a_state_not_a_failure(self) -> None:
        """A conductor before its first dispatch is in this state, and "delivered 0
        of 0" reads like something broke."""
        out, _ = _bc(
            {"message": "rebase", "mode": "queue", "all": True},
            {
                "ok": True,
                "mode": "queue",
                "requested": 0,
                "delivered": 0,
                "audience_empty": True,
                "results": [],
            },
        )
        assert "Nothing to broadcast to" in out
        assert "0/0" not in out

    def test_an_api_refusal_is_surfaced_as_an_error(self) -> None:
        out, _ = _bc(
            {"message": "rebase", "mode": "queue", "all": True},
            {"error": "a broadcast reaches at most 32 sessions"},
        )
        assert out.startswith("Error:")
        assert "at most 32" in out


class TestStatusReports:
    def test_it_reads_the_status_route(self) -> None:
        _, sent = _st({"ok": True, "caller": "chat-1", "tree": "readable", "sessions": []})
        assert sent.path == "/api/session-control/status"
        assert sent.session_key == "dashboard:chat-1-100"

    def test_each_live_row_shows_its_status_and_title(self) -> None:
        out, _ = _st(
            {
                "ok": True,
                "caller": "chat-1",
                "tree": "readable",
                "sessions": [
                    {
                        "target": "chat-2",
                        "title": "rebase the PR",
                        "status": "working",
                        "running": True,
                        "queue_depth": 0,
                        "source": "live",
                    },
                    {
                        "target": "chat-3",
                        "title": "write the doc",
                        "status": "queued",
                        "running": False,
                        "queue_depth": 2,
                        "source": "live",
                    },
                ],
            }
        )
        assert "chat-2" in out and "rebase the PR" in out and "working" in out
        assert "chat-3" in out and "queued" in out and "2 queued" in out

    def test_a_lost_row_says_the_dashboard_no_longer_holds_it(self) -> None:
        """`lost` is a row a live-only list cannot produce, so the prose has to
        distinguish it — a model that reads it as merely idle will try to message a
        session that is not there."""
        out, _ = _st(
            {
                "ok": True,
                "caller": "chat-1",
                "tree": "readable",
                "sessions": [{"target": "chat-7", "status": "lost", "source": "crew_log"}],
            }
        )
        assert "chat-7" in out and "lost" in out
        assert "idle" not in out

    def test_a_closed_row_says_it_finished_and_shows_its_stamp(self) -> None:
        """`closed` is the worker that finished and closed its tab — the prose must
        read as 'nothing to do', the opposite of `lost`, and surface ``closed_at``
        as a human-readable UTC time rather than a raw epoch number."""
        out, _ = _st(
            {
                "ok": True,
                "caller": "chat-1",
                "tree": "readable",
                "sessions": [
                    {
                        "target": "chat-7",
                        "status": "closed",
                        "source": "crew_log",
                        "closed_at": 1730000000000,
                    }
                ],
            }
        )
        assert "chat-7" in out and "closed" in out
        # Epoch MILLISECONDS rendered as UTC ISO-8601, not the raw number.
        assert "2024-10-27T03:33:20Z" in out
        assert "1730000000000" not in out
        assert "idle" not in out

    def test_an_incomplete_tree_says_the_count_is_a_floor(self) -> None:
        out, _ = _st(
            {
                "ok": True,
                "caller": "chat-1",
                "tree": "incomplete",
                "sessions": [
                    {
                        "target": "chat-2",
                        "title": "t",
                        "status": "idle",
                        "running": False,
                        "queue_depth": 0,
                        "source": "live",
                    }
                ],
            }
        )
        assert "INCOMPLETE" in out and "floor" in out

    def test_an_unreadable_tree_says_a_lost_worker_would_not_appear(self) -> None:
        """This is the sentence that stops a model reading a short list as proof it
        created nothing."""
        out, _ = _st(
            {
                "ok": True,
                "caller": "chat-1",
                "tree": "unreadable",
                "sessions": [
                    {
                        "target": "chat-2",
                        "title": "t",
                        "status": "idle",
                        "running": False,
                        "queue_depth": 0,
                        "source": "live",
                    }
                ],
            }
        )
        assert "unreadable" in out
        assert "lost would not appear" in out

    def test_a_readable_tree_adds_no_caveat(self) -> None:
        out, _ = _st(
            {
                "ok": True,
                "caller": "chat-1",
                "tree": "readable",
                "sessions": [
                    {
                        "target": "chat-2",
                        "title": "t",
                        "status": "idle",
                        "running": False,
                        "queue_depth": 0,
                        "source": "live",
                    }
                ],
            }
        )
        assert "floor" not in out and "unreadable" not in out

    def test_an_empty_roster_under_a_bad_read_says_the_list_may_be_short(self) -> None:
        out, _ = _st({"ok": True, "caller": "chat-1", "tree": "unreadable", "sessions": []})
        assert "no sessions" in out
        assert "unreadable" in out

    def test_an_empty_roster_under_a_good_read_is_stated_plainly(self) -> None:
        out, _ = _st({"ok": True, "caller": "chat-1", "tree": "readable", "sessions": []})
        assert "no sessions" in out
        assert "unreadable" not in out and "incomplete" not in out

    def test_an_api_refusal_is_surfaced_as_an_error(self) -> None:
        out, _ = _st({"error": "session control is disabled in config"})
        assert out.startswith("Error:")


class TestBroadcastSchema:
    def test_mode_is_required(self) -> None:
        """No default either way: defaulting to the queue swallows a caller's
        request to interrupt, and defaulting to the steer interrupts sessions it
        only meant to leave a note for."""
        with pytest.raises(ValidationError):
            validate_tool_args({"message": "hi"}, SESSION_BROADCAST_SCHEMA)

    def test_an_unknown_mode_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            validate_tool_args({"message": "hi", "mode": "interrupt"}, SESSION_BROADCAST_SCHEMA)

    @pytest.mark.parametrize("mode", ["queue", "steer"])
    def test_both_modes_validate(self, mode: str) -> None:
        cleaned = validate_tool_args(
            {"message": "hi", "mode": mode, "all": True}, SESSION_BROADCAST_SCHEMA
        )
        assert cleaned["mode"] == mode

    def test_the_schema_advertises_all_as_an_optional_boolean(self) -> None:
        from kiro_crew.mcp_dashboard import _tool_definitions

        tool = next(t for t in _tool_definitions() if t["name"] == "session_broadcast")
        schema = tool["inputSchema"]
        assert schema["properties"]["all"]["type"] == "boolean"
        assert "all" not in schema["required"]
        assert "all: true" in tool["description"]

    def test_message_is_required(self) -> None:
        with pytest.raises(ValidationError):
            validate_tool_args({"mode": "queue", "all": True}, SESSION_BROADCAST_SCHEMA)

    def test_a_non_string_target_is_refused_at_the_schema(self) -> None:
        """The model supplies these arguments, so a number in the list is a real
        input shape."""
        with pytest.raises(ValidationError):
            validate_tool_args(
                {"message": "hi", "mode": "queue", "targets": ["chat-2", 7]},
                SESSION_BROADCAST_SCHEMA,
            )

    def test_an_oversized_target_list_is_refused_at_the_schema(self) -> None:
        """Refused with the field named, rather than after a round trip.

        Sized FROM the cap. A literal here is the second number the constant's own
        comment warns about: raise the cap and a hardcoded list falls under it, so
        the test keeps passing while exercising no refusal at all.
        """
        with pytest.raises(ValidationError):
            validate_tool_args(
                {
                    "message": "hi",
                    "mode": "queue",
                    "targets": [f"chat-{i}" for i in range(validation.MAX_BROADCAST_TARGETS + 1)],
                },
                SESSION_BROADCAST_SCHEMA,
            )

    def test_the_schema_and_backend_share_one_target_cap(self) -> None:
        targets = next(
            field for field in SESSION_BROADCAST_SCHEMA.fields if field.name == "targets"
        )
        assert hasattr(validation, "MAX_BROADCAST_TARGETS")
        assert targets.max_items == validation.MAX_BROADCAST_TARGETS
        assert sc.MAX_BROADCAST_TARGETS == validation.MAX_BROADCAST_TARGETS


def test_both_tools_are_advertised_and_identity_gated() -> None:
    """A tool in the dispatcher but absent from the advertised set is unreachable;
    one advertised but outside `SESSION_CONTROL_TOOLS` skips the caller-identity
    gate and would let a subagent drive its parent's sessions."""
    from kiro_crew.mcp_dashboard import SESSION_CONTROL_TOOLS, _tool_definitions

    advertised = {t["name"] for t in _tool_definitions()}
    for tool in ("session_broadcast", "session_status"):
        assert tool in advertised
        assert tool in SESSION_CONTROL_TOOLS


def test_the_schemas_are_registered_for_the_dashboard_server() -> None:
    """A tool absent from its server's registry has its args passed through RAW,
    which for `mode` means an unvalidated string reaching the delivery choice."""
    from kiro_crew.validation import MCP_DASHBOARD_SCHEMAS

    assert MCP_DASHBOARD_SCHEMAS["session_broadcast"] is SESSION_BROADCAST_SCHEMA
    assert "session_status" in MCP_DASHBOARD_SCHEMAS
