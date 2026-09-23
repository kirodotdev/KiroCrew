"""Toolbox-shim report: a reserved ``kirocrew-*`` entry launches the managed invocation.

An agent spec authored outside Kiro Crew (a package manager, a hand-edited file)
cannot spell the managed command: it embeds the data home and the installed
version, so the one portable spelling is the bare launcher ``kirocrew``, which
resolves through the shared Toolbox dispatcher -- a different file from the
versioned binary. ``gatewayd._spawns_own_control_plane`` compares the spawned
binary by realpath against the managed entry, so such a spec's control plane
mounted, listed its tools, and refused every call ``identity_unattested``.

The rewriter now re-derives the launch of every stubbed reserved name from the
managed source before it resolves, hashes or bakes anything, so the stub's
target, the daemon's ``KIROCREW_MCP_TARGET_*`` and the gate's expectation are
one value. These tests drive that path with the REAL managed entry and the REAL
gate, then pin the boundaries: unreserved names are untouched, an unresolvable
managed name is left as declared, and the fingerprint sees the managed launch.
"""

from __future__ import annotations

import json
import logging
import shlex
from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS
from kiro_crew.mcp_gateway import rewriter
from kiro_crew.mcp_gateway.gatewayd import _spawns_own_control_plane
from kiro_crew.mcp_gateway.hashing import decode_target_args, expand_stub_flags
from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER, _rewrite_single_spec

TOOLBOX_SHIM = {"command": "kirocrew", "args": ["mcp-core"]}
REAPED_PIN = {
    "command": "/opt/toolbox/tools/kirocrew/0.6.0.9/bin/kirocrew",
    "args": ["mcp-core"],
}


def _rewrite(spec: dict, tmp_path: Path, *, stub_servers: frozenset[str]) -> tuple[dict, int]:
    return _rewrite_single_spec(
        spec,
        stubs_dir=tmp_path / "stubs",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "wd",
        sandbox_mode="auto",
        approval_mode="interactive",
        stub_servers=stub_servers,
        pooling_enabled=True,
        forward_env=False,
    )


def _stub_launch(entry: dict) -> tuple[str, list[str]]:
    """The (command, args) a stub entry tells gatewayd to spawn."""
    args = expand_stub_flags(entry["args"])
    command = args[args.index("--target-command") + 1]
    encoded = next(a for a in args if a.startswith("--target-args-b64="))
    return command, decode_target_args(encoded.partition("=")[2])


@pytest.fixture
def managed_core() -> dict:
    entry = agent_mod.managed_mcp_spec_entry("kirocrew-core")
    assert entry is not None and Path(entry["command"]).is_absolute()
    return entry


@pytest.mark.parametrize("declared", [TOOLBOX_SHIM, REAPED_PIN], ids=["toolbox-shim", "reaped-pin"])
def test_a_hand_authored_control_plane_launches_the_managed_binary_and_passes_the_gate(
    tmp_path: Path, caplog, declared: dict, managed_core: dict
) -> None:
    """The ticket's reproduction, end to end: the spec says ``kirocrew`` (or a
    version-pinned path an upgrade has reaped); the stub must carry the managed
    command and args, and the daemon gate -- the real one, on that exact launch --
    must hand over the token."""
    spec = {"name": "team-config", "mcpServers": {"kirocrew-core": dict(declared)}}
    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.rewriter"):
        new_spec, wrapped = _rewrite(spec, tmp_path, stub_servers=frozenset({"kirocrew-core"}))

    assert wrapped == 1
    entry = new_spec["mcpServers"]["kirocrew-core"]
    assert entry.get(_WRAPPER_MARKER) is True
    command, args = _stub_launch(entry)
    assert (command, args) == (managed_core["command"], managed_core["args"])
    # Negative control: what the spec declared is not what will be spawned.
    assert command != declared["command"]
    # The daemon's verdict on the launch the stub now names -- the same
    # function, on the same tuple, with a clean child env.
    assert _spawns_own_control_plane("kirocrew-core", command, args, env={}, work_dir=tmp_path)
    # And the declared launch, had it reached the daemon, is still refused: the
    # repair is what closes the gap, not a relaxed gate.
    assert not _spawns_own_control_plane(
        "kirocrew-core", declared["command"], declared["args"], env={}, work_dir=tmp_path
    )
    # The operator is told, once, what was declared and what will run.
    said = [r.getMessage() for r in caplog.records if "reserved server" in r.getMessage()]
    assert len(said) == 1
    # The declared command is named; its arguments are counted, never printed.
    assert repr(declared["command"]) in said[0]
    assert f"with {len(declared['args'])} argument(s)" in said[0]
    assert managed_core["command"] in said[0]


