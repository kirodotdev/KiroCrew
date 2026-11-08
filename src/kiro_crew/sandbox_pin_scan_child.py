"""The sandbox mount-source pin scan, as its own process.

Why this is not a thread
------------------------
The pin scan (:func:`kiro_crew.sandbox_mount_sweep._mount_pinned_source_names`)
reads ``/proc`` for every task on the host: a ``stat``, two ``readlink``-like
probes and a ``mountinfo`` read per task, tens of thousands of syscalls on a busy
host. Each one releases the GIL and then has to take it back, so while a pass runs
in the gateway every other gateway thread (the crew-log writers, the context-build
pool, the event loop) queues behind it in ``take_gil``. The answer the sweep needs
is small: the set of stand-in names a live mount namespace still binds, and
whether the scan covered every task that could bind one. So the scan runs in a
child process and sends that answer back as one JSON line.

Spawn, never fork
-----------------
The gateway carries 200+ threads, so this is a ``subprocess`` launch of a fresh
interpreter (``python -P -m kiro_crew.sandbox_pin_scan_child <kind>``), like
:mod:`kiro_crew.consolidation_write_child`. Only the scan kind goes in and only
names and two flags come out. The child runs at a lower CPU priority and exits
after one scan.

Fail closed
-----------
A child that cannot start, times out, crashes, exits non-zero or writes anything
but one well-formed reply makes :func:`scan_in_child` answer ``None``. The sweep
then treats the pass as an incomplete scan with coverage unproven, which reclaims
no directory, exactly as an incomplete in-process scan does.

``python -m kiro_crew.sandbox_pin_scan_child`` is the child's entry point; nothing
else runs it.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

logger = logging.getLogger("kiro_crew.sandbox")

_CHILD_MODULE = "kiro_crew.sandbox_pin_scan_child"
#: The two scans the sweep runs: the keyed ``kirocrew_sb_<pid>_`` shape and the
#: legacy ``tempfile`` shape (``sandbox_mount_sweep._bound_source_basenames``).
SCAN_KINDS = ("keyed", "legacy")
#: Longest one scan may take, start-up included. A scan takes seconds on a busy
#: host; past this the child is killed and the pass reclaims nothing.
SCAN_TIMEOUT_SECS = 300.0
#: ``os.nice`` increment in the child, as for the consolidation write child.
_CHILD_NICE = 10


def scan_in_child(
    kind: str, *, timeout: float = SCAN_TIMEOUT_SECS, argv: list[str] | None = None
) -> tuple[set[str], bool, bool] | None:
    """``(pinned, complete, covered)`` from a child scan, or None when it failed.

    ``argv`` replaces the child command line (tests use it to make a child that
    hangs, crashes or replies badly).
    """
    from kiro_crew import platform_compat

    if argv is None:
        # -P: never put the working directory on the child's sys.path, so a
        # kiro_crew/ directory in the gateway's cwd cannot shadow the package.
        argv = platform_compat.isolated_python_argv("-P", "-m", _CHILD_MODULE, kind)
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, this interpreter
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _failed(kind, f"timed out after {timeout:.0f} s")
        return None
    except OSError as exc:
        _failed(kind, f"could not start: {exc}")
        return None
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", errors="replace").strip()[-500:]
        _failed(kind, f"exited with code {proc.returncode}: {tail}")
        return None
    reply = _parse_reply(proc.stdout)
    if reply is None:
        _failed(kind, "wrote a malformed reply")
        return None
    return reply


def _failed(kind: str, why: str) -> None:
    logger.warning(
        "sandbox mount-source sweep: %s pin scan child failed (%s); this pass reclaims "
        "no directory",
        kind,
        why,
    )
    return None


def _parse_reply(raw: bytes) -> tuple[set[str], bool, bool] | None:
    """The child's one reply line, or None unless it has exactly the expected shape."""
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(msg, dict):
        return None
    pinned, complete, covered = msg.get("pinned"), msg.get("complete"), msg.get("covered")
    if (
        not isinstance(pinned, list)
        or not all(isinstance(name, str) for name in pinned)
        or not isinstance(complete, bool)
        or not isinstance(covered, bool)
    ):
        return None
    return set(pinned), complete, covered


def _scan(kind: str) -> dict[str, object]:
    from kiro_crew import sandbox_mount_sweep as sweep

    coverage = sweep._PinScanCoverage()
    matcher = sweep._is_legacy_source_name if kind == "legacy" else None
    pinned, complete = sweep._mount_pinned_source_names_in_process(
        "/proc", matcher=matcher, coverage=coverage
    )
    return {"pinned": sorted(pinned), "complete": complete, "covered": coverage.covered}


def main(argv: list[str] | None = None) -> int:
    """Child entry: run one scan and write its answer as one JSON line."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in SCAN_KINDS:
        print(f"usage: {_CHILD_MODULE} {{{'|'.join(SCAN_KINDS)}}}", file=sys.stderr)
        return 2
    # The reply owns the original stdout; anything else that prints goes to stderr.
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    if hasattr(os, "nice"):
        try:
            os.nice(_CHILD_NICE)
        except OSError:
            pass
    reply = _scan(args[0])
    out.write(json.dumps(reply, separators=(",", ":")).encode("utf-8") + b"\n")
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
