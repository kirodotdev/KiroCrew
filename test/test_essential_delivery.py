"""Measure submitted prompt bytes through the real provider receipt boundary."""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_member_essential_context import env as _essential_env

from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    EVENT_CLEAR_STATUS,
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.member_essential_context import MemberEssentialContextError, documents_for_member
from kiro_crew.members import (
    member_briefing_path,
    member_briefing_supported,
    slug_for_name,
    write_member_rules,
)
from kiro_crew.providers.acp import AcpProvider


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / ".kiro"))


env = _essential_env
HEADER = "[V2 ESSENTIAL CONTEXT"
SENTINEL = "Preference anchor: 请保留中文原文。"


class Wire:
    def __init__(self, project, backend=ACP_BACKEND_CLAUDE):
        self._work_dir = project
        self.backend = backend
        self._session_id = "native-conversation"
        self.process_instance = "process-one"
        self.messages = []
        self.events = [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Done"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]

    async def stream_events(self, message):
        self.messages.append(message)
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event


def provider(project, backend=ACP_BACKEND_CLAUDE):
    # Keep the real provider stream/identity/receipt implementation. Only replace
    # the subprocess transport; these tests make no OS-admission claim.
    result = object.__new__(AcpProvider)
    result._client = Wire(project, backend)
    result._native_context_documents = {}
    result._native_context_incarnation = None
    return result


def build(env, target, fresh=False, **kwargs):
    return env.builder.build_message(
        "CURRENT_REQUEST_中文",
        fresh,
        kwargs.pop("session_key", "dashboard:private"),
        memory_store=env.store,
        agent=kwargs.pop("agent", "writer-template"),
        context_provider=target,
        **kwargs,
    )[0]


async def send(target, message):
    return [event async for event in target.stream(message)]


@pytest.mark.asyncio
async def test_wire_without_receipt_fresh_and_twenty_warm_turns(env):
    """Also run unedited in a trusted baseline checkout to capture real wire bytes.

    See providers.md's essential-context measurement recipe. Only test
    instrumentation is copied there; every production import is snapshot-local.
    """
    target = provider(env.project)
    for turn in range(21):
        message, _ = env.builder.build_message(
            "CURRENT_REQUEST_中文",
            turn == 0,
            "dashboard:private",
            memory_store=env.store,
            agent="writer-template",
            project=str(env.project),
            provider_type="claude_code",
        )
        await send(target, message)
    messages = target.client.messages
    assert [m.count(HEADER) for m in messages] == [1] * 21
    assert all(m.count(SENTINEL) == 1 for m in messages)
    assert all(m.count("CURRENT_REQUEST_中文") == 1 for m in messages)
    env.forbidden.assert_not_called()
    report = "ESSENTIAL_WIRE_MESSAGES=" + json.dumps(messages, ensure_ascii=False)
    assert baseline_messages(report) == messages
    with pytest.raises(AssertionError, match="expected one baseline wire capture"):
        baseline_messages(report + "\n" + report)
    print("\n" + report)


def baseline_messages(report):
    """Read data from the independent pytest capture, never import its source."""
    prefix = "ESSENTIAL_WIRE_MESSAGES="
    captures = [line for line in report.splitlines() if line.startswith(prefix)]
    assert len(captures) == 1, "expected one baseline wire capture"
    messages = json.loads(captures[0][len(prefix) :])
    assert isinstance(messages, list) and len(messages) == 21
    assert all(isinstance(message, str) for message in messages)
    assert [m.count(HEADER) for m in messages] == [1] * 21
    assert all(m.count(SENTINEL) == 1 for m in messages)
    assert all(m.count("CURRENT_REQUEST_中文") == 1 for m in messages)
    return messages


@pytest.mark.parametrize(
    "bad_report",
    [
        "",
        "ESSENTIAL_WIRE_MESSAGES=[]",
        "ESSENTIAL_WIRE_MESSAGES={}",
        "ESSENTIAL_WIRE_MESSAGES=" + json.dumps([None] * 21),
        "ESSENTIAL_WIRE_MESSAGES=" + json.dumps([HEADER] * 21),
        "ESSENTIAL_WIRE_MESSAGES=" + json.dumps([HEADER + SENTINEL] * 21),
    ],
    ids=["missing", "empty", "object", "non-text", "no-anchor", "no-request"],
)
def test_baseline_capture_rejects_missing_or_incomplete_measurements(bad_report):
    with pytest.raises(AssertionError):
        baseline_messages(bad_report)


@pytest.mark.asyncio
async def test_actual_wire_fresh_and_twenty_unchanged_warm_turns(env):
    if report := os.environ.get("KIROCREW_ESSENTIAL_BASELINE"):
        before = baseline_messages(Path(report).read_text(encoding="utf-8"))
        baseline_sizes = [len(m.encode("utf-8")) for m in before]
        print(
            f"BASELINE_BYTES fresh={baseline_sizes[0]} warm={baseline_sizes[1:]} total={sum(baseline_sizes)} envelopes=21"
        )
    target = provider(env.project)
    for turn in range(21):
        await send(target, build(env, target, fresh=turn == 0))
    messages = target.client.messages
    assert [m.count(HEADER) for m in messages] == [1] + [0] * 20
    assert sum(m.count(SENTINEL) for m in messages) == 1
    assert all(m.count("CURRENT_REQUEST_中文") == 1 for m in messages)
    sizes = [len(m.encode("utf-8")) for m in messages]
    print(f"WIRE_BYTES fresh={sizes[0]} warm={sizes[1:]} total={sum(sizes)} envelopes=1")
    env.forbidden.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "surface",
    ["dashboard:ordinary", "dashboard:member.writer", "slack:x", "cron:job", "subagent:run"],
)
async def test_each_surface_and_minimal_conversation_gets_its_own_receipt(env, surface):
    first, second = provider(env.project), provider(env.project)
    options = dict(session_key=surface, minimal_context=surface.startswith("cron:"))
    await send(first, build(env, first, fresh=True, **options))
    await send(first, build(env, first, **options))
    await send(second, build(env, second, fresh=True, **options))
    assert [m.count(HEADER) for m in first.client.messages] == [1, 0]
    assert second.client.messages[0].count(HEADER) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        "AGENTS.md",
        "SOUL.md",
        "declared-guide.md",
        ".kiro/steering/always.md",
        "preferences",
        "projects",
        "rules",
        "briefing",
        "identity",
        "persona",
        "resources",
    ],
)
async def test_each_changed_source_refreshes_complete_snapshot_once(env, source):
    target = provider(env.project)
    await send(target, build(env, target, fresh=True))
    body = "UPDATED_SOURCE_中文"
    if source == "preferences":
        env.memory.write_preferences(body)
    elif source == "projects":
        env.memory.write_projects(body)
    elif source == "rules":
        write_member_rules(slug_for_name("writer"), member="writer", text=body)
    elif source == "briefing":
        path = member_briefing_path(slug_for_name("writer"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    elif source == "identity":
        cfg = KiroCrewConfig.load()
        cfg.agents["writer"].description = body
        cfg.save()
    elif source in {"persona", "resources"}:
        path = env.project / ".kiro/agents/writer-template.json"
        spec = json.loads(path.read_text(encoding="utf-8"))
        if source == "persona":
            spec["prompt"] = body
        else:
            (env.project / "new.md").write_text(body, encoding="utf-8")
            spec["resources"].append("file://new.md")
        path.write_text(json.dumps(spec), encoding="utf-8")
    else:
        (env.project / source).write_text(body, encoding="utf-8")
    await send(target, build(env, target))
    await send(target, build(env, target))
    if source == "briefing" and not member_briefing_supported():
        # Layer 4 fails closed where O_NOFOLLOW or the pinned walk is missing
        # (Windows): the agent-writable file is never read, the section says
        # so, and an uninjected source cannot change the snapshot. Reading it
        # anyway, or forcing a resend, would reopen the symlink race the
        # fail-closed contract exists to refuse.
        assert [m.count(HEADER) for m in target.client.messages] == [1, 0, 0]
        assert all(body not in message for message in target.client.messages)
        assert "your briefing file is NOT injected" in target.client.messages[0]
        assert "You are writer." in target.client.messages[0]
        env.forbidden.assert_not_called()
        return
    assert [m.count(HEADER) for m in target.client.messages] == [1, 1, 0]
    assert body in target.client.messages[1]
    assert "You are writer." in target.client.messages[1]
    env.forbidden.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["AGENTS.md", "SOUL.md", ".kiro/steering/always.md"])
async def test_optional_deletion_explicitly_replaces_old_source_set(env, source):
    target = provider(env.project)
    await send(target, build(env, target, fresh=True))
    old_body = (env.project / source).read_text(encoding="utf-8")
    (env.project / source).unlink()
    await send(target, build(env, target))
    await send(target, build(env, target))
    assert old_body not in target.client.messages[1]
    assert "replaces ALL prior V2 essential snapshots" in target.client.messages[1]
    assert [m.count(HEADER) for m in target.client.messages] == [1, 1, 0]


@pytest.mark.asyncio
async def test_missing_declared_source_still_refuses_on_warm_hit(env):
    target = provider(env.project)
    await send(target, build(env, target, fresh=True))
    (env.project / "declared-guide.md").unlink()
    with pytest.raises(MemberEssentialContextError, match="declared-guide"):
        build(env, target)
    assert len(target.client.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        {"resumed": True},
        {"needs_reinjection": True},
        {"blocks_reads": True},
        {"minimal_context": True},
        {"model_window": 32_000},
        {"context_groups": frozenset()},
        {"workspace": "another"},
        {"mode": "member"},
    ],
)
async def test_lifecycle_and_scope_changes_refresh_once(env, options):
    target = provider(env.project)
    await send(target, build(env, target, fresh=True))
    await send(target, build(env, target, fresh=bool(options.get("resumed")), **options))
    warm_options = {k: v for k, v in options.items() if k not in {"resumed", "needs_reinjection"}}
    await send(target, build(env, target, **warm_options))
    assert [m.count(HEADER) for m in target.client.messages] == [1, 1, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["exception", "hard_cancel", "cancel", "empty", "synthetic", "notice", "compaction", "clear"],
)
async def test_failed_or_compacted_delivery_retransmits(env, failure):
    target = provider(env.project)
    good = target.client.events
    bad = {
        "exception": [RuntimeError("transport failed")],
        "hard_cancel": [asyncio.CancelledError()],
        "cancel": [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="partial"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED),
        ],
        "empty": [good[-1]],
        "synthetic": [
            good[0],
            AcpEvent(
                kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN, synthetic_completion=True
            ),
        ],
        "notice": [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Compacting", control_notice=True),
            good[-1],
        ],
        "compaction": [AcpEvent(kind=EVENT_COMPACTION_STATUS, text="completed"), *good],
        "clear": [AcpEvent(kind=EVENT_CLEAR_STATUS), *good],
    }
    target.client.events = bad[failure]
    try:
        await send(target, build(env, target, fresh=True))
    except (RuntimeError, asyncio.CancelledError):
        assert failure in {"exception", "hard_cancel"}
    target.client.events = good
    await send(target, build(env, target))
    await send(target, build(env, target))
    assert [m.count(HEADER) for m in target.client.messages] == [1, 1, 0]


