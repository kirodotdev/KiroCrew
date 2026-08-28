"""Tests for the Releases-archive changelog parser."""

from __future__ import annotations

import pytest

from kiro_crew.changelog import base_version, build_release_list, parse_sections, running_release

# Shaped after the repo's real CHANGELOG.md: an em dash in the heading, a title
# and preamble before the first section, and h3 subsections inside the body.
REAL_SHAPE = """# Changelog

All notable changes to Kiro Crew are documented in this file.

## [0.1.2] — 2026-07-30

First public release.

### Chat from wherever you already are

- **One agent, ten ways in** — a dashboard, a desktop app, a CLI.
"""


class TestBaseVersion:
    # Every spelling the release pipeline can put on a build, with the source of
    # each: release.yml maps ANY prerelease tag to `rcN` for the wheel, and
    # nightly.yml emits the semver stamp plus a `.devN` wheel. If a build's own
    # version fails to fold, the user's version appears as a separate row with
    # `stale: false` -- the archive claims a version is released when it is not.
    EMITTED = [
        ("0.2.0-rc.2", "release.yml prerelease tag"),
        ("0.2.0-insider.4", "release.yml prerelease tag"),
        ("0.2.0-nightly.20260806t065257", "nightly.yml desktop stamp"),
        ("0.2.0-nightly.20260727", "retired nightly stamp, still installed"),
        ("0.2.0-nightly.202607261234", "retired nightly stamp, still installed"),
        ("0.2.0rc4", "wheel version for ANY prerelease tag"),
        ("0.2.0.dev20260806065257", "nightly wheel"),
        ("0.2.0rc4.dev5", "PEP 440 dev of a prerelease"),
        ("0.2.0.post1", "PEP 440 post-release"),
        ("0.2.0+abc123", "local/build segment"),
        ("0.2.0-rc.1+abc", "local segment on a semver prerelease"),
        ("0.2.0rc4+dirty", "local segment on a wheel prerelease"),
    ]

    # Malformed spellings that must NOT fold. A fold here is not cosmetic: the
    # heading would land on a real release's row and, because the last body for a
    # version wins, replace its notes and date with garbage.
    MALFORMED = [
        "0.1.2junk",
        "0.1.2rc4junk",
        "0.1.2b2elated",
        "0.1.2-rc.1oops",
        "0.1.2-bogus",
        "0.1.2-belated",
        "0.1.2beta",
        "0.1.2rc",
        "0.1.2post",
        "0.1.2-nightly.2026",
        # `\d` matches Arabic-Indic digits too, so this folded onto 0.1.2 and
        # overwrote its notes until every class became ASCII `[0-9]`.
        "0.1.2rc\u0664",
        "not-a-version",
        "",
    ]

    def test_bare_version_is_its_own_base(self):
        assert base_version("0.1.2") == "0.1.2"

    @pytest.mark.parametrize("version,source", EMITTED)
    def test_every_emitted_spelling_folds_onto_its_release(self, version, source):
        assert base_version(version) == "0.2.0", f"{source} did not fold"

    @pytest.mark.parametrize("version", MALFORMED)
    def test_a_malformed_spelling_keeps_its_own_identity(self, version):
        assert base_version(version) == version.strip()


class TestRunningRelease:
    """The running build's version is read leniently -- see `running_release`."""

    def test_a_label_the_allowlist_does_not_know_still_folds(self):
        """release.yml passes ANY `v1.2.3-<label>` tag through to the build.

        A label this module has never heard of must still put the reader on their
        own release's row. Failing to fold shows them a second row that looks
        released, which is the same lie as a stale archive.
        """
        assert running_release("0.1.2-preview.7") == ("0.1.2", True)
        assert running_release("0.1.2-canary.3") == ("0.1.2", True)

    def test_a_known_spelling_reads_the_same_as_a_heading(self):
        assert running_release("0.2.0-rc.2") == ("0.2.0", True)
        assert running_release("0.2.0rc4") == ("0.2.0", True)
        assert running_release("0.2.0") == ("0.2.0", False)

    def test_build_metadata_alone_is_not_a_prerelease(self):
        """`base != version` would badge a local build "In progress"; a marker won't."""
        assert running_release("0.2.0+abc123") == ("0.2.0", False)

    def test_an_unreadable_version_degrades_instead_of_raising(self):
        assert running_release("weird") == ("weird", False)
        assert running_release("") == ("", False)


class TestParseSections:
    def test_extracts_version_date_and_body(self):
        [(version, date, body)] = parse_sections(REAL_SHAPE)
        assert version == "0.1.2"
        assert date == "2026-07-30"
        assert body.startswith("First public release.")

    def test_preamble_is_not_part_of_any_release(self):
        [(_, _, body)] = parse_sections(REAL_SHAPE)
        assert "All notable changes" not in body

    def test_h3_subsections_stay_inside_the_body(self):
        [(_, _, body)] = parse_sections(REAL_SHAPE)
        assert "### Chat from wherever you already are" in body

    def test_hyphen_and_en_dash_dates_are_accepted(self):
        md = "## [0.3.0] - 2026-09-01\nx\n\n## [0.2.0] – 2026-08-12\ny\n"
        assert [(v, d) for v, d, _ in parse_sections(md)] == [
            ("0.3.0", "2026-09-01"),
            ("0.2.0", "2026-08-12"),
        ]

    def test_missing_date_is_empty_not_a_failure(self):
        [(version, date, _)] = parse_sections("## [0.4.0]\nbody\n")
        assert (version, date) == ("0.4.0", "")

    def test_unversioned_h2_ends_the_previous_body(self):
        md = "## [0.2.0] — 2026-08-12\nkept\n\n## Unreleased\nexcluded\n"
        [(_, _, body)] = parse_sections(md)
        assert "kept" in body
        assert "excluded" not in body

    def test_no_sections_yields_empty(self):
        assert parse_sections("# Changelog\n\nnothing here.\n") == []


