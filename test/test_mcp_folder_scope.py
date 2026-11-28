"""Folder scoping on a shared MCP backend: the caller's folder, path args and roots.

A shared backend runs in one Crew-private folder. These tests pin how the
gateway serves several chats from it (``mcp_gateway.folder_scope`` and
``backend_caller_scope``): relative folder arguments resolve against the
CALLER's folder, a required ``repo_path`` is filled, ``roots/list`` is answered
by the gateway with every attached chat's folder, and a server whose tools
cannot be scoped keeps the routing it had before.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp_gateway_backend_coverage_helpers import _no_real_metrics_file  # noqa: F401 -- autouse
from mcp_gateway_backend_coverage_helpers import _frames, _line
from mcp_gateway_backend_coverage_helpers import _make_backend as _make_plain_backend

from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway import backend_caller_scope as caller_scope
from kiro_crew.mcp_gateway import call_gate, claim, folder_scope, hazards, tab_tenancy
from kiro_crew.mcp_gateway.daemon import control
from kiro_crew.testing.wait import async_wait_until, await_until

FS_TOOLS = [
    {
        "name": "read_text_file",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "move_file",
        "inputSchema": {
            "type": "object",
            "properties": {"source": {"type": "string"}, "destination": {"type": "string"}},
        },
    },
]
GIT_TOOL = {
    "name": "git_status",
    "inputSchema": {
        "type": "object",
        "properties": {"repo_path": {"type": "string"}},
        "required": ["repo_path"],
    },
}
SHELL_TOOL = {
    "name": "run",
    "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}}},
}


_make_backend = _make_plain_backend


@pytest.fixture(autouse=True)
def _shared_crew_off(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """The owning gateway said Shared Crew is off: one local user, roots may be shared."""
    _floor_monkeypatch.setattr(caller_scope, "_SHARED_ROOTS", [True])


@pytest.fixture(autouse=True)
def _fresh_folders() -> Any:
    folder_scope.forget_session_folders()
    yield
    folder_scope.forget_session_folders()


def _folders(tmp_path: Path) -> tuple[str, str]:
    a, b = tmp_path / "projA", tmp_path / "projB"
    a.mkdir()
    b.mkdir()
    folder_scope.record_session_folder("dashboard:chat-a", str(a))
    folder_scope.record_session_folder("dashboard:chat-b", str(b))
    return str(a), str(b)


# --- pure rules -------------------------------------------------------------


def test_scan_tool_confinable_and_fill() -> None:
    assert folder_scope.scan_tool(FS_TOOLS[0]) == folder_scope.ToolScope(True, ())
    assert folder_scope.scan_tool(GIT_TOOL) == folder_scope.ToolScope(True, ("repo_path",))
    assert folder_scope.scan_tool(SHELL_TOOL).confinable is False
    assert folder_scope.scan_tool({"name": "x"}).confinable is False
    assert folder_scope.tools_confinable(FS_TOOLS + [GIT_TOOL]) is True
    assert folder_scope.tools_confinable(FS_TOOLS + [SHELL_TOOL]) is False
    assert folder_scope.tools_confinable([]) is None


def test_scope_arguments_rewrites_only_what_applies(tmp_path: Path) -> None:
    a, b = _folders(tmp_path)
    git = folder_scope.scan_tool(GIT_TOOL)
    fs = folder_scope.scan_tool(FS_TOOLS[0])
    # A relative folder argument and a missing required one resolve to the caller's folder.
    assert folder_scope.scope_arguments({"repo_path": "."}, git, a, folder_user=False) == {
        "repo_path": a
    }
    assert folder_scope.scope_arguments({}, git, b, folder_user=False) == {"repo_path": b}
    # A plain path is left alone unless the server works on the chat's folder.
    assert folder_scope.scope_arguments({"path": "note.txt"}, fs, a, folder_user=False) == {
        "path": "note.txt"
    }
    assert folder_scope.scope_arguments({"path": "note.txt"}, fs, b, folder_user=True) == {
        "path": os.path.join(b, "note.txt")
    }
    # An absolute path into another chat's folder is not refused: one local user.
    other = os.path.join(a, "secret.txt")
    assert folder_scope.scope_arguments({"path": other}, fs, b, folder_user=True) == {"path": other}
    # Nothing known, nothing changed.
    args = {"path": "x"}
    assert folder_scope.scope_arguments(args, None, a, folder_user=True) is args
    assert folder_scope.scope_arguments(args, fs, "", folder_user=True) is args


def test_only_absolute_folders_are_recorded() -> None:
    assert folder_scope.record_session_folder("s", "relative/dir") is False
    assert folder_scope.record_session_folder("s", 7) is False
    assert folder_scope.session_folder("s") == ""


def test_an_oversized_session_key_is_refused_not_kept(tmp_path: Path) -> None:
    longest = "k" * folder_scope._MAX_SESSION_KEY_LEN
    assert folder_scope.record_session_folder(longest, str(tmp_path)) is True
    too_long = longest + "k"
    assert folder_scope.record_session_folder(too_long, str(tmp_path)) is False
    assert too_long not in folder_scope._FOLDERS


def test_a_full_session_table_forgets_its_oldest_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(folder_scope, "_MAX_SESSIONS", 2)
    with caplog.at_level("WARNING", logger="kiro_crew.mcp_gateway.backend"):
        for key in ("s1", "s2", "s3", "s4"):
            folder_scope.record_session_folder(key, str(tmp_path))
    assert list(folder_scope._FOLDERS) == ["s3", "s4"]
    assert folder_scope._EVICTED[0] == 2
    assert sum("folder is forgotten" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_a_full_backend_makes_room_from_a_detached_chat_or_counts_the_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(caller_scope, "_MAX_SEEN_FOLDERS", 2)
    folders = {}
    for key in ("dashboard:c1", "dashboard:c2", "dashboard:c3", "dashboard:c4"):
        folders[key] = tmp_path / key.split(":")[1]
        folders[key].mkdir()
        folder_scope.record_session_folder(key, str(folders[key]))
    backend = _make_backend()
    for key in ("dashboard:c1", "dashboard:c2"):
        await backend.attach_stub(f"stub-{key}")
        await caller_scope.note_caller(backend, f"stub-{key}", _caller(key))
    await backend.detach_stub("stub-dashboard:c1")
    await backend.attach_stub("stub-dashboard:c3")
    await caller_scope.note_caller(backend, "stub-dashboard:c3", _caller("dashboard:c3"))
    st = caller_scope.state(backend)
    assert st.seen_folders == [
        str(folders["dashboard:c2"]),
        str(folders["dashboard:c3"]),
    ], "c1 made room"
    with caplog.at_level("WARNING", logger="kiro_crew.mcp_gateway.backend"):
        await backend.attach_stub("stub-dashboard:c4")
        await caller_scope.note_caller(backend, "stub-dashboard:c4", _caller("dashboard:c4"))
    assert str(folders["dashboard:c4"]) not in st.seen_folders, "every served folder is in use"
    assert st.folders_left_out >= 1
    assert any("is not served" in r.getMessage() for r in caplog.records)


def test_an_app_callbacks_unsent_call_is_not_held_for_a_resend() -> None:
    backend = _make_backend()
    frame = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
    assert caller_scope.keep_for_resend(backend, "__app_call__abc", frame) is False
    assert caller_scope.keep_for_resend(backend, "stub-a", frame) is True
    assert caller_scope.take_resend(backend, "__app_call__abc") == []


def test_claim_frame_carries_the_session_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claim.platform_compat, "get_process_start_id", lambda pid: None)
    frame = claim.build_claim_frame(4321, "dashboard:chat-a", None, "tok", "/work/projA")
    assert frame["work_dir"] == "/work/projA"
    assert "work_dir" not in claim.build_claim_frame(4321, "dashboard:chat-a", None, "tok")


@pytest.mark.asyncio
async def test_apply_claim_records_the_folder(tmp_path: Path) -> None:
    frame = {
        "type": "claim",
        "pid": 4321,
        "caller": {"session_key": "dashboard:chat-z"},
        "work_dir": str(tmp_path),
    }
    await control._apply_claim(frame)
    assert (
        folder_scope.session_folder("dashboard:chat-z") == ""
    ), "an unproven sender's folder is not kept"
    await control._apply_claim(frame, folder_trusted=True)
    assert folder_scope.session_folder("dashboard:chat-z") == str(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(("sender", "kept"), [(4100, True), (4199, False)])
async def test_a_claims_folder_is_taken_only_from_the_daemons_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sender: int, kept: bool
) -> None:
    """Any process at this uid can reach the socket; only the owning gateway names folders."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew import platform_compat
    from kiro_crew.mcp_gateway import gatewayd as gw
    from kiro_crew.mcp_gateway import socketsec

    monkeypatch.setattr(gw, "_OWNER_PID", 4100)
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _w: sender)
    monkeypatch.setattr(platform_compat, "is_exec_supervisor_of", lambda _o, _p: False)
    writer = MagicMock()
    writer.drain = AsyncMock()
    frame = {
        "type": "claim",
        "pid": 4321,
        "caller": {"session_key": "dashboard:chat-y"},
        "work_dir": str(tmp_path),
    }
    served = await control._serve_control_frame(frame, writer, MagicMock(), None)
    assert served is True
    assert folder_scope.session_folder("dashboard:chat-y") == (str(tmp_path) if kept else "")


def test_retract_forgets_only_roots_for_one_launch(tmp_path: Path) -> None:
    ledger = hazards.HazardLedger(tmp_path / "h.json")
    ledger.record("fs", hazards.HAZARD_ROOTS_REQUESTED, "id1")
    ledger.record("fs", hazards.HAZARD_ROOTS_REQUESTED, "id2")
    assert ledger.retract("fs", hazards.HAZARD_ROOTS_REQUESTED, "id1") is True
    assert ledger.codes_for("fs", "id1") == ()
    assert ledger.codes_for("fs", "id2") == (hazards.HAZARD_ROOTS_REQUESTED,)
    assert hazards.retract_observed("fs", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST, "id2") is False


