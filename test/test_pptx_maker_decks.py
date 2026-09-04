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

import inspect
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

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

    def test_a_symlinked_deck_is_not_listed_or_read(self) -> None:
        """A link in the deck root must not make an OUTSIDE directory a deck.

        `iterdir()` yields the entry unresolved and `is_dir()` FOLLOWS symlinks, so a
        `20260101-linked -> /elsewhere/deck` entry passed the old check — and every
        reader then took the LINK as its own containment root. `_read_brief` is bounded
        relative to the deck dir it is handed, so it stayed "inside" a deck whose real
        location was outside the root, and served that file's contents to the dashboard.

        Asserting BOTH halves: the id is absent, and the outside content does not appear
        in any row. Checking only the id would still pass if the brief leaked onto a
        different row.
        """
        outside = self.tmp / "outside" / "20260101-secret"
        self._write(outside / "specs" / "brief.md", "CONFIDENTIAL out-of-root brief")
        self._write(outside / "deck.json", "{}")
        os.symlink(outside, self.root / "20260101-linked")

        # A real deck alongside it, so this cannot pass by listing nothing at all.
        real = self._deck("20260102-real")
        self._write(real / "specs" / "brief.md", "legit brief")

        rows = decks.list_decks()
        self.assertEqual([r["deckId"] for r in rows], ["20260102-real"])
        self.assertNotIn(
            "CONFIDENTIAL",
            json.dumps(rows),
            "out-of-root file contents reached the dashboard",
        )

    def test_a_symlinked_artifact_subdirectory_is_not_walked(self) -> None:
        """Containing the DECK dir was not enough — its subdirectories are walked too.

        `glob()`/`listdir()`/`is_dir()` all follow symlinks, so a deck shipping
        `slides -> ~/.aws/sso/cache` had that directory ENUMERATED and its filenames
        returned as slide slugs. The file contents stayed gated, but the *names* leaked,
        which is enough to disclose account and token identifiers. Demonstrated before
        the fix: a linked `slides` reported `['9c1e2f-token', 'botocore-client-id']`.
        """
        secret = self.tmp / "sso-cache"
        secret.mkdir()
        self._write(secret / "botocore-client-id.json", "{}")
        self._write(secret / "9c1e2f-token.json", "{}")
        pngs = self.tmp / "pngs"
        pngs.mkdir()
        (pngs / "page1-leak.png").write_bytes(b"x")

        deck = self._deck("20260101-linked-subdirs")
        self._write(deck / "deck.json", "{}")
        os.symlink(secret, deck / "slides")
        os.symlink(secret, deck / "compose")
        os.symlink(pngs, deck / "preview")

        row = next(r for r in decks.list_decks() if r["deckId"] == "20260101-linked-subdirs")
        self.assertEqual(row["slideCount"], 0, "a linked slides dir was counted")
        self.assertIsNone(row["thumbnailUrl"], "a linked preview dir produced a thumbnail")

        detail = decks.deck_detail("20260101-linked-subdirs")
        self.assertEqual(
            [s["slug"] for s in (detail or {}).get("slides", [])],
            [],
            "sensitive filenames were returned as slide slugs",
        )

    def test_a_real_deck_with_all_subdirectories_still_works(self) -> None:
        """The containment must not break the ordinary case — a fix that returned
        nothing for every deck would pass the leak test above."""
        deck = self._deck("20260102-normal")
        self._write(deck / "deck.json", "{}")
        self._write(deck / "slides" / "intro.json", "{}")
        self._write(deck / "specs" / "brief.md", "legit brief")
        self._write(deck / "compose" / "intro_1700000000.json", "{}")
        (deck / "preview").mkdir(parents=True, exist_ok=True)
        (deck / "preview" / "page1-intro.png").write_bytes(b"x")

        row = next(r for r in decks.list_decks() if r["deckId"] == "20260102-normal")
        self.assertEqual(row["slideCount"], 1)
        self.assertIsNotNone(row["thumbnailUrl"])
        detail = decks.deck_detail("20260102-normal")
        self.assertEqual([s["slug"] for s in (detail or {})["slides"]], ["intro"])
        self.assertIn("brief", (detail or {})["specs"])
        self.assertIn("slides", (detail or {})["updatedAt"])

    def test_the_listing_uses_the_shared_containment_gate(self) -> None:
        """Structural: the loop must ask `paths.resolve_deck_dir`, not re-derive it.

        The bug was a second, weaker containment check living beside the real one. A
        future edit that goes back to trusting the directory entry fails here.
        """
        source = inspect.getsource(decks.list_decks)
        # The CALL, not the name: the explanatory comment above the fix mentions
        # `resolve_deck_dir` too, so a bare substring check passed even with the call
        # removed (verified against reverted code). Match the invocation.
        self.assertIn("paths.resolve_deck_dir(", source)
        # And that the loop variable it iterates is not what the readers are handed —
        # `deck_dir = entry` was exactly the reverted shape.
        self.assertNotRegex(source, r"deck_dir\s*=\s*entry\b")

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

    def test_a_symlinked_brief_is_not_followed(self) -> None:
        """`brief.md` can be a SYMLINK, and `read_text` follows one.

        The deck root is user-configurable and its contents are agent-written, so a
        link to `~/.ssh/config` would have had its contents previewed straight onto
        the Deck list — which every visit to the app loads. Resolving through the
        shared containment helper applies the same sensitive-path gate every other
        file this app serves already passes.
        """
        secret = self.tmp / "outside-secret.txt"
        secret.write_text("PRIVATE KEY MATERIAL", encoding="utf-8")
        deck = self._deck("20260101-link")
        self._write(deck / "deck.json", "{}")
        specs = deck / "specs"
        specs.mkdir(parents=True, exist_ok=True)
        (specs / "brief.md").symlink_to(secret)

        rows = decks.list_decks()

        self.assertEqual(len(rows), 1, "the deck itself must still be listed")
        self.assertEqual(rows[0]["brief"], "")
        self.assertNotIn("PRIVATE KEY MATERIAL", json.dumps(rows))

    def test_thumbnail_url_when_a_preview_exists(self) -> None:
        deck = self._deck("20260101-thumb")
        self._write(deck / "deck.json", "{}")
        self._write(deck / "preview" / "page1-intro.png", "png")
        self.assertEqual(
            decks.list_decks()[0]["thumbnailUrl"],
            "preview/20260101-thumb/preview/page1-intro.png",
        )

    def test_listing_is_capped(self) -> None:
        # Shrink the cap instead of materializing MAX_DECKS+5 (505) real directories.
        # The break being tested is the same at 5; building 505 deck trees was ~1010
        # filesystem syscalls for no extra coverage.
        with mock.patch.object(decks, "MAX_DECKS", 5):
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

