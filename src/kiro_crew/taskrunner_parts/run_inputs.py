"""What a task run is started from: the inputs ``TaskRunner`` accepts before any step runs.

The ``workspace_dir`` gate (canonicalised, sensitive locations refused, both outcomes
SEL-audited), the spec read gate that every spec read shares (``_read_spec_text``,
``_read_spec_prefix``), the audited YAML decomposer and which sources may not fall back from
it, and the live ``taskrunner.*`` config each run entry adopts (``_refresh_from_config``,
the host-safe parallel cap). A new check on what a run may read, or where it may run, goes
here.

Instance fields written: ``_max_parallel_steps``, ``_work_dir``, ``_workspace_dir``.

Instance fields read: ``_config_baseline``, ``_ctor_max_parallel_steps``,
``_ctor_work_dir``, ``_ctor_workspace_dir``.
"""

from __future__ import annotations

import codecs
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.hooks import safe_read_file_bytes_nolink, validate_file_path
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel
from kiro_crew.subagent import compute_memory_sized_parallel_cap
from kiro_crew.task_models import Task
from kiro_crew.task_planner import decompose_yaml

if TYPE_CHECKING:
    from kiro_crew.taskrunner import TaskRunner


# The facade's logger: records keep the name ``kiro_crew.taskrunner``.
logger = logging.getLogger("kiro_crew.taskrunner")


_MAX_PARALLEL_TASKS = (
    3  # ctor fallback when the memory-sized cap fails; live cap is self._max_parallel_steps
)


# Sources with no operator watching the run: an invalid workflow spec must fail
# with the SEL denial rather than degrade to the LLM decomposer. Attended sources
# (chat, dashboard, CLI/unsourced) keep the fallback.
_UNATTENDED_SOURCES = frozenset({"cron", "mcp"})


