"""An app's tools are advertised only while the app is enabled.

The nine tools of the ``apps`` domain under :mod:`kiro_crew.mcp_tools` reach
four built-in apps -- Issue Radar, Dev Fleet, Ops Mission Control and Design
Tweak -- whose gateway routes refuse a call with HTTP 403 while the app is
disabled (Dev Fleet's routes and the app reverse proxy Design Tweak sits behind
name the refusal ``app_not_enabled``, Ops Mission Control ``app_disabled``,
Issue Radar carries no code). ``tools/list`` therefore omits a tool while its
app is disabled: a session with none of the apps enabled sees none of them,
instead of tools that can only refuse.

The listing and the call must agree from ONE predicate: the ``installed.json``
reader in :mod:`kiro_crew.apps.manager` that every one of those routes consults.
These tests write real ``installed.json`` records into the isolated test home
rather than stubbing the reader, so what is pinned is the agreement itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps import manager as apps_manager
from kiro_crew.mcp_tools import apps as apps_tools
from kiro_crew.mcp_tools import build_tool_list, build_tool_names, dispatch

ISSUE_RADAR_TOOLS = frozenset(
    {"issue_radar_crew_read", "issue_radar_crew_record", "issue_radar_record_investigation"}
)
DEV_FLEET_TOOLS = frozenset({"pod_up", "pod_down", "pod_status", "pod_ls"})
OPS_MISSION_CONTROL_TOOLS = frozenset({"ops_mission_control_api"})
DESIGN_TWEAK_TOOLS = frozenset({"design_tweak_update_thread"})
APP_TOOLS = {
    "issue-radar": ISSUE_RADAR_TOOLS,
    "dev-fleet": DEV_FLEET_TOOLS,
    "ops-mission-control": OPS_MISSION_CONTROL_TOOLS,
    "design-tweak": DESIGN_TWEAK_TOOLS,
}
ALL_APP_TOOLS = ISSUE_RADAR_TOOLS | DEV_FLEET_TOOLS | OPS_MISSION_CONTROL_TOOLS | DESIGN_TWEAK_TOOLS


def _write_installed(name: str, *, enabled: bool) -> Path:
    """A real ``installed.json`` for *name* in the isolated test home."""
    path = apps_manager.app_dir(name) / apps_manager.INSTALLED_META_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": name, "enabled": enabled}), encoding="utf-8")
    return path


def _listed() -> set[str]:
    return {t["name"] for t in build_tool_list()}


def test_no_app_installed_lists_none_of_the_app_tools() -> None:
    """A fresh home has no app enabled, so the listing carries none of the app tools."""
    assert _listed() & ALL_APP_TOOLS == set()


def test_a_disabled_app_lists_none_of_its_tools() -> None:
    """Installed but switched off is the state every app's gateway route refuses."""
    for name in APP_TOOLS:
        _write_installed(name, enabled=False)
    assert _listed() & ALL_APP_TOOLS == set()


@pytest.mark.parametrize("enabled_app", sorted(APP_TOOLS))
def test_one_enabled_app_lists_exactly_its_tools(enabled_app: str) -> None:
    """Enabling one app surfaces its tools and nothing of its siblings."""
    for name in APP_TOOLS:
        _write_installed(name, enabled=name == enabled_app)
    assert _listed() & ALL_APP_TOOLS == APP_TOOLS[enabled_app]


def test_every_app_enabled_lists_every_app_tool_with_its_declared_shape() -> None:
    """The gate decides WHETHER a descriptor is emitted, never its shape."""
    for name in APP_TOOLS:
        _write_installed(name, enabled=True)
    listed = {t["name"]: t for t in build_tool_list()}
    declared = {t["name"]: t for t in apps_tools.schemas()}
    assert set(declared) == ALL_APP_TOOLS
    for name, spec in declared.items():
        assert listed[name] == spec, name


def test_listing_agrees_with_the_call_gate_from_one_predicate() -> None:
    """Every state the route's ``is_app_enabled`` reads, the listing reads the same way.

    Absent, disabled and enabled are the three states the predicate can settle; for
    each, a tool is listed exactly when the gateway would let its call through.
    """
    _write_installed("dev-fleet", enabled=True)
    _write_installed("ops-mission-control", enabled=False)
    # issue-radar: no installed.json at all.
    listed = _listed()
    for name, tools in APP_TOOLS.items():
        callable_ = apps_manager.is_app_enabled(name)
        assert (tools <= listed) is callable_, name
        assert (tools & listed == set()) is not callable_, name


