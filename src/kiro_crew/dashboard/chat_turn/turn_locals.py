"""The state one dashboard chat turn shares across its phases: :class:`_ChatTurn`.

``kiro_crew.dashboard.chat_runner._run_chat`` builds one ``_ChatTurn`` per call, as its first
statement, and every value of the turn that one of its closures writes, or reads after the turn
rebinds it, lives on it rather than in a local of one function: such an input of the turn, and
such a value the turn computes. A closure that writes such a value (the steer cut writes the
reply text from the steer handler's task) writes the attribute; the turn reads the attribute;
so every reader and writer shares one storage cell, as the closure cell it replaces did.

An attribute is declared on the class and set where the turn first binds the value, never
earlier, so reading one before the turn bound it raises ``UnboundLocalError``, with the message
reading the unbound local raised. A closure's read of a cell nothing had bound raised
``NameError``; ``UnboundLocalError`` is a ``NameError``, so a handler of either still catches it.
The declarations are strings: the class body evaluates nothing, so a test that rebinds a name on
the runner still reaches every reader.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

    from kiro_crew.dashboard.chat_runner import RefusalInfo


class _ChatTurn:
    """One turn's inputs and the state its phases share; see the module docstring."""

    # The inputs of ``_run_chat`` its closures read off the turn.
    message: "str"

    # State the turn's closures write, or read after the turn rebinds it.
    _consumed_reported: "bool"
    _crew_log_sid: "str"
    _crew_log_step: "int"
    _crew_log_turn_no: "int"
    _dispatch_lock_held: "bool"
    _irreversible_consumption_reported: "bool"
    _is_refusal_retry_turn: "bool"
    _produced_visible_output: "bool"
    _reply_flushed: "bool"
    _spec_hooks: "list[Any]"
    _spec_hooks_cwd: "str | None"
    _spec_hooks_unreadable: "bool"
    _stop_reason: "str"
    _terminal_directive_applied: "bool"
    _terminal_directive_kind: "str"
    _ttft_quiet_text: "bool"
    _turn_boundary_set: "bool"
    _turn_cost_usd: "float"
    _turn_credits: "float"
    _turn_elapsed_ms: "int"
    _turn_model: "str"
    _turn_msg_boundary: "int"
    _turn_project: "str"
    _turn_refusal: "RefusalInfo | None"
    _turn_stats0: "object"
    _turn_stats_captured: "bool"
    _turn_t0: "float"
    _turn_tool_calls: "int"
    _turn_usage_persisted: "bool"
    _wt_coded_this_turn: "bool"
    _wt_deciding_coding: "bool"
    _wt_last_cadence: "float"
    assistant_text: "str"
    chunk_seq: "int"
    client: "Any"
    selected_binding: "tuple[Any, ...]"

    def __init__(self, *, message: "str") -> None:
        self.message = message

    if not TYPE_CHECKING:

        def __getattr__(self, name: str) -> object:
            # Only reached for an attribute that was never set. A declared one is a
            # value the turn has not bound yet: raise what reading that local raised.
            if name in _ChatTurn.__annotations__:
                raise UnboundLocalError(
                    f"cannot access local variable {name!r} where it is not associated with a value"
                )
            raise AttributeError(name)
