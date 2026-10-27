"""The Copilot CLI harness: its routing guards, its launch MCP config, its child env.

Copilot CLI 1.0.100 is routed by an ACP v1 session config option (``allow_all``),
which Crew writes ``off`` on every session/new and session/load. Three things hold a
session there afterwards, and each is pinned here against the live corpus under
``test/fixtures/acp_frames/copilot/`` where the corpus carries the frame:

* the prompt-command allowlist refuses a ``/<command>`` the harness would run itself
  before the prompt reaches the wire (``ACP_BACKEND_PROMPT_COMMAND_ALLOWLIST``);
* the mid-session tripwire stops the harness when it reports ``allow_all`` on or the
  ``autopilot`` mode (``ACP_BACKEND_FORBIDDEN_MODES``);
* the child never inherits ``COPILOT_ALLOW_ALL`` or ``COPILOT_ASSISTED_APPROVAL``.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import acp_backends
from kiro_crew.acp.client import AcpClient, AcpToolGateUnroutable
from kiro_crew.acp.harness import copilot as copilot_mod
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.agent_sdk import tool_gate as gate
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_FORBIDDEN_MODES,
    ACP_BACKEND_PROMPT_COMMAND_ALLOWLIST,
    prompt_command_issue,
    session_config_drift_issue,
)
from kiro_crew.agent_sdk.drivers.acp_vocab import is_user_worded_failure

COPILOT = ACP_BACKEND_COPILOT
_CORPUS = Path(__file__).parent / "fixtures" / "acp_frames" / "copilot"
_AUTOPILOT = "https://agentclientprotocol.com/protocol/session-modes#autopilot"
_AGENT = "https://agentclientprotocol.com/protocol/session-modes#agent"


def _frames(name: str) -> list[dict]:
    lines = (_CORPUS / name).read_text().splitlines()
    return [json.loads(line) for line in lines[1:] if line.strip()]


def _updates(name: str, kind: str) -> list[dict]:
    return [
        f
        for f in _frames(name)
        if f.get("method") == "session/update"
        and f["params"]["update"].get("sessionUpdate") == kind
    ]


def _message(frame: dict) -> JsonRpcMessage:
    return JsonRpcMessage(**{k: frame[k] for k in ("method", "params")})


# ── the prompt-command allowlist ─────────────────────────────────────────────

# Each of these was MEASURED turning Copilot CLI 1.0.100's allow_all on, or (the
# zero-width one) is refused on the fail-closed side of the parser's stripping.
_MEASURED_ESCALATIONS = (
    " /allow-all on",
    "\n/allow-all on",
    "/ALLOW-ALL on",
    "/allow-all",
    "/yolo",
    "/Yolo",
    "/permissions allow-all",
    "/allow-all   on",
    "/yolo\ton",
    "\t/yolo",
    "\u00a0/yolo",
    "\u3000/yolo",
    "\u200b/allow-all on",
    "/autopilot on",
)

# Each of these was measured reaching the MODEL as ordinary text.
_MEASURED_PASS_THROUGH = (
    "/yolo/x",
    "/yolo.",
    "/yolo,",
    "//yolo",
    "/ yolo",
    "hello",
    "please read /etc/hosts",
    "/etc/hosts has a typo",
    "",
)


def test_an_underscore_name_is_refused_although_the_harness_did_not_run_it() -> None:
    """``/allow_all on`` reached the model on 1.0.100, but the grammar admits ``_``:
    refusing it costs a resent prompt, admitting a future command spelled that way
    would cost the gate."""
    assert prompt_command_issue(COPILOT, ["/allow_all on"])


@pytest.mark.parametrize("text", _MEASURED_ESCALATIONS)
def test_a_command_that_moves_the_permission_state_is_refused(text: str) -> None:
    issue = prompt_command_issue(COPILOT, [text])
    assert issue, text
    assert "approval mode" in issue


@pytest.mark.parametrize("text", _MEASURED_PASS_THROUGH)
def test_text_the_harness_sends_to_the_model_is_not_refused(text: str) -> None:
    assert prompt_command_issue(COPILOT, [text]) == ""


@pytest.mark.parametrize("name", sorted(ACP_BACKEND_PROMPT_COMMAND_ALLOWLIST[COPILOT]))
def test_every_allowlisted_command_passes_in_any_case(name: str) -> None:
    assert prompt_command_issue(COPILOT, [f"/{name}"]) == ""
    assert prompt_command_issue(COPILOT, [f"  /{name.upper()} with args"]) == ""


def test_a_command_this_build_has_never_seen_fails_closed() -> None:
    assert prompt_command_issue(COPILOT, ["/some-future-command"])


def test_every_text_block_is_checked_not_only_the_first() -> None:
    assert prompt_command_issue(COPILOT, ["look at this", "/yolo"])


def test_the_allowlist_never_admits_a_permission_command() -> None:
    """A guard on the table itself: none of the measured escalation names may be added."""
    allowed = ACP_BACKEND_PROMPT_COMMAND_ALLOWLIST[COPILOT]
    assert not allowed & {
        "allow-all",
        "yolo",
        "permissions",
        "autopilot",
        "mode",
        "clear",
        "plan",
        "research",
    }


@pytest.mark.parametrize("backend", sorted(acp_backends.ACP_BACKENDS_KNOWN - {COPILOT}))
def test_a_backend_without_a_row_is_never_refused(backend: str) -> None:
    assert prompt_command_issue(backend, ["/yolo"]) == ""


def _prompt_client(tmp_path: Path, backend: str) -> tuple[AcpClient, list]:
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    client._process = MagicMock(returncode=None)
    client._session_id = "copilot-session-1"
    sent: list = []

    async def _send(method: str, params: dict) -> int:
        sent.append((method, params))
        return len(sent)

    client._send_request = AsyncMock(side_effect=_send)
    client._settle_orphan_steers = AsyncMock()
    return client, sent


@pytest.mark.asyncio
async def test_a_refused_prompt_never_reaches_the_wire(tmp_path: Path) -> None:
    client, sent = _prompt_client(tmp_path, COPILOT)
    client._turn_done.clear()

    with pytest.raises(AcpError) as raised:
        await client._send_prompt("/yolo")

    assert sent == []
    assert "/yolo" in str(raised.value)
    # Nothing is in flight, so a caller waiting on the turn's end is released.
    assert client._turn_done.is_set()


@pytest.mark.asyncio
async def test_an_allowed_prompt_is_sent(tmp_path: Path) -> None:
    client, sent = _prompt_client(tmp_path, COPILOT)
    await client._send_prompt("/compact")
    assert [method for method, _ in sent] == ["session/prompt"]


@pytest.mark.asyncio
async def test_the_kiro_path_sends_a_slash_prompt_unchanged(tmp_path: Path) -> None:
    """harness-parity H13: the guard is a table lookup the kiro path never matches."""
    client, sent = _prompt_client(tmp_path, acp_backends.ACP_BACKEND_KIRO)
    await client._send_prompt("/yolo")
    assert [method for method, _ in sent] == ["session/prompt"]


# ── the mid-session tripwire ─────────────────────────────────────────────────


def test_the_corpus_shows_the_harness_reporting_allow_all_on() -> None:
    """The premise: the frame the tripwire reads is one Copilot CLI really sends."""
    values = [
        option.get("currentValue")
        for frame in _updates("handshake-live.jsonl", "config_option_update")
        for option in frame["params"]["update"]["configOptions"]
        if option.get("id") == "allow_all"
    ]
    assert "on" in values and "off" in values, values
    modes = [
        f["params"]["update"]["currentModeId"]
        for f in _updates("handshake-live.jsonl", "current_mode_update")
    ]
    assert _AUTOPILOT in modes and _AGENT in modes, modes


def test_allow_all_on_and_autopilot_are_drift_and_the_routed_state_is_not() -> None:
    for frame in _updates("handshake-live.jsonl", "config_option_update"):
        update = frame["params"]["update"]
        values = {o.get("id"): o.get("currentValue") for o in update["configOptions"]}
        drifted = values.get("allow_all") != "off" or values.get("mode") == _AUTOPILOT
        assert bool(session_config_drift_issue(COPILOT, update)) is drifted, values
    for frame in _updates("handshake-live.jsonl", "current_mode_update"):
        update = frame["params"]["update"]
        issue = session_config_drift_issue(COPILOT, update)
        assert bool(issue) is (update["currentModeId"] == _AUTOPILOT)


@pytest.mark.parametrize("backend", sorted(acp_backends.ACP_BACKENDS_KNOWN - {COPILOT}))
def test_drift_is_never_read_for_a_backend_without_a_row(backend: str) -> None:
    update = {"sessionUpdate": "current_mode_update", "currentModeId": _AUTOPILOT}
    assert session_config_drift_issue(backend, update) == ""


def test_every_forbidden_mode_backend_is_session_config_routed() -> None:
    """The tripwire reads the routing table's option, so a row elsewhere would be inert."""
    for backend in ACP_BACKEND_FORBIDDEN_MODES:
        assert gate.routing_for(backend) is gate.Routing.SESSION_CONFIG


