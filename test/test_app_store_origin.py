"""``app_public_id``: which installs keep a public id, read from the install record.

The store origin is decided by the record the install path writes, so these tests go
through ``install_app`` / ``set_app_provenance`` and, end to end, through
``install_from_registry`` itself. No real git or network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kiro_crew.apps import manager, registry, store_origin
from kiro_crew.metrics import feature_usage as fu

STORE_URL = "https://github.com/store-org/weather-app.git"


def _manifest(name: str, version: str = "1.0.0") -> dict:
    return {
        "name": name,
        "version": version,
        "displayName": name,
        "description": "store origin fixture",
        "author": "tester",
    }


def _source(root: Path, name: str, version: str = "1.0.0") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.json").write_text(json.dumps(_manifest(name, version)), encoding="utf-8")
    return root


def _install(tmp_path: Path, name: str, *, registry_id: str | None, url: str = STORE_URL) -> None:
    """Install *name* the way the registry path does; ``None`` installs from a local tree.

    ``registry_id=""`` is the store's own catalog, a non-empty one an owner-configured
    registry. Both run ``install_app`` under the registry coordinate and then record
    provenance, the two writes ``install_from_registry`` makes.
    """
    from kiro_crew.apps.manager_install import registry_source_repository

    source = _source(tmp_path / "src" / name, name)
    if registry_id is None:
        assert manager.install_app(source).ok
        return
    with registry_source_repository(STORE_URL):
        assert manager.install_app(source).ok
    assert manager.set_app_provenance(
        name, source=f"registry:{name}", url=url, registry=registry_id
    )


@pytest.fixture
def labels(monkeypatch):
    """Start from an empty label set; calling it fills the set from disk, as the gateway does."""
    monkeypatch.setattr(fu, "_public_app_ids", {})
    return fu.refresh_public_app_ids


def test_a_store_install_is_its_id(tmp_path, labels):
    _install(tmp_path, "weather-app", registry_id="")
    labels()
    assert fu.app_public_id("weather-app") == "weather-app"


@pytest.mark.parametrize(
    "registry_id",
    [None, "acme-index"],
    ids=["local install", "owner-configured registry"],
)
def test_an_install_from_anywhere_else_is_private(tmp_path, labels, registry_id):
    _install(tmp_path, "weather-app", registry_id=registry_id)
    labels()
    assert fu.app_public_id("weather-app") == "private"


def test_a_registry_record_without_a_source_url_is_private(tmp_path, labels):
    """A record from before provenance was captured cannot show it came from the store."""
    _install(tmp_path, "old-app", registry_id="", url="")
    labels()
    assert fu.app_public_id("old-app") == "private"


def test_a_store_install_in_dev_mode_is_private(tmp_path, labels):
    from dataclasses import replace

    from kiro_crew.apps.manager_records import _read_installed, _write_installed

    _install(tmp_path, "weather-app", registry_id="")
    meta = _read_installed("weather-app")
    assert meta is not None
    _write_installed("weather-app", replace(meta, dev=True))
    labels()
    assert fu.app_public_id("weather-app") == "private"


def test_unknown_names_are_private_and_non_strings_are_other(labels):
    labels()
    assert fu.app_public_id("never-installed") == "private"
    assert fu.app_public_id(None) == "other"
    assert fu.app_public_id(42) == "other"


def test_shipped_builtins_are_their_app_json_name(labels):
    from kiro_crew.apps.discovery import discover_builtin_apps

    labels()
    names = {a["name"] for a in discover_builtin_apps()}
    assert "auto-improvement" in names
    for name in names:
        assert fu.app_public_id(name) == name


def test_a_delisted_app_keeps_its_id(tmp_path, labels, monkeypatch):
    """The id is fixed at install: the catalog is never asked, so a delist changes nothing."""
    _install(tmp_path, "weather-app", registry_id="")

    def _no_catalog(*_a, **_k):
        raise AssertionError("the public id must not read the store catalog")

    monkeypatch.setattr(registry, "get_registry_app", _no_catalog)
    monkeypatch.setattr("kiro_crew.apps.official_catalog.inventory_for_install", _no_catalog)
    labels()
    assert fu.app_public_id("weather-app") == "weather-app"


def test_a_forged_record_only_names_its_own_app(tmp_path, labels, monkeypatch):
    """An edited record can make its own app look like a store install, nothing more.

    The record of a local install is rewritten to the store shape and to point at
    another app. Only the edited app's own directory name becomes public; the name it
    points at stays private, and no catalog is read to decide either.
    """
    from dataclasses import replace

    from kiro_crew.apps import official_catalog
    from kiro_crew.apps.manager_records import _read_installed, _write_installed

    _install(tmp_path, "notes-app", registry_id=None)
    meta = _read_installed("notes-app")
    assert meta is not None
    _write_installed(
        "notes-app",
        replace(
            meta,
            origin="registry",
            source="registry:weather-app",
            sourceUrl=STORE_URL,
            sourceRegistry="",
            dev=False,
        ),
    )

    def _no_catalog(*_a, **_k):
        raise AssertionError("the public id must not read the store catalog")

    monkeypatch.setattr(registry, "get_registry_app", _no_catalog)
    monkeypatch.setattr(official_catalog, "inventory_for_install", _no_catalog)
    monkeypatch.setattr(official_catalog, "cached_entries", _no_catalog)
    labels()
    assert fu.app_public_id("notes-app") == "notes-app"
    assert fu.app_public_id("weather-app") == "private"


def test_a_reinstall_from_a_local_tree_is_private(tmp_path, labels):
    """A new install from elsewhere rewrites the record, so that install is private."""
    _install(tmp_path, "weather-app", registry_id="")
    assert manager.update_app(_source(tmp_path / "local", "weather-app", "2.0.0")).ok
    labels()
    assert fu.app_public_id("weather-app") == "private"


def test_an_uninstalled_store_app_drops_out(tmp_path, labels):
    _install(tmp_path, "weather-app", registry_id="")
    labels()
    assert fu.app_public_id("weather-app") == "weather-app"
    assert manager.uninstall_app("weather-app").ok
    labels()
    assert fu.app_public_id("weather-app") == "private"


def test_a_failed_refresh_keeps_the_last_set(monkeypatch):
    monkeypatch.setattr(fu, "_public_app_ids", {"weather-app": "1.0.0"})

    def _boom() -> dict[str, str]:
        raise OSError("unreadable")

    monkeypatch.setattr(store_origin, "public_app_ids", _boom)
    fu.refresh_public_app_ids()
    assert fu.app_public_id("weather-app") == "weather-app"


def test_an_app_lifecycle_refresh_rebuilds_the_ids(monkeypatch):
    """Boot and every app install, update or uninstall run ``refresh_app_state``."""
    from kiro_crew.apps import builtin_adoption_gate, hidden_sessions
    from kiro_crew.dashboard.state_parts import slots_push

    calls: list[int] = []
    monkeypatch.setattr(fu, "refresh_public_app_ids", lambda: calls.append(1))
    monkeypatch.setattr(hidden_sessions, "refresh_hiding_apps", lambda: None)
    monkeypatch.setattr(builtin_adoption_gate, "refresh_gate_app_trust", lambda: None)
    slots_push.refresh_app_state()
    assert calls == [1]


def test_the_reconciler_tick_does_not_rescan_the_ids(monkeypatch):
    """The tick re-runs the tool gate's publish; that publish leaves the ids alone."""
    from kiro_crew.apps import builtin_adoption_gate

    calls: list[int] = []
    monkeypatch.setattr(fu, "refresh_public_app_ids", lambda: calls.append(1))
    monkeypatch.setattr(store_origin, "public_app_ids", lambda: calls.append(2) or {})
    monkeypatch.setattr("kiro_crew.hooks.set_builtin_app_names", lambda _v: None)
    monkeypatch.setattr("kiro_crew.hooks.set_builtin_app_mcp_servers", lambda _v: None)
    monkeypatch.setattr("kiro_crew.hooks.set_builtin_app_agents", lambda _v: None)
    builtin_adoption_gate._publish()
    assert calls == []


