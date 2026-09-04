"""Slack catches up with Discord on three fronts, so parity runs both ways.

Each class pins one of them, and each is a defect Slack shipped rather than a
refinement:

* ``TestOutboundUploads``: an agent that writes ``![chart](/tmp/chart.png)``
  used to ship the raw path to Slack as text while ``files_outbound=True``
  claimed the renderer extracted and uploaded it. The flag is what the capability
  ledger defines as ENFORCED, so these tests exercise the gate from both sides.
* ``TestFenceSafeSplitting``: the renderer's final no-stream render truncated an
  over-limit answer through ``_safe_update``, and everything it did split, Slack's
  own fence-blind ``split_message`` could cut inside a code fence.
* ``TestVoiceMemoRejections``: a voice memo whose transcription was unavailable
  or failed was dropped in TOTAL silence: nothing reached the prompt, so a
  voice-only message never started a turn, and the sender's successful send was
  indistinguishable from being ignored.
"""

from __future__ import annotations

import asyncio
import os
import stat
import threading
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import ACTIVATION_ALWAYS, KiroCrewConfig, MessagingConfig
from kiro_crew.messaging.outbound_files import ExtractLimits, OutboundFile, Rejection
from kiro_crew.messaging.split import split_markdown_safe
from kiro_crew.slack import events as ev
from kiro_crew.slack.files import (
    SLACK_MAX_TOTAL_UPLOAD_BYTES,
    SLACK_MAX_UPLOAD_FILE_BYTES,
    SLACK_MAX_UPLOAD_FILES,
    UPLOAD_LIMITS,
    VOICE_MEMO_FAILED,
    VOICE_MEMO_UNAVAILABLE,
    is_voice_memo,
    upload_outbound_files,
)
from kiro_crew.slack.format import SLACK_MSG_LIMIT, TRUNCATION_NOTICE
from kiro_crew.slack.handler import _THINKING
from kiro_crew.slack.renderer import SlackRenderer
from kiro_crew.slack.transport import SLACK_CAPABILITIES

#: A one-pixel PNG: real leading bytes, so the extractor's magic-byte allowlist
#: accepts it the way it would accept a chart the agent rendered.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00chart-pixels"


@pytest.fixture(autouse=True)
def _quiet_sel():
    """No audit file is written by a renderer or events test."""
    fake = MagicMock()
    with patch("kiro_crew.slack.renderer.sel", return_value=fake):
        with patch("kiro_crew.slack.events.sel", return_value=fake):
            yield fake


class _FakeSlack:
    """Recording ``SlackClientOps`` stand-in for the renderer's outbound calls."""

    def __init__(self, *, streaming: bool = True) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.uploads: list[dict[str, Any]] = []
        self.streaming = streaming
        self.upload_raises = False
        self._n = 0

    def _ts(self) -> str:
        self._n += 1
        return f"ts-{self._n}"

    # -- streaming surface --
    async def start_stream(self, channel, thread_ts, **kw) -> str | None:
        self.calls.append(("start_stream", {}))
        return self._ts() if self.streaming else None

    async def append_stream(self, channel, ts, text) -> bool:
        self.calls.append(("append_stream", {"text": text}))
        return True

    async def stop_stream(self, channel, ts, final_text=None) -> bool:
        self.calls.append(("stop_stream", {"final_text": final_text}))
        return True

    async def append_task(self, channel, ts, task_id, title, status, **kw) -> bool:
        self.calls.append(("append_task", {"title": title}))
        return True

    # -- message surface --
    async def post_message(self, channel, text, thread_ts=None, **kw) -> str:
        self.calls.append(("post_message", {"text": text}))
        return self._ts()

    async def update_message(self, channel, ts, text="", blocks=None) -> None:
        self.calls.append(("update_message", {"text": text}))

    async def post_blocks(self, channel, blocks, text, thread_ts=None, **kw) -> str:
        self.calls.append(("post_blocks", {"text": text}))
        return self._ts()

    async def set_thread_status(self, channel, thread_ts, status) -> None:
        self.calls.append(("set_thread_status", {"status": status}))

    async def upload_file(self, channel, thread_ts, file, filename, title) -> None:
        # Read through the path Slack was handed, exactly as files_upload_v2 does.
        with open(file, "rb") as fh:
            data = fh.read()
        self.uploads.append(
            {
                "channel": channel,
                "thread_ts": thread_ts,
                "path": file,
                "dir_mode": stat.S_IMODE(os.stat(os.path.dirname(file)).st_mode),
                "filename": filename,
                "title": title,
                "data": data,
            }
        )
        if self.upload_raises:
            raise RuntimeError("slack refused the upload")

    # -- assertion helpers --
    def texts(self) -> list[str]:
        """Every string this client was asked to show the user, in order."""
        return [kw.get("text") or "" for _m, kw in self.calls if "text" in kw]

    def shown(self) -> str:
        return "\n".join(self.texts())


