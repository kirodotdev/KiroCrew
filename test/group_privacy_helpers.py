"""Fakes for the group-privacy questions in :mod:`kiro_crew.platform_compat`.

Those questions read the account databases and a directory's POSIX access ACL.
The pins that drive them state both rather than inherit the test host's: a
corporate host's groups are shared and a container's are private, so a pin that
read the real ones would assert something different on each.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path


class _FakeGroup:
    """A ``grp`` record with a chosen member list, for the group-privacy pins.

    Named tuples from ``grp`` cannot be constructed with an arbitrary member list
    without also supplying a real gid that exists on the host, and the point of
    these pins is to state the membership rather than inherit the host's.
    """

    def __init__(self, name: str, members: list[str]) -> None:
        self.gr_name = name
        self.gr_gid = -1
        self.gr_mem = members


class _FakeUser:
    """A ``pwd`` record, for asserting a passwd enumeration that omits this account."""

    def __init__(self, name: str, gid: int) -> None:
        self.pw_name = name
        self.pw_gid = gid


_ACL_USER_OBJ, _ACL_USER, _ACL_GROUP_OBJ, _ACL_GROUP, _ACL_MASK, _ACL_OTHER = (
    0x01,
    0x02,
    0x04,
    0x08,
    0x10,
    0x20,
)


def _acl_xattr(*named: tuple[int, int, int], mask: int = 0o7, group_obj: int = 0o0) -> bytes:
    """A ``system.posix_acl_access`` value in the kernel's on-disk layout.

    ``named`` holds ``(tag, perm, id)`` entries for named users and groups; the
    owner, owning-group, mask and other entries every extended ACL carries are
    filled in around them.
    """
    unset = 0xFFFFFFFF
    entries = [(_ACL_USER_OBJ, 0o7, unset), *named, (_ACL_GROUP_OBJ, group_obj, unset)]
    entries += [(_ACL_MASK, mask, unset), (_ACL_OTHER, 0o0, unset)]
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *e) for e in entries)


def _serve_acl(monkeypatch, node: Path, value) -> None:
    """Make ``os.getxattr`` answer ``value`` for ``node``'s access ACL.

    ``value`` is the bytes to return, or an ``OSError`` to raise. Every other
    path and attribute goes to the real call, so the rest of the chain reads as
    the host has it.
    """
    real = os.getxattr

    def _fake(path, attribute, *a, **kw):
        if Path(path) == node and attribute == "system.posix_acl_access":
            if isinstance(value, OSError):
                raise value
            return value
        return real(path, attribute, *a, **kw)

    monkeypatch.setattr(os, "getxattr", _fake)
