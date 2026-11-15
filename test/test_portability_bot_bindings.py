"""Bot DM-thread bindings an instance import rebuilds from config (portability_bot_bindings).

Each test's data home is the rootdir conftest's autouse ``_isolate_kirocrew_home``;
archives are built in Python under ``tmp_path`` as an extraction directory holding
one top directory, the shape ``apply_import_zip`` hands its steps.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew import members
from kiro_crew import portability_bot_bindings as bindings
from kiro_crew.config.loader_locations import config_path
from kiro_crew.config.paths import config_dir
from kiro_crew.members import dm_binding_path, read_dm_binding_for_slot
from kiro_crew.portability_bot_types import ImportedBot
from kiro_crew.security import is_sensitive_path
from kiro_crew.staging import staging_dir

STORE = "atlas-mem"
SLOT = f"member-atlas.memory-{STORE}"
STEM = f"dashboard_{SLOT}"
ATLAS = ImportedBot(
    name="Atlas",
    slug="atlas",
    member_id="atlas",
    memory_store=STORE,
    generation=STORE,
    slot_key=SLOT,
    kiro_agent="Atlas",
)
OLD = ImportedBot(
    name="Old",
    slug="old",
    member_id="",
    memory_store="default",
    generation="",
    slot_key="member-old",
    kiro_agent="Old",
)


def _write_config(agents: dict | None = None) -> None:
    doc = {
        "agents": (
            agents
            if agents is not None
            else {"Atlas": {"member_id": "atlas", "memory_store": STORE}, "Old": {}}
        ),
        "memory_stores": {STORE: {"memory_version": 2, "owner_member_id": "atlas"}},
    }
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _snap(tmp_path: Path) -> Path:
    snap = tmp_path / "extract" / "kirocrew-export"
    snap.mkdir(parents=True)
    return snap


def _claims_dir(snap: Path) -> Path:
    path = snap / "member-records" / "member-bindings"
    path.mkdir(parents=True)
    return path


def _derived_claim() -> dict:
    return {
        "member_id": "atlas",
        "member": "Atlas",
        "slug": "atlas",
        "slot_key": SLOT,
        "memory_store": STORE,
        "created_ts": "2026-01-01T00:00:00Z",
    }


def _symlink_or_skip(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


def _rebuild(
    bot: ImportedBot = ATLAS, claims: dict | None = None, installed=(STEM,), owned_slugs=None
):
    [outcome] = bindings.rebuild_bindings(
        config_dir(), [bot], claims or {}, set(installed), owned_slugs=owned_slugs
    )
    assert outcome.bot == bot.name and outcome.part == "binding"
    return outcome


def test_member_stem_is_the_transcript_stem_of_the_slot_alias():
    assert bindings.member_stem(SLOT) == STEM
    assert bindings.member_stem("member-old") == "dashboard_member-old"
    assert bindings.is_member_stem(STEM)
    assert not bindings.is_member_stem("dashboard_chat-1")


def test_rebuilds_the_binding_from_config():
    _write_config()
    outcome = _rebuild()
    assert (outcome.status, outcome.reason) == ("rebuilt", "")
    binding = read_dm_binding_for_slot(SLOT)
    assert binding is not None
    assert {k: binding[k] for k in ("member_id", "member", "slug", "slot_key", "memory_store")} == {
        "member_id": "atlas",
        "member": "Atlas",
        "slug": "atlas",
        "slot_key": SLOT,
        "memory_store": STORE,
    }


def test_a_matching_archive_claim_does_not_block_the_rebuild():
    _write_config()
    assert _rebuild(claims={"atlas": _derived_claim()}).status == "rebuilt"


@pytest.mark.parametrize(
    "field, value",
    [
        ("member", "Other"),
        ("member_id", "other"),
        ("memory_store", "other-store"),
        ("slot_key", "member-atlas"),
        ("slug", "other"),
    ],
)
def test_a_forged_claim_vetoes_the_rebuild(field, value):
    _write_config()
    claim = _derived_claim()
    claim[field] = value
    outcome = _rebuild(claims={"atlas": claim})
    assert (outcome.status, outcome.reason) == ("not_rebuilt", "archive_binding_disagrees")
    assert not dm_binding_path("atlas").exists()


def test_an_unreadable_claim_vetoes_and_never_authorises():
    _write_config()
    outcome = _rebuild(claims={"atlas": None})
    assert (outcome.status, outcome.reason) == ("not_rebuilt", "archive_binding_disagrees")
    assert not dm_binding_path("atlas").exists()


def test_a_bot_without_member_id_gets_no_binding():
    _write_config()
    outcome = _rebuild(OLD, installed=("dashboard_member-old",))
    assert (outcome.status, outcome.reason) == ("not_rebuilt", "no_member_id")
    assert not dm_binding_path("old").exists()


def test_a_duplicate_member_id_gets_no_binding():
    _write_config(
        {
            "Atlas": {"member_id": "atlas", "memory_store": STORE},
            "Twin": {"member_id": "atlas"},
        }
    )
    outcome = _rebuild()
    assert (outcome.status, outcome.reason) == ("not_rebuilt", "member_id_ambiguous")
    assert not dm_binding_path("atlas").exists()


def test_a_colliding_slug_gets_no_binding():
    _write_config({"Atlas": {"member_id": "atlas", "memory_store": STORE}, "Atlas!": {}})
    outcome = _rebuild()
    assert (outcome.status, outcome.reason) == ("not_rebuilt", "slug_collision")
    assert not dm_binding_path("atlas").exists()


def test_a_transcript_already_on_the_destination_gets_no_binding():
    _write_config()
    sessions = config_dir() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{STEM}.jsonl").write_text("{}\n", encoding="utf-8")
    outcome = _rebuild(installed=(), owned_slugs=frozenset())
    assert (outcome.status, outcome.reason) == ("not_rebuilt", "transcript_predates_import")
    assert not dm_binding_path("atlas").exists()


def test_an_owned_slug_rebuilds_the_lost_binding_of_its_own_transcript():
    """The Bot's own thread at its own slot, whose binding was lost, gets it back."""
    _write_config()
    sessions = config_dir() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{STEM}.jsonl").write_text("{}\n", encoding="utf-8")
    outcome = _rebuild(installed=(), owned_slugs=frozenset({"atlas"}))
    assert (outcome.status, outcome.reason) == ("rebuilt", "")
    binding = read_dm_binding_for_slot(SLOT)
    assert binding is not None and binding["member"] == "Atlas"


