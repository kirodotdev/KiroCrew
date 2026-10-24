"""What a turn's entries record: the turn, its bodies, context, steps and tool calls.

It pins the fields the turn-time families write: a turn's ordinals, token counts, cost and
actor, message bodies and the chunks an oversize body splits into, the request
configuration, the composed context, model-call steps, tool calls that settle exactly once,
queued messages, the attempt a rerun of one ordinal carries and what a turn-end closer may
claim; and it holds every emitted type to the shape its type documents. How a log opens is
pinned in `test_crew_log_emit.py`; attempt seeding, closer placement, redaction and the
flag in `test_crew_log_emit_redaction_and_flag.py`; and the writer in
`test_crew_log_emit_write_path.py`.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from crew_log_emit_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    SESSION,
    _body,
    _isolated_home,
    _log_path,
    _open_session,
    _tool_result_event,
    _typed,
)

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import emit
from kiro_crew.crew_log import projection as crew_log_projection

# --- a whole turn ----------------------------------------------------------


def test_one_full_turn_produces_contiguous_seq():
    _open_session()
    emit.on_turn_started(SESSION, 4, "user")
    emit.on_tool_called(SESSION, 4, name="fs_read", call_id="tc-1")
    emit.on_tool_completed(SESSION, 4, name="fs_read", status="completed", call_id="tc-1")
    emit.on_turn_completed(
        SESSION,
        4,
        input_tokens=1200,
        output_tokens=340,
        cache_read_tokens=9000,
        cache_write_tokens=15,
        credits=0.42,
        duration_ms=8123,
        stop_reason="end_turn",
        model="claude-opus-5",
        provider="kiro",
    )
    body = _body()
    assert [e["seq"] for e in body] == list(range(1, len(body) + 1))
    assert [e["type"] for e in body] == [
        "session/opened",
        "turn/started",
        "tool/called",
        "tool/completed",
        "turn/completed",
    ]


def test_every_entry_of_a_turn_names_that_turn_in_its_data():
    # The turn identity is CARRIED, not looked up: the runner's ordinal is in
    # data.turn on every entry the turn produced, so no in-process state has to
    # survive for an entry to say which turn it belongs to.
    _open_session()
    emit.on_turn_started(SESSION, 4, "user")
    emit.on_tool_called(SESSION, 4, name="fs_read", call_id="tc-1")
    emit.on_tool_completed(SESSION, 4, name="fs_read", status="completed", call_id="tc-1")
    emit.on_turn_completed(SESSION, 4, stop_reason="end_turn")
    by_type = {e["type"]: e for e in _body()}
    for kind in ("turn/started", "tool/called", "tool/completed", "turn/completed"):
        assert by_type[kind]["data"]["turn"] == 4, kind


def test_no_session_entry_carries_a_thread():
    # `thread` points at another LINE's seq, which is only knowable for a unit
    # whose anchor is written before the entries citing it -- the crew log's
    # shape. A session entry names its turn in data instead.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    emit.on_tool_completed(SESSION, 1, status="completed", call_id="tc-1")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit.on_compaction_applied(SESSION, pct_before=0.9, pct_after=0.3)
    emit.on_session_closed(SESSION, "reset")
    for entry in _body():
        assert "thread" not in entry, entry["type"]


def test_entries_outside_a_turn_carry_no_thread():
    _open_session()
    emit.on_session_closed(SESSION, "reset")
    for entry in _body():
        assert "thread" not in entry


def test_two_turns_are_told_apart_by_their_own_ordinals():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit.on_turn_started(SESSION, 2, "user")
    emit.on_tool_called(SESSION, 2, name="fs_read", call_id="tc-2")
    tool = [e for e in _body() if e["type"] == "tool/called"][0]
    assert tool["data"]["turn"] == 2
    starts = [e["data"]["turn"] for e in _body() if e["type"] == "turn/started"]
    assert starts == [1, 2]


def test_a_tool_call_carries_its_position_in_the_turn_and_the_completion_reuses_it():
    # A step orders the calls of one turn without a reader comparing seq, and
    # tells two calls of the same tool apart when their ids are opaque.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-2")
    emit.on_tool_completed(SESSION, 1, status="completed", call_id="tc-1")
    called = [e for e in _body() if e["type"] == "tool/called"]
    assert [e["data"]["call_index"] for e in called] == [1, 2]
    done = [e for e in _body() if e["type"] == "tool/completed"][0]
    assert done["data"]["call_index"] == 1


def test_call_index_numbering_restarts_with_each_turn():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit.on_turn_started(SESSION, 2, "user")
    emit.on_tool_called(SESSION, 2, name="fs_read", call_id="tc-2")
    steps = {
        e["data"]["turn"]: e["data"]["call_index"] for e in _body() if e["type"] == "tool/called"
    }
    assert steps == {1: 1, 2: 1}


def test_turn_completed_carries_the_four_token_counts_and_cost():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_turn_completed(
        SESSION,
        1,
        input_tokens=11,
        output_tokens=22,
        cache_read_tokens=33,
        cache_write_tokens=44,
        credits=1.5,
        duration_ms=99,
        stop_reason="end_turn",
    )
    data = _body()[-1]["data"]
    assert data["tokens"] == {
        "input": 11,
        "output": 22,
        "cache_read": 33,
        "cache_write": 44,
    }
    assert data["credits"] == 1.5
    assert data["duration_ms"] == 99
    assert data["stop_reason"] == "end_turn"


def test_turn_completed_omits_tokens_the_provider_never_reported():
    """An unreported count is ABSENT, not a block of four zeros.

    ``TurnUsage`` zero-fills every dimension a provider does not report, so at this
    seam four zeros mean "nothing was reported". Written as a block they are a
    measurement the fold then counts as a reporting turn, and the panel shows a
    measured ``0 tokens`` beside a real bill. ``credits`` stays: it was billed.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_turn_completed(SESSION, 1, credits=1.49, duration_ms=99, stop_reason="end_turn")
    data = _body()[-1]["data"]
    assert "tokens" not in data
    assert data["credits"] == 1.49
    assert data["stop_reason"] == "end_turn"


def test_turn_completed_keeps_all_four_dimensions_once_any_is_measured():
    """A reported block answers every billed dimension, zeros included.

    Unlike ``background/completed``, which drops its zero members, the
    ``turn/completed`` schema requires all four inside a present ``tokens`` -- so
    a provider that reported cache reads alone still writes the other three as
    real zeros rather than as a sparse mapping a strict reader would refuse.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_turn_completed(SESSION, 1, cache_read_tokens=7, stop_reason="end_turn")
    data = _body()[-1]["data"]
    assert data["tokens"] == {"input": 0, "output": 0, "cache_read": 7, "cache_write": 0}


def test_turn_completed_omits_an_unbilled_credits_charge():
    """The same writer, the same fault, the other field: a zero charge is absent.

    A provider that does not bill in credits reports 0.0 through the shared
    ``TurnUsage`` contract, indistinguishable at this seam from a free turn -- the
    posture ``background/completed`` and both subagent closers already take. The
    token counts beside it are untouched: the two fields are guarded one by one.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_turn_completed(
        SESSION, 1, input_tokens=11, output_tokens=22, duration_ms=99, stop_reason="end_turn"
    )
    data = _body()[-1]["data"]
    assert "credits" not in data
    assert data["tokens"] == {"input": 11, "output": 22, "cache_read": 0, "cache_write": 0}


def test_turn_completed_drops_a_charge_that_is_not_a_finite_positive_number():
    """NaN, infinity and a negative are not charges; none of them is written."""
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    for turn, charge in enumerate((float("nan"), float("inf"), -0.5), start=1):
        emit.on_turn_completed(SESSION, turn, credits=charge, stop_reason="end_turn")
    closers = [e["data"] for e in _body() if e["type"] == "turn/completed"]
    assert len(closers) == 3
    assert all("credits" not in data for data in closers)


