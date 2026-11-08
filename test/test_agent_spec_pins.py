"""``agent.spec_pins``: one explicit choice among same-name specs, honoured by every reader.

Two packages each install a differing spec under one declared name. With no pin every
reader refuses, naming each file and the ``kirocrew agent pin`` command for it. With a
pin every reader (the KAS projection, the tool-policy read, the member mount gate, the
model and flag lookups, the pooled overlay, the kiro-cli skill view) answers from the
pinned file, and a pin naming none of the claimants is refused without falling back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import agent_spec_pins
from kiro_crew.agent_discovery import (
    AgentSpecPinError,
    AmbiguousAgentSpecError,
    declared_spec_claim,
    spec_by_declared_name,
)

_HOOK_A = {"agentSpawn": [{"command": "metrics-hook --agent-package PkgA --event spawn"}]}
_HOOK_B = {"agentSpawn": [{"command": "metrics-hook --agent-source-package=PkgB"}]}


def _spawn(globs: list[str]) -> dict[str, object]:
    return {"subagent": {"availableAgents": globs}}


def _write(directory: Path, file_name: str, **spec: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / file_name
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


@pytest.fixture
def pins(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The live pin map, as a dict the test edits; read at each call like the config."""
    current: dict[str, str] = {}
    monkeypatch.setattr(agent_spec_pins, "spec_pins", lambda: dict(current))
    return current


@pytest.fixture
def contested(tmp_path: Path) -> Path:
    """Two packages' differing specs for ``helper``, plus an unrelated agent."""
    agents = tmp_path / "agents"
    _write(agents, "PkgA-helper.json", name="helper", prompt="A", model="model-a", hooks=_HOOK_A)
    _write(agents, "PkgB-helper.json", name="helper", prompt="B", model="model-b", hooks=_HOOK_B)
    _write(agents, "other.json", name="other", prompt="O")
    return agents


# -- the resolver -----------------------------------------------------------------


def test_no_pin_refuses_and_names_every_file_and_its_pin_command(contested, pins):
    with pytest.raises(AmbiguousAgentSpecError) as exc:
        spec_by_declared_name(contested, "helper", operation="t", source="test")
    message = str(exc.value)
    assert "with differing content" in message
    assert "  kirocrew agent pin helper PkgA-helper.json" in message
    assert "  kirocrew agent pin helper PkgB-helper.json" in message
    assert not isinstance(exc.value, AgentSpecPinError)
    assert exc.value.agent == "helper"
    assert {p.name: pkg for p, pkg in exc.value.packages.items()} == {
        "PkgA-helper.json": "PkgA",
        "PkgB-helper.json": "PkgB",
    }


def test_a_pin_selects_its_file_for_the_resolver(contested, pins):
    pins["helper"] = "PkgB-helper.json"
    claim = declared_spec_claim(contested, "helper", operation="t", source="test")
    assert claim is not None
    assert claim.path.name == "PkgB-helper.json" and claim.pinned and claim.divergent
    assert [p.name for p in claim.claimants] == ["PkgA-helper.json", "PkgB-helper.json"]
    spec = spec_by_declared_name(contested, "helper", operation="t", source="test")
    assert spec is not None and spec["prompt"] == "B"


@pytest.mark.parametrize("pin", ["Missing-helper.json", "other.json"])
def test_a_broken_pin_fails_closed_without_falling_back(contested, pins, pin):
    """A missing file, and a file declaring another name, both refuse: never PkgA."""
    pins["helper"] = pin
    with pytest.raises(AgentSpecPinError) as exc:
        spec_by_declared_name(contested, "helper", operation="t", source="test")
    assert exc.value.pin == pin
    assert "will not fall back" in str(exc.value)
    assert "kirocrew agent pin helper --clear" in str(exc.value)


def test_an_unreadable_pinned_file_fails_closed(contested, pins):
    (contested / "PkgB-helper.json").write_text("{not json", encoding="utf-8")
    _write(contested, "PkgC-helper.json", name="helper", prompt="C")
    pins["helper"] = "PkgB-helper.json"
    with pytest.raises(AgentSpecPinError):
        spec_by_declared_name(contested, "helper", operation="t", source="test")


def test_identical_claimants_without_a_pin_still_resolve(tmp_path, pins):
    agents = tmp_path / "agents"
    _write(agents, "PkgA-twin.json", name="twin", prompt="same")
    _write(agents, "PkgB-twin.json", name="twin", prompt="same")
    claim = declared_spec_claim(agents, "twin", operation="t", source="test")
    assert claim is not None and not claim.pinned and not claim.divergent
    assert claim.path.name == "PkgA-twin.json"


