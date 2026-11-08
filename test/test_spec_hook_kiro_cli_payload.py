"""An agent spec's own hook reads kiro-cli's stdin field names when Crew runs it.

Crew runs on kiro-cli's V2 engine, which tells a spec hook the event in camelCase,
the tool by its full name (``shell``, ``@server/tool``) with its ``tool_tags``, and
a Stop's text as ``assistant_response``. On KAS Crew runs the same hook, so it must
say the same; a ``~/.kiro/hooks`` file hook was written for KAS and keeps KAS's names.
"""

from __future__ import annotations

import asyncio
import dataclasses

import kiro_crew.hooks as hooks_mod
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.hook_runtime.hook_dispatch import pre_tool_match_names
from kiro_crew.hooks import (
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    ScriptHook,
    ScriptHookResult,
    ScriptHookStore,
)


def _spec_hook(event_key: str, origin: str = "user") -> ScriptHook:
    (hook,) = spec_hooks.spec_script_hooks(
        "a1", {"hooks": {event_key: [{"matcher": "*", "command": "guard.sh"}]}}
    )
    if event_key == "stop":
        hook = dataclasses.replace(hook, matcher="")
    return dataclasses.replace(hook, spec_origin=origin)


def _record(monkeypatch) -> dict:
    seen: dict = {}

    async def fake_run(hook, context="", hook_event=None, **_kw):
        seen[hook.id] = hook_event
        return ScriptHookResult(hook_id=hook.id, hook_name=hook.name, event=hook.event)

    monkeypatch.setattr(hooks_mod, "run_script_hook", fake_run)
    return seen


def _fire_pre_tool(store, hooks, **names):
    _all, spec = pre_tool_match_names("touch /tmp/x", **names)
    return asyncio.run(
        store.fire(
            HOOK_EVENT_PRE_TOOL_USE,
            tool_name="touch /tmp/x",
            extra_hooks=hooks,
            extra_hooks_tool_names=spec,
        )
    )


def test_a_spec_hook_on_a_kas_shell_call_reads_kiro_cli_names(tmp_path, monkeypatch):
    seen = _record(monkeypatch)
    store = ScriptHookStore(tmp_path)
    page = ScriptHook(id="page", event=HOOK_EVENT_PRE_TOOL_USE, matcher="*", command="c")
    store._hooks[page.id] = page
    spec = _spec_hook("preToolUse")
    _fire_pre_tool(store, [spec], harness_tool_id="run_command")
    assert seen[spec.id]["hook_event_name"] == "preToolUse"
    assert seen[spec.id]["tool_name"] == "shell"
    assert seen[spec.id]["tool_tags"] == ["shell", "@builtin"]
    # A Hooks-page hook in the same fire keeps Crew's own names.
    assert seen["page"]["hook_event_name"] == HOOK_EVENT_PRE_TOOL_USE
    assert seen["page"]["tool_name"] == "touch /tmp/x"
    assert "tool_tags" not in seen["page"]


def test_a_spec_hook_on_an_mcp_call_is_told_the_server_tool_name(tmp_path, monkeypatch):
    seen = _record(monkeypatch)
    spec = _spec_hook("preToolUse", origin="project")
    _fire_pre_tool(
        ScriptHookStore(tmp_path),
        [spec],
        tool_identity="create_issue",
        mcp_server="github",
        harness_tool_id="github/create_issue",
    )
    assert seen[spec.id]["tool_name"] == "@github/create_issue"
    assert seen[spec.id]["tool_tags"] == ["@github/create_issue", "@github", "@mcp"]


def test_a_spec_stop_hook_reads_assistant_response(tmp_path, monkeypatch):
    seen = _record(monkeypatch)
    spec = _spec_hook("stop")
    asyncio.run(ScriptHookStore(tmp_path).fire(HOOK_EVENT_STOP, "done [x]", extra_hooks=[spec]))
    assert seen[spec.id]["hook_event_name"] == "stop"
    assert seen[spec.id]["assistant_response"] == "done [x]"
    # Crew's own key stays for a script that already reads it.
    assert seen[spec.id]["assistant_text"] == "done [x]"


def test_a_user_hook_file_hook_keeps_kas_names(tmp_path, monkeypatch):
    seen = _record(monkeypatch)
    spec = _spec_hook("preToolUse", origin="user_file")
    _fire_pre_tool(ScriptHookStore(tmp_path), [spec], harness_tool_id="run_command")
    assert seen[spec.id]["hook_event_name"] == HOOK_EVENT_PRE_TOOL_USE
    assert seen[spec.id]["tool_name"] == "execute_bash"
    assert "tool_tags" not in seen[spec.id]


def test_governance_is_asked_about_the_store_event_for_a_camelcase_payload(monkeypatch):
    asked: list = []

    def refuse(_sk, event):
        asked.append(event)
        return "refused"

    monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda _sk: None)
    monkeypatch.setattr("kiro_crew.hook_event_governance.event_refusal", refuse)
    hook = _spec_hook("preToolUse")
    result = asyncio.run(
        hooks_mod.run_script_hook(hook, "", {"hook_event_name": "preToolUse", "cwd": "/"})
    )
    assert asked == [HOOK_EVENT_PRE_TOOL_USE]
    assert result.refused


