"""Tests for the claude_code model list assembled by /api/models.

The dropdown is scoped to what the account can actually use: the backend's
advertised set is authoritative when present and the static registry is filtered
down to it, so a free-tier account is not offered flagship models it cannot run.
With no session live the last advertised snapshot stands in for it; only when
nothing was ever advertised (a first run) is the registry shown unfiltered,
since an empty advertised set cannot be told apart from "entitled to nothing".
"auto" always leads and is never filtered -- it is the configured-default
sentinel, not a served model.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from kiro_crew import model_registry
from kiro_crew.dashboard.handlers.agents import (
    _advertised_cc_models,
    _cc_models,
    _normalize_model_key,
)

# Canonical registry rows now lead the dropdown (replaces _CC_CURATED_MODELS).
_REGISTRY_NAMES = [r["model_name"] for r in model_registry.display_list("claude_code")]


def _request_with_providers(providers: dict) -> MagicMock:
    """Fake aiohttp request whose sessions.active_providers() yields `providers`.

    Mirrors the real SessionManager API (active_providers()) so the test can't
    pass against an attribute the production object doesn't have.
    """
    sessions = SimpleNamespace(active_providers=lambda: list(providers.values()))
    state = SimpleNamespace(sessions=sessions)
    req = MagicMock()
    req.app.__getitem__.return_value = state
    return req


def _FakeProvider(models, *, backend=None):
    """A provider double carrying a REAL capability record, not an identity flag.

    ``_advertised_cc_models`` selects a session by
    ``SessionCapabilities.resolves_model_from_advertised_list`` AND by
    ``model_id_namespace``, and ``capabilities_of`` requires a genuine record: a
    ``MagicMock(spec=...)``'s attributes are all truthy, so an attribute-shaped
    assertion would let this double claim every capability at once. Setting the
    real record is what makes the double describe a backend that exists.
    """
    from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE
    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.providers.acp import AcpProvider

    provider = MagicMock(spec=AcpProvider)
    provider.capabilities = capabilities_for(ACP_BACKEND_CLAUDE if backend is None else backend)
    provider.available_models.return_value = models
    return provider


class TestAdvertisedCcModels:
    def test_maps_modelid_name_description(self):
        # An unknown provider id (not in the registry) passes through unchanged.
        prov = _FakeProvider(
            [
                {"modelId": "claude-sonnet-4-6", "name": "Sonnet 4.6", "description": "Everyday"},
            ]
        )
        out = _advertised_cc_models(_request_with_providers({"s": prov}), "claude_code")
        assert out == [
            {
                "model_name": "claude-sonnet-4-6",
                "display_name": "Sonnet 4.6",
                "description": "Everyday",
            }
        ]

    def test_known_provider_id_kept_verbatim(self):
        # The advertised id is the value set_config_option accepts.
        prov = _FakeProvider(
            [
                {
                    "modelId": "global.anthropic.claude-opus-4-8[1m]",
                    "name": "Opus 4.8",
                    "description": "",
                },
            ]
        )
        out = _advertised_cc_models(_request_with_providers({"s": prov}), "claude_code")
        assert out[0]["model_name"] == "global.anthropic.claude-opus-4-8[1m]"

    def test_empty_when_no_active_sessions(self):
        assert _advertised_cc_models(_request_with_providers({}), "claude_code") == []

    def test_skips_provider_without_accessor(self):
        prov = _FakeProvider([])
        prov.available_models = None
        out = _advertised_cc_models(_request_with_providers({"s": prov}), "claude_code")
        assert out == []

    def test_skips_non_claude_providers(self):
        prov = _FakeProvider(
            [{"modelId": "claude-opus-5", "name": "Opus 5", "description": ""}],
            backend="",
        )
        out = _advertised_cc_models(_request_with_providers({"s": prov}), "claude_code")
        assert out == []

    def test_skips_a_codex_provider_holding_the_same_capability(self):
        """codex also resolves from its advertised list, and its ids are not claude's.

        Both harnesses hold ``resolves_model_from_advertised_list``, so the
        capability alone does not say whose list this is. Without the namespace
        gate a live codex session would answer the claude picker with codex ids,
        and claude-agent-acp refuses every one of them.
        """
        from kiro_crew.acp_backends import ACP_BACKEND_CODEX

        prov = _FakeProvider(
            [{"modelId": "gpt-5.4-codex", "name": "GPT-5.4 Codex", "description": ""}],
            backend=ACP_BACKEND_CODEX,
        )
        out = _advertised_cc_models(_request_with_providers({"s": prov}), "claude_code")
        assert out == []

    def test_a_live_label_is_clipped_to_the_cache_bound(self):
        """A live label past the cache's bound is clipped on the live path too."""
        big = model_registry.ADVERTISED_LABEL_MAX_CHARS
        prov = _FakeProvider(
            [{"modelId": "claude-big-1", "name": "N" * 10_000, "description": "D" * 10_000}]
        )
        out = _advertised_cc_models(_request_with_providers({"s": prov}), "claude_code")
        assert len(out) == 1
        assert out[0]["display_name"] == "N" * big
        assert out[0]["description"] == "D" * big
        rows = _cc_models(_request_with_providers({"s": prov}))
        assert all(len(r["display_name"]) <= big for r in rows)
        assert all(len(r["description"]) <= big for r in rows)
        big_row = next(r for r in rows if r["model_name"] == "claude-big-1")
        assert big_row["display_name"] == "N" * big

    def test_a_live_id_the_cache_refuses_never_reaches_the_picker(self):
        """An id past the cache's id bound is refused, so it never reaches the picker."""
        prov = _FakeProvider(
            [
                {
                    "modelId": "x" * (model_registry.ADVERTISED_MODEL_ID_MAX_CHARS + 1),
                    "name": "Huge",
                },
                {"modelId": "claude-ok-1", "name": "Ok"},
            ]
        )
        out = _advertised_cc_models(_request_with_providers({"s": prov}), "claude_code")
        assert [r["model_name"] for r in out] == ["claude-ok-1"]

    def test_a_live_session_with_only_refused_ids_falls_through_to_the_snapshot(self):
        """A live list whose every id is refused yields to the snapshot, not an empty picker."""
        model_registry.refresh_advertised_models("claude_code", ["claude-cached-1"])
        prov = _FakeProvider(
            [{"modelId": "x" * (model_registry.ADVERTISED_MODEL_ID_MAX_CHARS + 1), "name": "Huge"}]
        )
        out = _advertised_cc_models(_request_with_providers({"s": prov}), "claude_code")
        assert [r["model_name"] for r in out] == ["claude-cached-1"]


