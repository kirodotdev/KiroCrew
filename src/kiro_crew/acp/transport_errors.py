"""The ACP error taxonomy and the classification of harness failures.

Defines ``AcpError`` and its subclasses, and turns what a harness reports -- a JSON-RPC
error frame, its stderr, a compaction-failure payload -- into the exception, the
retry verdict and the user-facing message both ACP transports raise. The patterns
are module-level and shared, so the message a user reads and the retry decision the
transports make are always taken from one reading of the same text.

``kiro_crew.acp.client`` re-exports every name defined here.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
from dataclasses import dataclass
from typing import Any, Sequence

from kiro_crew.acp._dispatch import redact_text
from kiro_crew.acp.runtime_models import DEFAULT_MODEL, model_is_unusable
from kiro_crew.acp.types import ACP_BACKENDS_HOST_AUTH_CALLBACK
from kiro_crew.agent_sdk import host_auth
from kiro_crew.credential_errors import is_credential_propagation_delay
from kiro_crew.sandbox import (
    LAUNCHER_EXIT_PREFIXES,
    SANDBOX_LAYER_CREW,
    SANDBOX_LAYER_HARNESS,
    corroborate_launcher_refusal,
    launcher_refusal,
    sandbox_init_remediation,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

# Logged under the ACP client's name: these are the client's diagnostics, and operator
# log filters and level settings key on that name.
logger = logging.getLogger("kiro_crew.acp.client")


# Cap for the failure detail carried into the compaction notice: it is
# backend-echoed text on a chat row, not a log line.
_COMPACTION_DETAIL_MAX_CHARS = 200


# Keys that may carry a human-readable failure reason, MOST preferred first.
# The rank is what makes extraction deterministic on a payload carrying several
# of them: KAS nests a machine reason (``cause.reason`` =
# ``MODEL_TEMPORARILY_UNAVAILABLE``) beside a sentence written for the user
# (``userFacingSessionErrorMessage``), and the sentence is the one that tells a
# reader what to DO about it.
_COMPACTION_DETAIL_KEYS = (
    "userFacingSessionErrorMessage",
    "reason",
    "message",
    "error",
    "detail",
    "name",
)

# A named reason holding one of these words says nothing the notice does not
# already say. KAS's ``summarization_failed`` frame reported literally "error",
# which rendered as "Compaction failed: error" — no reason, and nothing to grep
# server-side. Rejecting them falls through to the raw shape, which at least
# carries the payload the backend actually sent.
_COMPACTION_DETAIL_PLACEHOLDERS = frozenset(
    {"error", "errored", "failed", "failure", "none", "null", "unknown", "unknown error"}
)

# Reason markers that make a failed compaction RETRYABLE: the summarization
# model call was throttled or 5xx'd, so the same conversation can succeed on a
# later attempt or on another model. Deliberately EXCLUDES the
# context/too-large family — retrying one of those replays the same overflow,
# which is the case the no-retry policy was written for.
_COMPACTION_TRANSIENT_MARKERS = (
    "temporarily unavailable",
    "throttl",
    "too many requests",
    "rate limit",
    "high volume of traffic",
    "service unavailable",
    "internalserverexception",
    "internal server error",
    "timed out",
    "timeout",
)

# Depth bound for the payload walk. The shapes we read are three levels at
# most (``status`` -> ``error`` -> ``cause``); the bound only stops a
# pathological or cyclic frame from walking forever.
_COMPACTION_WALK_MAX_DEPTH = 6


def _walk_compaction_payload(value: object, depth: int = 0):
    """Yield ``(key, leaf)`` pairs from a compaction notification payload.

    One walker serves both readers below, so the reason a notice DISPLAYS and
    the verdict that decides whether to RETRY are derived from the same view of
    the frame — a backend that moves a field cannot make them disagree.
    """
    if depth > _COMPACTION_WALK_MAX_DEPTH:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                yield from _walk_compaction_payload(child, depth + 1)
            else:
                yield str(key), child
    elif isinstance(value, list):
        for child in value:
            yield from _walk_compaction_payload(child, depth + 1)


def compaction_failure_detail(params: dict) -> str:
    """Best-effort reason text from a ``failed`` compaction notification.

    kiro-cli carries no dedicated error field today: ``summary`` is
    populated on success but typically empty on failure, which collapses the
    user-facing notice to "unknown error" with nothing to report or grep.
    Prefer any named reason the payload does carry, else fall
    back to the raw shape so the notice says something concrete. Redacted
    here (not at each call site) because this reaches the dashboard.

    Nested by design: KAS reports the real reason under ``cause``, and the
    previous one-level read saw only the flat sibling — a placeholder word —
    so the notice said "error" while the payload carried the whole throttle.
    """
    best_rank = len(_COMPACTION_DETAIL_KEYS)
    detail = ""
    for key, value in _walk_compaction_payload(params):
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text.lower() in _COMPACTION_DETAIL_PLACEHOLDERS:
            continue
        try:
            rank = _COMPACTION_DETAIL_KEYS.index(key)
        except ValueError:
            continue
        if rank < best_rank:
            best_rank, detail = rank, text
    if not detail:
        # No named reason anywhere — the raw params ARE the only evidence.
        detail = f"no reason reported by the agent (raw: {params})"
    # redact_text is the single-source scrub (exfil URLs + credentials) every
    # other LLM-influenced surface uses, including the sibling KAS summary.
    return redact_text(detail)[:_COMPACTION_DETAIL_MAX_CHARS]


def compaction_failure_is_transient(params: dict) -> bool:
    """True when a ``failed`` compaction is worth attempting again.

    Read from the STRUCTURED payload rather than the rendered notice: the
    notice is truncated, redacted and sometimes only a raw ``repr``, so
    matching prose would both miss real throttles and fire on a digit that
    happened to land in a summary. An HTTP status is therefore compared as a
    number under its own key, never as a substring.

    The distinction is load-bearing. A compaction that failed because the
    conversation overflows the window fails again identically, which is why
    the turn is abandoned without a retry; a compaction whose summarization
    call was throttled has nothing wrong with it and succeeds on the next
    attempt. Treating the second as permanent is what dropped the user's
    message with a bare "error" on the row.
    """
    for key, value in _walk_compaction_payload(params):
        if key == "httpStatusCode":
            # ``bool`` is an ``int`` subclass, so a stray True would otherwise
            # compare as 1 and read as a status code.
            if isinstance(value, int) and not isinstance(value, bool):
                if value == 429 or 500 <= value < 600:
                    return True
            continue
        if not isinstance(value, str):
            continue
        # Only REASON-BEARING keys are scanned -- the same set the reason reader
        # ranks. The frame also carries backend-echoed, conversation-derived text
        # (conversationSummary rides in the very payload the KAS branch passes
        # whole), so matching every string leaf would let a summary that merely
        # mentions "timeout" upgrade a permanent context overflow to transient
        # and replay it. A control decision must not be reachable from content
        # the model wrote -- the same reasoning that made this read the payload
        # instead of the rendered notice.
        if key not in _COMPACTION_DETAIL_KEYS:
            continue
        # Separators folded to spaces before matching: the same fault is spelled
        # as prose in the user-facing sentence ("temporarily unavailable") and as
        # a SCREAMING_SNAKE enum in the machine reason
        # ("MODEL_TEMPORARILY_UNAVAILABLE"), and a marker list that matched only
        # one spelling would classify the same failure two different ways
        # depending on which field the backend happened to fill.
        low = value.lower().replace("_", " ").replace("-", " ")
        if any(marker in low for marker in _COMPACTION_TRANSIENT_MARKERS):
            return True
    return False


class AcpError(Exception):
    """Base ACP error.

    ``transient`` carries the retry-eligibility verdict computed from the RAW
    JSON-RPC error at raise time (see :func:`_is_transient_raw_error`), so the
    retry layer (``llm_helpers``, ``chat_runner``) decides retryability
    independently of how :func:`_format_acp_error` words the user-facing
    message. ``None`` means "unclassified" — callers fall back to
    string-matching the formatted message.

    ``code`` is the raw JSON-RPC error code when the failure came back as an
    error frame (``-32602`` Invalid params, ``-32601`` Method not found, ...),
    else ``None``. Carried as data so a caller can classify a rejection by code
    instead of parsing the redacted message: a codex session dies at startup on
    a bare ``{"code": -32602, "message": "Invalid params"}`` with no ``data``,
    which no message match can tell from a protocol error.
    """

    def __init__(
        self, *args: object, transient: bool | None = None, code: int | None = None
    ) -> None:
        super().__init__(*args)
        self.transient = transient
        self.code = code
        # Reactive-fallback metadata, set by :func:`_raise_acp_error` when a
        # prompt-time error names a rejected model (so run_bg_oneliner can retry
        # once with a served model). Guarded so AcpModelUnavailable — which sets
        # ``advertised`` BEFORE calling super().__init__ — is not clobbered.
        if not hasattr(self, "rejected_model"):
            self.rejected_model: str | None = None
        if not hasattr(self, "advertised"):
            self.advertised: list[str] = []
        # Sign-in failure tag, set by :func:`_raise_acp_error` when the raw frame
        # is a session-expiry / rejected-credential answer, so the dashboard's
        # error row can offer the Kiro sign-in card instead of a retry.
        self.auth_required: bool = False
        # Spent-allowance tag, set by :func:`_raise_acp_error` when the raw
        # frame is a usage-limit answer ("The monthly usage limit has been
        # reached"), so the dashboard's error row can offer a route that needs
        # no inference where one exists (the feature-request form) instead of a
        # retry that reproduces the rejection. Terminal like ``auth_required``
        # and, like it, decided from the raw frame rather than the prose.
        self.usage_limit: bool = False
        # Structural-rejection tag, set by :func:`_raise_acp_error` when the raw
        # frame says the request shape is deterministic: malformed request, or
        # an unsupported image already embedded in the conversation. Unlike a
        # transient backend fault, re-sending the identical context reproduces
        # either rejection. ``transient`` already carries the retry-layer verdict
        # for the SAME frame (False); this is the narrower fact a self-driving
        # caller uses to stop re-firing the same context. A spent usage limit or
        # an unentitled model are also terminal, but a new context can succeed,
        # so they are not structural terminality.
        self.structural_terminal: bool = False
        # Narrow structural subtype for a model-window rejection. Higher layers
        # inspect the flag instead of importing another ACP exception across
        # their architecture boundary.
        self.context_overflow: bool = False
        # Whether this error happened while STARTING a session rather than on a
        # prompt or another request. The twin of
        # ``AcpRequestTimeout.session_start_failed`` on the runtime path: the two
        # exception families share no base, so a self-driving caller reads the
        # fact with ``getattr`` and both halves must spell it the same way. Only
        # the raise sites that know the method set it True.
        self.session_start_failed: bool = False
        # Narrow structural subtype used by the dashboard to distinguish a bad
        # current attachment from an unsupported image retained in native
        # history. Set only from the raw provider data field.
        self.image_format_unsupported: bool = False


class AcpTimeoutError(AcpError):
    """Prompt timed out."""

    def __init__(self, partial_output: str = "", *, message: str = "ACP prompt timed out"):
        self.partial_output = partial_output
        super().__init__(message)


class AcpPermissionNeeded(AcpError):  # noqa: N818
    """Tool approval required."""

    def __init__(self, prompt: str, response_so_far: str = ""):
        self.prompt = prompt
        self.response_so_far = response_so_far
        super().__init__("Permission needed")


class AcpProcessDied(AcpError):  # noqa: N818
    """kiro-cli process exited unexpectedly.

    ``ambiguous_delivery`` is True when the death followed a request-frame drain
    stall: the frame had already been handed to the transport, so a kiro-cli that
    merely paused reading could still consume it after the death is raised. The
    recovery path must then NOT replay the prompt verbatim (that would run its
    tools a second time) -- it resumes from restored conversation state instead,
    the same choice it makes once a turn has emitted output. A write that never
    reached the transport (a lock-phase stall, a broken pipe before the write)
    leaves this False: the replay is the frame's first and only delivery.
    An ambiguous death is never transient: a retry ladder that re-sends on a
    transient verdict would replay exactly the prompt that may have run, so the
    verdict is pinned False rather than left to the message's wording.
    """

    def __init__(self, *args: object, ambiguous_delivery: bool = False, **kwargs: object) -> None:
        # Forward transient/code to AcpError so AcpRegistrationRateLimited (which
        # subclasses this and passes transient=True) keeps working.
        if ambiguous_delivery:
            kwargs["transient"] = False
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.ambiguous_delivery = ambiguous_delivery


class AcpAuthRequired(AcpError):  # noqa: N818
    """The harness is not authenticated — the user must sign in again.

    Non-retryable: respawning the process hits the same wall, so callers must
    surface the actionable message and skip the retry ladder rather than
    reset-and-requeue the turn.

    ``backend`` decides whose sign-in fixes it. Only a harness that authenticates
    through Crew's own identity (``ACP_BACKENDS_HOST_AUTH_CALLBACK``, harness
    parity H6) is tagged ``auth_required`` so the dashboard offers the Kiro
    sign-in card; for every other harness that card would sign in the wrong
    thing, so the row stays a plain error carrying that harness's own remedy.
    """

    def __init__(self, message: str = "", *, backend: str = "") -> None:
        super().__init__(message)
        self.auth_required = backend in ACP_BACKENDS_HOST_AUTH_CALLBACK


class AcpSandboxInitFailed(AcpError):  # noqa: N818
    """An OS sandbox refused to initialize, so the agent child never started.

    Non-retryable, for the reason :class:`AcpAuthRequired` and
    :class:`AcpToolGateUnroutable` are: the refusal is a property of the host and
    its configuration, so the respawn the reconnect ladder would make hits the
    same wall, and every attempt costs a spawn plus a teardown before reporting
    the same thing. ``transient`` is fixed ``False`` so the retry ladders that
    read the verdict off the exception (``llm_helpers.acp_error_is_transient``,
    and through it every consumer from the dashboard turn to a cron tick) stop on
    the first one without any of them matching on wording.

    A DISTINCT type rather than a flag on ``AcpProcessDied``, because the two
    answer different questions. ``AcpProcessDied`` says the child is gone and
    leaves whether to resubmit the turn to its own classification; this says the
    child cannot be started at all until a human changes something.

    The message names which of the two isolation layers wrapped the spawn and what
    to do about it, and ``corroborated`` decides how far that goes: the switch that
    turns a layer OFF is emitted only when a TRUSTED run reached that verdict, never
    on the strength of the dead child's own stderr -- see
    :func:`sandbox.sandbox_init_remediation`.

    Deliberately NOT an automatic downgrade to an unconfined spawn. Falling back
    to no isolation on a sandbox failure is refused by design -- it would turn a
    broken host into a silently unsandboxed agent -- so the useful thing this can
    do is fail fast and say which layer to look at, loudly.
    """

    def __init__(self, *, layer: str, detail: str = "", corroborated: bool = False) -> None:
        # Layer and remedy are LOCALS folded into the message, not attributes: the
        # message is what an operator and every error surface read, and a
        # structured field with no reader is a promise nobody keeps.
        remediation = sandbox_init_remediation(layer, corroborated=corroborated)
        # ``detail`` is the child's own stderr, already redacted by the caller
        # that captured it (both transports redact before retaining: the text is
        # untrusted subprocess output that can echo a token, and this message
        # reaches a session card, the security event log, and a cron job's
        # persisted last_error).
        said = f" -- {detail}" if detail.strip() else ""
        super().__init__(
            f"the {layer} sandbox failed to initialize, so the agent process could "
            f"not start{said}. Retrying cannot fix this: {remediation}",
            transient=False,
        )


class AcpRegistrationRateLimited(AcpProcessDied):  # noqa: N818
    """The runtime died after its dynamic registration was throttled (HTTP 429).

    A SUBCLASS of :class:`AcpProcessDied`, because the process IS gone and every
    existing death handler must keep treating it as a death; the narrower fact is
    WHY: the child's registration calls were rate-limited by the endpoint, which
    is a transient property of the endpoint's capacity, not of this host or this
    request. ``transient`` is fixed True so the retry ladders that read the
    verdict off the exception (``llm_helpers.acp_error_is_transient``, and
    through it the sub-agent run loop and ``stream_and_collect``) retry with
    their existing bounded backoff instead of surfacing a terminal generic
    death. Replay SAFETY stays where it already lives: every consumer's
    zero-activity gate decides between a verbatim replay and a continue, so this
    classification never widens what a retry may re-run.

    The message carries ONE retained cause rather than the full stderr tail: the
    throttle prints the identical line on every attempt, and five copies of it
    behind a death summary is the repetitive wall this type exists to replace.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, transient=True)


