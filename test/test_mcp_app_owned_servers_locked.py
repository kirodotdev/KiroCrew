"""An app's MCP server cannot be uninstalled from the MCP Servers list.

An installed app declares its servers in its manifest, and the agent-config
rebuild re-derives every enabled app's servers from that manifest on each run
(``_collect_app_mcp_servers``), as does the boot reconcile. So an uninstall
that purges ``{app}:{server}`` from the config files is written straight back
by the rebuild the apply itself runs, and again at the next restart: the row
goes strikethrough, Apply says ok, and after the restart the server is listed
as before.

Ownership is the ``x-kirocrew-app-source`` stamp both writers of app entries (the
rebuild and the app bridge) put on what they mount, gated by the app being
installed and enabled; a user's own server never carries it.

The row names its app (``app``), the list hides Uninstall on it, and every
removal route refuses it with 409 ``mcp_server_app_owned``: ``/api/mcp/apply``
with ``uninstall``, ``/api/mcp/remove``, ``DELETE /api/mcp/servers/{name}`` and
``/api/capability/mcp/uninstall``. Switching the server off is still accepted:
the store's ``disabled: true`` survives the rebuild, so that is the control that
works; removing the app is what removes its server.

Every filesystem seam is redirected into ``tmp_path`` by the shared ``sandbox``
fixture; nothing touches the real ``~/.kiro``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

import pytest
from app_bridges_helpers import _stub_venv_probe  # noqa: F401
from app_bridges_helpers import app_env_fixture  # noqa: F401
from app_bridges_helpers import _make_app_source
from handlers_mcp_coverage_helpers import (  # noqa: F401
    _payload,
    _read_global,
    _request,
    _write_global,
    agents_dir_fixture,
    sandbox_fixture,
)

import kiro_crew.platform.context as platform_context
from conftest import requires_symlinks
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.agent_materialization import mcp_sources
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.dashboard.handlers import agents as agents_handler
from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.mcp_provenance import SOURCE_KEY

pytestmark = pytest.mark.asyncio

_APP = "chorus"
_SERVER = "chorus:chorus"


@pytest.fixture(name="app_owned")
def app_owned_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """One installed app declares one server; the manifest reader is not under test here.

    ``raising=False`` so that, with the fix reverted, each route test fails on its
    own assertion (a 200 where a 409 is expected) rather than erroring here. Patched
    through the ``mcp`` facade, which mirrors the name onto every module that binds
    it (the routes and the probe), so the undo restores each holder's own value.
    """
    monkeypatch.setattr(mcp_mod, "_app_owned_mcp_servers", lambda: {_SERVER: _APP}, raising=False)


def _assert_app_owned_refusal(resp: Any) -> None:
    payload = _payload(resp)
    assert resp.status == 409, payload
    assert payload.get("code") == "mcp_server_app_owned", payload
    assert payload.get("app") == _APP, payload
    assert _APP in payload.get("error", ""), payload


async def test_apply_refuses_uninstalling_an_app_server(
    sandbox: SimpleNamespace, app_owned: None
) -> None:
    _write_global(sandbox, {_SERVER: {"command": "x"}, "srv": {"command": "y"}})
    # The other change in the batch is not applied either: the refusal is the
    # whole request, before any write.
    other = {"name": "srv", "uninstall": True}
    resp = await mcp_mod.api_mcp_apply(
        _request({"changes": [other, {"name": _SERVER, "uninstall": True}]})
    )
    _assert_app_owned_refusal(resp)
    servers = _read_global(sandbox)
    assert servers[_SERVER] == {"command": "x"}
    assert "srv" in servers


async def test_apply_still_switches_an_app_server_off(
    sandbox: SimpleNamespace, app_owned: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The agent spec rebuild is not under test and must not write the host's.
    monkeypatch.setattr(mcp_mod, "rebuild_agent_config_reporting", lambda: (None, True))
    _write_global(sandbox, {_SERVER: {"command": "x"}})
    change = {"name": _SERVER, "kirocrew": False, "kiroGlobal": True}
    resp = await mcp_mod.api_mcp_apply(_request({"changes": [change]}))
    payload = _payload(resp)
    assert resp.status == 200, payload
    store = json.loads(mcp_mod._kirocrew_mcp_json().read_text(encoding="utf-8"))
    assert store["mcpServers"][_SERVER].get("disabled") is True


async def test_a_switched_off_app_server_still_refuses_removal(
    sandbox: SimpleNamespace, agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real ownership read answers here: only the app enablement reader is
    # patched, and the mounted entry is what the rebuild writes once the store
    # switched the server off (resolved command, ``disabled: true``, the stamp).
    monkeypatch.setattr(mcp_mod, "rebuild_agent_config_reporting", lambda: (None, True))
    _enabled_apps(monkeypatch, {_SERVER})
    entry = {"command": "/usr/bin/cat", "disabled": True, SOURCE_KEY: _SERVER}
    (agents_dir / AGENT_FILENAME).write_text(
        json.dumps({"name": "kirocrew", "mcpServers": {_SERVER: entry}}),
        encoding="utf-8",
    )
    _write_global(sandbox, {_SERVER: {"command": "cat"}})
    change = {"name": _SERVER, "kirocrew": False, "kiroGlobal": True}
    resp = await mcp_mod.api_mcp_apply(_request({"changes": [change]}))
    assert resp.status == 200, _payload(resp)

    resp = await mcp_mod.api_mcp_remove(_request({"name": _SERVER}))

    _assert_app_owned_refusal(resp)
    assert _SERVER in _read_global(sandbox)


def test_a_spec_the_hardened_reader_refuses_owns_nothing(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The agent spec is read through the hardened reader, so a file it refuses
    # (here: over the size cap; the same reader screens sensitive symlinks) is
    # never read as raw bytes and claims no owner.
    from kiro_crew import hooks
    from kiro_crew.dashboard.handlers import mcp_agent_sync

    _enabled_apps(monkeypatch, {_SERVER})
    entry = {"command": "/usr/bin/cat", SOURCE_KEY: _SERVER, "args": ["x" * 400]}
    (agents_dir / AGENT_FILENAME).write_text(
        json.dumps({"name": "kirocrew", "mcpServers": {_SERVER: entry}}),
        encoding="utf-8",
    )
    assert mcp_agent_sync._app_owned_mcp_servers() == {_SERVER: _APP}

    monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 256)
    assert mcp_agent_sync._app_owned_mcp_servers() == {}


@requires_symlinks
def test_a_symlinked_spec_to_a_sensitive_target_is_denied_and_owns_nothing(
    tmp_path: Path, agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``kirocrew.json`` is a symlink to a protected file: the hardened reader
    # refuses the resolved target and writes the SEL denial under this surface's
    # own label; the ownership read sees no spec and claims nothing.
    from types import SimpleNamespace

    from kiro_crew import agent_discovery
    from kiro_crew.dashboard.handlers import mcp_agent_sync

    _enabled_apps(monkeypatch, {_SERVER})
    target = tmp_path / "protected.json"
    target.write_text(
        json.dumps({"name": "kirocrew", "mcpServers": {_SERVER: {SOURCE_KEY: _SERVER}}}),
        encoding="utf-8",
    )
    (agents_dir / AGENT_FILENAME).symlink_to(target)
    monkeypatch.setattr(
        agent_discovery, "is_sensitive_canonical_path", lambda p: str(target) in str(p)
    )
    events: list[dict] = []
    monkeypatch.setattr(
        agent_discovery,
        "_sel",
        lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
    )

    assert mcp_agent_sync._app_owned_mcp_servers() == {}

    assert [(e["operation"], e["source"], e["outcome"]) for e in events] == [
        ("mcp_app_owned_servers", "dashboard", "denied")
    ]


async def test_remove_refuses_an_app_server(sandbox: SimpleNamespace, app_owned: None) -> None:
    _write_global(sandbox, {_SERVER: {"command": "x"}})
    resp = await mcp_mod.api_mcp_remove(_request({"name": _SERVER}))
    _assert_app_owned_refusal(resp)
    assert _SERVER in _read_global(sandbox)
    assert sandbox.synced == []


async def test_remove_by_the_raw_key_of_a_mounted_alias_is_refused(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The removal canonicalizes ``chorus:@scope/pkg`` to ``scope-pkg`` and pops
    # both spellings, so a request by the raw key must meet the same refusal as
    # one by the alias the app's entry is mounted under.
    monkeypatch.setattr(
        mcp_mod, "_app_owned_mcp_servers", lambda: {"scope-pkg": _APP}, raising=False
    )
    _write_global(sandbox, {"chorus:@scope/pkg": {"command": "npx"}})
    resp = await mcp_mod.api_mcp_remove(_request({"name": "chorus:@scope/pkg"}))
    _assert_app_owned_refusal(resp)
    assert "chorus:@scope/pkg" in _read_global(sandbox)


async def test_delete_refuses_an_app_server(sandbox: SimpleNamespace, app_owned: None) -> None:
    _write_global(sandbox, {_SERVER: {"command": "x"}})
    resp = await mcp_mod.api_mcp_server_detail(
        _request(None, match_info={"name": _SERVER}, method="DELETE")
    )
    _assert_app_owned_refusal(resp)
    assert _read_global(sandbox)[_SERVER] == {"command": "x"}
    assert sandbox.synced == []


class _Manager:
    """Records every call, so a refusal is shown to land before the manager."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def available(self) -> bool:
        self.calls.append("available")
        return False