class TestBuildReleaseList:
    def test_stable_build_with_a_section_is_current_and_not_in_progress(self):
        [rel] = build_release_list(REAL_SHAPE, "0.1.2")
        assert (rel.version, rel.is_current, rel.in_progress) == ("0.1.2", True, False)
        assert rel.date == "2026-07-30"

    def test_prerelease_adds_its_own_release_as_in_progress(self):
        """The 0.2.0-rc.1 case: 0.2.0 is listed though no section exists yet."""
        rels = build_release_list(REAL_SHAPE, "0.2.0-rc.1")
        assert [r.version for r in rels] == ["0.2.0", "0.1.2"]
        current = rels[0]
        assert current.is_current and current.in_progress
        assert current.body == "" and current.date == ""

    def test_nightly_maps_to_the_same_row_as_an_rc(self):
        """One mechanism must serve both prerelease channels."""
        rc = build_release_list(REAL_SHAPE, "0.2.0-rc.1")
        nightly = build_release_list(REAL_SHAPE, "0.2.0-nightly.20260806t065257")
        assert [(r.version, r.in_progress) for r in rc] == [
            (r.version, r.in_progress) for r in nightly
        ]

    def test_stable_build_without_a_section_is_still_listed(self):
        """v0.1.3 shipped with no section; the user on it must still see its row."""
        rels = build_release_list(REAL_SHAPE, "0.1.3")
        assert [r.version for r in rels] == ["0.1.3", "0.1.2"]
        assert rels[0].is_current and not rels[0].in_progress
        assert rels[0].body == ""

    def test_sectionless_versions_the_user_is_not_on_are_omitted(self):
        """0.1.0/0.1.1/0.1.3 have no sections and must not become dead rows."""
        assert [r.version for r in build_release_list(REAL_SHAPE, "0.1.2")] == ["0.1.2"]

    def test_promote_does_not_move_the_row(self):
        """Same list shape before and after 0.2.0 ships, so nothing jumps."""
        before = build_release_list(REAL_SHAPE, "0.2.0-rc.5")
        after = build_release_list(REAL_SHAPE, "0.2.0")
        assert [r.version for r in before] == [r.version for r in after]
        assert before[0].in_progress and not after[0].in_progress

    def test_numeric_sort_puts_0_10_above_0_9(self):
        md = "## [0.9.0] — 2026-01-01\na\n\n## [0.10.0] — 2026-02-01\nb\n"
        assert [r.version for r in build_release_list(md, "0.10.0")] == ["0.10.0", "0.9.0"]

    def test_prerelease_section_folds_into_its_base_row(self):
        md = "## [0.2.0-rc.1] — 2026-08-10\ndraft\n\n## [0.1.2] — 2026-07-30\nold\n"
        assert [r.version for r in build_release_list(md, "0.2.0-rc.1")] == ["0.2.0", "0.1.2"]

    def test_unknown_version_does_not_crash_or_add_a_row(self):
        rels = build_release_list(REAL_SHAPE, "")
        assert [r.version for r in rels] == ["0.1.2"]
        assert not rels[0].is_current

    def test_a_malformed_heading_cannot_overwrite_a_real_release(self):
        """The defect this guards: prefix folding made garbage win.

        ``[0.1.2junk]`` normalised to ``0.1.2`` and, because the last body for a
        version wins, replaced the real notes and their date -- so the archive
        showed GARBAGE under 0.1.2 with no way to tell.

        The malformed heading must come SECOND for this to bite: with it first,
        last-body-wins hands the win to the real notes even while the two rows
        are wrongly collapsed, and the assertions below would pass against the
        very defect they exist to catch.
        """
        md = "## [0.1.2] — 2026-07-30\n\nReal notes.\n\n## [0.1.2junk]\n\nGARBAGE\n"
        rels = {r.version: r for r in build_release_list(md, "0.1.2")}
        assert rels["0.1.2"].body.strip() == "Real notes."
        assert rels["0.1.2"].date == "2026-07-30"
        assert rels["0.1.2junk"].body.strip() == "GARBAGE"

    def test_a_prerelease_draft_section_does_not_replace_the_released_notes(self):
        """The fold is correct here; the OVERWRITE was not.

        ``## [0.2.0-rc.1]`` legitimately folds into 0.2.0, but with last-wins the
        rc draft lower in the file replaced the released section's body AND date
        -- and because the fold itself is endorsed, nothing looked wrong.
        Keep-a-changelog files are newest-first, so first-in-document-order wins.
        """
        md = (
            "## [0.2.0] — 2026-08-20\n\nReleased notes.\n\n"
            "## [0.2.0-rc.1] — 2026-08-10\n\nDraft notes.\n"
        )
        rels = build_release_list(md, "0.2.0")

        assert [r.version for r in rels] == ["0.2.0"]
        assert rels[0].body.strip() == "Released notes."
        assert rels[0].date == "2026-08-20"

    def test_a_build_on_an_unknown_prerelease_label_is_not_a_second_row(self):
        """A `-preview.N` tag is a prerelease of 0.1.2, not a release of its own.

        Before `running_release`, this produced two rows: the real 0.1.2 marked
        not-current, and `0.1.2-preview.7` marked current with `in_progress`
        false -- i.e. the reader's unreleased build presented as released.
        """
        rels = build_release_list("## [0.1.2] — 2026-07-30\n\nx\n", "0.1.2-preview.7")

        assert [r.version for r in rels] == ["0.1.2"]
        assert rels[0].is_current and rels[0].in_progress
