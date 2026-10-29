"""SlackRenderer: the text between tool calls folds into the steps, and the
reply is the turn's final answer only.

The recording client and scripted provider are ``test_slack_renderer.py``'s.
"""

from __future__ import annotations

import asyncio

from test_slack_renderer import _Provider, _RecSlack

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    AcpEvent,
)
from kiro_crew.messaging import TurnDriver
from kiro_crew.slack.renderer import _STATUS_WORKING, SlackRenderer


class TestNarrationFoldsIntoSteps:
    """Interim narration is not reply text, on Slack as on the dashboard.

    The dashboard folds a turn's narration ("I'll look at the open issues...")
    into its steps and shows only the text after the last step as the reply;
    Slack does the same. Each case drives the REAL TurnDriver, because the run
    its stream redactor holds across a tool call is what would otherwise split
    a sentence around a step.
    """

    @staticmethod
    def _run(events):
        rec = _RecSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        asyncio.run(TurnDriver(_Provider(events), renderer, approval_mode="auto").run("x"))
        streamed = [kw["text"] for m, kw in rec.calls if m == "append_stream"]
        tasks = [kw for m, kw in rec.calls if m == "append_task"]
        return rec, streamed, tasks

    def test_a_tool_using_turn_shows_only_the_final_answer(self):
        rec, streamed, tasks = self._run(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="I'll look at the open issues. "),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Let me find the repo and check the"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text=" backlog."),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="a", title="gh issue list"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Now let me read _dict_set_create."),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="b", title="read config.py"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="There are 3 open issues; #12 is the bug."),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        assert streamed == ["There are 3 open issues; #12 is the bug."], rec.calls
        joined = "".join(streamed)
        for narration in ("I'll look", "backlog", "_dict_set_create", "Let me"):
            assert narration not in joined, joined
        # The reply is appended after every step, so no step sits inside it.
        last_task = max(i for i, (m, _) in enumerate(rec.calls) if m == "append_task")
        first_text = min(i for i, (m, _) in enumerate(rec.calls) if m == "append_stream")
        assert last_task < first_text, rec.calls
        details = [t["details"] for t in tasks if t["details"]]
        assert details == [
            "I'll look at the open issues. Let me find the repo and check the backlog.\n"
            "gh issue list",
            "Now let me read _dict_set_create.\nread config.py",
        ], tasks

    def test_a_run_held_across_the_call_stays_in_its_sentence(self):
        """The redactor holds "overlay." until the text after the call.

        Shown where it arrives, it would open the reply after the step:
        "...writes the local" / step / "overlay.". It is the end of the
        narration, so it completes the step's details, and those are written
        once, whole: no details ever show the sentence cut short.
        """
        rec, streamed, tasks = self._run(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Let me find where the CLI writes the local"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text=" overlay."),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="a", title="git grep overlay"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="The overlay is config.local.json."),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        assert streamed == ["The overlay is config.local.json."], rec.calls
        details = [t["details"] for t in tasks if t["details"]]
        assert details == [
            "Let me find where the CLI writes the local overlay.\ngit grep overlay"
        ], tasks

    def test_held_narration_before_two_calls_lands_on_the_first_step(self):
        rec, streamed, tasks = self._run(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Reading both files."),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="a", title="read a.py"),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="b", title="read b.py"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Both define it."),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        assert streamed == ["Both define it."], rec.calls
        with_details = [(t["title"], t["details"]) for t in tasks if t["details"]]
        assert with_details == [
            ("read b.py", "read b.py"),
            ("read a.py", "Reading both files.\nread a.py"),
        ], tasks
        # Redrawn as it was last drawn: complete, not set back to running.
        assert [t["status"] for t in tasks if t["title"] == "read a.py"][-1] == "complete"

    def test_a_turn_that_ends_on_a_tool_answers_with_its_last_text(self):
        # The dashboard's final message is then that text; an empty reply
        # would read as "No response".
        rec, streamed, _ = self._run(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Started the deploy; it runs for an hour."),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="a", title="deploy"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        assert streamed == ["Started the deploy; it runs for an hour."], rec.calls

    def test_narration_in_a_step_is_redacted(self):
        key = "AKIA" + "1234567890ABCDEX"
        _, streamed, tasks = self._run(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text=f"Using key {key} now. "),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="a", title="aws s3 ls"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Listed."),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        details = " ".join(t["details"] for t in tasks)
        assert key not in details, tasks
        assert "Using key" in details, tasks
        assert streamed == ["Listed."]

    def test_the_thread_status_still_moves_while_the_reply_is_held(self):
        # The native status ("is using X") is the progress signal while the
        # reply is held; holding the text must not drop it.
        rec, _, _ = self._run(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Checking."),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="a", title="grep"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Done."),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        statuses = [kw["status"] for m, kw in rec.calls if m == "set_thread_status"]
        assert statuses[0] == _STATUS_WORKING and "is using grep" in statuses, statuses
        assert statuses[-1] == "", statuses  # cleared at the end, as before
        # Writing again after the step: the status stops naming the tool.
        at = statuses.index("is using grep")
        assert _STATUS_WORKING in statuses[at + 1 : -1], statuses

    def test_narration_held_across_a_wait_stays_with_the_wait(self):
        # The wait ends that message's steps; the held run that arrives after
        # the next call still ends the wait's narration, so it never becomes
        # the next step's text or a truncated reply.
        rec, streamed, tasks = self._run(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="I will wait for CI to finish."),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="w", title="wait", tool_name="wait"),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="s", title="shell"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        assert streamed == ["I will wait for CI to finish."], rec.calls
        assert not [t for t in tasks if "finish" in t["details"]], tasks

    def test_narration_held_across_a_wait_survives_a_refused_restart(self):
        # The stream opens, the wait ends it, and Slack refuses the next one, so
        # the turn finishes on the chat.update fallback. The wait's held run
        # still ends the wait's narration: the reply is the whole sentence, not
        # its stray end.
        class _RefusesASecondStream(_RecSlack):
            async def start_stream(self, channel, thread_ts, **kw):
                if any(m == "start_stream" for m, _ in self.calls):
                    self.calls.append(("start_stream", {"thread_ts": thread_ts}))
                    return None
                return await super().start_stream(channel, thread_ts, **kw)

        rec = _RefusesASecondStream()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        events = [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="I will wait for CI to finish."),
            AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="w", title="wait", tool_name="wait"),
            AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="s", title="shell"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        asyncio.run(TurnDriver(_Provider(events), renderer, approval_mode="auto").run("x"))
        updates = [kw["text"] for m, kw in rec.calls if m == "update_message"]
        assert updates and updates[-1].strip() == "I will wait for CI to finish.", rec.calls

    def test_an_unfinished_turn_ending_on_a_tool_keeps_its_last_text(self):
        from kiro_crew.messaging.empty_turn_copy import UNFINISHED_TURN_NOTICE

        _, streamed, _ = self._run(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Two of three checks pass so far."),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="a", title="pytest"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="timeout"),
            ]
        )
        assert "".join(streamed) == (
            f"Two of three checks pass so far.\n\n{UNFINISHED_TURN_NOTICE}"
        ), streamed

    def test_the_options_stamp_records_the_whole_turn(self):
        # The stamp is the transcript row of an OPTIONS turn, so it carries
        # every step's text, as the driver produced it -- not just the reply.
        captured: list[str] = []

        async def _stamp(text: str) -> str:
            captured.append(text)
            return "tok"

        rec = _RecSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        renderer.stamp_options = _stamp
        events = [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Checking the branches."),
            AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="a", title="git branch"),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Which one?\n[OPTIONS: main | dev]"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        out = asyncio.run(TurnDriver(_Provider(events), renderer, approval_mode="auto").run("x"))
        assert captured == [out], captured
        assert captured[0].startswith("Checking the branches.")

    def test_a_failed_step_keeps_its_narration_and_adds_the_outcome_below(self):
        rec, _, tasks = self._run(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Running the tests now.\n"),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="a", title="pytest"),
                AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="a", tool_status="failed"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="They fail."),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        details = [t["details"] for t in tasks if t["details"]]
        assert details == ["Running the tests now.\npytest", "\nfailed"], tasks

    def test_short_text_between_two_calls_never_reaches_the_reply(self):
        rec, streamed, _ = self._run(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Writing the overlay."),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="a", title="write"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Done."),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="b", title="test"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="All good."),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        assert streamed == ["All good."], rec.calls

    def test_a_step_that_fails_before_its_narration_ends_lists_the_outcome_last(self):
        rec, _, tasks = self._run(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Reading the config."),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="a", title="read"),
                AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="a", tool_status="failed"),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="b", title="ls"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="It is missing."),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        read = [t for t in tasks if t["title"].startswith("read") and t["details"]]
        assert [t["details"] for t in read] == ["Reading the config.\nread\nfailed"], tasks
        assert read[0]["status"] == "error"

    def test_a_finished_turn_releases_nothing_more_when_it_then_dies(self):
        # A fault after the reply went out (a footer post that 4xxs) must not
        # send the reply a second time.
        rec = _RecSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        events = [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="All done."),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        asyncio.run(TurnDriver(_Provider(events), renderer, approval_mode="auto").run("x"))
        renderer._finalized = False  # as if the fault came before the flag
        asyncio.run(renderer.release_held_text())
        streamed = [kw["text"] for m, kw in rec.calls if m == "append_stream"]
        assert streamed == ["All done."], rec.calls


