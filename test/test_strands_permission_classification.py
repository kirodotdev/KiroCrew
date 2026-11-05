"""Strands permission requests: classified from the gate envelope's owner, never the card.

The Strands CLI asks only for interrupts Crew's gate module raises, and its own
``tool_call`` card guesses ``kind`` from substrings of the tool name. So on this
harness every permission frame must be the gate's envelope, and the kind, the shell
verdict, the MCP identity and the harness tool id come from the envelope's ``owner``
and ``tool`` alone. Anything the envelope cannot classify is refused.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew.acp import _dispatch
from kiro_crew.acp._dispatch_parts.permission_requests import (
    STRANDS_BUILTIN_TOOL_KINDS,
    strands_envelope_identity,
)
from kiro_crew.acp.harness_tool_names import (
    STRANDS_TOOL_IDS_BY_KIRO_TOOL,
    harness_tool_match_names,
    qualified_harness_tool_id,
)
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_PI, ACP_BACKEND_STRANDS
from kiro_crew.agent_sdk.backends_routing import ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS
from kiro_crew.hook_runtime.tool_identity import _is_host_read_only_builtin

NONCE = "a" * 32
TOOL_USE_ID = "tooluse_abc123"


def _envelope(tool: str, owner: str | None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kiro-crew-gate": 1,
        "nonce": NONCE,
        "tool": tool,
        "toolCallId": TOOL_USE_ID,
        "input": extra.pop("input", {"path": "/tmp/x"}),
    }
    if owner is not None:
        body["owner"] = owner
    body.update(extra)
    return body


def _frame(body: dict[str, Any] | str, *, title: str | None = None) -> JsonRpcMessage:
    """The permission frame the Strands CLI sends for a gate interrupt.

    ``toolCallId`` is the INTERRUPT id and ``rawInput`` the interrupt reason
    (``strands-cli`` ``project/acp.ts``), with the Strands worker's two options.
    """
    message = body if isinstance(body, str) else json.dumps(body)
    name = title if title is not None else (body["tool"] if isinstance(body, dict) else "tool")
    return JsonRpcMessage(
        id=7,
        method="session/request_permission",
        params={
            "sessionId": "s1",
            "toolCall": {
                "toolCallId": "interrupt-1",
                "title": name,
                "status": "pending",
                "rawInput": {"method": "confirm", "title": name, "message": message},
            },
            "options": [
                {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
            ],
        },
    )


def _build(
    msg: JsonRpcMessage,
    *,
    backend: str = ACP_BACKEND_STRANDS,
    nonce: str | None = NONCE,
    card_shell: bool | None = None,
):
    """Build through the shared parser with the caches the CLI's card wrote.

    The card (``tool_call`` with ``toolCallId`` = the toolUseId) leaves an empty
    identity pair and, when *card_shell* is given, the shell verdict its
    substring-guessed kind produced.
    """
    shell_cache = {} if card_shell is None else {TOOL_USE_ID: card_shell}
    return _dispatch.build_permission_event(
        msg,
        tool_input_cache={},
        shell_cache=shell_cache,
        raw_params_cache={},
        mcp_server_name_cache={TOOL_USE_ID: ""},
        tool_name_cache={TOOL_USE_ID: ""},
        harness_tool_name_cache={TOOL_USE_ID: "read"},
        gate_envelope_nonce=nonce,
        harness_backend=backend,
    )


def test_a_builtin_read_is_the_harness_reader_joined_to_its_card():
    event, recorded = _build(_frame(_envelope("read", "builtin")))
    assert event is not None
    assert event.tool_call_id == TOOL_USE_ID
    assert event.tool_kind == "read"
    assert event.is_shell is False and event.shell_classified is True
    assert event.tool_name == "read" and event.mcp_server_name == ""
    assert event.mcp_identity_trusted is True and event.mcp_identity_unreadable is False
    assert event.harness_tool_id == "strands#read"
    assert _is_host_read_only_builtin(
        event.tool_name, event.mcp_server_name, mcp_identity_trusted=event.mcp_identity_trusted
    )
    # Strands offers no allow_always: Crew's "always" is its own grant, and the
    # harness is answered with the one-call allow.
    assert recorded == {"once": "allow", "always": "allow", "reject": "deny"}


def test_the_builtin_shell_is_a_command_whatever_the_card_cached():
    event, _ = _build(
        _frame(_envelope("shell", "builtin", input={"command": "ls"})), card_shell=False
    )
    assert event.is_shell is True and event.tool_kind == "execute"
    assert event.harness_tool_id == "strands#shell"


@pytest.mark.parametrize("name", ["read", "shell", "write", "read_secrets", "bash_runner"])
def test_a_project_tool_named_like_a_builtin_proves_nothing(name):
    # The card guessed ``read`` / ``execute`` from the name and cached a shell
    # verdict; neither is read, and the name earns no built-in alias.
    event, _ = _build(_frame(_envelope(name, "custom")), card_shell=True)
    assert event.tool_kind == "other"
    assert event.is_shell is False and event.shell_classified is True
    assert event.tool_name == "" and event.harness_tool_id == ""
    assert event.mcp_identity_unreadable is False
    assert not _is_host_read_only_builtin(
        event.tool_name, event.mcp_server_name, mcp_identity_trusted=event.mcp_identity_trusted
    )


def test_an_mcp_tool_names_its_server_so_per_tool_rules_bind():
    event, _ = _build(_frame(_envelope("mcp__docs__read_page", "mcp:docs")), card_shell=False)
    assert event.tool_kind == "other" and event.is_shell is False
    assert (event.mcp_server_name, event.tool_name) == ("docs", "read_page")
    assert event.mcp_identity_trusted is True
    assert _dispatch.identified_mcp_call(event) == ("docs", "read_page")
    assert event.harness_tool_id == "strands#@docs/read_page"
    # An MCP server's ``read_page`` is not the host's reader.
    assert not _is_host_read_only_builtin(
        event.tool_name, event.mcp_server_name, mcp_identity_trusted=True
    )


@pytest.mark.parametrize(
    "body",
    [
        _envelope("read", None),
        _envelope("read", "owner-from-the-future"),
        _envelope("read", ""),
        _envelope("read", 7),  # type: ignore[arg-type]
        _envelope("mcp__docs__read", "mcp:"),
        _envelope("mcp__docs__read", "mcp:other"),
        _envelope("mcp__docs__", "mcp:docs"),
        _envelope("mcp__" + "s" * 600 + "__t", "mcp:" + "s" * 600),
        _envelope("new_builtin", "builtin"),
    ],
    ids=[
        "no-owner",
        "unknown-owner",
        "empty-owner",
        "non-string-owner",
        "mcp-no-server",
        "mcp-title-names-another-server",
        "mcp-no-tool",
        "mcp-server-over-bound",
        "unclassified-builtin",
    ],
)
def test_an_envelope_that_cannot_be_classified_is_refused(body):
    event, _ = _build(_frame(body), card_shell=False)
    assert event.mcp_identity_unreadable is True
    assert event.mcp_identity_trusted is False
    assert event.is_shell is False and event.shell_classified is False
    assert event.tool_name == "" and event.mcp_server_name == "" and event.harness_tool_id == ""


@pytest.mark.parametrize(
    "msg,nonce",
    [
        (_frame("not json at all", title="read"), NONCE),
        (_frame(_envelope("read", "builtin", nonce="b" * 32)), NONCE),
        (_frame(_envelope("read", "builtin"), title="write"), NONCE),
        (_frame(_envelope("read", "builtin")), None),
    ],
    ids=["unparseable-message", "foreign-nonce", "title-mismatch", "no-session-nonce"],
)
def test_a_strands_frame_that_is_not_the_gate_envelope_is_refused(msg, nonce):
    event, _ = _build(msg, nonce=nonce, card_shell=True)
    assert event.mcp_identity_unreadable is True
    assert event.is_shell is False and event.harness_tool_id == ""


def test_the_refusal_reaches_the_hook_gate():
    """An unreadable identity is denied at Crew's own PreToolUse gate too."""
    from kiro_crew import hooks

    event, _ = _build(_frame(_envelope("read", "owner-from-the-future")))
    decision = hooks.HookManager().on_tool_call(event.title, **hooks.hook_gate_kwargs(event))
    assert decision.action == hooks.TOOL_DENY


