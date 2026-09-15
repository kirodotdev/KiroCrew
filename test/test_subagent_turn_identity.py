"""Real identity writers and process incarnations; no security verifier is replaced."""

import asyncio
import json
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_subagent_turn_resilience import (
    _complete_event,
    _manager,
    _mock_sessions,
    _text_event,
    _TransientError,
)

from kiro_crew.config import KiroCrewConfig
from kiro_crew.member_memory_auth import (
    bind_private_session_store,
    issue_member_session_proof,
    verify_member_session_proof,
)
from kiro_crew.messaging.identity import publish_turn_identity
from kiro_crew.session import SessionManager
from kiro_crew.session_pid_sig import verify_session_pid
from kiro_crew.subagent import SubagentInfo
from kiro_crew.testing.workflow_memory_scenario import (
    spawn_progress_summary,
    wait_for_spawn_result,
)

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _child(stack, tmp_path):
    child = stack.enter_context(
        subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=tmp_path,
        )
    )

    def stop():
        child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)

    stack.callback(stop)
    return child


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
async def test_stream_publishes_each_dedicated_attempt_not_shared(tmp_path, monkeypatch, shared):
    """First prompt, same-PID retry and replaced PID use the real registry/writer."""
    from kiro_crew.sel import sel

    sel()  # Normal gateway trust-root initialization in the fixture-owned directory.
    key = "subagent:identity-run"
    calls = []
    payload = '{"recall": "完整 JSON", "write": {"ok": true}}\n'
    with ExitStack() as stack:
        first, replacement = _child(stack, tmp_path), _child(stack, tmp_path)
        registry = SessionManager(KiroCrewConfig())

        async def stream(message):
            pid = registry.get_pid(key)
            expected = "dashboard:parent" if shared else key
            assert verify_session_pid(pid) == expected
            if shared:
                proof = issue_member_session_proof(expected, pid)
                assert proof and verify_member_session_proof(proof, expected)
            calls.append(pid)
            if len(calls) == 1:
                raise _TransientError("retry same live process")
            if len(calls) == 2:
                provider.client._pid = replacement.pid
                raise _TransientError("retry replaced process")
            yield _text_event(payload[:12])
            yield _text_event(payload[12:])
            yield _complete_event()

        sessions = _mock_sessions(stream)
        provider = sessions._provider
        provider.context_window_tokens = lambda: 0
        provider.context_used_tokens = lambda: 0
        provider.client = SimpleNamespace(_pid=first.pid)
        registry._sessions[key] = SimpleNamespace(provider=provider)
        sessions.get_pid = registry.get_pid
        manager = _manager(sessions)
        info = SubagentInfo(id="identity-run", task="return JSON", agent="kirocrew")
        manager._log_spawned(info)
        if shared:
            # The registered parent PID is deliberately visible to get_pid:
            # a shared child must not overwrite it even in that shape.
            from kiro_crew.config.loader import KiroCrewAgentConfig
            from kiro_crew.memory_stores import persist_member_config, provision_member_memory
            from kiro_crew.session_pid_sig import publish_session_pid

            cfg = KiroCrewConfig.load()
            cfg.agents["parent-member"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
            store = provision_member_memory(cfg, "parent-member")
            persist_member_config(cfg, "parent-member", create=True)
            from kiro_crew.history import ConversationLog

            bind_private_session_store("dashboard:parent", store)
            ConversationLog().update_metadata("dashboard:parent", {"memory_store": store})
            await asyncio.to_thread(publish_session_pid, first.pid, "dashboard:parent")
            await asyncio.to_thread(publish_session_pid, replacement.pid, "dashboard:parent")
            manager._should_use_session_sharing = lambda _info: True

            async def shared_provider(*_args):
                return provider

            manager._create_shared_session = shared_provider
        monkeypatch.setattr("kiro_crew.subagent.transient_retry_delay", lambda _attempt: 0)
        await asyncio.wait_for(manager._run_inner(info, key), 10)
        assert calls == [first.pid, first.pid, replacement.pid]
        assert Path(info.result_path).read_text(encoding="utf-8") == payload
        assert json.loads(Path(info.result_path).read_text(encoding="utf-8")) == json.loads(payload)


@pytest.mark.asyncio
async def test_private_binding_publisher_mints_real_proof_for_each_process(tmp_path):
    """Host publisher/proof integration, not a claim of kernel-confined execution."""
    from kiro_crew.config.loader import KiroCrewAgentConfig
    from kiro_crew.memory_stores import persist_member_config, provision_member_memory
    from kiro_crew.subagent_persistence import create_agent_folder

    cfg = KiroCrewConfig.load()
    cfg.agents["identity-member"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    store = provision_member_memory(cfg, "identity-member")
    persist_member_config(cfg, "identity-member", create=True)
    create_agent_folder("private-identity", memory_store=store)
    key = "subagent:private-identity"
    bind_private_session_store(key, store)
    registry = SessionManager(cfg)
    with ExitStack() as stack:
        for _ in range(2):
            child = _child(stack, tmp_path)
            registry._sessions[key] = SimpleNamespace(
                provider=SimpleNamespace(client=SimpleNamespace(_pid=child.pid))
            )
            assert issue_member_session_proof(key, child.pid) == ""
            await publish_turn_identity(registry, key)
            proof = issue_member_session_proof(key, child.pid)
            assert proof and verify_member_session_proof(proof, key)
            assert not verify_member_session_proof(proof, "subagent:foreign")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            "spawn rejected: no surface could show the approval prompt PRIVATE",
            "no_approval_surface",
        ),
        ("spawn rejected PRIVATE", "spawn_rejected"),
        ("memory_unavailable: PRIVATE", "memory_unavailable"),
        ("resume_failed: PRIVATE", "resume_failed"),
        ("cancelled", "cancelled"),
        ("PRIVATE", "failed"),
    ],
)
def test_spawn_diagnostics_have_no_payload(error, reason):
    state = dict(done=True, error=error, task="PRIVATE", result="PRIVATE", proof="PRIVATE")
    assert spawn_progress_summary(state) == {
        "status": "done",
        "awaiting_approval": False,
        "terminal_reason": reason,
    }
    assert "PRIVATE" not in json.dumps(spawn_progress_summary(state))


