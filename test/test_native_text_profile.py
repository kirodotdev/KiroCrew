"""Native text profile: the text-only, tool-less kiro-cli profile a scoped job runs under.

Everything here is host-enforced: private directories and files from the first
byte, tamper detection before every prompt, a scrubbed environment, an ACP
policy that permits exactly one prompt and no tools, and a receipt only when
all of that held. The runtime handshake advertises no client capabilities for
these jobs through ``NativeTextHarness``.
"""
from __future__ import annotations

import hashlib
import os
import stat

import pytest

from kiro_crew.acp import native_text_profile as ntp

SCOPE = "appjob:" + "a" * 64
PROMPT = "Return only the plan for the change."


@pytest.fixture
def profile():
    p = ntp.NativeTextProfile(SCOPE, PROMPT)
    yield p
    if not p.closed:
        p.close(processes_exited=True)


def _mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


# ── construction and the private tree ───────────────────────────────────────


def test_invalid_scope_or_prompt_is_refused():
    with pytest.raises(ntp.NativeTextError, match="scope"):
        ntp.NativeTextProfile("appjob:short", PROMPT)
    with pytest.raises(ntp.NativeTextError, match="prompt"):
        ntp.NativeTextProfile(SCOPE, "")
    with pytest.raises(ntp.NativeTextError, match="prompt"):
        ntp.NativeTextProfile(SCOPE, "x" * 120001)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_profile_tree_is_owner_only_and_the_roster_is_one_private_agent(profile):
    for directory in (profile.root, profile.home, profile.cwd, profile.temp,
                      profile.home / "agents", profile.home / "settings"):
        assert _mode(directory) == 0o700, directory
    agents = list((profile.home / "agents").iterdir())
    assert len(agents) == 1
    assert agents[0].name == profile.agent + ".json"
    for path in profile._files:
        assert _mode(path) == 0o600, path
    assert list(profile.cwd.iterdir()) == []
    profile.verify_files()  # the freshly built tree passes its own check


def test_tampering_is_detected_before_use(profile):
    agent_file = profile.home / "agents" / (profile.agent + ".json")
    original = agent_file.read_bytes()
    agent_file.write_bytes(original.replace(b'"tools":[]', b'"tools":["fs_read"]'))
    with pytest.raises(ntp.NativeTextError, match="configuration changed"):
        profile.verify_files()
    agent_file.write_bytes(original)
    profile.verify_files()

    (profile.home / "agents" / "shared.json").write_bytes(b"{}")
    with pytest.raises(ntp.NativeTextError, match="roster changed"):
        profile.verify_files()
    (profile.home / "agents" / "shared.json").unlink()

    (profile.cwd / "planted.txt").write_text("x")
    with pytest.raises(ntp.NativeTextError, match="not empty"):
        profile.verify_files()
    (profile.cwd / "planted.txt").unlink()

    if os.name == "posix":
        os.chmod(profile.cwd, 0o755)
        with pytest.raises(ntp.NativeTextError, match="not private"):
            profile.verify_files()
        os.chmod(profile.cwd, 0o700)
    profile.verify_files()


# ── environment ─────────────────────────────────────────────────────────────


def test_environment_is_an_allowlist_plus_private_homes(profile):
    inherited = {
        "HOME": "/srv/example-home", "PATH": "/usr/bin", "LANG": "C.UTF-8",
        "AWS_SECRET_ACCESS_KEY": "nope", "GITHUB_TOKEN": "nope",
        "KIRO_API_KEY": "k", "SOME_RANDOM": "x",
    }
    env = profile.environment(inherited)
    assert env["PATH"] == "/usr/bin" and env["LANG"] == "C.UTF-8"
    assert env["KIRO_HOME"] == str(profile.home)
    assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == str(profile.temp)
    assert env["KIRO_API_KEY"] == "k"  # the host's own auth injector key is kept
    for leaked in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "SOME_RANDOM"):
        assert leaked not in env


# ── session binding and the ACP policy ──────────────────────────────────────


def _handshake(profile):
    profile.bind("subagent:one")
    profile.validate_factory("subagent:one", str(profile.cwd), "kiro")
    profile.launch_started = True
    profile.version = next(iter(ntp.SUPPORTED_CLI))
    profile.mark_process_launch()
    profile.outbound("initialize", {})
    profile.initialized(profile.version, "proc-1")
    profile.outbound("session/new", {"cwd": str(profile.cwd), "mcpServers": []})
    profile.confirm_session("sess-1", [profile.agent, "default"], profile.agent, True)


def test_bind_requires_a_fresh_dedicated_subagent_session(profile):
    with pytest.raises(ntp.NativeTextError, match="dedicated"):
        profile.bind("owner-chat:1")
    profile.bind("subagent:one")
    with pytest.raises(ntp.NativeTextError, match="dedicated"):
        profile.bind("subagent:two")
    with pytest.raises(ntp.NativeTextError, match="scope mismatch"):
        profile.validate_factory("subagent:two", str(profile.cwd), "kiro")
    with pytest.raises(ntp.NativeTextError, match="scope mismatch"):
        profile.validate_factory("subagent:one", "/somewhere/else", "kiro")
    with pytest.raises(ntp.NativeTextError, match="backend"):
        profile.validate_factory("subagent:one", str(profile.cwd), "kas")