def test_an_owned_slug_still_runs_the_checks_before_a_rebuild():
    _write_config({"Atlas": {"member_id": "atlas", "memory_store": STORE}, "Atlas!": {}})
    sessions = config_dir() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{STEM}.jsonl").write_text("{}\n", encoding="utf-8")
    outcome = _rebuild(installed=(), owned_slugs=frozenset({"atlas"}))
    assert (outcome.status, outcome.reason) == ("not_rebuilt", "slug_collision")
    assert not dm_binding_path("atlas").exists()


def test_a_bot_with_no_transcript_needs_no_binding():
    _write_config()
    outcome = _rebuild(installed=())
    assert (outcome.status, outcome.reason) == ("not_needed", "")
    assert not dm_binding_path("atlas").exists()


def test_an_existing_unusable_binding_is_left_byte_for_byte():
    _write_config()
    path = dm_binding_path("atlas")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = b'{"member": "Someone", "slot_key": "member-atlas", "slug": "atlas"}'
    path.write_bytes(original)
    outcome = _rebuild()
    assert (outcome.status, outcome.reason) == ("not_rebuilt", "binding_exists_unusable")
    assert path.read_bytes() == original


def test_an_existing_binding_naming_the_bot_is_kept_unchanged():
    _write_config()
    assert _rebuild().status == "rebuilt"
    path = dm_binding_path("atlas")
    before = path.read_bytes()
    outcome = _rebuild()
    assert (outcome.status, outcome.reason) == ("kept", "")
    assert path.read_bytes() == before


