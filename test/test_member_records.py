"""Crewmate records: where they live, how they are fenced, and their one-time move.

A crewmate's permanent rules, goals and DM binding live in the gateway-only
``member-records/`` leaf. These tests pin that the record paths resolve there, that
the leaf is fenced on both planes (the OS sandbox mask and the file-tool floor), and
that records an older build wrote under ``trust/`` are found after the upgrade: moved
once, never lost, never promoted again after the move is sealed.
"""

from __future__ import annotations

import os
import sys
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

    def flaky_always():
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(member_records, "atomic_write", flaky)
    assert member_records.ensure_migrated() is False
    for kind in contents:
        assert (home / "trust" / kind / f"{_SLUG}.json").exists(), kind
    assert not (home / "member-records" / member_records.MIGRATION_MARKER).exists()

    # While the move is unfinished, a record still in the old layout never reads as
    # absent: the rules refuse the turn, the goal refuses the read, the binding
    # reads as unbound and cannot be rewritten over the old one.
    monkeypatch.setattr(member_records, "atomic_write", lambda *a, **k: flaky_always())
    with pytest.raises(members.MemberRulesUnreadable):
        members.read_member_rules(_SLUG, _MEMBER)
    with pytest.raises(member_goals.GoalsUnreadable):
        member_goals.read_goals(_SLUG, _MEMBER)
    assert members.read_dm_binding(_SLUG) is None
    with pytest.raises(OSError):
        members.write_dm_binding(_SLUG, member=_MEMBER, slot_key=members.member_slot_key(_SLUG, ""))

    monkeypatch.setattr(member_records, "atomic_write", real)
    assert _reads()[0] == "never push to main"
    assert not (home / "trust" / "member-rules").exists()


def test_no_old_record_is_removed_until_the_data_home_entry_is_synced(home, monkeypatch) -> None:
    """A pass that just made the records directory syncs the data home before removing."""
    contents = _write_all_three()
    _as_old_layout(home, contents)
    real = member_records.fsync_dir
    synced: list[Path] = []

    def refuse_home(path, *args, **kwargs):
        if Path(path) == home:
            raise OSError(5, "Input/output error")
        synced.append(Path(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(member_records, "fsync_dir", refuse_home)
    assert member_records.ensure_migrated() is False
    for kind in contents:
        assert (home / "trust" / kind / f"{_SLUG}.json").exists(), kind
    assert not (home / "member-records" / member_records.MIGRATION_MARKER).exists()

    monkeypatch.setattr(member_records, "fsync_dir", real)
    assert member_records.ensure_migrated()
    assert not (home / "trust" / "member-rules").exists()


def test_records_behind_a_link_are_neither_promoted_nor_read_as_absent(home, tmp_path) -> None:
    """The old build read through a link at ``trust/<kind>``; the move must not seal over it."""
    contents = _write_all_three()
    _as_old_layout(home, contents)
    elsewhere = tmp_path / "elsewhere"
    (home / "trust" / "member-rules").rename(elsewhere)
    (home / "trust" / "member-rules").symlink_to(elsewhere, target_is_directory=True)

    assert member_records.ensure_migrated() is False
    assert "reached through a link" in member_records.last_failure
    assert not (home / "member-records" / member_records.MIGRATION_MARKER).exists()
    with pytest.raises(members.MemberRulesUnreadable):
        members.read_member_rules(_SLUG, _MEMBER)
    with pytest.raises(sandbox.SandboxCeilingUnsealable, match="member-rules"):
        sandbox._member_records_move()
    assert (elsewhere / f"{_SLUG}.json").read_bytes() == contents["member-rules"]
    assert not (home / "member-records" / "member-rules" / f"{_SLUG}.json").exists()

    # The operator replaces the link with the directory it points to; the move finishes.
    (home / "trust" / "member-rules").unlink()
    elsewhere.rename(home / "trust" / "member-rules")
    assert sandbox._member_records_move() is True
    assert members.read_member_rules(_SLUG, _MEMBER) == "never push to main"


def test_a_link_that_leads_to_no_records_does_not_hold_the_move(home, tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    (home / "trust").mkdir()
    (home / "trust" / "member-goals").symlink_to(empty, target_is_directory=True)

    assert member_records.ensure_migrated()


def test_an_old_record_that_is_not_a_regular_file_holds_the_move(home, tmp_path) -> None:
    contents = _write_all_three()
    _as_old_layout(home, contents)
    old = home / "trust" / "member-rules" / f"{_SLUG}.json"
    target = tmp_path / "rules.json"
    target.write_bytes(old.read_bytes())
    old.unlink()
    old.symlink_to(target)

    assert member_records.ensure_migrated() is False
    assert "not a regular file" in member_records.last_failure
    with pytest.raises(members.MemberRulesUnreadable):
        members.read_member_rules(_SLUG, _MEMBER)
    assert old.is_symlink(), "nothing is removed while the move is held"


def test_a_spawn_is_refused_while_old_records_could_still_be_changed(home, monkeypatch) -> None:
    """A sandboxed process can write ``trust/``, so no sandbox launches mid-move."""
    contents = _write_all_three()
    _as_old_layout(home, contents)

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(member_records, "atomic_write", full_disk)
    with pytest.raises(sandbox.SandboxCeilingUnsealable, match="No space left on device"):
        sandbox._member_records_move()
    if sys.platform.startswith("linux"):
        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox.namespace_argv(["/bin/true"], "standard")
    for kind in contents:
        assert (home / "trust" / kind / f"{_SLUG}.json").exists(), kind


def test_a_spawn_is_refused_while_unsealed_even_with_no_old_records(home, monkeypatch) -> None:
    """An empty old layout is no excuse: a sandbox launched before the seal could fill it."""
    real = member_records.atomic_write

    def no_marker(path, data, **kwargs):
        if Path(path).name == member_records.MIGRATION_MARKER:
            raise OSError(28, "No space left on device")
        return real(path, data, **kwargs)

    monkeypatch.setattr(member_records, "atomic_write", no_marker)
    assert member_records.outstanding_kinds() == []
    with pytest.raises(sandbox.SandboxCeilingUnsealable, match="No space left on device"):
        sandbox._member_records_move()
    if sys.platform.startswith("linux"):
        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox.namespace_argv(["/bin/true"], "standard")

    monkeypatch.setattr(member_records, "atomic_write", real)
    assert sandbox._member_records_move() is True


def test_a_fresh_data_home_seals_on_the_first_spawn(tmp_path, monkeypatch) -> None:
    """No carve-out is needed for a new install: the move seals it at once."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "not-created"))
    monkeypatch.delenv(member_records._IN_SANDBOX_MARKER, raising=False)
    assert sandbox._member_records_move() is True
    assert (tmp_path / "not-created" / "member-records" / member_records.MIGRATION_MARKER).is_file()


def test_a_spawn_inside_a_sandbox_is_never_refused_by_the_move(home, monkeypatch) -> None:
    contents = _write_all_three()
    _as_old_layout(home, contents)
    monkeypatch.setenv(member_records._IN_SANDBOX_MARKER, "1")

    assert sandbox._member_records_move() is True


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
