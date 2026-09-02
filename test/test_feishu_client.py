"""Tests for kiro_crew.feishu.client."""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import threading
import types
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Fake lark_oapi SDK -- installed into sys.modules for the duration of tests
# ---------------------------------------------------------------------------


class _FakeLogLevel:
    WARNING = "WARNING"


class _FakeClientBuilder:
    def app_id(self, v: str) -> "_FakeClientBuilder":
        self._app_id = v
        return self

    def app_secret(self, v: str) -> "_FakeClientBuilder":
        self._app_secret = v
        return self

    def log_level(self, v: Any) -> "_FakeClientBuilder":
        return self

    def build(self) -> "_FakeRestClient":
        return _FakeRestClient()


class _FakeImReply:
    """Stub for lark.im.v1.message.reply -- records calls."""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.succeed = True
        # Fail every call from this index on (None = never). Lets a test drive a
        # multi-chunk send where an early chunk lands and a later one fails.
        self.fail_after: int | None = None

    def reply(self, req: Any) -> "_FakeReplyResp":
        self.calls.append(req)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            return _FakeReplyResp(False)
        return _FakeReplyResp(self.succeed)


class _FakeReplyResp:
    def __init__(self, ok: bool) -> None:
        self._ok = ok
        self.code = 0 if ok else 99999
        self.msg = "" if ok else "fake error"

    def success(self) -> bool:
        return self._ok


class _FakeV1:
    def __init__(self) -> None:
        self.message = _FakeImReply()


class _FakeIm:
    def __init__(self) -> None:
        self.v1 = _FakeV1()


class _FakeRestClient:
    """Stub for lark.Client -- records reply calls via .im.v1.message.reply."""

    def __init__(self) -> None:
        self.im = _FakeIm()

    @classmethod
    def builder(cls) -> _FakeClientBuilder:
        return _FakeClientBuilder()


class _FakeEventDispatcherHandlerBuilder:
    def __init__(self, *args: Any) -> None:
        self._handler: Any = None

    def register_p2_im_message_receive_v1(
        self, handler: Any
    ) -> "_FakeEventDispatcherHandlerBuilder":
        self._handler = handler
        return self

    def build(self) -> "_FakeEventDispatcherHandlerBuilder":
        return self


class _FakeEventDispatcherHandler:
    @staticmethod
    def builder(*args: Any) -> _FakeEventDispatcherHandlerBuilder:
        return _FakeEventDispatcherHandlerBuilder(*args)


