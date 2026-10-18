"""Memory, knowledge, dashboard, heartbeat, registry, publish and watchdog fields load and persist.

Memory backups, the episodic cap and the persistence and injection toggles;
knowledge auto-ingest and the pool's idle TTL; the dashboard's Tailscale opt-in,
widget density, default memory mode, GitLab host allowlist, theme and onboarding
flags; registry branches and publish relocation roots; the heartbeat delivery
default; and the watchdog fields with their per-agent overrides, the retired
orchestrator section and the provider factory's crew identity. Each hydrates from
config.json, falls back on junk and survives a save.
"""

from __future__ import annotations

import json
import threading
import unittest.mock
from pathlib import Path

import pytest
from config_loader_helpers import _load_from_dict, _load_from_dict_with_logs

import kiro_crew.config.loader as loader_module
from kiro_crew.config.loader import DashboardConfig, KiroCrewConfig, WatchdogConfig


@pytest.mark.parametrize("enabled,keep", [(False, 30), (True, 2)])
def test_memory_backup_controls_survive_load_and_save(tmp_path, monkeypatch, enabled, keep):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"memory": {"backup_enabled": enabled, "backup_keep": keep}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader_module, "config_path", lambda: path)
    config = KiroCrewConfig.load()
    assert config.memory.backup_enabled is enabled
    assert config.memory.backup_keep == keep
    config.save()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["memory"]["backup_enabled"] is enabled
    assert saved["memory"]["backup_keep"] == keep


@pytest.mark.parametrize(
    ("memory_data", "expected"),
    [
        pytest.param({}, True, id="missing-defaults-on"),
        pytest.param({"backup_enabled": False}, False, id="boolean-false"),
        pytest.param({"backup_enabled": True}, True, id="boolean-true"),
        pytest.param({"backup_enabled": 0}, True, id="integer-zero-is-invalid"),
        pytest.param({"backup_enabled": ""}, True, id="empty-string-is-invalid"),
        pytest.param({"backup_enabled": "false"}, True, id="string-false-is-invalid"),
        pytest.param({"backup_enabled": [False]}, True, id="list-is-invalid"),
    ],
)
def test_memory_backup_enabled_requires_json_boolean_without_jsonschema(
    tmp_path, monkeypatch, memory_data, expected
):
    """The shipped runtime still enforces the config field's bool contract."""
    from kiro_crew.config import validation

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"memory": memory_data}), encoding="utf-8")
    monkeypatch.setattr(validation, "_HAS_JSONSCHEMA", False)
    monkeypatch.setattr(loader_module, "config_path", lambda: path)
    monkeypatch.setattr(loader_module, "config_local_path", lambda: tmp_path / "missing.local.json")

    config = KiroCrewConfig.load()

    assert config.memory.backup_enabled is expected


def test_episodic_max_count_is_floored_at_one() -> None:
    """A zero or negative V1 episodic cap would refuse every merge-only write, even
    on an empty store, so the loader clamps it to 1. A positive value is kept as
    written."""
    assert _load_from_dict({"memory": {"episodic_max_count": 0}}).memory.episodic_max_count == 1
    assert _load_from_dict({"memory": {"episodic_max_count": -5}}).memory.episodic_max_count == 1
    assert _load_from_dict({"memory": {"episodic_max_count": 7}}).memory.episodic_max_count == 7


def test_dashboard_tailscale_hydrates_and_survives_a_round_trip() -> None:
    """The opt-in must survive ``load()`` and a later ``save()``.

    ``DashboardConfig`` is built field-by-field in ``load()``, so a nested
    section that nobody wires up is silently dropped: the documented
    ``kirocrew config set dashboard.tailscale.enabled true`` would land in
    config.json, read back as ``False``, and — because ``to_dict()`` re-serializes
    the default — be rewritten to ``false`` by the next unrelated ``save()``.
    That makes the whole feature inert while looking configured, so both halves
    are pinned here: hydration, and the round trip that would erase it.
    """
    assert KiroCrewConfig().dashboard.tailscale.enabled is False
    assert _load_from_dict({}).dashboard.tailscale.enabled is False

    enabled = _load_from_dict({"dashboard": {"tailscale": {"enabled": True}}})
    assert enabled.dashboard.tailscale.enabled is True

    # A save() built from the loaded config must not drop the operator's value.
    assert _load_from_dict(enabled.to_dict()).dashboard.tailscale.enabled is True

    # A malformed section degrades to the default instead of raising.
    for bad in ("yes", 1, [], None):
        assert _load_from_dict({"dashboard": {"tailscale": bad}}).dashboard.tailscale.enabled is (
            False
        ), bad
    assert (
        _load_from_dict(
            {"dashboard": {"tailscale": {"enabled": "true"}}}
        ).dashboard.tailscale.enabled
        is False
    )


