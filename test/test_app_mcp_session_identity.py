"""Builtin app MCP servers get the same per-session identity element as Crew's own.

The reported defect: a builtin app whose shipped manifest runs the host CLI
(``mochi:mochi``, ``command: "kirocrew"``) is mounted by kiro-cli straight from the
agent spec. It serves through ``mcp_shared.run_mcp_stdio_loop``, whose
``tools/call`` reads the session tool policy behind an attestation, and with no
session token every call refused as ``identity_unattested`` for session ``mcp``.
``kiro_control_plane_servers`` now re-declares those servers too, so the token
attach that follows reaches them, with the launch re-derived from the manifest.
"""

from __future__ import annotations

import json

import pytest

PINNED = {
    "command": "/crew/python3.12",
    "args": ["-s", "-P", "-m", "kiro_crew", "app", "mcp", "mochi"],
    "env": {"PYTHONPATH": "/crew/site-packages", "KIROCREW_HOME": "/crew/home"},
}


def _mount(monkeypatch, tmp_path, spec, launches):
    from kiro_crew.acp import session_mcp

    monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda *a, **k: spec)
    monkeypatch.setattr(session_mcp, "_global_settings", lambda **kwargs: {})
    monkeypatch.setattr(session_mcp, "_registry_mode", lambda: False)
    monkeypatch.setattr(
        session_mcp,
        "managed_mcp_spec_entry",
        lambda name, **_kw: dict(spec["mcpServers"][name]) if name in spec["mcpServers"] else None,
    )
    monkeypatch.setattr(session_mcp, "builtin_app_identity_launches", lambda: launches)
    return session_mcp.kiro_control_plane_servers("mochi--mochi", work_dir=tmp_path)


def _spec(entry):
    return {
        "tools": ["@kirocrew-core", "@mochi:mochi"],
        "mcpServers": {
            "kirocrew-core": {"command": "test-crew", "args": ["mcp-core"]},
            "mochi:mochi": entry,
        },
    }


def test_granted_builtin_app_server_gets_an_identity_element(tmp_path, monkeypatch):
    spec = _spec(dict(PINNED))
    elements = _mount(monkeypatch, tmp_path, spec, {"mochi:mochi": dict(PINNED)}).elements

    assert [e["name"] for e in elements] == ["kirocrew-core", "mochi:mochi"]
    mochi = elements[1]
    assert (mochi["command"], mochi["args"]) == (PINNED["command"], PINNED["args"])
    env = {p["name"]: p["value"] for p in mochi["env"]}
    assert env["PYTHONPATH"] == "/crew/site-packages"
    assert env["KIROCREW_HOME"] == "/crew/home"


def test_the_token_attach_reaches_the_app_element(tmp_path, monkeypatch):
    """The element is only useful because the runtime's attach covers every entry."""
    from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
    from kiro_crew.mcp_gateway.session_servers import attach_stub_session_token

    spec = _spec(dict(PINNED))
    elements = _mount(monkeypatch, tmp_path, spec, {"mochi:mochi": dict(PINNED)}).elements
    tokened = attach_stub_session_token(elements, "tok-123")
    mochi = next(e for e in tokened if e["name"] == "mochi:mochi")
    assert {"name": STUB_SESSION_TOKEN_ENV, "value": "tok-123"} in mochi["env"]


def test_a_hand_edited_app_launch_is_replaced_by_the_manifest_one(tmp_path, monkeypatch):
    """A spec entry naming another program never receives the element (or the token)."""
    spec = _spec({"command": "/tmp/evil", "args": ["--steal"]})
    elements = _mount(monkeypatch, tmp_path, spec, {"mochi:mochi": dict(PINNED)}).elements

    mochi = next(e for e in elements if e["name"] == "mochi:mochi")
    assert (mochi["command"], mochi["args"]) == (PINNED["command"], PINNED["args"])


def test_spec_env_cannot_move_the_app_data_home(tmp_path, monkeypatch):
    spec = _spec({**PINNED, "env": {"KIROCREW_HOME": "/elsewhere", "PYTHONPATH": "/evil"}})
    elements = _mount(monkeypatch, tmp_path, spec, {"mochi:mochi": dict(PINNED)}).elements

    env = {
        p["name"]: p["value"]
        for p in next(e for e in elements if e["name"] == "mochi:mochi")["env"]
    }
    assert env["KIROCREW_HOME"] == "/crew/home"
    assert env["PYTHONPATH"] == "/crew/site-packages"


def test_an_ungranted_app_server_gets_no_element(tmp_path, monkeypatch):
    spec = _spec(dict(PINNED))
    spec["tools"] = ["@kirocrew-core"]
    elements = _mount(monkeypatch, tmp_path, spec, {"mochi:mochi": dict(PINNED)}).elements

    assert [e["name"] for e in elements] == ["kirocrew-core"]


