"""Approval and sandbox posture do not partition the backend pool.

The four fields a stub reports under a "security boundary" label --
``sandbox_mode``, ``autoapprove_set_hash``, ``approval_mode`` and
``trust_all_tools`` -- are not ``PoolKey`` dimensions, because none of them
changes how a pooled backend behaves:

* ``gatewayd`` spawns backends outside any mount namespace (an accepted risk
  documented on ``backend.spawn_backend``), so two sessions configured for
  different sandbox tiers are confined identically.
* kiro-cli decides tool visibility and approval per agent, against that
  agent's own overlay entry, BEFORE a ``tools/call`` reaches the stub. The
  backend never receives these values.

This module walks the real path rather than hand-building keys: the rewriter
wraps an agent's ``mcpServers`` entry, the stub parses that entry's own flags
back, and ``PoolKey.from_register`` reads the payload the stub would send. So
what is asserted is what a live install does.

The pin that matters for a reviewer is the pair: the pool shares across
approval posture (``test_approval_posture_never_splits_the_pool``) AND each
agent keeps its own approval surface (``test_each_agent_keeps_its_own_*``).
Sharing a process is only safe because the second one holds.

``agent_name`` is itself a dimension, so the sharing assertions hold it equal
and run once per agent. What they isolate is the four fields, not agent
identity: two differently-named agents get two backends whatever these four
say.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.mcp_gateway import rewriter, stub
from kiro_crew.mcp_gateway.pool import PoolKey

_STUB_PYTHON_PREFIX = ["-s", "-m", rewriter._STUB_MODULE]

#: The two postures. Permissive and strict on every one of the four fields at
#: once, which is the shape a real pair of agents differs in.
PERMISSIVE: dict[str, Any] = {
    "auto_approve": ["execute_bash", "fs_write", "fs_read"],
    "approval_mode": "auto",
    "sandbox_mode": "off",
    "trust_all": True,
}
STRICT: dict[str, Any] = {
    "auto_approve": [],
    "approval_mode": "interactive",
    "sandbox_mode": "standard",
    "trust_all": False,
}


def _stub_flags_argv(entry: dict[str, Any]) -> list[str]:
    args = entry["args"]
    if args[:3] == _STUB_PYTHON_PREFIX:
        return args[3:]
    assert args[:2] == ["-m", rewriter._STUB_MODULE]
    return args[2:]


def _wrapped_entry(
    tmp_path: Path,
    *,
    agent_name: str,
    posture: dict[str, Any],
    work_dir: Path | None = None,
) -> dict[str, Any]:
    """An agent's ``mcpServers['demo-mcp']`` entry as the rewriter emits it.

    Everything outside *posture* is held equal across calls, so the only
    variable the assertions can be reading is the approval/sandbox posture.
    """
    original = {
        "command": sys.executable,
        "args": ["-c", "pass"],
        "autoApprove": list(posture["auto_approve"]),
    }
    return rewriter._build_stub_entry(
        stubs_dir=tmp_path / "stubs",
        server_name="demo-mcp",
        agent_name=agent_name,
        original=original,
        env_pairs={},
        target_command=sys.executable,
        socket_path=tmp_path / "gateway.sock",
        work_dir=work_dir or tmp_path,
        sandbox_mode=posture["sandbox_mode"],
        approval_mode=posture["approval_mode"],
    )


def _register_payload(entry: dict[str, Any], *, trust_all: bool) -> dict[str, Any]:
    """The Register frame the stub launched from *entry* would send.

    ``--trust-all`` has no rewriter-side input, so it is applied to the parsed
    namespace directly -- the same value the flag would set.
    """
    parsed = stub._parse_args(_stub_flags_argv(entry))
    parsed.trust_all = trust_all
    return stub.build_register_payload(parsed)


def _key(tmp_path: Path, *, agent_name: str, posture: dict[str, Any], **kw: Any) -> PoolKey:
    entry = _wrapped_entry(tmp_path, agent_name=agent_name, posture=posture, **kw)
    return PoolKey.from_register(_register_payload(entry, trust_all=posture["trust_all"]))


@pytest.mark.parametrize("agent_name", ["agent-a", "agent-b"])
def test_approval_posture_never_splits_the_pool(tmp_path, agent_name) -> None:
    """One server, one agent, two opposite postures: one backend.

    Asserted per agent so the result does not depend on whether
    ``agent_name`` is itself a dimension -- the claim is about the four
    approval/sandbox fields alone.
    """
    permissive = _key(tmp_path, agent_name=agent_name, posture=PERMISSIVE)
    strict = _key(tmp_path, agent_name=agent_name, posture=STRICT)
    assert permissive.stable_hash() == strict.stable_hash()
    assert permissive == strict


def test_two_agents_still_get_two_backends(tmp_path) -> None:
    """The limit of what this change buys, pinned so nobody reads the test
    above as "two agents now share a process".

    ``agent_name`` is a dimension, so one server reached by two named agents
    is still two processes even with identical posture. Taking the four fields
    out of the key is what makes posture stop mattering; whether agent
    identity should partition the pool is a separate question.
    """
    a = _key(tmp_path, agent_name="agent-a", posture=STRICT)
    b = _key(tmp_path, agent_name="agent-b", posture=STRICT)
    assert a.stable_hash() != b.stable_hash()


def test_the_payloads_really_did_differ(tmp_path) -> None:
    """Control for the test above: the two postures produce genuinely
    different Register frames, so the shared key is the pool ignoring them and
    not the fixture accidentally building one payload twice."""
    loose = _register_payload(
        _wrapped_entry(tmp_path, agent_name="agent-a", posture=PERMISSIVE),
        trust_all=True,
    )
    tight = _register_payload(
        _wrapped_entry(tmp_path, agent_name="agent-a", posture=STRICT),
        trust_all=False,
    )
    assert loose["autoapprove_set_hash"] != tight["autoapprove_set_hash"]
    assert loose["approval_mode"] == "auto" and tight["approval_mode"] == "interactive"
    assert loose["sandbox_mode"] == "off" and tight["sandbox_mode"] == "standard"
    assert loose["trust_all_tools"] is True and tight["trust_all_tools"] is False


def test_each_agent_keeps_its_own_autoapprove_list(tmp_path) -> None:
    """Where the per-agent approval decision actually lives.

    kiro-cli reads ``autoApprove`` off the wrapped entry in the agent's own
    overlay and gates the call there, before the stub sees it. The rewriter
    preserves the list per entry, so two agents sharing a backend still get
    their own approval surface.
    """
    loose = _wrapped_entry(tmp_path, agent_name="agent-a", posture=PERMISSIVE)
    tight = _wrapped_entry(tmp_path, agent_name="agent-b", posture=STRICT)
    assert loose["autoApprove"] == ["execute_bash", "fs_write", "fs_read"]
    assert tight.get("autoApprove", []) == []


def test_each_agent_keeps_its_own_posture_on_its_stub_flags(tmp_path) -> None:
    """The posture also stays per agent on the stub's own argv, so a backend
    that one day needs to be told the posture can be told it per call rather
    than by owning a process."""
    loose = _register_payload(
        _wrapped_entry(tmp_path, agent_name="agent-a", posture=PERMISSIVE),
        trust_all=True,
    )
    tight = _register_payload(
        _wrapped_entry(tmp_path, agent_name="agent-b", posture=STRICT),
        trust_all=False,
    )
    assert loose["agent_name"] == "agent-a" and tight["agent_name"] == "agent-b"
    assert loose["approval_mode"] != tight["approval_mode"]
    assert loose["autoapprove_set_hash"] != tight["autoapprove_set_hash"]


def test_a_real_dimension_still_splits(tmp_path) -> None:
    """Negative control: the key is not permissive. A dimension that DOES
    change backend behaviour still gets its own backend, so the equality above
    is about the four fields and not about a comparison that always passes."""
    here = _key(tmp_path, agent_name="agent-a", posture=STRICT)
    elsewhere_dir = tmp_path / "other-work-dir"
    elsewhere_dir.mkdir()
    elsewhere = _key(tmp_path, agent_name="agent-a", posture=STRICT, work_dir=elsewhere_dir)
    assert here.stable_hash() != elsewhere.stable_hash()
