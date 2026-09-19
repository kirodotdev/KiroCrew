"""The enforcing-backend predicate shares the spawn chokepoint's host probes."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kiro_crew import sandbox


@pytest.mark.parametrize(
    ("platform", "userns", "seatbelt_present", "seatbelt_works", "expected"),
    [
        ("linux", True, False, False, True),
        ("linux", False, True, True, False),
        ("darwin", False, True, True, True),
        ("darwin", False, False, True, False),
        ("darwin", False, True, False, False),
        ("win32", True, True, True, False),
        ("freebsd", True, True, True, False),
    ],
)
def test_enforcing_backend_platforms(
    monkeypatch, platform, userns, seatbelt_present, seatbelt_works, expected
):
    monkeypatch.setattr(sandbox, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(sandbox, "_backend", None)
    monkeypatch.setattr(sandbox, "_last_unshare_failure", None)
    monkeypatch.setattr(sandbox, "_probe_unshare_once", lambda: (userns, False, "probe denied", ""))
    monkeypatch.setattr(sandbox, "_macos_sandbox_state", lambda: False)
    # A PATH-provided executable cannot prove a usable trusted Seatbelt backend.
    which = Mock(return_value="/untrusted/sandbox-exec")
    monkeypatch.setattr(sandbox.shutil, "which", which)
    real_exists = sandbox.os.path.exists
    monkeypatch.setattr(
        sandbox.os.path,
        "exists",
        lambda path: (
            seatbelt_present
            if path in {"/usr/bin/sandbox-exec", "/usr/bin/true"}
            else real_exists(path)
        ),
    )
    run = Mock(return_value=SimpleNamespace(returncode=0 if seatbelt_works else 1, stderr=b""))
    monkeypatch.setattr(sandbox.subprocess, "run", run)

    assert sandbox.enforcing_backend_available() is expected
    assert sandbox.enforcing_backend_available() is expected  # same cached selection
    which.assert_not_called()
    assert run.call_count == int(platform == "darwin" and seatbelt_present)
    if run.called:
        assert run.call_args.args[0][0] == "/usr/bin/sandbox-exec"
        assert run.call_args.args[0][-1] == "/usr/bin/true"


@pytest.mark.parametrize("backend", ["none", "namespace", "sandbox-exec"])
def test_enforcing_backend_reuses_selection(monkeypatch, backend):
    detect = Mock(return_value=backend)
    monkeypatch.setattr(sandbox, "detect_backend", detect)
    monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: True)
    assert sandbox.enforcing_backend_available() is (backend != "none")
    detect.assert_called_once_with()
