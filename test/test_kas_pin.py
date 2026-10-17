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


def test_the_public_surface_is_exactly_the_four_names() -> None:
    assert sorted(kas_pin.__all__) == [
        "KAS_PIN_MANIFEST_SHA256",
        "KAS_PIN_VERSION",
        "KAS_VERSION_OVERRIDE_ENV",
        "pinned_version",
    ]
