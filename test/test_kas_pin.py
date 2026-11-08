"""The KAS engine pin: one version, one manifest digest, both well-formed.

``acp.kas_pin`` is bumped by changing BOTH constants in one commit. These cases
are what makes a half-bump (a new version with the old or a mangled digest) fail
CI instead of failing every ``kirocrew kas install`` of the release: the
installer compares the fetched ``manifest.json`` bytes against the digest before
parsing, so a digest that is not a sha256 can never match anything.
"""

from __future__ import annotations

import re

import pytest

from kiro_crew.acp import kas_pin

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def test_the_pinned_version_is_a_plain_semver() -> None:
    # The version is a directory name under ``<base>/kas/`` and under the data
    # home's ``engines/kas/``; a prerelease suffix or a stray character there is a
    # path, not a release.
    assert _SEMVER.match(kas_pin.KAS_PIN_VERSION), kas_pin.KAS_PIN_VERSION


def test_the_manifest_digest_is_a_lowercase_sha256() -> None:
    # ``hashlib.sha256(...).hexdigest()`` is what the installer compares against,
    # so the pin must be spelled exactly as that function spells it.
    assert _SHA256_HEX.match(kas_pin.KAS_PIN_MANIFEST_SHA256), kas_pin.KAS_PIN_MANIFEST_SHA256


def test_pinned_version_is_the_pin_unless_the_gateway_env_overrides_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(kas_pin.KAS_VERSION_OVERRIDE_ENV, raising=False)
    assert kas_pin.pinned_version() == kas_pin.KAS_PIN_VERSION
    monkeypatch.setenv(kas_pin.KAS_VERSION_OVERRIDE_ENV, "  ")
    assert kas_pin.pinned_version() == kas_pin.KAS_PIN_VERSION
    monkeypatch.setenv(kas_pin.KAS_VERSION_OVERRIDE_ENV, " 0.70.1 ")
    assert kas_pin.pinned_version() == "0.70.1"


def test_the_public_surface_is_exactly_the_pin_and_the_engine_gate() -> None:
    assert sorted(kas_pin.__all__) == [
        "KAS_PIN_MANIFEST_SHA256",
        "KAS_PIN_VERSION",
        "KAS_SPEC_PERMISSIONS_MIN_ENGINE_VERSION",
        "KAS_VERSION_OVERRIDE_ENV",
        "engine_spec_permissions_supported",
        "pinned_version",
    ]


class TestEngineSpecPermissionsGate:
    """The bundle route's spec ``permissions`` gate: the installed engine's version
    against the lowest engine verified to accept the field."""

    def test_the_pinned_engine_passes_the_floor(self) -> None:
        """A floor above the pin would withhold the block on every stock install,
        so a pin bump that forgets the floor, or a floor raised past the pin, fails here."""
        from kiro_crew.mcp_hot_reload import parse_kiro_cli_version

        assert kas_pin.engine_spec_permissions_supported(kas_pin.KAS_PIN_VERSION)
        pinned = parse_kiro_cli_version(kas_pin.KAS_PIN_VERSION)
        assert pinned is not None
        assert pinned >= kas_pin.KAS_SPEC_PERMISSIONS_MIN_ENGINE_VERSION

    @pytest.mark.parametrize("version", ["0.66.33", "0.66.34", "0.67.0", "1.0.0", "0.66.33-rc.1"])
    def test_at_or_above_the_floor_accepts(self, version: str) -> None:
        assert kas_pin.engine_spec_permissions_supported(version)

    @pytest.mark.parametrize("version", ["0.66.32", "0.65.99", "0.9.0"])
    def test_below_the_floor_withholds(self, version: str) -> None:
        assert not kas_pin.engine_spec_permissions_supported(version)

    @pytest.mark.parametrize("version", [None, "", "   ", "latest", "0.66", "v0.66.33", "0.66.x"])
    def test_an_unknown_version_withholds(self, version: str | None) -> None:
        """No engine, or a version the pin cannot read, is the smaller loss: the
        block is withheld rather than written onto a reader that may refuse it."""
        assert not kas_pin.engine_spec_permissions_supported(version)

    def test_a_suffix_or_padding_does_not_change_the_verdict(self) -> None:
        assert kas_pin.engine_spec_permissions_supported("0.66.33-beta.2+build.7")
        assert kas_pin.engine_spec_permissions_supported(" 0.70.1 ")
        assert not kas_pin.engine_spec_permissions_supported("0.66.32-rc.9")
