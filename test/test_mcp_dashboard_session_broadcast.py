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

from unittest.mock import patch

import pytest

from kiro_crew import mcp_dashboard as md
from kiro_crew import validation
from kiro_crew.dashboard import session_control as sc
from kiro_crew.mcp_dashboard import _call_tool_inner
from kiro_crew.validation import (
    SESSION_BROADCAST_SCHEMA,
    ValidationError,
    validate_tool_args,
)


@pytest.fixture(autouse=True)
def _caller():
    with patch(
        "kiro_crew.mcp_core._resolve_session_key_strict",
        return_value="dashboard:chat-1-100",
    ):
        yield


def _bc(args: dict, resp: dict):
    with patch("kiro_crew.mcp_dashboard._post", return_value=resp) as mock_post:
        out = _call_tool_inner("session_broadcast", args)
    return out, mock_post


def _st(resp: dict):
    with patch("kiro_crew.mcp_dashboard._get", return_value=resp) as mock_get:
        out = _call_tool_inner("session_status", {})
    return out, mock_get


def _row(target: str, **kw) -> dict:
    return {"target": target, "ok": True, "started": False, "steered": False, **kw}


class TestBroadcastForwarding:
    def test_the_mode_and_message_go_to_the_broadcast_route(self) -> None:
        _, mock_post = _bc(
            {"message": "rebase first", "mode": "queue"},
            {"ok": True, "mode": "queue", "requested": 1, "delivered": 1, "results": []},
        )
        path, body = mock_post.call_args.args
        assert path == "/api/session-control/broadcast"
        assert body == {"message": "rebase first", "mode": "queue"}

    def test_targets_are_forwarded_when_named(self) -> None:
        _, mock_post = _bc(
            {"message": "stop", "mode": "steer", "targets": ["chat-2", "chat-3"]},
            {"ok": True, "mode": "steer", "requested": 2, "delivered": 2, "results": []},
        )
        assert mock_post.call_args.args[1]["targets"] == ["chat-2", "chat-3"]

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
        _, mock_post = _bc(
            {"message": "stop", "mode": "steer"},
            {"ok": True, "mode": "steer", "requested": 0, "delivered": 0, "results": []},
        )
        timeout = mock_post.call_args.kwargs["timeout"]
        worst_case_delivery = md.MAX_BROADCAST_TARGETS * md.BROADCAST_TARGET_ALLOWANCE_SECS
        assert timeout == worst_case_delivery + md.BROADCAST_RESPONSE_MARGIN_SECS
        assert timeout > worst_case_delivery
        assert timeout > 30, "the default would expire mid-fleet"

    def test_an_omitted_targets_key_is_not_invented(self) -> None:
        """Omission selects the default audience without inventing a field."""
        _, mock_post = _bc(
            {"message": "rebase", "mode": "queue"},
            {"ok": True, "mode": "queue", "requested": 0, "delivered": 0, "results": []},
        )
        assert "targets" not in mock_post.call_args.args[1]

    def test_an_empty_target_list_is_forwarded_for_the_backend_to_refuse(self) -> None:
        """An empty list is a caller error the BACKEND owns, so it must travel.

        Dropping it here would silently widen a broadcast nobody asked to widen:
        the request would arrive indistinguishable from an omitted key and reach
        the whole default audience.
        """
        _, mock_post = _bc(
            {"message": "stop", "mode": "queue", "targets": []},
            {"ok": True, "mode": "queue", "requested": 0, "delivered": 0, "results": []},
        )
        body = mock_post.call_args.args[1]
        assert "targets" in body, "an empty list must not be dropped on the client"
        assert body["targets"] == []

    def test_an_explicit_null_targets_reaches_the_default_audience(self) -> None:
        """`targets: null` is the default audience, not a crash.

        The validator keeps the key for a non-required field passed as explicit
        JSON ``null`` and hands back the spec default (``None``), so a presence
        test sends ``None`` into ``list()`` and the model gets
        ``Error: 'NoneType' object is not iterable`` where the documented
        default-audience broadcast belongs.
        """
        out, mock_post = _bc(
            {"message": "rebase", "mode": "queue", "targets": None},
            {"ok": True, "mode": "queue", "requested": 0, "delivered": 0, "results": []},
        )
        assert "NoneType" not in out
        assert "targets" not in mock_post.call_args.args[1]

    def test_the_three_target_shapes_stay_distinguishable(self) -> None:
        """Omitted, empty and null are three outcomes, never two.

        Collapsing any pair breaks one of them: null must join omitted at the
        default audience, while the empty list must stay separable from both so
        the backend can refuse it.
        """
        resp = {
            "ok": True,
            "mode": "queue",
            "requested": 0,
            "delivered": 0,
            "results": [],
        }
        bodies = {}
        for label, args in (
            ("omitted", {"message": "go", "mode": "queue"}),
            ("empty", {"message": "go", "mode": "queue", "targets": []}),
            ("null", {"message": "go", "mode": "queue", "targets": None}),
        ):
            _, mock_post = _bc(args, resp)
            bodies[label] = mock_post.call_args.args[1]

        assert "targets" not in bodies["omitted"]
        assert "targets" not in bodies["null"]
        assert bodies["empty"]["targets"] == []
        assert bodies["null"] == bodies["omitted"], "null must behave like omitted"
        assert bodies["empty"] != bodies["omitted"], "an empty list is not an omission"


