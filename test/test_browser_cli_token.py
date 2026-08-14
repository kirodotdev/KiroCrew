"""The optional attach token: stored narrowly, never echoed, effective immediately."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from kiro_crew.browser_cli import token as mod


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(mod, "config_dir", lambda: tmp_path)
    return tmp_path


class TestStorage:
    def test_absent_by_default(self, home: Path):
        # Attaching works without a token, so no token is the normal state and must
        # not read as an error anywhere.
        assert mod.read_token() is None
        assert mod.has_token() is False
        assert mod.cli_env_overrides() == {}

    def test_round_trips_and_strips_paste_whitespace(self, home: Path):
        mod.set_token("  abc123\n")
        assert mod.read_token() == "abc123"
        assert mod.cli_env_overrides() == {mod.TOKEN_ENV: "abc123"}

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
    def test_written_owner_only(self, home: Path):
        mod.set_token("abc123")
        mode = stat.S_IMODE(mod.token_path().stat().st_mode)
        assert mode == 0o600, f"a credential must not be group/world readable, got {oct(mode)}"

    def test_blank_clears_rather_than_storing_an_empty_token(self, home: Path):
        mod.set_token("abc123")
        mod.set_token("   ")
        # An empty file would make has_token() true while the CLI sent nothing,
        # which reads as "configured" in the UI and behaves as unconfigured.
        assert mod.has_token() is False
        assert mod.token_path().exists() is False

    def test_clearing_an_absent_token_is_not_an_error(self, home: Path):
        mod.clear_token()
        assert mod.has_token() is False

    def test_unreadable_file_reads_as_absent(self, home: Path):
        mod.token_path().mkdir()  # a directory where a file belongs
        assert mod.read_token() is None


class TestItIsRegisteredAsACredential:
    def test_the_file_is_a_known_secret_leaf(self):
        # The agent inherits the token through the environment and never needs to
        # open the file, so the file stays behind the secret floor.
        from kiro_crew.security import _CREW_SECRET_LEAVES

        assert mod._TOKEN_FILE in _CREW_SECRET_LEAVES

    def test_the_module_never_returns_the_value_in_a_status_shape(self):
        # has_token exists so a status surface can report configuration without
        # handling the secret. Guard the boundary: a bool, never the string.
        assert isinstance(mod.has_token(), bool)
