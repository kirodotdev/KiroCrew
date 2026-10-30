"""What the log keeps out and when it writes at all: redaction, digests and the flag.

It pins that the emitter redacts a body once, whole, so no credential survives an overflow
split and a redaction failure writes no text; that tool arguments and results are recorded
as digests and measurements; that with the flag off every family is silent, does no
payload work and allocates no state; and that a flag-off launch loads no storage and
registers no shutdown hook, which first use registers once. Its redaction section also pins
emit order, attempt seeding and closer placement, and its flag sections a few tool,
approval, compaction, model and close fields. The other turn entries live in
`test_crew_log_emit_entries.py`, the writer in `test_crew_log_emit_write_path.py`.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from crew_log_emit_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    SESSION,
    _body,
    _isolated_home,
    _log_path,
    _open_session,
    _store_root,
    _tool_result_event,
    _typed,
)

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import emit

# --- redaction ------------------------------------------------------------


def test_no_job_is_ever_submitted_without_a_session_key():
    # A job in the "" bucket is ordered against nothing: it can be drained before
    # or after the session-keyed lifecycle entries it belongs between, so a reader
    # folding the file in order sees a config change land inside the wrong turn.
    # The signature makes the key required; this proves no caller passes an empty
    # one on the enabled path.
    import inspect

    # Read the PARAMETER, not the source text: a type alias, a reflowed signature
    # or a renamed annotation would all break a string match without changing the
    # thing that matters, which is that the key has no default to fall back on.
    param = inspect.signature(emit._submit).parameters["session_id"]
    assert param.default is inspect.Parameter.empty, (
        "session_id must stay required -- a default silently reintroduces the " "unordered bucket"
    )
    assert param.kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ), f"session_id became {param.kind}, which lets a caller omit it"
    _open_session()
    seen: list[str] = []
    real = emit._submit

    def _spy(job, what, session_id, *a, **kw):
        seen.append(session_id)
        return real(job, what, session_id, *a, **kw)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_submit", _spy)
        # Every family this PR emits, driven through one turn.
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_request_configured(SESSION, 1, model="m", provider="p", context_window=1000)
        emit.on_context_composed(SESSION, 1, step=1, blocks={"steering": 40}, total_chars=40)
        emit.on_step_started(SESSION, 1)
        _typed(1, "hello")
        emit.on_message_sent(SESSION, 1, step=1, text="hi")
        emit.on_tool_called(SESSION, 1, name="fs_read", call_id="call-1", args='{"path":"/x"}')
        emit.on_tool_completed(SESSION, 1, name="fs_read", call_id="call-1", result="ok")
        emit.close_open_tool_calls(SESSION, 1)
        emit.on_step_completed(SESSION, 1, 1, ms=5)
        emit.on_message_queued(SESSION, source="slack", size_bytes=3, queued_seq="q-1")
        emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.flush()
    assert seen, "no job was submitted at all -- the spy never ran"
    assert "" not in seen, f"{seen.count('')} job(s) landed in the unordered bucket"


def test_a_rerun_immediately_after_a_resume_does_not_reuse_the_attempt():
    # The seed is a queued job; deriving the attempt on the caller's thread would
    # read the map while that seed is still in the queue, and the first rerun
    # after a restart would then write an attempt the file already holds.
    _open_session()
    emit.on_turn_started(SESSION, 7, "user")
    emit.on_turn_completed(SESSION, 7, stop_reason="end_turn")
    assert emit.flush()
    emit.reset_caches()

    # Re-attach and rerun ordinal 7 with NO flush in between: the rerun's job is
    # submitted while the seed is still queued behind it.
    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7", resumed=True)
    emit.on_turn_started(SESSION, 7, "user")
    assert emit.flush()
    starts = [e for e in _body() if e["type"] == "turn/started" and e["data"]["turn"] == 7]
    assert len(starts) == 2, f"expected two starts at ordinal 7, got {len(starts)}"
    assert "attempt" not in starts[0]["data"], "the first try must carry no field"
    assert (
        starts[1]["data"].get("attempt") == 2
    ), f"the rerun reused the attempt: {starts[1]['data']}"


def test_the_attempt_seed_keeps_readable_counts_before_a_backward_tail():
    _open_session()
    emit.on_turn_started(SESSION, 7, "user", attempt=2)
    emit.on_turn_completed(SESSION, 7, stop_reason="end_turn")
    assert emit.flush()
    emit.reset_caches()

    path = _log_path()
    started = path.read_bytes().splitlines(keepends=True)[2]
    with open(path, "ab") as damaged:
        damaged.write(started)

    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7")
    assert emit.flush()
    seen: list[int] = []
    real_append = lg.CrewLog.append

    def _record_the_attempt(self, entry_type, data, *args, **kwargs):
        if entry_type == "turn/started":
            seen.append(int(data.get("attempt", 1)))
        return real_append(self, entry_type, data, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "append", _record_the_attempt)
        emit.on_turn_started(SESSION, 7, "user")
        emit.flush()
    assert seen == [3], f"the seed lost the readable count before the backward tail: {seen}"


def test_the_turn_closer_is_the_last_entry_of_its_turn():
    # `turn/completed` is the one entry whose POSITION carries meaning. A reader
    # folding in order treats it as the turn's boundary, so a `message/sent`
    # after it belongs to a turn the file already closed.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    first = emit.on_step_started(SESSION, 1)
    emit.on_message_sent(SESSION, 1, step=first, text="first model call")
    second = emit.on_step_started(SESSION, 1)
    emit.on_message_sent(SESSION, 1, step=second, text="the turn's last text")
    emit.on_step_completed(SESSION, 1, second, ms=5)
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.flush()
    types = [e["type"] for e in _body() if e["type"].startswith(("turn/", "step/", "message/"))]
    assert types[-1] == "turn/completed", f"the closer is not last: {types}"
    assert types.index("turn/completed") > max(
        i for i, t in enumerate(types) if t == "message/sent"
    ), "an assistant message lands after the turn that produced it was closed"


def test_the_closer_never_reaches_another_turns_open_call():
    # A transient can leave a call open and the same session then runs another
    # turn. Closing every open call for the session would stamp THIS turn's
    # ordinal on the earlier turn's tool -- a tool attributed to a turn that never
    # used it, which is worse than the open call it was trying to tidy.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="call-a", args="{}")
    # Turn 1 ends without a result frame for call-a AND without its closer running.
    emit.on_turn_started(SESSION, 2, "user")
    emit.on_tool_called(SESSION, 2, name="fs_write", call_id="call-b", args="{}")
    closed = emit.close_open_tool_calls(SESSION, 2)
    assert emit.flush()
    assert closed == 1, f"turn 2's closer touched {closed} calls, expected only its own"
    done = [e for e in _body() if e["type"] == "tool/completed"]
    assert [e["data"]["call_id"] for e in done] == ["call-b"]
    assert done[0]["data"]["turn"] == 2
    # The other turn's call is still open, which is the interrupted-turn repair's
    # job -- it works from the file, not from this process's memory.
    assert not [e for e in done if e["data"]["call_id"] == "call-a"]


def test_an_oversize_typed_message_is_recorded_rather_than_refused():
    # A received message is a body a person typed. Written straight onto the entry,
    # one over the ceiling is refused at append time and the message that actually
    # reached the turn is missing from the log.
    _open_session()
    body = "s" * (lg.MAX_ENTRY_BYTES + 500)
    _typed(1, body, source="dashboard")
    assert emit.flush()
    received = [e for e in _body() if e["type"] == "message/received"]
    chunks = [e for e in _body() if e["type"] == "message/chunk"]
    assert received, "the message was refused and lost"
    assert received[-1]["data"]["source"] == "dashboard"
    assert received[-1]["data"]["chunks"] == [c["seq"] for c in chunks]
    assert "".join(c["data"]["delta"] for c in chunks) == body


def test_entries_for_one_session_land_in_the_order_they_were_emitted():
    # The ordering guarantee, asserted on the file rather than on the signature. A
    # job in the shared "" bucket is ordered against nothing, so it can drain
    # before or after the session-keyed entries it belongs between -- and a reader
    # folding the file in order would see a config change inside the wrong turn or
    # a tool result before the call that produced it.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_request_configured(SESSION, 1, model="m", provider="p", context_window=1000)
    step = emit.on_step_started(SESSION, 1)
    emit.on_message_sent(SESSION, 1, step=step, text="first")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="c1", args="{}")
    emit.on_tool_completed(SESSION, 1, name="fs_read", call_id="c1", result="ok")
    emit.on_message_sent(SESSION, 1, step=step, text="second")
    emit.on_step_completed(SESSION, 1, step, ms=1)
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.flush()
    got = [e["type"] for e in _body() if e["type"] != "session/opened"]
    expected = [
        "turn/started",
        "request/configured",
        "step/started",
        "message/sent",
        "tool/called",
        "tool/completed",
        "message/sent",
        "step/completed",
        "turn/completed",
    ]
    assert got == expected, f"entries were reordered:\n  got      {got}\n  expected {expected}"
    seqs = [e["seq"] for e in _body()]
    assert seqs == sorted(seqs), "seq is not monotonic in the file"


def test_an_entry_in_the_window_before_the_turn_is_published_is_not_blamed_on_turn_zero():
    # The slot's ordinal is assigned partway through the turn while the ACP client
    # is reachable earlier, so an entry written between the two cannot take its turn
    # from the slot -- it would read 0 or the previous turn's number. The segment
    # flush is the real caller: it holds a slot and asks the emitter for the turn.
    # The emitter's live record is opened at the turn's start, so it is already
    # right in that window.
    _open_session()
    emit.on_turn_started(SESSION, 4, "user")
    assert emit.live_turn(SESSION) == 4, "the live record is not readable at turn start"
    emit.on_message_sent(SESSION, emit.live_turn(SESSION), text="partial reply")
    assert emit.flush()
    sent = [e for e in _body() if e["type"] == "message/sent"]
    assert sent and sent[-1]["data"]["turn"] == 4

    # And with no turn running the answer is 0, which the caller must treat as
    # "record what you actually have" rather than stamping 0 on an entry.
    emit.on_turn_completed(SESSION, 4, stop_reason="end_turn")
    assert emit.flush()
    assert emit.live_turn(SESSION) == 0
    assert emit.live_turn("no-such-session") == 0


def test_the_attempt_seed_reads_the_file_only_when_memory_cannot_answer():
    # The scan is O(file) on the single writer thread, so seeding a session that
    # already holds its counts costs one full rescan per open against a file that
    # only grows -- and every other session's appends queue behind it.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()

    scans: list[str] = []
    real = emit._seed_attempts

    def _spy(session_id, log):
        scans.append(session_id)
        return real(session_id, log)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_seed_attempts", _spy)

        # A warm reuse holds its counts: no scan.
        emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7")
        assert emit.flush()
        assert scans == [], "a warm reuse rescanned the file"

        # A resume belongs to a process that is gone: scan.
        emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7", resumed=True)
        assert emit.flush()
        assert scans == [SESSION], "a resume did not seed from the file"

        # A process without the counts in memory cannot answer either: scan.
        emit.reset_caches()
        emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7")
        assert emit.flush()
        assert scans == [SESSION, SESSION], "an evicted map was not re-seeded"


def test_a_turns_closers_name_the_turn_that_started_it():
    # The runner's message-slice index is reset when a mid-turn clear empties the
    # message list. That index and the crew log's turn ordinal must not be the same
    # value: sharing one would move the ordinal to 0 mid-turn, and the turn's own
    # closers -- emitted at the end -- would then name a turn that never started,
    # filing the turn's cost under a phantom ordinal 0 while the real turn shows a
    # start and no completion.
    _open_session()
    emit.on_turn_started(SESSION, 3, "user")
    step = emit.on_step_started(SESSION, 3)
    emit.on_tool_called(SESSION, 3, name="fs_read", call_id="c9", args="{}")
    # Whatever the runner does to its own slice index, the crew log's ordinal is the
    # one the turn opened with.
    emit.close_open_tool_calls(SESSION, 3)
    emit.on_step_completed(SESSION, 3, step, ms=1)
    emit.on_turn_completed(SESSION, 3, stop_reason="end_turn")
    assert emit.flush()
    turns = {e["data"].get("turn") for e in _body() if e["type"] != "session/opened"}
    assert turns == {3}, f"entries were split across ordinals: {sorted(turns, key=str)}"
    assert [e["type"] for e in _body()][-1] == "turn/completed"
    # The live record is released under the ordinal it was created with, so no
    # finished turn is left pinned for a later entry to be blamed on.
    assert emit.live_turn(SESSION) == 0, "a completed turn is still the live turn"


def test_a_credential_cannot_survive_the_overflow_split_at_any_offset():
    # The split is safe for one reason only: the body is redacted ONCE, whole,
    # before it is sliced. So a credential is already gone when slicing happens,
    # and no slice boundary can cut it into two unmatched halves. This is exactly
    # what a per-delta streaming emitter could not offer, which is why there is
    # none: redacting each delta on its own leaves a credential split across two
    # of them intact in both, and the pieces concatenate.
    marker = "A" + "KIA" + "IOSFODNN7" + "EXAMPLE" + "0" * 24
    secret = "aws_secret" + "_access_key=" + marker
    # Walk the credential across a slice boundary one byte at a time.
    boundary = lg.MAX_ENTRY_BYTES - 4096
    for offset in range(0, len(secret) + 1, 7):
        emit.reset_caches()
        _open_session()
        filler_before = "f" * max(0, boundary - offset)
        emit.on_message_sent(
            SESSION,
            1,
            step=1,
            text=filler_before + secret + "t" * 8000,
        )
        assert emit.flush()
        blob = _log_path().read_text(encoding="utf-8")
        assert marker not in blob, f"the credential survived at offset {offset}"
        # And reassembling the cited chunks must not rebuild it either.
        chunks = [e for e in _body() if e["type"] == "message/chunk"]
        assert marker not in "".join(
            c["data"]["delta"] for c in chunks
        ), f"the credential reassembled from chunks at offset {offset}"


def test_a_body_is_redacted_by_the_emitter_not_trusted_from_the_call_site():
    # Some sites hand over already-clean text and some hand over raw input. A
    # rule enforced here cannot be forgotten by the next site that is added.
    _open_session()
    # Assembled at runtime, never written as one literal: a credential-shaped
    # string in a source file is what the content scan exists to stop, and a test
    # fixture is not an exception the scanner can see.
    marker = "A" + "KIA" + "IOSFODNN7" + "EXAMPLE" + "0" * 24
    secret = "aws_secret" + "_access_key=" + marker
    _typed(1, f"use {secret} please")
    emit.on_message_sent(SESSION, 1, text=f"ok, {secret}")
    assert emit.flush()
    blob = json.dumps(_body())
    assert marker not in blob


def test_a_redaction_failure_writes_no_text_rather_than_the_raw_text(monkeypatch):
    # Fail CLOSED: the module's promise is that data carries no secrets.
    def boom(_text):
        raise RuntimeError("redaction is broken")

    monkeypatch.setattr(emit, "redact_exfiltration_urls", boom)
    _open_session()
    _typed(1, "a very secret thing")
    assert emit.flush()
    entry = _body()[-1]
    assert entry["data"]["text"] == ""
    assert "very secret thing" not in json.dumps(entry)


def test_an_empty_body_writes_no_sent_entry_at_all():
    _open_session()
    before = len(_body())
    emit.on_message_sent(SESSION, 1, text="")
    assert emit.flush()
    assert len(_body()) == before


# --- the new families obey the module flag -------------------------------


def test_every_new_family_is_silent_with_the_flag_off(monkeypatch):
    _open_session()
    monkeypatch.setenv(emit.CREW_LOG_ENV, "0")
    before = len(_body())
    emit.on_message_received(SESSION, 1, text="x")
    emit.on_message_sent(SESSION, 1, text="y")
    emit.on_request_configured(SESSION, 1, model="m", provider="p")
    emit.on_context_composed(SESSION, 1, blocks={"memory": 10})
    emit.on_step_started(SESSION, 1)
    emit.on_step_completed(SESSION, 1, 1, ms=1)
    emit.on_message_queued(SESSION, source="s", size_bytes=1, queued_seq="q")
    assert len(_body()) == before


def test_a_disabled_emitter_does_no_payload_work_at_all(monkeypatch):
    """Off is FREE, not merely silent.

    Hashing a tool result is proportional to its size and redaction walks a whole
    body, both once per frame on the event loop. An emitter that did that work and
    then discarded it inside the writer would charge a user who switched the log
    off for work it then throws away.
    """
    _open_session()
    monkeypatch.setenv(emit.CREW_LOG_ENV, "0")
    worked: list[str] = []
    monkeypatch.setattr(
        emit, "_payload_digest", lambda payload: worked.append("digest") or ("", -1)
    )
    monkeypatch.setattr(emit, "_safe_text", lambda text: worked.append("redact") or "")

    emit.on_tool_called(SESSION, 1, name="t", call_id="c1", args="v" * 10000)
    emit.on_tool_completed(SESSION, 1, call_id="c1", status="completed", result="r" * 10000)
    emit.on_message_received(SESSION, 1, text="body")
    emit.on_message_sent(SESSION, 1, text="body")
    emit.on_request_configured(SESSION, 1, model="m", provider="p", system="s" * 10000)

    assert worked == [], f"a disabled emitter still did payload work: {worked}"


def test_the_same_work_does_happen_when_enabled(monkeypatch):
    # The mirror of the test above, so it cannot pass by the calls being wrong.
    _open_session()
    worked: list[str] = []
    real_digest = emit._payload_digest
    real_safe = emit._safe_text
    monkeypatch.setattr(
        emit, "_payload_digest", lambda payload: (worked.append("digest"), real_digest(payload))[1]
    )
    monkeypatch.setattr(
        emit, "_safe_text", lambda text: (worked.append("redact"), real_safe(text))[1]
    )
    emit.on_tool_called(SESSION, 1, name="t", call_id="c1", args="v")
    emit.on_message_received(SESSION, 1, text="body")
    assert emit.flush()
    assert "digest" in worked
    assert "redact" in worked


def test_an_mcp_tool_records_its_server():
    _open_session()
    emit.on_tool_called(SESSION, 1, name="InternalSearch", server="builder-mcp", call_id="tc-3")
    assert _body()[-1]["data"]["server"] == "builder-mcp"


def test_tool_completion_measures_elapsed_ms_from_its_call():
    _open_session()
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-2")
    emit.on_tool_completed(SESSION, 1, name="fs_read", status="completed", call_id="tc-2")
    assert _body()[-1]["data"]["elapsed_ms"] >= 0


def test_completion_inherits_the_identity_the_call_frame_carried():
    """The terminal ACP frame repeats neither the tool name nor its server."""
    _open_session()
    emit.on_tool_called(SESSION, 1, name="InternalSearch", server="builder-mcp", call_id="tc-4")
    emit.on_tool_completed(SESSION, 1, status="completed", call_id="tc-4")
    data = _body()[-1]["data"]
    assert data["name"] == "InternalSearch"
    assert data["server"] == "builder-mcp"


def test_an_explicit_completion_name_is_not_overwritten_by_the_remembered_one():
    _open_session()
    emit.on_tool_called(SESSION, 1, name="old", server="old-srv", call_id="tc-5")
    emit.on_tool_completed(
        SESSION, 1, name="new", server="new-srv", status="completed", call_id="tc-5"
    )
    data = _body()[-1]["data"]
    assert data["name"] == "new"
    assert data["server"] == "new-srv"


def test_tool_completion_without_a_matching_call_omits_elapsed_ms():
    _open_session()
    emit.on_tool_completed(SESSION, 1, name="fs_read", status="completed", call_id="unseen")
    assert "elapsed_ms" not in _body()[-1]["data"]


def test_approval_request_and_decision_share_the_approval_id():
    _open_session()
    emit.on_turn_started(SESSION, 2, "user")
    emit.on_approval_requested(SESSION, 2, approval_id="ap-1", tool="execute_bash")
    emit.on_approval_decided(SESSION, 2, approval_id="ap-1", decision="rejected_once")
    request, decision = _body()[-2:]
    assert request["data"]["approval_id"] == decision["data"]["approval_id"] == "ap-1"
    assert request["data"]["tool"] == "execute_bash"
    assert decision["data"]["decision"] == "rejected_once"


def test_compaction_records_percentages_not_token_counts():
    _open_session()
    emit.on_compaction_applied(SESSION, pct_before=0.92, pct_after=0.31)
    data = _body()[-1]["data"]
    assert data["pct_before"] == 0.92
    assert data["pct_after"] == 0.31
    assert data["freed_pct"] == pytest.approx(0.61)
    assert "before_tokens" not in data


def test_model_selection_records_its_source():
    _open_session()
    emit.on_model_selected(SESSION, "claude-haiku-4.5", "fallback")
    assert _body()[-1]["data"] == {
        "model": "claude-haiku-4.5",
        "source": "fallback",
    }


def test_close_records_the_gateway_reason_verbatim():
    _open_session()
    emit.on_session_closed(SESSION, "shutdown")
    assert _body()[-1]["data"]["reason"] == "shutdown"


# --- the flag ------------------------------------------------------------


def test_a_tool_call_records_a_digest_of_its_arguments_never_the_arguments():
    """A hash and a size answer real questions; the bytes would be a liability.

    "Did this call run the same command as the last one" and "how big was this
    payload" are both answerable from a digest. Keeping the payload itself would
    make the crew log a place shell arguments and file contents accumulate.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(
        SESSION, 1, name="execute_bash", call_id="tc-1", args='{"command": "ls /tmp"}'
    )
    assert emit.flush()

    entry = next(e for e in _body() if e["type"] == "tool/called")
    assert entry["data"]["args_hash"] == hashlib.sha256(b'{"command": "ls /tmp"}').hexdigest()
    assert entry["data"]["args_bytes"] == 22
    assert "ls /tmp" not in json.dumps(entry), "the arguments themselves reached the file"


