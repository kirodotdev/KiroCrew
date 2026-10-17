"""Route tests: the STOP sentinel file.

The sentinel is written and cleared through a descriptor pinned to the verified
spec directory, never a path string, so a planted symlink or a swapped directory
cannot reach another spec's STOP; without directory pinning the helpers fail
closed and halt still stops the run.
"""

from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes, routes_source

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _never_touch_the_real_state,
    _redirect_state,
)

# ── symlink containment, state normalization, create pre-checks ──────────────
#
# One test group per finding, so a regression names the finding it re-opens.


def test_stop_sentinel_write_destroys_planted_symlink(tmp_path):
    """os.replace must swap the link itself, never write through it."""

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("original")
    os.symlink(victim, spec_dir / routes._STOP_FILE)

    assert routes._write_stop_sentinel(spec_dir) is True
    # The victim is untouched and STOP is now a real file.
    assert victim.read_text() == "original"
    assert not (spec_dir / routes._STOP_FILE).is_symlink()


# ── blocking sentinel write on the event loop ────────────────────────────────


def test_prepare_handoff_arms_under_the_same_lock_as_the_identity_check():
    """R80: the identity check and the sentinel arm must be ONE critical section.

    Correct ordering is not enough. With the check in its own ``with`` block, a
    same-name delete plus re-import landing after the lock is released still
    leaves the check passing for a spec that is gone while the arm lands on the
    replacement -- clearing a STOP the user's Pause had just written.

    Structural because the race is a thread interleaving: reproducing it by
    timing would be flaky, while the property that forbids it -- the arm sits
    INSIDE the locked block -- is exactly stated in the source."""

    src = inspect.getsource(routes._prepare_handoff)
    tree = ast.parse(src)
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef), "expected _prepare_handoff to be a sync def"

    locked_blocks = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.With)
        and any(
            isinstance(i.context_expr, ast.Name) and i.context_expr.id == "_INDEX_LOCK"
            for i in n.items
        )
    ]
    assert len(locked_blocks) == 1, "expected exactly one _INDEX_LOCK critical section"

    def calls_arm(node):
        return any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Name)
            and c.func.id == "_arm_stop_sentinel"
            for c in ast.walk(node)
        )

    assert calls_arm(locked_blocks[0]), (
        "_arm_stop_sentinel is outside the _INDEX_LOCK block, so a same-name "
        "re-import between the check and the arm can redirect it at a replacement"
    )
    # And nowhere else: an arm outside the section would reopen the window even
    # with one inside it.
    arms_outside = [
        n.name if hasattr(n, "name") else "<stmt>"
        for n in fn.body
        if n not in locked_blocks and calls_arm(n)
    ]
    assert not arms_outside, "a second _arm_stop_sentinel call sits outside the lock"


def test_stop_write_is_refused_for_a_replaced_spec(tmp_path, monkeypatch):
    """The write half of R80: creating a STOP is as destructive as removing one.

    A stale Stop must not halt the run belonging to a spec that took the same
    name after the original was deleted."""

    spec_dir = tmp_path / "wd"
    spec_dir.mkdir()
    # The index says this name now belongs to a DIFFERENT creation.
    monkeypatch.setattr(
        routes,
        "_load_index",
        lambda: {"s": {"spec_dir": str(spec_dir), "slot_key": "new-key"}},
    )

    assert routes._write_stop_sentinel_for_spec(spec_dir, "s", "stale-key") is False
    assert not (
        spec_dir / routes._STOP_FILE
    ).exists(), "a stale Stop wrote a STOP into the replacement's directory"
    # Ordinary case: the caller's captured key still matches, so it writes.
    assert routes._write_stop_sentinel_for_spec(spec_dir, "s", "new-key") is True
    assert (spec_dir / routes._STOP_FILE).exists()


