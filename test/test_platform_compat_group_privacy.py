"""Whose a group write bit is: the group-privacy questions in ``platform_compat``.

A group write bit lets every holder of the group replace what a directory holds,
so a check that credits the bit to the directory's owner has to know whom else it
admits. :func:`platform_compat.group_write_admits_another_account` is that one
question, asked by the cloud launcher's staging-chain guard and by the
provider-CLI resolver. These pin its two halves, the group's membership
(:func:`platform_compat._group_shared_with_another_account`) and the POSIX access
ACL (:func:`platform_compat._acl_admits_another_account`), and how it joins them.
"""

from __future__ import annotations

import errno
import os
import shutil
import struct
import subprocess

import pytest
from group_privacy_helpers import (
    _ACL_GROUP,
    _ACL_USER,
    _acl_xattr,
    _FakeGroup,
    _FakeUser,
    _serve_acl,
)

from kiro_crew import platform_compat


class TestGroupSharedWithAnotherAccount:
    """The membership half: every group entry carrying the gid and the passwd
    database, both read in full."""

    def test_group_privacy_fails_closed_when_the_host_will_not_enumerate(self, monkeypatch):
        # Drives the REAL helper. A directory backend that resolves the group but
        # will not enumerate passwd cannot show that no other account shares the gid,
        # so privacy is UNPROVEN and that must read as shared. The account's own
        # absence from the enumeration is the control: a non-empty list that does not
        # contain this account is still not an enumeration.
        if os.name != "posix":
            pytest.skip("POSIX group databases")
        import grp
        import pwd

        me = pwd.getpwuid(os.geteuid())
        mine = grp.getgrgid(me.pw_gid)

        monkeypatch.setattr(grp, "getgrgid", lambda gid: _FakeGroup("solo", []))
        monkeypatch.setattr(grp, "getgrall", lambda: [])
        monkeypatch.setattr(pwd, "getpwuid", lambda uid: me)
        monkeypatch.setattr(pwd, "getpwall", lambda: [])
        empty = platform_compat._group_shared_with_another_account(mine.gr_gid)
        assert empty is not None and "will not enumerate" in empty, empty

        monkeypatch.setattr(pwd, "getpwall", lambda: [_FakeUser("somebody-else", 4242)])
        no_control = platform_compat._group_shared_with_another_account(mine.gr_gid)
        assert no_control is not None and "will not enumerate" in no_control, no_control

        monkeypatch.setattr(pwd, "getpwall", lambda: [me])
        proven = platform_compat._group_shared_with_another_account(mine.gr_gid)
        assert proven is None, f"a group holding only this account is private, got {proven}"

    def test_both_halves_of_group_membership_are_load_bearing(self, monkeypatch):
        # Drives the REAL helper. On a corporate host BOTH halves fire at once, so a
        # single case cannot tell which one is carrying the verdict and a mutation
        # deleting either would survive. Each case here makes exactly one half fire.
        if os.name != "posix":
            pytest.skip("POSIX group databases")
        import grp
        import pwd

        me = pwd.getpwuid(os.geteuid())
        gid = me.pw_gid
        monkeypatch.setattr(pwd, "getpwuid", lambda uid: me)
        monkeypatch.setattr(grp, "getgrall", lambda: [])

        # Supplementary half alone: gr_mem names somebody else, and passwd shows this
        # account as the only holder of the gid.
        monkeypatch.setattr(grp, "getgrgid", lambda g: _FakeGroup("shared", [me.pw_name, "peer"]))
        monkeypatch.setattr(pwd, "getpwall", lambda: [me])
        by_gr_mem = platform_compat._group_shared_with_another_account(gid)
        assert by_gr_mem is not None and "shared with 1 other" in by_gr_mem, by_gr_mem

        # Primary half alone: gr_mem is empty, which is exactly how a group shared by
        # primary membership presents itself, and passwd holds the other account.
        monkeypatch.setattr(grp, "getgrgid", lambda g: _FakeGroup("shared", []))
        monkeypatch.setattr(pwd, "getpwall", lambda: [me, _FakeUser("peer", gid)])
        by_primary = platform_compat._group_shared_with_another_account(gid)
        assert by_primary is not None and "primary group of 1 other" in by_primary, by_primary

        # And a peer on a DIFFERENT gid proves the primary half discriminates on the
        # gid rather than merely on the enumeration holding more than one row.
        monkeypatch.setattr(pwd, "getpwall", lambda: [me, _FakeUser("peer", gid + 1)])
        unrelated = platform_compat._group_shared_with_another_account(gid)
        assert unrelated is None, f"a peer in another group is not a sharer, got {unrelated}"

    def test_group_privacy_matches_this_hosts_own_databases(self):
        # The real helper against the real host, checked against membership computed
        # independently here rather than against the helper's own answer. Either
        # verdict is a pass; disagreeing with the databases is the failure.
        if os.name != "posix":
            pytest.skip("POSIX group databases")
        import grp
        import pwd

        me = pwd.getpwuid(os.geteuid())
        entry = grp.getgrgid(me.pw_gid)
        same_gid = [g for g in grp.getgrall() if g.gr_gid == me.pw_gid]
        supplementary = {
            name for g in [entry, *same_gid] for name in g.gr_mem if name != me.pw_name
        }
        everyone = pwd.getpwall()
        enumerates = any(p.pw_name == me.pw_name for p in everyone)
        primary = {p.pw_name for p in everyone if p.pw_gid == me.pw_gid and p.pw_name != me.pw_name}
        expected_private = not supplementary and enumerates and not primary
        verdict = platform_compat._group_shared_with_another_account(me.pw_gid)
        assert (verdict is None) == expected_private, (
            f"helper said {verdict!r} for gid {me.pw_gid} ({entry.gr_name}), but the "
            f"databases say supplementary={sorted(supplementary)} "
            f"enumerates={enumerates} other_primary={len(primary)}"
        )

    def test_a_second_group_entry_with_the_same_gid_counts_as_shared(self, monkeypatch):
        # Drives the REAL helper. getgrgid answers with the first entry carrying the
        # gid, and a second entry with that gid hands its members the same group, so
        # an empty first entry must not read as private.
        if os.name != "posix":
            pytest.skip("POSIX group databases")
        import grp
        import pwd

        me = pwd.getpwuid(os.geteuid())
        gid = me.pw_gid
        twin = _FakeGroup("twin", ["peer"])
        twin.gr_gid = gid
        monkeypatch.setattr(pwd, "getpwuid", lambda uid: me)
        monkeypatch.setattr(pwd, "getpwall", lambda: [me])
        monkeypatch.setattr(grp, "getgrgid", lambda g: _FakeGroup("first", []))
        monkeypatch.setattr(grp, "getgrall", lambda: [_FakeGroup("first", []), twin])
        verdict = platform_compat._group_shared_with_another_account(gid)
        assert verdict is not None and "shared with 1 other" in verdict, verdict

        # The same entry on ANOTHER gid shares nothing with this one.
        twin.gr_gid = gid + 1
        assert platform_compat._group_shared_with_another_account(gid) is None

        # A group database that will not enumerate cannot show there is no second
        # entry, so privacy is unproven and reads as shared.
        def _no_enumeration():
            raise OSError(errno.EIO, os.strerror(errno.EIO))

        monkeypatch.setattr(grp, "getgrall", _no_enumeration)
        unproven = platform_compat._group_shared_with_another_account(gid)
        assert unproven is not None and "will not enumerate" in unproven, unproven


