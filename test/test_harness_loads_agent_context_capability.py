"""Which harnesses load the session's agent themselves, read as a capability.

``cli_chat_context.CliSessionContext.first_message`` sends the typed text as it
is, without Crew's session context, only where the provider's backend has
``SessionCapabilities.harness_loads_agent_context``. The answers are pinned per
backend here: the one the old ``is_kiro_backend`` or ``is_kas_backend`` check
gave. The CLI turn itself is driven in ``test_cli_chat_context.py``.
"""

from __future__ import annotations

import pytest

from kiro_crew.agent_sdk.backends import ACP_BACKENDS_KNOWN
from kiro_crew.agent_sdk.capabilities import capabilities_for, capabilities_of
from kiro_crew.providers.acp import AcpProvider


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_each_known_backend_keeps_its_answer(backend: str) -> None:
    provider = AcpProvider(acp_backend=backend)
    loads = capabilities_of(provider).harness_loads_agent_context
    assert loads is (provider.is_kiro_backend or provider.is_kas_backend)


def test_an_unknown_backend_does_not_load_the_agent_itself() -> None:
    caps = capabilities_for("some-harness-nobody-registered")
    assert caps.harness_loads_agent_context is False


def test_a_provider_with_no_capability_record_does_not_load_the_agent_itself() -> None:
    assert capabilities_of(object()).harness_loads_agent_context is False
