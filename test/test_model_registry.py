"""Tests for the canonical model registry reader."""

from __future__ import annotations

from kiro_crew import model_registry as mr


class TestModelRegistry:
    def test_to_provider_id_canonical_key(self):
        assert (
            mr.to_provider_id("opus-4.8-1m", "claude_code")
            == "global.anthropic.claude-opus-4-8[1m]"
        )

    def test_is_canonical_key_true_for_top_level_keys(self):
        # Top-level registry keys — the display-only ids the /api/models
        # fallback wrongly offered and the set-model guard must reject.
        assert mr.is_canonical_key("fable-5-1m") is True
        assert mr.is_canonical_key("opus-4.8-1m") is True
        assert mr.is_canonical_key("opus-4.8") is True
        # 'auto' is a registry key too; the set-model guard allows it separately.
        assert mr.is_canonical_key("auto") is True

    def test_is_canonical_key_false_for_aliases_and_unknowns(self):
        # kiro/acp ids are ALIASES (or unregistered), never top-level keys, so
        # they pass the guard unchanged.
        assert mr.is_canonical_key("claude-fable-5") is False
        assert mr.is_canonical_key("claude-opus-4.8") is False
        assert mr.is_canonical_key("claude-sonnet-5") is False
        assert mr.is_canonical_key("") is False

    def test_to_provider_id_identity_passthrough_for_provider_id(self):
        # An already-resolved provider id passes through unchanged (back-compat).
        pid = "global.anthropic.claude-opus-4-8[1m]"
        assert mr.to_provider_id(pid, "claude_code") == pid

    def test_to_provider_id_unknown_passes_through_unchanged(self):
        # An unrecognized value (real-but-unregistered Bedrock id, regional
        # profile, or future model) is passed through UNCHANGED — we never
        # silently rewrite an operator's explicit id to the flagship default.
        assert (
            mr.to_provider_id("us.anthropic.claude-opus-4-8[1m]", "claude_code")
            == "us.anthropic.claude-opus-4-8[1m]"
        )
        assert mr.to_provider_id("nonexistent-model", "claude_code") == "nonexistent-model"

    def test_corrupt_registry_default_translates_to_valid_provider_id(self, monkeypatch):
        # If model_registry.json is corrupt/missing, _REGISTRY is empty and the
        # indices resolve nothing — but the default()->to_provider_id chain must
        # STILL yield a valid Bedrock id, not the bare canonical key (which the
        # adapter/Bedrock would reject with -32603/400). This is the end-to-end
        # "a corrupt registry can't brick the provider" guarantee.
        monkeypatch.setattr(mr, "_REGISTRY", {}, raising=True)
        monkeypatch.setattr(mr, "_CANONICAL_INDEX", {}, raising=True)
        monkeypatch.setattr(mr, "_DEFAULTS", {}, raising=True)
        canonical = mr.default("claude_code")
        assert canonical == mr._FALLBACK_CANONICAL  # the bare key
        # The fallback key must translate to the paired VALID provider id.
        assert mr.to_provider_id(canonical, "claude_code") == mr._FALLBACK_PROVIDER_ID
        assert mr.to_provider_id(canonical, "claude_code") == (
            "global.anthropic.claude-opus-4-8[1m]"
        )

    def test_from_provider_id_empty_returns_empty_not_auto(self):
        # Empty means "no model", NOT the 'auto' canonical key.
        assert mr.from_provider_id("", "claude_code") == ""

    def test_window_unlisted_1m_id_heuristic(self):
        # Parity with the frontend: an unlisted [1m]/-1m id still gets 1M.
        assert mr.window("global.anthropic.claude-opus-9-9[1m]") == 1_000_000
        assert mr.window("claude-future-1m") == 1_000_000
        # A genuinely-unknown id now resolves to the 1M REFERENCE (never a silent
        # 200k): model_window returns None, the window() shim substitutes the
        # reference. This is the deliberate "never shrink an unknown model" rule.
        assert mr.window("something-else") == mr.REFERENCE_WINDOW_TOKENS
        assert mr.window("something-else") == 1_000_000

    def test_model_window_returns_none_for_unknown(self):
        # The central resolver returns None (not a guessed 200k) for a genuinely
        # unknown model, so the caller decides the fail-safe.
        assert mr.model_window("something-else") is None
        assert mr.model_window("gpt-does-not-exist") is None
        # …but a [1m] token is a real 1M signal (forward-compat).
        assert mr.model_window("claude-future-1m") == 1_000_000
        assert mr.window_source("claude-future-1m") == "heuristic"
        assert mr.window_source("something-else") == "unknown"

    def test_model_window_live_tokens_win(self):
        # A live usage_update.size wins over every static source.
        assert mr.model_window("opus-4.8-1m", live_tokens=123456) == 123456
        # bool is excluded (True must not read as 1 token).
        assert mr.model_window("opus-4.8-1m", live_tokens=True) == 1_000_000

    def test_supplementary_windows_cover_headless_non_anthropic_models(self):
        # Regression: non-Anthropic models kiro-cli serves (DeepSeek/Qwen/GLM/…)
        # are neither in the canonical registry nor [1m]-tagged, so without a
        # static floor a headless start (Slack/cron) that never seeds the
        # kiro-list cache resolves them to None ⇒ the 1M reference and
        # over-assembles context. The supplementary map is the floor.
        # Windows match kiro-cli --list-models context_window_tokens (2026-07).
        assert mr.model_window("deepseek-3.2") == 164_000
        assert mr.model_window("minimax-m2.5") == 196_000
        assert mr.model_window("minimax-m2.1") == 196_000
        assert mr.model_window("glm-5") == 200_000
        assert mr.model_window("gpt-5.6-sol") == 272_000
        assert mr.model_window("gpt-5.6-terra") == 272_000
        assert mr.model_window("gpt-5.6-luna") == 272_000
        assert mr.model_window("qwen3-coder-next") == 256_000
        assert mr.model_window("qwen3-coder-480b") == 256_000
        assert mr.model_window("glm-4.7-flash") == 128_000
        for m in ("deepseek-3.2", "minimax-m2.5", "glm-5", "gpt-5.6-terra",
                  "qwen3-coder-next", "glm-4.7-flash"):
            assert mr.has_known_window(m) is True
            assert mr.window_source(m) == "supplementary"

    def test_supplementary_bedrock_windows(self):
        # Bedrock/legacy ids (formerly model_tokens.json) resolve via the folded
        # supplementary map — the registry does not list them.
        assert mr.model_window("anthropic.claude-sonnet-4-20250514-v1:0") == 200_000
        assert mr.model_window("amazon.nova-pro-v1:0") == 300_000
        assert mr.model_window("claude-3-5-sonnet-20241022") == 200_000
        assert mr.window_source("amazon.nova-pro-v1:0") == "supplementary"
        # Longest-substring: a Bedrock profile id embedding the dotted name hits.
        assert mr.model_window("some-prefix/anthropic.claude-sonnet-4-20250514") == 200_000
        assert mr.has_known_window("amazon.nova-pro-v1:0") is True

    def test_window_shim_never_silent_200k(self):
        # The window() shim resolves unknown -> 1M reference (never a silent 200k
        # that would shrink an unknown model's budget). A model that really is
        # 200k (registry/supplementary) still reports 200k.
        assert mr.window("opus-4.8") == 200_000  # real registry 200k
        assert mr.window("amazon.nova-lite-v1:0") == 300_000  # real supplementary
        assert mr.window("nonexistent-zzz") == mr.REFERENCE_WINDOW_TOKENS

    def test_refresh_kiro_windows_overrides_and_extends_registry(self, tmp_path, monkeypatch):
        # The kiro-list cache wins over the static registry (corrects stale
        # values) AND covers models the registry lacks (GPT). Uses 'auto' for the
        # override case: the registry lists auto=200k but kiro serves it at 1M.
        # Manipulate the in-memory cache directly (restored in teardown) to avoid
        # a module reload that would leak state into other tests.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        saved = dict(mr._KIRO_WINDOWS)
        try:
            mr._KIRO_WINDOWS.clear()
            assert mr.model_window("auto") == 200_000  # stale registry literal
            assert mr.model_window("unlisted-model-zzz") is None  # neither registry nor supplementary
            # refresh does the in-memory update synchronously and returns True
            # when the cache changed (signalling the async caller to persist).
            changed = mr.refresh_kiro_windows(
                [
                    {"model_id": "auto", "context_window_tokens": 1000000},
                    {"model_id": "unlisted-model-zzz", "context_window_tokens": 272000},
                    {"model_id": "bad", "context_window_tokens": 0},  # skipped
                    {"model_id": "alsobad"},  # missing field, skipped
                ]
            )
            assert changed is True
            assert mr.model_window("auto") == 1000000  # cache corrects stale registry 200k
            assert mr.window_source("auto") == "kiro-list"
            assert mr.model_window("unlisted-model-zzz") == 272000
            assert mr.model_window("bad") is None  # 0 not cached
            # A no-op refresh (same data) returns False — no persist needed.
            assert mr.refresh_kiro_windows([{"model_id": "auto", "context_window_tokens": 1000000}]) is False
            # persist is a separate step (offloaded to an executor by the caller).
            mr.persist_kiro_windows()
            assert (tmp_path / "model_windows.json").is_file()  # persisted
        finally:
            mr._KIRO_WINDOWS.clear()
            mr._KIRO_WINDOWS.update(saved)

    def test_supports_effort_from_registry(self):
        assert mr.supports_effort("opus-4.8-1m") is True
        # auto entry has no supports_effort -> None (caller falls back).
        assert mr.supports_effort("auto") is None
        # unknown -> None
        assert mr.supports_effort("nonexistent") is None

    def test_kiro_dotted_aliases_resolve(self):
        # AIM-managed agents ship kiro dotted ids; they must map deterministically
        # (NOT fall back to the flagship), preserving e.g. agent-lite on sonnet.
        assert (
            mr.to_provider_id("claude-sonnet-4.6", "claude_code")
            == "global.anthropic.claude-sonnet-4-6[1m]"
        )
        assert (
            mr.to_provider_id("claude-opus-4.7", "claude_code")
            == "global.anthropic.claude-opus-4-7[1m]"
        )
        # Opus 4.6 has no Bedrock profile; alias collapses to the current flagship.
        assert (
            mr.to_provider_id("claude-opus-4.6", "claude_code")
            == "global.anthropic.claude-opus-4-8[1m]"
        )
        # bare 'opus'/'sonnet' aliases
        assert mr.to_provider_id("opus", "claude_code") == "global.anthropic.claude-opus-4-8[1m]"
        assert (
            mr.to_provider_id("sonnet", "claude_code") == "global.anthropic.claude-sonnet-4-6[1m]"
        )

    def test_legacy_dotted_ids_do_not_regress_to_flagship(self):
        # Models the OLD _CC_MODEL_ALIASES mapped to cheaper classes must NOT
        # silently resolve to the flagship Opus 4.8 1M (a cost regression).
        flagship = "global.anthropic.claude-opus-4-8[1m]"
        sonnet = "global.anthropic.claude-sonnet-4-6[1m]"
        # Sonnet/Haiku-class ids route to Sonnet (cheapest available), not Opus.
        for sid in (
            "claude-sonnet-4.5",
            "claude-sonnet-4.5-1m",
            "claude-sonnet-4",
            "claude-haiku-4.5",
        ):
            assert mr.to_provider_id(sid, "claude_code") == sonnet, sid
        # Opus 4.5 routes to the 200K Opus, not the 1M flagship.
        assert (
            mr.to_provider_id("claude-opus-4.5", "claude_code")
            == "global.anthropic.claude-opus-4-8"
        )
        # The -1m form of 4.6 no longer downgrades to 4.7; it maps to the flagship.
        assert mr.to_provider_id("claude-opus-4.6-1m", "claude_code") == flagship

    def test_fable_5_canonical_round_trip(self):
        # Fable 5 entry: canonical -> provider id -> canonical.
        assert (
            mr.to_provider_id("fable-5-1m", "claude_code")
            == "global.anthropic.claude-fable-5[1m]"
        )
        assert (
            mr.from_provider_id("global.anthropic.claude-fable-5[1m]", "claude_code")
            == "fable-5-1m"
        )

    def test_fable_5_aliases_resolve(self):
        expected = "global.anthropic.claude-fable-5[1m]"
        assert mr.to_provider_id("fable", "claude_code") == expected
        assert mr.to_provider_id("fable-5", "claude_code") == expected
        assert mr.to_provider_id("claude-fable-5", "claude_code") == expected

    def test_fable_5_window(self):
        assert mr.window("fable-5-1m") == 1_000_000

    def test_fable_5_supports_effort(self):
        assert mr.supports_effort("fable-5-1m") is True

    def test_fable_5_in_available_models(self):
        ids = mr.available_models("claude_code")
        assert "global.anthropic.claude-fable-5[1m]" in ids

    def test_bare_advertised_ids_fold_to_canonical_key(self):
        # claude-agent-acp advertises BARE ids (no "global.anthropic." prefix).
        # They must fold onto the canonical key via from_provider_id so the
        # dashboard dropdown does not show a duplicate row per model.
        assert mr.from_provider_id("claude-opus-4-8[1m]", "claude_code") == "opus-4.8-1m"
        assert mr.from_provider_id("claude-opus-4-8", "claude_code") == "opus-4.8"
        assert mr.from_provider_id("claude-opus-4-7[1m]", "claude_code") == "opus-4.7-1m"
        assert mr.from_provider_id("claude-sonnet-4-6[1m]", "claude_code") == "sonnet-4.6-1m"

    def test_fable_5_not_default(self):
        # Fable 5 is opt-in; Opus 4.8 stays default.
        assert mr.default("claude_code") == "opus-4.8-1m"

    def test_available_models_is_default_first(self):
        # The allowlist is default-first regardless of JSON key order: on the
        # 'auto' path settings.local.json omits the model key and the
        # claude-agent-acp adapter picks availableModels[0]. Adding Fable as the
        # first JSON entry must NOT make Auto sessions resolve to Fable.
        assert mr.available_models("claude_code")[0] == "global.anthropic.claude-opus-4-8[1m]"

    def test_auto_passes_through_empty(self):
        assert mr.to_provider_id("auto", "claude_code") == ""

    def test_window_by_canonical(self):
        assert mr.window("opus-4.8-1m") == 1_000_000
        assert mr.window("opus-4.8") == 200_000

    def test_window_by_provider_id(self):
        assert mr.window("global.anthropic.claude-opus-4-8[1m]") == 1_000_000

    def test_available_models_returns_provider_ids(self):
        ids = mr.available_models("claude_code")
        assert "global.anthropic.claude-opus-4-8[1m]" in ids
        assert "global.anthropic.claude-sonnet-4-6[1m]" in ids
        # 'auto' maps to "" and is excluded from the allowlist.
        assert "" not in ids

    def test_default_canonical(self):
        assert mr.default("claude_code") == "opus-4.8-1m"

    def test_from_provider_id_reverse_lookup(self):
        assert (
            mr.from_provider_id("global.anthropic.claude-opus-4-8[1m]", "claude_code")
            == "opus-4.8-1m"
        )

    def test_display_list_shape(self):
        rows = mr.display_list("claude_code")
        assert {"model_name", "display_name", "description"} <= set(rows[0])
        # default first
        assert rows[0]["model_name"] == "opus-4.8-1m"


