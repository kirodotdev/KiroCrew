from __future__ import annotations

from dataclasses import dataclass
from typing import Final

VERIFY_READ_ONLY_ARGV: Final[str] = "read_only_argv"
VERIFY_STATE_READBACK: Final[str] = "state_readback"
VERIFY_FUTURE_OBSERVATION: Final[str] = "future_observation"

VERIFICATION_KINDS: Final[frozenset[str]] = frozenset(
    {VERIFY_READ_ONLY_ARGV, VERIFY_STATE_READBACK, VERIFY_FUTURE_OBSERVATION}
)


class VerificationError(Exception):
    pass


@dataclass(frozen=True)
class ReadOnlyArgvOracle:
    kind: str
    argv: tuple[str, ...]
    expected_exit: int
    expected_output_class: str


@dataclass(frozen=True)
class StateReadbackOracle:
    kind: str
    target_kind: str
    expected_digest: str


@dataclass(frozen=True)
class FutureObservationOracle:
    kind: str
    behavior_key_canonical: str
    minimum_new_sessions: int


def build_read_only_argv(
    argv: tuple[str, ...], expected_exit: int, expected_output_class: str
) -> ReadOnlyArgvOracle:
    if not argv:
        raise VerificationError("read_only_argv requires a static argv token array")
    for token in argv:
        if not isinstance(token, str) or token == "":
            raise VerificationError("read_only_argv tokens must be non-empty strings")
    return ReadOnlyArgvOracle(
        kind=VERIFY_READ_ONLY_ARGV,
        argv=tuple(argv),
        expected_exit=expected_exit,
        expected_output_class=expected_output_class,
    )


def build_state_readback(target_kind: str, expected_digest: str) -> StateReadbackOracle:
    if target_kind not in ("lesson", "steering"):
        raise VerificationError("state_readback target must be lesson or steering")
    if len(expected_digest) != 64:
        raise VerificationError("state_readback requires a 64-char expected digest")
    return StateReadbackOracle(
        kind=VERIFY_STATE_READBACK, target_kind=target_kind, expected_digest=expected_digest
    )


def build_future_observation(
    behavior_key_canonical: str, minimum_new_sessions: int
) -> FutureObservationOracle:
    if not isinstance(minimum_new_sessions, int) or isinstance(minimum_new_sessions, bool):
        raise VerificationError("future_observation minimum must be an integer")
    if minimum_new_sessions < 1:
        raise VerificationError("future_observation minimum must be positive")
    return FutureObservationOracle(
        kind=VERIFY_FUTURE_OBSERVATION,
        behavior_key_canonical=behavior_key_canonical,
        minimum_new_sessions=minimum_new_sessions,
    )


def read_only_argv_passes(oracle: ReadOnlyArgvOracle, exit_status: int, output_class: str) -> bool:
    return exit_status == oracle.expected_exit and output_class == oracle.expected_output_class


def state_readback_passes(oracle: StateReadbackOracle, observed_digest: str) -> bool:
    return observed_digest == oracle.expected_digest


def future_observation_opens_manual_review(
    oracle: FutureObservationOracle, new_session_count: int
) -> bool:
    return new_session_count >= oracle.minimum_new_sessions