class AcpToolGateUnroutable(AcpError):  # noqa: N818
    """The harness's tool calls would not reach Kiro Crew's PreToolUse gate.

    Non-retryable, and a DISTINCT type from the transport errors around it: the
    condition is a configuration fact, so a respawn re-reads the same answer and
    refuses again while consuming a reconnect budget meant for transport faults.

    Wraps :class:`kiro_crew.acp_tool_gate.ToolGateUnroutable`, which cannot
    subclass ``AcpError`` itself -- it lives in a LEAF module that must not import
    this one (import cycle, and a forbidden-root edge for the SDK boundary gate).
    Branchless callers keep degrading through their generic ``AcpError`` handling.
    """


class PiGateExtensionTampered(AcpError):  # noqa: N818
    """The shipped gate extension's bytes do not match the digest this build pinned.

    Raised before any harness child is started: a gate whose code is not the code
    this build was tested with is not a gate, whatever its probe command says.
    """


class AcpModelUnavailable(AcpError):  # noqa: N818
    """An explicitly requested model is not available to this account.

    A DISTINCT type because the semantics differ from every other ``set_model``
    failure. The generic ones ("the call didn't land") are legitimately handled
    by tearing the session down and cold-starting. This one means "the request
    itself is invalid, and no amount of restarting changes that" — falling back
    to a reset would destroy a live conversation and then quietly land on a
    different model, reporting success. Callers must surface it (4xx / user
    error), not recover from it.

    Non-retryable: ``transient`` is fixed False, since no retry earns an
    entitlement.
    """

    def __init__(
        self,
        model_id: str,
        advertised: Sequence[str] | None = None,
        *,
        advertised_but_refused: bool = False,
    ) -> None:
        self.model_id = model_id
        self.advertised = list(advertised or [])
        usable = ", ".join(self.advertised) if self.advertised else "none advertised"
        # The identity hint is CONDITIONAL by construction ("if you expected") and
        # names only a read-only probe. A user genuinely on a free tier is
        # correctly served by the first sentence and should not be nudged toward
        # re-authenticating, so this must never read as an instruction to log out:
        # `whoami` answers "which tier am I actually on" for the user who signed in
        # to the wrong one, and merely confirms the situation for everyone else.
        if advertised_but_refused:
            # The adapter ADVERTISED this id and then refused it, on a harness
            # whose advertised list IS its entitlement: that is a spelling gap
            # between the list and the switch channel (a codex
            # ``<model>[<effort>]`` pair its ``model`` option does not take), not
            # an entitlement verdict. Pointing the user at their sign-in here
            # would send them to fix an account that is fine.
            #
            # The CALLER decides, because "advertised implies entitled" is a
            # per-harness fact that holds only for
            # ``ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS`` members. Read off
            # ``model_id in advertised`` alone it also fires for a harness that
            # advertises models an account cannot run, and tells a user on the
            # wrong tier their account is fine.
            super().__init__(
                f"The adapter advertised the model {model_id!r} but refused to "
                f"switch to it. This is a mismatch inside the adapter, not an "
                f"account restriction; try another entry from the list or restart "
                f"the session. Available models: {usable}.",
                transient=False,
            )
            return
        super().__init__(
            f"The model {model_id!r} is not available on your account. "
            f"Available models: {usable}. "
            f"If you expected this model to be included in your plan, check which "
            f"account you are signed in as with `kiro-cli whoami` — a Builder ID "
            f"sign-in carries a different entitlement than organization SSO.",
            transient=False,
        )


class AcpPromptBusy(AcpError):  # noqa: N818
    """A prompt is already in progress on this session.

    The backend still has an in-flight prompt (tool stall, timeout, or race
    between messages). Callers should reset the session so the next message
    cold-starts cleanly.
    """


# Named by the path callers import them from: an error chain renders each raised class
# as ``module.qualname`` (``subagent._describe_exception``), and that text reaches the
# Subagents panel and tracebacks, so this module's name must not leak into it.
for _raised in (
    AcpError,
    AcpTimeoutError,
    AcpPermissionNeeded,
    AcpProcessDied,
    AcpAuthRequired,
    AcpSandboxInitFailed,
    AcpRegistrationRateLimited,
    AcpToolGateUnroutable,
    PiGateExtensionTampered,
    AcpModelUnavailable,
    AcpPromptBusy,
):
    _raised.__module__ = "kiro_crew.acp.client"
