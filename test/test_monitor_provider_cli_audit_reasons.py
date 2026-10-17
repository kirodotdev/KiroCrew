"""Monitor provider CLI audit rows carry a fixed, credential-free reason.

Every refusal or failure of a source-monitor provider CLI (``glab``, ``az``,
``gh``) leaves an audit row whose ``error`` field names WHY with one word from
a fixed vocabulary, never a target, argv, credential, or process output. The
GitHub resolver refusal leaves the same evidence as the GitLab and Azure
resolver paths.
"""

from __future__ import annotations

import json
import subprocess
import sys
from urllib.error import URLError

import pytest

from kiro_crew import github_runner
from kiro_crew.monitoring import bitbucket_pull_request as bitbucket_module
from kiro_crew.monitoring import provider_cli as provider_cli_module
from kiro_crew.monitoring.bitbucket_pull_request import BitbucketPullRequestProvider
from kiro_crew.monitoring.github_pull_request import GitHubPullRequestProvider
from kiro_crew.monitoring.gitlab_merge_request import GitLabMergeRequestProvider
from kiro_crew.monitoring.provider_cli import run_provider_cli
from kiro_crew.sel import SecurityEventLog

_GITLAB_URL = "https://gitlab.example.com/acme/widgets/-/merge_requests/8"
_GITHUB_URL = "https://github.com/acme/widgets/pull/1"


@pytest.fixture
def audit(monkeypatch, tmp_path):
    monkeypatch.setattr(SecurityEventLog, "_instance", None)
    log = SecurityEventLog(base_dir=tmp_path / "sel", sync=True)
    monkeypatch.setattr(provider_cli_module, "sel", lambda: log)
    return log


def _rows(audit, operation):
    return [
        (event["outcome"], event["error"])
        for event in reversed(audit.recent())
        if event["operation"] == operation
    ]


def _no_override(monkeypatch, executable):
    monkeypatch.delenv(github_runner.PROVIDER_CLI_OVERRIDE_ENV[executable], raising=False)


def _stub_candidates(monkeypatch, message):
    monkeypatch.setattr(
        provider_cli_module,
        "provider_executable_candidates",
        lambda _executable: ("/nonexistent/glab",),
    )

    def refuse(_candidate, **_kwargs):
        raise ValueError(message)

    monkeypatch.setattr(provider_cli_module, "validate_provider_executable", refuse)


def test_missing_glab_records_executable_not_found(monkeypatch, audit):
    _no_override(monkeypatch, "glab")
    _stub_candidates(monkeypatch, "path does not exist")

    with pytest.raises(provider_cli_module.SetupError):
        run_provider_cli("glab", ["api", "projects/acme"], timeout=5)

    assert _rows(audit, "monitor.glab_probe") == [("denied", "executable_not_found")]


def test_untrusted_glab_records_executable_untrusted(monkeypatch, audit):
    _no_override(monkeypatch, "glab")
    _stub_candidates(monkeypatch, "owned by another user")

    with pytest.raises(provider_cli_module.SetupError):
        run_provider_cli("glab", ["api", "projects/acme"], timeout=5)

    assert _rows(audit, "monitor.glab_probe") == [("denied", "executable_untrusted")]


def test_unsupported_provider_cli_records_unsupported_provider(audit):
    with pytest.raises(provider_cli_module.SetupError):
        run_provider_cli("gh", ["api", "user"], timeout=5)

    assert _rows(audit, "monitor.gh_probe") == [("denied", "unsupported_provider")]


def test_owner_credential_refusal_records_authorization_refused(audit):
    provider = GitLabMergeRequestProvider(gitlab_hosts=["gitlab.example.com"])

    provider.probe([_GITLAB_URL], use_owner_credentials=False)

    assert _rows(audit, "monitor.glab_probe") == [("denied", "authorization_refused")]


def test_revoked_gitlab_host_records_host_not_allowlisted(audit):
    provider = GitLabMergeRequestProvider(
        gitlab_hosts=[],
        fetch=lambda *_args: pytest.fail("revoked host reached the provider"),
    )

    provider.probe([_GITLAB_URL])

    assert _rows(audit, "monitor.glab_probe") == [("denied", "host_not_allowlisted")]


