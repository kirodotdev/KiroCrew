"""Artifact kind inference, and blank documents whose kind settles on first save.

``_infer_kind`` resolves in one documented order, ``create()`` infers the kind
when the caller omits it, ``detect_editor_kind`` sniffs content, a document
created blank settles its kind on its first save, and ``settle_blank`` keeps,
saves or deletes a just-created blank in one step. The shared ``store`` fixture
lives in ``artifacts_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from artifacts_helpers import (  # noqa: F401 -- the fixture is requested by name
    store_fixture,
)

from kiro_crew.artifacts import (
    ArtifactComment,
    ArtifactNotFoundError,
    ArtifactStore,
    _infer_kind,
    detect_editor_kind,
)

# ── Kind inference (CR-1) ─────────────────────────────────────────────────────


class TestInferKind:
    """Resolution order of the standalone ``_infer_kind`` helper."""

    def test_explicit_wins(self) -> None:
        # A non-empty explicit kind is returned untouched, even when the
        # content / source_path would infer something else.
        assert _infer_kind("# heading", "doc.html", explicit="json") == "json"
        assert _infer_kind("<div/>", "", explicit="markdown") == "markdown"

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("plan.md", "markdown"),
            ("plan.markdown", "markdown"),
            ("page.html", "html"),
            ("page.htm", "html"),
            ("icon.svg", "svg"),
            ("data.json", "json"),
            ("notes.txt", "text"),
            ("Makefile", "text"),  # no extension → text
            ("archive.tar.gz", "text"),  # unknown extension → text
            ("UPPER.MD", "markdown"),  # case-insensitive
        ],
    )
    def test_extension_matrix(self, path: str, expected: str) -> None:
        # The source_path extension drives the kind regardless of the body.
        assert _infer_kind("any body at all", source_path=path) == expected

    @pytest.mark.parametrize(
        "content,expected",
        [
            ("<div>x</div>", "widget"),
            ("<table><tr></tr></table>", "widget"),
            ("<span>hi</span>", "widget"),
            ("<style>.a{}</style>", "widget"),
            ("<mcwidget>body</mcwidget>", "widget"),
            ("<!DOCTYPE html><html></html>", "widget"),
            ("# Plan\n\nbody", "markdown"),
            ("###### deep heading", "markdown"),
            ("just plain prose with no tags", "markdown"),
            ("  \n# leading whitespace then heading", "markdown"),
            ("<p>hello</p>", "widget"),  # a tag, but not a known marker → fallback
            ("a < b but no real tag", "widget"),  # stray '<' → fallback widget
            ("", "widget"),  # empty → legacy default
            ("   \n  ", "widget"),  # whitespace-only → legacy default
        ],
    )
    def test_content_sniff_matrix(self, content: str, expected: str) -> None:
        assert _infer_kind(content, source_path="") == expected

    def test_extension_beats_content_sniff(self) -> None:
        # A .md file whose body contains HTML is still markdown (extension wins).
        assert _infer_kind("<div>x</div>", source_path="notes.md") == "markdown"


class TestCreateKindInference:
    """``create()`` infers the kind when the caller omits it."""

    def test_create_infers_markdown_from_heading(self, store: ArtifactStore) -> None:
        art = store.create(name="Plan", content="# Title\n\nbody")
        assert art.kind == "markdown"

    def test_create_infers_widget_from_html(self, store: ArtifactStore) -> None:
        art = store.create(name="Dash", content="<div>x</div><table></table>")
        assert art.kind == "widget"

    def test_create_infers_from_source_path(self, store: ArtifactStore, tmp_path: Path) -> None:
        src = tmp_path / "plan.md"
        src.write_text("# live", encoding="utf-8")
        art = store.create(name="P", content="# live", source_path=str(src))
        assert art.kind == "markdown"

    def test_explicit_kind_overrides_inference(self, store: ArtifactStore) -> None:
        # Plain markdown content but the caller pins widget → widget wins.
        art = store.create(name="P", content="# Title", kind="widget")
        assert art.kind == "widget"

    def test_create_persists_inferred_kind(self, store: ArtifactStore) -> None:
        store.create(name="P", content="plain prose, no tags here", slug="p")
        assert store.get("p").kind == "markdown"


class TestDetectEditorKind:
    """:func:`detect_editor_kind` — the content sniff behind blank documents."""

    @pytest.mark.parametrize(
        "content,expected",
        [
            ('{"a": 1}', "json"),
            ('   {"a": 1}   ', "json"),
            ("[1, 2, 3]", "json"),
            ("[]", "json"),
            ("{}", "json"),
            ('<svg viewBox="0 0 1 1"></svg>', "svg"),
            ("<SVG></SVG>", "svg"),
            ('<?xml version="1.0"?>\n<svg></svg>', "svg"),
            ("<!DOCTYPE svg>\n<svg></svg>", "svg"),
        ],
    )
    def test_detects_structured_kinds(self, content: str, expected: str) -> None:
        assert detect_editor_kind(content) == expected

    @pytest.mark.parametrize(
        "content",
        [
            "",
            "   \n  ",
            "# A markdown heading",
            "just some prose",
            '{"a": 1',  # mid-edit syntax error
            "42",  # bare JSON scalar — valid JSON, but almost certainly prose
            "true",
            '"a quoted string"',
            "<div>html, not svg</div>",
            "text before <svg></svg>",  # anchored: not an SVG document
            "<html><body></body></html>",
        ],
    )
    def test_returns_none_for_unrecognized(self, content: str) -> None:
        """Never guesses. ``None`` is what keeps a re-typed kind from flapping."""
        assert detect_editor_kind(content) is None

    def test_never_returns_a_non_editable_kind(self) -> None:
        """``html`` / ``widget`` would strand the editor, so they're excluded."""
        for content in ("<div>x</div>", "<mcwidget>x</mcwidget>", "<!doctype html><html>"):
            assert detect_editor_kind(content) not in ("html", "widget")


