"""Sandbox 'cc' mode: the tier tables, the plan each tier yields, and the launcher stages.

The tier tables are read directly. What a tier masks, re-exposes and scrubs on Linux is
read off the confinement plan ``sandbox._spawn_plan`` builds for the namespace backend,
and the environment scrub runs as the launcher program's own ``scrub_env`` stage over
that plan's data. The launcher's two setup pre-reads -- the cc tier's exposed files and
the strict tier's ``known_hosts`` -- run as the program's own stages
(``preread_exposed_files``, ``mask_ssh_keys``) against real files. The macOS Seatbelt
profile and ``wrap_argv``'s routing are checked on what they produce.
"""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from test_sandbox_launcher_program import launch, payload, rendered_payload

import kiro_crew.sandbox as _sb_mod
from kiro_crew import sandbox_launcher_program as program
from kiro_crew.sandbox import (
    _AGENT_DENIED_ENV_KEYS,
    _CC_DIRS,
    _CC_EXPOSE_FILES,
    _CC_FILES,
    _STANDARD_DIRS,
    _build_seatbelt_profile,
    sandbox_exec_argv,
    sandboxed_spawn_argv,
    scrub_agent_denied_env,
    scrub_env,
    wrap_argv,
)
from kiro_crew.sandbox_plan import BACKEND_NAMESPACE, ConfinementPlan, namespace_payload

#: The launcher stages that MOUNT run against ``CoveringLibc``, which resolves each
#: ``/proc/self/fd/<n>`` target as only Linux can; the namespace launcher runs nowhere else.
_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the namespace launcher is Linux-only"
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
    # Planning a namespace spawn asks the HOST's ``ssh -V`` whether it knows
    # ``StrictHostKeyChecking=accept-new``. Nothing here is about that probe, and
    # a real ssh spawned from the test process is a host dependency the plan
    # must not vary with -- pin the answer so no binary runs.
    monkeypatch.setattr(_sb_mod, "_ssh_supports_accept_new", lambda: True)


def _plan(tier: str, **kwargs: object) -> ConfinementPlan:
    """The Linux namespace plan for one spawn at *tier* on this host."""
    return _sb_mod._spawn_plan(BACKEND_NAMESPACE, tier, **kwargs)


def _home(rel: str) -> str:
    """*rel* under ``$HOME``, spelled as the plan spells a tier entry."""
    return os.path.join(str(Path.home()), rel)


