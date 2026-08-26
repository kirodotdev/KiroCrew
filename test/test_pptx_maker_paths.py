"""Path-containment tests — the app's security boundary.

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

Deck artifacts are SERVED to a browser from a directory the presentation engine
writes into, so ``paths.resolve_*`` is the one place a crafted request path could
turn into an arbitrary file read. These tests pin the two independent guards
(per-segment allow-list, post-``resolve()`` containment) and the specific bypasses
each one exists to stop.

No engine, no subprocess, no network — pure filesystem fixtures.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from kiro_crew.apps.builtins.pptx_maker.backend import paths


class _DeckRootFixture(unittest.TestCase):
    """Base fixture: a temp deck root with one deck in it, pinned via the env override."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "decks"
        self.deck = self.root / "20260101-demo"
        (self.deck / "specs").mkdir(parents=True)
        (self.deck / "specs" / "brief.md").write_text("a brief", encoding="utf-8")
        (self.deck / "output.pptx").write_bytes(b"PK\x03\x04")
        self._prev = os.environ.get(paths.DECK_ROOT_ENV)
        os.environ[paths.DECK_ROOT_ENV] = str(self.root)

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop(paths.DECK_ROOT_ENV, None)
        else:
            os.environ[paths.DECK_ROOT_ENV] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestSegmentGrammar(unittest.TestCase):
    """The per-segment allow-list is the primary guard — nothing traversal-shaped
    may survive it, so the containment check below is only ever a backstop."""

    def test_accepts_ordinary_names(self) -> None:
        for name in ("brief.md", "page1-slide.png", "defs_1700000000.json", "a-b_c.1"):
            self.assertIsNotNone(paths.SEGMENT_RE.match(name), name)

    def test_rejects_traversal_and_separators(self) -> None:
        for name in ("..", ".", "", "a/b", "a\\b", "/etc", "..hidden", ".ssh"):
            self.assertIsNone(paths.SEGMENT_RE.match(name), name)

    def test_split_rejects_any_bad_segment(self) -> None:
        # One bad segment rejects the WHOLE path — a partial accept would let
        # "specs/../../secret" through on its good prefix.
        self.assertIsNone(paths.split_segments("specs/../secret"))
        self.assertIsNone(paths.split_segments("specs/.ssh/id_rsa"))
        self.assertIsNone(paths.split_segments(""))
        self.assertIsNone(paths.split_segments("/"))

    def test_split_drops_noise_segments(self) -> None:
        self.assertEqual(paths.split_segments("specs//./brief.md"), ["specs", "brief.md"])


class TestResolveDeckDir(_DeckRootFixture):
    def test_resolves_a_real_deck(self) -> None:
        resolved = paths.resolve_deck_dir("20260101-demo")
        self.assertEqual(resolved, self.deck.resolve())

    def test_deck_root_itself_is_not_a_deck(self) -> None:
        # "." would resolve to the root; a caller asking for a deck must land
        # strictly inside it, or the whole tree becomes browsable as one deck.
        self.assertIsNone(paths.resolve_deck_dir("."))
        self.assertIsNone(paths.resolve_deck_dir(""))

    def test_traversal_is_refused(self) -> None:
        self.assertIsNone(paths.resolve_deck_dir(".."))
        self.assertIsNone(paths.resolve_deck_dir("../decks"))
        self.assertIsNone(paths.resolve_deck_dir("/etc"))

    def test_missing_deck_is_none(self) -> None:
        self.assertIsNone(paths.resolve_deck_dir("does-not-exist"))

    def test_file_is_not_a_deck_dir(self) -> None:
        (self.root / "notadir").write_text("x", encoding="utf-8")
        self.assertIsNone(paths.resolve_deck_dir("notadir"))


