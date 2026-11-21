"""A raw-key MCP server switched off from its row stays one mounted, switched-off server.

A server stored under its raw package key (``npm:@playwright/mcp``) mounts as
``playwright-mcp``. Turning that row off writes ``disabled: true`` onto the raw
entry, and the next rebuild must render ONE ``playwright-mcp``, switched off --
not keep the previous rendering running beside a disabled ``playwright-mcp-2``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)

RAW = "npm:@playwright/mcp"
ALIAS = "playwright-mcp"
SPEC = {"command": "/usr/bin/cat", "args": ["pw"]}


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kiro_global: dict) -> SimpleHome:
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "defaults.json").write_text(json.dumps({"name": "kirocrew"}))
    (agent_dir / "prompt.md").write_text("prompt")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    kiro_dir = tmp_path / ".kiro" / "agents"
    kiro_dir.mkdir(parents=True)
    settings = tmp_path / ".kiro" / "settings" / "mcp.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"mcpServers": kiro_global}))
    store_dir = tmp_path / "crew"
    store_dir.mkdir()
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", settings)
    monkeypatch.setattr("kiro_crew.agent._CC_MCP_JSON", tmp_path / "nonexistent_cc.json")
    monkeypatch.setattr("kiro_crew.agent._user_dir", lambda: store_dir)
    monkeypatch.setattr("kiro_crew.agent._KIROCREW_BIN", "/usr/bin/kirocrew")
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: "/usr/bin/cat")
    return SimpleHome(kiro_dir / "kirocrew.json", store_dir / "mcp.json")


class SimpleHome:
    def __init__(self, spec: Path, store: Path) -> None:
        self.spec = spec
        self.store = store

    def write_store(self, servers: dict) -> None:
        self.store.write_text(json.dumps({"mcpServers": servers}))

    def mounted(self) -> dict:
        servers = json.loads(self.spec.read_text(encoding="utf-8"))["mcpServers"]
        return {k: v for k, v in servers.items() if k.startswith(ALIAS)}


def test_switching_a_raw_key_server_off_mounts_it_once_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config

    home = _home(tmp_path, monkeypatch, {})
    home.write_store({RAW: dict(SPEC)})
    rebuild_agent_config()
    assert list(home.mounted()) == [ALIAS]

    home.write_store({RAW: {**SPEC, "disabled": True}})
    rebuild_agent_config()
    mounted = home.mounted()
    assert list(mounted) == [ALIAS], mounted
    assert mounted[ALIAS].get("disabled") is True, mounted

    home.write_store({RAW: dict(SPEC)})
    rebuild_agent_config()
    mounted = home.mounted()
    assert list(mounted) == [ALIAS], mounted
    assert not mounted[ALIAS].get("disabled"), mounted


def _point_handlers_at(home: SimpleHome, settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", home.store)
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", settings)
    monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", list)


def test_switching_off_a_row_another_scope_declares_leaves_the_raw_entry_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Kiro global declares ``playwright-mcp`` and the store holds a raw
    ``npm:@playwright/mcp``. Red before: the row's switch muted the raw entry, so the
    global's ``playwright-mcp`` kept running while the row read Disabled."""
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    global_spec = {"command": "/usr/bin/cat", "args": ["global"]}
    home = _home(tmp_path, monkeypatch, {ALIAS: global_spec})
    _point_handlers_at(home, tmp_path / ".kiro" / "settings" / "mcp.json", monkeypatch)
    home.write_store({RAW: dict(SPEC)})
    rebuild_agent_config()

    assert mcp_mod._set_kirocrew_entry(ALIAS, enabled=False) == "disabled"
    stored = json.loads(home.store.read_text())["mcpServers"]
    assert stored == {RAW: SPEC, ALIAS: {"disabled": True}}
    assert mcp_mod._kirocrew_entry_is_muted(ALIAS) is True
    assert mcp_mod._set_kirocrew_entry(ALIAS, enabled=False, existing_only=True) == "noop"

    rebuild_agent_config()
    mounted = home.mounted()
    assert mounted[ALIAS]["args"] == ["global"], mounted
    assert mounted[ALIAS].get("disabled") is True, mounted
    assert [v for v in mounted.values() if v.get("args") == ["pw"]] == [SPEC], mounted

    assert mcp_mod._set_kirocrew_entry(ALIAS, enabled=True) == "enabled"
    assert json.loads(home.store.read_text())["mcpServers"] == {RAW: SPEC, ALIAS: {}}


