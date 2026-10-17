"""Per-app WebSocket event scope: how each frame shape is classified for an app socket.

Every event name that can reach an app socket is classified (the build fails on an unclassified
broadcast name); then the legacy ``"*"`` declaration, notification sources, app-published
``app_event`` frames, ``approval_resolved`` carrying its slot, ``/api/status`` once it needs a
declaration, a global event carrying a slot field, coalesced subagent batches filtered per item,
and the owner-only dashboard field frame.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import ws_event_scoping_helpers
from ws_event_scoping_helpers import _allowed, _make_slot, _make_state

from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.dashboard.token_auth import app_token_path_allowed
from kiro_crew.dashboard.ws_event_scope import build_allowed_event_set, ws_event_allowed

# The shared fixtures, bound here by name so pytest registers them for this module.
_clear_module_caches = ws_event_scoping_helpers._clear_module_caches
_clear_ws_scope_module_caches = ws_event_scoping_helpers._clear_ws_scope_module_caches


class TestEventTableCompleteness:
    """Every event that can reach an app socket must be classified.

    ``ws_event_allowed`` DENIES unknown event types (deny-by-default, CWE-269).
    That is the right default, but it makes an unclassified event a *silent*
    loss of functionality for apps rather than a loud error -- the failure mode
    ``workflow_run_event`` (the one declaration that exists in the tree) sits
    closest to.

    This test closes that class of bug: it walks the source for literal event
    names published onto the WS fan-out and asserts each one is classified.
    New events therefore fail the build here instead of vanishing in
    production.
    """

    def test_fire_event_names_are_classified(self):
        """The emitter path a literal-scanning guard cannot see.

        ``SubagentManager._fire_event`` names its event, and the Slack gateway
        dispatcher forwards it with ``broadcast_ws(etype, ...)`` -- a VARIABLE, so
        scanning broadcast call sites for string literals finds none of these.
        Read the literals at the emitter instead. An unclassified name here hits
        the unknown-event floor and is denied to EVERY app regardless of what it
        declared, which is silent loss of the whole subagent stream rather than a
        loud error.
        """
        source_root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        source_paths = [source_root / "subagent.py"]
        source_paths.extend(sorted((source_root / "subagent_manager").glob("*.py")))
        source_paths.extend(sorted((source_root / "subagent_parts").glob("*.py")))
        src = "\n".join(path.read_text(encoding="utf-8") for path in source_paths)
        # `_fire_event(` then the first string literal, across a line break.
        fired = set(re.findall(r'_fire_event\(\s*\n?\s*"([a-z_]+)"', src))
        assert len(fired) >= 8, f"emitter scan found too few names: {sorted(fired)}"
        unclassified = sorted(fired - self._classified())
        assert not unclassified, (
            "these events are fired to the WS fan-out but classified in no "
            f"ws_event_scope table, so every app token is denied them: {unclassified}"
        )

    def test_subagent_events_are_a_subset_of_slot_scoped(self):
        """A structural invariant the tables rely on but never state.

        The ``subagent:*`` vocabulary WIDENS slot visibility; it is not a second,
        independent tier. A member outside ``_SLOT_SCOPED_EVENTS`` falls to the
        unknown-event floor and is denied outright, and the SDK's slot-scoped
        drift loop stops covering it.
        """
        from kiro_crew.dashboard import ws_event_scope as mod

        assert mod._SUBAGENT_EVENTS <= mod._SLOT_SCOPED_EVENTS, (
            mod._SUBAGENT_EVENTS - mod._SLOT_SCOPED_EVENTS
        )

    @staticmethod
    def _classified() -> frozenset[str]:
        from kiro_crew.dashboard import ws_event_scope as m

        return frozenset(
            set(m._TIER0_ALWAYS)
            | set(m._SLOT_SCOPED_EVENTS)
            | set(m._SUBAGENT_EVENTS)
            | set(m._GLOBAL_EVENT_DECLARATIONS)
            # ``slots`` is short-circuited in the gate and filtered at the
            # payload layer instead (_serialize_for_client), so it is
            # deliberately absent from the tables.
            | {"slots"}
        )

    def test_every_broadcast_event_name_is_classified(self):
        import ast
        from pathlib import Path

        import kiro_crew

        root = Path(kiro_crew.__file__).parent
        # Owner-only fan-outs never reach an app socket, so their event names
        # need no classification.
        gated = {"broadcast_ws", "broadcast_ws_subagent_subscribers"}
        unclassified: list[tuple[str, int, str]] = []
        classified = self._classified()

        for path in root.rglob("*.py"):
            if "/builtins/" in str(path):
                continue  # app code, not gateway fan-out
            try:
                source = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            # A call to a gated name carries that identifier in the source text, so
            # a file naming none of them has nothing for the AST walk to find. Parsing
            # only the files that can match (~35 of ~1,170) turns a 4-6 s full-tree
            # parse into ~0.5 s without narrowing what the guard can see: the filter
            # is derived from ``gated``, so a name added there widens it too.
            if not any(name in source for name in gated):
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            # Module-level ``NAME = "literal"`` bindings, so a constant passed
            # as the event type is resolved rather than skipped.
            const_strs: dict[str, str] = {
                t.id: n.value.value
                for n in tree.body
                if isinstance(n, ast.Assign)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)
                for t in n.targets
                if isinstance(t, ast.Name)
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name not in gated or not node.args:
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    event_name = first.value
                elif isinstance(first, ast.Name) and first.id in const_strs:
                    # Module-level string constant (e.g. SIDE_RESULT_EVENT) —
                    # resolving these matters: ``chat.side_result`` is passed
                    # this way and a literal-only scan silently misses it.
                    event_name = const_strs[first.id]
                else:
                    continue  # dynamic event type -- cannot be checked statically
                if event_name not in classified:
                    unclassified.append((str(path.relative_to(root)), first.lineno, event_name))

        assert not unclassified, (
            "These WS events are broadcast but not classified in "
            "ws_event_scope.py, so app tokens will silently never receive "
            f"them: {unclassified}. Add each to _TIER0_ALWAYS, "
            "_SLOT_SCOPED_EVENTS (+_SUBAGENT_EVENTS if applicable), or "
            "_GLOBAL_EVENT_DECLARATIONS."
        )

    def test_broadcast_type_map_events_are_classified(self):
        """``_broadcast`` translates internal ``_type`` values into WS events.

        That second dispatch path funnels through the same chokepoint, so its
        event names must be classified too -- a literal scan of
        ``broadcast_ws`` calls alone would miss them (chat_message and the
        slots re-push both travel this way).
        """
        for event in (
            "slots",
            "slot_title",
            "refresh",
            "update_progress",
            "artifact_update",
            "chat_message",
            "notification",
        ):
            assert event in self._classified(), (
                f"{event!r} is emitted by DashboardState._broadcast but is not "
                "classified in ws_event_scope.py"
            )


# ---------------------------------------------------------------------------
# The legacy "*" declaration
# ---------------------------------------------------------------------------


class TestWildcardDeclaration:
    """``permissions.events: ["*"]`` predates this vocabulary and meant every
    event. Carried through as an opaque set member no gate recognises it, so
    such a manifest would keep its subscription and receive nothing."""

    def test_wildcard_expands_instead_of_passing_through(self):
        from kiro_crew.dashboard.ws_event_scope import build_allowed_event_set

        allowed = build_allowed_event_set(["*"])
        assert "*" not in allowed, "the wildcard must be expanded, not stored"
        assert "slots:all" in allowed
        assert "subagent:all" in allowed
        assert "notification:all" in allowed

    def test_wildcard_covers_every_global_declaration(self):
        from kiro_crew.dashboard.ws_event_scope import (
            _GLOBAL_EVENT_DECLARATIONS,
            build_allowed_event_set,
        )

        allowed = build_allowed_event_set(["*"])
        missing = sorted(d for d in set(_GLOBAL_EVENT_DECLARATIONS.values()) if d not in allowed)
        assert not missing, f"wildcard omits declarations: {missing}"

    def test_ordinary_declarations_pass_through_unchanged(self):
        from kiro_crew.dashboard.ws_event_scope import build_allowed_event_set

        assert build_allowed_event_set(["slots:own", "notification"]) == frozenset(
            {"slots:own", "notification"}
        )


# ---------------------------------------------------------------------------
# Notification source: canonical, prefixed, and system-only
# ---------------------------------------------------------------------------


class TestNotificationSourceParsing:
    """The note's field is ``source`` and an app push is ``app:<name>``.

    The gate must not read ``source_app``/``app`` -- keys no emitter writes -- so
    it saw "" for every note and accepted "" as the system stream: one app's
    private push reached any app holding ``notification:system``, and an app
    holding ``notification`` never saw its own.
    """

    def test_constants_match_the_real_emitters(self):
        """Pin the two producers, so a rename on either side fails here."""
        from kiro_crew.dashboard import ws_event_scope as wes
        from kiro_crew.notifications import bus

        # state.notify() -> payload_from_legacy() stamps this exact value.
        assert wes._SYSTEM_SOURCE == bus._SYSTEM_SOURCE

        # api_push_notification builds `source=f"app:{app_name}"`.
        push = Path(
            __import__("kiro_crew.dashboard.handlers.notifications_push", fromlist=["x"]).__file__
        ).read_text(encoding="utf-8")
        assert f'source=f"{wes._APP_SOURCE_PREFIX}{{app_name}}"' in push

    def test_parses_the_app_identity_out_of_the_prefix(self):
        from kiro_crew.dashboard.ws_event_scope import notification_source_app

        assert notification_source_app("app:mochi-pet") == "mochi-pet"
        # A bare name is NOT an app push: it never appears on the wire, and
        # treating it as one would accept a shape the server does not produce.
        assert notification_source_app("mochi-pet") == ""
        assert notification_source_app("system") == ""
        assert notification_source_app("") == ""

    def test_own_push_reaches_only_its_own_app(self):
        state = _make_state()
        note = {"source": "app:mochi-pet", "text": "hi"}
        assert (
            ws_event_allowed(
                "notification",
                note,
                app="mochi-pet",
                allowed_events=_allowed("notification"),
                state=state,
            )
            is True
        )
        # The leak: a SECOND app holding the system scope must not receive it.
        assert (
            ws_event_allowed(
                "notification",
                note,
                app="other-app",
                allowed_events=_allowed("notification:system"),
                state=state,
            )
            is False
        )
        assert (
            ws_event_allowed(
                "notification",
                note,
                app="other-app",
                allowed_events=_allowed("notification"),
                state=state,
            )
            is False
        )

    def test_only_the_literal_system_source_is_the_system_stream(self):
        state = _make_state()
        for source in ("app:other-app", "notifications_api", "", "System", "sys"):
            assert (
                ws_event_allowed(
                    "notification",
                    {"source": source},
                    app="mochi-pet",
                    allowed_events=_allowed("notification:system"),
                    state=state,
                )
                is False
            ), f"{source!r} must not read as the system stream"
        assert (
            ws_event_allowed(
                "notification",
                {"source": "system"},
                app="mochi-pet",
                allowed_events=_allowed("notification:system"),
                state=state,
            )
            is True
        )

    def test_ack_events_need_the_broad_scope(self):
        """`notification_ack`/`_unack` broadcast a bare `{"ts": ...}`.

        Nothing in that payload says WHOSE notification was acked, so own-only
        `notification` cannot be honoured for them. Letting them ride the plain
        declaration -- which is what removing them from the source filter did --
        handed an app the ack stream for every other app's and the system's
        notifications. Unattributable metadata takes the broad scope.
        """
        from kiro_crew.dashboard.ws_event_scope import (
            _SOURCE_FILTERED_EVENTS,
            _UNATTRIBUTED_NOTIFICATION_EVENTS,
        )

        state = _make_state()
        for event in ("notification_ack", "notification_unack"):
            # Not source-filtered (no `source` field to filter on) but still not
            # covered by the own-only declaration.
            assert event not in _SOURCE_FILTERED_EVENTS
            assert event in _UNATTRIBUTED_NOTIFICATION_EVENTS
            payload = {"ts": "2026-08-04T00:00:00Z"}
            assert (
                ws_event_allowed(
                    event,
                    payload,
                    app="mochi-pet",
                    allowed_events=_allowed("notification"),
                    state=state,
                )
                is False
            )
            assert (
                ws_event_allowed(
                    event,
                    payload,
                    app="mochi-pet",
                    allowed_events=_allowed("notification:system"),
                    state=state,
                )
                is False
            )
            assert (
                ws_event_allowed(
                    event,
                    payload,
                    app="mochi-pet",
                    allowed_events=_allowed("notification:all"),
                    state=state,
                )
                is True
            )
            assert (
                ws_event_allowed(
                    event,
                    payload,
                    app="mochi-pet",
                    allowed_events=_allowed("log"),
                    state=state,
                )
                is False
            )


# ---------------------------------------------------------------------------
# App-published events ride one fixed WS type
# ---------------------------------------------------------------------------


class TestAppEventFrames:
    """Every app event arrives as WS type ``app_event`` with the real name inside.

    The scope table is keyed by WS type, so the lookup could never match and each
    frame fell through to the global deny -- an app silently stopped receiving its
    OWN events the moment scoping landed.
    """

    def test_constant_matches_the_event_bus(self):
        """Pin the mirrored constant against the app layer's own definition."""
        from kiro_crew.apps.event_bus import APP_EVENT_WS_TYPE
        from kiro_crew.dashboard.ws_event_scope import _APP_EVENT_WS_TYPE

        assert _APP_EVENT_WS_TYPE == APP_EVENT_WS_TYPE

    def _frame(self, publisher: str, event: str) -> dict:
        # Shape produced by event_bus.build_broadcast_fn: the inner `type` is
        # renamed to `event` and rides beside `app` in the envelope.
        return {"app": publisher, "event": event, "data": {"n": 1}}

    def test_publisher_receives_its_own_event(self):
        state = _make_state()
        assert (
            ws_event_allowed(
                "app_event",
                self._frame("mochi", "tick"),
                app="mochi",
                allowed_events=_allowed("slots:own"),
                state=state,
            )
            is True
        )

    def test_wildcard_app_still_receives_its_own_event(self):
        """`events: ["*"]` expands into CORE scopes, which cannot contain an
        app-chosen event name -- so ownership, not a second declaration lookup,
        has to decide, or every wildcard app loses its own events."""
        from kiro_crew.dashboard.ws_event_scope import build_allowed_event_set

        state = _make_state()
        assert (
            ws_event_allowed(
                "app_event",
                self._frame("mochi", "tick"),
                app="mochi",
                allowed_events=build_allowed_event_set(["*"]),
                state=state,
            )
            is True
        )

    def test_another_apps_event_is_denied(self):
        """App events have no cross-app opt-in -- exposeToApps covers slots."""
        state = _make_state()
        assert (
            ws_event_allowed(
                "app_event",
                self._frame("workflows", "tick"),
                app="mochi",
                allowed_events=_allowed("slots:all", "notification:all"),
                state=state,
            )
            is False
        )

    def test_unattributable_envelope_is_denied(self):
        state = _make_state()
        for frame in ({"event": "tick"}, {"app": "mochi"}, {}, {"app": "", "event": ""}):
            assert (
                ws_event_allowed(
                    "app_event",
                    frame,
                    app="mochi",
                    allowed_events=_allowed("slots:own"),
                    state=state,
                )
                is False
            ), f"{frame!r} must be denied"


