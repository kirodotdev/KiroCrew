"""The config load pipeline, tested at its three stages and at the defaults reader.

``KiroCrewConfig.load()`` runs ``read_config_document`` (the only reader of the
config files and of the validated-data cache), ``build_config`` (the document
into a config, with no filesystem I/O) and ``persist_write_back`` (the write-back
migration a loaded document makes due). The section builders read every default
through ``sections.SectionReader``, so an absent key and an absent file agree.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import types
from pathlib import Path

import pytest

from kiro_crew.config import loader, sections

_LOGGER = "kiro_crew.config.loader"
_REAL_CONFIG = loader.KiroCrewConfig

#: Section fields whose empty-section build differs from the bare dataclass ON
#: PURPOSE, although both start from the field default. Each value is the reason;
#: every other field of every section must agree. The omitted-key reads that keep
#: their own default are pinned by name below.
_DECLARED_EXCEPTIONS = {
    # The loader folds the platform default (True on Windows) into the value so a
    # full-document save cannot turn "never decided" into a declared lockdown; the
    # dataclass default stays platform-independent for the schema snapshot.
    ("agent", "sandbox_allow_unsandboxed_exec"): "platform default resolved at load",
    # Passed through the selectable-backend gate, which reads the build's registry.
    ("agent", "member_acp_backend"): "normalized against the selectable backends",
}


def _document(tmp_path: Path, data: dict, *, loaded: bool = True) -> loader.ConfigDocument:
    return loader.ConfigDocument(
        ticket=0,
        path=tmp_path / "config.json",
        data=data,
        loaded=loaded,
        content_digest=None,
    )


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the loader's path seams at ``tmp_path``; return the home."""
    monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(loader, "config_local_path", lambda: tmp_path / "config.local.json")
    monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
    return tmp_path


def _write(path: Path, document: object) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def _section_fields() -> list[tuple[str, type]]:
    cfg = loader.KiroCrewConfig()
    return [
        (f.name, type(getattr(cfg, f.name)))
        for f in dataclasses.fields(loader.KiroCrewConfig)
        if not f.name.startswith("_") and dataclasses.is_dataclass(getattr(cfg, f.name))
    ]


# ---------------------------------------------------------------------------
# SectionReader: the one place a section default is resolved.
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Sample:
    count: int = 3
    label: str = "fallback"
    names: list = dataclasses.field(default_factory=lambda: ["seed"])


class TestSectionReader:
    def test_an_absent_key_reads_the_declared_default(self):
        reader = sections.SectionReader(_Sample, {})
        assert reader.get("count") == 3
        assert reader.get("label") == "fallback"

    def test_a_present_key_is_returned_as_stored_none_included(self):
        reader = sections.SectionReader(_Sample, {"count": "x", "label": None})
        assert reader.get("count") == "x"
        assert reader.get("label") is None

    def test_a_factory_default_is_fresh_on_every_read(self):
        reader = sections.SectionReader(_Sample, {})
        first, second = reader.get("names"), reader.get("names")
        assert first == second == ["seed"]
        assert first is not second
        first.append("mutated")
        assert reader.default("names") == ["seed"]

    def test_read_hands_the_coercer_the_value_and_the_same_default_as_fallback(self):
        seen: list[tuple] = []

        def coerce(value, fallback, *bounds, **options):
            seen.append((value, fallback, bounds, options))
            return "coerced"

        reader = sections.SectionReader(_Sample, {"label": "stored"})
        assert reader.read("count", coerce, 1, 9, hi=10) == "coerced"
        assert reader.read("label", coerce) == "coerced"
        assert seen == [(3, 3, (1, 9), {"hi": 10}), ("stored", "fallback", (), {})]

    def test_a_key_with_no_field_has_no_default(self):
        reader = sections.SectionReader(_Sample, {"stray": 1})
        with pytest.raises(KeyError):
            reader.get("stray")

    def test_the_safe_coercers_fall_back_to_the_field_default(self):
        reader = sections.SectionReader(_Sample, {"count": "not a number"})
        assert reader.read("count", sections._safe_int) == 3
        # The bounds still apply to that fallback, as they always have.
        assert reader.read("count", sections._safe_int, 5, 9) == 5


# ---------------------------------------------------------------------------
# build_config: the document into a config, without the filesystem.
# ---------------------------------------------------------------------------


