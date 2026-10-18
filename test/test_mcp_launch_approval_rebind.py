"""A changed ``${VAR}`` value rebinds an approved launch rather than refusing it.

An approval holds the pair of the command hash and the expanded env's
fingerprint, so rotating the value behind an approved placeholder changes the
pair. A gateway-start pass over such a rotation rebinds the pair to the new
expansion and retires the old one, keeps a readable display of the declared
text, audits the rebind as an approved launch rather than a managed one, and
persists no rebind when the env sidecar does not commit. A changed declared env
text or command is still refused, repeated rotations and a second agent
declaring the same launch never fill the per-server launch cap, a rebind neither
restores an approval revoked nor overwrites one made during the pass, and a pass
that keeps an agent's overlay because its spec cannot be read keeps that
agent's approved pair.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp_launch_approval_helpers import (
    _APPROVED_CMD,
    _ATTACK_CMD,
    _approval_module,
    _rewrite,
    _targets_running,
    _write_agent,
)

from kiro_crew import agent_discovery
from kiro_crew.mcp_gateway import rewriter

#: Opts this module out of the conftest fixture that approves every gatewayd launch.
ENFORCE_LAUNCH_APPROVAL = True


def _approve_as_the_toggle_does(tmp_path: Path, mod, store: Path) -> None:
    """Resolve what ``srv`` runs now and approve exactly that, as the MCP page does."""
    probe = mod.LaunchApprovals(probe=True)
    _rewrite(tmp_path, frozenset({"srv"}), probe)
    launches = list(probe.captured_launches.get("SRV", {}).values())
    assert launches, "the probe pass resolved no launch for srv"
    mod.approve({"srv": launches}, path=store)


def _rewrite_and_persist(tmp_path: Path, mod, store: Path):
    """One gateway-start pass against the stored approvals, persisted after."""
    approvals = mod.load_approvals(store)
    target_env = _rewrite(tmp_path, frozenset({"srv"}), approvals)
    mod.save_pass(approvals, path=store)
    return approvals, target_env


def _placeholder_agent(tmp_path: Path, value_ref: str = "${KC_TEST_TOKEN}") -> None:
    _write_agent(
        tmp_path / "agents",
        {"srv": {"command": _APPROVED_CMD, "args": ["-V"], "env": {"TOKEN": value_ref}}},
    )


def test_a_changed_placeholder_value_keeps_the_approval(tmp_path, monkeypatch):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "first")
    _placeholder_agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)

    monkeypatch.setenv("KC_TEST_TOKEN", "rotated")
    approvals, target_env = _rewrite_and_persist(tmp_path, mod, store)

    assert approvals.refused == {}
    assert _targets_running(target_env, _APPROVED_CMD)
    stored = mod.load_approvals(store)
    command_hash = rewriter.hash_command(_APPROVED_CMD, ["-V"])
    rotated = mod.env_fingerprint({"TOKEN": "rotated"})
    first = mod.env_fingerprint({"TOKEN": "first"})
    # gatewayd checks the pair the store holds, so the new expansion must be there
    # and the one it replaced must not linger.
    assert stored.admits_launch("SRV", command_hash, rotated)
    assert not stored.admits_launch("SRV", command_hash, first)
    assert stored.stored_refused == {}
    # The "Approved before" view keeps a readable display of the live pair.
    displays = stored.approved_displays.get("SRV", {})
    assert mod.launch_pair(command_hash, rotated) in displays
    assert "TOKEN=${KC_TEST_TOKEN}" in displays[mod.launch_pair(command_hash, rotated)][1]


def test_a_failed_sidecar_commit_does_not_persist_a_rebind(tmp_path, monkeypatch):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "first")
    _placeholder_agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)
    command_hash = rewriter.hash_command(_APPROVED_CMD, ["-V"])
    first = mod.env_fingerprint({"TOKEN": "first"})
    rotated = mod.env_fingerprint({"TOKEN": "rotated"})

    monkeypatch.setenv("KC_TEST_TOKEN", "rotated")
    real_commit = rewriter._SidecarLedger.commit
    monkeypatch.setattr(rewriter._SidecarLedger, "commit", lambda self: False)
    approvals = mod.load_approvals(store)
    _rewrite(tmp_path, frozenset({"srv"}), approvals)
    rebind_incomplete = getattr(approvals, "rebind_incomplete", None)
    assert rebind_incomplete is True, (
        "LaunchApprovals exposes no rebind_incomplete set by a pass whose "
        "environment sidecar did not publish"
    )
    mod.save_pass(approvals, path=store)

    stored = mod.load_approvals(store)
    assert stored.admits_launch("SRV", command_hash, first)
    assert not stored.admits_launch("SRV", command_hash, rotated)

    monkeypatch.setattr(rewriter._SidecarLedger, "commit", real_commit)
    approvals, target_env = _rewrite_and_persist(tmp_path, mod, store)
    assert _targets_running(target_env, _APPROVED_CMD)
    stored = mod.load_approvals(store)
    assert stored.admits_launch("SRV", command_hash, rotated)
    assert not stored.admits_launch("SRV", command_hash, first)


def test_a_rebind_is_audited_as_an_approved_launch_not_a_managed_one(tmp_path, monkeypatch, caplog):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "first")
    _placeholder_agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)

    monkeypatch.setenv("KC_TEST_TOKEN", "rotated")
    caplog.clear()
    with caplog.at_level("WARNING"):
        _rewrite_and_persist(tmp_path, mod, store)

    audit = [r.getMessage() for r in caplog.records if "recorded" in r.getMessage()]
    assert audit, "the save wrote the rebound pair without an audit line"
    # The operator approved this declaration; calling it managed states the
    # opposite provenance of what was admitted.
    assert not any("managed launch" in line for line in audit), audit
    assert any("${VAR} value changed" in line for line in audit), audit


def test_a_changed_declared_env_text_is_still_refused(tmp_path, monkeypatch):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "first")
    monkeypatch.setenv("KC_OTHER", "first")
    _placeholder_agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)

    # Same expansion, different declared text: the agent-writable part moved.
    _placeholder_agent(tmp_path, "${KC_OTHER}")
    approvals, target_env = _rewrite_and_persist(tmp_path, mod, store)

    assert approvals.refused == {"SRV": mod.REFUSED_CHANGED}
    assert not _targets_running(target_env, _APPROVED_CMD)


def test_a_changed_command_with_a_placeholder_is_still_refused(tmp_path, monkeypatch):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "first")
    _placeholder_agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)

    monkeypatch.setenv("KC_TEST_TOKEN", "rotated")
    _write_agent(
        tmp_path / "agents",
        {
            "srv": {
                "command": _ATTACK_CMD,
                "args": ["-c", "id"],
                "env": {"TOKEN": "${KC_TEST_TOKEN}"},
            }
        },
    )
    approvals, target_env = _rewrite_and_persist(tmp_path, mod, store)

    assert approvals.refused == {"SRV": mod.REFUSED_CHANGED}
    assert _targets_running(target_env, _ATTACK_CMD) == []


def test_repeated_placeholder_changes_never_reach_the_launch_cap(tmp_path, monkeypatch):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "v0")
    _placeholder_agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)

    for i in range(1, getattr(mod, "_MAX_SERVER_LAUNCHES") + 5):
        monkeypatch.setenv("KC_TEST_TOKEN", f"v{i}")
        approvals, _target_env = _rewrite_and_persist(tmp_path, mod, store)
        assert approvals.refused == {}, f"refused after {i} changes"

    assert len(mod.load_approvals(store).approved_pairs["SRV"]) == 1


def test_a_rebind_does_not_restore_an_approval_revoked_during_the_pass(tmp_path, monkeypatch):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "first")
    _placeholder_agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)

    monkeypatch.setenv("KC_TEST_TOKEN", "rotated")
    approvals = mod.load_approvals(store)
    _rewrite(tmp_path, frozenset({"srv"}), approvals)
    # The operator turns the stub off before the pass is persisted.
    mod.revoke(["srv"], path=store)
    mod.save_pass(approvals, path=store)

    stored = mod.load_approvals(store)
    command_hash = rewriter.hash_command(_APPROVED_CMD, ["-V"])
    assert not stored.approved.get("SRV")
    assert not stored.admits_launch("SRV", command_hash, mod.env_fingerprint({"TOKEN": "rotated"}))


def test_a_rebind_does_not_overwrite_an_approval_made_during_the_pass(tmp_path, monkeypatch):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "v0")
    _placeholder_agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)

    monkeypatch.setenv("KC_TEST_TOKEN", "v1")
    approvals = mod.load_approvals(store)
    _rewrite(tmp_path, frozenset({"srv"}), approvals)
    # The value moves again and the operator approves it before the pass is saved.
    monkeypatch.setenv("KC_TEST_TOKEN", "v2")
    _approve_as_the_toggle_does(tmp_path, mod, store)
    mod.save_pass(approvals, path=store)

    stored = mod.load_approvals(store)
    command_hash = rewriter.hash_command(_APPROVED_CMD, ["-V"])
    assert stored.admits_launch("SRV", command_hash, mod.env_fingerprint({"TOKEN": "v2"}))
    assert not stored.admits_launch("SRV", command_hash, mod.env_fingerprint({"TOKEN": "v1"}))
    assert not stored.admits_launch("SRV", command_hash, mod.env_fingerprint({"TOKEN": "v0"}))


def test_two_declarations_of_one_launch_never_reach_the_launch_cap(tmp_path, monkeypatch):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "v0")
    _placeholder_agent(tmp_path)
    # A second agent declares the same command and args with other env text.
    (tmp_path / "agents" / "b.json").write_text(
        json.dumps(
            {
                "name": "b",
                "mcpServers": {
                    "srv": {
                        "command": _APPROVED_CMD,
                        "args": ["-V"],
                        "env": {"TOKEN": "${KC_TEST_TOKEN}", "LEVEL": "debug"},
                    }
                },
            }
        )
    )
    _approve_as_the_toggle_does(tmp_path, mod, store)

    for i in range(1, getattr(mod, "_MAX_SERVER_LAUNCHES") + 5):
        monkeypatch.setenv("KC_TEST_TOKEN", f"v{i}")
        approvals, _target_env = _rewrite_and_persist(tmp_path, mod, store)
        assert approvals.refused == {}, f"refused after {i} changes"

    assert len(mod.load_approvals(store).approved_pairs["SRV"]) == 2


def _second_agent_declaring_the_same_launch(tmp_path: Path) -> None:
    """Agent ``b``: same command and args as ``a``, different declared env text."""
    (tmp_path / "agents" / "b.json").write_text(
        json.dumps(
            {
                "name": "b",
                "mcpServers": {
                    "srv": {
                        "command": _APPROVED_CMD,
                        "args": ["-V"],
                        "env": {"TOKEN": "${KC_TEST_TOKEN}", "LEVEL": "debug"},
                    }
                },
            }
        )
    )


def test_a_rebind_keeps_the_pair_of_an_agent_whose_source_could_not_be_read(tmp_path, monkeypatch):
    """A pass that kept an overlay retires nothing under the rebound command.

    The keep skips the spec read, so that agent's launches are never admitted
    and the pass's live set is missing them. Retiring what the pass did not see
    would drop the kept overlay's own approved pair, and the backend behind the
    overlay and sidecar still on disk would fail gatewayd's approval check.
    """
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "v0")
    _placeholder_agent(tmp_path)
    _second_agent_declaring_the_same_launch(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)
    command_hash = rewriter.hash_command(_APPROVED_CMD, ["-V"])
    kept_pair_env = mod.env_fingerprint({"TOKEN": "v0", "LEVEL": "debug"})
    assert mod.load_approvals(store).admits_launch("SRV", command_hash, kept_pair_env)

    # The value rotates, and b's source cannot be read on this pass: its
    # previous overlay and sidecar stay in effect, still carrying TOKEN=v0.
    monkeypatch.setenv("KC_TEST_TOKEN", "v1")
    real_read = agent_discovery.read_agent_spec_strict

    def _read_failing_for_b(spec_path, **kwargs):
        if spec_path.stem == "b":
            raise OSError("spec unreadable on this pass")
        return real_read(spec_path, **kwargs)

    monkeypatch.setattr(agent_discovery, "read_agent_spec_strict", _read_failing_for_b)
    approvals = mod.load_approvals(store)
    _rewrite(tmp_path, frozenset({"srv"}), approvals)
    mod.save_pass(approvals, path=store)

    overlay_dir = tmp_path / "home" / "mcp-gateway" / "agents"
    assert (overlay_dir / "b.json").is_file(), "the kept overlay was pruned"
    stored = mod.load_approvals(store)
    assert stored.admits_launch("SRV", command_hash, mod.env_fingerprint({"TOKEN": "v1"}))
    assert stored.admits_launch("SRV", command_hash, kept_pair_env), (
        "the kept overlay's approved pair was retired as unseen, so the backend "
        "behind it fails the approval check at spawn"
    )
    live_incomplete = getattr(approvals, "live_incomplete", None)
    assert (
        live_incomplete is True
    ), "LaunchApprovals exposes no live_incomplete set by a pass that kept an overlay"
