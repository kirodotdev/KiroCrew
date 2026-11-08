"""Tests for cron dashboard chat threading (inject_cron_result_to_dashboard)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.cron import CronJob
from kiro_crew.dashboard.cron_inject import (
    _MIN_PROMPT_CHARS_TO_REFERENCE,
    _PROMPT_LOOKBACK_ROWS,
    _REFERENCE_MARKER,
    _UNCHANGED_PROMPT_BODY,
    _parse_prompt_row,
    _prompt_row_body,
    inject_cron_result_to_dashboard,
    run_marker,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.session_surface import set_dashboard_surfaced


def _redacted(text: str) -> str:
    """The stored form of a prompt: the same two redactors the writer applies.

    Mirrors ``inject_cron_result_to_dashboard`` in ORDER as well as membership --
    the credential pass runs over the URL pass's output there, and a test that
    reversed them could assert a byte equality production never produces.
    """
    safe, _ = redact_exfiltration_urls(text)
    safe, _ = redact_credentials(safe)
    return safe


#: An instruction long enough for the repeat suppression to apply at all. Below
#: ``_MIN_PROMPT_CHARS_TO_REFERENCE`` a reference costs more bytes than it saves,
#: so a short message is stored verbatim every run BY DESIGN -- which means a test
#: that drives the suppression with a one-line message asserts the verbatim branch
#: while claiming to test the other, and cannot fail. Sized off the constant so it
#: stays long enough if the threshold is ever raised.
_LONG_INSTRUCTION = (
    "Check the on-call dashboard for new pages.\n"
    "For each page, summarize the alarm, the service, and whether it self-recovered.\n"
    "Skip anything already acknowledged.\n"
) * (1 + _MIN_PROMPT_CHARS_TO_REFERENCE // 160)
assert len(_LONG_INSTRUCTION) >= _MIN_PROMPT_CHARS_TO_REFERENCE


@pytest.fixture(autouse=True)
def _reset_surface_registry():
    """inject_cron_result_to_dashboard publishes to the process-global
    dashboard-surface registry; reset it so keys from these mock states
    never leak into other tests."""
    set_dashboard_surfaced(())
    yield
    set_dashboard_surfaced(())


def _make_state(history_messages=None):
    """Create a mock DashboardState with conversation_log."""
    state = MagicMock()
    slots = {}
    # Real dict: inject_cron_result_to_dashboard publishes the surface registry
    # via _sync_dashboard_slots, which iterates state._slots.values().
    state._slots = slots

    def get_or_create_slot(name=None, agent="", origin=""):
        # ``origin`` is recorded, not just tolerated: the cron paths must
        # declare SlotOrigin.CRON, and a fake that swallowed the kwarg
        # would let that regress silently (a cron slot relabelled USER is
        # readable by any app holding `slots:user`).
        if name not in slots:
            slot = MagicMock()
            slot.key = name
            slot._origin = origin
            slot.linked_session_key = ""
            slot.messages = []
            slot.title = ""

            def append(role, content, cls, broadcast=True, meta=None, mint_mid=True):
                # Mirror the real ``_ChatSlot.append`` contract: preserve a
                # supplied ``meta.mid`` and mint only when the caller allows it.
                # Disk replay passes ``mint_mid=False`` so a legacy row cannot
                # advertise an identity absent from its durable copy.
                supplied = meta.get("mid") if isinstance(meta, dict) else None
                stored_meta = dict(meta) if isinstance(meta, dict) else {}
                if mint_mid and not supplied:
                    stored_meta["mid"] = f"m-test-{len(slot.messages)}"
                msg = {
                    "role": role,
                    "content": content,
                    "cls": cls,
                    **({"meta": stored_meta} if stored_meta else {}),
                }
                slot.messages.append(msg)
                return msg

            slot.append = append
            slots[name] = slot
        return slots[name]

    state.get_or_create_slot = get_or_create_slot
    state.conversation_log = MagicMock()
    state.conversation_log.read_messages.return_value = history_messages or []
    state.push_slots_update = MagicMock()
    return state


def _make_job(
    job_id="abc123",
    name="test-cron",
    last_result="Hello world",
    message="do the thing",
    last_result_ts=0.0,
    timezone="UTC",
):
    # ``message``, ``last_result_ts``, ``last_result_stamp`` and ``timezone``
    # are real values, not Mock attributes: the injector writes the run's prompt
    # as a paired ``user`` row and reads the rendered stamp, so a MagicMock here
    # would reach the redactors as a non-string.
    #
    # Nothing else about the job decides how the prompt row is written. Whether
    # the instruction is stored or referenced is read from the TRANSCRIPT, so
    # these tests set that up through ``history_messages`` rather than through a
    # flag on the fake -- which is what makes them able to fail.
    job = MagicMock()
    job.id = job_id
    job.name = name
    job.last_result = last_result
    job.message = message
    job.last_result_ts = last_result_ts
    job.timezone = timezone
    # Rendered by the PRODUCTION renderer rather than hand-written, so the fake
    # cannot drift from the spelling the executor actually persists.
    job.last_result_stamp = CronJob._render_run_stamp(job, last_result_ts)
    job.agent_id = ""
    return job


def _inject(state, job, result_text, **kw):
    """The injection, with the transcript read its async callers now prefetch.

    ``history`` is a required parameter in production so that no async caller can
    leave the whole-transcript parse on the event loop. These tests
    drive the function synchronously, where a blocking read is the caller's own
    cost, so the read the injection performs in production lives here instead.
    """
    kw.setdefault(
        "history",
        state.conversation_log.read_messages(f"cron:{job.id}") if state.conversation_log else [],
    )
    # Production's async caller prefetches the transcript's dismissed set
    # off-loop and passes it in; the fallback binds only when it was READABLE.
    # These synchronous tests mirror that with a readable (empty) set so the
    # bind/hydrate/deliver path runs (an absent value would defer the bind).
    kw.setdefault("dismissed", [])
    inject_cron_result_to_dashboard(state, job, result_text, **kw)


def run_marker_of(content: str) -> str:
    """The ``<!-- cron-run:... -->`` marker inside a row, or ``""``."""
    start = content.find("<!-- cron-run:")
    if start == -1:
        return ""
    end = content.find("-->", start)
    return content[start : end + 3] if end != -1 else ""


class TestInjectCronResultToDashboard:
    def test_history_is_required_so_the_read_cannot_land_on_the_loop(self):
        """Omitting the prefetch is a TypeError at the call, not a production stall.

        Without ``history=`` this synchronous function would parse the whole
        transcript on the event loop, so the parameter has no default.
        """
        with pytest.raises(TypeError, match="history"):
            inject_cron_result_to_dashboard(_make_state(), _make_job(), "result")

    def test_slot_is_tagged_cron_not_user(self):
        """A cron result is the job's output, not something the person typed.

        An untagged slot would be labelled USER by
        get_or_create_slot's default, which puts private cron content inside the
        ``slots:user`` WS scope, so any app holding that scope would receive it.
        """
        from kiro_crew.dashboard.state import SlotOrigin

        state = _make_state()
        job = _make_job()
        _inject(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert slot._origin == SlotOrigin.CRON
        assert slot._origin != SlotOrigin.USER

    def test_sets_linked_session_key(self):
        state = _make_state()
        job = _make_job()
        _inject(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert slot.linked_session_key == f"cron:{job.id}"

    def test_sets_title_from_job_name(self):
        state = _make_state()
        job = _make_job(name="daily-standup")
        _inject(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert "daily-standup" in slot.title

    def test_hydrates_history_on_first_link(self):
        history = [
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "msg2"},
        ]
        state = _make_state(history_messages=history)
        job = _make_job()
        _inject(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        # History (2) + the run's prompt/result pair (2) = 4 messages
        assert len(slot.messages) == 4
        assert slot.messages[0]["content"] == "msg1"
        assert slot.messages[1]["content"] == "msg2"

    def test_hydrates_max_50_messages(self):
        history = [{"role": "assistant", "content": f"msg{i}"} for i in range(100)]
        state = _make_state(history_messages=history)
        job = _make_job()
        _inject(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        # 50 from history + the run's prompt/result pair (2) = 52
        assert len(slot.messages) == 52

    def test_does_not_rehydrate_on_second_call(self):
        history = [{"role": "assistant", "content": "old"}]
        state = _make_state(history_messages=history)
        job = _make_job()
        _inject(state, job, "result1")
        _inject(state, job, "result2")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        # history(1) + run1 pair(2) + run2's result(1) = 4 (no re-hydration).
        # Both runs carry the same unstamped prompt row, so the second run's
        # prompt dedups against the first while its differing result is kept.
        assert len(slot.messages) == 4

    def test_dedup_prevents_duplicate_result(self):
        """One run injected twice stays one pair.

        This keeps ``/to-chat`` idempotent against the executor's auto-inject. The
        second call must not read the first call's prompt row as its precedent: it
        would emit a REFERENCE, and two differing rows dedup to neither.
        """
        state = _make_state()
        job = _make_job(last_result_ts=1_756_000_000.0)
        _inject(state, job, "same result")
        _inject(state, job, "same result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert len(slot.messages) == 2
        assert [m["role"] for m in slot.messages] == ["user", "assistant"]

    def test_two_runs_with_identical_text_stay_two_rows(self):
        """Different runs must not collapse into one undated row.

        A daily job whose output repeats would dedup the second day away, so
        the tab showed one row for N runs and a follow-up turn could not tell
        which run it was answering. The per-run stamp keeps them distinct.
        """
        state = _make_state()
        monday = _make_job(last_result_ts=1_756_000_000.0)
        tuesday = _make_job(last_result_ts=1_756_086_400.0)
        _inject(state, monday, "same result")
        _inject(state, tuesday, "same result")
        slot = state.get_or_create_slot(name=f"cron-{monday.id}")
        results = [m["content"] for m in slot.messages if m["role"] == "assistant"]
        assert len(results) == 2, "two runs collapsed into one row"
        assert results[0] != results[1]
        assert "2025-08-24" in results[0] and "2025-08-25" in results[1]

    def test_two_runs_in_the_same_minute_stay_two_rows(self):
        """Stamp resolution bounds which distinct runs can collapse.

        The stamp is part of the compared content, so a minute stamp would merge
        two same-minute runs with identical output (a fast script cron).
        """
        state = _make_state()
        first = _make_job(last_result_ts=1_756_000_000.0)
        second = _make_job(last_result_ts=1_756_000_020.0)
        _inject(state, first, "same result")
        _inject(state, second, "same result")
        slot = state.get_or_create_slot(name=f"cron-{first.id}")
        results = [m["content"] for m in slot.messages if m["role"] == "assistant"]
        assert len(results) == 2, "same-minute runs collapsed into one row"
        assert results[0] != results[1]

    def test_two_runs_in_the_same_second_stay_two_rows(self):
        """Identity must not inherit the DISPLAY stamp's resolution.

        The invisible run marker carries the timestamp at full precision, so two
        runs in one second stay apart even though the shown stamp matches.
        """
        state = _make_state()
        first = _make_job(last_result_ts=1_756_000_000.100000)
        second = _make_job(last_result_ts=1_756_000_000.900000)
        _inject(state, first, "same result")
        _inject(state, second, "same result")
        slot = state.get_or_create_slot(name=f"cron-{first.id}")
        results = [m["content"] for m in slot.messages if m["role"] == "assistant"]
        assert len(results) == 2, "same-second runs collapsed into one row"
        assert results[0] != results[1]
        # The DISPLAYED stamp is identical for both -- the marker is what
        # separates them, which is the whole point of splitting the two.
        assert first.last_result_stamp == second.last_result_stamp

    def test_the_run_marker_does_not_render_and_survives_a_reload(self):
        """The marker is identity, not content the person is meant to read.

        An HTML comment so the tab shows only the stamp, and fixed 6dp so a job
        reloaded from the JSON store renders the byte-identical marker the
        executor wrote -- otherwise a reloaded job would re-append every row.
        """
        job = _make_job(last_result_ts=1_756_000_000.5)
        marker = run_marker(job)
        assert marker.strip().startswith("<!--") and marker.strip().endswith("-->")
        assert f"{job.id}:1756000000.500000" in marker
        # Survives the store round-trip the same way _job_from_record restores it.
        reloaded = _make_job(last_result_ts=float(f"{1_756_000_000.5:.6f}"))
        assert run_marker(reloaded) == marker

    def test_a_legacy_unstamped_run_carries_no_marker(self):
        """A row already on disk must keep dedup-ing against its own spelling.

        A store written before this shipped has no timestamp, so adding a marker
        to its rows would make every one of them look new and re-append the lot.
        """
        job = _make_job(last_result_ts=0.0)
        assert run_marker(job) == ""
        state = _make_state()
        _inject(state, job, "legacy output")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        results = [m["content"] for m in slot.messages if m["role"] == "assistant"]
        assert "<!-- cron-run:" not in results[0]

    def test_editing_the_timezone_after_a_run_does_not_respell_its_row(self):
        """A re-render under edited config would duplicate a row already on disk.

        ``append_if_absent`` dedups by ``(role, content)`` and the stamp is in the
        content, so the stamp is rendered once, when the result is recorded.
        """
        state = _make_state()
        job = _make_job(last_result_ts=1_756_000_000.0)
        _inject(state, job, "the result")
        # The user moves the job to another zone AFTER the run.
        job.timezone = "Asia/Tokyo"
        _inject(state, job, "the result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        results = [m["content"] for m in slot.messages if m["role"] == "assistant"]
        assert len(results) == 1, "a timezone edit duplicated an existing run row"

    def test_re_surfacing_does_not_write_a_prompt_row(self):
        """Only the run that produced a result knows the prompt behind it.

        ``/to-chat`` re-surfaces a STORED result, and ``job.message`` may have been
        edited since, so pairing them would cite an instruction that never ran.
        """
        state = _make_state()
        job = _make_job(message="the message as edited later")
        _inject(state, job, "output from an earlier run", include_prompt=False)
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert [m["role"] for m in slot.messages] == ["assistant"]
        assert "the message as edited later" not in slot.messages[0]["content"]

    def test_an_unchanged_prompt_is_referenced_not_repeated(self):
        """Run two of a persistent cron must not store the instruction again.

        Each row carries a per-run marker, so nothing dedups the copies; storing
        them would spend the log's rotation window and the replay budget.
        """
        state = _make_state(
            history_messages=[
                {"role": "user", "content": f"# Cron Run: test-cron\n\n{_LONG_INSTRUCTION}"},
                {"role": "assistant", "content": "# Cron Job Result: test-cron\n\nrun one"},
            ]
        )
        job = _make_job(
            message=_LONG_INSTRUCTION,
            last_result_ts=1_756_000_000.0,
        )
        _inject(state, job, "run two")

        # The last two rows are this run's pair; anything before them is the
        # hydrated transcript.
        prompt_row = state._slots["cron-abc123"].messages[-2]
        assert prompt_row["role"] == "user"
        assert _LONG_INSTRUCTION not in prompt_row["content"]
        assert _UNCHANGED_PROMPT_BODY in prompt_row["content"]

    def test_a_referenced_run_still_carries_its_own_boundary(self):
        """The placeholder row is still a run boundary.

        Only its body changes: the stamp and the marker still separate it from
        the next run.
        """
        state = _make_state(
            history_messages=[
                {"role": "user", "content": f"# Cron Run: test-cron\n\n{_LONG_INSTRUCTION}"},
            ]
        )
        job = _make_job(message=_LONG_INSTRUCTION, last_result_ts=1_756_000_000.0)
        _inject(state, job, "run two")

        prompt_row = state._slots["cron-abc123"].messages[-2]["content"]
        assert _UNCHANGED_PROMPT_BODY in prompt_row, "this run must be a REFERENCED one"
        assert job.last_result_stamp in prompt_row
        assert f"<!-- cron-run:abc123:{1_756_000_000.0:.6f} -->" in prompt_row
        # Reference-ness is structural: the row carries the invisible marker, and
        # the parser reads it back as a reference (not as an instruction whose
        # body happens to be the placeholder prose).
        assert _REFERENCE_MARKER in prompt_row
        assert _parse_prompt_row(prompt_row) == (True, _UNCHANGED_PROMPT_BODY)

    def test_a_message_that_is_literally_the_placeholder_is_a_real_instruction(self):
        """A run whose message equals the pointer prose must not be skipped.

        A ``message`` can literally BE :data:`_UNCHANGED_PROMPT_BODY`. For A, then
        the placeholder text, then A, skipping the middle row by its body would
        make run three reference run one. Reference-ness is keyed off the marker.
        """
        # A is long so suppression applies; the placeholder run is below the
        # threshold, so it is stored verbatim and plants the collision.
        placeholder = _UNCHANGED_PROMPT_BODY
        assert len(placeholder) < _MIN_PROMPT_CHARS_TO_REFERENCE
        row_a = (
            "# Cron Run: test-cron\n\n<!-- cron-run:abc123:1.000000 -->" f"\n\n{_LONG_INSTRUCTION}"
        )
        row_placeholder = (
            "# Cron Run: test-cron\n\n<!-- cron-run:abc123:2.000000 -->" f"\n\n{placeholder}"
        )
        # No reference marker, so the parser reads it as a verbatim instruction.
        assert _parse_prompt_row(row_placeholder) == (False, placeholder)
        state = _make_state(
            history_messages=[
                {"role": "user", "content": row_a},
                {"role": "assistant", "content": "# Cron Job Result: test-cron\n\nrun one"},
                {"role": "user", "content": row_placeholder},
                {"role": "assistant", "content": "# Cron Job Result: test-cron\n\nrun two"},
            ]
        )

        job = _make_job(message=_LONG_INSTRUCTION, last_result_ts=1_756_000_000.0)
        _inject(state, job, "run three")

        prompt_row = state._slots["cron-abc123"].messages[-2]["content"]
        assert prompt_row.endswith(_LONG_INSTRUCTION), (
            "the run above was given the placeholder text, not A, so A must be "
            "stored verbatim rather than referenced past it"
        )
        assert _UNCHANGED_PROMPT_BODY not in _parse_prompt_row(prompt_row)[1]

    def test_an_edited_prompt_is_written_verbatim_again(self):
        """A message edited on a live job puts the NEW text in the transcript."""
        old = _LONG_INSTRUCTION
        new = _LONG_INSTRUCTION.replace("Skip anything already acknowledged.", "Page me instead.")
        state = _make_state(
            history_messages=[
                {"role": "user", "content": f"# Cron Run: test-cron\n\n{old}"},
            ]
        )
        # A real timestamp gives the run a marker; at ts 0 this would take the
        # no-identity short-circuit instead of the compare.
        job = _make_job(message=new, last_result_ts=1_756_000_000.0)
        _inject(state, job, "a result")

        prompt_row = state._slots["cron-abc123"].messages[-2]["content"]
        assert prompt_row.endswith(new)
        assert _UNCHANGED_PROMPT_BODY not in prompt_row

    def test_a_reverted_instruction_is_not_referenced_against_the_older_copy(self):
        """A → B → A must store A again, not point at the run above.

        The placeholder means "same as the previous run", so only the LATEST
        instruction-bearing row may be compared.
        """
        first = _LONG_INSTRUCTION
        second = _LONG_INSTRUCTION.replace("Skip anything", "Escalate anything")
        state = _make_state(
            history_messages=[
                {"role": "user", "content": f"# Cron Run: test-cron\n\n{first}"},
                {"role": "assistant", "content": "# Cron Job Result: test-cron\n\nrun one"},
                {"role": "user", "content": f"# Cron Run: test-cron\n\n{second}"},
                {"role": "assistant", "content": "# Cron Job Result: test-cron\n\nrun two"},
            ]
        )
        job = _make_job(message=first, last_result_ts=1_756_000_000.0)
        _inject(state, job, "run three")

        prompt_row = state._slots["cron-abc123"].messages[-2]["content"]
        assert prompt_row.endswith(first), "the reverted text must be stored again"
        assert _UNCHANGED_PROMPT_BODY not in prompt_row

    def test_a_short_instruction_is_never_referenced(self):
        """Below the threshold a reference costs more than the text it replaces.

        A ~100-char pointer would make a one-line message's row BIGGER.
        """
        short = "check pipeline health"
        assert len(short) < _MIN_PROMPT_CHARS_TO_REFERENCE
        state = _make_state(
            history_messages=[
                {"role": "user", "content": f"# Cron Run: test-cron\n\n{short}"},
            ]
        )
        job = _make_job(message=short, last_result_ts=1_756_000_000.0)
        _inject(state, job, "a result")

        prompt_row = state._slots["cron-abc123"].messages[-2]["content"]
        assert prompt_row.endswith(short)
        assert _UNCHANGED_PROMPT_BODY not in prompt_row

    def test_a_copy_beyond_the_look_back_is_written_again(self):
        """A reference may not reach further than the reader can.

        The slot keeps 10,000 rows but the durable transcript only ~200 lines, so
        a copy already rotated off disk must not satisfy the scan.
        """

        def _row(ts: float, body: str, *, reference: bool = False) -> dict[str, str]:
            # Each row needs its OWN marker: identical rows collapse in the slot,
            # which would leave the copy in reach. Reference rows carry
            # _REFERENCE_MARKER, as the writer emits it.
            marker = f"\n\n<!-- cron-run:abc123:{ts:.6f} -->"
            if reference:
                marker = f"{marker}{_REFERENCE_MARKER}"
            return {"role": "user", "content": f"# Cron Run: test-cron{marker}\n\n{body}"}

        history = [_row(1.0, _LONG_INSTRUCTION)]
        # Enough intervening rows to push that copy out of reach, all of them
        # references so nothing nearer can be mistaken for the antecedent.
        for i in range(_PROMPT_LOOKBACK_ROWS):
            history.append(_row(2.0 + i, _UNCHANGED_PROMPT_BODY, reference=True))
        state = _make_state(history_messages=history)
        job = _make_job(message=_LONG_INSTRUCTION, last_result_ts=1_756_000_000.0)
        _inject(state, job, "a result")

        prompt_row = state._slots["cron-abc123"].messages[-2]["content"]
        assert prompt_row.endswith(_LONG_INSTRUCTION)
        assert _UNCHANGED_PROMPT_BODY not in prompt_row

    def test_an_instruction_starting_with_a_blank_line_still_round_trips(self):
        """Reading a body back must not eat newlines the instruction owns.

        The row is ``header\\n\\nbody``, so a greedy strip eats a leading blank line
        and the suppression silently never fires.

        The stored row needs a marker: only the marker branch re-finds the
        separator by hand, so an unmarked row stays green however greedy the strip.
        """
        message = f"\n\n{_LONG_INSTRUCTION}"
        stored_marker = "\n\n<!-- cron-run:abc123:1.000000 -->"
        state = _make_state(
            history_messages=[
                {
                    "role": "user",
                    "content": f"# Cron Run: test-cron{stored_marker}\n\n{message}",
                },
            ]
        )
        job = _make_job(message=message, last_result_ts=1_756_000_000.0)
        _inject(state, job, "a result")

        prompt_row = state._slots["cron-abc123"].messages[-2]["content"]
        assert _UNCHANGED_PROMPT_BODY in prompt_row
        assert _LONG_INSTRUCTION not in prompt_row

    def test_an_instruction_that_quotes_the_marker_is_read_whole(self):
        """A prompt is untrusted text and may spell the marker itself.

        The marker counts only in its own block right after the header; a
        row-wide search would cut the body at a marker the instruction quotes.
        """
        quoting = f"Explain what <!-- cron-run:x:1 --> means.\n{_LONG_INSTRUCTION}"
        marked = f"# Cron Run: test-cron{run_marker(_make_job(last_result_ts=1.0))}\n\n{quoting}"

        assert _prompt_row_body(f"# Cron Run: test-cron\n\n{quoting}") == quoting
        assert _prompt_row_body(marked) == quoting

    def test_only_a_user_row_counts_as_a_previous_run(self):
        """Evidence of what a run was given is a ``user`` row and nothing else.

        An assistant reply in a cron tab can quote the instruction verbatim,
        headers and all, without any run having written it.
        """
        marker = "\n\n<!-- cron-run:abc123:1.000000 -->"
        state = _make_state(
            history_messages=[
                {
                    "role": "assistant",
                    "content": f"# Cron Run: test-cron{marker}\n\n{_LONG_INSTRUCTION}",
                },
            ]
        )
        job = _make_job(message=_LONG_INSTRUCTION, last_result_ts=1_756_000_000.0)
        _inject(state, job, "a result")

        prompt_row = state._slots["cron-abc123"].messages[-2]["content"]
        assert prompt_row.endswith(_LONG_INSTRUCTION)
        assert _UNCHANGED_PROMPT_BODY not in prompt_row

    def test_the_prompt_is_rewritten_when_no_copy_survives(self):
        """The placeholder points ABOVE itself, so a copy must exist up there.

        The log rotates (10MB / ~200 lines), so the last verbatim row is
        eventually evicted. Re-writing the instruction when none survives keeps
        the reference resolvable instead of dangling.
        """
        state = _make_state(history_messages=[])
        job = _make_job(message=_LONG_INSTRUCTION, last_result_ts=1_756_000_000.0)
        _inject(state, job, "a result")

        prompt_row = state._slots["cron-abc123"].messages[-2]["content"]
        assert prompt_row.endswith(_LONG_INSTRUCTION)
        assert _UNCHANGED_PROMPT_BODY not in prompt_row

    def test_a_result_row_is_not_mistaken_for_a_prompt_copy(self):
        """``# Cron Job Result:`` must not satisfy the ``# Cron Run:`` scan.

        The result row's body is the instruction ITSELF, else the equality check
        would hide a ``_PROMPT_ROW_PREFIX`` widened to ``"# Cron"``. The parser is
        asserted directly because the end-to-end path also rejects the
        ``assistant`` role.
        """
        assert (
            _prompt_row_body(f"# Cron Job Result: test-cron\n\n{_LONG_INSTRUCTION}") is None
        ), "a result row records no instruction, however similar its header"

        state = _make_state(
            history_messages=[
                {
                    "role": "assistant",
                    "content": f"# Cron Job Result: test-cron\n\n{_LONG_INSTRUCTION}",
                },
            ]
        )
        job = _make_job(message=_LONG_INSTRUCTION, last_result_ts=1_756_000_000.0)
        _inject(state, job, "run two")

        prompt_row = state._slots["cron-abc123"].messages[-2]["content"]
        assert prompt_row.endswith(_LONG_INSTRUCTION)

    def test_three_runs_of_one_persistent_cron_read_as_three_runs(self):
        """End-to-end: the tab a user opens after three runs.

        Three distinguishable runs, the instruction stored ONCE. Driven through a
        real ``CronJob`` rather than the fake.
        """
        # Suppression applies only above ``_MIN_PROMPT_CHARS_TO_REFERENCE``, so a
        # one-liner would test the verbatim branch instead.
        message = _LONG_INSTRUCTION
        result = "No new pages since the last check."
        job = CronJob(id="abc123", name="oncall", message=message, timezone="UTC")

        state = _make_state()
        for i, ts in enumerate((1_756_000_000.0, 1_756_086_400.0, 1_756_172_800.0)):
            job.set_run_result(result)
            # Pin the epoch so the stamps and markers are deterministic; the
            # renderer stays the production one.
            job.last_result_ts = ts
            job.last_result_stamp = job._render_run_stamp(ts)
            # As in production: prefetch_cron_history returns None once the slot
            # is linked, so a list here would hide a decision read off the parameter.
            _inject(state, job, result, history=[] if i == 0 else None)

        rows = state._slots["cron-abc123"].messages
        assert [r["role"] for r in rows] == [
            "user",
            "assistant",
            "user",
            "assistant",
            "user",
            "assistant",
        ], "three runs, each a prompt/result pair"

        prompts = [r["content"] for r in rows if r["role"] == "user"]
        # The instruction is stored ONCE across the three runs.
        assert sum(message in p for p in prompts) == 1
        assert prompts[0].endswith(message)
        assert all(_UNCHANGED_PROMPT_BODY in p for p in prompts[1:])

        # The pointer must RESOLVE: each reference needs a row holding the
        # instruction within the look-back.
        for i, row in enumerate(rows):
            if row["role"] != "user" or _UNCHANGED_PROMPT_BODY not in row["content"]:
                continue
            above = rows[max(0, i - _PROMPT_LOOKBACK_ROWS) : i]
            assert any(
                r["role"] == "user" and message in r["content"] for r in above
            ), "a reference with no antecedent in reach describes a text nobody can read"

        # Every row still carries its own run's marker.
        markers = [run_marker_of(r["content"]) for r in rows]
        assert len(set(markers)) == 3, "one distinct marker per run"
        assert all(markers), "no row may lose its identity marker"

    def test_a_repeat_is_suppressed_even_though_history_is_none(self):
        """The steady state passes ``history=None``, and must still suppress.

        ``prefetch_cron_history`` returns ``None`` once the slot is linked, so a
        decision read off the parameter would never suppress in production.
        """
        message = _LONG_INSTRUCTION
        job = CronJob(id="abc123", name="oncall", message=message, timezone="UTC")
        state = _make_state()

        job.set_run_result("run one")
        _inject(state, job, "run one", history=[])
        job.set_run_result("run two")
        # Pin run two past run one: on a coarse clock (Windows/CPython <= 3.12,
        # ~15.6 ms) back-to-back ``set_run_result`` calls can share a timestamp,
        # and the dedupe would then correctly drop run two as a repeat.
        job.last_result_ts += 1.0
        job.last_result_stamp = job._render_run_stamp(job.last_result_ts)
        _inject(state, job, "run two", history=None)

        prompts = [
            r["content"] for r in state._slots["cron-abc123"].messages if r["role"] == "user"
        ]
        assert len(prompts) == 2
        assert sum(message in p for p in prompts) == 1, "stored once, not once per run"
        assert _UNCHANGED_PROMPT_BODY in prompts[1]

    def test_a_placeholder_is_not_a_surviving_copy_of_the_prompt(self):
        """A reference must not resolve to another reference.

        The placeholder shares the ``# Cron Run:`` header, so once the verbatim
        row has rotated away it must not count as a surviving copy.

        ``last_result_ts`` is set because at ``0.0`` the run has no marker and is
        refused suppression before any row is compared.
        """
        state = _make_state(
            history_messages=[
                {
                    "role": "user",
                    "content": f"# Cron Run: test-cron\n\n{_UNCHANGED_PROMPT_BODY}",
                },
                {"role": "assistant", "content": "# Cron Job Result: test-cron\n\nrun nine"},
            ]
        )
        job = _make_job(message=_LONG_INSTRUCTION, last_result_ts=1_756_000_000.0)
        _inject(state, job, "run ten")

        prompt_row = state._slots["cron-abc123"].messages[-2]["content"]
        assert (
            _LONG_INSTRUCTION in prompt_row
        ), "a placeholder-only window must trigger a fresh verbatim copy"

    def test_a_run_that_wrote_no_row_cannot_spend_the_change(self):
        """A hidden run must not leave the next one referencing the OLD text.

        ``hide_in_chat`` and ``persistent_session=False`` runs skip this
        injection, so a "changed" flag on the job would be spent by a run that
        wrote no row. The decision reads the transcript instead.
        """
        old = _LONG_INSTRUCTION
        new = _LONG_INSTRUCTION.replace("Skip anything already acknowledged.", "Page me instead.")
        job = CronJob(id="abc123", name="oncall", message=old, timezone="UTC")
        state = _make_state()

        # Run one is visible: it stores the original instruction.
        job.set_run_result("run one")
        _inject(state, job, "run one", history=[])

        # The message is edited on the live job, and the next run is HIDDEN --
        # it completes, but writes no row.
        job.message = new
        job.set_run_result("run two")

        # Run three is visible again.
        job.set_run_result("run three")
        _inject(state, job, "run three", history=None)

        prompt_row = state._slots["cron-abc123"].messages[-2]["content"]
        assert new in prompt_row, "the edit must reach the transcript"
        assert _UNCHANGED_PROMPT_BODY not in prompt_row

    def test_a_shortened_instruction_is_not_matched_by_the_longer_stored_one(self):
        """The stored body must EQUAL this run's prompt, not merely contain it.

        Both variants clear ``_MIN_PROMPT_CHARS_TO_REFERENCE``; otherwise length
        alone would refuse suppression and the compare would never run.
        """
        stored = _LONG_INSTRUCTION + "Then page the secondary on-call.\n"
        assert _LONG_INSTRUCTION in stored, "the stored text must CONTAIN the shortened one"
        state = _make_state(
            history_messages=[
                {"role": "user", "content": f"# Cron Run: test-cron\n\n{stored}"},
            ]
        )
        job = _make_job(message=_LONG_INSTRUCTION, last_result_ts=1_756_000_000.0)
        _inject(state, job, "a result")

        prompt_row = state._slots["cron-abc123"].messages[-2]["content"]
        assert prompt_row.endswith(_LONG_INSTRUCTION)
        assert _UNCHANGED_PROMPT_BODY not in prompt_row

    def test_a_credential_only_edit_references_the_row_holding_identical_bytes(self):
        """A redaction collision cannot lose anything a reader could have seen.

        Instructions differing only inside a credential redact to the same text.
        Both sides of the compare are post-redaction, so the reference resolves
        to exactly the bytes a verbatim copy would have stored.
        """
        secret_a = "ghp_" + "A" * 36
        secret_b = "ghp_" + "B" * 36
        first = f"{_LONG_INSTRUCTION}Authenticate with {secret_a}\n"
        second = f"{_LONG_INSTRUCTION}Authenticate with {secret_b}\n"
        assert first != second, "the raw instructions must genuinely differ"
        assert _redacted(first) == _redacted(second), (
            "this test is only meaningful if the redactors actually collide; "
            "if the credential pattern stopped matching, fix the fixture"
        )
        assert secret_b not in _redacted(second), "the credential must be redacted"

        state = _make_state(
            history_messages=[
                {"role": "user", "content": f"# Cron Run: test-cron\n\n{_redacted(first)}"},
            ]
        )
        job = _make_job(message=second, last_result_ts=1_756_000_000.0)
        _inject(state, job, "a result")

        messages = state._slots["cron-abc123"].messages
        prompt_row = messages[-2]["content"]
        assert _UNCHANGED_PROMPT_BODY in prompt_row, "the repeat must be referenced"
        # The point of the test: what the reference resolves TO is exactly what
        # this run would have written, so the placeholder costs no information.
        antecedent = _prompt_row_body(messages[0]["content"])
        assert antecedent == _redacted(second)

    def test_the_pair_is_persisted_as_one_grouped_write(self):
        """Both rows reach the durable log in a single ordered call.

        Two separate off-loop dispatches could interleave or half-fail, so the
        replay a follow-up turn reads could carry a reversed or partial run.
        """
        state = _make_state()
        job = _make_job(message="do the thing", last_result_ts=1_756_000_000.0)
        with patch("kiro_crew.dashboard.cron_inject.append_rows_if_absent_off_loop") as durable:
            _inject(state, job, "the result")
        assert durable.call_count == 1, "the pair must be ONE grouped write"
        rows = durable.call_args.args[2]
        assert [r[0] for r in rows] == ["user", "assistant"], "prompt row first"
        assert "do the thing" in rows[0][1]
        assert "the result" in rows[1][1]
        # Each row carries the window's own mid so the bounded read's identity
        # walk can match it rather than re-appending the injection.
        assert all(r[3] for r in rows)

    def test_the_prompt_is_persisted_verbatim(self):
        """The replay must record the prompt the run was GIVEN.

        The executor sends ``job.message`` as written, and leading indentation
        matters in a fenced block or an indented snippet.
        """
        state = _make_state()
        message = "    indented line\n\n```\ncode\n```\n"
        job = _make_job(message=message, last_result_ts=1_756_000_000.0)
        _inject(state, job, "the result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        prompt_row = next(m["content"] for m in slot.messages if m["role"] == "user")
        assert message in prompt_row, "the prompt was altered on the way to the row"

    def test_a_whitespace_only_message_writes_no_prompt_row(self):
        """Verbatim storage must not turn an empty message into a blank boundary.

        The emptiness test still runs on the stripped value, so a job whose
        message is only whitespace has no prompt to record -- writing the row
        anyway would put a run header in the tab above nothing.
        """
        state = _make_state()
        job = _make_job(message="   \n\t\n ")
        _inject(state, job, "the result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert [m["role"] for m in slot.messages] == ["assistant"]

    def test_the_marker_precedes_the_untrusted_body(self):
        """An unclosed fence in the body must not render the marker as code.

        A prompt or result is untrusted text; everything after an unclosed ```
        fence renders as code, so a marker placed at the end of the row would be
        printed verbatim in the tab instead of hidden. It goes ahead of the body.
        """
        state = _make_state()
        job = _make_job(message="```\nunclosed prompt fence", last_result_ts=1_756_000_000.0)
        _inject(state, job, "```\nunclosed result fence")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        for msg in slot.messages:
            body = msg["content"]
            assert "<!-- cron-run:" in body
            assert body.index("<!-- cron-run:") < body.index(
                "```"
            ), "the marker must sit before the fence that would swallow it"

    def test_prompt_row_is_written_beside_the_result(self):
        """A follow-up turn needs to see what the run was asked, not just its output.

        The executor streams the prompt straight to the provider and never
        persists it, so the replay a follow-up turn opens with carried results
        alone -- nothing said which instruction produced any of them.
        """
        state = _make_state()
        job = _make_job(message="summarize yesterday")
        _inject(state, job, "the result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert [m["role"] for m in slot.messages] == ["user", "assistant"]
        assert "summarize yesterday" in slot.messages[0]["content"]

    def test_empty_result_creates_slot_without_message(self):
        """A boundary row must never appear with nothing behind it.

        ``/to-chat`` on a job that has never produced a result reaches the
        injection with an empty ``result_text``; writing the prompt there would
        put a run header in the tab for a run that never happened. No result
        also means no run boundary, so neither durable write fires.
        """
        state = _make_state()
        job = _make_job()
        with patch("kiro_crew.dashboard.cron_inject.append_rows_if_absent_off_loop") as durable:
            _inject(state, job, "")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert slot.linked_session_key == f"cron:{job.id}"
        assert slot.messages == []
        durable.assert_not_called()
        state.conversation_log.append_if_absent.assert_not_called()
        state.conversation_log.append.assert_not_called()

    def test_pushes_slots_update(self):
        state = _make_state()
        job = _make_job()
        _inject(state, job, "result")
        state.push_slots_update.assert_called_once_with()
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert [m["role"] for m in slot.messages] == ["user", "assistant"]

    def test_publishes_the_tab_to_the_surface_registry(self):
        """The cron tab must be surfaced the moment it is created.

        Sub-agent routing, completion injection and widget delivery all read
        has_dashboard_surface; an unpublished slot would hide the first run's
        sub-agents."""
        from kiro_crew.dashboard.chat_utils import dashboard_slot_key
        from kiro_crew.session_surface import (
            has_dashboard_surface,
            set_dashboard_surfaced,
        )

        set_dashboard_surfaced(())
        try:
            state = _make_state()
            job = _make_job(job_id="188f71e5")
            _inject(state, job, "result")
            assert has_dashboard_surface("cron:188f71e5") is True
            assert dashboard_slot_key("cron:188f71e5") == "cron-188f71e5"
        finally:
            set_dashboard_surfaced(())


class TestPersistsResultToConversationLog:
    """the result must be written to the canonical ConversationLog
    under the linked key cron:{id} so a dashboard follow-up turn
    (chat_runner.build_session_replay) has it as context."""

    def test_appends_result_to_conversation_log_under_linked_key(self):
        state = _make_state()
        job = _make_job(job_id="job1", name="my-cron")
        _inject(state, job, "the result")
        # The atomic append_if_absent runs the dup check under the session lock.
        # Two rows per run: the prompt, then the result.
        assert state.conversation_log.append_if_absent.call_count == 2
        prompt_args, _ = state.conversation_log.append_if_absent.call_args_list[0]
        assert prompt_args[0] == "cron:job1"
        assert prompt_args[1] == "user"
        assert prompt_args[2].startswith("# Cron Run: my-cron")
        assert "do the thing" in prompt_args[2]
        args, kwargs = state.conversation_log.append_if_absent.call_args
        assert args[0] == "cron:job1"
        assert args[1] == "assistant"
        assert "the result" in args[2]
        assert args[2].startswith("# Cron Job Result: my-cron")
        # No unlocked append(). read_messages still runs once to hydrate the
        # fresh slot; that is not the persistence probe.
        state.conversation_log.append.assert_not_called()

    def test_delegates_log_dedup_to_append_if_absent(self):
        # The log dup check runs atomically inside append_if_absent, under the
        # per-session lock; the inject path must not persist on its own.
        # (Skip-on-duplicate is covered by
        # test_history_locking_remediation_locked_section::TestAppendIfAbsent.)
        state = _make_state()
        job = _make_job(job_id="job2", name="my-cron")
        _inject(state, job, "the result")
        assert state.conversation_log.append_if_absent.call_count == 2
        state.conversation_log.append.assert_not_called()
        # The window still shows the result the log write was delegated for.
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert slot.messages[-1]["content"].startswith("# Cron Job Result: my-cron")

    def test_durable_copy_carries_the_window_rows_id(self):
        # The durable transcript copy must ride with the SAME ``meta.mid`` the
        # window copy was minted; a re-minted or absent id leaves a bounded
        # slot-detail read unable to reconcile the two copies as one message.
        state = _make_state()
        job = _make_job(job_id="job5", name="my-cron")
        _inject(state, job, "the result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        window_mid = slot.messages[-1]["meta"]["mid"]
        kwargs = state.conversation_log.append_if_absent.call_args.kwargs
        assert kwargs["mid"] == window_mid, "the durable copy did not carry the window row's id"

    def test_no_conversation_log_does_not_crash(self):
        state = _make_state()
        state.conversation_log = None
        job = _make_job(job_id="job4")
        # Must not raise when conversation_log is unavailable.
        _inject(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert len(slot.messages) == 2


class TestHydrateSlotFromHistory:
    """Tests for hydrate_slot_from_history (accepts pre-loaded messages)."""

    def test_hydrates_messages_into_slot(self):
        from kiro_crew.dashboard.cron_inject import hydrate_slot_from_history

        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        state = _make_state(history_messages=history)
        slot = state.get_or_create_slot(name="cron-abc")
        hydrate_slot_from_history(slot, history)
        assert len(slot.messages) == 2
        assert slot.messages[0]["content"] == "hello"
        assert slot.messages[1]["content"] == "world"

    def test_empty_history_produces_no_messages(self):
        from kiro_crew.dashboard.cron_inject import hydrate_slot_from_history

        state = _make_state(history_messages=[])
        slot = state.get_or_create_slot(name="cron-abc")
        hydrate_slot_from_history(slot, [])
        assert len(slot.messages) == 0

    def test_skips_messages_with_empty_content(self):
        from kiro_crew.dashboard.cron_inject import hydrate_slot_from_history

        history = [
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "real message"},
        ]
        state = _make_state(history_messages=history)
        slot = state.get_or_create_slot(name="cron-abc")
        hydrate_slot_from_history(slot, history)
        assert len(slot.messages) == 1
        assert slot.messages[0]["content"] == "real message"

    def test_assigns_user_role_class(self):
        from kiro_crew.dashboard.cron_inject import hydrate_slot_from_history

        history = [
            {"role": "user", "content": "user msg"},
            {"role": "assistant", "content": "assistant msg"},
        ]
        state = _make_state(history_messages=history)
        slot = state.get_or_create_slot(name="cron-abc")
        hydrate_slot_from_history(slot, history)
        assert slot.messages[0]["cls"] == "msg msg-u"
        assert slot.messages[1]["cls"] == "msg msg-a"

    def test_preserves_persisted_row_ids(self):
        # A hydrated row must keep the ``meta.mid`` its disk copy carries.
        # Minting fresh ids here leaves the window and the disk holding
        # disjoint id sets for the same rows while the durable injection
        # copies make the region read all-id — the identity walk then marks
        # every hydrated row owed and a bounded read serves the history twice.
        from kiro_crew.dashboard.cron_inject import hydrate_slot_from_history

        history = [
            {"role": "user", "content": "hello", "meta": {"mid": "m-disk-1"}},
            {"role": "assistant", "content": "world", "meta": {"mid": "m-disk-2"}},
            {"role": "assistant", "content": "pre-id row"},
        ]
        state = _make_state(history_messages=history)
        slot = state.get_or_create_slot(name="cron-abc")
        hydrate_slot_from_history(slot, history)
        assert [m["meta"]["mid"] for m in slot.messages[:2]] == [
            "m-disk-1",
            "m-disk-2",
        ], "hydration re-minted ids the disk rows already carry"
        assert not (slot.messages[2].get("meta") or {}).get(
            "mid"
        ), "hydration invented an in-memory-only id for a legacy disk row"


class TestHasSlot:
    """Tests for DashboardState.has_slot method."""

    def test_returns_true_when_slot_exists(self):
        from kiro_crew.dashboard.state import DashboardState

        state = MagicMock(spec=DashboardState)
        state._slots = {"cron-abc": MagicMock()}
        state.has_slot = DashboardState.has_slot.__get__(state)
        assert state.has_slot("cron-abc") is True

    def test_returns_false_when_slot_missing(self):
        from kiro_crew.dashboard.state import DashboardState

        state = MagicMock(spec=DashboardState)
        state._slots = {}
        state.has_slot = DashboardState.has_slot.__get__(state)
        assert state.has_slot("nonexistent") is False