def test_a_repeated_call_is_recognisable_by_its_digest():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="a", args='{"path": "x"}')
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="b", args='{"path": "x"}')
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="c", args='{"path": "y"}')
    assert emit.flush()

    hashes = [e["data"]["args_hash"] for e in _body() if e["type"] == "tool/called"]
    assert hashes[0] == hashes[1] != hashes[2]


def test_a_call_with_no_arguments_records_no_digest_fields():
    # -1 would be a size, and 0 is a real size. Absent is the honest answer for
    # "nothing was handed over to digest".
    _open_session()
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    assert emit.flush()

    data = next(e for e in _body() if e["type"] == "tool/called")["data"]
    assert "args_hash" not in data and "args_bytes" not in data


def test_a_tool_completion_records_the_error_flag_and_a_result_digest():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    emit.on_tool_completed(
        SESSION, 1, call_id="tc-1", status="refused", is_error=True, result="denied by policy"
    )
    assert emit.flush()

    data = next(e for e in _body() if e["type"] == "tool/completed")["data"]
    assert data["is_error"] is True
    assert data["result_hash"] == hashlib.sha256(b"denied by policy").hexdigest()
    assert data["result_bytes"] == 16


def _record_tool_result_event(event, *, call_id: str = "tc-measured") -> dict:
    emit.on_tool_completed(
        SESSION,
        1,
        call_id=call_id,
        status=event.tool_status,
        result=event.tool_output,
        result_digest=event.tool_output_digest,
        result_bytes=event.tool_output_bytes,
    )
    assert emit.flush()
    return [e["data"] for e in _body() if e["type"] == "tool/completed"][-1]


