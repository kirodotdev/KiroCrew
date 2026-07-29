"""Computer-use REST API — Settings → Computer Use, plus the loopback legs.

Four endpoints, two very different trust models:

``GET  /api/computer-use/config``  browser-called, cookie-authed
``PUT  /api/computer-use/config``  browser-called, cookie-authed
``POST /api/computer-use/invoke``  MACHINE-only (loopback + ``X-Internal-Secret``)
``POST /api/computer-use/frame``   MACHINE-only (loopback), live-view frame ingress

The config pair is the operator's own out-of-band control surface. It reports the
keystone primary enable, the ``config.json`` budget knobs, the ADVISORY macOS
permission rows, the read-only built-in denylist, and a governance summary; the
PUT writes the enable to the KEYSTONE and the budgets to ``config.json``. Because
this handler is the ONLY writer of the primary enable, and because it does not
route through the agent tool gate, it is what makes "the agent cannot turn its own
desktop automation on" true.

``POST /api/computer-use/invoke`` is the other half of the thin-shim architecture:
the ``kirocrew-computer`` stdio MCP process does no accessibility work and no
governance evaluation — it resolves its session identity strictly and forwards
here, so the authoritative fail-CLOSED gate, all native work and all SEL auditing
happen in the gateway. It is registered in ``server._STRICT_INTERNAL_API_PATHS``
(loopback + secret, no cookie fall-through) because no browser ever calls it.

``POST /api/computer-use/frame`` is the live-view (PiP) frame ingress. It carries
NO new capture: the capture layer relays the JPEG it already encoded for the
model, and this handler only rebroadcasts it over the existing websocket. The
three suppressions that make a pixel egress path acceptable (an unattributable
capture, a window holding a secure field, and a withheld screenshot channel) are
all enforced BEFORE the POST, in
``computer_use/screencast.py`` — this handler's own job is the loopback gate, the
field bounds, and the OWNER-only rebroadcast. See :func:`api_computer_use_frame`.

Blocking work is offloaded: the keystone/config reads and the governance profile
walk touch the filesystem, the permission probe SHELLS OUT to
``kirocrew computer doctor --json`` rather than calling ctypes in-process (a
ctypes fault is not catchable in Python and would take the whole gateway — every
chat session, the cron scheduler, the Slack socket — down with it), and the
dispatch itself performs accessibility calls that block for tens of milliseconds.
None of it may run on the event loop.

Two DIFFERENT pools, and the split matters: the config reads/writes use the default
executor (short, bounded filesystem work), while the dispatch uses
``executors.subprocess_executor`` — the bounded pool reserved for calls that can
block on a wedged external resource, which is exactly a hung target application
parking a worker for the driver's whole messaging timeout. See
:func:`_dispatch_off_loop`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.computer_use import enable_state
from kiro_crew.computer_use import overlay as cursor_overlay
from kiro_crew.computer_use.screencast import (
    COMPUTER_USE_FRAME_EVENT,
    build_frame_payload,
    frame_scope,
)
from kiro_crew.computer_use.types import (
    MAX_SCREENSHOT_MAX_PX,
    MAX_TEXT_LIMIT,
    MAX_TREE_DEPTH_LIMIT,
    MAX_TREE_NODES_LIMIT,
    MIN_SCREENSHOT_MAX_PX,
    PERMISSION_UNKNOWN,
    PERMISSION_UNSUPPORTED,
    PLATFORM_MACOS,
    STATE_KEY_ALLOWED_APPS,
    STATE_KEY_ENABLED,
    STATE_KEY_EXTRA_DENIED_APPS,
    PolicyConfig,
    PolicyStateError,
)
from kiro_crew.config.loader import computer_use_state_path
from kiro_crew.executors import subprocess_executor

logger = logging.getLogger(__name__)

# ── SEL operation names ──
# One prefix so the audit trail can be filtered to this surface with one glob.
OP_CONFIG_SAVE = "computer_use.config_save"
OP_INVOKE = "computer_use.invoke"
OP_FRAME = "computer_use.frame"

# The permission probe is a short-lived child running ``kirocrew computer doctor
# --json``. Fixed argv, no shell, no agent-steerable input.
DOCTOR_SUBCOMMAND = "computer"
DOCTOR_ARGS: tuple[str, ...] = ("doctor", "--json")
# Seconds to wait for it. The probe is two framework calls; anything slower means
# a stalled TCC daemon, and the Settings page must render regardless — the rows
# are advisory, so "unknown" is a fine answer and far better than a hung request.
DOCTOR_TIMEOUT_SECS = 5.0

# Body caps for the PUT. The app lists are operator-authored patterns; the caps
# exist so a malformed or hostile request cannot make the keystone unbounded.
MAX_APP_PATTERNS = 64
MAX_APP_PATTERN_LEN = 256

# Keystone keys the PUT accepts as pattern lists.
_APP_LIST_KEYS = (STATE_KEY_ALLOWED_APPS, STATE_KEY_EXTRA_DENIED_APPS)

# Budget knobs the PUT accepts, with their ``(min, max)`` bound. Mirrors the
# ceilings in ``computer_use.types`` (which the config loader re-clamps at load,
# so this is defense in depth rather than the only bound).
_INT_LIMITS: dict[str, tuple[int, int]] = {
    "max_tree_nodes": (1, MAX_TREE_NODES_LIMIT),
    "max_tree_depth": (1, MAX_TREE_DEPTH_LIMIT),
    "text_limit": (1, MAX_TEXT_LIMIT),
    "screenshot_max_px": (MIN_SCREENSHOT_MAX_PX, MAX_SCREENSHOT_MAX_PX),
    "screenshot_jpeg_quality": (1, 100),
}
_BOOL_KEYS = ("attach_screenshot", "cursor_motion")


ERR_STATE_CORRUPT = "computer-use settings could not be updated because a settings file is corrupt"
ERR_DISPATCH_FAILED = "Error: computer use failed unexpectedly"


class StateCorruptError(Exception):
    """A settings file exists but is unreadable/not-a-dict — refuse to mutate it.

    Mirrors ``handlers/security.py::ConfigCorruptError``, for the same reason: a
    mutation that read a corrupt keystone as ``{}`` and wrote it back would
    silently reset the operator's allow-list and blocked-app additions, and one
    that read a corrupt ``config.json`` as ``{}`` would replace the user's entire
    configuration with six computer-use keys. The READ path stays fail-soft (a
    corrupt keystone renders as disabled, which is the safe interpretation); only
    the WRITE path refuses.
    """


def _sel():
    """Late-binding ``sel()`` for test monkeypatch compatibility.

    The function-local import is the deliberate circular-import exception every
    sibling handler uses (``core.py``, ``sessions.py``, ``files.py``, ``cron.py``,
    ``memory.py``, ``agents.py``, ``hooks.py``, ``prompts.py``): this module is
    imported BY ``handlers/__init__``, so a top-level ``from .. import sel`` would
    be a cycle. Resolving the package attribute per call is also what lets a test
    monkeypatch ``handlers.sel`` and have it take effect here.
    """
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811 — circular import

    return _pkg.sel()


def _audit(
    request: web.Request,
    *,
    operation: str,
    outcome: str,
    resources: str = "",
    error: str = "",
) -> None:
    """Best-effort SEL audit; a logging failure never breaks the request."""
    try:
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=resources,
            error=error,
        )
    except Exception:
        logger.warning("SEL logging failed for %s", operation, exc_info=True)


async def _off_loop(fn):
    """Run a blocking (filesystem / governance-resolution) callable in a thread.

    Same rationale as ``handlers/security.py::_run_off_loop``: reading the
    keystone and resolving governance profiles both walk the filesystem, and a
    slow or stalled FS must not freeze the gateway's sole event loop.

    The DEFAULT executor is correct for these: they are short, bounded filesystem
    reads and atomic writes. The dispatch leg is NOT one of them — see
    :func:`_dispatch_off_loop`.
    """
    return await asyncio.get_running_loop().run_in_executor(None, fn)


async def _dispatch_off_loop(fn):
    """Run the blocking computer-use dispatch on the bounded subprocess pool.

    Deliberately NOT :func:`_off_loop`. A dispatch performs accessibility
    round-trips into ANOTHER process, and a hung target application parks the
    worker for the driver's whole ``AX_MESSAGING_TIMEOUT_SECS`` — a call that can
    block on a wedged external resource, which is precisely what
    ``executors.subprocess_executor`` exists to contain. On the default pool a
    handful of wedged desktop calls would starve every other
    ``run_in_executor(None, …)`` user in the gateway *and* the event loop's own
    ``getaddrinfo``, turning one unresponsive app into a gateway-wide stall.

    Matches ``computer_use.tools.dispatch``, which offloads onto the same pool for
    the same reason; this handler cannot simply call that coroutine because it
    would then pay two executor hops per request.
    """
    return await asyncio.get_running_loop().run_in_executor(subprocess_executor(), fn)


# ── Permission probe (out-of-process, ADVISORY) ──


def _permission_block(state: str) -> dict[str, str]:
    """A uniform permission block with both grants reported as *state*.

    ``unknown`` (rather than ``missing``) is the degrade value: the rows are
    advisory, and telling a user a grant is missing when we merely failed to ask
    would send them to System Settings for nothing.
    """
    return {"accessibility": state, "screen_recording": state, "responsible_hint": ""}


def _normalize_permissions(raw: object) -> dict[str, str]:
    """Coerce the doctor's JSON into the payload's permission block.

    Tolerant by construction: the probe is a separate process whose exact output
    shape must not be a hard dependency of the Settings page rendering. Accepts
    either a top-level object or one nested under ``permissions``; anything
    unexpected degrades to ``unknown``.
    """
    if not isinstance(raw, dict):
        return _permission_block(PERMISSION_UNKNOWN)
    nested = raw.get("permissions")
    block = nested if isinstance(nested, dict) else raw
    out = _permission_block(PERMISSION_UNKNOWN)
    for key in ("accessibility", "screen_recording"):
        value = block.get(key)
        if isinstance(value, str) and value:
            out[key] = value
    hint = block.get("responsible_hint")
    if isinstance(hint, str):
        out["responsible_hint"] = hint
    return out


async def _probe_permissions(platform_id: str) -> dict[str, str]:
    """Probe macOS Accessibility / Screen Recording OUT OF PROCESS.

    Shells ``kirocrew computer doctor --json`` instead of calling the frameworks
    here, and that is the whole point: a missing ctypes ``argtypes`` is a SIGSEGV,
    not an exception (reproduced live during design), and in the gateway that
    would take down every chat session, the cron scheduler, the Slack socket and
    the dashboard WebSocket at once. In a short-lived child the blast radius is a
    permission row that reads ``unknown``.

    Never raises — every failure degrades to ``unknown``, because these rows are
    ADVISORY and must never gate the feature: macOS attributes a TCC grant to the
    RESPONSIBLE PARENT of the process tree, so a probe can honestly report
    ``missing`` while a full-fidelity capture succeeds.
    """
    if platform_id != PLATFORM_MACOS:
        # Nothing to probe and nothing to spawn: TCC is a macOS concept.
        return _permission_block(PERMISSION_UNSUPPORTED)

    # Reuses the resolver the managed MCP servers use, so the probe runs the SAME
    # install as the gateway (and falls back to ``<interpreter> -m kiro_crew``
    # when no console script is on PATH — the systemd-user-service case).
    from kiro_crew.agent import _kirocrew_mcp_invocation

    exe, prefix = _kirocrew_mcp_invocation(DOCTOR_SUBCOMMAND)
    argv = [exe, *prefix, *DOCTOR_ARGS]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=DOCTOR_TIMEOUT_SECS)
    except (asyncio.TimeoutError, TimeoutError):
        # BOTH, deliberately. On Python 3.11+ these are the same class, but on 3.10
        # — which CI still gates on — ``asyncio.TimeoutError`` is a distinct class
        # that does NOT inherit from the builtin. Catching only one let a timeout
        # fall through to the generic ``except Exception`` below, which returns the
        # same degraded block but SKIPS THE KILL, leaking one stalled child per
        # Settings poll. This passed locally on 3.12 and failed only on the 3.10
        # shard.
        # Kill the child before giving up: leaving a stalled probe running would
        # accumulate one process per Settings poll (the panel polls every 5s while
        # a grant is missing).
        logger.debug("computer-use permission probe timed out after %ss", DOCTOR_TIMEOUT_SECS)
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return _permission_block(PERMISSION_UNKNOWN)
    except Exception:
        logger.debug("computer-use permission probe could not run", exc_info=True)
        return _permission_block(PERMISSION_UNKNOWN)
    if proc.returncode != 0:
        return _permission_block(PERMISSION_UNKNOWN)
    try:
        return _normalize_permissions(json.loads(stdout.decode("utf-8", errors="replace")))
    except Exception:
        logger.debug("computer-use permission probe returned unparseable JSON", exc_info=True)
        return _permission_block(PERMISSION_UNKNOWN)


def _read_state_strict() -> dict:
    """Read the keystone for a MUTATION: raise on corrupt, ``{}`` when absent."""
    path = computer_use_state_path()
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateCorruptError(str(exc)) from exc
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateCorruptError(f"computer_use.json is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise StateCorruptError("computer_use.json top level is not a JSON object")
    return loaded


def _snapshot() -> dict[str, Any]:
    """Build everything in the GET payload except the permission block.

    Synchronous (keystone read + config load + governance profile walk) — callers
    offload it. The permission block is spliced in by the handler because it needs
    an out-of-process probe.
    """
    from kiro_crew.computer_use.backend import get_shared_backend, platform_id_for_current_os
    from kiro_crew.config.loader import KiroCrewConfig

    state = enable_state.load_state()
    # A malformed ``allowed_apps`` raises by DESIGN on the action path — refusing is
    # the safe direction for a value that was clearly meant to narrow something.
    # Here, on the READ path, it must not: letting it escape turned the Settings GET
    # into an HTTP 500, so a hand-edited keystone made the only UI that can repair it
    # unreachable. The page has to render precisely BECAUSE the file is broken.
    #
    # The empty allow-list this falls back to is not a silent widening: it is what
    # the panel DISPLAYS, never what a check consults. Every dispatch still calls
    # ``load_policy_config`` itself and still refuses on the same malformed value, so
    # the ceiling is unchanged — only its rendering degrades. ``policy_error`` is
    # published so the panel can say so rather than showing a confidently empty list.
    policy_error = ""
    try:
        policy_cfg = enable_state.load_policy_config(state)
    except PolicyStateError as exc:
        policy_cfg = PolicyConfig()
        policy_error = str(exc)
        logger.warning("computer-use keystone policy is malformed: %s", exc)
    limits = KiroCrewConfig.load().computer_use
    platform_id = platform_id_for_current_os()

    # ``status()`` is the driver's own answer, which is strictly better than the
    # platform id: a macOS host whose frameworks failed to load reports
    # unsupported WITH the reason. Never fatal — a driver that cannot even be
    # constructed still has to render a Settings row explaining why.
    supported = False
    reason = ""
    try:
        status = get_shared_backend().status()
        supported = bool(status.supported)
        reason = status.reason
        platform_id = status.platform_id or platform_id
    except Exception:
        logger.debug("computer-use backend status unavailable", exc_info=True)
        reason = "the computer-use driver could not be loaded on this host"

    return {
        "enabled": enable_state.is_enabled(state),
        "supported": supported,
        "platform": platform_id,
        "reason": reason,
        "max_tree_nodes": limits.max_tree_nodes,
        "max_tree_depth": limits.max_tree_depth,
        "text_limit": limits.text_limit,
        "attach_screenshot": limits.attach_screenshot,
        "screenshot_max_px": limits.screenshot_max_px,
        "screenshot_jpeg_quality": limits.screenshot_jpeg_quality,
        # Cursor Motion: purely visual, macOS only, and reported with
        # ``cursor_motion_supported`` so the panel can hide the row rather than
        # offer a toggle that draws nothing on this platform.
        "cursor_motion": limits.cursor_motion,
        "cursor_motion_supported": platform_id == PLATFORM_MACOS,
        "allowed_apps": list(policy_cfg.allowed_apps),
        "extra_denied_apps": list(policy_cfg.extra_denied_apps),
        # Non-empty ONLY when the keystone's policy could not be parsed. The lists
        # above are then empty because they could not be read — not because the
        # operator set no restriction — and the panel must be able to tell the two
        # apart rather than presenting a confidently empty allow-list.
        "policy_error": policy_error,
        # ``read_only`` is what the panel keys every control's ``disabled`` on, and
        # it is deliberately ``forbidden``-only rather than ``locked``. A NARROWED
        # ceiling still PERMITS the capability — it just bounds what the automation
        # may do — so the user must still be able to opt in; the PUT would succeed
        # and locking the toggle would take the choice away for no reason. Only a
        # ceiling that forbids the capability outright (or one we could not
        # evaluate, which the summary reports as ``forbidden``) disables the
        # controls, and then the PUT's own 409 is the authoritative refusal.
        #
        # Derived from GOVERNANCE, never from the platform: an unsupported platform
        # renders a reason instead of controls, which is a separate branch. Keeping
        # the two apart means a governed macOS host and an ungoverned Linux host can
        # never be confused for one another.
        # Ceilings, so the panel's number inputs bound themselves from the server
        # rather than re-spelling the limits in TypeScript.
        "limits": {key: [low, high] for key, (low, high) in _INT_LIMITS.items()},
    }


# ── Writes ──


def _coerce_app_patterns(raw: object) -> "list[str] | None":
    """Validate an operator-supplied app pattern list, or ``None`` when invalid.

    Patterns are lowercased and de-duplicated (the policy matcher casefolds
    anyway, so storing mixed case would only make the file's meaning less
    obvious). A non-list, an over-long list, a non-string entry or an over-long
    entry is REJECTED rather than silently filtered: this list narrows a security
    decision, and a request whose meaning we had to guess at should fail loudly.
    """
    if not isinstance(raw, list) or len(raw) > MAX_APP_PATTERNS:
        return None
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            return None
        cleaned = entry.strip().lower()
        if len(cleaned) > MAX_APP_PATTERN_LEN:
            return None
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _read_config_strict() -> dict:
    """Read ``config.json`` for a MUTATION: raise on corrupt, ``{}`` when absent.

    Factored out of :func:`_write_limits` so the preflight and the write share ONE
    definition of "usable". A second, hand-copied validation would be free to drift
    and would then either block a good save or let a bad one through.
    """
    from kiro_crew.config.loader import config_path

    path: Path = config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateCorruptError(f"config.json is not readable as a JSON object: {exc}") from exc
    if not isinstance(data, dict):
        raise StateCorruptError("config.json top level is not a JSON object")
    return data


def _assert_writable() -> None:
    """Raise ``StateCorruptError`` if either settings file is unusable. Synchronous.

    A read-only precheck used when one request mutates BOTH the keystone and
    ``config.json``: it reproduces exactly the validation the two writers perform,
    so the pair either both proceed or neither does. Called with the config lock
    held, so nothing can corrupt a file between this check and the writes.

    It is not a transaction — two separate files cannot be updated atomically
    without a rewrite of both writers — but it removes the only failure the writes
    realistically have, which is a file that cannot be parsed.
    """
    _read_state_strict()
    _read_config_strict()


def _write_state(patch: dict[str, Any]) -> None:
    """Read-modify-write the keystone atomically, owner-only. Synchronous.

    Raises :class:`StateCorruptError` rather than clobbering a
    populated-but-unparseable ceiling file — resetting an operator's allow-list to
    defaults because we could not parse it would be a silent security downgrade.
    """
    state = _read_state_strict()
    state.update(patch)
    enable_state.save_state(state)


def _write_limits(patch: dict[str, Any]) -> None:
    """Merge *patch* into ``config.json``'s ``computer_use`` section. Synchronous.

    Read-modify-write on the raw JSON (not ``KiroCrewConfig.to_dict()``) so an
    unrelated hand-edited or edition-contributed section is never rewritten by a
    limits change. The caller holds the shared config lock.
    """
    from kiro_crew.agent import _atomic_json_write
    from kiro_crew.config.loader import config_path

    path: Path = config_path()
    data = _read_config_strict()
    section = data.get("computer_use")
    if not isinstance(section, dict):
        section = {}
    section.update(patch)
    data["computer_use"] = section
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json_write(path, data)


async def _full_payload() -> dict[str, Any]:
    """The GET payload: the offloaded snapshot plus the out-of-process probe."""
    payload = await _off_loop(_snapshot)
    payload["permissions"] = await _probe_permissions(str(payload.get("platform", "")))
    return payload


# ── GET ──


async def api_computer_use_config_get(request: web.Request) -> web.Response:
    """GET /api/computer-use/config — full Settings snapshot (read, no audit)."""
    return web.json_response(await _full_payload())


# ── PUT ──


async def api_computer_use_config_save(request: web.Request) -> web.Response:
    """PUT /api/computer-use/config — write the enable (keystone) + limits (config).

    Accepts any subset of ``{enabled, allowed_apps, extra_denied_apps}`` (keystone)
    and ``{max_tree_nodes, max_tree_depth, text_limit, attach_screenshot,
    screenshot_max_px, screenshot_jpeg_quality}`` (``config.json``). Returns the
    refreshed GET payload so the panel re-renders from server truth rather than
    from its own optimistic guess.

    Refuses with **409** when the governance ceiling forbids computer use and the
    request would WIDEN the surface — matching ``api_denied_command_builtin_toggle``'s
    precedent: the request conflicts with immutable server state (the ceiling), it
    is not an auth failure of the caller. Turning the feature OFF, and shrinking a
    budget, are always allowed: narrowing can never conflict with a ceiling, and a
    user must never be locked out of switching this off.
    """
    from kiro_crew.dashboard.handlers.agents import _get_config_lock

    try:
        body = await request.json()
    except Exception:
        _audit(request, operation=OP_CONFIG_SAVE, outcome="denied", resources="invalid_json")
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        _audit(request, operation=OP_CONFIG_SAVE, outcome="denied", resources="body_not_object")
        return web.json_response({"error": "request body must be a JSON object"}, status=400)

    # ── Validate EVERYTHING before writing anything ──
    state_patch: dict[str, Any] = {}
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            _audit(
                request, operation=OP_CONFIG_SAVE, outcome="denied", resources="enabled=bad_type"
            )
            return web.json_response({"error": "enabled must be a boolean"}, status=400)
        state_patch[STATE_KEY_ENABLED] = body["enabled"]
    for key in _APP_LIST_KEYS:
        if key not in body:
            continue
        patterns = _coerce_app_patterns(body[key])
        if patterns is None:
            _audit(request, operation=OP_CONFIG_SAVE, outcome="denied", resources=f"{key}=invalid")
            return web.json_response(
                {
                    "error": (
                        f"{key} must be a list of at most {MAX_APP_PATTERNS} strings, "
                        f"each at most {MAX_APP_PATTERN_LEN} characters"
                    )
                },
                status=400,
            )
        state_patch[key] = patterns

    limits_patch: dict[str, Any] = {}
    for key, (low, high) in _INT_LIMITS.items():
        if key not in body:
            continue
        value = body[key]
        # ``isinstance(True, int)`` is True in Python, so bools are rejected
        # explicitly — a JSON ``true`` must not become the integer 1 for a budget.
        if isinstance(value, bool) or not isinstance(value, int):
            _audit(request, operation=OP_CONFIG_SAVE, outcome="denied", resources=f"{key}=bad_type")
            return web.json_response({"error": f"{key} must be an integer"}, status=400)
        if value < low or value > high:
            _audit(request, operation=OP_CONFIG_SAVE, outcome="denied", resources=f"{key}={value}")
            return web.json_response(
                {"error": f"{key} must be between {low} and {high}"}, status=400
            )
        limits_patch[key] = value
    for key in _BOOL_KEYS:
        if key not in body:
            continue
        if not isinstance(body[key], bool):
            _audit(request, operation=OP_CONFIG_SAVE, outcome="denied", resources=f"{key}=bad_type")
            return web.json_response({"error": f"{key} must be a boolean"}, status=400)
        limits_patch[key] = body[key]

    if not state_patch and not limits_patch:
        _audit(request, operation=OP_CONFIG_SAVE, outcome="denied", resources="empty_patch")
        return web.json_response({"error": "no known computer-use fields in body"}, status=400)

    # ── Governance ──
    # Only a request that could WIDEN the surface is gated: enabling the feature,
    # or editing the operator's own target lists (which a ceiling may pin).

    # ── Write ──
    # Read the CURRENT enable before mutating, so the session reset below fires only
    # on a real transition. Inside the lock, so it cannot race the write it guards.
    async with _get_config_lock():
        enabled_before = await _off_loop(enable_state.is_enabled)
        try:
            # PREFLIGHT both targets before mutating either. Both writers reject
            # the same way on an unreadable/non-object file, so asking them first
            # makes the pair all-or-nothing against the failure they realistically
            # have, which is a file that cannot be parsed.
            #
            # Deliberately NOT "reject requests that mix the two": the Settings
            # panel legitimately saves both at once, so refusing the shape would
            # break the normal path to fix an error path.
            if state_patch and limits_patch:
                await _off_loop(_assert_writable)
            # ORDER MATTERS, and it is the security-relevant half of this block.
            # The preflight cannot rule out a write-time ``OSError`` (a full disk,
            # a mode change between the check and the write), so one of the two
            # files can still fail after the other landed. Two files cannot be
            # updated atomically here, but the *direction* of a partial apply is
            # ours to choose — so the keystone goes LAST.
            #
            # Limits are budget knobs (tree size, JPEG quality); the keystone is
            # the ceiling (feature enable, real-pointer opt-in, app allow/block
            # lists). With this order a 500 can never mean "we told the operator
            # the save failed while the ceiling actually moved": the only state
            # reachable after a failure is limits-applied/ceiling-untouched. The
            # reverse order made the security half the one that could survive a
            # reported failure.
            if limits_patch:
                await _off_loop(lambda: _write_limits(limits_patch))
            if state_patch:
                await _off_loop(lambda: _write_state(state_patch))
        except StateCorruptError as exc:
            _audit(
                request,
                operation=OP_CONFIG_SAVE,
                outcome="denied",
                resources="state_corrupt",
                error=str(exc),
            )
            logger.error("refusing computer-use mutation: %s", exc)
            return web.json_response({"error": ERR_STATE_CORRUPT}, status=500)
        except OSError as exc:
            _audit(
                request,
                operation=OP_CONFIG_SAVE,
                outcome="error",
                resources="write_failed",
                error=str(exc),
            )
            logger.error("computer-use settings write failed: %s", exc)
            return web.json_response({"error": "failed to write computer-use settings"}, status=500)

    # Audit the DECISION, not the payload: the enable is the security-relevant
    # bit, and the app patterns can name applications the operator would rather
    # not have echoed into an audit line beyond their count.
    fields = ",".join(sorted({*state_patch, *limits_patch}))
    resources = f"fields={fields}"
    if STATE_KEY_ENABLED in state_patch:
        resources = f"enabled={state_patch[STATE_KEY_ENABLED]} {resources}"
    _audit(request, operation=OP_CONFIG_SAVE, outcome="ok", resources=resources)

    # Flipping the ENABLE changes the shim's tool surface, and kiro-cli caches
    # ``tools/list`` for the LIFETIME of a session — ACP has no
    # ``tools/list_changed`` notification to push the new set. Without this, a user
    # who enables computer use sees "0 tools" in the chat they are sitting in and
    # concludes the feature is broken; the tools only appear in some later session.
    # So reset sessions exactly the way ``POST /api/mcp/sync`` does when MCP
    # routing changes — same primitive, same reason.
    #
    # Only on the ENABLE key, and only when it actually changed: the budget knobs
    # are read per call and need no restart, and re-saving the same value must not
    # tear down the user's session.
    sessions_reset = 0
    if STATE_KEY_ENABLED in state_patch and state_patch[STATE_KEY_ENABLED] != enabled_before:
        from kiro_crew.dashboard.handlers.sessions import _reset_all_sessions

        try:
            sessions_reset = await _reset_all_sessions(request)
        except Exception:
            # The write already landed and was audited; a restart failure must not
            # report the SAVE as failed. Worst case is the pre-existing behaviour:
            # the new tool surface appears on the next cold session.
            logger.exception("computer-use enable saved, but session reset failed")

    payload = await _full_payload()
    payload["sessions_reset"] = sessions_reset
    return web.json_response(payload)


# ── POST /api/computer-use/invoke — the thin shim's loopback leg ──


# NOTE: ``_plane_a_can_prompt`` / ``_cu_titles_are_prompt_eligible`` USED to live
# here, inferring from ``~/.kiro/agents/*.json`` and ``hooks.auto_approve_tools``
# whether the PreToolUse gate would still prompt, and passing the answer as
# ``approval_recorded``. Both are deleted: the inference was a read of mutable state
# at the wrong time (kiro-cli loads its agent config at session start, so a grant
# removed from disk afterwards inverted the answer in the granting direction), and
# an ``interactive`` floor is satisfiable only by per-call proof. Do not reintroduce
# a variant — see the ``approval_recorded=False`` comment in
# :func:`api_computer_use_invoke`.


async def api_computer_use_invoke(request: web.Request) -> web.Response:
    """POST /api/computer-use/invoke — run one computer-use tool IN THE GATEWAY.

    MACHINE endpoint: the ``kirocrew-computer`` stdio MCP shim is the only caller,
    authenticated by loopback + ``X-Internal-Secret``. No browser calls it.

    The middleware listing (``server._STRICT_INTERNAL_API_PATHS``) is NOT sufficient
    on its own, so this handler re-asserts the machine grant itself. Being on the
    strict list does not mean "secret required": when the ``X-Internal-Secret``
    header is ABSENT, ``token_auth_middleware`` deliberately falls through to
    normal cookie auth so dashboard pages can call internal routes — and on a
    ``local_only=False`` deployment it reclassifies every strict path as "mixed"
    outright. Either way a caller holding only a valid dashboard cookie or an
    app-scoped token would reach this handler AND choose its own ``session_key``,
    which selects the governance profile — e.g. claiming ``dashboard:main`` to
    escape a ``cu-off-subagent`` profile. Requiring ``request["internal_auth"]``
    (set by the middleware ONLY after a constant-time ``X-Internal-Secret`` match)
    closes the cookie, app-token and non-local_only variants in one place,
    independent of how the route happens to be classified.

    Body: ``{"tool": "computer_click", "args": {...}, "session_key": "…",
    "agent": "…", "app": "…"}``. The reply is ``{"text": "…"}`` — the exact string
    the shim relays to the model, already gated, already shaped by the observation
    ceiling, already redacted. Failures are returned as that same ``Error: …``
    text with a 200 rather than a 5xx, because the shim's job is to relay a TOOL
    RESULT: a non-200 surfaces as a transport failure the model cannot reason
    about, whereas ``Error: …`` is what ``mcp_shared.call_tool_with_logging``
    classifies as a failed SEL outcome. Malformed requests (not from the real
    shim) still get 4xx.

    The identity fields are not an authorization claim this handler trusts: the
    ``session_key`` is resolved STRICTLY on the shim side (``KIROCREW_SESSION_KEY``,
    else ``KIROCREW_HOST_PID`` + the HMAC sidecar), which refuses an unresolvable
    key before it reaches the wire. Passing them in the body is how the gateway
    learns which surface is calling — it is the AUDIT identity, not a permit; the
    trust comes from the local-secret handshake plus that strict resolution.

    ``approval_recorded`` is passed as ``False`` and no longer changes any outcome:
    the interactive-approval ceiling it used to satisfy was removed with the rest of
    the governance model. It is still not minted from the request body, because a
    body field would be a claim the shim issues to itself.

    The dispatch is offloaded: accessibility and capture calls block for tens of
    milliseconds each and must not land on the event loop.
    """
    # Imported lazily, not at module scope: the dispatcher pulls in the whole
    # computer-use service and (on macOS) the native driver, and this handler
    # module is imported by the handlers package at gateway boot. Keeping the boot
    # path free of that graph matters for a feature that is off by default.
    from kiro_crew.computer_use.tools import dispatch_tool

    # AUTHORIZATION, before anything else is parsed: only the loopback machine
    # caller that presented the local secret may dispatch. ``internal_auth`` is
    # set by token_auth_middleware exclusively on a verified X-Internal-Secret
    # match; a cookie- or app-token-authenticated request never carries it. See the
    # docstring for why the strict-path listing alone does not guarantee this.
    if request.get("internal_auth") is not True:
        _audit(
            request,
            operation="computer_use_invoke",
            outcome="denied",
            resources=request.path,
            error="internal secret required",
        )
        return web.json_response({"error": "forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "request body must be a JSON object"}, status=400)

    tool = body.get("tool")
    if not isinstance(tool, str) or not tool:
        return web.json_response({"error": "tool must be a non-empty string"}, status=400)
    args = body.get("args") or {}
    if not isinstance(args, dict):
        return web.json_response({"error": "args must be a JSON object"}, status=400)
    session_key = body.get("session_key")
    agent = body.get("agent")
    app = body.get("app")

    resolved_session = session_key if isinstance(session_key, str) else ""
    resolved_agent = agent if isinstance(agent, str) else ""
    resolved_app = app if isinstance(app, str) else ""

    # Record THIS loop as the one Cursor Motion schedules its animation onto. Done
    # here, on the loop, rather than from a startup hook: the dispatch runs on a
    # worker thread where ``get_running_loop`` raises, and binding per request keeps
    # the reference from going stale across a gateway restart without adding
    # lifecycle wiring for a feature that ships off by default. A no-op unless the
    # operator enabled ``computer_use.cursor_motion`` on macOS.
    cursor_overlay.bind_gateway_loop()

    def _run() -> str:
        # ``frame_scope`` publishes the calling surface for the duration of this
        # one dispatch so the capture layer's live-view relay can be GOVERNED for
        # the same identity as the tool result. It is thread-local and restored on
        # exit, so a pooled worker never carries one surface's identity into the
        # next call — and a capture that happens with no scope (a CLI probe, or a
        # future caller that bypassed this handler) emits no frame at all.
        with frame_scope(session_key=resolved_session, agent=resolved_agent, app=resolved_app):
            return dispatch_tool(
                tool,
                args,
                session_key=resolved_session,
                agent=resolved_agent,
                app=resolved_app,
                # ALWAYS False, and deliberately so (reviewer finding, third round
                # on this line). No inference from this process can establish that a
                # human approved THIS call:
                #
                # * the internal secret proves kiro-cli is upstream, not that it
                #   prompted;
                # * inspecting ``allowedTools`` / ``autoApprove`` / the
                #   ``auto_approve_tools`` patterns is a read of MUTABLE state at the
                #   wrong time. kiro-cli loads its agent config at SESSION START, so a
                #   grant removed from disk afterwards leaves the live session
                #   auto-approving while a fresh read says a prompt would happen —
                #   exactly inverted, and the direction that grants.
                #
                # Nothing now READS this flag — the approval ceiling it fed was
                # removed with the rest of the governance model — so it costs no
                # behaviour. It stays False rather than being deleted because the
                # reasoning above is what any future per-call-approval feature has to
                # satisfy, and it would need a signed per-call token from kiro-cli
                # (an upstream protocol change), not another inference from disk.
                approval_recorded=False,
            )

    try:
        text = await _dispatch_off_loop(_run)
    except Exception as exc:
        # The dispatcher is contracted never to raise, so reaching here is a bug —
        # but it must not become a 5xx the shim cannot relay, and the reason must
        # not leak into the model's context beyond a generic failure.
        logger.exception("computer-use dispatch raised")
        _audit(
            request,
            operation=OP_INVOKE,
            outcome="error",
            resources=tool,
            error=type(exc).__name__,
        )
        return web.json_response({"text": ERR_DISPATCH_FAILED})
    return web.json_response({"text": text})


# ── POST /api/computer-use/frame — the live-view (PiP) frame ingress ──


async def api_computer_use_frame(request: web.Request) -> web.Response:
    """POST /api/computer-use/frame — rebroadcast one already-captured frame.

    MACHINE endpoint, LOOPBACK-gated exactly like ``api_browser_frame``: the only
    caller is this same gateway's capture thread (``computer_use/screencast.py``),
    and the body is a live view of the operator's own desktop, so an off-host POST
    is refused outright. The route is additionally in
    ``server._STRICT_INTERNAL_API_PATHS``, so the middleware requires the
    ``X-Internal-Secret``; the loopback check here is re-asserted rather than
    inherited, because a ``local_only=False`` deployment reclassifies strict paths
    as mixed and would otherwise admit a cookie-authenticated caller.

    **What this handler does NOT decide.** It is not the security boundary for the
    pixels. Every suppression is evaluated before the POST, next to the capture:

    * a snapshot whose window holds any secure (password) field is never mirrored
      — ``Snapshot.has_secure``, the driver's own predicate;
    * a withheld ``screenshot`` channel emits no frame — resolved through
      ``gate.permitted_observation_channels``, the same evaluator the tool path and
      the Settings snapshot use. It permits every channel today; the check is the
      seam, not a live restriction;
    * a capture with no published surface scope emits no frame, because a frame
      that cannot be attributed to a surface cannot be governed for one.

    Frames are the already-downscaled JPEGs the model itself received (1280px/q55
    by default); ``build_frame_payload`` refuses any other encoding, so a
    full-resolution PNG cannot travel this path.

    The rebroadcast is OWNER-only (``deliver_ws_owners``), not
    ``broadcast_ws``: an App Kit credential can open ``/api/ws`` and land in the
    all-clients set, and a live view of the operator's desktop must not cross that
    boundary. Awaited rather than fire-and-forget so the reply reflects delivery.
    """
    from kiro_crew.dashboard.origin import is_loopback

    if not is_loopback(request.remote or ""):
        _audit(
            request,
            operation=OP_FRAME,
            outcome="denied",
            resources="non-loopback",
            error="loopback only",
        )
        return web.json_response({"error": "loopback only"}, status=403)

    # And the machine grant, for exactly the reason ``api_computer_use_invoke``
    # re-asserts it: being listed in ``_STRICT_INTERNAL_API_PATHS`` does NOT prove
    # the secret was checked. With the header ABSENT the middleware deliberately
    # falls through to cookie auth, and on a ``local_only=False`` deployment it
    # reclassifies every strict path as "mixed" — either way a caller holding only
    # a dashboard cookie or an app-scoped token would reach this handler and could
    # inject arbitrary frames into every owner window's live view. Requiring
    # ``internal_auth`` (set only after a constant-time ``X-Internal-Secret``
    # match) closes all of those in one place.
    if request.get("internal_auth") is not True:
        _audit(
            request,
            operation=OP_FRAME,
            outcome="denied",
            resources=request.path,
            error="internal secret required",
        )
        return web.json_response({"error": "forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:
        _audit(request, operation=OP_FRAME, outcome="invalid_input", resources="invalid-json")
        return web.json_response({"error": "invalid JSON"}, status=400)

    payload = build_frame_payload(body if isinstance(body, dict) else {})
    if payload is None:
        _audit(request, operation=OP_FRAME, outcome="invalid_input", resources="no-frame-data")
        return web.json_response({"error": "no frame data"}, status=400)

    state = request.app["state"]
    delivered = await state.deliver_ws_owners(COMPUTER_USE_FRAME_EVENT, payload)
    # Audit the RELAY, never the pixels or the mirrored app: one line per frame
    # with the delivery count is what makes the egress path reviewable without the
    # audit log itself becoming a record of which applications were on screen.
    _audit(request, operation=OP_FRAME, outcome="ok", resources=f"delivered={delivered}")
    return web.json_response({"ok": True, "subscribers": delivered})


__all__ = [
    "StateCorruptError",
    "api_computer_use_config_get",
    "api_computer_use_config_save",
    "api_computer_use_frame",
    "api_computer_use_invoke",
]
