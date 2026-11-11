"""The Windows path budget: a payload file the previous version's uninstaller
cannot move out of the way abandons the update, so the budget fails the build."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging" / "check_windows_path_budget.py"
SPEC = importlib.util.spec_from_file_location("check_windows_path_budget", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
_PREVIOUS_DONT_WRITE_BYTECODE = sys.dont_write_bytecode
sys.dont_write_bytecode = True
try:
    SPEC.loader.exec_module(MODULE)
finally:
    sys.dont_write_bytecode = _PREVIOUS_DONT_WRITE_BYTECODE

BUILD_WORKFLOW = ROOT / ".github" / "workflows" / "build.yml"
INSTALLER_SCRIPT = ROOT / ".github" / "scripts" / "test-windows-installer.ps1"
BUILD_DESKTOP = ROOT / "packaging" / "build-desktop.sh"
PREFIX = "resources\\backend-dist\\kirocrew-backend"


def _file_at(root: Path, relative: str) -> Path:
    path = root.joinpath(*relative.split("\\"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    return path


def _relative_of_length(length: int) -> str:
    """A path below PREFIX whose full install-relative length is ``length``."""

    head = "Lib\\site-packages\\pkg\\"
    stem_length = length - len(PREFIX) - 1 - len(head) - len(".py")
    assert stem_length > 0
    return head + "m" * stem_length + ".py"


def test_budget_arithmetic() -> None:
    # 259 usable characters, minus C:\Users\<20-character profile>\AppData\Local\Temp
    # (48), minus \nsaXXXX.tmp\old-install\ (25: NSIS's three-letter temp prefix,
    # up to four hex digits and .tmp).
    assert MODULE.TEMP_ALLOWANCE == 48
    assert MODULE.MOVE_DIRECTORY == 25
    assert MODULE.RELATIVE_PATH_BUDGET == 186


def test_a_path_at_the_budget_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    relative = _relative_of_length(MODULE.RELATIVE_PATH_BUDGET)
    _file_at(tmp_path, relative)
    assert MODULE.main(["--root", str(tmp_path), "--prefix", PREFIX]) == 0
    assert (
        f"longest path relative to the install root is {MODULE.RELATIVE_PATH_BUDGET}"
        in capsys.readouterr().out
    )


def test_a_path_over_the_budget_fails_and_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _file_at(tmp_path, _relative_of_length(120))
    deep = _relative_of_length(MODULE.RELATIVE_PATH_BUDGET + 1)
    _file_at(tmp_path, deep)
    assert MODULE.main(["--root", str(tmp_path), "--prefix", PREFIX]) == 1
    err = capsys.readouterr().err
    assert "1 path(s) exceed the 186-character budget" in err
    assert f"{MODULE.RELATIVE_PATH_BUDGET + 1}: {PREFIX}\\{deep}" in err


def test_the_prefix_counts_toward_the_budget(tmp_path: Path) -> None:
    # Measured from the install root the same file is under budget; measured as
    # the bundled backend it is over, because the prefix is part of the move.
    relative = _relative_of_length(MODULE.RELATIVE_PATH_BUDGET + 1)
    _file_at(tmp_path, relative)
    assert MODULE.main(["--root", str(tmp_path)]) == 0
    assert MODULE.main(["--root", str(tmp_path), "--prefix", PREFIX]) == 1


def test_the_unpruned_test_bytecode_file_is_over_budget(tmp_path: Path) -> None:
    # The deepest file an unpruned payload carries, which the previous version's
    # uninstaller cannot move on the Windows runner: 201 characters below the
    # install root.
    deep = (
        "Lib\\site-packages\\kiro_crew\\apps\\builtins\\aws_control\\crew\\packaging\\tests\\"
        "__pycache__\\test_sensitive_source_and_report_identity_credential_floor.cpython-312.pyc"
    )
    _file_at(tmp_path, deep)
    assert len(f"{PREFIX}\\{deep}") == 201
    assert MODULE.main(["--root", str(tmp_path), "--prefix", PREFIX]) == 1


def test_an_empty_root_fails(tmp_path: Path) -> None:
    assert MODULE.main(["--root", str(tmp_path)]) == 1


def test_the_windows_jobs_run_the_gate() -> None:
    workflow = BUILD_WORKFLOW.read_text(encoding="utf-8")
    assert re.search(
        r"packaging/check_windows_path_budget\.py --root \$out --prefix \"resources\\backend-dist\\kirocrew-backend\"",
        workflow,
    ), "the CI payload is not measured against the path budget"
    script = INSTALLER_SCRIPT.read_text(encoding="utf-8")
    assert (
        "check_windows_path_budget.py" in script
    ), "the installed tree is not measured against the path budget"


def test_the_ci_payload_is_pruned_like_the_release_payload() -> None:
    """build.yml's assembly mirrors build_backend_windows's prune before precompiling.

    Without it pip's bytecode for every installed test tree ships in the CI
    payload, and those are its deepest files.
    """

    release = BUILD_DESKTOP.read_text(encoding="utf-8")
    start = release.index("build_backend_windows() {")
    release = release[start : release.index("\n}\n", start)]
    assert "-name __pycache__ -prune" in release
    assert "\\( -name tests -o -name test \\)" in release
    removed = re.search(r"rm -rf (Lib/test .*?)\s*\\\n\s*2>/dev/null", release, re.S)
    assert removed is not None
    release_dirs = {p.replace("/", "\\") for p in re.split(r"[\s\\]+", removed.group(1)) if p}

    workflow = BUILD_WORKFLOW.read_text(encoding="utf-8")
    assert '-Filter "__pycache__"' in workflow
    assert '$_.Name -eq "tests" -or $_.Name -eq "test"' in workflow
    ci_dirs = set(re.findall(r'"((?:Lib|include|libs|tcl)[^"]*)"', workflow))
    assert release_dirs <= ci_dirs, f"the CI prune misses {sorted(release_dirs - ci_dirs)}"
    # Pruned before the precompile pass, or it would delete the shipped closure.
    assert workflow.index('-Filter "__pycache__"') < workflow.index(
        "precompile_windows.py --root $out"
    )
