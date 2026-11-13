"""``rebuild_agent_spec``: what a dashboard settings write reports after its spec rebuild.

The config write has already landed when the rebuild runs, so every outcome is a
boolean answer, never a raise. A ``PlatformCompositionError`` is told apart from an
ordinary failure in the gateway log only: ERROR, naming governance, because the
cause is this host's enterprise setup rather than a transient write.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.dashboard.handlers._shared import rebuild_agent_spec
from kiro_crew.platform import PlatformCompositionError

_LOGGER = "kiro_crew.dashboard.handlers._shared"


def _records(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [(r.levelname, r.getMessage()) for r in caplog.records if r.name == _LOGGER]


def test_a_composition_refused_rebuild_is_an_error_naming_governance(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _refused() -> tuple[Path, bool]:
        raise PlatformCompositionError("companion missing")

    monkeypatch.setattr(agent_mod, "rebuild_agent_config_reporting", _refused)
    caplog.set_level("WARNING", logger=_LOGGER)

    assert rebuild_agent_spec("MCP import") is False

    ((level, message),) = _records(caplog)
    assert level == "ERROR"
    assert message == (
        "agent spec was not rewritten after MCP import: this host's enterprise setup "
        "could not be built (governance)"
    )


def test_an_ordinary_rebuild_failure_stays_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _broken() -> tuple[Path, bool]:
        raise RuntimeError("disk full")

    monkeypatch.setattr(agent_mod, "rebuild_agent_config_reporting", _broken)
    caplog.set_level("WARNING", logger=_LOGGER)

    assert rebuild_agent_spec("MCP import") is False

    assert _records(caplog) == [("WARNING", "agent spec rebuild failed after MCP import")]


@pytest.mark.parametrize(("wrote", "expected"), [(True, True), (False, False)])
def test_a_completed_rebuild_reports_whether_it_wrote(
    monkeypatch: pytest.MonkeyPatch, wrote: bool, expected: bool
) -> None:
    monkeypatch.setattr(agent_mod, "rebuild_agent_config_reporting", lambda: (Path(), wrote))

    assert rebuild_agent_spec("MCP import") is expected
