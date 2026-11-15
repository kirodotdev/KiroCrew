"""The crewmate file an instance import writes per imported Bot (spec kc-37418 R6).

The import writes only the minimal file, only for a safe imported name that runs
as its own agent, and only where nothing is at the path: an existing file, even
one that appears inside the owner-lock window, is left byte-for-byte unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew import mate_config
from kiro_crew.portability_bot_crewmates import (
    crewmate_file_notices,
    crewmate_name_held,
    restore_crewmate_files,
    safe_crewmate_name,
)
from kiro_crew.portability_bot_types import ImportedBot, is_minimal_crewmate_file


@pytest.fixture
def kiro(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A throwaway Kiro home with an empty user-level agents folder."""
    home = tmp_path / "kiro"
    (home / "agents").mkdir(parents=True)
    monkeypatch.setenv("KIRO_HOME", str(home))
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: home)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: home / "agents")
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    return home


def _bot(name: str, kiro_agent: str | None = None) -> ImportedBot:
    return ImportedBot(
        name=name,
        slug=name.lower(),
        member_id=name.lower(),
        memory_store=f"{name.lower()}-store",
        generation="1",
        slot_key=f"members:{name.lower()}",
        kiro_agent=name if kiro_agent is None else kiro_agent,
    )


def _tree(root: Path) -> dict[str, bytes | None]:
    return {
        p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
        for p in sorted(root.rglob("*"))
        if not p.name.endswith(".lock")
    }


def _only(outcomes):
    assert len(outcomes) == 1
    out = outcomes[0]
    assert out.part == "crewmate_file"
    return out.status, out.reason


def test_absent_file_is_written_as_the_minimal_file(kiro: Path) -> None:
    out = _only(restore_crewmate_files([_bot("ada")]))

    assert out == ("written", "")
    doc = json.loads(mate_config.mate_config_path("ada").read_text(encoding="utf-8"))
    assert is_minimal_crewmate_file(doc, "ada")
    assert mate_config.crewmate_file_on_record("ada")


def test_existing_file_is_kept_byte_for_byte(kiro: Path) -> None:
    path = mate_config.mate_config_path("ada")
    path.parent.mkdir(parents=True)
    original = b'{"name": "ada", "tools": ["fs_read"], "allowedTools": ["*"]}'
    path.write_bytes(original)

    assert _only(restore_crewmate_files([_bot("ada")])) == ("kept", "")
    assert path.read_bytes() == original
    # A kept file is not claimed for this instance.
    assert not mate_config._owners_path().exists()


@pytest.mark.parametrize(
    ("owned", "expected"),
    [
        (frozenset({"ada"}), ("kept", "")),
        (frozenset(), ("not_written", "held_by_other_bot")),
    ],
)
def test_existing_file_is_this_bots_only_when_it_was_here(
    kiro: Path, owned: frozenset[str], expected: tuple[str, str]
) -> None:
    path = mate_config.mate_config_path("ada")
    path.parent.mkdir(parents=True)
    original = b'{"name": "ada", "tools": ["fs_read"]}'
    path.write_bytes(original)

    assert _only(restore_crewmate_files([_bot("ada")], owned_slugs=owned)) == expected
    assert path.read_bytes() == original


def _on_record_with_grants(name: str) -> Path:
    """A crewmate file this data home wrote for *name*, with an ability added since."""
    spec = {**mate_config.minimal_mate_spec(name), "allowedTools": ["*"]}
    path = mate_config.write_mate_spec(name, spec, only_if_absent=True)
    assert path is not None and mate_config.crewmate_file_on_record(name)
    return path


