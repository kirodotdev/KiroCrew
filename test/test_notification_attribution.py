"""Every notification producer is attributed, or the bridge refuses its note.

Two halves of one rule. ``producer_identity_meta`` resolves the agent a session runs
as from the trusted sources -- the slot, subagent and cron registries, the live
session, the execution record bound to the key -- so an agent-produced note can name
it. And every call site that publishes a notification is accounted for here: each one
either carries producer identity or is explicitly tagged system-originated, so a new
publishing path that does neither fails this suite rather than a review.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers._shared import _session_agent_names, producer_identity_meta
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.notifications.attribution import SYSTEM_ORIGIN_KEY, system_origin

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


def _state(*, live: dict[str, str] | None = None) -> SimpleNamespace:
    """A dashboard state with no slot, subagent or cron match for any key."""
    live = live or {}
    sessions = SimpleNamespace(
        has_session=lambda key: key in live,
        _get_session_agent=lambda key: live.get(key, ""),
    )
    return SimpleNamespace(
        sessions=sessions,
        subagents=SimpleNamespace(running=[], _agents={}),
        _slots={},
        crons=SimpleNamespace(_jobs=[]),
    )


def _execution(template: str) -> ExecutionContext:
    return ExecutionContext(
        member_id=None,
        store=MemoryStoreRef("default"),
        selection_kind="template",
        template_id=template,
    )


class TestAnUnlinkedSessionNamesItsAgent:
    """GPT 6.1 (v73 F1): a Slack thread selects its agent without a dashboard slot,
    so the slot lookup misses it; the live session and its execution record do not."""

    KEY = "slack:T1:C1:1712793600.000100"

    def test_the_live_sessions_agent_is_named(self) -> None:
        state = _state(live={self.KEY: "slack-denied-agent"})
        with patch("kiro_crew.execution_context.read_session_execution", return_value=None):
            meta = producer_identity_meta(state, self.KEY)  # type: ignore[arg-type]
        assert meta["producer_agent"] == "slack-denied-agent"

    def test_the_execution_records_template_is_named(self) -> None:
        with patch(
            "kiro_crew.execution_context.read_session_execution",
            return_value=_execution("recorded-tmpl"),
        ):
            names = _session_agent_names(_state(), self.KEY)  # type: ignore[arg-type]
        assert names == ["recorded-tmpl"]

    def test_a_live_session_naming_no_agent_names_the_default(self) -> None:
        state = _state(live={self.KEY: ""})
        with (
            patch("kiro_crew.execution_context.read_session_execution", return_value=None),
            patch(
                "kiro_crew.notifications.attribution.default_agent_names",
                return_value=["default", "kirocrew"],
            ),
        ):
            names = _session_agent_names(state, self.KEY)  # type: ignore[arg-type]
        assert names == ["default", "kirocrew"]

    def test_an_unknown_key_names_nothing_so_the_bridge_refuses_it(self) -> None:
        with (
            patch("kiro_crew.execution_context.read_session_execution", return_value=None),
            patch(
                "kiro_crew.notifications.attribution.default_agent_names",
                return_value=["default"],
            ),
        ):
            assert _session_agent_names(_state(), self.KEY) == []  # type: ignore[arg-type]

    def test_an_unreadable_registry_names_nothing_rather_than_raising(self) -> None:
        state = _state()
        state.sessions = MagicMock(has_session=MagicMock(side_effect=RuntimeError("x")))
        with patch(
            "kiro_crew.execution_context.read_session_execution",
            side_effect=RuntimeError("y"),
        ):
            assert _session_agent_names(state, self.KEY) == []  # type: ignore[arg-type]


def test_system_origin_marks_the_note_and_keeps_its_meta() -> None:
    assert system_origin(kind="k", count=2) == {SYSTEM_ORIGIN_KEY: "1", "kind": "k", "count": 2}


# ── Every publishing call site is accounted for ──

#: (module path under src/kiro_crew, enclosing function) -> how the site attributes
#: its notes. "identity": its meta names the producing session, run, job or agent, and
#: the dashboard sink resolves the agent from it. "system": no agent produced it, and
#: its meta is built with ``system_origin``. "app": an app's own push, attributed by
#: its server-set ``app:<name>`` source. "adapter": forwards a caller's payload.
#: "dashboard-only": an agent-produced notice whose generating identity is NOT available
#: at the publish site, so it is tagged NEITHER system nor with a producer -- the bridge's
#: deny-unattributed rule then keeps it on the dashboard (never egressed to chat) until it
#: can carry real attribution. Fail-safe, deliberate.
_SITES: dict[tuple[str, str], str] = {
    ("apps/builtins/issue_radar/backend/watch.py", "_notify_new_issues"): "system",
    ("apps/builtins/ops_mission_control/backend/notify_out.py", "_push"): "app",
    ("dashboard/chat_handlers.py", "_report_lost_queued_prompts"): "identity",
    ("dashboard/chat_voice.py", "_sandbox_refusal_response"): "identity",
    ("dashboard/handlers/hooks.py", "_run_hook_agent"): "identity",
    ("dashboard/handlers/messaging.py", "api_notification_agent_push"): "identity",
    ("dashboard/handlers/notifications_push.py", "api_push_notification"): "app",
    ("dashboard/handlers/terminal.py", "_notify_shell_failed"): "system",
    ("dashboard/handlers/updates.py", "_arm_packaged_app"): "identity",
    ("dashboard/handlers/updates.py", "_apply"): "system",
    ("dashboard/messaging_api/proactive_send.py", "_deliver_send_message_fallback"): "identity",
    ("dashboard/notification_coordinator.py", "notify"): "adapter",
    ("dashboard/notification_coordinator.py", "notify_returning_handle"): "adapter",
    ("dashboard/server_runtime/config_watch.py", "_apply_default_model"): "system",
    ("dashboard/server_runtime/heartbeat.py", "_report_prior_crash_dump"): "system",
    ("dashboard/server_runtime/safety_grants.py", "_notify_unattended_expiry"): "system",
    ("dashboard/server_runtime/safety_grants.py", "_notify_restart_dropped_grant"): "system",
    ("dashboard/server_runtime/skill_learning.py", "_emit"): "dashboard-only",
    ("dashboard/slot_retention.py", "notify_left_in_history"): "system",
    ("dashboard/state.py", "__init__"): "identity",
    ("dashboard/state.py", "knowledge_store"): "system",
    ("notifications/resource_pressure.py", "_push_slice_oom"): "system",
    ("notifications/resource_pressure.py", "_push_critical"): "system",
    ("notifications/resource_pressure.py", "_push_tight"): "system",
    ("notifications/resource_pressure.py", "_push_recovery"): "system",
    ("slack/gateway.py", "_deliver_script_result"): "identity",
    ("slack/gateway.py", "_alert_cron_failure"): "identity",
    ("slack/gateway.py", "_cron_callback"): "identity",
    ("slack/gateway.py", "_announce_cron_quarantine"): "system",
    ("slack/gateway.py", "_notify_nudge_expired"): "identity",
    ("slack/gateway.py", "_notify_nudge_held"): "identity",
    ("slack/gateway.py", "_notify_consolidation_abandoned"): "system",
    ("slack/gateway.py", "_deliver_result"): "identity",
    ("slack/gateway.py", "_subagent_done"): "identity",
    ("slack/gateway.py", "_orphan_dm"): "identity",
    ("slack/gateway.py", "_task_notify"): "identity",
    ("slack/gateway.py", "_notice_wheel_update_once"): "system",
}


def _publishing_calls() -> list[tuple[str, str, ast.Call]]:
    """Every call that hands a note to the notification bus, with its site."""
    found: list[tuple[str, str, ast.Call]] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if "/tests/" in f"/{rel}" or "_tests/" in rel:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def visit(node: ast.AST, fn: str) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = node.name
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                recv = ast.unparse(node.func.value)
                attr = node.func.attr
                legacy = (
                    attr in ("notify", "notify_awaiting_persist")
                    and len(node.args) >= 3
                    and not recv.startswith("_notifications_for")
                    and recv != "self.publisher"
                )
                pushed = attr == "push" and (
                    recv.endswith("bus") or recv.endswith("notification_bus")
                )
                if legacy or pushed:
                    found.append((rel, fn, node))
            for child in ast.iter_child_nodes(node):
                visit(child, fn)

        visit(tree, "<module>")
    return found


def _meta_source(call: ast.Call) -> str:
    """The source of the ``meta=`` a call passes, directly or on its payload."""
    for keyword in call.keywords:
        if keyword.arg == "meta":
            return ast.unparse(keyword.value)
    if call.args and isinstance(call.args[0], ast.Call):
        for keyword in call.args[0].keywords:
            if keyword.arg == "meta":
                return ast.unparse(keyword.value)
    return ""


def test_every_publishing_site_is_attributed_or_tagged_system() -> None:
    calls = _publishing_calls()
    assert calls, "the scan found no publishing call at all; it is not looking"
    unknown = sorted({(rel, fn) for rel, fn, _ in calls} - set(_SITES))
    assert not unknown, (
        "new notification publishing site(s) with no attribution decision: "
        f"{unknown}. Give each note producer identity (session/run/job/agent meta) "
        "or build its meta with system_origin(...), then record the choice in _SITES."
    )
    for rel, fn, call in calls:
        kind = _SITES[(rel, fn)]
        meta = _meta_source(call)
        where = f"{rel}:{call.lineno} ({fn})"
        if kind == "system":
            assert "system_origin(" in meta, f"{where} is listed system but its meta is {meta!r}"
        elif kind == "identity":
            if call.args and isinstance(call.args[0], ast.Name) and not meta:
                # A payload built a few lines up (the agent push names its session
                # there); the bridge's runtime rule refuses it if that ever stops.
                continue
            assert meta and meta != "None", f"{where} is listed identity but passes no meta"
            assert "system_origin(" not in meta, f"{where} tags an agent note as system"
        elif kind == "dashboard-only":
            # Deliberately unattributed: NOT system-tagged and names no producer, so the
            # bridge's deny-unattributed rule keeps it on the dashboard. If a producer key
            # or a system tag ever appears here it would become bridge-eligible, which is
            # the behaviour this classification exists to prevent -- so assert neither is.
            assert "system_origin(" not in meta, f"{where} is dashboard-only but tags system"
            for producer_key in ("session_key", "producer_session", "producer_agent", "task_id"):
                assert (
                    producer_key not in meta
                ), f"{where} is dashboard-only but its meta names {producer_key}: {meta!r}"


def test_every_listed_site_still_exists() -> None:
    """A stale entry would let a renamed site's replacement go unreviewed."""
    present = {(rel, fn) for rel, fn, _ in _publishing_calls()}
    assert not sorted(set(_SITES) - present)