def _tripwire_client(backend: str = COPILOT) -> tuple[AcpClient, list]:
    client = AcpClient.__new__(AcpClient)
    client._acp_backend = backend
    client._session_id = "copilot-session-1"
    client._session_key = ""
    killed: list = []

    async def _kill(force: bool = False) -> None:
        killed.append(force)

    client._kill_process = _kill  # type: ignore[method-assign]
    return client, killed


def test_the_tripwire_stops_a_harness_that_turned_allow_all_on() -> None:
    frame = next(
        f
        for f in _updates("handshake-live.jsonl", "config_option_update")
        if any(
            o.get("id") == "allow_all" and o.get("currentValue") == "on"
            for o in f["params"]["update"]["configOptions"]
        )
    )
    client, killed = _tripwire_client()
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(client._tripwire_session_config(_message(frame)))
    assert killed == [True]


def test_the_tripwire_stops_a_harness_that_entered_autopilot() -> None:
    frame = next(
        f
        for f in _updates("handshake-live.jsonl", "current_mode_update")
        if f["params"]["update"]["currentModeId"] == _AUTOPILOT
    )
    client, killed = _tripwire_client()
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(client._tripwire_session_config(_message(frame)))
    assert killed == [True]


def test_the_routed_state_reported_again_is_a_no_op() -> None:
    client, killed = _tripwire_client()
    for frame in _updates("turn-live.jsonl", "config_option_update") + _updates(
        "handshake-live.jsonl", "current_mode_update"
    ):
        if frame["params"]["update"].get("currentModeId") == _AUTOPILOT:
            continue
        asyncio.run(client._tripwire_session_config(_message(frame)))
    assert killed == []


@pytest.mark.parametrize("backend", sorted(acp_backends.ACP_BACKENDS_KNOWN - {COPILOT}))
def test_the_tripwire_is_inert_on_every_other_backend(backend: str) -> None:
    frame = {
        "method": "session/update",
        "params": {
            "sessionId": "s",
            "update": {"sessionUpdate": "current_mode_update", "currentModeId": _AUTOPILOT},
        },
    }
    client, killed = _tripwire_client(backend)
    client._kill_process = None  # type: ignore[assignment]  # would raise if reached
    asyncio.run(client._tripwire_session_config(_message(frame)))
    assert killed == []


def test_the_tripwire_runs_on_every_reader_the_goose_one_does() -> None:
    import inspect

    from kiro_crew.acp import client_turn

    source = inspect.getsource(client_turn)
    assert "await self._tripwire_goose_mode(msg)" not in source
    assert source.count("await self._tripwire_permission_state(msg)") >= 3


# ── the session-config route itself ──────────────────────────────────────────


def test_session_new_advertises_the_value_crew_writes() -> None:
    new = next(f for f in _frames("handshake-live.jsonl") if f.get("id") == 2)
    assert gate.session_config_issue(COPILOT, new["result"]["configOptions"]) == ""


def test_session_load_advertises_it_too() -> None:
    load = next(
        f
        for f in _frames("session-load-live.jsonl")
        if isinstance(f.get("result"), dict) and "configOptions" in f["result"]
    )
    assert gate.session_config_issue(COPILOT, load["result"]["configOptions"]) == ""


def test_copilot_reads_routed() -> None:
    verdict, _why = gate.routing_verdict(COPILOT)
    assert verdict is gate.Verdict.ROUTED
    assert gate.is_enforced(COPILOT)


# ── the launch MCP config and the child environment ──────────────────────────


def _stdio(name: str, command: str, **env: str) -> dict:
    return {
        "name": name,
        "command": command,
        "args": ["-m", "x"],
        "env": [{"name": k, "value": v} for k, v in env.items()],
    }


def test_only_stdio_elements_go_to_the_launch_config() -> None:
    document = copilot_mod.copilot_mcp_config(
        [
            _stdio("crew", sys.executable, CREW_TOKEN="t"),
            # A url element stays on the wire even if it also names a command.
            {
                "name": "remote",
                "type": "http",
                "url": "https://example.invalid/mcp",
                "command": sys.executable,
                "headers": [],
            },
            {"name": "", "command": sys.executable},
            "not-an-element",
        ]
    )
    assert document == {
        "mcpServers": {
            "crew": {
                "type": "local",
                "command": sys.executable,
                "args": ["-m", "x"],
                "env": {"CREW_TOKEN": "t"},
                "tools": ["*"],
            }
        }
    }


def test_no_stdio_element_means_no_file() -> None:
    assert copilot_mod.copilot_mcp_config([]) is None
    assert copilot_mod.copilot_mcp_config([{"name": "r", "url": "https://x.invalid"}]) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_launch_config_is_owner_only_and_removed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(copilot_mod, "config_dir", lambda: tmp_path)
    path = copilot_mod.write_copilot_mcp_config({"mcpServers": {}})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    assert json.loads(Path(path).read_text()) == {"mcpServers": {}}
    copilot_mod.discard_copilot_mcp_config(path)
    assert not os.path.exists(path)
    copilot_mod.discard_copilot_mcp_config(path)  # already gone is the same outcome


def test_a_config_left_by_a_dead_gateway_is_swept(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(copilot_mod, "config_dir", lambda: tmp_path)
    directory = Path(copilot_mod._config_directory())
    stale = directory / "copilot-mcp-999999999-left.json"
    foreign = directory / "auth-signin-999999999-not-ours.json"
    foreign.write_text("{}")
    stale.write_text("{}")
    monkeypatch.setattr(copilot_mod.platform_compat, "pid_exists", lambda pid: False)
    fresh = copilot_mod.write_copilot_mcp_config({"mcpServers": {}})
    assert not stale.exists()
    # The staging directory is shared with the gateway's own auth-store temps.
    assert foreign.exists()
    assert os.path.exists(fresh)


def test_the_child_never_inherits_a_permission_source() -> None:
    env = {name: "true" for name in copilot_mod.COPILOT_DROPPED_ENV}
    env["PATH"] = "/usr/bin"
    copilot_mod.CopilotLaunch().apply_spawn_env(env)
    assert env == {"PATH": "/usr/bin"}
    assert {"COPILOT_ALLOW_ALL", "COPILOT_CLI_DIST_DIR", "COPILOT_PLAN_THEN_AUTOPILOT"} <= set(
        copilot_mod.COPILOT_DROPPED_ENV
    )


# ── repository configuration Copilot CLI would run unasked ───────────────────


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src" / "pkg").mkdir(parents=True)
    return root


@pytest.mark.parametrize(
    "relative",
    [
        ".github/hooks/sessionStart.json",
        ".github/hooks/probe.json",
        ".github/hooks/Other.JSON",
        ".github/copilot/settings.json",
        ".github/copilot/settings.local.json",
        ".mcp.json",
        ".github/mcp.json",
        ".github/lsp.json",
        ".github/extensions/ext/extension.mjs",
        ".github/plugin/plugin.json",
        ".claude-plugin/plugin.json",
    ],
)
def test_a_project_with_its_own_copilot_config_is_refused(tmp_path, relative: str) -> None:
    root = _repo(tmp_path)
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}")
    # From the root and from a subdirectory: the harness reads the git root's config.
    for cwd in (root, root / "src" / "pkg"):
        issue = copilot_mod.copilot_repo_config_issue(cwd)
        assert issue and "without asking for permission" in issue, cwd
        assert "only to the Copilot CLI harness" in issue


def test_a_project_without_it_starts(tmp_path) -> None:
    root = _repo(tmp_path)
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.json").write_text("{}")
    (root / ".github" / "hooks").mkdir()
    (root / ".github" / "hooks" / "README.md").write_text("not a hook")
    (root / ".github" / "copilot").mkdir()
    (root / ".github" / "copilot" / "instructions.json").write_text("{}")
    assert copilot_mod.copilot_repo_config_issue(root / "src") == ""
    assert copilot_mod.copilot_repo_config_issue("") == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_symlinked_ancestor_is_checked_on_both_chains(tmp_path) -> None:
    real = _repo(tmp_path)
    (real / ".mcp.json").write_text("{}")
    link = tmp_path / "link"
    link.symlink_to(real / "src")
    assert copilot_mod.copilot_repo_config_issue(link / "pkg")


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX modes")
def test_an_ancestor_crew_cannot_list_is_not_a_finding(tmp_path) -> None:
    locked = tmp_path / "locked"
    work = locked / "sub" / "work"
    work.mkdir(parents=True)
    locked.chmod(0o311)
    try:
        assert copilot_mod.copilot_repo_config_issue(work) == ""
        # A named file under it is still found: the harness opens it by name too.
        locked.chmod(0o711)
        (locked / ".mcp.json").write_text("{}")
        locked.chmod(0o311)
        assert copilot_mod.copilot_repo_config_issue(work)
    finally:
        locked.chmod(0o755)


