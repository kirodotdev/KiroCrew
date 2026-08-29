"""Scanner worker pool -- one shared AcpRuntime, one session per scanner call.

Modeled on ``code_review_sage/sage_lib/review_pool.py``. Key properties:

* Reference-counted runtime lifecycle -- ``begin_batch()`` lazily spawns the
  shared kiro-cli subprocess, ``end_batch()`` kills it once the last scan
  drains. Overlapping scans share one runtime and the last one out tears
  it down. No idle-timeout background task -- the runtime lives exactly
  as long as at least one scan is in flight.

* Shared semaphore across concurrent batches -- ``max_concurrent`` caps
  total in-flight scanner sessions on the shared runtime regardless of
  how many scans are running. The default (9) matches the maximum
  parallel scanner wave -- 8 always-on scanners plus at most one
  conditional scanner (design XOR email). The ceiling (9) matches the
  default because the concurrent-scan guard in the UI blocks a second
  scan from starting while one is in flight, so the pool never needs
  to admit more than one wave's worth of sessions.

* Simpler than Sage: no per-tool audit hook, no follow-up transcript
  management, no batch-scoped effort overlay -- writing-review scans
  are single-turn scanner prompts with no tool calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

try:
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp.types import (
        EVENT_COMPLETE,
        EVENT_PERMISSION_REQUEST,
        EVENT_TEXT_CHUNK,
    )
except ImportError:  # pragma: no cover - standalone/test fallback
    AcpRuntime = None  # type: ignore[assignment,misc]
    EVENT_TEXT_CHUNK = "text_chunk"  # type: ignore[assignment]
    EVENT_COMPLETE = "complete"  # type: ignore[assignment]
    EVENT_PERMISSION_REQUEST = "permission_request"  # type: ignore[assignment]

logger = logging.getLogger(__name__)

AGENT_NAME = "writing-review-reviewer"
# Max concurrent scanner sessions on the shared runtime. The parallel scanner
# wave is at most 9: 8 always-on scanners (clarity, naturalness, structure,
# evidence, consistency, attribution, audience, readability) plus AT MOST one
# conditional scanner (design XOR email, selected by ``doc_type``). Synthesis
# runs after the wave, never in parallel with it. The concurrent-scan guard
# in ``Workspace.tsx`` blocks a second scan from starting while one is in
# flight, so the pool never needs headroom for a second wave -- default and
# ceiling collapse onto the same number. Setting both to 9 keeps the
# semaphore permissive of the full wave without permitting anything the
# workload cannot actually produce.
DEFAULT_MAX_CONCURRENT = 9
MAX_CONCURRENT_CEIL = 9
DEFAULT_TIMEOUT = 60.0
DEFAULT_RETRY = 1

# Cold-start attempts for the sandbox-backed worker spawn. Sandbox probes
# occasionally miss on the first spawn after a long idle; a bounded
# linear-backoff retry covers the documented transient without letting a
# genuinely broken sandbox loop forever.
_SANDBOX_SPAWN_ATTEMPTS = 4

# Preview budget for raw-text log lines emitted on parse failure. 500 chars
# is enough to see the truncation point (or the malformed region) on almost
# every failure we have observed while staying compact in gateway.log so a
# high scan volume does not drown the file. A tighter cap loses the actual
# failure locus; a wider cap dilutes the log.
_LOG_PREVIEW_BUDGET_CHARS = 500


def _truncate_for_log(text: str, budget: int = _LOG_PREVIEW_BUDGET_CHARS) -> str:
    """Return ``text`` capped at ``budget`` chars with an ``[+N more chars]`` tail.

    Distinct from ``textwrap.shorten`` (which collapses whitespace) and from a
    plain slice (which silently hides the fact that the rest of the text was
    dropped). The explicit tail is the point: an operator reading the log
    should never mistake the preview for the full response.
    """
    if len(text) <= budget:
        return text
    return f"{text[:budget]}...[+{len(text) - budget} more chars]"


def _app_root() -> str:
    return str(Path(__file__).parent)


class TruncatedResponseError(ValueError):
    """Raised when the LLM's response was cut off before the JSON object closed.

    Distinct from a plain ``ValueError`` / ``json.JSONDecodeError`` because
    truncation is a distinct failure mode: the model was working fine, it just
    ran out of output tokens. The right recovery is "ask for less" (tighten
    the prompt, cap findings, chunk the doc), not "the response is malformed".

    Subclasses :class:`ValueError` so any caller that already broadly catches
    ``ValueError`` (like the driver's existing ``_classify_scanner_failure``
    fallback) continues to work if the classifier is not updated in lockstep;
    the more-specific handler simply gives a better failure reason when it is.

    ``partial_findings`` carries any complete ``{...}`` finding objects the
    parser was able to salvage from the truncated body BEFORE the cutoff
    (Layer 1). Callers that catch this error can merge those with a retry's
    output rather than throwing them away — a truncation that stopped on the
    9th finding still leaves 8 real findings on the floor, and the model
    already paid the tokens to emit them. Empty list => nothing was
    salvageable.
    """

    def __init__(self, message: str, partial_findings: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.partial_findings: list[dict[str, Any]] = list(partial_findings or [])


def _count_unclosed_containers(text_up_to_error: str) -> int:
    """Return the net count of unclosed ``{`` / ``[`` (opens minus closes).

    Walks ``text_up_to_error`` character by character, tracking whether the
    walker is inside a JSON string (`"..."`) so a stray brace or bracket
    inside string content does not skew the count. Escape sequences
    (``\\"``) are honoured. A ``"`` toggles the in-string flag; braces
    and brackets only count when the walker is OUTSIDE a string.

    Returns a positive integer when at least one container is still open at
    the error position (this is truncation — the model stopped mid-object /
    mid-array) and zero when every container that was opened has been
    closed (this is a structural malformation like ``{"foo":}``, not
    truncation). Never returns a negative number for real JSON output,
    but a negative return is treated as "not truncation" by callers.
    """
    open_container_count = 0
    inside_string = False
    escape_next_char = False
    for character in text_up_to_error:
        if inside_string:
            if escape_next_char:
                escape_next_char = False
            elif character == "\\":
                escape_next_char = True
            elif character == '"':
                inside_string = False
            continue
        if character == '"':
            inside_string = True
        elif character in "{[":
            open_container_count += 1
        elif character in "}]":
            open_container_count -= 1
    return open_container_count


def _extract_complete_findings(raw_text: str) -> list[dict[str, Any]]:
    """Salvage every complete ``{...}`` finding object from a possibly-truncated body.

    Walks the raw response looking for the ``"findings"`` array and yields
    each object inside it that is fully closed at the same brace-depth it
    opened at. Objects whose closing brace never arrives (the model was
    truncated mid-object) are dropped; everything before that cutoff is
    preserved.

    Called from the truncation path of :func:`_parse_first_json_object` to
    attach the salvaged list to :class:`TruncatedResponseError` (Layer 1 of
    the four-layer defensive stack). Never raises -- a body with no
    findings key, no array, or no complete objects simply returns ``[]``.

    Uses the same string-aware / escape-aware state machine as
    :func:`_count_unclosed_containers`, so a ``}`` inside a JSON string
    value (``"has}brace"``) does not falsely close a finding.
    """
    findings_key_start_index = raw_text.find('"findings"')
    if findings_key_start_index < 0:
        return []
    array_start_index = raw_text.find("[", findings_key_start_index)
    if array_start_index < 0:
        return []
    complete_finding_objects: list[dict[str, Any]] = []
    walk_position = array_start_index + 1
    total_length = len(raw_text)
    while walk_position < total_length:
        # Skip whitespace and inter-object commas before the next candidate.
        while walk_position < total_length and raw_text[walk_position] in " \n\t\r,":
            walk_position += 1
        if walk_position >= total_length or raw_text[walk_position] != "{":
            break
        object_start_index = walk_position
        # Walk until the matching ``}`` at depth zero, honouring strings
        # and escapes exactly like ``_count_unclosed_containers``.
        depth = 1
        inside_string = False
        escape_next_char = False
        walk_position += 1
        while walk_position < total_length and depth > 0:
            character = raw_text[walk_position]
            if inside_string:
                if escape_next_char:
                    escape_next_char = False
                elif character == "\\":
                    escape_next_char = True
                elif character == '"':
                    inside_string = False
            elif character == '"':
                inside_string = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
            walk_position += 1
        if depth != 0:
            # Reached EOF still inside this object -- everything from
            # here on is truncated, stop trying.
            break
        try:
            parsed_finding = json.loads(raw_text[object_start_index:walk_position])
        except json.JSONDecodeError:
            # The object closed cleanly at bracket level but its contents
            # are not valid JSON (e.g. an unquoted key). Stop rather than
            # try to skip past it -- the position we would jump to is not
            # reliable and further "recovery" risks yielding garbage.
            break
        if isinstance(parsed_finding, dict):
            complete_finding_objects.append(parsed_finding)
    return complete_finding_objects


def _is_truncation_error(decode_error: json.JSONDecodeError, decoded_text: str) -> bool:
    """True when a JSON decode error looks like an EOF-mid-object cut-off.

    Two shapes count as truncation:

    1. **``Unterminated ...``** — the parser opened a string / array / object
       and hit EOF before the closing token. Unambiguous truncation, always
       flag it, regardless of position.
    2. **Structurally unclosed at end of input** — a walker over the full
       decoded text sees at least one un-matched ``{`` / ``[``, so the
       model was still inside an object / array when its output stopped.
       Distinguishes real truncation from a structurally-balanced but
       malformed body like ``{"foo":}`` (Spock #1), where every ``{`` has
       a matching ``}`` and the parser fails on the empty value slot —
       that is bad JSON, not a token-limit cutoff, and retrying with a
       stricter cap only wastes another scan.

    The walker runs over the FULL decoded text rather than just the
    prefix up to ``decode_error.pos``: for a cutoff like ``{"foo":`` the
    prefix and the full text are the same, but for a malformed body like
    ``{"foo":}`` the error position sits at the ``}`` (char 7) and the
    prefix ``{"foo":`` alone would falsely look truncated. Walking the
    whole text sees the balancing close and correctly reports zero.
    """
    if decode_error.msg.startswith("Unterminated"):
        return True
    return _count_unclosed_containers(decoded_text) > 0


def _parse_first_json_object(raw_text: str) -> dict[str, Any]:
    """Return the first ``{...}`` JSON object in ``raw_text`` as a dict.

    Robust against two common LLM output patterns:

    * **Preamble** — ``Here is your response:\\n{"findings":[]}``. Skipped
      by finding the first ``{``.
    * **Trailing commentary** — ``{"findings":[]}\\n\\nLet me know if...``.
      ``JSONDecoder.raw_decode`` returns the first complete object and the
      index it stopped at; we discard everything after.

    Raises:
        TruncatedResponseError: The response ended mid-object or mid-string,
            which almost always means the model hit its ``max_output_tokens``
            ceiling and stopped emitting. Callers can classify this
            differently from a plain "bad JSON" because a retry with a
            shorter prompt (or a request for fewer findings) is the right
            recovery, not "the model is broken".
        ValueError: No JSON object could be found, or the first ``{`` starts
            an object that is malformed for a reason other than truncation.
    """
    if not raw_text or not raw_text.strip():
        logger.warning("scanner response parse failed: empty response body")
        raise ValueError("empty scanner response")
    first_brace_index = raw_text.find("{")
    if first_brace_index < 0:
        logger.warning(
            "scanner response parse failed: no JSON object; raw=%s",
            _truncate_for_log(raw_text),
        )
        raise ValueError("scanner response contained no JSON object")
    text_after_first_brace = raw_text[first_brace_index:]
    decoder = json.JSONDecoder()
    try:
        parsed_object, _end_index = decoder.raw_decode(text_after_first_brace)
    except json.JSONDecodeError as decode_error:
        if _is_truncation_error(decode_error, text_after_first_brace):
            partial_findings = _extract_complete_findings(text_after_first_brace)
            logger.warning(
                "scanner response truncated (%s at char %d); salvaged=%d; raw=%s",
                decode_error.msg,
                decode_error.pos,
                len(partial_findings),
                _truncate_for_log(text_after_first_brace),
            )
            raise TruncatedResponseError(
                "scanner response was truncated mid-output "
                f"(len={len(text_after_first_brace)}, {decode_error.msg}"
                f" at char {decode_error.pos})",
                partial_findings=partial_findings,
            ) from decode_error
        logger.warning(
            "scanner response malformed (%s at char %d); raw=%s",
            decode_error.msg,
            decode_error.pos,
            _truncate_for_log(text_after_first_brace),
        )
        raise
    if not isinstance(parsed_object, dict):
        logger.warning(
            "scanner response was JSON but not an object; raw=%s",
            _truncate_for_log(text_after_first_brace),
        )
        raise ValueError("scanner response was JSON but not an object")
    return parsed_object


class _BatchRuntimeHolder:
    """Own the shared AcpRuntime for the pool -- reference-counted.

    ``begin_batch`` (0 -> 1 running scans) lazily spawns one runtime.
    ``end_batch`` decrements; when it drains to 0 the runtime is killed
    and its RSS reclaimed. Concurrent/overlapping scans share the runtime
    and keep it alive until the last ``end_batch``. All state guarded by
    ``_lock``. Copied from Sage's ``_BatchRuntimeHolder`` with the same
    "spawn FIRST, then count" ordering so a spawn failure never leaves a
    counter stuck above zero.
    """

    def __init__(self, agent: str, work_dir: str) -> None:
        self._agent = agent
        self._work_dir = work_dir
        self._runtime: Any = None
        self._batches = 0
        self._lock = asyncio.Lock()

    async def begin_batch(self) -> None:
        async with self._lock:
            await self._ensure_runtime_locked()
            self._batches += 1

    async def end_batch(self) -> None:
        async with self._lock:
            self._batches = max(0, self._batches - 1)
            runtime_to_kill: Any = None
            if self._batches == 0:
                runtime_to_kill = self._runtime
                self._runtime = None
        if runtime_to_kill is not None:  # kill outside the lock -- SIGTERM->SIGKILL can block
            await self._kill(runtime_to_kill)

    async def acquire(self) -> Any:
        """Return the live shared runtime, spawning/self-healing if needed."""
        async with self._lock:
            return await self._ensure_runtime_locked()

    async def force_shutdown(self) -> None:
        async with self._lock:
            runtime_to_kill = self._runtime
            self._runtime = None
            self._batches = 0
        if runtime_to_kill is not None:
            await self._kill(runtime_to_kill)

    async def _ensure_runtime_locked(self) -> Any:
        if AcpRuntime is None:
            raise RuntimeError("AcpRuntime unavailable (kiro_crew.acp.runtime not importable)")
        current_runtime = self._runtime
        if current_runtime is not None and current_runtime.is_alive():
            return current_runtime
        # Stale runtime -- kill outside the lock later via caller path, but we
        # need a fresh one now, so drop the reference and spawn a new one.
        if current_runtime is not None:
            await self._kill(current_runtime)
        # Sandbox backend probe runs on a background thread; on the first spawn
        # after runtime death (which happens every batch under our Sage-clone
        # lifecycle) the probe's per-event-loop cache is cold and wrap_argv
        # raises SandboxUnavailableError with the "transient - retry" hint.
        # Retry a handful of times with a small backoff so a real user scan
        # is not killed by the race. Re-import here to avoid a top-level
        # import cycle (sandbox module transitively depends on config).
        try:
            from kiro_crew.sandbox import SandboxUnavailableError as _SandboxUnavailable
        except ImportError:  # pragma: no cover - defensive
            _SandboxUnavailable = RuntimeError  # type: ignore[assignment,misc]
        for attempt_index in range(_SANDBOX_SPAWN_ATTEMPTS):
            new_runtime = AcpRuntime(
                agent=self._agent, work_dir=self._work_dir, sandbox_mode="auto"
            )
            try:
                await new_runtime.spawn()
                break
            except _SandboxUnavailable as sandbox_error:
                if attempt_index == _SANDBOX_SPAWN_ATTEMPTS - 1:
                    raise
                logger.warning(
                    "sandbox probe cold on spawn attempt %d/%d; retrying: %s",
                    attempt_index + 1,
                    _SANDBOX_SPAWN_ATTEMPTS,
                    sandbox_error,
                )
                await asyncio.sleep(0.4 * (attempt_index + 1))
        self._runtime = new_runtime
        return new_runtime

    async def _kill(self, runtime_to_kill: Any) -> None:
        try:
            await runtime_to_kill.kill(expected=True)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            logger.debug("runtime kill error", exc_info=True)


class ScannerPool:
    """Dispatch scanner prompts as isolated ACP sessions on a shared runtime.

    Sized by ``max_concurrent`` (default 9, ceiling ``MAX_CONCURRENT_CEIL``
    which is also 9); the semaphore is shared across all in-flight scans
    so the ceiling is the true concurrency cap regardless of how many
    scans are running.
    """

    def __init__(
        self,
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        scanner_timeout_seconds: float = DEFAULT_TIMEOUT,
        retry_on_worker_death: int = DEFAULT_RETRY,
    ) -> None:
        self._max_concurrent = max(1, min(max_concurrent, MAX_CONCURRENT_CEIL))
        self._timeout = scanner_timeout_seconds
        self._retry = retry_on_worker_death
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        self._holder = _BatchRuntimeHolder(agent=AGENT_NAME, work_dir=_app_root())
        self._closed = False

    @property
    def is_closed(self) -> bool:
        """Whether the pool has been shut down and can no longer accept work."""
        return self._closed

    def resize(self, max_concurrent: int) -> None:
        """Swap in a new semaphore reflecting the current settings value.

        In-flight sends keep the semaphore they already acquired; new
        sends use the resized one. Idempotent when the size is unchanged.
        Matches Sage's ``begin_batch`` resize pattern so a settings PATCH
        takes effect on the next scan without a gateway restart.
        """
        clamped = max(1, min(max_concurrent, MAX_CONCURRENT_CEIL))
        if clamped == self._max_concurrent:
            return
        self._max_concurrent = clamped
        self._semaphore = asyncio.Semaphore(clamped)

    async def begin_batch(self) -> None:
        if self._closed:
            raise RuntimeError("ScannerPool is shut down")
        await self._holder.begin_batch()

    async def end_batch(self) -> None:
        await self._holder.end_batch()

    async def _run_one_session(self, runtime: Any, prompt_text: str) -> str:
        """Create a session, prompt, collect text chunks, always destroy the session."""
        handle = await runtime.create_session(cwd=_app_root(), agent=None)
        try:
            collected_parts: list[str] = []
            async for event in handle.prompt(prompt_text, timeout=self._timeout):
                event_kind = getattr(event, "kind", None)
                if event_kind == EVENT_TEXT_CHUNK:
                    collected_parts.append(getattr(event, "text", "") or "")
                elif event_kind == EVENT_PERMISSION_REQUEST:
                    # Scanner has no tools, but defensively decline anything requested.
                    request_id = getattr(event, "request_id", "")
                    try:
                        await handle.approve_tool(request_id)
                    except Exception:  # noqa: BLE001 - best-effort defensive path
                        logger.debug("permission approve failed", exc_info=True)
                elif event_kind == EVENT_COMPLETE:
                    break
            return "".join(collected_parts)
        finally:
            try:
                await handle.destroy()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.debug("session destroy error", exc_info=True)

    async def dispatch(self, prompt_text: str) -> dict[str, Any]:
        """Send a prompt to an isolated session, return parsed JSON response.

        Retries up to ``self._retry`` times on session-creation / worker failure.
        Raises the last ``RuntimeError`` if all attempts fail. Must be called
        while a batch is open (``begin_batch``).

        JSON parsing uses ``JSONDecoder.raw_decode`` on the first ``{`` so
        trailing commentary that some models emit after ``{"findings":[...]}``
        does not fail the whole scanner call. Anything before the first ``{``
        (a preamble like ``Here is your response:``) is stripped.
        """
        if self._closed:
            raise RuntimeError("ScannerPool is shut down")

        async with self._semaphore:
            last_error: RuntimeError | None = None
            total_attempts = 1 + max(0, self._retry)
            for attempt_index in range(total_attempts):
                try:
                    runtime = await self._holder.acquire()
                    raw_text = await self._run_one_session(runtime, prompt_text)
                    return _parse_first_json_object(raw_text)
                except RuntimeError as error:
                    last_error = error
                    logger.warning(
                        "scanner dispatch attempt %d/%d failed: %s",
                        attempt_index + 1,
                        total_attempts,
                        error,
                    )
            assert last_error is not None  # loop always executes >= 1 attempt
            raise last_error

    async def shutdown(self) -> None:
        """Force-kill the shared runtime -- called on app disable / gateway shutdown."""
        self._closed = True
        await self._holder.force_shutdown()


# Process-wide singleton -- driver calls get_pool() before each batch.
_POOL: ScannerPool | None = None


def get_pool() -> ScannerPool:
    """Lazily create and return the process-wide scanner pool."""
    global _POOL
    if _POOL is None or _POOL.is_closed:
        _POOL = ScannerPool()
    return _POOL


async def shutdown_pool() -> None:
    """Tear down the singleton pool (called on gateway shutdown / app disable)."""
    global _POOL
    if _POOL is None:
        return
    pool_instance = _POOL
    _POOL = None
    await pool_instance.shutdown()