def test_an_app_server_outside_the_shipped_set_gets_no_element(tmp_path, monkeypatch):
    """A user app or an app shipping its own binary is not Crew's code: no element."""
    spec = _spec(dict(PINNED))
    elements = _mount(monkeypatch, tmp_path, spec, {}).elements

    assert [e["name"] for e in elements] == ["kirocrew-core"]


def test_a_muted_app_server_stays_native(tmp_path, monkeypatch):
    """Native restrictions still withhold the element, as for the managed servers."""
    spec = _spec({**PINNED, "disabledTools": ["perform_pet_action"]})
    mount = _mount(monkeypatch, tmp_path, spec, {"mochi:mochi": dict(PINNED)})

    assert [e["name"] for e in mount.elements] == ["kirocrew-core"]
    assert "mochi:mochi" in mount.withheld


def test_a_stubbed_app_server_is_not_duplicated(tmp_path, monkeypatch):
    from kiro_crew.acp import session_mcp

    spec = _spec(dict(PINNED))
    monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda *a, **k: spec)
    monkeypatch.setattr(session_mcp, "_global_settings", lambda **kwargs: {})
    monkeypatch.setattr(session_mcp, "_registry_mode", lambda: False)
    monkeypatch.setattr(
        session_mcp,
        "managed_mcp_spec_entry",
        lambda name, **_kw: dict(spec["mcpServers"][name]) if name in spec["mcpServers"] else None,
    )
    monkeypatch.setattr(
        session_mcp, "builtin_app_identity_launches", lambda: {"mochi:mochi": dict(PINNED)}
    )
    elements = session_mcp.kiro_control_plane_servers(
        "mochi--mochi", work_dir=tmp_path, existing_names={"mochi:mochi"}
    ).elements

    assert [e["name"] for e in elements] == ["kirocrew-core"]


def test_unreadable_settings_withhold_the_app_servers_too(tmp_path, monkeypatch):
    """The withheld verdicts cover every server the function would have mounted."""
    from kiro_crew.acp import session_mcp

    unreadable = session_mcp.NativeSettingsUnreadable(
        session_mcp.NATIVE_SOURCE_PROJECT, tmp_path / "settings.json"
    )

    def _raise(_work_dir):
        raise unreadable

    monkeypatch.setattr(session_mcp, "native_settings_sources", _raise)
    mount = _mount(monkeypatch, tmp_path, _spec(dict(PINNED)), {"mochi:mochi": dict(PINNED)})

    assert mount.elements == []
    assert mount.withheld["mochi:mochi"] is unreadable
    assert set(session_mcp.IDENTITY_BOUND_SERVERS) <= set(mount.withheld)


def _write_manifest(root, name, servers):
    app = root / name
    app.mkdir()
    (app / "app.json").write_text(json.dumps({"name": name, "mcpServers": servers}))


def test_only_host_cli_servers_of_builtin_owned_apps_are_launches(tmp_path, monkeypatch):
    from kiro_crew.apps import execution, manager

    _write_manifest(
        tmp_path, "mochi", {"mochi": {"command": "kirocrew", "args": ["app", "mcp", "mochi"]}}
    )
    _write_manifest(tmp_path, "ownbin", {"srv": {"command": "/opt/ownbin", "args": []}})
    _write_manifest(tmp_path, "remote", {"srv": {"command": "kirocrew", "url": "http://x"}})
    _write_manifest(tmp_path, "badargs", {"srv": {"command": "kirocrew", "args": "app"}})
    _write_manifest(tmp_path, "shadowed", {"srv": {"command": "kirocrew", "args": ["app"]}})
    monkeypatch.setattr(execution, "_builtin_manifest_sources", lambda: (tmp_path.resolve(),))
    monkeypatch.setattr(manager, "builtin_owns_installed", lambda name: name != "shadowed")

    assert execution.builtin_app_host_cli_mcp_launches() == {
        "mochi:mochi": ("mochi", ("app", "mcp", "mochi"))
    }


def test_shipped_mochi_manifest_is_a_host_cli_launch():
    """Pin the real manifest: if Mochi stops shipping a host-CLI server this test
    says so, rather than the element silently disappearing."""
    from kiro_crew.apps import execution

    with pytest.MonkeyPatch.context() as mp:
        from kiro_crew.apps import manager

        mp.setattr(manager, "builtin_owns_installed", lambda name: True)
        launches = execution.builtin_app_host_cli_mcp_launches()
    assert launches.get("mochi:mochi") == ("mochi", ("app", "mcp", "mochi"))
