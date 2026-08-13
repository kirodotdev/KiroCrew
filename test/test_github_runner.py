"""The shared hardened gh runner (``kiro_crew.github_runner``).

One module now owns trusted-binary resolution, the minimal child environment,
and the SEL-audited spawn chokepoint for every ``gh``-spawning surface (the
dashboard PR sidebar, Issue Radar, Code Review Sage). These tests lock in the
properties that used to drift between the three copies:

* resolver precedence (caller override → ``KIROCREW_GH_BIN`` → candidates),
  including the fail-loud rule for an override that is SET but empty or wrong
  — silently ignoring a set override was the weaker of the historical
  behaviors and is pinned OUT here;
* the exact child environment: gh-scoped auth/network/TLS keys pass through,
  nothing else from a polluted gateway environment does (AWS/Slack/SSH), and
  ``pin_host`` pins ``GH_HOST`` for callers whose bare API paths cannot pass
  ``--hostname``;
* a SEL audit event on success, non-zero exit, and timeout for EVERY spawn —
  the property one of the three copies had silently lost;
* the re-export seams that keep the historical import locations working.
"""

from __future__ import annotations

import subprocess
import sys
from unittest import mock

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only gh hardening")

from kiro_crew import github_runner as runner  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_runner_state(monkeypatch):
    runner.reset_cache()
    monkeypatch.delenv("KIROCREW_GH_BIN", raising=False)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(runner, "agent_writable_roots", lambda: ())
    yield
    runner.reset_cache()


def _fake_gh(directory, name: str = "gh") -> str:
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / name
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return str(binary)


# ── resolve_gh ───────────────────────────────────────────────────────────────


class TestResolveGh:
    @pytest.fixture(autouse=True)
    def _hermetic_ownership(self, monkeypatch):
        """These tests pin resolution ORDER, caching, and messages — not the
        ownership policy (covered by the moved validation tests). The per-
        component ownership walk depends on who owns the host's tmp ancestry,
        so it is no-opped to keep the suite hermetic on any runner."""
        monkeypatch.setattr(
            runner, "check_provider_path_component",
            lambda path, *, label, uid, strict: None,
        )

    def test_caller_override_wins_over_generic_and_candidates(self, monkeypatch, tmp_path):
        caller = _fake_gh(tmp_path / "caller-bin")
        generic = _fake_gh(tmp_path / "generic-bin")
        candidate = _fake_gh(tmp_path / "candidate-bin")
        monkeypatch.setenv("KIROCREW_TEST_GH", caller)
        monkeypatch.setenv("KIROCREW_GH_BIN", generic)
        monkeypatch.setattr(
            runner, "PROVIDER_EXECUTABLE_CANDIDATES", {"gh": (candidate,), "glab": ()}
        )

        assert runner.resolve_gh(override_env="KIROCREW_TEST_GH") == caller

    def test_generic_override_wins_when_caller_var_is_unset(self, monkeypatch, tmp_path):
        generic = _fake_gh(tmp_path / "generic-bin")
        candidate = _fake_gh(tmp_path / "candidate-bin")
        monkeypatch.delenv("KIROCREW_TEST_GH", raising=False)
        monkeypatch.setenv("KIROCREW_GH_BIN", generic)
        monkeypatch.setattr(
            runner, "PROVIDER_EXECUTABLE_CANDIDATES", {"gh": (candidate,), "glab": ()}
        )

        assert runner.resolve_gh(override_env="KIROCREW_TEST_GH") == generic

    def test_candidates_are_used_without_any_override(self, monkeypatch, tmp_path):
        candidate = _fake_gh(tmp_path / "candidate-bin")
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(
            runner, "PROVIDER_EXECUTABLE_CANDIDATES", {"gh": (candidate,), "glab": ()}
        )

        assert runner.resolve_gh() == candidate

    def test_a_set_but_empty_override_fails_loudly(self, monkeypatch, tmp_path):
        """D4 lock-in: a SET override — even empty — is validated, never skipped.

        Silently falling through to the candidate scan would run a binary the
        operator was explicitly steering away from.
        """
        candidate = _fake_gh(tmp_path / "candidate-bin")
        monkeypatch.setenv("KIROCREW_TEST_GH", "")
        monkeypatch.setattr(
            runner, "PROVIDER_EXECUTABLE_CANDIDATES", {"gh": (candidate,), "glab": ()}
        )

        with pytest.raises(runner.SetupError, match="KIROCREW_TEST_GH.*path must be absolute"):
            runner.resolve_gh(override_env="KIROCREW_TEST_GH")

    def test_a_wrong_override_names_the_variable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_TEST_GH", str(tmp_path / "missing-gh"))

        with pytest.raises(runner.SetupError, match="KIROCREW_TEST_GH.*failed validation"):
            runner.resolve_gh(override_env="KIROCREW_TEST_GH")

    def test_strict_mode_refuses_path_hits(self, monkeypatch, tmp_path):
        planted = _fake_gh(tmp_path / "user-bin")
        monkeypatch.setenv("KIROCREW_PROVIDER_BIN_STRICT", "1")
        monkeypatch.setenv("PATH", str(tmp_path / "user-bin"))
        monkeypatch.setattr(
            runner,
            "PROVIDER_EXECUTABLE_CANDIDATES",
            {"gh": ("/nonexistent-kirocrew/gh",), "glab": ()},
        )

        with pytest.raises(runner.SetupError) as excinfo:
            runner.resolve_gh()
        # The user-owned PATH hit was never even considered a candidate.
        assert planted not in str(excinfo.value)

    def test_missing_gh_message_gives_install_guidance(self, monkeypatch):
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(
            runner,
            "PROVIDER_EXECUTABLE_CANDIDATES",
            {"gh": ("/nonexistent-kirocrew/gh",), "glab": ()},
        )

        with pytest.raises(runner.SetupError) as excinfo:
            runner.resolve_gh(override_env="KIROCREW_TEST_GH")

        message = str(excinfo.value)
        assert "brew install gh" in message
        assert "gh auth login" in message
        assert "KIROCREW_TEST_GH" in message
        # "path does not exist" rejections are noise, not guidance.
        assert "does not exist" not in message

    def test_resolution_is_cached_and_reset_clears_it(self, monkeypatch, tmp_path):
        candidate = _fake_gh(tmp_path / "candidate-bin")
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(
            runner, "PROVIDER_EXECUTABLE_CANDIDATES", {"gh": (candidate,), "glab": ()}
        )
        first = runner.resolve_gh()
        with mock.patch.object(runner, "validate_provider_executable") as validate:
            assert runner.resolve_gh() == first
            validate.assert_not_called()

        runner.reset_cache()
        with mock.patch.object(
            runner, "validate_provider_executable", return_value=candidate
        ) as validate:
            assert runner.resolve_gh() == candidate
            validate.assert_called()

    def test_a_changed_override_value_is_not_served_from_cache(self, monkeypatch, tmp_path):
        first = _fake_gh(tmp_path / "first-bin")
        second = _fake_gh(tmp_path / "second-bin")
        monkeypatch.setenv("KIROCREW_TEST_GH", first)
        assert runner.resolve_gh(override_env="KIROCREW_TEST_GH") == first
        monkeypatch.setenv("KIROCREW_TEST_GH", second)
        assert runner.resolve_gh(override_env="KIROCREW_TEST_GH") == second


