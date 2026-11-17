"""A run's Subagents card row renamed by the ``tool_call_update`` that refines its call.

A harness whose capability record says ``refines_tool_call_titles`` (claude, through
``ACP_BACKENDS_TOOL_TITLE_REFINEMENT``) names a call in its first ``tool_call`` before
the call's input has streamed, with the tool's generic title ("Terminal", "Read File",
"Task"), and sends the call's real title in a ``tool_call_update`` after it. The
dashboard's own tool row takes that title (``_on_tool_call_update`` in
``dashboard/chat_turn/tool_rows.py``); :class:`CardToolTitles` does the same for the
card a Crew-spawned run's ``subagent_tool`` frames draw. ``_run_inner_impl`` in
``run_turn`` notes the title each call and permission request puts on the card
(:meth:`CardToolTitles.note_call`) and hands every ``EVENT_TOOL_CALL_UPDATE`` to
:meth:`CardToolTitles.refine`.

The refinement goes out as one more ``subagent_tool`` frame, marked ``is_update``,
carrying the title the driver derived for the update (``select_tool_title``, as the
dashboard row's is), redacted as the run's other card titles are. The card renames the
row of the call it names wherever that row sits and lists no new call, so the frame
moves no count. One that repeats the title the card already shows for the call is not
sent. ``info.last_tool``, which the reconnect snapshot replays, takes the title only
while that call is still the run's newest, as the card's own tool line does. A harness
without the capability sends no such frame, so its cards show what its calls and
permission requests named.
"""

from __future__ import annotations

import hashlib
from typing import Any

from kiro_crew.agent_sdk.capabilities import capabilities_of

#: Calls one run remembers the card's title of. The ids come from the harness and the
#: model, so their number is bounded: past it the oldest is forgotten, and a
#: refinement of a call that old leaves the card's row as it was.
MAX_LISTED_CALLS = 512

#: Bytes of the digest a call's title is remembered by. A title is only compared, and
#: a shell call's is its whole command, so the text is not kept.
TITLE_DIGEST_BYTES = 8

#: Characters of a ``tool_call_id`` this remembers. A longer id is not listed, so its
#: refinements are not sent; the card keeps the title its call named.
MAX_CALL_ID_CHARS = 1024


class CardToolTitles:
    """The title one run's card shows for each call, and the refinements that rename it."""

    def __init__(self, client: Any) -> None:
        self._on = capabilities_of(client).refines_tool_call_titles
        self._title_digests: dict[str, bytes] = {}

    def note_call(self, event: Any) -> None:
        """Remember the title a call or permission request frame put on the card."""
        call_id = event.tool_call_id or ""
        if not self._on or not call_id or len(call_id) > MAX_CALL_ID_CHARS:
            return
        self._remember(call_id, _digest(event.title or ""))

    async def refine(self, manager: Any, info: Any, event: Any) -> None:
        """Rename the card's row for the call an ``EVENT_TOOL_CALL_UPDATE`` refines.

        A refinement with no title (one that carries only the call's input or
        content), one of a call the card does not list, and one repeating the
        title the card shows change nothing.
        """
        call_id = event.tool_call_id or ""
        title = event.title or ""
        if not self._on or not title:
            return
        digest = _digest(title)
        if self._title_digests.get(call_id, digest) == digest:
            return
        self._remember(call_id, digest)
        # circular import: kiro_crew.subagent imports subagent_manager while it
        # initialises; by the time a run streams it is loaded.
        from kiro_crew import subagent as ns

        if call_id == info.last_tool_call_id:
            info.last_tool = title
        await manager._fire_event(
            "subagent_tool",
            info,
            {
                "tool": ns._redact(title),
                "turns": info.turns,
                "tool_count": info.tool_count,
                "tool_call_id": ns._redact(call_id),
                "is_update": True,
            },
        )

    def _remember(self, call_id: str, digest: bytes) -> None:
        self._title_digests.pop(call_id, None)
        self._title_digests[call_id] = digest
        if len(self._title_digests) > MAX_LISTED_CALLS:
            del self._title_digests[next(iter(self._title_digests))]


def _digest(title: str) -> bytes:
    return hashlib.blake2b(
        title.encode("utf-8", "surrogatepass"), digest_size=TITLE_DIGEST_BYTES
    ).digest()