class TestColdPickerReadsTheSnapshot:
    """ "No live session" is not "nothing advertised".

    Every advertising session writes its list to the cross-session snapshot
    (``model_registry.advertised_models``). A cold dashboard, a gateway restart
    or a cold-start timeout must read it, or the picker offers registry rows the
    account cannot run and hides an id only the advertised half carries.
    """

    _UNCATALOGUED = "claude-not-in-the-registry-9[1m]"

    @staticmethod
    def _served_and_unserved() -> tuple[str, str]:
        """One registry row whose provider id is served, and one that is not."""
        rows = [
            r["model_name"]
            for r in model_registry.display_list("claude_code")
            if model_registry.to_provider_id(r["model_name"], "claude_code")
        ]
        return rows[0], rows[1]

    def _snapshot(self, monkeypatch, served_key: str) -> str:
        served_id = model_registry.to_provider_id(served_key, "claude_code")
        monkeypatch.setattr(
            model_registry,
            "_ADVERTISED_MODELS",
            {"claude_code": ["default", served_id, self._UNCATALOGUED]},
        )
        return served_id

    def test_cold_picker_filters_to_the_snapshot(self, monkeypatch):
        served, unserved = self._served_and_unserved()
        served_id = self._snapshot(monkeypatch, served)

        names = [m["model_name"] for m in _cc_models(_request_with_providers({}))]

        assert names[0] == "auto"
        assert served_id in names
        assert self._UNCATALOGUED in names
        assert unserved not in names

    def test_no_dashboard_state_reads_the_snapshot_too(self, monkeypatch):
        served, _ = self._served_and_unserved()
        served_id = self._snapshot(monkeypatch, served)
        request = MagicMock()
        request.app.__getitem__.side_effect = KeyError("state")

        names = [m["model_name"] for m in _advertised_cc_models(request, "claude_code")]

        assert names == ["default", served_id, self._UNCATALOGUED]

    def test_a_live_session_beats_the_snapshot(self, monkeypatch):
        served, _ = self._served_and_unserved()
        self._snapshot(monkeypatch, served)
        prov = _FakeProvider([{"modelId": "claude-live-only-1", "name": "Live"}])

        out = _advertised_cc_models(_request_with_providers({"s": prov}), "claude_code")

        assert [m["model_name"] for m in out] == ["claude-live-only-1"]

    def test_the_snapshot_is_read_per_namespace(self, monkeypatch):
        served, _ = self._served_and_unserved()
        self._snapshot(monkeypatch, served)

        assert _advertised_cc_models(_request_with_providers({}), "codex") == []


