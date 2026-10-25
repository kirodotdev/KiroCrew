"""The Discord renderer: streaming, finalization and what a landed message shows.

``DiscordRenderer`` streams a turn into one live message, rotates it at the
platform cap and seals it on finish; ``DiscordApprovalDecider`` turns a
permission request into buttons. Redaction is checked where it lands: one
follow-up notice when redaction rewrote a landed message, no credential put
together across a rotation seam, and a live bubble's seam grade kept across its
edits and its seal.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from discord_helpers import _drop_live_config_snapshot  # noqa: F401
from discord_helpers import FakeClient

from conftest import CREDENTIAL_STRADDLE_SHAPES
from kiro_crew.discord import renderer as discord_renderer
from kiro_crew.discord.client import DISCORD_MAX_TEXT
from kiro_crew.discord.renderer import (
    DiscordApprovalDecider,
    DiscordRenderer,
    _delivered_form,
    _redact_all,
    session_provenance_tag,
)
from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.messaging.display_safety import canonicalize_display, severs_a_credential
from kiro_crew.messaging.split import split_markdown_safe
from kiro_crew.testing.wait import finished

# ── renderer.py streaming/finalization ───────────────────────────────────


class TestRenderer:
    def _renderer(self) -> tuple[DiscordRenderer, FakeClient]:
        cli = FakeClient()
        r = DiscordRenderer(cli, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
        return r, cli

    @pytest.mark.asyncio
    async def test_stream_and_finalize(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_text_chunk("Hello ")
        await r.on_text_chunk("world")
        await r.on_done()
        assert cli.final_text().startswith("Hello world")
        # The turn footer is its own trailing subtext line, so the answer is a
        # prefix of the message rather than the whole of it.
        assert "\n\n-# Finished in " in cli.final_text()

    @pytest.mark.asyncio
    async def test_a_late_reasoning_note_does_not_duplicate_streamed_text(self) -> None:
        """Reasoning after streamed text must not re-post the answer already shown.

        ``_flush_thinking`` posts the note as its own message below the live answer
        bubble, sealing that bubble's segment first so ``_buf`` is consumed and the
        live id cleared. The streamed answer is therefore shown exactly once: the
        sealed bubble holds it, and the chunk after the note carries only new text.
        """
        cli = FakeClient()
        r = DiscordRenderer(
            cli, "chan1", DISCORD_CAPABILITIES, session_key="sk", show_thinking=True  # type: ignore[arg-type]
        )
        await r.on_turn_start()
        await r.on_text_chunk("The answer is Foo. ")
        await r.on_thinking("some private reasoning")
        await r.on_text_chunk("And here is more.")
        await r.on_done()
        # Reconstruct what the reader actually sees: each message id in send order,
        # with its FINAL text after any in-place edits (an edit updates a message
        # already delivered, it is not a second message). The streamed sentence is
        # streamed into one bubble and sealed there, so it appears on screen once;
        # a second bubble carrying it below the note would show it twice.
        mids: list[str] = []
        final: dict[str, str] = {}
        mid = 100
        for text, _ in cli.sent:
            mid += 1
            key = str(mid)
            mids.append(key)
            final[key] = text
        for edit_mid, text, _ in cli.edits:
            final[edit_mid] = text
        on_screen = [final[m] for m in mids]
        assert sum(t.count("The answer is Foo.") for t in on_screen) == 1
        # The reasoning note is its own delivered message.
        assert any("💭" in t for t in on_screen)
        # The later text is delivered, and does not re-prepend the shown answer.
        assert any("And here is more." in t and "The answer is Foo." not in t for t in on_screen)

    @pytest.mark.asyncio
    async def test_reasoning_as_the_last_event_still_finalizes_the_answer(self) -> None:
        """Reasoning that is the last event before on_done must not un-finalize.

        text -> on_thinking -> on_done with no further chunk reaches
        ``_flush_thinking`` from ``on_done`` itself. The note is still posted, but
        the answer segment must NOT be sealed there: ``on_done`` still has to attach
        the turn footer (and options/tables). Sealing early shipped the answer
        without its footer AND left ``on_done`` an empty segment that posted a
        spurious ``"…"`` placeholder message.
        """
        cli = FakeClient()
        r = DiscordRenderer(
            cli, "chan1", DISCORD_CAPABILITIES, session_key="sk", show_thinking=True  # type: ignore[arg-type]
        )
        await r.on_turn_start()
        await r.on_text_chunk("The whole answer.")
        await r.on_thinking("trailing reasoning")
        await r.on_done()
        mids: list[str] = []
        final: dict[str, str] = {}
        mid = 100
        for text, _ in cli.sent:
            mid += 1
            mids.append(str(mid))
            final[str(mid)] = text
        for edit_mid, text, _ in cli.edits:
            final[edit_mid] = text
        on_screen = [final[m] for m in mids]
        # The answer is delivered and carries the turn footer (it was finalized by
        # on_done, not sealed early by the note flush).
        assert any("The whole answer." in t and "\n\n-# Finished in " in t for t in on_screen)
        # No spurious placeholder-only message.
        assert not any(t.strip().startswith("…") for t in on_screen)
        # The reasoning note is still delivered.
        assert any("💭" in t for t in on_screen)

    @pytest.mark.asyncio
    async def test_a_trailing_note_is_not_the_predecessor_of_the_bubble_above_it(self) -> None:
        """A note left above an un-sealed live bubble must not become its seam base.

        On a trailing-reasoning turn the note is posted BELOW a still-live answer
        bubble (the finalize path skips the seal, so ``_stream_mid`` stays on that
        bubble). Recording the note as ``_sent_tail`` would make ``on_done``'s edit
        of the bubble ABOVE it grade against the note, and a note tail + answer head
        that join into a key would replace a leading span of VALID answer text with
        a redaction tag. The note is recorded only when the next delivery lands
        below it (``_stream_mid is None``).
        """
        cli = FakeClient()
        r = DiscordRenderer(
            cli, "chan1", DISCORD_CAPABILITIES, session_key="sk", show_thinking=True  # type: ignore[arg-type]
        )
        await r.on_turn_start()
        await r.on_text_chunk("The answer body.")
        await r.on_thinking("reasoning that arrives last")
        # At this point a live bubble is still open above the just-posted note.
        assert r._stream_mid is not None
        # The note did NOT displace the answer bubble as the graded predecessor.
        assert "💭" not in r._sent_tail
        await r.on_done()
        # And the finalized answer is intact (not corrupted by a note-seam grade).
        mids: list[str] = []
        final: dict[str, str] = {}
        mid = 100
        for text, _ in cli.sent:
            mid += 1
            mids.append(str(mid))
            final[str(mid)] = text
        for edit_mid, text, _ in cli.edits:
            final[edit_mid] = text
        on_screen = [final[m] for m in mids]
        assert any("The answer body." in t for t in on_screen)

    @pytest.mark.asyncio
    async def test_a_failed_seal_reports_not_landed_so_the_buffer_is_kept(self) -> None:
        """A seal whose delivery fails must return False, not report success.

        ``_flush_thinking`` (and any caller) discards ``_buf`` only on a True. If
        ``_seal_current`` returned True after a chunk failed to land, the segment
        the reader never saw would be dropped from any later delivery with no retry.
        """
        cli = FakeClient()
        r = DiscordRenderer(cli, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
        r._buf = ["A segment that will fail to deliver."]
        # No live message id: _seal_current takes the fresh-send path; force it to
        # fail so nothing lands.
        cli.fail_sends = True
        landed = await r._seal_current(extract_uploads=False)
        assert landed is False

    @pytest.mark.asyncio
    async def test_a_landed_seal_reports_true(self) -> None:
        """The success path still returns True so the buffer is cleared normally."""
        cli = FakeClient()
        r = DiscordRenderer(cli, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
        r._buf = ["A segment that delivers cleanly."]
        landed = await r._seal_current(extract_uploads=False)
        assert landed is True

    @pytest.mark.asyncio
    async def test_options_become_buttons_and_never_stream(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_text_chunk("Pick\n[OPTIONS: A | B]")
        # Live frames must never show the raw directive.
        for text, _ in cli.sent:
            assert "[OPTIONS" not in text
        await r.on_done()
        comps = cli.final_components()
        assert comps is not None
        labels = [b["label"] for row in comps for b in row["components"]]
        assert labels == ["A", "B"]
        assert "[OPTIONS" not in cli.final_text()
        # The sealed row is PROVENANCE-STAMPED with this renderer's session key —
        # the producer half of the stale-press fix. Without this pin, reverting
        # the call sites to untagged build_option_components(opts) keeps the
        # whole suite green while every new button falls back to the legacy
        # current-binding path, silently reopening cross-session injection.
        ids = [b["custom_id"] for row in comps for b in row["components"]]
        assert ids == [
            f"opt:0:{session_provenance_tag('sk')}",
            f"opt:1:{session_provenance_tag('sk')}",
        ]

    @pytest.mark.asyncio
    async def test_long_options_before_streamed_steer_ack_become_buttons(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        # The assistant's final line is a valid OPTIONS trailer. The provider's
        # internal steer acknowledgment follows it and arrives across chunks,
        # with the combined buffer well past Discord's message cap.
        await r.on_text_chunk(
            ("x" * 3800) + "\n\n[OPTIONS: Alpha | Bravo | Charlie]" + "\n\n[STEERING steer-7e6a4a0d"
        )
        await r.on_text_chunk("94314d2db: acknowledged]")
        await r.on_done()

        components = [c for _, c in cli.sent if c] + [c for _, _, c in cli.edits if c]
        labels = [b["label"] for row in components[0] for b in row["components"]]
        assert labels == ["Alpha", "Bravo", "Charlie"]
        visible = "\n".join([t for t, _ in cli.sent] + [t for _, t, _ in cli.edits])
        assert "[OPTIONS" not in visible
        assert "[STEERING" not in visible
        assert "steer-7e6a4a0d" not in visible
        assert "94314d2db" not in visible

    @pytest.mark.asyncio
    async def test_long_output_rotates_messages(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_text_chunk("A" * 5000)
        await r.on_done()
        # More than one message posted, none over the API cap.
        assert len(cli.sent) >= 2
        for text, _ in cli.sent:
            assert len(text) <= 2000
        for _, text, _c in cli.edits:
            assert len(text) <= 2000

    @pytest.mark.asyncio
    async def test_rotation_mid_code_block_keeps_live_fence_open(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        # Open a fence and stream past one message's worth of code so rotation
        # fires while the model's closing ``` has NOT arrived yet.
        await r.on_text_chunk("```py\n" + "x = 1\n" * 400)
        # The SEALED chunk is self-contained (synthetic closer appended)…
        sealed = cli.sent[0][0]
        assert sealed.count("```") % 2 == 0
        assert sealed.rstrip().endswith("```")
        # …but the retained live buffer must keep its fence OPEN, or everything
        # streamed afterwards renders as prose outside the code block.
        assert "".join(r._buf).count("```") % 2 == 1
        assert "".join(r._buf).startswith("```")  # continuation reopens the fence
        # The model's real closer finally streams in.
        await r.on_text_chunk("y = 2\n```")
        await r.on_done()
        final = cli.final_text()
        assert final.count("```") % 2 == 0  # balanced -> no stray backticks
        # The fence must CLOSE, which the balance check above already proves;
        # the message does not END on the closer because the turn footer is
        # appended as a trailing subtext line after it.
        assert final.startswith("```")
        assert final.split("-# Finished in ")[0].rstrip().endswith("```")
        assert "y = 2" in final.split("```")[1]  # post-rotation code stays inside

    @pytest.mark.asyncio
    async def test_rotation_mid_code_block_keeps_line_break(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        # The retained tail's content ends with a newline; dropping the
        # synthetic closer must not eat it, or the next streamed line lands on
        # the previous one ("x = 1y = 2") and both code lines are corrupted.
        await r.on_text_chunk("```py\n" + "x = 1\n" * 400)
        assert "".join(r._buf).endswith("\n")
        await r.on_text_chunk("y = 2\n```")
        await r.on_done()
        final = cli.final_text()
        assert "x = 1y = 2" not in final
        assert "x = 1\ny = 2" in final

    @pytest.mark.asyncio
    async def test_rotation_keeps_blank_code_lines(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        # The rotation boundary lands on the blank lines the model just emitted
        # INSIDE the open fence, leaving a newline-only remainder. Dropping it
        # pulls the next code line up onto the last one.
        await r.on_text_chunk("```py\n" + "x = 1\n" * 299 + "\n\n")
        assert "".join(r._buf).endswith("\n\n\n")
        await r.on_text_chunk("y = 2\n```")
        await r.on_done()
        final = cli.final_text()
        assert "x = 1\ny = 2" not in final  # later code did not shift up
        assert "\n\ny = 2" in final  # the blank code line survived

    @pytest.mark.asyncio
    async def test_rotation_keeps_blank_code_lines_before_more_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r, cli = self._renderer()
        monkeypatch.setattr(r, "_limit", lambda: 60)
        await r.on_turn_start()
        # Blank code lines straddling the rotation cut, with more code AFTER
        # them. The all-newline-tail case is already pinned above; this is the
        # one the round-2 remainder deleted -- the cut lands on the run, the
        # sealed side keeps the code, and the blank lines belong to the tail.
        await r.on_text_chunk("```py\n" + "x = 1\n" * 6 + "\n\n" + "y = 2\n" * 6)
        sealed = cli.sent[0][0]
        assert sealed.endswith("```")  # code sealed, synthetic closer on
        # The retained tail reopens the fence with the AUTHORED opener (language
        # tag and all, where a bare ``` reopen would have lost it).
        assert "".join(r._buf).startswith("```py\n")
        await r.on_text_chunk("```")
        await r.on_done()
        # The blank code line survived to what the user reads and the later code
        # did not shift up. Only ONE blank line is asserted here: _strip_steering
        # collapses every run of 3+ newlines to 2 on EVERY render, so the visible
        # cap is that normalizer's, not the splitter's.
        visible = "".join([t for t, _ in cli.sent] + [t for _, t, _c in cli.edits])
        assert "\n\n" in visible
        assert "x = 1y = 2" not in visible

    @pytest.mark.asyncio
    async def test_rotation_ignores_inline_backticks_in_prose(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        # Prose ABOUT fencing: the ``` sits mid-line, so it opens nothing. A
        # ``` SUBSTRING count reads the stream as "inside a code block", so the
        # splitter invents a closer for the sealed chunk, reopens the fence on
        # the retained tail, and the rotation then deletes that tail's closer --
        # leaving the live buffer inside an UNCLOSED code block, so every later
        # sentence renders as code.
        await r.on_text_chunk(
            "To open a code block you type ``` at the start of a line. "
            + ("Ordinary prose about how chat surfaces render markdown. " * 45)
        )
        buf = "".join(r._buf)
        assert "```" not in buf  # no reopen, no synthetic closer
        await r.on_text_chunk("Final sentence, outside any code block.")
        await r.on_done()
        # The author wrote no fence LINE anywhere, so any bare ``` line in any
        # frame -- live or sealed -- is one this renderer invented.
        for text in [t for t, _ in cli.sent] + [t for _, t, _c in cli.edits]:
            assert not any(ln.strip() == "```" for ln in text.split("\n"))
        assert "Final sentence, outside any code block." in cli.final_text()

    @pytest.mark.asyncio
    async def test_rotation_keeps_the_tail_open_behind_inline_backticks(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        # The mirror miscount: one literal ``` in the prose plus a genuinely
        # OPEN fence makes a ``` SUBSTRING count even, so a parity-based
        # splitter seals the retained tail shut around a live code block.
        await r.on_text_chunk(
            "You type ``` to open a block, like this:\n\n```py\n" + "x = 1\n" * 400
        )
        buf = "".join(r._buf)
        assert buf.startswith("```py\n")  # continuation reopens the live fence
        assert not buf.rstrip().endswith("```")  # and it is left OPEN
        await r.on_text_chunk("y = 2\n```")
        await r.on_done()
        final = cli.final_text()
        assert final.count("```") % 2 == 0
        assert "x = 1\ny = 2" in final  # post-rotation code stayed in the block

    @pytest.mark.asyncio
    async def test_authored_trailing_backticks_survive_when_nothing_split(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        # Prose about fencing ends with a literal ``` and is itself UNDER the
        # cap -- only the long [OPTIONS:] trailer pushes the buffer over it. The
        # trailer is detached before splitting, so the splitter hands back a
        # lone chunk, which is the final chunk and therefore untouched: the
        # author's backticks survive.
        body = ("To fence a block in Discord, open the line with " * 36) + "type ```"
        assert len(body) < r._limit()
        trailer = "\n\n[OPTIONS: " + ("Yes " * 20) + " | " + ("No " * 20) + "]"
        await r.on_text_chunk(body + trailer)
        await r.on_done()
        visible = "\n".join([t for t, _ in cli.sent] + [t for _, t, _c in cli.edits])
        assert "type ```" in visible

    @pytest.mark.asyncio
    async def test_rotation_keeps_authored_inner_fence_line_in_4_backtick_block(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        # A 4-backtick block whose CONTENT is markdown containing 3-backtick
        # examples -- how you document fencing. Per CommonMark the inner bare
        # ``` closes nothing (a closer must be at least as long as its opener),
        # so the source fence is still open at the cut.
        #
        # A per-chunk fence walk run AFTER prepending a bare ``` reopen throws
        # away the 4-backtick opener's run length: to that walk the tail's fence
        # is a 3-backtick one the authored inner ``` CLOSES. The shared splitter
        # carries the opener verbatim instead, so the reopen is the real fence
        # and the author's own ``` line is never mistaken for scaffolding.
        src = "````markdown\n" + "Nest a block:\n\n```py\nx = 1\n```\n\n" * 90
        assert len(src) > r._limit()
        await r.on_text_chunk(src)
        buf = "".join(r._buf)
        assert len(buf) < len(src)  # the rotation really fired
        assert buf.startswith("````markdown\n")  # run length and info string kept
        # The author's inner closer is the last thing they wrote; it must still
        # be there, with the blank line that followed it.
        assert buf.endswith("```\n\n")
        # Stronger: the retained tail IS the source's own tail (modulo the
        # continuation reopen) -- not a shortened copy of it.
        assert src.endswith(buf[len("````markdown\n") :])
        # It survives all the way to what the user reads.
        await r.on_text_chunk("Done.\n````")
        await r.on_done()
        frames = [t for t, _ in cli.sent] + [t for _, t, _c in cli.edits]
        authored = src.count("\n```\n")
        assert sum(f.count("\n```\n") for f in frames) >= authored

    @pytest.mark.asyncio
    async def test_tool_footer_transient(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_tool_call("t1", "grep")
        assert any("grep" in text for text, _ in cli.sent)
        await r.on_text_chunk("Result body")
        await r.on_done()
        assert "grep" not in cli.final_text()

    @pytest.mark.asyncio
    async def test_error_placeholder_when_no_output(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_done(stop_reason="error")
        assert "⚠️" in cli.final_text()

    @pytest.mark.asyncio
    async def test_prompt_choice_sends_separate_approval_message(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_prompt_choice([], request_id="req9")
        text, comps = cli.sent[-1]
        assert "Approve" in text
        ids = [b["custom_id"] for row in comps for b in row["components"]]
        # a:<rid>:<nonce>:<flag> — nonce guards against reused request IDs.
        assert len(ids) == 2
        assert ids[0].startswith("a:req9:") and ids[0].endswith(":1")
        assert ids[1].startswith("a:req9:") and ids[1].endswith(":0")
        from kiro_crew.messaging.renderer import new_approval_nonce

        nonce = ids[0].split(":")[2]
        # Length is the shared minter's, not a Discord-local literal: three channels
        # mint approval nonces and a per-channel copy is how one gets a weaker one.
        assert len(nonce) == len(new_approval_nonce()) > 10
        DiscordApprovalDecider._NONCES.pop(DiscordApprovalDecider.key("sk", "req9"), None)

    @pytest.mark.asyncio
    async def test_prompt_names_the_tool_the_request_is_about(self) -> None:
        """`_last_tool` is never cleared, so it names the PREVIOUS tool.

        A permission that arrives without its own titled tool_call would otherwise
        ask the operator to approve something other than what is about to run.
        """
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_tool_call("t1", "fs_read")
        await r.on_prompt_choice([], request_id="req1", tool_title="execute_bash")
        text, _ = cli.sent[-1]
        assert "execute_bash" in text
        assert "fs_read" not in text
        DiscordApprovalDecider._NONCES.pop(DiscordApprovalDecider.key("sk", "req1"), None)

    @pytest.mark.asyncio
    async def test_prompt_falls_back_to_the_last_tool_without_a_title(self) -> None:
        """Non-vacuity: the fallback still runs when the event carried no name."""
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_tool_call("t1", "fs_read")
        await r.on_prompt_choice([], request_id="req2")
        text, _ = cli.sent[-1]
        assert "fs_read" in text
        DiscordApprovalDecider._NONCES.pop(DiscordApprovalDecider.key("sk", "req2"), None)

    @pytest.mark.asyncio
    async def test_steer_marker_rotates_message_with_chip(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_text_chunk("first part [STEERING steer-ab12: focus on Y] second part")
        await r.on_done()
        all_texts = [t for t, _ in cli.sent] + [t for _, t, _ in cli.edits]
        # Marker never shown raw; chip carries the summary.
        assert all("[STEERING" not in t for t in all_texts)
        assert any("focus on Y" in t for t in all_texts)

    @pytest.mark.asyncio
    async def test_close_finalizes_unfinished_turn(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_text_chunk("partial")
        await r.close()
        assert cli.final_text().startswith("partial")
        assert "\n\n-# Finished in " in cli.final_text()

    @pytest.mark.asyncio
    async def test_no_rotation_steer_summary_chip(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        r.note_steer("my steer words")
        await r.on_text_chunk("answer body")
        await r.on_done()
        assert "my steer words" in cli.final_text()
        assert "answer body" in cli.final_text()


# ── DiscordApprovalDecider ───────────────────────────────────────────────


class TestApprovalDecider:
    @pytest.mark.asyncio
    async def test_resolve_approves_with_valid_nonce(self) -> None:
        decider = DiscordApprovalDecider(session_key="sk")
        ev = SimpleNamespace(request_id="r1")
        task = asyncio.ensure_future(decider(ev))
        # flake-ok: zero-delay yield so the decider registers its future; moved verbatim
        await asyncio.sleep(0)  # let the Future register
        key = DiscordApprovalDecider.key("sk", "r1")
        nonce = DiscordApprovalDecider.register_nonce(key)
        assert DiscordApprovalDecider.resolve_global(key, True, nonce=nonce)
        # flake-ok: awaits a task the line above resolved; moved verbatim
        assert await task is True

    @pytest.mark.asyncio
    async def test_stale_nonce_fails_closed(self) -> None:
        """A button from an earlier prompt (reused request ID) cannot resolve
        a new pending request — the nonce must match the CURRENT prompt's."""
        decider = DiscordApprovalDecider(session_key="sk")
        ev = SimpleNamespace(request_id="r1")
        task = asyncio.ensure_future(decider(ev))
        # flake-ok: zero-delay yield so the decider registers its future; moved verbatim
        await asyncio.sleep(0)
        key = DiscordApprovalDecider.key("sk", "r1")
        DiscordApprovalDecider.register_nonce(key)  # current prompt's nonce
        # Press carries an OLD nonce (from a prompt before a restart).
        assert not DiscordApprovalDecider.resolve_global(key, True, nonce="deadbeefdeadbeef")
        assert not task.done()  # still pending — stale press had no effect
        # A missing nonce also fails closed.
        assert not DiscordApprovalDecider.resolve_global(key, True)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            # flake-ok: awaits a task the test cancelled above; moved verbatim
            await task
        DiscordApprovalDecider._NONCES.pop(key, None)

    @pytest.mark.asyncio
    async def test_resolve_unknown_key_returns_false(self) -> None:
        assert not DiscordApprovalDecider.resolve_global("sk:none", True, nonce="x")

    @pytest.mark.asyncio
    async def test_timeout_records_the_stall_for_a_bound_loop(self, monkeypatch) -> None:
        """A Discord nudge cycle stalls here, not in the dashboard runner.

        Without this the loop keeps waking, is denied by default, and spends its
        whole cycle cap accomplishing nothing.
        """
        from kiro_crew import autonudge as _an
        from kiro_crew.discord import renderer as _rend

        recorded: list[str] = []
        monkeypatch.setattr(
            _an,
            "get_instance",
            lambda: SimpleNamespace(notify_approval_stalled=recorded.append),
        )
        # flake-ok: the test lets a 10 ms production timeout expire on purpose; moved verbatim
        monkeypatch.setattr(_rend, "_APPROVAL_TIMEOUT_S", 0.01)

        decider = DiscordApprovalDecider(session_key="discord:a:direct:7")
        assert await decider(SimpleNamespace(request_id="r-timeout")) is False

        assert recorded == ["discord:a:direct:7"], "the stall was not recorded"

    @pytest.mark.asyncio
    async def test_a_pressed_deny_is_not_recorded_as_a_stall(self, monkeypatch) -> None:
        """An explicit deny is a decision, not evidence that nobody is present.

        Recording it would stop a loop whose operator is right there declining
        one tool.
        """
        from kiro_crew import autonudge as _an

        recorded: list[str] = []
        monkeypatch.setattr(
            _an,
            "get_instance",
            lambda: SimpleNamespace(notify_approval_stalled=recorded.append),
        )

        decider = DiscordApprovalDecider(session_key="discord:a:direct:7")
        ev = SimpleNamespace(request_id="r-deny")
        task = asyncio.ensure_future(decider(ev))
        # flake-ok: zero-delay yield so the decider registers its future; moved verbatim
        await asyncio.sleep(0)
        key = DiscordApprovalDecider.key("discord:a:direct:7", "r-deny")
        nonce = DiscordApprovalDecider.register_nonce(key)
        assert DiscordApprovalDecider.resolve_global(key, False, nonce=nonce)

        # flake-ok: awaits a task the test resolved above; moved verbatim
        assert await task is False
        assert recorded == [], "a pressed deny must not count as an unanswered prompt"


