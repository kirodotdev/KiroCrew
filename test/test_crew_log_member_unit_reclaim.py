"""Reclaiming the crew log of a member the roster does not hold.

The authorization is the roster and nothing else: a member's unit is keyed by its
slug, so the only safe question is whether a live member still derives that key.
Neither an age nor a size takes part, which is what makes the refusals the weight
of this file -- keeping a deleted member's log costs disk, while removing a LIVE
member's log destroys a history nothing can rebuild.

So the two directions are pinned side by side: a delete collects the unit, and no
shape of this path touches a unit the roster still claims -- a surviving
namesake, a sibling member, a crew whose name the roster grammar rejects, or a
config that cannot be read at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.resolution import reset_degraded_observations
from kiro_crew.crew_log.schema import KIND_MEMBER
from kiro_crew.crew_log.store import REMOVE_OWNED, REMOVE_REMOVED, crew_log_dir
from kiro_crew.dashboard.handlers import agents as agents_mod
from kiro_crew.eventlog import service as service_mod
from kiro_crew.eventlog.types import MEMBER_CONFIG

GONE = "retired"
LIVE = "keeper"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one.

    The loader's degradation observations are cleared on both sides too: they are
    deliberately sticky for the life of a process, so the malformed-config case
    below would otherwise deny every later test in the same interpreter.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    reset_degraded_observations()
    service_mod.set_service(None)
    yield
    service_mod.set_service(None)
    reset_degraded_observations()


def _write_roster(home: Path, **members: dict) -> None:
    """Write ``config.json`` naming *members*, each value that member's record."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"agents": members}), encoding="utf-8")


def _captured() -> KiroCrewConfig:
    """The config as the delete handler captured it -- while the record still existed.

    The slug is resolved from this, so the tests that need a DIFFERENT roster at
    guard time take this first and rewrite ``config.json`` afterwards.
    """
    return KiroCrewConfig.load()


def _seed_log(slug: str, name: str, *, model: str = "m1") -> None:
    """Create *slug*'s member crew log and put one real event in it.

    Through the service, not by writing bytes: the unit has to be the one the
    product writes -- header, projections directory and all -- or a removal that
    left something behind would pass here and fail on a real member.
    """
    svc = service_mod.get_service()
    svc.ensure(slug, name)
    svc.append(slug, MEMBER_CONFIG, {"model": model, "changed": ["model"]})


def _unit(slug: str) -> Path:
    return crew_log_dir(KIND_MEMBER, slug)


# --- the unit is collected once its owner is gone ----------------------------


def test_delete_reclaims_the_departed_member_unit(tmp_path):
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    _seed_log(GONE, GONE)
    assert _unit(GONE).is_dir()

    captured = _captured()
    _write_roster(home)  # the delete committed: the record is gone
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    assert not _unit(GONE).exists()


def test_a_member_with_an_explicit_id_is_reclaimed_by_that_id(tmp_path):
    """The unit is keyed by the persisted ``member_id``, not by the folded name."""
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {"member_id": "mem-7"}})
    _seed_log("mem-7", GONE)
    _seed_log(GONE, "a different member")  # what the NAME alone would fold to

    captured = _captured()
    _write_roster(home)
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    assert not _unit("mem-7").exists()
    # The name fold is a different unit and was never this member's history.
    assert _unit(GONE).is_dir()


def test_reclaim_forgets_the_slug_so_a_later_read_answers_empty(tmp_path):
    """The service must not answer for a member whose files it just removed."""
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    _seed_log(GONE, GONE)
    svc = service_mod.get_service()
    assert svc.snapshot(GONE)["asOfSeq"] >= 0

    captured = _captured()
    _write_roster(home)
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    assert svc.snapshot(GONE) == {"asOfSeq": -1, "values": {}}
    assert svc.last_seq(GONE) == -1


def test_a_member_that_never_wrote_a_log_is_not_an_error(tmp_path):
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    captured = _captured()
    _write_roster(home)

    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)  # must not raise

    assert not _unit(GONE).exists()


# --- a LIVE member's log is never taken by this path -------------------------


