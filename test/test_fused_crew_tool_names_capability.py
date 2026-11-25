"""Which harnesses name a Crew tool only by a fused name, read as a capability.

``subagent_manager.run_turn`` reads the servers a session handed the harness, at
each tool dispatch, only where the run's backend has
``SessionCapabilities.names_crew_tools_by_fused_name``. The answers are pinned per
backend here: the one the old ``_harness in (ACP_BACKEND_PI, ACP_BACKEND_CLAUDE)``
check gave. The run loop itself is driven in ``test_pi_claude_wait_identity.py``.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_PI
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_KNOWN
from kiro_crew.agent_sdk.capabilities import capabilities_for


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_each_known_backend_keeps_its_answer(backend: str) -> None:
    fused = capabilities_for(backend).names_crew_tools_by_fused_name
    assert fused is (backend in (ACP_BACKEND_PI, ACP_BACKEND_CLAUDE))


def test_an_unknown_backend_does_not_name_crew_tools_by_fused_name() -> None:
    assert (
        capabilities_for("some-harness-nobody-registered").names_crew_tools_by_fused_name is False
    )
