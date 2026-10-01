"""Character budgets, model-window caps and whole-block background admission.

Every Crew context limit is a character count, never a token or cost estimate.
``_CONTEXT_BUDGET_BASE`` is the one discretionary allowance and each section cap is
a share of it (``_budget``); thread history and replay scale with the model window,
which never enlarges the background allowance. :func:`admit_background` is the
single place a session-context build spends that allowance: protected blocks are
kept whole and optional source blocks are admitted whole or omitted by name.

``_resolve_caps`` and ``_PROMPT_BUILD_EMBED_TIMEOUT_SECS`` stay on
:mod:`kiro_crew.context` because callers rebind them there; code here reads them
through the facade at call time.

New caps, window rules and admission policy belong here.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

logger = logging.getLogger("kiro_crew.context")

# Crew-owned background admission, in characters, independent of the model
# window. Reuse the former smallest-window budget for every ordinary session.
# Contract, explicit rules/preferences, replay and current request are separate;
# this is NOT a bound on the provider's full model input or a token estimate.
_CONTEXT_BUDGET_BASE = 33_000


def _budget(fraction: float) -> int:
    """A section char cap as a percentage of the budget base."""
    return int(_CONTEXT_BUDGET_BASE * fraction)


# Fixed per-section limits. Explicit readers may still use the activity caps;
# ordinary startup does not retrieve that activity. Complete user constraints
# are protected separately from optional retrieval and discovery.
_HISTORY_REFERENCE_BASE = 165_000
_HISTORY_BUDGET_CHARS = int(_HISTORY_REFERENCE_BASE * 0.21)
_MEMORY_PREFS_CAP = _budget(0.026)  # user preferences                     = 2.6%
_MEMORY_PROJECTS_CAP = _budget(0.039)  # active projects                      = 3.9%
_MEMORY_HISTORY_CAP = _budget(0.16)  # daily history (multi-tier decay)     = 16%
_LESSONS_CAP = _budget(0.226)  # learned corrections (high priority)  = 22.6%
# Startup rule allowance for the authored directive tier. Window-INDEPENDENT
# and deliberately NOT a share of ``_CONTEXT_BUDGET_BASE``: that base is the
# ordinary discretionary pool, and standing rules are not discretionary. The
# value restores the allowance a 1M-window session had before the base was
# pinned to its smallest-window value (165_000 * 0.226 = 37_290).
_LESSONS_STARTUP_CAP = 37_000
# Startup allowance for the ``pref.*`` semantic rows read complete on a fresh
# session. Window-INDEPENDENT for the same reason as the rule allowance above,
# and derived the same way: the semantic share (7.7%) of the 165_000 reference
# base a 1M-window session had before the base was pinned (165_000 * 0.077 =
# 12_705). Until now this block had NO cap below the model-safe ceiling, and it
# was the one startup block that had outgrown the rule budget (47.7K measured
# on one real store). Rows past it are deferred to memory_recall, not dropped.
_PREFS_STARTUP_CAP = 12_700
# Past findings the author marked as experience rather than as standing rules.
# A SEPARATE, deliberately smaller allowance instead of a share of
# ``_LESSONS_CAP``: the two tiers answer different questions, so a user with many
# findings must not be able to crowd out their own standing rules, and a user with
# many rules must not lose the findings budget. Both are window-independent.
_LESSON_EXPERIENCE_CAP = _budget(0.05)  # learned experience (on-demand tier)  = 5%


_SEMANTIC_MEMORY_CAP = _budget(0.077)  # semantic memory (vector)             = 7.7%
_EPISODIC_MEMORY_CAP = _budget(0.077)  # episodic memory (vector)             = 7.7%
_SKILLS_CAP = _budget(0.15)  # skills top-K block (lazy-loaded)     = 15%
_STEERING_CAP = _budget(0.10)  # steering resource files              = 10%
_PER_MESSAGE_CAP = 8_000  # truncate individual messages on fallback path
# Historical char cap for the episodic-memory block injected on new sessions
# (build_message). Bounds the top-8 episodic fragments; scaled down with the
# window at its call site but never exceeds this reference value.
_EPISODIC_INJECT_CAP = 3_000


@contextmanager
def _prompt_build_embedding_deadline(enabled: bool) -> Iterator[None]:
    """Carry one bounded embedding budget through a fresh prompt build."""
    if not enabled:
        yield
        return

    # Lazy on purpose: context.py already keeps the embedding backend behind
    # call-time seams so imports that only inspect context do not initialize it.
    from kiro_crew import context as ctx  # circular import: the facade imports this owner
    from kiro_crew.embeddings import (
        PRIORITY_INTERACTIVE,
        EmbeddingWork,
        embedding_work,
    )

    inherited_work = embedding_work.get()
    deadline = time.monotonic() + ctx._PROMPT_BUILD_EMBED_TIMEOUT_SECS
    if inherited_work is not None:
        deadline = min(deadline, inherited_work.deadline)
    work = EmbeddingWork(
        deadline=deadline,
        cancelled=(inherited_work.cancelled if inherited_work is not None else threading.Event()),
        priority=PRIORITY_INTERACTIVE,
    )
    token = embedding_work.set(work)
    try:
        yield
    finally:
        embedding_work.reset(token)


_COMPRESSED_HISTORY_CAP = int(_HISTORY_REFERENCE_BASE * 0.27)

# Fixed overhead reference; admission protects actual constraints, not a guess
# at their size. `_MAX_CONTEXT_CHARS` is derived from the single shared budget.
_PREAMBLE_HEADROOM = _budget(0.03)

# Model-window metadata remains available to replay and callers. It does not
# change the ordinary Crew background allowance.
_REFERENCE_WINDOW_TOKENS = 1_000_000
_MIN_CONTEXT_BUDGET_BASE = _CONTEXT_BUDGET_BASE
# The prompt-size estimate used elsewhere in the repo is four characters per
# token. Reserve seven eighths of that estimated model window for the agent
# prompt, request, optional context, and dense text. The three-budget floor keeps
# this emergency ceiling well above ordinary 33K admission on known 200K+ models.
_PROTECTED_CONTEXT_CHARS_PER_TOKEN = 4.0
_PROTECTED_CONTEXT_WINDOW_FRACTION = 0.125
_PROTECTED_CONTEXT_FLOOR = _CONTEXT_BUDGET_BASE * 3


@dataclass(frozen=True)
class _ResolvedCaps:
    """Fixed Crew section limits, in characters; never a full-input token budget."""

    base: int
    prefs: int
    projects: int
    memory_history: int
    lessons: int
    lessons_startup: int
    prefs_startup: int
    lesson_experience: int
    semantic: int
    episodic: int
    skills: int
    steering: int
    history_fallback: int
    per_message: int
    compressed_history: int
    preamble_headroom: int
    protected_context: int

    @property
    def max_context(self) -> int:
        """One shared admission budget; section limits do not add capacity."""
        return self.base


def _effective_window(window_tokens: int | None) -> int:
    """Resolve a usable context-window size, defaulting to the reference (1M).

    A ``None``/unset or non-positive window falls back to the reference window,
    NOT to a small default. This is deliberate: the default deployment runs
    ``provider=acp`` + ``model="auto"``, and the registry maps ``"auto"`` → 200K
    even though ACP auto actually runs a 1M-window model. Treating an
    unknown/auto window as the reference means ONLY an explicitly-selected
    smaller model scales the budget down — an unresolved window never silently
    shrinks the default deployment to 20%.
    """
    if not window_tokens or window_tokens <= 0:
        return _REFERENCE_WINDOW_TOKENS
    return window_tokens


@functools.lru_cache(maxsize=16)
def _resolve_caps_cached(window: int) -> _ResolvedCaps:
    # Window metadata never enlarges discretionary Crew context. Keep a single
    # derivation from the reference constants for existing diagnostic callers.
    base = _CONTEXT_BUDGET_BASE
    factor = 1.0

    def _scaled(reference_cap: int) -> int:
        return int(reference_cap * factor)

    return _ResolvedCaps(
        base=base,
        prefs=_scaled(_MEMORY_PREFS_CAP),
        projects=_scaled(_MEMORY_PROJECTS_CAP),
        memory_history=_scaled(_MEMORY_HISTORY_CAP),
        lessons=_scaled(_LESSONS_CAP),
        lessons_startup=_scaled(_LESSONS_STARTUP_CAP),
        prefs_startup=_scaled(_PREFS_STARTUP_CAP),
        lesson_experience=_scaled(_LESSON_EXPERIENCE_CAP),
        semantic=_scaled(_SEMANTIC_MEMORY_CAP),
        episodic=_scaled(_EPISODIC_MEMORY_CAP),
        skills=_scaled(_SKILLS_CAP),
        steering=_scaled(_STEERING_CAP),
        history_fallback=int(_HISTORY_BUDGET_CHARS * max(0.2, window / _REFERENCE_WINDOW_TOKENS)),
        per_message=int(_PER_MESSAGE_CAP * max(0.2, window / _REFERENCE_WINDOW_TOKENS)),
        compressed_history=int(
            _COMPRESSED_HISTORY_CAP * max(0.2, window / _REFERENCE_WINDOW_TOKENS)
        ),
        preamble_headroom=_scaled(_PREAMBLE_HEADROOM),
        protected_context=max(
            _PROTECTED_CONTEXT_FLOOR,
            int(window * _PROTECTED_CONTEXT_CHARS_PER_TOKEN * _PROTECTED_CONTEXT_WINDOW_FRACTION),
        ),
    )


def resolve_model_window(model: str | None) -> int | None:
    """Map a model string to a context window in tokens for budget scaling.

    Returns ``None`` (⇒ caps fall back to the 1M reference) for anything that is
    NOT a confidently-known smaller window:

    - ``""`` / ``None`` / ``"auto"``: the caller hasn't pinned a model. The
      default deployment runs ``provider=acp`` + ``model="auto"`` on a 1M-window
      model, so we must NOT scale down here — ``None`` keeps the reference.
    - An id the registry does not list: ``window()`` would default it to 200k,
      which would wrongly shrink an unknown model's budget. Return ``None`` so an
      unknown id keeps the full reference budget — UNLESS the id itself advertises
      a 1M window via a ``[1m]``/``-1m`` token (forward-compat), in which case we
      trust it as 1M.
    - A KNOWN model: its real registry window (e.g. Opus 4.8 200K ⇒ scale down).

    A context window is a property of the MODEL, not the provider serving it —
    Opus 4.8 is 200K whether reached via kiro-cli/``acp`` (the default provider)
    or ``claude_code`` — so membership and window are provider-independent (see
    ``model_registry.has_known_window`` / ``_WINDOW_INDEX``). kiro/acp model ids
    (``claude-opus-4.8``, ``claude-opus-4-8[1m]``, …) resolve because they are
    registry aliases. (An earlier draft gated membership on the caller's
    provider, which silently no-op'd the whole feature on the acp default.)
    """
    # Guard non-str (a mock/mis-shaped value from a caller) so the downstream
    # registry lookups can't raise on the context-build hot path.
    if not isinstance(model, str) or not model or model == "auto":
        return None
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    # Delegate to the central window authority: kiro-list cache > registry >
    # [1m] heuristic > None. It already returns None (not a silent 200k) for a
    # genuinely-unknown model, so an unrecognized id keeps the full reference
    # budget (via _effective_window(None)) rather than being wrongly shrunk —
    # exactly the guarantee this function existed to provide, now centralized.
    return ctx.model_registry.model_window(model)


def window_for_provider_client(client: object) -> int | None:
    """Resolve the active context window from a live provider client.

    Prefers a real usage-reported window via the provider's public
    :meth:`LLMProvider.context_window_tokens` accessor (0 until the backend has
    run a turn); otherwise derives it from the resolved model id on the inner
    ACP client (``client.client._model``) via :func:`resolve_model_window`.
    Returns ``None`` (⇒ 1M reference) for a client that exposes neither — a
    fail-safe that never shrinks the budget on missing data. Never raises: a
    mis-shaped/None client yields ``None``.

    Note: at a fresh (``is_new``) session — the only path that reads the budget —
    no turn has completed, so the live window is 0 and the model-id path is what
    actually resolves the window. The live-window branch covers later rebuilds.
    """
    # Prefer the provider ABC's public accessor (per-backend dispatch, safe 0
    # default) over reaching into private attrs — a new backend that reports its
    # window there is picked up without touching this function.
    getter = getattr(client, "context_window_tokens", None)
    if callable(getter):
        try:
            live = getter()
        except Exception:
            live = 0
        # bool is an int subclass; exclude it so a stray True can't read as 1 token.
        if isinstance(live, int) and not isinstance(live, bool) and live > 0:
            return live
    inner = getattr(client, "client", None)
    if inner is None:
        return None
    model = getattr(inner, "_model", "") or ""
    return resolve_model_window(model)


class ContextParts:
    """The ordered blocks of one session-context build, and which are protected.

    A protected block -- explicit preferences, rules, identity, steering, pinned
    skill bodies -- is kept whole below the model-safe ceiling; every other block
    competes for the discretionary allowance in :func:`admit_background`.
    """

    __slots__ = ("parts", "protected")

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.protected: set[int] = set()

    def append_required(self, text: str) -> None:
        # User rules, preferences and steering are not disposable background.
        self.protected.add(len(self.parts))
        self.parts.append(text)


def admit_background(
    blocks: ContextParts,
    *,
    protected_chars: int,
    max_context_chars: int,
    caps: _ResolvedCaps,
    compressed_history: str | None,
) -> str:
    """Join *blocks*, admitting background as whole source blocks.

    Never slices the joined prompt. Protected rules and preferences are outside the
    discretionary pool, and their overflow is reported rather than silently
    truncating a safety contract.
    """
    admitted: list[str] = []
    remaining = max(0, max_context_chars - protected_chars)
    omitted: set[str] = set()
    for index, part in enumerate(blocks.parts):
        if index in blocks.protected:
            admitted.append(part)
        elif part.startswith("[THREAD CONVERSATION HISTORY"):
            # Thread continuity has its own window-scaled allowance, not
            # the old-activity pool. Preserve framing and the newest tail.
            # The LLM-compressed variant is sized to the larger
            # ``compressed_history`` cap and opens with a verbatim thread
            # start; bounding it at the fallback cap would clip that head.
            thread_cap = caps.compressed_history if compressed_history else caps.history_fallback
            if len(part) > thread_cap:
                header, body = part.split("]\n", 1)
                marker = "[Older thread history omitted]\n"
                room = max(0, thread_cap - len(header) - 2 - len(marker))
                part = header + "]\n" + marker + body[-room:] if room else header + "]\n" + marker
                omitted.add("older thread history")
            admitted.append(part)
        elif len(part) <= remaining:
            admitted.append(part)
            remaining -= len(part)
        else:
            omitted.add("skills" if "[Skills:]" in part else "background context")
    if omitted:
        admitted.append(
            "[Context budget: omitted "
            + ", ".join(sorted(omitted))
            + "; use memory_recall for old memory and skill_search for skills.]\n\n"
        )
    if protected_chars > max_context_chars:
        logger.warning(
            "Protected context exceeds background budget: chars=%d budget=%d; "
            "mandatory content kept and model-safe lesson cap applied",
            protected_chars,
            max_context_chars,
        )
    return "".join(admitted)