def test_a_raw_entry_alone_is_still_the_row_it_aliases_onto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no other scope declaring the name, the row still edits the raw entry in place."""
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    home = _home(tmp_path, monkeypatch, {})
    _point_handlers_at(home, tmp_path / ".kiro" / "settings" / "mcp.json", monkeypatch)
    home.write_store({RAW: dict(SPEC)})
    assert mcp_mod._set_kirocrew_entry(ALIAS, enabled=False) == "disabled"
    assert json.loads(home.store.read_text())["mcpServers"] == {RAW: {**SPEC, "disabled": True}}
    assert mcp_mod._kirocrew_entry_is_muted(ALIAS) is True


def test_two_raw_keys_sharing_an_alias_keep_their_own_renderings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which raw key an alias rendering came from is not recoverable when two raw keys
    alias together, so neither adopts it: no server inherits the other's grants."""
    from kiro_crew.agent import rebuild_agent_config

    home = _home(tmp_path, monkeypatch, {})
    other = {"command": "/usr/bin/cat", "args": ["other"]}
    home.write_store({RAW: dict(SPEC), "pip:@playwright/mcp": other})
    rebuild_agent_config()
    first = home.mounted()
    assert sorted(v["args"][0] for v in first.values()) == ["other", "pw"], first
    rebuild_agent_config()
    assert home.mounted() == first


def test_an_agent_only_server_under_the_alias_keeps_its_own_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server only the agent spec declares under the alias is not this raw key's
    rendering. Red before: the store loop adopted it, overwrote its launch spec and
    handed the store server its ``autoApprove``."""
    from kiro_crew.agent import rebuild_agent_config

    home = _home(tmp_path, monkeypatch, {})
    rebuild_agent_config()
    agent_only = {"command": "/usr/bin/cat", "args": ["agent-only"], "autoApprove": ["*"]}
    spec_doc = json.loads(home.spec.read_text(encoding="utf-8"))
    spec_doc.setdefault("mcpServers", {})[ALIAS] = agent_only
    home.spec.write_text(json.dumps(spec_doc))

    home.write_store({RAW: dict(SPEC)})
    rebuild_agent_config()
    mounted = home.mounted()
    by_args = {v["args"][0]: v for v in mounted.values()}
    assert by_args["agent-only"].get("autoApprove") == ["*"], mounted
    assert "autoApprove" not in by_args["pw"], mounted


def test_a_modifier_only_stub_elsewhere_does_not_hide_the_raw_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``disabledTools`` stub the Kiro global holds under the alias declares no server,
    so the row is still the raw store entry. Red before: enabling answered ok and left
    the raw entry muted."""
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    home = _home(tmp_path, monkeypatch, {ALIAS: {"disabledTools": ["t"]}})
    _point_handlers_at(home, tmp_path / ".kiro" / "settings" / "mcp.json", monkeypatch)
    home.write_store({RAW: {**SPEC, "disabled": True}})
    assert mcp_mod._kirocrew_entry_is_muted(ALIAS) is True
    assert mcp_mod._set_kirocrew_entry(ALIAS, enabled=True) == "enabled"
    assert json.loads(home.store.read_text())["mcpServers"] == {RAW: SPEC}


