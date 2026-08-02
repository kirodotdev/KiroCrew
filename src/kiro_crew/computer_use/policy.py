"""Target/input policy and egress redaction for computer use.

Pure decision logic — no ctypes, no I/O, no platform calls. Every function here
is a *deny* gate: it either returns ``None`` (nothing to say) or a refusal
sentence. Nothing in this module can widen access.

Why this module carries so much weight: computer use is structurally invisible
to every other security plane in KiroCrew. ``is_sensitive_path`` cannot see a
password field's ``AXValue``; the bash deny rules cannot see a keystroke posted
into a terminal window; the exfil URL scan cannot see a logged-in banking tab
rendered as pixels. The path matchers protect the filesystem the agent reaches
through *tools*; this module is the only thing standing between the agent and
the same data reached through *the operator's own windows*.
"""

from __future__ import annotations

from kiro_crew import security
from kiro_crew.computer_use.types import (
    CLICK_METHOD_ACCESSIBILITY,
    CLICK_METHOD_APP_POST,
    CLICK_METHOD_AUTO,
    CLICK_METHOD_SKY_CLICK,
    CLICK_METHODS,
    DEFAULT_CLICK_METHOD,
    ERR_POINT_REQUIRED,
    ERR_SKY_CLICK_BUTTON,
    ERR_UNKNOWN_CLICK_METHOD,
    ERR_UNKNOWN_MOUSE_BUTTON,
    MOUSE_BUTTON_LEFT,
    MOUSE_BUTTONS,
    REFUSAL_ACCESSIBILITY_NEEDS_INDEX,
    REFUSAL_CLICK_TARGET_AMBIGUOUS,
    REFUSAL_CLICK_TARGET_MISSING,
    REFUSAL_DENIED_APP,
    REFUSAL_SECURE_TARGET,
    REFUSAL_TEXT_SENSITIVE,
    AppRef,
    DeniedApp,
    ElementRec,
    PolicyConfig,
)
from kiro_crew.platform import redact_via_context

# ── Categories (stable ids: they appear in the dashboard payload and in tests) ──
CATEGORY_KIROCREW_SELF = "kirocrew_self"

# ── The built-in target denylist (a FLOOR — code, not configuration) ──
#
# Matching is by bundle-id PREFIX (so a helper process under a blocked bundle is
# covered too) OR by a process-name SUBSTRING (the Windows and Linux drivers may
# only ever learn a process name, and macOS reports one for unbundled binaries).
# BOTH comparisons are case-insensitive on BOTH sides — entries below are written
# in whatever case Apple ships (``com.apple.userNotificationCenter``), and
# ``denied_rule_for`` lowercases the table entry as well as the subject. Comparing
# only a lowercased subject against a mixed-case entry made that one row
# unmatchable, which is a security row that reads as protection and does nothing.
# ``extra_denied_apps`` can only ADD to this list; there is deliberately no
# mechanism to remove an entry, so the floor cannot be edited away from the
# dashboard or by a prompt-injected agent.
#
# Honest scope statement, because a reviewer will find it otherwise: this list
# is *enumerable*, not provably complete. An app that embeds a shell without a
# recognizable bundle id — a terminal pane inside an IDE, an in-app JS console —
# is not covered. That is why the governance ``apps`` ruleset exists as the
# enterprise force-pin on top, and why the input-text scan below is a second,
# independent layer.
_DENIED_BUNDLE_PREFIXES: tuple[DeniedApp, ...] = (
    DeniedApp(
        category=CATEGORY_KIROCREW_SELF,
        # The ONE app that stays refused, and the only one that is not a judgement
        # call about the operator's own machine.
        #
        # KiroCrew's Settings UI is where the computer-use primary enable lives, and
        # that enable is on the keystone precisely so the AGENT cannot reach it (the
        # keystone sits behind ``security._SENSITIVE_HOME_DIRS``). If the agent could
        # DRIVE our own window, it would click that toggle itself — and every other
        # security control on the same page — which routes around the keystone
        # entirely. Refusing our own bundle is what keeps "the operator, out of band,
        # is the only one who can widen this" true.
        #
        # Everything else that used to be here (terminals, password managers, System
        # Settings, system auth dialogs) is deliberately GONE: on a personal machine
        # the agent is trusted with the desktop, and a shipped list of "apps you may
        # not automate" was both incomplete by construction and in the operator's way.
        reason=(
            "KiroCrew's own dashboard can change the agent's security settings, "
            "which must only be done by the operator out-of-band"
        ),
        bundle_prefixes=("com.amazon.kiro.crew", "dev.kiro.crew"),
        name_substrings=("kiro crew", "kirocrew"),
        # The dashboard is ALSO reachable as a browser tab, where the app identity
        # is Chrome's or Safari's and the two lists above cannot fire. The window
        # title is the only signal that survives that hosting, so it carries the
        # same rule. Substrings, not exact strings: the tab title takes a badge
        # prefix ("(3) Kiro Crew") and popouts a "<label> — Kiro Crew" suffix.
        title_substrings=("kiro crew", "kirocrew"),
    ),
)


