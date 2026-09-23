"""The ``kirocrew-crew-log`` MCP server: its tool set, its caps, and its refusals.

Four things are pinned here that no other suite can see:

* **The tool set is exactly three READS.** The server exists because the crew log
  is fenced from the agent for integrity and a read-only door costs that nothing.
  A write tool would cost it everything, so the names are ratcheted: adding one
  fails this file rather than shipping.
* **Every cap is the server's own promise**, not the endpoint's. A page that does
  not fit is cut at a ROW boundary with ``next_from`` rewritten, because half a
  JSON row is not a shorter answer.
* **Every failure is a typed code**, never a traceback and never prose alone. The
  agent branches on the code, and the endpoint's own code is carried through
  rather than re-derived here.
* **``unit`` has three forms and the rule separating them is stated**, so one
  argument cannot silently mean two different units.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew import mcp_crew_log as server

UNIT = "s-abc123"
CALLER = "dashboard:chat-owner"


@pytest.fixture(autouse=True)
def _identified():
    """Every test calls as a session the gateway can name, unless it says otherwise.

    The server refuses outright without a strict identity, so a test that did not
    establish one would be testing the refusal and nothing else.
    """
    with patch.object(server, "require_strict_session_key", return_value=(CALLER, "")):
        yield


def _call(name: str, **args: Any) -> dict[str, Any]:
    """One tool call, validated as the stdio loop validates it, parsed as JSON."""
    return json.loads(server._call_tool(name, args))


def _page(entries: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    payload = {
        "session_id": UNIT,
        "exists": True,
        "from": 1,
        "to": 100,
        "last_seq": entries[-1]["seq"] if entries else 0,
        "entries": entries,
        "next_from": None,
        "refs_unresolved": 0,
    }
    payload.update(over)
    return payload


def _entry(seq: int, etype: str = "turn/started", data: Any = None, **over: Any) -> dict[str, Any]:
    row = {
        "seq": seq,
        "time": 1_700_000_000_000 + seq,
        "type": etype,
        "src": "gateway",
        "data": {"turn": seq} if data is None else data,
    }
    row.update(over)
    return row


class TestTheToolSetIsRatcheted:
    """The whole point of the server: three reads, and no way to add a write."""

    def test_exactly_three_tools_by_name(self) -> None:
        assert [t["name"] for t in server._list_tools()] == [
            "crew_log_list",
            "crew_log_read",
            "crew_log_projection",
        ]

    def test_the_declared_set_matches_what_is_advertised(self) -> None:
        assert server.TOOLS == tuple(t["name"] for t in server._list_tools())

    def test_no_tool_name_reads_as_a_write(self) -> None:
        """A name-level ratchet, so a write cannot arrive under a read's clothing."""
        forbidden = ("write", "append", "delete", "remove", "prune", "repair", "set", "update")
        for name in server.TOOLS:
            assert not any(verb in name for verb in forbidden), name

    def test_every_tool_has_a_schema_so_args_are_never_passed_through_raw(self) -> None:
        from kiro_crew.validation import MCP_CREW_LOG_SCHEMAS

        assert set(MCP_CREW_LOG_SCHEMAS) == set(server.TOOLS)

    def test_the_projection_names_match_the_folds_the_gateway_serves(self) -> None:
        """The description lists folds without importing storage; drift would lie."""
        from kiro_crew.crew_log.projection import PROJECTION_NAMES

        assert server.PROJECTION_NAMES == PROJECTION_NAMES

    def test_the_caller_name_matches_the_one_the_endpoint_recognizes(self) -> None:
        from kiro_crew.dashboard.handlers.crew_log import CREW_LOG_MCP_CALLER
        from kiro_crew.dashboard.token_auth import KNOWN_INTERNAL_CALLERS

        assert server.SERVER_NAME == CREW_LOG_MCP_CALLER
        assert server.SERVER_NAME in KNOWN_INTERNAL_CALLERS

    def test_tools_are_advertised_while_the_flag_is_off(self, monkeypatch) -> None:
        """An agent must learn the flag state from a refusal, not a missing tool."""
        monkeypatch.delenv("KIROCREW_CREW_LOG", raising=False)
        assert len(server._list_tools()) == 3


