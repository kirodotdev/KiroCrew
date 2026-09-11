"""Presentation metadata, not ZIP part names, defines slide order and numbers."""

from __future__ import annotations

import zipfile

# Fixture construction/serialization only; all XML parsing stays in defusedxml.
# nosemgrep: python.lang.security.use-defused-xml.use-defused-xml
from xml.etree import ElementTree as ET

import pytest

from kiro_crew import doc_parser

_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_R = "http://schemas.openxmlformats.org/package/2006/relationships"
_PRESENTATION = "ppt/presentation.xml"
_RELS = "ppt/_rels/presentation.xml.rels"


def _deck(tmp_path, *, order=(2, 3, 1), targets=None, blank=(), external=(), broken=()):
    """Write the ordering parts of a deck with stable, independently named slides."""
    targets = targets or {i: f"slides/slide{i}.xml" for i in (1, 2, 3)}
    presentation = ET.Element(f"{{{_P}}}presentation")
    slide_ids = ET.SubElement(presentation, f"{{{_P}}}sldIdLst")
    relationships = ET.Element(f"{{{_PACKAGE_R}}}Relationships")
    parts = {}
    for i in order:
        ET.SubElement(slide_ids, f"{{{_P}}}sldId", {"id": str(255 + i), f"{{{_R}}}id": f"rId{i}"})
    for i, target in targets.items():
        if i not in broken:
            ET.SubElement(
                relationships,
                f"{{{_PACKAGE_R}}}Relationship",
                {
                    "Id": f"rId{i}",
                    "Type": f"{_R}/slide",
                    "Target": target,
                    "TargetMode": "External" if i in external else "Internal",
                },
            )
        slide = ET.Element(f"{{{_P}}}sld")
        if i not in blank:
            ET.SubElement(slide, f"{{{_A}}}t").text = f"Content {i}"
        # Tests spell the member name independently of production URI resolution.
        member = target.lstrip("/") if target.startswith("/") else f"ppt/{target}"
        parts[member] = ET.tostring(slide)
    parts[_PRESENTATION] = ET.tostring(presentation)
    parts[_RELS] = ET.tostring(relationships)
    path = tmp_path / "deck.pptx"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return path


@pytest.mark.parametrize("fileobj", [False, True])
def test_reordered_deck_uses_presentation_positions(tmp_path, fileobj):
    path = _deck(tmp_path)
    expected = (
        "--- Slide 1 ---\nContent 2\n\n"
        "--- Slide 2 ---\nContent 3\n\n"
        "--- Slide 3 ---\nContent 1"
    )
    if fileobj:
        with path.open("rb") as stream:
            assert doc_parser.extract_text(str(path), fileobj=stream) == expected
    else:
        assert doc_parser.extract_text(str(path)) == expected


def test_budget_keeps_the_actual_first_slide_and_never_reads_later_slides(tmp_path, monkeypatch):
    path = _deck(tmp_path)
    reads = []
    original = doc_parser._read_zip_entry

    def read(archive, name, *args, **kwargs):
        reads.append(name)
        return original(archive, name, *args, **kwargs)

    monkeypatch.setattr(doc_parser, "_read_zip_entry", read)
    assert doc_parser.extract_text(str(path), max_chars=1) == "--- Slide 1 ---\nContent 2"
    assert "ppt/slides/slide1.xml" not in reads
    assert "ppt/slides/slide3.xml" not in reads


def test_unreferenced_slide_parts_are_not_in_the_presentation(tmp_path):
    path = _deck(tmp_path, order=(2, 3))
    assert doc_parser.extract_text(str(path)) == (
        "--- Slide 1 ---\nContent 2\n\n--- Slide 2 ---\nContent 3"
    )


def test_blank_slide_keeps_the_following_presentation_number(tmp_path):
    path = _deck(tmp_path, blank=(2,))
    assert doc_parser.extract_text(str(path)) == (
        "--- Slide 2 ---\nContent 3\n\n--- Slide 3 ---\nContent 1"
    )


@pytest.mark.parametrize("target", ["slides/overview.xml", "/ppt/slides/overview.xml"])
def test_relationship_target_does_not_need_a_numeric_filename(tmp_path, target):
    path = _deck(tmp_path, order=(1,), targets={1: target})
    assert doc_parser.extract_text(str(path)) == "--- Slide 1 ---\nContent 1"


@pytest.mark.parametrize("unreadable", [{"external": (2,)}, {"broken": (2,)}])
def test_unresolvable_slide_does_not_reorder_the_remaining_slides(tmp_path, unreadable):
    path = _deck(tmp_path, **unreadable)
    assert doc_parser.extract_text(str(path)) == (
        "--- Slide 2 ---\nContent 3\n\n--- Slide 3 ---\nContent 1"
    )


def test_empty_presentation_does_not_fall_back_to_orphan_parts(tmp_path):
    path = _deck(tmp_path, order=())
    assert doc_parser.extract_text(str(path)) == ""


def test_repeated_references_read_each_slide_part_only_once(tmp_path, monkeypatch):
    path = _deck(tmp_path, order=(2,) * 100 + (3, 1))
    reads = []
    original = doc_parser._read_zip_entry

    def read(archive, name, *args, **kwargs):
        reads.append(name)
        return original(archive, name, *args, **kwargs)

    monkeypatch.setattr(doc_parser, "_read_zip_entry", read)
    assert doc_parser.extract_text(str(path)) == (
        "--- Slide 1 ---\nContent 2\n\n"
        "--- Slide 101 ---\nContent 3\n\n"
        "--- Slide 102 ---\nContent 1"
    )
    assert reads.count("ppt/slides/slide2.xml") == 1


def test_distinct_relationships_to_the_same_part_do_not_duplicate_text(tmp_path):
    path = _deck(
        tmp_path,
        order=(2, 1, 3),
        targets={1: "slides/shared.xml", 2: "slides/shared.xml", 3: "slides/slide3.xml"},
    )
    assert doc_parser.extract_text(str(path)) == (
        "--- Slide 1 ---\nContent 2\n\n--- Slide 3 ---\nContent 3"
    )


@pytest.mark.parametrize("part", [_PRESENTATION, _RELS])
def test_ordering_metadata_keeps_the_existing_read_cap(tmp_path, monkeypatch, part):
    path = _deck(tmp_path)
    original = doc_parser._read_zip_entry

    def capped_read(archive, name, *args, **kwargs):
        return original(archive, name, max_size=1 if name == part else None)

    monkeypatch.setattr(doc_parser, "_read_zip_entry", capped_read)
    assert doc_parser.extract_text(str(path)) == ""


@pytest.mark.parametrize("part", [_PRESENTATION, _RELS])
@pytest.mark.parametrize("xml", [b"<broken", b'<!DOCTYPE x [<!ENTITY e "expanded">]><x>&e;</x>'])
def test_invalid_ordering_metadata_does_not_fall_back_to_filename_order(
    tmp_path, monkeypatch, part, xml
):
    path = _deck(tmp_path)
    original = doc_parser._read_zip_entry

    def read(archive, name, *args, **kwargs):
        return xml if name == part else original(archive, name, *args, **kwargs)

    monkeypatch.setattr(doc_parser, "_read_zip_entry", read)
    assert doc_parser.extract_text(str(path)) == ""


def test_unchanged_order_is_a_control(tmp_path):
    path = _deck(tmp_path, order=(1, 2, 3))
    assert doc_parser.extract_text(str(path)) == (
        "--- Slide 1 ---\nContent 1\n\n"
        "--- Slide 2 ---\nContent 2\n\n"
        "--- Slide 3 ---\nContent 3"
    )
