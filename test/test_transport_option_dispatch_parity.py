"""Every transport dispatch is command-interpreted only for user-authored text.

The Slack half of this rule has its own suite. This one covers the other six
transports, because the guard the badge feature relies on -- an option label is
turn content, never permission to execute -- is only as good as its least-covered
transport, and a dispatch site added later anywhere in any of them reopens the
promotion path silently.

Two enclosing functions legitimately keep interpretation on, because the text they
dispatch is one the USER authored, so a command inside it is the user's own:

* ``_handle_busy`` -- their message, re-dispatched once the turn is no longer busy.
* ``_on_command_interaction`` -- their Discord slash command, replayed as ``!name``
  through the text path that owns the governance recheck.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

TRANSPORTS = ("telegram", "discord", "teams", "webex", "wecom", "weixin")

# Interpretation stays ON only where the dispatched text is the user's own.
USER_AUTHORED_FUNCTIONS = {"_handle_busy", "_on_command_interaction"}

# Transports with a widget the model's own labels come back through.
WIDGET_TRANSPORTS = ("telegram", "discord", "teams", "webex")


def _modules() -> list[tuple[str, str]]:
    out = []
    for name in TRANSPORTS:
        path = SRC / name / "transport_dispatch.py"
        if path.exists():
            out.append((name, path.read_text(encoding="utf-8")))
    return out


def _dispatch_sites(text: str) -> list[tuple[int, str, bool]]:
    """Return ``(line, enclosing_function, is_gated)`` for each real dispatch call."""
    lines = text.splitlines()
    sites = []
    for m in re.finditer(r"self\.handle_message\(", text):
        i = m.end() - 1
        depth = 0
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        gated = "interpret_commands=False" in text[m.end() : i]
        line = text.count("\n", 0, m.start())
        func = "?"
        for j in range(line, -1, -1):
            g = re.match(r"\s*(?:async )?def (\w+)", lines[j])
            if g:
                func = g.group(1)
                break
        sites.append((line + 1, func, gated))
    return sites


class TestEveryTransportDispatchIsClassified:
    def test_the_scan_finds_all_six_transports(self):
        found = [name for name, _ in _modules()]
        assert found == list(TRANSPORTS), found

    @pytest.mark.parametrize("name", WIDGET_TRANSPORTS)
    def test_each_widget_transport_has_a_gated_dispatch(self, name):
        # Vacuity guard: a scan that finds no gated site would pass the rule below
        # while proving nothing about the transports that render option buttons.
        text = dict(_modules())[name]
        assert any(gated for _, _, gated in _dispatch_sites(text)), name

    @pytest.mark.parametrize("name", TRANSPORTS)
    def test_no_dispatch_interprets_model_authored_text(self, name):
        text = dict(_modules())[name]
        offenders = [
            f"{name}/transport_dispatch.py:{line} in {func}"
            for line, func, gated in _dispatch_sites(text)
            if not gated and func not in USER_AUTHORED_FUNCTIONS
        ]
        assert not offenders, (
            "dispatch with commands interpreted and no user-authored provenance: "
            + "; ".join(offenders)
            + " -- pass interpret_commands=False, or declare the function in "
            "USER_AUTHORED_FUNCTIONS with the reason its text is the user's own"
        )

    def test_every_declared_exception_is_still_used(self):
        # An exception nobody exercises is a hole left open for no reason.
        seen = {func for _, text in _modules() for _, func, _ in _dispatch_sites(text)}
        assert USER_AUTHORED_FUNCTIONS <= seen, USER_AUTHORED_FUNCTIONS - seen
