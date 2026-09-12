"""One Slack renderer for both ingress kinds, and images that flow both ways.

A session with a Slack attachment has ONE Slack output path: the
``SlackRenderer`` the transport dispatcher drives for a Slack-born turn is the
same object the dashboard turn loop drives for a dashboard-born turn on a linked
session. These tests pin that single path and the two image legs:

* outbound -- the dashboard's uploads directory is an approved upload root
  beside the session cwd, so a pasted or agent-drawn picture travels to the
  thread; the echo and the link-time history seed carry pictures too;
* inbound -- a picture posted in Slack is promoted into that same uploads
  directory and recorded in the transcript as ``![image](path)``, so the
  dashboard renders it after the turn instead of showing a dead temp path.

The dashboard-ingress turn tests drive the real ``_run_chat`` with a scripted
provider and a recording Slack client, and check every item a linked thread
receives from a turn -- echo, status, stream, task cards, reply, OPTIONS control,
approval prompt, teardown -- is provided by the renderer.
"""

from __future__ import annotations

import inspect
import os
import stat
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew import uploads
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    AcpEvent,
)
from kiro_crew.dashboard import chat_runner, chat_slack
from kiro_crew.messaging.attachments import (
    Attachment,
    IngestResult,
    append_attachment_context,
    ingest_attachments,
)
from kiro_crew.messaging.outbound_files import REASON_SENSITIVE, REASON_SYMLINK, extract_local_refs
from kiro_crew.slack.renderer import (
    HISTORY_AGENT_ICON,
    HISTORY_USER_ICON,
    USER_ECHO_PREFIX,
    SlackRenderer,
    _approved_roots,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00picture-pixels"
KEY = "AKIAIOSFODNN7EXAMPLE"


@pytest.fixture(autouse=True)
def _quiet_sel():
    """No audit file is written by a renderer test."""
    fake = MagicMock()
    with patch("kiro_crew.slack.renderer.sel", return_value=fake):
        with patch("kiro_crew.messaging.attachments.sel", return_value=fake):
            yield fake


@pytest.fixture
def uploads_dir(tmp_path, monkeypatch) -> Path:
    """Pin the shared uploads directory to a per-test path."""
    target = tmp_path / "uploads"
    monkeypatch.setattr(uploads, "_UPLOAD_DIR", target)
    return target


class _RecSlack:
    """Recording ``SlackClientOps`` stand-in for everything the renderer calls."""

    def __init__(self, *, streaming: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.uploads: list[dict[str, Any]] = []
        self.streaming = streaming
        self.fail_post = False
        self._n = 0

    def _ts(self) -> str:
        self._n += 1
        return f"ts-{self._n}"

    async def start_stream(self, channel, thread_ts, **kw):
        self.calls.append(("start_stream", {"thread_ts": thread_ts, "user_id": kw.get("user_id")}))
        return self._ts() if self.streaming else None

    async def append_stream(self, channel, ts, text):
        self.calls.append(("append_stream", {"text": text}))
        return True

    async def stop_stream(self, channel, ts, final_text=None):
        self.calls.append(("stop_stream", {"ts": ts, "final_text": final_text}))
        return True

    async def append_task(self, channel, ts, task_id, title, status, **kw):
        self.calls.append(("append_task", {"task_id": task_id, "title": title, "status": status}))
        return True

    async def post_message(self, channel, text, thread_ts=None, **kw):
        if self.fail_post:
            raise RuntimeError("slack down")
        self.calls.append(("post_message", {"text": text, "thread_ts": thread_ts}))
        return self._ts()

    async def update_message(self, channel, ts, text="", blocks=None):
        self.calls.append(("update_message", {"text": text}))

    async def post_blocks(self, channel, blocks, text, thread_ts=None, **kw):
        self.calls.append(("post_blocks", {"blocks": blocks, "text": text}))
        return self._ts()

    async def set_thread_status(self, channel, thread_ts, status):
        self.calls.append(("set_thread_status", {"status": status}))

    async def upload_file(self, channel, thread_ts, file, filename, title):
        with open(file, "rb") as fh:
            data = fh.read()
        self.uploads.append(
            {"filename": filename, "title": title, "data": data, "thread": thread_ts}
        )

    # -- assertion helpers --
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def of(self, name: str) -> list[dict[str, Any]]:
        return [kw for n, kw in self.calls if n == name]

    def shown(self) -> str:
        return "\n".join(str(kw.get("text") or kw.get("final_text") or "") for _, kw in self.calls)


def _renderer(slack: _RecSlack, cwd: str | None = None, **kw) -> SlackRenderer:
    renderer = SlackRenderer(slack, "C1", "t1", reactions_enabled=False, show_thinking=False, **kw)
    if cwd is not None:
        renderer.authorize_upload_root(cwd)
    return renderer


# ---------------------------------------------------------------------------
# 1. The extractor accepts several roots and pins the read to the matching one
# ---------------------------------------------------------------------------


class TestMultiRootExtraction:
    def test_a_file_in_the_uploads_root_is_extracted(self, tmp_path) -> None:
        cwd, up = tmp_path / "cwd", tmp_path / "uploads"
        cwd.mkdir(), up.mkdir()
        pic = up / "abc_shot.png"
        pic.write_bytes(PNG)

        result = extract_local_refs(f"look ![shot]({pic})", within_root=[str(cwd), str(up)])

        assert [f.path for f in result.files] == [str(pic)]
        assert result.rejections == []
        assert "![shot]" not in result.rewritten_text

    def test_a_file_in_the_cwd_root_is_extracted(self, tmp_path) -> None:
        cwd, up = tmp_path / "cwd", tmp_path / "uploads"
        cwd.mkdir(), up.mkdir()
        pic = cwd / "chart.png"
        pic.write_bytes(PNG)

        result = extract_local_refs(f"![c]({pic})", within_root=[str(cwd), str(up)])

        assert [f.path for f in result.files] == [str(pic)]

    def test_a_sensitive_path_is_refused_under_any_root(self, tmp_path, monkeypatch) -> None:
        # ~/.aws sits under neither root, and is on the denylist besides.
        cwd, up = tmp_path / "cwd", tmp_path / "uploads"
        cwd.mkdir(), up.mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))
        aws = tmp_path / ".aws"
        aws.mkdir()
        creds = aws / "credentials.png"
        creds.write_bytes(PNG)

        result = extract_local_refs(f"![x]({creds})", within_root=[str(cwd), str(up)])

        assert result.files == []
        assert [r.reason for r in result.rejections] == [REASON_SENSITIVE]
        assert str(creds) in result.rewritten_text  # the markup stays visible

    def test_a_root_outside_both_trees_is_refused(self, tmp_path) -> None:
        cwd, up, elsewhere = tmp_path / "cwd", tmp_path / "uploads", tmp_path / "elsewhere"
        cwd.mkdir(), up.mkdir(), elsewhere.mkdir()
        pic = elsewhere / "p.png"
        pic.write_bytes(PNG)

        result = extract_local_refs(f"![x]({pic})", within_root=[str(cwd), str(up)])

        assert result.files == []
        assert [r.reason for r in result.rejections] == [REASON_SENSITIVE]

    @pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
    def test_a_symlink_into_the_uploads_root_is_refused(self, tmp_path) -> None:
        # Planted in the cwd, pointing into uploads: the leaf-symlink refusal
        # catches it, and even a link the leaf check missed would fail the read
        # gate, which pins the opened descriptor to the root the NAME matched.
        cwd, up = tmp_path / "cwd", tmp_path / "uploads"
        cwd.mkdir(), up.mkdir()
        real = up / "real.png"
        real.write_bytes(PNG)
        link = cwd / "link.png"
        link.symlink_to(real)

        result = extract_local_refs(f"![x]({link})", within_root=[str(cwd), str(up)])

        assert result.files == []
        assert [r.reason for r in result.rejections] == [REASON_SYMLINK]

    @pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
    def test_a_linked_directory_cannot_launder_one_root_through_another(self, tmp_path) -> None:
        # The leaf is a regular file but an ANCESTOR under cwd is a link into
        # uploads. The name matches the cwd root, the descriptor resolves under
        # uploads, so the read pinned to cwd refuses it -- the union of two roots
        # must not admit what neither root would alone.
        cwd, up = tmp_path / "cwd", tmp_path / "uploads"
        cwd.mkdir(), up.mkdir()
        (up / "inner").mkdir()
        real = up / "inner" / "pic.png"
        real.write_bytes(PNG)
        (cwd / "alias").symlink_to(up / "inner", target_is_directory=True)
        via_link = cwd / "alias" / "pic.png"

        result = extract_local_refs(f"![x]({via_link})", within_root=[str(cwd), str(up)])

        assert result.files == []
        assert len(result.rejections) == 1

    def test_a_single_string_root_still_works(self, tmp_path) -> None:
        pic = tmp_path / "p.png"
        pic.write_bytes(PNG)

        result = extract_local_refs(f"![x]({pic})", within_root=str(tmp_path))

        assert [f.path for f in result.files] == [str(pic)]

    def test_an_empty_root_list_refuses_everything(self, tmp_path) -> None:
        pic = tmp_path / "p.png"
        pic.write_bytes(PNG)

        result = extract_local_refs(f"![x]({pic})", within_root=[])

        assert result.files == []
        assert [r.reason for r in result.rejections] == [REASON_SENSITIVE]