class TestConstantValuedEventNamesAreClassified:
    """The completeness guard scans broadcast CALL SITES for literal names, so an
    event whose WS type comes from a CONSTANT is invisible to it -- which is how
    `app_event` shipped unclassified while the guard stayed green. Pin the known
    constant-valued types explicitly."""

    def test_app_event_is_classified(self):
        from kiro_crew.apps.event_bus import APP_EVENT_WS_TYPE
        from kiro_crew.dashboard import ws_event_scope as wes

        src = Path(wes.__file__).read_text(encoding="utf-8")
        assert APP_EVENT_WS_TYPE in src, (
            f"{APP_EVENT_WS_TYPE!r} is broadcast via a constant and must still be "
            "classified in ws_event_scope"
        )


# ---------------------------------------------------------------------------
# ``approval_resolved`` is slot-scoped, so the gate denies any frame it cannot
# attribute to a slot. The producers originally sent only {id, approved}, which
# meant an app received its approval REQUEST but never the RESOLUTION.
# ---------------------------------------------------------------------------


class TestApprovalResolvedCarriesSlot:
    @staticmethod
    def _state():
        from unittest.mock import AsyncMock

        from chat_test_helpers import _make_ready_kiro_prerequisite

        from kiro_crew.dashboard.state import DashboardState

        sessions = MagicMock(count=0)
        sessions.remove = AsyncMock()
        sessions.get_pid = MagicMock(return_value=None)
        state = DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=MagicMock(),
        )
        state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
        return state

    def test_slot_level_resolution_carries_owning_slot(self):
        """Drive the REAL resolve_approval and assert the broadcast payload."""
        import asyncio

        state = self._state()
        slot = state.get_or_create_slot("mochi-pet")
        slot._app = "mochi-pet"
        slot._origin = SlotOrigin.APP

        async def _run():
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            slot._approval_futures["a1"] = fut
            with patch.object(state, "broadcast_ws") as bws, patch("kiro_crew.dashboard.state.sel"):
                state.resolve_approval("a1", True)
            return [c for c in bws.call_args_list if c.args[0] == "approval_resolved"]

        calls = asyncio.run(_run())
        assert len(calls) == 1
        payload = calls[0].args[1]
        assert payload["id"] == "a1"
        assert payload["slot"] == "mochi-pet"

    def test_state_level_resolution_stays_unattributed(self):
        """A background (cron/subagent/gateway) approval owns no slot, so it
        must NOT claim one — the gate correctly withholds it from app tokens."""
        import asyncio

        state = self._state()

        async def _run():
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            state._approval_futures["b1"] = fut
            with patch.object(state, "broadcast_ws") as bws, patch("kiro_crew.dashboard.state.sel"):
                state.resolve_state_approval("b1", True)
            return [c for c in bws.call_args_list if c.args[0] == "approval_resolved"]

        calls = asyncio.run(_run())
        assert len(calls) == 1
        assert "slot" not in calls[0].args[1]

    def test_owning_app_receives_its_own_resolution(self):
        """End-to-end: the payload the real producer emits must pass the gate
        for the slot's owning app with NO extra declaration."""
        import asyncio

        state = self._state()
        slot = state.get_or_create_slot("mochi-pet")
        slot._app = "mochi-pet"
        slot._origin = SlotOrigin.APP

        async def _run():
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            slot._approval_futures["a1"] = fut
            with patch.object(state, "broadcast_ws") as bws, patch("kiro_crew.dashboard.state.sel"):
                state.resolve_approval("a1", True)
            return next(c.args[1] for c in bws.call_args_list if c.args[0] == "approval_resolved")

        payload = asyncio.run(_run())
        assert ws_event_allowed(
            "approval_resolved",
            payload,
            app="mochi-pet",
            allowed_events=frozenset(),
            state=state,
        )
        # ...and a DIFFERENT app still must not see it.
        assert not ws_event_allowed(
            "approval_resolved",
            payload,
            app="other-app",
            allowed_events=frozenset(),
            state=state,
        )

    def test_every_approval_resolved_producer_sends_a_slot(self):
        """Source guard: a new producer that omits the slot key would silently
        withhold the resolution from the owning app. Pin every broadcast site."""
        import re

        dashboard = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard"
        roots = [
            dashboard / f
            for f in (
                "interaction_coordinator.py",
                "chat_handlers.py",
                "chat_runner.py",
            )
        ]
        # The handlers facade is scanned with every ``chat_api`` owner it composes.
        roots += sorted((dashboard / "chat_api").glob("*.py"))
        # The runner is scanned with every ``chat_turn`` owner it composes.
        roots += sorted(
            owner for owner in (dashboard / "chat_turn").glob("*.py") if owner.name != "__init__.py"
        )
        found = 0
        for path in roots:
            src = path.read_text(encoding="utf-8")
            for m in re.finditer(r'"approval_resolved"', src):
                # Take the enclosing broadcast call: from the preceding
                # ``broadcast_ws`` to the matching close paren region.
                head = src.rfind("broadcast_ws", max(0, m.start() - 300), m.start())
                if head == -1:
                    continue
                found += 1
                block = src[head : m.start() + 400]
                assert '"slot"' in block or "payload" in block, (
                    f"{path.name}: approval_resolved broadcast near offset {m.start()} "
                    "does not carry a slot key"
                )
        assert found >= 4, f"expected >=4 approval_resolved producers, found {found}"


