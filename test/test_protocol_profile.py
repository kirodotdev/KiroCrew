"""Unit tests for the ACP ProtocolProfile seam.

These pin the two profile constants byte-for-byte, the backend→profile mapping,
and that each bundled adapter reports the profile the wave-2 design assigns it.
The profile is a pure seam over the former ``_is_claude`` wire branches, so these
values must equal the wire constants (``PROTOCOL_VERSION`` / ``PROTOCOL_VERSION_CLAUDE``)
the client used before the extraction.
"""

from __future__ import annotations

from kiro_crew.acp.harness_adapters import (
    ClaudeAdapter,
    HarnessAdapter,
    KasAdapter,
    KiroAdapter,
)
from kiro_crew.acp.protocol_profile import (
    KAS_PROFILE,
    KIRO_PROFILE,
    STANDARD_ACP_PROFILE,
    ProtocolProfile,
    profile_for_backend,
)
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS, ACP_BACKEND_KIRO


class TestProfileConstants:
    def test_kiro_profile_exact_fields(self):
        assert KIRO_PROFILE.protocol_version == "2025-08-22"
        assert KIRO_PROFILE.permission_option_style == "kiro"
        assert KIRO_PROFILE.supports_set_config_option is False
        assert KIRO_PROFILE.emits_thought_chunks is False

    def test_standard_profile_exact_fields(self):
        assert STANDARD_ACP_PROFILE.protocol_version == 1
        assert STANDARD_ACP_PROFILE.permission_option_style == "standard"
        assert STANDARD_ACP_PROFILE.supports_set_config_option is True
        assert STANDARD_ACP_PROFILE.emits_thought_chunks is True

    def test_kas_profile_exact_fields(self):
        # KAS is kiro-cli's dialect EXCEPT the initialize protocolVersion, which
        # the relay expects as the public-ACP integer 1.
        assert KAS_PROFILE.protocol_version == 1
        assert isinstance(KAS_PROFILE.protocol_version, int)
        assert not isinstance(KAS_PROFILE.protocol_version, bool)
        assert KAS_PROFILE.permission_option_style == "kiro"
        assert KAS_PROFILE.supports_set_config_option is False
        assert KAS_PROFILE.emits_thought_chunks is False

    def test_kas_profile_protocol_version_pins_the_runtime_wire(self):
        # Drift pin: import runtime's OWN constant so the profile and the wire
        # cannot silently diverge. If the KAS handshake integer ever changes in
        # runtime.py, this fails until KAS_PROFILE is updated to match.
        from kiro_crew.acp.runtime import PROTOCOL_VERSION_KAS

        assert KAS_PROFILE.protocol_version == PROTOCOL_VERSION_KAS
        assert profile_for_backend(ACP_BACKEND_KAS).protocol_version == PROTOCOL_VERSION_KAS

    def test_kiro_protocol_version_is_a_str(self):
        # The kiro wire is a date STRING, not the integer the public wire uses;
        # a regression to int would silently change the initialize handshake.
        assert isinstance(KIRO_PROFILE.protocol_version, str)

    def test_standard_protocol_version_is_an_int(self):
        assert isinstance(STANDARD_ACP_PROFILE.protocol_version, int)
        assert not isinstance(STANDARD_ACP_PROFILE.protocol_version, bool)

    def test_profile_is_frozen(self):
        import dataclasses

        try:
            KIRO_PROFILE.supports_set_config_option = True  # type: ignore[misc]
        except dataclasses.FrozenInstanceError:
            pass
        else:  # pragma: no cover - only runs on a regression
            raise AssertionError("ProtocolProfile must be frozen")

    def test_matches_client_wire_constants(self):
        # The seam must be byte-identical to the constants the _is_claude
        # branches used, or the extraction changed the wire.
        from kiro_crew.acp.client import PROTOCOL_VERSION, PROTOCOL_VERSION_CLAUDE

        assert KIRO_PROFILE.protocol_version == PROTOCOL_VERSION
        assert STANDARD_ACP_PROFILE.protocol_version == PROTOCOL_VERSION_CLAUDE


class TestProfileForBackend:
    def test_kiro_backend_maps_to_kiro_profile(self):
        assert profile_for_backend(ACP_BACKEND_KIRO) is KIRO_PROFILE

    def test_empty_string_maps_to_kiro_profile(self):
        # ACP_BACKEND_KIRO IS the empty string; assert the literal too so a
        # future change to the constant cannot silently drop the empty case.
        assert profile_for_backend("") is KIRO_PROFILE

    def test_kas_backend_maps_to_kas_profile(self):
        # KAS is the kiro-cli relay but with the public-ACP integer
        # protocolVersion, so it has its own profile (not KIRO_PROFILE).
        assert profile_for_backend(ACP_BACKEND_KAS) is KAS_PROFILE

    def test_claude_backend_maps_to_standard_profile(self):
        assert profile_for_backend(ACP_BACKEND_CLAUDE) is STANDARD_ACP_PROFILE

    def test_unknown_backend_falls_back_to_kiro_profile(self):
        # Fail-safe: an unrecognized backend gets kiro-cli's wire, the dialect
        # AcpClient's default path already speaks.
        assert profile_for_backend("something-else") is KIRO_PROFILE

    def test_returns_a_protocol_profile(self):
        assert isinstance(profile_for_backend(ACP_BACKEND_CLAUDE), ProtocolProfile)


class TestAdapterProfiles:
    def test_kiro_adapter_reports_kiro_profile(self):
        assert KiroAdapter().protocol_profile is KIRO_PROFILE

    def test_kas_adapter_reports_kas_profile(self):
        assert KasAdapter().protocol_profile is KAS_PROFILE

    def test_claude_adapter_reports_standard_profile(self):
        assert ClaudeAdapter().protocol_profile is STANDARD_ACP_PROFILE

    def test_generic_adapter_reports_standard_profile(self):
        # The generic adapter pins its profile explicitly (identical to the base)
        # so every operator harness / bundled Codex has the wire nailed as data.
        from kiro_crew.acp.harness_adapters import GenericAdapter

        assert GenericAdapter().protocol_profile is STANDARD_ACP_PROFILE

    def test_base_adapter_reports_standard_profile(self):
        # The generic adapter (every operator descriptor, bundled Codex) speaks
        # the public ACP wire.
        assert HarnessAdapter().protocol_profile is STANDARD_ACP_PROFILE