def test_an_unreadable_agents_dir_is_reported_not_read_as_empty(contested, monkeypatch):
    from kiro_crew import agent_spec_format, agent_spec_pin_ops

    def refuse(_directory):
        raise PermissionError("denied")

    monkeypatch.setattr(agent_spec_format, "iter_agent_spec_files_strict", refuse)
    with pytest.raises(agent_spec_pin_ops.PinRefused) as refused:
        agent_spec_pin_ops.check_pin_target(
            "helper", "PkgB-helper.json", source="cli", agents_dir=contested
        )
    assert refused.value.code == "agents_dir_unreadable"
    with pytest.raises(OSError):
        agent_spec_pin_ops.contested_agents(source="cli", agents_dir=contested)


def test_a_pin_with_no_claimant_left_refuses_before_any_filename_fallback(contested, pins):
    """No file declares the pinned name any more: that is a broken pin, never "no
    spec", so no reader falls back to a file named after the agent."""
    pins["helper"] = "PkgB-helper.json"
    for name in ("PkgA-helper.json", "PkgB-helper.json"):
        (contested / name).unlink()
    _write(contested, "helper.json", name="someone-else", prompt="X")
    with pytest.raises(AgentSpecPinError):
        spec_by_declared_name(contested, "helper", operation="t", source="test")


def test_a_pin_naming_the_lone_claimant_is_ignored(contested, pins):
    pins["other"] = "other.json"
    spec = spec_by_declared_name(contested, "other", operation="t", source="test")
    assert spec is not None and spec["prompt"] == "O"


def test_a_pin_whose_file_is_gone_refuses_the_lone_remaining_claimant(contested, pins):
    """Uninstalling the pinned file leaves one claimant, but that is not the file
    the operator chose: the start is refused, never run on the other file."""
    pins["helper"] = "PkgB-helper.json"
    (contested / "PkgB-helper.json").unlink()
    with pytest.raises(AgentSpecPinError) as exc:
        spec_by_declared_name(contested, "helper", operation="t", source="test")
    assert "--clear" in str(exc.value)


def test_pin_store_rejects_paths_and_traversal():
    assert agent_spec_pins.coerce_spec_pins(
        {
            "helper": "PkgA-helper.json",
            "md": "PkgA-md.md",
            "slash": "x/y.json",
            "back": "x\\y.json",
            "dots": "../x.json",
            "dir": "..",
            "suffix": "PkgA-helper.txt",
            "../bad": "PkgA.json",
            "num": 3,
        }
    ) == {"helper": "PkgA-helper.json", "md": "PkgA-md.md"}


# -- every reader -----------------------------------------------------------------


def test_the_kas_projection_follows_the_pin_and_a_pin_change_is_not_stale(contested, pins):
    from kiro_crew.acp.kas_agents import KasAgentTranslationError, load_agent_spec

    with pytest.raises(KasAgentTranslationError, match="kirocrew agent pin helper"):
        load_agent_spec(contested, "helper")
    pins["helper"] = "PkgA-helper.json"
    assert load_agent_spec(contested, "helper")["prompt"] == "A"
    # The directory is unchanged, so only the pin in the memo key makes this a miss.
    pins["helper"] = "PkgB-helper.json"
    assert load_agent_spec(contested, "helper")["prompt"] == "B"


def test_the_tool_policy_read_follows_the_pin(contested, pins):
    from kiro_crew.dashboard.handlers.sessions_parts.tool_policy import (
        _read_managed_tool_policy_sync,
    )

    spec_a = json.loads((contested / "PkgA-helper.json").read_text(encoding="utf-8"))
    spec_a["managedToolPolicy"] = {"kirocrew-core": {"exclude": ["a_only"]}}
    (contested / "PkgA-helper.json").write_text(json.dumps(spec_a), encoding="utf-8")
    with pytest.raises(AmbiguousAgentSpecError):
        _read_managed_tool_policy_sync(contested, "helper")
    pins["helper"] = "PkgA-helper.json"
    policy = _read_managed_tool_policy_sync(contested, "helper")
    assert policy is not None and "a_only" in json.dumps(policy)
    pins["helper"] = "PkgB-helper.json"
    assert "a_only" not in json.dumps(_read_managed_tool_policy_sync(contested, "helper"))


