"""A scripted gateway for ``kirocrew spawn``'s CLI tests.

``kirocrew spawn`` talks to the gateway through ``owner_gateway_client``: it mints
an owner token over the dashboard unix socket, then sends its calls there. This
fake stands in for that socket. Each request takes the next answer from
``script`` (a dict is the JSON reply, an exception is raised), and is recorded in
``calls`` with the socket path and the peer check it was opened with. TCP use
fails the test.
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import cli_commands, owner_gateway_client

#: ``/api/token/local``'s answer, the first request of every ``kirocrew spawn``.
MINTED = {"token": "owner-tok", "expires_in": 120}


class _Reply:
    def __init__(self, payload: object) -> None:
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_Reply":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def http_error(code: int, payload: object = None) -> urllib.error.HTTPError:
    """An ``HTTPError`` whose body is *payload* as JSON (or empty)."""
    raw = json.dumps(payload).encode() if payload is not None else b""
    return urllib.error.HTTPError("http://127.0.0.1/x", code, "err", {}, io.BytesIO(raw))  # type: ignore[arg-type]


def stub_module_clock(
    monkeypatch: pytest.MonkeyPatch, module: object, attr: str, **fakes: Any
) -> None:
    """Rebind *module*'s own ``time`` binding (``attr``) to a stand-in carrying *fakes*.

    Every other name is delegated to whatever the binding holds now (the stdlib
    module, or an earlier stand-in), so the stdlib clock itself is never patched:
    every event loop and thread in the worker reads that one (testing-conventions,
    "Never rebind a stdlib clock or ``time.sleep``").
    """
    current = getattr(module, attr)
    monkeypatch.setattr(module, attr, SimpleNamespace(**{**vars(current), **fakes}))


def install(monkeypatch: pytest.MonkeyPatch, *answers: Any, secret: str = "s3cret") -> Any:
    """Patch the owner client's socket and secret; return ``.calls`` and ``.script``."""
    fake = SimpleNamespace(calls=[], script=list(answers))

    def _unix(req, timeout, *, socket_path, verify_peer=None):
        fake.calls.append(
            SimpleNamespace(req=req, socket_path=socket_path, verify_peer=verify_peer)
        )
        answer = fake.script.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return _Reply(answer)

    def _tcp(*_a: object, **_k: object) -> None:
        raise AssertionError("kirocrew spawn must never use TCP")

    monkeypatch.setattr(owner_gateway_client, "unix_socket_urlopen", _unix)
    monkeypatch.setattr(owner_gateway_client, "read_local_secret", lambda _port: secret)
    monkeypatch.setattr(
        owner_gateway_client, "dashboard_socket_path", lambda port: Path(f"/s/dash-{port}.sock")
    )
    stub_module_clock(monkeypatch, owner_gateway_client, "time", sleep=lambda _s: None)
    monkeypatch.setattr(cli_commands, "loopback_urlopen", _tcp)
    stub_module_clock(monkeypatch, cli_commands, "_time", sleep=lambda _s: None)
    return fake