@pytest.mark.parametrize("owned", [frozenset(), frozenset({"other"})])
def test_an_existing_binding_of_a_bot_that_was_not_here_is_never_adopted(owned):
    # The destination held this slug for another Bot before the import (A12): its
    # binding is reported, never kept as this Bot's, whatever it names.
    _write_config()
    assert _rebuild().status == "rebuilt"
    path = dm_binding_path("atlas")
    before = path.read_bytes()
    [outcome] = bindings.rebuild_bindings(config_dir(), [ATLAS], {}, {STEM}, owned_slugs=owned)
    assert (outcome.status, outcome.reason) == ("not_rebuilt", "held_by_other_bot")
    assert path.read_bytes() == before


def test_an_existing_binding_naming_the_bot_is_kept_over_a_predating_transcript():
    # A restore onto the install that made the archive: its transcript and its
    # binding are both already here, and the Bot is whole (A8).
    _write_config()
    assert _rebuild().status == "rebuilt"
    sessions = config_dir() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{STEM}.jsonl").write_text("{}\n", encoding="utf-8")
    before = dm_binding_path("atlas").read_bytes()

    outcome = _rebuild(installed=())

    assert (outcome.status, outcome.reason) == ("kept", "")
    assert dm_binding_path("atlas").read_bytes() == before


def test_an_existing_binding_of_another_generation_is_unusable_with_no_transcript():
    _write_config()
    path = dm_binding_path("atlas")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps(
        {"member": "Atlas", "slot_key": "member-atlas", "slug": "atlas", "member_id": "atlas"}
    ).encode()
    path.write_bytes(original)

    outcome = _rebuild(installed=())

    assert (outcome.status, outcome.reason) == ("not_rebuilt", "binding_exists_unusable")
    assert path.read_bytes() == original


def test_a_binding_that_does_not_resolve_is_removed_again(monkeypatch):
    _write_config()
    monkeypatch.setattr(members, "read_dm_binding_for_slot", lambda _key: None)
    outcome = _rebuild()
    assert (outcome.status, outcome.reason) == ("not_rebuilt", "unreadable_record")
    assert not dm_binding_path("atlas").exists()


def test_a_failing_write_is_reported_per_bot(monkeypatch):
    _write_config()

    def refuse(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(members, "write_dm_binding", refuse)
    outcome = _rebuild()
    assert (outcome.status, outcome.reason) == ("not_rebuilt", "write_failed")


def test_an_unexpected_error_for_one_bot_does_not_stop_the_next(monkeypatch):
    real = bindings._derived_slot

    def explode_for_old(config, bot):
        if bot.name == "Old":
            raise RuntimeError("boom")
        return real(config, bot)

    monkeypatch.setattr(bindings, "_derived_slot", explode_for_old)
    old_v2 = ImportedBot("Old", "old", "old", STORE, "", "member-old", "Old")
    _write_config(
        {
            "Atlas": {"member_id": "atlas", "memory_store": STORE},
            "Old": {"member_id": "old"},
        }
    )
    outcomes = bindings.rebuild_bindings(
        config_dir(), [old_v2, ATLAS], {}, {STEM, "dashboard_member-old"}
    )
    assert [(o.bot, o.status, o.reason) for o in outcomes] == [
        ("Old", "not_rebuilt", "write_failed"),
        ("Atlas", "rebuilt", ""),
    ]


def test_an_unreadable_config_writes_nothing(monkeypatch):
    from kiro_crew.config import loader

    def unreadable():
        raise ValueError("bad config")

    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(unreadable))
    outcome = _rebuild()
    assert (outcome.status, outcome.reason) == ("not_rebuilt", "unreadable_record")


# -- reading the archive's claims -------------------------------------------------


def test_claims_read_objects_by_slug(tmp_path):
    snap = _snap(tmp_path)
    claims = _claims_dir(snap)
    (claims / "atlas.json").write_text(json.dumps(_derived_claim()), encoding="utf-8")
    (claims / "list.json").write_text("[1, 2]", encoding="utf-8")
    (claims / "garbage.json").write_bytes(b"\xff\xfe not json")
    (claims / "notes.txt").write_text("{}", encoding="utf-8")
    (claims / "Bad.Slug.json").write_text("{}", encoding="utf-8")
    got = bindings.read_archive_binding_claims(snap)
    assert got == {"atlas": _derived_claim(), "list": None, "garbage": None}