del _raised


# Auth failure on stderr is detected during spawn/prompt so we can raise
# AcpAuthRequired (non-retryable) instead of churning through the retry ladder.
# The detector is `is_auth_failure_output` below; there is deliberately no
# separate "not logged in" pattern here, because having one was the defect —
# `_RE_SESSION_EXPIRED` already carries that wording alongside the rest of the
# auth vocabulary, and a second narrower copy is what let the spawn path miss
# every expiry that does not use the banner's exact words.
#
# The DETECTION lives here; the MESSAGE does not. What an operator must do to sign
# a harness back in is a property of that harness, so every raise site reads
# `host_auth.signed_out_message(backend)` instead of a literal in this module. A
# literal here spelled `kiro-cli login` for whichever harness happened to fail,
# which is wrong the moment a harness signs in through its own credential file.


# ── Transient-error classification (shared by _format_acp_error and
# _is_transient_raw_error) ──
#
# Single source of truth for "is this ACP backend error a momentary,
# retry-worthy hiccup?". The user-facing message formatter AND the
# retry-eligibility classifier both key off these patterns, so the two can
# never drift again: otherwise the formatter could rewrite a generic 5xx into a
# friendly string the marker-based retry classifier does not recognise, silently
# preventing the retry from firing.
#
# Scopes mirror _format_acp_error's if/elif chain: model-unavailable matches
# the provider `data` field only (it extracts the model name from a structured
# string); throttle, auth, and the 5xx family match the combined
# `data + message` haystack so a 5xx token in either field is caught.
_RE_MODEL_UNAVAILABLE = re.compile(r"[Tt]he model '([^']+)' is not available")
# kiro-cli >= 2.16 rewording of the same capacity/rollout rejection, which
# names NO model: "The model you've selected is temporarily unavailable.
# Please use '/model' to select a different model and try again." Without its
# own pattern this wording fell through to the unknown-shape branch and was
# classified terminal, so unattended callers (cron, subagents, consolidation)
# failed fast on a momentary blip their retry ladder exists to absorb — the
# exact drift hazard the marker-coupling note above warns about. The quote
# class covers both the straight and typographic apostrophe in "you've".
_RE_MODEL_TEMP_UNAVAILABLE = re.compile(
    r"[Tt]he model you['\u2019]ve selected is temporarily unavailable"
)
# MPS ValidationException wording for a model the partition/account does not
# serve — distinct from the "is not available" capacity string above. Covers the
# ``auto`` sentinel in partitions that do not serve it.
_RE_INVALID_MODEL_ID = re.compile(r"[Ii]nvalid model ID:\s*([^\s,;'\"]+)")
_RE_THROTTLE_NAMED = re.compile(
    r"\b(ThrottlingException|TooManyRequestsException|ServiceQuotaExceededException)\b"
)
_RE_THROTTLE_GENERIC = re.compile(r"\b(rate.?limit|throttl(?:e|ed|ing))\b", re.IGNORECASE)
_RE_AUTH = re.compile(
    r"\b(AccessDenied(?:Exception)?|UnauthorizedException|ExpiredToken(?:Exception)?"
    r"|InvalidSignatureException|UnrecognizedClientException)\b"
)
_5XX_SEP = r"[ \t_-]?"
_RE_5XX_NAMED = re.compile(
    rf"\b(internal{_5XX_SEP}server{_5XX_SEP}error|internal{_5XX_SEP}failure"
    rf"|service{_5XX_SEP}unavailable(?:{_5XX_SEP}exception)?"
    rf"|dispatch{_5XX_SEP}failure|connection{_5XX_SEP}reset(?:{_5XX_SEP}error)?)\b",
    re.IGNORECASE,
)
_RE_5XX_STATUS = re.compile(r"(?:HTTP|status)\s*(?:code\s*)?(?:50[0234]|529)\b", re.IGNORECASE)
_RE_CONNECTION = re.compile(
    r"\bE(?:CONNREFUSED|CONNRESET|CONNABORTED|TIMEDOUT|PIPE|HOSTUNREACH|AI_AGAIN)\b"
    r"|\bsocket hang ?up\b"
    r"|\bfetch failed\b"
    r"|\bconnection (?:refused|reset|closed|error|timed ?out)\b",
    re.IGNORECASE,
)
# Genuine retry hint only. "response stream" is deliberately NOT matched here,
# because that would make this branch a catch-all: kiro-cli wraps EVERY mid-stream
# provider failure as "Encountered an error in the response stream: <real cause>",
# so the wrapper prefix alone — present on quota exhaustion, validation errors,
# anything — would classify the error as a momentary 5xx, tell the user to retry,
# and DISCARD the real cause. A monthly-usage-limit rejection would surface as
# "The model backend hit a transient error (HTTP 5xx)" and burn the retry ladder.
# The wrapper is a transport envelope, not a signal about the failure inside it;
# classification reads the inner detail (see _provider_detail).
#
# The hint wordings are PROVIDER-scoped, so onboarding a backend means auditing
# this alternation. "please try again" is Kiro/Bedrock. "try your request again"
# is the claude-agent-acp seam's generic upstream 500 ("Internal error: API
# Error: The system encountered an unexpected error during processing. Try your
# request again." with data {'errorKind': 'unknown'}): that frame carries no
# named exception and no HTTP status token, so its retry hint is the ONLY
# transient signal in it, and without this token the momentary blip reached the
# user as a terminal error card and the backoff ladder never engaged.
_RE_5XX_HINT = re.compile(r"(please try again|try your request again)", re.IGNORECASE)
# Session expiry, by HTTP status. An expired session is rejected with 401/403,
# and nothing else in this module recognised those codes: the error fell through
# to the 5xx family (a co-occurring DispatchFailure/ConnectionReset from the
# aborted request is enough to match) and the user was told to retry or switch
# models, neither of which can succeed against an expired login. Status is the
# primary signal because the rejection carries no explanatory wording.
_RE_AUTH_STATUS = re.compile(r"(?:HTTP|status)\s*(?:code\s*)?(?:401|403)\b", re.IGNORECASE)
# Session expiry, by wording. Complements the status match for backends that
# describe the expiry in prose without a machine-readable code. Deliberately
# excludes Bedrock's named exceptions, which _RE_AUTH already owns.
_RE_SESSION_EXPIRED = re.compile(
    r"\b(?:session\s+(?:has\s+)?expired|session\s+timed?\s*out"
    r"|login\s+(?:has\s+)?expired|authentication\s+(?:has\s+)?expired"
    r"|not\s+logged\s+in|not\s+authenticated|not\s+signed\s+in"
    r"|re-?authenticate|login\s+required|auth(?:entication)?\s+required)\b",
    re.IGNORECASE,
)
# Credential REJECTED rather than expired. Switching the active Kiro account
# invalidates the credential a long-lived kiro-cli child still holds, and the
# upstream rejection reports only that the bearer token is invalid: it carries
# no status code and never uses expiry wording, so neither _RE_AUTH_STATUS nor
# _RE_SESSION_EXPIRED matches it and the failure reaches the user as the raw
# upstream string with no sign-in affordance. Grouped with session expiry
# because the remedy is identical — sign in again; no retry can make a rejected
# credential valid. The gap between the two words is fenced to one sentence and
# one line so the pattern cannot span unrelated errors in a combined haystack.
_RE_INVALID_BEARER = re.compile(
    r"\b(?:bearer\s+token\b[^.\n]{0,80}?\binvalid|invalid\s+bearer\s+token)\b",
    re.IGNORECASE,
)


def _is_session_expired(haystack: str) -> bool:
    """True when the session credential is expired or rejected, not a backend fault.

    All three signals are terminal: retrying can neither refresh a login nor
    revive a credential the upstream has rejected. Checked before the 5xx family
    so an aborted request's transport error does not shadow the real cause.
    """
    return bool(
        _RE_AUTH_STATUS.search(haystack)
        or _RE_SESSION_EXPIRED.search(haystack)
        or _RE_INVALID_BEARER.search(haystack)
    )


def is_auth_failure_output(haystack: str) -> bool:
    """True when free-form kiro-cli output reports an auth failure.

    Companion to :func:`_is_session_expired` for output that is NOT a JSON-RPC
    error frame — i.e. whatever the CLI writes to stderr while starting up. It is
    the union of the two terminal auth families this module already recognises:
    ``_is_session_expired`` (401/403, expiry wording, rejected bearer token) and
    ``_RE_AUTH`` (the named service exceptions, which ``_is_session_expired``
    deliberately leaves to ``_RE_AUTH``). Both are terminal for the same reason —
    no retry refreshes a login — so for the single question "is this stderr an
    auth problem" they belong together.

    This exists because the two auth vocabularies had drifted apart by call path,
    not by intent. Everything above was reachable only from the error-frame path;
    the spawn / ``session/new`` path had its own detector matching the single
    literal banner ``not logged in``. Real expiry output does not use that
    wording — an expired bearer token produces ``AccessDeniedException: "Invalid
    token"`` and ``the bearer token included in the request is invalid`` — so the
    spawn path discarded an auth signal this module could already read, and the
    operator got a 90-second timeout instead of a sign-in prompt.

    Keeping one detector rather than widening the banner regex is the same
    anti-drift argument the module makes for its other shared patterns: a second
    vocabulary is what created the gap.

    :func:`kiro_crew.credential_errors.is_credential_propagation_delay` is the
    single carve-out: an auth-shaped rejection a retry DOES fix, so latching it
    would raise the explicitly non-retryable ``AcpAuthRequired`` and skip the
    ladder — and a cold-start burst is exactly when kiro-cli prints it on stderr.
    It lives in ``credential_errors.py``, not here, so consumers outside the ACP
    layer share the verdict without a fresh agent-SDK boundary import edge.
    """
    if is_credential_propagation_delay(haystack):
        return False
    return bool(_RE_AUTH.search(haystack)) or _is_session_expired(haystack)


