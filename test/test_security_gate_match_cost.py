"""The sensitive-path gate's match cost and its anchor resolutions per decision.

The matchers look the candidate's own ancestors up in the target set instead of
scanning every target, and one decision resolves the anchor roots once for every
list it checks. Neither may change a verdict: the tests below compare the new
matchers with a reference linear scan over a generated corpus, and count the
root resolutions a decision makes.
"""

from __future__ import annotations

import os
import random
from types import SimpleNamespace

import pytest

from kiro_crew import security
from kiro_crew.security import paths, paths_targets

SEP = os.sep


def _old_under(cand_cf: str, targets: set[str]) -> bool:
    """A reference linear scan: the path is a target or lies under one."""
    return any(cand_cf == t or cand_cf.startswith(t + SEP) for t in targets)


def _old_contains(cand_cf: str, targets: set[str]) -> bool:
    """A reference linear scan: a target is the directory or lies under it (*cand_cf* stripped)."""
    return any(t == cand_cf or t.startswith(cand_cf + SEP) for t in targets)


def _new_contains(cand_cf: str, targets: set[str]) -> bool:
    return cand_cf in targets or cand_cf in paths_targets._target_ancestors(targets)


def _near(path: str) -> list[str]:
    """Inputs next to *path*: itself, children, prefix-sharing siblings, parents."""
    parent = path.rsplit(SEP, 1)[0] if SEP in path else ""
    leaf = path.rsplit(SEP, 1)[-1]
    return [
        path,
        path + SEP,
        path + SEP + SEP,
        path + SEP + "child",
        path + SEP + "a" + SEP + "b",
        path + "x",  # ~/.ssh vs ~/.sshx
        path + ".bak",
        path + "-dotfiles" + SEP + "k",
        path[:-1] if path else path,
        path.upper(),
        path.casefold(),
        parent,
        parent + SEP,
        parent + SEP + "sibling",
        parent + SEP + "." + SEP + leaf,
        parent + SEP + "n" + SEP + ".." + SEP + leaf,
        "",
        SEP,
    ]


def test_ancestor_lookup_gives_the_linear_scan_answer_for_every_input() -> None:
    rng = random.Random(39162)
    alphabet = ["a", "b", "A", ".ssh", ".sshx", ".", "..", "", "x y", "é"]
    targets: set[str] = {"", SEP, SEP + SEP, SEP + "a" + SEP}  # odd rows on purpose
    for _ in range(300):
        depth = rng.randint(1, 5)
        targets.add(SEP + SEP.join(rng.choice(alphabet) for _ in range(depth)))
    for drop in ("", SEP):  # with and without the root-like rows
        rows = {t for t in targets if t != drop}
        corpus = {c for t in rows for c in _near(t)}
        corpus |= {SEP + SEP.join(rng.choice(alphabet) for _ in range(6)) for _ in range(500)}
        assert len(corpus) > 3000
        for cand in sorted(corpus):
            cf = cand.casefold()
            assert paths_targets._has_target_ancestor(cf, rows) is _old_under(cf, rows), cand
            stripped = cf.rstrip(SEP)
            assert _new_contains(stripped, rows) is _old_contains(stripped, rows), cand


@pytest.fixture
def linked_home(tmp_path, monkeypatch):
    """``$HOME`` a symlink to another folder, and a symlinked ``KIROCREW_HOME``."""
    real = tmp_path / "real" / "home"
    (real / ".ssh").mkdir(parents=True)
    (real / ".aws").mkdir()
    (real / ".aws" / "credentials").write_text("x")
    (real / "proj").mkdir()
    (real / "proj" / "to_ssh").symlink_to(real / ".ssh")
    home = tmp_path / "home"
    home.symlink_to(real)
    crew_real = tmp_path / "real" / "crew"
    crew_real.mkdir()
    crew = tmp_path / "crew"
    crew.symlink_to(crew_real)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    monkeypatch.setattr(paths_targets, "_home_targets_cache", {})
    return home