def _scrubbed(tmp_path: Path, tier: str, environ: dict[str, str]) -> dict[str, str]:
    """The environment the agent inherits once the launcher has scrubbed *environ*.

    The plan's own launcher data drives ``scrub_env``, so this is what the child execs
    with, not a list the test assembled.
    """
    run = launch(tmp_path, namespace_payload(_plan(tier)), environ=dict(environ))
    program.scrub_env(run)
    return run.environ


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
    """What each tier's Linux plan masks, re-exposes and leaves visible.

    ``_build_launcher_script`` renders exactly this plan into the launcher, so a field
    here is what the child acts on.
    """

    def test_extra_hidden_directory_is_bound_over(self):
        plan = _plan("strict", extra_hidden_dirs=("/private/kiro/crew",))

        assert "/private/kiro/crew" in plan.sensitive_dirs

    def test_cc_mode_uses_cc_dirs(self):
        masked = _plan("cc").sensitive_dirs
        for d in _CC_DIRS:
            assert _home(d) in masked, f"{d} should be masked by the cc launcher"

    def test_cc_mode_includes_expose_files(self):
        assert (_home(".aws/config"), "config") in _plan("cc").expose

    def test_cc_mode_includes_sensitive_files(self):
        files = _plan("cc").sensitive_files
        for f in _CC_FILES:
            assert _home(f) in files, f"{f} should be masked by the cc launcher's file loop"

    def test_cc_mode_does_not_hide_ssh(self):
        assert _plan("cc").hide_ssh is False

    def test_strict_mode_hides_ssh(self):
        assert _plan("strict").hide_ssh is True

    def test_standard_mode_does_not_hide_ssh(self):
        assert _plan("standard").hide_ssh is False

    def test_standard_mode_uses_standard_dirs(self):
        masked = _plan("standard").sensitive_dirs
        for d in _STANDARD_DIRS:
            assert _home(d) in masked

    def test_standard_mode_no_expose_files(self):
        assert _plan("standard").expose == ()

    def test_extra_expose_files_are_embedded_with_their_basename(self):
        """The enforced-adapter mask's Linux half rides the cc expose primitive.

        ``acp_tool_gate.adapter_expose_files`` hands absolute paths here; each
        must land in the plan's exposed files as a ``(source, basename)`` pair so
        the launcher restores a read-only copy inside the hidden parent. Standard
        tier, because that is the tier the codex adapter actually runs under.
        """
        plan = _plan("standard", extra_expose_files=("/h/u/.aws/config",))
        assert ("/h/u/.aws/config", "config") in plan.expose

    def test_cc_expose_files_survive_extra_entries(self):
        plan = _plan("cc", extra_expose_files=("/h/u/.aws/config",))
        assert (_home(".aws/config"), "config") in plan.expose
        assert ("/h/u/.aws/config", "config") in plan.expose

    def test_an_extra_expose_file_already_in_the_tier_list_appears_once(self):
        """cc + codex both name ``~/.aws/config``; the plan must carry it once.

        The restore loop writes each entry's destination then chmods it 0444, so
        a duplicate entry's second open-for-write raises PermissionError inside
        the launcher and the spawn dies. Without the dedupe the pair appears twice.
        """
        cfg = os.path.join(str(Path.home()), ".aws", "config")
        expose = _plan("cc", extra_expose_files=(cfg,)).expose
        assert expose.count((cfg, "config")) == 1, expose


