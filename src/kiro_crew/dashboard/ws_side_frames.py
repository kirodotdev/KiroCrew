"""Side-conversation and reply-thread frames the dashboard socket broadcasts.

Split out of :mod:`kiro_crew.dashboard.ws`, which re-exports every name here, so
callers keep importing them from ``kiro_crew.dashboard.ws``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

SIDE_RESULT_EVENT = "chat.side_result"
#: A reply landing in a thread on a crewmate chat message (``chat_threads``).
THREAD_REPLY_EVENT = "chat.thread_reply"
SIDE_QUEUE_EVENT = "chat.side_queue"
#: The slot's side conversation was closed, so every tab clears it (``api_side_close``).
SIDE_CLOSED_EVENT = "chat.side_closed"
SIDE_KIND = "side"


def broadcast_side_result(
    state: DashboardState,
    *,
    slot_key: str,
    run_id: str,
    role: str,
    content: str,
    is_error: bool = False,
    final: bool = False,
    ts: float | None = None,
    steer: bool = False,
    message_id: str = "",
) -> None:
    """Broadcast a side conversation event on the dedicated side channel.

    Emits ``{type: "chat.side_result", data: payload}`` to all WS clients.
    The event name and payload shape are reused from the upstream OpenClaw
    `/btw` protocol so a future shared client can interop. ``kind`` is
    translated from upstream ``"btw"`` to Kiro Crew's ``"side"``.

    The event channel is intentionally separate from ``chat_message`` so
    receivers that don't subscribe to side simply don't see it; this
    keeps side deltas out of the main transcript by construction.
    Receiver-side run-ID isolation is the frontend's responsibility via
    ``local_side_run_ids``.

    Set final=True on the terminal frame of a side turn so the frontend
    can flip the streaming flag off cleanly.

    ``message_id`` names the sidecar row the frame announces (a question, a
    steer, a final answer, a stop row). A panel restored from ``GET .../side``
    already holds that row, and the id is how it knows. Streaming deltas and
    the error notices the sidecar does not store carry none.

    No payload field is persisted — sidecar-only, ephemeral.
    """
    payload: dict[str, object] = {
        "kind": SIDE_KIND,
        "slot": slot_key,
        "run_id": run_id,
        "role": role,
        "content": redact_credentials(redact_exfiltration_urls(content)[0])[0],
        "ts": ts if ts is not None else time.time(),
    }
    if is_error:
        payload["is_error"] = True
    if final:
        payload["final"] = True
    if steer:
        payload["steer"] = True
    if message_id:
        payload["message_id"] = message_id
    # Owner-only, matching the queue frame and `_check_slot_ownership`: side answers and
    # steer echoes are the owner's own conversation, and an app that asks the HTTP API
    # about a slot it does not own gets a 404.
    state.broadcast_ws_owners(SIDE_RESULT_EVENT, payload)


def broadcast_thread_reply(
    state: DashboardState,
    *,
    slot_key: str,
    mid: str,
    run_id: str,
    role: str,
    content: str,
    is_error: bool = False,
    final: bool = False,
    ts: float | None = None,
    reply: dict[str, object] | None = None,
) -> None:
    """Broadcast one frame of a reply thread (``dashboard/chat_threads.py``).

    ``{type: "chat.thread_reply", data: payload}``: ``mid`` names the parent
    message the thread hangs off, ``run_id`` groups the streamed deltas of one
    crewmate reply, and the terminal frame (``final``) carries the stored
    ``reply`` record so the panel can replace its streamed text with the row the
    store holds. Owner-only, like the side chat: a thread is the owner's own
    conversation. Same channel discipline as ``chat.side_result`` -- a receiver
    that does not subscribe never sees it, so thread frames stay out of the main
    transcript by construction.

    Sent only while ``dashboard.crewmate_threads`` is on. The routes refuse
    before any turn starts, so this guard covers the one turn that was already
    running when the flag went off: its frames are dropped, and the reply it
    stores is served again once the flag is back on. The watcher's snapshot is
    the read (a plain attribute, never a disk load on the loop); before the
    watcher has one, the frame is dropped too -- the flag is off by default and
    a frame nobody can act on is the cheaper mistake.
    """
    from kiro_crew.config import live

    cfg = live.snapshot()
    if cfg is None or not cfg.dashboard.crewmate_threads:
        return
    payload: dict[str, object] = {
        "slot": slot_key,
        "mid": mid,
        "run_id": run_id,
        "role": role,
        "content": redact_credentials(redact_exfiltration_urls(content)[0])[0],
        "ts": ts if ts is not None else time.time(),
    }
    if is_error:
        payload["is_error"] = True
    if final:
        payload["final"] = True
    if reply is not None:
        payload["reply"] = reply
    state.broadcast_ws_owners(THREAD_REPLY_EVENT, payload)


def broadcast_side_queue(
    state: DashboardState,
    *,
    slot_key: str,
    action: str,
    queue_id: str,
    content: str = "",
    depth: int = 0,
    front: bool = False,
    steer_id: str = "",
    origin_client: str = "",
    rev: int | None = None,
) -> None:
    """Broadcast a side-queue mutation on the dedicated side channel.

    ``action`` is one of ``push`` | ``edit`` | ``cancel`` | ``drain``. ``drain``
    fires when the entry leaves the queue to become the next side turn, so the
    frontend can retire its card without waiting for the user frame. ``depth``
    is the queue length AFTER the mutation, letting a client that missed a frame
    resync its badge without a refetch.

    ``rev`` is the entry's revision after an ``edit``, the same count the side
    read reports, so a tab that holds both an edit and a read's copy of the entry
    keeps the newer one.

    ``front`` says the entry went to the HEAD of the queue rather than the tail —
    which is how a requeued steer and a failed drain's entry land. Without it a
    client appends them and shows a different next question than the backend will
    actually run.

    Kept separate from ``chat.side_result`` so a queue mutation never enters the
    transcript reducer, and separate from the main chat's ``queue_push`` so side
    queue entries can never be mistaken for parent-slot turns.
    """
    payload: dict[str, object] = {
        "kind": SIDE_KIND,
        "slot": slot_key,
        "action": action,
        "queue_id": queue_id,
        "depth": depth,
        "ts": time.time(),
    }
    if front:
        payload["front"] = True
    if rev is not None:
        payload["rev"] = rev
    if steer_id:
        # Not sensitive — an opaque ledger id. It lets the submitting client match
        # its own RAW steer text to this card, whose content is redacted here.
        payload["steer_id"] = steer_id
    if content:
        payload["content"] = redact_credentials(redact_exfiltration_urls(content)[0])[0]
    if origin_client:
        # Not sensitive — an opaque per-tab id. It lets a tab recognise its OWN action's echo,
        # so only the tab that cancelled takes the question back into its composer.
        payload["origin_client"] = origin_client
    # Owner-only: `_check_slot_ownership` answers 404 when an app asks about a slot it
    # does not own, and queue entries are the user's own prose. An unscoped broadcast
    # would hand that text to app sockets the HTTP layer keeps out.
    state.broadcast_ws_owners(SIDE_QUEUE_EVENT, payload)


def broadcast_side_closed(
    state: DashboardState,
    *,
    slot_key: str,
    origin_client: str = "",
    dropped: list[dict[str, str]] | None = None,
) -> None:
    """Tell every tab that the slot's side conversation is closed.

    Without it only the tab that closed it clears its panel. Another tab keeps
    rows the model has dropped, and its next question opens a fresh
    conversation under them. ``origin_client`` names the tab that closed it,
    which has already cleared its own panel. ``dropped`` names every queue entry
    the close dropped, including the steer id of a requeued steer, so a tab that
    missed its queue frame can return retained text from a delayed receipt.
    Owner-only, like every side frame.
    """
    payload: dict[str, object] = {"kind": SIDE_KIND, "slot": slot_key}
    if origin_client:
        payload["origin_client"] = origin_client
    if dropped:
        payload["dropped"] = dropped
    state.broadcast_ws_owners(SIDE_CLOSED_EVENT, payload)
