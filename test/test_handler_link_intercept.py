"""Tests for handler.py: !link-to-dashboard command and linked thread intercept."""

from __future__ import annotations

import base64
import os
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

#: Smallest valid 1x1 PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _make_slack():
    """Create a fully async-mocked Slack client."""
    slack = MagicMock()
    slack.post_message = AsyncMock()
    slack.post_blocks = AsyncMock()
    return slack


# ── !link-to-dashboard command tests ──


class TestLinkToDashboardCommand:
    """Cover handler.py lines 994-1011."""

    @pytest.mark.asyncio
    async def test_no_dashboard_state(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        with (
            patch.object(handler, "_dashboard_state", None),
            patch.object(handler, "is_allowed_user", return_value=True),
        ):
            result = await handler._handle_slash_command(
                "!link-to-dashboard",
                slack,
                MagicMock(),
                "C1",
                "t1",
                "msg1",
                "t1",
                "U1",
            )
        assert result == ""
        assert any("not available" in str(c).lower() for c in slack.post_message.call_args_list)

    @pytest.mark.asyncio
    async def test_not_in_thread(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        ds = MagicMock()
        ds.get_or_create_slot = MagicMock()
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
        ):
            result = await handler._handle_slash_command(
                "!link-to-dashboard",
                slack,
                MagicMock(),
                "C1",
                "msg1",
                "msg1",
                "msg1",
                "U1",
            )
        assert result == ""
        assert any("thread" in str(c).lower() for c in slack.post_message.call_args_list)

    @pytest.mark.asyncio
    async def test_empty_thread_returns_error(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        ds = MagicMock()
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch(
                "kiro_crew.slack.interactions._import_thread_to_slot",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await handler._handle_slash_command(
                "!link-to-dashboard",
                slack,
                MagicMock(),
                "C1",
                "t1",
                "msg1",
                "t1",
                "U1",
            )
        assert result == ""
        assert any("could not" in str(c).lower() for c in slack.post_message.call_args_list)

    @pytest.mark.asyncio
    async def test_unauthorized_user_blocked(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        with patch.object(handler, "is_allowed_user", return_value=False):
            result = await handler._handle_slash_command(
                "!link-to-dashboard",
                slack,
                MagicMock(),
                "C1",
                "t1",
                "msg1",
                "t1",
                "UBAD",
            )
        assert result == ""
        assert any("not authorized" in str(c).lower() for c in slack.post_message.call_args_list)

    @pytest.mark.asyncio
    async def test_success_emits_sel_audit(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        ds = MagicMock()
        slot = MagicMock()
        slot.key = "s1"
        slot.messages = [{"role": "user", "content": "hi"}]
        mock_sel_inst = MagicMock()
        orig_sel = handler.sel
        handler.sel = lambda: mock_sel_inst
        try:
            with (
                patch.object(handler, "_dashboard_state", ds),
                patch.object(handler, "is_allowed_user", return_value=True),
                patch(
                    "kiro_crew.slack.interactions._import_thread_to_slot",
                    new_callable=AsyncMock,
                    return_value=slot,
                ),
            ):
                result = await handler._handle_slash_command(
                    "!link-to-dashboard",
                    slack,
                    MagicMock(),
                    "C1",
                    "t1",
                    "msg1",
                    "t1",
                    "U1",
                )
        finally:
            handler.sel = orig_sel
        assert result == ""
        mock_sel_inst.log_tool_invocation.assert_called_once()
        kw = mock_sel_inst.log_tool_invocation.call_args[1]
        assert kw["tool_name"] == "link_to_dashboard"
        assert kw["outcome"] == "success"


# ── Linked thread intercept tests ──


class TestLinkedThreadIntercept:
    """Cover handler.py lines 1323-1345."""

    @pytest.mark.asyncio
    async def test_unauthorized_user_denied_with_sel(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        ds = MagicMock()
        _slot = MagicMock(key="slot1")
        type(_slot).running = PropertyMock(return_value=False)
        ds.get_linked_slot = MagicMock(return_value=_slot)
        mock_sel_inst = MagicMock()
        orig_sel = handler.sel
        handler.sel = lambda: mock_sel_inst
        try:
            with (
                patch.object(handler, "_dashboard_state", ds),
                patch.object(handler, "is_allowed_user", return_value=False),
            ):
                await handler.handle_message(
                    slack,
                    MagicMock(),
                    "C1",
                    "hello",
                    "t1",
                    "msg1",
                    "UBAD",
                )
                mock_sel_inst.log_tool_invocation.assert_called_once()
                kw = mock_sel_inst.log_tool_invocation.call_args[1]
                assert kw["outcome"] == "denied"
                assert kw["metadata"]["user_id"] == "UBAD"
        finally:
            handler.sel = orig_sel

    @pytest.mark.asyncio
    async def test_authorized_routes_to_slot_not_running(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        slot = MagicMock()
        type(slot).running = PropertyMock(return_value=False)
        slot.key = "slot1"
        slot._queue = []
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=slot)
        ds._background_tasks = set()
        ds.broadcast_ws = MagicMock()
        ds.push_slots_update = MagicMock()

        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as mock_run_chat,
        ):
            await handler.handle_message(
                slack,
                MagicMock(),
                "C1",
                "hello",
                "t1",
                "msg1",
                "U1",
            )
            slot.append.assert_called_once()
            mock_run_chat.assert_called_once()
            ds.broadcast_ws.assert_called_once()
            ds.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_redact_for_ui_original_for_llm(self):
        """Verify redacted text goes to UI (slot.append) but original goes to LLM (_run_chat)."""
        from kiro_crew.slack import handler

        slack = _make_slack()
        slot = MagicMock()
        type(slot).running = PropertyMock(return_value=False)
        slot.key = "slot1"
        slot._queue = []
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=slot)
        ds._background_tasks = set()
        ds.broadcast_ws = MagicMock()
        ds.push_slots_update = MagicMock()

        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as mock_run_chat,
            patch.object(
                handler, "redact_exfiltration_urls", return_value=("[REDACTED-URL]", True)
            ),
            patch.object(handler, "redact_credentials", return_value=("[REDACTED]", True)),
        ):
            await handler.handle_message(
                slack,
                MagicMock(),
                "C1",
                "hello http://evil.com",
                "t1",
                "msg1",
                "U1",
            )
            # UI gets redacted text — via append_and_surface, which passes
            # broadcast_user=True so the channel-typed row (never rendered
            # optimistically here) still reaches open dashboard windows through
            # append's own mid-carrying delivery.
            slot.append.assert_called_once_with(
                "user", "[REDACTED]", "msg msg-u", broadcast_user=True, meta=None
            )
            # LLM gets original text
            assert mock_run_chat.call_args[0][2] == "hello http://evil.com"

    @pytest.mark.asyncio
    async def test_authorized_queues_when_running(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        slot = MagicMock()
        type(slot).running = PropertyMock(return_value=True)
        slot.key = "slot1"
        slot._queue = []

        def queue_append(content, *, meta=None, directive_user_origin, directive_channel_origin):
            assert directive_user_origin is True
            assert directive_channel_origin is True
            # The linked-thread enqueue stamps the admission-time containment
            # snapshot so the drain can re-assert it at delivery.
            from kiro_crew.dashboard.session_control import QUEUED_CONTAINMENT_META_KEY

            assert isinstance(meta, dict) and QUEUED_CONTAINMENT_META_KEY in meta
            slot._queue.append({"id": "test", "content": content})
            return "test"

        slot.queue_append = queue_append
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=slot)
        ds.broadcast_ws = MagicMock()
        ds.push_slots_update = MagicMock()

        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as mock_run_chat,
        ):
            await handler.handle_message(
                slack,
                MagicMock(),
                "C1",
                "hello",
                "t1",
                "msg1",
                "U1",
            )
            assert len(slot._queue) == 1
            mock_run_chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_slack_image_reaches_the_linked_slots_turn(self, tmp_path, monkeypatch):
        """The structured list is the ONLY thing that puts a picture in front
        of the model (the builder never scans the appended path), so the
        linked-thread route must hand it on like a dashboard send: the raw
        paths as the provider copy, the redacted bounded list on the copy
        every observer reads -- on the immediate arm AND the queued one."""
        from kiro_crew.prompt_attachments import PromptAttachment
        from kiro_crew.slack import handler

        # Slack's temp file, as `process_slack_files` leaves it; the Slack
        # handler unlinks it in its done-callback, so the linked slot must be
        # handed a copy IT owns (an upload), never this path.
        uploads = tmp_path / "uploads"
        monkeypatch.setattr("kiro_crew.dashboard.handlers.files._UPLOAD_DIR", uploads)
        temp = tmp_path / "kc-slack" / "tmpab12.png"
        temp.parent.mkdir()
        temp.write_bytes(_PNG)
        shot = str(temp)
        atts = (PromptAttachment(path=shot, name="Screenshot 2026.png"),)

        # Immediate arm: the slot is idle, `_run_chat` gets the lists.
        slack = _make_slack()
        slot = MagicMock()
        type(slot).running = PropertyMock(return_value=False)
        slot.key = "slot1"
        slot._queue = []
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=slot)
        ds._background_tasks = set()
        ds.broadcast_ws = MagicMock()
        ds.push_slots_update = MagicMock()
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as mock_run_chat,
        ):
            await handler.handle_message(
                slack, MagicMock(), "C1", f"look\n{shot}", "t1", "msg1", "U1", attachments=atts
            )
            kwargs = mock_run_chat.call_args.kwargs
            (adopted,) = kwargs.get("_prompt_images")
            assert adopted != shot and adopted.startswith(str(uploads))
            assert adopted.endswith("_Screenshot_2026.png")
            assert kwargs.get("_attachment_meta") == {"images": [adopted]}
            assert kwargs.get("_attachments") == [adopted]
            # The Slack cleanup may run before the turn opens the file.
            temp.unlink()
            assert os.path.exists(adopted)
            with open(adopted, "rb") as fh:
                assert fh.read() == _PNG
            temp.write_bytes(_PNG)

        # Queued arm: the slot is busy, the entry carries both copies.
        busy = MagicMock()
        type(busy).running = PropertyMock(return_value=True)
        busy.key = "slot1"
        busy._queue = []
        seen: dict = {}

        def queue_append(
            content, *, meta=None, directive_user_origin, directive_channel_origin, **kw
        ):
            seen.update(meta=meta, **kw)
            busy._queue.append({"id": "q1", "content": content})
            return "q1"

        busy.queue_append = queue_append
        ds.get_linked_slot = MagicMock(return_value=busy)
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as mock_run_chat,
        ):
            await handler.handle_message(
                slack, MagicMock(), "C1", f"look\n{shot}", "t1", "msg1", "U1", attachments=atts
            )
            mock_run_chat.assert_not_called()
            (queued,) = seen.get("prompt_images")
            assert queued != shot and queued.startswith(str(uploads))
            assert seen["meta"].get("images") == [queued]


