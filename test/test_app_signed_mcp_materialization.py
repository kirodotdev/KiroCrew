"""An agent rebuild emits an app's MCP servers only when registration would admit them.

``_collect_app_mcp_servers`` re-derives every enabled app's MCP entries into the
default agent spec on each rebuild. It must apply the same decision the MCP
registration bridge applies (``bridges._registration_denied(action="mcp_register")``):
otherwise a server that registration refused (an unsigned stdio command under a
signed self-registered app's ``data/``, or an app a fleet policy bans) is copied
straight from the manifest, or from a stale live entry, into the spec kiro-cli runs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp_merge_helpers import bundled_defaults, run_install_mcp_merge
from test_app_signed_round3 import sign
from test_app_signed_round3 import signed_home as _signed_home
from test_app_signed_round3 import write_app

from kiro_crew.apps import manager

signed_home = _signed_home

_STDIO = {"command": "python3", "args": ["data/srv.py"]}
_URL = {"url": "http://127.0.0.1:9100/mcp"}


def _signed_external_app(tmp_path: Path, server: dict) -> Path:
    """Register a signed self-registered app ``probe`` declaring *server*."""
    tree = tmp_path / "external-app"
    write_app(tree, mcpServers={"tools": server})
    (tree / "server.py").write_text("print('runs outside the gateway')\n", encoding="utf-8")
    sign(tree)
    document = json.loads((tree / "app.json").read_text(encoding="utf-8"))
    result = manager.register_external_app(
        "probe",
        "1.0.0",
        "Probe",
        source=str(tree),
        manifest_data=document,
        version_checked=True,
    )
    assert result.ok, result.error
    return manager.app_dir("probe")


def _rebuild_sees(
    monkeypatch: pytest.MonkeyPatch,
    names: list[str],
    *,
    live: dict[str, Any] | None = None,
    manifests: dict[str, Any] | None = None,
) -> None:
    """Point the collector at *names*, all enabled, with *live* registered entries."""
    from kiro_crew import agent
    from kiro_crew.apps import bridges

    monkeypatch.setattr(manager, "list_apps", lambda: [{"name": n} for n in names])
    monkeypatch.setattr(manager, "is_app_enabled", lambda n: True)
    if manifests is not None:
        monkeypatch.setattr(manager, "get_app_manifest", lambda n: manifests.get(n))
    monkeypatch.setattr(bridges, "registered_app_mcp_servers", lambda: dict(live or {}))
    monkeypatch.setattr(agent, "_ceiling_filtered_spec", lambda ref, spec, *, audit=True: spec)


def _admit_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)


def _manifest(servers: dict[str, Any]) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        name="x",
        mcpServers=servers,
        agents=[],
        backend=SimpleNamespace(entryPoint=""),
        version="1.0.0",
    )


def test_unsigned_stdio_command_of_signed_external_app_is_not_emitted(
    signed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import agent

    metadata = _signed_external_app(tmp_path, _STDIO)
    (metadata / "data").mkdir(exist_ok=True)
    (metadata / "data" / "srv.py").write_text("print('unsigned')\n", encoding="utf-8")
    _admit_execution(monkeypatch)
    _rebuild_sees(monkeypatch, ["probe"])
    assert agent._collect_app_mcp_servers() == {}


def test_url_only_server_of_signed_external_app_is_kept(
    signed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import agent

    _signed_external_app(tmp_path, _URL)
    _admit_execution(monkeypatch)
    _rebuild_sees(monkeypatch, ["probe"])
    assert agent._collect_app_mcp_servers() == {"probe:tools": _URL}


def test_stale_live_command_entry_of_a_denied_app_is_dropped(
    signed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import agent

    metadata = _signed_external_app(tmp_path, _STDIO)
    (metadata / "data").mkdir(exist_ok=True)
    (metadata / "data" / "srv.py").write_text("print('unsigned')\n", encoding="utf-8")
    _admit_execution(monkeypatch)
    _rebuild_sees(monkeypatch, ["probe"], live={"probe:tools": dict(_STDIO)})
    assert agent._collect_app_mcp_servers() == {}


def test_fleet_banned_enabled_app_is_not_emitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import agent

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _admit_execution(monkeypatch)
    monkeypatch.setattr(
        "kiro_crew.apps.admission.app_admission_denied",
        lambda name, *a, **k: (
            "app 'banned' is denied by fleet policy" if name == "banned" else None
        ),
    )
    _rebuild_sees(
        monkeypatch,
        ["banned", "fine"],
        manifests={"banned": _manifest({"srv": _STDIO}), "fine": _manifest({"srv": _STDIO})},
        live={"banned:srv": {"url": "http://127.0.0.1:5555/mcp"}},
    )
    assert agent._collect_app_mcp_servers() == {"fine:srv": _STDIO}


def test_genuine_builtin_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A shipped builtin needs no third-party grant and keeps its entry."""
    from kiro_crew import agent

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: False)
    _rebuild_sees(
        monkeypatch, ["mochi"], manifests={"mochi": _manifest({"pet": {"command": "mochi-mcp"}})}
    )
    assert agent._collect_app_mcp_servers() == {"mochi:pet": {"command": "mochi-mcp"}}


