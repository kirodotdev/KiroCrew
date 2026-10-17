"""The dashboard instance's history IS a crew-log fold, proved against a real log.

The sibling suite (``test_dashboard_instance_registry``) runs with no session, which is
the degraded path: the savepoint carries the rows and no entry is appended. This file is
the other half -- the crew log on, a real session log open, and the fold rebuilt from the
entries rather than read from the savepoint -- because "the history is a projection of
the log" is a claim about the log and cannot be tested without one.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.dashboard_templates import catalog, instance

SESSION = "acp-dash-1"
SLOT = "member-fleet"
SLUG = "fleet"

PAGE = '<div><b data-dashboard-field="credits"></b><i data-dashboard-field="phase"></i></div>'
PAGE_EDITED = '<p><b data-dashboard-field="credits"></b><i data-dashboard-field="phase"></i></p>'


def _manifest():
    return {
        "id": "fixture-board",
        "version": 1,
        "title": "Fixture board",
        "description": "A template this test owns.",
        "source": "builtin",
        "fields": {
            "credits": {"type": "number", "source": {"fold": "usage", "path": "credits"}},
            "phase": {"type": "string", "source": {"agentic": True}},
        },
    }


def _retitled():
    """The same manifest under a new title: a MANIFEST-only edit.

    An html edit is refused -- the record carries the page and the `source` label
    together, so replacing one while keeping the other makes the record vouch for
    bytes nobody reviewed. The FIELDS are untouched, so the stored page still
    satisfies the parity rule and the edit is accepted.
    """
    return {**_manifest(), "title": "Fixture board, retitled"}


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    """Own data home, crew log ON, one fixture built-in, no writer state carried over.

    Patches through ``_floor_monkeypatch`` rather than the shared ``monkeypatch``
    (D11): an isolation patch on the test-owned undo stack is lifted by any test body
    that calls ``monkeypatch.undo()``, which would hand the rest of that test the real
    data home and the real registry.
    """
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    builtin = tmp_path / "builtin" / "fixture-board"
    builtin.mkdir(parents=True)
    (builtin / "manifest.json").write_text(json.dumps(_manifest()), encoding="utf-8")
    (builtin / "template.html").write_text(PAGE, encoding="utf-8")
    _floor_monkeypatch.setattr(catalog, "builtin_dir", lambda: builtin.parent)
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()
    yield
    crew_log_emit.drain_for_shutdown(timeout=2.0)
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()


def _open_log() -> None:
    """Create the session's crew log, then drop the handle.

    The emitter opens its own handle when the store appends, and a handle this test
    kept would own the write lease it needs -- the pattern the ledger and projection
    suites' fixtures established.
    """
    CrewLog.create(lg.KIND_SESSION, SESSION, owner="owner", agent="kirocrew", slot=SLOT)


def _entries() -> list[dict]:
    """Every ``dashboard/instance_changed`` entry in the session's log, in order."""
    handle = crew_log.open_session_log(SESSION)
    assert handle is not None, "the session's crew log is not readable"
    return [entry.data for entry in handle.iter_from(1) if entry.type == instance.ENTRY_TYPE]


def test_every_change_appends_exactly_one_entry_to_the_session_log():
    _open_log()
    instance.adopt(SLUG, "fixture-board", session_id=SESSION)
    instance.edit(SLUG, manifest=_retitled(), session_id=SESSION)
    instance.rollback(SLUG, 1, session_id=SESSION)
    assert crew_log_emit.flush(timeout=5.0), "the crew log writer did not drain"
    rows = _entries()
    assert [r["action"] for r in rows] == ["adopted", "edited", "rolled_back"]
    assert [r["instance_version"] for r in rows] == [1, 2, 3]
    assert {r["slug"] for r in rows} == {SLUG}


def test_an_entry_carries_what_changed_and_never_the_page():
    _open_log()
    instance.adopt(SLUG, "fixture-board", session_id=SESSION)
    assert crew_log_emit.flush(timeout=5.0)
    (row,) = _entries()
    # The whole reason the entry is bounded by construction: it describes the change,
    # so it can never be refused for size however large the page is.
    assert "html" not in row and "manifest" not in row
    assert row["html_bytes"] == len(PAGE.encode("utf-8"))
    assert row["template_id"] == "fixture-board" and row["template_version"] == 1


def test_a_refused_change_appends_nothing():
    _open_log()
    instance.adopt(SLUG, "fixture-board", session_id=SESSION)
    with pytest.raises(instance.InstanceRefused):
        instance.edit(
            SLUG,
            manifest={
                **_manifest(),
                "fields": {
                    **_manifest()["fields"],
                    "ghost": {"type": "string", "source": {"agentic": True}},
                },
            },
            session_id=SESSION,
        )
    assert crew_log_emit.flush(timeout=5.0)
    assert len(_entries()) == 1


def test_the_history_rebuilds_from_the_log_when_its_savepoint_is_lost():
    _open_log()
    instance.adopt(SLUG, "fixture-board", session_id=SESSION)
    instance.edit(SLUG, manifest=_retitled(), session_id=SESSION)
    assert crew_log_emit.flush(timeout=5.0)
    # The savepoint is a CACHE of the log. Deleting it must cost nothing, which is the
    # property that makes the log the authority rather than a second copy beside it.
    (instance.instance_dir(SLUG) / "history.json").unlink()
    rows = instance.history(SLUG, session_id=SESSION)
    assert [r["action"] for r in rows] == ["adopted", "edited"]
    assert [r["instance_version"] for r in rows] == [1, 2]


def test_a_rebuild_reads_only_this_crewmates_entries():
    _open_log()
    instance.adopt(SLUG, "fixture-board", session_id=SESSION)
    instance.edit(SLUG, manifest=_retitled(), session_id=SESSION)
    instance.adopt("scout", "fixture-board", session_id=SESSION)
    assert crew_log_emit.flush(timeout=5.0)
    # Three entries in ONE log, two of them this crewmate's.
    assert [r["slug"] for r in _entries()] == [SLUG, SLUG, "scout"]
    for slug in (SLUG, "scout"):
        (instance.instance_dir(slug) / "history.json").unlink()
    # The slug on the ENTRY is what keeps one crewmate's history out of the other's.
    # The folded ROW does not restate it: the row is read through a per-slug savepoint,
    # so the slug is the key rather than a field, and a row carrying it would be a
    # second place for the two to disagree.
    assert [r["action"] for r in instance.history(SLUG, session_id=SESSION)] == [
        "adopted",
        "edited",
    ]
    assert [r["action"] for r in instance.history("scout", session_id=SESSION)] == ["adopted"]


def test_a_change_with_the_crew_log_off_still_lands(monkeypatch):
    monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
    crew_log_emit.reset_caches()
    record = instance.adopt(SLUG, "fixture-board", session_id=SESSION)
    # The record is the FILE; the entry is history. So the emitter being off costs the
    # log row and not the dashboard -- the same posture the panel publish takes.
    assert record.instance_version == 1 and record.state == instance.STATE_LIVE
    assert [r["action"] for r in instance.history(SLUG)] == ["adopted"]


def test_a_change_with_no_session_lands_and_keeps_its_history_locally():
    instance.adopt(SLUG, "fixture-board", session_id="")
    assert instance.read(SLUG).instance_version == 1
    assert [r["action"] for r in instance.history(SLUG)] == ["adopted"]
