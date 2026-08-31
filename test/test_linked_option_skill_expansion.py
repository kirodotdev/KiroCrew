"""A clicked OPTIONS label must not load a local skill body.

`$skill` expansion is defined over what the USER typed -- the runner's own gate skips it
for `@prompt`-substituted text for exactly that reason. A label the model wrote is the same
case: `!dashboard $private-skill` arrives with `interpret_commands=False`, so the bang never
runs as a command, and the full label is dispatched as turn text instead. Without a
provenance flag that text reaches `$skill` expansion and the skill body goes to the model.
"""

from __future__ import annotations

import inspect

from kiro_crew.dashboard import chat_runner
from kiro_crew.slack import handler


def _flat() -> str:
    """Module source with all whitespace collapsed.

    The gates below are long boolean conditions, so the formatter decides whether each one
    sits on a single line or is split across several. Matching the collapsed form asserts the
    condition itself rather than the shape a formatter chose for it.
    """
    return " ".join(inspect.getsource(chat_runner).split())


class TestTheRunnerGateIsProvenanceAware:
    def test_run_chat_accepts_a_model_authored_flag(self):
        assert "_model_authored" in inspect.signature(chat_runner._run_chat).parameters

    def test_the_expansion_gate_consults_it(self):
        flat = _flat()
        marker = '"$" in message and not is_slash'
        assert marker in flat, "the expansion gate moved -- re-anchor this test"
        gate = flat[flat.index(marker) : flat.index(marker) + 160]
        assert "not _model_authored" in gate, gate

    def test_the_gate_still_skips_prompt_substituted_text(self):
        """The pre-existing arm must survive: this fix adds a reason, it replaces none."""
        flat = _flat()
        marker = '"$" in message and not is_slash'
        gate = flat[flat.index(marker) : flat.index(marker) + 160]
        assert "not prompt_expanded" in gate, gate
        assert "not is_slash" in gate, gate

    def test_the_at_prompt_expansion_consults_it_too(self):
        """Both expansions read local files, so both need the same provenance gate."""
        flat = _flat()
        marker = 'message.startswith("@") and not is_slash'
        assert marker in flat, "the at-expansion gate moved -- re-anchor this test"
        gate = flat[flat.index(marker) : flat.index(marker) + 160]
        assert "not _model_authored" in gate, gate


class TestTheLinkedRouteCarriesProvenance:
    def test_it_derives_the_flag_from_the_boundary_signal(self):
        src = inspect.getsource(handler.maybe_route_linked_thread)
        assert "_model_authored = not interpret_commands" in src

    def test_the_immediate_dispatch_forwards_it(self):
        src = inspect.getsource(handler.maybe_route_linked_thread)
        assert "_model_authored=_model_authored" in src

    def test_the_queued_dispatch_carries_it_too(self):
        """A busy slot must not be the hole: the queue entry needs the same provenance."""
        src = inspect.getsource(handler.maybe_route_linked_thread)
        assert '_queue_meta["model_authored"] = True' in src
        assert "meta=_queue_meta" in src

    def test_a_human_typing_in_a_linked_thread_keeps_expansion(self):
        """interpret_commands defaults True for a typed message, so the flag stays False."""
        params = inspect.signature(handler.maybe_route_linked_thread).parameters
        assert params["interpret_commands"].default is True


class TestTheDrainPropagatesIt:
    def test_the_drain_reads_the_queue_meta(self):
        src = inspect.getsource(chat_runner)
        assert '(item.get("meta") or {}).get("model_authored")' in src
        assert '"_model_authored": model_authored' in src