class TestAclAdmitsAnotherAccount:
    """The ACL half: named entries holding an effective write, read from the
    POSIX access ACL."""

    def test_acl_entries_count_only_an_effective_write_by_another_account(
        self, monkeypatch, tmp_path
    ):
        if not hasattr(os, "getxattr"):
            pytest.skip("POSIX ACL xattrs")
        node = tmp_path / "acl-node"
        node.mkdir()
        me = os.geteuid()

        _serve_acl(monkeypatch, node, _acl_xattr((_ACL_USER, 0o7, me), (_ACL_USER, 0o7, 0)))
        assert (
            platform_compat._acl_admits_another_account(node, os.geteuid()) is None
        ), "this account and root"

        # The mask caps every named entry, so rwx under an r-x mask writes nothing.
        _serve_acl(monkeypatch, node, _acl_xattr((_ACL_USER, 0o7, 4242), mask=0o5))
        assert (
            platform_compat._acl_admits_another_account(node, os.geteuid()) is None
        ), "masked write"

        _serve_acl(monkeypatch, node, _acl_xattr((_ACL_USER, 0o7, 4242)))
        verdict = platform_compat._acl_admits_another_account(node, os.geteuid())
        assert verdict is not None and "uid 4242" in verdict, verdict

    def test_a_named_group_acl_entry_asks_that_groups_membership(self, monkeypatch, tmp_path):
        if not hasattr(os, "getxattr"):
            pytest.skip("POSIX ACL xattrs")
        node = tmp_path / "acl-node"
        node.mkdir()
        asked = []

        def _shared_only_for_4343(gid):
            asked.append(gid)
            return "group 'peers', shared with 2 other account(s)" if gid == 4343 else None

        monkeypatch.setattr(
            platform_compat, "_group_shared_with_another_account", _shared_only_for_4343
        )
        _serve_acl(monkeypatch, node, _acl_xattr((_ACL_GROUP, 0o7, 4343)))
        verdict = platform_compat._acl_admits_another_account(node, os.geteuid())
        assert verdict is not None and "shared with 2 other" in verdict, verdict

        _serve_acl(monkeypatch, node, _acl_xattr((_ACL_GROUP, 0o7, 4444)))
        assert (
            platform_compat._acl_admits_another_account(node, os.geteuid()) is None
        ), "a private named group"
        assert asked == [4343, 4444], asked

    def test_no_acl_leaves_the_mode_bits_as_the_test(self, monkeypatch, tmp_path):
        if not hasattr(os, "getxattr"):
            pytest.skip("POSIX ACL xattrs")
        node = tmp_path / "acl-node"
        node.mkdir()
        for code in (errno.ENODATA, errno.ENOTSUP):
            _serve_acl(monkeypatch, node, OSError(code, os.strerror(code)))
            assert platform_compat._acl_admits_another_account(node, os.geteuid()) is None, code
        monkeypatch.delattr(os, "getxattr")
        assert (
            platform_compat._acl_admits_another_account(node, os.geteuid()) is None
        ), "no ACL API on this host"

    def test_an_acl_that_cannot_be_read_or_parsed_fails_closed(self, monkeypatch, tmp_path):
        if not hasattr(os, "getxattr"):
            pytest.skip("POSIX ACL xattrs")
        node = tmp_path / "acl-node"
        node.mkdir()
        good = _acl_xattr()
        for value in (
            struct.pack("<I", 3) + good[4:],  # an unknown version
            good[:-3],  # a torn entry
            b"\x02\x00",  # shorter than the header
            OSError(errno.EACCES, os.strerror(errno.EACCES)),
        ):
            _serve_acl(monkeypatch, node, value)
            verdict = platform_compat._acl_admits_another_account(node, os.geteuid())
            assert verdict == "an ACL this host cannot read", (value, verdict)

    def test_a_real_acl_entry_is_read_from_the_filesystem(self, tmp_path):
        # The other ACL pins serve crafted bytes. This one asks the kernel, so the
        # parser is checked against the layout the host really writes.
        if not hasattr(os, "getxattr") or shutil.which("setfacl") is None:
            pytest.skip("needs setfacl and POSIX ACL xattrs")
        node = tmp_path / "acl-node"
        node.mkdir(mode=0o700)

        def _setfacl(entry: str) -> bool:
            done = subprocess.run(
                ["setfacl", "-m", entry, str(node)], capture_output=True, check=False
            )
            return done.returncode == 0

        # An entry for this account is trusted, so a parser that reads the real
        # layout answers None here rather than failing closed on it.
        if not _setfacl(f"u:{os.geteuid()}:rwx"):
            pytest.skip("this filesystem will not take a named ACL entry")
        assert platform_compat._acl_admits_another_account(node, os.geteuid()) is None
        peer = 65534 if os.geteuid() != 65534 else 65533
        if not _setfacl(f"u:{peer}:rwx"):
            pytest.skip("this namespace maps no other uid")
        verdict = platform_compat._acl_admits_another_account(node, os.geteuid())
        assert verdict is not None and f"uid {peer}" in verdict, verdict


