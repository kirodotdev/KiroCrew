"""A skill import rides out a Windows hold on its freshly staged folder.

On Windows a folder rename fails with ``PermissionError`` (WinError 5) while any
handle is open on a file inside it, and an indexer or AV scanner opens freshly
written files. The three folder renames of a skill import (install, move-aside,
restore) go through ``atomic_write.replace_with_retry``, which retries that window.

This host cannot hold a Windows handle, so ``platform_compat.IS_WINDOWS`` is faked and
``os.replace`` raises the sharing violation a set number of times for one rename.
The retry's sleep is a recorder, so no test waits on the wall clock.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Callable

import pytest
from onboarding_import_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _register_predecessor_source,
)

from kiro_crew import atomic_write as aw
from kiro_crew import onboarding_import, platform_compat

pytestmark = pytest.mark.usefixtures("isolate_mcp_host_paths")

_Match = Callable[[Path, Path], bool]


def _installing(src: Path, _dst: Path) -> bool:
    return src.name.startswith(".review.import-")


def _moving_aside(_src: Path, dst: Path) -> bool:
    return dst.name.startswith(".review.replaced-")


def _restoring(src: Path, _dst: Path) -> bool:
    return src.name.startswith(".review.replaced-")


def _windows_hold(
    monkeypatch: pytest.MonkeyPatch, holds: dict[_Match, int]
) -> tuple[list[float], dict[_Match, int]]:
    """Fake Windows, and fail each matched rename its count of times (-1: always).

    Returns the recorded retry sleeps and the attempts made per matcher.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    sleeps: list[float] = []
    monkeypatch.setattr(aw, "time", SimpleNamespace(sleep=sleeps.append, monotonic=time.monotonic))
    real = os.replace
    attempts = {match: 0 for match in holds}

    def _replace(src: object, dst: object) -> None:
        for match, count in holds.items():
            if match(Path(str(src)), Path(str(dst))):
                attempts[match] += 1
                if count < 0 or attempts[match] <= count:
                    raise PermissionError(13, "Access is denied", str(src), 5)
        real(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", _replace)
    return sleeps, attempts


def _codex_skill(tmp_path: Path, text: str) -> Path:
    skill = tmp_path / "home" / ".codex" / "skills" / "review"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(text, encoding="utf-8")
    return tmp_path / "home"


def _import(api: ModuleType, home: Path, data_home: Path, strategy: str = "skip") -> dict:
    return api.apply_import(
        api.preview_import(home=home, env={}), data_home=data_home, conflict_strategy=strategy
    )


def _skills_root(data_home: Path) -> Path:
    return data_home / "skills" / "imported" / "codex"


def _leftovers(data_home: Path) -> list[str]:
    return sorted(p.name for p in _skills_root(data_home).iterdir() if p.name.startswith("."))


def test_fresh_import_released_within_the_budget_installs_and_leaves_no_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _codex_skill(tmp_path, "# New\n")
    data_home = tmp_path / "crew"
    sleeps, attempts = _windows_hold(monkeypatch, {_installing: 2})

    result = _import(onboarding_import, home, data_home)

    assert result["imported"]["skills"] == 1
    assert attempts[_installing] == 3
    assert len(sleeps) == 2
    assert (_skills_root(data_home) / "review" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# New\n"
    assert _leftovers(data_home) == []


@pytest.mark.parametrize("held", [_installing, _moving_aside], ids=["install", "move-aside"])
def test_overwrite_released_within_the_budget_installs_and_drops_the_retired_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held: _Match
) -> None:
    home = _codex_skill(tmp_path, "# Old\n")
    data_home = tmp_path / "crew"
    assert _import(onboarding_import, home, data_home)["imported"]["skills"] == 1
    _codex_skill(tmp_path, "# New\n")
    sleeps, attempts = _windows_hold(monkeypatch, {held: 2})

    result = _import(onboarding_import, home, data_home, "overwrite")

    assert result["imported"]["skills"] == 1
    assert attempts[held] == 3
    assert len(sleeps) == 2
    assert (_skills_root(data_home) / "review" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# New\n"
    assert _leftovers(data_home) == []


def test_overwrite_held_past_the_budget_puts_the_original_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _codex_skill(tmp_path, "# Old\n")
    data_home = tmp_path / "crew"
    assert _import(onboarding_import, home, data_home)["imported"]["skills"] == 1
    _codex_skill(tmp_path, "# New\n")
    # The install never gets through; the restore meets a brief hold of its own.
    _sleeps, attempts = _windows_hold(monkeypatch, {_installing: -1, _restoring: 2})

    result = _import(onboarding_import, home, data_home, "overwrite")

    assert result["imported"]["skills"] == 0
    assert attempts[_installing] == aw._REPLACE_MAX_ATTEMPTS
    assert (_skills_root(data_home) / "review" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# Old\n"
    assert not any(name.startswith(".review.replaced-") for name in _leftovers(data_home))
