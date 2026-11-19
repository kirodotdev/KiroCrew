"""``POST /api/browser/op`` -- run one ``browser`` MCP tool op on the Harness.

The ``browser`` tool lives in the ``kirocrew-core`` stdio shim, a separate
process, so it cannot call ``HarnessSupervisor.call`` in-process: it posts the
op here and this handler runs :func:`kiro_crew.browser_harness.ops.run_op`
against the gateway's one supervisor. MACHINE-only: the route is in
``_STRICT_INTERNAL_API_PATHS`` and the handler re-asserts
``request["internal_auth"] is True`` (set only on a verified
``X-Internal-Secret``), mirroring ``api_computer_use_invoke``. It does NOT also
require a loopback ``request.remote``: the shim reaches the gateway over the
AF_UNIX internal-API socket, where ``remote`` is empty.

Body: ``{"session_key": str, "op": str, "args": object}``. ``session_key`` is the
caller's chat slot, which the live card and the input lock are keyed by.

Responses (always HTTP 200 once authorised, so the shim reads one shape):

* ``{"ok": true, "result": {...}}`` -- the op ran;
* ``{"ok": false, "code": str, "error": str}`` -- the op answered a refusal
  (``password_field``, ``ref_stale``, ...), or this handler's own:
  ``not_installed`` (no Harness on this host), ``browser_repairing`` (an install
  job is running, such as the reinstall of an install that broke after an update),
  ``browser_error`` (the daemon or
  browser failed), ``timeout`` (the op ran past its budget), ``browser_busy``
  (another chat held the lock too long), ``user_in_control`` (the person held
  the browser for the whole ``HOLD_TIMEOUT_S``), ``interrupted`` (the person
  took over mid-op and has handed back; the op may not have finished),
  ``isolation_unknown`` (``browser.isolation`` was ``per_session`` and cannot be
  read now), ``browser_restarted`` (the browser restarted during an isolated op;
  the chat's context is gone, the next op makes a new one).

While the person is in control an op is not lost: the route answers at once with
``user_in_control`` and ``"hold": true``, and the ``browser`` tool in the shim
parks itself (a ``DeferredTool``) polling ``POST /api/browser/handback`` until
they hand the browser back (Esc, "Hand back", the turn's end, or the idle
bound on a takeover that outlived its turn, ``TAKEOVER_IDLE_S``), bounded by
``HOLD_TIMEOUT_S`` (10 minutes), then posts the op again, which runs. That is
what lets the agent resume on its own after a sign-in the person did for it. The
hold is sliced because the shim runs one worker thread and the ACP tool-stall
watchdog kills a tool call silent for 600 s; each slice pings the session
keepalive. ``/api/browser/handback`` itself waits at most ``HANDBACK_WAIT_S`` on
the hub's release signal.

The URL gate (public http(s) only, plus the operator's listed loopback origins)
and the ``capabilities.browse`` governance check run in the shim BEFORE the
post, and again in :func:`run_browser_op`, the one door every browser op goes
through (this route and an app's ``PagePort``): the requested address is
refused there before the browser starts, so a caller inside the gateway cannot
skip it. The route asks ``capabilities.browse`` again itself, fail-closed (403
``policy_denied``, ``handlers/browser_policy.py``), and so does every helper the
op runs (``HarnessSupervisor.call``), so a deny that lands mid-op ends it with the
same code. Beyond that it enforces what only the gateway can see: who holds the
browser, which tabs the agent owns.

The live card (``browser_harness.live_hub``) is driven from here too, because
this is the one place that knows both the session and the browser: the FIRST
op of a turn opens the hub's turn (a fresh ``turn_id``; the chat runner closes
it from ``_end_turn_tail``), every op is a step, and after each op the card
follows the daemon's current tab -- a stream is started on it, or restarted
when the agent moved to another tab, else only the address pill is updated.
While the owner's Chrome shows its Allow sheet the card reads ``waiting_allow``.

One daemon, many chats: every op runs under one gateway-wide lock, and before it
runs :func:`_bind_target` switches the daemon's current tab to the CALLING
session's own tab (adopting the daemon's tab the first time, opening a new one
when another chat owns it). So chat B's ``click`` never lands on chat A's page,
``tabs`` sees only the caller's tabs, and a takeover in chat A fences A's tab
while B keeps browsing its own.

Under ``browser.isolation: per_session`` the bind goes one step further: each
session's tabs open in its own browser context (``browser_harness.session_contexts``),
so chats do not share cookies either, and the daemon's current tab is never adopted.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import uuid
from collections.abc import Callable
from typing import Any

from aiohttp import web

from kiro_crew.apps import browser_sites
from kiro_crew.apps.browser_sites import app_browser_sites, browser_call_url, url_matches
from kiro_crew.browser_harness import ops as harness_ops
from kiro_crew.browser_harness import run_budget, session_contexts, url_gate
from kiro_crew.browser_harness.budgets import (
    CHANGE_REPORT_S,
    DECISION_HEADROOM_S,
    ENSURE_TIMEOUT_S,
    HANDBACK_WAIT_S,
    HOLD_TIMEOUT_S,
    LOCK_WAIT_S,
    OP_TIMEOUT_S,
    SLOW_OP_TIMEOUT_S,
)
from kiro_crew.browser_harness.decide import BrowserDecisions
from kiro_crew.browser_harness.governance import (
    DASHBOARD_SURFACE_KEY,
    DENIED_CODE,
    DENIED_TEXT,
    HOST_KEY,
    BrowsingDenied,
    browse_denied_async,
    stop_running_harness,
)
from kiro_crew.browser_harness.live_hub import (
    STATE_DONE,
    STATE_ERROR,
    STATE_WAITING_ALLOW,
    LiveHub,
    get_live_hub,
)
from kiro_crew.browser_harness.supervisor import IPC_LEAF, BrowserRestarted, HelperError
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.handlers.browser_policy import browse_refusal
from kiro_crew.dashboard.state import _normalize_slot_key
from kiro_crew.loop_lock import LoopBoundLock

logger = logging.getLogger(__name__)

# The budgets live in ``browser_harness.budgets``, shared with the shim that posts
# here, so the shim's HTTP timeout is always longer than this route's worst case.
#: How often the ensure wait re-reads the supervisor's connection for the card.
_ENSURE_POLL_S = 0.25


def _sel():
    """Late-binding ``sel()``: this module is imported BY ``handlers/__init__``."""
    from kiro_crew.dashboard import handlers

    return handlers.sel()


def _audit(request: web.Request, outcome: str, error: str = "") -> None:
    try:
        _sel().log_api_access(
            caller=str(request.get("user") or request.remote or ""),
            operation="browser_op",
            outcome=outcome,
            source="browser",
            resources=request.path,
            error=error or None,
        )
    except Exception:  # noqa: BLE001 - auditing never fails the request
        logger.debug("browser op audit failed", exc_info=True)


def _live_handlers() -> Any:
    """Late-binding import of the card's handler module (imported BY ``handlers/__init__``)."""
    from kiro_crew.dashboard.handlers import browser_live

    return browser_live


def live_session_key(session_key: str) -> str:
    """The hub key for a caller's session: the dashboard slot key the card is keyed by.

    The shim resolves its session as ``dashboard:<slot>`` or the bare slot; the card
    posts ``activeSlot``. Folding both through the slot directory's normaliser is
    what makes the frame, the event and the input lock meet on one key.
    """
    return _normalize_slot_key(session_key) or session_key


#: One op at a time across EVERY chat: the daemon has one current tab, and
#: ``_bind_target`` switches it to the caller's own before the op runs, so bind
#: and op must not interleave with another chat's.
_OP_LOCK = LoopBoundLock()


def _ledgered_targets(supervisor: Any) -> list[str]:
    relay = getattr(supervisor, "relay", None)
    return list(relay.agent_targets()) if relay is not None else []