def test_the_target_env_the_daemon_reads_names_the_managed_binary(
    tmp_path: Path, managed_core: dict
) -> None:
    """``_collect_target_env`` is what gatewayd's ``env_target_resolver`` spawns
    from; it must agree with the stub, or the PoolKey lies about the binary."""
    spec = {"name": "team-config", "mcpServers": {"kirocrew-core": dict(TOOLBOX_SHIM)}}
    new_spec, _ = _rewrite(spec, tmp_path, stub_servers=frozenset({"kirocrew-core"}))
    target_env: dict[str, str] = {}
    rewriter._collect_target_env(new_spec["mcpServers"], target_env)
    spawned = shlex.split(target_env["KIROCREW_MCP_TARGET_KIROCREW_CORE"])
    assert spawned == [managed_core["command"], *managed_core["args"]]


def test_the_repair_warning_never_prints_the_declared_arguments(
    tmp_path: Path, caplog, managed_core: dict
) -> None:
    """A hand-authored reserved entry may carry a token in ``args``; the warning
    persists in the gateway log, so it names the command and the argument count
    and never the argument values. The managed invocation (ours) is printed."""
    token = "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB"
    spec = {
        "name": "team-config",
        "mcpServers": {"kirocrew-core": {**TOOLBOX_SHIM, "args": ["mcp-core", "--token", token]}},
    }
    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.rewriter"):
        _rewrite(spec, tmp_path, stub_servers=frozenset({"kirocrew-core"}))
    said = [r.getMessage() for r in caplog.records if "reserved server" in r.getMessage()]
    assert len(said) == 1, said
    assert token not in said[0]
    assert "--token" not in said[0]
    assert "with 3 argument(s)" in said[0]
    assert repr(TOOLBOX_SHIM["command"]) in said[0]
    assert managed_core["command"] in said[0]


def test_reserved_entry_spec_keys_are_the_allow_half_of_the_managed_entry_keys() -> None:
    """``_RESERVED_ENTRY_SPEC_KEYS`` mirrors ``agent._MANAGED_MCP_ENTRY_KEYS`` minus
    the keys the repair sources from the managed declaration or the ownership
    rule; pinned so a restriction field added to ``agent.py`` cannot drift past
    the repair silently."""
    owned_by_repair = {"command", "args", "autoApprove", "env"}
    assert (
        set(rewriter._RESERVED_ENTRY_SPEC_KEYS)
        == agent_mod._MANAGED_MCP_ENTRY_KEYS - owned_by_repair
    )
    assert len(set(rewriter._RESERVED_ENTRY_SPEC_KEYS)) == len(rewriter._RESERVED_ENTRY_SPEC_KEYS)


def test_the_gates_argv_denial_names_a_count_not_the_spawned_values(tmp_path: Path) -> None:
    """The denial reason rides into the identity_unattested refusal, so a token in
    a hand-authored argv must not appear in it; the managed argv (ours) may."""
    managed = agent_mod.managed_mcp_spec_entry("kirocrew-core", include_opt_in=True)
    assert managed and managed.get("command")
    token = "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB"
    denial: list[str] = []
    assert not _spawns_own_control_plane(
        "kirocrew-core",
        managed["command"],
        [*managed.get("args", []), "--token", token],
        env={},
        work_dir=tmp_path,
        denial=denial,
    )
    assert denial and "differ from spec" in denial[0]
    assert token not in denial[0]
    assert f"args ({len(managed.get('args', [])) + 2})" in denial[0]


