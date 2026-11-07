"""Doubles for a transfer that opens its connection through ``asset_downloader.build_opener``.

``answer_with`` replaces the opener, so a test serves each response itself.
``redirect_to`` replaces only the transport under the real opener, so the redirect policy
in force is the one the code under test picked.
"""

from __future__ import annotations

import email.message
import io
import types
import urllib.request
import urllib.response
from typing import Callable

import pytest

from kiro_crew import asset_downloader


def answer_with(monkeypatch: pytest.MonkeyPatch, urlopen: Callable[..., object]) -> None:
    """Answer every connection with *urlopen*, called as ``(url or request, timeout=...)``."""
    monkeypatch.setattr(
        asset_downloader,
        "build_opener",
        lambda *_a, **_k: types.SimpleNamespace(open=urlopen),
    )


def redirect_to(monkeypatch: pytest.MonkeyPatch, url: str, hop: str, payload: bytes) -> list[str]:
    """Answer *url* with a 302 to *hop* and any other url with *payload*.

    Returns the urls requested, in order, so a test can tell a refused hop from a
    followed one.
    """
    requested: list[str] = []

    def _transport(_handler: object, _connection: object, req, **_kw: object):  # noqa: ANN001
        requested.append(req.full_url)
        headers = email.message.Message()
        if req.full_url == url:
            headers["Location"] = hop
            resp = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
            resp.msg = "Found"
        else:
            resp = urllib.response.addinfourl(io.BytesIO(payload), headers, req.full_url, 200)
            resp.msg = "OK"
        return resp

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", _transport)
    return requested
