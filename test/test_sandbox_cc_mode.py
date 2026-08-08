"""Tests for sandbox 'cc' mode — routing, dir lists, and profile generation."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

import kiro_crew.sandbox as _sb_mod
from kiro_crew.sandbox import (
    _AGENT_DENIED_ENV_KEYS,
    _CC_DIRS,
    _CC_EXPOSE_FILES,
    _CC_FILES,
    _STANDARD_DIRS,
    _build_launcher_script,
    _build_seatbelt_profile,
    sandbox_exec_argv,
    sandboxed_spawn_argv,
    scrub_agent_denied_env,
    scrub_env,
    wrap_argv,
)


@pytest.fixture(autouse=True)
def _neutralize_sandbox_env(monkeypatch):
    """Prevent the 'already inside sandbox' passthrough on sandboxed hosts."""
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    monkeypatch.setattr(
        _sb_mod, "_KIRO_INTERNAL_SETTINGS_PATH",
        "/nonexistent/kirocrew-test/amazon-internal.json",
    )


class TestCcDirsList:
    def test_hides_aws(self):
        """CC mode hides .aws dir (only .aws/config selectively exposed)."""
        assert ".aws" in _CC_DIRS

    def test_hides_kube(self):
        assert ".kube" in _CC_DIRS

    def test_allows_ssh_via_flag(self):
        """CC mode doesn't list .ssh in dirs — hiding is via hide_ssh flag."""
        assert ".ssh" not in _CC_DIRS

    def test_hides_gnupg(self):
        assert ".gnupg" in _CC_DIRS

    def test_hides_more_than_standard(self):
        """CC hides .aws and .kube while standard does not."""
        assert ".aws" in _CC_DIRS
        assert ".aws" not in _STANDARD_DIRS
        assert ".kube" in _CC_DIRS
        assert ".kube" not in _STANDARD_DIRS


class TestCcExposeFiles:
    def test_exposes_aws_config(self):
        assert ".aws/config" in _CC_EXPOSE_FILES

    def test_does_not_expose_credentials(self):
        assert ".aws/credentials" not in _CC_EXPOSE_FILES


class TestCcFilesList:
    def test_has_npmrc(self):
        assert ".npmrc" in _CC_FILES

    def test_has_pypirc(self):
        assert ".pypirc" in _CC_FILES

    def test_has_netrc(self):
        assert ".netrc" in _CC_FILES

    def test_has_git_credentials(self):
        assert ".git-credentials" in _CC_FILES

    def test_has_kirocrew_env(self):
        assert ".kirocrew/.env" in _CC_FILES


class TestBuildLauncherScriptCcMode:
    def test_extra_hidden_directory_is_bound_over(self):
        script = _build_launcher_script(
            "strict",
            extra_hidden_dirs=("/private/kiro/crew",),
        )

        assert "/private/kiro/crew" in script

    def test_cc_mode_uses_cc_dirs(self):
        script = _build_launcher_script("cc")
        for d in _CC_DIRS:
            assert d in script, f"{d} should be in cc launcher script"

    def test_cc_mode_includes_expose_files(self):
        script = _build_launcher_script("cc")
        assert "EXPOSE_FILES" in script
        assert ".aws/config" in script

    def test_cc_mode_includes_sensitive_files(self):
        script = _build_launcher_script("cc")
        assert "SENSITIVE_FILES" in script
        for f in _CC_FILES:
            assert f in script, f"{f} should be in cc launcher script"

    def test_cc_mode_does_not_hide_ssh(self):
        script = _build_launcher_script("cc")
        assert "HIDE_SSH = False" in script

    def test_strict_mode_hides_ssh(self):
        script = _build_launcher_script("strict")
        assert "HIDE_SSH = True" in script

    def test_standard_mode_does_not_hide_ssh(self):
        script = _build_launcher_script("standard")
        assert "HIDE_SSH = False" in script

    def test_standard_mode_uses_standard_dirs(self):
        script = _build_launcher_script("standard")
        for d in _STANDARD_DIRS:
            assert d in script

    def test_standard_mode_no_expose_files(self):
        script = _build_launcher_script("standard")
        assert "EXPOSE_FILES = []" in script


