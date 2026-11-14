"""A profile that extends a parent which failed to load is reported as a fallback.

When a profile file cannot be used (unreadable, unparseable, or a broken
``extends``), the store substitutes the fallback profile and records the stem in
``fallback_profile_names()``, which ``policy validate``, ``policy profile`` and
the Security page read so a broken file never looks like a deliberate lockdown.
A child that ``extends`` such a parent is composed over the parent's fallback,
so it too runs on the fallback's controls, but the child's stem was never
recorded: ``policy validate`` printed it as OK.
"""

from __future__ import annotations

import json
import types

import pytest

from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp


@pytest.fixture(autouse=True)
def _standalone_host(_floor_monkeypatch):
    # On the floor stack, not the test's own monkeypatch, so a test's undo() cannot drop it.
    _floor_monkeypatch.setattr(
        ctx_mod, "current_context", lambda: types.SimpleNamespace(governance=None)
    )
    yield
    gp.reset_store()


def _profiles(monkeypatch, tmp_path, files: dict[str, str]):
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    for name, text in files.items():
        (pdir / name).write_text(text, encoding="utf-8")
    monkeypatch.setattr(gp, "_PROFILES_DIR", pdir)
    gp.reset_store()


def test_a_child_of_an_unparseable_parent_is_a_fallback(monkeypatch, tmp_path):
    child = {"name": "cron", "extends": "base", "bind": {"type": "surface", "id": "cron"}}
    _profiles(monkeypatch, tmp_path, {"base.json": "{ not json", "cron.json": json.dumps(child)})

    names = gp.fallback_profile_names()
    assert "base" in names
    assert "cron" in names


def test_a_child_of_a_healthy_parent_is_not_a_fallback(monkeypatch, tmp_path):
    parent = {"name": "base", "tools": {"mode": "allow", "allow": ["read"]}}
    child = {"name": "cron", "extends": "base", "bind": {"type": "surface", "id": "cron"}}
    _profiles(
        monkeypatch,
        tmp_path,
        {"base.json": json.dumps(parent), "cron.json": json.dumps(child)},
    )

    assert gp.fallback_profile_names() == frozenset()