# --- the shared backend -----------------------------------------------------


def _caller(key: str) -> CallerContext:
    return CallerContext(session_key=key, from_gateway=True)


async def _two_chats(tmp_path: Path, tools: Any) -> Any:
    a, b = _folders(tmp_path)
    backend = _make_backend()
    inbox_a = await backend.attach_stub("stub-a")
    inbox_b = await backend.attach_stub("stub-b")
    await caller_scope.note_caller(backend, "stub-a", _caller("dashboard:chat-a"))
    await caller_scope.note_caller(backend, "stub-b", _caller("dashboard:chat-b"))
    if tools is not None:
        caller_scope.note_tools_list(
            backend, backend_pending("tools/list"), {"result": {"tools": tools}}
        )
    return backend, inbox_a, inbox_b, a, b


def backend_pending(method: str) -> Any:
    from kiro_crew.mcp_gateway.backend import _PendingRequest

    return _PendingRequest(stub_uuid="stub-a", original_id=1, method=method)


def _roots_answers(backend: Any) -> list[dict]:
    return [f for f in _frames(backend) if f.get("id") == "srv-roots" and "result" in f]


@pytest.mark.asyncio
async def test_gateway_answers_roots_with_every_chats_folder(tmp_path: Path) -> None:
    backend, inbox_a, inbox_b, a, b = await _two_chats(tmp_path, FS_TOOLS)
    await backend._route_backend_line(_line({"id": "srv-roots", "method": "roots/list"}))
    await async_wait_until(lambda: _roots_answers(backend))
    roots = _roots_answers(backend)[0]["result"]["roots"]
    assert [r["uri"] for r in roots] == [Path(a).as_uri(), Path(b).as_uri()]
    # Not routed to a chat, not recycled, and not recorded as per-chat evidence.
    assert inbox_a.empty() and inbox_b.empty()
    assert backend.dead_reason is None
    assert caller_scope.state(backend).roots_served is True


@pytest.mark.asyncio
async def test_with_shared_crew_on_a_folder_server_keeps_per_chat_roots(
    tmp_path: Path,
) -> None:
    """Sessions may belong to different people: no union, no offer, no retraction."""
    backend, inbox_a, _b, a, b = await _two_chats(tmp_path, FS_TOOLS)
    caller_scope.set_shared_roots(False)
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"capabilities": {}}}
    assert caller_scope.offer_roots(backend, init) is init, "no roots offered for the stubs"
    assert caller_scope.claim_roots_request(backend, {"id": "r", "method": "roots/list"}) is False
    assert not caller_scope.may_serve_roots(backend)
    call = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": "read_text_file", "arguments": {"path": "note.txt"}},
    }
    await backend.forward_from_stub("stub-b", call, caller=_caller("dashboard:chat-b"))
    (sent,) = [f for f in _frames(backend) if f.get("method") == "tools/call"]
    assert sent["params"]["arguments"] == {"path": "note.txt"}, "paths are the server's own"


@pytest.mark.parametrize(
    ("key", "local"),
    [
        ("dashboard:chat-1", True),
        ("dashboard:chat-1-parent:sub-7", True),
        ("cli_chat", True),
        ("slack:C1:1700000000.1", False),
        ("discord:123", False),
        ("cron:job-1", False),
        ("webex:room", False),
        ("dashboard", False),
        ("cli_chat:x", False),
        ("", False),
        (None, False),
    ],
)
def test_only_the_operators_own_surfaces_count_as_local(key: Any, local: bool) -> None:
    assert caller_scope.local_session(key) is local


@pytest.mark.asyncio
async def test_a_backend_with_an_unknown_or_channel_chat_keeps_per_chat_roots(
    tmp_path: Path,
) -> None:
    backend, _ia, _ib, a, b = await _two_chats(tmp_path, FS_TOOLS)
    folder_scope.record_session_folder("telegram:42", str(tmp_path))
    await backend.attach_stub("stub-t")
    await caller_scope.note_caller(backend, "stub-t", _caller("telegram:42"))
    assert not caller_scope.union_allowed(backend)
    assert caller_scope.claim_roots_request(backend, {"id": "r", "method": "roots/list"}) is False
    assert str(tmp_path) not in caller_scope.state(backend).seen_folders
    await backend.detach_stub("stub-t")
    await backend.attach_stub("stub-u")
    assert not caller_scope.union_allowed(backend), "a stub not attributed yet is not local"


@pytest.mark.asyncio
async def test_a_channel_chat_joining_a_union_server_retires_it_before_its_first_frame(
    tmp_path: Path,
) -> None:
    from kiro_crew.mcp_gateway.backend import BackendGone

    backend, *_rest = await _two_chats(tmp_path, FS_TOOLS)
    assert await _served_roots(backend, "r1"), "the two local chats share one roots answer"
    before = len(_frames(backend))
    await backend.attach_stub("stub-s")
    call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "read_text_file", "arguments": {"path": "note.txt"}},
    }
    with pytest.raises(BackendGone) as gone:
        await backend.forward_from_stub("stub-s", call, caller=_caller("slack:C1:1.2"))
    assert caller_scope.never_sent(gone.value), "the replacement runs it, per chat"
    assert backend._gone_broadcast
    assert len(_frames(backend)) == before, "nothing of the channel chat reached the server"


