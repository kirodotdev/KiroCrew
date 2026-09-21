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

import json
from collections.abc import Callable
from typing import Any

from kiro_crew import mcp_core

#: How long the presenter waits for the gateway's ``/api/push-verdict/run`` verdict.
#:
#: The route is not a quick judgement: within ONE request the gateway primes a cold mirror
#: (base fetch + candidate fetch, a guard timeout each), runs the guard (another guard
#: timeout), resolves the effective target, re-fetches the remote tip, and then PERFORMS THE
#: PUSH ITSELF (the push timeout) -- the gateway is the publisher, so the whole publish runs
#: inside this request. The server-side legs are bounded by the handler's own per-step
#: timeouts (``_GUARD_TIMEOUT_SECONDS`` on the fetches and the guard, ``_PUSH_TIMEOUT_SECONDS``
#: on the push, ``_GIT_TIMEOUT_SECONDS`` on the short reads); summed on the slowest path they
#: exceed 700s, so a presenter wait set near that bound expires DURING a legitimately slow
#: publish and ``_post``'s 30s client default expired long before any of it -- turning an
#: ordinary slow publish into a read-timeout the presenter rendered as a definite "Error:".
#: A transport failure on this route means the publish MAY STILL BE IN PROGRESS
#: (``_transport_failure``'s own contract forbids reading it as a rejection), so waiting for
#: the real verdict is the fix for the common case. Set with a clear margin ABOVE the summed
#: server call-chain bound so a legitimately slow publish is awaited to its real verdict, not
#: abandoned mid-publish.
_ROUTE_TIMEOUT_SECONDS = 1200


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
    #
    # An explicit timeout comfortably over the route's ~660s worst case (see
    # ``_ROUTE_TIMEOUT_SECONDS``): the gateway fetches three times and performs the push inside
    # this one request, so ``_post``'s 30s default would time out mid-publish and be rendered
    # as a definite failure while the publish was still running.
    d = mcp_core._post("/api/push-verdict/run", {}, session_key=sk, timeout=_ROUTE_TIMEOUT_SECONDS)

    # A TRANSPORT failure is checked BEFORE the ordinary-error render below, because it is not
    # a rejection. ``_post`` always sets ``mark_transport_error=True``, so a read-timeout or a
    # post-connect failure returns ``{"error": ..., "transport_error": True}`` -- and
    # ``_transport_failure``'s own contract says the request MAY have reached the gateway
    # before the response failed, so the caller must NOT present it as a definite rejection nor
    # retry on its own. Rendering it as "Error:" (as the code did) told the agent the publish
    # FAILED while the gateway may still have been pushing, so the agent might retry or act on a
    # false failure. Render it as INDETERMINATE instead: the publish may still be in progress,
    # so re-check the branch / re-run the verdict before assuming anything.
    if d.get("transport_error"):
        return (
            "INDETERMINATE: the request to the push-verdict gateway did not return a definite "
            "result, so the publish may still be in progress or may have completed -- do NOT "
            "assume it failed and do NOT immediately retry. The gateway performs the push "
            "itself and this call can legitimately take several minutes. Re-check whether the "
            "branch is already on the remote (or re-run push_verdict_run) before acting.\n"
            f"{d.get('error') or ''}"
        )

    # A ``not_published`` outcome is a JUDGED, complete answer, but the route returns it at a
    # non-2xx status (409 when HEAD/the destination moved, 500 when the push itself failed) --
    # a deliberate contract, since a moved tree IS an HTTP conflict. ``mcp_core._http_error_body``
    # flattens any non-2xx JSON that has no ``"error"`` key into ``{"error": <raw JSON text>}``,
    # so without this the ``api_err`` check below would fire first and the agent would see a raw
    # JSON blob instead of the actionable retry guidance. Recover the structured body here: if
    # the error text parses to a dict carrying our ``verdict``, promote its fields onto ``d`` so
    # the ``verdict`` branches below render, and clear the flattened ``error`` so ``api_err`` does
    # not pre-empt them. Only ``not_published`` is promoted; a genuine error body is left alone.
    if not d.get("verdict") and isinstance(d.get("error"), str):
        try:
            _body = json.loads(d["error"])
        except (ValueError, TypeError):
            _body = None
        if isinstance(_body, dict) and _body.get("verdict") == "not_published":
            d = {**d, **_body, "error": None}

    api_err = d.get("error")
    if api_err:
        return f"Error: {api_err}"

    # The route's exits, spelled the way the ROUTE spells them. These names are a
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
        # The route's exit on an installation that has not turned this gate on. Without this
        # branch the fall-through below rendered an ordinary, correct answer as "unrecognised
        # verdict", which reads as a product fault on every installation that has not turned
        # this gate on -- which is most of them.
        return (
            "push-verdict gating is not activated on this installation, so nothing was "
            "recorded and publishing is not gated on a verdict. An operator activates it "
            f"out of band.\n{detail}"
        )
    if verdict == "not_published":
        # The operation judged the branch but the push did NOT land: HEAD or the destination
        # moved out from under the judgement (409), or the push itself failed (500). No
        # receipt survives, so a later publish stays refused. Report it as a refusal, not a
        # pass -- the gateway performs the push, so "not published" is the failure the agent
        # must act on.
        return (
            f"NOT PUBLISHED: the gateway judged the branch against {base} but the push did "
            f"not land, so no verdict survives and publishing stays refused. Re-run after "
            f"resolving the cause.\n{detail}"
        )
    if verdict != "published":
        # An unrecognised verdict is reported rather than treated as a pass: this handler
        # does not get to decide what counts as one.
        return f"Error: the gateway returned an unrecognised verdict ({verdict!r})"

    # verdict == "published": the gateway ran the guard, recorded what it observed, and
    # performed the push itself. There is no separate agent push to make -- the branch is
    # already on the remote.
    head = (d.get("head") or "")[:12]
    return (
        f"Published by the gateway: head={head} base={base}. The gateway judged the "
        "stale-base guard and performed the push itself, so the branch is already on the "
        "remote -- do not run a separate git push."
    )


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "push_verdict_run": push_verdict_run,
}