@pytest.mark.asyncio
async def test_build_only_and_closed_partial_stream_never_acknowledge(env):
    target = provider(env.project)
    build(env, target, fresh=True)
    stream = target.stream(build(env, target))
    assert (await anext(stream)).kind == EVENT_TEXT_CHUNK
    await stream.aclose()
    await send(target, build(env, target))
    assert all(HEADER in m for m in target.client.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["client", "native_id", "process", "backend"])
async def test_replaced_incarnation_does_not_reuse_old_ack(env, change):
    target = provider(env.project)
    await send(target, build(env, target, fresh=True))
    if change == "client":
        target._client = Wire(env.project)
    elif change == "native_id":
        target.client._session_id = "other-session"
    elif change == "process":
        target.client.process_instance = "other-process"
    else:
        target.client.backend = ACP_BACKEND_KAS
    await send(target, build(env, target))
    assert HEADER in target.client.messages[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
async def test_native_inputs_and_manual_payload_contain_each_body_once(env, backend):
    target = provider(env.project, backend)
    native = dict(documents_for_member("writer-template", str(env.project), native_only=True))
    target._native_context_documents = native
    target._native_context_incarnation = target.context_incarnation
    await send(target, build(env, target, fresh=True))
    assert target.native_context_documents == {}
    combined = target.client.messages[-1]
    assert combined.count("Bound Soul: preserve the user's voice.") == 1
    assert combined.count("Declared guide: examples must be reproducible.") == 1
    assert target.client.messages[-1].count(SENTINEL) == 1
    assert "MANUAL_SECRET" not in combined
    assert "MATCH_SECRET" not in combined
    assert "AUTO_SECRET" not in combined


@pytest.mark.asyncio
async def test_native_snapshot_is_not_evidence_after_client_replacement(env):
    target = provider(env.project, ACP_BACKEND_KIRO)
    target._native_context_documents = dict(
        documents_for_member("writer-template", str(env.project), native_only=True)
    )
    target._native_context_incarnation = target.context_incarnation
    message = build(env, target, fresh=True)
    target._client = Wire(env.project, ACP_BACKEND_KIRO)
    await send(target, message)
    assert "Bound Soul: preserve the user's voice." in target.client.messages[-1]


@pytest.mark.asyncio
async def test_shared_runtime_provider_uses_same_receipt_boundary(env):
    wire = Wire(env.project, ACP_BACKEND_KIRO)
    handle = SimpleNamespace(
        prompt=wire.stream_events,
        session_id="shared",
        cwd=str(env.project),
        served_model="",
        native_context_documents={},
    )
    runtime = SimpleNamespace(process_instance="runtime-one", acp_backend=ACP_BACKEND_KIRO)
    target = AcpSessionProvider(handle, runtime)
    await send(target, build(env, target, fresh=True, project=str(env.project)))
    await send(target, build(env, target, project=str(env.project)))
    assert [m.count(HEADER) for m in wire.messages] == [1, 0]


@pytest.mark.asyncio
async def test_project_and_owner_template_switches_refresh_complete_context(env, tmp_path):
    import shutil

    target = provider(env.project)
    await send(target, build(env, target, fresh=True))
    other = tmp_path / "other-project"
    shutil.copytree(env.project, other)
    (other / "AGENTS.md").write_text("OTHER_PROJECT_GUIDE", encoding="utf-8")
    await send(target, build(env, target, project=str(other)))
    await send(target, build(env, target, project=str(other)))
    cfg = KiroCrewConfig.load()
    cfg.agents["writer"].kiro_agent = "other-template"
    cfg.save()
    (other / ".kiro/agents/other-template.json").write_text(
        json.dumps({"name": "other-template", "prompt": "OTHER_OWNER_PERSONA"}),
        encoding="utf-8",
    )
    await send(target, build(env, target, project=str(other), agent="other-template"))
    await send(target, build(env, target, project=str(other), agent="other-template"))
    assert [m.count(HEADER) for m in target.client.messages] == [1, 1, 0, 1, 0]
    assert "OTHER_PROJECT_GUIDE" in target.client.messages[1]
    assert "OTHER_OWNER_PERSONA" in target.client.messages[3]


@pytest.mark.asyncio
async def test_changed_content_with_preserved_size_and_mtime_refreshes(env):
    target = provider(env.project)
    await send(target, build(env, target, fresh=True))
    path = env.project / "SOUL.md"
    stamp = path.stat()
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("empathy", "clarity"), encoding="utf-8")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    await send(target, build(env, target))
    assert "clarity" in target.client.messages[-1]
    assert HEADER in target.client.messages[-1]


