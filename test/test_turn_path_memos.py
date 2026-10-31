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
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew import skill_trust
from kiro_crew.config import paths
from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture
def data_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    home = tmp_path / "data-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(paths, "_resolved_home", None, raising=False)
    from kiro_crew.config import load_memo

    load_memo.forget()
    yield home
    load_memo.forget()


def _write_switch(home: Path, enabled: bool) -> None:
    config = home / "config.json"
    data = json.loads(config.read_text()) if config.exists() else {}
    data.setdefault("skills", {})["project_skills_enabled"] = enabled
    staged = home / "config.json.tmp"
    staged.write_text(json.dumps(data))
    staged.replace(config)


def _count_rebuilds(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count full config rebuilds, leaving the real ``load`` bound (the memo only
    answers for the real loader; a replaced one is called every time)."""
    rebuilds: list[int] = []
    real = KiroCrewConfig._load_resolved.__func__

    def counting(cls):
        rebuilds.append(1)
        return real(cls)

    monkeypatch.setattr(KiroCrewConfig, "_load_resolved", classmethod(counting))
    return rebuilds


class TestProjectSkillsSwitch:
    def test_an_unchanged_config_is_read_once(self, data_home, monkeypatch):
        _write_switch(data_home, True)
        # The first load may write normalized fields back, which is a change to the
        # file; from then on the file is what this memo sees on every ask.
        assert skill_trust._project_skills_enabled() is True
        assert skill_trust._project_skills_enabled() is True
        rebuilds = _count_rebuilds(monkeypatch)
        assert [skill_trust._project_skills_enabled() for _ in range(5)] == [True] * 5
        assert rebuilds == []

    def test_a_replaced_loader_is_called_every_time(self, data_home, monkeypatch):
        calls = []

        def double(cls):
            calls.append(1)
            config = KiroCrewConfig()
            config.skills.project_skills_enabled = len(calls) % 2 == 1
            return config

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(double))
        assert [skill_trust._project_skills_enabled() for _ in range(3)] == [True, False, True]

    def test_an_edited_config_is_read_again(self, data_home):
        _write_switch(data_home, True)
        assert skill_trust._project_skills_enabled() is True
        _write_switch(data_home, False)
        assert skill_trust._project_skills_enabled() is False

    def test_a_failed_read_is_not_remembered(self, data_home, monkeypatch):
        _write_switch(data_home, True)

        def broken(cls):
            raise OSError("unreadable")

        with monkeypatch.context() as mp:
            mp.setattr(KiroCrewConfig, "load", classmethod(broken))
            assert skill_trust._project_skills_enabled() is False
        assert skill_trust._project_skills_enabled() is True

    def test_defaults_from_a_partial_read_are_not_remembered(self, data_home, monkeypatch):
        """A present file that did not read whole falls back to defaults with
        an unchanged stat fingerprint; that answer must not outlive the fault."""
        _write_switch(data_home, True)
        assert skill_trust._project_skills_enabled() is True
        from kiro_crew.config import load_memo, loader
        from kiro_crew.config.validation import _CONFIG_CACHE

        load_memo.forget()
        _CONFIG_CACHE.clear()
        real_read = loader.read_config_text

        def flaky(path, *a, **k):
            raise OSError("sharing violation")

        # Through the facade, which carries the patch to every owner that binds it.
        with monkeypatch.context() as mp:
            mp.setattr(loader, "read_config_text", flaky)
            # An unreadable file leaves the switch off: the load fails closed.
            assert skill_trust._project_skills_enabled() is False
        assert loader.read_config_text is real_read
        assert skill_trust._project_skills_enabled() is True


def _write_agent(home: Path, **fields: float) -> None:
    config = home / "config.json"
    data = json.loads(config.read_text()) if config.exists() else {}
    data.setdefault("agent", {}).update(fields)
    staged = home / "config.json.tmp"
    staged.write_text(json.dumps(data))
    staged.replace(config)


class TestTurnTimeouts:
    """The three config reads every turn dispatch makes, two of them on the loop."""

    def test_repeated_dispatch_reads_do_not_rebuild_the_config(self, data_home, monkeypatch):
        from kiro_crew.acp.client_timeouts import resolve_prompt_timeout
        from kiro_crew.dashboard import turn_dispatch

        _write_agent(data_home, chat_turn_timeout_secs=3600, soft_stop_budget_secs=5)
        # The first load may write normalized fields back (a change to the file);
        # after the file settles, every read is answered from the memo.
        for _ in range(2):
            first = (
                turn_dispatch.chat_turn_timeout_secs(),
                turn_dispatch.native_cancel_ack_timeout_secs(),
                resolve_prompt_timeout(),
            )
        loads = _count_rebuilds(monkeypatch)
        for _ in range(5):
            assert (
                turn_dispatch.chat_turn_timeout_secs(),
                turn_dispatch.native_cancel_ack_timeout_secs(),
                resolve_prompt_timeout(),
            ) == first
        assert loads == []
        assert first[:2] == (3600.0, 5.0)

    def test_an_edited_turn_ceiling_is_read_again(self, data_home):
        from kiro_crew.dashboard import turn_dispatch

        _write_agent(data_home, chat_turn_timeout_secs=3600)
        assert turn_dispatch.chat_turn_timeout_secs() == 3600.0
        _write_agent(data_home, chat_turn_timeout_secs=1800)
        assert turn_dispatch.chat_turn_timeout_secs() == 1800.0

    def test_an_unreadable_config_falls_back_every_time(self, data_home, monkeypatch):
        from kiro_crew.constants import CHAT_TURN_TIMEOUT
        from kiro_crew.dashboard import turn_dispatch

        def broken(cls):
            raise OSError("unreadable")

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(broken))
        assert turn_dispatch.chat_turn_timeout_secs() == CHAT_TURN_TIMEOUT
        assert turn_dispatch.chat_turn_timeout_secs() == CHAT_TURN_TIMEOUT


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

    def test_a_superseded_catalog_list_is_not_kept(self, tmp_path, monkeypatch, opened):
        loader, _resolved = self._loader(tmp_path, monkeypatch, opened)
        old = list(loader._iter())
        new = list(old)
        served = [old]
        monkeypatch.setattr(loader, "_iter", lambda project_dir=None: served[0])
        monkeypatch.setattr(loader, "_iter_cache", {"": (float("inf"), old)})
        loader._iter_visible()
        assert [entry[0] for entry in loader._visible_memo.values()] == [old]
        # A revalidation publishes a new list for the same scope.
        served[0] = new
        loader._iter_cache[""] = (float("inf"), new)
        loader._iter_visible()
        assert [entry[0] for entry in loader._visible_memo.values()] == [new]

    def test_an_oversized_catalog_is_filtered_uncached(self, tmp_path, monkeypatch, opened):
        from kiro_crew.skill_runtime import catalog

        loader, resolved = self._loader(tmp_path, monkeypatch, opened)
        monkeypatch.setattr(catalog, "_VISIBLE_MEMO_MAX_ROWS", 1)
        first = loader._iter_visible()
        assert [name for name, _p, _w in first] == ["app-one/kept"]
        assert loader._visible_memo == {}
        resolved.clear()
        assert loader._iter_visible() == first
        assert resolved, "an uncached catalog is filtered again on each read"

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

    def test_retained_text_is_bounded_by_characters(self, monkeypatch):
        from kiro_crew.dashboard import chat_utils_display as cud

        monkeypatch.setattr(cud, "_DISPLAY_PREVIEW_BY_OBJECT_MAX_CHARS", 1_000)
        oversized = "x" * 1_001
        cud._redacted_preview(oversized, 80)
        assert id(oversized) not in cud._display_preview_by_object
        held = ["msg %d " % i + "y" * 290 for i in range(8)]
        for text in held:
            cud._redacted_preview(text, 80)
            assert cud._display_preview_by_object_chars <= 1_000
        assert cud._display_preview_by_object_chars == sum(
            len(entry[0]) for entry in cud._display_preview_by_object.values()
        )
        assert id(held[-1]) in cud._display_preview_by_object

    def test_the_redaction_context_is_held_once(self, monkeypatch):
        from kiro_crew.dashboard import chat_utils_display as cud

        # A fresh host set per call, as the policy reader builds one.
        monkeypatch.setattr(cud, "_exempt_exact_hosts", lambda: frozenset({"a.example.com"}))
        texts = [f"message number {i}" for i in range(50)]
        for text in texts:
            cud._redacted_preview(text, 80)
        assert len(cud._display_preview_by_object) == 50
        assert cud._display_preview_by_object_context is not None
        # Entries hold only the text, the limit and the preview, never a context.
        assert all(
            len(entry) == 3 and isinstance(entry[1], int)
            for entry in cud._display_preview_by_object.values()
        )
        monkeypatch.setattr(cud, "_exempt_exact_hosts", lambda: frozenset({"b.example.com"}))
        cud._redacted_preview(texts[0], 80)
        assert len(cud._display_preview_by_object) == 1, "a new context empties the map"

    def test_a_different_limit_is_its_own_answer(self):
        from kiro_crew.dashboard import chat_utils_display as cud

        text = "word " * 100
        assert len(cud._redacted_preview(text, 10)) == 11
        assert len(cud._redacted_preview(text, 80)) == 81
