"""Whether a Task Runner step's final output says it is waiting on someone.

A step's turn ending normally says only that the agent stopped talking. A step
that ends with an ``[OPTIONS: ...]`` trailer, or whose last paragraph says it is
blocked, incomplete or stopping, has not done its work: no user reads the turn
live, so the question would go unanswered and the step would pass. The executor
reads the result here (``task_executor.execute_single_task``) and pauses the run
instead, with the question as the run's error.

Only the last paragraph is read for the declared phrases, so a step that reports
on something else being blocked earlier in its answer still passes.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

from kiro_crew import git_coord
from kiro_crew.messaging.renderer import split_options_trailer
from kiro_crew.task_models import Project, Task, TaskStatus

logger = logging.getLogger(__name__)

_ASK_MAX = 300
# Each phrase is an affirmative, present-tense statement about this step: the
# step itself is incomplete or blocked, or the agent says it is stopping or
# cannot go on.
_DECLARED_STOP_RE = re.compile(
    r"\b(?:this |the )?(?:step|task)(?: \d+)? (?:is|remains) (?:still )?"
    r"(?:incomplete|blocked)\b"
    r"|\bI(?:'m|’m| am) stopping(?: here\b|$)"
    r"|\bI (?:can't|can’t|cannot) (?:finish|complete|proceed|continue)\b",
    re.IGNORECASE,
)
# Right after the declaration, one of these makes the declaration itself
# hypothetical ("the step is incomplete only if the check fails").
_HYPOTHETICAL_RE = re.compile(r"^\W*(?:only\s+)?(?:if|when|whenever)\b", re.IGNORECASE)
# In the words set against the declaration by a contrast word, only a statement
# that this step is done, or that the blocker itself is resolved, resolves it ("but
# the new one worked and the step is done", "but that is now resolved"). Other work
# succeeding ("but local tests passed") resolves nothing, and neither does a
# numbered step, which may be another one ("but task 3 is complete").
_RESOLVED_RE = re.compile(
    r"\b(?:(?:the|this) (?:step|task) (?:is|was|has been) (?:now )?"
    r"(?:done|complete|completed|finished)"
    r"|(?:that|it) (?:is|was|has been) (?:now )?(?:fixed|resolved))\b",
    re.IGNORECASE,
)
# A negation anywhere before that statement ("but the token has not been fixed")
# means it resolves nothing.
_NEGATION_RE = re.compile(r"\b(?:not|never|no|nothing)\b|n['’]t\b", re.IGNORECASE)
# Before the declaration only a condition makes it hypothetical ("if verification
# fails, this step is blocked"); a success reported there ("tests passed and this
# step is blocked") is other work, not a resolution of this blocker.
_CONDITION_RE = re.compile(r"\b(?:if|unless|when|whenever|once|otherwise)\b", re.IGNORECASE)
# After the declaration, one of these names what the step still waits on ("blocked
# until the permissions are fixed", "but I can finish once you approve"): the
# words that follow it are the prerequisite, not a resolution.
_PREREQUISITE_RE = re.compile(r"\b(?:until|unless|pending|once)\b", re.IGNORECASE)
# Right after a resolution, one of these says it has not happened yet ("the task
# is complete only after you approve").
_UNMET_RE = re.compile(
    r"^\W*(?:only|if|when|whenever|after|once|until|unless|pending)\b", re.IGNORECASE
)
# Before the declaration, one of these sets earlier work against it ("tests
# passed, but this step is blocked"): only the words after it qualify the match.
# After the declaration, only the words after one can resolve it.
_CONTRAST_RE = re.compile(r"\b(?:but|however|yet)\b", re.IGNORECASE)


def _last_paragraph(text: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return paragraphs[-1] if paragraphs else ""


def _declares_stop(paragraph: str) -> bool:
    for clause in re.split(r"[.;!?]+", paragraph):
        # Every declaration is judged with its own qualifiers: a dismissed first
        # one ("I can't proceed with the old API, but ...") hides no later one.
        for match in _DECLARED_STOP_RE.finditer(clause.strip()):
            head = _CONTRAST_RE.split(match.string[: match.start()])[-1]
            tail = match.string[match.end() :]
            if _CONDITION_RE.search(head) or _HYPOTHETICAL_RE.search(tail):
                continue
            if not _resolved(tail):
                return True
    return False


def _resolved(tail: str) -> bool:
    head = _PREREQUISITE_RE.split(tail, 1)[0]
    contrasted = _CONTRAST_RE.split(head, 1)
    if len(contrasted) != 2:
        return False
    match = _RESOLVED_RE.search(contrasted[1])
    if match is None:
        return False
    # A resolution on a condition, before it or after it ("if you approve, the
    # task is complete", "the task is complete only after you approve"), has not
    # happened yet.
    before = contrasted[1][: match.start()]
    after = tail[len(head) - len(contrasted[1]) + match.end() :]
    return not (
        _NEGATION_RE.search(before) or _CONDITION_RE.search(before) or _UNMET_RE.search(after)
    )


def step_ask(result: str) -> str:
    """The question *result* leaves for the user, or ``""`` when it asks none."""
    body, choices = split_options_trailer(result or "")
    last = " ".join(_last_paragraph(body).split())
    if choices:
        ask = f"{last} [{' | '.join(choices)}]" if last else " | ".join(choices)
    elif _declares_stop(last):
        ask = last
    else:
        return ""
    return ask[:_ASK_MAX]


async def pause_on_ask(run: Project, task: Task, on_notify: Callable[..., Any]) -> bool:
    """Pause *run* when *task*'s result asks a question; ``True`` when it did.

    Called after every successful turn of the step, the review's retry included,
    so no attempt that stopped on a question reaches ``PASSED``.
    """
    ask = step_ask(task.result)
    if not ask:
        return False
    if run.branch_name:
        # The run's finalizer force-removes the worktree, and Resume recreates it
        # from the branch: the step's edits so far live on only as a commit.
        try:
            await git_coord.commit_step(run, task)
        except Exception as exc:
            logger.warning("Git commit before pausing task %d failed", task.index, exc_info=True)
            await _fail_keeping_worktree(run, task, ask, exc, on_notify)
            return True
    # Paused as a denied approval does, so its dependents never start on its
    # missing work.
    task.status = TaskStatus.PENDING
    task.error = f"Waiting on input: {ask}"
    run.status = "paused"
    run.error = f"Task {task.index} is waiting on input — paused: {ask}"
    await on_notify(
        f"Task {task.index} needs input",
        f"{task.title}\n{ask}\nEdit the task with the answer, then Resume.",
        run=run,
    )
    task.finished_at = time.time()
    return True


async def _fail_keeping_worktree(
    run: Project, task: Task, ask: str, exc: Exception, on_notify: Callable[..., Any]
) -> None:
    """Fail *run* with *task*'s uncommitted edits left on disk for the person.

    A resume would replace the worktree, so the run cannot offer one. Clearing
    ``worktree_path`` keeps ``git_coord.finalize`` from removing the directory;
    clearing ``work_dir`` too makes ``git_coord.workspace_is_valid`` refuse it, so
    a retry goes to recovery, which refuses the occupied path instead of reusing it.
    """
    kept = run.worktree_path
    run.worktree_path = run.work_dir = ""
    task.status = TaskStatus.FAILED
    task.error = (
        f"Waiting on input: {ask}. Its edits could not be committed ({exc}); "
        f"they are kept, uncommitted, in {kept}"
    )
    run.status = "failed"
    run.error = f"Task {task.index} failed: {task.error}"
    await on_notify(
        f"Error: Task {task.index}/{len(run.tasks)} failed",
        f"{task.title}\n{task.error}",
        run=run,
    )
    task.finished_at = time.time()
