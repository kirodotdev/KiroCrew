"""A crewmate worker's MCP tool limits hold on claude, as deny rules from its own spec.

The worker runs a spec built per session from the worker base and its crewmate's
file (``mate_worker.worker_session_spec``), so the claude seed reads its deny rules
from that spec, keyed by the session (``session_mcp_crewmate.crewmate_worker_deny_rules``),
never from the base on disk by name. The live capture
``test/fixtures/acp_frames/claude/worker-tool-limits-live.jsonl`` is the evidence
``ACP_BACKENDS_MATE_WORKER_MCP_TOOL_LIMITS`` holds claude to. A native-tool exclude
rides the session's ``tools`` option instead, an allow-list of the built-ins the
worker keeps; ``worker-native-tool-limits-live.jsonl`` is the evidence
``ACP_BACKENDS_MATE_WORKER_NATIVE_TOOL_LIMITS`` holds claude to. A native entry no
built-in row covers is still refused before spawn.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from acp_session_mcp_helpers import isolated_seed_provenance  # noqa: F401
from acp_session_mcp_helpers import _pin_harness

from kiro_crew import mate_worker
from kiro_crew.acp import session_mcp
from kiro_crew.acp.client import AcpClient, AcpError
from kiro_crew.acp.session_mcp_crewmate import crewmate_worker_deny_rules, worker_deny_rules
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_MATE_WORKER_MCP_TOOL_LIMITS,
    ACP_BACKENDS_MATE_WORKER_NATIVE_TOOL_LIMITS,
    ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS,
)
from kiro_crew.agent_sdk.backends import CC_PIN_INHERITED_MODE_ENV
from kiro_crew.mate_worker import MATE_WORKER_AGENT
from kiro_crew.session_capabilities import CapabilityStartupError

FIXTURES = Path(__file__).parent / "fixtures" / "acp_frames" / "claude"
KEY = "dashboard:chat-9"

BASE = {
    "name": MATE_WORKER_AGENT,
    "prompt": "You are the worker.",
    "tools": ["*"],
    "mcpServers": {
        "base-probe": {"command": "python3"},
        "whole-probe": {"command": "python3"},
    },
}
#: The crewmate file the capture was driven with, minus the probe's argv.
MATE = {
    "name": "pixel",
    "tools": ["*"],
    "mcpServers": {"limit-probe": {"command": "python3", "disabledTools": ["probe_hidden"]}},
    "excludedTools": ["@base-probe/base_hidden", "@whole-probe"],
}
HIDDEN = ("mcp__limit-probe__probe_hidden", "mcp__base-probe__base_hidden")
SIBLINGS = ("mcp__limit-probe__probe_echo", "mcp__base-probe__base_echo")


@pytest.fixture
def pixel_worker(tmp_path, monkeypatch):
    """``pixel``'s file beside the worker base; its worker session is ``chat-9``."""
    from kiro_crew.config import paths as paths_mod

    mates = tmp_path / "mates"
    mates.mkdir()
    (mates / f"{MATE_WORKER_AGENT}.json").write_text(json.dumps(BASE), encoding="utf-8")
    (mates / "pixel.json").write_text(json.dumps(MATE), encoding="utf-8")
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member, config=None: "pixel")
    monkeypatch.setattr(mate_worker, "globally_disabled_refs", lambda names: [])
    monkeypatch.setattr(session_mcp, "_global_settings", lambda: {})
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution",
        lambda key, **_kw: SimpleNamespace(member_id="pixel") if key == KEY else None,
    )
    return mates


def _capture() -> tuple[dict, list[dict]]:
    lines = (FIXTURES / "worker-tool-limits-live.jsonl").read_text(encoding="utf-8").splitlines()
    return json.loads(lines[0])["_meta"], [json.loads(line) for line in lines[1:]]


