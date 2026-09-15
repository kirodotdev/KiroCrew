"""The strict and mixed internal-API buckets must not overlap.

``_STRICT_INTERNAL_API_PATHS`` and ``_MIXED_INTERNAL_API_PATHS`` carry OPPOSITE auth
policy for a caller that is not on loopback: a strict path is hard-denied, a mixed path
falls through to ordinary cookie auth so a dashboard page can call it. ``token_auth``'s
middleware resolves the two independently, each by prefix-or-exact match against its own
set, so an entry satisfying both leaves the request's policy decided by the order the two
checks happen to run in rather than by anything a reader can see at the definition site.

An overlap does not merely make membership ambiguous, it silently changes which branch a
request takes. The remote-access promotion that widens strict paths to mixed is guarded on
"matches strict AND NOT mixed", so an overlapping entry skips that promotion and stays
strict even once the operator has opted into remote access.

Exact duplication is the obvious form. The form that reads as deliberate and still
collides is a prefix-nested split -- putting ``/api/artifacts`` in one bucket and a
hypothetical ``/api/artifacts/detail`` in the other -- because the middleware extends
every entry by ``/`` when matching. ``_collides`` mirrors that matcher, and the two
predicate tests below exist so a wrong mirror cannot pass unnoticed; if the middleware's
matching shape changes, this file has to follow it.
"""

from __future__ import annotations

from kiro_crew.dashboard.server import (
    _MIXED_INTERNAL_API_PATHS,
    _STRICT_INTERNAL_API_PATHS,
)


def _collides(a: str, b: str) -> bool:
    """Mirror of the middleware's prefix-or-exact match, applied across buckets."""
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


class TestInternalPathBucketDisjointness:
    def test_buckets_do_not_collide(self):
        """A path in both buckets has two contradictory auth policies."""
        clashes = sorted(
            (strict, mixed)
            for strict in _STRICT_INTERNAL_API_PATHS
            for mixed in _MIXED_INTERNAL_API_PATHS
            if _collides(strict, mixed)
        )
        assert not clashes, f"strict/mixed entries collide (prefix or exact): {clashes}"

    def test_neither_bucket_is_empty(self):
        """The collision check is a cross product, so an empty set passes it for free."""
        assert _STRICT_INTERNAL_API_PATHS
        assert _MIXED_INTERNAL_API_PATHS

    def test_collision_predicate_fires_on_exact_and_nested_paths(self):
        """Both collision shapes, in both nesting directions."""
        assert _collides("/api/example", "/api/example")
        assert _collides("/api/example/detail", "/api/example")
        assert _collides("/api/example", "/api/example/detail")

    def test_collision_predicate_ignores_sibling_prefixes(self):
        """A shared string prefix is not a shared path prefix -- the ``/`` matters.

        Each pair is a real neighbour inside one bucket, and each is a raw string prefix of
        the other, so dropping the ``/`` turns the check into a false positive on names
        that merely start alike.
        """
        assert not _collides("/api/browser/command", "/api/browser/command-drain")
        assert not _collides("/api/browser/command", "/api/browser/command-result")
