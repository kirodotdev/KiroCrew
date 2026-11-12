"""Notes (md-notebook) backend: the content-hash save-guard token.

``GET /api/note`` returns a ``contentHash`` beside ``mtime``, and ``PUT /api/note``
accepts it back as ``baseHash``. When sent, the hash alone decides freshness, so an
external write that kept the note's mtime still answers 409 instead of being
overwritten. Shared fixtures live in ``md_notebook_helpers.py``.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from md_notebook_helpers import _seed_template  # noqa: F401 - fixture
from md_notebook_helpers import fixtures_fixture  # noqa: F401 - fixture
from md_notebook_helpers import _clone, signed_client


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rewrite_keeping_mtime(target: Path, text: str) -> None:
    """An external write that restores the note's timestamps, as ``rsync -t`` does."""
    before = target.stat()
    target.write_bytes(text.encode("utf-8"))
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert target.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.asyncio
async def test_read_returns_the_hash_of_the_bytes_on_disk(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        target = Path(vault["localPath"]) / "One.md"
        # CRLF on disk: the hash covers the raw bytes, not the normalized text.
        target.write_bytes(b"# One\r\n\r\nbody\r\n")
        status, read = await client.get("/api/note?path=One.md")
        assert status == 200
        assert read["content"] == "# One\n\nbody\n"
        assert read["contentHash"] == _sha256(b"# One\r\n\r\nbody\r\n")


@pytest.mark.asyncio
async def test_same_mtime_external_write_is_a_conflict_with_base_hash(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        target = Path(vault["localPath"]) / "One.md"
        _, read = await client.get("/api/note?path=One.md")

        external = "# One\n\nchanged by another program\n"
        _rewrite_keeping_mtime(target, external)

        status, body = await client.put(
            "/api/note",
            {
                "path": "One.md",
                "content": "mine",
                "baseMtime": read["mtime"],
                "baseHash": read["contentHash"],
            },
        )
        assert status == 409, body
        assert body["code"] == "ESTALE"
        assert body["disk"] == external
        assert body["contentHash"] == _sha256(external.encode("utf-8"))
        assert target.read_text(encoding="utf-8") == external


@pytest.mark.asyncio
async def test_same_mtime_external_write_passes_the_mtime_only_guard(fixtures) -> None:
    """Without ``baseHash`` the mtime-only behaviour is unchanged, gap included."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        target = Path(vault["localPath"]) / "One.md"
        _, read = await client.get("/api/note?path=One.md")
        _rewrite_keeping_mtime(target, "# One\n\nchanged by another program\n")

        status, body = await client.put(
            "/api/note", {"path": "One.md", "content": "mine", "baseMtime": read["mtime"]}
        )
        assert status == 200, body
        assert target.read_text(encoding="utf-8") == "mine"


@pytest.mark.asyncio
async def test_moved_mtime_over_identical_bytes_saves_with_base_hash(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        target = Path(vault["localPath"]) / "One.md"
        _, read = await client.get("/api/note?path=One.md")

        newer_ns = round(read["mtime"] * 1_000_000) + 5_000_000_000
        os.utime(target, ns=(newer_ns, newer_ns))

        status, body = await client.put(
            "/api/note",
            {
                "path": "One.md",
                "content": "mine",
                "baseMtime": read["mtime"],
                "baseHash": read["contentHash"],
            },
        )
        assert status == 200, body
        assert body["contentHash"] == _sha256(b"mine")
        assert target.read_text(encoding="utf-8") == "mine"


@pytest.mark.asyncio
async def test_successful_save_hash_guards_the_next_save(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        _, read = await client.get("/api/note?path=One.md")
        status, first = await client.put(
            "/api/note",
            {"path": "One.md", "content": "one\r\n", "baseHash": read["contentHash"]},
        )
        assert status == 200, first
        _, reread = await client.get("/api/note?path=One.md")
        assert first["contentHash"] == reread["contentHash"]

        status, second = await client.put(
            "/api/note",
            {"path": "One.md", "content": "two", "baseHash": first["contentHash"]},
        )
        assert status == 200, second


@pytest.mark.asyncio
async def test_deleted_note_with_base_hash_conflicts_then_recreates(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        target = Path(vault["localPath"]) / "One.md"
        _, read = await client.get("/api/note?path=One.md")
        target.unlink()

        status, body = await client.put(
            "/api/note",
            {"path": "One.md", "content": "mine", "baseHash": read["contentHash"]},
        )
        assert status == 409, body
        assert body["code"] == "ESTALE"
        assert body["mtime"] == -1
        assert not target.exists()

        # "Keep mine" confirms the recreate with the -1 sentinel, whichever token
        # the first save carried.
        status, ok = await client.put(
            "/api/note",
            {
                "path": "One.md",
                "content": "mine",
                "baseMtime": body["mtime"],
                "baseHash": read["contentHash"],
            },
        )
        assert status == 200, ok
        assert target.read_text(encoding="utf-8") == "mine"


@pytest.mark.asyncio
async def test_note_deleted_between_stat_and_read_is_the_deleted_conflict(
    fixtures, monkeypatch
) -> None:
    """A deletion landing inside the hash check answers the -1 409, not a 404."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        target = Path(vault["localPath"]) / "One.md"
        _, read = await client.get("/api/note?path=One.md")

        real_read = _mod.read_note_snapshot

        async def deleted_after_stat(path: Path):
            # The check's stat has already seen the note; delete it before the read.
            if path == target and target.exists():
                target.unlink()
            return await real_read(path)

        monkeypatch.setattr(_mod, "read_note_snapshot", deleted_after_stat)
        status, body = await client.put(
            "/api/note",
            {"path": "One.md", "content": "mine", "baseHash": read["contentHash"]},
        )
        assert status == 409, body
        assert body["code"] == "ESTALE"
        assert body["mtime"] == -1
        assert not target.exists()


@pytest.mark.asyncio
async def test_keep_mine_retries_against_the_conflict_hash(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        target = Path(vault["localPath"]) / "One.md"
        _, read = await client.get("/api/note?path=One.md")
        _rewrite_keeping_mtime(target, "external\n")

        status, conflict = await client.put(
            "/api/note",
            {"path": "One.md", "content": "mine", "baseHash": read["contentHash"]},
        )
        assert status == 409

        status, body = await client.put(
            "/api/note",
            {
                "path": "One.md",
                "content": "mine",
                "baseMtime": conflict["mtime"],
                "baseHash": conflict["contentHash"],
            },
        )
        assert status == 200, body
        assert target.read_text(encoding="utf-8") == "mine"


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["baseHash", None])
async def test_contended_retry_rechecks_the_hash(fixtures, token) -> None:
    """A same-mtime edit landing in a contended save's backoff is caught on retry.

    Covers both the client token and the tokenless path's own sampled baseline.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        target = Path(vault["localPath"]) / "One.md"
        _, read = await client.get("/api/note?path=One.md")
        external = "external during backoff\n"

        attempts: list[int] = []
        real_replace = os.replace

        def contended_then_edited(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
            if str(dst) != str(target):
                return real_replace(src, dst, *args, **kwargs)
            attempts.append(1)
            if len(attempts) == 1:
                _rewrite_keeping_mtime(target, external)
                raise PermissionError(32, "The process cannot access the file")
            return real_replace(src, dst, *args, **kwargs)

        payload = {"path": "One.md", "content": "mine"}
        if token:
            payload[token] = read["contentHash"]
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_mod.platform_compat, "IS_WINDOWS", True)
            mp.setattr(_mod, "_SAVE_BACKOFF_SECONDS", 0.0)
            mp.setattr(_mod.os, "replace", contended_then_edited)
            status, body = await client.put("/api/note", payload)

        assert status == 409, body
        assert body["code"] == "ESTALE"
        assert body["contentHash"] == _sha256(external.encode("utf-8"))
        assert len(attempts) == 1, "the retry published without revalidating"
        assert target.read_text(encoding="utf-8") == external
        assert not list(target.parent.glob("One.md.*.tmp")), "a temp file was left behind"
