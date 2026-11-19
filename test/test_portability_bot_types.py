"""The instance import's per-Bot vocabulary (``portability_bot_types``)."""

from __future__ import annotations

import inspect
import os

import pytest

from kiro_crew import mate_config
from kiro_crew.portability_bot_types import (
    NOT_WHOLE_STATUSES,
    WHOLE_STATUSES,
    BoundedReadError,
    PartOutcome,
    carried_member_file,
    is_minimal_crewmate_file,
    is_whole,
    raw_entry_identity,
    raw_entry_slug,
    read_fd_at_most,
    safe_path_component,
)


@pytest.mark.parametrize(
    "rel",
    [
        "dashboard/instance.json",
        "dashboard/history.json",
        "dashboard/versions/1.json",
        "dashboard/versions/12.json",
        "briefing.md",
        "welcome_claimed.json",
    ],
)
def test_carried_member_files_are_admitted(rel: str) -> None:
    assert carried_member_file(rel.split("/"))


@pytest.mark.parametrize(
    "rel",
    [
        "activity.jsonl",
        "dm.json",
        "welcome_owed.json",
        "create_request.json",
        "dashboard/.lock",
        "dashboard/preview.json",
        "dashboard/versions/x.json",
        "dashboard/versions/1.json.tmp",
        "dashboard/versions/-1.json",
        "dashboard/versions/1/2.json",
        "versions/1.json",
        "dashboard",
        "",
    ],
)
def test_host_state_and_history_are_refused(rel: str) -> None:
    assert not carried_member_file([p for p in rel.split("/") if p] if rel else [])


def test_minimal_crewmate_file_matches_the_writer_shape() -> None:
    assert is_minimal_crewmate_file(mate_config.minimal_mate_spec("Ana"), "Ana")


@pytest.mark.parametrize(
    "key,value",
    [
        ("tools", ["shell"]),
        ("allowedTools", ["shell"]),
        ("mcpServers", {"x": {"command": "x"}}),
        ("skills", ["s"]),
        ("resources", ["file://x"]),
        ("prompt", "be helpful"),
        ("autoApprove", ["shell"]),
    ],
)
def test_any_added_ability_is_not_minimal(key: str, value: object) -> None:
    doc = mate_config.minimal_mate_spec("Ana")
    doc[key] = value
    assert not is_minimal_crewmate_file(doc, "Ana")


@pytest.mark.parametrize(
    "key,value",
    [
        ("prompt", []),
        ("prompt", {}),
        ("mcpServers", []),
        ("mcpServers", ""),
        ("tools", {}),
        ("tools", ""),
        ("allowedTools", ""),
        ("skills", {}),
        ("resources", ""),
        ("excludedTools", {}),
        ("excludedTools", ""),
    ],
)
def test_an_empty_value_of_another_type_is_not_minimal(key: str, value: object) -> None:
    # Empty of the wrong type is not the empty addition the loader reads for that key.
    assert not is_minimal_crewmate_file({"name": "Ana", key: value}, "Ana")
    doc = mate_config.minimal_mate_spec("Ana")
    doc[key] = value
    assert not is_minimal_crewmate_file(doc, "Ana")


def test_each_addition_may_be_left_out_or_hold_its_own_empty_value() -> None:
    assert is_minimal_crewmate_file({"name": "Ana"}, "Ana")
    assert is_minimal_crewmate_file({"name": "Ana", "prompt": "", "excludedTools": []}, "Ana")
    assert is_minimal_crewmate_file({"name": "Ana", "mcpServers": {}, "tools": []}, "Ana")


def test_minimal_file_for_another_name_or_a_non_object_is_not_minimal() -> None:
    assert not is_minimal_crewmate_file(mate_config.minimal_mate_spec("Ana"), "Bo")
    assert not is_minimal_crewmate_file([], "Ana")
    assert not is_minimal_crewmate_file(None, "Ana")


def test_whole_and_not_whole_statuses_are_disjoint() -> None:
    assert not (WHOLE_STATUSES & NOT_WHOLE_STATUSES)


