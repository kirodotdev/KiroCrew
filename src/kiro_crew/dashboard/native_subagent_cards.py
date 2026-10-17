"""The native sub-agent card's bounds, call admission and output dedup.

kiro-cli runs a native ``subagent`` crew inside the parent process and emits
every child frame -- tool calls, their results, reasoning and text -- tagged
with the CHILD's session id. The ACP handle re-tags each as subagent activity
(``AcpEvent.activity``) and ``chat_turn.native_cards._native_subagent_activity``
projects it onto the child's Subagents-panel card. This module holds the part
of that projection that touches no backend type and no redactor: the bounds a
card keeps a call's title, input and output within, the admission of a call on
its first frame, and the rule that prints a call's output once.

The projection itself, its per-child stream redactors and the whole-text
redaction live in ``dashboard/chat_turn/native_cards.py`` -- an owner composed
into the runner, with its own row in the redaction census -- because they read
the backend's event tags and call redactors, which this module may not. This
module holds no state and never imports the runner or the owner: both import
it, and the tests patch its bounds here, on the module that reads them.
"""

from __future__ import annotations

import re

#: Longest tool result a native card's live frame carries. The feed keeps the
#: same cut (the done-time ``result`` is cut again by ``_native_done_result``).
NATIVE_SUBAGENT_RESULT_CHARS = 4000
#: Longest tool input a native card's live tool frame carries.
NATIVE_SUBAGENT_INPUT_CHARS = 2000
#: Longest child tool-call id any call store keeps. kiro-cli's ids are short
#: tokens; a longer one is backend-controlled wire data and is refused (no row
#: anywhere) rather than cut, which could alias two ids.
NATIVE_SUBAGENT_CALL_ID_CHARS = 128
#: Most native child tool calls one turn holds rows for, across EVERY store
#: keyed on a child's call id: the turn's call-to-card attribution map, each
#: card's call titles and the record of outputs already printed. One constant
#: and one admission (``_admit_native_call``) for the whole population, so no
#: store can outgrow another; a call past it still streams to its card and its
#: output still reaches the feed, but it holds no row -- its name is not kept,
#: its repeated output is not told from a new one, and a flat copy of it on the
#: parent stream cannot be attributed (the runner then fails closed).
NATIVE_SUBAGENT_CALLS_MAX = 1024
#: The feed line for an output whose call name the bounds above did not keep,
#: so a reader of the finished card's result sees that a name is missing rather
#: than an output filed under nothing.
NATIVE_SUBAGENT_UNNAMED_FEED_LINE = "\u2192 Unnamed tool call\n"
#: kiro-cli prefixes a native child's live tool title with its own status word.
#: The card's rows strip it (their state says it), and the ``→ title`` line the
#: feed files a call's output under strips it too: the feed is read on the
#: finished card, where "Running:" is false. The live frame's title is sent as
#: received, so the client's one rule covers both wires.
_NATIVE_RUNNING_PREFIX = re.compile(r"^Running: ")


def _admit_native_call(info: dict, tc_card: dict, card_id: str, tcid: str, title: str) -> bool:
    """Admit a child's call into the turn's call stores, once, on its first frame.

    ``tc_card`` (call id -> card) is the one admission set for the population:
    a call holds a row in any store -- the attribution map, its card's titles,
    the printed-output record -- only after it is admitted here, and admission
    is refused for an over-long id or once the turn holds
    ``NATIVE_SUBAGENT_CALLS_MAX`` calls. A refused call gets no row anywhere;
    each of its frames is counted in ``info["calls_refused"]`` (a refused id
    cannot be told from a new one, so the count is of frames, as the roster's
    overflow count is) and the frame still projects onto the card. The count
    is kept on EVERY record, a closed child's included: the turn's directive
    gate reads it off every record until the turn ends (records leave the
    tracker only at turn end) to refuse a flat call the bound left
    unattributed, and a closed child's refused call is a child's all the same.

    Runs on the call's FIRST frame of any shape. kiro-cli's parser emits a
    terminal ``tool_call_update`` as a result event and then a refinement, so
    a call whose opening frame was dropped -- it arrived before the roster
    admitted the child, which is ordinary: a child starts working at once --
    is first seen through its result. Admitting on the result keeps the
    counter and the bounds ahead of every read: the refinement that follows
    refines the slot the admission opened (an empty title is filled by a later
    frame), the frame that reads ``tool_count`` finds it set, and the call is
    counted once however many frames it sends. Returns whether the call holds
    a row. A child whose record is already closed (``info["done"]``) still
    has its call attributed -- a child's calls do not stop being the child's
    when its roster entry closes first -- and its refusals counted, but the
    finished record takes no title and no count of calls: it is kept for
    reconnect replay and carries only bounded fields.
    """
    if len(tcid) > NATIVE_SUBAGENT_CALL_ID_CHARS or len(tc_card) >= NATIVE_SUBAGENT_CALLS_MAX:
        info["calls_refused"] = int(info.get("calls_refused") or 0) + 1
        return False
    tc_card[tcid] = card_id
    if not info.get("done"):
        info["tool_count"] = int(info.get("tool_count") or 0) + 1
        info.setdefault("call_titles", {})[tcid] = title
    return True


def _native_output_is_new(result_seen: dict, tc_card: dict, tcid: str, out: str) -> bool:
    """Whether a call's output differs from the last one printed for it.

    kiro-cli sends a call's output twice -- a content frame, then the terminal
    frame's ``rawOutput`` -- so the same text is printed once; a later DIFFERENT
    output of the same call (a progress line, then the summary) is printed too,
    not dropped as a repeat of the id. ``result_seen`` (call id -> digest of the
    last printed output) is keyed only for admitted calls, so it cannot outgrow
    the admission set; a refused call holds no row, so each of its outputs
    prints.
    """
    if tcid not in tc_card:
        return True
    digest = hash(out.strip())
    if result_seen.get(tcid) == digest:
        return False
    result_seen[tcid] = digest
    return True
