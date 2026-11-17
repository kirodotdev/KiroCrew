"""A container's About panel names a newer release only when its image can be pulled.

The release feed is the only version source. The feed lists a version before its
image is published, so the gateway confirms the image with GHCR's anonymous
pull-token flow (a token GET, then a manifest HEAD) and names the version only on
a 200. Anything else keeps the existing deferral line.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from update_check_install_aware_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _wheel_install,
)

from kiro_crew.dashboard.handlers import _update_image, updates
from kiro_crew.platform import update_capability

_TOKEN = "s3cr3t-anon-pull-token"


def _manifest(version: str, channel: str = "insider") -> bytes:
    return json.dumps(
        {
            "schema": "kirocrew-cli-artifact-manifest-v1",
            "channel": channel,
            "version": version,
        }
    ).encode()


class _Resp:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body
        self.content = self

    async def iter_chunked(self, _n: int):
        # Two chunks, as a chunked response arrives: a single read() would
        # see only the first.
        half = len(self._body) // 2
        for chunk in (self._body[:half], self._body[half:]):
            if chunk:
                yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeSession:
    """Records every request; answers from *plan* keyed by method."""

    def __init__(self, plan: dict[str, object], calls: list) -> None:
        self._plan = plan
        self._calls = calls

    def _answer(self, method: str, url: str, kw: dict) -> _Resp:
        self._calls.append((method, url, kw))
        result = self._plan[method]
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]

    def get(self, url, **kw):
        return self._answer("GET", url, kw)

    def head(self, url, **kw):
        return self._answer("HEAD", url, kw)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _token_ok() -> _Resp:
    return _Resp(200, json.dumps({"token": _TOKEN}).encode())


@pytest.fixture
def container(monkeypatch):
    monkeypatch.setattr(update_capability, "distribution", lambda: "docker")
    monkeypatch.setattr(updates, "_local_version", "0.1.2rc3")

    _feeds(monkeypatch, stable="0.1.1", insider="0.1.3rc2")


def _feeds(monkeypatch, *, stable, insider) -> list[str]:
    """Answer each lane's feed; a lane given an exception raises it."""
    seen: list[str] = []

    async def _feed(url: str):
        seen.append(url)
        lane = "stable" if "/feed/stable/" in url else "insider"
        value = stable if lane == "stable" else insider
        if isinstance(value, BaseException):
            raise value
        return 200, _manifest(value, lane)

    monkeypatch.setattr(updates, "_fetch_feed_bytes", _feed)
    return seen


def _run(monkeypatch, plan) -> tuple[dict, list]:
    calls: list = []
    monkeypatch.setattr(
        _update_image.aiohttp, "ClientSession", lambda **_kw: _FakeSession(plan, calls)
    )
    asyncio.run(updates._do_update_check())
    return dict(updates.get_update_info()), calls


def test_token_then_200_names_the_version(monkeypatch, container):
    info, calls = _run(monkeypatch, {"GET": _token_ok(), "HEAD": _Resp(200)})

    assert info["check_status"] == "deferred"
    assert info["unavailable_reason"] == "managed_by_image"
    assert info["can_apply"] is False
    assert info["image_available_version"] == "0.1.3-rc.2"
    # Availability only: no verdict, so no badge and no Update button.
    assert info["update_available"] is None
    assert info["image_release_notes_url"] == (
        "https://github.com/kirodotdev/KiroCrew/releases/tag/v0.1.3-rc.2"
    )
    # Exactly two requests, to the fixed host and scope, redirects off.
    assert [(m, u) for m, u, _ in calls] == [
        ("GET", "https://ghcr.io/token?scope=repository:kirodotdev/kirocrew:pull&service=ghcr.io"),
        ("HEAD", "https://ghcr.io/v2/kirodotdev/kirocrew/manifests/0.1.3-rc.2"),
    ]
    assert all(kw["allow_redirects"] is False for _, _, kw in calls)
    assert "headers" not in calls[0][2]
    assert calls[1][2]["headers"]["Authorization"] == f"Bearer {_TOKEN}"


@pytest.mark.parametrize(
    "head",
    [_Resp(404), _Resp(401), asyncio.TimeoutError(), OSError("boom")],
    ids=["404", "401", "timeout", "error"],
)
def test_a_non_200_manifest_keeps_todays_text(monkeypatch, container, head):
    info, _calls = _run(monkeypatch, {"GET": _token_ok(), "HEAD": head})

    assert info["check_status"] == "deferred"
    assert info["unavailable_reason"] == "managed_by_image"
    assert "image_available_version" not in info


