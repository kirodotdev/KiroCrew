"""A JSON ``null`` author or licence in app.json is no value, never the text "None".

``AppManifest.from_dict`` runs the optional ``author`` and ``license`` fields
through ``str()``; a ``null`` must not reach it, or ``"author": null`` reads as
the author "None" on the app's detail page.

``permissions.memory`` keeps its ``str()`` form here: the page reads
"False"/"None" as no request (``memoryAccessLabel.memoryRequested``). The
admission signature covers the app.json document as written, so neither
normalization changes what a signature covers.
"""

from __future__ import annotations

import pytest
from app_manifest_helpers import _valid_manifest

from kiro_crew.apps.manifest import AppManifest


@pytest.mark.parametrize("field", ["author", "license"])
def test_a_null_optional_text_field_is_empty(field: str) -> None:
    m = AppManifest.from_dict(_valid_manifest(**{field: None}))
    assert getattr(m, field) == ""
    assert field not in m.to_dict()


@pytest.mark.parametrize("field", ["author", "license"])
def test_a_present_optional_text_field_is_kept(field: str) -> None:
    m = AppManifest.from_dict(_valid_manifest(**{field: "Acme"}))
    assert getattr(m, field) == "Acme"


def test_the_signed_bytes_cover_author_and_licence_as_written() -> None:
    # The signature covers the document, so author and licence are signed, and a
    # null is signed as null rather than as the empty value the page shows.
    with_null = AppManifest.from_dict(_valid_manifest(author=None, license=None))
    with_text = AppManifest.from_dict(_valid_manifest(author="Acme", license="MIT"))
    assert with_null.signing_payload() != with_text.signing_payload()
    assert b'"author":null' in with_null.signing_payload()
    assert b'"license":null' in with_null.signing_payload()


@pytest.mark.parametrize("value", [False, None])
def test_a_false_or_null_memory_keeps_its_signed_form(value: object) -> None:
    m = AppManifest.from_dict(_valid_manifest(permissions={"memory": value}))
    assert m.permissions.memory == str(value)