def test_a_provider_that_bills_in_tokens_alone_folds_as_credits_unreported():
    """Writer to fold, for the provider shape the credits guard is for.

    ``TurnUsage`` leaves ``credits`` at 0.0 for a provider that bills in tokens
    (``claude_code``, ``bedrock``: token counts plus ``cost_usd``), and the runner
    hands that 0.0 to the closer as-is. Written as a charge, the ``usage`` fold
    counts every such turn in ``credits_reported`` and the panel's credits tile
    prints a measured ``0`` beside real token counts -- the same shape as the token
    defect, on the other field. The fold is given the writer's own output, so this
    holds the writer to what the panel will show.
    """
    _open_session()
    for turn in (1, 2, 3):
        emit.on_turn_started(SESSION, turn, "user")
        emit.on_turn_completed(
            SESSION,
            turn,
            input_tokens=400,
            output_tokens=90,
            credits=0.0,
            duration_ms=800,
            stop_reason="end_turn",
            model="claude",
            provider="claude_code",
        )
    assert emit.flush()
    usage = crew_log_projection.read_projection(SESSION, "usage").value
    assert usage["turns"]["completed"] == 3
    assert usage["turns"]["tokens_reported"] == 3
    assert usage["tokens"]["total"] == 3 * 490
    assert usage["turns"]["credits_reported"] == 0
    assert usage["credits_by_source"]["turn"] == {"credits": 0.0, "reported": 0}
    assert usage["by_model"]["claude"]["credits_reported"] == 0


def test_an_aborted_turn_leaves_a_started_with_no_completion():
    _open_session()
    emit.on_turn_started(SESSION, 2, "cron")
    types = [e["type"] for e in _body()]
    assert types.count("turn/started") == 1
    assert "turn/completed" not in types


def test_a_recovery_reentry_is_its_own_turn_pair_marked_by_depth():
    _open_session()
    emit.on_turn_started(SESSION, 5, "user", depth=0)
    emit.on_turn_completed(SESSION, 5, stop_reason="cancelled", depth=0)
    emit.on_turn_started(SESSION, 5, "user", depth=1)
    emit.on_turn_completed(SESSION, 5, stop_reason="end_turn", depth=1)
    turns = [e for e in _body() if e["type"].startswith("turn/")]
    assert [e["data"]["depth"] for e in turns] == [0, 0, 1, 1]


def test_every_actor_the_dispatcher_distinguishes_is_recorded_verbatim():
    _open_session()
    for actor in ("user", "app", "cron", "autonudge", "subagent", "crew"):
        emit.on_turn_started(SESSION, 1, actor)
    recorded = [e["data"]["actor"] for e in _body() if e["type"] == "turn/started"]
    assert recorded == ["user", "app", "cron", "autonudge", "subagent", "crew"]


def test_an_unknown_actor_is_recorded_as_other_not_guessed():
    _open_session()
    emit.on_turn_started(SESSION, 1, "something-new")
    assert _body()[-1]["data"]["actor"] == "other"


def test_an_app_authored_send_is_not_dispatched_as_a_person():
    """The send API names the app it already identified.

    The actor resolver's fallback is ``user``, so a dispatch site that observes
    a non-person producer and passes no actor does not leave the field absent --
    it states that a person typed the message. ``_api_chat`` holds that fact in
    ``request_app`` (stamped by the app-token auth middleware, which a person
    cannot write), so the branch that reads it must also hand it on.
    """
    import ast
    import inspect

    from kiro_crew.dashboard import chat_handlers

    tree = ast.parse(inspect.getsource(chat_handlers.api_chat))
    kwargs_writes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "_turn_kwargs"
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "_turn_actor"
    ]
    assert kwargs_writes, (
        "an app-authored send reaches the turn with no actor, so the crew log "
        "records a person who never typed anything"
    )


# --- tools, approvals, model, compaction ----------------------------------


def test_a_tool_call_is_identified_by_id_in_data_and_records_no_arguments():
    _open_session()
    emit.on_turn_started(SESSION, 3, "user")
    emit.on_tool_called(SESSION, 3, name="execute_bash", server="", kind="execute", call_id="tc-9")
    entry = _body()[-1]
    # ref is a citation of another crew log's lines, so a tool call id is data.
    assert "ref" not in entry
    assert entry["data"] == {
        "turn": 3,
        "call_id": "tc-9",
        "name": "execute_bash",
        "server": "",
        "kind": "execute",
        "call_index": 1,
    }


# --- message bodies -------------------------------------------------------


def test_an_observed_class_is_recorded_when_a_link_commits():
    """MUTATION-SENSITIVE: the record is taken when the class BECOMES true.

    The per-turn observation cannot see a link that commits and is removed inside one
    turn, and content authored through it is in the log with nothing saying it was
    published. So the surfaces that commit a link record the class themselves, and this
    is the entry point they call.
    """
    _open_session()
    emit.on_class_observed(SESSION, memory="persistent", channel=True)
    assert emit.flush()
    entry = _body()[-1]
    assert entry["type"] == "session/class"
    assert entry["data"]["channel"] is True
    assert entry["data"]["memory"] == "persistent"


def test_a_class_that_has_not_moved_records_nothing():
    """MUTATION-SENSITIVE: the recorder is free to call from every link site.

    Every assignment site calls it, several of which cannot have changed anything --
    a cron link, a slot being created. Writing a line for each would fill a log with
    restatements, so the latch is what makes calling it everywhere the cheap option.
    Without this the previous test passes while the log grows a line per bind.
    """
    _open_session()
    emit.on_class_observed(SESSION, memory="persistent", channel=True)
    assert emit.flush()
    before = len(_body())
    emit.on_class_observed(SESSION, memory="persistent", channel=True)
    assert emit.flush()
    assert len(_body()) == before, "a restatement of the same class is not a move"


def test_an_observed_class_with_no_memory_mode_records_nothing():
    """A fragment is refused here rather than appended and refused by every reader.

    The opening entry applies the same rule: a log that states no class refuses every
    test built on one, and appending a transition to it would leave a history with a
    middle and no beginning.
    """
    _open_session()
    emit.on_class_observed(SESSION, memory="", channel=True)
    assert emit.flush()
    assert [e["type"] for e in _body()].count("session/class") == 0


def test_a_received_message_records_its_body_role_and_surface():
    _open_session()
    _typed(1, "what broke?", source="dashboard", role="user")
    assert emit.flush()
    entry = _body()[-1]
    assert entry["type"] == "message/received"
    assert entry["data"]["text"] == "what broke?"
    assert entry["data"]["role"] == "user"
    assert entry["data"]["source"] == "dashboard"
    assert entry["data"]["turn"] == 1


def test_a_received_message_is_recorded_even_when_the_turn_is_then_refused():
    # The body is written before the dispatch gates, so the turns a reader most
    # wants explained -- the refused ones -- are not the ones missing their text.
    _open_session()
    _typed(1, "run it")
    emit.on_turn_refused(SESSION, 1, "not_authorized")
    assert emit.flush()
    kinds = [e["type"] for e in _body()]
    assert "message/received" in kinds
    assert "turn/refused" in kinds
    assert "turn/started" not in kinds


