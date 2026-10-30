"""Per-template MCP additions: the record, and every path that merges it.

The record lives outside every spec file and carries no trust. These tests pin the
store (owner-only, atomic, refuses a trust key), the merge at each installer (the
conductors, the worker mirror, the default agent) and at spawn (a template Crew does
not own, on the backends that read a spec per session), the worker's freshness
check, the support and reach queries against the tables they mirror, and the doctor
row.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from worker_agent_helpers import _accepting_kiro_cli  # noqa: F401
from worker_agent_helpers import _DEFAULT_SPEC_ON_DISK

from kiro_crew import agent
from kiro_crew import template_mcp_additions as tma
from kiro_crew.agent_files import (
    AGENT_FILENAME,
    CONDUCTOR_AGENT_FILENAME,
    LEDGER_CONDUCTOR_AGENT_FILENAME,
    PIPELINE_CONDUCTOR_AGENT_FILENAME,
    SECURITY_CONDUCTOR_AGENT_FILENAME,
    WORKER_AGENT_FILENAME,
)
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

_ENTRY = {"command": "/opt/fetch/bin/mcp-fetch", "args": ["--stdio"]}


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(data))
    return data


def _plant(template: str) -> None:
    """Write an addition for *template* straight into the record, past ``add_addition``'s
    refusal, as a hand edit could: the merge must not trust the record to be clean."""
    path = tma.record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1}
    data.setdefault("templates", {})[template] = {
        "fetch": {"entry": dict(_ENTRY), "approved_by": "o", "approved_at": "t", "card_id": "c"}
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def _add(template: str, server: str = "fetch", entry: dict | None = None, card: str = "c1"):
    return tma.add_addition(
        template, server, entry or dict(_ENTRY), approved_by="owner", card_id=card
    )


# -- the record -----------------------------------------------------------------


def test_an_addition_round_trips_with_its_provenance(home):
    added = _add("my-agent")
    got = tma.read_additions("my-agent")
    assert list(got) == ["fetch"]
    assert got["fetch"] == added
    assert got["fetch"].approved_by == "owner" and got["fetch"].card_id == "c1"
    assert got["fetch"].approved_at
    assert tma.list_additions() == {"my-agent": got}


def test_the_record_is_owner_only_and_outside_every_spec(home):
    _add("my-agent")
    path = tma.record_path()
    assert path.parent == home / tma.RECORD_DIR
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == tma.RECORD_VERSION


@pytest.mark.parametrize("key", sorted(tma.TRUST_KEYS))
def test_a_trust_key_is_refused_on_write(home, key):
    with pytest.raises(tma.AdditionError, match=f"trust_key:{key}"):
        _add("my-agent", entry={**_ENTRY, key: ["*"]})
    assert tma.read_additions("my-agent") == {}


@pytest.mark.parametrize(
    "entry",
    [
        {"command": "x", "cwd": "/"},
        {"args": ["x"]},
        {"command": "x", "url": "https://x"},
        {"url": "https://x", "args": []},
        {"command": "x", "env": {"A": 1}},
        {"command": "x", "timeout": True},
        "not-an-object",
    ],
)
def test_a_malformed_entry_is_refused(home, entry):
    with pytest.raises(tma.AdditionError):
        tma.add_addition("my-agent", "s", entry, approved_by="o", card_id="c")


def test_a_hand_edited_trust_key_never_reaches_a_merge(home):
    """The no-trust rule holds on READ too: a record someone edited by hand to carry
    autoApprove is skipped, so the merged map never holds a grant."""
    _add("my-agent")
    path = tma.record_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["templates"]["my-agent"]["fetch"]["entry"]["autoApprove"] = ["*"]
    path.write_text(json.dumps(data), encoding="utf-8")
    merged = tma.merge_into("my-agent", {})
    assert "fetch" not in merged
    assert all("autoApprove" not in e for e in merged.values())


def test_the_merge_only_adds(home):
    _add("my-agent")
    own = {"fetch": {"command": "the-templates-own"}}
    assert tma.merge_into("my-agent", own) == own
    assert tma.merge_into("my-agent", {}) == {"fetch": _ENTRY}
    assert own == {"fetch": {"command": "the-templates-own"}}


def test_a_restriction_only_entry_narrows_the_approved_server(home):
    """A checkout's (or the dashboard's) switch-off keys under the name, with no
    transport, narrow the approved server rather than withholding it whole."""
    _add("my-agent")
    restricted = {"disabledTools": ["dangerous"], "disabled": False, "x-kirocrew": {}}
    merged = tma.merge_into("my-agent", {"fetch": restricted})
    assert merged["fetch"] == {**_ENTRY, "disabledTools": ["dangerous"], "disabled": False}
    spec = {"tools": [], "mcpServers": {"fetch": {"disabledTools": ["dangerous"]}}}
    assert tma.merge_into_spec("my-agent", spec) == ["fetch"]
    assert spec["mcpServers"]["fetch"]["command"] == _ENTRY["command"] and "@fetch" in spec["tools"]
    # A declaration with anything else (here a launch) is the template's and stands.
    own = {"fetch": {"command": "theirs", "disabledTools": ["x"]}}
    assert tma.merge_into("my-agent", own) == own
    # Nothing but the two restriction keys is carried across.
    odd = {"fetch": {"disabledTools": ["x"], "autoApprove": ["*"]}}
    assert tma.merge_into("my-agent", odd) == odd


def test_the_array_hosts_read_keeps_an_approved_server_a_checkout_narrows(home, monkeypatch):
    from kiro_crew.acp import session_mcp

    spec = {
        "name": "my-agent",
        "tools": ["@kirocrew-core"],
        "mcpServers": {"fetch": {"disabledTools": ["dangerous"]}},
    }
    monkeypatch.setattr(
        session_mcp, "_agent_spec_and_snapshot_for", lambda *a, **k: (dict(spec), None)
    )
    _add("my-agent")
    got, _snapshot = session_mcp._session_spec_and_snapshot_for("my-agent")
    assert got["mcpServers"]["fetch"]["command"] == _ENTRY["command"]
    assert got["mcpServers"]["fetch"]["disabledTools"] == ["dangerous"]
    names = {e["name"] for e in session_mcp.session_mcp_servers("my-agent", spec=got, muted=())}
    assert "fetch" in names


def test_merge_into_spec_mounts_the_server_and_grants_nothing(home):
    _add("my-agent")
    spec: dict[str, Any] = {"tools": ["fs_read"], "allowedTools": ["fs_read"], "mcpServers": {}}
    assert tma.merge_into_spec("my-agent", spec) == ["fetch"]
    assert spec["mcpServers"]["fetch"] == _ENTRY
    assert spec["tools"] == ["fs_read", "@fetch"]
    assert spec["allowedTools"] == ["fs_read"]


def test_an_alias_writes_and_removes_its_successors_record(home):
    _add("kirocrew-ledger-conductor")
    assert "fetch" in tma.read_additions("kirocrew-conductor")
    assert tma.merge_into("kirocrew-ledger-conductor", {})["fetch"] == _ENTRY
    assert tma.remove_addition("kirocrew-ledger-conductor", "fetch") is True
    assert tma.read_additions("kirocrew-conductor") == {}


def test_remove_with_a_card_id_only_removes_that_cards_approval(home):
    _add("my-agent", card="old")
    _add("my-agent", card="new")
    assert tma.remove_addition("my-agent", "fetch", card_id="old") is False
    assert "fetch" in tma.read_additions("my-agent")
    assert tma.remove_addition("my-agent", "fetch", card_id="new") is True
    assert tma.read_additions("my-agent") == {}
    assert tma.remove_addition("my-agent", "fetch") is False


def test_an_unreadable_record_is_never_overwritten_blind(home):
    tma.record_path().parent.mkdir(parents=True)
    tma.record_path().write_text("{not json", encoding="utf-8")
    assert tma.read_additions("my-agent") == {}
    with pytest.raises(ValueError):
        _add("my-agent")
    assert tma.record_path().read_text(encoding="utf-8") == "{not json"


def test_the_record_directory_is_sealed_against_a_sandboxed_writer():
    """A sandboxed process that could write the record could add a server of its
    choosing and have the next rebuild launch it. The leaf is read-only to the OS
    sandbox (pre-created so the seal holds on a fresh home, and not satisfiable by
    a planted symlink), denied to the file-tool gate, and withheld from foreign
    harness children."""
    from kiro_crew import sandbox_leaves, sandbox_precreate
    from kiro_crew.security import paths_lists

    leaf = tma.RECORD_DIR
    assert leaf in sandbox_leaves._CREW_READONLY_LEAVES
    assert leaf in sandbox_leaves._CREW_CHILD_WITHHELD_LEAVES
    assert leaf in sandbox_precreate._CREW_PRECREATE_READONLY_DIR_LEAVES
    assert leaf in sandbox_precreate._CREW_NOFOLLOW_READONLY_DIR_LEAVES
    # On the read floor too: only the floor reaches an enforced adapter's OS mask and
    # the file-tool read gate, and an approved entry can hold an API key.
    assert leaf in paths_lists._CREW_SECRET_LEAVES
    # The floor fences reads AND writes for the file tools.
    from kiro_crew.security import paths as security_paths

    target = Path.home() / ".kiro" / "crew" / leaf / "additions.json"
    assert security_paths.is_sensitive_write_path(str(target))
    assert security_paths.is_sensitive_path(str(target))


# -- the queries, against the tables they mirror ----------------------------------


def test_the_constants_match_their_owners():
    from kiro_crew.agent_materialization import conductor_agents
    from kiro_crew.agent_parts import spec_build

    assert tma._TEMPLATE_ALIASES == conductor_agents.DEPRECATED_AGENT_SPECS
    assert spec_build._MAIN_AGENT_NAME in tma.DEFAULT_TEMPLATES
    assert Path(WORKER_AGENT_FILENAME).stem in tma.DEFAULT_TEMPLATES
    conductors = {
        Path(f).stem
        for f in (
            CONDUCTOR_AGENT_FILENAME,
            LEDGER_CONDUCTOR_AGENT_FILENAME,
            PIPELINE_CONDUCTOR_AGENT_FILENAME,
            SECURITY_CONDUCTOR_AGENT_FILENAME,
        )
    }
    assert tma.INSTALLER_MERGED_TEMPLATES == conductors


@pytest.mark.parametrize("template", sorted(tma.DEFAULT_TEMPLATES))
def test_the_default_agent_and_the_worker_are_refused_everywhere(home, template):
    """Their servers come from the MCP page: the default spec holds every server
    installed there and the worker mirrors it, so the record takes nothing for them."""
    with pytest.raises(tma.AdditionError, match="template_not_covered"):
        _add(template)
    assert tma.read_additions(template) == {}
    for backend in ("", "kas", "claude", "codex", "pi"):
        assert tma.addition_support(template, backend) == tma.AdditionSupport(
            False, tma.DEFAULT_REASON
        )
    assert tma.addition_reach(template) == ()
    _plant(template)
    spec = {"tools": [], "mcpServers": {}}
    assert tma.merge_at_spawn(template, spec) is spec


def test_reach_follows_the_alias():
    assert tma.addition_reach("kirocrew-conductor") == (
        "kirocrew-conductor",
        "kirocrew-ledger-conductor",
    )
    assert tma.addition_reach("kirocrew-ledger-conductor") == (
        "kirocrew-ledger-conductor",
        "kirocrew-conductor",
    )
    assert tma.addition_reach("kirocrew-pipeline-conductor") == ("kirocrew-pipeline-conductor",)
    assert tma.addition_reach("my-agent") == ("my-agent",)
    assert tma.addition_reach("bad name") == ()


def test_reach_matches_what_the_installers_actually_merge(tmp_path, monkeypatch):
    """Not a restatement: install every conductor and the worker, and read which carry
    an addition made to the goal conductor."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _add("kirocrew-conductor", server="from-conductor")
    specs = _install_conductors_and_worker(tmp_path, monkeypatch)
    carrying = {name for name, spec in specs.items() if "from-conductor" in spec["mcpServers"]}
    assert carrying == set(tma.addition_reach("kirocrew-conductor"))


