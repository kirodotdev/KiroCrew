#!/usr/bin/env python3
"""check_playwright_cli_attach_guard.py -- the attach guard still reads the installed CLI.

## The failure class

The managed launcher's attach guard (``ATTACH_GUARD_JS`` in
``src/kiro_crew/browser_cli/managed_launcher.py``) decides whether an argv is an
``attach`` by running the CLI's own parser, with the boolean options ``program.js``
adds read from that file's source. The installer pins ``@playwright/cli@latest``,
so a release that moves that layout (renames ``booleanOptions``, bundles
``program.js``, hides ``help.json``) would leave the guard on its built-in copy of
one version's options without anyone noticing, and a spelling like
``--json attach`` could then run with no time limit.

## What this checks

It runs the real guard against the installed CLI with ``--json attach --help``.
``--json`` is one of the options only ``program.js`` declares, so the guard reads
``attach`` as the verb only when it loaded the CLI's parser and found that list.
The guard's built-in copy is emptied and its deadline shortened to 1 ms, so a
guard that read the layout stops the command and exits 124 with its message,
and any other outcome is drift. Nothing reaches a browser: the CLI is stopped
before it acts. The guard and the CLI are always killed before this returns.

Usage::

    python3 scripts/check_playwright_cli_attach_guard.py [--entry PATH] [--node NODE]

``--entry`` defaults to ``@playwright/cli/playwright-cli.js`` under ``npm root -g``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.browser_cli import managed_launcher

_DEADLINE_LINE = "const TIMEOUT_MS = 120000;"
_BUILT_IN_PREFIX = "const PROGRAM_BOOLEANS = ["
_PROBE_ARGS = ("--json", "attach", "--help")
_TIMEOUT_EXIT = 124
_RUN_TIMEOUT_S = 60.0
_REAP_TIMEOUT_S = 5.0


def probe_guard() -> str | None:
    """The guard with a 1 ms deadline and no built-in option list, or None when
    the guard's own text no longer has the lines this check rewrites."""
    text = managed_launcher.ATTACH_GUARD_JS
    lines = text.splitlines(keepends=True)
    built_in = [line for line in lines if line.startswith(_BUILT_IN_PREFIX)]
    if text.count(_DEADLINE_LINE) != 1 or len(built_in) != 1:
        return None
    text = text.replace(_DEADLINE_LINE, "const TIMEOUT_MS = 1;")
    return text.replace(built_in[0], "const PROGRAM_BOOLEANS = [];\n")


def check(node: str, entry: Path) -> tuple[bool, str]:
    """Run the probe guard against *entry*; return ``(ok, what_happened)``."""
    guard_text = probe_guard()
    if guard_text is None:
        return False, "the guard's deadline or built-in option list moved; update this check"
    with tempfile.TemporaryDirectory() as tmp:
        guard = Path(tmp) / managed_launcher.GUARD_NAME
        guard.write_text(guard_text, encoding="utf-8")
        argv = [node, str(guard), str(entry), *_PROBE_ARGS]
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=platform_compat.IS_POSIX,
            )
        except OSError as exc:
            return False, f"could not launch {argv!r}: {exc}"
        try:
            out, err = proc.communicate(timeout=_RUN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return False, f"the guard did not finish within {_RUN_TIMEOUT_S:g}s"
        finally:
            platform_compat.kill_popen_tree(proc)
            try:
                proc.communicate(timeout=_REAP_TIMEOUT_S)
            except (subprocess.TimeoutExpired, ValueError):
                pass
    if proc.returncode == _TIMEOUT_EXIT and "Accept Chrome's debugging prompt" in err:
        return True, "the guard read --json as a boolean and limited the attach"
    return False, (
        f"the guard did not limit {' '.join(_PROBE_ARGS)!r} (exit {proc.returncode}); "
        f"stdout={out.strip()[:400]!r} stderr={err.strip()[:400]!r}"
    )


def _default_entry() -> Path | None:
    npm = shutil.which("npm")
    if npm is None:
        return None
    done = subprocess.run(
        [npm, "root", "-g"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    root = done.stdout.strip()
    return Path(root) / "@playwright" / "cli" / "playwright-cli.js" if root else None


def _version(entry: Path) -> str:
    try:
        return str(json.loads((entry.parent / "package.json").read_text("utf-8"))["version"])
    except (OSError, ValueError, KeyError):
        return "unknown"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--entry", type=Path, default=None, help="playwright-cli.js to check")
    parser.add_argument("--node", default=shutil.which("node") or "node", help="Node to run")
    args = parser.parse_args(argv)
    entry = args.entry or _default_entry()
    if entry is None or not entry.is_file():
        print(f"FAIL: no installed playwright-cli.js found (looked at {entry}).")
        return 1
    ok, what = check(args.node, entry)
    print(f"{'OK' if ok else 'FAIL'}: @playwright/cli {_version(entry)} ({entry}): {what}.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
