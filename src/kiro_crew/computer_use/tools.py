"""The ten computer-use tools and the single in-gateway dispatch chokepoint.

**Everything in this module runs in the GATEWAY process**, never in the stdio MCP
sidecar. :mod:`kiro_crew.mcp_computer` is a thin shim that resolves the caller's
identity strictly and forwards over loopback; all authorization, all accessibility
and capture work, and all audit happen here. That split is not stylistic:

* ``hooks._governance_denial`` — the PreToolUse gate — is fail-**OPEN** by
  deliberate repo policy, because it gates every tool call on every surface and a
  transient profile-store glitch must not wedge the product. Nothing in this
  module relies on it: it can be skipped entirely (a pre-authorized tool, a
  fail-open evaluation) without weakening anything below.
* The refusals that DO hold for computer use are enforced HERE, in band, on the
  dispatch path every caller goes through — the keystone enable, the
  KiroCrew's-own-window denylist, the secure-field and sensitive-text checks. They
  only work where the OS-resolved app identity and the addressed element's role are
  known, i.e. in this process.

**Ordering inside :func:`_dispatch` is a security property.** Read it as:

1. the keystone primary enable (a refusal that can only deny, and the cheapest —
   a disabled feature must not even enumerate the operator's windows);
2. OS identity resolution — the on-screen window list ONLY. The audit record's
   ``app_bundle_id`` / ``app_display_name`` MUST be what the driver resolved, never
   the agent's ``app`` argument, so this precedes step 5. No accessibility tree is
   walked and no pixels are captured in this step;
3. the cached element lookup — an in-memory read of a tree the model was already
   shown. Its own refusals ("call computer_get_state first") are DEFERRED until
   after step 5 so nothing is disclosed before the target policy has run;
3d. the pointer-request shape — one-of(``element_index`` | ``x``+``y``), the
   method/target match and the button enum, resolved into a frozen request whose
   ``click_method`` is CONCRETE. ``auto`` is resolved HERE, upstream of the driver,
   and **never** onto the pointer-moving method: the model must NAME ``global`` for
   the operator's cursor to move at all;
4. ``gate.require_computer_use`` — now audit-only, and unconditionally permits.
   Retained as the one place a future edition can reintroduce a decision without
   touching every call site; see :mod:`gate`;
4b. ``gate.require_pointer_move`` — reached only when the resolved method actually
   warps the operator's physical cursor. Also permits by default; an
   ``accessibility`` or ``app_post`` click never reaches it at all;
5. **the always-on target policy (``policy.check_app``) — the real refusal.** The
   built-in denylist floor, which an operator's allow-list can narrow but never
   widen. This is what stops the agent driving KiroCrew's own window;
6. the deferred snapshot-freshness refusal (TTL / missing state);
7. fingerprint drift verification against a fresh walk;
8. the input-target policy (secure-field refusal + the sensitive-text scan);
9. the driver call;
10. the post-action re-walk;
11. ``gate.apply_observation_ceiling`` + ``policy.redact_result`` on the way out.

**Step 11 applies to REFUSALS too, not only to results.** A refusal is prose about
the operator's desktop: a fingerprint-drift message names the element that moved
(its verbatim accessibility title), a driver failure names the action that failed,
a denied-app refusal names the resolved bundle id. Surfacing any of those verbatim
would make the one path that skips the egress pass the easiest way to read a token
out of a status bar. :func:`_refusal` is therefore the single exit for every
model-facing refusal string, and it applies the SAME two controls in the same
order. Refusals that are 100% this package's own static prose (the primary-enable
refusal, the generic governance denials, the "pass an element_index" hint) are
excluded by construction — they carry no desktop text, and running them through
redaction could only mangle the instruction the model needs.

**No blocking call touches the event loop.** :func:`dispatch_tool` is deliberately
SYNCHRONOUS and blocking (accessibility round-trips to another process, an
in-process JPEG encode, one keystone file read), and it is the caller's job to keep
it off the loop:

* the gateway handler (``dashboard/handlers/computer_use.py``) offloads it once per
  request, so one tool call costs exactly ONE thread hop rather than one per
  native step;
* an async caller with no offload machinery of its own uses :func:`dispatch`,
  which offloads onto ``subprocess_executor()`` — the same bounded pool the repo
  reserves for calls that can block on a wedged kernel resource, which is exactly
  what a hung target application is.

Verified safe under concurrency: four simultaneous accessibility walks on worker
threads kept the loop responsive for 40/40 heartbeat ticks, because ctypes
releases the GIL around the C call.

Every result is TEXT ONLY. ``validation.build_tool_response`` is the transport's
single exit and cannot express an image content block, so "tree first, relay the
screenshot as a path" is a property of the transport rather than a policy someone
can regress.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import time
from dataclasses import replace
from typing import Any, Mapping, Sequence

from kiro_crew.computer_use import enable_state, gate, keymap, overlay, policy, render, service
from kiro_crew.computer_use.types import (
    CLICK_METHOD_ACCESSIBILITY,
    CLICK_METHOD_APP_POST,
    CLICK_METHOD_AUTO,
    CLICK_METHODS,
    DEFAULT_CLICK_COUNT,
    DEFAULT_CLICK_METHOD,
    DEFAULT_MOUSE_BUTTON,
    DEFAULT_SCROLL_PAGES,
    ERR_UNKNOWN_CLICK_METHOD,
    ERR_UNKNOWN_KEY,
    ERROR_PREFIX,
    GOVERNED_VALUE_PLACEHOLDER,
    OBS_A11Y_TREE,
    OBS_ELEMENT_VALUES,
    OBS_SCREENSHOT,
    OBS_SUPPRESSED_BY_POLICY,
    OBS_SUPPRESSED_KEY,
    OBS_SUPPRESSED_NOTE,
    OBS_WINDOW_TITLES,
    POST_ACTION_SETTLE_SECS,
    REFUSAL_DISABLED,
    TOOL_CLICK,
    TOOL_DRAG,
    TOOL_END_TURN,
    TOOL_GET_STATE,
    TOOL_LIST_APPS,
    TOOL_PERFORM_ACTION,
    TOOL_PRESS_KEY,
    TOOL_SCROLL,
    TOOL_SET_VALUE,
    TOOL_TYPE_TEXT,
    AppRef,
    ClickRequest,
    ComputerUseError,
    DragRequest,
    ElementRec,
    KeyParseError,
    PolicyConfig,
    PolicyStateError,
    Snapshot,
    SnapshotRequest,
    StaleIndex,
)
from kiro_crew.executors import subprocess_executor
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.sel import sel
from kiro_crew.validation import MCP_COMPUTER_SCHEMAS, ValidationError, validate_tool_args

logger = logging.getLogger(__name__)

# ── Argument names (the MCP wire vocabulary) ──
# Shared with ``mcp_computer._list_tools`` and ``validation.MCP_COMPUTER_SCHEMAS``;
# a drifted spelling here would be rejected by the schema as an unknown field
# rather than silently ignored, which is why the schemas are mandatory.
ARG_APP = "app"
ARG_ELEMENT_INDEX = "element_index"
ARG_TEXT = "text"
ARG_KEY = "key"
ARG_VALUE = "value"
ARG_ACTION = "action"
ARG_DIRECTION = "direction"
ARG_PAGES = "pages"
ARG_SCREENSHOT = "screenshot"
ARG_TEXT_LIMIT = "text_limit"
ARG_MAX_TREE_NODES = "max_tree_nodes"
ARG_MAX_TREE_DEPTH = "max_tree_depth"
ARG_X = "x"
ARG_Y = "y"
ARG_CLICK_COUNT = "click_count"
ARG_MOUSE_BUTTON = "mouse_button"
ARG_CLICK_METHOD = "click_method"
ARG_FROM_X = "from_x"
ARG_FROM_Y = "from_y"
ARG_TO_X = "to_x"
ARG_TO_Y = "to_y"

# ── Result prose ──
END_TURN_TEXT = "Released every cached window state. Call {tool} again before acting."
ACTION_RESULT_HEADER = "{detail}\n\nRefreshed state:"
ERR_UNKNOWN_TOOL = "unknown computer-use tool '{tool}'."
ERR_INTERNAL = "computer use failed unexpectedly; see the gateway log for details."

# Quoted runs inside a refusal sentence. EVERY place this package interpolates
# desktop-derived text into a refusal wraps it in quotes — ``render.describe_record``
# uses ``"``, the drivers' app/action details use ``'`` — so this is what
# :func:`_strip_desktop_detail` removes when the ``element_values`` channel is
# denied. Non-greedy, and both quote styles, so a sentence carrying two quoted
# fragments (a drift message names the old AND the new element) loses both.
_QUOTED_RUN_RE = re.compile(r"\"[^\"]*\"|'[^']*'")

# Tools that address one application and therefore need a resolved OS identity
# before the gate can bound them. ``computer_list_apps`` enumerates every app (it
# has no single target) and ``computer_end_turn`` touches KiroCrew's own cache, so
# both are queried with ``requires_app_identity=False``.
_APP_SCOPED_TOOLS: frozenset[str] = frozenset(
    {
        TOOL_GET_STATE,
        TOOL_CLICK,
        TOOL_DRAG,
        TOOL_TYPE_TEXT,
        TOOL_PRESS_KEY,
        TOOL_SET_VALUE,
        TOOL_SCROLL,
        TOOL_PERFORM_ACTION,
    }
)

# Tools that address ONE element by index, and therefore have a resolved
# ``ElementRec`` for ``policy.check_input_target`` to apply the secure-field
# refusal to. ``computer_click`` is here even though its index is OPTIONAL: when
# one is supplied the resolved element is checked, and when it is not the target
# is a raw coordinate instead (the one-of enforced by
# ``policy.check_click_target``). Every OTHER member requires its index — see
# ``_ELEMENT_REQUIRED_TOOLS`` below — so there is always an element to check.
# ``computer_drag`` is NOT here — it has no element form at all.
_ELEMENT_SCOPED_TOOLS: frozenset[str] = frozenset(
    {
        TOOL_CLICK,
        TOOL_TYPE_TEXT,
        TOOL_PRESS_KEY,
        TOOL_SET_VALUE,
        TOOL_SCROLL,
        TOOL_PERFORM_ACTION,
    }
)
# Element index REQUIRED. Every element-scoped tool now requires it, keyboard tools
# included: an unnamed target has no role or subrole, so the always-on secure-field
# refusal in ``policy.check_input_target`` cannot inspect it and an indexless call
# would type into a focused password box on any ungoverned host. (``computer_click``
# is the one exception — it accepts EITHER an index or x/y coordinates, enforced as
# a one-of by ``policy.check_click_target``.)
_ELEMENT_REQUIRED_TOOLS: frozenset[str] = frozenset(
    {
        TOOL_TYPE_TEXT,
        TOOL_PRESS_KEY,
        TOOL_SET_VALUE,
        TOOL_SCROLL,
        TOOL_PERFORM_ACTION,
    }
)
# Tools that synthesize KEYSTROKES. Both are element-targeted, and both are refused
# outright without an index (see 3b in ``_dispatch``) — including
# ``computer_press_key``, because ``press_key('tab')`` can MOVE focus onto a
# password box and the next keystroke would land there.
_KEYBOARD_TOOLS: frozenset[str] = frozenset({TOOL_TYPE_TEXT, TOOL_PRESS_KEY})


def dispatch_tool(
    tool_name: str,
    args: Mapping[str, Any],
    *,
    session_key: str,
    agent: str = "",
    app: str = "",
    approval_recorded: bool = False,
) -> str:
    """Run one computer-use tool and return its final, already-shaped text.

    **BLOCKING.** The single entry point the gateway handler calls, from a worker
    thread. Returns prose on success and an ``"Error: ..."`` string on every
    refusal or failure — the prefix is load-bearing, not cosmetic:
    ``mcp_shared.call_tool_with_logging`` classifies it as a failed SEL outcome, so
    a refusal that omitted it would be audited as a success.

    *session_key* is the identity the shim resolved with
    ``mcp_core._resolve_session_key_strict`` (env var, or ``KIROCREW_HOST_PID``
    plus the HMAC sidecar signed with the keystone-protected ``sel_hmac.key``). It
    is used for the AUDIT RECORD, not for authorization: an empty value no longer
    refuses, because the unattended-surface rule is gone and a cron job driving the
    desktop is a supported flow. It is still never inferred here — the lenient
    resolver walks a file mcp_core itself documents as "agent-writable and therefore
    forgeable", so a guess would put a forgeable identity in the audit trail.

    *approval_recorded* is accepted and ignored. The interactive-approval floor was
    removed with the rest of the governance model; the parameter is kept so the
    gateway handler and the tests keep one signature.

    Contracted NEVER TO RAISE except for ``PlatformCompositionError``, which
    PROPAGATES: a host that cannot compose its platform context must not have a
    computer-use call quietly degrade to a text refusal. Every other exception
    becomes a generic error string, so a driver or rendering bug cannot escape into
    the caller's request handler.
    """
    try:
        return _dispatch(
            tool_name,
            args,
            session_key=session_key,
            agent=agent,
            app=app,
            approval_recorded=approval_recorded,
        )
    except PlatformCompositionError:
        raise
    except ValidationError as exc:
        return _refusal(
            str(exc), session_key=session_key, agent=agent, app=app, tool_name=tool_name
        )
    except (StaleIndex, KeyParseError, PolicyStateError, ComputerUseError) as exc:
        # The package's own typed failures are already model-facing PROSE (a stale
        # index names the age, a drift names both identities) — but prose ABOUT the
        # desktop, so it goes through the same exit as every rendered result rather
        # than being surfaced verbatim. See :func:`_refusal`.
        return _refusal(
            str(exc), session_key=session_key, agent=agent, app=app, tool_name=tool_name
        )
    except Exception:
        # Never leak an internal traceback or an arbitrary exception string to
        # the model: it can carry filesystem paths and, from a driver, raw
        # accessibility values.
        logger.exception("computer-use tool %s failed", tool_name)
        return _refusal(
            ERR_INTERNAL, session_key=session_key, agent=agent, app=app, tool_name=tool_name
        )


async def dispatch(
    tool_name: str,
    args: Mapping[str, Any],
    *,
    session_key: str,
    agent: str = "",
    app: str = "",
    approval_recorded: bool = False,
) -> str:
    """Async wrapper: :func:`dispatch_tool` offloaded off the event loop.

    For an async caller with no offload machinery of its own (an app backend, a
    future in-process surface). The gateway handler does NOT use this — it owns its
    own executor hop, so routing through here would just add a second one.

    ``subprocess_executor`` rather than the default executor: it is the bounded
    pool the repo reserves for calls that can block on a wedged kernel resource,
    and a hung target application parking a worker for the driver's whole messaging
    timeout is exactly that failure mode. Sharing the default pool would let one
    wedged app starve unrelated maintenance work.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        subprocess_executor(),
        functools.partial(
            dispatch_tool,
            tool_name,
            args,
            session_key=session_key,
            agent=agent,
            app=app,
            approval_recorded=approval_recorded,
        ),
    )


