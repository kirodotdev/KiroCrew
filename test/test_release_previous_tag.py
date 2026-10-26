"""The generated release notes compare against a base this repository chooses.

GitHub's own choice is the newest release tag that is an ancestor of the new
one. Releases are tagged on per-version release branches, so that choice lands
on a tag from the project's first weeks and every page lists thousands of old
pull requests. ``scripts/release_previous_tag.py`` picks the base by lineage
instead, and the release step passes it as ``previous_tag``.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from kiro_crew.subprocess_utf8 import UTF8_TEXT

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_previous_tag.py"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"


def previous_tag(current: str, tags: list[str]) -> str:
    """Call the script's own selection function, loaded from its path."""
    assert SCRIPT.is_file(), f"{SCRIPT} is missing"
    spec = importlib.util.spec_from_file_location("release_previous_tag", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.previous_tag(current, tags)


# A slice of the real tag history, deliberately out of order: the API returns
# refs sorted by name, which puts v0.10.0 before v0.9.0.
TAGS = [
    "refs/tags/v0.1.0",
    "refs/tags/v0.1.2",
    "refs/tags/v0.10.0",
    "refs/tags/v0.10.0-insider.2",
    "refs/tags/v0.10.0-insider.4",
    "refs/tags/v0.10.0-insider.5",
    "refs/tags/v0.10.0-rc.1",
    "refs/tags/v0.11.0-rc.1",
    "refs/tags/v0.2.0",
    "refs/tags/v0.2.0-rc.1",
    "refs/tags/v0.7.1",
    "refs/tags/v0.7.2",
    "refs/tags/v0.7.2-insider.1",
    "refs/tags/v0.8.0",
    "refs/tags/v0.9.0",
    "refs/tags/v0.9.0-insider.10",
    "refs/tags/v0.9.0-insider.9",
    "refs/tags/not-a-release",
    "refs/tags/v1.0.0-beta",
]


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        # Stable compares against the previous stable, by version, not by name.
        ("v0.10.0", "v0.9.0"),
        ("v0.9.0", "v0.8.0"),
        ("v0.8.0", "v0.7.2"),
        # A patch on an older line ignores newer stables above it.
        ("v0.9.1", "v0.9.0"),
        ("v0.7.2", "v0.7.1"),
        # A prerelease compares against the previous one on its own line, even
        # when the label changes along the line (rc.1 then insider.2).
        ("v0.10.0-insider.5", "v0.10.0-insider.4"),
        ("v0.10.0-insider.4", "v0.10.0-insider.2"),
        ("v0.10.0-insider.2", "v0.10.0-rc.1"),
        # Numbers compare as numbers, so insider.10 follows insider.9.
        ("v0.9.0-insider.10", "v0.9.0-insider.9"),
        # The first prerelease of a line compares against the last stable.
        ("v0.11.0-rc.1", "v0.10.0"),
        ("v0.10.0-rc.1", "v0.9.0"),
        ("v0.2.0-rc.1", "v0.1.2"),
        # Nothing earlier: empty, so the action falls back to GitHub's choice.
        ("v0.1.0", ""),
        # A tag outside the release shapes gets no base.
        ("not-a-release", ""),
    ],
)
def test_the_base_follows_the_release_lineage(current: str, expected: str) -> None:
    assert previous_tag(current, TAGS) == expected


def test_the_base_is_never_the_tag_itself_or_a_later_one() -> None:
    """A rerun sees its own tag (and later ones) in the list; neither is a base."""
    for tag in ("v0.10.0", "v0.10.0-insider.4", "v0.11.0-rc.1"):
        chosen = previous_tag(tag, TAGS)
        assert chosen and chosen != tag
        assert previous_tag(chosen, TAGS) != tag


def test_the_cli_reads_tag_names_from_stdin() -> None:
    """The workflow pipes the API's refs in; the script prints one tag."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "v0.10.0"],
        input="\n".join(TAGS) + "\n",
        capture_output=True,
        timeout=60,
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "v0.9.0\n"


def test_the_release_step_passes_the_chosen_base() -> None:
    """Without previous_tag the action lets GitHub choose, which is the defect."""
    workflow = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["github-release"]["steps"]
    by_name = {s.get("name"): (i, s) for i, s in enumerate(steps)}
    chooser_name = "Choose the previous tag for the generated notes"
    assert chooser_name in by_name, "no step chooses the previous tag"
    assert "Create GitHub Release" in by_name
    chooser_at, chooser = by_name[chooser_name]
    create_at, create = by_name["Create GitHub Release"]
    assert chooser_at < create_at
    assert chooser.get("id") == "notes_base"
    assert "scripts/release_previous_tag.py" in chooser.get("run", "")
    assert chooser.get("if") == create.get("if")
    assert create["with"].get("previous_tag") == "${{ steps.notes_base.outputs.previous_tag }}"


def test_a_failed_tag_listing_does_not_hold_back_the_release_page() -> None:
    """Only the notes' base depends on the listing, so its failure falls back."""
    workflow = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    chooser = next(
        s
        for s in workflow["jobs"]["github-release"]["steps"]
        if s.get("name") == "Choose the previous tag for the generated notes"
    )
    assert "if ! tags=$(gh api" in chooser["run"]
    assert previous_tag("v0.10.0", []) == ""


def _parse_version(text: str) -> tuple[int, int, int, int, int] | None:
    """The publish guard's parser, which the selection reads tags with."""
    path = ROOT / "scripts" / "check_feed_advance.py"
    spec = importlib.util.spec_from_file_location("check_feed_advance_for_notes", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse_version(text)


@pytest.mark.parametrize(
    "tag",
    ["v0.10.0", "v0.10.0-insider.5", "v0.11.0-rc.1", "v0.12.0-beta-preview.1", "v1.0.0-beta"],
)
def test_tags_read_the_same_as_the_publish_guard(tag: str) -> None:
    """One parser decides what a release tag is, for the notes and the feed guard."""
    major, minor, patch, is_release, number = _parse_version(tag) or pytest.fail(tag)
    spec = importlib.util.spec_from_file_location("release_previous_tag", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parsed = module.parse_tag("refs/tags/" + tag)
    assert parsed is not None, f"{tag} is a release tag to the publish guard"
    assert parsed.name == tag
    assert parsed.base == (major, minor, patch)
    assert parsed.number == (None if is_release else number)


def test_a_hyphenated_prerelease_label_follows_its_line() -> None:
    tags = TAGS + ["refs/tags/v0.12.0-beta-preview.1"]
    assert previous_tag("v0.12.0-beta-preview.2", tags) == "v0.12.0-beta-preview.1"
    assert previous_tag("v0.12.0-beta-preview.1", tags) == "v0.10.0"


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="runs the step under bash"
)
def test_a_failed_choice_does_not_hold_back_the_release_page(tmp_path: Path) -> None:
    """The step still succeeds, with an empty base, when the script itself fails."""
    workflow = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    chooser = next(
        s
        for s in workflow["jobs"]["github-release"]["steps"]
        if s.get("name") == "Choose the previous tag for the generated notes"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("gh", "echo refs/tags/v0.9.0"), ("python3", "exit 1")):
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        stub.chmod(0o755)
    output = tmp_path / "github_output"
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "GITHUB_OUTPUT": str(output),
        "REPO": "owner/repo",
        "TAG": "v0.10.0",
    }
    result = subprocess.run(
        ["bash", "-c", chooser["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=60,
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == "previous_tag=\n"