class TestBlankDocumentKind:
    """Creating a document blank, and letting its kind settle on first save."""

    def test_blank_create_defaults_to_editable_markdown(self, store: ArtifactStore) -> None:
        art = store.create(name="Untitled", content="")
        assert art.kind == "markdown"
        assert art.kind_auto is True

    def test_whitespace_only_create_counts_as_blank(self, store: ArtifactStore) -> None:
        art = store.create(name="Untitled", content="   \n ")
        assert (art.kind, art.kind_auto) == ("markdown", True)

    def test_content_bearing_create_pins_its_kind(self, store: ArtifactStore) -> None:
        art = store.create(name="Doc", content="# Real content")
        assert (art.kind, art.kind_auto) == ("markdown", False)

    def test_explicit_kind_pins_even_when_blank(self, store: ArtifactStore) -> None:
        art = store.create(name="Doc", content="", kind="json")
        assert (art.kind, art.kind_auto) == ("json", False)

    def test_file_backed_blank_is_not_auto(self, store: ArtifactStore, tmp_path: Path) -> None:
        """An extension is a real signal, so a blank file keeps its mapped kind."""
        src = tmp_path / "empty.json"
        src.write_text("", encoding="utf-8")
        art = store.create(name="Empty", content="", source_path=str(src))
        assert (art.kind, art.kind_auto) == ("json", False)

    def test_kind_auto_survives_a_reload(self, store: ArtifactStore) -> None:
        store.create(name="Untitled", content="", slug="u")
        assert store.get("u").kind_auto is True

    def test_legacy_meta_without_the_field_is_never_auto(self, store: ArtifactStore) -> None:
        store.create(name="Legacy", content="# hi", slug="legacy")
        meta_path = store.root / "legacy" / "meta.json"
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        del raw["kind_auto"]
        meta_path.write_text(json.dumps(raw), encoding="utf-8")
        assert store.get("legacy").kind_auto is False

    def test_first_json_save_retypes_the_document(self, store: ArtifactStore) -> None:
        store.create(name="Untitled", content="", slug="u")
        assert store.update("u", content='{"hello": "world"}').kind == "json"
        assert store.get("u").kind == "json"

    def test_first_svg_save_retypes_the_document(self, store: ArtifactStore) -> None:
        store.create(name="Untitled", content="", slug="u")
        assert store.update("u", content="<svg></svg>").kind == "svg"

    def test_prose_save_stays_markdown(self, store: ArtifactStore) -> None:
        store.create(name="Untitled", content="", slug="u")
        assert store.update("u", content="# My notes\n\nsome prose").kind == "markdown"

    def test_mid_edit_syntax_error_does_not_flap_the_kind(self, store: ArtifactStore) -> None:
        """Once JSON, a transient parse failure must not drop back to markdown."""
        store.create(name="Untitled", content="", slug="u")
        store.update("u", content='{"a": 1}')
        assert store.update("u", content='{"a": ').kind == "json"
        assert store.update("u", content='{"a": 2}').kind == "json"

    def test_retyping_stays_available_between_structured_kinds(self, store: ArtifactStore) -> None:
        store.create(name="Untitled", content="", slug="u")
        store.update("u", content='{"a": 1}')
        assert store.update("u", content="<svg></svg>").kind == "svg"

    def test_a_pinned_artifact_is_never_retyped(self, store: ArtifactStore) -> None:
        store.create(name="Doc", content="# Real content", slug="d")
        assert store.update("d", content='{"a": 1}').kind == "markdown"

    def test_an_explicit_kind_on_update_pins_the_artifact(self, store: ArtifactStore) -> None:
        store.create(name="Untitled", content="", slug="u")
        pinned = store.update("u", content="# prose", kind="text")
        assert (pinned.kind, pinned.kind_auto) == ("text", False)
        # Once pinned, JSON content does not re-type it.
        assert store.update("u", content='{"a": 1}').kind == "text"

    def test_snapshot_records_the_detected_kind_for_the_version(self, store: ArtifactStore) -> None:
        store.create(name="Untitled", content="", slug="u")
        art = store.update("u", content='{"a": 1}', snapshot=True)
        assert art.version_kinds[str(art.version)] == "json"
        # v1 was blank markdown and must still read back that way.
        assert store.get("u", version=1).kind == "markdown"


