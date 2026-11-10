"""``_with_canonical_globs`` keeps the per-glob path gate off the resolver pool.

``GET /api/skills`` canonicalises every agent's ``skill://`` globs on each request.
An already-canonical prefix skips ``validate_file_path`` on POSIX, since the
canonical spelling adds nothing. Every other prefix (any prefix on Windows) is
gated on the canonical gate, which off the event loop answers inline instead of
with one ``mc-pathres`` round trip per glob. These tests pin the skip, the inline
gate, that the result and the sensitive-path refusal are unchanged, and that
nothing is cached: a prefix that turns into a link later is gated again on the
next call.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from kiro_crew import security
from kiro_crew import skills as sk
from kiro_crew.skill_runtime.catalog import _with_canonical_globs

needs_posix_symlinks = pytest.mark.skipif(
    not hasattr(os, "symlink") or os.name == "nt", reason="needs POSIX symlinks"
)


def _counting_gate(monkeypatch) -> list[str]:
    calls: list[str] = []
    real = sk.validate_file_path

    def gate(raw: str, **kwargs: bool) -> str | None:
        calls.append(raw)
        return real(raw, **kwargs)

    monkeypatch.setattr(sk, "validate_file_path", gate)
    return calls


def _pool_recorder(monkeypatch) -> list[str]:
    """Every path this thread hands to the ``mc-pathres`` pool, passed through to it."""
    seen: list[str] = []
    real = security.paths._run_resolution_bounded
    caller = threading.get_ident()

    def recording(expanded, *args, **kwargs):
        if threading.get_ident() == caller:
            seen.append(expanded)
        return real(expanded, *args, **kwargs)

    # Cold target cache: the gate's anchors must be rebuilt too, not served.
    monkeypatch.setattr(security.paths, "_home_targets_cache", {})
    monkeypatch.setattr(security.paths, "_run_resolution_bounded", recording)
    return seen


@needs_posix_symlinks
def test_canonical_prefix_does_not_take_the_gate(tmp_path, monkeypatch):
    root = Path(os.path.realpath(tmp_path)) / "skills"
    (root / "demo").mkdir(parents=True)
    glob = str(root / "demo" / "SKILL.md")
    wild = str(root / "*" / "SKILL.md")
    calls = _counting_gate(monkeypatch)

    assert _with_canonical_globs([glob, wild]) == [glob, wild]
    assert calls == []


@needs_posix_symlinks
def test_linked_prefix_still_takes_the_gate_and_gains_its_canonical_spelling(tmp_path, monkeypatch):
    real_root = Path(os.path.realpath(tmp_path)) / "data" / "skills"
    (real_root / "demo").mkdir(parents=True)
    link = tmp_path / "link-skills"
    link.symlink_to(real_root, target_is_directory=True)
    glob = str(link / "demo" / "SKILL.md")
    calls = _counting_gate(monkeypatch)

    assert _with_canonical_globs([glob]) == [glob, str(real_root / "demo" / "SKILL.md")]
    assert calls == [str(link / "demo" / "SKILL.md")]


@needs_posix_symlinks
def test_a_prefix_that_becomes_a_link_is_gated_on_the_next_call(tmp_path, monkeypatch):
    base = Path(os.path.realpath(tmp_path))
    spelled = base / "skills"
    (spelled / "demo").mkdir(parents=True)
    glob = str(spelled / "demo" / "SKILL.md")
    calls = _counting_gate(monkeypatch)
    assert _with_canonical_globs([glob]) == [glob]
    assert calls == []

    # Swap the directory for a link: no earlier answer may be reused.
    target = base / "moved"
    spelled.rename(target)
    spelled.symlink_to(target, target_is_directory=True)

    assert _with_canonical_globs([glob]) == [glob, str(target / "demo" / "SKILL.md")]
    assert len(calls) == 1


@needs_posix_symlinks
def test_a_link_into_a_sensitive_dir_is_still_refused(tmp_path, monkeypatch):
    base = Path(os.path.realpath(tmp_path))
    secret = base / "secret"
    secret.mkdir()
    link = base / "skills"
    link.symlink_to(secret, target_is_directory=True)
    glob = str(link / "*" / "SKILL.md")
    monkeypatch.setattr(sk, "validate_file_path", lambda raw, **_kw: None)

    assert _with_canonical_globs([glob]) == [glob]


def test_project_local_prefix_is_never_resolved(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    (project / ".kiro" / "skills").mkdir(parents=True)
    glob = str(project / ".kiro" / "skills" / "*" / "SKILL.md")
    resolved: list[str] = []
    real_realpath = os.path.realpath

    def spy(path, *args, **kwargs):
        resolved.append(str(path))
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", spy)

    assert _with_canonical_globs([glob], project_dir=project) == [glob]
    assert not any(p.startswith(str(project)) for p in resolved)


def test_a_non_canonical_prefix_is_gated_without_a_resolver_pool_round_trip(tmp_path, monkeypatch):
    root = Path(os.path.realpath(tmp_path)) / "skills"
    (root / "demo").mkdir(parents=True)
    (root / "other").mkdir()
    # A ``..`` spelling is not canonical on any platform, so it takes the gate everywhere.
    glob = str(root / "other" / ".." / "demo" / "SKILL.md")
    submitted = _pool_recorder(monkeypatch)

    assert _with_canonical_globs([glob]) == [glob, str(root / "demo" / "SKILL.md")]
    assert submitted == []


def test_a_non_canonical_prefix_into_a_sensitive_dir_is_still_refused(monkeypatch):
    # The crew home is a per-test tmp dir here, and its ``diag`` folder is on the fence.
    crew_home = Path(os.path.realpath(os.environ["KIROCREW_HOME"]))
    (crew_home / "diag" / "demo").mkdir(parents=True)
    (crew_home / "other").mkdir()
    glob = str(crew_home / "other" / ".." / "diag" / "*" / "SKILL.md")
    submitted = _pool_recorder(monkeypatch)

    assert _with_canonical_globs([glob]) == [glob]
    assert submitted == []
