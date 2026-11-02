"""Pin facts the packaged configuration reference states against the code that owns them.

``src/kiro_crew/docs/configuration.md`` ships with the package and running agents
read it, so a value that drifts from the code misleads an agent at run time.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO, BASELINE_SELECTABLE_BACKENDS
from kiro_crew.config.sections import _VALID_STT_PROVIDERS
from kiro_crew.effort import EFFORT_LEVELS

DOC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "docs" / "configuration.md"


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def _row(key: str) -> str:
    """The table row whose first cell is ``key``."""
    for line in _doc().splitlines():
        if line.startswith(f"| `{key}` |"):
            return line
    raise AssertionError(f"no table row for {key} in {DOC.name}")


def test_every_selectable_acp_backend_has_a_row_in_the_backend_table() -> None:
    section = _doc().split("## ACP Backend", 1)[1].split("\n## ", 1)[0]
    listed = set(re.findall(r"^\| `([a-z]+)` \|", section, re.MULTILINE))
    expected = set(BASELINE_SELECTABLE_BACKENDS) - {ACP_BACKEND_KIRO}
    assert expected <= listed, sorted(expected - listed)


def test_stt_provider_row_names_every_selectable_provider() -> None:
    row = _row("stt.provider")
    for provider in _VALID_STT_PROVIDERS:
        assert f'`"{provider}"`' in row, provider


def test_knowledge_extraction_effort_row_names_every_effort_level() -> None:
    row = _row("knowledge.extraction_effort")
    for level in EFFORT_LEVELS:
        assert f"`{level}`" in row, level


def test_dm_single_session_row_names_both_preconditions() -> None:
    import inspect

    from kiro_crew.slack import events

    src = inspect.getsource(events._dm_single_session_enabled)
    assert "use_transport" in src and "ACTIVATION_REVIEW" in src
    row = _row("slack.dm_single_session")
    assert "`messaging.use_transport`" in row
    assert "`review` mode" in row


def test_stt_rows_name_the_range_the_loader_clamps_into() -> None:
    from kiro_crew.stt import limits

    idle = _row("stt.idle_evict_secs")
    assert f"{limits.MIN_IDLE_EVICT_SECS}-{limits.MAX_IDLE_EVICT_SECS} s" in idle
    timeout = _row("stt.timeout_secs")
    assert f"{limits.MIN_TIMEOUT_SECS}-{limits.MAX_TIMEOUT_SECS} s" in timeout
    for row in (idle, timeout):
        assert "clamped into that range" in row


def test_restore_window_row_names_the_settings_range_and_no_limit() -> None:
    import inspect

    from kiro_crew.dashboard.file_api import dashboard_config

    src = inspect.getsource(dashboard_config)
    # The Settings save is the one place this key is bounded; the loader keeps it as given.
    assert re.search(
        r'max\(\s*0,\s*min\(1440,\s*body_int\(body\["restore_window_minutes"\]\)\s*\)', src
    )
    row = _row("dashboard.restore_window_minutes")
    assert "0-1440" in row
    assert "`0` = no limit" in row
