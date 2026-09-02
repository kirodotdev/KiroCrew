"""Tests for projecting editorial blocks.

Three properties carry the weight here, and they are DIFFERENT GRAINS of the
same tolerance. An unknown ``form`` skips the WHOLE block -- the arrangement is
what a form names, and a block whose arrangement this client cannot draw has no
partial rendering that is not a guess. An unknown item ``type`` skips just that
CARD, because the arrangement can still be drawn around it. And artwork that is
not a plain path is DROPPED rather than handed to an ``<img>``.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.apps import official_editorial as oe


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(oe, "_cache_path", lambda: tmp_path / "editorial.json")
    return tmp_path


def _doc(sections: Any, version: int | Any = 1) -> dict[str, Any]:
    return {"schemaVersion": version, "sections": sections}


def _app(**over) -> dict[str, Any]:
    base: dict[str, Any] = {"type": "app", "appRef": "todo-ledger"}
    base.update(over)
    return base


def _coll(**over) -> dict[str, Any]:
    base: dict[str, Any] = {"type": "collection", "title": "Picks", "appRefs": ["a", "b"]}
    base.update(over)
    return base


def _full(item: Any) -> dict[str, Any]:
    return {"form": "full", "items": [item]}


def _row(*items: Any) -> dict[str, Any]:
    return {"form": "row", "items": list(items)}


def _cards(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten blocks to their cards, for tests about ITEM projection only."""
    return [item for block in blocks for item in block["items"]]


class TestBlocks:
    """The outer level: a `form` and the items it arranges."""

    def test_a_full_block_carries_its_form_and_one_item(self):
        got = oe.load_sections(fetcher=lambda: _doc([_full(_app())]))
        assert got == [{"form": "full", "items": [{"type": "app", "appRefs": ["todo-ledger"]}]}]

    def test_a_row_keeps_its_items_in_document_order(self):
        doc = _doc([_row(_coll(title="First"), _coll(title="Second"))])
        got = oe.load_sections(fetcher=lambda: doc)
        assert [i["title"] for i in got[0]["items"]] == ["First", "Second"]

    def test_the_one_plus_two_layout_projects_as_two_blocks(self):
        # The page this shape exists to express: one full-width card, then a row
        # of two. Two blocks, not three cards -- the grouping is the document's,
        # not inferred from array position.
        doc = _doc([_full(_app(appRef="lead")), _row(_coll(title="A"), _coll(title="B"))])
        got = oe.load_sections(fetcher=lambda: doc)
        assert [(b["form"], len(b["items"])) for b in got] == [("full", 1), ("row", 2)]

    @pytest.mark.parametrize("form", ["carousel", "grid", "shelf", "", None, 5])
    def test_an_unknown_form_skips_the_whole_block(self, form):
        # Skipping whole is the point: the arrangement cannot be drawn, so no
        # partial rendering of the block is not a guess. `carousel` is
        # published-schema-legal and lands here DELIBERATELY until a renderer
        # ships -- its presence must not cost neighbouring blocks their render.
        doc = _doc(
            [{"form": form, "items": [_app(), _app(appRef="x")]}, _full(_app(appRef="kept"))]
        )
        got = oe.load_sections(fetcher=lambda: doc)
        assert [i["appRefs"] for i in _cards(got)] == [["kept"]]

    def test_a_full_block_with_two_items_is_dropped(self):
        # `full` means one item across the whole width; two of them can only
        # stack, and stacked full-width blocks are two sections. A document
        # saying otherwise bypassed the publish gate.
        doc = _doc(
            [{"form": "full", "items": [_app(), _app(appRef="x")]}, _full(_app(appRef="kept"))]
        )
        got = oe.load_sections(fetcher=lambda: doc)
        assert [i["appRefs"] for i in _cards(got)] == [["kept"]]

    def test_a_row_that_falls_to_one_item_is_dropped_whole(self):
        # The floor is re-applied AFTER item projection: the second card here
        # dissolves (untitled collection), and a row of one renders as a
        # half-width card against empty space -- an arrangement the curator did
        # not write.
        doc = _doc([_row(_coll(title="kept-title"), _coll(title=None)), _full(_app(appRef="kept"))])
        got = oe.load_sections(fetcher=lambda: doc)
        assert [b["form"] for b in got] == ["full"]

    @pytest.mark.parametrize("items", ["nope", 5, None, {}], ids=["str", "int", "none", "dict"])
    def test_a_non_list_items_field_drops_that_block(self, items):
        doc = _doc([{"form": "full", "items": items}, _full(_app(appRef="kept"))])
        assert len(oe.load_sections(fetcher=lambda: doc)) == 1

    def test_a_non_dict_block_is_skipped(self):
        doc = _doc(["nope", 5, None, _full(_app(appRef="kept"))])
        assert len(oe.load_sections(fetcher=lambda: doc)) == 1

    def test_the_old_flat_shape_yields_the_default(self):
        # A pre-split document: cards at the top level, no `form`. Every entry
        # takes the unknown-form path, so a stale cache degrades to the derived
        # layout instead of half-rendering.
        doc = _doc([_app(), _coll()])
        assert oe.load_sections(fetcher=lambda: doc) == []


