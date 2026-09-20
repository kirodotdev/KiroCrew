"""A pre-output transient retry must not re-enable command dispatch.

A recovery entry replays the ORIGINAL text verbatim, so it is the same author's words. The
requeue builds fresh admission context, and that context must carry the turn's provenance:
without it the replay reads as user-authored, `_model_authored` arrives False, the leading
command word stays unblanked, and a clicked `/clear` label reaches the harness command arm on
the retry even though the first attempt refuses it.
"""

from __future__ import annotations

import inspect
import re

from kiro_crew.dashboard import chat_runner

SOURCE = inspect.getsource(chat_runner)


def _queue_recovery_body() -> str:
    """The nested requeue helper's own source, sliced to its `queue_insert` call."""
    start = SOURCE.index("def _queue_recovery(")
    end = SOURCE.index("Model-activity marker", start)
    return SOURCE[start:end]


class TestRecoveryKeepsModelProvenance:
    def test_the_slice_reaches_the_requeue(self):
        # Guards the guard: every assertion below is about this helper's own body.
        body = _queue_recovery_body()
        assert "queue_insert" in body
        assert "kind=kind" in body

    def test_the_requeue_carries_model_authored(self):
        body = _queue_recovery_body()
        assert '"model_authored"' in body, body[-900:]

    def test_the_requeue_carries_the_action_payload(self):
        body = _queue_recovery_body()
        assert '"action_context"' in body, body[-900:]

    def test_it_does_not_pass_bare_containment_meta(self):
        """The defect's exact shape: meta rebuilt with no provenance merged in."""
        body = _queue_recovery_body()
        assert not re.search(r"meta=containment_meta\(", body), body[-900:]

    def test_the_drain_reads_the_key_this_requeue_writes(self):
        """Both halves must name the SAME key, or preserving it changes nothing."""
        assert '.get("model_authored")' in SOURCE
        assert '.get("action_context")' in SOURCE

    def test_the_blanking_gate_still_keys_on_the_provenance(self):
        """Positive control on the consequence: provenance is what neutralises dispatch."""
        assert 'first_word = "" if _model_authored else' in SOURCE


class TestTheDrainRuleTheRequeueMustSatisfy:
    """Behavioural: the meta a requeue writes, read back by the real drain rule."""

    def test_a_preserved_flag_taints_the_replayed_turn(self):
        entry = {"content": "/clear", "meta": {"model_authored": True}}
        assert chat_runner.batch_is_model_authored([entry]) is True

    def test_meta_without_the_flag_reads_as_user_authored(self):
        """The defect, stated as behaviour: this is what a bare requeue produced."""
        entry = {"content": "/clear", "meta": {"containment": "x"}}
        assert chat_runner.batch_is_model_authored([entry]) is False

    def test_one_model_entry_taints_a_merged_batch(self):
        batch = [
            {"content": "hello", "meta": {}},
            {"content": "/clear", "meta": {"model_authored": True}},
        ]
        assert chat_runner.batch_is_model_authored(batch) is True

    def test_only_a_true_flag_counts(self):
        """A truthy string must not pass: the drain compares identity, not truthiness."""
        assert chat_runner.batch_is_model_authored([{"meta": {"model_authored": "yes"}}]) is False

    def test_a_missing_meta_is_not_an_error(self):
        assert chat_runner.batch_is_model_authored([{"content": "hi"}]) is False

    def test_the_payload_survives_the_same_requeue(self):
        entry = {"content": "Deploy it", "meta": {"action_context": "act::deploy"}}
        assert chat_runner.pair_action_contexts([entry]) == "act::deploy"