def test_the_capture_shows_every_limit_held_while_siblings_ran():
    meta, frames = _capture()
    assert meta["deny"] == [*sorted(HIDDEN), "mcp__whole-probe"]
    asked = {
        f["params"]["toolCall"]["title"]
        for f in frames
        if f.get("method") == "session/request_permission"
    }
    text = json.dumps(frames)
    for hidden in (*HIDDEN, "mcp__whole-probe__"):
        assert hidden not in text, "not even offered to the model"
    assert set(SIBLINGS) <= asked
    for sibling in SIBLINGS:
        assert f"tool-limit-probe-ran:{sibling.rsplit('__', 1)[1]}" in text


def test_the_captured_rules_are_what_crew_derives_for_the_session(pixel_worker):
    meta, _frames = _capture()
    assert (
        crewmate_worker_deny_rules(
            MATE_WORKER_AGENT, work_dir=None, session_key=KEY, backend=ACP_BACKEND_CLAUDE
        )
        == meta["deny"]
    )


def test_the_rules_equal_the_projections_own_parse(pixel_worker):
    """The seed's rules and the array's projection come from one worker spec."""
    projection = session_mcp.session_mcp_projection(
        MATE_WORKER_AGENT, session_key=KEY, worker_mcp_tool_limits_honoured=True
    )
    assert {"limit-probe", "base-probe", "whole-probe"} <= {e["name"] for e in projection.servers}
    assert crewmate_worker_deny_rules(
        MATE_WORKER_AGENT, work_dir=None, session_key=KEY, backend=ACP_BACKEND_CLAUDE
    ) == worker_deny_rules(projection.disabled_tools, projection.agent_spec)


def test_a_disabled_tool_on_a_crewmate_server_becomes_its_deny_rule(pixel_worker):
    (pixel_worker / "pixel.json").write_text(
        json.dumps(
            {
                "name": "pixel",
                "tools": ["*"],
                "mcpServers": {"srv": {"command": "x", "disabledTools": ["t"]}},
            }
        ),
        encoding="utf-8",
    )
    rules = crewmate_worker_deny_rules(
        MATE_WORKER_AGENT, work_dir=None, session_key=KEY, backend=ACP_BACKEND_CLAUDE
    )
    assert rules == ["mcp__srv__t"]


@pytest.mark.parametrize(
    "excluded",
    [
        [" @base-probe/base_hidden", "@whole-probe "],
        ["@base-probe / base_hidden", "\t@whole-probe"],
    ],
    ids=repr,
)
def test_an_exclude_with_stray_whitespace_is_still_its_deny_rule(pixel_worker, excluded):
    """An entry the refusal check trims and counts as carried is the rule it trims to."""
    (pixel_worker / "pixel.json").write_text(
        json.dumps({**MATE, "excludedTools": excluded}), encoding="utf-8"
    )
    rules = crewmate_worker_deny_rules(
        MATE_WORKER_AGENT, work_dir=None, session_key=KEY, backend=ACP_BACKEND_CLAUDE
    )
    assert rules == [*sorted(HIDDEN), "mcp__whole-probe"]


def test_the_rules_are_spelled_as_claude_code_names_the_tool():
    spec = {"excludedTools": ["@dot.srv/a.b", "@dot.srv2", "shell", "@x/*"]}
    assert worker_deny_rules(frozenset(), spec) == ["mcp__dot_srv2", "mcp__dot_srv__a_b"]


def test_an_ordinary_claude_session_keeps_the_rules_read_by_name(pixel_worker):
    """Any other agent, or a worker agent with no session, takes the name read."""
    for agent, key in (("pixel", KEY), (MATE_WORKER_AGENT, "")):
        assert (
            crewmate_worker_deny_rules(agent, work_dir=None, session_key=key, backend="claude")
            is None
        )


@pytest.mark.parametrize(
    "excluded",
    [
        ["use_aws"],
        ["introspect"],
        ["fs_*"],
        ["@jira/*"],
        ["*"],
        ["@jira/"],
        ["@builtin"],
        ["@builtin/*"],
    ],
    ids=str,
)
def test_a_limit_no_deny_carries_is_still_refused_on_claude(pixel_worker, excluded):
    (pixel_worker / "pixel.json").write_text(
        json.dumps({**MATE, "excludedTools": excluded}), encoding="utf-8"
    )
    with pytest.raises(CapabilityStartupError, match="capability_tool_limits_unsupported"):
        crewmate_worker_deny_rules(
            MATE_WORKER_AGENT, work_dir=None, session_key=KEY, backend=ACP_BACKEND_CLAUDE
        )
    from kiro_crew.session_allocation_parts.get_or_create import _worker_tool_limited_servers

    assert _worker_tool_limited_servers(KEY, True, True) == tuple(sorted(excluded))


