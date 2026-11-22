"""File-change snapshots for the write tools of harnesses other than kiro-cli.

Each harness's live frames are replayed through the real ``session/update``
parser and every tool event goes to ``_snapshot_tool_writes``, as the dashboard's
two tool-call handlers send it. The file is on disk with its pre-write content,
because these frames arrive before the call's permission request: the snapshot
must name that file and hold that content.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from kiro_crew.acp._dispatch_parts.session_update_parser import parse_session_update
from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_CALL_UPDATE
from kiro_crew.dashboard.chat_runner import _client_cwd, _snapshot_tool_writes

_FRAMES = Path(__file__).parent / "fixtures" / "acp_frames"


def _replay(fixture: str, placeholder: str, root: Path, cwd: str) -> list[tuple[str, dict]]:
    """``(toolCallId, snapshot)`` for every snapshot the fixture's tool events yield."""
    caches: dict = {
        "tool_input_cache": {},
        "shell_cache": {},
        "raw_params_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
        "tool_input_redacted_cache": {},
        "cache_scope": "test",
    }
    lines = (_FRAMES / fixture).read_text(encoding="utf-8").splitlines()[1:]
    out: list[tuple[str, dict]] = []
    for line in lines:
        # JSON-escaped, so a Windows path's backslashes stay valid inside the frame.
        frame = json.loads(line.replace(placeholder, json.dumps(str(root))[1:-1]))
        if frame.get("method") != "session/update":
            continue
        for event in parse_session_update(frame["params"]["update"], **caches):
            if event.kind in (EVENT_TOOL_CALL, EVENT_TOOL_CALL_UPDATE):
                out.extend((event.tool_call_id, snap) for snap in _snapshot_tool_writes(event, cwd))
    return out


def _first_per_call(snaps: list[tuple[str, dict]]) -> dict[str, dict]:
    first: dict[str, dict] = {}
    for call_id, snap in snaps:
        first.setdefault(call_id, snap)
    return first


def test_opencode_write_and_edit_snapshot_the_file(tmp_path: Path) -> None:
    probe = tmp_path / "probe.txt"
    probe.write_text("hello\n")
    first = _first_per_call(_replay("opencode/write-edit-live.jsonl", "<cwd>", tmp_path, cwd=""))
    assert set(first) == {"call_1_0", "call_2_0"}
    for snap in first.values():
        assert Path(snap["path"]) == probe
        assert snap["content"] == "hello\n"


def test_pi_relative_paths_join_the_session_directory(tmp_path: Path) -> None:
    probe = tmp_path / "probe.txt"
    probe.write_text("hello\n")
    first = _first_per_call(
        _replay("pi/write-edit-live.jsonl", "<cwd>", tmp_path, cwd=str(tmp_path))
    )
    assert set(first) == {"call_1_0", "call_2_0"}
    assert all(Path(s["path"]) == probe and s["content"] == "hello\n" for s in first.values())


def test_a_relative_path_without_a_session_directory_names_no_file(tmp_path: Path) -> None:
    (tmp_path / "probe.txt").write_text("hello\n")
    assert _replay("pi/write-edit-live.jsonl", "<cwd>", tmp_path, cwd="") == []
    assert _replay("pi/write-edit-live.jsonl", "<cwd>", tmp_path, cwd="relative") == []


def test_claude_edit_snapshots_before_the_permission_and_skips_reads(tmp_path: Path) -> None:
    note = tmp_path / "note.txt"
    note.write_text("alpha\n")
    snaps = _replay("claude/builtins-bash-edit-webfetch-live.jsonl", "/work", tmp_path, cwd="")
    # The ``{file_path}`` frame alone is also how a Read starts, so the first
    # snapshot is the ``{file_path, old_string}`` frame, still before the ask.
    assert snaps, "the Edit recorded no snapshot"
    assert {Path(s["path"]) for _, s in snaps} == {note}
    assert snaps[0][1]["content"] == "alpha\n"
    assert "fragment_before" not in snaps[0][1]


