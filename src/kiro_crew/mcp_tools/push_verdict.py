"""Push-verdict tool — the agent PRESENTS a request; the gateway decides and records.

``schemas()`` returns the advertisement half; ``HANDLERS`` maps names to behavior. Same
template as ``ledger.py``, including reaching shared plumbing as ``mcp_core`` attributes so
tests that rebind them still intercept the handler.

This module deliberately writes NOTHING. It does not run git, does not read a ref, and does
not compute a verdict: it posts to ``/api/push-verdict/run`` and renders what the gateway
answered. That is the whole point of the design — the party a gate constrains cannot also be
the party that records the gate's result, so the check and the record both live in the
gateway process and the agent's side is a presenter.

The tool carries NO worktree argument for the same reason ``ledger.py`` carries no slot
argument: the backend resolves the target from the CALLING SESSION's own project directory.
A worktree parameter would let a session ask about a clean tree and then publish from a
different one, and the verdict would be true about the wrong repository.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kiro_crew import mcp_core


def schemas() -> list[dict[str, Any]]:
    """Descriptor for the push-verdict tool."""
    return [
        {
            "name": "push_verdict_run",
            "description": (
                "Ask the gateway to run the prepare-pr pre-push guard on THIS session's "
                "worktree and record the result. Call it immediately before publishing a "
                "branch: on an installation where an operator enabled push-verdict "
                "gating, a push with no recorded verdict is REFUSED, and so is a command "
                "that both publishes and moves HEAD (commit, amend, rebase, reset, "
                "cherry-pick, fetch) because the commit it would publish is not the one "
                "any verdict describes. The gateway runs the check itself and owns the "
                "result — nothing you can write decides the outcome, so calling this is "
                "the only way to obtain a verdict. Takes no arguments: which worktree is "
                "judged comes from this session's project directory, not from you. "
                "Re-running after a commit, rebase or fetch is expected, since each of "
                "those invalidates the previous verdict. Answers REFUSED without recording "
                "anything when the guard rejects the branch, most often a base that has "
                "moved: rebase or merge, then ask again. A publish that redirects git "
                "elsewhere (-C, --git-dir, --work-tree, or a directory change) is refused "
                "too, because a verdict describes one repository."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        }
    ]


def _strict_session_key() -> tuple[str, str]:
    """Resolve the calling session strictly, refusing PID-walked identities.

    Returns ``(key, "")`` or ``("", error)``. The lenient default resolver includes a
    ``/proc`` ancestor walk, and a subagent lives under its parent slot's process tree — the
    walk would silently resolve to the PARENT session. For this tool that is not a
    disclosure risk but an authorization one: a subagent would obtain, or consume, its
    parent's push verdict, and a verdict is exactly the thing that must belong to one
    session only. The verified key is passed explicitly to the transport so the value that
    was checked is the value that is used.
    """
    return mcp_core.require_strict_session_key(
        "Error: this session's identity could not be verified strictly, so no push "
        "verdict can be recorded for it. Subagents inherit no session identity of their "
        "own — run the guard and publish from the parent session instead."
    )


def push_verdict_run(name: str, args: dict[str, Any]) -> str:
    sk, err = _strict_session_key()
    if err:
        return err

    # No payload: every field of the record comes from the gateway's own run. An empty body
    # is the honest shape of "please judge my session", and it is also the shape that cannot
    # carry a value the store would trust.
    d = mcp_core._post("/api/push-verdict/run", {}, session_key=sk)

    api_err = d.get("error")
    if api_err:
        return f"Error: {api_err}"

    # The route's FOUR exits, spelled the way the ROUTE spells them. These names are a
    # contract between the two halves, and the only thing that keeps a real refusal from
    # rendering as "unrecognised verdict" to the one reader who needs to act on it.
    verdict = d.get("verdict") or ""
    base = d.get("base") or "the base"
    detail = d.get("detail") or ""
    if verdict == "refused":
        return f"REFUSED by the push guard against {base}; no verdict was recorded.\n{detail}"
    if verdict == "error":
        return (
            f"The push guard could not complete against {base}, so no verdict was recorded "
            f"and publishing stays refused.\n{detail}"
        )
    if verdict == "not_activated":
        # The route's FOURTH exit. Without this branch the fall-through below rendered an
        # ordinary, correct answer as "unrecognised verdict", which reads as a product fault
        # on every installation that has not turned this gate on -- which is most of them.
        return (
            "push-verdict gating is not activated on this installation, so nothing was "
            "recorded and publishing is not gated on a verdict. An operator activates it "
            f"out of band.\n{detail}"
        )
    if verdict != "ok":
        # An unrecognised verdict is reported rather than treated as a pass: this handler
        # does not get to decide what counts as one.
        return f"Error: the gateway returned an unrecognised verdict ({verdict!r})"

    head = (d.get("head") or "")[:12]
    return (
        f"Verdict recorded for this session: head={head} base={base}. "
        "It is dropped if you commit, amend, rebase, reset, cherry-pick or fetch before "
        "publishing — run this again after any of those."
    )


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "push_verdict_run": push_verdict_run,
}