@pytest.mark.parametrize(
    "disabled", ["probe_hidden", {"probe_hidden": True}, [1], ["probe_hidden", ""]], ids=repr
)
def test_a_disabled_tools_value_no_deny_reads_is_refused_on_claude(pixel_worker, disabled):
    """A set ``disabledTools`` that is not a list of tool names refuses, never runs wider."""
    entry = {"command": "python3", "disabledTools": disabled}
    (pixel_worker / "pixel.json").write_text(
        json.dumps({**MATE, "mcpServers": {"limit-probe": entry}}), encoding="utf-8"
    )
    with pytest.raises(CapabilityStartupError, match="capability_tool_limits_unsupported"):
        crewmate_worker_deny_rules(
            MATE_WORKER_AGENT, work_dir=None, session_key=KEY, backend=ACP_BACKEND_CLAUDE
        )
    assert mate_worker.uncarried_tool_limits_of({"mcpServers": {"limit-probe": entry}}) == (
        "limit-probe",
    )


def test_a_muted_or_empty_disabled_tools_value_is_not_a_limit():
    muted = {"command": "python3", "disabled": True, "disabledTools": "probe_hidden"}
    empty = {"command": "python3", "disabledTools": []}
    assert mate_worker.uncarried_tool_limits_of({"mcpServers": {"a": muted, "b": empty}}) == ()


def test_codex_still_refuses_any_limit(pixel_worker):
    with pytest.raises(CapabilityStartupError, match="capability_tool_limits_unsupported"):
        crewmate_worker_deny_rules(
            MATE_WORKER_AGENT, work_dir=None, session_key=KEY, backend=ACP_BACKEND_CODEX
        )
    from kiro_crew.session_allocation_parts.get_or_create import _worker_tool_limited_servers

    assert _worker_tool_limited_servers(KEY) == ("base-probe", "limit-probe", "whole-probe")
    assert _worker_tool_limited_servers(KEY, True) == ()


def test_the_claude_mirror_builds_a_limited_workers_array(pixel_worker):
    from kiro_crew.providers.mirrors.registry import mirror_for

    projection = mirror_for(ACP_BACKEND_CLAUDE).session_projection(
        MATE_WORKER_AGENT, permission_surface_owned=True, session_key=KEY
    )
    assert "limit-probe" in {e["name"] for e in projection.params["mcpServers"]}


def test_the_seed_carries_the_workers_rules(tmp_path, pixel_worker):
    client = AcpClient(
        work_dir=tmp_path, agent=MATE_WORKER_AGENT, acp_backend=ACP_BACKEND_CLAUDE, session_key=KEY
    )
    client._write_claude_local_settings()
    assert client._claude_settings_authored is True
    data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    assert data["permissions"]["deny"] == [*sorted(HIDDEN), "mcp__whole-probe"]


def test_membership_is_positive_and_nests():
    """H6: the MCP half is an opt-in set; the whole-limits set sits inside it."""
    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.providers.acp import AcpProvider
    from kiro_crew.providers.base import LLMProvider

    assert ACP_BACKENDS_MATE_WORKER_MCP_TOOL_LIMITS == frozenset(
        {ACP_BACKEND_KAS, ACP_BACKEND_CLAUDE}
    )
    assert ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS <= ACP_BACKENDS_MATE_WORKER_MCP_TOOL_LIMITS
    assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS
    assert LLMProvider.mate_worker_mcp_tool_limits_supported.fget(object()) is False
    for backend in (ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX, ACP_BACKEND_KIRO):
        client = SimpleNamespace(_client=SimpleNamespace(backend=backend))
        runtime = SimpleNamespace(_runtime=SimpleNamespace(acp_backend=backend))
        want = backend in ACP_BACKENDS_MATE_WORKER_MCP_TOOL_LIMITS
        assert AcpProvider.mate_worker_mcp_tool_limits_supported.fget(client) is want
        assert AcpSessionProvider.mate_worker_mcp_tool_limits_supported.fget(runtime) is want