def _must_not_run(*args, **kwargs):
    raise AssertionError("an earlier answer already settled the question")


@pytest.mark.skipif(os.name != "posix", reason="POSIX ids; neither caller asks this on Windows")
class TestGroupWriteAdmitsAnotherAccount:
    """The whole question: the access ACL first, then the membership."""

    def test_an_acl_answer_comes_first_and_settles_it(self, monkeypatch, tmp_path):
        asked = []

        def _acl(node, mine):
            asked.append((node, mine))
            return "an ACL entry for another account (uid 4242)"

        monkeypatch.setattr(platform_compat, "_acl_admits_another_account", _acl)
        monkeypatch.setattr(platform_compat, "_group_shared_with_another_account", _must_not_run)

        answer = platform_compat.group_write_admits_another_account(tmp_path, 4343)

        assert answer == "an ACL entry for another account (uid 4242)"
        assert asked == [(tmp_path, os.geteuid())], "the ACL is asked about this account"

    def test_with_no_acl_answer_the_membership_decides(self, monkeypatch, tmp_path):
        asked = []
        shared = "group 'peers', shared with 3 other account(s)"

        def _membership(gid):
            asked.append(gid)
            return shared

        monkeypatch.setattr(platform_compat, "_acl_admits_another_account", lambda node, mine: None)
        monkeypatch.setattr(platform_compat, "_group_shared_with_another_account", _membership)
        assert platform_compat.group_write_admits_another_account(tmp_path, 4343) == shared
        assert asked == [4343]

        monkeypatch.setattr(platform_compat, "_group_shared_with_another_account", lambda gid: None)
        assert platform_compat.group_write_admits_another_account(tmp_path, 4343) is None

    def test_without_an_acl_api_the_membership_decides(self, monkeypatch, tmp_path):
        """macOS and the BSDs bind no ACL read in ``os``, so there the mode bits
        are the test, as on a directory that carries no ACL."""
        monkeypatch.delattr(os, "getxattr", raising=False)
        shared = "group 'staff', shared with 1 other account(s)"
        monkeypatch.setattr(
            platform_compat, "_group_shared_with_another_account", lambda gid: shared
        )
        assert platform_compat.group_write_admits_another_account(tmp_path, 20) == shared

        monkeypatch.setattr(platform_compat, "_group_shared_with_another_account", lambda gid: None)
        assert platform_compat.group_write_admits_another_account(tmp_path, 20) is None

    @pytest.mark.parametrize(
        ("access", "default", "members", "expected"),
        [
            pytest.param(None, None, [], None, id="as-installed"),
            pytest.param([(_ACL_USER, 0o5, "peer")], None, [], None, id="read-only-entry"),
            pytest.param(None, [(_ACL_USER, 0o7, "peer")], [], None, id="default-acl-only"),
            pytest.param([(_ACL_USER, 0o7, "me")], None, [], None, id="entry-for-this-account"),
            pytest.param(
                [(_ACL_USER, 0o7, "peer")],
                None,
                [],
                "an ACL entry for another account",
                id="write-entry-for-another-account",
            ),
            pytest.param(
                [(_ACL_GROUP, 0o7, "peers")],
                None,
                [],
                "an ACL entry for group 'peers', shared with 1 other account(s)",
                id="write-for-a-shared-group",
            ),
            pytest.param(
                None, None, ["peer"], "shared with 1 other account(s)", id="peer-joins-the-group"
            ),
        ],
    )
    def test_a_user_private_group_under_each_layout(
        self, monkeypatch, tmp_path, access, default, members, expected
    ):
        """A group the databases show holding this account alone, as umask 002
        leaves every per-user install directory, under each ACL such a directory
        meets. Only a write another account holds counts, and the default ACL,
        which governs what is created inside, is never read."""
        if not hasattr(os, "getxattr"):
            pytest.skip("POSIX ACL xattrs")
        import grp
        import pwd

        me = pwd.getpwuid(os.geteuid())
        own = _FakeGroup(me.pw_name, members)
        own.gr_gid = me.pw_gid
        peers = _FakeGroup("peers", ["peer"])
        peers.gr_gid = me.pw_gid + 1
        groups = {own.gr_gid: own, peers.gr_gid: peers}
        ids = {"me": me.pw_uid, "peer": me.pw_uid + 1, "peers": peers.gr_gid}
        monkeypatch.setattr(grp, "getgrgid", lambda gid: groups[gid])
        monkeypatch.setattr(grp, "getgrall", lambda: list(groups.values()))
        monkeypatch.setattr(pwd, "getpwuid", lambda uid: me)
        monkeypatch.setattr(pwd, "getpwall", lambda: [me])
        attributes = {
            name: _acl_xattr(*[(tag, perm, ids[who]) for tag, perm, who in named], group_obj=0o7)
            for name, named in (
                ("system.posix_acl_access", access),
                ("system.posix_acl_default", default),
            )
            if named is not None
        }

        def _getxattr(path, attribute, *args, **kwargs):
            if attribute in attributes:
                return attributes[attribute]
            raise OSError(errno.ENODATA, os.strerror(errno.ENODATA))

        monkeypatch.setattr(os, "getxattr", _getxattr)

        answer = platform_compat.group_write_admits_another_account(tmp_path, own.gr_gid)

        if expected is None:
            assert answer is None, answer
        else:
            assert answer is not None and expected in answer, answer
