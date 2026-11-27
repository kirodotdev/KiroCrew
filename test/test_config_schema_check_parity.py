"""``config.schema_check`` gives jsonschema's answers, so config cleaning did not change.

``validate_config_data`` deletes a field on each error while it walks the errors, so
the in-house checker must yield the errors ``jsonschema.Draft7Validator`` yields: the
same keyword, path and message, in the same order. These tests compare the two on
random documents drawn from the real config schema, run the whole cleaning step under
each, and pin that a schema keyword the checker does not implement is refused.

jsonschema is imported only here: it stays a declared dependency as this reference.
"""

from __future__ import annotations

import copy
import json
import logging
import random
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from kiro_crew.config import loader, schema_check, validation
from kiro_crew.config.loader import (
    DEGRADED_TAILSCALE,
    KiroCrewConfig,
    tailnet_effective_allowed_logins,
    tailnet_identity_unknown,
)
from kiro_crew.config.schema import JSON_SCHEMA

#: Values of every JSON type, including the ones type checks trip on (bool vs int, 3.0).
_VALUES: tuple[Any, ...] = (
    None, True, False, 0, 1, -3, 2.5, 3.0, "", "x", "auto", "a" * 300,
    [], [1, "a"], {}, {"zz": 1}, {"a": {"b": 1}},
)  # fmt: skip


def _sample(rng: random.Random, schema: Any, depth: int = 0) -> Any:
    """A document near *schema*: mostly the right shape, often a wrong type, enum
    miss or extra key, so most documents carry several errors at several depths."""
    if not isinstance(schema, dict) or depth > 6 or rng.random() < 0.15:
        return rng.choice(_VALUES)
    if "enum" in schema and rng.random() < 0.6:
        return rng.choice([*schema["enum"], rng.choice(_VALUES)])
    kind = schema.get("type")
    kind = kind[0] if isinstance(kind, list) else kind
    if kind == "object" or "properties" in schema:
        out: dict[str, Any] = {}
        for key, sub in schema.get("properties", {}).items():
            if rng.random() < 0.5:
                out[key] = _sample(rng, sub, depth + 1)
        for _ in range(rng.choice((0, 0, 0, 1, 2))):
            out[f"extra_{rng.randint(0, 3)}"] = rng.choice(_VALUES)
        extra_schema = schema.get("additionalProperties")
        if isinstance(extra_schema, dict):
            for _ in range(rng.choice((0, 1, 2))):
                out[f"k{rng.randint(0, 3)}"] = _sample(rng, extra_schema, depth + 1)
        return out
    if kind == "array":
        return [_sample(rng, schema.get("items", {}), depth + 1) for _ in range(rng.randint(0, 3))]
    by_type: dict[str, tuple[Any, ...]] = {
        "string": ("", "x", "auto", "a" * 300, 5),
        "integer": (0, 1, 2.0, 2.5, True, "3"),
        "number": (0, 1.5, True, "3", None),
        "boolean": (True, False, 0, "true"),
    }
    return rng.choice(by_type.get(kind, _VALUES))


def _errors(errors: Iterator[Any]) -> list[tuple[Any, ...]]:
    return [(e.validator, list(e.absolute_path), e.message, repr(e.instance)) for e in errors]


def _documents(seed: int, count: int) -> Iterator[Any]:
    rng = random.Random(seed)
    for _ in range(count):
        yield _sample(rng, JSON_SCHEMA)


@pytest.fixture(scope="module")
def reference() -> jsonschema.Draft7Validator:
    jsonschema.Draft7Validator.check_schema(JSON_SCHEMA)
    return jsonschema.Draft7Validator(JSON_SCHEMA)


@pytest.fixture(scope="module")
def checker() -> schema_check.SchemaChecker:
    schema_check.check_schema(JSON_SCHEMA)
    return schema_check.SchemaChecker(JSON_SCHEMA)


@pytest.mark.parametrize("seed", [1, 2])
def test_the_config_schema_yields_jsonschemas_errors_in_its_order(
    seed: int, reference: jsonschema.Draft7Validator, checker: schema_check.SchemaChecker
) -> None:
    total = 0
    for doc in _documents(seed, 300):
        want = _errors(reference.iter_errors(doc))
        assert _errors(checker.iter_errors(doc)) == want, doc
        assert checker.is_valid(doc) is reference.is_valid(doc)
        total += len(want)
    # The draw must exercise the checker, not compare empty lists.
    assert total > 5_000