# An OS sandbox that refused to initialize, read off the dead child's stderr.
# Detected during spawn/init for the same reason the auth vocabulary above is: the
# condition is DETERMINISTIC, so the respawn the reconnect ladder is about to make
# reproduces it exactly, and the operator gets a generic "process exited" card
# several failed attempts later instead of the one line that names the fix.
#
# Anchored on a sandbox token in every alternative, deliberately. The failure
# arrives as a three-line burst whose other two lines are a bare spawn error
# (``Failed to spawn child process`` / ``Invalid argument (os error 22)``) and a
# bare errno (``Operation not permitted``) -- neither is sandbox-specific, both
# are ordinary output for a missing binary, a bad interpreter or a denied
# credential file, and matching either alone would latch a PERMANENT verdict onto
# a class of deaths a respawn legitimately fixes. Whichever layer refused prints a
# sandbox token of its own, so requiring one costs no coverage:
#
# * ``sandbox initialization failed`` / ``sandbox_apply`` -- Seatbelt's own
#   refusal, printed by whichever process called it (the harness's internal
#   sandbox, or ``sandbox-exec`` under Crew's wrap).
# * ``sandbox-exec:`` -- the macOS wrapper Crew's own seatbelt path execs.
# * Crew's Linux namespace launcher refusing after the spawn, classified by
#   :func:`sandbox.launcher_refusal` rather than by its prefixes. The prefixes
#   alone are the WRONG test: that function deliberately answers ``None`` for
#   launcher lines that are not sandbox failures at all, and two of them are
#   actively retryable -- ``FATAL ... did not publish`` is a failed parent/child
#   pipe handshake it classifies as ``transient`` (it self-heals on the next
#   spawn), and the hardlinked-credential refusal means the sandbox WORKED and
#   found host state it must not paper over. Only its ``no_backend`` kind -- the
#   host cannot build the sandbox -- is this signature. Classified per LINE
#   because that function returns on its first matching line, so a hardlink line
#   above a real ``unshare`` refusal would otherwise hide it.
#
# The EMITTER does not identify the responsible LAYER, which is why the raise site
# supplies that separately -- see :func:`sandbox_init_failure`. A harness sandbox
# nested inside Crew's wrap fails with the harness's own wording while the layer
# to turn off is Crew's.
_RE_SANDBOX_INIT_FAILURE = re.compile(
    r"sandbox\s+initialization\s+failed|\bsandbox_apply\b|^\s*sandbox-exec:",
    re.IGNORECASE | re.MULTILINE,
)


def is_sandbox_init_failure_output(haystack: str) -> bool:
    """True when *haystack* (a child's stderr) carries an OS-sandbox init refusal.

    Shared by both ACP transports -- ``AcpClient`` reads its own stderr ring
    buffer, ``AcpRuntime`` latches per line at its drain -- so the two cannot come
    to disagree about what the signature IS, the same anti-drift reason
    :func:`is_auth_failure_output` is one function.
    """
    if _RE_SANDBOX_INIT_FAILURE.search(haystack):
        return True
    for line in haystack.splitlines():
        if not line.strip().startswith(LAUNCHER_EXIT_PREFIXES):
            continue
        classified = launcher_refusal(line)
        if classified is not None and classified[0] == "no_backend":
            return True
    return False


async def sandbox_init_failure(
    detail: str,
    *,
    crew_wrap: bool,
    mode: str = "",
    extra_hidden_dirs: tuple[str, ...] = (),
    corroboration_output: str = "",
) -> "AcpSandboxInitFailed":
    """Build the classified error for a spawn an OS sandbox refused.

    *crew_wrap* is the one fact the stderr cannot carry: WHICH layer wrapped the
    spawn. Callers read it from the argv their own wrap returned
    (:func:`sandbox.wrapped_by_crew_sandbox`) rather than from the signature,
    because a harness sandbox nested inside Crew's wrap prints the harness's
    wording while the layer the operator must change is Crew's -- the field
    reports this exists for were resolved by Crew's own switch, on stderr that
    named the harness. When Crew's wrap is NOT in the chain the harness's own
    sandbox is the only one left. Two layers, no third state: a spawn with neither
    cannot produce this signature at all.

    ASYNC because of the second question, which is the one that decides whether the
    message may hand out a disable switch: does a TRUSTED run agree that the layer
    refuses on this host? ``corroborate_launcher_refusal`` answers it by re-running
    the real launcher around a trusted no-op, whose stderr no child wrote -- and it
    blocks on a subprocess, so it goes off the loop exactly as the first-run gate
    runs it. It answers ``None`` off Linux and for output carrying no launcher line,
    and ``transient`` for a failed launcher readiness handshake; only its
    ``no_backend`` verdict -- the host cannot build the sandbox -- corroborates.

    *extra_hidden_dirs* is the refused spawn's own mask set so the trusted run
    exercises the same mounts. Omitting it can only make the trusted run refuse
    LESS, which withholds the switch rather than handing it out wrongly -- the safe
    direction for a default.

    *corroboration_output* is the stderr to CLASSIFY, separate from *detail* which
    is the stderr to SHOW, because the two need different shapes. The launcher's
    refusal is recognised per LINE, and a caller whose display string folds the tail
    onto one line behind a summary prefix (the shared runtime's ``death_summary()``
    does exactly that) would present nothing a line test can match -- corroboration
    would silently never run and the switch would never be reachable. Defaults to
    *detail*, which is the right answer for a caller holding the raw tail.
    """
    layer = SANDBOX_LAYER_CREW if crew_wrap else SANDBOX_LAYER_HARNESS
    corroborated = False
    to_classify = corroboration_output or detail
    if launcher_refusal(to_classify) is not None:
        verdict = await asyncio.to_thread(
            functools.partial(
                corroborate_launcher_refusal,
                to_classify,
                mode=mode or "strict",
                extra_hidden_dirs=extra_hidden_dirs,
            )
        )
        # ``no_backend`` ONLY. The same function answers ``("transient", ...)``
        # for a failed launcher readiness handshake, which says nothing about the
        # host and self-heals on the next spawn, so treating it as corroborated
        # would hand out the disable switch for a condition that fixes itself.
        # The identical test the signature detector applies to a launcher line,
        # for the identical reason.
        corroborated = verdict is not None and verdict[0] == "no_backend"
    return AcpSandboxInitFailed(layer=layer, detail=detail, corroborated=corroborated)


async def sandbox_init_failure_for_runtime(runtime: Any) -> "AcpSandboxInitFailed | None":
    """The classified error when *runtime* observed a sandbox refusal, else ``None``.

    ONE function for the three sites that translate a shared-runtime STARTUP
    failure, so they cannot answer this differently. All three are in
    ``providers/acp.py`` -- the first ``spawn()``, the resume respawn, and
    ``create_session()`` -- and each already restates the auth translation inline.
    A third restatement of THIS one is how one startup path ends up classifying
    what another does not, which is the shape that let the shared-runtime startup
    miss the latch entirely in the first place.

    Three and not four: the per-turn death translation reads no latch, because the
    latch is spent when ``initialize`` completes and a live session exists only
    after that.

    Duck-typed rather than annotated against ``AcpRuntime``: this module is below
    the runtime in the import order and cannot name it.

    Settles the stderr drain FIRST, and that is not a nicety. The latch is written
    by the drain task, while the death that brings a caller here is discovered on
    the stdout side and fails the pending ``initialize`` synchronously -- so both
    are runnable at once and a straight read can see ``False`` for a line already
    in the pipe. That is the ordinary shape of a real refusal, not a corner: the
    child writes its signature and closes stdout together. Bounded, and swallowing
    everything except a cancel of the caller (see :meth:`AcpRuntime.settle_stderr`).
    Because every caller asks this BEFORE the auth translation, the settle covers
    that read too.
    """
    await runtime.settle_stderr()
    if not runtime.saw_sandbox_init_failure():
        return None
    # The retained summary already carries the redacted stderr tail; it is empty
    # on a restricted session, where the latch still fires and the remedy is what
    # matters.
    return await sandbox_init_failure(
        runtime.death_summary() or "",
        crew_wrap=runtime.sandbox_wrapped_by_crew,
        mode=runtime.sandbox_mode,
        extra_hidden_dirs=runtime.sandbox_hidden_dirs,
        # The retained summary is what an operator READS -- one line, the tail
        # folded behind a returncode prefix. Corroboration needs the lines
        # themselves, so it gets them separately; passing the summary would leave
        # the launcher's own refusal unrecognisable and the trusted run unmade.
        corroboration_output=runtime.redacted_stderr_tail(),
    )


# A dynamic-registration call the endpoint throttled, read off the dead child's
# stderr. Deliberately CONJUNCTIVE per line: a line must carry both the
# registration context and an unambiguous too-many-requests marker before it
# classifies, so neither a model-turn throttle (429 with no registration
# context, which the provider-error path already classifies from its structured
# frame) nor an unrelated registration failure (auth rejection, malformed
# response — both terminal) can fire this. A bare ``429`` is deliberately NOT a
# marker: a digit that lands in an exit code or a byte count must not upgrade a
# crash to a throttle.
#
# Free-text stderr is an accepted evidence source here for the same reason it
# is for :func:`is_auth_failure_output` and the sandbox latch: the child's
# stderr is subprocess diagnostic output, not the structured frame the
# compaction classifier insists on — but it is also the ONLY surface this
# failure reaches, because the child dies before any frame can carry it. The
# consequence of a false positive is bounded (a budgeted retry of a turn whose
# replay safety is gated on observed activity by every consumer), which is why
# the conjunctive signature above is the whole defence this needs.
_RE_REGISTRATION_FAILED = re.compile(r"\bregistration failed\b", re.IGNORECASE)
_RE_REGISTRATION_THROTTLE_MARK = re.compile(
    r"\bHTTP 429\b|\btoo many requests\b|\brequested too many times\b", re.IGNORECASE
)


def registration_throttle_line(haystack: str) -> str | None:
    """The first line of *haystack* showing a throttled registration, or ``None``.

    *haystack* is a child's retained stderr (newline-joined lines). Matched per
    LINE so the two tokens must describe the same event: a registration failure
    early in the tail plus an unrelated throttle mention later must not combine
    into a verdict neither line supports. Returns the line itself so the caller
    can retain ONE sanitized cause instead of the tail's repeated copies.
    """
    for line in haystack.splitlines():
        if _RE_REGISTRATION_FAILED.search(line) and _RE_REGISTRATION_THROTTLE_MARK.search(line):
            return line.strip()
    return None


def is_registration_throttle_output(haystack: str) -> bool:
    """True when *haystack* (a child's stderr) shows a throttled registration.

    Shared by both ACP transports — ``AcpClient`` reads its own stderr ring
    buffer, the shared-runtime death translations read
    ``AcpRuntime.redacted_stderr_tail()`` — so the two cannot come to disagree
    about what the signature IS, the same anti-drift reason
    :func:`is_auth_failure_output` is one function.
    """
    return registration_throttle_line(haystack) is not None