def test_the_walk_stops_at_the_git_root(tmp_path) -> None:
    (tmp_path / ".github" / "hooks").mkdir(parents=True)
    (tmp_path / ".github" / "hooks" / "outer.json").write_text("{}")
    root = _repo(tmp_path)
    assert copilot_mod.copilot_repo_config_issue(root / "src") == ""
    # Without a git root it keeps walking, so the outer hooks are found.
    plain = tmp_path / "plain" / "dir"
    plain.mkdir(parents=True)
    assert copilot_mod.copilot_repo_config_issue(plain)


@pytest.mark.asyncio
async def test_a_project_with_repo_hooks_is_refused_before_anything_is_written(
    tmp_path, monkeypatch
) -> None:
    from kiro_crew.acp.harness.base import SpawnContext

    root = _repo(tmp_path)
    (root / ".github" / "hooks").mkdir(parents=True)
    (root / ".github" / "hooks" / "h.json").write_text("{}")
    monkeypatch.setattr(copilot_mod, "config_dir", lambda: tmp_path / "home")
    client = AcpClient(work_dir=root, acp_backend=COPILOT)
    ctx = SpawnContext.__new__(SpawnContext)
    object.__setattr__(ctx, "session", client)
    object.__setattr__(ctx, "work_dir", root)
    object.__setattr__(ctx, "sandbox_mode", "auto")

    with pytest.raises(AcpToolGateUnroutable) as raised:
        await copilot_mod.CopilotLaunch().resolve_spawn(ctx)
    assert "h.json" in str(raised.value)
    # A chat surface shows this text (with its remedy), not a generic retry reply.
    assert is_user_worded_failure(raised.value)
    assert list((tmp_path / "home").glob("auth-store-staging/copilot-mcp-*")) == []


@pytest.mark.asyncio
async def test_a_config_write_failure_is_worded_for_the_person(tmp_path, monkeypatch) -> None:
    from kiro_crew.acp import launch as launch_mod
    from kiro_crew.acp.harness.base import SpawnContext

    monkeypatch.setattr(copilot_mod, "config_dir", lambda: tmp_path / "home")
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)

    async def _resolve(_backend):
        return ("copilot", ["copilot", "--acp"], "copilot", "copilot")

    async def _prepare() -> None:
        return None

    def _full(_document):
        raise OSError("No space left on device")

    monkeypatch.setattr(launch_mod, "resolve_self_served_launch", _resolve)
    monkeypatch.setattr(copilot_mod, "write_copilot_mcp_config", _full)
    client._prepare_session_mcp = _prepare  # type: ignore[method-assign]
    client._session_mcp_servers = lambda: [_stdio("crew", sys.executable, T="t")]  # type: ignore
    ctx = SpawnContext.__new__(SpawnContext)
    object.__setattr__(ctx, "session", client)
    object.__setattr__(ctx, "work_dir", tmp_path)
    object.__setattr__(ctx, "sandbox_mode", "auto")

    with pytest.raises(AcpError) as raised:
        await copilot_mod.CopilotLaunch().resolve_spawn(ctx)
    assert "could not write the MCP server list" in str(raised.value)
    assert is_user_worded_failure(raised.value)


# ── the review fixes: read-back, drain restore, pre-prompt drift, file lifetime ──


def _options(allow_all: str, mode: str = _AGENT) -> list[dict]:
    new = next(f for f in _frames("handshake-live.jsonl") if f.get("id") == 2)
    options = json.loads(json.dumps(new["result"]["configOptions"]))
    for option in options:
        if option["id"] == "allow_all":
            option["currentValue"] = allow_all
        if option["id"] == "mode":
            option["currentValue"] = mode
    return options


def _routing_client(tmp_path: Path, answer: list[dict]) -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    client._acp_config_options = _options("on")

    async def _write(config_id: str, value: str) -> None:
        assert (config_id, value) == ("allow_all", "off")
        client._acp_config_options = answer

    client.set_config_option = _write  # type: ignore[method-assign]
    return client


@pytest.mark.asyncio
async def test_a_write_the_harness_answers_with_allow_all_still_on_is_refused(tmp_path) -> None:
    client = _routing_client(tmp_path, _options("on"))
    with pytest.raises(AcpToolGateUnroutable):
        await client._apply_session_permission_routing()


@pytest.mark.asyncio
async def test_only_config_reports_from_before_the_answer_are_dropped(tmp_path) -> None:
    client = _routing_client(tmp_path, _options("off"))
    stale = _buffered({"sessionUpdate": "config_option_update", "configOptions": _options("on")})
    oauth = JsonRpcMessage(method="_kiro.dev/mcp/oauth_request", params={"serverName": "x"})
    client._mcp_notifications.extend([stale, oauth])
    client._mcp_report_frame_floor = 2
    await client._apply_session_permission_routing()
    live = _buffered({"sessionUpdate": "config_option_update", "configOptions": _options("on")})
    client._mcp_notifications.append(live)

    client._drop_superseded_config_frames()

    # The stale restored report is gone, the OAuth frame stays, the live one stays,
    # and the report floor still marks the same frame (the live one).
    assert client._mcp_notifications == [oauth, live]
    assert client._mcp_report_frame_floor == 1


@pytest.mark.asyncio
async def test_a_mode_report_from_before_the_answer_is_kept(tmp_path) -> None:
    """The allow_all write does not move the mode, so its answer supersedes no mode."""
    client = _routing_client(tmp_path, _options("off"))
    mode = _buffered({"sessionUpdate": "current_mode_update", "currentModeId": _AUTOPILOT})
    client._mcp_notifications.append(mode)
    await client._apply_session_permission_routing()
    client._drop_superseded_config_frames()
    assert client._mcp_notifications == [mode]


@pytest.mark.asyncio
async def test_a_mode_report_the_start_drain_reads_still_stops_the_session(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    client._session_id = "copilot-session-1"
    client._acp_config_options = _options("off")
    client._mcp_notifications.append(
        _buffered({"sessionUpdate": "current_mode_update", "currentModeId": _AUTOPILOT})
    )
    client._read_message = AsyncMock(return_value=None)
    killed: list = []

    async def _kill(force: bool = False) -> None:
        killed.append(force)

    client._kill_process = _kill  # type: ignore[method-assign]
    await client._drain_notifications(duration=0.05, idle_exit=0.05)
    assert client._mcp_notifications == []
    with pytest.raises(AcpToolGateUnroutable):
        await client._refuse_session_config_drift()
    assert killed == [True]


@pytest.mark.asyncio
async def test_the_kiro_drain_keeps_no_mode_reports(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=acp_backends.ACP_BACKEND_KIRO)
    client._mcp_notifications.append(
        _buffered({"sessionUpdate": "current_mode_update", "currentModeId": _AUTOPILOT})
    )
    client._read_message = AsyncMock(return_value=None)
    await client._drain_notifications(duration=0.05, idle_exit=0.05)
    assert client._drain_mode_reports == []


def _buffered(update: dict) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update", params={"sessionId": "copilot-session-1", "update": update}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    ["cached-on", "buffered-on", "buffered-autopilot"],
)
async def test_a_move_no_reader_saw_stops_the_next_prompt(tmp_path, state: str) -> None:
    client, sent = _prompt_client(tmp_path, COPILOT)
    client._acp_config_options = _options("off")
    killed: list = []

    async def _kill(force: bool = False) -> None:
        killed.append(force)

    client._kill_process = _kill  # type: ignore[method-assign]
    if state == "cached-on":
        client._acp_config_options = _options("on")
    elif state == "buffered-on":
        client._mcp_notifications.append(
            _buffered({"sessionUpdate": "config_option_update", "configOptions": _options("on")})
        )
    else:
        client._mcp_notifications.append(
            _buffered({"sessionUpdate": "current_mode_update", "currentModeId": _AUTOPILOT})
        )

    with pytest.raises(AcpToolGateUnroutable):
        await client._send_prompt("hello")
    assert sent == [] and killed == [True]


@pytest.mark.asyncio
async def test_the_routed_state_lets_the_prompt_through(tmp_path) -> None:
    client, sent = _prompt_client(tmp_path, COPILOT)
    client._acp_config_options = _options("off")
    client._mcp_notifications.append(
        _buffered({"sessionUpdate": "config_option_update", "configOptions": _options("off")})
    )
    await client._send_prompt("hello")
    assert [method for method, _ in sent] == ["session/prompt"]