def test_copilot_apply_patch_string_snapshots_each_file(tmp_path: Path) -> None:
    snaps = _replay(
        "copilot/mcp-stdio-mount-live.jsonl", "/work/copilot-probe", tmp_path, cwd=str(tmp_path)
    )
    # The patch adds the file, so its before is empty.
    assert [(Path(s["path"]), s["content"], s["truncated"]) for _, s in snaps] == [
        (tmp_path / "edited.txt", "", False)
    ]


def test_deepseek_write_snapshots_the_file(tmp_path: Path) -> None:
    (tmp_path / "written.txt").write_text("old\n")
    snaps = _replay("deepseek/side-read-only-live.jsonl", "/work", tmp_path, cwd=str(tmp_path))
    assert [(Path(s["path"]), s["content"]) for _, s in snaps] == [
        (tmp_path / "written.txt", "old\n")
    ]


def _event(**fields: object) -> SimpleNamespace:
    base: dict = {
        "raw_tool_params": None,
        "diff_old_text": None,
        "diff_path": "",
        "mcp_server_name": "",
        "tool_kind": "",
        "tool_input": "",
        "tool_input_redacted": False,
    }
    base.update(fields)
    return SimpleNamespace(**base)


def test_shapes_that_are_not_a_builtin_write_record_nothing(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("x")
    write = {"filePath": str(f), "content": "y"}
    # An MCP tool with the same argument names is not the harness's write.
    assert _snapshot_tool_writes(_event(raw_tool_params=write, mcp_server_name="probe")) == []
    # A path alone is how a read starts.
    assert _snapshot_tool_writes(_event(raw_tool_params={"file_path": str(f)})) == []
    # The reserved purpose argument rides on any tool and does not change the shape.
    assert _snapshot_tool_writes(_event(raw_tool_params={**write, "__tool_use_purpose": "p"}))
    # A key outside the shape.
    assert _snapshot_tool_writes(_event(raw_tool_params={**write, "mode": "a"})) == []
    patch = f"*** Begin Patch\n*** Update File: {f}\n@@\n-x\n+y\n*** End Patch\n"
    assert _snapshot_tool_writes(_event(tool_kind="edit", tool_input=patch))
    # Every file a patch names is snapshotted; the turn budget, not the reader, caps them.
    many = "".join(f"*** Add File: {tmp_path / f'n{i}.txt'}\n+x\n" for i in range(60))
    big = _event(tool_kind="edit", tool_input=f"*** Begin Patch\n{many}*** End Patch\n")
    assert len(_snapshot_tool_writes(big)) == 60
    # A redacted patch may name other files than the ones it writes.
    redacted = _event(tool_kind="edit", tool_input=patch, tool_input_redacted=True)
    assert _snapshot_tool_writes(redacted) == []


def test_sensitive_paths_are_refused(tmp_path: Path, monkeypatch) -> None:
    # Path.home() reads USERPROFILE on Windows and never HOME; pin both.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    creds = tmp_path / ".aws" / "credentials"
    creds.parent.mkdir()
    creds.write_text("[default]\n")
    params = {"filePath": str(creds), "content": "x"}
    assert _snapshot_tool_writes(_event(raw_tool_params=params)) == []


def test_kiro_cli_shapes_keep_their_reader(tmp_path: Path) -> None:
    f = tmp_path / "k.txt"
    f.write_text("before\n")
    event = _event(
        raw_tool_params={"command": "create", "path": str(f), "content": "after\n"},
        diff_old_text="before\n",
        diff_path=str(f),
    )
    (snap,) = _snapshot_tool_writes(event, str(tmp_path))
    # create's content block is the before, with the digest only that path sets.
    assert snap["content"] == "before\n" and "pre_call_digest" in snap


def test_client_cwd_reads_only_a_string() -> None:
    assert _client_cwd(SimpleNamespace(cwd="/w")) == "/w"
    assert _client_cwd(SimpleNamespace(cwd=None)) == ""
    assert _client_cwd(object()) == ""