def test_an_agent_only_server_with_another_env_is_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same command and args but another ``env`` is another server: its grants stay its own."""
    from kiro_crew.agent import rebuild_agent_config

    home = _home(tmp_path, monkeypatch, {})
    rebuild_agent_config()
    agent_only = {**SPEC, "env": {"PROFILE": "agent"}, "autoApprove": ["*"]}
    spec_doc = json.loads(home.spec.read_text(encoding="utf-8"))
    spec_doc.setdefault("mcpServers", {})[ALIAS] = agent_only
    home.spec.write_text(json.dumps(spec_doc))

    home.write_store({RAW: dict(SPEC)})
    rebuild_agent_config()
    mounted = home.mounted()
    granted = [v for v in mounted.values() if v.get("autoApprove") == ["*"]]
    assert [v.get("env") for v in granted] == [{"PROFILE": "agent"}], mounted


def test_a_bare_command_raw_key_server_switched_off_mounts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry install writes a bare command (``npx``) and ``${HOME}`` args; the
    rendering holds the resolved path and the expanded args. Red before: the launch
    comparison saw ``cat`` against ``/usr/bin/cat`` and left a ``-2`` copy running.
    The stubbed ``/usr/bin/cat`` need not exist, as a pinned ``npx`` left behind by a
    node upgrade does not: identity, not liveness, decides adoption."""
    from kiro_crew.agent import rebuild_agent_config

    home = _home(tmp_path, monkeypatch, {})
    bare = {"command": "cat", "args": ["${HOME}/pw"], "env": {"PW_MODE": "x"}}
    home.write_store({RAW: dict(bare)})
    rebuild_agent_config()
    assert list(home.mounted()) == [ALIAS]

    home.write_store({RAW: {**bare, "disabled": True}})
    rebuild_agent_config()
    mounted = home.mounted()
    assert list(mounted) == [ALIAS], mounted
    assert mounted[ALIAS].get("disabled") is True, mounted


def test_a_malformed_global_server_map_declares_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-edited global whose ``mcpServers`` is not a map declares no server, so the
    row still edits the raw store entry instead of raising."""
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    home = _home(tmp_path, monkeypatch, {})
    settings = tmp_path / ".kiro" / "settings" / "mcp.json"
    settings.write_text(json.dumps({"mcpServers": ["x"]}))
    _point_handlers_at(home, settings, monkeypatch)
    home.write_store({RAW: {**SPEC, "disabled": True}})
    assert mcp_mod._kirocrew_entry_is_muted(ALIAS) is True
    assert mcp_mod._set_kirocrew_entry(ALIAS, enabled=True) == "enabled"
    assert json.loads(home.store.read_text())["mcpServers"] == {RAW: SPEC}


@pytest.mark.parametrize("existing_only", [True, False], ids=["toggle", "apply"])
@pytest.mark.parametrize("stub", [None, {"disabled": True}], ids=["raw-only", "with-stub"])
def test_enabling_a_row_another_scope_declares_lifts_a_muted_raw_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub: dict | None, existing_only: bool
) -> None:
    """The Kiro global declares ``playwright-mcp`` and the store's raw entry carries a
    mute (the pre-fix switch-off wrote it there). The listing folds that raw entry
    into the row, so an enable must lift it, with or without an exact-name stub.
    Red before: the enable answered ok and left the raw entry muted."""
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    global_spec = {"command": "/usr/bin/cat", "args": ["global"]}
    home = _home(tmp_path, monkeypatch, {ALIAS: global_spec})
    _point_handlers_at(home, tmp_path / ".kiro" / "settings" / "mcp.json", monkeypatch)
    store = {RAW: {**SPEC, "disabled": True}, **({ALIAS: stub} if stub else {})}
    home.write_store(store)
    assert mcp_mod._kirocrew_entry_is_muted(ALIAS) is True
    action = mcp_mod._set_kirocrew_entry(ALIAS, enabled=True, existing_only=existing_only)
    assert action == "enabled"
    stored = json.loads(home.store.read_text())["mcpServers"]
    assert stored[RAW] == SPEC, stored
    assert stored.get(ALIAS) == ({} if stub else None), stored
    assert mcp_mod._kirocrew_entry_is_muted(ALIAS) is False


def test_a_distinct_alias_sibling_does_not_block_the_switch_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another server already holds ``playwright-mcp-2``. It is not this raw key's
    rendering, so switching the raw-key row off still mounts it once, switched off,
    and the other server keeps its own mount untouched."""
    from kiro_crew.agent import rebuild_agent_config

    home = _home(tmp_path, monkeypatch, {})
    home.write_store({RAW: dict(SPEC)})
    rebuild_agent_config()
    other = {"command": "/usr/bin/cat", "args": ["other"], "autoApprove": ["*"]}
    spec_doc = json.loads(home.spec.read_text(encoding="utf-8"))
    spec_doc["mcpServers"][f"{ALIAS}-2"] = other
    home.spec.write_text(json.dumps(spec_doc))

    home.write_store({RAW: {**SPEC, "disabled": True}})
    rebuild_agent_config()
    mounted = home.mounted()
    ours = [v for v in mounted.values() if v.get("args") == ["pw"]]
    assert len(ours) == 1 and ours[0].get("disabled") is True, mounted
    assert mounted[f"{ALIAS}-2"] == other, mounted


