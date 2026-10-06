#!/usr/bin/env python3
"""check_playwright_cli_banner.py -- the installed playwright-cli banner still parses.

## The failure class

The Browser view proves that the ``playwright-cli show`` child owns the port it
serves by reading the child's own ``Listening on http://127.0.0.1:<port>``
stdout line (``_binding_reported_port_on_line`` in
``src/kiro_crew/browser_cli/view.py``). On a host with no other listener
attribution that line is the only ownership evidence, and the installer pins
``@playwright/cli@latest``. The parser is unit-tested against fixed strings
only, so an upstream wording change would first surface as a fail-closed
Browser view on users' machines.

## What this checks

It launches the CLI once with the exact argv the Browser view uses
(``show --port 0 --host 127.0.0.1``), reads its stdout under the same line,
byte and time bounds the view applies, and feeds every line to the real
parser. It exits 0 on the first line the parser accepts. Otherwise it exits 1
and prints the CLI version and every stdout line it read, so the drift report
names the version and the offending line. The child is always killed before
the script returns.

Usage::

    python3 scripts/check_playwright_cli_banner.py [--timeout SECONDS] [CLI ...]

``CLI`` defaults to ``playwright-cli`` on PATH; pass a full command (for
example ``npx --yes @playwright/cli@latest``) to check another launcher.
"""

from __future__ import annotations

import argparse
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Sequence

from kiro_crew import platform_compat
from kiro_crew.browser_cli import install, view

_VERSION_TIMEOUT_S = 30.0
_REAP_TIMEOUT_S = 5.0
_EOF = b""


def _cli_version(command: Sequence[str]) -> str:
    """The CLI's ``--version`` answer, or a short reason it gave none."""
    try:
        done = subprocess.run(
            [*command, "--version"],
            capture_output=True,
            timeout=_VERSION_TIMEOUT_S,
            check=False,
            env=install.cli_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unknown ({type(exc).__name__}: {exc})"
    text = (done.stdout + done.stderr).decode("utf-8", errors="replace")
    version = install._first_version(text)
    if version is None:
        return f"unknown (rc={done.returncode}, output={text.strip()[:200]!r})"
    return version


def _pump(stream, lines: queue.Queue[bytes]) -> None:
    """Forward *stream* line by line, then an EOF marker."""
    try:
        for line in iter(lambda: stream.readline(view._BINDING_PROOF_MAX_BYTES), _EOF):
            lines.put(line)
    finally:
        lines.put(_EOF)


def check(command: Sequence[str], timeout: float) -> tuple[int | None, list[bytes], str]:
    """Launch ``show --port 0`` once; return ``(port, lines_read, failure_reason)``.

    *port* is the port the parser accepted, or ``None`` with *failure_reason*
    set. The child is killed before this returns, whatever the outcome.
    """
    argv = view._show_argv(list(command), 0)
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=install.cli_env(),
            start_new_session=platform_compat.IS_POSIX,
        )
    except OSError as exc:
        return None, [], f"could not launch {argv!r}: {exc}"
    lines: queue.Queue[bytes] = queue.Queue()
    reader = threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True)
    reader.start()
    seen: list[bytes] = []
    total = 0
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, seen, f"no parsable banner within {timeout:g}s"
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line == _EOF:
                return (
                    None,
                    seen,
                    f"stdout closed (exit code {proc.poll()}) before a parsable banner",
                )
            seen.append(line)
            total += len(line)
            port = view._binding_reported_port_on_line(line, 0)
            if port is not None:
                return port, seen, ""
            if len(seen) >= view._BINDING_PROOF_MAX_LINES or total >= view._BINDING_PROOF_MAX_BYTES:
                return None, seen, "line/byte budget exhausted before a parsable banner"
    finally:
        platform_compat.kill_popen_tree(proc)
        try:
            proc.wait(timeout=_REAP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--timeout",
        type=float,
        default=view._BINDING_PROOF_TIMEOUT_S,
        help="seconds to wait for the banner (default: the Browser view's own budget)",
    )
    parser.add_argument("command", nargs="*", default=[install.CLI_BIN])
    args = parser.parse_args(argv)
    command = args.command or [install.CLI_BIN]

    resolved = shutil.which(command[0], path=install.cli_env().get("PATH")) or command[0]
    version = _cli_version(command)
    port, seen, reason = check(command, args.timeout)
    if port is not None:
        print(
            f"OK: playwright-cli {version} ({resolved}) banner parsed (port {port}): "
            f"{seen[-1].decode('utf-8', 'replace').strip()!r}"
        )
        return 0
    print(f"FAIL: playwright-cli {version} ({resolved}): {reason}.")
    print(f"Expected a stdout line like {view._VERIFIED_BINDING_BANNER_EXAMPLE!r}.")
    if seen:
        print("stdout lines read:")
        for line in seen:
            print(f"  {line.decode('utf-8', 'replace').rstrip()!r}")
    else:
        print("stdout lines read: none")
    return 1


if __name__ == "__main__":
    sys.exit(main())