@pytest.mark.asyncio
async def test_rule_reads_and_source_admission_run_after_a_warm_hit(env):
    from kiro_crew.members import MemberRulesUnreadable, member_rules_path

    target = provider(env.project)
    await send(target, build(env, target, fresh=True))
    await send(target, build(env, target))
    path = member_rules_path(slug_for_name("writer"))
    path.write_bytes(b"\xff")
    with pytest.raises(MemberRulesUnreadable):
        build(env, target)
    assert len(target.client.messages) == 2


@pytest.mark.asyncio
async def test_changed_pending_snapshot_cannot_be_acknowledged_by_older_send(env):
    target = provider(env.project)
    old_message = build(env, target, fresh=True)
    env.memory.write_preferences("NEW_PREFERENCE")
    build(env, target)
    await send(target, old_message)
    await send(target, build(env, target))
    assert HEADER in target.client.messages[-1]
    assert "NEW_PREFERENCE" in target.client.messages[-1]


@pytest.mark.asyncio
async def test_budget_and_source_tails_remain_complete_before_wire_dedup(env):
    target = provider(env.project)
    path = env.project / "AGENTS.md"
    body = "required guide\n" * 2200 + "REQUIRED_TAIL"
    path.write_text(body, encoding="utf-8")
    await send(target, build(env, target, fresh=True, model_window=32_000))
    await send(target, build(env, target, model_window=32_000))
    assert body in target.client.messages[0]
    assert HEADER not in target.client.messages[1]
    path.write_text("x" * 64_001, encoding="utf-8")
    with pytest.raises(MemberEssentialContextError, match="AGENTS.md"):
        build(env, target, model_window=32_000)
    assert len(target.client.messages) == 2


