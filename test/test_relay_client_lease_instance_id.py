"""The relay lease's kept crew instance ID when its file is held, damaged or missing."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

import pytest
import test_relay_client_lease as base

import kiro_crew.atomic_write as atomic_write_mod
from kiro_crew.relay_client import lease as lease_module
from kiro_crew.relay_client.lease import INSTANCE_ID_FILE, crew_instance_id

# The lease service and clock fixtures of the lease tests, registered here under their names.
lease, clock = base.lease, base.clock

KEPT_ID = "123e4567-e89b-12d3-a456-426614174000"


def _kept(home: Path, content: bytes) -> Path:
    path = home / INSTANCE_ID_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.mark.asyncio
async def test_an_instance_id_file_held_by_another_process_defers_and_keeps_the_id(
    lease: base.FakeLeaseService,
    tmp_path: Path,
    clock: base.StepClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    kept = _kept(tmp_path, f"{KEPT_ID}\n".encode("ascii"))
    real = atomic_write_mod.read_bytes_with_retry

    def _held(path: Any, **kwargs: Any) -> bytes:
        if Path(path) == kept:
            raise PermissionError(32, "the file is held by another process")
        return real(path, **kwargs)

    monkeypatch.setattr(atomic_write_mod, "read_bytes_with_retry", _held)
    lease.queue(200, base.answer(0))
    source = base.make_source(lease, tmp_path, clock)
    try:
        with caplog.at_level(logging.WARNING, logger=lease_module.logger.name):
            await base.assert_retry_later(source.current())
        # No new identity is made or sent: the call waits on the backoff steps.
        assert lease.requests == []
        assert kept.read_bytes() == f"{KEPT_ID}\n".encode("ascii")
        assert str(kept) in caplog.text
        assert "making a new one" not in caplog.text
        monkeypatch.setattr(atomic_write_mod, "read_bytes_with_retry", real)
        clock.now += 3600
        await source.current()
    finally:
        await source.aclose()
    assert lease.requests[0]["body"]["crewInstanceId"] == KEPT_ID


@pytest.mark.parametrize("content", [b"not a uuid\n", b"\xff\xfe\x00"])
def test_a_damaged_instance_id_file_gets_a_new_saved_id(tmp_path: Path, content: bytes) -> None:
    kept = _kept(tmp_path, content)
    made = crew_instance_id(tmp_path)
    assert str(uuid.UUID(made)) == made
    assert kept.read_text(encoding="ascii").strip() == made


def test_a_missing_instance_id_file_gets_a_new_saved_id(tmp_path: Path) -> None:
    made = crew_instance_id(tmp_path)
    assert (tmp_path / INSTANCE_ID_FILE).read_text(encoding="ascii").strip() == made
    assert crew_instance_id(tmp_path) == made


def test_a_held_instance_id_file_is_read_through_the_sharing_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kept = _kept(tmp_path, f"{KEPT_ID}\n".encode("ascii"))
    read: list[Path] = []
    real = atomic_write_mod.read_bytes_with_retry

    def _recorded(path: Any, **kwargs: Any) -> bytes:
        read.append(Path(path))
        return real(path, **kwargs)

    monkeypatch.setattr(atomic_write_mod, "read_bytes_with_retry", _recorded)
    assert crew_instance_id(tmp_path) == KEPT_ID
    assert read == [kept]