def _own_targets(hub: LiveHub, supervisor: Any, key: str, targets: list[str]) -> list[str]:
    """Those of *targets* in the browser context the op's bind put *key* in.

    The setting was read once, at the bind; the op reads the bind's answer, so a
    flip mid-op cannot show ``tabs list`` a different mode than the bind used.
    """
    relay = getattr(supervisor, "relay", None)
    if relay is None:
        return targets
    return session_contexts.in_context(relay, targets, hub.context_of(key))


def _opener_of(relay: Any, target_id: str) -> str:
    """The agent tab that opened popup *target_id* (``CdpRelay.opener_of``), or ``""``."""
    opener_of = getattr(relay, "opener_of", None)
    if not callable(opener_of):
        return ""
    opener = opener_of(target_id)
    return opener if isinstance(opener, str) else ""


def claim_popups(hub: LiveHub, key: str, relay: Any) -> list[str]:
    """Give *key* the popups its own tabs opened; return the ones newly claimed.

    The relay ledgers a popup by its opener (``CdpRelay.opener_of``) but knows no
    chat; the chat that owns the opener owns the popup, so ``tabs`` lists it, a
    switch may move onto it and the chat's end closes it. A popup of a popup is
    claimed on the same call, in ledger order.
    """
    if relay is None:
        return []
    claimed: list[str] = []
    mine = hub.targets_of(key)
    for target in list(relay.agent_targets()):
        if target in mine or hub.owner_of_target(target) is not None:
            continue
        if _owning_ancestor_is(hub, key, relay, target, mine):
            hub.add_target(key, target)
            mine.add(target)
            claimed.append(target)
    return claimed


def _owning_ancestor_is(hub: LiveHub, key: str, relay: Any, target: str, mine: set[str]) -> bool:
    """Whether *target*'s nearest owned ancestor, by opener links, is one of *key*'s.

    The links outlive a closed popup, so a popup whose own opener popup already
    closed still reaches the chat's tab. The walk stops at a tab another chat owns.
    """
    seen = {target}
    opener = _opener_of(relay, target)
    while opener and opener not in seen:
        if opener in mine:
            return True
        if hub.owner_of_target(opener) not in (None, key):
            return False
        seen.add(opener)
        opener = _opener_of(relay, opener)
    return False


def close_chat_tabs(state: Any, session_key: str) -> None:
    """Close, in the background, the agent tabs an ended chat session owned.

    Every other chat keeps its own, and nothing is started: a chat that never
    browsed, or a browser that is not up, has no tab to close. The close queues on
    :data:`_OP_LOCK` behind the ops already holding or awaiting it, so a tab one of
    them opens for this chat closes too.
    """
    key = live_session_key(session_key)
    if get_live_hub().get(key) is None:
        return
    task = asyncio.create_task(_close_tabs_of(state, key))
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


async def _close_tabs_of(state: Any, key: str) -> None:
    hub = get_live_hub()
    try:
        async with _OP_LOCK:
            if key in state._slots:
                # A chat reopened on this key (a resume) since the close: the tabs
                # under the key are its own now.
                return
            await hub.stop_stream(key)
            hub.reset_run(key)
            relay = _live_handlers()._current_relay()
            if relay is not None:
                claim_popups(hub, key, relay)
                try:
                    await relay.close_agent_targets(hub.targets_of(key))
                finally:
                    await relay.dispose_session_context(key)
            if key not in state._slots:
                # Not reopened during the awaits above: a resumed chat's turn opens
                # its session before it waits for the lock, so keep that one.
                hub.forget(key)
    except Exception:  # noqa: BLE001 - best effort beside a close that already committed
        logger.debug("browser: closing the tabs of %s failed", key, exc_info=True)


def caller_checked_call(
    supervisor: Any, caller_key: str, hub: LiveHub | None = None, session_key: str = ""
) -> Any:
    """``supervisor.call`` that first asks the ceiling for *caller_key*.

    ``HarnessSupervisor.call`` asks it with the engine's own empty key, so a profile
    bound to the caller's surface is not consulted there. This asks it before every
    helper the op runs, so a deny in that profile stops the op at its next step even
    when it landed while the op waited for :data:`_OP_LOCK`.

    With *hub* and *session_key*, every helper is also pinned to the relay the
    session's bind recorded (``LiveHub.bound_relay``, set under ``per_session``), so
    a daemon restart between the bind and a later step refuses the op
    (``BrowserRestarted``) instead of driving the new browser's default context.
    """

    async def _call(helper: str, **kwargs: Any) -> Any:
        if await browse_denied_async(caller_key, audit=False):
            # The same stop every other refusal of this deny schedules.
            stop_running_harness()
            raise BrowsingDenied()
        relay = hub.bound_relay(session_key) if hub is not None else None
        if relay is not None:
            kwargs["relay"] = relay
        return await supervisor.call(helper, **kwargs)

    return _call


class _GatedRelay:
    """The stream's relay as :class:`FrameOps` sees it: each CDP send asks the ceiling.

    Frame reads and writes go straight to the relay rather than through a helper, so
    without this a click inside a cross-origin frame would run under a deny until
    the op's next helper. Both keys are asked: the engine's own and the caller's.
    """

    def __init__(self, relay: Any, caller_key: str) -> None:
        self._relay = relay
        self._caller_key = caller_key

    async def send(self, method: str, params: dict[str, Any], sid: str) -> Any:
        if await browse_denied_async(HOST_KEY, audit=False) or await browse_denied_async(
            self._caller_key, audit=False
        ):
            stop_running_harness()
            raise BrowsingDenied()
        return await self._relay.send(method, params, sid)


def build_context(
    session_key: str,
    supervisor: Any,
    hub: LiveHub | None = None,
    *,
    caller_key: str | None = None,
    sites: tuple[str, ...] = (),
) -> harness_ops.OpContext:
    """Bind the gateway's supervisor, relay and live hub to one caller's session.

    *caller_key* is the caller's own runtime session key, which governance binds on;
    *session_key* is the card's normalized key. With it, every helper re-asks the
    ceiling for that caller (:func:`caller_checked_call`). *sites* are the approved
    ``browserSites`` of the app that owns the session (:func:`session_sites`): when
    there are any, a page is allowed only if it is one of them as well, so a landing
    off the list is refused and blanked like any other refused landing.
    """
    hub = hub if hub is not None else get_live_hub()
    helper = (
        supervisor.call
        if caller_key is None
        else caller_checked_call(supervisor, caller_key, hub, session_key)
    )

    def _step(label: str) -> Any:
        return hub.step(session_key, label)

    def _in_control() -> Any:
        return hub.in_control(session_key)

    def _agent_targets() -> list[str]:
        # The tabs THIS session owns, the browser still has, and that sit in the
        # context it browses in now: ``tabs list`` shows them and ``tabs switch``
        # moves only among them, never onto another chat's, and never onto a tab
        # from the other side of a flip of ``browser.isolation``. A popup one of
        # them opened since the last op counts as its own.
        claim_popups(hub, session_key, getattr(supervisor, "relay", None))
        mine = hub.targets_of(session_key)
        held = [t for t in _ledgered_targets(supervisor) if t in mine]
        return _own_targets(hub, supervisor, session_key, held)

    def _loop(info: Any) -> Any:
        return hub.loop(session_key, info)

    def _mark() -> Any:
        return hub.inner_mark(session_key)

    def _record(op: str, args: Any, result: Any, mark: Any) -> Any:
        if mark is None:
            return None
        return hub.record_op(session_key, op, args, result, mark, loop=True)

    def _frames() -> Any:
        return frame_ops(hub, session_key, caller_key=caller_key)

    return harness_ops.OpContext(
        helper=helper,
        step=_step,
        in_control=_in_control,
        agent_targets=_agent_targets,
        screenshot_dir=harness_ops.screenshot_dir(config_dir()),
        staging_dir=config_dir() / IPC_LEAF / "shots",
        sleep=asyncio.sleep,
        url_allowed=committed_url_allowed if not sites else _site_gate(sites),
        loop=_loop,
        mark=_mark,
        record=_record,
        frames=_frames,
        report_changes=_report_changes(),
    )