def test_a_two_mount_spec_an_older_build_left_heals_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spec holds ``playwright-mcp`` running beside a muted ``playwright-mcp-2``
    (the reported layout). The next rebuild folds the leftover
    copy and mounts the server once, switched off."""
    from kiro_crew.agent import rebuild_agent_config

    home = _home(tmp_path, monkeypatch, {})
    home.write_store({RAW: dict(SPEC)})
    rebuild_agent_config()
    home.write_store({RAW: {**SPEC, "disabled": True}})
    with monkeypatch.context() as patched:
        patched.setattr(
            "kiro_crew.agent_materialization.mcp_sources._adopt_stale_alias_rendering",
            lambda *args, **kwargs: None,
        )
        rebuild_agent_config()
    assert set(home.mounted()) == {ALIAS, f"{ALIAS}-2"}

    rebuild_agent_config()
    mounted = home.mounted()
    assert list(mounted) == [ALIAS], mounted
    assert mounted[ALIAS].get("disabled") is True, mounted
    spec = json.loads(home.spec.read_text(encoding="utf-8"))
    refs = [*spec.get("tools", []), *spec.get("allowedTools", [])]
    assert not [r for r in refs if isinstance(r, str) and f"{ALIAS}-2" in r], refs


def test_a_same_launch_sibling_with_its_own_grants_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``playwright-mcp-2`` that launches the same program but carries its own
    ``autoApprove`` is not a leftover copy of the base: switching the raw-key row
    off mounts the raw server switched off and keeps the sibling as it was."""
    from kiro_crew.agent import rebuild_agent_config

    home = _home(tmp_path, monkeypatch, {})
    home.write_store({RAW: dict(SPEC)})
    rebuild_agent_config()
    sibling = {**SPEC, "autoApprove": ["*"]}
    spec_doc = json.loads(home.spec.read_text(encoding="utf-8"))
    spec_doc["mcpServers"][f"{ALIAS}-2"] = sibling
    home.spec.write_text(json.dumps(spec_doc))

    home.write_store({RAW: {**SPEC, "disabled": True}})
    rebuild_agent_config()
    mounted = home.mounted()
    assert mounted[ALIAS].get("disabled") is True, mounted
    assert [v for v in mounted.values() if v.get("autoApprove") == ["*"]] == [sibling], mounted
    running = [v for v in mounted.values() if "autoApprove" not in v and not v.get("disabled")]
    assert running == [], mounted


