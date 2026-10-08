"""App Kit manifest contract for app-provided work acceptance kinds."""

from __future__ import annotations

from kiro_crew.apps.manifest import (
    MAX_ACCEPTANCE_INPUT_FIELDS,
    MAX_ACCEPTANCE_KINDS_PER_APP,
    MAX_ACCEPTANCE_STRING_CHARS,
    AppManifest,
)
from kiro_crew.work_vocab import (
    APP_ACCEPTANCE_APP_ID_MAX_CHARS,
    APP_ACCEPTANCE_KIND_ID_MAX_CHARS,
    app_acceptance_kind,
    split_app_acceptance_kind,
)

_DECLARATION = {
    "id": "release-ready",
    "inputSchema": {
        "type": "object",
        "properties": {
            "change_id": {"type": "integer", "minimum": 1, "maximum": 999999},
            "environment": {
                "type": "string",
                "enum": ["test", "production"],
                "minLength": 1,
                "maxLength": 32,
            },
            "strict": {"type": "boolean"},
        },
        "required": ["change_id", "environment"],
        "additionalProperties": False,
    },
    "endpoint": "acceptance/release-ready",
}


def _manifest(*, name: str = "release-app", kinds: object = None) -> AppManifest:
    contributes: dict[str, object] = {}
    if kinds is not None:
        contributes["acceptanceKinds"] = kinds
    return AppManifest.from_dict(
        {
            "name": name,
            "version": "1.0.0",
            "displayName": "Release App",
            "description": "Checks release state.",
            "backend": {"entryPoint": "server.py"},
            "contributes": contributes,
        }
    )


def test_acceptance_kind_round_trips_with_a_host_validated_schema() -> None:
    manifest = _manifest(kinds=[_DECLARATION])
    assert manifest.validate() == []
    contribution = manifest.contributes.acceptanceKinds[0]
    assert contribution.qualified_name(manifest.name) == "release-app:release-ready"
    assert (
        contribution.inputSchema.validate_input(
            {"change_id": 7, "environment": "test", "strict": True}
        )
        == []
    )
    serialized = manifest.to_dict()["contributes"]["acceptanceKinds"][0]
    assert serialized == _DECLARATION


def test_namespaces_are_structural_and_cannot_collide_with_builtins_or_siblings() -> None:
    first = app_acceptance_kind("release-app", "release-ready")
    second = app_acceptance_kind("other-app", "release-ready")
    assert first == "release-app:release-ready"
    assert second == "other-app:release-ready"
    assert first != second
    assert first not in {"pr_checks", "file", "human_approval", "cmd"}
    assert split_app_acceptance_kind(first) == ("release-app", "release-ready")
    assert split_app_acceptance_kind("release-app:release-ready:extra") is None
    assert split_app_acceptance_kind("release-app:file_name") is None


def test_duplicate_kind_ids_are_refused_within_one_app() -> None:
    errors = _manifest(kinds=[_DECLARATION, dict(_DECLARATION)]).validate()
    assert any("duplicate id" in error for error in errors)


def test_non_array_and_non_object_entries_are_reported_not_erased() -> None:
    malformed = _manifest(kinds="release-ready")
    assert malformed.contributes.bad_acceptance_kinds is True
    assert any("must be an array" in error for error in malformed.validate())

    dropped = _manifest(kinds=[_DECLARATION, "bad"])
    assert dropped.contributes.dropped_acceptance_kinds == 1
    assert any("must be an object" in error for error in dropped.validate())


def test_declaration_has_only_id_schema_and_fixed_relative_endpoint() -> None:
    for extra in (
        {"command": "check-release"},
        {"argv": ["check-release"]},
        {"url": "https://example.test/check"},
        {"mcpServer": "release"},
        {"tool": "check_release"},
    ):
        errors = _manifest(kinds=[{**_DECLARATION, **extra}]).validate()
        assert any("unsupported fields" in error for error in errors), extra

    for endpoint in (
        "https://example.test/check",
        "/api/apps/release-app/check",
        "../check",
        "check?mode=fast",
        "check\\escape",
        "check/{item}",
    ):
        errors = _manifest(kinds=[{**_DECLARATION, "endpoint": endpoint}]).validate()
        assert any("fixed app-relative path" in error for error in errors), endpoint


def test_schema_rejects_programs_nested_objects_and_unbounded_strings() -> None:
    forbidden_names = ("command", "argv", "executable_path", "url", "mcp_server", "tool_name")
    for name in forbidden_names:
        declaration = {
            **_DECLARATION,
            "inputSchema": {
                "type": "object",
                "properties": {name: {"type": "string", "maxLength": 20}},
                "required": [],
                "additionalProperties": False,
            },
        }
        errors = _manifest(kinds=[declaration]).validate()
        assert any("capability-shaped input name" in error for error in errors), name

    for property_schema in (
        {"type": "object"},
        {"type": "array"},
        {"type": "integer"},
        {"type": "number", "minimum": float("-inf"), "maximum": 10},
        {"type": "string", "pattern": ".*"},
        {"type": "string"},
        {"type": "string", "maxLength": MAX_ACCEPTANCE_STRING_CHARS + 1},
    ):
        declaration = {
            **_DECLARATION,
            "inputSchema": {
                "type": "object",
                "properties": {"value": property_schema},
                "required": [],
                "additionalProperties": False,
            },
        }
        assert _manifest(kinds=[declaration]).validate(), property_schema


