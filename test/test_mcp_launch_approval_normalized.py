"""An approved launch keeps its approval when only an edition-folded token moves.

An edition may supply a launch normalizer
(``McpToolingProvider.launch_token_normalizer``) that maps a version-only path segment to one canonical spelling. The store keeps
the normalized digest beside each approved pair. When a live launch is not
approved but normalizes to an approved pair with the same env, the rewriter's
``admit`` keeps the approval and records the live launch's LITERAL fingerprint,
the way the PATHEXT casing repair does. ``hash_command``, the pool key
and gatewayd's check never see the normalizer.

The normalizer here is a stand-in written for these tests: it folds ``v<n>``
directly under ``/opt/pkgs/<pkg>/``. Core ships no rule of its own.
"""

from __future__ import annotations

import logging
import re
from typing import Callable

import pytest

from kiro_crew.mcp_gateway import launch_approval as mod
from kiro_crew.platform.defaults import DefaultMcpToolingProvider

#: Opts this module out of the conftest fixture that approves every gatewayd launch.
ENFORCE_LAUNCH_APPROVAL = True

_CMD = "/opt/bin/tool-mcp"
_OLD = ("--sops", "/opt/pkgs/demo/v3/sops", "--port", "0")
_NEW = ("--sops", "/opt/pkgs/demo/v4/sops", "--port", "0")
_ENV = {"DEMO_FLAG": "1"}
_STEM = mod.target_stem("srv")
_SEGMENT = re.compile(r"^(/opt/pkgs/[^/]+/)v\d+(/|$)")


def _fold(token: str) -> str:
    return _SEGMENT.sub(r"\1v*\2", token)


def _launch(
    args: tuple[str, ...], env: dict[str, str] = _ENV, cmd: str = _CMD
) -> mod.ResolvedLaunch:
    return mod.ResolvedLaunch(
        mod.launch_fingerprint(cmd, args, env),
        cmd,
        args,
        frozenset({mod.env_fingerprint(env)}),
        tuple(sorted(env.items())),
    )


def _admit(
    approvals: mod.LaunchApprovals,
    args: tuple[str, ...],
    env: dict[str, str] = _ENV,
    cmd: str = _CMD,
) -> bool:
    live = _launch(args, env, cmd)
    return approvals.admit(
        "srv",
        live.fingerprint,
        launch=(cmd, list(args)),
        env=env,
        derived_env_hash=mod.env_fingerprint(env),
    )


def _install(monkeypatch, fn: Callable[[str], str]) -> None:
    """Make the platform context's MCP tooling provider normalize with *fn*."""
    monkeypatch.setattr(
        DefaultMcpToolingProvider, "launch_token_normalizer", lambda self, token: fn(token)
    )


def _unchanged(token: str) -> str:
    return token


@pytest.fixture
def normalizer(monkeypatch):
    _install(monkeypatch, _fold)
    return _fold


@pytest.fixture
def store(tmp_path, normalizer):
    path = tmp_path / "approvals.json"
    mod.approve({"srv": [_launch(_OLD)]}, path=path)
    return path


def test_a_moved_version_folder_keeps_the_approval(store, caplog):
    approvals = mod.load_approvals(store)
    assert _admit(approvals, _NEW)
    assert approvals.refused == {}
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        assert mod.save_pass(approvals, path=store)
    assert mod.NORMALIZED_AUDIT_REASON in caplog.text

    stored = mod.load_approvals(store)
    env_hash = mod.env_fingerprint(_ENV)
    # gatewayd checks the literal digest the stub sends; the rewriter recorded it.
    assert stored.admits_launch(_STEM, mod.hash_command(_CMD, list(_NEW)), env_hash)
    assert stored.refused == {}


def test_the_next_update_keeps_the_approval_too(store):
    first = mod.load_approvals(store)
    assert _admit(first, _NEW)
    assert mod.save_pass(first, path=store)

    newer = ("--sops", "/opt/pkgs/demo/v5/sops", "--port", "0")
    second = mod.load_approvals(store)
    assert _admit(second, newer)
    assert second.refused == {}


def _full_pass(store, *launches: tuple[str, ...]) -> mod.LaunchApprovals:
    approvals = mod.load_approvals(store)
    approvals.full_pass = True
    for args in launches:
        assert _admit(approvals, args)
    mod.save_pass(approvals, path=store)
    return approvals


def _version(n: int) -> tuple[str, ...]:
    return ("--sops", f"/opt/pkgs/demo/v{n}/sops", "--port", "0")


def test_many_updates_never_reach_the_launch_cap(store):
    for n in range(4, 4 + mod._MAX_SERVER_LAUNCHES + 6):
        approvals = _full_pass(store, _version(n))
        assert approvals.refused == {}
    stored = mod.load_approvals(store)
    assert len(stored.approved[_STEM]) == 1
    assert len(stored.approved_pairs[_STEM]) == 1


