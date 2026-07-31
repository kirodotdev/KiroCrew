"""Deck discovery and detail tests.

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

The deck directory layout is the ENGINE's, not ours, so these tests build real
fixture trees in that shape and assert the two behaviours the studio depends on:

* an in-progress deck is listed (the user must see a deck the moment the agent
  starts writing its brief, not only once slides exist);
* the newest compose epoch per slug wins, because the engine writes a new file on
  every recompose instead of overwriting — picking the wrong one shows the first
  render forever.

No engine, no subprocess.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from kiro_crew.apps.builtins.pptx_maker.backend import decks, paths


class _DeckTree(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "decks"
        self.root.mkdir(parents=True)
        self._prev = os.environ.get(paths.DECK_ROOT_ENV)
        os.environ[paths.DECK_ROOT_ENV] = str(self.root)

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop(paths.DECK_ROOT_ENV, None)
        else:
            os.environ[paths.DECK_ROOT_ENV] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _deck(self, deck_id: str) -> Path:
        deck = self.root / deck_id
        deck.mkdir(parents=True, exist_ok=True)
        return deck

    def _write(self, path: Path, content: str = "x") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


class TestListDecks(_DeckTree):
    def test_empty_root(self) -> None:
        self.assertEqual(decks.list_decks(), [])

    def test_missing_root_is_empty_not_an_error(self) -> None:
        os.environ[paths.DECK_ROOT_ENV] = str(self.tmp / "absent")
        self.assertEqual(decks.list_decks(), [])

    def test_in_progress_deck_is_listed(self) -> None:
        """A deck with only a brief must appear — that is what makes the studio
        show the deck while the agent is still planning it."""
        deck = self._deck("20260101-early")
        self._write(deck / "specs" / "brief.md", "the brief")
        rows = decks.list_decks()
        self.assertEqual([r["deckId"] for r in rows], ["20260101-early"])
        self.assertEqual(rows[0]["slideCount"], 0)
        self.assertEqual(rows[0]["brief"], "the brief")

    def test_bare_empty_directory_is_skipped(self) -> None:
        # The engine creates the directory before writing anything, so a bare dir
        # is not yet a deck and would otherwise show as an empty row.
        self._deck("20260101-nothing")
        self.assertEqual(decks.list_decks(), [])

    def test_dotted_and_underscored_dirs_are_skipped(self) -> None:
        for name in (".hidden", "_scratch"):
            self._write(self._deck(name) / "deck.json", "{}")
        self.assertEqual(decks.list_decks(), [])

    def test_slide_count_and_pptx_url(self) -> None:
        deck = self._deck("20260102-full")
        self._write(deck / "slides" / "intro.json", "{}")
        self._write(deck / "slides" / "outro.json", "{}")
        (deck / "output.pptx").write_bytes(b"PK\x03\x04")
        row = decks.list_decks()[0]
        self.assertEqual(row["slideCount"], 2)
        self.assertEqual(row["pptxUrl"], "preview/20260102-full/output.pptx")

    def test_name_comes_from_deck_json(self) -> None:
        deck = self._deck("20260103-named")
        self._write(deck / "deck.json", json.dumps({"name": "Quarterly Review"}))
        self.assertEqual(decks.list_decks()[0]["name"], "Quarterly Review")

    def test_malformed_deck_json_falls_back_to_directory_name(self) -> None:
        deck = self._deck("20260104-broken")
        self._write(deck / "deck.json", "{not json")
        self.assertEqual(decks.list_decks()[0]["name"], "20260104-broken")

    def test_newest_deck_first(self) -> None:
        for deck_id in ("20260101-a", "20260305-b", "20260202-c"):
            self._write(self._deck(deck_id) / "deck.json", "{}")
        self.assertEqual(
            [r["deckId"] for r in decks.list_decks()],
            ["20260305-b", "20260202-c", "20260101-a"],
        )

    def test_brief_is_truncated(self) -> None:
        deck = self._deck("20260101-long")
        self._write(deck / "specs" / "brief.md", "y" * (decks.BRIEF_PREVIEW_CHARS + 500))
        self.assertEqual(len(decks.list_decks()[0]["brief"]), decks.BRIEF_PREVIEW_CHARS)

    def test_thumbnail_url_when_a_preview_exists(self) -> None:
        deck = self._deck("20260101-thumb")
        self._write(deck / "deck.json", "{}")
        self._write(deck / "preview" / "page1-intro.png", "png")
        self.assertEqual(
            decks.list_decks()[0]["thumbnailUrl"],
            "preview/20260101-thumb/preview/page1-intro.png",
        )

    def test_listing_is_capped(self) -> None:
        for i in range(decks.MAX_DECKS + 5):
            self._write(self._deck(f"2026-{i:04d}") / "deck.json", "{}")
        self.assertEqual(len(decks.list_decks()), decks.MAX_DECKS)


class TestDeckDetail(_DeckTree):
    def test_unknown_deck_is_none(self) -> None:
        self.assertIsNone(decks.deck_detail("nope"))

    def test_traversal_deck_id_is_none(self) -> None:
        self.assertIsNone(decks.deck_detail("../.."))

    def test_slide_order_follows_the_outline(self) -> None:
        """The outline is what the user approved, so it — not directory sort
        order — decides the order slides are shown in."""
        deck = self._deck("20260101-order")
        self._write(
            deck / "specs" / "outline.md",
            "# Outline\n- [zebra] last alphabetically\n- [apple] first alphabetically\n",
        )
        self._write(deck / "slides" / "zebra.json", "{}")
        self._write(deck / "slides" / "apple.json", "{}")
        detail = decks.deck_detail("20260101-order")
        assert detail is not None
        self.assertEqual([s["slug"] for s in detail["slides"]], ["zebra", "apple"])

    def test_slide_order_falls_back_to_slide_files(self) -> None:
        deck = self._deck("20260101-noout")
        self._write(deck / "slides" / "b.json", "{}")
        self._write(deck / "slides" / "a.json", "{}")
        detail = decks.deck_detail("20260101-noout")
        assert detail is not None
        self.assertEqual([s["slug"] for s in detail["slides"]], ["a", "b"])

    def test_outline_slug_without_a_slide_file_is_dropped(self) -> None:
        # A planned-but-not-yet-composed slide has no render payload, so listing
        # it would put an unfillable placeholder in the grid.
        deck = self._deck("20260101-partial")
        self._write(deck / "specs" / "outline.md", "- [done]\n- [planned]\n")
        self._write(deck / "slides" / "done.json", "{}")
        detail = decks.deck_detail("20260101-partial")
        assert detail is not None
        self.assertEqual([s["slug"] for s in detail["slides"]], ["done"])

    def test_newest_compose_epoch_wins(self) -> None:
        """The engine writes a NEW <slug>_<epoch>.json per recompose rather than
        overwriting, so the highest epoch is the current render."""
        deck = self._deck("20260101-epoch")
        self._write(deck / "slides" / "intro.json", "{}")
        self._write(deck / "compose" / "intro_1700000000.json", "{}")
        self._write(deck / "compose" / "intro_1700009999.json", "{}")
        detail = decks.deck_detail("20260101-epoch")
        assert detail is not None
        self.assertEqual(
            detail["slides"][0]["composeUrl"],
            "preview/20260101-epoch/compose/intro_1700009999.json",
        )

    def test_newest_defs_epoch_wins(self) -> None:
        deck = self._deck("20260101-defs")
        self._write(deck / "slides" / "intro.json", "{}")
        self._write(deck / "compose" / "defs_1700000000.json", "{}")
        self._write(deck / "compose" / "defs_1700005555.json", "{}")
        detail = decks.deck_detail("20260101-defs")
        assert detail is not None
        self.assertEqual(detail["defsUrl"], "preview/20260101-defs/compose/defs_1700005555.json")

    def test_preview_png_matched_by_page_number(self) -> None:
        deck = self._deck("20260101-png")
        self._write(deck / "specs" / "outline.md", "- [one]\n- [two]\n")
        self._write(deck / "slides" / "one.json", "{}")
        self._write(deck / "slides" / "two.json", "{}")
        self._write(deck / "preview" / "page2-two.png", "png")
        detail = decks.deck_detail("20260101-png")
        assert detail is not None
        self.assertIsNone(detail["slides"][0]["previewUrl"])
        self.assertEqual(
            detail["slides"][1]["previewUrl"], "preview/20260101-png/preview/page2-two.png"
        )

    def test_spec_tabs_and_updated_timestamps(self) -> None:
        deck = self._deck("20260101-specs")
        self._write(deck / "specs" / "brief.md", "b")
        self._write(deck / "specs" / "outline.md", "- [x]\n")
        self._write(deck / "specs" / "art-direction.html", "<html></html>")
        detail = decks.deck_detail("20260101-specs")
        assert detail is not None
        self.assertEqual(set(detail["specs"]), {"brief", "outline", "artDirection"})
        # updatedAt is what the viewer diffs across polls to auto-focus the tab
        # that just changed, so every present spec must carry a timestamp.
        for key in ("brief", "outline", "artDirection"):
            self.assertGreater(detail["updatedAt"][key], 0)

    def test_art_direction_markdown_variant_is_accepted(self) -> None:
        deck = self._deck("20260101-artmd")
        self._write(deck / "specs" / "art-direction.md", "# art")
        detail = decks.deck_detail("20260101-artmd")
        assert detail is not None
        self.assertEqual(
            detail["specs"]["artDirection"], "preview/20260101-artmd/specs/art-direction.md"
        )

    def test_served_urls_are_relative_never_filesystem_paths(self) -> None:
        """Every URL handed to the browser must be a relative API path — an
        absolute filesystem path would invite the frontend to ask for it back."""
        deck = self._deck("20260101-rel")
        self._write(deck / "slides" / "intro.json", "{}")
        self._write(deck / "compose" / "intro_1.json", "{}")
        self._write(deck / "specs" / "brief.md", "b")
        (deck / "output.pptx").write_bytes(b"PK")
        detail = decks.deck_detail("20260101-rel")
        assert detail is not None
        urls = [
            detail["pptxUrl"],
            detail["defsUrl"],
            *detail["specs"].values(),
            *[s["composeUrl"] for s in detail["slides"]],
        ]
        for url in urls:
            if url is None:
                continue
            self.assertTrue(url.startswith("preview/"), url)
            self.assertNotIn(str(self.tmp), url)

    def test_absolute_paths_are_only_the_reveal_targets(self) -> None:
        # dirPath/pptxPath exist solely to feed the dashboard's reveal/open
        # endpoint, which re-validates them; nothing else is absolute.
        deck = self._deck("20260101-abs")
        self._write(deck / "deck.json", "{}")
        (deck / "output.pptx").write_bytes(b"PK")
        detail = decks.deck_detail("20260101-abs")
        assert detail is not None
        self.assertTrue(detail["dirPath"].startswith(str(self.root.resolve())))
        self.assertTrue(str(detail["pptxPath"]).endswith("output.pptx"))


if __name__ == "__main__":
    unittest.main()


_FAKE_AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


class TestAgentMetadataIsRedacted:
    """Deck names and brief previews are agent-authored text bound for the UI.

    ``AUTOSDE backend-security-controls`` requires both redaction passes before
    model output reaches an external surface, and both of these are returned by
    ``/decks`` and ``/deck`` — so a credential the agent wrote into a deck's own
    metadata would otherwise be echoed straight into the dashboard.
    """

    def test_a_credential_in_the_deck_name_is_redacted(self, tmp_path: Path):
        deck = tmp_path / "deck-1"
        deck.mkdir()
        (deck / "deck.json").write_text(
            json.dumps({"name": f"Q3 review {_FAKE_AWS_SECRET}"}), encoding="utf-8"
        )
        name = decks._deck_name(deck)
        assert _FAKE_AWS_SECRET not in name
        assert "REDACTED" in name

    def test_a_credential_in_the_brief_preview_is_redacted(self, tmp_path: Path):
        deck = tmp_path / "deck-2"
        (deck / "specs").mkdir(parents=True)
        (deck / "specs" / "brief.md").write_text(
            f"# Brief\naws_secret_access_key = {_FAKE_AWS_SECRET}\n", encoding="utf-8"
        )
        preview = decks._read_brief(deck)
        assert _FAKE_AWS_SECRET not in preview
        assert "REDACTED" in preview

    def test_redaction_runs_before_the_preview_truncation(self, tmp_path: Path):
        """Slicing first could cut a credential in half and let the tail through."""
        deck = tmp_path / "deck-3"
        (deck / "specs").mkdir(parents=True)
        padding = "x" * (decks.BRIEF_PREVIEW_CHARS - 20)
        (deck / "specs" / "brief.md").write_text(
            f"{padding}{_FAKE_AWS_SECRET}\n", encoding="utf-8"
        )
        preview = decks._read_brief(deck)
        assert _FAKE_AWS_SECRET not in preview
        assert len(preview) <= decks.BRIEF_PREVIEW_CHARS

    def test_an_ordinary_name_is_unchanged(self, tmp_path: Path):
        deck = tmp_path / "deck-4"
        deck.mkdir()
        (deck / "deck.json").write_text(json.dumps({"name": "Q3 review"}), encoding="utf-8")
        assert decks._deck_name(deck) == "Q3 review"