def test_trusted_app_without_policy_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import agent

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _admit_execution(monkeypatch)
    live = {"plain:web": {"url": "http://127.0.0.1:43210/mcp"}}
    _rebuild_sees(
        monkeypatch,
        ["plain"],
        manifests={"plain": _manifest({"cli": dict(_STDIO), "web": dict(_URL)})},
        live=live,
    )
    assert agent._collect_app_mcp_servers() == {
        "plain:cli": _STDIO,
        "plain:web": {"url": "http://127.0.0.1:43210/mcp"},
    }


def test_an_untrusted_third_party_app_is_not_emitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bridges refuse an app the operator never trusted; so does the rebuild."""
    from kiro_crew import agent

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: False)
    _rebuild_sees(monkeypatch, ["plain"], manifests={"plain": _manifest({"cli": _STDIO})})
    assert agent._collect_app_mcp_servers() == {}


def test_a_failing_admission_check_skips_the_app_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import agent
    from kiro_crew.apps import bridges

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _admit_execution(monkeypatch)
    real = bridges._registration_denied

    def _boom(name: str, **kwargs: Any) -> Any:
        if name == "broken":
            raise RuntimeError("policy store unreadable")
        return real(name, **kwargs)

    monkeypatch.setattr(bridges, "_registration_denied", _boom)
    _rebuild_sees(
        monkeypatch,
        ["broken", "fine"],
        manifests={"broken": _manifest({"srv": _STDIO}), "fine": _manifest({"srv": _STDIO})},
        live={"broken:srv": dict(_STDIO)},
    )
    assert agent._collect_app_mcp_servers() == {"fine:srv": _STDIO}


@pytest.mark.parametrize(
    "enabled, denied, admitted",
    [(True, False, True), (True, True, False), (False, False, False)],
)
def test_the_final_merge_keeps_an_on_disk_app_entry_only_when_admitted(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, denied: bool, admitted: bool
) -> None:
    """The locked merge in write_default_spec re-reads the spec on disk; an enabled
    app the collector refused (banned, untrusted, unsigned command) must not have
    its stale on-disk entry put back."""
    from kiro_crew.agent_materialization import default_spec_commit, mcp_sources

    calls: list[str] = []

    def _denied(name: str, manifest: Any, entries: Any = None) -> bool:
        calls.append(name)
        return denied

    monkeypatch.setattr(mcp_sources, "_app_mcp_admission_denied", _denied)
    cache: dict[str, bool] = {}
    keep = default_spec_commit._app_mcp_key_admitted(
        "probe:tools", cache, lambda n: enabled, lambda n: object()
    )
    assert keep is admitted
    # One decision per app per merge, even with several of its keys.
    default_spec_commit._app_mcp_key_admitted(
        "probe:other", cache, lambda n: enabled, lambda n: object()
    )
    assert calls == (["probe"] if enabled else [])


def test_the_final_merge_drops_an_app_whose_state_cannot_be_read() -> None:
    from kiro_crew.agent_materialization import default_spec_commit

    def _boom(name: str) -> bool:
        raise ValueError("malformed installed.json")

    assert default_spec_commit._app_mcp_key_admitted("probe:tools", {}, _boom, _boom) is False


def test_the_final_merge_reinserts_on_disk_entries_only_through_the_gate() -> None:
    import inspect

    from kiro_crew.agent_materialization import default_spec_commit

    src = inspect.getsource(default_spec_commit.write_default_spec)
    assert "if _k in on_disk_app and _app_of_key_enabled(_k):" in src
    assert "_app_mcp_key_admitted(" in src


@pytest.fixture
def alias_rebuild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., dict]:
    """Rebuild through the real merge, alias normalization and locked spec write."""
    from kiro_crew.apps import bridges

    cfg_dir = bundled_defaults(tmp_path)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "kirocrew_home"))
    monkeypatch.setattr(
        bridges, "_mcp_json_path", lambda: tmp_path / "kiro_agents" / "kirocrew.json"
    )
    monkeypatch.setattr(manager, "app_enabled_state", lambda n: True)
    _admit_execution(monkeypatch)
    _rebuild_sees(
        monkeypatch, ["probe"], manifests={"probe": _manifest({"@playwright/mcp": _STDIO})}
    )

    def rebuild(**scopes: Any) -> dict:
        return run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers=scopes.get("provider", {}),
            kiro_servers=scopes.get("global", {}),
            kirocrew_servers=scopes.get("store", {}),
            which_side_effect=scopes.get("which", lambda c, **kw: c),
        )

    return rebuild


@pytest.mark.parametrize("collision", [False, True])
@pytest.mark.parametrize("revocation", ["fleet", "trust", "disabled", "unreadable"])
def test_nonclean_rebuild_removes_revoked_app_alias_and_refs(
    alias_rebuild: Callable[..., dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collision: bool,
    revocation: str,
) -> None:
    """A carried-over slash-free command cannot outlive its app's admission."""
    scopes = {"store": {"playwright-mcp": _URL}} if collision else {}
    alias = "playwright-mcp-2" if collision else "playwright-mcp"
    initial = alias_rebuild(**scopes)
    assert initial["mcpServers"][alias]["command"] == "python3"
    assert f"@{alias}" in initial["tools"]
    refs = [f"@{alias}", f"@{alias}/read"]
    initial["tools"].extend(refs)
    initial["allowedTools"].extend(refs)
    path = tmp_path / "kiro_agents" / "kirocrew.json"
    path.write_text(json.dumps(initial), encoding="utf-8")
    if revocation == "fleet":
        monkeypatch.setattr(
            "kiro_crew.apps.admission.app_admission_denied", lambda *a, **k: "denied"
        )
    elif revocation == "trust":
        monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: False)
    elif revocation == "disabled":
        monkeypatch.setattr(manager, "is_app_enabled", lambda n: False)
    else:

        def unreadable(name: str) -> bool:
            raise ValueError("unreadable app state")

        monkeypatch.setattr(manager, "is_app_enabled", unreadable)

    rebuilt = alias_rebuild(**scopes)
    assert alias not in rebuilt["mcpServers"]
    for field in ("tools", "allowedTools"):
        assert not set(refs).intersection(rebuilt[field])
    if collision:
        assert rebuilt["mcpServers"]["playwright-mcp"]["url"] == _URL["url"]


