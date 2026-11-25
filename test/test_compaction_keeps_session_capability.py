"""Which harnesses keep their native session across ``/compact``, read as a capability.

``CompactionCoordinator._compact_session`` compacts in place, leaving the durable
session mapping untouched, only where the provider's backend has
``SessionCapabilities.keeps_session_across_compaction``. The answers are pinned
per backend here: the one the old ``is_claude_backend`` provider check gave. The
arm itself is driven in ``test_session_compaction.py``.
"""

from __future__ import annotations

import pytest

from kiro_crew.agent_sdk.backends import ACP_BACKENDS_KNOWN
from kiro_crew.agent_sdk.capabilities import capabilities_for, capabilities_of
from kiro_crew.providers.acp import AcpProvider, is_claude_backend


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_each_known_backend_keeps_its_answer(backend: str) -> None:
    provider = AcpProvider(acp_backend=backend)
    keeps = capabilities_of(provider).keeps_session_across_compaction
    assert keeps is is_claude_backend(provider)


def test_an_unknown_backend_does_not_keep_its_session() -> None:
    caps = capabilities_for("some-harness-nobody-registered")
    assert caps.keeps_session_across_compaction is False


def test_a_provider_with_no_capability_record_does_not_keep_its_session() -> None:
    assert capabilities_of(object()).keeps_session_across_compaction is False
