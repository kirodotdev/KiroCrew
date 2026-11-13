"""``cli.sh`` says before its next steps when agents need an AppArmor profile first.

A kernel that restricts unprivileged user namespaces through AppArmor
(``kernel.apparmor_restrict_unprivileged_userns=1``, stock Ubuntu 23.10 and newer)
refuses the agent sandbox until ``kirocrew service install`` writes the
``kirocrew-userns`` profile, which needs sudo. The installer never asks for root, so it
says so instead of leading straight to a chat that cannot run.

A test cannot set the kernel's sysctl, so these lift ``_userns_profile_notice``
verbatim out of the real script and run it on files of their own.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kiro_crew.service import apparmor
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

CLI_SH = Path(__file__).resolve().parents[1] / "cli.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX shell installer; not exercised on native Windows"
)


def _function(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    end = text.index("\n}", start) + len("\n}")
    return text[start:end]


def _notice(tmp_path: Path, sysctl: str | None, *, profile: bool) -> str:
    """Run the lifted function under ``set -eu`` and return what it printed."""
    sysctl_file = tmp_path / "apparmor_restrict_unprivileged_userns"
    if sysctl is not None:
        sysctl_file.write_text(sysctl, encoding="utf-8")
    profile_file = tmp_path / apparmor.PROFILE_NAME
    if profile:
        profile_file.write_text(f"profile {apparmor.PROFILE_NAME} {{}}\n", encoding="utf-8")
    script = _function(CLI_SH.read_text(encoding="utf-8"), "_userns_profile_notice")
    result = subprocess.run(
        ["sh", "-eu", "-c", script + '\n_userns_profile_notice "$1" "$2"\n', "sh"]
        + [str(sysctl_file), str(profile_file)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    return result.stderr


def test_a_restricted_host_without_the_profile_is_told_the_command_and_that_it_needs_sudo(
    tmp_path: Path,
) -> None:
    notice = _notice(tmp_path, "1\n", profile=False)

    assert "agents cannot run until Kiro Crew's AppArmor profile is installed" in notice
    assert "kirocrew service install    # needs sudo" in notice


@pytest.mark.parametrize(
    ("sysctl", "profile"),
    [("1\n", True), ("0\n", False), (None, False)],
    ids=["profile-installed", "not-restricted", "no-sysctl"],
)
def test_no_notice_where_agents_can_run_or_the_kernel_has_no_such_restriction(
    tmp_path: Path, sysctl: str | None, profile: bool
) -> None:
    assert _notice(tmp_path, sysctl, profile=profile) == ""


def test_the_installer_reads_the_real_sysctl_and_profile_before_its_next_steps() -> None:
    """The profile path is the one ``kirocrew doctor`` checks, so a rename moves both."""
    text = CLI_SH.read_text(encoding="utf-8")
    call = (
        "\n_userns_profile_notice /proc/sys/kernel/apparmor_restrict_unprivileged_userns "
        f"{apparmor.PROFILE_PATH.as_posix()}\n"
    )

    assert text.count(call) == 1
    assert text.index(call) < text.index('echo "Next steps:"')
