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
        assert mr.window("something-else") == 200_000

    def test_supports_effort_from_registry(self):
        assert mr.supports_effort("opus-4.8-1m") is True
        # auto entry has no supports_effort -> None (caller falls back).
        assert mr.supports_effort("auto") is None
        # unknown -> None
        assert mr.supports_effort("nonexistent") is None

    def test_kiro_dotted_aliases_resolve(self):
        # AIM-managed agents ship kiro dotted ids; they must map deterministically
        # (NOT fall back to the flagship), preserving e.g. meshclaw-lite on sonnet.
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