def test_prepare_handoff_reports_tasks_and_clears_a_stale_sentinel(tmp_path):
    """Non-vacuous: the bundled helper really gates on tasks.md, removes a stale
    STOP file, and returns the resolved sentinel path."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    stale = spec_dir / routes._STOP_FILE
    stale.write_text("old")

    has_tasks, sentinel = routes._prepare_handoff(spec_dir)
    assert has_tasks is False
    assert not stale.exists(), "stale STOP sentinel survived; the new run stops immediately"
    assert sentinel.endswith(routes._STOP_FILE)

    (spec_dir / "tasks.md").write_text("- [ ] task")
    assert routes._prepare_handoff(spec_dir)[0] is True


# ── sentinel isolation; the index is arbitrated before the slot ──────────────


def test_symlinked_spec_dir_cannot_touch_another_specs_sentinel(tmp_path):
    """The reported attack: an agent replaces its own indexed spec directory with
    a symlink to a PAUSED spec's directory. The sentinel helpers operated on
    realpath(spec_dir), so the handoff endpoint would delete the OTHER spec's
    STOP file and resume a run the user had stopped."""

    paused = tmp_path / "paused-spec"
    paused.mkdir()
    stop = paused / routes._STOP_FILE
    stop.write_text("stopped by the user")

    attacker = tmp_path / "attacker-spec"
    os.symlink(paused, attacker)
    (paused / "tasks.md").write_text("- [ ] task")

    # Every sentinel operation must refuse the symlinked directory.
    assert routes._verified_spec_dir(attacker) is None
    assert routes._arm_stop_sentinel(attacker) == ""
    assert routes._write_stop_sentinel(attacker) is False
    routes._clear_stop_sentinel(attacker)
    assert stop.read_text() == "stopped by the user", "another spec's STOP file was deleted"

    # ...and the handoff must not report itself ready off a symlinked dir, even
    # though tasks.md resolves through the link.
    ready, sentinel = routes._prepare_handoff(attacker)
    assert ready is False and sentinel == ""


def test_verified_spec_dir_accepts_an_ordinary_directory(tmp_path):
    """Non-vacuous: the guard must not reject a normal spec directory, or the
    whole Stop/handoff path silently stops working."""
    spec_dir = Path(os.path.realpath(tmp_path)) / "spec"
    spec_dir.mkdir()
    assert routes._verified_spec_dir(spec_dir) == spec_dir
    assert routes._prepare_handoff(spec_dir) == (False, str(spec_dir / routes._STOP_FILE))
    (spec_dir / "tasks.md").write_text("- [ ] t")
    assert routes._prepare_handoff(spec_dir)[0] is True
    assert routes._write_stop_sentinel(spec_dir) is True


# ── the body is parsed first; sentinel clear is directory-pinned ─────────────


def test_sentinel_clear_is_pinned_to_the_verified_directory(tmp_path, monkeypatch):
    """The reported attack: the directory is verified, then the agent swaps it
    for a symlink, and a PATH-based unlink resolves through the replacement and
    deletes a STOP file outside the spec. The unlink is now relative to a pinned
    non-following descriptor, so it lands in the verified directory or nowhere."""
    real = Path(os.path.realpath(tmp_path))
    spec = real / "wd" / ".kiro" / "specs" / "s1"
    spec.mkdir(parents=True)
    (spec / routes._STOP_FILE).write_text("stop")

    victim = real / "other" / ".kiro" / "specs" / "s2"
    victim.mkdir(parents=True)
    victim_stop = victim / routes._STOP_FILE
    victim_stop.write_text("do not delete me")

    # Baseline: it clears its OWN sentinel.
    routes._clear_stop_sentinel(spec)
    assert not (spec / routes._STOP_FILE).exists()

    # Swap the verified directory for a link to the victim AFTER verification by
    # making _verified_spec_dir hand back a path whose last component is a link.
    link = real / "wd" / ".kiro" / "specs" / "linked"
    link.symlink_to(victim, target_is_directory=True)
    monkeypatch.setattr(routes, "_verified_spec_dir", lambda p: link)
    routes._clear_stop_sentinel(link)

    if routes._CAN_PIN_DIR:
        assert victim_stop.exists(), "the unlink followed a replaced directory"
    else:  # pragma: no cover - Windows fallback
        assert True


def test_sentinel_pin_capability_is_resolved_once():
    """Guard: the confinement must be decided from real platform capability, not
    a per-call guess, and the source must not fall back to a bare path unlink."""
    assert routes._CAN_PIN_DIR is (hasattr(os, "O_DIRECTORY") and os.unlink in os.supports_dir_fd)
    src = inspect.getsource(routes._clear_stop_sentinel)
    assert "dir_fd=dir_fd" in src
    assert "O_NOFOLLOW" in src


# ── sentinel writes are pinned to the verified directory ─────────────────────


def test_sentinel_write_is_pinned_to_the_verified_directory(tmp_path, monkeypatch):
    """Pinning the sentinel CLEAR to a directory descriptor is only half the fence: a
    WRITE that works through paths keeps the traversal open. An agent that swaps its
    verified directory for a symlink between the check and the open redirects both the
    temp create and the rename, so ANOTHER active spec receives the STOP file and
    halts."""
    real = Path(os.path.realpath(tmp_path))
    mine = real / "wd" / ".kiro" / "specs" / "mine"
    mine.mkdir(parents=True)

    victim = real / "other" / ".kiro" / "specs" / "victim"
    victim.mkdir(parents=True)

    # Baseline: it writes its own sentinel.
    assert routes._write_stop_sentinel(mine) is True
    assert (mine / routes._STOP_FILE).is_file()

    # Now the verified path's last component is a link to the victim -- the swap
    # this finding describes.
    link = real / "wd" / ".kiro" / "specs" / "swapped"
    link.symlink_to(victim, target_is_directory=True)
    monkeypatch.setattr(routes, "_verified_spec_dir", lambda p: link)
    routes._write_stop_sentinel(link)

    if routes._CAN_PIN_DIR:
        assert not (
            victim / routes._STOP_FILE
        ).exists(), "the sentinel write followed a replaced directory and halted another spec"
    else:  # pragma: no cover - Windows fallback
        assert True


def test_sentinel_write_leaves_no_temp_behind_on_failure(tmp_path, monkeypatch):
    """A failed rename must not strand the temp file in the spec directory."""
    real = Path(os.path.realpath(tmp_path))
    spec = real / "wd" / ".kiro" / "specs" / "s"
    spec.mkdir(parents=True)
    monkeypatch.setattr(routes, "_verified_spec_dir", lambda p: spec)

    def _boom(*_a, **_k):
        raise OSError("rename failed")

    monkeypatch.setattr(os, "replace", _boom)
    assert routes._write_stop_sentinel(spec) is False
    assert list(spec.iterdir()) == [], f"left {[p.name for p in spec.iterdir()]}"


def test_both_sentinel_helpers_pin_the_directory():
    """Source guard: write and clear must BOTH operate relative to a pinned
    descriptor, and the capability probe must cover every op they use."""
    for fn in (routes._write_stop_sentinel, routes._clear_stop_sentinel):
        src = inspect.getsource(fn)
        assert "O_DIRECTORY" in src and "dir_fd" in src, fn.__name__
    # The probe spans several lines, so slice to the closing paren of the
    # expression rather than the first ")" inside it.
    src = routes_source()
    probe = src.split("_CAN_PIN_DIR = ", 1)[1].split("\n)", 1)[0]
    for op in ("os.open", "os.unlink", "os.rename"):
        assert op in probe, f"{op} is not covered by the pin capability probe"


# ── sentinel helpers fail closed without directory pinning ───────────────────


def test_sentinel_helpers_fail_closed_without_directory_pinning(tmp_path, monkeypatch):
    """The reported vector: without dir_fd pinning the fallback resolved the sentinel
    BY PATH, so an agent that swapped its own spec directory for a junction between
    the verification and the write landed STOP in another spec -- halting an unrelated
    run. The clear path had the same shape, and could delete another spec's STOP file,
    letting that run resume. Both now do nothing at all."""
    spec_dir = tmp_path / "p" / ".kiro" / "specs" / "pinned"
    spec_dir.mkdir(parents=True)
    monkeypatch.setattr(routes, "_CAN_PIN_DIR", False)

    assert routes._write_stop_sentinel(spec_dir) is False, "wrote without pinning"
    assert not (spec_dir / routes._STOP_FILE).exists(), "a STOP file was created by path"
    assert list(spec_dir.iterdir()) == [], f"left a temp file behind: {list(spec_dir.iterdir())}"

    # A pre-existing sentinel is LEFT ALONE rather than unlinked through a path that
    # could have been redirected.
    (spec_dir / routes._STOP_FILE).write_text("1")
    routes._clear_stop_sentinel(spec_dir)
    assert (spec_dir / routes._STOP_FILE).exists(), "cleared by path without pinning"

    # With pinning the same calls still work, so the guard is conditional, not a
    # blanket disable.
    monkeypatch.setattr(routes, "_CAN_PIN_DIR", True)
    routes._clear_stop_sentinel(spec_dir)
    assert not (spec_dir / routes._STOP_FILE).exists(), "pinned clear did nothing"
    assert routes._write_stop_sentinel(spec_dir) is True
    assert (spec_dir / routes._STOP_FILE).is_file()


def test_no_sentinel_path_is_resolved_by_string():
    """Source guard: every sentinel filesystem call is relative to a pinned
    descriptor. Asserting on the CALLS, not on the comment: a `dir_fd=` keyword on
    each mutating call is the property that makes the swap unreachable."""
    for fn in (routes._write_stop_sentinel, routes._clear_stop_sentinel):
        src = inspect.getsource(fn)
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "_STOP_FILE" not in stripped:
                continue
            if "os.open(" in stripped or "os.unlink(" in stripped or "os.replace(" in stripped:
                assert (
                    "dir_fd" in stripped
                ), f"{fn.__name__} touches the sentinel by path: {stripped}"
        # And no path arithmetic builds a sentinel target any more.
        assert "real_dir / _STOP_FILE" not in src, f"{fn.__name__} still joins the path"


@pytest.mark.asyncio
async def test_halt_still_stops_the_run_without_a_sentinel(tmp_path, monkeypatch):
    """Failing closed must not weaken Pause: the loop removal and the turn cancel are
    the authoritative stops, and both have to happen even when no sentinel could be
    written."""
    _redirect_state(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "_write_stop_sentinel", lambda _d: False)
    removed: list[str] = []
    halted: list[str] = []

    async def _remove(name, **kw):
        removed.append(name)

    async def _halt(state, name, **kw):
        halted.append(name)
        return True

    monkeypatch.setattr(routes, "_remove_nudge_loop", _remove)
    monkeypatch.setattr(routes, "_halt_active_turn", _halt)

    await routes._halt_execution(
        None, "quiet", tmp_path / "p" / ".kiro" / "specs" / "quiet", reason="user stop"
    )
    assert removed == ["quiet"], "the nudge loop was not removed"
    assert halted == ["quiet"], "the in-flight turn was not cancelled"


# ── a stale execute must not clear a replacement's STOP sentinel ──


def _paused_spec(tmp_path, slot_key):
    """A spec whose Pause has been persisted: tasks.md present, STOP on disk."""
    spec_dir = tmp_path / "proj" / ".kiro" / "specs" / "paused"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "tasks.md").write_text("- [ ] task")
    stop = spec_dir / routes._STOP_FILE
    stop.write_text("stop")
    routes._save_index(
        {
            "paused": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "proj"),
                "slot_key": slot_key,
            }
        }
    )
    return spec_dir, stop


def test_prepare_handoff_refuses_to_clear_when_the_identity_moved(tmp_path, monkeypatch):
    """The clear is destructive, so it is gated on identity, not merely ordered.

    Arming removes the STOP a Pause wrote. A stale same-name, same-path execute
    carrying NO client claim cannot be refused by comparing claims, so the act
    itself has to check: with the index now on a different creation, the sentinel
    must survive.
    """
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path / "state")
    spec_dir, stop = _paused_spec(tmp_path, slot_key="spec-builder-paused-replacement")

    ready, sentinel = routes._prepare_handoff(spec_dir, "paused", "spec-builder-paused-stale")

    assert ready is False, "a stale identity was allowed to arm the run"
    assert stop.exists(), "the replacement's STOP sentinel was cleared by a stale execute"


def test_prepare_handoff_still_clears_for_the_matching_identity(tmp_path, monkeypatch):
    """The gate must not break the ordinary path it guards."""
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path / "state")
    spec_dir, stop = _paused_spec(tmp_path, slot_key="spec-builder-paused-abc")

    ready, sentinel = routes._prepare_handoff(spec_dir, "paused", "spec-builder-paused-abc")

    assert ready is True, "the matching identity was refused"
    assert not stop.exists(), "the stale STOP was not cleared for the current creation"
    assert sentinel == str(spec_dir / routes._STOP_FILE)


def test_prepare_handoff_unpinned_call_keeps_working(tmp_path, monkeypatch):
    """Specs predating per-creation keys carry no slot_key, so the gate is skipped
    rather than turning every legacy handoff into a refusal."""
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path / "state")
    spec_dir, stop = _paused_spec(tmp_path, slot_key="")

    assert routes._prepare_handoff(spec_dir, "paused", "")[0] is True
    assert not stop.exists()
