"""Readers for the steer requests: ``_session/steering`` (whether it is advertised, and
its answer) and the goose frames its own steer request depends on.

``acp/types.py`` re-exports them, which is the import path callers use.
"""

from __future__ import annotations


def steering_outcome(result: object) -> str:
    """The ``outcome`` of a ``_session/steering`` answer; "" for any other shape.

    The answer is adapter-authored JSON: a result that is not an object is read as
    no outcome (undelivered) rather than trusted to have ``.get``.
    """
    if not isinstance(result, dict):
        return ""
    outcome = result.get("outcome")
    return outcome if isinstance(outcome, str) else ""


def steering_advertised(init_resp: object) -> bool:
    """Whether an ``initialize`` result advertises the ``_session/steering`` request.

    The extension is advertised at the TOP level of the result, as
    ``_meta.steering.supported``, a sibling of ``agentCapabilities``. Only a
    literal ``true`` counts.
    """
    meta = init_resp.get("_meta") if isinstance(init_resp, dict) else None
    steering = meta.get("steering") if isinstance(meta, dict) else None
    return isinstance(steering, dict) and steering.get("supported") is True


def goose_announced_run(params: object) -> tuple[bool, str | None]:
    """``(announced, run_id)`` for a goose ``session/update`` *params*.

    goose sends ``session_info_update`` with ``_meta.goose.activeRunId`` set when a
    prompt starts and null when it ends; its ``_goose/unstable/session/steer`` must
    name that id. ``announced`` is False for every other frame, so the caller keeps
    what it holds; True with ``None`` means the run ended. The one reader of this
    frame shape, for ``AcpClient`` and the shared session handle alike.
    """
    update = params.get("update") if isinstance(params, dict) else None
    if not isinstance(update, dict) or update.get("sessionUpdate") != "session_info_update":
        return False, None
    meta = update.get("_meta")
    goose = meta.get("goose") if isinstance(meta, dict) else None
    if not isinstance(goose, dict) or "activeRunId" not in goose:
        return False, None
    run_id = goose.get("activeRunId")
    return True, run_id if isinstance(run_id, str) and run_id else None


def goose_steer_echo_id(params: object) -> str:
    """The ``messageId`` goose's echo of a drained steer carries, or ``""``.

    goose echoes each steer it drains into the running turn as a
    ``user_message_chunk`` marked ``_meta.goose.steer`` with the ``messageId`` its
    steer answer returned. Any other frame answers ``""``. The one reader of this
    frame shape, for ``AcpClient`` and the shared session handle alike.
    """
    update = params.get("update") if isinstance(params, dict) else None
    if not isinstance(update, dict) or update.get("sessionUpdate") != "user_message_chunk":
        return ""
    meta = update.get("_meta")
    goose = meta.get("goose") if isinstance(meta, dict) else None
    if not isinstance(goose, dict) or goose.get("steer") is not True:
        return ""
    return str(goose.get("messageId") or "")