@pytest.mark.parametrize(
    ("preview", "off"), [(False, True), (True, False), (RuntimeError("x"), False)]
)
def test_the_gateway_reports_shared_crew_off_only_when_it_knows(
    monkeypatch: pytest.MonkeyPatch, preview: Any, off: bool
) -> None:
    import kiro_crew.shared_crew.preview as sc_preview

    def _enabled() -> bool:
        if isinstance(preview, Exception):
            raise preview
        return bool(preview)

    monkeypatch.setattr(sc_preview, "preview_enabled", _enabled)
    assert folder_scope.shared_crew_off() is off


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sender", "said", "shared"),
    [(4100, True, True), (4100, False, False), (4100, None, False), (4199, True, False)],
)
async def test_only_the_owners_announce_lets_roots_be_shared(
    monkeypatch: pytest.MonkeyPatch, sender: int, said: Any, shared: bool
) -> None:
    """A daemon starts per chat; the owner's verified announce opens sharing."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew import platform_compat
    from kiro_crew.mcp_gateway import gatewayd as gw
    from kiro_crew.mcp_gateway import socketsec

    caller_scope.set_shared_roots(False)
    monkeypatch.setattr(gw, "_OWNER_PID", 4100)
    monkeypatch.setattr(gw, "_OWNER_PORT", 0)
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _w: sender)
    monkeypatch.setattr(platform_compat, "is_exec_supervisor_of", lambda _o, _p: False)
    pool = MagicMock()
    pool.drain_matching_to_bluegreen = AsyncMock(return_value=0)
    frame: dict[str, Any] = {"type": "owner-port", "port": 4555}
    if said is not None:
        frame["shared_crew_off"] = said
    await control._apply_owner_port(frame, MagicMock(), pool)
    assert caller_scope.shared_roots() is shared


@pytest.mark.asyncio
async def test_shared_crew_turning_on_retires_backends_serving_chat_folders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from unittest.mock import MagicMock

    from kiro_crew import platform_compat
    from kiro_crew.mcp_gateway import gatewayd as gw
    from kiro_crew.mcp_gateway import socketsec

    monkeypatch.setattr(gw, "_OWNER_PID", 4100)
    monkeypatch.setattr(gw, "_OWNER_PORT", 4555)
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _w: 4100)
    monkeypatch.setattr(platform_compat, "is_exec_supervisor_of", lambda _o, _p: False)
    served, plain = _make_backend(), _make_backend()
    caller_scope.state(served).roots_served = True
    retired: list[Any] = []

    async def _drain(match: Any) -> int:
        retired.extend(b for b in (served, plain) if match(b))
        return len(retired)

    pool = MagicMock()
    pool.drain_matching_to_bluegreen = _drain
    frame = {"type": "owner-port", "port": 4555, "shared_crew_off": False}
    await control._apply_owner_port(frame, MagicMock(), pool)
    assert caller_scope.shared_roots() is False
    assert retired == [served]


@pytest.mark.asyncio
async def test_the_owner_port_announce_carries_the_gateways_shared_crew_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from kiro_crew.mcp_gateway import manager

    sent: list[dict[str, Any]] = []

    async def _roundtrip(frame: dict[str, Any]) -> dict[str, Any]:
        sent.append(frame)
        return {"type": "owner-port-set", "port": frame["port"]}

    monkeypatch.setattr(manager, "shared_crew_off", lambda: True)
    gm = manager.GatewayManager.__new__(manager.GatewayManager)
    gm._owner_port = 4555
    gm._owner_port_warned = False
    gm._spec = SimpleNamespace(socket_path="/tmp/gw.sock")
    gm._control_roundtrip = _roundtrip  # type: ignore[method-assign]
    assert await gm._send_owner_port() is True
    assert sent == [{"type": "owner-port", "port": 4555, "shared_crew_off": True}]


@pytest.mark.asyncio
async def test_unknown_tools_are_listed_before_roots_are_answered(tmp_path: Path) -> None:
    backend, _a, _b, _fa, _fb = await _two_chats(tmp_path, None)
    await backend._route_backend_line(_line({"id": "srv-roots", "method": "roots/list"}))

    def _listing() -> Any:
        return next((f for f in _frames(backend) if f.get("method") == "tools/list"), None)

    await async_wait_until(lambda: _listing() is not None)
    await backend._route_backend_line(
        _line({"id": _listing()["id"], "result": {"tools": FS_TOOLS}})
    )
    await async_wait_until(lambda: _roots_answers(backend))
    assert caller_scope.state(backend).folder_confinable is True


@pytest.mark.asyncio
async def test_a_channel_chat_attaching_while_the_tools_are_listed_gets_no_union(
    tmp_path: Path,
) -> None:
    """The roots answer waits on the tool list; a Slack chat joins in that window."""
    backend, _a, _b, fa, fb = await _two_chats(tmp_path, None)
    await backend._route_backend_line(_line({"id": "srv-roots", "method": "roots/list"}))

    def _listing() -> Any:
        return next((f for f in _frames(backend) if f.get("method") == "tools/list"), None)

    await async_wait_until(lambda: _listing() is not None)
    await backend.attach_stub("stub-s")
    await caller_scope.note_caller(backend, "stub-s", _caller("slack:C1:1.2"))
    await backend._route_backend_line(
        _line({"id": _listing()["id"], "result": {"tools": FS_TOOLS}})
    )

    def _answer() -> Any:
        return next((f for f in _frames(backend) if f.get("id") == "srv-roots"), None)

    await async_wait_until(
        lambda: _answer() is not None or caller_scope.state(backend).roots_routed
    )
    answer = _answer()
    roots = (answer or {}).get("result", {}).get("roots", [])
    assert not roots, "no chat folder goes to a server a channel chat shares"
    assert not caller_scope.state(backend).roots_served


@pytest.mark.asyncio
async def test_a_channel_chat_attaching_while_the_union_is_written_retires_the_backend(
    tmp_path: Path,
) -> None:
    backend, *_rest = await _two_chats(tmp_path, FS_TOOLS)
    held, release = asyncio.Event(), asyncio.Event()

    async def _slow_drain() -> None:
        if not held.is_set():
            held.set()
            await release.wait()

    backend.stdin.drain = _slow_drain  # type: ignore[method-assign]
    await backend._route_backend_line(_line({"id": "srv-roots", "method": "roots/list"}))
    await await_until(held, what="the roots answer is being written")
    await backend.attach_stub("stub-s")
    await caller_scope.note_caller(backend, "stub-s", _caller("slack:C1:1.2"))
    assert backend._gone_broadcast, "the backend goes before the channel chat's first frame"
    release.set()


@pytest.mark.asyncio
async def test_an_unnamed_chat_forwarding_to_a_union_server_retires_it(
    tmp_path: Path,
) -> None:
    """A warm-pool stub not claimed yet has no session key: it is not local."""
    from kiro_crew.mcp_gateway.backend import BackendGone

    backend, *_rest = await _two_chats(tmp_path, FS_TOOLS)
    assert await _served_roots(backend, "r1")
    before = len(_frames(backend))
    await backend.attach_stub("stub-w")
    call = {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}
    with pytest.raises(BackendGone) as gone:
        await backend.forward_from_stub("stub-w", call, caller=None)
    assert caller_scope.never_sent(gone.value)
    assert backend._gone_broadcast
    assert len(_frames(backend)) == before


@pytest.mark.asyncio
async def test_unconfinable_server_keeps_per_chat_routing(tmp_path: Path) -> None:
    backend, *_ = await _two_chats(tmp_path, FS_TOOLS + [SHELL_TOOL])
    await backend._route_backend_line(_line({"id": "srv-roots", "method": "roots/list"}))
    assert _roots_answers(backend) == []
    assert "cannot route without a cross-tenant leak" in (backend.dead_reason or "")


@pytest.mark.asyncio
async def test_unknown_folder_keeps_the_old_routing(tmp_path: Path) -> None:
    backend = _make_backend()
    inbox = await backend.attach_stub("stub-x")
    await caller_scope.note_caller(backend, "stub-x", _caller("dashboard:chat-without-folder"))
    await backend._route_backend_line(_line({"id": "srv-roots", "method": "roots/list"}))
    assert (await_json(inbox))["method"] == "roots/list"


def await_json(inbox: Any) -> dict:
    import json

    return json.loads(inbox.get_nowait().decode("utf-8"))


@pytest.mark.asyncio
async def test_roots_change_is_announced_when_a_new_folder_attaches(tmp_path: Path) -> None:
    backend, *_rest, a, b = await _two_chats(tmp_path, FS_TOOLS)
    await backend._route_backend_line(_line({"id": "srv-roots", "method": "roots/list"}))
    await async_wait_until(lambda: _roots_answers(backend))

    def _changes() -> int:
        return sum(
            1 for f in _frames(backend) if f.get("method") == "notifications/roots/list_changed"
        )

    c = tmp_path / "projC"
    c.mkdir()
    folder_scope.record_session_folder("dashboard:chat-c", str(c))
    await backend.attach_stub("stub-c")
    await caller_scope.note_caller(backend, "stub-c", _caller("dashboard:chat-c"))
    assert _changes() == 1
    # A folder stays served when its chat's stub leaves (a reconnect or a turn).
    await backend.detach_stub("stub-a")
    assert _changes() == 1
    # A second chat in a folder already served changes nothing.
    await backend.attach_stub("stub-b2")
    await caller_scope.note_caller(backend, "stub-b2", _caller("dashboard:chat-b"))
    assert _changes() == 1


@pytest.mark.asyncio
async def test_relative_path_reaches_the_server_as_the_callers_file(tmp_path: Path) -> None:
    backend, _a, _b, a, b = await _two_chats(tmp_path, FS_TOOLS + [GIT_TOOL])
    caller_scope.state(backend).roots_served = True
    call = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": "read_text_file", "arguments": {"path": "note.txt"}},
    }
    await backend.forward_from_stub("stub-b", call, caller=_caller("dashboard:chat-b"))
    git = {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "git_status"}}
    await backend.forward_from_stub("stub-a", git, caller=_caller("dashboard:chat-a"))

    def _sent() -> list[dict]:
        return [f for f in _frames(backend) if f.get("method") == "tools/call"]

    # These tools declare nothing, so neither call waits for the other.
    sent = _sent()
    assert len(sent) == 2
    assert sent[0]["params"]["arguments"] == {"path": os.path.join(b, "note.txt")}
    assert sent[1]["params"]["arguments"] == {"repo_path": a}


@pytest.mark.asyncio
async def test_a_call_while_the_roots_answer_is_pending_is_the_callers_file(
    tmp_path: Path,
) -> None:
    """Two chats start at once: chat B's read reaches the gateway after the server
    asked for roots, before the answer is written."""
    backend, _a, _b, _pa, b = await _two_chats(tmp_path, FS_TOOLS)
    assert caller_scope.claim_roots_request(backend, {"id": "srv-roots", "method": "roots/list"})
    assert not caller_scope.state(backend).roots_served, "the answer is still pending"
    call = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": "read_text_file", "arguments": {"path": "note.txt"}},
    }
    await backend.forward_from_stub("stub-b", call, caller=_caller("dashboard:chat-b"))
    (sent,) = [f for f in _frames(backend) if f.get("method") == "tools/call"]
    assert sent["params"]["arguments"] == {"path": os.path.join(b, "note.txt")}
    await async_wait_until(lambda: _roots_answers(backend))


@pytest.mark.asyncio
async def test_a_chat_joining_while_the_first_roots_answer_is_written_is_announced(
    tmp_path: Path,
) -> None:
    a, b = _folders(tmp_path)
    backend = _make_backend()
    await backend.attach_stub("stub-a")
    await caller_scope.note_caller(backend, "stub-a", _caller("dashboard:chat-a"))
    caller_scope.note_tools_list(
        backend, backend_pending("tools/list"), {"result": {"tools": FS_TOOLS}}
    )
    held, release = asyncio.Event(), asyncio.Event()

    async def _slow_drain() -> None:
        held.set()
        await release.wait()

    backend.stdin.drain = _slow_drain  # type: ignore[method-assign]
    await backend._route_backend_line(_line({"id": "srv-roots", "method": "roots/list"}))
    await await_until(held, what="the roots answer is being written")
    await backend.attach_stub("stub-b")
    await caller_scope.note_caller(backend, "stub-b", _caller("dashboard:chat-b"))
    release.set()

    def _changes() -> int:
        return sum(
            1 for f in _frames(backend) if f.get("method") == "notifications/roots/list_changed"
        )

    await async_wait_until(lambda: _changes() == 1)
    assert caller_scope.state(backend).roots_answer == [a, b]


@pytest.mark.asyncio
async def test_crews_own_servers_are_never_rewritten(tmp_path: Path) -> None:
    backend, _a, _b, _fa, _fb = await _two_chats(tmp_path, [GIT_TOOL])
    backend.supports_caller_identity = True
    call = {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "git_status"}}
    assert caller_scope.scope_forward(backend, "stub-a", call, _caller("dashboard:chat-a")) is call


# --- serialized calls (call_gate) ---------------------------------------------


STORE_WRITER = {
    "name": "create_entities",
    "inputSchema": {"type": "object", "properties": {"entities": {"type": "array"}}},
    "annotations": {"readOnlyHint": False},
}


def _tool(name: str, props: dict, read_only: Any) -> dict:
    tool: dict[str, Any] = {"name": name, "inputSchema": {"type": "object", "properties": props}}
    if read_only is not None:
        tool["annotations"] = {"readOnlyHint": read_only}
    return tool


def test_only_a_store_writing_server_serializes_and_only_its_writers() -> None:
    """Queue-wait report: 37 of 41 servers took turns under 'no readOnlyHint'."""
    read_graph = _tool("read_graph", {}, True)
    # server-memory: declared writers that name no target write one store.
    assert call_gate.serialized_tools([STORE_WRITER, read_graph]) == {"create_entities"}
    # server-filesystem / mcp-server-git: every writer names the file or repository.
    fs = [
        _tool("write_file", {"path": {}, "content": {}}, False),
        _tool("move_file", {"source": {}, "destination": {}}, False),
        _tool("search_files", {"path": {}, "pattern": {}}, True),
    ]
    git = [_tool("git_commit", {"repo_path": {}, "message": {}}, False)]
    assert call_gate.serialized_tools(fs) == frozenset()
    assert call_gate.serialized_tools(git) == frozenset()
    # Request/response servers that declare nothing (most internal servers).
    plain = [_tool("email_search", {"query": {}}, None), _tool("post", {"text": {}}, None)]
    assert call_gate.serialized_tools(plain) == frozenset()
    # A browser with a tab per caller: every call, readers too.
    assert call_gate.serialized_tools([read_graph, STORE_WRITER], whole_server=True) == {
        "read_graph",
        "create_entities",
    }
    assert call_gate.serialized_tools([]) == frozenset()


@pytest.mark.asyncio
async def test_a_reader_and_a_filesystem_call_do_not_wait_behind_a_slow_call(
    tmp_path: Path,
) -> None:
    """Chat B's call goes out while chat A's is still running."""
    read_graph = _tool("read_graph", {}, True)
    for tools, first, second in (
        ([STORE_WRITER, read_graph], "create_entities", "read_graph"),
        (
            [_tool("search_files", {"path": {}}, True), _tool("write_file", {"path": {}}, False)],
            "search_files",
            "write_file",
        ),
    ):
        (tmp_path / first).mkdir()
        backend, *_ = await _two_chats(tmp_path / first, tools)
        for stub, chat, name, i in (
            ("stub-a", "dashboard:chat-a", first, 1),
            ("stub-b", "dashboard:chat-b", second, 2),
        ):
            frame = {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name}}
            await backend.forward_from_stub(stub, frame, caller=_caller(chat))

        def _sent(b: Any = backend) -> set[str]:
            return {f["params"]["name"] for f in _frames(b) if f.get("method") == "tools/call"}

        # Both reach the server although nothing answered the first.
        await async_wait_until(lambda: _sent() == {first, second})


@pytest.mark.asyncio
async def test_gate_writes_one_call_at_a_time_and_drops_queued_cancels() -> None:
    written: list[str] = []

    async def write(fid: str, frame: dict) -> bool:
        written.append(fid)
        return True

    gate = call_gate.CallGate(write)
    for fid in ("f1", "f2", "f3"):
        gate.submit(fid, {"id": fid})
    await async_wait_until(lambda: written == ["f1"])
    assert gate.cancel("f2") == call_gate.DROPPED
    gate.release("f1")
    await async_wait_until(lambda: written == ["f1", "f3"])
    gate.fail_all()
    assert gate.in_flight is None and gate.queued() == []


@pytest.mark.asyncio
async def test_cancelled_in_flight_call_frees_the_slot_after_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(call_gate, "CANCEL_GRACE_SECS", 0)
    written: list[str] = []

    async def write(fid: str, frame: dict) -> bool:
        written.append(fid)
        return True

    gate = call_gate.CallGate(write)
    gate.submit("f1", {})
    gate.submit("f2", {})
    await async_wait_until(lambda: written == ["f1"])
    assert gate.cancel("f1") == call_gate.SENT
    await async_wait_until(lambda: written == ["f1", "f2"])


@pytest.mark.asyncio
async def test_parallel_writes_reach_a_stateful_server_one_by_one(tmp_path: Path) -> None:
    backend, *_ = await _two_chats(tmp_path, [STORE_WRITER])

    def _calls() -> list[dict]:
        return [f for f in _frames(backend) if f.get("method") == "tools/call"]

    for i in range(40):
        stub, chat = ("stub-a", "dashboard:chat-a") if i % 2 else ("stub-b", "dashboard:chat-b")
        frame = {
            "jsonrpc": "2.0",
            "id": i,
            "method": "tools/call",
            "params": {"name": "create_entities"},
        }
        await backend.forward_from_stub(stub, frame, caller=_caller(chat))
    for n in range(1, 41):
        # Never more than one call written ahead of the replies.
        await async_wait_until(lambda n=n: len(_calls()) == n)
        await backend._route_backend_line(_line({"id": _calls()[-1]["id"], "result": {}}))
    assert len(_calls()) == 40


@pytest.mark.asyncio
async def test_a_queued_call_cancelled_never_reaches_the_server(tmp_path: Path) -> None:
    backend, inbox_a, *_ = await _two_chats(tmp_path, [{**STORE_WRITER, "name": "w"}])
    for i in (1, 2):
        frame = {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": "w"}}
        await backend.forward_from_stub("stub-a", frame, caller=_caller("dashboard:chat-a"))
    cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}}
    await backend.forward_from_stub("stub-a", cancel, caller=_caller("dashboard:chat-a"))

    def _calls() -> list[dict]:
        return [f for f in _frames(backend) if f.get("method") == "tools/call"]

    await async_wait_until(lambda: len(_calls()) == 1)
    await backend._route_backend_line(_line({"id": _calls()[0]["id"], "result": {}}))
    assert (
        caller_scope.state(backend).call_gate.in_flight is None
        and caller_scope.state(backend).call_gate.queued() == []
    )
    assert len(_calls()) == 1
    assert not any(f.get("method") == "notifications/cancelled" for f in _frames(backend))


# --- a browser tab per caller (tab_tenancy) ---------------------------------

PW_TABS = {
    "name": "browser_tabs",
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "new", "close", "select"]},
            "index": {"type": "number"},
            "isolatedContext": {"type": "string"},
        },
    },
}


def test_tabs_tool_found_by_shape() -> None:
    close = {"name": "browser_close", "inputSchema": {"type": "object", "properties": {}}}
    assert tab_tenancy.find_tabs_tool([PW_TABS, close]) == ("browser_tabs", "browser_close")
    plain = {"name": "tabs", "inputSchema": {"properties": {"action": {"enum": ["list"]}}}}
    assert tab_tenancy.find_tabs_tool([plain]) is None


def test_object_valued_schema_members_are_a_non_tab_shape_not_an_error() -> None:
    odd = {
        "name": {"not": "a string"},
        "inputSchema": {
            "properties": {
                "action": {"enum": [{"op": "include"}, ["list"]]},
                "index": {},
                "isolatedContext": {},
            }
        },
    }
    renamed = {**odd, "name": "odd_tabs"}
    assert tab_tenancy.find_tabs_tool([odd, renamed]) is None
    assert tab_tenancy.single_page_browser([odd, renamed]) is False


def test_listing_is_limited_to_the_callers_tabs() -> None:
    a, b = tab_tenancy.context_name("dashboard:chat-a"), tab_tenancy.context_name(
        "dashboard:chat-b"
    )
    text = (
        "### Open tabs\n"
        f"- 0: [A](http://x/a) [isolatedContext: {a}]\n"
        f"- 1: (current) [B](http://x/b) [isolatedContext: {b}]\n"
        f"- 2: [A2](http://x/a2) [isolatedContext: {a}]\n"
        "### Page"
    )
    tabs = tab_tenancy.parse_tabs(text)
    assert [t.index for t in tab_tenancy.own_tabs(tabs, a)] == [0, 2]
    assert tab_tenancy.map_index(1, tab_tenancy.own_tabs(tabs, a)) == 2
    assert tab_tenancy.map_index(2, tab_tenancy.own_tabs(tabs, a)) is None
    assert tab_tenancy.rewrite_listing(text, b) == (
        f"### Open tabs\n- 0: (current) [B](http://x/b) [isolatedContext: {b}]\n### Page"
    )
    reply = {"result": {"content": [{"type": "text", "text": text}]}}
    out = tab_tenancy.rewrite_reply(reply, a)["result"]["content"][0]["text"]
    assert "[B]" not in out and "- 1: [A2]" in out


@pytest.mark.asyncio
async def test_a_departed_chats_call_in_flight_frees_the_slot_after_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(call_gate, "CANCEL_GRACE_SECS", 0)
    backend, *_ = await _two_chats(tmp_path, [{**STORE_WRITER, "name": "w"}])

    def _calls() -> list[dict]:
        return [f for f in _frames(backend) if f.get("method") == "tools/call"]

    for stub, chat, i in (("stub-a", "dashboard:chat-a", 1), ("stub-b", "dashboard:chat-b", 2)):
        frame = {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": "w"}}
        await backend.forward_from_stub(stub, frame, caller=_caller(chat))
    await async_wait_until(lambda: len(_calls()) == 1)
    # The chat stops: its call is cancelled upstream and gets no reply.
    await backend.cancel_in_flight_for_stub("stub-a")
    await backend.detach_stub("stub-a")
    await async_wait_until(lambda: len(_calls()) == 2)


@pytest.mark.asyncio
async def test_a_call_cancelled_before_it_is_written_is_never_sent(tmp_path: Path) -> None:
    backend, *_ = await _two_chats(tmp_path, [{**STORE_WRITER, "name": "w"}])
    gate = caller_scope.state(backend).call_gate
    frame = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "w"}}
    await backend.forward_from_stub("stub-a", frame, caller=_caller("dashboard:chat-a"))
    fid = gate.in_flight
    backend._pending_requests[fid].cancelled = True
    await async_wait_until(lambda: gate.in_flight is None)
    assert not [f for f in _frames(backend) if f.get("method") == "tools/call"]


@pytest.mark.asyncio
async def test_a_stop_while_the_tab_is_prepared_never_sends_the_call(tmp_path: Path) -> None:
    """Stop/disconnect (``cancel_in_flight_for_stub`` + detach) during preparation."""
    backend, *_ = await _two_chats(tmp_path, [PW_TABS, {"name": "browser_navigate"}])
    gate = caller_scope.state(backend).call_gate
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "browser_navigate", "arguments": {"url": "http://x"}},
    }
    await backend.forward_from_stub("stub-a", frame, caller=_caller("dashboard:chat-a"))

    def _listing() -> Any:
        return next(
            (f for f in _frames(backend) if f.get("params", {}).get("name") == "browser_tabs"),
            None,
        )

    # The gateway is preparing chat A's tab (its tab listing is out, unanswered).
    await async_wait_until(lambda: _listing() is not None)
    await backend.cancel_in_flight_for_stub("stub-a")
    await backend.detach_stub("stub-a")
    await backend._route_backend_line(
        _line({"id": _listing()["id"], "result": {"content": [{"type": "text", "text": ""}]}})
    )
    await async_wait_until(lambda: gate.in_flight is None)
    names = [f.get("params", {}).get("name") for f in _frames(backend)]
    assert "browser_navigate" not in names
    assert not any(f.get("method") == "notifications/cancelled" for f in _frames(backend))


@pytest.mark.asyncio
async def test_a_failed_internal_tab_call_fails_the_callers_call(tmp_path: Path) -> None:
    backend, inbox_a, *_ = await _two_chats(tmp_path, [PW_TABS, {"name": "browser_close"}])
    frame = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "browser_close"}}
    await backend.forward_from_stub("stub-a", frame, caller=_caller("dashboard:chat-a"))

    def _listing() -> Any:
        return next(
            (f for f in _frames(backend) if f.get("params", {}).get("name") == "browser_tabs"),
            None,
        )

    await async_wait_until(lambda: _listing() is not None)
    failed = {"content": [{"type": "text", "text": "boom"}], "isError": True}
    await backend._route_backend_line(_line({"id": _listing()["id"], "result": failed}))
    await async_wait_until(lambda: not inbox_a.empty())
    reply = await_json(inbox_a)
    assert reply["id"] == 7 and reply["result"]["isError"] is True


@pytest.mark.asyncio
async def test_a_cancel_once_the_frame_is_going_out_is_sent_upstream() -> None:
    async def write(fid: str, frame: dict) -> bool:
        return True

    gate = call_gate.CallGate(write)
    gate.submit("f1", {})
    gate.mark_sending("f1")
    assert gate.cancel("f1") == call_gate.SENT
    gate.fail_all()


def test_a_single_page_browser_is_recorded_so_the_next_chat_gets_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hazards, "_sink", hazards.HazardLedger(tmp_path / "h.json"))
    navigate = {
        "name": "puppeteer_navigate",
        "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}},
    }
    assert tab_tenancy.single_page_browser([navigate]) is True
    assert tab_tenancy.single_page_browser([navigate, PW_TABS]) is False
    backend = _make_backend()
    caller_scope.note_tools_list(
        backend, backend_pending("tools/list"), {"result": {"tools": [navigate]}}
    )
    key = backend.pool_key
    identity = hazards.launch_identity(
        key.command_args_hash, key.effective_env_hash, key.binary_version
    )
    assert hazards.retreat_codes(key.server_name, identity) == (hazards.HAZARD_SINGLE_PAGE_BROWSER,)


@pytest.mark.asyncio
async def test_the_gateways_own_tab_call_carries_a_start_time(tmp_path: Path) -> None:
    backend, *_ = await _two_chats(tmp_path, [PW_TABS, {"name": "browser_navigate"}])
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "browser_navigate"},
    }
    await backend.forward_from_stub("stub-a", frame, caller=_caller("dashboard:chat-a"))

    def _internal() -> Any:
        return next(
            (p for p in backend._pending_requests.values() if p.original_id is None),
            None,
        )

    await async_wait_until(lambda: _internal() is not None)
    assert _internal().t_start_ms > 0
    gate = caller_scope.state(backend).call_gate
    task = gate._task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_a_repository_name_is_never_taken_for_a_folder(tmp_path: Path) -> None:
    a, _b = _folders(tmp_path)
    github = {
        "name": "get_file_contents",
        "inputSchema": {
            "type": "object",
            "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}},
            "required": ["owner", "repo"],
        },
    }
    scope = folder_scope.scan_tool(github)
    args = {"owner": "octo-org", "repo": "hello-world"}
    assert folder_scope.scope_arguments(dict(args), scope, a, folder_user=False) == args
    assert folder_scope.scope_arguments({"owner": "x"}, scope, a, folder_user=False) == {
        "owner": "x"
    }


def test_page_content_shaped_like_a_tab_line_is_kept() -> None:
    a = tab_tenancy.context_name("dashboard:chat-a")
    text = "### Result\n- 0: [Other](http://x/o)\n### Open tabs\n- 0: [Other](http://x/o)"
    out = tab_tenancy.rewrite_listing(text, a)
    assert out == "### Result\n- 0: [Other](http://x/o)\n### Open tabs"
    assert tab_tenancy.rewrite_listing(text, a, whole=True) == "### Result\n### Open tabs"


@pytest.mark.asyncio
async def test_a_shared_single_page_browser_is_recycled_at_once(tmp_path: Path) -> None:
    navigate = {
        "name": "puppeteer_navigate",
        "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}},
    }
    backend, *_ = await _two_chats(tmp_path, [navigate])
    assert backend.dead_reason is not None and "single-page browser" in backend.dead_reason
    await asyncio.gather(*list(caller_scope.state(backend).scope_tasks))


@pytest.mark.asyncio
async def test_the_upstream_initialize_offers_roots_on_first_start_and_respawn() -> None:
    """A filesystem server given no folder needs roots; a harness may not offer them."""
    backend = _make_backend()
    inbox = await backend.attach_stub("stub-a")
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"capabilities": {}}}
    await backend.forward_from_stub("stub-a", init, caller=_caller("dashboard:chat-a"))
    sent = next(f for f in _frames(backend) if f.get("method") == "initialize")
    assert sent["params"]["capabilities"]["roots"] == {"listChanged": True}
    assert init["params"]["capabilities"] == {}
    assert caller_scope.state(backend).harness_offers_roots is False
    assert inbox.empty()
    # A respawned backend is primed from the captured frame: the same offer.
    replacement = _make_backend()
    task = asyncio.ensure_future(replacement.prime_initialize(dict(init)))
    await async_wait_until(
        lambda: any(f.get("method") == "initialize" for f in _frames(replacement))
    )
    primed = next(f for f in _frames(replacement) if f.get("method") == "initialize")
    assert primed["params"]["capabilities"]["roots"] == {"listChanged": True}
    task.cancel()
    with pytest.raises((asyncio.CancelledError, Exception)):
        await task


def test_crews_own_servers_get_no_roots_offer() -> None:
    backend = _make_backend()
    backend.supports_caller_identity = True
    init = {"jsonrpc": "2.0", "method": "initialize", "params": {"capabilities": {}}}
    assert caller_scope.offer_roots(backend, init) is init


@pytest.mark.asyncio
async def test_a_declined_roots_request_the_harness_never_offered_is_answered_here(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Its own ledger: the process sink is shared with every other test.
    monkeypatch.setattr(hazards, "_sink", hazards.HazardLedger(tmp_path / "h.json"))
    backend = _make_backend()
    inbox = await backend.attach_stub("stub-a")
    caller_scope.state(backend).harness_offers_roots = False
    # A local chat whose folder has not arrived yet, so the union is unknown.
    await caller_scope.note_caller(backend, "stub-a", _caller("dashboard:chat-a"))
    await backend._route_backend_line(_line({"id": "srv-roots", "method": "roots/list"}))

    def _answer() -> Any:
        return next((f for f in _frames(backend) if f.get("id") == "srv-roots"), None)

    await async_wait_until(lambda: _answer() is not None)
    assert _answer()["error"]["code"] == -32601
    assert inbox.empty()
    key = backend.pool_key
    identity = hazards.launch_identity(
        key.command_args_hash, key.effective_env_hash, key.binary_version
    )
    assert hazards.observed_codes(key.server_name, identity) == ()
    # Once the folder is known and the tools are scoped, the server is told to ask again.
    folder_scope.record_session_folder("dashboard:chat-a", str(tmp_path))
    caller_scope.note_tools_list(
        backend, backend_pending("tools/list"), {"result": {"tools": FS_TOOLS}}
    )
    await caller_scope.note_caller(backend, "stub-a", _caller("dashboard:chat-a"))
    assert any(f.get("method") == "notifications/roots/list_changed" for f in _frames(backend))


@pytest.mark.asyncio
async def test_a_pending_decline_is_rechecked_when_tools_or_folder_arrive_later(
    tmp_path: Path,
) -> None:
    backend = _make_backend()
    await backend.attach_stub("stub-a")
    caller_scope.state(backend).harness_offers_roots = False
    # Attributed before the folder is known and before the tools are listed.
    await caller_scope.note_caller(backend, "stub-a", _caller("dashboard:chat-a"))
    await backend._route_backend_line(_line({"id": "srv-roots", "method": "roots/list"}))
    await async_wait_until(lambda: caller_scope.state(backend).roots_pending)

    def _changes() -> int:
        return sum(
            1 for f in _frames(backend) if f.get("method") == "notifications/roots/list_changed"
        )

    # The claim's folder arrives for the same caller, then the tools.
    folder_scope.record_session_folder("dashboard:chat-a", str(tmp_path))
    await caller_scope.note_caller(backend, "stub-a", _caller("dashboard:chat-a"))
    assert _changes() == 0  # tools not known yet: nothing servable
    caller_scope.note_tools_list(
        backend, backend_pending("tools/list"), {"result": {"tools": FS_TOOLS}}
    )
    await async_wait_until(lambda: _changes() == 1)
    await caller_scope.note_caller(backend, "stub-a", _caller("dashboard:chat-a"))
    assert _changes() == 1


@pytest.mark.asyncio
async def test_server_discover_is_answered_here_so_the_harness_uses_initialize() -> None:
    """On the handshake-free revision a server has no roots/list, so a folder server gets no folder."""
    backend = _make_backend()
    inbox = await backend.attach_stub("stub-a")
    discover = {"jsonrpc": "2.0", "id": 4, "method": "server/discover", "params": {}}
    await backend.forward_from_stub("stub-a", discover, caller=_caller("dashboard:chat-a"))
    assert await_json(inbox) == {
        "jsonrpc": "2.0",
        "id": 4,
        "error": {"code": -32601, "message": "Method not found"},
    }
    assert not [f for f in _frames(backend) if f.get("method") == "server/discover"]
    own = _make_backend()
    own.supports_caller_identity = True
    await own.attach_stub("stub-a")
    await own.forward_from_stub("stub-a", dict(discover), caller=_caller("dashboard:chat-a"))
    assert [f for f in _frames(own) if f.get("method") == "server/discover"]


def test_launch_folders_are_the_existing_directory_arguments(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    args = ["--read-only", str(conf), "rel", "file.txt", str(tmp_path / "missing")]
    (tmp_path / "rel").mkdir()
    assert caller_scope.launch_folders(args, str(tmp_path)) == [
        str(conf),
        str(tmp_path / "rel"),
    ]


@pytest.mark.asyncio
async def test_a_configured_directory_stays_in_the_roots_answer(tmp_path: Path) -> None:
    """Roots replace the directories a filesystem server was started with."""
    backend, *_rest, a, b = await _two_chats(tmp_path, FS_TOOLS)
    conf = tmp_path / "configured"
    conf.mkdir()
    caller_scope.note_launch(backend, caller_scope.launch_folders([str(conf)], str(tmp_path)))
    await backend._route_backend_line(_line({"id": "srv-roots", "method": "roots/list"}))
    await async_wait_until(lambda: _roots_answers(backend))
    roots = [r["uri"] for r in _roots_answers(backend)[0]["result"]["roots"]]
    assert roots == [conf.as_uri(), Path(a).as_uri(), Path(b).as_uri()]


async def _served_roots(backend: Any, rid: str) -> list[str]:
    """Ask as the server does after a list_changed, and read the gateway's answer."""
    await backend._route_backend_line(_line({"id": rid, "method": "roots/list"}))

    def _answer() -> Any:
        return next((f for f in _frames(backend) if f.get("id") == rid and "result" in f), None)

    await async_wait_until(lambda: _answer() is not None)
    return [r["uri"] for r in _answer()["result"]["roots"]]


