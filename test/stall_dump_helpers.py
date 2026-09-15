"""Synthesized loop-stall crash dumps for the attribution and breaker tests.

Written in faulthandler's own format (``Timeout (...)!`` preamble, ``Thread
0x...`` blocks newest-first, ``  File "...", line N in f`` frames) behind the
crash-dump store's real 4-line header, so what the tests parse is what a
gateway writes. Files are named by a sequence rather than the wall clock so
several dumps can be minted inside one second (the store names to the second).
"""

from __future__ import annotations

import os
from pathlib import Path

from kiro_crew.dashboard import crash_dump_store

_SITE = "/opt/venv/lib/python3.12/site-packages/kiro_crew"

# The field crash, frames innermost-first exactly as faulthandler prints them.
CRON_STACK = [
    f'  File "{_SITE}/security/__init__.py", line 7969 in is_sensitive_bash_command',
    f'  File "{_SITE}/llm_helpers.py", line 2058 in _resolve_permission',
    f'  File "{_SITE}/llm_helpers.py", line 1596 in stream_and_collect',
    f'  File "{_SITE}/slack/gateway.py", line 1192 in _cron_stream_with_posttoken_resume',
    f'  File "{_SITE}/slack/gateway.py", line 4471 in _cron_callback',
    f'  File "{_SITE}/cron.py", line 3591 in _execute',
    f'  File "{_SITE}/cron.py", line 3544 in _execute_with_timeout',
    f'  File "{_SITE}/cron.py", line 3356 in _run_job_isolated',
    '  File "/usr/lib/python3.12/asyncio/events.py", line 88 in _run',
    '  File "/usr/lib/python3.12/asyncio/base_events.py", line 1986 in _run_once',
]
CHAT_STACK = [
    f'  File "{_SITE}/security/__init__.py", line 7969 in is_sensitive_bash_command',
    f'  File "{_SITE}/hooks.py", line 713 in on_tool_call',
    f'  File "{_SITE}/dashboard/chat_runner.py", line 7742 in _run_turn',
    '  File "/usr/lib/python3.12/asyncio/events.py", line 88 in _run',
]
SLACK_STACK = [
    f'  File "{_SITE}/llm_helpers.py", line 2058 in _resolve_permission',
    f'  File "{_SITE}/llm_helpers.py", line 1596 in stream_and_collect',
    f'  File "{_SITE}/slack/gateway.py", line 5219 in _handle_message',
    f'  File "{_SITE}/slack/handler.py", line 3397 in handle',
]
IDLE_WORKER = [
    '  File "/usr/lib/python3.12/threading.py", line 359 in wait',
    '  File "/usr/lib/python3.12/concurrent/futures/thread.py", line 90 in _worker',
]

_DUMP_SEQ = 0


def write_dump(
    dumps_dir: Path,
    pid: int,
    main_stack: list[str],
    *,
    mtime: float,
    domain: str | None = None,
    start: str = "1",
) -> Path:
    """A dump in the store's on-disk shape: its 4-line header, then stacks.

    ``domain`` defaults to THIS process's PID domain, so a marker written by the
    test process joins it; pass another to model a dump from a container or
    host that is not the one reading it."""
    global _DUMP_SEQ
    _DUMP_SEQ += 1
    dumps_dir.mkdir(parents=True, exist_ok=True)
    path = dumps_dir / f"{crash_dump_store.DUMP_PREFIX}2026090{_DUMP_SEQ:02d}T000000Z.txt"
    header = [
        "# KiroCrew loop-stall crash dump — opened test",  # brand-ok: mirrors the header bytes the store writes
        f"# PID: {pid} @ {domain or crash_dump_store._pid_domain()} start={start}",
        "# If thread stacks appear below, the event loop wedged and faulthandler fired.",
        "",
    ]
    body = ["Timeout (0:00:25)!", "Thread 0x00007f0000000001 (most recent call first):"]
    body += IDLE_WORKER
    body += ["Thread 0x00007f0000000002 (most recent call first):"]
    body += main_stack
    path.write_text("\n".join(header + body) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path
