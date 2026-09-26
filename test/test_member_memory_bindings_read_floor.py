"""The member-memory bindings leaf sits on the READ gate's floor, not the write tier.

A binding record under ``<crew data home>/member-memory-bindings/`` carries the RAW
session key it binds -- ``{"version": 1, "session_key": <raw>, "memory_store": ...}``
under ``sessions/<digest>/`` and ``pids/`` -- so an agent file tool that can open one
gets a usable credential rather than a digest. ``sandbox._CREW_CHILD_WITHHELD_LEAVES``
already classifies the leaf as one no child may read, and
``agent_sdk.tool_gate.adapter_hidden_credential_dirs`` enforces that classification by
projecting ``security.sensitive_home_dirs()``. Putting the leaf on the WRITE tier alone
satisfies neither reader: ``_WRITE_PROTECTED_HOME_PATHS`` is documented as "readable but
not writable", and the mask cannot deny a leaf the floor does not name.

Every assertion here is paired with a control, because the whole claim is that ONE leaf
moved:

* ``crons.json`` -- a floor leaf of the same declared class (its withheld-leaf comment
  says "a cron entry carries the session key its run executes under"). It must be
  refused and masked, or the gate is not being reached and a pass here means nothing.
* ``config.json`` -- an unrelated leaf that is write-protected and deliberately
  READABLE (the dashboard file viewer, ``cat`` and knowledge indexing all read it). It
  must stay readable through ``sensitive_path_refusal`` and stay absent from the
  credential mask. This is the control that makes the diff provably one leaf rather
  than a promotion of the whole write tier.
* the gateway's own reader -- ``subagent_persistence.read_run_execution`` opens the
  retained sidecar with ``Path.read_text`` and never consults this gate, so cold
  continuation of a retained V1 run must still resolve its app owner.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

_LEAF = "member-memory-bindings"
_RAW_SESSION_KEY = "dashboard:chat-bindings-floor-0000"
_SAME_CLASS_CONTROL = "crons.json"
_READABLE_CONTROL = "config.json"


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway data home, reached through the override the gate anchors on."""
    home = tmp_path / "crew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    return home


def _plant_binding(home: Path) -> Path:
    """A binding record in the on-disk shape a retained V1 install carries."""
    digest = hashlib.sha256(_RAW_SESSION_KEY.encode()).hexdigest()
    path = home / _LEAF / "sessions" / digest / "memory.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"version": 1, "session_key": _RAW_SESSION_KEY, "memory_store": "teammate-private"}
        ),
        encoding="utf-8",
    )
    return path


def _plant_controls(home: Path) -> tuple[Path, Path]:
    """The same-class floor leaf and the unrelated write-protected readable leaf."""
    same_class = home / _SAME_CLASS_CONTROL
    same_class.write_text(json.dumps({"jobs": []}), encoding="utf-8")
    readable = home / _READABLE_CONTROL
    readable.write_text(json.dumps({"agents": {}}), encoding="utf-8")
    return same_class, readable


def test_the_agent_read_gate_refuses_a_binding_record_and_spares_a_readable_leaf(
    crew_home: Path,
) -> None:
    """The path tier of the agent tool gate refuses the binding, not the whole tier."""
    from kiro_crew import sandbox
    from kiro_crew.security import sensitive_path_refusal, write_protected_home_paths

    binding = _plant_binding(crew_home)
    same_class, readable = _plant_controls(crew_home)

    # PRECONDITION -- the record on disk carries the raw key, so a read that lands
    # returns a credential rather than an opaque identifier.
    on_disk = json.loads(binding.read_text(encoding="utf-8"))
    assert on_disk["session_key"] == _RAW_SESSION_KEY, (
        "fixture is not the record under protection: the planted binding carries no "
        "raw session key"
    )

    # PRECONDITION -- the sandbox module classifies this leaf as one no child may
    # read, which is the classification this floor entry exists to make enforceable.
    withheld = getattr(sandbox, "_CREW_CHILD_WITHHELD_LEAVES", None)
    assert withheld is not None, (
        "sandbox._CREW_CHILD_WITHHELD_LEAVES is absent, so the tree cannot be asked "
        "what it classifies the leaf as"
    )
    assert _LEAF in withheld, (
        f"{_LEAF!r} is not classified as withheld from a sandboxed child, so this "
        "file is asserting a floor entry the sandbox module does not ask for"
    )

    # CONTROL -- a floor leaf of the same declared class is refused, so the gate is
    # reached at all and a refusal below is the leaf's own.
    assert sensitive_path_refusal(str(same_class)) is not None, (
        f"broken fixture: the same-class control {_SAME_CLASS_CONTROL} is not refused "
        "either, so this gate is not being exercised"
    )

    refusal = sensitive_path_refusal(str(binding))
    leaked = "" if refusal else json.loads(binding.read_text(encoding="utf-8"))["session_key"]
    assert refusal is not None, (
        f"the agent file-read gate permits {binding.name} under {_LEAF}/, so a read "
        f"returns the raw session key {leaked!r}; the leaf must be on "
        "_CREW_SECRET_LEAVES, because _WRITE_PROTECTED_HOME_PATHS does not cover reads"
    )

    # CONTROL, the one that bounds the change -- an unrelated write-protected leaf
    # stays READABLE. config.json is write-protected precisely because reads of it are
    # routine and intended, so a diff that promoted the write tier onto the read floor
    # would fail here while every assertion above still passed.
    assert any(entry.endswith(f"/{_READABLE_CONTROL}") for entry in write_protected_home_paths()), (
        f"broken fixture: {_READABLE_CONTROL} is not on the write-protected tier, so "
        "it cannot serve as the readable control"
    )
    assert sensitive_path_refusal(str(readable)) is None, (
        f"{_READABLE_CONTROL} is write-protected and must stay READABLE through this "
        "gate -- the dashboard file viewer, plain reads and knowledge indexing all "
        "open it. A refusal here means the write tier was promoted onto the read "
        "floor rather than one leaf being placed on it"
    )


