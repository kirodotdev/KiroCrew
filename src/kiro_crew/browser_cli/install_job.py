"""The gateway-owned record of one browser install job.

A dashboard page cannot tell which install is running from its own mutation
state: a refreshed page or a second tab never saw the request. So the gateway
keeps one :class:`BrowserInstallJob` per install, published before any work
starts, and every status read reports it. The record is plain in-memory state
on the event loop: a gateway restart starts with no job, which is what keeps a
stale "running" from surviving one.

Mutation happens on the event loop only. The installer runs in a worker thread
and reports stages through a callback that the handler marshals back with
``loop.call_soon_threadsafe``; every such update names the job id it was issued
for, and :meth:`BrowserInstallJob.apply_stage` ignores an id that is not its
own, so a late callback from a finished job cannot move a newer one.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kiro_crew.browser_cli import install as browser_cli_install

KIND_CLI_SETUP = "cli_setup"
KIND_ENGINE_DOWNLOAD = "engine_download"

STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"

STAGE_PREPARING = "preparing"
STAGES = (
    STAGE_PREPARING,
    browser_cli_install.STAGE_INSTALLING_CLI,
    browser_cli_install.STAGE_DOWNLOADING_BROWSER,
    browser_cli_install.STAGE_INSTALLING_SKILLS,
    browser_cli_install.STAGE_FINISHING,
)

ERROR_STEP_FAILED = "step_failed"
ERROR_TIMEOUT = "timeout"
ERROR_EXCEPTION = "exception"
ERROR_INTERRUPTED = "interrupted"

#: The panel renders the detail verbatim, so it is bounded the same way the
#: ``last_error`` string always was.
ERROR_DETAIL_CAP = 2000

#: Wall clock for every timestamp; a module attribute so tests can pin it.
clock = time.time


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def bounded_detail(text: str, limit: int = ERROR_DETAIL_CAP, *, keep_tail: bool = False) -> str:
    """Redact the FULL text, then truncate to *limit*.

    Any pre-redaction cut can split a credential so its anchor is gone and no
    pattern matches; the fragment then lands inside the displayed window.
    *keep_tail* keeps the END instead of the start: an installer prints the part
    worth reading (Playwright's missing-library list) last.
    """
    if limit <= 0:
        return ""
    redacted = browser_cli_install.redact_install_output(text)
    return redacted[-limit:] if keep_tail else redacted[:limit]


@dataclass
class BrowserInstallJob:
    """One install operation: what it targets, where it is, how it ended."""

    id: str
    kind: str
    engine: str | None
    status: str
    stage: str
    started_at: float
    updated_at: float
    finished_at: float | None = None
    error_code: str | None = None
    error_detail: str | None = None

    @classmethod
    def start(cls, kind: str, engine: str | None = None) -> BrowserInstallJob:
        """A new running job at ``preparing``; publish it before starting work."""
        now = clock()
        return cls(
            id=uuid.uuid4().hex,
            kind=kind,
            engine=engine,
            status=STATUS_RUNNING,
            stage=STAGE_PREPARING,
            started_at=now,
            updated_at=now,
        )

    @property
    def running(self) -> bool:
        return self.status == STATUS_RUNNING

    def apply_stage(self, job_id: str, stage: str) -> bool:
        """Move to *stage* if *job_id* is this job and it still runs."""
        if job_id != self.id or not self.running or stage not in STAGES:
            return False
        self.stage = stage
        self.updated_at = clock()
        return True

    def finish(
        self,
        job_id: str,
        status: str,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> bool:
        """Record the terminal outcome once; a stale id or second call is ignored."""
        if job_id != self.id or not self.running:
            return False
        now = clock()
        self.status = status
        self.error_code = error_code
        self.error_detail = error_detail
        self.finished_at = now
        self.updated_at = now
        return True

    def snapshot(self) -> dict[str, Any]:
        """The JSON shape served to the dashboard, with ``elapsed_s`` computed now."""
        end = self.finished_at if self.finished_at is not None else clock()
        return {
            "id": self.id,
            "kind": self.kind,
            "engine": self.engine,
            "status": self.status,
            "stage": self.stage,
            "started_at": _iso(self.started_at),
            "updated_at": _iso(self.updated_at),
            "finished_at": _iso(self.finished_at) if self.finished_at is not None else None,
            "elapsed_s": round(max(0.0, end - self.started_at), 1),
            "error_code": self.error_code,
            "error_detail": self.error_detail,
        }


def outcome_of(result: dict[str, Any], default_step: str) -> tuple[str, str | None, str | None]:
    """``(status, error_code, error_detail)`` for an installer *result*.

    The LAST step decides, never the first failed one: the installer returns
    ``ok`` from its last step, and every earlier gate returns on a real failure,
    so the last step is the one that decided the outcome and carries its remedy.
    Its return code separates a timeout and an interruption from an ordinary
    step failure.
    """
    steps = result.get("steps") or []
    if result.get("ok"):
        return STATUS_SUCCEEDED, None, None
    if not steps:
        return STATUS_FAILED, ERROR_STEP_FAILED, bounded_detail(f"{default_step}: failed")
    last = steps[-1]
    # `stderr`, not only `error`: install steps carry `stderr`, and the npm or
    # download output in it is what tells a registry auth error apart from a
    # blocked download. Redacted again here because the `error` fallback never
    # passed through the installer's own scrub.
    raw = last.get("stderr") or last.get("error") or "failed"
    raw_detail = str(raw).strip()
    hint = bounded_detail(str(last.get("hint") or "").strip())
    if hint and raw_detail.endswith(hint):
        raw_detail = raw_detail[: -len(hint)].rstrip()

    name = bounded_detail(f"{last.get('name', default_step)}: ")
    if hint:
        hint = hint[-ERROR_DETAIL_CAP:]
        body_limit = max(0, ERROR_DETAIL_CAP - len(name) - len(hint) - 2)
        detail = f"{name}{bounded_detail(raw_detail, body_limit, keep_tail=True)}\n\n{hint}"
        detail = detail[-ERROR_DETAIL_CAP:]
    else:
        detail = bounded_detail(f"{name}{raw_detail}")
    returncode = last.get("returncode")
    if returncode == browser_cli_install.INTERRUPTED_RC:
        return STATUS_INTERRUPTED, ERROR_INTERRUPTED, detail
    # install._run reports an expired subprocess budget as TIMEOUT_RC.
    if returncode == browser_cli_install.TIMEOUT_RC:
        return STATUS_FAILED, ERROR_TIMEOUT, detail
    return STATUS_FAILED, ERROR_STEP_FAILED, detail
