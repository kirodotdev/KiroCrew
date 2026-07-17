"""Dynamic context-budget scaling: section caps scale proportionally with the
active model's context window.

The historical caps (``_CONTEXT_BUDGET_BASE`` and its derived section caps) were
hand-tuned for a 1M-token window. This suite pins the contract that the SAME
percentage of the window is spent on memory/lessons/history regardless of model
size — e.g. a section that consumes 20% of a 1M window must consume 20% of a
200K window too (i.e. one-fifth the absolute chars).
"""

from __future__ import annotations

import pytest

from kiro_crew import context as ctx
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader


def _builder_for(store, tmp_path):
    return ctx.ContextBuilder(
        memory=store,
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )


# ── Reference window / base invariants ──────────────────────────────────────


def test_reference_window_is_one_million():
    # The frozen module-level caps are the 1M-reference values, so the reference
    # window must be 1M for the scale factor to be identity at 1M.
    assert ctx._REFERENCE_WINDOW_TOKENS == 1_000_000


def test_scaled_base_at_reference_window_equals_module_base():
    # Identity at the reference window: no drift from the historical 165k base.
    caps = ctx._resolve_caps(1_000_000)
    assert caps.base == ctx._CONTEXT_BUDGET_BASE


def test_all_caps_at_reference_window_equal_module_constants():
    # Byte-for-byte identity at the reference window — the default deployment
    # (which resolves to the 1M reference) is completely unchanged by scaling.
    caps = ctx._resolve_caps(1_000_000)
    assert caps.prefs == ctx._MEMORY_PREFS_CAP
    assert caps.projects == ctx._MEMORY_PROJECTS_CAP
    assert caps.memory_history == ctx._MEMORY_HISTORY_CAP
    assert caps.lessons == ctx._LESSONS_CAP
    assert caps.semantic == ctx._SEMANTIC_MEMORY_CAP
    assert caps.episodic == ctx._EPISODIC_MEMORY_CAP
    assert caps.skills == ctx._SKILLS_CAP
    assert caps.steering == ctx._STEERING_CAP
    assert caps.history_fallback == ctx._HISTORY_BUDGET_CHARS
    assert caps.compressed_history == ctx._COMPRESSED_HISTORY_CAP
    assert caps.preamble_headroom == ctx._PREAMBLE_HEADROOM
    assert caps.max_context == ctx._MAX_CONTEXT_CHARS


# ── Proportional scaling (the core spec) ─────────────────────────────────────


def test_caps_scale_linearly_to_one_fifth_at_200k():
    # 200K is one-fifth of the 1M reference, so every cap is one-fifth its
    # 1M value (the user's "20% of 1M → 20% applied to 200K" requirement).
    caps = ctx._resolve_caps(200_000)
    assert caps.memory_history == ctx._MEMORY_HISTORY_CAP // 5
    assert caps.lessons == ctx._LESSONS_CAP // 5
    assert caps.semantic == ctx._SEMANTIC_MEMORY_CAP // 5
    assert caps.episodic == ctx._EPISODIC_MEMORY_CAP // 5
    assert caps.prefs == ctx._MEMORY_PREFS_CAP // 5
    assert caps.projects == ctx._MEMORY_PROJECTS_CAP // 5


def test_percentage_of_window_is_invariant_across_models():
    # The whole point: a section's share of the WINDOW is the same on a 200K
    # model as on a 1M model, even though the absolute char count differs 5x.
    caps_1m = ctx._resolve_caps(1_000_000)
    caps_200k = ctx._resolve_caps(200_000)
    share_1m = caps_1m.lessons / 1_000_000
    share_200k = caps_200k.lessons / 200_000
    assert share_1m == pytest.approx(share_200k, rel=1e-3)


def test_larger_than_reference_window_scales_up():
    # A hypothetical 2M model gets double the caps (the scaling is not clamped
    # to shrink-only — it is genuinely proportional).
    caps = ctx._resolve_caps(2_000_000)
    assert caps.base == 2 * ctx._CONTEXT_BUDGET_BASE


# ── Global ceiling stays the sum of scaled section caps ──────────────────────


def test_global_ceiling_is_sum_of_scaled_section_caps():
    caps = ctx._resolve_caps(200_000)
    expected = (
        caps.compressed_history
        + caps.prefs
        + caps.projects
        + caps.memory_history
        + caps.semantic
        + caps.episodic
        + caps.lessons
        + caps.skills
        + caps.steering
        + caps.preamble_headroom
    )
    assert caps.max_context == expected


# ── Fail-safe fallbacks (must never shrink the default deployment) ───────────