class _StepClock:
    """Monotonic clock that jumps past the edit throttle on every read."""

    def __init__(self, step: float = 2.0) -> None:
        self._t = 0.0
        self._step = step

    def __call__(self) -> float:
        self._t += self._step
        return self._t


def _renderer(slack: _FakeSlack, tmp_path=None, **kw) -> SlackRenderer:
    """A renderer with reactions off and (optionally) uploads authorized."""
    renderer = SlackRenderer(
        slack,
        "C1",
        "t1",
        reactions_enabled=False,
        show_thinking=False,
        now=_StepClock(),
        **kw,
    )
    if tmp_path is not None:
        renderer.authorize_upload_root(str(tmp_path))
    return renderer


async def _turn(renderer: SlackRenderer, *chunks: str) -> None:
    for chunk in chunks:
        await renderer.on_text_chunk(chunk)
    await renderer.on_done(stop_reason="end_turn")


# ---------------------------------------------------------------------------
# 1 + 2. files_outbound is now the capability the ledger says it is
# ---------------------------------------------------------------------------


class TestOutboundUploads:
    def test_the_declared_budgets_are_slack_s_own(self) -> None:
        # Fed in as budgets, so an oversize file is refused BY THE READ and keeps
        # its markup, rather than being uploaded and rejected by Slack.
        assert UPLOAD_LIMITS.max_file_bytes == SLACK_MAX_UPLOAD_FILE_BYTES
        assert UPLOAD_LIMITS.max_files == SLACK_MAX_UPLOAD_FILES
        assert UPLOAD_LIMITS.max_total_bytes == SLACK_MAX_TOTAL_UPLOAD_BYTES

    @pytest.mark.asyncio
    async def test_an_image_reference_becomes_an_upload(self, tmp_path) -> None:
        chart = tmp_path / "chart.png"
        chart.write_bytes(PNG)
        slack = _FakeSlack(streaming=False)
        renderer = _renderer(slack, tmp_path)

        await _turn(renderer, f"Here is the chart:\n\n![revenue]({chart})\n")

        assert len(slack.uploads) == 1, slack.calls
        assert slack.uploads[0]["data"] == PNG
        assert slack.uploads[0]["thread_ts"] == "t1"
        # The picture travels; the path does not survive as prose.
        assert str(chart) not in slack.shown()
        assert "![revenue]" not in slack.shown()
        assert "Here is the chart:" in slack.shown()

    @pytest.mark.asyncio
    async def test_the_upload_carries_the_validated_bytes_not_the_path(self, tmp_path) -> None:
        # The gates ran against ONE inode's bytes. If the transport re-opened the
        # path, anything able to write that directory between extraction and
        # upload would substitute what gets sent -- so the bytes it hands Slack
        # must be OutboundFile.data even when the path now holds something else.
        swapped = tmp_path / "chart.png"
        swapped.write_bytes(b"\x89PNG\r\n\x1a\nsomething-else-entirely")
        slack = _FakeSlack()
        file = OutboundFile(path=str(swapped), data=PNG, alt="chart", mime="image/png")

        rejections = await upload_outbound_files(slack, "C1", "t1", [file])

        assert rejections == []
        assert slack.uploads[0]["data"] == PNG
        assert slack.uploads[0]["path"] != str(swapped)

    @pytest.mark.asyncio
    async def test_the_staged_copy_is_owner_only_and_removed(self, tmp_path) -> None:
        slack = _FakeSlack()
        file = OutboundFile(path="/tmp/chart.png", data=PNG, alt="", mime="image/png")

        await upload_outbound_files(slack, "C1", "t1", [file])

        staged = slack.uploads[0]["path"]
        assert not os.path.exists(staged), "the staged bytes outlived the upload"
        if os.name == "posix":
            assert slack.uploads[0]["dir_mode"] == 0o700

    @pytest.mark.asyncio
    async def test_the_filename_comes_from_the_sniffed_type(self, tmp_path) -> None:
        # An extension proves nothing: the file below is a PNG called .jpg, and
        # what Slack is told must be what the leading bytes say.
        lying = tmp_path / "chart.jpg"
        lying.write_bytes(PNG)
        slack = _FakeSlack(streaming=False)
        renderer = _renderer(slack, tmp_path)

        await _turn(renderer, f"![c]({lying})")

        assert slack.uploads[0]["filename"] == "chart.png"

    @pytest.mark.asyncio
    async def test_files_outbound_false_keeps_printing_the_path(self, tmp_path) -> None:
        # The ledger's contract: a channel declaring False degrades honestly to
        # the markdown path, and never silently drops the picture.
        chart = tmp_path / "chart.png"
        chart.write_bytes(PNG)
        slack = _FakeSlack(streaming=False)
        renderer = _renderer(
            slack, tmp_path, capabilities=replace(SLACK_CAPABILITIES, files_outbound=False)
        )

        await _turn(renderer, f"chart: ![c]({chart})")

        assert slack.uploads == []
        assert str(chart) in slack.shown()

    @pytest.mark.asyncio
    async def test_a_restricted_session_ships_no_bytes(self, tmp_path) -> None:
        # A session the user expected to leave no trace must not push its bytes
        # into a channel every member can read.
        chart = tmp_path / "chart.png"
        chart.write_bytes(PNG)
        slack = _FakeSlack(streaming=False)
        renderer = _renderer(slack, tmp_path, uploads_allowed=False)

        await _turn(renderer, f"chart: ![c]({chart})")

        assert slack.uploads == []
        assert str(chart) in slack.shown()

    @pytest.mark.asyncio
    async def test_an_unauthorized_root_ships_no_bytes(self, tmp_path) -> None:
        chart = tmp_path / "chart.png"
        chart.write_bytes(PNG)
        slack = _FakeSlack(streaming=False)
        # No authorize_upload_root call: "anywhere" is not an approved root.
        renderer = _renderer(slack)

        await _turn(renderer, f"chart: ![c]({chart})")

        assert slack.uploads == []
        assert str(chart) in slack.shown()

    @pytest.mark.asyncio
    async def test_a_reference_outside_the_root_is_refused_visibly(self, tmp_path) -> None:
        outside = tmp_path / "outside.png"
        outside.write_bytes(PNG)
        root = tmp_path / "cwd"
        root.mkdir()
        slack = _FakeSlack(streaming=False)
        renderer = _renderer(slack, root)

        await _turn(renderer, f"see ![c]({outside})")

        shown = slack.shown()
        assert slack.uploads == []
        # Refused, and the refusal says which reference and why.
        assert str(outside) in shown
        assert "not sent" in shown

    @pytest.mark.asyncio
    async def test_an_oversize_file_is_refused_visibly(self, tmp_path) -> None:
        chart = tmp_path / "chart.png"
        chart.write_bytes(PNG)
        slack = _FakeSlack(streaming=False)
        renderer = _renderer(slack, tmp_path)
        tiny = ExtractLimits(max_files=4, max_total_bytes=1024, max_file_bytes=4)

        with patch("kiro_crew.slack.renderer.UPLOAD_LIMITS", tiny):
            await _turn(renderer, f"see ![c]({chart})")

        shown = slack.shown()
        assert slack.uploads == []
        assert "per-file limit" in shown
        assert str(chart) in shown, "a refused reference keeps its markup"

    @pytest.mark.asyncio
    async def test_a_failed_upload_is_reported(self, tmp_path) -> None:
        chart = tmp_path / "chart.png"
        chart.write_bytes(PNG)
        slack = _FakeSlack(streaming=False)
        slack.upload_raises = True
        renderer = _renderer(slack, tmp_path)

        await _turn(renderer, f"see ![c]({chart})")

        # The reference was already cut out of the text, so an unreported failure
        # would be a reply about a picture with neither picture nor reason.
        shown = slack.shown()
        assert "not sent" in shown
        assert str(chart) in shown

    @pytest.mark.asyncio
    async def test_extraction_happens_at_the_seal_not_at_a_length_cut(self, tmp_path) -> None:
        # The seal sees the reference whole, in its original fence context. A
        # length cut that ran extraction could bisect `![alt](path)` and lose the
        # attachment, so it never does.
        chart = tmp_path / "chart.png"
        chart.write_bytes(PNG)
        slack = _FakeSlack(streaming=False)
        renderer = _renderer(slack, tmp_path)
        long_prose = "\n".join(f"line {i} of the analysis" * 3 for i in range(220))

        await _turn(renderer, f"{long_prose}\n\n![c]({chart})\n\nand that is the trend.")

        texts = slack.texts()
        assert len(slack.uploads) == 1, slack.calls
        assert all("![c](" not in t and str(chart) not in t for t in texts), texts
        assert any("and that is the trend." in t for t in texts), texts

    @pytest.mark.asyncio
    async def test_the_append_only_stream_never_shows_the_path(self, tmp_path) -> None:
        # Slack streams by APPENDING, so markup that lands there stays in the
        # transcript beside the picture. It is withheld instead, and the withheld
        # tail is released at the seal.
        chart = tmp_path / "chart.png"
        chart.write_bytes(PNG)
        slack = _FakeSlack(streaming=True)
        renderer = _renderer(slack, tmp_path)

        await _turn(renderer, "Here it is: ", f"![c]({chart})", " and more after it.")

        appended = [kw["text"] for m, kw in slack.calls if m == "append_stream"]
        assert appended, slack.calls
        assert all("![c](" not in t and str(chart) not in t for t in appended), appended
        assert "Here it is:" in "".join(appended)
        assert "and more after it." in "".join(appended), "the withheld tail was lost"
        assert len(slack.uploads) == 1

    @pytest.mark.asyncio
    async def test_a_live_frame_never_shows_the_path(self, tmp_path) -> None:
        chart = tmp_path / "chart.png"
        chart.write_bytes(PNG)
        slack = _FakeSlack(streaming=False)
        renderer = _renderer(slack, tmp_path)

        await renderer.on_text_chunk(f"chart: ![c]({chart}) done")
        frames = [kw["text"] for m, kw in slack.calls if m == "update_message"]

        assert frames, slack.calls
        assert all(str(chart) not in t for t in frames), frames

    @pytest.mark.asyncio
    async def test_a_fenced_reference_is_documentation_not_an_upload(self, tmp_path) -> None:
        chart = tmp_path / "chart.png"
        chart.write_bytes(PNG)
        slack = _FakeSlack(streaming=False)
        renderer = _renderer(slack, tmp_path)

        await _turn(renderer, f"like this:\n\n```\n![c]({chart})\n```\n")

        assert slack.uploads == []
        assert str(chart) in slack.shown(), "fenced markup is content and stays verbatim"