def test_many_updates_with_two_envs_never_reach_the_launch_cap(store):
    envs = (_ENV, {"DEMO_FLAG": "2"})
    mod.approve({"srv": [_launch(_OLD, env) for env in envs]}, path=store)
    for n in range(4, 4 + mod._MAX_SERVER_LAUNCHES):
        args = _version(n)
        approvals = mod.load_approvals(store)
        approvals.full_pass = True
        for env in envs:
            assert _admit(approvals, args, env)
        assert mod.save_pass(approvals, path=store)
        assert approvals.refused == {}

        stored = mod.load_approvals(store)
        launches = [_launch(args, env) for env in envs]
        assert stored.approved[_STEM] == {launch.fingerprint for launch in launches}
        assert stored.approved_pairs[_STEM] == set().union(
            *(launch.approval_identities for launch in launches)
        )
        assert stored.refused == {}


def test_a_source_still_live_is_kept(store):
    _full_pass(store, _OLD, _NEW)
    stored = mod.load_approvals(store)
    env_hash = mod.env_fingerprint(_ENV)
    for args in (_OLD, _NEW):
        assert stored.admits_launch(_STEM, mod.hash_command(_CMD, list(args)), env_hash)


def test_a_partial_pass_retires_nothing(store):
    approvals = mod.load_approvals(store)
    assert _admit(approvals, _NEW)
    mod.save_pass(approvals, path=store)  # not a full pass
    stored = mod.load_approvals(store)
    env_hash = mod.env_fingerprint(_ENV)
    assert stored.admits_launch(_STEM, mod.hash_command(_CMD, list(_OLD)), env_hash)


def test_hash_command_and_the_pool_key_stay_literal(normalizer):
    assert mod.hash_command(_CMD, list(_OLD)) != mod.hash_command(_CMD, list(_NEW))


@pytest.mark.parametrize(
    "args",
    [
        ("--sops", "/opt/pkgs/demo/v4/sops", "--port", "1"),  # another argument changed
        ("--sops", "/opt/pkgs/other/v4/sops", "--port", "0"),  # another package
        ("--sops", "/opt/pkgs/demo/v4/other", "--port", "0"),  # path below the folder
        ("--sops", "/opt/pkgs/demo/x4/sops", "--port", "0"),  # not a version segment
        ("--sops", "/opt/pkgs/demo/v4/sops", "--port", "0", "--extra"),  # extra argument
    ],
)
def test_any_other_change_is_still_refused(store, args):
    approvals = mod.load_approvals(store)
    assert not _admit(approvals, args)
    assert approvals.refused == {_STEM: mod.REFUSED_CHANGED}


def test_a_changed_env_is_still_refused(store):
    approvals = mod.load_approvals(store)
    assert not _admit(approvals, _NEW, {"DEMO_FLAG": "2"})
    assert approvals.refused == {_STEM: mod.REFUSED_CHANGED}


def test_a_folded_program_path_is_still_refused(tmp_path, normalizer):
    path = tmp_path / "approvals.json"
    old_cmd, new_cmd = "/opt/pkgs/demo/v3/bin/tool", "/opt/pkgs/demo/v4/bin/tool"
    assert normalizer(old_cmd) == normalizer(new_cmd)  # the stand-in would fold it
    mod.approve({"srv": [_launch(_OLD, cmd=old_cmd)]}, path=path)
    approvals = mod.load_approvals(path)
    assert not _admit(approvals, _NEW, cmd=new_cmd)
    assert approvals.refused == {_STEM: mod.REFUSED_CHANGED}


def test_without_a_normalizer_the_change_is_refused(store, monkeypatch):
    _install(monkeypatch, _unchanged)
    approvals = mod.load_approvals(store)
    assert not _admit(approvals, _NEW)
    assert approvals.refused == {_STEM: mod.REFUSED_CHANGED}


def test_an_approval_from_before_the_normalizer_learns_it_on_the_next_pass(tmp_path, monkeypatch):
    path = tmp_path / "approvals.json"
    mod.approve({"srv": [_launch(_OLD)]}, path=path)  # the public default folds nothing
    _install(monkeypatch, _fold)
    # The update arrives first: the stored pair has no normalized digest yet.
    early = mod.load_approvals(path)
    assert not _admit(early, _NEW)

    # A pass that admits the approved launch exactly records the digest ...
    exact = mod.load_approvals(path)
    assert _admit(exact, _OLD)
    assert mod.save_pass(exact, path=path)

    # ... so the next update keeps the approval.
    later = mod.load_approvals(path)
    assert _admit(later, _NEW)
    assert later.refused == {}