class TestTheArrayIsHeldToTheSettingsInForce:
    """The array is held to the deny rules Claude Code will read, not to a later render."""

    def _seeded(self, tmp_path):
        client = AcpClient(
            work_dir=tmp_path,
            agent=MATE_WORKER_AGENT,
            acp_backend=ACP_BACKEND_CLAUDE,
            session_key=KEY,
        )
        client._write_claude_local_settings()
        assert client._claude_settings_authored is True
        return client

    def _narrow(self, mates):
        narrowed = {**MATE, "excludedTools": [*MATE["excludedTools"], "@limit-probe/probe_echo"]}
        (mates / "pixel.json").write_text(json.dumps(narrowed), encoding="utf-8")

    def test_an_unchanged_file_mounts_the_array(self, tmp_path, pixel_worker):
        client = self._seeded(tmp_path)
        assert "limit-probe" in {e["name"] for e in client._session_mcp_servers()}

    def test_a_narrowing_after_the_seed_withholds_the_array(self, tmp_path, pixel_worker):
        client = self._seeded(tmp_path)
        self._narrow(pixel_worker)
        assert client._session_mcp_servers() == []

    def test_a_render_that_was_never_written_does_not_count(self, tmp_path, pixel_worker):
        """A declined re-seed renders the new rules but ships the old file."""
        client = self._seeded(tmp_path)
        self._narrow(pixel_worker)
        client._render_claude_settings_payload()
        assert client._session_mcp_servers() == []

    def test_a_widening_after_the_seed_still_mounts(self, tmp_path, pixel_worker):
        client = self._seeded(tmp_path)
        (pixel_worker / "pixel.json").write_text(
            json.dumps({**MATE, "excludedTools": ["@whole-probe"]}), encoding="utf-8"
        )
        assert "base-probe" in {e["name"] for e in client._session_mcp_servers()}

    def test_the_inline_settings_are_what_is_judged(self, tmp_path, pixel_worker):
        """With the project's file excluded, the inline settings are what ships."""
        from kiro_crew.acp.client_mcp_array import _claude_worker_settings_lag

        client = self._seeded(tmp_path)
        spec = mate_worker.worker_session_spec(dict(BASE), KEY, mcp_tool_limits_carried=True)
        client._session_mcp_excludes_local = True
        client._session_mcp_inline_settings = {"permissions": {"deny": []}}
        assert _claude_worker_settings_lag(client, spec) is True
        client._session_mcp_inline_settings = {
            "permissions": {"deny": [*HIDDEN, "mcp__whole-probe"]}
        }
        assert _claude_worker_settings_lag(client, spec) is False

    def test_a_linked_settings_file_is_not_followed(self, tmp_path, pixel_worker):
        """A link at the settings path, or bytes that are not Crew's seed, carry no rules.

        The target holds every rule plus trailing whitespace, so it is held whether
        the read refuses the link (POSIX ``O_NOFOLLOW``) or follows it and finds bytes
        that are not the recorded seed (Windows has no ``O_NOFOLLOW``).
        """
        from kiro_crew.acp.client_mcp_array import _claude_worker_settings_lag

        client = self._seeded(tmp_path)
        spec = mate_worker.worker_session_spec(dict(BASE), KEY, mcp_tool_limits_carried=True)
        path = tmp_path / ".claude" / "settings.local.json"
        target = tmp_path / "elsewhere.json"
        seed = path.read_bytes()
        target.write_bytes(seed + b"\n\n")
        path.unlink()
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("this host cannot create a symlink")
        client._session_mcp_excludes_local = False
        client._session_mcp_inline_settings = None
        assert _claude_worker_settings_lag(client, spec) is True
        path.unlink()
        path.write_bytes(seed)
        assert _claude_worker_settings_lag(client, spec) is False

    def test_no_spec_reads_nothing(self, tmp_path, pixel_worker, monkeypatch):
        from kiro_crew.acp.client_mcp_array import _claude_worker_settings_lag

        client = self._seeded(tmp_path)

        def _no_read(*_a, **_kw):
            raise AssertionError("the settings path was read")

        monkeypatch.setattr(client, "_settings_path_bytes", _no_read)
        assert _claude_worker_settings_lag(client, None) is False

    def test_an_ordinary_session_is_never_held(self, tmp_path):
        from kiro_crew.acp.client_mcp_array import _claude_worker_settings_lag

        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        assert _claude_worker_settings_lag(client, {"excludedTools": ["@srv/t"]}) is False


