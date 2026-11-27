"""Fail a fake tunnel's peer calls the way a closed loopback port does, at once.

After ``SshTunnelManager.connect()`` succeeds, the manager primes the peer session
(``_exchange_link``) and some paths ask ``token_validates``: both are an aiohttp GET to
``127.0.0.1:<local_port>``. The tests' fake tunnels bind nothing, so that GET meets a
closed port. Linux refuses it in under a millisecond; Windows retries the SYN and refuses
after about 2 s, once per call, which made these files cost minutes there.

``refused_peer_connects`` swaps ``aiohttp`` for the ``ssh_tunnel_manager`` family only,
through the facade, whose mirror rebinds it in every owner that holds it (``_child``,
``_peer``, ``_transport``); patching one owner directly would let the facade's undo restore
the others to that owner's value. The global ``aiohttp`` module is never touched. The view's
``ClientSession`` raises the same ``ClientConnectorError`` a refused connect raises, so the
product's own ``except`` branches run and return the same outcome; every other ``aiohttp``
name resolves to the real module. A test that replaced ``ClientSession`` itself keeps its
stand-in: the view only answers for the real class. A test that talks to a real aiohttp
server through the family, or whose subject is a real refused connect, asks for
``real_peer_connects`` instead.
"""

from __future__ import annotations

import errno
from typing import Any

import aiohttp
import pytest
from aiohttp.client_reqrep import ConnectionKey
from yarl import URL

from kiro_crew.instances import ssh_tunnel_manager as _facade

_REAL_CLIENT_SESSION = aiohttp.ClientSession


def _refused(url: object) -> aiohttp.ClientConnectorError:
    """The error aiohttp's connector raises for a plain-http connect nobody accepts."""
    target = URL(str(url))
    key = ConnectionKey(
        host=target.host or "127.0.0.1",
        port=target.port,
        is_ssl=False,
        ssl=True,
        proxy=None,
        proxy_auth=None,
        proxy_headers_hash=None,
    )
    address = (key.host, key.port)
    return aiohttp.ClientConnectorError(
        key, ConnectionRefusedError(errno.ECONNREFUSED, f"Connect call failed {address}")
    )


class _RefusedRequest:
    """What ``session.get(...)`` / ``session.request(...)`` return: raises on use."""

    def __init__(self, url: object) -> None:
        self._url = url

    async def __aenter__(self) -> Any:
        raise _refused(self._url)

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def __await__(self):
        async def _raise() -> Any:
            raise _refused(self._url)

        return _raise().__await__()


class _RefusingClientSession:
    """A ``ClientSession`` whose every request fails like a connect to a closed port."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.closed = False

    async def __aenter__(self) -> _RefusingClientSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        await self.close()
        return False

    async def close(self) -> None:
        self.closed = True

    def request(self, method: str, url: object, **kwargs: Any) -> _RefusedRequest:
        return _RefusedRequest(url)

    def get(self, url: object, **kwargs: Any) -> _RefusedRequest:
        return _RefusedRequest(url)

    def post(self, url: object, **kwargs: Any) -> _RefusedRequest:
        return _RefusedRequest(url)


class _PeerAiohttp:
    """``aiohttp`` as the tunnel-manager family sees it under ``refused_peer_connects``."""

    def __getattr__(self, name: str) -> Any:
        current = getattr(aiohttp, name)
        if name == "ClientSession" and current is _REAL_CLIENT_SESSION:
            return _RefusingClientSession
        return current


@pytest.fixture
def refused_peer_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    # Product code may log or format the error, so it must render like the real one.
    sample = _refused("http://127.0.0.1:53400/api/status")
    assert str(sample) == (
        "Cannot connect to host 127.0.0.1:53400 ssl:default"
        " [Connect call failed ('127.0.0.1', 53400)]"
    ), str(sample)
    assert repr(sample) and sample.errno == errno.ECONNREFUSED
    monkeypatch.setattr(_facade, "aiohttp", _PeerAiohttp())


@pytest.fixture
def real_peer_connects(refused_peer_connects: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt one test back out of a file-wide ``refused_peer_connects``: it dials a real
    aiohttp server, or a real closed port, through the family. Depending on that fixture
    orders this after it."""
    monkeypatch.setattr(_facade, "aiohttp", aiohttp)