class TestUnitResolution:
    def test_a_raw_unit_id_is_passed_through_untouched(self) -> None:
        with patch.object(server, "_get", return_value=_page([_entry(1)])) as got:
            _call("crew_log_read", unit=UNIT)
        assert f"/units/{UNIT}/page" in got.call_args[0][0]

    def test_a_slot_key_is_resolved_through_the_gateway(self) -> None:
        seen: list[str] = []

        def fake_get(path: str, *a: Any, **k: Any) -> dict[str, Any]:
            seen.append(path)
            if "/resolve" in path:
                return {"key": "chat-1533-1789617503", "unit": UNIT}
            return _page([_entry(1)])

        with patch.object(server, "_get", side_effect=fake_get):
            _call("crew_log_read", unit="chat-1533-1789617503")
        assert "/api/crew-log/resolve?key=chat-1533-1789617503" in seen[0]
        assert f"/units/{UNIT}/page" in seen[1]

    def test_a_namespaced_key_is_resolved_too(self) -> None:
        with patch.object(server, "_get", return_value={"unit": UNIT}) as got:
            unit, err = server._resolve_unit("dashboard:chat-1533-1789617503", CALLER)
        assert (unit, err) == (UNIT, "")
        assert "/resolve" in got.call_args[0][0]

    def test_self_resolves_through_the_strict_gate_and_sends_that_key(self) -> None:
        """``self`` is the caller's own key, resolved once and sent as itself."""
        with patch.object(server, "_get", return_value={"unit": UNIT}) as got:
            server._call_tool("crew_log_projection", {"unit": "self", "name": "status"})
        assert f"key={CALLER.replace(':', '%3A')}" in got.call_args_list[0][0][0]
        assert got.call_args_list[0].kwargs["session_key"] == CALLER

    def test_an_unresolvable_key_keeps_the_endpoint_code(self) -> None:
        refusal = {"error": "no live ACP session", "code": "unresolvable_key"}
        with patch.object(server, "_get", return_value=refusal):
            body = _call("crew_log_read", unit="chat-1-2")
        assert body["error"]["code"] == "unresolvable_key"

    def test_an_empty_unit_is_refused_before_any_request(self) -> None:
        with patch.object(server, "_get") as got:
            unit, err = server._resolve_unit("   ", CALLER)
        assert unit == ""
        assert json.loads(err)["error"]["code"] == "unknown_unit"
        got.assert_not_called()


class TestTypedErrors:
    @pytest.mark.parametrize(
        "code",
        ["crew_log_disabled", "unknown_unit", "unknown_projection", "bad_range", "forbidden"],
    )
    def test_the_endpoints_code_is_carried_through(self, code: str) -> None:
        with patch.object(server, "_get", return_value={"error": "refused", "code": code}):
            body = _call("crew_log_read", unit=UNIT)
        assert body["error"]["code"] == code

    def test_a_transport_failure_reads_as_unavailable(self) -> None:
        """A bare error with no code never reached a handler; nothing answered."""
        with patch.object(server, "_get", return_value={"error": "<urlopen error refused>"}):
            body = _call("crew_log_list")
        assert body["error"]["code"] == "unavailable"

    def test_a_refusal_is_never_a_traceback(self) -> None:
        with patch.object(server, "_get", return_value={"error": "boom", "code": "forbidden"}):
            rendered = server._call_tool("crew_log_projection", {"unit": UNIT, "name": "status"})
        assert "Traceback" not in rendered
        assert json.loads(rendered)["error"]["code"] == "forbidden"

    def test_an_unknown_projection_name_is_refused_by_the_schema(self) -> None:
        with patch.object(server, "_get") as got:
            rendered = server._call_tool("crew_log_projection", {"unit": UNIT, "name": "nope"})
        assert "must be one of" in rendered and "name" in rendered
        got.assert_not_called()


