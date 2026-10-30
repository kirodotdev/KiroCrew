"""MiMo Code's seed config and session MCP array: routing read-back, version gate,
per-tool denies and wildcards, and the mirror's projection."""

from __future__ import annotations

import asyncio
import functools
import itertools
import json
import logging
import os
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import port_resolution
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp import session_mcp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import mimocode as mimocode_harness
from kiro_crew.acp.harness import mimocode as mimocode_mod
from kiro_crew.acp.harness_tool_names import (
    MAX_HARNESS_CONFIG_MCP_SERVERS,
    MAX_HARNESS_TOOL_NAME_LEN,
    mimocode_spelling,
)
from kiro_crew.acp.session_mcp import CONTROL_PLANE_SERVERS, SessionMcpProjection, ToolsAllowlist
from kiro_crew.agent_sdk.backends import ACP_BACKEND_MIMOCODE
from kiro_crew.providers.mirrors import identity as identity_mod
from kiro_crew.providers.mirrors import mimocode as mimocode_mirror
from kiro_crew.providers.mirrors import mimocode as mirror_mod
from kiro_crew.providers.mirrors.identity import identity_bound_crew_servers
from kiro_crew.providers.mirrors.mimocode import (
    MimoCodeMirror,
    mimocode_deny_rules,
    mimocode_elements,
    mimocode_projection,
    mimocode_tool_id,
    place_mimocode_array,
)

# ── acp mimocode readback
# Refusing branches of the read-back, seed and version gate, pinned against
# ``acp.harness.mimocode``'s own read-back, permission reduction, seed merge and version
# probe (no code shared with opencode). Children are answered at
# ``mimocode_mod.subprocess_mod.run``; no process starts.

#: Read-back argv as the launch passes it, sandbox-wrapped.
_ARGV = ["/opt/bin/mimo", "debug", "config"]
#: Env names the harness, launch row or config read; cleared so no shell steers a case.
_MIMOCODE_ENV_NAMES = (
    "MIMOCODE_HOME",
    "MIMOCODE_CONFIG_CONTENT",
    "MIMOCODE_CONFIG",
    "MIMOCODE_CONFIG_DIR",
    "MIMO_BIN",
)
_READBACK_UNREADABLE = "the resolved configuration could not be read back"
_READBACK_UNPARSED = "the resolved configuration could not be parsed"
_VERSION_UNREADABLE = "the resolved binary did not report a readable version"
# Fake AWS secret-key shape (40 base64 chars).
_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_GITHUB_TOKEN_SHAPE = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"
_EXFIL_URL = "https://evil.example.com/collect?data=aGVsbG8gd29ybGQgdGhpcyBpcyBhIHNlY3JldA=="


