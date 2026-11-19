"""The instance import's roster merge and imported-Bot derivation (portability_bot_roster)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from kiro_crew import crew_teams
from kiro_crew import portability_bot_roster as roster
from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache, update_config_locked
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.handlers.members_thread import _member_thread_slot
from kiro_crew.members import member_slug
from kiro_crew.memory_stores import MEMORY_STORES_DIR_NAME


def _bot(member_id: str, store: str, workspace: str, **extra) -> dict:
    return {
        "member_id": member_id,
        "memory_store": store,
        "workspace": workspace,
        "kiro_agent": extra.pop("kiro_agent", "kirocrew"),
        **extra,
    }


def _store(owner: str, version: int = 2) -> dict:
    return {"owner_member_id": owner, "memory_version": version}


def _destination() -> dict:
    return {
        "agents": {
            "kirocrew": {"kiro_agent": "kirocrew"},
            "Atlas": _bot("atlas", "atlas-mem", "member-atlas"),
        },
        "memory_stores": {"default": {}, "atlas-mem": _store("atlas")},
        "workspaces": {"default": {"dir": "workspace"}, "member-atlas": {"dir": "workspace-x"}},
    }


@pytest.fixture
def home() -> Path:
    mc = config_dir()
    doc = _destination()
    update_config_locked(mc / "config.json", mutate=lambda _d: copy.deepcopy(doc))
    _invalidate_config_cache()
    # One ordinary load first, so the loader's own migrations are on disk before a
    # test compares the file: merge_roster's name scans load agent specs.
    KiroCrewConfig.load()
    _invalidate_config_cache()
    return mc


def _read(mc: Path) -> dict:
    data = json.loads((mc / "config.json").read_text(encoding="utf-8"))
    data.pop("meta", None)
    return data


def _carry(mc: Path, *stores: str) -> set[str]:
    """Stand in for the named-store merge: the directories it created here."""
    for store in stores:
        (mc / MEMORY_STORES_DIR_NAME / store).mkdir(parents=True, exist_ok=True)
    return set(stores)


def _archive(**bots: tuple[dict, dict | None]) -> dict:
    agents, stores = {}, {}
    for name, (entry, store_entry) in bots.items():
        agents[name] = entry
        if store_entry is not None:
            stores[entry["memory_store"]] = store_entry
    return {"agents": agents, "memory_stores": stores}


def _reasons(result: roster.RosterMerge) -> dict[str, str]:
    for row in result.skipped:
        assert (row.part, row.status) == ("roster", "skipped")
    return {row.bot: row.reason for row in result.skipped}


def _assert_destination_unchanged(before: dict, after: dict) -> None:
    for section in ("agents", "memory_stores", "workspaces"):
        for key, value in before[section].items():
            assert after[section][key] == value, (section, key)


def test_free_bot_is_added_with_its_store_and_workspace(home, monkeypatch):
    released: list[str] = []
    real_release = crew_teams.release_for_create
    monkeypatch.setattr(
        crew_teams,
        "release_for_create",
        lambda name: (released.append(name), real_release(name))[1],
    )
    entry = _bot("nova", "nova-mem", "member-nova", description="hi")
    before = _read(home)

    result = roster.merge_roster(
        _archive(Nova=(entry, _store("nova"))), home, stores_added=_carry(home, "nova-mem")
    )

    assert result.added == ["Nova"] and result.skipped == []
    assert released == ["Nova"]
    after = _read(home)
    # Its own crewmate file, whatever the archive named (A10).
    assert after["agents"]["Nova"] == {**entry, "kiro_agent": "Nova"}
    # Its store is owned by the Bot's own name, whatever the archive says (A14).
    assert after["memory_stores"]["nova-mem"] == {**_store("nova"), "owner_member": "Nova"}
    assert after["workspaces"]["member-nova"] == {"dir": "workspace-member-nova"}
    assert (home / "workspace-member-nova").is_dir()
    _assert_destination_unchanged(before, after)
    assert set(after["agents"]) == set(before["agents"]) | {"Nova"}


def test_the_archive_workspace_dir_is_never_used(home):
    archive = _archive(Nova=(_bot("nova", "nova-mem", "member-nova"), _store("nova")))
    archive["workspaces"] = {"member-nova": {"dir": "/somewhere/else"}}

    roster.merge_roster(archive, home, stores_added=_carry(home, "nova-mem"))

    assert _read(home)["workspaces"]["member-nova"] == {"dir": "workspace-member-nova"}


@pytest.mark.parametrize(
    ("name", "entry", "store_entry", "carried", "reason"),
    [
        ("Atlas", _bot("atlas2", "a2-mem", "member-a2"), _store("atlas2"), True, "name_taken"),
        ("ATLAS", _bot("atlas2", "a2-mem", "member-a2"), _store("atlas2"), True, "name_taken"),
        ("Other", _bot("atlas", "o-mem", "member-o"), _store("atlas"), True, "slug_taken"),
        ("Vega", _bot("vega", "atlas-mem", "member-v"), _store("vega"), False, "store_taken"),
        ("Vega", _bot("vega", "vega-mem", "member-v"), _store("vega"), False, "store_not_carried"),
        ("Vega", _bot("vega", "vega-mem", "member-atlas"), _store("vega"), True, "workspace_taken"),
        ("Vega", _bot("vega", "vega-mem", "default"), _store("vega"), True, "workspace_taken"),
        ("Vega", _bot("", "vega-mem", "member-v"), _store(""), True, "no_member_id"),
        ("Vega", {"memory_store": "vega-mem", "workspace": "w"}, None, True, "no_member_id"),
        ("Vega", _bot("vega", "default", "member-v"), None, True, "config_rejected"),
        ("Vega", _bot("vega", "vega-mem", "member-v"), _store("other"), True, "config_rejected"),
        ("Vega", _bot("vega", "vega-mem", "member-v"), _store("vega", 1), True, "config_rejected"),
        ("Vega", _bot("vega", "vega-mem", "member-v"), None, True, "config_rejected"),
        ("Vega", _bot("../x", "vega-mem", "member-v"), _store("../x"), True, "config_rejected"),
        ("a\x00b", _bot("ab", "vega-mem", "member-v"), _store("ab"), True, "config_rejected"),
    ],
)
def test_each_collision_is_skipped_with_its_reason(home, name, entry, store_entry, carried, reason):
    store = entry["memory_store"]
    if store == "atlas-mem":
        # The merge kept this install's own store: it exists, the merge did not create it.
        (home / MEMORY_STORES_DIR_NAME / store).mkdir(parents=True)
    added = _carry(home, store) if carried and store != "default" else set()
    before = _read(home)

    result = roster.merge_roster(_archive(**{name: (entry, store_entry)}), home, stores_added=added)

    assert result.added == []
    assert _reasons(result) == {name: reason}
    assert _read(home) == before


def test_the_same_bot_is_kept_and_nothing_is_written(home):
    # An install merging its own export: same name, member_id and memory store.
    before = (home / "config.json").read_bytes()
    archive = _archive(
        Atlas=(_bot("atlas", "atlas-mem", "member-atlas", model="other"), _store("atlas"))
    )

    result = roster.merge_roster(archive, home, stores_added=set())

    assert (result.kept, result.added, result.skipped) == (["Atlas"], [], [])
    assert (home / "config.json").read_bytes() == before


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (_bot("atlas", "other-mem", "member-atlas"), "name_taken"),
        (_bot("atlas2", "atlas-mem", "member-atlas"), "name_taken"),
        (_bot("", "atlas-mem", "member-atlas"), "no_member_id"),
    ],
)
def test_a_name_match_that_is_not_the_same_bot_is_skipped(home, entry, reason):
    result = roster.merge_roster(
        _archive(Atlas=(entry, _store(entry["member_id"]))), home, stores_added=set()
    )

    assert result.kept == []
    assert _reasons(result) == {"Atlas": reason}


def test_the_reserved_default_agent_is_never_a_bot(home):
    archive = {"agents": {"default": {"kiro_agent": "kirocrew"}}, "memory_stores": {}}

    result = roster.merge_roster(archive, home, stores_added=set())

    assert (result.kept, result.added, result.skipped) == ([], [], [])


def test_an_added_entry_drops_the_harness_model_and_approval_fields(home):
    # A25: the model choices and the capability switch are the destination's defaults.
    entry = _bot(
        "nova",
        "nova-mem",
        "member-nova",
        kiro_agent="someone-else",
        approval_mode="trust",
        acp_backend="claude",
        model="archive-model",
        worker_model="archive-worker-model",
        reasoning_effort="high",
        worker_reasoning_effort="low",
        dynamic_capabilities=True,
        starred=True,
        star_order=3,
        autocompact_pct=80,
        avatar={"emoji": "x"},
    )

    result = roster.merge_roster(
        _archive(Nova=(entry, _store("nova"))), home, stores_added=_carry(home, "nova-mem")
    )

    assert result.added == ["Nova"]
    added = _read(home)["agents"]["Nova"]
    assert added["kiro_agent"] == "Nova"
    dropped = (
        "approval_mode",
        "acp_backend",
        "model",
        "worker_model",
        "reasoning_effort",
        "worker_reasoning_effort",
        "dynamic_capabilities",
    )
    assert not [key for key in dropped if key in added], added
    assert (added["starred"], added["star_order"], added["avatar"]) == (True, 3, {"emoji": "x"})


@pytest.mark.parametrize(
    "extra",
    [
        {"unknown_field": "x"},
        {"starred": "yes"},
        {"star_order": True},
        {"autocompact_pct": float("nan")},
        {"avatar": ["x"]},
        {"description": None},
    ],
)
def test_an_entry_field_that_does_not_validate_is_config_rejected(home, extra):
    entry = _bot("nova", "nova-mem", "member-nova", **extra)
    before = _read(home)

    result = roster.merge_roster(
        _archive(Nova=(entry, _store("nova"))), home, stores_added=_carry(home, "nova-mem")
    )

    assert _reasons(result) == {"Nova": "config_rejected"}
    assert _read(home) == before


@pytest.mark.parametrize(
    "store_entry",
    [
        {"owner_member_id": "nova", "memory_version": True},
        {"owner_member_id": "nova", "memory_version": 2.0},
        {"owner_member_id": "nova", "memory_version": 2, "unknown_field": "x"},
        {"owner_member_id": "nova", "memory_version": 2, "description": 5},
        {"owner_member_id": "nova", "memory_version": 2, "embedding_provider": None},
        {"owner_member_id": "", "memory_version": 2},
        "not an object",
    ],
)
def test_a_store_entry_that_does_not_validate_is_config_rejected(home, store_entry):
    entry = _bot("nova", "nova-mem", "member-nova")
    before = _read(home)

    result = roster.merge_roster(
        _archive(Nova=(entry, store_entry)), home, stores_added=_carry(home, "nova-mem")
    )

    assert _reasons(result) == {"Nova": "config_rejected"}
    assert _read(home) == before


def test_the_store_entry_owner_label_is_the_bots_name():
    added = roster.merged_store_entry(
        "Nova",
        "nova",
        {
            "owner_member_id": "nova",
            "memory_version": 2,
            "owner_member": "Mallory",
            "description": "d",
        },
    )
    assert added == {
        "owner_member_id": "nova",
        "memory_version": 2,
        "owner_member": "Nova",
        "description": "d",
    }


def test_a_member_id_owning_a_destination_store_is_taken(home):
    update_config_locked(
        home / "config.json",
        mutate=lambda d: {**d, "memory_stores": {**d["memory_stores"], "orphan": _store("vega")}},
    )

    result = roster.merge_roster(
        _archive(Vega=(_bot("vega", "vega-mem", "member-v"), _store("vega"))),
        home,
        stores_added=_carry(home, "vega-mem"),
    )

    assert _reasons(result) == {"Vega": "member_id_taken"}


def test_two_archive_bots_that_collide_with_each_other(home):
    archive = _archive(
        Nova=(_bot("nova", "nova-mem", "member-nova"), _store("nova")),
        nova=(_bot("nova2", "nova2-mem", "member-nova2"), _store("nova2")),
        Lyra=(_bot("lyra", "lyra-mem", "member-nova"), _store("lyra")),
    )

    result = roster.merge_roster(
        archive, home, stores_added=_carry(home, "nova-mem", "nova2-mem", "lyra-mem")
    )

    assert result.added == ["Nova"]
    assert _reasons(result) == {"nova": "name_taken", "Lyra": "workspace_taken"}


def test_v1_bot_and_free_bot_in_one_archive(home):
    archive = _archive(
        Old=({"memory_store": "default", "workspace": "default"}, None),
        Nova=(_bot("nova", "nova-mem", "member-nova"), _store("nova")),
    )

    result = roster.merge_roster(archive, home, stores_added=_carry(home, "nova-mem"))

    assert result.added == ["Nova"]
    assert _reasons(result) == {"Old": "no_member_id"}


def test_a_concurrent_create_under_the_lock_wins(home, monkeypatch):
    """The conditions are re-checked on the document read under the lock."""
    real_update = update_config_locked

    def racing_update(path=None, *, mutate, **kw):
        def raced(data):
            data["agents"]["nova"] = {"kiro_agent": "kirocrew"}
            return mutate(data)

        return real_update(path, mutate=raced, **kw)

    monkeypatch.setattr("kiro_crew.config.loader.update_config_locked", racing_update)
    result = roster.merge_roster(
        _archive(Nova=(_bot("nova", "nova-mem", "member-nova"), _store("nova"))),
        home,
        stores_added=_carry(home, "nova-mem"),
    )
    assert result.added == []
    assert _reasons(result) == {"Nova": "name_taken"}
    assert "Nova" not in _read(home)["agents"]


def _nova_merge(home: Path, **kw) -> roster.RosterMerge:
    return roster.merge_roster(
        _archive(Nova=(_bot("nova", "nova-mem", "member-nova"), _store("nova"))),
        home,
        stores_added=_carry(home, "nova-mem"),
        **kw,
    )


def test_teams_are_released_inside_the_locked_mutation(home, monkeypatch):
    # As every create path: inside the config write, before the name is registered
    # (crew_teams.release_name's contract), so no concurrent create can team it between.
    from kiro_crew.config import loader

    real_update = loader.update_config_locked
    target = home / "config.json"
    depth = [0]
    seen: list[tuple[bool, bool]] = []

    def tracked(path=None, *, mutate, **kw):
        mine = path == target
        depth[0] += mine
        try:
            return real_update(path, mutate=mutate, **kw)
        finally:
            depth[0] -= mine

    def release(name):
        seen.append((depth[0] > 0, name in _read(home)["agents"]))

    monkeypatch.setattr(loader, "update_config_locked", tracked)
    monkeypatch.setattr(crew_teams, "release_for_create", release)
    result = _nova_merge(home)
    assert result.added == ["Nova"]
    assert seen == [(True, False)]
    assert "Nova" in _read(home)["agents"]


def test_a_workspace_directory_already_here_skips_the_bot_before_any_release(home, monkeypatch):
    # A25: the folder is never adopted, and the Bot's team memberships are not released
    # for a Bot that is not added.
    released: list[str] = []
    monkeypatch.setattr(crew_teams, "release_for_create", released.append)
    (home / "workspace-member-nova").mkdir()
    (home / "workspace-member-nova" / "notes.md").write_text("mine", encoding="utf-8")
    before = _read(home)

    result = _nova_merge(home)

    assert result.added == []
    assert _reasons(result) == {"Nova": "workspace_taken"}
    assert released == []
    assert _read(home) == before
    assert (home / "workspace-member-nova" / "notes.md").read_text(encoding="utf-8") == "mine"


@pytest.mark.parametrize(
    "extra",
    [{"member_id": "Not A Slug"}, {"memory_store": "default"}],
)
def test_a_bot_failing_a_check_releases_nothing(home, monkeypatch, extra):
    released: list[str] = []
    monkeypatch.setattr(crew_teams, "release_for_create", released.append)
    entry = {**_bot("nova", "nova-mem", "member-nova"), **extra}

    result = roster.merge_roster(
        _archive(Nova=(entry, _store("nova"))), home, stores_added=_carry(home, "nova-mem")
    )

    assert result.added == []
    assert released == []


def test_teams_unavailable_refuses_the_bot(home, monkeypatch):
    def refuse(_name):
        raise crew_teams.TeamsUnavailable("unreadable")

    monkeypatch.setattr(crew_teams, "release_for_create", refuse)

    result = _nova_merge(home)

    assert result.added == []
    assert _reasons(result) == {"Nova": "write_failed"}
    assert "Nova" not in _read(home)["agents"]
    assert "member-nova" not in _read(home).get("workspaces", {})
    assert not (home / "workspace-member-nova").exists()


def test_no_release_when_the_import_installed_the_archive_teams(home, monkeypatch):
    released: list[str] = []
    monkeypatch.setattr(crew_teams, "release_for_create", released.append)

    result = _nova_merge(home, teams_installed=True)

    assert result.added == ["Nova"]
    assert released == []


def test_a_foreign_crewmate_file_at_the_name_skips_the_bot(home):
    # Added with kiro_agent forced to its name, the Bot would run with the grants of
    # a file this import never wrote (A20).
    from kiro_crew import mate_config

    path = mate_config.mate_config_path("Nova")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": "Nova", "allowedTools": ["*"]}), encoding="utf-8")

    result = _nova_merge(home)

    assert result.added == []
    assert _reasons(result) == {"Nova": "held_by_other_bot"}
    assert "Nova" not in _read(home)["agents"]


def test_a_minimal_file_another_data_home_owns_skips_the_bot_up_front(home, monkeypatch):
    # Byte-minimal, so free by content, but the owner record names another data home:
    # the Bot is never added onto it (A29), and the file is never scanned again inside
    # the mutation (A31).
    from kiro_crew import mate_config
    from kiro_crew import portability_bot_crewmates as crewmates

    path = mate_config.mate_config_path("Nova")
    path.parent.mkdir(parents=True, exist_ok=True)
    minimal = mate_config.mate_spec_text(mate_config.minimal_mate_spec("Nova"))
    path.write_text(minimal, encoding="utf-8")
    mate_config._owners_path().write_text(
        json.dumps({"Nova": "/elsewhere/other-crew-home"}), encoding="utf-8"
    )
    scans: list[str] = []
    real = crewmates.crewmate_name_held

    def counted(name: str, **kw: object) -> str:
        scans.append(name)
        return real(name, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(crewmates, "crewmate_name_held", counted)

    result = _nova_merge(home)

    assert result.added == []
    assert _reasons(result) == {"Nova": "foreign_owner"}
    assert "Nova" not in _read(home)["agents"]
    assert path.read_text(encoding="utf-8") == minimal
    assert scans == ["Nova"]


def test_this_homes_file_with_grants_for_a_gone_bot_skips_the_bot(home):
    # An earlier Nova of this data home was deleted and its file, with an added
    # ability, stayed: on record here, but its grants are not the archive Bot's (A24).
    from kiro_crew import mate_config

    spec = {**mate_config.minimal_mate_spec("Nova"), "allowedTools": ["*"]}
    path = mate_config.write_mate_spec("Nova", spec, only_if_absent=True)
    assert path is not None and mate_config.crewmate_file_on_record("Nova")
    before = path.read_bytes()

    result = _nova_merge(home)

    assert result.added == []
    assert _reasons(result) == {"Nova": "held_by_other_bot"}
    assert "Nova" not in _read(home)["agents"]
    assert path.read_bytes() == before


def test_this_homes_own_crewmate_file_lets_the_bot_be_added(home):
    from kiro_crew import mate_config

    assert mate_config.write_mate_config("Nova", only_if_absent=True) is not None
    assert mate_config.crewmate_file_on_record("Nova")

    assert _nova_merge(home).added == ["Nova"]


def test_a_user_agent_of_the_name_skips_the_bot(home):
    from kiro_crew.config import paths as paths_mod

    agents = paths_mod.kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "Nova.json").write_text(json.dumps({"name": "Nova"}), encoding="utf-8")

    result = _nova_merge(home)

    assert result.added == []
    assert _reasons(result) == {"Nova": "user_agent_named"}


def test_a_write_that_fails_after_the_mutation_removes_its_empty_workspace(home, monkeypatch):
    released: list[str] = []
    monkeypatch.setattr(crew_teams, "release_for_create", released.append)

    def run_then_fail(target, *, mutate, **kw):
        mutate(json.loads(Path(target).read_text(encoding="utf-8")))
        raise OSError("disk full")

    monkeypatch.setattr("kiro_crew.config.loader.update_config_locked", run_then_fail)
    (home / "workspace-member-kept").mkdir()
    result = roster.merge_roster(
        _archive(
            Nova=(_bot("nova", "nova-mem", "member-nova"), _store("nova")),
            Kept=(_bot("kept", "kept-mem", "member-kept"), _store("kept")),
        ),
        home,
        stores_added=_carry(home, "nova-mem", "kept-mem"),
    )

    assert result.added == []
    assert _reasons(result) == {"Nova": "write_failed", "Kept": "workspace_taken"}
    # The directory this call made is gone; one that was already there is left.
    assert not (home / "workspace-member-nova").exists()
    assert (home / "workspace-member-kept").is_dir()


def test_a_refused_write_reports_every_bot_and_never_raises(home, monkeypatch):
    def refuse(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr("kiro_crew.config.loader.update_config_locked", refuse)

    result = roster.merge_roster(
        _archive(Nova=(_bot("nova", "nova-mem", "member-nova"), _store("nova"))),
        home,
        stores_added=_carry(home, "nova-mem"),
    )

    assert result.added == []
    assert _reasons(result) == {"Nova": "write_failed"}


def test_a_write_that_lands_and_then_raises_reports_the_bots_that_landed(home, monkeypatch):
    real_update = update_config_locked

    def land_then_fail(target, *, mutate, **kw):
        real_update(target, mutate=mutate, **kw)
        raise OSError("the step after the write failed")

    monkeypatch.setattr("kiro_crew.config.loader.update_config_locked", land_then_fail)
    monkeypatch.setattr(crew_teams, "release_for_create", lambda _name: None)

    result = roster.merge_roster(
        _archive(
            Nova=(_bot("nova", "nova-mem", "member-nova"), _store("nova")),
            Atlas=(_bot("atlas", "atlas-mem", "member-atlas"), _store("atlas")),
        ),
        home,
        stores_added=_carry(home, "nova-mem"),
    )

    # Nova's entries are in the config the raised write left: it is added, and its
    # workspace directory stays; the kept Bot is still kept.
    assert result.added == ["Nova"] and result.kept == ["Atlas"]
    assert result.skipped == []
    assert _read(home)["agents"]["Nova"]["kiro_agent"] == "Nova"
    assert (home / "workspace-member-nova").is_dir()


def test_a_raised_write_whose_config_cannot_be_read_again_adds_nothing(home, monkeypatch):
    import kiro_crew.portability_bot_export as export_mod

    real_update = update_config_locked

    def land_then_fail(target, *, mutate, **kw):
        real_update(target, mutate=mutate, **kw)
        raise ValueError("refused after the write")

    monkeypatch.setattr("kiro_crew.config.loader.update_config_locked", land_then_fail)
    monkeypatch.setattr(crew_teams, "release_for_create", lambda _name: None)
    monkeypatch.setattr(export_mod, "read_config_document", lambda _mc: None)

    result = roster.merge_roster(
        _archive(Nova=(_bot("nova", "nova-mem", "member-nova"), _store("nova"))),
        home,
        stores_added=_carry(home, "nova-mem"),
    )

    assert result.added == []
    assert _reasons(result) == {"Nova": "config_rejected"}


def test_a_crewmate_pre_scan_that_raises_refuses_only_that_bot(home, monkeypatch):
    from kiro_crew import portability_bot_crewmates as crewmates

    real = crewmates.crewmate_name_held

    def scan(name, **kw):
        if name == "Nova":
            raise RuntimeError("owner record unreadable")
        return real(name, **kw)

    monkeypatch.setattr(crewmates, "crewmate_name_held", scan)

    result = roster.merge_roster(
        _archive(
            Nova=(_bot("nova", "nova-mem", "member-nova"), _store("nova")),
            Vega=(_bot("vega", "vega-mem", "member-vega"), _store("vega")),
        ),
        home,
        stores_added=_carry(home, "nova-mem", "vega-mem"),
    )

    assert result.added == ["Vega"]
    assert _reasons(result) == {"Nova": "config_rejected"}
    assert "Nova" not in _read(home)["agents"]


def test_a_check_that_raises_for_one_bot_refuses_only_that_bot(home, monkeypatch):
    real = roster._candidate_reason

    def judge(name, *a, **kw):
        if name == "Nova":
            raise RecursionError("archive entry too deep")
        return real(name, *a, **kw)

    monkeypatch.setattr(roster, "_candidate_reason", judge)

    result = roster.merge_roster(
        _archive(
            Nova=(_bot("nova", "nova-mem", "member-nova"), _store("nova")),
            Vega=(_bot("vega", "vega-mem", "member-vega"), _store("vega")),
        ),
        home,
        stores_added=_carry(home, "nova-mem", "vega-mem"),
    )

    assert result.added == ["Vega"]
    assert _reasons(result) == {"Nova": "config_rejected"}


def test_an_empty_archive_roster_writes_nothing(home):
    before = (home / "config.json").read_bytes()
    assert roster.merge_roster({"agents": "x"}, home, stores_added=set()) == roster.RosterMerge()
    assert (home / "config.json").read_bytes() == before


def test_imported_bots_slot_equals_the_thread_rule(home):
    roster.merge_roster(
        _archive(Nova=(_bot("nova", "nova-mem", "member-nova", kiro_agent="Nova"), _store("nova"))),
        home,
        stores_added=_carry(home, "nova-mem"),
    )

    bots, outcomes = roster.imported_bots(home, ["Nova", "Atlas"])

    assert outcomes == []
    cfg = KiroCrewConfig.load()
    for bot in bots:
        slug = member_slug(bot.name, cfg)
        slot_key, generation = _member_thread_slot(cfg, bot.name, slug)
        assert (bot.slug, bot.slot_key, bot.generation) == (slug, slot_key, generation)
        assert bot.member_id == cfg.agents[bot.name].member_id
        assert bot.memory_store == cfg.agents[bot.name].memory_store
    nova = next(b for b in bots if b.name == "Nova")
    assert (nova.slug, nova.generation, nova.kiro_agent) == ("nova", "nova-mem", "Nova")
    assert nova.slot_key.endswith("nova-mem")


def test_imported_bots_v1_bot_gets_the_slug_slot(home):
    bots, outcomes = roster.imported_bots(home, ["kirocrew"])
    assert outcomes == []
    cfg = KiroCrewConfig.load()
    assert bots[0].slot_key == _member_thread_slot(cfg, "kirocrew", bots[0].slug)[0]
    assert bots[0].member_id == "" and bots[0].generation == ""


def test_imported_bots_store_not_owned_and_unreadable(home, monkeypatch):
    update_config_locked(
        home / "config.json",
        mutate=lambda d: {
            **d,
            "agents": {
                **d["agents"],
                # A V2 identity whose store declares another owner.
                "Thief": _bot("thief", "t-mem", "member-t"),
            },
            "memory_stores": {**d["memory_stores"], "t-mem": _store("someone")},
        },
    )
    _invalidate_config_cache()

    bots, outcomes = roster.imported_bots(home, ["Thief", "Ghost", "Atlas"])

    assert [b.name for b in bots] == ["Atlas"]
    assert [(o.bot, o.part, o.status, o.reason) for o in outcomes] == [
        ("Thief", "roster", "skipped", "store_not_owned"),
        ("Ghost", "roster", "skipped", "unreadable_record"),
    ]


def test_imported_bots_any_other_failure_is_unreadable(home, monkeypatch):
    def boom(*_a):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("kiro_crew.dashboard.handlers.members_thread._member_thread_slot", boom)

    bots, outcomes = roster.imported_bots(home, ["Atlas"])

    assert bots == []
    assert [(o.bot, o.reason) for o in outcomes] == [("Atlas", "unreadable_record")]


def test_imported_bots_never_raises_on_an_unreadable_roster(home, monkeypatch):
    def boom():
        raise RuntimeError("unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(boom))

    bots, outcomes = roster.imported_bots(home, ["Atlas", "Nova"])

    assert bots == []
    assert [(o.bot, o.reason) for o in outcomes] == [
        ("Atlas", "unreadable_record"),
        ("Nova", "unreadable_record"),
    ]


@pytest.mark.parametrize(
    ("archive_entry", "destination_entry", "same"),
    [
        ({"kiro_agent": "Old"}, {"kiro_agent": "Old", "workspace": "default"}, True),
        ({"member_id": "", "memory_store": "m"}, {"memory_store": "m"}, True),
        ({"kiro_agent": "Old"}, {"memory_store": "other"}, False),
        ({"kiro_agent": "Old"}, {"member_id": "old"}, False),
        (
            {"member_id": "old", "memory_store": "m"},
            {"member_id": "old", "memory_store": "m"},
            True,
        ),
        ({"member_id": 3}, {"member_id": 3}, False),
    ],
)
def test_same_bot_matches_a_version_1_bot_on_both_sides(archive_entry, destination_entry, same):
    assert roster.same_bot("Old", archive_entry, {"Old": destination_entry}) is same
    assert roster.same_bot("Old", archive_entry, {"Other": destination_entry}) is False


def test_a_version_1_bot_already_here_is_kept_and_nothing_is_written(home):
    update_config_locked(
        home / "config.json",
        mutate=lambda d: {**d, "agents": {**d["agents"], "Old": {"kiro_agent": "Old"}}},
    )
    before = (home / "config.json").read_bytes()

    result = roster.merge_roster(
        {"agents": {"Old": {"kiro_agent": "Old"}}}, home, stores_added=set()
    )

    assert (result.kept, result.added, result.skipped) == (["Old"], [], [])
    assert (home / "config.json").read_bytes() == before