@pytest.fixture
def stand_in(monkeypatch, tmp_path):
    """Run the trusted Python interpreter in place of glab, outside the sandbox."""
    monkeypatch.setattr(provider_cli_module, "resolve_provider_cli", lambda _e: sys.executable)
    monkeypatch.setattr(
        provider_cli_module,
        "sandboxed_spawn_argv",
        lambda argv, **kwargs: (argv, kwargs["env"], None),
    )
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("import sys; sys.exit(3)", ("failed", "exit_nonzero")),
        ("pass", ("completed", "")),
    ],
)
def test_process_exit_is_recorded_with_its_reason(audit, stand_in, code, expected):
    run_provider_cli("glab", ["-c", code], timeout=30)

    assert _rows(audit, "monitor.glab_probe") == [("invoked", ""), expected]


def test_process_timeout_records_timeout(audit, stand_in):
    with pytest.raises(subprocess.TimeoutExpired):
        run_provider_cli("glab", ["-c", "import time; time.sleep(30)"], timeout=0.5)

    assert _rows(audit, "monitor.glab_probe") == [("invoked", ""), ("failed", "timeout")]


def test_oversized_output_records_output_too_large(audit, stand_in):
    code = "import sys; sys.stdout.buffer.write(b'x' * (1024 * 1024 + 1))"
    with pytest.raises(ValueError, match="output exceeds"):
        run_provider_cli("glab", ["-c", code], timeout=30)

    assert _rows(audit, "monitor.glab_probe") == [
        ("invoked", ""),
        ("failed", "output_too_large"),
    ]


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (github_runner.SetupError("no usable `gh` CLI found"), "executable_untrusted"),
        (FileNotFoundError("gh"), "executable_not_found"),
    ],
)
def test_github_resolver_refusal_writes_one_denied_row(audit, error, reason):
    def resolver():
        raise error

    provider = GitHubPullRequestProvider(
        resolver=resolver,
        runner=lambda *_a, **_k: pytest.fail("gh ran after its resolver refused"),
    )

    results = provider.probe([_GITHUB_URL, "https://github.com/acme/widgets/pull/2"])

    assert {r.observation.reason_code for r in results.values()} == {"provider_setup"}
    assert _rows(audit, "monitor.gh_probe") == [("denied", reason)]


def test_resolve_gh_names_why_it_refused(monkeypatch):
    for name in (github_runner.GH_BIN_ENV, github_runner.GH_PREVALIDATED_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(github_runner, "provider_executable_candidates", lambda _e: ())

    with pytest.raises(github_runner.SetupError) as missing:
        github_runner.resolve_gh(cache=False)

    monkeypatch.setenv(github_runner.GH_BIN_ENV, "relative/gh")
    with pytest.raises(github_runner.SetupError) as untrusted:
        github_runner.resolve_gh(cache=False)

    assert missing.value.reason == "executable_not_found"
    assert untrusted.value.reason == "executable_untrusted"


def test_reason_rows_never_carry_targets(audit):
    GitLabMergeRequestProvider(gitlab_hosts=[]).probe([_GITLAB_URL])

    dumped = json.dumps(audit.recent())
    assert "gitlab.example.com" not in dumped
    assert "acme/widgets" not in dumped


class _Response:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self._body


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (TimeoutError("read timed out"), "timeout"),
        (URLError(TimeoutError("connect timed out")), "timeout"),
        (URLError("connection refused"), "provider_error"),
        (b"x" * (bitbucket_module._MAX_RESPONSE_BYTES + 1), "output_too_large"),
    ],
    ids=["read-timeout", "connect-timeout", "refused", "oversized"],
)
def test_bitbucket_request_failure_records_its_reason(monkeypatch, tmp_path, outcome, reason):
    monkeypatch.setattr(SecurityEventLog, "_instance", None)
    log = SecurityEventLog(base_dir=tmp_path / "sel", sync=True)
    monkeypatch.setattr(bitbucket_module, "sel", lambda: log)

    class Opener:
        def open(self, _request, *, timeout):
            if isinstance(outcome, BaseException):
                raise outcome
            return _Response(outcome)

    monkeypatch.setattr(bitbucket_module, "build_opener", lambda _redirect: Opener())
    target = bitbucket_module.parse_bitbucket_pull_request_target(
        "https://bitbucket.org/acme/widgets/pull-requests/10"
    )

    with pytest.raises((OSError, ValueError)):
        BitbucketPullRequestProvider._fetch_https(target, "pull_request", {})

    assert _rows(log, "monitor.bitbucket_probe") == [("invoked", ""), ("failed", reason)]
