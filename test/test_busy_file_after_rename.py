"""A file another process holds open at the final rename is busy, not refused.

On Windows a rename onto a file that another handle has open fails. Two
writers treat that as a busy file:

* the descriptor-pinned replace (``hooks.verified_replace_file_nolink``) answers
  ``"busy"``, which is not the ``"refused"`` kept for a hardlink, a scope escape or
  an ACL it cannot carry (the prompt route's 503 is pinned in
  ``test_prompts_authoring.py``);
* the fleet probe's stdlib-only ``_atomic_write`` retries the rename.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.hooks import verified_replace_file_nolink

# ── the descriptor-pinned replace ──


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _denied(winerror: int) -> PermissionError:
    exc = PermissionError(13, "Access is denied")
    exc.winerror = winerror  # set by CPython on Windows only
    return exc


@pytest.fixture
def by_name_rename(_floor_monkeypatch, monkeypatch):
    """The by-name rename branch Windows always takes, with that rename denied.

    Set ``winerror[0]`` to the Windows error code the final rename fails with.
    """
    _floor_monkeypatch.setattr(os, "supports_dir_fd", set())
    real_replace = os.replace
    winerror = [32]

    def refuse_final_rename(src, dst, *args, **kwargs):
        # Only the staged sibling onto the target: the staging is untouched.
        if Path(dst).name == "p.md" and Path(src).name != "p.md":
            raise _denied(winerror[0])
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", refuse_final_rename)
    return winerror


@pytest.mark.parametrize("winerror", [32, 33], ids=["sharing", "lock"])
def test_a_held_file_on_windows_is_busy_and_untouched(
    tmp_path, monkeypatch, by_name_rename, winerror
):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    by_name_rename[0] = winerror
    target = tmp_path / "p.md"
    target.write_text("BEFORE", encoding="utf-8")

    outcome = verified_replace_file_nolink(
        str(target), "AFTER", _sha("BEFORE"), max_bytes=1_000_000
    )

    assert outcome == "busy"
    assert target.read_text(encoding="utf-8") == "BEFORE"
    # The staged sibling is cleaned up like any other failed replace.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["p.md"]


def test_any_other_windows_denial_stays_refused(tmp_path, monkeypatch, by_name_rename):
    """A directory ACL that denies the rename is persistent: retrying cannot help."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    by_name_rename[0] = 5  # ERROR_ACCESS_DENIED
    target = tmp_path / "p.md"
    target.write_text("BEFORE", encoding="utf-8")

    outcome = verified_replace_file_nolink(
        str(target), "AFTER", _sha("BEFORE"), max_bytes=1_000_000
    )

    assert outcome == "refused"


def test_the_same_error_on_posix_stays_refused(tmp_path, monkeypatch, by_name_rename):
    """On POSIX an open handle never blocks a rename, so the error is genuine."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    target = tmp_path / "p.md"
    target.write_text("BEFORE", encoding="utf-8")

    outcome = verified_replace_file_nolink(
        str(target), "AFTER", _sha("BEFORE"), max_bytes=1_000_000
    )

    assert outcome == "refused"


# ── the fleet probe's stdlib-only writer ──


def _load_fleet_probe_sessions():
    path = (
        Path(__file__).resolve().parent.parent
        / "src/kiro_crew/builtin_skills/pipeline-conductor/scripts/fleet_probe_sessions.py"
    )
    spec = importlib.util.spec_from_file_location("_fleet_probe_sessions_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_fleet_probe_writer_retries_a_held_file_on_windows(tmp_path, monkeypatch):
    mod = _load_fleet_probe_sessions()
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")
    real_replace = os.replace
    refusals = [2]

    def held_twice(src, dst, *args, **kwargs):
        if refusals[0]:
            refusals[0] -= 1
            raise PermissionError(32, "The process cannot access the file")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(mod.os, "replace", held_twice)
    monkeypatch.setattr(mod, "_ON_WINDOWS", True, raising=False)
    # The module's own pause, not the shared ``time.sleep`` every thread uses.
    monkeypatch.setattr(mod, "_REPLACE_BACKOFF_SECONDS", 0, raising=False)

    try:
        mod._atomic_write(target, "new")
    except PermissionError as exc:
        pytest.fail(f"the writer let a held file raise: {exc}")

    assert target.read_text(encoding="utf-8") == "new"
    assert refusals == [0]