def test_the_tool_policy_reason_names_the_pin_command(contested, pins):
    from kiro_crew.dashboard.handlers.sessions_parts.tool_policy import _ambiguous_spec_reason

    with pytest.raises(AmbiguousAgentSpecError) as exc:
        spec_by_declared_name(contested, "helper", operation="t", source="test")
    reason = _ambiguous_spec_reason("helper", exc.value)
    assert "kirocrew agent pin helper PkgA-helper.json" in reason
    assert str(contested) not in reason


def test_agent_spec_path_follows_the_pin_only_for_readers(contested, pins):
    from kiro_crew.agent import agent_spec_path, session_agent_spec_path

    pins["helper"] = "PkgB-helper.json"
    assert session_agent_spec_path("helper", agents_dir=contested) == (
        contested / "PkgB-helper.json"
    )
    # A writer never follows a pin: the files belong to the package installer.
    with pytest.raises(AmbiguousAgentSpecError):
        agent_spec_path("helper", agents_dir=contested)
    pins["helper"] = "Missing.json"
    with pytest.raises(AgentSpecPinError):
        session_agent_spec_path("helper", agents_dir=contested)


def test_the_member_mount_gate_reads_the_pinned_spec(contested, pins, monkeypatch):
    from kiro_crew.acp import session_mcp

    real = session_mcp.session_agent_spec_path
    monkeypatch.setattr(
        session_mcp,
        "session_agent_spec_path",
        lambda name: real(name, agents_dir=contested),
    )
    assert session_mcp._user_agent_spec("helper") is None
    pins["helper"] = "PkgA-helper.json"
    spec = session_mcp._user_agent_spec("helper")
    assert spec is not None and spec["prompt"] == "A"


def test_first_match_readers_follow_the_pin(contested, pins):
    rows = [
        (json.loads(p.read_text(encoding="utf-8")), p) for p in sorted(contested.glob("*.json"))
    ]
    assert agent_spec_pins.pick_spec_row(iter(rows), "helper")["prompt"] == "A"
    pins["helper"] = "PkgB-helper.json"
    assert agent_spec_pins.pick_spec_row(iter(rows), "helper")["prompt"] == "B"
    pins["helper"] = "Missing.json"
    assert agent_spec_pins.pick_spec_row(iter(rows), "helper") is None


def test_the_model_readers_follow_the_pin(contested, pins, monkeypatch):
    from kiro_crew import session
    from kiro_crew.config.loader import KiroCrewConfig

    pins["helper"] = "PkgB-helper.json"
    assert KiroCrewConfig._resolve_named_agent_model("helper", agents_dir=contested) == "model-b"
    monkeypatch.setattr(session, "kiro_agents_dir_path", lambda: contested)
    if hasattr(session.SessionManager, "_agent_model_cache"):
        monkeypatch.setattr(session.SessionManager, "_agent_model_cache", {}, raising=False)
    assert session.SessionManager._resolve_agent_model("helper") == "model-b"
    # A pin change inside the cache's lifetime answers from the newly pinned file.
    pins["helper"] = "PkgA-helper.json"
    assert session.SessionManager._resolve_agent_model("helper") == "model-a"


def test_the_crew_context_flag_reader_follows_the_pin(contested, pins, monkeypatch):
    from kiro_crew import context
    from kiro_crew.context_assembly.agent_spec import _read_include_crew_context

    spec_b = json.loads((contested / "PkgB-helper.json").read_text(encoding="utf-8"))
    spec_b["includeCrewContext"] = False
    (contested / "PkgB-helper.json").write_text(json.dumps(spec_b), encoding="utf-8")
    monkeypatch.setattr(context, "kiro_agents_dir", lambda: contested)
    pins["helper"] = "PkgA-helper.json"
    assert _read_include_crew_context("helper") is True
    pins["helper"] = "PkgB-helper.json"
    assert _read_include_crew_context("helper") is False
    # The per-turn cache follows a pin change instead of keeping the first file's flag.
    monkeypatch.setattr(context, "_INCLUDE_CREW_CONTEXT_CACHE", {})
    assert context._agent_includes_crew_context("helper") is False
    pins["helper"] = "PkgA-helper.json"
    assert context._agent_includes_crew_context("helper") is True


