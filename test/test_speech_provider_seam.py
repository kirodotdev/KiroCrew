"""The ``speech`` platform seam: edition STT/TTS providers join validation and defaults.

Phase 1 of the pluggable speech providers RFC. The seam
must be invisible on a standalone install (same valid sets, defaults and warning
text), must let a registered name through every config validator, must never let
an edition default beat a stored value, and must refuse at boot any name that
would shadow a built-in.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import unittest.mock
from pathlib import Path
from typing import Any, Optional

import pytest

from kiro_crew import speech_providers, voice_reply
from kiro_crew.config import sections_stt
from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
from kiro_crew.platform import bootstrap as bootstrap_mod
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import (
    PROFILE_ENTERPRISE,
    PlatformCompositionError,
    reset_context,
    set_context,
)
from kiro_crew.platform.defaults import DefaultSpeechProviderRegistry


@dataclasses.dataclass
class _Provider:
    name: str
    streaming: bool = False


class _Registry:
    def __init__(
        self,
        stt: tuple = (),
        tts: tuple = (),
        default_stt: Optional[str] = None,
        default_tts: Optional[str] = None,
    ) -> None:
        self._stt = tuple(_Provider(n) for n in stt)
        self._tts = tuple(_Provider(n) for n in tts)
        self._default_stt = default_stt
        self._default_tts = default_tts

    def stt_providers(self) -> tuple:
        return self._stt

    def tts_providers(self) -> tuple:
        return self._tts

    def default_stt(self) -> Optional[str]:
        return self._default_stt

    def default_tts(self) -> Optional[str]:
        return self._default_tts


@pytest.fixture(autouse=True)
def _clean_state():
    """Each test starts with no context, no cached config and no warned values."""
    reset_context()
    _invalidate_config_cache()
    sections_stt._WARNED_STT_PROVIDERS.clear()
    sections_stt._DEFERRED_STT_PROVIDERS.clear()
    yield
    reset_context()
    _invalidate_config_cache()
    sections_stt._WARNED_STT_PROVIDERS.clear()
    sections_stt._DEFERRED_STT_PROVIDERS.clear()
    bootstrap_mod._reset_boot_state()


def _install(registry: Any) -> None:
    ctx = build_default_context(KiroCrewConfig())
    set_context(dataclasses.replace(ctx, speech=registry))


def _load_stt(tmp_path: Path, section: dict) -> str:
    (tmp_path / "config.json").write_text(json.dumps({"stt": section}))
    _invalidate_config_cache()
    with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
        return KiroCrewConfig.load().stt.provider


#: The exact text a standalone install logged before the seam existed.
_STANDALONE_STT_WARNING = (
    "Unknown STT provider 'whispr'; using 'off' instead, so no recogniser runs until "
    "the value is fixed. Selectable providers: local, apple, transcribe, off. Run "
    "'kirocrew config set stt.provider <provider>' to choose one, or "
    "'kirocrew config defaults --adopt stt.provider' to drop the stored value."
)
_STANDALONE_TTS_WARNING = (
    "voice_reply.provider 'ploly' not in ['polly', 'system'], defaulting to 'system'"
)


class TestStandaloneIsUnchanged:
    """The default registry, and no context at all, give today's answers byte for byte."""

    @pytest.fixture(params=["no_context", "default_registry"])
    def standalone(self, request):
        if request.param == "default_registry":
            _install(DefaultSpeechProviderRegistry())

    def test_valid_sets_are_the_builtins(self, standalone) -> None:
        assert sections_stt.valid_stt_providers() == ("local", "apple", "transcribe", "off")
        assert sections_stt.valid_stt_providers() is sections_stt._VALID_STT_PROVIDERS
        assert voice_reply.valid_providers() == frozenset({"polly", "system"})
        assert voice_reply.valid_providers() is voice_reply.VALID_PROVIDERS

    def test_defaults_are_the_builtins(self, standalone) -> None:
        assert sections_stt.default_stt_provider() == "local"
        assert sections_stt.stt_provider_resolution(None) == "local"
        assert voice_reply.default_provider() == "system"
        assert voice_reply.resolve_configured_provider({}) == "system"
        assert voice_reply.resolve_configured_provider(None) == "system"

    def test_warning_text_is_unchanged(self, standalone, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            assert sections_stt._validated_stt_provider("whispr") == "off"
            assert voice_reply.resolve_configured_provider({"provider": "ploly"}) == "system"
        messages = [r.getMessage() for r in caplog.records]
        assert _STANDALONE_STT_WARNING in messages
        assert _STANDALONE_TTS_WARNING in messages

    def test_loaded_config_is_unchanged(self, standalone, tmp_path) -> None:
        assert _load_stt(tmp_path, {}) == "local"
        assert _load_stt(tmp_path, {"provider": "transcribe"}) == "transcribe"
        assert _load_stt(tmp_path, {"provider": "whisper"}) == "local"
        assert _load_stt(tmp_path, {"provider": "acme"}) == "off"
        assert voice_reply.resolve_configured_provider({"provider": "acme"}) == "system"

    def test_default_registry_passes_boot_validation(self) -> None:
        speech_providers.validate_speech_registry(DefaultSpeechProviderRegistry())
        assert build_default_context(KiroCrewConfig()).speech.stt_providers() == ()


class TestRegisteredProvider:
    """A name registered on the context is accepted by every config validator."""

    @pytest.fixture(autouse=True)
    def _acme(self):
        _install(_Registry(stt=("acme",), tts=("acme",)))

    def test_valid_sets_include_it(self) -> None:
        assert sections_stt.valid_stt_providers() == ("local", "apple", "transcribe", "acme", "off")
        assert "acme" in voice_reply.valid_providers()

    def test_config_load_keeps_it(self, tmp_path, caplog) -> None:
        """Through the jsonschema enum too, which lists only the built-ins."""
        with caplog.at_level(logging.WARNING):
            assert _load_stt(tmp_path, {"provider": "acme"}) == "acme"
        assert "enum violation" not in caplog.text
        assert "Unknown STT provider" not in caplog.text

    def test_tts_resolution_keeps_it(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            assert voice_reply.resolve_configured_provider({"provider": "acme"}) == "acme"
            assert voice_reply.synthesis_settings({"provider": "acme"})["provider"] == "acme"
        assert "not in" not in caplog.text

    def test_it_is_not_coerced_or_adopted_away(self) -> None:
        assert not sections_stt.stt_provider_is_coerced("acme")

    def test_unknown_name_lists_it(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            assert sections_stt._validated_stt_provider("whispr") == "off"
        assert "Selectable providers: local, apple, transcribe, acme, off." in caplog.text

    def test_config_set_accepts_it(self) -> None:
        from kiro_crew.cli_config import _declared_enum_value

        assert _declared_enum_value("stt.provider", "acme") == "acme"
        with pytest.raises(ValueError):
            _declared_enum_value("stt.provider", "nope")


class TestEditionDefault:
    """``default_stt()``/``default_tts()`` apply only to an ABSENT key."""

    @pytest.fixture(autouse=True)
    def _acme_default(self):
        _install(_Registry(stt=("acme",), tts=("acme",), default_stt="acme", default_tts="acme"))

    def test_absent_key_takes_the_edition_default(self, tmp_path) -> None:
        assert _load_stt(tmp_path, {}) == "acme"
        assert sections_stt.stt_provider_resolution(None) == "acme"
        assert voice_reply.resolve_configured_provider({}) == "acme"
        assert voice_reply.resolve_configured_provider(None) == "acme"

    @pytest.mark.parametrize("stored", ["local", "apple", "transcribe", "off"])
    def test_a_stored_stt_value_wins(self, tmp_path, stored) -> None:
        assert _load_stt(tmp_path, {"provider": stored}) == stored

    @pytest.mark.parametrize("stored", ["polly", "system"])
    def test_a_stored_tts_value_wins(self, stored) -> None:
        assert voice_reply.resolve_configured_provider({"provider": stored}) == stored

    def test_a_mistyped_value_never_reaches_the_edition_default(self, tmp_path) -> None:
        assert _load_stt(tmp_path, {"provider": "whispr"}) == "off"
        assert _load_stt(tmp_path, {"provider": "whisper"}) == "local"
        assert voice_reply.resolve_configured_provider({"provider": "ploly"}) == "system"
        assert voice_reply.resolve_configured_provider({"provider": ["x"]}) == "system"

    def test_adopting_a_retired_value_keeps_its_effective_provider(self) -> None:
        """``--adopt`` deletes a key only when deletion lands where the value did.

        A retired ``whisper`` runs as ``local``; deleting it would now select the
        edition default instead, so adoption must write ``local``.
        """
        from kiro_crew.config import superseded_defaults as sd

        entry = next(c for c in sd.COERCED_VALUES if c.dotted_key == "stt.provider")
        assert entry.adopted_value("whisper") == "local"
        reset_context()
        assert entry.adopted_value("whisper") is None

    @pytest.mark.parametrize("malformed", [False, 0, [], {}])
    def test_a_malformed_falsy_tts_value_keeps_the_builtin_default(self, malformed) -> None:
        assert voice_reply.resolve_configured_provider({"provider": malformed}) == "system"

    @pytest.mark.parametrize("empty", [None, ""])
    def test_null_or_empty_tts_value_is_absent(self, empty) -> None:
        assert voice_reply.resolve_configured_provider({"provider": empty}) == "acme"

    def test_a_home_with_no_config_file_takes_the_edition_default(self, tmp_path) -> None:
        _invalidate_config_cache()
        with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            assert KiroCrewConfig.load().stt.provider == "acme"

    def test_a_stale_piper_model_resolves_like_an_absent_provider(self) -> None:
        assert voice_reply.resolve_configured_provider({"piper_model": "/m/en.onnx"}) == "acme"

    def test_the_removed_piper_value_keeps_the_builtin_default(self) -> None:
        assert voice_reply.resolve_configured_provider({"provider": "piper"}) == "system"

    def test_a_builtin_edition_default_needs_no_registration(self, tmp_path) -> None:
        _install(_Registry(default_stt="transcribe", default_tts="polly"))
        assert _load_stt(tmp_path, {}) == "transcribe"
        assert voice_reply.resolve_configured_provider({}) == "polly"


def _boot_with(monkeypatch, registry: Any, cfg: Optional[KiroCrewConfig] = None):
    cfg = cfg or KiroCrewConfig()
    companion = dataclasses.replace(
        build_default_context(cfg, profile=PROFILE_ENTERPRISE), speech=registry
    )
    monkeypatch.setenv("KIROCREW_PROFILE", "enterprise")
    monkeypatch.setattr(bootstrap_mod, "plugin_entry_points", lambda: [object()])
    monkeypatch.setattr(
        bootstrap_mod, "discover_companion_context", lambda profile, cfg, **_kw: companion
    )
    return bootstrap_mod.bootstrap_context(cfg)


class TestPreBootWarning:
    """Before boot on a companion host, an unknown name may be about to be registered."""

    def _companion_host(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.platform.discovery.plugin_entry_points", lambda: [object()])

    def test_a_name_the_edition_registers_never_warns(self, monkeypatch, caplog) -> None:
        self._companion_host(monkeypatch)
        with caplog.at_level(logging.WARNING):
            assert sections_stt._validated_stt_provider("acme") == "off"
            _install(_Registry(stt=("acme",)))
            sections_stt.warn_deferred_stt_providers()
        assert "Unknown STT provider" not in caplog.text

    def test_a_name_nobody_registers_still_warns_once_at_boot(self, monkeypatch, caplog) -> None:
        self._companion_host(monkeypatch)
        with caplog.at_level(logging.WARNING):
            sections_stt._validated_stt_provider("whispr")
            assert "Unknown STT provider" not in caplog.text
            _install(_Registry(stt=("acme",)))
            sections_stt.warn_deferred_stt_providers()
            sections_stt.warn_deferred_stt_providers()
        assert caplog.text.count("Unknown STT provider 'whispr'") == 1

    def test_a_retired_name_is_never_deferred(self, monkeypatch, caplog) -> None:
        self._companion_host(monkeypatch)
        with caplog.at_level(logging.WARNING):
            assert sections_stt._validated_stt_provider("whisper") == "local"
        assert "retired" in caplog.text

    def test_standalone_warns_immediately(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr("kiro_crew.platform.discovery.plugin_entry_points", lambda: [])
        with caplog.at_level(logging.WARNING):
            sections_stt._validated_stt_provider("whispr")
        assert _STANDALONE_STT_WARNING in [r.getMessage() for r in caplog.records]

    def test_boot_flushes_the_deferred_check(self, monkeypatch, caplog) -> None:
        self._companion_host(monkeypatch)
        sections_stt._validated_stt_provider("whispr")
        with caplog.at_level(logging.WARNING):
            _boot_with(monkeypatch, _Registry(stt=("acme",)))
        assert "Unknown STT provider 'whispr'" in caplog.text


class TestBootRefusesShadowing:
    @pytest.mark.parametrize(
        "registry, needle",
        [
            (_Registry(stt=("transcribe",)), "'transcribe'"),
            (_Registry(stt=("local",)), "'local'"),
            (_Registry(stt=("off",)), "'off'"),
            (_Registry(stt=("whisper",)), "'whisper'"),
            (_Registry(tts=("polly",)), "'polly'"),
            (_Registry(tts=("system",)), "'system'"),
            (_Registry(tts=("piper",)), "'piper'"),
            (_Registry(stt=("acme", "acme")), "registered twice"),
            (_Registry(stt=("",)), "no usable name"),
            (_Registry(tts=(" acme",)), "no usable name"),
            # A reserved name in another case, or padded with whitespace or an
            # invisible character, must not slip past the reserved check.
            (_Registry(stt=("OFF",)), "no usable name"),
            (_Registry(stt=("Local",)), "no usable name"),
            (_Registry(tts=("Polly",)), "no usable name"),
            (_Registry(stt=("local\u200b",)), "no usable name"),
            (_Registry(stt=("\u200boff",)), "no usable name"),
            (_Registry(stt=("off\n",)), "no usable name"),
            (_Registry(stt=("off ",)), "no usable name"),
            (_Registry(tts=("system\t",)), "no usable name"),
            (_Registry(stt=("acme\u00a0",)), "no usable name"),
            (_Registry(stt=("ａｃｍｅ",)), "no usable name"),
            (_Registry(default_stt="acme"), "default_stt()"),
            (_Registry(default_tts="acme"), "default_tts()"),
        ],
    )
    def test_a_colliding_registry_fails_boot(self, monkeypatch, registry, needle) -> None:
        with pytest.raises(PlatformCompositionError, match="speech provider registry") as exc:
            _boot_with(monkeypatch, registry)
        assert needle in str(exc.value)

    def test_an_unreadable_registry_fails_boot(self, monkeypatch) -> None:
        class _Broken(_Registry):
            def stt_providers(self) -> tuple:
                raise RuntimeError("down")

        with pytest.raises(PlatformCompositionError, match="could not be read"):
            _boot_with(monkeypatch, _Broken())

    def test_reserved_names_are_compared_casefolded(self) -> None:
        """The reserved check itself folds case, beside the slug rule."""
        with pytest.raises(PlatformCompositionError, match="never replace one"):
            speech_providers._checked_names(
                (_Provider("off"),), "STT", frozenset({"OFF"}), "is reserved"
            )

    @pytest.mark.parametrize("name", ["acme", "acme-cloud", "acme_2", "a"])
    def test_a_lowercase_slug_is_accepted(self, name) -> None:
        speech_providers.validate_speech_registry(_Registry(stt=(name,), tts=(name,)))

    def test_a_valid_registry_boots(self, monkeypatch) -> None:
        ctx = _boot_with(monkeypatch, _Registry(stt=("acme",), tts=("acme",)))
        assert [p.name for p in ctx.speech.stt_providers()] == ["acme"]
        assert "acme" in voice_reply.valid_providers()


class TestBootRefreshesThePreBootConfig:
    """The boot config was loaded before any context existed, against built-ins only."""

    def test_a_stored_registered_provider_survives_boot(self, monkeypatch, tmp_path) -> None:
        (tmp_path / "config.json").write_text(json.dumps({"stt": {"provider": "acme"}}))
        with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            cfg = KiroCrewConfig.load()
            assert cfg.stt.provider == "off"  # nothing registered yet
            _boot_with(monkeypatch, _Registry(stt=("acme",)), cfg)
            assert cfg.stt.provider == "acme"
            assert KiroCrewConfig.load().stt.provider == "acme"

    def test_standalone_boot_does_not_reload(self, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_PROFILE", "standalone")
        with unittest.mock.patch.object(KiroCrewConfig, "load") as load:
            bootstrap_mod.bootstrap_context(KiroCrewConfig())
        load.assert_not_called()


def test_the_dashboard_has_no_sentence_for_the_unavailable_code() -> None:
    """So it renders the backend ``detail`` instead of a canned Off sentence."""
    from kiro_crew import transcribe

    source = (Path(__file__).resolve().parents[1] / "website/src/lib/sttProviders.ts").read_text(
        encoding="utf-8"
    )
    # The code may be named elsewhere (the Status badge reads it), but it must
    # not have a catalog sentence: ``unavailableMessage`` then shows ``detail``.
    catalog = source.split("export const UNAVAILABLE_CODE_KEY", 1)[1].split("\n}\n", 1)[0]
    assert "stt_provider_off" in catalog
    assert transcribe.CODE_PROVIDER_UNAVAILABLE not in catalog