def _dispatch(
    tool_name: str,
    args: Mapping[str, Any],
    *,
    session_key: str,
    agent: str,
    app: str,
    approval_recorded: bool,
) -> str:
    """The ordered chokepoint. See the module docstring for why the order is fixed."""
    if tool_name not in MCP_COMPUTER_SCHEMAS:
        # Refused before anything else: an unregistered tool would pass RAW
        # through validation, and a ValidationError raised deeper inside a
        # handler would escape the stdio loop and kill the server.
        return _static_refusal(
            ERR_UNKNOWN_TOOL.format(tool=tool_name),
            session_key=session_key,
            agent=agent,
            tool_name=tool_name,
        )
    clean = validate_tool_args(dict(args), MCP_COMPUTER_SCHEMAS[tool_name])

    svc = service.get_shared_service()

    # (1) PRIMARY ENABLE — the keystone. ONE read serves both the enable test and
    #     the operator's target policy, so a hand-edited file cannot be observed in
    #     two different states within a single dispatch.
    state = enable_state.load_state()
    if not enable_state.is_enabled(state):
        return _static_refusal(
            REFUSAL_DISABLED, session_key=session_key, agent=agent, tool_name=tool_name
        )
    cfg = enable_state.load_policy_config(state)

    # (2) OS IDENTITY RESOLUTION — window list only, no tree walk, no pixels.
    target: "AppRef | None" = None
    if tool_name in _APP_SCOPED_TOOLS:
        target = svc.resolve_app(str(clean.get(ARG_APP) or ""))

    # (3) CACHED ELEMENT LOOKUP — in-memory, so the gate can bound the addressed
    #     element's role and subrole. Its refusals are deferred (see below).
    rec: "ElementRec | None" = None
    deferred: "StaleIndex | None" = None
    if target is not None and tool_name in _ELEMENT_SCOPED_TOOLS:
        raw_index = clean.get(ARG_ELEMENT_INDEX)
        if isinstance(raw_index, int):
            try:
                rec = svc.element(svc.cached(target, session_key=session_key), raw_index)
            except StaleIndex as exc:
                # DEFERRED, not raised: "no state for 'Finder'" would confirm the
                # app exists and is reachable to a caller governance is about to
                # refuse. Authorization first, diagnostics second.
                deferred = exc
        elif tool_name in _ELEMENT_REQUIRED_TOOLS:
            # The LAST line of defence, and a reachable one — not merely a guard for
            # the in-process entry point. ``MCP_COMPUTER_SCHEMAS`` is the enforcement
            # layer for the MCP path, so this stays even where that schema already
            # demands the field: a spec that relaxed one of these back to optional
            # (as ``computer_type_text`` and ``computer_press_key`` once were) must
            # still be refused here rather than typing into an uninspectable target.
            raise ValidationError(ARG_ELEMENT_INDEX, "required")

    # (3d) POINTER-REQUEST SHAPE — one-of(element_index | x+y), the method/target
    #      match, and the button enum, all resolved into a frozen request whose
    #      ``method`` is CONCRETE. Resolving here rather than in the driver is what
    #      makes step (4b) possible at all: the pointer permit can only be demanded
    #      once ``auto`` has been turned into a real method, and doing that upstream
    #      of the gate means no backend can re-decide it afterwards.
    #      A shape refusal is about the CALLER'S OWN ARGUMENTS and discloses nothing
    #      about the desktop, which is why it may precede authorization (the schema
    #      validation at the top of this function already does).
    request: "ClickRequest | DragRequest | None" = None
    if tool_name == TOOL_CLICK:
        built = _build_click_request(clean)
        if isinstance(built, str):
            return _static_refusal(built, session_key=session_key, agent=agent, tool_name=tool_name)
        request = built
    elif tool_name == TOOL_DRAG:
        built_drag = _build_drag_request(clean)
        if isinstance(built_drag, str):
            return _static_refusal(
                built_drag, session_key=session_key, agent=agent, tool_name=tool_name
            )
        request = built_drag

    # (4) AUTHORIZATION — fail-closed, before any accessibility or capture call.
    denial = gate.require_computer_use(
        tool_name,
        session_key=session_key,
        agent=agent,
        app=app,
        app_bundle_id="" if target is None else target.bundle_id,
        app_display_name="" if target is None else target.name,
        observations=_declared_observations(tool_name),
        target_roles=() if rec is None else (rec.role, rec.subrole),
        requires_app_identity=tool_name in _APP_SCOPED_TOOLS,
        approval_recorded=approval_recorded,
    )
    if denial:
        # The gate already audited its own governance decision with the scope and
        # item; this records the tool-invocation outcome so the two views agree.
        return _static_refusal(denial, session_key=session_key, agent=agent, tool_name=tool_name)

    # (4b) THE REAL-POINTER PATH — reached only when the resolved method actually
    #      warps the operator's physical cursor. It no longer needs a permit of its
    #      own: one enable covers the feature. What still protects the cursor is
    #      upstream, in ``policy.resolve_click_method`` — ``auto`` NEVER resolves to
    #      ``global``, so the model has to NAME the pointer-moving method, and an
    #      ``accessibility`` or ``app_post`` click never reaches this branch. The
    #      call is kept for its AUDIT: ``audit_pointer_move`` records the one event
    #      an operator would most want to find afterwards.
    if request is not None and request.moves_pointer:
        pointer_denial = gate.require_pointer_move(
            tool_name,
            method=request.method,
            session_key=session_key,
            agent=agent,
            app=app,
        )
        if pointer_denial:
            return _static_refusal(
                pointer_denial, session_key=session_key, agent=agent, tool_name=tool_name
            )

    # (5) ALWAYS-ON TARGET POLICY — the built-in denylist floor plus the
    #     operator's additions. Runs AFTER governance so a governance denial is
    #     reported as such, and independently of it so the floor holds on an
    #     ungoverned host (where ``resolve(None, None, ...)`` permits everything).
    if target is not None:
        refusal = policy.check_app(target, cfg)
        if refusal:
            # Through ``_refusal``, not raw: this sentence names the RESOLVED
            # bundle id / display name, which is desktop-derived text like any
            # other. (The refusal's *reason* half is our own static prose.)
            return _refusal(
                refusal, session_key=session_key, agent=agent, app=app, tool_name=tool_name
            )

    # (6) DEFERRED SNAPSHOT-FRESHNESS REFUSAL — now that the call is authorized.
    if deferred is not None:
        raise deferred

    _audit_allowed(session_key, agent, tool_name, target)
    if request is not None and request.moves_pointer:
        # A SECOND audit record, naming the method, on the ALLOW path. The generic
        # invocation log above cannot answer "did the agent ever take control of my
        # mouse?" — a pointer-moving click is indistinguishable from an AXPress in
        # it — and that is the one question this path exists to keep answerable.
        gate.audit_pointer_move(
            tool_name,
            method=request.method,
            session_key=session_key,
            agent=agent,
            app_label="" if target is None else target.label,
        )
    return _run(tool_name, clean, svc, target, rec, cfg, session_key, agent, app, request)


