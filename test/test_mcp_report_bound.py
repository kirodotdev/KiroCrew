"""THE cut for a session's MCP report: what a bounded list keeps, what it counts.

Three things are held here: the one helper (``keep``), the one reader of a host
status snapshot, and the rule that the report cuts a list through that helper and
nothing else, over every list the report bounds.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kiro_crew.acp import mcp_report_bound, mcp_session_report
from kiro_crew.acp.mcp_report_bound import keep, read_status_snapshot
from kiro_crew.acp.mcp_session_report import BUCKET_CAP, STATUS_CAP, McpSessionReport
from kiro_crew.acp.mcp_start_wait import left_to_first_turn
from kiro_crew.acp.types import (
    EVENT_MCP_OAUTH_REQUEST,
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    METHOD_KAS_MCP_STATUS,
    JsonRpcMessage,
)
from kiro_crew.start_priority import StartPriority

_FIELDS = ("accessModeFilteredServers",)


# ── the helper ──


@pytest.mark.parametrize("count, kept, left_out", [(2, 2, 0), (3, 3, 0), (4, 3, 1), (9, 3, 6)])
def test_keep_answers_the_first_names_and_how_many_more_there_were(count, kept, left_out):
    names = [f"s{i}" for i in range(count)]

    assert keep(names, 3) == (tuple(names[:kept]), left_out)


def test_keep_counts_a_repeat_of_a_kept_name_once():
    assert keep(["a", "b", "a", "c", "b", "d", "e", "d"], 3) == (("a", "b", "c"), 3)


def test_keep_holds_nothing_past_its_bound():
    """It is handed a generator and asked for two: what it returns is all it kept."""
    handed = (f"s{i}" for i in range(10_000))

    kept, left_out = keep(handed, 2)

    assert (kept, left_out) == (("s0", "s1"), 9_998)


# ── the snapshot reader ──


def _read(params, cap: int = 3):
    return read_status_snapshot(
        params,
        cap=cap,
        dropped_fields=_FIELDS,
        clean_name=lambda text: text.strip()[:8],
        clean_error=lambda text: text[:5],
    )


@pytest.mark.parametrize("params", [None, [], {}, {"servers": None}, {"servers": "github"}])
def test_a_frame_with_no_server_list_is_not_a_snapshot(params):
    assert _read(params) is None


def test_each_named_entry_is_read_by_what_the_host_says_of_it():
    entries = [
        {"name": "up", "status": "connected"},
        {"name": "broken", "status": "failed", "errorMessage": "exit 1 after a long trace"},
        {"name": "locked", "status": "failed", "failedAuthorization": True},
        {"name": "off", "status": "disabled"},
        {"name": "late", "status": "connecting"},
        {"name": "odd", "status": ["connected"]},
        {"name": "silent"},
    ]

    snapshot = _read({"servers": entries}, cap=8)

    assert snapshot.servers == (
        ("up", "connected", False, ""),
        ("broken", "failed", False, "exit "),
        ("locked", "failed", True, ""),
        ("off", "disabled", False, ""),
        ("late", "", False, ""),
        ("odd", "", False, ""),
        ("silent", "", False, ""),
    )
    assert snapshot.disabled == {"off"}
    assert snapshot.pending == {"late", "odd", "silent"}
    assert (snapshot.dropped, snapshot.omitted) == ((), 0)


def test_only_bounded_fields_of_an_entry_leave_the_reader():
    """Not the entry itself: a host may put anything in one."""
    entry = {"name": "up", "status": "connected", "tools": ["t"] * 50, "_meta": {"k": "v" * 900}}

    (read,) = _read({"servers": [entry]}).servers

    assert read == ("up", "connected", False, "")


def test_an_entry_that_names_no_server_is_skipped_and_not_counted():
    entries = ["github", {"status": "connected"}, {"name": "  "}, {"name": None}, {"name": 7}]

    snapshot = _read({"servers": entries}, cap=1)

    assert [name for name, *_ in snapshot.servers] == ["7"]
    assert snapshot.omitted == 0


def test_a_name_is_the_cleaned_one():
    snapshot = _read(
        {"servers": [{"name": " a-long-server-name "}], "accessModeFilteredServers": [" banned "]}
    )

    assert [name for name, *_ in snapshot.servers] == ["a-long-s"]
    assert snapshot.dropped == ("banned",)


def test_server_entries_past_the_bound_are_counted_and_not_read():
    entries = [{"name": f"s{i}", "status": "connecting"} for i in range(5)]

    snapshot = _read({"servers": entries})

    assert [name for name, *_ in snapshot.servers] == ["s0", "s1", "s2"]
    assert snapshot.pending == {"s0", "s1", "s2"}
    assert snapshot.omitted == 2


def test_dropped_names_past_the_bound_are_counted_and_a_repeat_is_not():
    listed = [{"name": "a"}, "b", {"name": "a"}, "c", 7, {"name": None}, "d", "e"]

    snapshot = _read({"servers": [], "accessModeFilteredServers": listed})

    assert snapshot.dropped == ("a", "b", "c")
    assert snapshot.omitted == 2


def test_a_server_listed_more_than_once_is_read_by_its_last_entry():
    """Connected, connecting again, connected: the last one is what the host says."""
    entries = [
        {"name": "wanted", "status": "connected"},
        {"name": "wanted", "status": "connecting"},
        {"name": "wanted", "status": "connected"},
    ]

    snapshot = _read({"servers": entries})

    assert snapshot.servers == (("wanted", "connected", False, ""),)
    assert (snapshot.pending, snapshot.omitted) == (set(), 0)


def test_a_last_entry_past_the_bound_still_decides_a_server_that_was_read():
    """The bound is on how many servers are read, not on where an entry sits: a
    server read here is never left with a state the host replaced further on."""
    entries = [
        {"name": "wanted", "status": "connecting"},
        {"name": "a", "status": "connected"},
        {"name": "b", "status": "connected"},
        {"name": "c", "status": "connected"},
        {"name": "wanted", "status": "failed", "errorMessage": "exit 1"},
    ]

    snapshot = _read({"servers": entries})

    assert [name for name, *_ in snapshot.servers] == ["wanted", "a", "b"]
    assert snapshot.servers[0] == ("wanted", "failed", False, "exit ")
    assert (snapshot.pending, snapshot.omitted) == (set(), 1)


def test_both_lists_past_the_bound_are_counted_together():
    params = {
        "servers": [{"name": f"s{i}"} for i in range(4)],
        "accessModeFilteredServers": [f"d{i}" for i in range(6)],
    }

    assert _read(params).omitted == 1 + 3


# ── every list the report bounds: under, at and over its bound ──


def _names(count: int) -> list[str]:
    return [f"srv-{i:03d}" for i in range(count)]


def _snapshot(**params) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS, params={"sessionId": "s", "servers": [], **params}
    )


def _spec(names: list[str], *, muted: bool) -> dict:
    return {"mcpServers": {name: {"command": "x", "disabled": muted} for name in names}}


def _fill_wire(report: McpSessionReport, names: list[str]) -> None:
    report.begin_session([{"name": name} for name in names])


def _fill_wire_later(report: McpSessionReport, names: list[str]) -> None:
    report.include_configured(tuple(names))


def _fill_dropped(report: McpSessionReport, names: list[str]) -> None:
    report.fold_frame(_snapshot(accessModeFilteredServers=[{"name": n} for n in names]))


def _fill_listed(status: str):
    def fill(report: McpSessionReport, names: list[str]) -> None:
        report.fold_frame(_snapshot(servers=[{"name": n, "status": status} for n in names]))

    return fill


def _fill_bucket(kind: str):
    def fill(report: McpSessionReport, names: list[str]) -> None:
        for name in names:
            report.record_event(kind, name, "exit 1")

    return fill


#: Each list of the report that has a bound: how it is filled, how what it kept
#: is read, its bound, where its count of names left out is read, and whether a
#: name left out makes the report partial (a list of server STATES does; a list
#: of findings about servers has its own count and does not).
_LISTS = {
    "the wire roster": (_fill_wire, lambda r: r.configured, STATUS_CAP, None, True),
    "the wire roster, added to": (
        _fill_wire_later,
        lambda r: r.configured,
        STATUS_CAP,
        None,
        True,
    ),
    "declared servers": (
        lambda r, names: r.record_declared(_spec(names, muted=False)),
        lambda r: r.declared,
        STATUS_CAP,
        lambda r: r.declared_omitted,
        True,
    ),
    "switched-off servers": (
        lambda r, names: r.record_switched_off(_spec(names, muted=True)),
        lambda r: r.switched_off,
        STATUS_CAP,
        None,
        True,
    ),
    "registry-dropped servers": (
        _fill_dropped,
        lambda r: r.registry_dropped,
        STATUS_CAP,
        None,
        True,
    ),
    "servers the host lists as starting": (
        _fill_listed("connecting"),
        lambda r: r._host_pending,
        STATUS_CAP,
        None,
        True,
    ),
    "servers the host lists as disabled": (
        _fill_listed("disabled"),
        lambda r: r._host_disabled,
        STATUS_CAP,
        None,
        True,
    ),
    "connected servers": (
        _fill_bucket(EVENT_MCP_SERVER_INITIALIZED),
        lambda r: r.payload()["ready"],
        STATUS_CAP,
        None,
        True,
    ),
    "failed servers": (
        _fill_bucket(EVENT_MCP_SERVER_INIT_FAILURE),
        lambda r: r.payload()["failed"],
        STATUS_CAP,
        None,
        True,
    ),
    "servers waiting for a sign-in": (
        _fill_bucket(EVENT_MCP_OAUTH_REQUEST),
        lambda r: r.payload()["awaiting_auth"],
        STATUS_CAP,
        None,
        True,
    ),
    "unresolved refs": (
        lambda r, names: r.record_unresolved_refs(names),
        lambda r: r.unresolved_refs,
        BUCKET_CAP,
        None,
        True,
    ),
    "servers that gave no tools": (
        lambda r, names: r.record_no_tools(names),
        lambda r: r.no_tools,
        BUCKET_CAP,
        lambda r: r.no_tools_omitted,
        False,
    ),
    "header secret refs": (
        lambda r, names: r.record_header_secret_refs(names),
        lambda r: r.header_secret_refs,
        BUCKET_CAP,
        lambda r: r.header_secret_refs_omitted,
        False,
    ),
    "env placeholder refs": (
        lambda r, names: r.record_env_placeholder_refs(names),
        lambda r: r.env_placeholder_refs,
        BUCKET_CAP,
        lambda r: r.env_placeholder_refs_omitted,
        False,
    ),
}


@pytest.mark.parametrize("over", [-1, 0, 1, 7], ids=["under", "at", "one-over", "seven-over"])
@pytest.mark.parametrize("which", sorted(_LISTS))
def test_every_bounded_list_keeps_up_to_its_bound_and_counts_the_rest(which, over):
    fill, kept, cap, own_count, makes_partial = _LISTS[which]
    report = McpSessionReport()
    report.begin_session([])
    left_out = max(0, over)

    fill(report, _names(cap + over))

    assert len(kept(report)) == cap + min(0, over)
    if own_count is not None:
        assert own_count(report) == left_out
    assert report.left_out == (left_out if makes_partial else 0)


@pytest.mark.parametrize("over", [-1, 0, 1, 7], ids=["under", "at", "one-over", "seven-over"])
def test_the_servers_a_start_leaves_to_its_turn_are_bounded_the_same_way(over):
    names = _names(STATUS_CAP + over)
    params = {
        "_meta": {"kiro": {"customAgents": [{"id": "a", "mcpServers": dict.fromkeys(names)}]}}
    }

    left, more = left_to_first_turn(params, "a", (), StartPriority.FOREGROUND)

    assert (len(left), more) == (STATUS_CAP + min(0, over), max(0, over))


def test_a_report_stays_partial_after_a_list_fits_again():
    """A later value that fits does not bring back the report that was left out."""
    report = McpSessionReport()
    report.begin_session([])
    _fill_dropped(report, _names(STATUS_CAP + 5))
    assert report.left_out == 5

    _fill_dropped(report, _names(3))

    assert (len(report.registry_dropped), report.left_out) == (3, 5)


def test_a_snapshot_that_fits_does_not_make_a_left_out_server_read_as_starting():
    """256 servers, then the one the session expects, connected and past the bound.
    The next snapshot is empty: that server has no report, and is still not
    called starting, because its report was left out and never came back."""
    report = McpSessionReport()
    report.begin_session([])
    report.record_declared(_spec(["wanted"], muted=False))
    listed = [{"name": name, "status": "connected"} for name in (*_names(STATUS_CAP), "wanted")]
    report.fold_frame(_snapshot(servers=listed))
    assert (report.left_out, report.not_ready()) == (1, {})

    report.fold_frame(_snapshot())

    assert (report.left_out, report.not_ready()) == (1, {})


def test_a_roster_that_was_cut_stays_cut_when_a_name_is_added_to_it():
    """Adding a name it already holds must not take the count of the ones it left out."""
    report = McpSessionReport()
    _fill_wire(report, _names(STATUS_CAP + 1))
    assert report.left_out == 1

    report.include_configured(("srv-000",))
    assert (len(report.configured), report.left_out) == (STATUS_CAP, 1)

    report.include_configured(("one-more",))
    assert (len(report.configured), report.left_out) == (STATUS_CAP, 2)


def test_a_server_the_host_lists_twice_reads_as_its_last_entry():
    report = McpSessionReport()
    report.begin_session([])
    states = ["connected", "connecting", "connected"]

    report.fold_frame(_snapshot(servers=[{"name": "wanted", "status": s} for s in states]))

    assert report.payload()["ready"] == ["wanted"]
    assert (report.not_ready(), report.left_out) == ({}, 0)


def test_a_server_whose_last_entry_comes_after_the_bound_is_not_left_starting():
    """Starting, then more servers than the report reads, then connected."""
    report = McpSessionReport()
    report.begin_session([])
    listed = [{"name": "wanted", "status": "connecting"}]
    listed += [{"name": name, "status": "connected"} for name in _names(STATUS_CAP)]
    listed += [{"name": "wanted", "status": "connected"}]

    report.fold_frame(_snapshot(servers=listed))

    assert report.left_out == 1
    assert "wanted" in report.payload()["ready"]
    assert report.not_ready() == {}


def test_a_new_session_attempt_starts_whole():
    report = McpSessionReport()
    report.begin_session([])
    report.record_declared(_spec(_names(STATUS_CAP + 2), muted=False))
    _fill_bucket(EVENT_MCP_SERVER_INITIALIZED)(report, _names(STATUS_CAP + 1))
    assert report.left_out == 3

    report.begin_session([])

    assert report.left_out == 0


# ── the rule itself ──


#: Names a counted or early-ending loop is written with.
_COUNTED_LOOPS = ("islice", "enumerate", "range", "zip", "next")


def _list_cuts(
    source: str, *, string_cuts: tuple[str, ...] = (), bound_guards: tuple[str, ...] = ()
) -> list[str]:
    """Every place in *source* that can cut a list at a bound without the helper.

    Read off the syntax tree, as ``function: what``: a slice with a start or an
    end; a comparison of a length, or with a name that holds a bound (any name
    with ``cap`` in it); a loop that can stop early or is counted (``break``,
    ``while``, ``enumerate``, ``range``, ``zip``, ``next``); and ``islice``.
    Those are the constructions a cut is written with, whatever its bound is
    called. *string_cuts* names the functions whose slice bounds ONE string;
    *bound_guards* the functions that are a bound's own guard.
    """
    found: list[str] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else scope
            )
            names = {n.id.lower() for n in ast.walk(child) if isinstance(n, ast.Name)}
            if isinstance(child, ast.Slice) and (
                child.lower is not None or child.upper is not None
            ):
                if scope not in string_cuts:
                    found.append(f"{scope}: slice")
            elif isinstance(child, ast.Compare) and (
                "len" in names or any("cap" in n for n in names)
            ):
                if scope not in bound_guards:
                    found.append(f"{scope}: comparison with a bound")
            elif isinstance(child, (ast.Break, ast.While)):
                found.append(f"{scope}: a loop that stops early")
            elif isinstance(child, ast.Name) and child.id in _COUNTED_LOOPS:
                found.append(f"{scope}: {child.id}")
            elif isinstance(child, ast.Attribute) and child.attr == "islice":
                found.append(f"{scope}: islice")
            visit(child, inner)

    visit(ast.parse(source), "<module>")
    return found


def _source(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


def test_the_report_cuts_a_list_through_the_one_helper_and_nothing_else():
    """No slice, no comparison with a bound and no ``islice`` outside three places.

    The two slices are a STRING's: ``sanitize_sink_text`` bounds one name or one
    error, and ``servers_exposing_no_tools`` strips a prefix. The comparisons
    are the full-bucket guard of ``_record``, and each of its two branches
    counts the report it keeps out. Every list is cut by
    ``mcp_report_bound.keep``.
    """
    source = _source(mcp_session_report)
    strings = ("sanitize_sink_text", "servers_exposing_no_tools")

    assert _list_cuts(source, string_cuts=strings, bound_guards=("_record",)) == []
    assert _list_cuts(source, bound_guards=("_record",)) == [f"{name}: slice" for name in strings]
    assert _list_cuts(source, string_cuts=strings) == ["_record: comparison with a bound"] * 2
    assert source.count("self._refuse_report(") == 2


def test_the_helpers_module_bounds_a_list_in_the_helper_alone():
    source = _source(mcp_report_bound)

    assert _list_cuts(source, bound_guards=("keep",)) == []
    assert _list_cuts(source) == ["keep: comparison with a bound"]


@pytest.mark.parametrize(
    "source",
    [
        "kept = names[:STATUS_CAP]",
        "kept = names[1:cap]",
        "del names[_BUCKET_CAP:]",
        "kept = names[-3:]",
        "if len(out) == STATUS_CAP:\n    pass",
        "while len(out) < _BUCKET_CAP:\n    out.append(1)",
        "kept = [n for i, n in enumerate(names) if i < cap]",
        "kept = [n for i, n in enumerate(names) if i < limit]",
        "kept = [n for _, n in zip(range(3), names)]",
        "kept = list(islice(names, 3))",
        "kept = list(itertools.islice(names, 3))",
        # A second capped helper whose bound is not called a cap.
        "def cut(items, limit):\n    out = []\n    for item in items:\n"
        "        if len(out) == limit:\n            break\n        out.append(item)\n    return out",
        "def cut(items, limit):\n    out = []\n    for item in items:\n"
        "        if len(out) < limit:\n            out.append(item)\n    return out",
        "def cut(items, limit):\n    out, n = [], 0\n    while n != limit:\n"
        "        out.append(items[n])\n        n += 1\n    return out",
    ],
)
def test_the_guard_sees_each_way_to_cut_a_list(source):
    assert _list_cuts(source)


@pytest.mark.parametrize(
    "source",
    ["bucket[:] = [n for n in bucket if n not in stale]", "kept, more = keep(names, STATUS_CAP)"],
)
def test_the_guard_passes_what_cuts_nothing_itself(source):
    assert _list_cuts(source) == []