#: Small schemas for the edges the config schema has no instance of today.
_EDGE_SCHEMAS: tuple[dict[str, Any], ...] = (
    {"type": "integer"},
    {"type": "number"},
    {"type": ["integer", "null"]},
    {"type": ["string", "boolean"]},
    {"enum": [1, "a", None, [1, 2], {"k": True}]},
    {"enum": [True]},
    {"enum": [0, 1.0]},
    {"items": False},
    {"items": True},
    {"items": {"type": "string", "maxLength": 0}},
    {"properties": {"a": False, "b": True}},
    {"additionalProperties": False, "properties": {"a": {}}},
    {"additionalProperties": {"type": "integer"}},
    {"maxLength": 2, "pattern": "^a"},
    {"type": "object", "additionalProperties": False},
)
_EDGE_VALUES: tuple[Any, ...] = (
    *_VALUES, 1.0, 0.0, -0.0, [True], [1.0, 2], {"k": 1}, {"k": True}, {"a": 1, "b": 2, "c": 3},
    {"z": 1, "y": 2, 3: 4}, ["", "ab"], "ab", "abc", "ba",
)  # fmt: skip


@pytest.mark.parametrize("schema", _EDGE_SCHEMAS, ids=lambda s: json.dumps(s))
def test_edge_schemas_match_jsonschema(schema: dict[str, Any]) -> None:
    jsonschema.Draft7Validator.check_schema(schema)
    schema_check.check_schema(schema)
    reference = jsonschema.Draft7Validator(schema)
    checker = schema_check.SchemaChecker(schema)
    for value in _EDGE_VALUES:
        assert _errors(checker.iter_errors(value)) == _errors(reference.iter_errors(value)), value


def _clean(doc: dict, validator: Any, monkeypatch: pytest.MonkeyPatch, caplog: Any) -> tuple:
    """*doc* after ``validate_config_data`` with *validator*, and what it logged."""
    monkeypatch.setattr(validation, "_schema_validator", lambda _schema: validator)
    # A deprecated field warns once per process; each run must start from none told.
    monkeypatch.setattr(validation, "_REPORTED_DEPRECATED_FIELDS", set())
    caplog.clear()
    with caplog.at_level(logging.INFO):
        out = validation.validate_config_data(copy.deepcopy(doc))
    # Only the schema step's own lines: the earlier steps warn once per process.
    return out, [
        (r.name, r.levelno, r.getMessage())
        for r in caplog.records
        if r.getMessage().startswith(_SCHEMA_STEP_LINES)
    ]


_SCHEMA_STEP_LINES = (
    "Config: enum violation",
    "Config: type mismatch",
    "Config: validation error",
    "Config: flat workspace",
)


