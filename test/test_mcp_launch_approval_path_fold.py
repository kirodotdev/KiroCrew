"""The gateway's own part of a declared ``PATH`` is not part of a launch approval.

``env.spec_env_path`` appends the gateway's own ``PATH`` (plus the well-known
MCP directories) to every declared spec ``PATH``, so a re-rendered service unit
or a package installer that adds or moves a directory there changed every such
server's fingerprint and paused its share. The approval hash folds that part
(``hashing.hash_launch_env``); the spec's own entries still count, a ``PATH``
that is not that composition hashes as written, and the pool key is unchanged.
The gateway part is pinned for the broker's life and handed to gatewayd, so a
live ``PATH`` change cannot make the two sides fold differently. Only that
current part folds: a value saved already expanded asks once after the part
moves, and no earlier part can hide an entry after the own entries. An
approval stored before the fold is asked for again once, and an adopted
gatewayd pinned to another part is replaced.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from mcp_launch_approval_helpers import (
    _APPROVED_CMD,
    _approval_module,
    _rewrite,
    _targets_running,
    _write_agent,
)

from conftest import host_abs
from kiro_crew import env as env_mod
from kiro_crew.mcp_gateway import hashing, rewriter

#: Opts this module out of the conftest fixture that approves every gatewayd launch.
ENFORCE_LAUNCH_APPROVAL = True

_OWN = os.pathsep.join([host_abs("opt", "tools", "bin"), host_abs("opt", "tools", "sbin")])
_ARGS = ["-V"]


def _gateway_path(monkeypatch, tmp_path: Path, *names: str) -> None:
    """Point this process's PATH at fresh directories, as a re-rendered unit does."""
    dirs = []
    for name in names:
        d = tmp_path / "gw" / name
        d.mkdir(parents=True, exist_ok=True)
        dirs.append(str(d))
    monkeypatch.setenv("PATH", os.pathsep.join(dirs))


def _agent(tmp_path: Path, own: str = _OWN) -> dict[str, str]:
    env = {"PATH": env_mod.spec_env_path(own)}
    _write_agent(
        tmp_path / "agents", {"srv": {"command": _APPROVED_CMD, "args": _ARGS, "env": env}}
    )
    return env


def test_a_changed_gateway_path_keeps_the_env_fingerprint(tmp_path, monkeypatch):
    mod = _approval_module()
    _gateway_path(monkeypatch, tmp_path, "bin", "cargo")
    before = env_mod.spec_env_path(_OWN)
    folded = mod.env_fingerprint({"PATH": before})

    _gateway_path(monkeypatch, tmp_path, "sbin", "node-22.24.0")
    after = env_mod.spec_env_path(_OWN)

    assert after != before
    assert mod.env_fingerprint({"PATH": after}) == folded


def test_the_specs_own_entries_still_count(tmp_path, monkeypatch):
    mod = _approval_module()
    _gateway_path(monkeypatch, tmp_path, "bin")
    mine = mod.env_fingerprint({"PATH": env_mod.spec_env_path(_OWN)})
    other = mod.env_fingerprint({"PATH": env_mod.spec_env_path(host_abs("opt", "other", "bin"))})
    reordered = os.pathsep.join(reversed(_OWN.split(os.pathsep)))

    assert other != mine
    assert mod.env_fingerprint({"PATH": env_mod.spec_env_path(reordered)}) != mine


def test_a_path_that_is_not_the_composition_hashes_as_written(tmp_path, monkeypatch):
    mod = _approval_module()
    _gateway_path(monkeypatch, tmp_path, "bin")
    hand = {"PATH": os.pathsep.join([host_abs("opt", "a"), host_abs("opt", "b")]), "OTHER": "x"}

    assert mod.env_fingerprint(hand) == hashing.hash_declared_env(hand)
    # The same composition under another key is content, not the gateway's part.
    moved = {"TOOLS_PATH": env_mod.spec_env_path(_OWN)}
    assert mod.env_fingerprint(moved) == hashing.hash_declared_env(moved)


