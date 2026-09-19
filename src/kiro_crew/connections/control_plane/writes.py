"""W01 · L07: the replay gate for a non-idempotent connector write.

A non-idempotent write (POST-creating a resource, sending a message) that is
issued and then leaves its caller with an UNCERTAIN outcome -- a timeout, a
dropped connection, a 5xx with no usable body -- is the dangerous case this
module exists for. Blindly retrying such a write can DO IT TWICE: two issues
created, two messages sent. The core invariant is:

    When a non-idempotent write's outcome is ``unknown``, a replay MUST be
    refused unless there is evidence the prior attempt did NOT take effect (or
    its recorded result can be reused).

This is pure decision logic over a recorded attempt, zero IO. It does not issue
the write, hold a client, or resolve a credential -- it decides, given what is
KNOWN about a prior attempt, whether replaying it now is allowed. The three
outcome states are deliberately distinct from both the success envelope
(:mod:`kiro_crew.connections.control_plane.result`) and the RUN-01 error
taxonomy (:mod:`kiro_crew.connections.control_plane.errors`): those classify
what a call RETURNED, this records what is known about whether a call's effect
LANDED.

The three attempt outcomes
--------------------------
- ``succeeded`` -- the write is known to have taken effect. A replay would
  duplicate it, so the gate refuses to reissue and instead REUSES the recorded
  result (``reuse``). This is why an attempt record carries an optional
  ``recorded_result``: a known-succeeded write has a result to hand back rather
  than redo.
- ``failed_not_applied`` -- the write is known NOT to have taken effect (a
  provider rejection that never mutated state, a pre-flight failure). A replay
  is safe and ALLOWED: reissuing cannot duplicate an effect that never landed.
- ``unknown`` -- the write was issued and the outcome is uncertain. This is the
  case blind retry gets wrong. For a NON-idempotent operation the gate REFUSES
  the replay with a typed rejection, because reissuing might do the effect a
  second time and nothing here proves it didn't.

The idempotent-write exception
------------------------------
Whether an ``unknown`` outcome is safe to replay is decided ONLY by an explicit,
trusted idempotency assertion the caller places on the attempt record
(``idempotent=True``) -- a PUT-to-a-fixed-key upsert, a create carrying a
provider-honored idempotency token the caller vouches for. When set, an
``unknown`` replay is allowed; otherwise it is refused.

Idempotency is NEVER inferred from the descriptor's ``effect``. In particular
``effect=delete`` is NOT treated as idempotent: a second ``delete`` can land on
a resource that was RECREATED in the interim (deleting someone else's new
resource), and a given API's delete may itself not be idempotent -- so
``effect`` cannot stand in for a trusted idempotency guarantee. This keeps the
safe default (refuse) and makes the exception something a caller must explicitly
state, not something the gate guesses from an effect label.

Attribution: a record must belong to THIS request
--------------------------------------------------
An attempt record carries an identity -- ``operation_id`` plus an
``args_fingerprint`` and an ``idempotency_key`` -- and the gate CHECKS that
identity before honoring any outcome. A record whose ``operation_id`` differs
from the descriptor's, or whose fingerprint/key differ from the request's own,
is refused: another operation's record, or another argument set's recorded
result, is never a basis to allow a replay or reuse a result. The stored
fingerprint and key are compared, not merely retained.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, TypedDict

from kiro_crew.connections.control_plane.errors import OperationError, operation_error
from kiro_crew.connections.control_plane.operation import OperationDescriptor
from kiro_crew.connections.control_plane.result import OperationResult

#: Bumped when this module's shapes change, mirroring the sibling modules'
#: module-level schema-version constant.
WRITES_SCHEMA_VERSION = 1

# --- attempt outcome: what is KNOWN about whether the write's effect landed --
#: The write is known to have taken effect; its result is recorded for reuse.
ATTEMPT_SUCCEEDED = "succeeded"
#: The write is known NOT to have taken effect; a replay is safe.
ATTEMPT_FAILED_NOT_APPLIED = "failed_not_applied"
#: The outcome is uncertain (timeout / dropped connection / bodyless 5xx). This
#: is the case blind retry gets wrong.
ATTEMPT_UNKNOWN = "unknown"

#: The three-value closed set of what is known about a prior attempt's effect.
#: Distinct from the success envelope's ``ResultStatus`` and the RUN-01
#: ``ErrorClass``: those classify what a call returned; this records whether the
#: effect landed.
AttemptOutcome = Literal["succeeded", "failed_not_applied", "unknown"]

#: Tuple form of :data:`AttemptOutcome`'s closed set.
ATTEMPT_OUTCOMES: tuple[AttemptOutcome, ...] = (
    "succeeded",
    "failed_not_applied",
    "unknown",
)

# --- replay decision: the gate's verdict -----------------------------------
#: Reissue the write: the prior attempt provably did not apply.
REPLAY_ALLOW: Literal["allow"] = "allow"
#: Do NOT reissue; hand back the prior attempt's recorded result instead.
REPLAY_REUSE: Literal["reuse"] = "reuse"
#: Refuse the replay: the outcome is uncertain and the operation is not
#: idempotent, so reissuing risks a duplicate effect.
REPLAY_REFUSE: Literal["refuse"] = "refuse"

#: The three-value closed set of the gate's verdict.
ReplayVerdict = Literal["allow", "reuse", "refuse"]

#: Tuple form of :data:`ReplayVerdict`'s closed set.
REPLAY_VERDICTS: tuple[ReplayVerdict, ...] = ("allow", "reuse", "refuse")


class AttemptRecord(TypedDict):
    """What is known about ONE non-idempotent-write attempt.

    Every field present, matching the sibling seam ``TypedDict``s' shape.

    Identity is the triple ``(operation_id, args_fingerprint, idempotency_key)``
    -- the operation issued, a fingerprint of the arguments it was issued with,
    and the caller-supplied idempotency key that ties a retry to its original.
    Two attempts with the same triple are the SAME logical write; that is what
    lets the gate recognize a retry as a replay of a known prior attempt.

    ``outcome`` is one of the :data:`AttemptOutcome` closed set -- what is known
    about whether the effect landed.

    ``recorded_result`` is the :class:`OperationResult` a ``succeeded`` attempt
    returned, so the gate can REUSE it rather than reissue; it is ``None`` for a
    ``failed_not_applied`` or ``unknown`` attempt, which have no result to hand
    back.

    ``idempotent`` is the caller's explicit assertion that this operation is
    safe to replay even after an ``unknown`` outcome (a fixed-key upsert, a
    provider-honored idempotency token). It is an OVERRIDE, defaulting to the
    non-idempotent-safe behavior; the gate never infers it.
    """

    operation_id: str
    args_fingerprint: str
    idempotency_key: str
    outcome: AttemptOutcome
    recorded_result: OperationResult | None
    idempotent: bool


class ReplayDecision(TypedDict):
    """The gate's verdict on whether a replay of a recorded attempt is allowed.

    Every field present, matching the sibling seam ``TypedDict``s' shape.

    ``verdict`` is one of the :data:`ReplayVerdict` closed set. ``reuse_result``
    carries the recorded result to hand back when ``verdict == "reuse"`` and is
    ``None`` otherwise. ``error`` carries the typed :class:`OperationError` when
    ``verdict == "refuse"`` and is ``None`` otherwise -- a refusal is always a
    typed rejection built through :func:`operation_error`, never a bare
    boolean.
    """

    verdict: ReplayVerdict
    reuse_result: OperationResult | None
    error: OperationError | None


def args_fingerprint(args: dict[str, Any]) -> str:
    """A stable content fingerprint of a write's arguments.

    Two calls issued with the same arguments produce the same fingerprint
    regardless of key order, so a retry fingerprints identically to its
    original. This is a plain content hash for identity/equality, NOT a
    security primitive and NOT a place credentials belong -- arguments are the
    resource shape being written, and a credential value never travels here (the
    seam's context carries a ``binding_ref``, never a token). The value is
    serialized with sorted keys so ordering does not change the digest.
    """

    encoded = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def record_attempt(
    *,
    operation_id: str,
    args_fingerprint: str,
    idempotency_key: str,
    outcome: AttemptOutcome,
    recorded_result: OperationResult | None = None,
    idempotent: bool = False,
) -> AttemptRecord:
    """Build an :class:`AttemptRecord` with every field present.

    A convenience constructor mirroring :func:`operation_error`'s shape, so a
    caller cannot accidentally omit a field. ``recorded_result`` defaults to
    ``None`` (only a ``succeeded`` attempt carries one) and ``idempotent``
    defaults to ``False`` (the safe, non-idempotent default -- the exception is
    something a caller must state).
    """

    return {
        "operation_id": operation_id,
        "args_fingerprint": args_fingerprint,
        "idempotency_key": idempotency_key,
        "outcome": outcome,
        "recorded_result": recorded_result,
        "idempotent": idempotent,
    }


def _is_replay_safe_after_unknown(record: AttemptRecord) -> bool:
    """Whether an ``unknown`` outcome is safe to replay for this operation.

    Safe ONLY when the caller explicitly, trustworthily asserted idempotency on
    the attempt record (``idempotent=True``) -- a fixed-key upsert or a
    provider-honored idempotency token the caller vouches for. Idempotency is
    NEVER inferred from the descriptor's ``effect``: an ``effect=delete`` second
    attempt can hit a resource RECREATED in the interim (deleting someone else's
    new resource), and a given API's delete may itself not be idempotent, so
    ``effect`` cannot stand in for a trusted idempotency guarantee. The safe
    default is that an ``unknown`` write is not replayable; the exception is
    something the caller must state, not something the gate presumes.
    """

    return record["idempotent"]


def replay_decision(
    descriptor: OperationDescriptor,
    record: AttemptRecord,
    *,
    request_args: dict[str, Any],
    request_idempotency_key: str,
) -> ReplayDecision:
    """Decide whether replaying the recorded attempt is allowed.

    The gate over the core invariant. ``descriptor`` identifies the operation
    THIS request is for; ``record`` is what is known about a prior attempt;
    ``request_args`` is THIS request's own argument mapping and
    ``request_idempotency_key`` its own idempotency key. All three identity
    facets are checked FIRST -- a record that does not belong to this request is
    refused before any outcome is honored.

    Attribution (step 1) is UNCONDITIONAL and UNSKIPPABLE. Both identity inputs
    are REQUIRED keyword arguments, and the argument fingerprint is computed
    HERE from ``request_args`` rather than accepted pre-computed -- so there is
    no way to invoke the gate that gets ``allow`` or ``reuse`` without the
    argument comparison actually running. A record is refused (typed rejection,
    never allow/reuse) when any of these differ:

    - ``record["operation_id"]`` != ``descriptor["operation_id"]`` -- a record
      for another operation.
    - ``record["args_fingerprint"]`` != ``fingerprint(request_args)`` -- a
      record for another argument set. This is the residual hole the earlier
      optional-parameter shape left open: honoring a different argument set's
      recorded result is exactly the mis-attribution the gate exists to prevent,
      and it cannot be silently skipped.
    - ``record["idempotency_key"]`` != ``request_idempotency_key`` -- a record
      for another key. The key is a REQUIRED argument (not optional): a caller
      that uses no key passes ``""`` explicitly, and the comparison still runs
      (a record carrying a key against a ``""`` request correctly refuses).
      Absence must be an explicit empty key, never an omitted parameter that
      silently disables the check.

    Then, for a record that belongs to this request:

    - ``failed_not_applied`` -> ``allow``: the prior write provably did not
      land, so a reissue cannot duplicate an effect.
    - ``succeeded`` WITH a recorded result -> ``reuse``: hand back the recorded
      result rather than reissue and duplicate it. ``succeeded`` with NO
      recorded result -> ``refuse``: there is nothing to reuse, and reusing an
      empty result would silently return nothing.
    - ``unknown`` -> ``refuse`` UNLESS the caller explicitly asserted
      idempotency on the record (``idempotent=True``). The outcome is uncertain
      and nothing proves the effect did not land, so a blind reissue risks doing
      it twice. Idempotency is decided ONLY by that explicit trusted assertion,
      never by the descriptor's ``effect``.
    """

    # --- step 1: attribution -- does this record belong to this request? ----
    # Unconditional and unskippable: the fingerprint is computed here, so a
    # caller cannot reach allow/reuse without the argument comparison running.
    if record["operation_id"] != descriptor["operation_id"]:
        return _refuse(
            "replay refused: the attempt record is for operation "
            f"{record['operation_id']} but this request is for "
            f"{descriptor['operation_id']}; a record for another operation is "
            "never a basis to allow or reuse"
        )
    if record["args_fingerprint"] != args_fingerprint(request_args):
        return _refuse(
            "replay refused: the attempt record's argument fingerprint does not "
            f"match this request's for operation {descriptor['operation_id']}; a "
            "record for another argument set is never a basis to allow or reuse"
        )
    if record["idempotency_key"] != request_idempotency_key:
        return _refuse(
            "replay refused: the attempt record's idempotency key does not match "
            f"this request's for operation {descriptor['operation_id']}; a record "
            "for another key is never a basis to allow or reuse"
        )

    outcome = record["outcome"]

    if outcome == ATTEMPT_FAILED_NOT_APPLIED:
        return {"verdict": REPLAY_ALLOW, "reuse_result": None, "error": None}

    if outcome == ATTEMPT_SUCCEEDED:
        recorded = record["recorded_result"]
        if recorded is None:
            # Nothing to reuse: a succeeded attempt with no recorded result
            # cannot be replayed (would duplicate) nor reused (would return
            # empty). Refuse rather than silently hand back nothing.
            return _refuse(
                "replay refused: the prior attempt for operation "
                f"{descriptor['operation_id']} is recorded as succeeded but "
                "carries no recorded result to reuse; reissuing could duplicate "
                "the effect and reusing would return an empty result"
            )
        return {
            "verdict": REPLAY_REUSE,
            "reuse_result": recorded,
            "error": None,
        }

    # outcome == ATTEMPT_UNKNOWN -- the case blind retry gets wrong.
    if _is_replay_safe_after_unknown(record):
        return {"verdict": REPLAY_ALLOW, "reuse_result": None, "error": None}

    return _refuse(
        "replay refused: the prior attempt for operation "
        f"{descriptor['operation_id']} left an uncertain (unknown) outcome and "
        "this operation is not asserted idempotent, so reissuing it could apply "
        "the effect a second time; no evidence proves the prior attempt did not "
        "take effect"
    )


def _refuse(detail: str) -> ReplayDecision:
    """A typed ``refuse`` decision, built through :func:`operation_error`.

    Every refusal on this gate is a typed :class:`OperationError` (``conflict``)
    with a redacted detail -- never a bare boolean or an untyped None.
    """

    return {
        "verdict": REPLAY_REFUSE,
        "reuse_result": None,
        "error": operation_error("conflict", detail),
    }
