from __future__ import annotations

import http.client
import inspect
import unittest

from kiro_byo.providers import moonshot_chat, qwen_chat, xiaomi_mimo, zhipu_chat
from kiro_byo.providers.base import ProviderTransportError
from kiro_byo.providers.http_sse import iter_response_lines, read_response_bytes
from kiro_byo.providers.qwen_chat import QwenChatInterrupted


class _ClosedResponse:
    def read(self) -> bytes:
        raise ValueError("I/O operation on closed response")




class _IncompleteResponse:
    def read(self) -> bytes:
        raise http.client.IncompleteRead(b"", 8)


class _IncompleteStream:
    def __iter__(self):
        yield b"data: first\n"
        raise http.client.IncompleteRead(b"", 4)


class ProviderSharedTransportTests(unittest.TestCase):
    def test_native_chat_adapters_delegate_wire_engine(self) -> None:
        for module in (zhipu_chat, moonshot_chat, qwen_chat, xiaomi_mimo):
            with self.subTest(module=module.__name__):
                source = inspect.getsource(module)
                self.assertIn("run_native_chat_completion", source)
                self.assertNotIn("for raw_line in", source)
                self.assertNotIn("urllib.error", source)
                self.assertNotIn("json.loads", source)

    def test_shared_read_raises_callers_interruption_type(self) -> None:
        with self.assertRaises(QwenChatInterrupted):
            read_response_bytes(
                _ClosedResponse(),
                lambda: True,
                interrupted_type=QwenChatInterrupted,
            )

    def test_incomplete_nonstream_body_is_normalized_as_transport_error(self) -> None:
        with self.assertRaises(ProviderTransportError) as ctx:
            read_response_bytes(_IncompleteResponse(), lambda: False)
        self.assertIn("incomplete read", str(ctx.exception).lower())
        self.assertEqual(ctx.exception.category, "transport")
        self.assertTrue(ctx.exception.retryable)

    def test_incomplete_stream_is_normalized_after_prior_lines(self) -> None:
        stream = iter_response_lines(_IncompleteStream(), lambda: False)
        self.assertEqual(next(stream), b"data: first\n")
        with self.assertRaises(ProviderTransportError):
            next(stream)


if __name__ == "__main__":
    unittest.main()