def test_terminal_spawn_failure_does_not_wait_for_missing_file(tmp_path):
    class Client:
        def get(self, route):
            assert route == "/api/spawn/child?limit=1"
            return {
                "done": True,
                "error": "spawn rejected: no surface could show the approval prompt",
            }

    with pytest.raises(AssertionError, match='"terminal_reason": "no_approval_surface"') as failure:
        wait_for_spawn_result(Client(), tmp_path, "child")
    assert '"missing_file_polls": 0' in str(failure.value)
    assert str(tmp_path) not in str(failure.value)


def test_poll_counts_missing_and_partial_json_separately(tmp_path, monkeypatch):
    path = tmp_path / "subagents" / "child" / "result.txt"
    path.parent.mkdir(parents=True)
    polls = []

    class Client:
        def get(self, _route):
            polls.append(1)
            if len(polls) == 2:
                path.write_text('{"recall":', encoding="utf-8")
            return {"done": len(polls) == 2}

    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    with pytest.raises(AssertionError) as failure:
        wait_for_spawn_result(Client(), tmp_path, "child")
    assert '"missing_file_polls": 1' in str(failure.value)
    assert '"non_json_polls": 1' in str(failure.value)
    assert "recall" not in str(failure.value)


def test_poll_returns_whole_json_without_transcript_stripping(tmp_path):
    path = tmp_path / "subagents" / "child" / "result.txt"
    path.parent.mkdir(parents=True)
    payload = {"recall": "x" * 5000, "write": {"ok": True}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    client = SimpleNamespace(get=lambda _route: {"done": True})
    assert wait_for_spawn_result(client, tmp_path, "child") == payload
    path.write_text("preamble\n" + json.dumps(payload), encoding="utf-8")
    with pytest.raises(AssertionError, match='"non_json_polls": 1'):
        wait_for_spawn_result(client, tmp_path, "child")


def test_valid_json_prefix_is_not_a_finished_result(tmp_path, monkeypatch):
    path = tmp_path / "subagents" / "child" / "result.txt"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    states = iter(({"done": False}, {"done": True}))
    client = SimpleNamespace(get=lambda _route: next(states))
    monkeypatch.setattr(
        "time.sleep", lambda _seconds: path.write_text('{"full": true}', encoding="utf-8")
    )
    assert wait_for_spawn_result(client, tmp_path, "child") == {"full": True}


def test_status_failure_diagnostic_does_not_echo_http_body(tmp_path):
    def failed(_route):
        raise AssertionError("PRIVATE path task result proof")

    with pytest.raises(AssertionError, match="status_unavailable") as failure:
        wait_for_spawn_result(SimpleNamespace(get=failed), tmp_path, "child")
    assert "PRIVATE" not in str(failure.value)
    assert failure.value.__context__ is None