def test_an_archive_without_bindings_has_no_claims(tmp_path):
    assert bindings.read_archive_binding_claims(_snap(tmp_path)) == {}


def test_an_oversize_claim_reads_as_none(tmp_path):
    snap = _snap(tmp_path)
    claims = _claims_dir(snap)
    padded = dict(_derived_claim(), pad="x" * bindings.MAX_CLAIM_BYTES)
    (claims / "atlas.json").write_text(json.dumps(padded), encoding="utf-8")
    assert bindings.read_archive_binding_claims(snap) == {"atlas": None}


def test_a_linked_claim_reads_as_none_and_its_target_is_not_read(tmp_path):
    snap = _snap(tmp_path)
    claims = _claims_dir(snap)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_derived_claim()), encoding="utf-8")
    _symlink_or_skip(outside, claims / "atlas.json")
    assert bindings.read_archive_binding_claims(snap) == {"atlas": None}


def test_a_linked_bindings_directory_is_not_followed(tmp_path):
    snap = _snap(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "atlas.json").write_text(json.dumps(_derived_claim()), encoding="utf-8")
    (snap / "member-records").mkdir()
    _symlink_or_skip(elsewhere, snap / "member-records" / "member-bindings")
    assert bindings.read_archive_binding_claims(snap) == {}


def test_claims_stop_at_the_file_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(bindings, "MAX_CLAIM_FILES", 2)
    snap = _snap(tmp_path)
    claims = _claims_dir(snap)
    for slug in ("a", "b", "c"):
        (claims / f"{slug}.json").write_text("{}", encoding="utf-8")
    assert bindings.read_archive_binding_claims(snap) == {"a": {}, "b": {}}


def test_a_linked_claim_vetoes_the_rebuild_end_to_end(tmp_path):
    _write_config()
    snap = _snap(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_derived_claim()), encoding="utf-8")
    _symlink_or_skip(outside, _claims_dir(snap) / "atlas.json")
    outcome = _rebuild(claims=bindings.read_archive_binding_claims(snap))
    assert (outcome.status, outcome.reason) == ("not_rebuilt", "archive_binding_disagrees")


# -- orphan transcripts -----------------------------------------------------------


def test_orphans_are_member_transcripts_matching_no_imported_slot(tmp_path):
    _write_config()
    from kiro_crew.config.loader import KiroCrewConfig

    snap = _snap(tmp_path)
    sessions = snap / "sessions"
    sessions.mkdir()
    for stem in (
        STEM,  # the imported Bot's own slot: installed, not an orphan
        "dashboard_member-atlas.memory-older-gen",  # another generation of Atlas
        "dashboard_member-ghost",  # no Bot on the destination
        "dashboard_chat-1",  # an ordinary chat
    ):
        (sessions / f"{stem}.jsonl").write_text("{}\n", encoding="utf-8")
    _symlink_or_skip(sessions / f"{STEM}.jsonl", sessions / "dashboard_member-linked.jsonl")
    rows = bindings.orphan_transcripts(snap, [ATLAS], KiroCrewConfig.load())
    assert rows == [
        {"slug": "atlas", "generation": "older-gen", "candidates": ["Atlas"]},
        {"slug": "ghost", "generation": "", "candidates": []},
    ]


def test_a_transcript_the_destination_holds_is_no_orphan(tmp_path):
    snap = _snap(tmp_path)
    (snap / "sessions").mkdir()
    for stem in ("dashboard_member-ghost", "dashboard_member-here"):
        (snap / "sessions" / f"{stem}.jsonl").write_text("{}\n", encoding="utf-8")
    mc = tmp_path / "dest"
    (mc / "sessions").mkdir(parents=True)
    (mc / "sessions" / "dashboard_member-here.jsonl").write_text("{}\n", encoding="utf-8")
    rows = bindings.orphan_transcripts(snap, [], None, mc=mc)
    assert [row["slug"] for row in rows] == ["ghost"]