def test_opencode_apply_patch_is_told_write_not_its_mcp_split():
    # The fused title splits as ``@apply/patch`` too, but the call is opencode's own
    # write tool, so a guard on ``tool_name == "write"`` must still see it.
    from kiro_crew.acp.harness_tool_names import qualified_harness_tool_id

    tool_id = qualified_harness_tool_id("opencode", "apply_patch", ("apply",))
    _all, spec = pre_tool_match_names("apply_patch", harness_tool_id=tool_id)
    payload = spec_hooks.kiro_cli_hook_payload({}, HOOK_EVENT_PRE_TOOL_USE, spec)
    assert payload["tool_name"] == "write"
    assert payload["tool_tags"] == ["write", "@builtin"]


def test_opencode_mcp_web_search_is_told_its_mcp_name_and_tags():
    # ``web_search`` is also a kiro-cli built-in name, but the call is server
    # ``web``'s tool ``search``: it must not be tagged ``@builtin``.
    from kiro_crew.acp.harness_tool_names import qualified_harness_tool_id

    tool_id = qualified_harness_tool_id("opencode", "web_search", ("web",))
    _all, spec = pre_tool_match_names("web_search", harness_tool_id=tool_id)
    payload = spec_hooks.kiro_cli_hook_payload({}, HOOK_EVENT_PRE_TOOL_USE, spec)
    assert payload["tool_name"] == "@web/search"
    assert payload["tool_tags"] == ["@web/search", "@web", "@mcp"]


# ── Every harness's native tools get kiro-cli's own name ──


def _native_calls():
    """``(backend, harness_tool_id, kiro-cli row, mcp_server)`` for every native tool.

    KAS ids come bare; the harness ids are qualified as each harness's permission
    event qualifies them. goose's built-in tools run under its ``developer``
    extension, so the call also states that as its server.
    """
    from kiro_crew.acp.harness_tool_names import HARNESS_TOOL_ID_SEPARATOR, HARNESS_TOOL_TABLES
    from kiro_crew.acp.kas_permissions import KAS_TOOL_IDS_BY_KIRO_TOOL
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_GOOSE, ACP_BACKEND_KAS

    for row, ids in KAS_TOOL_IDS_BY_KIRO_TOOL.items():
        for tool_id in ids:
            yield ACP_BACKEND_KAS, tool_id, row, ""
    for backend, table in HARNESS_TOOL_TABLES.items():
        server = "developer" if backend == ACP_BACKEND_GOOSE else ""
        for row, ids in table.items():
            for tool in ids:
                yield backend, f"{backend}{HARNESS_TOOL_ID_SEPARATOR}{tool}", row, server


def test_every_backend_crew_fires_spec_hooks_for_is_walked():
    # A new backend Crew fires spec hooks for must bring a table this walk reads.
    from kiro_crew.acp.harness_tool_names import HARNESS_TOOL_TABLES
    from kiro_crew.agent_sdk.backends import (
        ACP_BACKEND_KAS,
        ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS,
        ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS,
    )

    fired = ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS | ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS
    assert fired <= {ACP_BACKEND_KAS, *HARNESS_TOOL_TABLES}


def test_every_table_row_has_a_kiro_cli_hook_name():
    from kiro_crew.acp.harness_tool_names import HARNESS_TOOL_TABLES
    from kiro_crew.acp.kas_permissions import KAS_TOOL_IDS_BY_KIRO_TOOL, SPEC_HOOK_TOOL_TAGS

    rows = set(KAS_TOOL_IDS_BY_KIRO_TOOL).union(*HARNESS_TOOL_TABLES.values())
    tagged = {tool for tools in SPEC_HOOK_TOOL_TAGS.values() for tool in tools}
    assert rows | tagged <= set(spec_hooks.KIRO_CLI_BUILTIN_HOOK_NAMES)


def test_a_kas_code_call_keeps_its_read_and_write_tags():
    # ``code`` is a KAS id no row reaches, and kiro-cli's own ``code`` tool.
    _all, spec = pre_tool_match_names("code", harness_tool_id="code")
    payload = spec_hooks.kiro_cli_hook_payload({}, HOOK_EVENT_PRE_TOOL_USE, spec)
    assert (payload["tool_name"], payload["tool_tags"]) == ("code", ["read", "write", "@builtin"])


