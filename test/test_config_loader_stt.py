"""The ``stt`` section: the on-device defaults and what the loader makes of a stored value.

Streaming and voice input are on by default with a downloadable model; a retired
provider degrades to ``local`` and says so once; fields the retired providers
needed are inert; ``stt.model`` always resolves onto a catalog name; and the
millisecond knobs are clamped to the floors the recogniser enforces.
"""

from __future__ import annotations

import json
import logging
import unittest.mock
from pathlib import Path

import pytest

import kiro_crew.config.loader as loader_module
from kiro_crew.config.loader import (
    STT_PROVIDER_LOCAL,
    KiroCrewConfig,
    SttConfig,
    _validated_stt_model,
    _validated_stt_provider,
)
from kiro_crew.stt import limits as stt_limits
from kiro_crew.stt import models as stt_models


def _loaded_stt(tmp_path: Path, section: dict) -> SttConfig:
    """Load a config carrying *section* under ``stt`` and return the parsed block.

    Goes through ``KiroCrewConfig.load()`` rather than constructing an
    ``SttConfig``: every coercion under test here (the provider degrade, the model
    canonicalisation, the millisecond clamps) lives on the load path, so a
    dataclass built by hand skips all of them and proves nothing about what a
    hand-edited ``config.json`` actually produces.
    """
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"stt": section}))
    with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
        return KiroCrewConfig.load().stt


#: Every superseded ``stt.model`` name, and the catalog entry it must select.
#: Written out as literals on purpose: reading the expectations out of the loader's
#: own alias table would assert only that a dict maps to itself, and the rows worth
#: guarding are the ones that look like substitutions and are not. ``medium`` and
#: the whole full-size ``large`` lineage land on ``large-v3-turbo``, which is at
#: least as accurate as what was asked for and decodes faster; the English-only
#: names drop to the multilingual model of the SAME size rather than to the
#: default, which is the difference between losing a little English accuracy and
#: silently demoting someone from the accuracy ceiling to the second-smallest model.
_SUPERSEDED_STT_MODELS: dict[str, str] = {
    "turbo": "large-v3-turbo",
    "large": "large-v3-turbo",
    "large-v1": "large-v3-turbo",
    "large-v2": "large-v3-turbo",
    "large-v3": "large-v3-turbo",
    "large-v3-turbo-q5_0": "large-v3-turbo",
    "large-v3-turbo-q8_0": "large-v3-turbo",
    "medium": "large-v3-turbo",
    "medium.en": "large-v3-turbo",
    "small.en": "small",
    "base.en": "base",
    "tiny.en": "tiny",
}

#: Fields ``stt`` carried while each retired provider needed its own out-of-band
#: install: a whisper CLI path, two HuggingFace repo ids, and a compute device.
_REMOVED_STT_FIELDS = ("whisper_path", "mlx_model", "parakeet_model", "device")


class TestSttStreamingDefaultsOn:
    """Pin the fresh-install default for ``stt.streaming`` to True.

    The default recogniser runs inside this process on every supported OS, with no
    account and no per-word bill, so live partials are what voice input is for
    rather than an extra to be earned: a fresh install shows words while the user
    is still speaking.

    The last case is the other half of the rule and the reason the flip is safe. A
    stored ``false`` is a deliberate trade (less CPU locally, fewer API calls on
    ``transcribe``), and a moved default must never overrule a written-down choice.
    """

    def test_stt_config_dataclass_default_is_true(self) -> None:
        assert SttConfig().streaming is True

    def test_missing_stt_key_loads_streaming_true(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({}))
        with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            cfg = KiroCrewConfig.load()
        assert cfg.stt.streaming is True

    def test_partial_stt_block_without_streaming_key_loads_true(self, tmp_path: Path) -> None:
        stt = _loaded_stt(tmp_path, {"provider": "transcribe", "language_code": "en-US"})
        assert stt.streaming is True

    def test_explicitly_stored_false_still_wins(self, tmp_path: Path) -> None:
        assert _loaded_stt(tmp_path, {"streaming": False}).streaming is False


