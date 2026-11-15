"""Where the self-update engine stages a release wheel, and who can reach it.

The wheel's SHA-256 is checked as it lands, and pip opens the same path seconds later.
So the staging directory must be one no agent sandbox can write: masked as a whole
directory at OS level, fenced from the agent's file tools, and created before any
sandbox starts so the mask has a target. ``trust/`` is not such a place: it stays
sandbox read-write for the SEL log.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Iterator

import pytest

from kiro_crew import sandbox_leaves, sandbox_precreate, security
from kiro_crew.platform import wheel_engine, wheel_engine_transfer
from kiro_crew.platform.wheel_engine import ManagedVenvLayout
from kiro_crew.platform_compat import IS_POSIX

_LEAF = "update-staging"


@pytest.fixture
def crew_home(tmp_path: Path) -> Iterator[Path]:
    home = tmp_path / "crew"
    with pytest.MonkeyPatch.context() as mp:
        # Through the facade: its mirror carries the patch to the owner module.
        mp.setattr(wheel_engine, "data_home", lambda: home)
        yield home


def test_the_engine_and_the_sandbox_spell_the_leaf_the_same() -> None:
    assert (
        wheel_engine_transfer._UPDATE_STAGING_LEAF == sandbox_leaves._UPDATE_STAGING_LEAF == _LEAF
    )


def test_the_wheel_is_staged_in_a_top_level_leaf_not_under_trust(crew_home: Path) -> None:
    staging = wheel_engine_transfer._staging_dir()
    assert staging == crew_home / _LEAF
    assert staging.is_dir()
    assert "trust" not in staging.relative_to(crew_home).parts


def test_the_staging_leaf_is_masked_in_every_sandbox_and_never_sandbox_visible() -> None:
    assert _LEAF in sandbox_leaves._CREW_HIDDEN_LEAVES
    assert _LEAF not in sandbox_leaves._CREW_SANDBOX_VISIBLE_LEAVES
    assert _LEAF not in sandbox_leaves._CREW_READONLY_LEAVES


def test_the_staging_leaf_is_created_before_a_sandbox_starts() -> None:
    assert _LEAF in sandbox_precreate._CREW_PRECREATE_HIDDEN_DIR_LEAVES


def test_agent_file_tools_are_refused_a_staged_wheel() -> None:
    targets = security._home_dir_targets(
        tuple(d for d in security.sensitive_home_dirs() if d.endswith(_LEAF))
    )
    assert targets, "the file-tool fence names no update-staging directory"
    for target in sorted(targets):
        staged = os.path.join(target, "kirocrew-update-x", "kirocrew-1.0-py3-none-any.whl")
        assert security.is_sensitive_path(staged), target


@pytest.mark.skipif(not IS_POSIX, reason="the shadow-venv engine is POSIX-only")
def test_a_sweep_clears_a_stale_run_an_older_build_left_under_trust(
    crew_home: Path, tmp_path: Path
) -> None:
    legacy = crew_home / "trust" / "update-staging"
    stale = legacy / "kirocrew-update-old"
    stale.mkdir(parents=True)
    old = time.time() - wheel_engine._STAGING_STALE_SECS - 60
    os.utime(stale, (old, old))
    fresh = legacy / "kirocrew-update-new"
    fresh.mkdir()
    venv = tmp_path / "crew-venv"
    venv.mkdir()
    layout = ManagedVenvLayout(legacy=venv, stable_link=tmp_path / "crew-venv-current")

    wheel_engine._sweep_layout_debris(layout)

    assert not stale.exists()
    assert fresh.exists(), "a run younger than the stale window is kept"