def test_pi_envelopes_are_read_as_before():
    """The owner field is Strands' only: pi keeps the extension's own kind."""
    body = _envelope("read", "custom", kind="read")
    event, _ = _build(_frame(body), backend=ACP_BACKEND_PI)
    assert event.tool_kind == "read"
    assert event.mcp_identity_unreadable is False


def test_every_strands_builtin_is_classified():
    """A built-in this table misses would be refused on every call."""
    expected = {
        "shell",
        "read",
        "write",
        "edit",
        "web_fetch",
        "web_search",
        "programmatic_tool_caller",
        "subagent",
    }
    assert set(STRANDS_BUILTIN_TOOL_KINDS) == expected
    read_kinds = {name for name, kind in STRANDS_BUILTIN_TOOL_KINDS.items() if kind == "read"}
    assert read_kinds == {"read"}
    for name in expected:
        identity = strands_envelope_identity({"title": name, "owner": "builtin"})
        assert identity is not None
        assert identity.is_shell is (name == "shell")


def test_strands_spec_hooks_meet_kiro_cli_names_for_builtins_only():
    assert ACP_BACKEND_STRANDS in ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS
    assert harness_tool_match_names("strands#shell") == ("execute_bash", "shell")
    assert harness_tool_match_names("strands#write")[0] == "fs_write"
    assert "fs_read" in harness_tool_match_names("strands#read")
    assert "mcp__docs__read_page" in harness_tool_match_names("strands#@docs/read_page")
    # The card's name never becomes an id on this harness.
    assert qualified_harness_tool_id(ACP_BACKEND_STRANDS, "shell") == ""
    named = {tool for ids in STRANDS_TOOL_IDS_BY_KIRO_TOOL.values() for tool in ids}
    assert named <= set(STRANDS_BUILTIN_TOOL_KINDS)