@pytest.mark.parametrize("reason", ["blocked", "too_large"])
def test_a_refused_prompt_expansion_records_the_input_and_the_refusal(reason):
    # A blocked or oversized @prompt expansion returns before the normal emit
    # block, so without the reorder it left the accepted turn with NO entry. It
    # now records the same pair every dispatch gate does: the accepted input, then
    # a turn/refused naming the reason -- and nothing derived from a request the
    # refused turn never assembled.
    _open_session()
    emit.on_message_received(SESSION, 1, role="user", text="@secret run it", source="dashboard")
    emit.on_turn_refused(SESSION, 1, reason, "user", depth=0)
    assert emit.flush()
    received = [e for e in _body() if e["type"] == "message/received"]
    refused = [e for e in _body() if e["type"] == "turn/refused"]
    assert len(received) == 1
    assert received[0]["data"]["text"] == "@secret run it"
    assert len(refused) == 1
    assert refused[0]["data"]["reason"] == reason
    kinds = [e["type"] for e in _body()]
    assert "turn/started" not in kinds
    assert "request/configured" not in kinds
    assert "context/composed" not in kinds


def test_an_accepted_expansion_records_the_input_exactly_once():
    # The accepted path is unchanged: one message/received, no turn/refused. The
    # refusal reorder must not double-write on the path the gate did not take.
    _open_session()
    emit.on_message_received(SESSION, 1, role="user", text="hello", source="dashboard")
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()
    received = [e for e in _body() if e["type"] == "message/received"]
    assert len(received) == 1
    assert [e["type"] for e in _body() if e["type"] == "turn/refused"] == []


def test_chat_runner_records_the_input_and_refusal_on_a_blocked_expansion():
    # The reorder lives in _run_chat: the blocked/too_large @prompt branch must
    # emit on_message_received then on_turn_refused, mirroring the not_authorized
    # gate. Asserted structurally rather than by driving the whole runner.
    import ast

    from kiro_crew.dashboard import chat_runner

    tree = ast.parse(
        "\n".join(
            turn_file.read_text(encoding="utf-8")
            for turn_file in [
                *sorted(Path(chat_runner.__file__).with_name("chat_turn").glob("*.py")),
                Path(chat_runner.__file__),
            ]
            if turn_file.name != "__init__.py"
        )
    )
    src = "\n".join(
        turn_file.read_text(encoding="utf-8")
        for turn_file in [
            *sorted(Path(chat_runner.__file__).with_name("chat_turn").glob("*.py")),
            Path(chat_runner.__file__),
        ]
        if turn_file.name != "__init__.py"
    )
    assert "🔒 Prompt blocked" in src

    def _emit_attr(node, name):
        # A crew_log_emit.<name>(...) call, however the module is aliased.
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == name
        )

    def _string_in(node, needle):
        return any(
            isinstance(c, ast.Constant) and isinstance(c.value, str) and needle in c.value
            for c in ast.walk(node)
        )

    # Find every statement block (an if/elif body, orelse, loop or with body) that
    # appends the "Prompt blocked" notice, and confirm the refusal pair lives IN
    # THAT SAME block -- not merely somewhere in the 9000-line function. A refactor
    # that moved the refusal elsewhere, or dropped the paired message/received,
    # must fail this.
    blocks = []
    for node in ast.walk(tree):
        for body in (getattr(node, "body", None), getattr(node, "orelse", None)):
            if not isinstance(body, list):
                continue
            for stmt in body:
                if (
                    isinstance(stmt, ast.Expr)
                    and _string_in(stmt.value, "🔒 Prompt blocked")
                    and body not in blocks
                ):
                    blocks.append(body)

    assert blocks, "no block appends the '🔒 Prompt blocked' notice"

    def _co_located(block):
        received_at = None
        refused_at = None
        for i, stmt in enumerate(block):
            if not isinstance(stmt, ast.Expr):
                continue
            call = stmt.value
            if _emit_attr(call, "on_message_received"):
                received_at = i
            elif _emit_attr(call, "on_turn_refused") and any(
                ast.unparse(a) == "_status" for a in call.args
            ):
                refused_at = i
        return received_at is not None and refused_at is not None and received_at < refused_at

    assert any(_co_located(block) for block in blocks), (
        "the blocked/too_large branch must emit on_message_received then "
        "on_turn_refused(_status, ...) in the SAME block as the 'Prompt blocked' "
        "notice"
    )


def test_a_step_less_context_composed_is_written_for_the_first_call(tmp_path):
    # on_context_composed is emitted before the first step/started, so it carries
    # no step. The join is a reader rule -- a step-less composition belongs
    # to the turn's first model call -- and the entry is still written and still
    # ordered after the message it was derived from.
    _open_session()
    emit.on_message_received(SESSION, 1, role="user", text="do it", source="dashboard")
    emit.on_context_composed(SESSION, 1, blocks={"system": 400})
    emit.on_step_started(SESSION, 1)
    assert emit.flush()
    body = _body()
    kinds = [e["type"] for e in body]
    composed = [e for e in body if e["type"] == "context/composed"]
    assert len(composed) == 1
    assert "step" not in composed[0]["data"], "the turn-opening composition is step-less"
    # Ordering invariant: the composition is written after the message it derives
    # from and before the first model call opens.
    assert kinds.index("message/received") < kinds.index("context/composed")
    assert kinds.index("context/composed") < kinds.index("step/started")


def test_attachments_are_identifiers_not_refs():
    # An attachment is not a crew log unit, so it cannot be cited by a Ref -- the
    # same reason a tool call id lives in data.
    _open_session()
    _typed(1, "see this", attachments=["img-1", "img-2"])
    assert emit.flush()
    entry = _body()[-1]
    assert entry["data"]["attachments"] == ["img-1", "img-2"]
    assert "attachments_omitted" not in entry["data"]
    assert "ref" not in entry


def test_no_attachment_key_when_there_are_none():
    _open_session()
    _typed(1, "plain")
    assert emit.flush()
    assert "attachments" not in _body()[-1]["data"]


def test_a_sent_message_records_the_text_and_the_model_call_it_came_from():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_message_sent(SESSION, 1, step=2, text="the log was rotated")
    assert emit.flush()
    entry = _body()[-1]
    assert entry["type"] == "message/sent"
    assert entry["data"]["text"] == "the log was rotated"
    assert entry["data"]["step"] == 2


def test_an_interrupted_reply_says_so_and_a_normal_one_stays_silent():
    _open_session()
    emit.on_message_sent(SESSION, 1, text="half a thou")
    emit.on_message_sent(SESSION, 1, text="all of it", interrupted=True)
    assert emit.flush()
    sent = [e for e in _body() if e["type"] == "message/sent"]
    assert "interrupted" not in sent[0]["data"]
    assert sent[1]["data"]["interrupted"] is True


def test_a_body_too_big_for_one_line_is_split_and_cited_in_order():
    _open_session()
    # Sized from the format's ceiling, not from the slice: the slice decides how
    # big each piece is, the ceiling decides whether splitting happens at all.
    huge = "x" * (lg.MAX_ENTRY_BYTES + 1000)
    expected = math.ceil(len(huge) / emit._CHUNK_TEXT_CHARS)
    emit.on_message_sent(SESSION, 4, step=1, text=huge)
    assert emit.flush()
    body = _body()
    chunks = [e for e in body if e["type"] == "message/chunk"]
    sent = [e for e in body if e["type"] == "message/sent"][-1]
    assert len(chunks) == expected
    # The citation is in order, and reassembling it returns the original text.
    assert sent["data"]["chunks"] == [c["seq"] for c in chunks]
    assert "".join(c["data"]["delta"] for c in chunks) == huge
    assert sent["data"]["chars"] == len(huge)
    assert "text" not in sent["data"]


def test_a_body_that_fits_is_not_split_at_all():
    # The slice size is not the trigger. A body several times the slice still
    # rides on one line when the line can hold it.
    _open_session()
    emit.on_message_sent(SESSION, 1, text="x" * (emit._CHUNK_TEXT_CHARS * 2))
    assert emit.flush()
    assert not [e for e in _body() if e["type"] == "message/chunk"]
    assert _body()[-1]["data"]["text"]


