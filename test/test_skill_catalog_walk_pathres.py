"""The skills catalog walk judges each row on the canonical sensitive-path gate.

``_iter_uncached`` resolves every row it walks through ``validate_file_path``:
once for the dedupe set of the skills dir, and once per row of each extra path.
On the default gate each call resolved the path again on the two-worker
``mc-pathres`` pool, so one walk of a few dozen skills queued well over a hundred
requests ahead of the event loop's own checks. The walk runs on a worker thread
and passes ``canonical_gate=True``, which judges the ``realpath`` it computed
inline off the loop and keeps the bounded gate on it. These tests pin that by
counting pool submissions, and pin that a fenced link is still dropped.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import security
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.skill_runtime import catalog
from kiro_crew.skills import SkillsLoader

_SKILLS = 6


def _skill(root: Path, name: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n", encoding="utf-8")
    return path


@pytest.fixture
def pool_submissions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every path handed to the ``mc-pathres`` pool, passed through to it."""
    calls: list[str] = []
    real = security.paths._run_resolution_bounded

    def counting(expanded, *args, **kwargs):
        calls.append(expanded)
        return real(expanded, *args, **kwargs)

    monkeypatch.setattr(security.paths, "_run_resolution_bounded", counting)
    # A cold anchor cache, so an anchor rebuild through the pool would count too.
    monkeypatch.setattr(security.paths, "_home_targets_cache", {})
    return calls


@pytest.fixture
def loader(tmp_path: Path, opened) -> SkillsLoader:
    """A skills dir and one extra path, each holding a few skills."""
    base = Path(os.path.realpath(tmp_path))
    for i in range(_SKILLS):
        _skill(base / "skills", f"own-{i}")
        _skill(base / "extra", f"extra-{i}")
    cfg = KiroCrewConfig()
    cfg.skills.extra_paths = [str(base / "extra")]
    return opened(SkillsLoader(skills_path=base / "skills", install_builtins=False, config=cfg))


def _names(rows) -> set[str]:
    return {name for name, _path, _root in rows}


class TestTheWalkStaysOffThePool:
    def test_control_the_default_gate_does_submit(self, tmp_path, pool_submissions) -> None:
        # If this stopped holding, the zero counts below would prove nothing.
        security.is_sensitive_path(os.path.realpath(tmp_path / "x.md"))
        assert pool_submissions

    def test_a_walk_submits_nothing_to_the_resolver_pool(self, loader, pool_submissions) -> None:
        rows = catalog._iter_uncached(loader)

        assert len(rows) == 2 * _SKILLS
        assert pool_submissions == []

    @pytest.mark.asyncio
    async def test_a_walk_on_a_worker_while_the_loop_runs(self, loader, pool_submissions) -> None:
        # The real shape: the refresh worker walks while a loop is live on
        # another thread. The gate decides by the CALLING thread.
        rows = await asyncio.to_thread(catalog._iter_uncached, loader)

        assert len(rows) == 2 * _SKILLS
        assert pool_submissions == []

    @pytest.mark.asyncio
    async def test_on_the_event_loop_the_walk_keeps_the_bounded_gate(
        self, loader, pool_submissions, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The inline gate resolves its anchors on the calling thread, which on
        # the loop is the blocking call the pool exists to keep off it.
        monkeypatch.setattr(
            security.paths,
            "is_sensitive_resolved_path",
            lambda *a, **k: pytest.fail("the inline gate ran on the event loop"),
        )

        rows = catalog._iter_uncached(loader)

        assert len(rows) == 2 * _SKILLS
        assert pool_submissions, "the walk on the loop never reached the bounded gate"


class TestAFencedLinkIsStillDropped:
    @requires_symlinks
    def test_an_extra_path_link_to_a_crew_secret_is_not_listed(
        self, loader, pool_submissions
    ) -> None:
        crew_home = Path(os.environ["KIROCREW_HOME"])
        crew_home.mkdir(parents=True, exist_ok=True)
        secret = crew_home / ".env"
        secret.write_text("TOKEN=leaked\n", encoding="utf-8")
        leak = Path(loader._extra_paths[0]) / "leak"
        leak.mkdir()
        (leak / "SKILL.md").symlink_to(secret)

        names = _names(catalog._iter_uncached(loader))

        assert "leak" not in names
        assert {f"extra-{i}" for i in range(_SKILLS)} <= names
        assert pool_submissions == []