def test_the_installed_counter_closes_via(monkeypatch):
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "kiro_crew.metrics.events.emit_counter", lambda n, a: seen.append((n, dict(a)))
    )
    monkeypatch.setattr(fu, "_public_app_ids", {"weather-app": "1.2.3"})
    fu.record_app_installed("weather-app", "store")
    fu.record_app_installed("weather-app", "default")
    fu.record_app_installed("mine", "a-url")
    v = "entity_version"
    assert seen == [
        (fu.APP_INSTALLED, {"app": "weather-app", "via": "store", v: "1.2.3"}),
        (fu.APP_INSTALLED, {"app": "weather-app", "via": "default", v: "1.2.3"}),
        (fu.APP_INSTALLED, {"app": "private", "via": "other", v: "private"}),
    ]


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.2.3", "1.2.3"),
        ("2026.11.20", "2026.11.20"),
        ("12345.1.1", "other"),
        ("1.2.3-beta", "other"),
        ("1.2.3+build.7", "other"),
        ("1.2", "other"),
        ("", "other"),
    ],
)
def test_the_public_version_keeps_only_plain_numbers(monkeypatch, version, expected):
    monkeypatch.setattr(fu, "_public_app_ids", {"weather-app": version})
    assert fu.app_public_version("weather-app") == expected