def test_a_settings_injected_reserved_name_is_repaired_like_a_spec_one(
    managed_core: dict,
) -> None:
    """``settings/mcp.json`` is the other source of a stubbed reserved name; the
    injection set must carry the managed launch too, or the same user config
    yields a working control plane on one backend and a denied one on the other."""
    settings = {"mcpServers": {"kirocrew-core": dict(TOOLBOX_SHIM)}}
    out = rewriter._injectable_settings_servers(settings, frozenset({"kirocrew-core"}))
    assert set(out) == {"kirocrew-core"}
    assert out["kirocrew-core"]["command"] == managed_core["command"]
    assert out["kirocrew-core"]["args"] == managed_core["args"]


def test_a_spec_already_naming_the_managed_launch_is_not_rewritten_or_warned(
    tmp_path: Path, caplog, managed_core: dict
) -> None:
    """Kiro Crew's own ``kirocrew.json`` carries the managed launch; it must
    round-trip silently -- the warning is for hand-authored specs only."""
    spec = {
        "name": "kirocrew",
        "mcpServers": {
            "kirocrew-core": {"command": managed_core["command"], "args": managed_core["args"]}
        },
    }
    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.rewriter"):
        new_spec, wrapped = _rewrite(spec, tmp_path, stub_servers=frozenset({"kirocrew-core"}))
    assert wrapped == 1
    assert _stub_launch(new_spec["mcpServers"]["kirocrew-core"]) == (
        managed_core["command"],
        managed_core["args"],
    )
    assert not [r for r in caplog.records if "reserved server" in r.getMessage()]


@pytest.mark.parametrize(
    "bad_args", [8080, "--flag", {"a": 1}, True], ids=["int", "str", "dict", "bool"]
)
def test_a_scalar_args_on_a_reserved_name_does_not_abort_the_rewrite_pass(
    tmp_path: Path, bad_args: object, managed_core: dict
) -> None:
    """``"args": 8080`` is the hand edit ``_hashable_args`` and ``_normalized_env``
    already defend this file against: a TypeError out of ``_rewrite_single_spec``
    aborts the pass for EVERY agent and leaves the broker unstarted. The repair's
    comparison and the stub builder both read args through ``_spec_args``, so the
    entry is repaired to the managed launch and the pass completes; a sibling
    third-party entry with the same malformed shape is wrapped too."""
    spec = {
        "name": "agent-a",
        "mcpServers": {
            "kirocrew-core": {"command": "kirocrew", "args": bad_args},
            "third": {"command": managed_core["command"], "args": bad_args},
        },
    }
    new_spec, wrapped = _rewrite(spec, tmp_path, stub_servers=frozenset({"kirocrew-core", "third"}))
    assert wrapped == 2
    assert _stub_launch(new_spec["mcpServers"]["kirocrew-core"]) == (
        managed_core["command"],
        managed_core["args"],
    )
    assert _stub_launch(new_spec["mcpServers"]["third"]) == (managed_core["command"], [])


def test_spec_args_reads_only_a_sequence() -> None:
    assert rewriter._spec_args({"args": ["a", 1, None]}) == ["a", "1", "None"]
    assert rewriter._spec_args({"args": ("a",)}) == ["a"]
    for bad in (8080, "--flag", {"a": 1}, True, None):
        assert rewriter._spec_args({"args": bad}) == []
    assert rewriter._spec_args({}) == []


def test_an_unreserved_name_keeps_the_command_it_declared(tmp_path: Path) -> None:
    """The repair is keyed on the reserved set, never on the command: a
    third-party server that happens to launch ``kirocrew`` is the operator's
    business and is resolved as declared."""
    spec = {"name": "agent-a", "mcpServers": {"my-tool": dict(TOOLBOX_SHIM)}}
    out = rewriter._repair_control_plane_entry("my-tool", spec["mcpServers"]["my-tool"], "agent-a")
    assert out == TOOLBOX_SHIM
    assert "my-tool" not in KIROCREW_BIN_MCP_SERVERS


