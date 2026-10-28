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
  ``not_installed`` (no Harness on this host), ``browser_error`` (the daemon or
  browser failed), ``timeout`` (the op ran past its budget), ``browser_busy``
  (another chat held the lock too long), ``user_in_control`` (the person held
  the browser for the whole ``HOLD_TIMEOUT_S``), ``interrupted`` (the person
  took over mid-op and has handed back; the op may not have finished).

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
post; this route trusts its one caller for those and enforces what only the
gateway can see: who holds the browser, which tabs the agent owns.

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
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import uuid
from typing import Any

from aiohttp import web

from kiro_crew.browser_harness import ops as harness_ops
from kiro_crew.browser_harness import url_gate
from kiro_crew.browser_harness.budgets import (
    DECISION_HEADROOM_S,
    ENSURE_TIMEOUT_S,
    HANDBACK_WAIT_S,
    HOLD_TIMEOUT_S,
    LOCK_WAIT_S,
    OP_TIMEOUT_S,
    SLOW_OP_TIMEOUT_S,
)
from kiro_crew.browser_harness.decide import BrowserDecisions
from kiro_crew.browser_harness.live_hub import (
    STATE_DONE,
    STATE_WAITING_ALLOW,
    LiveHub,
    get_live_hub,
)
from kiro_crew.config.paths import config_dir
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


def build_context(
    session_key: str, supervisor: Any, hub: LiveHub | None = None
) -> harness_ops.OpContext:
    """Bind the gateway's supervisor, relay and live hub to one caller's session."""
    hub = hub if hub is not None else get_live_hub()

    def _step(label: str) -> Any:
        return hub.step(session_key, label)

    def _in_control() -> Any:
        return hub.in_control(session_key)

    def _agent_targets() -> list[str]:
        # The tabs THIS session owns and the browser still has: ``tabs list`` shows
        # them and ``tabs switch`` moves only among them, never onto another chat's.
        mine = hub.targets_of(session_key)
        return [t for t in _ledgered_targets(supervisor) if t in mine]

    def _loop(info: Any) -> Any:
        return hub.loop(session_key, info)

    def _mark() -> Any:
        return hub.inner_mark(session_key)

    def _record(op: str, args: Any, result: Any, mark: Any) -> Any:
        if mark is None:
            return None
        return hub.record_op(session_key, op, args, result, mark, loop=True)

    def _frames() -> Any:
        return frame_ops(hub, session_key)

    return harness_ops.OpContext(
        helper=supervisor.call,
        step=_step,
        in_control=_in_control,
        agent_targets=_agent_targets,
        screenshot_dir=harness_ops.screenshot_dir(config_dir()),
        sleep=asyncio.sleep,
        url_allowed=committed_url_allowed,
        loop=_loop,
        mark=_mark,
        record=_record,
        frames=_frames,
    )


def frame_ops(hub: LiveHub, key: str) -> Any:
    """The cross-origin frames of *key*'s tab, through its live stream's session.

    ``None`` unless a stream runs on the tab the session is bound to: the frames'
    child sessions hang off that stream (``browser_harness.frames``).
    """
    from kiro_crew.browser_harness.frames import FrameOps

    sess = hub.get(key)
    stream = sess.stream if sess is not None else None
    registry = getattr(stream, "frames", None)
    if stream is None or registry is None or not stream.session_id:
        return None
    if stream.target_id != hub.target_of(key):
        return None
    return FrameOps(stream.relay, stream.session_id, registry, harness_ops.WALKER_SOURCE)


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
    return url_gate.url_allowed(url, allowed, port)


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


def _open_turn(hub: LiveHub, key: str) -> None:
    """The first browser op of a turn opens the card's turn; later ops join it.

    A session whose turn is over but still streams (the person reopened the
    finished card, or held the browser past the turn's end) is NOT a turn to join:
    the op opens a new one, and the running stream carries over to it.
    """
    sess = hub.get(key)
    if sess is None or not sess.turn_id or sess.state == STATE_DONE or sess.turn_over:
        hub.turn_started(key, uuid.uuid4().hex)


async def _bind_target(hub: LiveHub, key: str, supervisor: Any) -> str:
    """Point the daemon's current tab at THIS session's own tab before an op runs.

    The daemon has one current tab and every chat shares the daemon, so without
    this chat B's ``click`` lands on the page chat A is reading -- or the page
    A's person has taken over. The rule: a session keeps the tab it was bound to
    while the browser still has it; a session with no live tab adopts the daemon's
    current tab if no other session owns it, else opens a tab of its own. Called
    under :data:`_OP_LOCK`, so the switch and the op that follows are one step.
    """
    ledgered = set(_ledgered_targets(supervisor))
    tab = await supervisor.call("current_tab")
    current = ""
    if isinstance(tab, dict):
        current = str(tab.get("targetId") or tab.get("target_id") or "")
    mine = hub.target_of(key)
    if mine and mine in ledgered:
        if current != mine:
            await supervisor.call("switch_tab", target=mine, activate=False)
        return mine
    owner = hub.owner_of_target(current) if current else None
    if current and current in ledgered and owner in (None, key):
        hub.bind_target(key, current)
        return current
    created = await supervisor.call("new_tab", url="about:blank")
    target = str(created or "")
    if not target:
        raise RuntimeError("the browser opened no tab for this session")
    hub.bind_target(key, target)
    return target