class TestBuildConfig:
    @pytest.mark.parametrize("section", [name for name, _ in _section_fields()])
    def test_an_empty_section_builds_the_dataclass_defaults(self, tmp_path, section):
        """An absent key and an absent file give one answer, for every field.

        A builder that restated a default as a literal could drift from its field
        and answer the opposite value only for the installs whose ``config.json``
        predates the key; the bare dataclass is what a home with no file gets.
        """
        built = getattr(loader.build_config(_document(tmp_path, {section: {}})), section)
        bare = getattr(loader.KiroCrewConfig(), section)
        drift = {
            f.name: (getattr(built, f.name), getattr(bare, f.name))
            for f in dataclasses.fields(bare)
            if (section, f.name) not in _DECLARED_EXCEPTIONS
            and getattr(built, f.name) != getattr(bare, f.name)
        }
        assert drift == {}

    @pytest.mark.parametrize("platform_default", [True, False])
    def test_the_declared_exceptions_are_what_they_say(
        self, tmp_path, monkeypatch, platform_default
    ):
        monkeypatch.setattr(loader, "unsandboxed_exec_platform_default", lambda: platform_default)
        agent = loader.build_config(_document(tmp_path, {"agent": {}})).agent
        assert agent.sandbox_allow_unsandboxed_exec is platform_default
        assert agent.member_acp_backend == loader._normalize_acp_backend("kas")
        assert set(_DECLARED_EXCEPTIONS) == {
            ("agent", "sandbox_allow_unsandboxed_exec"),
            ("agent", "member_acp_backend"),
        }
        assert set(_DECLARED_EXCEPTIONS) <= {
            (section, f.name) for section, dto in _section_fields() for f in dataclasses.fields(dto)
        }

    def test_it_performs_no_config_file_io(self, tmp_path, monkeypatch):
        def refuse(*_args, **_kwargs):
            raise AssertionError("build_config reached a config I/O seam")

        for seam in (
            "config_path",
            "config_local_path",
            "config_dir",
            "read_config_text",
            "_config_fingerprint",
            "atomic_write",
            "write_config_atomically",
            "_persist_config_migration",
        ):
            monkeypatch.setattr(loader, seam, refuse)
        cfg = loader.build_config(
            _document(
                tmp_path,
                {
                    "agent": {"model": "fixture-model", "max_subagents": 2},
                    "workspaces": {"default": "~/ws"},
                    "dashboard": {"theme_mode": "dark"},
                },
            )
        )
        assert cfg.agent.model == "fixture-model"
        assert cfg.agent.max_subagents == 2
        assert cfg.workspaces["default"].dir == "~/ws"
        assert cfg.dashboard.theme_mode == "dark"
        assert list(tmp_path.iterdir()) == []

    def test_the_write_back_is_not_part_of_the_build(self, tmp_path):
        cfg = loader.build_config(_document(tmp_path, {"workspaces": {"default": "~/ws"}}))
        assert cfg.agents == {}
        assert cfg.default_agent == ""

    def test_an_unloaded_document_builds_defaults_and_the_default_crew(self, tmp_path):
        cfg = loader.build_config(_document(tmp_path, {}, loaded=False))
        assert cfg.default_agent == "default"
        assert cfg.agents["default"].kiro_agent == (cfg.agent.default_agent or "kirocrew")
        assert cfg._base_unreadable is False
        assert cfg.agent == loader.KiroCrewConfig().agent
        assert cfg.skills.project_skills_enabled is True

    def test_an_unreadable_file_keeps_the_project_skills_switch_off(self, config_home):
        """The read leaves the off-switch in ``data``; the unloaded build honours it."""
        (config_home / "config.json").write_text("{broken", encoding="utf-8")
        doc = loader.read_config_document()
        assert doc.loaded is False
        assert doc.base_unreadable is True
        assert doc.data == {"skills": {"project_skills_enabled": False}}
        cfg = loader.build_config(doc)
        assert cfg._base_unreadable is True
        assert cfg.skills.project_skills_enabled is False

    def test_the_inline_entry_reads_take_their_field_defaults(self, tmp_path):
        cfg = loader.build_config(
            _document(
                tmp_path,
                {
                    "agents": {"crew": {}},
                    "memory_stores": {"store": {}},
                    "registries": [{"repo": "fixture/repo"}],
                    "dashboard": {"jira_auth": [{"host": "jira.example"}]},
                },
            )
        )
        bare = loader.KiroCrewConfig()
        assert cfg.agents["crew"] == loader.KiroCrewAgentConfig()
        assert cfg.memory_stores["store"] == loader.MemoryStoreConfig()
        assert cfg.dashboard.jira_auth == [loader.JiraAuthEntry(host="jira.example")]
        for name in (
            "default_workspace",
            "default_memory_store",
            "timezone",
            "snapshot_dir",
            "connections_ui",
            "auto_update",
            "slack_dm_activation",
            "observe_max_messages",
            "observe_ttl_hours",
            "hooks",
        ):
            assert getattr(cfg, name) == getattr(bare, name), name
        # The one entry read whose default is deliberately not the field's.
        assert cfg.registries == [
            loader.ExternalRegistryConfig(repo="fixture/repo", branch="mainline")
        ]

    def test_an_omitted_pool_size_reads_the_constant_at_call_time(self, tmp_path, monkeypatch):
        monkeypatch.setattr(loader, "DEFAULT_POOL_SIZE", 3)
        assert loader.build_config(_document(tmp_path, {"session": {}})).session.pool_size == 3

    def test_a_malformed_section_is_reported_and_built_from_defaults(self, tmp_path, caplog):
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        cfg = loader.build_config(
            _document(
                tmp_path, {"memory": "oops", "dashboard": {"default_memory_mode": "persistent"}}
            )
        )
        assert "memory" in cfg.degraded_sections
        assert cfg.memory == loader.KiroCrewConfig().memory
        assert any(
            "'memory' section is not a JSON object" in r.getMessage()
            for r in caplog.records
            if r.name == _LOGGER
        )

    def test_a_degraded_dashboard_section_forces_temporary_memory_mode(self, tmp_path):
        cfg = loader.build_config(_document(tmp_path, {"dashboard": "oops"}))
        assert "dashboard" in cfg.degraded_sections
        assert cfg.dashboard.default_memory_mode == "temporary"

    def test_a_rebound_module_config_class_neither_breaks_nor_steers_a_build(
        self, tmp_path, monkeypatch
    ):
        """Tests rebind ``loader.KiroCrewConfig``; the build never reads that global."""
        monkeypatch.setattr(loader, "KiroCrewConfig", types.SimpleNamespace(load=lambda: None))
        cfg = loader.build_config(_document(tmp_path, {}), _REAL_CONFIG)
        assert type(cfg) is _REAL_CONFIG
        assert (cfg.timezone, cfg.default_workspace, cfg.observe_max_messages) == (
            "",
            "default",
            200,
        )

    def test_the_sticky_degraded_set_is_an_input_of_the_build(self, tmp_path):
        data = {"dashboard": {"default_memory_mode": "persistent"}}
        before = loader.build_config(_document(tmp_path, json.loads(json.dumps(data))))
        assert before.dashboard.default_memory_mode == "persistent"
        assert loader.DEGRADED_WHOLE_CONFIG not in before.degraded_sections
        loader._mark_file_degraded(tmp_path / "config.json")
        after = loader.build_config(_document(tmp_path, json.loads(json.dumps(data))))
        assert after.dashboard.default_memory_mode == "temporary"
        assert loader.DEGRADED_WHOLE_CONFIG in after.degraded_sections

    def test_unknown_keys_are_captured_from_the_base_view(self, tmp_path):
        doc = _document(tmp_path, {"edition_thing": {"x": 1}, "agent": {"no_such_key": 2}})
        doc.base_shadow = {"edition_thing": {"x": "base"}}
        cfg = loader.build_config(doc)
        assert cfg._extra_sections == {"edition_thing": {"x": "base"}}
        assert cfg._extra_keys == {"agent": {"no_such_key": 2}}