def blocked_app_categories() -> tuple[dict[str, str], ...]:
    """The denylist as ``({category, reason}, ...)`` for the Settings panel.

    Read-only on purpose: the panel explains the floor, it does not edit it.
    """
    return tuple({"category": d.category, "reason": d.reason} for d in _DENIED_BUNDLE_PREFIXES)


def denied_rule_for(app: AppRef) -> DeniedApp | None:
    """Return the built-in denylist rule matching *app*, or ``None``.

    THREE independent signals, any one of which denies — a driver may know the
    bundle id, the process name, or only one of the two, and the third covers the
    case where none of them is ours at all:

    * ``bundle_prefixes`` / ``name_substrings`` — the app's own identity;
    * ``title_substrings`` — the resolved WINDOW TITLE. Load-bearing for the
      self-target rule: KiroCrew's dashboard served into a browser tab presents
      Chrome's (or Safari's) bundle id and process name, so identity matching cannot
      see it, and the agent could drive our own Settings UI — clicking the very
      toggle the keystone exists to keep out of its reach.

    Deliberately a SUBSTRING test on the title rather than an exact match: the tab
    title carries a badge prefix (``(3) Kiro Crew``) and a page suffix
    (``… — Kiro Crew``), and popout frames prepend their own label.

    The known false-positive cost, stated rather than discovered: any window whose
    title merely CONTAINS "kiro crew" is refused — a code editor with this repo open,
    a browser reading this very PR. That is accepted because the failure directions
    are not symmetric. Refusing an unrelated window is visible and recoverable (the
    refusal names the target); failing to refuse our own dashboard silently hands the
    agent its own security settings. A title is also the only signal available — the
    driver has no page URL, and ``AXURL`` is not read anywhere in this package.
    """
    bundle = (app.bundle_id or "").strip().lower()
    name = (app.name or "").strip().lower()
    title = (app.window_title or "").strip().lower()
    for rule in _DENIED_BUNDLE_PREFIXES:
        # Both sides are lowercased. Lowercasing only the *subject* was a real
        # defect: ``com.apple.userNotificationCenter`` is stored mixed-case (that
        # is how Apple ships the bundle id), so no lowercased bundle could ever
        # start with it and that denylist row silently matched nothing. A table
        # entry that cannot fire is worse than a missing one — it reads as
        # protection during review. ``TestDenylistCaseFolding`` walks the whole
        # table, so a future mixed-case entry cannot silently do nothing either.
        if bundle and any(bundle.startswith(p.lower()) for p in rule.bundle_prefixes):
            return rule
        if name and any(sub.lower() in name for sub in rule.name_substrings):
            return rule
        if title and any(sub.lower() in title for sub in rule.title_substrings):
            return rule
    return None