@pytest.mark.asyncio
async def test_closing_after_raw_terminal_keeps_receipt(env):
    target = provider(env.project)
    stream = target.stream(build(env, target, fresh=True))
    async for event in stream:
        if event.kind == EVENT_COMPLETE:
            break
    await stream.aclose()
    await send(target, build(env, target))
    assert HEADER not in target.client.messages[-1]


@pytest.mark.asyncio
async def test_late_old_stream_cleanup_cannot_retract_new_receipt(env):
    target = provider(env.project)
    old_stream = target.stream(build(env, target, fresh=True))
    await anext(old_stream)
    try:
        env.memory.write_preferences("NEXT_SNAPSHOT")
        await send(target, build(env, target))
    finally:
        await old_stream.aclose()
    await send(target, build(env, target))
    assert HEADER not in target.client.messages[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
async def test_startup_captures_native_inputs_after_real_private_binding(env, monkeypatch, backend):
    from kiro_crew.history import ConversationLog
    from kiro_crew.member_memory_auth import bind_private_session_store

    key = "dashboard:native-start"
    bind_private_session_store(key, env.store)
    ConversationLog().update_metadata(key, {"memory_store": env.store, "agent": "writer"})
    target = AcpProvider(
        work_dir=env.project, agent="writer-template", session_key=key, acp_backend=backend
    )
    inputs = []

    async def start_transport():
        # Replace only subprocess startup. The real private resolver, source
        # admission, snapshot capture and provider.start sequencing all execute.
        inputs.extend(
            documents_for_member(target.client._agent, str(env.project), native_only=True)
        )
        target._client = Wire(env.project, backend)

    monkeypatch.setattr(target, "_start_kiro_runtime", start_transport)
    await target.start()
    assert target._private_memory is True
    assert target.native_context_documents == {}
    await send(target, build(env, target, fresh=True, session_key=key))
    combined = target.client.messages[-1]
    assert combined.count("Bound Soul: preserve the user's voice.") == 1
    assert combined.count("Declared guide: examples must be reproducible.") == 1


@pytest.mark.asyncio
async def test_existing_receipt_cannot_bypass_changed_protected_binding(env):
    from kiro_crew.context import session_store_for_turn
    from kiro_crew.history import ConversationLog
    from kiro_crew.member_memory_auth import bind_private_session_store
    from kiro_crew.memory_stores import UnknownMemoryStore

    key = "dashboard:private"
    bind_private_session_store(key, env.store)
    log = ConversationLog()
    log.update_metadata(key, {"memory_store": env.store, "agent": "writer"})
    env.builder.conversation_log = log
    target = provider(env.project)
    await send(target, build(env, target, fresh=True))
    log.update_metadata(key, {"memory_store": "default"})
    with pytest.raises(UnknownMemoryStore, match="protected member binding"):
        await session_store_for_turn(env.builder, key)
    assert len(target.client.messages) == 1


def command_target(env, shared):
    wire = Wire(env.project, ACP_BACKEND_KIRO)
    wire.stream_command = wire.stream_events
    if shared:
        handle = SimpleNamespace(
            prompt=wire.stream_events,
            stream_command=wire.stream_events,
            session_id="shared",
            served_model="",
            native_context_documents={},
        )
        runtime = SimpleNamespace(process_instance="runtime", acp_backend=ACP_BACKEND_KIRO)
        return wire, AcpSessionProvider(handle, runtime)
    target = provider(env.project, ACP_BACKEND_KIRO)
    target._client = wire
    return wire, target


RESET_STATUS = {
    "/compact": AcpEvent(kind=EVENT_COMPACTION_STATUS, text="completed"),
    "/clear": AcpEvent(kind=EVENT_CLEAR_STATUS),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("receipt", ["status", "none", "late"])
@pytest.mark.parametrize("command", ["/compact", "/clear"])
async def test_history_resetting_command_restores_snapshot_on_next_turn(
    env, shared, receipt, command
):
    """Fresh, warm dedup, reset command, complete resend, warm dedup again.

    ``status`` answers the command with its own notification; ``none`` never
    sends one (KAS-style or dropped); ``late`` delivers it inside the next
    warm turn, which then cannot be trusted either and resends once more.
    """
    wire, target = command_target(env, shared)
    good = wire.events
    await send(target, build(env, target, fresh=True, project=str(env.project)))
    await send(target, build(env, target, project=str(env.project)))
    wire.events = [RESET_STATUS[command], *good] if receipt == "status" else good
    async for _event in target.stream_command(command):
        pass
    wire.events = [RESET_STATUS[command], *good] if receipt == "late" else good
    await send(target, build(env, target, project=str(env.project)))
    wire.events = good
    await send(target, build(env, target, project=str(env.project)))
    await send(target, build(env, target, project=str(env.project)))
    expected = [1, 0, 0, 1, 1, 0] if receipt == "late" else [1, 0, 0, 1, 0, 0]
    assert [message.count(HEADER) for message in wire.messages] == expected
    assert wire.messages[2] == command
    assert all(message.count(SENTINEL) == message.count(HEADER) for message in wire.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
async def test_unrelated_slash_command_keeps_receipt(env, shared):
    wire, target = command_target(env, shared)
    await send(target, build(env, target, fresh=True, project=str(env.project)))
    async for _event in target.stream_command("/help"):
        pass
    await send(target, build(env, target, project=str(env.project)))
    assert [message.count(HEADER) for message in wire.messages] == [1, 0, 0]


def test_sdk_names_every_receipt_invalidating_observation():
    from kiro_crew.agent_sdk import CONTEXT_EVENT_CLEAR
    from kiro_crew.essential_delivery import (
        RECEIPT_INVALIDATING_EVENTS,
        RECEIPT_RESETTING_COMMANDS,
        EssentialDelivery,
    )

    assert CONTEXT_EVENT_CLEAR == EVENT_CLEAR_STATUS
    assert {EVENT_CLEAR_STATUS, EVENT_COMPACTION_STATUS} <= RECEIPT_INVALIDATING_EVENTS
    assert RECEIPT_RESETTING_COMMANDS == {"/compact", "/clear"}
    delivery = EssentialDelivery()
    assert delivery.prepare_command("/clear") is True
    assert delivery.prepare_command("/compact keep the plan") is True
    assert delivery.prepare_command("/help") is False
    assert delivery.prepare_command("/clearance") is False
    assert delivery.prepare_command("") is False


@pytest.mark.asyncio
async def test_shared_cancel_invalidates_prior_receipt(env):
    from unittest.mock import AsyncMock

    wire = Wire(env.project, ACP_BACKEND_KIRO)
    handle = SimpleNamespace(
        prompt=wire.stream_events,
        session_id="shared",
        served_model="",
        native_context_documents={},
        is_turn_active=True,
        cancel=AsyncMock(),
    )
    runtime = SimpleNamespace(process_instance="runtime", acp_backend=ACP_BACKEND_KIRO)
    target = AcpSessionProvider(handle, runtime)
    await send(target, build(env, target, fresh=True, project=str(env.project)))
    assert await target.cancel() == "acked"
    await send(target, build(env, target, project=str(env.project)))
    assert [message.count(HEADER) for message in wire.messages] == [1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "compaction"])
async def test_same_prepared_native_prompt_retry_restores_full_sources(env, failure):
    target = provider(env.project, ACP_BACKEND_KIRO)
    target._native_context_documents = dict(
        documents_for_member("writer-template", str(env.project), native_only=True)
    )
    target._native_context_incarnation = target.context_incarnation
    message = build(env, target, fresh=True)
    good = target.client.events
    target.client.events = (
        [AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED)]
        if failure == "cancel"
        else [AcpEvent(kind=EVENT_COMPACTION_STATUS, text="completed"), *good]
    )
    await send(target, message)
    target.client.events = good
    await send(target, message)
    await send(target, message)
    body = "Bound Soul: preserve the user's voice."
    assert [m.count(body) for m in target.client.messages] == [1, 1, 0]
    assert [m.count(HEADER) for m in target.client.messages] == [1, 1, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["prompt", "resource", "remove", "model"])
async def test_distinct_execution_template_refreshes_once(env, change):
    path = env.project / ".kiro/agents/executor.json"
    guide = env.project / "execution.md"
    guide.write_text("EXECUTOR_GUIDE", encoding="utf-8")
    spec = {"name": "executor", "prompt": "EXECUTOR_PERSONA", "resources": ["file://execution.md"]}
    path.write_text(json.dumps(spec), encoding="utf-8")
    target = provider(env.project)
    await send(target, build(env, target, fresh=True, agent="executor"))
    if change == "prompt":
        spec["prompt"] = "UPDATED_EXECUTOR"
    elif change == "resource":
        guide.write_text("UPDATED_EXECUTOR", encoding="utf-8")
    elif change == "remove":
        spec["resources"] = []
        guide.unlink()
    else:
        spec["model"] = "different-model"
    path.write_text(json.dumps(spec), encoding="utf-8")
    await send(target, build(env, target, agent="executor"))
    await send(target, build(env, target, agent="executor"))
    assert [m.count(HEADER) for m in target.client.messages] == [1, 1, 0]
    assert target.client.messages[0].count("EXECUTOR_PERSONA") == 1
    assert "You are writer." in target.client.messages[1]
    if change == "remove":
        assert "EXECUTOR_GUIDE" not in target.client.messages[1]
    elif change != "model":
        assert "UPDATED_EXECUTOR" in target.client.messages[1]


@pytest.mark.asyncio
async def test_non_native_conditional_guides_have_guarded_discovery_not_always_bodies(env):
    target = provider(env.project, ACP_BACKEND_CLAUDE)
    await send(target, build(env, target, fresh=True))
    message = target.client.messages[-1]
    assert message.count("CONDITIONAL GUIDE, NOT ACTIVE INSTRUCTIONS") == 3
    assert "explicitly requests #manual" in message
    assert "matching fileMatchPattern" in message
    assert "description is relevant" in message
    assert all(body not in message for body in ("MANUAL_SECRET", "MATCH_SECRET", "AUTO_SECRET"))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
@pytest.mark.parametrize("declared_roots", [False, True])
async def test_native_launch_and_activation_own_exact_sources(
    env, monkeypatch, backend, declared_roots
):
    from unittest.mock import AsyncMock

    from kiro_crew.acp import client as client_module
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp.types import METHOD_SESSION_NEW
    from kiro_crew.config.paths import kiro_agents_dir

    path = env.project / ".kiro/agents/writer-template.json"
    spec = json.loads(path.read_text(encoding="utf-8"))
    if declared_roots:
        spec["resources"] += ["file://AGENTS.md", "file://SOUL.md", "file://.kiro/steering/**/*.md"]
    path.write_text(json.dumps(spec), encoding="utf-8")
    agents = kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    (agents / path.name).write_text(json.dumps(spec), encoding="utf-8")
    rt = AcpRuntime(
        work_dir=env.project, agent="writer-template", acp_backend=backend, private_memory=True
    )
    if backend == ACP_BACKEND_KIRO:
        monkeypatch.setattr(
            client_module, "_resolve_kiro_bin_for_spawn", AsyncMock(return_value=sys.executable)
        )
        plan = await rt._resolve_spawn_plan()
        assert plan.argv[:4] == [sys.executable, "acp", "--agent", "writer-template"]
        assert (
            dict(plan.native_context_documents)[str(env.project / "AGENTS.md")]
            == "Project rules: run the review checks."
        )
    rt._initialized = True
    rt._expect_mcp_reports = False
    requests = []

    async def transport(method, params, timeout=None):
        requests.append((method, params))
        if method == METHOD_SESSION_NEW:
            return {
                "sessionId": "native-proof",
                "modes": {
                    "currentModeId": "writer-template",
                    "availableModes": [{"id": "writer-template", "name": "Writer"}],
                },
            }
        return {}

    monkeypatch.setattr(rt, "_send_and_await", transport)
    handle = await rt.create_session(cwd=env.project, agent="writer-template", mcp_servers=[])
    target = AcpSessionProvider(handle, rt)
    wire = Wire(env.project, backend)
    monkeypatch.setattr(handle, "prompt", wire.stream_events)
    try:
        await send(target, build(env, target, fresh=True, project=str(env.project)))
        native = target.native_context_documents
        combined = "\n".join(native.values()) + wire.messages[-1]
        assert combined.count("Bound Soul: preserve the user's voice.") == 1
        assert combined.count("Declared guide: examples must be reproducible.") == 1
        assert combined.count("Project rules: run the review checks.") == 1
        assert combined.count("Project Soul: write with empathy.") == 1
        assert all(
            body not in combined for body in ("MANUAL_SECRET", "MATCH_SECRET", "AUTO_SECRET")
        )
        if backend == ACP_BACKEND_KIRO:
            assert combined.count("Project rules: run the review checks.") == 1
            assert "MANUAL_SECRET" not in combined
        else:
            params = next(params for method, params in requests if method == METHOD_SESSION_NEW)
            assert (
                params["_meta"]["kiro"]["customAgents"][0]["prompt"]
                == "Bound Soul: preserve the user's voice."
            )
        (env.project / "SOUL.md").write_text("UPDATED_NATIVE_SOURCE", encoding="utf-8")
        await send(target, build(env, target, project=str(env.project)))
        await send(target, build(env, target, project=str(env.project)))
        assert "UPDATED_NATIVE_SOURCE" in wire.messages[-2]
        assert HEADER not in wire.messages[-1]
    finally:
        rt._session_queues.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,body",
    [
        ("Use #manual", "MANUAL_SECRET"),
        ("Edit src/main.py", "MATCH_SECRET"),
        ("Use #auto", "AUTO_SECRET"),
    ],
)
async def test_non_native_selector_includes_full_guide_only_at_trigger(env, text, body):
    (env.project / ".kiro/steering/match.md").write_text(
        "---\ninclusion: fileMatch\nfileMatchPattern: '**/*.py'\n---\nMATCH_SECRET",
        encoding="utf-8",
    )
    target = provider(env.project)

    def prompt(message, fresh):
        return env.builder.build_message(
            message,
            fresh,
            "dashboard:selectors",
            agent="writer-template",
            memory_store=env.store,
            project=str(env.project),
            context_provider=target,
        )[0]

    await send(target, prompt("Unrelated request", True))
    await send(target, prompt(text, False))
    assert body not in target.client.messages[0]
    assert body in target.client.messages[1]
    assert (
        sum(
            secret in target.client.messages[1]
            for secret in ("MANUAL_SECRET", "MATCH_SECRET", "AUTO_SECRET")
        )
        == 1
    )