@pytest.mark.parametrize(
    ("fresh_home", "expected"),
    [
        # A fresh data home at the same path finds the file this instance wrote (A20).
        (True, ("kept", "")),
        # This data home wrote it, for an earlier Bot of that name that is gone (A24).
        (False, ("not_written", "held_by_other_bot")),
    ],
)
def test_a_file_on_record_with_grants_is_own_only_in_a_fresh_data_home(
    kiro: Path, fresh_home: bool, expected: tuple[str, str]
) -> None:
    path = _on_record_with_grants("ada")
    before = path.read_bytes()

    outcome = restore_crewmate_files([_bot("ada")], owned_slugs=frozenset(), fresh_home=fresh_home)

    assert _only(outcome) == expected
    assert path.read_bytes() == before


@pytest.mark.parametrize("on_record", [True, False])
def test_an_exactly_minimal_file_is_kept_whoever_wrote_it(kiro: Path, on_record: bool) -> None:
    path = mate_config.mate_config_path("ada")
    if on_record:
        assert mate_config.write_mate_config("ada", only_if_absent=True) is not None
    else:
        path.parent.mkdir(parents=True)
        path.write_text(
            mate_config.mate_spec_text(mate_config.minimal_mate_spec("ada")), encoding="utf-8"
        )
    assert mate_config.crewmate_file_on_record("ada") is on_record

    outcome = restore_crewmate_files([_bot("ada")], owned_slugs=frozenset())

    assert _only(outcome) == ("kept", "")


def test_the_minimal_document_in_other_whitespace_is_still_minimal(kiro: Path) -> None:
    # Equal as parsed JSON: the same document, written without indentation.
    path = mate_config.mate_config_path("ada")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(mate_config.minimal_mate_spec("ada")), encoding="utf-8")

    assert crewmate_name_held("ada") == ""
    assert _only(restore_crewmate_files([_bot("ada")], owned_slugs=frozenset())) == ("kept", "")


def _foreign_file(name: str, body: bytes) -> Path:
    """A crewmate file the owner record gives another data home."""
    path = mate_config.mate_config_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    mate_config._owners_path().write_text(
        json.dumps({name: "/elsewhere/other-crew-home"}), encoding="utf-8"
    )
    return path


@pytest.mark.parametrize("owned", [None, frozenset({"ada"}), frozenset()])
@pytest.mark.parametrize("fresh_home", [True, False])
def test_a_file_another_data_home_owns_is_never_own_or_free(
    kiro: Path, owned: frozenset[str] | None, fresh_home: bool
) -> None:
    minimal = mate_config.mate_spec_text(mate_config.minimal_mate_spec("ada")).encode()
    path = _foreign_file("ada", minimal)

    assert crewmate_name_held("ada", fresh_home=fresh_home) == "foreign_owner"
    outcome = restore_crewmate_files([_bot("ada")], owned_slugs=owned, fresh_home=fresh_home)
    assert _only(outcome) == ("not_written", "foreign_owner")
    assert path.read_bytes() == minimal


def test_a_link_another_data_home_owns_is_foreign_too(kiro: Path) -> None:
    path = mate_config.mate_config_path("ada")
    path.parent.mkdir(parents=True)
    # Dangling, beside the file, so the name still resolves inside the crewmate folder.
    path.symlink_to(path.parent / "missing.json")
    mate_config._owners_path().write_text(
        json.dumps({"ada": "/elsewhere/other-crew-home"}), encoding="utf-8"
    )

    assert crewmate_name_held("ada") == "foreign_owner"
    assert _only(restore_crewmate_files([_bot("ada")])) == ("not_written", "foreign_owner")
    assert path.is_symlink()


def test_an_unreadable_owner_record_makes_an_existing_file_held(kiro: Path) -> None:
    path = mate_config.mate_config_path("ada")
    path.parent.mkdir(parents=True)
    path.write_text(
        mate_config.mate_spec_text(mate_config.minimal_mate_spec("ada")), encoding="utf-8"
    )
    mate_config._owners_path().write_text("{not json", encoding="utf-8")

    assert crewmate_name_held("ada") == "held_by_other_bot"