def test_a_long_result_records_the_full_redacted_output_measurement():
    from kiro_crew import session_directive

    full_output = "x" * session_directive.MAX_TOOL_RESULT_CHARS + "distinct suffix"
    event = _tool_result_event(full_output)
    displayed = full_output[: session_directive.MAX_TOOL_RESULT_CHARS]
    assert event.tool_output == displayed, "the display truncation changed"

    _open_session()
    data = _record_tool_result_event(event)

    assert data["result_bytes"] == len(
        full_output.encode("utf-8")
    ), "the crew log measured the displayed prefix instead of the full redacted output"
    assert data["result_hash"] == hashlib.sha256(full_output.encode("utf-8")).hexdigest()
    assert (
        data["result_hash"] != hashlib.sha256(displayed.encode("utf-8")).hexdigest()
    ), "the crew log hashed the displayed prefix instead of the full redacted output"


def test_equal_display_prefixes_record_different_full_result_hashes():
    from kiro_crew import session_directive

    prefix = "x" * session_directive.MAX_TOOL_RESULT_CHARS
    _open_session()
    first = _record_tool_result_event(_tool_result_event(prefix + "a"), call_id="tc-a")
    second = _record_tool_result_event(_tool_result_event(prefix + "b"), call_id="tc-b")

    assert (
        first["result_hash"] != second["result_hash"]
    ), "two full outputs with the same displayed prefix recorded the same hash"