# ── an approval press that lands before the waiter is registered ─────────
#
# ``TurnDriver`` dispatches ``PROMPT_CHOICE`` to the renderer and only then awaits
# the decider. The renderer suspends in between (a thread hop for the
# display-safety scan, then the send), so a press can be processed while no future
# is registered yet. The decision window opens when the nonce is armed; a press
# with no armed nonce, a foreign nonce, or a decision left unawaited must still be
# unable to answer a request the user was never asked about.


class _SendCapture:
    """Minimal outbound capture: only the approval prompt's own send is used."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, object]] = []
        self.raise_on_send = False
        #: When set, every send returns None, which is how the real client reports
        #: a failure: a revoked token, a channel it cannot write to, a rate limit
        #: or 5xx past its retries. It does NOT raise for those.
        self.fail_sends = False

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        components: object = None,
        reply_to_message_id: object = None,
    ) -> str | None:
        if self.raise_on_send:
            raise RuntimeError("send exploded")
        self.sent.append((channel_id, text, components))
        return None if self.fail_sends else "m1"


class _PressDuringSend(_SendCapture):
    """Presses Approve from inside the send, the widest part of the early-press gap.

    The press runs while the renderer is suspended on the send and before the
    driver has reached the decider.
    """

    def __init__(self, key: str, *, approved: bool = True) -> None:
        super().__init__()
        self._key = key
        self._approved = approved
        #: What ``resolve_global`` answered the presser. False would mean the
        #: handler reports the approval as expired.
        self.press_resolved: bool | None = None

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        components: object = None,
        reply_to_message_id: object = None,
    ) -> str | None:
        nonce = _nonce_from(components)
        self.press_resolved = DiscordApprovalDecider.resolve_global(
            self._key, self._approved, nonce=nonce
        )
        return await super().send_message(
            channel_id, text, components=components, reply_to_message_id=reply_to_message_id
        )


def _nonce_from(components: object) -> str:
    """The nonce Discord would send back with a press on these buttons."""
    assert isinstance(components, list) and components
    row = components[0]
    assert isinstance(row, dict)
    approve = row["components"][0]
    # ``a:<request_id>:<nonce>:<1|0>``
    return str(approve["custom_id"]).split(":")[2]


@pytest.fixture
def _clean_approval_registry():
    """No cross-test bleed through the decider's process-global maps."""
    DiscordApprovalDecider._REGISTRY.clear()
    DiscordApprovalDecider._NONCES.clear()
    yield
    DiscordApprovalDecider._REGISTRY.clear()
    DiscordApprovalDecider._NONCES.clear()