#: The built-ins a worker excluding ``shell`` and ``fs_write`` must not be offered.
WITHHELD = (
    "Bash",
    "Monitor",
    "PowerShell",
    "TaskStop",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
)


def _native_capture() -> tuple[dict, list[dict]]:
    lines = (FIXTURES / "worker-native-tool-limits-live.jsonl").read_text(encoding="utf-8")
    first, *rest = lines.splitlines()
    return json.loads(first)["_meta"], [json.loads(line) for line in rest]


def test_the_native_capture_withheld_the_excluded_tools_while_the_rest_ran():
    meta, frames = _native_capture()
    assert not set(WITHHELD) & set(meta["offered_tools"]), "not offered to the model"
    assert {"Bash", "Write", "Edit", "Monitor", "PowerShell"} <= set(meta["control_offered_tools"])
    assert {"mcp__native-probe__probe_echo", "ToolSearch"} <= set(meta["offered_tools"])
    called = {
        (f["params"]["update"]["_meta"]["claudeCode"]["toolName"], f["params"]["sessionId"])
        for f in frames
        if f.get("params", {}).get("update", {}).get("sessionUpdate") == "tool_call"
    }
    assert {"Read", "Grep", "Agent"} <= {name for name, _ in called}
    assert not {name for name, _ in called} & set(WITHHELD)
    # The subagent the worker started ran on its own session with the same limits.
    assert {name for name, sid in called if sid != "claude-session-1"} <= {"ToolSearch", "Glob"}
    asked = [f for f in frames if f.get("method") == "session/request_permission"]
    assert [f["params"]["toolCall"]["title"] for f in asked] == ["mcp__native-probe__probe_echo"]
    assert "PROBE:hello" in json.dumps(frames)
    assert meta["files_after"] == [".claude", "seed.txt"]


def test_the_captured_tools_are_what_crew_derives_for_the_worker(pixel_worker):
    from kiro_crew.providers.mirrors.registry import mirror_for

    meta, _ = _native_capture()
    native = {**MATE, "excludedTools": [*MATE["excludedTools"], *meta["excluded_tools"]]}
    (pixel_worker / "pixel.json").write_text(json.dumps(native), encoding="utf-8")
    mirror = mirror_for(ACP_BACKEND_CLAUDE)
    for owned in (True, False):
        projection = mirror.session_projection(
            MATE_WORKER_AGENT, permission_surface_owned=owned, session_key=KEY
        )
        assert projection.builtin_tools == tuple(meta["options"]["tools"])
    # The MCP excludes alone keep claude's whole preset.
    (pixel_worker / "pixel.json").write_text(json.dumps(MATE), encoding="utf-8")
    projection = mirror.session_projection(
        MATE_WORKER_AGENT, permission_surface_owned=True, session_key=KEY
    )
    assert projection.builtin_tools is None