def registration_rate_limited_error(base: str, cause: str) -> "AcpRegistrationRateLimited":
    """Build the typed error for a death whose evidence shows a registration throttle.

    ONE composer for every translation site (the shared-runtime ``_died``, the
    provider's ``_translate_dead``, the direct client's own death paths), so the
    guidance and the one-retained-cause shape cannot drift apart. *base* is the
    site's own death context; *cause* is the single matched stderr line, already
    redacted by whichever reader captured it.
    """
    return AcpRegistrationRateLimited(
        f"{base} — dynamic registration was rate-limited by the endpoint "
        f"(HTTP 429); this is endpoint throttling, not a crash — retry later. "
        f"Cause: {cause}"
    )


# Account/plan capacity is EXHAUSTED — terminal. Distinct from a throttle: a
# throttle clears in seconds and a retry is the right move, whereas a spent
# monthly allowance does not come back until it resets, so retrying only adds
# latency before the same rejection. Checked BEFORE the throttle branch because
# some limit messages also carry rate-limit-ish wording.
_RE_USAGE_LIMIT = re.compile(
    r"\b(?:monthly|daily|weekly)\s+(?:usage\s+)?limit\b"
    r"|\busage\s+limit\s+has\s+been\s+reached\b"
    r"|\b(?:MonthlyLimitError|FreeTierLimitExceeded)\b",
    re.IGNORECASE,
)
# kiro-cli's generic wrapper for a backend generation failure that died BEFORE
# the response stream was established (so no request_id, no error class, and
# none of the tokens above). Observed a case where data was exactly "Kiro
# failed to generate a response" while an independent gateway was
# simultaneously getting model-unavailable for the same model family. These
# pre-stream failures are overwhelmingly momentary capacity or rollout blips,
# so they are retry-worthy. Matched against the provider `data` field only
# (like model-unavailable): the phrase is a provider wrapper, never JSON-RPC
# boilerplate, and scoping to `data` keeps a stray echo in `message` from
# flipping an otherwise-terminal error.
_RE_GENERATE_FAILED = re.compile(r"failed to generate a response", re.IGNORECASE)
# kiro-cli's sibling wrapper for the same class of backend failure, observed
# AFTER tool results had landed and carrying a request_id: "The service failed
# to process the request (request_id: ...)". Same momentary-blip reasoning and
# the same `data`-only scoping as _RE_GENERATE_FAILED; kept as its own pattern
# so the two branches can word their guidance for the moment each one fails.
_RE_PROCESS_FAILED = re.compile(r"failed to process the request", re.IGNORECASE)

# kiro-cli's structural rejection of a payload the backend could not parse:
# "Improperly formed request". This string has NO source-side handling and
# passes through the unknown-shape branch verbatim, so the user gets
# the raw provider text with no repair affordance. This is a DETERMINISTIC
# rejection (the payload was rejected for its shape, not a momentary backend
# fault), so retrying the identical payload can only reproduce the same
# rejection: it is TERMINAL (non-retryable). Matched against the provider `data`
# field only (like model-unavailable / generate-failed), so a stray echo of the
# phrase in the JSON-RPC `message` cannot flip an unrelated error into this
# branch. Drives BOTH _format_acp_error (repair guidance) and
# _is_transient_raw_error (terminal verdict) so wording and retry-eligibility
# never drift.
_RE_MALFORMED_REQUEST = re.compile(r"[Ii]mproperly formed request", re.IGNORECASE)

# kiro-cli's deterministic context-window rejection. It can arrive before the
# first model event, when a custom agent's prompt, resources, and MCP schemas
# already fill the window, or mid-conversation, when accumulated history does.
# The same native session cannot shrink the envelope it was rejected on, so
# replaying there reproduces the rejection; only a fresh session or a smaller
# always-loaded set can succeed.
_RE_CONTEXT_WINDOW_OVERFLOW = re.compile(r"\bcontext window overflowed\b", re.IGNORECASE)

# Kiro's image validator rejects either the machine reason or its typed wrapper.
# The wrapper is the only stable token present in some ACP error data, while the
# machine reason appears in kiro-cli's own log. Both mean the identical
# conversation payload will be rejected again until the offending current image
# is removed or native image history is dropped.
_RE_IMAGE_FORMAT_UNSUPPORTED = re.compile(
    r"(?:IMAGE_FORMAT_UNSUPPORTED|ImageValidationError|Could not process image)",
    re.IGNORECASE,
)

# kiro-cli's OWN refusal of a request no compaction round can make fit: "This
# message is too large to send, and it contains no text that can be shortened.
# Remove or reduce the attached content and try again." It is raised when the
# context overflowed and the pending message is IRREDUCIBLE (image blocks have no
# truncated form), and kiro-cli deliberately does not compact on the way to
# saying so, nor does it append the failed message to the native history -- so the
# conversation is byte-identical before and after the failure, and re-sending the
# same context reproduces the verdict on every attempt. That makes it a
# STRUCTURAL rejection in the same sense as _RE_MALFORMED_REQUEST (a size verdict
# rather than a shape verdict, but equally deterministic), and a self-driving
# caller that re-inlines the same attachment each cycle must stop rather than
# re-fire it. Matched against the provider `data` field only, like its two
# siblings: the ACP server hands the agent-loop error to `internal_error(text)`,
# so the sentence rides in `data` beside the -32603 boilerplate `message`, and a
# phrase echo carried only by `message` must not stamp an unrelated error. The
# wording ends in "try again", so the terminal verdict is stated explicitly in
# _is_transient_raw_error rather than left to the unknown fall-through: a
# co-occurring retry hint or 5xx wrapper must not rescue a size verdict.
_RE_OVERSIZED_REQUEST = re.compile(r"[Tt]his message is too large to send")

# kiro-cli's wording for a concurrent in-flight prompt on the session, read by
# the user-facing formatter below and by `_raise_acp_error`'s AcpPromptBusy
# classification. One pattern, but two haystacks: the formatter scopes to the
# provider `data` field while the classifier also searches the JSON-RPC
# `message`, so an echo carried only by `message` raises AcpPromptBusy under the
# unrecognised-shape text.
_PROMPT_BUSY_RE = re.compile(r"already in progress", re.IGNORECASE)

# kiro-cli's envelope for a failure that happened mid-stream. The text after the
# colon is the provider's own message — the same words the CLI prints in a
# terminal — so it is what a user needs to see.
_RE_STREAM_ENVELOPE = re.compile(
    r"^\s*Encountered an error in the response stream:\s*", re.IGNORECASE
)
# Trailing "(request_id: ...)" is stripped from the detail because every branch
# re-appends it via req_id_suffix; leaving it would print the id twice.
_RE_TRAILING_REQ_ID = re.compile(r"\s*\(request_id:\s*[0-9a-fA-F-]+\)\s*$")


def _provider_detail(data: str) -> str:
    """The provider's own error text, unwrapped from kiro-cli's stream envelope.

    Returns "" when *data* carries nothing worth showing. Used by the
    unknown-shape fallback so an unrecognised provider failure surfaces its real
    message (CLI parity) instead of a ``repr`` of the JSON-RPC dict. Recognised
    failure modes keep their curated guidance and do not call this.
    """
    detail = _RE_STREAM_ENVELOPE.sub("", str(data or "")).strip()
    detail = _RE_TRAILING_REQ_ID.sub("", detail).strip()
    return detail


def _model_is_unentitled(data: str, available_models: Sequence[str] | None) -> str | None:
    """Return the rejected model name iff the account is not entitled to it.

    Upstream reports entitlement failures and transient capacity failures with
    the SAME string ("The model 'X' is not available"), so the string alone
    cannot tell them apart. The advertised model list can: it is captured at
    session init from what this account is actually served, so a rejected model
    that is absent from it was never on offer -- an entitlement problem no retry
    can fix. A rejected model that IS advertised really is a transient
    capacity/rollout blip.

    Returns None when the model is advertised, when nothing was rejected, or
    when *available_models* is None/empty (entitlement unknowable -- treat as
    transient rather than telling a user their plan lacks a model on no
    evidence).

    Both :func:`_format_acp_error` and :func:`_is_transient_raw_error` route
    through this single helper so the user-facing wording and the retry verdict
    cannot drift apart -- see the drift warning above.
    """
    # Two wordings name the rejected id: kiro-cli's "The model 'X' is not
    # available" and the MPS validation frame "Invalid model ID: X" (the shape
    # the background path's ``_rejected_model_from_error`` already accepts).
    # Both are judged against the same served list so the entitlement wording
    # and the retry verdict cannot depend on which frame the backend emitted.
    match = _RE_MODEL_UNAVAILABLE.search(data) or _RE_INVALID_MODEL_ID.search(data)
    if not match:
        return None
    if not available_models:
        return None
    rejected = match.group(1)
    # Route the served-list membership through the canonical helper: casing has
    # no meaning in these ids, and an entitled-but-differently-cased match must
    # not be reported as unentitled. The empty-list guard above preserves the
    # None-on-empty contract, so the helper's empty-means-usable answer is
    # never consulted here.
    if not model_is_unusable(rejected, available_models):
        return None
    return rejected