def _approval_renderer(client: object) -> DiscordRenderer:
    return DiscordRenderer(
        client,  # type: ignore[arg-type]
        "chan1",
        DISCORD_CAPABILITIES,
        session_key="sk",
    )


@pytest.mark.usefixtures("_clean_approval_registry")
class TestPressBeforeTheWaiter:
    """A press in the gap is honoured, not dropped and reported as expired."""

    @pytest.mark.asyncio
    async def test_press_while_the_prompt_is_being_sent_is_honored(self) -> None:
        key = DiscordApprovalDecider.key("sk", "r1")
        cli = _PressDuringSend(key)
        await _approval_renderer(cli).on_prompt_choice([], "r1", tool_title="shell")
        # The presser was told the decision landed, not that it had expired.
        assert cli.press_resolved is True
        # And the driver's later wait reads that decision instead of denying.
        decider = DiscordApprovalDecider(session_key="sk")
        assert await decider(SimpleNamespace(request_id="r1")) is True
        assert decider.last_deny_cause == ""

    @pytest.mark.asyncio
    async def test_deny_press_in_the_gap_is_honored_as_a_denial(self) -> None:
        key = DiscordApprovalDecider.key("sk", "r2")
        cli = _PressDuringSend(key, approved=False)
        await _approval_renderer(cli).on_prompt_choice([], "r2", tool_title="shell")
        assert cli.press_resolved is True
        decider = DiscordApprovalDecider(session_key="sk")
        assert await decider(SimpleNamespace(request_id="r2")) is False

    @pytest.mark.asyncio
    async def test_the_recorded_decision_is_consumed_once(self) -> None:
        """A second request reusing the id gets its own window, not this answer."""
        key = DiscordApprovalDecider.key("sk", "r3")
        cli = _PressDuringSend(key)
        await _approval_renderer(cli).on_prompt_choice([], "r3", tool_title="shell")
        decider = DiscordApprovalDecider(session_key="sk")
        assert await decider(SimpleNamespace(request_id="r3")) is True
        assert key not in DiscordApprovalDecider._REGISTRY
        assert key not in DiscordApprovalDecider._NONCES


