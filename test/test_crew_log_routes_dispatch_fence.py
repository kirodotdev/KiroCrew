"""The agent's crew-log door: its identity gate, the dispatch fence, the post-read re-check.

A session reads its own unit or one its recorded ``session/opened`` creator edge says it
dispatched, keyed by slot. The grant is checked again when the offloaded read returns.
Shared fixtures come from ``crew_log_routes_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from crew_log_routes_helpers import _fresh_dispatch_scan  # noqa: F401
from crew_log_routes_helpers import _is_owner  # noqa: F401
from crew_log_routes_helpers import _isolated_home  # noqa: F401
from crew_log_routes_helpers import (
    CHILD_SLOT,
    CHILD_UNIT,
    CONDUCTOR_KEY,
    CONDUCTOR_SLOT,
    CONDUCTOR_UNIT,
    GRANDCHILD_UNIT,
    OWNER_KEY,
    SESSION,
    STRANGER_UNIT,
    _as_conductor,
    _class_moved,
    _dispatch_tree,
    _flag_on,
    _internal_request,
    _log,
    _opened,
    _Slot,
    _turn,
)

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.dashboard.handlers import crew_log as routes

# --------------------------------------------------------------------------- #
# The agent's door: the unit-keyed routes and who may walk through them
# --------------------------------------------------------------------------- #


def test_the_caller_name_is_pinned_to_the_mcp_servers_own(monkeypatch):
    """Two modules name one component; a rename on one side must fail here."""
    from kiro_crew.mcp_crew_log import SERVER_NAME

    assert routes.CREW_LOG_MCP_CALLER == SERVER_NAME


def test_the_enable_hint_names_the_real_flag():
    assert routes.CREW_LOG_ENV in routes.crew_log_enable_hint()


def test_the_enable_hint_says_to_unset_the_flag_not_to_set_it():
    """The log is on by default, so the only way to be off is a falsy value."""
    assert "unset" in routes.crew_log_enable_hint()
    assert f"{routes.CREW_LOG_ENV}=1" not in routes.crew_log_enable_hint()


def test_the_enable_hint_names_the_env_file_the_gateway_reads(monkeypatch, tmp_path):
    """An agent is told to edit this file, so naming the wrong one wastes the turn.

    The file read is ``config.loader.env_path()``, which follows ``KIROCREW_HOME``; a
    hint that hardcodes the default home sends an operator with another home to an
    inert file, and the flag appears not to work.
    """
    from kiro_crew.config.loader import env_path

    home = tmp_path / "elsewhere"
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    hint = routes.crew_log_enable_hint()
    assert str(env_path()).startswith(str(home))
    assert (
        str(env_path()) in hint or ("~/" + env_path().relative_to(Path.home()).as_posix()) in hint
    )
    assert "~/.kiro/crew/.env" not in hint


def test_the_session_ledger_refusal_names_the_env_file_the_gateway_reads(monkeypatch, tmp_path):
    from kiro_crew import session_ledger
    from kiro_crew.constants import env_file_display

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
    with pytest.raises(session_ledger.LedgerUnavailable) as caught:
        session_ledger._require_crew_log("s-any")
    assert env_file_display() in str(caught.value)
    assert "~/.kiro/crew/.env" not in str(caught.value)


@pytest.mark.parametrize(
    "refusal",
    [
        "work_ledger",
        "mcp_ledger",
    ],
)
def test_every_crew_log_off_refusal_names_the_env_file_the_gateway_reads(
    monkeypatch, tmp_path, refusal
):
    """The same file, spelled once, in every place that tells an operator to edit it.

    Issue Radar's crew store refuses the same way; the app pins its own refusal in
    its ``tests/``.
    """
    import importlib
    import inspect

    from kiro_crew import session_ledger
    from kiro_crew.dashboard.handlers import work_ledger
    from kiro_crew.mcp_tools import ledger as mcp_ledger

    module = {
        "session_ledger": session_ledger,
        "work_ledger": work_ledger,
        "mcp_ledger": mcp_ledger,
    }[refusal]
    source = inspect.getsource(module)
    if refusal == "work_ledger":  # mirror facade: code is in its owners
        source += "".join(inspect.getsource(importlib.import_module(o)) for o in module._OWNERS)
    assert "~/.kiro/crew/.env" not in source
    assert "env_file_display()" in source


def test_the_page_reader_is_the_one_shared_implementation():
    """One implementation, so the browser door and the agent door cannot differ."""
    from kiro_crew.crew_log import read as shared

    assert routes.MAX_PAGE_REFS == shared.MAX_PAGE_REFS
    assert routes._read_page.__module__ == routes.__name__
    with patch.object(shared, "read_page", return_value={"exists": False}) as called:
        routes._read_page(SESSION, 1, 2)
    called.assert_called_once_with(SESSION, 1, 2)


def test_a_read_with_the_flag_off_says_how_to_switch_it_on(monkeypatch):
    """MUTATION-SENSITIVE: the agent learns the flag state from THIS refusal."""
    monkeypatch.setenv(routes.CREW_LOG_ENV, "0")
    request = _internal_request("/api/crew-log/sessions")
    response = asyncio.run(routes.api_crew_log_sessions(request))
    assert response.status == 422
    body = json.loads(response.text)
    assert body["code"] == "crew_log_disabled"
    assert routes.CREW_LOG_ENV in body["error"]


def test_a_read_with_the_flag_unset_is_not_refused(monkeypatch):
    """The crew log is on by default, so an install that never set the flag reads."""
    monkeypatch.delenv(routes.CREW_LOG_ENV, raising=False)
    request = _internal_request("/api/crew-log/sessions")
    response = asyncio.run(routes.api_crew_log_sessions(request))
    assert response.status != 422
    assert json.loads(response.text).get("code") != "crew_log_disabled"


class TestTheDispatchFence:
    """A strict session identity, and then a scope: own unit, or one it dispatched.

    The scope comes from the recorded ``session/opened`` creator edge; the owner's tab
    still reaches everything. Caller classes ``session_control.authorize_target`` refuses
    are refused here too, because each is about where a read lands.
    """

    def test_a_conductor_reads_the_unit_it_dispatched(self, monkeypatch):
        """MUTATION-SENSITIVE: the lineage arm itself.

        The conductor is not the owner's tab and the child is not its own unit, so
        nothing but the recorded creator edge can admit this read.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["session_id"] == CHILD_UNIT

    def test_a_conductor_reads_a_descendant_forty_generations_down(self, monkeypatch):
        """MUTATION-SENSITIVE: the walk is bounded by the tree, not by a number.

        The repeat check on ``seen`` already ends the walk. A fixed depth cap would refuse
        an honest chain longer than the cap; this chain is 40 deep.
        """
        _flag_on(monkeypatch)
        depth = 40
        slots = [f"chat-deep-{i}" for i in range(depth)]
        units = [f"s-deep-{i}" for i in range(depth)]
        _opened(_log(units[0], slot=slots[0]), slot=slots[0])
        for i in range(1, depth):
            _opened(
                _log(units[i], slot=slots[i]),
                slot=slots[i],
                parent_slot=slots[i - 1],
                parent_sid=units[i - 1],
            )
        request = _internal_request(
            f"/api/crew-log/units/{units[-1]}/page",
            match={"unit": units[-1]},
            session_key=f"subagent:{slots[0]}",
            slots={slots[0]: _Slot()},
            sessions={f"subagent:{slots[0]}": units[0]},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200, response.text
        assert json.loads(response.text)["session_id"] == units[-1]

    def test_a_conductor_reads_a_grandchild(self, monkeypatch):
        """MUTATION-SENSITIVE: the walk is transitive, not one generation.

        The grandchild's edge names the CHILD, so admitting this needs the chain to
        be followed past its first link.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{GRANDCHILD_UNIT}/page", match={"unit": GRANDCHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["session_id"] == GRANDCHILD_UNIT

    def test_a_conductor_keeps_its_children_across_a_gateway_restart(self, monkeypatch):
        """MUTATION-SENSITIVE: the fence keys on the recorded SLOT, not on the sid.

        A restart gives the tab a new ACP session id while the child's entry still names
        the old unit as ``sid``. Only the slot survives the restart.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        # The same slot, a new session: a second log for the conductor's tab, whose
        # own entry cites no creator because the mint witness died with the process.
        reattached = "s-conductor-after-restart"
        _opened(_log(reattached, slot=CONDUCTOR_SLOT), slot=CONDUCTOR_SLOT)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            sessions={CONDUCTOR_KEY: reattached},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["session_id"] == CHILD_UNIT

    def test_a_target_whose_log_records_no_class_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the fail-closed half of the recorded-class test.

        A log with no recorded class cannot say whether it was app-owned, incognito or
        published, and a closed session has no live answer, so the absence refuses.
        """
        _flag_on(monkeypatch)
        _dispatch_tree(record_class=False)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "does not record what kind of session it is" in json.loads(response.text)["error"]

    def test_an_unreadable_middle_log_refuses_as_unknown_lineage_not_out_of_scope(
        self, monkeypatch, caplog
    ):
        """MUTATION-SENSITIVE: the scan's fault bit reaches the refusal SITE, not the caller.

        The middle child's announce record is unreadable. The caller gets the plain "out
        of scope" (a different answer would be an existence oracle); only the operator's
        log says the scan was incomplete.

        The damage is planted as bytes because the scanner caches an unreadable line's
        verdict; a patched read would test only the transient case.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        child_log = lg.crew_log_path(lg.KIND_SESSION, CHILD_UNIT)
        lines = child_log.read_bytes().split(b"\n")
        # Line 1 (header) stays, so the unit is not skipped as a stray directory. Line 2,
        # the announce record, becomes valid JSON that is not an entry: "announced,
        # unreadable" rather than "not announced yet".
        assert len(lines) >= 2, lines
        lines[1] = b'{"not": "an entry"}'
        child_log.write_bytes(b"\n".join(lines))
        from kiro_crew.crew_log import read as crew_log_read

        crew_log_read._TREE._heads.clear()

        request = _as_conductor(
            f"/api/crew-log/units/{GRANDCHILD_UNIT}/page", match={"unit": GRANDCHILD_UNIT}
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers.crew_log"):
            response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        # The wire carries only the ordinary refusal.
        assert "does not fall inside that scope" in json.loads(response.text)["error"]
        # The operator's log records the fault bit at the refusal site.
        assert any("lineage scan was INCOMPLETE" in record.message for record in caplog.records)

    def test_an_intact_store_logs_no_incomplete_scan_when_it_refuses(self, monkeypatch, caplog):
        """The control: an intact store refuses without an incomplete-scan record.

        Without it, logging "INCOMPLETE" on every denial would pass the test above.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{STRANGER_UNIT}/page", match={"unit": STRANGER_UNIT}
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers.crew_log"):
            response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "does not fall inside that scope" in json.loads(response.text)["error"]
        assert not any("lineage scan was INCOMPLETE" in r.message for r in caplog.records)

    def test_the_refusal_text_is_identical_whether_the_scan_was_short(self, monkeypatch):
        """MUTATION-SENSITIVE: no existence oracle at this door.

        The door passes the requested unit as the scan's ``preferred``, so a caller can
        steer which case it hits. Comparing whole bodies catches any new wording.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        intact = _as_conductor(
            f"/api/crew-log/units/{STRANGER_UNIT}/page", match={"unit": STRANGER_UNIT}
        )
        intact_body = json.loads(asyncio.run(routes.api_crew_log_unit_page(intact)).text)["error"]

        child_log = lg.crew_log_path(lg.KIND_SESSION, CHILD_UNIT)
        lines = child_log.read_bytes().split(b"\n")
        lines[1] = b'{"not": "an entry"}'
        child_log.write_bytes(b"\n".join(lines))
        from kiro_crew.crew_log import read as crew_log_read

        crew_log_read._TREE._heads.clear()
        # The first request seeded the session tree projection from the intact log;
        # drop it so the second request reads the damaged store as a new process does.
        from kiro_crew.crew_log import session_tree_projection

        session_tree_projection.reset_for_tests()

        short = _as_conductor(
            f"/api/crew-log/units/{GRANDCHILD_UNIT}/page", match={"unit": GRANDCHILD_UNIT}
        )
        short_body = json.loads(asyncio.run(routes.api_crew_log_unit_page(short)).text)["error"]
        assert short_body == intact_body

    def test_a_listing_only_lineage_fault_is_recorded_by_the_producer_once(
        self, monkeypatch, caplog
    ):
        """MUTATION-SENSITIVE: the listing consumer gets an operator record too.

        A listing never reaches the refusal door, and the cached fault never clears, so
        the producer must say which unit is unreadable. It must say so ONCE across two
        scans: a warning where the cache is read would log on every scan.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        child_log = lg.crew_log_path(lg.KIND_SESSION, CHILD_UNIT)
        lines = child_log.read_bytes().split(b"\n")
        assert len(lines) >= 2, lines
        lines[1] = b'{"not": "an entry"}'
        child_log.write_bytes(b"\n".join(lines))
        from kiro_crew.crew_log import read as crew_log_read

        crew_log_read._TREE._heads.clear()

        # ``list_session_units`` builds its dispatch view only when a slot is in scope,
        # so without scope_slot this fault is never reached.
        with caplog.at_level(logging.WARNING, logger="kiro_crew.crew_log.session_tree"):
            first = crew_log_read.list_session_units(limit=50, scope_slot=CONDUCTOR_SLOT)
            second = crew_log_read.list_session_units(limit=50, scope_slot=CONDUCTOR_SLOT)

        # The listing still tells its caller the set is short.
        assert first["truncated"] is True
        assert second["truncated"] is True
        # The producer accounted for it, naming the unit whose bytes carry the fault.
        judged = [r for r in caplog.records if "creator edge is unknown" in r.message]
        assert len(judged) == 1, [r.message for r in judged]
        unit_name = child_log.parent.name
        assert unit_name, "the fixture resolved no unit directory name"
        assert unit_name in judged[0].message

    @pytest.mark.parametrize(
        ("recorded", "word"),
        [
            ({"channel": True}, "published to a channel"),
            ({"app": "travel-desk"}, "app-scoped"),
            ({"memory": "incognito"}, "incognito"),
        ],
        ids=["channel", "app", "incognito"],
    )
    def test_a_closed_targets_class_is_read_from_its_own_log(self, monkeypatch, recorded, word):
        """MUTATION-SENSITIVE: the recorded class decides a target with no live slot.

        A finished run has no live slot, so only the class on the child's opening entry
        can refuse it.
        """
        _flag_on(monkeypatch)
        _dispatch_tree(**recorded)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert word in json.loads(response.text)["error"]

    def test_a_class_acquired_after_the_log_opened_refuses_a_closed_target(self, monkeypatch):
        """MUTATION-SENSITIVE: the whole reason the class history is folded, not read.

        The child gains a channel after opening, so the opener records none while later
        turns carry a third party's words. The target has no live slot to check.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        _class_moved(CHILD_UNIT, channel=True)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "published to a channel" in json.loads(response.text)["error"]

    def test_a_class_that_moved_back_stays_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the fold keeps the most restrictive value EVER held.

        The newest statement is clean, but the turns that ran while published are still in
        this log.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        _class_moved(CHILD_UNIT, channel=True)
        _class_moved(CHILD_UNIT)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "published to a channel" in json.loads(response.text)["error"]

    def test_a_class_history_with_no_beginning_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: a fold that saw only moves cannot say what it moved FROM.

        Retention can drop the opening entry, and the opener may have been app-owned.
        ``complete`` refuses: a log with an opening ``class`` also records its transitions.
        """
        _flag_on(monkeypatch)
        _dispatch_tree(record_class=False)
        _class_moved(CHILD_UNIT)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "not the class it started from" in json.loads(response.text)["error"]

    def test_a_closed_target_whose_class_never_moved_is_still_read(self, monkeypatch):
        """The admitting direction, which is the feature this whole fence exists for.

        Without it, a gate that refuses every closed target passes the refusal tests above.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        _class_moved(CHILD_UNIT, memory="persistent")
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["session_id"] == CHILD_UNIT

    def test_a_class_fold_that_stopped_short_of_the_file_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: a partial fold cannot answer a question about the whole log.

        ``iter_from`` stops at an unknown entry type, so a restrictive move after it would
        be unseen. The fold is shortened directly: the rule is about the seq it reached.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        from kiro_crew.crew_log import read as crew_log_read

        real = crew_log_read.projections.fold_session

        def _short(session_id, names, **kwargs):
            bundle = real(session_id, names, **kwargs)
            return type(bundle)(
                session_id=bundle.session_id,
                last_seq=max(0, bundle.last_seq - 1),
                checkpoints=bundle.checkpoints,
                origin=bundle.origin,
            )

        monkeypatch.setattr(crew_log_read.projections, "fold_session", _short)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "does not record what kind of session it is" in json.loads(response.text)["error"]

    def test_a_target_whose_class_moves_mid_read_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the re-check RE-FOLDS the target's class.

        The target is published and unlinked again during the read, so both live reads see
        it clean while the payload holds a channel turn. Reusing the grant's fold misses it.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        real = routes._read_page

        def _moving(*args, **kwargs):
            _class_moved(CHILD_UNIT, channel=True)
            _class_moved(CHILD_UNIT)
            return real(*args, **kwargs)

        monkeypatch.setattr(routes, "_read_page", _moving)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "published to a channel" in json.loads(response.text)["error"]

    def test_a_target_whose_class_holds_still_mid_read_is_still_answered(self, monkeypatch):
        """MUTATION-SENSITIVE: the re-fold is a TEST, not a second refusal.

        Without it, refusing whenever the log grew during the read would pass the test
        above and break every read of a working session.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        real = routes._read_page

        def _moving(*args, **kwargs):
            _class_moved(CHILD_UNIT, memory="persistent")
            return real(*args, **kwargs)

        monkeypatch.setattr(routes, "_read_page", _moving)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["session_id"] == CHILD_UNIT

    def test_a_target_whose_log_lost_its_front_segment_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: a log that does not hold seq 1 cannot be dated.

        Retention trims front segments. The survivor starts at a re-attachment's opener,
        which states a class while the lost first opener did not; only the segment NAME
        says the file starts past seq 1. The re-attachment keeps the dispatch edge, so
        the class, not the scope, is why it is refused.

        The trim is built on disk because no writer rotates yet.
        """
        _flag_on(monkeypatch)
        _dispatch_tree(record_class=False)
        child = CrewLog.open(lg.KIND_SESSION, CHILD_UNIT)
        _turn(child, 1)
        _opened(child, slot=CHILD_SLOT, parent_slot=CONDUCTOR_SLOT, parent_sid=CONDUCTOR_UNIT)

        directory = lg.crew_log_dir(lg.KIND_SESSION, CHILD_UNIT)
        head = directory / "log.jsonl"
        lines = head.read_text(encoding="utf-8").splitlines(keepends=True)
        # Line 0 is the log HEADER, not an entry, so the openers are found by type
        # rather than by position and the RE-ATTACHMENT is the second of them.
        openers = [
            i for i, line in enumerate(lines) if json.loads(line).get("type") == "session/opened"
        ]
        assert len(openers) == 2, f"expected an original opener and a re-attachment, got {openers}"
        # Every segment starts with a copy of the log HEADER; the tree reads line 2 of the
        # oldest surviving segment for the dispatch edge.
        kept = [lines[0]] + lines[openers[1] :]
        first_kept = json.loads(kept[1])["seq"]
        assert first_kept > 1, "a trim that keeps seq 1 is not a trim"
        (directory / f"log.{first_kept}.jsonl").write_text("".join(kept), encoding="utf-8")
        head.unlink()
        from kiro_crew.crew_log import read as crew_log_read

        crew_log_read.reset_class_bundles()

        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        error = json.loads(response.text)["error"]
        # The retention case gets its own text: unlike an unrecorded class, nothing can
        # make this log readable again.
        assert "no longer holds its own beginning" in error, error
        assert "does not record what kind of session" not in error, error

    def test_a_bare_session_key_is_read_as_its_own_slot(self, monkeypatch):
        """MUTATION-SENSITIVE: a key with no colon is a bare SLOT name, not a broken key.

        Splitting on the colon and taking the second part raises ``IndexError`` (a 500) on
        such a key. It is not a widening: class tests and the lineage walk still run on it.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _internal_request(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            session_key=CONDUCTOR_SLOT,
            slots={CONDUCTOR_SLOT: _Slot()},
            sessions={CONDUCTOR_SLOT: CONDUCTOR_UNIT},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200, response.text
        assert json.loads(response.text)["session_id"] == CHILD_UNIT

    def test_a_target_whose_log_has_a_damaged_line_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: byte damage must not raise an authorization ceiling.

        The store skips an unreadable interior line, so the fold still reaches the tail
        and the short-fold refusal cannot fire; only the contiguity check refuses. The
        damage is interior: a damaged last line looks like an append in flight, and a
        move with nothing after it has no turn run under it yet.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        _class_moved(CHILD_UNIT, channel=True)
        child = CrewLog.open(lg.KIND_SESSION, CHILD_UNIT)
        _turn(child, 1)
        head = lg.crew_log_dir(lg.KIND_SESSION, CHILD_UNIT) / "log.jsonl"
        lines = head.read_text(encoding="utf-8").splitlines(keepends=True)
        moves = [i for i, ln in enumerate(lines) if json.loads(ln).get("type") == "session/class"]
        assert len(moves) == 1, f"expected one move to damage, got {moves}"
        lines[moves[0]] = "{not json at all\n"
        head.write_text("".join(lines), encoding="utf-8")
        from kiro_crew.crew_log import read as crew_log_read

        crew_log_read.reset_class_bundles()

        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403, response.text
        error = json.loads(response.text)["error"]
        assert "does not record what kind of session it is" in error, error

    def test_an_out_of_tree_target_is_refused_without_naming_its_class(self, monkeypatch):
        """The scope refusal comes FIRST, so a class is not readable one refusal at a time.

        The stranger's log records no class here, so a gate that tested the class
        before the scope would answer with the class refusal and let any caller learn
        something about a unit it may not read at all.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        _opened(_log("s-lonely", slot="chat-lonely"), slot="chat-lonely", record_class=False)
        request = _as_conductor("/api/crew-log/units/s-lonely/page", match={"unit": "s-lonely"})
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        error = json.loads(response.text)["error"]
        assert "does not fall inside that scope" in error
        assert "kind of session" not in error

    def test_a_mirrored_owner_tab_reads_only_its_own_unit(self, monkeypatch):
        """MUTATION-SENSITIVE: the owner arm is held to the caller classes too.

        A tab mirrored to a channel republishes every turn, so it keeps only its own unit.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        owner_unit = "s-owner"
        _opened(_log(owner_unit, slot="chat-owner"), slot="chat-owner")
        slots = {"chat-owner": _Slot(key=OWNER_KEY, linked_session_key="slack:C500")}
        sessions = {OWNER_KEY: owner_unit}
        peer = _internal_request(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots=slots,
            sessions=sessions,
        )
        response = asyncio.run(routes.api_crew_log_unit_page(peer))
        assert response.status == 403
        assert "channel" in json.loads(response.text)["error"]
        own = _internal_request(
            f"/api/crew-log/units/{owner_unit}/page",
            match={"unit": owner_unit},
            slots=slots,
            sessions=sessions,
        )
        assert asyncio.run(routes.api_crew_log_unit_page(own)).status == 200

    def test_a_sibling_cannot_read_a_sibling(self, monkeypatch):
        """MUTATION-SENSITIVE: the fence, from the side it exists to hold.

        The stranger's tree does not contain the conductor, so a walk that ran to
        the end of the chain still must not admit it. A gate that admitted every
        identified caller would pass this read.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{STRANGER_UNIT}/page", match={"unit": STRANGER_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        body = json.loads(response.text)
        assert body["code"] == "forbidden"
        assert "does not fall inside that scope" in body["error"]

    def test_the_lineage_walk_runs_off_the_event_loop(self, monkeypatch):
        """MUTATION-SENSITIVE: the fold is the only arm that touches the filesystem.

        It stats every unit, so it must not run on the loop. The verdict is asserted too,
        so a scan that never ran cannot pass.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        from kiro_crew.crew_log import read as crew_log_read

        real = crew_log_read.dispatch_view
        seen: dict[str, bool] = {}

        def _watching(preferred=()):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                seen["on_loop"] = False
            else:
                seen["on_loop"] = True
            return real(preferred)

        monkeypatch.setattr(crew_log_read, "dispatch_view", _watching)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert seen == {"on_loop": False}

    def test_a_malformed_unit_is_refused_rather_than_crashing(self, monkeypatch):
        """MUTATION-SENSITIVE: the lineage walk's own error containment.

        The unit id is whatever a request named, and the store rejects some spellings
        outright. A walk that let that escape would turn an unauthorized read into a
        server error -- no verdict, and an audit row that records neither outcome. The
        walk answers "no creator recorded" instead, which the fence refuses.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        bad = "bad\\id"
        request = _as_conductor(f"/api/crew-log/units/{bad}/page", match={"unit": bad})
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert json.loads(response.text)["code"] == "forbidden"

    def test_a_session_reads_its_own_unit(self, monkeypatch):
        """No slot lookup and no walk: the cheapest arm, and the one every caller has."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CONDUCTOR_UNIT}/page", match={"unit": CONDUCTOR_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200

    def test_a_channel_linked_caller_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the mirrored caller-class exclusion.

        A linked session's conversation is a channel thread, so a peer's log it reads is
        published there. It is refused even on the child it dispatched.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots={"conductor": _Slot(linked_session_key="slack:C123")},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "channel" in json.loads(response.text)["error"]

    def test_a_channel_linked_caller_still_reads_its_own_unit(self, monkeypatch):
        """The exclusion is about reading PAST its own record, not about reading.

        Without this the mirrored exclusions would be indistinguishable from a
        blanket refusal of the class, which is a different and wider rule.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CONDUCTOR_UNIT}/page",
            match={"unit": CONDUCTOR_UNIT},
            slots={"conductor": _Slot(linked_session_key="slack:C123")},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200

    @pytest.mark.parametrize("prefix", ["cron", "taskrunner"], ids=["cron", "taskrunner"])
    def test_an_unattended_caller_cannot_read_a_peer(self, monkeypatch, prefix):
        """A scheduled run has no operator watching what it did with the content."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        key = f"{prefix}:nightly"
        request = _internal_request(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            session_key=key,
            slots={"nightly": _Slot()},
            sessions={key: CONDUCTOR_UNIT},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "unattended" in json.loads(response.text)["error"]

    def test_an_app_owned_caller_cannot_read_a_peer(self, monkeypatch):
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots={"conductor": _Slot(app="travel-desk")},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "app-owned" in json.loads(response.text)["error"]

    def test_an_incognito_caller_cannot_read_a_peer(self, monkeypatch):
        """That session was created to leave and learn nothing."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots={"conductor": _Slot(memory_mode="incognito")},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "incognito" in json.loads(response.text)["error"]

    def test_a_caller_the_slot_table_cannot_place_reads_only_its_own(self, monkeypatch):
        """A caller with no live slot cannot be placed in a dispatch tree.

        It keeps its own unit, because that is resolved from the session manager
        rather than from the slot table.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        peer = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}, slots={}
        )
        assert asyncio.run(routes.api_crew_log_unit_page(peer)).status == 403
        own = _as_conductor(
            f"/api/crew-log/units/{CONDUCTOR_UNIT}/page",
            match={"unit": CONDUCTOR_UNIT},
            slots={},
        )
        assert asyncio.run(routes.api_crew_log_unit_page(own)).status == 200

    def test_a_live_channel_linked_target_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the mirrored TARGET-class exclusion.

        A child minted by ``session_create`` is unlinked, so this is the case where
        it was linked AFTERWARDS -- read from its live slot, which is why the slot
        is matched to the unit it is currently writing.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        child_key = "dashboard:chat-child"
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots={
                "conductor": _Slot(),
                "chat-child": _Slot(key=child_key, linked_session_key="slack:C999"),
            },
            sessions={CONDUCTOR_KEY: CONDUCTOR_UNIT, child_key: CHILD_UNIT},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "channel" in json.loads(response.text)["error"]

    def test_a_closed_target_is_read_by_its_dispatcher(self, monkeypatch):
        """The whole point: a FINISHED child's recorded log.

        No live slot is writing the child's unit here, which is the ordinary state
        of a completed run. ``authorize_target`` answers 404 for that, and this door
        deliberately does not.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        assert asyncio.run(routes.api_crew_log_unit_page(request)).status == 200

    def test_a_caller_with_no_session_key_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the identity requirement, ahead of every scope arm.

        An unnamed caller is refused because the audit row for a read is only worth
        keeping if it names the session that made it.
        """
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request("/api/crew-log/sessions", session_key="")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        body = json.loads(response.text)
        assert body["code"] == "forbidden"
        assert "session identity" in body["error"]

    def test_a_request_naming_another_component_is_refused(self, monkeypatch):
        """The route serves ONE internal caller; the header is validated, not trusted."""
        _flag_on(monkeypatch)
        request = _internal_request("/api/crew-log/sessions", caller="kirocrew-dashboard")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        assert routes.CREW_LOG_MCP_CALLER in json.loads(response.text)["error"]

    def test_the_owner_at_a_dashboard_tab_is_admitted(self, monkeypatch):
        _flag_on(monkeypatch)
        handle = _log()
        _opened(handle)
        request = _internal_request("/api/crew-log/sessions")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200
        assert json.loads(response.text)["kind"] == "session"


class TestTheGrantIsRecheckedAfterTheRead:
    """A verdict about the TARGET's class must still hold when the payload returns.

    The read runs off the loop, a suspension point during which the target can append a
    class move.
    """

    def _links_during(self, monkeypatch, slot, name: str = "_read_page"):
        """Make the offloaded read acquire a channel link as its side effect.

        The link lands while the read is in flight, which is the only moment this
        guard exists for: before it, the gate refuses; after it, the payload is
        already out.
        """
        real = getattr(routes, name)

        def _linking(*args, **kwargs):
            slot.linked_session_key = "telegram:-100999"
            return real(*args, **kwargs)

        monkeypatch.setattr(routes, name, _linking)

    def test_the_owner_arm_keeps_reading_a_target_the_lineage_arm_would_refuse(self, monkeypatch):
        """MUTATION-SENSITIVE: the re-check re-runs the tests the GRANT ran, no others.

        The owner arm runs no target test. This target records no class, so an
        unconditional target re-check would refuse the owner's read of an old unit.
        """
        _flag_on(monkeypatch)
        _opened(_log("s-old", slot="chat-old"), slot="chat-old", record_class=False)
        request = _internal_request("/api/crew-log/units/s-old/page", match={"unit": "s-old"})
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["session_id"] == "s-old"

    def test_a_payload_carrying_no_record_of_its_grant_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the re-check fails closed on a mark it cannot read.

        Each granting arm marks which target test it ran. Without the mark, falling back
        to the live test alone would skip the recorded test for a target with a live slot.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        assert routes.TARGET_CLASS_KEY not in request, "the gate has not run yet"
        response = asyncio.run(
            routes._stale_grant_refusal(request, "session_crew_log.read", unit=CHILD_UNIT)
        )
        assert response is not None
        assert "cannot be checked against the grant" in json.loads(response.text)["error"]

    def test_a_caller_that_links_to_a_channel_mid_read_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the caller half of the re-check on the page route.

        A mirror bound while the read is suspended would publish the child's log; the gate
        saw an unlinked caller. Only the handoff is covered: a link after the payload
        returns is a residual named in ``_stale_grant_refusal``'s docstring.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        slot = _Slot()
        self._links_during(monkeypatch, slot)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots={CONDUCTOR_SLOT: slot},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "lands in that channel's thread" in json.loads(response.text)["error"]

    def test_a_caller_that_links_during_the_refold_offload_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the caller verdict is re-read AFTER the refold suspends.

        The caller verdict is sampled before the arms run, and the lineage arm suspends
        again to refold the target. Hooking ``recorded_class`` places the link inside that
        second suspension, after the sample.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        slot = _Slot()
        from kiro_crew.crew_log import read as crew_log_read

        real = crew_log_read.recorded_class
        calls = []

        def _linking(*args, **kwargs):
            # The gate's fold (call 1) precedes the caller sample, which would catch a
            # link there; link on the re-check's call instead.
            calls.append(1)
            if len(calls) >= 2:
                slot.linked_session_key = "telegram:-100999"
            return real(*args, **kwargs)

        monkeypatch.setattr(crew_log_read, "recorded_class", _linking)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots={CONDUCTOR_SLOT: slot},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert len(calls) >= 2, "the re-check never refolded, so the window was not exercised"
        assert response.status == 403
        assert "lands in that channel's thread" in json.loads(response.text)["error"]

    def test_a_target_that_moves_mid_read_still_lets_a_caller_read_its_own_unit(self, monkeypatch):
        """MUTATION-SENSITIVE: the re-check is CONDITIONAL, not a blanket refusal.

        The own-unit exemption returns before any target test; without this case an
        unconditional refusal would pass the others and break every self-read.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        real = routes._read_page

        def _moving(*args, **kwargs):
            _class_moved(CONDUCTOR_UNIT, channel=True)
            return real(*args, **kwargs)

        monkeypatch.setattr(routes, "_read_page", _moving)
        request = _as_conductor(
            f"/api/crew-log/units/{CONDUCTOR_UNIT}/page",
            match={"unit": CONDUCTOR_UNIT},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["session_id"] == CONDUCTOR_UNIT

    def test_a_caller_that_links_mid_projection_is_refused(self, monkeypatch):
        """The same race on the projection route, which offloads its own fold."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        slot = _Slot()
        real = routes._crew_log().read_projection

        def _linking(*args, **kwargs):
            slot.linked_session_key = "telegram:-100999"
            return real(*args, **kwargs)

        monkeypatch.setattr(routes._crew_log(), "read_projection", _linking)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/projection/status",
            match={"unit": CHILD_UNIT, "name": "status"},
            slots={CONDUCTOR_SLOT: slot},
        )
        response = asyncio.run(routes.api_crew_log_unit_projection(request))
        assert response.status == 403
        assert "lands in that channel's thread" in json.loads(response.text)["error"]

    def test_a_caller_that_links_mid_listing_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the listing's scope is re-derived and compared.

        A listing carries its scope as a FILTER, so the rows were selected under the
        wider scope the caller held at the gate. Acquiring a link narrows that scope
        to its own unit, and the rows already gathered include the units it
        dispatched -- so the comparison, not a row test, is what refuses.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        slot = _Slot()
        real = routes._crew_log_read().list_session_units

        def _linking(*args, **kwargs):
            slot.linked_session_key = "telegram:-100999"
            return real(*args, **kwargs)

        monkeypatch.setattr(routes._crew_log_read(), "list_session_units", _linking)
        request = _as_conductor("/api/crew-log/sessions", slots={CONDUCTOR_SLOT: slot})
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403

    def test_every_offloading_route_rechecks_before_it_answers(self):
        """MUTATION-SENSITIVE: the guard is at the choke point, not at one call site.

        A new route missing the re-check is an absent line, so the rule is asserted over
        the source: the re-check has ONE caller (the offload helper), and the gate and that
        helper are the only places a door route may suspend.
        """
        import ast
        import importlib
        import inspect

        # a mirror facade: its code is in the owners it lists
        tree = ast.parse(
            inspect.getsource(routes)
            + "".join(inspect.getsource(importlib.import_module(o)) for o in routes._OWNERS)
        )
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        ]

        def _calls(node):
            # Bare and attribute calls both count: a gate call that goes unrecognised
            # would exempt its route from the suspension rules below.
            names = set()
            for n in ast.walk(node):
                if not isinstance(n, ast.Call):
                    continue
                if isinstance(n.func, ast.Name):
                    names.add(n.func.id)
                elif isinstance(n.func, ast.Attribute):
                    names.add(n.func.attr)
            return names

        # An allowlist of suspension points (``await``, ``async with``, ``async for``):
        # listing storage calls instead would miss ``run_in_executor`` or a gather.
        allowed_awaits = {"_authorize_crew_log_read", "_offload_then_recheck"}
        # Bare names only, so an attribute-form await is named back (fail closed);
        # ``ast.unparse`` below only prints the offender.

        def _suspends_outside(node):
            offenders = []
            for n in ast.walk(node):
                if isinstance(n, (ast.AsyncWith, ast.AsyncFor)):
                    offenders.append(type(n).__name__)
                    continue
                if not isinstance(n, ast.Await):
                    continue
                awaited = n.value
                name = ""
                if isinstance(awaited, ast.Call):
                    if isinstance(awaited.func, ast.Name):
                        name = awaited.func.id
                    else:
                        name = ast.unparse(awaited.func)
                if name not in allowed_awaits:
                    offenders.append(name or ast.unparse(awaited))
            return offenders

        rechecking = sorted(f.name for f in functions if "_stale_grant_refusal" in _calls(f))
        assert rechecking == ["_offload_then_recheck"], (
            "the re-check is reachable from more than one place, so a route can call "
            f"it out of order or not at all: {rechecking}"
        )

        doors = [f for f in functions if "_authorize_crew_log_read" in _calls(f)]
        for door in doors:
            outside = _suspends_outside(door)
            assert not outside, (
                f"{door.name} suspends at {outside}, outside the gate and the helper, "
                "so it can answer under a grant taken before the read; offload through "
                "_offload_then_recheck"
            )
        folded = sorted(f.name for f in doors if "_offload_then_recheck" in _calls(f))
        # A control: an empty door set would satisfy the loop above silently.
        assert folded == [
            "api_crew_log_sessions",
            "api_crew_log_unit_page",
            "api_crew_log_unit_projection",
        ], f"the agent door's offloading routes are not the three expected: {folded}"

        # Second control: resolve is a door route that answers from memory, so it owes
        # no re-check.
        resolve = [f for f in doors if f.name == "api_crew_log_resolve"]
        assert len(resolve) == 1, "resolve is no longer an agent-door route"
        assert "_offload_then_recheck" not in _calls(resolve[0])
        assert "_stale_grant_refusal" not in _calls(resolve[0])
        assert _suspends_outside(resolve[0]) == []


class TestTheOffloadHelperOwnsTheOrder:
    """The three steps after the gate live in one helper, so the helper is pinned.

    Two steps have no other cover: mapping a storage refusal to a response, and shaping
    only after the re-check holds.
    """

    def _request(self) -> object:
        _dispatch_tree()
        return _as_conductor(f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT})

    @pytest.mark.parametrize(
        ("code", "status", "reported"),
        [
            ("invalid_id", 400, "invalid_id"),
            ("unknown_entry_type", 409, "unknown_entry_type"),
            ("no_ledger", 422, "no_ledger"),
            ("", 422, "crew_log_error"),
        ],
    )
    def test_a_storage_refusal_reaches_the_caller_with_a_code_it_can_act_on(
        self, monkeypatch, code, status, reported
    ):
        """MUTATION-SENSITIVE: the refusal is mapped, not raised and not flattened.

        Each refusal keeps its own status and code: a bad unit id is the caller's to fix,
        an unparsed entry type is the deployment's.
        """
        _flag_on(monkeypatch)
        from kiro_crew.crew_log.errors import CrewLogError

        shaped: list = []

        def _work():
            raise CrewLogError("the store refused", code=code)

        def _respond(payload):
            shaped.append(payload)
            return web.json_response({"shaped": True})

        response = asyncio.run(
            routes._offload_then_recheck(
                self._request(), "session_crew_log.read", _work, _respond, unit=CHILD_UNIT
            )
        )
        assert response.status == status
        body = json.loads(response.text)
        assert body["code"] == reported
        assert body["error"] == "the store refused"
        assert shaped == [], "the route shaped an answer for a read that produced none"

    def test_the_shaping_never_runs_when_the_grant_was_withdrawn(self, monkeypatch):
        """MUTATION-SENSITIVE: the re-check precedes the shaping, not just the return.

        Shaping first and refusing after would give the right response while route code
        touched content the withdrawn grant does not cover.
        """
        _flag_on(monkeypatch)
        read: list = []
        shaped: list = []
        refusal = web.json_response({"error": "withdrawn"}, status=403)

        async def _withdrawn(request, operation, *, unit="", listing=False):
            return refusal

        monkeypatch.setattr(routes, "_stale_grant_refusal", _withdrawn)

        def _work():
            read.append(1)
            return {"exists": True}

        def _respond(payload):
            shaped.append(payload)
            return web.json_response({"shaped": True})

        response = asyncio.run(
            routes._offload_then_recheck(
                self._request(), "session_crew_log.read", _work, _respond, unit=CHILD_UNIT
            )
        )
        assert response is refusal
        assert read == [1], "the payload was never built, so the order was not exercised"
        assert shaped == [], "the route's shaping ran under a withdrawn grant"

    def test_the_shaping_receives_the_payload_the_read_produced(self, monkeypatch):
        """The other half of the order: once the grant holds, the route shapes it.

        Paired with the case above so that "never shapes" cannot pass both. The
        payload is handed over untouched, which is what lets a route keep a 404 for
        an absent log and a wrapper key for a fold.
        """
        _flag_on(monkeypatch)

        async def _holds(request, operation, *, unit="", listing=False):
            return None

        monkeypatch.setattr(routes, "_stale_grant_refusal", _holds)
        payload = {"exists": True, "entries": []}
        shaped: list = []

        def _respond(seen):
            shaped.append(seen)
            return web.json_response({"shaped": True})

        response = asyncio.run(
            routes._offload_then_recheck(
                self._request(),
                "session_crew_log.read",
                lambda: payload,
                _respond,
                unit=CHILD_UNIT,
            )
        )
        assert shaped == [payload]
        assert shaped[0] is payload
        assert json.loads(response.text) == {"shaped": True}

    def test_a_route_reaches_the_mapping_for_its_own_read(self, monkeypatch):
        """The page route answers a storage refusal, not a 500.

        The cases above pin the helper; this pins that a route is wired to it, so a
        route that stopped offloading through it would not keep this cover.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        from kiro_crew.crew_log.errors import CrewLogError

        def _refusing(*args, **kwargs):
            raise CrewLogError("bad unit id", code="invalid_id")

        monkeypatch.setattr(routes, "_read_page", _refusing)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 400
        assert json.loads(response.text)["code"] == "invalid_id"