def test_the_session_options_carry_the_list_and_keep_a_narrower_one(tmp_path):
    meta, _ = _native_capture()
    client = AcpClient(
        work_dir=tmp_path, agent=MATE_WORKER_AGENT, acp_backend=ACP_BACKEND_CLAUDE, session_key=KEY
    )
    client._claude_worker_builtin_tools = tuple(meta["options"]["tools"])
    assert client._claude_session_meta()["claudeCode"]["options"]["tools"] == (
        meta["options"]["tools"]
    )
    client._spec_zero_tools = True
    assert client._claude_session_meta()["claudeCode"]["options"]["tools"] == []
    client._spec_zero_tools = False
    client._claude_worker_builtin_tools = None
    assert "tools" not in client._claude_session_meta()["claudeCode"]["options"]


@pytest.mark.asyncio
@pytest.mark.parametrize("version, stopped", [("0.83.9", True), ("", True), ("0.94.0", False)])
async def test_a_narrowed_worker_needs_an_adapter_that_honours_its_tools_list(
    tmp_path, monkeypatch, version, stopped
):
    """Below the options floor, or with no version, the ``tools`` list may be ignored."""
    monkeypatch.delenv(CC_PIN_INHERITED_MODE_ENV, raising=False)
    client = AcpClient(
        work_dir=tmp_path, agent=MATE_WORKER_AGENT, acp_backend=ACP_BACKEND_CLAUDE, session_key=KEY
    )
    client._claude_worker_builtin_tools = ("Read", "Grep")
    sent, killed = _pin_harness(client, monkeypatch)
    client._agent_version_read = True
    client._agent_version = version
    resp = {"sessionId": "s-1", "modes": {"currentModeId": "auto"}}
    if stopped:
        with pytest.raises(AcpError, match="not verified to honour"):
            await client._pin_claude_starting_mode(resp)
        assert killed == [True]
    else:
        # Above the floor its mode is any other session's: kept, with the pin off.
        await client._pin_claude_starting_mode(resp)
        assert killed == []
    assert sent == []


@pytest.mark.parametrize(
    "ref, withheld",
    [
        ("shell", {"Bash", "Monitor", "PowerShell", "TaskStop"}),
        ("@builtin/execute_bash", {"Bash", "Monitor", "PowerShell", "TaskStop"}),
        ("write", {"Write", "Edit", "MultiEdit", "NotebookEdit"}),
        ("read", {"Read", "Grep", "Glob"}),
        ("subagent", {"Task", "Agent"}),
        (" web_search ", {"WebSearch"}),
    ],
)
def test_a_mapped_native_exclude_is_carried_and_withholds_its_built_ins(ref, withheld):
    from kiro_crew.agent_sdk.claude_worker_tools import claude_worker_builtin_tools

    assert mate_worker.uncarried_tool_limits_of({"excludedTools": [ref]}) == (ref.strip(),)
    assert (
        mate_worker.uncarried_tool_limits_of(
            {"excludedTools": [ref]}, native_tool_limits_carried=True
        )
        == ()
    )
    kept = claude_worker_builtin_tools([ref])
    assert kept is not None and not set(kept) & withheld and "ToolSearch" in kept


def test_native_membership_is_positive():
    """H6: native excludes are an opt-in set of their own, never claude by absence."""
    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.providers.acp import AcpProvider
    from kiro_crew.providers.base import LLMProvider

    assert ACP_BACKENDS_MATE_WORKER_NATIVE_TOOL_LIMITS == frozenset(
        {ACP_BACKEND_KAS, ACP_BACKEND_CLAUDE}
    )
    assert ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS <= ACP_BACKENDS_MATE_WORKER_NATIVE_TOOL_LIMITS
    assert LLMProvider.mate_worker_native_tool_limits_supported.fget(object()) is False
    for backend in (ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX, ACP_BACKEND_KIRO):
        client = SimpleNamespace(_client=SimpleNamespace(backend=backend))
        runtime = SimpleNamespace(_runtime=SimpleNamespace(acp_backend=backend))
        want = backend in ACP_BACKENDS_MATE_WORKER_NATIVE_TOOL_LIMITS
        assert AcpProvider.mate_worker_native_tool_limits_supported.fget(client) is want
        # The shared runtime carries no ``tools`` list, so it never claims the carrier.
        assert AcpSessionProvider.mate_worker_native_tool_limits_supported.fget(runtime) is False
