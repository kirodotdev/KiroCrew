"""The ``capabilities.script_hooks`` ``hooks`` list: a profile can let named hooks run.

A profile (such as the one bound to scheduled jobs) turns script hooks on and names
the hooks that may run. An agent spec's hook is named ``<agent>/<Event>/<n>``, and a
hook document with a name of its own ``<agent>/<Event>/<name>`` too; a Hooks-page
hook by its name. A listed hook runs, and its exit 2 still blocks; an
unlisted one is refused and its PreToolUse still blocks. No ``hooks`` list: every
hook runs, as before. Being listed changes only whether the hook runs: the shell
deny list and the sandbox still apply.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.platform.governance import parse_profile, resolve

SCOPE = "capabilities.script_hooks"


def _profile(hooks: list[str] | None, mode: str = "allow") -> object:
    gate: dict = {"enabled": True}
    if hooks is not None:
        gate["scopes"] = {"hooks": {"mode": mode, mode: hooks}}
    return parse_profile({"name": "cron", "capabilities": {"script_hooks": gate}})


def _install(monkeypatch: pytest.MonkeyPatch, profile: object) -> None:
    from kiro_crew.platform import context as pc
    from kiro_crew.platform import governance_profiles

    monkeypatch.setattr("kiro_crew.edition.EDITION", "oss")
    real = pc.current_context()
    ctx = SimpleNamespace(governance=None, security=real.security)
    monkeypatch.setattr(pc, "current_context", lambda: ctx)
    monkeypatch.setattr(governance_profiles, "resolve_active_scope", lambda *a, **k: profile)
    # A CI runner may have no namespace sandbox; the spawn path is still the
    # sandboxed one (``sandboxed_spawn_argv_async``), only its wrapping is a pass-through.
    monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **k: (list(argv), None))
    monkeypatch.setattr("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: list(argv))


def _spec_hooks(agent: str, origin: str, *commands: str) -> list:
    from kiro_crew.agent_sdk.spec_hooks import _convert

    entries = [{"command": c} for c in commands]
    hooks, _ = _convert(agent, {"hooks": {"preToolUse": entries}})
    return [dataclasses.replace(h, spec_origin=origin) for h in hooks]


def _doc(trigger: str, command: str, **extra: object) -> dict:
    return {"trigger": trigger, "action": {"type": "command", "command": command}, **extra}


def _documents(agent: str, *docs: dict) -> list:
    from kiro_crew.agent_sdk.spec_hooks import _convert

    hooks, _ = _convert(agent, {"hooks": list(docs)})
    return [dataclasses.replace(h, spec_origin="user") for h in hooks]


def test_the_hooks_list_names_hooks_case_insensitively() -> None:
    profile = _profile(["guarded/PreToolUse/1"])
    assert resolve(None, profile, SCOPE, "hooks:guarded/pretooluse/1").permitted
    assert not resolve(None, profile, SCOPE, "hooks:guarded/PreToolUse/2").permitted
    assert resolve(None, _profile(None), SCOPE, "hooks:anything").permitted


def test_a_hook_is_named_by_its_spec_place_or_its_own_name() -> None:
    from kiro_crew.hook_event_governance import hook_policy_name
    from kiro_crew.hooks import ScriptHook

    first, second = _spec_hooks("guarded", "user", "a", "b")
    assert hook_policy_name(first) == "guarded/PreToolUse/1"
    assert hook_policy_name(second) == "guarded/PreToolUse/2"
    (project,) = _spec_hooks("guarded", "project", "a")
    assert hook_policy_name(project) == "guarded/PreToolUse/1"
    page = ScriptHook(id="abc123", name="  email-guard ", event=first.event, command="a")
    assert hook_policy_name(page) == "email-guard"


def test_a_compaction_hook_takes_no_number_so_a_grant_keeps_its_hook() -> None:
    from kiro_crew.agent_sdk.spec_hooks import _convert
    from kiro_crew.hook_event_governance import hook_policy_name

    def doc(trigger: str, command: str, **extra: object) -> dict:
        return {"trigger": trigger, "action": {"type": "command", "command": command}, **extra}

    documents = [
        doc("PreCompact", "c", confirm=True),
        doc("PreToolUse", "a"),
        doc("postCompact", "c"),
        doc("PreToolUse", "b"),
    ]
    hooks, unconfirmable = _convert("guarded", {"hooks": documents})
    named = [dataclasses.replace(h, spec_origin="user") for h in hooks]
    assert [(hook_policy_name(h), h.command) for h in named] == [
        ("guarded/PreToolUse/1", "a"),
        ("guarded/PreToolUse/2", "b"),
    ]
    assert unconfirmable == 0


@pytest.mark.asyncio
async def test_a_listed_hook_runs_and_an_unlisted_one_blocks(monkeypatch) -> None:
    from kiro_crew.hooks import run_script_hook

    _install(monkeypatch, _profile(["guarded/PreToolUse/1"]))
    listed, unlisted = _spec_hooks("guarded", "user", "exit 0", "exit 0")

    ran = await run_script_hook(listed, "", {"session_key": "cron:job"})
    assert ran.exit_code == 0 and not ran.refused

    refused = await run_script_hook(unlisted, "", {"session_key": "cron:job"})
    assert refused.refused and refused.exit_code == 2
    assert "'guarded/PreToolUse/2' is not in the security policy's hooks list" in refused.error
    refused.from_spec = True
    assert refused.pre_tool_blocks


@pytest.mark.asyncio
async def test_a_listed_hook_that_exits_2_still_blocks(monkeypatch) -> None:
    from kiro_crew.hooks import run_script_hook

    _install(monkeypatch, _profile(["guarded/PreToolUse/1"]))
    (listed,) = _spec_hooks("guarded", "user", "exit 2")
    result = await run_script_hook(listed, "", {"session_key": "cron:job"})
    assert result.exit_code == 2 and not result.refused


@pytest.mark.asyncio
async def test_a_listed_hook_still_meets_the_deny_list_and_the_sandbox(monkeypatch) -> None:
    from kiro_crew import hooks as hooks_mod
    from kiro_crew import sandbox
    from kiro_crew.hooks import ScriptHook, run_script_hook

    _install(monkeypatch, _profile(["guarded/PreToolUse/1", "email-guard"]))
    monkeypatch.setattr(
        hooks_mod, "effective_denied_regexes_from_config", lambda: [r"\bforbidden-word\b"]
    )
    spawned: list[list[str]] = []
    real_spawn = sandbox.sandboxed_spawn_argv_async

    async def _recording(argv, *a, **k):
        spawned.append(list(argv))
        return await real_spawn(argv, *a, **k)

    monkeypatch.setattr(sandbox, "sandboxed_spawn_argv_async", _recording)

    (spec_hook,) = _spec_hooks("guarded", "project", "echo forbidden-word")
    page = ScriptHook(id="p", name="email-guard", event=spec_hook.event, command="")
    page.command = "echo forbidden-word"
    for hook in (spec_hook, page):
        result = await run_script_hook(hook, "", {"session_key": "cron:job"})
        assert result.refused and result.exit_code == 2
        assert "governance" not in result.error
    assert spawned == []

    page.command = "exit 0"
    result = await run_script_hook(page, "", {"session_key": "cron:job"})
    assert result.exit_code == 0
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_without_a_hooks_list_every_hook_runs(monkeypatch) -> None:
    from kiro_crew.hooks import run_script_hook

    _install(monkeypatch, _profile(None))
    for hook in _spec_hooks("guarded", "project", "exit 0", "exit 0"):
        result = await run_script_hook(hook, "", {"session_key": "cron:job"})
        assert result.exit_code == 0 and not result.refused


@pytest.mark.asyncio
async def test_a_skills_only_hook_follows_the_hooks_list(monkeypatch, tmp_path: Path) -> None:
    from kiro_crew.hooks import HOOK_EVENT_USER_PROMPT_SUBMIT, ScriptHookStore

    store = ScriptHookStore(tmp_path)
    store.create(
        {
            "name": "loader",
            "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
            "command": "",
            "skills": ["plain"],
        }
    )
    _install(monkeypatch, _profile(["other"]))
    assert await store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, "hi", session_key="cron:job") == []
    _install(monkeypatch, _profile(["loader"]))
    results = await store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, "hi", session_key="cron:job")
    assert [r.stdout for r in results] == ["Load skills: $plain"]


def test_each_hooks_list_decision_is_audited(monkeypatch) -> None:
    from kiro_crew import sel as sel_mod
    from kiro_crew.hook_event_governance import hook_refusal

    rows: list[dict] = []
    monkeypatch.setattr(
        sel_mod,
        "sel",
        lambda: SimpleNamespace(log_governance_decision=lambda **k: rows.append(k)),
    )
    _install(monkeypatch, _profile(["guarded/PreToolUse/1"]))
    listed, unlisted = _spec_hooks("guarded", "user", "a", "b")
    assert hook_refusal("cron:job", listed) is None
    assert hook_refusal("cron:job", unlisted)
    assert [(r["item"], r["outcome"], r["session_key"]) for r in rows] == [
        ("hooks:guarded/PreToolUse/1", "allowed", "cron:job"),
        ("hooks:guarded/PreToolUse/2", "denied", "cron:job"),
    ]


def test_a_named_hook_document_keeps_its_grant_when_an_update_moves_it(monkeypatch) -> None:
    from kiro_crew.hook_event_governance import hook_refusal

    _install(monkeypatch, _profile(["pkg/PreToolUse/read-guard"]))
    (guard,) = _documents("pkg", _doc("PreToolUse", "a", name="read-guard"))
    added, moved = _documents(
        "pkg", _doc("PreToolUse", "b", name="telemetry"), _doc("PreToolUse", "a", name="read-guard")
    )
    assert hook_refusal("cron:job", guard) is None
    assert hook_refusal("cron:job", moved) is None
    assert hook_refusal("cron:job", added) == (
        "hook 'pkg/PreToolUse/1' (also 'pkg/PreToolUse/telemetry') "
        "is not in the security policy's hooks list"
    )


def test_a_name_that_reads_as_a_number_a_glob_or_two_hooks_is_no_stable_name() -> None:
    from kiro_crew.hook_event_governance import hook_stable_name

    hooks = _documents(
        "pkg",
        _doc("PreToolUse", "a"),
        _doc("PreToolUse", "b", name="PreToolUse-7"),
        _doc("PreToolUse", "c", name=" 3 "),
        _doc("PreToolUse", "d", name="x/1"),
        _doc("PreToolUse", "d2", name="x\\1"),
        _doc("PreToolUse", "e", name="guard*"),
        _doc("PreToolUse", "f", name="Shared"),
        _doc("PostToolUse", "g", name="shared"),
        _doc("PreToolUse", "h", name=" read-guard "),
    )
    assert [hook_stable_name(h) for h in hooks] == [""] * 8 + ["pkg/PreToolUse/read-guard"]
    (object_form,) = _spec_hooks("pkg", "user", "a")
    assert hook_stable_name(object_form) == ""


def test_a_deny_of_either_name_refuses_and_each_name_is_audited(monkeypatch) -> None:
    from kiro_crew import sel as sel_mod
    from kiro_crew.hook_event_governance import hook_refusal

    rows: list[dict] = []
    monkeypatch.setattr(
        sel_mod,
        "sel",
        lambda: SimpleNamespace(log_governance_decision=lambda **k: rows.append(k)),
    )
    (hook,) = _documents("pkg", _doc("PreToolUse", "a", name="telemetry"))
    admitted = []
    for profile in (
        _profile(["pkg/PreToolUse/telemetry"], mode="deny"),
        _profile(["pkg/PreToolUse/1"], mode="deny"),
        _profile(["pkg/PreToolUse/1"]),
    ):
        _install(monkeypatch, profile)
        admitted.append(hook_refusal("cron:job", hook) is None)
    assert admitted == [False, False, True]
    by_number = ("hooks:pkg/PreToolUse/1", "run_script_hook:pkg/PreToolUse/1")
    by_name = ("hooks:pkg/PreToolUse/telemetry", "run_script_hook:pkg/PreToolUse/telemetry")
    assert [(r["item"], r["tool_name"], r["outcome"]) for r in rows] == [
        (*by_number, "allowed"),
        (*by_name, "denied"),
        (*by_number, "denied"),
        (*by_name, "allowed"),
        (*by_number, "allowed"),
        (*by_name, "denied"),
    ]


def test_an_evaluation_error_on_either_name_refuses_in_an_lts_build(monkeypatch) -> None:
    from kiro_crew.hook_event_governance import hook_refusal
    from kiro_crew.platform import governance

    real = governance.resolve

    def failing_on(broken: str):
        def resolve(ceiling, profile, scope, item, **kwargs):
            if item == broken:
                raise RuntimeError("profile unreadable")
            return real(ceiling, profile, scope, item, **kwargs)

        return resolve

    _install(monkeypatch, _profile(["pkg/PreToolUse/1", "pkg/PreToolUse/telemetry"]))
    monkeypatch.setattr("kiro_crew.edition_locks.locked_off", lambda: True)
    (hook,) = _documents("pkg", _doc("PreToolUse", "a", name="telemetry"))
    assert hook_refusal("cron:job", hook) is None
    for broken in ("hooks:pkg/PreToolUse/1", "hooks:pkg/PreToolUse/telemetry"):
        monkeypatch.setattr(governance, "resolve", failing_on(broken))
        assert hook_refusal("cron:job", hook)


def test_a_hook_named_only_with_spaces_still_runs_without_a_hooks_list(monkeypatch) -> None:
    from kiro_crew.hook_event_governance import hook_refusal
    from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, ScriptHook

    _install(monkeypatch, _profile(None))
    page = ScriptHook(id="abc12345", name="   ", event=HOOK_EVENT_PRE_TOOL_USE, command="true")
    assert hook_refusal("cron:job", page) is None
