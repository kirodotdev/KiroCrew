"""Readers for the ``_session/steering`` request: whether it is advertised, and its answer.

``acp/types.py`` re-exports both, which is the import path callers use.
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
