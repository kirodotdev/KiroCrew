"""Member sessions get session control automatically, bounded by ownership.

The crew-member operating model — the DM thread dispatches real work into
worker sessions it creates and patrols — holds with ZERO configuration: a
member caller passes the session-control gates without the global
``agent.session_control`` opt-in, and is bounded to the workers it created
itself instead. These tests pin the three halves of that contract:

* the gate bypass (member caller passes with the switch off; an ordinary
  caller still needs it),
* the ownership boundary (a member cannot touch a slot it did not create,
  even when the global switch is ON),
* the persistence of the boundary's input (``created_by`` written at birth
  and restored on rehydrate — without it every worker a member dispatched
  would come back unowned after a restart and the fail-closed check would
  strand them).

The worker_* kirocrew-core tools ride the same server-side authorization, so
tool-level coverage here is registration only.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.config import loader, validation
from kiro_crew.config.resolution import DEGRADED_MEMBERS, DEGRADED_WHOLE_CONFIG
from kiro_crew.dashboard import session_control as sc
from kiro_crew.members import DM_SLOT_KEY_PREFIX


class TestMemberCallerPredicate:
    def test_member_slot_key_is_a_member_caller(self):
        assert sc._member_caller(DM_SLOT_KEY_PREFIX + "radar")

    def test_ordinary_and_unattended_slots_are_not(self):
        assert not sc._member_caller("chat-1-abc")
        assert not sc._member_caller("cron-xyz")
        assert not sc._member_caller("")


def _slot(key: str, *, created_by: str = "", workspace: str = "default") -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        workspace=workspace,
        memory_mode="persistent",
        _app="",
        linked_session_key="",
        _created_by=created_by,
        mode="",
        running=False,
        messages=[],
    )


class _State:
    def __init__(self, slots: dict[str, SimpleNamespace]):
        self._slots = slots

    def get_slot(self, key: str):
        return self._slots.get(key)


class TestAuthorizeTargetMemberPath:
    """Drive authorize_target through the real gate order with a fake state."""

    def _authorize(self, state, caller_key, target_key):
        # caller_slot_key maps a session key to an open slot; the member path
        # is exercised below the identity resolution, so pin the mapping and
        # the workspace reads to keep the fixture at the authorization layer.
        with (
            patch.object(sc, "caller_slot_key", return_value=caller_key),
            patch.object(sc, "session_control_enabled", return_value=False),
            patch.object(sc, "_resolve_slot", return_value=state._slots.get(target_key)),
        ):
            return sc.authorize_target(
                state,
                caller_session_key="dashboard:whatever",
                target=target_key,
                operation="send",
            )

    def test_member_controls_its_own_worker_with_switch_off(self):
        member = DM_SLOT_KEY_PREFIX + "radar"
        worker = _slot("chat-1-w1", created_by=member)
        state = _State({member: _slot(member), "chat-1-w1": worker})
        try:
            self._authorize(state, member, "chat-1-w1")
        except sc.SessionControlError as exc:
            # Workspace plumbing differs per deployment; the pin is that the
            # member path got PAST the config gate and the ownership check.
            assert exc.code not in ("session_control_disabled", "not_creator"), exc.code

    def test_member_cannot_touch_a_slot_it_did_not_create(self):
        member = DM_SLOT_KEY_PREFIX + "radar"
        foreign = _slot("chat-1-user", created_by="")
        state = _State({member: _slot(member), "chat-1-user": foreign})
        with pytest.raises(sc.SessionControlError) as exc_info:
            self._authorize(state, member, "chat-1-user")
        assert exc_info.value.code == "not_creator"

    def test_ownership_binds_even_when_globally_enabled(self):
        member = DM_SLOT_KEY_PREFIX + "radar"
        foreign = _slot("chat-1-user", created_by="")
        state = _State({member: _slot(member), "chat-1-user": foreign})
        with (
            patch.object(sc, "caller_slot_key", return_value=member),
            patch.object(sc, "session_control_enabled", return_value=True),
            patch.object(sc, "_resolve_slot", return_value=foreign),
        ):
            with pytest.raises(sc.SessionControlError) as exc_info:
                sc.authorize_target(
                    _State(state._slots),
                    caller_session_key="dashboard:whatever",
                    target="chat-1-user",
                    operation="send",
                )
        assert exc_info.value.code == "not_creator"

    def test_ordinary_caller_still_needs_the_switch(self):
        state = _State({"chat-1-a": _slot("chat-1-a"), "chat-1-b": _slot("chat-1-b")})
        with pytest.raises(sc.SessionControlError) as exc_info:
            self._authorize(state, "chat-1-a", "chat-1-b")
        assert exc_info.value.code == "session_control_disabled"


class TestMemberDispatchSwitch:
    """The ``members.dispatch`` operator switch over the member bypass.

    The member bypass at both gates is gated by ``member_dispatch_enabled()``
    (``members.dispatch``, default ON). These tests pin the OFF path — the whole
    point of the change — which the existing member tests never touch because
    the config default resolves the switch ON, so a regression dropping the
    ``member_dispatch_enabled()`` term would leave every other member test green.

    We mirror the existing style: patch ``sc.session_control_enabled`` to the
    global-OFF posture the switch is meant to override, and patch
    ``sc.member_dispatch_enabled`` to flip it. With the switch OFF a member
    caller must be refused with ``session_control_disabled`` at BOTH gates; with
    it ON the bypass must still stand.

    The resolver's own fail-safe direction gets its own cases below. Its ON
    default makes a DEGRADED load — one that succeeded but discarded the value —
    as dangerous as one that raised, which is a case ``agent.session_control``
    (default off) does not have.
    """

    def test_authorize_target_refuses_member_when_dispatch_off(self):
        # Global switch off AND members.dispatch off: the member bypass drops,
        # so a member caller is refused just like any ordinary caller.
        member = DM_SLOT_KEY_PREFIX + "radar"
        worker = _slot("chat-1-w1", created_by=member)
        state = _State({member: _slot(member), "chat-1-w1": worker})
        with (
            patch.object(sc, "caller_slot_key", return_value=member),
            patch.object(sc, "session_control_enabled", return_value=False),
            patch.object(sc, "member_dispatch_enabled", return_value=False),
            patch.object(sc, "_resolve_slot", return_value=worker),
        ):
            with pytest.raises(sc.SessionControlError) as exc_info:
                sc.authorize_target(
                    state,
                    caller_session_key="dashboard:whatever",
                    target="chat-1-w1",
                    operation="send",
                )
        assert exc_info.value.code == "session_control_disabled"

    def test_authorize_target_bypass_stands_when_dispatch_on(self):
        # Global switch off but members.dispatch ON: the bypass holds, so the
        # member gets PAST the config gate (and its own ownership check).
        member = DM_SLOT_KEY_PREFIX + "radar"
        worker = _slot("chat-1-w1", created_by=member)
        state = _State({member: _slot(member), "chat-1-w1": worker})
        with (
            patch.object(sc, "caller_slot_key", return_value=member),
            patch.object(sc, "session_control_enabled", return_value=False),
            patch.object(sc, "member_dispatch_enabled", return_value=True),
            patch.object(sc, "_resolve_slot", return_value=worker),
        ):
            try:
                sc.authorize_target(
                    state,
                    caller_session_key="dashboard:whatever",
                    target="chat-1-w1",
                    operation="send",
                )
            except sc.SessionControlError as exc:
                assert exc.code not in ("session_control_disabled", "not_creator"), exc.code

    def test_create_session_refuses_member_when_dispatch_off(self):
        # The create gate carries the same conjunction. With both switches off a
        # member caller can no longer create a session — the switch narrows it.
        member = DM_SLOT_KEY_PREFIX + "radar"
        state = _State({member: _slot(member)})
        with (
            patch.object(sc, "caller_slot_key", return_value=member),
            patch.object(sc, "session_control_enabled", return_value=False),
            patch.object(sc, "member_dispatch_enabled", return_value=False),
        ):
            with pytest.raises(sc.SessionControlError) as exc_info:
                asyncio.run(sc.create_session(state, caller_session_key="dashboard:whatever"))
        assert exc_info.value.code == "session_control_disabled"

    def test_create_session_bypass_stands_when_dispatch_on(self):
        # members.dispatch ON: the member passes the config gate. It may still
        # fail later on deployment-specific plumbing, but NOT at the switch.
        member = DM_SLOT_KEY_PREFIX + "radar"
        state = _State({member: _slot(member)})
        with (
            patch.object(sc, "caller_slot_key", return_value=member),
            patch.object(sc, "session_control_enabled", return_value=False),
            patch.object(sc, "member_dispatch_enabled", return_value=True),
        ):
            try:
                asyncio.run(sc.create_session(state, caller_session_key="dashboard:whatever"))
            except sc.SessionControlError as exc:
                # A SessionControlError is fine as long as it is NOT the config
                # gate rejecting the member — the bypass must have carried it
                # past the switch.
                assert exc.code != "session_control_disabled", exc.code
            except Exception:
                # Any non-SessionControlError means the member already cleared
                # the config gate and tripped on deployment-specific plumbing
                # our fixture does not provide — exactly what we want to prove.
                pass

    def test_member_dispatch_enabled_fails_closed_on_config_read_error(self):
        # A config read that RAISES resolves to False (fail closed), the same
        # conservative posture as session_control_enabled: the bypass is dropped
        # rather than left silently alive on config corruption.
        with patch.object(sc.KiroCrewConfig, "load", side_effect=RuntimeError("boom")):
            assert sc.member_dispatch_enabled() is False

    @pytest.mark.parametrize(
        "degraded",
        [
            # An unreadable / non-object config FILE. `_mark_file_degraded` adds
            # both the bare marker and a per-file entry; the bare one is what a
            # gate matches on.
            frozenset({DEGRADED_WHOLE_CONFIG, DEGRADED_WHOLE_CONFIG + "config.json"}),
            # A present `members` section that is not a JSON object, e.g.
            # `{"members": []}` — coerced to `{}`, so `dispatch` came back as the
            # dataclass default, not from the operator.
            frozenset({DEGRADED_MEMBERS}),
        ],
    )
    def test_degraded_load_drops_the_bypass_despite_the_on_default(self, degraded):
        # `load()` DEGRADES rather than raising on both shapes: it returns a
        # config whose `members.dispatch` is the True default. Trusting that
        # default would re-grant a bypass an operator had set to false, with no
        # error, and re-reading the file cannot recover the setting because
        # `load()` has already rewritten config.json in normalized form. So the
        # unknown must resolve to off.
        cfg = SimpleNamespace(
            degraded_sections=degraded,
            members=SimpleNamespace(dispatch=True),
        )
        with patch.object(sc.KiroCrewConfig, "load", return_value=cfg):
            assert sc.member_dispatch_enabled() is False

    def test_an_unrelated_degraded_section_leaves_the_bypass_alone(self):
        # Scoped, not blanket: a malformed `slack` section says nothing about
        # what the operator asked for here, and denying on it would turn every
        # unrelated config typo into a silent withdrawal of the zero-config
        # contract this feature exists to provide.
        cfg = SimpleNamespace(
            degraded_sections=frozenset({"slack", "dashboard.tailscale"}),
            members=SimpleNamespace(dispatch=True),
        )
        with patch.object(sc.KiroCrewConfig, "load", return_value=cfg):
            assert sc.member_dispatch_enabled() is True

    def test_a_clean_load_reports_the_operators_value_either_way(self):
        # The degraded guard must not swallow the ordinary path: a clean load
        # returns exactly what the operator set.
        for value, expected in ((True, True), (False, False)):
            cfg = SimpleNamespace(
                degraded_sections=frozenset(),
                members=SimpleNamespace(dispatch=value),
            )
            with patch.object(sc.KiroCrewConfig, "load", return_value=cfg):
                assert sc.member_dispatch_enabled() is expected

    #: Every ``members`` shape an operator can actually write, and the grant each
    #: must resolve to. Absence is the only one that may resolve ON.
    _END_TO_END_SHAPES = [
        # No config at all, and an empty section: the zero-configuration grant.
        ({}, True),
        ({"members": {}}, True),
        ({"members": {"dispatch": True}}, True),
        # An explicit withdrawal, and the same withdrawal written by an editor
        # that quoted it. `bool("false")` is True, so the quoted form is the one
        # that used to hand back the opposite of what the operator read.
        ({"members": {"dispatch": False}}, False),
        ({"members": {"dispatch": "false"}}, False),
        # A `members` section that is not an object: whatever it carried is gone,
        # so the operator's intent is UNKNOWN and cannot be read as consent.
        ({"members": []}, False),
        ({"members": "nope"}, False),
        # Scoped: an unrelated malformed section says nothing about this grant.
        ({"slack": [], "members": {"dispatch": True}}, True),
    ]

    @pytest.mark.parametrize("payload,expected", _END_TO_END_SHAPES)
    def test_end_to_end_from_a_real_config_file(self, tmp_path, payload, expected):
        # The mocked cases above hand `member_dispatch_enabled()` a hand-built
        # `degraded_sections`, which proves the gate reads it but NOT that the
        # loader ever puts `members` there. Three layers have to cooperate for
        # that -- validation preserving the malformed value, the loader recording
        # the coercion, the gate reading it -- and only a real config.json
        # exercises all three. Without this test the guard passed while being
        # unreachable in production: the advisory jsonschema pass repaired the
        # value before `_coerced_section` could witness it.
        (tmp_path / "config.json").write_text(json.dumps(payload), encoding="utf-8")
        loader._invalidate_config_cache()
        with patch.object(loader, "config_dir", return_value=tmp_path):
            assert sc.member_dispatch_enabled() is expected
        loader._invalidate_config_cache()

    def test_the_members_paths_are_exempt_from_validation_repair(self):
        # The registry entry is load-bearing, not decoration: `_apply_field_default`
        # deletes a schema-violating value, and for a grant whose default is ON
        # that deletion IS the widening -- it happens before any detector runs.
        assert "members" in validation._FAIL_CLOSED_PATHS
        assert "members.dispatch" in validation._FAIL_CLOSED_PATHS
        data = {"members": {"dispatch": "false"}}
        assert validation._apply_field_default(data, "members.dispatch") is False
        assert data == {"members": {"dispatch": "false"}}

    def test_degraded_section_key_matches_the_real_config_field(self):
        # `DEGRADED_MEMBERS` is the string `degraded_sections` reports for this
        # section, which is the config dataclass's FIELD name. Renaming the
        # section without updating the constant would not fail any behavioural
        # test — the gate would simply stop matching and fall back to reading the
        # ON default, i.e. it would fail OPEN silently. Pin the two together.
        assert DEGRADED_MEMBERS in {f.name for f in fields(sc.KiroCrewConfig)}


class TestWorkerToolsRegistered:
    def test_worker_tools_advertised_on_kirocrew_core(self):
        from kiro_crew.mcp_tools import build_tool_list

        names = {t["name"] for t in build_tool_list()}
        assert {"worker_create", "worker_send", "worker_read", "worker_stop"} <= names

    def test_worker_domain_schema_and_handlers_agree(self):
        from kiro_crew.mcp_tools import workers

        advertised = {t["name"] for t in workers.schemas()}
        assert advertised == set(workers.HANDLERS)


class TestWorkerSessionSchemaParity:
    """Each ``WORKER_*_SCHEMA`` must stay within its ``SESSION_*_SCHEMA`` twin.

    The four ``worker_*`` kirocrew-core tools and the four ``session_*``
    kirocrew-dashboard tools forward to the SAME ``/api/session-control/*``
    gateway endpoints, so the shape those endpoints accept must not depend on
    which server the caller happened to reach. Today that parity is held only
    by the comment above ``WORKER_CREATE_SCHEMA`` in ``validation.py``; this
    test turns it into an executable invariant.

    Parity here is a *narrowing*, not byte equality: ``worker_create``
    deliberately omits ``folder`` (a member's workers land unfiled in v1, per
    that comment). So for each pair we assert the worker property set is a
    subset of the session set (the worker introduces no field the endpoint
    would reject), and that the only fields the session set carries beyond the
    worker set are the explicitly documented ``allowed_missing`` ones. A field
    added to a ``session_*`` schema but forgotten on ``worker_*`` — or an
    accidental extra worker narrowing — both trip this test.
    """

    # worker schema -> (session schema, properties worker may omit)
    @classmethod
    def _pairs(cls):
        from kiro_crew import validation as v

        return [
            (v.WORKER_CREATE_SCHEMA, v.SESSION_CREATE_SCHEMA, {"folder"}),
            (v.WORKER_SEND_SCHEMA, v.SESSION_SEND_SCHEMA, set()),
            (v.WORKER_READ_SCHEMA, v.SESSION_READ_MESSAGE_SCHEMA, set()),
            (v.WORKER_STOP_SCHEMA, v.SESSION_STOP_SCHEMA, set()),
        ]

    def test_worker_property_sets_stay_within_session_counterparts(self):
        for worker_schema, session_schema, allowed_missing in self._pairs():
            worker_props = {f.name for f in worker_schema.fields}
            session_props = {f.name for f in session_schema.fields}
            # (a) The worker surface never introduces a property the shared
            # endpoint would not accept from the session surface.
            assert worker_props <= session_props, (
                f"{worker_schema.tool_name} introduces "
                f"{worker_props - session_props} absent from "
                f"{session_schema.tool_name}"
            )
            # (b) The ONLY narrowing is the documented one — a new session_*
            # field forgotten on worker_* (or an accidental worker omission)
            # fails here.
            assert session_props - worker_props == allowed_missing, (
                f"{worker_schema.tool_name} vs {session_schema.tool_name}: "
                f"expected only {allowed_missing} to be omitted, got "
                f"{session_props - worker_props}"
            )

    def test_shared_fields_have_compatible_type_and_required(self):
        # A shared field must not just carry the same type/required flag: the
        # value bounds that decide what the shared /api/session-control/*
        # endpoint ACCEPTS must line up too. A future divergence in max_len,
        # min_val, max_val, pattern, or the enum allow-list between a worker_*
        # and its session_* twin would otherwise pass here while making the
        # accepted payload depend on which server the caller reached — the exact
        # failure mode this parity test exists to prevent.
        def _pattern_src(field):
            # FieldSpec.pattern is a compiled re.Pattern (or None); compare the
            # source string so two independently-compiled equivalents match and
            # a genuine divergence is still caught.
            pat = field.pattern
            return None if pat is None else pat.pattern

        for worker_schema, session_schema, _allowed_missing in self._pairs():
            worker_fields = {f.name: f for f in worker_schema.fields}
            session_fields = {f.name: f for f in session_schema.fields}
            for name in worker_fields.keys() & session_fields.keys():
                wf = worker_fields[name]
                sf = session_fields[name]
                assert wf.type == sf.type, (
                    f"{worker_schema.tool_name}.{name} type {wf.type!r} != "
                    f"{session_schema.tool_name}.{name} type {sf.type!r}"
                )
                assert wf.required == sf.required, (
                    f"{worker_schema.tool_name}.{name} required={wf.required} != "
                    f"{session_schema.tool_name}.{name} required={sf.required}"
                )
                # Value bounds — everything on FieldSpec that constrains the
                # accepted input for a shared field.
                for attr in ("default", "max_len", "min_val", "max_val", "allowed"):
                    assert getattr(wf, attr) == getattr(sf, attr), (
                        f"{worker_schema.tool_name}.{name} {attr}="
                        f"{getattr(wf, attr)!r} != {session_schema.tool_name}.{name} "
                        f"{attr}={getattr(sf, attr)!r}"
                    )
                assert _pattern_src(wf) == _pattern_src(sf), (
                    f"{worker_schema.tool_name}.{name} pattern "
                    f"{_pattern_src(wf)!r} != {session_schema.tool_name}.{name} "
                    f"pattern {_pattern_src(sf)!r}"
                )


class TestCreatedByRecentSessionRestore:
    """created_by must survive the bulk recent-session restore path too.

    _rehydrate_slot_from_history restores it, but the startup path is
    _apply_recent_session — a member-created worker restored there without
    created_by comes back unowned, and authorize_target then refuses the
    legitimate creator with not_creator.
    """

    def test_recent_session_restore_rehydrates_created_by(self, tmp_path, monkeypatch):
        import json as _json
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.dashboard.chat import restore_recent_sessions
        from kiro_crew.dashboard.state import DashboardState
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        meta_line = {
            "_type": "metadata",
            "created_at": "2026-03-23T10:00:00",
            "last_consolidated": 0,
            "title": "Worker",
            "agent": "kirocrew",
            "created_by": "member-autofix",
        }
        rows = [
            _json.dumps(meta_line),
            _json.dumps({"role": "user", "content": "task", "ts": "2026-03-23T10:00:00"}),
        ]
        path = tmp_path / "dashboard_chat-1-worker.jsonl"
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        path.touch()

        sessions = MagicMock(count=0)
        sessions.get_pid = MagicMock(return_value=None)
        sessions.remove = AsyncMock()
        state = DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )
        assert restore_recent_sessions(state, window_minutes=60) == 1
        assert state._slots["chat-1-worker"]._created_by == "member-autofix"


class TestWorkerHandlers:
    """Behavioral coverage of the four handlers.

    Handlers reach shared plumbing as call-time attributes of ``mcp_core``
    (by design -- see workers.py's module docstring), so patching the
    attributes on the module intercepts every call. Each handler shares the
    identity/error shape, so those are pinned once on worker_create and the
    tool-specific reply branches are pinned per tool.
    """

    def _patch(self, monkeypatch, *, caller="member-autofix", post=None, get=None):
        from kiro_crew import mcp_core

        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: caller)
        recorded: dict[str, object] = {}
        if post is not None:

            def _post(path, payload, session_key=""):
                recorded["path"] = path
                recorded["payload"] = payload
                recorded["session_key"] = session_key
                return post

            monkeypatch.setattr(mcp_core, "_post", _post)
        if get is not None:

            def _get(path, session_key=""):
                recorded["path"] = path
                recorded["session_key"] = session_key
                return get

            monkeypatch.setattr(mcp_core, "_get", _get)
        return recorded

    def test_create_success_names_the_worker(self, monkeypatch):
        from kiro_crew.mcp_tools import workers

        rec = self._patch(monkeypatch, post={"target": "chat-1-w1", "title": "Fix issue"})
        out = workers.worker_create("worker_create", {"title": "Fix issue"})
        assert "chat-1-w1" in out and "worker_send" in out
        assert rec["path"] == "/api/session-control/create"
        assert rec["payload"] == {"title": "Fix issue"}
        assert rec["session_key"] == "member-autofix"

    def test_create_error_is_reported_not_raised(self, monkeypatch):
        from kiro_crew.mcp_tools import workers

        self._patch(monkeypatch, post={"error": "forbidden"})
        out = workers.worker_create("worker_create", {})
        assert out.startswith("Error:") and "forbidden" in out

    def test_non_member_and_unidentified_callers_are_refused_before_any_request(self, monkeypatch):
        from kiro_crew import mcp_core
        from kiro_crew.mcp_tools import workers

        def _boom(*a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("no request may leave without a member identity")

        monkeypatch.setattr(mcp_core, "_post", _boom)
        monkeypatch.setattr(mcp_core, "_get", _boom)
        # An ordinary chat session, a session-key-prefixed ordinary session,
        # and an unidentifiable caller are all refused with the same pointer
        # to the assigned session_* surface — worker_* answers only members.
        for caller in ("chat-1-ordinary", "dashboard_chat-1-ordinary", ""):
            monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda c=caller: c)
            for handler, args in (
                (workers.worker_create, {}),
                (workers.worker_send, {"target": "t", "message": "m"}),
                (workers.worker_read, {"target": "t"}),
                (workers.worker_stop, {"target": "t"}),
            ):
                out = handler("x", args)
                assert "answer only for a crew member" in out

    def test_member_key_accepted_in_both_spellings(self, monkeypatch):
        from kiro_crew.mcp_tools import workers

        for caller in ("member-autofix", "dashboard_member-autofix"):
            rec = self._patch(monkeypatch, caller=caller, post={"target": "w"})
            workers.worker_stop("worker_stop", {"target": "w"})
            assert rec["session_key"] == caller

    def test_send_distinguishes_started_from_queued(self, monkeypatch):
        from kiro_crew.mcp_tools import workers

        self._patch(monkeypatch, post={"target": "chat-1-w1", "started": True})
        started = workers.worker_send("worker_send", {"target": "chat-1-w1", "message": "go"})
        assert "started a turn" in started

        self._patch(monkeypatch, post={"target": "chat-1-w1", "started": False})
        queued = workers.worker_send("worker_send", {"target": "chat-1-w1", "message": "go"})
        assert "Queued" in queued

    def test_read_renders_transcript_state_and_cursor(self, monkeypatch):
        from kiro_crew.mcp_tools import workers

        rec = self._patch(
            monkeypatch,
            get={
                "target": "chat-1-w1",
                "title": "Fix issue",
                "running": True,
                "queue_depth": 2,
                "total": 5,
                "messages": [{"role": "assistant", "content": "done step 1"}],
                "next_since": 5,
            },
        )
        out = workers.worker_read("worker_read", {"target": "chat-1-w1", "limit": 10, "since": 3})
        assert "still working" in out and "2 message(s) queued" in out
        assert "[assistant] done step 1" in out
        assert "since=5" in out
        assert "target=chat-1-w1" in str(rec["path"]) and "since=3" in str(rec["path"])

    def test_read_empty_window_and_idle_state(self, monkeypatch):
        from kiro_crew.mcp_tools import workers

        self._patch(
            monkeypatch,
            get={"target": "chat-1-w1", "title": "t", "running": False, "messages": []},
        )
        out = workers.worker_read("worker_read", {"target": "chat-1-w1"})
        assert "idle" in out and "No messages in that window yet." in out

    def test_read_error_is_reported(self, monkeypatch):
        from kiro_crew.mcp_tools import workers

        self._patch(monkeypatch, get={"error": "not_creator"})
        out = workers.worker_read("worker_read", {"target": "chat-1-x"})
        assert out.startswith("Error:") and "not_creator" in out

    def test_stop_branches_sent_already_stopping_and_noop(self, monkeypatch):
        from kiro_crew.mcp_tools import workers

        self._patch(monkeypatch, post={"target": "chat-1-w1"})
        assert "Stop sent" in workers.worker_stop("worker_stop", {"target": "chat-1-w1"})

        self._patch(
            monkeypatch,
            post={"target": "chat-1-w1", "info": "already stopping", "already_stopping": True},
        )
        assert "still stands" in workers.worker_stop("worker_stop", {"target": "chat-1-w1"})

        self._patch(monkeypatch, post={"target": "chat-1-w1", "info": "no turn running"})
        assert "nothing to stop" in workers.worker_stop("worker_stop", {"target": "chat-1-w1"})

    def test_send_error_is_reported(self, monkeypatch):
        from kiro_crew.mcp_tools import workers

        self._patch(monkeypatch, post={"error": "workspace_mismatch"})
        out = workers.worker_send("worker_send", {"target": "t", "message": "m"})
        assert out.startswith("Error:") and "workspace_mismatch" in out