class TestApprovedRoots:
    def test_the_uploads_dir_rides_beside_a_valid_cwd(self, uploads_dir, tmp_path) -> None:
        assert _approved_roots(str(tmp_path)) == (str(tmp_path), str(uploads_dir))

    def test_an_invalid_cwd_authorizes_nothing_not_even_uploads(self, uploads_dir) -> None:
        assert _approved_roots("") == ()
        assert _approved_roots("relative/dir") == ()

    @pytest.mark.asyncio
    async def test_an_agent_picture_in_the_uploads_dir_reaches_the_thread(
        self, uploads_dir, tmp_path
    ) -> None:
        # The dashboard-pasted case: the dashboard composer wrote the
        # picture under uploads/, the transcript names that path, and the reply
        # references it. With uploads/ an approved root, it travels.
        uploads_dir.mkdir()
        pic = uploads_dir / "0123abcd_pasted.png"
        pic.write_bytes(PNG)
        slack = _RecSlack(streaming=False)
        renderer = _renderer(slack, cwd=str(tmp_path / "project"))

        await renderer.on_text_chunk(f"as you pasted:\n\n![image]({pic})\n")
        await renderer.on_done(stop_reason="end_turn")

        assert [u["data"] for u in slack.uploads] == [PNG]
        assert str(pic) not in slack.shown()


