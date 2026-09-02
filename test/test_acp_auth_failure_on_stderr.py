"""kiro-cli auth failures on stderr must be classified as auth, not as a timeout.

Two auth classifiers had drifted apart by call path. The JSON-RPC error-frame path
reached the full vocabulary (`_is_session_expired` plus `_RE_AUTH`). The spawn /
`session/new` path had its own detector matching one literal banner, `not logged in`.

Real expired-token output does not use that wording -- it reports
`AccessDeniedException: "Invalid token"` and `the bearer token included in the
request is invalid` -- so the spawn path discarded an auth signal the codebase
could already read, and the operator saw `Request session/new timed out after 90s`.
That is not a cosmetic message problem: `AcpAuthRequired` is explicitly
non-retryable, so failing to reach it burns the whole retry ladder and the full
timeout budget per attempt on a credential no retry can fix.

The stderr corpus below is real kiro-cli output captured during an actual token
expiry, not invented wording.

The first two tests deliberately assert through `AcpRuntime.saw_not_logged_in()`
rather than through the new helper. That predicate exists in both the fixed and
the unfixed tree, so on unfixed code these fail with an AssertionError showing the
real defect -- not with an ImportError showing only that a new function is absent.
"""

import pytest

# Real kiro-cli stderr during an expired IAM Identity Center bearer token.
EXPIRED_TOKEN_STDERR = [
    'GetProfile failed: AccessDeniedException: "Invalid token" (HTTP 400)',
    "Failed to fetch models from API: service error, using fallback list",
    "Access denied: The bearer token included in the request is invalid.",
]

# Ordinary startup chatter from a healthy, logged-in run.
HEALTHY_STDERR = [
    "Starting kiro-cli 2.19.1",
    "INFO listening on stdio",
    "Loaded 3 MCP servers",
    "Failed to fetch models from API: service error, using fallback list",
]


class _FakeStream:
    """Minimal asyncio-stream stand-in yielding fixed stderr lines then EOF."""

    def __init__(self, lines):
        self._lines = [f"{line}\n".encode() for line in lines]

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _FakeProcess:
    def __init__(self, lines):
        self.stderr = _FakeStream(lines)


def _runtime(lines):
    """An AcpRuntime with only the stderr machinery wired up.

    `_saw_auth_failure` is seeded unconditionally so this helper works against a
    tree that has no such attribute; the unfixed `saw_not_logged_in` ignores it.
    """
    from kiro_crew.acp.runtime import AcpRuntime

    runtime = AcpRuntime.__new__(AcpRuntime)
    runtime._stderr_lines = []
    runtime._saw_auth_failure = False
    runtime._process = _FakeProcess(lines)
    return runtime


@pytest.mark.asyncio
async def test_real_expired_token_stderr_raises_the_auth_predicate():
    """The defect itself, asserted through the predicate the callers use.

    Drives the real `_drain_stderr` over real expired-token stderr, then asks the
    question the four `AcpAuthRequired` call sites ask. Fails on unfixed code
    (`False`), because none of these lines contain the banner wording.
    """
    runtime = _runtime(EXPIRED_TOKEN_STDERR)
    await runtime._drain_stderr()
    assert runtime.saw_not_logged_in() is True, (
        "real expired-token stderr was not classified as an auth failure, so the "
        "caller raises a generic timeout instead of AcpAuthRequired"
    )


@pytest.mark.asyncio
async def test_the_observation_survives_the_stderr_ring_buffer():
    """The latch, and it is the difference between a real answer and a stale one.

    `_stderr_lines` is a 20-line ring, and nothing asks about auth until a request
    has already timed out. On a chatty startup the auth line is evicted by then, so
    a detector that re-scans the buffer answers "no auth problem" -- indistinguishable
    from a real negative.

    Puts the auth line first, then enough noise to overflow the ring. Fails on any
    implementation that scans the buffer instead of latching on arrival.
    """
    runtime = _runtime([EXPIRED_TOKEN_STDERR[2]] + [f"noise line {i}" for i in range(40)])
    await runtime._drain_stderr()

    assert len(runtime._stderr_lines) == 20, "precondition: the ring must have trimmed"
    assert not any(
        "bearer token" in line for line in runtime._stderr_lines
    ), "precondition: the auth line must have been evicted, or this proves nothing"
    assert (
        runtime.saw_not_logged_in() is True
    ), "the auth observation was lost when the ring buffer trimmed it"


@pytest.mark.asyncio
async def test_a_healthy_run_never_reports_auth_failure():
    """No false positives, and the latch is not a stuck bit on ordinary output.

    The corpus includes the fallback-list line, which co-occurs with the real auth
    failure but carries no auth signal of its own: classifying on co-occurrence
    rather than on content would flag every degraded-model-fetch run.
    """
    runtime = _runtime(HEALTHY_STDERR * 6)
    await runtime._drain_stderr()
    assert runtime.saw_not_logged_in() is False


def test_the_banner_wording_is_absent_from_the_real_corpus():
    """The premise of the defect, pinned so it cannot be argued away later.

    If kiro-cli ever does emit `not logged in` alongside these lines, the old
    narrow detector would have fired and this class of failure would be moot. It
    does not, and this records that.
    """
    joined = "\n".join(EXPIRED_TOKEN_STDERR).lower()
    assert "not logged in" not in joined


@pytest.mark.parametrize("line", [EXPIRED_TOKEN_STDERR[0], EXPIRED_TOKEN_STDERR[2]])
def test_each_auth_bearing_line_is_recognised(line):
    """Per-line coverage of the shared detector."""
    from kiro_crew.acp.client import is_auth_failure_output

    assert is_auth_failure_output(line) is True, f"not recognised: {line!r}"


def test_the_legacy_banner_still_classifies():
    """Widening must not drop what the narrow detector already caught."""
    from kiro_crew.acp.client import is_auth_failure_output

    assert is_auth_failure_output("kiro-cli: not logged in") is True
    assert is_auth_failure_output("Not Logged In") is True


def test_noise_lines_are_not_flagged_individually():
    """Each healthy line must be silent on its own, not merely in aggregate."""
    from kiro_crew.acp.client import is_auth_failure_output

    for line in HEALTHY_STDERR:
        assert is_auth_failure_output(line) is False, f"false positive on {line!r}"
