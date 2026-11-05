"""A directive tool must not promise a message its own result cannot carry.

monitor_start and monitor_update hand their request back to the gateway inside
their tool result, and :func:`session_directive.encode` refuses a result longer
than :data:`session_directive.MAX_DIRECTIVE_CHARS` encoded characters. The
confirmation text, the other arguments and JSON escaping share that budget. So
the size a tool description promises for ``message`` has to be a size that
actually fits, and an over-budget refusal has to say how much to cut: the
encoded total alone does not tell a caller how long its message may be.
"""

from __future__ import annotations

import re

import pytest

from kiro_crew import mcp_core, session_directive
from kiro_crew.constants import MAX_BANNER_CHARS
from kiro_crew.mcp_tools import control
from kiro_crew.mcp_tools.control_parts.schemas import schemas

# Instruction-shaped text. Naming one pull request once makes monitor_start gate the
# loop, which has the longest confirmation; quotes and newlines each encode to two
# characters. The filler names no URL, so a long message still names one subject.
_HEAD = "Watch https://github.com/acme/widgets/pull/42. "
_FILLER = 'If the "build" check fails,\nread its log and report the failing step. '
_SIZE = re.compile(r"(\d[\d,]*)\s*(?:chars|characters)\b")


@pytest.fixture()
def bound_session(monkeypatch):
    """Make the handlers believe they run on a session that can carry a loop."""
    monkeypatch.setattr(
        mcp_core, "require_strict_session_key", lambda *_a, **_k: ("dashboard:1", None)
    )
    monkeypatch.setattr(mcp_core, "_autonudge_binding_key", lambda sk: "chat-1-123")
    monkeypatch.setattr(mcp_core, "_structured_monitor_binding_key", lambda sk: "chat-1-123")


def _message_description(tool: str) -> str:
    spec = next(s for s in schemas() if s["name"] == tool)
    return spec["inputSchema"]["properties"]["message"]["description"]


def _advertised_size(tool: str) -> int:
    """The first size the ``message`` description states: the promise a caller reads."""
    found = _SIZE.search(_message_description(tool))
    assert found, f"{tool}'s message description states no size limit"
    return int(found.group(1).replace(",", ""))


def _text(size: int) -> str:
    return (_HEAD + _FILLER * (size // len(_FILLER) + 1))[:size]


def _start(message: str, **extra) -> str:
    args = {"message": message, "interval_secs": 300, "max_cycles": 24, **extra}
    return control.monitor_start("monitor_start", args)


@pytest.mark.parametrize("extra", [{}, {"gate": False}], ids=["gated", "ungated"])
def test_monitor_start_accepts_a_message_of_the_advertised_size(bound_session, extra):
    size = _advertised_size("monitor_start")
    out = _start(_text(size), **extra)
    assert session_directive.has_marker(out), (
        f"the description promises {size} characters, but a message that long is "
        f"refused: {out[:200]}"
    )
    if "gate" not in extra:
        assert "acme/widgets#42" in out, "the gated case must arm an observed loop"


def test_monitor_start_accepts_the_advertised_size_with_the_longest_banner(bound_session):
    size = _advertised_size("monitor_start")
    out = _start(_text(size), banner="b" * MAX_BANNER_CHARS)
    assert session_directive.has_marker(out), (
        f"a {size}-character message with a {MAX_BANNER_CHARS}-character banner is "
        f"refused: {out[:200]}"
    )
    assert "acme/widgets#42" in out, "the case must arm an observed loop"


def test_monitor_update_accepts_a_message_of_the_advertised_size(bound_session):
    size = _advertised_size("monitor_update")
    out = control.monitor_update("monitor_update", {"message": _text(size)})
    assert session_directive.has_marker(out), (
        f"the description promises {size} characters, but a message that long is "
        f"refused: {out[:200]}"
    )


@pytest.mark.parametrize(("char", "count"), [("a", 5227), ("检", 1000)], ids=["ascii", "non-ascii"])
def test_an_oversized_refusal_says_how_much_to_cut(bound_session, char, count):
    message = char * count
    out = _start(message, gate=False)
    assert out.startswith("Error:") and session_directive.is_refusal(out)
    assert "`message`" in out, "the refusal must name the argument to shorten"
    found = re.search(r"by at least ([\d,]+) characters", out)
    assert found, f"the refusal must say how much to cut: {out[:300]}"
    cut = int(found.group(1).replace(",", ""))
    assert cut <= len(message), "the cut must be one the caller can make"
    assert session_directive.has_marker(
        _start(message[:-cut], gate=False)
    ), "cutting that much fits"
    assert not session_directive.has_marker(
        _start(message[: -(cut - 1)], gate=False)
    ), "and the figure is not padded: one character less than it is still refused"


def test_a_monitor_update_refusal_names_the_field_inside_its_patch(bound_session):
    out = control.monitor_update("monitor_update", {"message": "a" * 4000})
    assert session_directive.is_refusal(out)
    assert "`message` (4000 chars)" in out, f"the refusal must name the caller's field: {out[:300]}"
    assert "`patch`" not in out


def test_the_refusal_names_the_largest_argument_of_any_directive_tool():
    items = [{"title": "Next", "description": "Do it.", "prompt": "p" * 5000}]
    out = session_directive.encode("suggest_followup", {"items": items}, "Card requested.")
    assert session_directive.is_refusal(out)
    assert "`items`" in out, "the refusal must name the argument to shorten"
