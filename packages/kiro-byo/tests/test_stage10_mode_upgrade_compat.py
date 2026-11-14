"""Stage 10: Mode identity stability and data-only hot-reload contracts."""
from __future__ import annotations

import pytest

from kiro_byo.mode_lifecycle import (
    ModeLifecycleError,
    install_or_update_mode,
    mode_lifecycle_status,
    rollback_custom_mode,
    set_custom_mode_enabled,
)
from kiro_byo.mode_registry import load_mode_registry
from kiro_byo.paths import use_app_home
from kiro_byo.policy_packs import resolve_policy


def _mode(version="1.0.0", *, types=("flash", "deep"), enabled=True, quality=0.2):
    return {
        "id": "aurora", "schema_version": 4, "registry_api": 1,
        "plugin_version": version, "display_name": "Aurora", "enabled": enabled,
        "default_policy": types[0],
        "policies": [
            {"id": tid, "semantics": {"display_name": tid.title()},
             "execution": {"routing": {"quality": quality, "speed": 0.7, "economy": 0.1}}}
            for tid in types
        ],
    }


def test_live_upgrade_cannot_remove_existing_type_ids(tmp_path):
    with use_app_home(tmp_path):
        install_or_update_mode(_mode())
        before = mode_lifecycle_status("aurora")
        with pytest.raises(ModeLifecycleError, match="remove existing Type"):
            install_or_update_mode(_mode("1.1.0", types=("flash",)))
        after = mode_lifecycle_status("aurora")
        assert after["sha256"] == before["sha256"]
        assert after["plugin_version"] == "1.0.0"
        assert resolve_policy("aurora", "deep")[1].id == "deep"


def test_enabled_upgrade_can_add_type_and_refresh_policy_on_next_read(tmp_path):
    with use_app_home(tmp_path):
        install_or_update_mode(_mode())
        existing_pack, old_policy = resolve_policy("aurora", "flash")
        upgrade = _mode("1.1.0", types=("flash", "deep", "new"), quality=0.4)
        install_or_update_mode(upgrade)
        current_pack, new_policy = resolve_policy("aurora", "flash")
        assert existing_pack.plugin_version == "1.0.0"
        assert old_policy.routing.quality == 0.2  # Immutable in-flight view.
        assert current_pack.plugin_version == "1.1.0"
        assert new_policy.routing.quality == 0.4
        assert resolve_policy("aurora", "new")[1].id == "new"
        assert rollback_custom_mode("aurora").plugin_version == "1.0.0"
        assert resolve_policy("aurora", "deep")[1].id == "deep"


def test_explicit_disable_allows_breaking_type_migration(tmp_path):
    with use_app_home(tmp_path):
        install_or_update_mode(_mode())
        set_custom_mode_enabled("aurora", False)
        install_or_update_mode(_mode("1.1.0", types=("flash",), enabled=False))
        assert not load_mode_registry().get("aurora").enabled
        with pytest.raises(Exception, match="not available yet"):
            resolve_policy("aurora", "flash")
        set_custom_mode_enabled("aurora", True)
        assert resolve_policy("aurora", "flash")[1].id == "flash"


def test_builtin_dyna_ignores_user_upgrade_and_remains_executable(tmp_path):
    with use_app_home(tmp_path):
        install_or_update_mode(_mode())
        install_or_update_mode(_mode("1.1.0", quality=0.3))
        registry = load_mode_registry()
        assert registry.get("tiga").type_ids == ("sky", "multi", "power", "glitter")
        assert registry.get("dyna").enabled is True
        assert resolve_policy("dyna", "flash")[1].id == "flash"


def test_four_tiga_types_route_through_shared_harness_without_provider_calls(tmp_path):
    """An execution-plan smoke test, not live model/API or Crew E2E."""
    from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
    from kiro_byo.mode_router import build_execution_plan, route_model

    with use_app_home(tmp_path):
        fast = ModelSpec(
            "test", "Test", "fast", "Test Flash", tool_use=True,
            routing_tiers=("fast", "balanced"), routing_quality=0.67,
            routing_speed=0.98, routing_economy=0.98, reasoning_supported=True,
        )
        strong = ModelSpec(
            "test", "Test", "strong", "Test Pro", tool_use=True,
            routing_tiers=("strong",), routing_quality=0.98,
            routing_speed=0.43, routing_economy=0.35, reasoning_supported=True,
        )
        config = KiroByoConfig(
            (ProviderSpec("test", "Test", "openai-compatible", "https://example.invalid/v1", "", (fast, strong)),),
            mode_pack="tiga", mode_type="multi",
        )
        for type_id in ("sky", "multi", "power", "glitter"):
            decision = route_model(
                config, pack="tiga", policy=type_id,
                prompt="Please analyze and check the code", history=[],
                has_image=False, tools_available=True, tools_required=False,
            )
            assert decision is not None
            assert (decision.pack, decision.policy) == ("tiga", type_id)
            assert build_execution_plan(decision) is not None
        assert route_model(config, pack="dyna", policy="flash", prompt="hello", history=[], has_image=False) is not None
