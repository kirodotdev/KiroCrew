"""Shared readers for the macOS lane's workflow source.

The repo's convention for anything two test modules both need is a dedicated
``*_helpers.py`` imported by BARE name (see ``chat_test_helpers``,
``mcp_merge_helpers``): no ``test_*`` module imports another, and a
``from test.test_x import ...`` form does not resolve under CI's rootdir at all,
because ``test`` is not an importable package there and is a CPython stdlib name
besides. Both lane test modules read the same workflow, so the readers live here
rather than being duplicated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def load_workflow(name: str) -> dict[str, Any]:
    """One workflow document, parsed from source."""
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def verdict_script() -> str:
    """The `decide` job's verdict step, as bash, so its logic is executed not read."""
    steps = load_workflow("macos-on-demand.yml")["jobs"]["decide"]["steps"]
    return next(step["run"] for step in steps if step.get("id") == "verdict")