class _CapabilityRequest:
    """A request ``read_bounded_json(max_bytes=None)`` accepts."""

    content_type = "application/json"
    can_read_body = True

    def __init__(self, body: Any) -> None:
        self._body = body
        self.app = {"state": None}

    async def json(self) -> Any:
        return self._body


async def test_capability_uninstall_refuses_an_app_server(
    _floor_monkeypatch: pytest.MonkeyPatch, app_owned: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )
    mgr = _Manager()

    class _Ctx:
        capability_manager = mgr

    monkeypatch.setattr(platform_context, "current_context", lambda: _Ctx())
    monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp._agent_home_not_owned", lambda _n: None)
    resp = await agents_handler.api_capability_mcp_uninstall(
        _CapabilityRequest({"server_id": _SERVER})
    )
    _assert_app_owned_refusal(resp)
    assert mgr.calls == []


async def test_a_third_party_server_with_a_colon_is_not_app_owned(
    sandbox: SimpleNamespace, app_owned: None
) -> None:
    # Ownership is read from the stamp, never from the colon: a global
    # ``npm:foo`` belongs to nobody even when an app called ``npm`` is installed.
    _write_global(sandbox, {"npm:foo": {"command": "y"}})
    resp = await mcp_mod.api_mcp_remove(_request({"name": "npm:foo"}))
    assert resp.status == 200, _payload(resp)
    assert "npm:foo" not in _read_global(sandbox)