def title_is_denied(title: str) -> bool:
    """Whether *title* alone trips a built-in denylist ``title_substrings`` rule.

    Exposed for the window-enumeration layer, which must decide WHICH window of a
    multi-window process to surface. Input is delivered per-PID
    (``CGEventPostToPid``), so if any window of a process is our own dashboard the
    whole process has to refuse — ``apps_macos.list_apps`` therefore prefers a
    denied title over an innocuous one rather than keeping whichever window the
    window server happened to list first.

    Lives here, next to the table it reads, so the enumeration layer cannot drift
    from the policy layer's idea of a denied title.
    """
    subject = (title or "").strip().lower()
    if not subject:
        return False
    return any(
        sub.lower() in subject for rule in _DENIED_BUNDLE_PREFIXES for sub in rule.title_substrings
    )


def check_app(app: AppRef, cfg: PolicyConfig) -> str | None:
    """Refuse *app* as a computer-use target, or return ``None`` to allow it.

    Order is deliberate and tightest-first:

    1. the built-in denylist (a floor — never overridable);
    2. the operator's ``extra_denied_apps`` additions;
    3. the operator's ``allowed_apps`` allow-list, when non-empty.

    Evaluating the floor first means an operator cannot accidentally allow-list
    their way past it, and an empty allow-list means "everything not denied"
    rather than "nothing" — the allow-list is an optional narrowing, not the
    primary enable (that lives on the keystone state file).
    """
    rule = denied_rule_for(app)
    if rule is not None:
        return REFUSAL_DENIED_APP.format(app=app.bundle_id or app.name, reason=rule.reason)

    bundle = (app.bundle_id or "").strip().lower()
    name = (app.name or "").strip().lower()
    for pattern in cfg.extra_denied_apps:
        if _matches_operator_pattern(pattern, bundle, name):
            return REFUSAL_DENIED_APP.format(
                app=app.bundle_id or app.name,
                reason="added to the blocked list by the operator",
            )

    if cfg.allowed_apps and not any(
        _matches_operator_pattern(pattern, bundle, name) for pattern in cfg.allowed_apps
    ):
        return REFUSAL_DENIED_APP.format(
            app=app.bundle_id or app.name,
            reason="not in the operator's allowed-apps list",
        )
    return None


def check_input_target(
    app: AppRef,
    rec: ElementRec | None,
    text: str,
    cfg: PolicyConfig,
) -> str | None:
    """Refuse an input action, or return ``None`` to allow it.

    Two independent layers, in order:

    1. **Secure-target refusal.** A macOS password box reports
       ``role='AXTextField'`` with ``subrole='AXSecureTextField'``, so
       ``ElementRec.secure`` (set from BOTH attributes by the driver) is the
       only reliable signal. Writing into it would let the agent set — or, with
       a follow-up read, learn — a credential.
    2. **Text scan.** Free text destined for another application is run through
       the same generic matchers the bash gate uses. This is explicitly a
       SECOND layer, not the primary control: the maintainers' own position
       (recorded in ``security.py``) is that chasing shell-parser completeness
       in a text matcher is a losing game, which is why terminals are refused
       wholesale by :func:`check_app`. The residual false-positive risk (prose
       that happens to look like a destructive command) is accepted: these
       patterns are command-shaped, and refusing to type is recoverable while
       typing ``rm -rf`` into a shell is not.

    The *app* argument is not re-checked here — :func:`check_app` runs first at
    the single dispatch chokepoint — but it is taken so refusals can name the
    target, and so a future per-app input rule has a home.
    """
    if rec is not None and rec.secure:
        return REFUSAL_SECURE_TARGET.format(
            index=rec.index,
            app=app.bundle_id or app.name,
            subrole=rec.subrole or rec.role,
        )
    if not text:
        return None

    reason = security.is_sensitive_bash_command(text)
    if reason:
        return REFUSAL_TEXT_SENSITIVE.format(app=app.bundle_id or app.name, reason=reason)
    reason = security.audit_bash_exfiltration(text)
    if reason:
        return REFUSAL_TEXT_SENSITIVE.format(app=app.bundle_id or app.name, reason=reason)
    # ``is_denied`` with ``denied_regexes=None`` fails CLOSED to the full
    # built-in rule set — the right posture here: a user opt-out from a bash
    # deny rule is a decision about commands the AGENT runs under the tool gate,
    # not a licence to type the same command into somebody else's window.
    reason = security.is_denied(text)
    if reason:
        return REFUSAL_TEXT_SENSITIVE.format(app=app.bundle_id or app.name, reason=reason)
    return None


