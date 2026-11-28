"""Which harnesses lend a custom agent kiro-cli's default resources, read as a capability.

Context assembly gives an unmapped custom agent kiro-cli's global and workspace
steering and default skill trees only where the backend behind the provider label
has ``SessionCapabilities.inherits_kiro_default_resources``. The answers are pinned
per backend here: the one the old ``provider_type == PROVIDER_ACP`` label check gave.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew import context
from kiro_crew.agent_sdk import capabilities as capabilities_module
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKENDS_KNOWN
from kiro_crew.agent_sdk.capabilities import UNKNOWN_BACKEND_CAPABILITIES, capabilities_for
from kiro_crew.agent_sdk.drivers.acp import provider_label_for_backend
from kiro_crew.agent_sdk.provider_identity import PROVIDER_ACP


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_each_known_backend_keeps_its_answer(backend: str) -> None:
    label = provider_label_for_backend(backend)
    old = label == PROVIDER_ACP
    assert capabilities_for(backend).inherits_kiro_default_resources is old
    assert context.label_inherits_kiro_defaults(label) is old


def test_an_unknown_backend_inherits_nothing() -> None:
    assert (
        capabilities_for("some-harness-nobody-registered").inherits_kiro_default_resources is False
    )
    assert UNKNOWN_BACKEND_CAPABILITIES.inherits_kiro_default_resources is False


@pytest.mark.parametrize("label", [None, "", "some-label-nobody-registered"])
def test_a_label_no_backend_carries_inherits_nothing(label: str | None) -> None:
    assert context.label_inherits_kiro_defaults(label) is False


def test_the_sites_follow_the_capability_not_the_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grant KAS the capability and the label check follows it."""
    real = capabilities_module.capabilities_for

    def granted(backend: str):
        caps = real(backend)
        if backend == ACP_BACKEND_KAS:
            return dataclasses.replace(caps, inherits_kiro_default_resources=True)
        return caps

    monkeypatch.setattr(capabilities_module, "capabilities_for", granted)
    monkeypatch.setattr(context.acp_driver, "inherits_default_resources", lambda _project: True)
    kas = provider_label_for_backend(ACP_BACKEND_KAS)

    assert context.label_inherits_kiro_defaults(kas) is True
    assert context._project_steering_delivered(kas, False, None) is True
