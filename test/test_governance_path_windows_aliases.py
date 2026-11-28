"""A governance ``path`` rule meets a Windows namespace alias as its plain spelling.

On Windows ``\\\\?\\C:\\x``, ``\\\\.\\C:\\x``, ``//?/C:/x`` and ``\\??\\C:\\x`` open the
same file as ``C:\\x``, and so does ``C:\\x::$DATA``. The sensitive-path keystone folds
those spellings (``security.paths_resolver._fold_windows_alias``); the governance
matcher's ``_norm_item`` folds them with the same function, so a policy deny naming
``C:\\x`` is not dodged by a prefix the OS strips. Off Windows the same characters
are ordinary file-name bytes and nothing is folded.

The Windows half runs on every host by reading paths as a Windows gateway does, the
way ``test_sensitive_path_windows_aliases`` drives the keystone: the matcher's ``os``
is swapped (through the ``governance`` facade, which every owner's ``os`` follows) for
one whose ``path`` is ``ntpath``, and ``_ON_WINDOWS`` is set.
"""

from __future__ import annotations

import ntpath
import os
import posixpath

import pytest
from governance_policy_helpers import _policy_body

from kiro_crew.platform import governance
from kiro_crew.platform.governance import parse_policy, resolve
from kiro_crew.platform.governance_parts import matching
from kiro_crew.security import paths_resolver

SECRET = r"C:\data\secret\k"
PREFIXES = ("\\\\?\\", "\\\\.\\", "\\??\\")


def _aliases(path: str) -> list[str]:
    return [prefix + path for prefix in PREFIXES] + ["//?/" + path.replace("\\", "/")]


class _OsWithPath:
    """Governance's private view of ``os``, with one ``path`` flavour swapped in."""

    def __init__(self, flavour) -> None:
        self.path = flavour

    def __getattr__(self, name: str) -> object:
        return getattr(os, name)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(governance, "os", _OsWithPath(ntpath))
    monkeypatch.setattr(paths_resolver, "_ON_WINDOWS", True)