class TestCcModelsMerge:
    def test_registry_set_always_present_even_without_session(self):
        # No live provider and no snapshot (a first run) → nothing was ever
        # advertised, so entitlement is UNKNOWN and the full canonical registry is
        # shown unfiltered. An empty advertised set
        # cannot be distinguished from "this account gets nothing", and an empty
        # picker on a cold dashboard is worse than a superset.
        out = _cc_models(_request_with_providers({}))
        names = [m["model_name"] for m in out]
        assert "opus-4.8-1m" in names
        assert "opus-4.8" in names
        assert set(_REGISTRY_NAMES) <= set(names)
        # "auto" leads, not the registry's default-flagged flagship. The flag used
        # to sort a specific paid model to the top and present it as the default.
        assert names[0] == "auto"

    def test_advertised_set_filters_the_registry(self):
        """The advertised set is authoritative: unentitled registry rows go away.

        This is the free-tier case. If the registry led unconditionally and the
        adapter could only ADD, an account served two models would still be
        offered the full flagship list and only find out at prompt time.
        """
        prov = _FakeProvider(
            [{"modelId": "global.anthropic.claude-sonnet-4-6[1m]", "name": "Sonnet 4.6"}]
        )
        out = _cc_models(_request_with_providers({"s": prov}))
        names = [m["model_name"] for m in out]
        assert names[0] == "auto"
        assert "global.anthropic.claude-sonnet-4-6[1m]" in names
        # The flagship is in the registry but was NOT advertised → filtered out.
        assert "opus-4.8-1m" not in names
        assert "opus-4.8" not in names

    def test_advertised_native_name_wins_for_survivors(self):
        """The backend's OWN label wins over the registry's stale version.

        The registry pins ``claude-sonnet-4-6[1m]`` to "Sonnet 4.6 (1M context)",
        but Claude Code is authoritative for its own picker text: when it
        advertises a name for the id, that name is shown. The row's wire value
        stays the advertised id the backend accepts back.
        """
        prov = _FakeProvider(
            [
                {
                    "modelId": "global.anthropic.claude-sonnet-4-6[1m]",
                    "name": "Sonnet 5.5",
                    "description": "Most efficient",
                }
            ]
        )
        out = _cc_models(_request_with_providers({"s": prov}))
        row = next(m for m in out if m["model_name"] == "global.anthropic.claude-sonnet-4-6[1m]")
        assert row["display_name"] == "Sonnet 5.5"
        assert row["description"] == "Most efficient"

    def test_unknown_advertised_models_still_pass_through(self):
        # Forward-compat: a model the registry does not list is still offered when
        # the backend advertises it, otherwise a newly-served model is unreachable.
        prov = _FakeProvider(
            [
                {"modelId": "claude-opus-4-1", "name": "Opus 4.1", "description": ""},
                {"modelId": "claude-sonnet-4-5", "name": "Sonnet 4.5", "description": ""},
            ]
        )
        out = _cc_models(_request_with_providers({"s": prov}))
        names = [m["model_name"] for m in out]
        assert "claude-opus-4-1" in names
        assert "claude-sonnet-4-5" in names
        # And the unentitled registry flagship is gone.
        assert "opus-4.8-1m" not in names
        # "auto" still leads and is never filtered by entitlement -- it is the
        # configured-default sentinel, not a model the backend serves.
        assert names[0] == "auto"

    def test_configured_default_is_not_resurrected_when_unentitled(self):
        """A stale config pick must not outlive the entitlement.

        Force-including it would reintroduce exactly the unusable option the
        filter removes.
        """
        prov = _FakeProvider(
            [{"modelId": "global.anthropic.claude-sonnet-4-6[1m]", "name": "Sonnet 4.6"}]
        )
        out = _cc_models(_request_with_providers({"s": prov}), configured_default="opus-4.8-1m")
        names = [m["model_name"] for m in out]
        assert "opus-4.8-1m" not in names
        assert names[0] == "auto"

    def test_configured_default_still_included_when_nothing_advertised(self):
        # Entitlement unknown → trust the operator's config rather than dropping
        # their selected model from the picker.
        out = _cc_models(_request_with_providers({}), configured_default="some-custom-model")
        names = [m["model_name"] for m in out]
        assert "some-custom-model" in names
        assert names[0] == "auto"  # still after nothing, before everything else

    def test_no_duplicate_when_adapter_lists_known_model(self):
        # The adapter advertises provider ids that ARE in the registry; each
        # collapses to one row carrying the advertised wire id (registry display).
        prov = _FakeProvider(
            [
                {
                    "modelId": "global.anthropic.claude-sonnet-4-6[1m]",
                    "name": "Sonnet 4.6",
                    "description": "",
                },
                {
                    "modelId": "global.anthropic.claude-opus-4-8[1m]",
                    "name": "Opus 4.8",
                    "description": "",
                },
            ]
        )
        out = _cc_models(_request_with_providers({"s": prov}))
        names = [m["model_name"] for m in out]
        assert names.count("global.anthropic.claude-opus-4-8[1m]") == 1
        assert names.count("global.anthropic.claude-sonnet-4-6[1m]") == 1

    def test_registry_display_is_fallback_when_backend_sends_no_name(self):
        # The backend advertises the id but with NO usable name (name == id, the
        # ``_advertised_cc_models`` id-fallback). Then the registry's friendly
        # display name is kept, while the wire value stays the advertised id.
        prov = _FakeProvider(
            [
                {
                    "modelId": "global.anthropic.claude-opus-4-8[1m]",
                    "name": "",
                    "description": "",
                },
            ]
        )
        out = _cc_models(_request_with_providers({"s": prov}))
        opus48 = next(m for m in out if m["model_name"] == "global.anthropic.claude-opus-4-8[1m]")
        assert opus48["display_name"] == "Opus 4.8 (1M context)"

    def test_advertised_native_name_overrides_registry_display(self):
        # The backend DOES send a name for a known id: it wins over the registry's.
        prov = _FakeProvider(
            [
                {
                    "modelId": "global.anthropic.claude-opus-4-8[1m]",
                    "name": "Opus 5.5",
                    "description": "For complex work",
                },
            ]
        )
        out = _cc_models(_request_with_providers({"s": prov}))
        opus = next(m for m in out if m["model_name"] == "global.anthropic.claude-opus-4-8[1m]")
        assert opus["display_name"] == "Opus 5.5"
        assert opus["description"] == "For complex work"

    def test_configured_default_force_included(self):
        out = _cc_models(_request_with_providers({}), configured_default="custom-model-xyz")
        names = [m["model_name"] for m in out]
        assert "custom-model-xyz" in names

    def test_configured_default_not_duplicated_if_already_present(self):
        out = _cc_models(
            _request_with_providers({}),
            configured_default="opus-4.8-1m",
        )
        names = [m["model_name"] for m in out]
        assert names.count("opus-4.8-1m") == 1

    def test_configured_default_auto_does_not_insert_blank_row(self):
        # cc_model="auto" round-trips to "" (auto's provider id is empty), which
        # must NOT be inserted as a blank-named row at the top of the dropdown —
        # the "auto" registry row already covers it.
        out = _cc_models(_request_with_providers({}), configured_default="auto")
        names = [m["model_name"] for m in out]
        assert "" not in names
        assert all(m["model_name"] for m in out)
        # the canonical "auto" row is still present, exactly once.
        assert names.count("auto") == 1

    def test_two_advertised_ids_sharing_a_key_stay_distinct(self):
        # `fable` and `claude-fable-5` both fold to fable-5-1m through the registry,
        # but the backend advertises them as two models. Neither may overwrite the
        # other's label.
        prov = _FakeProvider(
            [
                {"modelId": "fable", "name": "Fable 5.1", "description": ""},
                {"modelId": "claude-fable-5", "name": "Fable 5", "description": ""},
                {"modelId": "opus", "name": "Opus 5.5", "description": ""},
            ]
        )
        out = _cc_models(_request_with_providers({"s": prov}))
        names = [m["model_name"] for m in out]
        assert names[0] == "auto"
        fable = next(m for m in out if m["model_name"] == "fable")
        assert fable["display_name"] == "Fable 5.1"
        fable5 = next(m for m in out if m["model_name"] == "claude-fable-5")
        assert fable5["display_name"] == "Fable 5"
        assert len(names) == len(set(names))

    def test_an_advertised_id_listed_twice_yields_one_row(self):
        prov = _FakeProvider(
            [
                {"modelId": "claude-fable-5", "name": "Fable 5", "description": ""},
                {"modelId": "claude-fable-5", "name": "Fable 5", "description": ""},
            ]
        )
        out = _cc_models(_request_with_providers({"s": prov}))
        names = [m["model_name"] for m in out]
        assert names.count("claude-fable-5") == 1

    def test_a_versioned_id_claims_the_registry_row_before_a_bare_alias(self):
        # The alias is advertised first, but it names Claude Code's current model,
        # so the versioned id takes the registry row and the alias keeps its own.
        prov = _FakeProvider(
            [
                {"modelId": "fable", "name": "Fable 5.1", "description": ""},
                {"modelId": "claude-fable-5", "name": "Fable 5", "description": ""},
            ]
        )
        out = _cc_models(_request_with_providers({"s": prov}))
        names = [m["model_name"] for m in out]
        claimer = next(m for m in out if m["model_name"] == "claude-fable-5")
        assert claimer["display_name"] == "Fable 5"
        alias = next(m for m in out if m["model_name"] == "fable")
        assert alias["display_name"] == "Fable 5.1"
        assert names.index("claude-fable-5") < names.index("fable")

    def test_a_bare_alias_without_a_native_label_shows_its_id(self):
        # An unlabelled advertised row carries its id as display_name, so the alias
        # must show that id, not the registry's versioned label for the same row.
        prov = _FakeProvider([{"modelId": "opus", "name": "opus", "description": ""}])
        out = _cc_models(_request_with_providers({"s": prov}))
        opus = next(m for m in out if m["model_name"] == "opus")
        assert opus["display_name"] == "opus"