def _clear_mimocode_env(monkeypatch) -> None:
    for name in [n for n in os.environ if n.startswith("MIMOCODE_")]:
        monkeypatch.delenv(name, raising=False)
    for name in _MIMOCODE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _answer(stdout: str, returncode: int = 0, stderr: str = ""):
    return type("_Completed", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


def _readback(
    monkeypatch,
    tmp_path,
    stdout: str = "",
    *,
    returncode: int = 0,
    stderr: str = "",
    raises: BaseException | None = None,
    extra_env: dict[str, str] | None = None,
    seed: str | None = None,
    **env: str,
):
    """Launch-style read-back; returns (verdict, child call or ``{}``, client)."""
    _clear_mimocode_env(monkeypatch)
    seen: dict = {}

    def _run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = dict(kwargs)
        seen["env"] = dict(kwargs["env"])
        if raises is not None:
            raise raises
        return _answer(stdout, returncode, stderr)

    monkeypatch.setattr(mimocode_mod.subprocess_mod, "run", _run)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    client = AcpClient(
        work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE, extra_env=extra_env or {}
    )
    if seed is None:
        seed = mimocode_mod._mimocode_routing_config(ACP_BACKEND_MIMOCODE)
    verdict = mimocode_mod._verify_mimocode_routing(client, ACP_BACKEND_MIMOCODE, list(_ARGV), seed)
    return verdict, seen, client


# ── Read-backs that could not run or be read


@pytest.mark.parametrize(
    ("raised", "named"),
    [
        (OSError("no such probe binary"), "(no such probe binary)"),
        (
            mimocode_mod.subprocess_mod.TimeoutExpired(_ARGV, 30.0),
            "timed out after 30.0 seconds",
        ),
    ],
    ids=["oserror", "timeout"],
)
def test_a_read_back_child_that_cannot_run_is_an_exec_issue(
    monkeypatch, tmp_path, raised, named
) -> None:
    (issue, remedy), seen, client = _readback(monkeypatch, tmp_path, raises=raised)
    assert seen["argv"] == _ARGV, "the child was never attempted"
    assert issue.startswith(f"{_READBACK_UNREADABLE} (")
    assert named in issue
    # Harness remedy (run its command), not the gate's (remove an override).
    assert "'mimo debug config'" in remedy and mimocode_mod.MIMO_INSTALL_COMMAND in remedy
    assert "higher-precedence" not in remedy
    assert client._mimocode_native_mounts == frozenset(), "no document was read"


def test_a_non_zero_exit_with_a_silent_child_names_only_the_exit(monkeypatch, tmp_path) -> None:
    (issue, remedy), _seen, _client = _readback(monkeypatch, tmp_path, returncode=3)
    assert issue == f"{_READBACK_UNREADABLE} (exit 3)"
    assert "'mimo debug config'" in remedy


def test_a_recognised_stderr_fault_reaches_the_refusal_without_its_bytes(
    monkeypatch, tmp_path
) -> None:
    (issue, _remedy), _seen, _client = _readback(
        monkeypatch,
        tmp_path,
        returncode=1,
        stderr="Error: cannot parse config at line 3\n",
    )
    assert issue == f"{_READBACK_UNREADABLE} (exit 1: its configuration could not be parsed)"
    assert "line 3" not in issue


def test_a_secret_in_an_unrecognised_stderr_is_not_republished(monkeypatch, tmp_path) -> None:
    (issue, _remedy), _seen, _client = _readback(
        monkeypatch, tmp_path, returncode=1, stderr=f"auth failed for key={_SECRET}\n"
    )
    assert _SECRET not in issue
    assert issue == (
        f"{_READBACK_UNREADABLE} (exit 1, and its stderr holds no message this "
        "gateway recognises)"
    )


def test_a_secret_beside_a_recognised_fault_is_not_republished(monkeypatch, tmp_path) -> None:
    (issue, _remedy), _seen, _client = _readback(
        monkeypatch,
        tmp_path,
        returncode=2,
        stderr=f"parse error in provider block apiKey={_SECRET}\n",
    )
    assert issue.startswith(f"{_READBACK_UNREADABLE} (exit 2: ")
    assert "its configuration could not be parsed" in issue
    assert _SECRET not in issue and "apiKey" not in issue


@pytest.mark.parametrize(
    "stdout",
    ["no json here", "mimo 0.1.15\n{not json", '[{"permission": "ask"}]', "", '"ask"'],
    ids=["no-brace", "broken-object", "array", "empty", "json-string"],
)
def test_an_unparseable_document_is_a_harness_issue(monkeypatch, tmp_path, stdout) -> None:
    (issue, remedy), _seen, client = _readback(monkeypatch, tmp_path, stdout)
    assert issue == _READBACK_UNPARSED
    assert "'mimo debug config'" in remedy
    assert client._mimocode_native_mounts == frozenset(), "a refused document set no fields"


def test_a_banner_before_the_document_is_tolerated(monkeypatch, tmp_path) -> None:
    """A banner precedes the object, so it is searched for."""
    verdict, _seen, client = _readback(
        monkeypatch,
        tmp_path,
        'mimo 0.1.15 (build abc)\n{"permission": {"*": "ask"}, "mcp": {"plain": {}, '
        '"srv": {"type": "local", "command": ["srv", "-v"]}}}\n',
    )
    assert verdict == ("", "")
    # Set only after a parse: this passes on the parse branch alone.
    assert client._mimocode_native_mounts == frozenset({"plain", "srv"})
    launch = mimocode_mod.MimoCodeMountLaunch(("srv", "-v"), {})
    assert client._mimocode_native_mount_launches == {"srv": launch}


# ── Bounds on the config's MCP server names


def _mcp_document(servers: dict) -> str:
    return json.dumps({"permission": {"*": "ask"}, "mcp": servers})


def test_rewritten_names_at_the_count_bound_are_kept(monkeypatch, tmp_path) -> None:
    servers = {f"s.{i}": {} for i in range(MAX_HARNESS_CONFIG_MCP_SERVERS)}
    verdict, _seen, client = _readback(monkeypatch, tmp_path, _mcp_document(servers))
    assert verdict == ("", "")
    assert len(client._mimocode_config_mcp_servers) == MAX_HARNESS_CONFIG_MCP_SERVERS


def test_rewritten_names_past_the_count_bound_refuse(monkeypatch, tmp_path) -> None:
    """Refuses, never truncates: a dropped name would miss its deny."""
    over = MAX_HARNESS_CONFIG_MCP_SERVERS + 1
    servers = {f"s.{i}": {} for i in range(over)}
    (issue, remedy), _seen, client = _readback(monkeypatch, tmp_path, _mcp_document(servers))
    assert issue == (
        f"its config mounts {over} MCP servers, more than the "
        f"{MAX_HARNESS_CONFIG_MCP_SERVERS} Crew tracks per session"
    )
    assert remedy.startswith("Remove MCP servers from MiMo Code's own config")
    assert client._mimocode_config_mcp_servers == ()
    assert client._mimocode_native_mounts == frozenset(), "refused before the mounts were kept"


def test_names_written_as_they_are_count_toward_the_bound(monkeypatch, tmp_path) -> None:
    """Every mounted name is a native mount, so unrewritten names count too."""
    over = MAX_HARNESS_CONFIG_MCP_SERVERS + 1
    servers = {f"s-{i}_x": {} for i in range(over)}
    (issue, _remedy), _seen, client = _readback(monkeypatch, tmp_path, _mcp_document(servers))
    assert issue.startswith(f"its config mounts {over} MCP servers")
    assert client._mimocode_config_mcp_servers == ()
    assert client._mimocode_native_mounts == frozenset(), "refused before the mounts were kept"


def test_plain_names_at_the_count_bound_are_kept_as_native_mounts(monkeypatch, tmp_path) -> None:
    servers = {f"s-{i}_x": {} for i in range(MAX_HARNESS_CONFIG_MCP_SERVERS)}
    verdict, _seen, client = _readback(monkeypatch, tmp_path, _mcp_document(servers))
    assert verdict == ("", "")
    assert client._mimocode_config_mcp_servers == ()
    assert len(client._mimocode_native_mounts) == MAX_HARNESS_CONFIG_MCP_SERVERS


@pytest.mark.parametrize("name_char", ["x", "."], ids=["plain", "rewritten"])
def test_a_name_at_the_length_bound_is_kept(monkeypatch, tmp_path, name_char) -> None:
    name = "a" + name_char * (MAX_HARNESS_TOOL_NAME_LEN - 1)
    verdict, _seen, client = _readback(monkeypatch, tmp_path, _mcp_document({name: {}}))
    assert verdict == ("", "")
    assert client._mimocode_native_mounts == frozenset({name})


@pytest.mark.parametrize("name_char", ["x", "."], ids=["plain", "rewritten"])
def test_a_name_past_the_length_bound_refuses(monkeypatch, tmp_path, name_char) -> None:
    """The bound covers every name, rewritten or not."""
    name = "a" + name_char * MAX_HARNESS_TOOL_NAME_LEN
    (issue, remedy), _seen, client = _readback(monkeypatch, tmp_path, _mcp_document({name: {}}))
    assert issue == (
        "its config mounts an MCP server whose name is over "
        f"{MAX_HARNESS_TOOL_NAME_LEN} characters"
    )
    assert name not in issue
    assert remedy.startswith("Remove MCP servers from MiMo Code's own config")
    assert client._mimocode_native_mounts == frozenset()


@pytest.mark.parametrize("mcp", [None, [], "docs.server", 3], ids=["null", "list", "str", "int"])
def test_an_mcp_value_that_is_not_a_map_names_no_server(mcp) -> None:
    assert mimocode_mod._mimocode_config_mcp_server_names({"mcp": mcp}) == ((), "")


def test_non_string_and_empty_names_are_not_counted() -> None:
    resolved = {"mcp": {"": {}, 7: {}, "docs.server": {}}}
    assert mimocode_mod._mimocode_config_mcp_server_names(resolved) == (("docs.server",), "")


# ── Child environment


def test_the_read_back_child_is_scrubbed_and_carries_the_overlay(monkeypatch, tmp_path) -> None:
    """Ambient and overlay credentials are scrubbed (overlay first); plain keys pass."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # Exists, so the socket repair keeps it and only the scrub decides.
    (tmp_path / "agent.sock").write_text("", encoding="utf-8")
    verdict, seen, _client = _readback(
        monkeypatch,
        tmp_path,
        '{"permission": {"*": "ask"}}',
        extra_env={
            "XDG_CONFIG_HOME": str(tmp_path / "overlay"),
            "MIMO_PROBE_OVERLAY": "from-the-session",
            "TELEGRAM_BOT_TOKEN": "overlay-must-not-bypass-the-scrub",
        },
        SLACK_BOT_TOKEN="xoxb-should-not-travel",
        AWS_SECRET_ACCESS_KEY="should-not-travel",
        AWS_SESSION_TOKEN="should-not-travel",
        KIRO_API_KEY="should-not-travel",
        SSH_AUTH_SOCK=str(tmp_path / "agent.sock"),
    )
    assert verdict == ("", "")
    env = seen["env"]
    for leaked in (
        "SLACK_BOT_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "KIRO_API_KEY",
        "SSH_AUTH_SOCK",
        "TELEGRAM_BOT_TOKEN",
    ):
        assert leaked not in env, f"{leaked} reached the read-back child"
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "overlay")
    assert env["MIMO_PROBE_OVERLAY"] == "from-the-session"
    assert env.get("PATH"), "the child still needs a PATH"


def test_the_read_back_child_gets_the_seed_verbatim_and_a_bounded_call(
    monkeypatch, tmp_path
) -> None:
    seed = json.dumps({"permission": "ask", "model": "probe/m"})
    _verdict, seen, _client = _readback(
        monkeypatch,
        tmp_path,
        '{"permission": {"*": "ask"}}',
        seed=seed,
        MIMOCODE_PERMISSION='{"*": "allow"}',
        MIMOCODE_ENABLE_EXEC_TOOL="1",
        MIMOCODE_DISABLE_CRON="0",
    )
    kwargs = seen["kwargs"]
    assert seen["argv"] == _ARGV, "the wrapped argv is used as handed over"
    assert kwargs["env"]["MIMOCODE_CONFIG_CONTENT"] == seed
    assert kwargs["timeout"] == mimocode_mod._MIMOCODE_READBACK_TIMEOUT_S
    assert kwargs["cwd"] == str(tmp_path)
    assert "shell" not in kwargs
    for name in mimocode_mod._MIMOCODE_STRIPPED_ENV:
        assert name not in kwargs["env"]
    for name, value in mimocode_mod._MIMOCODE_PINNED_ENV:
        assert kwargs["env"][name] == value


def test_a_refused_variable_in_the_overlay_refuses_before_any_child(monkeypatch, tmp_path) -> None:
    """Cron env lands in the overlay, so it is checked like the ambient."""
    (issue, remedy), seen, _client = _readback(
        monkeypatch,
        tmp_path,
        '{"permission": {"*": "ask"}}',
        extra_env={"MIMOCODE_HOME": str(tmp_path / "relocated")},
    )
    assert seen == {}, "the read-back child ran with a refused variable"
    assert issue.startswith("MIMOCODE_HOME is set, and it moves MiMo Code's credential")
    assert remedy == mimocode_mod._MIMOCODE_REFUSED_ENV_REMEDY


def test_an_empty_refused_variable_does_not_refuse(monkeypatch, tmp_path) -> None:
    verdict, seen, _client = _readback(
        monkeypatch, tmp_path, '{"permission": {"*": "ask"}}', MIMOCODE_HOME=""
    )
    assert verdict == ("", "")
    assert seen["argv"] == _ARGV


# ── Version probe

_SANDBOX = ["/opt/run/kirocrew_sandbox_launcher"]
_VERSION_CLEANUP = "/opt/run/kirocrew_sandbox_version"


def _version_refusal(monkeypatch, tmp_path, run, *, extra_env: dict[str, str] | None = None):
    """Run ``_mimocode_version_refusal``: (verdict, probe calls, unlinks)."""
    _clear_mimocode_env(monkeypatch)
    calls: list[dict] = []
    unlinked: list[str] = []

    async def _wrap(argv, **kwargs):
        return [*_SANDBOX, *argv], _VERSION_CLEANUP

    def _run(argv, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        return run(argv, **kwargs)

    monkeypatch.setattr(mimocode_mod, "wrap_argv_async", _wrap)
    monkeypatch.setattr(mimocode_mod.subprocess_mod, "run", _run)
    monkeypatch.setattr(launch_mod, "_unlink_readback_launcher", unlinked.append)
    client = AcpClient(
        work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE, extra_env=extra_env or {}
    )
    verdict = asyncio.run(
        asyncio.wait_for(
            mimocode_mod._mimocode_version_refusal(
                client, "/opt/bin/mimo", sandbox_mode="standard", hidden=(), expose=()
            ),
            timeout=60,
        )
    )
    return verdict, calls, unlinked


def _raise(exc: BaseException):
    def _run(argv, **kwargs):
        raise exc

    return _run


@pytest.mark.parametrize(
    "run",
    [
        lambda argv, **kw: _answer("0.1.15", returncode=2),
        _raise(OSError("exec of the probe failed")),
        _raise(mimocode_mod.subprocess_mod.TimeoutExpired(["mimo", "--version"], 15.0)),
    ],
    ids=["non-zero-exit", "oserror", "timeout"],
)
def test_a_probe_that_fails_reports_no_version(monkeypatch, tmp_path, run) -> None:
    """Every failure is ``None``, which refuses, even in-range stdout with a non-zero exit."""
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    monkeypatch.setattr(mimocode_mod.subprocess_mod, "run", run)
    assert mimocode_mod._mimocode_reported_version(client, ["/opt/bin/mimo", "--version"]) is None
    (issue, remedy), calls, unlinked = _version_refusal(monkeypatch, tmp_path, run)
    assert issue == (f"{_VERSION_UNREADABLE}, so the launch cannot establish it is the tested 0.1")
    assert "@mimo-ai/cli@0.1" in remedy and "MIMO_BIN" in remedy
    assert [call["argv"] for call in calls] == [[*_SANDBOX, "/opt/bin/mimo", "--version"]]
    assert unlinked == [_VERSION_CLEANUP], "the probe's launcher leaked on refusal"


def test_an_in_range_probe_passes_and_is_reclaimed(monkeypatch, tmp_path) -> None:
    verdict, calls, unlinked = _version_refusal(
        monkeypatch, tmp_path, lambda argv, **kw: _answer("mimo 0.1.15\n")
    )
    assert verdict == ("", "")
    assert unlinked == [_VERSION_CLEANUP]
    [call] = calls
    assert call["timeout"] == mimocode_mod._MIMOCODE_VERSION_TIMEOUT_S
    assert call["cwd"] == str(tmp_path)
    assert "shell" not in call


def test_the_probe_child_is_scrubbed_stripped_and_pinned(monkeypatch, tmp_path) -> None:
    """Probe env is built like the read-back's, minus the seed."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-should-not-travel")
    verdict, calls, _unlinked = _version_refusal(
        monkeypatch,
        tmp_path,
        lambda argv, **kw: _answer("0.1.15"),
        extra_env={
            "MIMO_PROBE_OVERLAY": "from-the-session",
            "MIMOCODE_EXPERIMENTAL": "1",
            "MIMOCODE_CODEX_MODE": "true",
        },
    )
    assert verdict == ("", "")
    env = calls[0]["env"]
    assert "SLACK_BOT_TOKEN" not in env
    assert env["MIMO_PROBE_OVERLAY"] == "from-the-session"
    assert "MIMOCODE_EXPERIMENTAL" not in env
    for name, value in mimocode_mod._MIMOCODE_PINNED_ENV:
        assert env[name] == value
    assert "MIMOCODE_CONFIG_CONTENT" not in env, "the version child carries no seed"


# ── The ruleset and the asking posture


def _rules(value: object) -> list:
    return mimocode_mod._mimocode_ruleset(value)


@pytest.mark.parametrize(
    ("value", "rules"),
    [
        ("ask", [("*", "*", "ask")]),
        ({"*": "ask", "bash": "deny"}, [("*", "*", "ask"), ("bash", "*", "deny")]),
        (
            {"bash": {"git *": "allow", "*": "ask"}},
            [("bash", "git *", "allow"), ("bash", "*", "ask")],
        ),
        ({}, []),
        (None, []),
        (["ask"], []),
        ({"bash": 1, "edit": {"*": 2}}, []),
    ],
    ids=["bare", "map", "pattern-map", "empty", "none", "list", "non-actions"],
)
def test_a_permission_value_becomes_mimo_codes_ordered_ruleset(value, rules) -> None:
    """``Permission.fromConfig`` (``permission/index.ts:674-694``), in config order."""
    assert [tuple(rule) for rule in _rules(value)] == rules


@pytest.mark.parametrize(
    "value",
    [
        "ask",
        {"*": "ask"},
        {"bash": "allow", "*": "ask"},
        {"*": "ask", "bash": "ask", "srv_tool": "deny"},
        {"*": "ask", "edit": "deny"},
        {"*": "ask", "bash": {"rm *": "deny"}},
    ],
    ids=[
        "bare",
        "star",
        "outranked-allow",
        "asks-and-crew-deny",
        "operator-deny-after",
        "pattern-deny-after",
    ],
)
def test_every_call_asking_or_denied_is_ask(value) -> None:
    """The last ``*``/``*`` rule is the floor; asks/denies after it gate or exclude calls."""
    assert mimocode_mod._mimocode_posture(_rules(value), ("srv_tool",)) == "ask"


@pytest.mark.parametrize(
    ("value", "observed"),
    [
        ("allow", "allow"),
        ({"bash": "ask", "*": "allow"}, "allow"),
        ({"*": "deny"}, "deny"),
    ],
    ids=["bare-allow", "star-allow-last", "star-deny"],
)
def test_a_floor_that_is_not_ask_is_what_is_in_force(value, observed) -> None:
    assert mimocode_mod._mimocode_posture(_rules(value), ()) == observed


@pytest.mark.parametrize(
    ("value", "spelled"),
    [
        ({"*": "ask", "webfetch": "allow"}, {"webfetch": "allow"}),
        ({"*": "ask", "bash": {"git *": "allow"}}, {"bash": {"git *": "allow"}}),
        ({"bash": "deny", "*": "ask"}, {"bash": "deny"}),
        ({"bash": {"rm *": "deny"}, "*": "ask"}, {"bash": {"rm *": "deny"}}),
        ({"bash": "ask"}, {"bash": "ask"}),
    ],
    ids=["allow-after", "pattern-allow-after", "deny-before", "pattern-deny-before", "no-floor"],
)
def test_an_unasked_call_or_a_cancelled_deny_is_quoted(value, spelled) -> None:
    """Quoted in config spelling: an allow after the floor (skips the host gate), an operator
    deny before it (made an ask), or no floor (agent defaults decide)."""
    observed = mimocode_mod._mimocode_posture(_rules(value), ())
    assert observed == json.dumps(spelled, sort_keys=True)


def test_crews_own_deny_before_the_floor_is_left_to_the_in_force_check() -> None:
    """A lower source's seeded deny before ``*`` is left to ``_mimocode_disabled`` (server
    withheld), not refused as the operator's."""
    rules = _rules({"srv_tool": "deny", "*": "ask"})
    assert mimocode_mod._mimocode_posture(rules, ("srv_tool",)) == "ask"
    assert mimocode_mod._mimocode_disabled(["srv_tool"], rules) == frozenset()


def test_an_empty_ruleset_reads_as_absent() -> None:
    assert mimocode_mod._mimocode_posture([], ()) is None


def test_an_agent_rule_is_appended_after_the_top_level_ones() -> None:
    """``agent/agent.ts:486``: agent rules come last and can outrank the seed."""
    resolved = {
        "permission": {"*": "ask"},
        "agent": {
            "plan": {"options": {}},
            "build": {"permission": {"bash": "allow"}},
            "legacy": {"permission": "ask", "mode": "primary"},
            "odd": "not-a-map",
        },
    }
    contexts = mimocode_mod._mimocode_rule_contexts(resolved, "permission")
    assert [(name, [tuple(r) for r in rules]) for name, rules in contexts] == [
        ("", [("*", "*", "ask")]),
        ("build", [("*", "*", "ask"), ("bash", "*", "allow")]),
        ("legacy", [("*", "*", "ask"), ("*", "*", "ask")]),
    ]


def test_an_agent_printed_with_an_empty_permission_adds_no_context() -> None:
    """Measured on 0.1.15: a model-only agent's ``permission: {}`` inherits top-level rules."""
    resolved = {"permission": {"*": "ask"}, "agent": {"build": {"model": "m", "permission": {}}}}
    assert [name for name, _ in mimocode_mod._mimocode_rule_contexts(resolved, "permission")] == [
        ""
    ]


@pytest.mark.parametrize("agents", [None, {}, [], "build", {"build": "allow"}], ids=repr)
def test_nothing_walkable_adds_no_agent_context(agents) -> None:
    resolved = {"permission": "ask", "agent": agents}
    assert len(mimocode_mod._mimocode_rule_contexts(resolved, "permission")) == 1


# ── The seed over an unusable ambient value


@pytest.mark.parametrize(
    ("ambient", "logged"),
    [
        ("not json at all", "is not valid JSON"),
        ('{"model": "probe/m"', "is not valid JSON"),
        ('"a string"', "is not a JSON object"),
        ("[1, 2]", "is not a JSON object"),
        ("null", "is not a JSON object"),
    ],
    ids=["text", "truncated-object", "string", "array", "null"],
)
@pytest.mark.parametrize("deny_rules", [(), ("srv_tool",)], ids=["no-denies", "denies"])
def test_an_unusable_ambient_value_yields_the_seed_alone(
    monkeypatch, caplog, ambient, logged, deny_rules
) -> None:
    """Dropped from the merge and logged; the seed is never withheld."""
    _clear_mimocode_env(monkeypatch)
    monkeypatch.setenv("MIMOCODE_CONFIG_CONTENT", ambient)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
        seed = json.loads(mimocode_mod._mimocode_routing_config(ACP_BACKEND_MIMOCODE, deny_rules))
    if deny_rules:
        assert seed == {"permission": {"*": "ask", "srv_tool": "deny"}}
    else:
        assert seed == {"permission": "ask"}
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        f"MIMOCODE_CONFIG_CONTENT {logged}, so this session's harness config carries "
        "only Crew's permission routing."
    ]