# ---------------------------------------------------------------------------
# 2. The two entry points a Slack-born turn never needed
# ---------------------------------------------------------------------------


class TestEchoUserMessage:
    @pytest.mark.asyncio
    async def test_the_echo_is_italic_behind_a_speech_balloon(self) -> None:
        slack = _RecSlack()
        renderer = _renderer(slack)

        await renderer.echo_user_message("what does this do?")

        assert slack.of("post_message") == [
            {"text": f"{USER_ECHO_PREFIX} _what does this do?_", "thread_ts": "t1"}
        ]

    @pytest.mark.asyncio
    async def test_redaction_runs_over_the_full_text_before_the_cut(self) -> None:
        # The credential sits past the 500-character cut. Cutting first would
        # split it into unmatchable fragments; redacting first catches it whole
        # and the echo is then cut to length.
        slack = _RecSlack()
        renderer = _renderer(slack)
        text = "x" * 490 + " " + KEY

        await renderer.echo_user_message(text)

        shown = slack.of("post_message")[0]["text"]
        assert KEY not in shown
        assert len(shown) <= len(f"{USER_ECHO_PREFIX} __") + 500

    @pytest.mark.asyncio
    async def test_a_pasted_picture_is_uploaded_after_the_echo(self, uploads_dir, tmp_path) -> None:
        uploads_dir.mkdir()
        pic = uploads_dir / "0123abcd_shot.png"
        pic.write_bytes(PNG)
        slack = _RecSlack()
        renderer = _renderer(slack, cwd=str(tmp_path))

        await renderer.echo_user_message(f"why is this red?\n![image]({pic})")

        assert slack.of("post_message")[0]["text"] == f"{USER_ECHO_PREFIX} _why is this red?_"
        assert [u["data"] for u in slack.uploads] == [PNG]
        assert str(pic) not in slack.shown()

    @pytest.mark.asyncio
    async def test_extraction_precedes_the_length_cut(self, uploads_dir, tmp_path) -> None:
        # The image reference sits past the 500-char echo cut. Cutting first
        # would bisect the markup and lose the picture; extracting first sees it
        # whole, so the picture is uploaded and the text is cut afterwards.
        uploads_dir.mkdir()
        pic = uploads_dir / "0123abcd_late.png"
        pic.write_bytes(PNG)
        slack = _RecSlack()
        renderer = _renderer(slack, cwd=str(tmp_path))

        await renderer.echo_user_message("y" * 600 + f"\n![image]({pic})")

        assert [u["data"] for u in slack.uploads] == [PNG]
        assert "![image]" not in slack.shown()

    @pytest.mark.asyncio
    async def test_no_roots_means_the_path_stays_prose_and_nothing_ships(self, tmp_path) -> None:
        pic = tmp_path / "shot.png"
        pic.write_bytes(PNG)
        slack = _RecSlack()
        renderer = _renderer(slack)  # no authorize_upload_root

        await renderer.echo_user_message(f"see ![image]({pic})")

        assert slack.uploads == []
        assert str(pic) in slack.shown()

    @pytest.mark.asyncio
    async def test_a_slack_refusal_never_raises(self) -> None:
        slack = _RecSlack()
        slack.fail_post = True
        renderer = _renderer(slack)

        await renderer.echo_user_message("hello")  # must not raise