def test_the_pool_key_still_sees_the_real_path(tmp_path, monkeypatch):
    _gateway_path(monkeypatch, tmp_path, "bin")
    before = hashing.hash_effective_env({"PATH": env_mod.spec_env_path(_OWN)})
    _gateway_path(monkeypatch, tmp_path, "sbin")

    assert hashing.hash_effective_env({"PATH": env_mod.spec_env_path(_OWN)}) != before


def test_gatewayd_folds_with_the_part_it_was_handed(tmp_path, monkeypatch):
    mod = _approval_module()
    _gateway_path(monkeypatch, tmp_path, "bin")
    value = env_mod.spec_env_path(_OWN)
    folded = mod.env_fingerprint({"PATH": value})
    handed = hashing.gateway_path()

    # gatewayd's own PATH differs from the gateway's; it is handed the gateway's.
    _gateway_path(monkeypatch, tmp_path, "daemon")
    assert mod.env_fingerprint({"PATH": value}) != folded
    hashing.pin_gateway_path(None)
    assert mod.env_fingerprint({"PATH": value}) != folded
    hashing.pin_gateway_path(handed)
    assert mod.env_fingerprint({"PATH": value}) == folded


def test_a_live_path_change_keeps_the_pinned_part(tmp_path, monkeypatch):
    """A ``.env`` ``PATH`` edit applied live, or an added ffmpeg directory, moves
    the gateway's own ``PATH`` after gatewayd was handed its part. The rewriter,
    the dashboard and gatewayd must still fold one composition."""
    mod = _approval_module()
    _gateway_path(monkeypatch, tmp_path, "bin")
    mod.pin_gateway_part()
    value = env_mod.spec_env_path(_OWN)
    folded = mod.env_fingerprint({"PATH": value})
    handed = hashing.gateway_path()

    _gateway_path(monkeypatch, tmp_path, "bin", "ffmpeg")

    assert env_mod.spec_env_path(_OWN) == value
    assert mod.env_fingerprint({"PATH": value}) == folded
    assert hashing.gateway_path() == handed


def _next_start(monkeypatch, tmp_path: Path, mod, *names: str) -> None:
    """A gateway restart under a new PATH: the broker pins the new part."""
    monkeypatch.setattr(env_mod, "_PINNED_GATEWAY_SPEC_PATH", None)
    _gateway_path(monkeypatch, tmp_path, *names)
    mod.pin_gateway_part()


def test_a_saved_expanded_value_asks_once_after_the_gateway_path_moves(tmp_path, monkeypatch):
    """App Kit register saves ``emit_env`` output (own entries + the gateway part)
    into ``mcp.json``. Only the current part folds, so after the part moves the
    old part reads as authored entries and the launch is approved again once."""
    mod = _approval_module()
    store = tmp_path / "approvals.json"
    _gateway_path(monkeypatch, tmp_path, "bin", "node-22.1.0", "cargo")
    mod.pin_gateway_part()
    saved = env_mod.spec_env_path(_OWN)
    _agent(tmp_path, own=saved)
    _approve_as_the_toggle_does(tmp_path, mod, store)

    _next_start(monkeypatch, tmp_path, mod, "bin", "node-22.24.0")
    _agent(tmp_path, own=saved)
    approvals, target_env = _rewrite_and_persist(tmp_path, mod, store)

    assert approvals.refused.get("SRV") == mod.REFUSED_CHANGED
    assert not _targets_running(target_env, _APPROVED_CMD)


def test_an_earlier_gateway_part_after_the_own_entries_still_counts(tmp_path, monkeypatch):
    """An agent inserts a gateway part an earlier start used after the server's
    own entries. Its directories are a search path the person never approved."""
    mod = _approval_module()
    store = tmp_path / "approvals.json"
    _gateway_path(monkeypatch, tmp_path, "bin", "tools")
    mod.pin_gateway_part()
    earlier_part = env_mod.gateway_spec_path()
    _agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)
    _rewrite_and_persist(tmp_path, mod, store)

    _next_start(monkeypatch, tmp_path, mod, "bin")
    _agent(tmp_path, own=os.pathsep.join([_OWN, earlier_part]))
    approvals, target_env = _rewrite_and_persist(tmp_path, mod, store)

    assert approvals.refused.get("SRV") == mod.REFUSED_CHANGED
    assert not _targets_running(target_env, _APPROVED_CMD)


