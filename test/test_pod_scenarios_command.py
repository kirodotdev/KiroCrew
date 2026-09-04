"""Tests for ``kirocrew pod scenarios`` discovery."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew import seed as seed_mod
from kiro_crew.pod import cli as pod_cli
from kiro_crew.pod.config import PodConfig

_SRC = str(Path(kiro_crew.__file__).resolve().parents[1])
_REPO = Path(_SRC).parent


def _run_cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the real parser outside the checkout with isolated host state."""
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "os-home"),
            "USERPROFILE": str(tmp_path / "os-home"),
            "KIROCREW_HOME": str(tmp_path / "crew-home"),
            "KIROCREW_PROJECT_DIR": str(_REPO),
            "PYTHONPATH": _SRC,
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "kiro_crew", "pod", "scenarios", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        env=env,
        timeout=30,
        check=False,
    )


def test_human_output_lists_sorted_names_and_descriptions(tmp_path: Path) -> None:
    result = _run_cli(tmp_path)

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "SCENARIO  DESCRIPTION"
    assert [line.split()[0] for line in lines[1:4]] == ["empty", "minimal", "rich"]
    assert "A KIROCREW_HOME with nothing in it but this manifest." in lines[1]
    assert lines[-1] == "seed one with: kirocrew pod up <worktree> --seed empty"


def test_json_output_is_a_stable_row_array(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "--json")

    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert [row["name"] for row in rows] == ["empty", "minimal", "rich"]
    assert all(set(row) == {"name", "description"} for row in rows)
    assert all(row["description"] for row in rows)


def test_unknown_flag_is_rejected_by_argparse(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "--not-a-scenarios-flag")

    assert result.returncode == 2
    assert "unrecognized arguments: --not-a-scenarios-flag" in result.stderr
    assert result.stdout == ""


def test_handler_sorts_even_if_registry_order_changes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(seed_mod, "available_fixtures", lambda: ["zeta", "alpha"])
    monkeypatch.setattr(
        seed_mod,
        "fixture_summary",
        lambda name: {"alpha": "first", "zeta": "last"}[name],
        raising=False,
    )

    pod_cli._scenarios(PodConfig.load(), argparse.Namespace(json=True))

    assert json.loads(capsys.readouterr().out) == [
        {"name": "alpha", "description": "first"},
        {"name": "zeta", "description": "last"},
    ]


def test_empty_registry_has_explicit_human_and_json_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(seed_mod, "available_fixtures", lambda: [])

    pod_cli._scenarios(PodConfig.load(), argparse.Namespace(json=False))
    assert capsys.readouterr().out == (
        "no seed scenarios found (the packaged fixtures tree is missing)\n"
    )

    pod_cli._scenarios(PodConfig.load(), argparse.Namespace(json=True))
    assert json.loads(capsys.readouterr().out) == []


def test_description_parser_needs_no_pyyaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "yaml", None)

    assert seed_mod.fixture_summary("minimal") == (
        "Populated KIROCREW_HOME with two workspaces, preferences/projects/semantic/"
    )