@pytest.mark.usefixtures("windows")
class TestOnWindows:
    @pytest.mark.parametrize("item", _aliases(SECRET))
    def test_a_namespace_alias_meets_the_deny_its_plain_spelling_meets(self, item):
        assert matching._match_path(SECRET, r"C:\data\secret\*")
        assert matching._match_path(item, r"C:\data\secret\*")
        assert matching._match_path(item, SECRET)

    def test_the_default_stream_meets_a_rule_naming_the_file(self):
        assert matching._match_path(SECRET + "::$DATA", SECRET)
        assert matching._match_path(SECRET + "::$data", SECRET)

    @pytest.mark.parametrize("item", _aliases(r"C:\ws\..\secret.txt"))
    def test_a_traversal_behind_an_alias_still_cannot_satisfy_an_allow_prefix(self, item):
        assert not matching._match_path(item, r"C:\ws\**")
        assert matching._match_path(item, r"C:\secret.txt")

    @pytest.mark.parametrize(
        "item",
        [
            r"\\?\UNC\srv\share\data\secret\k",  # a share, not the local drive
            r"\\?\Volume{0f5b1c2e-0000-0000-0000-000000000000}\data\secret\k",
            SECRET + ":alt",  # a named stream is another stream
        ],
    )
    def test_other_namespaces_and_named_streams_stay_as_written(self, item):
        assert not matching._match_path(item, SECRET)

    @pytest.mark.parametrize(
        "item",
        [
            r"\\localhost\C$\data\secret\k",
            r"\\127.0.0.1\c$\data\secret\k",
            r"\\?\UNC\localhost\C$\data\secret\k",
            r"\??\UNC\localhost\C$\data\secret\k",
        ],
    )
    def test_a_loopback_admin_share_meets_the_deny_its_plain_spelling_meets(self, item):
        assert matching._match_path(item, r"C:\data\secret\*")
        assert matching._match_path(item, SECRET)

    def test_a_drive_share_on_another_host_stays_as_written(self):
        assert not matching._match_path(r"\\fileserver\C$\data\secret\k", SECRET)

    def test_a_policy_deny_refuses_every_alias(self):
        ceiling = parse_policy(
            _policy_body(filesystem={"read": {"mode": "deny", "deny": [r"C:\data\secret\**"]}})
        )
        for item in [SECRET, *_aliases(SECRET), SECRET + "::$DATA"]:
            assert not resolve(ceiling, None, "filesystem.read", item).permitted, item
        assert resolve(ceiling, None, "filesystem.read", r"C:\data\public\k").permitted

    def test_a_deny_written_as_a_loopback_share_still_refuses_that_spelling(self):
        share = r"\\localhost\C$\data\private"
        ceiling = parse_policy(
            _policy_body(filesystem={"read": {"mode": "deny", "deny": [share + r"\**"]}})
        )
        assert not resolve(ceiling, None, "filesystem.read", share + r"\report.txt").permitted
        assert matching._match_path(share + r"\report.txt", share + r"\**")
        assert resolve(ceiling, None, "filesystem.read", r"C:\data\public\k").permitted

    def test_a_deny_written_as_a_loopback_share_refuses_the_drive_spelling(self, monkeypatch):
        # A ``/``-written rule meets a ``\`` item through the Windows separator glob.
        monkeypatch.setattr(governance, "_WINDOWS_PATHS", True)
        ceiling = parse_policy(
            _policy_body(
                filesystem={"read": {"mode": "deny", "deny": [r"//localhost/c$/data/private/**"]}}
            )
        )
        assert not resolve(ceiling, None, "filesystem.read", r"C:\data\private\k").permitted
        assert resolve(ceiling, None, "filesystem.read", r"C:\data\public\k").permitted

    @pytest.mark.parametrize("item", [r"\\localhost\C$\ws\a.txt", r"C:\ws\a.txt"])
    def test_an_allow_written_as_a_loopback_share_still_admits(self, item):
        ceiling = parse_policy(
            _policy_body(
                filesystem={"write": {"mode": "allow", "allow": [r"\\localhost\C$\ws\**"]}}
            )
        )
        assert resolve(ceiling, None, "filesystem.write", item).permitted
        assert not resolve(ceiling, None, "filesystem.write", r"C:\elsewhere\a.txt").permitted

    @pytest.mark.parametrize("deny", [r"\\*\C$\private\**", r"\\localhost\[CD]$\private\**"])
    def test_a_share_deny_the_fold_cannot_rewrite_still_refuses(self, deny):
        ceiling = parse_policy(_policy_body(filesystem={"read": {"mode": "deny", "deny": [deny]}}))
        item = r"\\localhost\C$\private\report.txt"
        assert not resolve(ceiling, None, "filesystem.read", item).permitted
        assert resolve(ceiling, None, "filesystem.read", r"\\localhost\C$\public\k").permitted

    @pytest.mark.parametrize(
        "deny", [r"\\?\UNC\localhost\C$\private\**", r"//?/UNC/localhost/C$/private/**"]
    )
    def test_a_folded_deny_still_meets_what_its_glob_met(self, deny, monkeypatch):
        # ``?`` is a glob character, so the rule as written also meets a share on a
        # one-letter host that no fold rewrites; the folded rule must not lose that.
        monkeypatch.setattr(governance, "_WINDOWS_PATHS", True)
        ceiling = parse_policy(_policy_body(filesystem={"read": {"mode": "deny", "deny": [deny]}}))
        for item in (r"\\x\UNC\localhost\C$\private\report.txt", r"C:\private\report.txt"):
            assert not resolve(ceiling, None, "filesystem.read", item).permitted, item
        assert resolve(ceiling, None, "filesystem.read", r"C:\public\k").permitted

    @pytest.mark.parametrize("prefix", ["\\\\.\\UNC\\", "\\\\?\\UNC\\", "\\??\\UNC\\"])
    def test_a_traversal_above_a_device_share_does_not_meet_a_drive_allow(self, prefix):
        # Win32 roots a device form above the share, so ``..`` leaves ``localhost\C$``
        # for another host: the path is not ``C:\work\x\f`` and must not meet its allow.
        ceiling = parse_policy(
            _policy_body(filesystem={"write": {"mode": "allow", "allow": [r"C:\work\**"]}})
        )
        item = prefix + r"localhost\C$\..\..\work\x\f"
        assert paths_resolver._fold_loopback_admin_share(item) == item
        assert not resolve(ceiling, None, "filesystem.write", item).permitted
        plain = prefix + r"localhost\C$\work\x\f"
        assert resolve(ceiling, None, "filesystem.write", plain).permitted
        # A plain share clamps ``..`` at its root, as ``C:\..`` does: it still folds.
        assert resolve(ceiling, None, "filesystem.write", r"\\localhost\C$\x\..\work\f").permitted

    def test_a_share_allow_the_fold_cannot_rewrite_still_admits(self):
        allow = r"\\*\C$\ws\**"
        assert matching._match_path(r"\\localhost\C$\ws\a.txt", allow)
        assert not matching._match_path(r"\\localhost\C$\ws\..\secret.txt", allow)

    def test_a_traversal_behind_a_loopback_share_cannot_satisfy_its_allow(self):
        allow = r"\\localhost\C$\ws\**"
        assert not matching._match_path(r"\\localhost\C$\ws\..\secret.txt", allow)
        assert not matching._match_path(r"\\fileserver\C$\ws\a.txt", allow)


