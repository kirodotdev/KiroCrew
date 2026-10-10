"""Nested shapes for agentic dashboard fields, declared and checked at load.

A field's top-level ``type`` alone let a Needs-you card written as ``{title, detail}``
land as "an array" while the page, which reads ``text``, dropped every row of it. The
cases here pin the LOAD half of the gap: the manifest grammar in
:mod:`kiro_crew.dashboard_templates.manifest` declares a shape, and a shape it cannot
express is refused when the manifest is parsed rather than when a value arrives.

Every case builds its own manifest, so the grammar is checked against inputs chosen to
exercise it rather than against whatever a shipped page happens to declare. The write
half -- a value checked against a declared shape -- is pinned in
``test_dynamic_dashboard.py`` beside the rest of ``dashboard_agentic.check_write``.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.dashboard_templates.manifest import ManifestError, parse_manifest


def _raw(**field: Any) -> dict[str, Any]:
    return {
        "id": "demo",
        "version": 1,
        "title": "Demo",
        "description": "A demo",
        "source": "user",
        "fields": {"f": {"source": {"agentic": True}, **field}},
    }


# -------------------------------------------------------------------------- #
# the manifest declares a shape and the loader checks it
# -------------------------------------------------------------------------- #


class TestTheManifestShape:
    @pytest.mark.parametrize(
        ("field", "needle"),
        [
            ({"type": "string", "items": {"type": "string"}}, "items is only for an array"),
            ({"type": "array", "items": {"type": "bogus"}}, "type 'bogus'"),
            ({"type": "array", "items": {"type": "string", "propertys": {}}}, "unknown shape key"),
            (
                {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["b"]},
                "undeclared properties ['b']",
            ),
            (
                {
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                    "values": {"type": "string"},
                },
                "cannot both be declared",
            ),
            ({"type": "string", "enum": []}, "enum must list"),
            ({"type": "string", "enum": [1]}, "enum must list"),
            ({"type": "array", "enum": ["a"]}, "enum is only for"),
        ],
    )
    def test_a_bad_shape_is_refused_at_load(self, field: dict[str, Any], needle: str) -> None:
        with pytest.raises(ManifestError) as caught:
            parse_manifest(_raw(**field))
        assert needle in str(caught.value), str(caught.value)

    def test_a_shape_nested_past_the_bound_is_refused(self) -> None:
        shape: dict[str, Any] = {"type": "string"}
        for _ in range(9):
            shape = {"type": "array", "items": shape}
        with pytest.raises(ManifestError, match="deeper than"):
            parse_manifest(_raw(**shape))

    def test_a_fold_field_cannot_declare_a_shape(self) -> None:
        raw = _raw(type="string")
        raw["fields"]["g"] = {
            "type": "array",
            "items": {"type": "string"},
            "source": {"fold": "work", "path": "items"},
        }
        with pytest.raises(ManifestError, match="only an agentic field declares a shape"):
            parse_manifest(raw)

    def test_a_shape_the_grammar_accepts_round_trips(self) -> None:
        """The POSITIVE control: a refusal set proves nothing without one accepted shape.

        Every other case here asserts a refusal, so a grammar that refused everything
        would read exactly as green. This one requires the parse to succeed AND the
        parsed shape to carry back what was declared.
        """
        parsed = parse_manifest(
            _raw(
                type="array",
                items={
                    "type": "object",
                    "properties": {"text": {"type": "string"}, "ask": {"type": "string"}},
                    "required": ["text"],
                },
            )
        )
        item = parsed.fields["f"].shape.items
        assert item is not None
        assert set(item.properties) == {"text", "ask"}
        assert item.required == ("text",)
