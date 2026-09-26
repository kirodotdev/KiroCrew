"""The client silence watchdog and the gateway status cadence are one contract.

``website/src/hooks/useWebSocket.ts`` tears down a visible page's socket once
``WS_SILENCE_MS`` passes with no frame delivered. It can do so only because
``dashboard/ws.py`` pushes a ``dashboard`` status frame on every socket every
``_WS_STATUS_INTERVAL`` seconds, so a healthy socket is never silent that long.
Neither side imports the other's number. Raise the cadence past the window and
every healthy socket falls silent for long enough to be replaced, the
replacement does the same, and each connected dashboard cycles through a full
catch-up forever with nothing red on either side. This pin fails instead: the
window must span at least three status intervals, so one delayed tick never
trips the watchdog and a slower cadence cannot make every dashboard reconnect
in a loop.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kiro_crew.dashboard import ws

_REPO = Path(__file__).resolve().parents[1]
_USE_WEBSOCKET_TS = _REPO / "website" / "src" / "hooks" / "useWebSocket.ts"

#: Exported integer constants as the hook spells them. Anchored to the line
#: so comments, differently named exports, and computed values are misses; the
#: digits allow TypeScript numeric separators.
_FRONTEND_MS_LINES = {
    "WS_SILENCE_MS": re.compile(
        r"^export const WS_SILENCE_MS = (\d+(?:_\d+)*);?\s*$", re.MULTILINE
    ),
    "WS_SILENCE_CHECK_MS": re.compile(
        r"^export const WS_SILENCE_CHECK_MS = (\d+(?:_\d+)*);?\s*$", re.MULTILINE
    ),
}
_THAW_GRACE_ASSIGNMENT = re.compile(
    r"^[ \t]*graceUntilRef\.current = now - lastFrameAtRef\.current "
    r"> silenceWindowMs\(\)\n"
    r"^[ \t]*\? 0\n"
    r"^[ \t]*: now \+ WS_SILENCE_CHECK_MS \* 2[ \t]*$",
    re.MULTILINE,
)

#: A single late status tick must never read as silence, so the window has to
#: hold more than two ticks; three is the smallest whole count with that slack.
_MIN_TICKS_IN_WINDOW = 3


def _frontend_ms(source: str, name: str) -> int:
    """An exported integer timing constant, or a loud failure."""
    hits = _FRONTEND_MS_LINES[name].findall(source)
    if len(hits) != 1:
        pytest.fail(
            f"expected exactly one `export const {name} = <int>` line in "
            f"{_USE_WEBSOCKET_TS}, found {len(hits)}; the frontend timing "
            f"contract pinned against `_WS_STATUS_INTERVAL` could not be read"
        )
    return int(hits[0].replace("_", ""))


def test_minimum_tick_floor_stays_three():
    assert _MIN_TICKS_IN_WINDOW == 3, (
        "the silence window must survive two lost or late status frames, "
        "so its floor is three ticks"
    )


def test_silence_window_spans_at_least_three_status_ticks():
    source = _USE_WEBSOCKET_TS.read_text(encoding="utf-8")
    silence_ms = _frontend_ms(source, "WS_SILENCE_MS")
    interval_s = ws._WS_STATUS_INTERVAL
    assert interval_s > 0, "the status push cadence must be a positive number of seconds"
    floor_ms = _MIN_TICKS_IN_WINDOW * interval_s * 1000
    assert silence_ms >= floor_ms, (
        f"WS_SILENCE_MS={silence_ms}ms in useWebSocket.ts spans fewer than "
        f"{_MIN_TICKS_IN_WINDOW} status ticks of _WS_STATUS_INTERVAL={interval_s}s "
        f"(needs >= {floor_ms}ms). A status cadence slower than the silence window "
        f"makes every healthy socket look dead, so every connected dashboard "
        f"reconnects in a loop with nothing red on either side; change the two "
        f"together or not at all"
    )


def test_thaw_grace_covers_one_status_interval():
    source = _USE_WEBSOCKET_TS.read_text(encoding="utf-8")
    check_ms = _frontend_ms(source, "WS_SILENCE_CHECK_MS")
    grace_hits = _THAW_GRACE_ASSIGNMENT.findall(source)
    if len(grace_hits) != 1:
        pytest.fail(
            "expected exactly one hidden-to-visible grace assignment ending in "
            "`now + WS_SILENCE_CHECK_MS * 2`; the thaw grace contract could not be read"
        )
    interval_ms = ws._WS_STATUS_INTERVAL * 1000
    assert 2 * check_ms >= interval_ms, (
        f"the hidden-to-visible grace is {2 * check_ms}ms, shorter than "
        f"_WS_STATUS_INTERVAL={interval_ms}ms; a thawed live socket must have time "
        f"to deliver its next status frame before replacement"
    )


def test_a_renamed_or_computed_constant_fails_the_pin_loudly():
    """Guard the guard: the parser must not skip a source that lost the line."""
    with pytest.raises(pytest.fail.Exception, match="exactly one"):
        _frontend_ms("export const WS_SILENCE_WINDOW_MS = 20_000\n", "WS_SILENCE_MS")
    with pytest.raises(pytest.fail.Exception, match="exactly one"):
        _frontend_ms(
            "export const WS_SILENCE_MS = 4 * WS_SILENCE_CHECK_MS\n",
            "WS_SILENCE_MS",
        )
    assert _frontend_ms("export const WS_SILENCE_MS = 20_000\n", "WS_SILENCE_MS") == 20_000
