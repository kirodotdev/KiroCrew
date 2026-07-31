"""Style / template library tests.

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

The library is the only part of this app that WRITES on a browser request, so the
tests cover the validation ladder (name grammar, content sniffing, size caps,
collision refusal) and the state bookkeeping that must follow a rename or delete
— a pinned style that keeps its old name after a rename silently stops being
applied.

The engine is mocked at the ``engine.user_config_dir`` / ``engine.load_lists``
boundary: never a real subprocess.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kiro_crew.apps.builtins.pptx_maker.backend import engine, library


class _LibraryFixture(unittest.TestCase):
    """A temp engine user-config dir, with the engine bridge mocked out."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.config_dir = self.tmp / "sdpm"
        self.config_dir.mkdir(parents=True)
        self._patches = [
            mock.patch.object(engine, "user_config_dir", return_value=self.config_dir),
            mock.patch.object(
                engine,
                "user_subdir",
                side_effect=lambda sub: self.config_dir / sub,
            ),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self._patches:
            patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self) -> dict:
        path = self.config_dir / "state.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_state(self, data: dict) -> None:
        (self.config_dir / "state.json").write_text(json.dumps(data), encoding="utf-8")


class TestCoverHtml(unittest.TestCase):
    """The library thumbnail shows the FIRST slide only — a style document can
    hold a dozen, and rendering all of them makes every thumbnail a stack."""

    def test_extracts_only_the_first_slide(self) -> None:
        html = (
            "<html><head><style>.slide{color:red}</style></head><body>"
            '<div class="slide">ONE</div><div class="slide">TWO</div></body></html>'
        )
        cover = library.cover_html(html)
        self.assertIn("ONE", cover)
        self.assertNotIn("TWO", cover)

    def test_preserves_the_head_so_styling_survives(self) -> None:
        html = '<html><head><style>.slide{color:red}</style></head><body><div class="slide">A</div></body></html>'
        self.assertIn(".slide{color:red}", library.cover_html(html))

    def test_single_slide_document(self) -> None:
        html = '<html><body><div class="slide">ONLY</div></body></html>'
        self.assertIn("ONLY", library.cover_html(html))

    def test_document_with_no_slide_markup_is_passed_through(self) -> None:
        self.assertIn("plain", library.cover_html("<html><body>plain</body></html>"))

    def test_injects_a_body_reset(self) -> None:
        # Style documents ship their own page padding/zoom; without the reset the
        # thumbnail iframe shows that chrome instead of the slide.
        self.assertIn("margin:0!important", library.cover_html("<html></html>"))


class TestImportStyle(_LibraryFixture):
    def test_writes_a_new_style(self) -> None:
        status, payload = library.import_style("brand", "<html>x</html>")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"imported": "brand"})
        self.assertTrue((self.config_dir / "styles" / "brand.html").is_file())

    def test_rejects_a_bad_name(self) -> None:
        for name in ("../evil", "a/b", "", ".hidden"):
            status, _ = library.import_style(name, "<html></html>")
            self.assertEqual(status, 400, name)

    def test_rejects_non_html_content(self) -> None:
        status, payload = library.import_style("plain", "just text")
        self.assertEqual(status, 400)
        self.assertIn("HTML", payload["error"])

    def test_rejects_an_oversized_body(self) -> None:
        big = "<html>" + "x" * (library.MAX_STYLE_BYTES + 1)
        status, _ = library.import_style("big", big)
        self.assertEqual(status, 413)

    def test_refuses_to_overwrite(self) -> None:
        library.import_style("brand", "<html>1</html>")
        status, payload = library.import_style("brand", "<html>2</html>")
        self.assertEqual(status, 409)
        # The original content must survive a refused import.
        self.assertIn("1", (self.config_dir / "styles" / "brand.html").read_text(encoding="utf-8"))

    def test_engine_not_ready_is_503(self) -> None:
        with mock.patch.object(engine, "user_subdir", return_value=None):
            status, payload = library.import_style("brand", "<html></html>")
        self.assertEqual(status, 503)
        self.assertIn("engine", payload["error"])