def test_every_split_line_is_inside_the_format_ceiling():
    _open_session()
    emit.on_message_sent(SESSION, 1, text="\u4e2d" * (emit._CHUNK_TEXT_CHARS * 2))
    assert emit.flush()
    # Non-ASCII is the case the slice size exists for: ensure_ascii turns one
    # character into six bytes, so a slice measured in characters alone would be
    # refused at append time and the body would vanish.
    for line in _log_path().read_text(encoding="utf-8").splitlines():
        assert len(line.encode("utf-8")) <= lg.MAX_ENTRY_BYTES


def test_an_astral_body_still_splits_inside_the_ceiling():
    # A character outside the BMP escapes as a SURROGATE PAIR -- twelve bytes, not
    # six -- so a slice sized for the six-byte case is refused, and a refused chunk
    # kills the whole split and loses the body rather than just cutting it badly.
    _open_session()
    body = "\U0001f600" * (emit._CHUNK_TEXT_CHARS + 500)
    emit.on_message_sent(SESSION, 1, text=body)
    assert emit.flush()
    chunks = [e for e in _body() if e["type"] == "message/chunk"]
    sent = [e for e in _body() if e["type"] == "message/sent"]
    assert chunks, "the body was not split"
    assert sent, "the body was lost: no message/sent followed the chunks"
    assert "".join(c["data"]["delta"] for c in chunks) == body
    assert sent[-1]["data"]["chunks"] == [c["seq"] for c in chunks]
    for line in _log_path().read_text(encoding="utf-8").splitlines():
        assert len(line.encode("utf-8")) <= lg.MAX_ENTRY_BYTES


def test_a_lone_surrogate_body_never_raises_into_the_caller():
    # A JSON payload can carry one. Fail-soft means it is dropped or escaped, not
    # raised at the call site.
    _open_session()
    emit.on_message_received(SESSION, 1, text="before \ud800 after")
    emit.on_message_sent(SESSION, 1, text="reply \ud800 here")
    assert emit.flush()


def test_an_oversize_received_body_is_split_like_a_sent_one():
    # Both bodies go through one seam, so a pasted message too big for a line gets
    # the same split. Written straight onto the entry it would be refused at append
    # time and the message would be lost whole.
    _open_session()
    body = "p" * (lg.MAX_ENTRY_BYTES + 1000)
    emit.on_message_received(SESSION, 1, text=body, source="dashboard")
    assert emit.flush()
    chunks = [e for e in _body() if e["type"] == "message/chunk"]
    got = [e for e in _body() if e["type"] == "message/received"]
    assert chunks, "an oversize received body was not split"
    assert got, "the received entry was lost"
    assert got[-1]["data"]["chunks"] == [c["seq"] for c in chunks]
    assert "".join(c["data"]["delta"] for c in chunks) == body
    # The non-body fields still ride on the citing entry.
    assert got[-1]["data"]["role"] == "user"
    assert got[-1]["data"]["source"] == "dashboard"
    for line in _log_path().read_text(encoding="utf-8").splitlines():
        assert len(line.encode("utf-8")) <= lg.MAX_ENTRY_BYTES


def test_oversize_attachment_metadata_keeps_the_received_message():
    _open_session()
    attachments = [f"file-{index}-" + "\U0001f600" * 1024 for index in range(12)]
    cases = (
        ("small body", False),
        ("p" * (lg.MAX_ENTRY_BYTES + 1000), True),
    )

    for turn, (body, split) in enumerate(cases, start=1):
        emit.on_message_received(
            SESSION,
            turn,
            text=body,
            source="dashboard",
            attachments=attachments,
        )
    assert emit.flush()

    received = [entry for entry in _body() if entry["type"] == "message/received"]
    assert len(received) == len(
        cases
    ), "an accepted message was lost when attachment metadata exceeded the line ceiling"
    chunks = {entry["seq"]: entry for entry in _body() if entry["type"] == "message/chunk"}
    for entry, (body, split) in zip(received, cases, strict=True):
        data = entry["data"]
        kept = data.get("attachments", [])
        assert kept == attachments[: len(kept)]
        assert data["attachments_omitted"] == len(attachments) - len(kept)
        if split:
            assert "text" not in data
            assert "".join(chunks[seq]["data"]["delta"] for seq in data["chunks"]) == body
        else:
            assert data["text"] == body
            assert "chunks" not in data
    assert emit.dropped_writes() == 0
    for line in _log_path().read_text(encoding="utf-8").splitlines():
        assert len(line.encode("utf-8")) <= lg.MAX_ENTRY_BYTES


def test_every_body_family_produces_one_body_representation():
    # FR-7 is amended in this change to include bodies, so the redacted body is
    # what goes in the log. The invariant is the SHAPE every body-bearing entry
    # has, asserted on the entries rather than on the source that produces them:
    # exactly one of `text` or `chunks`, never both and never neither. That holds
    # through any refactor of how the seam is written, and it is what a reader
    # actually depends on.
    assert emit.BODY_MODE == emit.BODY_MODE_TEXT
    _open_session()
    small, large = "a short body", "L" * (lg.MAX_ENTRY_BYTES + 400)
    emit.on_turn_started(SESSION, 1, "user")
    _typed(1, small)
    emit.on_message_sent(SESSION, 1, step=1, text=small)
    _typed(2, large)
    emit.on_message_sent(SESSION, 2, step=1, text=large)
    assert emit.flush()

    families = {"message/received", "message/sent"}
    seen: set[str] = set()
    for entry in _body():
        if entry["type"] not in families:
            continue
        seen.add(entry["type"])
        data = entry["data"]
        has_text, has_chunks = "text" in data, "chunks" in data
        assert has_text != has_chunks, (
            f"{entry['type']} carries {'both' if has_text else 'neither'} "
            f"text and chunks: {sorted(data)}"
        )
        if has_chunks:
            # A citation must name real entries and carry the original length.
            assert data["chunks"], "an empty chunk citation"
            assert data.get("chars", 0) > 0
    assert seen == families, f"a body family produced no entry: {sorted(families - seen)}"


def test_overflow_chunks_carry_a_body_that_would_otherwise_be_lost():
    # The only producer of `message/chunk`. Written unconditionally, because the
    # alternative is an append refused for size and a body missing from the log.
    _open_session()
    emit.on_message_sent(SESSION, 1, text="y" * (lg.MAX_ENTRY_BYTES + 1000))
    assert emit.flush()
    assert [e["type"] for e in _body() if e["type"] == "message/chunk"]


def test_a_split_chunk_is_marked_ignorable():
    # A reader that does not know the type may skip them and still read the
    # message/sent that cites them.
    _open_session()
    emit.on_message_sent(SESSION, 1, text="z" * (lg.MAX_ENTRY_BYTES + 1000))
    assert emit.flush()
    marked = [e for e in _body() if e["type"] == "message/chunk"]
    assert marked
    for entry in marked:
        assert entry["ignorable"] is True


# --- streamed deltas ------------------------------------------------------


def test_the_request_configuration_is_written_once_and_then_only_on_change():
    _open_session()
    for turn in (1, 2, 3):
        emit.on_request_configured(
            SESSION, turn, model="claude-opus-5", provider="acp", context_window=200000
        )
    emit.on_request_configured(
        SESSION, 4, model="claude-sonnet-5", provider="acp", context_window=200000
    )
    assert emit.flush()
    written = [e for e in _body() if e["type"] == "request/configured"]
    # Three identical turns produce ONE entry; the swap produces the second.
    assert [e["data"]["turn"] for e in written] == [1, 4]
    assert [e["data"]["model"] for e in written] == ["claude-opus-5", "claude-sonnet-5"]


