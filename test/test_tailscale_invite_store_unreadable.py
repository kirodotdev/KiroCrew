"""The Tailscale invite store never rewrites a state file it could not read."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.shared_crew import tailscale_invite_store as store
from kiro_crew.shared_crew import tailscale_invites as invites


@pytest.fixture
def state_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return store.root() / store.STATE_FILE


def _add(invite_id: str) -> Any:
    return lambda s: s["invites"].update({invite_id: {"id": invite_id}})


@contextmanager
def _refusing_state_reads(state_file: Path) -> Iterator[None]:
    """The store's state read is refused for every attempt, so it outlasts any retry."""
    real = Path.read_bytes

    def _read_bytes(self: Path) -> bytes:
        if self == state_file:
            raise PermissionError(13, "held by another process", str(self))
        return real(self)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "read_bytes", _read_bytes)
        yield


def test_a_refused_read_leaves_the_file_byte_identical(state_file: Path) -> None:
    store.update_state(_add("i1"))
    store.update_state(_add("i2"))
    before = state_file.read_bytes()

    with _refusing_state_reads(state_file), pytest.raises(store.StateUnreadableError):
        store.update_state(_add("i3"))

    assert state_file.read_bytes() == before


@pytest.mark.parametrize("body", [b"{not json", b'{"schema": 99}', b"[]", b'{"schema": 1\xff}'])
def test_an_invalid_file_is_not_rewritten(state_file: Path, body: bytes) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_bytes(body)

    with pytest.raises(store.StateUnreadableError):
        store.update_state(_add("i1"))

    assert state_file.read_bytes() == body
    # A read that only displays the state still answers an empty one.
    assert store.load_state()["invites"] == {}


def test_an_action_reports_the_refused_read(state_file: Path) -> None:
    store.update_state(lambda s: s.update(device_id="nSelf"))
    before = state_file.read_bytes()

    with _refusing_state_reads(state_file), pytest.raises(OSError):
        asyncio.run(invites.disconnect("owner"))

    assert state_file.read_bytes() == before


def test_a_missing_file_reads_empty_and_the_first_write_creates_it(state_file: Path) -> None:
    assert not state_file.exists()
    assert store.load_state()["invites"] == {}

    store.update_state(_add("i1"))

    assert list(store.load_state()["invites"]) == ["i1"]
