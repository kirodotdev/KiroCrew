"""The read floor resolves a path once, in its own thread, and the gate matches that.

``screen_file_path`` and ``safe_read_file`` run ``os.path.realpath`` themselves and
then ask the sensitive-path gate about the result. Inside
:func:`security.caller_canonicalised` the gate matches that exact spelling as given,
so no second resolution of it reaches the ``mc-pathres`` pool, while the anchors are
still resolved on the pool and a stall there still refuses. What must not change is
pinned beside it: a link into a credential store is refused through the floor, and
the mark covers only the one string the caller resolved.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

import kiro_crew.executors as ex
from conftest import requires_symlinks
from kiro_crew import hooks, security
from kiro_crew.security import paths


@pytest.fixture(autouse=True)
def _fresh_gate_state(_floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Empty stall books and a home under *tmp_path*."""
    _floor_monkeypatch.setattr(paths, "_path_resolve_degraded", {})
    _floor_monkeypatch.setattr(paths, "_path_resolve_load_probes", {})
    _floor_monkeypatch.setattr(paths, "_path_resolve_thread_waits", {})
    home = tmp_path / "home"
    home.mkdir()
    _floor_monkeypatch.setenv("HOME", str(home))
    _floor_monkeypatch.setenv("USERPROFILE", str(home))
    security._home_targets_cache.clear()
    yield
    security._home_targets_cache.clear()
    ex.shutdown_maintenance_executor()


@pytest.fixture
def candidate_hops(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every candidate the gate sends to the pool, answered here with its realpath."""
    asked: list[str] = []

    def recording(expanded: str) -> set[str]:
        asked.append(expanded)
        return {os.path.realpath(expanded)}

    # Through the facade, which writes every owner that binds the name.
    monkeypatch.setattr(paths, "_resolved_forms_bounded", recording)
    return asked


def _plain_file(tmp_path: Path) -> Path:
    target = tmp_path / "ws" / "notes.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hello", encoding="utf-8")
    return target


def _sensitive_link(tmp_path: Path) -> Path:
    key = tmp_path / "home" / ".ssh" / "id_rsa"
    key.parent.mkdir(parents=True)
    key.write_text("not a key", encoding="utf-8")
    link = tmp_path / "ws" / "link.md"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(key)
    return link


def test_plain_file_is_read_without_a_second_resolution(tmp_path, candidate_hops) -> None:
    target = _plain_file(tmp_path)
    assert hooks.safe_read_file(str(target)) == "hello"
    assert hooks.validate_file_path(str(target)) == os.path.realpath(target)
    assert candidate_hops == []


def test_an_opened_descriptor_in_a_credential_store_is_still_refused(
    tmp_path, monkeypatch, candidate_hops
) -> None:
    target = _plain_file(tmp_path)
    key = tmp_path / "home" / ".ssh" / "id_rsa"
    key.parent.mkdir(parents=True)
    key.write_text("not a key", encoding="utf-8")
    # The identity check sees the file that was validated, the kernel-path check after
    # it names the key: the marked kernel path is still matched against the anchors.
    answers = iter([os.path.realpath(target)])
    flagged = os.path.realpath(key)
    monkeypatch.setattr(hooks, "_fd_real_path", lambda _fd: next(answers, flagged))
    with pytest.raises(PermissionError, match="^Blocked: opened sensitive path"):
        hooks.safe_read_file(str(target))
    assert candidate_hops == []


@requires_symlinks
def test_link_into_a_credential_store_is_still_refused(tmp_path, candidate_hops) -> None:
    link = _sensitive_link(tmp_path)
    with pytest.raises(PermissionError, match="^Blocked: access to sensitive path"):
        hooks.safe_read_file(str(link))
    assert hooks.validate_file_path(str(link)) is None
    assert hooks.screen_file_path(str(link)) is hooks.PathDecline.REFUSED
    assert candidate_hops == []


@requires_symlinks
def test_only_the_marked_spelling_skips_resolution(tmp_path, candidate_hops) -> None:
    plain = os.path.realpath(_plain_file(tmp_path))
    link = str(_sensitive_link(tmp_path))
    with security.caller_canonicalised(plain):
        assert security.is_sensitive_path(link)
    assert candidate_hops == [link]
    assert not security.is_sensitive_path(plain)
    assert set(candidate_hops) == {link, plain}


def test_a_relative_spelling_is_never_marked(tmp_path, monkeypatch, candidate_hops) -> None:
    monkeypatch.chdir(_plain_file(tmp_path).parent)
    with security.caller_canonicalised("notes.md"):
        assert not security.is_sensitive_path("notes.md")
    assert set(candidate_hops) == {"notes.md"}


def test_a_stalled_anchor_still_refuses_a_marked_path(tmp_path, monkeypatch) -> None:
    target = _plain_file(tmp_path)
    resolved = os.path.realpath(target)
    asked_inline: list[bool] = []

    def stalled(home_dirs: list[str], *, inline: bool = False) -> set[str]:
        asked_inline.append(inline)
        raise security.PathResolutionStalled(resolved, os.path.dirname(resolved))

    monkeypatch.setattr(paths, "_home_dir_targets", stalled)
    with pytest.raises(PermissionError) as refused:
        hooks.safe_read_file(str(target))
    assert security.is_unverifiable_path_refusal(str(refused.value))
    assert hooks.screen_file_path(str(target)) is hooks.PathDecline.UNCHECKED
    assert asked_inline and not any(asked_inline)


def test_only_the_read_floor_marks_a_spelling() -> None:
    # A marked spelling skips link resolution, so each site that marks one must have
    # resolved it on its own thread first. A new site has to be added here on review.
    src = Path(hooks.__file__).resolve().parent
    callers = sorted(
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if "caller_canonicalised(" in path.read_text(encoding="utf-8", errors="replace")
    )
    assert callers == [
        "hook_runtime/descriptor_identity.py",
        "hook_runtime/safe_reads.py",
        "security/paths_targets.py",
    ]