def test_the_system_prompt_is_recorded_as_a_digest_never_as_text():
    _open_session()
    emit.on_request_configured(SESSION, 1, model="m", provider="acp", system="SECRET PROMPT")
    assert emit.flush()
    data = _body()[-1]["data"]
    assert "SECRET PROMPT" not in json.dumps(data)
    assert len(data["system"]) == 64
    assert data["system_bytes"] == len("SECRET PROMPT")


def test_a_changed_system_prompt_alone_is_a_change():
    _open_session()
    emit.on_request_configured(SESSION, 1, model="m", provider="acp", system="one")
    emit.on_request_configured(SESSION, 2, model="m", provider="acp", system="two")
    assert emit.flush()
    assert len([e for e in _body() if e["type"] == "request/configured"]) == 2


def test_the_configuration_carries_no_tool_list_at_all():
    # The gateway never receives the resolved tool list -- the backend serves
    # specs with tool search on. An empty list every turn would read as "no
    # tools", which is false, so the field is absent instead.
    _open_session()
    emit.on_request_configured(SESSION, 1, model="m", provider="acp", context_window=1)
    assert emit.flush()
    assert "tools" not in _body()[-1]["data"]


def test_a_reopened_session_writes_its_configuration_again():
    _open_session()
    emit.on_request_configured(SESSION, 1, model="m", provider="acp")
    emit.on_session_closed(SESSION, "reset")
    assert emit.flush()
    _open_session()
    emit.on_request_configured(SESSION, 1, model="m", provider="acp")
    assert emit.flush()
    assert len([e for e in _body() if e["type"] == "request/configured"]) == 2


def test_a_failed_configuration_append_is_retried_not_suppressed(monkeypatch):
    # Remembering the fingerprint before the append landed would turn one
    # transient failure into a permanent gap: every later identical configuration
    # would be suppressed as unchanged, and the session would carry no record of
    # what it ran as.
    _open_session()
    assert emit.flush()
    original = lg.CrewLog.append
    failures = {"left": 1}

    def _fail_once(self, entry_type, *args, **kwargs):
        if entry_type == "request/configured" and failures["left"]:
            failures["left"] -= 1
            raise lg.CrewLogError("disk said no", code=lg.CODE_BAD_DATA)
        return original(self, entry_type, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _fail_once)
    emit.on_request_configured(SESSION, 1, model="m", provider="acp", context_window=7)
    assert emit.flush()
    assert not [e for e in _body() if e["type"] == "request/configured"]
    # Same configuration, next turn: it must be written, not treated as unchanged.
    emit.on_request_configured(SESSION, 2, model="m", provider="acp", context_window=7)
    assert emit.flush()
    written = [e for e in _body() if e["type"] == "request/configured"]
    assert len(written) == 1
    assert written[0]["data"]["turn"] == 2


# --- composed context -----------------------------------------------------


def test_composed_context_itemises_the_blocks_with_estimated_tokens():
    _open_session()
    emit.on_context_composed(
        SESSION, 1, step=1, blocks={"lessons": 400, "memory": 800}, total_chars=1200
    )
    assert emit.flush()
    data = _body()[-1]["data"]
    # Newest-biggest first, characters exact, tokens derived at 4 chars each.
    assert data["sources"] == [
        {"kind": "memory", "chars": 800, "tokens": 200},
        {"kind": "lessons", "chars": 400, "tokens": 100},
    ]
    assert data["chars"] == 1200
    assert data["tokens"] == 300


def test_the_token_count_says_that_it_is_an_estimate():
    # The only tokenizer available is the wrong one for the served model, so the
    # entry admits the number is derived rather than measured.
    _open_session()
    emit.on_context_composed(SESSION, 1, blocks={"memory": 40})
    assert emit.flush()
    assert _body()[-1]["data"]["tokens_estimated"] is True


@pytest.mark.parametrize(
    ("label", "recorded"),
    [
        # Steering, tool specs and injected ledger context have no marker, so their
        # characters are genuinely a remainder -- and `unclassified` is what the
        # splitter calls it. The label passes through: its readers carry a translated
        # string for that name and none for a catch-all, so renaming it here makes a
        # named remainder render as an untranslated word in every shipped locale.
        pytest.param(
            "unclassified", "unclassified", id="test_the_named_remainder_keeps_its_own_label"
        ),
        # An empty label names nothing, so no reader can be given a translation for
        # it. This is the ONLY label the emitter renames.
        pytest.param("", "other", id="test_a_source_with_no_label_at_all_is_reported_as_other"),
    ],
)
def test_a_remainder_source_keeps_a_label_a_reader_can_translate(label, recorded):
    _open_session()
    emit.on_context_composed(SESSION, 1, blocks={label: 100, "lessons": 40})
    assert emit.flush()
    kinds = [s["kind"] for s in _body()[-1]["data"]["sources"]]
    assert kinds == [recorded, "lessons"]


def test_empty_and_negative_blocks_are_dropped_rather_than_recorded():
    _open_session()
    emit.on_context_composed(SESSION, 1, blocks={"memory": 0, "lessons": -5, "skill_index": 8})
    assert emit.flush()
    assert [s["kind"] for s in _body()[-1]["data"]["sources"]] == ["skill_index"]


def test_no_blocks_writes_nothing():
    _open_session()
    before = len(_body())
    emit.on_context_composed(SESSION, 1, blocks={})
    emit.on_context_composed(SESSION, 1, blocks=None)
    assert emit.flush()
    assert len(_body()) == before


def test_budget_cuts_ride_beside_the_blocks_and_pass_the_declaration():
    # The entry is validated against its declaration on write; an undeclared member
    # would refuse the WHOLE entry and lose the block sizes too.
    _open_session()
    cut = {
        "lesson_rules": {"reason": "budget", "omitted": 23, "total": 92, "budget_chars": 37000},
        "background": {"reason": "budget", "sections": ["skills"]},
    }
    emit.on_context_composed(SESSION, 1, blocks={"lessons": 400}, omitted=cut)
    assert emit.flush()
    data = _body()[-1]["data"]
    assert data["sources"] == [{"kind": "lessons", "chars": 400, "tokens": 100}]
    assert data["omitted"] == cut


def test_a_composition_that_cut_nothing_carries_no_omitted_field():
    _open_session()
    emit.on_context_composed(SESSION, 1, blocks={"lessons": 400}, omitted={})
    assert emit.flush()
    assert "omitted" not in _body()[-1]["data"]


# --- model-call steps -----------------------------------------------------


def test_a_step_is_one_model_call_and_numbering_restarts_each_turn():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.on_step_started(SESSION, 1) == 1
    assert emit.on_step_started(SESSION, 1) == 2
    emit.on_turn_completed(SESSION, 1)
    emit.on_turn_started(SESSION, 2, "user")
    assert emit.on_step_started(SESSION, 2) == 1


def test_a_step_records_how_long_the_model_call_took():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    step = emit.on_step_started(SESSION, 1)
    emit.on_step_completed(SESSION, 1, step, ms=1234)
    assert emit.flush()
    done = [e for e in _body() if e["type"] == "step/completed"][-1]
    assert done["data"] == {"turn": 1, "step": step, "ms": 1234}


def test_closing_a_step_that_never_opened_writes_nothing():
    _open_session()
    before = len(_body())
    emit.on_step_completed(SESSION, 1, 0, ms=5)
    assert emit.flush()
    assert len(_body()) == before


def test_a_tool_call_names_the_model_call_that_issued_it():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_step_started(SESSION, 1)
    emit.on_tool_called(SESSION, 1, name="a", call_id="c1")
    emit.on_step_started(SESSION, 1)
    emit.on_tool_called(SESSION, 1, name="b", call_id="c2")
    emit.on_tool_called(SESSION, 1, name="c", call_id="c3")
    assert emit.flush()
    calls = [e["data"] for e in _body() if e["type"] == "tool/called"]
    # step says WHICH model call; call_index orders the calls within the turn.
    assert [c["step"] for c in calls] == [1, 2, 2]
    assert [c["call_index"] for c in calls] == [1, 2, 3]


