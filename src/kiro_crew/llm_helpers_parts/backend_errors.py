"""Which backend failures a turn retries, and how long it waits before the next try.

The prompt-busy and transient retry budgets and their backoff curves, the markers that
classify a backend error as transient, a lost session, credentials the process cannot
reload, a dropped connection or an over-full context, the network-drop recovery window,
and the notices a surface shows when it reconnects a lost session.

Served through :mod:`kiro_crew.llm_helpers`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import random
import re
import time
from typing import Any

from kiro_crew.agent_sdk.drivers.acp import provider_error_client
from kiro_crew.agent_sdk.drivers.acp_turn_vocab import AcpAuthRequired, AcpError, AcpPromptBusy
from kiro_crew.credential_errors import is_credential_propagation_delay
from kiro_crew.platform.context import redact_via_context

_PROMPT_BUSY_RETRIES = 2
_PROMPT_BUSY_DELAY = 1.5  # seconds between retries


# Transient backend (Bedrock 5xx / throttle / stream-reset) retry budget. These
# are server-side hiccups where the credential is VALID — retry helps, re-auth
# does not. Kept separate from the prompt-busy budget above.
_TRANSIENT_RETRIES = 3
_TRANSIENT_DELAY = 2.0  # base seconds; exponential backoff + jitter
# The interactive ladder's base for a throttle. KAS already retries a throttled
# request for about 6 s inside one attempt (3 tries, 1 s then 2 s apart), so a
# 2 s gap rarely outlasts the throttle window; one KAS attempt's length does.
THROTTLE_RETRY_BASE_DELAY = 6.0

# Per-process RNG for retry jitter, seeded from os.urandom so gateways hit by the
# same backend 5xx do not retry in lockstep. Not seeded from os.getpid(): in a
# container the gateway is often PID 1, so every host would draw the same jitter.
# Tests patch asyncio.sleep or reseed _JITTER_RNG for determinism.
_JITTER_RNG = random.Random()

# Substrings (lowercased) that mark a RETRYABLE transient backend failure.
# Matched against the formatted AcpError message (see acp.client._format_acp_error).
# Auth/validation markers are deliberately ABSENT so those fail fast — a retry
# cannot fix an expired token or a bad request, and silently retrying them would
# only delay the correct "re-auth"/"fix the request" signal to the operator. The
# one auth-shaped exception (a credential IAM has not propagated yet) is handled
# structurally in _is_transient_acp_error, ABOVE the exclusion list, because no
# marker here could ever be reached for it — see is_credential_propagation_delay.
_TRANSIENT_MARKERS = (
    "internal server error",
    "internal error: api error",
    "serviceunavailable",
    "service unavailable",
    "throttl",  # ThrottlingException + "Bedrock is throttling"
    "toomanyrequests",
    "servicequotaexceeded",
    "modelstreamerror",
    "connection reset",
    "connectionreset",
    "dispatch failure",  # AWS SDK connector-level I/O failure (conn/DNS/TLS drop)
    "dispatchfailure",  # Rust DispatchFailure variant (unspaced)
    # Model-unavailable capacity/rollout, in _format_acp_error's wording. The
    # "on bedrock" spelling keeps transcripts from older gateways classifying;
    # a rewording of that branch must add its marker here. The unentitled-model
    # branch is terminal (_model_is_unentitled) and must not match: retrying a
    # model the account cannot use never succeeds.
    "is unavailable on the backend",
    "is unavailable on bedrock",
    # kiro-cli >= 2.16 raw capacity wording ("The model you've selected is
    # temporarily unavailable..."), as stored transcripts may still carry it.
    # Starts after "you've" so straight and typographic apostrophes both match.
    "selected is temporarily unavailable",
    "transient error (http 5xx)",  # _format_acp_error's generic-5xx message
    # kiro-cli's post-stream wrapper ("The service failed to process the
    # request (request_id: ...)"). Matches the raw provider text and
    # _format_acp_error's rewrite alike, since the rewrite keeps the phrase.
    "failed to process the request",
    # IAM credential-propagation race, matched against _format_acp_error's
    # rewritten wording. The RAW provider sentence ("The security token included
    # in the request is invalid") is matched structurally instead — see the
    # is_credential_propagation_delay call below.
    "credential-propagation delay",
    # Connection-level failures (client._RE_CONNECTION): raw errno tokens for
    # restored errors, plus the formatter's wording for fresh ones.
    "econnrefused",
    "econnreset",
    "econnaborted",
    "etimedout",
    "epipe",
    "ehostunreach",
    "eai_again",
    "socket hang up",
    "fetch failed",
    "could not reach the model backend",
)

# The members of _TRANSIENT_MARKERS that name the network path rather than a
# provider answer. Read only for an error raised without the structured
# ``connection_failure`` verdict, and only when no other transient marker is
# present, so a frame that also carries a provider answer keeps the fixed count.
_CONNECTION_MARKERS = (
    "connection reset",
    "connectionreset",
    "dispatch failure",
    "dispatchfailure",
    "econnrefused",
    "econnreset",
    "econnaborted",
    "etimedout",
    "epipe",
    "ehostunreach",
    "eai_again",
    "socket hang up",
    "fetch failed",
    "could not reach the model backend",
)
_PROVIDER_ANSWER_MARKERS = tuple(m for m in _TRANSIENT_MARKERS if m not in _CONNECTION_MARKERS)


def _is_transient_acp_error(msg: str) -> bool:
    """True iff an AcpError message looks like a retryable transient backend
    failure. Auth failures are explicitly excluded (they need re-auth, not retry)."""
    low = msg.lower()
    if is_credential_propagation_delay(msg):
        # The one auth-shaped failure a retry DOES fix: a credential IAM has not
        # propagated yet. Checked BEFORE the exclusions below because Bedrock
        # ships this rejection AS UnrecognizedClientException, so the
        # short-circuit would return False and no marker could ever be reached.
        return True
    if (
        "authentication failed" in low
        or "accessdenied" in low
        or "expiredtoken" in low
        or "unrecognizedclient" in low
        or "invalidsignature" in low
    ):
        return False
    return any(m in low for m in _TRANSIENT_MARKERS)


# ── Public reuse surface for callers with their own stream loop ──
#
# The interactive path (dashboard.chat_runner) reads the ACP stream directly
# instead of through stream_and_collect, so it imports these to share the same
# classifier, retry budget and backoff curve.

# Retries after the initial attempt.
TRANSIENT_RETRIES = _TRANSIENT_RETRIES


def is_transient_backend_error(msg: str) -> bool:
    """True iff *msg* (a formatted AcpError string) is a retryable transient
    backend failure (5xx / throttle / stream-reset) rather than an
    auth/validation error. Public alias of :func:`_is_transient_acp_error`."""
    return _is_transient_acp_error(msg)


def is_prompt_busy(exc: BaseException) -> bool:
    """True when *exc* says the backend already holds an in-flight prompt.

    Structural first: ``_format_acp_error`` rewrites "prompt already in progress"
    into prose without the marker, and ``AcpSessionHandle`` formats before
    raising. The substring fallback covers unformatted and history-restored
    messages, scoped to ``AcpError`` so an unrelated exception is never a wedge.

    ``channel.run_channel_agent`` shares this predicate for the same recovery
    (replace the session, replay once), so the two surfaces agree on a wedge.
    """
    return isinstance(exc, AcpPromptBusy) or (
        isinstance(exc, AcpError) and "already in progress" in str(exc)
    )


# The provider's refusal of a request larger than the model's window, in the
# spellings the ACP backends answer with: kiro's typed "context window
# overflowed", the claude adapter's "Prompt is too long", the model service's
# "Request exceeds the maximum allowed size" byte cap, and the OpenAI-style
# "maximum context length" a compatible endpoint returns through it.
_RE_CONTEXT_OVERFLOW_TEXT = re.compile(
    r"prompt is too long|context window overflowed|maximum context length"
    r"|request exceeds the maximum allowed size",
    re.IGNORECASE,
)


def is_context_overflow_error(exc: BaseException) -> bool:
    """True when *exc* is a turn refused as too long for the model's window.

    Structural first: the raise-time classifier's ``context_overflow`` tag, which
    owns the claude adapter's ``message``-borne refusal through the anchored
    ``_RE_ADAPTER_INPUT_TOO_LONG`` in ``acp/oversized_request_errors.py``. The text match
    is the fallback for a frame that classifier cannot read (a non-dict error,
    formatted ``Prompt error: <raw>``), for an ``AcpError`` built from text, and for the
    OpenAI-style "maximum context length", which no captured frame places yet, so
    it stays untagged rather than risk a false terminal on an echo. Scoped to
    ``AcpError`` so an unrelated exception mentioning a long prompt never counts.
    The only consumer forces a ``_bg`` recycle, so a false positive costs one
    provider respawn.
    """
    if not isinstance(exc, AcpError):
        return False
    return getattr(exc, "context_overflow", False) is True or bool(
        _RE_CONTEXT_OVERFLOW_TEXT.search(str(exc))
    )


def acp_error_is_transient(exc: BaseException) -> bool:
    """Authoritative retry-eligibility decider for an ACP error.

    Prefers the structured verdict carried on ``AcpError.transient`` — classified
    from the RAW JSON-RPC error at raise time (see
    ``acp.client._is_transient_raw_error``) — so the retry decision is
    independent of how the user-facing message is worded. Falls back to
    string-matching the formatted message for exceptions raised without the flag
    (legacy raise paths, non-``AcpError`` exceptions, tests).

    The formatted message may rewrite a generic 5xx into a friendly string that
    the marker-based string classifier alone does not recognise; relying on the
    structured flag keeps that case retryable."""
    flag = getattr(exc, "transient", None)
    if isinstance(flag, bool):
        return flag
    return is_transient_backend_error(str(exc))


def acp_error_is_pipe_death(exc: BaseException) -> bool:
    """True when an ACP error says the agent process itself has gone away.

    Matched on the client's own wording ("ACP process exited (code=N)",
    "ACP process not running"). A JSON-RPC error frame never counts, whatever
    its text: a process that answered a request is alive. The claude adapter
    answers a failed Claude Code start with ``-32603`` and "Claude Code process
    exited with code 1", which a fresh process reproduces, so treating it as a
    death retried the turn behind "Connection lost — retrying…" until "Session
    stuck" and never showed the adapter's own words.
    """
    if not isinstance(exc, AcpError) or getattr(exc, "code", None) is not None:
        return False
    msg = str(exc)
    return "process exited" in msg or "not running" in msg


def acp_error_is_session_not_found(exc: BaseException) -> bool:
    """True when the live backend cannot serve this chat and only a fresh process can.

    Two raw answers share that remedy:

    - "session not found": the live backend dropped the session. The process
      still answers, so dead-provider eviction never fires and every later
      prompt draws the same answer. The transcript is usually intact, so the
      remedy is a fresh process that re-loads the SAME id, not a retry here.
    - ``cloud_credential_error`` (``AcpError.credential_reload``, from
      ``data.errorKind`` in ``acp.transport_errors._raise_acp_error``): the
      process cached credentials it cannot re-load, so only a fresh process
      helps. It takes the same reset-and-reload-once path, with its one-retry
      budget and no replay after tool activity.

    Scoped to ``AcpError`` so an unrelated exception that mentions a missing
    session (an HTTP 404 body, a dashboard lookup) never resets a chat.
    """
    if not isinstance(exc, AcpError):
        return False
    if "session not found" in str(exc).lower():
        return True
    return acp_error_needs_fresh_credentials(exc)


def acp_error_needs_fresh_credentials(exc: BaseException) -> bool:
    """True when *exc* is the provider's ``cloud_credential_error`` answer.

    The cause half of :func:`acp_error_is_session_not_found`: a surface that
    recycles the process reads this to word its notices for credentials, not
    for a lost session, and to hand the provider's own remedy to the user.
    """
    return isinstance(exc, AcpError) and getattr(exc, "credential_reload", False) is True


#: AWS exception names that say a credential expired: signing in again clears them.
_EXPIRED_TOKEN_CODES = frozenset({"ExpiredToken", "ExpiredTokenException"})


def acp_error_needs_sign_in(exc: BaseException) -> bool:
    """True when *exc* says the harness needs a new sign-in, read from typed data only.

    The harness reported itself signed out (``AcpAuthRequired``); the frame was a
    session-expiry answer ``_raise_acp_error`` tagged ``sign_in_needed`` (any
    harness) or ``auth_required`` (the KAS sign-in card); or its ``auth_error_code`` (the AWS exception name, read from
    the raw frame at raise time) is an expired token. The message is never read,
    so expiry wording in any other error is not a sign-out. ``is True`` keeps a
    stand-in with a truthy attribute out.
    """
    if isinstance(exc, AcpAuthRequired):
        return True
    if not isinstance(exc, AcpError):
        return False
    code = getattr(exc, "auth_error_code", "")
    # ``sign_in_needed`` is set on every harness; ``auth_required`` only on one that
    # signs in through Crew (KAS), so reading that alone missed kiro-cli/Claude/BYO.
    return (
        getattr(exc, "sign_in_needed", False) is True
        or getattr(exc, "auth_required", False) is True
        or (isinstance(code, str) and code in _EXPIRED_TOKEN_CODES)
    )


#: The frame's structured ``data`` as the ACP formatter appends it to the provider
#: text (``...try again.: {'errorKind': 'cloud_credential_error'}``). Machine data
#: that tells the reader nothing, so the give-up line ends on the provider's words.
#: The match starts at the ``:`` (not at whitespace before it), so a long
#: whitespace run in provider text cannot make the scan quadratic; the space
#: before the ``:`` is stripped after the substitution.
_FRAME_DATA_TAIL_RE = re.compile(
    r""":\s*\{\s*['"]errorKind['"]\s*:\s*['"][A-Za-z0-9_]*['"]\s*\}\s*$"""
)


def credential_give_up_text(exc: BaseException | str) -> str:
    """The dead end of a credential failure that the fresh process drew too.

    The helper itself is broken (an expired SSO login, a revoked role), so no
    restart or new chat can fix it: the provider's own instruction is the only
    actionable line. Each surface appends its own next step.

    The provider text is redacted here, through the egress redaction path, so
    every surface that shows the line gets it scrubbed: the dashboard row and the
    channel post do not redact on their own. That path finds link and credential
    spans on the original text and rewrites it once, so a value one pass would
    leave once the other had rewritten its surroundings is still removed. The
    frame's trailing ``data`` repr is dropped.
    """
    detail, dropped = _FRAME_DATA_TAIL_RE.subn("", str(exc))
    if dropped:
        detail = detail.rstrip()
    detail = redact_via_context(detail)
    return (
        "The agent still could not load its cloud credentials after a restart. "
        f"Fix them first. The provider said: {detail}"
    )


#: Set by :func:`stream_and_collect` on an ``AcpError`` it propagates: True when a
#: tool call fired during the turn, so re-sending the prompt could repeat it.
TOOL_ACTIVITY_ATTR = "turn_tool_activity"


def acp_error_after_tool_activity(exc: BaseException) -> bool:
    """True when *exc* ended a turn in which a tool call had already fired."""
    return getattr(exc, TOOL_ACTIVITY_ATTR, False) is True


#: Shown once by a surface that re-loads a lost session and retries the turn.
SESSION_NOT_FOUND_RETRY_NOTICE = "⟳ The agent lost its session — reconnecting…"
#: The same one retry, when the cause is credentials the process could not load.
CREDENTIAL_RELOAD_RETRY_NOTICE = (
    "⟳ The agent could not load its cloud credentials — restarting it to load them again…"
)
#: The error a surface ends on when the one reconnect did not give it a usable session.
SESSION_NOT_FOUND_GIVE_UP_TEXT = "Could not reconnect the agent's session."
#: A session lost after the turn started working: replaced, but not replayed,
#: since a tool may already have run.
SESSION_NOT_FOUND_NOT_REPLAYED_TEXT = (
    "The agent lost its session after it had started working. It reconnects on the "
    "next message; this one was not re-run, so its tools do not run twice."
)


def transient_retry_delay(attempt: int) -> float:
    """Backoff delay (seconds) for the *attempt*-th (1-based) transient retry.

    Exponential (base ``_TRANSIENT_DELAY``, doubling per attempt) plus
    per-process jitter, so every caller that retries transient backend errors
    backs off on the identical curve and co-located peers don't retry in
    lockstep (see ``_JITTER_RNG``)."""
    base = _TRANSIENT_DELAY * (2 ** (attempt - 1))
    return base + _JITTER_RNG.random() * 0.25 * base


def throttle_retry_delay(attempt: int) -> float:
    """The :func:`transient_retry_delay` curve from :data:`THROTTLE_RETRY_BASE_DELAY`.

    The interactive ladder's wait before re-sending a turn the provider throttled
    (``acp_error_is_throttle``); every other transient keeps the 2 s base.
    """
    base = THROTTLE_RETRY_BASE_DELAY * (2 ** (attempt - 1))
    return base + _JITTER_RNG.random() * 0.25 * base


# ── Network-drop recovery window (interactive turns) ──
#
# A dropped network path (Wi-Fi, VPN reconnect, laptop wake) usually lasts longer
# than the ~14s the TRANSIENT_RETRIES ladder covers. A connection failure keeps
# retrying until CONNECTION_RECOVERY_WINDOW_SECS after the first retry, backoff
# capped at CONNECTION_RETRY_MAX_DELAY so the turn resumes soon after the network.
# Provider answers (5xx, throttle, capacity) keep the fixed count: waiting longer
# does not help a provider that is answering with errors.
CONNECTION_RECOVERY_WINDOW_SECS = 300.0
CONNECTION_RETRY_MAX_DELAY = 30.0


def acp_error_is_connection_failure(exc: BaseException) -> bool:
    """True when *exc* is a transient failure of the network path itself.

    Prefers the structured ``AcpError.connection_failure`` verdict taken from
    the raw frame at raise time, and falls back to the message text for an
    exception raised without it. Never true for an error the transient
    classifier rejects, so a terminal error cannot enter the recovery window.
    """
    if not acp_error_is_transient(exc):
        return False
    flag = getattr(exc, "connection_failure", None)
    if isinstance(flag, bool):
        return flag
    low = str(exc).lower()
    return any(m in low for m in _CONNECTION_MARKERS) and not any(
        m in low for m in _PROVIDER_ANSWER_MARKERS
    )


def acp_error_is_throttle(exc: BaseException) -> bool:
    """True when *exc* is a transient failure because the provider throttled.

    Prefers the structured ``AcpError.throttle`` verdict taken from the raw frame
    at raise time, and falls back to the message text for an exception raised
    without it. Never true for an error the transient classifier rejects.
    """
    if not acp_error_is_transient(exc):
        return False
    flag = getattr(exc, "throttle", None)
    if isinstance(flag, bool):
        return flag
    client: Any = provider_error_client()
    if client is None:
        return False
    return bool(client.classify_provider_error(str(exc)).kind == client.PROVIDER_ERROR_THROTTLE)


def connection_recovery_open(
    exc: BaseException, ladder_started_at: float, *, now: float | None = None
) -> bool:
    """True while a connection failure may keep retrying past TRANSIENT_RETRIES.

    *ladder_started_at* is the ``time.monotonic()`` reading taken when the
    current retry ladder made its first retry; ``0.0`` (no ladder) is never
    open. The window is measured from that first retry, so one outage gets one
    window however many attempts it takes.
    """
    if ladder_started_at <= 0.0 or not acp_error_is_connection_failure(exc):
        return False
    current = time.monotonic() if now is None else now
    return (current - ladder_started_at) < CONNECTION_RECOVERY_WINDOW_SECS


def connection_retry_delay(attempt: int) -> float:
    """Backoff delay (seconds) for the *attempt*-th retry of a connection failure.

    The same curve as :func:`transient_retry_delay`, capped at
    :data:`CONNECTION_RETRY_MAX_DELAY` (plus the same proportional jitter) so a
    long outage is probed every half minute rather than at a doubling interval.
    """
    base = min(_TRANSIENT_DELAY * (2 ** (min(attempt, 32) - 1)), CONNECTION_RETRY_MAX_DELAY)
    return base + _JITTER_RNG.random() * 0.25 * base


class PromptBusyExhaustedError(Exception):
    """Provider was shut down after prompt-busy retries were exhausted."""