class TestAppItems:
    def test_a_single_ref_comes_through_as_a_one_entry_list(self):
        # Both types spell refs as a list so the caller resolves them one way;
        # `type` is what the renderer branches on.
        got = _cards(oe.load_sections(fetcher=lambda: _doc([_full(_app())])))
        assert got == [{"type": "app", "appRefs": ["todo-ledger"]}]

    def test_a_blurb_comes_through(self):
        doc = _doc([_full(_app(blurb="Every page on one screen."))])
        got = _cards(oe.load_sections(fetcher=lambda: doc))
        assert got[0]["blurb"] == "Every page on one screen."

    def test_a_published_title_is_ignored(self):
        """The app's own name is the heading. A curator wanting other words is
        describing a collection, and honouring a title here would give two ways
        to spell one card."""
        doc = _doc([_full(_app(title="Our pick of the week"))])
        assert "title" not in _cards(oe.load_sections(fetcher=lambda: doc))[0]

    @pytest.mark.parametrize(
        "ref", ["", "   ", None, 5, [], {}], ids=["blank", "ws", "none", "int", "list", "dict"]
    )
    def test_an_unusable_ref_drops_that_card(self, ref):
        # The row keeps its floor through the neighbour, so only the bad card
        # goes; a `full` block would lose the whole block instead.
        doc = _doc([_row(_app(appRef=ref), _coll(appRefs=["a", "b"]), _app(appRef="kept"))])
        got = _cards(oe.load_sections(fetcher=lambda: doc))
        assert [i["appRefs"] for i in got] == [["a", "b"], ["kept"]]

    def test_a_ref_list_is_not_accepted(self):
        """`appRefs` on an `app` item is the retired shape. Reading it would
        resurrect the ambiguity the two types exist to remove."""
        doc = _doc([_full({"type": "app", "appRefs": ["todo-ledger"]})])
        assert oe.load_sections(fetcher=lambda: doc) == []


class TestCollectionItems:
    def test_order_title_and_blurb_come_through(self):
        doc = _doc(
            [_full(_coll(appRefs=["a", "b", "c"], title="Staff picks", blurb="Three of them"))]
        )
        got = _cards(oe.load_sections(fetcher=lambda: doc))
        assert got[0]["appRefs"] == ["a", "b", "c"], "the curator's order is what renders"
        assert got[0]["title"] == "Staff picks"
        assert got[0]["blurb"] == "Three of them"

    def test_blank_and_non_string_refs_are_dropped(self):
        doc = _doc([_full(_coll(appRefs=["  a  ", "", 5, None, "b"]))])
        assert _cards(oe.load_sections(fetcher=lambda: doc))[0]["appRefs"] == ["a", "b"]

    def test_a_duplicate_ref_is_collapsed_keeping_the_first_position(self):
        doc = _doc([_full(_coll(appRefs=["a", "b", "a", "c"]))])
        assert _cards(oe.load_sections(fetcher=lambda: doc))[0]["appRefs"] == ["a", "b", "c"]

    def test_a_collection_without_a_title_is_dropped(self):
        """The theme is the only thing explaining why these apps share a card, so
        an untitled one is dropped rather than rendered as an anonymous pile."""
        doc = _doc([_row(_coll(title=None), _coll(title="kept"), _coll(title="kept too"))])
        got = _cards(oe.load_sections(fetcher=lambda: doc))
        assert [i["title"] for i in got] == ["kept", "kept too"]

    @pytest.mark.parametrize(
        "title", ["", "   ", 5, [], {}], ids=["blank", "ws", "int", "list", "dict"]
    )
    def test_an_unusable_title_is_dropped(self, title):
        assert oe.load_sections(fetcher=lambda: _doc([_full(_coll(title=title))])) == []

    def test_a_collection_that_falls_to_one_ref_is_dropped_not_demoted(self):
        """A one-app collection is an `app` placement wearing a costume. Showing
        the survivor under the group's theme would state something the curator
        did not write."""
        doc = _doc([_full(_coll(appRefs=["only", ""])), _full(_coll(appRefs=["a", "b"]))])
        got = _cards(oe.load_sections(fetcher=lambda: doc))
        assert [i["appRefs"] for i in got] == [["a", "b"]]

    @pytest.mark.parametrize("refs", ["nope", 5, None, {}], ids=["str", "int", "none", "dict"])
    def test_a_non_list_appRefs_drops_that_card(self, refs):
        doc = _doc([_full(_coll(appRefs=refs)), _full(_coll())])
        assert len(oe.load_sections(fetcher=lambda: doc)) == 1


