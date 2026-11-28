"""The ``version_alignment:`` line of a bundle the dashboard collects.

Inside the gateway the one side-by-side update known is one the gateway staged
itself: the restart-ready note names it, no live-target pointer sends the next
start elsewhere, and a restart still runs it. Anything else is not checked.
"""

from __future__ import annotations

import time

import pytest

from kiro_crew import diagnostics, update_restart_ready
from kiro_crew.dashboard.handlers import diagnostics as dh
from kiro_crew.platform import wheel_apply
from kiro_crew.service import live_target


def _reaches_raises(version: str) -> bool:
    raise ValueError(version)


@pytest.mark.parametrize(
    ("staged", "pinned", "reaches", "compared"),
    [
        (True, False, lambda version: True, True),
        (True, False, lambda version: False, False),
        (True, True, lambda version: True, False),
        (True, False, _reaches_raises, False),
        (False, False, lambda version: True, False),
    ],
    ids=["staged", "link-moved-since", "live-target-pinned", "unreadable", "no-note"],
)
def test_the_dashboard_bundle_compares_an_update_the_gateway_staged(
    tmp_path, monkeypatch, staged, pinned, reaches, compared
) -> None:
    note = update_restart_ready.RestartReady(
        update_restart_ready.REASON_STAGED, "9.9.9", since=time.time() - 7200
    )
    monkeypatch.setattr(update_restart_ready, "current", lambda: note if staged else None)
    monkeypatch.setattr(live_target, "read_target", lambda: tmp_path if pinned else None)
    monkeypatch.setattr(wheel_apply, "restart_reaches", reaches)

    alignment = dh._staged_update_alignment()
    if not compared:
        assert alignment is None
        return
    assert alignment is not None
    assert alignment.startswith("version_skew for at least ")
    assert alignment.endswith(
        f": the gateway runs {diagnostics.__version__}, 9.9.9 is selected;"
        " restart the gateway to run it"
    )