def test_a_reserved_name_the_managed_source_cannot_resolve_is_left_as_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``None`` from the managed source (closed ``spec_gate``, unresolvable
    invocation) means there is nothing to repair TO. The entry stays as declared
    and the daemon's gate rules on it, as before -- the rewriter never invents a
    launch."""
    monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", lambda name, **_kw: None)
    declared = dict(TOOLBOX_SHIM)
    out = rewriter._repair_control_plane_entry("kirocrew-core", declared, "agent-a")
    assert out is declared


def test_the_repaired_entry_is_composed_from_the_managed_source_outward(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The repaired entry is an allow-list over the managed launch, not a copy of
    the spec with two keys swapped. Restriction fields (mute, disabledTools,
    timeout, type) and an ordinary declared variable are the spec's and carry
    over; ``autoApprove`` is dropped (kiro-cli honours it BEFORE the PreToolUse
    gate, so a hand-written grant on a reserved name would approve our tools
    ungoverned); any other key is dropped and named."""
    managed = {"command": "/opt/crew/bin/kirocrew", "args": ["mcp-core"]}
    monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", lambda name, **_kw: dict(managed))
    monkeypatch.setattr(agent_mod, "_MANAGED_MCP_SERVERS", {"kirocrew-core": {}})
    declared = {
        **TOOLBOX_SHIM,
        "env": {"MY_FLAG": "1"},
        "autoApprove": ["*"],
        "disabledTools": ["workflow_run"],
        "disabled": False,
        "timeout": 30,
        "poolable": True,
        "somethingElse": {"x": 1},
    }
    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.rewriter"):
        out = rewriter._repair_control_plane_entry("kirocrew-core", declared, "agent-a")
    assert out == {
        "command": managed["command"],
        "args": managed["args"],
        "env": {"MY_FLAG": "1"},
        "disabledTools": ["workflow_run"],
        "disabled": False,
        "timeout": 30,
    }
    assert "autoApprove" not in out
    assert declared["command"] == "kirocrew", "the caller's dict is not mutated"
    said = [r.getMessage() for r in caplog.records]
    assert any("dropping 'somethingElse'" in m for m in said)
    assert any("declares autoApprove" in m for m in said)
    assert not any("'poolable'" in m for m in said), "the internal hint is dropped silently"


def test_a_spec_auto_approve_on_a_reserved_name_is_dropped_and_named(caplog) -> None:
    """kiro-cli honours ``autoApprove`` before Kiro Crew's PreToolUse gate runs, so a
    hand-written grant on a reserved name would approve our tools past the
    governance gate. Nothing is applied in its place: no managed declaration
    carries one today, and a fresh build writes none."""
    with pytest.MonkeyPatch.context() as mp:
        managed = {"command": "/opt/crew/bin/kirocrew", "args": ["mcp-core"]}
        mp.setattr(agent_mod, "managed_mcp_spec_entry", lambda name, **_kw: dict(managed))
        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.rewriter"):
            out = rewriter._repair_control_plane_entry(
                "kirocrew-core", {**TOOLBOX_SHIM, "autoApprove": ["*"]}, "agent-a"
            )
    assert "autoApprove" not in out
    assert any("declares autoApprove" in r.getMessage() for r in caplog.records)
    for name in KIROCREW_BIN_MCP_SERVERS:
        assert "autoApprove" not in agent_mod._MANAGED_MCP_SERVERS.get(name, {}), name


