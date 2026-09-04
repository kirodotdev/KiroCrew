"""Forwarding a turn to the Kiro Crew backend on loopback.

Everything here follows the backend facts pinned in ``container/CONTRACT.md``:

* Turn endpoint is ``POST /v1/chat/completions`` with ``{model, messages, id,
  stream}``.
* Authenticate with the ``X-Internal-Secret`` header, read from disk **on every
  attempt** via ``common.read_boot_secret``. The secret is ``os.urandom(16)`` per
  boot and is never persisted, so a cached copy 403s silently after a backend
  restart. It is never stored on the client or in a global here.
* The client's ``Origin`` and any ``X-Forwarded-*`` header are never forwarded.
  Rather than copy-and-strip, outbound headers are built from scratch, so a
  header the customer sends cannot reach the backend by accident.
* On a 403, re-read the secret once and retry once, then give up (relay it).
* Every turn runs inside ``transcript.prepared_turn``: the per-slot lock plus the
  on-demand fetch of that one conversation's transcript. The scope is built by
  the caller and entered here on the streaming path, so both transports share one
  definition of "the conversation is present and the slot is ours".
"""

from __future__ import annotations

import json
import logging
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
from container import common
from fastapi import Response
from fastapi.responses import StreamingResponse

from . import transcript
from .stream import project_sse

logger = logging.getLogger("smc.front.backend")

TURN_PATH = "/v1/chat/completions"


def build_outbound_headers(secret: str, *, stream: bool) -> dict[str, str]:
    """The complete header set sent to the backend. Built from scratch: no
    customer header is ever passed through, so ``Origin`` / ``X-Forwarded-*``
    cannot leak in and trip the backend's CSRF check."""
    return {
        common.HEADER: secret,
        "content-type": "application/json",
        "accept": "text/event-stream" if stream else "application/json",
    }


def _read_secret(settings: common.Settings) -> str:
    return common.read_boot_secret(settings.backend_run_dir, settings.backend_port)


def _sse_error(message: str, *, kind: str = "server_error") -> bytes:
    """A customer-safe SSE error frame in OpenAI stream shape (matches the real
    backend, which emits ``data: {"error": ...}`` then ``data: [DONE]`` — never a
    named event). Detail stays server-side, never here.

    ``kind`` carries the same code the non-streamed path returns in its JSON body,
    so one failure is named identically on both transports.
    """
    err = {"error": {"message": message, "type": kind}}
    return f"data: {json.dumps(err)}\n\ndata: [DONE]\n\n".encode("utf-8")


async def forward_completion(
    client: httpx.AsyncClient,
    settings: common.Settings,
    body: dict[str, Any],
) -> Response:
    """Single, non-streamed turn. Relays the backend's status and body verbatim.

    A non-streamed completion returns assistant text; the backend's ``/v1``
    ``usage`` block is hardcoded to zero, so there is nothing to redact here.
    """
    url = settings.backend_base_url + TURN_PATH
    headers = build_outbound_headers(_read_secret(settings), stream=False)
    # A transport failure is EXPECTED here, not exceptional: the backend is a sibling
    # process that ECS restarts, and a turn in flight when that happens raised
    # httpx.RequestError straight out of this coroutine. FastAPI turned that into a
    # 500 with a stack-shaped body, which tells the caller "this service is broken"
    # when the truth is "try again in a moment". 503 is that difference, and it is the
    # difference a retrying client acts on.
    try:
        resp = await client.post(url, json=body, headers=headers)

        if resp.status_code == 403:
            # Re-read once (a fresh boot secret may be on disk) and retry once.
            headers = build_outbound_headers(_read_secret(settings), stream=False)
            resp = await client.post(url, json=body, headers=headers)
    except httpx.RequestError as exc:
        logger.warning("backend transport failure on a non-streamed turn: %s", exc)
        return Response(
            content=json.dumps(
                {
                    "error": {
                        "message": "the crew's backend is not reachable right now",
                        "type": "service_unavailable",
                        "code": "backend_unreachable",
                    }
                }
            ),
            status_code=503,
            media_type="application/json",
        )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