class TestPostHistoryRow:
    @pytest.mark.asyncio
    async def test_roles_get_their_icons(self) -> None:
        slack = _RecSlack()
        renderer = _renderer(slack)

        assert await renderer.post_history_row("user", "hi") is True
        assert await renderer.post_history_row("assistant", "hello") is True

        texts = [kw["text"] for kw in slack.of("post_message")]
        assert texts == [f"{HISTORY_USER_ICON} hi", f"{HISTORY_AGENT_ICON} hello"]

    @pytest.mark.asyncio
    async def test_a_row_with_a_picture_uploads_it_after_the_text(
        self, uploads_dir, tmp_path
    ) -> None:
        uploads_dir.mkdir()
        pic = uploads_dir / "0123abcd_shot.png"
        pic.write_bytes(PNG)
        slack = _RecSlack()
        renderer = _renderer(slack, cwd=str(tmp_path))

        await renderer.post_history_row("user", f"this one\n![image]({pic})")

        assert slack.of("post_message")[0]["text"] == f"{HISTORY_USER_ICON} this one"
        assert [u["data"] for u in slack.uploads] == [PNG]

    @pytest.mark.asyncio
    async def test_a_credential_in_history_is_redacted(self) -> None:
        slack = _RecSlack()
        renderer = _renderer(slack)

        await renderer.post_history_row("assistant", f"your key is {KEY}")

        assert KEY not in slack.shown()

    @pytest.mark.asyncio
    async def test_a_failed_post_reports_false(self) -> None:
        slack = _RecSlack()
        slack.fail_post = True
        renderer = _renderer(slack)

        assert await renderer.post_history_row("user", "hi") is False


class TestTeardownAndApprovalGuard:
    @pytest.mark.asyncio
    async def test_close_finishes_an_unfinished_turns_surface(self) -> None:
        # A turn that never reached on_done: the open card is completed, the
        # stream stopped and the status cleared, so the thread reads as ended.
        slack = _RecSlack()
        renderer = _renderer(slack)
        await renderer.on_turn_start()
        await renderer.on_tool_call("tc1", "Running: shell", tool_kind="execute")

        await renderer.close()

        statuses = [kw["status"] for kw in slack.of("append_task")]
        assert statuses == ["in_progress", "complete"]
        assert len(slack.of("stop_stream")) == 1
        assert slack.of("set_thread_status")[-1]["status"] == ""

    @pytest.mark.asyncio
    async def test_close_after_on_done_touches_nothing(self) -> None:
        slack = _RecSlack()
        renderer = _renderer(slack)
        await renderer.on_text_chunk("done")
        await renderer.on_done(stop_reason="end_turn")
        before = list(slack.calls)

        await renderer.close()

        assert slack.calls == before

    @pytest.mark.asyncio
    async def test_no_decider_means_no_approval_card(self) -> None:
        slack = _RecSlack()
        renderer = _renderer(slack, decider=None)

        await renderer.on_prompt_choice(
            [{"id": "allow", "label": "Allow"}], "rq1", tool_title="shell"
        )

        assert slack.of("post_blocks") == []