@pytest.mark.asyncio
async def test_the_launch_config_goes_when_initialize_answers_or_fails(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    path = tmp_path / "cfg.json"
    path.write_text("{}")
    client._copilot_mcp_config_path = str(path)
    client._send_request = AsyncMock(return_value=1)
    client._wait_for_response = AsyncMock(side_effect=AcpError("boom"))
    with pytest.raises(AcpError):
        await client._initialize_session()
    assert not path.exists()
    assert client._copilot_mcp_config_path == ""


@pytest.mark.asyncio
async def test_a_refused_spawn_removes_the_config_it_wrote(tmp_path, monkeypatch) -> None:
    from kiro_crew.acp import launch as launch_mod
    from kiro_crew.acp.harness.base import SpawnContext

    monkeypatch.setattr(copilot_mod, "config_dir", lambda: tmp_path)
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)

    async def _resolve(_backend):
        return ("copilot", ["copilot", "--acp"], "copilot", "copilot")

    async def _prepare() -> None:
        return None

    async def _refuse(*_a, **_k):
        raise AcpToolGateUnroutable("no sandbox")

    monkeypatch.setattr(launch_mod, "resolve_self_served_launch", _resolve)
    monkeypatch.setattr(launch_mod, "_run_preflight_bounded", _refuse)
    client._prepare_session_mcp = _prepare  # type: ignore[method-assign]
    client._session_mcp_servers = lambda: [_stdio("crew", sys.executable, T="t")]  # type: ignore
    ctx = SpawnContext.__new__(SpawnContext)
    object.__setattr__(ctx, "session", client)
    object.__setattr__(ctx, "sandbox_mode", "auto")

    with pytest.raises(AcpToolGateUnroutable):
        await copilot_mod.CopilotLaunch().resolve_spawn(ctx)
    assert list((tmp_path / "auth-store-staging").glob("copilot-mcp-*.json")) == []
    assert client._copilot_mcp_config_path == ""


def test_a_spawn_that_never_reached_initialize_leaves_no_config(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    path = tmp_path / "left.json"
    path.write_text("{}")
    client._copilot_mcp_config_path = str(path)
    client._reset_state()
    assert not path.exists() and client._copilot_mcp_config_path == ""


def test_a_git_pointer_git_would_reject_does_not_end_the_walk(tmp_path) -> None:
    (tmp_path / ".github" / "hooks").mkdir(parents=True)
    (tmp_path / ".github" / "hooks" / "outer.json").write_text("{}")
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / ".git").write_text("gitdir: does/not/exist\n")
    assert copilot_mod.copilot_repo_config_issue(inner)
    # A worktree pointer whose target exists is a root, so the outer hooks are not read.
    (tmp_path / "real.git").mkdir()
    (inner / ".git").write_text(f"gitdir: {tmp_path / 'real.git'}\n")
    assert copilot_mod.copilot_repo_config_issue(inner) == ""


def test_a_failed_write_leaves_no_partial_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(copilot_mod, "config_dir", lambda: tmp_path)
    with pytest.raises(TypeError):
        copilot_mod.write_copilot_mcp_config({"mcpServers": {"x": object()}})
    assert list((tmp_path / "auth-store-staging").glob("copilot-mcp-*.json")) == []


@pytest.mark.asyncio
async def test_a_drift_refusal_releases_a_waiter(tmp_path) -> None:
    client, sent = _prompt_client(tmp_path, COPILOT)
    client._acp_config_options = _options("on")

    async def _kill(force: bool = False) -> None:
        return None

    client._kill_process = _kill  # type: ignore[method-assign]
    client._turn_done.clear()
    with pytest.raises(AcpToolGateUnroutable):
        await client._send_prompt("hello")
    assert client._turn_done.is_set() and sent == []


def test_the_frame_path_check_reaches_the_copilot_tripwire() -> None:
    frame = next(
        f
        for f in _updates("handshake-live.jsonl", "current_mode_update")
        if f["params"]["update"]["currentModeId"] == _AUTOPILOT
    )
    client, killed = _tripwire_client()
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(client._tripwire_permission_state(_message(frame)))
    assert killed == [True]


def test_the_launch_config_lives_in_the_masked_staging_directory(tmp_path, monkeypatch) -> None:
    """Every agent sandbox masks this directory whole and the read gate fences it."""
    from kiro_crew import sandbox_leaves
    from kiro_crew.security.paths import is_sensitive_path

    monkeypatch.setattr(copilot_mod, "config_dir", lambda: tmp_path)
    path = Path(copilot_mod.write_copilot_mcp_config({"mcpServers": {}}))
    try:
        assert path.parent == tmp_path / sandbox_leaves._AUTH_STORE_STAGING_LEAF
        assert path.name.startswith("copilot-mcp-")
    finally:
        copilot_mod.discard_copilot_mcp_config(str(path))
    home = Path.home()
    assert is_sensitive_path(str(home / ".kiro" / "crew" / "auth-store-staging" / path.name))


@pytest.mark.asyncio
async def test_the_spawn_hands_the_config_back_to_this_child_alone(tmp_path, monkeypatch) -> None:
    from kiro_crew.acp import launch as launch_mod
    from kiro_crew.acp.harness.base import SpawnContext

    monkeypatch.setattr(copilot_mod, "config_dir", lambda: tmp_path / "home")
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)

    async def _resolve(_backend):
        return ("copilot", ["copilot", "--acp"], "copilot", "copilot")

    async def _prepare() -> None:
        return None

    async def _mask(*_a, **_k):
        return ("/home/x/.config/gh",)

    monkeypatch.setattr(launch_mod, "resolve_self_served_launch", _resolve)
    monkeypatch.setattr(launch_mod, "_run_preflight_bounded", _mask)
    client._prepare_session_mcp = _prepare  # type: ignore[method-assign]
    client._session_mcp_servers = lambda: [_stdio("crew", sys.executable, T="t")]  # type: ignore
    ctx = SpawnContext.__new__(SpawnContext)
    object.__setattr__(ctx, "session", client)
    object.__setattr__(ctx, "work_dir", tmp_path)
    object.__setattr__(ctx, "sandbox_mode", "auto")

    plan = await copilot_mod.CopilotLaunch().resolve_spawn(ctx)
    try:
        assert plan.extra_expose_files == (client._copilot_mcp_config_path,)
        assert plan.argv[-1] == "@" + client._copilot_mcp_config_path
        assert "auth-store-staging" in client._copilot_mcp_config_path
    finally:
        await client._discard_copilot_mcp_config()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_the_config_path_is_the_resolved_spelling(tmp_path, monkeypatch) -> None:
    """The sandbox carve-out matches the realpath spelling, so the file must use it."""
    real = tmp_path / "real-home"
    real.mkdir()
    link = tmp_path / "linked-home"
    link.symlink_to(real)
    monkeypatch.setattr(copilot_mod, "config_dir", lambda: link)
    path = copilot_mod.write_copilot_mcp_config({"mcpServers": {}})
    try:
        assert path.startswith(str(real.resolve()))
    finally:
        copilot_mod.discard_copilot_mcp_config(path)


# ── edit targets reach the shared permission parser ──────────────────────────


def _edit_permission_frame(name: str) -> dict:
    return next(
        f
        for f in _frames(name)
        if f.get("method") == "session/request_permission"
        and f["params"]["toolCall"].get("kind") == "edit"
    )


def _event_for(tmp_path: Path, frame: dict, backend: str = COPILOT):
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    return client._build_permission_event(
        JsonRpcMessage(id=frame["id"], method=frame["method"], params=frame["params"])
    )


def test_a_copilot_edit_names_its_file_to_the_write_ceiling(tmp_path) -> None:
    """The ``filesystem.write`` ceiling classifies an edit by its target set."""
    from kiro_crew.platform.governance import classify_tool_args

    frame = _edit_permission_frame("mcp-stdio-mount-live.jsonl")
    target = frame["params"]["toolCall"]["rawInput"]["fileName"]
    event = _event_for(tmp_path, frame)
    assert event is not None and event.raw_params_trusted
    assert ("filesystem.write", target) in classify_tool_args("edit", event.raw_tool_params)
    assert event.diff_path == target


def test_each_frame_of_a_multi_file_patch_is_judged_by_its_own_file(tmp_path) -> None:
    frame = _edit_permission_frame("mcp-stdio-mount-live.jsonl")
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    seen = []
    for name in ("/w/alpha.txt", "/w/beta/gamma.txt"):
        params = json.loads(json.dumps(frame["params"]))
        params["toolCall"]["rawInput"]["fileName"] = name
        params["toolCall"]["rawInput"]["diff"] = f"--- a/dev/null\n+++ b{name}\n+x\n"
        params["toolCall"]["locations"] = [{"path": name}]
        event = client._build_permission_event(
            JsonRpcMessage(id=frame["id"], method=frame["method"], params=params)
        )
        seen.append(event.raw_tool_params["path"])
    assert seen == [["/w/alpha.txt"], ["/w/beta/gamma.txt"]]


def test_an_edit_naming_no_absolute_target_is_denied_by_the_edit_gate(tmp_path) -> None:
    from kiro_crew.llm_helpers_parts.tool_input_denials import _edit_target_denial

    frame = _edit_permission_frame("mcp-stdio-mount-live.jsonl")
    params = json.loads(json.dumps(frame["params"]))
    params["toolCall"]["rawInput"] = {"diff": "+x"}
    params["toolCall"]["locations"] = [{"path": "relative/only.txt"}]
    event = _event_for(tmp_path, {**frame, "params": params})
    assert _edit_target_denial(event.raw_tool_params, event.diff_path, "edit") is not None
    # The frame still reaches the edit gate on trusted provenance (not the document
    # scan), with a relative stand-in the gate reads as unanchored; a relative path
    # the harness sent is never taken as a target.
    from kiro_crew.platform.tool_paths import edit_target_candidates

    assert event.raw_params_trusted
    assert event.raw_tool_params == {"path": ["copilot-edit-target-unresolved"]}
    assert edit_target_candidates(
        event.raw_tool_params, event.diff_path, tool_kind="edit"
    ).unanchored