class TestBuildSeatbeltProfileCcMode:
    def test_extra_expose_file_carves_read_only_out_of_an_extra_hidden_dir(self):
        """The enforced adapter's ``~/.aws/config`` on macOS.

        Read deny becomes ``require-all (subpath) (require-not (literal))`` --
        the same shape the strict tier uses for ``.ssh/known_hosts`` -- while the
        write and hardlink denies stay blanket over the subpath, so the child
        can read its ``credential_process`` entry and nothing else, and cannot
        rewrite or hardlink the file it is allowed to read.
        """
        profile = _build_seatbelt_profile(
            "standard",
            extra_hidden_dirs=("/h/u/.aws",),
            extra_expose_files=("/h/u/.aws/config",),
        )
        assert (
            '(deny file-read* (require-all (subpath "/h/u/.aws")'
            ' (require-not (literal "/h/u/.aws/config"))))'
        ) in profile
        assert '(deny file-read* (subpath "/h/u/.aws"))' not in profile
        assert '(deny file-write* (subpath "/h/u/.aws"))' in profile
        assert '(deny file-link (subpath "/h/u/.aws"))' in profile

    def test_extra_expose_file_is_carved_when_the_tier_already_hides_the_dir(self):
        """strict already lists ``.aws``; the tier loop must carry the carve-out too.

        Seatbelt cannot cancel an earlier blanket deny with a later narrower one,
        so if only the extra-hidden loop carved the file out, a strict-tier codex
        would still fail auth. Both loops must emit the ``require-not`` shape and
        neither may emit the bare subpath read deny for that dir.
        """
        home = str(Path.home())
        aws = os.path.join(home, ".aws")
        cfg = os.path.join(aws, "config")
        profile = _build_seatbelt_profile(
            "strict", extra_hidden_dirs=(aws,), extra_expose_files=(cfg,)
        )
        assert f'(deny file-read* (subpath "{aws}"))' not in profile
        assert f'(require-not (literal "{cfg}"))' in profile
        assert f'(deny file-write* (subpath "{aws}"))' in profile

    def test_extra_expose_file_outside_any_hidden_dir_emits_nothing(self):
        """No deny to carve out of means no rule at all -- never a bare allow."""
        profile = _build_seatbelt_profile(
            "standard",
            extra_hidden_dirs=("/h/u/.kube",),
            extra_expose_files=("/h/u/.aws/config",),
        )
        assert ".aws/config" not in profile
        assert '(deny file-read* (subpath "/h/u/.kube"))' in profile

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
    propagates into os.environ for trusted children. The namespace launcher and
    the sandbox-exec wrapper both scrub these keys."""

    def test_default_set_includes_slack_tokens(self):
        assert "SLACK_BOT_TOKEN" in _AGENT_DENIED_ENV_KEYS
        assert "SLACK_APP_TOKEN" in _AGENT_DENIED_ENV_KEYS
        assert "KIROCREW_OWNER_ID" in _AGENT_DENIED_ENV_KEYS
        assert "FEISHU_APP_ID" in _AGENT_DENIED_ENV_KEYS
        assert "FEISHU_APP_SECRET" in _AGENT_DENIED_ENV_KEYS

    def test_cc_launcher_scrubs_agent_creds(self, tmp_path):
        """The cc plan's scrub list carries the cred keys, and the launcher drops them."""
        prefixes = _plan("cc").env_scrub_prefixes
        for key in _AGENT_DENIED_ENV_KEYS:
            assert key in prefixes, f"{key} should be in the cc launcher's scrub list"
        env = _scrubbed(tmp_path, "cc", {key: "FAKE" for key in _AGENT_DENIED_ENV_KEYS})
        leaked = sorted(key for key in _AGENT_DENIED_ENV_KEYS if key in env)
        assert not leaked, f"the cc launcher let these reach the agent: {leaked}"

    def test_strict_launcher_scrubs_agent_creds(self, tmp_path):
        prefixes = _plan("strict").env_scrub_prefixes
        for key in _AGENT_DENIED_ENV_KEYS:
            assert key in prefixes
        env = _scrubbed(tmp_path, "strict", {key: "FAKE" for key in _AGENT_DENIED_ENV_KEYS})
        assert not [key for key in _AGENT_DENIED_ENV_KEYS if key in env]

    def test_standard_launcher_does_not_scrub_agent_creds(self, tmp_path):
        """Standard mode is for trusted subprocess wrappers (git, aws CLI,
        kubectl). They legitimately need Slack tokens for things like cron
        scripts. Only cc/strict (LLM-controlled agents) scrub them."""
        prefixes = _plan("standard").env_scrub_prefixes
        # No scrub entry may even contain a token's name: the list is the standard
        # launcher's whole scrub, so a key inside any entry is a key it targets.
        for key in _AGENT_DENIED_ENV_KEYS:
            assert not any(
                key in prefix for prefix in prefixes
            ), f"{key} should NOT be in the standard launcher's scrub list"
        env = _scrubbed(tmp_path, "standard", {key: "FAKE" for key in _AGENT_DENIED_ENV_KEYS})
        dropped = sorted(key for key in _AGENT_DENIED_ENV_KEYS if key not in env)
        assert not dropped, f"the standard launcher scrubbed {dropped}"

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
        """The launcher ``wrap_argv`` writes for cc carries the cc plan: ``~/.aws``
        masked with its ``config`` re-exposed, and ``~/.ssh`` left alone."""
        wrapped, cleanup = wrap_argv(["echo", "hi"], mode="cc")
        assert cleanup is not None
        try:
            carried = rendered_payload(Path(cleanup).read_text(encoding="utf-8"))
            assert carried["hide_ssh"] == 0
            assert _home(".aws") in carried["sensitive_dirs"]
            assert [_home(".aws/config"), "config"] in carried["expose_files"]
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
        from kiro_crew.config.loader import CRED_KIRO_API_KEY, CREDENTIAL_KEYS

        missing = set(CREDENTIAL_KEYS) - set(_AGENT_DENIED_ENV_KEYS) - {CRED_KIRO_API_KEY}
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

    def test_cc_and_strict_launchers_strip_oss_channel_secrets(self, tmp_path):
        keys = ("WECOM_BOT_ID", "WECOM_SECRET", "TELEGRAM_BOT_TOKEN")
        for mode in ("cc", "strict"):
            env_prefixes = _plan(mode).env_scrub_prefixes
            for key in keys:
                assert key in env_prefixes, f"{key} missing from {mode} launcher"
            env = _scrubbed(tmp_path, mode, {key: _FAKE_CHANNEL_ENV[key] for key in keys})
            for key in keys:
                assert key not in env, f"{key} reached the agent through the {mode} launcher"

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
# These run the launcher program's own stage,
# ``sandbox_launcher_program.preread_exposed_files``, over real files, so they
# cannot drift from what the child actually executes. The stage records what it
# read in ``Launch.expose_data``, the bytes the later restore writes back into
# the empty mask, and that is what is asserted.


