"""Which harnesses end a turn on a rejected permission request, read as a capability.

``subagent_manager.run_turn`` reads a clean ``end_turn`` right after a host
refusal as the turn ending on that refusal only where the client has
``SessionCapabilities.ends_turn_on_permission_reject``. The answers are pinned per
backend here: the one the old ``is_opencode_backend`` check gave. The resume
itself is driven in ``test_subagent_refused_turn_resume.py``.
"""

from __future__ import annotations

import pytest

from kiro_crew.agent_sdk.backends import ACP_BACKENDS_KNOWN
from kiro_crew.agent_sdk.capabilities import capabilities_for, capabilities_of
from kiro_crew.providers.acp import AcpProvider


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_each_known_backend_keeps_its_answer(backend: str) -> None:
    provider = AcpProvider(acp_backend=backend)
    ends = capabilities_of(provider).ends_turn_on_permission_reject
    assert ends is provider.is_opencode_backend


def test_an_unknown_backend_does_not_end_the_turn_on_a_reject() -> None:
    caps = capabilities_for("some-harness-nobody-registered")
    assert caps.ends_turn_on_permission_reject is False


def test_a_client_with_no_capability_record_does_not_end_the_turn_on_a_reject() -> None:
    assert capabilities_of(object()).ends_turn_on_permission_reject is False
