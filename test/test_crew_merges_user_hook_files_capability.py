"""Which harnesses get the user's standalone hook files folded in, as a capability.

``spec_hooks.merge_user_hook_files`` and the withheld project hook-files notice in
``dashboard/chat_turn/turn_spec_hooks`` act only where the provider's backend has
``SessionCapabilities.crew_merges_user_hook_files``. The answers are pinned per
backend here: the one the old ``backend == ACP_BACKEND_KAS`` check gave. Both
readers are driven in ``test_kas_user_hook_files.py``.
"""

from __future__ import annotations

import pytest

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKENDS_KNOWN
from kiro_crew.agent_sdk.capabilities import capabilities_for, capabilities_of


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_each_known_backend_keeps_its_answer(backend: str) -> None:
    assert capabilities_for(backend).crew_merges_user_hook_files is (backend == ACP_BACKEND_KAS)


def test_an_unknown_backend_merges_no_user_hook_files() -> None:
    caps = capabilities_for("some-harness-nobody-registered")
    assert caps.crew_merges_user_hook_files is False


def test_a_provider_with_no_capability_record_merges_no_user_hook_files() -> None:
    assert capabilities_of(object()).crew_merges_user_hook_files is False