def test_empty_env_and_args_do_not_block_the_switch_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-installs write ``"env": {}`` / ``"args": []``; the rendering drops them as
    launch no-ops. Switching the row off still mounts the server once, switched off."""
    from kiro_crew.agent import rebuild_agent_config

    home = _home(tmp_path, monkeypatch, {})
    empty = {"command": "/usr/bin/cat", "args": [], "env": {}}
    home.write_store({RAW: dict(empty)})
    rebuild_agent_config()
    assert list(home.mounted()) == [ALIAS]

    home.write_store({RAW: {**empty, "disabled": True}})
    rebuild_agent_config()
    mounted = home.mounted()
    assert list(mounted) == [ALIAS], mounted
    assert mounted[ALIAS].get("disabled") is True, mounted


@pytest.mark.parametrize(
    "authored",
    [
        {"disabledTools": ["write"], "autoApprove": ["*"]},
        {"disabledTools": ["write"], "autoApprove": ["*"], "timeout": 5},
    ],
    ids=["guard-and-grant", "guard-grant-and-timeout"],
)
def test_adoption_keeps_a_guard_the_agent_spec_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authored: dict
) -> None:
    """An agent-spec ``playwright-mcp`` written with its own grant and tool guard is
    a declaration, not the store's earlier rendering: the grant shows it. When the
    store gains ``npm:@playwright/mcp`` with the same launch and neither field, the
    server mounts once and keeps every modifier the spec wrote."""
    from kiro_crew.agent import rebuild_agent_config

    home = _home(tmp_path, monkeypatch, {})
    home.write_store({})
    rebuild_agent_config()
    spec_doc = json.loads(home.spec.read_text(encoding="utf-8"))
    spec_doc["mcpServers"][ALIAS] = {**SPEC, **authored}
    home.spec.write_text(json.dumps(spec_doc))
    rebuild_agent_config()

    home.write_store({RAW: dict(SPEC)})
    rebuild_agent_config()
    mounted = home.mounted()
    assert list(mounted) == [ALIAS], mounted
    for key, value in authored.items():
        assert mounted[ALIAS].get(key) == value, mounted


def test_a_sibling_with_its_own_tool_guard_is_not_folded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent-only ``playwright-mcp-2`` launching the same server with its own
    ``disabledTools`` is a declaration, not this raw key's leftover rendering, so it
    keeps its mount and guard when the raw-key row is switched off."""
    from kiro_crew.agent import rebuild_agent_config

    home = _home(tmp_path, monkeypatch, {})
    home.write_store({RAW: dict(SPEC)})
    rebuild_agent_config()
    guarded = {**SPEC, "disabledTools": ["write"]}
    spec_doc = json.loads(home.spec.read_text(encoding="utf-8"))
    spec_doc["mcpServers"][f"{ALIAS}-2"] = guarded
    home.spec.write_text(json.dumps(spec_doc))

    home.write_store({RAW: {**SPEC, "disabled": True}})
    rebuild_agent_config()
    mounted = home.mounted()
    assert mounted[ALIAS].get("disabled") is True, mounted
    assert [v for v in mounted.values() if v.get("disabledTools") == ["write"]] == [
        guarded
    ], mounted


@pytest.mark.parametrize(
    "stub", [{"disabledTools": ["t"]}, {}], ids=["tool-toggled-off", "tool-toggled-back-on"]
)
def test_a_tool_toggled_row_switched_off_mounts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub: dict
) -> None:
    """A tool toggle leaves a modifier-only stub under the alias in the Kiro global:
    ``{"disabledTools": [...]}``, or ``{}`` once the tool is back on. The stub
    declares no server, so the raw store entry is still the row's server: switching
    the row off mounts it once, switched off, and switching it on mounts it once,
    running."""
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    home = _home(tmp_path, monkeypatch, {})
    settings = tmp_path / ".kiro" / "settings" / "mcp.json"
    _point_handlers_at(home, settings, monkeypatch)
    home.write_store({RAW: dict(SPEC)})
    rebuild_agent_config()
    assert list(home.mounted()) == [ALIAS]

    settings.write_text(json.dumps({"mcpServers": {ALIAS: stub}}))
    rebuild_agent_config()
    assert list(home.mounted()) == [ALIAS]

    assert mcp_mod._set_kirocrew_entry(ALIAS, enabled=False) == "disabled"
    assert json.loads(home.store.read_text())["mcpServers"] == {RAW: {**SPEC, "disabled": True}}
    rebuild_agent_config()
    mounted = home.mounted()
    assert list(mounted) == [ALIAS], mounted
    assert mounted[ALIAS].get("disabled") is True, mounted

    assert mcp_mod._set_kirocrew_entry(ALIAS, enabled=True) == "enabled"
    rebuild_agent_config()
    mounted = home.mounted()
    assert list(mounted) == [ALIAS], mounted
    assert not mounted[ALIAS].get("disabled"), mounted