@pytest.mark.parametrize("ambient", [None, ""], ids=["unset", "empty"])
def test_no_ambient_value_is_merged_silently(monkeypatch, caplog, ambient) -> None:
    _clear_mimocode_env(monkeypatch)
    if ambient is not None:
        monkeypatch.setenv("MIMOCODE_CONFIG_CONTENT", ambient)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
        seed = json.loads(mimocode_mod._mimocode_routing_config(ACP_BACKEND_MIMOCODE))
    assert seed == {"permission": "ask"}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_an_ambient_permission_is_replaced_and_its_other_keys_kept(monkeypatch) -> None:
    _clear_mimocode_env(monkeypatch)
    monkeypatch.setenv(
        "MIMOCODE_CONFIG_CONTENT",
        json.dumps({"permission": {"*": "allow"}, "model": "probe/m", "mcp": {"x": {}}}),
    )
    seed = json.loads(mimocode_mod._mimocode_routing_config(ACP_BACKEND_MIMOCODE))
    assert seed == {"permission": "ask", "model": "probe/m", "mcp": {"x": {}}}


# ── What refusals quote from operator config

_TAINT = f"{_GITHUB_TOKEN_SHAPE} {_EXFIL_URL}"


@pytest.mark.parametrize(
    ("document", "branch"),
    [
        ({"permission": f"allow {_TAINT}"}, "'permission' reads"),
        ({"permission": {"*": "ask", f"x {_TAINT}": "allow"}}, "'permission' reads"),
        (
            {"permission": {"*": "ask"}, "agent": {"build": {"permission": f"allow {_TAINT}"}}},
            "agent 'build' adds rules after it",
        ),
        (
            {"permission": {"*": "ask"}, "agent": {f"x {_TAINT}": {"permission": "allow"}}},
            "adds rules after it: 'permission' reads 'allow'",
        ),
    ],
    ids=["top-level-value", "mixed-map-key", "agent-value", "agent-name"],
)
def test_the_observed_value_is_redacted_before_it_reaches_a_refusal(
    monkeypatch, tmp_path, document, branch
) -> None:
    (issue, _remedy), _seen, _client = _readback(monkeypatch, tmp_path, json.dumps(document))
    assert branch in issue, issue
    assert _GITHUB_TOKEN_SHAPE not in issue
    assert "evil.example.com/collect" not in issue
    assert "[REDACTED" in issue


def test_a_native_mount_name_is_redacted_in_its_refusal(monkeypatch, tmp_path) -> None:
    """A withheld server the harness mounts itself is named, scrubbed."""
    name = f"docs {_TAINT}"
    _clear_mimocode_env(monkeypatch)
    monkeypatch.setattr(
        mimocode_mod.subprocess_mod,
        "run",
        lambda argv, **kw: _answer(json.dumps({"permission": {"*": "ask"}, "mcp": {name: {}}})),
    )
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    client._session_mcp_unhonoured = frozenset({name})
    issue, remedy = mimocode_mod._verify_mimocode_routing(
        client,
        ACP_BACKEND_MIMOCODE,
        list(_ARGV),
        mimocode_mod._mimocode_routing_config(ACP_BACKEND_MIMOCODE),
    )
    assert issue.startswith("a switched-off tool on '")
    assert "MiMo Code mounts that server itself" in issue
    assert _GITHUB_TOKEN_SHAPE not in issue and "evil.example.com/collect" not in issue
    assert remedy.startswith("Remove that tool's key from your MiMo Code config")


class _ScrubbingSession:
    """Read-back session surface whose scrub records each input."""

    def __init__(self, tmp_path) -> None:
        self._spawn_work_dir = str(tmp_path)
        self._extra_env: dict[str, str] = {}
        self.seen: list[object] = []

    @staticmethod
    def _agent_env_for_spawn() -> dict[str, str]:
        return {}

    def _scrub_observed(self, value):
        self.seen.append(value)
        return "<scrubbed>" if value == "build" else value


def test_the_read_back_quotes_through_the_scrub_its_session_hands_it(monkeypatch, tmp_path) -> None:
    _clear_mimocode_env(monkeypatch)
    document = {"permission": {"*": "ask"}, "agent": {"build": {"permission": "allow"}}}
    monkeypatch.setattr(
        mimocode_mod.subprocess_mod, "run", lambda argv, **kw: _answer(json.dumps(document))
    )
    session = _ScrubbingSession(tmp_path)
    issue, _remedy = mimocode_mod._verify_mimocode_routing(
        session,
        ACP_BACKEND_MIMOCODE,
        list(_ARGV),
        mimocode_mod._mimocode_routing_config(ACP_BACKEND_MIMOCODE),
    )
    assert session.seen == ["ask", "allow", "build"], session.seen
    assert issue.startswith("agent '<scrubbed>' adds rules after it: ")
    assert "build" not in issue