def test_a_status_only_terminal_frame_records_no_result_measurement():
    event = _tool_result_event(None)
    assert event.tool_output_bytes == -1, "a status-only frame invented a byte count"
    assert event.tool_output_digest == "", "a status-only frame invented a digest"

    _open_session()
    data = _record_tool_result_event(event)

    assert "result_bytes" not in data, "a status-only terminal frame recorded a false byte count"
    assert "result_hash" not in data, "a status-only terminal frame recorded a false hash"


def test_chat_runner_forwards_the_full_result_measurement_to_the_emitter():
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
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "on_tool_completed"
    ]
    assert len(calls) == 1
    keywords = {kw.arg: ast.unparse(kw.value) for kw in calls[0].keywords if kw.arg}
    assert (
        keywords.get("result_digest") == "event.tool_output_digest"
    ), "chat_runner dropped the full result digest"
    assert (
        keywords.get("result_bytes") == "event.tool_output_bytes"
    ), "chat_runner dropped the full result byte count"


def test_an_unstated_error_flag_is_left_off_rather_than_recorded_as_success():
    # Tri-state on purpose: "nobody said" is not the same claim as "it worked".
    _open_session()
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    emit.on_tool_completed(SESSION, 1, call_id="tc-1", status="completed")
    assert emit.flush()

    data = next(e for e in _body() if e["type"] == "tool/completed")["data"]
    assert "is_error" not in data


