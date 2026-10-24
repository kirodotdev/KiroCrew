"""Crewmate records: where they live, how they are fenced, and their one-time move.

A crewmate's permanent rules, goals and DM binding live in the gateway-only
``member-records/`` leaf. These tests pin that the record paths resolve there, that
the leaf is fenced on both planes (the OS sandbox mask and the file-tool floor), and
that records an older build wrote under ``trust/`` are found after the upgrade: moved
once, never lost, never promoted again after the move is sealed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew import member_goals, member_records, members, sandbox, sandbox_tiers, security
from kiro_crew.config.paths import data_home

_SLUG = "atlas"
_MEMBER = "Atlas"


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = (tmp_path / "crew").resolve()
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.delenv(member_records._IN_SANDBOX_MARKER, raising=False)
    assert data_home().resolve() == home
    return home


def _write_all_three() -> dict[str, bytes]:
    """Write one record of each kind with the gateway's writers; return kind -> bytes."""
    members.write_member_rules(_SLUG, member=_MEMBER, text="never push to main")
    member_goals.set_goal(_SLUG, _MEMBER, "ship the release", by="user")
    members.write_dm_binding(_SLUG, member=_MEMBER, slot_key=members.member_slot_key(_SLUG, ""))
    return {
        path.parent.name: path.read_bytes()
        for path in (
            members.member_rules_path(_SLUG),
            member_goals.member_goals_path(_SLUG),
            members.dm_binding_path(_SLUG),
        )
    }


def _as_old_layout(home: Path, contents: dict[str, bytes]) -> None:
    """Put *contents* where an older build kept them, as if this build never ran."""
    root = home / member_records.MEMBER_RECORDS_LEAF
    for kind, data in contents.items():
        old = home / "trust" / kind
        old.mkdir(parents=True, exist_ok=True)
        (old / f"{_SLUG}.json").write_bytes(data)
        (root / kind / f"{_SLUG}.json").unlink()
    (root / member_records.MIGRATION_MARKER).unlink()


def _reads() -> tuple[str, list[str], dict | None]:
    rules = members.read_member_rules(_SLUG, _MEMBER)
    goals = [g["text"] for g in member_goals.read_goals(_SLUG, _MEMBER)["goals"]]
    return rules, goals, members.read_dm_binding(_SLUG)


# ── where they live and how they are fenced ──


def test_every_record_path_is_in_the_gateway_only_leaf(home) -> None:
    root = home / "member-records"
    for path in (
        members.member_rules_path(_SLUG),
        member_goals.member_goals_path(_SLUG),
        members.dm_binding_path(_SLUG),
    ):
        assert root in path.parents, path
        assert home / "trust" not in path.parents, path


def test_the_file_tools_refuse_every_record(home) -> None:
    _write_all_three()
    for path in (
        members.member_rules_path(_SLUG),
        member_goals.member_goals_path(_SLUG),
        members.dm_binding_path(_SLUG),
    ):
        assert home / "member-records" in path.parents, path
        assert security.is_sensitive_path(str(path)), path
        assert security.is_sensitive_write_path(str(path)), path


def test_the_leaf_is_masked_precreated_and_never_aliased() -> None:
    assert member_records.MEMBER_RECORDS_LEAF in sandbox._CREW_HIDDEN_LEAVES
    assert member_records.MEMBER_RECORDS_LEAF in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
    assert member_records.MEMBER_RECORDS_LEAF in sandbox._CREW_NO_ALIAS_LEAVES
    assert member_records.MEMBER_RECORDS_LEAF not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES
    assert member_records.MEMBER_RECORDS_LEAF not in sandbox._CREW_READONLY_LEAVES


def test_the_spelled_names_match_their_owners() -> None:
    assert member_records.RECORD_KINDS == (
        members.RULES_DIR_NAME,
        member_goals.GOALS_DIR_NAME,
        members.DM_BINDINGS_DIR_NAME,
    )
    assert member_records._IN_SANDBOX_MARKER == sandbox_tiers._IN_SANDBOX_MARKER


# ── the one-time move ──


def test_records_written_by_the_old_layout_are_found_after_the_upgrade(home) -> None:
    contents = _write_all_three()
    expected = _reads()
    _as_old_layout(home, contents)

    assert _reads() == expected
    root = home / "member-records"
    for kind, data in contents.items():
        assert (root / kind / f"{_SLUG}.json").read_bytes() == data
        assert not (home / "trust" / kind).exists(), kind
    assert (root / member_records.MIGRATION_MARKER).is_file()


