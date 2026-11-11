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
agent's approved pair. Other agents filling one server's launch cap first
neither refuse that server's rebind nor stop another server's rebinds from
retiring their old expansions.
"""

from __future__ import annotations

import json
import shlex
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


def _approve_as_the_toggle_does(
    tmp_path: Path, mod, store: Path, names: tuple[str, ...] = ("srv",)
) -> None:
    """Resolve what *names* run now and approve exactly that, as the MCP page does."""
    probe = mod.LaunchApprovals(probe=True)
    _rewrite(tmp_path, frozenset(names), probe)
    for name in names:
        launches = list(probe.captured_launches.get(mod.target_stem(name), {}).values())
        assert launches, f"the probe pass resolved no launch for {name}"
        mod.approve({name: launches}, path=store)


def _rewrite_and_persist(tmp_path: Path, mod, store: Path, names: tuple[str, ...] = ("srv",)):
    """One gateway-start pass against the stored approvals, persisted after."""
    approvals = mod.load_approvals(store)
    target_env = _rewrite(tmp_path, frozenset(names), approvals)
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
                "tools": ["*"],
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
                "tools": ["*"],
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


def _crew_agents(tmp_path: Path, agents: dict[str, dict]) -> None:
    """Agents named to sort after every ``aim-NN`` one, as ``kiro-crew`` does."""
    source = tmp_path / "agents"
    source.mkdir(parents=True, exist_ok=True)
    for name, servers in agents.items():
        (source / f"{name}.json").write_text(
            json.dumps({"name": name, "tools": ["*"], "mcpServers": servers})
        )


def _crowd_before(tmp_path: Path, server: str, count: int) -> None:
    """*count* agents, read first, each declaring *server* with a launch of its own.

    The shape of AIM specs that each pass their own ``--skill-paths``.
    """
    for index in range(count):
        name = f"aim-{index:02d}"
        entry = {"command": _APPROVED_CMD, "args": ["--skill-paths", f"/skills/{index}"]}
        (tmp_path / "agents" / f"{name}.json").write_text(
            json.dumps({"name": name, "tools": ["*"], "mcpServers": {server: entry}})
        )


def _wrapped(tmp_path: Path, agent: str, server: str) -> bool:
    overlay = tmp_path / "home" / "mcp-gateway" / "agents" / f"{agent}.json"
    entry = json.loads(overlay.read_text())["mcpServers"][server]
    return bool(entry.get(rewriter._WRAPPER_MARKER))


_PLACEHOLDER_ENTRY = {
    "command": _APPROVED_CMD,
    "args": ["-V"],
    "env": {"TOKEN": "${KC_TEST_TOKEN}"},
}


def test_a_rebind_is_admitted_after_other_agents_fill_the_launch_cap(tmp_path, monkeypatch):
    """A changed expansion of the approved launch is the approved launch, cap or not.

    Agents read first fill the per-server cap with launches of their own. The
    rebind is still wrapped and replaces its old expansion, and a second agent's
    unchanged approved pair under the same command, also past the cap, is kept
    as live rather than retired as unseen.
    """
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    cap = getattr(mod, "_MAX_SERVER_LAUNCHES")
    monkeypatch.setenv("KC_TEST_TOKEN", "v0")
    static_entry = {"command": _APPROVED_CMD, "args": ["-V"], "env": {"LEVEL": "debug"}}
    _crew_agents(
        tmp_path, {"kiro-crew": {"srv": _PLACEHOLDER_ENTRY}, "other": {"srv": static_entry}}
    )
    _approve_as_the_toggle_does(tmp_path, mod, store)
    _crowd_before(tmp_path, "srv", cap + 1)
    command_hash = rewriter.hash_command(_APPROVED_CMD, ["-V"])
    static_pair = mod.launch_pair(command_hash, mod.env_fingerprint({"LEVEL": "debug"}))

    for i in range(1, 3):
        monkeypatch.setenv("KC_TEST_TOKEN", f"v{i}")
        approvals, target_env = _rewrite_and_persist(tmp_path, mod, store)

        assert approvals.refused == {"SRV": mod.REFUSED_TOO_MANY}
        assert _wrapped(tmp_path, "kiro-crew", "srv"), f"rebind refused after {i} changes"
        assert _wrapped(tmp_path, "other", "srv")
        assert not _wrapped(tmp_path, "aim-00", "srv")
        running = {target_env[key] for key in _targets_running(target_env, _APPROVED_CMD)}
        assert [shlex.split(spec) for spec in running] == [[_APPROVED_CMD, "-V"]]
        rotated = mod.launch_pair(command_hash, mod.env_fingerprint({"TOKEN": f"v{i}"}))
        assert mod.load_approvals(store).approved_pairs["SRV"] == {rotated, static_pair}


def test_a_cap_filled_on_one_server_keeps_another_servers_rebinds_retiring(tmp_path, monkeypatch):
    """The launch cap one server reaches is that server's alone.

    ``big`` has more launches before its approved one than the cap retains,
    while ``tok``'s ``${VAR}`` keeps changing. ``big``'s approved launch is
    wrapped, and each ``tok`` rebind still replaces the old expansion rather
    than piling expansions up until ``tok``'s own cap refuses it.
    """
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    cap = getattr(mod, "_MAX_SERVER_LAUNCHES")
    names = ("big", "tok")
    monkeypatch.setenv("KC_TEST_TOKEN", "v0")
    big_entry = {"command": _APPROVED_CMD, "args": []}
    _crew_agents(tmp_path, {"kiro-crew": {"big": big_entry, "tok": _PLACEHOLDER_ENTRY}})
    _approve_as_the_toggle_does(tmp_path, mod, store, names)
    _crowd_before(tmp_path, "big", cap + 1)
    big_hash = rewriter.hash_command(_APPROVED_CMD, [])
    tok_hash = rewriter.hash_command(_APPROVED_CMD, ["-V"])

    for i in range(1, 4):
        monkeypatch.setenv("KC_TEST_TOKEN", f"v{i}")
        approvals, _target_env = _rewrite_and_persist(tmp_path, mod, store, names)

        assert approvals.refused == {"BIG": mod.REFUSED_TOO_MANY}
        assert _wrapped(tmp_path, "kiro-crew", "big")
        assert _wrapped(tmp_path, "kiro-crew", "tok")
        stored = mod.load_approvals(store)
        assert stored.admits_launch("BIG", big_hash, mod.env_fingerprint({}))
        # Keeping a pair per change would reach the cap after that many changes.
        rotated = mod.launch_pair(tok_hash, mod.env_fingerprint({"TOKEN": f"v{i}"}))
        assert stored.approved_pairs["TOK"] == {rotated}, f"old expansions kept after {i} changes"