def test_without_imported_bots_every_member_transcript_is_an_orphan(tmp_path):
    snap = _snap(tmp_path)
    (snap / "sessions").mkdir()
    (snap / "sessions" / f"{STEM}.jsonl").write_text("{}\n", encoding="utf-8")
    assert bindings.orphan_transcripts(snap, [], None) == [
        {"slug": "atlas", "generation": STORE, "candidates": []}
    ]


def test_an_archive_without_sessions_has_no_orphans(tmp_path):
    assert bindings.orphan_transcripts(_snap(tmp_path), [ATLAS], None) == []


# -- the one verdict, judged before any transcript is installed (A36) -------------


def _verdict(bot: ImportedBot, claims=None, *, config=None, owned_slugs=None):
    from kiro_crew.config.loader import KiroCrewConfig

    config = KiroCrewConfig.load() if config is None else config
    return bindings.binding_ladder(
        config_dir(), bot, {} if claims is None else claims, config, owned_slugs=owned_slugs
    )[0]


def _status(outcome) -> tuple[str, str]:
    return outcome.status, outcome.reason


def test_verdict_is_the_rebuild_ladder_without_a_write():
    _write_config()
    assert _status(_verdict(OLD)) == ("not_rebuilt", "no_member_id")
    assert _status(_verdict(ATLAS)) == (bindings.REBUILD, "")
    # Judging wrote nothing; the rebuild then reports the same answers.
    assert not dm_binding_path("atlas").exists()
    assert _rebuild(OLD, installed=("dashboard_member-old",)).reason == "no_member_id"
    assert _rebuild().status == "rebuilt"
    assert _status(_verdict(ATLAS, owned_slugs=frozenset())) == ("not_rebuilt", "held_by_other_bot")
    assert _status(_verdict(ATLAS, owned_slugs={"atlas"})) == ("kept", "")


def test_verdict_vetoes_on_a_disagreeing_or_unreadable_claim_set():
    _write_config()
    from kiro_crew.config.loader import KiroCrewConfig

    config = KiroCrewConfig.load()
    claim = dict(_derived_claim(), member="Mallory")
    assert _status(_verdict(ATLAS, {"atlas": claim})) == (
        "not_rebuilt",
        "archive_binding_disagrees",
    )
    unread, _gen = bindings.binding_ladder(config_dir(), ATLAS, None, config)
    assert _status(unread) == ("not_rebuilt", "unreadable_record")
    [outcome] = bindings.rebuild_bindings(config_dir(), [ATLAS], None, {STEM}, config=config)
    assert _status(outcome) == ("not_rebuilt", "unreadable_record")
    assert not dm_binding_path("atlas").exists()
    # An unread claim set only vetoes a write: a binding already here is still kept.
    assert _rebuild().status == "rebuilt"
    [outcome] = bindings.rebuild_bindings(config_dir(), [ATLAS], None, {STEM}, config=config)
    assert _status(outcome) == ("kept", "")


def test_verdict_with_an_unread_config_or_a_held_transcript():
    _write_config()
    unread, _gen = bindings.binding_ladder(config_dir(), ATLAS, {}, None)
    assert _status(unread) == ("not_rebuilt", "unreadable_record")
    # The destination already holds the thread, on a slug it did not own: the
    # chat merge would not install it, so the verdict is the one for a held thread.
    sessions = config_dir() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{STEM}.jsonl").write_text("{}\n", encoding="utf-8")
    assert _status(_verdict(ATLAS, owned_slugs=frozenset())) == (
        "not_rebuilt",
        "transcript_predates_import",
    )
    assert _status(_verdict(ATLAS, owned_slugs={"atlas"})) == (bindings.REBUILD, "")


@pytest.mark.parametrize("owned", [frozenset(), frozenset({"other"})])
def test_verdict_names_another_bots_binding_on_a_slug_not_owned(owned):
    _write_config()
    path = dm_binding_path("atlas")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"member": "Someone", "slot_key": "member-atlas", "slug": "atlas"}')
    assert _status(_verdict(ATLAS, owned_slugs=owned)) == ("not_rebuilt", "held_by_other_bot")
    assert _status(_verdict(ATLAS, owned_slugs={"atlas"})) == (
        "not_rebuilt",
        "binding_exists_unusable",
    )
    [outcome] = bindings.rebuild_bindings(config_dir(), [ATLAS], {}, {STEM}, owned_slugs=owned)
    assert _status(outcome) == ("not_rebuilt", "held_by_other_bot")


