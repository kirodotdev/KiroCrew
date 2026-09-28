"""``stt.provider = off`` is a real value, and an unknown value lands on it.

Both halves come from one incident (kirodotdev/KiroCrew#13179): a gateway whose
native recogniser raised ``SIGILL`` on model load was told to run
``kirocrew config set stt.provider off``. That value did not exist, so the loader
degraded it to ``local`` -- the very engine that was crashing -- and said so only
in a WARNING line that nobody read. The write was accepted, the setting was
silently inverted, and the crash loop continued.

Three rules follow, each pinned below:

- ``off`` is selectable. It disables every speech path exactly as
  ``stt.enabled = false`` does, and it is reachable from the same enum the
  Settings picker and ``config set`` read.
- An UNKNOWN provider degrades to ``off``, never to ``local``. A value the loader
  cannot honour must fail closed: nothing loads, nothing bills, and the warning
  names the value that was stored and the one it became. Retired names keep
  their existing mapping onto ``local`` -- those users demonstrably wanted local
  recognition, and the mapping is what keeps their voice input working.
- ``config set`` refuses a value outside a declared enum at write time. Accepting
  ``stt.provider = whispr`` and then explaining at every load why it is not in
  force is the failure mode this exists to close.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.config import loader as loader_module
from kiro_crew.config.loader import KiroCrewConfig, SttConfig, _validated_stt_provider
from kiro_crew.config.schema import SCHEMA_REGISTRY
from kiro_crew.config.sections import STT_PROVIDER_LOCAL, STT_PROVIDER_OFF


@pytest.fixture(autouse=True)
def _reset_warn_once():
    """The degrade log is once-per-value-per-process, so tests must not inherit it."""
    loader_module._WARNED_STT_PROVIDERS.clear()
    yield
    loader_module._WARNED_STT_PROVIDERS.clear()


def _loaded_stt(tmp_path: Path, stt_block: dict) -> SttConfig:
    (tmp_path / "config.json").write_text(json.dumps({"stt": stt_block}))
    with patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
        return KiroCrewConfig.load().stt


class TestOffIsSelectable:
    def test_off_is_in_the_selectable_list(self) -> None:
        assert STT_PROVIDER_OFF == "off"
        assert STT_PROVIDER_OFF in loader_module._VALID_STT_PROVIDERS

    def test_off_validates_to_itself_without_a_warning(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            assert _validated_stt_provider(STT_PROVIDER_OFF) == STT_PROVIDER_OFF
        assert caplog.text == ""

    def test_a_stored_off_loads_as_off(self, tmp_path: Path) -> None:
        assert _loaded_stt(tmp_path, {"provider": "off"}).provider == STT_PROVIDER_OFF

    def test_off_is_offered_to_the_settings_picker(self) -> None:
        """The picker and ``config set`` read the same tuple; a value only one of
        them accepts is a value a user can store and then not see."""
        from kiro_crew.dashboard.handlers.core import _stt_providers

        assert STT_PROVIDER_OFF in _stt_providers()

    def test_off_never_streams(self) -> None:
        """The live socket grants by POSITIVE membership; ``off`` must not be in it."""
        from kiro_crew.dashboard.stt_stream import _STREAMING_PROVIDERS

        assert STT_PROVIDER_OFF not in _STREAMING_PROVIDERS


class TestOffDisablesEverySpeechPath:
    def test_the_batch_module_spells_off_the_same_way(self) -> None:
        """``transcribe`` keeps ``kiro_crew.config`` off its import path, so it
        carries its own copy of the value; the two must never drift."""
        from kiro_crew import transcribe

        assert transcribe.STT_PROVIDER_OFF == STT_PROVIDER_OFF

    def test_availability_names_the_provider_control_not_the_toggle(self) -> None:
        """Its own code, because the remedy differs: ``stt_disabled`` renders as
        "switch it on above", and the Enabled toggle IS on for this user."""
        from kiro_crew import transcribe

        result = transcribe.availability_detail(SttConfig(enabled=True, provider="off"))
        assert result.ok is False
        assert result.code == transcribe.CODE_PROVIDER_OFF
        assert result.code != transcribe.CODE_DISABLED

    def test_the_toggle_still_wins_when_both_are_off(self) -> None:
        from kiro_crew import transcribe

        result = transcribe.availability_detail(SttConfig(enabled=False, provider="off"))
        assert result.code == transcribe.CODE_DISABLED

    @pytest.mark.asyncio
    async def test_batch_transcription_dispatches_nothing(self, tmp_path: Path) -> None:
        """``transcribe_audio``'s dispatch treats every non-apple, non-transcribe
        value as ``local``; ``off`` must return before that branch is reached."""
        from kiro_crew import transcribe

        audio = tmp_path / "memo.wav"
        audio.write_bytes(b"RIFF")
        with (
            patch.object(transcribe, "_transcribe_local", new=AsyncMock()) as local,
            patch.object(transcribe, "_transcribe_aws", new=AsyncMock()) as aws,
            patch.object(transcribe, "_transcribe_apple", new=AsyncMock()) as apple,
        ):
            result = await transcribe.transcribe_audio(
                str(audio), SttConfig(enabled=True, provider="off")
            )
        assert result is None
        local.assert_not_called()
        aws.assert_not_called()
        apple.assert_not_called()


class TestUnknownProviderFailsClosed:
    def test_an_unknown_value_degrades_to_off_not_local(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            assert _validated_stt_provider("whispr") == STT_PROVIDER_OFF
        assert "whispr" in caplog.text
        assert f"using {STT_PROVIDER_OFF!r}" in caplog.text
        for selectable in loader_module._VALID_STT_PROVIDERS:
            assert selectable in caplog.text

    def test_a_stored_unknown_value_loads_as_off(self, tmp_path: Path) -> None:
        assert _loaded_stt(tmp_path, {"provider": "whispr"}).provider == STT_PROVIDER_OFF

    @pytest.mark.parametrize("retired", loader_module._RETIRED_STT_PROVIDERS)
    def test_a_retired_name_still_lands_on_local(self, retired: str) -> None:
        """Retired names are not unknown: the user had local recognition and
        keeps it. Only a value with no history fails closed."""
        assert _validated_stt_provider(retired) == STT_PROVIDER_LOCAL


class TestConfigSetRefusesValuesOutsideAnEnum:
    """``config set`` is the write path the incident used; refuse there."""

    @staticmethod
    def _run(config_dir: Path, key: str, value: str) -> None:
        from kiro_crew.cli_config import _config_cmd

        args = argparse.Namespace(config_action="set", key=key, value=value, file=None, local=False)
        with (
            patch("kiro_crew.cli_config.config_path", return_value=config_dir / "config.json"),
            patch(
                "kiro_crew.cli_config.config_local_path",
                return_value=config_dir / "config.local.json",
            ),
            patch("kiro_crew.config.loader.config_dir", return_value=config_dir),
            patch("kiro_crew.cli_config.sel"),
        ):
            _config_cmd(args)

    @staticmethod
    def _config_dir(tmp_path: Path) -> Path:
        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(json.dumps({"stt": {"provider": "local"}}))
        return config_dir

    def test_an_unknown_provider_is_refused_at_the_write(self, tmp_path: Path, capsys) -> None:
        config_dir = self._config_dir(tmp_path)

        with pytest.raises(SystemExit) as exc:
            self._run(config_dir, "stt.provider", "whispr")

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "whispr" in err
        for selectable in loader_module._VALID_STT_PROVIDERS:
            assert selectable in err
        saved = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
        assert saved["stt"]["provider"] == "local"

    def test_off_is_written(self, tmp_path: Path) -> None:
        config_dir = self._config_dir(tmp_path)

        self._run(config_dir, "stt.provider", "off")

        saved = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
        assert saved["stt"]["provider"] == "off"

    def test_a_case_variant_is_written_in_the_enum_spelling(self, tmp_path: Path) -> None:
        """``LOCAL`` round-trips to ``local``. The loader matches this key exactly,
        so writing the user's spelling would admit here what degrades there --
        the write-then-degrade gap this check exists to close."""
        config_dir = self._config_dir(tmp_path)

        self._run(config_dir, "stt.provider", "LOCAL")

        saved = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
        assert saved["stt"]["provider"] == "local"
        with patch("kiro_crew.config.loader.config_dir", return_value=config_dir):
            assert KiroCrewConfig.load().stt.provider == STT_PROVIDER_LOCAL

    def test_a_key_without_an_enum_is_unaffected(self, tmp_path: Path) -> None:
        """The check is scoped to declared enums; free-form strings keep writing."""
        config_dir = self._config_dir(tmp_path)

        self._run(config_dir, "stt.language_code", "zh-CN")

        saved = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
        assert saved["stt"]["language_code"] == "zh-CN"

    def test_a_model_alias_is_written_as_its_catalog_row(self, tmp_path: Path) -> None:
        """`stt.model`'s enum lists catalog rows, but the loader resolves aliases onto
        them (`turbo` -> `large-v3-turbo`); the write must admit what the load does,
        the way the dashboard PUT does through `canonical_name`."""
        config_dir = self._config_dir(tmp_path)

        self._run(config_dir, "stt.model", "turbo")

        saved = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
        assert saved["stt"]["model"] == "large-v3-turbo"

    def test_spelling_the_loader_normalizes_is_not_refused(self, tmp_path: Path) -> None:
        """``agent.log_level`` is upper-cased at load; a lower-case write worked
        before this check existed and must keep working."""
        config_dir = self._config_dir(tmp_path)

        self._run(config_dir, "agent.log_level", "debug")

        saved = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
        assert saved["agent"]["log_level"] == "DEBUG"

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            (entry.path, value)
            for entry in SCHEMA_REGISTRY
            if entry.enum_values and "*" not in entry.path
            for value in entry.enum_values
            if isinstance(value, str) and value
        ],
    )
    def test_every_declared_enum_value_is_admitted_in_any_case(self, key: str, value: str) -> None:
        """The gate now stands in front of every declared enum key, so the rule it
        assumes -- what the loader accepts is the enum, up to spelling -- is pinned
        here for each key rather than audited once: the value itself, its lower-
        and upper-cased spellings all resolve to the enum's own spelling. A key
        whose loader grows an alias table the enum does not list shows up as a
        refusal here, the way ``stt.model`` did."""
        from kiro_crew.cli_config import _declared_enum_value

        for spelling in (value, value.lower(), value.upper(), value.swapcase()):
            assert _declared_enum_value(key, spelling) == value, (key, spelling)

    def test_the_model_alias_table_is_what_the_loader_resolves(self) -> None:
        """``stt.model`` is the one key whose loader accepts names outside its enum;
        every alias the catalog knows must be admitted and stored as its row."""
        from kiro_crew.cli_config import _declared_enum_value
        from kiro_crew.stt import models as stt_models

        rows = {entry.path: entry.enum_values for entry in SCHEMA_REGISTRY}["stt.model"]
        for alias, row in stt_models._ALIASES.items():
            assert row in rows, (alias, row)
            assert _declared_enum_value("stt.model", alias) == row


class TestTheResolverIsPure:
    def test_resolution_answers_without_logging_or_remembering(self, caplog) -> None:
        """`stt_provider_resolution` is the rule; `_validated_stt_provider` is the
        rule plus the load-time notice. A surface that only needs the answer must
        get it with no log line and no entry in the once-per-process set."""
        from kiro_crew.config import sections

        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            assert sections.stt_provider_resolution("whispr") == STT_PROVIDER_OFF
            assert sections.stt_provider_resolution("mlx") == STT_PROVIDER_LOCAL
            assert sections.stt_provider_resolution(None) == STT_PROVIDER_LOCAL
            assert sections.stt_provider_resolution("apple") == "apple"
        assert caplog.text == ""
        assert loader_module._WARNED_STT_PROVIDERS == set()

    def test_the_wrapper_agrees_with_the_resolver_and_warns_once(self, caplog) -> None:
        from kiro_crew.config import sections

        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            for value in ("whispr", "mlx", None, "apple", ["a", "list"]):
                assert _validated_stt_provider(value) == sections.stt_provider_resolution(value)
                assert _validated_stt_provider(value) == sections.stt_provider_resolution(value)
        assert caplog.text.count("whispr") == 1
        assert caplog.text.count("mlx") == 1


class TestAdoptingACoercedProviderKeepsWhatRuns:
    """``config defaults --adopt`` is the remedy the degrade WARNING names. It must
    never move the effective provider: an unknown value runs as ``off`` and must
    STAY ``off`` after adoption, not become the default ``local`` -- which for the
    incident's user is the engine that was crashing."""

    @staticmethod
    def _adopt(config_dir: Path) -> str:
        from kiro_crew.cli_config import _config_cmd

        args = argparse.Namespace(
            config_action="defaults", adopt=True, keep=False, keys=None, file=None, local=False
        )
        with (
            patch("kiro_crew.cli_config.config_path", return_value=config_dir / "config.json"),
            patch(
                "kiro_crew.cli_config.config_local_path",
                return_value=config_dir / "config.local.json",
            ),
            patch("kiro_crew.config.loader.config_dir", return_value=config_dir),
            patch("kiro_crew.cli_config.sel"),
        ):
            _config_cmd(args)
        return (config_dir / "config.json").read_text(encoding="utf-8")

    def test_an_unknown_provider_is_adopted_as_off(self, tmp_path: Path, capsys) -> None:
        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(json.dumps({"stt": {"provider": "whispercpp"}}))

        saved = json.loads(self._adopt(config_dir))

        assert saved["stt"]["provider"] == STT_PROVIDER_OFF
        assert "set to 'off'" in capsys.readouterr().out
        with patch("kiro_crew.config.loader.config_dir", return_value=config_dir):
            assert KiroCrewConfig.load().stt.provider == STT_PROVIDER_OFF

    def test_a_retired_provider_is_still_adopted_by_removal(self, tmp_path: Path) -> None:
        """Retired names resolve to the default, so the absent key says the same."""
        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(json.dumps({"stt": {"provider": "whisper"}}))

        saved = json.loads(self._adopt(config_dir))

        assert "provider" not in saved["stt"]

    def test_the_listing_says_what_the_value_runs_as(self) -> None:
        from kiro_crew.config import superseded_defaults as sd

        entry = next(c for c in sd.COERCED_VALUES if c.dotted_key == "stt.provider")
        assert entry.resolves_to("whispercpp") == STT_PROVIDER_OFF
        assert entry.resolves_to("whisper") == STT_PROVIDER_LOCAL
        assert "'off'" in sd.coercion_summary(entry, "whispercpp")
        assert "changes nothing" not in sd.coercion_summary(entry, "whispercpp")