@pytest.mark.usefixtures("_clean_approval_registry")
class TestWindowStillFailsClosed:
    """Opening the window earlier must not widen what a press can reach."""

    @pytest.mark.asyncio
    async def test_press_before_the_nonce_is_armed_is_refused(self) -> None:
        key = DiscordApprovalDecider.key("sk", "r4")
        assert DiscordApprovalDecider.resolve_global(key, True, nonce="whatever") is False

    @pytest.mark.asyncio
    async def test_foreign_nonce_in_the_gap_is_refused_and_leaves_the_window_live(
        self,
    ) -> None:
        key = DiscordApprovalDecider.key("sk", "r5")
        DiscordApprovalDecider.register_nonce(key)
        assert DiscordApprovalDecider.resolve_global(key, True, nonce="deadbeefdeadbeef") is False
        reserved = DiscordApprovalDecider._REGISTRY.get(key)
        assert reserved is not None and not reserved.done()

    @pytest.mark.asyncio
    async def test_a_decision_left_unawaited_cannot_answer_the_next_request(self) -> None:
        """The replay bound. Same key, new prompt, so the old answer is void."""
        key = DiscordApprovalDecider.key("sk", "r6")
        stale_nonce = DiscordApprovalDecider.register_nonce(key)
        assert DiscordApprovalDecider.resolve_global(key, True, nonce=stale_nonce) is True
        # The turn died before the driver reached the decider: nobody read it.
        fresh_nonce = DiscordApprovalDecider.register_nonce(key)
        assert fresh_nonce != stale_nonce
        # The old press cannot reach the new prompt.
        assert DiscordApprovalDecider.resolve_global(key, True, nonce=stale_nonce) is False
        # Nor can the recorded approval be adopted by the new request's wait.
        reserved = DiscordApprovalDecider._REGISTRY.get(key)
        assert reserved is not None and not reserved.done()

    @pytest.mark.asyncio
    async def test_a_second_arm_does_not_orphan_a_live_waiter(self) -> None:
        """Re-arming keeps the future the waiter is blocked on."""
        decider = DiscordApprovalDecider(session_key="sk")
        task = asyncio.ensure_future(decider(SimpleNamespace(request_id="r7")))
        # flake-ok: zero-delay yield so the decider registers its future; moved verbatim
        await asyncio.sleep(0)
        key = DiscordApprovalDecider.key("sk", "r7")
        nonce = DiscordApprovalDecider.register_nonce(key)
        assert DiscordApprovalDecider.resolve_global(key, True, nonce=nonce) is True
        # flake-ok: awaits a task the line above resolved; moved verbatim
        assert await task is True