@pytest.mark.parametrize("collision", [False, True])
def test_nonclean_rebuild_keeps_admitted_app_alias(
    alias_rebuild: Callable[..., dict], collision: bool
) -> None:
    scopes = {"store": {"playwright-mcp": _URL}} if collision else {}
    alias = "playwright-mcp-2" if collision else "playwright-mcp"
    initial = alias_rebuild(**scopes)
    rebuilt = alias_rebuild(**scopes)
    assert rebuilt["mcpServers"][alias] == initial["mcpServers"][alias]
    assert f"@{alias}" in rebuilt["tools"]


@pytest.mark.parametrize("scope", ["provider", "global", "store"])
@pytest.mark.parametrize("alias", ["playwright-mcp", "playwright-mcp-2"])
def test_nonclean_rebuild_keeps_user_declared_app_alias(
    alias_rebuild: Callable[..., dict],
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    alias: str,
) -> None:
    scopes = {scope: {alias: _URL}}
    alias_rebuild(**scopes)
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: False)
    rebuilt = alias_rebuild(**scopes)
    assert rebuilt["mcpServers"][alias]["url"] == _URL["url"]
    assert f"@{alias}" in rebuilt["tools"]


@pytest.mark.parametrize("denied_apps", [{"first"}, {"second"}, {"first", "second"}])
def test_a_shared_alias_follows_the_apps_that_still_emit_it(
    alias_rebuild: Callable[..., dict],
    monkeypatch: pytest.MonkeyPatch,
    denied_apps: set[str],
) -> None:
    """Two apps declaring the same launch share one alias; it stays while an admitted
    app still emits it, recorded as that app's, and goes with the last of them."""
    from kiro_crew.mcp_provenance import APP_KEY

    _rebuild_sees(
        monkeypatch,
        ["first", "second"],
        manifests={n: _manifest({"@playwright/mcp": _STDIO}) for n in ("first", "second")},
    )
    assert "playwright-mcp" in alias_rebuild()["mcpServers"]
    monkeypatch.setattr(
        "kiro_crew.apps.admission.app_admission_denied",
        lambda name, *a, **k: "denied" if name in denied_apps else None,
    )
    rebuilt = alias_rebuild()
    admitted = {"first", "second"} - denied_apps
    if admitted:
        assert rebuilt["mcpServers"]["playwright-mcp"][APP_KEY] in admitted
        assert "@playwright-mcp" in rebuilt["tools"]
    else:
        assert "playwright-mcp" not in rebuilt["mcpServers"]
        assert "@playwright-mcp" not in rebuilt["tools"]


