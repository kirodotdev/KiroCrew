"""Reused signed checkouts refuse tracked caches, not generated dependencies."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_app_signed_round3 import sign
from test_app_signed_round3 import signed_home as _signed_home
from test_app_signed_round3 import write_app

from kiro_crew.apps import admission, signed_checkout

signed_home = _signed_home


@pytest.fixture
def git_files(monkeypatch: pytest.MonkeyPatch) -> Mock:
    monkeypatch.setattr(signed_checkout, "wrap_argv", lambda argv, **kwargs: (argv, None))
    monkeypatch.setattr(signed_checkout, "cgroup_scope_argv", lambda argv: argv)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=b"app.json\x00server.py\x00"))
    monkeypatch.setattr(signed_checkout, "run_limited", run)
    return run


@pytest.mark.parametrize(
    "entry",
    [
        "__pycache__/helper.pyc",
        ".venv/site.py",
        "node_modules/pkg/index.js",
        "backend/__pycache__/helper.pyc",
    ],
)
def test_reused_signed_checkout_refuses_tracked_excluded_files(
    tmp_path: Path, signed_home: Path, git_files: Mock, entry: str
) -> None:
    root = tmp_path / "source"
    write_app(root)
    manifest = sign(root)
    target = root / entry
    target.parent.mkdir(parents=True)
    target.write_text("publisher code", encoding="utf-8")
    git_files.return_value.stdout += entry.encode("utf-8") + b"\x00"
    reason = admission.app_admission_denied(
        "probe", manifest, "install_from_registry", app_root=root, fresh_checkout=False
    )
    assert reason is not None and entry in reason
    git_files.assert_called_once()


def test_reused_checkout_preserves_untracked_generated_dependencies(
    tmp_path: Path, signed_home: Path, git_files: Mock
) -> None:
    root = tmp_path / "source"
    write_app(root)
    manifest = sign(root)
    for name in (".venv", "node_modules", "__pycache__"):
        path = root / name / "generated"
        path.parent.mkdir(parents=True)
        path.write_text("generated", encoding="utf-8")
    assert (
        admission.app_admission_denied(
            "probe", manifest, "install_from_registry", app_root=root, fresh_checkout=False
        )
        is None
    )
    assert all(
        (root / name / "generated").exists() for name in (".venv", "node_modules", "__pycache__")
    )


def test_git_read_is_relative_credential_free_and_execution_neutralized(
    tmp_path: Path, git_files: Mock
) -> None:
    root = tmp_path / "repository" / "apps" / "probe"
    git_files.return_value.stdout = b"app.json\x00ui/data/labels.json\x00"
    assert signed_checkout.tracked_unpackaged_entry(root) is None
    argv = git_files.call_args.args[0]
    assert ["-c", "core.fsmonitor=false"] == argv[3:5]
    assert argv[1:3] == ["-c", f"core.hooksPath={os.devnull}"]
    assert argv[-5:] == ["ls-files", "--cached", "-z", "--", "."]
    assert git_files.call_args.kwargs["cwd"] == str(root)
    assert git_files.call_args.kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
    assert git_files.call_args.kwargs["timeout"] == 30


@pytest.mark.parametrize("failure", ["returncode", "exception", "encoding"])
def test_unreadable_tracked_list_refuses_cleanly(
    tmp_path: Path, git_files: Mock, failure: str
) -> None:
    if failure == "returncode":
        git_files.return_value.returncode = 1
    elif failure == "exception":
        git_files.side_effect = OSError("git unavailable")
    else:
        git_files.return_value.stdout = b"\xff\x00"
    assert (
        signed_checkout.tracked_unpackaged_entry(tmp_path)
        == "publisher tracked files could not be inspected"
    )


def test_no_signature_policy_does_not_query_git(
    tmp_path: Path, signed_home: Path, git_files: Mock
) -> None:
    root = tmp_path / "source"
    manifest = write_app(root)
    (signed_home / "app_admission.json").write_text(json.dumps({}), encoding="utf-8")
    assert (
        admission.app_admission_denied(
            "probe", manifest, "install_from_registry", app_root=root, fresh_checkout=False
        )
        is None
    )
    git_files.assert_not_called()
