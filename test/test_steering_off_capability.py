"""Which harnesses honour a chat's per-file steering switch, read as a capability.

The context list offers the switch and Crew's steering load leaves switched-off
files out only where ``SessionCapabilities.honors_chat_steering_off`` is True. The
answers are pinned per provider label here, the ones the two readers gave when they
asked whether the label was Claude Code's.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew import steering_off
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CODEX, ACP_BACKENDS_KNOWN
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.agent_sdk.drivers.acp import provider_label_for_backend
from kiro_crew.agent_sdk.kas_tool_names import backend_of_provider_label

#: The one label the readers honoured before.
HONOURED = "claude_code"


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_each_known_backend_keeps_its_answer(backend: str) -> None:
    label = provider_label_for_backend(backend)
    assert steering_off.honoured_for(label) is (label == HONOURED)


@pytest.mark.parametrize("label", [None, "", "some-label-nobody-registered"])
def test_a_label_naming_no_backend_is_not_honoured(label) -> None:
    assert steering_off.honoured_for(label) is False


def test_an_unknown_backend_does_not_honour_the_switch() -> None:
    assert capabilities_for("some-harness-nobody-registered").honors_chat_steering_off is False


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_each_label_maps_back_to_its_backend(backend: str) -> None:
    assert backend_of_provider_label(provider_label_for_backend(backend)) == backend


def test_the_answer_follows_the_capability(monkeypatch) -> None:
    """Give codex the capability and a codex chat is offered the switch."""
    import kiro_crew.agent_sdk.capabilities as caps_mod

    real = caps_mod.capabilities_for

    def _codex_honours(backend: str):
        record = real(backend)
        if backend == ACP_BACKEND_CODEX:
            return dataclasses.replace(record, honors_chat_steering_off=True)
        return record

    monkeypatch.setattr(caps_mod, "capabilities_for", _codex_honours)
    assert steering_off.honoured_for(provider_label_for_backend(ACP_BACKEND_CODEX)) is True