def test_registry_branchless_legacy_entry_preserves_mainline():
    # Regression: URL registries changed new-entry branch default to "main",
    # but a legacy config entry that OMITS "branch" relied on the historical
    # "mainline" default. Silently retargeting it to "main" on upgrade would
    # break registries whose content still lives on "mainline". A branchless
    # entry must load as "mainline"; an explicit branch is honored verbatim.
    loaded = _load_from_dict(
        {
            "registries": [
                {"name": "legacy", "repo": "https://example.com/org/legacy.git"},
                {
                    "name": "explicit",
                    "repo": "https://example.com/org/new.git",
                    "branch": "main",
                },
            ]
        }
    )
    by_name = {r.name: r for r in loaded.registries}
    assert by_name["legacy"].branch == "mainline"
    assert by_name["explicit"].branch == "main"


def test_publish_relocate_roots_parsed_and_round_trips():
    # Regression (PR #14 alice): publish.relocate_roots was declared + consumed by
    # the relocate handler but NOT parsed in from_dict, so an operator value was
    # silently dropped (permanently []) and lost on round-trip.
    loaded = _load_from_dict(
        {
            "publish": {
                "allowed_destinations": ["provider-a"],
                "relocate_roots": ["/srv/shared", "  "],
            }
        }
    )
    # Parsed (blank entries filtered), not ignored.
    assert loaded.publish.relocate_roots == ["/srv/shared"]
    assert loaded.publish.allowed_destinations == ["provider-a"]
    # Survives a to_dict() -> load() round-trip.
    reloaded = _load_from_dict(loaded.to_dict())
    assert reloaded.publish.relocate_roots == ["/srv/shared"]