# ── Linked thread intercept on the messaging-transport path ──


class TestTransportLinkedThreadIntercept:
    """The transport path (handle_message_transport) must route linked threads
    to their dashboard slot via the shared maybe_route_linked_thread helper,
    identically to native — otherwise /kirocrew link-to-dashboard silently
    breaks under default-ON."""

    @pytest.mark.asyncio
    async def test_transport_authorized_routes_to_slot(self):
        from kiro_crew.slack import handler, transport_dispatch

        slack = _make_slack()
        slot = MagicMock()
        type(slot).running = PropertyMock(return_value=False)
        slot.key = "slot1"
        slot._queue = []
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=slot)
        ds._background_tasks = set()
        ds.broadcast_ws = MagicMock()
        ds.push_slots_update = MagicMock()
        # Booby-trap: the transport must NOT acquire a session for a linked thread.
        sessions = MagicMock()
        sessions.get_or_create = AsyncMock(side_effect=AssertionError("session acquired"))

        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as mock_run_chat,
        ):
            await transport_dispatch.handle_message_transport(
                slack,
                sessions,
                "C1",
                "hello",
                "t1",
                "msg1",
                "U1",
            )
            slot.append.assert_called_once()
            mock_run_chat.assert_called_once()
            ds.push_slots_update.assert_called_once()
            sessions.get_or_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_transport_route_hands_the_image_list_to_the_linked_slot(
        self, tmp_path, monkeypatch
    ):
        """Same route, other door: the transport path forwards the structured
        list into ``maybe_route_linked_thread`` too, or a Slack picture posted
        in a linked thread is dropped only when the gateway runs the newer
        transport."""
        from kiro_crew.prompt_attachments import image_attachments
        from kiro_crew.slack import handler, transport_dispatch

        uploads = tmp_path / "uploads"
        monkeypatch.setattr("kiro_crew.dashboard.handlers.files._UPLOAD_DIR", uploads)
        temp = tmp_path / "tmpab12.png"
        temp.write_bytes(_PNG)
        shot = str(temp)
        slack = _make_slack()
        slot = MagicMock()
        type(slot).running = PropertyMock(return_value=False)
        slot.key = "slot1"
        slot._queue = []
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=slot)
        ds._background_tasks = set()
        ds.broadcast_ws = MagicMock()
        ds.push_slots_update = MagicMock()
        sessions = MagicMock()
        sessions.get_or_create = AsyncMock(side_effect=AssertionError("session acquired"))

        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as mock_run_chat,
        ):
            await transport_dispatch.handle_message_transport(
                slack,
                sessions,
                "C1",
                f"look\n{shot}",
                "t1",
                "msg1",
                "U1",
                attachments=image_attachments([shot]),
            )
            (adopted,) = mock_run_chat.call_args.kwargs.get("_prompt_images")
            assert adopted != shot and adopted.startswith(str(uploads))
            assert mock_run_chat.call_args.kwargs.get("_attachment_meta") == {"images": [adopted]}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("change", ["unlink", "relink", "closing"])
    async def test_a_relink_during_image_adoption_refuses_instead_of_appending_to_the_old_slot(
        self, tmp_path, monkeypatch, change
    ):
        """The link target is resolved before the copy and must be resolved again
        after it: an unlink, a relink or a close that lands while the picture is
        being copied would otherwise append the send to a slot the thread no
        longer points at, or to one about to be torn down."""
        from kiro_crew.prompt_attachments import PromptAttachment
        from kiro_crew.slack import handler

        uploads = tmp_path / "uploads"
        monkeypatch.setattr("kiro_crew.dashboard.handlers.files._UPLOAD_DIR", uploads)
        temp = tmp_path / "kc-slack" / "tmpab12.png"
        temp.parent.mkdir()
        temp.write_bytes(_PNG)
        atts = (PromptAttachment(path=str(temp), name="shot.png"),)

        slack = _make_slack()
        old_slot = MagicMock()
        type(old_slot).running = PropertyMock(return_value=False)
        old_slot.key = "slot-old"
        old_slot._queue = []
        new_slot = MagicMock()
        type(new_slot).running = PropertyMock(return_value=False)
        new_slot.key = "slot-new"
        new_slot._queue = []
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=old_slot)
        ds._background_tasks = set()
        ds.broadcast_ws = MagicMock()
        ds.push_slots_update = MagicMock()
        adopt = handler.adopt_attachment_copies

        async def adopt_then_relink(attachments):
            copies = await adopt(attachments)
            if change == "unlink":
                ds.get_linked_slot = MagicMock(return_value=None)
            elif change == "relink":
                ds.get_linked_slot = MagicMock(return_value=new_slot)
            else:
                type(old_slot).is_closing = PropertyMock(return_value=True)
            return copies

        monkeypatch.setattr(handler, "adopt_attachment_copies", adopt_then_relink)
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch.object(handler, "append_and_surface") as append,
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as mock_run_chat,
        ):
            await handler.handle_message(
                slack, MagicMock(), "C1", "look", "t1", "msg1", "U1", attachments=atts
            )
        append.assert_not_called()
        mock_run_chat.assert_not_called()
        assert old_slot._queue == [] and new_slot._queue == []
        assert not uploads.exists() or list(uploads.iterdir()) == []
        posted = " ".join(str(c.args[1]) for c in slack.post_message.call_args_list)
        assert "Not sent" in posted

    @pytest.mark.asyncio
    async def test_an_image_over_the_dashboard_upload_cap_refuses_the_adoption(
        self, tmp_path, monkeypatch
    ):
        """The copies are dashboard uploads, so they take the upload writer's
        size cap too -- and an image over it is not silently left out: the
        adoption raises, so the route refuses the turn where the user can see
        it, and any copy already made is removed."""
        from kiro_crew.prompt_attachments import PromptAttachment
        from kiro_crew.slack import handler

        uploads = tmp_path / "uploads"
        monkeypatch.setattr("kiro_crew.dashboard.handlers.files._UPLOAD_DIR", uploads)
        monkeypatch.setattr("kiro_crew.dashboard.handlers.files._MAX_UPLOAD_BYTES", len(_PNG))
        small = tmp_path / "small.png"
        small.write_bytes(_PNG)
        big = tmp_path / "big.png"
        big.write_bytes(_PNG + b"\0")

        with pytest.raises(handler.AttachmentAdoptionError):
            await handler.adopt_attachment_copies(
                (
                    PromptAttachment(path=str(small), name="small.png"),
                    PromptAttachment(path=str(big), name="big.png"),
                )
            )
        assert list(uploads.iterdir()) == []  # the small copy did not outlive the refusal

    @pytest.mark.asyncio
    async def test_a_link_swapped_in_for_the_picture_is_refused_without_a_copy(
        self, tmp_path, monkeypatch
    ):
        """The adoption copy runs in the gateway, outside any sandbox. A link
        planted at the validated path (to a credential file, say) must be
        refused by the same descriptor-pinned reader every other staging path
        uses -- never followed, never copied under the upload key."""
        from kiro_crew.prompt_attachments import PromptAttachment
        from kiro_crew.slack import handler

        uploads = tmp_path / "uploads"
        monkeypatch.setattr("kiro_crew.dashboard.handlers.files._UPLOAD_DIR", uploads)
        secret = tmp_path / "secret.env"
        secret.write_bytes(b"private configuration bytes")
        link = tmp_path / "shot.png"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available here")

        with pytest.raises(handler.AttachmentAdoptionError):
            await handler.adopt_attachment_copies(
                (PromptAttachment(path=str(link), name="shot.png"),)
            )
        assert not uploads.exists() or list(uploads.iterdir()) == []

    @pytest.mark.asyncio
    async def test_an_adopted_copy_is_owner_only_like_every_upload(self, tmp_path, monkeypatch):
        """The adopted copy lands in the dashboard's upload directory beside files
        the upload writer creates owner-only; a copy left at the umask default
        would be the one picture there another local user can read."""
        import os
        import stat

        from kiro_crew.prompt_attachments import PromptAttachment
        from kiro_crew.slack import handler

        if os.name == "nt":
            pytest.skip("POSIX permission bits")
        uploads = tmp_path / "uploads"
        monkeypatch.setattr("kiro_crew.dashboard.handlers.files._UPLOAD_DIR", uploads)
        src = tmp_path / "shot.png"
        src.write_bytes(_PNG)
        old_umask = os.umask(0o022)
        try:
            adopted = await handler.adopt_attachment_copies(
                (PromptAttachment(path=str(src), name="shot.png"),)
            )
        finally:
            os.umask(old_umask)
        [(_temp, copy)] = adopted
        assert stat.S_IMODE(os.stat(copy).st_mode) == 0o600

    @pytest.mark.asyncio
    async def test_a_copy_that_fails_mid_write_leaves_no_truncated_upload(
        self, tmp_path, monkeypatch
    ):
        """A copy that raises after the destination exists (disk full, a
        write error) must not leave that truncated file under the dashboard's
        upload key -- the file server and the resolver would serve it with no
        row or turn tying it to anything (Opus review). The destination is
        registered for cleanup before the bytes move, not after."""
        import pathlib

        from kiro_crew.dashboard.handlers import files as upload_files
        from kiro_crew.prompt_attachments import PromptAttachment
        from kiro_crew.slack import handler

        uploads = tmp_path / "uploads"
        monkeypatch.setattr("kiro_crew.dashboard.handlers.files._UPLOAD_DIR", uploads)
        src = tmp_path / "shot.png"
        src.write_bytes(_PNG)

        def _half_write(path, data):
            pathlib.Path(path).write_bytes(data[: len(data) // 2])
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(upload_files, "_write_file_restricted", _half_write)
        with pytest.raises(handler.AttachmentAdoptionError):
            await handler.adopt_attachment_copies(
                (PromptAttachment(path=str(src), name="shot.png"),)
            )
        assert list(uploads.iterdir()) == []

    @pytest.mark.asyncio
    async def test_an_unwritable_upload_dir_refuses_the_turn_visibly(self, tmp_path, monkeypatch):
        """Fail closed: when the copy cannot be made, the linked route appends
        no row, starts no turn and queues nothing -- it answers in the thread
        that nothing was sent. Running without the picture would answer about
        an image the model never saw; queueing would lose it with the temp file."""
        from kiro_crew.dashboard.state import _ChatSlot
        from kiro_crew.prompt_attachments import PromptAttachment
        from kiro_crew.slack import handler

        blocker = tmp_path / "not-a-dir"
        blocker.write_bytes(b"file where the upload dir must be")
        monkeypatch.setattr("kiro_crew.dashboard.handlers.files._UPLOAD_DIR", blocker / "uploads")
        temp = tmp_path / "tmpab12.png"
        temp.write_bytes(_PNG)
        slot = _ChatSlot("slot1")
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=slot)
        ds._background_tasks = set()
        ds.broadcast_ws = MagicMock()
        ds.push_slots_update = MagicMock()
        slack = _make_slack()
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as mock_run_chat,
        ):
            await handler.handle_message(
                slack,
                MagicMock(),
                "C1",
                f"look\n{temp}",
                "t1",
                "msg1",
                "U1",
                attachments=(PromptAttachment(path=str(temp), name="shot.png"),),
            )
            mock_run_chat.assert_not_called()
        assert slot.messages == []
        assert slot._queue == []
        slack.post_message.assert_awaited_once()
        args = slack.post_message.await_args.args
        assert args[0] == "C1" and args[2] == "t1"
        assert "Nothing was sent" in args[1]

    @pytest.mark.asyncio
    async def test_the_persisted_linked_row_names_the_adopted_copies(self, tmp_path, monkeypatch):
        """The row is what a later regenerate / edit-resend re-runs, so it must
        carry the adopted copies -- in `meta.images` like every dashboard row and
        in its text -- not the Slack temp path the handler unlinks on return.
        After that unlink, a regenerate-shaped read of the row still hands the
        provider a real file."""
        from kiro_crew.dashboard.chat_runner import _turn_prompt_attachments
        from kiro_crew.dashboard.slot_queue_repository import retained_image_meta
        from kiro_crew.dashboard.state import _ChatSlot
        from kiro_crew.prompt_attachments import PromptAttachment
        from kiro_crew.slack import handler

        uploads = tmp_path / "uploads"
        monkeypatch.setattr("kiro_crew.dashboard.handlers.files._UPLOAD_DIR", uploads)
        temp = tmp_path / "kc-slack" / "tmpab12.png"
        temp.parent.mkdir()
        temp.write_bytes(_PNG)
        shot = str(temp)
        slot = _ChatSlot("slot1")  # a REAL slot: the row must be stored, not mocked
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=slot)
        ds._background_tasks = set()
        ds.broadcast_ws = MagicMock()
        ds.push_slots_update = MagicMock()
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as mock_run_chat,
        ):
            await handler.handle_message(
                _make_slack(),
                MagicMock(),
                "C1",
                f"look at this\n{shot}",
                "t1",
                "msg1",
                "U1",
                attachments=(PromptAttachment(path=shot, name="shot.png"),),
            )
            (adopted,) = mock_run_chat.call_args.kwargs["_prompt_images"]

        row = slot.messages[-1]
        assert row["role"] == "user"
        assert row["meta"]["images"] == [adopted]
        assert adopted in row["content"] and shot not in row["content"]

        # The Slack handler's cleanup runs; the row's references still resolve.
        temp.unlink()
        rebuilt = retained_image_meta(row["meta"], row["content"], row["content"])
        assert [a.path for a in _turn_prompt_attachments(rebuilt)] == [adopted]
        assert os.path.exists(adopted)

    @pytest.mark.asyncio
    async def test_transport_unauthorized_denied(self):
        from kiro_crew.slack import handler, transport_dispatch

        slack = _make_slack()
        _slot = MagicMock(key="slot1")
        type(_slot).running = PropertyMock(return_value=False)
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=_slot)
        mock_sel_inst = MagicMock()
        orig_sel = handler.sel
        handler.sel = lambda: mock_sel_inst
        sessions = MagicMock()
        sessions.get_or_create = AsyncMock(side_effect=AssertionError("session acquired"))
        try:
            with (
                patch.object(handler, "_dashboard_state", ds),
                patch.object(handler, "is_allowed_user", return_value=False),
            ):
                await transport_dispatch.handle_message_transport(
                    slack,
                    sessions,
                    "C1",
                    "hello",
                    "t1",
                    "msg1",
                    "UBAD",
                )
                # Denied with SEL audit; no session acquired.
                mock_sel_inst.log_tool_invocation.assert_called_once()
                assert mock_sel_inst.log_tool_invocation.call_args[1]["outcome"] == "denied"
                assert any(
                    "not authorized" in str(c).lower() for c in slack.post_message.call_args_list
                )
                sessions.get_or_create.assert_not_called()
        finally:
            handler.sel = orig_sel