def test_shared_sessions_listing_feeds_stems_and_orphans(tmp_path):
    snap = _snap(tmp_path)
    sessions = snap / "sessions"
    sessions.mkdir()
    (sessions / "dashboard_member-ghost.jsonl").write_text("{}\n", encoding="utf-8")
    listing = bindings.sessions_listing(snap)
    assert set(listing) == {"dashboard_member-ghost.jsonl"}
    # The steps read the listing they are given, not the tree again.
    (sessions / "dashboard_member-late.jsonl").write_text("{}\n", encoding="utf-8")
    assert bindings.archive_member_stems(snap, listing=listing) == frozenset(
        {"dashboard_member-ghost"}
    )
    rows = bindings.orphan_transcripts(snap, [], None, listing=listing)
    assert [row["slug"] for row in rows] == ["ghost"]
    assert bindings.sessions_listing(_snap(tmp_path / "empty")) == {}


def test_rebuild_bindings_uses_the_config_it_is_given():
    _write_config()
    [outcome] = bindings.rebuild_bindings(config_dir(), [ATLAS], {}, {STEM}, config=None)
    assert (outcome.status, outcome.reason) == ("not_rebuilt", "unreadable_record")


def test_archive_member_stems_lists_only_member_thread_files(tmp_path):
    snap = _snap(tmp_path)
    sessions = snap / "sessions"
    sessions.mkdir()
    for stem in (STEM, "dashboard_chat-1"):
        (sessions / f"{stem}.jsonl").write_text("{}\n", encoding="utf-8")
    (sessions / "dashboard_member-dir.jsonl").mkdir()
    assert bindings.archive_member_stems(snap) == frozenset({STEM})
    assert bindings.archive_member_stems(_snap(tmp_path / "empty")) == frozenset()


# -- the sensitive-path answers this change relies on, unchanged ------------------

_PAGE = "crew-panels/x.dashboard-page." + "0" * 32 + ".json"


@pytest.mark.parametrize(
    "rel, sensitive",
    [
        ("member-records/member-bindings/x.json", True),
        (_PAGE, True),
        ("members/x/dashboard/instance.json", False),
        ("artifacts/x-dashboard/current.html", False),
    ],
)
def test_sensitive_path_answers_at_the_data_home(rel, sensitive):
    assert is_sensitive_path(str(config_dir() / rel)) is sensitive


@pytest.mark.parametrize(
    "rel",
    [
        "member-records/member-bindings/x.json",
        _PAGE,
        "members/x/dashboard/instance.json",
        "artifacts/x-dashboard/current.html",
    ],
)
def test_sensitive_path_answers_under_the_neutral_staging_path(rel):
    # The replace strip judges extracted files at ``staging_dir() / snap.name``.
    # The member-records and crew-panels fences are data-home leaves, so none of
    # these is flagged there: the import keeps those archive trees out by never
    # copying them, not by the strip.
    assert is_sensitive_path(str(staging_dir() / "kirocrew-export" / rel)) is False