def test_spawn_prep_runs_the_move(home) -> None:
    contents = _write_all_three()
    _as_old_layout(home, contents)

    assert sandbox._member_records_move() is True
    for kind, data in contents.items():
        assert (home / "member-records" / kind / f"{_SLUG}.json").read_bytes() == data


def test_a_sealed_move_never_promotes_a_file_written_under_trust_later(home) -> None:
    _write_all_three()
    assert member_records.ensure_migrated()
    planted = home / "trust" / "member-rules"
    planted.mkdir(parents=True)
    (planted / f"{_SLUG}.json").write_text(
        '{"member": "Atlas", "slug": "atlas", "rules": "anything goes"}', encoding="utf-8"
    )

    assert member_records.ensure_migrated()
    assert members.read_member_rules(_SLUG, _MEMBER) == "never push to main"
    assert (planted / f"{_SLUG}.json").exists(), "the move does not touch it once sealed"


def test_a_pass_stopped_after_its_copy_is_finished_by_the_next(home) -> None:
    contents = _write_all_three()
    _as_old_layout(home, contents)
    # The earlier pass wrote the rules copy and stopped before removing the old file.
    root = home / "member-records"
    (root / "member-rules" / f"{_SLUG}.json").write_bytes(contents["member-rules"])

    assert member_records.ensure_migrated()
    assert not (home / "trust" / "member-rules" / f"{_SLUG}.json").exists()
    assert (root / "member-rules" / f"{_SLUG}.json").read_bytes() == contents["member-rules"]


def test_a_different_new_record_wins_and_the_old_bytes_are_kept(home) -> None:
    contents = _write_all_three()
    _as_old_layout(home, contents)
    root = home / "member-records"
    newer = b'{"member": "Atlas", "slug": "atlas", "rules": "newer"}'
    (root / "member-rules" / f"{_SLUG}.json").write_bytes(newer)

    assert member_records.ensure_migrated()
    assert (root / "member-rules" / f"{_SLUG}.json").read_bytes() == newer
    old = home / "trust" / "member-rules" / f"{_SLUG}.json"
    assert old.read_bytes() == contents["member-rules"]


def test_a_failed_copy_removes_nothing_and_leaves_the_move_unsealed(home, monkeypatch) -> None:
    contents = _write_all_three()
    _as_old_layout(home, contents)
    real = member_records.atomic_write
    calls = {"n": 0}

    def flaky(path, data, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        return real(path, data, **kwargs)

    monkeypatch.setattr(member_records, "atomic_write", flaky)
    assert member_records.ensure_migrated() is False
    for kind in contents:
        assert (home / "trust" / kind / f"{_SLUG}.json").exists(), kind
    assert not (home / "member-records" / member_records.MIGRATION_MARKER).exists()

    monkeypatch.setattr(member_records, "atomic_write", real)
    assert _reads()[0] == "never push to main"
    assert not (home / "trust" / "member-rules").exists()


def test_nothing_reached_through_a_link_is_promoted(home, tmp_path) -> None:
    contents = _write_all_three()
    _as_old_layout(home, contents)
    elsewhere = tmp_path / "elsewhere"
    (home / "trust" / "member-rules").rename(elsewhere)
    (home / "trust" / "member-rules").symlink_to(elsewhere, target_is_directory=True)

    assert member_records.ensure_migrated()
    assert members.read_member_rules(_SLUG, _MEMBER) == ""
    assert (elsewhere / f"{_SLUG}.json").read_bytes() == contents["member-rules"]


def test_the_move_never_runs_inside_a_sandbox(home, monkeypatch) -> None:
    contents = _write_all_three()
    _as_old_layout(home, contents)
    monkeypatch.setenv(member_records._IN_SANDBOX_MARKER, "1")

    assert member_records.ensure_migrated() is False
    for kind in contents:
        assert (home / "trust" / kind / f"{_SLUG}.json").exists(), kind


def test_a_fresh_home_seals_with_nothing_to_move(home) -> None:
    assert member_records.ensure_migrated()
    assert member_records.migrate_from_trust(home / "member-records", home / "trust") == []
    assert sorted(os.listdir(home / "member-records")) == sorted(
        [member_records.LOCK_NAME, member_records.MIGRATION_MARKER]
    )
