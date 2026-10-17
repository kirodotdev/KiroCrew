"""Unit coverage for the pure, dependency-free helpers in
``kiro_crew.apps.registry_pipeline.caches``: the cache-stem sanitizer and the
manifest source-coordinate builder. Both feed cache FILE NAMES and KEYS derived
from values an external registry index controls, so each must map a hostile or
malformed value to a safe, distinct, non-crashing identity. These tests exercise
the sanitize/slug and malformed-default branches directly, with no filesystem or
git fixtures."""

from __future__ import annotations

import re
from hashlib import sha256

from kiro_crew.apps.registry_pipeline import caches


def test_a_pure_safe_name_is_returned_byte_identical() -> None:
    """A name of only ``[A-Za-z0-9_.-]`` with no ``..`` is kept as-is, so an
    existing cache stays valid across the sanitizer."""
    for name in ("my-app", "Registry_1", "a.b.c", "App-2.0_rc1"):
        assert caches._safe_cache_stem(name) == name


def test_a_traversal_or_separator_name_is_slugified_and_hash_disambiguated() -> None:
    """A name carrying a path separator or ``..`` traversal is slugified AND
    suffixed with a short stable hash of the ORIGINAL name, so the derived path
    can never escape the cache dir nor collide with another name."""
    hostile = "../../config"
    stem = caches._safe_cache_stem(hostile)
    # No traversal or separators survive.
    assert ".." not in stem
    assert "/" not in stem and "\\" not in stem
    # Slug + short stable hash of the original.
    expected_digest = sha256(hostile.encode("utf-8")).hexdigest()[:8]
    assert stem.endswith(expected_digest)
    assert re.match(r"^[A-Za-z0-9_\-]+$", stem)
    # Stable across calls.
    assert caches._safe_cache_stem(hostile) == stem
    # A name that slugifies to empty still yields a usable stem.
    only_bad = caches._safe_cache_stem("///")
    assert only_bad.startswith("app-")


def test_a_dotdot_only_name_is_also_disambiguated() -> None:
    """``..`` triggers the slug+hash path even without a separator character."""
    stem = caches._safe_cache_stem("a..b")
    assert stem.endswith(sha256("a..b".encode("utf-8")).hexdigest()[:8])


def test_source_coordinates_fold_in_branch_and_commit() -> None:
    """A well-formed entry yields ``(origin, ref, subdirectory, name)`` with the
    branch always folded into the ref and the commit folded in when present, so
    a branch change or a republished pin is a cache MISS, not a stale hit."""
    entry = {
        "name": "demo",
        "git": "https://example.com/org/repo.git",
        "branch": "release",
        "commit": "abc123",
        "subdirectory": "pkg",
    }
    origin, ref, subdirectory, name = caches._manifest_source_coordinates(entry)
    assert name == "demo"
    assert subdirectory == "pkg"
    assert ref == "branch:release|commit:abc123"
    # Origin is credential-free (userinfo stripped by the normalizer).
    assert "@" not in origin


def test_source_coordinates_default_a_missing_branch_to_main() -> None:
    """With no branch the ref defaults to ``branch:main``; absent commit leaves
    the ref commit-free."""
    _o, ref, _s, _n = caches._manifest_source_coordinates({"name": "x"})
    assert ref == "branch:main"


def test_source_coordinates_degrade_every_malformed_value_safely() -> None:
    """Every value an external index controls degrades to a safe default when it
    is not a string, producing a distinct-but-harmless identity rather than a
    crash: a non-string name/branch/subdirectory must not raise."""
    entry = {
        "name": 7,  # not a string
        "branch": None,  # not a string / falsy
        "commit": 123,  # not a string -> ignored
        "subdirectory": ["nope"],  # not a string
    }
    origin, ref, subdirectory, name = caches._manifest_source_coordinates(entry)
    assert name == ""
    assert subdirectory == ""
    assert ref == "branch:main"  # None branch -> default
    assert isinstance(origin, str)
