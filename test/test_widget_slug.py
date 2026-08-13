"""Slug-derivation parity tests (Python side).

The vectors in :data:`PARITY_VECTORS` are duplicated verbatim in
``website/src/test/widgetSlug.test.ts``. They are the contract that keeps
``kiro_crew.widget_slug.derive_widget_slug`` and the frontend's
``deriveWidgetSlug`` producing identical output — if they drift, an
auto-registered widget artifact becomes invisible to the frontend probe and the
star button creates a duplicate. Change a vector here and the TS suite fails too.
"""

from __future__ import annotations

import pytest

from kiro_crew.widget_slug import DERIVED_SLUG_RE, derive_widget_slug

#: ``(message_ts, widget_index) -> slug``. Values captured from the frontend
#: implementation; both suites assert against these exact strings.
PARITY_VECTORS = [
    ("1779995123.456789", 0, "4dc7b6b89ccdb068"),
    ("1779995123.456789", 1, "4ec7b84b9dcdb1fb"),
    ("1779995123.456789", 2, "4fc7b9de9ecdb38e"),
    ("abc", 0, "9eeb65d8bb7210c8"),
    ("", 0, "07c8788634148f16"),
    ("2026-07-27T12:00:00.000Z", 0, "3f8bf58f985f41bf"),
    # Non-ASCII: exercises the UTF-16-code-unit iteration both sides must agree
    # on (JS charCodeAt vs Python code points).
    ("日本語", 0, "19d0c6e579a66e35"),
    # Astral plane (emoji) — one Python code point, two JS surrogates. Pins the
    # surrogate-pair split in derive_widget_slug.
    ("a\U0001f600b", 0, "c3c71a8239d21872"),
    # Two-digit index: the seed is a string join, so 10 must not collide with 1.
    ("1779995123.456789", 10, "7f6769a135cee291"),
]


class TestDeriveWidgetSlug:
    @pytest.mark.parametrize("message_ts,widget_index,expected", PARITY_VECTORS)
    def test_matches_frontend_vector(self, message_ts: str, widget_index: int, expected: str):
        assert derive_widget_slug(message_ts, widget_index) == expected

    def test_output_shape_is_16_lowercase_hex(self):
        for message_ts, widget_index, _ in PARITY_VECTORS:
            slug = derive_widget_slug(message_ts, widget_index)
            assert DERIVED_SLUG_RE.match(slug), slug

    def test_deterministic(self):
        assert derive_widget_slug("ts", 3) == derive_widget_slug("ts", 3)

    def test_index_changes_slug(self):
        assert derive_widget_slug("ts", 0) != derive_widget_slug("ts", 1)

    def test_ts_changes_slug(self):
        assert derive_widget_slug("ts-a", 0) != derive_widget_slug("ts-b", 0)

    def test_index_is_not_string_concatenation_ambiguous(self):
        """``("a1", 0)`` and ``("a", 10)`` must not collide.

        The seed joins with ``#``, which is absent from both inputs here, so the
        separator is what prevents the collision. A regression that dropped it
        would silently alias two different widgets onto one artifact.
        """
        assert derive_widget_slug("a1", 0) != derive_widget_slug("a", 10)

    def test_slug_passes_artifact_slug_validation(self):
        """A derived slug must be a legal artifact slug, or registration 500s."""
        from kiro_crew.artifacts import _validate_slug

        for message_ts, widget_index, _ in PARITY_VECTORS:
            slug = derive_widget_slug(message_ts, widget_index)
            assert _validate_slug(slug) == slug
