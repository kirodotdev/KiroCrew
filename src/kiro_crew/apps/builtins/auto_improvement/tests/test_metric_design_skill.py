"""The metric-design skill tells agents how Phase 1 works; pin its facts to code.

The skill body is read by the discovery agent and by any chat that triggers on
"calibrate metric", so a stale claim steers a model. Each claim below is checked
against the code that owns it: the calibrate route and get_ruler tool exist,
the ruler path and default rep count match the runner, and the skill does not
say the UI gates Start or that an agent writes a metric-design document.
"""

from __future__ import annotations

import re
from pathlib import Path

_APP = Path(__file__).resolve().parents[1]
_SKILL = _APP / "skills" / "metric-design" / "SKILL.md"


def _skill() -> str:
    return _SKILL.read_text(encoding="utf-8")


def _src(rel: str) -> str:
    return (_APP / rel).read_text(encoding="utf-8")


def test_calibrate_route_named_by_skill_is_registered() -> None:
    assert "`POST /api/apps/auto-improvement/calibrate`" in _skill()
    routes = _src("backend/routes.py")
    assert '_PREFIX = f"/api/apps/{store.APP_NAME}"' in routes
    assert 'APP_NAME = "auto-improvement"' in _src("backend/store.py")
    assert 'add("POST", f"{_PREFIX}/calibrate"' in routes
    # The skill calls the route owner-only; the handler must keep that gate.
    assert 'require_owner_dashboard_request(request, "auto_improvement.calibrate")' in routes


def test_get_ruler_tool_named_by_skill_is_registered() -> None:
    assert "`get_ruler`" in _skill()
    assert '"get_ruler": (_tool_get_ruler,' in _src("backend/mcp_server.py")


def test_ruler_path_and_fields_match_runner() -> None:
    skill = _skill()
    assert "data/repos/<workspace-key>/ruler/" in skill
    runner = _src("backend/runner.py")
    assert re.search(
        r'/ "repos"\s*/ store_mod\.workspace_key\(config\)\s*/ "ruler"\s*/ "ruler\.json"', runner
    )
    assert 'status = "calibrated" if cleared else "canary_failed"' in runner
    assert '"anchors": [{"name": "baseline", "value": median}]' in runner


def test_calibration_reps_default_and_clamp_match_code() -> None:
    skill = _skill()
    assert "`calibrationReps`, default 5" in skill
    assert "2–10" in skill
    assert '_pos_int(config.get("calibrationReps"), 5)' in _src("backend/runner.py")
    assert "max(2, min(int(baseline_reps), 10))" in _src("profiles/github_repo/profile.py")


def test_canary_opt_out_named_by_skill_exists() -> None:
    assert "`canaryAdvisory: true`" in _skill()
    assert 'canary_advisory=_as_bool(config.get("canaryAdvisory"), False)' in _src(
        "backend/runner.py"
    )


def test_skill_drops_claims_the_code_does_not_back() -> None:
    skill = _skill()
    # No UI Start lock exists; the gate is the backend pre-flight.
    assert "disables Start" not in skill
    # Calibration writes one JSON record, not a separate review document.
    assert "metric-design document to review" not in skill
    assert "around 30 repetitions" not in skill
    # The skill must not tell an agent it runs Phase 1 itself.
    assert "This skill drives Phase 1" not in skill