class TestAcpProviderIds:
    """kiro-cli (the 'acp' provider) advertises BARE dotted ids (no
    'global.anthropic.' prefix, no '[1m]'), e.g. 'claude-opus-4.8', as seen in
    'kiro-cli chat --list-models'. Its session/set_model ONLY accepts those ids.
    After /api/models canonicalizes the picker to registry keys (e.g.
    'opus-4.8-1m'), the ACP factory MUST translate the canonical key back to the
    'acp' id via to_acp_id — otherwise kiro rejects it with 'The model
    'opus-4.8-1m' is not available'. These lock in that translation (model-id
    regression)."""

    def test_canonical_key_translates_to_kiro_id(self):
        # The exact regression: the picker sends 'opus-4.8-1m'; kiro needs the
        # bare dotted id.
        assert mr.to_acp_id("opus-4.8-1m") == "claude-opus-4.8"

    def test_all_canonical_keys_translate(self):
        expected = {
            "opus-4.8-1m": "claude-opus-4.8",
            "opus-4.8": "claude-opus-4.5",
            "opus-4.7-1m": "claude-opus-4.7",
            "sonnet-4.6-1m": "claude-sonnet-4.6",
            "fable-5-1m": "claude-fable-5",
        }
        for canonical, kiro_id in expected.items():
            assert mr.to_acp_id(canonical) == kiro_id, canonical

    def test_auto_translates_to_empty(self):
        assert mr.to_acp_id("auto") == ""
        assert mr.to_acp_id("") == ""

    def test_kiro_id_passes_through_unchanged(self):
        # An already-resolved kiro id is idempotent under translation.
        assert mr.to_acp_id("claude-opus-4.8") == "claude-opus-4.8"

    def test_unknown_model_passes_through_unchanged(self):
        # Non-Anthropic kiro models (GPT, DeepSeek, …) are not in the registry;
        # their ids must pass through so kiro can still select them.
        assert mr.to_acp_id("gpt-5.6-sol") == "gpt-5.6-sol"
        assert mr.to_acp_id("deepseek-3.2") == "deepseek-3.2"

    def test_distinct_kiro_aliases_are_NOT_downgraded(self):
        # REGRESSION GUARD: claude-haiku-4.5 / claude-sonnet-4.5 / claude-sonnet-4
        # are registry ALIASES of sonnet-4.6-1m (added only to fold
        # claude-agent-acp's advertised ids for dropdown dedup), and
        # claude-opus-4.6 is an alias of opus-4.8-1m. But kiro-cli serves each as
        # a DISTINCT real model. to_acp_id (unlike to_provider_id) must pass these
        # through unchanged — otherwise the Haiku-pinned kirocrew-knowledge agent
        # (and mcp_core subagents) silently run on Sonnet.
        assert mr.to_acp_id("claude-haiku-4.5") == "claude-haiku-4.5"
        assert mr.to_acp_id("claude-sonnet-4.5") == "claude-sonnet-4.5"
        assert mr.to_acp_id("claude-sonnet-4") == "claude-sonnet-4"
        assert mr.to_acp_id("claude-opus-4.6") == "claude-opus-4.6"
        # Contrast: to_provider_id DOES downgrade them on the claude_code path
        # (the claude backend has no Haiku), which is why the acp path needs its
        # own alias-blind translator.
        assert mr.to_provider_id("claude-haiku-4.5", "claude_code") == (
            "global.anthropic.claude-sonnet-4-6[1m]"
        )

    def test_kiro_id_folds_to_canonical_key(self):
        # Reverse: the bare kiro id resolves back to the canonical key (used by
        # /api/models to dedup the dropdown).
        assert mr.from_provider_id("claude-opus-4.8", "acp") == "opus-4.8-1m"
        assert mr.from_provider_id("claude-sonnet-4.6", "acp") == "sonnet-4.6-1m"

    def test_distinct_kiro_models_have_own_canonical_on_acp(self):
        # REGRESSION (adversarial review): haiku-4.5 / sonnet-4.5 / sonnet-4 /
        # opus-4.6 are DISTINCT kiro models but claude_code ALIASES of a 1M
        # canonical. On the acp path each must resolve to its OWN canonical (so
        # /api/models keeps them as separate pickable rows and _normalize_model
        # stores them distinctly) with its REAL window — while the claude_code
        # index still folds them for claude-agent-acp dropdown dedup/downgrade.
        cases = {
            # kiro id            acp canonical    window   cc fold (downgrade)
            "claude-haiku-4.5":  ("haiku-4.5",    200_000, "sonnet-4.6-1m"),
            "claude-sonnet-4.5": ("sonnet-4.5",   200_000, "sonnet-4.6-1m"),
            "claude-sonnet-4":   ("sonnet-4",     200_000, "sonnet-4.6-1m"),
            "claude-opus-4.6":   ("opus-4.6-1m", 1_000_000, "opus-4.8-1m"),
        }
        for kiro_id, (acp_canon, win, cc_canon) in cases.items():
            assert mr.from_provider_id(kiro_id, "acp") == acp_canon, kiro_id
            assert mr.from_provider_id(kiro_id, "claude_code") == cc_canon, kiro_id
            # Window is the model's REAL served window (not the 1M fold) — the
            # acp-index-first resolution in _registry_window.
            assert mr.model_window(kiro_id) == win, kiro_id
            assert mr.model_window(acp_canon) == win, acp_canon
            # Round-trip: the acp canonical translates back to the kiro id, so a
            # picked model actually RUNS on kiro (not the folded flagship).
            assert mr.to_acp_id(acp_canon) == kiro_id, acp_canon

    def test_distinct_models_are_selectable_rows(self):
        # All four distinct models appear as their own acp dropdown rows (not
        # collapsed) with their own display label.
        names = {r["model_name"] for r in mr.display_list("acp")}
        assert {"haiku-4.5", "sonnet-4.5", "sonnet-4", "opus-4.6-1m", "sonnet-4.6-1m"} <= names

    def test_available_models_returns_kiro_ids(self):
        ids = mr.available_models("acp")
        assert "claude-opus-4.8" in ids
        assert "claude-sonnet-4.6" in ids
        assert "" not in ids

    def test_default_first(self):
        # Default-first ordering matches the claude_code column.
        assert mr.available_models("acp")[0] == "claude-opus-4.8"