class _FakeWSClient:
    """Stub for lark.ws.Client -- start() blocks on an event, stop() sets it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._stop_event = threading.Event()
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True
        self._stop_event.wait(timeout=2.0)

    def stop(self) -> None:
        self.stopped = True
        self._stop_event.set()


class _FakeWSClientRaising(_FakeWSClient):
    """stop() raises to exercise the tolerant close() path."""

    def stop(self) -> None:
        self.stopped = True
        self._stop_event.set()
        raise RuntimeError("ws stop boom")


class _FakeWS:
    """Namespace for lark_oapi.ws containing Client."""

    Client = _FakeWSClient


# -- Reply request stubs ---------------------------------------------------


class _FakeReplyMessageRequestBuilder:
    def __init__(self) -> None:
        self._message_id: str = ""
        self._body: Any = None

    def message_id(self, v: str) -> "_FakeReplyMessageRequestBuilder":
        self._message_id = v
        return self

    def request_body(self, v: Any) -> "_FakeReplyMessageRequestBuilder":
        self._body = v
        return self

    def build(self) -> "_FakeReplyMessageRequestBuilder":
        return self


class _FakeReplyMessageRequest:
    @staticmethod
    def builder() -> _FakeReplyMessageRequestBuilder:
        return _FakeReplyMessageRequestBuilder()


class _FakeReplyMessageRequestBodyBuilder:
    def __init__(self) -> None:
        self._content: str = ""
        self._msg_type: str = ""

    def content(self, v: str) -> "_FakeReplyMessageRequestBodyBuilder":
        self._content = v
        return self

    def msg_type(self, v: str) -> "_FakeReplyMessageRequestBodyBuilder":
        self._msg_type = v
        return self

    def build(self) -> "_FakeReplyMessageRequestBodyBuilder":
        return self


class _FakeReplyMessageRequestBody:
    @staticmethod
    def builder() -> _FakeReplyMessageRequestBodyBuilder:
        return _FakeReplyMessageRequestBodyBuilder()


# ---------------------------------------------------------------------------
# Fixture: inject fake lark_oapi into sys.modules
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fake_lark_sdk():
    """Install stub lark_oapi modules; remove them after the test."""
    lark_mod = types.ModuleType("lark_oapi")
    lark_mod.Client = _FakeRestClient  # type: ignore[attr-defined]
    lark_mod.LogLevel = _FakeLogLevel  # type: ignore[attr-defined]
    lark_mod.EventDispatcherHandler = _FakeEventDispatcherHandler  # type: ignore[attr-defined]
    lark_mod.ws = _FakeWS  # type: ignore[attr-defined]

    im_v1_mod = types.ModuleType("lark_oapi.api.im.v1")
    im_v1_mod.ReplyMessageRequest = _FakeReplyMessageRequest  # type: ignore[attr-defined]
    im_v1_mod.ReplyMessageRequestBody = _FakeReplyMessageRequestBody  # type: ignore[attr-defined]

    api_mod = types.ModuleType("lark_oapi.api")
    im_mod = types.ModuleType("lark_oapi.api.im")

    originals = {}
    keys = ["lark_oapi", "lark_oapi.api", "lark_oapi.api.im", "lark_oapi.api.im.v1"]
    for k in keys:
        originals[k] = sys.modules.get(k)

    sys.modules["lark_oapi"] = lark_mod
    sys.modules["lark_oapi.api"] = api_mod
    sys.modules["lark_oapi.api.im"] = im_mod
    sys.modules["lark_oapi.api.im.v1"] = im_v1_mod

    yield

    for k in keys:
        if originals[k] is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = originals[k]

    # Force-remove cached import in the client module if it was imported
    if "kiro_crew.feishu.client" in sys.modules:
        mod = sys.modules["kiro_crew.feishu.client"]
        # Clear any cached lark ref from module-level
        if hasattr(mod, "_lark_mod_cache"):
            delattr(mod, "_lark_mod_cache")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    message_id: str = "msg-1",
    message_type: str = "text",
    content: str | None = None,
    open_id: str = "ou_abc123",
    chat_type: str = "p2p",
    chat_id: str = "",
    mentions: list[tuple[str, str]] | None = None,
) -> Any:
    """Build a fake P2ImMessageReceiveV1 data object matching lark-oapi shape."""
    if content is None:
        content = json.dumps({"text": "hello"})

    class SenderID:
        pass

    class Sender:
        pass

    class Message:
        pass

    class Event:
        pass

    class Data:
        pass

    sid = SenderID()
    sid.open_id = open_id  # type: ignore[attr-defined]

    sender = Sender()
    sender.sender_id = sid  # type: ignore[attr-defined]

    msg = Message()
    msg.message_id = message_id  # type: ignore[attr-defined]
    msg.message_type = message_type  # type: ignore[attr-defined]
    msg.content = content  # type: ignore[attr-defined]
    msg.chat_type = chat_type  # type: ignore[attr-defined]
    msg.chat_id = chat_id  # type: ignore[attr-defined]

    class Mention:
        pass

    mention_objs = []
    for key, name in mentions or []:
        m = Mention()
        m.key = key  # type: ignore[attr-defined]
        m.name = name  # type: ignore[attr-defined]
        mention_objs.append(m)
    msg.mentions = mention_objs or None  # type: ignore[attr-defined]

    event = Event()
    event.message = msg  # type: ignore[attr-defined]
    event.sender = sender  # type: ignore[attr-defined]

    data = Data()
    data.event = event  # type: ignore[attr-defined]
    return data


# ---------------------------------------------------------------------------
# Tests: __init__
# ---------------------------------------------------------------------------


class TestInit:
    """LarkClient.__init__ builds the REST client via the stub SDK."""

    def test_builds_rest_client(self) -> None:
        from kiro_crew.feishu.client import LarkClient

        client = LarkClient(app_id="aid", app_secret="asec")
        assert client._lark is not None
        assert client._app_id == "aid"
        assert client._app_secret == "asec"

    def test_import_error_without_sdk(self) -> None:
        """When lark_oapi is absent, ImportError with install hint is raised."""
        # Temporarily remove the fake SDK
        saved = {}
        keys = [
            "lark_oapi",
            "lark_oapi.api",
            "lark_oapi.api.im",
            "lark_oapi.api.im.v1",
        ]
        for k in keys:
            saved[k] = sys.modules.pop(k, None)

        try:
            # Patch the import inside __init__ by making the lazy import fail.
            # The code does `import lark_oapi as lark` at the top of __init__.
            # With no lark_oapi in sys.modules, a fresh import will raise.
            # But since the module is already imported, we use a different
            # approach: patch builtins.__import__ to block lark_oapi.
            import builtins

            from kiro_crew.feishu.client import LarkClient

            original_import = builtins.__import__

            def _blocking_import(name: str, *args: Any, **kwargs: Any) -> Any:
                if name == "lark_oapi" or name.startswith("lark_oapi."):
                    raise ImportError("No module named 'lark_oapi'")
                return original_import(name, *args, **kwargs)

            builtins.__import__ = _blocking_import
            try:
                with pytest.raises(ImportError, match="lark-oapi"):
                    LarkClient(app_id="a", app_secret="s")
            finally:
                builtins.__import__ = original_import
        finally:
            # Restore fake SDK modules
            for k in keys:
                if saved[k] is not None:
                    sys.modules[k] = saved[k]


# ---------------------------------------------------------------------------
# Tests: send_reply
# ---------------------------------------------------------------------------


class TestSendReply:
    """send_reply: happy path, truncation, error tolerance."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_true(self) -> None:
        from kiro_crew.feishu.client import LarkClient

        client = LarkClient(app_id="a", app_secret="s")
        result = await client.send_reply("msg-1", "Hello")
        assert result is True

    @pytest.mark.asyncio
    async def test_long_reply_is_chunked_not_truncated(self) -> None:
        """A reply over the per-message cap is SPLIT; no character is dropped."""
        from kiro_crew.feishu.client import FEISHU_MAX_TEXT, LarkClient

        client = LarkClient(app_id="a", app_secret="s")
        long_text = "x" * (FEISHU_MAX_TEXT + 100)
        assert await client.send_reply("msg-1", long_text) is True

        reply_mock = client._lark.im.v1.message
        assert len(reply_mock.calls) == 2, "expected one chunk per cap-sized slice"
        sent = [json.loads(req._body._content)["text"] for req in reply_mock.calls]
        # Every chunk respects the transport cap...
        assert all(len(s) <= FEISHU_MAX_TEXT for s in sent)
        # ...and concatenating them reproduces the answer EXACTLY -- the point of
        # chunking over truncating is that nothing is silently lost.
        assert "".join(sent) == long_text
        assert not any(s.endswith("...") for s in sent)

    @pytest.mark.asyncio
    async def test_chunk_failure_stops_and_reports_false(self) -> None:
        """A mid-sequence REST failure returns False rather than half-success."""
        from kiro_crew.feishu.client import FEISHU_MAX_TEXT, LarkClient

        client = LarkClient(app_id="a", app_secret="s")
        reply_mock = client._lark.im.v1.message
        reply_mock.fail_after = 1  # first chunk lands, second raises

        assert await client.send_reply("msg-1", "y" * (FEISHU_MAX_TEXT * 3)) is False
        # Stopped at the failure instead of pressing on through the remainder.
        assert len(reply_mock.calls) == 2

    @pytest.mark.asyncio
    async def test_failure_returns_false(self) -> None:
        from kiro_crew.feishu.client import LarkClient

        client = LarkClient(app_id="a", app_secret="s")
        # Make the reply fail
        client._lark.im.v1.message.succeed = False
        result = await client.send_reply("msg-1", "Hi")
        assert result is False