@pytest.mark.asyncio
async def test_t2_an_open_chat_keeps_its_folder_while_its_stub_reconnects(tmp_path: Path) -> None:
    """macOS T2: chat 1's stub detaches mid-session; chat 2 must still reach P1."""
    backend, _ia, _ib, a, b = await _two_chats(tmp_path, FS_TOOLS)
    assert await _served_roots(backend, "r1") == [Path(a).as_uri(), Path(b).as_uri()]
    # Chat 1 is still open; its stub connection cycles (a turn, a reconnect).
    await backend.detach_stub("stub-a")
    assert Path(a).as_uri() in await _served_roots(backend, "r2")


@pytest.mark.asyncio
async def test_t6_closing_one_of_two_chats_in_a_folder_keeps_that_folder(tmp_path: Path) -> None:
    """macOS T6: chats 1 and 3 in P1, chat 2 in P2; chat 1 closes, then chat 3's stub cycles."""
    backend, _ia, _ib, a, b = await _two_chats(tmp_path, FS_TOOLS)
    folder_scope.record_session_folder("dashboard:chat-c", a)
    await backend.attach_stub("stub-c")
    await caller_scope.note_caller(backend, "stub-c", _caller("dashboard:chat-c"))
    await backend.detach_stub("stub-a")
    await backend.detach_stub("stub-c")
    roots = await _served_roots(backend, "r3")
    assert Path(a).as_uri() in roots and Path(b).as_uri() in roots


