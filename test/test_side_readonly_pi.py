"""The safe scope of a pi Side Chat turn: envelope-proven read builtins.

pi states no tool name on its own frames; its gate-extension envelope carries
the harness's own tool name (nonce-verified). On a ``side_read_only`` session
that name is mapped through ``PI_READ_ONLY_BUILTINS`` onto the permission
event as ``harness_builtin_tool``, where the READ_ONLY gate auto-approves the
read builtins (read/grep/find/ls) and refuses everything else. The side
launcher passes ``--exclude-tools bash,powershell``, so pi has no shell there. pi's extension renders grep/find/ls as kind
``search``; a side session rewrites that to ``read`` once the name mapped to a
read builtin, so the shared gate's "search narrows" rule is unchanged. Off a side session
the envelope names nothing, so the normal-turn gate sees the input it saw
before.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from kiro_crew.acp._dispatch import GATE_ENVELOPE_MARKER, build_permission_event
from kiro_crew.acp.harness_tool_names import PI_READ_ONLY_BUILTINS
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_PI, ACP_BACKENDS_SIDE_READONLY
from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, HookManager

NONCE = "f00dfeed" * 4
# Absolute on the host running the test: on Windows a POSIX ``/tmp/...`` has no
# drive, so the dispatch guard would rightly map nothing.
_ABS_PATH = str(Path(tempfile.gettempdir()) / "a.txt")


def _envelope(**overrides) -> str:
    body = {
        GATE_ENVELOPE_MARKER: 1,
        "nonce": NONCE,
        "toolCallId": "call_pi_side",
        "tool": "read",
        "kind": "read",
        "input": {"path": _ABS_PATH},
    }
    body.update(overrides)
    return json.dumps(body)


def _permission_frame(message: str, title: str = "read") -> JsonRpcMessage:
    """pi-acp's rendering of a confirm dialog, as captured live."""
    return JsonRpcMessage(
        id=0,
        method="session/request_permission",
        params={
            "sessionId": "s",
            "toolCall": {
                "toolCallId": "pi-ui-9d3bda1a",
                "title": title,
                "kind": "other",
                "status": "pending",
                "rawInput": {"method": "confirm", "title": title, "message": message},
            },
            "options": [
                {"optionId": "yes", "name": "Yes", "kind": "allow_once"},
                {"optionId": "no", "name": "No", "kind": "reject_once"},
            ],
        },
    )


def _side_event(tool: str, kind: str, **overrides):
    servers = overrides.pop("servers", ())
    event, _ = build_permission_event(
        _permission_frame(_envelope(tool=tool, kind=kind), title=tool),
        gate_envelope_nonce=NONCE,
        harness_backend=overrides.pop("backend", ACP_BACKEND_PI),
        harness_mcp_servers=servers,
        side_read_only=overrides.pop("side_read_only", True),
        **overrides,
    )
    assert event is not None
    return event


@pytest.mark.parametrize(
    ("tool", "builtin"),
    [
        ("read", "fs_read"),
        ("grep", "grep"),
        ("find", "glob"),
        ("ls", "fs_read"),
        ("write", ""),
        ("edit", ""),
        ("bash", ""),
    ],
)
def test_the_envelope_names_the_read_builtin_for_side_chat(tool, builtin):
    assert _side_event(tool, "read").harness_builtin_tool == builtin


def test_an_mcp_rewritten_title_matches_nothing():
    """A Crew server's tool arrives in the server's spelling after
    ``pi_mcp_title``; it is not a bare builtin name."""
    event = _side_event("mcp__myserver__read", "read", servers=("myserver",))
    assert event.harness_builtin_tool == ""


def test_only_the_pi_backend_reads_the_pi_table():
    event = _side_event("read", "read", backend="")
    assert event.harness_builtin_tool == ""


def test_a_main_chat_pi_permission_event_names_no_builtin():
    # Outside Side Chat the name would bind kiro-cli-named deny rules to pi.
    event = _side_event("read", "read", side_read_only=False)
    assert event.harness_builtin_tool == ""


def test_a_dialog_without_an_envelope_names_no_builtin():
    event, _ = build_permission_event(
        _permission_frame("Allow rm -rf?", title="Dangerous!"),
        harness_backend=ACP_BACKEND_PI,
        side_read_only=True,
    )
    assert event is not None
    assert event.harness_builtin_tool == ""


def test_every_pi_read_builtin_maps_to_a_host_read_only_tool():
    from kiro_crew.hooks import _HOST_READ_ONLY_BUILTIN_TOOLS

    assert set(PI_READ_ONLY_BUILTINS.values()) <= _HOST_READ_ONLY_BUILTIN_TOOLS


def test_pi_joins_the_side_readonly_set():
    assert ACP_BACKEND_PI in ACP_BACKENDS_SIDE_READONLY