# ---------------------------------------------------------------------------
# Tests: send_reply -- fence safety (split_markdown_safe integration)
# ---------------------------------------------------------------------------


def _fence_balanced(chunk: str) -> bool:
    """True when *chunk* has no unterminated fenced code block.

    A fence opener is <=3 spaces indent + >=3 backticks/tildes + optional info.
    A closer is the same char at least as long with nothing else on the line.
    We walk line-by-line through the real grammar (same as split.py) to count
    balanced open/close pairs.
    """
    import re as _re

    _open_bt = _re.compile(r"^ {0,3}(`{3,})[^`]*$")
    _open_tl = _re.compile(r"^ {0,3}(~{3,}).*$")
    _close = _re.compile(r"^ {0,3}((?:`{3,})|(?:~{3,}))[ \t]*$")

    fence_char: str | None = None
    fence_len: int = 0

    for raw_line in chunk.split("\n"):
        line = raw_line.rstrip("\r")
        if fence_char is not None:
            m = _close.match(line)
            if m and m.group(1)[0] == fence_char and len(m.group(1)) >= fence_len:
                fence_char = None
                fence_len = 0
        else:
            m = _open_bt.match(line) or _open_tl.match(line)
            if m:
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))

    return fence_char is None


def _content_lines(text: str) -> list[str]:
    """Extract non-fence-delimiter content lines from a markdown string."""
    import re as _re

    _open_bt = _re.compile(r"^ {0,3}(`{3,})[^`]*$")
    _open_tl = _re.compile(r"^ {0,3}(~{3,}).*$")
    _close = _re.compile(r"^ {0,3}((?:`{3,})|(?:~{3,}))[ \t]*$")

    lines: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.rstrip("\r")
        if _open_bt.match(line) or _open_tl.match(line) or _close.match(line):
            continue
        lines.append(line)
    return lines


