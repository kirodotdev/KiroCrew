"""The approval store's own calls keep its records bounded and current.

These tests call ``launch_approval`` directly, with no dashboard handler or
gateway start involved. A stored refusal carrying a removed field loads and
re-emits without it, an approval that lands before a deferred ``save_pass``
suppresses that pass's refusal, and ``restore`` puts a revoked approval back
unless a reapproval lands after the revoke, which it keeps. A refused stem past
the server record cap appears in no in-memory structure and no saved document,
the target filter drops its target, and a later pass adds no refusal past the
cap. The record cap on an approved stem, the name bound, the per-server launch
cap and the display bounds are pinned in ``test_mcp_launch_approval.py``.
"""

from __future__ import annotations

import json

import pytest
from mcp_launch_approval_helpers import (
    _APPROVED_CMD,
    _ATTACK_CMD,
    _approval_module,
    _target_spec,
)

from kiro_crew.mcp_gateway import rewriter

#: Opts this module out of the conftest fixture that approves every gatewayd launch.
ENFORCE_LAUNCH_APPROVAL = True


def test_removed_refusal_fields_are_not_loaded_or_emitted(tmp_path):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    store.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": {},
                "refused": {
                    "SRV": {
                        "name": "srv",
                        "reason": mod.REFUSED_CHANGED,
                        "command": [_ATTACK_CMD],
                        "approved_command": [_APPROVED_CMD],
                        "refused_at": "2020-01-02T03:04:05Z",
                        "commands": [[_ATTACK_CMD]],
                        "envs": [[]],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    mod._write(
        store, mod._document(mod.load_approvals(store), mod.load_approvals(store).stored_refused)
    )
    document = json.loads(store.read_text(encoding="utf-8"))
    refusal = mod.refused_servers(store)["srv"]

    assert not {"command", "approved_command", "refused_at"} & document["refused"]["SRV"].keys()
    assert refusal == {
        "reason": mod.REFUSED_CHANGED,
        "commands": [[_ATTACK_CMD]],
        "envs": [[]],
        "complete": True,
    }


@pytest.mark.parametrize("full_pass", [True, False])
def test_an_approval_landing_before_deferred_save_suppresses_that_refusal(tmp_path, full_pass):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    env_hash = mod.env_fingerprint({})
    fingerprint = mod.launch_fingerprint(_ATTACK_CMD, ["-c", "id"], {})
    snapshot = mod.LaunchApprovals()
    assert not snapshot.admit(
        "srv",
        fingerprint,
        launch=(_ATTACK_CMD, ["-c", "id"]),
        env={},
        derived_env_hash=env_hash,
    )
    snapshot.full_pass = full_pass
    launch = mod.ResolvedLaunch(
        fingerprint,
        _ATTACK_CMD,
        ("-c", "id"),
        frozenset({env_hash}),
    )

    mod.approve({"srv": [launch]}, path=store)
    assert not mod.save_pass(snapshot, path=store)

    assert mod.refused_servers(store) == {}
    stored = mod.load_approvals(store)
    assert stored.admits_launch("SRV", rewriter.hash_command(_ATTACK_CMD, ["-c", "id"]), env_hash)


def test_an_over_record_cap_stem_is_absent_from_every_structure(tmp_path):
    mod = _approval_module()
    assert mod is not None
    cap = getattr(mod, "_MAX_SERVER_RECORDS")
    env_hash = mod.env_fingerprint({})
    approvals = mod.LaunchApprovals()
    for index in range(cap):
        assert not approvals.admit(
            f"refused-{index:04d}",
            mod.launch_fingerprint(_ATTACK_CMD, [str(index)], {}),
            launch=(_ATTACK_CMD, [str(index)]),
            env={},
            derived_env_hash=env_hash,
        )
    assert not approvals.admit(
        "overflow",
        mod.launch_fingerprint(_ATTACK_CMD, ["overflow"], {}),
        launch=(_ATTACK_CMD, ["overflow"]),
        env={},
        derived_env_hash=env_hash,
    )
    _kept, dropped = mod.filter_target_env(
        {"KIROCREW_MCP_TARGET_OVERFLOW": _target_spec(_ATTACK_CMD, "-c", "id")}, approvals
    )
    assert dropped == ["OVERFLOW"]
    populations = (
        approvals.approved,
        approvals.approved_pairs,
        approvals.names,
        approvals.captured,
        approvals.captured_launches,
        approvals.refused,
        approvals.refused_commands,
        approvals.launch_identities,
        approvals.stored_refused,
    )
    assert all("OVERFLOW" not in population for population in populations)
    assert len(approvals.refused) == cap

    approvals.full_pass = True
    store = tmp_path / "approvals.json"
    assert mod.save_pass(approvals, path=store)
    document = json.loads(store.read_text(encoding="utf-8"))
    assert len(document["refused"]) == cap
    assert "OVERFLOW" not in document["refused"]

    later = mod.LaunchApprovals()
    assert not later.admit(
        "later",
        mod.launch_fingerprint(_ATTACK_CMD, ["later"], {}),
        launch=(_ATTACK_CMD, ["later"]),
        env={},
        derived_env_hash=env_hash,
    )
    mod.save_pass(later, path=store)
    document = json.loads(store.read_text(encoding="utf-8"))
    assert len(document["refused"]) == cap
    assert "LATER" not in document["refused"]


def test_restore_keeps_a_reapproval_that_lands_after_revoke(tmp_path):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    env_hash = mod.env_fingerprint({})
    old_launch = mod.ResolvedLaunch(
        mod.launch_fingerprint(_APPROVED_CMD, ["old"], {}),
        _APPROVED_CMD,
        ("old",),
        frozenset({env_hash}),
    )
    new_launch = mod.ResolvedLaunch(
        mod.launch_fingerprint(_APPROVED_CMD, ["new"], {}),
        _APPROVED_CMD,
        ("new",),
        frozenset({env_hash}),
    )

    mod.approve({"srv": [old_launch]}, path=store)
    snapshot = mod.revoke(["srv"], path=store)
    mod.restore(snapshot, path=store)
    restored = mod.load_approvals(store)
    assert restored.admits_command("SRV", rewriter.hash_command(_APPROVED_CMD, ["old"]))

    snapshot = mod.revoke(["srv"], path=store)
    mod.approve({"srv": [new_launch]}, path=store)
    mod.restore(snapshot, path=store)
    current = mod.load_approvals(store)
    old_hash = rewriter.hash_command(_APPROVED_CMD, ["old"])
    new_hash = rewriter.hash_command(_APPROVED_CMD, ["new"])
    new_pair = next(iter(new_launch.approval_identities))
    assert current.admits_command("SRV", new_hash)
    assert not current.admits_command("SRV", old_hash)
    assert current.approved_displays["SRV"][new_pair][0] == [_APPROVED_CMD, "new"]


@pytest.mark.parametrize("vendor", ["creds-agent", "local-creds-agent-mcp"])
def test_a_recorded_approval_never_lets_a_credential_vendor_spawn(vendor):
    """An approval recorded before the vendors joined ``UNPOOLABLE_SERVERS`` must
    not let gatewayd spawn one: its shared backend would run outside every chat
    sandbox. Both gateway-side checks refuse it -- the target filter that builds
    gatewayd's env and the per-spawn ``admits_launch`` -- while the same approval
    shape still admits an ordinary server, so the refusal is the denylist's."""
    from kiro_crew.mcp_gateway.hashing import hash_command

    mod = _approval_module()
    assert mod is not None
    env_hash = mod.env_fingerprint({})
    command_hash = hash_command(_APPROVED_CMD, [])
    approvals = mod.LaunchApprovals()
    stem = mod.target_stem(vendor)
    for approved in (stem, "SRV"):
        approvals.approved_pairs[approved] = {mod.launch_pair(command_hash, env_hash)}

    kept, dropped = mod.filter_target_env(
        {
            f"KIROCREW_MCP_TARGET_{stem}": _target_spec(_APPROVED_CMD),
            "KIROCREW_MCP_TARGET_SRV": _target_spec(_APPROVED_CMD),
        },
        approvals,
    )
    assert list(kept) == ["KIROCREW_MCP_TARGET_SRV"]
    assert dropped == [stem]
    # Not a refusal the person can approve away: nothing is waiting on them.
    assert stem not in approvals.refused

    assert approvals.admits_launch("SRV", command_hash, env_hash)
    assert not approvals.admits_launch(stem, command_hash, env_hash)
