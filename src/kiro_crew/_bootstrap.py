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
``ModuleNotFoundError`` from a source checkout, installs the checkout's
declared dependencies once and retries the import in-process. A failed import
is side-effect-free (Python evicts the failing module from ``sys.modules``),
so the retry needs no re-exec — which also sidesteps the POSIX/Windows
``execv`` divergence. HOW the install runs is
:func:`kiro_crew.dep_sync.sync_or_reinstall`'s decision: an editable
reinstall where pip can replace the console script, and a dependency-only
install where it cannot — which on Windows is always, since this process was
launched through the very ``kirocrew.exe`` a reinstall would have to rewrite.
That is why the heal now runs there instead of printing a manual one-liner: a
dependency install never touches that wrapper, and a missing dependency is
precisely what brought us here.

Everything imported here MUST be stdlib: this module runs BEFORE the
package's dependencies are known to exist. ``kiro_crew.dep_sync`` is the one
package import it makes, and counts as stdlib for this purpose because it is
required to stay stdlib-only — it imports nothing else, so it cannot raise the
very error being healed, and a test asserts that. Output MUST stay
ASCII-only — it prints before ``platform_compat.ensure_utf8_console()`` has
run, so non-ASCII would UnicodeEncodeError on Windows cp1252 pipes.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from typing import Callable

# Module scope, not function-local: this is the one package import this file can
# safely make at import time, because dep_sync imports the standard library only
# (asserted by a test) and so cannot raise the ModuleNotFoundError this file
# exists to heal. `kiro_crew.__init__` itself imports nothing but asyncio.
from kiro_crew import dep_sync

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
    """Bring this venv up to the checkout's declared dependencies. ``True`` on success.

    The repo path is derived from this module's own ``__file__`` -- never user or
    agent input -- and the argv is built by :mod:`kiro_crew.dep_sync`, which picks
    the editable reinstall where it can run and a dependency-only install where it
    cannot. Windows is the case where it cannot: pip cannot replace the running
    ``kirocrew.exe`` this very process was launched through, and a reinstall that
    dies on it has already deleted the editable ``.pth``. Installing only the
    declared requirements never touches that wrapper, which is why the heal can run
    there at all -- it is exactly the missing dependency that brought us here.
    """
    root = _source_checkout_root()
    if root is None:
        return False
    print(
        f"kirocrew: missing dependency {missing!r} - your checkout added "
        "dependencies since the last install. Installing the declared "
        "requirements to catch up...",
        file=sys.stderr,
    )

    def _emit(message: str, error: bool) -> None:
        # This module's output must stay ASCII (see the module docstring): it
        # prints before ensure_utf8_console(), and these messages carry pip's
        # output and filesystem paths, neither of which is ASCII by nature.
        print(message.encode("ascii", "replace").decode("ascii"), file=sys.stderr)

    try:
        rc = dep_sync.sync_or_reinstall(
            root, Path(sys.executable), _emit, timeout=_PIP_TIMEOUT_SECS
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return rc == 0


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