def _run(
    tool_name: str,
    clean: Mapping[str, Any],
    svc: service.ComputerUseService,
    target: "AppRef | None",
    rec: "ElementRec | None",
    cfg: PolicyConfig,
    session_key: str,
    agent: str,
    app: str,
    request: "ClickRequest | DragRequest | None" = None,
) -> str:
    """Execute an authorized tool. Never reached before the gate has said yes."""
    if tool_name == TOOL_END_TURN:
        # Scoped to the calling session: a model ending ITS turn must not drop
        # another surface's live element indices.
        svc.end_turn(session_key=session_key)
        return END_TURN_TEXT.format(tool=TOOL_GET_STATE)

    if tool_name == TOOL_LIST_APPS:
        # Filtered through the SAME denylist every other verb passes. Listing is not
        # a harmless read: an ``AppRef`` carries the window TITLE, and a terminal can
        # be given an arbitrary title with an OSC escape (so can a browser tab, an
        # editor, a chat window), which is precisely the content the denylist exists
        # to keep out of model context. A denied app is omitted entirely rather than
        # shown with its title blanked — its mere presence is not what matters, and a
        # placeholder row would just invite the model to try it and get refused.
        # TWO filters, and only the first is now a policy decision.
        # ``policy.check_app`` applies the built-in denylist plus the operator's own
        # allow/deny lists, so an app the agent may not touch is not enumerated
        # either. ``gate.app_is_disclosable`` used to apply the per-app governance
        # axes; with those gone it only asks "does this window have an identity at
        # all", dropping rows there is nothing to show for. Both are kept in this
        # order so reintroducing a disclosure ceiling is a one-function change.
        visible = tuple(
            ref
            for ref in svc.list_apps()
            if policy.check_app(ref, cfg) is None
            and gate.app_is_disclosable(
                bundle_id=ref.bundle_id,
                display_name=ref.name,
                session_key=session_key,
                agent=agent,
                app=app,
            )
        )
        return _render_apps(visible, session_key=session_key, agent=agent, app=app)

    assert target is not None  # every remaining tool is app-scoped (step 2)

    if tool_name == TOOL_GET_STATE:
        req = service.snapshot_request(
            max_nodes=_opt_int(clean, ARG_MAX_TREE_NODES),
            max_depth=_opt_int(clean, ARG_MAX_TREE_DEPTH),
            text_limit=_opt_int(clean, ARG_TEXT_LIMIT),
            want_image=_opt_bool(clean, ARG_SCREENSHOT),
        )
        # Pixels a policy forbids are never CAPTURED, not merely stripped after
        # the fact — and the omission is announced, because a model that asked for
        # a screenshot and silently got none retries in a loop. The response
        # shaper below is still the enforcement point (it holds even if this
        # pre-check is bypassed); this is the "don't do the work" optimization.
        channels = gate.permitted_observation_channels(
            session_key=session_key, agent=agent, app=app
        )
        notes: list[str] = []
        if req.want_image and OBS_SCREENSHOT not in channels:
            req = replace(req, want_image=False)
            notes.append(OBS_SUPPRESSED_NOTE)
        return _render_snapshot(
            svc.snapshot(target, req, session_key=session_key),
            req,
            session_key=session_key,
            agent=agent,
            app=app,
            extra_notes=tuple(notes),
            screenshot_suppressed=bool(notes),
        )

    # ── Mutating verbs ──
    # A refreshed structural view follows every action, but WITHOUT pixels: a
    # mutator deliberately declares no observation channel at the gate (so a fleet
    # denying screenshots does not lose the ability to click), and capturing a
    # frame a policy might forbid — then discarding it — would be both slower and
    # a needless brush with the ceiling. A model that needs to see the result
    # calls ``computer_get_state``.
    # Inherit the TREE BUDGET the cached snapshot was walked at, because a mutating
    # tool takes no budget arguments of its own and the config default would silently
    # shrink the tree: a model shown element 1400 under ``max_tree_nodes=2001`` would
    # have its click refused by the drift check ("no element at that index") and would
    # then be handed a 1200-node refresh, so re-snapshotting reproduced the same
    # refusal forever. Only ``want_image`` is forced off (see below).
    req = replace(_mutation_walk_budget(svc, target, session_key=session_key), want_image=False)

    if rec is not None:
        # Fingerprint drift against a FRESH walk. Unconditional on every mutating
        # action: this is what turns element addressing from a hope into a check.
        svc.verify_fingerprint(target, rec, req, session_key=session_key)

    text = str(clean.get(ARG_TEXT) or "") if tool_name == TOOL_TYPE_TEXT else ""
    if tool_name == TOOL_SET_VALUE:
        text = str(clean.get(ARG_VALUE) or "")
    if tool_name in (TOOL_TYPE_TEXT, TOOL_SET_VALUE, TOOL_PRESS_KEY):
        # Input-target policy: the secure-field refusal (a macOS password box is
        # ``role='AXTextField'`` with ``subrole='AXSecureTextField'`` and a
        # READABLE value, so ``ElementRec.secure`` is the only reliable signal)
        # plus the sensitive-text scan.
        refusal = policy.check_input_target(target, rec, text, cfg)
        if refusal:
            # Through ``_refusal``: the secure-target sentence names the app and the
            # element's subrole, and the sensitive-text sentence quotes the deny
            # rule's reason, which can echo the path the agent tried to type.
            return _refusal(
                refusal, session_key=session_key, agent=agent, app=app, tool_name=tool_name
            )

    detail = _perform(tool_name, clean, svc, target, rec, text, request)
    # LET THE UI REPAINT before re-reading it. Without this the refresh walk can
    # observe the PRE-action tree, so the model is shown a result that looks like
    # the action did nothing and retries an action that already succeeded — a
    # double click, a doubled keystroke, a second row deleted. AppKit and Electron
    # both settle well inside this window; it is the same reason the reference
    # implementation sleeps between an action and its refresh.
    time.sleep(POST_ACTION_SETTLE_SECS)
    # The action changed the UI, so every cached index is now suspect. Re-walking
    # here (rather than invalidating and making the model call get_state again)
    # keeps the cache authoritative and gives the model the post-action tree in the
    # same turn.
    snap = svc.snapshot(target, req, session_key=session_key)
    body = _render_snapshot(snap, req, session_key=session_key, agent=agent, app=app)
    # ``detail`` is redacted SEPARATELY, and it has to be. The body was already
    # redacted inside ``render_tree``, but the header was concatenated after that
    # pass — and ``detail`` is not our prose: every driver confirmation interpolates
    # app-supplied text (``_click_text`` embeds ``app.name``, the process name macOS
    # reports), so a process named ``Notes key=AKIA…`` put a raw credential in front
    # of a fully redacted tree. Verified: the string survived end-to-end before this
    # line, and ``redact_credentials`` masks it.
    #
    # Redacting the two parts separately rather than the joined string is deliberate:
    # ``render_tree`` deliberately appends the screenshot note AFTER its own
    # redaction, because the per-user temp path contains a long random segment that
    # the bare-secret-key heuristic masks — re-running redaction over the joined text
    # would destroy every screenshot path (verified live, documented in
    # ``render._render_image_note``). So the header is redacted on its own and the
    # already-redacted body is left untouched.
    return f"{ACTION_RESULT_HEADER.format(detail=policy.redact_result(detail))}\n{body}"