def test_a_rerun_of_the_same_turn_ordinal_is_distinguishable_by_its_attempt():
    """Regenerate and rewind reuse a turn ordinal the log already named.

    Without a discriminator two starts at the same ordinal are indistinguishable,
    so a fold cannot tell a deliberate rerun from a duplicate write -- and those
    call for opposite handling.
    """
    _open_session()
    emit.on_turn_started(SESSION, 4, "user")
    emit.on_turn_completed(SESSION, 4, stop_reason="end_turn")
    emit.on_turn_started(SESSION, 4, "user", attempt=2)
    emit.on_turn_completed(SESSION, 4, stop_reason="end_turn")
    emit.on_turn_started(SESSION, 4, "user", attempt=3)
    assert emit.flush()

    starts = [e["data"] for e in _body() if e["type"] == "turn/started"]
    assert [d["turn"] for d in starts] == [4, 4, 4]
    assert "attempt" not in starts[0], "attempt 1 is every turn and must cost no field"
    assert starts[1]["attempt"] == 2
    assert starts[2]["attempt"] == 3


def test_a_turn_start_carries_the_message_it_answers_when_the_caller_knows_it():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user", message_seq=7)
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit.on_turn_started(SESSION, 2, "user")
    assert emit.flush()

    starts = [e["data"] for e in _body() if e["type"] == "turn/started"]
    assert starts[0]["message_seq"] == 7
    assert "message_seq" not in starts[1], "0 is not a seq and must not be recorded as one"


