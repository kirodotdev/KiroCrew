"""Tests for /api/file-read and /api/file-write endpoints."""

from __future__ import annotations

import errno
import json
import os
import stat
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import (
    _sanitize_blocks,
    api_file_read,
    api_file_write,
    api_send_message,
)


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-read", api_file_read)
    app.router.add_post("/api/file-write", api_file_write)
    return app


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


@pytest.fixture
def tmp_file(tmp_path):
    f = tmp_path / "test.md"
    f.write_text("hello world")
    return f


@pytest.fixture
def home_patch(tmp_path):
    """Patch expanduser and realpath so tmp_path is treated as $HOME."""
    real_realpath = os.path.realpath

    def fake_expanduser(p):
        return p.replace("~", str(tmp_path))

    with patch("os.path.expanduser", side_effect=fake_expanduser), patch(
        "os.path.realpath", side_effect=real_realpath
    ), patch("pathlib.Path.home", return_value=tmp_path):
        yield tmp_path


class TestFileRead:
    @pytest.mark.asyncio
    async def test_read_success(self, tmp_file, mock_sel, home_patch):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={tmp_file}")
            assert resp.status == 200
            text = await resp.text()
            assert "hello world" in text
            mock_sel.log_tool_invocation.assert_called_with(
                session_key="dashboard",
                tool_name="file_read",
                outcome="success",
                resources=str(tmp_file),
            )

    @pytest.mark.asyncio
    async def test_read_missing_path(self, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/file-read?path=")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_read_outside_home(self, mock_sel, home_patch):
        """Non-sensitive paths outside home are allowed; only is_sensitive_path blocks."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/file-read?path=/etc/passwd")
            assert resp.status in (200, 404)  # allowed, depends on file existence

    @pytest.mark.asyncio
    async def test_read_sensitive_path(self, mock_sel, home_patch):
        ssh_dir = home_patch / ".ssh"
        ssh_dir.mkdir()
        key_file = ssh_dir / "id_rsa"
        key_file.write_text("secret")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={key_file}")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_read_not_found(self, mock_sel, home_patch):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={home_patch}/nonexistent.txt")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_read_json_sets_application_json_content_type(
        self, tmp_path, mock_sel, home_patch
    ):
        # .json files MUST be served with application/json so the browser
        # DevTools "Response" preview renders the body as a tree (instead
        # of plain text) and downstream tooling like jq can be piped
        # directly. Synthetic fixture only.
        f = tmp_path / "fixture.json"
        f.write_text('{"a": 1, "label": "中文標籤範例"}', encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}")
            assert resp.status == 200
            ct = resp.headers["Content-Type"]
            assert ct.startswith("application/json"), ct
            assert "charset=utf-8" in ct.lower(), ct
            text = await resp.text()
            assert "中文標籤範例" in text
            # Round-trip parse to prove the bytes are valid UTF-8 JSON.
            assert json.loads(text)["label"] == "中文標籤範例"

    @pytest.mark.asyncio
    async def test_read_jsonl_sets_x_ndjson_content_type(self, tmp_path, mock_sel, home_patch):
        # JSONL is NOT a single JSON document — must use application/x-ndjson
        # so clients don't try to parse the whole body as one JSON value.
        f = tmp_path / "lines.jsonl"
        f.write_text('{"a":1}\n{"b":2}\n', encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}")
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("application/x-ndjson")

    @pytest.mark.asyncio
    async def test_read_html_served_as_text_plain_to_block_xss(
        self, tmp_path, mock_sel, home_patch
    ):
        # Security: HTML must NOT be served with text/html content-type
        # because user/LLM-generated files may contain <script> tags or on*
        # attributes that would execute in the dashboard origin. HtmlViewer
        # renders HTML via a sandboxed srcDoc iframe, so the file-read
        # endpoint never needs to deliver executable HTML.
        f = tmp_path / "evil.html"
        f.write_text("<html><script>alert(1)</script></html>", encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}")
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith(
                "text/plain"
            ), f"HTML files MUST be served as text/plain (got {resp.headers['Content-Type']})"

    @pytest.mark.asyncio
    async def test_read_md_sets_text_markdown_content_type(self, tmp_path, mock_sel, home_patch):
        f = tmp_path / "note.md"
        f.write_text("# title", encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}")
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/markdown")

    @pytest.mark.asyncio
    async def test_read_unknown_extension_falls_back_to_text_plain(
        self, tmp_path, mock_sel, home_patch
    ):
        # Default content_type for files without a known extension stays
        # text/plain so existing behaviour is preserved.
        f = tmp_path / "notes.log"
        f.write_text("line 1\nline 2\n", encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}")
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/plain")

    @pytest.mark.asyncio
    async def test_read_utf8_multibyte_round_trip(self, tmp_path, mock_sel, home_patch):
        # Regression: the dashboard file viewer reported "Invalid JSON" for
        # files containing CJK characters. Verify that UTF-8 bytes survive
        # the read pipeline (open + redactors + Response.text) byte-for-byte.
        # Synthetic fixture only — no real customer/case data.
        payload = {
            "ascii_key": "value",
            "labels": {
                "zh": "中文標籤範例",
                "ja": "日本語ラベル",
                "ko": "한국어 라벨",
            },
            "tags": ["測試", "テスト", "테스트", "🐾"],
        }
        f = tmp_path / "fixture.json"
        f.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}")
            assert resp.status == 200
            text = await resp.text()
            parsed = json.loads(text)
            assert parsed["labels"] == payload["labels"]
            assert parsed["tags"] == payload["tags"]


class TestFileReadPathKind:
    """``X-Path-Kind`` lets a caller tell a directory apart from a missing path.

    Both are 404 (a read has no content to return either way), so the status
    code alone is ambiguous. The dashboard's markdown path chips need the
    difference: a directory gets a folder affordance, a path that is not on
    disk gets no affordance at all.
    """

    @pytest.mark.asyncio
    async def test_directory_is_404_with_dir_kind(self, tmp_path, mock_sel, home_patch):
        d = tmp_path / "somedir"
        d.mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={d}")
            assert resp.status == 404
            assert resp.headers["X-Path-Kind"] == "dir"
            assert "directory" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_missing_is_404_with_missing_kind(self, mock_sel, home_patch):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={home_patch}/nope.txt")
            assert resp.status == 404
            assert resp.headers["X-Path-Kind"] == "missing"

    @pytest.mark.asyncio
    async def test_head_on_file_reports_file_kind(self, tmp_file, mock_sel, home_patch):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.head(f"/api/file-read?path={tmp_file}")
            assert resp.status == 200
            assert resp.headers["X-Path-Kind"] == "file"

    @pytest.mark.asyncio
    async def test_head_on_directory_reports_dir_kind(self, tmp_path, mock_sel, home_patch):
        # The isfile() gate precedes the HEAD branch, so HEAD and GET must agree
        # on kind. The chip probe uses HEAD, so this is the path that matters.
        d = tmp_path / "headdir"
        d.mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.head(f"/api/file-read?path={d}")
            assert resp.status == 404
            assert resp.headers["X-Path-Kind"] == "dir"

    @pytest.mark.asyncio
    async def test_head_on_missing_reports_missing_kind(self, mock_sel, home_patch):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.head(f"/api/file-read?path={home_patch}/ghost.md")
            assert resp.status == 404
            assert resp.headers["X-Path-Kind"] == "missing"

    @pytest.mark.asyncio
    async def test_forbidden_path_leaks_no_kind(self, mock_sel, home_patch):
        """A denylisted path must 400 without disclosing whether it exists.

        The chip treats a missing header as "not actionable", so a 400 renders
        as plain text — the probe must not become an existence oracle for
        credential stores.
        """
        ssh_dir = home_patch / ".ssh"
        ssh_dir.mkdir()
        key = ssh_dir / "id_rsa"
        key.write_text("secret")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={key}")
            assert resp.status == 400
            assert "X-Path-Kind" not in resp.headers


class TestFileWrite:
    @pytest.mark.asyncio
    async def test_write_success(self, tmp_file, mock_sel, home_patch):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/file-write", json={"path": str(tmp_file), "content": "updated"}
            )
            assert resp.status == 200
            assert tmp_file.read_text(encoding="utf-8") == "updated"
            mock_sel.log_tool_invocation.assert_called_with(
                session_key="dashboard",
                tool_name="file_write",
                outcome="success",
                resources=str(tmp_file),
            )

    @pytest.mark.asyncio
    async def test_write_outside_home(self, mock_sel, home_patch):
        """Non-sensitive paths outside home are allowed; /etc/evil returns 404 (not found)."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/file-write", json={"path": "/etc/evil", "content": "x"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_write_invalid_json(self, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/file-write", data=b"not json", headers={"Content-Type": "application/json"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_write_sensitive_path(self, mock_sel, home_patch):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/file-write", json={"path": str(home_patch / ".ssh/id_rsa"), "content": "x"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_write_routes_acl_preservation_through_atomic_write(
        self, tmp_file, mock_sel, home_patch, monkeypatch
    ):
        """api_file_write must carry the source inode's ACL, not just its bits.

        The old mkstemp+copymode path carried permission BITS only, dropping a
        named POSIX ACL on every save. Assert the handler now
        routes through atomic_write with an OPEN source descriptor via
        preserve_access_control_from and the existing file mode; a revert to
        copymode fails here.
        """
        import os as _os

        import kiro_crew.atomic_write as aw
        from kiro_crew.dashboard.handlers import files as files_mod

        captured: dict[str, object] = {}
        original = files_mod.atomic_write

        def recording(target, content, **kwargs):
            captured["kwargs"] = dict(kwargs)
            captured["thread"] = threading.current_thread().ident
            src_fd = kwargs.get("preserve_access_control_from")
            if isinstance(src_fd, int):
                captured["source_bytes"] = _os.read(src_fd, 4096)
            original(target, content, **kwargs)

        monkeypatch.setattr(files_mod, "atomic_write", recording)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/file-write", json={"path": str(tmp_file), "content": "updated"}
            )
            assert resp.status == 200

        kwargs = captured["kwargs"]
        # See the steering twin: a descriptor where the xattr syscalls exist,
        # None where they do not (Windows), because a handle held open there
        # would make os.replace fail. The kwarg itself must always be passed.
        assert "preserve_access_control_from" in kwargs
        if aw.ACCESS_CONTROL_XATTRS_SUPPORTED:
            assert isinstance(kwargs["preserve_access_control_from"], int)
            assert captured["source_bytes"] == b"hello world"
        else:  # pragma: no cover - exercised on Windows CI only
            assert kwargs["preserve_access_control_from"] is None
        assert kwargs["mode"] == stat.S_IMODE(tmp_file.stat().st_mode)
        assert tmp_file.read_text(encoding="utf-8") == "updated"
        # Off the event loop (no-blocking-call-on-event-loop): every call in the
        # transaction is a blocking filesystem call, so a network-backed path
        # would otherwise freeze chat and the heartbeat -- and atomic_write's
        # Windows rename retry degrades to a single attempt on a loop thread.
        assert captured["thread"] != threading.current_thread().ident

    @pytest.mark.asyncio
    async def test_write_refuses_a_final_component_swapped_to_a_link(
        self, tmp_path, mock_sel, home_patch, monkeypatch
    ):
        """A TOCTOU swap of the final component into a link is a 4xx, not a 500.

        ``_validate_dashboard_path`` canonicalizes through ``realpath``, so the
        path reaching the handler is symlink-free by construction and a leaf link
        is followed to its target exactly as it was before this change. The
        ``O_NOFOLLOW`` in ``open_access_control_source`` therefore only closes
        the window where that component is swapped for a link AFTER the check.
        When it fires, that is a rejected target rather than a server fault: the
        handler returns 404 (matching the steering peer's ``notfound``) and
        ``atomic_write`` is never reached.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        target = tmp_path / "real.md"
        target.write_text("protected", encoding="utf-8")

        def swapped_under_us(*args, **kwargs):
            raise OSError(errno.ELOOP, "symbolic link loop")

        def fail_if_called(*args, **kwargs):
            raise AssertionError("atomic_write must not run once the open is refused")

        monkeypatch.setattr(files_mod, "open_access_control_source", swapped_under_us)
        monkeypatch.setattr(files_mod, "atomic_write", fail_if_called)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/file-write", json={"path": str(target), "content": "attacker"}
            )
            assert resp.status == 404
            assert (await resp.json())["code"] == "not_found"
        assert target.read_text(encoding="utf-8") == "protected"
        mock_sel.log_tool_invocation.assert_called_with(
            session_key="dashboard",
            tool_name="file_write",
            outcome="not_found",
            resources=str(target),
        )

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.name == "nt", reason="creating a symlink on Windows needs elevation")
    async def test_write_follows_a_leaf_symlink_to_its_canonical_target(
        self, tmp_path, mock_sel, home_patch
    ):
        """Writing a leaf symlink lands on its target, as it did before.

        Stated as a test because the ACL carry added an ``os.open`` on the write
        path, and it must not change which inode a save reaches: ``realpath``
        resolution happens in the path guard, upstream of everything here.
        """
        real = tmp_path / "real.md"
        real.write_text("old", encoding="utf-8")
        link = tmp_path / "link.md"
        link.symlink_to(real)

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/file-write", json={"path": str(link), "content": "new"})
            assert resp.status == 200
        assert real.read_text(encoding="utf-8") == "new"
        assert link.is_symlink()


def _make_send_app(state) -> web.Application:
    app = web.Application()
    app.router.add_post("/api/send-message", api_send_message)
    app["state"] = state
    return app


def _mock_state(slack_client=None, owner_id=""):
    state = MagicMock()
    state.slack_client = slack_client
    state.owner_id = owner_id
    return state


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_message_missing_text(self):
        app = _make_send_app(_mock_state())
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/send-message", json={})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_send_message_dashboard_only(self):
        state = _mock_state()
        app = _make_send_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/send-message", json={"text": "hello"})
            assert resp.status == 200
            data = await resp.json()
            assert data == {"ok": True, "slack": False, "session": False, "delivered_to": "notification"}
            state.notify.assert_called_once_with("agent", "Agent Message", "hello")

    @pytest.mark.asyncio
    async def test_send_message_with_slack(self):
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="C123")
        slack.post_message = AsyncMock(return_value="1712793600.000001")
        state = _mock_state(slack_client=slack, owner_id="U123")
        app = _make_send_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message", json={"text": "hello", "title": "Test", "session": "slack"}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data == {"ok": True, "slack": True, "session": False, "delivered_to": "slack", "ts": "1712793600.000001"}
            state.notify.assert_called_once_with("agent", "Test", "hello")
            slack.open_dm.assert_called_once_with("U123")
            slack.post_message.assert_called_once_with(
                "C123",
                "hello",
                thread_ts=None,
                unfurl_links=None,
                unfurl_media=None,
                reply_broadcast=None,
            )

    @pytest.mark.asyncio
    async def test_send_message_slack_error(self):
        slack = MagicMock()
        slack.open_dm = AsyncMock(side_effect=Exception("fail"))
        state = _mock_state(slack_client=slack, owner_id="U123")
        app = _make_send_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message", json={"text": "hello", "session": "slack"}
            )
            assert resp.status == 502
            data = await resp.json()
            assert data["ok"] is False
            assert "fail" in data["error"]

    @pytest.mark.asyncio
    async def test_send_message_slack_post_error(self):
        """502 when open_dm succeeds but post_message raises."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="C123")
        slack.post_message = AsyncMock(side_effect=Exception("slack_api_error"))
        state = _mock_state(slack_client=slack, owner_id="U123")
        app = _make_send_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message", json={"text": "hello", "session": "slack"}
            )
            assert resp.status == 502
            data = await resp.json()
            assert data["ok"] is False
            assert "slack_api_error" in data["error"]

    @pytest.mark.asyncio
    async def test_send_message_with_blocks(self):
        """Blocks are sent via post_blocks with text as fallback."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="C123")
        slack.post_blocks = AsyncMock(return_value="1712793600.000001")
        state = _mock_state(slack_client=slack, owner_id="U123")
        app = _make_send_app(state)
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hello"}}]
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message", json={"text": "fallback", "blocks": blocks, "session": "slack"}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data == {"ok": True, "slack": True, "session": False, "delivered_to": "slack", "ts": "1712793600.000001"}
            slack.post_blocks.assert_called_once_with(
                "C123",
                blocks,
                "fallback",
                thread_ts=None,
                unfurl_links=None,
                unfurl_media=None,
                reply_broadcast=None,
            )
            slack.post_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_message_without_blocks_uses_post_message(self):
        """Without blocks, falls back to post_message (backward compat)."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="C123")
        slack.post_message = AsyncMock(return_value="1712793600.000001")
        state = _mock_state(slack_client=slack, owner_id="U123")
        app = _make_send_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message", json={"text": "hello", "session": "slack"}
            )
            assert resp.status == 200
            slack.post_message.assert_called_once_with(
                "C123",
                "hello",
                thread_ts=None,
                unfurl_links=None,
                unfurl_media=None,
                reply_broadcast=None,
            )
            slack.post_blocks.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_message_blocks_passed_to_post_blocks(self):
        """Blocks are forwarded to post_blocks with content intact."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="C123")
        slack.post_blocks = AsyncMock(return_value="1712793600.000001")
        state = _mock_state(slack_client=slack, owner_id="U123")
        app = _make_send_app(state)
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "safe text"}}]
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message", json={"text": "fallback", "blocks": blocks, "session": "slack"}
            )
            assert resp.status == 200
            # Verify blocks were passed (sanitized) — content should survive intact
            call_args = slack.post_blocks.call_args
            sent_blocks = call_args[0][1]
            assert sent_blocks[0]["text"]["text"] == "safe text"

    @pytest.mark.asyncio
    async def test_send_message_session_origin(self):
        """session='origin' injects into the cron's originating session and triggers a turn."""
        state = _mock_state()
        # Mock a slot that the cron originated from
        mock_slot = MagicMock()
        mock_slot.running = False
        # Real _ChatSlot defaults this False; a bare MagicMock returns a truthy
        # Mock and would trip the busy guard (running or _in_stage_execution),
        # diverting origin-inject to the queue branch.
        mock_slot._in_stage_execution = False
        mock_slot.task = None
        mock_slot.key = "chat-1-1712793600"
        state.get_slot = MagicMock(return_value=mock_slot)
        state._background_tasks = set()
        state.push_slots_update = MagicMock()
        # Mock cron job with session_key pointing to the origin session
        mock_job = MagicMock()
        mock_job.id = "abc12345"
        mock_job.name = "check pipeline"
        mock_job.session_key = "dashboard:chat-1-1712793600"
        state.crons.list_jobs = MagicMock(return_value=[mock_job])
        app = _make_send_app(state)
        with patch(
            "kiro_crew.dashboard.chat_runner._run_chat", new_callable=AsyncMock
        ) as mock_run, patch(
            "kiro_crew.dashboard.handlers.messaging.rehydrate_slot_from_history_async"
        ) as mock_rehydrate:
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={
                        "text": "build failed",
                        "session": "origin",
                        "caller_session": "cron:abc12345",
                    },
                )
                assert resp.status == 200
                data = await resp.json()
                assert data == {"ok": True, "slack": False, "session": True, "delivered_to": "session"}
                # Hot-path: in-memory slot found, no rehydrate needed.
                state.get_slot.assert_called_once_with("chat-1-1712793600")
                mock_rehydrate.assert_not_called()
                # Injected as user message to trigger agent turn
                call_args = mock_slot.append.call_args
                assert call_args[0][0] == "inject"
                assert '[Cron notification from "check pipeline"]' in call_args[0][1]
                assert "build failed" in call_args[0][1]
                assert json.loads(call_args[0][2]) == {"cronLabel": "check pipeline"}
                mock_run.assert_called_once()
                # Should NOT fall back to notify/Slack
                state.notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_message_session_origin_queued(self):
        """Queues the message when the target session is already running."""
        state = _mock_state()
        mock_slot = MagicMock()
        mock_slot.running = True
        mock_slot._queue = []
        mock_slot.queue_append = lambda content, kind="": (
            mock_slot._queue.append({"id": "test", "content": content}) or "test"
        )
        state.get_slot = MagicMock(return_value=mock_slot)
        mock_job = MagicMock()
        mock_job.id = "abc12345"
        mock_job.name = "monitor build"
        mock_job.session_key = "dashboard:chat-1-1712793600"
        state.crons.list_jobs = MagicMock(return_value=[mock_job])
        app = _make_send_app(state)
        with patch(
            "kiro_crew.dashboard.handlers.messaging.rehydrate_slot_from_history_async"
        ) as mock_rehydrate:
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={
                        "text": "build failed",
                        "session": "origin",
                        "caller_session": "cron:abc12345",
                    },
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["session"] is True
                # Message queued, not triggering a new turn
                assert len(mock_slot._queue) == 1
                assert "build failed" in mock_slot._queue[0]["content"]
                call_args = mock_slot.append.call_args
                assert call_args[0][0] == "queued"
                # Hot-path: no rehydrate when slot is in memory.
                mock_rehydrate.assert_not_called()
                state.notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_message_session_origin_revives_missing_slot(self):
        """When slot isn't in memory (e.g. after gateway restart), rehydrate via
        _rehydrate_slot_from_history and still trigger an agent turn on the revived
        slot. Regression test for silent-fail bug where cron→origin injection fell
        back to owner DM after gateway restart.

        Mirrors the coverage of test_send_message_session_origin (happy path) —
        patches _run_chat, asserts it was invoked on the revived slot, and verifies
        the injected message content matches the cron-notification contract.

        This is a focused routing test: it mocks _rehydrate_slot_from_history so
        we can assert the handler calls it exactly when get_slot returns None. The
        end-to-end rehydrate path (real ConversationLog, real DashboardState,
        real _ChatSlot creation) is covered by
        TestRehydrateSlotFromHistory in test_session_restore.py."""
        state = _mock_state()
        # Simulate cold-start: slot not loaded in memory yet.
        state.get_slot = MagicMock(return_value=None)
        # Rehydrate helper returns a slot reconstructed from persisted history.
        mock_slot = MagicMock()
        mock_slot.running = False
        # Real _ChatSlot defaults this False; a bare MagicMock returns a truthy
        # Mock and would trip the busy guard (running or _in_stage_execution),
        # diverting origin-inject to the queue branch.
        mock_slot._in_stage_execution = False
        mock_slot.task = None
        mock_slot.key = "chat-1-1712793600"
        state._background_tasks = set()
        state.push_slots_update = MagicMock()
        mock_job = MagicMock()
        mock_job.id = "abc12345"
        mock_job.name = "test-cron"
        mock_job.session_key = "dashboard:chat-1-1712793600"
        state.crons.list_jobs = MagicMock(return_value=[mock_job])
        app = _make_send_app(state)
        with patch(
            "kiro_crew.dashboard.chat_runner._run_chat", new_callable=AsyncMock
        ) as mock_run, patch(
            "kiro_crew.dashboard.handlers.messaging.rehydrate_slot_from_history_async",
            return_value=mock_slot,
        ) as mock_rehydrate:
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": "update", "session": "origin", "caller_session": "cron:abc12345"},
                )
                assert resp.status == 200
                data = await resp.json()
                # Session delivery succeeded — no Slack DM fallback.
                assert data == {"ok": True, "slack": False, "session": True, "delivered_to": "session"}
                # Hot-path miss: get_slot called first, then rehydrate helper.
                state.get_slot.assert_called_once_with("chat-1-1712793600")
                mock_rehydrate.assert_called_once_with(state, "chat-1-1712793600")
                # Agent turn was triggered on the revived slot (the whole point of the fix).
                mock_run.assert_called_once()
                # Injected as user message with cron-notification contract.
                call_args = mock_slot.append.call_args
                assert call_args[0][0] == "inject"
                assert '[Cron notification from "test-cron"]' in call_args[0][1]
                assert "update" in call_args[0][1]
                # Message was injected, not sent as a notification.
                state.notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_message_session_origin_rehydrate_reads_off_the_loop(self):
        """The cold-slot rehydration must not parse the transcript on the loop.

        Issue #7408: this handler called the SYNCHRONOUS rehydrate, which read and
        JSON-parsed the whole transcript inline -- 100-300 ms on a large store,
        stalling every other request. Asserted by thread identity rather than by
        the name of the function called, so the guarantee survives a rename.
        """
        from kiro_crew.dashboard import chat_persistence

        state = _mock_state()
        state.get_slot = MagicMock(return_value=None)
        state.conversation_log = MagicMock()
        state._slots = {}
        mock_job = MagicMock()
        mock_job.id = "abc12345"
        mock_job.name = "test-cron"
        mock_job.session_key = "dashboard:chat-1-1712793600"
        state.crons.list_jobs = MagicMock(return_value=[mock_job])
        app = _make_send_app(state)
        seen: list[int] = []

        def _prefetch(*_a, **_kw):
            seen.append(threading.get_ident())
            # messages=None means "nothing persisted", so the handler falls back to
            # the notification path -- keeping this test about the read's location.
            return ({}, True, None, {}, None)

        with patch.object(chat_persistence, "_prefetch_rehydrate_inputs", _prefetch):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": "update", "session": "origin", "caller_session": "cron:abc12345"},
                )
                assert resp.status == 200

        assert seen, (
            "the off-loop prefetch never ran: either the read is happening inline "
            "on the loop again (the #7408 defect) or this seam moved"
        )
        assert threading.get_ident() not in seen, (
            "the transcript was read on the event-loop thread; the handler must "
            "await rehydrate_slot_from_history_async"
        )

    @pytest.mark.asyncio
    async def test_send_message_session_origin_rehydrate_returns_none_falls_back(self):
        """When get_slot returns None AND rehydrate returns None (no persisted
        session on disk), fall back to normal delivery (notification + optional
        Slack DM). Prevents phantom-slot creation when the origin session was
        never persisted or was explicitly closed."""
        state = _mock_state()
        state.get_slot = MagicMock(return_value=None)
        mock_job = MagicMock()
        mock_job.id = "abc12345"
        mock_job.name = "test-cron"
        mock_job.session_key = "dashboard:chat-1-1712793600"
        state.crons.list_jobs = MagicMock(return_value=[mock_job])
        app = _make_send_app(state)
        with patch(
            "kiro_crew.dashboard.handlers.messaging.rehydrate_slot_from_history_async", return_value=None
        ) as mock_rehydrate:
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": "update", "session": "origin", "caller_session": "cron:abc12345"},
                )
                assert resp.status == 200
                data = await resp.json()
                # No session delivery — fell through to notification.
                assert data["session"] is False
                mock_rehydrate.assert_called_once_with(state, "chat-1-1712793600")
                state.notify.assert_called_once()
                call_args = state.notify.call_args[0]
                assert call_args[1] == "⏰ test-cron"
                assert "session closed" in call_args[2]

    @pytest.mark.asyncio
    async def test_send_message_session_origin_no_cron(self):
        """Falls back when caller is not a cron session."""
        state = _mock_state()
        app = _make_send_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "update", "session": "origin", "caller_session": "dashboard:chat-1"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["session"] is False
            state.notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_message_session_origin_stateless_cron(self):
        """Stateless cron (session key 'cron:{id}:{run_id}') resolves the job correctly."""
        state = _mock_state()
        mock_slot = MagicMock()
        mock_slot.running = False
        # Real _ChatSlot defaults this False; a bare MagicMock returns a truthy
        # Mock and would trip the busy guard (running or _in_stage_execution),
        # diverting origin-inject to the queue branch.
        mock_slot._in_stage_execution = False
        mock_slot.task = None
        mock_slot.key = "chat-1-1712793600"
        state.get_slot = MagicMock(return_value=mock_slot)
        state._background_tasks = set()
        state.push_slots_update = MagicMock()
        mock_job = MagicMock()
        mock_job.id = "abc12345"
        mock_job.name = "stateless-cron"
        mock_job.session_key = "dashboard:chat-1-1712793600"
        state.crons.list_jobs = MagicMock(return_value=[mock_job])
        app = _make_send_app(state)
        with patch(
            "kiro_crew.dashboard.chat_runner._run_chat", new_callable=AsyncMock
        ) as mock_run, patch("kiro_crew.dashboard.handlers.messaging.rehydrate_slot_from_history_async"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={
                        "text": "update",
                        "session": "origin",
                        "caller_session": "cron:abc12345:f9a1b2c3",
                    },
                )
                assert resp.status == 200
                data = await resp.json()
                assert data == {"ok": True, "slack": False, "session": True, "delivered_to": "session"}
                state.get_slot.assert_called_once_with("chat-1-1712793600")
                mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_message_session_rejects_arbitrary_key(self):
        """Arbitrary slot keys are rejected — only 'origin' and 'slack' are valid."""
        state = _mock_state()
        state.get_slot = MagicMock()
        app = _make_send_app(state)
        with patch(
            "kiro_crew.dashboard.handlers.messaging.rehydrate_slot_from_history_async"
        ) as mock_rehydrate:
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={
                        "text": "update",
                        "session": "chat-1-1712793600",
                        "caller_session": "cron:abc",
                    },
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["session"] is False
                # Should NOT attempt any slot lookup or rehydrate for a non-"origin" key.
                state.get_slot.assert_not_called()
                mock_rehydrate.assert_not_called()
                state.notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_message_session_slack_bypasses_origin(self):
        """session='slack' is the explicit opt-out: skip origin routing entirely
        and fall through to the Slack DM path (+ dashboard notification). Even
        if the cron has a valid originating session that would normally receive
        the injection, session='slack' routes to Slack instead."""
        state = _mock_state()
        mock_slot = MagicMock()
        mock_slot.running = False
        state.get_slot = MagicMock(return_value=mock_slot)
        # Cron has an origin that WOULD be resolvable — proves session='slack'
        # suppresses resolution regardless.
        mock_job = MagicMock()
        mock_job.id = "abc12345"
        mock_job.name = "notify-slack-cron"
        mock_job.session_key = "dashboard:chat-1-1712793600"
        state.crons.list_jobs = MagicMock(return_value=[mock_job])
        app = _make_send_app(state)
        with patch(
            "kiro_crew.dashboard.handlers.messaging.rehydrate_slot_from_history_async"
        ) as mock_rehydrate:
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={
                        "text": "heads up",
                        "session": "slack",
                        "caller_session": "cron:abc12345",
                    },
                )
                assert resp.status == 200
                data = await resp.json()
                # No origin injection despite a valid origin being available.
                assert data["session"] is False
                # Rehydrate and origin get_slot path never engage for session='slack'.
                state.get_slot.assert_not_called()
                mock_rehydrate.assert_not_called()
                # Dashboard notification always fires (contract invariant).
                state.notify.assert_called_once()


