"""The sign-in hint a failed provider call carries names the right server.

A bare ``glab auth login`` signs in to gitlab.com, so for a self-managed GitLab
it sends the user to the wrong place. The hint names the host there, and GitLab's
own ``401 Unauthorized`` wording (which the old markers missed entirely) counts
as an auth failure.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.source_providers import runner

HOST = "gitlab.example.internal"


def test_self_managed_glab_failure_carries_the_host_scoped_login_command() -> None:
    exc = runner._provider_failure("glab", b"glab: 401 Unauthorized (HTTP 401)", HOST)
    assert exc.login_command == f"glab auth login --hostname {HOST}"


@pytest.mark.parametrize(
    ("executable", "stderr", "host"),
    [
        ("glab", b"glab: 401 Unauthorized (HTTP 401)", "gitlab.com"),
        ("glab", b"glab: 404 Not Found", HOST),
        ("gh", b"gh: not logged in", "github.com"),
    ],
)
def test_no_login_command_without_a_self_managed_sign_in_failure(executable, stderr, host) -> None:
    assert runner._provider_failure(executable, stderr, host).login_command == ""


def test_self_managed_glab_hint_names_the_host() -> None:
    msg = runner._provider_failure_message("glab", b"glab: 401 Unauthorized (HTTP 401)", HOST)
    assert msg.endswith(f"Run `glab auth login --hostname {HOST}`, then retry.")
    assert "Run `glab auth login`," not in msg


@pytest.mark.parametrize("host", ["gitlab.com", "GitLab.com", ""])
def test_gitlab_com_keeps_the_generic_hint(host: str) -> None:
    msg = runner._provider_failure_message("glab", b"glab: 401 Unauthorized (HTTP 401)", host)
    assert msg.endswith("Run `glab auth login`, then retry.")
    assert "--hostname" not in msg


def test_gh_is_unchanged() -> None:
    msg = runner._provider_failure_message("gh", b"gh: not logged in", "github.com")
    assert msg.endswith("Run `gh auth login`, then retry.")


@pytest.mark.parametrize(
    "stderr", [b"HTTP 401: Bad credentials", b'{"message":"401 Unauthorized"}']
)
def test_gh_does_not_gain_the_glab_401_markers(stderr: bytes) -> None:
    msg = runner._provider_failure_message("gh", stderr, "github.com")
    assert "auth login" not in msg


@pytest.mark.parametrize(
    "stderr",
    [
        b'{"message":"401 Unauthorized"}',  # what glab prints for an expired sign-in
        b"HTTP 401: bad token",
        b"authentication failed",
    ],
)
def test_401_wording_counts_as_an_auth_failure(stderr: bytes) -> None:
    msg = runner._provider_failure_message("glab", stderr, HOST)
    assert f"`glab auth login --hostname {HOST}`" in msg


def test_other_failures_get_no_hint() -> None:
    msg = runner._provider_failure_message("glab", b"glab: 404 Not Found", HOST)
    assert "sign-in" not in msg and "auth login" not in msg


@pytest.mark.asyncio
async def test_run_json_hands_the_host_to_the_parser(monkeypatch) -> None:
    seen = {}

    async def fake_run_provider(*argv, max_output_bytes, host, parse):
        seen["host"] = host
        with pytest.raises(runner.SourceProviderError) as err:
            parse(1, b"", b"glab: 401 Unauthorized (HTTP 401)")
        seen["message"] = str(err.value)

    monkeypatch.setattr(runner, "_run_provider", fake_run_provider)
    await runner._run_json("glab", "api", "projects/1", host=HOST)
    assert seen["host"] == HOST
    assert f"--hostname {HOST}" in seen["message"]