# ---------------------------------------------------------------------------
# 3. A dashboard-ingress turn on a linked session drives the renderer
# ---------------------------------------------------------------------------


def _harness(tmp_path, uploads_dir, *, events, streaming=True):
    state = _make_state(tmp_path)
    client = MagicMock()
    client.shutdown = AsyncMock()
    client.approve_tool = AsyncMock()
    client.reject_tool = AsyncMock()
    cwd = tmp_path / "project"
    cwd.mkdir(exist_ok=True)
    client.cwd = str(cwd)

    async def _stream(msg):
        for ev in events:
            yield ev

    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.record_failure = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.is_slack_paused = MagicMock(return_value=False)
    state.sessions.get_session_for_thread = MagicMock(return_value=None)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    state.owner_id = "U_OWNER"
    slack = _RecSlack(streaming=streaming)
    state.slack_client = slack
    slot = state.get_or_create_slot("linked-slot")
    slot.append("user", "hello", "msg msg-u")
    state.link_slack(slot.key, "111.222", "C123")
    return state, slot, slack, client, cwd


def _events(*extra: AcpEvent) -> list[AcpEvent]:
    return [
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="first part "),
        AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id="tc1",
            title="Running: shell",
            tool_name="shell",
            tool_kind="execute",
        ),
        *extra,
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="final answer [OPTIONS: Yes | No]"),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]