class TestSettleBlank:
    """:meth:`ArtifactStore.settle_blank` -- resolving a just-created blank.

    This is where the keep / save / delete decision lives, and it lives here
    rather than in the browser for one reason: it has to be atomic. Deciding in
    the client means reading the artifact and then acting on it, and a save
    landing in that window -- from a popout window on the same document, or from
    an agent -- gets overwritten or deleted. Re-reading cannot close that window
    because the window is between the read and the write.

    Every case below therefore asserts the SAME safety property from a different
    direction: unless the document is pristine on every axis the record can
    report, it is left exactly as it is.
    """

    UNTITLED = "Untitled"

    def _blank(self, store: ArtifactStore) -> None:
        store.create(name=self.UNTITLED, content="", slug="u")

    def test_deletes_an_abandoned_blank(self, store: ArtifactStore) -> None:
        self._blank(store)
        assert store.settle_blank("u", untitled_name=self.UNTITLED) == "deleted"
        with pytest.raises(ArtifactNotFoundError):
            store.get("u")

    def test_saves_an_unsaved_draft_instead_of_deleting(self, store: ArtifactStore) -> None:
        self._blank(store)
        assert store.settle_blank("u", untitled_name=self.UNTITLED, draft="# notes") == "saved"
        assert store.get("u").content == "# notes"

    def test_a_whitespace_draft_is_not_a_draft(self, store: ArtifactStore) -> None:
        self._blank(store)
        assert store.settle_blank("u", untitled_name=self.UNTITLED, draft="  \n ") == "deleted"

    def test_saving_a_draft_does_not_bump_the_version(self, store: ArtifactStore) -> None:
        """The draft is the document's first content, not an edit to it."""
        self._blank(store)
        store.settle_blank("u", untitled_name=self.UNTITLED, draft="# notes")
        assert store.get("u").version == 1

    @pytest.mark.parametrize(
        "draft,expected",
        [
            pytest.param('{"a": 1}', "json", id="json"),
            pytest.param("<svg></svg>", "svg", id="svg"),
            pytest.param("# notes", "markdown", id="markdown"),
        ],
    )
    def test_a_settled_draft_gets_kind_detection(
        self, store: ArtifactStore, draft: str, expected: str
    ) -> None:
        """A settled draft is the document's first content, so it earns the same
        detection an ordinary save would. Without this, typing JSON into a blank
        and navigating away stored it as markdown and rendered it wrongly."""
        self._blank(store)
        assert store.settle_blank("u", untitled_name=self.UNTITLED, draft=draft) == "saved"
        assert store.get("u").kind == expected

    def test_a_settled_draft_respects_an_explicitly_chosen_kind(self, store: ArtifactStore) -> None:
        """A kind the user picked must survive; only auto-assigned kinds re-detect."""
        self._blank(store)
        store.update("u", kind="text")
        assert store.settle_blank("u", untitled_name=self.UNTITLED, draft='{"a": 1}') == "saved"
        assert store.get("u").kind == "text"

    def test_a_concurrent_save_is_never_overwritten(self, store: ArtifactStore) -> None:
        """The headline race: content arrived while the page still held a draft."""
        self._blank(store)
        store.update("u", content="# saved in the popout")
        assert store.settle_blank("u", untitled_name=self.UNTITLED, draft="# stale") == "kept"
        assert store.get("u").content == "# saved in the popout"

    @pytest.mark.parametrize(
        "invest",
        [
            pytest.param(lambda s: s.update("u", content="# written"), id="content"),
            pytest.param(lambda s: s.update("u", name="Release plan"), id="renamed"),
            pytest.param(lambda s: s.update("u", description="scratch pad"), id="described"),
            pytest.param(lambda s: s.update("u", tags=["ops"]), id="tagged"),
            pytest.param(lambda s: s.set_folder("u", "abc123"), id="filed"),
            pytest.param(lambda s: s.set_pinned("u", True), id="starred"),
            pytest.param(
                lambda s: s.add_comment("u", ArtifactComment(id="c1", body="hm")),
                id="commented",
            ),
        ],
    )
    def test_keeps_a_document_with_any_investment(self, store: ArtifactStore, invest) -> None:
        self._blank(store)
        invest(store)
        assert store.settle_blank("u", untitled_name=self.UNTITLED) == "kept"
        assert store.get("u") is not None

    @pytest.mark.parametrize(
        "invest,check",
        [
            pytest.param(
                lambda s: s.update("u", name="Release plan"),
                lambda a: a.name == "Release plan",
                id="renamed",
            ),
            pytest.param(
                lambda s: s.update("u", tags=["ops"]), lambda a: a.tags == ["ops"], id="tagged"
            ),
            pytest.param(
                lambda s: s.update("u", kind="text"), lambda a: a.kind == "text", id="typed"
            ),
            pytest.param(lambda s: s.set_pinned("u", True), lambda a: a.pinned, id="starred"),
        ],
    )
    def test_investment_does_not_cost_the_user_their_draft(
        self, store: ArtifactStore, invest, check
    ) -> None:
        """The two questions are independent. Naming or tagging a document says
        nothing about whether the editor is holding text that was never saved --
        and refusing to write it because the name changed loses their typing.
        Metadata survives alongside it."""
        self._blank(store)
        invest(store)
        assert (
            store.settle_blank("u", untitled_name=self.UNTITLED, draft="# first paragraph")
            == "saved"
        )
        art = store.get("u")
        assert art.content == "# first paragraph"
        assert check(art)

    def test_a_draft_is_rescued_even_when_deletion_is_forbidden(self, store: ArtifactStore) -> None:
        """``allow_delete=False`` means "I have writes you may not have applied".
        It cannot make deletion safe; it has no bearing on saving the buffer."""
        self._blank(store)
        assert (
            store.settle_blank(
                "u", untitled_name=self.UNTITLED, draft="# typed", allow_delete=False
            )
            == "saved"
        )
        assert store.get("u").content == "# typed"

    def test_deletion_is_refused_when_the_caller_forbids_it(self, store: ArtifactStore) -> None:
        self._blank(store)
        assert store.settle_blank("u", untitled_name=self.UNTITLED, allow_delete=False) == "kept"
        assert store.get("u") is not None

    def test_keeps_a_document_with_history_even_if_emptied_again(
        self, store: ArtifactStore
    ) -> None:
        """A snapshot is history worth keeping. Content alone would miss this: the
        live body can be emptied again after a snapshot was taken."""
        self._blank(store)
        store.update("u", content="# v2", snapshot=True)
        store.update("u", content="")
        assert store.settle_blank("u", untitled_name=self.UNTITLED) == "kept"

    def test_a_comment_is_seen_even_though_it_touches_no_field(self, store: ArtifactStore) -> None:
        """Comments live in a sidecar that add_comment writes WITHOUT touching
        meta.json, so no field on the record reveals them."""
        self._blank(store)
        store.add_comment("u", ArtifactComment(id="c1", body="is this right?"))
        assert store.settle_blank("u", untitled_name=self.UNTITLED) == "kept"
        assert len(store.list_comments("u")) == 1

    def test_a_differently_named_document_is_not_an_untitled_one(
        self, store: ArtifactStore
    ) -> None:
        """The placeholder is supplied by the caller (it is localised), so a
        mismatch must mean 'named' rather than 'unrecognised, delete it'."""
        store.create(name="Sans titre", content="", slug="u")
        assert store.settle_blank("u", untitled_name=self.UNTITLED) == "kept"

    def test_missing_artifact_raises_rather_than_reporting_success(
        self, store: ArtifactStore
    ) -> None:
        with pytest.raises(ArtifactNotFoundError):
            store.settle_blank("nope", untitled_name=self.UNTITLED)