def test_flag_off_writes_no_file_at_all(monkeypatch):
    monkeypatch.setenv(emit.CREW_LOG_ENV, "0")
    assert emit.enabled() is False
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit.on_session_closed(SESSION, "reset")
    assert not _store_root().exists()


def test_flag_unset_records_by_default(monkeypatch):
    monkeypatch.delenv(emit.CREW_LOG_ENV, raising=False)
    assert emit.enabled() is True
    _open_session()
    assert _log_path().is_file()


def test_flag_off_allocates_no_state_either(monkeypatch):
    """Off means off: no file AND nothing held in memory.

    Writing is guarded by the entry points, but live-turn state is allocated
    before the write is handed over, so an unguarded allocation would accumulate a
    record per turn on every gateway running with the emitter off -- invisible,
    because no file appears to give it away. It shows the moment the flag comes
    on: a turn the flag-off calls had numbered would continue that numbering.
    """
    monkeypatch.setenv(emit.CREW_LOG_ENV, "0")
    assert emit.enabled() is False

    for turn in range(1, 6):
        emit.on_session_opened(SESSION, agent="kirocrew")
        emit.on_turn_started(SESSION, turn, "user")
        emit.on_tool_called(SESSION, turn, name="fs_read", call_id=f"tc-{turn}")

    assert emit.live_turn(SESSION) == 0, "live-turn state allocated with the flag off"
    assert emit._tracker_instance is None, "the turn tracker was built with the flag off"
    assert emit._open == {}
    assert emit.buffered_writes() == 0
    assert not _store_root().exists()

    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    _open_session()
    emit.on_tool_called(SESSION, 5, name="fs_read", call_id="tc-after")
    assert emit.flush()
    (call,) = [e["data"] for e in _body() if e["type"] == "tool/called"]
    assert call["call_index"] == 1, "a flag-off turn left its numbering behind"


