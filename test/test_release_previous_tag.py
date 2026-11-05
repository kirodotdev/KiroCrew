"""The generated release notes compare against a base this repository chooses.

GitHub's own choice is the newest release tag that is an ancestor of the new
one. Releases are tagged on per-version release branches, so that choice lands
on a tag from the project's first weeks and every page lists thousands of old
pull requests. ``scripts/release_previous_tag.py`` picks the base by lineage
instead, and the release step passes it as ``previous_tag``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
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


def _module():
    assert SCRIPT.is_file(), f"{SCRIPT} is missing"
    spec = importlib.util.spec_from_file_location("release_previous_tag", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def previous_tag(current: str, releases: list[str]) -> str:
    """Call the script's own selection on ``<tag> <published|draft>`` lines."""
    module = _module()
    return module.previous_tag(current, module.parse_releases("\n".join(releases)))


# A slice of the real release history, deliberately out of order: the API
# does not list releases by version, and by name v0.10.0 sorts before v0.9.0.
TAGS = [
    "v0.1.0 published",
    "v0.1.2 published",
    "v0.10.0 published",
    "v0.10.0-insider.2 published",
    "v0.10.0-insider.4 published",
    "v0.10.0-insider.5 published",
    "v0.10.0-rc.1 published",
    "v0.11.0-rc.1 published",
    "v0.2.0 published",
    "v0.2.0-rc.1 published",
    "v0.7.1 published",
    "v0.7.2 published",
    "v0.7.2-insider.1 published",
    "v0.8.0 published",
    "v0.9.0 published",
    "v0.9.0-insider.10 published",
    "v0.9.0-insider.9 published",
    "not-a-release published",
    "v1.0.0-beta published",
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


# A line whose v0.11.0-rc.2 run failed before the release page was created:
# the tag exists, the release does not, so it is not in the release listing.
# The rc.3 page must still list everything merged since rc.1.
FAILED_RC = [
    "v0.10.0 published",
    "v0.11.0-rc.1 published",
    "v0.11.0-rc.3 published",
]


def test_a_prerelease_that_published_no_page_is_not_a_base() -> None:
    assert previous_tag("v0.11.0-rc.3", FAILED_RC) == "v0.11.0-rc.1"
    # Had rc.2 published its page, rc.2 would be the base.
    assert previous_tag("v0.11.0-rc.3", [*FAILED_RC, "v0.11.0-rc.2 published"]) == "v0.11.0-rc.2"


def test_a_prerelease_with_only_a_draft_page_is_not_a_base() -> None:
    """Prerelease pages are published on create, so a draft one never shipped."""
    releases = [*FAILED_RC, "v0.11.0-rc.2 draft"]
    assert previous_tag("v0.11.0-rc.3", releases) == "v0.11.0-rc.1"
    # With every earlier prerelease of the line unpublished, the first published
    # one falls back to the last stable, as the first prerelease of a line does.
    assert previous_tag("v0.11.0-rc.2", ["v0.10.0 published", "v0.11.0-rc.1 draft"]) == "v0.10.0"


def test_a_published_prerelease_is_the_base_for_the_next_one() -> None:
    assert previous_tag("v0.11.0-rc.2", FAILED_RC) == "v0.11.0-rc.1"
    assert previous_tag("v0.11.0-insider.4", [*FAILED_RC, "v0.11.0-insider.4 published"]) == (
        "v0.11.0-rc.3"
    )


def test_a_stable_still_in_draft_is_the_base_for_the_next_stable() -> None:
    """A stable page is a draft until record-stable-promotion publishes it."""
    releases = ["v0.9.0 published", "v0.10.0 draft", "v0.10.1 draft"]
    assert previous_tag("v0.10.1", releases) == "v0.10.0"
    assert previous_tag("v0.11.0", releases) == "v0.10.1"
    # ...and for the first prerelease of the next line.
    assert previous_tag("v0.11.0-rc.1", releases) == "v0.10.1"


def test_stable_releases_pick_the_same_base_published_or_draft() -> None:
    """Draft-then-promote does not move a stable release's base."""
    published = [line for line in TAGS if line.split()[0].count("-") == 0]
    drafted = [line.replace(" published", " draft") for line in published]
    for current in ("v0.10.0", "v0.9.0", "v0.8.0", "v0.9.1", "v0.7.2", "v0.1.0"):
        assert previous_tag(current, drafted) == previous_tag(current, published)
        assert previous_tag(current, drafted) == previous_tag(current, TAGS)


@pytest.mark.parametrize("line", ["v0.9.0", "v0.9.0 unknown", "v0.9.0 published extra", ""])
def test_a_line_without_a_release_state_is_not_a_candidate(line: str) -> None:
    assert previous_tag("v0.10.0", [line]) == ""


def test_the_cli_reads_releases_from_stdin() -> None:
    """The workflow pipes the API's releases in; the script prints one tag."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "v0.11.0-rc.3"],
        input="\n".join(FAILED_RC) + "\n",
        capture_output=True,
        timeout=60,
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "v0.11.0-rc.1\n"


def test_the_cli_picks_the_previous_stable() -> None:
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


def _chooser_run() -> str:
    workflow = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    return next(
        s["run"]
        for s in workflow["jobs"]["github-release"]["steps"]
        if s.get("name") == "Choose the previous tag for the generated notes"
    )


def test_the_step_lists_releases_not_tag_refs() -> None:
    """Tag refs include prereleases whose run failed before a page existed."""
    run = _chooser_run()
    assert "matching-refs/tags" not in run
    assert re.search(r'gh api --paginate "repos/\$REPO/releases\?per_page=100"', run), run


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="runs the step under bash"
)
def test_the_step_skips_a_tag_whose_run_published_no_page(tmp_path: Path) -> None:
    """End to end: rc.2's tag exists, its page does not, so rc.3 compares against rc.1."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # The stub answers both listings, after GitHub's --jq filter: the tag refs
    # still carry rc.2, the release listing does not.
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *matching-refs/tags*) printf "%s\\n" refs/tags/v0.10.0 refs/tags/v0.11.0-rc.1'
        " refs/tags/v0.11.0-rc.2 refs/tags/v0.11.0-rc.3 ;;\n"
        '  *releases*) printf "%s\\n" "v0.10.0 published" "v0.11.0-rc.1 published"'
        ' "v0.11.0-rc.3 published" ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    python3 = bin_dir / "python3"
    # -B: the script imports a sibling from scripts/, and the replacement env
    # drops the suite's bytecode suppression, so the child must not write a
    # cache into the checkout.
    python3.write_text(f'#!/bin/sh\nexec "{sys.executable}" -B "$@"\n', encoding="utf-8")
    python3.chmod(0o755)
    output = tmp_path / "github_output"
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "GITHUB_OUTPUT": str(output),
        "REPO": "owner/repo",
        "TAG": "v0.11.0-rc.3",
    }
    result = subprocess.run(
        ["bash", "-c", _chooser_run()],
        cwd=ROOT,
        env=env,
        capture_output=True,
        timeout=60,
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == "previous_tag=v0.11.0-rc.1\n"


@pytest.mark.skipif(shutil.which("jq") is None, reason="evaluates the step's jq filter")
def test_the_step_feeds_the_script_the_release_state() -> None:
    """The step's jq filter writes the lines the script reads, drafts included."""
    match = re.search(r"--jq '([^']+)'", _chooser_run())
    assert match, "the release listing has no --jq filter"
    page = [
        {"tag_name": "v0.10.0", "draft": True, "prerelease": False},
        {"tag_name": "v0.11.0-rc.1", "draft": False, "prerelease": True},
    ]
    result = subprocess.run(
        ["jq", "-r", match.group(1)],
        input=json.dumps(page),
        capture_output=True,
        timeout=60,
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["v0.10.0 draft", "v0.11.0-rc.1 published"]
    module = _module()
    assert module.previous_tag("v0.11.0", module.parse_releases(result.stdout)) == "v0.10.0"


def test_a_failed_tag_listing_does_not_hold_back_the_release_page() -> None:
    """Only the notes' base depends on the listing, so its failure falls back."""
    assert "if ! releases=$(gh api" in _chooser_run()
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
    tags = TAGS + ["v0.12.0-beta-preview.1 published"]
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
    for name, body in (("gh", "echo v0.9.0 published"), ("python3", "exit 1")):
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