def test_crewmate_name_held_follows_the_own_file_rule(kiro: Path) -> None:
    path = mate_config.mate_config_path("ada")
    assert crewmate_name_held("ada") == ""
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"name": "ada", "allowedTools": ["*"]}')
    assert crewmate_name_held("ada") == "held_by_other_bot"
    assert crewmate_name_held("ada", fresh_home=True) == "held_by_other_bot"
    path.unlink()
    assert mate_config.write_mate_config("ada", only_if_absent=True) is not None
    assert crewmate_name_held("ada") == ""
    path.unlink()
    _on_record_with_grants("ada")
    assert crewmate_name_held("ada") == "held_by_other_bot"
    assert crewmate_name_held("ada", fresh_home=True) == ""
    (kiro / "agents" / "bea.json").write_text(json.dumps({"name": "bea"}), encoding="utf-8")
    assert crewmate_name_held("bea") == "user_agent_named"


def test_file_created_after_the_absence_check_is_kept(kiro: Path, monkeypatch) -> None:
    """A writer that lands inside the owner-lock window, after the check, wins."""
    path = mate_config.mate_config_path("ada")
    racer = b'{"name": "ada", "prompt": "racer"}'
    real_stage = mate_config._stage_spec
    calls: list[str] = []

    def stage_then_race(target: Path, text: str) -> str:
        staged = real_stage(target, text)
        calls.append(staged)
        target.write_bytes(racer)
        return staged

    monkeypatch.setattr(mate_config, "_stage_spec", stage_then_race)

    assert _only(restore_crewmate_files([_bot("ada")])) == ("kept", "")
    assert calls, "the race stub never ran"
    assert path.read_bytes() == racer
    assert not Path(calls[0]).exists()
    assert sorted(p.name for p in path.parent.iterdir()) == ["ada.json"]


def test_file_created_once_the_lock_is_held_is_kept(kiro: Path, monkeypatch) -> None:
    path = mate_config.mate_config_path("ada")
    racer = b'{"name": "ada", "prompt": "racer"}'
    real_read = mate_config._read_owners

    def read_then_race() -> dict[str, str]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(racer)
        return real_read()

    monkeypatch.setattr(mate_config, "_read_owners", read_then_race)

    assert _only(restore_crewmate_files([_bot("ada")])) == ("kept", "")
    assert path.read_bytes() == racer


def test_bot_running_another_agent_needs_no_file(kiro: Path) -> None:
    assert _only(restore_crewmate_files([_bot("ada", kiro_agent="builder")])) == (
        "not_needed",
        "",
    )
    assert not mate_config.mate_config_path("ada").exists()


@pytest.mark.parametrize("name", ["../x", "a/b", ".hidden", "a\\b", "nul\0x", ""])
def test_unsafe_names_write_nothing_anywhere(kiro: Path, name: str) -> None:
    mate_config.mate_config_path("seed").parent.mkdir(parents=True)
    before = _tree(kiro)

    assert _only(restore_crewmate_files([_bot(name)])) == ("not_written", "unsafe_name")
    assert _tree(kiro) == before
    assert not safe_crewmate_name(name)


def test_name_through_a_linked_path_is_unsafe(kiro: Path, tmp_path: Path) -> None:
    folder = paths_mod.mate_agents_dir()
    folder.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    try:
        (folder / "ada.json").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    assert _only(restore_crewmate_files([_bot("ada")])) == ("not_written", "unsafe_name")
    assert outside.read_text(encoding="utf-8") == "{}"


def test_a_mate_service_name_never_reaches_the_crewmate_step(kiro: Path) -> None:
    # A44: a service name is a reserved name (is_reserved_bot_name), so no import step
    # hands one to restore_crewmate_files; should one arrive, the writer's own refusal
    # writes nothing and the part is a failed write, never a file over the spec.
    from kiro_crew.agent_files import MATE_SERVICE_AGENT_NAMES
    from kiro_crew.portability_bot_types import REASONS, is_reserved_bot_name

    name = sorted(MATE_SERVICE_AGENT_NAMES)[0]
    assert is_reserved_bot_name(name)
    assert "reserved_name" not in REASONS
    before = _tree(kiro)

    assert _only(restore_crewmate_files([_bot(name)])) == ("not_written", "write_failed")
    assert _tree(kiro) == before