class TestDashboardIngressDrivesTheRenderer:
    @pytest.mark.asyncio
    async def test_the_thread_gets_echo_status_stream_cards_reply_and_control(
        self, tmp_path, uploads_dir
    ) -> None:
        state, slot, slack, _client, _cwd = _harness(tmp_path, uploads_dir, events=_events())
        recorded: list[Any] = []
        with patch.object(
            chat_runner, "remember_slack_options", lambda s, k, p: recorded.append(p)
        ):
            await chat_runner._run_chat(state, slot, "typed in dashboard")

        # The user echo, redaction path and all.
        assert slack.of("post_message")[0]["text"] == f"{USER_ECHO_PREFIX} _typed in dashboard_"
        # The in-flight indicator is the renderer's thread status.
        assert slack.of("set_thread_status")[0]["status"] == "is working on your request"
        # Streaming with a recipient, so chat.startStream does not demote.
        assert slack.of("start_stream")[0]["user_id"] == "U_OWNER"
        # Per-tool task cards, opened then completed.
        assert [kw["status"] for kw in slack.of("append_task")] == ["in_progress", "complete"]
        # EVERY text segment streams -- the pre-tool text too, which a final-
        # segment-only mirror would drop -- and the OPTIONS markup never reaches
        # the stream.
        streamed = "".join(kw["text"] for kw in slack.of("append_stream"))
        assert "first part" in streamed and "final answer" in streamed
        assert "[OPTIONS" not in slack.shown()
        assert len(slack.of("stop_stream")) == 1
        # The OPTIONS control rides the footer and its ts is retained.
        assert len(recorded) == 1
        assert recorded[0].choices == ("Yes", "No")
        assert recorded[0].ts
        # The status is cleared at the end.
        assert slack.of("set_thread_status")[-1]["status"] == ""

    @pytest.mark.asyncio
    async def test_a_credential_split_across_chunks_never_reaches_the_stream(
        self, tmp_path, uploads_dir
    ) -> None:
        # Slack renders consecutive appends as one run, so two halves that each
        # pass a per-chunk scan would spell the key whole in the thread. The
        # mirror feeds the renderer only the rolling redactor's confirmed-safe
        # prefix, exactly as the transport driver does for a Slack-born turn.
        head, tail = KEY[:9], KEY[9:]
        events = [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text=f"your key is {head}"),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text=f"{tail} and that is all"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=events)

        await chat_runner._run_chat(state, slot, "show me")

        streamed = "".join(kw["text"] for kw in slack.of("append_stream"))
        assert KEY not in streamed and KEY not in slack.shown()
        assert "and that is all" in streamed

    @pytest.mark.asyncio
    async def test_a_synthetic_turn_is_not_echoed_but_still_answered(
        self, tmp_path, uploads_dir
    ) -> None:
        from kiro_crew.dashboard.chat_utils import _CONN_RECOVER_MSG

        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=_events())

        await chat_runner._run_chat(state, slot, _CONN_RECOVER_MSG, _synthetic_payload=True)

        assert not any(USER_ECHO_PREFIX in kw["text"] for kw in slack.of("post_message"))
        assert "final answer" in slack.shown()

    @pytest.mark.asyncio
    async def test_a_slash_turn_reaches_slack_not_at_all(self, tmp_path, uploads_dir) -> None:
        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=_events())

        await chat_runner._run_chat(state, slot, "/context")

        assert slack.calls == []

    @pytest.mark.asyncio
    async def test_a_slack_born_message_is_not_echoed_back(self, tmp_path, uploads_dir) -> None:
        # The message arrived from the thread (its files with it), so the mirror
        # must not repeat it there; the answer still goes to the thread.
        uploads_dir.mkdir()
        pic = uploads_dir / "0123abcd_from_slack.png"
        pic.write_bytes(PNG)
        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=_events())

        await chat_runner._run_chat(
            state,
            slot,
            f"from slack\n![image]({pic})",
            _directive_user_origin=True,
            _ingress="slack",
        )

        assert not any(USER_ECHO_PREFIX in kw["text"] for kw in slack.of("post_message"))
        assert slack.uploads == [], "a Slack-born picture must not be re-uploaded to Slack"
        assert "final answer" in slack.shown()

    @pytest.mark.asyncio
    async def test_a_dashboard_pasted_picture_travels_with_the_echo(
        self, tmp_path, uploads_dir
    ) -> None:
        uploads_dir.mkdir()
        pic = uploads_dir / "0123abcd_pasted.png"
        pic.write_bytes(PNG)
        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=_events())

        await chat_runner._run_chat(state, slot, f"why red?\n![image]({pic})")

        assert slack.of("post_message")[0]["text"] == f"{USER_ECHO_PREFIX} _why red?_"
        assert [u["data"] for u in slack.uploads] == [PNG]

    @pytest.mark.asyncio
    async def test_an_agent_drawn_chart_travels_with_the_reply(self, tmp_path, uploads_dir) -> None:
        state, slot, slack, _c, cwd = _harness(tmp_path, uploads_dir, events=[])
        chart = cwd / "chart.png"
        chart.write_bytes(PNG)
        events = [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text=f"here:\n\n![chart]({chart})\n"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]

        async def _stream(msg):
            for ev in events:
                yield ev

        client = state.sessions.get_or_create.return_value[0]
        client.stream = _stream

        await chat_runner._run_chat(state, slot, "draw it")

        assert [u["data"] for u in slack.uploads] == [PNG]
        assert str(chart) not in slack.shown()

    @pytest.mark.asyncio
    async def test_thinking_follows_slack_show_thinking(self, tmp_path, uploads_dir) -> None:
        # Reasoning lands ABOVE the answer: the renderer posts it once, when the
        # first answer text arrives, so it has to precede that text.
        events = [AcpEvent(kind=EVENT_THINKING_CHUNK, text="let me reason"), *_events()]
        for enabled in (True, False):
            state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=events)
            cfg = MagicMock()
            cfg.slack.show_thinking = enabled
            with patch("kiro_crew.dashboard.slack_mirror.KiroCrewConfig.load", return_value=cfg):
                await chat_runner._run_chat(state, slot, "think")
            posted = any("💭" in kw["text"] for kw in slack.of("post_message"))
            assert posted is enabled, f"show_thinking={enabled}: thinking posted={posted}"

    @pytest.mark.asyncio
    async def test_a_paused_mirror_silences_everything(self, tmp_path, uploads_dir) -> None:
        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=_events())
        state.sessions.is_slack_paused = MagicMock(return_value=True)

        await chat_runner._run_chat(state, slot, "typed in dashboard")

        assert slack.calls == []

    @pytest.mark.asyncio
    async def test_a_crash_mid_tool_still_tears_the_stream_down(
        self, tmp_path, uploads_dir
    ) -> None:
        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=[])

        async def _stream(msg):
            yield AcpEvent(
                kind=EVENT_TOOL_CALL, tool_call_id="tc1", title="Running: shell", tool_name="shell"
            )
            raise RuntimeError("provider died")

        client = state.sessions.get_or_create.return_value[0]
        client.stream = _stream

        await chat_runner._run_chat(state, slot, "typed in dashboard")

        assert [kw["status"] for kw in slack.of("append_task")] == ["in_progress", "complete"]
        assert len(slack.of("stop_stream")) == 1
        assert slack.of("set_thread_status")[-1]["status"] == ""

    @pytest.mark.asyncio
    async def test_the_approval_prompt_is_posted_once_through_the_dashboards_own_path(
        self, tmp_path, uploads_dir
    ) -> None:
        events = [
            AcpEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="rq1",
                title="shell",
                options=[{"id": "allow", "label": "Allow"}],
            ),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="done"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=events)
        # Delivery "fails" so the dashboard auto-declines and the turn does not
        # park on the approval window; what matters here is WHO posts the prompt.
        with patch.object(
            chat_runner, "post_linked_approval", new_callable=AsyncMock, return_value=None
        ) as linked:
            await chat_runner._run_chat(state, slot, "run it")

        linked.assert_awaited_once()
        assert not any(
            kw["text"] == "Tool approval requested" for kw in slack.of("post_blocks")
        ), "the renderer must not post an approval card of its own"


