from __future__ import annotations

import hashlib
import json

import pytest

from kiro_crew.personal_insights import insights_registry as registry
from kiro_crew.personal_insights.insights_placeholders import (
    contains_placeholder,
    declared_literal_overlaps_placeholder,
)
from kiro_crew.personal_insights.insights_registry import (
    CLASSES,
    REGISTRY_DIGEST,
    REGISTRY_PATH,
    REGISTRY_SCHEMA_VERSION,
    RegistryError,
    load_registry,
    parse_registry,
)


def _bytes() -> bytes:
    return REGISTRY_PATH.read_bytes()


def _payload() -> dict:
    return json.loads(_bytes())


def test_build_time_embedded_digest_equals_shipped_bytes() -> None:
    assert hashlib.sha256(_bytes()).hexdigest() == REGISTRY_DIGEST


def test_schema_version_is_capability_3_0() -> None:
    loaded = load_registry()
    assert loaded.schema_version == "kiro.personal-insights.capability/3.0"
    assert REGISTRY_SCHEMA_VERSION == "kiro.personal-insights.capability/3.0"


def test_one_entry_per_class_plus_no_action() -> None:
    loaded = load_registry()
    present = {capability.cls for capability in loaded.capabilities}
    assert present == set(CLASSES)
    assert any(capability.capability_id == "no-action" for capability in loaded.capabilities)


def test_existing_capability_uses_real_read_only_command() -> None:
    loaded = load_registry()
    existing = loaded.by_id("existing.app-list")
    assert existing.template.kind == "static_argv"
    assert existing.template.argv_tokens == ("kirocrew", "app", "list")


def test_digest_tamper_is_rejected(tmp_path, monkeypatch) -> None:
    tampered = _bytes().replace(b"prompt.context-setup-reuse", b"prompt.context-setup-reusex")
    target = tmp_path / "capability-registry-p1.json"
    target.write_bytes(tampered)
    monkeypatch.setattr(registry, "REGISTRY_PATH", target)
    with pytest.raises(RegistryError, match="digest mismatch"):
        load_registry()


def test_absent_registry_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "missing.json")
    with pytest.raises(RegistryError, match="absent"):
        load_registry()


def test_zero_byte_registry_is_rejected(tmp_path, monkeypatch) -> None:
    target = tmp_path / "capability-registry-p1.json"
    target.write_bytes(b"")
    monkeypatch.setattr(registry, "REGISTRY_PATH", target)
    with pytest.raises(RegistryError, match="empty"):
        load_registry()


def test_sidecar_sha256_is_forbidden(tmp_path, monkeypatch) -> None:
    target = tmp_path / "capability-registry-p1.json"
    target.write_bytes(_bytes())
    (tmp_path / "capability-registry-p1.json.sha256").write_text(REGISTRY_DIGEST, encoding="utf-8")
    monkeypatch.setattr(registry, "REGISTRY_PATH", target)
    with pytest.raises(RegistryError, match="sidecar"):
        load_registry()


def test_registry_path_not_from_home_or_env() -> None:
    assert REGISTRY_PATH.is_absolute()
    assert "personal_insights" in str(REGISTRY_PATH)


def test_unknown_top_level_field_is_rejected() -> None:
    payload = _payload()
    payload["surprise"] = 1
    with pytest.raises(RegistryError, match="unknown field"):
        parse_registry(json.dumps(payload).encode("utf-8"))


def test_unknown_entry_field_is_rejected() -> None:
    payload = _payload()
    payload["capabilities"][0]["surprise"] = 1
    with pytest.raises(RegistryError, match="unknown field"):
        parse_registry(json.dumps(payload).encode("utf-8"))


def test_unknown_template_field_is_rejected() -> None:
    payload = _payload()
    payload["capabilities"][0]["template"]["surprise"] = 1
    with pytest.raises(RegistryError, match="unknown field"):
        parse_registry(json.dumps(payload).encode("utf-8"))


def test_unknown_class_is_rejected() -> None:
    payload = _payload()
    payload["capabilities"][0]["class"] = "frobnicate"
    with pytest.raises(RegistryError, match="unknown capability class"):
        parse_registry(json.dumps(payload).encode("utf-8"))