def test_user_level_agent_of_that_name_is_refused(kiro: Path) -> None:
    (kiro / "agents" / "ada.json").write_text(
        json.dumps({"name": "ada", "prompt": "mine"}), encoding="utf-8"
    )

    assert _only(restore_crewmate_files([_bot("ada")])) == (
        "not_written",
        "user_agent_named",
    )
    assert not mate_config.mate_config_path("ada").exists()


def test_foreign_owner_is_reported_and_its_file_kept(kiro: Path, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(paths_mod, "data_home", lambda: tmp_path / "instance-a")
    mate_config.write_mate_config("ada")
    path = mate_config.mate_config_path("ada")
    path.write_bytes(b'{"name": "ada", "prompt": "theirs"}')
    monkeypatch.setattr(paths_mod, "data_home", lambda: tmp_path / "instance-b")

    assert _only(restore_crewmate_files([_bot("ada")])) == ("not_written", "foreign_owner")
    assert path.read_bytes() == b'{"name": "ada", "prompt": "theirs"}'


def test_write_error_is_reported_not_raised(kiro: Path, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise mate_config.MateConfigError("disk full")

    monkeypatch.setattr(mate_config, "write_mate_config", boom)
    assert _only(restore_crewmate_files([_bot("ada")])) == ("not_written", "write_failed")

    def crash(*_a, **_k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(mate_config, "write_mate_config", crash)
    assert _only(restore_crewmate_files([_bot("ada")])) == ("not_written", "write_failed")


def test_one_outcome_per_bot_in_order(kiro: Path) -> None:
    bots = [_bot("ada"), _bot("../x"), _bot("bo", kiro_agent="builder")]
    outcomes = restore_crewmate_files(bots)
    assert [(o.bot, o.status) for o in outcomes] == [
        ("ada", "written"),
        ("../x", "not_written"),
        ("bo", "not_needed"),
    ]


def test_customised_bot_gets_the_notice_and_only_imported_names_count(kiro: Path) -> None:
    bots = [_bot("ada"), _bot("bo")]
    customised = ["ada", "stranger"]

    outcomes = restore_crewmate_files(bots)

    assert [o.status for o in outcomes] == ["written", "written"]
    written = {o.bot for o in outcomes if o.status == "written"}
    assert crewmate_file_notices(bots, customised, written=written) == {
        "ada": ["starting_abilities_only"]
    }
    # A file already here kept its abilities: nothing was lost, so no notice.
    assert crewmate_file_notices(bots, customised, written=set()) == {}
    assert not mate_config.mate_config_path("stranger").exists()
    # The written file is the minimal one, never the customised abilities.
    doc = json.loads(mate_config.mate_config_path("ada").read_text(encoding="utf-8"))
    assert is_minimal_crewmate_file(doc, "ada")


def test_default_write_still_replaces_an_existing_file(kiro: Path) -> None:
    path = mate_config.mate_config_path("ada")
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"name": "ada", "tools": ["x"]}')

    assert mate_config.write_mate_config("ada") == path
    assert is_minimal_crewmate_file(json.loads(path.read_text(encoding="utf-8")), "ada")


def test_write_mate_spec_only_if_absent_returns_the_path_or_none(kiro: Path) -> None:
    spec = {"name": "ada", "prompt": "p"}
    path = mate_config.write_mate_spec("ada", dict(spec), only_if_absent=True)
    assert path == mate_config.mate_config_path("ada")
    first = path.read_bytes()

    assert (
        mate_config.write_mate_spec("ada", {"name": "ada", "prompt": "q"}, only_if_absent=True)
        is None
    )
    assert path.read_bytes() == first
