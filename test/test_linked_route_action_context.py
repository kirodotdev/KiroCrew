"""The opaque action payload survives a linked-thread click, on the builder channel only.

An `action::` click carries an agent-authored payload. On the ordinary Slack path it reaches
`build_message` as an ARGUMENT, never joined to the user's text. The linked-slot route has to
do the same: dropping it made a click on a linked thread lose context the identical click
keeps anywhere else, and joining it to the dispatched text would feed `$skill`/`@prompt`.
"""

from __future__ import annotations

import inspect

from kiro_crew.dashboard import chat_runner
from kiro_crew.slack import handler


def _route_src() -> str:
    return inspect.getsource(handler.maybe_route_linked_thread)


def _flat_runner() -> str:
    """Runner source with whitespace collapsed, so a formatter's line breaks cannot matter."""
    return " ".join(inspect.getsource(chat_runner).split())


class TestTheRouteAcceptsAndForwardsThePayload:
    def test_the_route_takes_the_payload(self):
        assert "action_context" in inspect.signature(handler.maybe_route_linked_thread).parameters

    def test_the_idle_dispatch_forwards_it(self):
        assert "_action_context=action_context or None" in _route_src()

    def test_the_busy_branch_carries_it_in_queue_meta(self):
        """A busy slot must not be the hole: the queued entry needs the same payload."""
        src = _route_src()
        assert '_queue_meta["action_context"] = action_context' in src
        assert "meta=_queue_meta" in src

    def test_the_caller_hands_it_over(self):
        assert "action_context=action_context" in inspect.getsource(handler.handle_message)


class TestTheRunnerRoutesItToTheBuilderOnly:
    def test_run_chat_accepts_the_payload(self):
        assert "_action_context" in inspect.signature(chat_runner._run_chat).parameters

    def test_it_reaches_the_context_builder(self):
        assert "action_context=_action_context or None" in _flat_runner()

    def test_the_drain_reads_it_back(self):
        flat = _flat_runner()
        assert '(item.get("meta") or {}).get("action_context")' in flat
        assert '"_action_context": action_context' in flat

    def test_a_merged_drain_keeps_every_payload_paired_with_its_own_label(self):
        """The drain must delegate to the pairing helper, not re-derive a first-wins pick.

        Behaviour is covered by the merged-clicks suite; this pins only that the drain still
        routes through the helper, so a later edit cannot quietly bypass the pairing.
        """
        assert "action_context = pair_action_contexts(consumed)" in _flat_runner()
        assert callable(chat_runner.pair_action_contexts)

    def test_the_payload_is_never_joined_to_the_dispatched_text(self):
        """The whole point: it must not become part of the string a click sends."""
        src = _route_src()
        assert "_dispatch_text" not in src
        for bad in ("text + action_context", "action_context + text", 'f"{action_context}'):
            assert bad not in src, bad
