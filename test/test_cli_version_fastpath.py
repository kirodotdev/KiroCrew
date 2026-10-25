"""Fast-path contract for bare `--version` (cli.md Import Weight Contract).

Bare `--version` must print `kirocrew <version>` (plus the source-checkout
note when the package runs from `<repo>/src`) and exit 0 on both entry paths, without paying for `kiro_crew.cli`'s heavy module scope — and importing
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

from kiro_crew import SOURCE_CHECKOUT_NOTE, __version__, version_line

_EXPECTED_VERSION_LINE = f"{version_line()}\n"


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


def _layout(root: Path, *, checkout: bool, stamped: bool = False) -> Path:
    """A package dir under *root*: ``<repo>/src/kiro_crew`` or ``site-packages``."""
    if checkout:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (root / "setup.cfg").write_text("[metadata]\nname = kiro-crew\n", encoding="utf-8")
    else:
        pkg = root / "lib" / "site-packages" / "kiro_crew"
        pkg.mkdir(parents=True)
    if stamped:
        (pkg / "BUILD_VERSION").write_text(__version__, encoding="utf-8")
    return pkg


class TestVersionProvenance:
    def test_source_checkout_says_so(self, tmp_path: Path) -> None:
        pkg = _layout(tmp_path, checkout=True)
        assert version_line(str(pkg)) == f"kirocrew {__version__} {SOURCE_CHECKOUT_NOTE}"

    def test_installed_package_prints_the_bare_version(self, tmp_path: Path) -> None:
        pkg = _layout(tmp_path, checkout=False)
        assert version_line(str(pkg)) == f"kirocrew {__version__}"

    def test_a_stamped_build_is_not_a_checkout(self, tmp_path: Path) -> None:
        pkg = _layout(tmp_path, checkout=True, stamped=True)
        assert version_line(str(pkg)) == f"kirocrew {__version__}"

    def test_the_note_stays_ascii(self) -> None:
        # `_bootstrap` prints it before the console is switched to UTF-8.
        assert SOURCE_CHECKOUT_NOTE
        assert SOURCE_CHECKOUT_NOTE.isascii()

    def test_argparse_entry_prints_the_same_line(self, tmp_path: Path) -> None:
        res = _run_probe(
            "import sys; sys.argv = ['kirocrew', '--version', '--verbose'];"
            " from kiro_crew.cli import main; main()",
            tmp_path,
        )
        assert res.returncode == 0, f"argparse --version failed:\n{res.stderr}"
        assert res.stdout == _EXPECTED_VERSION_LINE
