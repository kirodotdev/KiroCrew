"""The state one dashboard chat turn shares across its phases: :class:`_ChatTurn`.

``kiro_crew.dashboard.chat_runner._run_chat`` builds one ``_ChatTurn`` per call, as its first
statement, and every value of the turn that a function it calls with the turn writes, or reads
after the turn rebinds it, lives on it rather than in a local of one function: such an input of
the turn, and such a value the turn computes. A function that writes such a value (the steer cut
writes the reply text from the steer handler's task) writes the attribute; the turn reads the
attribute; so every reader and writer shares one storage cell.

An attribute is declared on the class and set where the turn first binds the value, never
earlier, so reading one before the turn bound it raises ``UnboundLocalError``, with the message
reading the unbound local raised. A read of a closure cell nothing had bound raised
``NameError``; ``UnboundLocalError`` is a ``NameError``, so a handler of either still catches it.
The declarations are strings: the class body evaluates nothing, so a test that rebinds a name on
the runner still reaches every reader.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from asyncio import Task
    from asyncio.locks import Lock
    from collections.abc import Awaitable, Callable
    from typing import Any

    from kiro_crew.dashboard.chat_runner import DashboardState, RefusalInfo, _ChatSlot
    from kiro_crew.dashboard.chat_turn.turn_stats import _FirstVisibleClock
    from kiro_crew.dashboard.poisoned_escalation import PoisonedConversationEscalation
    from kiro_crew.dashboard.responsive_chat import ResponsiveTurn
    from kiro_crew.security import StreamRedactor


class _ChatTurn:
    """One turn's inputs and the state its phases share; see the module docstring."""

    # The inputs of ``_run_chat`` the functions it calls with the turn read off it.
    state: "DashboardState"
    slot: "_ChatSlot"
    message: "str"
    _prompt_depth: "int"
    _attachments: "tuple[str, ...] | list[str]"
    _attachment_meta: "dict[str, list[str]] | None"
    _directive_user_origin: "bool"
    _turn_provenance_restored: "bool"
    _directive_self_wake: "bool"
    _directive_loop_id: "str"
    _directive_loop_gen: "int"
    _directive_channel_origin: "bool"
    _commands_off: "bool"
    _on_consumed: "Callable[[bool], None] | None"
    _on_irreversibly_consumed: "Callable[[], Awaitable[None] | None] | None"
    _current_message: "dict | None"

    # State those functions write, or read after the turn rebinds it.
    _consumed_reported: "bool"
    _crew_log_actor: "str"
    _crew_log_sid: "str"
    _crew_log_step: "int"
    _crew_log_turn_no: "int"
    _current_replay_message: "dict[Any, Any] | None"
    _dispatch_lock: "Lock"
    _dispatch_lock_held: "bool"
    _escalation: "PoisonedConversationEscalation"
    _irreversible_consumption_reported: "bool"
    _is_refusal_retry_turn: "bool"
    _is_synthetic: "bool"
    _owner_host_allowed: "bool"
    _owner_project: "Any | None"
    _produced_visible_output: "bool"
    _recovery_turn_id: "str"
    _reply_flushed: "bool"
    _responsive_owner: "Task[Any] | None"
    _responsive_turn: "ResponsiveTurn"
    _session_stop_gen_at_entry: "int"
    _spec_hooks: "list[Any]"
    _spec_hooks_cwd: "str | None"
    _spec_hooks_unreadable: "bool"
    _stop_gen_at_entry: "int"
    _stop_reason: "str"
    _terminal_directive_applied: "bool"
    _terminal_directive_kind: "str"
    _terminal_directive_tcids: "set[str]"
    _thinkred: "StreamRedactor"
    _ttft_quiet_text: "bool"
    _ttft_visible: "_FirstVisibleClock"
    _turn_boundary_set: "bool"
    _turn_cost_usd: "float"
    _turn_credits: "float"
    _turn_elapsed_ms: "int"
    _turn_emitted: "bool"
    _turn_is_completion: "bool"
    _turn_model: "str"
    _turn_msg_boundary: "int"
    _turn_project: "str"
    _turn_refusal: "RefusalInfo | None"
    _turn_stats0: "object"
    _turn_stats_captured: "bool"
    _turn_t0: "float"
    _turn_thought: "bool"
    _turn_tool_calls: "int"
    _turn_usage_persisted: "bool"
    _wsred: "StreamRedactor"
    _wt_coded_this_turn: "bool"
    _wt_deciding_coding: "bool"
    _wt_last_cadence: "float"
    assistant_text: "str"
    chunk_seq: "int"
    client: "Any"
    first_word: "str"
    owner_agent: "str"
    owner_app: "str"
    registered_slot: "_ChatSlot | None"
    selected_binding: "tuple[Any, ...]"
    session_key: "str"
    sessions: "Any | None"

    def __init__(
        self,
        *,
        state: "DashboardState",
        slot: "_ChatSlot",
        message: "str",
        _prompt_depth: "int",
        _attachments: "tuple[str, ...] | list[str]",
        _attachment_meta: "dict[str, list[str]] | None",
        _directive_user_origin: "bool",
        _turn_provenance_restored: "bool",
        _directive_self_wake: "bool",
        _directive_loop_id: "str",
        _directive_loop_gen: "int",
        _directive_channel_origin: "bool",
        _commands_off: "bool",
        _on_consumed: "Callable[[bool], None] | None",
        _on_irreversibly_consumed: "Callable[[], Awaitable[None] | None] | None",
        _current_message: "dict | None",
    ) -> None:
        self.state = state
        self.slot = slot
        self.message = message
        self._prompt_depth = _prompt_depth
        self._attachments = _attachments
        self._attachment_meta = _attachment_meta
        self._directive_user_origin = _directive_user_origin
        self._turn_provenance_restored = _turn_provenance_restored
        self._directive_self_wake = _directive_self_wake
        self._directive_loop_id = _directive_loop_id
        self._directive_loop_gen = _directive_loop_gen
        self._directive_channel_origin = _directive_channel_origin
        self._commands_off = _commands_off
        self._on_consumed = _on_consumed
        self._on_irreversibly_consumed = _on_irreversibly_consumed
        self._current_message = _current_message

    if not TYPE_CHECKING:

        def __getattr__(self, name: str) -> object:
            # Only reached for an attribute that was never set. A declared one is a
            # value the turn has not bound yet: raise what reading that local raised.
            if name in _ChatTurn.__annotations__:
                raise UnboundLocalError(
                    f"cannot access local variable {name!r} where it is not associated with a value"
                )
            raise AttributeError(name)
