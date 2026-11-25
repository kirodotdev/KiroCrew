"""``scripts/capture_tools_allowlist.py`` -- its parsing and verdict, with no live run.

The script's reason to exist is a live KAS session, which this suite has none of. What
it can hold is everything a verdict is computed from: which snapshot counts as the
activated agent's, how an offered tag is matched to the projected set, which calls are
the engine's own, and what a reduced capture may carry. The committed captures under
``test/fixtures/acp_frames/kas/tools-allowlist-*-live.jsonl`` are replayed through the
same functions, so a verdict in the runbook's table is one this code still reaches.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "capture_tools_allowlist.py"
FIXTURES = sorted(
    (REPO_ROOT / "test" / "fixtures" / "acp_frames" / "kas").glob("tools-allowlist-*-live.jsonl")
)
GUIDE = REPO_ROOT / "docs" / "guides" / "tools-allowlist-capture.md"


def _load():
    spec = importlib.util.spec_from_file_location("capture_tools_allowlist", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: ``@dataclass`` resolves annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cap():
    return _load()


def _read(path: pathlib.Path) -> tuple[dict, list[dict]]:
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return lines[0]["_meta"], lines[1:]


def _tags(*tags: tuple[str, str]) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "_kiro/tools/didChange",
        "params": {"sessionId": "s", "tags": [{"source": s, "tag": t} for s, t in tags]},
    }


def _status(*servers: tuple[str, str, list[str]]) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "_kiro/mcp/status",
        "params": {
            "sessionId": "s",
            "servers": [
                {"name": n, "status": st, "tools": [{"name": t} for t in tools]}
                for n, st, tools in servers
            ],
        },
    }


NEW_ANSWER = {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s"}}
MODE_ANSWER = {"jsonrpc": "2.0", "id": 3, "result": {}}


# ── the committed captures ──────────────────────────────────────────────────────


def test_there_are_committed_captures():
    assert FIXTURES, "no tools-allowlist capture under test/fixtures/acp_frames/kas/"


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_each_capture_replays_to_its_recorded_verdict(cap, path):
    meta, frames = _read(path)
    offered = cap.offered_after_activation(frames)
    verdict = cap.judge(
        meta["projected"], meta["sent"], offered, cap.tool_calls(frames), meta["sent"]["mcpServers"]
    )
    assert verdict.verdict == meta["verdict"], verdict.reason


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_each_capture_carries_the_bundle_provenance(cap, path):
    meta, frames = _read(path)
    assert meta["backend"] == "kas"
    assert meta["recorded"] == "live"
    assert meta["route"] == "kas-bundle"
    assert meta["variant"] in {v.name for v in cap.VARIANTS}
    from kiro_crew.mcp_hot_reload import parse_kiro_cli_version

    assert parse_kiro_cli_version(meta["agent_version"]) is not None
    # Every capture saw an activated snapshot: an UNPROVEN one is not committed.
    assert cap.offered_after_activation(frames).tags_seen


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_each_capture_is_free_of_host_markers(cap, path):
    assert cap.host_markers(path.read_text(encoding="utf-8")) == []


def test_the_runbook_table_names_every_committed_capture():
    text = GUIDE.read_text(encoding="utf-8")
    for path in FIXTURES:
        meta, _ = _read(path)
        assert f"`{path.name}`" in text, path.name
        assert meta["verdict"] in text


def test_the_variants_cover_the_rfc_list(cap):
    names = {v.name for v in cap.VARIANTS}
    for wanted in (
        "default",
        "mcp-json-files",
        "include-mcp-json-absent",
        "include-mcp-json-true",
        "wildcard",
        "powers",
        "subagent",
        "tool-search",
        "kas-subagents-run",
    ):
        assert wanted in names


# ── which snapshot counts ───────────────────────────────────────────────────────


def test_the_default_modes_snapshot_before_session_new_is_ignored(cap):
    frames = [
        _tags(("builtin", "read"), ("builtin", "shell")),
        NEW_ANSWER,
        _tags(("mcp", "@probe/probe_note")),
        MODE_ANSWER,
    ]
    offered = cap.offered_after_activation(frames)
    assert offered.builtin_tags == []
    assert offered.mcp_tags == ["@probe/probe_note"]


def test_the_last_snapshot_of_each_kind_wins(cap):
    frames = [
        NEW_ANSWER,
        _status(("probe", "connecting", [])),
        _tags(),
        _status(("probe", "connected", ["probe_note"])),
        _tags(("mcp", "@probe/probe_note")),
    ]
    offered = cap.offered_after_activation(frames)
    assert offered.catalog_tags == ["@probe/probe_note"]
    assert offered.mcp_tags == ["@probe/probe_note"]


def test_no_snapshot_after_activation_is_unproven(cap):
    frames = [_tags(("mcp", "@probe/probe_note")), NEW_ANSWER, MODE_ANSWER]
    offered = cap.offered_after_activation(frames)
    verdict = cap.judge(["@probe"], {"tools": ["@probe"]}, offered, [], ["probe"])
    assert verdict.verdict == cap.VERDICT_UNPROVEN


# ── matching an offered tag to the projected set ────────────────────────────────


@pytest.mark.parametrize(
    ("projected", "excluded", "tag", "builtin", "expected"),
    [
        (["@probe"], [], "@probe/probe_note", False, True),
        (["@probe/probe_note"], [], "@probe/probe_note", False, True),
        (["@probe"], [], "@lab-user/lab_echo", False, False),
        ("*", [], "@lab-user/lab_echo", False, True),
        ("*", ["@lab-user"], "@lab-user/lab_echo", False, False),
        (["@probe"], ["@probe/probe_note"], "@probe/probe_note", False, False),
        (["fs_read", "read_file"], [], "read", True, True),
        (["@probe"], [], "read", True, False),
        (["execute_bash"], [], "shell", True, True),
        (["fs_write"], [], "shell", True, False),
        # A family this table does not know is outside every list bound.
        (["fs_read"], [], "brand-new-family", True, False),
        ("*", ["@builtin"], "read", True, False),
    ],
)
def test_admits(cap, projected, excluded, tag, builtin, expected):
    assert cap.admits(projected, excluded, tag, builtin=builtin) is expected


# ── the verdict ─────────────────────────────────────────────────────────────────


def _offered(cap, builtin=(), mcp=(), servers=None):
    offered = cap.Offered(tags_seen=True, builtin_tags=list(builtin), mcp_tags=list(mcp))
    offered.servers = servers or {}
    return offered


def test_inside_the_bound_is_bounded(cap):
    v = cap.judge(
        ["@probe"], {"tools": ["@probe"]}, _offered(cap, mcp=["@probe/probe_note"]), [], ["probe"]
    )
    assert v.verdict == cap.VERDICT_BOUNDED
    assert v.carried_by == "didChange"


def test_an_offered_builtin_outside_is_not_bounded(cap):
    v = cap.judge(
        ["@probe"], {"tools": ["@probe"]}, _offered(cap, builtin=["shell"]), [], ["probe"]
    )
    assert v.verdict == cap.VERDICT_NOT_BOUNDED
    assert v.offered_outside == ["builtin:shell"]


def test_a_wildcard_projection_is_never_bounded(cap):
    v = cap.judge("*", {"tools": "*"}, _offered(cap, mcp=["@probe/probe_note"]), [], ["probe"])
    assert v.verdict == cap.VERDICT_NOT_BOUNDED


def test_a_tool_mounted_after_projection_is_sent_outside(cap):
    sent = {"tools": ["@probe", "invoke_sub_agent"]}
    v = cap.judge(["@probe"], sent, _offered(cap, mcp=["@probe/probe_note"]), [], ["probe"])
    assert v.verdict == cap.VERDICT_NOT_BOUNDED
    assert v.sent_outside == ["invoke_sub_agent"]


def test_a_call_outside_is_not_bounded_and_engine_wrappers_are_not_calls(cap):
    calls = [
        cap.Call("tool_load", "completed", False),
        cap.Call("fetch_cloud_config", "completed", False),
        cap.Call("@probe/probe_note", "completed", False),
        cap.Call("@lab-user/lab_echo", "completed", True),
    ]
    v = cap.judge(
        ["@probe"],
        {"tools": ["@probe"]},
        _offered(cap, mcp=["@probe/probe_note"]),
        calls,
        ["probe"],
    )
    assert v.verdict == cap.VERDICT_NOT_BOUNDED
    assert v.calls_outside == ["@lab-user/lab_echo"]


def test_carried_by_names_the_source(cap):
    servers = {"probe": {"status": "connected", "layer": "agent", "tools": ["probe_note"]}}
    both = cap.judge(
        ["@probe"],
        {"tools": ["@probe"]},
        _offered(cap, mcp=["@probe/probe_note"], servers=servers),
        [],
        ["probe"],
    )
    status_only = cap.judge(
        ["@probe"], {"tools": ["@probe"]}, _offered(cap, servers=servers), [], ["probe"]
    )
    assert (both.carried_by, status_only.carried_by) == ("both", "status")


def test_a_started_undeclared_server_is_named(cap):
    servers = {"lab-user": {"status": "connected", "layer": "user-local", "tools": ["lab_echo"]}}
    v = cap.judge(["@probe"], {"tools": ["@probe"]}, _offered(cap, servers=servers), [], ["probe"])
    assert v.started_undeclared == ["lab-user"]


# ── calls ───────────────────────────────────────────────────────────────────────


def test_tool_calls_read_identity_status_and_ask(cap):
    def update(kind, call_id, **fields):
        return {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s",
                "update": {"sessionUpdate": kind, "toolCallId": call_id, **fields},
            },
        }

    frames = [
        update(
            "tool_call",
            "a",
            title="@probe/probe_note",
            status="pending",
            rawInput={"tool_id": "probe::probe_note"},
            _meta={"kiro": {"serverName": "probe"}},
        ),
        update("tool_call_update", "a", status="completed"),
        update(
            "tool_call",
            "b",
            title="Running",
            status="pending",
            _meta={"kiro": {"toolId": "execute_bash"}},
        ),
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "session/request_permission",
            "params": {"sessionId": "s", "toolCall": {"toolCallId": "b"}},
        },
        update("tool_call_update", "b", status="failed"),
    ]
    calls = {c.tool: (c.status, c.asked) for c in cap.tool_calls(frames)}
    assert calls == {"@probe/probe_note": ("completed", False), "execute_bash": ("failed", True)}


# ── reduction and the scratch home ──────────────────────────────────────────────


def test_reduce_drops_inventories_and_normalizes_ids_and_paths(cap):
    frames = [
        {
            "jsonrpc": "2.0",
            "method": "_kiro/steering/documents_changed",
            "params": {"documents": []},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "sessionId": "sess_1",
                "_meta": {"createdAt": "x"},
                "modes": {"availableModes": [{"id": "tools-probe"}, {"id": "other"}]},
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "_kiro/mcp/status",
            "params": {
                "sessionId": "sess_1",
                "servers": [
                    {
                        "name": "probe",
                        "krn": "krn:x:0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b",
                        "cwd": "/scratch/w",
                    }
                ],
            },
        },
    ]
    out = cap.reduce_frames(frames, session_id="sess_1", swaps=[("/scratch/w", "/work")])
    text = json.dumps(out)
    assert "documents_changed" not in text
    assert "sess_1" not in text and "kas-session-1" in text
    assert "0f1e2d3c" not in text and "synthetic-id-1" in text
    assert "/scratch/w" not in text and "/work" in text
    assert "createdAt" not in text and '"other"' not in text


def test_the_scratch_home_check_refuses_the_operators_own(cap, tmp_path):
    import os
    import pwd

    real = pwd.getpwuid(os.getuid()).pw_dir
    with pytest.raises(cap.CaptureError):
        cap.check_scratch_home(
            {"HOME": real, "KIROCREW_HOME": str(tmp_path), "KIRO_HOME": str(tmp_path)}
        )
    with pytest.raises(cap.CaptureError):
        cap.check_scratch_home({"HOME": str(tmp_path)})
    scratch = {
        "HOME": str(tmp_path),
        "KIROCREW_HOME": str(tmp_path / "c"),
        "KIRO_HOME": str(tmp_path / "k"),
    }
    assert cap.check_scratch_home(scratch) == tmp_path.resolve()


def test_an_unknown_variant_is_a_usage_error(cap):
    assert cap.main(["--variant", "no-such-variant"]) == cap.EXIT_USAGE


# ── prompted captures: sub-agent calls, permissions and chunk joining ───────────


def _update(kind, call_id, **fields):
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s",
            "update": {"sessionUpdate": kind, "toolCallId": call_id, **fields},
        },
    }


def test_a_sub_agent_and_its_shell_call_are_named(cap):
    frames = [
        _update(
            "tool_call",
            "a",
            title="Sub-agent: helper",
            status="pending",
            _meta={"kiro": {"kind": "agent-subtask"}},
        ),
        _update(
            "tool_call",
            "b",
            title="Print the word outside",
            status="pending",
            rawInput={"command": "echo outside"},
            _meta={"kiro": {"agentSubtaskId": "a"}},
        ),
        _update(
            "tool_call",
            "c",
            title="Load Resource",
            status="in_progress",
            rawInput={"id": "tool::probe::probe_note"},
        ),
    ]
    assert [c.tool for c in cap.tool_calls(frames)] == [
        "invoke_sub_agent",
        "execute_bash",
        "load_resource",
    ]


def test_a_sub_agents_shell_call_is_outside_a_bounded_spec(cap):
    calls = [
        cap.Call("invoke_sub_agent", "completed", True),
        cap.Call("execute_bash", "failed", True),
    ]
    projected = ["@probe", "use_subagent", "invoke_sub_agent"]
    v = cap.judge(
        projected, {"tools": projected}, _offered(cap, mcp=["@probe/probe_note"]), calls, ["probe"]
    )
    assert v.calls_outside == ["execute_bash"]


def test_only_the_probe_and_an_allowed_sub_agent_are_approved(cap):
    probe = {"toolCall": {"title": "@probe/probe_note"}}
    child = {
        "toolCall": {"title": "Sub-agent: helper", "_meta": {"kiro": {"kind": "agent-subtask"}}}
    }
    shell = {"toolCall": {"title": "echo outside"}}
    assert cap.permission_is_probe(probe) and not cap.permission_is_probe(shell)
    assert cap.permission_is_subagent(child) and not cap.permission_is_subagent(shell)
    options = {
        "options": [
            {"optionId": "y", "kind": "allow_once"},
            {"optionId": "n", "kind": "reject_once"},
        ]
    }
    assert cap._reject_option(options) == "n"
    assert cap._option_of_kind(options, ("allow_once",)) == "y"


def test_one_messages_chunks_are_joined_and_others_are_not(cap):
    def chunk(text, reply):
        return {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                    "_meta": {"kiro": {"replayId": reply}},
                },
            },
        }

    frames = [
        chunk("a", "r1"),
        chunk("b", "r1"),
        chunk("c", "r2"),
        {"jsonrpc": "2.0", "id": 4, "result": {}},
    ]
    out = cap.join_message_chunks(frames)
    texts = [f["params"]["update"]["content"]["text"] for f in out if "params" in f]
    assert texts == ["ab", "c"]
    assert len(out) == 3
