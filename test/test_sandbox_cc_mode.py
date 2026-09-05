"""Tests for sandbox 'cc' mode — routing, dir lists, and profile generation."""

from __future__ import annotations

import os
import runpy
import textwrap
from pathlib import Path
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
        _sb_mod,
        "_KIRO_INTERNAL_SETTINGS_PATH",
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

    def test_extra_hidden_file_leaf_also_gets_a_literal_deny(self):
        """A file-shaped entry needs a ``literal`` rule, not only a ``subpath`` one.

        Most of what the adapter credential mask passes here is a plain FILE --
        ``.codex/auth.json``, ``.claude/.credentials.json``, ``.netrc``,
        ``.git-credentials``, ``sel_hmac.key`` -- and whether a ``subpath`` rule
        alone denies a non-directory was asserted in three comments in this tree
        while the ``crew_hidden`` branch of the same function said the opposite
        ("A leaf may be a plain file, which no subpath rule addresses"). Nothing
        executes ``sandbox-exec`` here, so that could not be settled by test; the
        profile emits BOTH shapes instead, and this pins the literal so the mask
        never depends on the unverified reading again.
        """
        leaf = "/Users/someone/.netrc"
        profile = _build_seatbelt_profile("standard", extra_hidden_dirs=(leaf,))

        assert f'(deny file-read* (literal "{leaf}"))' in profile
        assert f'(deny file-write* (literal "{leaf}"))' in profile
        assert f'(deny file-link (literal "{leaf}"))' in profile
        # the subpath rule stays -- a directory entry still needs it
        assert f'(deny file-read* (subpath "{leaf}"))' in profile

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
        assert "FEISHU_APP_ID" in _AGENT_DENIED_ENV_KEYS
        assert "FEISHU_APP_SECRET" in _AGENT_DENIED_ENV_KEYS

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
        """Every gateway-owned credential key is agent-denied.

        ``KIRO_API_KEY`` is the one deliberate exception: it is the AGENT's own
        model credential, not a gateway-owned channel token — kiro-cli reads it
        from its own environment, so denying it would break model auth in a
        post-scrub container. The spawn paths re-inject it explicitly
        (``config.loader.inject_kiro_cli_api_key``) instead of letting it ride
        the inherited environ.
        """
        from kiro_crew.config.loader import _CREDENTIAL_KEYS, CRED_KIRO_API_KEY

        missing = set(_CREDENTIAL_KEYS) - set(_AGENT_DENIED_ENV_KEYS) - {CRED_KIRO_API_KEY}
        assert not missing, f"loader credential keys not in agent denylist: {sorted(missing)}"
        # The carve-out stays exactly one key wide and never joins the denylist:
        # a denied KIRO_API_KEY would strip the agent's own credential.
        assert CRED_KIRO_API_KEY not in _AGENT_DENIED_ENV_KEYS

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
        with (
            patch("kiro_crew.sandbox.detect_backend", return_value="none"),
            patch("kiro_crew.sandbox._allow_unsandboxed_exec", return_value=True),
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


# ── The cc-mode expose pre-read ──
#
# Selective exposure keeps ~/.aws/config readable inside an otherwise-hidden
# ~/.aws, so credential_process still resolves. It is an optimisation, and the
# pre-read of it happens during sandbox SETUP -- so an OSError there aborts the
# child before the command runs at all. `isfile` covers ABSENT; these tests
# cover UNREADABLE, which is a different condition (an EACCES on open() still
# passes isfile when the path is traversable and stat-able).
#
# Like test_sandbox_hardlink_scan.py, these run the block from the SHIPPED
# launcher source rather than a copy, so they cannot drift from what the child
# actually executes.
_EXPOSE_BLOCK_START = "expose_data = {}"
_EXPOSE_BLOCK_END = "# Bind-mount empty dirs over credential paths"
#: Structural landmarks the slice must contain, so an edit that moves either
#: marker and shrinks the block fails HERE rather than leaving the assertions
#: below vacuously green against a fragment that no longer holds the read.
_EXPOSE_SLICE_LANDMARKS = (
    "for src_path, filename in EXPOSE_FILES:",  # the loop
    "os.path.isfile(src_path)",  # the absent-file guard
    'open(src_path, "rb")',  # the read itself
)


def _expose_pre_read_source() -> str:
    """The expose pre-read, lifted verbatim out of the generated cc launcher.

    Sliced from the START OF THE LINE, not from the marker: ``dedent`` measures
    the common prefix across all lines, so a first line already stripped of its
    indent leaves the rest indented and the block will not parse.
    """
    script = _build_launcher_script("cc")
    start = script.rindex("\n", 0, script.index(_EXPOSE_BLOCK_START)) + 1
    end = script.rindex("\n", 0, script.index(_EXPOSE_BLOCK_END, start)) + 1
    block = textwrap.dedent(script[start:end])
    missing = [mark for mark in _EXPOSE_SLICE_LANDMARKS if mark not in block]
    assert not missing, f"the extracted expose pre-read is missing {missing}"
    return block


def _run_expose_pre_read(
    *, expose_files: list[tuple[str, str]], tmp_path: Path
) -> tuple[dict[str, bytes], str]:
    """Run the pre-read over *expose_files*; return ``(expose_data, stderr)``.

    Via ``runpy.run_path`` rather than ``exec`` for the reason given in
    test_sandbox_hardlink_scan.py: ``exec`` trips the SAST gate's
    ``exec-detected`` rule, and a suppression would be this repo's first.
    """
    written: list[str] = []

    class _Stderr:
        def write(self, text: str) -> int:
            written.append(text)
            return len(text)

    fake_sys = type("_sys", (), {"stderr": _Stderr()})()
    block = tmp_path / "_expose_block.py"
    block.write_text(_expose_pre_read_source(), encoding="utf-8")
    result = runpy.run_path(
        str(block),
        init_globals={"os": os, "sys": fake_sys, "EXPOSE_FILES": expose_files},
    )
    return result["expose_data"], "".join(written)


def _require_eacces(path: Path) -> None:
    """Make *path* unreadable, or skip if this host cannot make it so."""
    path.chmod(0o000)
    if os.access(path, os.R_OK):  # root, or a filesystem ignoring the mode
        pytest.skip("this host can read a 0000 file; EACCES is unreachable")


class TestCcExposePreReadIsNonFatal:
    def test_an_unreadable_expose_source_does_not_abort_setup(self, tmp_path: Path) -> None:
        """The regression. Before the guard this raised PermissionError.

        The read sits in sandbox setup, so the exception killed the spawn
        outright. Measured consequence on one host: every cc-mode spawn died,
        which is the whole ``command`` cron kind (``run_command_sandboxed`` uses
        ``mode="cc"`` while ``run_script_sandboxed`` uses ``mode="standard"``),
        and the repeated failures latched three jobs into auto-pause.
        """
        src = tmp_path / "config"
        src.write_text("[default]\nregion = us-east-1\n", encoding="utf-8")
        _require_eacces(src)

        # Not raising IS the assertion; _run_expose_pre_read propagates.
        expose_data, _ = _run_expose_pre_read(
            expose_files=[(str(src), "config")], tmp_path=tmp_path
        )

        assert str(src) not in expose_data, "an unreadable source must not be exposed"

    def test_an_unreadable_expose_source_is_reported_on_stderr(self, tmp_path: Path) -> None:
        """Degrading SILENTLY would be the opposite of the intent.

        Without the exposure the child has no ~/.aws/config, so Bedrock auth
        fails later with an error pointing nowhere near the pre-read. The
        warning is what connects the two.
        """
        src = tmp_path / "config"
        src.write_text("[default]\n", encoding="utf-8")
        _require_eacces(src)

        _, stderr = _run_expose_pre_read(expose_files=[(str(src), "config")], tmp_path=tmp_path)

        assert stderr, "skipping an exposure silently must not be an option"
        assert str(src) in stderr, "the warning must name the path it skipped"

    def test_a_readable_expose_source_is_still_read(self, tmp_path: Path) -> None:
        """Positive control: the guard must not swallow the happy path.

        This passes with or without the guard, on purpose -- it is what would
        catch a "fix" that skipped every exposure.
        """
        src = tmp_path / "config"
        src.write_bytes(b"[default]\nregion = eu-west-1\n")

        expose_data, stderr = _run_expose_pre_read(
            expose_files=[(str(src), "config")], tmp_path=tmp_path
        )

        assert expose_data[str(src)] == b"[default]\nregion = eu-west-1\n"
        assert stderr == "", "a successful read must stay quiet"

    def test_an_absent_expose_source_stays_silent(self, tmp_path: Path) -> None:
        """``isfile`` still shorts out first, so absence is not a warning.

        ~/.aws/config does not exist on plenty of hosts. Warning there would put
        a line on stderr for every cc-mode spawn on all of them.
        """
        expose_data, stderr = _run_expose_pre_read(
            expose_files=[(str(tmp_path / "absent"), "config")], tmp_path=tmp_path
        )

        assert expose_data == {}
        assert stderr == "", "an absent optional exposure is not a problem"

    def test_the_guard_is_the_exception_not_a_pre_flight_access_check(self, tmp_path: Path) -> None:
        """`os.access` is not a valid substitute for catching the error.

        Measured on the affected host: `os.stat()` succeeded and `os.access()`
        reported both X_OK and R_OK as True while the operation was denied
        anyway. So a reviewer "tightening" the guard into
        `os.access(src_path, os.R_OK)` would look equivalent from the source and
        silently restore the abort. This pins the read as being attempted and the
        failure as being caught, by handing the block an `os` whose `access`
        lies exactly the way the real one did.
        """
        src = tmp_path / "config"
        src.write_text("[default]\n", encoding="utf-8")
        _require_eacces(src)

        opened: list[str] = []

        class _LyingOs:
            """The real ``os``, but ``access`` always says yes (as measured)."""

            def access(self, path, mode) -> bool:
                return True

            def __getattr__(self, name: str):
                return getattr(os, name)

        real_open = open

        def _counting_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        written: list[str] = []

        class _Stderr:
            def write(self, text: str) -> int:
                written.append(text)
                return len(text)

        fake_sys = type("_sys", (), {"stderr": _Stderr()})()
        block = tmp_path / "_expose_block_access.py"
        block.write_text(_expose_pre_read_source(), encoding="utf-8")
        result = runpy.run_path(
            str(block),
            init_globals={
                "os": _LyingOs(),
                "sys": fake_sys,
                "open": _counting_open,
                "EXPOSE_FILES": [(str(src), "config")],
            },
        )

        # A pre-flight os.access guard would have skipped the open entirely and
        # emitted nothing, so BOTH of these fail on that rewrite.
        assert opened == [str(src)], "the read must be attempted, not gated on os.access"
        assert "".join(written), "the denied read must still be reported"
        assert str(src) not in result["expose_data"]

    def test_one_unreadable_source_does_not_block_the_others(self, tmp_path: Path) -> None:
        """The skip is per entry, not per loop.

        ``_CC_EXPOSE_FILES`` carries one path today, so without this the
        per-entry scope is only implied by where the ``try`` sits.
        """
        bad = tmp_path / "unreadable"
        bad.write_text("x\n", encoding="utf-8")
        _require_eacces(bad)
        good = tmp_path / "readable"
        good.write_bytes(b"kept\n")

        expose_data, stderr = _run_expose_pre_read(
            expose_files=[(str(bad), "unreadable"), (str(good), "readable")],
            tmp_path=tmp_path,
        )

        assert str(bad) not in expose_data
        assert expose_data[str(good)] == b"kept\n"
        assert str(bad) in stderr
        assert str(good) not in stderr


# ── The known_hosts pre-read: same shape as the expose read, OPPOSITE remedy ──
#
# Same root cause (an unguarded `isfile` -> `open` that can raise during setup),
# but the safe direction is REVERSED, so this site fails CLOSED where the expose
# read degrades open. The asymmetry is not a style choice:
#
#   - an unreadable ~/.aws/config costs REACHABILITY;
#   - an unreadable known_hosts costs VERIFICATION, because the launcher sets
#     StrictHostKeyChecking=accept-new in GIT_SSH_COMMAND gated only on that
#     variable being unset -- never on whether this read succeeded. Continuing
#     with an empty kh_data therefore leaves auto-accept on with no trust
#     anchors, so any host key is accepted as new.
#
# Reach: HIDE_SSH is set at the DEFAULT strict level (`hide_ssh = sandbox_level
# == "strict"`, and `sandbox_level` defaults to "strict"), not just in cc mode.
#
# Sliced from the SHIPPED launcher for the same anti-drift reason as the block
# above. Only the pre-read is sliced, NOT the whole `.ssh` block: the lines
# around it call `_libc.mount()`, which cannot run in-process.
_KH_BLOCK_START = 'kh_data = b""'
_KH_BLOCK_END = "# Cross-fs source for the same kernel-race"
#: Structural landmarks, so an edit that moves a marker and shrinks the slice
#: fails HERE rather than leaving the assertions vacuously green.
_KH_SLICE_LANDMARKS = (
    "os.path.isfile(SSH_KNOWN_HOSTS)",  # the absent-file guard
    'open(SSH_KNOWN_HOSTS, "rb")',  # the read itself
)


def _known_hosts_pre_read_source() -> str:
    """The known_hosts pre-read, lifted verbatim out of the strict launcher."""
    script = _build_launcher_script("strict")
    start = script.rindex("\n", 0, script.index(_KH_BLOCK_START)) + 1
    end = script.rindex("\n", 0, script.index(_KH_BLOCK_END, start)) + 1
    block = textwrap.dedent(script[start:end])
    missing = [mark for mark in _KH_SLICE_LANDMARKS if mark not in block]
    assert not missing, f"the extracted known_hosts pre-read is missing {missing}"
    return block


def _run_known_hosts_pre_read(
    *, known_hosts: str, tmp_path: Path, stderr_sink: list[str] | None = None
) -> tuple[bytes, str]:
    """Run the pre-read over *known_hosts*; return ``(kh_data, stderr)``.

    Propagates whatever the block raises -- the refusal is the behaviour under
    test. Pass ``stderr_sink`` to keep the collected stderr reachable when it DOES
    raise, since the returned tuple is unreachable in exactly that case; the
    caller owns the list, so nothing has to be carried in module state.
    """
    written: list[str] = stderr_sink if stderr_sink is not None else []

    class _Stderr:
        def write(self, text: str) -> int:
            written.append(text)
            return len(text)

    fake_sys = type("_sys", (), {"stderr": _Stderr()})()
    block = tmp_path / "_known_hosts_block.py"
    block.write_text(_known_hosts_pre_read_source(), encoding="utf-8")
    result = runpy.run_path(
        str(block),
        init_globals={"os": os, "sys": fake_sys, "SSH_KNOWN_HOSTS": known_hosts},
    )
    return result["kh_data"], "".join(written)


class TestKnownHostsPreReadFailsClosed:
    def test_an_unreadable_known_hosts_aborts_setup(self, tmp_path: Path) -> None:
        """Unreadable host-trust data must FAIL CLOSED, not degrade.

        This test previously pinned the opposite, and that was a defect. The
        launcher injects ``StrictHostKeyChecking=accept-new`` into
        ``GIT_SSH_COMMAND`` (built at sandbox.py:1513-1515, applied at
        sandbox.py:1786-1793) gated only on that variable being unset -- NOT on
        whether known_hosts was restored. So degrading to an empty ``kh_data``
        leaves the sandbox pointing ``UserKnownHostsFile`` at an absent file
        while auto-accept is still on: every host reads as NEW, and an
        interceptor's key is accepted. With known_hosts PRESENT, ``accept-new``
        REFUSES a CHANGED key. Degrading therefore converts "refuse a changed
        key" into "accept anything".

        That is why this site is NOT symmetric with the EXPOSE_FILES pre-read.
        Hiding ~/.aws/config only costs reachability; hiding known_hosts removes
        a trust anchor while leaving the auto-accept that anchor was gating.
        """
        kh = tmp_path / "known_hosts"
        kh.write_text("example.com ssh-ed25519 AAAA\n", encoding="utf-8")
        _require_eacces(kh)

        stderr: list[str] = []
        with pytest.raises(OSError):
            _run_known_hosts_pre_read(known_hosts=str(kh), tmp_path=tmp_path, stderr_sink=stderr)

        # The refusal and its diagnostic are ONE behaviour observed from ONE setup,
        # so they are asserted together. Refusing silently would strand the operator
        # on a bare OSError out of a pre-read they have no reason to connect to host
        # trust, which is why the message is pinned as tightly as the raise.
        emitted = "".join(stderr)
        assert emitted, "refusing must not be silent"
        assert str(kh) in emitted, "the message must name the path"
        assert "FATAL" in emitted, "this is a refusal, not a warning"

    def test_a_readable_known_hosts_is_still_read(self, tmp_path: Path) -> None:
        """Positive control: the guard must not swallow the happy path.

        Passes with or without the guard, on purpose -- it is what would catch a
        "fix" that skipped the exposure unconditionally.
        """
        kh = tmp_path / "known_hosts"
        kh.write_bytes(b"host.example ssh-rsa BBBB\n")

        kh_data, stderr = _run_known_hosts_pre_read(known_hosts=str(kh), tmp_path=tmp_path)

        assert kh_data == b"host.example ssh-rsa BBBB\n"
        assert stderr == "", "a successful read must stay quiet"

    def test_an_absent_known_hosts_stays_silent(self, tmp_path: Path) -> None:
        """``isfile`` still shorts out first, so absence is not a warning.

        Plenty of hosts have a .ssh directory and no known_hosts; warning there
        would put a line on stderr for every strict-mode spawn on all of them.
        """
        kh_data, stderr = _run_known_hosts_pre_read(
            known_hosts=str(tmp_path / "absent"), tmp_path=tmp_path
        )

        assert kh_data == b""
        assert stderr == "", "an absent known_hosts is not a problem"

    def test_the_known_hosts_guard_is_the_exception_not_a_pre_flight_access_check(
        self, tmp_path: Path
    ) -> None:
        """`os.access` is not a valid substitute here either.

        Same measurement as the expose site: `os.stat()` succeeded and
        `os.access()` reported R_OK True while the read was denied anyway. Pinned
        the same way -- hand the block an `os` whose `access` lies, and assert
        the read was still ATTEMPTED and the failure CAUGHT.
        """
        kh = tmp_path / "known_hosts"
        kh.write_text("example.com ssh-ed25519 AAAA\n", encoding="utf-8")
        _require_eacces(kh)

        opened: list[str] = []

        class _LyingOs:
            """The real ``os``, but ``access`` always says yes (as measured)."""

            def access(self, path, mode) -> bool:
                return True

            def __getattr__(self, name: str):
                return getattr(os, name)

        real_open = open

        def _counting_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        written: list[str] = []

        class _Stderr:
            def write(self, text: str) -> int:
                written.append(text)
                return len(text)

        fake_sys = type("_sys", (), {"stderr": _Stderr()})()
        block = tmp_path / "_known_hosts_block_access.py"
        block.write_text(_known_hosts_pre_read_source(), encoding="utf-8")
        with pytest.raises(OSError):
            runpy.run_path(
                str(block),
                init_globals={
                    "os": _LyingOs(),
                    "sys": fake_sys,
                    "open": _counting_open,
                    "SSH_KNOWN_HOSTS": str(kh),
                },
            )

        # A pre-flight os.access guard would have skipped the open entirely,
        # emitted nothing, and CONTINUED with an empty kh_data -- which is the
        # fail-open this site must not do. All three of these fail on that
        # rewrite: no open attempted, no message, and no refusal.
        assert opened == [str(kh)], "the read must be attempted, not gated on os.access"
        assert "".join(written), "the denied read must still be reported"
        assert "FATAL" in "".join(written), "this is a refusal, not a warning"