async def test_stamped_rows_name_the_owning_app() -> None:
    owned = {_SERVER: _APP}
    app_row: dict = {"name": _SERVER}
    other: dict = {"name": "srv"}
    mcp_mod._stamp_config_state(app_row, {}, {}, frozenset(), owned)
    mcp_mod._stamp_config_state(other, {}, {}, frozenset(), owned)
    assert app_row["app"] == _APP
    assert "app" not in other


def _enabled_apps(monkeypatch: pytest.MonkeyPatch, sources: Iterable[str]) -> None:
    """The enabled apps' declarations ``_app_owned_mcp_server_apps`` gates stamps by.

    Patched where the lookup reads them (``_collect_app_mcp_servers``, the manifest
    reader for enabled apps), as exact ``{app}:{server}`` keys. Records the audit
    flag each call passes so a test can assert the read writes no audit rows.
    """
    audits: list[bool] = []

    def _collect(audit: bool = True) -> dict[str, Any]:
        audits.append(audit)
        return {source: {"command": "cat"} for source in sources}

    monkeypatch.setattr(mcp_sources, "_collect_app_mcp_servers", _collect)
    monkeypatch.setattr(mcp_sources, "_test_audits", audits, raising=False)


def test_a_same_url_global_server_is_told_apart_by_the_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An app's ``team/service`` and the user's global ``team-service`` share the
    # url and differ only in their headers; the rebuild mounts the app's at
    # ``team-service-2``. Only the stamp says which one is the app's.
    _enabled_apps(monkeypatch, {"chorus:team/service"})
    url = "https://mcp.example.com/mcp"
    mounted = {
        "team-service": {"url": url, "headers": {"Authorization": "user"}},
        "team-service-2": {
            "url": url,
            "headers": {"X-App": "1"},
            SOURCE_KEY: "chorus:team/service",
        },
    }

    assert mcp_sources._app_owned_mcp_server_apps(mounted) == {"team-service-2": _APP}


def test_a_stamped_raw_key_answers_for_its_alias_until_the_rebuild_mounts_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Enabling an app writes ``chorus:@scope/pkg`` through the bridge; the list
    # shows that row as ``scope-pkg`` before any rebuild has aliased it.
    _enabled_apps(monkeypatch, {"chorus:@scope/pkg"})
    raw = {"chorus:@scope/pkg": {"command": "/usr/bin/npx", SOURCE_KEY: "chorus:@scope/pkg"}}

    assert mcp_sources._app_owned_mcp_server_apps(raw) == {
        "chorus:@scope/pkg": _APP,
        "scope-pkg": _APP,
    }
    # A user's own entry already holding the alias is not claimed through it.
    assert mcp_sources._app_owned_mcp_server_apps(
        {**raw, "scope-pkg": {"command": "/usr/bin/npx"}}
    ) == {"chorus:@scope/pkg": _APP}