@pytest.mark.parametrize("site", sorted(_SITES), ids=lambda s: f"{s[0]}:{s[1]}")
def test_a_site_that_loses_its_attribution_is_denied_at_the_bridge(site) -> None:
    """The enforcement point is the bridge, not any one route: a note from any listed
    site that arrives without the producer identity (or system tag) its kind requires
    is refused there as unattributed, and the same note carrying it is let through."""
    from kiro_crew.notifications.bridge import BridgeDispatcher

    kind = _SITES[site]
    if kind == "adapter":
        pytest.skip("forwards the caller's payload; attribution is the caller's")
    bridge = BridgeDispatcher(sink_resolver=lambda _t: None, settings_reader=lambda _c: {})
    if kind == "dashboard-only":
        # This site's note names no producer and is not system-tagged, so the bridge
        # denies it (dashboard-only). The paired "attributed" form shows the bridge WOULD
        # forward it once it carried real producer identity.
        unattributed_note = {"source": "system", "channel": "system.skills"}
        attributed = {
            "source": "system",
            "channel": "system.skills",
            "session_key": "slack:T1:C1:1.2",
            "producer_agent": "researcher",
        }
        assert bridge._unattributed(unattributed_note) != ""
        assert bridge._unattributed(attributed) == ""
        return
    if kind == "app":
        attributed = {"source": "app:some-app", "channel": "some-app.alerts"}
        stripped = {"source": "system", "channel": "some-app.alerts"}
    elif kind == "system":
        attributed = {"source": "system", "channel": "system.update", SYSTEM_ORIGIN_KEY: "1"}
        stripped = {"source": "system", "channel": "system.update"}
    else:
        attributed = {
            "source": "system",
            "channel": "system.agent",
            "session_key": "slack:T1:C1:1.2",
            "producer_agent": "researcher",
        }
        stripped = {
            "source": "system",
            "channel": "system.agent",
            "session_key": "slack:T1:C1:1.2",
        }
    assert bridge._unattributed(attributed) == ""
    assert bridge._unattributed(stripped) != ""


