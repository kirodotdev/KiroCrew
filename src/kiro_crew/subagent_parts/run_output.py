"""What a sub-agent run reports: redacted, bounded result and error text, the usage
line, the delivery record a completion carries, and the per-run credit accounting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kiro_crew.llm_helpers import provider_last_turn_usage

if TYPE_CHECKING:
    import time

    from kiro_crew.subagent import (
        MAX_ERROR_DETAIL_LEN,
        SubagentInfo,
        _billing_stats,
        _sum_usage,
        math,
        redact_and_truncate,
        redact_credentials,
        redact_exfiltration_urls,
    )


def _redact(text: str) -> str:
    """Redact credentials and exfiltration URLs from text."""
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _redact_and_truncate(text: str, max_chars: int) -> str:
    """Redact over the FULL text, then truncate (never ``_redact(x[:n])``).

    Truncating first can cut a credential in half at the boundary, leaving a
    fragment the redaction regexes do not match — the raw remainder would
    then leak into the surface this feeds. Delegates to the canonical helper.
    """
    return redact_and_truncate(text, max_chars)


# Bound for a rendered exception chain. The rendering reaches a WS frame, a
# tombstone and the Subagents panel, so it is capped rather than trusted -- to
# ``process_identity.MAX_ERROR_DETAIL_LEN``, the one bound every retained error
# field of a run's terminal record is held to.
_MAX_ERROR_CHAIN = 4


def _describe_exception(exc: BaseException) -> str:
    """Render *exc* as ``Type: message``, following its cause chain.

    A bare ``str(exc)`` drops the class, and for a whole family of failures the
    message alone cannot be attributed to a subsystem: ``bad parameter or other
    API misuse`` is unreadable prose until ``sqlite3.InterfaceError`` names
    what raised it. The module is included for anything outside ``builtins``,
    because the bare class name is frequently just as ambiguous as the message.

    The chain is followed because the outermost exception is often a generic
    wrapper whose ``__cause__`` holds the real fault. ``__context__`` is
    followed only when it was not suppressed, matching how a traceback decides
    the same question, so an unrelated exception that merely happened to be in
    flight is not reported as this one's cause.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(parts) < _MAX_ERROR_CHAIN:
        if id(current) in seen:
            break
        seen.add(id(current))
        cls = type(current)
        name = cls.__qualname__
        module = getattr(cls, "__module__", "")
        if module and module != "builtins":
            name = f"{module}.{name}"
        message = str(current).strip()
        parts.append(f"{name}: {message}" if message else name)
        nxt = current.__cause__
        if nxt is None and not current.__suppress_context__:
            nxt = current.__context__
        current = nxt
    return " <- caused by ".join(parts)[:MAX_ERROR_DETAIL_LEN]


_MAX_DONE_RESULT_LEN = 50_000  # cap subagent_done payload to avoid bloating WS frames


def _done_result(text: str) -> str:
    """Redact + cap result for inclusion in subagent_done event."""
    if not text:
        return ""
    redacted = _redact(text)
    if len(redacted) <= _MAX_DONE_RESULT_LEN:
        return redacted
    return "…(truncated)\n" + redacted[-_MAX_DONE_RESULT_LEN:]


