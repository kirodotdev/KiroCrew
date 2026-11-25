"""Whether an unlanded turn's merge card counts as delivered, read as a capability.

``turn_stats._merge_cards_reached_the_model`` applies the Kiro "kept the prompt"
contract only where the turn's client has
``SessionCapabilities.keeps_prompt_on_unlanded_turn``. The per-backend answers
are pinned by ``test_harness_parity.test_merge_card_kept_prompt_rule_is_kiro_only``;
this file pins that the rule follows the field, not the backend's name.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from kiro_crew.acp_backends import ACP_BACKEND_DEEPSEEK
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.chat_runner import STOP_REASON_END_TURN
from kiro_crew.dashboard.chat_turn.turn_stats import _merge_cards_reached_the_model


def _unlanded_turn(client: object) -> SimpleNamespace:
    """A turn that left before landing after a real ``end_turn``."""
    return SimpleNamespace(
        client=client,
        _turn_lands=False,
        _empty_verdict_left_prompt_unanswered=False,
        _compaction_completed=False,
        _had_empty_response_verdict=False,
        _recovering_compaction=False,
        _stop_reason=STOP_REASON_END_TURN,
        _terminal_synthetic=False,
        _turn_emitted=True,
        needs_conversation_discard=False,
        needs_session_reset=False,
    )


def test_the_rule_follows_the_capability_not_the_backend_name() -> None:
    keeps = dataclasses.replace(
        capabilities_for(ACP_BACKEND_DEEPSEEK), keeps_prompt_on_unlanded_turn=True
    )
    client = SimpleNamespace(capabilities=keeps)
    assert _merge_cards_reached_the_model(_unlanded_turn(client)) is True


@pytest.mark.parametrize(
    "client",
    [
        SimpleNamespace(
            is_kiro_backend=True,
            capabilities=dataclasses.replace(
                capabilities_for(ACP_BACKEND_KIRO), keeps_prompt_on_unlanded_turn=False
            ),
        ),
        SimpleNamespace(),
        None,
    ],
    ids=["kiro-name-without-the-field", "no-capability-record", "no-client"],
)
def test_a_client_without_the_field_does_not_consume_the_card(client: object) -> None:
    assert _merge_cards_reached_the_model(_unlanded_turn(client)) is False
