"""``!stop`` in a Conductor thread (RFC part D): decided before the code-channel gate.

A Conductor thread needs no code-channel scope, so :func:`.routing.inbound_gate`
asks this module first. Only the thread's Stop is decided here; the thread's other
messages take the linked-thread route. Kept out of ``routing`` (its line cap) and
reading ``routing`` lazily, so the names tests patch there stay the ones used.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.slack.code_channels import stop_helpers, surface, thread_runtime
from kiro_crew.slack.code_channels.store import Binding, store
from kiro_crew.slack.code_channels.surface_store import room_key

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


def is_stop_text(text: str) -> bool:
    """Whether *text* is a bare ``!stop``, or one addressed to this bot (its own leading mention).

    One reading for both entry points. A message addressed to someone else
    (``<@U0OTHER> !stop``) is theirs to answer, not a Stop of this thread.
    """
    from kiro_crew.slack.code_channels import routing

    body = routing.strip_leading_mention(text)
    if body.strip().lower() != "!stop":
        return False
    mentioned = routing._MENTION.findall(text[: len(text) - len(body)])
    return not mentioned or mentioned == [routing._self_user_id()]


def is_owner_stop(channel: str, event: dict[str, Any], text: str, sender_id: str) -> bool:
    """Whether *event* is the owner's pure ``!stop`` in a live Conductor thread.

    The inbound route lets it past the activation and mention drops (a channel in
    review or off mode can still hold a thread, and Stop only removes work), so
    :func:`decide` sees it. Anyone else's, and any other message, is not exempt.
    """
    from kiro_crew.slack.code_channels import binding as _binding
    from kiro_crew.slack.handler import is_owner

    thread = binding_of(channel, event)
    return (
        thread is not None
        and is_stop_text(text)
        and is_owner(sender_id)
        # Only what :func:`decide` acts on: else it would pass the message on, and a
        # channel in off mode would be answered.
        and acts_on(thread, _binding.live_config())
    )


def acts_on(binding: Binding, cfg: Any) -> bool:
    """Whether the thread's Stop is live: its switch is on, or its conductor still runs.

    With the switch off a conductor still running is stopped, as a code channel's Stop
    does, so turning the switch off never strands a running turn.
    """
    from kiro_crew.slack.code_channels import routing

    slot = routing.bound_slot(routing._dashboard_state(), binding)
    return thread_runtime.room_runtime_on(binding, cfg) or getattr(slot, "running", False) is True


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

    if from_trusted_bot or not is_stop_text(text):
        return routing.PASS
    if not acts_on(binding, cfg):
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
    # First, before any await: a board write the thread admitted before this press
    # writes nothing more. Under the thread's own key, not the channel's (it holds
    # other threads), so it skips ``routing._stop_bound``'s channel fence and status
    # hold; the stopped turn's dispatch is fenced in stop_helpers.stop_channel.
    surface.mark_stopped(room_key(binding))
    await stop_helpers.stop_channel(
        orch, binding, sender_id, channel, source=OPERATION, say_idle=True
    )
    return routing.DROP
