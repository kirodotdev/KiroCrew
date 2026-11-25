"""Which harnesses move a live session to Auto in place, read as a capability.

``model_fallback._reassert_explicit_auto``, ``slot_model._wire_model_id`` and
``slot_model._auto_repick_moves_session`` send an Auto pick as a live switch only
where the provider's backend has ``SessionCapabilities.model_auto_switches_in_place``.
The answers are pinned per backend here: the one the old ``is_kas_backend`` check
gave. The three readers are driven in ``test_settle_session_model_deferred_pick.py``
and ``test_dashboard_chat_model_live_switch.py``.
"""

from __future__ import annotations

import pytest

from kiro_crew.agent_sdk.backends import ACP_BACKENDS_KNOWN
from kiro_crew.agent_sdk.capabilities import capabilities_for, capabilities_of
from kiro_crew.providers.acp import AcpProvider


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_each_known_backend_keeps_its_answer(backend: str) -> None:
    provider = AcpProvider(acp_backend=backend)
    switches = capabilities_of(provider).model_auto_switches_in_place
    assert switches is provider.is_kas_backend


def test_an_unknown_backend_does_not_switch_auto_in_place() -> None:
    caps = capabilities_for("some-harness-nobody-registered")
    assert caps.model_auto_switches_in_place is False


def test_a_provider_with_no_capability_record_does_not_switch_auto_in_place() -> None:
    assert capabilities_of(object()).model_auto_switches_in_place is False