def test_two_tools_in_one_model_call_share_a_step_but_not_a_position():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_step_started(SESSION, 1)
    emit.on_tool_called(SESSION, 1, name="a", call_id="c1")
    emit.on_tool_called(SESSION, 1, name="b", call_id="c2")
    assert emit.flush()
    calls = [e["data"] for e in _body() if e["type"] == "tool/called"]
    assert {c["step"] for c in calls} == {1}
    assert [c["call_index"] for c in calls] == [1, 2]


def test_a_tool_call_with_no_announced_step_omits_it_rather_than_guessing():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="a", call_id="c1")
    assert emit.flush()
    data = [e["data"] for e in _body() if e["type"] == "tool/called"][-1]
    assert "step" not in data
    assert data["call_index"] == 1


def test_a_completion_reuses_both_of_its_calls_ordinals():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_step_started(SESSION, 1)
    emit.on_tool_called(SESSION, 1, name="a", call_id="c1")
    emit.on_tool_completed(SESSION, 1, call_id="c1", status="completed")
    assert emit.flush()
    done = [e["data"] for e in _body() if e["type"] == "tool/completed"][-1]
    assert done["step"] == 1
    assert done["call_index"] == 1


# --- a tool call settles exactly once --------------------------------------


def test_a_second_terminal_frame_for_the_same_call_writes_no_second_closer():
    # Both update parsers can emit a status-only terminal frame for one tool, so
    # two frames for the same call_id is ordinary. The first settles the call; the
    # second must add nothing, or one call ends up with two tool/completed entries.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_step_started(SESSION, 1)
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="c1")
    emit.on_tool_completed(SESSION, 1, call_id="c1", status="completed")
    emit.on_tool_completed(SESSION, 1, call_id="c1", status="completed")
    assert emit.flush()
    closers = [e for e in _body() if e["type"] == "tool/completed"]
    assert len(closers) == 1
    # The one that landed is the FIRST frame: it kept the remembered identity.
    assert closers[0]["data"]["name"] == "fs_read"


def test_a_terminal_frame_for_a_never_opened_call_still_gets_its_closer():
    # The absence of a tool/called is NOT the same as "already settled by us": a
    # result for a call whose opener was never recorded is a real close and must
    # be written, with empty name/server and no elapsed.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_completed(SESSION, 1, call_id="never-opened", status="completed")
    assert emit.flush()
    closers = [e for e in _body() if e["type"] == "tool/completed"]
    assert len(closers) == 1
    data = closers[0]["data"]
    assert data["call_id"] == "never-opened"
    assert data["name"] == ""
    assert data["server"] == ""
    assert "elapsed_ms" not in data


def test_a_frame_after_the_group_sweep_closed_the_call_writes_nothing():
    # close_open_tool_calls IS the closer at a tool-group boundary, so it marks the
    # calls it sweeps settled. A terminal frame arriving after the sweep is a
    # duplicate and must not double-write.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_step_started(SESSION, 1)
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="c1")
    assert emit.close_open_tool_calls(SESSION, 1, status="completed") == 1
    emit.on_tool_completed(SESSION, 1, call_id="c1", status="completed")
    assert emit.flush()
    closers = [e for e in _body() if e["type"] == "tool/completed"]
    assert len(closers) == 1
    # The sweep's closer, not the late frame's.
    assert closers[0]["data"]["result_bytes"] == 0


def test_settle_once_holds_for_a_terminal_frame_from_either_parser():
    # The guard is in on_tool_completed, the ONE consumer both parsers feed, so a
    # frame produced by _dispatch and a frame produced by the AcpClient parser
    # settle the same call identically: the first writes the closer, the second
    # (whichever parser it came from) writes nothing.
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.types import EVENT_TOOL_RESULT, JsonRpcMessage

    # Parser A: the shared _dispatch terminal-frame builder.
    parser_a = _tool_result_event(None)
    assert parser_a.kind == EVENT_TOOL_RESULT

    # Parser B: the AcpClient tool_call_update parser, same terminal frame shape.
    client = AcpClient()
    msg = JsonRpcMessage(
        params={
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-measured",
                "status": "completed",
            }
        }
    )
    parser_b = client._extract_tool_call_update(msg)
    assert parser_b is not None and parser_b.kind == EVENT_TOOL_RESULT

    def _record(event) -> None:
        emit.on_tool_completed(
            SESSION,
            1,
            call_id=event.tool_call_id,
            status=event.tool_status,
            result=event.tool_output,
            result_digest=event.tool_output_digest,
            result_bytes=event.tool_output_bytes,
        )

    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    _record(parser_a)
    _record(parser_b)
    assert emit.flush()
    closers = [e for e in _body() if e["type"] == "tool/completed"]
    assert len(closers) == 1, "the two parsers' frames wrote two closers for one call"


def test_a_settled_call_can_be_reopened_and_reclosed_in_a_later_turn():
    # Settled markers go with their turn's live record, which the turn's own terminal
    # releases once it lands, so the same call_id reused in a later turn settles again
    # rather than being dropped as a stale duplicate.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_completed(SESSION, 1, call_id="reused", status="completed")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.flush()
    assert emit.live_turn(SESSION) == 0, "the landed terminal did not release its turn"
    emit.on_turn_started(SESSION, 2, "user")
    emit.on_tool_completed(SESSION, 2, call_id="reused", status="completed")
    assert emit.flush()
    closers = [e["data"] for e in _body() if e["type"] == "tool/completed"]
    assert [c["turn"] for c in closers] == [1, 2]


# --- a step may cover consecutive tool-only calls ---------------------------


def test_the_step_boundary_is_derived_and_may_cover_consecutive_tool_only_calls():
    # The ACP stream exposes only the tool-group-to-text transition, so a turn that
    # calls tools, is called again with their results and calls MORE tools -- text
    # only at the end -- shows one transition and the two model calls collapse into
    # one step. The format documents this as a lower bound rather than pretending
    # the step count is a model-call count; every tool still orders by call_index.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    step = emit.on_step_started(SESSION, 1)
    assert step == 1
    emit.on_tool_called(SESSION, 1, name="read", call_id="c1")
    emit.on_tool_called(SESSION, 1, name="edit", call_id="c2")
    # A second tool-only model call with no intervening text produces no new
    # step/started: the runner only opens one at a text transition.
    emit.on_tool_called(SESSION, 1, name="read", call_id="c3")
    assert emit.flush()
    steps = [e for e in _body() if e["type"] == "step/started"]
    assert len(steps) == 1, "a documented limitation: consecutive tool-only calls share a step"
    calls = [e["data"] for e in _body() if e["type"] == "tool/called"]
    assert [c["step"] for c in calls] == [1, 1, 1]
    assert [c["call_index"] for c in calls] == [1, 2, 3]


# --- tool payload accounting ---------------------------------------------


def test_the_error_flag_is_recorded_as_given_not_derived_from_status():
    # The stream has no error boolean: a refusal is the presence of a refusal
    # object, so the site that can see that computes it and this records it.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_completed(SESSION, 1, call_id="c1", status="refused", is_error=True)
    emit.on_tool_completed(SESSION, 1, call_id="c2", status="completed", is_error=False)
    assert emit.flush()
    done = [e["data"] for e in _body() if e["type"] == "tool/completed"]
    assert done[0]["is_error"] is True
    assert done[1]["is_error"] is False


# --- the queue ------------------------------------------------------------


