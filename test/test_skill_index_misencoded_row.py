"""One misencoded global SKILL.md costs its own row, never the whole skill index.

``list_skills()`` feeds both the per-turn skill index and ``GET /api/skills``.
Its unconfined metadata read is strict on purpose: the same reader serves
writers that must never rewrite metadata they could not decode. Left uncaught,
that strictness escapes the listing whole: one UTF-16 file (PowerShell's
default ``Out-File`` encoding, which opens with ``0xFF``) raises
``UnicodeDecodeError`` out of ``list_skills()``, every chat turn's context
build fails and the endpoint answers 500. The listing is a reader, so it
catches at its own call site: that one row is dropped with one warning naming
the file and the encoding problem, and every other row survives. A UTF-8 file
that merely carries a byte-order mark is not misencoded and loads with its
frontmatter and body intact.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from kiro_crew import skills as sk
from kiro_crew.skills import SkillsLoader

_SKILL_TEXT = (
    "---\nname: {name}\ndescription: {name} description\n---\n# {name}\n\nbody of {name}\n"
)
# The same skill with a trigger phrase, for the per-message matcher.
_TRIGGERED_SKILL_TEXT = (
    "---\nname: {name}\ndescription: {name} description\ntriggers: zebra enclosure\n---\n"
    "# {name}\n\nbody of {name}\n"
)

# What PowerShell 5.1's ``Out-File`` writes by default: a UTF-16LE byte-order
# mark followed by UTF-16LE text. The first byte is 0xFF, which no UTF-8
# decoder accepts.
_UTF16_SKILL = b"\xff\xfe" + _SKILL_TEXT.format(name="broken").encode("utf-16-le")
# The same file saved wrongly a second time: different text, so a different
# size, behind the same first byte -- the decoder's complaint reads the same,
# and only the file's stat identity tells the two saves apart.
_UTF16_SKILL_RESAVED = b"\xff\xfe" + _SKILL_TEXT.format(name="broken again").encode("utf-16-le")
# A UTF-8 file saved "with BOM" (Notepad, many Windows editors): valid UTF-8,
# prefixed with EF BB BF.
_UTF8_BOM_SKILL = b"\xef\xbb\xbf" + _SKILL_TEXT.format(name="bom").encode("utf-8")


def _write_skill(root: Path, name: str, raw: bytes | None = None) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_bytes(_SKILL_TEXT.format(name=name).encode("utf-8") if raw is None else raw)
    return path


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno >= logging.WARNING]


@pytest.fixture(params=["serial", "pooled"])
def read_branch(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run each case through both ``rows()`` branches of the listing.

    The pooled branch engages only at ``_CATALOG_READ_BATCH`` entries, where a
    worker's exception surfaces at ``future.result()`` instead of at the call.
    Lowering the batch to two puts a three-skill catalog on the worker pool.
    """
    if request.param == "pooled":
        monkeypatch.setattr(sk, "_CATALOG_READ_BATCH", 2)
    return str(request.param)


