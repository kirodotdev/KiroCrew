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


#: Boards that listen for ONE typed reply from the host, which the frame's change event
#: cannot carry: the host answers a click on the page (the org-chart's "open worker chat"
#: button) with its own message, and the page can only hear that through ``message``.
#: Allowed only while the listener ignores every other message before it does anything:
#: the sender check and the type guard come first, and nothing in front of the guard redraws.
_TYPED_REPLY_LISTENERS = {"org-chart": "kirocrew-dashboard:open-session-result"}
_LISTENER = "addEventListener('message'"


@pytest.mark.parametrize("name", _TEMPLATES)
def test_a_board_does_not_redraw_on_every_message(name: str) -> None:
    html = (_BUILTIN / name / "template.html").read_text(encoding="utf-8").replace('"', "'")
    if name in _TYPED_REPLY_LISTENERS:
        return
    assert (
        _LISTENER not in html
    ), f"{name} redraws on every message, so a read that changed nothing rebuilds it"


@pytest.mark.parametrize("name", sorted(_TYPED_REPLY_LISTENERS))
def test_a_typed_reply_listener_ignores_every_other_message(name: str) -> None:
    html = (_BUILTIN / name / "template.html").read_text(encoding="utf-8").replace('"', "'")
    assert html.count(_LISTENER) == 1, f"{name} may hold exactly one message listener"
    body = html[html.index(_LISTENER) :]
    guard = f"d.type !== '{_TYPED_REPLY_LISTENERS[name]}') return;"
    assert (
        guard in body
    ), f"{name}'s message listener must return unless the message is its typed reply"
    before_guard = body[: body.index(guard)]
    assert (
        "render(" not in before_guard
    ), f"{name}'s message listener redraws before checking the message type"


@pytest.mark.parametrize("name", _TEMPLATES)
def test_a_board_redraws_on_the_change_event(name: str) -> None:
    html = (_BUILTIN / name / "template.html").read_text(encoding="utf-8").replace('"', "'")
    assert (
        f"addEventListener('{dashboard_frame.PAGE_EVENT}'" in html
    ), f"{name} does not listen for the frame's change event, so a refill never redraws it"