class TestCaps:
    def test_the_limit_is_clamped_to_the_documented_maximum(self) -> None:
        """MUTATION GUARD: raise MAX_READ_LIMIT and this goes red.

        The cap is the tool's own promise. A caller asking for more must get the
        cap and page, not a read bounded only by how big the file happens to be.
        """
        assert server.MAX_READ_LIMIT == 200
        from kiro_crew.validation import MCP_CREW_LOG_SCHEMAS

        spec = {f.name: f for f in MCP_CREW_LOG_SCHEMAS["crew_log_read"].fields}
        assert spec["limit"].max_val == 200
        with patch.object(server, "_get", return_value=_page([_entry(1)])) as got:
            body = _call("crew_log_read", unit=UNIT, from_seq=1, limit=200)
        assert "to=200" in got.call_args[0][0]
        assert body["from_seq"] == 1

    def test_a_limit_over_the_cap_is_refused_by_the_schema(self) -> None:
        with patch.object(server, "_get") as got:
            rendered = server._call_tool("crew_log_read", {"unit": UNIT, "limit": 201})
        assert "limit" in rendered
        got.assert_not_called()

    def test_a_row_is_trimmed_and_says_it_was(self) -> None:
        big = {"text": "x" * (server.MAX_DATA_CHARS * 3)}
        with patch.object(server, "_get", return_value=_page([_entry(1, data=big)])):
            body = _call("crew_log_read", unit=UNIT)
        trimmed = body["entries"][0]["data"]["_trimmed"]
        assert trimmed.endswith("chars)")
        assert "…" in trimmed

    def test_full_returns_one_row_untrimmed(self) -> None:
        big = {"text": "y" * (server.MAX_DATA_CHARS * 3)}
        with patch.object(server, "_get", return_value=_page([_entry(7, data=big)])):
            body = _call("crew_log_read", unit=UNIT, from_seq=7, limit=1, full=True)
        assert body["entries"][0]["data"] == big

    def test_an_oversized_page_is_cut_at_a_row_and_next_from_moves(self) -> None:
        """The cap is on BYTES, and the cut has to leave parseable JSON behind."""
        rows = [_entry(seq, data={"text": "z" * 2000}) for seq in range(1, 101)]
        with patch.object(server, "_get", return_value=_page(rows, last_seq=100)):
            rendered = server._call_tool("crew_log_read", {"unit": UNIT, "full": True})
        assert len(rendered.encode("utf-8")) <= server.MAX_READ_BYTES
        body = json.loads(rendered)  # cut at a row boundary, so it still parses
        assert body["truncated_to_fit"] is True
        assert body["next_from"] == body["entries"][-1]["seq"] + 1
        assert len(body["entries"]) < len(rows)

    def test_one_full_row_over_the_budget_is_still_within_the_cap(self) -> None:
        """MUTATION GUARD: the cap is ABSOLUTE, `full=True` included.

        A crew log carries message bodies, so one row can exceed the whole budget
        on its own -- which would make the shape a caller reaches for to see a row
        whole the one shape that ignores the cap.
        """
        huge = {"text": "q" * (server.MAX_READ_BYTES * 2)}
        with patch.object(server, "_get", return_value=_page([_entry(5, data=huge)])):
            rendered = server._call_tool(
                "crew_log_read", {"unit": UNIT, "from_seq": 5, "limit": 1, "full": True}
            )
        assert len(rendered.encode("utf-8")) <= server.MAX_READ_BYTES
        body = json.loads(rendered)
        assert body["truncated_to_fit"] is True
        assert body["returned"] == 1
        assert "chars)" in body["entries"][0]["data"]["_trimmed"]

    def test_a_cut_page_reports_the_rows_it_actually_carries(self) -> None:
        """MUTATION GUARD: a body claiming 100 rows beside 40 of them is worse than
        a short page -- the count is what a caller checks against its request."""
        rows = [_entry(seq, data={"text": "w" * 2000}) for seq in range(1, 101)]
        with patch.object(server, "_get", return_value=_page(rows, last_seq=100)):
            body = _call("crew_log_read", unit=UNIT, full=True)
        assert body["truncated_to_fit"] is True
        assert body["returned"] == len(body["entries"])
        assert body["returned"] < len(rows)

    def test_a_page_that_fits_is_not_marked_truncated(self) -> None:
        with patch.object(server, "_get", return_value=_page([_entry(1), _entry(2)])):
            body = _call("crew_log_read", unit=UNIT)
        assert "truncated_to_fit" not in body
        assert body["returned"] == 2