@pytest.mark.parametrize("launch", ["shim", "managed"], ids=["repaired-launch", "exact-launch"])
def test_the_repaired_entry_holds_its_env_to_the_managed_ownership_rule(
    monkeypatch: pytest.MonkeyPatch, caplog, launch: str
) -> None:
    """The launch is ours, so its environment answers to the rule the disk writer
    (``agent._enforce_managed_mcp_ownership``) and the ACP element
    (``session_mcp._managed_element_env``) already apply: Kiro Crew's reserved
    namespace, the loader channels, the home-deriving and launcher-exec keys are
    dropped; an ordinary variable stays. Otherwise a spec's
    ``KIROCREW_APPROVAL_MODE=auto`` reaches a tokened control plane and its
    subagents skip approval. Applies whether the launch needed repair or the spec
    copied the managed command exactly."""
    managed = {"command": "/opt/crew/bin/kirocrew", "args": ["mcp-core"]}
    monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", lambda name, **_kw: dict(managed))
    base = dict(TOOLBOX_SHIM) if launch == "shim" else dict(managed)
    declared = {
        **base,
        "env": {
            "KIROCREW_APPROVAL_MODE": "auto",
            "KIROCREW_HOME": "/elsewhere",
            "LD_PRELOAD": "/tmp/evil.so",
            "PYTHONPATH": "/tmp/shadow",
            "HOME": "/tmp/otherhome",
            "PATH": "/tmp/bin",
            "NODE_OPTIONS": "--require /tmp/x.js",
            "MY_FLAG": "1",
            # Malformed values are dropped, never stringified into live variables.
            "NOT_A_STRING": {"nested": 1},
            "NONE_VALUE": None,
        },
    }
    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.rewriter"):
        out = rewriter._repair_control_plane_entry("kirocrew-core", declared, "agent-a")
    assert out["command"] == managed["command"] and out["args"] == managed["args"]
    assert out["env"] == {"MY_FLAG": "1"}
    assert declared["env"]["KIROCREW_APPROVAL_MODE"] == "auto", "caller's dict not mutated"
    dropped = [r.getMessage() for r in caplog.records if "dropping" in r.getMessage()]
    assert any("'HOME'" in m for m in dropped) and any("'PATH'" in m for m in dropped)


def test_a_reserved_entry_whose_env_is_entirely_withheld_loses_the_env_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = {"command": "/opt/crew/bin/kirocrew", "args": ["mcp-core"]}
    monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", lambda name, **_kw: dict(managed))
    out = rewriter._repair_control_plane_entry(
        "kirocrew-core", {**managed, "env": {"KIROCREW_APPROVAL_MODE": "auto"}}, "agent-a"
    )
    assert "env" not in out
    # And an already-managed entry with no env is returned as-is (no churn).
    same = dict(managed)
    assert rewriter._repair_control_plane_entry("kirocrew-core", same, "agent-a") is same


def test_every_reserved_name_is_repaired_not_only_the_control_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The set is ``KIROCREW_BIN_MCP_SERVERS`` -- the same one the daemon hands a
    token to -- so a granted opt-in server such as ``kirocrew-dashboard`` gets the
    same repair; it is identity-bound for the same reason."""
    monkeypatch.setattr(
        agent_mod,
        "managed_mcp_spec_entry",
        lambda name, **_kw: {"command": "/opt/crew/bin/kirocrew", "args": [name]},
    )
    for name in KIROCREW_BIN_MCP_SERVERS:
        out = rewriter._repair_control_plane_entry(
            name, {"command": "kirocrew", "args": [name]}, "a"
        )
        assert out["command"] == "/opt/crew/bin/kirocrew", name


def test_the_fingerprint_sees_the_managed_launch_so_an_upgrade_regenerates_overlays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kirocrew upgrade moves the managed binary while every spec file stays
    byte-identical. The kept-overlay path must not keep serving a stub that
    hashes last version's path, so the launch is a fingerprint input."""
    source = tmp_path / "agents"
    source.mkdir()
    (source / "a.json").write_text(json.dumps({"name": "a", "mcpServers": {}}), encoding="utf-8")
    kwargs = dict(
        source_dir=source,
        settings_path=tmp_path / "mcp.json",
        overlay_dir=tmp_path / "overlay",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "wd",
        sandbox_mode="auto",
        approval_mode="interactive",
        stub_set=frozenset({"kirocrew-core", "kirocrew-cron"}),
        pooling_enabled=True,
        forward_env=False,
        identity_keys=(),
    )
    monkeypatch.setattr(
        agent_mod,
        "managed_mcp_spec_entry",
        lambda name, **_kw: {"command": "/toolbox/kirocrew/0.7.0.8/bin/kirocrew", "args": [name]},
    )
    before = rewriter._rewrite_inputs_fingerprint(**kwargs)
    monkeypatch.setattr(
        agent_mod,
        "managed_mcp_spec_entry",
        lambda name, **_kw: {"command": "/toolbox/kirocrew/0.7.0.9/bin/kirocrew", "args": [name]},
    )
    after = rewriter._rewrite_inputs_fingerprint(**kwargs)

    assert before["managed_control_plane"] == {
        "kirocrew-core": ["/toolbox/kirocrew/0.7.0.8/bin/kirocrew", ["kirocrew-core"]],
        "kirocrew-cron": ["/toolbox/kirocrew/0.7.0.8/bin/kirocrew", ["kirocrew-cron"]],
    }
    assert before != after
    assert {k for k in before if before[k] != after[k]} == {"managed_control_plane"}
    assert rewriter._FINGERPRINT_SCHEMA >= 9