class TestCorruptRegistryFallback:
    """The hardcoded _FALLBACK_PROVIDER_IDS table must not drift from the JSON."""

    def test_fallback_table_matches_registry(self):
        # Every claude_code canonical key + alias in the loaded registry must map
        # to the SAME provider id in the hardcoded fallback table, so the
        # corrupt-registry path (empty index) rescues any persisted cc_model to a
        # valid provider id instead of leaking a bare canonical key to Bedrock.
        for canonical, entry in mr._REGISTRY.items():
            pid = entry.get("providers", {}).get("claude_code")
            if pid is None:
                continue
            keys = [canonical, *entry.get("aliases", [])]
            for k in keys:
                assert k in mr._FALLBACK_PROVIDER_IDS, (
                    f"{k!r} missing from _FALLBACK_PROVIDER_IDS (corrupt-registry "
                    f"rescue would leak the bare key to Bedrock)"
                )
                assert mr._FALLBACK_PROVIDER_IDS[k] == pid, (
                    f"_FALLBACK_PROVIDER_IDS[{k!r}] drifted from the registry "
                    f"({mr._FALLBACK_PROVIDER_IDS[k]!r} != {pid!r})"
                )

    def test_fallback_rescues_non_default_key_when_index_empty(self, monkeypatch):
        # Simulate a corrupt/missing registry: empty index. to_provider_id must
        # still rescue a NON-default canonical key (not just the flagship).
        monkeypatch.setattr(mr, "_CANONICAL_INDEX", {})
        assert (
            mr.to_provider_id("sonnet-4.6-1m", "claude_code")
            == "global.anthropic.claude-sonnet-4-6[1m]"
        )
        assert (
            mr.to_provider_id("opus-4.8-1m", "claude_code")
            == "global.anthropic.claude-opus-4-8[1m]"
        )


