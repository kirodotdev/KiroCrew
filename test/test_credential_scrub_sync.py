"""KIRO_API_KEY credential-scrub coverage.

The Docker entrypoint moves every ``_CREDENTIAL_KEYS`` entry from the process
environment into the data home's ``.env`` (mode 600) so credentials never
reside in a long-lived ``/proc/<pid>/environ``, and ``load_credentials()``
refuses to re-inject them while ``_KIROCREW_CREDS_SCRUBBED=1``. ``KIRO_API_KEY``
is kiro-cli's OWN model credential: unlike the gateway-owned channel tokens it
must still reach the kiro-cli child and the ``whoami`` identity probe, so the
spawn paths re-inject it explicitly from the ``.env`` file. These tests pin:

1. the entrypoint's shell scrub list and the loader's tuple stay in sync
   (bidirectional — a key added to one without the other fails here),
2. the ``.env`` fallback readers behave (environ precedence, last-wins,
   absent/unreadable file),
3. the ACP spawn env re-injection is scoped to the kiro-cli backend,
4. the identity probe falls back to the ``.env`` file post-scrub.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp.client import _resolve_spawn_env
from kiro_crew.config.loader import (
    _CREDENTIAL_KEYS,
    CRED_KIRO_API_KEY,
    inject_kiro_cli_api_key,
    read_env_file_credential,
    strip_kiro_cli_api_key,
)
from kiro_crew.kiro_prerequisite import (
    KiroPrerequisiteService,
    ProcessResult,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENTRYPOINT = _REPO_ROOT / "docker" / "entrypoint.sh"


async def _no_audit(**_kwargs: Any) -> None:
    return None


class TestScrubListSync:
    """docker/entrypoint.sh CRED_KEYS mirrors config/loader.py _CREDENTIAL_KEYS."""

    def test_entrypoint_and_loader_lists_are_identical(self) -> None:
        """Set equality both ways: a key scrubbed by the entrypoint that the
        loader would happily re-inject (the exact defect behind KIRO_API_KEY
        leaking into /proc/<pid>/environ), or a loader key the entrypoint never
        scrubs, both fail here."""
        text = _ENTRYPOINT.read_text()
        match = re.search(r'^CRED_KEYS="([^"]+)"', text, re.MULTILINE)
        assert match, "CRED_KEYS assignment not found in docker/entrypoint.sh"
        entrypoint_keys = set(match.group(1).split())
        assert entrypoint_keys == set(_CREDENTIAL_KEYS)

    def test_kiro_api_key_is_in_both_lists(self) -> None:
        """The regression this file exists for, pinned by name."""
        assert CRED_KIRO_API_KEY in _CREDENTIAL_KEYS
        assert CRED_KIRO_API_KEY in _ENTRYPOINT.read_text()


class TestReadEnvFileCredential:
    def test_reads_value_and_last_occurrence_wins(self, tmp_path: Path) -> None:
        """Same semantics as load_credentials(): dict-overwrite means the last
        line for a key is the one that counts."""
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# comment\n"
            "OTHER=nope\n"
            "KIRO_API_KEY=first\n"
            "KIRO_API_KEY = second \n"
        )
        assert read_env_file_credential("KIRO_API_KEY", env_file) == "second"

    def test_absent_file_reads_as_unset(self, tmp_path: Path) -> None:
        assert read_env_file_credential("KIRO_API_KEY", tmp_path / "missing.env") == ""

    def test_absent_key_reads_as_unset(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("OTHER=value\n")
        assert read_env_file_credential("KIRO_API_KEY", env_file) == ""


class TestInjectKiroCliApiKey:
    def test_injects_from_env_file_when_absent(self, tmp_path: Path, monkeypatch) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("KIRO_API_KEY=from-file\n")
        monkeypatch.setattr(
            "kiro_crew.config.loader.env_path", lambda: env_file
        )
        env: dict[str, str] = {"PATH": "/usr/bin"}
        inject_kiro_cli_api_key(env)
        assert env[CRED_KIRO_API_KEY] == "from-file"

    def test_existing_env_value_wins(self, tmp_path: Path, monkeypatch) -> None:
        """Same precedence as load_credentials(): the environment beats .env."""
        env_file = tmp_path / ".env"
        env_file.write_text("KIRO_API_KEY=from-file\n")
        monkeypatch.setattr(
            "kiro_crew.config.loader.env_path", lambda: env_file
        )
        env = {CRED_KIRO_API_KEY: "from-environ"}
        inject_kiro_cli_api_key(env)
        assert env[CRED_KIRO_API_KEY] == "from-environ"

    def test_noop_when_unset_everywhere(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.config.loader.env_path", lambda: tmp_path / "missing.env"
        )
        env: dict[str, str] = {}
        inject_kiro_cli_api_key(env)
        assert CRED_KIRO_API_KEY not in env


class TestStripKiroCliApiKey:
    """Foreign backends never receive kiro-cli's credential — inherited or not."""

    def test_strips_an_inherited_value(self) -> None:
        env = {CRED_KIRO_API_KEY: "inherited", "PATH": "/usr/bin"}
        strip_kiro_cli_api_key(env)
        assert CRED_KIRO_API_KEY not in env
        assert env["PATH"] == "/usr/bin"

    def test_windows_fold_catches_a_differently_cased_spelling(self, monkeypatch) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        env = {"Kiro_Api_Key": "inherited"}
        strip_kiro_cli_api_key(env)
        assert not env

    def test_posix_stays_exact(self, monkeypatch) -> None:
        """A lower-cased lookalike is a genuinely different variable on POSIX."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        env = {"kiro_api_key": "lookalike", CRED_KIRO_API_KEY: "real"}
        strip_kiro_cli_api_key(env)
        assert env == {"kiro_api_key": "lookalike"}


class TestSpawnEnvInjection:
    """_resolve_spawn_env injects for kiro-cli, strips for foreign backends."""

    def test_kiro_backend_gets_the_key(self, tmp_path: Path, monkeypatch) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("KIRO_API_KEY=spawn-key\n")
        monkeypatch.setattr(
            "kiro_crew.config.loader.env_path", lambda: env_file
        )
        env: dict[str, str] = {"PATH": "/usr/bin"}
        _resolve_spawn_env(env, kiro_api_key=True)
        assert env[CRED_KIRO_API_KEY] == "spawn-key"

    def test_foreign_backend_never_receives_it(self, tmp_path: Path, monkeypatch) -> None:
        """The credential is kiro-cli's alone — a foreign ACP backend spawned
        through the same path must not inherit it from the .env file."""
        env_file = tmp_path / ".env"
        env_file.write_text("KIRO_API_KEY=spawn-key\n")
        monkeypatch.setattr(
            "kiro_crew.config.loader.env_path", lambda: env_file
        )
        env: dict[str, str] = {"PATH": "/usr/bin"}
        _resolve_spawn_env(env, kiro_api_key=False)
        assert CRED_KIRO_API_KEY not in env

    def test_foreign_backend_loses_an_inherited_value(self, tmp_path: Path, monkeypatch) -> None:
        """The deny scrub deliberately exempts this key, so the foreign branch
        must actively remove a copy inherited from the raw os.environ snapshot
        — merely skipping re-injection would hand a Claude/KAS child the Kiro
        model credential on any host that has it exported."""
        monkeypatch.setattr(
            "kiro_crew.config.loader.env_path", lambda: tmp_path / "missing.env"
        )
        env = {CRED_KIRO_API_KEY: "inherited", "PATH": "/usr/bin"}
        _resolve_spawn_env(env, kiro_api_key=False)
        assert CRED_KIRO_API_KEY not in env


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)


class TestIdentityProbeEnvFileFallback:
    """Post-scrub Docker: whoami still sees the credential, --version never does."""

    @staticmethod
    def _service(tmp_path: Path, run: Any) -> KiroPrerequisiteService:
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")
        data_home = tmp_path / "data-home"
        data_home.mkdir(parents=True, exist_ok=True)
        (data_home / ".env").write_text("KIRO_API_KEY=key-from-env-file\n")
        # No KIRO_API_KEY in the environ — the entrypoint scrubbed it.
        environ = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
        return KiroPrerequisiteService(
            platform_name="linux",
            environ=environ,
            home=tmp_path,
            data_home=data_home,
            process_runner=run,
            audit_writer=_no_audit,
        )

    @pytest.mark.asyncio
    async def test_whoami_gets_the_key_from_the_env_file(self, tmp_path: Path) -> None:
        seen: dict[str, dict[str, str]] = {}

        async def run(_command: str, args: list[str], **kwargs: Any) -> ProcessResult:
            seen[args[0]] = dict(kwargs.get("env") or {})
            return ProcessResult(ok=True)

        service = self._service(tmp_path, run)
        await service.snapshot(force=True)

        assert seen["whoami"].get(CRED_KIRO_API_KEY) == "key-from-env-file"
        # --version is the first execution of an unvalidated candidate; the
        # fallback must not widen its credential-free environment.
        assert CRED_KIRO_API_KEY not in seen["--version"]

    @pytest.mark.asyncio
    async def test_environ_value_still_wins_over_the_file(self, tmp_path: Path) -> None:
        seen: dict[str, dict[str, str]] = {}

        async def run(_command: str, args: list[str], **kwargs: Any) -> ProcessResult:
            seen[args[0]] = dict(kwargs.get("env") or {})
            return ProcessResult(ok=True)

        service = self._service(tmp_path, run)
        service._environ["KIRO_API_KEY"] = "key-from-environ"
        await service.snapshot(force=True)

        assert seen["whoami"].get(CRED_KIRO_API_KEY) == "key-from-environ"


class TestJiraTokenScrubGuard:
    """Per-host JIRA_TOKEN_* keys are blocked by the re-injection guard."""

    def test_entrypoint_scrubs_dynamic_jira_tokens(self) -> None:
        """docker/entrypoint.sh feeds JIRA_TOKEN_* into the credential scrub loop."""
        text = _ENTRYPOINT.read_text()
        # The dynamic key capture must exist, restrict to hex suffix, and feed into loop
        assert "JIRA_TOKEN_[0-9A-Fa-f]" in text
        assert "JIRA_DYNAMIC" in text
        assert "$CRED_KEYS $JIRA_DYNAMIC" in text

    def test_loader_skips_jira_token_when_scrubbed(self, monkeypatch, tmp_path) -> None:
        """load_credentials() must NOT re-inject JIRA_TOKEN_* into os.environ
        when _KIROCREW_CREDS_SCRUBBED=1."""
        import os

        from kiro_crew.config.loader import KiroCrewConfig

        # Set up a minimal .env with a per-host Jira token
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        env_file = config_dir / ".env"
        env_file.write_text("JIRA_TOKEN_AABBCC=secret-token\n")
        env_file.chmod(0o600)

        # Simulate Docker scrub signal
        monkeypatch.setenv("_KIROCREW_CREDS_SCRUBBED", "1")
        # Remove any pre-existing value
        monkeypatch.delenv("JIRA_TOKEN_AABBCC", raising=False)

        # Patch config_dir to point at our tmp
        monkeypatch.setattr(
            "kiro_crew.config.loader.config_dir", lambda: config_dir
        )

        cfg = KiroCrewConfig.load()
        cfg.load_credentials()

        # The token must NOT have been re-injected
        assert os.environ.get("JIRA_TOKEN_AABBCC") is None
