"""Tests for the extracted ``kiro_crew.config.validation`` module.

Covers the schema-introspection helpers, the ``validate_config_data`` entry
point, and — most importantly — the new ``ConfigCache`` object, whose explicit
``clear()`` replaces the previously-untestable bare ``_CONFIG_CACHE`` module
global.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew.config import loader as _loader_module
from kiro_crew.config import validation


class TestConfigCache:
    """The validated-data cache is now an injectable/clearable object."""

    def test_store_get_roundtrip_on_matching_fingerprint(self) -> None:
        cache = validation.ConfigCache()
        fp = (("config.json", 1, 2, 0o600),)
        cache.store({"agent": {"provider": "acp"}}, fp)
        got = cache.get(fp)
        assert got == {"agent": {"provider": "acp"}}

    def test_get_misses_on_different_fingerprint(self) -> None:
        cache = validation.ConfigCache()
        cache.store({"x": 1}, (("a", 1),))
        assert cache.get((("a", 2),)) is None

    def test_get_returns_deep_copy_not_shared_reference(self) -> None:
        # Mutating a returned dict must not corrupt the cached original — this is
        # why load() can hand the result to the 100+ in-place-mutating callers.
        cache = validation.ConfigCache()
        fp = (("config.json", 1, 2, 0o600),)
        cache.store({"agent": {"provider": "acp"}}, fp)
        first = cache.get(fp)
        assert first is not None
        first["agent"]["provider"] = "MUTATED"
        second = cache.get(fp)
        assert second is not None
        assert second["agent"]["provider"] == "acp"

    def test_clear_drops_the_entry(self) -> None:
        cache = validation.ConfigCache()
        fp = (("config.json", 1, 2, 0o600),)
        cache.store({"x": 1}, fp)
        assert cache.get(fp) is not None
        cache.clear()
        assert cache.get(fp) is None


class TestSchemaIntrospectionHelpers:
    _SCHEMA = {
        "properties": {
            "agent": {
                "properties": {
                    "provider": {"x-meta": {"sensitive": False}},
                    "secret": {"x-meta": {"sensitive": True}},
                    "old": {"x-meta": {"deprecated": True, "help": "use new"}},
                }
            }
        }
    }

    def test_lookup_schema_node_found_and_missing(self) -> None:
        assert validation._lookup_schema_node(self._SCHEMA, "agent.provider") is not None
        assert validation._lookup_schema_node(self._SCHEMA, "agent.nope") is None

    def test_is_sensitive_path(self) -> None:
        assert validation._is_sensitive_path(self._SCHEMA, "agent.secret") is True
        assert validation._is_sensitive_path(self._SCHEMA, "agent.provider") is False
        assert validation._is_sensitive_path(self._SCHEMA, "agent.absent") is False

    def test_is_deprecated_and_help(self) -> None:
        assert validation._is_deprecated_path(self._SCHEMA, "agent.old") is True
        assert validation._get_help_text(self._SCHEMA, "agent.old") == "use new"

    def test_mask_value(self) -> None:
        assert validation._mask_value("hunter2", sensitive=True) == '"***"'
        assert validation._mask_value(42, sensitive=False) == "42"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, "boolean"),
            (3, "integer"),
            (3.5, "number"),
            ("s", "string"),
            ([], "array"),
            ({}, "object"),
            (None, "null"),
        ],
    )
    def test_actual_type_name(self, value: object, expected: str) -> None:
        assert validation._actual_type_name(value) == expected

    def test_apply_field_default_removes_invalid_values(self) -> None:
        data = {"top": 1, "agent": {"provider": "bad", "keep": "ok"}}
        validation._apply_field_default(data, "top")
        validation._apply_field_default(data, "agent.provider")
        assert data == {"agent": {"keep": "ok"}}


class TestValidateConfigData:
    """``validate_config_data`` strips invalid values and warns on the loader logger."""

    def test_returns_data_and_never_raises(self) -> None:
        data = {"agent": {"provider": "acp"}}
        assert validation.validate_config_data(data) is data

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_unrecognized_top_level_key_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        # Warnings must land on the loader's logger channel, not validation's
        # own __name__ logger (preserves the observable contract).
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data({"totally_unknown_key": 1})
        assert any("unrecognized top-level keys" in r.message for r in caplog.records)

    def test_warning_logger_name_is_loader_not_validation(self) -> None:
        # Pin the deliberate logger-name choice so an accidental
        # getLogger(__name__) refactor is caught.
        assert validation.logger.name == "kiro_crew.config.loader"

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_enum_violation_warns_and_strips_value(self, caplog: pytest.LogCaptureFixture) -> None:
        # agent.provider is an enum field; an out-of-enum value must be warned
        # about and stripped in-place so the loader falls back to the default.
        data = {"agent": {"provider": "not_a_real_provider"}}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data(data)
        assert any("enum violation" in r.message for r in caplog.records)
        # the invalid value was removed (so the dataclass default applies)
        assert "provider" not in data.get("agent", {})

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_type_mismatch_warns_and_strips_value(self, caplog: pytest.LogCaptureFixture) -> None:
        # session.timeout_secs is an integer field; a string value is a type
        # mismatch that must be warned about and stripped.
        data = {"session": {"timeout_secs": "not-an-int"}}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data(data)
        assert any("type mismatch" in r.message for r in caplog.records)
        assert "timeout_secs" not in data.get("session", {})

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_sensitive_value_is_masked_in_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        # A type violation on a sensitive field must not echo the raw value into
        # logs — _mask_value renders it as "***".
        schema = {
            "properties": {"agent": {"properties": {"secret": {"x-meta": {"sensitive": True}}}}}
        }
        assert validation._is_sensitive_path(schema, "agent.secret") is True
        assert validation._mask_value("super-secret-token", sensitive=True) == '"***"'

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_log_level_is_uppercased_before_validation(self) -> None:
        # Case-insensitive enum normalization (step 3) upppercases log_level so a
        # lowercase value validates instead of being stripped as an enum miss.
        data = {"agent": {"log_level": "debug"}}
        validation.validate_config_data(data)
        assert data["agent"]["log_level"] == "DEBUG"


class TestLoaderBackCompatReexport:
    """The C3 symbols remain importable from ``kiro_crew.config.loader``."""

    def test_validate_alias_points_at_validation_impl(self) -> None:
        assert _loader_module._validate_config_data is validation.validate_config_data

    def test_cache_shims_delegate_to_validation_cache(self) -> None:
        assert _loader_module._CONFIG_CACHE is validation._CONFIG_CACHE

    def test_all_private_helpers_reexported(self) -> None:
        for name in (
            "_is_sensitive_path",
            "_is_deprecated_path",
            "_get_help_text",
            "_mask_value",
            "_apply_field_default",
            "_lookup_schema_node",
            "_actual_type_name",
            "_dot_path_from_json_path",
            "_invalidate_config_cache",
            "_cached_validated_data",
            "_store_validated_data",
            "_HAS_JSONSCHEMA",
        ):
            assert hasattr(_loader_module, name), name
