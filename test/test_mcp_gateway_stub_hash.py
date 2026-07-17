"""_hash_permission_profile must be injective over the auto-approve set.

A non-injective delimiter would let two distinct permission surfaces collide
onto one PoolKey/backend, defeating the per-permission isolation the hash
exists to enforce.
"""

from __future__ import annotations

from kiro_crew.mcp_gateway.stub import _hash_permission_profile


def test_comma_bearing_tool_name_does_not_collide() -> None:
    # ["a,b"] and ["a", "b"] both flattened to "a,b" under the old comma
    # delimiter; with a NUL delimiter they must hash differently.
    h1 = _hash_permission_profile(["a,b"], "interactive", False)
    h2 = _hash_permission_profile(["a", "b"], "interactive", False)
    assert h1 != h2


def test_same_set_is_stable_and_order_independent() -> None:
    h1 = _hash_permission_profile(["read", "write"], "interactive", False)
    h2 = _hash_permission_profile(["write", "read"], "interactive", False)
    assert h1 == h2  # sorted() inside -> order-independent


def test_mode_and_trust_are_part_of_the_key() -> None:
    base = _hash_permission_profile(["read"], "interactive", False)
    assert base != _hash_permission_profile(["read"], "autonomous", False)
    assert base != _hash_permission_profile(["read"], "interactive", True)