def _perform(
    tool_name: str,
    clean: Mapping[str, Any],
    svc: service.ComputerUseService,
    target: AppRef,
    rec: "ElementRec | None",
    text: str,
    request: "ClickRequest | DragRequest | None" = None,
) -> str:
    """Call the driver for one mutating verb. Returns its confirmation text."""
    if tool_name == TOOL_CLICK:
        assert isinstance(request, ClickRequest)  # built in step 3d
        # Cursor Motion: draw the visible cursor gliding to the target just before a
        # REAL-POINTER click, so the operator watching the desktop can see what the
        # agent is about to do. Only for the pointer-moving method — the app-scoped
        # and accessibility paths never move the physical cursor, so animating one
        # there would show a gesture that is not happening. Off by default, macOS
        # only, non-blocking, and never a permit (both pointer permits were checked
        # upstream).
        if request.moves_pointer and request.point is not None:
            overlay.show_pointer_motion(request.point[0], request.point[1], request.count)
        return svc.click(target, rec, request)
    if tool_name == TOOL_DRAG:
        assert isinstance(request, DragRequest)  # built in step 3d
        # The drag's START point: the glide shows the pointer arriving where the
        # sweep begins. The sweep itself is drawn by the real cursor the driver is
        # about to move, so animating the end point too would double it.
        if request.moves_pointer:
            overlay.show_pointer_motion(request.start[0], request.start[1])
        return svc.drag(target, request)
    if tool_name == TOOL_TYPE_TEXT:
        return svc.type_text(target, rec, text)
    if tool_name == TOOL_SET_VALUE:
        assert rec is not None
        return svc.set_value(target, rec, text)
    if tool_name == TOOL_SCROLL:
        assert rec is not None
        direction = str(clean.get(ARG_DIRECTION) or "")
        raw_pages = clean.get(ARG_PAGES)
        pages = (
            float(raw_pages)
            if isinstance(raw_pages, (int, float)) and not isinstance(raw_pages, bool)
            else DEFAULT_SCROLL_PAGES
        )
        return svc.scroll(target, rec, direction, pages)
    if tool_name == TOOL_PERFORM_ACTION:
        assert rec is not None
        return svc.perform_action(target, rec, str(clean.get(ARG_ACTION) or ""))
    if tool_name == TOOL_PRESS_KEY:
        spec = str(clean.get(ARG_KEY) or "")
        # Parsed here, in the platform-free layer, so an unknown key is refused
        # BEFORE a keystroke is synthesized into a live window. ``keymap`` is the
        # package's single key vocabulary: a future driver with a different one
        # must EXTEND keymap rather than bypass this check, or an unsupported
        # spelling would reach a real application.
        try:
            keymap.parse_key(spec)
        except KeyParseError as exc:
            raise KeyParseError(f"{ERR_UNKNOWN_KEY.format(key=spec)} {exc}") from exc
        return svc.press_key(target, rec, spec)
    raise ComputerUseError(ERR_UNKNOWN_TOOL.format(tool=tool_name))


