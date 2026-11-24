"""``!stop`` in a Conductor thread (RFC part D): decided before the code-channel gate.

A Conductor thread needs no code-channel scope, so :func:`.routing.inbound_gate`
asks this module first. Only the thread's Stop is decided here; the thread's other
messages take the linked-thread route. Kept out of ``routing`` (its line cap) and
reading ``routing`` lazily, so the names tests patch there stay the ones used.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.slack.code_channels import stop_helpers, thread_runtime
from kiro_crew.slack.code_channels.store import Binding, store

OPERATION = "slack.conductor_thread.stop"


def binding_of(channel: str, event: dict[str, Any]) -> Binding | None:
    """The live Conductor thread *event* is in, or None.

    From the store's loaded snapshot only (never a cold load on the event loop);
    an ended thread is not live here.
    """
    from kiro_crew.slack.code_channels import routing

    loaded = store().loaded_thread_bindings() if channel else None
    for binding in loaded or ():
        if (
            binding.channel_id == channel
            and not binding.ended_at
            and routing._in_thread(event, binding.anchor_ts)
        ):
            return binding
    return None


def is_owner_stop(channel: str, event: dict[str, Any], text: str, sender_id: str) -> bool:
    """Whether *event* is the owner's pure ``!stop`` in a live Conductor thread.

    The inbound route lets it past the activation and mention drops (a channel in
    review or off mode can still hold a thread, and Stop only removes work), so
    :func:`decide` sees it. Anyone else's, and any other message, is not exempt.
    """
    from kiro_crew.slack.code_channels import routing
    from kiro_crew.slack.handler import is_owner

    return (
        routing.strip_leading_mention(text).strip().lower() == "!stop"
        and is_owner(sender_id)
        and binding_of(channel, event) is not None
    )


async def decide(
    orch: Any,
    cfg: Any,
    binding: Binding,
    *,
    text: str,
    sender_id: str,
    from_trusted_bot: bool,
) -> Any:
    """``!stop`` in a Conductor thread stops its conductor and its workers; PASS for anything else.

    Owner only, by identity: another member, a shared user or the allow-list
    gets nothing stopped (and the word reaches no other Stop). With the switch
    off it stops only a conductor still running, as a code channel's Stop does,
    so turning the switch off never strands a running turn.
    """
    from kiro_crew.slack.code_channels import routing

    if from_trusted_bot or routing.strip_leading_mention(text).strip().lower() != "!stop":
        return routing.PASS
    slot = routing.bound_slot(routing._dashboard_state(), binding)
    if not (
        thread_runtime.room_runtime_on(binding, cfg) or getattr(slot, "running", False) is True
    ):
        return routing.PASS
    channel = binding.channel_id
    resources = f"channel={channel} thread={binding.anchor_ts}"
    from kiro_crew.slack.handler import is_owner

    if not is_owner(sender_id):
        routing._audit(sender_id, OPERATION, "denied", resources, "unauthorized sender")
        return routing.DROP
    # No channel-activation check (review or off): Stop only removes work, so the owner's
    # Stop is admitted whatever the channel's activation, even where a thread started.
    routing._audit(sender_id, OPERATION, "allowed", resources)
    # A thread has no channel-wide stop fence or status hold (the channel holds other
    # threads), so it skips ``routing._stop_bound``'s: its Stop fences the stopped
    # turn's dispatch itself (stop_helpers.stop_channel).
    # shortcut: Slack writes a thread admitted before the Stop are not fenced; key the
    # fence by thread when part C's board writes need it.
    await stop_helpers.stop_channel(
        orch, binding, sender_id, channel, source=OPERATION, say_idle=True
    )
    return routing.DROP
