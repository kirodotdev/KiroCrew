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

ROOT = Path(__file__).resolve().parents[1]

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


def test_self_test_passes_on_this_platform() -> None:
    """The whole self-test stays green, so the gate floor keeps working here."""
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(ROOT / "scripts" / "check_decisions_history.py"),
            "--test",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
