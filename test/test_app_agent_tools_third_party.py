"""A third-party app's manifest ``agentTools``: listed on ``kirocrew-core`` once the owner approved them.

The install card lists each declared tool and installing approves those entries
(``apps/agent_tool_grants.py``). An update that adds or changes a tool holds it back
until the owner approves it on the app's page. These tests use synthetic apps written
into the isolated test home, so they name no real app:

- the grant family: the entry digest, its registration, explicit-only, install and
  update;
- the reader: an approved tool lists, an unapproved or changed one does not, and the
  name rules (a first-party name, a clash, a ``strictSession`` block);
- the gateway: the call route serves an approved tool through the signed proxy and
  refuses another app's session, and the listing route answers what the MCP server
  lists;
- the ``kirocrew-core`` server: it lists and dispatches what the gateway answers.
"""

from __future__ import annotations

import copy
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from conftest import requires_symlinks
from kiro_crew import mcp_core
from kiro_crew.apps import app_agent_tools
from kiro_crew.apps import manager as apps_manager
from kiro_crew.apps import manager_records
from kiro_crew.apps.manifest_agent_tools import AgentToolsConfig
from kiro_crew.apps.point_grants import (
    explicit_only_grant_families,
    grant_source,
    point_grant_families,
)
from kiro_crew.dashboard.handlers import app_agent_tools as handler_mod
from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

_APP = "fake-app"
_OTHER = "other-app"


def _tool(name: str = "fake_app_note", **overrides: Any) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": name,
        "title": "Post a note",
        "description": "Post one note to an item.",
        "inputSchema": {
            "type": "object",
            "properties": {"item_id": {"type": "string", "maxLength": 64}},
            "required": ["item_id"],
        },
        "maxResultChars": 50,
    }
    tool.update(overrides)
    return tool


def _block(*tools: dict[str, Any], route: str = "agent-tools") -> dict[str, Any]:
    return {"route": route, "tools": list(tools) or [_tool()]}


def _manifest(name: str = _APP, block: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "version": "1.0.0",
        "displayName": name,
        "description": "synthetic",
        "backend": {"entryPoint": "server.py", "port": "auto"},
        "agentTools": _block() if block is None else block,
    }


def _entries(name: str = _APP, block: dict[str, Any] | None = None) -> list[str]:
    return grant_source(_manifest(name, block))["agentTools"]


def _all_approved(manifest: dict[str, Any]) -> dict[str, list[str]]:
    """What an install stores when its card listed every declared tool."""
    declared = grant_source(manifest)
    grants = manager_records._consented_grant_entries(declared)
    grants["agentTools"] = list(declared["agentTools"])
    return grants


def _install(
    name: str = _APP,
    block: dict[str, Any] | None = None,
    *,
    enabled: bool = True,
    approve: bool | list[str] = True,
) -> None:
    """A real installed app in the isolated test home, with its approved set stored.

    ``approve=True`` stores what an install whose card listed every tool stores;
    a list stores those ``agentTools`` entries only.
    """
    root = apps_manager.app_dir(name)
    root.mkdir(parents=True, exist_ok=True)
    manifest = _manifest(name, block)
    (root / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / apps_manager.INSTALLED_META_FILENAME).write_text(
        json.dumps({"name": name, "enabled": enabled, "approvedGrantsStored": True}),
        encoding="utf-8",
    )
    grants = _all_approved(manifest)
    if approve is not True:
        grants["agentTools"] = list(approve or [])
    manager_records._write_approved_grants(name, grants)


@pytest.fixture(autouse=True)
def provenance(_floor_monkeypatch) -> dict[str, Any]:
    """Every synthetic app is third party unless a test names it first party.

    ``first_party`` stands in for ``execution.builtin_app_names()`` and for the shipped
    manifests: such an app's declaration is read from the data home. The caches are
    cleared so each test sees its own apps.
    """
    state: dict[str, Any] = {"first_party": set()}

    def _shipped_tools() -> tuple[app_agent_tools.DeclaredTool, ...]:
        return app_agent_tools._collect(sorted(state["first_party"]), drop_clashes=False)

    _floor_monkeypatch.setattr(
        app_agent_tools, "_first_party_apps", lambda: frozenset(state["first_party"])
    )
    _floor_monkeypatch.setattr(
        app_agent_tools, "_first_party_manifest", apps_manager.get_app_manifest
    )
    _floor_monkeypatch.setattr(app_agent_tools, "shipped_tools", _shipped_tools)
    _floor_monkeypatch.setattr(
        app_agent_tools,
        "_shipped_first_party_roots",
        lambda: {name: apps_manager.app_dir(name) for name in state["first_party"]},
    )
    _floor_monkeypatch.setattr(app_agent_tools, "_cache", None)
    _floor_monkeypatch.setattr(app_agent_tools, "_third_party_cache", None, raising=False)
    return state


@pytest.fixture(autouse=True)
def _quiet_sel(_floor_monkeypatch):
    from kiro_crew.dashboard import token_auth

    class _Sel:
        def log_api_access(self, **_kw: Any) -> None:
            pass

    _floor_monkeypatch.setattr(handler_mod, "sel", lambda: _Sel())
    _floor_monkeypatch.setattr(token_auth, "_sel_fn", lambda: _Sel())


def _listed() -> list[app_agent_tools.DeclaredTool]:
    """What the gateway lists: the first-party tools, then the approved third-party ones."""
    return [*app_agent_tools.declared_tools(), *app_agent_tools.third_party_tools()]


def _names() -> list[str]:
    return [t.name for t in _listed()]


# ── The grant family ─────────────────────────────────────────────────────────


def test_the_family_is_registered_and_explicit_only():
    assert "agentTools" in point_grant_families()
    assert "agentTools" in explicit_only_grant_families()
    assert "agentTools" in manager_records.STAGED_GRANT_FAMILIES


def test_an_entry_is_the_tool_name_and_a_stable_digest():
    (entry,) = _entries()
    name, digest = entry.split("@")
    assert name == "fake_app_note"
    assert len(digest) == 12 and all(c in "0123456789abcdef" for c in digest)
    # The same declaration written another way (key order, an explicit default)
    # is the same entry, so a reformatted manifest asks nothing again.
    reordered = dict(reversed(list(_tool().items())))
    assert _entries(block=_block(reordered)) == [entry]
    assert _entries(block=_block(_tool(timeoutSeconds=30))) == [entry]
    # A parsed manifest gives the same entry as the raw dict.
    from kiro_crew.apps.manifest import AppManifest

    assert grant_source(AppManifest.from_dict(_manifest()))["agentTools"] == [entry]


@pytest.mark.parametrize(
    "change",
    [
        {"route": "other-route"},
        {"title": "Another title"},
        {"description": "Post one note, then email the owner."},
        {"inputSchema": {"type": "object", "properties": {"item_id": {"type": "string"}}}},
        {"timeoutSeconds": 60},
        {"result": "text"},
        {"strictSession": True},
        {"maxResultChars": 51},
    ],
    ids=lambda c: next(iter(c)),
)
def test_every_digest_field_changes_the_entry(change):
    (before,) = _entries()
    if "route" in change:
        after = _entries(block=_block(route=change["route"]))
    else:
        after = _entries(block=_block(_tool(**change)))
    assert len(after) == 1 and after != [before]


def test_an_install_alone_approves_no_tool():
    """No card means no tool: the owner approves them on the app's page."""
    consented = manager_records._consented_grant_entries(grant_source(_manifest()))
    assert consented["agentTools"] == []


def test_a_stored_set_from_before_the_family_approves_no_tool():
    _install()
    text = json.dumps({"api": [], "events": []}) + "\n"
    manager_records.protected_app_state.write_grants_text(_APP, text)
    assert manager_records.staged_app_grants(_APP, "agentTools", _entries) == []
    assert _names() == []


