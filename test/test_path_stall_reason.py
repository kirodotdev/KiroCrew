"""A resolver stall is refused as a timed-out check, not as a sensitive path.

Every case here still REFUSES -- the gates fail closed on a stall exactly as before.
What is pinned is the WORDING: a stall opens with ``UNVERIFIABLE_PATH_PREFIX``,
names the stalled prefix and, when a cooldown is open, how long it has left; a real
match keeps its own words. The cooldown is the real one: the prefix is seeded in
``_path_resolve_degraded`` with a pinned clock, no gate function is stubbed.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from kiro_crew import security
from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig
from kiro_crew.llm_helpers import _edit_target_denial
from kiro_crew.security import paths

_NOW = 1_000_000.0


def _open_cooldown(monkeypatch, target: str, seconds: float) -> str:
    """Open a real per-prefix cooldown over *target*; return the stalled prefix."""
    prefix = paths._stall_prefix(os.path.expanduser(target))
    monkeypatch.setattr(paths, "_path_resolve_clock", lambda: _NOW)
    monkeypatch.setitem(paths._path_resolve_degraded, prefix, (_NOW + seconds, 1))
    return prefix


def _target(tmp_path: Path) -> str:
    target = tmp_path / "notes" / "README.md"
    target.parent.mkdir(parents=True)
    target.write_text("hello", encoding="utf-8")
    return str(target)


def test_read_stall_still_refuses_and_names_the_prefix_and_the_wait(
    tmp_path: Path, monkeypatch
) -> None:
    target = _target(tmp_path)
    prefix = _open_cooldown(monkeypatch, target, 42.3)

    assert security.is_sensitive_path(target) is True  # still fail-closed
    reason = security.sensitive_path_refusal(target)
    assert reason is not None
    assert security.is_unverifiable_path_refusal(reason)
    assert "access to sensitive path" not in reason
    assert "check timed out" in reason
    assert repr(prefix) in reason
    assert "about 43 s more" in reason
    assert reason.endswith(f"Path: {target!r}")


def test_write_stall_still_refuses_and_is_worded_as_a_stall(tmp_path: Path, monkeypatch) -> None:
    target = _target(tmp_path)
    _open_cooldown(monkeypatch, target, 30.0)

    assert security.is_sensitive_write_path(target) is True  # still fail-closed
    reason = security.sensitive_write_path_refusal(target)
    assert reason is not None
    assert security.is_unverifiable_path_refusal(reason)
    assert "protected path" not in reason
    assert "about 30 s more" in reason


def test_write_match_keeps_its_wording(monkeypatch) -> None:
    monkeypatch.setattr(paths, "_path_in_home_dirs", lambda *a, **k: True)
    reason = security.sensitive_write_path_refusal("/x/config.json")
    assert reason == "Blocked: write to protected path: /x/config.json"
    assert not security.is_unverifiable_path_refusal(reason)


def test_clear_write_path_is_not_refused(tmp_path: Path) -> None:
    assert security.sensitive_write_path_refusal(_target(tmp_path)) is None


def test_stall_without_an_open_cooldown_says_a_short_wait(monkeypatch) -> None:
    # Pool saturated / load arm: refused, no cooldown opened, so no seconds to name.
    def stalled(*args, **kwargs):
        raise security.PathResolutionStalled("/w/x", "/w/x")

    monkeypatch.setattr(paths, "_path_in_home_dirs", stalled)
    reason = security.sensitive_path_refusal("/w/x/file")
    assert reason is not None and security.is_unverifiable_path_refusal(reason)
    assert "after a short wait" in reason
    assert " s more" not in reason


def test_hook_edit_named_only_by_the_diff_block_is_refused_as_a_stall(
    tmp_path: Path, monkeypatch
) -> None:
    target = _target(tmp_path)
    _open_cooldown(monkeypatch, target, 30.0)
    decision = HookManager(HooksConfig.from_dict({})).on_tool_call(
        "Editing the notes",
        session_key="cli_chat",
        tool_kind="edit",
        raw_params={"command": "create", "fileText": "x"},
        diff_path=target,
    )
    assert decision.action == TOOL_DENY  # still fail-closed
    assert security.is_unverifiable_path_refusal(decision.reason)
    assert "write-protected config path" not in decision.reason


def test_permission_card_edit_denial_is_worded_as_a_stall(tmp_path: Path, monkeypatch) -> None:
    target = _target(tmp_path)
    _open_cooldown(monkeypatch, target, 30.0)
    hit = _edit_target_denial({"path": target}, "", "edit")
    assert hit is not None  # still fail-closed
    kind, reason, subject = hit
    assert kind == "path" and subject == target
    assert security.is_unverifiable_path_refusal(reason)
    assert "protected path" not in reason


def test_permission_card_edit_match_keeps_its_wording() -> None:
    hit = _edit_target_denial({"path": "~/.kiro/crew/config.json"}, "", "edit")
    assert hit is not None
    assert hit[1].startswith("Blocked: write to protected path: ")


@pytest.mark.parametrize(("seconds", "shown"), [(0.2, 1), (5.0, 5), (5.1, 6)])
def test_retry_hint_rounds_up_to_a_whole_second(
    tmp_path: Path, monkeypatch, seconds: float, shown: int
) -> None:
    target = _target(tmp_path)
    _open_cooldown(monkeypatch, target, seconds)
    reason = security.sensitive_path_refusal(target)
    assert reason is not None and f"about {shown} s more" in reason


def test_an_uncharged_stall_does_not_blame_the_prefix(tmp_path: Path, monkeypatch) -> None:
    # The real wait-allowance arm of ``_run_resolution_bounded``: the calling thread
    # has spent its allowance, so the resolution is refused without probing and the
    # prefix is NOT charged. The refusal must not say the check timed out under it.
    target = _target(tmp_path)
    expanded = os.path.expanduser(target)
    prefix = paths._stall_prefix(expanded)
    monkeypatch.setattr(paths, "_path_resolve_clock", lambda: _NOW)
    monkeypatch.delitem(paths._path_resolve_degraded, prefix, raising=False)
    monkeypatch.setitem(
        paths._path_resolve_thread_waits,
        threading.get_ident(),
        (_NOW + 100.0, paths._PATH_RESOLVE_WAIT_CAP_SECS),
    )

    with pytest.raises(security.PathResolutionStalled) as raised:
        paths._resolved_forms_bounded(expanded)
    assert prefix not in paths._path_resolve_degraded  # nothing was charged
    reason = paths._stall_refusal(raised.value, target)

    assert security.is_unverifiable_path_refusal(reason)
    assert "did not complete in time" in reason
    assert "timed out under" not in reason
    assert repr(prefix) not in reason
    assert "after a short wait" in reason


def test_an_open_cooldown_names_the_prefix_it_charged(tmp_path: Path, monkeypatch) -> None:
    target = _target(tmp_path)
    prefix = _open_cooldown(monkeypatch, target, 10.0)
    reason = paths._stall_refusal(security.PathResolutionStalled(target, prefix), target)
    assert f"the check timed out under {prefix!r}" in reason
    assert "about 10 s more" in reason


def test_an_anchor_stall_does_not_say_only_paths_under_home_are_refused(monkeypatch) -> None:
    """A stalled anchor rebuild is charged to the home prefix but refuses a path
    outside it too, so the wait names the checks that touch the prefix."""
    home = os.path.expanduser("~")
    prefix = _open_cooldown(monkeypatch, home, 20.0)
    outside = os.path.join(os.path.dirname(prefix) or os.sep, "kc-elsewhere", "notes.md")
    reason = paths._stall_refusal(security.PathResolutionStalled(home, prefix), outside)
    assert f"the check timed out under {prefix!r}" in reason
    assert "Path checks that touch that prefix are refused for about 20 s more" in reason
    assert "Paths under that prefix" not in reason
    assert reason.endswith(f"Path: {outside!r}")
