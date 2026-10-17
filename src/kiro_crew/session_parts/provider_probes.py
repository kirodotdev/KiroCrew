"""Defensive reads of a provider's optional turn and identity probes.

Each probe answers for any object a session can hold -- a real provider, a warm-pool
double, a minimal stub -- and reads anything but an exact answer as the safe one, so
the drain, watchdog and recycle paths that ask can never mis-fire on a test double.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.session import LLMProvider


# There is no standalone ClaudeCodeProvider: Claude Code is driven through the
# ACP adapter. The name is kept
# (always None) so the legacy ``ClaudeCodeProvider is not None and
# isinstance(...)`` guards below short-circuit cleanly. The live path is
# ``_is_claude_backend``, on a harness the public build ships as selectable.
ClaudeCodeProvider = None  # type: ignore[assignment,misc]


def _watchdog_handle_of(provider: Any) -> Any | None:
    """The live ACP session handle behind *provider*, if it can rebind its watchdog.

    Two shapes carry one: ``AcpSessionProvider`` holds ``_handle`` directly, and
    ``AcpProvider`` wraps one as ``_client`` once its runtime is up. Anything
    else (a placeholder client, a fake provider in a test, a Claude driver) has
    no windows to re-clamp and is skipped.
    """
    for candidate in (provider, getattr(provider, "_client", None)):
        handle = getattr(candidate, "_handle", None)
        if handle is not None and callable(getattr(handle, "rebind_watchdog", None)):
            return handle
    return None


def _is_claude_provider(provider: Any) -> bool:
    """Honor the dynamically registered legacy Claude provider seam."""
    return bool(ClaudeCodeProvider is not None and isinstance(provider, ClaudeCodeProvider))


def _provider_effectively_alive(provider: Any) -> bool:
    """Whether a session's provider should be treated as live (NOT stale).

    Uses the process-level check (is_process_alive), not is_alive() which has a
    600s stale-activity threshold that falsely kills idle sessions. A CC
    per_session provider whose process has exited is still effectively alive:
    its session state is on disk and reconnects lazily on the next stream(), so
    it must not be evicted as stale.

    Used by the two post-acquire re-checks (the post-semaphore re-validate and
    the won-race re-validate). The in-lock live fast path keeps its own inline
    copy of this decision because it also evicts the stale entry and emits
    path-specific logging; that copy must stay in sync with this helper.

    Read straight off the declared ABC: ``LLMProvider.is_process_alive``
    defaults to ``is_alive()``, so every provider answers the call and no
    capability probe is needed (harness-parity H14).
    """
    alive = provider.is_process_alive()
    if (
        not alive
        and ClaudeCodeProvider is not None
        and isinstance(provider, ClaudeCodeProvider)
        and provider.connection_mode == "per_session"
    ):
        alive = True
    return alive


def _provider_uses_kiro_identity_store(provider: Any) -> bool:
    """Whether *provider*'s child authenticates from kiro-cli's identity store.

    Reads the capability the object DECLARES (harness-parity H14) rather than
    probing private attributes: ``LLMProvider`` declares it with a safe default of
    False, ``AcpProvider`` / ``AcpSessionProvider`` grant it by membership in
    ``backends_retired_by_host_logout()``, and ``AcpRuntime`` declares the same
    property under the same name because the sweep reaches shared runtimes too.

    Fails CLOSED on anything that does not declare it -- a test double or a future
    holder is left running rather than recycled on a store it may never read.
    """

    declared = getattr(provider, "uses_kiro_identity_store", False)
    return declared is True


def _provider_has_active_turn(provider: LLMProvider) -> bool:
    """True only if ``provider`` reports a real in-flight turn.

    Real providers implement ``has_active_turn()`` as a synchronous method that
    returns a plain ``bool``. This helper guards the shutdown-drain path against
    (a) providers that don't implement it (warm-pool doubles, minimal stubs) and
    (b) test doubles whose auto-generated attribute returns a coroutine
    (``AsyncMock``) — calling that would otherwise leak an un-awaited coroutine
    warning. Anything that is not exactly ``True`` is treated as "no active
    turn", so the drain is a strict opt-in that can never mis-fire on a double.
    """
    fn = getattr(provider, "has_active_turn", None)
    if not callable(fn):
        return False
    try:
        res = fn()
    except Exception:
        return False
    if inspect.isawaitable(res):
        # A double returned an awaitable instead of a bool — close it to avoid
        # a RuntimeWarning and treat as "no active turn".
        close = getattr(res, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        return False
    return res is True


def _provider_background_launch(provider: LLMProvider) -> tuple[float, str] | None:
    """``provider``'s newest background launch as ``(seconds since, description)``.

    ``None`` unless the provider answers with exactly that shape. Same defensive
    shape as :func:`_provider_has_active_turn`: the probe is optional, a raising
    one reads as "nothing launched", and an ``AsyncMock``-style double's
    coroutine is closed. A ``MagicMock`` attribute answers a ``MagicMock``,
    which is not a tuple, so a double can never hold the watchdog off.
    """
    fn = getattr(provider, "background_launch", None)
    if not callable(fn):
        return None
    try:
        res = fn()
    except Exception:
        return None
    if inspect.isawaitable(res):
        close = getattr(res, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        return None
    if not (isinstance(res, tuple) and len(res) == 2):
        return None
    age, description = res
    if (
        isinstance(age, bool)
        or not isinstance(age, (int, float))
        or not isinstance(description, str)
    ):
        return None
    return float(age), description


def _context_pct_is_unknown(provider: LLMProvider) -> bool:
    """True only if ``provider`` reports its 0% context reading as unknown.

    Mirrors :func:`_provider_has_active_turn`'s defensive shape: the probe is
    optional (stubs and warm-pool doubles need not implement it), and an
    ``AsyncMock``-style double that returns a coroutine is closed rather than
    left to raise a RuntimeWarning. Anything that is not exactly ``True`` reads
    as "the percentage is trustworthy", keeping the caller's recycle decision
    fail-quiet on a double.
    """
    fn = getattr(provider, "context_usage_unknown", None)
    if not callable(fn):
        return False
    try:
        res = fn()
    except Exception:
        return False
    if inspect.isawaitable(res):
        close = getattr(res, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        return False
    return res is True


def _provider_has_unfinished_turn(provider: LLMProvider) -> bool:
    """True only if ``provider`` reports a native turn that has not reached its
    done boundary — INDEPENDENT of cancel state (unlike
    :func:`_provider_has_active_turn`).

    The shutdown drain filters on THIS, not ``has_active_turn``. A turn that has
    already been ``session/cancel``'d but whose native turn-done ack has not yet
    arrived reports ``has_active_turn() is False`` yet still holds kiro-cli's
    native-session lock open; killing the process now reproduces the
    empty-response-after-restart bug. Reporting it as "unfinished" keeps
    it in the drain set so the ack is waited on before teardown.

    Same defensive guard as :func:`_provider_has_active_turn`: providers that
    don't implement the method (warm-pool doubles, minimal stubs) or doubles
    whose auto-generated attribute returns a coroutine (``AsyncMock``) are
    treated as "no unfinished turn", so the drain can never mis-fire on a
    double.
    """
    fn = getattr(provider, "has_unfinished_turn", None)
    if not callable(fn):
        return False
    try:
        res = fn()
    except Exception:
        return False
    if inspect.isawaitable(res):
        close = getattr(res, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        return False
    return res is True


def _provider_turn_is_known_terminal(provider: LLMProvider) -> bool:
    """True only when *provider* explicitly reports no unfinished native turn.

    Missing, raising, or awaitable stand-ins are unknown rather than terminal.
    The deadline path may treat ``no_turn`` as settled only with this independent
    done-boundary proof; collapsing unknown to False would recreate the original
    false-success race on minimal providers and test doubles.
    """
    fn = getattr(provider, "has_unfinished_turn", None)
    if not callable(fn):
        return False
    try:
        res = fn()
    except Exception:
        return False
    if inspect.isawaitable(res):
        close = getattr(res, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        return False
    return res is False
