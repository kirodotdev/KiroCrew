"""Serve Laya on this machine's CPU for Kiro Crew's decision seam.

Runs inside the model's own environment, never the gateway's: it imports torch
and laya, which Kiro Crew does not depend on. The weights are a directory the
gateway already downloaded and verified, so nothing here reaches the network.
"""

import argparse
import asyncio
import os
import sys
import threading
from typing import Any

os.environ.setdefault("HF_HUB_OFFLINE", "1")


def _torch_threads() -> int:
    # Half the CPUs this process may run on, at most 16: one inference keeps the
    # model fast while the owner's sessions keep the other half of the machine.
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    return max(1, min(16, (cpus or 2) // 2))


TORCH_THREADS = _torch_threads()
# Read by the OpenMP runtime when torch loads, so it must be set before the import.
os.environ.setdefault("OMP_NUM_THREADS", str(TORCH_THREADS))
os.environ.setdefault("MKL_NUM_THREADS", str(TORCH_THREADS))
# How long a decision waits for the one in progress before it is refused. The
# client's own timeout is 2 s, so a request queued longer than this would be
# answered close to or after the moment its caller gave up.
QUEUE_WAIT_SECS = 1.0
# How long a decision's body may take to arrive. It is read before the decision
# waits for the one in progress, so a client that stops sending cannot hold it.
BODY_WAIT_SECS = 10.0
# Largest body read ahead: Laya's own MAX_BODY_BYTES, which refuses a larger one.
_BODY_AHEAD_MAX_BYTES = 2 * 1024 * 1024
# Decision requests buffered at once. Laya bounds this itself by admitting before
# it reads a body; the read-ahead comes first, so it carries its own bound.
_READ_AHEAD_SLOTS = 4
# The gateway counts this server ready only once it echoes this secret, so a
# program that took the port while the weights loaded is never mistaken for it.
ATTEST = os.environ.pop("KIROCREW_LOCAL_ATTEST", "")


def _wait_for_end_of_input() -> None:
    # On Windows stdin is a synchronous pipe, and a read left pending on it makes
    # other queries of that pipe wait behind it. The start-up code of numpy's
    # OpenBLAS DLL is one of them, so the model imports would never return. There
    # the pipe is polled until the gateway closes its end; no read is left
    # pending. A stdin that is not a pipe is read to its end as elsewhere.
    if sys.platform == "win32":
        import msvcrt
        import time

        import _winapi

        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        while True:
            try:
                _winapi.PeekNamedPipe(handle, 0)
            except BrokenPipeError:
                return
            except OSError:
                break
            time.sleep(0.5)
    sys.stdin.buffer.read()


def _exit_when_gateway_lets_go() -> None:
    # The gateway holds this process's stdin open for as long as it wants the
    # server. End of input -- a stop, or the gateway exiting -- ends the server,
    # so it never outlives the process that started it.
    _wait_for_end_of_input()
    os._exit(0)


threading.Thread(target=_exit_when_gateway_lets_go, daemon=True).start()


class _OneAtATime:
    """ASGI wrapper running one decision request at a time.

    Laya runs one inference at a time but queues every other request behind it,
    so a burst is still computed to the end after its clients have given up. The
    body is read first, within ``BODY_WAIT_SECS``, so a client that stops sending
    never holds the gate. A request that then cannot start within ``wait_secs``
    is answered 503 at once; the gate records it as a provider error and the
    caller falls back as it does for any refused decision.
    """

    def __init__(self, inner: Any, wait_secs: float) -> None:
        self.inner = inner
        self.wait_secs = wait_secs
        self.lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(_READ_AHEAD_SLOTS)

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http" or scope.get("path") != "/v1/systemone":
            return await self.inner(scope, receive, send)
        if self.slots.locked():
            await _reply(send, 503, b'{"detail": "busy: another decision is running"}')
            return
        async with self.slots:
            await self._decide(scope, receive, send)

    async def _decide(self, scope, receive, send):  # type: ignore[no-untyped-def]
        try:
            ahead = await asyncio.wait_for(_read_ahead(receive), timeout=BODY_WAIT_SECS)
        except asyncio.TimeoutError:
            await _reply(send, 408, b'{"detail": "request body was not received in time"}')
            return
        if ahead is None:
            await _reply(send, 413, b'{"detail": "request body too large"}')
            return
        if ahead[-1].get("type") != "http.request":
            return  # the client went away mid-body; nobody is reading a reply

        async def replay():  # type: ignore[no-untyped-def]
            if ahead:
                return ahead.pop(0)
            return await receive()

        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=self.wait_secs)
        except asyncio.TimeoutError:
            await _reply(send, 503, b'{"detail": "busy: another decision is running"}')
            return
        try:
            await self.inner(scope, replay, send)
        finally:
            self.lock.release()


async def _read_ahead(receive: Any) -> list | None:
    # The body's messages up to its end or a disconnect; None past the cap.
    messages: list = []
    total = 0
    while True:
        message = await receive()
        messages.append(message)
        if message.get("type") != "http.request":
            return messages
        total += len(message.get("body", b""))
        if total > _BODY_AHEAD_MAX_BYTES:
            return None
        if not message.get("more_body"):
            return messages


async def _reply(send: Any, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


import torch  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.responses import PlainTextResponse  # noqa: E402
from laya.router import Router  # noqa: E402
from laya.serve import create_app  # noqa: E402

torch.set_num_threads(TORCH_THREADS)
torch.set_num_interop_threads(1)

parser = argparse.ArgumentParser()
parser.add_argument("--weights", required=True)
parser.add_argument("--port", type=int, required=True)
args = parser.parse_args()

# Only the English checkpoint is downloaded; a Router naming the others would
# reach for the network the first time a request routed to one.
router = Router(models={"english": args.weights}, device="cpu", max_loaded=1)
router.preload(["english"])
app = create_app(router)


@app.get("/kirocrew-attest", include_in_schema=False)
def _attest() -> PlainTextResponse:
    return PlainTextResponse(ATTEST, status_code=200 if ATTEST else 404)


uvicorn.run(
    _OneAtATime(app, QUEUE_WAIT_SECS), host="127.0.0.1", port=args.port, log_level="warning"
)