def _gate_corpus(home) -> list[str]:
    targets = paths._home_dir_targets(
        paths._SENSITIVE_HOME_DIRS + paths._WRITE_PROTECTED_HOME_PATHS, inline=True
    )
    corpus = {c for t in targets for c in _near(t)}
    for rel in (
        ".ssh/id_rsa",
        ".sshx/id_rsa",
        ".SSH/id_rsa",
        "proj/to_ssh/id_rsa",
        "proj/../.aws/credentials",
        ".aws/credentialsx",
        "proj",
        "",
    ):
        corpus.add(os.path.join(str(home), rel))
        corpus.add(os.path.join(os.path.realpath(home), rel))
    return sorted(c for c in corpus if c)


def test_gate_matchers_agree_with_the_linear_scan_under_a_linked_home(linked_home) -> None:
    lists = (
        paths._SENSITIVE_HOME_DIRS,
        paths._SENSITIVE_HOME_DIRS + paths._WRITE_PROTECTED_HOME_PATHS,
    )
    corpus = _gate_corpus(linked_home)
    assert len(corpus) > 1000
    refused = 0
    for rows in lists:
        targets = paths._home_dir_targets(rows, inline=True)
        for i, path in enumerate(corpus):
            # The bounded spelling costs a resolver round trip per input, so it
            # runs on every 16th; the inline spelling runs on all of them.
            for pre_resolved in (True, False) if i % 16 == 0 else (True,):
                cands = paths._candidate_forms(path, pre_resolved=pre_resolved)
                old = any(_old_under(c.casefold(), targets) for c in cands)
                new = paths._path_in_home_dirs(path, rows, pre_resolved=pre_resolved)
                assert new is old, (path, pre_resolved)
                refused += old
    targets = paths._home_dir_targets(paths._SENSITIVE_HOME_DIRS, inline=True)
    for path in corpus:
        cands = paths._candidate_forms(path, pre_resolved=True)
        old = any(_old_contains(c.casefold().rstrip(SEP), targets) for c in cands)
        assert security.path_contains_sensitive(path, pre_resolved=True) is old, path
    assert refused, "the corpus never reached a target; the fixture home is wrong"


@pytest.fixture
def frozen_target_clock(monkeypatch):
    """Stop the target cache from expiring mid-test.

    A cache miss on the inline path resolves the roots a second time under its
    lock, so a slow worker crossing the expiry would change the counts below. The
    ``time`` is replaced through the ``paths`` facade, which writes every owner
    module that binds it, never the shared stdlib clock.
    """
    monkeypatch.setattr(paths, "time", SimpleNamespace(monotonic=lambda: 100.0))


def _count(monkeypatch, name: str) -> list[int]:
    calls: list[int] = []
    real = getattr(paths_targets, name)

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(paths_targets, name, counting)
    return calls


def test_one_inline_decision_resolves_the_roots_once_and_the_next_again(
    linked_home, frozen_target_clock, monkeypatch
) -> None:
    # A .tmp beside a keystone leaf is checked against the read list AND the
    # artifact parents: two lists, one root resolution. Warm the cache first so
    # the count is the decision's, not a cold build's re-resolution under its lock.
    artifact = os.path.join(os.path.realpath(linked_home), ".ssh", "x.tmp")
    security.is_sensitive_resolved_path(artifact)
    security.path_contains_sensitive(str(linked_home), pre_resolved=True)
    calls = _count(monkeypatch, "_resolve_root_anchors")
    security.is_sensitive_resolved_path(artifact)
    assert len(calls) == 1
    security.is_sensitive_resolved_path(artifact)
    assert len(calls) == 2, "a decision must resolve its roots fresh, not reuse the last"
    security.path_contains_sensitive(str(linked_home), pre_resolved=True)
    assert len(calls) == 3


def test_one_bounded_write_decision_resolves_the_roots_once(
    linked_home, frozen_target_clock, monkeypatch
) -> None:
    probe = os.path.join(str(linked_home), "proj", "notes.tmp")
    assert security.is_sensitive_write_path(probe) is False  # warm
    calls = _count(monkeypatch, "_resolved_root_key")
    assert security.is_sensitive_write_path(probe) is False
    assert len(calls) == 1
    assert security.is_sensitive_path(probe) is False
    assert len(calls) == 2