def test_a_queued_message_records_its_size_and_place_but_no_turn():
    # It belongs to no turn yet: stamping the RUNNING turn's ordinal would
    # attribute one person's message to another's turn.
    _open_session()
    emit.on_message_queued(SESSION, source="slack", size_bytes=42, queued_seq="q-7")
    assert emit.flush()
    entry = _body()[-1]
    assert entry["type"] == "message/queued"
    assert entry["data"] == {"source": "slack", "bytes": 42, "queued_seq": "q-7"}
    assert "turn" not in entry["data"]


def test_a_queued_message_does_not_record_its_body_twice():
    # The body arrives with message/received when the queue drains.
    _open_session()
    emit.on_message_queued(SESSION, source="dashboard", size_bytes=9, queued_seq="q-1")
    assert emit.flush()
    assert "text" not in _body()[-1]["data"]


def test_a_nested_prompt_turn_keeps_the_author_of_the_turn_that_carried_it():
    """A replacing expansion re-enters the runner, and the actor travels with it.

    An ``@prompt`` mention re-dispatches the turn with the expanded body. The
    actor resolver's fallback is ``user``, so a re-entry that forwards the
    directive flags but drops the actor records a person for a turn an app
    authored -- the same false statement the top-level dispatch was fixed to stop
    making, one frame deeper.
    """
    import ast

    from kiro_crew.dashboard import chat_runner

    tree = ast.parse(
        "\n".join(
            turn_file.read_text(encoding="utf-8")
            for turn_file in [
                *sorted(Path(chat_runner.__file__).with_name("chat_turn").glob("*.py")),
                Path(chat_runner.__file__),
            ]
            if turn_file.name != "__init__.py"
        )
    )
    reentries = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "_run_chat"
        and any(kw.arg == "_prompt_depth" for kw in node.keywords)
    ]
    assert reentries, "the expansion re-entry moved -- this pin no longer sees it"
    for call in reentries:
        assert any(kw.arg == "_turn_actor" for kw in call.keywords), (
            "the expansion re-entry drops the actor, so a nested app turn is "
            "recorded as a person who never typed anything"
        )


# --- provenance ------------------------------------------------------------


def test_a_typed_automation_banner_cannot_attribute_a_turn_to_automation():
    """The actor is structural or it is ``user``.

    A turn's actor is a claim a reader takes as fact, and the banner these
    injections wrap their text in is a string the user can type into the composer.
    So the runner reads the enqueue-time ``kind`` tag -- stamped by the producer,
    absent from anything a user writes -- and never the message.
    """
    from kiro_crew.dashboard.chat_runner import _actor_for_queue_items
    from kiro_crew.dashboard.chat_utils import (
        CRON_NOTIFICATION_KIND,
        SUBAGENT_COMPLETION_KIND,
    )
    from kiro_crew.dashboard.state import (
        CRON_NOTIFY_PREFIX,
        SUBAGENT_COMPLETION_PREFIXES,
    )

    typed = [
        {"content": f'{CRON_NOTIFY_PREFIX}"payroll"]\nrun it', "kind": ""},
        {"content": f"{SUBAGENT_COMPLETION_PREFIXES[0]} done", "kind": ""},
    ]
    for item in typed:
        assert _actor_for_queue_items([item]) == ""

    assert _actor_for_queue_items([{"content": "x", "kind": CRON_NOTIFICATION_KIND}]) == "cron"
    assert (
        _actor_for_queue_items([{"content": "x", "kind": SUBAGENT_COMPLETION_KIND}]) == "subagent"
    )


def test_a_recovery_is_a_second_turn_of_the_same_actor():
    """A stall or watchdog re-entry does not change who caused the turn.

    The recovery carries a fresh queue id and a kind that names no producer, so
    without the stamp its actor would fall back to the default and record an
    autonudge's retry as a user turn.
    """
    from kiro_crew.dashboard.chat_runner import TURN_ACTOR_META_KEY, _actor_for_queue_items
    from kiro_crew.dashboard.chat_utils import SYNTHETIC_RECOVERY_KIND

    recovery = [
        {
            "content": "replayed verbatim",
            "kind": SYNTHETIC_RECOVERY_KIND,
            "meta": {TURN_ACTOR_META_KEY: "autonudge"},
        }
    ]
    assert _actor_for_queue_items(recovery) == "autonudge"


def test_a_stamped_actor_outside_the_vocabulary_is_ignored():
    # The stamp is gateway-authored, but a value the emitter does not recognise
    # would be recorded as `other`, which says less than the honest default.
    from kiro_crew.dashboard.chat_runner import TURN_ACTOR_META_KEY, _actor_for_queue_items
    from kiro_crew.dashboard.chat_utils import SYNTHETIC_RECOVERY_KIND

    bogus = [{"content": "x", "kind": SYNTHETIC_RECOVERY_KIND, "meta": {TURN_ACTOR_META_KEY: "??"}}]
    assert _actor_for_queue_items(bogus) == ""


def test_a_queue_kind_beats_a_stamped_actor():
    # A cron notification that also carries a stamp is still a cron turn: the kind
    # is set by the producer minting the entry, the stamp by whoever retried one.
    from kiro_crew.dashboard.chat_runner import TURN_ACTOR_META_KEY, _actor_for_queue_items
    from kiro_crew.dashboard.chat_utils import CRON_NOTIFICATION_KIND

    both = [
        {
            "content": "x",
            "kind": CRON_NOTIFICATION_KIND,
            "meta": {TURN_ACTOR_META_KEY: "subagent"},
        }
    ]
    assert _actor_for_queue_items(both) == "cron"


# --- attempts at one turn ordinal -----------------------------------------


def test_rerunning_one_ordinal_increments_the_attempt():
    # A regenerate or a rewind reruns a turn the ordinal already names. Without
    # the discriminator the two starts are identical lines and a fold cannot tell
    # a retry from a duplicate write.
    _open_session()
    for _ in range(3):
        emit.on_turn_started(SESSION, 7, "user")
    assert emit.flush()
    starts = [e["data"] for e in _body() if e["type"] == "turn/started"]
    assert [d.get("attempt", 1) for d in starts] == [1, 2, 3]


def test_the_first_attempt_carries_no_field():
    # The common case is a turn that was never rerun; it should not pay a field.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()
    assert "attempt" not in [e for e in _body() if e["type"] == "turn/started"][-1]["data"]


def test_attempts_are_counted_per_ordinal_not_per_session():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_turn_started(SESSION, 2, "user")
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()
    starts = [
        (e["data"]["turn"], e["data"].get("attempt", 1))
        for e in _body()
        if e["type"] == "turn/started"
    ]
    assert starts == [(1, 1), (2, 1), (1, 2)]


def test_a_restart_between_two_retries_still_increments():
    """The map cannot survive the restart that makes the collision possible.

    So it is rebuilt from the file on resume. Without that, a rerun after a
    restart writes attempt 1 at an ordinal the file already has an attempt 1 for
    -- exactly the collision the field exists to prevent.
    """
    _open_session()
    emit.on_turn_started(SESSION, 4, "user")
    emit.on_turn_started(SESSION, 4, "user")
    assert emit.flush()
    # A new gateway process: caches gone, the file is all that is left.
    emit.reset_caches()
    emit.on_session_opened(SESSION, agent="kirocrew", resumed=True)
    assert emit.flush()
    emit.on_turn_started(SESSION, 4, "user")
    assert emit.flush()
    attempts = [e["data"].get("attempt", 1) for e in _body() if e["type"] == "turn/started"]
    assert attempts == [1, 2, 3], f"a restart reset the attempt count: {attempts}"


def test_an_explicit_attempt_overrides_the_count():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user", attempt=9)
    assert emit.flush()
    assert [e for e in _body() if e["type"] == "turn/started"][-1]["data"]["attempt"] == 9


# --- a closer states only what the site observed --------------------------


