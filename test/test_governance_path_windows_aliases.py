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


# The tool expands ``$VAR`` and ``~`` only, so it writes ``C:\ws\%DECOY%\..\k`` as
# ``C:\ws\k``; this host's ``ntpath.expandvars`` reads it as ``C:\ws\a\k``.
DECOY_ITEM = r"C:\ws\%KC_DECOY%\..\k"


@pytest.mark.usefixtures("windows")
class TestPercentVariableKeptLiteralByTheTool:
    @pytest.fixture(autouse=True)
    def _decoy(self, _floor_monkeypatch):
        _floor_monkeypatch.setenv("KC_DECOY", r"a\b")

    def test_a_deny_meets_the_file_the_tool_writes(self):
        assert matching._match_denied_path(DECOY_ITEM, r"C:\ws\k")

    def test_a_deny_still_meets_the_expanded_reading(self):
        assert matching._match_denied_path(DECOY_ITEM, r"C:\ws\a\k")

    def test_an_allow_must_cover_both_readings(self):
        assert not matching._path_allowed(DECOY_ITEM, [r"C:\ws\a\**"])
        assert not matching._path_allowed(DECOY_ITEM, [r"C:\ws\k"])
        assert matching._path_allowed(DECOY_ITEM, [r"C:\ws\**"])

    def test_separate_allow_entries_may_cover_each_reading(self):
        assert matching._path_allowed(DECOY_ITEM, [r"C:\ws\k", r"C:\ws\a\k"])
        ceiling = parse_policy(
            _policy_body(
                filesystem={"write": {"mode": "allow", "allow": [r"C:\ws\k", r"C:\ws\a\k"]}}
            )
        )
        assert resolve(ceiling, None, "filesystem.write", DECOY_ITEM).permitted
        ceiling = parse_policy(
            _policy_body(filesystem={"write": {"mode": "allow", "allow": [r"C:\ws\a\**"]}})
        )
        assert not resolve(ceiling, None, "filesystem.write", DECOY_ITEM).permitted

    def test_the_plain_matcher_still_meets_the_host_reading(self):
        # Policy translation tests deny entries with the plain matcher, so it must
        # meet every pattern it met before (either spelling, never both).
        assert matching._MATCHERS["path"](DECOY_ITEM, r"C:\ws\a\**")
        assert matching._MATCHERS["path"](DECOY_ITEM, r"C:\ws\k")

    def test_a_policy_deny_refuses_the_spelling(self):
        ceiling = parse_policy(
            _policy_body(filesystem={"write": {"mode": "deny", "deny": [r"C:\ws\k"]}})
        )
        assert not resolve(ceiling, None, "filesystem.write", DECOY_ITEM).permitted


@pytest.mark.usefixtures("windows")
def test_an_intersection_drops_a_percent_entry_one_set_covers_on_one_reading_only(
    _floor_monkeypatch,
):
    # The tool writes ``C:\ws\k``; this host reads ``C:\ws\a\k``. A set whose glob
    # covers only the host reading must not keep the entry in the intersection.
    from kiro_crew.platform import enterprise_policy_translate as translate

    _floor_monkeypatch.setenv("KC_DECOY", r"a\b")
    row = translate.Managed(("filesystem", "write"), matcher="path")
    sets = [[DECOY_ITEM], [r"C:\ws\a\**"]]
    assert translate._intersect(sets, "filesystem.write", row) == []
    sets = [[DECOY_ITEM], [r"C:\ws\**"]]
    assert translate._intersect(sets, "filesystem.write", row) == [DECOY_ITEM]
