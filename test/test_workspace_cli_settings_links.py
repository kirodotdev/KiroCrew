"""A ``.kiro`` link must not carry the workspace overlay into kiro-cli's own settings.

``<kiro home>/settings/cli.json`` holds the user's own kiro-cli defaults. A work dir
whose ``.kiro`` or ``.kiro/settings`` is a link into that folder would otherwise have
every overlay writer edit the global file. Any other link, such as a ``.kiro`` shared
across git worktrees, is still followed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.providers.acp import _clear_cli_overlay_effort, _write_cli_overlay

_MODEL = "claude-opus-4.7"


def _tree(root: Path) -> dict[str, bytes | None]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes() if p.is_file() else None
        for p in root.rglob("*")
    }


@pytest.mark.parametrize(
    ("link_name", "target_name", "home_has_settings"),
    [(".kiro", "", True), (".kiro", "", False), (".kiro/settings", "settings", True)],
)
def test_a_link_into_the_kiro_home_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_name: str,
    target_name: str,
    home_has_settings: bool,
) -> None:
    home = tmp_path / "fakehome" / ".kiro"
    home.mkdir(parents=True)
    if home_has_settings:
        own = {"chat.modelDefaults": {_MODEL: {"output_config": {"effort": "low"}}}}
        (home / "settings").mkdir()
        (home / "settings" / "cli.json").write_text(json.dumps(own), encoding="utf-8")
    monkeypatch.setenv("KIRO_HOME", str(home))
    link = tmp_path / "work" / link_name
    link.parent.mkdir(parents=True)
    platform_compat.symlink_or_junction(home / target_name, link)
    before = _tree(home)

    with pytest.raises(OSError, match="global kiro settings"):
        _write_cli_overlay(tmp_path / "work", _MODEL, "high")
    assert _clear_cli_overlay_effort(tmp_path / "work", _MODEL) is False

    assert _tree(home) == before


def test_a_kiro_shared_across_worktrees_is_still_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "fakehome" / ".kiro"))
    shared = tmp_path / "shared" / ".kiro"
    shared.mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    platform_compat.symlink_or_junction(shared, work / ".kiro")

    _write_cli_overlay(work, _MODEL, "high")

    written = json.loads((shared / "settings" / "cli.json").read_text(encoding="utf-8"))
    assert written["chat.modelDefaults"][_MODEL]["output_config"]["effort"] == "high"
