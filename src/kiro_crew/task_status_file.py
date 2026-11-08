"""The status file a Task Runner step declares, and what it says about the step.

A YAML workflow agent may name ``status_file``: a path, relative to the step's work
directory, where the step reports its own outcome as a JSON object. A step's turn
ending normally says only that the agent stopped talking, so when a step names one,
the executor reads it as soon as the turn lands (``task_executor.execute_task``). A
``"status"`` of ``"failed"`` fails the step, and so does a file that is missing,
unreadable, outside the work directory, too large or not a JSON object, because the
step said it would write it. Any other status, or none, lets the step go on to its
verification and review as before.

Nothing here runs a command or follows a path out of the work directory. The file is
read by the gateway, never by the step, so the read goes through the guarded reader
(``hooks.safe_read_file_bytes_nolink``): a sensitive path, a hardlinked inode, a FIFO
or another non-regular file, and a descriptor that lands outside the work directory
are refused unread, and a refusal fails the step.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from kiro_crew import hooks
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.task_models import Project, Task, TaskStatus

#: Largest status file read; a larger one fails the step unread.
STATUS_FILE_MAX_BYTES = 1024 * 1024
#: The value of ``"status"`` that fails the step (compared case-insensitively).
FAILED_STATUS = "failed"
#: Keys whose text is quoted as the reason a failed step gives.
_REASON_KEYS = ("reason", "error", "message")
_REASON_MAX = 300
_PATH_MAX = 512


def validate_status_file(name: str, value: object) -> str:
    """*value* as a ``status_file`` for agent *name*, or ``ValueError``.

    A non-empty relative path with no ``..`` part, on either path flavour, so a
    spec cannot point the check at a file outside the step's work directory.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Agent '{name}' status_file must be a non-empty string")
    if len(value) > _PATH_MAX or "\x00" in value:
        raise ValueError(f"Agent '{name}' status_file is not a usable path")
    for flavour in (PurePosixPath(value), PureWindowsPath(value)):
        if flavour.is_absolute() or flavour.anchor or ".." in flavour.parts:
            raise ValueError(
                f"Agent '{name}' status_file must be a path inside the step's work directory"
            )
    return value


def _shown(text: str) -> str:
    text, _ = redact_exfiltration_urls(" ".join(text.split()))
    text, _ = redact_credentials(text)
    return text[:_REASON_MAX]


def declared_failure(work_dir: Path, status_file: str) -> str:
    """Why the step's declared status file fails it, or ``""`` when it does not.

    Blocking file I/O: call it off the event loop.
    """
    shown_name = _shown(status_file)
    try:
        # realpath, not Path.resolve(): a link loop then reaches the guarded reader,
        # which refuses it, instead of raising RuntimeError (Python before 3.13).
        root = Path(os.path.realpath(work_dir))
        path = Path(os.path.realpath(root / status_file))
        if not path.is_relative_to(root):
            return f"Status file {shown_name} is outside the step's work directory"
        if not os.path.lexists(path):
            return f"The step did not write its status file {shown_name}"
        data = hooks.safe_read_file_bytes_nolink(
            str(path), within_root=str(root), max_bytes=STATUS_FILE_MAX_BYTES
        )
    except hooks.FileTooLargeError:
        return f"Status file {shown_name} is larger than {STATUS_FILE_MAX_BYTES} bytes"
    except OSError as exc:
        return f"Status file {shown_name} could not be read ({type(exc).__name__})"
    if data is None:
        return f"Status file {shown_name} could not be read safely"
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return f"Status file {shown_name} is not valid JSON"
    if not isinstance(doc, dict):
        return f"Status file {shown_name} is not a JSON object"
    status = doc.get("status")
    if not isinstance(status, str) or status.strip().lower() != FAILED_STATUS:
        return ""
    reason = next((doc[k] for k in _REASON_KEYS if isinstance(doc.get(k), str) and doc[k]), "")
    detail = f": {_shown(reason)}" if reason else ""
    return f"The step reported failure in {shown_name}{detail}"


async def fail_on_declared_failure(run: Project, task: Task, work_dir: Path) -> bool:
    """Fail *task* and its run when its declared status file fails it; whether it did.

    A failure the step declared is not retried and not re-planned around: the run
    is ``failed`` (``_try_replan`` plans nothing for a failed run), so its
    dependent steps never start and a resume runs the step again.
    """
    failure = await asyncio.to_thread(declared_failure, work_dir, task.status_file)
    if not failure:
        return False
    task.error = failure
    task.status = TaskStatus.FAILED
    if run.status == "running":
        run.status = "failed"
        run.error = f"Task {task.index} failed: {failure}"
    return True