@pytest.mark.parametrize(
    "token",
    [_Resp(500), _Resp(200, b"not json"), asyncio.TimeoutError()],
    ids=["500", "junk", "timeout"],
)
def test_a_failed_token_request_keeps_todays_text_and_skips_the_head(monkeypatch, container, token):
    info, calls = _run(monkeypatch, {"GET": token, "HEAD": _Resp(200)})

    assert "image_available_version" not in info
    assert [m for m, _, _ in calls] == ["GET"]


def test_the_lane_comes_from_the_running_build_not_the_channel_file(
    monkeypatch, container, tmp_path
):
    # A container has no channel file; the fixture's "insider" file must not matter.
    (tmp_path / "channel").write_text("stable\n")
    seen = _feeds(monkeypatch, stable="0.1.1", insider="0.1.3rc2")
    info, _calls = _run(monkeypatch, {"GET": _token_ok(), "HEAD": _Resp(200)})

    # The stable feed does not list this rc's release, so the lane is insider.
    assert [u.split("/feed/")[1] for u in seen] == [
        "stable/latest-cli.json",
        "insider/latest-cli.json",
    ]
    assert info["image_available_version"] == "0.1.3-rc.2"


def test_a_promoted_rc_on_the_stable_feed_follows_stable(monkeypatch, container):
    # A stable image built by promotion still runs the candidate's rc stamp.
    monkeypatch.setattr(updates, "_local_version", "0.1.3rc2")
    seen = _feeds(monkeypatch, stable="0.1.3rc2", insider="0.1.4rc1")
    info, calls = _run(monkeypatch, {"GET": _token_ok(), "HEAD": _Resp(200)})

    assert [u.split("/feed/")[1] for u in seen] == ["stable/latest-cli.json"]
    assert calls == []
    assert "image_available_version" not in info


def test_a_promoted_rc_names_a_newer_stable_not_an_rc(monkeypatch, container):
    # Stable moved past the promoted build; insider has a pullable RC too.
    monkeypatch.setattr(updates, "_local_version", "0.12.2rc2")
    seen = _feeds(monkeypatch, stable="0.12.3", insider="0.13.0rc1")
    info, calls = _run(monkeypatch, {"GET": _token_ok(), "HEAD": _Resp(200)})

    assert [u.split("/feed/")[1] for u in seen] == ["stable/latest-cli.json"]
    assert info["image_available_version"] == "0.12.3"
    assert calls[1][1].endswith("/manifests/0.12.3")


@pytest.mark.parametrize("err", [asyncio.TimeoutError(), OSError("down")], ids=["timeout", "error"])
def test_a_failed_stable_read_keeps_todays_text(monkeypatch, container, err):
    seen = _feeds(monkeypatch, stable=err, insider="0.1.3rc2")
    info, calls = _run(monkeypatch, {"GET": _token_ok(), "HEAD": _Resp(200)})

    assert [u.split("/feed/")[1] for u in seen] == ["stable/latest-cli.json"]
    assert calls == []
    assert info["check_status"] == "deferred"
    assert "image_available_version" not in info


def test_nothing_is_requested_when_the_feed_has_nothing_newer(monkeypatch, container):
    monkeypatch.setattr(updates, "_local_version", "0.1.3rc2")
    info, calls = _run(monkeypatch, {"GET": _token_ok(), "HEAD": _Resp(200)})

    assert calls == []
    assert info["check_status"] == "deferred"
    assert "image_available_version" not in info


def test_the_token_never_reaches_logs_or_the_response(monkeypatch, container, caplog):
    caplog.set_level(logging.DEBUG)
    for head in (_Resp(200), _Resp(404), OSError(f"leak {_TOKEN}")):
        info, _calls = _run(monkeypatch, {"GET": _token_ok(), "HEAD": head})
        assert _TOKEN not in json.dumps(info)
    assert _TOKEN not in caplog.text


def test_a_nightly_version_names_no_image(monkeypatch, container):
    _feeds(monkeypatch, stable="0.1.1", insider="0.2.0.dev20261111085733")
    info, calls = _run(monkeypatch, {"GET": _token_ok(), "HEAD": _Resp(200)})

    assert calls == []
    assert "image_available_version" not in info