def _is_transient_raw_error(error: object, available_models: Sequence[str] | None = None) -> bool:
    """True iff a raw ACP JSON-RPC ``error`` is a retryable transient backend
    failure (Bedrock 5xx / throttle / model-unavailable rollout) rather than an
    auth/validation/unknown error that a retry cannot fix.

    Classifies from the RAW ``{code, message, data}`` — never the formatted
    user-facing string — so the retry decision is independent of message
    wording. :class:`AcpError` carries this verdict (``.transient``) to the
    retry layer (``llm_helpers``, ``chat_runner``). Precedence:
    unentitled-model(terminal) → usage-limit(terminal) →
    malformed-request(terminal) → context-window-overflow(terminal) →
    unsupported-image(terminal) → oversized-request(terminal) →
    model-unavailable → throttle →
    credential-propagation(transient) → auth(terminal) →
    session-expiry(terminal) → connection failure(transient) → generic 5xx /
    pre-stream generation failure → unknown(terminal). Every step mirrors
    :func:`_format_acp_error`'s if/elif order EXCEPT the structural branches:
    that formatter checks malformed-request LAST and has no oversized-request
    branch at all (the provider's own sentence is shown verbatim), while this
    classifier deliberately hoists all three above the 5xx family so a
    co-occurring connector token or retry hint cannot rescue a payload the
    backend rejected for its shape or size (see
    ``test_terminal_branches_outrank_a_co_occurring_dispatch_failure``). A frame
    carrying both wordings therefore reads as 5xx prose with a terminal verdict;
    only the verdict drives retries.

    *available_models* is this account's advertised set when the caller knows
    it. It only ever makes the verdict MORE conservative: a model the account
    was never offered is terminal instead of being retried to no purpose. Omit
    it and behaviour is unchanged from before this parameter existed.
    """
    if not isinstance(error, dict):
        return False
    data = str(error.get("data", "") or "")
    message = str(error.get("message", "") or "")
    haystack = f"{data} {message}"
    if _model_is_unentitled(data, available_models):
        # Terminal: the account is not entitled to this model, so every retry
        # spends latency to reproduce the same rejection.
        return False
    if _RE_USAGE_LIMIT.search(haystack):
        # Terminal: the allowance is spent until it resets. Ahead of the throttle
        # check so limit wording that also reads as rate-limiting stays terminal.
        return False
    if _RE_MALFORMED_REQUEST.search(data):
        # Terminal: a payload rejected for its STRUCTURE will be
        # rejected identically on every retry, so there is no momentary fault to
        # wait out. Scoped to `data` (mirrors _format_acp_error) so a phrase
        # echo in the JSON-RPC `message` can't flip an unrelated error. Stated
        # explicitly and terminal-first (like _RE_USAGE_LIMIT) so the verdict is
        # intentional and self-documenting, not incidental to the False
        # fall-through, and so a future transient marker added below cannot
        # accidentally match malformed-request wording.
        return False
    if _RE_CONTEXT_WINDOW_OVERFLOW.search(data):
        # Terminal on this native session: its startup/turn envelope already
        # exceeds the model window, so replaying it reproduces the rejection.
        return False
    if _RE_IMAGE_FORMAT_UNSUPPORTED.search(data):
        # Terminal and structural: the validator rejected image data in this
        # exact conversation payload. A co-occurring generic 5xx wrapper must
        # not spend retries replaying the same unsupported bytes.
        return False
    if _RE_OVERSIZED_REQUEST.search(data):
        # Terminal and structural: kiro-cli found no compaction round that could
        # make this request fit and did not alter the conversation on the way to
        # saying so, so the identical payload is refused identically on every
        # retry. Stated here because the sentence itself ends in "try again":
        # left to the unknown fall-through, a retry-hint pattern added below
        # could read a size verdict as a momentary blip.
        return False
    if _RE_MODEL_UNAVAILABLE.search(data):
        return True
    if _RE_MODEL_TEMP_UNAVAILABLE.search(data):
        # Nameless capacity rejection (kiro-cli >= 2.16 wording). Matched
        # against `data` only, like its named sibling above, so a phrase echo
        # in the JSON-RPC `message` can't flip an otherwise-terminal error.
        # No entitlement check is possible (the wording names no model), but
        # the bounded retry budget caps the cost if one ever slips through.
        return True
    if _RE_THROTTLE_NAMED.search(haystack) or _RE_THROTTLE_GENERIC.search(haystack):
        return True
    if is_credential_propagation_delay(haystack):
        # Transient: the credential is valid, IAM has just not propagated it yet.
        # Checked BEFORE both auth branches below, which match this very frame
        # (UnrecognizedClientException, HTTP 403) and would return terminal.
        return True
    if _RE_AUTH.search(haystack):
        # Auth is terminal — a retry can't fix an expired/denied credential.
        return False
    if _is_session_expired(haystack):
        # Session expiry is terminal — retrying can't refresh an expired login.
        return False
    if _RE_CONNECTION.search(haystack):
        # A temporary endpoint failure is safe for the bounded retry ladder.
        return True
    return bool(
        _RE_5XX_NAMED.search(haystack)
        or _RE_5XX_STATUS.search(haystack)
        or _RE_5XX_HINT.search(haystack)
        or _RE_GENERATE_FAILED.search(data)
        or _RE_PROCESS_FAILED.search(data)
    )


#: ``ProviderErrorClass.kind`` values, in the precedence
#: :func:`classify_provider_error` applies (first match wins).
PROVIDER_ERROR_USAGE_LIMIT = "usage_limit"
PROVIDER_ERROR_MALFORMED_REQUEST = "malformed_request"
PROVIDER_ERROR_MODEL_UNAVAILABLE = "model_unavailable"
PROVIDER_ERROR_THROTTLE = "throttle"
PROVIDER_ERROR_CREDENTIAL_PROPAGATION = "credential_propagation"
PROVIDER_ERROR_AUTH = "auth"
PROVIDER_ERROR_SESSION_EXPIRED = "session_expired"
PROVIDER_ERROR_CONNECTION = "connection"
PROVIDER_ERROR_HTTP_5XX = "http_5xx"
PROVIDER_ERROR_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderErrorClass:
    """One provider-error verdict: what the text says and whether a retry can help.

    ``kind`` is one of the ``PROVIDER_ERROR_*`` tokens; ``retryable`` is exactly
    the answer :func:`_is_transient_raw_error` gives for the same text, so the
    two can never disagree about a frame; ``matched`` is the token the pattern
    hit (for logs), never the whole message.
    """

    kind: str
    retryable: bool
    matched: str = ""

    @property
    def terminal(self) -> bool:
        return not self.retryable


def classify_provider_error(haystack: str, *, data: str | None = None) -> ProviderErrorClass:
    """Name the provider failure in *haystack* using this module's ONE pattern set.

    *haystack* is the message text (formatted or raw); *data* is the raw JSON-RPC
    ``error.data`` field when the caller has it (defaults to *haystack*), because
    the malformed-request and model-unavailable patterns are matched against the
    provider's own field only, never against a phrase echo in ``message``.

    Precedence mirrors :func:`_is_transient_raw_error` exactly: usage-limit →
    malformed-request → model-unavailable → throttle → credential-propagation →
    auth → session-expiry → connection → 5xx (named / status / retry hint) →
    unknown. ``unknown`` is terminal. This is the public face of the private
    ``_RE_*`` patterns: the dependency coordinator's ACP adapter and any other
    reader classify through it so a third copy of the vocabulary cannot drift.
    """
    text = haystack or ""
    data_field = text if data is None else data
    if _RE_USAGE_LIMIT.search(text):
        return ProviderErrorClass(PROVIDER_ERROR_USAGE_LIMIT, False, "usage limit")
    if _RE_MALFORMED_REQUEST.search(data_field):
        return ProviderErrorClass(PROVIDER_ERROR_MALFORMED_REQUEST, False, "malformed request")
    if _RE_MODEL_UNAVAILABLE.search(data_field) or _RE_MODEL_TEMP_UNAVAILABLE.search(data_field):
        return ProviderErrorClass(PROVIDER_ERROR_MODEL_UNAVAILABLE, True, "model unavailable")
    match = _RE_THROTTLE_NAMED.search(text) or _RE_THROTTLE_GENERIC.search(text)
    if match:
        return ProviderErrorClass(PROVIDER_ERROR_THROTTLE, True, match.group(0))
    if is_credential_propagation_delay(text):
        return ProviderErrorClass(
            PROVIDER_ERROR_CREDENTIAL_PROPAGATION, True, "credential propagation"
        )
    match = _RE_AUTH.search(text)
    if match:
        return ProviderErrorClass(PROVIDER_ERROR_AUTH, False, match.group(0))
    if _is_session_expired(text):
        return ProviderErrorClass(PROVIDER_ERROR_SESSION_EXPIRED, False, "session expired")
    match = _RE_CONNECTION.search(text)
    if match:
        return ProviderErrorClass(PROVIDER_ERROR_CONNECTION, True, match.group(0))
    match = (
        _RE_5XX_NAMED.search(text)
        or _RE_5XX_STATUS.search(text)
        or _RE_5XX_HINT.search(text)
        or _RE_GENERATE_FAILED.search(data_field)
        or _RE_PROCESS_FAILED.search(data_field)
    )
    if match:
        return ProviderErrorClass(PROVIDER_ERROR_HTTP_5XX, True, match.group(0))
    return ProviderErrorClass(PROVIDER_ERROR_UNKNOWN, False)


def _auto_remedy(available_models: Sequence[str] | None) -> str:
    """The "set agent.model to 'auto'" remediation step, or nothing when the
    partition does not serve ``auto``.

    The capacity-blip messages list three remedies, and the second is the
    ``auto`` sentinel. On a partition whose advertised list lacks ``auto`` that
    advice re-opens the circle the unentitled-``auto`` branch closes: the user
    follows it and the next turn dies on "no access to model 'auto'". So the
    step is emitted only when ``auto`` is served, or when the served list is
    unknown (nothing to check against, keep the historical advice). The
    numbering of the remaining step shifts so the list still reads (1)(2)(3)
    or (1)(2).
    """
    if model_is_unusable(DEFAULT_MODEL, available_models):
        return "or (2) "
    return f"(2) set agent.model to '{DEFAULT_MODEL}' in ~/.kiro/crew/config.json, or (3) "