# ---------------------------------------------------------------------------
# ``/api/status`` after removal from the implicit-allow list: an app that
# DECLARES it still reaches it, so the tightening does not lock out the
# shipped consumers (``design_critique`` declares it in app.json).
# ---------------------------------------------------------------------------


class TestApiStatusRequiresDeclaration:
    def test_declared_app_still_reaches_api_status(self):
        """Removing the implicit grant must not break apps that declare it."""
        with patch(
            "kiro_crew.dashboard.token_auth._app_api_allowlist",
            return_value=("/api/status",),
        ):
            assert app_token_path_allowed("design-critique", "/api/status") is True

    def test_undeclared_app_is_denied_api_status(self):
        with patch("kiro_crew.dashboard.token_auth._app_api_allowlist", return_value=()):
            assert app_token_path_allowed("mochi-pet", "/api/status") is False

    def test_shipped_manifests_that_use_api_status_declare_it(self):
        """Source guard: no shipped builtin may RELY on the implicit allow.

        Derived, not a hardcoded name list: a name list goes stale the moment a
        builtin's permissions change (``crew_companion`` legitimately dropped
        ``/api/status`` from its manifest once it stopped calling it, which
        silently reds a hardcoded assertion instead of catching a real break).
        The invariant that actually matters is that any builtin whose shipped
        code reaches ``/api/status`` declares it in ``permissions.api`` -- with
        the implicit grant gone, an undeclared caller gets a 403.
        """
        import json

        builtins = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "apps" / "builtins"
        frontends = Path(__file__).resolve().parents[1] / "website" / "src" / "apps"

        declared: list[str] = []
        for manifest in builtins.glob("*/app.json"):
            perms = json.loads(manifest.read_text(encoding="utf-8")).get("permissions", {})
            api = perms.get("api") or []
            if isinstance(api, list) and "/api/status" in api:
                declared.append(manifest.parent.name)

        # Declaring it must remain a live, exercised path -- otherwise this
        # whole class could pass against a build where nothing can reach the
        # endpoint at all.
        assert declared, "no builtin declares /api/status; guard is vacuous"

        undeclared_callers: list[str] = []
        for app_dir in sorted(builtins.glob("*/app.json")):
            name = app_dir.parent.name
            if name in declared:
                continue
            roots = [app_dir.parent]
            fe = frontends / name.replace("_", "-")
            if fe.is_dir():
                roots.append(fe)
            for root in roots:
                for src in root.rglob("*"):
                    if not src.is_file():
                        continue
                    if src.suffix not in (".py", ".ts", ".tsx", ".js", ".jsx"):
                        continue
                    try:
                        text = src.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                    if "/api/status" in text:
                        undeclared_callers.append(f"{name}:{src.name}")
                        break

        assert not undeclared_callers, (
            "these builtins reach /api/status without declaring it in "
            f"permissions.api, so they now 403: {undeclared_callers}"
        )