@pytest.mark.usefixtures("_clean_approval_registry")
class TestPromptThatNeverWentOut:
    @pytest.mark.asyncio
    async def test_retire_closes_the_window(self) -> None:
        key = DiscordApprovalDecider.key("sk", "r8")
        nonce = DiscordApprovalDecider.register_nonce(key)
        DiscordApprovalDecider.retire(key)
        assert DiscordApprovalDecider.resolve_global(key, True, nonce=nonce) is False
        assert key not in DiscordApprovalDecider._REGISTRY
        # Idempotent: a caller that retires twice is not an error.
        DiscordApprovalDecider.retire(key)

    @pytest.mark.asyncio
    async def test_a_failed_send_retires_the_window_and_raises(self) -> None:
        cli = _SendCapture()
        cli.raise_on_send = True
        key = DiscordApprovalDecider.key("sk", "r9")
        with pytest.raises(RuntimeError):
            await _approval_renderer(cli).on_prompt_choice([], "r9", tool_title="shell")
        assert key not in DiscordApprovalDecider._NONCES
        assert key not in DiscordApprovalDecider._REGISTRY

    @pytest.mark.asyncio
    async def test_a_send_that_returns_no_id_denies_at_once(self) -> None:
        """The failure the real client actually reports: no id, no exception.

        Dropping the reservation here would look tidy and still cost the user the
        whole decision window on a prompt nobody can see, so the refusal is
        recorded for the wait to adopt.
        """
        cli = _SendCapture()
        cli.fail_sends = True
        await _approval_renderer(cli).on_prompt_choice([], "r11", tool_title="shell")
        decider = DiscordApprovalDecider(session_key="sk")
        # No timeout patch: a window that is waited out would hang this test.
        assert await finished(decider(SimpleNamespace(request_id="r11")), "the decision") is False
        key = DiscordApprovalDecider.key("sk", "r11")
        assert key not in DiscordApprovalDecider._REGISTRY
        assert key not in DiscordApprovalDecider._NONCES

    @pytest.mark.asyncio
    async def test_refuse_undelivered_cannot_overwrite_a_real_decision(self) -> None:
        """A press that already landed outranks a later delivery verdict."""
        key = DiscordApprovalDecider.key("sk", "r12")
        nonce = DiscordApprovalDecider.register_nonce(key)
        assert DiscordApprovalDecider.resolve_global(key, True, nonce=nonce) is True
        DiscordApprovalDecider.refuse_undelivered(key)
        decider = DiscordApprovalDecider(session_key="sk")
        assert await finished(decider(SimpleNamespace(request_id="r12")), "the decision") is True

    @pytest.mark.asyncio
    async def test_no_press_still_denies_when_the_window_elapses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # flake-ok: the test lets a 10 ms production timeout expire on purpose; moved verbatim
        monkeypatch.setattr(discord_renderer, "_APPROVAL_TIMEOUT_S", 0.01)
        cli = _SendCapture()
        key = DiscordApprovalDecider.key("sk", "r10")
        await _approval_renderer(cli).on_prompt_choice([], "r10", tool_title="shell")
        decider = DiscordApprovalDecider(session_key="sk")
        assert await decider(SimpleNamespace(request_id="r10")) is False
        assert key not in DiscordApprovalDecider._REGISTRY
        assert key not in DiscordApprovalDecider._NONCES


@pytest.mark.usefixtures("_clean_approval_registry")
class TestTurnTeardown:
    """A reservation must not outlive the turn that opened it."""

    @pytest.mark.asyncio
    async def test_discard_session_closes_a_window_no_wait_ever_adopted(self) -> None:
        """The prompt went out, then the turn ended before the decider."""
        cli = _SendCapture()
        key = DiscordApprovalDecider.key("sk", "r13")
        await _approval_renderer(cli).on_prompt_choice([], "r13", tool_title="shell")
        nonce = _nonce_from(cli.sent[0][2])
        DiscordApprovalDecider.discard_session("sk")
        assert key not in DiscordApprovalDecider._REGISTRY
        assert key not in DiscordApprovalDecider._NONCES
        # The buttons left in the channel resolve nothing.
        assert DiscordApprovalDecider.resolve_global(key, True, nonce=nonce) is False

    @pytest.mark.asyncio
    async def test_discard_session_does_not_reach_a_sibling_key(self) -> None:
        """``sk`` must not match ``sk2``: the prefix carries its own separator."""
        mine = DiscordApprovalDecider.key("sk", "r14")
        other = DiscordApprovalDecider.key("sk2", "r14")
        DiscordApprovalDecider.register_nonce(mine)
        DiscordApprovalDecider.register_nonce(other)
        DiscordApprovalDecider.discard_session("sk")
        assert mine not in DiscordApprovalDecider._REGISTRY
        assert other in DiscordApprovalDecider._REGISTRY

    @pytest.mark.asyncio
    async def test_discard_session_keeps_a_decision_already_recorded(self) -> None:
        """A press that landed is a delivered decision, not a stale reservation."""
        key = DiscordApprovalDecider.key("sk", "r15")
        nonce = DiscordApprovalDecider.register_nonce(key)
        assert DiscordApprovalDecider.resolve_global(key, True, nonce=nonce) is True
        DiscordApprovalDecider.discard_session("sk")
        decider = DiscordApprovalDecider(session_key="sk")
        assert await finished(decider(SimpleNamespace(request_id="r15")), "the decision") is True