class _NtPathAt:
    """``ntpath`` with ``abspath`` anchored at a fixed Windows CWD."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd

    def abspath(self, path: str) -> str:
        return ntpath.normpath(ntpath.join(self.cwd, path))

    def __getattr__(self, name: str) -> object:
        return getattr(ntpath, name)


@pytest.mark.parametrize(
    "item",
    [
        r"\??\UNC\localhost\C$\data\secret\k",
        r"\\?\UNC\localhost\C$\data\secret\k",
        r"\\localhost\C$\data\secret\k",
    ],
)
def test_a_share_item_folds_before_win32_abspath_reroots_it(item, _floor_monkeypatch):
    # Win32 ``abspath`` (GetFullPathNameW) reads a leading ``\??\`` as root-relative
    # and returns ``C:\??\UNC\...``; ``ntpath.join`` onto a ``C:`` CWD does the same.
    _floor_monkeypatch.setattr(governance, "os", _OsWithPath(_NtPathAt(r"C:\work")))
    _floor_monkeypatch.setattr(paths_resolver, "_ON_WINDOWS", True)
    ceiling = parse_policy(
        _policy_body(filesystem={"read": {"mode": "deny", "deny": [r"C:\data\secret\**"]}})
    )
    assert not resolve(ceiling, None, "filesystem.read", item).permitted
    assert resolve(ceiling, None, "filesystem.read", r"C:\data\public\k").permitted


#: A relative and a root-relative read of ``C:\work\private\report.txt``.
RELATIVE_PRIVATE = [r"private\report.txt", r"\work\private\report.txt"]


class TestUnderALoopbackShareCwd:
    """A gateway whose CWD is ``\\\\localhost\\C$\\work``: a relative item only
    becomes a share path once it is anchored, and meets a rule as its absolute
    spelling does."""

    @pytest.fixture(autouse=True)
    def _share_cwd(self, _floor_monkeypatch):
        share_os = _OsWithPath(_NtPathAt(r"\\localhost\C$\work"))
        _floor_monkeypatch.setattr(governance, "os", share_os)
        _floor_monkeypatch.setattr(paths_resolver, "_ON_WINDOWS", True)

    @pytest.mark.parametrize(
        "deny",
        [r"\\localhost\C$\work\private\**", r"C:\work\private\**", r"\\*\C$\work\private\**"],
    )
    @pytest.mark.parametrize("item", RELATIVE_PRIVATE)
    def test_a_relative_read_still_meets_the_deny(self, deny, item):
        ceiling = parse_policy(_policy_body(filesystem={"read": {"mode": "deny", "deny": [deny]}}))
        assert not resolve(ceiling, None, "filesystem.read", item).permitted
        assert resolve(ceiling, None, "filesystem.read", r"public\a.txt").permitted

    @pytest.mark.parametrize("allow", [r"\\localhost\C$\work\**", r"C:\work\**", r"\\*\C$\work\**"])
    @pytest.mark.parametrize("item", RELATIVE_PRIVATE)
    def test_a_relative_write_still_meets_the_allow(self, allow, item):
        ceiling = parse_policy(
            _policy_body(filesystem={"write": {"mode": "allow", "allow": [allow]}})
        )
        assert resolve(ceiling, None, "filesystem.write", item).permitted
        assert not resolve(ceiling, None, "filesystem.write", r"..\elsewhere\a.txt").permitted


def test_off_windows_a_prefix_is_part_of_the_file_name(monkeypatch):
    monkeypatch.setattr(governance, "os", _OsWithPath(posixpath))
    monkeypatch.setattr(paths_resolver, "_ON_WINDOWS", False)
    item = "\\\\?\\" + SECRET
    assert matching._norm_item(item).endswith(item)
    assert not matching._match_path(item, SECRET)