# ---------------------------------------------------------------------------
# 3. the renderer splits with the shared fence-safe splitter
# ---------------------------------------------------------------------------


def _fenced_answer(lines: int = 400) -> str:
    body = "\n".join(f"    value_{i} = compute({i})" for i in range(lines))
    return f"intro paragraph\n\n```python\n{body}\n```\n\ntrailing paragraph"


class TestFenceSafeSplitting:
    @pytest.mark.asyncio
    async def test_the_whole_answer_survives_the_message_limit(self) -> None:
        slack = _FakeSlack(streaming=False)
        renderer = _renderer(slack)
        answer = _fenced_answer()
        assert len(answer) > SLACK_MSG_LIMIT

        await _turn(renderer, answer)

        texts = slack.texts()
        assert not any(TRUNCATION_NOTICE in t for t in texts), "the tail was truncated away"
        assert any("intro paragraph" in t for t in texts), texts
        assert any("trailing paragraph" in t for t in texts), texts
        assert any("value_399" in t for t in texts), "the last code line never arrived"

    @pytest.mark.asyncio
    async def test_no_chunk_exceeds_what_slack_accepts(self) -> None:
        slack = _FakeSlack(streaming=False)
        renderer = _renderer(slack)

        await _turn(renderer, _fenced_answer())

        for text in slack.texts():
            assert len(text) <= SLACK_MSG_LIMIT, len(text)

    @pytest.mark.asyncio
    async def test_a_cut_inside_a_fence_is_sealed_and_reopened(self) -> None:
        slack = _FakeSlack(streaming=False)
        renderer = _renderer(slack)

        await _turn(renderer, _fenced_answer())

        rendered = [t for t in slack.texts() if "value_" in t]
        assert len(rendered) > 1, "the answer was not split at all"
        for text in rendered:
            # A sealed chunk carries a synthetic closer, and the next reopens the
            # original opener line -- so every chunk's fences balance. The
            # backtick-counting splitter cut here and left one hanging.
            assert text.count("```") % 2 == 0, text[:200]
        # The opener line, info string included, is reopened rather than lost.
        assert sum(t.count("```python") for t in rendered) > 1, rendered[0][:200]

    @pytest.mark.asyncio
    async def test_an_unbreakable_line_is_bounded_again(self) -> None:
        # The splitter documents one over-limit case: a line that admits no cut
        # clean on both sides is placed WHOLE and carries its fence scaffolding
        # (a 300-character reopener, the newline, the synthetic closer) on top of
        # the limit. A caller owes that case an answer, because Slack's update
        # path would truncate the tail INCLUDING the closer and say nothing.
        fence = "~" * 300  # a long opener, so the scaffolding outgrows the headroom
        run = "`" * 3700  # every candidate cut lands inside a backtick run
        answer = f"intro\n\n{fence}\n" + f"{run}\n" * 3 + f"{fence}\n"
        assert max(len(c) for c in split_markdown_safe(answer, 3800)) > SLACK_MSG_LIMIT
        slack = _FakeSlack(streaming=False)
        renderer = _renderer(slack)

        await _turn(renderer, answer)

        assert slack.texts(), slack.calls
        for text in slack.texts():
            assert len(text) <= SLACK_MSG_LIMIT, len(text)
        # Bounded, not truncated: every authored character still arrives.
        assert slack.shown().count("`") >= len(run) * 3

    @pytest.mark.asyncio
    async def test_a_short_answer_still_lands_in_one_message(self) -> None:
        slack = _FakeSlack(streaming=False)
        renderer = _renderer(slack)

        await _turn(renderer, "short and sweet")

        updates = [kw["text"] for m, kw in slack.calls if m == "update_message"]
        posts = [kw["text"] for m, kw in slack.calls if m == "post_message"]
        assert updates[-1] == "short and sweet"
        # Only the fallback's own placeholder: an answer that fits gains no
        # continuation replies.
        assert posts == [_THINKING]

    @pytest.mark.asyncio
    async def test_the_splitter_runs_off_the_event_loop(self) -> None:
        # Its CPU work must not pause every other session on the gateway's one
        # loop, and a pathological delimiter run is exactly the input that costs.
        threads: list[str] = []

        def _record(text: str, limit: int, **kw) -> list[str]:
            threads.append(threading.current_thread().name)
            return split_markdown_safe(text, limit, **kw)

        slack = _FakeSlack(streaming=False)
        renderer = _renderer(slack)
        with patch("kiro_crew.slack.renderer.split_markdown_safe", _record):
            await _turn(renderer, _fenced_answer())

        assert threads, "the shared splitter was never used"
        loop_thread = threading.current_thread().name
        assert all(name != loop_thread for name in threads), threads

    @pytest.mark.asyncio
    async def test_long_reasoning_is_split_not_dropped(self) -> None:
        slack = _FakeSlack(streaming=False)
        renderer = SlackRenderer(
            slack, "C1", "t1", reactions_enabled=False, show_thinking=True, now=_StepClock()
        )

        await renderer.on_thinking("reasoning step. " * 600)
        await renderer.on_text_chunk("the answer")
        await renderer.on_done(stop_reason="end_turn")

        posts = [kw["text"] for m, kw in slack.calls if m == "post_message"]
        thinking = [t for t in posts if "reasoning step." in t]
        assert len(thinking) > 1, "an over-limit 💭 reply is rejected whole by Slack"
        assert thinking[0].startswith("💭")
        for text in thinking:
            assert len(text) <= SLACK_MSG_LIMIT, len(text)


