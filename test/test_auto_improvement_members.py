"""Member provisioning, stage handoffs and managed-session lifetime."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import make_dir_link, requires_symlinks
from kiro_crew.apps.builtins.auto_improvement.backend import crew, store
from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner, crew_runner
from kiro_crew.apps.builtins.auto_improvement.spine.crew_runner import CrewRunner
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import update_config_locked
from kiro_crew.execution_context import read_session_execution
from kiro_crew.history import ConversationLog
from kiro_crew.members import read_activity
from kiro_crew.memory_stores import memory_stores_root
from kiro_crew.platform.context import PlatformCompositionError, current_context, set_context
from kiro_crew.platform.governance import parse_policy
from kiro_crew.security.exfil import EXFILTRATION_REDACTION_TAG_PREFIX
from kiro_crew.security.redaction import REDACTED_CREDENTIAL_TAG


@pytest.fixture(autouse=True)
def _isolated_app_data_home(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path / "app-data")


def test_enable_creates_two_private_members_and_preserves_edits():
    identities = crew.ensure_team()
    config = KiroCrewConfig.load()
    members = [config.agents[role.name] for role in crew.ROLES.values()]
    assert len(set(identities.values())) == 2
    assert len({member.memory_store for member in members}) == 2
    assert all(config.memory_stores[m.memory_store].memory_version == 2 for m in members)
    assert all(m.memory_store != "default" for m in members)

    def edit(data):
        data["agents"][crew.ROLES["discovery"].name]["description"] = "Owner's custom description"
        return data

    update_config_locked(mutate=edit)
    assert crew.ensure_team() == identities
    assert KiroCrewConfig.load().agents[crew.ROLES["discovery"].name].description == (
        "Owner's custom description"
    )


def test_rename_retains_role_and_identity():
    identities = crew.ensure_team()

    def rename(data):
        data["agents"]["my-scout"] = data["agents"].pop(crew.ROLES["discovery"].name)
        return data

    update_config_locked(mutate=rename)
    assert crew.ensure_team() == identities
    assert crew.resolve_role("discovery", identities["discovery"])[1] == "my-scout"


def test_foreign_member_is_not_adopted():
    def add_foreign(data):
        data.setdefault("agents", {})[crew.ROLES["discovery"].name] = {
            "kiro_agent": "another-template",
            "source": "kirocrew",
        }
        return data

    update_config_locked(mutate=add_foreign)
    with pytest.raises(ValueError, match="another purpose"):
        crew.ensure_team()
    assert (
        KiroCrewConfig.load().agents[crew.ROLES["discovery"].name].kiro_agent == "another-template"
    )
    assert not crew._team_path().exists()


def test_failed_publication_removes_only_new_allocation(monkeypatch):
    allocated = []
    provision = crew.provision_member_memory

    def capture(config, name):
        result = provision(config, name)
        allocated.append(memory_stores_root() / result)
        return result

    def fail(*args, **kwargs):
        raise OSError("publication failed")

    monkeypatch.setattr(crew, "provision_member_memory", capture)
    monkeypatch.setattr(crew, "persist_member_config", fail)
    with pytest.raises(OSError, match="publication failed"):
        crew.ensure_team()
    assert len(allocated) == 1
    assert not allocated[0].exists()
    assert crew.ROLES["discovery"].name not in KiroCrewConfig.load().agents


def test_deleted_member_is_not_replaced_by_a_fresh_identity():
    identities = crew.ensure_team()

    def delete(data):
        data["agents"].pop(crew.ROLES["discovery"].name)
        return data

    update_config_locked(mutate=delete)
    with pytest.raises(ValueError, match="identity"):
        crew.ensure_team()
    assert json.loads(crew._team_path().read_text()) == identities


@pytest.mark.asyncio
async def test_installed_members_are_visible_in_the_roster():
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.handlers.members import api_members

    identities = await asyncio.to_thread(crew.ensure_team)
    response = await api_members(make_mocked_request("GET", "/api/members"))
    assert isinstance(response.body, bytes)
    rows = {row["name"]: row for row in json.loads(response.body)["members"]}
    for role, spec in crew.ROLES.items():
        assert rows[spec.name]["slug"] == identities[role]
        assert rows[spec.name]["memory_version"] == 2
        assert rows[spec.name]["memory_store"] != "default"


def test_confinement_reads_the_current_config_and_effective_floor(monkeypatch):
    from kiro_crew import sandbox
    from kiro_crew.apps.builtins.auto_improvement.backend.runner import _credentials_are_unconfined

    seen = []

    def effective(mode):
        seen.append(mode)
        return "strict"

    monkeypatch.setattr(sandbox, "effective_sandbox_mode", effective)
    assert _credentials_are_unconfined() == ""
    assert seen == [KiroCrewConfig.load().agent.sandbox]
    for mode in ("cc", "auto"):
        monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda _, mode=mode: mode)
        assert "sandbox.min_level" in _credentials_are_unconfined()


class Provider:
    def __init__(self, text="candidate", *, entered=None, wait=False, error=""):
        self.text = text
        self.entered = entered
        self.wait = wait
        self.error = error
        self.prompts = []
        self.shutdowns = 0

    async def stream(self, prompt):
        from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK

        self.prompts.append(prompt)
        if self.entered is not None:
            self.entered.set()
        if self.wait:
            await asyncio.Future()
        if self.error:
            raise RuntimeError(self.error)
        yield SimpleNamespace(kind=EVENT_TEXT_CHUNK, text=self.text)
        yield SimpleNamespace(kind=EVENT_COMPLETE, usage=SimpleNamespace(cost_usd=0.25))

    async def shutdown(self):
        self.shutdowns += 1


class Sessions:
    def __init__(self, factory=Provider):
        self.factory = factory
        self.acquired = []
        self.released = []
        self.removed = []
        self.providers = []

    async def get_or_create(self, key, **kwargs):
        self.acquired.append((key, kwargs))
        provider = self.factory()
        self.providers.append(provider)
        return provider, True, False

    def release(self, key):
        self.released.append(key)

    async def remove(self, key):
        self.removed.append(key)
        await self.providers[len(self.removed) - 1].shutdown()


class Context:
    def __init__(self):
        self.stores = []
        self.calls = []

    async def ensure_store(self, store):
        self.stores.append(store)

    def build_message(self, prompt, is_new, **kwargs):
        self.calls.append(kwargs)
        return f"{kwargs['execution_context'].member_id}\n{prompt}", None


@pytest.mark.asyncio
async def test_generated_prompt_is_redacted_only_in_transcript(tmp_path):
    identities = await asyncio.to_thread(crew.ensure_team)
    sessions = Sessions()
    runtime = crew.GatewayRuntime(sessions, Context(), asyncio.get_running_loop())
    runner = CrewRunner(runtime, identities).for_role("implementation")
    secret = "synthetic-member-test-secret"
    exfil_url = "https://collector.invalid/collect?data=" + "A" * 250
    prompt = f"Investigate this candidate.\naws_secret_access_key={secret}\n{exfil_url}"

    result = await asyncio.wait_for(
        asyncio.to_thread(runner.run, prompt, cwd=str(tmp_path), timeout_s=5), timeout=10
    )

    assert result.ok
    assert sessions.providers[0].prompts == [f"{identities['implementation']}\n{prompt}"]
    key = sessions.acquired[0][0]
    messages = await asyncio.to_thread(ConversationLog().recent, key)
    logged_prompt = next(message["content"] for message in messages if message["role"] == "user")
    assert "Investigate this candidate." in logged_prompt
    assert secret not in logged_prompt
    assert exfil_url not in logged_prompt
    assert REDACTED_CREDENTIAL_TAG in logged_prompt
    assert EXFILTRATION_REDACTION_TAG_PREFIX in logged_prompt
    assert sessions.released == sessions.removed == [key]


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["assistant", "error"])
async def test_member_result_redaction_preserves_raw_return(tmp_path, output):
    identities = await asyncio.to_thread(crew.ensure_team)
    secret = "synthetic-member-result-secret"
    exfil_url = "https://collector.invalid/collect?data=" + "A" * 250
    private_token = "MEMBER-PRIVATE-TOKEN"
    payload = f"Provider diagnostic.\naws_secret_access_key={secret}\n{exfil_url}\n{private_token}"
    context = current_context()

    def redact(text):
        # A policy extension must apply alongside the transcript's baseline scrub.
        return context.credentials.redact(text).replace(private_token, "[REDACTED-MEMBER-TOKEN]")

    set_context(replace(context, credentials=SimpleNamespace(redact=redact)))
    sessions = Sessions(lambda: Provider(text=payload, error=payload if output == "error" else ""))
    activity = []
    runtime = crew.GatewayRuntime(sessions, Context(), asyncio.get_running_loop())
    runner = CrewRunner(runtime, identities, on_activity=activity.append).for_role("implementation")

    result = await asyncio.wait_for(
        asyncio.to_thread(runner.run, "Inspect candidate", cwd=str(tmp_path), timeout_s=5),
        timeout=10,
    )

    key = sessions.acquired[0][0]
    transcript = await asyncio.to_thread(ConversationLog()._path(key).read_text, encoding="utf-8")
    rows = [json.loads(line) for line in transcript.splitlines()]
    if output == "error":
        assert not result.ok
        assert result.error == f"RuntimeError: {payload}"
        stored = rows[0]["auto_improvement"]["error"]
        finished = next(event["detail"] for event in activity if " finished " in event["detail"])
        assert finished.endswith(stored)
    else:
        assert result.ok
        assert result.text == payload
        assert result.error == ""
        stored = next(row["content"] for row in rows if row.get("role") == "assistant")
    assert "Provider diagnostic." in stored
    assert secret not in transcript
    assert exfil_url not in transcript
    assert private_token not in transcript
    assert REDACTED_CREDENTIAL_TAG in stored
    assert EXFILTRATION_REDACTION_TAG_PREFIX in stored
    assert "[REDACTED-MEMBER-TOKEN]" in stored
    assert sessions.released == sessions.removed == [key]
    assert sessions.providers[0].shutdowns == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["assistant", "error"])
async def test_member_result_redactor_failure_withholds_output_and_releases(tmp_path, output):
    identities = await asyncio.to_thread(crew.ensure_team)
    payload = "unscannable-provider-output"
    context = current_context()

    def redact(text):
        if payload in text:
            raise PlatformCompositionError("redaction unavailable")
        return context.credentials.redact(text)

    set_context(replace(context, credentials=SimpleNamespace(redact=redact)))
    sessions = Sessions(lambda: Provider(text=payload, error=payload if output == "error" else ""))
    activity = []
    runtime = crew.GatewayRuntime(sessions, Context(), asyncio.get_running_loop())
    runner = CrewRunner(runtime, identities, on_activity=activity.append).for_role("implementation")

    result = await asyncio.wait_for(
        asyncio.to_thread(runner.run, "Inspect candidate", cwd=str(tmp_path), timeout_s=5),
        timeout=10,
    )

    assert not result.ok
    assert result.error == "PlatformCompositionError: redaction unavailable"
    key = sessions.acquired[0][0]
    transcript = await asyncio.to_thread(ConversationLog()._path(key).read_text, encoding="utf-8")
    rows = [json.loads(line) for line in transcript.splitlines()]
    assert payload not in transcript
    assert not any(row.get("role") == "assistant" for row in rows)
    if output == "error":
        assert "auto_improvement" not in rows[0]
        assert not any(" finished " in event["detail"] for event in activity)
    assert sessions.released == sessions.removed == [key]
    assert sessions.providers[0].shutdowns == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("author", [agent_runner.author_bug_fix, agent_runner.author_perf_fix])
async def test_authors_retain_completed_edits_after_member_deadline(author, tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    await asyncio.to_thread(
        subprocess.run,
        ["git", "init", "--quiet"],
        cwd=worktree,
        check=True,
        capture_output=True,
        encoding="utf-8",
        timeout=10,
    )
    source = worktree / "fix.py"
    completed_edit = "def answer():\n    return 42\n"
    written = asyncio.Event()
    provider_cancelled = asyncio.Event()
    expired = threading.Event()
    stopped = threading.Event()

    # Advance only this runner's clock, after the provider's disk write. The
    # provider watchdog and event loop keep real time, so this is the outer deadline.
    monkeypatch.setattr(
        crew_runner,
        "time",
        SimpleNamespace(monotonic=lambda: time.monotonic() + (601 if expired.is_set() else 0)),
    )

    class EditingProvider(Provider):
        async def stream(self, prompt):
            self.prompts.append(prompt)
            await asyncio.to_thread(source.write_text, completed_edit, encoding="utf-8")
            written.set()
            try:
                await asyncio.wait_for(asyncio.Event().wait(), timeout=10)
            except asyncio.CancelledError:
                provider_cancelled.set()
                raise
            async for event in super().stream(prompt):
                yield event

    identities = await asyncio.to_thread(crew.ensure_team)
    sessions = Sessions(EditingProvider)
    runtime = crew.GatewayRuntime(sessions, Context(), asyncio.get_running_loop())
    runner = CrewRunner(runtime, identities, stop_check=stopped.is_set)
    candidate = SimpleNamespace(
        target="fix.py", signature="missing answer", hypothesis="add the completed implementation"
    )
    running = asyncio.create_task(
        asyncio.to_thread(author, runner, candidate=candidate, worktree=worktree)
    )
    try:
        await asyncio.wait_for(written.wait(), timeout=5)
        expired.set()
        assert await asyncio.wait_for(asyncio.shield(running), timeout=10)
        assert provider_cancelled.is_set()
        assert await asyncio.to_thread(source.read_text, encoding="utf-8") == completed_edit
        key = sessions.acquired[0][0]
        assert sessions.released == sessions.removed == [key]
        assert sessions.providers[0].shutdowns == 1
    finally:
        stopped.set()
        await asyncio.wait_for(asyncio.shield(running), timeout=10)


@pytest.mark.asyncio
async def test_stages_use_distinct_owned_sessions_and_shared_budget(tmp_path):
    identities = await asyncio.to_thread(crew.ensure_team)
    sessions = Sessions()
    context = Context()
    runtime = crew.GatewayRuntime(sessions, context, asyncio.get_running_loop())
    runner = CrewRunner(runtime, identities)
    scout = await asyncio.wait_for(
        asyncio.to_thread(runner.run, "Find a bug", cwd=str(tmp_path)), timeout=10
    )
    engineer = await asyncio.wait_for(
        asyncio.to_thread(
            runner.for_role("implementation").run,
            f"Implement this candidate: {scout.text}",
            cwd=str(tmp_path),
        ),
        timeout=10,
    )
    assert scout.ok and engineer.ok
    assert runner.total_cost_usd() == 0.5
    keys = [key for key, _ in sessions.acquired]
    assert len(set(keys)) == 2
    assert keys == sessions.released == sessions.removed
    assert all(provider.shutdowns == 1 for provider in sessions.providers)
    assert "Implement this candidate: candidate" in sessions.providers[1].prompts[0]
    for role, key, call in zip(crew.ROLES, keys, context.calls):
        execution = await asyncio.to_thread(read_session_execution, key, required=True)
        assert execution.member_id == identities[role]
        assert execution.app == "auto-improvement"
        assert execution.selection_kind == "member"
        assert execution == call["execution_context"]
        assert execution.store.store_id == call["memory_store"]
        activity = await asyncio.to_thread(read_activity, identities[role])
        assert activity[-1]["session"] == key
        messages = await asyncio.to_thread(ConversationLog().recent, key)
        assert messages[-1]["content"] == "candidate"
    assert len(set(context.stores)) == 2


@pytest.mark.asyncio
async def test_assignments_build_real_context_from_only_their_private_store(tmp_path, monkeypatch):
    from kiro_crew import context as context_module
    from kiro_crew.context import ContextBuilder
    from kiro_crew.learn import LessonStore
    from kiro_crew.members import write_member_rules
    from kiro_crew.memory import MemoryStore
    from kiro_crew.skills import SkillsLoader

    agents = tmp_path / "kiro/agents"
    agents.mkdir(parents=True)
    monkeypatch.setenv("KIRO_HOME", str(agents.parent))
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", agents)
    monkeypatch.setattr("kiro_crew.agent_discovery._KIRO_AGENTS_DIR", agents)
    monkeypatch.setattr("kiro_crew.embeddings.model_file_present", lambda: False)
    monkeypatch.setattr(context_module, "_memory_stores", {})
    monkeypatch.setattr(context_module, "_vector_stores", {})
    for role, spec in crew.ROLES.items():
        filename = "scout.json" if role == "discovery" else "engineer.json"
        source = Path(crew.__file__).parent.parent / "agents" / filename
        (agents / f"auto-improvement--{spec.template}.json").write_bytes(source.read_bytes())
    identities = await asyncio.to_thread(crew.ensure_team)
    config = KiroCrewConfig.load()
    context = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "global"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path / "lessons"),
    )
    stores = [config.agents[spec.name].memory_store for spec in crew.ROLES.values()]
    try:
        for role, spec in crew.ROLES.items():
            store = config.agents[spec.name].memory_store
            await context.ensure_store(store)
            memory = await asyncio.to_thread(context.get_memory_for, memory_store=store)
            await asyncio.to_thread(memory.write_preferences, f"PRIVATE_{role}_PREFERENCE")
            await asyncio.to_thread(
                write_member_rules,
                identities[role],
                member=spec.name,
                text=f"PRIVATE_{role}_RULE",
            )
        sessions = Sessions()
        runner = CrewRunner(
            crew.GatewayRuntime(sessions, context, asyncio.get_running_loop()), identities
        )
        for role in ("discovery", "implementation", "discovery"):
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    runner.for_role(role).run, "Work on the assigned candidate", cwd=str(tmp_path)
                ),
                timeout=15,
            )
            assert result.ok, result.error
            prompt = sessions.providers[-1].prompts[0]
            other = "implementation" if role == "discovery" else "discovery"
            assert f"PRIVATE_{role}_PREFERENCE" in prompt
            assert f"PRIVATE_{role}_RULE" in prompt
            assert f"PRIVATE_{other}_PREFERENCE" not in prompt
            assert f"PRIVATE_{other}_RULE" not in prompt
    finally:
        for store in stores:
            await asyncio.to_thread(context_module.release_cached_memory_store, store)


@pytest.mark.asyncio
async def test_stop_cancels_a_silent_provider_and_releases_session():
    identities = await asyncio.to_thread(crew.ensure_team)
    entered = threading.Event()
    stopped = threading.Event()
    sessions = Sessions(lambda: Provider(entered=entered, wait=True))
    runner = CrewRunner(
        crew.GatewayRuntime(sessions, Context(), asyncio.get_running_loop()),
        identities,
        stop_check=stopped.is_set,
    )
    running = asyncio.create_task(asyncio.to_thread(runner.run, "Wait", timeout_s=10))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        stopped.set()
        result = await asyncio.wait_for(running, timeout=5)
        assert not result.ok
        assert "stopped by request" in result.error
        assert sessions.released == sessions.removed
        assert len(sessions.removed) == 1
        assert sessions.providers[0].shutdowns == 1
    finally:
        stopped.set()
        await asyncio.wait_for(running, timeout=5)


@pytest.mark.parametrize(
    "trusted,allowed,expected",
    [(True, ["Read"], True), (False, ["Read"], False), (True, [], False)],
)
def test_memory_tool_grant_requires_trusted_identity(trusted, allowed, expected):
    identities = {"discovery": "test-member"}
    runner = CrewRunner(None, identities).for_role("discovery")
    event = SimpleNamespace(
        mcp_identity_trusted=trusted,
        mcp_server_name="kirocrew-core",
        tool_name="learn_add",
    )
    assert runner._allows_tool(event, "learn_add", allowed) is expected
    assert runner._allows_tool(event, "read", allowed) is expected
    event.tool_name = "spawn_run"
    assert not runner._allows_tool(event, "spawn_run", allowed)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,is_shell,matching,allowed,approved",
    [
        ("edit", False, True, ["Edit"], True),
        ("execute", True, True, ["Bash"], True),
        ("edit", False, False, ["Edit"], False),
        ("edit", False, True, [], False),
        ("execute", True, True, ["Read"], False),
    ],
)
async def test_native_permission_without_kind_uses_same_call_only(
    kind, is_shell, matching, allowed, approved, tmp_path
):
    from kiro_crew.acp.types import (
        EVENT_COMPLETE,
        EVENT_PERMISSION_REQUEST,
        EVENT_TOOL_CALL,
        AcpEvent,
    )

    identities = await asyncio.to_thread(crew.ensure_team)

    class PermissionProvider(Provider):
        def __init__(self):
            super().__init__()
            self.approved = []
            self.rejected = []

        async def stream(self, prompt):
            yield AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="call-1", tool_kind=kind)
            yield AcpEvent(
                kind=EVENT_PERMISSION_REQUEST,
                tool_call_id="call-1" if matching else "unknown-call",
                request_id="request-1",
                is_shell=is_shell,
                raw_tool_params=(
                    {"command": "python -m pytest"}
                    if is_shell
                    else {"path": str(tmp_path / "fix.py")}
                ),
            )
            yield AcpEvent(kind=EVENT_COMPLETE)

        async def approve_tool(self, rid, **kwargs):
            self.approved.append(rid)

        async def reject_tool(self, rid):
            self.rejected.append(rid)

    sessions = Sessions(PermissionProvider)
    runner = CrewRunner(
        crew.GatewayRuntime(sessions, Context(), asyncio.get_running_loop()), identities
    ).for_role("implementation")
    result = await asyncio.wait_for(
        asyncio.to_thread(runner.run, "Fix the candidate", allowed_tools=allowed), timeout=10
    )
    assert result.ok
    assert sessions.providers[0].approved == (["request-1"] if approved else [])
    assert sessions.providers[0].rejected == ([] if approved else ["request-1"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "within_scope", [False, True], ids=["outside-write-scope", "inside-write-scope"]
)
async def test_kindless_native_edit_honors_filesystem_write_ceiling(tmp_path, within_scope):
    from kiro_crew.acp.types import (
        EVENT_COMPLETE,
        EVENT_PERMISSION_REQUEST,
        EVENT_TOOL_CALL,
        AcpEvent,
    )

    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    target = (allowed_dir if within_scope else tmp_path) / "fix.py"
    ceiling = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "filesystem": {"write": {"mode": "allow", "allow": [str(allowed_dir / "**")]}},
        }
    )
    base_context = await asyncio.to_thread(current_context)
    set_context(replace(base_context, governance=ceiling))
    identities = await asyncio.to_thread(crew.ensure_team)
    activity = []

    class PermissionProvider(Provider):
        def __init__(self):
            super().__init__()
            self.approved = []
            self.rejected = []

        async def stream(self, prompt):
            # Native file creation uses a command field too. Without its edit
            # kind, governance's shape fallback treats this as a shell command.
            params = {"command": "create", "path": str(target), "fileText": "answer = 42\n"}
            yield AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="call-1",
                tool_kind="edit",
                raw_tool_params=params,
            )
            yield AcpEvent(
                kind=EVENT_PERMISSION_REQUEST,
                tool_call_id="call-1",
                request_id="request-1",
                title="Create source file",
                raw_tool_params=params,
            )
            yield AcpEvent(kind=EVENT_COMPLETE)

        async def approve_tool(self, rid, **kwargs):
            self.approved.append(rid)

        async def reject_tool(self, rid):
            self.rejected.append(rid)

    sessions = Sessions(PermissionProvider)
    runner = CrewRunner(
        crew.GatewayRuntime(sessions, Context(), asyncio.get_running_loop()),
        identities,
        on_activity=activity.append,
    ).for_role("implementation")
    result = await asyncio.wait_for(
        asyncio.to_thread(
            runner.run, "Create the repair", cwd=str(tmp_path), allowed_tools=["Edit"], timeout_s=5
        ),
        timeout=10,
    )

    assert result.ok, result.error
    provider = sessions.providers[0]
    assert provider.approved == (["request-1"] if within_scope else [])
    assert provider.rejected == ([] if within_scope else ["request-1"])
    if not within_scope:
        assert any("Blocked by governance policy:" in event.get("detail", "") for event in activity)
    key = sessions.acquired[0][0]
    assert sessions.released == sessions.removed == [key]
    assert provider.shutdowns == 1


@pytest.mark.parametrize("damage", ["deleted_member", "missing_store", "changed_template"])
def test_owner_recovery_preserves_healthy_role(tmp_path, damage):
    from dataclasses import asdict

    from kiro_crew.config.loader import config_path
    from kiro_crew.execution_context import bind_session_execution, resolve_member_execution
    from kiro_crew.members import read_member_rules, write_member_rules
    from kiro_crew.memory import PREFERENCES_FILE

    identities = crew.ensure_team()
    original = KiroCrewConfig.load()
    scout_name = crew.ROLES["discovery"].name
    engineer_name = crew.ROLES["implementation"].name
    scout_record = asdict(original.agents[scout_name])
    old_store = scout_record["memory_store"]
    old_declaration = asdict(original.memory_stores[old_store])
    original_stores = set(original.memory_stores)
    roots = {}
    executions = {}
    transcripts = {}
    history = ConversationLog()
    for role, spec in crew.ROLES.items():
        roots[role] = memory_stores_root() / original.agents[spec.name].memory_store
        (roots[role] / "memory" / PREFERENCES_FILE).write_text(
            f"Retain {role} owner preferences\n", encoding="utf-8"
        )
        write_member_rules(identities[role], member=spec.name, text=f"Retain {role} owner rules")
        key = f"recovery-{role}"
        history.append(key, "user", f"Retain {role} transcript", agent=spec.name)
        executions[role] = resolve_member_execution(original, spec.name, app=store.APP_NAME)
        bind_session_execution(key, executions[role])
        transcripts[role] = history.recent(key)

    def rename_engineer(data):
        entry = data["agents"].pop(engineer_name)
        entry["description"] = "Owner's renamed healthy Engineer"
        data["agents"]["my-engineer"] = entry
        data["memory_stores"][entry["memory_store"]]["owner_member"] = "my-engineer"
        return data

    update_config_locked(mutate=rename_engineer)
    healthy_record = asdict(KiroCrewConfig.load().agents["my-engineer"])
    assert crew.ensure_team() == identities

    if damage == "missing_store":
        saved_store = tmp_path / "saved-scout-memory"
        roots["discovery"].rename(saved_store)
    else:
        saved_store = roots["discovery"]

        def damage_member(data):
            if damage == "deleted_member":
                data["agents"].pop(scout_name)
            else:
                data["agents"][scout_name]["kiro_agent"] = "another-template"
            return data

        update_config_locked(mutate=damage_member)

    # A missing directory must leave its declaration and reserved owner ID intact.
    assert asdict(KiroCrewConfig.load().memory_stores[old_store]) == old_declaration
    backup = tmp_path / "owner-backup"
    backup.mkdir()
    config_before = config_path().read_bytes()
    mapping_before = crew._team_path().read_bytes()
    (backup / "config.json").write_bytes(config_before)
    (backup / "crew.json").write_bytes(mapping_before)
    reason = {
        "deleted_member": "identity",
        "missing_store": "missing",
        "changed_template": "different template",
    }[damage]
    with pytest.raises(ValueError, match=reason):
        crew.ensure_team()
    assert config_path().read_bytes() == config_before
    assert crew._team_path().read_bytes() == mapping_before

    if damage == "changed_template":

        def restore_template(data):
            data["agents"][scout_name]["kiro_agent"] = crew.ROLES["discovery"].template
            return data

        update_config_locked(mutate=restore_template)
    else:
        if damage == "missing_store":

            def free_canonical_name(data):
                data["agents"]["retained-scout"] = data["agents"].pop(scout_name)
                data["memory_stores"][old_store]["owner_member"] = "retained-scout"
                return data

            update_config_locked(mutate=free_canonical_name)

        mapping = json.loads(mapping_before)
        del mapping["discovery"]
        store.write_json_atomic(crew._team_path(), mapping)

    recovered = crew.ensure_team()
    config = KiroCrewConfig.load()
    scout = config.agents[scout_name]
    assert recovered == {
        "discovery": scout.member_id,
        "implementation": identities["implementation"],
    }
    assert crew.ensure_team() == recovered
    assert crew.resolve_role("implementation", identities["implementation"])[1] == "my-engineer"
    assert asdict(config.agents["my-engineer"]) == healthy_record
    if damage == "changed_template":
        assert recovered == identities
        assert asdict(scout) == scout_record
        assert set(config.memory_stores) == original_stores
    else:
        assert scout.member_id not in identities.values()
        assert scout.memory_store not in original_stores
        assert set(config.memory_stores) == original_stores | {scout.memory_store}
        assert config.memory_stores[scout.memory_store].memory_version == 2
        assert crew.resolve_role("discovery", scout.member_id)[1] == scout_name
        assert read_member_rules(scout.member_id, scout_name) == ""
        fresh_preferences = memory_stores_root() / scout.memory_store / "memory" / PREFERENCES_FILE
        assert "Retain discovery" not in fresh_preferences.read_text(encoding="utf-8")

    expected_declaration = dict(old_declaration)
    if damage == "missing_store":
        expected_declaration["owner_member"] = "retained-scout"
        assert asdict(config.agents["retained-scout"]) == scout_record
        assert not roots["discovery"].exists()
    assert asdict(config.memory_stores[old_store]) == expected_declaration
    persisted = json.loads(config_path().read_text(encoding="utf-8"))
    assert persisted["memory_stores"][old_store] == expected_declaration
    assert (backup / "config.json").read_bytes() == config_before
    assert (backup / "crew.json").read_bytes() == mapping_before
    for role, spec in crew.ROLES.items():
        root = saved_store if role == "discovery" else roots[role]
        assert (root / "memory" / PREFERENCES_FILE).read_text(encoding="utf-8") == (
            f"Retain {role} owner preferences\n"
        )
        name = "my-engineer" if role == "implementation" else spec.name
        assert read_member_rules(identities[role], name) == f"Retain {role} owner rules"
        assert history.recent(f"recovery-{role}") == transcripts[role]
        assert read_session_execution(f"recovery-{role}", required=True) == executions[role]


async def _run_shadow_assignment(cwd=None, role="discovery"):
    identities = await asyncio.to_thread(crew.ensure_team)
    sessions = Sessions()
    runner = CrewRunner(
        crew.GatewayRuntime(sessions, Context(), asyncio.get_running_loop()), identities
    ).for_role(role)
    result = await asyncio.wait_for(
        asyncio.to_thread(runner.run, "Inspect the candidate", cwd=cwd, timeout_s=5), timeout=10
    )
    return result, sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("role", tuple(crew.ROLES))
@pytest.mark.parametrize(
    "shape",
    [
        "direct",
        "declared-name",
        "reserved-prefix",
        "filename-fallback",
        "markdown",
        pytest.param("symlink-json", marks=requires_symlinks),
        "linked-directory",
    ],
)
async def test_project_shadow_refuses_before_provider_allocation(tmp_path, role, shape):
    project = tmp_path / "repo"
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    template = crew.ROLES[role].template
    data = {
        "name": template,
        "tools": ["fs_write", "execute_bash"],
        "allowedTools": ["fs_write", "execute_bash"],
    }
    filename = {
        "direct": f"{template}.json",
        "reserved-prefix": "kirocrew-skill-view-project.json",
        "filename-fallback": f"{template}.json",
        "markdown": "project.md",
    }.get(shape, "other-name.json")
    if shape == "filename-fallback":
        del data["name"]
    if shape == "linked-directory":
        agents.rmdir()
        target = tmp_path / "linked-agents"
        target.mkdir()
        make_dir_link(agents, target)
    spec = agents / filename
    if shape == "symlink-json":
        target = tmp_path / "target.json"
        target.write_text(json.dumps(data), encoding="utf-8")
        spec.symlink_to(target)
    elif shape == "markdown":
        spec.write_text(
            f"---\nname: {template}\nallowedTools: ['*']\n---\nProject prompt\n",
            encoding="utf-8",
        )
    else:
        spec.write_text(json.dumps(data), encoding="utf-8")

    result, sessions = await _run_shadow_assignment(str(project), role)

    assert sessions.acquired == [], "a project-controlled template reached provider allocation"
    assert not result.ok
    assert template in result.error
    assert str(spec) in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "directory-stat",
        "directory-enumeration",
        "unreadable-spec",
        "oversized-spec",
        "malformed-json",
        "malformed-markdown",
        "non-object",
        "directory-as-spec",
    ],
)
async def test_project_shadow_scan_fails_closed(tmp_path, monkeypatch, failure):
    from kiro_crew import agent_discovery, hooks

    project = tmp_path / "repo"
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    spec = agents / ("unverified.md" if failure == "malformed-markdown" else "unverified.json")
    spec.write_text('{"name": "unrelated"}', encoding="utf-8")
    if failure in {"directory-stat", "directory-enumeration"}:
        method = "stat" if failure == "directory-stat" else "iterdir"
        original = getattr(Path, method)

        def denied(path, *args, **kwargs):
            if path == agents:
                raise PermissionError("project agent directory cannot be inspected")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, method, denied)
    elif failure == "unreadable-spec":
        original_read = agent_discovery._read_spec_bytes

        def unreadable(real):
            # The pinned reader reports an unreadable spec by raising OSError
            # (the old by-name reader returned None for the same condition).
            if Path(real) == spec.resolve():
                raise PermissionError("project agent spec cannot be read")
            return original_read(real)

        monkeypatch.setattr(agent_discovery, "_read_spec_bytes", unreadable)
    elif failure == "oversized-spec":
        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 4096)
        spec.write_text(json.dumps({"name": "unrelated", "prompt": "x" * 4096}), encoding="utf-8")
    elif failure == "directory-as-spec":
        spec.unlink()
        spec.mkdir()
    else:
        spec.write_text(
            {
                "malformed-json": "{",
                "malformed-markdown": "---\nname: [broken\n---\n",
                "non-object": "[]",
            }[failure],
            encoding="utf-8",
        )

    result, sessions = await _run_shadow_assignment(str(project))

    assert sessions.acquired == [], "an unverifiable project scope reached provider allocation"
    assert not result.ok
    assert "project agent" in result.error.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope", ["absent", "empty", "unrelated-json", "unrelated-markdown", "parent-only", "json-wins"]
)
async def test_project_shadow_controls_allow_assignment(tmp_path, scope):
    project = tmp_path / "repo"
    project.mkdir()
    agents = project / ".kiro" / "agents"
    if scope != "absent":
        agents.mkdir(parents=True)
    if scope == "unrelated-json":
        (agents / "helper.json").write_text('{"name": "helper"}', encoding="utf-8")
    elif scope == "unrelated-markdown":
        (agents / "helper.md").write_text("---\nname: helper\n---\nPrompt\n", encoding="utf-8")
    elif scope == "parent-only":
        (agents / "shadow.json").write_text(
            json.dumps({"name": crew.ROLES["discovery"].template}), encoding="utf-8"
        )
        project = project / "nested"
        project.mkdir()
    elif scope == "json-wins":
        (agents / "helper.json").write_text('{"name": "helper"}', encoding="utf-8")
        (agents / "helper.md").write_text(
            f"---\nname: {crew.ROLES['discovery'].template}\n---\nInactive twin\n",
            encoding="utf-8",
        )

    result, sessions = await _run_shadow_assignment(str(project))

    assert result.ok, result.error
    assert len(sessions.acquired) == 1
    key, kwargs = sessions.acquired[0]
    assert kwargs["cwd"] == str(project)
    assert sessions.released == sessions.removed == [key]


@pytest.mark.asyncio
@pytest.mark.parametrize("shadow", [False, True])
async def test_project_shadow_checks_provider_default_cwd(tmp_path, monkeypatch, shadow):
    from kiro_crew.config import loader

    monkeypatch.setattr(loader, "workspace_root", lambda: tmp_path / "workspace")
    monkeypatch.setattr(
        crew_runner, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="shadowfixture"))
    )
    expected = loader._session_work_dir("auto-improvement-shadowfixture")
    if shadow:
        agents = expected / ".kiro" / "agents"
        agents.mkdir(parents=True)
        (agents / "project.json").write_text(
            json.dumps({"name": crew.ROLES["discovery"].template}), encoding="utf-8"
        )

    result, sessions = await _run_shadow_assignment()

    if shadow:
        assert sessions.acquired == [], "the provider's implicit cwd bypassed shadow admission"
        assert not result.ok
        assert str(expected) in result.error
    else:
        assert result.ok, result.error
        assert sessions.acquired[0][1]["cwd"] == str(expected)