class TestOneMisencodedSkillDropsOnlyItsOwnRow:
    def test_the_other_rows_survive_and_one_warning_names_the_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, read_branch: str
    ) -> None:
        root = tmp_path / "skills"
        _write_skill(root, "alpha")
        _write_skill(root, "beta")
        broken = _write_skill(root, "broken", _UTF16_SKILL)
        loader = SkillsLoader(skills_path=root, install_builtins=False)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.skills"):
            rows = loader.list_skills()

        assert sorted(row["key"] for row in rows) == ["alpha", "beta"], read_branch
        # The surviving rows keep their own metadata; nothing about them degrades.
        by_key = {row["key"]: row for row in rows}
        assert by_key["alpha"]["name"] == "alpha"
        assert by_key["beta"]["description"] == "beta description"
        warnings = _warnings(caplog)
        assert len(warnings) == 1, [record.getMessage() for record in warnings]
        message = warnings[0].getMessage()
        assert str(broken) in message
        assert "utf-8" in message.lower(), message

    def test_a_second_listing_keeps_the_rows_and_does_not_repeat_the_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, read_branch: str
    ) -> None:
        """The listing runs on every chat turn and every Skills-page poll, so
        the warning is bounded per unchanged file the way the HTML-page refusal
        is; a persistent bad file must not write the same line every few
        seconds. (Re-saving the file changes its stat fingerprint and earns a
        fresh warning; that is the bound's key, not the path alone.)"""
        root = tmp_path / "skills"
        _write_skill(root, "alpha")
        _write_skill(root, "beta")
        _write_skill(root, "broken", _UTF16_SKILL)
        loader = SkillsLoader(skills_path=root, install_builtins=False)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.skills"):
            first = loader.list_skills()
            second = loader.list_skills()

        assert sorted(row["key"] for row in first) == ["alpha", "beta"]
        assert sorted(row["key"] for row in second) == ["alpha", "beta"]
        assert len(_warnings(caplog)) == 1, read_branch

    def test_a_bad_file_re_saved_between_two_listings_earns_a_fresh_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, read_branch: str
    ) -> None:
        """A warm listing reuses the stat fingerprints its own catalog walk
        took and stats nothing itself. Until the next walk those fingerprints
        are the walk's, so a bad file the operator re-saves out of band -- new
        bytes, a later mtime, still not UTF-8 -- keeps its old identity in the
        listing's hands. Keyed on that, the bound would swallow the fresh line
        the operator is promised until the catalog refreshes; the key is a stat
        taken when the read fails, so the second wrong save is reported now."""
        root = tmp_path / "skills"
        _write_skill(root, "alpha")
        _write_skill(root, "beta")
        broken = _write_skill(root, "broken", _UTF16_SKILL)
        loader = SkillsLoader(skills_path=root, install_builtins=False)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.skills"):
            first = loader.list_skills()
            # The walk fingerprinted the file and the warm listing trusts that
            # fingerprint without a stat of its own; the re-save below does not
            # move it, since no walk runs in between.
            hinted = loader._catalog_fingerprint_hint(None).get(str(broken))
            assert hinted, "precondition: the warm listing holds the walk's fingerprint"
            broken.write_bytes(_UTF16_SKILL_RESAVED)
            later = broken.stat().st_mtime_ns + 2_000_000_000
            os.utime(broken, ns=(later, later))
            assert loader._catalog_fingerprint_hint(None).get(str(broken)) == hinted
            second = loader.list_skills()

        assert sorted(row["key"] for row in first) == ["alpha", "beta"], read_branch
        assert sorted(row["key"] for row in second) == ["alpha", "beta"], read_branch
        warnings = _warnings(caplog)
        assert len(warnings) == 2, [record.getMessage() for record in warnings]
        # Same file, same complaint: the second line is earned by the file's
        # new stat identity alone, not by a different problem text.
        assert warnings[0].getMessage() == warnings[1].getMessage()
        assert str(broken) in warnings[1].getMessage()

    def test_a_file_that_cannot_be_opened_degrades_the_same_way(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The unconfined reader raises rather than refuses on an open failure
        (writers must hear it), so a permission-denied SKILL.md reaches the same
        call site as a misencoded one and gets the same answer: its row, not the
        index. Simulated at the choke point, since a mode-000 file is not
        deniable to root and not expressible on Windows."""
        root = tmp_path / "skills"
        _write_skill(root, "alpha")
        _write_skill(root, "beta")
        locked = _write_skill(root, "locked")
        loader = SkillsLoader(skills_path=root, install_builtins=False)
        real_read = loader._read_enumerated_skill_bytes

        def denied(path: Path, within: str | None, **kwargs: object) -> bytes | None:
            if path == locked:
                raise PermissionError(13, "Permission denied", str(path))
            return real_read(path, within, **kwargs)

        monkeypatch.setattr(loader, "_read_enumerated_skill_bytes", denied)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.skills"):
            rows = loader.list_skills()

        assert sorted(row["key"] for row in rows) == ["alpha", "beta"]
        warnings = _warnings(caplog)
        assert len(warnings) == 1, [record.getMessage() for record in warnings]
        assert str(locked) in warnings[0].getMessage()
        assert "Permission denied" in warnings[0].getMessage()

    def test_a_file_gone_before_its_stat_is_dropped_not_listed_as_a_phantom(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enumeration is cached, so a file removed after the walk still reaches
        the listing. Its stat fails before any read; that is the same answer as
        a failed read, not a row named after a path with nothing behind it."""
        root = tmp_path / "skills"
        _write_skill(root, "alpha")
        _write_skill(root, "beta")
        gone = _write_skill(root, "gone")
        loader = SkillsLoader(skills_path=root, install_builtins=False)
        assert {entry[0] for entry in loader._iter_visible(None)} == {"alpha", "beta", "gone"}
        # Force the stat path: a fingerprint hint from the walk would skip the
        # stat and fail at the read instead (covered above).
        monkeypatch.setattr(loader, "_catalog_fingerprint_hint", lambda project_dir: {})
        gone.unlink()

        with caplog.at_level(logging.WARNING, logger="kiro_crew.skills"):
            rows = loader.list_skills()

        assert sorted(row["key"] for row in rows) == ["alpha", "beta"]
        warnings = _warnings(caplog)
        assert len(warnings) == 1, [record.getMessage() for record in warnings]
        assert str(gone) in warnings[0].getMessage()
        assert "could not stat" in warnings[0].getMessage()

    def test_a_stored_row_naming_a_path_no_filesystem_can_hold_is_dropped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, read_branch: str
    ) -> None:
        """The search index is an agent-writable crew-home leaf, and a fresh
        process adopts its stored enumeration without walking. Both screens on
        a stored row are lexical, so a row whose key and path agree but carry a
        NUL byte is admitted -- and ``os.stat`` refuses such a path with
        ``ValueError``, not ``OSError``, before any syscall. That row must cost
        itself, not the index: the listing feeds every chat turn's context
        build and ``GET /api/skills``, and the poisoned snapshot stands until
        the next scheduled re-walk."""
        root = tmp_path / "skills"
        _write_skill(root, "alpha")
        _write_skill(root, "beta")
        first = SkillsLoader(skills_path=root, install_builtins=False)
        assert sorted(row["key"] for row in first.list_skills()) == ["alpha", "beta"]
        index = first._search_index
        assert index is not None
        scope = first._catalog_scope_id("")
        stored = index.catalog_snapshot(scope)
        assert stored is not None
        rows, _built_at = stored
        # The branch name keeps the two variants' keys apart: the warning bound
        # is process-wide and a failed stat has no fingerprint, so an identical
        # path would be "already warned about" by the time the second runs.
        forged_key = f"nul\x00{read_branch}"
        forged_path = str(root / forged_key / "SKILL.md")
        assert (
            index.store_catalog(
                scope, [*rows, (forged_key, forged_path, "")], epoch=index.catalog_epoch()
            )
            == "stored"
        )

        # A new process: no in-memory list, no walk fingerprints, so the stored
        # snapshot is adopted and the row takes the listing's own stat path.
        second = SkillsLoader(skills_path=root, install_builtins=False)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.skills"):
            listed = second.list_skills()

        assert sorted(row["key"] for row in listed) == ["alpha", "beta"], read_branch
        warnings = _warnings(caplog)
        assert len(warnings) == 1, [record.getMessage() for record in warnings]
        assert forged_path in warnings[0].getMessage()
        assert "could not stat" in warnings[0].getMessage()