def _approve_as_the_toggle_does(tmp_path: Path, mod, store: Path) -> None:
    probe = mod.LaunchApprovals(probe=True)
    _rewrite(tmp_path, frozenset({"srv"}), probe)
    launches = list(probe.captured_launches.get("SRV", {}).values())
    assert launches
    mod.approve({"srv": launches}, path=store)


def _rewrite_and_persist(tmp_path: Path, mod, store: Path):
    approvals = mod.load_approvals(store)
    target_env = _rewrite(tmp_path, frozenset({"srv"}), approvals)
    mod.save_pass(approvals, path=store)
    return approvals, target_env


def test_a_share_survives_a_changed_gateway_path(tmp_path, monkeypatch):
    mod = _approval_module()
    store = tmp_path / "approvals.json"
    _gateway_path(monkeypatch, tmp_path, "bin", "cargo")
    _agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)

    _gateway_path(monkeypatch, tmp_path, "sbin")
    _agent(tmp_path)
    approvals, target_env = _rewrite_and_persist(tmp_path, mod, store)

    assert approvals.refused == {}
    assert _targets_running(target_env, _APPROVED_CMD)


def test_a_changed_own_entry_is_still_refused(tmp_path, monkeypatch):
    mod = _approval_module()
    store = tmp_path / "approvals.json"
    _gateway_path(monkeypatch, tmp_path, "bin")
    _agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)

    _agent(tmp_path, own=os.pathsep.join([host_abs("opt", "evil", "bin"), _OWN]))
    approvals, target_env = _rewrite_and_persist(tmp_path, mod, store)

    assert approvals.refused.get("SRV") == mod.REFUSED_CHANGED
    assert not _targets_running(target_env, _APPROVED_CMD)


def test_an_approval_stored_before_the_fold_asks_once(tmp_path, monkeypatch):
    """A store written before the fold holds the literal digests. They match no
    folded identity, so the launch is refused once and the person approves it
    again; nothing is carried over without them."""
    mod = _approval_module()
    store = tmp_path / "approvals.json"
    _gateway_path(monkeypatch, tmp_path, "bin")
    env = _agent(tmp_path)
    approvals = mod.LaunchApprovals()
    command_hash = rewriter.hash_command(_APPROVED_CMD, _ARGS)
    approvals.approved["SRV"] = {
        f"{command_hash}:{hashing.hash_declared_env(env)}",
    }
    approvals.approved_pairs["SRV"] = {
        mod.launch_pair(command_hash, hashing.hash_declared_env(env))
    }
    approvals.names["SRV"] = "srv"
    mod._write(store, mod._document(approvals, {}))

    refused, target_env = _rewrite_and_persist(tmp_path, mod, store)

    assert refused.refused.get("SRV") == mod.REFUSED_CHANGED
    assert not _targets_running(target_env, _APPROVED_CMD)


def test_a_daemon_pinned_to_another_part_is_not_adopted(tmp_path, monkeypatch):
    """An in-place restart keeps the daemon; a package installer moved PATH in
    between, so the new gateway pins another part. That daemon would fold every
    approval differently, so it counts as drift and is replaced."""
    from kiro_crew.code_fingerprint import code_fingerprint
    from kiro_crew.mcp_gateway.manager import GatewayManager, GatewaySpec

    manager = GatewayManager(GatewaySpec(socket_path=tmp_path / "gateway.sock"))
    env_mod.pin_gateway_spec_path(host_abs("gw", "new"))
    pong = {"type": "pong", "targets": [], "fingerprint": code_fingerprint()}

    assert manager._code_drift({**pong, "gateway_path": host_abs("gw", "old")}) is True
    assert manager._code_drift({**pong, "gateway_path": None}) is True
    assert manager._code_drift({**pong, "gateway_path": host_abs("gw", "new")}) is False


