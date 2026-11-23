"""A spec edit committed while ``rebuild_agent_config`` runs is not reverted.

The rebuild reads its base copy of ``kirocrew.json`` long before it takes
``agents_spec_lock`` for the write. A writer that commits a read-merge-write under
that lock in between (the agent-detail PATCH pattern) must not be overwritten
with the stale base. Pinned here: under the lock the rebuild reads the file again and
carries the other writer's edit to a key it did not derive; a key the rebuild
derives (the hooks, the ``includeMcpJson: false`` pin) stays as derived; a carried
``toolsSettings`` loses the retired shell keys; with no concurrent writer the output
is unchanged.

The race is made deterministic: a pass-through wrap of ``_load_existing_config``
runs the concurrent writer on a second thread and joins it before the rebuild
continues.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable

import pytest

from kiro_crew import agent as agent_mod

_JOIN_TIMEOUT_S = 30


@pytest.fixture()
def spec_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway project, data home and agents dir, with one rebuild already run."""
    from kiro_crew.apps import bridges

    project = tmp_path / "project" / "agents"
    project.mkdir(parents=True)
    (project / "defaults.json").write_text(
        json.dumps({"name": "kirocrew", "tools": ["@kirocrew-core"]}), encoding="utf-8"
    )
    (project / "prompt.md").write_text("prompt", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path / "project"))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))

    kiro_dir = tmp_path / ".kiro" / "agents"
    kiro_dir.mkdir(parents=True)
    path = kiro_dir / "kirocrew.json"
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: kiro_dir)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "absent-kiro.json")
    monkeypatch.setattr(agent_mod, "_CC_MCP_JSON", tmp_path / "absent-cc.json")
    monkeypatch.setattr(bridges, "_mcp_json_path", lambda: path)
    agent_mod.rebuild_agent_config()
    _locked_edit(path, lambda spec: spec.__setitem__("description", "BASE"))
    agent_mod.rebuild_agent_config()
    return path


def _spec(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _locked_edit(path: Path, change: Callable[[dict], None]) -> None:
    """A read-merge-write under the real spec lock and atomic writer (the PATCH pattern)."""
    with agent_mod.agents_spec_lock(path.parent):
        spec = _spec(path)
        change(spec)
        agent_mod._atomic_json_write(path, spec)


def _race_after_base_read(monkeypatch: pytest.MonkeyPatch, writer: Callable[[], None]) -> None:
    """Run *writer* on a second thread right after the rebuild's base read, and join it."""
    real = agent_mod._load_existing_config

    def _racing(*args: Any, **kwargs: Any) -> Any:
        out = real(*args, **kwargs)
        thread = threading.Thread(target=writer)
        thread.start()
        thread.join(timeout=_JOIN_TIMEOUT_S)
        assert not thread.is_alive(), "the concurrent spec writer did not finish"
        return out

    monkeypatch.setattr(agent_mod, "_load_existing_config", _racing)


def test_an_edit_committed_during_the_rebuild_survives_it(
    spec_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _race_after_base_read(
        monkeypatch,
        lambda: _locked_edit(spec_path, lambda spec: spec.__setitem__("description", "EDIT")),
    )
    caplog.set_level(logging.INFO, logger=agent_mod.logger.name)

    agent_mod.rebuild_agent_config()

    assert _spec(spec_path)["description"] == "EDIT"
    carried = [r.getMessage() for r in caplog.records if "kept a concurrent edit" in r.getMessage()]
    assert carried == ["rebuild of kirocrew.json kept a concurrent edit to: description"]


def test_a_key_removed_during_the_rebuild_stays_removed(
    spec_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _race_after_base_read(
        monkeypatch, lambda: _locked_edit(spec_path, lambda spec: spec.pop("description"))
    )

    agent_mod.rebuild_agent_config()

    assert "description" not in _spec(spec_path)


def test_an_edit_committed_before_the_rebuild_still_survives(spec_path: Path) -> None:
    _locked_edit(spec_path, lambda spec: spec.__setitem__("description", "CTRL"))

    agent_mod.rebuild_agent_config()

    assert _spec(spec_path)["description"] == "CTRL"


def test_a_key_the_rebuild_derives_is_not_taken_from_the_concurrent_writer(
    spec_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    derived_hooks = _spec(spec_path)["hooks"]
    assert derived_hooks, "the fixture's rebuild should install the PreToolUse hooks"
    _race_after_base_read(
        monkeypatch,
        lambda: _locked_edit(spec_path, lambda spec: spec.__setitem__("hooks", {})),
    )

    agent_mod.rebuild_agent_config()

    assert _spec(spec_path)["hooks"] == derived_hooks


def test_the_rebuild_keeps_the_mcp_json_merge_pinned_off_against_the_concurrent_writer(
    spec_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _spec(spec_path)
    assert base["includeMcpJson"] is False and "useLegacyMcpJson" not in base

    def _merge_on(spec: dict) -> None:
        spec["includeMcpJson"] = True
        spec["useLegacyMcpJson"] = True

    _race_after_base_read(monkeypatch, lambda: _locked_edit(spec_path, _merge_on))

    agent_mod.rebuild_agent_config()

    rebuilt = _spec(spec_path)
    assert rebuilt["includeMcpJson"] is False
    assert "useLegacyMcpJson" not in rebuilt


def test_a_carried_auto_approve_grant_still_meets_the_ceiling(
    spec_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent_materialization import auto_approve

    real_may = auto_approve._may_auto_approve
    monkeypatch.setattr(
        auto_approve, "_may_auto_approve", lambda ref: ref != "withheld_tool" and real_may(ref)
    )
    _race_after_base_read(
        monkeypatch,
        lambda: _locked_edit(
            spec_path, lambda spec: spec.setdefault("allowedTools", []).append("withheld_tool")
        ),
    )

    agent_mod.rebuild_agent_config()

    assert "withheld_tool" not in _spec(spec_path)["allowedTools"]


def test_a_carried_tools_settings_loses_the_retired_shell_keys(
    spec_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "toolsSettings" not in _spec(spec_path)
    settings = {
        "execute_bash": {
            "autoAllowReadonly": True,
            "deniedCommands": ["rm"],
            "allowedCommands": ["ls"],
        }
    }
    _race_after_base_read(
        monkeypatch,
        lambda: _locked_edit(spec_path, lambda spec: spec.__setitem__("toolsSettings", settings)),
    )

    agent_mod.rebuild_agent_config()

    assert _spec(spec_path)["toolsSettings"] == {"execute_bash": {"allowedCommands": ["ls"]}}


def test_unparseable_concurrent_bytes_carry_nothing(
    spec_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _garble() -> None:
        with agent_mod.agents_spec_lock(spec_path.parent):
            spec_path.write_text("{not json", encoding="utf-8")

    _race_after_base_read(monkeypatch, _garble)

    agent_mod.rebuild_agent_config()

    assert _spec(spec_path)["description"] == "BASE"


def test_with_no_concurrent_writer_the_rebuild_writes_the_same_bytes(
    spec_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = spec_path.read_bytes()
    agent_mod.rebuild_agent_config()
    with_check = spec_path.read_bytes()

    spec_path.write_bytes(seed)
    monkeypatch.setattr(agent_mod, "_carry_concurrent_spec_edits", lambda *_a, **_k: None)
    agent_mod.rebuild_agent_config()

    assert with_check == spec_path.read_bytes()
