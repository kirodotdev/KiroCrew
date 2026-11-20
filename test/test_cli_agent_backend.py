"""``--backend`` on ``kirocrew agent create`` / ``agent update``: the crew's harness pin.

The CLI stores ``agents.<name>.acp_backend`` under the same rule the crew editor's
save applies (``crewmate_harness.harness_pin_rejected``), and a harness change
re-judges the stored model pins against the new harness.
"""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path

import pytest
from model_registry_row_helpers import add_claude_only_row

from kiro_crew.cli import main

SELECTABLE = {"", "kas", "claude"}


@pytest.fixture(autouse=True)
def _selectable(_floor_monkeypatch):
    """A fixed selectable set: kiro-cli, KAS and Claude Code."""
    from kiro_crew.agent_sdk import backends

    _floor_monkeypatch.setattr(backends, "selectable_backends", lambda: frozenset(SELECTABLE))


def _write_config(tmp_path: Path, **research: str) -> Path:
    data = {
        "agents": {
            "default": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
            },
            "research": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
                **research,
            },
        },
        "default_agent": "default",
        "workspaces": {"default": {"dir": "workspace"}},
        "memory_stores": {"default": {}},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _run(cfg_path: Path, *argv: str) -> None:
    with (
        unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
        unittest.mock.patch("sys.argv", ["kirocrew", "agent", *argv]),
    ):
        main()


def _saved(cfg_path: Path, name: str = "research") -> dict:
    return json.loads(cfg_path.read_text(encoding="utf-8"))["agents"][name]


def test_update_pins_the_harness_and_changes_nothing_else(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg_path = _write_config(tmp_path)
    before = _saved(cfg_path)

    _run(cfg_path, "update", "research", "--backend", "claude")

    assert _saved(cfg_path) == {**before, "acp_backend": "claude"}
    out = capsys.readouterr().out
    assert "Updated agent: research" in out
    assert "Harness: claude" in out


def test_an_empty_backend_clears_the_pin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg_path = _write_config(tmp_path, acp_backend="claude")

    _run(cfg_path, "update", "research", "--backend", "")

    assert _saved(cfg_path)["acp_backend"] == ""
    assert "Harness: (inherit)" in capsys.readouterr().out


def test_a_harness_crewmates_cannot_be_pinned_to_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg_path = _write_config(tmp_path)
    before = cfg_path.read_text(encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run(cfg_path, "update", "research", "--backend", "opencode")

    assert exc_info.value.code == 1
    assert "acp_backend must be one of" in capsys.readouterr().err
    assert cfg_path.read_text(encoding="utf-8") == before


def test_the_backend_alone_is_something_to_update(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg_path = _write_config(tmp_path)

    _run(cfg_path, "update", "research", "--backend", "kas")

    assert "nothing to update" not in capsys.readouterr().err
    assert _saved(cfg_path)["acp_backend"] == "kas"


def test_a_new_model_is_judged_on_the_new_harness(tmp_path: Path, monkeypatch) -> None:
    pin = add_claude_only_row(monkeypatch)
    cfg_path = _write_config(tmp_path)

    _run(cfg_path, "update", "research", "--backend", "claude", "--model", pin)

    saved = _saved(cfg_path)
    assert (saved["acp_backend"], saved["model"]) == ("claude", pin)


@pytest.mark.parametrize("field", ["model", "worker_model"])
def test_moving_off_a_harness_rejudges_the_stored_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch, field: str
) -> None:
    pin = add_claude_only_row(monkeypatch)
    cfg_path = _write_config(tmp_path, acp_backend="claude", **{field: pin})
    before = cfg_path.read_text(encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run(cfg_path, "update", "research", "--backend", "kas")

    assert exc_info.value.code == 1
    assert f"'{pin}' is not a model kiro-cli serves" in capsys.readouterr().err
    assert cfg_path.read_text(encoding="utf-8") == before


def test_create_stores_the_pin(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_path = _write_config(tmp_path)

    _run(cfg_path, "create", "--name", "oncall", "--backend", "claude")

    assert _saved(cfg_path, "oncall")["acp_backend"] == "claude"
    assert "Harness: claude" in capsys.readouterr().out


def test_create_judges_the_model_on_the_pinned_harness(tmp_path: Path, monkeypatch) -> None:
    pin = add_claude_only_row(monkeypatch)
    cfg_path = _write_config(tmp_path)

    _run(cfg_path, "create", "--name", "oncall", "--backend", "claude", "--model", pin)

    saved = _saved(cfg_path, "oncall")
    assert (saved["acp_backend"], saved["model"]) == ("claude", pin)


def test_create_refuses_an_unknown_harness_before_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg_path = _write_config(tmp_path)
    before = cfg_path.read_text(encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run(cfg_path, "create", "--name", "oncall", "--backend", "nonesuch")

    assert exc_info.value.code == 1
    assert "acp_backend must be one of" in capsys.readouterr().err
    assert cfg_path.read_text(encoding="utf-8") == before