# --- The denial reason reaches the session --------------------------------------
#
# Diagnosis on the ticket took three wrong turns because the one accurate line
# ("spawned X is not the spec's Y") lived only in the daemon's stdout. It now
# rides on the frames forwarded to the denied backend and is quoted by that
# backend's ``identity_unattested`` refusal.


def test_the_gate_reports_why_it_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = tmp_path / "kirocrew"
    launcher.write_text("#!/bin/sh\n")
    other = tmp_path / "toolbox-exec"
    other.write_text("#!/bin/sh\n")
    monkeypatch.setattr(
        agent_mod,
        "managed_mcp_spec_entry",
        lambda name, **_kw: {"command": str(launcher), "args": ["mcp-core"]},
    )
    from kiro_crew.mcp_gateway import gatewayd as gw

    denial: list[str] = []
    assert not gw._spawns_own_control_plane(
        "kirocrew-core", str(other), ["mcp-core"], env={}, denial=denial
    )
    assert denial == [f"spawned {str(other)!r} is not the spec's {str(launcher)!r}"]
    accepted: list[str] = []
    assert gw._spawns_own_control_plane(
        "kirocrew-core", str(launcher), ["mcp-core"], env={}, denial=accepted
    )
    assert accepted == []
    # A name outside the reserved set is not a denial and records nothing.
    quiet: list[str] = []
    assert not gw._spawns_own_control_plane("my-tool", str(other), [], env={}, denial=quiet)
    assert quiet == []


def test_a_denied_reserved_backend_is_told_why_and_never_handed_the_token() -> None:
    from types import SimpleNamespace

    from kiro_crew.mcp_caller import CallerContext
    from kiro_crew.mcp_gateway import gatewayd as gw

    base = CallerContext(session_key="dashboard:chat-69")
    conn = SimpleNamespace(stub_session_token="tok")
    reason = "spawned '/opt/local/bin/kirocrew' is not the spec's '/tb/kirocrew'"
    denied = SimpleNamespace(control_plane=False, control_plane_denial=reason)
    theirs = SimpleNamespace(control_plane=False, control_plane_denial="")
    ours = SimpleNamespace(control_plane=True, control_plane_denial="")

    handed = gw._caller_for_backend(denied, base, conn)  # type: ignore[arg-type]
    assert handed is not None and handed.identity_denial == reason
    assert handed.session_token == "", "a denial never comes with the token"
    assert base.identity_denial == "" and base.session_token == "", "base never mutated"
    assert gw._caller_for_backend(theirs, base, conn) is base  # type: ignore[arg-type]
    accepted = gw._caller_for_backend(ours, base, conn)  # type: ignore[arg-type]
    assert accepted is not None and accepted.session_token == "tok"
    assert accepted.identity_denial == ""


def test_the_denial_survives_the_meta_round_trip_and_is_absent_when_empty() -> None:
    from kiro_crew.mcp_caller import CALLER_META_KEY, CallerContext, build_caller_meta

    plain = build_caller_meta(CallerContext(session_key="k"))
    assert "identityDenial" not in plain[CALLER_META_KEY]
    reason = "child environment carries non-empty LD_PRELOAD"
    meta = build_caller_meta(CallerContext(session_key="k", identity_denial=reason))
    assert meta[CALLER_META_KEY]["identityDenial"] == reason
    parsed = CallerContext.from_meta(meta)
    assert parsed is not None and parsed.identity_denial == reason
    assert parsed.session_token == ""
