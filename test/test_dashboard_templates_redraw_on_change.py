"""Built-in boards redraw on the frame's change event, never on every message.

The frame fires ``kirocrew-dashboard`` only when a read moved something the page
shows. A board that redraws on any ``message`` instead wipes and rebuilds its
sections on every read, including the many that change nothing, and the reader sees
the page blink as if it reloaded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import dashboard_frame

_BUILTIN = Path(dashboard_frame.__file__).resolve().parent / "dashboard_templates" / "builtin"
_TEMPLATES = sorted(p.parent.name for p in _BUILTIN.glob("*/template.html"))


def test_the_builtin_boards_were_found() -> None:
    assert len(_TEMPLATES) >= 13, _TEMPLATES


@pytest.mark.parametrize("name", _TEMPLATES)
def test_a_board_does_not_redraw_on_every_message(name: str) -> None:
    html = (_BUILTIN / name / "template.html").read_text(encoding="utf-8").replace('"', "'")
    assert (
        "addEventListener('message'" not in html
    ), f"{name} redraws on every message, so a read that changed nothing rebuilds it"


@pytest.mark.parametrize("name", _TEMPLATES)
def test_a_board_redraws_on_the_change_event(name: str) -> None:
    html = (_BUILTIN / name / "template.html").read_text(encoding="utf-8").replace('"', "'")
    assert (
        f"addEventListener('{dashboard_frame.PAGE_EVENT}'" in html
    ), f"{name} does not listen for the frame's change event, so a refill never redraws it"