def _finite_nonnegative_number(value: object) -> float | None:
    """Return a safe numeric telemetry value, excluding booleans and NaN/inf."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0.0 else None


def format_subagent_usage(credits: object, elapsed: object) -> str:
    """Format the terminal usage line shared by all subagent delivery surfaces."""
    credit_value = _finite_nonnegative_number(credits)
    elapsed_value = _finite_nonnegative_number(elapsed)
    if credit_value is None or elapsed_value is None:
        return ""
    # Match the dashboard's Math.round for non-negative elapsed telemetry;
    # Python's round uses ties-to-even and would render 12.5 differently.
    total_seconds = math.floor(elapsed_value + 0.5)
    elapsed_text = (
        f"{total_seconds // 60}m {total_seconds % 60}s"
        if total_seconds >= 60
        else f"{total_seconds}s"
    )
    # Zero also represents a provider that does not report credit billing.
    if credit_value == 0:
        return elapsed_text
    digits = 1 if credit_value >= 10 else 2
    return f"{credit_value:.{digits}f} credits · {elapsed_text}"


@dataclass(frozen=True)
class SubagentDelivery:
    """Terminal usage captured when a completion acquires delivery debt.

    *report_owed* marks the debt of a memory-wait expiry rather than a run: the
    store owes its report (``TaskStore.owed_reports``) until it reaches the
    parent, and it has no run folder, so settling it clears that mark
    (``taskq_clear_owed_reports``) and writes no ``delivered`` tombstone.

    *tombstone* is the record a restart orphan's notice owes: the fields the boot
    reconciler would have written, outcome included. Settling writes it in place
    of the ``delivered`` mark, so a cut-off or failed orphan is not relabelled
    completed, and *elapsed* and *credits* go unused.

    *leaked_process* marks a COMPLETED run whose teardown could not end its
    process (``SubagentInfo._leaked_process``). The ``delivered`` tombstone the
    settle writes then carries the flag, so the next start ends the survivor
    (``list_leaked_processes``); without it the tombstone ends the run and the
    process is reached by nothing.
    """

    agent_id: str
    elapsed: float
    credits: float
    report_owed: bool = False
    leaked_process: bool = False
    tombstone: dict[str, Any] | None = field(default=None, hash=False)


#: What ``on_orphan_notify`` returns when it queued an orphan's notice for the
#: parent agent AND took the tombstone that notice owes, which the drain of the
#: notice writes. Any other truthy value means delivered with no debt taken.
ORPHAN_NOTICE_OWED = "owed"


@dataclass
class _RunCreditAccounting:
    """Settle each attempted turn once, including consumer-side interruptions."""

    info: SubagentInfo
    provider: object | None = None
    stats_before: object | None = None
    pending: bool = False
    total: Any = field(default_factory=lambda: provider_last_turn_usage(None))

    def __post_init__(self) -> None:
        self.total.credits = self._valid_credits(self.info.credits) or 0.0

    @staticmethod
    def _valid_credits(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            credits = float(value or 0.0)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            return None
        return credits if math.isfinite(credits) and credits >= 0.0 else None

    def begin(self, provider: object) -> None:
        self.provider = provider
        self.stats_before = _billing_stats(provider)
        self.pending = True

    def settle(self, completion: object | None = None) -> None:
        if not self.pending:
            return
        self.pending = False
        event_usage = getattr(completion, "usage", None)
        usage = event_usage
        if usage is None or not hasattr(usage, "credits"):
            usage = provider_last_turn_usage(self.provider, since=self.stats_before)
        # Provider usage is an external boundary. Reject malformed credit values
        # before accumulation so they cannot poison persisted or JSON telemetry.
        if self._valid_credits(getattr(usage, "credits", None)) is None:
            return
        self.total = _sum_usage(self.total, usage)
        self.info.credits = self._valid_credits(self.total.credits) or 0.0


def _timeout_context(
    info: "SubagentInfo", *, include_elapsed: bool = True, turn_limit: int = 0
) -> str:
    """Build a human-readable context string for timeout errors.

    ``turn_limit`` is the resolved effective turn cap (per-spawn override →
    manager default → hardcoded). ``info.max_turns`` alone is only the raw
    per-spawn override, which is 0 when unset and would render a misleading
    ``turn N/0``. When no positive cap is known, the cap is omitted entirely.
    """
    limit = turn_limit or info.max_turns
    parts = [f"turn {info.turns}/{limit}" if limit > 0 else f"turn {info.turns}"]
    if info.last_tool:
        parts.append(f"last tool: {_redact(info.last_tool)}")
    if include_elapsed:
        elapsed = info.elapsed if info.elapsed > 0 else (time.time() - info.started)
        parts.append(f"elapsed: {int(elapsed)}s")
    return " | ".join(parts)
