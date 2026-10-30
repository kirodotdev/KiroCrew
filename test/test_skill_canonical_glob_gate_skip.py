"""``_with_canonical_globs`` skips the pooled path gate for an already-canonical prefix.

``GET /api/skills`` canonicalises every agent's ``skill://`` globs on each request.
Each glob went through ``validate_file_path``, whose sensitive-path check is a
pooled round trip, even when the prefix resolves to itself and the canonical
spelling therefore adds nothing. These tests pin the skip, that the result is
unchanged, and that nothing is cached: a prefix that turns into a link later is
gated again on the next call.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew import skills as sk
from kiro_crew.skill_runtime.catalog import _with_canonical_globs

needs_posix_symlinks = pytest.mark.skipif(
    not hasattr(os, "symlink") or os.name == "nt", reason="needs POSIX symlinks"
)


def _counting_gate(monkeypatch) -> list[str]:
    calls: list[str] = []
    real = sk.validate_file_path

    def gate(raw: str) -> str | None:
        calls.append(raw)
        return real(raw)

    monkeypatch.setattr(sk, "validate_file_path", gate)
    return calls


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
    monkeypatch.setattr(sk, "validate_file_path", lambda raw: None)

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
