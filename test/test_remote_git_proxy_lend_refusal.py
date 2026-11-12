"""A login the upstream refuses stops being lent to the guest.

Split from ``test_remote_git_proxy`` (file-size cap): an upstream 401 republishes the
lend list at once, and a refused value is not lent again until the source changes.
"""

from __future__ import annotations

import asyncio
import base64
import shutil
from pathlib import Path

import pytest
from test_remote_git_proxy import HEADER, FrameLink, HttpBackendUpstream

from kiro_crew.remote_session import git_gateway
from kiro_crew.remote_session.git_gateway import GitProxyChannel, GitProxyRefusal
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


@pytest.mark.asyncio
async def test_a_rejected_gateway_login_is_republished_at_once(monkeypatch):
    """After an upstream 401 the guest hears the new list without waiting out
    the refresh, so the next command goes direct."""
    monkeypatch.setattr(git_gateway, "LEND_REFRESH_SECS", 3600.0)
    link, valid = FrameLink(), {"github.com"}

    async def credential(host):
        if host not in valid:
            raise GitProxyRefusal(502, f"Kiro Crew: no {host} login found")
        return HEADER

    channel = GitProxyChannel(
        link,
        "dashboard:x",
        credential_source=credential,
        agent="researcher",
        egress_permitted=lambda key, host, agent: True,
        logins_lent=lambda: True,
        http_session=HttpBackendUpstream(Path("/nonexistent"), status=401),
    )
    serve = asyncio.ensure_future(channel.serve())
    try:
        link.guest({"t": "lend", "id": 0})
        await async_wait_until(lambda: link.frames, describe=lambda: "first list")
        valid.clear()  # the stored login stops working
        link.get(1, "/github.com/acme/w/info/refs?service=git-receive-pack")
        await async_wait_until(
            lambda: any(f["id"] == 1 and f["t"] == "err" for f in link.frames),
            describe=lambda: f"frames: {link.frames}",
        )
        # The refused value is remembered and the cached login dropped.
        assert channel._refused == {"github.com": HEADER}
        assert "github.com" not in channel._credentials
        await async_wait_until(
            lambda: [f for f in link.frames if f["t"] == "lend"][-1]["hosts"] == [],
            describe=lambda: f"frames: {link.frames}",
        )
    finally:
        link.stdout.feed_eof()
        await await_until(serve, what="gateway channel closed")


@pytest.mark.asyncio
async def test_a_revoked_login_its_source_still_returns_stops_being_lent(monkeypatch):
    """`gh auth token` can keep printing a token the host has revoked. The same
    refused value must not keep the host routed here; a fresh sign-in (a new
    value) lends again."""
    monkeypatch.setattr(git_gateway, "LEND_REFRESH_SECS", 3600.0)
    link, token = FrameLink(), {"value": HEADER}

    async def credential(host):
        if host != "github.com":
            raise GitProxyRefusal(502, f"Kiro Crew: no {host} login found")
        return token["value"]

    channel = GitProxyChannel(
        link,
        "dashboard:x",
        credential_source=credential,
        agent="researcher",
        egress_permitted=lambda key, host, agent: True,
        logins_lent=lambda: True,
        http_session=HttpBackendUpstream(Path("/nonexistent"), status=401),
    )
    serve = asyncio.ensure_future(channel.serve())

    def last_lend():
        lends = [f for f in link.frames if f["t"] == "lend"]
        return lends[-1]["hosts"] if lends else None

    try:
        link.guest({"t": "lend", "id": 0})
        await async_wait_until(lambda: last_lend() == ["github.com"], describe=lambda: "lent")
        link.get(1, "/github.com/acme/w/info/refs?service=git-receive-pack")
        await async_wait_until(
            lambda: any(f["id"] == 1 and f["t"] == "err" for f in link.frames),
            describe=lambda: f"{link.frames}",
        )
        assert channel._refused == {"github.com": HEADER}
        await async_wait_until(lambda: last_lend() == [], describe=lambda: f"{link.frames}")
        assert "github.com" not in channel._credentials
        token["value"] = "Basic " + base64.b64encode(b"x-access-token:fresh").decode()
        channel._lend_stale.set()
        await async_wait_until(lambda: last_lend() == ["github.com"], describe=lambda: "re-lent")
        assert channel._credentials["github.com"][0] == token["value"]
    finally:
        link.stdout.feed_eof()
        await await_until(serve, what="gateway channel closed")