class TestStyleLifecycle(_LibraryFixture):
    def test_delete(self) -> None:
        library.import_style("gone", "<html></html>")
        status, payload = library.delete_style("gone")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"deleted": "gone"})
        self.assertFalse((self.config_dir / "styles" / "gone.html").exists())

    def test_delete_missing_is_404(self) -> None:
        status, _ = library.delete_style("never")
        self.assertEqual(status, 404)

    def test_delete_drops_the_pin(self) -> None:
        library.import_style("pinned", "<html></html>")
        self._write_state({"pinned_styles": ["pinned", "other"]})
        library.delete_style("pinned")
        self.assertEqual(self._state()["pinned_styles"], ["other"])

    def test_rename(self) -> None:
        library.import_style("old", "<html>content</html>")
        status, payload = library.rename_style("old", "new")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"renamed": {"from": "old", "to": "new"}})
        self.assertFalse((self.config_dir / "styles" / "old.html").exists())
        self.assertIn(
            "content", (self.config_dir / "styles" / "new.html").read_text(encoding="utf-8")
        )

    def test_rename_carries_the_pin(self) -> None:
        """A pin that keeps the old name after a rename silently stops applying —
        the style is still "pinned" in state but no such file exists."""
        library.import_style("old", "<html></html>")
        self._write_state({"pinned_styles": ["old"]})
        library.rename_style("old", "new")
        self.assertEqual(self._state()["pinned_styles"], ["new"])

    def test_rename_onto_an_existing_name_is_409(self) -> None:
        library.import_style("a", "<html>A</html>")
        library.import_style("b", "<html>B</html>")
        status, _ = library.rename_style("a", "b")
        self.assertEqual(status, 409)
        self.assertIn("A", (self.config_dir / "styles" / "a.html").read_text(encoding="utf-8"))
        self.assertIn("B", (self.config_dir / "styles" / "b.html").read_text(encoding="utf-8"))

    def test_rename_rejects_a_bad_target_name(self) -> None:
        library.import_style("ok", "<html></html>")
        status, _ = library.rename_style("ok", "../escape")
        self.assertEqual(status, 400)
        self.assertTrue((self.config_dir / "styles" / "ok.html").is_file())

    def test_rename_missing_source_is_404(self) -> None:
        status, _ = library.rename_style("absent", "target")
        self.assertEqual(status, 404)


class TestPinStyle(_LibraryFixture):
    def test_pin_then_unpin(self) -> None:
        status, payload = library.pin_style("brand", True)
        self.assertEqual(status, 200)
        self.assertEqual(payload["pinnedStyles"], ["brand"])
        status, payload = library.pin_style("brand", False)
        self.assertEqual(payload["pinnedStyles"], [])

    def test_pin_is_idempotent(self) -> None:
        library.pin_style("brand", True)
        _, payload = library.pin_style("brand", True)
        self.assertEqual(payload["pinnedStyles"], ["brand"])

    def test_pin_rejects_a_bad_name(self) -> None:
        status, _ = library.pin_style("../evil", True)
        self.assertEqual(status, 400)

    def test_pin_survives_a_corrupt_state_file(self) -> None:
        # A torn state.json must not make pinning fail — the app rewrites it.
        (self.config_dir / "state.json").write_text("{not json", encoding="utf-8")
        status, payload = library.pin_style("brand", True)
        self.assertEqual(status, 200)
        self.assertEqual(payload["pinnedStyles"], ["brand"])


