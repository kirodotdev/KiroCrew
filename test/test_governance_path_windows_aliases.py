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

    def test_a_policy_deny_refuses_every_alias(self):
        ceiling = parse_policy(
            _policy_body(filesystem={"read": {"mode": "deny", "deny": [r"C:\data\secret\**"]}})
        )
        for item in [SECRET, *_aliases(SECRET), SECRET + "::$DATA"]:
            assert not resolve(ceiling, None, "filesystem.read", item).permitted, item
        assert resolve(ceiling, None, "filesystem.read", r"C:\data\public\k").permitted


def test_off_windows_a_prefix_is_part_of_the_file_name(monkeypatch):
    monkeypatch.setattr(governance, "os", _OsWithPath(posixpath))
    monkeypatch.setattr(paths_resolver, "_ON_WINDOWS", False)
    item = "\\\\?\\" + SECRET
    assert matching._norm_item(item).endswith(item)
    assert not matching._match_path(item, SECRET)