def test_unknown_or_auto_window_falls_back_to_reference():
    # The default deployment runs provider=acp + model="auto"; the registry maps
    # "auto"→200K but ACP auto actually runs a 1M model. Resolving MUST NOT
    # silently shrink that deployment to 20% — an unresolved/auto window falls
    # back to the reference (1M) so only an EXPLICIT smaller model scales down.
    assert ctx._effective_window(None) == ctx._REFERENCE_WINDOW_TOKENS
    assert ctx._effective_window(0) == ctx._REFERENCE_WINDOW_TOKENS
    assert ctx._effective_window(-1) == ctx._REFERENCE_WINDOW_TOKENS


def test_tiny_window_is_floored_not_zeroed():
    # A pathologically small window must not collapse caps to ~0 (which would
    # inject an empty/degenerate context). A floor keeps memory usable.
    caps = ctx._resolve_caps(1_000)
    assert caps.base >= ctx._MIN_CONTEXT_BUDGET_BASE
    assert caps.lessons > 0
    assert caps.memory_history > 0


# ── End-to-end: build_session_context honours the resolved window ────────────


def test_build_session_context_scales_memory_for_small_window(tmp_path):
    ws = tmp_path / "ws"
    store = MemoryStore(workspace=ws)
    # A history far larger than a 200K model's scaled history cap but smaller
    # than the 1M cap, so the two windows must yield different truncation.
    big = "HISTLINE " * 6000  # ~54k chars
    store.write_projects("# Projects\n\n" + big)

    builder = _builder_for(store, tmp_path)

    ctx_1m = builder.build_session_context(model_window=1_000_000)
    ctx_200k = builder.build_session_context(model_window=200_000)

    # The 200K context must be strictly smaller — the projects section is
    # truncated harder under the scaled-down cap.
    assert len(ctx_200k) < len(ctx_1m)


# ── per_message cap scales with the history budget ───────────────────────────


def test_per_message_cap_scales_and_never_exceeds_history_budget():
    # Regression: _PER_MESSAGE_CAP was fixed at 8000 while history_fallback
    # scaled down, so on a 200K window one big recent message (~8000 chars)
    # exceeded the whole scaled history budget (~6930) and dropped ALL history.
    caps = ctx._resolve_caps(200_000)
    assert caps.per_message == ctx._PER_MESSAGE_CAP // 5
    # The call site clamps to min(per_message, budget); assert the raw scaled
    # value is itself below the reference per-message cap so scaling happened.
    assert caps.per_message < ctx._PER_MESSAGE_CAP


def test_small_window_still_injects_recent_history(tmp_path):
    # A single very large recent message must still produce SOME history on a
    # 200K model (the newest message, truncated) — not an empty history block.
    from kiro_crew.history import ConversationLog

    log = ConversationLog(base_dir=tmp_path / "hist")
    key = "thread-small-win"
    log.append(key, "user", "X" * 40_000)  # one message far bigger than 200K budget
    log.append(key, "assistant", "Y" * 40_000)

    builder = ctx.ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        conversation_log=log,
    )
    out = builder.build_session_context(session_key=key, model_window=200_000)
    # History block present and non-empty (the newest message, truncated to the
    # scaled budget) rather than dropped entirely.
    assert "THREAD CONVERSATION HISTORY" in out
    assert "…[truncated]" in out


# ── build_session_replay scales with the window ──────────────────────────────


def test_session_replay_budget_scales_with_window(tmp_path):
    from kiro_crew.history import ConversationLog

    log = ConversationLog(base_dir=tmp_path / "hist")
    key = "replay-scale"
    for i in range(60):
        log.append(key, "user", f"msg {i} " + "Z" * 2000)
        log.append(key, "assistant", f"reply {i} " + "W" * 2000)

    replay_1m = ctx.build_session_replay(log, key, model_window=1_000_000)
    replay_200k = ctx.build_session_replay(log, key, model_window=200_000)
    assert replay_1m and replay_200k
    # 200K replay is scaled to ~1/5 the budget, so materially shorter.
    assert len(replay_200k) < len(replay_1m)


def test_session_replay_default_window_unchanged(tmp_path):
    # No model_window ⇒ 1M reference ⇒ full _REPLAY_BUDGET_CHARS (unchanged).
    from kiro_crew.history import ConversationLog

    log = ConversationLog(base_dir=tmp_path / "hist")
    key = "replay-default"
    for i in range(60):
        log.append(key, "user", f"msg {i} " + "Z" * 2000)
    default = ctx.build_session_replay(log, key)
    explicit_1m = ctx.build_session_replay(log, key, model_window=1_000_000)
    assert default == explicit_1m