@pytest.mark.parametrize("admitted", [False, True])
@pytest.mark.parametrize("alias", ["playwright-mcp", "playwright-mcp-2"])
def test_locked_merge_admits_on_disk_only_alias(
    alias_rebuild: Callable[..., dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    admitted: bool,
    alias: str,
) -> None:
    from kiro_crew.agent_materialization import default_spec_commit, mcp_sources

    alias_rebuild()
    path = tmp_path / "kiro_agents" / "kirocrew.json"
    path.write_text(json.dumps({"mcpServers": {alias: _STDIO}}), encoding="utf-8")
    monkeypatch.setattr(manager, "is_app_enabled", lambda n: admitted)
    config: dict[str, Any] = {"mcpServers": {}, "tools": [f"@{alias}"]}
    default_spec_commit.write_default_spec(
        path,
        config,
        clean=False,
        gated_off=frozenset(),
        sources=mcp_sources.McpSources({}, {}, {}, set()),
        resolved=mcp_sources.ResolvedServers(set(), {}, {}),
        app_owned_at_start={},
    )
    written = json.loads(path.read_text(encoding="utf-8"))
    assert (alias in written["mcpServers"]) is admitted
    assert (f"@{alias}" in written["tools"]) is admitted


def test_alias_owner_census_keeps_disabled_and_unreadable_enablement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.agent_materialization import mcp_sources

    _rebuild_sees(
        monkeypatch,
        ["first", "second", "missing"],
        manifests={
            n: _manifest({"@playwright/mcp": _STDIO, "plain": _STDIO}) for n in ("first", "second")
        },
    )
    monkeypatch.setattr(manager, "is_app_enabled", lambda n: False)
    monkeypatch.setattr(manager, "app_enabled_state", lambda n: None)
    assert mcp_sources._app_mcp_alias_owners() == {"playwright-mcp": frozenset({"first", "second"})}

    def unreadable() -> list:
        raise ValueError("unreadable app registry")

    monkeypatch.setattr(manager, "list_apps", unreadable)
    assert mcp_sources._app_mcp_alias_owners() == {}


def _python3_entries(config: dict) -> list[str]:
    return [
        k
        for k, v in config["mcpServers"].items()
        if isinstance(v, dict) and v.get("command") == "python3"
    ]


@pytest.mark.parametrize("scope", ["global", "store"])
def test_a_refused_app_alias_does_not_displace_the_users_slashed_server(
    alias_rebuild: Callable[..., dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    """The app's alias took the base name first and the user's ``@playwright/mcp``
    landed on ``-2``. Refusing the app must drop the app's command, not the user's
    server, whatever the names say."""
    scopes = {scope: {"@playwright/mcp": _URL}}
    initial = alias_rebuild(**scopes)
    assert initial["mcpServers"]["playwright-mcp"]["command"] == "python3"
    assert initial["mcpServers"]["playwright-mcp-2"]["url"] == _URL["url"]
    path = tmp_path / "kiro_agents" / "kirocrew.json"
    path.write_text(json.dumps(initial), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: False)

    rebuilt = alias_rebuild(**scopes)
    assert _python3_entries(rebuilt) == []
    users = [k for k, v in rebuilt["mcpServers"].items() if v.get("url") == _URL["url"]]
    assert users, "the user's own server was deleted"
    assert all(f"@{k}" in rebuilt["tools"] for k in users)
    assert "@playwright-mcp" not in rebuilt["tools"] or "playwright-mcp" in users


@pytest.mark.parametrize("scope", ["global", "store"])
def test_a_scope_declaring_a_carried_app_alias_replaces_the_app_entry(
    alias_rebuild: Callable[..., dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    """A user naming the exact key a refused app's carried-over entry holds gets
    their own declaration, not the app's command under their name."""
    initial = alias_rebuild()
    assert initial["mcpServers"]["playwright-mcp"]["command"] == "python3"
    path = tmp_path / "kiro_agents" / "kirocrew.json"
    path.write_text(json.dumps(initial), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: False)

    rebuilt = alias_rebuild(**{scope: {"playwright-mcp": _URL}})
    assert _python3_entries(rebuilt) == []
    assert rebuilt["mcpServers"]["playwright-mcp"]["url"] == _URL["url"]
    assert "@playwright-mcp" in rebuilt["tools"]


@pytest.mark.parametrize(
    "scope", ["global", "store", "store-after-global-modifier", "provider-after-global-modifier"]
)
@pytest.mark.parametrize("declaration", [{"timeout": 30}, {}, _URL, {"command": "user-mcp"}])
@pytest.mark.parametrize("claim", ["census", "start"])
def test_a_legacy_alias_scope_must_supply_its_own_transport(
    alias_rebuild: Callable[..., dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    declaration: dict,
    claim: str,
) -> None:
    """A modifier cannot vouch for an unmarked app launch; a transport replaces it."""
    from kiro_crew import agent
    from kiro_crew.agent_materialization import mcp_sources
    from kiro_crew.mcp_provenance import APP_KEY

    initial = alias_rebuild()
    entry = initial["mcpServers"]["playwright-mcp"]
    assert entry.pop(APP_KEY) == "probe"
    assert entry == _STDIO
    entry["env"] = {"APP_ONLY": "stale"}
    path = tmp_path / "kiro_agents" / "kirocrew.json"
    path.write_text(json.dumps(initial), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: False)
    if claim == "start":
        collect = mcp_sources._collect_app_mcp_servers

        def lose_manifest_after_start() -> dict:
            monkeypatch.setattr(manager, "get_app_manifest", lambda n: None)
            return collect()

        monkeypatch.setattr(agent, "_collect_app_mcp_servers", lose_manifest_after_start)

    after_modifier = scope.endswith("-after-global-modifier")
    scopes = (
        {
            "global": {"playwright-mcp": {"timeout": 30}},
            scope.split("-", 1)[0]: {"playwright-mcp": declaration},
        }
        if after_modifier
        else {scope: {"playwright-mcp": declaration}}
    )
    rebuilt = alias_rebuild(**scopes)
    if "command" in declaration or "url" in declaration:
        entry = rebuilt["mcpServers"]["playwright-mcp"]
        expected = {"timeout": 30, **declaration} if after_modifier else declaration
        assert entry == expected
        assert "@playwright-mcp" in rebuilt["tools"]
    else:
        assert "playwright-mcp" not in rebuilt["mcpServers"]
        assert "@playwright-mcp" not in rebuilt["tools"]


@pytest.mark.parametrize("name", ["playwright-mcp", "playwright-mcp-2"])
def test_a_card_added_server_named_like_a_disabled_apps_alias_is_kept(
    alias_rebuild: Callable[..., dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    from kiro_crew import template_mcp_additions as tma

    home = tmp_path / "kirocrew_home"
    monkeypatch.setattr(tma, "config_dir", lambda: home)
    tma.add_addition(
        "kirocrew", name, dict(_URL), approved_by="owner", card_id="card-1", mount_only=True
    )
    monkeypatch.setattr(manager, "is_app_enabled", lambda n: False)
    first = alias_rebuild()
    assert first["mcpServers"][name]["url"] == _URL["url"]
    path = tmp_path / "kiro_agents" / "kirocrew.json"
    path.write_text(json.dumps(first), encoding="utf-8")
    rebuilt = alias_rebuild()
    assert rebuilt["mcpServers"][name]["url"] == _URL["url"]
    assert f"@{name}" in rebuilt["tools"]


@pytest.mark.parametrize("scope", [None, "global", "store"])
@pytest.mark.parametrize("change", ["manifest-gone", "server-dropped-and-refused"])
def test_a_marked_alias_is_gated_after_its_manifest_stops_claiming_it(
    alias_rebuild: Callable[..., dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    scope: str | None,
) -> None:
    initial = alias_rebuild()
    assert initial["mcpServers"]["playwright-mcp"]["command"] == "python3"
    path = tmp_path / "kiro_agents" / "kirocrew.json"
    path.write_text(json.dumps(initial), encoding="utf-8")
    if change == "manifest-gone":
        monkeypatch.setattr(manager, "get_app_manifest", lambda n: None)
    else:
        monkeypatch.setattr(manager, "get_app_manifest", lambda n: _manifest({"other": _URL}))
        monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: False)

    scopes = {scope: {"playwright-mcp": {"timeout": 30}}} if scope else {}
    rebuilt = alias_rebuild(**scopes)
    assert "playwright-mcp" not in rebuilt["mcpServers"]
    assert "@playwright-mcp" not in rebuilt["tools"]


def _locked_write(path: Path, config: dict, **kwargs: Any) -> dict:
    from kiro_crew.agent_materialization import default_spec_commit, mcp_sources

    kwargs.setdefault("app_owned_at_start", {})
    default_spec_commit.write_default_spec(
        path,
        config,
        clean=False,
        gated_off=frozenset(),
        sources=mcp_sources.McpSources({}, {}, {}, set()),
        resolved=mcp_sources.ResolvedServers(set(), {}, {}),
        **kwargs,
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_legacy_alias_claimed_only_at_rebuild_start_is_dropped(
    alias_rebuild: Callable[..., dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unmarked, and the manifest does not read: the start-of-rebuild claim still
    names it app-owned, and with no readable owner it cannot be admitted."""
    alias_rebuild()
    path = tmp_path / "kiro_agents" / "kirocrew.json"
    path.write_text(json.dumps({"mcpServers": {"playwright-mcp": _STDIO}}), encoding="utf-8")
    monkeypatch.setattr(manager, "get_app_manifest", lambda n: None)
    written = _locked_write(
        path,
        {"mcpServers": {"playwright-mcp": dict(_STDIO)}, "tools": ["@playwright-mcp"]},
        app_owned_at_start={"playwright-mcp": True},
    )
    assert "playwright-mcp" not in written["mcpServers"]
    assert "@playwright-mcp" not in written["tools"]


def test_a_marker_in_a_user_scope_is_never_honoured(
    alias_rebuild: Callable[..., dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scopes = {"global": {"mine": {**_URL, "x-kirocrew-app": "probe"}}}
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: False)
    rebuilt = alias_rebuild(**scopes)
    assert rebuilt["mcpServers"]["mine"] == _URL
    path = tmp_path / "kiro_agents" / "kirocrew.json"
    path.write_text(json.dumps(rebuilt), encoding="utf-8")
    assert alias_rebuild(**scopes)["mcpServers"]["mine"] == _URL


def test_an_admitted_alias_carries_its_app_and_the_marker_stays_out_of_the_session(
    alias_rebuild: Callable[..., dict],
) -> None:
    from kiro_crew.acp import session_mcp
    from kiro_crew.mcp_provenance import APP_KEY

    rebuilt = alias_rebuild()
    entry = rebuilt["mcpServers"]["playwright-mcp"]
    assert entry[APP_KEY] == "probe"
    assert {k: v for k, v in entry.items() if k != APP_KEY} == _STDIO
    element = session_mcp.acp_server_element("playwright-mcp", entry)
    assert element is not None and APP_KEY not in element
    assert APP_KEY in session_mcp._NATIVE_INERT_KEYS
    assert alias_rebuild()["mcpServers"]["playwright-mcp"] == entry


def test_a_url_only_manifest_does_not_admit_a_stale_stdio_live_entry(
    signed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import agent

    metadata = _signed_external_app(tmp_path, _URL)
    (metadata / "data").mkdir(exist_ok=True)
    (metadata / "data" / "srv.py").write_text("print('unsigned')\n", encoding="utf-8")
    _admit_execution(monkeypatch)
    _rebuild_sees(monkeypatch, ["probe"], live={"probe:tools": dict(_STDIO)})
    assert agent._collect_app_mcp_servers() == {}


def test_a_url_only_manifest_still_admits_its_live_url(
    signed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import agent

    _signed_external_app(tmp_path, _URL)
    _admit_execution(monkeypatch)
    live = {"url": "http://127.0.0.1:43210/mcp"}
    _rebuild_sees(monkeypatch, ["probe"], live={"probe:tools": dict(live)})
    assert agent._collect_app_mcp_servers() == {"probe:tools": live}


@pytest.mark.parametrize("entry, declarative", [(_URL, True), (_STDIO, False)])
def test_the_final_merge_uses_the_url_shortcut_only_for_url_entries(
    monkeypatch: pytest.MonkeyPatch, entry: dict, declarative: bool
) -> None:
    from kiro_crew.agent_materialization import default_spec_commit
    from kiro_crew.apps import bridges

    seen: list[bool] = []

    def _record(name: str, **kwargs: Any) -> None:
        seen.append(kwargs["declarative"])

    monkeypatch.setattr(bridges, "_registration_denied", _record)
    monkeypatch.setattr(bridges, "_app_resource_root", lambda n: Path("/nonexistent"))
    assert default_spec_commit._app_mcp_key_admitted(
        "probe:tools",
        {},
        lambda n: True,
        lambda n: _manifest({"tools": _URL}),
        {"probe": [dict(entry)]},
    )
    assert seen == [declarative]


@pytest.mark.parametrize("scope", ["provider", "global", "store"])
def test_an_unmarked_user_url_named_like_an_app_alias_keeps_its_headers(
    alias_rebuild: Callable[..., dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    """Only a carried launch is replaced: a user URL entry that merely shares an app
    alias name keeps headers whose only copy is the rendered spec."""
    from kiro_crew.mcp_provenance import APP_KEY

    user_url = {"url": "https://user.invalid/mcp"}
    scopes = {scope: {"playwright-mcp": user_url}}
    initial = alias_rebuild(**scopes)
    entry = initial["mcpServers"]["playwright-mcp"]
    assert entry["url"] == user_url["url"]
    assert APP_KEY not in entry
    entry["headers"] = {"Authorization": "Bearer placeholder"}
    path = tmp_path / "kiro_agents" / "kirocrew.json"
    path.write_text(json.dumps(initial), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: False)

    rebuilt = alias_rebuild(**scopes)
    kept = rebuilt["mcpServers"]["playwright-mcp"]
    assert kept["url"] == user_url["url"]
    assert kept["headers"] == {"Authorization": "Bearer placeholder"}
    assert "command" not in kept
    assert "@playwright-mcp" in rebuilt["tools"]


@pytest.fixture
def signed_stdio(
    signed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    """A signed, execution-trusted installed app with no live MCP registration."""
    from kiro_crew.apps import bridges

    root = signed_home / "apps" / "probe"
    write_app(
        root,
        resources="app",
        mcpServers={"tools": {"command": "python3", "args": ["-E", "server.py"]}},
    )
    (root / "server.py").write_text("import cache_helper\n", encoding="utf-8")
    (root / "cache_helper.py").write_text("VALUE = 'publisher'\n", encoding="utf-8")
    manifest = sign(root)
    _admit_execution(monkeypatch)
    registered_reader = bridges.registered_app_mcp_servers
    _rebuild_sees(monkeypatch, ["probe"], manifests={"probe": manifest})
    monkeypatch.setattr(bridges, "registered_app_mcp_servers", registered_reader)
    monkeypatch.setattr(manager, "app_enabled_state", lambda n: True)
    path = tmp_path / "agent.json"
    monkeypatch.setattr(bridges, "_mcp_json_path", lambda: path)
    monkeypatch.setattr(bridges, "_schedule_unresolvable_warning", lambda *a: None)
    return signed_home, root, path


def _assert_gateway_cache(entry: dict, home: Path) -> Path:
    prefix = Path(entry["env"]["PYTHONPYCACHEPREFIX"])
    assert prefix.parent == home / "run" / "app-pycache" / "probe"
    assert prefix.is_dir()
    args = entry["args"]
    assert args[-3:-1] == ["-X", f"pycache_prefix={prefix}"]
    assert "-E" in args
    return prefix


def test_manifest_fallback_is_omitted_without_allocating_a_cache(
    signed_stdio: tuple[Path, Path, Path],
) -> None:
    from kiro_crew import agent

    home, _, path = signed_stdio
    base = home / "run" / "app-pycache"
    before = set(base.rglob("*"))
    for _ in range(2):
        assert agent._collect_app_mcp_servers() == {}
        assert set(base.rglob("*")) == before
    written = _locked_write(path, {"mcpServers": {}, "tools": ["@probe:tools"]})
    assert "probe:tools" not in written["mcpServers"]
    assert "@probe:tools" not in written["tools"]
    assert set(base.rglob("*")) == before


@pytest.mark.parametrize("resources", ["app", "gateway"])
def test_an_omitted_signed_python_server_is_named_once_with_its_reason(
    signed_stdio: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    resources: str,
) -> None:
    from kiro_crew import agent
    from kiro_crew.agent_materialization import mcp_sources

    monkeypatch.setattr(mcp_sources, "_UNPINNED_WARNED", set())
    monkeypatch.setattr(manager, "list_apps", lambda: [{"name": "probe", "resources": resources}])

    def named() -> list[str]:
        return [r.getMessage() for r in caplog.records if "probe:tools" in r.getMessage()]

    with caplog.at_level("WARNING"):
        # A preview read names nothing: only the rebuild that writes the spec does.
        assert agent._collect_app_mcp_servers(audit=False) == {}
        assert named() == []
        assert mcp_sources._UNPINNED_WARNED == set()
        for _ in range(2):
            assert agent._collect_app_mcp_servers() == {}
    lines = named()
    assert len(lines) == 1
    assert "requires signed apps" in lines[0]
    assert ("resources: app" in lines[0]) is (resources == "app")


def _registered_signed_entry(path: Path) -> dict:
    from kiro_crew.apps import bridges, bridges_mcp

    assert bridges._register_mcp_servers("probe", manager.get_app_manifest("probe")) == [
        "probe:tools"
    ]
    entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["probe:tools"]
    assert bridges_mcp.registered_app_mcp_servers()["probe:tools"] == entry
    return entry


def test_registration_output_passes_pure_pin_validation(
    signed_stdio: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps.signed_mcp import signed_launch_pinned

    home, root, path = signed_stdio
    entry = _registered_signed_entry(path)
    _assert_gateway_cache(entry, home)
    original = json.loads(json.dumps(entry))

    def no_mkdir(*args: Any, **kwargs: Any) -> None:
        pytest.fail("pin validation must not create directories")

    monkeypatch.setattr(Path, "mkdir", no_mkdir)
    assert signed_launch_pinned("probe", entry, resource_root=root)
    assert entry == original


_PIN_CASES = [
    "registered",
    "no-pin",
    "mismatch",
    "outside",
    "leaf-sibling-link",
    "leaf-app-link",
    "base-link",
    "run-link",
    "cache-root-link",
    "missing",
    "app-tree",
    "extra-option",
    "glued-option",
    "script-argument",
    "malformed-args",
    "relative",
    "traversal",
]
# An absent leaf holds no bytecode: a later allocation for the same app removes
# earlier leaves, so a registered sibling's pin legitimately names none.
_PIN_ACCEPTED = {"registered", "missing"}


def _carried_pin(case: str, entry: dict, home: Path, root: Path) -> dict:
    from kiro_crew.testing.links import make_dir_link

    prefix = Path(entry["env"]["PYTHONPYCACHEPREFIX"])
    if case == "no-pin":
        return {"command": entry["command"], "args": ["-E", str(root / "server.py")]}
    if case == "mismatch":
        entry["env"]["PYTHONPYCACHEPREFIX"] = str(prefix.parent / "different")
    elif case in {"outside", "app-tree"}:
        prefix = (home if case == "outside" else root / "__pycache__") / "cache"
        prefix.mkdir(parents=True)
    elif case in {"leaf-sibling-link", "leaf-app-link", "base-link", "run-link", "cache-root-link"}:
        link = {
            "base-link": prefix.parent,
            "run-link": home / "run",
            "cache-root-link": home / "run" / "app-pycache",
        }.get(case, prefix)
        target = (
            root / "__pycache__" if case == "leaf-app-link" else link.with_name(link.name + "-real")
        )
        link.rename(target)
        make_dir_link(link, target)
    elif case == "missing":
        prefix.rmdir()
    elif case == "extra-option":
        entry["args"][0:0] = ["-X", f"pycache_prefix={prefix}"]
    elif case == "glued-option":
        entry["args"][1:3] = [f"-Xpycache_prefix={prefix}"]
    elif case == "script-argument":
        entry["args"] = ["-E", str(root / "server.py"), "-X", f"pycache_prefix={prefix}"]
    elif case == "malformed-args":
        entry["args"][0] = None
    elif case == "relative":
        prefix = Path("run/app-pycache/probe/cache")
    elif case == "traversal":
        prefix = prefix.parent / ".." / "probe" / prefix.name
    if case in {"outside", "app-tree", "relative", "traversal"}:
        entry["env"]["PYTHONPYCACHEPREFIX"] = str(prefix)
        entry["args"][2] = f"pycache_prefix={prefix}"
    return entry


@pytest.mark.parametrize("case", _PIN_CASES)
def test_collector_and_private_member_filter_the_same_carried_pin(
    signed_stdio: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from kiro_crew import agent, agent_capabilities
    from kiro_crew.apps import bridges, bridges_mcp

    home, root, path = signed_stdio
    entry = _carried_pin(case, _registered_signed_entry(path), home, root)
    path.write_text(json.dumps({"mcpServers": {"probe:tools": entry}}), encoding="utf-8")
    # Use the real disk reader: the registered map is not a separate authority.
    monkeypatch.setattr(
        bridges, "registered_app_mcp_servers", bridges_mcp.registered_app_mcp_servers
    )
    base = home / "run" / "app-pycache"
    before = set(base.rglob("*"))
    expected = {"probe:tools": entry} if case in _PIN_ACCEPTED else {}
    for _ in range(2):
        assert agent._collect_app_mcp_servers(audit=False) == expected
        spec = {"mcpServers": {"probe:tools": entry}}
        agent_capabilities._maintain_owned({"parent": {}, "parent_spec": {}}, spec)
        assert spec["mcpServers"] == expected
        assert set(base.rglob("*")) == before


@pytest.mark.parametrize("source", ["carried", "disk"])
@pytest.mark.parametrize("ownership", ["colon", "marker", "census"])
@pytest.mark.parametrize("case", _PIN_CASES)
def test_locked_merge_validates_carried_signed_python(
    signed_stdio: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    ownership: str,
    case: str,
) -> None:
    home, root, path = signed_stdio
    entry = _carried_pin(case, _registered_signed_entry(path), home, root)
    key = "probe:tools" if ownership == "colon" else "playwright-mcp"
    if ownership == "marker":
        entry["x-kirocrew-app"] = "probe"
    if ownership == "census":
        write_app(
            root,
            resources="app",
            mcpServers={"@playwright/mcp": {"command": "python3", "args": ["-E", "server.py"]}},
        )
        manifest = sign(root)
        monkeypatch.setattr(manager, "get_app_manifest", lambda n: manifest)
    config = {"mcpServers": {}, "tools": [f"@{key}"], "allowedTools": [f"@{key}/read"]}
    path.write_text(
        json.dumps({"mcpServers": {key: entry} if source == "disk" else {}}), encoding="utf-8"
    )
    if source == "carried":
        config["mcpServers"][key] = entry
    base = home / "run" / "app-pycache"
    before = set(base.rglob("*"))
    written = _locked_write(path, config)
    assert (key in written["mcpServers"]) is (case in _PIN_ACCEPTED)
    assert (f"@{key}" in written["tools"]) is (case in _PIN_ACCEPTED)
    if case in _PIN_ACCEPTED:
        assert written["mcpServers"][key] == entry
    else:
        assert f"@{key}/read" not in written["allowedTools"]
    assert set(base.rglob("*")) == before


@pytest.mark.parametrize("command", ["python3", "node"])
def test_no_policy_stdio_rebuild_keeps_its_launch_contract(
    signed_stdio: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    from kiro_crew import agent

    home, root, path = signed_stdio
    (home / "app_admission.json").unlink()
    entry = {"command": command, "args": ["-E", "server.py"], "env": {"KEEP": "value"}}
    manifest = write_app(root, resources="app", mcpServers={"tools": entry})
    monkeypatch.setattr(manager, "get_app_manifest", lambda n: manifest)
    assert agent._collect_app_mcp_servers() == {"probe:tools": entry}
    path.write_text(json.dumps({"mcpServers": {"probe:tools": entry}}), encoding="utf-8")
    written = _locked_write(path, {"mcpServers": {}, "tools": ["@probe:tools"]})
    assert written["mcpServers"]["probe:tools"] == entry


def test_signed_non_python_fallback_keeps_argv_and_secret_reference(
    signed_stdio: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import agent

    _, root, path = signed_stdio
    entry = {
        "command": "node",
        "args": ["server.js", "-E"],
        "env": {"TOKEN": "secret://app-token"},
    }
    write_app(root, resources="app", mcpServers={"tools": entry})
    manifest = sign(root)
    monkeypatch.setattr(manager, "get_app_manifest", lambda n: manifest)
    assert agent._collect_app_mcp_servers() == {"probe:tools": entry}
    path.write_text(json.dumps({"mcpServers": {"probe:tools": entry}}), encoding="utf-8")
    assert (
        _locked_write(path, {"mcpServers": {}, "tools": []})["mcpServers"]["probe:tools"] == entry
    )


@pytest.mark.parametrize("root_kind", ["installed", "resource"])
def test_pin_validator_refuses_cache_nested_in_either_app_tree(
    signed_stdio: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, root_kind: str
) -> None:
    from kiro_crew.apps.signed_mcp import signed_launch_pinned
    from kiro_crew.testing.links import make_dir_link

    home, root, path = signed_stdio
    entry = _registered_signed_entry(path)
    if root_kind == "installed":
        moved = root.with_name("moved-app")
        root.rename(moved)
        make_dir_link(root, home / "run")
        resource = moved
    else:
        resource = home / "run"
    assert not signed_launch_pinned("probe", entry, resource_root=resource)


@pytest.mark.parametrize(
    "command",
    ["python", "python3.12", "py", "python.exe", "pythonw", "python3.13t", "pypy3", "pypy3.10"],
)
@pytest.mark.parametrize("target", [["script.py"], ["-m", "module"], ["-c", "pass"]])
def test_pin_validator_shares_registration_python_options(
    signed_stdio: tuple[Path, Path, Path], command: str, target: list[str]
) -> None:
    from kiro_crew.apps.signed_mcp import signed_launch_pinned, with_python_cache_prefix

    _, root, path = signed_stdio
    entry = _registered_signed_entry(path)
    prefix = entry["env"]["PYTHONPYCACHEPREFIX"]
    entry["command"] = command
    entry["args"] = ["-IE", *target, "-X", "pycache_prefix=script-argument"]
    entry = with_python_cache_prefix(entry, prefix)
    assert signed_launch_pinned("probe", entry, resource_root=root)


@pytest.mark.parametrize("case", _PIN_CASES)
def test_pin_validator_checks_every_carried_selection(
    signed_stdio: tuple[Path, Path, Path], case: str
) -> None:
    from kiro_crew.apps.signed_mcp import signed_launch_pinned

    home, root, path = signed_stdio
    entry = _carried_pin(case, _registered_signed_entry(path), home, root)
    assert signed_launch_pinned("probe", entry, resource_root=root) is (case in _PIN_ACCEPTED)


def test_every_registered_signed_python_server_survives_collection(
    signed_stdio: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registration allocates one cache per server and each allocation removes the
    app's earlier leaves; every registered server must still be collected, also
    after a later allocation (backend start, lifecycle hook) for the same app."""
    from kiro_crew import agent
    from kiro_crew.apps import bridges
    from kiro_crew.apps.signed_imports import pycache_prefix_env

    home, root, path = signed_stdio
    servers = {name: {"command": "python3", "args": ["-E", "server.py"]} for name in ("one", "two")}
    write_app(root, resources="app", mcpServers=servers)
    manifest = sign(root)
    monkeypatch.setattr(manager, "get_app_manifest", lambda n: manifest)
    registered = bridges._register_mcp_servers("probe", manifest)
    assert sorted(registered) == ["probe:one", "probe:two"]
    on_disk = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]

    collected = agent._collect_app_mcp_servers(audit=False)
    assert sorted(collected) == ["probe:one", "probe:two"]
    pycache_prefix_env("probe", root)
    collected = agent._collect_app_mcp_servers(audit=False)
    assert sorted(collected) == ["probe:one", "probe:two"]
    for ref in ("probe:one", "probe:two"):
        assert collected[ref]["args"] == on_disk[ref]["args"]
