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

#: An engine release, optionally followed by the Node runtime a republish of that
#: same engine build bundles (``0.66.117-node22.23.4``).
_PIN_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-node\d+\.\d+\.\d+(?:-al2)?)?$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def test_the_pinned_version_is_a_release_with_an_optional_node_suffix() -> None:
    # The version is a directory name under ``<base>/kas/`` and under the data
    # home's ``engines/kas/``; any other suffix or a stray character there is a
    # path, not a release.
    assert _PIN_VERSION.match(kas_pin.KAS_PIN_VERSION), kas_pin.KAS_PIN_VERSION


@pytest.mark.parametrize(
    "version", ["0.66.117", "0.66.117-node22.23.4", "0.66.117-node22.23.4-al2"]
)
def test_the_pin_version_shape_accepts_a_release_and_a_node_republish(version: str) -> None:
    assert _PIN_VERSION.match(version)


@pytest.mark.parametrize(
    "version", ["0.66.117-rc.1", "0.66.117-node22", "v0.66.117", "0.66", "0.66.117-al2"]
)
def test_the_pin_version_shape_refuses_anything_else(version: str) -> None:
    assert not _PIN_VERSION.match(version)


def test_the_manifest_digest_is_a_lowercase_sha256() -> None:
    # ``hashlib.sha256(...).hexdigest()`` is what the installer compares against,
    # so the pin must be spelled exactly as that function spells it.
    assert _SHA256_HEX.match(kas_pin.KAS_PIN_MANIFEST_SHA256), kas_pin.KAS_PIN_MANIFEST_SHA256


def test_pinned_version_is_the_pin_unless_the_gateway_env_overrides_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kas_pin, "host_pin_version", lambda: kas_pin.KAS_PIN_VERSION)
    monkeypatch.delenv(kas_pin.KAS_VERSION_OVERRIDE_ENV, raising=False)
    assert kas_pin.pinned_version() == kas_pin.KAS_PIN_VERSION
    monkeypatch.setenv(kas_pin.KAS_VERSION_OVERRIDE_ENV, "  ")
    assert kas_pin.pinned_version() == kas_pin.KAS_PIN_VERSION
    monkeypatch.setenv(kas_pin.KAS_VERSION_OVERRIDE_ENV, " 0.70.1 ")
    assert kas_pin.pinned_version() == "0.70.1"


def test_the_engine_release_is_the_pin_without_its_node_suffix() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", kas_pin.KAS_PIN_ENGINE_RELEASE)
    assert kas_pin.KAS_PIN_VERSION.startswith(kas_pin.KAS_PIN_ENGINE_RELEASE)


def test_the_glibc_floors_cover_only_the_two_pins_and_linux_keys() -> None:
    # 2.26 is the lowest glibc any published build (the AL2 one) is linked for;
    # the normal pin keeps nodejs.org's 2.28, and arm64 has no AL2 build.
    floors = kas_pin.KAS_PIN_GLIBC_FLOORS
    assert set(floors) == {kas_pin.KAS_PIN_VERSION, kas_pin.KAS_PIN_AL2_VERSION}
    linux = {k for k in kas_pin.KAS_PIN_NODE_BUILDS if k.startswith("linux-")}
    for by_key in floors.values():
        assert set(by_key) <= linux
        assert all((2, 26) <= f <= (2, 28) for f in by_key.values()), by_key
    assert floors[kas_pin.KAS_PIN_VERSION] == {"linux-x64": (2, 28), "linux-arm64": (2, 28)}
    assert floors[kas_pin.KAS_PIN_AL2_VERSION] == {"linux-x64": (2, 26)}


def test_both_pins_name_one_engine_release_and_their_own_digest() -> None:
    # One engine release (and so one engine minimum-version check) serves both.
    assert kas_pin.KAS_PIN_AL2_VERSION == f"{kas_pin.KAS_PIN_VERSION}-al2"
    assert kas_pin.KAS_PIN_AL2_VERSION.split("-node", 1)[0] == kas_pin.KAS_PIN_ENGINE_RELEASE
    assert kas_pin.KAS_PIN_MANIFESTS == {
        kas_pin.KAS_PIN_VERSION: kas_pin.KAS_PIN_MANIFEST_SHA256,
        kas_pin.KAS_PIN_AL2_VERSION: kas_pin.KAS_PIN_AL2_MANIFEST_SHA256,
    }
    assert _SHA256_HEX.match(kas_pin.KAS_PIN_AL2_MANIFEST_SHA256)
    assert kas_pin.KAS_PIN_AL2_MANIFEST_SHA256 != kas_pin.KAS_PIN_MANIFEST_SHA256
    assert _PIN_VERSION.match(kas_pin.KAS_PIN_AL2_VERSION)


@pytest.mark.parametrize(
    "system, machine, glibc, want",
    [
        ("Linux", "x86_64", (2, 25), "al2"),
        ("Linux", "x86_64", (2, 26), "al2"),
        ("Linux", "x86_64", (2, 27), "al2"),
        ("Linux", "x86_64", (2, 28), "normal"),
        ("Linux", "x86_64", (2, 34), "normal"),
        ("Linux", "x86_64", None, "normal"),
        ("Linux", "amd64", (2, 26), "al2"),
        ("Linux", "aarch64", (2, 26), "normal"),
        ("Linux", "aarch64", (2, 34), "normal"),
        ("Darwin", "arm64", (2, 26), "normal"),
        ("Darwin", "x86_64", (2, 26), "normal"),
        ("Windows", "AMD64", (2, 26), "normal"),
    ],
)
def test_only_a_linux_x64_host_below_glibc_2_28_picks_the_al2_pin(
    monkeypatch: pytest.MonkeyPatch, system: str, machine: str, glibc, want: str
) -> None:
    from kiro_crew import constants

    monkeypatch.setattr(constants, "_host_glibc_version", lambda: glibc)
    expected = kas_pin.KAS_PIN_AL2_VERSION if want == "al2" else kas_pin.KAS_PIN_VERSION
    assert kas_pin.host_pin_version(system, machine) == expected


def test_pinned_version_follows_the_host_unless_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew import constants

    monkeypatch.delenv(kas_pin.KAS_VERSION_OVERRIDE_ENV, raising=False)
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    monkeypatch.setattr(constants, "_host_glibc_version", lambda: (2, 26))
    assert kas_pin.pinned_version() == kas_pin.KAS_PIN_AL2_VERSION
    # The same host after an upgrade to glibc 2.28+ moves back to the normal pin.
    monkeypatch.setattr(constants, "_host_glibc_version", lambda: (2, 34))
    assert kas_pin.pinned_version() == kas_pin.KAS_PIN_VERSION
    monkeypatch.setenv(kas_pin.KAS_VERSION_OVERRIDE_ENV, "0.70.1")
    assert kas_pin.pinned_version() == "0.70.1"


def test_the_public_surface_is_exactly_the_pin_and_the_engine_gate() -> None:
    assert sorted(kas_pin.__all__) == [
        "KAS_CHECKS_AUTO_APPROVALS_MIN_ENGINE_VERSION",
        "KAS_PIN_AL2_MANIFEST_SHA256",
        "KAS_PIN_AL2_VERSION",
        "KAS_PIN_ENGINE_RELEASE",
        "KAS_PIN_MANIFEST_SHA256",
        "KAS_PIN_VERSION",
        "KAS_SPEC_PERMISSIONS_MIN_ENGINE_VERSION",
        "KAS_VERSION_OVERRIDE_ENV",
        "engine_checks_auto_approvals",
        "engine_spec_permissions_supported",
        "host_pin_version",
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