def test_an_update_holds_back_a_new_and_a_changed_tool_only():
    old = _block(_tool(), _tool("fake_app_list"))
    new = _block(
        _tool(inputSchema={"type": "object", "properties": {}}),
        _tool("fake_app_list"),
        _tool("fake_app_tag"),
    )
    approved = _all_approved(_manifest(block=old))
    after = manager_records._approved_grants_after_manifest_change(
        existing_approved=approved,
        old_permissions=grant_source(_manifest(block=old)),
        new_permissions=grant_source(_manifest(block=new)),
    )
    kept = [e.split("@")[0] for e in after["agentTools"]]
    held = manager_records._held_back_entries(after, grant_source(_manifest(block=new)))
    assert kept == ["fake_app_list"]
    assert [e.split("@")[0] for e in held["agentTools"]] == ["fake_app_note", "fake_app_tag"]


def test_an_update_that_changes_the_route_holds_back_every_tool():
    old = _block(_tool(), _tool("fake_app_list"))
    approved = _all_approved(_manifest(block=old))
    after = manager_records._approved_grants_after_manifest_change(
        existing_approved=approved,
        old_permissions=grant_source(_manifest(block=old)),
        new_permissions=grant_source(
            _manifest(block=_block(_tool(), _tool("fake_app_list"), route="tools"))
        ),
    )
    assert after["agentTools"] == []


def test_the_app_row_shows_held_back_tools_by_name_and_description():
    from kiro_crew.apps.agent_tool_grants import agent_tools_grant_row

    _install(block=_block(_tool(), _tool("fake_app_list")), approve=[])
    approved = _entries(block=_block(_tool()))
    manager_records._write_approved_grants(
        _APP,
        {
            **_all_approved(_manifest()),
            "agentTools": approved,
        },
    )
    row = agent_tools_grant_row(_APP, _manifest(block=_block(_tool(), _tool("fake_app_list"))))
    grant = row["agentToolsGrant"]
    assert [t["name"] for t in grant["approved"]] == ["fake_app_note"]
    (staged,) = grant["staged"]
    assert staged["name"] == "fake_app_list"
    assert staged["description"] == "Post one note to an item."
    assert staged["entry"].startswith("fake_app_list@")


def test_a_first_party_app_row_has_no_agent_tools_grant(provenance):
    from kiro_crew.apps.agent_tool_grants import agent_tools_grant_row

    provenance["first_party"].add(_APP)
    _install()
    assert agent_tools_grant_row(_APP, _manifest()) == {}


def test_a_pin_adopted_app_row_does_not_reenter_get_app(
    real_home, tmp_path, monkeypatch, provenance
):
    from builtin_adoption_helpers import REPOSITORY, install_pins, pin_row, seed_agent

    from kiro_crew.apps.builtin_adoption_gate import adopted_under_pin_trust

    provenance["first_party"].add(_APP)
    # Keep the shipped declaration, and give the adopted record a registry origin.
    monkeypatch.setattr(app_agent_tools, "_first_party_apps", lambda: frozenset())
    _install(approve=[])
    record = apps_manager._read_installed(_APP)
    record.origin = "registry"
    record.source = f"registry:{_APP}"
    record.sourceUrl = REPOSITORY
    apps_manager._write_installed(_APP, record)
    install_pins(
        monkeypatch,
        tmp_path / "adoption-pins.json",
        [pin_row("a" * 40, "b" * 64, "c" * 64, name=_APP)],
    )
    seed_agent(
        real_home,
        {"apps_trusted": [_APP], "apps_trusted_repositories": {_APP: REPOSITORY}},
    )
    get_app = apps_manager.get_app
    calls = []

    def counted_get_app(name):
        calls.append(name)
        if len(calls) > 1:
            pytest.fail("app row building reentered get_app")
        return get_app(name)

    monkeypatch.setattr(apps_manager, "get_app", counted_get_app)
    row = apps_manager.get_app(_APP)
    assert row["name"] == _APP and "agentToolsGrant" not in row
    assert calls == [_APP]
    assert not app_agent_tools.is_third_party(_APP)
    calls.clear()
    rows = apps_manager.list_apps()
    assert [row["name"] for row in rows] == [_APP]
    assert "agentToolsGrant" not in rows[0]
    assert calls == []
    # The real repository-bound trust lookup still works and enters get_app only once.
    assert adopted_under_pin_trust(_APP)
    assert calls == [_APP]


def test_the_catalog_discloses_the_tools_before_install():
    from kiro_crew.apps.official_catalog import _disclosed_permissions_manifest

    block = _block(_tool(), _tool("fake_app_list", description="List items."))
    note, listed = _entries(block=block)
    assert _disclosed_permissions_manifest({"agentTools": block}) == {
        "agentTools": {
            "tools": [
                {
                    "name": "fake_app_note",
                    "description": "Post one note to an item.",
                    "entry": note,
                },
                {"name": "fake_app_list", "description": "List items.", "entry": listed},
            ]
        }
    }
    entry = {"agentTools": block}
    with_api = {**entry, "permissions": {"api": ["/api/apps/fake-app/x"]}}
    assert set(_disclosed_permissions_manifest(with_api) or {}) == {"permissions", "agentTools"}
    assert _disclosed_permissions_manifest({"agentTools": "not a block"}) is None


@pytest.mark.parametrize("kind", [[], 5, ["strng"], ["string", []], ["string", "null"], "null"])
def test_bad_catalog_tool_types_leave_valid_siblings_listed(monkeypatch, kind):
    from kiro_crew.apps import official_catalog

    bad = _tool()
    bad["inputSchema"]["properties"]["note"] = {"type": kind}
    good = _tool()
    good["inputSchema"]["properties"]["note"] = {"type": "string"}
    good_block = _block(good)
    entries = [
        {"name": "bad-app", "agentTools": _block(bad, _tool("fake_app_other"))},
        {"name": _APP, "agentTools": good_block},
    ]
    monkeypatch.setattr(official_catalog, "load_official_catalog", lambda: entries)
    rows = {row["name"]: row for row in official_catalog.list_catalog_rows()}
    assert set(rows) == {"bad-app", _APP}
    assert "manifest" not in rows["bad-app"]
    (tool,) = rows[_APP]["manifest"]["agentTools"]["tools"]
    assert tool["name"] == "fake_app_note"
    assert tool["entry"] == _entries(block=good_block)[0]
    assert AgentToolsConfig.from_value(entries[0]["agentTools"]).errors
    assert (
        app_agent_tools.tool_from_row({"app": "bad-app", "route": "agent-tools", "tool": bad})
        is None
    )


@pytest.mark.parametrize("error", [TypeError, ValueError, AttributeError])
def test_catalog_parser_failure_is_isolated_to_one_app(monkeypatch, caplog, error):
    from kiro_crew.apps import official_catalog

    bad = _block()
    good = _block()
    parse = AgentToolsConfig.from_value

    def fail_one(block):
        if block is bad:
            raise error("invalid declaration")
        return parse(block)

    monkeypatch.setattr(AgentToolsConfig, "from_value", fail_one)
    monkeypatch.setattr(
        official_catalog,
        "load_official_catalog",
        lambda: [{"name": "bad-app", "agentTools": bad}, {"name": _APP, "agentTools": good}],
    )
    with caplog.at_level("DEBUG", logger=official_catalog.__name__):
        rows = {row["name"]: row for row in official_catalog.list_catalog_rows()}
    assert set(rows) == {"bad-app", _APP}
    assert "manifest" not in rows["bad-app"]
    assert rows[_APP]["manifest"]["agentTools"]["tools"][0]["name"] == "fake_app_note"
    assert "app bad-app: invalid agentTools omitted" in caplog.text


def test_a_lone_surrogate_in_a_declaration_still_gives_an_entry():
    """A ``\\ud800`` JSON escape in a declared field does not crash the digest."""
    import hashlib

    from kiro_crew.apps.official_catalog import _disclosed_permissions_manifest

    block = json.loads(json.dumps(_block(_tool(description="Note \ud800 one."))))
    (entry,) = _entries(block=block)
    assert entry.startswith("fake_app_note@") and entry != _entries()[0]
    disclosed = _disclosed_permissions_manifest({"agentTools": block}) or {}
    assert [t["entry"] for t in disclosed["agentTools"]["tools"]] == [entry]
    # Any other string keeps the digest it had before: the UTF-8 bytes are unchanged.
    (plain,) = _entries(block=_block(_tool(description="Notiz für ein Element.")))
    tool = AgentToolsConfig.from_value(_block(_tool(description="Notiz für ein Element.")))
    decl = tool.tools[0]
    fields = {
        "route": "agent-tools",
        "title": decl.title,
        "description": decl.description,
        "inputSchema": decl.input_schema,
        "timeoutSeconds": decl.timeout_seconds,
        "result": decl.result,
        "strictSession": decl.strict_session,
        "maxResultChars": decl.max_result_chars,
    }
    text = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert plain == "fake_app_note@" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