@pytest.mark.asyncio
async def test_a_rekeyed_slot_keeps_its_cancel_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancelled call re-sent after a rehandshake still frees the slot on time."""
    monkeypatch.setattr(call_gate, "CANCEL_GRACE_SECS", 0)
    written: list[str] = []

    async def write(fid: str, frame: dict) -> bool:
        written.append(fid)
        return True

    gate = call_gate.CallGate(write)
    gate.submit("f1", {})
    gate.submit("f3", {})
    await async_wait_until(lambda: written == ["f1"])
    gate.mark_sending("f1")
    assert gate.cancel("f1") == call_gate.SENT
    gate.rekey("f1", "f2")
    await async_wait_until(lambda: written == ["f1", "f3"])


@pytest.mark.asyncio
async def test_a_failed_gated_write_keeps_the_queue_for_the_replacement() -> None:
    written: list[str] = []

    async def write(fid: str, frame: dict) -> bool:
        written.append(fid)
        gate.mark_sending(fid)  # as the real writer does, once it holds the lock
        raise BrokenPipeError("closed")

    gate = call_gate.CallGate(write)
    gate.submit("f1", {}, origin={"id": 1})
    gate.submit("f2", {}, origin={"id": 2})
    await async_wait_until(lambda: gate._task is not None and gate._task.done())
    assert written == ["f1"], "nothing more is pumped into the dead pipe"
    assert gate.fail_all() == [("f2", {"id": 2})]


@pytest.mark.asyncio
async def test_a_cancel_while_the_call_waits_for_the_write_lock_drops_it(
    tmp_path: Path,
) -> None:
    """Another write holds the backend's stdin; the queued call is cancelled meanwhile."""
    writer_tool = _tool("create_entities", {"entities": {"type": "array"}}, False)
    backend, *_ = await _two_chats(tmp_path, [writer_tool])
    lock = getattr(backend.stdin, "_mc_write_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        backend.stdin._mc_write_lock = lock  # type: ignore[attr-defined]
    await lock.acquire()
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "create_entities", "arguments": {"entities": []}},
    }
    await backend.forward_from_stub("stub-a", frame, caller=_caller("dashboard:chat-a"))
    gate = caller_scope.state(backend).call_gate
    # The call has reached the write and waits for the lock.
    await async_wait_until(lambda: bool(getattr(lock, "_waiters", None)))
    cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
    await backend.forward_from_stub("stub-a", cancel, caller=_caller("dashboard:chat-a"))
    lock.release()
    await async_wait_until(lambda: gate.in_flight is None)
    assert not [
        f for f in _frames(backend) if f.get("method") in ("tools/call", "notifications/cancelled")
    ]