class TestTemplates(_LibraryFixture):
    _PPTX = b"PK\x03\x04rest-of-a-zip"

    def setUp(self) -> None:
        super().setUp()
        self.analyze = mock.patch.object(
            engine, "analyze_template", return_value={"name": "deck", "layout_count": 3}
        )
        self.analyze.start()

    def tearDown(self) -> None:
        self.analyze.stop()
        super().tearDown()

    def test_import_writes_and_analyzes(self) -> None:
        status, payload = library.import_template("deck", self._PPTX, "corporate")
        self.assertEqual(status, 200)
        self.assertEqual(payload["imported"], "deck")
        self.assertEqual(payload["metadata"]["layout_count"], 3)
        self.assertTrue((self.config_dir / "templates" / "deck.pptx").is_file())

    def test_rejects_a_non_pptx_upload(self) -> None:
        status, payload = library.import_template("deck", b"<html>nope", "")
        self.assertEqual(status, 400)
        self.assertIn(".pptx", payload["error"])
        self.assertFalse((self.config_dir / "templates" / "deck.pptx").exists())

    def test_rejects_an_oversized_upload(self) -> None:
        status, _ = library.import_template("deck", b"PK" + b"0" * library.MAX_TEMPLATE_BYTES, "")
        self.assertEqual(status, 413)

    def test_rejects_a_bad_name(self) -> None:
        status, _ = library.import_template("../evil", self._PPTX, "")
        self.assertEqual(status, 400)

    def test_refuses_to_overwrite(self) -> None:
        library.import_template("deck", self._PPTX, "")
        status, _ = library.import_template("deck", self._PPTX, "")
        self.assertEqual(status, 409)

    def test_import_survives_a_failed_analysis(self) -> None:
        """Analysis is best effort: an un-analyzed template still works, so a
        failure must not undo an import the user can already see on disk."""
        with mock.patch.object(engine, "analyze_template", return_value={"description": "x"}):
            status, payload = library.import_template("deck", self._PPTX, "x")
        self.assertEqual(status, 200)
        self.assertTrue((self.config_dir / "templates" / "deck.pptx").is_file())
        self.assertEqual(payload["metadata"], {"description": "x"})

    def test_delete_drops_cached_metadata(self) -> None:
        library.import_template("deck", self._PPTX, "")
        self._write_state({"template_metadata": {"deck": {"name": "deck"}, "keep": {}}})
        status, _ = library.delete_template("deck")
        self.assertEqual(status, 200)
        self.assertEqual(list(self._state()["template_metadata"]), ["keep"])

    def test_rename_carries_metadata_across(self) -> None:
        library.import_template("old", self._PPTX, "")
        self._write_state({"template_metadata": {"old": {"name": "old", "layouts": 4}}})
        status, _ = library.rename_template("old", "new")
        self.assertEqual(status, 200)
        metadata = self._state()["template_metadata"]
        self.assertNotIn("old", metadata)
        self.assertEqual(metadata["new"], {"name": "new", "layouts": 4})

    def test_rename_onto_an_existing_name_is_409(self) -> None:
        library.import_template("a", self._PPTX, "")
        library.import_template("b", self._PPTX, "")
        status, _ = library.rename_template("a", "b")
        self.assertEqual(status, 409)