# ---------------------------------------------------------------------------
# 4. a voice memo that produced no words is never silent
# ---------------------------------------------------------------------------


def _orch() -> MagicMock:
    orch = MagicMock()
    orch._cfg = KiroCrewConfig(
        slack_channels={},
        slack_dm_activation=ACTIVATION_ALWAYS,
        messaging=MessagingConfig(use_transport=False),
    )
    orch.slack_command = "kirocrew"
    orch._owner_id = "U_OWNER"
    orch._allowed_users = {"U_OWNER"}
    orch._tracking_channels = set()
    orch._open_channels = set()
    orch._approval_mode = ""
    orch.channel_history = MagicMock()
    orch.channel_history._user_names = {}
    orch.slack = AsyncMock()
    orch.slack.record_channel_team = MagicMock()
    orch.slack.get_user_info = AsyncMock(return_value={})
    orch.sessions = AsyncMock()
    orch.sessions.enqueue = MagicMock(return_value=False)
    orch.sessions.is_busy = MagicMock(return_value=False)
    orch.sessions.is_cancelled = MagicMock(return_value=False)
    orch.sessions.has_session = MagicMock(return_value=False)
    orch.sessions.get_session_for_thread = MagicMock(return_value=None)
    orch.sessions.dequeue = MagicMock(return_value=None)
    orch.ctx_builder = None
    orch.cron_svc = None
    orch.conv_log = None
    orch.consolidator = None
    orch.subagent_mgr = None
    orch.task_runner = None
    orch.dashboard_state = None
    orch._handler_tasks = set()
    orch._session_tasks = {}
    orch._pending_queue = {}
    return orch