# ---------------------------------------------------------------------------
# read_config_document: the files (or the cache) into a document.
# ---------------------------------------------------------------------------


class TestReadConfigDocument:
    def test_no_files_reads_an_unloaded_document_and_creates_nothing(self, config_home):
        doc = loader.read_config_document()
        assert doc.loaded is False
        assert doc.data == {}
        assert doc.content_digest == loader.config_content_stamp()
        assert list(config_home.iterdir()) == []

    def test_an_unparseable_base_alone_is_unloaded_and_unreadable(self, config_home):
        (config_home / "config.json").write_text("{broken", encoding="utf-8")
        doc = loader.read_config_document()
        assert doc.loaded is False
        assert doc.base_unreadable is True
        assert doc.content_digest == loader.config_content_stamp()

    def test_the_overlay_merges_and_the_base_copy_of_what_it_touched_is_kept(self, config_home):
        _write(config_home / "config.json", {"agent": {"model": "base", "streaming": False}})
        _write(config_home / "config.local.json", {"agent": {"model": "overlay"}})
        doc = loader.read_config_document()
        assert doc.loaded is True
        assert doc.data["agent"]["model"] == "overlay"
        assert doc.data["agent"]["streaming"] is False
        assert doc.overlay == {"agent": {"model": "overlay"}}
        assert doc.base_shadow == {"agent": {"model": "base", "streaming": False}}

    def test_only_a_disk_read_finds_an_adoptable_default(self, config_home, monkeypatch):
        _write(config_home / "config.json", {"agent": {"subagent_timeout_secs": 1800}})
        stats: list[tuple] = []
        real = loader._config_fingerprint

        def counting():
            stats.append(())
            return real()

        monkeypatch.setattr(loader, "_config_fingerprint", counting)
        first = loader.read_config_document()
        second = loader.read_config_document()
        assert [e.dotted_key for e in first.adoptable] == ["agent.subagent_timeout_secs"]
        assert second.adoptable == []
        assert second.data == first.data
        assert second.content_digest == first.content_digest
        assert second.ticket > first.ticket
        assert len(stats) == 2


