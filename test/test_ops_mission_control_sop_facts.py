"""Ops Mission Control SOP text must match what the app ships.

The SOPs are read by cron agents, so a path the tool refuses or a schedule that
disagrees with ``app.json`` sends the agent the wrong way. Each check reads the
shipped files; none needs a gateway.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kiro_crew.mcp_tools.apps import OPS_MISSION_CONTROL_ALLOWED_CALLS

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
_SOPS = _SRC / "builtin_skills" / "ops-mission-control" / "sops"
_APP_JSON = _SRC / "apps" / "builtins" / "ops_mission_control" / "app.json"

# A backticked `GET /x` or `POST /x` in SOP prose. Gateway routes outside the app
# base (`/api/...`) are plain routes, not tool paths, so they are excluded.
_CALL = re.compile(r"`(GET|POST) (/(?!api/)[a-z/_-]+)")


def _sops() -> list[Path]:
    return sorted(_SOPS.glob("*.md"))


@pytest.mark.parametrize("sop", _sops(), ids=lambda p: p.name)
def test_every_app_call_in_a_sop_is_reachable_through_the_tool(sop: Path) -> None:
    text = sop.read_text(encoding="utf-8")
    for method, path in _CALL.findall(text):
        assert (method, path.rstrip("/")) in OPS_MISSION_CONTROL_ALLOWED_CALLS, (
            f"{sop.name} tells the agent to call {method} {path}, which "
            "ops_mission_control_api refuses"
        )


def test_sop_cron_expr_matches_the_shipped_cron() -> None:
    crons = json.loads(_APP_JSON.read_text(encoding="utf-8"))["crons"]
    by_name = {c["name"]: c for c in crons}
    checked = 0
    for sop in _sops():
        text = sop.read_text(encoding="utf-8")
        name = re.search(r"^cron: ops-mission-control/(\S+)$", text, re.M)
        schedule = re.search(r'^schedule: "([^"]+)"$', text, re.M)
        if not (name and schedule):
            continue
        cron = by_name.get(name.group(1))
        if cron is None or "cron_expr" not in cron:
            continue
        assert schedule.group(1) == cron["cron_expr"], sop.name
        checked += 1
    assert checked, "no SOP with a cron_expr schedule was checked"


def test_dispatch_sop_runs_the_server_side_cycle() -> None:
    text = (_SOPS / "dispatch.md").read_text(encoding="utf-8")
    steps = text.split("## Steps", 1)[1].split("## Rules", 1)[0]
    first_step = steps.split("\n2. ", 1)[0]
    assert "`POST /dispatch`" in first_step
    # The manual board claim may be named only as the thing NOT to use.
    for line in steps.splitlines():
        if "POST /incident/claim" in line:
            assert "Do NOT" in steps.split("POST /incident/claim", 1)[0][-200:], line