class TestRedactionNotice:
    """When redaction rewrote what landed, one follow-up notice says so.

    Discord edits its answer in place and rotates segments, so the tally counts
    each LANDED message's final state and the notice goes out once, at the end
    of ``on_done``. The shared wording is pinned in
    ``test_credential_redaction_notice.py``.
    """

    _SECRET_URI = "postgresql://user:SuperSecret123@db.example.com:5432/prod"

    @pytest.mark.asyncio
    async def test_redacted_answer_is_followed_by_one_notice(self) -> None:
        cli = FakeClient()
        r = DiscordRenderer(cli, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
        await r.on_text_chunk(f"Run: psql {self._SECRET_URI}")
        await r.on_done()

        texts = [t for t, _c in cli.sent] + [t for _i, t, _c in cli.edits]
        assert not any("SuperSecret123" in t for t in texts)
        notices = [t for t, _c in cli.sent if "Security notice" in t]
        assert len(notices) == 1
        assert "SuperSecret123" not in notices[0]

    @pytest.mark.asyncio
    async def test_clean_answer_sends_no_notice(self) -> None:
        cli = FakeClient()
        r = DiscordRenderer(cli, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
        await r.on_text_chunk("All green, deploy finished.")
        await r.on_done()

        assert not any("Security notice" in t for t, _c in cli.sent)

    @pytest.mark.asyncio
    async def test_notice_send_failure_does_not_fail_a_delivered_turn(self) -> None:
        cli = FakeClient()
        real_send = cli.send_message

        async def send_but_fail_the_notice(channel_id, text, **kw):
            if "Security notice" in text:
                raise RuntimeError("discord down after the answer")
            return await real_send(channel_id, text, **kw)

        cli.send_message = send_but_fail_the_notice  # type: ignore[method-assign]
        r = DiscordRenderer(cli, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
        await r.on_text_chunk(f"Run: psql {self._SECRET_URI}")
        await r.on_done()  # must not raise
        # The answer itself landed.
        assert any("[REDACTED: credential]" in t for t, _c in cli.sent) or any(
            "[REDACTED: credential]" in t for _i, t, _c in cli.edits
        )


class TestRotationSeamCredentialSafety:
    """A rotation must not hand the reader a key by putting two frames in a row.

    The length cut lands on the RAW buffer and every frame is redacted ALONE, so a
    credential the model wrote with markup across the cut matches nothing in either
    frame -- and the reader's client renders the markup away and reads the halves
    as one key, one message under the other.

    Every shape here is asserted on the SCREEN: the frames the fake client actually
    received, read the two ways a reader can produce (canonicalise the copied join,
    and canonicalise each frame then read them in order). That is the same pair
    ``joins_to_a_credential`` grades, but stated over the whole delivered sequence
    and using only the redactor and the canonicaliser, so it holds whatever the
    renderer did to get there.
    """

    def _renderer(
        self, monkeypatch: pytest.MonkeyPatch, limit: int
    ) -> tuple[DiscordRenderer, FakeClient]:
        cli = FakeClient()
        r = DiscordRenderer(cli, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
        monkeypatch.setattr(r, "_limit", lambda: limit)
        monkeypatch.setattr("kiro_crew.discord.renderer._EDIT_THROTTLE_S", 1e9)
        return r, cli

    #: A cut inside an unbroken run of non-space characters is a HARD cut at the
    #: budget, which is what puts the boundary inside the credential rather than at
    #: some paragraph break the splitter would have preferred.
    _LIMIT = 200

    def _straddling_source(self, head: str, tail: str) -> str:
        """One long word whose character at offset ``_LIMIT`` splits *head*/*tail*."""
        return "a" * (self._LIMIT - len(head)) + head + tail + "b" * self._LIMIT

    async def _screen(self, monkeypatch: pytest.MonkeyPatch, src: str) -> list[str]:
        """Every frame the reader ends up with, rotation then final seal."""
        r, cli = self._renderer(monkeypatch, self._LIMIT)
        r._buf = [src]
        await r._rotate_on_length()
        await r._seal_current(extract_uploads=False)
        return [text for text, _ in cli.sent]

    @pytest.mark.asyncio
    async def test_a_cut_after_trailing_space_is_graded_on_the_trimmed_form(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discord shows the reader the trimmed text, so the grade must trim too.

        This shape is MEASURED, not assumed. At this limit the splitter's own chunks
        already sever, so the rotation does consult the offset search; the offset a
        non-trimming transform returns leaves halves whose RAW forms are safe -- the
        spaces sit between them -- while their DELIVERED forms sit flush and read as one
        key. The assertion is therefore on the delivered forms: comparing the raw frames
        is what makes this hazard invisible.
        """
        head, tail = "AKIAIOSF", "ODNN7EXAMPLE"
        prefix = "word " * 20
        src = prefix + head + "   " + tail + " trailing prose here"
        r, cli = self._renderer(monkeypatch, len(prefix + head) + 3)
        r._buf = [src]

        await r._rotate_on_length()
        await r._seal_current(extract_uploads=False)

        frames = [text for text, _ in cli.sent]
        assert len(frames) >= 2, f"fixture delivered {len(frames)} frame(s), so it grades no seam"
        self._assert_no_key_on_screen([_delivered_form(f) for f in frames])

    @staticmethod
    def _assert_no_key_on_screen(frames: list[str]) -> None:
        for reading in (
            canonicalize_display("".join(frames)),
            "".join(canonicalize_display(f) for f in frames),
        ):
            assert _redact_all(reading) == reading, f"key readable across frames: {frames}"

    @pytest.mark.asyncio
    async def test_a_held_image_ref_cannot_be_re_delivered_as_the_rejected_pair(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The offset search grades the source; the rotation delivers source PLUS held.

        An image reference starting at or before the budget leaves the sealable
        source SHORTER than the budget, so the search takes its ``limit >= len(text)``
        early return and hands back the whole string. Re-assigning the frames from
        that answer reproduces the byte-identical pair the gate above just rejected,
        and the reader sees the label ``![my-secret-value](...)`` canonicalises to
        sitting straight under a dangling ``SecretAccessKey=``. So the search's answer
        is a candidate, not a verdict: the delivered pair is graded again and the
        rotation delivers nothing rather than the pair it already refused.
        """
        limit = 100
        head = "prose " * 10 + "SecretAccessKey="
        held = "![my-secret-value](/tmp/kc-13494-missing.png)"
        src = head + held
        assert len(src) > limit, "fixture does not rotate"
        assert len(head) <= limit, "fixture does not reach the search's early return"
        assert _redact_all(head) == head and _redact_all(held) == held, "fixture leaks alone"
        assert severs_a_credential(
            [head, held], _redact_all, _delivered_form
        ), "fixture is not a straddle, so the rotation never consults the search"

        r, cli = self._renderer(monkeypatch, limit)
        r._buf = [src]
        await r._rotate_on_length()

        assert cli.sent == [], "the rotation delivered the pair the gate rejected"
        assert "".join(r._buf) == src, "withheld text must ride the next rotation intact"

        await r._seal_current(extract_uploads=False)
        self._assert_no_key_on_screen([_delivered_form(t) for t, _ in cli.sent])

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("head", "tail"), CREDENTIAL_STRADDLE_SHAPES)
    async def test_a_straddled_credential_never_reaches_two_frames(
        self, monkeypatch: pytest.MonkeyPatch, head: str, tail: str
    ) -> None:
        rejoined = (
            canonicalize_display(head + tail),
            canonicalize_display(head) + canonicalize_display(tail),
        )
        assert any(_redact_all(r) != r for r in rejoined), "fixture is not a straddle"

        frames = await self._screen(monkeypatch, self._straddling_source(head, tail))
        assert frames, "nothing was delivered at all"
        self._assert_no_key_on_screen(frames)

    @pytest.mark.asyncio
    async def test_an_innocent_body_still_rotates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Control: the grading refuses boundaries, it does not stop rotating."""
        r, cli = self._renderer(monkeypatch, self._LIMIT)
        r._buf = ["word " * 200]
        await r._rotate_on_length()
        assert cli.sent, "an innocent body was withheld"

    @pytest.mark.asyncio
    async def test_no_safe_offset_withholds_the_text_instead_of_sending_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure direction: nothing safe to cut means nothing goes out.

        With no sampled offset safe, the rotation must not fall back on the cut it
        already refused. It delivers NOTHING, keeps the buffer whole, and the final
        seal then redacts that buffer as one string -- where the key is intact and
        matches, so it is replaced rather than shown.
        """
        head, tail = "AKIAIOSF", "ODNN7EXAMPLE"
        src = self._straddling_source(head, tail)
        monkeypatch.setattr("kiro_crew.discord.renderer.safe_split_offset", lambda *a, **k: 0)
        r, cli = self._renderer(monkeypatch, self._LIMIT)
        r._buf = [src]

        await r._rotate_on_length()

        assert cli.sent == [], "text went out on a cut the grading had refused"
        assert "".join(r._buf) == src, "the withheld text was not kept whole"

    @pytest.mark.asyncio
    async def test_a_presentation_fallback_retains_no_piece_over_the_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A graded cut must not buy one safe boundary with every later one.

        The presentation branch is TERMINAL: the piece it retains is parked as
        ``_delivery_text`` with no further rotation ahead of it, and the only thing
        that bounds it after that is ``_seal_current``'s own re-split -- which cuts
        on length alone and grades no boundary. So a fallback that replaced the
        splitter's bounded chunks with ``[head, whole remainder]`` moved the FIRST
        boundary to safety and handed every later one in the same text to an
        ungraded cut. The invariant is stated on the retained SIZE, which is what
        makes it hold whatever the text is.

        The sealed frames are deliberately not size-asserted: the seal redacts each
        one, and the replacement text is longer than the key it covers, so a frame
        legitimately ends up wider than the budget. That growth is the protection
        working, not a boundary escaping.
        """
        head, tail = "AKIAIOSF", "ODNN7EXAMPLE"
        # One unbroken word, so the splitter cuts hard at the budget and the first
        # cut lands inside the credential. Several budgets wide, so a single graded
        # offset would leave a remainder many times the budget.
        src = "a" * (self._LIMIT - len(head)) + head + tail + "b" * (self._LIMIT * 6)
        assert severs_a_credential(
            await asyncio.to_thread(split_markdown_safe, src, self._LIMIT),
            _redact_all,
            _delivered_form,
        ), "fixture does not sever, so it never reaches the graded fallback"

        r, cli = self._renderer(monkeypatch, self._LIMIT)
        r._delivery_text = src
        await r._rotate_on_length()

        retained = r._delivery_text or ""
        assert len(cli.sent) > 1, "one frame only, so the fallback carved nothing"
        assert len(retained) <= self._LIMIT, (
            f"a {len(retained)}-char piece is retained against a {self._LIMIT} budget; "
            "the seal's own re-split is the next cut and it grades no boundary"
        )

        await r._seal_current(extract_uploads=False)
        self._assert_no_key_on_screen([_delivered_form(t) for t, _ in cli.sent])

        await r._seal_current(extract_uploads=False)
        delivered = "".join(text for text, _ in cli.sent)
        assert head + tail not in delivered, "the final seal shipped the key"

    @pytest.mark.asyncio
    async def test_a_markup_span_covering_a_whole_piece_is_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Piece length is no defence: canonicalising DROPS a link's target.

        Three pieces, where no neighbouring PAIR reveals anything -- the link needs
        its closing bracket, which is in the third piece -- while the full join
        collapses the url to its label and puts that label against ``AKIA``. So the
        middle piece is swallowed whole, which a pairwise grade cannot see.
        """
        key_head, key_tail = "AKIA", "IOSFODNN7EXAMPLE"
        src = (
            "a" * (self._LIMIT - len(key_head))
            + key_head
            + "["
            + key_tail
            + "](http://q/"
            + "b" * self._LIMIT
            + ") rest"
        )
        frames = await self._screen(monkeypatch, src)
        assert frames, "nothing was delivered at all"
        self._assert_no_key_on_screen(frames)

    @pytest.mark.asyncio
    async def test_a_retained_buffer_holds_only_source_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whatever is kept live must be a SLICE of the source, never rejoined chunks.

        ``split_markdown_safe`` does not concatenate back to its input: a chunk is
        rstripped and a fence is closed at the seal and reopened after it. Rejoining
        chunks would glue one paragraph's last word onto the next, or invent a fence
        run the model never wrote, and that is the text the user is eventually sent.
        """
        src = (
            "a" * (self._LIMIT - 8)
            + "AKIAIOSF"
            + "ODNN7EXAMPLE"
            + "\n\npara two\n\n```py\nx = 1\n```\n\npara three "
            + "c" * self._LIMIT
        )
        r, _cli = self._renderer(monkeypatch, self._LIMIT)
        r._buf = [src]
        await r._rotate_on_length()
        retained = "".join(r._buf)
        assert retained in src, "retained buffer is not a slice of the source"

    @pytest.mark.asyncio
    async def test_a_boundary_is_graded_on_the_text_the_seal_delivers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A steering marker between the halves vanishes at the seal.

        The grade must run on the delivered form. Graded raw, the marker separates
        the halves and no credential pattern matches; delivered, the seal removes it
        and the two messages sit flush together.
        """
        r, cli = self._renderer(monkeypatch, self._LIMIT)
        r._buf = ["a" * (self._LIMIT - 8) + "AKIAIOSF" + "ODNN7EXAMPLE" + "b" * self._LIMIT]
        await r._rotate_on_length()
        await r._seal_current(extract_uploads=False)
        self._assert_no_key_on_screen([text for text, _ in cli.sent])

    @pytest.mark.asyncio
    async def test_the_seals_own_split_grades_its_boundary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The last cut before the wire is the one nothing rotates behind.

        A buffer the rotation WITHHELD as unsafe is parked whole, so it arrives at
        the seal over the platform cap. The seal splits it again -- and that cut had
        no redactor, where Telegram's own splitter call has carried one all along. So
        the gate that refused to cut handed the same text to a cut made on length
        alone, and a key the markup spans is severed there instead.

        The link target is a long run with no space in it, which is what makes the
        cut land inside the credential rather than at a break the splitter prefers.
        """
        # Narrow the generic screen to the readings that existed when this
        # regression was added. The key lives in the link target behind an
        # innocuous label, broken by a run of ``*``. Neither whole-text reading
        # sees it: canonicalising collapses the link to its label, and the literal
        # form has the run between the halves. The seal's own split grading must
        # still catch the key after each delivered frame consumes the emphasis run.
        from kiro_crew.messaging import display_safety
        from kiro_crew.messaging import split as messaging_split

        further_readings = (display_safety._plain_reading,)
        monkeypatch.setattr(display_safety, "FURTHER_READINGS", further_readings)
        monkeypatch.setattr(
            messaging_split,
            "_RENDERINGS",
            (display_safety.canonicalize_display, *further_readings),
        )
        src = (
            "a" * (DISCORD_MAX_TEXT - 100)
            + "[l](https://x/AKIA"
            + "*" * 300
            + "IOSFODNN7EXAMPLE)"
            + "b" * 400
        )
        assert len(src) > DISCORD_MAX_TEXT, "fixture does not reach the platform cap"
        assert (
            discord_renderer._redact_transformed(src) == src
        ), "the seal's own redaction already catches it"
        assert severs_a_credential(
            split_markdown_safe(src, DISCORD_MAX_TEXT), _redact_all, _delivered_form
        ), "an ungraded split of this fixture no longer severs"

        r, cli = self._renderer(monkeypatch, self._LIMIT)
        r._buf = [src]
        await r._seal_current(extract_uploads=False)

        frames = [text for text, _ in cli.sent]
        assert len(frames) >= 2, f"fixture did not split at the seal: {len(frames)}"
        self._assert_no_key_on_screen(frames)


class TestTheLiveBubbleSeamSurvivesItsEditLifecycle:
    """A live bubble's seam grade must persist across its edits and its seal.

    The defect (Opus 5 B1): the fresh send grades the bubble against the message
    above it and records the DELIVERED FRAME into ``_sent_tail`` (so the next fresh
    message grades against what the reader last saw). That record erased the message
    above, so a later EDIT of the same bubble -- and the final seal, which edits it
    in place -- rewrote the bubble with UNGRADED text, restoring the credential span
    the fresh-send grade had repaired and delivering the key across the predecessor
    and the bubble.

    Fixed by keeping the bubble's own predecessor in ``_bubble_above`` (which
    ``_record_sent`` does not touch) and grading the edit branch, the seal, and the
    fallback send against it. Every assertion is on the SCREEN -- the frames the
    fake client received, read the two ways a reader can produce them.
    """

    @staticmethod
    def _assert_no_key_on_screen(frames: list[str]) -> None:
        for reading in (
            canonicalize_display("".join(frames)),
            "".join(canonicalize_display(f) for f in frames),
        ):
            assert _redact_all(reading) == reading, f"key readable across frames: {frames}"

    def _renderer(self, monkeypatch: pytest.MonkeyPatch) -> tuple[DiscordRenderer, FakeClient]:
        cli = FakeClient()
        r = DiscordRenderer(
            cli, "chan1", DISCORD_CAPABILITIES, session_key="sk", show_thinking=True  # type: ignore[arg-type]
        )
        # No throttle: every chunk becomes its own live frame so the bubble is
        # edited more than once, which is exactly where the ungraded rewrite lived.
        monkeypatch.setattr("kiro_crew.discord.renderer._EDIT_THROTTLE_S", 0.0)
        return r, cli

    @staticmethod
    def _screen(cli: FakeClient) -> list[str]:
        """Every message's FINAL delivered form (the last edit wins per bubble)."""
        final: dict[str, str] = {}
        order: list[str] = []
        mid = 100
        for text, _ in cli.sent:
            mid += 1
            key = str(mid)
            order.append(key)
            final[key] = text
        for edit_mid, text, _ in cli.edits:
            final[edit_mid] = text
        return [final[k] for k in order]

    @pytest.mark.asyncio
    async def test_an_edit_of_the_open_bubble_is_graded_against_the_message_above(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The core B1 case: a note ends in a prefix, the answer bubble completes it.

        The reasoning note ends ``...AKIA`` and the answer streamed into the fresh
        bubble below it begins ``IOSFODNN7EXAMPLE...``; the client renders the note's
        subtext marker away, so note-tail + bubble-head read as one key across two
        messages. The first answer frame is graded (fresh send), but the SECOND
        chunk EDITS the same bubble -- and before the fix that edit shipped ungraded
        text, restoring the key. It must now be graded against ``_bubble_above``.
        """
        r, cli = self._renderer(monkeypatch)
        await r.on_turn_start()
        # Reasoning note whose tail is a credential PREFIX (matches nothing alone).
        await r.on_thinking("here is the reasoning and the key is AKIA")
        await r._flush_thinking()
        # The note is now a delivered message; a fresh answer lands below it.
        await r.on_text_chunk("IOSFODNN7EXAMPLE")
        # A SECOND chunk edits that same open bubble -- the path B1 left ungraded.
        await r.on_text_chunk(" and some more answer text after the key")
        await r.on_done()

        frames = self._screen(cli)
        assert any("💭" in f for f in frames), "the reasoning note must be delivered"
        assert len(frames) >= 2, f"fixture delivered {len(frames)} frame(s); it grades no seam"
        self._assert_no_key_on_screen([_delivered_form(f) for f in frames])

    @pytest.mark.asyncio
    async def test_the_final_seal_edit_does_not_restore_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``on_done``'s seal edits the open bubble in place -- graded, not raw.

        The seal reaches ``_land_sealed`` with ``_stream_mid`` set, so it EDITS the
        bubble. That edit must be graded against the bubble's predecessor, or the
        final frame the reader keeps can restore the credential.
        """
        r, cli = self._renderer(monkeypatch)
        await r.on_turn_start()
        await r.on_thinking("reasoning ending in AKIA")
        await r._flush_thinking()
        # A single answer chunk: fresh send opens the bubble, then on_done seals it
        # by EDITING that same bubble -- the seal-edit path.
        await r.on_text_chunk("IOSFODNN7EXAMPLE is the completing half")
        await r.on_done()

        frames = self._screen(cli)
        self._assert_no_key_on_screen([_delivered_form(f) for f in frames])

    @pytest.mark.asyncio
    async def test_the_record_still_lets_the_next_fresh_message_grade(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fix must not break the ORIGINAL contract ``_record_sent`` serves.

        ``_bubble_above`` holds the bubble's predecessor; ``_sent_tail`` must still
        record the delivered FRAME so a NEXT fresh message (a new bubble opened
        after this one seals) grades against what the reader last saw. Here the
        bubble's own frame ends in a prefix and a second turn's fresh message
        completes it -- that cross-bubble seam is what ``_sent_tail`` guards.
        """
        r, cli = self._renderer(monkeypatch)
        await r.on_turn_start()
        await r.on_text_chunk("the answer ends in AKIA")
        await r.on_done()
        # Record must have captured the delivered frame (not stayed empty), so the
        # next fresh message has a real predecessor to grade against.
        assert r._sent_tail, "the delivered frame must be recorded for the next seam"
        assert "AKIA" in r._sent_tail or _redact_all(r._sent_tail) != r._sent_tail

    @pytest.mark.asyncio
    async def test_a_transient_edit_failure_grades_the_fallback_against_sent_tail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed seal-edit falls to a fresh POST below the STILL-VISIBLE bubble.

        The defect (GPT F1 on the B1 fix): ``edit_message_with_files`` returns False
        for ANY failure, so a transient 429/5xx is indistinguishable from a deleted
        message -- but on a transient failure the bubble is STILL ON SCREEN, so the
        fallback POST lands directly below that visible frame. Grading it against
        ``_bubble_above`` (the message ABOVE the bubble) would leave the seam to the
        visible frame ungraded, and a frame ending ``AKIA`` + a fallback beginning
        ``IOSFODNN7EXAMPLE`` would deliver the key across the two. The fallback must
        grade against ``_sent_tail`` -- the frame the reader actually sees.
        """
        r, cli = self._renderer(monkeypatch)
        await r.on_turn_start()
        # Fresh send opens a bubble whose delivered frame ends in a credential PREFIX.
        await r.on_text_chunk("the streamed answer so far AKIA")
        assert r._stream_mid is not None, "a live bubble must be open"
        # The seal will EDIT that open bubble; force a TRANSIENT edit failure so it
        # falls through to a fresh POST while the bubble stays visible. The POST
        # completes the key.
        r._buf = ["IOSFODNN7EXAMPLE and the rest of the finalized answer"]
        r._delivery_text = None

        async def _edit_fails(*_a: Any, **_k: Any) -> bool:
            return False

        monkeypatch.setattr(r._client, "edit_message_with_files", _edit_fails)
        await r._seal_current(extract_uploads=False)

        frames = self._screen(cli)
        assert len(frames) >= 2, f"fixture delivered {len(frames)} frame(s); it grades no seam"
        self._assert_no_key_on_screen([_delivered_form(f) for f in frames])

    @pytest.mark.asyncio
    async def test_a_successful_live_edit_records_the_frame_it_put_on_screen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live EDIT is what the reader sees, so it must become ``_sent_tail``.

        The defect (GPT F1 on the F1 fix): ``_stream_live``'s edit branch updated
        only ``_shown``, never ``_sent_tail``, so after a live edit ``_sent_tail``
        still held the bubble's FIRST frame. A later failed seal-edit falls to a
        fresh POST graded against ``_pending_note_tail or _sent_tail`` -- and if
        ``_sent_tail`` is the stale first frame (ending in a credential prefix)
        rather than the edited frame on screen, the POST completing that prefix
        would seat the key across two bubbles. Recording the edited frame fixes it.

        Read directly off ``_sent_tail``: after a live edit it must equal the
        edited frame, not the opening one.
        """
        r, cli = self._renderer(monkeypatch)
        await r.on_turn_start()
        # Fresh send opens the bubble (frame 1).
        await r.on_text_chunk("frame one opening text")
        first_frame = r._sent_tail
        assert first_frame, "the fresh send must record the opening frame"
        # A second chunk EDITS the same open bubble (no throttle) -> frame 2.
        await r.on_text_chunk(" and now considerably more streamed answer text")
        assert r._stream_mid is not None, "the bubble must still be open (an edit, not a send)"
        # _sent_tail must now be the EDITED frame the reader sees, not frame 1.
        assert r._sent_tail != first_frame, (
            "the successful live edit did not record what it put on screen -- a later "
            "failed-seal fallback would grade against the stale opening frame"
        )
        assert r._sent_tail == r._shown, "the recorded frame must be the one shown"

    @pytest.mark.asyncio
    async def test_a_failed_live_edit_does_not_record_the_unsent_frame(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live edit that FAILS must not poison ``_sent_tail`` with an unsent frame.

        The defect (GPT F1 on the F6 fix): the edit branch recorded
        ``_shown``/``_sent_tail`` UNCONDITIONALLY, but ``edit_message`` returns False
        for any failure (a transient 429/5xx included). Recording a frame no reader
        ever saw makes it the graded predecessor of a later fallback POST, which
        could then seat a credential across two bubbles. ``_record_sent``'s own
        contract is "called only after a send or edit reports success" -- so on a
        failed edit ``_sent_tail`` must stay on the frame still on screen.
        """
        r, cli = self._renderer(monkeypatch)
        await r.on_turn_start()
        # Fresh send opens the bubble (recorded frame 1 = what is on screen).
        await r.on_text_chunk("frame one opening text")
        on_screen_frame = r._sent_tail
        assert on_screen_frame, "the fresh send must record the opening frame"

        # The next chunk EDITS the bubble, but the edit FAILS.
        async def _edit_fails(*_a: Any, **_k: Any) -> bool:
            return False

        monkeypatch.setattr(r._client, "edit_message", _edit_fails)
        await r.on_text_chunk(" a second chunk whose edit will fail to land")
        # _sent_tail must NOT have advanced to the unsent (failed) frame.
        assert r._sent_tail == on_screen_frame, (
            "a failed live edit recorded an unsent frame as _sent_tail -- a later "
            "fallback POST would grade against a message no reader ever saw"
        )

    @pytest.mark.asyncio
    async def test_a_deleted_bubble_grades_the_fallback_against_the_visible_neighbour(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A moderator-deleted bubble: the fallback POST lands below _bubble_above.

        ``edit_message`` returns False for BOTH a transient failure and a DELETED
        message; when a moderator/automod
        deletes the live bubble, the on-screen neighbour is ``_bubble_above`` (the
        message that was above it), NOT the deleted ``_sent_tail``. Grading against
        the deleted frame leaves the seam to the visible neighbour ungraded and can
        reconstruct a credential across the two visible messages. The classified
        EDIT_GONE outcome routes the fallback to ``_bubble_above``.

        Driven through ``_land_sealed`` directly: ``_sent_tail`` is set to an
        INNOCUOUS deleted frame and ``_bubble_above`` to the visible neighbour ending
        in a credential PREFIX, so grading against the wrong one (the deleted
        ``_sent_tail``) leaves the fallback text's leading key half unrepaired.
        """
        r, cli = self._renderer(monkeypatch)
        await r.on_turn_start()
        r._stream_mid = "99"  # a live bubble exists...
        r._bubble_above = "the visible neighbour above ends in AKIA"  # ...below THIS
        r._record_sent("an innocuous deleted-bubble frame")  # _sent_tail = deleted frame
        r._pending_note_tail = ""
        cli.edit_gone = True  # the edit finds the bubble DELETED (404)
        # The fallback POST completes the key begun in the visible neighbour.
        await r._land_sealed("IOSFODNN7EXAMPLE completes the credential", [], None)

        frames = [t for t, _ in cli.sent] + [t for _m, t, _c in cli.edits]
        # The visible neighbour is what the reader sees above the POST; read them
        # together the way the grader must have accounted for.
        neighbour = "the visible neighbour above ends in AKIA"
        self._assert_no_key_on_screen(
            [_delivered_form(neighbour)] + [_delivered_form(f) for f in frames]
        )