# ── Pointer-request construction ──


def _build_click_request(clean: Mapping[str, Any]) -> "ClickRequest | str":
    """Resolve a validated ``computer_click`` payload, or return a refusal string.

    Returns a :class:`ClickRequest` whose ``method`` is CONCRETE — ``auto`` is
    resolved here, upstream of both the gate and the driver, which is what lets the
    pointer permit be demanded for exactly the right calls and stops any backend
    re-deciding the method afterwards.

    A ``str`` return is a refusal (the caller prefixes it). Refusals are returned
    rather than raised so the argument-shape errors read as ordinary tool results;
    they name the caller's own arguments and disclose nothing about the desktop,
    which is why they are exempt from :func:`_refusal`'s egress pass.

    Order: one-of(target) first, then the method/target match, then the button.
    Target first because the method rules are stated in terms of which target was
    given, so reporting "accessibility needs an element_index" for a call that also
    passed coordinates would name the wrong problem.
    """
    element_index = _opt_int(clean, ARG_ELEMENT_INDEX)
    point = _opt_point(clean, ARG_X, ARG_Y)
    refusal = policy.check_click_target(element_index, point)
    if refusal:
        return refusal
    method = str(clean.get(ARG_CLICK_METHOD) or DEFAULT_CLICK_METHOD)
    refusal = policy.check_click_method(method, element_index=element_index, point=point)
    if refusal:
        return refusal
    button = str(clean.get(ARG_MOUSE_BUTTON) or DEFAULT_MOUSE_BUTTON)
    refusal = policy.check_mouse_button(button)
    if refusal:
        return refusal
    resolved = policy.resolve_click_method(method, element_index=element_index, point=point)
    # Checked on the RESOLVED method, not the requested one: ``auto`` carries no
    # button constraint of its own, and it never resolves to ``sky_click`` anyway —
    # but checking the request would let a future ``auto`` mapping slip a
    # right-button request onto a left-only recipe. Checked after resolution and
    # before the request is built, so no incompatible pair can reach a driver.
    refusal = policy.check_method_button(resolved, button)
    if refusal:
        return refusal
    count = _opt_int(clean, ARG_CLICK_COUNT)
    return ClickRequest(
        method=resolved,
        # Dropped for the accessibility path: an AXPress has no location, and
        # carrying a stale point would make the driver's "which form is this?" test
        # ambiguous. Only reachable when the caller gave an index, since
        # ``check_click_method`` refuses ``accessibility`` without one.
        point=None if resolved == CLICK_METHOD_ACCESSIBILITY else point,
        button=button,
        count=DEFAULT_CLICK_COUNT if count is None else count,
    )