def test_a_reserved_bots_thread_is_never_listed_as_orphaned(tmp_path):
    snap = _snap(tmp_path)
    (snap / "sessions").mkdir()
    (snap / "sessions" / "dashboard_member-mate.memory-m1.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )
    (snap / "sessions" / f"{STEM}.jsonl").write_text("{}\n", encoding="utf-8")
    assert bindings.stem_slug("dashboard_member-mate.memory-m1") == "mate"
    assert bindings.stem_slug("dashboard_chat-1") == ""
    rows = bindings.orphan_transcripts(snap, [], None, passthrough_slugs={"mate"})
    assert [r["slug"] for r in rows] == ["atlas"]


@pytest.mark.parametrize("names_the_bot", [True, False])
def test_a_binding_that_appears_after_a_reused_verdict_is_never_written_over(names_the_bot):
    # The verdict was judged before the chat merge, when no binding file was there;
    # one that appeared since is kept as it is, never overwritten by the rebuild.
    from kiro_crew.config.loader import KiroCrewConfig

    _write_config()
    config = KiroCrewConfig.load()
    judged = bindings.binding_ladder(config_dir(), ATLAS, {}, config)
    assert judged[0].status == bindings.REBUILD
    path = dm_binding_path("atlas")
    if names_the_bot:
        members.write_dm_binding("atlas", member="Atlas", slot_key=SLOT, memory_store=STORE)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"member": "Someone", "slot_key": "member-atlas", "slug": "atlas"}')
    before = path.read_bytes()
    [outcome] = bindings.rebuild_bindings(
        config_dir(), [ATLAS], {}, {STEM}, config=config, verdicts={"Atlas": judged}
    )
    expected = ("kept", "") if names_the_bot else ("not_rebuilt", "binding_exists_unusable")
    assert (outcome.status, outcome.reason) == expected
    assert path.read_bytes() == before


def test_a_reused_rebuild_verdict_whose_thread_was_not_installed_is_not_needed():
    # Judged before the chat merge as if the merge installs the thread; it did not
    # (the stem is not in installed_stems) and none is here: no binding is written.
    from kiro_crew.config.loader import KiroCrewConfig

    _write_config()
    config = KiroCrewConfig.load()
    judged = bindings.binding_ladder(config_dir(), ATLAS, {}, config, owned_slugs=frozenset())
    assert judged[0].status == bindings.REBUILD
    [outcome] = bindings.rebuild_bindings(
        config_dir(),
        [ATLAS],
        {},
        frozenset(),
        owned_slugs=frozenset(),
        config=config,
        verdicts={"Atlas": judged},
    )
    assert (outcome.status, outcome.reason) == ("not_needed", "")
    assert not os.path.lexists(dm_binding_path("atlas"))
    # The same verdict with the thread installed writes the binding.
    [outcome] = bindings.rebuild_bindings(
        config_dir(),
        [ATLAS],
        {},
        {STEM},
        owned_slugs=frozenset(),
        config=config,
        verdicts={"Atlas": judged},
    )
    assert outcome.status == bindings.REBUILD


def test_every_reused_verdict_whose_thread_did_not_land_is_not_needed():
    # An owned V1 Bot (no member_id) is refused before the chat merge, judged as if
    # the merge installs its thread; the merge refused that thread and none is here,
    # so there is no thread to bind and nothing was refused.
    from kiro_crew.config.loader import KiroCrewConfig

    _write_config()
    config = KiroCrewConfig.load()
    owned = frozenset({"old"})
    judged = bindings.binding_ladder(config_dir(), OLD, {}, config, owned_slugs=owned)
    assert judged[0].status == "not_rebuilt" and judged[0].reason
    [outcome] = bindings.rebuild_bindings(
        config_dir(),
        [OLD],
        {},
        frozenset(),
        owned_slugs=owned,
        config=config,
        verdicts={"Old": judged},
    )
    assert (outcome.status, outcome.reason) == ("not_needed", "")
    assert not os.path.lexists(dm_binding_path("old"))


def test_a_binding_here_still_decides_a_reused_verdict_whose_thread_did_not_land():
    # Judged again after the merge, the ladder's first step still decides: a binding
    # naming this Bot is kept, even though its thread did not land.
    from kiro_crew.config.loader import KiroCrewConfig

    _write_config()
    config = KiroCrewConfig.load()
    judged = bindings.binding_ladder(config_dir(), ATLAS, {}, config)
    assert judged[0].status == bindings.REBUILD
    members.write_dm_binding("atlas", member="Atlas", slot_key=SLOT, memory_store=STORE)
    [outcome] = bindings.rebuild_bindings(
        config_dir(), [ATLAS], {}, frozenset(), config=config, verdicts={"Atlas": judged}
    )
    assert (outcome.status, outcome.reason) == ("kept", "")
