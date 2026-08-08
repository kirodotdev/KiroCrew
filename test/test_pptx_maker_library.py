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
import os
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


class TestStyleReadsRejectHardlinks(_LibraryFixture):
    """A HARDLINK is what path-based containment cannot see.

    ``resolve_library_file`` resolves and re-checks containment, which stops a
    symlink — but a hardlink has no target to resolve: ``is_symlink()`` is False and
    ``resolve()`` returns the path itself, so every path-based check passes. The
    styles dir is agent-writable, so the agent can create one and point it at a
    credential file. Both read paths therefore go through
    ``safe_read_file_bytes_nolink``, which rejects ``st_nlink > 1`` on the OPENED
    descriptor.
    """

    def setUp(self) -> None:
        super().setUp()
        self.styles = self.config_dir / "styles"
        self.styles.mkdir(parents=True, exist_ok=True)
        secret = self.tmp / "outside_secret"
        secret.write_text("Host prod\n  User SECRETUSER\n", encoding="utf-8")
        os.link(secret, self.styles / "pwned.html")
        (self.styles / "ok.html").write_text(
            '<html><body><div class="slide">legit</div></body></html>', encoding="utf-8"
        )
        self._lists = mock.patch.object(
            engine,
            "load_lists",
            return_value={
                "stylesDirs": [str(self.styles)],
                "styles": [{"name": "pwned"}, {"name": "ok"}],
            },
        )
        self._lists.start()

    def tearDown(self) -> None:
        self._lists.stop()
        super().tearDown()

    @unittest.skipUnless(hasattr(os, "link"), "needs hardlinks")
    def test_style_html_refuses_a_hardlinked_file(self) -> None:
        self.assertIsNone(library.style_html("pwned"))

    @unittest.skipUnless(hasattr(os, "link"), "needs hardlinks")
    def test_list_styles_serves_no_cover_for_a_hardlinked_file(self) -> None:
        covers = {row["name"]: row["coverHtml"] for row in library.list_styles()}
        self.assertEqual(covers["pwned"], "")
        self.assertNotIn("SECRETUSER", json.dumps(covers))

    def test_an_ordinary_style_is_still_readable(self) -> None:
        """The guard must not break the normal path — a refusal-only pair of tests
        would pass just as well against a reader that refused everything."""
        self.assertIn("legit", library.style_html("ok") or "")
        covers = {row["name"]: row["coverHtml"] for row in library.list_styles()}
        self.assertIn("legit", covers["ok"])


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

    def test_concurrent_renames_onto_one_name_cannot_overwrite(self) -> None:
        """Two entries renamed onto the same unused name — only one may win.

        `Path.rename` REPLACES an existing target on POSIX, so `exists()` then `rename`
        was check-then-act: both callers passed the probe against a name that did not
        exist yet, and the second silently destroyed the first's file while both
        answered 200. `os.link` refuses an existing target atomically, so the filesystem
        arbitrates.
        """
        import threading

        library.import_style("a", "<html>A</html>")
        library.import_style("b", "<html>B</html>")
        results: list[tuple[int, dict]] = []
        lock = threading.Lock()

        def worker(src: str) -> None:
            out = library.rename_style(src, "merged")
            with lock:
                results.append(out)

        threads = [threading.Thread(target=worker, args=(s,)) for s in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(
            sorted(status for status, _ in results),
            [200, 409],
            "both renames succeeded, so one silently overwrote the other",
        )
        # The winner's content is intact, and the loser's file still exists under its
        # own name — a refused rename must not consume the source.
        merged = (self.config_dir / "styles" / "merged.html").read_text(encoding="utf-8")
        self.assertIn(merged, ("<html>A</html>", "<html>B</html>"))
        survivors = [
            n for n in ("a", "b") if (self.config_dir / "styles" / f"{n}.html").is_file()
        ]
        self.assertEqual(len(survivors), 1, "the refused rename lost its source file")

    def test_rename_rejects_a_bad_target_name(self) -> None:
        library.import_style("ok", "<html></html>")
        status, _ = library.rename_style("ok", "../escape")
        self.assertEqual(status, 400)
        self.assertTrue((self.config_dir / "styles" / "ok.html").is_file())

    def test_rename_missing_source_is_404(self) -> None:
        status, _ = library.rename_style("absent", "target")
        self.assertEqual(status, 404)

    def test_a_failed_state_write_undoes_the_rename(self) -> None:
        """The rename has already committed when state is written.

        So a failing write left the file under its NEW name while `state.json`
        still pinned the OLD one — a pin pointing at a name that no longer exists,
        with the request reporting 500 as though nothing had happened. The undo is
        what keeps the two consistent and makes a retry possible.
        """
        library.import_style("old", "<html>content</html>")
        self._write_state({"pinned_styles": ["old"]})
        with mock.patch.object(library, "_save_state", side_effect=OSError("disk full")):
            status, payload = library.rename_style("old", "new")
        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], "style_rename_failed")
        # The file is back under its original name, and the pin still resolves.
        self.assertTrue((self.config_dir / "styles" / "old.html").is_file())
        self.assertFalse((self.config_dir / "styles" / "new.html").exists())
        self.assertEqual(self._state()["pinned_styles"], ["old"])


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

    def test_concurrent_pins_do_not_lose_each_other(self) -> None:
        """Two tabs pinning different styles must both survive.

        Every verb here loads `state.json`, changes one key and writes it back, and the
        routes run them on the subprocess executor — so two requests genuinely execute
        at once. Without a lock both workers read the same `pinned_styles`, each
        appended its own name, and the later write discarded the earlier pin while BOTH
        responses reported success. `atomic_write` never helped: the write was atomic,
        the read-modify-write around it was not.

        A plain sleep in `_load_state` makes it deterministic without a barrier: a
        barrier cannot be used here precisely BECAUSE the fix works — the lock
        serializes the two critical sections, so the second thread never reaches a
        rendezvous the first is waiting at, and both time out. The delay instead widens
        the window an unlocked implementation would interleave in, while a locked one
        simply queues.
        """
        import threading
        import time

        real_load = library._load_state

        def slow_load():
            result = real_load()
            time.sleep(0.05)
            return result

        names = ["brand", "mono"]
        errors: list[BaseException] = []

        def worker(name: str) -> None:
            try:
                library.pin_style(name, True)
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        with mock.patch.object(library, "_load_state", slow_load):
            threads = [threading.Thread(target=worker, args=(n,)) for n in names]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

        assert not errors, errors
        # BOTH pins survive. Unlocked, the later write discarded the earlier one and
        # this held a single name while both requests had reported success.
        self.assertEqual(sorted(self._state().get("pinned_styles", [])), sorted(names))

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

    def test_the_rename_move_runs_under_the_state_lock(self) -> None:
        """The file MOVE and the state update must be one critical section.

        Split, a concurrent delete of the new name could interleave between the link
        and the state write, leaving `state.json` naming a file that no longer exists
        while both verbs answered 200. The move is what makes the state stale, so the
        lock has to span it — `delete_style`/`delete_template` already do.

        Observed structurally, from inside the filesystem call, rather than by racing
        threads: a timing test for an interleaving is inherently flaky, and what needs
        pinning is that the two steps cannot be separated.
        """
        library.import_template("deck", self._PPTX, "corporate")
        held: list[bool] = []
        real_link = library.os.link

        def observe(src, dst):  # noqa: ANN001 - mirrors os.link
            held.append(library.state_transaction().locked())
            return real_link(src, dst)

        with mock.patch.object(library.os, "link", side_effect=observe):
            status, _ = library.rename_template("deck", "deck2")

        assert status == 200
        assert held == [True]
        assert not library.state_transaction().locked()

    def test_the_analysis_runs_under_the_state_lock(self) -> None:
        """`analyze_template` is a READ-MODIFY-WRITE of `state.json`, not a pure read.

        Its engine snippet does `get_state()` -> mutate `template_metadata` ->
        `update_state(...)`, so two concurrent imports each read the map before
        either wrote it and the second write dropped the first template's metadata —
        while both answered 200. That is the same lost-update shape the `O_EXCL` name
        claim fixes for the FILE, one level up at the shared metadata document.

        Asserted structurally, by observing the lock's state from inside the analysis,
        rather than by racing two threads: a timing test for a lost update is
        inherently flaky, and what actually needs pinning is that the read and the
        write cannot be interleaved — which is precisely "the lock is held here".
        """
        held: list[bool] = []

        def observe(path, description):  # noqa: ANN001 - mock signature mirrors engine
            held.append(library.state_transaction().locked())
            return {"name": "deck", "layout_count": 3}

        with mock.patch.object(engine, "analyze_template", side_effect=observe):
            status, _ = library.import_template("deck", self._PPTX, "corporate")

        self.assertEqual(status, 200)
        self.assertEqual(held, [True])
        # And it is RELEASED afterwards, or the next import would deadlock.
        self.assertFalse(library.state_transaction().locked())

    def test_a_failed_write_leaves_no_partial_file_to_block_the_retry(self) -> None:
        """A direct `write_bytes` left a PARTIAL file at the target on failure.

        A `.pptx` is megabytes, so a mid-write failure (disk full, upload cut
        short) is a real outcome — and the partial file then trips the
        `target.exists()` check, so every retry answers `template_exists` and the
        user is stuck with a corrupt template they cannot replace. Writing to a
        same-directory temp and `os.replace`-ing means the target only ever appears
        complete.
        """
        with mock.patch.object(
            library.os, "replace", side_effect=OSError("disk full")
        ):
            status, payload = library.import_template("deck", self._PPTX, "")
        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], "template_write_failed")
        # No target, and no leftover temp to accumulate.
        templates = self.config_dir / "templates"
        self.assertFalse((templates / "deck.pptx").exists())
        self.assertEqual(list(templates.glob("*.part")), [])
        self.assertEqual(list(templates.glob(".*.part")), [])
        # And the retry now succeeds rather than reporting `template_exists`.
        status, _ = library.import_template("deck", self._PPTX, "")
        self.assertEqual(status, 200)

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
        # `os.fdopen`, not `atomic_write`: the import now CLAIMS the name with an
        # `O_EXCL` create and writes through that descriptor, so the fd write is the
        # step that can fail once the name is taken (see `import_style`).
        with mock.patch.object(library.os, "fdopen", side_effect=OSError("disk full")):
            status, payload = library.import_style("brand", "<html></html>")
        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], "style_write_failed")

    def test_a_failed_style_write_leaves_no_placeholder(self) -> None:
        """The exclusive create claims the name BEFORE the bytes land, so a failed
        write must remove it — otherwise every retry answers `style_exists` against a
        file holding nothing, and the user can never import that name again.

        Parametrized over the two DISTINCT failure points, because they need different
        cleanup and only one of them was handled at first: `os.fdopen` takes ownership
        of the descriptor only once it SUCCEEDS, so a failure there leaves the raw fd
        open — and on Windows an open handle makes the unlink fail with a sharing
        violation, leaving exactly the stuck placeholder this test forbids. That was a
        Windows-only break in this very cleanup path, invisible on a green macOS run.
        """
        real_fdopen = library.os.fdopen

        class _FailingHandle:
            """A handle that opens, then fails on write — and still closes its fd.

            `TextIOWrapper` is an immutable type, so its `write` cannot be patched;
            wrapping the real handle is the honest way to reach the write-failure
            branch. Closing matters: the branch relies on `with` having released the
            descriptor before it unlinks, which is what makes the unlink work on
            Windows.
            """

            def __init__(self, inner):  # noqa: ANN001
                self._inner = inner

            def write(self, _data):  # noqa: ANN001
                raise OSError("disk full")

            def __enter__(self):
                return self

            def __exit__(self, *exc):  # noqa: ANN002
                self._inner.close()
                return False

        for label, target_mock in (
            ("fdopen", mock.patch.object(library.os, "fdopen", side_effect=OSError("disk full"))),
            (
                "write",
                mock.patch.object(
                    library.os,
                    "fdopen",
                    side_effect=lambda *a, **kw: _FailingHandle(real_fdopen(*a, **kw)),
                ),
            ),
        ):
            with self.subTest(failure=label):
                path = self.config_dir / "styles" / "brand.html"
                path.unlink(missing_ok=True)
                with target_mock:
                    library.import_style("brand", "<html></html>")
                self.assertFalse(path.exists(), f"{label} failure left a placeholder")
                # And the name is genuinely reusable.
                status, _ = library.import_style("brand", "<html></html>")
                self.assertEqual(status, 200)

    def test_a_failed_template_write_leaves_no_placeholder(self) -> None:
        """Same for the binary sibling, which claims the name with a zero-byte file.

        `atomic_write` cleans up its own temp file; the zero-byte placeholder at
        the TARGET is this module's to remove, which is what this pins.
        """
        with mock.patch.object(library, "atomic_write", side_effect=OSError("disk full")):
            status, _ = library.import_template("deck", b"PK\x03\x04data")
        self.assertEqual(status, 500)
        self.assertFalse((self.config_dir / "templates" / "deck.pptx").exists())

    def test_a_duplicate_import_is_409_not_an_overwrite(self) -> None:
        """Sequential duplicate — already caught by the probe, pinned so the `O_EXCL`
        create keeps answering with the documented code rather than an OSError."""
        library.import_style("brand", "<html>first</html>")
        status, payload = library.import_style("brand", "<html>second</html>")
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "style_exists")
        body = (self.config_dir / "styles" / "brand.html").read_text(encoding="utf-8")
        self.assertIn("first", body, "the duplicate import overwrote the original")

    def test_concurrent_imports_of_one_name_cannot_overwrite(self) -> None:
        """The race the exclusive create actually fixes.

        The sequential test above passes against the OLD code too — `exists()` catches a
        duplicate that arrives after the first has landed. The defect was CONCURRENT:
        both callers passed the probe, `atomic_write` REPLACES, and the later write
        silently destroyed the first user's style while both answered 200. Measured
        against the pre-fix code with a delay in `exists()`:

            statuses: [200, 200]      <- no 409 anywhere
            final content: <html>first</html>

        Exactly one 200 and one 409 is the whole assertion — which of the two wins is
        a genuine race and must not be pinned.
        """
        import threading

        results: list[tuple[int, dict]] = []
        lock = threading.Lock()

        def worker(marker: str) -> None:
            out = library.import_style("brand", f"<html>{marker}</html>")
            with lock:
                results.append(out)

        threads = [threading.Thread(target=worker, args=(m,)) for m in ("first", "second")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(
            sorted(status for status, _ in results),
            [200, 409],
            "both imports succeeded, so one silently overwrote the other",
        )
        # And the surviving file is one of the two whole documents, never a mix.
        body = (self.config_dir / "styles" / "brand.html").read_text(encoding="utf-8")
        self.assertIn(body, ("<html>first</html>", "<html>second</html>"))

    def test_a_failed_style_delete_is_500(self) -> None:
        # `os.replace`, not `Path.unlink`: the delete STAGES the file aside before
        # writing state, so that rename is the step that can fail while the entry
        # is still live (see `_delete_with_state`).
        library.import_style("gone", "<html></html>")
        with mock.patch.object(library.os, "replace", side_effect=OSError("busy")):
            status, payload = library.delete_style("gone")
        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], "style_delete_failed")
        # And the style is still there, so a retry is possible.
        self.assertTrue((self.config_dir / "styles" / "gone.html").is_file())

    def test_a_failed_state_write_restores_a_deleted_style(self) -> None:
        """Same hazard as the rename, reached by the other verb.

        The old order unlinked FIRST and wrote state after, so a failing write left
        the file gone while the pin still named it — and the request reported 500, so
        the user believed nothing had happened. Staging the file means the operation
        fails with the library and the state still agreeing.
        """
        library.import_style("doomed", "<html></html>")
        self._write_state({"pinned_styles": ["doomed"]})
        with mock.patch.object(library, "_save_state", side_effect=OSError("disk full")):
            status, payload = library.delete_style("doomed")
        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], "style_delete_failed")
        # The file is back and the pin still resolves to it.
        self.assertTrue((self.config_dir / "styles" / "doomed.html").is_file())
        self.assertEqual(self._state()["pinned_styles"], ["doomed"])
        # No staged leftover.
        self.assertEqual(list((self.config_dir / "styles").glob(".*deleting")), [])

    def test_a_failed_style_rename_is_500(self) -> None:
        library.import_style("old", "<html></html>")
        # `os.link`, not `Path.rename`: the rename now CLAIMS the new name with a link
        # (which refuses an existing target atomically) and then unlinks the old one, so
        # the link is the step that can fail. See `rename_style`.
        with mock.patch.object(library.os, "link", side_effect=OSError("busy")):
            status, payload = library.rename_style("old", "new")
        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], "style_rename_failed")

    def test_a_failed_template_write_is_500(self) -> None:
        # The import delegates the temp-write-and-replace to `atomic_write`, so
        # that is the seam to fail. Patching `Path.write_bytes` here silently
        # stopped injecting anything once the hand-rolled copy was removed, and
        # the assertions below then passed a 200 straight through.
        with mock.patch.object(library, "atomic_write", side_effect=OSError("disk full")):
            status, payload = library.import_template("deck", b"PK\x03\x04", "")
        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], "template_write_failed")

    def test_a_failed_template_delete_is_500(self) -> None:
        with mock.patch.object(engine, "analyze_template", return_value={}):
            library.import_template("deck", b"PK\x03\x04", "")
        with mock.patch.object(library.os, "replace", side_effect=OSError("busy")):
            status, payload = library.delete_template("deck")
        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], "template_delete_failed")
        self.assertTrue((self.config_dir / "templates" / "deck.pptx").is_file())

    def test_a_failed_template_rename_is_500(self) -> None:
        with mock.patch.object(engine, "analyze_template", return_value={}):
            library.import_template("old", b"PK\x03\x04", "")
        # `os.link`, not `Path.rename`: the rename now CLAIMS the new name with a link
        # (which refuses an existing target atomically) and then unlinks the old one, so
        # the link is the step that can fail. See `rename_template`.
        with mock.patch.object(library.os, "link", side_effect=OSError("busy")):
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