def _format_acp_error(
    error: object,
    available_models: Sequence[str] | None = None,
    *,
    backend: str = "",
) -> str:
    """Format a JSON-RPC error from the ACP backend into actionable user text.

    The ACP backend (kiro-cli or claude-agent-acp) surfaces upstream Bedrock
    failures as JSON-RPC ``error`` objects with shape
    ``{"code": int, "message": str, "data": str}``.  The ``data`` field
    typically contains the raw provider error string and a request_id.

    For known failure modes (model unavailable, throttling, auth) we rewrite
    the message into concrete recovery steps.  For everything else we fall
    back to the previous behaviour ``"Prompt error: <raw dict>"`` so we don't
    swallow new error shapes.

    The provider request_id is preserved in every variant so that operators
    can correlate against support tickets and Bedrock logs.

    Security: the ``data`` field originates from upstream and may contain
    credential patterns or exfiltration URLs (especially in the fallback
    path that echoes the raw dict).  The return value is therefore passed
    through ``redact_credentials`` and ``redact_exfiltration_urls`` before
    being raised to the dashboard / Slack / CLI surfaces.
    """
    if isinstance(error, dict):
        data = str(error.get("data", "") or "")
        message = str(error.get("message", "") or "")
        haystack = f"{data} {message}"

        req_id_match = re.search(
            r"request(?:_|\s+)id\s*[:=]\s*([0-9a-fA-F-]+)", data, re.IGNORECASE
        )
        req_id_suffix = f" (request_id: {req_id_match.group(1)})" if req_id_match else ""

        # Entitlement failure: this account was never offered the model, so the
        # capacity/rollout advice below would be actively misleading (there is
        # nothing to wait for). Checked FIRST because upstream uses the same
        # string for both cases.
        unentitled = _model_is_unentitled(data, available_models)
        if unentitled:
            usable = [m.strip() for m in (available_models or []) if m and m.strip()]
            # Cap the list: an account with many models would otherwise bury the
            # message, and the picker shows the full set anyway.
            shown = ", ".join(usable[:8])
            more = f" (+{len(usable) - 8} more)" if len(usable) > 8 else ""
            if unentitled.strip().lower() == DEFAULT_MODEL:
                # The rejected id IS the "let the backend choose" sentinel: some
                # partitions do not serve it, so the usual "set agent.model to
                # 'auto'" advice would send the user in a circle. Every layer
                # that can name a model (session picker, per-agent pin, the
                # global default under Settings -> Chat) has to move off it.
                # The CAUSE is not named: the served list looks the same for a
                # regional partition and a plan/tier exclusion, so the message
                # states only what the evidence supports — not on this account.
                # The same row reaches the CLI, subagents and messaging
                # channels, where there is no picker and no Settings page, so
                # the config.json spelling of the default is named too.
                formatted = (
                    f"Your account does not have access to model '{unentitled}' — "
                    f"the automatic model choice is not available on your account. "
                    f"Available to you: {shown}{more}. Pick one of these in the "
                    f"model picker for this session, and change the default model "
                    f"under Settings → Chat (agent.model in ~/.kiro/crew/config.json) "
                    f"so new sessions do not start on 'auto' again. Retrying will "
                    f"not help."
                    f"{req_id_suffix}"
                )
            elif not model_is_unusable(DEFAULT_MODEL, available_models):
                # Same two-step shape as the branches around it (the error card
                # says "do both" under every entitlement row): the picker fixes
                # this session, the default stops the next one -- and here
                # 'auto' is served, so it is the natural value for the default.
                formatted = (
                    f"Your account does not have access to model '{unentitled}'. "
                    f"Available to you: {shown}{more}. Pick one of these in the "
                    f"model picker for this session, and change the default model "
                    f"under Settings → Chat if it is set to '{unentitled}' — set "
                    f"agent.model to 'auto' in ~/.kiro/crew/config.json to let the "
                    f"backend choose a model your plan includes. Retrying will not help."
                    f"{req_id_suffix}"
                )
            else:
                # A pinned model rejected on a partition that does not serve
                # ``auto`` either: recommending ``auto`` here would re-open the
                # circle the branch above closes, so only the picker is offered.
                formatted = (
                    f"Your account does not have access to model '{unentitled}'. "
                    f"Available to you: {shown}{more}. Pick one in the model picker "
                    f"for this session, and change the default model under "
                    f"Settings → Chat (agent.model in ~/.kiro/crew/config.json) if "
                    f"it is set to '{unentitled}'. Retrying will not help."
                    f"{req_id_suffix}"
                )
        # Bedrock model alias resolved to a version that is currently
        # unavailable (capacity throttle, region rollout in progress,
        # deprecated, etc.).
        elif _RE_USAGE_LIMIT.search(haystack):
            # Plan allowance exhausted. Quote the provider's own sentence rather
            # than paraphrasing it: it is the authoritative statement of WHICH
            # limit was hit, and the CLI shows exactly this, so the dashboard
            # matching it means the two surfaces cannot tell different stories.
            _limit_detail = _provider_detail(data)
            # The provider sentence has no trailing period, so add one before
            # appending guidance or the two run together as one sentence.
            if _limit_detail and _limit_detail[-1] not in ".!?":
                _limit_detail += "."
            formatted = (
                f"{_limit_detail} Retrying will not help until the limit resets. "
                f"Check your plan's usage allowance, or switch to a model or "
                f"account tier with remaining capacity."
                f"{req_id_suffix}"
            )
        elif _RE_CONTEXT_WINDOW_OVERFLOW.search(data):
            # The provider rejected the whole envelope it was sent. The formatter
            # is surface-blind: it cannot tell a first turn from a later one, so
            # it names neither and gives both remedies. Retrying on this native
            # session reproduces the failure either way; only the caller knows
            # which cause applies.
            formatted = (
                "The agent context exceeded the model window. "
                "Retrying on the same model session will not help. In an established "
                "conversation the accumulated history no longer fits, so start a fresh "
                "session; in a fresh session the always-loaded agent resources and "
                "tools already fill the window, so reduce them."
                f"{req_id_suffix}"
            )
        elif _RE_IMAGE_FORMAT_UNSUPPORTED.search(data):
            # Deterministic image rejection. A new attachment can be repaired by
            # the sender; an image retained in native history needs a fresh
            # conversation. The formatter is surface-blind, so it names no
            # dashboard-only command.
            formatted = (
                "The model could not process an image in this conversation. "
                "Retrying the unchanged conversation will fail again. If this "
                "turn attached the image, remove it or re-encode it as PNG or "
                "JPEG; otherwise start a new conversation so retained image "
                "data is not replayed."
                f"{req_id_suffix}"
            )
        elif _RE_MODEL_UNAVAILABLE.search(data):
            model = str(_RE_MODEL_UNAVAILABLE.search(data).group(1))  # type: ignore[union-attr]
            formatted = (
                f"Model '{model}' is unavailable on the backend right now "
                f"(capacity throttle or region rollout). Try: (1) pick a "
                f"different model in the model picker, {_auto_remedy(available_models)}"
                f"wait a minute and retry."
                f"{req_id_suffix}"
            )
        elif _RE_MODEL_TEMP_UNAVAILABLE.search(data):
            # Same capacity/rollout rejection as above in kiro-cli >= 2.16
            # wording, which names no model. Rewritten for the same reasons as
            # its named sibling: the provider's advice quotes the '/model' TUI
            # command, which does nothing in the dashboard, Slack, or a cron —
            # and the "is unavailable on the backend" prose keeps the
            # _TRANSIENT_MARKERS string fallback recognising it for free.
            formatted = (
                f"The selected model is unavailable on the backend right now "
                f"(capacity throttle or region rollout). Try: (1) pick a "
                f"different model in the model picker, {_auto_remedy(available_models)}"
                f"wait a minute and retry."
                f"{req_id_suffix}"
            )
        elif _RE_THROTTLE_NAMED.search(haystack) or _RE_THROTTLE_GENERIC.search(haystack):
            # Bedrock throttle / rate limit. Cover both AWS service exception
            # names and the generic phrasing the ACP backend sometimes uses.
            formatted = (
                "Bedrock is throttling requests. Try: (1) wait a few seconds and "
                "retry, or (2) switch to a different model in the picker (e.g. sonnet)."
                f"{req_id_suffix}"
            )
        elif is_credential_propagation_delay(haystack):
            # Not-yet-propagated credential (see is_credential_propagation_delay).
            # Ahead of the Bedrock-auth and session-expiry branches, which match
            # the same frame and would tell the operator to re-authenticate a
            # credential that is already valid. Names "credential-propagation
            # delay" because llm_helpers._TRANSIENT_MARKERS keys on that phrase,
            # so the string-fallback classifier recognises this wording too.
            formatted = (
                "The AWS credential was rejected as invalid — usually a transient IAM "
                "credential-propagation delay right after a credential is minted, in "
                "which case the same credential works within a few seconds. Retry in a "
                "moment; if it keeps failing the access key itself is invalid (deleted, "
                "rotated, or mistyped) — refresh your AWS credentials."
                f"{req_id_suffix}"
            )
        elif _RE_AUTH.search(haystack):
            # Bedrock auth failure — almost always missing/expired AWS
            # credentials.
            formatted = (
                "Bedrock authentication failed. Refresh your AWS credentials "
                "(e.g. re-run your SSO/login or 'aws sso login'), then retry. If "
                "the failure persists, check that the configured AWS profile has "
                "Bedrock InvokeModel access."
                f"{req_id_suffix}"
            )
        elif _is_session_expired(haystack):
            # Session expiry (401/403, or prose saying as much) — distinct from
            # the Bedrock credential errors above. Retrying or switching models
            # cannot succeed, so the message must not suggest either.
            #
            # The sign-in half comes from the harness's own declaration: which
            # command re-authenticates is a per-harness fact. This arm HAS
            # evidence -- a 401/403 or prose saying the session expired -- so it
            # takes the signed-out message, which is allowed to assert the state;
            # the standing caveat the panel renders unprobed is not.
            #
            # An empty *backend* resolves to the kiro declaration, because the
            # kiro id IS the empty string. That is correct rather than a fallback:
            # a caller reaching here with no backend in hand is on the shared
            # runtime, whose harnesses are the two on the host identity store.
            # The retry verdict stays hard-coded -- that is this arm's own finding
            # about the error, not sign-in advice.
            formatted = (
                "Your session has expired. "
                f"{host_auth.signed_out_message(backend)} "
                "Retrying or switching models will not help — this is a "
                "sign-in issue, not a backend error."
                f"{req_id_suffix}"
            )
        elif _RE_CONNECTION.search(haystack):
            formatted = (
                "Could not reach the model backend (connection refused, reset, "
                "or timed out). Retry in a moment. If it keeps happening, check "
                "that the backend endpoint is up and listening."
                f"{req_id_suffix}"
            )
        elif (
            _RE_5XX_NAMED.search(haystack)
            or _RE_5XX_STATUS.search(haystack)
            or _RE_5XX_HINT.search(haystack)
        ):
            # Transient backend 5xx — Bedrock/Codewhisperer surfaces a
            # momentary InternalServerError (often wrapped in a
            # CodewhispererChatResponseStream ServiceError with a
            # "please try again" hint). Distinct from throttling: this is a
            # server-side blip, not a rate limit, so the guidance is just to
            # retry rather than switch models or back off.
            #
            # Match the combined `data + message` haystack so a 5xx token in
            # either field is caught. Scanning `message` is safe
            # here precisely because we require a real transient *token* (named
            # exception, HTTP/status-50[0234]/529, or an explicit retry hint):
            # -32603's canonical message is literally "Internal error", which
            # carries no such token, so a bare uncaught -32603 (malformed-
            # request, context-length, deterministic backend bug) still falls
            # through to the unknown-shape branch rather than being mis-told to
            # retry a condition that will never succeed. A bare numeric like
            # "max_tokens 500" is likewise not a status code and won't match.
            formatted = (
                "The model backend hit a transient error (HTTP 5xx). This is "
                "usually momentary — retry in a moment. If it keeps happening, "
                "switch to a different model in the picker."
                f"{req_id_suffix}"
            )
        elif _RE_GENERATE_FAILED.search(data):
            # kiro-cli's generic pre-stream generation failure ("Kiro failed
            # to generate a response"): the backend call died before a
            # response stream existed, so there is no request_id and no error
            # class. Almost always a momentary model capacity / rollout blip
            # — same guidance as the 5xx branch. Kept as its own branch (not
            # folded into _RE_5XX_HINT) because it matches `data` only, so a
            # stray phrase echo in the JSON-RPC `message` can't flip an
            # otherwise-terminal error to transient.
            formatted = (
                "The model failed to generate a response (transient error — the "
                "backend call died before streaming started, usually a momentary "
                "capacity blip). Retry in a moment; if it keeps happening, switch "
                "to a different model in the picker."
                f"{req_id_suffix}"
            )
        elif _RE_PROCESS_FAILED.search(data):
            # kiro-cli's sibling wrapper ("The service failed to process the
            # request (request_id: ...)"): the backend call failed after the
            # turn was under way, so a request_id exists but no error class.
            # Same momentary-blip guidance as the branch above, scoped to
            # `data` for the same reason. The phrase is kept in the rewrite so
            # the string classifier in llm_helpers matches either form.
            formatted = (
                "The model backend failed to process the request (transient error, "
                "usually a momentary capacity blip). Retry in a moment; if it keeps "
                "happening, switch to a different model in the picker."
                f"{req_id_suffix}"
            )
        elif _RE_MALFORMED_REQUEST.search(data):
            # Structural rejection: the backend refused the payload
            # because of its shape, not a momentary fault. Retrying the same
            # payload will be rejected identically, so the guidance is to REPAIR
            # or reset the conversation rather than retry. /compact shrinks and
            # rebuilds the conversation; a new conversation drops whatever in the
            # accumulated context tripped the parser. Kept plain and
            # non-alarmist, matching the other branches. Matched against `data`
            # only via the shared _RE_MALFORMED_REQUEST, so a phrase echo in the
            # JSON-RPC `message` can't flip an unrelated error into this branch.
            #
            # Only `/compact` is named, because this formatter does not know
            # which surface renders its output and the reset command differs per
            # surface -- see docs/system-specs/common/error-handling.md.
            formatted = (
                "The request was rejected as malformed. This is a structural "
                "problem with the request, so retrying it as-is will not help. "
                "Try `/compact` to shrink and repair the conversation, or start "
                "a new conversation."
                f"{req_id_suffix}"
            )
        elif _PROMPT_BUSY_RE.search(data):
            # The backend still has an in-flight prompt on this session.
            # This means a previous turn didn't complete cleanly (tool stall,
            # timeout, or race between messages). The session will auto-recover
            # on the next attempt once the stale turn expires.
            #
            # Names no command, for the reason given in the malformed-request
            # branch above: this formatter is surface-blind. Telling the user to
            # "send `!restart`" would be wrong three ways -- it is a Slack-only
            # bang alias, it is owner-gated even there, and it restarts the
            # GATEWAY rather than the session. It is also unnecessary: the
            # handlers reset and re-queue on AcpPromptBusy by themselves (see
            # dashboard/chat_runner.py's retry-eligible branch).
            formatted = (
                "I'm still processing a previous request. Please wait a moment "
                "and try again; it clears on its own once the stale turn "
                "expires. If it persists, start a new conversation."
            )
        elif host_auth.reports_signed_out(backend, haystack):
            # The harness's OWN words for "no provider / no key", declared per
            # harness in ``host_auth``. Without this the answer fell through to the
            # branch below and reached the user as a raw -32603 frame that names
            # no fix; the declared message names the one that works.
            formatted = f"{host_auth.signed_out_message(backend)}{req_id_suffix}"
        else:
            # Unrecognised failure mode. Show the PROVIDER'S OWN message when
            # there is one — it is the true error, and the same words the CLI
            # prints, so the two surfaces agree. This is the path every provider
            # failure without a curated branch above takes; an over-broad 5xx
            # match would swallow such an error and report a momentary blip, so
            # the real cause would never reach the user at all.
            #
            # Falls back to the raw dict only when there is no usable detail
            # (empty/odd data), so a genuinely opaque shape still loses nothing.
            # Redaction below scrubs any embedded secrets either way.
            _detail = _provider_detail(data)
            if _detail:
                # Keep the JSON-RPC `message` too when it carries signal. For
                # -32603 it is the fixed boilerplate "Internal error" (pure
                # noise next to the provider text), but other codes use it as
                # the actual summary, and dropping it there would lose the only
                # description the error has.
                _summary = "" if message.strip().lower() == "internal error" else message.strip()
                if _summary and _summary.lower() not in _detail.lower():
                    formatted = f"{_summary}: {_detail}{req_id_suffix}"
                else:
                    formatted = f"{_detail}{req_id_suffix}"
            else:
                formatted = f"Prompt error: {error}"
    else:
        formatted = f"Prompt error: {error}"

    # Defense-in-depth: scrub any credentials or suspicious exfiltration URLs
    # that may have been embedded in the upstream provider response before
    # the message reaches dashboard / Slack / CLI surfaces.
    redacted, url_warnings = redact_exfiltration_urls(formatted)
    redacted, cred_warnings = redact_credentials(redacted)
    if url_warnings or cred_warnings:
        # Log so security review can spot when an upstream provider is
        # echoing sensitive content back. The warning lists are bounded
        # (one entry per match) and intentionally do NOT include the matched
        # values themselves — those have already been redacted.
        logger.warning(
            "ACP error contained sensitive content (scrubbed before raise): "
            "%d suspicious url(s), %d credential pattern(s)",
            len(url_warnings),
            len(cred_warnings),
        )
    return redacted