@pytest.mark.parametrize(
    "stub",
    [{"disabledTools": ["t"]}, {"timeout": 30, "autoApprove": ["t"]}],
    ids=["tool-stub", "other-non-launch-keys"],
)
def test_a_switch_written_onto_the_kiro_global_stub_mutes_the_one_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub: dict
) -> None:
    """``POST /api/mcp/toggle`` finds the stub in the Kiro global and writes the
    switch there instead of on the raw store entry. The stub still declares no
    server, so the mount is the raw entry's rendering, muted by the stub."""
    from kiro_crew.agent import rebuild_agent_config

    home = _home(tmp_path, monkeypatch, {})
    settings = tmp_path / ".kiro" / "settings" / "mcp.json"
    home.write_store({RAW: dict(SPEC)})
    rebuild_agent_config()
    settings.write_text(json.dumps({"mcpServers": {ALIAS: stub}}))
    rebuild_agent_config()

    settings.write_text(json.dumps({"mcpServers": {ALIAS: {**stub, "disabled": True}}}))
    rebuild_agent_config()
    mounted = home.mounted()
    assert list(mounted) == [ALIAS], mounted
    assert mounted[ALIAS].get("disabled") is True, mounted
    assert mounted[ALIAS].get("timeout") == stub.get("timeout"), mounted

    settings.write_text(json.dumps({"mcpServers": {ALIAS: stub}}))
    rebuild_agent_config()
    mounted = home.mounted()
    assert list(mounted) == [ALIAS], mounted
    assert not mounted[ALIAS].get("disabled"), mounted


def _provider_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: dict) -> Path:
    path = tmp_path / "provider" / "mcp.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"mcpServers": servers}))
    monkeypatch.setattr(
        "kiro_crew.agent_materialization.mcp_sources._extra_mcp_scope_globals", lambda: [path]
    )
    return path


def test_a_tool_stub_in_a_provider_global_does_not_block_the_switch_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same modifier-only stub held by an edition's provider global claims nothing either."""
    from kiro_crew.agent import rebuild_agent_config

    home = _home(tmp_path, monkeypatch, {})
    home.write_store({RAW: dict(SPEC)})
    rebuild_agent_config()
    _provider_global(tmp_path, monkeypatch, {ALIAS: {"disabledTools": ["t"]}})
    rebuild_agent_config()

    home.write_store({RAW: {**SPEC, "disabled": True}})
    rebuild_agent_config()
    mounted = home.mounted()
    assert list(mounted) == [ALIAS], mounted
    assert mounted[ALIAS].get("disabled") is True, mounted


def test_a_provider_global_declaring_the_alias_keeps_its_own_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider global that launches a server under the alias claims the name, even
    with the store entry's own launch: that declaration stays the mount with its own
    grant, and the raw store entry mounts beside it, as for a Kiro global declaration."""
    from kiro_crew.agent import rebuild_agent_config

    home = _home(tmp_path, monkeypatch, {})
    provider = {**SPEC, "autoApprove": ["*"]}
    _provider_global(tmp_path, monkeypatch, {ALIAS: provider})
    home.write_store({RAW: dict(SPEC)})
    rebuild_agent_config()
    rebuild_agent_config()
    mounted = home.mounted()
    assert mounted[ALIAS] == provider, mounted
    assert [k for k, v in mounted.items() if v == SPEC] == [f"{ALIAS}-2"], mounted
