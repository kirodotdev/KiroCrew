"""``_json_body`` is the body guard every mutating auto_improvement route shares.

It DELIBERATELY diverges from the dashboard's ``read_bounded_json`` contract: a
malformed OR non-object body collapses to ``{}`` (an empty config patch), because
every call site only reads known keys off the result and an empty patch is a safe
no-op. What must NOT diverge is the catch width — it was ``except Exception``,
which swallowed a mid-read transport error (a client disconnect) and mislabelled
it as ``{}``. These tests pin the narrowed ``(LookupError, RecursionError,
ValueError)`` catch: a client-input failure still yields ``{}``, but a transport
error propagates.

Runtime execution is deferred to CI, where aiohttp is installed.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.auto_improvement.backend import routes


def _req_raising(exc: Exception):
    """A real ``web.Request`` whose ``.json()`` raises *exc*."""
    request = make_mocked_request("PUT", "/api/apps/auto-improvement/config")

    async def _json(*_args: object, **_kwargs: object) -> object:
        raise exc

    request.json = _json  # type: ignore[method-assign]
    return request


def _req_returning(value: object):
    request = make_mocked_request("PUT", "/api/apps/auto-improvement/config")

    async def _json(*_args: object, **_kwargs: object) -> object:
        return value

    request.json = _json  # type: ignore[method-assign]
    return request


@pytest.mark.asyncio
async def test_a_non_object_body_becomes_an_empty_patch() -> None:
    # Documented divergence: a non-object is a safe empty patch here, not a 400.
    assert await routes._json_body(_req_returning([1, 2, 3])) == {}


@pytest.mark.asyncio
async def test_malformed_json_becomes_an_empty_patch() -> None:
    assert await routes._json_body(_req_raising(ValueError("bad json"))) == {}


@pytest.mark.asyncio
async def test_an_unknown_charset_codec_is_tolerated_not_a_500() -> None:
    # An unknown ``charset=`` codec raises LookupError, not ValueError. The old
    # ``except Exception`` caught it by accident; the narrowed catch must still
    # cover it so a bad codec is a tolerated empty patch, not an uncaught 500.
    assert await routes._json_body(_req_raising(LookupError("unknown encoding"))) == {}


@pytest.mark.asyncio
async def test_a_transport_error_propagates_not_swallowed() -> None:
    # A client disconnect mid-read is NOT a client-input mistake. The narrowed
    # catch must let it propagate rather than reporting it as an empty patch.
    class _Disconnect(Exception):
        pass

    with pytest.raises(_Disconnect):
        await routes._json_body(_req_raising(_Disconnect("connection reset")))