def test_the_enforced_adapter_credential_mask_denies_the_bindings_leaf(
    crew_home: Path,
) -> None:
    """The OS mask an enforced harness is confined by is a projection of this floor."""
    from kiro_crew.security import sandbox_credential_targets

    _plant_binding(crew_home)
    _plant_controls(crew_home)

    targets = sandbox_credential_targets()

    # PRECONDITION -- the projection is anchored on the override data home, so a miss
    # below is a missing LEAF and not a missing anchor.
    assert any(str(crew_home) in target for target in targets), (
        "broken fixture: no mask target is anchored under the override data home, so "
        "leaf coverage cannot be judged"
    )

    # CONTROL -- the same-class floor leaf is projected.
    assert any(target.endswith(_SAME_CLASS_CONTROL) for target in targets), (
        f"broken fixture: the same-class control {_SAME_CLASS_CONTROL} is absent from "
        "the projection, so the projection is not being exercised"
    )

    assert any(target.endswith(_LEAF) for target in targets), (
        f"sandbox_credential_targets() does not yield {_LEAF}, so "
        "agent_sdk.tool_gate.adapter_hidden_credential_dirs projects no OS deny for "
        "it and an enforced foreign adapter's child can open the retained binding "
        "records that carry raw session keys. The leaf's presence in "
        "sandbox._CREW_CHILD_WITHHELD_LEAVES does not mask it: that list is an "
        "exclusion input, and the mask's base set is this floor"
    )

    # CONTROL -- the write-only tier is NOT projected into the mask, so this assertion
    # pins one leaf reaching the mask rather than a tier reaching it. Anchored on the
    # data home rather than matched by filename: ``.docker/config.json`` is a floor
    # leaf of its own, so a bare suffix match would find a path with no relation to
    # the crew home and the control would pass for the wrong reason.
    crew_readable = str(crew_home / _READABLE_CONTROL)
    assert crew_readable not in targets, (
        f"{crew_readable} is write-protected, not floor-listed, so the credential mask "
        "must not deny it; if it appears the write tier was folded into the floor"
    )


def test_the_write_tier_omits_the_leaf_while_writes_stay_refused() -> None:
    """One placement, and the write refusal survives it.

    ``is_sensitive_write_path`` is the union of both tiers, so moving the leaf from the
    write tier to the floor keeps writes refused. The duplicate placement is what must
    not come back: ``write_protected_home_paths()`` is published to
    ``security_posture`` as "reads allowed", which is false for this leaf.
    """
    from kiro_crew.security import (
        is_sensitive_write_path,
        sensitive_home_dirs,
        write_protected_home_paths,
    )

    floor = sensitive_home_dirs()
    write_tier = write_protected_home_paths()

    assert any(
        entry.endswith(f"/{_LEAF}") for entry in floor
    ), f"{_LEAF} is absent from the read+write floor"
    assert not any(entry.endswith(f"/{_LEAF}") for entry in write_tier), (
        f"{_LEAF} is on the write-only tier as well as the floor. That tier is "
        "published to the security-posture surface as 'reads allowed', which would "
        "tell an operator the agent may read a record carrying a raw session key"
    )
    # The sibling that belongs on the write tier stays there, so the assertion above
    # pins a removal of one entry rather than an emptied tuple.
    assert any(entry.endswith("/subagents") for entry in write_tier), (
        "the canonical run records left the write-only tier; this file asserts one "
        "leaf moved, not that the tier was rewritten"
    )

    home = Path.home()
    for entry in floor:
        if entry.endswith(f"/{_LEAF}"):
            assert is_sensitive_write_path(str(home / entry)), (
                f"writes to {entry} are permitted; the floor must keep the write "
                "refusal a write-tier placement would supply"
            )