class TestSttFreshInstallPosture:
    """Voice input is on, and on-device, out of the box.

    These defaults only make sense together: recognition costs one model download
    and then nothing, so ``enabled`` is honest rather than an opt-in hiding a
    working feature, and the provider it turns on is the one with no precondition.
    Pinning ``enabled`` alone would leave room for the one combination that is
    wrong for a fresh install, on by default and pointing at a recogniser that
    bills an AWS account.
    """

    def test_enabled_by_default(self) -> None:
        assert SttConfig().enabled is True

    def test_the_default_model_is_one_the_downloader_can_fetch(self) -> None:
        """``model`` is a key into the sha256-pinned catalog, so a default missing
        from it would leave a fresh install unable to dictate at all."""
        assert SttConfig().model == stt_models.DEFAULT_MODEL
        assert SttConfig().model in {m.name for m in stt_models.CATALOG}

    def test_a_config_with_no_stt_block_loads_that_same_posture(self, tmp_path: Path) -> None:
        """The load path and the dataclass are separate statements of every default,
        and the load path is the only one an install actually reads."""
        stt = _loaded_stt(tmp_path, {})
        assert (stt.enabled, stt.provider, stt.model) == (
            True,
            STT_PROVIDER_LOCAL,
            stt_models.DEFAULT_MODEL,
        )