def test_a_strands_card_shell_guess_never_names_a_post_tool_hook():
    """PostToolUse ids on Strands come from the envelope only, never the card's kind."""
    from types import SimpleNamespace

    from kiro_crew.acp_backends import ACP_BACKEND_GOOSE
    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.agent_sdk.spec_hooks import post_tool_hook_tool_id

    strands = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_STRANDS))
    # A project tool named ``bash_runner``: the card guessed ``execute``, the
    # envelope said ``custom`` and stated no id.
    assert post_tool_hook_tool_id(strands, harness_tool_id="", is_shell=True) == ""
    assert (
        post_tool_hook_tool_id(strands, harness_tool_id="strands#shell", is_shell=True)
        == "strands#shell"
    )
    # Other Crew-fired backends keep the shell fallback they had.
    goose = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_GOOSE))
    assert post_tool_hook_tool_id(goose, harness_tool_id="", is_shell=True) != ""


@pytest.mark.parametrize(
    "server,tool,builtin",
    [("web", "fetch", "web_fetch"), ("web", "search", "web_search"), ("fs", "write", "fs_write")],
)
def test_an_mcp_tool_whose_fused_name_spells_a_builtin_meets_only_its_mcp_forms(
    server, tool, builtin
):
    """``@web/fetch`` is a server's tool, never the ``web_fetch`` built-in a hook names."""
    names = harness_tool_match_names(f"strands#@{server}/{tool}")
    assert names == (f"@{server}/{tool}", f"mcp__{server}__{tool}")
    assert builtin not in names and f"{server}_{tool}" not in names


def test_a_strands_mcp_tool_never_meets_a_builtin_spec_hook_by_its_bare_name():
    """``@docs/web_fetch`` is a server's tool: a ``web_fetch`` spec hook must not run on it."""
    from kiro_crew.hooks import pre_tool_match_names

    event, _ = _build(_frame(_envelope("mcp__docs__web_fetch", "mcp:docs")))
    every, spec = pre_tool_match_names(
        event.title,
        tool_identity=event.tool_name,
        mcp_server=event.mcp_server_name,
        harness_tool_id=event.harness_tool_id,
    )
    assert spec == ("@docs/web_fetch", "mcp__docs__web_fetch")
    assert "web_fetch" not in every
    # A built-in still meets its kiro-cli name.
    builtin, _ = _build(_frame(_envelope("web_fetch", "builtin")))
    _, spec = pre_tool_match_names(
        builtin.title,
        tool_identity=builtin.tool_name,
        mcp_server=builtin.mcp_server_name,
        harness_tool_id=builtin.harness_tool_id,
    )
    assert "web_fetch" in spec


def test_other_harnesses_keep_the_bare_mcp_name():
    from kiro_crew.hooks import pre_tool_match_names

    _, spec = pre_tool_match_names(
        "x", tool_identity="web_fetch", mcp_server="docs", harness_tool_id="goose#web_fetch"
    )
    assert "web_fetch" in spec


def test_a_renamed_builtin_shell_is_refused_not_downgraded():
    """A built-in under a name Crew has not classified is refused, never read as ``other``."""
    event, _ = _build(_frame(_envelope("bash", "builtin", input={"command": "ls"})))
    assert event.mcp_identity_unreadable is True
    assert event.is_shell is False and event.shell_classified is False


@pytest.mark.parametrize("name", ["read", "bash"])
def test_a_renamed_sdk_shell_owner_is_refused(name):
    event, _ = _build(_frame(_envelope(name, "renamed-sdk-shell", input={"command": "ls"})))
    assert event.mcp_identity_unreadable is True
    assert event.tool_kind != "read" and event.tool_name == ""