def test_schema_rejects_undeclared_or_wrong_typed_stored_input() -> None:
    schema = _manifest(kinds=[_DECLARATION]).contributes.acceptanceKinds[0].inputSchema
    assert schema.validate_input({"change_id": 7, "environment": "test"}) == []
    assert schema.validate_input({"change_id": True, "environment": "test"})
    assert schema.validate_input({"change_id": 7, "environment": "staging"})
    assert schema.validate_input({"change_id": 7})
    assert schema.validate_input({"change_id": 7, "environment": "test", "extra": "not declared"})


def test_integer_input_range_checks_arbitrary_precision_values() -> None:
    huge = 10**400
    declaration = {
        **_DECLARATION,
        "inputSchema": {
            "type": "object",
            "properties": {"value": {"type": "integer", "minimum": -huge, "maximum": huge}},
            "required": ["value"],
            "additionalProperties": False,
        },
    }
    manifest = _manifest(kinds=[declaration])
    assert manifest.validate() == []
    schema = manifest.contributes.acceptanceKinds[0].inputSchema

    for value in (-huge, 0, huge):
        assert schema.validate_input({"value": value}) == []
    assert any("above maximum" in error for error in schema.validate_input({"value": huge + 1}))
    assert any("below minimum" in error for error in schema.validate_input({"value": -huge - 1}))
    assert schema.validate_input({"value": True}) == ["input.value: expected integer"]
    assert schema.validate_input({"value": 1.0}) == ["input.value: expected integer"]
    assert schema.validate_input({"value": 0, "extra": 1}) == [
        "input has undeclared fields ['extra']"
    ]


def test_number_input_accepts_finite_values_and_rejects_non_finite_floats() -> None:
    declaration = {
        **_DECLARATION,
        "inputSchema": {
            "type": "object",
            "properties": {"value": {"type": "number", "minimum": -10, "maximum": 10}},
            "required": ["value"],
            "additionalProperties": False,
        },
    }
    manifest = _manifest(kinds=[declaration])
    assert manifest.validate() == []
    schema = manifest.contributes.acceptanceKinds[0].inputSchema

    for value in (-10, 0, 10, -9.5, 0.25, 9.5):
        assert schema.validate_input({"value": value}) == []
    assert any("above maximum" in error for error in schema.validate_input({"value": 10**400}))
    assert any("below minimum" in error for error in schema.validate_input({"value": -(10**400)}))
    for value in (float("nan"), float("inf"), float("-inf")):
        assert schema.validate_input({"value": value}) == ["input.value: expected number"]
    assert schema.validate_input({"value": True}) == ["input.value: expected number"]


def test_kind_and_property_caps_are_refused() -> None:
    overlong_app_id = "a" * (APP_ACCEPTANCE_APP_ID_MAX_CHARS + 1)
    assert app_acceptance_kind(overlong_app_id, "ready") == ""
    assert any(
        "requires an app name of at most" in error
        for error in _manifest(name=overlong_app_id, kinds=[_DECLARATION]).validate()
    )

    overlong_id = "k" * (APP_ACCEPTANCE_KIND_ID_MAX_CHARS + 1)
    assert app_acceptance_kind("release-app", overlong_id) == ""
    assert any(
        "id must be a lowercase kebab slug" in error
        for error in _manifest(kinds=[dict(_DECLARATION, id=overlong_id)]).validate()
    )

    kinds = [
        dict(_DECLARATION, id=f"kind-{index}") for index in range(MAX_ACCEPTANCE_KINDS_PER_APP + 1)
    ]
    assert any("kinds exceeds" in error for error in _manifest(kinds=kinds).validate())

    properties = {
        f"field_{index}": {"type": "boolean"} for index in range(MAX_ACCEPTANCE_INPUT_FIELDS + 1)
    }
    declaration = {
        **_DECLARATION,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": [],
            "additionalProperties": False,
        },
    }
    assert any("properties exceeds" in error for error in _manifest(kinds=[declaration]).validate())


def test_acceptance_kinds_are_covered_by_the_manifest_signature() -> None:
    payload = _manifest(kinds=[_DECLARATION]).signing_payload()
    assert b"acceptanceKinds" in payload
    changed = {**_DECLARATION, "endpoint": "acceptance/release-ready-v2"}
    assert _manifest(kinds=[changed]).signing_payload() != payload
    assert b"acceptanceKinds" not in _manifest().signing_payload()


def test_schema_reports_malformed_enum_and_required_entries_instead_of_raising() -> None:
    declaration = {
        **_DECLARATION,
        "inputSchema": {
            **_DECLARATION["inputSchema"],
            "required": ["change_id", 7],
        },
    }
    errors = _manifest(kinds=[declaration]).validate()
    assert any("required entries must be strings" in error for error in errors)

    manifest = _manifest(kinds=[_DECLARATION])
    recursive: list[object] = []
    recursive.append(recursive)
    manifest.contributes.acceptanceKinds[0].inputSchema.properties["environment"].enum = recursive
    errors = manifest.validate()
    assert any("enum values must be finite JSON scalars" in error for error in errors)


def test_acceptance_kinds_require_a_gateway_managed_server_backend() -> None:
    no_backend = _manifest(kinds=[_DECLARATION])
    no_backend.backend.entryPoint = ""
    assert any("requires backend.entryPoint" in error for error in no_backend.validate())

    client_backend = _manifest(kinds=[_DECLARATION])
    client_backend.platform.installMode = "client"
    assert any("installMode 'server'" in error for error in client_backend.validate())