def test_a_switched_off_stamped_server_is_still_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    _enabled_apps(monkeypatch, {_SERVER})
    mounted = {
        _SERVER: {
            "command": "/usr/bin/cat",
            "env": {"PATH": "/long/path"},
            "disabled": True,
            SOURCE_KEY: _SERVER,
        },
        "srv": {"command": "/usr/bin/cat", "env": {"PATH": "/long/path"}},
    }

    assert mcp_sources._app_owned_mcp_server_apps(mounted) == {_SERVER: _APP}


def test_a_stamp_from_a_disabled_or_removed_app_claims_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A disabled app contributes no declaration, so its stamp is a leftover.
    _enabled_apps(monkeypatch, set())

    mounted = {_SERVER: {"command": "cat", SOURCE_KEY: _SERVER}}

    assert mcp_sources._app_owned_mcp_server_apps(mounted) == {}
    # A read, not a rebuild: it writes no withheld-auto-approve audit rows.
    assert mcp_sources._test_audits == [False]


def test_another_apps_switched_off_declaration_does_not_unlock_this_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two installed apps declare ``@scope/pkg``; only ``chorus`` is enabled, so
    # only its declaration is read. The mounted entry is the enabled app's and
    # stays owned; a leftover stamp from the switched-off ``rival`` claims nothing.
    _enabled_apps(monkeypatch, {"chorus:@scope/pkg"})
    mounted = {
        "scope-pkg": {"command": "/usr/bin/npx", SOURCE_KEY: "chorus:@scope/pkg"},
        "scope-pkg-2": {"command": "/usr/bin/npx", SOURCE_KEY: "rival:@scope/pkg"},
    }

    assert mcp_sources._app_owned_mcp_server_apps(mounted) == {"scope-pkg": _APP}


def test_a_malformed_stamp_or_entry_claims_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _enabled_apps(monkeypatch, {_SERVER, "chorus"})
    mounted: dict[str, Any] = {
        "a": {"command": "cat", SOURCE_KEY: ["chorus:chorus"]},
        "b": {"command": "cat", SOURCE_KEY: "chorus"},
        "c": "not-a-dict",
        "d": None,
    }

    assert mcp_sources._app_owned_mcp_server_apps(mounted) == {}


def test_app_bridge_registration_stamps_the_source(
    tmp_path: Path, app_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The boot reconcile (reregister_app_mcp_servers) writes through this function.
    import kiro_crew.apps.bridges as bridges

    mcp_path = tmp_path / "kirocrew.json"
    monkeypatch.setattr(bridges, "_mcp_json_path", lambda: mcp_path)
    src = _make_app_source(
        tmp_path,
        mcpServers={"tool": {"command": "cat", "args": ["x"]}, "@scope/pkg": {"command": "cat"}},
    )
    install_app(src)
    manifest = AppManifest.from_json_file(
        app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
    )

    bridges._register_mcp_servers("test-app", manifest)

    written = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]
    assert written["test-app:tool"][SOURCE_KEY] == "test-app:tool"
    assert written["test-app:@scope/pkg"][SOURCE_KEY] == "test-app:@scope/pkg"

    # With no rebuild in between, the list shows the scoped server as
    # ``scope-pkg`` and the lock already answers for that name.
    _enabled_apps(monkeypatch, {"test-app:tool", "test-app:@scope/pkg"})
    owned = mcp_sources._app_owned_mcp_server_apps(written)
    assert owned["test-app:tool"] == "test-app"
    assert owned["test-app:@scope/pkg"] == "test-app"
    assert owned["scope-pkg"] == "test-app"


async def test_app_owned_mcp_keys_are_read_from_manifests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = SimpleNamespace(
        mcpServers={"chorus": {"command": "x"}, "@scope/pkg": {"command": "y"}}
    )
    rival = SimpleNamespace(mcpServers={"@scope/pkg": {"command": "z"}})
    monkeypatch.setattr(
        "kiro_crew.apps.manager.list_apps",
        lambda: [
            {"name": _APP},
            {"name": "rival"},
            {"name": "murky"},
            {"name": "broken"},
            {"noname": True},
        ],
        raising=False,
    )
    states = {_APP: True, "rival": False, "murky": None, "broken": True}
    monkeypatch.setattr(
        "kiro_crew.apps.manager.app_enabled_state", lambda name: states[name], raising=False
    )

    def _manifest(name: str) -> Any:
        if name == "broken":
            raise RuntimeError("unreadable manifest")
        if name == "rival":
            return rival
        if name == "murky":
            return SimpleNamespace(mcpServers={"murk": {"command": "m"}})
        return manifest

    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", _manifest, raising=False)
    assert mcp_sources._app_owned_mcp_keys() == {_SERVER: True, "scope-pkg": False}


