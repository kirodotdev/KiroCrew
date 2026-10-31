"""Per-turn and per-broadcast work answered from a memo, without changing an answer.

Each memo below sits on a path that runs several times a turn or on every slots
broadcast with the GIL held (on the event loop, for the preview): the skills off
switch rebuilt the whole config per catalog read, the disabled-app filter resolved
real paths for every skill per read, and every slot's last-message preview hashed
the whole message. Each test pins both halves: the repeated call skips the work,
and a change to any input the answer depends on is still seen.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import skill_trust
from kiro_crew.config import paths
from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture
def data_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "data-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(paths, "_resolved_home", None, raising=False)
    monkeypatch.setattr(skill_trust, "_enabled_memo", None)
    return home


def _write_switch(home: Path, enabled: bool) -> None:
    config = home / "config.json"
    data = json.loads(config.read_text()) if config.exists() else {}
    data.setdefault("skills", {})["project_skills_enabled"] = enabled
    staged = home / "config.json.tmp"
    staged.write_text(json.dumps(data))
    staged.replace(config)


class TestProjectSkillsSwitch:
    def test_an_unchanged_config_is_read_once(self, data_home, monkeypatch):
        _write_switch(data_home, True)
        loads = []
        real = KiroCrewConfig.load.__func__

        def counting(cls):
            loads.append(1)
            return real(cls)

        # The first load may write normalized fields back, which is a change to the
        # file; from then on the file is what this memo sees on every ask.
        assert skill_trust._project_skills_enabled() is True
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(counting))
        assert [skill_trust._project_skills_enabled() for _ in range(5)] == [True] * 5
        assert len(loads) <= 1

    def test_an_edited_config_is_read_again(self, data_home):
        _write_switch(data_home, True)
        assert skill_trust._project_skills_enabled() is True
        _write_switch(data_home, False)
        assert skill_trust._project_skills_enabled() is False

    def test_a_failed_read_is_not_remembered(self, data_home, monkeypatch):
        _write_switch(data_home, True)

        def broken(cls):
            raise OSError("unreadable")

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(broken))
        assert skill_trust._project_skills_enabled() is False
        monkeypatch.undo()
        monkeypatch.setenv("KIROCREW_HOME", str(data_home))
        monkeypatch.setattr(paths, "_resolved_home", None, raising=False)
        assert skill_trust._project_skills_enabled() is True


def _skill(root: Path, key: str) -> None:
    path = root / key / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {key}\ndescription: d\n---\nbody\n", encoding="utf-8")


class TestVisibleRows:
    def _loader(self, tmp_path, monkeypatch, opened):
        from kiro_crew.skills import SkillsLoader

        root = tmp_path / "skills"
        _skill(root, "app-one/kept")
        _skill(root, "app-two/hidden")
        loader = opened(SkillsLoader(skills_path=root, install_builtins=False))
        monkeypatch.setattr(loader, "_get_disabled_app_names", lambda: frozenset({"app-two"}))
        resolved = []
        real = loader._owning_app

        def counting(name, skill_file, within=None):
            resolved.append(name)
            return real(name, skill_file, within)

        monkeypatch.setattr(loader, "_owning_app", counting)
        return loader, resolved

    def test_the_same_catalog_is_filtered_once(self, tmp_path, monkeypatch, opened):
        loader, resolved = self._loader(tmp_path, monkeypatch, opened)
        first = loader._iter_visible()
        assert [name for name, _p, _w in first] == ["app-one/kept"]
        resolved.clear()
        assert loader._iter_visible() == first
        assert resolved == []

    def test_a_changed_disabled_set_filters_again(self, tmp_path, monkeypatch, opened):
        loader, _resolved = self._loader(tmp_path, monkeypatch, opened)
        assert [name for name, _p, _w in loader._iter_visible()] == ["app-one/kept"]
        monkeypatch.setattr(loader, "_get_disabled_app_names", lambda: frozenset({"app-one"}))
        assert [name for name, _p, _w in loader._iter_visible()] == ["app-two/hidden"]

    def test_a_skill_mutation_drops_the_memo(self, tmp_path, monkeypatch, opened):
        loader, resolved = self._loader(tmp_path, monkeypatch, opened)
        loader._iter_visible()
        loader._invalidate_iter_cache()
        assert loader._visible_memo == {}


class TestLastMessagePreview:
    @pytest.fixture(autouse=True)
    def _clean(self):
        from kiro_crew.dashboard import chat_utils_display as cud

        cud._clear_display_redaction_cache()
        yield
        cud._clear_display_redaction_cache()

    def test_the_same_message_object_is_not_hashed_again(self, monkeypatch):
        from kiro_crew.dashboard import chat_utils_display as cud

        text = "a reply that is long enough to be cut " * 20
        first = cud._redacted_preview(text, 80)
        keys = []
        real = cud._display_redaction_cache_key
        monkeypatch.setattr(
            cud, "_display_redaction_cache_key", lambda *a, **k: keys.append(1) or real(*a, **k)
        )
        assert cud._redacted_preview(text, 80) == first
        assert keys == []

    def test_an_equal_string_in_another_object_answers_the_same(self):
        from kiro_crew.dashboard import chat_utils_display as cud

        text = "token AKIAIOSFODNN7EXAMPLE in a message " * 3
        twin = "".join(list(text))
        assert twin is not text
        assert cud._redacted_preview(text, 80) == cud._redacted_preview(twin, 80)
        assert "AKIAIOSFODNN7EXAMPLE" not in cud._redacted_preview(twin, 80)

    def test_a_changed_exempt_host_set_is_seen(self, monkeypatch):
        from kiro_crew.dashboard import chat_utils_display as cud

        text = "see https://tenant.example.com/view and more"
        cud._redacted_preview(text, 80)
        keys = []
        real = cud._display_redaction_cache_key
        monkeypatch.setattr(
            cud, "_display_redaction_cache_key", lambda *a, **k: keys.append(1) or real(*a, **k)
        )
        monkeypatch.setattr(cud, "_exempt_exact_hosts", lambda: frozenset({"tenant.example.com"}))
        cud._redacted_preview(text, 80)
        assert keys, "a new exempt-host set must miss the object memo"

    def test_a_different_limit_is_its_own_answer(self):
        from kiro_crew.dashboard import chat_utils_display as cud

        text = "word " * 100
        assert len(cud._redacted_preview(text, 10)) == 11
        assert len(cud._redacted_preview(text, 80)) == 81