def test_the_pong_reports_the_pinned_part(tmp_path, monkeypatch):
    from kiro_crew.mcp_gateway.daemon import control

    env_mod.pin_gateway_spec_path(host_abs("gw", "handed"))

    assert control._pong_payload()["gateway_path"] == host_abs("gw", "handed")


class _SpawnCaptured(RuntimeError):
    pass


def test_the_manager_hands_gatewayd_the_gateway_path(tmp_path, monkeypatch):
    from kiro_crew.mcp_gateway.manager import GatewayManager, GatewaySpec

    _gateway_path(monkeypatch, tmp_path, "bin")
    expected = env_mod.gateway_spec_path()
    captured: dict = {}

    async def _fake_spawn(*_argv, env=None, **_kwargs):
        captured["env"] = dict(env or {})
        raise _SpawnCaptured

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    manager = GatewayManager(GatewaySpec(socket_path=tmp_path / "gateway.sock"))
    monkeypatch.setattr(manager, "_gatewayd_log_path", lambda: tmp_path / "gatewayd.stdout")
    monkeypatch.setattr(manager, "_credential_watch_paths", lambda: [])
    with pytest.raises(_SpawnCaptured):
        asyncio.run(manager._spawn_once())

    assert captured["env"][hashing.GATEWAY_PATH_ENV] == expected


def test_a_daemon_on_another_part_yields_to_a_stand_down(tmp_path, monkeypatch):
    """Same code, another part: the stand-down the gateway sends must be honoured,
    or the gateway settles on adopting a daemon that refuses its approvals."""
    from kiro_crew.code_fingerprint import code_fingerprint
    from kiro_crew.mcp_gateway.daemon import control

    monkeypatch.setattr(control, "resolvable_target_stems", lambda: {"PDF"})
    env_mod.pin_gateway_spec_path(host_abs("gw", "old"))
    frame = {"type": "stand-down", "need": ["PDF"], "caller_fingerprint": code_fingerprint()}

    stop = asyncio.Event()
    same = control._apply_stand_down({**frame, "caller_gateway_path": host_abs("gw", "old")}, stop)
    assert same["type"] == "stand-down-rejected" and not stop.is_set()
    moved = control._apply_stand_down({**frame, "caller_gateway_path": host_abs("gw", "new")}, stop)
    assert moved["type"] == "standing-down" and stop.is_set()


def test_the_stand_down_names_the_gateways_part(tmp_path, monkeypatch):
    from kiro_crew.mcp_gateway.manager import GatewayManager, GatewaySpec

    manager = GatewayManager(GatewaySpec(socket_path=tmp_path / "gateway.sock"))
    env_mod.pin_gateway_spec_path(host_abs("gw", "new"))
    sent: list[dict] = []

    async def _roundtrip(frame):
        sent.append(frame)
        return None

    monkeypatch.setattr(manager, "_control_roundtrip", _roundtrip)
    asyncio.run(manager._request_stand_down([], stale_code=True))

    assert sent and sent[0]["caller_gateway_path"] == host_abs("gw", "new")


def test_an_ambiguous_launch_still_matches_a_secret_free_approval(tmp_path, monkeypatch):
    """Two coherent sidecars differ only in a secret key, so gatewayd falls back to
    the non-secret projection. That must match the folded secret-free approval."""
    from kiro_crew.mcp_gateway.daemon import launch as daemon_launch

    mod = _approval_module()
    _gateway_path(monkeypatch, tmp_path, "bin")
    shared = {"PATH": env_mod.spec_env_path(_OWN)}
    with_secret = {**shared, "AWS_SECRET_ACCESS_KEY": "x"}
    assert hashing.non_secret_env(with_secret) == shared
    monkeypatch.setattr(
        daemon_launch, "_coherent_env_sidecars", lambda _key, _ids: [shared, with_secret]
    )

    class _Key:
        effective_env_hash = hashing.hash_effective_env(shared)

    assert daemon_launch._approval_env_identity(_Key()) == mod.env_fingerprint(shared)
