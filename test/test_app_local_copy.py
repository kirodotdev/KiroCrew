"""Make a local copy of a marketplace app: the explicit way to change it.

A marketplace app refuses a folder update (``app_update_market_source``) so its
listing provenance is never overwritten silently. ``make_local_copy`` is the
visible way out: the app becomes the person's own LOCAL app, losing its
``market:`` source and its marketplace trust grant, while its data (the
``data/`` directory, which holds its state, settings and credential files)
is kept. It needs an explicit confirm.
"""

from __future__ import annotations

import json
from dataclasses import replace

from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from app_manager_helpers import _make_app_source

from kiro_crew.apps.manager import (
    _read_installed,
    _write_installed,
    app_data_dir,
    install_app,
    update_app,
)
from kiro_crew.apps.manager_local_copy import make_local_copy

NAME = "test-app"
SOURCE = f"market:acme/{NAME}@3"
SHA = "d" * 64


def _install_market(tmp_path):
    result = install_app(
        _make_app_source(tmp_path / "v1", name=NAME),
        expected_name=NAME,
        market_source=SOURCE,
        source_commit=SHA,
    )
    assert result.ok, result.error
    data = app_data_dir(NAME)
    (data / "state.json").write_text('{"count": 7}')
    (data / "secrets.json").write_text('{"token": "t"}')
    return data


def _grant_market_trust(app_home):
    cfg = app_home / "config.json"
    raw = json.loads(cfg.read_text())
    raw["agent"]["apps_trusted"] = [NAME]
    raw["agent"]["apps_trusted_repositories"] = {NAME: f"market:acme/{NAME}"}
    cfg.write_text(json.dumps(raw))
    return cfg


def test_same_name_needs_an_explicit_confirm(app_home, tmp_path):
    _install_market(tmp_path)
    result = make_local_copy(NAME)
    assert result.ok is False
    assert result.error_code == "app_local_copy_confirm_required"
    assert _read_installed(NAME).source == SOURCE


def test_an_enabled_app_is_refused(app_home, tmp_path):
    _install_market(tmp_path)
    meta = _read_installed(NAME)
    _write_installed(NAME, replace(meta, enabled=True))
    result = make_local_copy(NAME, confirm_replace=True)
    assert result.ok is False
    assert result.error_code == "app_local_copy_disable_first"
    assert _read_installed(NAME).source == SOURCE


def test_only_a_marketplace_app_has_a_local_copy(app_home, tmp_path):
    assert install_app(_make_app_source(tmp_path / "v1", name=NAME)).ok
    result = make_local_copy(NAME, confirm_replace=True)
    assert result.ok is False
    assert result.error_code == "app_local_copy_not_market"


def test_same_name_copy_drops_market_source_and_grant_and_keeps_data(app_home, tmp_path):
    data = _install_market(tmp_path)
    cfg = _grant_market_trust(app_home)

    result = make_local_copy(NAME, confirm_replace=True)

    assert result.ok, result.error
    meta = _read_installed(NAME)
    assert meta.origin == "local"
    assert not meta.source.startswith("market:")
    assert meta.sourceCommit == ""
    agent = json.loads(cfg.read_text())["agent"]
    assert NAME not in agent.get("apps_trusted", [])
    assert NAME not in agent.get("apps_trusted_repositories", {})
    assert (data / "state.json").read_text() == '{"count": 7}'
    assert (data / "secrets.json").read_text() == '{"token": "t"}'


def test_same_name_copy_records_a_source_no_update_can_lose(app_home, tmp_path):
    _install_market(tmp_path)
    assert make_local_copy(NAME, confirm_replace=True).ok
    # Not a path: a default update cannot reach a tree the swap renames away,
    # it is told to name a folder; the lineage stays readable.
    assert _read_installed(NAME).source == f"local-copy:{SOURCE}"
    later = update_app(_make_app_source(tmp_path / "v2", name=NAME, version="1.1.0"))
    assert later.ok, later.error


def test_a_copy_takes_no_folder(app_home, tmp_path):
    # A folder is applied afterwards by the ordinary update of the now-local app.
    _install_market(tmp_path)
    try:
        make_local_copy(NAME, source=tmp_path, confirm_replace=True)  # type: ignore[call-arg]
    except TypeError:
        assert _read_installed(NAME).source == SOURCE
        return
    raise AssertionError("make_local_copy must not take a folder")
