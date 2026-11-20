"""Antigravity's effort levels are remembered per model so an empty draft can show them."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import advertised_effort_cache as cache
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.handlers.agents_harness_effort import harness_effort

MODEL = "gemini-4-argon"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


def _live(model: str, levels: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        capabilities=capabilities_for("antigravity"),
        served_model=model,
        get_valid_effort_levels=lambda: list(levels),
        supports_effort=lambda: True,
    )


def _state(*providers: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        sessions=SimpleNamespace(
            active_providers=lambda: list(providers), get_provider=lambda _k: None
        )
    )


def test_a_draft_with_no_record_stays_unknown():
    out = harness_effort(_state(), "antigravity", MODEL)
    assert out == {"effort_supported": None, "effort_levels": []}


def test_a_remembered_model_shows_its_levels_before_any_session(monkeypatch):
    cache.remember("antigravity", MODEL, ["low", "medium", "high"])
    out = harness_effort(_state(), "antigravity", MODEL)
    assert out["effort_supported"] is True
    assert out["effort_levels"] == ["low", "medium", "high"]


def test_another_model_does_not_borrow_the_record():
    cache.remember("antigravity", MODEL, ["low", "high"])
    assert harness_effort(_state(), "antigravity", "gemini-4-neon")["effort_supported"] is None
    assert harness_effort(_state(), "antigravity", "auto")["effort_supported"] is None


def test_the_record_survives_a_restart_and_a_live_session_replaces_it(monkeypatch):
    cache.remember("antigravity", MODEL, ["low", "high"])
    assert cache.recall("antigravity", MODEL) == ["low", "high"]
    live = _live(MODEL, ["low", "medium", "high"])
    out = harness_effort(_state(live), "antigravity", MODEL)
    assert out["effort_levels"] == ["low", "medium", "high"]
    assert cache.recall("antigravity", MODEL) == ["low", "medium", "high"]


def test_a_session_that_advertises_nothing_retracts_the_record():
    cache.remember("antigravity", MODEL, ["low"])
    cache.remember("antigravity", MODEL, [])
    assert cache.recall("antigravity", MODEL) == []


def test_another_harness_is_not_given_remembered_levels():
    cache.remember("goose", MODEL, ["low", "high"])
    assert harness_effort(_state(), "goose", MODEL)["effort_supported"] is None


def test_an_off_only_record_is_not_a_control():
    cache.remember("antigravity", MODEL, ["off"])
    assert harness_effort(_state(), "antigravity", MODEL)["effort_supported"] is None


def test_a_corrupt_file_reads_as_no_record(tmp_path):
    from kiro_crew.config.paths import data_home

    data_home().mkdir(parents=True, exist_ok=True)
    (data_home() / "advertised_effort_levels.json").write_text("{not json", encoding="utf-8")
    assert cache.recall("antigravity", MODEL) == []
