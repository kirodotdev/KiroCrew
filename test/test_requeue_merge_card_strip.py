"""A requeued turn strips its merge-card frames in one shared step.

A turn that does not land puts its merge-card contexts back on the queue, and
the replay turn drains them, so a replay still carrying a card's frame would
hand the model the card twice. The strip is ``_strip_merge_cards``, which
``_queue_recovery`` runs before it queues; the replay arms' behaviour tests are
``TestVerbatimReplayMergeCards`` in ``test_chat_runner_coverage_merge_cards.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

from kiro_crew.dashboard.chat_turn.turn_ledgers import _strip_merge_cards


def test_strip_step_replaces_only_a_verbatim_requeue() -> None:
    turn = SimpleNamespace(message="[card]\nctx\nhi", _message_without_merge_cards="ctx\nhi")
    assert _strip_merge_cards(turn, "[card]\nctx\nhi") == "ctx\nhi"
    assert _strip_merge_cards(turn, "continue") == "continue"
    assert _strip_merge_cards(turn, "[card]\nctx\nhi", keep_merge_cards=True) == "[card]\nctx\nhi"