def _op_budget(op: str, ctx: harness_ops.OpContext) -> float:
    """The route's budget for *op*: its own, the decision hooks', and the change report's."""
    own = SLOW_OP_TIMEOUT_S if op in harness_ops.SLOW_OPS else OP_TIMEOUT_S
    report = CHANGE_REPORT_S if ctx.report_changes and op in harness_ops.REPORT_OPS else 0.0
    return own + DECISION_HEADROOM_S + report


def _report_changes() -> bool:
    """``browser.report_changes``, read fresh for each op. Off when it cannot be read."""
    try:
        from kiro_crew.config import KiroCrewConfig

        return KiroCrewConfig.load().browser.report_changes is True
    except Exception:  # noqa: BLE001 - an unreadable setting leaves every result as it was
        return False


def _run_limits() -> tuple[int, int]:
    """``browser.run.max_ops`` and ``max_repeats``, read fresh for each op. Off (0) when unreadable."""
    try:
        from kiro_crew.config import KiroCrewConfig

        run = KiroCrewConfig.load().browser.run
        return run.max_ops, run.max_repeats
    except Exception:  # noqa: BLE001 - an unreadable setting leaves every op as it was
        return 0, 0


def frame_ops(hub: LiveHub, key: str, *, caller_key: str | None = None) -> Any:
    """The cross-origin frames of *key*'s tab, through its live stream's session.

    ``None`` unless a stream runs on the tab the session is bound to: the frames'
    child sessions hang off that stream (``browser_harness.frames``). With
    *caller_key*, every CDP send asks the ceiling first (:class:`_GatedRelay`).
    """
    from kiro_crew.browser_harness.frames import FrameOps

    sess = hub.get(key)
    stream = sess.stream if sess is not None else None
    registry = getattr(stream, "frames", None)
    if stream is None or registry is None or not stream.session_id:
        return None
    if stream.target_id != hub.target_of(key):
        return None
    relay = stream.relay if caller_key is None else _GatedRelay(stream.relay, caller_key)
    return FrameOps(relay, stream.session_id, registry, harness_ops.WALKER_SOURCE)


def committed_url_allowed(url: str) -> bool:
    """The navigate gate for the URL a navigation LANDED on, judged in the gateway.

    The same predicate the shim applies to the requested URL
    (``browser_harness.url_gate``), with the operator's loopback allowlist read
    fresh and the gateway's own port refused. Fail-CLOSED: a config read failure
    admits only public targets, an unknown port admits no local one.
    """
    try:
        from kiro_crew.config import KiroCrewConfig

        raw = getattr(KiroCrewConfig.load().dashboard, "browser_local_origins", [])
        allowed = (
            frozenset(e for e in raw if isinstance(e, str))
            if isinstance(raw, list)
            else frozenset()
        )
    except Exception:  # noqa: BLE001 - a config read failure must not widen the gate
        allowed = frozenset()
    try:
        from kiro_crew import mcp_core

        port: int | None = mcp_core._api_port()
    except Exception:  # noqa: BLE001 - unknowable port -> no local target is provably safe
        port = None
    try:
        return url_gate.url_allowed(url, allowed, port)
    except ValueError:  # an address the URL parser cannot read is no page to open
        return False


def _site_gate(sites: tuple[str, ...]) -> Any:
    """:func:`committed_url_allowed`, narrowed to the pages *sites* name."""

    def _allowed(url: str) -> bool:
        return committed_url_allowed(url) and url_matches(sites, url)

    return _allowed


def session_app(request: web.Request, key: str) -> str:
    """The app that owns the dashboard slot *key*, or ``""`` (no slot, or a person's)."""
    return slot_app(_request_state(request), key)


def slot_app(state: Any, key: str) -> str:
    """:func:`session_app`, read off the dashboard *state* itself."""
    slots = getattr(state, "_slots", None) or {}
    slot = slots.get(key) if isinstance(slots, dict) else None
    return str(getattr(slot, "_app", "") or "")


async def session_sites(request: web.Request, key: str, *aliases: str) -> tuple[str, ...]:
    """:func:`slot_sites` for the dashboard state *request* carries."""
    return await slot_sites(_request_state(request), key, *aliases)


async def slot_sites(state: Any, key: str, *aliases: str) -> tuple[str, ...]:
    """The approved ``browserSites`` of the app that owns slot *key*; empty for any other.

    The tool gate approves such a session's ``browser`` calls without a card
    (``hooks._tier_app_browser_sites``), so its ops are held to these pages
    whoever approved them. Read fresh per op, off the loop: a revoke lands at the
    next op. A slot the gate approved a call for in this process (*key* or one of
    *aliases*, the caller's own spelling) stays held once its sites are gone:
    :data:`browser_sites.NO_PAGES`, which admits no page, so a call approved before
    the revoke never runs unconfined.
    """
    app = slot_app(state, key)
    sites = await asyncio.to_thread(app_browser_sites, app) if app else ()
    if not sites and browser_sites.session_was_granted(
        key, *(a.removeprefix("dashboard:") for a in aliases)
    ):
        return browser_sites.NO_PAGES
    return sites


#: The refusal a held slot (:data:`browser_sites.NO_PAGES`) answers with: what happened
#: and how it ends. The hold lasts only while the app has no approved site.
SITES_REVOKED_TEXT = (
    "this app's approved browsing sites were withdrawn (the app was disabled or "
    "uninstalled, its trust was removed, or its site approval was dropped), so this "
    "chat opens no page; enable the app and approve its sites again on the app's page, "
    "and this chat browses those pages again"
)


def _off_site_url(op: str, args: dict[str, Any], sites: tuple[str, ...]) -> str | None:
    """The URL *op* would open that *sites* do not name, or None (the tool gate's reading)."""
    _readable, url = browser_call_url({"op": op, "args": args})
    if url is None or url_matches(sites, url.strip()):
        return None
    return url[:120]


async def _supervisor(hub: LiveHub, key: str) -> Any:
    """The gateway's supervisor with the daemon running, or None when not installed.

    The wait for ``ensure()`` is bounded and watched: while the supervisor reports
    ``waiting_allow`` the card is told so, which is how the person learns that
    Chrome is asking for a click.
    """
    home = config_dir()
    if not harness_ops.browsing_available(home):
        return None
    sup = importlib.import_module("kiro_crew.browser_harness.supervisor")
    instance = sup.get_supervisor(home)
    task = instance.ensure_in_background()
    deadline = asyncio.get_running_loop().time() + ENSURE_TIMEOUT_S
    told_allow = False
    while True:
        done, _pending = await asyncio.wait({task}, timeout=_ENSURE_POLL_S)
        if done:
            if task.cancelled():
                # Someone else stopped the start (Disconnect, or a policy stop).
                raise _StartStopped("the browser start was stopped")
            task.result()  # raises what ensure() raised
            return instance
        # ``status()`` stats the install marker and, before the first start settles
        # the browser choice, runs the discovery probes: off the loop, like ``_start``.
        status = await asyncio.to_thread(instance.status)
        if status.get("connection") == sup.WAITING_ALLOW and not told_allow:
            told_allow = True
            browser = status.get("browser") or {}
            hub.set_state(
                key,
                STATE_WAITING_ALLOW,
                browser=browser.get("kind"),
                headless=browser.get("headless"),
            )
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(str(status.get("detail") or "the browser did not come up in time"))


class _StartStopped(RuntimeError):
    """The background start this op waited on was cancelled by another caller."""


def _end_allow_wait(hub: LiveHub, key: str) -> None:
    """A start parked on Chrome's Allow sheet put the card in waiting_allow: end it."""
    sess = hub.get(key)
    if sess is not None and STATE_WAITING_ALLOW in (sess.state, sess.queued_state):
        hub.set_state(key, STATE_ERROR)


def _start_denied(request: Any, hub: LiveHub, key: str) -> tuple[dict[str, Any], int]:
    """The op's answer when a deny stopped the browser before it came up."""
    _end_allow_wait(hub, key)
    _audit(request, "denied", DENIED_CODE)
    return {"ok": False, "code": DENIED_CODE, "error": DENIED_TEXT}, 403


