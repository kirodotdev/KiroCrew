"""Issue Radar's ``read_signals`` signals a read-degrade through ``read_json_or``.

``test/test_read_json_or_signal.py`` pins ``atomic_write.read_json_or`` for the
core, with the fixtures and the Windows sharing-violation simulator this reuses.
This is one converted site proving the wiring end to end: before the conversion its
``except (OSError, json.JSONDecodeError): return {}`` folded the Windows
sharing-violation window into the same silent ``{}`` a genuinely absent cache
returns.
"""

from __future__ import annotations

import json
import logging

from test_read_json_or_signal import _fresh_dedup_state, _no_backoff_sleep  # noqa: F401
from windows_sim import read_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat
from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime


def test_converted_site_read_signals_emits_the_degrade(tmp_path, monkeypatch, caplog):
    """``read_signals`` (site 14 of the conversion) signals a read-degrade."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "on_event_loop", lambda: True)
    path = crew_runtime.signals_path("o", "r", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": crew_runtime.SIGNALS_SCHEMA, "items": {}}), "utf-8")

    with caplog.at_level(logging.WARNING, logger=crew_runtime.logger.name):
        with read_sharing_violation(match=path.name, times=10_000):
            result = crew_runtime.read_signals("o", "r", tmp_path)

    assert result == {}  # unchanged fallback
    recs = [r for r in caplog.records if r.name == crew_runtime.logger.name]
    assert len(recs) == 1
    assert "outcome=default_returned" in recs[0].getMessage()