class TestUnknownItemTypesAreSkipped:
    @pytest.mark.parametrize("kind", ["spotlight", "rail", "banner", "story", "", None, 5])
    def test_a_type_with_no_surface_skips_the_card_not_the_block(self, kind):
        # The narrower grain: the row's arrangement can still be drawn around a
        # card it does not know, so the neighbours survive. The retired
        # `spotlight` / `rail` / `banner` names take the same path.
        doc = _doc(
            [
                _row(
                    {"type": kind, "appRefs": ["a", "b"], "title": "T"},
                    _coll(appRefs=["a", "b"]),
                    _app(appRef="kept"),
                )
            ]
        )
        got = _cards(oe.load_sections(fetcher=lambda: doc))
        assert [i["appRefs"] for i in got] == [["a", "b"], ["kept"]]

    def test_a_non_dict_item_is_skipped(self):
        doc = _doc([_row("nope", _coll(appRefs=["a", "b"]), _app(appRef="kept"))])
        assert len(_cards(oe.load_sections(fetcher=lambda: doc))) == 2


class TestArtwork:
    def test_a_catalog_relative_ref_resolves_against_the_catalog_base(self):
        doc = _doc([_full(_app(artwork={"ref": "assets/editorial/abc.png"}))])
        art = _cards(oe.load_sections(fetcher=lambda: doc))[0]["artwork"]
        assert art["url"] == f"{oe.OFFICIAL_CATALOG_BASE}assets/editorial/abc.png"

    def test_the_dark_variant_and_alt_come_through(self):
        doc = _doc(
            [
                _full(
                    _app(
                        artwork={
                            "ref": "assets/editorial/a.png",
                            "refDark": "assets/editorial/b.png",
                            "alt": "  A quiet timeline  ",
                        }
                    )
                )
            ]
        )
        art = _cards(oe.load_sections(fetcher=lambda: doc))[0]["artwork"]
        assert art["urlDark"].endswith("b.png")
        assert art["alt"] == "A quiet timeline"

    @pytest.mark.parametrize(
        "ref",
        [
            "javascript:alert(1)",
            "data:image/svg+xml;base64,AA",
            "https://evil.example/x.png",
            "//evil.example/x.png",
            "assets/../../etc/passwd",
            "",
            None,
            5,
        ],
        ids=["js", "data", "https", "protocol-relative", "traversal", "empty", "none", "int"],
    )
    def test_anything_that_is_not_a_plain_path_is_dropped(self, ref):
        # `javascript:` and `data:` carry no slash after the colon, so a naive
        # `"://" in ref` test passes them straight into an `<img>` src.
        doc = _doc([_full(_app(artwork={"ref": ref}))])
        card = _cards(oe.load_sections(fetcher=lambda: doc))[0]
        assert "artwork" not in card, "the card survives, the artwork does not"

    def test_a_dark_only_artwork_is_dropped_entirely(self):
        # Rendering nothing on the default appearance is worse than no art.
        doc = _doc([_full(_app(artwork={"refDark": "assets/editorial/b.png"}))])
        assert "artwork" not in _cards(oe.load_sections(fetcher=lambda: doc))[0]

    def test_an_unusable_dark_variant_keeps_the_light_one(self):
        doc = _doc(
            [_full(_app(artwork={"ref": "assets/editorial/a.png", "refDark": "javascript:x"}))]
        )
        art = _cards(oe.load_sections(fetcher=lambda: doc))[0]["artwork"]
        assert art["url"].endswith("a.png")
        assert "urlDark" not in art

    @pytest.mark.parametrize("art", ["nope", 5, None, []], ids=["str", "int", "none", "list"])
    def test_a_non_dict_artwork_is_dropped(self, art):
        doc = _doc([_full(_app(artwork=art))])
        assert "artwork" not in _cards(oe.load_sections(fetcher=lambda: doc))[0]


class TestRefusalsAndCaps:
    @pytest.mark.parametrize(
        "version", [None, "1", 1.0, True, 2], ids=["none", "str", "float", "true", "2"]
    )
    def test_an_unsupported_schema_version_yields_nothing(self, version):
        doc = _doc([_full(_app())], version=version)
        assert oe.load_sections(fetcher=lambda: doc) == []

    @pytest.mark.parametrize("sections", ["nope", 5, None, {}], ids=["str", "int", "none", "dict"])
    def test_a_non_list_sections_field_yields_nothing(self, sections):
        assert oe.load_sections(fetcher=lambda: _doc(sections)) == []

    def test_a_failed_fetch_yields_nothing(self):
        assert oe.load_sections(fetcher=lambda: None) == []

    def test_more_blocks_than_the_cap_are_truncated(self):
        many = [_full(_app(appRef=f"a{i}")) for i in range(oe.MAX_SECTIONS + 10)]
        assert len(oe.load_sections(fetcher=lambda: _doc(many))) == oe.MAX_SECTIONS

    def test_more_refs_than_the_cap_are_truncated(self):
        doc = _doc([_full(_coll(appRefs=[f"a{i}" for i in range(oe.MAX_APP_REFS + 10)]))])
        got = _cards(oe.load_sections(fetcher=lambda: doc))
        assert len(got[0]["appRefs"]) == oe.MAX_APP_REFS


class TestTheLiveDocument:
    def test_the_live_shape_today_yields_no_sections(self):
        # `sections: []` is what the CDN publishes right now, so shipping this
        # consumer changes nothing until a curator authors a block.
        doc = {"schemaVersion": 1, "sections": []}
        assert oe.load_sections(fetcher=lambda: doc) == []