class TestListing(_LibraryFixture):
    def test_styles_carry_a_cover_thumbnail(self) -> None:
        styles_dir = self.config_dir / "styles"
        styles_dir.mkdir(parents=True, exist_ok=True)
        (styles_dir / "brand.html").write_text(
            '<html><body><div class="slide">COVER</div></body></html>', encoding="utf-8"
        )
        with mock.patch.object(
            engine,
            "load_lists",
            return_value={
                "styles": [{"name": "brand", "source": "user"}],
                "templates": [],
                "stylesDirs": [str(styles_dir)],
            },
        ):
            rows = library.list_styles()
        self.assertEqual(len(rows), 1)
        self.assertIn("COVER", rows[0]["coverHtml"])

    def test_style_with_no_readable_file_still_lists(self) -> None:
        # A style the engine knows about but whose file we cannot read must still
        # appear (without a thumbnail) rather than vanishing from the library.
        with mock.patch.object(
            engine,
            "load_lists",
            return_value={
                "styles": [{"name": "ghost", "source": "builtin"}],
                "templates": [],
                "stylesDirs": [str(self.config_dir / "nowhere")],
            },
        ):
            rows = library.list_styles()
        self.assertEqual(rows[0]["name"], "ghost")
        self.assertEqual(rows[0]["coverHtml"], "")

    def test_style_html_returns_none_when_absent(self) -> None:
        with mock.patch.object(
            engine, "load_lists", return_value={"styles": [], "templates": [], "stylesDirs": []}
        ):
            self.assertIsNone(library.style_html("absent"))

    def test_user_style_shadows_a_builtin_of_the_same_name(self) -> None:
        """First-match ordering is the engine's own shadowing rule — a user style
        replaces a builtin with the same name."""
        user_dir = self.config_dir / "styles"
        builtin_dir = self.config_dir / "builtin-styles"
        user_dir.mkdir(parents=True, exist_ok=True)
        builtin_dir.mkdir(parents=True, exist_ok=True)
        (user_dir / "dup.html").write_text("<html>USER</html>", encoding="utf-8")
        (builtin_dir / "dup.html").write_text("<html>BUILTIN</html>", encoding="utf-8")
        with mock.patch.object(
            engine,
            "load_lists",
            return_value={
                "styles": [],
                "templates": [],
                "stylesDirs": [str(user_dir), str(builtin_dir)],
            },
        ):
            self.assertIn("USER", library.style_html("dup") or "")

    def test_templates_pass_through_engine_metadata(self) -> None:
        with mock.patch.object(
            engine,
            "load_lists",
            return_value={
                "styles": [],
                "templates": [{"name": "corp", "layout_count": 9}, "not-a-dict"],
                "stylesDirs": [],
            },
        ):
            rows = library.list_templates()
        self.assertEqual(rows, [{"name": "corp", "layout_count": 9}])

    def test_is_user_owned(self) -> None:
        self.assertTrue(library.is_user_owned({"source": "user"}))
        self.assertFalse(library.is_user_owned({"source": "builtin"}))
        self.assertFalse(library.is_user_owned({}))

    def test_a_non_dict_style_entry_is_skipped(self) -> None:
        """The list crosses a subprocess boundary, so a malformed entry must be
        dropped rather than crashing the whole listing."""
        with mock.patch.object(
            engine,
            "load_lists",
            return_value={
                "styles": ["not-a-dict", {"name": "real", "source": "user"}],
                "templates": [],
                "stylesDirs": [],
            },
        ):
            rows = library.list_styles()
        self.assertEqual([r["name"] for r in rows], ["real"])

    def test_a_style_with_no_name_lists_without_a_thumbnail(self) -> None:
        """A nameless entry cannot be resolved to a file; it must not be used to
        probe the filesystem."""
        with mock.patch.object(
            engine,
            "load_lists",
            return_value={"styles": [{"source": "user"}], "templates": [], "stylesDirs": ["/x"]},
        ):
            rows = library.list_styles()
        self.assertEqual(rows[0]["coverHtml"], "")

    def test_a_traversing_style_name_never_reads_outside_the_library(self) -> None:
        """The listing resolves each name against the engine's style dirs; a name
        that escapes must yield no thumbnail rather than leaking file contents."""
        outside = self.tmp / "secret.html"
        outside.write_text("<html>SECRET</html>", encoding="utf-8")
        styles_dir = self.config_dir / "styles"
        styles_dir.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(
            engine,
            "load_lists",
            return_value={
                "styles": [{"name": "../secret", "source": "user"}],
                "templates": [],
                "stylesDirs": [str(styles_dir)],
            },
        ):
            rows = library.list_styles()
        self.assertEqual(rows[0]["coverHtml"], "")
        self.assertIsNone(library.style_html("../secret"))


class TestEngineNotReady(_LibraryFixture):
    """Every mutating entry point must answer 503 rather than writing somewhere
    unexpected when the engine has not been provisioned yet."""

    def test_each_mutation_is_503_without_a_user_dir(self) -> None:
        with mock.patch.object(engine, "user_subdir", return_value=None):
            for label, result in (
                ("import_style", library.import_style("a", "<html></html>")),
                ("delete_style", library.delete_style("a")),
                ("rename_style", library.rename_style("a", "b")),
                ("import_template", library.import_template("a", b"PK\x03\x04", "")),
                ("delete_template", library.delete_template("a")),
                ("rename_template", library.rename_template("a", "b")),
            ):
                self.assertEqual(result[0], 503, label)
                self.assertEqual(result[1]["code"], "engine_not_ready", label)

    def test_pin_is_503_when_the_config_dir_is_unknown(self) -> None:
        with mock.patch.object(engine, "user_config_dir", return_value=None):
            status, payload = library.pin_style("brand", True)
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "engine_not_ready")

    def test_an_uncreatable_user_dir_is_503_not_a_crash(self) -> None:
        """A read-only config dir must degrade to "engine not ready" rather than
        raising out of a worker thread."""
        with mock.patch.object(engine, "user_subdir", return_value=self.config_dir / "styles"), (
            mock.patch.object(library.Path, "mkdir", side_effect=OSError("read-only"))
        ):
            status, payload = library.import_style("brand", "<html></html>")
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "engine_not_ready")


