"""Grant matching shared by worker exclusions and mate capability previews."""

from __future__ import annotations

import pytest

from kiro_crew.agent_sdk.mcp_refs import entry_grants, entry_reaches


@pytest.mark.parametrize(
    "entry, expected",
    [
        ("@notes", True),
        ("@notes/", True),
        ("@notes/*", True),
        ("@notes/write_*", True),
        ("@notes/write_not?", True),
        ("@notes/write_[n]ote", True),
        ("@NOTES/WRITE_*", True),
        ("*", True),
        ("write_*", True),
        ("write_not?", True),
        ("@notes/read_*", False),
        ("@other/write_*", False),
        ("@notes/writ?", False),
        ("@", False),
        ("@/write_note", False),
        ("", False),
    ],
)
def test_entry_reaches_tool_part_globs(entry, expected):
    assert entry_reaches(entry, "@notes/write_note") is expected


@pytest.mark.parametrize("entry", ["@notes", "@notes/", "@notes/*", "*"])
@pytest.mark.parametrize("ref", ["@notes", "@notes/", "@notes/*"])
def test_entry_reaches_whole_server_selectors(entry, ref):
    assert entry_reaches(entry, ref)


_SEND = "@kirocrew-dashboard/session_send"


@pytest.mark.parametrize(
    "entry",
    [
        _SEND,
        "*",
        "@kirocrew-dashboard",
        "@kirocrew-dashboard/*",
        "@kirocrew-dashboard/session_*",
        "@kirocrew-dashboard/*_send",
        "@kirocrew-dashboard/session_sen?",
    ],
)
def test_entry_grants_only_kiro_cli_grant_shapes(entry):
    assert entry_grants(entry, _SEND)


@pytest.mark.parametrize(
    "entry",
    [
        "session_send",
        "*_send",
        "@*",
        "@*/session_send",
        "@kirocrew-*/session_send",
        "@kirocrew-dashboar?",
        "@KIROCREW-DASHBOARD/SESSION_SEND",
        "@kirocrew-dashboard/SESSION_*",
        "@kirocrew-dashboard/",
        "@kirocrew-dashboard/session_create",
        "@kirocrew-dashboard/session_[s]end",
        "@other/session_send",
        "@",
        "",
    ],
)
def test_entry_grants_refuses_what_entry_reaches_over_matches(entry):
    assert not entry_grants(entry, _SEND)


@pytest.mark.parametrize("entry", ["session_send", "*_send", "@*"])
def test_entry_reaches_keeps_its_over_match(entry):
    assert entry_reaches(entry, _SEND)


@pytest.mark.parametrize("ref", ["@kirocrew-dashboard", "@kirocrew-dashboard/*", "@srv/a?", ""])
def test_entry_grants_never_grants_a_pattern_ref(ref):
    assert not entry_grants("*", ref)


def test_entry_grants_bare_builtin_needs_exact_or_star():
    assert entry_grants("fs_read", "fs_read")
    assert entry_grants("*", "fs_read")
    assert not entry_grants("fs_*", "fs_read")
    assert not entry_grants("@builtin", "fs_read")


_INDEX_ENTRIES = [
    "@notes",
    "@notes/",
    "@notes/*",
    "@notes/write_*",
    "@notes/write_not?",
    "@notes/write_[n]ote",
    "@notes/write_note",
    "@NOTES/WRITE_NOTE",
    "@Notes/Read",
    "@straße/x",
    "@STRASSE/y",
    "@other/write_note",
    "@*",
    "@*/write_note",
    "@kirocrew-*",
    "@kirocrew-?ore/run",
    "@[ab]c/t",
    "*",
    "write_*",
    "write_note",
    "WRITE_NOTE",
    "fs_read",
    "fs_*",
    "x/y",
    "a[b",
    "@",
    "@/write_note",
    "",
]
_INDEX_REFS = [
    *_INDEX_ENTRIES,
    "@notes/read",
    "@notes/READ",
    "@notes/write_note/sub",
    "@strasse/x",
    "@STRASSE/x",
    "@kirocrew-core",
    "@kirocrew-core/run",
    "@ac/t",
    "@other",
    "fs_READ",
    "x/",
    "y",
    "@x/y",
    "@q/fs_read",
    "a[b",
]


@pytest.mark.parametrize("ref", _INDEX_REFS)
def test_reach_index_answers_what_trying_every_entry_answers(ref):
    from kiro_crew.agent_sdk.mcp_refs import ReachIndex

    assert ReachIndex(_INDEX_ENTRIES).reaches(ref) is any(
        entry_reaches(entry, ref) for entry in _INDEX_ENTRIES
    )


def test_reach_index_agrees_on_every_subset_and_after_adds():
    import random

    from kiro_crew.agent_sdk.mcp_refs import ReachIndex

    rng = random.Random(37625)
    for _ in range(300):
        entries = rng.sample(_INDEX_ENTRIES, rng.randint(0, len(_INDEX_ENTRIES)))
        index = ReachIndex(entries[: len(entries) // 2])
        for entry in entries[len(entries) // 2 :]:
            index.add(entry)
        for ref in _INDEX_REFS:
            assert index.reaches(ref) is any(entry_reaches(e, ref) for e in entries), (
                entries,
                ref,
            )