# ---------------------------------------------------------------------------
# persist_write_back and the whole load.
# ---------------------------------------------------------------------------


class TestPersistWriteBack:
    def test_a_document_without_a_crew_is_migrated_in_memory_and_on_disk(self, config_home):
        _write(config_home / "config.json", {"workspaces": {"default": {"dir": "~/ws"}}})
        doc = loader.read_config_document()
        cfg = loader.build_config(doc)
        assert cfg.agents == {}
        loader.persist_write_back(cfg, doc)
        assert cfg.default_agent == "default"
        assert cfg.agents["default"].kiro_agent == "kirocrew"
        stored = json.loads((config_home / "config.json").read_text(encoding="utf-8"))
        assert stored["agents"]["default"]["kiro_agent"] == "kirocrew"
        assert stored["default_agent"] == "default"
        assert stored["workspaces"] == {"default": {"dir": "~/ws"}}
        assert (config_home / "config.json.bak").is_file()

    def test_a_degraded_load_keeps_the_malformed_bytes(self, config_home, caplog):
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        # A malformed narrowing validation cannot repair, in a document that also
        # lacks its crew: the write-back is due, and must not run.
        original = json.dumps({"publish": {"allowed_destinations": "github"}})
        (config_home / "config.json").write_text(original, encoding="utf-8")
        doc = loader.read_config_document()
        cfg = loader.build_config(doc)
        loader.persist_write_back(cfg, doc)
        assert (config_home / "config.json").read_text(encoding="utf-8") == original
        assert any(
            "skipping write-back migration" in r.getMessage()
            for r in caplog.records
            if r.name == _LOGGER
        )

    def test_a_load_with_no_files_writes_nothing(self, config_home):
        cfg = loader.KiroCrewConfig.load()
        assert cfg.default_agent == "default"
        assert list(config_home.iterdir()) == []

    def test_an_unloaded_document_is_not_written_back(self, config_home):
        doc = loader.read_config_document()
        cfg = loader.build_config(doc)
        loader.persist_write_back(cfg, doc)
        assert list(config_home.iterdir()) == []

    def test_a_subclass_load_reads_the_base_config_top_level_defaults(
        self, config_home, monkeypatch
    ):
        """An omitted top-level key reads KiroCrewConfig's default, even for a subclass.

        Only a load with no config file builds the bare subclass, whose own defaults
        then apply. The timezone publication is stubbed so the subclass's value can
        never reach the process-wide snapshot.
        """
        monkeypatch.setattr(loader, "publish_config_timezone", lambda *_args, **_kwargs: None)

        @dataclasses.dataclass
        class Sub(loader.KiroCrewConfig):
            timezone: str = "Europe/Paris"
            snapshot_dir: str = "/sub/snap"
            default_workspace: str = "sub-ws"
            observe_max_messages: int = 7

        bare = Sub.load()
        assert type(bare) is Sub
        assert (bare.timezone, bare.snapshot_dir, bare.default_workspace) == (
            "Europe/Paris",
            "/sub/snap",
            "sub-ws",
        )
        _write(config_home / "config.json", {"agent": {"model": "m"}})
        loaded = Sub.load()
        assert type(loaded) is Sub
        assert (loaded.timezone, loaded.snapshot_dir, loaded.default_workspace) == (
            "",
            "",
            "default",
        )
        assert loaded.observe_max_messages == 200

    def test_load_is_the_three_stages_in_order(self, config_home):
        _write(config_home / "config.json", {"agent": {"model": "m"}})
        loaded = loader.KiroCrewConfig.load()
        assert loaded.agent.model == "m"
        # The write-back ran: the crew it seeded is in memory and on disk.
        assert loaded.default_agent == "default"
        stored = json.loads((config_home / "config.json").read_text(encoding="utf-8"))
        assert stored["agents"]["default"]["kiro_agent"] == "kirocrew"
        assert loader.build_config(loader.read_config_document()) == loaded
