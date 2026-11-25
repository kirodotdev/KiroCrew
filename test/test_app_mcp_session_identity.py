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
    (app / "app.json").write_text(
        json.dumps({"name": name, "mcpServers": servers}), encoding="utf-8"
    )


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


def _elements(monkeypatch, servers, launches):
    from kiro_crew.acp import session_mcp

    monkeypatch.setattr(session_mcp, "_registry_mode", lambda: False)
    monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", lambda name, **_kw: None)
    monkeypatch.setattr(session_mcp, "builtin_app_identity_launches", lambda: launches)
    spec = {"tools": [f"@{name}" for name in servers], "mcpServers": servers}
    return spec, session_mcp.session_mcp_servers("mochi--mochi", spec=spec, muted=())


def _array(monkeypatch, entry, launches):
    _spec_unused, (element,) = _elements(monkeypatch, {"mochi:mochi": entry}, launches)
    return {p["name"]: p["value"] for p in element["env"]}


def _governed(monkeypatch, max_age=600):
    from kiro_crew.platform import policy_distribution as pd

    monkeypatch.setattr(pd, "central_ceiling_installed", lambda: True)
    monkeypatch.setattr(pd, "effective_max_cache_age", lambda: max_age)
    return pd


def test_the_session_array_gives_the_app_server_the_gateway_policy_markers(monkeypatch):
    """The array other ACP harnesses receive carries the markers on a governed host."""
    from kiro_crew.platform import policy_distribution as pd

    entry = {**PINNED, "env": {**PINNED["env"], pd.POLICY_MAX_AGE_ENV: "9"}}
    assert _array(monkeypatch, entry, {"mochi:mochi": dict(PINNED)}) == entry["env"]

    _governed(monkeypatch, max_age=0)
    env = _array(monkeypatch, entry, {"mochi:mochi": dict(PINNED)})

    assert env[pd.POLICY_CACHE_ONLY_ENV] == "1"
    # The entry's own copy is the spec's, not the gateway's: it is dropped.
    assert pd.POLICY_MAX_AGE_ENV not in env
    assert env["KIROCREW_HOME"] == "/crew/home"
    assert pd.POLICY_MAX_AGE_ENV in entry["env"]

    _governed(monkeypatch, max_age=600)
    env = _array(monkeypatch, entry, {"mochi:mochi": dict(PINNED)})
    assert env[pd.POLICY_MAX_AGE_ENV] == "600"


@pytest.mark.parametrize(
    "entry",
    [
        {"command": "/tmp/other", "args": PINNED["args"]},
        {"command": PINNED["command"], "args": [*PINNED["args"], "--x"]},
    ],
    ids=["command", "args"],
)
def test_a_hand_edited_app_launch_gets_no_policy_markers_in_the_array(monkeypatch, entry):
    pd = _governed(monkeypatch)
    env = _array(monkeypatch, entry, {"mochi:mochi": PINNED})

    assert pd.POLICY_CACHE_ONLY_ENV not in env


def test_a_user_server_beside_the_app_entry_is_as_on_main(monkeypatch):
    servers = {
        "mochi:mochi": dict(PINNED),
        "user-srv": {"command": "/opt/user", "args": ["serve"], "env": {"A": "1"}},
    }
    _spec_unused, before = _elements(monkeypatch, servers, {"mochi:mochi": dict(PINNED)})
    _governed(monkeypatch)
    _spec_unused, after = _elements(monkeypatch, servers, {"mochi:mochi": dict(PINNED)})

    by_name = {e["name"]: e for e in after}
    assert by_name["user-srv"] == next(e for e in before if e["name"] == "user-srv")
    assert by_name["mochi:mochi"] != next(e for e in before if e["name"] == "mochi:mochi")


@pytest.mark.parametrize(
    "entry",
    [
        {**PINNED, "env": None},
        {**PINNED, "env": [{"name": "A", "value": "1"}]},
        {**PINNED, "args": "-m kiro_crew"},
    ],
    ids=["env-none", "env-list", "args-string"],
)
def test_a_malformed_app_entry_is_left_as_is(monkeypatch, entry):
    servers = {"mochi:mochi": entry}
    _spec_unused, before = _elements(monkeypatch, servers, {"mochi:mochi": dict(PINNED)})
    _governed(monkeypatch)
    _spec_unused, after = _elements(monkeypatch, servers, {"mochi:mochi": dict(PINNED)})

    assert after == before


def test_goose_keeps_the_reference_and_the_markers_on_an_app_element(monkeypatch):
    """goose writes the array to its sessions.db, so a resolved value must not ride it."""
    from kiro_crew.acp.session_mcp_env_refs import keep_env_references

    pd = _governed(monkeypatch)
    monkeypatch.setenv("MOCHI_REGION", "eu-west-1")
    entry = {**PINNED, "env": {**PINNED["env"], "REGION": "${MOCHI_REGION}"}}
    spec, elements = _elements(monkeypatch, {"mochi:mochi": entry}, {"mochi:mochi": PINNED})
    assert {"name": "REGION", "value": "eu-west-1"} in elements[0]["env"]

    (kept,) = keep_env_references(elements, spec)
    env = {p["name"]: p["value"] for p in kept["env"]}
    assert env["REGION"] == "${MOCHI_REGION}"
    assert env[pd.POLICY_CACHE_ONLY_ENV] == "1"
    assert env[pd.POLICY_MAX_AGE_ENV] == "600"


@pytest.mark.parametrize("later", ["new-bound", "no-ceiling", "launches-empty", "launches-fail"])
def test_goose_keeps_the_reference_when_the_gateway_changes_after_the_array(monkeypatch, later):
    """The match reads the element's own markers, not the gateway a second time."""
    from kiro_crew.acp.session_mcp_env_refs import keep_env_references

    pd = _governed(monkeypatch)
    monkeypatch.setenv("MOCHI_REGION", "eu-west-1")
    entry = {**PINNED, "env": {**PINNED["env"], "REGION": "${MOCHI_REGION}"}}
    spec, elements = _elements(monkeypatch, {"mochi:mochi": entry}, {"mochi:mochi": PINNED})
    if later == "new-bound":
        monkeypatch.setattr(pd, "effective_max_cache_age", lambda: 30)
    elif later == "no-ceiling":
        monkeypatch.setattr(pd, "central_ceiling_installed", lambda: False)
    else:
        from kiro_crew.acp import session_mcp

        def _gone():
            if later == "launches-fail":
                raise OSError("manifest unreadable")
            return {}

        monkeypatch.setattr(session_mcp, "builtin_app_identity_launches", _gone)

    (kept,) = keep_env_references(elements, spec)
    env = {p["name"]: p["value"] for p in kept["env"]}
    assert env["REGION"] == "${MOCHI_REGION}"
    assert env[pd.POLICY_CACHE_ONLY_ENV] == "1"
    assert env[pd.POLICY_MAX_AGE_ENV] == "600"