def _user_claimants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *files: str) -> None:
    """User-level files declaring ``helper``, so the overlay lookup sees a contest."""
    from kiro_crew import agent

    agents = tmp_path / "user-agents"
    agents.mkdir()
    for file_name in files:
        if file_name.endswith(".md"):
            (agents / file_name).write_text("---\nname: helper\n---\nB\n", encoding="utf-8")
        else:
            _write(agents, file_name, name="helper", prompt=file_name)
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: agents)


def test_the_pooled_overlay_stem_is_the_pinned_file(tmp_path, pins, monkeypatch):
    from kiro_crew.mcp_gateway.session_servers import _load_overlay_for_agent

    _user_claimants(tmp_path, monkeypatch, "PkgA-helper.json", "PkgB-helper.json")
    overlay = tmp_path / "overlay"
    _write(overlay, "helper.json", name="helper", mcpServers={"from": {"command": "direct"}})
    _write(overlay, "PkgB-helper.json", name="helper", mcpServers={"from": {"command": "B"}})
    assert _load_overlay_for_agent(overlay, "helper")["mcpServers"]["from"]["command"] == "direct"
    pins["helper"] = "PkgB-helper.json"
    assert _load_overlay_for_agent(overlay, "helper")["mcpServers"]["from"]["command"] == "B"


def test_a_markdown_pin_pools_the_overlay_named_after_its_stem(tmp_path, pins, monkeypatch):
    from kiro_crew.mcp_gateway.session_servers import _load_overlay_for_agent

    _user_claimants(tmp_path, monkeypatch, "PkgA-helper.json", "PkgB-helper.md")
    # The rewriter writes ``PkgB-helper.md``'s overlay as ``PkgB-helper.json``.
    overlay = tmp_path / "overlay"
    _write(overlay, "PkgA-helper.json", name="helper", mcpServers={"from": {"command": "A"}})
    _write(overlay, "PkgB-helper.json", name="helper", mcpServers={"from": {"command": "B"}})
    pins["helper"] = "PkgB-helper.md"
    assert _load_overlay_for_agent(overlay, "helper")["mcpServers"]["from"]["command"] == "B"


@pytest.mark.parametrize("pinned_overlay", [None, "{not json", {"name": "someone-else"}])
def test_a_pin_without_a_usable_overlay_pools_nothing(tmp_path, pins, pinned_overlay, monkeypatch):
    from kiro_crew.mcp_gateway.session_servers import _load_overlay_for_agent

    _user_claimants(tmp_path, monkeypatch, "PkgA-helper.json", "PkgB-helper.md")
    overlay = tmp_path / "overlay"
    _write(overlay, "helper.json", name="helper", mcpServers={"from": {"command": "direct"}})
    _write(overlay, "PkgA-helper.json", name="helper", mcpServers={"from": {"command": "A"}})
    if isinstance(pinned_overlay, str):
        (overlay / "PkgB-helper.json").write_text(pinned_overlay, encoding="utf-8")
    elif pinned_overlay is not None:
        _write(overlay, "PkgB-helper.json", **pinned_overlay)
    pins["helper"] = "PkgB-helper.md"
    assert _load_overlay_for_agent(overlay, "helper") is None


def test_the_parent_spawn_allowlist_is_the_pinned_parents(tmp_path, pins):
    from kiro_crew.subagent_parts.agent_policy import _scan_parent_spawn_allowlists

    agents = tmp_path / "agents"
    _write(agents, "PkgA-lead.json", name="lead", toolsSettings=_spawn(["alpha"]))
    _write(agents, "PkgB-lead.json", name="lead", toolsSettings=_spawn(["beta"]))
    both, _ = _scan_parent_spawn_allowlists(agents, "lead", "spawn_available_agents")
    assert both is not None and len(both) == 2
    pins["lead"] = "PkgB-lead.json"
    pinned, _ = _scan_parent_spawn_allowlists(agents, "lead", "spawn_available_agents")
    assert pinned is not None and len(pinned) == 1 and "beta" in json.dumps(pinned)
    # A pin chooses among user-level files only: the project scan keeps every
    # claimant's allowlist even when a project file shares the pinned name.
    project, _ = _scan_parent_spawn_allowlists(agents, "lead", "spawn_available_agents_project")
    assert project is not None and len(project) == 2