# ── gh_env ───────────────────────────────────────────────────────────────────


class TestGhEnv:
    def test_polluted_gateway_env_never_reaches_the_child(self, monkeypatch):
        """The D3 lock-in: gh-scoped auth/network/TLS keys pass, secrets do not."""
        polluted = {
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "AWS_ACCESS_KEY_ID": "AKIAXXXX",
            "SLACK_BOT_TOKEN": "xoxb-secret",
            "SSH_AUTH_SOCK": "/run/agent.sock",
            "SSH_AGENT_PID": "4242",
            "GIT_SSH_COMMAND": "ssh -i /home/user/.ssh/id_rsa",
            "KIROCREW_INTERNAL_TOKEN": "internal",
            "GH_TOKEN": "gho_token",
            "GH_ENTERPRISE_TOKEN": "ghe_token",
            "GITHUB_ENTERPRISE_TOKEN": "ghe_token2",
            "GH_CONFIG_DIR": "/home/user/.config/gh",
            "ALL_PROXY": "socks5://proxy:1080",
            "REQUESTS_CA_BUNDLE": "/etc/ssl/bundle.pem",
            "CURL_CA_BUNDLE": "/etc/ssl/curl.pem",
        }
        for key, value in polluted.items():
            monkeypatch.setenv(key, value)

        env = runner.gh_env()

        for secret_key in (
            "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "SLACK_BOT_TOKEN",
            "SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_SSH_COMMAND", "KIROCREW_INTERNAL_TOKEN",
        ):
            assert secret_key not in env, secret_key
        for passthrough_key in (
            "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_CONFIG_DIR",
            "ALL_PROXY", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        ):
            assert env[passthrough_key] == polluted[passthrough_key]
        # Deterministic output pins, always present.
        assert env["GH_PAGER"] == "cat"
        assert env["NO_COLOR"] == "1"

    def test_every_key_is_allowlisted(self, monkeypatch):
        """Exact-set property: everything in the child env is either the safe
        base, the gh passthrough, or one of the fixed pins — nothing else."""
        monkeypatch.setenv("GH_TOKEN", "gho_token")
        monkeypatch.setenv("SOME_RANDOM_SECRET", "boom")
        from kiro_crew.apps import registry

        allowed = (
            set(registry._SAFE_ENV_KEYS)
            | set(runner.GH_ENV_PASSTHROUGH)
            | {"GH_PAGER", "NO_COLOR", "GH_HOST"}
        )
        for key in runner.gh_env(pin_host="github.com"):
            assert key in allowed, key

    def test_pin_host_sets_gh_host_and_unpinned_does_not(self, monkeypatch):
        monkeypatch.delenv("GH_HOST", raising=False)
        assert "GH_HOST" not in runner.gh_env()
        assert runner.gh_env(pin_host="github.com")["GH_HOST"] == "github.com"

    def test_pin_host_overrides_an_ambient_gh_host(self, monkeypatch):
        """A configured enterprise default cannot survive the pin — this is the
        property the sidebar's bare API paths rely on."""
        monkeypatch.setenv("GH_HOST", "ghe.internal.example")
        assert runner.gh_env(pin_host="github.com")["GH_HOST"] == "github.com"


