"""Which harnesses get Crew's "may need a sign-in" row, read as a capability.

``mcp_session._notice_mcp_may_need_sign_in`` reads the report's
``sign_in_suspected`` list only where the provider's backend has
``SessionCapabilities.reports_mcp_sign_in_suspects``. The answers are pinned per
backend here: the one the old ``capabilities_of(provider).backend ==
ACP_BACKEND_OPENCODE`` check gave. The row itself is driven in
``test_opencode_mcp_sign_in_probe.py``.
"""

from __future__ import annotations

import pytest

from kiro_crew.agent_sdk.backends import ACP_BACKEND_OPENCODE, ACP_BACKENDS_KNOWN
from kiro_crew.agent_sdk.capabilities import capabilities_for, capabilities_of


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_each_known_backend_keeps_its_answer(backend: str) -> None:
    caps = capabilities_for(backend)
    assert caps.reports_mcp_sign_in_suspects is (caps.backend == ACP_BACKEND_OPENCODE)


def test_an_unknown_backend_reports_no_sign_in_suspects() -> None:
    caps = capabilities_for("some-harness-nobody-registered")
    assert caps.reports_mcp_sign_in_suspects is False


def test_a_provider_with_no_capability_record_reports_no_sign_in_suspects() -> None:
    assert capabilities_of(object()).reports_mcp_sign_in_suspects is False