class TestSendReplyFenceSafety:
    """Fence-safe splitting: code blocks survive chunking intact."""

    @pytest.mark.asyncio
    async def test_long_reply_with_fence_produces_balanced_chunks(self) -> None:
        """Every chunk of a fenced-code reply is independently fence-balanced."""
        from kiro_crew.feishu.client import FEISHU_MAX_TEXT, LarkClient

        client = LarkClient(app_id="a", app_secret="s")

        # Build a reply that exceeds the cap and contains a fenced code block.
        code_body = "print('hello world')\n" * 200  # plenty of lines
        text = (
            "Here is some code:\n\n"
            "```python\n"
            f"{code_body}"
            "```\n\n"
            "And some trailing prose.\n"
        )
        assert len(text) > FEISHU_MAX_TEXT, "precondition: text must exceed cap"

        assert await client.send_reply("msg-1", text) is True

        reply_mock = client._lark.im.v1.message
        assert len(reply_mock.calls) >= 2, "expected multiple chunks"

        sent = [json.loads(req._body._content)["text"] for req in reply_mock.calls]

        # Every chunk except the LAST must be fence-balanced.
        # The splitter deliberately leaves the final chunk open (streaming
        # contract), so we check all-but-last strictly and note the final.
        for i, chunk in enumerate(sent[:-1]):
            assert _fence_balanced(chunk), f"Chunk {i} is NOT fence-balanced:\n{chunk[:200]!r}..."

    @pytest.mark.asyncio
    async def test_long_reply_with_fence_preserves_content_lines(self) -> None:
        """All original code-block content lines survive in order across chunks."""
        from kiro_crew.feishu.client import FEISHU_MAX_TEXT, LarkClient

        client = LarkClient(app_id="a", app_secret="s")

        # Build distinctive content lines so we can verify ordering.
        code_lines = [f"line_{i:04d} = {i}" for i in range(400)]
        code_body = "\n".join(code_lines) + "\n"
        text = "Preamble text here.\n\n" "```python\n" f"{code_body}" "```\n\n" "Postamble.\n"
        assert len(text) > FEISHU_MAX_TEXT

        assert await client.send_reply("msg-1", text) is True

        reply_mock = client._lark.im.v1.message
        sent = [json.loads(req._body._content)["text"] for req in reply_mock.calls]

        # Concatenate all chunks and extract content lines from the result.
        reassembled = "".join(sent)
        result_content = _content_lines(reassembled)

        # Every original code line must appear in order in the result content.
        # The splitter may ADD reopener/closer scaffolding lines, so we don't
        # assert byte-for-byte equality -- we assert subsequence inclusion.
        original_code = code_lines  # without trailing newlines
        j = 0
        for content_line in result_content:
            if j < len(original_code) and content_line == original_code[j]:
                j += 1
        assert j == len(
            original_code
        ), f"Only found {j}/{len(original_code)} original code lines in order"

    @pytest.mark.asyncio
    async def test_short_reply_with_fence_sent_as_one_message(self) -> None:
        """A reply under the cap containing a fence is sent untouched."""
        from kiro_crew.feishu.client import FEISHU_MAX_TEXT, LarkClient

        client = LarkClient(app_id="a", app_secret="s")

        text = "Look:\n\n```bash\necho hi\n```\n\nDone.\n"
        assert len(text) < FEISHU_MAX_TEXT

        assert await client.send_reply("msg-1", text) is True

        reply_mock = client._lark.im.v1.message
        assert len(reply_mock.calls) == 1
        sent = json.loads(reply_mock.calls[0]._body._content)["text"]
        assert sent == text


# ---------------------------------------------------------------------------
# Tests: _sync_reply
# ---------------------------------------------------------------------------


class TestSyncReply:
    """_sync_reply builds correct request and raises on failure."""

    def test_raises_runtime_error_on_failure(self) -> None:
        from kiro_crew.feishu.client import LarkClient

        client = LarkClient(app_id="a", app_secret="s")
        client._lark.im.v1.message.succeed = False
        with pytest.raises(RuntimeError, match="Feishu reply error"):
            client._sync_reply("msg-1", "text")

    def test_happy_path_no_raise(self) -> None:
        from kiro_crew.feishu.client import LarkClient

        client = LarkClient(app_id="a", app_secret="s")
        # Should not raise
        client._sync_reply("msg-1", "text")


# ---------------------------------------------------------------------------
# Tests: _handle_receive_v1
# ---------------------------------------------------------------------------


class TestHandleReceiveV1:
    """Inbound message handling: dispatch, dedup, filtering."""

    @pytest.mark.asyncio
    async def test_well_formed_text_dispatches(self) -> None:
        from kiro_crew.feishu.client import LarkClient

        received: list[Any] = []

        async def handler(inbound: Any) -> None:
            received.append(inbound)

        client = LarkClient(app_id="a", app_secret="s", on_message=handler)
        client._loop = asyncio.get_running_loop()

        data = _make_event(
            message_id="m1",
            content=json.dumps({"text": "@_user_1 hello world"}),
            open_id="ou_xyz",
            chat_type="group",
            chat_id="chat-99",
        )
        client._handle_receive_v1(data)
        await asyncio.sleep(0.05)

        assert len(received) == 1
        inbound = received[0]
        assert inbound.open_id == "ou_xyz"
        assert inbound.text == "hello world"  # @mention stripped
        assert inbound.message_id == "m1"
        assert inbound.chat_type == "group"
        assert inbound.chat_id == "chat-99"

    @pytest.mark.asyncio
    async def test_non_text_message_type_ignored(self) -> None:
        from kiro_crew.feishu.client import LarkClient

        received: list[Any] = []

        async def handler(inbound: Any) -> None:
            received.append(inbound)

        client = LarkClient(app_id="a", app_secret="s", on_message=handler)
        client._loop = asyncio.get_running_loop()

        data = _make_event(message_id="img-1", message_type="image")
        client._handle_receive_v1(data)
        await asyncio.sleep(0.05)

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_no_open_id_ignored(self) -> None:
        from kiro_crew.feishu.client import LarkClient

        received: list[Any] = []

        async def handler(inbound: Any) -> None:
            received.append(inbound)

        client = LarkClient(app_id="a", app_secret="s", on_message=handler)
        client._loop = asyncio.get_running_loop()

        data = _make_event(message_id="no-sender", open_id="")
        client._handle_receive_v1(data)
        await asyncio.sleep(0.05)

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_invalid_json_content_ignored(self) -> None:
        from kiro_crew.feishu.client import LarkClient

        received: list[Any] = []

        async def handler(inbound: Any) -> None:
            received.append(inbound)

        client = LarkClient(app_id="a", app_secret="s", on_message=handler)
        client._loop = asyncio.get_running_loop()

        data = _make_event(message_id="bad-json", content="not-json{{{")
        client._handle_receive_v1(data)
        await asyncio.sleep(0.05)

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_whitespace_only_after_mention_strip_ignored(self) -> None:
        from kiro_crew.feishu.client import LarkClient

        received: list[Any] = []

        async def handler(inbound: Any) -> None:
            received.append(inbound)

        client = LarkClient(app_id="a", app_secret="s", on_message=handler)
        client._loop = asyncio.get_running_loop()

        data = _make_event(
            message_id="ws-only",
            content=json.dumps({"text": "@_user_1   "}),
        )
        client._handle_receive_v1(data)
        await asyncio.sleep(0.05)

        assert len(received) == 0


