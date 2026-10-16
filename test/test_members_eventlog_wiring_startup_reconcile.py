"""The startup reconcile sweep over the per-member event logs.

``reconcile_members_at_startup`` writes one closer per interrupted patrol and
slot and nothing on a rerun, writes a member's config only from live config
bytes it can name, and leaves ambiguous, foreign and placeholder logs alone with
warnings that carry no values.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from members_eventlog_wiring_helpers import _fresh_eventlog  # noqa: F401
from members_eventlog_wiring_helpers import CREW, _agent, _fake_config

from kiro_crew import eventlog_hooks, members
from kiro_crew.eventlog import types
from kiro_crew.eventlog.service import get_service


def _seed_config_baseline(svc, slug, agent_cfg):
    """Plant the member/config a sweep would then find already correct.

    Appended directly rather than through ``reconcile_member_config``: seeding is not
    the behaviour under test, and that entry now requires a stamp naming the LIVE
    config bytes, which a test with no config file on disk cannot supply. Using it here
    would make these tests depend on a currency check they are not about.
    """
    svc.append(
        slug,
        types.MEMBER_CONFIG,
        {
            **eventlog_hooks._config_snapshot_for_agent(agent_cfg),
            "changed": list(eventlog_hooks._CONFIG_FIELDS),
        },
    )


# ---------------------------------------------------------------------------
# 3. reconcile_members_at_startup: synthesize interrupted closers, once
# ---------------------------------------------------------------------------
class TestStartupReconcile:
    def test_no_entry_reconciles_without_naming_the_live_config_bytes(self, monkeypatch):
        """There is ONE reconcile entry and it always asserts content currency.

        A stamp that does not describe the live file refuses, and so does a caller with
        no stamp at all -- there is no second, exempt entry point a caller could reach
        for instead. The sweep loads config itself and passes that load's digest, so no
        caller can write a member/config from bytes it cannot name. Both refusals are
        pinned together because a single entry that accepted a falsy stamp would open
        that exemption silently.
        """
        from kiro_crew.config import loader as loader_mod

        cfg = _fake_config({CREW: _agent(model="sonnet")})
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        monkeypatch.setattr(loader_mod, "config_content_stamp", lambda: "the-live-bytes")
        view = svc.snapshot(slug)["values"].get(types.PROJ_ROSTER, {})
        seq_before = svc.last_seq(slug)

        # A digest that does not describe the live file.
        refused = eventlog_hooks.reconcile_member_config(
            slug, CREW, cfg.agents[CREW], view, config_stamp="bytes-this-caller-read-earlier"
        )
        assert refused is None, "a stamp that does not match the live config must refuse"
        assert svc.last_seq(slug) == seq_before, "the refusal must append nothing"

        # No stamp at all: the sweep's old exemption, now closed.
        for absent in (None, ""):
            unnamed = eventlog_hooks.reconcile_member_config(
                slug, CREW, cfg.agents[CREW], view, config_stamp=absent
            )
            assert unnamed is None, (
                f"config_stamp={absent!r} reconciled without naming any bytes, which is "
                "the startup-sweep exemption this change removed"
            )
            assert svc.last_seq(slug) == seq_before, "an unnamed reconcile must append nothing"

        # The case the currency COMPARISON cannot decide: the live config cannot be
        # named either, so `absent != config_content_stamp()` is False and the
        # comparison reads two unknowns as a match. Unknown is never a match, so the
        # refusal has to be reached before the comparison, not by it.
        monkeypatch.setattr(loader_mod, "config_content_stamp", lambda: None)
        for absent in (None, ""):
            both_unnamed = eventlog_hooks.reconcile_member_config(
                slug, CREW, cfg.agents[CREW], view, config_stamp=absent
            )
            assert both_unnamed is None, (
                f"config_stamp={absent!r} against an equally unnamed live config "
                "reconciled: two unknown provenances were compared as equal"
            )
            assert svc.last_seq(slug) == seq_before, "an unnamed reconcile must append nothing"
        monkeypatch.setattr(loader_mod, "config_content_stamp", lambda: "the-live-bytes")

        # The same member and view DO reconcile once the stamp names the live bytes,
        # so the refusals above are about provenance and not about the comparison.
        wrote = eventlog_hooks.reconcile_member_config(
            slug, CREW, cfg.agents[CREW], view, config_stamp="the-live-bytes"
        )
        assert wrote, "a stamp naming the live config must reconcile"
        assert svc.last_seq(slug) > seq_before, "the stamped reconcile must append"
        assert (
            svc.snapshot(slug)["values"].get(types.PROJ_ROSTER, {}).get("model") == "sonnet"
        ), "the stamped reconcile must land the config it was handed"

    def test_the_sweep_writes_the_live_config_not_the_one_it_was_handed(self, monkeypatch):
        """A save that landed after boot must survive the startup sweep.

        The gateway hands the sweep the config object it loaded when it was
        constructed, and the sweep runs later as a background task with the HTTP port
        already listening -- so a dashboard save can have landed in between. Writing
        the handed values would append the pre-save snapshot over the save's own
        member/config and regress the projection, which has no compaction to undo it.
        The sweep therefore loads config itself and reconciles from THAT, so the live
        value is what reaches the log.
        """
        from kiro_crew.config import loader as loader_mod

        boot_cfg = _fake_config({CREW: _agent(model="sonnet")})
        live_cfg = _fake_config({CREW: _agent(model="opus")})
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        _seed_config_baseline(svc, slug, boot_cfg.agents[CREW])
        assert svc.snapshot(slug)["values"].get(types.PROJ_ROSTER, {}).get("model") == "sonnet"

        monkeypatch.setattr(
            loader_mod, "load_config_with_content_stamp", lambda: (live_cfg, "live-bytes")
        )
        monkeypatch.setattr(loader_mod, "config_content_stamp", lambda: "live-bytes")

        eventlog_hooks.reconcile_members_at_startup(
            boot_cfg, SimpleNamespace(_slots={}), SimpleNamespace(get_by_slot=lambda key: None)
        )

        assert svc.snapshot(slug)["values"].get(types.PROJ_ROSTER, {}).get("model") == "opus", (
            "the sweep wrote the config it was handed at boot, so a save that landed "
            "while it was queued was overwritten in the projection"
        )

    @pytest.mark.parametrize(
        ("stamp", "degraded"),
        [
            (None, frozenset()),
            ("live-bytes", frozenset({"whole-config"})),
        ],
        ids=["no-digest", "degraded-to-defaults"],
    )
    def test_bad_provenance_withholds_the_config_write_but_not_the_closers(
        self, monkeypatch, stamp, degraded
    ):
        """Unnamed or unfaithful config withholds the correcting write, nothing else.

        A config that could not be read whole, or that read whole but would not parse,
        leaves field DEFAULTS standing in for what the operator wrote; correcting the
        log from those would overwrite good values because of a typo. The closers below
        are decided from live process state against the log and never from config
        content, so they must still land -- an interrupted slot reading open until the
        next boot is a separate defect from a degraded config.
        """
        from kiro_crew.config import loader as loader_mod

        cfg = _fake_config({CREW: _agent(model="sonnet")})
        live_cfg = _fake_config({CREW: _agent(model="opus")})
        live_cfg.degraded_sections = degraded
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        _seed_config_baseline(svc, slug, cfg.agents[CREW])
        svc.append(slug, types.PATROL_STARTED, {"slot_key": "member-code-reviewer"})
        svc.append(slug, types.SLOT_OPENED, {"slot_key": "worker-1"})
        seq_before = svc.last_seq(slug)

        monkeypatch.setattr(loader_mod, "load_config_with_content_stamp", lambda: (live_cfg, stamp))
        monkeypatch.setattr(loader_mod, "config_content_stamp", lambda: "live-bytes")

        wrote = eventlog_hooks.reconcile_members_at_startup(
            cfg, SimpleNamespace(_slots={}), SimpleNamespace(get_by_slot=lambda key: None)
        )

        assert wrote == 2, "the closers are not about config content and must still land"
        appended = svc.history(slug, before=None, limit=10)[: svc.last_seq(slug) - seq_before]
        corrected = types.MEMBER_CONFIG in {e["type"] for e in appended}
        assert not corrected, "bad provenance reached the log as a correcting member/config"
        assert svc.snapshot(slug)["values"].get(types.PROJ_ROSTER, {}).get("model") == "sonnet", (
            "the projection was corrected from a config that names no bytes or is "
            "degraded to defaults"
        )

    def test_writes_one_closer_each_then_nothing_on_rerun(self):
        cfg = _fake_config({CREW: _agent()})
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        # Establish the config baseline first so the sweep's config-reconcile
        # step is a no-op — this test is about the two interrupted CLOSERS, not
        # the incidental first member/config.
        _seed_config_baseline(svc, slug, cfg.agents[CREW])
        # A wake armed for a slot the autonudge service does not hold, and a
        # driving.open slot missing from state._slots.
        svc.append(slug, types.PATROL_STARTED, {"slot_key": "member-code-reviewer"})
        svc.append(slug, types.SLOT_OPENED, {"slot_key": "worker-1"})

        # autonudge has no loop for the armed slot; state holds neither slot.
        autonudge = SimpleNamespace(get_by_slot=lambda key: None)
        state = SimpleNamespace(_slots={})

        seq_before = svc.last_seq(slug)
        wrote = eventlog_hooks.reconcile_members_at_startup(cfg, state, autonudge)
        assert wrote == 2

        events = svc.history(slug, before=None, limit=10)
        newest_types = {(e["type"], e["data"].get("reason")) for e in events[:2]}
        assert (types.PATROL_STOPPED, "interrupted") in newest_types
        assert (types.SLOT_CLOSED, "interrupted") in newest_types
        assert svc.last_seq(slug) == seq_before + 2, "only the two closers should be appended"

        # A second run appends nothing: patrol is now stopped, the slot closed,
        # and the config still matches.
        seq_after = svc.last_seq(slug)
        assert eventlog_hooks.reconcile_members_at_startup(cfg, state, autonudge) == 0
        assert svc.last_seq(slug) == seq_after

    def test_a_contended_patrol_closer_does_not_starve_the_slot_closers(self, monkeypatch):
        """One closer losing its tail must not cost the same member's other closers.

        The patrol closer is attempted before the slot closers, so an exhaustion that
        propagated straight out of the member's sweep would skip every slot below it
        -- in BOTH passes, because the retry re-enters at the same first closer. The
        slot would then read open until the next boot even though nothing was
        contending for it. Only the patrol closer is starved here; the slot's own
        append is left alone, so a green run is the slot closer having been reached.
        """
        from kiro_crew.eventlog import service as svc_mod

        cfg = _fake_config({CREW: _agent()})
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        _seed_config_baseline(svc, slug, cfg.agents[CREW])
        svc.append(slug, types.PATROL_STARTED, {"slot_key": "member-code-reviewer"})
        svc.append(slug, types.SLOT_OPENED, {"slot_key": "worker-1"})

        real_closer = svc.append_closer_if_still_applies
        attempted = []

        def _patrol_always_loses_its_tail(target, type, data, **kwargs):
            attempted.append(type)
            if type == types.PATROL_STOPPED:
                raise svc_mod.CloserTailContention(target, type)
            return real_closer(target, type, data, **kwargs)

        monkeypatch.setattr(svc, "append_closer_if_still_applies", _patrol_always_loses_its_tail)

        wrote = eventlog_hooks.reconcile_members_at_startup(
            cfg, SimpleNamespace(_slots={}), SimpleNamespace(get_by_slot=lambda key: None)
        )

        monkeypatch.setattr(svc, "append_closer_if_still_applies", real_closer)
        assert types.PATROL_STOPPED in attempted, "the patrol closer was never attempted"
        assert types.SLOT_CLOSED in attempted, (
            "the slot closer was never attempted: the patrol closer's contention "
            "propagated out of the member's sweep and skipped it"
        )
        events = svc.history(slug, before=None, limit=None)
        assert (types.SLOT_CLOSED, "interrupted") in {
            (e["type"], e["data"].get("reason")) for e in events
        }, "the interrupted slot was left open by a closer that never ran"
        driving = svc.snapshot(slug)["values"].get(types.PROJ_DRIVING, {}) or {}
        assert "worker-1" not in (
            driving.get("open", []) or []
        ), "the slot still reads open after the sweep"
        assert wrote == 1, f"the sweep reported {wrote} closer(s), not the one that landed"

    def test_a_closer_refused_the_write_lease_also_leaves_its_siblings_alone(self, monkeypatch):
        """Losing the lease is the closer's other contention, and costs the same.

        A closer has two ways to come back empty-handed against a busy member: it
        loses the tail on every attempt, or it never gets write ownership and the
        store refuses it. Both are one member being written to by another process,
        both leave the closer unplaced, and neither says anything about the closers
        below it -- so containing only the first would starve the same slots through
        the second door. A refusal is also the store's own guarantee that nothing was
        written, which is what makes one more attempt safe.
        """
        from kiro_crew.crew_log.errors import CODE_ALREADY_OWNED, CrewLogError

        cfg = _fake_config({CREW: _agent()})
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        _seed_config_baseline(svc, slug, cfg.agents[CREW])
        svc.append(slug, types.PATROL_STARTED, {"slot_key": "member-code-reviewer"})
        svc.append(slug, types.SLOT_OPENED, {"slot_key": "worker-1"})

        real_closer = svc.append_closer_if_still_applies
        attempted = []

        def _patrol_never_gets_the_lease(target, type, data, **kwargs):
            attempted.append(type)
            if type == types.PATROL_STOPPED:
                raise CrewLogError(
                    f"another process owns the log for {target!r}", code=CODE_ALREADY_OWNED
                )
            return real_closer(target, type, data, **kwargs)

        monkeypatch.setattr(svc, "append_closer_if_still_applies", _patrol_never_gets_the_lease)

        wrote = eventlog_hooks.reconcile_members_at_startup(
            cfg, SimpleNamespace(_slots={}), SimpleNamespace(get_by_slot=lambda key: None)
        )

        monkeypatch.setattr(svc, "append_closer_if_still_applies", real_closer)
        assert types.PATROL_STOPPED in attempted, "the patrol closer was never attempted"
        assert types.SLOT_CLOSED in attempted, (
            "the slot closer was never attempted: the patrol closer's refused lease "
            "propagated out of the member's sweep and skipped it"
        )
        driving = svc.snapshot(slug)["values"].get(types.PROJ_DRIVING, {}) or {}
        assert "worker-1" not in (
            driving.get("open", []) or []
        ), "the slot still reads open after the sweep"
        assert attempted.count(types.PATROL_STOPPED) == 2, (
            f"the patrol closer was attempted {attempted.count(types.PATROL_STOPPED)} time(s): "
            "a member whose closer was refused the lease must still reach the retry pass"
        )
        assert wrote == 1, f"the sweep reported {wrote} closer(s), not the one that landed"

    def test_a_contended_member_is_recovered_by_the_retry_pass(self, monkeypatch):
        """The sweep runs once per boot, so a lost closer must not be lost for good.

        Another process bursts appends into one member's log across the whole first
        attempt, so every closer for it exhausts its tail retries. The burst then
        stops, and the sweep's own second pass places the closers -- without which the
        interrupted slot reads open and the patrol reads armed until the next restart.
        """
        from kiro_crew.eventlog import log as log_mod
        from kiro_crew.eventlog import service as svc_mod

        cfg = _fake_config({CREW: _agent()})
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        _seed_config_baseline(svc, slug, cfg.agents[CREW])
        svc.append(slug, types.PATROL_STARTED, {"slot_key": "member-code-reviewer"})
        svc.append(slug, types.SLOT_OPENED, {"slot_key": "worker-1"})

        autonudge = SimpleNamespace(get_by_slot=lambda key: None)
        state = SimpleNamespace(_slots={})

        # Enough foreign commits to exhaust the first pass's attempts, then silence,
        # so the retry pass runs unobstructed.
        budget = [svc_mod._CLOSER_TAIL_ATTEMPTS]
        real_fold = svc._fold_gap_locked
        bursts = []

        def _fold_then_a_foreign_commit(target, log, *, below=None):
            real_fold(target, log, below=below)
            if target == slug and below is None and budget[0] > 0:
                budget[0] -= 1
                bursts.append(True)
                log_mod.MemberLog(slug).append(
                    types.SLOT_OPENED, {"slot_key": f"foreign-{len(bursts)}"}
                )

        monkeypatch.setattr(svc, "_fold_gap_locked", _fold_then_a_foreign_commit)

        wrote = eventlog_hooks.reconcile_members_at_startup(cfg, state, autonudge)

        monkeypatch.setattr(svc, "_fold_gap_locked", real_fold)
        assert bursts, "the foreign burst never landed; this test proved nothing"
        assert budget[0] == 0, f"the burst was not consumed ({budget[0]} left), so no exhaustion"
        events = svc.history(slug, before=None, limit=None)
        closer_kinds = {(e["type"], e["data"].get("reason")) for e in events}
        assert (
            types.PATROL_STOPPED,
            "interrupted",
        ) in closer_kinds, "the patrol closer was abandoned by the first pass and never retried"
        assert (
            types.SLOT_CLOSED,
            "interrupted",
        ) in closer_kinds, "the slot closer was abandoned by the first pass and never retried"
        # The report must match what is on disk. A closer that landed before a later
        # one lost the tail is still a closer this sweep wrote, and the retry pass
        # cannot recount it -- the landed event changed the projection its predicate
        # reads, so the retry declines it correctly.
        on_disk = len(
            [
                e
                for e in events
                if e["type"] in (types.PATROL_STOPPED, types.SLOT_CLOSED)
                and e["data"].get("reason") == "interrupted"
            ]
        )
        assert wrote == on_disk, (
            f"the sweep reported {wrote} closer(s) but {on_disk} are on disk: a closer "
            "that landed before a later one lost the tail was not counted"
        )

    def test_a_closer_that_landed_before_a_later_one_lost_the_tail_is_counted(self, monkeypatch):
        """The reported count must match the closers on disk.

        A later closer can exhaust its tail retries after an earlier one has already
        landed. A total RETURNED from the per-member step is discarded along with that
        exception, and the retry pass cannot recount it: the landed closer changed the
        very projection its predicate reads, so the retry declines it correctly. The
        events are on disk either way; only the report would be wrong.
        """
        from kiro_crew.eventlog import service as svc_mod

        cfg = _fake_config({CREW: _agent()})
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        _seed_config_baseline(svc, slug, cfg.agents[CREW])
        svc.append(slug, types.PATROL_STARTED, {"slot_key": "member-code-reviewer"})
        svc.append(slug, types.SLOT_OPENED, {"slot_key": "worker-1"})

        autonudge = SimpleNamespace(get_by_slot=lambda key: None)
        state = SimpleNamespace(_slots={})

        # First closer lands; every later one reports exhaustion. No sleeping, no
        # foreign writer: the point is purely what the count does when the step
        # raises after a write.
        real_closer = svc.append_closer_if_still_applies
        calls = []

        def _first_lands_then_contention(target, type, data, **kw):
            calls.append(type)
            if len(calls) == 1:
                return real_closer(target, type, data, **kw)
            raise svc_mod.CloserTailContention(target, type)

        monkeypatch.setattr(svc, "append_closer_if_still_applies", _first_lands_then_contention)

        wrote = eventlog_hooks.reconcile_members_at_startup(cfg, state, autonudge)

        monkeypatch.setattr(svc, "append_closer_if_still_applies", real_closer)
        assert len(calls) >= 2, f"only {len(calls)} closer call(s); the fixture proved nothing"
        on_disk = len(
            [
                e
                for e in svc.history(slug, before=None, limit=None)
                if e["type"] in (types.PATROL_STOPPED, types.SLOT_CLOSED)
                and e["data"].get("reason") == "interrupted"
            ]
        )
        assert on_disk >= 1, "the fixture must land at least one closer to have one to lose"
        assert wrote == on_disk, (
            f"the sweep reported {wrote} closer(s) but {on_disk} are on disk: a closer "
            "that landed before a later one lost the tail was not counted"
        )

    def test_a_colliding_free_form_slug_is_never_reconciled(self, caplog):
        owner = "dr. eggbot"
        other = "Dr. Eggbot"
        slug = members.slug_for_name(owner)
        assert members.slug_for_name(other) == slug
        cfg = _fake_config(
            {owner: _agent(model="owner-model"), other: _agent(model="other-model")},
            default=owner,
        )
        svc = get_service()
        svc.ensure(slug, owner)
        before = svc.last_seq(slug)

        eventlog_hooks.reconcile_members_at_startup(
            cfg, SimpleNamespace(_slots={}), SimpleNamespace(get_by_slot=lambda key: None)
        )

        assert svc.last_seq(slug) == before
        assert "model" not in svc.snapshot(slug)["values"].get(types.PROJ_ROSTER, {})
        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == "kiro_crew.eventlog_hooks" and record.levelname == "WARNING"
        ] == ["member event-log startup reconcile skipped ambiguous slug='dr-eggbot' (2 members)"]

    def test_a_malformed_colliding_claimant_blocks_reconciliation(self, caplog):
        owner = "dr. eggbot"
        malformed = "Dr. Eggbot "
        slug = members.slug_for_name(owner)
        assert members.slug_for_name(malformed) == slug
        cfg = _fake_config(
            {owner: _agent(model="owner-model"), malformed: _agent(model="other-model")},
            default=owner,
        )
        svc = get_service()
        svc.ensure(slug, owner)
        before = svc.last_seq(slug)

        eventlog_hooks.reconcile_members_at_startup(
            cfg, SimpleNamespace(_slots={}), SimpleNamespace(get_by_slot=lambda key: None)
        )

        assert svc.last_seq(slug) == before
        assert "model" not in svc.snapshot(slug)["values"].get(types.PROJ_ROSTER, {})
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.name == "kiro_crew.eventlog_hooks" and record.levelname == "WARNING"
        ]
        assert warnings == [
            "member event-log startup reconcile skipped 1 non-dispatchable name(s) and 0 unresolved slug(s)",
            "member event-log startup reconcile skipped ambiguous slug='dr-eggbot' (2 members)",
        ]
        assert malformed not in caplog.text

    def test_a_foreign_log_header_is_never_reconciled(self, caplog):
        name = "dr. eggbot"
        slug = members.slug_for_name(name)
        cfg = _fake_config({name: _agent(model="member-model")}, default=name)
        svc = get_service()
        svc.ensure(slug, "former member")
        before = svc.last_seq(slug)

        eventlog_hooks.reconcile_members_at_startup(
            cfg, SimpleNamespace(_slots={}), SimpleNamespace(get_by_slot=lambda key: None)
        )

        assert svc.last_seq(slug) == before
        assert svc.logged_name(slug) == "former member"
        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == "kiro_crew.eventlog_hooks" and record.levelname == "WARNING"
        ] == ["member event-log startup reconcile skipped slug='dr-eggbot' with foreign header"]

    def test_invalid_names_and_unresolved_slugs_are_counted_without_values(
        self, caplog, monkeypatch
    ):
        invalid = "unsafe password=shortvalue "
        unresolved = "valid member"
        cfg = _fake_config(
            {invalid: _agent(model="invalid-model"), unresolved: _agent(model="valid-model")},
            default=unresolved,
        )

        real_member_slug = members.member_slug

        def _resolve_or_fail(name, config):
            if name == unresolved:
                raise RuntimeError
            return real_member_slug(name, config)

        monkeypatch.setattr(members, "member_slug", _resolve_or_fail)
        eventlog_hooks.reconcile_members_at_startup(
            cfg, SimpleNamespace(_slots={}), SimpleNamespace(get_by_slot=lambda key: None)
        )

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "kiro_crew.eventlog_hooks" and record.levelname == "WARNING"
        ]
        assert messages == [
            "member event-log startup reconcile skipped 1 non-dispatchable name(s) and 1 unresolved slug(s)"
        ]
        assert invalid not in caplog.text
        assert unresolved not in caplog.text

    def test_reconcile_failure_does_not_log_exception_text(self, caplog, monkeypatch):
        name = "dr. eggbot"
        slug = members.slug_for_name(name)
        cfg = _fake_config({name: _agent(model="member-model")}, default=name)
        svc = get_service()
        svc.ensure(slug, name)
        private_header = "HEADER_PRIVATE_VALUE"

        def _fail_logged_name(_slug):
            raise RuntimeError(private_header)

        monkeypatch.setattr(svc, "logged_name", _fail_logged_name)
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.eventlog_hooks"):
            eventlog_hooks.reconcile_members_at_startup(
                cfg, SimpleNamespace(_slots={}), SimpleNamespace(get_by_slot=lambda key: None)
            )

        assert private_header not in caplog.text
        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == "kiro_crew.eventlog_hooks" and record.levelname == "WARNING"
        ] == ["member event-log startup reconcile failed for slug='dr-eggbot'"]

    def test_a_placeholder_log_header_is_ambiguous_and_never_reconciled(self, caplog):
        """A header equal to the slug names nobody, and a slug can be REUSED, so
        the sweep cannot tell "this member's own log, written before the roster
        knew the name" from "a retired member's log whose slug a new member was
        handed". A write into the wrong log is append-only and unrecoverable;
        the sweep skips and reports the slug, exactly like a foreign header."""
        name = "dr. eggbot"
        slug = members.slug_for_name(name)
        cfg = _fake_config({name: _agent(model="member-model")}, default=name)
        svc = get_service()
        svc.ensure(slug, slug)
        before = svc.last_seq(slug)

        eventlog_hooks.reconcile_members_at_startup(
            cfg, SimpleNamespace(_slots={}), SimpleNamespace(get_by_slot=lambda key: None)
        )

        assert svc.last_seq(slug) == before
        assert svc.logged_name(slug) == slug
        assert "model" not in svc.snapshot(slug)["values"].get(types.PROJ_ROSTER, {})
        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == "kiro_crew.eventlog_hooks" and record.levelname == "WARNING"
        ] == ["member event-log startup reconcile skipped slug='dr-eggbot' with placeholder header"]

    def test_a_recreated_member_handed_a_retired_slug_never_inherits_the_old_log(self, caplog):
        """The reuse sequence: a package-discovered template is registered with no
        ``member_id`` under a hyphenated name that folds onto itself as slug, its
        log is written under that slug, the row is pruned, and a display-name
        member is later created -- the allocator finds nothing reserving the slug
        and hands the new member the identical ``member_id``. Reconciliation
        must not write the new member's configuration into the retired log."""
        retired = "issue-radar"
        slug = members.slug_for_name(retired)
        assert slug == retired, "fixture needs a name that folds onto itself"
        svc = get_service()
        # The retired template's own writer: name == slug, so the header holds
        # the slug (the nameless-writer placeholder shape).
        old_cfg = _fake_config({retired: _agent(model="retired-model")}, default=retired)
        svc.ensure(slug, retired)
        _seed_config_baseline(svc, slug, old_cfg.agents[retired])
        svc.append(slug, types.PATROL_STARTED, {"slot_key": "member-issue-radar"})
        before = svc.last_seq(slug)

        # Pruned, then recreated as a display-name member that was allocated the
        # very same identity (nothing reserved the slug once the row was gone).
        new_name = "Issue Radar"
        assert new_name != slug
        new_cfg = _fake_config(
            {new_name: _agent(member_id=slug, model="new-model")}, default=new_name
        )
        assert members.member_slug(new_name, new_cfg) == slug

        eventlog_hooks.reconcile_members_at_startup(
            new_cfg, SimpleNamespace(_slots={}), SimpleNamespace(get_by_slot=lambda key: None)
        )

        assert svc.last_seq(slug) == before, "the retired log received the new member's events"
        assert svc.logged_name(slug) == retired
        roster = svc.snapshot(slug)["values"][types.PROJ_ROSTER]
        assert roster["model"] == "retired-model"
        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == "kiro_crew.eventlog_hooks" and record.levelname == "WARNING"
        ] == [
            "member event-log startup reconcile skipped slug='issue-radar' with placeholder header"
        ]
        assert new_name not in caplog.text

    def test_an_explicit_member_id_decides_which_log_is_reconciled(self, monkeypatch):
        """Reconcile by persisted identity, not by folding the display name.

        A member carrying an explicit ``member_id`` owns the log at that id. A
        sweep that folds the name instead creates and reconciles a SECOND log,
        so the member's history splits and their real log is never corrected.
        """
        from kiro_crew.config import loader as loader_mod

        # No space: the sweep skips any name failing _AGENT_NAME_RE, so a
        # two-word fixture would be skipped and the test would pass vacuously.
        name = "AliceExample"
        member_id = "alice-two"
        folded = members.slug_for_name(name)
        assert folded != member_id, "fixture must distinguish the two identities"

        cfg = _fake_config({name: _agent(member_id=member_id)}, default=name)
        # The sweep loads config itself, so this fixture must answer that load too --
        # otherwise the reconcile reads the developer's real config, which does not
        # carry this member, and the test passes or fails on the host's contents.
        monkeypatch.setattr(
            loader_mod, "load_config_with_content_stamp", lambda: (cfg, "live-bytes")
        )
        monkeypatch.setattr(loader_mod, "config_content_stamp", lambda: "live-bytes")
        svc = get_service()
        svc.ensure(member_id, name)
        before = svc.last_seq(member_id)

        eventlog_hooks.reconcile_members_at_startup(
            cfg, SimpleNamespace(_slots={}), SimpleNamespace(get_by_slot=lambda key: None)
        )

        assert svc.last_seq(member_id) > before, (
            "the sweep reconciled some other log: the member's own log at their "
            "explicit member_id gained nothing"
        )
        assert folded not in svc.slugs(), (
            f"the sweep folded the name and created a second log at {folded!r}, "
            "so this member's history is split across two files"
        )
