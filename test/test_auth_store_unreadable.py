"""An auth store whose file exists but cannot be read fails closed and keeps the file.

On Windows a backup, sync or antivirus handle opened without read sharing makes the read
fail with PermissionError. Reading that as "no records" let a logged-out cookie or a
revoked refresh chain back in, and the next write replaced the file with the empty set.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest
from token_auth_helpers import clear_nonces  # noqa: F401 -- autouse fixture

import kiro_crew.dashboard.token_auth as _ta
from kiro_crew.dashboard.refresh_tokens import RefreshStateManager
from kiro_crew.dashboard.token_auth import generate_token, revoke_access_cookie, validate_token


def _block_reads(monkeypatch, name: str) -> None:
    real = Path.read_text

    def read_text(self, *args, **kwargs):
        if self.name == name:
            raise PermissionError(errno.EACCES, "held by another process", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)


def test_unreadable_revoked_nonce_store_fails_closed_and_recovers(tmp_path, monkeypatch):
    token = generate_token("alice")
    assert revoke_access_cookie(token) is True
    state = tmp_path / "token_revoked_nonces.json"
    on_disk = state.read_text(encoding="utf-8")

    monkeypatch.setattr(_ta, "_revoked_store_singleton", None)  # simulate a restart
    with monkeypatch.context() as m:
        _block_reads(m, state.name)
        valid, _, reason = validate_token(token, use_session_exp=True)
        assert (valid, reason) == (False, "session revocation state unavailable")
        other = generate_token("bob")
        with pytest.raises(OSError):  # kept in memory, but reported as not stored
            revoke_access_cookie(other)
    assert state.read_text(encoding="utf-8") == on_disk  # not written over while unread

    # The holder let go: the next check re-reads, with no restart.
    valid, _, reason = validate_token(token, use_session_exp=True)
    assert (valid, reason) == (False, "session revoked")
    assert validate_token(other, use_session_exp=True)[2] == "session revoked"
    assert validate_token(generate_token("carol"), use_session_exp=True)[0] is True
    # Retrying the logout that answered 503 stores it before reporting success.
    assert revoke_access_cookie(other) is True
    nonces = {e["nonce"] for e in json.loads(state.read_text())["revoked_nonces"]}
    assert len(nonces) == 2


def test_unreadable_refresh_state_degrades_keeps_file_and_recovers(tmp_path, monkeypatch):
    state = tmp_path / "rt.json"
    first = RefreshStateManager(state_path=state)
    assert first.revoke_chain("chain-1", 4102444800.0) is True
    on_disk = state.read_text()

    with monkeypatch.context() as m:
        _block_reads(m, state.name)
        mgr = RefreshStateManager(state_path=state)
        assert "could not be read" in mgr.degraded_reason()
        assert mgr.revoke_chain("chain-2", 4102444800.0) is False
    assert state.read_text() == on_disk

    assert mgr.degraded_reason() == ""
    assert mgr.is_chain_revoked("chain-1") is True


def test_missing_files_still_start_empty(tmp_path):
    assert RefreshStateManager(state_path=tmp_path / "rt.json").degraded_reason() == ""
    assert _ta.RevokedNonceStore(state_path=tmp_path / "n.json").readable() is True