def check_click_target(
    element_index: "int | None",
    point: "tuple[float, float] | None",
) -> str | None:
    """Refuse an ambiguous or targetless click, or return ``None``.

    Exactly ONE of (``element_index`` | ``x`` + ``y``) must be supplied. Both
    failures are REFUSED rather than resolved by a precedence rule, and that is a
    deliberate choice in both directions:

    * **both given** — the two name different targets (an element's press action
      versus a screen point), and there is no reading of the caller's intent that
      picks one. Silently preferring the index would make a model that meant the
      coordinates act somewhere else entirely, in a live application, with no
      signal that it happened.
    * **neither given** — there is nothing to click. The reference implementation
      makes ``element_index`` optional and coordinates optional independently, so
      an omission is expressible; here it is refused with the fix named.

    Pure argument shape, so it lives beside the other refusals rather than in the
    schema: ``validate_tool_args`` checks fields independently and has no
    cross-field vocabulary, and the in-process entry point must be bounded too.
    """
    has_point = point is not None
    has_index = element_index is not None
    if has_point and has_index:
        return REFUSAL_CLICK_TARGET_AMBIGUOUS
    if not has_point and not has_index:
        return REFUSAL_CLICK_TARGET_MISSING
    return None


def check_click_method(
    method: str,
    *,
    element_index: "int | None",
    point: "tuple[float, float] | None",
) -> str | None:
    """Refuse a click method that cannot address the target given, or ``None``.

    Two shape rules, both of which would otherwise surface as a confusing driver
    failure rather than as a legible argument error:

    * ``accessibility`` presses a specific control (``AXPress``), so it REQUIRES an
      ``element_index`` — there is no coordinate form of an accessibility action;
    * every other concrete method delivers a mouse event at a POINT, so it requires
      ``x`` + ``y``.

    ``auto`` is exempt: it is resolved from whatever the caller supplied
    (:func:`resolve_click_method`), which is the entire reason it is the default.
    """
    if method == CLICK_METHOD_AUTO:
        return None
    if method not in CLICK_METHODS:
        return ERR_UNKNOWN_CLICK_METHOD.format(method=method)
    if method == CLICK_METHOD_ACCESSIBILITY:
        return None if element_index is not None else REFUSAL_ACCESSIBILITY_NEEDS_INDEX
    if point is None:
        return ERR_POINT_REQUIRED.format(method=method)
    return None


def check_method_button(method: str, button: str) -> str | None:
    """Refuse a (method, button) pair the method cannot actually perform.

    Today this is one rule: ``sky_click`` is a LEFT-button recipe. Its private event
    sequence was reverse-engineered for a left click, and the button number is one
    field among nine — there is no evidence the rest of the recipe (the primer pair,
    the focus-flag record) is button-agnostic, and inventing a right-click variant
    would be guessing at undocumented ABI.

    Refusing is the only honest option, and the reason it is a REFUSAL rather than a
    silent downgrade is the same reason the accessibility menu ladder never falls
    back to a press: turning "open the context menu" into "activate the control" is
    a DIFFERENT gesture than the one requested, and it can destroy data. The
    previous behaviour built the recipe with the left-button codes regardless of
    what the caller asked for, so a right-click request through this method
    activated the control instead — silently, and on a background window the
    operator cannot see.

    Checked HERE, at the dispatch chokepoint, so the refusal is uniform with every
    other argument check and reaches the model as a legible message naming the
    working alternative. :mod:`macos_skylight` re-checks it as defence in depth,
    since that module is reachable from tests and from any future call site.
    """
    if method == CLICK_METHOD_SKY_CLICK and button != MOUSE_BUTTON_LEFT:
        return ERR_SKY_CLICK_BUTTON.format(button=button)
    return None