def forward_stream(
    client: httpx.AsyncClient,
    settings: common.Settings,
    body: dict[str, Any],
    scope: AbstractAsyncContextManager[None],
) -> StreamingResponse:
    """Streamed turn. Holds the turn scope for the whole stream and projects the
    backend's SSE down to the customer allowlist.

    ``scope`` is ``transcript.prepared_turn(...)``: the per-slot lock plus the
    on-demand transcript fetch, built by the caller and entered HERE rather than
    in the request handler, because the slot stays busy on the backend until the
    stream ends. Entering it inside the generator also means a client that
    disconnects before the body starts never takes the lock at all.

    An SSE response commits status 200 before the body streams, so a failure
    (a backend 403 that survives the one retry, or a transcript that could not be
    fetched) is surfaced as an error FRAME rather than an HTTP status. The frame
    carries the same code the non-streamed path returns, so the customer is told
    the turn failed instead of being handed a conversation that looks forgotten.
    """
    url = settings.backend_base_url + TURN_PATH

    async def generator():
        try:
            async with scope:
                try:
                    secret = _read_secret(settings)
                except common.BackendSecretUnavailable:
                    yield _sse_error("service unavailable")
                    return

                stream_cm = client.stream(
                    "POST", url, json=body, headers=build_outbound_headers(secret, stream=True)
                )
                resp = await stream_cm.__aenter__()
                # Track the stream we actually HOLD, so the finally below closes that
                # one exactly once. The retry path exits the first stream before
                # opening a second, so a single `stream_cm` name meant the finally
                # could exit an already-closed context, or one whose __aenter__ never
                # completed.
                entered: AbstractAsyncContextManager[Any] | None = stream_cm
                try:
                    if resp.status_code == 403:
                        await stream_cm.__aexit__(None, None, None)
                        entered = None
                        # The FIRST secret read is guarded; this one was not. A backend
                        # that restarts and 403s while its replacement secret is not yet
                        # on disk raised BackendSecretUnavailable from here, and nothing
                        # above catches it. Status 200 is already committed by the time a
                        # body streams, so the stream ended TRUNCATED with no `[DONE]`
                        # and a client waiting for the sentinel waited forever. That is
                        # the same defect as the transport case below, one line over, and
                        # it survived the transport fix because it is a different type.
                        try:
                            secret = _read_secret(settings)
                        except common.BackendSecretUnavailable:
                            yield _sse_error("service unavailable")
                            return
                        stream_cm = client.stream(
                            "POST",
                            url,
                            json=body,
                            headers=build_outbound_headers(secret, stream=True),
                        )
                        resp = await stream_cm.__aenter__()
                        entered = stream_cm

                    if resp.status_code != 200:
                        await resp.aread()
                        yield _sse_error("upstream error")
                        return

                    async for frame in project_sse(resp.aiter_bytes()):
                        yield frame
                finally:
                    if entered is not None:
                        await entered.__aexit__(None, None, None)
        except transcript.TranscriptUnavailable:
            # The conversation could not be restored. Refuse the turn: answering
            # would show the customer an empty history and then overwrite the
            # real one in S3 at the next backup cycle.
            logger.error("stream refused: transcript unavailable", exc_info=True)
            yield _sse_error(
                "conversation is temporarily unavailable",
                kind=transcript.TranscriptUnavailable.code,
            )
        except httpx.RequestError as exc:
            # The backend went away mid-turn -- an ECS restart, not a bug. Status 200
            # is already committed by the time a body streams, so the only way to say
            # "this failed" is a frame. Without this the generator raised and the
            # response ended TRUNCATED, with no `[DONE]`: a client waiting for the
            # sentinel waits forever, which reads as a hung crew rather than a retry.
            # ``_sse_error`` appends `[DONE]`, so the stream is always well formed.
            logger.warning("stream ended early: backend transport failure: %s", exc)
            yield _sse_error(
                "the crew's backend is not reachable right now",
                kind="backend_unreachable",
            )

    return StreamingResponse(generator(), media_type="text/event-stream")