# ---------------------------------------------------------------------------
# A GLOBAL event must never be judged by a slot field that appears in its
# payload. ``notify()`` merges meta keys FLAT into the note, so a real caller
# (the heartbeat in slack/gateway_runtime/result_egress.py: meta={"slot": slot.key})
# puts a top-level ``slot`` on a ``notification`` frame.
# ---------------------------------------------------------------------------


class TestGlobalEventWithSlotDoesNotBypassScope:
    def _state_with_own_slot(self):
        slot = _make_slot(owner_app="mochi-pet", origin=SlotOrigin.APP, key="chat-1")
        return _make_state({"chat-1": slot})

    def test_notification_with_slot_still_needs_notification_scope(self):
        """The bypass: slots:all alone must NOT deliver a notification body."""
        state = self._state_with_own_slot()
        payload = {
            "kind": "heartbeat",
            "source": "system",
            "channel": "system.heartbeat",
            "title": "Still working",
            "body": "secret progress detail",
            "slot": "chat-1",  # flat-merged from notify(meta={"slot": ...})
        }
        assert not ws_event_allowed(
            "notification",
            payload,
            app="mochi-pet",
            allowed_events=frozenset({"slots:all"}),
            state=state,
        )

    def test_notification_with_slot_delivered_when_scope_declared(self):
        """With the real scope it is delivered — the fix only strips the
        smuggled inference, it does not break legitimate delivery."""
        state = self._state_with_own_slot()
        payload = {
            "kind": "heartbeat",
            "source": "system",
            "channel": "system.heartbeat",
            "title": "Still working",
            "body": "progress",
            "slot": "chat-1",
        }
        assert ws_event_allowed(
            "notification",
            payload,
            app="mochi-pet",
            allowed_events=frozenset({"notification:system"}),
            state=state,
        )

    def test_slack_heartbeat_really_passes_a_slot_in_meta(self):
        """Source guard: this whole class is only meaningful because a shipped
        caller flat-merges a slot onto a global notification frame."""
        gw = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "kiro_crew"
            / "slack"
            / "gateway_runtime"
            / "result_egress.py"
        ).read_text(encoding="utf-8")
        assert 'meta={"slot": slot.key}' in gw

    def test_meta_merge_is_flat_so_slot_lands_top_level(self):
        """Source guard on the mechanism: bus.push merges meta keys flat."""
        bus = (
            Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "notifications" / "bus.py"
        ).read_text(encoding="utf-8")
        assert "note[key] = value" in bus


