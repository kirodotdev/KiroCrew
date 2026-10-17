"""Serve Strands Decider 2B on this machine's CPU for Kiro Crew's decision seam.

Runs inside the model's own environment, never the gateway's: it imports torch,
transformers, peft and strands_decider, which Kiro Crew does not depend on. The
checkpoint is the LoRA adapter and head; the Qwen base it adapts is mirrored
beside it in ``base/``. Both were downloaded and verified by the gateway, so
nothing here reaches the network.
"""

import argparse
import asyncio
import json
import os
import sys
import threading
from typing import Any

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


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
# client's own timeout is 5 s and one CPU inference takes about 3 s, so a request
# queued longer than this would be answered after its caller gave up.
QUEUE_WAIT_SECS = 1.0
# The gateway counts this server ready only once it echoes this secret, so a
# program that took the port while the weights loaded is never mistaken for it.
ATTEST = os.environ.pop("KIROCREW_LOCAL_ATTEST", "")


def _exit_when_gateway_lets_go() -> None:
    # The gateway holds this process's stdin open for as long as it wants the
    # server. End of input -- a stop, or the gateway exiting -- ends the server,
    # so it never outlives the process that started it.
    sys.stdin.buffer.read()
    os._exit(0)


threading.Thread(target=_exit_when_gateway_lets_go, daemon=True).start()

import torch  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.responses import PlainTextResponse  # noqa: E402
from strands_decider import modeling  # noqa: E402
from strands_decider.server import create_app  # noqa: E402

torch.set_num_threads(TORCH_THREADS)
torch.set_num_interop_threads(1)

parser = argparse.ArgumentParser()
parser.add_argument("--weights", required=True)
parser.add_argument("--port", type=int, required=True)
args = parser.parse_args()

# The checkpoint's config names its base by Hub id; the pinned files cannot be
# edited, so the id is replaced with the mirrored copy as the config is read.
BASE_DIR = os.path.join(args.weights, "base")
_from_json = modeling.StrandsDeciderConfig.from_json


def _from_json_local_base(path: str) -> "modeling.StrandsDeciderConfig":
    config = _from_json(path)
    config.base_model = BASE_DIR
    return config


modeling.StrandsDeciderConfig.from_json = staticmethod(_from_json_local_base)  # type: ignore[method-assign]

app = create_app(args.weights, device="cpu", model_name="strands-decider-2b")


@app.get("/kirocrew-attest", include_in_schema=False)
def _attest() -> PlainTextResponse:
    return PlainTextResponse(ATTEST, status_code=200 if ATTEST else 404)


def _blank_null_criteria(body: bytes) -> bytes:
    # Kiro Crew sends an option with no rubric as ``null``; Strands' schema
    # types every rubric as a string and answers 422 to a null one, which would
    # skip every decision. An empty rubric means the same thing to it.
    try:
        data = json.loads(body)
    except ValueError:
        return body
    questions = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(questions, dict):
        return body
    for question in questions.values():
        criteria = question.get("criteria") if isinstance(question, dict) else None
        if isinstance(criteria, dict):
            for option, rubric in criteria.items():
                if rubric is None:
                    criteria[option] = ""
    return json.dumps(data).encode()


class _NullCriteriaToEmpty:
    """ASGI wrapper applying ``_blank_null_criteria`` to decision requests."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http" or scope.get("path") != "/v1/systemone":
            return await self.inner(scope, receive, send)
        chunks = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        body = _blank_null_criteria(b"".join(chunks))
        headers = [(k, v) for k, v in scope["headers"] if k != b"content-length"]
        headers.append((b"content-length", str(len(body)).encode()))
        delivered = False

        async def replay():  # type: ignore[no-untyped-def]
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.inner(dict(scope, headers=headers), replay, send)


class _OneAtATime:
    """ASGI wrapper running one decision request at a time.

    Strands' endpoint is synchronous, so its framework runs each request on a
    thread pool of about 40 with no lock, and every inference uses
    ``TORCH_THREADS`` cores. Concurrent requests then oversubscribe the CPU until
    each takes longer than the client waits, and an abandoned request is still
    computed to the end. A request that cannot start within ``QUEUE_WAIT_SECS``
    is answered 503 at once; the gate records it as a provider error and the
    caller falls back as it does for any refused decision.
    """

    def __init__(self, inner: Any, wait_secs: float) -> None:
        self.inner = inner
        self.wait_secs = wait_secs
        self.lock = asyncio.Lock()

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http" or scope.get("path") != "/v1/systemone":
            return await self.inner(scope, receive, send)
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=self.wait_secs)
        except asyncio.TimeoutError:
            body = b'{"detail": "busy: another decision is running"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        try:
            await self.inner(scope, receive, send)
        finally:
            self.lock.release()


uvicorn.run(
    _OneAtATime(_NullCriteriaToEmpty(app), QUEUE_WAIT_SECS),
    host="127.0.0.1",
    port=args.port,
    log_level="warning",
)