class TestWidgetDensityRoundTrip:
    """Tests for dashboard.widget_density persistence."""

    def test_widget_density_defaults_to_more(self) -> None:
        cfg = _load_from_dict({})
        assert cfg.dashboard.widget_density == "more"

    def test_widget_density_loaded_from_config(self) -> None:
        cfg = _load_from_dict({"dashboard": {"widget_density": "less"}})
        assert cfg.dashboard.widget_density == "less"

    def test_widget_density_survives_save_load(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        cfg = _load_from_dict({"dashboard": {"widget_density": "less"}})
        cfg_file = tmp_path / "config.json"
        with patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            cfg.save()
            loaded = KiroCrewConfig.load()
        assert loaded.dashboard.widget_density == "less"


class TestDefaultMemoryModeRoundTrip:
    """Tests for dashboard.default_memory_mode persistence."""

    def test_defaults_to_persistent(self) -> None:
        cfg = _load_from_dict({})
        assert cfg.dashboard.default_memory_mode == "persistent"

    def test_loads_from_config(self) -> None:
        cfg = _load_from_dict({"dashboard": {"default_memory_mode": "incognito"}})
        assert cfg.dashboard.default_memory_mode == "incognito"

    def test_invalid_value_falls_back_to_temporary(self) -> None:
        cfg = _load_from_dict({"dashboard": {"default_memory_mode": "surprise"}})
        assert cfg.dashboard.default_memory_mode == "temporary"

    def test_malformed_dashboard_section_falls_back_to_temporary(self) -> None:
        cfg = _load_from_dict({"dashboard": "not-an-object"})
        assert cfg.dashboard.default_memory_mode == "temporary"

    def test_unreadable_config_falls_back_to_temporary(self) -> None:
        cfg = _load_from_dict("not valid json {{{")
        assert cfg.dashboard.default_memory_mode == "temporary"

    def test_invalid_value_fails_closed_before_schema_validation(self) -> None:
        """Schema cleanup must not erase corruption into the Persistent default."""

        def assert_normalized(data: dict) -> dict:
            assert data["dashboard"]["default_memory_mode"] == "temporary"
            return data

        with unittest.mock.patch(
            "kiro_crew.config.loader._validate_config_data",
            side_effect=assert_normalized,
        ):
            cfg = _load_from_dict({"dashboard": {"default_memory_mode": "surprise"}})

        assert cfg.dashboard.default_memory_mode == "temporary"

    def test_locked_write_invalidates_unchanged_fingerprint_cache(self, tmp_path: Path) -> None:
        """A successful PUT-equivalent write must be visible with a coarse fingerprint."""
        from kiro_crew.config.loader import (
            _invalidate_config_cache,
            update_config_locked,
            write_config_atomically,
        )

        cfg_file = tmp_path / "config.json"
        local_file = tmp_path / "config.local.json"
        write_config_atomically(
            cfg_file,
            {
                "agents": {"default": {"kiro_agent": "kirocrew"}},
                "default_agent": "default",
                "workspaces": {"default": {"dir": "~/workspace"}},
                "dashboard": {"default_memory_mode": "incognito"},
            },
        )
        _invalidate_config_cache()
        try:
            with (
                unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
                unittest.mock.patch(
                    "kiro_crew.config.loader.config_local_path", return_value=local_file
                ),
                unittest.mock.patch(
                    "kiro_crew.config.loader._config_fingerprint",
                    return_value=(("coarse-filesystem", 1, 1, 0o600),),
                ),
            ):
                assert KiroCrewConfig.load().dashboard.default_memory_mode == "incognito"

                def select_temporary(data: dict) -> dict:
                    data.setdefault("dashboard", {})["default_memory_mode"] = "temporary"
                    return data

                update_config_locked(
                    cfg_file,
                    mutate=select_temporary,
                    stamp_meta=False,
                )
                assert KiroCrewConfig.load().dashboard.default_memory_mode == "temporary"
        finally:
            _invalidate_config_cache()

    def test_inflight_reader_cannot_restore_stale_cache_after_write(self, tmp_path: Path) -> None:
        """A pre-write read cannot publish after a same-fingerprint write completes."""
        from kiro_crew.config.loader import (
            _invalidate_config_cache,
            update_config_locked,
            write_config_atomically,
        )

        cfg_file = tmp_path / "config.json"
        local_file = tmp_path / "config.local.json"
        write_config_atomically(
            cfg_file,
            {
                "agents": {"default": {"kiro_agent": "kirocrew"}},
                "default_agent": "default",
                "workspaces": {"default": {"dir": "~/workspace"}},
                "dashboard": {"default_memory_mode": "incognito"},
            },
        )
        _invalidate_config_cache()

        real_read_text = Path.read_text
        old_read = threading.Event()
        release_read = threading.Event()
        blocked = {"done": False}
        observed: list[str] = []
        errors: list[BaseException] = []
        reader: threading.Thread | None = None

        def read_then_wait(self_path: Path, *args, **kwargs) -> str:
            content = real_read_text(self_path, *args, **kwargs)
            if self_path == cfg_file and not blocked["done"]:
                blocked["done"] = True
                old_read.set()
                if not release_read.wait(timeout=5):
                    raise TimeoutError("config write did not release the blocked reader")
            return content

        def load_during_write() -> None:
            try:
                observed.append(KiroCrewConfig.load().dashboard.default_memory_mode)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        try:
            with (
                unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
                unittest.mock.patch(
                    "kiro_crew.config.loader.config_local_path", return_value=local_file
                ),
                unittest.mock.patch(
                    "kiro_crew.config.loader._config_fingerprint",
                    return_value=(("coarse-filesystem", 1, 1, 0o600),),
                ),
                unittest.mock.patch.object(Path, "read_text", read_then_wait),
            ):
                reader = threading.Thread(target=load_during_write)
                reader.start()
                assert old_read.wait(timeout=5), "reader did not capture the pre-write config"

                def select_temporary(data: dict) -> dict:
                    data.setdefault("dashboard", {})["default_memory_mode"] = "temporary"
                    return data

                update_config_locked(
                    cfg_file,
                    mutate=select_temporary,
                    stamp_meta=False,
                )
                release_read.set()
                reader.join(timeout=5)
                assert not reader.is_alive(), "blocked config reader did not finish"
                assert not errors
                assert observed == ["incognito"]
                assert KiroCrewConfig.load().dashboard.default_memory_mode == "temporary"
        finally:
            release_read.set()
            if reader is not None:
                reader.join(timeout=5)
            _invalidate_config_cache()

    def test_survives_save_load(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        cfg = _load_from_dict({"dashboard": {"default_memory_mode": "temporary"}})
        cfg_file = tmp_path / "config.json"
        with patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            cfg.save()
            loaded = KiroCrewConfig.load()
        assert loaded.dashboard.default_memory_mode == "temporary"


def test_heartbeat_default_deliver_default_is_slack():
    """Absent config -> backward-compatible 'slack' default."""
    cfg = _load_from_dict({})
    assert cfg.heartbeat.default_deliver == "slack"


def test_heartbeat_default_deliver_accepts_dashboard():
    cfg = _load_from_dict({"heartbeat": {"default_deliver": "dashboard"}})
    assert cfg.heartbeat.default_deliver == "dashboard"


def test_heartbeat_default_deliver_invalid_falls_back_to_slack():
    """Any value outside {slack, dashboard} normalizes to the safe default."""
    cfg = _load_from_dict({"heartbeat": {"default_deliver": "carrier-pigeon"}})
    assert cfg.heartbeat.default_deliver == "slack"


class TestKnowledgeAutoIngest:
    """The auto-add / project-docs / budget / dedup-cadence keys."""

    def test_auto_add_documents_defaults_off(self) -> None:
        # Auto-ingest is opt-in: a config that never mentions the key must not
        # start writing to the Library or spending extraction calls.
        assert _load_from_dict({}).knowledge.auto_add_documents is False

    def test_auto_add_documents_reads_canonical_key(self) -> None:
        cfg = _load_from_dict({"knowledge": {"auto_add_documents": False}})
        assert cfg.knowledge.auto_add_documents is False

    def test_legacy_spelling_is_honoured(self) -> None:
        # Renaming without this would silently revert every existing config to
        # the default on upgrade.
        cfg = _load_from_dict({"knowledge": {"auto_ingest_doc_links": False}})
        assert cfg.knowledge.auto_add_documents is False

    def test_canonical_wins_over_legacy(self) -> None:
        cfg = _load_from_dict(
            {"knowledge": {"auto_add_documents": False, "auto_ingest_doc_links": True}}
        )
        assert cfg.knowledge.auto_add_documents is False

    def test_round_trip_settles_on_the_canonical_key(self) -> None:
        data = _load_from_dict({"knowledge": {"auto_ingest_doc_links": True}}).to_dict()
        assert data["knowledge"]["auto_add_documents"] is True
        assert "auto_ingest_doc_links" not in data["knowledge"]

    def test_artifact_ingest_defaults_off(self) -> None:
        assert _load_from_dict({}).knowledge.auto_ingest_artifacts is False

    def test_an_empty_knowledge_section_leaves_every_auto_path_off(self) -> None:
        # The two arrive through different readers (a legacy-aware helper and an
        # inline .get() call), so one flipped in isolation is a real risk.
        kc = _load_from_dict({"knowledge": {}}).knowledge
        assert (kc.auto_add_documents, kc.auto_ingest_artifacts) == (False, False)

    def test_folder_chunk_budget_default(self) -> None:
        assert _load_from_dict({}).knowledge.folder_ingest_chunk_budget == 300

    def test_folder_chunk_budget_reads_value(self) -> None:
        cfg = _load_from_dict({"knowledge": {"folder_ingest_chunk_budget": 40}})
        assert cfg.knowledge.folder_ingest_chunk_budget == 40

    def test_folder_chunk_budget_zero_is_allowed(self) -> None:
        cfg = _load_from_dict({"knowledge": {"folder_ingest_chunk_budget": 0}})
        assert cfg.knowledge.folder_ingest_chunk_budget == 0

    @pytest.mark.parametrize("bad", [-5, "many", True, None, 1.5])
    def test_folder_chunk_budget_rejects_junk(self, bad: object) -> None:
        cfg = _load_from_dict({"knowledge": {"folder_ingest_chunk_budget": bad}})
        assert cfg.knowledge.folder_ingest_chunk_budget == 300

    def test_dedup_cadence_default(self) -> None:
        assert _load_from_dict({}).knowledge.dedup_every_n_sweeps == 12

    def test_dedup_cadence_zero_disables(self) -> None:
        cfg = _load_from_dict({"knowledge": {"dedup_every_n_sweeps": 0}})
        assert cfg.knowledge.dedup_every_n_sweeps == 0

    @pytest.mark.parametrize("bad", [-1, "often", True])
    def test_dedup_cadence_rejects_junk(self, bad: object) -> None:
        cfg = _load_from_dict({"knowledge": {"dedup_every_n_sweeps": bad}})
        assert cfg.knowledge.dedup_every_n_sweeps == 12

    def test_new_keys_are_dashboard_editable(self) -> None:
        # A key absent from the allowlist is rejected by PATCH /api/config/kirocrew,
        # so its toggle would render and then fail to save.
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        for key in (
            "knowledge.auto_add_documents",
            "knowledge.auto_ingest_artifacts",
            "knowledge.folder_ingest_chunk_budget",
            "knowledge.dedup_every_n_sweeps",
        ):
            assert key in _EDITABLE_CONFIG, key


class TestMemoryPersistenceAndInjectionToggles:
    """``memory.persistence_enabled`` / ``inject_memory`` / ``inject_lessons``:
    default on, override reads, junk falls back to the default (``_safe_bool``
    accepts only real booleans)."""

    def test_defaults_are_on(self) -> None:
        mc = _load_from_dict({}).memory
        assert (mc.persistence_enabled, mc.inject_memory, mc.inject_lessons) == (
            True,
            True,
            True,
        )

    def test_an_empty_memory_section_leaves_every_toggle_on(self) -> None:
        mc = _load_from_dict({"memory": {}}).memory
        assert (mc.persistence_enabled, mc.inject_memory, mc.inject_lessons) == (
            True,
            True,
            True,
        )

    @pytest.mark.parametrize("key", ["persistence_enabled", "inject_memory", "inject_lessons"])
    def test_false_reads_false(self, key: str) -> None:
        mc = _load_from_dict({"memory": {key: False}}).memory
        assert getattr(mc, key) is False

    @pytest.mark.parametrize("key", ["persistence_enabled", "inject_memory", "inject_lessons"])
    @pytest.mark.parametrize("bad", ["false", 0, 1, None, [], {}])
    def test_junk_falls_back_to_on(self, key: str, bad: object) -> None:
        mc = _load_from_dict({"memory": {key: bad}}).memory
        assert getattr(mc, key) is True


class TestKnowledgePoolIdleTtl:
    """``knowledge.pool_idle_ttl_secs`` parsing: default, override, explicit 0,
    and rejection of negative / bool / typed-wrong values back to the default."""

    def test_default_is_300(self) -> None:
        cfg = _load_from_dict({})
        assert cfg.knowledge.pool_idle_ttl_secs == 300

    def test_reads_value(self) -> None:
        cfg = _load_from_dict({"knowledge": {"pool_idle_ttl_secs": 60}})
        assert cfg.knowledge.pool_idle_ttl_secs == 60

    def test_zero_preserved(self) -> None:
        # 0 is a valid explicit "keep warm forever" opt-out, not a fallback.
        cfg = _load_from_dict({"knowledge": {"pool_idle_ttl_secs": 0}})
        assert cfg.knowledge.pool_idle_ttl_secs == 0

    def test_negative_falls_back_to_default(self) -> None:
        cfg = _load_from_dict({"knowledge": {"pool_idle_ttl_secs": -1}})
        assert cfg.knowledge.pool_idle_ttl_secs == 300

    def test_bool_falls_back_to_default(self) -> None:
        # bool is an int subclass; must not read True as a 1s TTL.
        cfg = _load_from_dict({"knowledge": {"pool_idle_ttl_secs": True}})
        assert cfg.knowledge.pool_idle_ttl_secs == 300

    def test_numeric_string_preserves_legacy_coercion(self) -> None:
        cfg = _load_from_dict({"knowledge": {"pool_idle_ttl_secs": "60"}})
        assert cfg.knowledge.pool_idle_ttl_secs == 60

    def test_integral_float_preserves_legacy_coercion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.config.validation as validation

        monkeypatch.setattr(validation, "_HAS_JSONSCHEMA", False)

        cfg = _load_from_dict({"knowledge": {"pool_idle_ttl_secs": 60.0}})
        assert cfg.knowledge.pool_idle_ttl_secs == 60

    def test_non_integral_float_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.config.validation as validation

        monkeypatch.setattr(validation, "_HAS_JSONSCHEMA", False)

        cfg = _load_from_dict({"knowledge": {"pool_idle_ttl_secs": 0.5}})
        assert cfg.knowledge.pool_idle_ttl_secs == 300


class TestOrchestratorWatchdogThemeAreParsed:
    """load() must actually parse the watchdog and dashboard-theme fields from
    config.json. They are advertised in config-baseline.json and served by
    /api/config/schema, and real consumers read them (acp/session_handle_watchdog.py
    .watchdog), but the cls(...) construction passed no watchdog= kwarg and
    DashboardConfig omitted theme_mode/
    theme_color/onboarded — so config values were silently ignored (defaults
    always won) and the server-authoritative theme never round-tripped (the
    onboarding modal re-armed on every gateway restart)."""

    def test_retired_orchestrator_section_loads_and_is_ignored(self) -> None:
        cfg, warnings = _load_from_dict_with_logs(
            {"orchestrator": {"stage_timeout_seconds": 60}, "watchdog": {"check_after_secs": 5}}
        )
        assert not hasattr(cfg, "orchestrator")
        assert cfg.watchdog.check_after_secs == 5.0
        assert "orchestrator" not in cfg.to_dict()
        assert not [w for w in warnings if "orchestrator" in w]

    def test_watchdog_fields_are_parsed(self) -> None:
        cfg = _load_from_dict(
            {"watchdog": {"tool_stall_hard_cap_secs": 61.0, "check_after_secs": 5.0}}
        )
        assert cfg.watchdog.tool_stall_hard_cap_secs == 61.0
        assert cfg.watchdog.check_after_secs == 5.0

    def test_per_agent_watchdog_overrides_are_parsed(self) -> None:
        """agents.*.watchdog_tool_stall_* overrides survive load(); read by
        acp/session_handle_watchdog._load_watchdog_settings for sessions on that agent."""
        cfg = _load_from_dict(
            {
                "agents": {
                    "pr-reviewer": {
                        "kiro_agent": "pr-reviewer-kiro",
                        "watchdog_tool_stall_suspect_secs": 900.0,
                        "watchdog_tool_stall_hard_cap_secs": 1800.0,
                    }
                }
            }
        )
        assert cfg.agents["pr-reviewer"].watchdog_tool_stall_suspect_secs == 900.0
        assert cfg.agents["pr-reviewer"].watchdog_tool_stall_hard_cap_secs == 1800.0

    def test_per_agent_watchdog_overrides_default_to_inherit(self) -> None:
        """An agent entry without overrides parses to 0 (inherit the global)."""
        cfg = _load_from_dict({"agents": {"builder": {"kiro_agent": "kirocrew"}}})
        assert cfg.agents["builder"].watchdog_tool_stall_suspect_secs == 0.0
        assert cfg.agents["builder"].watchdog_tool_stall_hard_cap_secs == 0.0

    def test_per_agent_watchdog_override_junk_and_negative_collapse_to_inherit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """config.json is hand-editable: junk collapses to 0 (inherit) and a
        negative value is clamped to 0 — never an instant-cancel window, never
        a crashed load. jsonschema is disabled so the raw values reach the
        _safe_float guards (with it enabled, validation strips them first)."""
        import kiro_crew.config.validation as validation

        monkeypatch.setattr(validation, "_HAS_JSONSCHEMA", False)

        cfg = _load_from_dict(
            {
                "agents": {
                    "a": {
                        "kiro_agent": "kirocrew",
                        "watchdog_tool_stall_suspect_secs": "junk",
                        "watchdog_tool_stall_hard_cap_secs": -5,
                    }
                }
            }
        )
        assert cfg.agents["a"].watchdog_tool_stall_suspect_secs == 0.0
        assert cfg.agents["a"].watchdog_tool_stall_hard_cap_secs == 0.0

    def test_per_agent_watchdog_overrides_round_trip(self) -> None:
        """to_dict() (what save() writes) carries the overrides, so a save
        from any routine path does not silently drop a hand-written override."""
        cfg = _load_from_dict(
            {
                "agents": {
                    "pr-reviewer": {
                        "kiro_agent": "pr-reviewer-kiro",
                        "watchdog_tool_stall_suspect_secs": 900.0,
                    }
                }
            }
        )
        td = cfg.to_dict()
        assert td["agents"]["pr-reviewer"]["watchdog_tool_stall_suspect_secs"] == 900.0

    def test_factory_resolves_canonical_crew_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The provider factory resolves crew_agent ONCE at provider-creation:
        an explicit kwarg wins verbatim (the dashboard's resolved identity,
        including the authoritative ""), and absent that, a crew-name-passing
        surface (Slack/cron — see _resolve_model_for_agent's surface
        convention) is covered by crew-namespace membership; a non-crew name
        (a kiro template) yields "" so no override can attach to it."""
        import kiro_crew.providers.acp as acp_mod

        captured: list[dict] = []

        class _FakeProvider:
            def __init__(self, **kwargs: object) -> None:
                captured.append(kwargs)

        monkeypatch.setattr(acp_mod, "AcpProvider", _FakeProvider)
        cfg = _load_from_dict({"agents": {"pr-reviewer": {"kiro_agent": "pr-reviewer-kiro"}}})
        factory = cfg.create_provider_factory()

        factory("k1", agent="pr-reviewer-kiro", crew_agent="pr-reviewer")
        factory("k2", agent="pr-reviewer")  # crew-name surface, no kwarg
        factory("k3", agent="pr-reviewer-kiro")  # kiro template, no kwarg
        factory("k4", agent="pr-reviewer-kiro", crew_agent="")  # explicit no-crew

        assert [c["crew_agent"] for c in captured] == ["pr-reviewer", "pr-reviewer", "", ""]

    def test_dashboard_theme_fields_are_parsed(self) -> None:
        cfg = _load_from_dict(
            {
                "dashboard": {
                    "theme_mode": "dark",
                    "theme_color": "#ff0000",
                    "onboarded": True,
                    "import_onboarded": False,
                }
            }
        )
        assert cfg.dashboard.theme_mode == "dark"
        assert cfg.dashboard.theme_color == "#ff0000"
        assert cfg.dashboard.onboarded is True
        assert cfg.dashboard.import_onboarded is False

    def test_missing_import_onboarded_inherits_existing_onboarded(self) -> None:
        cfg = _load_from_dict({"dashboard": {"onboarded": True}})
        assert cfg.dashboard.import_onboarded is True

    def test_import_onboarded_defaults_false_for_new_config(self) -> None:
        assert DashboardConfig().import_onboarded is False
        cfg = _load_from_dict({})
        assert cfg.dashboard.import_onboarded is False

    def test_import_onboarded_round_trips(self) -> None:
        cfg = _load_from_dict({"dashboard": {"import_onboarded": True}})
        assert cfg.dashboard.import_onboarded is True
        assert cfg.to_dict()["dashboard"]["import_onboarded"] is True

    def test_crewmates_onboarded_defaults_false_for_new_config(self) -> None:
        assert DashboardConfig().crewmates_onboarded is False
        cfg = _load_from_dict({})
        assert cfg.dashboard.crewmates_onboarded is False

    def test_crewmates_onboarded_round_trips(self) -> None:
        cfg = _load_from_dict({"dashboard": {"crewmates_onboarded": True}})
        assert cfg.dashboard.crewmates_onboarded is True
        assert cfg.to_dict()["dashboard"]["crewmates_onboarded"] is True

    def test_import_onboarded_string_false_falls_back_without_jsonschema(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.config.validation as validation

        monkeypatch.setattr(validation, "_HAS_JSONSCHEMA", False)

        cfg = _load_from_dict({"dashboard": {"import_onboarded": "false"}})

        assert cfg.dashboard.import_onboarded is False

    def test_invalid_import_onboarded_inherits_onboarded_without_jsonschema(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.config.validation as validation

        monkeypatch.setattr(validation, "_HAS_JSONSCHEMA", False)

        cfg = _load_from_dict({"dashboard": {"onboarded": True, "import_onboarded": 0}})

        assert cfg.dashboard.import_onboarded is True

    def test_absent_sections_use_defaults(self) -> None:
        cfg = _load_from_dict({})
        assert cfg.watchdog.tool_stall_hard_cap_secs == WatchdogConfig().tool_stall_hard_cap_secs
        assert cfg.dashboard.theme_mode == ""
        assert cfg.dashboard.onboarded is False
        assert cfg.dashboard.import_onboarded is False

    def test_bad_values_fall_back_without_crashing(self) -> None:
        cfg = _load_from_dict(
            {
                "watchdog": {"check_after_secs": "junk"},
            }
        )
        assert cfg.watchdog.check_after_secs == 60.0


class TestGitLabHostAllowlist:
    """dashboard.gitlab_hosts authorizes self-managed GitLab instances for the
    Changes panel, so it must fail closed and never sanitize a malformed entry
    into something that still reaches a provider CLI."""

    def test_default_is_empty(self) -> None:
        assert _load_from_dict({}).dashboard.gitlab_hosts == []

    def test_normalizes_case_and_trailing_dot(self) -> None:
        cfg = _load_from_dict(
            {"dashboard": {"gitlab_hosts": ["  GitLab.Acme.Internal.  ", "git.example:8443"]}}
        )
        assert cfg.dashboard.gitlab_hosts == ["gitlab.acme.internal", "git.example:8443"]

    def test_drops_entries_that_are_not_bare_hosts(self) -> None:
        cfg = _load_from_dict(
            {
                "dashboard": {
                    "gitlab_hosts": [
                        "https://gitlab.acme.internal",
                        "gitlab.acme.internal/path",
                        "user@gitlab.acme.internal",
                        "*.acme.internal",
                        "gitlab.acme.internal:99999",
                        "gitlab.acme.internal:0",
                        "",
                        "   ",
                        42,
                        None,
                    ]
                }
            }
        )
        assert cfg.dashboard.gitlab_hosts == []

    def test_normalizes_explicit_default_https_port(self) -> None:
        # The browser URL API drops :443, so a host:443 entry could never match a
        # frontend-normalized URL; store it as the bare host so both sides agree.
        cfg = _load_from_dict(
            {"dashboard": {"gitlab_hosts": ["gitlab.acme.internal:443", "git.example:8443"]}}
        )
        assert cfg.dashboard.gitlab_hosts == ["gitlab.acme.internal", "git.example:8443"]

    def test_drops_entry_with_an_embedded_port(self) -> None:
        # Only the LAST colon is treated as the port separator, so validating the
        # name with a pattern that itself allows a port would let this malformed
        # entry silently authorize "gitlab.example:8443".
        cfg = _load_from_dict(
            {"dashboard": {"gitlab_hosts": ["gitlab.example:8443:443", "a:1:2:3"]}}
        )
        assert cfg.dashboard.gitlab_hosts == []

    def test_drops_malformed_port_instead_of_granting_the_bare_host(self) -> None:
        # A colon with no valid ASCII-digit port is a malformed entry, not a
        # portless host. Before this guard, "gitlab.example:" fell through to the
        # portless branch and authorized the bare host the operator never wrote,
        # and int() coerced "+443"/"1_000"/" 443" into a real port -- each a
        # deny-by-default violation. A genuine ported host in the same list must
        # still be accepted, so the drop is per-entry, not all-or-nothing.
        cfg = _load_from_dict(
            {
                "dashboard": {
                    "gitlab_hosts": [
                        "gitlab.example:",
                        "gitlab.example:+443",
                        "gitlab.example:-1",
                        "gitlab.example:1_000",
                        "gitlab.example: 443",
                        "gitlab.example:8443",
                    ]
                }
            }
        )
        assert cfg.dashboard.gitlab_hosts == ["gitlab.example:8443"]

    def test_canonicalizes_ported_absolute_fqdn(self) -> None:
        # The dot sits mid-string when a port follows, so the port must be split
        # off before trailing dots are stripped or the entry never matches the
        # URL-normalized "gitlab.example:8443".
        cfg = _load_from_dict(
            {"dashboard": {"gitlab_hosts": ["gitlab.example.:8443", "plain.example.:443"]}}
        )
        assert cfg.dashboard.gitlab_hosts == ["gitlab.example:8443", "plain.example"]

    def test_canonicalizes_non_canonical_port_text(self) -> None:
        # A leading-zero port passed validation but was stored verbatim, while
        # both URL APIs normalize to "8443" -- so the entry could never match.
        cfg = _load_from_dict(
            {"dashboard": {"gitlab_hosts": ["git.example:08443", "other.example:0443"]}}
        )
        assert cfg.dashboard.gitlab_hosts == ["git.example:8443", "other.example"]

    def test_drops_gitlab_com_and_duplicates(self) -> None:
        cfg = _load_from_dict(
            {
                "dashboard": {
                    "gitlab_hosts": [
                        "gitlab.com",
                        "www.gitlab.com",
                        "gitlab.acme.internal",
                        "GITLAB.ACME.INTERNAL",
                    ]
                }
            }
        )
        assert cfg.dashboard.gitlab_hosts == ["gitlab.acme.internal"]

    def test_non_list_falls_back_to_empty(self) -> None:
        cfg = _load_from_dict({"dashboard": {"gitlab_hosts": "gitlab.acme.internal"}})
        assert cfg.dashboard.gitlab_hosts == []
