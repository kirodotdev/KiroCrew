"""Which harnesses get Kiro Crew's persona prompt, read as a capability.

``context_assembly.agent_spec._resolve_agent_prompt`` gives the default agent Crew's
own persona prompt, respelled for the harness, only where the backend behind the
provider label has ``SessionCapabilities.crew_sends_persona_prompt``. The answers
are pinned per backend here: the one the old ``is_claude_code`` label check gave.
The reader itself is driven in ``test_cc_prompt_branding.py``.
"""

from __future__ import annotations

import pytest

from kiro_crew.agent_sdk.backends import ACP_BACKENDS_KNOWN
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.agent_sdk.drivers.acp import provider_label_for_backend

#: The one label that got the persona before.
SENT = "claude_code"


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_each_known_backend_keeps_its_answer(backend: str) -> None:
    sends = capabilities_for(backend).crew_sends_persona_prompt
    assert sends is (provider_label_for_backend(backend) == SENT)


def test_an_unknown_backend_gets_no_persona() -> None:
    assert capabilities_for("some-harness-nobody-registered").crew_sends_persona_prompt is False