def test_the_gateways_own_reader_still_resolves_a_retained_run(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one legitimate reader opens the sidecar directly, so the floor spares it."""
    from kiro_crew import subagent_persistence

    agent_id = "bindings-floor-run"
    # The DEFAULT store: this test is about WHICH READER opens the sidecar, and a
    # named store would need a config declaration the throwaway home has none of.
    store = ""

    subagents = crew_home / "subagents" / agent_id
    subagents.mkdir(parents=True)
    sidecar = crew_home / _LEAF / agent_id / "memory.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(
        json.dumps(
            {
                "version": 2,
                "memory_store": store,
                "memory_mode": "persistent",
                "app": "ops-mission-control",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(subagent_persistence, "_subagents_dir", lambda: crew_home / "subagents")

    # PRECONDITION -- the agent tool gate refuses this very path, so a success below
    # is the direct keystone read rather than the gate having been widened.
    from kiro_crew.security import sensitive_path_refusal

    assert sensitive_path_refusal(str(sidecar)) is not None, (
        "broken fixture: the gate does not refuse the sidecar, so this asserts "
        "nothing about a reader that bypasses the gate"
    )

    execution = subagent_persistence.read_run_execution(
        agent_id,
        state={"memory_store": store, "memory_binding_version": 2, "agent": "teammate"},
    )

    assert execution.app == "ops-mission-control", (
        "the gateway's cold-continuation read of a retained sidecar lost the run's "
        "app owner, so the floor entry reached a reader it must not reach"
    )
    assert execution.memory_mode == "persistent"


def test_the_owning_spec_states_the_leafs_read_disposition() -> None:
    """The document the router sends a reader to must not call the leaf readable.

    ``AGENTS.md`` routes anyone touching sensitive paths to
    ``docs/system-specs/modules/security.md`` FIRST, and that passage described both
    roots as sharing "the file-tool write-only gate" with results that "remain
    readable". That is the write-tier disposition, and it is the opposite of the
    leaf's: an agent file tool may not open a binding record at all. A reader
    designing against the spec would treat a raw session key as agent-readable.

    Prose is asserted because prose is what drifted, and the two guards are what
    keep the assertion honest rather than decorative. The PREMISE reads tier
    membership from the CODE, so moving the leaf back fails here on the premise
    instead of leaving a sentence that is true again by accident. The CONTROL pins
    the sibling whose disposition did not change, so the passage cannot satisfy this
    by being emptied.
    """
    from kiro_crew.security import sensitive_home_dirs, write_protected_home_paths

    # PREMISE -- this test claims something about the SPEC only while the code puts
    # the leaf on the floor and off the write tier.
    assert any(entry.endswith(f"/{_LEAF}") for entry in sensitive_home_dirs()), (
        f"{_LEAF} left the read+write floor. The spec wording pinned below describes "
        "that placement, so decide the placement before rewording the passage"
    )
    assert not any(entry.endswith(f"/{_LEAF}") for entry in write_protected_home_paths())

    spec = Path(__file__).resolve().parents[1] / "docs" / "system-specs" / "modules" / "security.md"
    body = spec.read_text(encoding="utf-8")
    start = body.index("### Member memory boundaries")
    # The next heading at ANY level, so the slice is this passage and not everything
    # up to the next same-level one -- a wider slice would let an unrelated paragraph
    # satisfy the assertions below.
    # Searched from the END of the heading's own line, because ``^`` matches at
    # offset 0 of a slice and would otherwise find the heading itself.
    body_after_heading = body.index("\n", start) + 1
    after = re.search(r"^#{1,6} ", body[body_after_heading:], re.MULTILINE)
    assert after is not None, "no heading follows the passage; the slice is unbounded"
    end = body_after_heading + after.start()
    # Collapsed to one line, so where the source happens to wrap cannot decide
    # whether a phrase matches.
    section = " ".join(body[start:end].split())

    assert _LEAF in section, "the passage stopped naming the leaf it is about"
    assert "read+write floor" in section, (
        f"the passage does not state that {_LEAF} sits on the read+write floor, so a "
        "reader routed here still learns the agent's file tools may open a record "
        "carrying a raw session key"
    )
    assert re.search(r"Both roots[^.]*write-only gate", section) is None, (
        f"the passage still puts both roots on the file-tool write-only gate. {_LEAF} "
        "is not on that tier, and that tier is what security_posture publishes as "
        "'Reads allowed'"
    )

    # CONTROL -- the canonical run records DID stay readable, and the passage must
    # still say so. Without this, deleting the paragraph would pass.
    assert "subagents" in section and "readable" in section, (
        "the passage no longer describes the canonical run records as readable; this "
        "pins one leaf moving tiers, not the passage being removed"
    )