def test_duplicate_priority_within_behavior_scope_is_rejected() -> None:
    payload = _payload()
    behavior = ["session_owner|repeats_context_setup|positive|global"]
    payload["capabilities"][0]["behavior_keys"] = behavior
    payload["capabilities"][0]["selection_priority"] = 55
    payload["capabilities"][1]["behavior_keys"] = behavior
    payload["capabilities"][1]["selection_priority"] = 55
    with pytest.raises(RegistryError, match="duplicate selection_priority"):
        parse_registry(json.dumps(payload).encode("utf-8"))


def test_distinct_behavior_may_reuse_priority() -> None:
    payload = _payload()
    payload["capabilities"][0]["selection_priority"] = 77
    payload["capabilities"][1]["selection_priority"] = 77
    parsed = parse_registry(json.dumps(payload).encode("utf-8"))
    assert len(parsed.capabilities) == 5


def test_empty_argv_token_is_rejected() -> None:
    payload = _payload()
    for capability in payload["capabilities"]:
        if capability["template"]["kind"] == "static_argv":
            capability["template"]["argv_tokens"] = ["kirocrew", "", "list"]
    with pytest.raises(RegistryError, match="empty or non-string argv token"):
        parse_registry(json.dumps(payload).encode("utf-8"))


@pytest.mark.parametrize("token", ["{behavior}", "$HOME", "<slot>", "arg}"])
def test_dynamic_argv_source_is_rejected(token: str) -> None:
    payload = _payload()
    for capability in payload["capabilities"]:
        if capability["template"]["kind"] == "static_argv":
            capability["template"]["argv_tokens"] = ["kirocrew", token]
    with pytest.raises(RegistryError, match="dynamic argv token source"):
        parse_registry(json.dumps(payload).encode("utf-8"))


def test_non_static_template_may_not_carry_argv() -> None:
    payload = _payload()
    payload["capabilities"][0]["template"]["argv_tokens"] = ["kirocrew"]
    with pytest.raises(RegistryError, match="only static_argv"):
        parse_registry(json.dumps(payload).encode("utf-8"))


@pytest.mark.parametrize("literal", ["<slot>", "{{x}}", "[CAP]", "TODO", "YOUR_NAME"])
def test_declared_literal_overlapping_placeholder_is_rejected(literal: str) -> None:
    assert declared_literal_overlaps_placeholder(literal) is True
    payload = _payload()
    payload["capabilities"][0]["template"]["declared_literals"] = [literal]
    with pytest.raises(RegistryError, match="overlaps a placeholder"):
        parse_registry(json.dumps(payload).encode("utf-8"))


def test_benign_declared_literal_is_allowed() -> None:
    assert contains_placeholder("a == b") is False
    payload = _payload()
    payload["capabilities"][0]["template"]["declared_literals"] = ["a == b", "x >= y"]
    parsed = parse_registry(json.dumps(payload).encode("utf-8"))
    assert parsed.capabilities[0].template.declared_literals == ("a == b", "x >= y")


def test_schema_version_mismatch_is_rejected() -> None:
    payload = _payload()
    payload["schema_version"] = "kiro.personal-insights.capability/2.0"
    with pytest.raises(RegistryError, match="schema version mismatch"):
        parse_registry(json.dumps(payload).encode("utf-8"))


def test_missing_class_entry_is_rejected() -> None:
    payload = _payload()
    payload["capabilities"] = [c for c in payload["capabilities"] if c["class"] != "prompt"]
    with pytest.raises(RegistryError, match="missing an entry for class: prompt"):
        parse_registry(json.dumps(payload).encode("utf-8"))


# ── Spec v6 defect-closure behavioral tests ──


def test_canonical_lesson_fields_are_exact_spec_v6() -> None:
    from kiro_crew.personal_insights.insights_canonical import LESSON_FIELDS

    assert LESSON_FIELDS == ("rule", "category", "negative", "repo_scope", "applies")


def test_canonical_lesson_digest_uses_applies_not_scope() -> None:
    from kiro_crew.personal_insights.insights_canonical import canonical_lesson_digest

    with_applies = canonical_lesson_digest(
        {
            "rule": "r",
            "category": "preference",
            "negative": None,
            "repo_scope": None,
            "applies": "always",
        }
    )
    without = canonical_lesson_digest(
        {
            "rule": "r",
            "category": "preference",
            "negative": None,
            "repo_scope": None,
            "applies": "on_topic",
        }
    )
    assert with_applies != without


