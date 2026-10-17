"""The crew, live-unit and ledger-write helpers the Issue Radar crew store tests share.

``test_issue_radar_crew_store.py`` and its ``_fold_bounds``, ``_progress_lines``,
``_skip_index``, ``_legacy_carry``, ``_write_refusal``, ``_unit_order`` and
``_non_finite`` siblings import what they use from here by name: ``OWNER`` and
``REPO``, the repository every test records under; ``_crew`` and ``_live_crew``, a crew
record and the crew log unit its slot runs on, which ``_unit`` creates; ``_record``,
``_item``, ``_sweep`` and ``_pass``, which write one ledger update, a crew-level sweep
or a pass the way the write route does; ``_entries``, the ``radar/recorded`` entries a
unit holds; and ``_tick``, which lets the writer's millisecond clock move so two stamps
can differ. ``_isolated_crew_log`` is the autouse fixture every one of those files
imports: each test gets its own crew log data home with the crew log on, and no writer
state is carried between tests.
"""

import itertools
import time

import pytest

from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log_projection
from kiro_crew.crew_log.schema import KIND_SESSION
from kiro_crew.crew_log.store import CrewLog

OWNER, REPO = "kirodotdev", "KiroCrew"  # brand-ok: the repository name

_SEQ = itertools.count(1)


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture, moved verbatim; no test in the modules that import it calls monkeypatch.undo()
def _isolated_crew_log(tmp_path, monkeypatch):
    """Own crew log data home, crew log on, and no writer state carried between tests."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(crew_log_emit.CREW_LOG_ENV, "1")
    crew_log_emit.reset_caches()
    crew_log_projection.forget_slot_folds()
    cs._undrained.clear()
    yield
    crew_log_emit.drain_for_shutdown(timeout=2.0)
    crew_log_emit.reset_caches()
    crew_log_projection.forget_slot_folds()
    cs._undrained.clear()


def _crew(root, name="Andromeda", **spec):
    return cs.create_crew(OWNER, REPO, {"name": name, **spec}, root)


def _unit(crew_id, session_id=None):
    """Create the crew log unit *crew_id*'s slot is serving on; answer its id.

    The handle is dropped: the emitter opens its own when the ledger appends, and a
    handle this test kept would own the write lease it needs.
    """
    session_id = session_id or f"acp-{next(_SEQ)}"
    CrewLog.create(
        KIND_SESSION, session_id, owner="owner", agent="kirocrew", slot=cs.slot_key_for(crew_id)
    )
    return session_id


def _live_crew(root, name="Andromeda", **spec):
    """A crew and the id of the live unit its slot runs on."""
    crew = _crew(root, name, **spec)
    return crew, _unit(crew["id"])


def _record(root, cid, sid, number, patch, kind="implement", text="progress", **kw):
    return cs.commit_work_progress(
        OWNER, REPO, cid, number, patch, kind, text, root=root, session_id=sid, **kw
    )


def _item(root, cid, sid, number, patch, kind="implement", text="progress"):
    return _record(root, cid, sid, number, patch, kind, text)["item"]


def _sweep(root, cid, sid, text="queue empty"):
    return cs.record_crew_checkpoint(OWNER, REPO, cid, text, root, session_id=sid)


def _entries(session_id):
    """The ``radar/recorded`` entries *session_id*'s crew log holds, oldest first."""
    handle = CrewLog.open(KIND_SESSION, session_id)
    try:
        return [e for e in handle.iter_from(1) if e.type == cs.LEDGER_ENTRY_TYPE]
    finally:
        del handle


def _tick():
    """Let the writer's millisecond clock move, so two stamps can differ.

    Without this the stamp assertions are TOOTHLESS: two appends inside one
    millisecond carry an equal stamp whether the code guards it or not, and a "did
    not renew" test would pass with the guard deleted.
    """
    # flake-ok: pre-existing 4 ms sleep that lets the writer's millisecond clock move between two stamps, moved verbatim; tracked for a de-flake pass
    time.sleep(0.004)


# ── the shared skip index ───────────────────────────────────────────────────
#
# A pass is recorded as part of the update that skips the issue: the same entry
# carries the phase, the line and the skip row. The index every crew consults
# before investigating is the fold of those rows across every crew of the
# repository.


def _pass(root, cid, sid, number, reason, scope, **patch):
    return _record(
        root,
        cid,
        sid,
        number,
        {"phase": "skipped", **patch},
        "skip",
        f"passed on #{number}",
        skip_reason=reason,
        skip_scope=scope,
    )
