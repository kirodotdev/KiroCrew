"""A tips_state.json that cannot be read or parsed is never replaced by defaults.

Dismissals, snoozes and the opt-out are user intent. A read error serves
defaults for that request without saving them or caching them, a UTF-8 BOM file
reads, and a file that does not parse is kept aside as ``.corrupt-<stamp>``
before the first save replaces it.
"""

from __future__ import annotations

import json
import logging
import os
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_crew.tips as tips_mod
from kiro_crew.tips import _load_state, _save_state, get_tips_cache

_SEED = {
    "dismissed": ["user-dismissed-tip"],
    "snoozed": {"tip-s": 4102444800.0},
    "opted_out": True,
}


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(tips_mod, "_resolve_pool", lambda: None)
    monkeypatch.setattr(tips_mod, "_load_bundled_catalog", lambda: [])
    monkeypatch.setattr(tips_mod, "_load_curated_tips", lambda: [])
    with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
        yield tmp_path


@contextmanager
def _hold(path: Path) -> Iterator[None]:
    """Make reads of *path* raise the PermissionError a Windows sharing violation does."""
    real_text, real_bytes = Path.read_text, Path.read_bytes

    def _refuse(target: Path) -> None:
        if target == path:
            raise PermissionError(13, "being used by another process", str(target))

    def held_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        _refuse(self)
        return real_text(self, *args, **kwargs)

    def held_bytes(self: Path):  # type: ignore[no-untyped-def]
        _refuse(self)
        return real_bytes(self)

    with patch.object(Path, "read_text", held_text), patch.object(Path, "read_bytes", held_bytes):
        yield


def _asides(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.corrupt-*"))


@pytest.mark.asyncio
async def test_a_read_error_at_cache_build_keeps_the_saved_intent(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = home / "tips_state.json"
    path.write_text(json.dumps(_SEED), encoding="utf-8")
    before = path.read_bytes()
    state = types.SimpleNamespace()

    with _hold(path), caplog.at_level(logging.WARNING, logger="kiro_crew.tips"):
        cache = await get_tips_cache(state)
    assert cache.state.opted_out is False
    assert any(
        str(path) in r.getMessage() and "PermissionError" in r.getMessage() for r in caplog.records
    )

    # A handler saving that state (feedback "shown", say) writes nothing.
    cache.state.shown["tip-new"] = 1
    _save_state(cache.state)
    assert path.read_bytes() == before
    assert not hasattr(state, "_tips_cache")

    cache = await get_tips_cache(state)
    assert cache.state.opted_out is True
    assert cache.state.dismissed == ["user-dismissed-tip"]
    assert "tip-s" in cache.state.snoozed


def test_a_utf8_bom_state_file_loads(home: Path) -> None:
    path = home / "tips_state.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(_SEED).encode("utf-8"))
    st = _load_state()
    assert st.opted_out is True
    assert st.dismissed == ["user-dismissed-tip"]


@pytest.mark.parametrize("raw", [b"not valid json!!!", b"[]", b"\xff\xfe{\x00}\x00"])
def test_an_unparseable_file_is_kept_aside_before_the_first_save(home: Path, raw: bytes) -> None:
    path = home / "tips_state.json"
    path.write_bytes(raw)
    st = _load_state()
    assert st.opted_out is False

    st.opted_out = True
    _save_state(st)

    asides = _asides(path)
    assert len(asides) == 1
    assert asides[0].read_bytes() == raw
    assert json.loads(path.read_text(encoding="utf-8"))["opted_out"] is True

    _save_state(st)
    assert len(_asides(path)) == 1


def test_a_missing_file_saves_without_a_copy(home: Path) -> None:
    st = _load_state()
    _save_state(st)
    path = home / "tips_state.json"
    assert path.exists()
    assert _asides(path) == []


def _request(method: str, path: str, state: object, body: dict | None = None):  # type: ignore[no-untyped-def]
    import asyncio
    from unittest.mock import Mock

    from aiohttp import streams
    from aiohttp.test_utils import make_mocked_request

    payload = None
    if body is not None:
        payload = streams.StreamReader(
            Mock(_reading_paused=False), 2**16, loop=asyncio.get_event_loop()
        )
        payload.feed_data(json.dumps(body).encode())
        payload.feed_eof()
    req = make_mocked_request(method, path, payload=payload)
    req.app["state"] = state
    return req


@pytest.mark.asyncio
async def test_while_unreadable_next_serves_nothing_and_starts_no_generation(home: Path) -> None:
    from unittest.mock import MagicMock

    path = home / "tips_state.json"
    path.write_text(json.dumps(_SEED), encoding="utf-8")
    cfg = MagicMock()
    cfg.dashboard.tips_enabled = True
    state = types.SimpleNamespace()
    with (
        _hold(path),
        patch("kiro_crew.tips.KiroCrewConfig") as mock_cfg,
        patch("kiro_crew.tips.maybe_refresh") as refresh,
    ):
        mock_cfg.load.return_value = cfg
        resp = await tips_mod.api_tips_next(_request("GET", "/api/tips/next", state))
    assert resp.status == 204
    refresh.assert_not_called()


@pytest.mark.asyncio
async def test_while_unreadable_feedback_is_refused_and_the_file_kept(home: Path) -> None:
    path = home / "tips_state.json"
    path.write_text(json.dumps(_SEED), encoding="utf-8")
    before = path.read_bytes()
    state = types.SimpleNamespace()
    with _hold(path):
        resp = await tips_mod.api_tips_feedback(
            _request("POST", "/api/tips/feedback", state, {"id": "tip-new", "action": "shown"})
        )
    assert resp.status == 503
    assert json.loads(resp.text)["code"] == "tips_state_unreadable"
    assert path.read_bytes() == before

    resp = await tips_mod.api_tips_feedback(
        _request("POST", "/api/tips/feedback", state, {"id": "", "action": "optin"})
    )
    assert resp.status == 200
    assert json.loads(path.read_text(encoding="utf-8"))["dismissed"] == ["user-dismissed-tip"]