def test_a_broken_pin_leaves_the_parent_spawn_allowlist_unknown(tmp_path, pins):
    from kiro_crew.subagent_parts.agent_policy import _scan_parent_spawn_allowlists

    agents = tmp_path / "agents"
    _write(agents, "PkgA-lead.json", name="lead", toolsSettings=_spawn(["alpha"]))
    _write(agents, "PkgB-lead.json", name="lead")
    pins["lead"] = "Missing.json"
    # Neither the other claimant's (absent) list nor a filename fallback stands in.
    assert _scan_parent_spawn_allowlists(agents, "lead", "spawn_available_agents") == (
        None,
        False,
    )


# -- kiro-cli ---------------------------------------------------------------------


def test_kiro_cli_launch_on_the_authored_name_is_refused_when_contested(
    contested, pins, monkeypatch
):
    from kiro_crew import agent as agent_mod
    from kiro_crew.agent_materialization import fork_refresh

    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: contested)
    with pytest.raises(fork_refresh.AgentSpecLaunchUnresolved) as exc:
        fork_refresh.require_launch_resolves_one_file("helper", "helper")
    assert isinstance(exc.value.__cause__, AmbiguousAgentSpecError)
    pins["helper"] = "PkgB-helper.json"
    # Pinned, but no view: kiro-cli would choose the file itself.
    with pytest.raises(fork_refresh.AgentSpecLaunchUnresolved, match="could not be handed"):
        fork_refresh.require_launch_resolves_one_file("helper", None)
    # A skill view (any Crew-owned name other than the authored one) passes.
    fork_refresh.require_launch_resolves_one_file("helper", "kirocrew-view-abc")
    # An uncontested name passes on its authored name.
    fork_refresh.require_launch_resolves_one_file("other", "other")


def test_a_project_spec_shadows_contested_user_level_claimants(
    contested, pins, monkeypatch, tmp_path
):
    from kiro_crew import agent
    from kiro_crew.agent_materialization import fork_refresh

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: contested)
    checkout = tmp_path / "checkout"
    _write(checkout / ".kiro" / "agents", "helper.json", name="helper", prompt="project")
    # kiro-cli runs the checkout's own spec, so the user-level contest chooses nothing.
    fork_refresh.require_launch_resolves_one_file("helper", "helper", checkout)
    with pytest.raises(fork_refresh.AgentSpecLaunchUnresolved):
        fork_refresh.require_launch_resolves_one_file("helper", "helper", tmp_path / "bare")


def test_the_welcome_message_comes_from_the_pinned_file(contested, pins):
    from kiro_crew.agent_discovery import agent_welcome_message

    (contested / "PkgA-helper.json").write_text(
        json.dumps({"name": "helper", "prompt": "A", "welcomeMessage": "Hello from A"})
    )
    pins["helper"] = "PkgB-helper.json"
    assert agent_welcome_message("helper", agents_dir=contested) == ""
    pins["helper"] = "PkgA-helper.json"
    assert agent_welcome_message("helper", agents_dir=contested) == "Hello from A"


def test_a_pin_command_for_an_option_shaped_file_still_pins_it():
    import argparse

    from kiro_crew.cli_parser_agent import register_agent_parser

    command = agent_spec_pins.pin_command("helper", "-helper.json")
    assert command == "kirocrew agent pin helper -- -helper.json"
    parser = argparse.ArgumentParser()
    register_agent_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(command.split()[1:])
    assert (args.name, args.file, args.clear) == ("helper", "-helper.json", False)


def test_the_launch_refusal_runs_inside_the_ceiling_check(contested, pins, monkeypatch):
    from kiro_crew import agent as agent_mod
    from kiro_crew.acp_backends import ACP_BACKEND_KIRO
    from kiro_crew.agent_materialization import fork_refresh

    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: contested)
    with pytest.raises(fork_refresh.SharedTemplateOverCeiling):
        fork_refresh.require_shared_template_within_ceiling(
            "helper", ACP_BACKEND_KIRO, launch_name="helper"
        )


# -- the dashboard error meta -----------------------------------------------------