async def follow_current_tab(hub: LiveHub, key: str, supervisor: Any) -> None:
    """Point the session's stream at the daemon's current tab; update the address pill.

    One cheap IPC call (``current_tab``) per op. A new target (first op, ``tabs
    new``/``switch``) starts or restarts the screencast on it; the same target only
    refreshes url and title. Failures are logged, never surfaced: the op itself
    already answered.
    """
    relay = getattr(supervisor, "relay", None)
    if relay is None:
        return
    try:
        tab = await supervisor.call("current_tab")
    except Exception:  # noqa: BLE001 - the card is best-effort beside the op
        logger.debug("browser live: current_tab failed", exc_info=True)
        return
    if not isinstance(tab, dict):
        return
    target = str(tab.get("targetId") or tab.get("target_id") or "")
    if not target:
        return
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
        return
    if target in set(_ledgered_targets(supervisor)):
        hub.bind_target(key, target)
    stream = sess.stream if sess is not None else None
    if stream is not None and getattr(stream, "target_id", None) == target:
        hub.page_changed(key, url=url, title=title)
        return
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
    hub = get_live_hub()
    key = live_session_key(session_key)
    _open_turn(hub, key)
    try:
        supervisor = await _supervisor(hub, key)
    except Exception as exc:  # noqa: BLE001 - the browser plane failed to come up
        logger.warning("browser op: supervisor unavailable: %s", exc)
        return web.json_response({"ok": False, "code": "browser_error", "error": _safe(exc)})
    if supervisor is None:
        return web.json_response(
            {
                "ok": False,
                "code": "not_installed",
                "error": "browsing is not set up on this host; install it from Settings -> Browser",
            }
        )
    ctx = build_context(key, supervisor, hub)
    # The optional decision-model hooks (``browser.step``), keyed by the caller's own
    # session key -- the identity the gate samples and governance binds on, not the
    # card's slot key. They ask the gate first and run the op exactly as written
    # whenever it answers no.
    ctx.decisions = BrowserDecisions(session_key)
    # Plus the decision hooks' own ceilings, so a hook never eats into the op's time.
    budget = (
        SLOW_OP_TIMEOUT_S if op in harness_ops.SLOW_OPS else OP_TIMEOUT_S
    ) + DECISION_HEADROOM_S
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
        return web.json_response(
            {"ok": False, "code": "browser_busy", "error": "another chat is using the browser"}
        )
    try:
        if op in _NAVIGATING_OPS:
            await _refresh_credentials(supervisor)
        # The per-session fence is shared with the person's Take over (LiveHub.claim):
        # a claim waits for this op to leave it (cancelling the op if it is still
        # running), so a takeover never lands inside an op, and the in_control check
        # inside is then the whole story.
        async with hub.op_fence(key):
            mark = hub.op_mark(key)
            result = await _fenced_op(request, hub, key, supervisor, ctx, op, args, budget)
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
    said = hub.take_handback(key)
    if result.get("ok"):
        answer: dict[str, Any] = {"ok": True, "result": result}
        if said is not None:
            answer["handed_back"] = said
        return web.json_response(answer)
    if said is not None:
        result = {**result, "handed_back": said}
    return web.json_response(result)


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
    request: web.Request, hub: LiveHub, key: str, op: str, *, interrupted: bool
) -> web.Response:
    """The person holds the browser: a refusal the shim turns into a parked wait."""
    _audit(request, "allowed")
    # The shim now polls for the hand-back: a note the person writes goes to it.
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
    return web.json_response(body)


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
    hub = get_live_hub()
    key = live_session_key(session_key)
    if "options" in body:
        # The parked op's own hand-back choices, shown on the card's hand-back bar.
        hub.set_handback_options(key, body.get("options"))
    released = await hub.wait_for_release(key, HANDBACK_WAIT_S)
    answer: dict[str, Any] = {"released": released, "hold_timeout_s": HOLD_TIMEOUT_S}
    if released:
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
) -> dict[str, Any] | object:
    """Bind, run, follow -- under both locks.

    Returns the op's result, or :data:`_HELD` (the person claimed before the op
    touched the page) or :data:`_INTERRUPTED` (the claim cut the op short).
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
        await asyncio.wait_for(_bind_target(hub, key, supervisor), timeout=OP_TIMEOUT_S)
        if hub.in_control(key):
            # A claim landed during the bind; the op has not touched the page yet.
            return _HELD
        if op == "ask":
            # A quick ask binds to the card's own stream: make sure one runs on the
            # tab just bound, then put the ask on the card (it never runs ``run_op``).
            await follow_current_tab(hub, key, supervisor)
            from kiro_crew.dashboard.handlers.browser_ask import run_ask

            result = await asyncio.wait_for(run_ask(hub, key, args), timeout=OP_TIMEOUT_S)
            _audit(request, "allowed")
            return result
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
    except Exception as exc:  # noqa: BLE001 - a helper or the daemon raised
        logger.warning("browser op %s failed: %s", op, exc)
        _audit(request, "error", type(exc).__name__)
        return {"ok": False, "code": "browser_error", "error": _safe(exc)}
    _audit(request, "allowed")
    if not hub.in_control(key):
        await follow_current_tab(hub, key, supervisor)
    return result


def _safe(exc: BaseException) -> str:
    """One line of the failure, bounded, so a daemon traceback never floods the model."""
    text = str(exc).strip().splitlines()
    return (text[0] if text else type(exc).__name__)[:300]
