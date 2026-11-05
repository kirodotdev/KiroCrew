"""The coalesced slot list as per-row deltas, for sockets that apply them.

A dashboard tab that declares ``slot_diff.1`` (``SLOT_DIFF_WS_FLAG``) is sent each
coalesced slot list as one of two frames:

- ``{"type": "slots", "data": [...], <envelope>, "slotsSeq": N}``: the full list,
  exactly the frame a legacy socket of the same audience gets plus its sequence
  number. Sent when the socket holds no base (just connected, or asked to
  resync), when the list gained or reordered a row, when an envelope field
  appeared or went away, and when the delta would be over half the full frame.
- ``{"type": "slots_diff", "data": {"base", "rows", "removed", "envelope",
  "seq"}}``: what changed since list ``base``. A ``rows`` entry is
  ``{"key", "row": {...}}``, the whole changed row. ``removed`` names rows that
  left, ``envelope`` the envelope fields whose value moved. A list where nothing
  changed is still sent, as an empty delta, because the client runs its per-list
  reconciles on every list it receives.

The client rebuilds the list from the last one it received: unchanged rows as they
were, a changed row as given. That is the list the full frame would have carried,
value for value, so it goes through the same list handler. A delta whose ``base``
is not the list the client holds asks for a resync (the ``slots_resync`` message,
answered by :func:`answer_resync`), and the client ignores deltas until the
numbered list that answers it arrives.

Rows are compared as their JSON text, so a change is never hidden by Python
equality (``True == 1``) or by a value shared with a later serialization. The
texts are the ones the broadcast already built for the full frames
(:func:`row_texts`), so a row is serialized once per pass whatever the audience.

Each audience has its own base: owner sockets get the owner projection, other
dashboard-user sockets the dashboard-user projection, as in the full frames.
Only dashboard-user sockets take part: the flag's writer in ``ws.py`` requires
``_is_dashboard_user``, and :func:`diff_sockets` checks it again, because the
user view is sent without the app scope filter.

Event-loop only: ``_do_slots_broadcast`` hops to the serving loop before it gets
here, so two passes never mint one sequence number twice.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew.dashboard.websocket_hub import SLOT_DIFF_SEQ_KEY, SLOT_DIFF_WS_FLAG

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.dashboard_state import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.state")

OWNER_VIEW = "owner"
USER_VIEW = "user"

#: A delta larger than this share of the full frame is sent as the full frame.
_MAX_DIFF_SHARE = 0.5

#: Why a diff socket got a full list instead of a delta, counted per socket send
#: since the process started: ``no_base`` (the socket held no list this view's
#: base describes: just connected, resynced, or its base was replaced),
#: ``rows_added_or_moved``, ``envelope_keys`` (an envelope field appeared or went
#: away) and ``over_share`` (the delta was over :data:`_MAX_DIFF_SHARE`).
FULL_FRAME_REASONS: Counter[str] = Counter()

#: Resyncs from one socket within :data:`_RESYNC_WINDOW_S` before one warning.
_RESYNC_WARN_AFTER = 5
_RESYNC_WINDOW_S = 60.0
_RESYNC_TIMES_KEY = "_slot_diff_resync_times"


@dataclass
class DiffBase:
    """The last list one audience was sent, as JSON text per row and envelope field."""

    seq: int
    keys: list[str]
    rows: dict[str, str]
    envelope: dict[str, str]
    #: Set when a ``slot_patch`` went out after this list: the tabs applied it,
    #: so this list differs from what they show and a resync must not replay it.
    patched: bool = False


def diff_sockets(
    state: "DashboardState",
) -> tuple[list[web.WebSocketResponse], list[web.WebSocketResponse]]:
    """Open dashboard-user ``slots_diff`` sockets, split into ``(owner, dashboard user)``."""
    owners = getattr(state, "_owner_ws_clients", None) or set()
    owner_ws: list[web.WebSocketResponse] = []
    user_ws: list[web.WebSocketResponse] = []
    for ws in list(getattr(state, "_ws_clients", None) or ()):
        if ws.closed or ws.get(SLOT_DIFF_WS_FLAG) is not True:
            continue
        if ws.get("_is_dashboard_user") is not True:
            continue
        (owner_ws if ws in owners else user_ws).append(ws)
    return owner_ws, user_ws


def row_texts(rows: list[dict[str, Any]], cache: dict[int, str]) -> list[str]:
    """Each row's JSON text, reusing *cache* for a row dict another view shares.

    The audience views of one pass share every row whose ``source_links`` they
    do not re-project (``serialize_slot_views``), so keying the cache by the
    dict's identity serializes such a row once for all of them. The cache must
    not outlive the pass that built the rows.
    """
    out: list[str] = []
    for row in rows:
        text = cache.get(id(row))
        if text is None:
            text = cache[id(row)] = json.dumps(row)
        out.append(text)
    return out


def list_text(texts: list[str]) -> str:
    """The list ``json.dumps`` writes for rows whose texts are *texts*."""
    return "[" + ", ".join(texts) + "]"


def slots_frame_text(
    texts: list[str], envelope_texts: dict[str, str], seq: int | None = None
) -> str:
    """The ``slots`` frame ``json.dumps`` would write, from cached text.

    With *seq* it carries ``slotsSeq``, the numbered list a diff socket holds as
    its base; without, it is the legacy frame.
    """
    parts = [f'"type": "slots", "data": {list_text(texts)}']
    parts.extend(f"{json.dumps(field)}: {text}" for field, text in envelope_texts.items())
    if seq is not None:
        parts.append(f'"slotsSeq": {seq}')
    return "{" + ", ".join(parts) + "}"


def envelope_texts_of(envelope: dict[str, Any]) -> dict[str, str]:
    """Each envelope field's JSON text, in frame order."""
    return {field: json.dumps(value) for field, value in envelope.items()}