# ---------------------------------------------------------------------------
# Tests: _handle_receive_v1 drop logging
# ---------------------------------------------------------------------------


class TestHandleReceiveV1DropLogging:
    """Every silent-drop branch must emit a log line naming the reason.

    Without a log line a dropped inbound message leaves no trace anywhere --
    nothing in gateway.log, nothing in the SEL audit log -- making a
    delivered-and-ignored message indistinguishable from a dead WebSocket. Each
    drop logs at INFO with its reason (and message_id where one is known), so
    diagnosis is a one-minute read rather than an hours-long elimination.
    """

    def _make_client(self):
        from kiro_crew.feishu.client import LarkClient

        received: list[Any] = []

        async def handler(inbound: Any) -> None:
            received.append(inbound)

        client = LarkClient(app_id="a", app_secret="s", on_message=handler)
        return client, received

    def test_missing_event_logs(self, caplog: Any) -> None:
        client, received = self._make_client()

        class Data:
            event = None

        with caplog.at_level("INFO", logger="kiro_crew.feishu.client"):
            client._handle_receive_v1(Data())

        assert received == []
        assert any("no event" in r.message for r in caplog.records)

    def test_missing_message_logs(self, caplog: Any) -> None:
        client, received = self._make_client()

        class Event:
            message = None

        class Data:
            event = Event()

        with caplog.at_level("INFO", logger="kiro_crew.feishu.client"):
            client._handle_receive_v1(Data())

        assert received == []
        assert any("no message" in r.message for r in caplog.records)

    def test_missing_message_id_logs(self, caplog: Any) -> None:
        client, received = self._make_client()
        data = _make_event(message_id="")

        with caplog.at_level("INFO", logger="kiro_crew.feishu.client"):
            client._handle_receive_v1(data)

        assert received == []
        assert any("no message_id" in r.message for r in caplog.records)

    def test_non_text_message_type_logs_type_and_id(self, caplog: Any) -> None:
        client, received = self._make_client()
        data = _make_event(message_id="img-1", message_type="image")

        with caplog.at_level("INFO", logger="kiro_crew.feishu.client"):
            client._handle_receive_v1(data)

        assert received == []
        drop = next((r for r in caplog.records if "unsupported message_type" in r.message), None)
        assert drop is not None
        # The offending type and the message_id are both in the rendered line.
        assert "image" in drop.getMessage()
        assert "img-1" in drop.getMessage()

    def test_missing_open_id_logs(self, caplog: Any) -> None:
        client, received = self._make_client()
        data = _make_event(message_id="no-sender", open_id="")

        with caplog.at_level("INFO", logger="kiro_crew.feishu.client"):
            client._handle_receive_v1(data)

        assert received == []
        assert any("open_id" in r.message for r in caplog.records)

    def test_invalid_json_logs(self, caplog: Any) -> None:
        client, received = self._make_client()
        data = _make_event(message_id="bad-json", content="not-json{{{")

        with caplog.at_level("INFO", logger="kiro_crew.feishu.client"):
            client._handle_receive_v1(data)

        assert received == []
        assert any("not valid JSON" in r.message for r in caplog.records)

    def test_mention_only_body_logs(self, caplog: Any) -> None:
        client, received = self._make_client()
        data = _make_event(
            message_id="ws-only",
            content=json.dumps({"text": "@_user_1   "}),
        )

        with caplog.at_level("INFO", logger="kiro_crew.feishu.client"):
            client._handle_receive_v1(data)

        assert received == []
        assert any("mention-only" in r.message for r in caplog.records)

    def test_empty_resolved_text_logs(self, caplog: Any, monkeypatch: Any) -> None:
        """The empty-resolved-text guard also logs its drop.

        Reaching this branch organically is hard -- any non-mention text keeps
        the resolved text non-empty, and a mention-only body drops one guard
        earlier. It is a defensive guard, so we drive it directly by forcing
        ``_resolve_mentions`` to return whitespace for a body that clears the
        mention-only check.
        """
        import kiro_crew.feishu.client as client_mod

        client, received = self._make_client()
        monkeypatch.setattr(client_mod, "_resolve_mentions", lambda raw, mentions: "   ")
        data = _make_event(
            message_id="empty-resolved",
            content=json.dumps({"text": "real words here"}),
        )

        with caplog.at_level("INFO", logger="kiro_crew.feishu.client"):
            client._handle_receive_v1(data)

        assert received == []
        assert any("resolved text is empty" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Tests: start()
# ---------------------------------------------------------------------------


class TestStart:
    """start() spawns a daemon thread and stores the WS client."""

    @pytest.mark.asyncio
    async def test_start_spawns_thread(self) -> None:
        from kiro_crew.feishu.client import LarkClient

        client = LarkClient(app_id="a", app_secret="s")
        await client.start()

        assert client._ws_client is not None
        assert client._thread is not None
        assert client._thread.daemon is True
        assert client._thread.is_alive()

        # Clean up: stop and join
        client._ws_client.stop()
        client._thread.join(timeout=1.0)
        assert not client._thread.is_alive()


# ---------------------------------------------------------------------------
# Tests: health transitions (on_state_change)
# ---------------------------------------------------------------------------


class TestHealthTransitions:
    """The Settings badge is driven by these transitions, so they are contract.

    Without them a rejected app id/secret leaves the badge reading "connected"
    forever: lark-oapi exposes no connect event, and the only evidence of a
    refusal is the receiver thread ending seconds after start().
    """

    @pytest.mark.asyncio
    async def test_start_reports_healthy_then_close_reports_down(self) -> None:
        from kiro_crew.feishu.client import LarkClient

        client = LarkClient(app_id="a", app_secret="s")
        seen: list[tuple[bool, str]] = []
        client.on_state_change = lambda ok, why: seen.append((ok, why))

        await client.start()
        assert seen[0] == (True, "")

        await client.close()
        client._thread.join(timeout=1.0)
        # An intentional shutdown is "not connected" with NO reason: nothing
        # failed, and inventing one would show a false error in Settings.
        assert seen[-1] == (False, "")

    @pytest.mark.asyncio
    async def test_receiver_exit_reports_the_reason(self) -> None:
        """A receiver that returns on its own is the bad-credential signal."""
        from kiro_crew.feishu.client import LarkClient

        client = LarkClient(app_id="a", app_secret="s")
        seen: list[tuple[bool, str]] = []
        client.on_state_change = lambda ok, why: seen.append((ok, why))

        await client.start()
        # End the receive loop WITHOUT going through client.close(), so
        # `_closed` stays False — exactly the shape lark produces for a refused
        # app: ws.start() returns on its own while the channel still believes it
        # is running. Driving the stub directly keeps this deterministic rather
        # than racing its internal timeout.
        client._ws_client.stop()
        client._thread.join(timeout=5.0)
        assert not client._thread.is_alive()

        assert seen[0] == (True, "")
        assert seen[-1][0] is False
        assert "receiver stopped" in seen[-1][1]

    def test_transitions_are_deduped_but_the_first_always_publishes(self) -> None:
        from kiro_crew.feishu.client import LarkClient

        client = LarkClient(app_id="a", app_secret="s")
        seen: list[tuple[bool, str]] = []
        client.on_state_change = lambda ok, why: seen.append((ok, why))

        client._notify_state(False, "")
        client._notify_state(False, "")
        assert seen == [(False, "")], "the first report publishes even if unhealthy"

        client._notify_state(False, "first reason")
        client._notify_state(False, "first reason")
        # A repeat must not overwrite the FIRST reason with an identical later
        # one, and must not spam the observer.
        assert seen == [(False, ""), (False, "first reason")]

    def test_an_observer_that_raises_cannot_break_the_receiver(self) -> None:
        from kiro_crew.feishu.client import LarkClient

        client = LarkClient(app_id="a", app_secret="s")

        def _boom(ok: bool, why: str) -> None:
            raise RuntimeError("observer blew up")

        client.on_state_change = _boom
        # Swallowed: the badge is a side channel, and losing it must never take
        # the channel down with it.
        client._notify_state(True, "")


# ---------------------------------------------------------------------------
# Tests: close()
# ---------------------------------------------------------------------------


class TestClose:
    """close() sets the flag, stops the ws, shuts executor down."""

    @pytest.mark.asyncio
    async def test_close_sets_flag_and_stops(self) -> None:
        from kiro_crew.feishu.client import LarkClient

        client = LarkClient(app_id="a", app_secret="s")
        await client.start()

        assert client._closed is False
        await client.close()

        assert client._closed is True
        assert client._ws_client.stopped is True
        # Thread should exit since stop() sets the event
        client._thread.join(timeout=1.0)
        assert not client._thread.is_alive()

    @pytest.mark.asyncio
    async def test_ws_stop_is_offloaded_off_the_event_loop_thread(self) -> None:
        """The synchronous ws.stop() must not run on the gateway event loop.

        lark-oapi's stop() closes the socket and can block on a wedged peer.
        On the loop that freezes every task, and the caller's asyncio.wait_for
        cannot rescue it — a timeout only fires at an await point, so a
        synchronous call inside the coroutine runs to completion regardless.
        Asserting the calling THREAD (rather than trusting the source line)
        keeps the guarantee even if the call is refactored.
        """
        import threading

        from kiro_crew.feishu.client import LarkClient

        client = LarkClient(app_id="a", app_secret="s")
        await client.start()

        loop_thread_id = threading.get_ident()
        stop_thread_ids: list[int] = []
        real_stop = client._ws_client.stop

        def _recording_stop() -> None:
            stop_thread_ids.append(threading.get_ident())
            real_stop()

        client._ws_client.stop = _recording_stop  # type: ignore[method-assign]

        await client.close()

        assert stop_thread_ids, "ws.stop() was never called"
        assert loop_thread_id not in stop_thread_ids, (
            "ws.stop() ran on the event loop thread — a wedged peer would "
            "freeze the whole gateway, not just this shutdown"
        )
        client._thread.join(timeout=1.0)

    @pytest.mark.asyncio
    async def test_close_tolerates_ws_stop_raising(self) -> None:
        from kiro_crew.feishu.client import LarkClient

        # Patch ws module to use the raising variant
        lark_mod = sys.modules["lark_oapi"]

        class _RaisingWS:
            Client = _FakeWSClientRaising

        original_ws = lark_mod.ws  # type: ignore[attr-defined]
        lark_mod.ws = _RaisingWS  # type: ignore[attr-defined]
        try:
            client = LarkClient(app_id="a", app_secret="s")
            await client.start()
            # Should not raise
            await client.close()
            assert client._closed is True
            client._thread.join(timeout=1.0)
        finally:
            lark_mod.ws = original_ws  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_close_without_start(self) -> None:
        """close() on a never-started client does not raise."""
        from kiro_crew.feishu.client import LarkClient

        client = LarkClient(app_id="a", app_secret="s")
        await client.close()
        assert client._closed is True


# ---------------------------------------------------------------------------
# Tests: threshold clamping
# ---------------------------------------------------------------------------


class TestThresholdClamp:
    """FeishuConfig.__post_init__ delegates to the shared _normalize_threshold_pair,
    so Feishu inherits the 1..100 range and the soft <= hard ordering rather than
    carrying its own copy that can drift from the other channels."""

    def test_soft_above_hard_is_lowered_to_hard(self) -> None:
        """Otherwise the hard branch wins and the soft nudge never fires."""
        from kiro_crew.config.loader import FeishuConfig

        c = FeishuConfig(soft_threshold_pct=95, hard_threshold_pct=50)
        assert c.soft_threshold_pct == 50
        assert c.hard_threshold_pct == 50

    def test_out_of_range_values_clamped(self) -> None:
        """Above 100 saturates; below the floor lands ON the floor, not at 0."""
        from kiro_crew.config.loader import FeishuConfig

        c = FeishuConfig(soft_threshold_pct=-10, hard_threshold_pct=200)
        assert c.soft_threshold_pct == 1
        assert c.hard_threshold_pct == 100

    def test_zero_hard_threshold_is_raised_off_zero(self) -> None:
        """A 0% hard threshold reads as "always over" in ``_maybe_notice``
        (``pct >= hard``), so every single turn would force a compaction and
        throw away the conversation. The floor is 1, which is why the shared
        ``_clamp_pct`` -- not a hand-rolled ``max(0, ...)`` -- is the only
        correct statement of this range."""
        from kiro_crew.config.loader import FeishuConfig

        c = FeishuConfig(soft_threshold_pct=0, hard_threshold_pct=0)
        assert c.hard_threshold_pct == 1
        assert c.soft_threshold_pct == 1

    def test_matches_the_sibling_channel_configs_exactly(self) -> None:
        """Pins the delegation itself: the same input must normalize identically
        for Feishu and for a channel that already uses the shared helper, so a
        future edit cannot silently reintroduce a private clamp."""
        from kiro_crew.config.loader import FeishuConfig, WeixinConfig

        for soft, hard in ((0, 0), (-10, 200), (95, 50), (80, 95), (100, 100)):
            f = FeishuConfig(soft_threshold_pct=soft, hard_threshold_pct=hard)
            w = WeixinConfig(soft_threshold_pct=soft, hard_threshold_pct=hard)
            assert (f.soft_threshold_pct, f.hard_threshold_pct) == (
                w.soft_threshold_pct,
                w.hard_threshold_pct,
            ), f"drift at soft={soft} hard={hard}"


# ---------------------------------------------------------------------------
# Tests: mention resolution
# ---------------------------------------------------------------------------


class TestMentionResolution:
    """Mentions become names so an instruction naming a third party survives."""

    @pytest.mark.asyncio
    async def test_third_party_mention_becomes_a_name(self) -> None:
        from kiro_crew.feishu.client import LarkClient

        seen: list[str] = []

        async def handler(inbound) -> None:
            seen.append(inbound.text)

        client = LarkClient(app_id="a", app_secret="s", on_message=handler)
        client._loop = asyncio.get_running_loop()
        client._handle_receive_v1(
            _make_event(
                content=json.dumps({"text": "@_user_1 ask @_user_2 to review"}),
                mentions=[("@_user_1", "Bot"), ("@_user_2", "Alice")],
            )
        )
        await asyncio.sleep(0.05)

        # The colleague's name survives instead of being deleted.
        assert seen == ["@Bot ask @Alice to review"]

    @pytest.mark.asyncio
    async def test_command_text_strips_the_mention_so_group_commands_work(self) -> None:
        """A group `/new` arrives mentioning the bot; both forms must be carried.

        ``text`` keeps the resolved mention (the agent should see who was
        addressed), while ``command_text`` is mention-free so the dispatcher's
        command match can fire. Deriving the second form in the dispatcher is
        impossible -- the ``@_user_N`` placeholders are gone by then.
        """
        from kiro_crew.feishu.client import LarkClient

        seen: list[tuple[str, str]] = []

        async def handler(inbound) -> None:
            seen.append((inbound.text, inbound.command_text))

        client = LarkClient(app_id="a", app_secret="s", on_message=handler)
        client._loop = asyncio.get_running_loop()
        client._handle_receive_v1(
            _make_event(
                content=json.dumps({"text": "@_user_1 /new"}),
                mentions=[("@_user_1", "FeishuBot")],
                chat_type="group",
                chat_id="oc_grp",
            )
        )
        await asyncio.sleep(0.05)

        assert seen == [("@FeishuBot /new", "/new")]

    @pytest.mark.asyncio
    async def test_a_second_mention_yields_no_command_body(self) -> None:
        """ "@Bot /new @Alice" must NOT be readable as the bare command "/new".

        Deleting every placeholder collapses it to exactly "/new", so the
        dispatcher would intercept and bump the generation -- stranding the
        conversation on a message that named a third party and was never a bare
        command. The loss is unrecoverable, so ambiguity resolves to "prompt".
        """
        from kiro_crew.feishu.client import LarkClient

        seen: list[tuple[str, str]] = []

        async def handler(inbound) -> None:
            seen.append((inbound.text, inbound.command_text))

        client = LarkClient(app_id="a", app_secret="s", on_message=handler)
        client._loop = asyncio.get_running_loop()
        client._handle_receive_v1(
            _make_event(
                content=json.dumps({"text": "@_user_1 /new @_user_2"}),
                mentions=[("@_user_1", "FeishuBot"), ("@_user_2", "Alice")],
                chat_type="group",
                chat_id="oc_grp",
            )
        )
        await asyncio.sleep(0.05)

        # Both names still reach the agent; no command body is offered.
        assert seen == [("@FeishuBot /new @Alice", "")]

    @pytest.mark.asyncio
    async def test_a_mention_that_does_not_lead_yields_no_command_body(self) -> None:
        """Only a LEADING mention can be the bot's own; mid-text is a prompt."""
        from kiro_crew.feishu.client import LarkClient

        seen: list[tuple[str, str]] = []

        async def handler(inbound) -> None:
            seen.append((inbound.text, inbound.command_text))

        client = LarkClient(app_id="a", app_secret="s", on_message=handler)
        client._loop = asyncio.get_running_loop()
        client._handle_receive_v1(
            _make_event(
                content=json.dumps({"text": "tell @_user_2 about /new"}),
                mentions=[("@_user_2", "Alice")],
                chat_type="group",
                chat_id="oc_grp",
            )
        )
        await asyncio.sleep(0.05)

        assert seen == [("tell @Alice about /new", "")]

    def test_reply_payload_is_utf8_not_escaped(self) -> None:
        """A CJK reply must serialize to its UTF-8 size, not its escaped size.

        The splitter bounds a reply in CHARACTERS; Feishu bounds the request in
        serialized payload size. With ``ensure_ascii`` left at its default every
        CJK char becomes ``\\uXXXX`` -- 6 bytes instead of 3 -- so an ordinary
        4000-char Chinese reply serializes to roughly double and the reply API
        rejects it outright. Asserted on the byte length, because character count
        is exactly the measure that hides this.
        """
        import json as _json

        from kiro_crew.feishu import client as client_mod

        body = "中" * 4000
        escaped = len(_json.dumps({"text": body}).encode("utf-8"))
        actual = len(_json.dumps({"text": body}, ensure_ascii=False).encode("utf-8"))

        assert escaped > 24_000, "fixture no longer demonstrates the expansion"
        assert actual < 13_000, f"payload still escaped ({actual} bytes)"

        src = pathlib.Path(client_mod.__file__).read_text(encoding="utf-8")
        assert (
            'json.dumps({"text": text}, ensure_ascii=False)' in src
        ), "send_reply must serialize the reply body as UTF-8"

    @pytest.mark.asyncio
    async def test_at_all_keeps_its_scope(self) -> None:
        from kiro_crew.feishu.client import LarkClient

        seen: list[str] = []

        async def handler(inbound) -> None:
            seen.append(inbound.text)

        client = LarkClient(app_id="a", app_secret="s", on_message=handler)
        client._loop = asyncio.get_running_loop()
        client._handle_receive_v1(_make_event(content=json.dumps({"text": "@_all standup now"})))
        await asyncio.sleep(0.05)

        assert seen == ["@all standup now"]

    @pytest.mark.asyncio
    async def test_unresolvable_placeholder_is_dropped(self) -> None:
        """No name available: drop it rather than leak an opaque token."""
        from kiro_crew.feishu.client import LarkClient

        seen: list[str] = []

        async def handler(inbound) -> None:
            seen.append(inbound.text)

        client = LarkClient(app_id="a", app_secret="s", on_message=handler)
        client._loop = asyncio.get_running_loop()
        client._handle_receive_v1(
            _make_event(content=json.dumps({"text": "@_user_9 do the thing"}))
        )
        await asyncio.sleep(0.05)

        assert seen == ["do the thing"]