def test_a_flag_turned_off_mid_turn_still_releases_what_it_allocated(monkeypatch):
    # The release paths stay unguarded for exactly this: state allocated while the
    # flag was on must not be stranded by turning it off.
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.live_turn(SESSION) == 1

    monkeypatch.setenv(emit.CREW_LOG_ENV, "0")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.live_turn(SESSION) == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        *(
            pytest.param(value, True, id=f"test_the_flag_is_on_when_empty_or_truthy-{value}")
            for value in ["1", "true", "TRUE", " yes ", "on", "", "  "]
        ),
        *(
            pytest.param(
                value,
                False,
                id=f"test_the_flag_is_off_for_a_falsy_or_unrecognised_spelling-{value}",
            )
            for value in ["0", "false", "FALSE", " no ", "off", "Off", "2", "disable", "fasle"]
        ),
    ],
)
def test_the_flag_follows_the_spelling(monkeypatch, value, expected):
    monkeypatch.setenv(emit.CREW_LOG_ENV, value)
    assert emit.enabled() is expected


def test_the_flag_is_read_per_call_not_at_import(monkeypatch):
    monkeypatch.setenv(emit.CREW_LOG_ENV, "0")
    _open_session()
    assert not _store_root().exists()
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    _open_session()
    assert _log_path().is_file()


# --- the gateway boot path -------------------------------------------------