class TestTheHandRolledPathIsGone:
    def test_chat_runner_no_longer_posts_to_slack_by_hand(self) -> None:
        src = inspect.getsource(chat_runner)
        for gone in (
            'initial_text="Thinking…"',
            "render_for_slack(",
            "build_options_blocks(",
            "state.slack_client.start_stream(",
            "state.slack_client.append_task(",
            "state.slack_client.stop_stream(",
            "_mirror_stream_ts",
        ):
            assert gone not in src, f"hand-rolled Slack call still present: {gone}"
        # The renderer is the one Slack output.
        assert "open_slack_mirror(" in src
        assert "_mirror.on_text_chunk(" in src
        assert "_mirror.on_tool_call(" in src
        assert "_mirror.on_done(" in src
        assert "_mirror.close()" in src

    def test_the_backfill_no_longer_formats_rows_by_hand(self) -> None:
        src = inspect.getsource(chat_slack)
        assert "_format_backfill_parts" not in src
        assert "post_history_row(" in src


# ---------------------------------------------------------------------------
# 4. Inbound: a Slack picture lands where the dashboard renders from
# ---------------------------------------------------------------------------


class TestInboundSlackImagePersists:
    @pytest.mark.asyncio
    async def test_the_image_is_promoted_into_uploads_with_the_dashboards_naming(
        self, uploads_dir
    ) -> None:
        async def _download(url, dest):
            Path(dest).write_bytes(PNG)

        result = await ingest_attachments(
            [Attachment(name="Screenshot 2026-09-12 at 09.38.png", mimetype="image/png", url="u")],
            download=_download,
            source="slack",
            persist_images=True,
        )

        assert len(result.image_paths) == 1
        path = Path(result.image_paths[0])
        assert path.parent == uploads_dir
        assert path.exists() and path.read_bytes() == PNG
        # <uuid hex>_<sanitized name>: 32 hex chars, an underscore, the name with
        # every unsafe character replaced.
        prefix, _, name = path.name.partition("_")
        assert len(prefix) == 32 and all(c in "0123456789abcdef" for c in prefix)
        assert name == "Screenshot_2026-09-12_at_09.38.png"
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        # Persisted: the caller must NOT delete it.
        assert result.persisted_paths == [str(path)]
        assert result.temp_paths == []

    @pytest.mark.asyncio
    async def test_the_suffix_follows_the_sniffed_type(self, uploads_dir) -> None:
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 16

        async def _download(url, dest):
            Path(dest).write_bytes(jpeg)

        result = await ingest_attachments(
            [Attachment(name="photo.png", mimetype="image/png", url="u")],
            download=_download,
            source="slack",
            persist_images=True,
        )

        assert Path(result.image_paths[0]).name.endswith("_photo.jpg")

    @pytest.mark.asyncio
    async def test_the_row_carries_the_dashboards_markdown_form(self, uploads_dir) -> None:
        async def _download(url, dest):
            Path(dest).write_bytes(PNG)

        result = await ingest_attachments(
            [Attachment(name="shot.png", mimetype="image/png", url="u")],
            download=_download,
            source="slack",
            persist_images=True,
        )

        row = append_attachment_context("look at this", result)

        assert row == f"look at this\n![image]({result.image_paths[0]})"

    @pytest.mark.asyncio
    async def test_the_prompt_encoder_still_finds_the_path_inside_the_markdown(
        self, uploads_dir
    ) -> None:
        from kiro_crew.acp.prompt_blocks import build_prompt_blocks

        pil_image = pytest.importorskip("PIL.Image")
        uploads_dir.mkdir()
        pic = uploads_dir / "0123abcd_shot.png"
        pil_image.new("RGB", (2, 2), (255, 0, 0)).save(pic, format="PNG")
        row = append_attachment_context(
            "look", IngestResult(image_paths=[str(pic)], persisted_paths=[str(pic)])
        )

        blocks = build_prompt_blocks(row, max_image_bytes=1 << 20)

        assert any(b.get("type") == "image" for b in blocks), blocks

    @pytest.mark.asyncio
    async def test_an_opaque_file_keeps_the_temp_behaviour(self, uploads_dir) -> None:
        async def _download(url, dest):
            Path(dest).write_bytes(b"PK\x03\x04zip-bytes")

        result = await ingest_attachments(
            [Attachment(name="bundle.zip", mimetype="application/zip", url="u")],
            download=_download,
            source="slack",
            persist_images=True,
        )

        assert len(result.file_paths) == 1
        assert not Path(result.file_paths[0]).is_relative_to(uploads_dir)
        assert result.persisted_paths == []
        assert result.temp_paths == result.file_paths
        assert result.file_paths[0] in append_attachment_context("", result)
        os.unlink(result.file_paths[0])

    @pytest.mark.asyncio
    async def test_without_the_flag_images_stay_temp(self, uploads_dir) -> None:
        async def _download(url, dest):
            Path(dest).write_bytes(PNG)

        result = await ingest_attachments(
            [Attachment(name="shot.png", mimetype="image/png", url="u")],
            download=_download,
            source="discord",
        )

        assert not Path(result.image_paths[0]).is_relative_to(uploads_dir)
        assert result.temp_paths == result.image_paths
        assert append_attachment_context("", result) == result.image_paths[0]
        os.unlink(result.image_paths[0])

    @pytest.mark.asyncio
    async def test_process_slack_files_opts_into_persistence(self, uploads_dir) -> None:
        from kiro_crew.slack.files import process_slack_files

        orch = MagicMock()
        orch.slack = AsyncMock()

        async def _download(url, dest):
            Path(dest).write_bytes(PNG)

        orch.slack.download_file = AsyncMock(side_effect=_download)

        result = await process_slack_files(
            orch,
            [
                {
                    "mimetype": "image/png",
                    "url_private_download": "u",
                    "filetype": "png",
                    "name": "a.png",
                }
            ],
        )

        assert isinstance(result, IngestResult)
        assert result.persisted_paths == result.image_paths
        assert Path(result.image_paths[0]).parent == uploads_dir