def test_cleaning_a_config_is_unchanged(
    reference: jsonschema.Draft7Validator,
    checker: schema_check.SchemaChecker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole cleaning step, which deletes while it walks: same document, same log."""
    logged = 0
    for doc in _documents(3, 150):
        if not isinstance(doc, dict):
            continue
        want = _clean(doc, reference, monkeypatch, caplog)
        assert _clean(doc, checker, monkeypatch, caplog) == want, doc
        logged += len(want[1])
    assert logged > 1_000


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#/definitions/x"},
        {"oneOf": [{"type": "string"}]},
        {"anyOf": [{"type": "string"}]},
        {"minimum": 0},
        {"required": ["a"]},
        {"patternProperties": {"^a": {}}},
        {"items": [{"type": "string"}]},
        {"properties": {"a": {"const": 1}}},
        {"additionalProperties": {"format": "email"}},
        {"type": "int"},
        {"type": []},
        {"type": ["string", "string"]},
        {"enum": "a"},
        {"maxLength": -1},
        {"maxLength": True},
        {"pattern": "("},
        {"properties": {"a": {"$schema": schema_check.DRAFT_07}}},
        {"$schema": "https://json-schema.org/draft/2020-12/schema"},
        {"properties": []},
        [],
    ],
    ids=lambda s: json.dumps(s),
)
def test_a_keyword_the_checker_does_not_implement_is_refused(schema: Any) -> None:
    with pytest.raises(ValueError):
        schema_check.check_schema(schema)


def test_annotations_and_crew_metadata_are_accepted() -> None:
    schema_check.check_schema(
        {
            "$schema": schema_check.DRAFT_07,
            "title": "t",
            "description": "d",
            "$comment": "c",
            "default": 1,
            "examples": [1],
            "x-meta": {"anything": True},
            "properties": {"a": {"type": "integer", "default": 0, "x-meta": {}}},
        }
    )


def test_no_kiro_crew_module_imports_jsonschema() -> None:
    """Only this test may: a process that loads its config must not pay for it."""
    src = Path(validation.__file__).resolve().parents[1]
    importers = [
        str(path.relative_to(src))
        for path in sorted(src.rglob("*.py"))
        if any(
            line.strip().startswith(("import jsonschema", "from jsonschema"))
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    ]
    assert importers == []


class TestSecurityFallbacksAreUnchanged:
    """A malformed narrowing of a default-open allowlist must stay evidence the loader
    degrades, under the in-house checker exactly as under jsonschema."""

    TAILNET = {
        "enabled": True,
        "trust_identity": True,
        "allowed_logins": ["alice@example.com"],
        "pin_scope": "node",
    }

    def _loaded(self, home: Path, document: dict, validator: Any, monkeypatch: Any) -> Any:
        (home / "config.json").write_text(json.dumps(document), encoding="utf-8")
        monkeypatch.setattr(loader, "config_path", lambda: home / "config.json")
        monkeypatch.setattr(loader, "config_local_path", lambda: home / "config.local.json")
        monkeypatch.setattr(loader, "config_dir", lambda: home)
        monkeypatch.setattr(validation, "_schema_validator", lambda _schema: validator)
        loader._invalidate_config_cache()
        loader.reset_degraded_observations()
        try:
            return KiroCrewConfig.load()
        finally:
            loader._invalidate_config_cache()
            loader.reset_degraded_observations()

    def _both(self, tmp_path: Path, document: dict, monkeypatch: Any) -> list[Any]:
        jsonschema_validator = jsonschema.Draft7Validator(JSON_SCHEMA)
        in_house = schema_check.SchemaChecker(JSON_SCHEMA)
        loaded = []
        for name, validator in (("jsonschema", jsonschema_validator), ("in-house", in_house)):
            (tmp_path / name).mkdir()
            loaded.append(self._loaded(tmp_path / name, document, validator, monkeypatch))
        return loaded

    @pytest.mark.parametrize(
        "value", ["github", [1, "github"], {"github": True}, 7, None], ids=repr
    )
    def test_a_malformed_publish_allowed_destinations(
        self, value: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = {"publish": {"allowed_destinations": value}}
        kept = validation.validate_config_data(copy.deepcopy(document))
        assert kept["publish"]["allowed_destinations"] == value
        before, after = self._both(tmp_path, document, monkeypatch)
        assert after.publish.allowed_destinations == before.publish.allowed_destinations
        assert after.degraded_sections == before.degraded_sections
        # ``publish_governance`` denies every publish while the section is degraded.
        assert "publish" in after.degraded_sections

    @pytest.mark.parametrize(
        "value", ["alice@example.com", [1], [1, "alice@example.com"], {"a": 1}, 7], ids=repr
    )
    def test_a_malformed_tailscale_allowed_logins(
        self, value: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = {"dashboard": {"tailscale": {**self.TAILNET, "allowed_logins": value}}}
        kept = validation.validate_config_data(copy.deepcopy(document))
        assert kept["dashboard"]["tailscale"]["allowed_logins"] == value
        before, after = self._both(tmp_path, document, monkeypatch)

        def enforced(cfg: KiroCrewConfig) -> tuple:
            ts = cfg.dashboard.tailscale
            sections = cfg.degraded_sections
            return (
                ts.trust_identity,
                tuple(ts.allowed_logins),
                tailnet_effective_allowed_logins(sections, ts.allowed_logins),
                tailnet_identity_unknown(sections),
            )

        assert enforced(after) == enforced(before)
        assert after.degraded_sections == before.degraded_sections
        assert DEGRADED_TAILSCALE in after.degraded_sections
        assert "bob@example.com" not in enforced(after)[2]