def test_other_backends_build_the_same_frame_unseeded(tmp_path) -> None:
    """harness-parity H13: the seeding is copilot's alone."""
    frame = _edit_permission_frame("mcp-stdio-mount-live.jsonl")
    event = _event_for(tmp_path, frame, backend=acp_backends.ACP_BACKEND_CODEX)
    assert event is None or not event.raw_params_trusted


def _edit_frame_with(raw_input, *, kind="edit", locations=None) -> dict:
    frame = json.loads(json.dumps(_edit_permission_frame("mcp-stdio-mount-live.jsonl")))
    tool_call = frame["params"]["toolCall"]
    tool_call["rawInput"] = raw_input
    tool_call["locations"] = locations or []
    if kind is None:
        tool_call.pop("kind", None)
    else:
        tool_call["kind"] = kind
    return frame


def test_a_move_is_judged_by_the_file_it_removes_too(tmp_path) -> None:
    """Measured: a ``*** Move to:`` frame's ``fileName`` is the destination only."""
    diff = (
        "\ndiff --git a/w/src.txt b/w/moved/dst.txt\nindex 0000000..0000000 100644\n"
        "--- a/w/src.txt\n+++ b/w/moved/dst.txt\n@@ -1,1 +1,1 @@\n-s\n+d\n"
    )
    frame = _edit_frame_with(
        {"fileName": "/w/moved/dst.txt", "diff": diff}, locations=[{"path": "/w/moved/dst.txt"}]
    )
    event = _event_for(tmp_path, frame)
    assert event.raw_tool_params == {"path": ["/w/moved/dst.txt", "/w/src.txt"]}


def test_a_new_file_diff_does_not_name_dev_null(tmp_path) -> None:
    diff = "--- a/dev/null\n+++ b/w/new.txt\n@@ -1,0 +1,1 @@\n+n\n"
    event = _event_for(tmp_path, _edit_frame_with({"fileName": "/w/new.txt", "diff": diff}))
    assert event.raw_tool_params == {"path": ["/w/new.txt"]}


@pytest.mark.parametrize("kind", ["other", None, "move"])
def test_a_write_outside_the_edit_kind_is_still_routed_to_the_write_plane(tmp_path, kind) -> None:
    from kiro_crew.platform.tool_paths import is_edit_call

    raw = {"fileName": "/w/x.txt", "diff": "+x"} if kind != "move" else {}
    frame = _edit_frame_with(raw, kind=kind, locations=[{"path": "/w/x.txt"}])
    event = _event_for(tmp_path, frame)
    assert event.raw_tool_params == {"path": ["/w/x.txt"]}
    assert is_edit_call(event.tool_kind or "", event.diff_path)


def test_a_read_frame_is_left_to_the_shared_parser(tmp_path) -> None:
    frame = _edit_frame_with({"path": "/w/r.txt"}, kind="read", locations=[{"path": "/w/r.txt"}])
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    client._seed_copilot_edit_targets(
        JsonRpcMessage(id=frame["id"], method=frame["method"], params=frame["params"])
    )
    assert client._tool_call_diff_path == {} and client._tool_call_params == {}


def _move_diff(source: str, dest: str) -> str:
    # Copilot folds a POSIX path's leading ``/`` into the ``a/`` prefix; a drive
    # path keeps its drive after the prefix (``a/C:\\...``).
    src, dst = source.lstrip("/"), dest.lstrip("/")
    return (
        f"\ndiff --git a/{src} b/{dst}\nindex 0000000..0000000 100644\n"
        f"--- a/{src}\n+++ b/{dst}\n@@ -1,1 +1,1 @@\n-s\n+d\n"
    )