class TestBroadcastReports:
    def test_a_queue_broadcast_reports_each_target_and_the_tally(self) -> None:
        out, _ = _bc(
            {"message": "rebase", "mode": "queue"},
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
            {"message": "stop", "mode": "steer"},
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
            {"message": "stop", "mode": "steer"},
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
            {"message": "rebase", "mode": "queue"},
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
            {"message": "rebase", "mode": "queue"},
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
            {"message": "rebase", "mode": "queue"},
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
            {"message": "rebase", "mode": "queue"},
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
            {"message": "rebase", "mode": "queue"},
            {"error": "a broadcast reaches at most 32 sessions"},
        )
        assert out.startswith("Error:")
        assert "at most 32" in out


class TestStatusReports:
    def test_it_reads_the_status_route(self) -> None:
        _, mock_get = _st({"ok": True, "caller": "chat-1", "tree": "readable", "sessions": []})
        assert mock_get.call_args.args[0] == "/api/session-control/status"

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

    def test_a_gone_row_says_the_dashboard_no_longer_holds_it(self) -> None:
        """`gone` is the row a live-only list cannot produce, so the prose has to
        distinguish it — a model that reads it as merely idle will try to message a
        session that is not there."""
        out, _ = _st(
            {
                "ok": True,
                "caller": "chat-1",
                "tree": "readable",
                "sessions": [{"target": "chat-7", "status": "gone", "source": "crew_log"}],
            }
        )
        assert "chat-7" in out and "gone" in out
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
        cleaned = validate_tool_args({"message": "hi", "mode": mode}, SESSION_BROADCAST_SCHEMA)
        assert cleaned["mode"] == mode

    def test_message_is_required(self) -> None:
        with pytest.raises(ValidationError):
            validate_tool_args({"mode": "queue"}, SESSION_BROADCAST_SCHEMA)

    def test_a_non_string_target_is_refused_at_the_schema(self) -> None:
        """The model supplies these arguments, so a number in the list is a real
        input shape."""
        with pytest.raises(ValidationError):
            validate_tool_args(
                {"message": "hi", "mode": "queue", "targets": ["chat-2", 7]},
                SESSION_BROADCAST_SCHEMA,
            )

    def test_an_oversized_target_list_is_refused_at_the_schema(self) -> None:
        """Refused with the field named, rather than after a round trip."""
        with pytest.raises(ValidationError):
            validate_tool_args(
                {
                    "message": "hi",
                    "mode": "queue",
                    "targets": [f"chat-{i}" for i in range(33)],
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
