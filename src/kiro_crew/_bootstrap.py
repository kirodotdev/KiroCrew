"""Console-script entry that self-heals a stale editable install.

``kirocrew`` is often run from a git checkout (``pip install -e .``). A plain
``git pull`` never re-runs dependency resolution, so a commit that adds a
runtime dependency leaves every subsequent CLI invocation dying at import
time with a raw ``ModuleNotFoundError`` traceback — for a state the tool can
repair itself. Release installs are unaffected (pip resolves
``install_requires`` at install time) and ``kirocrew update`` already re-runs
``pip install -e .``; this closes the git-pull gap.

This module is the console-script entry point
(``kirocrew = kiro_crew._bootstrap:main``). It imports the real CLI and, on
``ModuleNotFoundError`` from a source checkout, runs ONE
``pip install -e <repo>`` and retries the import in-process. A failed import
is side-effect-free (Python evicts the failing module from ``sys.modules``),
so the retry needs no re-exec — which also sidesteps the POSIX/Windows
``execv`` divergence. On Windows the heal itself is skipped — a running
console launcher cannot be replaced — and the one-line manual fix is printed
instead, as it is outside a source checkout.

Everything imported here MUST be stdlib: this module runs BEFORE the
package's dependencies are known to exist. Output MUST stay ASCII-only —
it prints before ``platform_compat.ensure_utf8_console()`` has run, so
non-ASCII would UnicodeEncodeError on Windows cp1252 pipes.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from typing import Callable

_PIP_TIMEOUT_SECS = 300


def _import_cli() -> Callable[[], None]:
    """Import the real CLI entry (separated so tests can stub the failure)."""
    from kiro_crew.cli import main as cli_main

    return cli_main


def _source_checkout_root() -> Path | None:
    """Repo root when running from an editable/source install, else ``None``.

    An editable install resolves ``kiro_crew`` inside ``<repo>/src/``; a wheel
    install resolves it inside ``site-packages``. Only the former has our
    ``setup.cfg`` two levels up.
    """
    root = Path(__file__).resolve().parents[2]
    if (root / "setup.cfg").is_file() and (root / "src" / "kiro_crew").is_dir():
        return root
    return None


def _self_heal(missing: str) -> bool:
    """Run ONE ``pip install -e <repo>``; ``True`` when it succeeded.

    The argv is fixed and the repo path is derived from this module's own
    ``__file__`` — never user or agent input. pip's own stderr flows to the
    console so failures stay diagnosable.
    """
    if sys.platform == "win32":
        # Windows locks a running console launcher, so pip cannot replace
        # kirocrew.exe from inside a process started by it and the reinstall
        # fails partway. The manual one-liner works there: the user runs it
        # from a shell where kirocrew is not running.
        return False
    root = _source_checkout_root()
    if root is None:
        return False
    print(
        f"kirocrew: missing dependency {missing!r} - your checkout added "
        "dependencies since the last install. Running one-time "
        "`pip install -e .` to catch up...",
        file=sys.stderr,
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", str(root), "--quiet"],
            timeout=_PIP_TIMEOUT_SECS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def main() -> None:
    """Import the real CLI, self-healing a stale editable install once."""
    try:
        cli_main = _import_cli()
    except ModuleNotFoundError as exc:
        if not _self_heal(exc.name or str(exc)):
            print(
                f"kirocrew: cannot start - {exc}.\n"
                "Your installed dependencies are older than your checkout. "
                "Fix with: pip install -e <path to your Kiro Crew checkout>",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc
        # Import-system finder caches are per-directory-mtime; a package
        # installed after interpreter start can stay invisible on
        # coarse-mtime filesystems without an explicit invalidation.
        importlib.invalidate_caches()
        try:
            cli_main = _import_cli()
        except ModuleNotFoundError as exc2:  # heal ran but did not cover it
            print(
                f"kirocrew: still failing after reinstall - {exc2}. "
                "Check `pip install -e .` output for errors.",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc2
        print("kirocrew: dependencies restored.", file=sys.stderr)
    cli_main()
