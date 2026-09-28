"""Folder steering never reaches the harness layer (design Property 3).

Folder steering is delivered by ONE seam -- the Context_Builder -- because that is
the only layer every provider passes through. The harness layer is enumerated per
host, so a folder-steering field carried there is a silent hole per provider that
does not read it: the kiro harness declared the documents as native launch
documents (a dedup RECORD, not a transmission channel) while Codex, KAS and every
config-authored host ignored the field entirely.

These assertions are structural rather than generated on purpose. The property
worth pinning is that there is no folder-steering INPUT to vary: with no field on
``SpawnContext`` and no parameter on ``AcpRuntime.__init__``, every harness
necessarily produces a byte-identical ``SpawnPlan`` for a folder chat and a
non-folder chat, for every host at once and for every host added later. A
behavioural test could only ever cover the hosts it enumerated.

Validates: Requirements 4.1, 4.2, 4.3, 4.4, 5.2.
"""

from __future__ import annotations

import dataclasses
import inspect

from kiro_crew.acp.harness import KiroHarness, SpawnContext
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.member_essential_context import kiro_launch_documents


def test_spawn_context_carries_no_folder_steering_field() -> None:
    """Req 4.2 -- the spawn contract has no folder-steering input to differ on."""
    names = {f.name for f in dataclasses.fields(SpawnContext)}
    assert "steering_dirs" not in names
    assert not [n for n in names if "steering" in n]


def test_acp_runtime_constructor_accepts_no_folder_steering() -> None:
    """Req 4.2 -- nothing can hand a runtime folder steering to carry onto a spawn."""
    params = inspect.signature(AcpRuntime.__init__).parameters
    assert "steering_dirs" not in params
    assert not [n for n in params if "steering" in n]


def test_kiro_resolve_spawn_mentions_no_steering() -> None:
    """Req 4.1, 4.3 -- the kiro harness neither computes nor declares folder steering.

    Asserted on the source text because the absence must hold for the whole
    function, including a branch a behavioural call would not enter.
    """
    source = inspect.getsource(KiroHarness.resolve_spawn)
    assert "steering" not in source.lower()


def test_kiro_launch_documents_takes_no_extra_steering_dirs() -> None:
    """Req 4.4 -- folder documents are never declared host-native.

    The native-launch declaration is what the member envelope strips bodies
    against, so a folder document listed here would arrive body-less.
    """
    params = inspect.signature(kiro_launch_documents).parameters
    assert "extra_steering_dirs" not in params
    assert not [n for n in params if "steering" in n]


def test_chat_slot_does_not_persist_steering_dirs() -> None:
    """Req 5.2 -- no slot-side cache to go stale when a folder is edited."""
    from kiro_crew.dashboard.state import _ChatSlot

    slots = set(_ChatSlot.__slots__)
    assert "steering_dirs" not in slots
    assert not [n for n in slots if "steering" in n]
