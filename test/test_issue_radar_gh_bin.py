"""Issue Radar's ``gh`` resolution (``github_client._gh_bin``).

Issue Radar shells out to the user's own ``gh`` session, so the binary it picks
is a trust decision. It deliberately shares both the search order and the
validation with every other gh surface via the shared hardened runner
(``kiro_crew.github_runner.resolve_gh``): the well-known install dirs first,
then the ambient ``PATH``, accepting the user's own (Homebrew, asdf,
``~/.local/bin``) install and refusing only provenance the user did not choose.
These tests pin that wiring — a regression here either locks out every stock
``brew install gh`` (the bug this replaced) or silently accepts an
agent-plantable shim.
"""
import os
import sys
import tempfile

import pytest

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only gh resolution"),
    pytest.mark.skipif(
        sys.platform != "win32"
        and os.stat(tempfile.gettempdir()).st_uid not in (0, os.geteuid()),
        reason="temp dir owned by another user; provider ownership checks reject it",
    ),
]

from kiro_crew import github_runner  # noqa: E402
from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_gh_cache(monkeypatch):
    github_runner.reset_cache()
    monkeypatch.delenv("KIROCREW_ISSUE_RADAR_GH", raising=False)
    monkeypatch.delenv("KIROCREW_GH_BIN", raising=False)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    yield
    github_runner.reset_cache()


def _fake_gh(directory) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "gh"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return str(binary)


def test_gh_bin_accepts_a_user_owned_install_from_path(monkeypatch, tmp_path) -> None:
    """The Homebrew/asdf case: user-owned, only on PATH, no root-owned copy."""
    binary = _fake_gh(tmp_path / "user-bin")
    monkeypatch.setattr(
        github_runner,
        "PROVIDER_EXECUTABLE_CANDIDATES",
        {"gh": ("/nonexistent-kirocrew/gh",), "glab": ("/nonexistent-kirocrew/glab",)},
    )
    monkeypatch.setenv("PATH", str(tmp_path / "user-bin"))

    assert gh._gh_bin() == binary


def test_gh_bin_refuses_a_shim_inside_the_agent_writable_tree(monkeypatch, tmp_path) -> None:
    project = tmp_path / "project"
    binary = _fake_gh(project / "bin")
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: (project.resolve(),))
    monkeypatch.setattr(
        github_runner,
        "PROVIDER_EXECUTABLE_CANDIDATES",
        {"gh": (binary,), "glab": ("/nonexistent-kirocrew/glab",)},
    )
    monkeypatch.setenv("PATH", str(project / "bin"))

    with pytest.raises(gh.GhSetupError) as excinfo:
        gh._gh_bin()

    assert excinfo.value.reason == "not_installed"
    assert "agent-writable tree" in str(excinfo.value)


def test_gh_bin_missing_gives_install_guidance(monkeypatch) -> None:
    monkeypatch.setattr(
        github_runner,
        "PROVIDER_EXECUTABLE_CANDIDATES",
        {"gh": ("/nonexistent-kirocrew/gh",), "glab": ("/nonexistent-kirocrew/glab",)},
    )
    monkeypatch.setenv("PATH", "")

    with pytest.raises(gh.GhSetupError) as excinfo:
        gh._gh_bin()

    message = str(excinfo.value)
    assert excinfo.value.reason == "not_installed"
    assert "brew install gh" in message
    assert "gh auth login" in message
    assert "sudo" not in message


def test_gh_bin_strict_mode_still_requires_a_root_owned_copy(monkeypatch, tmp_path) -> None:
    binary = _fake_gh(tmp_path / "user-bin")
    monkeypatch.setenv("KIROCREW_PROVIDER_BIN_STRICT", "1")
    monkeypatch.setattr(
        github_runner,
        "PROVIDER_EXECUTABLE_CANDIDATES",
        {"gh": (binary,), "glab": ("/nonexistent-kirocrew/glab",)},
    )

    with pytest.raises(gh.GhSetupError, match="not root-owned"):
        gh._gh_bin()


def test_gh_bin_override_failure_is_a_setup_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KIROCREW_ISSUE_RADAR_GH", str(tmp_path / "missing-gh"))

    with pytest.raises(gh.GhSetupError) as excinfo:
        gh._gh_bin()

    assert excinfo.value.reason == "not_installed"
    assert "KIROCREW_ISSUE_RADAR_GH" in str(excinfo.value)


def test_gh_bin_caches_the_resolved_path(monkeypatch, tmp_path) -> None:
    binary = _fake_gh(tmp_path / "user-bin")
    monkeypatch.setattr(
        github_runner,
        "PROVIDER_EXECUTABLE_CANDIDATES",
        {"gh": (binary,), "glab": ("/nonexistent-kirocrew/glab",)},
    )

    assert gh._gh_bin() == binary
    # Second call must not re-validate: point the candidates at nothing and
    # confirm the cached answer is returned anyway.
    monkeypatch.setattr(
        github_runner,
        "PROVIDER_EXECUTABLE_CANDIDATES",
        {"gh": ("/nonexistent-kirocrew/gh",), "glab": ("/nonexistent-kirocrew/glab",)},
    )
    assert gh._gh_bin() == binary