# ── resolve_model_window: model string → window tokens policy ────────────────
# Window is a property of the MODEL, not the serving provider, so the function
# takes no provider arg — it always resolves against the window-bearing registry
# index. This is the fix for the acp no-op bug (an earlier draft gated on the
# caller's provider, which is empty for acp and disabled scaling entirely).


def test_resolve_model_window_explicit_small_model():
    # An explicitly-selected 200K model resolves to its real window so caps
    # scale down.
    assert ctx.resolve_model_window("opus-4.8") == 200_000


def test_resolve_model_window_explicit_large_model():
    assert ctx.resolve_model_window("opus-4.8-1m") == 1_000_000


def test_resolve_model_window_auto_returns_none():
    # "auto" and empty must return None so caps fall back to the 1M reference —
    # NOT 200K (the registry's literal "auto" window). See _effective_window.
    assert ctx.resolve_model_window("auto") is None
    assert ctx.resolve_model_window("") is None
    assert ctx.resolve_model_window(None) is None


def test_resolve_model_window_unknown_model_returns_none():
    # An unrecognized id must NOT be treated as 200K (window()'s default) — that
    # would silently shrink an unknown model's budget. Unknown → None → reference.
    assert ctx.resolve_model_window("some-future-model-xyz") is None


def test_resolve_model_window_known_regardless_of_serving_provider():
    # Regression for the acp no-op: a known 200K model must resolve to 200K even
    # on the default acp deployment. Window is intrinsic to the model, not the
    # provider — has_known_window takes no provider arg and works for kiro/acp
    # model ids (they are registry aliases).
    assert ctx.resolve_model_window("opus-4.8") == 200_000
    assert ctx.model_registry.has_known_window("opus-4.8")
    # A kiro/acp-advertised dotted id also resolves (the default provider path).
    assert ctx.model_registry.has_known_window("claude-opus-4.8")
    assert ctx.resolve_model_window("claude-opus-4-8") == 200_000


def test_resolve_model_window_unknown_1m_id_scales_up():
    # Forward-compat: an unlisted id that clearly advertises a 1M window (via the
    # [1m] token) is trusted as 1M rather than falling back — parity with the
    # registry's window() heuristic.
    assert ctx.resolve_model_window("claude-future-9[1m]") == 1_000_000


# ── window_for_provider_client: live client → window tokens ──────────────────


class _FakeInner:
    def __init__(self, model=""):
        self._model = model


class _FakeClient:
    """Mimics an LLMProvider: public context_window_tokens() + inner .client."""

    def __init__(self, model="", window=0):
        self._window = window
        self.client = _FakeInner(model)

    def context_window_tokens(self) -> int:
        return self._window


def test_window_for_client_prefers_live_reported_window():
    # A real usage_update populated the live window — trust it verbatim, via the
    # public accessor (even when it disagrees with the model id).
    client = _FakeClient(model="opus-4.8-1m", window=200_000)
    assert ctx.window_for_provider_client(client) == 200_000


def test_window_for_client_falls_back_to_resolved_model_id():
    # No live window yet (0) — derive from the resolved model id.
    client = _FakeClient(model="opus-4.8", window=0)
    assert ctx.window_for_provider_client(client) == 200_000


def test_window_for_client_auto_model_returns_none():
    client = _FakeClient(model="auto", window=0)
    assert ctx.window_for_provider_client(client) is None


def test_window_for_client_ignores_bool_live_window():
    # bool is an int subclass; a stray True from a mis-typed stat must NOT be
    # read as a 1-token window. Falls through to the model id.
    client = _FakeClient(model="opus-4.8", window=True)  # type: ignore[arg-type]
    assert ctx.window_for_provider_client(client) == 200_000


def test_window_for_client_handles_missing_attrs():
    # A provider without an accessor / .client must not raise — returns None.
    assert ctx.window_for_provider_client(object()) is None
    assert ctx.window_for_provider_client(None) is None


def test_window_for_client_accessor_raises_is_swallowed():
    # If the public accessor raises, fall back to the model id — never propagate.
    class _Boom:
        client = _FakeInner("opus-4.8")

        def context_window_tokens(self):
            raise RuntimeError("stats not ready")

    assert ctx.window_for_provider_client(_Boom()) == 200_000


def test_window_for_client_non_str_model_returns_none():
    # A client whose _model is non-str (e.g. a test AsyncMock, or a mis-shaped
    # provider) must NOT raise — resolve returns None ⇒ the 1M reference.
    class _Weird:
        _model = object()  # not a str

    client = type("C", (), {"client": _Weird()})()
    assert ctx.window_for_provider_client(client) is None


def test_resolve_model_window_non_str_returns_none():
    assert ctx.resolve_model_window(object()) is None  # type: ignore[arg-type]