class TestTheWarningIsBounded:
    def test_the_warning_cache_keeps_its_bound(self) -> None:
        """The bound is the whole point of routing the warning through a cache:
        the listing runs every few seconds, so an unbounded cache would grow by
        one entry per distinct bad (file, fingerprint, problem) for the life of
        the process. The count assertions above stay green with the bound
        gone; this pins it."""
        from kiro_crew.skill_runtime import listing

        assert listing._warn_unreadable_skill.cache_info().maxsize == 256


class TestThePerTurnReadersDegradeTheSameWay:
    """``get_triggered_skills`` runs on every message once ``skills.max_triggered``
    is above its shipped 0, and ``split_triggered`` / ``trigger_hint`` render its
    matches; all three read the same strict frontmatter. One misencoded skill
    must cost its own match, not the turn."""

    def _loader(self, tmp_path: Path) -> tuple[SkillsLoader, Path]:
        from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig

        root = tmp_path / "skills"
        _write_skill(root, "alpha", _TRIGGERED_SKILL_TEXT.format(name="alpha").encode("utf-8"))
        _write_skill(root, "beta", _TRIGGERED_SKILL_TEXT.format(name="beta").encode("utf-8"))
        broken = _write_skill(root, "broken", _UTF16_SKILL)
        loader = SkillsLoader(
            skills_path=root,
            install_builtins=False,
            config=KiroCrewConfig(skills=SkillsConfig(max_triggered=5)),
        )
        return loader, broken

    def test_the_matcher_returns_the_good_matches_and_warns_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        loader, broken = self._loader(tmp_path)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.skills"):
            matched = loader.get_triggered_skills("please handle the zebra enclosure")

        assert sorted(matched) == ["alpha", "beta"]
        warnings = _warnings(caplog)
        assert len(warnings) == 1, [record.getMessage() for record in warnings]
        assert str(broken) in warnings[0].getMessage()
        assert "utf-8" in warnings[0].getMessage().lower()

    def test_the_split_and_the_pointer_skip_the_bad_name_and_warn_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        loader, broken = self._loader(tmp_path)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.skills"):
            bodies, pointers = loader.split_triggered(["alpha", "broken", "beta"])
            hint = loader.trigger_hint(["broken", "alpha"])

        assert (bodies, pointers) == (["alpha", "beta"], [])
        assert "alpha" in hint
        assert "broken" not in hint
        warnings = _warnings(caplog)
        # The same file, the same problem: the bound holds across the two readers.
        assert len(warnings) == 1, [record.getMessage() for record in warnings]
        assert str(broken) in warnings[0].getMessage()

    def test_the_listing_and_the_matcher_warn_once_between_them(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One turn runs both: the context build lists, the matcher reads. Each
        keys its warning on a stat taken when its read fails -- the listing's
        own fingerprint is the walk's and can be stale, the matcher holds none
        -- so the two agree on the key. Unless they do, the same bad file is
        reported twice in its first turn."""
        loader, broken = self._loader(tmp_path)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.skills"):
            listed = loader.list_skills()
            matched = loader.get_triggered_skills("please handle the zebra enclosure")

        assert sorted(row["key"] for row in listed) == ["alpha", "beta"]
        assert sorted(matched) == ["alpha", "beta"]
        warnings = _warnings(caplog)
        assert len(warnings) == 1, [record.getMessage() for record in warnings]
        assert str(broken) in warnings[0].getMessage()


class TestAUtf8ByteOrderMarkIsNotMisencoding:
    def test_the_skill_lists_with_its_frontmatter_and_loads_without_the_mark(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, read_branch: str
    ) -> None:
        root = tmp_path / "skills"
        _write_skill(root, "alpha")
        _write_skill(root, "beta")
        _write_skill(root, "bom", _UTF8_BOM_SKILL)
        loader = SkillsLoader(skills_path=root, install_builtins=False)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.skills"):
            rows = loader.list_skills()

        assert sorted(row["key"] for row in rows) == ["alpha", "beta", "bom"], read_branch
        row = next(row for row in rows if row["key"] == "bom")
        # The mark must not hide the frontmatter fence: name and description
        # come from the file, not from the path-derived fallback.
        assert row["name"] == "bom"
        assert row["description"] == "bom description"
        assert _warnings(caplog) == []

        body = loader.load_skill("bom")
        assert body is not None
        assert "\ufeff" not in body
        assert body.startswith("---\nname: bom\n")
        assert "body of bom" in body

    def test_metadata_stored_before_the_decoder_change_is_rebuilt_on_upgrade(
        self, tmp_path: Path
    ) -> None:
        """The index persists derived metadata keyed by stat fingerprint and
        reuses it while the file is unchanged. A byte-order-marked file indexed
        by the previous decoder therefore holds a frontmatter-less row that an
        unchanged file would keep serving across restarts -- unless the schema
        version moves, which drops and rebuilds every derived row once."""
        import sqlite3

        root = tmp_path / "skills"
        _write_skill(root, "bom", _UTF8_BOM_SKILL)
        first = SkillsLoader(skills_path=root, install_builtins=False)
        index = first._search_index
        assert index is not None
        row = next(row for row in first.list_skills() if row["key"] == "bom")
        assert row["fingerprint"]
        # Plant what the previous decoder stored for this very file: the same
        # fingerprint, no frontmatter fields, under the schema version that
        # decoder shipped with. Spelled as the literal 6 on purpose: relative to
        # the current version, the seed would follow a revert of the bump and
        # the test would keep passing while deployed v6 rows stayed stale.
        assert index.store_metadata([(row["path"], row["fingerprint"], {"_catalog_key": "bom"})])
        assert index.metadata_snapshot()[row["path"]][1] == {"_catalog_key": "bom"}
        with sqlite3.connect(str(index._path)) as conn:
            conn.execute("UPDATE skill_index_schema SET version = ?", (6,))

        upgraded = SkillsLoader(skills_path=root, install_builtins=False)
        listed = next(row for row in upgraded.list_skills() if row["key"] == "bom")
        assert listed["description"] == "bom description"
        assert listed["name"] == "bom"