def test_is_whole_follows_the_status_sets() -> None:
    ok = PartOutcome("Ana", "binding", "rebuilt")
    bad = PartOutcome("Ana", "dashboard", "page_not_rebuilt", "digest_mismatch")
    assert is_whole([ok])
    assert is_whole([])
    assert not is_whole([ok, bad])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"part": "elbow", "status": "added"},
        {"part": "roster", "status": "maybe"},
        {"part": "roster", "status": "skipped", "reason": "because"},
    ],
)
def test_outcomes_outside_the_vocabulary_are_refused(kwargs: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        PartOutcome("Ana", **kwargs)


@pytest.mark.parametrize(
    ("name", "entry", "slug"),
    [
        ("Atlas", {"member_id": "atlas-1"}, "atlas-1"),
        ("Atlas", {"member_id": ""}, "atlas"),
        ("Atlas", {}, "atlas"),
        ("Atlas", None, "atlas"),
        ("Atlas", {"member_id": 7}, "atlas"),
    ],
)
def test_raw_entry_slug_is_the_member_id_else_the_name_slug(name, entry, slug) -> None:
    assert raw_entry_slug(name, entry) == slug


def test_raw_entry_slug_refuses_an_invalid_member_id() -> None:
    with pytest.raises(ValueError):
        raw_entry_slug("Atlas", {"member_id": "../x"})


@pytest.mark.parametrize(
    ("name", "safe"),
    [
        ("Atlas", True),
        ("a b", True),
        ("", False),
        (".hidden", False),
        ("a/b", False),
        ("a\\b", False),
        ("a\x00b", False),
        (None, False),
        ("a:b", False),
        ("C:", False),
        *((f"a{c}b", False) for c in '<>"|?*'),
        ("a\x01b", False),
        ("a\x1fb", False),
        ("a\tb", False),
        ("CON", False),
        ("con", False),
        ("Nul.json", False),
        ("aux.tar.gz", False),
        ("PRN ", False),
        ("COM1", False),
        ("com9.txt", False),
        ("LPT1", False),
        ("lpt9", False),
        ("COM0", True),
        ("COM10", True),
        ("console", True),
        ("Connor", True),
        ("AUXILIARY", True),
    ],
)
def test_safe_path_component(name, safe) -> None:
    assert safe_path_component(name) is safe


@pytest.mark.parametrize(
    ("entry", "identity"),
    [
        ({"member_id": "atlas", "memory_store": "atlas-mem"}, ("atlas", "atlas-mem")),
        # A version-1 Bot: the loader's defaults stand in for the absent keys.
        ({"kiro_agent": "Old"}, ("", "default")),
        ({"member_id": "", "memory_store": "shared"}, ("", "shared")),
        ({"member_id": 7}, None),
        ({"memory_store": None}, None),
        ("not an entry", None),
    ],
)
def test_raw_entry_identity_reads_like_the_loader(entry, identity):
    assert raw_entry_identity(entry) == identity


def _fd_with(tmp_path, data: bytes) -> int:
    path = tmp_path / "f.bin"
    path.write_bytes(data)
    return os.open(path, os.O_RDONLY)


@pytest.mark.parametrize("size", [0, 1, 9, 10])
def test_read_fd_at_most_returns_a_file_within_the_limit(tmp_path, size):
    fd = _fd_with(tmp_path, b"x" * size)
    try:
        assert read_fd_at_most(fd, 10) == b"x" * size
    finally:
        os.close(fd)


def test_read_fd_at_most_refuses_one_byte_past_the_limit_and_reads_no_further(tmp_path):
    fd = _fd_with(tmp_path, b"x" * 1000)
    try:
        with pytest.raises(BoundedReadError):
            read_fd_at_most(fd, 10)
        assert os.lseek(fd, 0, os.SEEK_CUR) == 11
    finally:
        os.close(fd)


def test_read_fd_at_most_folds_a_read_error_into_its_one_error(monkeypatch):
    from kiro_crew import portability_bot_types

    def boom(_fd, _n):
        raise OSError("EIO")

    monkeypatch.setattr(portability_bot_types.os, "read", boom)
    with pytest.raises(BoundedReadError) as caught:
        read_fd_at_most(3, 10)
    assert isinstance(caught.value.__cause__, OSError)
    assert isinstance(caught.value, ValueError)


def test_every_bounded_reader_uses_the_one_helper(tmp_path):
    """The export, the store installer and the claim reader keep their own failure shape."""
    from kiro_crew import portability_bot_bindings, portability_bot_export, portability_bot_stores

    for fn in (
        portability_bot_export._read_fd_bounded,
        portability_bot_stores._read_staged,
        portability_bot_bindings._read_claim,
    ):
        source = inspect.getsource(fn)
        assert "read_fd_at_most(" in source and "os.read(" not in source, fn.__name__

    fd = _fd_with(tmp_path, b"y" * 20)
    try:
        os.lseek(fd, 5, os.SEEK_SET)
        assert portability_bot_export._read_fd_bounded(fd, 10) is None
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        assert portability_bot_export._read_fd_bounded(fd, 20) == b"y" * 20
    finally:
        os.close(fd)
    staged = tmp_path / "f.bin"
    with pytest.raises(BoundedReadError):
        portability_bot_stores._read_staged(staged, 10)
    assert portability_bot_stores._read_staged(staged, 20) == b"y" * 20


def test_the_export_checks_a_pinned_package_with_the_artifact_store_rule(monkeypatch):
    from kiro_crew import artifacts, portability_bot_export

    assert portability_bot_export._valid_slug("demo")
    assert not portability_bot_export._valid_slug("demo\n")
    assert not portability_bot_export._valid_slug(7)
    monkeypatch.setattr(artifacts, "slug_is_well_formed", lambda _slug: False)
    assert not portability_bot_export._valid_slug("demo")
    assert "_SLUG_RE" not in inspect.getsource(portability_bot_export)


def test_the_store_installer_has_no_unused_refusal_type():
    from kiro_crew import portability_bot_stores

    assert not hasattr(portability_bot_stores, "_StoreRefused")


def test_the_reserved_default_member_is_the_members_rosters() -> None:
    from kiro_crew.dashboard.handlers import members_roster
    from kiro_crew.portability_bot_types import RESERVED_DEFAULT_MEMBER

    assert RESERVED_DEFAULT_MEMBER == members_roster.RESERVED_DEFAULT_MEMBER


def test_the_carried_single_files_are_the_member_space_and_welcome_names() -> None:
    from kiro_crew import members, portability_bot_stores
    from kiro_crew.dashboard.mate_welcome import GREETING_MARKER_FILENAME
    from kiro_crew.portability_bot_types import carried_member_file

    assert portability_bot_stores.BRIEFING_FILES == (
        members.BRIEFING_FILE_NAME,
        GREETING_MARKER_FILENAME,
    )
    for name in portability_bot_stores.BRIEFING_FILES:
        assert carried_member_file((name,)), name