@pytest.mark.parametrize(
    "call",
    [
        'state.notify("update", "t", "b")',
        'state.notify("agent", "t", "b", meta=None)',
    ],
)
def test_the_scan_classifies_a_bare_call_as_unattributed(call: str) -> None:
    node = ast.parse(call).body[0].value  # type: ignore[attr-defined]
    meta = _meta_source(node)
    assert "system_origin(" not in meta and (not meta or meta == "None")


# ── Producer lookup stays off the event loop (GPT 6.1 / Opus 5.5, v75) ──


def test_the_loop_side_pass_never_reads_a_persisted_record() -> None:
    """``_deliver_note`` runs on the loop for every note, routed or not; the execution
    record can fall through to a transcript read and a legacy backfill rewrite, so the
    loop-side pass must not touch it."""
    from kiro_crew.dashboard.state import DashboardState

    identities = {"session_key": "dashboard:archived-member-chat"}
    with (
        patch(
            "kiro_crew.execution_context.read_session_execution",
            side_effect=AssertionError("persisted read on the loop"),
        ),
        patch(
            "kiro_crew.notifications.attribution.default_agent_names",
            side_effect=AssertionError("config read on the loop"),
        ),
    ):
        DashboardState._add_producer_identities(_state(), identities)  # type: ignore[arg-type]
    assert "producer_agent" not in identities