class TestBuildSeatbeltProfileCcMode:
    def test_extra_hidden_directory_denies_reads_and_writes(self):
        profile = _build_seatbelt_profile(
            "strict",
            extra_hidden_dirs=("/private/kiro/crew",),
        )

        assert '(deny file-read* (subpath "/private/kiro/crew"))' in profile
        assert '(deny file-write* (subpath "/private/kiro/crew"))' in profile
        assert '(deny file-link (subpath "/private/kiro/crew"))' in profile

    def test_cc_does_not_deny_aws(self):
        """CC seatbelt does NOT deny .aws — macOS needs full .aws access for
        credential_process and SSO token caches. LLM deny patterns provide
        the security layer instead."""
        profile = _build_seatbelt_profile("cc")
        assert ".aws" not in profile

    def test_cc_denies_kube(self):
        profile = _build_seatbelt_profile("cc")
        assert ".kube" in profile

    def test_cc_denies_sensitive_files(self):
        profile = _build_seatbelt_profile("cc")
        assert ".npmrc" in profile
        assert ".netrc" in profile
        assert ".git-credentials" in profile
        assert ".kirocrew/.env" in profile
        assert "literal" in profile

    def test_cc_does_not_deny_ssh(self):
        profile = _build_seatbelt_profile("cc")
        assert ".ssh" not in profile

    def test_strict_denies_ssh(self):
        profile = _build_seatbelt_profile("strict")
        assert ".ssh" in profile

    def test_standard_does_not_deny_ssh(self):
        profile = _build_seatbelt_profile("standard")
        assert ".ssh" not in profile


class TestWrapArgvCcMode:
    @patch("kiro_crew.sandbox.detect_backend", return_value="sandbox-exec")
    def test_cc_mode_routes_to_sandbox(self, _mock_backend):
        wrapped, cleanup = wrap_argv(["echo", "hi"], mode="cc")
        assert len(wrapped) > 2
        assert cleanup is not None
        os.unlink(cleanup)

    def test_off_mode_no_sandbox(self):
        wrapped, cleanup = wrap_argv(["echo", "hi"], mode="off")
        assert wrapped == ["echo", "hi"]
        assert cleanup is None

    @patch("kiro_crew.sandbox.detect_backend", return_value="sandbox-exec")
    def test_cc_seatbelt_does_not_deny_aws(self, _mock_backend):
        """CC seatbelt does NOT deny .aws on macOS — full access needed."""
        wrapped, cleanup = wrap_argv(["echo", "hi"], mode="cc")
        assert cleanup is not None
        try:
            content = open(cleanup).read()
            assert ".aws" not in content
        finally:
            os.unlink(cleanup)

    @patch("kiro_crew.sandbox.detect_backend", return_value="sandbox-exec")
    def test_cc_seatbelt_profile_does_not_deny_ssh(self, _mock_backend):
        """CC profile should not contain ssh deny rules."""
        wrapped, cleanup = wrap_argv(["echo", "hi"], mode="cc")
        assert cleanup is not None
        try:
            content = open(cleanup).read()
            lines = [ln for ln in content.splitlines() if ".ssh" in ln and "deny" in ln]
            assert lines == []
        finally:
            os.unlink(cleanup)