def _delta(
    base: DiffBase, keys: list[str], texts: list[str], envelope: dict[str, str]
) -> tuple[list[tuple[str, str]], list[str], list[tuple[str, str]]] | str:
    """``(changed rows, removed keys, moved envelope fields)`` from *base*.

    Returns the :data:`FULL_FRAME_REASONS` reason instead when only a full frame
    can say it.
    """
    if list(envelope) != list(base.envelope):
        return "envelope_keys"
    if len(set(keys)) != len(keys):
        return "rows_added_or_moved"
    removed: list[str] = []
    remaining = iter(base.keys)
    for key in keys:
        for held in remaining:
            if held == key:
                break
            removed.append(held)
        else:
            # A row the base does not hold here: added, or moved.
            return "rows_added_or_moved"
    removed.extend(remaining)
    rows = [(key, text) for key, text in zip(keys, texts) if base.rows[key] != text]
    moved = [(field, text) for field, text in envelope.items() if base.envelope[field] != text]
    return rows, removed, moved


def _diff_frame_text(
    base_seq: int,
    seq: int,
    rows: list[tuple[str, str]],
    removed: list[str],
    moved: list[tuple[str, str]],
) -> str:
    """The ``slots_diff`` frame ``json.dumps`` would write, spliced from row texts."""
    row_parts = ", ".join(f'{{"key": {json.dumps(key)}, "row": {text}}}' for key, text in rows)
    env_parts = ", ".join(f"{json.dumps(field)}: {text}" for field, text in moved)
    return (
        f'{{"type": "slots_diff", "data": {{"base": {base_seq}, "rows": [{row_parts}], '
        f'"removed": {json.dumps(removed)}, "envelope": {{{env_parts}}}, "seq": {seq}}}}}'
    )


def _allowed(state: "DashboardState", ws: web.WebSocketResponse, view: str, rows: object) -> bool:
    """The hub chokepoint's verdict for a dashboard-user (non-owner) socket.

    Owner sockets are sent the owner view directly, as ``_send_ws_owners`` does.
    Every other socket passes ``_ws_client_allowed``, which records the
    dashboard-user grant in the SEL like any frame the hub sends.
    """
    if view == OWNER_VIEW:
        return True
    check = getattr(state, "_ws_client_allowed", None)
    return True if check is None else bool(check(ws, "slots", {"slots": rows}))


