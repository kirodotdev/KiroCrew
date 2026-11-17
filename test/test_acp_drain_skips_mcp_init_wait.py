"""A session on a host that never announces MCP servers skips the no-report wait.

``drain_init`` holds a session start and resume for up to
``_MCP_DRAIN_NO_REPORT_CEILING`` (6 s) waiting for the first MCP-init frame. Only
the kiro family (kiro-cli and KAS) names such a frame
(``notification_aliases.mcp_init``); codex-acp, claude-agent-acp and goose send
plain ``session/update`` only, so on them nothing can end the wait early and every
start paid the full ceiling. ``AcpRuntime._host_sends_mcp_init`` gates the three
drain sites on it.

What each test pins, and the one-line change that breaks it:

* ``test_only_a_host_that_names_an_mcp_init_frame_waits_for_one``: the flag per
  backend. Breaks if ``_host_sends_mcp_init`` is always True (the old wait).
* ``test_a_skipped_wait_still_records_the_specs_switched_off_servers``: the
  switched-off rows stay. Breaks if the host check is folded into
  ``_expect_mcp_reports`` itself, which also skips ``record_switched_off``.
* ``test_an_unknown_backend_keeps_the_wait``: an unknown name keeps waiting, so
  the spawn refuses it later with the harness's own error.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiro_crew.acp.runtime_class import AcpRuntime
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO


@pytest.mark.parametrize("backend", ["codex", "claude", "goose"])
def test_only_a_host_that_names_an_mcp_init_frame_waits_for_one(backend):
    assert AcpRuntime(work_dir="/w", acp_backend=backend)._host_sends_mcp_init is False
    for announcing in (ACP_BACKEND_KAS, ACP_BACKEND_KIRO):
        assert AcpRuntime(work_dir="/w", acp_backend=announcing)._host_sends_mcp_init is True


@pytest.mark.parametrize("backend", ["codex", "claude", "goose"])
def test_a_skipped_wait_still_records_the_specs_switched_off_servers(backend):
    rt = AcpRuntime(work_dir="/w", acp_backend=backend)
    assert rt._expect_mcp_reports is True
    handle = MagicMock()
    spec = MagicMock()
    rt._guard_unresolved_mcp_refs(handle, spec, "agent", [])
    handle.mcp_session_report.return_value.record_switched_off.assert_called_once_with(spec)


def test_an_unknown_backend_keeps_the_wait():
    from kiro_crew.acp.runtime_class import _host_announces_mcp_starts

    assert _host_announces_mcp_starts("no-such-backend") is True