def _resolve_workspace_dir(raw: str) -> str:
    """Canonicalize a user-supplied workspace_dir and reject sensitive paths.

    Expands ``~`` and resolves symlinks + ``..`` traversal BEFORE the
    ``is_sensitive_path`` check, so a value like ``/tmp/../../home/user/.aws``
    or a symlink pointing at ``~/.ssh`` cannot slip past by presenting a
    non-sensitive-looking spelling. Returns the resolved absolute path, or an
    empty string when ``raw`` is blank. Raises ``ValueError`` for
    sensitive/credential locations.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    resolved = str(Path(raw).expanduser().resolve())
    if is_sensitive_path(resolved):
        # Security-relevant permission decision — audit before rejecting so a
        # probe for workspace_dir bypass vectors leaves a trace in the SEL log.
        try:
            sel().log_tool_invocation(
                session_key="taskrunner",
                source="taskrunner",
                tool_name="workspace_dir_validate",
                outcome="denied",
                metadata={"raw": raw, "resolved": resolved, "reason": "sensitive_path"},
            )
        except Exception:
            logger.debug("SEL audit for workspace_dir rejection failed", exc_info=True)
        raise ValueError(
            "workspace_dir resolves to a sensitive/credential path and was " f"rejected: {raw!r}"
        )
    # Accepting a workspace_dir authorizes LLM-driven autonomous execution in
    # that directory — a permission decision, so audit the "allowed" outcome too
    # (mirrors the "denied" branch above). Audit failure must not block the run.
    try:
        sel().log_tool_invocation(
            session_key="taskrunner",
            source="taskrunner",
            tool_name="workspace_dir_validate",
            outcome="allowed",
            metadata={"raw": raw, "resolved": resolved},
        )
    except Exception:
        logger.debug("SEL audit for workspace_dir acceptance failed", exc_info=True)
    return resolved


def _read_spec_text(path: str, max_chars: int | None) -> str | None:
    """Read and normalize spec text through the descriptor gate.

    A caller may hold a *path* that passed ``hooks.validate_file_path``, but that
    judges the NAME; re-opening the name reads whatever inode the name points at
    by then. A hardlink alias shares its target's inode under an innocent name,
    so every name-based check passes while the bytes belong to the target. The
    read therefore goes through ``hooks.safe_read_file_bytes_nolink``: it opens
    FIRST (refusing a link at the final component), ``fstat``s that one
    descriptor and refuses ``st_nlink > 1``, a non-regular inode, and a sensitive
    or out-of-root real path, then reads that same descriptor. ``within_root`` is
    the spec's own directory, which also pins the opened inode on Windows where
    ``O_NOFOLLOW`` does not exist.

    ``max_chars`` asks for a bounded prefix, cut to fit. ``None`` asks for the
    whole spec, bounded by the gate's own ``hooks.MAX_FILE_BYTES``, where a file
    past that cap raises ``hooks.FileTooLargeError`` instead of yielding a silent
    prefix.

    Returns ``None`` for anything the gate refuses or cannot read. Every spec
    read shares this one function, so the gate holds at all of them: a read that
    skipped it would place the aliased target's bytes in the LLM prompt, the
    persisted run and the review context.
    """
    # ``within_root`` is derived from the CANONICAL path: a caller may hand over
    # ``~/specs/task.md`` or a path relative to the process directory, and the
    # root has to name the same directory the descriptor lands in.
    canonical = validate_file_path(path)
    if canonical is None:
        try:
            sel().log_tool_invocation(
                session_key="taskrunner",
                source="taskrunner",
                tool_name="spec_read_validate",
                outcome="denied",
                metadata={
                    "raw": path,
                    "reason": "name_validation_rejected",
                    "bounded": max_chars is not None,
                },
            )
        except Exception:
            logger.debug("SEL audit for spec name rejection failed", exc_info=True)
        return None
    read_limit: int | None
    if max_chars is None:
        read_limit = None
        allow_truncate = False
    else:
        # A UTF-8 code point is at most four bytes, so this many bytes always
        # holds at least ``max_chars`` characters; the bound stays in characters
        # below.
        read_limit = 4 * max_chars
        allow_truncate = True
    raw = safe_read_file_bytes_nolink(
        canonical,
        within_root=os.path.dirname(canonical),
        max_bytes=read_limit,
        allow_truncate=allow_truncate,
    )
    if raw is None:
        try:
            sel().log_tool_invocation(
                session_key="taskrunner",
                source="taskrunner",
                tool_name="spec_read_validate",
                outcome="denied",
                metadata={
                    "raw": path,
                    "resolved": canonical,
                    "reason": "descriptor_gate_rejected",
                    "bounded": max_chars is not None,
                },
            )
        except Exception:
            logger.debug("SEL audit for spec descriptor rejection failed", exc_info=True)
        return None
    try:
        sel().log_tool_invocation(
            session_key="taskrunner",
            source="taskrunner",
            tool_name="spec_read_validate",
            outcome="allowed",
            metadata={"raw": path, "resolved": canonical, "bounded": max_chars is not None},
        )
    except Exception:
        logger.debug("SEL audit for spec read acceptance failed", exc_info=True)
    # The decode is strict, as a text-mode read is: invalid UTF-8 raises, and a
    # bounded caller maps that to "". A truncating read returns at most
    # ``read_limit`` bytes without saying whether it cut, so a full-length result
    # is the one case that may end mid code point through no fault of the file;
    # there the tail is held back (``final=False``), which loses nothing — every
    # complete character before a cut at ``read_limit`` bytes lies at or past
    # index ``max_chars`` and is dropped by the bound below. Any other result is
    # the whole file and is finalized, so an incomplete sequence at EOF is the
    # malformed spec it is, not a silently shorter one.
    cut_possible = allow_truncate and len(raw) == read_limit
    text = codecs.getincrementaldecoder("utf-8")().decode(raw, final=not cut_possible)
    # Universal newlines, as a text-mode read normalizes them.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if max_chars is not None:
        text = text[:max_chars]
    return text.strip()


def _read_spec_prefix(path: str, max_chars: int) -> str:
    """Read a bounded spec prefix on a worker thread.

    Returns ``""`` for anything the gate refuses or cannot read — the same empty
    prefix the caller substitutes for an unreadable spec, so a refusal tells a
    caller nothing about whether a path is protected.
    """
    text = _read_spec_text(path, max_chars)
    return "" if text is None else text


def _decompose_yaml_with_audit(
    yaml_content: str,
    task_id: str,
    source: str = "",
    spec_name: str = "",
) -> list[Task]:
    """Decompose YAML with SEL audit logging.

    ``source``/``spec_name`` carry the run's provenance. Without them a
    cron/MCP-sourced denial is recorded against ``dashboard`` — the one surface
    that did not start the run — which makes the audit trail unusable for
    exactly the unattended callers it exists to record.
    """
    metadata: dict[str, Any] = {"task_id": task_id}
    if source:
        metadata["source"] = source
    if spec_name:
        metadata["spec_name"] = spec_name
    caller = source or "dashboard"
    try:
        tasks = decompose_yaml(yaml_content)
        sel().log_tool_invocation(
            session_key=caller,
            source="taskrunner",
            tool_name="decompose_yaml",
            outcome="ok",
            metadata={**metadata, "task_count": len(tasks)},
        )
        return tasks
    except Exception as exc:
        sel().log_tool_invocation(
            session_key=caller,
            source="taskrunner",
            tool_name="decompose_yaml",
            outcome="error",
            metadata={**metadata, "error": str(exc)},
        )
        raise


def _clamp_parallel_steps(requested: int | None, cfg: KiroCrewConfig | None) -> int:
    """Bound *requested* by the host-safe ceiling; ``0``/``None`` means the ceiling."""
    try:
        auto_cap = (
            compute_memory_sized_parallel_cap(cfg) if cfg is not None else _MAX_PARALLEL_TASKS
        )
    except Exception:
        auto_cap = _MAX_PARALLEL_TASKS
    auto_cap = max(1, auto_cap)
    if requested and requested >= 1:
        return min(int(requested), auto_cap)
    return auto_cap


def _refresh_from_config(self: TaskRunner) -> None:
    """Adopt ``taskrunner.max_parallel_steps`` / ``workspace_dir`` for the NEXT run.

    Called at each run entry so a write to ``config.json`` from any writer
    takes effect on the next run without a gateway restart, while a run that
    is already executing keeps the values it started with (its parallel cap
    and work dir are bound per run, not read from ``self`` mid-flight).

    Reads the watcher's last applied snapshot (a plain attribute read); before
    the watcher has primed there is none and the refresh is skipped, never
    replaced by a ``load()`` on the event loop. A field is adopted only when
    its config value MOVED since the baseline this runner was constructed
    against; an unchanged field keeps the constructor's argument.
    ``workspace_dir`` goes through the same sensitive-path validation the
    constructor applies.
    """
    if self._config_baseline is None:
        return
    # The snapshot is a plain attribute read. Before the watcher has primed
    # there is nothing to refresh from without a ``load()`` -- which parses
    # and validates the file on the event loop these entry points run on --
    # so the constructor's values stand until the first primed run.
    cfg = live.snapshot()
    if cfg is None:
        return
    base_steps, base_ws = self._config_baseline
    try:
        new_steps = int(cfg.taskrunner.max_parallel_steps)
        new_ws = str(cfg.taskrunner.workspace_dir)
    except (AttributeError, TypeError, ValueError):
        return
    if new_steps != base_steps:
        requested: int | None = new_steps
    else:
        requested = self._ctor_max_parallel_steps
    self._max_parallel_steps = self._clamp_parallel_steps(requested, cfg)
    if new_ws != base_ws:
        try:
            resolved = _resolve_workspace_dir(new_ws)
        except ValueError:
            # A rejected (sensitive) path keeps the current target; the
            # rejection is already SEL-audited by the validator.
            logger.warning("taskrunner.workspace_dir rejected on reload; keeping current target")
            return
    else:
        resolved = self._ctor_workspace_dir
    if resolved != self._workspace_dir:
        self._workspace_dir = resolved
        self._work_dir = Path(resolved) if resolved else self._ctor_work_dir
