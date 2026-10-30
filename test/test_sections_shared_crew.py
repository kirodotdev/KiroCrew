"""``dashboard.shared_crew``: defaults, bounds and exposure flags."""

from __future__ import annotations

import pytest

from kiro_crew.config import sections_shared_crew as sc
from kiro_crew.config.loader_builders import _build_dashboard_config
from kiro_crew.config.sections_dashboard import DashboardConfig


@pytest.fixture(autouse=True)
def _shared_crew_preview(_floor_monkeypatch):
    """These cases pin the preview-on reader (``shared_crew.preview``)."""
    _floor_monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")


def test_defaults_keep_every_switch_off():
    cfg = sc.shared_crew_config_from(None)
    assert cfg == sc.SharedCrewConfig()
    assert cfg.admission_enabled is False
    assert cfg.hard_limits is False
    assert cfg.tailnet_direct is False
    assert cfg.local_authorization is False
    assert cfg.containment_enabled is True
    assert (cfg.principal_concurrency, cfg.crew_concurrency, cfg.typing_idle_seconds) == (4, 0, 10)


def test_dashboard_config_carries_the_section_with_defaults():
    assert DashboardConfig().shared_crew == sc.SharedCrewConfig()
    built = _build_dashboard_config(set(), {"shared_crew": {"admission_enabled": True}})
    assert built.shared_crew.admission_enabled is True
    assert _build_dashboard_config(set(), {}).shared_crew == sc.SharedCrewConfig()


@pytest.mark.parametrize(
    ("name", "raw", "want"),
    [
        ("principal_concurrency", 0, 1),
        ("principal_concurrency", 10_000, 256),
        ("crew_concurrency", -5, 0),
        ("crew_concurrency", 5_000, 1024),
        ("typing_idle_seconds", 1, 3),
        ("typing_idle_seconds", 600, 60),
        ("principal_concurrency", "eight", 4),
    ],
)
def test_integers_are_clamped_and_wrong_types_read_as_default(name, raw, want):
    assert getattr(sc.shared_crew_config_from({name: raw}), name) == want


def test_flags_read_booleans_and_ignore_wrong_types():
    cfg = sc.shared_crew_config_from({"admission_enabled": True, "hard_limits": "yes"})
    assert cfg.admission_enabled is True
    assert cfg.hard_limits is False


@pytest.mark.parametrize(
    ("raw", "want"), [(True, True), (False, False), ("true", False), (1, False)]
)
def test_tailnet_direct_turns_on_only_for_literal_true(raw, want, caplog):
    cfg = sc.shared_crew_config_from({"tailnet_direct": raw})
    assert cfg.tailnet_direct is want
    warned = "not true or false" in caplog.text
    assert warned is (not isinstance(raw, bool))


@pytest.mark.parametrize(("raw", "want"), [(False, False), (True, True), ("off", True), (0, True)])
def test_local_authorization_turns_off_only_for_literal_false(raw, want):
    assert sc.shared_crew_config_from({"local_authorization": raw}).local_authorization is want


def test_effective_limits_resolve_auto_and_cap_per_person():
    auto = sc.effective_limits(
        sc.SharedCrewConfig(principal_concurrency=8), auto_crew_concurrency=3
    )
    assert (auto.crew_active, auto.crew_children, auto.principal_active) == (3, 6, 3)
    assert auto.principal_children == min(sc.PRINCIPAL_CHILD_LIMIT, 6)
    assert auto.principal_storage_bytes == sc.PRINCIPAL_STORAGE_MB * 1024 * 1024
    assert auto.enabled is False
    fixed = sc.effective_limits(sc.SharedCrewConfig(crew_concurrency=20), auto_crew_concurrency=0)
    assert (fixed.crew_active, fixed.principal_active) == (20, 4)
    floor = sc.effective_limits(sc.SharedCrewConfig(), auto_crew_concurrency=0)
    assert floor.crew_active == 1
    as_dict = fixed.to_dict()
    assert as_dict["crew_queue"] == sc.CREW_QUEUE_DEPTH
    assert set(as_dict) == set(sc.EffectiveLimits.__dataclass_fields__)


#: The keys whose behaviour is not in this build: their help text says so.
_NOT_ACTIVE = {"tailnet_direct", "local_authorization"}


def test_every_key_needs_a_restart_and_says_whether_it_is_active():
    from dataclasses import fields

    from kiro_crew.config.schema import requires_restart

    assert requires_restart("dashboard.shared_crew")
    for f in fields(sc.SharedCrewConfig):
        assert requires_restart(f"dashboard.shared_crew.{f.name}"), f.name
        not_active = f.metadata["help"].startswith("Not active in this build yet.")
        assert not_active is (f.name in _NOT_ACTIVE), f.name
        assert "Takes effect" not in f.metadata["help"], f.name


@pytest.mark.parametrize("base_present", [False, True])
def test_load_shared_crew_config_merges_local_overlay(tmp_path, monkeypatch, base_present):
    import json

    from kiro_crew.config import loader

    base = tmp_path / "config.json"
    local = tmp_path / "config.local.json"
    monkeypatch.setattr(loader, "config_path", lambda: base)
    monkeypatch.setattr(loader, "config_local_path", lambda: local)
    if base_present:
        base.write_text(
            json.dumps(
                {
                    "dashboard": {
                        "shared_crew": {
                            "admission_enabled": False,
                            "crew_concurrency": 8,
                            "principal_concurrency": 3,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
    local.write_text(
        json.dumps(
            {
                "dashboard": {
                    "shared_crew": {
                        "admission_enabled": True,
                        "crew_concurrency": 1,
                        "hard_limits": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = sc.load_shared_crew_config()
    assert cfg.admission_enabled is True
    assert cfg.hard_limits is True
    assert cfg.crew_concurrency == 1
    assert cfg.principal_concurrency == (3 if base_present else 4)


@pytest.mark.parametrize("content", ["{", "[]", '{"dashboard": {"shared_crew": null}}'])
def test_load_shared_crew_config_refuses_invalid_overlay(tmp_path, monkeypatch, content):
    from kiro_crew.config import loader

    local = tmp_path / "config.local.json"
    monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "absent.json")
    monkeypatch.setattr(loader, "config_local_path", lambda: local)
    local.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        sc.load_shared_crew_config()