# ---------------------------------------------------------------------------
# Coalesced subagent batches: above SubagentEventCoalescer's threshold ONE
# frame carries MANY subagents' rows, so there is no top-level slot. They must
# reach the app (they were falling through to the unknown-event deny, losing
# ALL subagent status/output) but be filtered per item.
# ---------------------------------------------------------------------------


class TestSubagentBatchFrames:
    def _state(self):
        mine = _make_slot(owner_app="mochi-pet", origin=SlotOrigin.APP, key="mine")
        theirs = _make_slot(owner_app="other-app", origin=SlotOrigin.APP, key="theirs")
        return _make_state({"mine": mine, "theirs": theirs})

    @pytest.mark.parametrize("event", ["subagent_batch_update", "subagent_batch_chunks"])
    def test_batch_frame_is_not_denied_as_unknown(self, event):
        """The frame must pass the gate — denying it starved apps of their OWN
        subagent events once the coalescer engaged."""
        state = self._state()
        assert ws_event_allowed(
            event,
            {"updates": [], "chunks": []},
            app="mochi-pet",
            allowed_events=frozenset(),
            state=state,
        )

    def test_filter_keeps_only_own_items(self):
        from kiro_crew.dashboard.ws_event_scope import filter_subagent_batch_for_app

        state = self._state()
        items = [
            {"id": "a1", "slot": "mine", "text": "mine"},
            {"id": "a2", "slot": "theirs", "text": "secret"},
        ]
        kept = filter_subagent_batch_for_app(items, "mochi-pet", frozenset(), state)
        assert [i["slot"] for i in kept] == ["mine"]

    def test_filter_drops_items_with_unresolvable_slot(self):
        from kiro_crew.dashboard.ws_event_scope import filter_subagent_batch_for_app

        state = self._state()
        items = [{"id": "a1", "slot": "gone"}, {"id": "a2"}, "notadict"]
        assert filter_subagent_batch_for_app(items, "mochi-pet", frozenset(), state) == []

    def test_subagent_all_sees_every_item(self):
        from kiro_crew.dashboard.ws_event_scope import filter_subagent_batch_for_app

        state = self._state()
        items = [{"id": "a1", "slot": "mine"}, {"id": "a2", "slot": "theirs"}]
        kept = filter_subagent_batch_for_app(items, "mochi-pet", frozenset({"subagent:all"}), state)
        assert len(kept) == 2

    def test_batch_events_really_exist_with_per_item_slots(self):
        """Source guard: this class is only meaningful while the coalescer emits
        these two frames AND seeds every buffered row with its slot."""
        src = (
            Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "subagent_scale.py"
        ).read_text(encoding="utf-8")
        assert '"subagent_batch_update"' in src
        assert '"subagent_batch_chunks"' in src
        # every buffered update row is seeded with a slot
        assert '{"slot": payload.get("slot", "")}' in src
        # every chunk row carries its slot
        assert '"slot": buf["slot"]' in src

    def test_batch_item_key_map_matches_the_emitter(self):
        """The payload keys the filter reads must be the ones actually sent."""
        from kiro_crew.dashboard.ws_event_scope import _SUBAGENT_BATCH_ITEM_KEY

        src = (
            Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "subagent_scale.py"
        ).read_text(encoding="utf-8")
        assert '"subagent_batch_update", {"updates": updates}' in src
        assert '"subagent_batch_chunks", {"chunks": chunks}' in src
        assert _SUBAGENT_BATCH_ITEM_KEY == {
            "subagent_batch_update": "updates",
            "subagent_batch_chunks": "chunks",
        }


