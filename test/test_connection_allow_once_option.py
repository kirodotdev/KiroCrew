"""The allow-always downgrade answers with an option id the harness offered.

A Crew-owned Connection call is answered once only (``once_for_always``). KAS asks
such a call through an explicit ``ask`` rule, and an explicit ask offers no
allow-always option, so the frame records its one allow id (``accept``) in both
slots. Answering with an id the frame never offered reads to KAS as a refusal
("The user rejected this tool call."), so the call never runs.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.client_permissions import once_for_always
from kiro_crew.acp.types import OPTION_ALLOW_ALWAYS, OPTION_ALLOW_ONCE

# KAS's explicit ask: Allow, Deny, Always deny. No "always-accept".
_KAS_EXPLICIT = {"once": "accept", "always": "accept", "reject": "reject"}
# KAS's implicit ask offers both allow options.
_KAS_IMPLICIT = {"once": "accept", "always": "always-accept", "reject": "reject"}


def test_explicit_ask_is_answered_with_the_offered_allow_id() -> None:
    assert once_for_always("accept", _KAS_EXPLICIT) == "accept"


def test_canonical_always_on_an_explicit_ask_maps_to_the_offered_allow_id() -> None:
    assert once_for_always(OPTION_ALLOW_ALWAYS, _KAS_EXPLICIT) == "accept"


@pytest.mark.parametrize("resolved", ["always-accept", OPTION_ALLOW_ALWAYS])
def test_allow_always_is_downgraded_to_the_offered_once_id(resolved: str) -> None:
    assert once_for_always(resolved, _KAS_IMPLICIT) == "accept"


def test_a_once_id_passes_through_unchanged() -> None:
    assert once_for_always("accept", _KAS_IMPLICIT) == "accept"


def test_no_recorded_once_id_falls_back_to_the_canonical_one() -> None:
    assert once_for_always(OPTION_ALLOW_ALWAYS, {}) == OPTION_ALLOW_ONCE