def test_every_native_tool_of_every_harness_gets_its_kiro_cli_name():
    wrong = []
    for backend, tool_id, row, server in _native_calls():
        tool = tool_id.rpartition("#")[2]
        _all, spec = pre_tool_match_names(
            tool, tool_identity=tool, mcp_server=server, harness_tool_id=tool_id
        )
        got = spec_hooks.kiro_cli_hook_payload({}, HOOK_EVENT_PRE_TOOL_USE, spec)
        name, tags = spec_hooks.KIRO_CLI_BUILTIN_HOOK_NAMES[row]
        if (got.get("tool_name"), got.get("tool_tags")) != (name, list(tags)):
            wrong.append((backend, tool_id, got.get("tool_name"), got.get("tool_tags")))
    assert wrong == []


def test_goose_developer_write_is_told_write():
    _all, spec = pre_tool_match_names(
        "write", tool_identity="write", mcp_server="developer", harness_tool_id="goose#write"
    )
    payload = spec_hooks.kiro_cli_hook_payload({}, HOOK_EVENT_PRE_TOOL_USE, spec)
    assert (payload["tool_name"], payload["tool_tags"]) == ("write", ["write", "@builtin"])


def test_mcp_calls_on_every_harness_get_the_server_tool_name():
    cases = [
        # (title, tool_identity, mcp_server, harness_tool_id)
        ("t", "create_issue", "github", "github/create_issue"),  # KAS
        ("t", "create_issue", "github", "strands#@github/create_issue"),
        (
            "mcp__github__create_issue",
            "mcp__github__create_issue",
            "",
            "claude#mcp__github__create_issue",
        ),
    ]
    for title, identity, server, tool_id in cases:
        _all, spec = pre_tool_match_names(
            title, tool_identity=identity, mcp_server=server, harness_tool_id=tool_id
        )
        payload = spec_hooks.kiro_cli_hook_payload({}, HOOK_EVENT_PRE_TOOL_USE, spec)
        assert payload["tool_name"] == "@github/create_issue", tool_id
        assert payload["tool_tags"] == ["@github/create_issue", "@github", "@mcp"], tool_id


def test_a_trusted_server_name_with_double_underscore_is_kept_whole():
    # claude fuses ``mcp__team__docs__search``; the trusted server is ``team__docs``.
    _all, spec = pre_tool_match_names(
        "mcp__team__docs__search",
        tool_identity="search",
        mcp_server="team__docs",
        harness_tool_id="claude#mcp__team__docs__search",
    )
    payload = spec_hooks.kiro_cli_hook_payload({}, HOOK_EVENT_PRE_TOOL_USE, spec)
    assert payload["tool_name"] == "@team__docs/search"
    assert payload["tool_tags"] == ["@team__docs/search", "@team__docs", "@mcp"]


def test_a_server_named_builtin_something_is_an_mcp_server():
    # Only ``@builtin/<name>`` is the kind namespace; ``builtin__docs`` is a server.
    _all, spec = pre_tool_match_names(
        "t",
        tool_identity="delete_file",
        mcp_server="builtin__docs",
        harness_tool_id="strands#@builtin__docs/delete_file",
    )
    payload = spec_hooks.kiro_cli_hook_payload({}, HOOK_EVENT_PRE_TOOL_USE, spec)
    assert payload["tool_name"] == "@builtin__docs/delete_file"
    assert payload["tool_tags"] == ["@builtin__docs/delete_file", "@builtin__docs", "@mcp"]


# ── A KAS write call's tool_input carries kiro-cli's keys too ──


def _write_payload(tool_id: str, tool_input: dict) -> dict:
    _all, spec = pre_tool_match_names("t", harness_tool_id=tool_id)
    return spec_hooks.kiro_cli_hook_payload(
        {"tool_input": tool_input}, HOOK_EVENT_PRE_TOOL_USE, spec
    )


def test_a_kas_fs_write_input_reads_as_kiro_cli_create():
    got = _write_payload("fs_write", {"path": "/w/a.py", "text": "x = 1\n"})
    assert got["tool_input"] == {
        "path": "/w/a.py",
        "text": "x = 1\n",
        "command": "create",
        "content": "x = 1\n",
    }


def test_a_kas_str_replace_input_reads_as_kiro_cli_str_replace():
    raw = {"path": "/w/a.py", "oldStr": "a", "newStr": "b", "replace_all": True}
    got = _write_payload("str_replace", raw)
    assert got["tool_input"] == {**raw, "command": "strReplace", "replaceAll": True}
    # The caller's dict is not changed in place.
    assert "command" not in raw


def test_a_key_the_input_already_has_is_kept():
    got = _write_payload(
        "fs_write", {"path": "/w/a", "text": "new", "command": "x", "content": "y"}
    )
    assert (got["tool_input"]["command"], got["tool_input"]["content"]) == ("x", "y")


def test_a_kas_fs_append_is_not_told_it_creates_the_file():
    # fs_append states {path, text} like fs_write, but the text is a fragment.
    raw = {"path": "/w/a.log", "text": "one more line\n"}
    got = _write_payload("fs_append", raw)
    assert got["tool_input"] == raw


def test_a_non_write_call_keeps_its_input_as_is():
    got = _write_payload("run_command", {"command": "ls"})
    assert got["tool_input"] == {"command": "ls"}