class TestSttRetiredProviders:
    """A retired provider name degrades to ``local``, and the warning names it.

    Each retired recogniser needed an install the user performed themselves (a
    whisper CLI on ``PATH``, an ``mlx`` or ``faster-whisper`` wheel) and has no
    dispatcher behind it now, so a stored value has to land on the resident engine
    rather than leave voice input pointing at nothing. It degrades instead of
    raising for the same reason an unusable persisted backend does: this value
    arrives from ``config.json``, and failing the load would take the whole
    gateway down over a setting.

    The old value has to appear in the warning. It is the only place an operator
    learns why the provider they chose is not the one running, and "unknown
    provider" without the name is unactionable.
    """

    @pytest.fixture(autouse=True)
    def _reset_warn_once(self):
        """The degrade log is once-per-value-per-process, so tests must not inherit it."""
        loader_module._WARNED_STT_PROVIDERS.clear()
        yield
        loader_module._WARNED_STT_PROVIDERS.clear()

    @pytest.mark.parametrize("retired", loader_module._RETIRED_STT_PROVIDERS)
    def test_retired_provider_degrades_to_local(self, retired: str, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            assert _validated_stt_provider(retired) == STT_PROVIDER_LOCAL
        assert retired in caplog.text
        assert "retired" in caplog.text

    @pytest.mark.parametrize("retired", loader_module._RETIRED_STT_PROVIDERS)
    def test_stored_retired_provider_loads_as_local(self, retired: str, tmp_path: Path) -> None:
        assert _loaded_stt(tmp_path, {"provider": retired}).provider == STT_PROVIDER_LOCAL

    def test_the_load_path_says_which_retired_value_it_replaced(
        self, tmp_path: Path, caplog
    ) -> None:
        """The reason has to reach the log through schema validation, not around it.

        A retired name is deliberately absent from the schema's enum, so the plain
        validation path would report "enum violation" and drop the key, leaving the
        operator told that a value was rejected and not that the recogniser they
        chose does not exist. The absence of that generic line is the assertion:
        it is what shows the degrade ran first.
        """
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            caplog.clear()
            stt = _loaded_stt(tmp_path, {"provider": "parakeet"})
        assert stt.provider == STT_PROVIDER_LOCAL
        assert "parakeet" in caplog.text
        assert "retired" in caplog.text
        assert "enum violation" not in caplog.text

    def test_a_retired_name_is_not_also_selectable(self) -> None:
        """The two lists have to stay disjoint: a name in both would be accepted by
        the validator and never reach the branch that explains it is gone."""
        assert not set(loader_module._RETIRED_STT_PROVIDERS) & set(
            loader_module._VALID_STT_PROVIDERS
        )

    def test_an_unknown_provider_is_told_what_it_could_have_been(self, caplog) -> None:
        """A typo gets the selectable list; a retired name gets the reason instead,
        because "mlx is not one of local/apple/transcribe/off" answers the wrong
        question for someone who had it working yesterday. A typo also lands on
        ``off`` rather than ``local``: ``test_stt_provider_off`` carries why."""
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            assert _validated_stt_provider("whispr") == "off"
        assert "whispr" in caplog.text
        for selectable in loader_module._VALID_STT_PROVIDERS:
            assert selectable in caplog.text

    def test_the_same_unusable_provider_is_only_said_once(self, caplog) -> None:
        """A stored retired name must not narrate itself on every config load.

        The gateway loads config repeatedly -- several times a second under normal
        dashboard traffic -- and this degrade sits on that path, so without
        deduplication one stale ``config.json`` value fills the log for the whole
        session and buries everything else. The retirement is per-install
        information: saying it once is what makes it readable.
        """
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            for _ in range(25):
                assert _validated_stt_provider("whisper") == STT_PROVIDER_LOCAL
        assert caplog.text.count("retired") == 1

    def test_each_distinct_unusable_provider_still_gets_its_own_line(self, caplog) -> None:
        """Deduplication is per VALUE, not a single global latch -- otherwise the
        first bad value would silence the explanation for every later one."""
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            assert _validated_stt_provider("whisper") == STT_PROVIDER_LOCAL
            assert _validated_stt_provider("parakeet") == STT_PROVIDER_LOCAL
            assert _validated_stt_provider("whispr") == "off"
        assert "whisper" in caplog.text
        assert "parakeet" in caplog.text
        assert "whispr" in caplog.text

    def test_a_non_string_provider_degrades_rather_than_raising(self, tmp_path: Path) -> None:
        """The membership tests take an ``object``, so a hand-edited number or a
        JSON ``null`` has to fall through instead of a TypeError. ``null`` names
        nothing and is read as the absent key (the default); a number is a value
        that cannot be honoured and fails closed like any other unknown one."""
        assert _loaded_stt(tmp_path, {"provider": None}).provider == STT_PROVIDER_LOCAL
        assert _loaded_stt(tmp_path, {"provider": 7}).provider == "off"


class TestSttRemovedFieldsAreInert:
    """A config written for the retired providers still loads, and is not degraded.

    The removed keys named the out-of-band installs those providers needed. An
    unknown key inside a section must be IGNORED rather than treated as a section
    the loader could not read: marking ``stt`` degraded would stop the write-back
    migration from normalising the file (it refuses to overwrite evidence) and
    would tell every consumer the operator's voice settings are unknown, when only
    four dead keys are.
    """

    _LEGACY_SECTION = {
        "provider": "mlx",
        "whisper_path": "/usr/local/bin/whisper",
        "mlx_model": "mlx-community/whisper-large-v3-turbo",
        "parakeet_model": "mlx-community/parakeet-tdt-0.6b-v3",
        "device": "cpu",
        "language_code": "fr-FR",
    }

    def test_the_legacy_section_loads_without_degrading_anything(self, tmp_path: Path) -> None:
        """An empty degraded set is exactly what the write-back migration checks.

        So a legacy section counted as degraded would keep the dead keys on disk for
        every future load, and the publish gate, which fails closed on that set,
        would start denying over four inert keys.
        """
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"stt": dict(self._LEGACY_SECTION)}))
        with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            cfg = KiroCrewConfig.load()
        assert cfg.degraded_sections == frozenset()

    def test_the_settings_that_still_exist_are_honoured(self, tmp_path: Path) -> None:
        """The dead keys must not poison the live ones sharing the section."""
        stt = _loaded_stt(tmp_path, dict(self._LEGACY_SECTION))
        assert stt.language_code == "fr-FR"
        assert stt.provider == STT_PROVIDER_LOCAL

    def test_the_removed_fields_are_not_attributes(self, tmp_path: Path) -> None:
        stt = _loaded_stt(tmp_path, dict(self._LEGACY_SECTION))
        for name in _REMOVED_STT_FIELDS:
            assert not hasattr(stt, name), name

    def test_a_round_trip_does_not_carry_them_forward(self, tmp_path: Path) -> None:
        """``to_dict`` is what a save writes back, so a key it still emitted would
        be re-persisted forever and keep offering the dashboard a setting with
        nothing behind it."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"stt": dict(self._LEGACY_SECTION)}))
        with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            written = KiroCrewConfig.load().to_dict()["stt"]
        assert set(written).isdisjoint(_REMOVED_STT_FIELDS)
        assert written["provider"] == STT_PROVIDER_LOCAL


class TestSttModelCatalog:
    """``stt.model`` always resolves onto a name the catalog carries.

    The value becomes a filename under the models directory and the key to a
    sha256 pin, so an arbitrary string must never reach a path, and a name the
    downloader cannot fetch must never reach the dashboard's menu. Superseded
    names are mapped rather than dropped, because dropping them downgrades
    whoever deliberately picked the accuracy ceiling.
    """

    @pytest.mark.parametrize("name", [m.name for m in stt_models.CATALOG])
    def test_every_catalog_name_is_accepted_unchanged(self, name: str) -> None:
        assert _validated_stt_model(name) == name

    @pytest.mark.parametrize(("stored", "target"), sorted(_SUPERSEDED_STT_MODELS.items()))
    def test_a_superseded_name_maps_to_its_target(self, stored: str, target: str) -> None:
        assert _validated_stt_model(stored) == target

    def test_every_alias_the_loader_knows_states_its_target_here(self) -> None:
        """An alias added to the catalog has to declare its intent in this file.

        Without this ratchet a new alias is exercised by nothing and may point
        anywhere, including at the default, which is the silent downgrade the alias
        table exists to prevent.
        """
        assert set(stt_models._ALIASES) == set(_SUPERSEDED_STT_MODELS)

    def test_no_superseded_name_shadows_a_real_catalog_entry(self) -> None:
        """An alias keyed on a live name would redirect that model away from itself,
        so ``base`` could stop meaning ``base``."""
        assert {m.name for m in stt_models.CATALOG}.isdisjoint(_SUPERSEDED_STT_MODELS)

    def test_every_target_is_downloadable(self) -> None:
        assert set(_SUPERSEDED_STT_MODELS.values()) <= {m.name for m in stt_models.CATALOG}

    def test_the_advertised_menu_is_the_catalog_plus_the_custom_sentinel(self) -> None:
        """The enum the schema publishes drives the dashboard picker. Anything it
        offers beyond the catalog is a model the downloader cannot fetch.

        ``custom`` is the one exception and is asserted exactly rather than
        excluded: it names no artifact, so what makes it loadable is the
        ``custom_model_path`` / ``custom_model_sha256`` pair, and the loader
        degrades it to the default when they are not both usable. Pinning the set
        in both directions keeps a second sentinel from being added without a
        load path behind it."""
        assert set(loader_module._VALID_STT_MODELS) == {m.name for m in stt_models.CATALOG} | {
            stt_models.CUSTOM_MODEL
        }

    def test_custom_is_accepted_only_with_a_usable_path_and_digest(self, caplog) -> None:
        """The sentinel is a selection, not a model: without the pair there is
        nothing to load and nothing to verify it against, so it must degrade the
        way an unknown name does rather than reach a load."""
        digest = "a" * 64
        assert (
            _validated_stt_model(
                stt_models.CUSTOM_MODEL,
                custom_path="ggml-x.bin",
                custom_sha256=digest,
            )
            == stt_models.CUSTOM_MODEL
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            assert _validated_stt_model(stt_models.CUSTOM_MODEL) == stt_models.DEFAULT_MODEL
        assert stt_models.CUSTOM_MODEL in caplog.text

    def test_an_unknown_name_degrades_to_the_default(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.stt.models"):
            assert _validated_stt_model("ggml-enormous") == stt_models.DEFAULT_MODEL
        assert "ggml-enormous" in caplog.text

    def test_a_non_string_or_empty_name_degrades_to_the_default(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            assert _validated_stt_model(7) == stt_models.DEFAULT_MODEL
            assert _validated_stt_model("") == stt_models.DEFAULT_MODEL
            assert _validated_stt_model(None) == stt_models.DEFAULT_MODEL

    def test_a_stored_superseded_name_loads_as_its_target(self, tmp_path: Path) -> None:
        """The mapping has to happen on the load path, not only in the validator:
        this is where an install that predates the catalog reads its model from."""
        assert _loaded_stt(tmp_path, {"model": "turbo"}).model == "large-v3-turbo"

    def test_a_stored_unknown_name_loads_as_the_default(self, tmp_path: Path) -> None:
        assert _loaded_stt(tmp_path, {"model": "../../etc/passwd"}).model == (
            stt_models.DEFAULT_MODEL
        )


class TestSttIntervalClamps:
    """A stored millisecond value is already the effective one.

    ``vad.Endpointer`` raises ``silence_ms`` to ``MIN_SILENCE_MS`` and a live
    session raises ``partial_interval_ms`` to ``MIN_PARTIAL_INTERVAL_MS`` when the
    recogniser is built. If the loader stored anything below those floors, the
    settings panel would read back a number nothing honours, which is the worst
    available shape: the operator sees their choice, and the recogniser runs a
    different one.

    Every bound here is read from ``kiro_crew.stt.limits``, which owns them. A
    literal in a test is how the two copies drift apart without either side
    noticing, and this test is what would have to notice.
    """

    def test_silence_ms_is_raised_to_the_floor_the_vad_enforces(self, tmp_path: Path) -> None:
        stt = _loaded_stt(tmp_path, {"silence_ms": 1})
        assert stt.silence_ms == stt_limits.MIN_SILENCE_MS

    def test_partial_interval_ms_is_raised_to_the_session_floor(self, tmp_path: Path) -> None:
        stt = _loaded_stt(tmp_path, {"partial_interval_ms": 1})
        assert stt.partial_interval_ms == stt_limits.MIN_PARTIAL_INTERVAL_MS

    def test_the_vad_does_not_re_clamp_what_the_loader_stored(self, tmp_path: Path, caplog) -> None:
        """Run the loaded value through the real enforcer.

        ``Endpointer`` warns whenever it has to raise the window it was handed, so
        silence is the observable proof that the two floors are one number rather
        than two that happen to match today.
        """
        from kiro_crew.stt.vad import Endpointer

        stt = _loaded_stt(tmp_path, {"silence_ms": 1})
        with caplog.at_level(logging.WARNING, logger="kiro_crew.stt.vad"):
            caplog.clear()
            Endpointer(silence_ms=stt.silence_ms)
        assert [r.message for r in caplog.records if r.name == "kiro_crew.stt.vad"] == []

    def test_a_numeric_string_is_coerced_and_then_clamped(self, tmp_path: Path) -> None:
        """Older writers stored these as strings, and the raw-dict bounds pass skips
        anything that is not already an int, so the coercion site is the only place
        the range is actually enforced."""
        stt = _loaded_stt(tmp_path, {"silence_ms": "1", "partial_interval_ms": "1"})
        assert stt.silence_ms == stt_limits.MIN_SILENCE_MS
        assert stt.partial_interval_ms == stt_limits.MIN_PARTIAL_INTERVAL_MS

    def test_a_value_inside_the_range_is_stored_verbatim(self, tmp_path: Path) -> None:
        """The clamp must not be a blanket overwrite: a deliberate setting between
        the floor and the ceiling has to survive untouched."""
        inside = stt_limits.MIN_SILENCE_MS + 1
        cadence = stt_limits.MIN_PARTIAL_INTERVAL_MS + 1
        stt = _loaded_stt(tmp_path, {"silence_ms": inside, "partial_interval_ms": cadence})
        assert (stt.silence_ms, stt.partial_interval_ms) == (inside, cadence)

    def test_both_knobs_are_capped_at_the_shared_ceiling(self, tmp_path: Path) -> None:
        stt = _loaded_stt(
            tmp_path,
            {
                "silence_ms": stt_limits.MAX_INTERVAL_MS * 10,
                "partial_interval_ms": stt_limits.MAX_INTERVAL_MS * 10,
            },
        )
        assert stt.silence_ms == stt_limits.MAX_INTERVAL_MS
        assert stt.partial_interval_ms == stt_limits.MAX_INTERVAL_MS

    def test_idle_evict_secs_is_clamped_to_the_range_limits_declares(self, tmp_path: Path) -> None:
        """Zero is legal and means "release the model as soon as it goes idle", so
        the floor cannot be raised to 1 without taking away the setting a
        memory-constrained host needs."""
        assert _loaded_stt(tmp_path, {"idle_evict_secs": -1}).idle_evict_secs == (
            stt_limits.MIN_IDLE_EVICT_SECS
        )
        assert _loaded_stt(tmp_path, {"idle_evict_secs": 0}).idle_evict_secs == 0
        assert _loaded_stt(
            tmp_path, {"idle_evict_secs": stt_limits.MAX_IDLE_EVICT_SECS + 1}
        ).idle_evict_secs == (stt_limits.MAX_IDLE_EVICT_SECS)

    def test_the_loader_bounds_are_the_ones_limits_owns(self) -> None:
        """``limits`` exists so a bound is stated once. The floors are imported from
        it; these three are still spelled out in the loader, so this is the only
        thing standing between them and a silent divergence.
        """
        assert loader_module._STT_INTERVAL_MS_MAX == stt_limits.MAX_INTERVAL_MS
        assert loader_module._STT_IDLE_EVICT_SECS_MIN == stt_limits.MIN_IDLE_EVICT_SECS
        assert loader_module._STT_IDLE_EVICT_SECS_MAX == stt_limits.MAX_IDLE_EVICT_SECS

    def test_a_bool_is_not_a_millisecond_count(self, tmp_path: Path) -> None:
        """``bool`` subclasses ``int``, so a checkbox value that leaked into this key
        would read as a 1 ms window unless something refuses it. It lands on the
        default rather than on the floor, which is the difference between "this
        setting was not usable" and "this setting asked for the fastest cadence"."""
        stt = _loaded_stt(tmp_path, {"silence_ms": True, "partial_interval_ms": True})
        assert stt.silence_ms == stt_limits.DEFAULT_SILENCE_MS
        assert stt.partial_interval_ms == stt_limits.DEFAULT_PARTIAL_INTERVAL_MS