# ---------------------------------------------------------------------------
def _rejected_model_from_error(error: object) -> str | None:
    """Return the model id a prompt-time error reports as invalid/unavailable.

    Powers the reactive fallback in ``run_bg_oneliner``: on the SUBSTITUTE
    (background) path, a rejected model is retried once against the account's
    advertised list. Matches both the MPS ``Invalid model ID: X``
    ValidationException (including the ``auto`` sentinel where a partition does
    not serve it) and the ``The model 'X' is not
    available`` wording. Returns None when the error names no specific model.
    """
    if not isinstance(error, dict):
        return None
    data = f"{error.get('data', '')} {error.get('message', '')}"
    m = _RE_INVALID_MODEL_ID.search(data) or _RE_MODEL_UNAVAILABLE.search(data)
    return m.group(1) if m else None


def _raise_acp_error(
    error: object,
    available_models: Sequence[str] | None = None,
    *,
    backend: str = "",
) -> None:
    """Format and raise the appropriate :class:`AcpError` for *error*.

    Delegates formatting to ``_format_acp_error``. Prompt-busy uses
    ``AcpPromptBusy``; other failures use ``AcpError`` and receive any
    structural, model, auth, or usage tags from their raw frame.

    *available_models* is passed to BOTH the formatter and the transient
    classifier so a model-rejection's wording and its retry verdict are decided
    from the same evidence.

    *backend* only reaches the formatter, where an auth-expiry needs the failing
    harness's own sign-in message. It defaults to empty, which is the KIRO id
    rather than a sentinel: a caller with no backend in reach is on the shared
    runtime, and both harnesses there resolve tokens from kiro-cli's own store,
    so kiro's message is the right answer for it. The retry verdict does not
    depend on it.
    """
    formatted = _format_acp_error(error, available_models, backend=backend)
    # Detect prompt-busy from the raw error (before formatting rewrites it)
    raw_data = ""
    if isinstance(error, dict):
        raw_data = f"{error.get('data', '')} {error.get('message', '')}"
    if _PROMPT_BUSY_RE.search(raw_data):
        raise AcpPromptBusy(formatted)
    err = AcpError(formatted, transient=_is_transient_raw_error(error, available_models))
    # Tag deterministic STRUCTURAL rejections so self-driving callers can
    # stop resending identical context. Keep the classifier data-scoped: a phrase
    # echoed only in JSON-RPC ``message`` cannot stamp an unrelated error. Four
    # answers carry the tag: context-window overflow, malformed request, image
    # validation failure, and kiro-cli's own refusal of a request it could not
    # shrink to fit -- each reproduced exactly by re-sending the same context.
    raw_data_field = str(error.get("data", "") or "") if isinstance(error, dict) else ""
    _context_overflow = bool(_RE_CONTEXT_WINDOW_OVERFLOW.search(raw_data_field))
    _image_format_unsupported = bool(_RE_IMAGE_FORMAT_UNSUPPORTED.search(raw_data_field))
    if (
        _context_overflow
        or _RE_MALFORMED_REQUEST.search(raw_data_field)
        or _image_format_unsupported
        or _RE_OVERSIZED_REQUEST.search(raw_data_field)
    ):
        err.structural_terminal = True
    if _context_overflow:
        # Explicit even though the shared classifier reaches the same verdict:
        # subtype metadata and same-session retry eligibility stay inseparable.
        err.transient = False
        err.context_overflow = True
    if _image_format_unsupported:
        err.image_format_unsupported = True
    # Tag a model-rejection so the SUBSTITUTE (background) retry layer can pick a
    # served model; harmless on every other error (attributes just stay unset).
    rejected = _rejected_model_from_error(error)
    if rejected:
        err.rejected_model = rejected
        err.advertised = list(available_models or [])
    # Tag a session-expiry / rejected-credential answer so the dashboard can offer
    # the fix -- the Kiro sign-in card -- instead of a Continue that hits the same
    # wall. Decided from the raw frame, never from the prose, and only when the
    # formatter would have reached its sign-in branch: a Bedrock-named credential
    # exception (`_RE_AUTH`, a different remedy) or a usage-limit answer that
    # happens to carry a 401/403 is not a Kiro sign-in problem. Gated on the
    # harness signing in through Crew's own identity (harness parity H6): for a
    # harness with its own credential store the card would sign in the wrong
    # thing, so its 401 stays a plain error carrying that harness's remedy.
    if (
        not rejected
        and backend in ACP_BACKENDS_HOST_AUTH_CALLBACK
        and _is_session_expired(raw_data)
        and not _RE_AUTH.search(raw_data)
        and not _RE_USAGE_LIMIT.search(raw_data)
    ):
        err.auth_required = True
    # Tag a spent plan allowance the same way: from the raw frame, and only when
    # the formatter reached its usage-limit branch -- an entitlement rejection
    # that happens to carry limit wording keeps its own (served-model) remedy.
    # The sign-in tag above already withholds itself for this wording, so the
    # two tags are exclusive and the row's kind is unambiguous.
    if _RE_USAGE_LIMIT.search(raw_data) and not _model_is_unentitled(
        raw_data_field, available_models
    ):
        err.usage_limit = True
    raise err
