"""Tests for projecting editorial spotlight sections.

Two properties carry the weight here: an unknown section `type` is SKIPPED (the
document's own stated contract, and what lets a curator publish a shape before
every client renders it), and artwork that is not a plain path is DROPPED rather
than handed to an `<img>`.
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


class TestAppSections:
    def test_a_single_ref_comes_through_as_a_one_entry_list(self):
        # Both types spell refs as a list so the caller resolves them one way;
        # `type` is what the renderer branches on.
        got = oe.load_sections(fetcher=lambda: _doc([_app()]))
        assert got == [{"type": "app", "appRefs": ["todo-ledger"]}]

    def test_a_blurb_comes_through(self):
        doc = _doc([_app(blurb="Every page on one screen.")])
        assert oe.load_sections(fetcher=lambda: doc)[0]["blurb"] == "Every page on one screen."

    def test_a_published_title_is_ignored(self):
        """The app's own name is the heading. A curator wanting other words is
        describing a collection, and honouring a title here would give two ways
        to spell one card."""
        doc = _doc([_app(title="Our pick of the week")])
        assert "title" not in oe.load_sections(fetcher=lambda: doc)[0]

    @pytest.mark.parametrize(
        "ref", ["", "   ", None, 5, [], {}], ids=["blank", "ws", "none", "int", "list", "dict"]
    )
    def test_an_unusable_ref_drops_that_section(self, ref):
        doc = _doc([_app(appRef=ref), _app(appRef="kept")])
        assert [s["appRefs"] for s in oe.load_sections(fetcher=lambda: doc)] == [["kept"]]

    def test_a_ref_list_is_not_accepted(self):
        """`appRefs` on an `app` section is the retired shape. Reading it would
        resurrect the ambiguity the two types exist to remove."""
        doc = _doc([{"type": "app", "appRefs": ["todo-ledger"]}])
        assert oe.load_sections(fetcher=lambda: doc) == []


class TestCollectionSections:
    def test_order_title_and_blurb_come_through(self):
        doc = _doc([_coll(appRefs=["a", "b", "c"], title="Staff picks", blurb="Three of them")])
        got = oe.load_sections(fetcher=lambda: doc)
        assert got[0]["appRefs"] == ["a", "b", "c"], "the curator's order is what renders"
        assert got[0]["title"] == "Staff picks"
        assert got[0]["blurb"] == "Three of them"

    def test_blank_and_non_string_refs_are_dropped(self):
        doc = _doc([_coll(appRefs=["  a  ", "", 5, None, "b"])])
        assert oe.load_sections(fetcher=lambda: doc)[0]["appRefs"] == ["a", "b"]

    def test_a_duplicate_ref_is_collapsed_keeping_the_first_position(self):
        doc = _doc([_coll(appRefs=["a", "b", "a", "c"])])
        assert oe.load_sections(fetcher=lambda: doc)[0]["appRefs"] == ["a", "b", "c"]

    def test_a_collection_without_a_title_is_dropped(self):
        """The theme is the only thing explaining why these apps share a card, so
        an untitled one is dropped rather than rendered as an anonymous pile."""
        doc = _doc([_coll(title=None), _coll(title="kept")])
        assert [s["title"] for s in oe.load_sections(fetcher=lambda: doc)] == ["kept"]

    @pytest.mark.parametrize(
        "title", ["", "   ", 5, [], {}], ids=["blank", "ws", "int", "list", "dict"]
    )
    def test_an_unusable_title_is_dropped(self, title):
        assert oe.load_sections(fetcher=lambda: _doc([_coll(title=title)])) == []

    def test_a_collection_that_falls_to_one_ref_is_dropped_not_demoted(self):
        """A one-app collection is an `app` placement wearing a costume. Showing
        the survivor under the group's theme would state something the curator
        did not write."""
        doc = _doc([_coll(appRefs=["only", ""]), _coll(appRefs=["a", "b"])])
        got = oe.load_sections(fetcher=lambda: doc)
        assert [s["appRefs"] for s in got] == [["a", "b"]]

    @pytest.mark.parametrize("refs", ["nope", 5, None, {}], ids=["str", "int", "none", "dict"])
    def test_a_non_list_appRefs_drops_that_section(self, refs):
        doc = _doc([_coll(appRefs=refs), _coll()])
        assert len(oe.load_sections(fetcher=lambda: doc)) == 1

    def test_several_sections_of_both_types_all_come_through(self):
        doc = _doc([_app(appRef="a"), _coll(appRefs=["b", "c"]), _app(appRef="d")])
        got = oe.load_sections(fetcher=lambda: doc)
        assert [(s["type"], s["appRefs"]) for s in got] == [
            ("app", ["a"]),
            ("collection", ["b", "c"]),
            ("app", ["d"]),
        ]


class TestUnknownTypesAreSkipped:
    @pytest.mark.parametrize("kind", ["spotlight", "rail", "banner", "story", "", None, 5])
    def test_a_type_with_no_surface_is_skipped_not_refused(self, kind):
        # Skipping is the document's contract. A future type must not take the
        # whole layout down on a client that predates it -- and the retired
        # `spotlight` / `rail` / `banner` names take the same path, so a stale
        # document degrades to the derived layout instead of failing.
        doc = _doc([{"type": kind, "appRefs": ["a", "b"], "title": "T"}, _app(appRef="kept")])
        got = oe.load_sections(fetcher=lambda: doc)
        assert [s["appRefs"] for s in got] == [["kept"]]

    def test_a_non_dict_section_is_skipped(self):
        doc = _doc(["nope", 5, None, _app(appRef="kept")])
        assert len(oe.load_sections(fetcher=lambda: doc)) == 1


class TestArtwork:
    def test_a_catalog_relative_ref_resolves_against_the_catalog_base(self):
        doc = _doc([_app(artwork={"ref": "assets/editorial/abc.png"})])
        art = oe.load_sections(fetcher=lambda: doc)[0]["artwork"]
        assert art["url"] == f"{oe.OFFICIAL_CATALOG_BASE}assets/editorial/abc.png"

    def test_the_dark_variant_and_alt_come_through(self):
        doc = _doc(
            [
                _app(
                    artwork={
                        "ref": "assets/editorial/a.png",
                        "refDark": "assets/editorial/b.png",
                        "alt": "  A quiet timeline  ",
                    }
                )
            ]
        )
        art = oe.load_sections(fetcher=lambda: doc)[0]["artwork"]
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
        doc = _doc([_app(artwork={"ref": ref})])
        section = oe.load_sections(fetcher=lambda: doc)[0]
        assert "artwork" not in section, "the section survives, the artwork does not"

    def test_a_dark_only_artwork_is_dropped_entirely(self):
        # Rendering nothing on the default appearance is worse than no art.
        doc = _doc([_app(artwork={"refDark": "assets/editorial/b.png"})])
        assert "artwork" not in oe.load_sections(fetcher=lambda: doc)[0]

    def test_an_unusable_dark_variant_keeps_the_light_one(self):
        doc = _doc([_app(artwork={"ref": "assets/editorial/a.png", "refDark": "javascript:x"})])
        art = oe.load_sections(fetcher=lambda: doc)[0]["artwork"]
        assert art["url"].endswith("a.png")
        assert "urlDark" not in art

    @pytest.mark.parametrize("art", ["nope", 5, None, []], ids=["str", "int", "none", "list"])
    def test_a_non_dict_artwork_is_dropped(self, art):
        doc = _doc([_app(artwork=art)])
        assert "artwork" not in oe.load_sections(fetcher=lambda: doc)[0]


class TestRefusalsAndCaps:
    @pytest.mark.parametrize(
        "version", [None, "1", 1.0, True, 2], ids=["none", "str", "float", "true", "2"]
    )
    def test_an_unsupported_schema_version_yields_nothing(self, version):
        doc = _doc([_app()], version=version)
        assert oe.load_sections(fetcher=lambda: doc) == []

    @pytest.mark.parametrize("sections", ["nope", 5, None, {}], ids=["str", "int", "none", "dict"])
    def test_a_non_list_sections_field_yields_nothing(self, sections):
        assert oe.load_sections(fetcher=lambda: _doc(sections)) == []

    def test_a_failed_fetch_yields_nothing(self):
        assert oe.load_sections(fetcher=lambda: None) == []

    def test_more_sections_than_the_cap_are_truncated(self):
        many = [_app(appRef=f"a{i}") for i in range(oe.MAX_SECTIONS + 10)]
        assert len(oe.load_sections(fetcher=lambda: _doc(many))) == oe.MAX_SECTIONS

    def test_more_refs_than_the_cap_are_truncated(self):
        doc = _doc([_coll(appRefs=[f"a{i}" for i in range(oe.MAX_APP_REFS + 10)])])
        assert len(oe.load_sections(fetcher=lambda: doc)[0]["appRefs"]) == oe.MAX_APP_REFS


class TestOneDocumentTwoReaders:
    def test_both_readers_share_one_fetch(self):
        calls: list[int] = []

        def fetcher():
            calls.append(1)
            return {
                "schemaVersion": 1,
                "categories": [{"id": "other", "label": "Other", "order": 1}],
                "sections": [_app()],
            }

        assert oe.load_category_order(fetcher=fetcher) == ["other"]
        assert len(oe.load_sections(fetcher=fetcher)) == 1
        assert len(calls) == 1, "the second reader must be served from cache"

    def test_the_live_shape_today_yields_no_sections(self):
        # `sections: []` is what the CDN publishes right now, so shipping this
        # consumer changes nothing until a curator authors a section.
        doc = {"schemaVersion": 1, "categories": [], "sections": []}
        assert oe.load_sections(fetcher=lambda: doc) == []
