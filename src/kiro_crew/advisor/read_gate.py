"""kiro-cli ``preToolUse`` hook that gates the reviewer's builtin reads.

kiro-cli approves its builtin ``fs_read`` / ``grep`` natively, so no permission
request reaches Crew for them; it does run the agent spec's ``preToolUse`` hooks
for every tool call, hands the hook the tool input on stdin as JSON, and blocks
the call when the hook exits 2. This module is that hook for the managed
reviewer spec: it judges the read with :func:`advisor_permission_gate` (the
read-only ceiling, the governance ceiling, the operator's deny state and the
sensitive-path floor) BEFORE kiro-cli executes it.

Exit codes are the whole contract, and only 2 blocks: kiro-cli treats any other
non-zero exit as a hook error and runs the tool anyway (verified on 2.21.4). So
every failure here -- unreadable stdin, a gate exception, an import error --
exits 2. The installer refuses to write a spec whose hook command cannot run,
because a missing command is the same fail-open.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, TextIO

from kiro_crew.subprocess_utf8 import UTF8_TEXT

#: How kiro-cli names its builtin read in the hook payload; the gate authorizes
#: on the trusted tool name.
_HOOK_TOOL_NAMES = {"read": "fs_read"}

BLOCK = 2


def _interpreter() -> str:
    return sys.executable


def shell_quote(path: str) -> str:
    return shlex.quote(path)


def hook_argv() -> list[str]:
    return [_interpreter(), "-m", "kiro_crew.advisor.read_gate"]


def hook_command() -> str:
    """The spec's hook command: the gateway's own interpreter running this module."""
    return " ".join(shell_quote(part) for part in hook_argv())


def _probe(cwd: Path, path: str) -> int:
    payload = {
        "hook_event_name": "preToolUse",
        "cwd": str(cwd),
        "tool_name": "read",
        "tool_input": {"operations": [{"mode": "Line", "path": path}]},
    }
    result = subprocess.run(  # noqa: S603 - argv is the gateway's own interpreter
        hook_argv(), input=json.dumps(payload), capture_output=True, timeout=60, **UTF8_TEXT
    )
    return result.returncode


def self_test(*, cwd: Path) -> str:
    """Execute the complete hook command against a known-denied and a
    known-allowed read; return ``""`` when both verdicts are right, else why not.

    The interpreter check proves the command can start; this proves the module
    it runs still judges -- an editable install runs the hook from the source
    tree, and a hook that exits anything but 2 lets kiro-cli run the read.
    """
    try:
        denied = _probe(cwd, str(Path.home() / ".ssh" / "id_rsa"))
        if denied != BLOCK:
            return f"deny probe exited {denied}, expected {BLOCK}"
        allowed = _probe(cwd, str(cwd / "probe.txt"))
        if allowed != 0:
            return f"allow probe exited {allowed}, expected 0"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"hook command did not run: {exc}"
    return ""


def _event(payload: dict[str, Any]) -> SimpleNamespace:
    raw_name = str(payload.get("tool_name") or "").strip()
    tool_name = _HOOK_TOOL_NAMES.get(raw_name, raw_name)
    tool_input = payload.get("tool_input")
    raw = tool_input if isinstance(tool_input, dict) else None
    return SimpleNamespace(
        kind="permission_request",
        tool_name=tool_name,
        tool_kind=raw_name,
        title=tool_name,
        raw_tool_params=raw,
        tool_input=json.dumps(tool_input) if raw is not None else "",
        mcp_server_name="",
        mcp_identity_trusted=False,
    )


def run(
    *,
    stdin: TextIO,
    stderr: TextIO,
    gate: Callable[..., str] | None = None,
) -> int:
    """Judge one hook payload; return the process exit code (0 allow, 2 block)."""
    try:
        payload = json.loads(stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("hook payload is not an object")
        if gate is None:
            # A fresh process: the governance ceiling the gate consults lives on
            # the platform context, installed at gateway boot -- boot it here
            # the same way (idempotent; a failed boot blocks, never falls open).
            from kiro_crew.advisor.composition import advisor_permission_gate
            from kiro_crew.config import KiroCrewConfig
            from kiro_crew.platform.bootstrap import boot_platform

            boot_platform(KiroCrewConfig.load())
            gate = advisor_permission_gate
        cwd = payload.get("cwd")
        reason = gate(_event(payload), cwd=str(cwd) if cwd else None)
    except Exception as exc:  # noqa: BLE001 - any failure must block, never fall open
        print(f"advisor read gate: blocked, {exc}", file=stderr)
        return BLOCK
    if reason:
        print(f"advisor read gate: {reason}", file=stderr)
        return BLOCK
    return 0


if __name__ == "__main__":
    sys.exit(run(stdin=sys.stdin, stderr=sys.stderr))