class TestAgentDeniedEnvKeys:
    """Sandboxed agents (cc/strict) must not see credentials that loader.py
    propagates into os.environ for trusted children. The launcher script and
    sandbox-exec wrapper both scrub these keys."""

    def test_default_set_includes_slack_tokens(self):
        assert "SLACK_BOT_TOKEN" in _AGENT_DENIED_ENV_KEYS
        assert "SLACK_APP_TOKEN" in _AGENT_DENIED_ENV_KEYS
        assert "KIROCREW_OWNER_ID" in _AGENT_DENIED_ENV_KEYS

    def test_cc_launcher_scrubs_agent_creds(self):
        """cc launcher script's ENV_PREFIXES list contains the cred keys."""
        script = _build_launcher_script("cc")
        for key in _AGENT_DENIED_ENV_KEYS:
            assert key in script, f"{key} should appear in cc launcher ENV_PREFIXES"

    def test_strict_launcher_scrubs_agent_creds(self):
        script = _build_launcher_script("strict")
        for key in _AGENT_DENIED_ENV_KEYS:
            assert key in script

    def test_standard_launcher_does_not_scrub_agent_creds(self):
        """Standard mode is for trusted subprocess wrappers (git, aws CLI,
        kubectl). They legitimately need Slack tokens for things like cron
        scripts. Only cc/strict (LLM-controlled agents) scrub them."""
        script = _build_launcher_script("standard")
        # Tokens should NOT appear in the standard launcher's ENV_PREFIXES.
        # We check by looking for the key inside the JSON-encoded list right
        # after "ENV_PREFIXES = " — substring on the whole script would also
        # match comments, so be precise.
        line = next(ln for ln in script.splitlines() if ln.startswith("ENV_PREFIXES = "))
        for key in _AGENT_DENIED_ENV_KEYS:
            assert key not in line, f"{key} should NOT be in standard ENV_PREFIXES"

    def test_cc_sandbox_exec_scrubs_agent_creds(self, monkeypatch):
        """sandbox-exec (macOS) cc path emits env -u for cred keys present in env."""
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
        monkeypatch.setenv("KIROCREW_OWNER_ID", "U123")
        argv, cleanup = sandbox_exec_argv(["echo", "hi"], sandbox_level="cc")
        try:
            assert "-u" in argv and "SLACK_BOT_TOKEN" in argv
            assert "KIROCREW_OWNER_ID" in argv
        finally:
            if cleanup:
                os.unlink(cleanup)

    def test_standard_sandbox_exec_does_not_scrub_agent_creds(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
        argv, cleanup = sandbox_exec_argv(["echo", "hi"], sandbox_level="standard")
        try:
            assert "SLACK_BOT_TOKEN" not in argv
        finally:
            if cleanup:
                os.unlink(cleanup)

    @patch("kiro_crew.sandbox.detect_backend", return_value="namespace")
    def test_cc_namespace_launcher_hides_aws_exposes_config(self, _mock_backend):
        wrapped, cleanup = wrap_argv(["echo", "hi"], mode="cc")
        assert cleanup is not None
        try:
            content = open(cleanup).read()
            assert "HIDE_SSH = False" in content
            assert ".aws" in content
            assert "EXPOSE_FILES" in content
            assert ".aws/config" in content
        finally:
            os.unlink(cleanup)


# Sentinel values only; never use real credentials in these tests.
_FAKE_CHANNEL_ENV = {
    "SLACK_BOT_TOKEN": "xoxb-FAKE-slack-bot",
    "SLACK_APP_TOKEN": "xapp-FAKE-slack-app",
    "SLACK_USER_TOKEN": "xoxp-FAKE-slack-user",
    "WECOM_BOT_ID": "FAKE-wecom-bot-id",
    "WECOM_SECRET": "FAKE-wecom-secret",
    "TELEGRAM_BOT_TOKEN": "0000:FAKE-telegram-token",
    "KIROCREW_OWNER_ID": "U_FAKE_OWNER",
}


class TestChannelCredentialIsolation:
    """Gateway-only channel credentials never reach agent subprocesses."""

    def test_denylist_covers_loader_credentials(self):
        from kiro_crew.config.loader import _CREDENTIAL_KEYS

        missing = set(_CREDENTIAL_KEYS) - set(_AGENT_DENIED_ENV_KEYS)
        assert not missing, f"loader credential keys not in agent denylist: {sorted(missing)}"

    def test_scrub_env_strips_channel_secrets(self, monkeypatch):
        for key, value in _FAKE_CHANNEL_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("KIROCREW_UNRELATED_KEEPME", "keep-this-value")

        cleaned = scrub_env()

        for key in _FAKE_CHANNEL_ENV:
            assert key not in cleaned, f"{key} leaked through scrub_env"
        assert cleaned.get("KIROCREW_UNRELATED_KEEPME") == "keep-this-value"

    def test_standard_spawn_strips_channel_secrets(self, monkeypatch):
        for key, value in _FAKE_CHANNEL_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("KIROCREW_UNRELATED_KEEPME", "keep-this-value")
        with patch("kiro_crew.sandbox.detect_backend", return_value="none"), patch(
            "kiro_crew.sandbox._allow_unsandboxed_exec", return_value=True
        ):
            _argv, env, cleanup = sandboxed_spawn_argv(["echo", "hi"], mode="standard")
        try:
            for key in _FAKE_CHANNEL_ENV:
                assert key not in env, f"{key} leaked into standard spawn env"
            assert env.get("KIROCREW_UNRELATED_KEEPME") == "keep-this-value"
        finally:
            if cleanup:
                os.unlink(cleanup)

    def test_cc_and_strict_launchers_strip_oss_channel_secrets(self):
        for mode in ("cc", "strict"):
            script = _build_launcher_script(mode)
            env_prefixes = next(
                line for line in script.splitlines() if line.startswith("ENV_PREFIXES = ")
            )
            for key in ("WECOM_BOT_ID", "WECOM_SECRET", "TELEGRAM_BOT_TOKEN"):
                assert key in env_prefixes, f"{key} missing from {mode} launcher"

    def test_macos_cc_launcher_strips_oss_channel_secrets(self, monkeypatch):
        keys = ("WECOM_BOT_ID", "WECOM_SECRET", "TELEGRAM_BOT_TOKEN")
        for key in keys:
            monkeypatch.setenv(key, _FAKE_CHANNEL_ENV[key])

        argv, cleanup = sandbox_exec_argv(["echo", "hi"], sandbox_level="cc")
        try:
            for key in keys:
                assert key in argv, f"{key} missing from sandbox-exec argv"
        finally:
            if cleanup:
                os.unlink(cleanup)

    def test_scrub_agent_denied_env_strips_all_denied_keys(self):
        env = dict(_FAKE_CHANNEL_ENV)
        env["KIROCREW_UNRELATED_KEEPME"] = "keep-this-value"

        cleaned = scrub_agent_denied_env(env)

        for key in _AGENT_DENIED_ENV_KEYS:
            assert key not in cleaned, f"{key} survived scrub_agent_denied_env"
        for key in _FAKE_CHANNEL_ENV:
            assert key not in cleaned, f"{key} survived scrub_agent_denied_env"
        assert cleaned.get("KIROCREW_UNRELATED_KEEPME") == "keep-this-value"

    def test_scrub_agent_denied_env_preserves_aws_ssh(self):
        # Unlike scrub_env, the parent channel-credential scrub must leave the
        # AWS/SSH env the standard sandbox intentionally exposes intact.
        env = {
            "WECOM_SECRET": "FAKE-wecom-secret",
            "AWS_ACCESS_KEY_ID": "FAKE-akid",
            "AWS_SECRET_ACCESS_KEY": "FAKE-secret",
            "AWS_SESSION_TOKEN": "FAKE-session",
            "SSH_AUTH_SOCK": "/tmp/fake-agent.sock",
            "PATH": "/usr/bin",
        }

        cleaned = scrub_agent_denied_env(env)

        assert "WECOM_SECRET" not in cleaned
        assert cleaned["AWS_ACCESS_KEY_ID"] == "FAKE-akid"
        assert cleaned["AWS_SECRET_ACCESS_KEY"] == "FAKE-secret"
        assert cleaned["AWS_SESSION_TOKEN"] == "FAKE-session"
        assert cleaned["SSH_AUTH_SOCK"] == "/tmp/fake-agent.sock"
        assert cleaned["PATH"] == "/usr/bin"
