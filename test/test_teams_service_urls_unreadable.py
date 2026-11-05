"""A serviceUrl store that cannot be read is never replaced by a smaller map.

A read error at load leaves the file alone until a read succeeds, and then the
next flush writes the file's rows plus the new ones. A UTF-8 BOM file reads, and
one that does not parse is kept as ``<name>.corrupt-<stamp>`` before the write.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.teams.service_urls import ServiceUrlStore

_SVC = "https://smba.trafficmanager.net/amer/"


async def _seed(path: Path) -> None:
    seed = ServiceUrlStore(path=path)
    seed.remember("a:conv1", _SVC, identity="one@example.com")
    seed.remember("a:conv2", _SVC)
    await seed.flush()


def _conversations(path: Path) -> list[str]:
    return sorted(json.loads(path.read_text(encoding="utf-8"))["conversations"])


@contextmanager
def _held(path: Path) -> Iterator[None]:
    """Reads of *path* raise the PermissionError a Windows sharing violation does."""
    real_text, real_bytes = Path.read_text, Path.read_bytes

    def _refuse(target: Path) -> None:
        if target == path:
            raise PermissionError(13, "Permission denied", str(target))

    def text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        _refuse(self)
        return real_text(self, *args, **kwargs)

    def raw(self: Path):  # type: ignore[no-untyped-def]
        _refuse(self)
        return real_bytes(self)

    with patch.object(Path, "read_text", text), patch.object(Path, "read_bytes", raw):
        yield


@pytest.mark.asyncio
async def test_an_unreadable_store_is_not_rewritten_with_one_conversation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "teams_service_urls.json"
    await _seed(path)
    store = ServiceUrlStore(path=path)

    with _held(path), caplog.at_level(logging.WARNING, logger="kiro_crew.teams.service_urls"):
        await store.ensure_loaded()
        assert store.loaded is True
        store.remember("a:conv3", _SVC)
        await store.flush()
    assert _conversations(path) == ["a:conv1", "a:conv2"]
    assert any(str(path) in r.getMessage() for r in caplog.records)

    # The read succeeds again: the next flush writes all three.
    await store.flush()
    assert _conversations(path) == ["a:conv1", "a:conv2", "a:conv3"]
    assert store.conversation_for("one@example.com") == "a:conv1"


@pytest.mark.asyncio
async def test_a_conversation_forgotten_while_unreadable_stays_forgotten(tmp_path: Path) -> None:
    path = tmp_path / "teams_service_urls.json"
    await _seed(path)
    store = ServiceUrlStore(path=path)
    with _held(path):
        await store.ensure_loaded()
    store.forget("a:conv1")
    await store.flush()
    assert _conversations(path) == ["a:conv2"]
    assert store.conversation_for("one@example.com") == ""


@pytest.mark.asyncio
async def test_a_stable_route_still_asks_for_the_pending_flush(tmp_path: Path) -> None:
    """The inbound path flushes only when ``remember`` says so; a route never changes."""
    path = tmp_path / "teams_service_urls.json"
    await _seed(path)
    store = ServiceUrlStore(path=path)
    with _held(path):
        await store.ensure_loaded()
        assert store.remember("a:conv3", _SVC) is True
        await store.flush()

    assert store.remember("a:conv3", _SVC) is True
    await store.flush()
    assert _conversations(path) == ["a:conv1", "a:conv2", "a:conv3"]
    assert store.remember("a:conv3", _SVC) is False


@pytest.mark.asyncio
async def test_a_forget_during_the_re_read_stays_forgotten(tmp_path: Path) -> None:
    path = tmp_path / "teams_service_urls.json"
    await _seed(path)
    store = ServiceUrlStore(path=path)
    with _held(path):
        await store.ensure_loaded()
    store.remember("a:conv1", _SVC)
    real = store._read_rows

    def forget_mid_read():  # type: ignore[no-untyped-def]
        store.forget("a:conv1")  # a 403 lands while the re-read runs
        return real()

    with patch.object(store, "_read_rows", forget_mid_read):
        await store.flush()
    assert _conversations(path) == ["a:conv2"]


@pytest.mark.asyncio
async def test_a_utf8_bom_store_loads(tmp_path: Path) -> None:
    path = tmp_path / "teams_service_urls.json"
    await _seed(path)
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    store = ServiceUrlStore(path=path)
    await store.ensure_loaded()
    assert store.get("a:conv1") == _SVC


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b"{not json", b"[]", b"\xff\xfe{\x00}\x00"])
async def test_an_unparseable_store_is_kept_aside_before_the_write(
    tmp_path: Path, raw: bytes
) -> None:
    path = tmp_path / "teams_service_urls.json"
    path.write_bytes(raw)
    store = ServiceUrlStore(path=path)
    await store.ensure_loaded()
    store.remember("a:conv3", _SVC)
    await store.flush()

    assert _conversations(path) == ["a:conv3"]
    asides = sorted(tmp_path.glob("teams_service_urls.json.corrupt-*"))
    assert len(asides) == 1
    assert asides[0].read_bytes() == raw

    store.remember("a:conv4", _SVC)
    await store.flush()
    assert len(sorted(tmp_path.glob("teams_service_urls.json.corrupt-*"))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("identities", [["x"], "x", 7])
async def test_a_non_object_identities_field_keeps_every_route(
    tmp_path: Path, identities: object
) -> None:
    path = tmp_path / "teams_service_urls.json"
    await _seed(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["identities"] = identities
    path.write_text(json.dumps(data), encoding="utf-8")

    store = ServiceUrlStore(path=path)
    await store.ensure_loaded()
    assert store.get("a:conv1") == _SVC
    store.remember("a:conv3", _SVC)
    await store.flush()

    assert _conversations(path) == ["a:conv1", "a:conv2", "a:conv3"]


@pytest.mark.asyncio
async def test_an_unexpected_load_error_keeps_the_file_aside_before_the_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "teams_service_urls.json"
    await _seed(path)
    before = path.read_bytes()
    store = ServiceUrlStore(path=path)

    def broken():  # type: ignore[no-untyped-def]
        raise RuntimeError("unexpected")

    with patch.object(store, "_read_rows", broken):
        await store.ensure_loaded()
    store.remember("a:conv3", _SVC)
    await store.flush()

    copies = list(tmp_path.glob("teams_service_urls.json.corrupt-*"))
    assert [copy.read_bytes() for copy in copies] == [before]
    assert _conversations(path) == ["a:conv3"]


@pytest.mark.asyncio
async def test_a_seen_at_too_large_for_a_float_keeps_the_route(tmp_path: Path) -> None:
    path = tmp_path / "teams_service_urls.json"
    await _seed(path)
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    data["conversations"]["a:conv1"]["seen_at"] = 0
    path.write_text(
        json.dumps(data).replace('"seen_at": 0', '"seen_at": 1' + "0" * 400), encoding="utf-8"
    )

    store = ServiceUrlStore(path=path)
    await store.ensure_loaded()
    assert store.get("a:conv1") == _SVC
    store.remember("a:conv3", _SVC)
    await store.flush()
    assert _conversations(path) == ["a:conv1", "a:conv2", "a:conv3"]
