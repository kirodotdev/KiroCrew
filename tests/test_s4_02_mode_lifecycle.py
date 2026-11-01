"""S4-02: versioned data-only Mode transitions, fail-closed guards, rollback."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_byo.paths import use_app_home
from kiro_byo.policy_packs import PolicyPackError, get_policy_pack, resolve_policy, public_policy_packs
from kiro_byo.mode_lifecycle import (
    ModeLifecycleError, install_or_update_mode, set_custom_mode_enabled,
    rollback_custom_mode, mode_lifecycle_status,
)


def pack(version="1.0.0", *, quality=0.2, enabled=True, mode_id="aurora"):
    return {"schema_version": 4, "id": mode_id, "plugin_version": version,
            "registry_api": 1, "enabled": enabled, "display_name": "Aurora",
            "default_policy": "flash", "policies": [{
                "id": "flash", "semantics": {"display_name": "Flash"},
                "execution": {"routing": {"quality": quality, "speed": 0.7, "economy": 0.1}},
            }]}


def test_install_update_disable_enable_and_rollback(tmp_path):
    with use_app_home(tmp_path):
        original = install_or_update_mode(pack())
        assert original.plugin_version == "1.0.0"
        assert get_policy_pack("aurora").policy("flash").routing.quality == 0.2
        assert mode_lifecycle_status("aurora")["rollback_available"]
        updated = install_or_update_mode(pack("1.1.0", quality=0.25))
        assert updated.plugin_version == "1.1.0"
        assert public_policy_packs()["packs"][-1]["plugin_version"] == "1.1.0"
        assert rollback_custom_mode("aurora").plugin_version == "1.0.0"
        assert not mode_lifecycle_status("aurora")["rollback_available"]
        disabled = set_custom_mode_enabled("aurora", False)
        assert not disabled.enabled
        with pytest.raises(PolicyPackError, match="not available yet"):
            resolve_policy("aurora", "flash")
        assert rollback_custom_mode("aurora").enabled is True
        set_custom_mode_enabled("aurora", False)
        with pytest.raises(ModeLifecycleError, match="implicitly enable"):
            install_or_update_mode(pack("1.2.0", enabled=True))
        enabled = set_custom_mode_enabled("aurora", True)
        assert enabled.enabled
        assert resolve_policy("aurora", "flash")[1].id == "flash"


def test_rollback_initial_install_uninstalls_without_affecting_tiga(tmp_path):
    with use_app_home(tmp_path):
        install_or_update_mode(pack())
        assert rollback_custom_mode("aurora") is None
        assert get_policy_pack("aurora") is None
        assert get_policy_pack("tiga").policy("multi") is not None
        with pytest.raises(ModeLifecycleError, match="no managed rollback"):
            rollback_custom_mode("aurora")


def test_incompatible_versions_and_reserved_modes_never_activate(tmp_path):
    with use_app_home(tmp_path):
        for name in ("tiga", "dyna"):
            with pytest.raises(ModeLifecycleError, match="reserved built-in"):
                install_or_update_mode(pack(mode_id=name))
            with pytest.raises(ModeLifecycleError, match="reserved built-in"):
                set_custom_mode_enabled(name, True)
        wrong = pack()
        wrong["registry_api"] = 999
        with pytest.raises(PolicyPackError, match="incompatible"):
            install_or_update_mode(wrong)
        invalid = pack()
        invalid["plugin_version"] = "2.0.0-alpha"
        with pytest.raises(PolicyPackError, match="plugin_version"):
            install_or_update_mode(invalid)
        assert get_policy_pack("dyna").enabled is True


def test_monotonic_version_checksum_guard_and_manual_edit_protection(tmp_path):
    with use_app_home(tmp_path):
        install_or_update_mode(pack())
        status = mode_lifecycle_status("aurora")
        with pytest.raises(ModeLifecycleError, match="greater"):
            install_or_update_mode(pack())
        with pytest.raises(ModeLifecycleError, match="checksum"):
            install_or_update_mode(pack("1.1.0"), expected_sha256="bad")
        install_or_update_mode(pack("1.1.0"), expected_sha256=status["sha256"])
        file = tmp_path / "policy_packs" / "aurora.json"
        data = json.loads(file.read_text(encoding="utf-8"))
        data["description"] = "manual edit"
        file.write_text(json.dumps(data), encoding="utf-8")
        assert mode_lifecycle_status("aurora")["rollback_conflicted"] is True
        with pytest.raises(ModeLifecycleError, match="manual changes"):
            rollback_custom_mode("aurora")
        assert json.loads(file.read_text(encoding="utf-8"))["description"] == "manual edit"


def test_unmanaged_legacy_packs_still_hot_load(tmp_path):
    with use_app_home(tmp_path):
        directory = tmp_path / "policy_packs"
        directory.mkdir()
        raw = pack()
        raw.pop("plugin_version")
        raw.pop("registry_api")
        (directory / "aurora.json").write_text(json.dumps(raw), encoding="utf-8")
        assert get_policy_pack("aurora").plugin_version == "0.0.0"
        assert get_policy_pack("aurora").registry_api == 1
        install_or_update_mode(pack())
        assert get_policy_pack("aurora").plugin_version == "1.0.0"


def test_failed_swap_never_replaces_original_pack(tmp_path, monkeypatch):
    from kiro_byo import mode_lifecycle
    with use_app_home(tmp_path):
        install_or_update_mode(pack())
        target = tmp_path / "policy_packs" / "aurora.json"
        before = target.read_bytes()
        real = mode_lifecycle._atomic_write
        def interrupted(path, payload):
            if path == target:
                raise OSError("simulated interrupted commit")
            return real(path, payload)
        monkeypatch.setattr(mode_lifecycle, "_atomic_write", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            install_or_update_mode(pack("1.1.0"))
        assert target.read_bytes() == before
        assert get_policy_pack("aurora").plugin_version == "1.0.0"
        # The previous rollback journal survives an interrupted update.
        assert rollback_custom_mode("aurora") is None