def send_slot_diffs(
    state: "DashboardState",
    view: str,
    sockets: list[web.WebSocketResponse],
    rows: list[dict[str, Any]],
    envelope_texts: dict[str, str],
    texts: list[str],
) -> int:
    """Send *rows* to *sockets* as a delta or a sequenced full frame.

    *texts* are the rows' JSON texts and *envelope_texts* the envelope's, both
    built once for the whole pass. Returns the length of the full frame, which
    sizes the coalescing window the same way the legacy frame does. A view with
    no socket drops its base.
    """
    bases: dict[str, DiffBase] | None = state._slot_diff_bases
    if bases is None:
        bases = state._slot_diff_bases = {}
    if not sockets:
        bases.pop(view, None)
        return 0
    keys = [str(row.get("key", "")) for row in rows]
    seq = state._slot_diff_seq + 1
    state._slot_diff_seq = seq
    full_msg = slots_frame_text(texts, envelope_texts, seq)
    full_len = len(full_msg)

    base = bases.get(view)
    diff_msg: str | None = None
    reason = "no_base"
    if base is not None:
        delta = _delta(base, keys, texts, envelope_texts)
        if isinstance(delta, str):
            reason = delta
        else:
            diff_msg = _diff_frame_text(base.seq, seq, *delta)
            if len(diff_msg) > full_len * _MAX_DIFF_SHARE:
                diff_msg = None
                reason = "over_share"

    dead: list[web.WebSocketResponse] = []
    for ws in sockets:
        if not _allowed(state, ws, view, rows):
            continue
        held = ws.get(SLOT_DIFF_SEQ_KEY)
        if diff_msg is not None and base is not None and held == (view, base.seq):
            msg = diff_msg
        else:
            msg = full_msg
            FULL_FRAME_REASONS[reason if diff_msg is None else "no_base"] += 1
        ws[SLOT_DIFF_SEQ_KEY] = (view, seq)
        try:
            state._spawn_ws_send(ws, msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state._remove_ws(ws)
    bases[view] = DiffBase(seq=seq, keys=keys, rows=dict(zip(keys, texts)), envelope=envelope_texts)
    return full_len


def answer_resync(state: "DashboardState", ws: web.WebSocketResponse) -> None:
    """Answer a ``slots_resync`` from *ws* with the list its view last received.

    The held base is that list, so the answer is one sequenced full frame to the
    one socket that asked, with no re-serialization and nothing sent to anyone
    else. When the view holds no base yet, or a ``slot_patch`` went out after it
    (the tab applied that patch, so replaying the base would undo it), the answer
    is a coalesced push instead, which rebuilds the list and owes it to every
    consumer.
    """
    ws.pop(SLOT_DIFF_SEQ_KEY, None)
    _note_resync(ws)
    owners = getattr(state, "_owner_ws_clients", None) or set()
    view = OWNER_VIEW if ws in owners else USER_VIEW
    base = (state._slot_diff_bases or {}).get(view)
    if base is None or base.patched:
        state.push_slots_update()
        return
    texts = [base.rows[key] for key in base.keys]
    if not _allowed(state, ws, view, texts):
        return
    ws[SLOT_DIFF_SEQ_KEY] = (view, base.seq)
    FULL_FRAME_REASONS["no_base"] += 1
    try:
        state._spawn_ws_send(ws, slots_frame_text(texts, base.envelope, base.seq))
    except Exception:
        state._remove_ws(ws)


def _note_resync(ws: web.WebSocketResponse) -> None:
    """Warn once per window when one socket keeps asking for resyncs."""
    now = time.monotonic()
    times = [t for t in ws.get(_RESYNC_TIMES_KEY, ()) if now - t < _RESYNC_WINDOW_S]
    times.append(now)
    ws[_RESYNC_TIMES_KEY] = times
    if len(times) == _RESYNC_WARN_AFTER:
        logger.warning(
            "slots_diff: one socket asked for %d resyncs in %.0f s; its deltas keep missing",
            len(times),
            _RESYNC_WINDOW_S,
        )