# ── per tool mcp deny mimocode
# Opencode's mechanism (``test_per_tool_mcp_deny_single_binary.py``) on MiMo Code's
# launch and mirror: a switched-off tool becomes a ``deny`` on its harness tool id in
# the ``permission`` Crew seeds via ``MIMOCODE_CONFIG_CONTENT``; the server stays
# mounted. The read-back evaluates rules last-match-wins (0.1.15
# ``permission/evaluate.ts``); a rule not in force withholds its server. Fields are
# ``_mimocode_*``, never opencode's.

_CORE = {"command": "/opt/kirocrew", "args": ["mcp-core"]}
_CRON = {"command": "/opt/kirocrew", "args": ["mcp-cron"]}

# Every env name the read-back consults (refused relocation, seed, config paths, binary
# override, stripped and pinned names): operator exports must not change a test.
_DENY_ENV_NAMES = tuple(
    dict.fromkeys(
        (
            "MIMOCODE_HOME",
            "MIMOCODE_CONFIG_CONTENT",
            "MIMOCODE_CONFIG",
            "MIMOCODE_CONFIG_DIR",
            "MIMO_BIN",
            *mimocode_harness._MIMOCODE_STRIPPED_ENV,
            *(name for name, _value in mimocode_harness._MIMOCODE_PINNED_ENV),
            *(name for name, _why in mimocode_harness._MIMOCODE_REFUSED_ENV),
        )
    )
)


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """Agent specs from a temp directory."""
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
    managed = {"kirocrew-core": dict(_CORE), "kirocrew-cron": dict(_CRON)}
    monkeypatch.setattr(
        session_mcp,
        "managed_mcp_spec_entry",
        lambda name: dict(managed[name]) if name in managed else None,
    )
    monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
    return d


