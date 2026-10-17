"""Contract tests for the registry's optional ``auth`` block.

The block says how a provider's OAuth client comes into being: ``dcr`` (the
default when absent) or ``preregistered`` (an operator registers an app in the
vendor console). The runtime branches on the mode and the runbooks print the
redirect URI, so every field is validated at LOAD -- a misspelling must fail the
registry, not silently fall back to DCR against a vendor that refuses it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.connections import RegistryValidationError, get_all_registry_providers
from kiro_crew.connections import registry as registry_module
from kiro_crew.connections.registry import (
    AUTH_MODE_DCR,
    AUTH_MODE_PREREGISTERED,
    CALLBACK_PATH,
    DEFAULT_REDIRECT_HOST,
    REDIRECT_HOSTS,
    _load_registry,
    auth_mode,
    get_preregistered_providers,
    get_provider,
    get_visible_providers,
    is_preregistered,
    redirect_host,
    redirect_uri,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

VALID_AUTH = {
    "mode": "preregistered",
    "confidential": True,
    "redirect_host": "127.0.0.1",
    "redirect_port": 48200,
    "registration_guide": "oauth-app-registration/notion.md",
}


def _load(tmp_path: Path, payload: list[dict]) -> dict[str, dict]:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(payload), encoding="utf-8")
    return {p["slug"]: p for p in _load_registry(registry_path)}


def _payload_with_auth(slug: str, auth: object) -> list[dict]:
    payload = get_all_registry_providers()
    target = next(p for p in payload if p["slug"] == slug)
    target["auth"] = auth
    return payload


def _rejects(tmp_path: Path, slug: str, auth: object, match: str) -> None:
    with pytest.raises(RegistryValidationError, match=match):
        _load(tmp_path, _payload_with_auth(slug, auth))


# ── the vocabulary ──


def test_constants_are_the_closed_vocabulary_the_runtime_branches_on():
    assert AUTH_MODE_DCR == "dcr"
    assert AUTH_MODE_PREREGISTERED == "preregistered"
    assert REDIRECT_HOSTS == ("127.0.0.1", "localhost")
    assert DEFAULT_REDIRECT_HOST == "127.0.0.1"
    assert CALLBACK_PATH == "/callback"


# ── a valid block ──


def test_a_valid_preregistered_block_loads_and_is_queryable(tmp_path):
    loaded = _load(tmp_path, _payload_with_auth("notion", dict(VALID_AUTH)))
    notion = loaded["notion"]
    assert notion["auth"] == VALID_AUTH
    assert auth_mode(notion) == AUTH_MODE_PREREGISTERED
    assert is_preregistered(notion) is True
    assert redirect_host(notion) == "127.0.0.1"
    assert redirect_uri(notion) == "http://127.0.0.1:48200/callback"


def test_redirect_host_defaults_when_the_block_omits_it(tmp_path):
    auth = {k: v for k, v in VALID_AUTH.items() if k != "redirect_host"}
    loaded = _load(tmp_path, _payload_with_auth("notion", auth))
    assert redirect_host(loaded["notion"]) == DEFAULT_REDIRECT_HOST
    assert redirect_uri(loaded["notion"]) == "http://127.0.0.1:48200/callback"


def test_localhost_is_the_other_accepted_redirect_host(tmp_path):
    auth = {**VALID_AUTH, "redirect_host": "localhost"}
    loaded = _load(tmp_path, _payload_with_auth("notion", auth))
    assert redirect_uri(loaded["notion"]) == "http://localhost:48200/callback"


def test_non_confidential_is_a_valid_shape(tmp_path):
    auth = {**VALID_AUTH, "confidential": False}
    loaded = _load(tmp_path, _payload_with_auth("notion", auth))
    assert loaded["notion"]["auth"]["confidential"] is False


def test_a_bare_dcr_block_loads_and_reads_as_dcr(tmp_path):
    loaded = _load(tmp_path, _payload_with_auth("notion", {"mode": "dcr"}))
    notion = loaded["notion"]
    assert auth_mode(notion) == AUTH_MODE_DCR
    assert is_preregistered(notion) is False
    assert redirect_uri(notion) is None


def test_an_absent_block_reads_as_dcr():
    notion = get_provider("notion")
    assert notion is not None and "auth" not in notion
    assert auth_mode(notion) == AUTH_MODE_DCR
    assert is_preregistered(notion) is False
    assert redirect_uri(notion) is None
    assert redirect_host(notion) == DEFAULT_REDIRECT_HOST


def test_port_range_bounds_are_inclusive(tmp_path):
    for port in (1024, 49151):
        loaded = _load(
            tmp_path, _payload_with_auth("notion", {**VALID_AUTH, "redirect_port": port})
        )
        assert redirect_uri(loaded["notion"]) == f"http://127.0.0.1:{port}/callback"


# ── rejected shapes ──


@pytest.mark.parametrize("bad", ["preregistered", 1, None, ["mode"], True])
def test_auth_must_be_an_object(tmp_path, bad):
    _rejects(tmp_path, "notion", bad, "auth must be an object")


def test_mode_is_required(tmp_path):
    _rejects(tmp_path, "notion", {"confidential": True}, "auth.mode is required")


@pytest.mark.parametrize("bad_mode", ["DCR", "Preregistered", "dynamic", "", None, 1])
def test_mode_must_be_from_the_closed_set(tmp_path, bad_mode):
    _rejects(tmp_path, "notion", {**VALID_AUTH, "mode": bad_mode}, "auth.mode must be one of")


@pytest.mark.parametrize(
    "extra",
    [
        {"confidential": True},
        {"redirect_port": 48200},
        {"redirect_host": "127.0.0.1"},
        {"registration_guide": "oauth-app-registration/notion.md"},
    ],
)
def test_dcr_mode_with_any_other_field_is_two_contradictory_statements(tmp_path, extra):
    _rejects(tmp_path, "notion", {"mode": "dcr", **extra}, "must carry no other fields")


def test_unknown_auth_fields_are_rejected(tmp_path):
    _rejects(
        tmp_path,
        "notion",
        {**VALID_AUTH, "client_secret": "nope"},
        "auth has unknown fields: client_secret",
    )


def test_unknown_field_on_a_dcr_block_names_the_field_not_the_mode_rule(tmp_path):
    _rejects(tmp_path, "notion", {"mode": "dcr", "extra": 1}, "auth has unknown fields: extra")


@pytest.mark.parametrize("missing", ["confidential", "redirect_port", "registration_guide"])
def test_preregistered_mode_requires_its_fields(tmp_path, missing):
    auth = {k: v for k, v in VALID_AUTH.items() if k != missing}
    _rejects(tmp_path, "notion", auth, f"preregistered auth is missing fields: {missing}")


def test_missing_fields_are_all_named(tmp_path):
    _rejects(
        tmp_path,
        "notion",
        {"mode": "preregistered"},
        "missing fields: confidential, redirect_port, registration_guide",
    )


@pytest.mark.parametrize("bad", ["true", 1, 0, None, "yes"])
def test_confidential_must_be_a_boolean(tmp_path, bad):
    _rejects(
        tmp_path, "notion", {**VALID_AUTH, "confidential": bad}, "confidential must be a boolean"
    )


@pytest.mark.parametrize("bad", [1023, 49152, 0, -1, 80, 65535, 1_000_000])
def test_port_outside_the_non_privileged_non_ephemeral_range_is_rejected(tmp_path, bad):
    _rejects(
        tmp_path,
        "notion",
        {**VALID_AUTH, "redirect_port": bad},
        "redirect_port must be between 1024 and 49151",
    )


@pytest.mark.parametrize("bad", [True, False, "48200", 48200.0, None, [48200]])
def test_port_must_be_an_integer_and_not_a_bool(tmp_path, bad):
    _rejects(
        tmp_path, "notion", {**VALID_AUTH, "redirect_port": bad}, "redirect_port must be an integer"
    )


@pytest.mark.parametrize(
    "bad", ["0.0.0.0", "::1", "[::1]", "127.0.0.2", "LOCALHOST", "localhost.", "example.com", "", 1]
)
def test_redirect_host_must_be_one_of_the_two_loopback_spellings(tmp_path, bad):
    _rejects(
        tmp_path,
        "notion",
        {**VALID_AUTH, "redirect_host": bad},
        "redirect_host must be one of 127.0.0.1, localhost",
    )


@pytest.mark.parametrize(
    "bad",
    [
        "notion.md",
        "oauth-app-registration/Notion.md",
        "oauth-app-registration/notion",
        "oauth-app-registration/notion.MD",
        "oauth-app-registration/../notion.md",
        "oauth-app-registration/sub/notion.md",
        "/oauth-app-registration/notion.md",
        "docs/guides/oauth-app-registration/notion.md",
        "oauth-app-registration/notion_v2.md",
        "oauth-app-registration/-notion.md",
        "",
        None,
        42,
    ],
)
def test_registration_guide_must_be_a_runbook_path(tmp_path, bad):
    _rejects(
        tmp_path,
        "notion",
        {**VALID_AUTH, "registration_guide": bad},
        "registration_guide must be a docs/guides path",
    )


def test_a_hyphenated_slug_guide_is_accepted(tmp_path):
    auth = {**VALID_AUTH, "registration_guide": "oauth-app-registration/google-drive.md"}
    loaded = _load(tmp_path, _payload_with_auth("notion", auth))
    assert (
        loaded["notion"]["auth"]["registration_guide"] == "oauth-app-registration/google-drive.md"
    )


def test_the_error_names_the_offending_entry_index(tmp_path):
    payload = get_all_registry_providers()
    index = next(i for i, p in enumerate(payload) if p["slug"] == "notion")
    payload[index]["auth"] = {"mode": "nope"}
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RegistryValidationError, match=f"provider at index {index}: auth.mode"):
        _load_registry(registry_path)


# ── cross-provider invariants ──


def test_duplicate_redirect_port_across_two_providers_is_rejected_at_load(tmp_path):
    payload = get_all_registry_providers()
    asana = next(p for p in payload if p["slug"] == "asana")
    asana["auth"] = {**asana["auth"], "redirect_port": 48101}  # github's port
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        RegistryValidationError, match="auth.redirect_port 48101 is already used by github"
    ):
        _load_registry(registry_path)


def test_two_preregistered_providers_on_distinct_ports_both_load(tmp_path):
    loaded = _load(tmp_path, _payload_with_auth("notion", dict(VALID_AUTH)))
    ports = {slug: p["auth"]["redirect_port"] for slug, p in loaded.items() if is_preregistered(p)}
    assert ports == {"github": 48101, "asana": 48102, "notion": 48200}


def test_a_dcr_provider_does_not_occupy_a_port(tmp_path):
    """Only a pre-registered block reserves a port; a bare dcr block cannot collide."""
    payload = get_all_registry_providers()
    for slug in ("notion", "linear"):
        next(p for p in payload if p["slug"] == slug)["auth"] = {"mode": "dcr"}
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = {p["slug"]: p for p in _load_registry(registry_path)}
    assert not is_preregistered(loaded["notion"]) and not is_preregistered(loaded["linear"])


# ── visibility ──


def test_a_launch_gated_preregistered_provider_is_visible():
    """Its card is an instruction ("an administrator must configure an OAuth app"),
    and hiding an instruction is how the operator never learns the step exists."""
    visible = {p["slug"]: p for p in get_visible_providers()}
    assert "github" in visible and visible["github"]["launch_gate_passed"] is False
    assert "asana" in visible and visible["asana"]["launch_gate_passed"] is False


def test_a_launch_gated_dcr_provider_stays_hidden():
    visible = {p["slug"] for p in get_visible_providers()}
    superhuman = get_provider("superhuman")
    assert superhuman is not None
    assert superhuman["launch_gate_passed"] is False and not is_preregistered(superhuman)
    assert "superhuman" not in visible


def test_vendor_approval_pending_hides_even_a_preregistered_provider(tmp_path, monkeypatch):
    """Vendor approval is a hard hide: a card no per-install app can satisfy is a
    dead end, not an instruction."""
    payload = get_all_registry_providers()
    figma = next(p for p in payload if p["slug"] == "figma")
    assert figma["vendor_approval_pending"] is True
    figma["auth"] = {**VALID_AUTH, "registration_guide": "oauth-app-registration/figma.md"}
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = _load_registry(registry_path)

    monkeypatch.setattr(registry_module, "_PROVIDERS", loaded)
    visible = {p["slug"] for p in get_visible_providers()}
    preregistered = {p["slug"] for p in get_preregistered_providers()}

    assert "figma" in preregistered  # the accessor for the runtime is ungated
    assert "figma" not in visible  # the gallery is not
    assert {"github", "asana"} <= visible


def test_visible_providers_are_copies():
    (github,) = [p for p in get_visible_providers() if p["slug"] == "github"]
    github["auth"]["redirect_port"] = 1
    fresh = get_provider("github")
    assert fresh is not None and fresh["auth"]["redirect_port"] == 48101


# ── the shipped registry ──


def test_shipped_registry_preregisters_github_and_asana_on_pinned_ports():
    by_slug = {p["slug"]: p for p in get_preregistered_providers()}
    assert set(by_slug) == {"github", "asana"}
    for slug, port in (("github", 48101), ("asana", 48102)):
        auth = by_slug[slug]["auth"]
        assert auth["mode"] == AUTH_MODE_PREREGISTERED
        assert auth["confidential"] is True
        assert auth["redirect_host"] == "127.0.0.1"
        assert auth["redirect_port"] == port
        assert auth["registration_guide"] == f"oauth-app-registration/{slug}.md"
        assert redirect_uri(by_slug[slug]) == f"http://127.0.0.1:{port}/callback"


def test_shipped_registry_redirect_ports_are_unique():
    ports = [p["auth"]["redirect_port"] for p in get_preregistered_providers()]
    assert len(ports) == len(set(ports))


def test_shipped_preregistered_providers_refuse_dcr_in_their_l0_baseline():
    """The block exists because the vendor refuses RFC 7591; the L0 baseline must agree."""
    for provider in get_preregistered_providers():
        assert provider["l0_expectations"]["dcr"] is False, provider["slug"]


def test_shipped_preregistered_providers_carry_the_operator_instruction():
    for provider in get_preregistered_providers():
        assert "Settings → OAuth Apps" in provider["prerequisite_copy"], provider["slug"]


def test_every_shipped_registration_guide_exists_on_disk():
    for provider in get_preregistered_providers():
        guide = REPO_ROOT / "docs" / "guides" / provider["auth"]["registration_guide"]
        assert guide.is_file(), guide


def test_no_shipped_provider_carries_a_dcr_block():
    """Absent means DCR; a bare ``{"mode": "dcr"}`` is legal but nothing ships one."""
    for provider in get_all_registry_providers():
        if "auth" in provider:
            assert provider["auth"]["mode"] == AUTH_MODE_PREREGISTERED, provider["slug"]