def _build_drag_request(clean: Mapping[str, Any]) -> "DragRequest | str":
    """Resolve a validated ``computer_drag`` payload, or return a refusal string.

    All four coordinates are schema-required, so the only shapes left to police are
    the button and the method. ``auto`` resolves to ``app_post`` — the app-scoped
    mouse path — because a drag is coordinate-only and ``resolve_click_method``'s
    accessibility preference cannot apply; the same invariant holds as for a click:
    **``auto`` never resolves to the pointer-moving method.**
    """
    button = str(clean.get(ARG_MOUSE_BUTTON) or DEFAULT_MOUSE_BUTTON)
    refusal = policy.check_mouse_button(button)
    if refusal:
        return refusal
    start = _opt_point(clean, ARG_FROM_X, ARG_FROM_Y)
    end = _opt_point(clean, ARG_TO_X, ARG_TO_Y)
    if start is None or end is None:
        # Unreachable through the MCP path (all four are schema-required), but this
        # module is also the in-process entry point for an app backend.
        raise ValidationError(ARG_FROM_X, "from_x, from_y, to_x and to_y are all required")
    method = str(clean.get(ARG_CLICK_METHOD) or DEFAULT_CLICK_METHOD)
    if method not in CLICK_METHODS:
        return ERR_UNKNOWN_CLICK_METHOD.format(method=method)
    if method == CLICK_METHOD_ACCESSIBILITY:
        # Named explicitly rather than silently downgraded: no accessibility action
        # expresses a sweep between two points, so honouring the request is
        # impossible and substituting a different method would perform a gesture the
        # caller did not ask for.
        return ERR_UNKNOWN_CLICK_METHOD.format(method=method)
    resolved = CLICK_METHOD_APP_POST if method == CLICK_METHOD_AUTO else method
    return DragRequest(start=start, end=end, method=resolved, button=button)


def _opt_point(args: Mapping[str, Any], x_name: str, y_name: str) -> "tuple[float, float] | None":
    """A coordinate pair, or ``None`` when EITHER half is absent.

    Both-or-neither on purpose: a lone ``x`` is not a target, and treating it as
    ``(x, 0)`` would click the top edge of the screen. The caller then reports the
    missing half through the ordinary one-of / point-required refusals rather than
    acting on a coordinate the model never wrote.
    """
    raw_x = args.get(x_name)
    raw_y = args.get(y_name)
    # ``bool`` is excluded explicitly because it is an ``int`` subclass, so
    # ``x: true`` would otherwise become the coordinate 1. The schema already
    # rejects it on the MCP path; this is the belt for the in-process entry point.
    # Inlined rather than delegated to a predicate so mypy can narrow the types.
    if not isinstance(raw_x, (int, float)) or isinstance(raw_x, bool):
        return None
    if not isinstance(raw_y, (int, float)) or isinstance(raw_y, bool):
        return None
    return float(raw_x), float(raw_y)


# ── Response shaping ──


def _refusal(
    detail: str,
    *,
    session_key: str,
    agent: str,
    app: str,
    tool_name: str = "",
) -> str:
    """The single exit for a refusal whose text can quote the operator's desktop.

    Also the single place a PRE-GATE refusal is audited. The gate
    audits its own denials and ``_audit_allowed`` records permitted calls, which left
    a hole between them: a schema ``ValidationError``, an unknown tool, a bad
    ``click_method``, a stale index, an unparseable key or the paste refusal all
    return through here WITHOUT ever reaching the gate, so nothing was recorded. An
    audit trail with a gap at "malformed or refused attempts" is the wrong shape for
    this surface — a burst of them is exactly the signal an investigation wants.

    ``tool_name`` is optional only because a handful of call sites refuse before the
    tool name is known to be meaningful; when it is empty the event still records the
    surface and the outcome.

    Refusal prose is not exempt from egress control just because the call failed.
    ``StaleIndex`` from a fingerprint drift embeds ``render.describe_record`` for
    BOTH the cached and the fresh element, i.e. two verbatim accessibility titles;
    a ``ComputerUseError`` from the driver quotes the app label; a
    ``ValidationError`` echoes an argument. So this applies the same two controls
    :func:`_render_snapshot` applies, in the same order:

    1. **the observation ceiling** — a policy that denies ``element_values`` or
       ``file_paths`` must not be readable around by provoking a drift, so the
       refusal's text is put THROUGH ``apply_observation_ceiling`` as the
       ``PAYLOAD_TEXT`` field (the channel ``_scrub_paths`` already understands),
       and the element-values deny — which has no per-field meaning in a flat
       sentence — drops the desktop detail entirely in favour of the ordinary
       "call ``computer_get_state`` again" instruction. Dropping rather than
       narrowing is the honest reading: a drift message IS an element value.
    2. **the redaction pass** — ``policy.redact_result``, the same call every
       renderer in :mod:`render` ends with.

    The ceiling's suppression notes are deliberately NOT appended here: a refusal
    is one sentence the model must act on, and "screenshot suppressed" alongside
    "call get_state again" would only be noise (nothing in a refusal carries
    pixels or a tree in the first place).

    Fail-closed on a ceiling that cannot be evaluated: ``permitted_observation_
    channels`` already returns the empty set on an error, which lands on the
    detail-dropping branch below.
    """
    _audit_refused(session_key, agent, tool_name)
    if OBS_ELEMENT_VALUES not in gate.permitted_observation_channels(
        session_key=session_key, agent=agent, app=app
    ):
        # Before the path scrub, matching ``apply_observation_ceiling``'s own
        # ordering: a fragment already replaced by the placeholder needs no scrub.
        detail = _strip_desktop_detail(detail)
    shaped = gate.apply_observation_ceiling(
        {gate.PAYLOAD_TEXT: detail},
        session_key=session_key,
        agent=agent,
        app=app,
    )
    return f"{ERROR_PREFIX}{policy.redact_result(str(shaped.get(gate.PAYLOAD_TEXT) or ''))}"