def _write_spec(agents_dir: Path, servers: dict, tools: list) -> None:
    spec = {"name": "kirocrew", "mcpServers": servers, "tools": tools}
    (agents_dir / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")


def _names(projection) -> list[str]:
    return [str(e.get("name")) for e in projection.params["mcpServers"]]


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


class _OverBudget(Exception):
    """Raised by the step tracer past a glob walk's budget."""


def _unenforced(resolved: dict, rules) -> frozenset:
    """The read-back's in-force check on a resolved document."""
    contexts = mimocode_harness._mimocode_rule_contexts(resolved, "permission")
    return mimocode_harness._mimocode_unenforced_denies(contexts, rules)


class TestMimoCodeRuleEvaluation:
    def test_the_wildcard_is_the_harnesss(self):
        match = mimocode_harness._mimocode_wildcard
        assert match("probe_secret", "*")
        assert match("probe_secret", "probe_*")
        assert match("probe_secret", "probe_secre?")
        assert not match("probe_secret", "probe")
        assert not match("probe.secret", "probe_secret")
        # The trailing " *" also matches the bare prefix (``wildcard.ts``).
        assert match("ls", "ls *")

    def test_the_wildcard_ignores_case_on_windows_only(self, monkeypatch):
        """The ``i`` flag ``util/wildcard.ts:17`` adds on win32."""
        match = mimocode_harness._mimocode_wildcard
        monkeypatch.setattr(mimocode_harness.sys, "platform", "linux")
        assert not match("SRV_Tool", "srv_tool")
        monkeypatch.setattr(mimocode_harness.sys, "platform", "win32")
        assert match("SRV_Tool", "srv_tool")

    @staticmethod
    def _harness_regex(value: str, pattern: str) -> bool:
        """0.1.15 ``util/wildcard.ts`` ``match`` regex: oracle for short inputs only."""
        specials = set(".+^${}()|[]\\")
        value = value.replace("\\", "/")
        pattern = pattern.replace("\\", "/")
        body = "".join(
            ".*" if c == "*" else "." if c == "?" else "\\" + c if c in specials else c
            for c in pattern
        )
        if body.endswith(" .*"):
            body = body[:-3] + "( .*)?"
        return re.fullmatch(body, value, re.DOTALL) is not None

    def test_the_glob_agrees_with_the_harness_regex(self):
        """Small-alphabet patterns against every short value."""
        rng = random.Random(7)
        alphabet = "ab*? \\.\n"
        values = ["".join(t) for n in range(5) for t in itertools.product("ab .\n", repeat=n)]
        patterns = [
            "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 7))) for _ in range(600)
        ]
        patterns += ["ls *", "a *", " *", "*", "?", "a?b*", "\\*", "a\\b", "**", "a**b", "*a*"]
        match = mimocode_harness._mimocode_wildcard
        for pattern in patterns:
            for value in values:
                assert match(value, pattern) == self._harness_regex(value, pattern), (
                    value,
                    pattern,
                )

    @staticmethod
    def _glob_steps(call, budget: int) -> tuple[object, int]:
        """Run *call*, counting segment-matcher lines (not time, so CI load cannot skew it):
        0 means no walk; past *budget* it stops (``None``, ``budget + 1``) so backtracking
        fails the bound instead of running exponentially."""
        codes = {
            mimocode_harness._mimocode_segments_match.__code__,
            mimocode_harness._mimocode_literal_at.__code__,
        }
        steps = 0

        def tracer(frame, event, _arg):
            nonlocal steps
            if frame.f_code not in codes:
                return None
            if event == "line":
                steps += 1
                if steps > budget:
                    raise _OverBudget
            return tracer

        result: object = None
        previous = sys.gettrace()
        sys.settrace(tracer)
        try:
            result = call()
        except _OverBudget:
            result = None
        finally:
            sys.settrace(previous)
        return result, steps

    @staticmethod
    def _bound(value: str, pattern: str) -> int:
        """Backtrack-free line budget, constant per (value, pattern) cell; backtracking
        ``*?``x40 needs exponentially more."""
        return 12 * (len(value) + 1) * (len(pattern) + 1)

    def _assert_the_match_walks_the_glob(self, call=None, expected: object = True) -> None:
        """A cheap *call* (default: one match) uses the segment walk. Checked first, so a
        regex-compiling matcher fails here, not by backtracking in ``re`` beyond any budget."""
        if call is None:
            call = functools.partial(mimocode_harness._mimocode_wildcard, "probe_secret", "probe_*")
        result, steps = self._glob_steps(call, 10_000)
        assert result == expected
        assert steps > 0

    def test_a_pathological_key_is_matched_in_bounded_steps(self):
        """``*?*?...Z`` costs a backtracking regex seconds (GIL held) on a 30-char id; the walk
        stays bounded and correct."""
        self._assert_the_match_walks_the_glob()
        pattern = "*?" * 40 + "Z"
        match = mimocode_harness._mimocode_wildcard
        for value, expected in (("a" * 30, False), ("a" * 45 + "Z", True), ("a" * 30 + "Z", False)):
            bound = self._bound(value, pattern)
            result, steps = self._glob_steps(lambda v=value: match(v, pattern), bound)
            assert 0 < steps <= bound, (value, steps)
            assert result is expected, value

    def test_the_step_count_grows_with_the_input_not_exponentially(self):
        """Doubling both sides at most quadruples work, plus slack."""
        self._assert_the_match_walks_the_glob()
        match = mimocode_harness._mimocode_wildcard
        small_value, small_pattern = "a" * 15, "*?" * 20 + "Z"
        large_value, large_pattern = "a" * 30, "*?" * 40 + "Z"
        small_bound = self._bound(small_value, small_pattern)
        large_bound = self._bound(large_value, large_pattern)
        _, small = self._glob_steps(lambda: match(small_value, small_pattern), small_bound)
        _, large = self._glob_steps(lambda: match(large_value, large_pattern), large_bound)
        # Bound each, or two budget-stopped walks read as a budget ratio.
        assert 0 < small <= small_bound, small
        assert large <= large_bound, large
        assert small <= large <= 6 * small, (small, large)

    def test_the_read_back_survives_a_pathological_operator_key(self):
        """Read-back rule evaluation reaches the same bounded walk."""
        benign = {"permission": {"*": "ask", "probe_secret": "deny"}}
        self._assert_the_match_walks_the_glob(
            lambda: _unenforced(benign, ["probe_secret"]), frozenset()
        )
        key = "*?" * 40 + "Z"
        for tool_id in ("probe_secret", "probe_" + "x" * 24):
            resolved = {"permission": {key: "allow", "*": "ask", tool_id: "deny"}}
            # One glob per rule tried, each in its own bound.
            bound = 4 * self._bound(tool_id, key)
            result, steps = self._glob_steps(
                lambda r=resolved, t=tool_id: _unenforced(r, [t]), bound
            )
            assert 0 < steps <= bound, (tool_id, steps)
            assert result == frozenset(), tool_id

    def test_a_trailing_deny_is_in_force(self):
        resolved = {"permission": {"bash": "allow", "*": "ask", "probe_secret": "deny"}}
        assert _unenforced(resolved, ["probe_secret"]) == frozenset()

    def test_a_deny_a_lower_source_put_before_the_star_is_not_in_force(self):
        resolved = {"permission": {"probe_secret": "deny", "*": "ask"}}
        assert _unenforced(resolved, ["probe_secret"]) == frozenset({"probe_secret"})

    def test_an_agent_override_is_appended_and_can_outrank(self):
        resolved = {
            "permission": {"*": "ask", "probe_secret": "deny"},
            "agent": {"build": {"permission": {"*": "ask"}}, "plan": {"model": "x"}},
        }
        assert _unenforced(resolved, ["probe_secret"]) == frozenset({"probe_secret"})

    def test_an_agent_with_no_rules_of_its_own_keeps_the_deny(self):
        """0.1.15 prints ``permission: {}`` for it; it inherits the seed."""
        resolved = {
            "permission": {"*": "ask", "probe_secret": "deny"},
            "agent": {"build": {"model": "x", "permission": {}}},
        }
        assert _unenforced(resolved, ["probe_secret"]) == frozenset()

    def test_a_later_pattern_rule_on_the_same_tool_unhides_it(self):
        resolved = {"permission": {"*": "ask", "probe_secret": "deny", "probe_*": {"x": "allow"}}}
        assert _unenforced(resolved, ["probe_secret"]) == frozenset({"probe_secret"})

    def test_the_seeded_rules_are_read_from_the_seed(self):
        seed = json.dumps(
            {"permission": {"*": "ask", "a_b": "deny", "c_d": {"x": "deny"}, "e_f": "ask"}}
        )
        assert mimocode_harness._mimocode_seeded_deny_rules(seed, "permission") == ("a_b",)
        assert (
            mimocode_harness._mimocode_seeded_deny_rules('{"permission": "ask"}', "permission")
            == ()
        )


class TestMimoCodeDisabled:
    """``Permission.disabled`` (0.1.15 ``permission/index.ts:702-722``): the last rule
    naming a grouped tool OR its group decides."""

    disabled = staticmethod(mimocode_harness._mimocode_disabled)

    def _rules(self, value):
        return mimocode_harness._mimocode_ruleset(value)

    def test_a_group_rule_hides_every_tool_in_it(self):
        rules = self._rules({"*": "ask", "edit": "deny", "read": "deny"})
        tools = ["edit", "write", "apply_patch", "multiedit", "read", "view_image", "bash"]
        assert self.disabled(tools, rules) == frozenset(set(tools) - {"bash"})

    def test_a_later_group_rule_re_enables_a_tool_its_own_rule_hid(self):
        rules = self._rules({"*": "ask", "write": "deny", "edit": "ask"})
        assert self.disabled(["write"], rules) == frozenset()

    def test_a_later_own_rule_wins_over_the_group(self):
        rules = self._rules({"*": "ask", "edit": "deny", "write": "ask"})
        assert self.disabled(["write", "multiedit"], rules) == frozenset({"multiedit"})

    def test_a_pattern_deny_does_not_hide(self):
        rules = self._rules({"*": "ask", "bash": {"rm *": "deny"}})
        assert self.disabled(["bash"], rules) == frozenset()

    def test_an_ungrouped_mcp_id_is_decided_by_its_own_name(self):
        rules = self._rules({"*": "ask", "srv_tool": "deny", "edit": "ask"})
        assert self.disabled(["srv_tool"], rules) == frozenset({"srv_tool"})


class TestMimoCodeReadBack:
    @pytest.fixture
    def client(self, tmp_path):
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
        c._session_harness_deny_rules = ("probe_secret",)
        return c

    def test_the_seed_puts_each_deny_after_the_star(self, client, monkeypatch):
        monkeypatch.delenv("MIMOCODE_CONFIG_CONTENT", raising=False)
        seed = json.loads(
            mimocode_harness._mimocode_routing_config(
                ACP_BACKEND_MIMOCODE, client._session_harness_deny_rules
            )
        )
        assert list(seed["permission"].items()) == [("*", "ask"), ("probe_secret", "deny")]

    def test_no_rules_keeps_the_plain_ask(self, monkeypatch):
        monkeypatch.delenv("MIMOCODE_CONFIG_CONTENT", raising=False)
        assert (
            json.loads(mimocode_harness._mimocode_routing_config(ACP_BACKEND_MIMOCODE))[
                "permission"
            ]
            == "ask"
        )

    def _read_back(self, client, monkeypatch, resolved: dict) -> tuple[str, str]:
        # Child env derives from the gateway's; MIMOCODE_HOME refuses pre-run. Clear every
        # consulted name and ``agent.env``.
        for name in _DENY_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        client._agent_env_snapshot = {}
        monkeypatch.setattr(
            mimocode_harness.subprocess_mod, "run", lambda *a, **k: _completed(json.dumps(resolved))
        )
        seed = json.dumps({"permission": {"*": "ask", "probe_secret": "deny"}})
        return mimocode_harness._verify_mimocode_routing(
            client, ACP_BACKEND_MIMOCODE, ["mimo", "debug", "config"], seed
        )

    def test_the_seeded_shape_passes_with_every_deny_in_force(self, client, monkeypatch):
        issue = self._read_back(
            client, monkeypatch, {"permission": {"*": "ask", "probe_secret": "deny"}}
        )
        assert issue == ("", "")
        assert client._mimocode_denies_unenforced == frozenset()

    def test_an_outranked_deny_is_recorded_on_mimos_own_field(self, client, monkeypatch):
        issue = self._read_back(
            client, monkeypatch, {"permission": {"probe_secret": "deny", "*": "ask"}}
        )
        assert issue == ("", "")
        assert client._mimocode_denies_unenforced == frozenset({"probe_secret"})
        assert client._opencode_denies_unenforced == frozenset()

    def test_an_outranked_deny_on_a_native_mount_refuses_the_session(self, client, monkeypatch):
        issue, remedy = self._read_back(
            client,
            monkeypatch,
            {"permission": {"probe_secret": "deny", "*": "ask"}, "mcp": {"probe": {}}},
        )
        assert issue == (
            "a switched-off tool on 'probe' is not denied in the resolved config, and "
            "MiMo Code mounts that server itself, where withholding it from Crew's array "
            "cannot reach"
        )
        assert remedy == (
            "Remove that tool's key from your MiMo Code config, or stop mounting the "
            f"server in {mimocode_harness._MIMOCODE_MCP_SOURCES}."
        )
        assert "~/.claude.json" in remedy and "MIMOCODE_DISABLE_CLAUDE_CODE_MCP" in remedy

    def test_an_agent_printed_with_an_empty_permission_starts(self, client, monkeypatch):
        """0.1.15 prints ``permission: {}`` for a model-only agent (``agent.build.model``,
        measured), which inherits the seed: it starts and the deny holds."""
        resolved = {
            "permission": {"*": "ask", "probe_secret": "deny"},
            "agent": {"build": {"model": "probe/m", "options": {}, "permission": {}}},
        }
        assert self._read_back(client, monkeypatch, resolved) == ("", "")
        assert client._mimocode_denies_unenforced == frozenset()

    def test_an_operators_deny_after_the_star_starts(self, client, monkeypatch):
        """A deny after the floor holds in MiMo Code: the call never runs."""
        resolved = {"permission": {"*": "ask", "probe_secret": "deny", "edit": "deny"}}
        assert self._read_back(client, monkeypatch, resolved) == ("", "")

    def test_an_agents_allow_after_the_star_refuses_and_names_the_agent(self, client, monkeypatch):
        resolved = {
            "permission": {"*": "ask", "probe_secret": "deny"},
            "agent": {"build": {"permission": {"bash": "allow"}}},
        }
        issue, _remedy = self._read_back(client, monkeypatch, resolved)
        assert issue.startswith("agent 'build' adds rules after it: 'permission' reads")
        assert '{"bash": "allow"}' in issue

    def test_a_native_mount_whose_deny_holds_starts(self, client, monkeypatch):
        issue = self._read_back(
            client,
            monkeypatch,
            {"permission": {"*": "ask", "probe_secret": "deny"}, "mcp": {"probe": {}}},
        )
        assert issue == ("", "")
        assert client._mimocode_native_mounts == frozenset({"probe"})

    def test_an_operators_own_outranked_deny_is_still_refused(self, client, monkeypatch):
        issue, remedy = self._read_back(
            client,
            monkeypatch,
            {"permission": {"bash": {"pwd": "deny"}, "*": "ask", "probe_secret": "deny"}},
        )
        # Quotes the operator deny "*" made an ask, plus the gate's outranking-source remedy;
        # no exec, parse or MIMOCODE_HOME refusal names either.
        assert issue == (
            '\'permission\' reads \'{"bash": {"pwd": "deny"}}\' rather than '
            "the required 'ask', so privileged tools would not ask"
        )
        assert remedy == (
            "MiMo Code resolved permission to something other than 'ask' even with Kiro "
            "Crew's own setting supplied, so a higher-precedence config source is "
            "overriding it. Remove that override to select this harness."
        )


class TestMimoCodeGroupAliasedToolIds:
    """0.1.15 ``disabled()`` judges ``read``/``view_image`` by ``read`` and the edit family
    by ``edit`` (``permission/index.ts:699-722``); a later group rule can re-enable a
    fused MCP id there, so its server is withheld whole."""

    def test_an_aliased_fused_id_gets_no_deny_rule(self):
        assert mimocode_mirror.mimocode_tool_id("view", "image") == "view_image"
        assert mimocode_mirror.mimocode_deny_rules({("view", "image"), ("apply", "patch")}) == ()
        assert mimocode_mirror.mimocode_deny_rules({("probe", "secret")}) == ("probe_secret",)

    def test_an_aliased_tool_withholds_its_server(self, agents_dir, tmp_path):
        _write_spec(
            agents_dir,
            {"view": {"command": "/bin/x", "disabledTools": ["image"]}},
            ["@view"],
        )
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
        assert "view" not in [e["name"] for e in c._resolve_session_mcp_servers()]
        assert c._session_harness_deny_rules == ()
        assert c._session_mcp_unhonoured == frozenset({"view"})


class TestMimoCodeDeniesInForce:
    """In-force denies come from the seed, never from intent."""

    @pytest.fixture
    def client(self, tmp_path):
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
        c._session_harness_deny_rules = ("probe_secret",)
        return c

    def test_a_seed_that_carries_the_rule_keeps_it(self, client):
        seed = json.dumps({"permission": {"*": "ask", "probe_secret": "deny"}})
        assert mimocode_harness.settle_mimocode_denies(client, seed) == frozenset()
        assert client._mimocode_denies_in_force == {"probe_secret"}
        assert client._opencode_denies_in_force is None

    def test_a_seed_that_left_the_rule_out_loses_it(self, client):
        assert mimocode_harness.settle_mimocode_denies(
            client, json.dumps({"permission": "ask"})
        ) == {"probe_secret"}
        assert client._mimocode_denies_in_force == frozenset()

    def test_an_outranked_rule_is_lost_too(self, client):
        seed = json.dumps({"permission": {"*": "ask", "probe_secret": "deny"}})
        client._mimocode_denies_unenforced = frozenset({"probe_secret"})
        assert mimocode_harness.settle_mimocode_denies(client, seed) == {"probe_secret"}

    def test_a_lost_rule_withholds_its_server_on_the_re_projection(self, agents_dir, tmp_path):
        _write_spec(
            agents_dir,
            {"narrowed": {"command": "/bin/x", "disabledTools": ["danger"]}},
            ["@narrowed"],
        )
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
        assert "narrowed" in [e["name"] for e in c._resolve_session_mcp_servers()]
        assert c._session_harness_deny_rules == ("narrowed_danger",)
        assert mimocode_harness.settle_mimocode_denies(c, json.dumps({"permission": "ask"})) == {
            "narrowed_danger"
        }
        assert "narrowed" not in [e["name"] for e in c._resolve_session_mcp_servers()]
        assert c._session_mcp_unhonoured == frozenset({"narrowed"})

    def test_an_opencode_field_does_not_narrow_a_mimocode_session(self, agents_dir, tmp_path):
        """The projection reads THIS session's harness in-force set."""
        _write_spec(
            agents_dir,
            {"narrowed": {"command": "/bin/x", "disabledTools": ["danger"]}},
            ["@narrowed"],
        )
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
        c._opencode_denies_in_force = frozenset()
        assert "narrowed" in [e["name"] for e in c._resolve_session_mcp_servers()]

    def test_a_re_projection_that_narrows_a_native_mount_refuses(self, tmp_path):
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
        c._mimocode_native_mounts = frozenset({"second"})
        c._session_mcp_unhonoured = frozenset({"second"})
        with pytest.raises(mimocode_harness.AcpToolGateUnroutable, match="second"):
            mimocode_harness.refuse_native_mount_of_unhonoured(c)

    def test_a_re_projection_that_narrows_only_array_servers_starts(self, tmp_path):
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
        c._mimocode_native_mounts = frozenset({"other"})
        c._session_mcp_unhonoured = frozenset({"second"})
        assert mimocode_harness.refuse_native_mount_of_unhonoured(c) is None
        # Overlap refuses: the start above is from disjoint sets, not an inert check.
        c._session_mcp_unhonoured = frozenset({"second", "other"})
        with pytest.raises(mimocode_harness.AcpToolGateUnroutable, match="'other'"):
            mimocode_harness.refuse_native_mount_of_unhonoured(c)

    def test_the_spawn_arm_settles_once_and_rechecks_after_the_re_projection(self):
        import inspect

        source = inspect.getsource(mimocode_harness)
        assert "lost = settle_mimocode_denies(session, self._config_content)" in source
        assert source.count("session._mimocode_denies_in_force = ") == 1
        arm = inspect.getsource(mimocode_harness.MimoCodeLaunch.resolve_spawn)
        arm = arm[arm.index("lost = settle_mimocode_denies(") :]
        assert arm.index("session._resolve_session_mcp_servers") < arm.index(
            "refuse_native_mount_of_unhonoured(session)"
        )

    def test_a_narrowed_server_stays_mounted_with_its_rule(self, agents_dir):
        _write_spec(
            agents_dir,
            {
                "narrowed": {"command": "/bin/x", "disabledTools": ["danger"]},
                "plain": {"command": "/bin/y"},
            },
            ["@narrowed", "@plain"],
        )
        projection = mimocode_mirror.mimocode_projection("kirocrew")
        assert set(_names(projection)) == {"narrowed", "plain"}
        assert projection.harness_deny_rules == ("narrowed_danger",)
        assert projection.denied_tools == frozenset()
        assert projection.unhonoured_servers == frozenset()


# ── mimocode session mcp
# The session MCP array at the mirror layer, via :mod:`kiro_crew.providers.mirrors.mimocode`:
# element pass (:func:`mimocode_elements`), placement of both halves
# (:func:`place_mimocode_array`), tool id and deny-rule spelling, and the in-force set
# handed down. Measured adapter facts live in ``test/fixtures/acp_frames/mimocode``;
# this pins exact arrays.

#: Pinned port; never dialled or bound, only a control-plane ``KIROCREW_BOUND_PORT`` value.
_PORT = 0
_BOUND_PORT = {"name": "KIROCREW_BOUND_PORT", "value": str(_PORT)}


@pytest.fixture
def pinned_identity(monkeypatch):
    """Pin ``control_plane_identity_env``'s two call-time lookups (module attributes), so
    identity env is the session's values plus the pinned port, no host ``KIROCREW_HOME``."""
    monkeypatch.setattr(agent_mod, "_managed_mcp_env", lambda: {})
    monkeypatch.setattr(port_resolution, "resolve_serving_port", lambda: _PORT)


@pytest.fixture
def mcp_agents_dir(tmp_path, monkeypatch, pinned_identity):
    """Agent specs from a temp dir; materialization (rebuilding bundled defaults) is skipped as
    tests supply the spec, and a pinned managed source makes control-plane elements exact."""
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
    managed = {"kirocrew-core": dict(_CORE), "kirocrew-cron": dict(_CRON)}
    monkeypatch.setattr(
        session_mcp,
        "managed_mcp_spec_entry",
        lambda name: dict(managed[name]) if name in managed else None,
    )
    monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
    return d


def _write_mcp_spec(mcp_agents_dir: Path, *, servers: dict, tools: list) -> None:
    spec = {"name": "kirocrew", "mcpServers": servers, "tools": tools}
    (mcp_agents_dir / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")


def _stdio(name: str, command: str = "/bin/x", *, env: list | None = None) -> dict:
    """A stdio element as ``acp_server_element`` emits it, tag included."""
    return {"name": name, "command": command, "args": [], "env": list(env or []), "type": "stdio"}


def _untagged(name: str, command: str = "/bin/x", *, env: list | None = None) -> dict:
    """That element after this harness's stdio tag strip."""
    return {"name": name, "command": command, "args": [], "env": list(env or [])}


def _projection(
    servers: list[dict],
    *,
    refs: frozenset[str] | set[str] = frozenset(),
    grant_all: bool = False,
    muted: frozenset[str] = frozenset(),
) -> SessionMcpProjection:
    """A real projection with only placement's fields."""
    return SessionMcpProjection(
        servers=servers,
        restricted=frozenset(),
        disabled_tools=frozenset(),
        disabled_servers=muted,
        allowlist=ToolsAllowlist(
            applies=True, grant_all=grant_all, refs=frozenset(refs), muted=muted
        ),
    )


def _messages(caplog, level: int, logger: str = mirror_mod.__name__) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == logger and r.levelno == level]


# ── element pass: only the stdio tag goes


class TestTheStdioTagStrip:
    """Any ``type`` makes an element remote (0.1.15 ``acp/agent.ts:1146-1157``), so only the
    stdio tag goes; ``http``/``sse`` schemas match on it."""

    def test_a_stdio_element_loses_its_tag_and_remote_elements_keep_theirs(self):
        stdio = _stdio("local")
        http = {"name": "web", "type": "http", "url": "https://example.invalid/mcp", "headers": []}
        sse = {"name": "events", "type": "sse", "url": "https://example.invalid/sse", "headers": []}
        assert mimocode_elements([stdio, http, sse]) == [
            _untagged("local"),
            {"name": "web", "type": "http", "url": "https://example.invalid/mcp", "headers": []},
            {"name": "events", "type": "sse", "url": "https://example.invalid/sse", "headers": []},
        ]
        # A copy: the caller's element keeps its tag.
        assert stdio["type"] == "stdio"

    def test_an_untagged_stdio_element_passes_through_unchanged(self):
        assert mimocode_elements([_untagged("bare")]) == [_untagged("bare")]

    def test_a_non_dict_member_is_dropped(self):
        assert mimocode_elements([_stdio("ok"), "nope", None, 3]) == [  # type: ignore[list-item]
            _untagged("ok")
        ]


# ── identity carriage


class TestIdentityRidesOnlyTheControlPlane:
    """``KIROCREW_SESSION_KEY`` is a credential; only the two rebuilt managed elements carry it."""

    def test_the_control_plane_set_is_core_and_cron(self):
        assert CONTROL_PLANE_SERVERS == ("kirocrew-core", "kirocrew-cron")

    def test_the_identity_rides_the_control_plane_and_no_other_element(self, pinned_identity):
        third_party_env = [{"name": "A", "value": "1"}]
        elements = [
            _stdio("kirocrew-core", "/opt/kirocrew"),
            _stdio("kirocrew-cron", "/opt/kirocrew"),
            _stdio("third-party", env=third_party_env),
            # Managed Crew server outside the control plane: no identity.
            _stdio("kirocrew-dashboard", "/opt/kirocrew"),
            {"name": "web", "type": "http", "url": "https://example.invalid/mcp", "headers": []},
        ]
        identity = [
            {"name": "KIROCREW_STUB_SESSION_TOKEN", "value": "tok-1"},
            {"name": "KIROCREW_SESSION_KEY", "value": "sk-1"},
            {"name": "KIROCREW_CHANNEL_ID", "value": "ch-1"},
            _BOUND_PORT,
        ]
        out = mimocode_elements(
            elements, session_key="sk-1", channel_id="ch-1", session_token="tok-1"
        )
        assert out == [
            _untagged("kirocrew-core", "/opt/kirocrew", env=identity),
            _untagged("kirocrew-cron", "/opt/kirocrew", env=identity),
            _untagged("third-party", env=third_party_env),
            _untagged("kirocrew-dashboard", "/opt/kirocrew"),
            {"name": "web", "type": "http", "url": "https://example.invalid/mcp", "headers": []},
        ]

    def test_no_identity_is_resolved_when_all_three_inputs_are_empty(self, monkeypatch):
        """Never resolved: an empty key would pose as a credential, authenticating nothing."""

        def _refuse(*_args, **_kwargs):
            raise AssertionError("identity resolved for a session that has none")

        monkeypatch.setattr(mirror_mod, "control_plane_identity_env", _refuse)
        elements = [
            _stdio("kirocrew-core", "/opt/kirocrew"),
            _stdio("kirocrew-cron", "/opt/kirocrew"),
        ]
        assert mimocode_elements(elements) == [
            _untagged("kirocrew-core", "/opt/kirocrew"),
            _untagged("kirocrew-cron", "/opt/kirocrew"),
        ]

    @pytest.mark.parametrize(
        ("kwargs", "pair"),
        [
            ({"session_key": "sk-1"}, {"name": "KIROCREW_SESSION_KEY", "value": "sk-1"}),
            ({"channel_id": "ch-1"}, {"name": "KIROCREW_CHANNEL_ID", "value": "ch-1"}),
            ({"session_token": "tok-1"}, {"name": "KIROCREW_STUB_SESSION_TOKEN", "value": "tok-1"}),
        ],
        ids=["session_key", "channel_id", "session_token"],
    )
    def test_any_one_identity_input_carries_the_identity(self, pinned_identity, kwargs, pair):
        out = mimocode_elements(
            [_stdio("kirocrew-core", "/opt/kirocrew"), _stdio("plain")], **kwargs
        )
        assert out == [
            _untagged("kirocrew-core", "/opt/kirocrew", env=[pair, _BOUND_PORT]),
            _untagged("plain"),
        ]

    def test_a_stale_identity_pair_is_replaced_rather_than_duplicated(self, pinned_identity):
        stale = [{"name": "KIROCREW_SESSION_KEY", "value": "stale"}, {"name": "KEEP", "value": "1"}]
        core = _stdio("kirocrew-core", "/opt/kirocrew", env=stale)
        (out,) = mimocode_elements([core], session_key="fresh")
        assert out["env"] == [
            {"name": "KEEP", "value": "1"},
            {"name": "KIROCREW_SESSION_KEY", "value": "fresh"},
            _BOUND_PORT,
        ]
        assert core["env"][0] == {"name": "KIROCREW_SESSION_KEY", "value": "stale"}

    def test_the_identity_is_resolved_once_under_the_mimocode_label(self, monkeypatch):
        seen: list[tuple[str, str, str, str]] = []

        def _spy(session_key, channel_id, *, label, session_token=""):
            seen.append((session_key, channel_id, label, session_token))
            return {"X": "1"}

        monkeypatch.setattr(mirror_mod, "control_plane_identity_env", _spy)
        elements = [
            _stdio("kirocrew-core", "/opt/kirocrew"),
            _stdio("kirocrew-cron", "/opt/kirocrew"),
        ]
        out = mimocode_elements(
            elements, session_key="sk-1", channel_id="ch-1", session_token="tok-1"
        )
        assert seen == [("sk-1", "ch-1", "mimocode", "tok-1")]
        assert [e["env"] for e in out] == [[{"name": "X", "value": "1"}]] * 2

    def test_a_failed_lookup_warns_under_the_mimocode_label(self, monkeypatch, caplog):
        def _unavailable():
            raise RuntimeError("unavailable")

        monkeypatch.setattr(agent_mod, "_managed_mcp_env", _unavailable)
        monkeypatch.setattr(port_resolution, "resolve_serving_port", _unavailable)
        caplog.set_level(logging.WARNING, logger=identity_mod.__name__)
        (out,) = mimocode_elements([_stdio("kirocrew-core", "/opt/kirocrew")], session_key="sk-1")
        # Fail-soft: the session key rides; unresolvable parts are missing.
        assert out["env"] == [{"name": "KIROCREW_SESSION_KEY", "value": "sk-1"}]
        assert _messages(caplog, logging.WARNING, identity_mod.__name__) == [
            "mimocode session MCP: could not resolve the managed home",
            "mimocode session MCP: could not resolve the serving port",
        ]


# ── placement: withholding from both halves


class TestWithholding:
    def test_an_unhonoured_server_is_withheld_from_both_halves(self, caplog):
        """The same-named stub is the unrestricted server, withheld too; `tools` grants both."""
        caplog.set_level(logging.INFO, logger=mirror_mod.__name__)
        projection = _projection(
            [_stdio("narrowed"), _stdio("plain")], refs={"narrowed", "plain", "pooled"}
        )
        out = place_mimocode_array(
            projection,
            unhonoured=frozenset({"narrowed"}),
            stub_elements=[_stdio("narrowed", "/opt/stub"), _stdio("pooled", "/opt/stub")],
        )
        assert out == [_untagged("plain"), _untagged("pooled", "/opt/stub")]
        warnings = _messages(caplog, logging.WARNING)
        assert len(warnings) == 2, warnings
        assert warnings[0].startswith(
            "mimocode session MCP: withholding server 'narrowed' -- one of its tools is switched off"
        )
        assert warnings[0].endswith("This session runs without that server")
        assert warnings[1] == (
            "mimocode session MCP: withholding pooled stub 'narrowed' -- the projection withheld "
            "the server it wraps, and a stub re-adds it unrestricted"
        )
        assert _messages(caplog, logging.INFO) == []

    def test_an_unhonoured_control_plane_warns_that_it_cannot_report_back(
        self, pinned_identity, caplog
    ):
        caplog.set_level(logging.WARNING, logger=mirror_mod.__name__)
        projection = _projection(
            [_stdio("kirocrew-core", "/opt/kirocrew"), _stdio("kirocrew-cron", "/opt/kirocrew")],
            refs={"kirocrew-core", "kirocrew-cron"},
        )
        out = place_mimocode_array(
            projection,
            unhonoured=frozenset({"kirocrew-core"}),
            session_key="sk-1",
        )
        key = {"name": "KIROCREW_SESSION_KEY", "value": "sk-1"}
        assert out == [_untagged("kirocrew-cron", "/opt/kirocrew", env=[key, _BOUND_PORT])]
        (warning,) = _messages(caplog, logging.WARNING)
        assert warning.endswith(
            "This session runs without that server; with kirocrew-core withheld it cannot "
            "report back to its channel"
        )

    def test_crews_identity_bound_servers_are_withheld_from_both_halves(self, caplog):
        """Non-control-plane Crew servers come unreplaced from the spec, which cannot carry the
        session identity; `tools` grants all, so only the withhold rule drops them (core stays)."""
        caplog.set_level(logging.WARNING, logger=mirror_mod.__name__)
        bound = sorted(identity_bound_crew_servers())
        assert "kirocrew-dashboard" in bound, "the identity-bound set lost a known member"
        projection = _projection(
            [_stdio("kirocrew-core", "/opt/kirocrew")]
            + [_stdio(name, "/opt/kirocrew") for name in bound]
            + [_stdio("third-party")],
            grant_all=True,
        )
        out = place_mimocode_array(
            projection,
            unhonoured=frozenset(),
            stub_elements=[_stdio(bound[0], "/opt/stub")],
        )
        assert out == [_untagged("kirocrew-core", "/opt/kirocrew"), _untagged("third-party")]
        assert _messages(caplog, logging.WARNING) == [
            f"mimocode session MCP: withholding server {name!r} -- it is one of Crew's own "
            "servers, and an element the agent spec describes cannot carry this session's "
            "identity"
            for name in bound
        ] + [
            f"mimocode session MCP: withholding pooled stub {bound[0]!r} -- the projection "
            "withheld the server it wraps, and a stub re-adds it unrestricted"
        ]


# ── pooled-stub half


class TestPooledStubs:
    def test_a_stub_is_held_to_the_specs_tools_allowlist(self, caplog):
        caplog.set_level(logging.INFO, logger=mirror_mod.__name__)
        out = place_mimocode_array(
            _projection([], refs={"granted"}),
            unhonoured=frozenset(),
            stub_elements=[_stdio("granted", "/opt/stub"), _stdio("ungranted", "/opt/stub")],
        )
        assert out == [_untagged("granted", "/opt/stub")]
        assert _messages(caplog, logging.INFO) == [
            "mimocode session MCP: not mounting pooled stub 'ungranted' -- the agent spec's "
            "`tools` does not reference it, and the allowlist that filtered the translated "
            "half applies to a stub of the same name"
        ]
        assert _messages(caplog, logging.WARNING) == []

    def test_a_star_grant_still_refuses_a_server_switched_off_whole(self):
        out = place_mimocode_array(
            _projection([], grant_all=True, muted=frozenset({"off"})),
            unhonoured=frozenset(),
            stub_elements=[_stdio("off", "/opt/stub"), _stdio("on", "/opt/stub")],
        )
        assert out == [_untagged("on", "/opt/stub")]

    def test_a_stub_gets_the_stdio_strip_and_a_remote_stub_keeps_its_tag(self):
        stdio_stub = _stdio("pooled", "/opt/stub")
        remote_stub = {
            "name": "remote",
            "type": "sse",
            "url": "https://example.invalid/sse",
            "headers": [],
        }
        out = place_mimocode_array(
            _projection([], refs={"pooled", "remote"}),
            unhonoured=frozenset(),
            stub_elements=[stdio_stub, remote_stub],
        )
        assert out == [
            _untagged("pooled", "/opt/stub"),
            {"name": "remote", "type": "sse", "url": "https://example.invalid/sse", "headers": []},
        ]
        # A copy: a stub reused next session keeps its shape.
        assert stdio_stub["type"] == "stdio"

    def test_a_stub_carries_no_crew_identity_and_follows_the_translated_half(self, pinned_identity):
        out = place_mimocode_array(
            _projection(
                [_stdio("kirocrew-core", "/opt/kirocrew")], refs=set(CONTROL_PLANE_SERVERS)
            ),
            unhonoured=frozenset(),
            stub_elements=[_stdio("kirocrew-cron", "/opt/stub")],
            session_key="sk-1",
        )
        key = {"name": "KIROCREW_SESSION_KEY", "value": "sk-1"}
        assert out == [
            _untagged("kirocrew-core", "/opt/kirocrew", env=[key, _BOUND_PORT]),
            _untagged("kirocrew-cron", "/opt/stub"),
        ]

    def test_a_non_mapping_stub_is_skipped(self):
        out = place_mimocode_array(
            _projection([], grant_all=True),
            unhonoured=frozenset(),
            stub_elements=["nope", None, _stdio("pooled", "/opt/stub")],  # type: ignore[list-item]
        )
        assert out == [_untagged("pooled", "/opt/stub")]


# ── through the mirror, from a real spec


def _managed(name: str, env: list | None = None) -> dict:
    """A pinned managed control-plane element, tag stripped."""
    source = _CORE if name == "kirocrew-core" else _CRON
    return {
        "name": name,
        "command": source["command"],
        "args": list(source["args"]),
        "env": env or [],
    }


class TestThroughTheMirror:
    def test_every_transport_is_mounted_and_only_stdio_loses_its_tag(self, mcp_agents_dir):
        _write_mcp_spec(
            mcp_agents_dir,
            servers={
                "local": {"command": "/bin/x"},
                "web": {"url": "https://example.invalid/mcp"},
                "events": {"url": "https://example.invalid/sse", "type": "sse"},
            },
            tools=["@local", "@web", "@events"],
        )
        assert mimocode_projection("kirocrew").params["mcpServers"] == [
            {"name": "events", "type": "sse", "url": "https://example.invalid/sse", "headers": []},
            _untagged("local"),
            {"name": "web", "type": "http", "url": "https://example.invalid/mcp", "headers": []},
        ]

    def test_the_mirror_carries_the_identity_on_the_control_plane_only(self, mcp_agents_dir):
        _write_mcp_spec(
            mcp_agents_dir,
            servers={"third-party": {"command": "/bin/x", "env": {"A": "1"}}},
            tools=["@kirocrew-core", "@kirocrew-cron", "@third-party"],
        )
        params = MimoCodeMirror().session_params("kirocrew", session_key="sk-1", channel_id="ch-1")
        identity = [
            {"name": "KIROCREW_SESSION_KEY", "value": "sk-1"},
            {"name": "KIROCREW_CHANNEL_ID", "value": "ch-1"},
            _BOUND_PORT,
        ]
        assert params["mcpServers"] == [
            _managed("kirocrew-core", identity),
            _managed("kirocrew-cron", identity),
            _untagged("third-party", env=[{"name": "A", "value": "1"}]),
        ]

    def test_a_server_whose_rule_is_lost_cannot_return_as_its_stub(self, mcp_agents_dir, caplog):
        caplog.set_level(logging.WARNING, logger=mirror_mod.__name__)
        _write_mcp_spec(
            mcp_agents_dir,
            servers={
                "narrowed": {"command": "/bin/x", "disabledTools": ["danger"]},
                "plain": {"command": "/bin/y"},
            },
            tools=["@narrowed", "@plain"],
        )
        projection = MimoCodeMirror().session_projection(
            "kirocrew",
            stub_server_names=("narrowed",),
            stub_elements=[_stdio("narrowed", "/opt/stub")],
            harness_denies_in_force=frozenset(),
        )
        assert projection.params["mcpServers"] == [_untagged("plain", "/bin/y")]
        assert projection.unhonoured_servers == frozenset({"narrowed"})
        assert projection.restricted_servers == frozenset({"narrowed"})
        assert projection.harness_deny_rules == ("narrowed_danger",)
        warnings = _messages(caplog, logging.WARNING)
        assert len(warnings) == 2, warnings
        assert warnings[0].startswith(
            "mimocode session MCP: a switched-off tool on 'narrowed' has no deny rule in force"
        )
        assert warnings[1].startswith("mimocode session MCP: withholding pooled stub 'narrowed'")

    def test_the_same_stub_mounts_when_its_rule_is_in_force(self, mcp_agents_dir, caplog):
        caplog.set_level(logging.WARNING, logger=mirror_mod.__name__)
        _write_mcp_spec(
            mcp_agents_dir,
            servers={
                "narrowed": {"command": "/bin/x", "disabledTools": ["danger"]},
                "plain": {"command": "/bin/y"},
            },
            tools=["@narrowed", "@plain"],
        )
        projection = MimoCodeMirror().session_projection(
            "kirocrew",
            stub_server_names=("narrowed",),
            stub_elements=[_stdio("narrowed", "/opt/stub")],
            harness_denies_in_force=frozenset({"narrowed_danger"}),
        )
        assert projection.params["mcpServers"] == [
            _untagged("plain", "/bin/y"),
            _untagged("narrowed", "/opt/stub"),
        ]
        assert projection.unhonoured_servers == frozenset()
        assert projection.harness_deny_rules == ("narrowed_danger",)
        assert _messages(caplog, logging.WARNING) == []


class TestADisabledServerNeverMountsThroughItsStub:
    """MiMo Code's copy of ``test_acp_session_mcp.py::TestDisabledServersPooledStub``
    (claude, codex, opencode, goose): a WHOLE-off server never mounts via its
    pooled stub, whatever the source or `tools`; an enabled pooled one mounts."""

    @pytest.mark.parametrize("source", ["spec", "global"])
    @pytest.mark.parametrize("tools", [["@my-server", "@ok"], ["*"]], ids=["referenced", "star"])
    def test_mimocode_withholds_a_disabled_servers_stub(
        self, mcp_agents_dir, tmp_path, source, tools
    ):
        disabled = {"command": "/bin/x", "disabled": True}
        servers = {
            "my-server": dict(disabled) if source == "spec" else {"command": "/bin/x"},
            "ok": {"command": "/bin/ok"},
        }
        _write_mcp_spec(mcp_agents_dir, servers=servers, tools=tools)
        if source == "global":
            (tmp_path / "settings-mcp.json").write_text(
                json.dumps({"mcpServers": {"my-server": disabled}}), encoding="utf-8"
            )
        projection = MimoCodeMirror().session_projection(
            "kirocrew",
            stub_server_names=("my-server", "ok"),
            stub_elements=[_stdio("my-server", "/stub"), _stdio("ok", "/stub")],
            permission_surface_owned=True,
        )
        control_plane = (
            [_managed("kirocrew-core"), _managed("kirocrew-cron")] if tools == ["*"] else []
        )
        assert projection.params["mcpServers"] == control_plane + [_untagged("ok", "/stub")]
        assert "my-server" in projection.disabled_servers


# ── consuming the client's in-force rule set


def _two_narrowed(mcp_agents_dir: Path) -> None:
    _write_mcp_spec(
        mcp_agents_dir,
        servers={
            "narrowed": {"command": "/bin/x", "disabledTools": ["danger"]},
            "other": {"command": "/bin/y", "disabledTools": ["risky"]},
        },
        tools=["@narrowed", "@other"],
    )


class TestTheInForceSet:
    """Every collection type accepted for ``harness_denies_in_force`` (the read-back's
    answer) must withhold alike, whatever the client's spelling."""

    @pytest.mark.parametrize("kind", [list, tuple, set, frozenset], ids=lambda k: k.__name__)
    def test_every_collection_type_withholds_the_same_server(self, mcp_agents_dir, kind):
        _two_narrowed(mcp_agents_dir)
        projection = MimoCodeMirror().session_projection(
            "kirocrew", harness_denies_in_force=kind(["other_risky"])
        )
        assert projection.params["mcpServers"] == [_untagged("other", "/bin/y")]
        assert projection.unhonoured_servers == frozenset({"narrowed"})
        assert projection.restricted_servers == frozenset({"narrowed"})
        assert projection.harness_deny_rules == ("narrowed_danger", "other_risky")

    @pytest.mark.parametrize("kind", [list, tuple, set, frozenset], ids=lambda k: k.__name__)
    def test_an_empty_collection_of_every_type_withholds_every_narrowed_server(
        self, mcp_agents_dir, kind
    ):
        _two_narrowed(mcp_agents_dir)
        projection = MimoCodeMirror().session_projection("kirocrew", harness_denies_in_force=kind())
        assert projection.params["mcpServers"] == []
        assert projection.unhonoured_servers == frozenset({"narrowed", "other"})

    def test_none_is_the_seed_path_where_the_asked_rules_are_the_ones_in_force(
        self, mcp_agents_dir
    ):
        _two_narrowed(mcp_agents_dir)
        projection = MimoCodeMirror().session_projection("kirocrew", harness_denies_in_force=None)
        assert projection.params["mcpServers"] == [
            _untagged("narrowed"),
            _untagged("other", "/bin/y"),
        ]
        assert projection.unhonoured_servers == frozenset()
        assert projection.harness_deny_rules == ("narrowed_danger", "other_risky")

    def test_session_params_is_the_projections_array_and_ignores_the_permission_flag(
        self, mcp_agents_dir
    ):
        _write_mcp_spec(
            mcp_agents_dir,
            servers={"plain": {"command": "/bin/y"}},
            tools=["@plain", "@pooled"],
        )
        stubs = [_stdio("pooled", "/opt/stub")]
        mirror = MimoCodeMirror()
        params = mirror.session_params(
            "kirocrew",
            stub_server_names=("pooled",),
            stub_elements=stubs,
            permission_surface_owned=False,
        )
        assert params == {
            "mcpServers": [_untagged("plain", "/bin/y"), _untagged("pooled", "/opt/stub")]
        }
        assert (
            params
            == mirror.session_projection(
                "kirocrew", stub_server_names=("pooled",), stub_elements=stubs
            ).params
        )


# ── tool ids and deny rules


class TestToolIdAndDenyRules:
    """Tool ids are ``sanitize(server) + "_" + sanitize(tool)``, class ``[^a-zA-Z0-9_-]``
    (0.1.15 ``mcp/index.ts:361``); other spellings switch nothing off."""

    @pytest.mark.parametrize(
        ("server", "tool", "expected"),
        [
            ("probe-core", "secret", "probe-core_secret"),
            ("Srv_9-x", "Tool-1_y", "Srv_9-x_Tool-1_y"),
            ("a.b", "c d", "a_b_c_d"),
            ("s*", "t?", "s__t_"),
            ("a/b:c", "x@y", "a_b_c_x_y"),
            ("服务", "工具", "_____"),
            ("ops", "delete\U0001f600", "ops_delete__"),
            ("s\U0001f600", "t", "s___t"),
        ],
        ids=[
            "plain",
            "kept-class",
            "dot-space",
            "wildcards",
            "punctuation",
            "non-ascii",
            "astral-tool",
            "astral-server",
        ],
    )
    def test_both_halves_are_sanitized(self, server, tool, expected):
        """A character outside the BMP is two UTF-16 units, so two ``_``: the id the
        harness registers, and the one ``mimocode_spelling`` reads a title with."""
        assert mimocode_tool_id(server, tool) == expected
        assert mimocode_tool_id(server, tool) == (
            f"{mimocode_spelling(server)}_{mimocode_spelling(tool)}"
        )

    def test_a_rule_can_never_hold_a_wildcard(self):
        assert not set("*?") & set(mimocode_tool_id("*?*", "?*?"))

    def test_the_rules_are_sorted_unique_and_skip_ids_a_group_rule_decides(self):
        rules = mimocode_deny_rules(
            [
                ("zeta", "tool"),
                ("zeta", "tool"),
                # Two pairs fusing to one id: one rule denies both.
                ("a_b", "c"),
                ("a", "b_c"),
                ("m.x", "y"),
                # Fused ids in MiMo Code's edit or read group.
                ("view", "image"),
                ("apply", "patch"),
                # OpenCode's MCP resource tools join no group; own names decide.
                ("list.mcp", "resources"),
                ("read_mcp", "resource"),
            ]
        )
        assert rules == ("a_b_c", "list_mcp_resources", "m_x_y", "read_mcp_resource", "zeta_tool")

    def test_the_group_reachable_ids_are_the_grouped_tools_a_fused_id_can_spell(self):
        """0.1.15 ``permission/index.ts:699-700``; fused ids always hold ``_``."""
        assert mirror_mod.MIMOCODE_PERMISSION_GROUPS == {
            "edit": ("edit", "write", "apply_patch", "multiedit"),
            "read": ("read", "view_image"),
        }
        assert sorted(mirror_mod._GROUP_REACHABLE_TOOL_IDS) == ["apply_patch", "view_image"]
        for fused in mirror_mod._GROUP_REACHABLE_TOOL_IDS:
            server, _, tool = fused.partition("_")
            assert mimocode_tool_id(server, tool) == fused
            assert mimocode_deny_rules([(server, tool)]) == (), fused

    def test_no_disabled_tools_is_no_rules(self):
        assert mimocode_deny_rules(()) == ()
        assert mimocode_deny_rules(frozenset()) == ()


def test_a_logged_server_name_is_redacted_and_kept_on_one_line(caplog) -> None:
    """Agent-spec or stub names pass the package's one sink cleaner before logging."""
    caplog.set_level(logging.INFO, logger=mirror_mod.__name__)
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    name = f"srv-{secret}\ninjected"
    projection = _projection([_stdio(name)], refs={name})
    place_mimocode_array(
        projection,
        unhonoured=frozenset({name}),
        stub_elements=[_stdio(name, "/opt/stub")],
    )
    warnings = _messages(caplog, logging.WARNING)
    assert len(warnings) == 2, warnings
    for warning in warnings:
        assert secret not in warning
        assert "\n" not in warning
        assert "[REDACTED: credential]" in warning