# ── Bare `sessions` keyword fall-through in a linked thread ──


class TestSessionsKeywordFallThrough:
    """The bare ``sessions`` keyword must win over a linked dashboard DM, the
    same way ``!``-bang commands fall through — otherwise the native session
    picker is unreachable in a linked thread."""

    def _linked_ds(self):
        slot = MagicMock()
        type(slot).running = PropertyMock(return_value=False)
        slot.key = "slot1"
        slot._queue = []
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=slot)
        ds._background_tasks = set()
        ds.broadcast_ws = MagicMock()
        ds.push_slots_update = MagicMock()
        return ds, slot

    @pytest.mark.asyncio
    async def test_bare_sessions_falls_through_not_routed(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        ds, slot = self._linked_ds()
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
        ):
            result = await handler.maybe_route_linked_thread(
                "sessions", "slack:t1", "U1", "C1", slack, "t1"
            )
        # Falls through to normal handling: no user row appended, no queueing,
        # no dashboard broadcast — the caller's keyword branch takes over.
        assert result is False
        slot.append.assert_not_called()
        slot.queue_append.assert_not_called()
        ds.push_slots_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_exact_sessions_text_still_routed(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        ds, slot = self._linked_ds()
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock),
        ):
            result = await handler.maybe_route_linked_thread(
                "sessions please", "slack:t1", "U1", "C1", slack, "t1"
            )
        # The predicate is exact-match only: anything else keeps routing to
        # the linked slot, pinning the narrowing.
        assert result is True
        slot.append.assert_called_once()

    @pytest.mark.asyncio
    async def test_unauthorized_sessions_still_denied(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        ds, slot = self._linked_ds()
        mock_sel_inst = MagicMock()
        orig_sel = handler.sel
        handler.sel = lambda: mock_sel_inst
        try:
            with (
                patch.object(handler, "_dashboard_state", ds),
                patch.object(handler, "is_allowed_user", return_value=False),
            ):
                result = await handler.maybe_route_linked_thread(
                    "sessions", "slack:t1", "UBAD", "C1", slack, "t1"
                )
            # The auth deny stays ahead of the keyword fall-through: an
            # unauthorized sender gets the denial, not the session picker.
            assert result is True
            kw = mock_sel_inst.log_tool_invocation.call_args[1]
            assert kw["outcome"] == "denied"
            assert any(
                "not authorized" in str(c).lower() for c in slack.post_message.call_args_list
            )
        finally:
            handler.sel = orig_sel

    @pytest.mark.asyncio
    async def test_pinned_options_answer_sessions_still_delivered(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        ds, slot = self._linked_ds()
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock),
        ):
            result = await handler.maybe_route_linked_thread(
                "sessions",
                "slack:t1",
                "U1",
                "C1",
                slack,
                "t1",
                target_slot=slot,
                route_pinned=True,
            )
        # A pinned OPTIONS answer whose label text is exactly "sessions" is a
        # DELIVERY to the conversation that asked the question — it must reach
        # the pinned slot, not be swallowed by the keyword fall-through.
        assert result is True
        slot.append.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_message_reaches_sessions_command_when_linked(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        ds, slot = self._linked_ds()
        mock_sel_inst = MagicMock()
        orig_sel = handler.sel
        handler.sel = lambda: mock_sel_inst
        try:
            with (
                patch.object(handler, "_dashboard_state", ds),
                patch.object(handler, "is_allowed_user", return_value=True),
                patch.object(handler, "is_owner", return_value=True),
                patch.object(
                    handler, "_handle_sessions_command", new_callable=AsyncMock
                ) as mock_cmd,
            ):
                await handler.handle_message(
                    slack,
                    MagicMock(),
                    "C1",
                    "sessions",
                    "t1",
                    "msg1",
                    "U1",
                )
            # End to end: the keyword wins over the linked DM — the native
            # session picker path runs and the slot gets no user row.
            mock_cmd.assert_awaited_once()
            slot.append.assert_not_called()
        finally:
            handler.sel = orig_sel