# ── run_gh ───────────────────────────────────────────────────────────────────


def _proc(returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout="", stderr="")


class TestRunGh:
    def test_refuses_a_non_absolute_binary(self):
        with pytest.raises(runner.SetupError, match="absolute gh path"):
            runner.run_gh(["gh", "api", "user"], timeout=5, audit_caller="core:test")

    def test_spawn_env_is_exactly_gh_env(self, monkeypatch):
        """D1 lock-in: the chokepoint hands the child gh_env(), never the
        gateway's full environment."""
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
        monkeypatch.setenv("GH_TOKEN", "gho_token")
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return _proc()

        with mock.patch.object(runner.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(runner, "_audit_run"):
            runner.run_gh(["/usr/bin/gh", "api", "user"], timeout=5, audit_caller="core:test")

        assert captured["kwargs"]["env"] == runner.gh_env()
        assert "AWS_SECRET_ACCESS_KEY" not in captured["kwargs"]["env"]
        assert captured["argv"] == ["/usr/bin/gh", "api", "user"]
        assert captured["kwargs"]["timeout"] == 5
        assert "shell" not in captured["kwargs"]

    def test_pin_host_is_forwarded_to_the_child_env(self, monkeypatch):
        """Issue Radar's bare API paths never pass --hostname, so its run_gh
        calls pin GH_HOST — an ambient enterprise default must not steer them."""
        monkeypatch.setenv("GH_HOST", "ghe.internal.example")
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["kwargs"] = kwargs
            return _proc()

        with mock.patch.object(runner.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(runner, "_audit_run"):
            runner.run_gh(
                ["/usr/bin/gh", "api", "user"], timeout=5, audit_caller="core:test",
                pin_host="github.com",
            )
        assert captured["kwargs"]["env"]["GH_HOST"] == "github.com"

    def test_audits_an_oserror_spawn_failure_then_reraises(self):
        """A cached binary gone bad (chmod'd, replaced with a non-executable)
        must land in the audit trail, not escape as an unaudited failure."""
        with mock.patch.object(
            runner.subprocess, "run", side_effect=PermissionError("denied")
        ), mock.patch.object(runner, "_audit_run") as audit:
            with pytest.raises(PermissionError):
                runner.run_gh(
                    ["/usr/bin/gh", "api", "user"], timeout=5, audit_caller="core:test"
                )
        assert audit.call_args_list[-1] == mock.call(
            "core:test", "gh api user", "failure", error="PermissionError"
        )

    def test_audits_invoked_before_the_spawn_then_ok(self):
        calls: list[tuple] = []

        def fake_run(argv, **kwargs):
            calls.append(("spawn",))
            return _proc()

        def fake_audit(caller, target, outcome, **kwargs):
            calls.append(("audit", outcome, kwargs.get("critical", False)))

        with mock.patch.object(runner.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(runner, "_audit_run", side_effect=fake_audit):
            runner.run_gh(["/usr/bin/gh", "api", "user"], timeout=5, audit_caller="core:test")
        # The invoked event is critical and lands BEFORE the child runs.
        assert calls == [("audit", "invoked", True), ("spawn",), ("audit", "ok", False)]

    def test_audits_non_zero_exit(self):
        with mock.patch.object(runner.subprocess, "run", return_value=_proc(returncode=1)), \
                mock.patch.object(runner, "_audit_run") as audit:
            proc = runner.run_gh(
                ["/usr/bin/gh", "api", "user"], timeout=5, audit_caller="core:test"
            )
        assert proc.returncode == 1
        assert audit.call_args_list[-1] == mock.call(
            "core:test", "gh api user", "failure", error="exit 1"
        )

    def test_audits_timeout_then_reraises(self):
        with mock.patch.object(
            runner.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 5)
        ), mock.patch.object(runner, "_audit_run") as audit:
            with pytest.raises(subprocess.TimeoutExpired):
                runner.run_gh(
                    ["/usr/bin/gh", "api", "user"], timeout=5, audit_caller="core:test"
                )
        assert audit.call_args_list[-1] == mock.call(
            "core:test", "gh api user", "failure", error="timeout after 5s"
        )

    def test_audit_event_carries_the_caller_namespace(self, monkeypatch):
        """Issue Radar's historical SEL operation identity survives the shared
        emission point."""
        events: list[dict] = []

        class _FakeSel:
            def log_api_access(self, **kwargs):
                events.append(kwargs)

        monkeypatch.setattr("kiro_crew.sel.sel", lambda: _FakeSel())
        runner._audit_run("core:issue-radar", "gh api repos/o/r", "ok")

        assert events == [
            {
                "caller": "core:issue-radar",
                "operation": "issue_radar.gh_run",
                "outcome": "ok",
                "source": "builtin-app",
                "resources": "gh api repos/o/r",
                "error": "",
                "critical": False,
            }
        ]

    def test_unavailable_audit_refuses_the_spawn(self, monkeypatch):
        """Audit-or-deny: with SEL storage unusable, gh must NOT run unaudited."""
        monkeypatch.setattr(
            "kiro_crew.sel.sel", mock.Mock(side_effect=RuntimeError("sel down"))
        )
        with mock.patch.object(runner.subprocess, "run", return_value=_proc()) as spawn:
            with pytest.raises(runner.SetupError, match="refusing to run gh unaudited"):
                runner.run_gh(
                    ["/usr/bin/gh", "api", "user"], timeout=5, audit_caller="core:test"
                )
            spawn.assert_not_called()

    def test_outcome_audit_failure_never_breaks_the_call(self, monkeypatch):
        """Once the invoked record landed, a failed OUTCOME write is logged,
        not turned into a feature failure — the spawn already happened."""

        class _FlakySel:
            def __init__(self) -> None:
                self.calls = 0

            def log_api_access(self, **kwargs):
                self.calls += 1
                if kwargs.get("outcome") != "invoked":
                    raise RuntimeError("sel went away mid-call")

        flaky = _FlakySel()
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: flaky)
        with mock.patch.object(runner.subprocess, "run", return_value=_proc()):
            proc = runner.run_gh(
                ["/usr/bin/gh", "api", "user"], timeout=5, audit_caller="core:test"
            )
        assert proc.returncode == 0
        assert flaky.calls == 2


# ── re-export seams ──────────────────────────────────────────────────────────


class TestReExports:
    def test_source_providers_validation_is_the_shared_function(self):
        from kiro_crew.dashboard.handlers import source_providers

        assert (
            source_providers._validate_provider_executable
            is runner.validate_provider_executable
        )
        assert (
            source_providers.provider_executable_candidates
            is runner.provider_executable_candidates
        )
        assert (
            source_providers._PROVIDER_EXECUTABLE_CANDIDATES
            is runner.PROVIDER_EXECUTABLE_CANDIDATES
        )

    def test_github_client_url_parser_is_the_shared_function(self):
        from kiro_crew.apps.builtins.issue_radar.backend import github_client

        assert github_client.parse_github_repo_url is runner.parse_github_repo_url

    def test_repo_url_error_is_one_class_across_layers(self):
        from kiro_crew.apps.builtins.issue_radar.backend import errors, github_client

        assert errors.RepoUrlError is runner.RepoUrlError
        assert github_client.RepoUrlError is runner.RepoUrlError
        with pytest.raises(errors.RepoUrlError):
            runner.parse_github_repo_url("https://evil.example/o/r")

    def test_source_providers_gh_auth_keys_derive_from_the_canonical_union(self):
        """D3 lock-in: the sidebar's gh key set can no longer drift from the
        app-side passthrough — it derives from the runner's canonical list,
        minus the enterprise tokens its github.com-pinned child can never use."""
        from kiro_crew.dashboard.handlers import source_providers

        assert source_providers._PROVIDER_AUTH_ENV_KEYS["gh"] == frozenset(
            runner.GH_ENV_PASSTHROUGH
        ) - {"GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}