def test_a_refused_start_carries_the_chooser_meta(contested, pins, monkeypatch):
    from kiro_crew import agent_spec_pin_ops
    from kiro_crew.dashboard.chat_turn.recovery import _terminal_error_meta

    try:
        try:
            spec_by_declared_name(contested, "helper", operation="t", source="test")
        except AmbiguousAgentSpecError as inner:
            raise RuntimeError("cannot project agent 'helper' onto KAS") from inner
    except RuntimeError as outer:
        # The meta is built on the event loop: from the refusal's own parses, so no
        # spec file is opened again.
        monkeypatch.setattr(
            agent_spec_pin_ops,
            "_read_for",
            lambda *_a: pytest.fail("ambiguity_meta reopened a spec file"),
        )
        meta = _terminal_error_meta(outer)
    assert meta is not None
    assert meta["code"] == "agent_spec_ambiguous" and meta["agent"] == "helper"
    assert meta["pin"] is None
    assert [(c["file"], c["package"]) for c in meta["claimants"]] == [
        ("PkgA-helper.json", "PkgA"),
        ("PkgB-helper.json", "PkgB"),
    ]
    assert all(isinstance(c["mtime"], int) for c in meta["claimants"])
    # Each row says what sets it apart, so the choice is not a blind one.
    assert [c["differs"] for c in meta["claimants"]] == [
        ["prompt", "model", "hooks"],
        ["prompt", "model", "hooks"],
    ]
    assert [c["description"] for c in meta["claimants"]] == [None, None]


def test_a_pin_command_never_carries_a_control_character():
    """Names come from a user-writable directory: one the pin grammar refuses is
    shown by its ``repr``, so no escape sequence reaches the terminal."""
    line = agent_spec_pins.pin_command("helper\x1b]0;x\x07", "PkgA\x1b[2J.json")
    assert "\x1b" not in line and "\x07" not in line
    assert agent_spec_pins.pin_command("helper", "PkgA-helper.json") == (
        "kirocrew agent pin helper PkgA-helper.json"
    )


def test_the_chooser_counts_the_files_past_its_bound(tmp_path):
    from kiro_crew import agent_spec_pin_ops

    paths = [
        _write(tmp_path, f"P{i:02d}-helper.json", name="helper", prompt=str(i)) for i in range(35)
    ]
    exc = AmbiguousAgentSpecError("m", paths=paths, agent="helper")
    meta = agent_spec_pin_ops.ambiguity_meta(exc)
    assert meta is not None
    assert len(meta["claimants"]) == 32 and meta["omitted"] == 3


def test_a_pin_past_the_readers_bound_is_refused_not_dropped(user_agents, monkeypatch):
    from kiro_crew import agent_spec_pin_ops

    monkeypatch.setattr(agent_spec_pins, "MAX_SPEC_PINS", 1)
    agent_spec_pin_ops.write_spec_pin("other", "other.json")
    with pytest.raises(agent_spec_pin_ops.PinRefused) as refused:
        agent_spec_pin_ops.write_spec_pin("helper", "PkgB-helper.json")
    assert refused.value.code == "too_many_pins"
    assert _stored_pins() == {"other": "other.json"}
    # Updating or clearing an existing pin stays possible at the bound.
    agent_spec_pin_ops.write_spec_pin("other", "other.json")
    agent_spec_pin_ops.write_spec_pin("other", None)
    assert _stored_pins() == {}


def test_a_claimant_row_carries_its_own_description(tmp_path):
    from kiro_crew.agent_spec_pin_ops import claimant_rows

    a = _write(tmp_path, "A.json", name="helper", description="Reviews pipelines", tools=["read"])
    b = _write(tmp_path, "B.json", name="helper", description="  ", tools=["read"])
    rows = claimant_rows([a, b])
    assert [r["description"] for r in rows] == ["Reviews pipelines", None]
    assert [r["differs"] for r in rows] == [["description"], ["description"]]


def test_a_claimant_description_is_redacted_before_it_reaches_the_card(tmp_path):
    from kiro_crew.agent_spec_pin_ops import claimant_rows

    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    a = _write(tmp_path, "A.json", name="helper", description=f"Uses token {secret} to sync")
    b = _write(tmp_path, "B.json", name="helper", description="Plain")
    rows = claimant_rows([a, b])
    assert secret not in rows[0]["description"]
    assert rows[0]["description"].startswith("Uses token ")
    labelled = claimant_rows([a, b], {a: secret, b: "PkgB"})
    assert secret not in (labelled[0]["package"] or "")
    assert labelled[1]["package"] == "PkgB"


# -- the CLI ----------------------------------------------------------------------


