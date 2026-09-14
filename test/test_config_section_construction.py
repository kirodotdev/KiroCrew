"""Section construction stays bounded without caching resolved configuration."""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from dataclasses import fields, is_dataclass

import pytest
from member_memory_helpers import patch_private_memory_supported

from kiro_crew.config import loader
from kiro_crew.memory_stores import (
    UnknownMemoryStore,
    persist_member_config,
    provision_member_memory,
    require_memory_store,
)


def _write(path, sections):
    document = {
        "agents": {"default": {"kiro_agent": "kirocrew"}},
        "default_agent": "default",
        "workspaces": {"default": {"dir": "~/workspace"}},
        **sections,
    }
    path.write_text(json.dumps(document), encoding="utf-8")


class TestSectionConstruction:
    def test_compound_sections_have_their_own_construction_frames(self):
        """Large nested calls made every traced load costly, even on a cache hit."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(loader.KiroCrewConfig._load_resolved)))
        assembly = next(
            node.value
            for node in tree.body[0].body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "cfg" for target in node.targets)
            and isinstance(node.value, ast.Call)
        )
        inline = [
            keyword.arg
            for keyword in assembly.keywords
            if isinstance(keyword.value, ast.Call)
            and isinstance(keyword.value.func, ast.Name)
            and keyword.value.func.id.endswith("Config")
            and len(keyword.value.keywords) > 1
        ]
        assert not inline, f"Compound section constructors returned to the resolver frame: {inline}"

    @pytest.mark.parametrize("populated", [False, True])
    def test_cache_hits_build_independent_objects_and_mutable_values(
        self, tmp_path, monkeypatch, populated
    ):
        monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
        sections = (
            {
                "agent": {"apps_trusted": ["fixture-app"]},
                "memory": {"decay_rates": {"tool": 0.1}, "semantic_keys": ["fixture"]},
                "dashboard": {"terminal": {"enabled": False}},
            }
            if populated
            else {}
        )
        _write(tmp_path / "config.json", sections)
        loader.KiroCrewConfig.load()  # Complete any one-shot marker work before the cache hit.
        first = loader.KiroCrewConfig.load()
        second = loader.KiroCrewConfig.load()
        for field in fields(first):
            left, right = getattr(first, field.name), getattr(second, field.name)
            if is_dataclass(left):
                assert left is not right, field.name
                assert left == right, field.name
        first.agent.apps_trusted.append("mutated")
        first.memory.decay_rates["mutated"] = 9.0
        first.memory.semantic_keys.append("mutated")
        first.dashboard.terminal["mutated"] = True
        third = loader.KiroCrewConfig.load()
        assert third == second
        assert "mutated" not in third.agent.apps_trusted
        assert "mutated" not in third.memory.decay_rates
        assert "mutated" not in third.memory.semantic_keys
        assert "mutated" not in third.dashboard.terminal

    @pytest.mark.parametrize(
        "roots,decay,megabytes,hidden,expected_roots,expected_decay,expected_mb,expected_hidden",
        [
            (
                ["/fixture", 7],
                {"tool": 0.2},
                12.5,
                [" sample-a ", "auto", "sample-a"],
                ["/fixture"],
                {"tool": 0.2},
                12.5,
                ["sample-a"],
            ),
            ([], {}, 0, [], [], {}, 0.0, []),
        ],
    )
    def test_named_expression_values_do_not_cross_section_or_load_boundaries(
        self,
        tmp_path,
        monkeypatch,
        roots,
        decay,
        megabytes,
        hidden,
        expected_roots,
        expected_decay,
        expected_mb,
        expected_hidden,
    ):
        monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
        path = tmp_path / "config.json"
        _write(
            path,
            {
                "agent": {"subagent_cwd_allowed_roots": roots},
                "memory": {"decay_rates": decay},
                "knowledge": {"max_ingest_file_mb": megabytes},
                "dashboard": {"model_picker_hidden_models": hidden},
            },
        )
        cfg = loader.KiroCrewConfig.load()
        assert cfg.agent.subagent_cwd_allowed_roots == expected_roots
        assert cfg.memory.decay_rates == expected_decay
        assert cfg.knowledge.max_ingest_file_mb == expected_mb
        assert cfg.dashboard.model_picker_hidden_models == expected_hidden
        _write(path, {"knowledge": {"max_ingest_file_mb": 99.0}})
        fresh = loader.KiroCrewConfig.load()
        assert fresh.memory.decay_rates == {}
        assert fresh.knowledge.max_ingest_file_mb == 99.0
        assert fresh.dashboard.model_picker_hidden_models == []

    @pytest.mark.parametrize("change", ["revoke", "malformed", "corrupt"])
    def test_warm_load_observes_privacy_control_withdrawal(self, tmp_path, monkeypatch, change):
        monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
        path = tmp_path / "config.json"
        _write(
            path,
            {
                "agent": {"member_dispatch": True},
                "memory": {"private_provisioning_enabled": True},
                "skills": {"project_skills_enabled": True},
                "dashboard": {"default_memory_mode": "persistent"},
            },
        )
        for _ in range(2):
            cfg = loader.KiroCrewConfig.load()
            assert cfg.agent.member_dispatch is True
            assert cfg.memory.private_provisioning_enabled is True
            assert cfg.skills.project_skills_enabled is True
            assert cfg.dashboard.default_memory_mode == "persistent"
        if change == "corrupt":
            path.write_text("{broken", encoding="utf-8")
        else:
            value = False if change == "revoke" else "false"
            _write(
                path,
                {
                    "agent": {"member_dispatch": value},
                    "memory": {"private_provisioning_enabled": value},
                    "skills": {"project_skills_enabled": value},
                    "dashboard": {"default_memory_mode": "temporary"},
                },
            )
        fresh = loader.KiroCrewConfig.load()
        assert fresh.skills.project_skills_enabled is False
        assert fresh.dashboard.default_memory_mode == "temporary"
        if change != "corrupt":
            assert fresh.agent.member_dispatch is False
            assert fresh.memory.private_provisioning_enabled is False

    @pytest.mark.parametrize("change", ["owner", "binding", "corrupt"])
    def test_warm_private_store_admission_rejects_changed_config(
        self, tmp_path, monkeypatch, change
    ):
        patch_private_memory_supported(monkeypatch)
        monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        cfg = loader.KiroCrewConfig.load()
        cfg.agents["alice"] = loader.KiroCrewAgentConfig(kiro_agent="kirocrew")
        store = provision_member_memory(cfg, "alice")
        persist_member_config(cfg, "alice", create=True)
        assert require_memory_store(store) == store
        assert require_memory_store(store) == store
        path = tmp_path / "config.json"
        if change == "corrupt":
            path.write_text("{broken", encoding="utf-8")
        else:

            def withdraw(data):
                if change == "owner":
                    data["memory_stores"][store]["owner_member"] = "different-owner"
                else:
                    del data["agents"]["alice"]
                return data

            loader.update_config_locked(mutate=withdraw)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            if change == "owner":
                assert persisted["memory_stores"][store]["owner_member"] == "different-owner"
            else:
                assert "alice" not in persisted["agents"]
        with pytest.raises(UnknownMemoryStore):
            require_memory_store(store)
