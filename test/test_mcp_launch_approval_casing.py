"""An approved launch keeps its approval through the PATHEXT casing repair.

``env.saved_command_casing`` rewrites a saved ``...\\tool.EXE`` to the on-disk
``...\\tool.exe``. A launch whose only difference from an approved one is that
repair stays approved, with its own audit reason. A stem in another case, an
extra argument, a missing approved display, or a spelling the repair does not
produce is still refused as changed.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew import env as env_mod
from kiro_crew.mcp_gateway import launch_approval as mod

#: Opts this module out of the conftest fixture that approves every gatewayd launch.
ENFORCE_LAUNCH_APPROVAL = True

_APPROVED = "X:\\bin\\tool.EXE"
_REPAIRED = "X:\\bin\\tool.exe"
_ENV = {"DEMO_FLAG": "1"}
_STEM = mod.target_stem("srv")


def _launch(command: str, args: tuple[str, ...] = ()) -> mod.ResolvedLaunch:
    return mod.ResolvedLaunch(
        mod.launch_fingerprint(command, args, _ENV),
        command,
        args,
        frozenset({mod.env_fingerprint(_ENV)}),
        tuple(sorted(_ENV.items())),
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    # The repair as it runs on Windows when the file on disk is ``tool.exe``.
    monkeypatch.setattr(
        env_mod,
        "saved_command_casing",
        lambda path: _REPAIRED if path == _APPROVED else path,
    )
    path = tmp_path / "approvals.json"
    mod.approve({"srv": [_launch(_APPROVED)]}, path=path)
    return path


def _admit(approvals: mod.LaunchApprovals, command: str, args: tuple[str, ...] = ()) -> bool:
    live = _launch(command, args)
    return approvals.admit(
        "srv",
        live.fingerprint,
        launch=(command, list(args)),
        env=_ENV,
        derived_env_hash=mod.env_fingerprint(_ENV),
    )


def test_the_casing_repair_keeps_the_approval(store, caplog):
    approvals = mod.load_approvals(store)
    assert _admit(approvals, _REPAIRED)
    assert approvals.refused == {}
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        assert mod.save_pass(approvals, path=store)
    assert mod.CASING_AUDIT_REASON in caplog.text

    stored = mod.load_approvals(store)
    repaired = mod.hash_command(_REPAIRED, [])
    assert stored.admits_launch(_STEM, repaired, mod.env_fingerprint(_ENV))
    assert stored.refused == {}


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("X:\\bin\\TOOL.exe", ()),  # stem case differs: not the repair's output
        (_REPAIRED, ("--extra",)),  # one extra argument
        ("X:\\bin\\tool.Exe", ()),  # a spelling the repair does not produce
    ],
)
def test_any_other_change_is_still_refused(store, command, args):
    approvals = mod.load_approvals(store)
    assert not _admit(approvals, command, args)
    assert approvals.refused == {_STEM: mod.REFUSED_CHANGED}


def test_no_approved_display_is_still_refused(store):
    approvals = mod.load_approvals(store)
    approvals.approved_displays.clear()
    assert not _admit(approvals, _REPAIRED)
    assert approvals.refused == {_STEM: mod.REFUSED_CHANGED}


def test_without_the_repair_the_change_is_refused(store, monkeypatch):
    # POSIX, or a Windows directory entry spelled ``tool.EXE``: no repair runs.
    monkeypatch.setattr(env_mod, "saved_command_casing", lambda path: path)
    approvals = mod.load_approvals(store)
    assert not _admit(approvals, _REPAIRED)
    assert approvals.refused == {_STEM: mod.REFUSED_CHANGED}


def test_a_revoke_before_the_save_drops_the_repair(store, caplog):
    approvals = mod.load_approvals(store)
    assert _admit(approvals, _REPAIRED)
    mod.revoke(["srv"], path=store)
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        mod.save_pass(approvals, path=store)
    assert mod.CASING_AUDIT_REASON not in caplog.text

    stored = mod.load_approvals(store)
    env_hash = mod.env_fingerprint(_ENV)
    for command in (_APPROVED, _REPAIRED):
        assert not stored.admits_launch(_STEM, mod.hash_command(command, []), env_hash)
    assert not stored.approved.get(_STEM)
    assert not stored.approved_pairs.get(_STEM)
    assert not stored.approved_displays.get(_STEM)