def _judge(**kwargs):
    kwargs.setdefault("session_key", "side:test")
    return HookManager().on_tool_call("pi tool", classifier_only=True, **kwargs)


def test_a_proven_read_auto_approves():
    r = _judge(harness_builtin_tool="fs_read", tool_kind="read", raw_params={"path": "/tmp/a.txt"})
    assert r.action == TOOL_AUTO_APPROVE
    assert r.read_only is True


def test_a_side_grep_search_kind_is_rewritten_to_read():
    """pi's extension renders grep/find/ls as kind ``search``. The rewrite is
    at this dispatch site, not in the shared gate, so a claude Grep of the
    same kind still asks."""
    event = _side_event("grep", "search")
    assert event.harness_builtin_tool == "grep"
    assert event.tool_kind == "read"


def test_an_unmapped_search_kind_is_kept():
    event = _side_event("write", "search")
    assert event.harness_builtin_tool == ""
    assert event.tool_kind == "search"


def test_a_proven_read_with_search_kind_still_asks_at_the_gate():
    """The shared tier does not special-case ``search``. pi reaches an
    auto-approve only after dispatch rewrote the kind."""
    r = _judge(harness_builtin_tool="grep", tool_kind="search", raw_params={"path": "/tmp/a.txt"})
    assert r.action == TOOL_ALLOW


def test_a_relative_path_maps_no_builtin():
    """pi resolves a relative path against its work dir while the
    sensitive-path tiers resolve it against the gateway's, so a relative
    target must not reach the gate as a proven read."""
    event, _ = build_permission_event(
        _permission_frame(_envelope(kind="search", input={"path": "../../.ssh/id_rsa"})),
        gate_envelope_nonce=NONCE,
        harness_backend=ACP_BACKEND_PI,
        side_read_only=True,
    )
    assert event is not None
    assert event.harness_builtin_tool == ""
    assert event.tool_kind == "search"


def test_a_missing_path_maps_no_builtin():
    event, _ = build_permission_event(
        _permission_frame(_envelope(input={})),
        gate_envelope_nonce=NONCE,
        harness_backend=ACP_BACKEND_PI,
        side_read_only=True,
    )
    assert event is not None
    assert event.harness_builtin_tool == ""


def test_a_proven_read_with_a_write_kind_still_asks():
    """Kinds narrow, never widen: ``execute`` refuses even a proven read."""
    r = _judge(
        harness_builtin_tool="fs_read", tool_kind="execute", raw_params={"path": "/tmp/a.txt"}
    )
    assert r.action == TOOL_ALLOW


def test_search_kind_without_a_proven_builtin_still_asks():
    r = _judge(tool_kind="search", raw_params={"path": "/tmp/a.txt"})
    assert r.action == TOOL_ALLOW


def test_a_proven_read_behind_a_server_still_asks():
    r = _judge(harness_builtin_tool="fs_read", mcp_server_name="x", tool_kind="read")
    assert r.action != TOOL_AUTO_APPROVE


def test_the_committed_side_capture_replays_its_gate_envelopes():
    """The corpus file the set cites replays through the dispatch parsers.

    ``test_acp_frame_replay`` already snapshots every fixture; this pins what
    that snapshot must KEEP meaning for this member: four permission frames,
    each read back as its gate envelope (``read``, ``grep``, ``find`` and ``ls``,
    each with its trusted absolute path and mapped to its host read-only tool),
    so the capture stays evidence that every Side Chat call on pi asks through
    the extension first. The shell
    calls predate ``--exclude-tools bash,powershell``; a side session has no shell now.
    """
    from pathlib import Path

    from acp_frame_replay_harness import read_fixture, replay_frames

    fixture = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "acp_frames"
        / "pi"
        / "side-read-only-live.jsonl"
    )
    meta, frames = read_fixture(fixture)
    assert meta["recorded"] == "live"
    assert meta["backend"] == "pi"
    replayed = replay_frames(frames, gate_envelope_nonce=meta.get("gate_envelope_nonce"))
    permissions = [e for e in replayed if e.get("action") == "permission"]
    assert len(permissions) == 4
    events = [p["events"][0] for p in permissions]
    assert [e["title"] for e in events] == ["read", "grep", "find", "ls"]
    assert [e["tool_kind"] for e in events] == ["read", "search", "search", "search"]
    assert all(e["title"] in PI_READ_ONLY_BUILTINS for e in events)
    assert events[0]["raw_tool_params"] == {"path": "/work/note.txt"}
    assert events[1]["raw_tool_params"] == {"pattern": "hello", "path": "/work"}
    assert events[2]["raw_tool_params"] == {"pattern": "*.txt", "path": "/work"}
    assert events[3]["raw_tool_params"] == {"path": "/work"}
    # The confined launcher drops the shell, so no call in the capture is one.
    assert not any(e["title"] in ("bash", "powershell") for e in events)
