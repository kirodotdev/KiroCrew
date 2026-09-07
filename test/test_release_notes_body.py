"""The release body is assembled, so its shape needs its own contract.

`github-release` hands the extracted CHANGELOG section to
softprops/action-gh-release as ``body_path`` while ALSO setting
``generate_release_notes: true``, and that action PRE-PENDS the body to the
generated notes rather than replacing them. So the published body is always
``our section + whatever GitHub generates``, and a defect can appear without
anyone writing it. Three did, on v0.3.0 and again on v0.5.0:

* the CHANGELOG's required ``### Contributors`` list, copied in verbatim,
  duplicated the contributor block GitHub renders natively from the tag range;
* the CHANGELOG's ~76-column hard wrapping became real ``<br>``s, because GitHub
  renders a release body with GFM line breaks ON, leaving a fixed-width column
  and a wide empty gutter down the page;
* the generated commit list ran to 746 lines for 922 commits and pushed the body
  past GitHub's 125,000-character ceiling, publishing it truncated mid-word.

These tests pin the two transforms the extract step applies, by RUNNING the
step's own embedded script rather than restating what it should do.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from kiro_crew.subprocess_utf8 import UTF8_TEXT

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
STEP_NAME = "Extract CHANGELOG section for the release body"


def _extract_step_script() -> str:
    """Pull the step's embedded python out of the workflow, verbatim."""
    workflow = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    step = next(
        s for s in workflow["jobs"]["github-release"]["steps"] if s.get("name") == STEP_NAME
    )
    run = step["run"]
    body = run.split("<<'PY_EXTRACT'\n", 1)[1].rsplit("PY_EXTRACT", 1)[0]
    return textwrap.dedent(body)


def _run_step(tmp_path: Path, changelog: str, *, channel: str = "stable", version: str = "9.9.9"):
    """Run the real step against a synthetic CHANGELOG in an isolated cwd."""
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    script = tmp_path / "extract.py"
    script.write_text(_extract_step_script(), encoding="utf-8")
    # The environment is INHERITED, with only PYTHONPATH overridden so the child
    # can import `kiro_crew.changelog` from a cwd that is not the checkout.
    # Handing it a from-scratch dict instead drops SystemRoot on Windows, and
    # Winsock cannot initialize without it: `import asyncio` in the child then
    # raises WinError 10106 before the script under test runs at all.
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [sys.executable, str(script), channel, version],
        cwd=tmp_path,
        capture_output=True,
        env=env,
        **UTF8_TEXT,
    )
    notes = tmp_path / "release-notes.md"
    return result, notes.read_text(encoding="utf-8") if notes.exists() else None


SECTION = """# Changelog

## [9.9.9] — 2026-01-01

An opening paragraph that the CHANGELOG wraps at a narrow column so it
reads well in a rendered file, spanning three source lines in total
before the blank line that ends it.

### A feature heading

- **Something shipped** — a bullet whose prose also wraps across more
  than one source line, the way every entry in this file does.
- **Another one** — short.

### Contributors

@alice @bob
@carol

## [9.9.8] — 2025-12-01

Older section that must not leak into the notes.
"""


def test_contributors_subsection_is_dropped_from_the_release_body(tmp_path: Path) -> None:
    """The CHANGELOG must keep its list; the release body must not carry it.

    AGENTS.md requires the section to end with `### Contributors` because that
    copy ships inside the wheel and feeds the dashboard's Releases page, where no
    native block exists. The release page renders its own from the tag range, so
    the body carrying ours puts two lists next to each other.
    """
    result, notes = _run_step(tmp_path, SECTION)
    assert result.returncode == 0, result.stderr
    assert notes is not None
    assert "### Contributors" not in notes
    assert "@alice" not in notes and "@carol" not in notes
    # The rest of the section survives.
    assert "### A feature heading" in notes
    assert "Something shipped" in notes
    # And a neighbouring release never leaks in.
    assert "9.9.8" not in notes and "must not leak" not in notes


def test_paragraphs_and_bullets_are_joined_onto_one_line(tmp_path: Path) -> None:
    """A hard-wrapped body renders as a narrow column with an empty gutter.

    GitHub renders issue/PR/release bodies with GFM line breaks ON, so each
    source newline inside a paragraph becomes a <br>. Joining them lets the
    browser reflow to the container instead.
    """
    result, notes = _run_step(tmp_path, SECTION)
    assert result.returncode == 0, result.stderr
    assert notes is not None

    lines = notes.split("\n")
    opening = next(line for line in lines if line.startswith("An opening paragraph"))
    assert "spanning three source lines" in opening, "paragraph was not joined"
    assert opening.endswith("that ends it."), opening

    bullet = next(line for line in lines if line.startswith("- **Something shipped**"))
    assert "the way every entry in this file does." in bullet, "bullet was not joined"

    # Structure survives: headings stay on their own lines, bullets stay bullets.
    assert "### A feature heading" in lines
    assert sum(1 for line in lines if line.startswith("- **")) == 2


def test_code_fences_keep_their_own_line_breaks(tmp_path: Path) -> None:
    """Joining must never reflow a fenced block -- that would corrupt commands."""
    changelog = textwrap.dedent("""\
        # Changelog

        ## [9.9.9] — 2026-01-01

        Intro prose that wraps
        across two lines.

        ```bash
        first --command
        second --command
        ```

        Trailing prose that also
        wraps.
        """)
    result, notes = _run_step(tmp_path, changelog)
    assert result.returncode == 0, result.stderr
    assert notes is not None
    assert "first --command\nsecond --command" in notes, "fence contents were joined"
    assert "Intro prose that wraps across two lines." in notes


def test_a_prerelease_gets_an_empty_body_not_a_changelog_section(tmp_path: Path) -> None:
    """Insider/nightly have no shipped section; body_path must still be a file."""
    result, notes = _run_step(tmp_path, SECTION, channel="insider")
    assert result.returncode == 0, result.stderr
    assert notes == ""


def test_a_missing_section_fails_closed(tmp_path: Path) -> None:
    """The stable gate should have caught it; this refuses rather than shipping empty."""
    result, _ = _run_step(tmp_path, SECTION, version="7.7.7")
    assert result.returncode != 0
    assert "has no '## [7.7.7]' section" in result.stderr + result.stdout


@pytest.mark.parametrize("field", ["body_path", "generate_release_notes"])
def test_the_release_step_still_combines_both_inputs(field: str) -> None:
    """If either input goes away the transforms above stop being load-bearing.

    Pinned so a future change that drops `generate_release_notes` (and with it the
    native New Contributors block) or stops passing a body is a deliberate edit
    here, not a silent one.
    """
    workflow = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    step = next(
        s
        for s in workflow["jobs"]["github-release"]["steps"]
        if "action-gh-release" in str(s.get("uses", ""))
    )
    assert field in step["with"]