@pytest.mark.asyncio
async def test_a_cancel_during_the_rehandshake_write_reaches_the_moved_slot(
    tmp_path: Path,
) -> None:
    """The slot moves to the retry before its write, so a cancel then arms the grace."""
    from kiro_crew.mcp_gateway.backend_handshake import (
        _LOST_SESSION_ERROR_CODE,
        _LOST_SESSION_ERROR_MESSAGE,
    )

    writer_tool = _tool("create_entities", {"entities": {"type": "array"}}, False)
    backend, *_ = await _two_chats(tmp_path, [writer_tool])
    backend._upstream_init_frame = {"jsonrpc": "2.0", "method": "initialize", "params": {}}
    backend._init_state = "ready"
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "create_entities", "arguments": {"entities": []}},
    }
    await backend.forward_from_stub("stub-a", frame, caller=_caller("dashboard:chat-a"))

    def _calls() -> list[dict]:
        return [f for f in _frames(backend) if f.get("method") == "tools/call"]

    await async_wait_until(lambda: len(_calls()) == 1)
    gate = caller_scope.state(backend).call_gate
    held, release = asyncio.Event(), asyncio.Event()

    async def _slow_drain() -> None:
        # Only the rehandshake write is held; the cancel's own write goes out.
        if not held.is_set():
            held.set()
            await release.wait()

    backend.stdin.drain = _slow_drain  # type: ignore[method-assign]
    lost = {"code": _LOST_SESSION_ERROR_CODE, "message": _LOST_SESSION_ERROR_MESSAGE}
    routed = asyncio.create_task(
        backend._route_backend_line(_line({"id": _calls()[0]["id"], "error": lost}))
    )
    await await_until(held, what="the rehandshake write is draining")
    retry_fid = _calls()[-1]["id"]
    assert gate.in_flight == retry_fid, "the slot is the retry's before the write ends"
    cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
    # Its own write waits behind the held one; the slot is decided before that.
    cancelling = asyncio.create_task(
        backend.forward_from_stub("stub-a", cancel, caller=_caller("dashboard:chat-a"))
    )
    await async_wait_until(lambda: gate._grace is not None)
    release.set()
    await routed
    await cancelling
    assert gate.in_flight == retry_fid
    gate._grace.cancel()