def test_a_revoke_before_the_save_drops_the_repair(store, caplog):
    approvals = mod.load_approvals(store)
    assert _admit(approvals, _NEW)
    mod.revoke(["srv"], path=store)
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        mod.save_pass(approvals, path=store)
    assert mod.NORMALIZED_AUDIT_REASON not in caplog.text

    stored = mod.load_approvals(store)
    env_hash = mod.env_fingerprint(_ENV)
    for args in (_OLD, _NEW):
        assert not stored.admits_launch(_STEM, mod.hash_command(_CMD, list(args)), env_hash)


def test_a_normalizer_that_raises_folds_nothing(store, monkeypatch):
    def broken(token: str) -> str:
        raise RuntimeError("boom")

    _install(monkeypatch, broken)
    approvals = mod.load_approvals(store)
    assert not _admit(approvals, _NEW)
    assert approvals.refused == {_STEM: mod.REFUSED_CHANGED}


def test_a_normalizer_that_returns_a_non_string_folds_nothing(store, monkeypatch):
    _install(monkeypatch, lambda token: None)
    assert mod.normalized_command_hash(_CMD, list(_NEW)) is None
    assert not _admit(mod.load_approvals(store), _NEW)


def test_a_provider_without_the_method_folds_nothing(store, monkeypatch):
    monkeypatch.delattr(DefaultMcpToolingProvider, "launch_token_normalizer")
    assert mod.normalized_command_hash(_CMD, list(_NEW)) is None
    assert not _admit(mod.load_approvals(store), _NEW)


def test_staggered_two_env_updates_never_reach_the_launch_cap(store):
    envs = (_ENV, {"DEMO_FLAG": "2"})
    mod.approve({"srv": [_launch(_OLD, env) for env in envs]}, path=store)
    versions = [_OLD, _OLD]
    for n in range(4, 4 + mod._MAX_SERVER_LAUNCHES):
        for moved in range(2):
            versions[moved] = _version(n)
            approvals = mod.load_approvals(store)
            approvals.full_pass = True
            for args, env in zip(versions, envs):
                assert _admit(approvals, args, env)
            assert mod.save_pass(approvals, path=store)
            assert approvals.refused == {}
            stored = mod.load_approvals(store)
            launches = [_launch(args, env) for args, env in zip(versions, envs)]
            if moved == 1:
                assert stored.approved[_STEM] == {launch.fingerprint for launch in launches}
            assert stored.approved_pairs[_STEM] == set().union(
                *(launch.approval_identities for launch in launches)
            )
            assert stored.stored_refused == {}


def test_an_env_rebind_then_a_version_move_keeps_the_approval(tmp_path, normalizer):
    path = tmp_path / "approvals.json"
    declared = {"TOKEN": "${KC_TEST_TOKEN}"}
    first_env = mod.env_fingerprint({"TOKEN": "first"})
    rotated_env = mod.env_fingerprint({"TOKEN": "rotated"})
    original = _launch(_OLD, declared)
    mod.approve(
        {
            "srv": [
                mod.ResolvedLaunch(
                    original.fingerprint, _CMD, _OLD, frozenset({first_env}), original.declared_env
                )
            ]
        },
        path=path,
    )
    rebound = mod.load_approvals(path)
    rebound.full_pass = True
    assert rebound.admit(
        "srv",
        original.fingerprint,
        launch=(_CMD, list(_OLD)),
        env=declared,
        derived_env_hash=rotated_env,
    )
    assert mod.save_pass(rebound, path=path)
    stored = mod.load_approvals(path)
    old_pair = mod.launch_pair(mod.hash_command(_CMD, list(_OLD)), first_env)
    live_pair = mod.launch_pair(mod.hash_command(_CMD, list(_OLD)), rotated_env)
    assert stored.approved_pairs[_STEM] == {live_pair}
    assert old_pair not in stored.approved_normalized.get(_STEM, {})
    assert live_pair in stored.approved_normalized[_STEM]

    moved = mod.load_approvals(path)
    moved.full_pass = True
    assert moved.admit(
        "srv",
        _launch(_NEW, declared).fingerprint,
        launch=(_CMD, list(_NEW)),
        env=declared,
        derived_env_hash=rotated_env,
    )
    assert mod.save_pass(moved, path=path)
    current = mod.load_approvals(path)
    assert current.approved_pairs[_STEM] == {
        mod.launch_pair(mod.hash_command(_CMD, list(_NEW)), rotated_env)
    }
    assert current.stored_refused == {}