def test_a_note_names_a_bounded_set_of_producer_keys() -> None:
    """An app push can put a 64 KB ``producer_session`` on its note; the per-note lookup
    work stays bounded and repeated keys are looked up once."""
    from kiro_crew.dashboard.state import _MAX_PRODUCER_KEYS, _producer_keys

    assert _producer_keys({"producer_session": "\n".join(["subagent:a"] * 30000)}) == ["subagent:a"]
    many = "\n".join(f"subagent:{i}" for i in range(30000))
    keys = _producer_keys({"session_key": "dashboard:x", "producer_session": many})
    assert len(keys) == _MAX_PRODUCER_KEYS and keys[0] == "dashboard:x"


def test_the_loop_side_pass_looks_each_key_up_once() -> None:
    from kiro_crew.dashboard.state import DashboardState

    calls: list[str] = []

    def _meta(_state, key, *, persisted=True):
        assert persisted is False
        calls.append(key)
        return {}

    with patch("kiro_crew.dashboard.handlers._shared.producer_identity_meta", _meta):
        DashboardState._add_producer_identities(
            _state(),  # type: ignore[arg-type]
            {"session_key": "dashboard:x", "producer_session": "dashboard:x\nsubagent:c"},
        )
    assert calls == ["dashboard:x", "subagent:c"]


@pytest.mark.asyncio
async def test_the_bridge_finishes_the_lookup_off_the_loop_and_only_when_routed() -> None:
    import threading
    from unittest import mock

    from kiro_crew.notifications.bridge import BridgeDispatcher

    ran_on: list[str] = []

    def _resolver(note: dict) -> None:
        ran_on.append(threading.current_thread().name)
        note["producer_agent"] = "recorded-agent"

    sent: list[str] = []

    class _Sink:
        async def send(self, text, recheck=None):
            if recheck is not None and await recheck():
                return ""
            sent.append(text)
            return "m1"

    settings = {"system.agent": {"deliver_to": ["slack"]}}
    bridge = BridgeDispatcher(
        sink_resolver=lambda _t: _Sink(),
        settings_reader=lambda channel: settings.get(channel, {}),
        identity_resolver=_resolver,
    )
    note = {
        "channel": "system.agent",
        "source": "system",
        "priority": "critical",
        "title": "t",
        "session_key": "dashboard:archived-member-chat",
    }
    asked: list[str] = []

    def _vet(*_a, **k):
        if k.get("agent"):
            asked.append(k["agent"])
        return mock.Mock(permitted=True, rule="", layer="", reason="")

    with (
        mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_vet),
        mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
    ):
        await bridge.dispatch({**note, "channel": "system.unrouted"})
        assert ran_on == []  # an unrouted note costs no persisted lookup at all
        await bridge.dispatch(note)
    assert ran_on and ran_on[0] != threading.main_thread().name
    assert "recorded-agent" in asked and len(sent) == 1
    assert "producer_agent" not in note  # the caller's note is not mutated


def test_the_persisted_pass_judges_a_named_parent_even_when_a_child_agent_is_named() -> None:
    """A subagent completion names its child's agent AND its parent session; the
    parent's recorded agent is still resolved and added, so a parent agent that
    denies Slack is judged rather than skipped behind the child's name."""
    from kiro_crew.dashboard.state import DashboardState

    looked_up: list[str] = []

    def _names(_state, key, *, persisted=True):
        assert persisted is True
        looked_up.append(key)
        return ["parent-agent"] if key == "dashboard:closed-parent" else []

    note = {
        "producer_agent": "child-agent",
        "session_key": "dashboard:closed-parent",
        "producer_session": "subagent:c1",
    }
    with patch("kiro_crew.dashboard.handlers._shared._session_agent_names", _names):
        DashboardState._resolve_persisted_producer_agents(_state(), note)  # type: ignore[arg-type]
    assert looked_up == ["dashboard:closed-parent", "subagent:c1"]
    assert note["producer_agent"].split("\n") == ["child-agent", "parent-agent"]