class TestATurnCutInsideAConstruct:
    """A tool call can land inside text that only renders whole (a background
    child's call interleaves with the parent's stream). The text is not cut
    there: it stays in the reply, so the construct is sealed whole."""

    @staticmethod
    def _run(events):
        rec = _RecSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        asyncio.run(TurnDriver(_Provider(events), renderer, approval_mode="auto").run("x"))
        streamed = [kw["text"] for m, kw in rec.calls if m == "append_stream"]
        tasks = [kw for m, kw in rec.calls if m == "append_task"]
        return rec, streamed, tasks

    def test_an_options_line_split_by_a_tool_call_keeps_its_choices(self):
        rec, streamed, tasks = self._run(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Pick one.\n[OPTIONS: Alpha | "),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="c", title="child step"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Beta]"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        assert "".join(streamed).strip() == "Pick one.", rec.calls
        assert not [t for t in tasks if "Pick one" in t.get("details", "")], tasks
        # Both choices reach the options control the footer posts.
        footer = str([kw["blocks"] for name, kw in rec.calls if name == "post_blocks"])
        assert "Alpha" in footer and "Beta" in footer, rec.calls

    def test_a_code_fence_split_by_a_tool_call_stays_one_block(self):
        _, streamed, tasks = self._run(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Run this:\n```\nmake "),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="c", title="child step"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="test\n```\nThen push."),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        reply = "".join(streamed)
        assert "```\nmake test\n```" in reply, streamed
        assert reply.startswith("Run this:"), streamed
        assert not [t for t in tasks if "make" in t.get("details", "")], tasks


class TestNarrationImages:
    """A step's details cannot carry a file, so a local image the narration
    links is uploaded with the reply's, not dropped with no trace."""

    def test_an_image_in_narration_is_uploaded(self, tmp_path):
        chart = tmp_path / "chart.png"
        chart.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        uploads: list[str] = []

        class _Uploads(_RecSlack):
            async def upload_file(self, channel, thread_ts, file, filename, title):
                uploads.append(filename)

        rec = _Uploads()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        renderer.authorize_upload_root(str(tmp_path))
        events = [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text=f"Here is the chart ![c]({chart}).\n"),
            AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="m", title="memory save"),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Done."),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        asyncio.run(TurnDriver(_Provider(events), renderer, approval_mode="auto").run("x"))
        assert uploads == ["chart.png"], rec.calls
        shown = " ".join(str(kw) for _m, kw in rec.calls)
        assert str(chart) not in shown

    def test_narration_and_reply_images_share_one_message_budget(self, tmp_path):
        # Six images in the narration and six in the answer: one reply, so one
        # budget of SLACK_MAX_UPLOAD_FILES; the rest are refused with a note.
        from kiro_crew.slack.files import SLACK_MAX_UPLOAD_FILES

        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        refs = []
        for n in range(12):
            (tmp_path / f"img{n}.png").write_bytes(png)
            refs.append(f"![i{n}]({tmp_path / f'img{n}.png'})")
        uploads: list[str] = []

        class _Uploads(_RecSlack):
            async def upload_file(self, channel, thread_ts, file, filename, title):
                uploads.append(filename)

        rec = _Uploads()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        renderer.authorize_upload_root(str(tmp_path))
        events = [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Charts:\n" + "\n".join(refs[:6]) + "\n"),
            AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="m", title="memory save"),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="More:\n" + "\n".join(refs[6:]) + "\nDone."),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        asyncio.run(TurnDriver(_Provider(events), renderer, approval_mode="auto").run("x"))
        assert len(uploads) == SLACK_MAX_UPLOAD_FILES, uploads
        streamed = "".join(kw["text"] for m, kw in rec.calls if m == "append_stream")
        assert "not sent" in streamed, streamed


class TestTheHeldEndOfAStepsNarration:
    """The driver holds the last run of the text before a tool call until it sees
    what follows; these pin what the renderer does with it."""

    def test_a_held_end_that_opens_an_options_line_goes_back_to_the_reply(self):
        # The cut saw only "Pick one.\n[": the held "OPTIONS:" shows the text
        # was the reply's, so it is sealed whole and the choices reach the footer.
        async def scenario():
            rec = _RecSlack()
            renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
            await renderer.on_text_chunk("Pick one.\n[")
            renderer.tool_text_held = True
            await renderer.on_tool_call("c", "Running: child step")
            renderer.tool_text_held = False
            await renderer.on_text_continuation("OPTIONS: Alpha | ")
            await renderer.on_text_chunk("Beta]")
            await renderer.on_done("end_turn")
            return rec

        rec = asyncio.run(scenario())
        streamed = "".join(kw["text"] for m, kw in rec.calls if m == "append_stream")
        assert streamed.strip() == "Pick one.", rec.calls
        tasks = [kw for m, kw in rec.calls if m == "append_task"]
        assert not [t for t in tasks if "Pick one" in t.get("details", "")], tasks
        footer = str([kw["blocks"] for name, kw in rec.calls if name == "post_blocks"])
        assert "Alpha" in footer and "Beta" in footer, rec.calls

    def test_an_approval_shows_the_steps_narration_before_its_buttons(self):
        # The held end comes only with the text after the decision, so the step
        # shows what there is when the approval goes up, marked as cut.
        async def scenario():
            rec = _RecSlack()
            renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
            await renderer.on_text_chunk("I'll delete the stale branch to clean ")
            renderer.tool_text_held = True
            await renderer.on_tool_call("d", "Running: git branch -D old")
            renderer.tool_text_held = False
            await renderer.on_prompt_choice(
                [{"id": "allow", "label": "Allow"}], 1, "git branch -D old"
            )
            return rec

        rec = asyncio.run(scenario())
        names = [m for m, _ in rec.calls]
        details = [kw.get("details", "") for m, kw in rec.calls if m == "append_task"]
        assert any("I'll delete the stale branch to clean…" in d for d in details), rec.calls
        last_detail = max(i for i, m in enumerate(names) if m == "append_task")
        assert last_detail < names.index("post_blocks"), names

    def test_an_approval_preview_keeps_the_narration_its_held_end_completes(self):
        # After the preview the step is still the narration's owner: a second
        # call with no text between, then the held end, then the turn's end
        # answers with the whole sentence, not the held fragment.
        async def scenario():
            rec = _RecSlack()
            renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
            await renderer.on_text_chunk("I will delete the stale ")
            renderer.tool_text_held = True
            await renderer.on_tool_call("a", "Running: git branch -D old")
            await renderer.on_prompt_choice([{"id": "allow", "label": "Allow"}], 1, "git")
            await renderer.on_tool_call("b", "Running: git status")
            renderer.tool_text_held = False
            await renderer.on_text_continuation("branch.")
            await renderer.on_done("end_turn")
            return rec

        rec = asyncio.run(scenario())
        streamed = "".join(kw["text"] for m, kw in rec.calls if m == "append_stream")
        assert streamed.strip() == "I will delete the stale branch.", rec.calls
        details = [kw.get("details", "") for m, kw in rec.calls if m == "append_task"]
        # The preview is written once; the held end does not draw it again.
        assert sum("I will delete the stale" in d for d in details) == 1, details


class TestChoicesOfferedBeforeAToolCall:
    def test_an_options_line_in_narration_still_posts_its_buttons(self):
        # The step shows the question; the choices go in the turn's footer, and
        # the stamp still records the whole turn.
        captured: list[str] = []

        async def _stamp(text: str) -> str:
            captured.append(text)
            return "tok"

        rec = _RecSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        renderer.stamp_options = _stamp
        events = [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Which one?\n\n[OPTIONS: Alpha | Beta]\n"),
            AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="n", title="memory save"),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Saved your note."),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        out = asyncio.run(TurnDriver(_Provider(events), renderer, approval_mode="auto").run("x"))
        footer = str([kw["blocks"] for name, kw in rec.calls if name == "post_blocks"])
        assert "Alpha" in footer and "Beta" in footer, rec.calls
        assert captured == [out], captured

    def test_the_last_options_line_in_narration_wins(self):
        # The dashboard reads the row's last marker; Slack's footer must match.
        rec = _RecSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        events = [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Which env?\n[OPTIONS: Alpha | Beta]\n"),
            AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="a", title="git branch"),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Which region?\n[OPTIONS: East | West]\n"),
            AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="b", title="ls"),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Checked."),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        asyncio.run(TurnDriver(_Provider(events), renderer, approval_mode="auto").run("x"))
        footer = str([kw["blocks"] for name, kw in rec.calls if name == "post_blocks"])
        assert "East" in footer and "West" in footer, rec.calls
        assert "Alpha" not in footer, rec.calls
