"""PPTX Maker's bundled agents ship a ``welcomeMessage`` the core's reader renders.

``agent_discovery.spec_welcome_message`` is the field's only reader, and its rules
(coercion, the length ceiling) are pinned in ``test/test_agent_welcome_message.py``.
This pins the app's side: at least one of its shipped agent specs carries a hint
that reader returns, so switching to that agent shows the user what to do first.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.agent_discovery import spec_welcome_message

_AGENTS = Path(__file__).resolve().parents[1] / "agents"


def test_the_bundled_agents_ship_a_hint_the_reader_renders() -> None:
    specs = sorted(_AGENTS.glob("*.json"))
    assert specs, f"no bundled agent specs under {_AGENTS}"
    rendered = {
        p.stem: spec_welcome_message(json.loads(p.read_text(encoding="utf-8"))) for p in specs
    }
    assert [
        stem for stem, text in rendered.items() if text
    ], f"no bundled agent ships a welcomeMessage the reader renders: {rendered}"