class TestResolveDeckFile(_DeckRootFixture):
    def test_resolves_a_nested_artifact(self) -> None:
        resolved = paths.resolve_deck_file("20260101-demo", "specs/brief.md")
        self.assertEqual(resolved, (self.deck / "specs" / "brief.md").resolve())

    def test_traversal_out_of_the_deck_is_refused(self) -> None:
        secret = self.tmp / "secret.txt"
        secret.write_text("nope", encoding="utf-8")
        # Both the segment grammar and (were it bypassed) containment refuse this.
        self.assertIsNone(paths.resolve_deck_file("20260101-demo", "../secret.txt"))
        self.assertIsNone(paths.resolve_deck_file("20260101-demo", "specs/../../secret.txt"))

    def test_symlink_escaping_the_deck_is_refused(self) -> None:
        """The segment allow-list cannot see through a symlink, so containment
        after ``resolve()`` is what stops this one — the reason both guards exist."""
        outside = self.tmp / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.deck / "escape.md"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this platform")
        self.assertIsNone(paths.resolve_deck_file("20260101-demo", "escape.md"))

    def test_directory_is_not_served_as_a_file(self) -> None:
        self.assertIsNone(paths.resolve_deck_file("20260101-demo", "specs"))

    def test_missing_file_is_none(self) -> None:
        self.assertIsNone(paths.resolve_deck_file("20260101-demo", "specs/nope.md"))

    def test_unknown_deck_short_circuits(self) -> None:
        self.assertIsNone(paths.resolve_deck_file("other", "specs/brief.md"))


class TestResolveLibraryFile(unittest.TestCase):
    """Shared by every style/template read AND write, so a bad name can never
    reach a filesystem call regardless of which verb the request used."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.lib = self.tmp / "styles"
        self.lib.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resolves_a_name_to_a_suffixed_path(self) -> None:
        resolved = paths.resolve_library_file(self.lib, "my-style", ".html")
        self.assertEqual(resolved, (self.lib / "my-style.html").resolve())

    def test_target_need_not_exist(self) -> None:
        # A create/rename target does not exist yet — resolution must still work
        # or importing a new style would be impossible.
        self.assertIsNotNone(paths.resolve_library_file(self.lib, "brand-new", ".html"))

    def test_rejects_traversal_names(self) -> None:
        for name in ("..", "../evil", "a/b", "", ".hidden"):
            self.assertIsNone(paths.resolve_library_file(self.lib, name, ".html"), name)


class TestDeckRootResolution(_DeckRootFixture):
    def test_env_override_wins(self) -> None:
        self.assertEqual(paths.deck_root(), self.root.resolve())

    def test_engine_config_is_used_when_no_override(self) -> None:
        os.environ.pop(paths.DECK_ROOT_ENV, None)
        configured = self.tmp / "from-config"
        configured.mkdir()
        prev_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "cfg")
        try:
            config_path = paths.engine_config_path()
            config_path.parent.mkdir(parents=True, exist_ok=True)
            # Serialized with json.dumps, not "%s"-interpolated: a Windows temp
            # path is full of backslashes, so hand-rolling the JSON produced an
            # invalid \escape, read_engine_config() swallowed it as {}, and this
            # asserted the ~/Documents FALLBACK instead of the configured dir.
            # Both production writers (routes.save, provision._seed_deck_root)
            # use json.dumps, so this now matches what the app actually writes.
            config_path.write_text(
                json.dumps({"output_dir": str(configured)}), encoding="utf-8"
            )
            self.assertEqual(paths.deck_root(), configured.resolve())
        finally:
            if prev_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = prev_xdg

    def test_malformed_engine_config_falls_back(self) -> None:
        """A corrupt engine config must not raise — the UI still needs a path to
        show, and a broken config is a recoverable state, not a 500."""
        os.environ.pop(paths.DECK_ROOT_ENV, None)
        prev_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "cfg2")
        try:
            config_path = paths.engine_config_path()
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text("{not json", encoding="utf-8")
            self.assertEqual(
                paths.deck_root(), (Path.home() / paths.ENGINE_DEFAULT_DECK_ROOT).resolve()
            )
        finally:
            if prev_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = prev_xdg


if __name__ == "__main__":
    unittest.main()