def resolve_click_method(
    method: str,
    *,
    element_index: "int | None",
    point: "tuple[float, float] | None",
) -> str:
    """Turn a requested method (possibly ``auto``) into a CONCRETE one.

    ``auto`` prefers ``accessibility`` when an element was named and falls back to
    ``app_post`` when a point was: the accessibility path needs no pointer, no
    coordinate transform and no window geometry, so it is both safer and more
    reliable, and the app-scoped mouse path is the next-best thing for a target
    that has no addressable element.

    **``auto`` NEVER resolves to ``global``.** That is a load-bearing invariant with
    its own test, not a preference: ``global`` warps the operator's physical cursor,
    and an implicit resolution onto it would let a model take the mouse without ever
    naming the method. Since the separate pointer opt-in was removed this is the
    ONLY thing standing between an ordinary click and the operator's cursor, so it
    matters more now, not less. A model that wants the pointer must say so.

    Called AFTER :func:`check_click_target` and :func:`check_click_method`, so a
    concrete method arriving here is already known to match its target shape and an
    ``auto`` has at least one of the two forms.
    """
    if method != CLICK_METHOD_AUTO:
        return method
    if element_index is not None:
        return CLICK_METHOD_ACCESSIBILITY
    if point is not None:
        return CLICK_METHOD_APP_POST
    # Unreachable via the dispatcher (``check_click_target`` refused the empty
    # case). Falling back to the default rather than raising keeps this a total
    # function for the in-process caller; ``check_click_method`` then refuses.
    return DEFAULT_CLICK_METHOD


def check_mouse_button(button: str) -> str | None:
    """Refuse an unknown mouse button, or return ``None``.

    Refused rather than defaulted to left for the same reason ``keymap.parse_key``
    refuses an unknown modifier: silently substituting a different button sends a
    DIFFERENT gesture than requested into a live application, and a right-click
    that quietly becomes a left-click can activate something instead of opening a
    context menu.
    """
    return None if button in MOUSE_BUTTONS else ERR_UNKNOWN_MOUSE_BUTTON.format(button=button)


def redact_result(text: str) -> str:
    """Final egress pass for every rendered result.

    Routes through the platform credential/exfil seam
    (``platform.redact_via_context``) rather than calling ``security.redact``
    directly, so a loaded companion's extra credential and cookie patterns
    apply. Fail-closed on a composition error is the shim's own behavior — we
    deliberately do not catch it here, because a host that could not compose
    its redaction policy must not silently emit an unredacted accessibility
    tree.

    Every renderer in :mod:`render` ends with this call. Accessibility trees and
    window titles leak real user data — filesystem paths, mounted volume names,
    bundle ids and document names were all observed in live probes — so this is
    not belt-and-suspenders, it is the primary egress control for tree text.
    """
    return redact_via_context(text)


def _matches_operator_pattern(pattern: str, bundle: str, name: str) -> bool:
    """Substring match of an operator-supplied pattern against both identifiers.

    Substring rather than glob so an operator typing ``terminal`` or
    ``com.apple.`` gets the intuitive (broader) result. Broadening an operator's
    *deny* entry is safe; for the allow-list it is the operator's own explicit
    choice, and the built-in floor is evaluated first regardless.
    """
    pat = pattern.strip().lower()
    if not pat:
        return False
    return (bool(bundle) and pat in bundle) or (bool(name) and pat in name)