def _open_turn(hub: LiveHub, key: str) -> None:
    """The first browser op of a turn opens the card's turn; later ops join it.

    A session whose turn is over but still streams (the person reopened the
    finished card, or held the browser past the turn's end) is NOT a turn to join:
    the op opens a new one, and the running stream carries over to it.
    """
    sess = hub.get(key)
    if sess is None or not sess.turn_id or sess.state == STATE_DONE or sess.turn_over:
        hub.turn_started(key, uuid.uuid4().hex)


async def _bind_target(hub: LiveHub, key: str, supervisor: Any, call: Any = None) -> str:
    """Point the daemon's current tab at THIS session's own tab before an op runs.

    The daemon has one current tab and every chat shares the daemon, so without
    this chat B's ``click`` lands on the page chat A is reading -- or the page
    A's person has taken over. The rule: a session keeps the tab it was bound to
    while the browser still has it; a session with no live tab adopts the daemon's
    current tab if no other session owns it, else opens a tab of its own. Called
    under :data:`_OP_LOCK`, so the switch and the op that follows are one step.

    Under ``browser.isolation: per_session`` a session binds only a tab of its own
    browser context, created on its first isolated op and seeded with the edition's
    credentials; every tab the daemon opens during the op, ``tabs new`` included,
    lands in it through the relay (``CdpRelay.create_context``). It never adopts
    the daemon's tab. A session keeps a tab only in the context it browses in now,
    so after a flip of the setting either way the first op opens a new tab rather
    than keep one from the other side. An unreadable setting refuses the op
    (:class:`session_contexts.IsolationUnknown`).

    The context, its seed and ``create_context`` all live on one relay, and a
    helper call restarts a dead daemon onto a fresh relay (``supervisor.call`` goes
    through ``ensure()``), on which none of them exist: the tab would then open in
    the shared default context with isolation still on. So the browser is brought
    up BEFORE the relay is read, and that relay is recorded on the session
    (``LiveHub.bind_context``) as the pin every helper of the op, the bind's own
    included, is checked against (``caller_checked_call``): a restart in between
    refuses the op (``BrowserRestarted``).
    """
    call = supervisor.call if call is None else call
    if not session_contexts.per_session():
        hub.bind_context(key, "")
        return await _bind(hub, key, supervisor, call, context="")
    relay = (await supervisor.ensure()).relay
    context, fresh_context = await relay.ensure_session_context(key)
    hub.bind_context(key, context, relay)
    relay.create_context = context
    try:
        if fresh_context:
            await supervisor.seed_context(relay, context)
        return await _bind(hub, key, supervisor, call, context=context)
    except BaseException:
        if fresh_context:
            # A context that never got its seed and its first tab is not kept, so
            # the next op starts it over rather than read it as settled.
            relay.create_context = ""
            await relay.dispose_session_context(key)
        raise


async def _bind(hub: LiveHub, key: str, supervisor: Any, call: Any, *, context: str) -> str:
    """Bind *key* to a tab in *context*: ``""`` is the shared default context.

    Only in the default context may a session adopt the daemon's current tab.
    """
    relay = getattr(supervisor, "relay", None)
    ledgered = set(_ledgered_targets(supervisor))
    if relay is not None:
        ledgered = set(session_contexts.in_context(relay, ledgered, context))
    try:
        tab = await call("current_tab")
    except HelperError:
        # The daemon's tab is gone (an ended chat closed it, or the person did):
        # there is no current tab, so this session binds or opens its own.
        tab = None
    current = ""
    if isinstance(tab, dict):
        current = str(tab.get("targetId") or tab.get("target_id") or "")
    mine = hub.target_of(key)
    if mine and mine in ledgered:
        if current != mine:
            await call("switch_tab", target=mine, activate=False)
        return mine
    opener = _live_opener(hub, key, getattr(supervisor, "relay", None), mine, ledgered)
    if opener:
        # The session was on a popup that closed (a sign-in window that finished):
        # it goes back to the tab that opened it, not to a fresh blank one.
        if current != opener:
            await call("switch_tab", target=opener, activate=False)
        hub.bind_target(key, opener)
        hub.note_tab_moved(key, {"popup_closed": {"target_id": mine, "returned_to": opener}})
        return opener
    owner = hub.owner_of_target(current) if current else None
    if not context and current and current in ledgered and owner in (None, key):
        hub.bind_target(key, current)
        return current
    created = await call("new_tab", url="about:blank")
    target = str(created or "")
    if not target:
        raise RuntimeError("the browser opened no tab for this session")
    hub.bind_target(key, target)
    return target


def _restarted() -> dict[str, Any]:
    return harness_ops.refusal(
        "browser_restarted", "the browser restarted during the op; its context is gone"
    )


def _live_opener(hub: LiveHub, key: str, relay: Any, target: str, ledgered: set[str]) -> str:
    """The nearest still-open tab of *key*'s that opened *target*, or ``""``."""
    seen: set[str] = set()
    opener = _opener_of(relay, target) if relay is not None and target else ""
    while opener and opener not in seen:
        if opener in ledgered and hub.owner_of_target(opener) in (None, key):
            return opener
        seen.add(opener)
        opener = _opener_of(relay, opener)
    return ""


#: Longest popup ``url`` and ``title`` an answer carries (the live hub's caps).
POPUP_URL_CAP = 2048
POPUP_TITLE_CAP = 300


async def follow_popup(
    hub: LiveHub,
    key: str,
    supervisor: Any,
    call: Any,
    url_allowed: Callable[[str], bool] = committed_url_allowed,
) -> dict[str, Any] | None:
    """Move *key* onto a popup its tab opened since the last look; describe it.

    A sign-in button that opens its own window leaves the agent on the page behind
    it, where nothing happens, so the newest popup becomes the session's tab, as a
    person's browser would bring it to the front. Returns what the op's answer
    carries (``target_id``, plus ``url`` and ``title`` when the navigate gate admits
    the address), or ``None`` when no popup opened. The gate is still applied to the
    page itself when the stream follows the tab (:func:`follow_current_tab`).

    Only a popup in the context the op's bind put *key* in is followed: after a
    flip of ``browser.isolation``, a popup the chat's tab from the other side opens
    is still the chat's (its end closes it), but the chat never moves onto it.
    """
    relay = getattr(supervisor, "relay", None)
    claimed = claim_popups(hub, key, relay)
    if relay is None or not claimed:
        return None
    followable = session_contexts.in_context(relay, claimed, hub.context_of(key))
    if not followable:
        return None
    popup = followable[-1]
    await call("switch_tab", target=popup, activate=False)
    hub.bind_target(key, popup)
    view: dict[str, Any] = {"target_id": popup}
    try:
        tab = await call("current_tab")
    except HelperError:
        hub.note_tab_moved(key, {"popup": view})
        return view
    url = str(tab.get("url") or "") if isinstance(tab, dict) else ""
    if url and url_allowed(url):
        view["url"] = url[:POPUP_URL_CAP]
        view["title"] = str(tab.get("title") or "")[:POPUP_TITLE_CAP]
    hub.note_tab_moved(key, {"popup": view})
    return view


#: How long a failed op waits for the browser to report its popup gone. Chrome's
#: ``targetDestroyed`` can trail the daemon's lost session by a few frames.
POPUP_CLOSE_GRACE_S = 1.0
_POPUP_CLOSE_POLL_S = 0.05


async def _bound_popup_gone(
    hub: LiveHub, key: str, supervisor: Any, *, sleep: Any = asyncio.sleep
) -> str:
    """*key*'s bound tab, when it is a popup that left the ledger, else ``""``.

    Waits up to :data:`POPUP_CLOSE_GRACE_S` for the browser to report it gone.
    """
    relay = getattr(supervisor, "relay", None)
    popup = hub.target_of(key)
    if relay is None or not popup or not _opener_of(relay, popup):
        return ""
    waited = 0.0
    while popup in set(_ledgered_targets(supervisor)):
        if waited >= POPUP_CLOSE_GRACE_S:
            return ""
        await sleep(_POPUP_CLOSE_POLL_S)
        waited += _POPUP_CLOSE_POLL_S
    return popup