class TestNormalizeModelKey:
    """`_normalize_model_key` routes through the canonical registry.

    Mirror of the frontend `normalizeModelKey` unit tests in
    `website/src/test/model.displayModel.test.ts` -- the two must agree, which is
    the whole point of folding through the shared `model_registry.json`.
    """

    def test_auto_default_and_unset(self):
        # auto/default fold to the sentinel; an unset id stays "" (distinct).
        assert _normalize_model_key(" auto ") == "auto"
        assert _normalize_model_key("default") == "auto"
        assert _normalize_model_key("DEFAULT") == "auto"
        assert _normalize_model_key("") == ""
        assert _normalize_model_key("   ") == ""

    def test_alias_key_and_provider_id_fold_to_one_key(self):
        # An alias, the canonical key, and the claude_code provider id (with or
        # without a routing prefix) all resolve to one canonical key, any case.
        assert _normalize_model_key("claude-opus-4.8") == "opus-4.8-1m"
        assert _normalize_model_key("Claude-Opus-4.8") == "opus-4.8-1m"
        assert _normalize_model_key("opus-4.8-1m") == "opus-4.8-1m"
        assert _normalize_model_key("opus") == "opus-4.8-1m"
        assert _normalize_model_key("global.anthropic.claude-opus-4-8[1m]") == "opus-4.8-1m"
        # The "fold a provider/partition prefix" case: a regional
        # profile id that is not itself a registry entry folds after the peel.
        assert _normalize_model_key("us.anthropic.claude-opus-4-8[1m]") == "opus-4.8-1m"

    def test_distinct_context_window_variants_stay_apart(self):
        # The old dot->dash fold made both of these `claude-opus-4-8`, equating a
        # 200K model with a 1M one. The registry lists them as separate entries.
        assert _normalize_model_key("claude-opus-4-8") == "opus-4.8"  # 200K
        assert _normalize_model_key("claude-opus-4.8") == "opus-4.8-1m"  # 1M
        assert _normalize_model_key("claude-opus-4-8") != _normalize_model_key("claude-opus-4.8")

    def test_kiro_distinct_models_stay_apart_via_acp_first_fold(self):
        # The claude_code index aliases these onto Sonnet/Opus 4.8 for dropdown
        # dedup, but kiro serves them as DISTINCT real models. Resolving the acp
        # index first (canonical_key's documented order) keeps them apart, so the
        # shared fold cannot equate a Haiku pin with Sonnet 4.6 (a real 1M->200K
        # swap the downgrade flag must catch).
        assert _normalize_model_key("claude-haiku-4.5") == "haiku-4.5"
        assert _normalize_model_key("claude-sonnet-4.5") == "sonnet-4.5"
        assert _normalize_model_key("claude-sonnet-4") == "sonnet-4"
        assert _normalize_model_key("claude-opus-4.6") == "opus-4.6-1m"
        assert _normalize_model_key("claude-sonnet-4.6") == "sonnet-4.6-1m"
        assert _normalize_model_key("claude-haiku-4.5") != _normalize_model_key("claude-sonnet-4.6")
        assert _normalize_model_key("claude-opus-4.6") != _normalize_model_key("claude-opus-4.8")
        # acp-only canonical keys resolve to themselves.
        assert _normalize_model_key("haiku-4.5") == "haiku-4.5"
        assert _normalize_model_key("opus-4.6-1m") == "opus-4.6-1m"

    def test_unregistered_id_uses_the_string_fold(self):
        # GPT/DeepSeek/Qwen and future models are absent from the (Anthropic-only)
        # registry, so they keep the historical trim/lowercase/dot->dash fold.
        assert _normalize_model_key("GPT-5.6") == "gpt-5-6"
        assert _normalize_model_key("deepseek-3.2") == "deepseek-3-2"
        assert _normalize_model_key("claude-opus-5") == "claude-opus-5"