@pytest.fixture
def real_home(tmp_path, monkeypatch):
    """A data home that admits a third-party install, as ``test_app_kiro_calls`` sets one up."""
    from kiro_crew.apps import execution

    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    monkeypatch.setattr(execution, "third_party_ceiling_closed", lambda _name: None)
    return home


def _source(tmp_path, block: dict[str, Any], version: str) -> Any:
    source = tmp_path / f"source-{version}" / _APP
    source.mkdir(parents=True)
    manifest = {**_manifest(block=block), "version": version}
    manifest.pop("backend")
    (source / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    return source


def test_an_install_no_card_preceded_approves_no_tool(real_home, tmp_path):
    """A trusted-repository skip, a registry row with no block, or the CLI: no card."""
    from kiro_crew.apps.manager import get_app, install_app

    assert install_app(_source(tmp_path, _block(_tool(), _tool("fake_app_list")), "1.0.0")).ok
    assert _names() == []
    staged = get_app(_APP)["agentToolsGrant"]["staged"]
    assert [t["name"] for t in staged] == ["fake_app_note", "fake_app_list"]


def test_an_install_with_a_list_type_names_what_to_fix(real_home, tmp_path):
    from kiro_crew.apps.manager import install_app

    tool = _tool()
    tool["inputSchema"]["properties"]["note"] = {"type": ["string", "null"]}
    result = install_app(_source(tmp_path, _block(tool), "1.0.0"))
    assert not result.ok
    assert (
        "agentTools tool 'fake_app_note' argument 'note': "
        "type must be a single type name string, not a list"
    ) in (result.error or "")


def test_a_card_install_approves_only_the_entries_the_card_listed(real_home, tmp_path):
    from kiro_crew.apps.manager import get_app, install_app

    block = _block(_tool(), _tool("fake_app_list"))
    note, _listed = _entries(block=block)
    # The card listed one tool, plus an entry the manifest does not declare (a
    # stale row's digest): only the declared, listed one is approved.
    stale = "fake_app_list@000000000000"
    assert install_app(_source(tmp_path, block, "1.0.0"), agent_tools_consent=[note, stale]).ok
    assert _names() == ["fake_app_note"]
    grant = get_app(_APP)["agentToolsGrant"]
    assert [t["name"] for t in grant["approved"]] == ["fake_app_note"]
    assert [t["name"] for t in grant["staged"]] == ["fake_app_list"]
    stored = manager_records.staged_app_grants(_APP, "agentTools", lambda: [note, stale])
    assert stored == [note]


@pytest.mark.asyncio
@pytest.mark.parametrize("consent", ["shown", "stale", "absent"])
async def test_a_self_managed_registry_install_keeps_only_fresh_card_consent(
    real_home, tmp_path, monkeypatch, consent
):
    from apps_registry_helpers import _identity_harness

    from kiro_crew.apps import registry

    block = _block(_tool(), _tool("fake_app_list"))
    manifest = _manifest(block=block)
    manifest.pop("backend")
    source = tmp_path / "checkout"
    _identity_harness(monkeypatch, source, cloned_manifest=manifest)
    monkeypatch.setattr(
        registry,
        "get_registry_app",
        lambda name: {
            "name": _APP,
            "repo": "https://example.com/demo.git",
            "branch": "main",
            "resources": "app",
        },
    )
    monkeypatch.setattr("kiro_crew.apps.official_catalog.inventory_for_install", lambda name: None)
    note, listed = _entries(block=block)
    stale = "fake_app_list@000000000000"
    shown = [note, stale] if consent == "shown" else [stale] if consent == "stale" else []
    result = await registry.install_from_registry(_APP, agent_tools_consent=shown)
    assert result["ok"], result
    expected = [note] if consent == "shown" else []
    stored = manager_records._effective_approved_grants(_APP, apps_manager._read_installed(_APP))
    assert stored["agentTools"] == expected

    # Relaunching or updating an installed app cannot turn card entries into new grants.
    for version in ("1.0.0", "1.1.0"):
        manifest["version"] = version
        assert apps_manager.register_external_app(
            _APP,
            version,
            _APP,
            manifest_data=manifest,
            agent_tools_consent=[note, listed],
        ).ok
        stored = manager_records._effective_approved_grants(
            _APP, apps_manager._read_installed(_APP)
        )
        assert stored["agentTools"] == expected


@pytest.mark.parametrize("declares_tools", [False, True])
def test_widening_without_held_agent_tools_never_checks_provenance(monkeypatch, declares_tools):
    manifest = _manifest()
    if not declares_tools:
        manifest.pop("agentTools")
    permissions = grant_source(manifest)
    approved = _all_approved(manifest)

    def unexpected_provenance(_name):
        pytest.fail("no held agent tools should need a provenance scan")

    monkeypatch.setattr(app_agent_tools, "is_third_party", unexpected_provenance)
    held = manager_records._widening_entries(_APP, approved, permissions)
    assert not any(held.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("third_party", [False, True])
async def test_self_managed_registry_precomputes_agent_tool_provenance_off_loop(
    real_home, tmp_path, monkeypatch, provenance, widened_ops, third_party
):
    from apps_registry_helpers import _identity_harness

    from kiro_crew.apps import registry

    manifest = _manifest()
    manifest.pop("backend")
    _identity_harness(monkeypatch, tmp_path / "checkout", cloned_manifest=manifest)
    monkeypatch.setattr(
        registry,
        "get_registry_app",
        lambda name: {
            "name": _APP,
            "repo": "https://example.com/demo.git",
            "branch": "main",
            "resources": "app",
        },
    )
    monkeypatch.setattr("kiro_crew.apps.official_catalog.inventory_for_install", lambda name: None)
    if not third_party:
        provenance["first_party"].add(_APP)
    loop_thread = threading.get_ident()
    checked = []
    registered = []
    is_third_party = app_agent_tools.is_third_party
    register = apps_manager.register_external_app

    def check_provenance(name):
        assert threading.get_ident() != loop_thread, "provenance scanned on the event loop"
        checked.append(name)
        return is_third_party(name)

    def register_on_loop(*args, **kwargs):
        assert threading.get_ident() == loop_thread
        assert kwargs["third_party"] is third_party
        registered.append(kwargs["name"])
        return register(*args, **kwargs)

    monkeypatch.setattr(app_agent_tools, "is_third_party", check_provenance)
    monkeypatch.setattr(apps_manager, "register_external_app", register_on_loop)
    result = await registry.install_from_registry(_APP)
    assert result["ok"], result
    assert checked == [_APP]
    assert registered == [_APP]
    stored = manager_records._effective_approved_grants(_APP, apps_manager._read_installed(_APP))
    assert stored["agentTools"] == []
    assert ("grants_widened" in widened_ops) is third_party


def test_install_update_and_approval_end_to_end(real_home, tmp_path):
    from kiro_crew.apps.manager import enable_app, get_app, install_app, update_app

    v1 = _block(_tool(), _tool("fake_app_list"))
    assert install_app(_source(tmp_path, v1, "1.0.0"), agent_tools_consent=_entries(block=v1)).ok
    assert _names() == ["fake_app_note", "fake_app_list"]

    v2 = _block(
        _tool(inputSchema={"type": "object", "properties": {}}),
        _tool("fake_app_list"),
        _tool("fake_app_tag"),
    )
    assert update_app(_source(tmp_path, v2, "1.1.0")).ok
    # The unchanged tool keeps working; the changed and the added one wait.
    assert _names() == ["fake_app_list"]
    staged = get_app(_APP)["agentToolsGrant"]["staged"]
    assert [t["name"] for t in staged] == ["fake_app_note", "fake_app_tag"]

    assert enable_app(_APP, grants_consent={"agentTools": [t["entry"] for t in staged]}).ok
    assert _names() == ["fake_app_note", "fake_app_list", "fake_app_tag"]
    assert get_app(_APP)["agentToolsGrant"]["staged"] == []


@pytest.fixture
def widened_ops(monkeypatch) -> list[str]:
    """The SEL operations ``update_app`` and ``register_external_app`` log."""
    ops: list[str] = []

    class _Sel:
        def log_api_access(self, **kw: Any) -> None:
            ops.append(kw.get("operation", ""))

    monkeypatch.setattr(apps_manager, "sel", lambda: _Sel())
    return ops


def _adopted_record_without_agent_tools(provenance, tmp_path, block: dict[str, Any]) -> None:
    """A pin-trusted former built-in adopted before the family: no ``agentTools`` key."""
    from kiro_crew.apps.manager import install_app

    provenance["first_party"].add(_APP)
    assert install_app(_source(tmp_path, block, "1.0.0")).ok
    grants = _all_approved(_manifest(block=block))
    grants.pop("agentTools")
    manager_records._write_approved_grants(_APP, grants)


@pytest.mark.parametrize("route", ["update_app", "register_external_app"])
def test_a_first_party_update_never_reports_its_agent_tools_as_new_permissions(
    real_home, tmp_path, provenance, widened_ops, route
):
    """Its tools need no grant, so the page has nothing to offer."""
    from kiro_crew.apps.manager import register_external_app, update_app

    block = _block(_tool(), _tool("fake_app_list"))
    _adopted_record_without_agent_tools(provenance, tmp_path, block)
    widened_ops.clear()
    if route == "update_app":
        result = update_app(_source(tmp_path, block, "1.1.0"))
        assert result.ok and "new permissions" not in result.message
    else:
        manifest = {**_manifest(block=block), "version": "1.1.0"}
        assert register_external_app(_APP, "1.1.0", _APP, manifest_data=manifest).ok
    assert "grants_widened" not in widened_ops


def test_a_third_party_update_that_adds_a_tool_still_reports_new_permissions(
    real_home, tmp_path, widened_ops
):
    from kiro_crew.apps.manager import install_app, update_app

    v1 = _block(_tool())
    assert install_app(_source(tmp_path, v1, "1.0.0"), agent_tools_consent=_entries(block=v1)).ok
    widened_ops.clear()
    result = update_app(_source(tmp_path, _block(_tool(), _tool("fake_app_list")), "1.1.0"))
    assert result.ok and "new permissions" in result.message
    assert "grants_widened" in widened_ops


def _install_both_and_revoke_note(tmp_path) -> dict[str, Any]:
    from kiro_crew.apps.manager import install_app, revoke_agent_tools

    v1 = _block(_tool(), _tool("fake_app_list"))
    assert install_app(_source(tmp_path, v1, "1.0.0"), agent_tools_consent=_entries(block=v1)).ok
    assert revoke_agent_tools(_APP, [_entries(block=v1)[0]]).ok
    return v1


@pytest.mark.parametrize("route", ["update_app", "register_external_app"])
def test_a_revoked_tool_alone_never_reports_new_permissions(
    real_home, tmp_path, widened_ops, route
):
    """The page offers a revoked tool as an off switch, not as a permission to approve."""
    from kiro_crew.apps.manager import register_external_app, update_app

    v1 = _install_both_and_revoke_note(tmp_path)
    widened_ops.clear()
    if route == "update_app":
        result = update_app(_source(tmp_path, v1, "1.1.0"))
        assert result.ok and "new permissions" not in result.message
    else:
        manifest = {**_manifest(block=v1), "version": "1.1.0"}
        assert register_external_app(_APP, "1.1.0", _APP, manifest_data=manifest).ok
    assert "grants_widened" not in widened_ops


def test_an_added_tool_beside_a_revoked_one_still_reports_new_permissions(
    real_home, tmp_path, widened_ops
):
    from kiro_crew.apps.manager import update_app

    v1 = _install_both_and_revoke_note(tmp_path)
    widened_ops.clear()
    result = update_app(_source(tmp_path, _block(*v1["tools"], _tool("fake_app_tag")), "1.1.0"))
    assert result.ok and "new permissions" in result.message
    assert "grants_widened" in widened_ops


def test_an_unreadable_revoked_list_counts_no_agent_tool_as_widening(
    real_home, tmp_path, widened_ops
):
    """The page then offers no tool either (``agent_tools_grant_row``)."""
    from kiro_crew.apps import protected_app_state
    from kiro_crew.apps.manager import update_app

    v1 = _install_both_and_revoke_note(tmp_path)
    protected_app_state._revoked_tools_path(_APP).write_text("not json", encoding="utf-8")
    widened_ops.clear()
    result = update_app(_source(tmp_path, _block(*v1["tools"], _tool("fake_app_tag")), "1.1.0"))
    assert result.ok and "new permissions" not in result.message
    assert "grants_widened" not in widened_ops


def test_the_install_routes_read_only_listed_strings():
    from kiro_crew.apps.routes_registry import agent_tools_consent

    assert agent_tools_consent({"name": "x"}) == []
    assert agent_tools_consent({"agentToolsConsent": "fake_app_note@1"}) == []
    assert agent_tools_consent({"agentToolsConsent": ["a@1", 2, "", None, "b@2"]}) == ["a@1", "b@2"]
    assert len(agent_tools_consent({"agentToolsConsent": ["e"] * 100})) == 16


# ── The reader ───────────────────────────────────────────────────────────────


def test_an_approved_tool_is_listed_as_third_party():
    _install()
    assert app_agent_tools.declared_tools() == ()
    (tool,) = app_agent_tools.third_party_tools()
    assert app_agent_tools.declared_tool("fake_app_note") == tool
    assert (tool.app, tool.route, tool.name, tool.third_party) == (
        _APP,
        "agent-tools",
        "fake_app_note",
        True,
    )


@requires_symlinks
def test_a_linked_app_json_lists_nothing(tmp_path):
    _install()
    manifest = apps_manager.app_dir(_APP) / "app.json"
    target = tmp_path / "other.json"
    manifest.rename(target)
    manifest.symlink_to(target)

    assert _names() == []


def test_a_lookup_during_an_update_swap_does_not_hide_the_tool_after_it():
    _install()
    with manager_records._tree_swap(_APP):
        # The swap grants nothing while it is open.
        assert _names() == []
    assert _names() == ["fake_app_note"]


def test_a_lookup_in_the_install_window_does_not_hide_the_tool_after_it():
    """An install writes the approved set, then the record: between them nothing is granted."""
    _install()
    record = apps_manager.app_dir(_APP) / apps_manager.INSTALLED_META_FILENAME
    text = record.read_text(encoding="utf-8")
    record.unlink()
    assert _names() == []
    record.write_text(text, encoding="utf-8")
    assert _names() == ["fake_app_note"]


def test_an_unapproved_tool_is_not_listed():
    _install(approve=[])
    assert _names() == []
    assert app_agent_tools.declared_tool("fake_app_note") is None


def test_a_changed_declaration_is_not_listed_until_approved_again():
    _install()
    assert _names() == ["fake_app_note"]
    changed = _block(_tool(description="Post one note, then email the owner."))
    (apps_manager.app_dir(_APP) / "app.json").write_text(
        json.dumps(_manifest(block=changed)), encoding="utf-8"
    )
    assert _names() == []
    grants = _all_approved(_manifest(block=changed))
    manager_records._write_approved_grants(_APP, grants)
    assert _names() == ["fake_app_note"]


def test_a_first_party_name_stays_the_first_party_apps(provenance, monkeypatch):
    provenance["first_party"].add(_OTHER)
    _install(_OTHER, _block(_tool("other_app_note")))
    _install(_APP, _block(_tool("fake_app_note")))
    assert _names() == ["other_app_note", "fake_app_note"]
    # A first-party app with a stated namespace can declare the third-party app's
    # name; the first-party app keeps it.
    first = app_agent_tools.DeclaredTool(
        app=_OTHER, route="agent-tools", decl=AgentToolsConfig.from_value(_block()).tools[0]
    )
    monkeypatch.setattr(app_agent_tools, "declared_tools", lambda: (first,))
    monkeypatch.setattr(app_agent_tools, "_third_party_cache", None)
    assert app_agent_tools.third_party_tools() == ()
    assert app_agent_tools.declared_tool("fake_app_note") == first


def test_a_name_two_third_party_apps_declare_belongs_to_neither():
    _install("fake-app", _block(_tool("fake_app_note")))
    _install("fake", _block(_tool("fake_app_note")))
    assert _names() == []


def test_a_block_with_a_strict_session_tool_lists_nothing():
    _install(block=_block(_tool(strictSession=True)))
    assert _names() == []


def test_a_first_party_app_is_not_read_as_third_party(provenance):
    provenance["first_party"].add(_APP)
    _install(approve=[])
    (tool,) = app_agent_tools.declared_tools()
    assert not tool.third_party
    assert app_agent_tools.third_party_tools() == ()


# ── The gateway ──────────────────────────────────────────────────────────────


def _request(
    body: Any,
    *,
    method: str = "POST",
    path: str = "/api/app-tools/call",
    internal: bool = True,
    calling_app: str = "",
    session_key: str = "",
    state: Any = None,
) -> web.Request:
    app = web.Application()
    if state is not None:
        app["state"] = state
    headers = {"X-Session-Key": session_key} if session_key else None
    req = make_mocked_request(method, path, headers=headers, app=app)
    if internal:
        req["internal_auth"] = True
    if calling_app:
        # What token_auth publishes for a session an app owns.
        req["app"] = calling_app
    req._read_bytes = json.dumps(body).encode()
    return req


_CALL = {"tool": "fake_app_note", "arguments": {"item_id": "i-1"}}


@pytest.fixture
def proxied(monkeypatch) -> list[tuple[str, str, Any]]:
    from kiro_crew.apps import routes as app_routes

    seen: list[tuple[str, str, Any]] = []

    async def _proxy(request: web.Request, *, read_body: bytes | None = None) -> web.Response:
        seen.append((request.match_info["name"], request.match_info["path"], read_body))
        return web.json_response({"proxied": True})

    monkeypatch.setattr(app_routes, "handle_app_api_proxy", _proxy)
    return seen


async def _call(**kw: Any) -> tuple[int, dict[str, Any]]:
    resp = await handler_mod.api_app_agent_tool_call(_request(_CALL, **kw))
    return resp.status, json.loads(resp.body)


@pytest.mark.asyncio
async def test_an_approved_tool_is_served_through_the_signed_proxy(proxied):
    _install()
    assert await _call() == (200, {"proxied": True})
    assert proxied == [(_APP, "agent-tools", json.dumps(_CALL).encode())]


@pytest.mark.asyncio
async def test_an_unapproved_tool_is_refused_unknown(proxied):
    _install(approve=[])
    status, body = await _call()
    assert (status, body["code"]) == (404, "unknown_tool")
    assert proxied == []


@pytest.mark.asyncio
async def test_a_disabled_app_is_refused(proxied):
    _install(enabled=False)
    status, body = await _call()
    assert (status, body["code"]) == (403, "app_not_enabled")
    assert proxied == []


@pytest.mark.asyncio
async def test_a_session_another_app_owns_is_refused(proxied):
    _install()
    status, body = await _call(calling_app=_OTHER)
    assert (status, body["code"]) == (403, "cross_app")
    assert proxied == []


@pytest.mark.asyncio
async def test_a_session_the_declaring_app_owns_is_served(proxied):
    _install()
    assert await _call(calling_app=_APP) == (200, {"proxied": True})


@pytest.mark.asyncio
async def test_a_gone_dashboard_slot_is_refused(proxied):
    _install()
    state = SimpleNamespace(_slots={})
    status, body = await _call(session_key="dashboard:gone-slot", state=state)
    assert (status, body["code"]) == (403, "caller_unattributable")
    assert proxied == []


@pytest.mark.asyncio
async def test_a_live_unowned_dashboard_slot_is_served(proxied):
    _install()
    state = SimpleNamespace(_slots={"live": SimpleNamespace()})
    assert await _call(session_key="dashboard:live", state=state) == (200, {"proxied": True})


@pytest.mark.asyncio
async def test_a_first_party_tool_skips_the_cross_app_check(provenance, proxied):
    provenance["first_party"].add(_APP)
    _install()
    assert await _call(calling_app=_OTHER) == (200, {"proxied": True})


def test_the_listing_route_is_strict_internal():
    assert "/api/app-tools/list" in _STRICT_INTERNAL_API_PATHS


@pytest.mark.asyncio
async def test_the_listing_route_answers_the_approved_third_party_tools(provenance):
    provenance["first_party"].add(_OTHER)
    _install(_OTHER, _block(_tool("other_app_note")))
    _install(_APP, _block(_tool(), _tool("fake_app_list")), approve=_entries(block=_block()))
    req = _request(None, method="GET", path="/api/app-tools/list")
    resp = await handler_mod.api_app_agent_tool_list(req)
    rows = json.loads(resp.body)["tools"]
    assert [(r["app"], r["tool"]["name"]) for r in rows] == [(_APP, "fake_app_note")]
    (tool,) = [app_agent_tools.tool_from_row(r) for r in rows]
    assert tool == app_agent_tools.third_party_tools()[0]


@pytest.mark.asyncio
async def test_the_listing_route_refuses_without_the_internal_secret():
    _install()
    req = _request(None, method="GET", path="/api/app-tools/list", internal=False)
    resp = await handler_mod.api_app_agent_tool_list(req)
    assert (resp.status, json.loads(resp.body)["code"]) == (403, "loopback_only")


class TestTheListingRouteFailsClosed:
    """``GET /api/app-tools/list`` behind the REAL ``token_auth_middleware``.

    Membership in ``_STRICT_INTERNAL_API_PATHS`` is not proof on its own: with no
    ``X-Internal-Secret`` the middleware falls through to token auth, so the handler's
    own ``internal_auth`` check is what refuses a dashboard, teammate or app token.
    """

    _SECRET = "the-real-secret"
    _PATH = "/api/app-tools/list"

    def _client(self) -> Any:
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.token_auth import token_auth_middleware

        app = web.Application(
            middlewares=[
                token_auth_middleware(
                    internal_paths=_STRICT_INTERNAL_API_PATHS,
                    internal_secret=self._SECRET,
                    local_only=True,
                )
            ]
        )
        app.router.add_get(self._PATH, handler_mod.api_app_agent_tool_list)
        return TestClient(TestServer(app))

    async def _get(self, path: str = _PATH, **headers: str) -> tuple[int, str]:
        async with self._client() as client:
            resp = await client.get(path, headers=headers)
            return resp.status, await resp.text()

    @staticmethod
    def _refused(status: int, text: str) -> None:
        assert status in (401, 403)
        assert "fake_app_note" not in text and '"tools"' not in text

    @pytest.mark.asyncio
    async def test_a_request_without_the_secret_is_refused(self):
        _install()
        self._refused(*await self._get())

    @pytest.mark.asyncio
    async def test_a_wrong_secret_is_refused(self):
        _install()
        self._refused(*await self._get(**{"X-Internal-Secret": "guessed-wrong"}))

    @pytest.mark.asyncio
    async def test_a_dashboard_or_teammate_token_from_a_browser_is_refused(self):
        from kiro_crew.dashboard.token_auth import generate_token

        _install()
        for user in ("owner", "teammate"):
            token = generate_token(user, 3600)
            self._refused(
                *await self._get(f"{self._PATH}?token={token}", Origin="http://127.0.0.1:5466")
            )

    @pytest.mark.asyncio
    async def test_an_app_token_is_refused(self):
        from kiro_crew.dashboard.token_auth import generate_token

        _install()
        token = generate_token(_APP, 3600, app=_APP)
        self._refused(*await self._get(f"{self._PATH}?token={token}"))
        self._refused(*await self._get(Authorization=f"Bearer {token}"))

    @pytest.mark.asyncio
    async def test_the_right_secret_lists_and_the_answer_holds_no_secret(self):
        """The control case, and what the answer may carry: names, descriptors, route."""
        import os

        _install()
        root = apps_manager.app_dir(_APP)
        (root / ".app_secret").write_text("app-secret-sentinel", encoding="utf-8")
        status, text = await self._get(**{"X-Internal-Secret": self._SECRET})
        assert status == 200
        body = json.loads(text)
        assert set(body) == {"tools"}
        (row,) = body["tools"]
        assert set(row) == {"app", "route", "tool"}
        assert set(row["tool"]) <= {
            "name",
            "title",
            "description",
            "inputSchema",
            "maxResultChars",
            "strictSession",
            "timeoutSeconds",
            "result",
        }
        for leaked in (self._SECRET, "app-secret-sentinel", os.environ["KIROCREW_HOME"], str(root)):
            assert leaked not in text
        assert not {"env", "token", "secret", "path", "command"} & set(row["tool"])


def test_a_listing_row_that_breaks_the_block_rules_is_dropped():
    row = {"app": _APP, "route": "agent-tools", "tool": _tool()}
    assert app_agent_tools.tool_from_row(row) is not None
    assert app_agent_tools.tool_from_row({**row, "app": _OTHER}) is None  # foreign namespace
    assert app_agent_tools.tool_from_row({**row, "tool": _tool(strictSession=True)}) is None
    assert app_agent_tools.tool_from_row({**row, "route": "../x"}) is None


# ── The kirocrew-core server ─────────────────────────────────────────────────


class _FakeGateway:
    """``mcp_core._get`` / ``_post`` against a gateway that lists *rows*."""

    def __init__(self, rows: list[dict[str, Any]], answer: dict[str, Any]) -> None:
        self.rows = rows
        self.answer = answer
        self.posts: list[tuple[str, Any]] = []

    def get(self, path: str, *_a: Any, **_kw: Any) -> dict[str, Any]:
        assert path == "/api/app-tools/list"
        return {"tools": copy.deepcopy(self.rows)}

    def post(self, path: str, body: Any = None, **_kw: Any) -> dict[str, Any]:
        self.posts.append((path, copy.deepcopy(body)))
        return dict(self.answer)


@pytest.fixture
def gateway(monkeypatch) -> _FakeGateway:
    """The server process: it reads no approval, only what the gateway answers."""
    row = {"app": _APP, "route": "agent-tools", "tool": _tool()}
    fake = _FakeGateway([row], {"ok": True})
    monkeypatch.setattr(mcp_core, "_get", fake.get)
    monkeypatch.setattr(mcp_core, "_post", fake.post)
    monkeypatch.setattr(app_agent_tools, "third_party_tools", lambda: ())
    return fake


def test_the_server_lists_what_the_gateway_answers(gateway):
    from kiro_crew.mcp_tools.catalog import app_tools as catalog

    _install(approve=[])
    assert [t["name"] for t in catalog.advertised()] == ["fake_app_note"]
    assert "fake_app_note" not in [t["name"] for t in catalog.schemas()]


@pytest.mark.parametrize("location", ["gateway", "stdio", "pooled-stdio"])
def test_third_party_rows_use_the_callers_transport(monkeypatch, location):
    from unittest.mock import Mock

    from kiro_crew import mcp_caller, mcp_listing_gateway, mcp_listing_relay
    from kiro_crew.mcp_tools import app_tools
    from kiro_crew.mcp_tools.catalog import app_tools as catalog

    _install(_APP, _block(_tool(), _tool("fake_app_list")), approve=_entries(block=_block()))
    rows = handler_mod._third_party_rows()
    get = Mock(return_value={"tools": rows})
    monkeypatch.setattr(mcp_core, "_get", get)
    caller = mcp_caller.CallerContext(session_key="dashboard:mine", from_gateway=True)
    if location == "gateway":
        get.side_effect = AssertionError("in-process reads must not call HTTP")
        monkeypatch.setattr(mcp_listing_gateway, "_left_to_the_server", lambda *a, **kw: "")
    else:
        monkeypatch.setattr(app_agent_tools, "third_party_tools", lambda: ())
        caller = (
            mcp_caller.CallerContext.from_meta(mcp_caller.build_caller_meta(caller))
            if location == "pooled-stdio"
            else None
        )
    previous = mcp_caller.current_caller()
    mcp_caller.set_current_caller(caller)
    try:
        if location == "gateway":
            body = json.loads(
                mcp_listing_gateway.session_listing_body(
                    None,
                    "mcp-core",
                    "dashboard:mine",
                    "kirocrew",
                    code="",
                    place="",
                    private_member=False,
                )
            )
            listed = body[mcp_listing_relay.LISTING_KEY]["tools"]
        else:
            listed = catalog.advertised()
        names = {t["name"] for t in listed}
        assert "fake_app_note" in names and "fake_app_list" not in names
        assert app_tools.declared("fake_app_note") is not None
        assert app_tools.tool_schema("fake_app_note") is not None
        assert app_tools.declared("fake_app_list") is None
        assert app_tools.tool_schema("fake_app_list") is None
        assert app_tools.app_agent_tool("fake_app_list", {}) == (
            "Error: no installed app declares the tool 'fake_app_list'."
        )
        if location == "gateway":
            get.assert_not_called()
        else:
            get.assert_called_with("/api/app-tools/list")
    finally:
        mcp_caller.set_current_caller(previous)


def test_the_server_lists_nothing_when_the_gateway_cannot_answer(monkeypatch):
    from kiro_crew.mcp_tools.catalog import app_tools as catalog

    def _down(*_a: Any, **_kw: Any) -> Any:
        raise OSError("gateway down")

    monkeypatch.setattr(mcp_core, "_get", _down)
    _install(approve=[])
    assert catalog.advertised() == []


def test_the_server_validates_and_forwards_a_call(gateway):
    from kiro_crew import mcp_tools
    from kiro_crew.validation_parts.field_engine import ValidationError

    _install(approve=[])
    with pytest.raises(ValidationError):
        mcp_core._validate_args("fake_app_note", {"item_id": "i-1", "extra": 1})
    args = mcp_core._validate_args("fake_app_note", {"item_id": "i-1"})
    assert mcp_tools.dispatch("fake_app_note", args) == '{"ok": true}'
    assert gateway.posts == [("/api/app-tools/call", _CALL)]


@pytest.mark.parametrize("with_error", [False, True])
def test_a_third_party_text_answer_never_hands_over_a_credential_verbatim(gateway, with_error):
    """Reject the credential envelope even when the redactor keeps its opaque token."""
    from kiro_crew import mcp_tools
    from kiro_crew.mcp_tools import app_tools
    from kiro_crew.testing.synthetic_credentials import synth

    secret = synth(32, alphabet="0123456789abcdef")
    assert app_tools.redact(secret) == secret
    gateway.rows[0]["tool"] = _tool(result="text", maxResultChars=500)
    gateway.answer = {"text": ["before ", " after"], "credential": secret}
    if with_error:
        gateway.answer["error"] = secret
    _install(approve=[])
    text = mcp_tools.dispatch("fake_app_note", {"item_id": "i-1"})
    assert secret not in text
    assert text.startswith("Error: fake_app_note returned no result")


def test_an_unknown_name_is_still_unknown(gateway):
    from kiro_crew import mcp_tools

    assert mcp_tools.dispatch("nobody_tool", {}) == "Unknown tool: nobody_tool"
    assert gateway.posts == []


def test_the_own_agent_grant_never_covers_a_third_party_tool():
    """``kirocrew-core`` is approved for the default agent, so a person's chat calls an
    approved tool unasked; an app's own agent is no such grant, so it keeps its card."""
    from kiro_crew.mcp_tools.catalog import app_tools as catalog

    _install()
    assert [t.name for t in app_agent_tools.third_party_tools()] == ["fake_app_note"]
    assert catalog._tools() == []


# ── Revoking an approved tool on the app page ────────────────────────────────

_OWNER = "U_OWNER"
_TWO = _block(_tool(), _tool("fake_app_list"))


def _revoke_request(
    entries: Any,
    *,
    user: str = _OWNER,
    app_claim: str | None = "",
    internal: bool = False,
    payload: Any = None,
) -> web.Request:
    """``POST /api/apps/{name}/enable`` carrying ``agentToolsRevoke`` (or *payload*), as
    *user* sends it."""
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id=_OWNER)
    req = make_mocked_request(
        "POST",
        f"/api/apps/{_APP}/enable",
        headers={"Content-Type": "application/json"},
        app=app,
        match_info={"name": _APP},
    )
    if app_claim is not None:
        req["app"] = app_claim
    if user:
        req["user"] = user
    if internal:
        req["internal_auth"] = True
    body = {"agentToolsRevoke": entries} if payload is None else payload

    async def _json() -> Any:
        return body

    req.json = _json  # type: ignore[method-assign]
    return req


@pytest.fixture
def enable_calls(monkeypatch) -> list[Any]:
    """The enable route's own enable: a revoke must never reach it."""
    from kiro_crew.apps import routes as app_routes

    monkeypatch.setattr(type(make_mocked_request("POST", "/")), "can_read_body", True)
    calls: list[Any] = []

    def _enable(*a: Any, **kw: Any) -> Any:
        calls.append((a, kw))
        raise AssertionError("a revoke must not enable the app")

    monkeypatch.setattr(app_routes, "enable_app", _enable)
    return calls


async def _revoke(entries: Any, **kw: Any) -> tuple[int, dict[str, Any]]:
    from kiro_crew.apps import routes as app_routes

    resp = await app_routes.handle_enable_app(_revoke_request(entries, **kw))
    return resp.status, json.loads(resp.body)


def _stored() -> str | None:
    from kiro_crew.apps import protected_app_state

    return protected_app_state.read_grants_text(_APP)


@pytest.mark.asyncio
async def test_a_revoke_drops_only_that_tool_and_its_call_is_refused(enable_calls, proxied):
    _install(block=_TWO)
    note, listed = _entries(block=_TWO)
    before = json.loads(_stored() or "{}")
    status, body = await _revoke([note])
    assert status == 200, body
    assert enable_calls == []
    assert _names() == ["fake_app_list"]
    after = json.loads(_stored() or "{}")
    assert after == {**before, "agentTools": [listed]}
    route = await handler_mod.api_app_agent_tool_list(
        _request(None, method="GET", path="/api/app-tools/list")
    )
    assert [r["tool"]["name"] for r in json.loads(route.body)["tools"]] == ["fake_app_list"]
    status, body = await _call()
    assert (status, body["code"]) == (404, "unknown_tool")
    assert proxied == []
    grant = apps_manager.get_app(_APP)["agentToolsGrant"]
    assert grant["staged"] == []
    assert [t["name"] for t in grant["revoked"]] == ["fake_app_note"]


@pytest.mark.asyncio
@pytest.mark.parametrize("submitted", ["current", "unheld_digest"])
async def test_a_revoke_removes_every_approved_entry_for_the_name(real_home, proxied, submitted):
    v1 = _TWO
    v2 = _block(_tool(description="Post one note, then email the owner."), _tool("fake_app_list"))
    _install(block=v1)
    note_v1, listed = _entries(block=v1)
    note_v2 = _entries(block=v2)[0]
    manifest = apps_manager.app_dir(_APP) / "app.json"
    manifest.write_text(json.dumps(_manifest(block=v2)), encoding="utf-8")
    assert apps_manager.enable_app(_APP, grants_consent={"agentTools": [note_v2]}).ok
    before = json.loads(_stored() or "{}")
    assert set(before["agentTools"]) == {note_v1, note_v2, listed}
    assert note_v1 != note_v2
    assert _names() == ["fake_app_note", "fake_app_list"]

    entry = note_v2 if submitted == "current" else "fake_app_note@000000000000"
    assert apps_manager.revoke_agent_tools(_APP, [entry, "fake_app_never@000000000000"]).ok
    manifest.write_text(json.dumps(_manifest(block=v1)), encoding="utf-8")
    assert [t.name for t in app_agent_tools.third_party_tools()] == ["fake_app_list"]
    assert app_agent_tools.declared_tool("fake_app_note") is None
    status, body = await _call()
    assert (status, body["code"]) == (404, "unknown_tool")
    assert proxied == []
    assert json.loads(_stored() or "{}") == {**before, "agentTools": [listed]}
    assert _revoked_names() == {"fake_app_note"}

    manifest.write_text(json.dumps(_manifest(block=v2)), encoding="utf-8")
    assert apps_manager.enable_app(_APP, grants_consent={"agentTools": [note_v2]}).ok
    assert json.loads(_stored() or "{}")["agentTools"] == [listed, note_v2]
    assert _revoked_names() == frozenset()
    assert _names() == ["fake_app_note", "fake_app_list"]
    manifest.write_text(json.dumps(_manifest(block=v1)), encoding="utf-8")
    assert app_agent_tools.declared_tool("fake_app_note") is None


@pytest.mark.asyncio
async def test_a_revoke_of_an_entry_that_is_not_approved_changes_nothing(enable_calls):
    _install(block=_TWO, approve=_entries(block=_block()))
    before = _stored()
    for entries in ([_entries(block=_TWO)[1]], ["fake_app_list@000000000000"], []):
        status, body = await _revoke(entries)
        assert status == 200, body
        assert _stored() == before
        assert _revoked_names() == frozenset()
    assert _names() == ["fake_app_note"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entries", ["fake_app_note@1", {"x": 1}, None, [1], ["e"] * 17])
async def test_a_malformed_revoke_is_refused(enable_calls, entries):
    _install()
    before = _stored()
    status, body = await _revoke(entries)
    assert (status, body["code"]) == (400, "invalid_agent_tools_revoke")
    assert _stored() == before


_REFUSED_CALLERS = pytest.mark.parametrize(
    "caller",
    [
        {"app_claim": _APP},  # the app's own token
        {"app_claim": None, "user": "", "internal": True},  # the internal secret
        {"user": ""},  # no session
    ],
    ids=["app_token", "internal_secret", "no_session"],
)


@pytest.fixture
def teammate(monkeypatch) -> None:
    """A Shared Crew teammate's session: it passes the dashboard's owner gate."""
    from kiro_crew.shared_crew import teammate_access

    monkeypatch.setattr(
        teammate_access, "teammate_passes_owner_gate", lambda r: r.get("user") == "U_TEAMMATE"
    )


@pytest.mark.asyncio
@_REFUSED_CALLERS
async def test_an_app_an_agent_or_no_session_cannot_revoke(enable_calls, teammate, caller):
    _install()
    before = _stored()
    status, _body = await _revoke(_entries(), **caller)
    assert status in (401, 403)
    assert _stored() == before
    assert _names() == ["fake_app_note"]


@pytest.mark.asyncio
async def test_a_teammate_can_revoke(enable_calls, teammate):
    _install()
    status, body = await _revoke(_entries(), user="U_TEAMMATE")
    assert status == 200, body
    assert _names() == []
    assert _revoked_names() == {"fake_app_note"}


@pytest.mark.asyncio
async def test_a_revoke_that_cannot_be_saved_reports_failure(enable_calls, monkeypatch):
    from kiro_crew.apps import protected_app_state

    _install()

    def _full(*_a: Any, **_kw: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(protected_app_state, "write_grants_text", _full)
    status, body = await _revoke(_entries())
    assert (status, body["code"]) == (500, "agent_tools_not_saved")


@pytest.mark.parametrize("revoked_text", [None, '{ "revoked": ["fake_app_old"] }\n'])
def test_a_failed_revoke_restores_both_records(monkeypatch, revoked_text):
    from kiro_crew.apps import protected_app_state

    _install(block=_TWO)
    protected_app_state._write_revoked_text(_APP, revoked_text)
    path = protected_app_state._revoked_tools_path(_APP)
    before_revoked = path.read_bytes() if path.exists() else None
    before_approved = _stored()

    def _full(*_a: Any, **_kw: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(apps_manager, "_write_approved_grants", _full)
    result = apps_manager.revoke_agent_tools(_APP, [_entries(block=_TWO)[0]])
    assert not result.ok
    assert result.error_code == "agent_tools_not_saved"
    assert (path.read_bytes() if path.exists() else None) == before_revoked
    assert _stored() == before_approved
    assert _names() == ["fake_app_note", "fake_app_list"]


@pytest.mark.parametrize("other_revoked", [False, True])
def test_a_failed_turn_on_restores_both_records(real_home, monkeypatch, other_revoked):
    from kiro_crew.apps import protected_app_state

    _install(block=_TWO, approve=[_entries(block=_TWO)[1]])
    names = ["fake_app_note", "fake_app_old"] if other_revoked else ["fake_app_note"]
    protected_app_state._write_revoked_text(_APP, json.dumps({"revoked": names}, indent=2) + "\n")
    path = protected_app_state._revoked_tools_path(_APP)
    before_revoked = path.read_bytes()
    before_approved = _stored()

    def _full(*_a: Any, **_kw: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(apps_manager, "_write_approved_grants", _full)
    result = apps_manager.enable_app(_APP, grants_consent={"agentTools": [_entries(block=_TWO)[0]]})
    assert not result.ok
    assert result.error_code == "agent_tools_not_saved"
    assert path.read_bytes() == before_revoked
    assert _revoked_names() == frozenset(names)
    assert _stored() == before_approved
    assert _names() == ["fake_app_list"]


@pytest.mark.parametrize("turn_on", [False, True])
def test_a_failed_revoked_record_restore_still_reports_not_saved(
    real_home, monkeypatch, caplog, turn_on
):
    from kiro_crew.apps import protected_app_state

    _install(approve=not turn_on)
    if turn_on:
        protected_app_state.update_revoked_agent_tools(_APP, add=frozenset({"fake_app_note"}))
    before_approved = _stored()

    def _full(*_a: Any, **_kw: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(apps_manager, "_write_approved_grants", _full)
    monkeypatch.setattr(protected_app_state, "_write_revoked_text", _full)
    result = (
        apps_manager.enable_app(_APP, grants_consent={"agentTools": _entries()})
        if turn_on
        else apps_manager.revoke_agent_tools(_APP, _entries())
    )
    assert not result.ok
    assert result.error_code == "agent_tools_not_saved"
    assert _stored() == before_approved
    assert "Could not restore an app's revoked agent tools" in caplog.text


def test_a_revoked_tool_comes_back_only_through_the_approval(real_home):
    from kiro_crew.apps.manager import enable_app, revoke_agent_tools

    _install(block=_TWO)
    note, listed = _entries(block=_TWO)
    assert revoke_agent_tools(_APP, [note]).ok
    assert _names() == ["fake_app_list"]
    # Approving another app's or a made-up entry brings nothing back.
    assert enable_app(_APP, grants_consent={"agentTools": ["fake_app_note@000000000000"]}).ok
    assert _names() == ["fake_app_list"]
    assert enable_app(_APP, grants_consent={"agentTools": [note]}).ok
    assert _names() == ["fake_app_note", "fake_app_list"]

    # The manifest changed the tool since the card was drawn: its old entry approves nothing.
    assert revoke_agent_tools(_APP, [note]).ok
    changed = _block(
        _tool(description="Post one note, then email the owner."), _tool("fake_app_list")
    )
    (apps_manager.app_dir(_APP) / "app.json").write_text(
        json.dumps(_manifest(block=changed)), encoding="utf-8"
    )
    assert enable_app(_APP, grants_consent={"agentTools": [note]}).ok
    assert _names() == ["fake_app_list"]
    stored = manager_records._read_approved_grants(_APP) or {}
    assert stored["agentTools"] == [listed]


def _revoked_names() -> frozenset[str]:
    from kiro_crew.apps import protected_app_state

    return protected_app_state.read_revoked_agent_tools(_APP)


def _grant() -> dict[str, list[str]]:
    row = apps_manager.get_app(_APP)["agentToolsGrant"]
    return {key: [t["name"] for t in row[key]] for key in ("approved", "staged", "revoked")}


def test_a_revoked_tool_stays_off_across_updates_until_the_owner_turns_it_on(real_home, tmp_path):
    from kiro_crew.apps.manager import enable_app, install_app, revoke_agent_tools, update_app

    v1 = _block(_tool(), _tool("fake_app_list"))
    assert install_app(_source(tmp_path, v1, "1.0.0"), agent_tools_consent=_entries(block=v1)).ok
    note = _entries(block=v1)[0]
    assert revoke_agent_tools(_APP, [note]).ok
    assert _revoked_names() == {"fake_app_note"}

    # An update that changes the revoked tool: a new entry, still off and not "new".
    v2 = _block(_tool(description="Post one note, then email the owner."), _tool("fake_app_list"))
    assert update_app(_source(tmp_path, v2, "1.1.0")).ok
    assert _names() == ["fake_app_list"]
    assert _grant() == {"approved": ["fake_app_list"], "staged": [], "revoked": ["fake_app_note"]}

    # An update that adds a tool: the added one waits in the notice.
    v3 = _block(*v2["tools"], _tool("fake_app_tag"))
    assert update_app(_source(tmp_path, v3, "1.2.0")).ok
    assert _grant() == {
        "approved": ["fake_app_list"],
        "staged": ["fake_app_tag"],
        "revoked": ["fake_app_note"],
    }

    # The owner's turn-on of the revoked tool, at its current entry, ends the revoke.
    changed = [e for e in _entries(block=v3) if e.startswith("fake_app_note@")]
    assert enable_app(_APP, grants_consent={"agentTools": changed}).ok
    assert _revoked_names() == frozenset()
    assert _names() == ["fake_app_note", "fake_app_list"]
    assert _grant()["revoked"] == []


def test_an_install_card_never_approves_a_revoked_tool_and_uninstall_clears_the_list(
    real_home, tmp_path
):
    from kiro_crew.apps import protected_app_state
    from kiro_crew.apps.manager import install_app, uninstall_app

    # A revoked list that outlived its install record (the approved set can too).
    protected_app_state.update_revoked_agent_tools(_APP, add=frozenset({"fake_app_note"}))
    block = _block(_tool(), _tool("fake_app_list"))
    assert install_app(
        _source(tmp_path, block, "1.0.0"), agent_tools_consent=_entries(block=block)
    ).ok
    assert _names() == ["fake_app_list"]
    assert _grant()["revoked"] == ["fake_app_note"]
    assert uninstall_app(_APP).ok
    assert _revoked_names() == frozenset()


def test_an_unreadable_revoked_list_approves_no_card_tool_and_offers_none(real_home, tmp_path):
    from kiro_crew.apps import protected_app_state
    from kiro_crew.apps.manager import install_app

    path = protected_app_state._revoked_tools_path(_APP)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    block = _block(_tool(), _tool("fake_app_list"))
    assert install_app(
        _source(tmp_path, block, "1.0.0"), agent_tools_consent=_entries(block=block)
    ).ok
    assert _names() == []
    assert _grant() == {"approved": [], "staged": [], "revoked": ["fake_app_note", "fake_app_list"]}


def _turn_on_request(**caller: Any) -> web.Request:
    return _revoke_request(None, payload={"grantsConsent": {"agentTools": _entries()}}, **caller)


@pytest.mark.asyncio
@_REFUSED_CALLERS
async def test_an_app_an_agent_or_no_session_cannot_turn_a_revoked_tool_on(
    enable_calls, teammate, caller
):
    from kiro_crew.apps import protected_app_state
    from kiro_crew.apps import routes as app_routes

    _install(approve=[])
    protected_app_state.update_revoked_agent_tools(_APP, add=frozenset({"fake_app_note"}))
    resp = await app_routes.handle_enable_app(_turn_on_request(**caller))
    assert resp.status in (401, 403)
    assert enable_calls == []
    assert _revoked_names() == {"fake_app_note"}


@pytest.mark.asyncio
async def test_a_teammate_can_turn_a_revoked_tool_on(real_home, teammate, monkeypatch):
    from kiro_crew.apps import protected_app_state
    from kiro_crew.apps import routes as app_routes
    from kiro_crew.apps.manager_records import AppResult

    monkeypatch.setattr(type(make_mocked_request("POST", "/")), "can_read_body", True)
    real_enable = apps_manager.enable_app

    def _enable(*a: Any, **kw: Any) -> Any:
        # The real approval, then a stop: the rest of the route starts the app's
        # backend, which this test has none of.
        assert real_enable(*a, **kw).ok
        return AppResult(ok=False, name=_APP, error="stop here")

    monkeypatch.setattr(app_routes, "enable_app", _enable)
    _install(approve=[])
    protected_app_state.update_revoked_agent_tools(_APP, add=frozenset({"fake_app_note"}))
    await app_routes.handle_enable_app(_turn_on_request(user="U_TEAMMATE"))
    assert _revoked_names() == frozenset()
    assert _names() == ["fake_app_note"]