# An AKIA-shaped access key ID. Used for the deck-ID test specifically, because the
# SECRET above contains "/" and so is already refused by `SEGMENT_RE` as a directory
# name — a deck-ID test built on it passes without the redaction check and proves
# nothing. An access key ID is all `[A-Z0-9]`, so it IS a legal directory name and is
# the value that actually reached the dashboard.
_FAKE_AKIA = "AKIAIOSFODNN7EXAMPLE"


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

    def test_a_credential_shaped_deck_ID_is_refused_entirely(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """The deck ID is the one agent-authored field that cannot be redacted on the
        way out — it is the directory name, the `preview/<deckId>/...` URL segment and
        the handle every later request sends back, so rewriting it would break the deck.
        The deck is therefore REFUSED.

        This was the same leak one field over from `_deck_name` above: the display name
        was scrubbed while `deckId` carried the credential into the `/decks` response
        verbatim. Asserted on the whole serialized payload, not just the id, since the
        value also appears inside the artifact URLs.
        """
        root = tmp_path / "decks"
        root.mkdir()
        for name in (f"20260101-{_FAKE_AKIA}", "20260102-quarterly"):
            deck = root / name
            (deck / "specs").mkdir(parents=True)
            (deck / "deck.json").write_text("{}", encoding="utf-8")
            (deck / "specs" / "brief.md").write_text("ordinary brief", encoding="utf-8")
        monkeypatch.setenv(paths.DECK_ROOT_ENV, str(root))

        rows = decks.list_decks()

        assert [r["deckId"] for r in rows] == ["20260102-quarterly"]
        assert _FAKE_AKIA not in json.dumps(rows)
        # And the deck cannot be reached by asking for it directly either.
        assert decks.deck_detail(f"20260101-{_FAKE_AKIA}") is None

    def test_a_credential_shaped_slide_slug_is_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """The same leak as the deck ID, one level down — and reachable only by the
        FALLBACK slug path, which is why the outline's grammar is not a defence.

        `_slide_order` prefers `specs/outline.md`, whose `[a-z0-9-]+` slug grammar
        cannot spell an AKIA key. But with no outline it falls back to the slide
        FILENAMES, and `SEGMENT_RE` allows `[A-Za-z0-9._-]` — so an `AKIA…`-named
        slide file carried the credential into `/deck`, both as the slide's own label
        and inside every `preview/<deckId>/…` artifact URL built from it.

        Deliberately built with **no** `outline.md`, so the fallback is what supplies
        the slugs. An outline-based version of this test passes without the fix and
        proves nothing.

        The slide is SKIPPED rather than redacted, for the deck-ID reason above: the
        slug is the filename and the URL segment the browser sends back, so a
        scrubbed value would name a slide that resolves to nothing. The clean slide
        must still be served — a guard that dropped the whole deck would be a
        denial-of-service on one bad filename.
        """
        root = tmp_path / "decks"
        deck = root / "20260101-demo"
        (deck / "slides").mkdir(parents=True)
        (deck / "compose").mkdir(parents=True)
        (deck / "deck.json").write_text("{}", encoding="utf-8")
        for slug in (_FAKE_AKIA, "intro"):
            (deck / "slides" / f"{slug}.json").write_text("{}", encoding="utf-8")
            (deck / "compose" / f"{slug}.json").write_text("{}", encoding="utf-8")
        monkeypatch.setenv(paths.DECK_ROOT_ENV, str(root))

        detail = decks.deck_detail("20260101-demo")

        assert detail is not None
        assert [s["slug"] for s in detail["slides"]] == ["intro"]
        # Asserted on the whole payload, not just the slugs: the value also reached
        # the previewUrl / composeUrl strings built from it.
        assert _FAKE_AKIA not in json.dumps(detail)

    def test_a_credential_shaped_thumbnail_filename_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Third instance of one class, which is why the guard is in `_preview_url`.

        The engine names preview PNGs, so the filename is agent-authored and
        `SEGMENT_RE` permits `[A-Za-z0-9._-]` — an `AKIA…`-named PNG put the
        credential straight into `thumbnailUrl`. After `deckId` and the slide slug,
        this was the same defect a third time, so every artifact URL is now screened
        segment-by-segment at the one point they are all built.

        The deck itself must still be listed: only the offending LINK is dropped.
        """
        root = tmp_path / "decks"
        deck = root / "20260101-demo"
        (deck / "preview").mkdir(parents=True)
        (deck / "specs").mkdir(parents=True)
        (deck / "specs" / "brief.md").write_text("ordinary brief", encoding="utf-8")
        (deck / "deck.json").write_text("{}", encoding="utf-8")
        (deck / "preview" / f"{_FAKE_AKIA}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        monkeypatch.setenv(paths.DECK_ROOT_ENV, str(root))

        rows = decks.list_decks()

        assert [r["deckId"] for r in rows] == ["20260101-demo"]
        assert rows[0]["thumbnailUrl"] is None
        assert _FAKE_AKIA not in json.dumps(rows)

    def test_an_ordinary_thumbnail_is_still_linked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """The screen must not drop honest artifact URLs — a refusal-only test would
        pass against a builder that returned `None` for everything."""
        root = tmp_path / "decks"
        deck = root / "20260102-demo"
        (deck / "preview").mkdir(parents=True)
        (deck / "specs").mkdir(parents=True)
        (deck / "specs" / "brief.md").write_text("ordinary brief", encoding="utf-8")
        (deck / "deck.json").write_text("{}", encoding="utf-8")
        (deck / "preview" / "page_1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        monkeypatch.setenv(paths.DECK_ROOT_ENV, str(root))

        rows = decks.list_decks()

        assert rows[0]["thumbnailUrl"] == "preview/20260102-demo/preview/page_1.png"

    @unittest.skipUnless(hasattr(os, "link"), "needs hardlinks")
    def test_hardlinked_deck_files_are_not_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """A HARDLINK is what `contained_deck_file` cannot see.

        It resolves and re-checks containment, which stops a symlink — but a hardlink
        has no target to resolve: `is_symlink()` is False and `resolve()` returns the
        path itself, so every path-based check passes. The deck tree is agent-written,
        so `os.link("~/.ssh/config", "specs/brief.md")` previewed SSH configuration
        onto the Deck list, which every visit to the app loads. Verified before the fix.

        All THREE deck-tree readers are covered in one test because they share one
        guarded reader (`_read_deck_text`) — the point of centralizing it. An honest
        deck is asserted alongside, since a reader that refused everything would
        satisfy the refusals while breaking the app.
        """
        root = tmp_path / "decks"
        secret = tmp_path / "outside_secret"
        secret.write_text("Host prod\n  User SECRETUSER\n", encoding="utf-8")

        evil = root / "20260101-evil"
        (evil / "specs").mkdir(parents=True)
        for target in (evil / "deck.json", evil / "specs" / "brief.md",
                       evil / "specs" / "outline.md"):
            os.link(secret, target)

        good = root / "20260102-good"
        (good / "specs").mkdir(parents=True)
        (good / "deck.json").write_text(json.dumps({"name": "Quarterly"}), encoding="utf-8")
        (good / "specs" / "brief.md").write_text("a real brief", encoding="utf-8")

        monkeypatch.setenv(paths.DECK_ROOT_ENV, str(root))
        rows = decks.list_decks()

        assert "SECRETUSER" not in json.dumps(rows)
        by_id = {r["deckId"]: r for r in rows}
        # The hostile deck degrades to its directory name and an empty brief.
        assert by_id["20260101-evil"]["name"] == "20260101-evil"
        assert by_id["20260101-evil"]["brief"] == ""
        # The honest deck is unaffected.
        assert by_id["20260102-good"]["name"] == "Quarterly"
        assert by_id["20260102-good"]["brief"] == "a real brief"

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

    def test_a_credential_in_the_directory_name_is_redacted(self, tmp_path: Path):
        """The FALLBACK is agent-controlled too.

        The engine creates deck directories from the model's own name for the deck, so
        a deck with no `deck.json` yet — every specs-only deck still in progress —
        surfaced its directory name to the Deck list. Redacting the metadata path and
        not this one left the identical leak on the other branch.
        """
        # An ACCESS KEY ID, not `_FAKE_AWS_SECRET`: the secret-key fixture contains
        # `/`, so it cannot appear in a single path segment. A key id is the shape a
        # directory name can actually carry, which is what makes this reachable.
        key_id = "AKIAIOSFODNN7EXAMPLE"
        deck = tmp_path / f"deck-{key_id}"
        deck.mkdir()
        # No deck.json at all: the directory name is what gets returned.
        name = decks._deck_name(deck)
        assert key_id not in name
        assert "REDACTED" in name

    def test_an_ordinary_directory_name_is_unchanged(self, tmp_path: Path):
        deck = tmp_path / "20260802-q3-review"
        deck.mkdir()
        assert decks._deck_name(deck) == "20260802-q3-review"