class TestLibraryWriteFailures(_LibraryFixture):
    """Filesystem failures must become the documented 500 + ``code`` pair, never
    an exception escaping into the route layer."""

    def test_a_failed_style_write_is_500(self) -> None:
        with mock.patch.object(library, "atomic_write", side_effect=OSError("disk full")):
            status, payload = library.import_style("brand", "<html></html>")
        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], "style_write_failed")

    def test_a_failed_style_delete_is_500(self) -> None:
        library.import_style("gone", "<html></html>")
        with mock.patch.object(library.Path, "unlink", side_effect=OSError("busy")):
            status, payload = library.delete_style("gone")
        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], "style_delete_failed")

    def test_a_failed_style_rename_is_500(self) -> None:
        library.import_style("old", "<html></html>")
        with mock.patch.object(library.Path, "rename", side_effect=OSError("busy")):
            status, payload = library.rename_style("old", "new")
        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], "style_rename_failed")

    def test_a_failed_template_write_is_500(self) -> None:
        with mock.patch.object(library.Path, "write_bytes", side_effect=OSError("disk full")):
            status, payload = library.import_template("deck", b"PK\x03\x04", "")
        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], "template_write_failed")

    def test_a_failed_template_delete_is_500(self) -> None:
        with mock.patch.object(engine, "analyze_template", return_value={}):
            library.import_template("deck", b"PK\x03\x04", "")
        with mock.patch.object(library.Path, "unlink", side_effect=OSError("busy")):
            status, payload = library.delete_template("deck")
        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], "template_delete_failed")

    def test_a_failed_template_rename_is_500(self) -> None:
        with mock.patch.object(engine, "analyze_template", return_value={}):
            library.import_template("old", b"PK\x03\x04", "")
        with mock.patch.object(library.Path, "rename", side_effect=OSError("busy")):
            status, payload = library.rename_template("old", "new")
        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], "template_rename_failed")

    def test_a_failed_pin_write_is_500(self) -> None:
        with mock.patch.object(library, "atomic_write", side_effect=OSError("disk full")):
            status, payload = library.pin_style("brand", True)
        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], "pin_write_failed")


class TestNameValidationIsShared(_LibraryFixture):
    """Every verb resolves through ``paths.resolve_library_file``, so the name
    grammar cannot drift between read, create, rename and delete."""

    _BAD = ("../evil", "a/b", "", ".hidden", "a\\b", "..", "with space")

    def test_delete_refuses_every_bad_name(self) -> None:
        for name in self._BAD:
            status, payload = library.delete_style(name)
            self.assertEqual(status, 400, name)
            self.assertEqual(payload["code"], "invalid_style_name", name)

    def test_template_delete_refuses_every_bad_name(self) -> None:
        for name in self._BAD:
            status, payload = library.delete_template(name)
            self.assertEqual(status, 400, name)
            self.assertEqual(payload["code"], "invalid_template_name", name)

    def test_template_rename_refuses_a_bad_source_or_target(self) -> None:
        with mock.patch.object(engine, "analyze_template", return_value={}):
            library.import_template("ok", b"PK\x03\x04", "")
        for name, new_name in (("ok", "../escape"), ("../escape", "ok")):
            status, _ = library.rename_template(name, new_name)
            self.assertEqual(status, 400, f"{name}->{new_name}")
        self.assertTrue((self.config_dir / "templates" / "ok.pptx").is_file())

    def test_template_delete_missing_is_404(self) -> None:
        status, payload = library.delete_template("never")
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "template_not_found")

    def test_template_rename_missing_source_is_404(self) -> None:
        status, payload = library.rename_template("absent", "target")
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "template_not_found")


