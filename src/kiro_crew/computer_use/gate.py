"""The computer-use enable check, and the audit of what it allowed.

Deliberately small. An earlier revision of this module carried a full governance
model for computer use — eight ``SCOPE_CATALOG`` rows (capability, actions, apps,
app_names, observations, targets, approval, pointer), an unattended-surface
refusal, an interactive-approval floor, and a per-app disclosure filter. All of it
is gone: the product decision is that computer use is one operator opt-in, and
after that the agent drives the desktop the way the operator would.

What remains here is the one question worth asking on every call — *is the feature
on?* — plus the SEL audit trail, so the operator has a record of what the agent did
to their desktop.

Where the real protections live now, none of which is in this file:

* the **primary enable** is on the keystone ``computer_use.json``
  (``enable_state``), which ``security._SENSITIVE_HOME_DIRS`` fences the agent away
  from. That is what keeps "the agent cannot turn on its own desktop automation"
  true, and it is checked at the dispatch chokepoint in :mod:`tools`;
* **KiroCrew's own window** is refused by :mod:`policy`, because driving our own
  Settings UI would route around the keystone above;
* **password fields** are never read and never photographed (``ElementRec.secure``
  → the renderer emits a placeholder, and a window holding one gets no screenshot
  at all). That is a privacy floor, not a policy knob;
* **credential redaction** on the way out is the repo-wide control every other
  egress path already runs.

Nothing here touches ctypes, the filesystem, or a platform framework, so this
module imports and tests identically on macOS, Linux, Windows and in CI.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from kiro_crew.computer_use.types import (
    ALL_OBSERVATION_CHANNELS,
    AUDIT_POINTER_ITEM,
    AUDIT_TOOL_PREFIX,
    REFUSAL_POINTER_NOT_ENABLED,
)
from kiro_crew.platform.governance import (
    CU_CLASS_MUTATE,
    computer_use_action_classes,
)

logger = logging.getLogger(__name__)

# Payload keys the renderers hand to :func:`apply_observation_ceiling`, and read
# back out of it. Named constants because ``tools`` and ``render`` both build and
# destructure these dicts, and a typo would silently drop a field rather than fail.
PAYLOAD_SCREENSHOT = "screenshot"
PAYLOAD_SCREENSHOT_META = ("screenshot_width", "screenshot_height", "screenshot_bytes")
PAYLOAD_WINDOW_TITLE = "window_title"
PAYLOAD_ELEMENTS = "elements"
PAYLOAD_APPS = "apps"
PAYLOAD_NOTES = "notes"
PAYLOAD_TEXT = "text"
ELEMENT_VALUE_KEY = "value"
ELEMENT_TITLE_KEY = "title"
APP_WINDOW_TITLE_KEY = "window_title"


def require_computer_use(
    action: str,
    *,
    session_key: str,
    agent: str = "",
    app: str = "",
    app_bundle_id: str = "",
    app_display_name: str = "",
    observations: "Any" = (),
    target_roles: "Any" = (),
    requires_app_identity: bool = True,
    approval_recorded: bool = False,
) -> "str | None":
    """Always returns ``None`` (proceed). Audits the call.

    The signature is preserved so ``tools.py``'s ordered chokepoint reads the same
    and so a future edition can reintroduce a decision here without touching every
    call site — but there is no longer a governance decision to make. The keystone
    primary enable, checked upstream in :mod:`tools`, is the whole gate.

    Every parameter after ``action`` is accepted and ignored on purpose rather than
    deleted: they are the identity the SEL audit records, and dropping them from the
    signature would make the audit line poorer for no gain.
    """
    _audit_allowed(session_key, agent, action, app_bundle_id or app_display_name)
    return None


def require_pointer_move(
    action: str,
    *,
    method: str,
    session_key: str,
    agent: str = "",
    app: str = "",
    pointer_enabled: bool = True,
) -> "str | None":
    """Always returns ``None`` when the feature is on.

    The real-pointer path used to need a second opt-in (``allow_pointer_move``) and
    a governance permit of its own. Both are gone: one enable covers the feature,
    and ``policy.resolve_click_method`` still requires the model to NAME
    ``click_method: "global"`` explicitly — ``auto`` never resolves to it, so the
    pointer is never warped by accident.

    ``pointer_enabled`` is retained (defaulting to True) only so an in-process
    caller can still refuse locally if it wants to.
    """
    if not pointer_enabled:
        return REFUSAL_POINTER_NOT_ENABLED
    return None


def audit_pointer_move(
    action: str,
    *,
    method: str,
    session_key: str,
    agent: str = "",
    app_label: str = "",
) -> None:
    """SEL-audit a real-pointer gesture. Best-effort; never raises.

    Retained after the governance removal because it is the one record that says
    the operator's physical cursor was moved — the thing they would most want to
    find in a log afterwards.

    ``tool_kind`` is deliberately its OWN value rather than the plain
    ``computer_use`` every other call uses: it is what makes "did the agent ever
    take control of my mouse?" answerable with one filter over the trail, instead of
    requiring the reader to parse ``resources`` on every computer-use row.
    """
    try:
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key=session_key,
            agent=agent or "kirocrew",
            source="mcp",
            tool_name=f"{AUDIT_TOOL_PREFIX}{action}",
            tool_kind="computer_use_pointer",
            outcome="ok",
            resources=(
                f"{AUDIT_POINTER_ITEM}={method}" + (f" app={app_label}" if app_label else "")
            ),
        )
    except Exception:
        logger.debug("pointer-move audit failed", exc_info=True)


def is_mutating_action(action: str) -> bool:
    """Whether *action* synthesizes input into another application.

    Reads the code-owned class table in ``platform/governance.py`` rather than a
    private copy, so "which verbs are mutating" has one definition.
    """
    return CU_CLASS_MUTATE in computer_use_action_classes(action)


def permitted_observation_channels(
    *, session_key: str = "", agent: str = "", app: str = ""
) -> frozenset[str]:
    """Every observation channel. No channel is withheld any more.

    Kept as a function (rather than inlining the constant at the call sites) so the
    screenshot relay and the renderers keep one shared answer, and so an edition
    that wants to narrow observations again has a single place to do it.
    """
    return frozenset(ALL_OBSERVATION_CHANNELS)


def apply_observation_ceiling(
    payload: Mapping[str, Any], *, session_key: str = "", agent: str = "", app: str = ""
) -> dict[str, Any]:
    """Pass *payload* through unchanged.

    The ceiling used to blank window titles, strip element values and scrub file
    paths according to the ``computer_use.observations`` scope. With that scope gone
    there is nothing to narrow: the renderers' own secure-field suppression and the
    package-wide credential redaction are what protect the output now.
    """
    return dict(payload)


def app_is_disclosable(
    *,
    bundle_id: str,
    display_name: str,
    session_key: str = "",
    agent: str = "",
    app: str = "",
) -> bool:
    """Whether ``computer_list_apps`` may name this application.

    Now only "does it have an identity at all" — an app with neither a bundle id
    nor a display name is dropped because there is nothing to show, not because a
    policy forbids it. The per-app governance axes that used to filter this list
    are gone.
    """
    return bool(bundle_id or display_name)


def _audit_allowed(session_key: str, agent: str, action: str, target: str) -> None:
    """SEL-audit an allowed computer-use call. Best-effort; never raises.

    Every call is audited, not only the interesting ones: with the governance layer
    removed, this trail is the operator's primary record of what the agent did to
    their desktop.
    """
    try:
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key=session_key,
            agent=agent or "kirocrew",
            source="mcp",
            tool_name=f"{AUDIT_TOOL_PREFIX}{action}",
            tool_kind="computer_use",
            outcome="ok",
            resources=f"app={target}" if target else "",
        )
    except Exception:
        logger.debug("computer-use audit failed", exc_info=True)


__all__ = [
    "APP_WINDOW_TITLE_KEY",
    "ELEMENT_TITLE_KEY",
    "ELEMENT_VALUE_KEY",
    "PAYLOAD_ELEMENTS",
    "PAYLOAD_SCREENSHOT",
    "PAYLOAD_SCREENSHOT_META",
    "PAYLOAD_WINDOW_TITLE",
    "PAYLOAD_APPS",
    "PAYLOAD_NOTES",
    "PAYLOAD_TEXT",
    "app_is_disclosable",
    "apply_observation_ceiling",
    "audit_pointer_move",
    "is_mutating_action",
    "permitted_observation_channels",
    "require_computer_use",
    "require_pointer_move",
]
