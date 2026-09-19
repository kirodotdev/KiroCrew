"""Tests for the shared transport approval-mode resolver.

Every channel gateway needs the same decision: given the orchestrator's CLI
``--approval`` override, the runtime YOLO toggle, and the configured
``agent.approval_mode``, does this turn auto-approve or does it go interactive
(deny-by-default)? One shared implementation answers it, and each channel
reaches it through its own named seam.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.messaging.driver import (
    APPROVAL_AUTO,
    APPROVAL_INTERACTIVE,
    resolve_transport_approval_mode,
)


def _orch(**overrides):
    base = dict(
        _approval_mode=None,
        _cfg=SimpleNamespace(agent=SimpleNamespace(approval_mode=APPROVAL_INTERACTIVE)),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestResolveTransportApprovalMode:
    def test_cli_yolo_override_wins(self) -> None:
        assert resolve_transport_approval_mode(_orch(_approval_mode="yolo")) == APPROVAL_AUTO

    def test_cli_auto_override_resolves_auto(self) -> None:
        assert resolve_transport_approval_mode(_orch(_approval_mode=APPROVAL_AUTO)) == APPROVAL_AUTO

    def test_cli_interactive_override_resolves_interactive(self) -> None:
        assert (
            resolve_transport_approval_mode(_orch(_approval_mode=APPROVAL_INTERACTIVE))
            == APPROVAL_INTERACTIVE
        )

    def test_absent_override_falls_back_to_configured_auto(self) -> None:
        orch = _orch(_cfg=SimpleNamespace(agent=SimpleNamespace(approval_mode=APPROVAL_AUTO)))
        assert resolve_transport_approval_mode(orch) == APPROVAL_AUTO

    def test_absent_override_falls_back_to_configured_interactive(self) -> None:
        assert resolve_transport_approval_mode(_orch()) == APPROVAL_INTERACTIVE

    def test_unknown_mode_collapses_to_interactive(self) -> None:
        """Anything that is not exactly ``auto`` is deny-by-default."""
        assert (
            resolve_transport_approval_mode(_orch(_approval_mode="something_else"))
            == APPROVAL_INTERACTIVE
        )

    def test_runtime_yolo_probe_wins_over_the_absent_override(self) -> None:
        """A channel that has a runtime YOLO toggle folds it in here."""
        orch = _orch(_approval_mode=None)
        assert resolve_transport_approval_mode(orch, yolo_probe=lambda: True) == APPROVAL_AUTO

    def test_cli_override_beats_an_inactive_runtime_yolo_probe(self) -> None:
        orch = _orch(_approval_mode=APPROVAL_INTERACTIVE)
        assert (
            resolve_transport_approval_mode(orch, yolo_probe=lambda: False) == APPROVAL_INTERACTIVE
        )

    def test_missing_override_attribute_is_tolerated(self) -> None:
        """Duck-typed orchestrators: no ``_approval_mode`` attribute at all."""
        orch = SimpleNamespace(
            _cfg=SimpleNamespace(agent=SimpleNamespace(approval_mode=APPROVAL_AUTO))
        )
        assert resolve_transport_approval_mode(orch) == APPROVAL_AUTO

    def test_yolo_probe_that_raises_does_not_break_resolution(self) -> None:
        """A broken runtime toggle must not take the whole turn down."""

        def _boom() -> bool:
            raise RuntimeError("probe unavailable")

        assert (
            resolve_transport_approval_mode(_orch(_approval_mode=APPROVAL_AUTO), yolo_probe=_boom)
            == APPROVAL_AUTO
        )


@pytest.mark.parametrize(
    "module_name",
    [
        "kiro_crew.discord.gateway",
        "kiro_crew.feishu.gateway",
        "kiro_crew.imessage.gateway",
        "kiro_crew.teams.gateway",
        "kiro_crew.telegram.gateway",
        "kiro_crew.webex.gateway",
        "kiro_crew.wecom.gateway",
        "kiro_crew.weixin.gateway",
        "kiro_crew.whatsapp.gateway",
    ],
)
def test_each_gateway_keeps_its_named_seam(module_name: str) -> None:
    """The per-channel name must stay importable.

    Channel tests import ``_resolve_approval_mode`` from the gateway module
    itself, so the shared helper is reached *through* that seam rather than
    replacing it.
    """
    module = pytest.importorskip(module_name)
    assert callable(module._resolve_approval_mode)


@pytest.mark.parametrize(
    "module_name",
    [
        "kiro_crew.discord.gateway",
        "kiro_crew.feishu.gateway",
        "kiro_crew.imessage.gateway",
        "kiro_crew.teams.gateway",
        "kiro_crew.telegram.gateway",
        "kiro_crew.webex.gateway",
        "kiro_crew.wecom.gateway",
        "kiro_crew.weixin.gateway",
        "kiro_crew.whatsapp.gateway",
    ],
)
def test_gateway_seam_delegates_to_the_shared_helper(module_name: str) -> None:
    """Every channel resolves the same way as the shared implementation."""
    module = pytest.importorskip(module_name)
    orch = _orch(_approval_mode="yolo")
    assert module._resolve_approval_mode(orch) == APPROVAL_AUTO
    orch = _orch(_approval_mode="interactive")
    assert module._resolve_approval_mode(orch) == APPROVAL_INTERACTIVE