def test_a_turn_end_closer_does_not_call_an_unfinalised_tool_completed():
    """At turn end nothing observed the tool's outcome.

    The stream may have raised, the process may have died, or the call may simply
    never have been finalised. `completed` there states a success no site saw, in a
    file nothing rewrites, so the default is `unknown` -- the same word
    `repair_interrupted_turn` writes for an unmatched `tool/called`, which is the
    identical claim reached from the file instead of from memory.

    `is_error` stays absent rather than being taken from the turn's own failure: a
    turn that raised says so in its own terminal, and stamping the TOOL as errored
    would be a second unobserved claim.

    Mutation guard: restoring `completed` as the default reddens this.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_write", call_id="c1")
    assert emit.close_open_tool_calls(SESSION, 1) == 1
    emit.on_turn_failed(SESSION, 1, error="RuntimeError")
    assert emit.flush()

    done = [e["data"] for e in _body() if e["type"] == "tool/completed"]
    assert len(done) == 1
    assert done[0]["status"] == "unknown", "an unobserved outcome was recorded as success"
    assert "is_error" not in done[0], "the tool was not observed to fail either"


# --- an outputless tool still gets its closer -----------------------------


def test_a_tool_that_produced_no_output_is_still_closed():
    """A tool with no output sends no result frame at all.

    So `tool_final` never arrives and the completion path never runs. Its
    `tool/called` would stay open for the life of the file, and a fold counting
    open calls would report a turn that never finished using a tool it had in
    fact finished with.

    `completed` is the caller's claim, not the default: the tool-group boundary
    passes it because the model went on to produce text, which means the tools it
    was waiting on finished.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_step_started(SESSION, 1)
    emit.on_tool_called(SESSION, 1, name="fs_write", call_id="c1")
    assert emit.close_open_tool_calls(SESSION, 1, status="completed") == 1
    assert emit.flush()
    done = [e["data"] for e in _body() if e["type"] == "tool/completed"]
    assert len(done) == 1
    assert done[0]["call_id"] == "c1"
    assert done[0]["status"] == "completed"
    # Zero bytes, not an absent field: the tool genuinely produced none, which is
    # a different claim from "the payload was not recorded".
    assert done[0]["result_bytes"] == 0
    assert "result_hash" not in done[0]
    assert done[0]["call_index"] == 1
    assert done[0]["step"] == 1


def test_closing_open_calls_leaves_an_already_completed_one_alone():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="a", call_id="c1")
    emit.on_tool_called(SESSION, 1, name="b", call_id="c2")
    emit.on_tool_completed(SESSION, 1, call_id="c1", status="completed", result="out")
    assert emit.close_open_tool_calls(SESSION, 1) == 1
    assert emit.flush()
    done = [e["data"] for e in _body() if e["type"] == "tool/completed"]
    assert [d["call_id"] for d in done] == ["c1", "c2"]
    assert done[0]["result_bytes"] == 3
    assert done[1]["result_bytes"] == 0


def test_closing_open_calls_is_a_no_op_when_none_are_open():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    before = len(_body())
    assert emit.close_open_tool_calls(SESSION, 1) == 0
    assert emit.flush()
    assert len(_body()) == before


def test_a_turn_never_completes_with_an_open_call_inside_it():
    # The shape the interrupted-turn repair exists to fix. A LIVE turn must not
    # produce it in the first place.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="a", call_id="c1")
    emit.close_open_tool_calls(SESSION, 1)
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.flush()
    kinds = [e["type"] for e in _body()]
    assert kinds.index("tool/completed") < kinds.index("turn/completed")


# --- every entry matches the shape its type documents --------------------


def test_every_emitted_type_matches_the_documented_shape():
    """The spec's data-shape table and the writer must not drift.

    One test over every family this module emits, rather than one assertion per
    field scattered across the file: a row that stops matching the writer is how a
    reader ends up trusting a field that is not there.
    """
    required = {
        "session/opened": {"agent", "slot", "model", "cwd", "owner", "resumed"},
        "session/closed": {"reason"},
        "turn/started": {"turn", "actor", "depth"},
        "turn/refused": {"turn", "actor", "reason", "depth"},
        "turn/completed": {"turn", "depth", "stop_reason", "duration_ms", "credits", "tokens"},
        "step/started": {"turn", "step"},
        "step/completed": {"turn", "step", "ms"},
        "tool/called": {"turn", "call_id", "name", "server", "kind"},
        "tool/completed": {"turn", "call_id", "name", "server", "status"},
        "message/received": {"turn", "role", "text", "source"},
        "message/sent": {"turn"},
        "message/chunk": {"turn", "delta"},
        "message/queued": {"source", "bytes", "queued_seq"},
        "request/configured": {"turn", "model", "provider", "context_window"},
        "context/composed": {"turn", "sources", "chars", "tokens", "tokens_estimated"},
        "model/selected": {"model", "source"},
        "compaction/applied": {"pct_before", "pct_after", "freed_pct"},
        "approval/requested": {"turn", "approval_id", "tool"},
        "approval/decided": {"turn", "approval_id", "decision"},
        "plan/updated": {"turn", "items"},
        "background/completed": {"kind"},
        "subagent/spawned": {"turn", "agent_id"},
        "subagent/steered": {"agent_id"},
        "subagent/completed": {"agent_id"},
        "subagent/failed": {"agent_id"},
    }
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    step = emit.on_step_started(SESSION, 1)
    emit.on_request_configured(SESSION, 1, model="m", provider="acp", context_window=9)
    emit.on_context_composed(SESSION, 1, step=step, blocks={"memory": 40})
    emit.on_message_received(SESSION, 1, text="hi", source="dashboard")
    emit.on_message_sent(SESSION, 1, step=step, text="there")
    emit.on_tool_called(SESSION, 1, name="t", call_id="c1", args="{}")
    emit.on_tool_completed(SESSION, 1, call_id="c1", status="completed", result="out")
    emit.on_step_completed(SESSION, 1, step, ms=5)
    emit.on_model_selected(SESSION, "m", "fallback", turn=1)
    emit.on_compaction_applied(SESSION, pct_before=0.8, pct_after=0.4)
    emit.on_approval_requested(SESSION, 1, approval_id="r1", tool="shell", reason="ls")
    emit.on_approval_decided(SESSION, 1, approval_id="r1", decision="approved")
    emit.on_plan_updated(SESSION, 1, items=[{"id": "a", "text": "t", "completed": False}])
    emit.on_subagent_spawned(SESSION, 1, agent_id="ab12", agent="kirocrew", scope={"memory": True})
    emit.on_subagent_steered(SESSION, agent_id="ab12", mode="interrupt")
    emit.on_subagent_completed(SESSION, agent_id="ab12", duration_ms=7)
    emit.on_subagent_failed(SESSION, agent_id="cd34", reason="boom", outcome="failed")
    emit.on_background_completed(SESSION, kind="title", model="m", credits=0.1)
    # A MEASURED close: credits and tokens are written only when the provider
    # reported them, so the shape table's row is exercised with a usage to report.
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn", credits=0.2, input_tokens=5)
    emit.on_message_queued(SESSION, source="slack", size_bytes=3, queued_seq="q1")
    emit.on_turn_refused(SESSION, 2, "not_authorized")
    emit.on_session_closed(SESSION, "reset")
    emit.on_message_sent(SESSION, 1, step=step, text="o" * (lg.MAX_ENTRY_BYTES + 200))
    assert emit.flush()
    seen: dict[str, int] = {}
    for entry in _body():
        kind = entry["type"]
        seen[kind] = seen.get(kind, 0) + 1
        assert kind in required, f"{kind} is emitted but has no documented shape here"
        missing = required[kind] - set(entry["data"])
        assert not missing, f"{kind} is missing documented field(s): {sorted(missing)}"
    unexercised = sorted(set(required) - set(seen))
    assert not unexercised, f"documented but not exercised: {unexercised}"