async def _popup_closed_under_op(
    hub: LiveHub, key: str, supervisor: Any, call: Any, *, sleep: Any = asyncio.sleep
) -> dict[str, Any] | None:
    """The answer for an op whose own action closed its popup, or ``None``.

    Only a failure of the op itself reaches here, never one of the bind before it.
    Only a popup counts (:func:`_bound_popup_gone`). Then the session returns to
    its opener and the op answers ``ok`` with ``popup_closed``; any other failure
    stays the error it was.
    """
    relay = getattr(supervisor, "relay", None)
    popup = await _bound_popup_gone(hub, key, supervisor, sleep=sleep)
    if not popup:
        return None
    ledgered = set(_ledgered_targets(supervisor))
    opener = _live_opener(hub, key, relay, popup, ledgered)
    if not opener:
        return None
    try:
        await call("switch_tab", target=opener, activate=False)
    except Exception:  # noqa: BLE001 - the next op's bind retries the switch
        logger.debug("browser: returning to the opener of a closed popup failed", exc_info=True)
    hub.bind_target(key, opener)
    return {
        "ok": True,
        "popup_closed": {
            "target_id": popup,
            "returned_to": opener,
            "hint": "the popup window closed during this action (for example a "
            "finished sign-in); ops now run on the tab that opened it. Snapshot it.",
        },
    }


#: Ops that act on an element or a point of the current page. A ref or a coordinate
#: is per document (``e1`` exists on every page), so after the bind moved the chat
#: onto a different page such an op would hit an unrelated element.
_PAGE_TARGETED_OPS = frozenset(
    {
        "click",
        "drag",
        "scroll",
        "type",
        "paste",
        "press_key",
        "hover",
        "select_option",
        "dialog",
        "reflex",
    }
)


#: What each kind of unreported tab move tells the agent.
_TAB_MOVE_HINTS = {
    "popup": "a popup window opened and is now the current tab; snapshot it. "
    "When it closes, ops return to the tab that opened it.",
    "popup_closed": "the popup window closed; ops now run on the tab that opened it. "
    "Snapshot it.",
}


def _refused_for_tab_move(hub: LiveHub, key: str, op: str) -> dict[str, Any] | None:
    """The refusal for a page-targeted op while a tab move is unreported, or ``None``.

    The ref or point the agent sent was read off the page it was on before a popup
    moved its tab, so it would land on an unrelated element. Ops that do not point
    into the page (``navigate``, ``snapshot``, ``screenshot``, ``tabs``, ...) run.
    """
    pending = hub.pending_tab_move(key)
    if op not in _PAGE_TARGETED_OPS or not pending:
        return None
    if "popup" in pending:
        return harness_ops.refusal(
            "popup_opened",
            "a popup window opened and is now the current tab; snapshot it before acting",
        )
    return harness_ops.refusal(
        "popup_closed",
        "the popup closed and ops now run on the tab that opened it; snapshot it before acting",
    )


def _with_tab_move(hub: LiveHub, key: str, answer: Any) -> Any:
    """Report the unreported tab move on *answer*, success or refusal alike.

    Taking it clears it, so the agent is told exactly once. A non-answer (the
    person holds the browser, or a claim cut the op short) leaves it pending.
    """
    if not isinstance(answer, dict) or answer.get("code") == DENIED_CODE:
        # A governance deny is shown as fixed text, so the move waits for the next answer.
        return answer
    notice = hub.take_tab_move(key)
    if not notice:
        return answer
    out = dict(answer)
    for kind, view in notice.items():
        if kind not in out:
            out[kind] = {**view, "hint": _TAB_MOVE_HINTS[kind]}
    return out