@pytest.fixture
def user_agents(contested, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", lambda: contested)
    return contested


def _cli(*argv: str) -> None:
    import argparse

    from kiro_crew.cli_agent_pin import agent_pin

    parser = argparse.ArgumentParser()
    parser.add_argument("name", nargs="?")
    parser.add_argument("file", nargs="?")
    parser.add_argument("--clear", action="store_true")
    agent_pin(parser.parse_args(list(argv)))


def _stored_pins() -> dict:
    from kiro_crew.config import loader

    doc = json.loads(loader.config_path().read_text(encoding="utf-8"))
    return doc.get("agent", {}).get("spec_pins", {})


def test_cli_pin_writes_and_clears_are_audited(user_agents, monkeypatch):
    import kiro_crew.sel as sel_mod

    events: list[dict] = []

    class _Sel:
        def log_api_access(self, **kwargs):
            events.append(kwargs)

    monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
    _cli("helper", "PkgB-helper.json")
    _cli("helper", "--clear")
    assert [(e["operation"], e["resources"], e["source"]) for e in events] == [
        ("agent_spec_pin.write", "helper=PkgB-helper.json", "cli"),
        ("agent_spec_pin.clear", "helper", "cli"),
    ]


def test_cli_sets_lists_and_clears_a_pin(user_agents, capsys):
    _cli("helper", "PkgB-helper.json")
    assert _stored_pins() == {"helper": "PkgB-helper.json"}
    assert agent_spec_pins.spec_pins() == {"helper": "PkgB-helper.json"}
    _cli()
    assert "helper -> PkgB-helper.json" in capsys.readouterr().out
    _cli("helper", "--clear")
    assert _stored_pins() == {}
    assert "Cleared" in capsys.readouterr().out


@pytest.mark.parametrize(
    "file_name, fragment",
    [
        ("../PkgB-helper.json", "not a spec file name"),
        # Exact name only: another case is not the file a pin is matched against.
        ("pkgb-helper.json", "is not a spec in the agents directory"),
        ("sub/PkgB-helper.json", "not a spec file name"),
        ("Missing.json", "is not a spec in the agents directory"),
        ("other.json", "declares the name 'other'"),
    ],
)
def test_cli_refuses_a_pin_the_resolver_could_not_honour(user_agents, capsys, file_name, fragment):
    with pytest.raises(SystemExit) as exc:
        _cli("helper", file_name)
    assert exc.value.code == 1
    assert fragment in capsys.readouterr().err
    from kiro_crew.config import loader

    assert not loader.config_path().exists() or _stored_pins() == {}


@pytest.mark.parametrize("argv", [("helper", "PkgB-helper.json"), ("helper", "--clear")])
def test_cli_refuses_a_pin_the_local_overlay_overrides(user_agents, capsys, argv):
    from kiro_crew.config.loader_locations import config_local_path

    local = config_local_path()
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(json.dumps({"agent": {"spec_pins": {"helper": "Missing.json"}}}))
    with pytest.raises(SystemExit) as exc:
        _cli(*argv)
    assert exc.value.code == 1
    assert "overrides config.json" in capsys.readouterr().err
    from kiro_crew.config import loader

    assert not loader.config_path().exists() or _stored_pins() == {}


def test_cli_list_marks_a_pin_on_an_uncontested_name_broken(user_agents, monkeypatch, capsys):
    # One file declares ``other``: a pin naming a different file is refused by the
    # resolver, so the listing must not call it merely "not contested".
    monkeypatch.setattr(
        agent_spec_pins,
        "spec_pins",
        lambda: {"other": "Missing.json", "helper": "PkgA-helper.json", "gone": "x.json"},
    )
    _cli()
    out = capsys.readouterr().out
    assert "other -> Missing.json  (broken" in out
    assert "gone -> x.json  (broken" in out
    assert "helper -> PkgA-helper.json\n" in out


def test_cli_list_shows_a_pin_on_the_lone_claimant_as_not_contested(
    user_agents, monkeypatch, capsys
):
    monkeypatch.setattr(agent_spec_pins, "spec_pins", lambda: {"other": "other.json"})
    _cli()
    assert "other -> other.json  (not contested" in capsys.readouterr().out


def test_a_pinned_lone_link_never_falls_back_to_a_file_declaring_another_name(tmp_path, pins):
    from kiro_crew.agent_parts.spec_lookup import session_agent_spec_path

    user = tmp_path / "user-agents"
    _write(user, "helper.json", name="other", prompt="O")
    target = _write(tmp_path / "pkg", "helper.json", name="helper", prompt="B")
    try:
        (user / "PkgB-helper.json").symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    pins["helper"] = "PkgB-helper.json"
    # The pinned link is the only claimant: no path answer, so the caller's linked
    # reader runs, and the stem-matching file of another agent is never returned.
    assert session_agent_spec_path("helper", agents_dir=user) is None


def test_an_empty_mate_scope_leaves_a_pinned_link_to_the_linked_reader(tmp_path, pins, monkeypatch):
    from kiro_crew import agent
    from kiro_crew.member_essential_context import resolve_template_path

    user = tmp_path / "user-agents"
    _write(user, "PkgA-helper.json", name="helper", prompt="A")
    target = _write(tmp_path / "pkg", "helper.json", name="helper", prompt="B")
    try:
        (user / "PkgB-helper.json").symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    mates = tmp_path / "mates"
    mates.mkdir()
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: user)
    monkeypatch.setattr("kiro_crew.config.paths.mate_agents_dir", lambda: mates)
    pins["helper"] = "PkgB-helper.json"
    # No crewmate file declares the name: the mate probe must not refuse, so the
    # caller reaches the linked reader instead of a path-less pin error.
    assert resolve_template_path("helper") is None