def test_a_live_member_is_never_reclaimed(tmp_path):
    """The safety boundary: the roster still holds the name, so nothing goes.

    The handler only reaches this after committing the delete, so a roster that
    still names the member means a same-name record was committed in the window --
    and that record's own unit is THIS one, because the slug is the key.
    """
    home = tmp_path / "home"
    _write_roster(home, **{LIVE: {}})
    _seed_log(LIVE, LIVE)
    before = (_unit(LIVE) / "log.jsonl").read_bytes()

    agents_mod._reclaim_deleted_member_crew_log(LIVE, _captured())

    assert _unit(LIVE).is_dir()
    assert (_unit(LIVE) / "log.jsonl").read_bytes() == before


def test_a_recreated_namesake_keeps_the_unit(tmp_path):
    """Recreation between the commit and the removal is what the re-decision is for."""
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    _seed_log(GONE, GONE)
    captured = _captured()

    # The delete committed, then a same-name member was created before the removal
    # got to decide. It derives the same slug, so this unit is now its history.
    _write_roster(home, **{GONE: {}})
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    assert _unit(GONE).is_dir()
    assert (_unit(GONE) / "log.jsonl").read_text(encoding="utf-8").strip()


def test_deleting_one_member_leaves_its_siblings_untouched(tmp_path):
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}, LIVE: {}})
    _seed_log(GONE, GONE)
    _seed_log(LIVE, LIVE)

    captured = _captured()
    _write_roster(home, **{LIVE: {}})
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    assert not _unit(GONE).exists()
    assert _unit(LIVE).is_dir()


def test_a_live_member_whose_name_the_roster_grammar_rejects_is_still_claimed(tmp_path):
    """Existence is not addressability: an ungrammatical name can be a live crew.

    The create route checks a crew name for credential shape only, so the roster
    view's grammar filter must not decide this -- filtering such a crew out would
    report a live owner as gone and hand its history to the removal.
    """
    home = tmp_path / "home"
    odd = "Crew Member!"
    _write_roster(home, **{odd: {"member_id": "mem-odd"}})
    _seed_log("mem-odd", odd)

    agents_mod._reclaim_deleted_member_crew_log(odd, _captured())

    assert _unit("mem-odd").is_dir()


def test_an_unreadable_roster_removes_nothing(tmp_path):
    """Fails closed: no roster to prove the owner is gone means the log stays."""
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    _seed_log(GONE, GONE)
    captured = _captured()

    (home / "config.json").write_text("{ not json", encoding="utf-8")
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    assert _unit(GONE).is_dir()


# --- the service door's own contract ----------------------------------------


def test_the_guard_refusal_is_reported_as_owned_not_removed(tmp_path):
    """The re-decision's refusal is reported rather than swallowed."""
    _write_roster(tmp_path / "home", **{LIVE: {}})
    _seed_log(LIVE, LIVE)

    status = service_mod.get_service().remove_unit(LIVE, still_unclaimed=lambda: False)

    assert status == REMOVE_OWNED
    assert _unit(LIVE).is_dir()


def test_the_predicate_is_asked_under_the_hold_and_a_true_answer_removes(tmp_path):
    """The same call with a true predicate removes, so the refusal above IS the guard."""
    _write_roster(tmp_path / "home", **{GONE: {}})
    _seed_log(GONE, GONE)
    asked: list[bool] = []

    def still_unclaimed() -> bool:
        # The unit is intact when the guard runs: the removal happens after it.
        asked.append(_unit(GONE).is_dir())
        return True

    status = service_mod.get_service().remove_unit(GONE, still_unclaimed=still_unclaimed)

    assert status == REMOVE_REMOVED
    assert asked == [True]
    assert not _unit(GONE).exists()


# --- the claim predicate itself ---------------------------------------------


@pytest.mark.parametrize(
    "roster, slug, claimed",
    [
        ({LIVE: {}}, LIVE, True),
        ({LIVE: {}}, GONE, False),
        ({}, LIVE, False),
        ({LIVE: {"member_id": "mem-1"}}, "mem-1", True),
        # An explicit id, not the name, is what derives the unit.
        ({LIVE: {"member_id": "mem-1"}}, LIVE, False),
    ],
)
def test_claim_predicate_reads_the_roster_by_persisted_identity(tmp_path, roster, slug, claimed):
    _write_roster(tmp_path / "home", **roster)
    assert agents_mod._member_slug_is_claimed(slug) is claimed