class TestAdvertisedModelCache:
    """The provider-advertised model cache: seed source + wire-id folding.

    Uses monkeypatch to isolate the module-global ``_ADVERTISED_MODELS`` per
    test (it is process-wide runtime state, like ``_KIRO_WINDOWS``).
    """

    def test_seed_falls_back_to_registry_on_cold_cache(self, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})
        # The registry allowlist, deduped so a base-window id never rides next to
        # its 1M sibling (the pre-dedup list still shipped both).
        assert mr.seed_available_models("claude_code") == mr._dedup_window_siblings(
            mr.available_models("claude_code")
        )

    def test_seed_drops_base_window_sibling_of_a_1m_id(self, monkeypatch):
        # The 4.8 fix: the registry emits both the [1m] and the 200K spelling of
        # Opus 4.8; the seed must carry only the 1M one so the adapter cannot
        # collapse a 4.8 pick to the base window.
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})
        seed = mr.seed_available_models("claude_code")
        assert "global.anthropic.claude-opus-4-8[1m]" in seed
        assert "global.anthropic.claude-opus-4-8" not in seed

    def test_seed_dedups_advertised_siblings_too(self, monkeypatch):
        # A backend that advertises BOTH spellings is deduped the same way.
        monkeypatch.setattr(
            mr,
            "_ADVERTISED_MODELS",
            {
                "claude_code": [
                    "global.anthropic.claude-opus-4-8[1m]",
                    "global.anthropic.claude-opus-4-8",
                    "global.anthropic.claude-sonnet-5",
                ]
            },
        )
        assert mr.seed_available_models("claude_code") == [
            "global.anthropic.claude-opus-4-8[1m]",
            "global.anthropic.claude-sonnet-5",
        ]

    def test_dedup_keeps_order_and_drops_exact_dupes(self):
        # No 1M sibling to collapse against → only exact duplicates removed,
        # order preserved.
        assert mr._dedup_window_siblings(["a", "b", "a", "c"]) == ["a", "b", "c"]

    def test_dedup_keeps_distinct_base_models(self):
        # Two different 1M models are both kept — dedup is per normalized base.
        ids = ["global.anthropic.claude-opus-5[1m]", "global.anthropic.claude-opus-4-8[1m]"]
        assert mr._dedup_window_siblings(ids) == ids

    def test_seed_prefers_advertised_when_warm(self, monkeypatch):
        served = [
            "global.anthropic.claude-opus-5[1m]",
            "global.anthropic.claude-opus-4-8[1m]",
        ]
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": served})
        assert mr.seed_available_models("claude_code") == served

    def test_refresh_reports_change_and_dedupes(self, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})
        assert mr.refresh_advertised_models("claude_code", ["a", "b", "a"]) is True
        assert mr.advertised_models("claude_code") == ["a", "b"]
        # Same set again → no change.
        assert mr.refresh_advertised_models("claude_code", ["a", "b"]) is False

    def test_refresh_empty_never_wipes_a_good_cache(self, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": ["a"]})
        assert mr.refresh_advertised_models("claude_code", []) is False
        assert mr.advertised_models("claude_code") == ["a"]

    def test_persist_round_trips(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})
        monkeypatch.setattr(mr, "_advertised_models_cache_path", lambda: tmp_path / "pm.json")
        mr.refresh_advertised_models("claude_code", ["global.anthropic.claude-opus-5[1m]"])
        mr.persist_advertised_models()
        # Reload into a fresh dict and confirm the sidecar was written.
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})
        mr._load_advertised_models()
        assert mr.advertised_models("claude_code") == ["global.anthropic.claude-opus-5[1m]"]

    def test_wire_id_folds_bare_onto_advertised_versioned(self, monkeypatch):
        # The core fix: a bare id the registry does not carry folds onto the
        # versioned [1m] id the backend advertised, so it stops collapsing to
        # the base window.
        monkeypatch.setattr(
            mr, "_ADVERTISED_MODELS", {"claude_code": ["global.anthropic.claude-opus-5[1m]"]}
        )
        assert (
            mr.resolve_wire_model_id("claude-opus-5", "claude_code")
            == "global.anthropic.claude-opus-5[1m]"
        )

    def test_wire_id_prefers_1m_when_both_spellings_advertised(self, monkeypatch):
        monkeypatch.setattr(
            mr,
            "_ADVERTISED_MODELS",
            {
                "claude_code": [
                    "global.anthropic.claude-opus-5",
                    "global.anthropic.claude-opus-5[1m]",
                ]
            },
        )
        assert (
            mr.resolve_wire_model_id("claude-opus-5", "claude_code")
            == "global.anthropic.claude-opus-5[1m]"
        )

    def test_wire_id_passthrough_when_cold_or_unmatched(self, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})
        # Cold cache → unchanged.
        assert mr.resolve_wire_model_id("claude-opus-5", "claude_code") == "claude-opus-5"
        # Warm but no normalized match → unchanged (never rewrites to a model
        # the provider does not serve).
        monkeypatch.setattr(
            mr, "_ADVERTISED_MODELS", {"claude_code": ["global.anthropic.claude-sonnet-4-6[1m]"]}
        )
        assert mr.resolve_wire_model_id("claude-opus-5", "claude_code") == "claude-opus-5"

    def test_wire_id_leaves_auto_and_empty_alone(self, monkeypatch):
        monkeypatch.setattr(
            mr, "_ADVERTISED_MODELS", {"claude_code": ["global.anthropic.claude-opus-5[1m]"]}
        )
        assert mr.resolve_wire_model_id("auto", "claude_code") == "auto"
        assert mr.resolve_wire_model_id("", "claude_code") == ""

    def test_wire_id_keeps_an_already_advertised_id(self, monkeypatch):
        served = "global.anthropic.claude-opus-4-8[1m]"
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": [served]})
        assert mr.resolve_wire_model_id(served, "claude_code") == served

    def test_to_provider_id_unknown_still_passes_through(self, monkeypatch):
        # Guard: the pure translation is unchanged — folding lives in
        # resolve_wire_model_id, not to_provider_id.
        monkeypatch.setattr(
            mr, "_ADVERTISED_MODELS", {"claude_code": ["global.anthropic.claude-opus-5[1m]"]}
        )
        assert mr.to_provider_id("claude-opus-5", "claude_code") == "claude-opus-5"