@pytest.mark.asyncio
async def test_a_lost_session_during_tab_preparation_rehandshakes_once(tmp_path: Path) -> None:
    from kiro_crew.mcp_gateway.backend_handshake import (
        _LOST_SESSION_ERROR_CODE,
        _LOST_SESSION_ERROR_MESSAGE,
    )

    backend, *_ = await _two_chats(tmp_path, [PW_TABS, {"name": "browser_navigate"}])
    backend._upstream_init_frame = {"jsonrpc": "2.0", "method": "initialize", "params": {}}
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "browser_navigate"},
    }
    await backend.forward_from_stub("stub-a", frame, caller=_caller("dashboard:chat-a"))

    def _tab_calls() -> list[dict]:
        return [f for f in _frames(backend) if f.get("params", {}).get("name") == "browser_tabs"]

    await async_wait_until(lambda: len(_tab_calls()) == 1)
    lost = {"code": _LOST_SESSION_ERROR_CODE, "message": _LOST_SESSION_ERROR_MESSAGE}
    await backend._route_backend_line(_line({"id": _tab_calls()[0]["id"], "error": lost}))
    await async_wait_until(lambda: len(_tab_calls()) == 2)
    methods = [f.get("method") for f in _frames(backend)]
    assert "initialize" in methods and "notifications/initialized" in methods
    task = caller_scope.state(backend).call_gate._task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_the_first_chat_keeps_the_browsers_own_profile_and_a_second_gets_its_own(
    tmp_path: Path,
) -> None:
    """The default context holds the user's persistent profile (logins); only a
    later chat is moved into an isolated context."""
    backend, *_ = await _two_chats(tmp_path, [PW_TABS, {"name": "browser_navigate"}])

    def _tab_calls() -> list[dict]:
        return [f for f in _frames(backend) if f.get("params", {}).get("name") == "browser_tabs"]

    async def _answer(text: str) -> None:
        await backend._route_backend_line(
            _line(
                {
                    "id": _tab_calls()[-1]["id"],
                    "result": {"content": [{"type": "text", "text": text}]},
                }
            )
        )

    navigate = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "browser_navigate"}}
    await backend.forward_from_stub(
        "stub-a", {**navigate, "id": 1}, caller=_caller("dashboard:chat-a")
    )
    await async_wait_until(lambda: len(_tab_calls()) == 1)
    await _answer("### Open tabs\nNo open tabs.")
    await async_wait_until(lambda: len(_tab_calls()) == 2)
    assert _tab_calls()[1]["params"]["arguments"] == {"action": "new"}, "the default context"
    await _answer("### Result\nopened")

    def _navigates() -> list[dict]:
        return [
            f for f in _frames(backend) if f.get("params", {}).get("name") == "browser_navigate"
        ]

    await async_wait_until(lambda: len(_navigates()) == 1)
    nav_a = _navigates()[0]
    await backend._route_backend_line(_line({"id": nav_a["id"], "result": {"content": []}}))

    await backend.forward_from_stub(
        "stub-b", {**navigate, "id": 2}, caller=_caller("dashboard:chat-b")
    )
    await async_wait_until(lambda: len(_tab_calls()) == 3)
    await _answer("### Open tabs\n- 0: (current) [A](https://a.example/)")
    await async_wait_until(lambda: len(_tab_calls()) == 4)
    assert _tab_calls()[3]["params"]["arguments"] == {
        "action": "new",
        "isolatedContext": tab_tenancy.context_name("dashboard:chat-b"),
    }
    listing = "### Open tabs\n- 0: [A](https://a.example/)\n- 1: (current) [B](https://b.example/) [isolatedContext: x]"
    reply = {"result": {"content": [{"type": "text", "text": listing}]}}
    pending_a = backend_pending("tools/call")
    pending_a.session_key, pending_a.tool_name = "dashboard:chat-a", "browser_tabs"
    shown = tab_tenancy.result_text(caller_scope.scope_reply(backend, pending_a, reply))
    assert "a.example" in shown and "b.example" not in shown, "chat A sees only its own tabs"
    task = caller_scope.state(backend).call_gate._task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class _SelRecorder:
    events: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        type(self).events.append(kw)


def _governed(monkeypatch: pytest.MonkeyPatch, denied: set[str]) -> list[tuple[str, str]]:
    """Governance that denies the ``@server/tool`` refs in *denied*; records profile lookups."""
    import kiro_crew.platform.governance as gov
    import kiro_crew.platform.governance_profiles as profiles
    import kiro_crew.sel as sel

    lookups: list[tuple[str, str]] = []
    surface = object()

    def _resolve(_ceiling: Any, _profile: Any, scope: str, ref: str) -> Any:
        assert scope == "mcp"
        return SimpleNamespace(permitted=ref not in denied, reason=f"{ref} is denied")

    def _scope(session_key: str, *, agent: str = "", **_kw: Any) -> Any:
        lookups.append((session_key, agent))
        return surface

    monkeypatch.setattr(gov, "load_security_policy", lambda *a, **k: object())
    monkeypatch.setattr(gov, "assert_policy_signature_satisfied", lambda _c: None)
    monkeypatch.setattr(gov, "resolve", _resolve)
    monkeypatch.setattr(profiles, "resolve_active_scope", _scope)
    monkeypatch.setattr(profiles, "resolve_task_bound_profile", lambda _a: None)
    _SelRecorder.events = []
    monkeypatch.setattr(sel, "SecurityEventLog", _SelRecorder)
    return lookups


@pytest.mark.asyncio
async def test_a_denied_tabs_tool_fails_the_callers_browser_call_closed_and_audited(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ceiling allows browser_snapshot but denies browser_tabs: the gateway's
    own tab call for the snapshot is refused, and nothing reaches the server."""
    server = _make_backend().pool_key.server_name
    _governed(monkeypatch, {f"@{server}/browser_tabs"})
    backend, inbox_a, *_ = await _two_chats(tmp_path, [PW_TABS, {"name": "browser_snapshot"}])
    frame = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "browser_snapshot"},
    }
    await backend.forward_from_stub("stub-a", frame, caller=_caller("dashboard:chat-a"))

    await async_wait_until(lambda: not inbox_a.empty())
    reply = await_json(inbox_a)
    assert reply["id"] == 7 and reply["result"]["isError"] is True
    assert f"@{server}/browser_tabs is denied" in reply["result"]["content"][0]["text"]
    assert not [f for f in _frames(backend) if f.get("method") == "tools/call"]
    (event,) = _SelRecorder.events
    assert event["operation"] == "mcp-gateway.tab-call" and event["outcome"] == "denied"
    assert event["caller"] == "dashboard:chat-a"


@pytest.mark.asyncio
async def test_a_private_browser_backend_gets_no_tab_tenancy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sharing off: the browser call reaches the server as sent, with no tab calls."""
    server = _make_backend().pool_key.server_name
    _governed(monkeypatch, {f"@{server}/browser_tabs"})
    backend = _make_backend()
    backend.exclusive_token = "private-1"
    await backend.attach_stub("stub-a")
    await caller_scope.note_caller(backend, "stub-a", _caller("dashboard:chat-a"))
    caller_scope.note_tools_list(
        backend,
        backend_pending("tools/list"),
        {"result": {"tools": [PW_TABS, {"name": "browser_snapshot"}]}},
    )
    assert caller_scope.state(backend).tab_tools is None
    frame = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "browser_snapshot"},
    }
    await backend.forward_from_stub("stub-a", frame, caller=_caller("dashboard:chat-a"))
    sent = [f for f in _frames(backend) if f.get("method") == "tools/call"]
    assert [f["params"]["name"] for f in sent] == ["browser_snapshot"]
    assert _SelRecorder.events == []


@pytest.mark.asyncio
async def test_a_stub_with_no_agent_is_governed_by_its_surface_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lookups = _governed(monkeypatch, set())
    backend, *_ = await _two_chats(tmp_path, [PW_TABS, {"name": "browser_snapshot"}])
    assert backend.agent_for_stub("stub-a") == ""
    frame = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "browser_snapshot"},
    }
    await backend.forward_from_stub("stub-a", frame, caller=_caller("dashboard:chat-a"))

    def _tab_calls() -> list[dict]:
        return [f for f in _frames(backend) if f.get("params", {}).get("name") == "browser_tabs"]

    await async_wait_until(lambda: len(_tab_calls()) == 1)
    assert lookups == [("dashboard:chat-a", "")], "the check ran, on the surface profile"
    assert [e["outcome"] for e in _SelRecorder.events] == ["allowed"]
    task = caller_scope.state(backend).call_gate._task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_an_app_callback_with_no_agent_is_still_refused() -> None:
    """The tab path's surface fallback does not reach app callbacks."""
    from kiro_crew.mcp_gateway import app_call

    assert app_call._agent_for_call({"agent": ""}) is None
    assert app_call._governance_denial("srv", "t", "k", agent=None) == (
        "cannot resolve the calling session's agent"
    )


@pytest.mark.asyncio
async def test_a_new_tab_opens_in_the_callers_own_context_whatever_it_names(
    tmp_path: Path,
) -> None:
    backend, *_ = await _two_chats(tmp_path, [PW_TABS, {"name": "browser_navigate"}])

    def _tab_calls() -> list[dict]:
        return [f for f in _frames(backend) if f.get("params", {}).get("name") == "browser_tabs"]

    new = {"action": "new", "isolatedContext": "work"}
    for n, (stub, key) in enumerate(
        (("stub-a", "dashboard:chat-a"), ("stub-b", "dashboard:chat-b"))
    ):
        frame = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "browser_tabs", "arguments": dict(new)},
        }
        await backend.forward_from_stub(stub, frame, caller=_caller(key))
        await async_wait_until(lambda: len(_tab_calls()) == 2 * n + 1)
        listing = {"content": [{"type": "text", "text": "### Open tabs\n- 0: (current) [A](x)"}]}
        await backend._route_backend_line(_line({"id": _tab_calls()[-1]["id"], "result": listing}))
        await async_wait_until(lambda: len(_tab_calls()) == 2 * n + 2)
        await backend._route_backend_line(
            _line({"id": _tab_calls()[-1]["id"], "result": {"content": []}})
        )
    first, second = _tab_calls()[1], _tab_calls()[3]
    assert first["params"]["arguments"] == {"action": "new"}, "the first chat's default context"
    assert second["params"]["arguments"] == {
        "action": "new",
        "isolatedContext": tab_tenancy.context_name("dashboard:chat-b"),
    }