def test_a_private_app_version_is_private(monkeypatch):
    monkeypatch.setattr(fu, "_public_app_ids", {"weather-app": "1.2.3"})
    assert fu.app_public_version("mine") == "private"
    assert fu.app_public_version(None) == "other"


def test_the_version_is_read_from_the_install_record(tmp_path, labels):
    _install(tmp_path, "weather-app", registry_id="")
    labels()
    assert fu.app_public_version("weather-app") == "1.0.0"
    from kiro_crew.apps.discovery import discover_builtin_apps

    shipped = {a["name"]: a["version"] for a in discover_builtin_apps()}
    assert fu._public_app_ids["auto-improvement"] == shipped["auto-improvement"]


# -- end to end through install_from_registry ---------------------------------


@pytest.fixture
def _registry_install(monkeypatch, tmp_path):
    """Admit a registry install whose clone 'produces' a local tree."""
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    # No real git runs here, so a runner without git must not refuse the install.
    monkeypatch.setattr(registry, "git_unavailable_refusal", lambda _n: None)
    monkeypatch.setattr(
        "kiro_crew.apps.registry_pipeline.install.git_unavailable_refusal", lambda _n: None
    )
    monkeypatch.setattr("kiro_crew.apps.official_catalog.inventory_for_install", lambda n: None)
    clone = _source(tmp_path / "clone", "weather-app")

    async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
        return {"ok": True, "pkg_dir": clone}

    monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)
    monkeypatch.setattr(
        registry, "_fetch_app_manifest", AsyncMock(return_value=_manifest("weather-app"))
    )

    def _use(entry: dict) -> None:
        monkeypatch.setattr(registry, "get_registry_app", lambda n: entry)

    return _use


@pytest.fixture
def installed_counts(monkeypatch):
    """Every ``kirocrew.app.installed`` point recorded, as ``(app, via)``."""
    seen: list[tuple[object, object]] = []

    def _emit(name, attrs):
        if name == fu.APP_INSTALLED:
            seen.append((attrs.get("app"), attrs.get("via")))

    monkeypatch.setattr("kiro_crew.metrics.events.emit_counter", _emit)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "expected"),
    [({}, "weather-app"), ({"_registry": "acme-index"}, "private")],
    ids=["store catalog row", "owner-configured registry row"],
)
async def test_install_from_registry_end_to_end(
    _registry_install, labels, installed_counts, extra, expected
):
    entry = {"name": "weather-app", "gitUrl": STORE_URL, "repo": STORE_URL, "branch": "main"}
    _registry_install({**entry, **extra})
    result = await registry.install_from_registry("weather-app")
    assert result["ok"] is True, result.get("error")
    assert installed_counts == [(expected, "store")]
    labels()
    assert fu.app_public_id("weather-app") == expected


@pytest.mark.asyncio
async def test_a_default_install_counts_once_and_an_update_does_not(
    _registry_install, labels, installed_counts, monkeypatch
):
    entry = {"name": "weather-app", "gitUrl": STORE_URL, "repo": STORE_URL, "branch": "main"}
    _registry_install(entry)
    first = await registry.install_from_registry("weather-app", via="default")
    assert first["ok"] is True, first.get("error")
    # The update resolves the same row (its pin check is not under test here).
    monkeypatch.setattr(
        "kiro_crew.apps.registry_pipeline.install._resolve_install_entry",
        lambda _n: (dict(entry), ""),
    )
    again = await registry.install_from_registry("weather-app")
    assert again["ok"] is True, again.get("error")
    assert installed_counts == [("weather-app", "default")]


@pytest.fixture
def lifecycle_counts(monkeypatch):
    """Every ``kirocrew.app.*`` lifecycle point recorded, as ``(name, attrs)``."""
    seen: list[tuple[str, dict]] = []
    names = (fu.APP_INSTALLED, fu.APP_UPDATED, fu.APP_UNINSTALLED)

    def _emit(name, attrs):
        if name in names:
            seen.append((name, dict(attrs)))

    monkeypatch.setattr("kiro_crew.metrics.events.emit_counter", _emit)
    return seen