class TestEveryRequestCarriesTheStrictlyResolvedKey:
    """The endpoint scopes each read on the key it is handed, so one call must not
    be able to present two, and none of them may come from the lenient walk.

    The lenient resolver walks ``/proc`` ancestors. A subagent lives under its
    spawner's process tree and the spawner is commonly the owner's own dashboard
    tab, so a leniently-resolved key would hand the endpoint an OWNER identity for
    a subagent's call and the wider-read rule would admit it.
    """

    @pytest.mark.parametrize(
        "tool,args",
        [
            ("crew_log_list", {}),
            ("crew_log_read", {"unit": UNIT}),
            ("crew_log_projection", {"unit": UNIT, "name": "status"}),
        ],
        ids=["list", "read", "projection"],
    )
    def test_the_key_is_sent_explicitly_on_every_leg(self, tool, args) -> None:
        with patch.object(server, "_get", return_value=_page([_entry(1)])) as got:
            server._call_tool(tool, dict(args))
        assert got.call_args_list
        for call in got.call_args_list:
            assert call.kwargs.get("session_key") == CALLER, call

    def test_resolving_another_session_key_still_sends_the_callers_own(self) -> None:
        """Naming somebody else's key must not change WHOSE authority the read has."""

        def fake_get(path: str, *a: Any, **k: Any) -> dict[str, Any]:
            return {"unit": UNIT} if "/resolve" in path else _page([_entry(1)])

        with patch.object(server, "_get", side_effect=fake_get) as got:
            _call("crew_log_read", unit="chat-9-1789617503")
        assert [c.kwargs.get("session_key") for c in got.call_args_list] == [CALLER, CALLER]

    @pytest.mark.parametrize(
        "tool,args",
        [
            ("crew_log_list", {}),
            ("crew_log_read", {"unit": UNIT}),
            ("crew_log_projection", {"unit": UNIT, "name": "status"}),
        ],
        ids=["list", "read", "projection"],
    )
    def test_an_unidentifiable_caller_reads_nothing_at_all(self, tool, args) -> None:
        """MUTATION GUARD: drop the gate and this goes red.

        Fails closed, including for the caller's own unit -- "its own unit" is
        derived from the same key, so an unnamed caller has no own unit either.
        """
        with patch.object(
            server, "require_strict_session_key", return_value=("", "Error: no identity")
        ):
            with patch.object(server, "_get") as got:
                body = json.loads(server._call_tool(tool, dict(args)))
        assert body["error"]["code"] == "forbidden"
        got.assert_not_called()


class TestRowShaping:
    def test_rows_carry_seq_ts_type_and_data(self) -> None:
        with patch.object(server, "_get", return_value=_page([_entry(4, "turn/completed")])):
            body = _call("crew_log_read", unit=UNIT)
        row = body["entries"][0]
        assert row["seq"] == 4
        assert row["type"] == "turn/completed"
        assert row["ts"] == 1_700_000_000_004
        assert row["data"] == {"turn": 4}

    def test_a_resolved_citation_is_reported_as_its_verdict(self) -> None:
        entry = _entry(2, ref_resolution={"status": "ok", "entries": 3})
        with patch.object(server, "_get", return_value=_page([entry])):
            body = _call("crew_log_read", unit=UNIT)
        assert body["entries"][0]["ref"] == {"status": "ok", "entries": 3}

    def test_an_unresolved_citation_is_reported_rather_than_dropped(self) -> None:
        entry = _entry(2, ref={"unit": "s-other", "id": "x", "from_seq": 1, "to_seq": 2})
        with patch.object(server, "_get", return_value=_page([entry], refs_unresolved=1)):
            body = _call("crew_log_read", unit=UNIT)
        assert body["entries"][0]["ref"]["status"] == "unresolved"
        assert body["refs_unresolved"] == 1

    def test_types_filters_and_the_filter_is_named_in_the_answer(self) -> None:
        rows = [_entry(1, "turn/started"), _entry(2, "tool/called"), _entry(3, "turn/completed")]
        with patch.object(server, "_get", return_value=_page(rows)):
            body = _call("crew_log_read", unit=UNIT, types=["tool/called"])
        assert [r["type"] for r in body["entries"]] == ["tool/called"]
        assert body["filtered_by_types"] == ["tool/called"]

    def test_since_ts_drops_older_rows(self) -> None:
        rows = [_entry(1), _entry(2), _entry(3)]
        with patch.object(server, "_get", return_value=_page(rows)):
            body = _call("crew_log_read", unit=UNIT, since_ts=1_700_000_000_002)
        assert [r["seq"] for r in body["entries"]] == [2, 3]