@pytest.mark.asyncio
async def test_hooks_spans_and_abandoned_preparation_keep_receipt_correct(env):
    from unittest.mock import Mock

    from kiro_crew.hooks import HookResult

    target = provider(env.project)
    original = "generated #manual\nactual user request"
    start = original.index("actual")
    span = []
    prompt, _ = env.builder.build_message(
        original,
        True,
        "dashboard:scope",
        agent="writer-template",
        memory_store=env.store,
        project=str(env.project),
        context_provider=target,
        user_text_range=(start, len(original)),
        user_span_out=span,
    )
    assert prompt[span[0] : span[1]] == "actual user request"
    assert "MANUAL_SECRET" not in prompt
    # This prepared request never reaches the provider.
    env.builder.hooks.on_message = Mock(
        return_value=HookResult(action="modify", text="Use #manual")
    )
    rewritten, _ = env.builder.build_message(
        "original user input",
        False,
        "dashboard:scope",
        agent="writer-template",
        memory_store=env.store,
        project=str(env.project),
        context_provider=target,
    )
    assert "MANUAL_SECRET" in rewritten
    await send(target, "Trusted caller prefix\n" + rewritten)
    await send(target, "Trusted caller prefix\n" + rewritten)
    assert [m.count(HEADER) for m in target.client.messages] == [1, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["auto", "tool-path"])