class TestSanitizeBlocks:
    def test_redacts_strings_in_nested_blocks(self):
        """All string values in blocks are passed through redactors."""

        def mock_redactor(s):
            return s.replace("SECRET", "[REDACTED]"), [s] if "SECRET" in s else []

        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "has SECRET here"}}]
        result = _sanitize_blocks(blocks, mock_redactor)
        assert result[0]["text"]["text"] == "has [REDACTED] here"
        # Original not mutated
        assert blocks[0]["text"]["text"] == "has SECRET here"

    def test_truncates_to_50_blocks(self):
        blocks = [{"type": "divider"} for _ in range(100)]
        result = _sanitize_blocks(blocks, lambda s: (s, []))
        assert len(result) == 50

    def test_depth_limit(self):
        """Beyond _MAX_WALK_DEPTH: strings are still sanitized, containers are dropped."""
        # Build a 20-level deep nested dict (exceeds _MAX_WALK_DEPTH=10)
        obj: dict = {"text": "deep_leaf"}
        for _ in range(20):
            obj = {"nested": obj}
        blocks = [obj]
        # Use a targeted redactor that only modifies values containing "deep"
        # so structural keys pass through unchanged
        result = _sanitize_blocks(blocks, lambda s: (s.replace("deep", "DEEP"), []))
        # Should not raise
        assert isinstance(result, list)
        # Walk to depth boundary — containers beyond limit are dropped to {}
        node = result[0]
        for i in range(20):
            if "nested" not in node:
                break
            node = node["nested"]
        assert (
            "text" not in node
        ), f"deep leaf should have been truncated but was reached at depth {i}"
        # A shallow value SHOULD be sanitized
        shallow = [{"text": "deep_value"}]
        result2 = _sanitize_blocks(shallow, lambda s: (s.replace("deep", "DEEP"), []))
        assert result2[0]["text"] == "DEEP_value"
