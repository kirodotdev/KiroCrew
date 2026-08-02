"""Tests for the prewarm hot-key store's persistence path.

Focused on the platform seam: ``flush()`` writes identity-bearing keys, so it
applies owner-only protection before any content lands. POSIX mode bits and the
Windows DACL are different carriers, and the POSIX one is reached through a name
that does not exist on Windows -- which is what these pin.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew.mcp_gateway.prewarm import HotKeyStore

pytestmark = pytest.mark.xdist_group("mcp_gateway")


def _register(server: str = "test-mcp") -> dict[str, object]:
    """A payload complete enough for ``PoolKey.from_register`` -- ``record()``
    silently drops anything that will not parse, so a partial dict would make
    every assertion below vacuous."""
    return {
        "type": "register",
        "stub_uuid": "stub-1",
        "server_name": server,
        "agent_name": "test-agent",
        "command_args_hash": "a" * 64,
        "effective_env_hash": "b" * 64,
        "work_dir": "/tmp",
        "binary_version": "deadbeef",
        "os_uid": 1000,
        "sandbox_mode": "standard",
        "autoapprove_set_hash": "c" * 64,
        "approval_mode": "interactive",
        "trust_all_tools": False,
        "user_identity": "test-user",
        "config_snapshot_hash": "d" * 64,
    }


def test_flush_persists_keys_on_posix(tmp_path: Path) -> None:
    store = HotKeyStore(tmp_path / "hot-keys.json")
    store.record(_register())
    assert store.flush() is True

    payload = json.loads((tmp_path / "hot-keys.json").read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["keys"], "a recorded key must be persisted"


@pytest.mark.skipif(pc.IS_WINDOWS, reason="asserts POSIX mode bits")
def test_flush_makes_the_key_file_owner_only_on_posix(tmp_path: Path) -> None:
    store = HotKeyStore(tmp_path / "hot-keys.json")
    store.record(_register())
    store.flush()
    assert (tmp_path / "hot-keys.json").stat().st_mode & 0o777 == 0o600


def test_flush_persists_when_os_fchmod_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.fchmod`` does not exist on Windows, and ``flush``'s only handler
    catches ``OSError`` -- so naming it directly let an ``AttributeError`` escape
    the method after ``_dirty`` had already been cleared. The result was the
    worst of both: the re-arm never ran, so every observation was lost silently
    AND the exception propagated to the caller.

    The attribute is deleted rather than the test being run on Windows, so the
    guard executes on the matrix that actually runs it. ``IS_POSIX`` is flipped
    too so the Windows DACL branch is taken, and ``restrict_to_owner`` is stubbed
    because the real one shells out to ``icacls``, which cannot succeed here --
    the stub doubles as the assertion that the DACL is applied to the TEMP file,
    before any content is written.
    """
    monkeypatch.delattr(os, "fchmod", raising=False)
    assert not hasattr(os, "fchmod"), "precondition: os.fchmod must be hidden"
    monkeypatch.setattr(pc, "IS_POSIX", False)
    monkeypatch.setattr(pc, "IS_WINDOWS", True)

    protected: list[str] = []

    def _fake_restrict(p: object) -> None:
        protected.append(str(p))
        # The DACL must land while the file is still empty.
        assert os.path.getsize(str(p)) == 0, "content written before protection"

    monkeypatch.setattr(pc, "restrict_to_owner", _fake_restrict)

    path = tmp_path / "hot-keys.json"
    store = HotKeyStore(path)
    store.record(_register())

    assert store.flush() is True, "flush must not fail where os.fchmod is absent"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["keys"], "keys must be persisted, not silently dropped"
    assert protected, "the Windows branch must apply an owner-only DACL"
    # _dirty was cleared before the write and must stay cleared on success --
    # a re-armed flag here would mean the write was treated as failed.
    assert store._dirty is False


def test_flush_closes_fd_when_restrict_to_owner_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``restrict_to_owner`` raises on the temp file (e.g. Windows DACL
    subsystem failure), the raw file descriptor from ``mkstemp`` must still be
    closed -- otherwise each retry leaks an fd, and sustained failures exhaust
    the process descriptor table.

    Regression: the prior code only closed ``fd`` via ``os.fdopen(fd, "w")``,
    but ``restrict_to_owner`` is called BEFORE ``os.fdopen``.  If it raised,
    the ``fd`` was never closed.
    """
    monkeypatch.delattr(os, "fchmod", raising=False)
    monkeypatch.setattr(pc, "IS_POSIX", False)
    monkeypatch.setattr(pc, "IS_WINDOWS", True)

    closed_fds: list[int] = []
    _real_close = os.close

    def _tracking_close(fd: int) -> None:
        closed_fds.append(fd)
        _real_close(fd)

    monkeypatch.setattr(os, "close", _tracking_close)

    def _failing_restrict(_path: object) -> None:
        raise OSError(13, "Access is denied")

    monkeypatch.setattr(pc, "restrict_to_owner", _failing_restrict)

    path = tmp_path / "hot-keys.json"
    store = HotKeyStore(path)
    store.record(_register())

    # flush must not raise (the outer except OSError catches it), and must
    # return False (failure). The real assertion is that the fd was closed.
    assert store.flush() is False, "flush must report failure"
    assert closed_fds, "the raw fd from mkstemp must be closed on failure"
    # _dirty must be re-armed so the next flush retries.
    assert store._dirty is True