# ---------------------------------------------------------------------------
# Owner-only: a crewmate's dashboard field frame
# ---------------------------------------------------------------------------


class TestDashboardFieldFrameIsOwnerOnly:
    """The field frame carries work-ledger and crew-log values, which is exactly what
    the whole ``/api/members`` surface withholds from an app token.

    An app never receives it either way -- it goes out on ``broadcast_ws_owners``,
    which no app socket is in, and an unclassified event is denied by the floor. What
    the classification buys is the AUDIT: an operator reading the trail sees a
    deliberate ``owner_only`` and not ``unknown_event``, which reads as somebody's
    manifest being wrong. It also means a later caller broadcasting the name literally
    cannot silently start reaching an app token.
    """

    FRAME = {"slug": "atlas", "dashboard": "atlas-dashboard", "read": {"goal": "x"}}

    def test_a_wildcard_manifest_does_not_receive_it(self):
        state = _make_state()
        assert (
            ws_event_allowed(
                "dashboard_fields",
                self.FRAME,
                app="mochi-pet",
                allowed_events=build_allowed_event_set(["*"]),
                state=state,
            )
            is False
        )

    def test_the_denial_is_audited_as_owner_only_and_not_as_an_unknown_event(self):
        """The whole difference this classification makes, and the only place it shows."""
        state = _make_state()
        with patch("kiro_crew.sel.sel") as sel_mock:
            ws_event_allowed(
                "dashboard_fields",
                self.FRAME,
                app="mochi-pet",
                allowed_events=build_allowed_event_set(["*"]),
                state=state,
            )
        outcomes = [
            c.kwargs.get("outcome") for c in sel_mock.return_value.log_api_access.call_args_list
        ]
        assert outcomes == ["denied:owner_only"], (
            "the trail reports this denial as a misconfiguration rather than as the "
            f"deliberate one it is: {outcomes}"
        )

    def test_it_is_the_name_the_push_actually_broadcasts(self):
        """Pinned against the emitter's own constant, so a rename cannot leave the
        classification behind on a string nothing sends."""
        from kiro_crew.dashboard import ws_event_scope
        from kiro_crew.dashboard.handlers import member_dashboard_fields

        assert member_dashboard_fields.FIELD_FRAME in ws_event_scope._OWNER_ONLY_EVENTS