async def follow_current_tab(
    hub: LiveHub,
    key: str,
    supervisor: Any,
    *,
    caller_key: str | None = None,
    sites: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Point the session's stream at the daemon's current tab; update the address pill.

    One cheap IPC call (``current_tab``) per op. A new target (first op, ``tabs
    new``/``switch``) starts or restarts the screencast on it; the same target only
    refreshes url and title. Failures are logged, never surfaced: the op itself
    already answered. The one exception is a browser restarted since the session's
    bind pinned a relay (``LiveHub.bound_relay``): the follow refuses with
    ``browser_restarted`` instead of streaming the new browser's default context.
    """
    relay = getattr(supervisor, "relay", None)
    if relay is None:
        return None
    bound = hub.bound_relay(key)
    if bound is not None and bound is not relay:
        # The browser restarted after the bind: the tab on the new relay is the
        # default context's, not this chat's to stream, and the op's own answer
        # may have read the restart as a failed step rather than named it.
        return _restarted()
    call = (
        supervisor.call
        if caller_key is None
        else caller_checked_call(supervisor, caller_key, hub, key)
    )
    try:
        tab = await call("current_tab")
    except BrowserRestarted:
        return _restarted()
    except Exception:  # noqa: BLE001 - the card is best-effort beside the op
        logger.debug("browser live: current_tab failed", exc_info=True)
        return None
    if not isinstance(tab, dict):
        return None
    target = str(tab.get("targetId") or tab.get("target_id") or "")
    if not target:
        return None
    url = str(tab.get("url") or "")
    title = str(tab.get("title") or "")
    sess = hub.get(key)
    # The op may have moved the daemon (``tabs new``, ``tabs switch``): the tab it
    # is on now is this session's, unless another session already owns it -- and
    # then it is NOT this card's to show either: streaming it would put chat A's
    # page on chat B's card and route B's takeover input onto A's tab, around the
    # fence ``_bind_target`` built. The next op re-binds B to its own tab.
    owner = hub.owner_of_target(target)
    if owner not in (None, key):
        logger.debug("browser live: current tab %s belongs to %s, not streamed", target, owner)
        return None
    # A popup that has not committed its first document reports url "": it is left
    # to load (not sent to about:blank) but not streamed either, so no frame of the
    # page it lands on is published before a later follow judges that URL. Only the
    # exact string passes: a missing or non-string url is still judged.
    if tab.get("url") == "":
        if target in set(_ledgered_targets(supervisor)):
            hub.bind_target(key, target)
        await hub.stop_stream(key)
        return None
    if tab.get("url") != "about:blank":
        refused = await harness_ops._refuse_committed_url(
            build_context(key, supervisor, hub, caller_key=caller_key, sites=sites), url
        )
        if refused is not None:
            await hub.stop_stream(key)
            hub.page_changed(key, url="about:blank", title="")
            return refused
    if target in set(_ledgered_targets(supervisor)):
        hub.bind_target(key, target)
    stream = sess.stream if sess is not None else None
    if stream is not None and getattr(stream, "target_id", None) == target:
        hub.page_changed(key, url=url, title=title)
        return None
    browser: dict[str, Any] = {}
    if hasattr(supervisor, "status"):
        browser = (await asyncio.to_thread(supervisor.status)).get("browser") or {}
    _live_handlers().refresh_live_config(hub)
    try:
        await hub.start_stream(
            key,
            relay,
            target,
            browser=str(browser.get("kind") or "kiro_chromium"),
            headless=bool(browser.get("headless", True)),
            url=url,
            title=title,
        )
    except Exception:  # noqa: BLE001 - a screencast that will not start leaves the op intact
        logger.debug("browser live: start_stream failed", exc_info=True)
    return None


async def api_browser_op(request: web.Request) -> web.Response:
    """POST /api/browser/op -- see the module docstring."""
    if request.get("internal_auth") is not True:
        _audit(request, "denied", "internal-secret authentication required")
        return web.json_response({"error": "loopback only", "code": "loopback_only"}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    op = body.get("op")
    args = body.get("args")
    session_key = body.get("session_key")
    if not isinstance(op, str) or op not in harness_ops.OPS:
        return web.json_response({"error": "unknown op", "code": "bad_op"}, status=400)
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return web.json_response(
            {"error": "args must be an object", "code": "args_must_be_object"}, status=400
        )
    if not isinstance(session_key, str) or not session_key:
        return web.json_response(
            {"error": "session_key required", "code": "session_key_required"}, status=400
        )
    answer, status = await run_browser_op(
        session_key, op, args, state=_request_state(request), caller=request
    )
    if status == 403:
        # The one answer that is not 200: a browse deny before the op started.
        return web.json_response(
            {"ok": False, "code": DENIED_CODE, "error": DENIED_TEXT}, status=403
        )
    return web.json_response(answer)


class GatewayCaller:
    """The audit identity of a caller inside the gateway (an app's ``PagePort``).

    :func:`_audit` reads a request's ``user``, ``remote`` and ``path``; this answers
    the same three, so an SDK op writes the same audit row a Bot op does.
    """

    remote = ""

    def __init__(self, user: str, path: str = "app.browser") -> None:
        self._user = user
        self.path = path

    def get(self, key: str, default: Any = None) -> Any:
        return self._user if key == "user" else default


def _request_state(request: web.Request) -> Any:
    return request.app.get("state") if hasattr(request.app, "get") else None


#: Refused before the op reaches the browser: the address it asked to open is not
#: one the navigate gate admits (:func:`committed_url_allowed`).
URL_BLOCKED_TEXT = (
    "the browser only opens public http(s) pages, plus the loopback origins the user "
    "listed under Settings -> Browser -> Local preview origins; this address is refused"
)


async def run_browser_op(
    session_key: str,
    op: str,
    args: dict[str, Any],
    *,
    state: Any = None,
    caller: Any = None,
) -> tuple[dict[str, Any], int]:
    """Run one browser op for *session_key* through every gate; ``(answer, http_status)``.

    The one door every browser op goes through: the ``/api/browser/op`` route (the
    Bot's ``browser`` tool) and an app's ``PagePort`` (``apps/browser_page_port.py``)
    both call it, so no gate has two copies. In order: the ``capabilities.browse``
    check for *session_key*; the navigate gate on the address the op asks to open
    (before the browser starts, so a refused address is never requested); an app
    slot's approved ``browserSites``; the takeover hold; the gateway-wide
    :data:`_OP_LOCK`; the second browse check after the lock;
    the per-session fence shared with takeover; the tab binding; and the per-caller
    checks of :func:`caller_checked_call` and :class:`_GatedRelay`.

    *session_key* is the caller's runtime session key (``dashboard:<slot>`` for a
    dashboard slot), which governance binds on. *state* is the dashboard state, read
    for the slot's owning app. *caller* is the audit identity: the route's request,
    or a :class:`GatewayCaller`. Answers are the route's own bodies; the status is
    403 only for a browse deny before the op started, else 200.
    """
    # The audit identity every helper below reads (``_audit`` takes a request's shape).
    request: Any = caller if caller is not None else GatewayCaller("gateway")
    if op not in harness_ops.OPS or not isinstance(args, dict):
        return {"error": "unknown op", "code": "bad_op"}, 400
    # The shim asked (and audited) the same row before posting; this is the
    # gateway's own check, which a direct POST cannot skip and which fails closed.
    denied = await browse_refusal(session_key=session_key, audit=False)
    if denied is not None:
        _audit(request, "denied", DENIED_CODE)
        return {"error": DENIED_TEXT, "code": DENIED_CODE}, denied.status
    # The shim gates the requested address too, and answers sooner; this copy is the
    # one an in-gateway caller cannot skip. Without it a request to an internal
    # address leaves before the landing check (``_refuse_committed_url``) can refuse.
    _readable, wanted = browser_call_url({"op": op, "args": args})
    # Off the loop: the gate reads the operator's local-origins setting from disk.
    if isinstance(wanted, str) and not await asyncio.to_thread(
        committed_url_allowed, wanted.strip()
    ):
        _audit(request, "denied", "url_blocked")
        return harness_ops.refusal("url_blocked", URL_BLOCKED_TEXT), 200
    hub = get_live_hub()
    key = live_session_key(session_key)
    sites = await slot_sites(state, key, session_key)
    off_site = _off_site_url(op, args, sites) if sites else None
    if off_site is not None:
        _audit(request, "denied", "site_not_allowed")
        return (
            harness_ops.refusal(
                "site_not_allowed",
                (
                    SITES_REVOKED_TEXT
                    if sites == browser_sites.NO_PAGES
                    else f"{off_site} is not one of the pages this app may browse; "
                    "open one of its approved sites instead"
                ),
            ),
            200,
        )
    if not isinstance(request, GatewayCaller):
        # The card's turn is the Bot's: its turn end folds it. An app's op joins a
        # turn the Bot has open and never opens one, since nothing would end it,
        # and opening one would wipe what the person said for the Bot.
        _open_turn(hub, key)
    try:
        supervisor = await _supervisor(hub, key)
    except BrowsingDenied:
        return _start_denied(request, hub, key)
    except _StartStopped as exc:
        if await browse_denied_async(session_key, audit=False):
            return _start_denied(request, hub, key)
        _end_allow_wait(hub, key)
        return {"ok": False, "code": "browser_error", "error": str(exc)}, 200
    except Exception as exc:  # noqa: BLE001 - the browser plane failed to come up
        logger.warning("browser op: supervisor unavailable: %s", exc)
        return {"ok": False, "code": "browser_error", "error": _safe(exc)}, 200
    if supervisor is None:
        from kiro_crew.dashboard.handlers.browser_harness_status import (
            _install_active,
            repair_browser_harness,
        )

        # A job already running (a repair an earlier op or Settings started, or an
        # Install press) is a retry too, not "ask the user to install".
        if state is not None and (await repair_browser_harness(state) or _install_active(state)):
            return {
                "ok": False,
                "code": "browser_repairing",
                "error": "the browser engine is being installed; try again in a minute",
            }, 200
        return (
            {
                "ok": False,
                "code": "not_installed",
                "error": "browsing is not set up on this host; install it from Settings -> Browser",
            },
            200,
        )
    ctx = build_context(key, supervisor, hub, caller_key=session_key, sites=sites)
    # The optional decision-model hooks (``browser.step``), keyed by the caller's own
    # session key -- the identity the gate samples and governance binds on, not the
    # card's slot key. They ask the gate first and run the op exactly as written
    # whenever it answers no.
    ctx.decisions = BrowserDecisions(session_key)
    # Plus the decision hooks' and the change report's own ceilings, so neither ever
    # eats into the op's time.
    budget = _op_budget(op, ctx)
    if hub.in_control(key):
        # Answered at once, with ``hold``: the shim parks the tool and polls
        # ``/api/browser/handback`` until the person lets go, then posts the op
        # again. Nothing waits here, so other chats keep browsing meanwhile.
        return _held(request, hub, key, op, interrupted=False)
    try:
        await asyncio.wait_for(_OP_LOCK.acquire(), timeout=LOCK_WAIT_S)
    except asyncio.TimeoutError:
        # Bounded so this route always answers inside the shim's budget: a fast op
        # queued behind another chat's slow one is told so, instead of landing after
        # the tool has already reported it failed.
        _audit(request, "error", "browser_busy")
        return {
            "ok": False,
            "code": "browser_busy",
            "error": "another chat is using the browser",
        }, 200
    try:
        # The route's first check may be a whole queued op old by now: ask again for
        # the caller before anything reaches the browser, the credential refresh too.
        late = await _denied_after_op(request, session_key)
        if late is not None:
            return late, 200
        if op in _NAVIGATING_OPS:
            await _refresh_credentials(supervisor)
        # The per-session fence is shared with the person's Take over (LiveHub.claim):
        # a claim waits for this op to leave it (cancelling the op if it is still
        # running), so a takeover never lands inside an op, and the in_control check
        # inside is then the whole story.
        async with hub.op_fence(key):
            mark = hub.op_mark(key)
            result = await _fenced_op(
                request,
                hub,
                key,
                supervisor,
                ctx,
                op,
                args,
                budget,
                caller_key=session_key,
                sites=sites,
            )
    finally:
        _OP_LOCK.release()
    if result is _HELD:
        return _held(request, hub, key, op, interrupted=False)
    if result is _INTERRUPTED:
        hub.record_op(key, op, args, {"ok": False, "code": "interrupted"}, mark)
        return _held(request, hub, key, op, interrupted=True)
    assert isinstance(result, dict)
    # The turn replay's event for this op: generic, from the step it reported and
    # the result it answered (``browser_harness.recording.agent_event``). An ask
    # records its own ``person`` events instead.
    if op != "ask":
        hub.record_op(key, op, args, result, mark)
    # A note left for a parked poll that never came back to take it reaches this op.
    # The note is the person's word to the Bot: an app's op leaves it for the Bot.
    said = None if isinstance(request, GatewayCaller) else hub.take_handback(key)
    if result.get("ok"):
        answer: dict[str, Any] = {"ok": True, "result": result}
        if said is not None:
            answer["handed_back"] = said
        return answer, 200
    if said is not None:
        result = {**result, "handed_back": said}
    if sites == browser_sites.NO_PAGES and result.get("code") == "redirect_refused":
        # A held slot's page was blanked: say why it is held and how it ends.
        result = {**result, "error": SITES_REVOKED_TEXT}
    return result, 200


#: Ops that can load a new page. Before each, the gateway asks the edition's
#: credential provider whether its source changed (``browser_harness/credentials.py``),
#: so a sign-in the person just re-ran is in the browser before the page loads.
_NAVIGATING_OPS = frozenset({"navigate", "tabs"})


async def _refresh_credentials(supervisor: Any) -> None:
    """Re-pull credentials whose source changed; never fails the op it precedes."""
    refresh = getattr(supervisor, "refresh_credentials_if_source_changed", None)
    if refresh is None:
        return
    try:
        await refresh()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the page still loads with what the browser holds
        logger.warning("browser op: credential refresh failed", exc_info=True)


#: ``_fenced_op`` outcomes that are not a result: the person claimed before the op
#: touched the page (the shim holds, then re-posts), or cut it short mid-flight
#: (the shim holds, then reports ``interrupted``; the op is never re-run).
_HELD = object()
_INTERRUPTED = object()


def _held(
    request: Any, hub: LiveHub, key: str, op: str, *, interrupted: bool
) -> tuple[dict[str, Any], int]:
    """The person holds the browser: a refusal the shim turns into a parked wait."""
    _audit(request, "allowed")
    if not isinstance(request, GatewayCaller):
        # The shim now polls for the hand-back: a note the person writes goes to it.
        # An app's call does not wait for one, so it parks nothing.
        hub.park(key)
    if interrupted:
        body = harness_ops.refusal(
            "interrupted",
            f"the user took over the browser while `{op}` was running, so it may not "
            "have finished; take a snapshot to see where the page is before repeating it",
        )
    else:
        body = harness_ops.refusal(
            "user_in_control",
            "the user has taken over the browser; the op runs once they hand it back",
        )
    body["hold"] = True
    return body, 200


async def api_browser_handback(request: web.Request) -> web.Response:
    """POST /api/browser/handback -- has the person handed the browser back yet?

    The parked ``browser`` tool's poll while the person is in control. Same trust
    class as ``/api/browser/op`` (internal secret, strict path). Body
    ``{"session_key": str, "options"?: [str]}`` (``options``: up to four short
    hand-back choices the parked op offers the person). Waits at most
    ``HANDBACK_WAIT_S`` on the hub's release signal (a short long-poll, so a release
    lands within a second or two of the Esc) and answers ``{"released": bool}``,
    plus, once released, ``handed_back``: ``{"choice", "note"}`` the person gave as
    they handed back, or ``null``. That is the person's own words from the card's
    hand-back bar; a poll never carries anything read from the page.
    """
    if request.get("internal_auth") is not True:
        _audit(request, "denied", "internal-secret authentication required")
        return web.json_response({"error": "loopback only", "code": "loopback_only"}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = None
    session_key = body.get("session_key") if isinstance(body, dict) else None
    if not isinstance(session_key, str) or not session_key:
        return web.json_response(
            {"error": "session_key required", "code": "session_key_required"}, status=400
        )
    # A deny ends a parked op: the tool reads the code and stops waiting.
    denied = await browse_refusal(session_key=session_key, audit=False)
    if denied is not None:
        return denied
    hub = get_live_hub()
    key = live_session_key(session_key)
    if "options" in body:
        # The parked op's own hand-back choices, shown on the card's hand-back bar.
        hub.set_handback_options(key, body.get("options"))
    released = await hub.wait_for_release(key, HANDBACK_WAIT_S)
    if hub.hold_over(key):
        # The hub's hold ended first: any hand-back since went to chat, so the op
        # stops here instead of running behind what the person said there. A note
        # given to this op before it was taken back again still reaches it here.
        # The end is left in place, so every call parked in this session hears it.
        said = hub.take_handback(key)
        return web.json_response(
            {
                "released": False,
                "hold_over": True,
                "hold_timeout_s": HOLD_TIMEOUT_S,
                "handed_back": said,
            }
        )
    answer: dict[str, Any] = {"released": released, "hold_timeout_s": HOLD_TIMEOUT_S}
    if released:
        hub.handback_seen(key)
        # What the person said as they handed back: their own words, never page content.
        answer["handed_back"] = hub.take_handback(key)
    return web.json_response(answer)


async def _fenced_op(
    request: web.Request,
    hub: LiveHub,
    key: str,
    supervisor: Any,
    ctx: harness_ops.OpContext,
    op: str,
    args: dict[str, Any],
    budget: float,
    *,
    caller_key: str = DASHBOARD_SURFACE_KEY,
    sites: tuple[str, ...] = (),
) -> dict[str, Any] | object:
    """:func:`_run_fenced_op`, with any unreported popup tab move on its answer.

    Every answer carries it, a refusal or a failure as much as a success: a chat
    moved onto a popup without being told would aim its next ref at a page it
    never saw.
    """
    answer = await _run_fenced_op(
        request, hub, key, supervisor, ctx, op, args, budget, caller_key=caller_key, sites=sites
    )
    if op == "ask":
        # A quick ask's text is its outcome alone (``mcp_tools.browser._ask_text``),
        # so the move would be cleared without being shown: it waits for the next op.
        return answer
    return _with_tab_move(hub, key, answer)


async def _run_fenced_op(
    request: web.Request,
    hub: LiveHub,
    key: str,
    supervisor: Any,
    ctx: harness_ops.OpContext,
    op: str,
    args: dict[str, Any],
    budget: float,
    *,
    caller_key: str = DASHBOARD_SURFACE_KEY,
    sites: tuple[str, ...] = (),
) -> dict[str, Any] | object:
    """Bind, run, follow -- under both locks.

    Returns the op's result, or :data:`_HELD` (the person claimed before the op
    touched the page) or :data:`_INTERRUPTED` (the claim cut the op short).
    *caller_key* is the caller's runtime session key, which every helper, frame send
    and closing check asks the ceiling for.
    """
    if hub.in_control(key):
        # Before any tab switch: the person holds this session's tab.
        return _HELD
    sess = hub.get(key)
    if op != "ask" and sess is not None and sess.ask is not None and not sess.ask.outcome:
        # A quick ask waits on the person: until it ends the page is theirs, so a
        # parallel tool call (or a reflex loop) cannot press the robot check or the
        # confirm button, type into the asked field, or move the tab. Checked under
        # both locks, where an ask is put on the card, so it cannot race one.
        return harness_ops.refusal(
            "ask_pending", "a quick ask is waiting for the user; wait for its outcome"
        )

    try:
        return await _bound_op(
            request, hub, key, supervisor, ctx, op, args, budget, caller_key, sites
        )
    finally:
        relay = getattr(supervisor, "relay", None)
        if relay is not None:
            # A tab opened outside an op (the daemon's own) belongs to no chat.
            relay.create_context = ""


async def _bound_op(
    request: web.Request,
    hub: LiveHub,
    key: str,
    supervisor: Any,
    ctx: harness_ops.OpContext,
    op: str,
    args: dict[str, Any],
    budget: float,
    caller_key: str,
    sites: tuple[str, ...],
) -> dict[str, Any] | object:
    async def _bind() -> dict[str, Any] | None:
        # The bind comes first: it records the context this op browses in, which
        # decides the popups it may follow. Then a popup that opened after the last
        # op's answer is followed, so this op runs on it.
        try:
            await _bind_target(hub, key, supervisor, ctx.helper)
        except (BrowsingDenied, BrowserRestarted, session_contexts.IsolationUnknown):
            raise
        except Exception:
            # The bound popup closed while the bind ran (its ledger snapshot was
            # stale): bind again, now onto its opener, and the op still runs.
            if not await _bound_popup_gone(hub, key, supervisor):
                raise
            await _bind_target(hub, key, supervisor, ctx.helper)
        return await follow_popup(hub, key, supervisor, ctx.helper, ctx.url_allowed)

    bound = False
    try:
        await asyncio.wait_for(_bind(), timeout=OP_TIMEOUT_S)
        bound = True
        moved = _refused_for_tab_move(hub, key, op)
        if hub.in_control(key):
            # A claim landed during the bind; the op has not touched the page yet.
            return _HELD
        if op == "ask":
            # A quick ask binds to the card's own stream: make sure one runs on the
            # tab just bound, then put the ask on the card (it never runs ``run_op``).
            refused = await follow_current_tab(
                hub, key, supervisor, caller_key=caller_key, sites=sites
            )
            if refused is not None:
                _audit(request, "denied", str(refused.get("code") or ""))
                return refused
            from kiro_crew.dashboard.handlers.browser_ask import run_ask

            result = await asyncio.wait_for(run_ask(hub, key, args), timeout=OP_TIMEOUT_S)
            denied = await _denied_after_op(request, caller_key)
            if denied is not None:
                return denied
            _audit(request, "allowed")
            return result
        if moved is not None:
            # The ref or point the agent sent belongs to the page it left.
            result = moved
        else:
            max_ops, max_repeats = _run_limits()
            if max_ops or max_repeats:
                sess = hub.get(key)
                if sess is not None:
                    stopped = run_budget.check(
                        sess.run, op, args, max_ops=max_ops, max_repeats=max_repeats
                    )
                    if stopped is not None:
                        _audit(request, "denied", run_budget.CODE)
                        return stopped
            task = asyncio.ensure_future(harness_ops.run_op(op, args, ctx))
            hub.op_started(key, task)
            try:
                result = await asyncio.wait_for(task, timeout=budget)
            except asyncio.CancelledError:
                if hub.op_finished(key):
                    # Cancelled BY a claim, not by our own client leaving: the person
                    # took over mid-op; the op's remaining steps never ran.
                    return _INTERRUPTED
                raise
            finally:
                hub.op_finished(key)
    except asyncio.TimeoutError:
        _audit(request, "error", "timeout")
        return {"ok": False, "code": "timeout", "error": f"{op} ran past {budget:g}s"}
    except session_contexts.IsolationUnknown as exc:
        _audit(request, "error", "isolation_unknown")
        return harness_ops.refusal("isolation_unknown", _safe(exc))
    except BrowserRestarted as exc:
        # The chat's context went down with the old browser; its next op makes a new one.
        _audit(request, "error", "browser_restarted")
        return harness_ops.refusal("browser_restarted", _safe(exc))
    except BrowsingDenied:
        # A deny landed mid-op: the check that raised it has scheduled the stop
        # (``HarnessSupervisor.call``, ``caller_checked_call``, ``_GatedRelay``).
        _audit(request, "denied", DENIED_CODE)
        return harness_ops.refusal(DENIED_CODE, DENIED_TEXT)
    except Exception as exc:  # noqa: BLE001 - a helper or the daemon raised
        closed = await _popup_closed_under_op(hub, key, supervisor, ctx.helper) if bound else None
        if closed is not None:
            # The op's own click closed its popup (a sign-in window that finished):
            # that is the page working, not the op failing.
            if not hub.in_control(key):
                # The opener may have been sent somewhere the gate refuses as the
                # popup closed: that refusal is the answer, as on the success path.
                refused = await follow_current_tab(
                    hub, key, supervisor, caller_key=caller_key, sites=sites
                )
                if refused is not None:
                    _audit(request, "denied", str(refused.get("code") or ""))
                    return refused
            denied = await _denied_after_op(request, caller_key)
            if denied is not None:
                return denied
            _audit(request, "allowed")
            return closed
        logger.warning("browser op %s failed: %s", op, exc)
        _audit(request, "error", type(exc).__name__)
        return {"ok": False, "code": "browser_error", "error": _safe(exc)}
    denied = await _denied_after_op(request, caller_key)
    if denied is not None:
        return denied
    if not hub.in_control(key):
        await _follow_popup_after_op(hub, key, supervisor, ctx, op)
    if not hub.in_control(key):
        # Asked again: the person may have taken the tab during the late looks.
        refused = await follow_current_tab(hub, key, supervisor, caller_key=caller_key, sites=sites)
        if refused is not None:
            _audit(request, "denied", str(refused.get("code") or ""))
            return refused
    # Once more after the follow's own helpers, so the answer never reports success
    # under a ceiling that denied while it ran.
    denied = await _denied_after_op(request, caller_key)
    if denied is not None:
        return denied
    _audit(request, "allowed")
    return result


#: Ops whose page action may open a popup a little after the op answers: a click
#: handler or an Enter that calls ``window.open`` after a fetch. They get
#: :data:`POPUP_LATE_READS` more looks, :data:`POPUP_LATE_POLL_S` apart.
POPUP_LATE_OPS = frozenset({"click", "press_key"})
POPUP_LATE_READS = 3
POPUP_LATE_POLL_S = 0.15


async def _follow_popup_after_op(
    hub: LiveHub,
    key: str,
    supervisor: Any,
    ctx: harness_ops.OpContext,
    op: str,
) -> dict[str, Any] | None:
    """:func:`follow_popup` after the op answered; a failure leaves the answer as it was.

    After a :data:`POPUP_LATE_OPS` op, a popup that is not there on the first look
    is looked for again, a bounded number of times, and never once the person
    holds the tab. A deny that lands here is still caught: the check after it
    reports it.
    """

    async def _look() -> dict[str, Any] | None:
        view = await follow_popup(hub, key, supervisor, ctx.helper, ctx.url_allowed)
        if op not in POPUP_LATE_OPS:
            return view
        for _ in range(POPUP_LATE_READS):
            if view is not None or hub.in_control(key):
                break
            await asyncio.sleep(POPUP_LATE_POLL_S)
            # The op already finished, so a takeover during the wait cancels
            # nothing: it holds the lock and waits on the fence this poll holds.
            if hub.in_control(key):
                break
            view = await follow_popup(hub, key, supervisor, ctx.helper, ctx.url_allowed)
        return view

    try:
        return await asyncio.wait_for(_look(), timeout=OP_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - the op itself already answered
        logger.debug("browser: following a popup after the op failed", exc_info=True)
        return None


async def _denied_after_op(
    request: web.Request, caller_key: str = DASHBOARD_SURFACE_KEY
) -> dict[str, Any] | None:
    """The refusal when a deny landed while an op ran, else ``None``.

    For an op that caught the refusal itself (a reflex loop reads a refused helper as
    an unreadable page) or one whose deny landed after its last helper: the answer
    still says why it stopped, and the browser stops too. It asks for the engine's
    own key and for *caller_key*, the caller's runtime session key, which is the
    only one a surface-bound profile answers to; the route's first check stops the
    browser on that deny as well (``browse_refusal``).
    """
    if not await browse_denied_async(HOST_KEY, audit=False) and not await browse_denied_async(
        caller_key, audit=False
    ):
        return None
    stop_running_harness()
    _audit(request, "denied", DENIED_CODE)
    return harness_ops.refusal(DENIED_CODE, DENIED_TEXT)


def _safe(exc: BaseException) -> str:
    """One line of the failure, bounded, so a daemon traceback never floods the model."""
    text = str(exc).strip().splitlines()
    return (text[0] if text else type(exc).__name__)[:300]