def _strip_desktop_detail(text: str) -> str:
    """Drop every quoted fragment from a refusal, keeping its instruction.

    Used only when the ``element_values`` channel is DENIED. Every place this
    package interpolates desktop text into a refusal wraps it in quotes —
    ``render.describe_record`` uses ``"`` for a title, and the drivers plus
    ``policy`` use ``'`` for an app label — so replacing each quoted run with the
    governance placeholder removes the disclosure while leaving the actionable half
    intact: ``element_index 7 changed since the last computer_get_state (was
    <redacted:policy>, now <redacted:policy>). Call computer_get_state again.``

    A blunt instrument on purpose. A refusal is one flat sentence with no field
    structure for the ceiling to narrow, so it is biased toward OVER-redacting: a
    reason string carrying two apostrophes has its middle replaced too. That costs
    a little prose on a path the operator has already chosen to lock down, whereas
    under-redacting is the disclosure this function exists to prevent.
    """
    return _QUOTED_RUN_RE.sub(GOVERNED_VALUE_PLACEHOLDER, text)


def _render_apps(apps: Sequence[AppRef], *, session_key: str, agent: str, app: str) -> str:
    """Shape and render the application list through the observation ceiling."""
    payload = {
        gate.PAYLOAD_APPS: tuple(
            {
                "name": ref.name,
                "bundle_id": ref.bundle_id,
                "pid": ref.pid,
                gate.APP_WINDOW_TITLE_KEY: ref.window_title,
            }
            for ref in apps
        ),
        gate.PAYLOAD_NOTES: (),
    }
    shaped = gate.apply_observation_ceiling(payload, session_key=session_key, agent=agent, app=app)
    rebuilt = tuple(
        AppRef(
            name=str(entry.get("name") or ""),
            pid=int(entry.get("pid") or 0),
            bundle_id=str(entry.get("bundle_id") or ""),
            window_title=str(entry.get(gate.APP_WINDOW_TITLE_KEY) or ""),
        )
        for entry in shaped.get(gate.PAYLOAD_APPS) or ()
        if isinstance(entry, Mapping)
    )
    return _with_notes(render.render_apps(rebuilt), shaped)


def _render_snapshot(
    snap: Snapshot,
    req: SnapshotRequest,
    *,
    session_key: str,
    agent: str,
    app: str,
    extra_notes: tuple[str, ...] = (),
    screenshot_suppressed: bool = False,
) -> str:
    """Shape and render one snapshot through the observation ceiling.

    The snapshot is flattened into the structured payload the ceiling understands,
    narrowed, and then rebuilt — so the ceiling shapes the DATA rather than a
    request flag, and an implementation that attached a screenshot unconditionally
    still could not leak past a deny.
    """
    payload: dict[str, Any] = {
        gate.PAYLOAD_WINDOW_TITLE: snap.window_title,
        gate.PAYLOAD_ELEMENTS: tuple(_element_payload(elem) for elem in snap.elements),
        gate.PAYLOAD_SCREENSHOT: snap.image_path,
        "screenshot_width": snap.image_width,
        "screenshot_height": snap.image_height,
        "screenshot_bytes": len(snap.image_jpeg),
        gate.PAYLOAD_NOTES: extra_notes,
    }
    if screenshot_suppressed:
        payload[OBS_SUPPRESSED_KEY] = OBS_SUPPRESSED_BY_POLICY
    shaped = gate.apply_observation_ceiling(payload, session_key=session_key, agent=agent, app=app)
    kept_image = bool(shaped.get(gate.PAYLOAD_SCREENSHOT))
    rebuilt = replace(
        snap,
        window_title=str(shaped.get(gate.PAYLOAD_WINDOW_TITLE) or ""),
        elements=tuple(
            _element_from_payload(entry)
            for entry in shaped.get(gate.PAYLOAD_ELEMENTS) or ()
            if isinstance(entry, Mapping)
        ),
        image_path=str(shaped.get(gate.PAYLOAD_SCREENSHOT) or ""),
        # The bytes are dropped alongside the path so ``render`` cannot report a
        # size for an image the model was not given.
        image_jpeg=snap.image_jpeg if kept_image else b"",
        image_width=snap.image_width if kept_image else 0,
        image_height=snap.image_height if kept_image else 0,
    )
    return _with_notes(render.render_tree(rebuilt, text_limit=req.text_limit), shaped)


def _element_payload(elem: ElementRec) -> dict[str, Any]:
    """Flatten one record into the ceiling's element shape.

    EVERY field, not just the governable ones. ``frame``, ``traits`` and ``focused``
    are not observation channels the ceiling narrows, but this dict is the ONLY thing
    ``_element_from_payload`` gets to rebuild from, so a field omitted here is a field
    silently deleted from every rendered tree — the lossy-rebuild bug
    ``capture_macos`` was fixed for by switching to ``dataclasses.replace``. Keep this
    in sync with ``ElementRec``; ``test_computer_use_snapshot.py`` asserts the
    round-trip is total so a newly added field cannot be forgotten here.
    """
    return {
        "index": elem.index,
        "role": elem.role,
        "subrole": elem.subrole,
        gate.ELEMENT_TITLE_KEY: elem.title,
        gate.ELEMENT_VALUE_KEY: elem.value,
        "actions": elem.actions,
        "depth": elem.depth,
        "secure": elem.secure,
        "enabled": elem.enabled,
        "frame": elem.frame,
        "traits": elem.traits,
        "focused": elem.focused,
    }


def _element_from_payload(entry: Mapping[str, Any]) -> ElementRec:
    """Rebuild a record from a (possibly narrowed) element mapping.

    Defensive about every field: the mapping has been through the ceiling, which
    replaces values with placeholders and may have been extended by a future
    channel, so a missing or retyped key must not raise inside a render path.
    """
    actions = entry.get("actions")
    traits = entry.get("traits")
    return ElementRec(
        index=int(entry.get("index") or 0),
        role=str(entry.get("role") or ""),
        subrole=str(entry.get("subrole") or ""),
        title=str(entry.get(gate.ELEMENT_TITLE_KEY) or ""),
        value=str(entry.get(gate.ELEMENT_VALUE_KEY) or ""),
        actions=tuple(str(a) for a in actions) if isinstance(actions, (list, tuple)) else (),
        depth=int(entry.get("depth") or 0),
        secure=bool(entry.get("secure")),
        enabled=bool(entry.get("enabled", True)),
        # A half-read frame is worse than none (see ``snapshot_macos``): only a
        # complete 4-tuple of finite numbers becomes a rect, anything else is None.
        frame=_frame_from_payload(entry.get("frame")),
        traits=tuple(str(t) for t in traits) if isinstance(traits, (list, tuple)) else (),
        focused=bool(entry.get("focused")),
    )


