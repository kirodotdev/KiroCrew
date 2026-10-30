"""The decision-ledger-history self-test reads its own diagnostics everywhere.

``scripts/check_decisions_history.py --test`` asserts that a rejecting probe's
output names the ledger child it rejected. The head-side validators print paths
as the platform renders them — backslash separators on native Windows — while
``DECISIONS_DIR`` is slash-separated, so an unfixed match reports a correct
rejection as ``missing rejection and path`` and fails the self-test that
``kirocrew-prepare-pr`` runs on every contribution.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 10x the worst call-phase duration observed on Windows in run 39959306936
# shard 1: the test completed the call phase in 13.7 s on a loaded GitHub
# runner.  The test-level @pytest.mark.timeout is set to twice this value so
# pytest-timeout ends the test by raising a Timeout exception before it can
# kill the xdist worker and silently drop any pending tests.
_SELFTEST_HANG_GUARD_SECS = 140

#: A rejection line's shape, with the parts after the path the way
#: ``entries_in_tree`` words them for a directory child.
_REJECTION = (
    " is a symlink. The ledger directory and every directory above it must be a "
    "real directory; a link makes the gate compare whatever the link points at."
)


def _load_gate():
    """The gate script, loaded from ``scripts/``.

    ``scripts/`` is not an importable package, so the module is loaded by file
    path — the same way ``test/test_docker_wheel_layer_contract.py`` loads its
    script. It is registered in ``sys.modules`` so tests that each need the
    gate reuse one module instead of re-executing it.
    """
    name = "check_decisions_history"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "check_decisions_history.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _windows_render(absolute: str) -> str:
    """A path the way ``str(WindowsPath)`` renders it: backslash separators.

    ``check_decisions_history.py`` prints ``Path`` objects with f-strings, so a
    probe on native Windows emits exactly this shape for the same repository.
    """
    return absolute.replace("/", "\\")


def test_native_separator_diagnostic_names_the_child() -> None:
    """A Windows-rendered rejection still satisfies the self-test's match."""
    gate = _load_gate()
    report = (
        "::error::decision-ledger-history: "
        + _windows_render("C:/repo/docs/decisions/nested")
        + _REJECTION
    )
    assert gate._names_ledger_child(report, "nested")


def test_posix_separator_diagnostic_names_the_child() -> None:
    """A forward-slash rejection keeps matching; the fold must not break it."""
    gate = _load_gate()
    report = "::error::decision-ledger-history: /tmp/probe/docs/decisions/notes.txt" + (
        " has an unadmitted name. Every ledger child must be a flat regular file "
        "named README.md or ending in .md; nothing else is allowed."
    )
    assert gate._names_ledger_child(report, "notes.txt")


def test_clean_report_does_not_name_the_child() -> None:
    """A clean verdict is not mistaken for a rejection naming the child."""
    gate = _load_gate()
    report = "decision-ledger-history: 2 entries at origin/main intact at head OK\n"
    assert not gate._names_ledger_child(report, "notes.txt")


def test_other_childs_rejection_does_not_match() -> None:
    """A rejection naming a different child is not read as the probed one."""
    gate = _load_gate()
    report = (
        "::error::decision-ledger-history: "
        + _windows_render("C:/repo/docs/decisions/other.txt")
        + " has an unadmitted name. Every ledger child must be a flat regular "
        "file named README.md or ending in .md; nothing else is allowed."
    )
    assert not gate._names_ledger_child(report, "notes.txt")


@pytest.mark.timeout(300)
def test_self_test_passes_on_this_platform() -> None:
    """The whole self-test stays green, so the gate floor keeps working here.

    The self-test runs in a subprocess so that ``_self_test()``'s own use of
    ``contextlib.redirect_stdout/stderr`` and ``os.chdir`` (for the CWD probe)
    cannot affect the in-process capture state of other tests if a git call
    stalls past the hang guard.  ``_SELFTEST_HANG_GUARD_SECS`` is the
    lost-run bound: 10x the worst call-phase time observed in CI (13.7 s on
    Windows in run 39959306936 shard 1).  On timeout the subprocess is killed
    and reaped before teardown, leaving no dangling processes.  The ``-X utf8``
    flag keeps the encoding behaviour consistent across platforms.
    """
    proc = subprocess.Popen(
        [
            sys.executable,
            "-X",
            "utf8",
            str(ROOT / "scripts" / "check_decisions_history.py"),
            "--test",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    try:
        stdout, _ = proc.communicate(timeout=_SELFTEST_HANG_GUARD_SECS)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, _ = proc.communicate()
        pytest.fail(
            f"_self_test() subprocess did not finish within "
            f"{_SELFTEST_HANG_GUARD_SECS}s (hang guard). Partial output: {stdout}"
        )
    assert proc.returncode == 0, stdout