def test_the_parser_accepts_the_pin_subcommand():
    import argparse

    from kiro_crew.cli_parser_agent import register_agent_parser

    parser = argparse.ArgumentParser()
    register_agent_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["agent", "pin", "helper", "PkgA-helper.json"])
    assert (args.agent_action, args.name, args.file, args.clear) == (
        "pin",
        "helper",
        "PkgA-helper.json",
        False,
    )


# -- the endpoints ----------------------------------------------------------------


def _app():
    from aiohttp import web

    from kiro_crew.dashboard.routes import agent_config as agent_config_routes

    @web.middleware
    async def _identity(request, handler):
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    class _State:
        owner_id = ""

    app = web.Application(middlewares=[_identity])
    app["state"] = _State()
    agent_config_routes.register(app)
    return app


@pytest.mark.asyncio
async def test_the_endpoint_sets_a_pin_and_serves_nothing_else(user_agents):
    """The chooser's PUT is the dashboard's one pin route; listing and clearing
    are the CLI's, so no GET or DELETE is served."""
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(_app())) as client:
        resp = await client.put("/api/agent-spec-pins/helper", json={"file": "PkgB-helper.json"})
        assert resp.status == 200, await resp.text()
        assert _stored_pins() == {"helper": "PkgB-helper.json"}
        assert agent_spec_pin_ops_contested_pin_state() == "ok"
        for method, path in (
            ("GET", "/api/agent-spec-pins"),
            ("DELETE", "/api/agent-spec-pins/helper"),
        ):
            resp = await client.request(method, path)
            assert resp.status in (404, 405), (method, resp.status)
        assert _stored_pins() == {"helper": "PkgB-helper.json"}


def agent_spec_pin_ops_contested_pin_state() -> str:
    from kiro_crew.agent_spec_pin_ops import contested_agents

    (row,) = contested_agents(source="cli")
    return row["pin_state"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path, body, status, code",
    [
        ("/api/agent-spec-pins/helper", {"file": "../x.json"}, 400, "invalid_file_name"),
        ("/api/agent-spec-pins/helper", {"file": "a/b.json"}, 400, "invalid_file_name"),
        ("/api/agent-spec-pins/helper", {"file": 7}, 400, "invalid_file_name"),
        ("/api/agent-spec-pins/helper", {"file": "Missing.json"}, 404, "file_not_found"),
        ("/api/agent-spec-pins/helper", {"file": "other.json"}, 400, "file_declares_other_name"),
        (
            "/api/agent-spec-pins/bad%20name",
            {"file": "PkgA-helper.json"},
            400,
            "invalid_agent_name",
        ),
    ],
)
async def test_endpoints_validate_like_the_cli(user_agents, path, body, status, code):
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(_app())) as client:
        resp = await client.put(path, json=body)
        assert resp.status == status, await resp.text()
        assert (await resp.json())["code"] == code
    from kiro_crew.config import loader

    assert not loader.config_path().exists() or _stored_pins() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["PUT"])
async def test_endpoints_are_owner_only(user_agents, method):
    from aiohttp.test_utils import TestClient, TestServer

    path = "/api/agent-spec-pins/helper"
    async with TestClient(TestServer(_app())) as client:
        for headers in ({"X-Test-User": "someone-else"}, {"X-Test-App": "some-app"}):
            resp = await client.request(
                method, path, json={"file": "PkgB-helper.json"}, headers=headers
            )
            assert resp.status in (401, 403), (method, headers, resp.status)
    from kiro_crew.config import loader

    assert not loader.config_path().exists() or _stored_pins() == {}
