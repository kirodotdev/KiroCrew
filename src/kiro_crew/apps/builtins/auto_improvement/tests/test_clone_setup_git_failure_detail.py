"""A failed git step reports git's diagnosis, not its access-rights advisory.

git ends every transport or auth fatal with the same two advisory lines. A
summary that keeps only stderr's LAST line reports an SSH key failure as
``git clone failed: and the repository exists.``, which sends the operator to
check a repository that is fine. The clone, list-branches, checkout and fetch
failures in ``clone_setup`` all summarise through ``_git_stderr_summary``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
from kiro_crew.apps.builtins.auto_improvement.backend.clone_setup import CloneSpec

#: git's SSH publickey fatal, verbatim from the issue report.
_SSH_AUTH_FAILURE = (
    "git@github.com: Permission denied (publickey).\n"
    "fatal: Could not read from remote repository.\n"
    "\n"
    "Please make sure you have the correct access rights\n"
    "and the repository exists.\n"
)


def test_the_advisory_block_is_dropped_and_the_diagnosis_kept() -> None:
    detail = clone_setup._git_stderr_summary(_SSH_AUTH_FAILURE)

    assert detail == (
        "git@github.com: Permission denied (publickey). "
        "fatal: Could not read from remote repository."
    )
    assert "repository exists" not in detail


def test_a_one_line_failure_is_reported_unchanged() -> None:
    assert clone_setup._git_stderr_summary("fatal: bad object\n") == "fatal: bad object"


def test_only_the_last_two_diagnostic_lines_are_kept() -> None:
    stderr = "Cloning into 'r'...\nremote: Repository not found.\nfatal: repository not found\n"

    assert clone_setup._git_stderr_summary(stderr) == (
        "remote: Repository not found. fatal: repository not found"
    )


def test_an_advisory_only_stream_still_reports_a_line() -> None:
    advisory = "Please make sure you have the correct access rights\nand the repository exists.\n"

    assert clone_setup._git_stderr_summary(advisory) == "and the repository exists."


def test_empty_stderr_reports_an_empty_detail() -> None:
    assert clone_setup._git_stderr_summary(None) == ""
    assert clone_setup._git_stderr_summary("\n\n") == ""


def test_a_failed_clone_names_the_auth_failure(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    spec = CloneSpec("o/r", "git@github.com:o/r.git", "o--r")
    failed = subprocess.CompletedProcess(["git", "clone"], 128, "", _SSH_AUTH_FAILURE)
    with (
        mock.patch.object(clone_setup, "validate_target_url", return_value=(spec, "")),
        mock.patch.object(clone_setup.subprocess, "run", return_value=failed),
    ):
        result, err = clone_setup.setup_safe_clone("https://github.com/o/r", root)

    assert result == {}
    assert err.startswith("git clone failed: ")
    assert "Permission denied (publickey)" in err
    assert "Could not read from remote repository" in err
    assert not err.endswith("and the repository exists.")


def test_a_credential_in_the_detail_is_redacted_before_the_bound(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    spec = CloneSpec("o/r", "https://github.com/o/r.git", "o--r")
    token = "s3cr3t-remote-token"  # a fake secret, only asserted absent
    stderr = f"fatal: unable to access 'https://ci-bot:{token}@github.com/o/r.git/'\n" + (
        "Please make sure you have the correct access rights\nand the repository exists.\n"
    )
    failed = subprocess.CompletedProcess(["git", "clone"], 128, "", stderr)
    with (
        mock.patch.object(clone_setup, "validate_target_url", return_value=(spec, "")),
        mock.patch.object(clone_setup.subprocess, "run", return_value=failed),
    ):
        _, err = clone_setup.setup_safe_clone("https://github.com/o/r", root)

    assert token not in err
    assert "unable to access" in err


#: A TLS failure as ``git clone`` writes it: the progress line first, which names
#: the absolute scratch path, then the single fatal that is the whole cause.
_TLS_FAILURE = (
    "Cloning into '/home/alice/.autoimprove-scratch/kirodotdev--KiroCrew'...\n"
    "fatal: unable to access 'https://github.com/kirodotdev/KiroCrew.git/': "
    "SSL certificate problem: self-signed certificate in certificate chain\n"
)


def test_the_clone_progress_line_is_dropped() -> None:
    assert clone_setup._git_stderr_summary(_TLS_FAILURE) == (
        "fatal: unable to access 'https://github.com/kirodotdev/KiroCrew.git/': "
        "SSL certificate problem: self-signed certificate in certificate chain"
    )
    bare = "Cloning into bare repository 'r.git'...\nfatal: repository not found\n"
    assert clone_setup._git_stderr_summary(bare) == "fatal: repository not found"


def test_a_single_fatal_clone_failure_keeps_its_whole_cause(tmp_path: Path) -> None:
    # The progress line would have pushed this cause past the 200-char bound.
    root = tmp_path / "root"
    root.mkdir()
    spec = CloneSpec("o/r", "https://github.com/o/r.git", "o--r")
    failed = subprocess.CompletedProcess(["git", "clone"], 128, "", _TLS_FAILURE)
    with (
        mock.patch.object(clone_setup, "validate_target_url", return_value=(spec, "")),
        mock.patch.object(clone_setup.subprocess, "run", return_value=failed),
    ):
        _, err = clone_setup.setup_safe_clone("https://github.com/o/r", root)

    assert err.endswith("self-signed certificate in certificate chain")
    assert "Cloning into" not in err
    assert "autoimprove-scratch" not in err