async def test_conditional_discovery_reaches_the_real_admitted_reader(env, selection):
    from kiro_crew.hooks import safe_read_file

    guide = env.project / ".kiro/steering" / ("auto.md" if selection == "auto" else "match.md")
    body = (
        "---\ninclusion: auto\nname: database\ndescription: Database schema changes\n---\nDATABASE_GUIDE_TAIL"
        if selection == "auto"
        else "---\ninclusion: fileMatch\nfileMatchPattern: '**/*.py'\n---\nPYTHON_GUIDE_TAIL"
    )
    guide.write_text(body, encoding="utf-8")
    target = provider(env.project)
    prompt = build(env, target, fresh=True)
    assert str(guide) in prompt
    assert body not in prompt
    # The scripted model chooses by the advertised condition, rather than
    # receiving every body unconditionally. Exercise the real admitted reader;
    # the foreign harness's fs_read implementation remains an integration check.
    selected_condition = "Database schema changes" if selection == "auto" else "**/*.py"
    assert selected_condition in prompt
    result = await asyncio.to_thread(safe_read_file, str(guide))
    assert result == body


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["dedicated", "shared"])
@pytest.mark.parametrize("event_type", [EVENT_CLEAR_STATUS, EVENT_COMPACTION_STATUS])
async def test_provider_receipt_wrap_and_history_invalidation_contract(env, adapter, event_type):
    """Both provider adapters pair wire acknowledgment with observable history loss."""
    if adapter == "dedicated":
        target = provider(env.project, ACP_BACKEND_KIRO)
        wire = target.client
    else:
        wire = Wire(env.project, ACP_BACKEND_KIRO)
        handle = SimpleNamespace(
            prompt=wire.stream_events,
            session_id="shared-contract",
            cwd=str(env.project),
            served_model="",
            native_context_documents={},
        )
        runtime = SimpleNamespace(process_instance="contract-runtime", acp_backend=ACP_BACKEND_KIRO)
        target = AcpSessionProvider(handle, runtime)
    await send(target, build(env, target, fresh=True, project=str(env.project)))
    await send(target, build(env, target, project=str(env.project)))
    good = wire.events
    wire.events = [AcpEvent(kind=event_type, text="completed"), *good]
    await send(target, build(env, target, project=str(env.project)))
    wire.events = good
    await send(target, build(env, target, project=str(env.project)))
    await send(target, build(env, target, project=str(env.project)))
    assert [message.count(HEADER) for message in wire.messages] == [1, 0, 0, 1, 0]