class TestListing:
    def test_the_query_carries_every_filter(self) -> None:
        with patch.object(server, "_get", return_value={"units": [], "scanned": 0}) as got:
            _call(
                "crew_log_list",
                slot_contains="chat-15",
                active_within_secs=3600,
                with_type_counts=True,
                limit=10,
            )
        path = got.call_args[0][0]
        assert "slot_contains=chat-15" in path
        assert "active_within_secs=3600" in path
        assert "with_type_counts=1" in path
        assert "limit=10" in path

    def test_the_tool_takes_no_kind_at_all(self) -> None:
        """One kind of unit is written, so the tool offers no way to name another.

        The schema refuses it as an unknown field, which is a clearer answer than
        an enum with a single legal value: the agent learns the argument does not
        exist rather than that it exists and is pointless.
        """
        with patch.object(server, "_get") as got:
            rendered = server._call_tool("crew_log_list", {"kind": "crew"})
        assert "kind" in rendered
        got.assert_not_called()

    def test_the_tool_description_offers_no_kind(self) -> None:
        listed = next(t for t in server._list_tools() if t["name"] == "crew_log_list")
        assert "kind" not in listed["inputSchema"]["properties"]
        assert "kind" not in listed["description"]


class TestManagedRegistration:
    """A managed server is named in several registries; a half-registered one breaks."""

    def test_named_in_every_managed_registry(self) -> None:
        from kiro_crew import agent, mcp_cleanup, mcp_discovery, onboarding_import

        name = server.SERVER_NAME
        assert name in agent._MANAGED_MCP_SERVERS
        assert name in mcp_cleanup.KIROCREW_BIN_MCP_SERVERS
        assert mcp_discovery._MANAGED_SERVER_SUBCOMMANDS.get(name) == "mcp-crew-log"
        assert name in mcp_discovery._MANAGED_SERVER_NAMES
        assert mcp_discovery._MANAGED_SERVER_TOOL_MODULES.get(name) == "kiro_crew.mcp_crew_log"
        assert name in onboarding_import._managed_mcp_names()

    def test_it_is_an_assignable_set_and_carries_no_auto_approve(self) -> None:
        from kiro_crew import agent, mcp_cleanup

        spec = agent._MANAGED_MCP_SERVERS[server.SERVER_NAME]
        assert spec.get("opt_in") is True
        assert "autoApprove" not in spec
        assert server.SERVER_NAME in mcp_cleanup.OPT_IN_BIN_MCP_SERVERS

    def test_it_is_registered_as_a_reflexive_tool_module(self) -> None:
        """It calls the strict gate for ``self``; the registry is how that is policed."""
        from kiro_crew.mcp_core import REFLEXIVE_TOOL_MODULES

        assert "mcp_crew_log.py" in REFLEXIVE_TOOL_MODULES

    def test_it_advertises_caller_identity(self) -> None:
        """Without the advertisement gatewayd injects no caller block on a pooled
        backend, and every scoped read fails closed."""
        from kiro_crew import mcp_discovery

        assert server.ADVERTISE_CALLER_IDENTITY is True
        assert server.SERVER_NAME in mcp_discovery._MANAGED_SERVERS_CALLER_AWARE
        assert not mcp_discovery.managed_server_is_session_bound(server.SERVER_NAME)
