"""Prevent silent growth of legacy monoliths while behavior is being preserved."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_architecture_ratchet_keeps_hotspots_bounded() -> None:
    report = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "architecture_guard.py"), "--check"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert report.returncode == 0, report.stdout + "\n" + report.stderr
    assert "architecture budget: PASS" in report.stdout


def test_architecture_budget_explicitly_targets_core_execution_and_acp() -> None:
    budget = json.loads((ROOT / "config" / "architecture_budget.json").read_text(encoding="utf-8"))
    assert budget["functions_over_100_lines"] <= 54
    assert budget["functions_over_200_lines"] <= 13
    assert "src/kiro_byo/turn_runner.py" in budget["hotspot_file_lines"]
    assert "src/kiro_byo/acp_server.py" in budget["hotspot_file_lines"]
    assert budget["hotspot_function_lines"]["src/kiro_byo/integration_manager.py::install_from_app"] <= 198


def test_v4_prompt_route_boundary_is_a_separately_bounded_module() -> None:
    budget = json.loads((ROOT / "config" / "architecture_budget.json").read_text(encoding="utf-8"))
    assert budget["hotspot_file_lines"]["src/kiro_byo/prompt_routing.py"] <= 240
    assert budget["hotspot_function_lines"]["src/kiro_byo/turn_runner.py::_handle_prompt_request"] <= 92
    assert budget["hotspot_function_lines"]["src/kiro_byo/turn_runner.py::_execute_prompt_request_locked"] <= 162