def test_support_for_a_conductor_needs_a_backend_that_runs_spec_servers():
    for template in tma.INSTALLER_MERGED_TEMPLATES:
        for backend in ("", "kas", "claude", "codex"):
            assert tma.addition_support(template, backend) == tma.AdditionSupport(True)
        refused = tma.addition_support(template, "pi")
        assert refused == tma.AdditionSupport(False, tma.NO_SPEC_SERVERS_REASON)


def test_a_crewmate_service_template_is_refused_and_never_merged_at_spawn(home):
    from kiro_crew.agent_files import MATE_SERVICE_AGENT_FILENAMES

    for filename in MATE_SERVICE_AGENT_FILENAMES:
        template = Path(filename).stem
        for backend in ("", "kas", "claude", "codex"):
            refused = tma.addition_support(template, backend)
            assert refused == tma.AdditionSupport(False, tma.CREWMATE_REASON)
        with pytest.raises(tma.AdditionError):
            _add(template)
        spec = {"tools": [], "mcpServers": {}}
        assert tma.merge_at_spawn(template, spec) is spec


def test_every_other_built_in_template_is_refused_and_never_merged_at_spawn(home):
    """The heartbeat copies a fixed server list and the background agents are nobody's
    chat; their installers take no additions, so nothing may promise one."""
    from kiro_crew.agent_files import MATE_SERVICE_AGENT_FILENAMES, OWNED_KIRO_AGENT_FILES

    others = (
        {Path(f).stem for f in OWNED_KIRO_AGENT_FILES}
        - set(tma.INSTALLER_MERGED_TEMPLATES)
        - set(tma.DEFAULT_TEMPLATES)
        - {Path(f).stem for f in MATE_SERVICE_AGENT_FILENAMES}
    )
    assert "kirocrew-heartbeat" in others
    for template in sorted(others):
        for backend in ("", "kas", "claude", "codex"):
            assert tma.addition_support(template, backend) == tma.AdditionSupport(
                False, tma.BUILT_IN_REASON
            )
        with pytest.raises(tma.AdditionError):
            _add(template)
        _plant(template)
        spec = {"tools": [], "mcpServers": {}}
        assert tma.merge_at_spawn(template, spec) is spec