def _frame_from_payload(raw: Any) -> "tuple[float, float, float, float] | None":
    """Coerce a payload ``frame`` back to a rect, or ``None``.

    Defensive for the same reason as the rest of ``_element_from_payload``: the
    mapping has been through the ceiling and may have been re-typed. A partial or
    non-numeric rect resolves to ``None`` rather than a plausible-looking rectangle
    pointing somewhere else — ``bool`` is excluded explicitly because it is an ``int``
    subclass, and a NaN/inf coordinate is refused because it would render as garbage
    a model might pass to a coordinate click.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    out: list[float] = []
    for part in raw:
        if isinstance(part, bool) or not isinstance(part, (int, float)):
            return None
        value = float(part)
        if value != value or value in (float("inf"), float("-inf")):
            return None
        out.append(value)
    return (out[0], out[1], out[2], out[3])


def _with_notes(body: str, shaped: Mapping[str, Any]) -> str:
    """Append the ceiling's suppression notes below a rendered body.

    Appended AFTER the renderer's redaction pass on purpose: the notes are this
    package's own static prose (no user data), and running them through redaction
    could only mangle the explanation the model needs to stop retrying.
    """
    notes = tuple(str(note) for note in shaped.get(gate.PAYLOAD_NOTES) or () if str(note))
    if not notes:
        return body
    return "\n".join((body, "", *notes))


def _declared_observations(tool_name: str) -> tuple[str, ...]:
    """The observation channels a tool's reply would carry, for the gate.

    Only the MANDATORY channel of each observation tool is declared, and only for
    the observation tools:

    * ``computer_get_state`` declares ``a11y_tree``. Denying it denies the call —
      correct, because "no accessibility tree" has no partial form. ``screenshot``
      is deliberately NOT declared: a fleet that denies pixels must still be able
      to read a tree, so that channel is enforced by response shaping (the call
      succeeds with the image omitted and annotated) rather than by a gate denial.
    * ``computer_list_apps`` declares ``window_titles`` — the only user data it
      can disclose.
    * A mutator declares nothing. Its refreshed tree is narrowed by the response
      shaper; declaring a channel here would let "deny screenshots" silently
      become "deny clicking".
    """
    if tool_name == TOOL_GET_STATE:
        return (OBS_A11Y_TREE,)
    if tool_name == TOOL_LIST_APPS:
        return (OBS_WINDOW_TITLES,)
    return ()


# ── Helpers ──


def _mutation_walk_budget(
    svc: "service.ComputerUseService", target: AppRef, *, session_key: str
) -> SnapshotRequest:
    """The tree budget a mutating action's walks must use.

    A mutating tool accepts no ``max_tree_nodes`` / ``max_tree_depth`` /
    ``text_limit`` arguments — the model set those on the ``computer_get_state`` that
    produced the indices it is now acting on. So the drift-verification walk and the
    post-action refresh both have to reproduce THAT walk, not the config default:
    with the default, an index above it resolves to "no element at that index", and
    the refresh the model is handed next is truncated the same way, so calling
    ``computer_get_state`` again cannot break the loop.

    Falls back to the config default when the cache holds no stamped budget (a
    snapshot a backend built directly, or an action on an app with no cached state —
    which the freshness check refuses moments later anyway).
    """
    cached = svc.index.get(target.window_key, session_key=session_key)
    if cached is not None and cached.walk_budget is not None:
        return cached.walk_budget
    return service.snapshot_request()


def _opt_int(args: Mapping[str, Any], name: str) -> "int | None":
    """A validated optional int argument, or ``None`` when absent."""
    value = args.get(name)
    # ``bool`` is an ``int`` subclass; the schema already rejects it, but this
    # helper is also reached from the in-process entry point.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_bool(args: Mapping[str, Any], name: str) -> "bool | None":
    """A validated optional bool argument, or ``None`` when absent."""
    value = args.get(name)
    return value if isinstance(value, bool) else None


def _static_refusal(detail: str, *, session_key: str, agent: str, tool_name: str) -> str:
    """An audited exit for a refusal made of this package's OWN static prose.

    Distinct from :func:`_refusal`, and the difference is the point. ``_refusal``
    handles text that can quote the operator's desktop and therefore has to traverse
    the observation ceiling and the redaction pass. These refusals are static
    sentences about the CALLER'S OWN request — an unregistered tool name, the feature
    being disabled, a missing ``element_index``, a coordinate form under a targets
    ceiling, a malformed pointer request, a governance denial — so shaping them would
    be pointless work on a string that contains nothing to shape.

    What they DID share with ``_refusal`` and were missing is the audit: six sites
    returned ``f"{ERROR_PREFIX}{…}"`` inline, so
    a malformed CLI click produced no SEL record at all. Routing them through one
    helper is what makes that structural rather than a rule to remember — a future
    seventh refusal cannot be added without either using this or being obviously
    inconsistent with the five beside it.
    """
    _audit_refused(session_key, agent, tool_name)
    return f"{ERROR_PREFIX}{detail}"


def _audit_refused(session_key: str, agent: str, tool_name: str) -> None:
    """SEL-audit a PRE-GATE refusal (validation, unknown tool, stale index, paste …).

    Deliberately records NO resources: the refusal text can quote a window title or
    an accessibility value, and this event fires before the observation ceiling has
    been applied to it — so the audit line carries the fact and the tool name, never
    the desktop detail. ``log_tool_invocation`` redacts its own fields, but "redacted
    credentials" is a weaker guarantee than "never included".

    Best-effort and logged, matching :func:`_audit_allowed`: an audit failure must not
    turn a refusal into a crash.
    """
    try:
        sel().log_tool_invocation(
            session_key=session_key,
            agent=agent or "kirocrew",
            source="mcp",
            tool_name=tool_name or "computer_use",
            tool_kind="computer_use",
            outcome="refused",
            resources="",
        )
    except Exception:
        logger.debug("computer-use refusal audit failed", exc_info=True)


def _audit_allowed(session_key: str, agent: str, tool_name: str, target: "AppRef | None") -> None:
    """SEL-audit an ALLOWED computer-use call.

    Every call is audited, not only refusals: the operator's record of what the
    agent did to their desktop is the whole point of an audit trail for this
    surface, and a permitted ``set_value`` in an authenticated app is exactly the
    event a later investigation needs. Denials are audited inside the gate.

    Best-effort — an audit failure must not wedge an authorized call — but logged,
    never swallowed silently.
    """
    try:
        sel().log_tool_invocation(
            session_key=session_key,
            agent=agent or "kirocrew",
            source="mcp",
            tool_name=tool_name,
            tool_kind="computer_use",
            outcome="allowed",
            # The resolved identity, not the agent's claim — the same value the
            # gate authorized. ``log_tool_invocation`` redacts its own fields.
            resources="" if target is None else target.label,
        )
    except Exception:
        logger.debug("computer-use allow audit failed", exc_info=True)


__all__ = ["dispatch", "dispatch_tool"]
