"""Fast-path contract for bare `--version` (cli.md Import Weight Contract).

Bare `--version` must print `kirocrew <version>` and exit 0 on both entry
paths, without paying for `kiro_crew.cli`'s heavy module scope — and importing
`cli` itself must never exit the process, whatever argv is set (there is
deliberately no module-scope version guard left in `cli.py`: every real entry
answers earlier). `--verbose --version` reaches argparse's `version` action
instead, and must print the same line as the bare form. The `-c` cases run in
fresh interpreters so import-time behavior is observed directly (same reason as
test_cli_lazy_imports).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from kiro_crew import __version__

_EXPECTED_VERSION_LINE = f"kirocrew {__version__}\n"


def _run_module_version(tmp_path: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    """`python -m kiro_crew [flags] --version` end to end (bare: the `__main__` guard)."""
    return subprocess.run(
        [sys.executable, "-m", "kiro_crew", *flags, "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=tmp_path,
        env={**os.environ, "KIROCREW_HOME": str(tmp_path / "home")},
    )


def _run_probe(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run a `-c` probe with an isolated data home under `tmp_path`."""
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=tmp_path,
        env={**os.environ, "KIROCREW_HOME": str(tmp_path / "home")},
    )


class TestVersionFastPath:
    def test_module_entry_prints_version(self, tmp_path: Path) -> None:
        res = _run_module_version(tmp_path)
        assert res.returncode == 0, f"--version failed:\n{res.stderr}"
        assert res.stdout == _EXPECTED_VERSION_LINE

    def test_bootstrap_entry_prints_version(self, tmp_path: Path) -> None:
        res = _run_probe(
            "import sys; sys.argv = ['kirocrew', '--version'];"
            " from kiro_crew._bootstrap import main; main()",
            tmp_path,
        )
        assert res.returncode == 0, f"bootstrap --version failed:\n{res.stderr}"
        assert res.stdout == _EXPECTED_VERSION_LINE

    def test_argparse_version_matches_fast_path(self, tmp_path: Path) -> None:
        # `--verbose` is a real top-level flag, so this argv is valid but not
        # bare: the `__main__` guard lets it through to argparse's `version` action.
        bare = _run_module_version(tmp_path)
        full = _run_module_version(tmp_path, "--verbose")
        assert full.returncode == 0, f"--verbose --version failed:\n{full.stderr}"
        assert full.stdout == bare.stdout

    def test_cli_import_never_exits(self, tmp_path: Path) -> None:
        res = _run_probe(
            "import sys; sys.argv = ['kirocrew', '--version'];"
            " import kiro_crew.cli; print('IMPORTED')",
            tmp_path,
        )
        assert res.returncode == 0, f"cli import failed:\n{res.stderr}"
        assert res.stdout == "IMPORTED\n"