def test_a_full_pass_keeps_unrelated_approved_pairs(store):
    unrelated = [
        _launch(("--sops", "/opt/pkgs/other/v3/sops")),
        _launch(("--plain", "unversioned")),
    ]
    mod.approve({"srv": [_launch(_OLD), *unrelated]}, path=store)
    before = mod.load_approvals(store)
    pairs = set().union(*(launch.approval_identities for launch in unrelated))
    assert len(pairs & before.approved_normalized[_STEM].keys()) == 1
    partial = mod.load_approvals(store)
    assert _admit(partial, _NEW)
    assert mod.save_pass(partial, path=store)
    _full_pass(store, _NEW)
    stored = mod.load_approvals(store)
    assert stored.approved[_STEM] == {
        _launch(_NEW).fingerprint,
        *(launch.fingerprint for launch in unrelated),
    }
    assert stored.approved_pairs[_STEM] == pairs | _launch(_NEW).approval_identities
    assert stored.stored_refused == {}


@pytest.mark.parametrize("same_derived_env", [False, True])
def test_an_unused_pair_with_another_env_is_never_retired(store, same_derived_env):
    live = _launch(_NEW)
    unused = _launch(_OLD, {"DEMO_FLAG": "2"})
    if same_derived_env:
        unused = mod.ResolvedLaunch(
            unused.fingerprint, _CMD, _OLD, live.derived_env, unused.declared_env
        )
    mod.approve({"srv": [live, unused]}, path=store)
    before = mod.load_approvals(store)
    assert set(before.approved_normalized[_STEM].values()) == {
        mod.normalized_command_hash(_CMD, list(_NEW))
    }

    approvals = mod.load_approvals(store)
    approvals.full_pass = True
    assert _admit(approvals, _NEW)
    mod.save_pass(approvals, path=store)

    stored = mod.load_approvals(store)
    assert stored.approved[_STEM] == {live.fingerprint, unused.fingerprint}
    assert stored.approved_pairs[_STEM] == live.approval_identities | unused.approval_identities
    assert stored.approved_normalized[_STEM] == before.approved_normalized[_STEM]
    assert stored.admit(
        "srv",
        unused.fingerprint,
        launch=(_CMD, list(_OLD)),
        env=dict(unused.declared_env),
        derived_env_hash=next(iter(unused.derived_env)),
    )
    assert stored.refused == {}


def test_an_approval_during_the_pass_is_never_retired(store):
    approvals = mod.load_approvals(store)
    approvals.full_pass = True
    assert _admit(approvals, _OLD)
    newer = _launch(_NEW, {"DEMO_FLAG": "2"})
    mod.approve({"srv": [_launch(_OLD), newer]}, path=store)
    mod.save_pass(approvals, path=store)

    stored = mod.load_approvals(store)
    assert stored.approved[_STEM] == {_launch(_OLD).fingerprint, newer.fingerprint}
    assert stored.approved_pairs[_STEM] == (
        _launch(_OLD).approval_identities | newer.approval_identities
    )
    assert approvals.refused == {}
    assert stored.stored_refused == {}


def test_a_retirement_skipped_for_a_newer_approval_happens_on_the_next_pass(store):
    approvals = mod.load_approvals(store)
    approvals.full_pass = True
    assert _admit(approvals, _NEW)
    unrelated = _launch(("--plain", "unversioned"))
    mod.approve({"srv": [_launch(_OLD), unrelated]}, path=store)
    assert mod.save_pass(approvals, path=store)

    stored = mod.load_approvals(store)
    assert stored.approved[_STEM] == {
        _launch(_OLD).fingerprint,
        _launch(_NEW).fingerprint,
        unrelated.fingerprint,
    }
    assert stored.approved_pairs[_STEM] == (
        _launch(_OLD).approval_identities
        | _launch(_NEW).approval_identities
        | unrelated.approval_identities
    )
    assert approvals.refused == {}
    assert stored.stored_refused == {}

    _full_pass(store, _NEW)
    stored = mod.load_approvals(store)
    assert stored.approved[_STEM] == {_launch(_NEW).fingerprint, unrelated.fingerprint}
    assert stored.approved_pairs[_STEM] == (
        _launch(_NEW).approval_identities | unrelated.approval_identities
    )
    assert stored.stored_refused == {}


@pytest.mark.parametrize("launch_count", [1, mod._MAX_SERVER_LAUNCHES])
def test_a_full_pass_without_a_normalizer_writes_nothing(tmp_path, launch_count):
    path = tmp_path / "approvals.json"
    launches = [_launch(_version(n)) for n in range(3, 3 + launch_count)]
    mod.approve({"srv": launches}, path=path)
    original = path.read_bytes()
    approvals = mod.load_approvals(path)
    assert len(approvals.approved[_STEM]) == launch_count
    assert approvals.approved_normalized == {}
    approvals.full_pass = True
    assert _admit(approvals, _OLD)
    assert approvals.captured == {}
    assert not mod.save_pass(approvals, path=path)
    assert path.read_bytes() == original
    assert approvals.refused == {}
    assert mod.load_approvals(path).stored_refused == {}