@pytest.mark.asyncio
async def test_an_update_counts_one_updated_with_the_new_version(
    _registry_install, labels, lifecycle_counts, monkeypatch, tmp_path
):
    entry = {"name": "weather-app", "gitUrl": STORE_URL, "repo": STORE_URL, "branch": "main"}
    _registry_install(entry)
    assert (await registry.install_from_registry("weather-app"))["ok"] is True
    monkeypatch.setattr(
        "kiro_crew.apps.registry_pipeline.install._resolve_install_entry",
        lambda _n: (dict(entry), ""),
    )
    _source(tmp_path / "clone", "weather-app", "1.1.0")
    again = await registry.install_from_registry("weather-app")
    assert again["ok"] is True, again.get("error")
    app = {"app": "weather-app", "via": "store"}
    assert lifecycle_counts == [
        (fu.APP_INSTALLED, {**app, "entity_version": "1.0.0"}),
        (fu.APP_UPDATED, {**app, "entity_version": "1.1.0"}),
    ]


@pytest.mark.asyncio
async def test_an_older_concurrent_refresh_does_not_relabel_the_update(
    labels, lifecycle_counts, monkeypatch
):
    """Another app's older scan lands on the shared map while this count waits."""
    monkeypatch.setattr(store_origin, "public_app_ids", lambda: {"weather-app": "1.1.0"})
    real_to_thread = asyncio.to_thread

    async def _to_thread_then_stale_refresh(func, *args, **kwargs):
        result = await real_to_thread(func, *args, **kwargs)
        monkeypatch.setattr(fu, "_public_app_ids", {"weather-app": "1.0.0"})
        return result

    monkeypatch.setattr(store_origin.asyncio, "to_thread", _to_thread_then_stale_refresh)
    await store_origin.count_install("weather-app", "store", was_installed=True)
    assert lifecycle_counts == [
        (fu.APP_UPDATED, {"app": "weather-app", "via": "store", "entity_version": "1.1.0"})
    ]


@pytest.mark.parametrize("keep_data", [True, False])
def test_an_uninstall_counts_once_with_the_removed_version(
    tmp_path, labels, lifecycle_counts, keep_data
):
    _install(tmp_path, "weather-app", registry_id="")
    labels()
    assert manager.uninstall_app("weather-app", keep_data=keep_data).ok
    assert lifecycle_counts == [
        (
            fu.APP_UNINSTALLED,
            {"app": "weather-app", "entity_version": "1.0.0", "keep_data": str(keep_data).lower()},
        )
    ]


def test_an_uninstall_in_a_process_that_never_filled_the_map_names_the_app(
    tmp_path, labels, lifecycle_counts
):
    """A CLI uninstall: nothing filled the map, so the uninstall reads the record itself."""
    _install(tmp_path, "weather-app", registry_id="")
    assert fu._public_app_ids == {}
    assert manager.uninstall_app("weather-app").ok
    assert lifecycle_counts == [
        (
            fu.APP_UNINSTALLED,
            {"app": "weather-app", "entity_version": "1.0.0", "keep_data": "true"},
        )
    ]


def test_a_failed_uninstall_is_not_counted(labels, lifecycle_counts):
    labels()
    assert not manager.uninstall_app("never-installed").ok
    assert lifecycle_counts == []


@pytest.mark.asyncio
async def test_a_raising_recorder_leaves_install_and_uninstall_results_unchanged(
    _registry_install, labels, monkeypatch, tmp_path
):
    def _boom(*_a, **_k):
        raise RuntimeError("recorder down")

    for name in ("record_app_installed", "record_app_updated", "record_app_uninstalled"):
        monkeypatch.setattr(fu, name, _boom)
    entry = {"name": "weather-app", "gitUrl": STORE_URL, "repo": STORE_URL, "branch": "main"}
    _registry_install(entry)
    first = await registry.install_from_registry("weather-app")
    assert first["ok"] is True, first.get("error")
    monkeypatch.setattr(
        "kiro_crew.apps.registry_pipeline.install._resolve_install_entry",
        lambda _n: (dict(entry), ""),
    )
    again = await registry.install_from_registry("weather-app")
    assert again["ok"] is True, again.get("error")
    gone = manager.uninstall_app("weather-app")
    assert gone.ok, gone.error
    assert manager.get_app("weather-app") is None


def test_a_local_install_is_not_counted_and_is_private(tmp_path, labels, installed_counts):
    """A local folder never goes through the registry install, and its label is private."""
    _install(tmp_path, "weather-app", registry_id=None)
    labels()
    assert installed_counts == []
    fu.record_app_installed("weather-app", "store")
    assert installed_counts == [("private", "store")]
