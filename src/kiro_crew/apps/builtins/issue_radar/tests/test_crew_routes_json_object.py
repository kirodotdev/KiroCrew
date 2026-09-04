"""``_json_object`` is the body guard the agent write path shares with
``_body_preamble``.

It follows the dashboard's ``read_bounded_json`` contract on catch width: the
catch was narrowed from ``except Exception`` to the client-input failure set
``(LookupError, RecursionError, ValueError)`` so a mid-read transport error (a
client disconnect) propagates as itself rather than being mislabelled a 400. A
client-input failure (malformed JSON, an unknown ``charset=`` codec) still turns
into a 400; a non-object body is a 400; a non-finite number is a 400 (the
deliberate divergence). These tests pin that boundary so a future re-widening to
``except Exception`` — which would swallow the disconnect again — fails loudly.

Runtime execution is deferred to CI, where aiohttp is installed.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import crew_routes


def _req(payload: object) -> web.Request:
    """A real ``web.Request`` whose ``.json()`` yields (or raises) *payload*.

    Mirrors ``test_pr_actions._req``: built on aiohttp's own
    ``make_mocked_request`` so the handler runs against the actual Request type.
    Passing an Exception makes ``.json()`` raise it, reaching the malformed-body
    path.
    """
    request = make_mocked_request("POST", "/api/apps/issue-radar/agent/issue/comment")

    async def _json(*_args: object, **_kwargs: object) -> object:
        if isinstance(payload, Exception):
            raise payload
        return payload

    request.json = _json  # type: ignore[method-assign]
    return request


@pytest.mark.asyncio
async def test_malformed_json_is_400() -> None:
    body, early = await crew_routes._json_object(_req(ValueError("bad json")))
    assert body == {}
    assert early is not None
    assert early.status == 400


@pytest.mark.asyncio
async def test_unknown_charset_codec_is_400_not_500() -> None:
    # An unknown ``charset=`` codec raises LookupError, not ValueError. The old
    # ``except Exception`` caught it by accident; the narrowed catch must still
    # cover it so a bad codec is a 400, not an uncaught 500.
    body, early = await crew_routes._json_object(_req(LookupError("unknown encoding")))
    assert body == {}
    assert early is not None
    assert early.status == 400


@pytest.mark.asyncio
async def test_non_object_body_is_400() -> None:
    body, early = await crew_routes._json_object(_req([1, 2, 3]))
    assert body == {}
    assert early is not None
    assert early.status == 400


@pytest.mark.asyncio
async def test_transport_error_propagates_not_swallowed() -> None:
    # A client disconnect mid-read is NOT a client-input mistake. The narrowed
    # catch must let it propagate rather than reporting it as a 400. A re-widening
    # to ``except Exception`` would swallow this and fail the test.
    class _Disconnect(Exception):
        pass

    with pytest.raises(_Disconnect):
        await crew_routes._json_object(_req(_Disconnect("connection reset")))