@pytest.mark.parametrize("body", ["{not json", "null", "[]", '"off"'])
def test_unreadable_enablement_lists_the_tools(body: str) -> None:
    """A read fault lists rather than hides, as the policy filter does.

    kiro-cli calls ``tools/list`` once per session and caches it, so hiding on a
    transient fault would hide the tools for the session's whole life; the call
    path still refuses while the app cannot be proven enabled, so a listed tool
    that refuses is not a hole. The read may answer ``None`` or may raise; either
    way the descriptor build survives it, because an exception escaping
    ``tools/list`` would withdraw every tool of the pooled server.
    """
    _write_installed("dev-fleet", enabled=True).write_text(body, encoding="utf-8")
    try:
        state = apps_manager.app_enabled_state("dev-fleet")
    except Exception:
        state = None
    assert state is not False
    assert DEV_FLEET_TOOLS <= _listed()


def test_the_names_only_build_lists_the_declaration_without_reading(monkeypatch) -> None:
    """The names-only build never consults the gate, so it reads no enablement record.

    ``build_tool_names`` is what in-process discovery reads on the gateway's event
    loop, keeping only tool NAMES; it takes each domain's ``schemas()`` directly,
    the same rule that keeps ``spawn.schemas`` and ``control.schemas`` from their
    live reads on that path. Every app is installed and disabled here, so the full
    build (the stdio server's answer to a model) and the names-only build differ
    visibly: the first hides the app tools, the second still names them all.
    """
    for name in APP_TOOLS:
        _write_installed(name, enabled=False)
    reads: list[str] = []
    real = apps_tools.app_enabled_state

    def _counted(name: str) -> bool | None:
        reads.append(name)
        return real(name)

    monkeypatch.setattr(apps_tools, "app_enabled_state", _counted)
    assert _listed() & ALL_APP_TOOLS == set()
    assert reads, "the full build must read enablement"
    reads.clear()

    names = build_tool_names()
    assert ALL_APP_TOOLS <= set(names)
    assert names == [t["name"] for t in build_tool_list(names_only=True)]
    assert reads == []


def test_dispatch_of_a_hidden_tool_still_reaches_the_gateway_refusal(monkeypatch) -> None:
    """Hiding a descriptor changes the listing only; the call path is untouched.

    The handler still runs and still surfaces the route's own ``app_not_enabled``
    refusal -- a hidden tool is not an unknown one.
    """
    from kiro_crew import mcp_core

    refusal = {
        "ok": False,
        "code": "app_not_enabled",
        "error": "dev-fleet is not enabled. Turn the Dev Fleet app on in the dashboard App Store.",
    }
    monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: dict(refusal))
    assert "pod_ls" not in _listed()
    out = dispatch("pod_ls", {})
    assert out.startswith("Error: pod ls failed [app_not_enabled]")
    assert "Unknown tool" not in out


def test_declaration_and_handlers_cover_every_app_tool_regardless_of_enablement() -> None:
    """The gate sits between the declaration and the listing, not inside either half.

    ``schemas()`` is what ``test_mcp_tool_registry`` holds against ``HANDLERS``; both
    must keep naming every tool, or a disabled app would read as a registry drift.
    """
    assert {t["name"] for t in apps_tools.schemas()} == ALL_APP_TOOLS
    assert set(apps_tools.HANDLERS) == ALL_APP_TOOLS
    assert set(apps_tools.TOOL_APPS) == ALL_APP_TOOLS


def test_tool_apps_name_the_apps_whose_routes_gate_the_calls() -> None:
    """``TOOL_APPS`` is spelled in literals; each must be the name the route reads.

    The listing agrees with the call only if both read the same ``installed.json``,
    and the file is found by the app's installed name -- so a literal that drifts
    from the app's own ``APP_NAME`` would gate the listing on a record the route
    never consults. Design Tweak's backend keeps its constant in the server module
    (whose import starts the process plumbing), so it is pinned to the manifest
    name the app is installed under, which is what the app reverse proxy reads.
    """
    from kiro_crew.apps.builtins import design_tweak as design_tweak_pkg
    from kiro_crew.apps.builtins.dev_fleet import agent_pod_api
    from kiro_crew.apps.builtins.issue_radar.backend import store as issue_radar_store
    from kiro_crew.apps.builtins.ops_mission_control.backend import store as omc_store

    design_tweak_manifest = json.loads(
        (Path(design_tweak_pkg.__file__).parent / "app.json").read_text(encoding="utf-8")
    )
    expected = {
        **{tool: issue_radar_store.APP_NAME for tool in ISSUE_RADAR_TOOLS},
        **{tool: agent_pod_api.APP_NAME for tool in DEV_FLEET_TOOLS},
        **{tool: omc_store.APP_NAME for tool in OPS_MISSION_CONTROL_TOOLS},
        **{tool: design_tweak_manifest["name"] for tool in DESIGN_TWEAK_TOOLS},
    }
    assert apps_tools.TOOL_APPS == expected
    assert set(expected.values()) == set(APP_TOOLS)