# ---------------------------------------------------------------------------
# 5. The link-time backfill uploads pictures
# ---------------------------------------------------------------------------


class TestBackfillUploadsImages:
    @pytest.mark.asyncio
    async def test_a_history_row_with_a_picture_seeds_the_picture(
        self, tmp_path, uploads_dir, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.chat_backfill import BackfillSelection

        uploads_dir.mkdir()
        pic = uploads_dir / "0123abcd_shot.png"
        pic.write_bytes(PNG)
        state = _make_state(tmp_path)
        state.owner_id = "U_OWNER"
        slack = _RecSlack()
        state.slack_client = slack
        slot = state.get_or_create_slot("s1")
        slot.project = str(tmp_path)
        rows = [
            {"role": "user", "content": f"this one\n![image]({pic})"},
            {"role": "assistant", "content": "I see a red button."},
        ]
        slot.messages.extend(rows)
        monkeypatch.setattr(
            chat_slack,
            "select_backfill_messages",
            lambda _s, _sl: BackfillSelection(first_turn=[], recent=[rows], skipped_turns=0),
        )
        state.link_slack(slot.key, "thread-1", "C-1")

        await chat_slack.drain_slack_backfill(state, slot, "C-1", "thread-1")

        texts = [kw["text"] for kw in slack.of("post_message")]
        assert texts == [
            f"{HISTORY_USER_ICON} this one",
            f"{HISTORY_AGENT_ICON} I see a red button.",
        ]
        assert [u["data"] for u in slack.uploads] == [PNG]
        assert str(pic) not in slack.shown()