def test_an_app_server_comes_back_after_a_purge_and_a_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report's mechanism: the rebuild re-derives the entry, so a purge cannot stick."""
    from kiro_crew.agent import rebuild_agent_config

    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "defaults.json").write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")
    (agent_dir / "prompt.md").write_text("prompt", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    kiro_dir = tmp_path / ".kiro" / "agents"
    kiro_dir.mkdir(parents=True)
    settings = tmp_path / ".kiro" / "settings" / "mcp.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    store_dir = tmp_path / "crew"
    store_dir.mkdir()
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", settings)
    monkeypatch.setattr("kiro_crew.agent._CC_MCP_JSON", tmp_path / "nonexistent_cc.json")
    monkeypatch.setattr("kiro_crew.agent._user_dir", lambda: store_dir)
    monkeypatch.setattr("kiro_crew.agent._KIROCREW_BIN", "/usr/bin/kirocrew")
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: "/usr/bin/cat")
    manifest = SimpleNamespace(
        mcpServers={
            "chorus": {"command": "cat", "args": ["chorus"]},
            "@scope/pkg": {"command": "cat", "args": ["pkg"]},
        },
        backend=SimpleNamespace(entryPoint=""),
    )
    monkeypatch.setattr("kiro_crew.apps.manager.list_apps", lambda: [{"name": _APP}], raising=False)
    monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda n: True, raising=False)
    monkeypatch.setattr("kiro_crew.apps.manager.app_enabled_state", lambda n: True, raising=False)
    monkeypatch.setattr(
        "kiro_crew.apps.manager.get_app_manifest", lambda n: manifest, raising=False
    )
    monkeypatch.setattr(
        "kiro_crew.apps.bridges.registered_app_mcp_servers", lambda: {}, raising=False
    )
    spec = kiro_dir / "kirocrew.json"

    def _mounted() -> dict[str, Any]:
        servers: dict[str, Any] = json.loads(spec.read_text(encoding="utf-8"))["mcpServers"]
        # The stamp rides every rebuild, onto the alias a slash-named server mounts as.
        assert servers[_SERVER][SOURCE_KEY] == _SERVER
        assert servers["scope-pkg"][SOURCE_KEY] == "chorus:@scope/pkg"
        return servers

    rebuild_agent_config()
    assert _SERVER in _mounted()

    data = json.loads(spec.read_text(encoding="utf-8"))
    del data["mcpServers"][_SERVER]
    spec.write_text(json.dumps(data), encoding="utf-8")
    rebuild_agent_config()
    assert _SERVER in _mounted()

    (store_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": {_SERVER: {"disabled": True}}}), encoding="utf-8"
    )
    rebuild_agent_config()
    mounted = _mounted()
    assert mounted[_SERVER].get("disabled") is True
    assert mounted[_SERVER]["command"] == "/usr/bin/cat"
    assert mcp_sources._app_owned_mcp_server_apps(mounted) == {
        _SERVER: _APP,
        "scope-pkg": _APP,
    }

    # The user then declares their own ``scope-pkg`` (same program, other args)
    # in the Kiro global: it takes the alias over from the carried-over entry
    # and the app's moves to ``scope-pkg-2``; the stamp moves with the app's.
    settings.write_text(
        json.dumps({"mcpServers": {"scope-pkg": {"command": "cat", "args": ["mine"]}}}),
        encoding="utf-8",
    )
    rebuild_agent_config()
    servers = json.loads(spec.read_text(encoding="utf-8"))["mcpServers"]
    assert SOURCE_KEY not in servers["scope-pkg"]
    assert servers["scope-pkg"]["args"] == ["mine"]
    assert servers["scope-pkg-2"][SOURCE_KEY] == "chorus:@scope/pkg"
    assert mcp_sources._app_owned_mcp_server_apps(servers) == {
        _SERVER: _APP,
        "scope-pkg-2": _APP,
    }

    # A stamp a user file carries (a hand copy of a rendered entry) is dropped:
    # only the entries the app loop mounts are app-sourced.
    settings.write_text(
        json.dumps(
            {"mcpServers": {"mine": {"command": "cat", "args": ["x"], SOURCE_KEY: _SERVER}}}
        ),
        encoding="utf-8",
    )
    rebuild_agent_config()
    servers = json.loads(spec.read_text(encoding="utf-8"))["mcpServers"]
    assert SOURCE_KEY not in servers["mine"]
    assert "mine" not in mcp_sources._app_owned_mcp_server_apps(servers)