def test_negative_selection_priority_is_rejected() -> None:
    payload = _payload()
    payload["capabilities"][0]["selection_priority"] = -1
    with pytest.raises(RegistryError, match="selection_priority"):
        parse_registry(json.dumps(payload).encode("utf-8"))


def test_unknown_behavior_key_is_rejected() -> None:
    payload = _payload()
    payload["capabilities"][0]["behavior_keys"] = ["bad|key|shape"]
    with pytest.raises(RegistryError, match="behavior key"):
        parse_registry(json.dumps(payload).encode("utf-8"))


def test_unknown_platform_is_rejected() -> None:
    payload = _payload()
    payload["capabilities"][0]["supported_platforms"] = ["plan9"]
    with pytest.raises(RegistryError, match="platform"):
        parse_registry(json.dumps(payload).encode("utf-8"))


def test_wrong_container_type_is_rejected() -> None:
    payload = _payload()
    payload["capabilities"][0]["behavior_keys"] = "not-a-list"
    with pytest.raises(RegistryError, match="must be a list"):
        parse_registry(json.dumps(payload).encode("utf-8"))


def test_empty_required_string_is_rejected() -> None:
    payload = _payload()
    payload["capabilities"][0]["capability_id"] = ""
    with pytest.raises(RegistryError, match="non-empty"):
        parse_registry(json.dumps(payload).encode("utf-8"))


def test_static_argv_requires_positive_output_bound() -> None:
    payload = _payload()
    for cap in payload["capabilities"]:
        if cap["template"]["kind"] == "static_argv":
            cap["template"]["max_output_bytes"] = 0
    with pytest.raises(RegistryError, match="positive"):
        parse_registry(json.dumps(payload).encode("utf-8"))


def test_static_argv_requires_read_only_argv_verification() -> None:
    payload = _payload()
    for cap in payload["capabilities"]:
        if cap["template"]["kind"] == "static_argv":
            cap["verification_template"] = "verify.state-readback.lesson"
    with pytest.raises(RegistryError, match="read_only_argv"):
        parse_registry(json.dumps(payload).encode("utf-8"))


def test_local_state_entry_requires_exact_undo() -> None:
    payload = _payload()
    for cap in payload["capabilities"]:
        if cap["risk_class"] == "local_state_proposal":
            cap["undo_template"] = "undo.none"
    with pytest.raises(RegistryError, match="undo"):
        parse_registry(json.dumps(payload).encode("utf-8"))


def test_documentation_sources_are_real_canonical_ids() -> None:
    from kiro_crew.personal_insights.insights_registry import DOCUMENTATION_SOURCES

    loaded = load_registry()
    for capability in loaded.capabilities:
        assert capability.documentation_source in DOCUMENTATION_SOURCES


def test_existing_capability_matches_needs_capability_discovery() -> None:
    loaded = load_registry()
    existing = loaded.by_id("existing.app-list")
    assert any("needs_capability_discovery" in key for key in existing.behavior_keys)
    assert existing.verification_template.startswith("verify.read-only-argv")


# ── panel round: registry verification mapping (exact, not startswith) ──


def test_verification_templates_are_exact_closed_values() -> None:
    from kiro_crew.personal_insights.insights_registry import (
        VERIFICATION_TEMPLATE_KINDS,
        verification_kind_for_template,
    )

    loaded = load_registry()
    for capability in loaded.capabilities:
        assert capability.verification_template in VERIFICATION_TEMPLATE_KINDS
        kind = verification_kind_for_template(capability.verification_template)
        assert kind in {"read_only_argv", "state_readback", "future_observation"}


def test_unknown_verification_template_is_rejected_even_with_known_prefix() -> None:
    from kiro_crew.personal_insights.insights_registry import (
        RegistryError,
        verification_kind_for_template,
    )

    with pytest.raises(RegistryError):
        verification_kind_for_template("verify.read-only-argv.not-a-real-entry")
    with pytest.raises(RegistryError):
        verification_kind_for_template("verify.read-only-argv")


def test_registry_build_rejects_unknown_verification_template() -> None:
    payload = _payload()
    payload["capabilities"][0]["verification_template"] = "verify.read-only-argv.bogus"
    with pytest.raises(RegistryError, match="verification template"):
        parse_registry(json.dumps(payload).encode("utf-8"))