def test_conductor_support_follows_the_spec_server_delivery_answer():
    from kiro_crew.acp_backends import ACP_BACKENDS_KNOWN
    from kiro_crew.agent_sdk.drivers.acp import backend_delivers_spec_servers

    for backend in ACP_BACKENDS_KNOWN:
        expected = backend_delivers_spec_servers(backend)
        assert tma.addition_support("kirocrew-conductor", backend).supported is expected


def test_support_for_any_other_template_needs_a_per_session_channel():
    from kiro_crew.acp_backends import ACP_BACKEND_KIRO

    refused = tma.addition_support("my-agent", ACP_BACKEND_KIRO)
    assert refused.supported is False
    assert refused.reason == tma.UNSUPPORTED_REASON
    assert tma.addition_support("my-agent", "pi").supported is False
    for backend in ("kas", "claude", "codex"):
        assert tma.addition_support("my-agent", backend).supported is True


# -- the conductor installers ------------------------------------------------------

_CONDUCTOR_INSTALLERS = [
    ("_install_conductor_agent", CONDUCTOR_AGENT_FILENAME, "kirocrew-conductor"),
    ("_install_ledger_conductor_agent", LEDGER_CONDUCTOR_AGENT_FILENAME, "kirocrew-conductor"),
    (
        "_install_pipeline_conductor_agent",
        PIPELINE_CONDUCTOR_AGENT_FILENAME,
        "kirocrew-pipeline-conductor",
    ),
    (
        "_install_security_conductor_agent",
        SECURITY_CONDUCTOR_AGENT_FILENAME,
        "kirocrew-security-conductor",
    ),
]