_BOOT_PROBE = """
import atexit, importlib, json, os, sys

# The child runs the real boot path, so it must never see the operator's home: refuse
# to import anything unless the home the parent isolated is the one in force.
if os.path.expanduser("~") != os.environ["KC_PROBE_HOME"]:
    raise SystemExit("the probe's home is not isolated: " + os.path.expanduser("~"))

registered = []
_real = atexit.register


def _record(fn, *a, **k):
    registered.append(getattr(fn, "__name__", repr(fn)))
    return _real(fn, *a, **k)


atexit.register = _record
importlib.import_module("kiro_crew.dashboard.server")
emit = sys.modules.get("kiro_crew.crew_log.emit")
glue = {"kiro_crew.crew_log.emit", *getattr(emit, "_OWNERS", ())}
print(
    json.dumps(
        {
            "storage": sorted(
                k
                for k in sys.modules
                if k.startswith("kiro_crew.crew_log.")
                and k not in glue
            ),
            "glue": "kiro_crew.crew_log.emit" in sys.modules,
            "hooks": [n for n in registered if "drain_for_shutdown" in n],
        }
    )
)
"""


def _boot_probe(tmp_path: Path) -> dict:
    """Import a boot-path module in a CLEAN interpreter with the flag off.

    A clean process is the only place this is observable: the suite has already
    imported both the emitter and its storage package, so an in-process check
    would read this test file's own imports rather than the boot path's.
    """
    # The child's home, data home and workspace root are all this test's: the boot path
    # resolves ``Path.home()`` at import time, and a child left to find its own would
    # read the operator's real one.
    user_home = tmp_path / "user"
    user_home.mkdir()
    env = {
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": str(tmp_path),
        "HOME": str(user_home),
        "KC_PROBE_HOME": str(user_home),
        "KIROCREW_HOME": str(tmp_path / "home"),
        "KIROCREW_WORKSPACE": str(tmp_path / "workspace"),
        "KIROCREW_CREW_LOG": "0",
    }
    if sys.platform == "win32":  # pragma: no cover - parity with the lease suite
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        # ``Path.home()`` has no ``pwd`` fallback on Windows and reads ``USERPROFILE``.
        env["USERPROFILE"] = str(user_home)
    # ``cwd`` moves off the repo, so the interpreter path has to be absolute: an interpreter
    # invoked through a relative PATH entry reports a relative ``sys.executable``, which a
    # child started elsewhere cannot find. Absolutise without resolving symlinks, because a
    # venv interpreter links to the system one and following that link drops the venv's own
    # site-packages.
    interpreter = os.path.abspath(sys.executable)
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [interpreter, "-c", _BOOT_PROBE],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert done.returncode == 0, f"the boot probe did not run: {done.stderr[-2000:]}"
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_flag_off_launch_does_not_load_the_storage_subsystem(tmp_path: Path) -> None:
    """The boot path may reach the emitter's glue, never the storage package.

    AUTOSDE's ``no-new-work-on-gateway-boot-path`` rule asks for an optional
    subsystem's import to be gated rather than only its calls, so this asserts the
    split the module documents: the glue is importable for free, and the store,
    schema and lease stay unloaded until a call reaches storage.
    """
    seen = _boot_probe(tmp_path)
    assert seen["glue"], "the probe did not reach the emitter at all, so it proves nothing"
    assert seen["storage"] == [], f"a flag-off launch imported storage: {seen['storage']}"


def test_a_flag_off_launch_registers_no_shutdown_hook(tmp_path: Path) -> None:
    """Importing the emitter must not register the backstop drain.

    An ``atexit`` handler installed at import is work every launch pays for a
    subsystem it will never call, and it is the half of the boot-path cost that a
    module-load census cannot see.
    """
    seen = _boot_probe(tmp_path)
    assert seen["hooks"] == [], (
        "a flag-off launch registered the crew log drain at exit: " f"{seen['hooks']}"
    )


def test_the_shutdown_hook_is_registered_once_on_first_use(monkeypatch) -> None:
    """First use registers the backstop exactly once, however many passes follow."""
    registered: list[str] = []
    monkeypatch.setattr(
        emit.atexit, "register", lambda fn, *a, **k: registered.append(getattr(fn, "__name__", ""))
    )
    monkeypatch.setattr(emit, "_shutdown_hook_registered", False, raising=False)

    emit._ensure_shutdown_hook()
    emit._ensure_shutdown_hook()

    assert registered == ["drain_for_shutdown"], (
        "the backstop drain was not registered exactly once on first use: " f"{registered}"
    )