async def _always_on_gate_approves(event) -> bool:
    from unittest.mock import MagicMock, patch

    import kiro_crew.sel as sel_mod
    from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission
    from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

    class _Provider:
        async def approve_tool(self, request_id: str) -> None:
            pass

        async def reject_tool(self, request_id: str) -> None:
            pass

    llm_event = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="Move and update file",
        request_id="r1",
        tool_kind=event.tool_kind,
        # Empty so the verdict comes from the event's trusted targets alone: the
        # document scan of the diff text would also name the source.
        tool_input="",
        raw_tool_params=event.raw_tool_params,
        raw_params_trusted=event.raw_params_trusted,
        shell_classified=event.shell_classified,
        is_shell=event.is_shell,
        diff_path=event.diff_path,
    )
    with patch.object(sel_mod, "sel", lambda: MagicMock()):
        return await _resolve_permission(
            _Provider(), llm_event, ToolApprovalPolicy.AUTO_APPROVE, None  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("kind", ["edit", "move", "other", None])
@pytest.mark.asyncio
async def test_a_moves_source_is_refused_at_permission_time(tmp_path, kind) -> None:
    """A move's destination is harmless, its SOURCE is write-protected config: the
    permission path refuses it (the transport floor judges the trusted targets)."""
    source = os.path.join(os.path.expanduser("~"), ".kiro", "agents", "pwn.json")
    dest = str(tmp_path / "harmless.txt")
    frame = _edit_frame_with(
        {"fileName": dest, "diff": _move_diff(source, dest)},
        kind=kind,
        locations=[{"path": dest}],
    )
    event = _event_for(tmp_path, frame)
    # Nothing about the call is rewritten: its kind and shell classification stay.
    assert event.tool_kind == (kind or "") and not event.shell_classified
    assert event.raw_params_trusted and event.diff_path == dest
    assert source in event.raw_tool_params["path"]
    assert await _always_on_gate_approves(event) is False
    harmless = _edit_frame_with(
        {"fileName": dest, "diff": _move_diff(str(tmp_path / "old.txt"), dest)},
        kind=kind,
        locations=[{"path": dest}],
    )
    assert await _always_on_gate_approves(_event_for(tmp_path, harmless)) is True


@pytest.mark.parametrize("cache", ["_tool_call_is_shell", "_tool_call_mcp_server"])
def test_a_shell_or_mcp_call_is_not_seeded(tmp_path, cache) -> None:
    frame = _edit_frame_with({"fileName": "/w/x.txt", "diff": "+x"})
    call_id = frame["params"]["toolCall"]["toolCallId"]
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    getattr(client, cache)[call_id] = True if cache == "_tool_call_is_shell" else "srv"
    client._seed_copilot_edit_targets(
        JsonRpcMessage(id=frame["id"], method=frame["method"], params=frame["params"])
    )
    assert client._tool_call_params == {} and client._tool_call_diff_path == {}


def test_a_cached_content_block_path_stays_a_target(tmp_path) -> None:
    frame = _edit_frame_with({"fileName": "/w/a.txt", "diff": "+a"})
    call_id = frame["params"]["toolCall"]["toolCallId"]
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    client._tool_call_diff_path[call_id] = "/w/block.txt"
    for _ in range(2):
        event = client._build_permission_event(
            JsonRpcMessage(id=frame["id"], method=frame["method"], params=frame["params"])
        )
        assert event.raw_tool_params["path"] == ["/w/block.txt", "/w/a.txt"]


@pytest.mark.parametrize(
    "kwargs, options",
    [
        ({"always": True}, {"once": "allow_once", "always": "allow_always"}),
        ({"option_id": "allow_always"}, {"once": "allow_once", "always": "allow_always"}),
        ({}, {"once": "allow_always", "always": "allow_always"}),
    ],
)
@pytest.mark.asyncio
async def test_allow_always_is_answered_as_allow_once(tmp_path, kwargs, options) -> None:
    """Copilot's allow-always would stop it asking, so no later file would be judged."""
    from kiro_crew.acp.types import OUTCOME_SELECTED

    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    frame = _edit_frame_with({"fileName": str(tmp_path / "a.txt"), "diff": "+a"})
    client._permission_gate_events[42] = client._build_permission_event(
        JsonRpcMessage(id=42, method=frame["method"], params=frame["params"])
    )
    client._permission_options[42] = options
    client._send_response = AsyncMock()
    await client.approve_tool(42, **kwargs)
    client._send_response.assert_awaited_once_with(
        42, {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": "allow_once"}}
    )


def test_seeding_merges_into_the_tool_calls_own_params(tmp_path) -> None:
    frame = _edit_frame_with({"fileName": "/w/a.txt", "diff": "+a"})
    call_id = frame["params"]["toolCall"]["toolCallId"]
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    client._tool_call_params[call_id] = {"url": "https://example.invalid/x", "path": "rel.txt"}
    seen = []
    for name in ("/w/a.txt", "/w/b.txt"):
        params = json.loads(json.dumps(frame["params"]))
        params["toolCall"]["rawInput"]["fileName"] = name
        event = client._build_permission_event(
            JsonRpcMessage(id=frame["id"], method=frame["method"], params=params)
        )
        seen.append(event.raw_tool_params)
    base = {"url": "https://example.invalid/x"}
    assert seen == [
        {**base, "path": ["/w/a.txt", "rel.txt"]},
        {**base, "path": ["/w/b.txt", "rel.txt"]},
    ]


def test_seeding_keeps_every_path_of_a_cached_path_list(tmp_path) -> None:
    """A ``tool_call`` frame that cached a LIST of paths keeps each one, after the seed."""
    frame = _edit_frame_with({"fileName": "/w/a.txt", "diff": "+a"})
    call_id = frame["params"]["toolCall"]["toolCallId"]
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    client._tool_call_params[call_id] = {"path": ["rel.txt", "/w/c.txt"]}
    event = client._build_permission_event(
        JsonRpcMessage(id=frame["id"], method=frame["method"], params=frame["params"])
    )
    assert event.raw_tool_params == {"path": ["/w/a.txt", "rel.txt", "/w/c.txt"]}


@pytest.mark.parametrize(
    "raw",
    [
        {"fileName": "rel/x.txt", "diff": "+x"},
        {"fileName": "/w/x.txt", "diff": '--- "a/w/q x.txt"\n+++ b/w/x.txt\n@@\n'},
        {"fileName": "/w/x.txt", "diff": "--- /w/y.txt\n+++ b/w/x.txt\n@@\n"},
        {"fileName": "/w/x.txt", "diff": "rename from w/y.txt\nrename to w/x.txt\n"},
        {"fileName": "/w/x.txt", "diff": "--- a/w/y\u2028z.txt\n+++ b/w/x.txt\n@@\n"},
        {"fileName": "/w/x.txt", "diff": "---\ta/w/y.txt\n+++ b/w/x.txt\n@@\n"},
        {"fileName": "/w/x.txt", "path": "rel/y.txt"},
    ],
)
def test_an_unreadable_target_is_denied_as_unanchored(tmp_path, raw) -> None:
    from kiro_crew.platform.tool_paths import edit_target_candidates

    event = _event_for(tmp_path, _edit_frame_with(raw, locations=[{"path": "/w/x.txt"}]))
    assert event.diff_path == "copilot-edit-target-unresolved"
    assert edit_target_candidates(
        event.raw_tool_params, event.diff_path, tool_kind="edit"
    ).unanchored


def test_a_nested_path_spelling_is_a_target(tmp_path) -> None:
    raw = {"fileName": "/w/x.txt", "edits": [{"filePath": "/w/nested.txt"}]}
    event = _event_for(tmp_path, _edit_frame_with(raw))
    assert "/w/nested.txt" in event.raw_tool_params["path"]


def test_a_copilot_delete_keeps_its_caution_badge() -> None:
    from kiro_crew.platform.tool_paths import is_workspace_edit_call

    assert not is_workspace_edit_call("delete", "/w/x.txt")
    assert not is_workspace_edit_call("move", "/w/x.txt")
    assert is_workspace_edit_call("", "/w/x.txt") and is_workspace_edit_call("other", "/w/x.txt")


def test_a_relative_location_is_unresolved(tmp_path) -> None:
    frame = _edit_frame_with({"fileName": "/w/x.txt", "diff": "+x"}, locations=[{"path": "r.txt"}])
    assert _event_for(tmp_path, frame).diff_path == "copilot-edit-target-unresolved"


def test_header_parsing_reads_headers_only_and_both_tab_spellings(tmp_path) -> None:
    diff = (
        "diff --git a/w/s p.txt b/w/s p.txt\n--- a/w/s p.txt\t\n+++ b/w/s p.txt\t\n"
        "@@ -1,2 +1,2 @@\n--- a/w/body-removed.txt\n+++ b/w/body-added.txt\n"
    )
    event = _event_for(tmp_path, _edit_frame_with({"fileName": "/w/other.txt", "diff": diff}))
    assert event.raw_tool_params["path"] == ["/w/other.txt", "/w/s p.txt\t", "/w/s p.txt"]


def test_a_windows_drive_header_keeps_its_drive() -> None:
    from kiro_crew.acp.client_copilot import _copilot_diff_header_paths

    assert _copilot_diff_header_paths("--- a/C:/w/x.txt\n+++ b/C:/w/y.txt\n@@\n") == (
        ["C:/w/x.txt", "C:/w/y.txt"],
        False,
    )


# ── MCP identity is recovered from the tool_call title ───────────────────────


def _mcp_frames() -> tuple[dict, dict]:
    frames = _frames("mcp-stdio-mount-live.jsonl")
    tool_call = next(
        f
        for f in frames
        if f.get("method") == "session/update"
        and f["params"]["update"].get("sessionUpdate") == "tool_call"
        and f["params"]["update"].get("title") == "probe-marker"
    )
    permission = next(
        f
        for f in frames
        if f.get("method") == "session/request_permission"
        and f["params"]["toolCall"].get("title") == "marker"
    )
    return tool_call, permission


def _client_with_placed(tmp_path, names, monkeypatch):
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    monkeypatch.setattr(client, "_session_mcp_servers", lambda: [{"name": n} for n in names])
    return client


def _feed(client, frame):
    return client._extract_tool_event(
        JsonRpcMessage(method=frame["method"], params=frame["params"])
    )


def test_an_mcp_call_carries_its_server_to_the_gate(tmp_path, monkeypatch) -> None:
    """A ceiling about ``@probe/marker`` can only bind when the event names the server."""
    tool_call, permission = _mcp_frames()
    client = _client_with_placed(tmp_path, ["kirocrew-core", "probe"], monkeypatch)
    _feed(client, tool_call)
    event = client._build_permission_event(
        JsonRpcMessage(
            id=permission["id"], method=permission["method"], params=permission["params"]
        )
    )
    assert (event.mcp_server_name, event.tool_name) == ("probe", "marker")
    assert event.mcp_identity_trusted


def test_the_tool_call_event_carries_the_recovered_identity(tmp_path, monkeypatch) -> None:
    """The turn loop reads the EVENT, not the caches, to resolve a directive."""
    tool_call, _ = _mcp_frames()
    client = _client_with_placed(tmp_path, ["kirocrew-core", "probe"], monkeypatch)
    event = _feed(client, tool_call)
    assert (event.mcp_server_name, event.tool_name) == ("probe", "marker")
    assert event.mcp_identity_trusted is True
    assert event.tool_identity_trusted is True


def test_a_copilot_directive_call_resolves_to_its_directive(tmp_path, monkeypatch) -> None:
    """``kirocrew-core-reset_conversation`` is the only spelling Copilot sends for it.

    ``directive_tool_from_call`` cannot read that hyphenated wire title, so without the
    recovered identity on the event no directive or input digest is recorded and the
    tool completes without resetting anything.
    """
    from kiro_crew import session_directive

    tool_call, _ = _mcp_frames()
    tool_call = json.loads(json.dumps(tool_call))
    tool_call["params"]["update"]["title"] = "kirocrew-core-reset_conversation"
    client = _client_with_placed(tmp_path, ["kirocrew-core", "probe"], monkeypatch)
    event = _feed(client, tool_call)
    assert session_directive.directive_tool_from_call("", "", event.wire_title) == ""
    assert (
        session_directive.directive_tool_from_call(
            event.mcp_server_name, event.tool_name, event.wire_title
        )
        == "reset_conversation"
    )
    assert session_directive.directive_tool_for(event.mcp_server_name, event.tool_name)


@pytest.mark.parametrize(
    ("title", "placed"),
    [("probe-marker", ["other"]), ("probe-m-arker", ["probe", "probe-m"])],
    ids=["unmatched", "ambiguous"],
)
def test_an_unattributed_title_leaves_the_event_unidentified(
    tmp_path, monkeypatch, title, placed
) -> None:
    tool_call, _ = _mcp_frames()
    tool_call = json.loads(json.dumps(tool_call))
    tool_call["params"]["update"]["title"] = title
    client = _client_with_placed(tmp_path, placed, monkeypatch)
    event = _feed(client, tool_call)
    assert (event.mcp_server_name, event.tool_name) == ("", "")
    assert not event.mcp_identity_trusted


def test_a_title_matching_no_placed_server_is_left_alone(tmp_path, monkeypatch) -> None:
    tool_call, _ = _mcp_frames()
    client = _client_with_placed(tmp_path, ["other"], monkeypatch)
    _feed(client, tool_call)
    call_id = tool_call["params"]["update"]["toolCallId"]
    assert not client._tool_call_mcp_server.get(call_id)
    assert call_id not in client._copilot_ambiguous_mcp_calls


@pytest.mark.asyncio
async def test_a_title_two_placed_servers_could_produce_is_refused(tmp_path, monkeypatch) -> None:
    """``probe`` serving ``m-arker`` and ``probe-m`` serving ``arker`` share one title."""
    tool_call, permission = _mcp_frames()
    tool_call = json.loads(json.dumps(tool_call))
    tool_call["params"]["update"]["title"] = "probe-m-arker"
    client = _client_with_placed(tmp_path, ["probe", "probe-m"], monkeypatch)
    _feed(client, tool_call)
    call_id = tool_call["params"]["update"]["toolCallId"]
    assert call_id in client._copilot_ambiguous_mcp_calls
    assert not client._tool_call_mcp_server.get(call_id)
    event = client._build_permission_event(
        JsonRpcMessage(
            id=permission["id"], method=permission["method"], params=permission["params"]
        )
    )
    client.reject_tool = AsyncMock()
    client._audit_spec_restriction = MagicMock()
    assert await client._refuse_identity_drift(event) is True
    client.reject_tool.assert_awaited_once_with(event.request_id)


@pytest.mark.asyncio
async def test_an_unambiguous_mcp_call_is_not_refused(tmp_path, monkeypatch) -> None:
    tool_call, permission = _mcp_frames()
    client = _client_with_placed(tmp_path, ["probe"], monkeypatch)
    _feed(client, tool_call)
    event = client._build_permission_event(
        JsonRpcMessage(
            id=permission["id"], method=permission["method"], params=permission["params"]
        )
    )
    client.reject_tool = AsyncMock()
    assert await client._refuse_identity_drift(event) is False


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_a_symlinked_git_pointer_is_never_opened(tmp_path, monkeypatch) -> None:
    """A workspace ``.git`` link could name a credential file the gateway can read."""
    real = tmp_path / "real.git"
    real.mkdir()
    target = tmp_path / "pointer"
    target.write_text(f"gitdir: {real}\n")
    work = tmp_path / "work"
    work.mkdir()
    try:
        (work / ".git").symlink_to(target)
    except OSError:
        pytest.skip("symlink not permitted")
    opened: list[str] = []
    real_open = os.open
    monkeypatch.setattr(
        copilot_mod.os, "open", lambda p, *a, **k: (opened.append(str(p)), real_open(p, *a, **k))[1]
    )
    assert copilot_mod._is_git_root(str(work)) is False
    assert opened == []


def test_a_sensitive_git_pointer_is_never_opened(tmp_path, monkeypatch) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").write_text(f"gitdir: {tmp_path}\n")
    monkeypatch.setattr(copilot_mod, "is_sensitive_path", lambda p: True)
    opened: list[str] = []
    real_open = os.open
    monkeypatch.setattr(
        copilot_mod.os, "open", lambda p, *a, **k: (opened.append(str(p)), real_open(p, *a, **k))[1]
    )
    assert copilot_mod._is_git_root(str(work)) is False
    assert opened == []


def test_an_oversized_git_file_is_not_a_pointer(tmp_path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").write_text(f"gitdir: {tmp_path}\n" + "x" * 5000)
    assert copilot_mod._is_git_root(str(work)) is False


@pytest.mark.parametrize("version", ["1.0.200", "1.0.99", ""])
def test_an_unverified_copilot_release_is_refused_at_the_handshake(tmp_path, version) -> None:
    """The list-based guards fail open on another release, so the session is refused."""
    from kiro_crew.acp import client_copilot as cp

    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    client._agent_version = version
    client._agent_version_read = True
    with pytest.raises(cp.AcpToolGateUnroutable) as raised:
        client._refuse_unverified_copilot_version()
    assert raised.value.transient is False and raised.value.user_worded is True
    message = str(raised.value)
    assert f"npm i -g @github/copilot@{cp.COPILOT_VERIFIED_VERSION}" in message
    # UX: two sentences a chat reader can act on -- what happened, then the fix --
    # and none of the guard internals (those belong to the refusal's docstring).
    assert message.count(". ") == 1 and message.endswith(
        f"start a new chat: npm i -g @github/copilot@{cp.COPILOT_VERIFIED_VERSION}"
    )
    assert "refused" in message.split(". ")[0]
    for jargon in ("project-config", "allowlist", "environment variables"):
        assert jargon not in message
    if version:
        assert f"Copilot CLI {version} " in message


def test_the_verified_copilot_release_starts(tmp_path) -> None:
    from kiro_crew.acp import client_copilot as cp

    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    client._agent_version = cp.COPILOT_VERIFIED_VERSION
    client._agent_version_read = True
    client._refuse_unverified_copilot_version()


def test_the_version_refusal_is_copilots_alone(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=acp_backends.ACP_BACKEND_GOOSE)
    client._agent_version = "9.9.9"
    client._agent_version_read = True
    client._refuse_unverified_copilot_version()


def test_a_copilot_session_judges_every_permission_request(tmp_path) -> None:
    """The auto-approve site must run the identity refusal too."""
    assert AcpClient(work_dir=tmp_path, acp_backend=COPILOT)._judges_permission_requests


def test_only_an_other_kind_call_gains_an_mcp_identity(tmp_path, monkeypatch) -> None:
    tool_call, _ = _mcp_frames()
    tool_call = json.loads(json.dumps(tool_call))
    tool_call["params"]["update"]["kind"] = "fetch"
    client = _client_with_placed(tmp_path, ["probe"], monkeypatch)
    _feed(client, tool_call)
    assert not client._tool_call_mcp_server.get(tool_call["params"]["update"]["toolCallId"])


@pytest.mark.asyncio
async def test_a_permission_frame_naming_another_tool_is_refused(tmp_path, monkeypatch) -> None:
    tool_call, permission = _mcp_frames()
    permission = json.loads(json.dumps(permission))
    permission["params"]["toolCall"]["title"] = "something-else"
    client = _client_with_placed(tmp_path, ["probe"], monkeypatch)
    _feed(client, tool_call)
    event = client._build_permission_event(
        JsonRpcMessage(
            id=permission["id"], method=permission["method"], params=permission["params"]
        )
    )
    assert not event.mcp_server_name
    client.reject_tool = AsyncMock()
    client._audit_spec_restriction = MagicMock()
    assert await client._refuse_identity_drift(event) is True


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_git_is_never_opened(tmp_path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    os.mkfifo(work / ".git")
    assert copilot_mod._is_git_root(str(work)) is False


def test_a_hard_linked_git_pointer_is_never_opened(tmp_path) -> None:
    real = tmp_path / "real.git"
    real.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text(f"gitdir: {real}\n")
    work = tmp_path / "work"
    work.mkdir()
    try:
        os.link(elsewhere, work / ".git")
    except OSError:
        pytest.skip("hard links not permitted")
    assert copilot_mod._is_git_root(str(work)) is False
    os.remove(elsewhere)
    assert copilot_mod._is_git_root(str(work)) is True


@pytest.mark.asyncio
async def test_a_served_tool_under_an_unplaced_server_spelling_is_refused(
    tmp_path, monkeypatch
) -> None:
    """``Probe-marker`` asked about as ``marker``: served, but its server matches no placed name."""
    tool_call, permission = _mcp_frames()
    tool_call = json.loads(json.dumps(tool_call))
    tool_call["params"]["update"]["title"] = "Probe_x-marker"
    client = _client_with_placed(tmp_path, ["probe"], monkeypatch)
    _feed(client, tool_call)
    event = client._build_permission_event(
        JsonRpcMessage(
            id=permission["id"], method=permission["method"], params=permission["params"]
        )
    )
    client.reject_tool = AsyncMock()
    client._audit_spec_restriction = MagicMock()
    assert await client._refuse_identity_drift(event) is True


@pytest.mark.asyncio
async def test_a_built_in_with_no_server_shape_is_not_refused(tmp_path, monkeypatch) -> None:
    tool_call, permission = _mcp_frames()
    tool_call = json.loads(json.dumps(tool_call))
    tool_call["params"]["update"]["title"] = "Using skill: marker"
    client = _client_with_placed(tmp_path, ["probe"], monkeypatch)
    _feed(client, tool_call)
    event = client._build_permission_event(
        JsonRpcMessage(
            id=permission["id"], method=permission["method"], params=permission["params"]
        )
    )
    client.reject_tool = AsyncMock()
    assert await client._refuse_identity_drift(event) is False


def test_the_sign_in_remedy_gives_only_actions() -> None:
    """UX: the remedy says what to run; how the harness reads it is not the reader's."""
    from kiro_crew.agent_sdk.host_auth import declaration_for

    remedy = declaration_for(COPILOT).sign_in_remedy
    assert "copilot login" in remedy and "gh auth login" in remedy
    assert "Neither is checked" not in remedy and "the harness reads" not in remedy


def test_the_install_hint_pins_the_verified_release() -> None:
    """A new install lands on the release the defences were measured on."""
    from kiro_crew.acp import client_copilot as cp
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_LAUNCH, COPILOT_VERIFIED_VERSION

    record = ACP_BACKEND_LAUNCH[COPILOT]
    assert record.install_command == f"npm i -g @github/copilot@{COPILOT_VERIFIED_VERSION}"
    assert cp.COPILOT_VERIFIED_VERSION is COPILOT_VERIFIED_VERSION


def test_an_edit_seed_is_not_retained_past_its_event(tmp_path) -> None:
    """GPT: 300 patches in a turn must not grow the params caches by 300 entries."""
    frame = _edit_permission_frame("mcp-stdio-mount-live.jsonl")
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    client._tool_call_params["kept"] = {"path": "/w/kept.txt"}
    for n in range(300):
        params = json.loads(json.dumps(frame["params"]))
        params["toolCall"]["toolCallId"] = f"call-{n}"
        params["toolCall"]["rawInput"]["fileName"] = f"/w/{n}.txt"
        event = client._build_permission_event(
            JsonRpcMessage(id=n, method=frame["method"], params=params)
        )
        assert event.raw_tool_params["path"][0] == f"/w/{n}.txt"
    assert client._tool_call_params == {"kept": {"path": "/w/kept.txt"}}
    assert client._tool_call_diff_path == {}


@pytest.mark.asyncio
async def test_identity_records_are_bounded_and_overflow_refuses(tmp_path, monkeypatch) -> None:
    from kiro_crew.acp.client_tool_events import (
        COPILOT_IDENTITY_OVERFLOW,
        COPILOT_IDENTITY_RECORDS_MAX,
    )

    tool_call, permission = _mcp_frames()
    client = _client_with_placed(tmp_path, ["probe", "probe-m"], monkeypatch)
    for n in range(COPILOT_IDENTITY_RECORDS_MAX + 5):
        frame = json.loads(json.dumps(tool_call))
        frame["params"]["update"]["toolCallId"] = f"call-{n}"
        frame["params"]["update"]["title"] = "probe-m-arker"
        _feed(client, frame)
    assert len(client._copilot_ambiguous_mcp_calls) == COPILOT_IDENTITY_RECORDS_MAX + 1
    assert COPILOT_IDENTITY_OVERFLOW in client._copilot_ambiguous_mcp_calls
    # Overflowed: even a call never recorded is refused this turn.
    clean = _client_with_placed(tmp_path, ["probe"], monkeypatch)
    clean._copilot_ambiguous_mcp_calls = client._copilot_ambiguous_mcp_calls
    _feed(clean, tool_call)
    event = clean._build_permission_event(
        JsonRpcMessage(
            id=permission["id"], method=permission["method"], params=permission["params"]
        )
    )
    clean.reject_tool = AsyncMock()
    clean._audit_spec_restriction = MagicMock()
    assert await clean._refuse_identity_drift(event) is True


def test_the_version_refusal_runs_at_the_handshake() -> None:
    """After the version is read and before any session exists (goose's precedent)."""
    import inspect

    source = inspect.getsource(AcpClient._initialize_session)
    read = source.index("self._agent_version = agent_version_from_init(init_resp)")
    refuse = source.index("self._refuse_unverified_copilot_version()")
    assert read < refuse < source.index("# 2. Try session/load")


def test_the_verified_version_is_the_recorded_handshake() -> None:
    from kiro_crew.acp import client_copilot as cp

    reported = {
        f["result"]["agentInfo"]["version"]
        for name in ("mcp-stdio-mount-live.jsonl",)
        for f in _frames(name)
        if isinstance(f.get("result"), dict) and "agentInfo" in f["result"]
    }
    assert reported == {cp.COPILOT_VERIFIED_VERSION}


@pytest.mark.asyncio
async def test_an_over_long_server_shaped_title_is_refused(tmp_path, monkeypatch) -> None:
    from kiro_crew.acp.harness.copilot import COPILOT_TITLE_MAX_CHARS

    tool_call, permission = _mcp_frames()
    tool_call = json.loads(json.dumps(tool_call))
    tool_call["params"]["update"]["title"] = "x" * COPILOT_TITLE_MAX_CHARS + "-marker"
    client = _client_with_placed(tmp_path, ["probe"], monkeypatch)
    _feed(client, tool_call)
    # Bounded: the over-long title itself is not kept.
    assert not client._copilot_unmatched_titles
    event = client._build_permission_event(
        JsonRpcMessage(
            id=permission["id"], method=permission["method"], params=permission["params"]
        )
    )
    client.reject_tool = AsyncMock()
    client._audit_spec_restriction = MagicMock()
    assert await client._refuse_identity_drift(event) is True


def test_a_finished_call_releases_its_identity_records(tmp_path, monkeypatch) -> None:
    tool_call, _ = _mcp_frames()
    tool_call = json.loads(json.dumps(tool_call))
    tool_call["params"]["update"]["title"] = "elsewhere-marker"
    client = _client_with_placed(tmp_path, ["probe"], monkeypatch)
    _feed(client, tool_call)
    call_id = tool_call["params"]["update"]["toolCallId"]
    assert call_id in client._copilot_unmatched_titles
    update = {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": "completed"}
    client._extract_tool_call_update(
        JsonRpcMessage(method="session/update", params={"sessionId": "s", "update": update})
    )
    assert call_id not in client._copilot_unmatched_titles


def test_the_seed_restore_keeps_whatever_was_cached(tmp_path) -> None:
    frame = _edit_frame_with({"fileName": "/w/a.txt", "diff": "+a"})
    call_id = frame["params"]["toolCall"]["toolCallId"]
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    odd = ["not", "a", "str"]
    client._tool_call_diff_path[call_id] = odd  # type: ignore[assignment]
    client._build_permission_event(
        JsonRpcMessage(id=frame["id"], method=frame["method"], params=frame["params"])
    )
    assert client._tool_call_diff_path[call_id] is odd


@pytest.mark.asyncio
async def test_an_unverified_release_is_refused_once_without_a_respawn(tmp_path) -> None:
    """A fresh process reports the same version, so the refusal must not be retried."""
    from kiro_crew.acp import client_copilot as cp

    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    spawns = {"n": 0}

    async def _spawn():
        spawns["n"] += 1
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdin.drain = AsyncMock()
        client._process = proc

    async def _init():
        client._agent_version = "1.0.200"
        client._agent_version_read = True
        client._refuse_unverified_copilot_version()

    def _reset():
        client._process = None

    client._spawn = _spawn
    client._initialize_session = _init
    client._snapshot_process_tree = AsyncMock()
    client._kill_process = AsyncMock()
    client._reset_state = _reset
    with pytest.raises(cp.AcpToolGateUnroutable):
        await client.ensure_ready()
    assert spawns["n"] == 1
    assert client._kill_process.await_count == 1


@pytest.mark.asyncio
async def test_a_reask_of_an_unplaced_served_tool_is_refused_again(tmp_path, monkeypatch) -> None:
    tool_call, permission = _mcp_frames()
    tool_call = json.loads(json.dumps(tool_call))
    tool_call["params"]["update"]["title"] = "Probe_x-marker"
    client = _client_with_placed(tmp_path, ["probe"], monkeypatch)
    _feed(client, tool_call)
    client.reject_tool = AsyncMock()
    client._audit_spec_restriction = MagicMock()
    # A first frame naming something else leaves the record for the re-ask that
    # names the bare tool, which must still be refused.
    first = json.loads(json.dumps(permission["params"]))
    first["toolCall"]["title"] = "unrelated"
    client._build_permission_event(
        JsonRpcMessage(id=permission["id"], method=permission["method"], params=first)
    )
    event = client._build_permission_event(
        JsonRpcMessage(
            id=permission["id"], method=permission["method"], params=permission["params"]
        )
    )
    assert await client._refuse_identity_drift(event) is True


@pytest.mark.parametrize(
    "text",
    [
        "/yolo\ufeff",
        "/yolo\ufeffon",
        "/allow-all\ufeff on",
        "/yolo\u200b",
        "/yolo\u00e9",
        "/compact\u200b",
        "/\ufeffyolo on",
        "/\u200byolo",
    ],
)
def test_a_command_name_ended_by_a_character_the_runtimes_disagree_on_is_refused(text) -> None:
    """Opus: Python's split() keeps U+FEFF in the word, Copilot's JS splits on it."""
    assert prompt_command_issue(COPILOT, [text])


@pytest.mark.parametrize("text", ["/yolo\u00a0on", "/compact\ufeff"])
def test_unicode_whitespace_ends_the_name_as_the_harness_reads_it(text) -> None:
    issue = prompt_command_issue(COPILOT, [text])
    assert bool(issue) is text.startswith("/yolo")


@pytest.mark.parametrize("text", ["/yolo.", "/etc/hosts", "/compact:x"])
def test_a_path_shaped_slash_is_still_not_a_command(text) -> None:
    assert prompt_command_issue(COPILOT, [text]) == ""


@pytest.mark.parametrize("title", ["probe-m-arker", "elsewhere-marker"])
def test_an_over_long_tool_call_id_is_never_stored(tmp_path, monkeypatch, title) -> None:
    from kiro_crew.acp._dispatch_parts.tool_call_provenance import TOOL_CALL_CACHE_MAX_KEY_CHARS
    from kiro_crew.acp.harness.copilot import COPILOT_IDENTITY_OVERFLOW

    tool_call, _ = _mcp_frames()
    client = _client_with_placed(tmp_path, ["probe", "probe-m"], monkeypatch)
    long_id = "x" * (TOOL_CALL_CACHE_MAX_KEY_CHARS + 1)
    frame = json.loads(json.dumps(tool_call))
    frame["params"]["update"]["title"] = title
    client._recover_copilot_mcp_identity(long_id, frame["params"]["update"])
    assert client._copilot_ambiguous_mcp_calls == {COPILOT_IDENTITY_OVERFLOW}
    assert client._copilot_unmatched_titles == {}
