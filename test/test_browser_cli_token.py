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

    def test_a_failed_clear_is_reported_instead_of_claiming_success(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mod.set_token("abc123")

        def refuse_unlink(*args, **kwargs):
            raise PermissionError("token is still in use")

        monkeypatch.setattr(Path, "unlink", refuse_unlink)
        with pytest.raises(PermissionError, match="still in use"):
            mod.clear_token()

    def test_unreadable_file_reads_as_absent(self, home: Path):
        mod.token_path().mkdir()  # a directory where a file belongs
        assert mod.read_token() is None


class TestPasteNormalization:
    """The extension shows the token as a shell assignment, so both forms arrive.

    Storing the variable name as part of the credential fails silently and far
    away: the panel reports "stored" while the extension keeps prompting.
    """

    @pytest.mark.parametrize(
        "pasted",
        [
            "tok-value",
            "PLAYWRIGHT_MCP_EXTENSION_TOKEN=tok-value",
            "  PLAYWRIGHT_MCP_EXTENSION_TOKEN=tok-value\n",
            "export PLAYWRIGHT_MCP_EXTENSION_TOKEN=tok-value",
            "EXPORT PLAYWRIGHT_MCP_EXTENSION_TOKEN=tok-value",
            'PLAYWRIGHT_MCP_EXTENSION_TOKEN="tok-value"',
            "PLAYWRIGHT_MCP_EXTENSION_TOKEN='tok-value'",
            "set PLAYWRIGHT_MCP_EXTENSION_TOKEN=tok-value",
            "setx PLAYWRIGHT_MCP_EXTENSION_TOKEN=tok-value",
            # A name transcribed in the wrong case is still the name.
            "playwright_mcp_extension_token=tok-value",
        ],
        ids=[
            "bare",
            "assignment",
            "assignment-padded",
            "export",
            "export-caps",
            "double-quoted",
            "single-quoted",
            "set",
            "setx",
            "lowercase-name",
        ],
    )
    def test_every_reasonable_paste_yields_the_same_token(self, home: Path, pasted: str):
        mod.set_token(pasted)
        assert mod.read_token() == "tok-value"
        assert mod.cli_env_overrides() == {mod.TOKEN_ENV: "tok-value"}

    @pytest.mark.parametrize(
        "value",
        [
            # base64url padding: these tokens legitimately end in '='.
            "abc123==",
            "a=b",
            "-ZpIqE3oycOCoseznXbu-bwUpP6Bw-u5MpO52f0n_dA",
            # A name that is NOT ours must not be treated as a prefix to strip.
            "SOME_OTHER_VAR=abc123",
        ],
        ids=["padding", "inner-equals", "urlsafe", "foreign-assignment"],
    )
    def test_a_bare_token_containing_equals_is_passed_through_byte_exact(
        self, home: Path, value: str
    ):
        """The prefix is stripped only when the text left of the FIRST '=' is
        exactly our variable name. Any looser rule (split on the last '=', strip
        anything before an '=') corrupts these."""
        mod.set_token(value)
        assert mod.read_token() == value

    @pytest.mark.parametrize("value", ['"abc123', 'abc123"', "'abc123", "abc123'"])
    def test_a_token_wrapped_in_unmatched_quotes_keeps_them(self, home: Path, value: str):
        """Quotes are stripped as a matched pair only, so a token that genuinely
        starts or ends with one is not silently shortened. Both ends are covered:
        the guard is an ``and`` of startswith/endswith, and an ``or`` would eat
        half of these."""
        mod.set_token(value)
        assert mod.read_token() == value

    def test_an_assignment_with_an_empty_value_clears_rather_than_storing_the_name(
        self, home: Path
    ):
        mod.set_token("tok-value")
        mod.set_token("PLAYWRIGHT_MCP_EXTENSION_TOKEN=")
        assert mod.has_token() is False

    def test_a_token_already_stored_in_the_assignment_form_repairs_itself_on_read(self, home: Path):
        """An install that stored the pasted assignment before this normalization
        existed holds a token that cannot work and reports as configured. Reading
        repairs it, so the user does not have to notice and re-paste."""
        mod.token_path().write_text(f"{mod.TOKEN_ENV}=legacy-value\n", encoding="utf-8")
        assert mod.read_token() == "legacy-value"
        assert mod.cli_env_overrides() == {mod.TOKEN_ENV: "legacy-value"}

    def test_normalization_is_idempotent(self, home: Path):
        once = mod.normalize_paste(f"{mod.TOKEN_ENV}=tok-value")
        assert mod.normalize_paste(once) == once


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
