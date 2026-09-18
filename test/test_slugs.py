"""Tests for the shared slug hash fallback.

The four backend slug builders fall back to ``slug_hash_fallback`` when a
name filters to the empty string, so distinct non-ASCII names must derive
distinct, stable, pattern-safe identifiers.
"""

from __future__ import annotations

import re

from kiro_crew.slugs import slug_hash_fallback

_SHAPE_RE = re.compile(r"^[a-z0-9-]+-[0-9a-f]{16}$")


class TestSlugHashFallback:
    def test_distinct_inputs_derive_distinct_ids(self) -> None:
        assert slug_hash_fallback("\u4f1a\u8bae\u7eaa\u8981", "artifact") != slug_hash_fallback(
            "\u8cb7\u3044\u7269\u30ea\u30b9\u30c8", "artifact"
        )

    def test_same_input_is_stable(self) -> None:
        assert slug_hash_fallback("\u4f1a\u8bae\u7eaa\u8981", "workflow") == slug_hash_fallback(
            "\u4f1a\u8bae\u7eaa\u8981", "workflow"
        )

    def test_shape_is_prefix_dash_16_hex(self) -> None:
        out = slug_hash_fallback("\u0440\u0430\u0431\u043e\u0442\u0430", "instance")
        assert out.startswith("instance-")
        assert _SHAPE_RE.match(out)

    def test_prefix_separates_namespaces(self) -> None:
        assert slug_hash_fallback("\u4f1a\u8bae", "artifact") != slug_hash_fallback(
            "\u4f1a\u8bae", "custom"
        )

    def test_lone_surrogate_input_is_total(self) -> None:
        # A JSON string escape can decode to a lone surrogate; the fallback
        # must derive an id for it rather than raise UnicodeEncodeError.
        out = slug_hash_fallback("\ud800", "artifact")
        assert _SHAPE_RE.match(out)
        assert out == slug_hash_fallback("\ud800", "artifact")
        assert out != slug_hash_fallback("\udfff", "artifact")