# ── Regression: the packaged-app arm notice vets its requester, never host-only ──
#
# The arm route publishes a note whose title/body carry requester-controlled text
# (the named version and the asking session). Tagging it ``system_origin()`` passed
# it on the host profile alone, so a messaging-denied producer that reached the arm
# route saw its text egressed to the owner's routed chat DM. ``X-Session-Key`` is
# unverified here, so the note must also carry the AUTHENTICATED caller app
# (``request["app"]``, server-set from the token) as a ``producer_app`` subject,
# merged with any app the named session resolves to, so the bridge vets the app's
# own transport denial and not just a session the caller named. A call that names
# neither leaves the note unattributed and the bridge refuses it.


async def _drive_arm(
    session_key: str | None,
    *,
    app: str = "",
    session_meta: dict[str, str] | None = None,
) -> dict[str, object]:
    """Run ``_arm_packaged_app`` and return the meta it published, mocking the disk
    and request-identity edges only. ``app`` is the authenticated ``request['app']``
    the token-auth middleware would have set; ``session_meta`` is what the named
    session resolves to via ``producer_identity_meta``."""
    from kiro_crew.dashboard.handlers import updates as updates_mod

    captured: dict[str, object] = {}
    state = _state()
    state.notify = lambda *a, **k: captured.update(k.get("meta") or {"__none__": True})  # type: ignore[attr-defined]

    headers = {"X-Session-Key": session_key} if session_key is not None else {}
    request_fields = {"app": app}
    request = SimpleNamespace(
        headers=headers,
        app={"state": state},
        get=lambda k, default=None: request_fields.get(k, default),
        json=_async_return({}),
        remote="unix",
    )

    with (
        patch.object(updates_mod, "resolve_provider", lambda: None),
        patch(
            "kiro_crew.platform.app_update_request.get_app_update_requests",
            lambda: SimpleNamespace(
                arm=lambda target_version, requested_by: (
                    SimpleNamespace(to_public=lambda _now: {}),
                    True,  # is_new_ask
                )
            ),
        ),
        patch.object(updates_mod, "_audit_update_event", _async_noop()),
        patch.object(
            updates_mod, "producer_identity_meta", lambda _s, key, **_k: session_meta or {}
        ),
    ):
        await updates_mod._arm_packaged_app(request)  # type: ignore[arg-type]
    return captured


def _async_return(value: object):
    async def _coro():
        return value

    return lambda: _coro()


def _async_noop():
    async def _noop(*_a, **_k):
        return None

    return _noop


@pytest.mark.asyncio
async def test_the_arm_notice_names_its_requesting_session_not_the_host() -> None:
    meta = await _drive_arm("dashboard:ui-5")
    assert meta.get("session_key") == "dashboard:ui-5"
    assert SYSTEM_ORIGIN_KEY not in meta


@pytest.mark.asyncio
async def test_the_arm_notice_with_no_producer_names_nothing() -> None:
    # No ``X-Session-Key`` and no authenticated app -> empty meta -> the bridge
    # refuses it as unattributed rather than egressing under the permissive host.
    meta = await _drive_arm(None)
    assert "session_key" not in meta
    assert "producer_app" not in meta
    assert SYSTEM_ORIGIN_KEY not in meta


@pytest.mark.asyncio
async def test_the_arm_notice_vets_the_authenticated_app_even_with_a_named_session() -> None:
    # The attack path: an app token denied the transport names a permitted owner
    # session in the unverified X-Session-Key. The authenticated app must still be
    # a producer_app subject so the bridge vets ITS denial, not just the session's.
    meta = await _drive_arm("dashboard:owner-permitted", app="denied-app")
    assert "denied-app" in str(meta.get("producer_app", "")).split("\n")


@pytest.mark.asyncio
async def test_the_authenticated_app_is_merged_with_the_session_derived_app() -> None:
    meta = await _drive_arm(
        "dashboard:owner", app="caller-app", session_meta={"producer_app": "session-app"}
    )
    apps = str(meta.get("producer_app", "")).split("\n")
    assert "caller-app" in apps and "session-app" in apps
