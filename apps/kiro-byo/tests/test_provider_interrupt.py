from __future__ import annotations

import threading
import time
import unittest

from kiro_byo.providers.openai_compatible import (
    OpenAICompatibleInterrupted,
    _InterruptibleResponse,
    _read_response_bytes,
)


class _BlockingResponse:
    def __init__(self) -> None:
        self.closed = False
        self.released = threading.Event()

    def read(self) -> bytes:
        self.released.wait(2)
        if self.closed:
            raise ValueError("I/O operation on closed response")
        return b"{}"

    def close(self) -> None:
        self.closed = True
        self.released.set()


class ProviderInterruptTests(unittest.TestCase):
    def test_interrupt_closes_blocked_http_response(self) -> None:
        response = _BlockingResponse()
        interrupt = threading.Event()
        outcome: list[BaseException] = []

        def worker() -> None:
            try:
                with _InterruptibleResponse(response, interrupt.is_set) as live:
                    _read_response_bytes(live, interrupt.is_set)
            except BaseException as exc:  # test captures worker exception
                outcome.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(0.08)
        interrupt.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(response.closed)
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], OpenAICompatibleInterrupted)


if __name__ == "__main__":
    unittest.main()