def _memo(**over) -> dict:
    memo = {
        "mimetype": "audio/webm",
        "url_private_download": "https://x.invalid/memo.webm",
        "filetype": "webm",
        "name": "memo.webm",
    }
    memo.update(over)
    return memo


async def _drain(orch: MagicMock) -> None:
    for _ in range(3):
        await asyncio.sleep(0)
    tasks = list(orch._handler_tasks) + list(ev._bg_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _route_voice(
    orch: MagicMock, files: list[dict], *, available: bool, transcripts: list[str], text: str = ""
) -> AsyncMock:
    """Route one voice-bearing message and hand back the ``handle_message`` mock."""
    event = {
        "user": "U_OWNER",
        "channel": "D1",
        "text": text,
        "ts": "100.0",
        "team": "T1",
        "files": files,
    }
    with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
        with patch("kiro_crew.slack.events.stt_available", return_value=available):
            with patch(
                "kiro_crew.slack.events._transcribe_with_reaction",
                new_callable=AsyncMock,
                return_value=transcripts,
            ):
                with patch(
                    "kiro_crew.slack.events.process_slack_files",
                    new_callable=AsyncMock,
                    return_value=([], []),
                ):
                    with patch(
                        "kiro_crew.slack.events.handle_message", new_callable=AsyncMock
                    ) as handle:
                        await ev._route_message(orch, event, ev.SeenCache())
                        await _drain(orch)
    return handle


class TestVoiceMemoRejections:
    def test_one_predicate_decides_what_a_voice_memo_is(self) -> None:
        # Slack ships voice clips with a VIDEO container mimetype, and two
        # spellings of that test is how a memo gets transcribed but not reported.
        assert is_voice_memo({"mimetype": "audio/mp4"})
        assert is_voice_memo({"mimetype": "video/webm"})
        assert not is_voice_memo({"mimetype": "image/png"})
        assert not is_voice_memo({})

    @pytest.mark.asyncio
    async def test_an_unavailable_transcriber_is_surfaced(self) -> None:
        orch = _orch()

        handle = await _route_voice(orch, [_memo()], available=False, transcripts=[])

        handle.assert_awaited()  # the turn RUNS: silence was the defect
        body = handle.await_args[0][3]
        assert VOICE_MEMO_UNAVAILABLE in body

    @pytest.mark.asyncio
    async def test_a_failed_transcription_is_surfaced(self) -> None:
        orch = _orch()

        handle = await _route_voice(orch, [_memo()], available=True, transcripts=[])

        handle.assert_awaited()
        assert VOICE_MEMO_FAILED in handle.await_args[0][3]

    @pytest.mark.asyncio
    async def test_one_note_per_memo_that_produced_nothing(self) -> None:
        orch = _orch()

        handle = await _route_voice(
            orch,
            [_memo(name="a.webm"), _memo(name="b.webm"), _memo(name="c.webm")],
            available=True,
            transcripts=["only the first one"],
        )

        body = handle.await_args[0][3]
        assert "only the first one" in body
        assert body.count(VOICE_MEMO_FAILED) == 2

    @pytest.mark.asyncio
    async def test_a_transcribed_memo_gets_no_note(self) -> None:
        orch = _orch()

        handle = await _route_voice(orch, [_memo()], available=True, transcripts=["hello there"])

        body = handle.await_args[0][3]
        assert "hello there" in body
        assert VOICE_MEMO_FAILED not in body
        assert VOICE_MEMO_UNAVAILABLE not in body

    @pytest.mark.asyncio
    async def test_a_non_audio_attachment_gets_no_note(self) -> None:
        orch = _orch()

        handle = await _route_voice(
            orch,
            [{"mimetype": "image/png", "url_private": "https://x.invalid/a.png"}],
            available=False,
            transcripts=[],
            text="look at this",
        )

        body = handle.await_args[0][3]
        assert VOICE_MEMO_UNAVAILABLE not in body
        assert VOICE_MEMO_FAILED not in body

    @pytest.mark.asyncio
    async def test_the_notes_read_exactly_as_the_other_channels_do(self, monkeypatch) -> None:
        # Slack cannot call the neutral transcriber (it downloads through the bot
        # token on its own upstream path), so the wording is pinned against what
        # that function emits. Editing one copy without the other turns this red
        # instead of letting Slack and Discord describe one failure two ways.
        from kiro_crew import transcribe
        from kiro_crew.messaging.attachments import IngestResult, transcribe_audio_attachments

        monkeypatch.setattr(transcribe, "is_available", lambda *a, **k: False)
        unavailable = await transcribe_audio_attachments(
            IngestResult(audio_paths=["/tmp/memo.webm"]), "Slack"
        )
        assert unavailable.rejections == [VOICE_MEMO_UNAVAILABLE]

        async def _no_words(path: str) -> str:
            return ""

        monkeypatch.setattr(transcribe, "is_available", lambda *a, **k: True)
        monkeypatch.setattr(transcribe, "transcribe_audio", _no_words)
        failed = await transcribe_audio_attachments(
            IngestResult(audio_paths=["/tmp/memo.webm"]), "Slack"
        )
        assert failed.rejections == [VOICE_MEMO_FAILED]


class TestReleasedTailIsRedacted:
    """Cutting image markup rejoins the text around it, so the JOIN is an egress.

    The seal already redacts ``clean_text`` after extraction for exactly this
    reason -- its own comment says the join "can spell a credential neither half
    did". The withheld tail is a second egress created by the same cut: it goes
    out through ``_append_stream``, and on the streaming path it is the text the
    user ends up reading, because ``chat.stopStream`` does not replace appended
    text. A scan performed upstream of the cut cannot have seen the joined form,
    so the tail needs its own.
    """

    #: Split so NEITHER half is a credential on its own: the leading half is a
    #: bare `AKIA` and the trailing half is the remaining body. Only removing the
    #: markup between them spells the key, which is what makes this a join
    #: hazard rather than a chunk the rolling redactor should already have caught.
    _HEAD = "AKIA"
    _TAIL = "IOSFODNN7EXAMPLE"

    @pytest.mark.asyncio
    async def test_a_credential_spelled_by_the_cut_is_not_appended(self, tmp_path) -> None:
        chart = tmp_path / "chart.png"
        chart.write_bytes(PNG)
        slack = _FakeSlack(streaming=True)
        renderer = _renderer(slack, tmp_path)

        await _turn(renderer, f"key {self._HEAD}![revenue]({chart}){self._TAIL} done")

        # Concatenated with NO separator, because that is how Slack renders a run
        # of appends: as one continuous message body. Joining them on a newline
        # (what ``shown()`` does, for readability) would hide exactly the defect
        # this test exists for -- the credential is spelled BETWEEN two appends,
        # so each one is individually clean and the rendered text is not.
        streamed = "".join(kw["text"] for m, kw in slack.calls if m == "append_stream")
        assert self._HEAD + self._TAIL not in streamed, slack.calls
        assert self._HEAD + self._TAIL not in slack.shown(), slack.calls
        # The markup itself still goes, and the picture still travels: the fix is
        # a redaction, not a suppression of the release path.
        assert "![revenue]" not in slack.shown()
        assert len(slack.uploads) == 1, slack.calls

    @pytest.mark.asyncio
    async def test_the_release_path_itself_redacts_not_just_its_caller(self) -> None:
        """Pinned at the chokepoint, so a later append site inherits the scan."""
        slack = _FakeSlack(streaming=True)
        renderer = _renderer(slack)
        renderer._ref_hold = f"{self._HEAD}{self._TAIL} trailing prose"

        released = await renderer._release_refs()

        assert released
        assert self._HEAD + self._TAIL not in released
        assert "trailing prose" in released

    @pytest.mark.asyncio
    async def test_an_exfiltration_url_in_the_tail_is_redacted_too(self) -> None:
        """Both outbound redactors, in the seal's order -- URLs then credentials."""
        slack = _FakeSlack(streaming=True)
        renderer = _renderer(slack)
        renderer._ref_hold = "see https://evil.example.com/?q=AKIAIOSFODNN7EXAMPLE now"

        released = await renderer._release_refs()

        assert "AKIAIOSFODNN7EXAMPLE" not in released


class TestEveryRendererEgressMeetsTheDisplayFloor:
    """One floor at every sink in this renderer, not one scan per reviewed line.

    Neither `AKIA**<rest>**` nor `[AKIA](https://x)<rest>` matches a credential
    pattern as written, and Slack renders the markup away and shows the reader an
    intact key. A literal-only scan therefore checks one of the TWO forms that
    leave here, and the gap was found three times on three different lines --
    a rejection note, a reasoning block, a sealed body -- which is what makes the
    sink, not the line, the right place for it.
    """

    #: Emphasis markers Slack renders away; the literal string is not a credential.
    _COLLAPSING = "AKIA**IOSFODNN7EXAMPLE**"
    _KEY = "AKIAIOSFODNN7EXAMPLE"

    def _leaked(self, slack: _FakeSlack) -> bool:
        """True if any string this client was shown carries the key once rendered."""
        return any(self._KEY in text.replace("*", "") for text in slack.texts())

    @pytest.mark.asyncio
    async def test_a_sealed_reply_body(self) -> None:
        slack = _FakeSlack(streaming=True)
        renderer = _renderer(slack)
        await _turn(renderer, f"here it is {self._COLLAPSING}")
        assert not self._leaked(slack), slack.calls

    @pytest.mark.asyncio
    async def test_a_posted_thinking_block(self) -> None:
        slack = _FakeSlack(streaming=False)
        renderer = _renderer(slack)
        renderer._show_thinking = True
        await renderer.on_thinking(f"thinking about {self._COLLAPSING}")
        await renderer._maybe_post_thinking()
        assert slack.texts(), "premise: the thinking block was posted"
        assert not self._leaked(slack), slack.calls

    def test_an_upload_rejection_note(self) -> None:
        """The rejected destination came from the model, inside rendered italics."""
        renderer = _renderer(_FakeSlack(streaming=False))
        note = renderer._rejection_notes(
            [Rejection(dest=f"/tmp/{self._COLLAPSING}.png", reason="too big", detail="")]
        )
        assert self._KEY not in note.replace("*", ""), note

    @pytest.mark.asyncio
    async def test_an_appended_stream_chunk(self) -> None:
        """Appended text is FINAL on this path, so an unscanned append is permanent."""
        slack = _FakeSlack(streaming=True)
        renderer = _renderer(slack)
        renderer._stream_ts = "ts-1"
        await renderer._append_stream(f"tail {self._COLLAPSING}")
        assert not self._leaked(slack), slack.calls