def test_handshake_order_and_isolated_session_parameters(profile):
    profile.bind("subagent:one")
    with pytest.raises(ntp.NativeTextError, match="not validated"):
        profile.mark_process_launch()
    profile.launch_started = True
    profile.version = next(iter(ntp.SUPPORTED_CLI))
    profile.mark_process_launch()
    with pytest.raises(ntp.NativeTextError, match="version"):
        profile.initialized("0.0.1", "proc-1")
    profile.initialized(profile.version, "proc-1")
    with pytest.raises(ntp.NativeTextError, match="reinitialize"):
        profile.outbound("initialize", {})
    with pytest.raises(ntp.NativeTextError, match="not isolated"):
        profile.outbound("session/new", {"cwd": "/tmp", "mcpServers": []})
    with pytest.raises(ntp.NativeTextError, match="not isolated"):
        profile.outbound("session/new", {"cwd": str(profile.cwd), "mcpServers": [{"name": "x"}]})
    profile.outbound("session/new", {"cwd": str(profile.cwd), "mcpServers": []})
    with pytest.raises(ntp.NativeTextError, match="reuse"):
        profile.outbound("session/new", {"cwd": str(profile.cwd), "mcpServers": []})
    with pytest.raises(ntp.NativeTextError, match="private agent mode"):
        profile.confirm_session("sess-1", ["default"], "default", True)
    profile.confirm_session("sess-1", [profile.agent], profile.agent, True)
    assert profile.ready


def test_exactly_one_prompt_matching_the_scoped_request(profile):
    _handshake(profile)
    good = {"sessionId": "sess-1", "prompt": [{"type": "text", "text": PROMPT}]}
    with pytest.raises(ntp.NativeTextError, match="differs"):
        profile.outbound("session/prompt", {"sessionId": "sess-1",
                                            "prompt": [{"type": "text", "text": "something else"}]})
    with pytest.raises(ntp.NativeTextError, match="differs"):
        profile.outbound("session/prompt", {"sessionId": "sess-1", "prompt": [
            {"type": "text", "text": PROMPT}, {"type": "text", "text": PROMPT}]})
    profile.outbound("session/prompt", good)
    assert profile.prompt_requests == 1
    with pytest.raises(ntp.NativeTextError, match="differs"):
        profile.outbound("session/prompt", good)  # a second prompt is refused
    with pytest.raises(ntp.NativeTextError, match="after prompt"):
        profile.outbound("session/set_model", {"sessionId": "sess-1", "modelId": "m"})


def test_forbidden_operations_are_refused(profile):
    _handshake(profile)
    with pytest.raises(ntp.NativeTextError, match="agent switch"):
        profile.outbound("session/set_mode", {"sessionId": "sess-1", "modeId": "default"})
    with pytest.raises(ntp.NativeTextError, match="option is forbidden"):
        profile.outbound("session/set_config_option",
                         {"sessionId": "sess-1", "configId": "permission_mode", "value": "yolo"})
    with pytest.raises(ntp.NativeTextError, match="operation is forbidden"):
        profile.outbound("fs/read_text_file", {"sessionId": "sess-1", "path": "/etc/passwd"})
    with pytest.raises(ntp.NativeTextError, match="not ready"):
        profile.outbound("session/prompt", {"sessionId": "other", "prompt": []})
    with pytest.raises(ntp.NativeTextError, match="cleanup session mismatch"):
        profile.outbound("session/cancel", {"sessionId": "other"})
    profile.outbound("session/cancel", {"sessionId": "sess-1"})  # own session: allowed


def test_inbound_tool_activity_or_permission_request_invalidates_isolation(profile):
    _handshake(profile)
    assert profile.observe_inbound("_kiro.dev/subagent/list_update",
                                   {"subagents": [], "pendingStages": []}) is False
    assert profile.observe_inbound("session/update",
                                   {"update": {"sessionUpdate": "agent_message_chunk"}}) is False
    assert profile.observe_inbound("session/update",
                                   {"update": {"sessionUpdate": "tool_call"}}) is True
    assert profile.failed and not profile.ready
    assert profile.receipt() is None

    fresh = ntp.NativeTextProfile("appjob:" + "b" * 64, PROMPT)
    try:
        _handshake(fresh)
        assert fresh.observe_inbound("session/request_permission", {}) is True
        assert fresh.observe_inbound.__self__.failed
    finally:
        fresh.close(processes_exited=True)


def test_receipt_only_after_one_prompt_under_intact_isolation(profile):
    _handshake(profile)
    assert profile.receipt() is None  # no prompt yet
    profile.outbound("session/prompt",
                     {"sessionId": "sess-1", "prompt": [{"type": "text", "text": PROMPT}]})
    receipt = profile.receipt()
    assert receipt["policy"] == "kiro-v2-private-text-v1"
    assert receipt["tools"] == [] and receipt["mcp_servers"] == []
    assert receipt["prompt_requests"] == 1 and receipt["fresh_session"] is True
    assert receipt["scope_sha256"] == "a" * 64
    assert receipt["process_instance"] == "proc-1"
    assert hashlib.sha256(PROMPT.encode()).hexdigest() == profile.prompt_digest


# ── cleanup ─────────────────────────────────────────────────────────────────


def test_close_removes_the_tree_only_when_processes_exited(profile):
    root = profile.root
    assert profile.close(processes_exited=False) is False
    assert root.exists()
    assert profile.close(processes_exited=True) is True
    assert not root.exists() and profile.closed
    with pytest.raises(ntp.NativeTextError, match="unavailable"):
        profile.verify_files()


# ── the harness view ────────────────────────────────────────────────────────


def test_native_text_harness_advertises_no_client_capabilities():
    class Harness:
        protocol_version = 7
        client_capabilities = {"fs": {"readTextFile": True}, "terminal": True}

        def reclaim_policy(self, *a, **k):
            return "inner-answer"

    view = ntp.NativeTextHarness(Harness())
    assert view.client_capabilities == {}
    assert view.protocol_version == 7  # every other answer is the host's
    assert view.reclaim_policy() == "inner-answer"
