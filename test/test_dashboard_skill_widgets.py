"""The dashboard skill's widgets section: its example is a package the validator takes.

An agent writes its first ``widgets`` block from that example, so the example is
loaded straight out of SKILL.md and run through ``validate_package``; a validator
change that would refuse it fails here instead of on an agent's first save.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from kiro_crew import mcp_panel
from kiro_crew.artifact_store import dashboard_widgets as dw
from kiro_crew.artifact_store.dashboard_package import DASHBOARD_KIND, validate_package

SKILL = Path(__file__).resolve().parents[1] / "src/kiro_crew/builtin_skills/dashboard/SKILL.md"


def _section() -> str:
    text = SKILL.read_text("utf-8")
    start = text.index("## 8. Phone widgets")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def _example() -> dict:
    (block,) = re.findall(r"```json\n(.*?)\n```", _section(), flags=re.S)
    return json.loads(block)


def test_the_skill_example_validates():
    example = _example()
    package = {
        "kind": DASHBOARD_KIND,
        "bound_to": "crewmate:pebble",
        "model": example["model"],
        "view": {"html": '<p data-dashboard-field="open_prs"></p>'},
        "widgets": example["widgets"],
    }
    out = validate_package(package)
    assert [w["id"] for w in out["widgets"]] == ["build-health"]
    # Every block type the example shows is one the validator knows.
    assert {b["type"] for b in out["widgets"][0]["blocks"]} <= set(dw._BLOCKS)


def test_the_section_names_every_block_type_and_the_v1_rules():
    flat = " ".join(_section().split())
    for kind in dw._BLOCKS:
        assert f"| `{kind}` |" in flat, kind
    assert "up to four home-screen widgets" in flat and dw.MAX_WIDGETS == 4
    assert f"1 to {dw.MAX_BLOCKS} blocks" in flat
    assert f"1 to {dw.MAX_WIDGET_LABEL_LEN} characters" in flat
    assert f"at most {dw.MAX_UNIT_LEN}" in flat
    assert f"`{dw.RESERVED_WIDGET_ID}` widget" in flat
    assert "only the Bot's owner sees widgets" in flat
    assert "never values or HTML" in flat
    assert "plain text" in flat


def test_the_save_tool_describes_widgets_as_optional():
    (tool,) = [t for t in mcp_panel._list_tools() if t["name"] == "dashboard_save"]
    schema = tool["inputSchema"]
    assert "widgets" not in schema["required"]
    prop = schema["properties"]["widgets"]
    assert prop["type"] == "array" and prop["maxItems"] == dw.MAX_WIDGETS
    flat = " ".join(prop["description"].split())
    assert "owner only" in flat and "plain text" in flat and "pass [] to remove" in flat
    for kind in dw._BLOCKS:
        assert f"`{kind}`" in flat, kind
