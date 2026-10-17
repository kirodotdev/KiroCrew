"""The one redact -> chunk -> send leg for unattended channel egress.

Three legs push text into a non-Slack channel conversation without passing a
renderer and redact it with the context-aware ``redact_via_context`` pair: the
dashboard reply mirror (``chat_runner._deliver_cross_surface_reply``),
the proactive completion leg for subagent and cron output
(``GatewayOrchestrator._deliver_channel_reply``) and the compaction notice
(``chat_compaction_notice._deliver_via_transport``). Each resolves and vets its own
target through the governed ladder (``_resolve_channel_target``), then hands the
transport, the conversation and the text here, so the transform between the vetted
target and the wire has one owner rather than a copy per leg.

What this owns, in order:

1. **Strip trailing control-tag lines.** These legs carry output authored under
   dashboard rules to a client that renders HTML comments literally.
   Strip-then-redact matches ``display_safe``.
2. **Redact in DISPLAY form** (``redact_for_display``). No renderer runs on this
   path, and a renderer is where a turn normally gets that floor; a literal-only
   scan lets a markdown-collapse credential (``AKIA**...**``, which the client
   renders whole) reach the channel. ``redact_via_context`` is the redactor rather
   than the neutral ``display_safe``: it is context-aware, so a loaded companion's
   extra credential patterns apply, and the shared sink's default pair would drop
   them.
3. **Optionally number ``[OPTIONS:]`` choices** (``render_options_as_text``):
   ``send_message`` takes no choices, so a leg that asks for it ships a numbered
   list rather than the raw marker.
4. **Chunk in the transport's own unit** (``chunk_for_transport``: bytes for a
   byte-capped channel such as Webex, chars otherwise), fence-safe, off the loop.
   The seam check grades with the SAME ``redact_via_context`` pair as step 2: a
   companion-contributed credential split across a seam is invisible to the
   narrower default pair and would ship whole across two adjacent messages.
5. **Send each part** to the caller's conversation and thread.

Resolution, governance vetting, DM-target resolution and logging stay with the
caller: they differ per leg, and each leg's SEL trail names its own producer.

The channel-neutral send legs in ``dashboard/messaging_api/channel_delivery.py``
and ``dashboard/handlers/messaging.py`` are not on this path on purpose: they
redact through the neutral ``display_safe_for`` sink, which also defangs
broadcast-mention grammars, so routing them here would change what they send.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kiro_crew.constants import strip_control_comments
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.renderer import chunk_for_transport, render_options_as_text
from kiro_crew.messaging.transport import delivery_confirmed
from kiro_crew.platform.context import redact_via_context


async def channel_egress_parts(
    text: str, capabilities: Any, *, number_options: bool = False
) -> list[str]:
    """Return *text* stripped, display-redacted and chunked for *capabilities*.

    Steps 1-4 of the module contract. ``chunk_for_transport`` runs off the loop
    because a long reply makes the fence-safe split and its seam redaction
    measurable work.
    """
    safe, _ = redact_for_display(strip_control_comments(text), redact_via_context)
    if number_options:
        safe = render_options_as_text(safe, capabilities)
    return await asyncio.to_thread(
        chunk_for_transport, safe, capabilities, redactor=redact_via_context
    )


async def send_channel_parts(
    transport: Any,
    conversation_id: str,
    parts: list[str],
    *,
    thread_id: str | None,
    require_confirmation: bool = False,
) -> bool:
    """Send *parts* in order to one vetted conversation. Step 5 of the contract.

    With *require_confirmation*, the send stops at the first part the transport
    does not confirm (``delivery_confirmed`` owns which id convention it follows)
    and returns False: the remaining chunks of a message whose head never landed
    would arrive as an orphaned fragment. Without it, every part is sent and the
    result is True. A transport exception propagates; the caller owns its logging
    and its degrade path.

    Kept apart from :func:`channel_egress_parts` so a caller can let a redaction
    failure (``redact_via_context`` re-raises a composition error on purpose)
    propagate while still treating a transport failure as best-effort.
    """
    for part in parts:
        sent = await transport.send_message(conversation_id, part, thread_id=thread_id)
        if require_confirmation and not delivery_confirmed(transport.capabilities, sent):
            return False
    return True