@pytest.mark.asyncio
async def test_a_replacement_answers_roots_asked_while_it_is_primed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The respawn primes the new process before the stub attaches; roots are asked then."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.mcp_gateway import gatewayd as gw
    from kiro_crew.mcp_gateway.pool import BackendPool

    folder_scope.record_session_folder("dashboard:chat-a", str(tmp_path))
    old = _make_backend()
    await old.attach_stub("stub-a")
    fresh = _make_backend()
    caller_scope.note_tools_list(
        fresh, backend_pending("tools/list"), {"result": {"tools": FS_TOOLS}}
    )
    asked: list[dict] = []

    async def _prime(_init: Any, timeout: float) -> None:
        # The server asks for its roots during the replayed handshake.
        caller_scope.state(fresh).harness_offers_roots = False
        await fresh._route_backend_line(_line({"id": "srv-roots", "method": "roots/list"}))
        await async_wait_until(lambda: _roots_answers(fresh) or _declined())
        asked.extend(_roots_answers(fresh))

    def _declined() -> bool:
        return any(f.get("id") == "srv-roots" and "error" in f for f in _frames(fresh))

    fresh.prime_initialize = _prime  # type: ignore[method-assign]
    monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

    out = await gw._respawn_backend_for_stub(
        BackendPool(max_backends=2),
        fresh.pool_key,
        lambda k: None,
        "stub-a",
        MagicMock(),
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
        old,
        None,
        None,
        caller=_caller("dashboard:chat-a"),
    )

    assert out is not None
    out[2].cancel()
    assert [r["uri"] for r in asked[0]["result"]["roots"]] == [tmp_path.as_uri()]


@pytest.mark.asyncio
async def test_closing_an_earlier_tab_keeps_the_saved_tab_on_its_page() -> None:
    """Chat A's saved tab is its second one; A closes its first while B holds the page."""
    from kiro_crew.mcp_gateway.backend import _PendingRequest

    backend = _make_backend()
    st = caller_scope.state(backend)
    st.tab_tools = ("browser_tabs", "browser_close")
    ctx = tab_tenancy.context_name("dashboard:chat-a")
    ok = {"result": {"content": [{"type": "text", "text": "### Result\nclosed"}]}}

    def _close(index: int, reply: dict) -> None:
        pending = _PendingRequest(
            stub_uuid="stub-a",
            original_id=1,
            method="tools/call",
            session_key="dashboard:chat-a",
            tool_name="browser_tabs",
            tool_arguments={"action": "close", "index": index},
        )
        caller_scope.scope_reply(backend, pending, reply)

    st.tab_active[ctx] = 1
    _close(0, ok)
    assert st.tab_active[ctx] == 0, "the same page, now first"
    _close(0, {"result": {"content": [], "isError": True}})
    assert st.tab_active[ctx] == 0, "a failed close moves nothing"
    st.tab_active[ctx] = 0
    _close(1, ok)
    assert st.tab_active[ctx] == 0, "a later tab closing leaves it"


# --- a call that reaches a dead backend runs on the replacement ---------------


@pytest.mark.asyncio
@pytest.mark.xdist_group("mcp_gateway")
@pytest.mark.skipif(sys.platform == "win32", reason="the real-daemon helpers dial a unix socket")
async def test_a_call_sent_after_its_backend_died_runs_on_the_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, short_sock_dir: Path
) -> None:
    """A real daemon and server: the server is killed, then a chat calls a tool.

    The call never reached the dead process, so the respawn runs it once on the
    replacement and the chat gets its result instead of a retry error.
    """
    from fake_pool_mcp_server import recorded
    from mcp_resource_subscription_helpers import (
        INIT_PARAMS,
        RawConnection,
        register_payload_like_the_stub,
        running_daemon,
    )

    from kiro_crew.mcp_gateway.resource_holder import HolderSpec, holder_register_frame

    # gatewayd installs the process-wide hazard sink; restored after the test.
    monkeypatch.setattr(hazards, "_sink", None)
    sock, work = short_sock_dir / "gw.sock", tmp_path / "ws"
    work.mkdir()
    launches = tmp_path / "launches.txt"
    async with running_daemon(
        monkeypatch, sock, work, launches, tmp_path / "subs.txt", tmp_path / "callers.txt"
    ) as daemon:
        spec = HolderSpec(
            session_key="dashboard:chat-b",
            channel_id="",
            server="fake",
            register=register_payload_like_the_stub(sock, work),
            initialize={},
        )
        conn = await RawConnection.register(sock, holder_register_frame(spec))
        try:
            assert "result" in await conn.request(1, "initialize", INIT_PARAMS)
            (dead,) = [b for b in daemon.backends() if b.is_alive]
            os.kill(dead.pid, signal.SIGKILL)
            await async_wait_until(
                lambda: not dead.is_alive, describe=lambda: "the killed backend still alive"
            )

            reply = await conn.request(
                2, "tools/call", {"name": "emit_update", "arguments": {"uri": "x://a"}}
            )

            assert reply.get("result") == {"content": []}, reply
            assert len(recorded(launches)) == 2, "served by a replacement process"
        finally:
            await conn.close()


def test_the_gate_hands_back_only_calls_it_never_wrote() -> None:
    gate = call_gate.CallGate(lambda fid, frame: asyncio.sleep(0, True))
    gate._current, gate._sent, gate._current_origin = "f1", True, {"id": 1}
    gate._queue["f2"] = ({"id": "f2"}, {"id": 2})
    gate._queue["f3"] = ({"id": "f3"}, None)
    assert gate.fail_all() == [("f2", {"id": 2})], "a written call and a frameless one stay out"
    gate._current, gate._sent, gate._current_origin = "f4", False, {"id": 4}
    assert gate.fail_all() == [("f4", {"id": 4})], "the slot holder before its write"
    gate._current, gate._sent, gate._unwanted = "f5", False, True
    gate._current_origin = {"id": 5}
    assert gate.fail_all() == [], "a cancelled call is owed nothing"


#: A third-party server whose two tools write its own store, so they take turns:
#: ``slow`` never answers, ``write`` answers with the pid that ran it. Every
#: launch and every call is logged as ``<pid> <what>``.
_QUEUE_SERVER = r"""
import json, os, sys
log = sys.argv[1]
def note(what):
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"{os.getpid()} {what}\n")
def reply(msg, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
note("launch")
writer = {"readOnlyHint": False}
tools = [
    {"name": n, "inputSchema": {"type": "object", "properties": {}}, "annotations": writer}
    for n in ("slow", "write")
]
for line in sys.stdin:
    msg = json.loads(line) if line.strip() else {}
    method = msg.get("method")
    if method == "initialize":
        reply(msg, {"protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}}, "serverInfo": {"name": "q", "version": "1"}})
    elif method == "tools/list":
        reply(msg, {"tools": tools})
    elif method == "tools/call":
        name = msg["params"]["name"]
        note(name)
        if name == "write":
            reply(msg, {"content": [{"type": "text", "text": f"pid={os.getpid()}"}]})
    elif method == "ping" and "id" in msg:
        reply(msg, {})
"""


@pytest.mark.asyncio
@pytest.mark.xdist_group("mcp_gateway")
@pytest.mark.skipif(sys.platform == "win32", reason="the real-daemon helpers dial a unix socket")
async def test_a_call_queued_behind_another_runs_on_the_replacement_when_the_server_dies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, short_sock_dir: Path
) -> None:
    """Chat B's call waits behind chat A's; the server is killed; B's call never reached it."""
    import mcp_resource_subscription_helpers as helpers
    from mcp_resource_subscription_helpers import (
        INIT_PARAMS,
        RawConnection,
        register_payload_like_the_stub,
        running_daemon,
    )

    from kiro_crew.mcp_gateway.resource_holder import HolderSpec, holder_register_frame

    server = tmp_path / "queue_server.py"
    server.write_text(_QUEUE_SERVER, encoding="utf-8")
    monkeypatch.setattr(helpers, "FAKE_SERVER", server)
    # gatewayd installs the process-wide hazard sink; restored after the test.
    monkeypatch.setattr(hazards, "_sink", None)
    sock, work, log = short_sock_dir / "gw.sock", tmp_path / "ws", tmp_path / "server.log"
    work.mkdir()

    def lines() -> list[str]:
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    async with running_daemon(
        monkeypatch, sock, work, log, tmp_path / "subs.txt", tmp_path / "unused.txt"
    ) as daemon:
        conns = []
        for key in ("dashboard:chat-a", "dashboard:chat-b"):
            spec = HolderSpec(
                session_key=key,
                channel_id="",
                server="q",
                register=register_payload_like_the_stub(sock, work),
                initialize={},
            )
            conns.append(await RawConnection.register(sock, holder_register_frame(spec)))
        a, b = conns
        try:
            for conn in conns:
                assert "result" in await conn.request(1, "initialize", INIT_PARAMS)
                assert "result" in await conn.request(2, "tools/list", {})
            (dead,) = [be for be in daemon.backends() if be.is_alive]
            gate = caller_scope.state(dead).call_gate
            assert gate is not None, "the server's writers take turns"
            slow = asyncio.create_task(a.request(3, "tools/call", {"name": "slow"}))
            await async_wait_until(lambda: f"{dead.pid} slow" in lines())
            queued = asyncio.create_task(b.request(3, "tools/call", {"name": "write"}))
            await async_wait_until(lambda: len(gate.queued()) == 1)

            os.kill(dead.pid, signal.SIGKILL)

            a_reply, b_reply = await slow, await queued
            assert "error" in a_reply, "a call the server had stays failed"
            launches = [ln.split()[0] for ln in lines() if ln.endswith(" launch")]
            assert len(launches) == 2, lines()
            assert b_reply.get("result", {}).get("content") == [
                {"type": "text", "text": f"pid={launches[1]}"}
            ], b_reply
            assert f"{dead.pid} write" not in lines(), "the dead server never saw it"
        finally:
            for conn in conns:
                await conn.close()