def _run_expose_pre_read(
    *, expose_files: list[tuple[str, str]], tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> tuple[dict[str, bytes], str]:
    """Run the pre-read over *expose_files*; return ``(expose_data, stderr)``.

    Propagates whatever the stage raises, so not raising is itself asserted.
    """
    run = launch(tmp_path, payload(expose_files=[[src, name] for src, name in expose_files]))
    capfd.readouterr()
    program.preread_exposed_files(run)
    return run.expose_data, capfd.readouterr().err


def _require_eacces(path: Path) -> None:
    """Make *path* unreadable, or skip if this host cannot make it so."""
    path.chmod(0o000)
    if os.access(path, os.R_OK):  # root, or a filesystem ignoring the mode
        pytest.skip("this host can read a 0000 file; EACCES is unreachable")


def _denied_read_reported(stderr: str) -> bool:
    """Whether *stderr* carries the error an ``open`` of an unreadable file raised.

    The stage reports the exception it caught, so this text is present only when the
    read was ATTEMPTED and denied -- never when something skipped the read up front.
    """
    return f"[Errno {errno.EACCES}]" in stderr


class TestCcExposePreReadIsNonFatal:
    def test_an_unreadable_expose_source_does_not_abort_setup(self, tmp_path: Path, capfd) -> None:
        """The regression the guard closes. Without it this raises PermissionError.

        The read sits in sandbox setup, so the exception kills the spawn
        outright. Measured consequence on one host: every cc-mode spawn died,
        which is both cron kinds (``run_command_sandboxed`` and
        ``run_script_sandboxed`` both use ``mode="cc"``),
        and the repeated failures latched three jobs into auto-pause.
        """
        src = tmp_path / "config"
        src.write_text("[default]\nregion = us-east-1\n", encoding="utf-8")
        _require_eacces(src)

        # Not raising IS the assertion; _run_expose_pre_read propagates.
        expose_data, _ = _run_expose_pre_read(
            expose_files=[(str(src), "config")], tmp_path=tmp_path, capfd=capfd
        )

        assert str(src) not in expose_data, "an unreadable source must not be exposed"

    def test_an_unreadable_expose_source_is_reported_on_stderr(self, tmp_path: Path, capfd) -> None:
        """Degrading SILENTLY would be the opposite of the intent.

        Without the exposure the child has no ~/.aws/config, so Bedrock auth
        fails later with an error pointing nowhere near the pre-read. The
        warning is what connects the two.
        """
        src = tmp_path / "config"
        src.write_text("[default]\n", encoding="utf-8")
        _require_eacces(src)

        _, stderr = _run_expose_pre_read(
            expose_files=[(str(src), "config")], tmp_path=tmp_path, capfd=capfd
        )

        assert stderr, "skipping an exposure silently must not be an option"
        assert str(src) in stderr, "the warning must name the path it skipped"

    def test_a_readable_expose_source_is_still_read(self, tmp_path: Path, capfd) -> None:
        """Positive control: the guard must not swallow the happy path.

        This passes with or without the guard, on purpose -- it is what would
        catch a "fix" that skipped every exposure.
        """
        src = tmp_path / "config"
        src.write_bytes(b"[default]\nregion = eu-west-1\n")

        expose_data, stderr = _run_expose_pre_read(
            expose_files=[(str(src), "config")], tmp_path=tmp_path, capfd=capfd
        )

        assert expose_data[str(src)] == b"[default]\nregion = eu-west-1\n"
        assert stderr == "", "a successful read must stay quiet"

    def test_an_absent_expose_source_stays_silent(self, tmp_path: Path, capfd) -> None:
        """``isfile`` still shorts out first, so absence is not a warning.

        ~/.aws/config does not exist on plenty of hosts. Warning there would put
        a line on stderr for every cc-mode spawn on all of them.
        """
        expose_data, stderr = _run_expose_pre_read(
            expose_files=[(str(tmp_path / "absent"), "config")], tmp_path=tmp_path, capfd=capfd
        )

        assert expose_data == {}
        assert stderr == "", "an absent optional exposure is not a problem"

    def test_the_guard_is_the_exception_not_a_pre_flight_access_check(
        self, tmp_path: Path, capfd, monkeypatch
    ) -> None:
        """`os.access` is not a valid substitute for catching the error.

        Measured on the affected host: `os.stat()` succeeded and `os.access()`
        reported both X_OK and R_OK as True while the operation was denied
        anyway. So a reviewer "tightening" the guard into
        `os.access(src_path, os.R_OK)` would look equivalent from the source and
        silently restore the abort. This pins the read as being attempted and the
        failure as being caught, by running the stage while `os.access` lies
        exactly the way the real one did.
        """
        src = tmp_path / "config"
        src.write_text("[default]\n", encoding="utf-8")
        _require_eacces(src)

        with monkeypatch.context() as patched:
            patched.setattr(os, "access", lambda _path, _mode, **_kw: True)
            expose_data, stderr = _run_expose_pre_read(
                expose_files=[(str(src), "config")], tmp_path=tmp_path, capfd=capfd
            )

        # A pre-flight os.access guard trusts the lie and opens unguarded, which
        # raises out of the stage; one that skips emits nothing. Both fail here.
        assert _denied_read_reported(stderr), "the read must be attempted, not gated on os.access"
        assert str(src) in stderr, "the denied read must still be reported"
        assert str(src) not in expose_data

    def test_one_unreadable_source_does_not_block_the_others(self, tmp_path: Path, capfd) -> None:
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
            capfd=capfd,
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
#     with no known_hosts therefore leaves auto-accept on with no trust
#     anchors, so any host key is accepted as new.
#
# Reach: the plan hides ~/.ssh at the DEFAULT strict level (`hide_ssh` is
# `tier == "strict"`, and `sandbox_level` defaults to "strict"), not just in cc mode.
#
# These run the launcher program's own ``mask_ssh_keys`` stage over a real
# ~/.ssh. Its read comes first, so a refusal is observed before any mount; the
# cases that get past the read go on to bind the stand-in over ~/.ssh, through
# ``CoveringLibc``, and are Linux-only like the launcher itself.


def _ssh_launch(tmp_path: Path, known_hosts: bytes | None) -> tuple[program.Launch, Path, Path]:
    """A strict-tier launch over a real ``~/.ssh`` holding a key and, optionally, known_hosts."""
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("key", encoding="utf-8")
    kh = ssh / "known_hosts"
    if known_hosts is not None:
        kh.write_bytes(known_hosts)
    run = launch(tmp_path, payload(hide_ssh=1, ssh_dir=str(ssh), ssh_known_hosts=str(kh)))
    return run, ssh, kh


class TestKnownHostsPreReadFailsClosed:
    def test_an_unreadable_known_hosts_aborts_setup(self, tmp_path: Path, capfd) -> None:
        """Unreadable host-trust data must FAIL CLOSED, not degrade.

        This must fail closed because the launcher's ``scrub_env`` stage injects
        ``StrictHostKeyChecking=accept-new`` into ``GIT_SSH_COMMAND`` gated only
        on that variable being unset -- NOT on whether known_hosts was restored.
        So degrading to no known_hosts leaves the sandbox pointing
        ``UserKnownHostsFile`` at an absent file while auto-accept is still on:
        every host reads as NEW, and an interceptor's key is accepted. With
        known_hosts PRESENT, ``accept-new`` REFUSES a CHANGED key. Degrading
        therefore converts "refuse a changed key" into "accept anything".

        That is why this site is NOT symmetric with the exposed-file pre-read.
        Hiding ~/.aws/config only costs reachability; hiding known_hosts removes
        a trust anchor while leaving the auto-accept that anchor was gating.
        """
        run, _ssh, kh = _ssh_launch(tmp_path, b"example.com ssh-ed25519 AAAA\n")
        _require_eacces(kh)
        capfd.readouterr()

        with pytest.raises(OSError):
            program.mask_ssh_keys(run)

        # The refusal and its diagnostic are ONE behaviour observed from ONE setup,
        # so they are asserted together. Refusing silently would strand the operator
        # on a bare OSError out of a pre-read they have no reason to connect to host
        # trust, which is why the message is pinned as tightly as the raise.
        emitted = capfd.readouterr().err
        assert emitted, "refusing must not be silent"
        assert str(kh) in emitted, "the message must name the path"
        assert "FATAL" in emitted, "this is a refusal, not a warning"

    @_LINUX_ONLY
    def test_a_readable_known_hosts_is_still_read(self, tmp_path: Path, capfd) -> None:
        """Positive control: the guard must not swallow the happy path.

        Passes with or without the guard, on purpose -- it is what would catch a
        "fix" that skipped the exposure unconditionally. What was read is what
        the masked ~/.ssh holds afterwards.
        """
        run, ssh, _kh = _ssh_launch(tmp_path, b"host.example ssh-rsa BBBB\n")
        capfd.readouterr()

        program.mask_ssh_keys(run)

        assert sorted(os.listdir(ssh)) == ["known_hosts"], "the keys were not masked"
        assert (ssh / "known_hosts").read_bytes() == b"host.example ssh-rsa BBBB\n"
        assert capfd.readouterr().err == "", "a successful read must stay quiet"

    @_LINUX_ONLY
    def test_an_absent_known_hosts_stays_silent(self, tmp_path: Path, capfd) -> None:
        """``isfile`` still shorts out first, so absence is not a warning.

        Plenty of hosts have a .ssh directory and no known_hosts; warning there
        would put a line on stderr for every strict-mode spawn on all of them.
        """
        run, ssh, _kh = _ssh_launch(tmp_path, None)
        capfd.readouterr()

        program.mask_ssh_keys(run)

        assert os.listdir(ssh) == [], "the masked ~/.ssh must hold no known_hosts"
        assert capfd.readouterr().err == "", "an absent known_hosts is not a problem"

    def test_the_known_hosts_guard_is_the_exception_not_a_pre_flight_access_check(
        self, tmp_path: Path, capfd, monkeypatch
    ) -> None:
        """`os.access` is not a valid substitute here either.

        Same measurement as the expose site: `os.stat()` succeeded and
        `os.access()` reported R_OK True while the read was denied anyway. Pinned
        the same way -- run the stage while `os.access` lies, and assert the read
        was still ATTEMPTED and the failure CAUGHT, reported and re-raised.
        """
        run, _ssh, kh = _ssh_launch(tmp_path, b"example.com ssh-ed25519 AAAA\n")
        _require_eacces(kh)
        capfd.readouterr()

        with monkeypatch.context() as patched:
            patched.setattr(os, "access", lambda _path, _mode, **_kw: True)
            with pytest.raises(OSError) as raised:
                program.mask_ssh_keys(run)

        # A pre-flight os.access guard would skip the open and CONTINUE with no
        # known_hosts -- the fail-open this site must not do. The error that
        # escapes is the denied open of known_hosts itself, and it was reported.
        assert raised.value.errno == errno.EACCES, "the read must be attempted"
        assert raised.value.filename == str(kh), "the read must be of known_hosts"
        emitted = capfd.readouterr().err
        assert _denied_read_reported(emitted), "the denied read must still be reported"
        assert "FATAL" in emitted, "this is a refusal, not a warning"