class TestStateBookkeepingEdgeCases(_LibraryFixture):
    """A rename/delete must leave ``state.json`` consistent, and must tolerate
    the engine having written a shape we did not expect."""

    def test_a_wrong_typed_pin_list_is_not_written_back(self) -> None:
        self._write_state({"pinned_styles": "brand"})
        library.import_style("brand", "<html></html>")
        status, _ = library.delete_style("brand")
        self.assertEqual(status, 200)
        # Left untouched rather than coerced: the engine owns this file.
        self.assertEqual(self._state()["pinned_styles"], "brand")

    def test_pin_replaces_a_wrong_typed_pin_list(self) -> None:
        """`pin_style` DOES rewrite the key, because the user just asked for a
        pin — the result must be a real list, not a mangled string."""
        self._write_state({"pinned_styles": "not-a-list"})
        status, payload = library.pin_style("brand", True)
        self.assertEqual(status, 200)
        self.assertEqual(payload["pinnedStyles"], ["brand"])

    def test_pin_normalizes_non_string_entries(self) -> None:
        """``state.json`` is the ENGINE's file, so it can hold junk. Pins are
        compared to style names by equality, and the value is serialized to the
        UI — a bare int would never match and would round-trip as a non-name."""
        self._write_state({"pinned_styles": [7]})
        status, payload = library.pin_style("brand", True)
        self.assertEqual(status, 200)
        self.assertEqual(payload["pinnedStyles"], ["7", "brand"])

    def test_pin_preserves_other_state_keys(self) -> None:
        """The engine owns ``state.json``; pinning must not drop its template
        metadata."""
        self._write_state({"template_metadata": {"corp": {"name": "corp"}}})
        library.pin_style("brand", True)
        self.assertEqual(list(self._state()["template_metadata"]), ["corp"])

    def test_unpinning_a_style_that_was_never_pinned_is_a_no_op(self) -> None:
        status, payload = library.pin_style("brand", False)
        self.assertEqual(status, 200)
        self.assertEqual(payload["pinnedStyles"], [])

    def test_a_wrong_typed_metadata_map_is_left_alone_on_delete(self) -> None:
        """``state.json`` is the ENGINE's file. A LIST that happens to contain the
        template's name still satisfies ``name in metadata``, so only the explicit
        dict check stops a ``del`` on a list (a TypeError out of a worker thread)."""
        with mock.patch.object(engine, "analyze_template", return_value={}):
            library.import_template("deck", b"PK\x03\x04", "")
        self._write_state({"template_metadata": ["deck", "other"]})
        status, _ = library.delete_template("deck")
        self.assertEqual(status, 200)
        self.assertEqual(self._state()["template_metadata"], ["deck", "other"])

    def test_a_wrong_typed_metadata_map_is_left_alone_on_rename(self) -> None:
        with mock.patch.object(engine, "analyze_template", return_value={}):
            library.import_template("old", b"PK\x03\x04", "")
        self._write_state({"template_metadata": ["old"]})
        status, _ = library.rename_template("old", "new")
        self.assertEqual(status, 200)
        self.assertEqual(self._state()["template_metadata"], ["old"])

    def test_rename_leaves_an_unrelated_pin_untouched(self) -> None:
        library.import_style("old", "<html></html>")
        self._write_state({"pinned_styles": ["someone-else"]})
        library.rename_style("old", "new")
        self.assertEqual(self._state()["pinned_styles"], ["someone-else"])

    def test_a_corrupt_state_file_does_not_block_a_delete(self) -> None:
        library.import_style("gone", "<html></html>")
        (self.config_dir / "state.json").write_text("{not json", encoding="utf-8")
        status, _ = library.delete_style("gone")
        self.assertEqual(status, 200)
        self.assertFalse((self.config_dir / "styles" / "gone.html").exists())


if __name__ == "__main__":
    unittest.main()
