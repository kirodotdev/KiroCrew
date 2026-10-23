"""The two session folds no side panel draws: ``class``, what kind of session a log
belongs to over its whole life, and ``outline``, one row per turn.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from kiro_crew.crew_log.errors import CODE_BAD_DATA, CrewLogError
from kiro_crew.crew_log.projection_values import _as_str
from kiro_crew.crew_log.schema import Entry

#: Newest turn rows an ``outline`` keeps, the same window posture and the same value
#: as :data:`TIMELINE_LIMIT`. The count that fell off the front rides beside the rows,
#: so a reader is never shown a window that looks like a whole conversation.
OUTLINE_TURN_LIMIT: Final[int] = 200

#: CHARACTERS of an outline row's first-user-line preview. Characters and not bytes,
#: the unit every fold's clamp counts in (:data:`TEXT_LIMIT`, :func:`_as_str`),
#: so one figure describes the clamp and the byte cost is read off it: a 4-byte UTF-8
#: character is four times an ASCII one, which is the direction
#: :data:`_SLOT_FOLD_ROW_BYTES`' measurements are taken in.
#:
#: Smaller than :data:`OUTLINE_REPLY_CHARS` because the two previews are read for
#: different lengths: a prompt is scanned to RECOGNISE a turn and a reply is scanned to
#: recall what came of it. The pair is also the row's whole free-text budget -- a row
#: with two unclamped text fields is the shape that breaks a byte budget -- so it is
#: stated here as one decision rather than inherited twice from ``TEXT_LIMIT``.
OUTLINE_PROMPT_CHARS: Final[int] = 100

#: CHARACTERS of an outline row's settled-reply preview. Same unit and same reason as
#: :data:`OUTLINE_PROMPT_CHARS`, at :data:`TEXT_LIMIT`'s own figure: the reply is the
#: field a reader actually reads for content.
OUTLINE_REPLY_CHARS: Final[int] = 200

#: Types an ``outline`` records, and the whole of what one row can be built from.
#:
#: WHAT IS ABSENT IS THE CONTRACT. ``context/composed`` is where every injected block
#: is recorded -- system-injected rules, memory, environment context -- and it is not
#: here, so no row can carry one. ``tool/called`` and ``tool/completed`` are not here
#: either, so no row can carry tool output. That is structural rather than filtered:
#: a type absent from this set never reaches :func:`_outline_step` at all, because
#: ``affects`` is built from it and the kernel skips both the copy and the step.
#:
#: It holds for the prompt column for a second, stronger reason. ``message/received``
#: carries the body the gateway ACCEPTED -- both writers pass the typed message
#: (``on_message_received(role="user", text=user_typed_message)``) -- never the
#: composed prompt, so the injected blocks are not in the field this fold reads even
#: before the type set is applied.
OUTLINE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "turn/started",
        "turn/refused",
        "turn/completed",
        "message/received",
        "message/sent",
    }
)


# --------------------------------------------------------------------------- #
# class -- what kind of session this log belongs to, over its whole life
# --------------------------------------------------------------------------- #


def _class_start() -> dict[str, Any]:
    return {
        # Whether the FIRST opener this fold saw stated a class. ``saw_opener`` is
        # what makes it the first one rather than any one: a log carries an opening
        # entry per re-attachment, so a log whose original opener predates the field
        # gains a later one that does state a class, and letting that set ``opened``
        # would date the log by an entry written long after the part whose class is
        # unknown.
        "opened": False,
        "saw_opener": False,
        "stated": 0,
        "memory": "",
        "app": "",
        "channel": False,
        # The workspace the FIRST stated class named, and whether a later one named a
        # different one. Not folded most-restrictively like the members above, because a
        # workspace is an identity rather than a restriction -- there is no "more
        # restrictive" workspace to keep. What a reader needs is whether ONE workspace
        # owns this log's whole content, so the first is kept and any move is recorded as
        # a fact of its own. A log that moved belongs to no single workspace, and a
        # cross-session read of it is refused whichever workspace asks.
        "workspace": "",
        "workspace_moved": False,
        # The last seq this fold RECEIVED, and whether the history it saw has a hole
        # in it. Separate from ``complete``, which is about the log's beginning: a log
        # can begin properly and still be missing a record in the middle.
        "last_seq": 0,
        "damaged": False,
    }


def _class_read(data: Mapping[str, Any]) -> dict[str, Any] | None:
    """The class members of *data*, or ``None`` when it states no class.

    ``memory`` is required, so its absence is what says a class was not stated. A
    line carrying the other members without it is a fragment, and a fragment reads
    as nothing stated rather than as a class with an unknown memory mode -- the
    same rule the reader applies to a missing object.

    ``workspace`` is NOT required, and that is deliberate: whether a class was stated
    and which workspace stated it are two questions, and a line that names a memory
    mode did state a class. An absent workspace reads as the empty string, which no
    live slot can produce (a slot's workspace defaults to ``default``), so the arm that
    compares workspaces refuses on it rather than treating it as a match.
    """
    memory = data.get("memory")
    if not isinstance(memory, str) or not memory:
        return None
    app = data.get("app")
    workspace = data.get("workspace")
    return {
        "memory": _as_str(memory),
        "app": _as_str(app) if isinstance(app, str) else "",
        "channel": data.get("channel") is True,
        "workspace": _as_str(workspace) if isinstance(workspace, str) else "",
    }


def _class_absorb(state: dict[str, Any], stated: dict[str, Any]) -> None:
    """Fold *stated* into *state*, keeping the most restrictive value ever held.

    Restrictive, not latest, and that is the whole semantics of this fold. The
    question a reader asks is whether this log could hold content that must not
    cross a boundary, and content is durable: a session published to a channel for
    one turn holds that turn's words for good, so a later turn reporting no channel
    does not make the log readable again. The same reasoning covers an app that
    owned the session and a memory mode that was ever not persistent.

    So each member only ever moves AWAY from the permissive value: ``channel``
    latches true, ``app`` keeps the first owner it ever had, and ``memory`` keeps
    the first non-persistent mode. A member that has never been restrictive tracks
    what was last stated, which is what makes an ordinary session's fold read as
    the ordinary class rather than as an empty one.
    """
    state["stated"] += 1
    if state["memory"] == "" or state["memory"] == "persistent":
        state["memory"] = stated["memory"]
    if not state["app"] and stated["app"]:
        state["app"] = stated["app"]
    state["channel"] = state["channel"] or stated["channel"]
    # Workspace is the exception to the paragraph above: it is an identity, so there is
    # no more-restrictive value to keep. The first one stated is kept, and a later one
    # that differs sets ``workspace_moved`` -- which is itself the restrictive fact,
    # since a log whose content spans two workspaces is owned by neither.
    if not state["workspace"]:
        state["workspace"] = stated["workspace"]
    elif stated["workspace"] and stated["workspace"] != state["workspace"]:
        state["workspace_moved"] = True


def _class_step(state: dict[str, Any], entry: Entry) -> None:
    # Seq CONTIGUITY, and for this fold only. The store skips an unparseable interior
    # line deliberately -- its own words: one unreadable record must not make the rest
    # of the file unreadable -- and that is right for a fold accumulating totals, where
    # a lost entry costs a count. It is wrong for this one: the skipped line may be the
    # SOLE record of a restriction, and dropping it turns a restricted log into a
    # permissive answer, which is an authorization ceiling raised by byte damage. So a
    # gap in the seqs this fold receives marks the history damaged and the reader
    # refuses on it, while every other fold keeps the store's tolerance.
    #
    # The FIRST entry seen is accepted at whatever seq it carries: a log whose front
    # retention took does not begin at 1, and that is the reader's own check to make,
    # from the segment names, rather than a hole reported from the middle.
    previous = state["last_seq"]
    state["last_seq"] = entry.seq
    if previous and entry.seq != previous + 1:
        state["damaged"] = True
    if entry.type == "write/dropped":
        # The log itself saying an append was permanently lost. For a fold whose
        # answer is an authorization ceiling that is a hole: the lost append may have
        # been the class move that restricted this session, and nothing else records
        # it. This is what lets the RECORDER be best-effort at the call site -- a
        # class move that never reaches the file cannot leave the log readable.
        state["damaged"] = True
        return
    if entry.type == "session/opened":
        first = not state["saw_opener"]
        state["saw_opener"] = True
        recorded = entry.data.get("class")
        if not isinstance(recorded, Mapping):
            # A log opened before the field existed. Nothing is absorbed and
            # ``opened`` stays false, so the render reports a history with no
            # beginning rather than an unrestricted session.
            return
        stated = _class_read(recorded)
        if stated is None:
            # The object is THERE and cannot be read, which is damage rather than
            # age: a writer that records the field records it whole, and the
            # declaration refuses a fragment at append. Absence is a date; an
            # unreadable presence is a hole.
            state["damaged"] = True
            return
        if first:
            # Only the log's own beginning can date it. A later opener is written
            # when a new gateway process re-attaches to the same session, so on a log
            # whose first opener predates the field it would otherwise supply a
            # beginning for a stretch of the log it was not present for.
            state["opened"] = True
        _class_absorb(state, stated)
    elif entry.type == "session/class":
        stated = _class_read(entry.data)
        if stated is None:
            # A move was recorded and cannot be read. What it moved TO is the whole
            # content of this entry, so skipping it discards a transition this fold
            # exists to carry -- and the direction it discards is always toward
            # permissive, since only a restriction is worth recording a move for.
            state["damaged"] = True
            return
        _class_absorb(state, stated)


def _class_render(state: dict[str, Any]) -> dict[str, Any]:
    return {
        # Whether any class was stated at all. False for a log written before the
        # class was recorded, and for one whose class-bearing entries retention has
        # taken.
        "recorded": state["stated"] > 0,
        # Whether the history has a BEGINNING -- the log's FIRST opening entry
        # stated a class. A fold that saw only transitions knows the class moved and
        # not what it moved from, so it cannot report the earliest class the log
        # held, and a reader deciding an authorization question must treat that as
        # unknown. A LATER opener does not supply that beginning: one is written per
        # re-attachment, so on a log whose first opener predates the field it would
        # date a stretch of the log it was not present for.
        # This is also what dates the log: the opening ``class`` object and
        # ``session/class`` were declared together, so a log stating the first was
        # written by a build that records the second, and its absence of transitions
        # is therefore a real account of a class that never moved rather than the
        # silence of a writer that could not say.
        "complete": state["opened"],
        # Whether the history this fold saw has a HOLE: a seq the store skipped
        # because the line was unreadable, or a class record present and unreadable.
        # Distinct from ``complete`` at the other end of the same question -- that one
        # is about the log's beginning, this one about its middle -- and a reader
        # deciding an authorization question refuses on either, because the record a
        # hole swallows is more likely to be a restriction than a relaxation: only a
        # restriction is worth writing a move for.
        "damaged": state["damaged"],
        "memory": state["memory"],
        "app": state["app"],
        "channel": state["channel"],
        # Which workspace owns this log's content, and whether more than one ever did.
        # A cross-session read compares the first against the caller's own workspace and
        # refuses on the second, so an empty ``workspace`` (no live slot can state one)
        # and a moved one both refuse rather than matching.
        "workspace": state["workspace"],
        "workspace_moved": state["workspace_moved"],
    }


# --------------------------------------------------------------------------- #
# outline -- one row per turn: the first user line and the settled reply
# --------------------------------------------------------------------------- #
#
# WHAT THIS FOLD IS FOR. A reader skimming a long session wants the conversation a
# human would recognise: what was asked, and what came of it, once per turn. The
# ``timeline`` fold answers a different question with the same entries -- it lists
# MOMENTS and deliberately carries no bodies -- and the page route serves the bodies
# whole. Neither gives a turn-per-row skim, which is what a dashboard block binds to.
#
# WHAT A ROW IS ANCHORED ON, and why it is NOT the turn boundary. ``turn/started`` is
# the obvious anchor and it is the wrong one here, because of the order this product's
# writer actually uses: ``on_message_received`` runs BEFORE the dispatch gates and
# ``on_turn_started`` runs after them (both in ``_turn_dispatch_gates``), with
# five refusal gates in between. So a REFUSED turn carries its prompt and its
# ``turn/refused`` and never gets a boundary entry at all. A fold anchored on the
# boundary would drop every refused turn's row outright -- and worse, that turn's
# ``message/received`` would then land on the PREVIOUS row and report one turn's
# question against another turn's answer. Rows are therefore anchored on the turn
# ORDINAL: the first entry naming an ordinal above the newest row opens the row, and
# that entry's seq is the load-through target. In this log that entry IS the prompt,
# which makes it a better target than the boundary: paging back through it loads the
# turn including the question.
#
# WHAT "SETTLED" MEANS, which is the one rule here that had to be decided rather than
# read off the writer. A turn that was steered, retried or ran several model calls has
# more than one candidate reply, and the fold must name one:
#
#   1. A ``message/sent`` carrying ``interrupted`` is a reply a steer CUT, and it is
#      skipped outright -- it does not become the draft and it does not clear a draft
#      already held. A cut reply is the one candidate that is provably not what the
#      turn settled on, and this product records that fact on the entry
#      (``interrupted``: "True when a steer cut this reply"), so the rule reads it
#      rather than guessing from ordering.
#   2. Otherwise the NEWEST uncut reply wins: each one overwrites a draft, so a
#      multi-step turn settles on its last model call rather than its first.
#   3. The draft commits at the turn's own closer and not before, so an OPEN turn
#      shows no reply at all. A reply shown while its turn is still running is a
#      reading of a turn that has not happened yet.
#   4. A RETRY -- a second ``turn/started`` at the same ordinal, which this product
#      marks with ``attempt`` -- CLEARS the row's committed reply and the draft. The
#      previous attempt's reply is stale by the rerun's own declaration. The row keeps
#      its prompt and its anchor: the rerun re-ran the same question.
#
# Rule 4 is where this diverges from the reference implementation a second time. There
# a non-advancing boundary is skipped and the standing entry keeps the old reply until
# something overwrites it; here the rerun is a recorded fact with its own field, so the
# fold can say "that answer was retracted" instead of serving it until replaced.


def _outline_preview(value: Any, limit: int) -> str:
    """*value*'s text collapsed to one line and clipped to *limit* CHARACTERS.

    Characters, which is the unit every clamp in this module counts in, and an
    ellipsis when anything was cut -- so a clipped line is never read as a whole one.

    Sliced BEFORE the whitespace collapse rather than after. The collapse walks the
    whole string, and this runs on every message entry in the log, so normalising a
    body at the entry ceiling to produce a hundred characters would pay the length of
    the log's bulk for a preview. The slice is deliberately wider than *limit*
    (whitespace collapse SHRINKS a string, so a slice at exactly *limit* can come back
    short); what survives past the slice is marked cut even when the collapsed head
    came back under budget, because the remainder was real and went unread.
    """
    if not isinstance(value, str) or not value:
        return ""
    window = limit * 4
    head = value[:window]
    unread = len(value) > window
    collapsed = " ".join(head.split())
    if not collapsed:
        return ""
    if len(collapsed) > limit:
        return collapsed[: limit - 1].rstrip() + "\u2026"
    return collapsed + "\u2026" if unread else collapsed


def _outline_start() -> dict[str, Any]:
    return {
        # Rows in ascending turn order, newest last, windowed to OUTLINE_TURN_LIMIT.
        "turns": [],
        # Rows cut off the front, so the window says it is one.
        "dropped": 0,
        # The open turn's newest UNCUT reply, held until its closer commits it. Not
        # rendered: a draft is a reply whose turn has not settled, and rule 3 above is
        # that no reader sees one.
        "draft": "",
        # Which ordinal the draft belongs to; 0 outside a turn. The draft is committed
        # only by a closer naming this ordinal, so a closer arriving out of order
        # cannot commit one turn's reply onto another turn's row.
        "draft_turn": 0,
        # The draft's model call (step) and the raw text it was built from. One model
        # call can write its reply in two `message/sent` entries -- the runner cuts the
        # last run of a row off at a tool call or approval prompt and gives it back
        # under the row's own step -- and the two are joined (rule 2a). The preview is
        # rebuilt from the joined raw rather than from two previews, since collapsing
        # each part alone would drop the space between them.
        "draft_step": 0,
        "draft_raw": "",
        # Whether the current call's body is unreadable here: a part of it was stored
        # as ``message/chunk`` and its ``message/sent`` carried no ``text``. Such a
        # call's preview stays empty for the whole call, so a later same-step part (a
        # run given back under the row's step) cannot resurrect it as a last-part-only
        # preview with no sign the body before it was unreadable.
        "draft_unreadable": False,
        # The newest seq folded, for the same guard the ``timeline`` fold states: a
        # seq restarts at 1 in the next succession unit, so a seq at or below this one
        # is the mark that a slot-wide read is folding two units into one list.
        "last_seq": 0,
    }


def _outline_row(state: dict[str, Any], turn: int, entry: Entry) -> "dict[str, Any] | None":
    """The row *turn* belongs to, opening one when *entry* is the first to name it.

    ``None`` when *turn* is BELOW the newest row's ordinal: that is a regressive
    boundary, and placing it would unsort the outline. The newest row's OWN ordinal is
    not regressive -- that is the retry and the rest of a turn's entries -- so it is
    returned rather than refused.
    """
    turns: list[dict[str, Any]] = state["turns"]
    if turns:
        last = turns[-1]
        if turn == last["turn"]:
            return last
        if turn < last["turn"]:
            return None
    row: dict[str, Any] = {
        "turn": turn,
        # The anchor: the seq of the first entry that named this turn. See the section
        # note -- in this log that is the prompt, not the boundary.
        "seq": entry.seq,
        "time": entry.time,
        "actor": "",
        "prompt": "",
        "reply": "",
    }
    turns.append(row)
    if len(turns) > OUTLINE_TURN_LIMIT:
        state["dropped"] += len(turns) - OUTLINE_TURN_LIMIT
        del turns[: len(turns) - OUTLINE_TURN_LIMIT]
    return row


def _outline_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type not in OUTLINE_TYPES:
        return
    if entry.seq <= state["last_seq"]:
        # The guard ``_timeline_step`` states, for the same reason and in the same
        # words: this fold spans ONE succession unit. It is session-keyed, so the only
        # reader today never gets here; the day a slot-keyed reader folds it, two units
        # interleaved by wall clock arrive as an inversion and must fail loudly rather
        # than render as one scrambled conversation.
        raise CrewLogError(
            f"outline row at seq {entry.seq} is at or below the fold's seq "
            f"{state['last_seq']}; the outline fold spans one succession unit, not a "
            f"slot of them -- order the units by succession before folding",
            code=CODE_BAD_DATA,
            field="seq",
        )
    state["last_seq"] = entry.seq
    data = entry.data
    turn = data.get("turn")
    if not isinstance(turn, int) or isinstance(turn, bool) or turn < 0:
        # A row is placed BY its ordinal, so an entry that names none names no row.
        # Checked here rather than trusted from the type declaration: a declaration
        # binds the writer, and a damaged or planted line is the input that ignores it.
        return
    row = _outline_row(state, turn, entry)
    if row is None:
        return

    if entry.type == "message/received":
        # THE FIRST-USER-LINE RULE, both halves of it. ``role`` must be the human one,
        # so a body written under another role cannot reach the column a reader reads
        # as the question -- today both writers pass ``role="user"`` with the TYPED
        # message, and this gate is what keeps that true of a writer added later. And
        # the row's prompt is filled only while it is still empty, so a STEER (a second
        # human message inside the same turn) keeps the line that opened the turn.
        if _as_str(data.get("role")) != "user" or row["prompt"]:
            return
        row["prompt"] = _outline_preview(data.get("text"), OUTLINE_PROMPT_CHARS)
        return

    if entry.type == "message/sent":
        # Rule 1: a cut reply is skipped and does NOT clear the standing draft.
        if data.get("interrupted") is True:
            return
        # Rule 2: the newest uncut reply wins, INCLUDING one with no readable body.
        #
        # An empty preview is not the same fact as a cut reply, and only rule 1 may
        # keep a draft standing. A body too long for one line is stored as
        # ``message/chunk`` entries and its citing ``message/sent`` carries ``chunks``
        # and ``chars`` and no ``text`` at all -- so a turn that narrates before a tool
        # call ("let me check that") and then answers at length has a LAST reply this
        # fold cannot read. Holding the narration there would publish an intermediate
        # line as the thing the turn settled on, which no reader can tell from a real
        # answer. Empty is the honest preview, and it is what this fold's scope says:
        # it does not read ``message/chunk``, so an overflow body has none here.
        #
        # Rule 2a: two uncut ``message/sent`` of ONE model call -- same turn and same
        # step -- are that call's reply in parts, joined in order. The runner cuts the
        # last run of a row off at a tool call or approval prompt and logs it under the
        # row's own step once the text after it settles the run, so the reply arrives as
        # the row's entry and then that run. Folded into one preview, the row shows the
        # whole reply and not only the part given back last. A LATER step is the next
        # model call and still replaces, so rule 2 (newest call wins) is unchanged; a
        # step of 0 or absent never joins, as no two real calls share one.
        #
        # A call whose body is unreadable here stays empty across the join: if any part
        # of the call was stored as ``message/chunk`` (no ``text``), rule 2's empty
        # preview must hold even when a readable same-step part (the given-back run)
        # arrives after it -- otherwise the outline would show that run as the settled
        # reply with no sign the body before it could not be read.
        text = data.get("text")
        step = data.get("step")
        readable = isinstance(text, str) and bool(text)
        same_call = (
            state["draft_turn"] == turn
            and isinstance(step, int)
            and not isinstance(step, bool)
            and step > 0
            and state["draft_step"] == step
        )
        if same_call:
            state["draft_unreadable"] = state["draft_unreadable"] or not readable
            if readable and not state["draft_unreadable"]:
                state["draft_raw"] = state["draft_raw"] + text
        else:
            state["draft_unreadable"] = not readable
            state["draft_raw"] = text if readable else ""
        state["draft"] = (
            ""
            if state["draft_unreadable"]
            else _outline_preview(state["draft_raw"], OUTLINE_REPLY_CHARS)
        )
        state["draft_turn"] = turn
        state["draft_step"] = step if isinstance(step, int) and not isinstance(step, bool) else 0
        return

    if entry.type == "turn/started":
        row["actor"] = _as_str(data.get("actor"))
        # RULE 4, AND IT KEYS ON THE START RATHER THAN ON THE NUMBER. A start at an
        # ordinal that already has one is the turn running again, so every TERMINAL
        # marker the row carries -- the refusal that stopped the last try, the answer
        # the last try settled on, and the draft that answer commits from -- is
        # retracted here. The row keeps its prompt and its anchor: the rerun re-runs
        # the same question.
        #
        # Keying on ``attempt`` alone is not enough, because a rerun is numbered only
        # when the writer can number it. ``on_turn_refused`` does not advance the
        # attempt counter, so a refused turn that is rewound or regenerated starts
        # again at attempt 1 with the field omitted -- and a row would then show one
        # try's refusal beside the next try's answer.
        row.pop("refused", None)
        row["reply"] = ""
        state["draft"] = ""
        state["draft_turn"] = 0
        state["draft_step"] = 0
        state["draft_raw"] = ""
        state["draft_unreadable"] = False
        attempt = data.get("attempt")
        if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 1:
            # RECORDED separately from the retraction, because which try this is and
            # what the last try produced are two different facts: a numbered rerun says
            # so on the row, an unnumbered one is still a rerun.
            row["attempt"] = attempt
        return

    if entry.type == "turn/refused":
        # A turn a gate stopped before it ran. Recorded so a reader sees a turn that
        # produced nothing rather than one still open -- and the row exists at all
        # only because rows are anchored on the ordinal; see the section note.
        row["actor"] = _as_str(data.get("actor"))
        row["refused"] = _as_str(data.get("reason"))
        # RETRACTED HERE TOO, and not only at ``turn/started``. A refusal is written
        # WITHOUT a start -- the gates run between the two, which is the same ordering
        # the ordinal anchor exists for -- so a rerun that is refused never reaches the
        # retraction a start carries. Regenerate removes a settled answer and runs the
        # turn again; Stop while it is still preparing lands exactly here, and leaving
        # the committed reply would show the answer that was REMOVED beside the reason
        # the rerun produced none. "This ordinal produced nothing" has to retract both
        # the committed reply and any standing draft, or it is only half said.
        row["reply"] = ""
        state["draft"] = ""
        state["draft_turn"] = 0
        state["draft_step"] = 0
        state["draft_raw"] = ""
        state["draft_unreadable"] = False
        return

    # turn/completed -- rule 3: the draft commits here and nowhere earlier.
    if state["draft"] and state["draft_turn"] == turn:
        row["reply"] = state["draft"]
    state["draft"] = ""
    state["draft_turn"] = 0
    state["draft_step"] = 0
    state["draft_raw"] = ""
    state["draft_unreadable"] = False


def _outline_copy(state: dict[str, Any]) -> dict[str, Any]:
    """Every container ``_outline_step`` can reach, copied; the rest shared.

    Spelled out rather than left to the deep copy because this fold wakes on the
    message entries that are the bulk of a log, and a deep copy of the window would
    pay for the whole window on each of them. Two levels is all there is: the row list
    and each row, whose values are scalars.
    """
    return {
        **state,
        "turns": [dict(row) for row in state["turns"]],
    }


def _outline_render(state: dict[str, Any]) -> dict[str, Any]:
    turns: list[dict[str, Any]] = state["turns"]
    return {
        "turns": [dict(row) for row in turns],
        # The window's own account of itself, beside the rows rather than inferred.
        "dropped": state["dropped"],
        "limit": OUTLINE_TURN_LIMIT,
        # The clamps the previews were built with, so a block rendering a row knows
        # what a trailing ellipsis means without reading the fold's code.
        "prompt_chars": OUTLINE_PROMPT_CHARS,
        "reply_chars": OUTLINE_REPLY_CHARS,
        "first_turn": turns[0]["turn"] if turns else None,
        "last_turn": turns[-1]["turn"] if turns else None,
    }
