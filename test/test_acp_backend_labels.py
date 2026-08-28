"""Provider labels and process-lifecycle recognition per backend.

The label indexes three things that must agree: resume compatibility, session-map
persistence, and session-file cleanup routing. A backend persisted under the
wrong label has its session id pruned for want of a transcript that was never
going to be there.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiro_crew import session_pid
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_KNOWN,
    PROVIDER_LABEL_CLAUDE,
    PROVIDER_LABEL_CODEX,
    PROVIDER_LABEL_DEFAULT,
    PROVIDER_LABEL_GOOSE,
    PROVIDER_LABEL_KAS,
    PROVIDER_LABEL_OPENCODE,
    PROVIDER_LABEL_PI,
)
from kiro_crew.providers.acp import provider_label


def _session_provider(backend: str) -> MagicMock:
    provider = MagicMock(spec=AcpSessionProvider)
    provider.backend = backend
    return provider


class TestProviderLabel:
    @pytest.mark.parametrize(
        ("backend", "expected"),
        [
            (ACP_BACKEND_KIRO, PROVIDER_LABEL_DEFAULT),
            (ACP_BACKEND_CLAUDE, PROVIDER_LABEL_CLAUDE),
            (ACP_BACKEND_KAS, PROVIDER_LABEL_KAS),
            (ACP_BACKEND_CODEX, PROVIDER_LABEL_CODEX),
            (ACP_BACKEND_GOOSE, PROVIDER_LABEL_GOOSE),
            (ACP_BACKEND_OPENCODE, PROVIDER_LABEL_OPENCODE),
            (ACP_BACKEND_PI, PROVIDER_LABEL_PI),
        ],
    )
    def test_every_backend_maps_to_its_own_label(self, backend: str, expected: str) -> None:
        assert provider_label(_session_provider(backend)) == expected

    def test_no_two_backends_share_a_label(self) -> None:
        """A shared label would merge two backends' resume compatibility."""
        labels = [provider_label(_session_provider(b)) for b in sorted(ACP_BACKENDS_KNOWN)]
        assert len(labels) == len(set(labels))

    def test_every_known_backend_has_a_non_default_label_unless_it_is_kiro(self) -> None:
        """A new backend falling through to the kiro label is the failure mode.

        session_map skips transcript validation for any non-default label, so a
        backend mislabelled as kiro has its session id pruned when the kiro
        transcript it never wrote is found missing.
        """
        for backend in sorted(ACP_BACKENDS_KNOWN):
            label = provider_label(_session_provider(backend))
            if backend == ACP_BACKEND_KIRO:
                assert label == PROVIDER_LABEL_DEFAULT
            else:
                assert label != PROVIDER_LABEL_DEFAULT, backend

    def test_an_unrelated_object_falls_back_to_the_default_label(self) -> None:
        assert provider_label(object()) == PROVIDER_LABEL_DEFAULT

    def test_a_non_string_backend_attribute_is_not_a_registry_id(self) -> None:
        """``MagicMock.backend`` is another MagicMock, which is truthy.

        Treating that as a registry id minted ``acp:<MagicMock ...>``, which
        ``detect_provider_switch`` read as a harness change and cleared the
        stored resume sid. Only a real string id is a backend.
        """
        from kiro_crew.providers.acp import AcpProvider

        provider = MagicMock(spec=AcpProvider)
        # Leave provider.backend as the MagicMock default (not a str).
        assert provider_label(provider) == PROVIDER_LABEL_DEFAULT

    def test_an_unknown_string_backend_gets_its_own_acp_label(self) -> None:
        assert provider_label(_session_provider("byo-adapter")) == "acp:byo-adapter"


class TestManagedAgentMarkers:
    def test_every_backend_process_marker_is_recognised(self) -> None:
        """A marker absent here means a leaked process, not a wrong kill."""
        from kiro_crew.acp.backends import process_markers

        for marker in process_markers():
            assert marker in session_pid._MANAGED_AGENT_MARKERS, marker

    def test_codex_is_recognised(self) -> None:
        assert "codex" in session_pid._MANAGED_AGENT_MARKERS

    def test_the_original_markers_are_retained(self) -> None:
        assert "kiro-cli" in session_pid._MANAGED_AGENT_MARKERS
        assert "claude" in session_pid._MANAGED_AGENT_MARKERS
        assert "opencode" in session_pid._MANAGED_AGENT_MARKERS
        assert "pi-acp" in session_pid._MANAGED_AGENT_MARKERS
        assert "pi" in session_pid._MANAGED_AGENT_MARKERS
