"""A home with no refresh-chain store gets ``{}`` before a namespace spawn, so the mask mounts.

The store is created on its first write, and ``mount(2)`` cannot mask an absent path in a
data-home root that is writable in-sandbox. The stub must read exactly as a missing store.
The behaviour the stub shares with ``.env`` (an existing file, a symlink, a failed create)
is pinned in ``test_sandbox_env_mask_target.py``. Every path is under ``tmp_path``.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew import sandbox
from kiro_crew.dashboard import refresh_tokens


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "crew"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


def test_an_absent_store_is_created_as_an_empty_object_and_reads_as_absent(
    crew_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = crew_home / refresh_tokens._STATE_FILE_NAME
    assert not os.path.lexists(store)
    established: list[str] = []

    assert sandbox._materialize_refresh_chains_mask_target(established) == str(store)

    assert stat.S_ISREG(os.lstat(store).st_mode)
    assert store.read_bytes() == b"{}"
    assert established == [str(store)]
    with caplog.at_level(logging.DEBUG, logger=refresh_tokens.logger.name):
        loaded = refresh_tokens.RefreshStateManager(state_path=store)
    assert [r for r in caplog.records if r.name == refresh_tokens.logger.name] == []
    assert loaded.degraded_reason() == ""
    assert (loaded._consumed_jtis, loaded._revoked_chains, loaded._chain_peers) == ({}, {}, {})


def test_a_failed_write_leaves_the_store_empty_and_says_so(
    crew_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = crew_home / refresh_tokens._STATE_FILE_NAME
    established: list[str] = []

    def full_disk(fd: int, mode: str) -> None:
        os.close(fd)
        raise OSError(errno.ENOSPC, "No space left on device")

    with mock.patch.object(os, "fdopen", side_effect=full_disk):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
            assert sandbox._materialize_refresh_chains_mask_target(established) == str(store)

    assert store.read_bytes() == b""
    assert established == [str(store)]
    assert f"could not write {store}" in caplog.text