def _pin_conductor_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: SPEC_PERMISSIONS_MIN_VERSION
    )
    monkeypatch.setattr(
        agent,
        "build_agent_config",
        lambda: {
            "name": "kirocrew",
            "prompt": "file://x",
            "mcpServers": {
                "kirocrew-core": {"command": "/resolved/kirocrew", "args": ["mcp-core"]},
            },
            "tools": ["@kirocrew-core"],
            "allowedTools": [],
        },
    )
    monkeypatch.setattr(
        agent, "_kirocrew_mcp_invocation", lambda sub: ("/resolved/kirocrew", [sub])
    )
    monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: True)


def _install_conductors_and_worker(tmp_path, monkeypatch) -> dict[str, dict]:
    _pin_conductor_template(tmp_path, monkeypatch)
    if not (tmp_path / AGENT_FILENAME).exists():
        (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    out = {}
    for installer, filename, _ in _CONDUCTOR_INSTALLERS:
        assert getattr(agent, installer)() is True
        out[Path(filename).stem] = json.loads((tmp_path / filename).read_text(encoding="utf-8"))
    agent._install_worker_agent()
    out["kirocrew-worker"] = json.loads(
        (tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8")
    )
    return out


@pytest.mark.parametrize(("installer", "filename", "template"), _CONDUCTOR_INSTALLERS)
def test_a_conductor_installer_merges_its_additions_and_grants_nothing(
    tmp_path, monkeypatch, installer, filename, template
):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _pin_conductor_template(tmp_path, monkeypatch)
    _add(template)
    assert getattr(agent, installer)() is True
    data = json.loads((tmp_path / filename).read_text(encoding="utf-8"))
    assert data["mcpServers"]["fetch"]["command"] == _ENTRY["command"]
    assert "@fetch" in data["tools"]
    # No trust from the record: not in allowedTools, no autoApprove, and the
    # Crew-shipped servers are still exactly what the base mounts.
    assert not any(ref.startswith("@fetch") for ref in data["allowedTools"])
    assert "autoApprove" not in data["mcpServers"]["fetch"]
    assert {"kirocrew-core", "kirocrew-dashboard", "kirocrew-work"} <= set(data["mcpServers"])


@pytest.mark.parametrize(("installer", "filename", "template"), _CONDUCTOR_INSTALLERS)
def test_a_removed_conductor_addition_is_gone_on_the_next_install(
    tmp_path, monkeypatch, installer, filename, template
):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _pin_conductor_template(tmp_path, monkeypatch)
    _add(template)
    assert getattr(agent, installer)() is True
    assert tma.remove_addition(template, "fetch")
    assert getattr(agent, installer)() is True
    data = json.loads((tmp_path / filename).read_text(encoding="utf-8"))
    assert "fetch" not in data["mcpServers"]
    assert "@fetch" not in data["tools"]


def test_another_templates_addition_does_not_reach_a_conductor(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _pin_conductor_template(tmp_path, monkeypatch)
    _add("kirocrew-pipeline-conductor")
    assert agent._install_conductor_agent() is True
    data = json.loads((tmp_path / CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert "fetch" not in data["mcpServers"]


# -- the spawn merge (templates Crew does not own) ---------------------------------


def test_merge_at_spawn_merges_a_custom_template_and_writes_nothing(home, tmp_path):
    _add("my-agent")
    spec = {"tools": ["@kirocrew-core"], "mcpServers": {"kirocrew-core": {"command": "k"}}}
    before = json.dumps(spec)
    out = tma.merge_at_spawn("my-agent", spec)
    assert out is not spec and json.dumps(spec) == before
    assert out["mcpServers"]["fetch"] == _ENTRY and "@fetch" in out["tools"]
    assert "allowedTools" not in out


def test_merge_at_spawn_leaves_a_conductor_alone(home):
    """A conductor's installer already merged its additions into its file."""
    for template in tma.INSTALLER_MERGED_TEMPLATES:
        _add(template)
        spec = {"tools": [], "mcpServers": {}}
        assert tma.merge_at_spawn(template, spec) is spec


def test_the_array_hosts_read_merges_a_custom_template(home, tmp_path, monkeypatch):
    from kiro_crew.acp import session_mcp

    spec = {"name": "my-agent", "tools": ["@kirocrew-core"], "mcpServers": {}}
    monkeypatch.setattr(
        session_mcp, "_agent_spec_and_snapshot_for", lambda *a, **k: (dict(spec), None)
    )
    _add("my-agent")
    got, snapshot = session_mcp._session_spec_and_snapshot_for("my-agent")
    assert got["mcpServers"]["fetch"] == _ENTRY and snapshot is None
    servers = session_mcp.session_mcp_servers("my-agent", spec=got, muted=())
    assert "fetch" in {element["name"] for element in servers}


def test_the_kas_read_merges_a_custom_template(home, tmp_path, monkeypatch):
    from kiro_crew.acp.harness import kas

    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "my-agent.json").write_text(
        json.dumps({"name": "my-agent", "tools": ["@kirocrew-core"], "mcpServers": {}}),
        encoding="utf-8",
    )
    _add("my-agent")
    got, where = kas.resolve_projected_spec(agents, "my-agent", None)
    assert where == agents
    assert got["mcpServers"]["fetch"] == _ENTRY and "@fetch" in got["tools"]
    on_disk = json.loads((agents / "my-agent.json").read_text(encoding="utf-8"))
    assert on_disk["mcpServers"] == {}


def test_the_kas_read_merges_a_project_only_template(home, tmp_path, monkeypatch):
    """A checkout's spec is filtered to its honoured keys; the approved additions come
    from the user's own record, so they reach it after the filter."""
    from kiro_crew.acp.harness import kas

    agents = tmp_path / "agents"
    agents.mkdir()
    checkout = tmp_path / "checkout"
    (checkout / ".kiro" / "agents").mkdir(parents=True)
    (checkout / ".kiro" / "agents" / "proj-agent.json").write_text(
        json.dumps({"name": "proj-agent", "tools": ["@kirocrew-core"]}), encoding="utf-8"
    )
    monkeypatch.setattr(kas.paths_mod, "mate_agents_dir", lambda: tmp_path / "no-mates")
    _add("proj-agent")
    got, _where = kas.resolve_projected_spec(agents, "proj-agent", str(checkout))
    assert got["mcpServers"]["fetch"] == _ENTRY and "@fetch" in got["tools"]


def test_the_kas_read_keeps_a_checkout_mute_on_an_approved_server(home, tmp_path, monkeypatch):
    from kiro_crew.acp.harness import kas

    agents = tmp_path / "agents"
    agents.mkdir()
    checkout = tmp_path / "checkout"
    (checkout / ".kiro" / "agents").mkdir(parents=True)
    (checkout / ".kiro" / "agents" / "proj-agent.json").write_text(
        json.dumps(
            {
                "name": "proj-agent",
                "tools": ["@kirocrew-core"],
                "mcpServers": {
                    "fetch": {"disabled": True, "command": "checkout-cmd"},
                    "other": {"disabledTools": ["x"], "command": "never"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(kas.paths_mod, "mate_agents_dir", lambda: tmp_path / "no-mates")
    _add("proj-agent")
    got, _where = kas.resolve_projected_spec(agents, "proj-agent", str(checkout))
    assert got["mcpServers"]["fetch"] == {**_ENTRY, "disabled": True}
    # Only switch-off keys for an approved name: no checkout launch, no other name.
    assert "other" not in got["mcpServers"]


def test_an_addition_landing_mid_read_never_launches_a_checkout_muted_server(
    home, tmp_path, monkeypatch
):
    from kiro_crew import template_mcp_additions as tma
    from kiro_crew.acp.harness import kas

    agents = tmp_path / "agents"
    agents.mkdir()
    checkout = tmp_path / "checkout"
    (checkout / ".kiro" / "agents").mkdir(parents=True)
    (checkout / ".kiro" / "agents" / "proj-agent.json").write_text(
        json.dumps(
            {"name": "proj-agent", "tools": [], "mcpServers": {"fetch": {"disabled": True}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(kas.paths_mod, "mate_agents_dir", lambda: tmp_path / "no-mates")
    real = tma.read_additions
    calls = {"n": 0}

    def racing(template):
        # The record is empty on the first read and holds ``fetch`` from then on,
        # as if ``add_addition`` ran between two reads of one spawn.
        calls["n"] += 1
        if calls["n"] == 1:
            return {}
        if calls["n"] == 2:
            _add("proj-agent")
        return real(template)

    monkeypatch.setattr(tma, "read_additions", racing)
    got, _where = kas.resolve_projected_spec(agents, "proj-agent", str(checkout))
    fetch = (got.get("mcpServers") or {}).get("fetch")
    assert fetch is None or fetch.get("disabled") is True


def test_the_kas_read_merges_a_crewmate_folder_template(home, tmp_path, monkeypatch):
    from kiro_crew.acp.harness import kas

    agents = tmp_path / "agents"
    agents.mkdir()
    mates = tmp_path / "mates"
    mates.mkdir()
    (mates / "their-agent.json").write_text(
        json.dumps({"name": "their-agent", "tools": [], "mcpServers": {}}), encoding="utf-8"
    )
    monkeypatch.setattr(kas.paths_mod, "mate_agents_dir", lambda: mates)
    _add("their-agent")
    got, where = kas.resolve_projected_spec(agents, "their-agent", None)
    assert where == mates
    assert got["mcpServers"]["fetch"] == _ENTRY


# -- doctor --------------------------------------------------------------------------


def test_the_doctor_row_lists_each_templates_additions(home, capsys):
    from kiro_crew.doctor_checks import mcp

    mcp._doctor_template_mcp_additions()
    assert capsys.readouterr().out == ""
    _add("kirocrew-conductor", card="card-9")
    mcp._doctor_template_mcp_additions()
    out = capsys.readouterr().out
    assert "mcp additions: 'kirocrew-conductor'" in out
    assert "'fetch' (card 'card-9'," in out


def _page_switches(monkeypatch, switched: dict[str, list[str]]) -> None:
    """The MCP page's ``disabledTools`` switch, as ``list_servers`` reports it."""
    from types import SimpleNamespace

    from kiro_crew import mcp_discovery

    rows = [SimpleNamespace(name=n, disabled_tools=list(t)) for n, t in switched.items()]
    monkeypatch.setattr(mcp_discovery, "list_servers", lambda: rows)


def test_an_addition_never_brings_back_a_tool_the_page_switched_off(home, monkeypatch):
    tma.add_addition(
        "kirocrew-conductor", "github", {"command": "gh-mcp"}, approved_by="u", card_id="c"
    )
    _page_switches(monkeypatch, {"github": ["delete_repo"], "other": ["x"]})
    merged = tma.merge_into("kirocrew-conductor", {})
    assert merged["github"]["disabledTools"] == ["delete_repo"]
    assert merged["github"]["command"] == "gh-mcp"
    # A tool switched off after the approval is off on the next merge too.
    _page_switches(monkeypatch, {"github": ["delete_repo", "push"]})
    assert tma.merge_into("kirocrew-conductor", {})["github"]["disabledTools"] == [
        "delete_repo",
        "push",
    ]


def test_a_restriction_and_the_page_switch_both_apply(home, monkeypatch):
    tma.add_addition(
        "kirocrew-conductor", "github", {"command": "gh-mcp"}, approved_by="u", card_id="c"
    )
    _page_switches(monkeypatch, {"github": ["delete_repo"]})
    merged = tma.merge_into("kirocrew-conductor", {"github": {"disabledTools": ["push"]}})
    assert merged["github"]["disabledTools"] == ["push", "delete_repo"]


def test_an_unreadable_page_adds_no_switch(home, monkeypatch):
    from kiro_crew import mcp_discovery

    tma.add_addition(
        "kirocrew-conductor", "github", {"command": "gh-mcp"}, approved_by="u", card_id="c"
    )

    def boom():
        raise OSError("config unreadable")

    monkeypatch.setattr(mcp_discovery, "list_servers", boom)
    assert "disabledTools" not in tma.merge_into("kirocrew-conductor", {})["github"]
