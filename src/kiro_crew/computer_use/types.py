"""Data types and constants for computer use.

Single home for EVERY constant and frozen dataclass the computer-use package
uses (AGENTS.md: no hardcoded strings/values in business logic). Deliberately
platform-free and dependency-free — this module imports nothing from
``kiro_crew`` and never touches ctypes, so it loads identically on macOS,
Linux, Windows and in CI.

The exceptions live here too: they are part of the seam contract that
``backend.py`` publishes and the platform drivers raise, and keeping them
beside the dataclasses means a caller needs exactly one import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# ── Platform identifiers ──
# ``ComputerUseBackend.platform_id`` values. Stable strings: they surface in
# refusal text, the dashboard payload and the SEL audit trail, so tests pin
# them and they must not drift with an internal rename.
PLATFORM_MACOS = "macos"
PLATFORM_WINDOWS = "windows"
PLATFORM_LINUX = "linux"
PLATFORM_FAKE = "fake"
PLATFORM_UNSUPPORTED = "unsupported"

# ── MCP tool names (server ``kirocrew-computer``) ──
# All prefixed ``computer_`` so they can never collide with the playwright
# server's ``browser_*`` tools in a shared allowlist.
TOOL_LIST_APPS = "computer_list_apps"
TOOL_GET_STATE = "computer_get_state"
TOOL_CLICK = "computer_click"
TOOL_DRAG = "computer_drag"
TOOL_TYPE_TEXT = "computer_type_text"
TOOL_PRESS_KEY = "computer_press_key"
TOOL_SET_VALUE = "computer_set_value"
TOOL_SCROLL = "computer_scroll"
TOOL_PERFORM_ACTION = "computer_perform_action"
TOOL_END_TURN = "computer_end_turn"

# Read-only tools: observation only, no input synthesis. ``computer_end_turn``
# is control-plane (it drops cached snapshots) and is neither read nor mutate.
READ_ONLY_TOOLS: frozenset[str] = frozenset({TOOL_LIST_APPS, TOOL_GET_STATE})
# Every tool that synthesizes input into another application's window. These
# are the ones the PreToolUse gate must NEVER auto-approve.
MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        TOOL_CLICK,
        TOOL_DRAG,
        TOOL_TYPE_TEXT,
        TOOL_PRESS_KEY,
        TOOL_SET_VALUE,
        TOOL_SCROLL,
        TOOL_PERFORM_ACTION,
    }
)
ALL_TOOLS: tuple[str, ...] = (
    TOOL_LIST_APPS,
    TOOL_GET_STATE,
    TOOL_CLICK,
    TOOL_DRAG,
    TOOL_TYPE_TEXT,
    TOOL_PRESS_KEY,
    TOOL_SET_VALUE,
    TOOL_SCROLL,
    TOOL_PERFORM_ACTION,
    TOOL_END_TURN,
)

# ── Click methods (the ``click_method`` argument vocabulary) ──
# How a click reaches the target. All five of the reference implementation's
# methods. Four are built on PUBLIC APIs; ``sky_click`` alone needs the private
# SkyLight framework and is quarantined in ``macos_skylight`` — see that module for
# why the trade is made and how it degrades when Apple removes a symbol.
CLICK_METHOD_AUTO = "auto"
CLICK_METHOD_ACCESSIBILITY = "accessibility"
CLICK_METHOD_APP_POST = "app_post"
# The PRIVATE SkyLight path: clicks a window that is BEHIND other windows, without
# raising it and without moving the pointer. The one method whose implementation
# touches undocumented ABI, quarantined in ``macos_skylight``. Like ``global`` it
# must be NAMED — ``auto`` never resolves onto it, because a private-API path should
# never be reached by a model that did not ask for it.
CLICK_METHOD_SKY_CLICK = "sky_click"
CLICK_METHOD_GLOBAL = "global"
CLICK_METHODS: tuple[str, ...] = (
    CLICK_METHOD_AUTO,
    CLICK_METHOD_ACCESSIBILITY,
    CLICK_METHOD_APP_POST,
    CLICK_METHOD_SKY_CLICK,
    CLICK_METHOD_GLOBAL,
)
DEFAULT_CLICK_METHOD = CLICK_METHOD_AUTO
# Methods that MOVE THE OPERATOR'S REAL POINTER (``CGWarpMouseCursorPosition``
# plus a global ``CGEventPost``). ``auto`` is deliberately absent and must NEVER
# be added: the model has to NAME a pointer-warping method for the cursor to move
# at all, and an implicit resolution onto one would let it take the mouse without
# ever asking. That naming requirement is what the Settings copy discloses.
POINTER_MOVING_METHODS: frozenset[str] = frozenset({CLICK_METHOD_GLOBAL})

# ── Mouse buttons ──
# Values are the ``CGMouseButton`` enum; the mapping to the six per-button event
# types lives beside the FFI in ``macos_ffi``, because those are ABI facts.
MOUSE_BUTTON_LEFT = "left"
MOUSE_BUTTON_RIGHT = "right"
MOUSE_BUTTON_MIDDLE = "middle"
MOUSE_BUTTONS: tuple[str, ...] = (
    MOUSE_BUTTON_LEFT,
    MOUSE_BUTTON_RIGHT,
    MOUSE_BUTTON_MIDDLE,
)
DEFAULT_MOUSE_BUTTON = MOUSE_BUTTON_LEFT

# ── Coordinate click / drag bounds ──
# Coordinates are SCREEN points in the top-left convention every other part of
# this package uses. The ceiling is generous rather than display-derived: a
# multi-display desktop legitimately extends far past one screen's width, and the
# OS clamps a delivered event anyway — this bound only exists so a nonsense value
# cannot be handed to the FFI.
MAX_SCREEN_COORD = 32767.0
MIN_SCREEN_COORD = -32768.0
# Click count, matching the reference's double/triple-click support. The cap is
# 3 because macOS itself only reports up to a triple click as a distinct gesture.
DEFAULT_CLICK_COUNT = 1
MIN_CLICK_COUNT = 1
# ``MAX_CLICK_COUNT`` is defined in the Cursor Motion block below and is the
# same number for the same reason; a separate constant here would let the two
# drift so that the overlay drew fewer pulses than the driver posted clicks.

# Pause between a mutating action and the refresh walk that follows it. Not a
# politeness delay: without it the walk can read the PRE-action tree, and a model
# shown an unchanged result retries an action that already landed (a doubled click,
# a second row deleted). 150ms is what the reference implementation settled on after
# live tuning, and it is below the threshold where a turn feels slow.
POST_ACTION_SETTLE_SECS = 0.15

# ── Observation channels ──
# Was the ``computer_use.observations`` scope's item vocabulary; that scope is gone
# and ``gate.permitted_observation_channels`` now returns all of these
# unconditionally. Retained as the shared names the renderers and the screenshot
# relay agree on, and as the seam an edition would narrow.
# What a result MAY carry. Queried one channel at a time and enforced at RESPONSE
# SHAPING (``gate.apply_observation_ceiling``), not only against the caller's
# request flag — an implementation that attaches a screenshot unconditionally
# must still not leak past a deny.
OBS_A11Y_TREE = "a11y_tree"
OBS_SCREENSHOT = "screenshot"
OBS_OCR = "ocr"
OBS_WINDOW_TITLES = "window_titles"
OBS_FILE_PATHS = "file_paths"
OBS_ELEMENT_VALUES = "element_values"
# Every channel the enforcer knows. An operator's ``deny`` list may name a
# channel outside this tuple (open set, forward-compatible); the tuple is what
# the Settings snapshot enumerates and what ``apply_observation_ceiling``
# iterates.
ALL_OBSERVATION_CHANNELS: tuple[str, ...] = (
    OBS_A11Y_TREE,
    OBS_SCREENSHOT,
    OBS_OCR,
    OBS_WINDOW_TITLES,
    OBS_FILE_PATHS,
    OBS_ELEMENT_VALUES,
)

# Why a suppressed screenshot is ANNOUNCED rather than silently dropped: a model
# that asked for pixels and got none retries in a loop unless it is told the
# omission was deliberate. Mirrors ``SECURE_WINDOW_NOTE``'s reasoning for the
# always-on floor.
OBS_SUPPRESSED_KEY = "screenshot_suppressed_by"
OBS_SUPPRESSED_BY_POLICY = "policy"
OBS_SUPPRESSED_NOTE = (
    "Screenshot suppressed: your organization's security policy does not permit "
    "screen capture. The accessibility tree above is the full available detail."
)
OBS_VALUES_SUPPRESSED_NOTE = (
    "Element values suppressed: your organization's security policy does not "
    "permit reading field contents."
)
OBS_TITLES_SUPPRESSED_NOTE = (
    "Window titles suppressed: your organization's security policy does not "
    "permit reading window titles."
)
OBS_TREE_SUPPRESSED_NOTE = (
    "Accessibility tree suppressed: your organization's security policy does not "
    "permit reading window contents."
)
OBS_PATHS_SUPPRESSED_NOTE = (
    "Filesystem paths in this result were replaced: your organization's security "
    "policy does not permit disclosing file paths."
)
GOVERNED_VALUE_PLACEHOLDER = "<redacted:policy>"
GOVERNED_PATH_PLACEHOLDER = "<redacted:path>"

# Absolute-path shapes scrubbed when the ``file_paths`` channel is denied.
# Deliberately broad and deliberately NOT a completeness claim: an accessibility
# tree leaked real volume names, document paths and bundle ids in live probes, and
# a relative path or a path split across two AX attributes is not matched. The
# honest framing (documented in computer-use.md): denying ``file_paths`` reduces
# disclosure, it does not prove absence — a fleet that needs a bound should also
# narrow ``apps``. Covers POSIX absolute paths, ``~``-relative paths, and Windows
# drive/UNC paths.
PATH_SCRUB_PATTERNS: tuple[str, ...] = (
    r"(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'<>|]*",
    r"~?/[A-Za-z0-9._~\-][^\s\"'<>|]*",
)

# ── Keystone primary-enable state file ──
# NOT config.json: a primary enable for full desktop observation plus input
# synthesis is a security ceiling, and ``security.py`` records the governing
# precedent (the denied-command opt-out is kept off config.json for exactly
# this reason). See ``enable_state`` for the full rationale.
STATE_FILE_NAME = "computer_use.json"
STATE_KEY_ENABLED = "enabled"
STATE_KEY_ALLOWED_APPS = "allowed_apps"
STATE_KEY_EXTRA_DENIED_APPS = "extra_denied_apps"
# The REAL-POINTER opt-in. On the keystone rather than in ``config.json`` for the
# same reason ``enabled`` is: taking over the operator's physical mouse is a
# security ceiling, and ``config.json`` is writable by an auto-approved agent
# shell (``is_sensitive_bash_command`` does NOT block ``echo … > config.json``),
# so an opt-in stored there could be flipped by prompt injection. Read with the
# Keystone opt-in for the ``kirocrew computer apps`` / ``call`` diagnostics. Those
# two run desktop tools from a process whose caller CANNOT be authenticated unless
# the gateway injected a signed identity — an env var is writable by any shell the
# agent controls and a TTY can be allocated, so neither is evidence. The operator
# therefore consents once, in the keystone the agent cannot write, and an

# ── Snapshot / tree budgets ──
# Defaults are the shipped config.json values; the ``*_LIMIT`` ceilings are the
# hard caps the MCP schemas validate against, so an agent cannot ask for an
# unbounded walk.
DEFAULT_MAX_TREE_NODES = 1200
MAX_TREE_NODES_LIMIT = 5000
DEFAULT_MAX_TREE_DEPTH = 64
MAX_TREE_DEPTH_LIMIT = 128
DEFAULT_TEXT_LIMIT = 500
MAX_TEXT_LIMIT = 20000
MAX_ELEMENT_INDEX = 5000
MAX_TYPE_TEXT_LEN = 10000
MAX_KEY_LEN = 64
MAX_ACTION_LEN = 64

# ── Snapshot index lifecycle ──
# Element indices address a LIVE UI, so a cached snapshot is only usable for a
# short window. 90s is long enough for a model to reason between two tool calls
# and short enough that a stale tree is refused rather than acted on. Only a
# handful of apps are ever in play in one turn; the cap bounds sidecar RSS.
SNAPSHOT_TTL_SECS = 90
MAX_INDEXED_APPS = 8

# ── Scroll ──
SCROLL_UP = "up"
SCROLL_DOWN = "down"
SCROLL_LEFT = "left"
SCROLL_RIGHT = "right"
SCROLL_DIRECTIONS: tuple[str, ...] = (SCROLL_UP, SCROLL_DOWN, SCROLL_LEFT, SCROLL_RIGHT)
DEFAULT_SCROLL_PAGES = 1.0
MIN_SCROLL_PAGES = 0.1
MAX_SCROLL_PAGES = 20.0

# ── Screenshot ──
# Computer use does NOT inherit browse's 1920/q70: the accessibility tree is
# the primary channel and the image is corroboration, so 1280/q55 (measured
# fully legible, ~8.3k tokens vs ~41k for a raw PNG) is the right trade.
DEFAULT_ATTACH_SCREENSHOT = True
DEFAULT_SCREENSHOT_MAX_PX = 1280
DEFAULT_SCREENSHOT_JPEG_QUALITY = 55
MIN_SCREENSHOT_MAX_PX = 320
MAX_SCREENSHOT_MAX_PX = 4096
SCREENSHOT_DIR_NAME = "kirocrew-computer-shots"
SCREENSHOT_FILE_PREFIX = "shot-"
SCREENSHOT_FILE_SUFFIX = ".jpeg"
SCREENSHOT_MIME = "image/jpeg"
# Ring-trim: the dir is a cache, not an archive. Bounded so a long session
# cannot fill the temp volume.
SCREENSHOT_KEEP = 200

# ── Accessibility attribute / role names ──
AX_ROLE = "AXRole"
AX_SUBROLE = "AXSubrole"
AX_TITLE = "AXTitle"
AX_VALUE = "AXValue"
AX_DESCRIPTION = "AXDescription"
AX_CHILDREN = "AXChildren"
AX_WINDOWS = "AXWindows"
# Alternate child collections. A table, outline or list frequently exposes its
# rows ONLY here and reports an empty (or scaffolding-only) ``AXChildren`` — so a
# children-only walk renders a spreadsheet or a file list as a container with
# nothing in it, which reads to the model as "this app has no content" rather than
# "you are looking through the wrong attribute".
AX_ROWS = "AXRows"
AX_VISIBLE_CHILDREN = "AXVisibleChildren"
#: Child attributes read for every node, in the order they are merged. Duplicates
#: across collections are dropped (a row is commonly in BOTH ``AXRows`` and
#: ``AXVisibleChildren``), so the extra reads add coverage, never duplicate nodes.
AX_CHILD_ATTRIBUTES: tuple[str, ...] = (AX_CHILDREN, AX_ROWS, AX_VISIBLE_CHILDREN)
#: Roles whose real content lives in ``AXRows``/``AXVisibleChildren``. For these,
#: the alternate collections are read FIRST so the rows keep the low indices a
#: model is most likely to address, rather than landing after a wall of
#: scaffolding.
ROW_BEARING_ROLES: frozenset[str] = frozenset({"AXTable", "AXOutline", "AXList", "AXBrowser"})
AX_ENABLED = "AXEnabled"
# Geometry. Both come back as ``AXValue`` boxes (a ``CGPoint`` and a ``CGSize``),
# never as plain numbers — see ``macos_ffi.ax_frame``.
AX_POSITION = "AXPosition"
AX_SIZE = "AXSize"
# Trait attributes: read as a TRI-STATE (``macos_ffi.ax_bool_opt``), because
# absent and False mean different things. ``AXSelected`` absent means "no notion
# of selection here"; ``AXSelected=False`` means "selectable, not selected".
AX_SELECTED = "AXSelected"
AX_EXPANDED = "AXExpanded"
# The focused element of the whole app subtree. Read ONCE per walk off the
# application element (not per node) — it is what turns "there are 40 text fields"
# into "the caret is in this one", which is the difference between typing into the
# right box and guessing.
AX_FOCUSED_UI_ELEMENT = "AXFocusedUIElement"
# The user's current text selection inside the focused element. Read once per
# walk, and NEVER for a secure element (a selected password is still a password).
AX_SELECTED_TEXT = "AXSelectedText"
AX_PRESS_ACTION = "AXPress"
# The press LADDER, tried in order when an element advertises no usable ``AXPress``.
# ``-25206`` (action unsupported) is the single most common click failure, and it is
# recoverable: a disclosure triangle answers ``AXConfirm``, a Finder row and a link
# answer ``AXOpen``, and a right-click target answers ``AXShowMenu`` and nothing
# else. Trying the siblings costs one AX round-trip each (~1ms) and converts a
# refusal — which the model can only answer by guessing coordinates — into a click.
AX_CONFIRM_ACTION = "AXConfirm"
AX_OPEN_ACTION = "AXOpen"
AX_SHOW_MENU_ACTION = "AXShowMenu"
#: Left-button press ladder, most-specific first.
AX_PRESS_LADDER: tuple[str, ...] = (AX_PRESS_ACTION, AX_CONFIRM_ACTION, AX_OPEN_ACTION)
#: A right click is a DIFFERENT gesture, not a press: only ``AXShowMenu`` expresses
#: it. Never fall back to ``AXPress`` for it — that would silently turn "open the
#: context menu" into "activate the control", which is the wrong action entirely.
AX_MENU_LADDER: tuple[str, ...] = (AX_SHOW_MENU_ACTION,)
AX_PARENT = "AXParent"
#: How many ancestors the click ladder walks up looking for a pressable container.
#: Web content routinely renders a clickable row as a plain unactionable text node
#: inside a pressable ancestor two or three levels up, so the whole row is dead to
#: an element click even though a coordinate click on it works. Bounded low: the
#: ancestor has to plausibly BE the thing the model meant, and by four levels up it
#: is more likely to be the page than the row — pressing that would activate
#: something the model never addressed.
MAX_ANCESTOR_PRESS_HOPS = 3
#: Largest ancestor-to-target area ratio still treated as "the same control".
#: The geometric guard on the ancestor fallback, and the thing that keeps it from
#: becoming a wrong-click generator: a container many times the size of the element
#: addressed is a layout wrapper (a scroll area, the page body), not the row, and
#: pressing it would activate an unrelated widget. A row wrapping a text node is
#: typically 1-6x its area, so this admits the real case and rejects the page.
MAX_ANCESTOR_AREA_RATIO = 8.0

# THE security-critical constant. A real macOS password box reports
# ``AXRole == "AXTextField"`` (innocuous) with ``AXSubrole ==
# "AXSecureTextField"`` and a READABLE ``AXValue`` — so a role-only check
# misses every password field. Both attributes are compared against this.
SECURE_SUBROLE = "AXSecureTextField"

# Structural containers that carry no information of their own. Elided from the
# rendered tree when they have no title/value, WITHOUT consuming an element
# index, so the numbering the model sees stays dense.
ELIDABLE_ROLES: frozenset[str] = frozenset({"AXGroup", "AXUnknown", "AXSplitGroup"})

# Trait words rendered in parentheses after an element's role. Deliberately plain
# English rather than the AX attribute names: ``editable`` is what a model acts on,
# ``AXValue is settable`` is an implementation detail of how we learned it.
TRAIT_SELECTED = "selected"
TRAIT_EXPANDED = "expanded"
TRAIT_EDITABLE = "editable"
#: Inline marker on the focused element's tree line.
FOCUS_MARKER = "<focused>"
#: Trailing summary lines. The focus line names the element the caret is in, which
#: is what a "type this" request means by "here"; the selection line reproduces
#: what the operator has highlighted, which no amount of tree walking recovers.
FOCUS_NOTE = "Focus: element {index} ({label})."
SELECTION_NOTE = "Selected text: [{text}]"
#: Window origin note. Element frames are window-LOCAL, so a coordinate click —
#: which takes SCREEN coordinates — needs this to convert. Without it the two
#: coordinate systems in one response are silently incompatible.
WINDOW_ORIGIN_NOTE = (
    "Window origin on screen: x={x},y={y} ({width}x{height}). "
    "Element frames above are relative to it — add the origin for a screen point."
)

# ── Rendering ──
# A secure field's value is NEVER rendered; this placeholder takes its place.
SECURE_PLACEHOLDER = "<secure>"
TREE_INDENT = "  "
TRUNCATED_NOTE = "[tree truncated at {count} nodes]"
DEPTH_NOTE = "[subtree elided below depth {depth}]"
SCREENSHOT_NOTE = (
    "Screenshot: {path}\n"
    "  ({width}x{height} jpeg, {size}) — read it with the fs_read tool only if "
    "the tree is insufficient."
)
SECURE_WINDOW_NOTE = (
    "Screenshot suppressed: this window contains a secure (password) field, so "
    "its pixels are not captured."
)
#: A truncated walk cannot prove the window holds no password field, so
#: ``capture_macos`` treats "unknown" as "present" and captures nothing. That
#: refusal used to be SILENT, which is the normal state for a browser (Chrome
#: measured 1475 nodes against a 1200 default) — so ``screenshot: true`` on
#: Chrome/Slack/VS Code returned no image and no reason, and the model retried in
#: exactly the loop the sibling notes exist to prevent. It names the remedy,
#: because raising the budget is something the model can actually do.
TRUNCATED_WINDOW_NOTE = (
    "Screenshot suppressed: the accessibility tree was truncated, so this window "
    "cannot be confirmed free of a secure (password) field. Re-run with a higher "
    "max_tree_nodes / max_tree_depth to capture it."
)
NO_APPS_NOTE = "No applications with on-screen windows were found."

# ── Permission probe states (ADVISORY ONLY — never a hard gate) ──
# macOS attributes a TCC grant to the RESPONSIBLE PARENT process, so a probe
# can report "missing" while a full-fidelity capture succeeds. The feature must
# never be gated on these; they are a Settings-UI hint.
PERMISSION_GRANTED = "granted"
PERMISSION_MISSING = "missing"
PERMISSION_UNKNOWN = "unknown"
PERMISSION_UNSUPPORTED = "unsupported"

# ── Refusal / error text ──
# Every tool result is TEXT ONLY (``validation.build_tool_response`` cannot
# express an image block), and an error is the literal string ``"Error: ..."``
# — ``mcp_shared.call_tool_with_logging`` classifies that prefix as a failed
# SEL outcome, so the prefix is load-bearing, not cosmetic.
ERROR_PREFIX = "Error: "
REFUSAL_UNSUPPORTED = "computer use is not supported on this platform ({platform}): {reason}"
REFUSAL_DISABLED = (
    "computer use is disabled. Enable it in the KiroCrew dashboard under "
    "Settings -> Computer Use; it cannot be enabled by an agent."
)
REFUSAL_UNATTENDED = (
    "computer use is not available to unattended sessions ({session}). It runs "
    "only in an interactive session where a human can see the approval prompt."
)
REFUSAL_DENIED_APP = "'{app}' is a blocked target for computer use ({reason})."
REFUSAL_SECURE_TARGET = (
    "refusing to send input to a secure (password) field: element {index} of "
    "'{app}' has subrole {subrole}."
)
REFUSAL_TEXT_SENSITIVE = "refusing to type this text into '{app}': {reason}"
REFUSAL_GOVERNANCE = "computer use is not permitted by the active security policy: {reason}"

# ── Refusals ──
# The real-pointer path, refused when an IN-PROCESS caller passed
# ``pointer_enabled=False`` to ``gate.require_pointer_move``. There is no operator
# opt-in behind it any more — one enable covers the feature — so this is reachable
# only from a direct caller that chose to refuse locally, never from the dispatch
# chokepoint. Kept because that local opt-out is still a supported thing to do.
REFUSAL_POINTER_NOT_ENABLED = (
    "Blocked: moving the real mouse pointer is switched off for this caller. Use "
    "click_method 'accessibility' or 'app_post' instead — neither moves the pointer."
)
# ``click_method: accessibility`` with no ``element_index``. AXPress addresses an
# element; there is no coordinate form of it.
REFUSAL_ACCESSIBILITY_NEEDS_INDEX = (
    "click_method 'accessibility' presses a specific control, so it needs an "
    "element_index. Pass one, or use click_method 'app_post' to click a point."
)
# Exactly one of (element_index | x+y). Validated rather than guessed: picking
# one silently would make a model that supplied both act on a target it did not
# unambiguously name, and picking neither has no meaning at all.
REFUSAL_CLICK_TARGET_AMBIGUOUS = (
    "give either element_index or both x and y, not both forms — they name "
    "different targets and there is no rule for which should win"
)
REFUSAL_CLICK_TARGET_MISSING = (
    "give either element_index (from computer_get_state) or both x and y screen " "coordinates"
)
# Every pointer-moving action is audited by METHOD, on the allow path as well as
# the deny path: the whole point of gating this one path separately is that the
# operator can later answer "did the agent ever take control of my mouse?".
AUDIT_POINTER_ITEM = "click_method={method}"
# ``tool_name`` prefix for every computer-use SEL record, so the audit trail can
# be filtered to this feature with one glob.
AUDIT_TOOL_PREFIX = "computer_use:"
ERR_NO_STATE = "no state for '{app}'. Call {tool} first."
ERR_STALE_STATE = "state for '{app}' is {age}s old. Call {tool} again."
ERR_UNKNOWN_INDEX = "element_index {index} is not in the last state for '{app}' ({count} elements)."
ERR_INDEX_DRIFT = (
    "element_index {index} changed since the last {tool} (was {before}, now "
    "{after}). Call {tool} again."
)
ERR_APP_NOT_FOUND = (
    "no application with an on-screen window matches '{query}'. Call {tool} to "
    "see what is available."
)
ERR_UNKNOWN_KEY = "unknown key '{key}'."
ERR_ACTION_FAILED = "{action} on element {index} of '{app}' failed ({detail})."
ERR_UNKNOWN_CLICK_METHOD = "unknown click_method '{method}'."
# The real-pointer confinement refusal. A ``global`` click/drag is delivered by
# warping the cursor and posting to the system, which carries no pid — so the
# point must be verified to belong to the app the permits were granted for, or an
# allowed app plus coordinates over a denied one would click the denied one.
# Actionable on purpose: naming the app-scoped alternatives is what lets the model
# recover without the operator having to widen anything.
ERR_POINT_NOT_OWNED = (
    "refusing a real-pointer action at ({x}, {y}): that point is not inside the "
    "'{app}' window, and a pointer-moving click lands on whatever application is "
    "there. Use click_method 'app_post' (or 'accessibility' with an element_index), "
    "which deliver to '{app}' directly."
)
ERR_UNKNOWN_MOUSE_BUTTON = "unknown mouse_button '{button}'."
# ``app_post`` / ``global`` need a point. ``auto`` resolving to ``app_post``
# already has one by construction (that branch is only taken when x/y were
# given), so this only fires for an explicitly-named method.
ERR_POINT_REQUIRED = "click_method '{method}' clicks a screen point, so it needs both x and y."
# ``sky_click`` is a LEFT-button recipe. Its private event sequence was
# reverse-engineered for a left click and the button number is one field among nine,
# so there is no evidence the rest of the recipe is button-agnostic — a right-click
# variant would be invented, not observed. Refused rather than downgraded for the
# same reason ``AX_MENU_LADDER`` never falls back to a press: silently turning "open
# the context menu" into "activate the control" performs a DIFFERENT gesture than
# the one requested, can destroy data, and here it happens on a background window
# the operator cannot see. Enforced at the chokepoint by
# ``policy.check_method_button``, re-checked inside ``macos_skylight``.
ERR_SKY_CLICK_BUTTON = (
    "click_method 'sky_click' supports only the left mouse button (got '{button}'). "
    "Use click_method 'app_post' for a '{button}' click — it delivers to the same "
    "application through a public API."
)

# ── Cursor Motion: the path model (``computer_use.cursor_motion``) ──
# A PURELY COSMETIC fake-cursor animation. It exists so a human watching the
# screen can see WHERE the agent is acting, which is the only affordance that
# makes an unattended desktop action reviewable in real time. It is never on the
# path of a tool's success: every number below shapes pixels and nothing else.
#
# The path is one cubic Bezier from start to end, bowed sideways by a
# perpendicular arc. The arc amount reproduces the reference implementation's
# clamp exactly — ``clamp(distance * 0.22, 28, 110) * curve_scale`` — because the
# feel of the motion is the whole point and those three numbers ARE the feel:
# proportional for a mid-length move, floored so a 20px nudge still visibly
# curves, ceilinged so a cross-screen sweep does not balloon into a semicircle.
CURVE_DISTANCE_RATIO = 0.22
CURVE_AMOUNT_MIN = 28.0
CURVE_AMOUNT_MAX = 110.0
DEFAULT_CURVE_SCALE = 1.0
MAX_CURVE_SCALE = 4.0
# Control-point placement along the chord. The curved form front-loads the first
# handle (0.18/0.10) and pushes the second one nearly to the end (0.80/0.96),
# which is what makes the cursor leave fast and arrive settling rather than
# sweeping a symmetric bow. ``curve_scale == 0`` degenerates to the classic
# thirds placement, i.e. an exactly straight line.
CURVE_C1_ALONG = 0.18
CURVE_C1_ACROSS = 0.10
CURVE_C2_ALONG = 0.80
CURVE_C2_ACROSS = 0.96
STRAIGHT_C1_FRACTION = 1.0 / 3.0
STRAIGHT_C2_FRACTION = 2.0 / 3.0
# The second handle gets less than half the arc offset of the first: the bow
# decays toward the target so the final approach is nearly along the chord.
CURVE_C2_OFFSET_RATIO = 0.48
# Degenerate-input floor. A zero-length delta has no direction and no normal, so
# a naive normalize would divide by zero and every sample would be NaN.
MOTION_EPSILON = 1e-9
MIN_MOTION_DISTANCE = 1.0

# ── Cursor Motion: the settle spring ──
# A critically-ish damped spring on PROGRESS (not on position): progress is
# integrated 0 -> 1 by the spring and then fed through the Bezier, so the cursor
# eases in, coasts, and settles without overshooting its target POSITION even
# though the progress value itself may exceed 1 by a hair.
#
# ``response``/``damping`` are the reference's published pair; stiffness and drag
# are DERIVED (``(2*pi/response)**2``, ``2*damping*sqrt(stiffness)``) rather than
# hardcoded so the two knobs stay the only tunables. The velocity-Verlet
# integrator runs at a fixed 1/240s so a slow frame cannot change the shape of
# the motion — it only changes how many samples are drawn.
SPRING_RESPONSE = 1.4
SPRING_DAMPING_FRACTION = 0.9
SPRING_DT = 1.0 / 240.0
# Ceiling on derived stiffness, so a hand-passed ``response`` near zero cannot
# produce an effectively infinite spring constant (and a NaN on the first step).
SPRING_MAX_STIFFNESS = 28800.0
# Settle test: progress must have REACHED the target and be within this distance
# of it. Both halves matter — the first rejects the rising edge, the second the
# overshoot ring-down. Measured settle time for the constants above is ~1.429s.
SPRING_SETTLE_DISTANCE = 0.01
# Hard bound on the integrator loop, so a pathological configuration terminates.
# 4096 steps at 1/240s is ~17s of simulated time, an order of magnitude past the
# real settle point.
SPRING_MAX_STEPS = 4096
SPRING_FALLBACK_SETTLE_SECS = 1.43

# ── Cursor Motion: sampling ──
# The path is handed to the overlay as a fixed list of points, not as a live
# callback: the overlay is a separate process, so a pre-sampled path means the
# supervisor sends ONE message and the overlay never has to evaluate a Bezier.
DEFAULT_PATH_SAMPLES = 96
MIN_PATH_SAMPLES = 2
MAX_PATH_SAMPLES = 600
# Total travel time. Derived from the spring's own settle time so the visual
# duration and the easing curve cannot drift apart, then clamped: a sub-100ms
# move reads as a teleport, and nothing cosmetic may hold a caller for a second
# and a half beyond the settle point.
MIN_MOVE_DURATION_MS = 100
MAX_MOVE_DURATION_MS = 2000
#: Distance (px) at or above which a move gets the spring's FULL settle time.
#: Below it the duration scales down linearly, because the spring's settle point
#: is distance-INDEPENDENT: without this a 1px nudge animated for the same ~1.4s
#: as a 600px sweep, which reads as a hang rather than as motion. Scaling by
#: distance is what makes a short hop look short.
FULL_SPEED_DISTANCE = 400.0
#: Below this distance the arc is dropped entirely (a straight, brief hop). The
#: arc floor is 28px, so a 1-3px nudge would otherwise loop ~28px out and back —
#: a visible curlicue for a move the eye reads as "it barely went anywhere".
STRAIGHT_MOVE_DISTANCE = 24.0
# Click pulse: one sine half-period per click, matching the reference's 0.16s.
CLICK_PULSE_MS = 160
CLICK_PULSE_GAP_MS = 50
MAX_CLICK_COUNT = 3
# Alpha floor at the peak of a click pulse (1.0 - depth). A dip rather than a
# scale change: scaling the window would re-place the tip anchor every frame.
CLICK_PULSE_DEPTH = 0.55
#: Largest share of a pulse one frame may advance. The dip IS the click, and it
#: is drawn as ``sin(progress * pi)`` -- which is 0 at BOTH progress 0.0 and 1.0.
#: So a frame gap wide enough to jump the whole pulse (a loaded machine, a GC
#: pause, a descheduled thread -- routinely a full pulse on a busy CI runner)
#: renders alpha 1.0 twice and no dip at all: the click silently does not appear.
#: Capping the per-frame advance guarantees at least one sample near the peak,
#: i.e. at least ``ceil(1 / step)`` frames per pulse.
CLICK_PULSE_MAX_STEP = 0.25

# ── Overlay process (``computer_use.overlay`` / ``overlay_proc``) ──
# The overlay MUST be out of process: AppKit needs a main-thread run loop and the
# gateway's main thread is the asyncio loop. See ``overlay_proc``'s docstring.
OVERLAY_MODULE = "kiro_crew.computer_use.overlay_proc"
# NDJSON on stdin, one command per line. Kept minimal on purpose — the overlay is
# a dumb renderer and every decision (which path, how long, whether at all) is
# made in the gateway where it can be tested.
OVERLAY_CMD_KEY = "type"
OVERLAY_CMD_MOVE = "move"
OVERLAY_CMD_CLICK = "click"
OVERLAY_CMD_HIDE = "hide"
OVERLAY_CMD_QUIT = "quit"
OVERLAY_KEY_POINTS = "points"
OVERLAY_KEY_X = "x"
OVERLAY_KEY_Y = "y"
OVERLAY_KEY_MS = "ms"
OVERLAY_KEY_COUNT = "count"
# Printed on stdout once the window exists, so the supervisor can tell "spawned"
# from "actually rendering" without polling AppKit from another process.
OVERLAY_READY_LINE = "KIROCREW_OVERLAY_READY"
# The overlay auto-hides itself this long after the last command, so a crashed
# gateway that never sends ``hide`` cannot leave a cursor parked on the user's
# screen. The stdin-EOF exit is the primary guarantee; this is the backstop for
# the case where the pipe stays open but nobody is writing.
OVERLAY_IDLE_HIDE_SECS = 30.0
# Run-loop slice per pumped frame. ~4ms gives >200fps headroom (measured 270fps)
# while still yielding to AppKit so the window actually composites.
OVERLAY_FRAME_SLICE_SECS = 0.004
# Idle poll slice when there is nothing to animate: long enough that the process
# costs no measurable CPU, short enough that a queued command starts within a
# frame or two.
OVERLAY_IDLE_SLICE_SECS = 0.05
# Bounds on how long the supervisor waits for the child, and how long a single
# animation may hold the overlay. Both exist so a wedged AppKit cannot make a
# cosmetic subsystem hold anything.
OVERLAY_SPAWN_TIMEOUT_SECS = 5.0
OVERLAY_STOP_TIMEOUT_SECS = 2.0
# Supervisor back-off: after this many consecutive spawn/write failures the
# supervisor stops trying for the life of the process. A cursor that cannot be
# drawn is cosmetic; a respawn loop against a broken AppKit is not.
OVERLAY_MAX_FAILURES = 3
# NSWindow / NSApplication ABI constants used by the overlay process. Stable
# published values (mirroring how ``macos_ffi`` keeps its CF/CG constants beside
# the FFI they describe) — but they live HERE because they are configuration of
# the overlay's window, and ``overlay_proc`` must be readable as "policy from
# types, ctypes plumbing local".
NS_WINDOW_STYLE_BORDERLESS = 0
NS_BACKING_STORE_BUFFERED = 2
# NSStatusWindowLevel: above normal windows and above floating panels, below the
# screen saver. The agent's cursor must be visible over whatever it is driving.
NS_STATUS_WINDOW_LEVEL = 25
# NSWindowSharingNone. THE load-bearing one: with it the overlay is invisible to
# screencapture/CGWindowList, so the fake cursor never pollutes the screenshots
# the agent itself takes (A/B verified live — visible without it, invisible with
# it). Never change this to 1 "so the user can screen-share the cursor": that
# would feed the agent's own decoration back into its observations.
NS_WINDOW_SHARING_NONE = 0
# NSApplicationActivationPolicyAccessory: no Dock icon, no menu bar, and the
# overlay never steals focus from the app being driven.
NS_ACTIVATION_POLICY_ACCESSORY = 1
# canJoinAllSpaces | stationary | ignoresCycle — the cursor follows the user
# across Spaces, does not slide with a Space switch, and never appears in
# Cmd-Tab.
NS_COLLECTION_BEHAVIOR = 1 | 16 | 64
# NSImageScaleProportionallyUpOrDown, for the NSImageView holding the glyph.
NS_IMAGE_SCALE_PROPORTIONAL = 3
# Fallback glyph box when ``NSCursor arrowCursor`` cannot be measured (it
# reported 28x40 with a (5,5) hot spot on the probe machine). Only used so the
# window still has a sane size; a missing glyph degrades to an empty window
# rather than a crash.
CURSOR_GLYPH_WIDTH = 28.0
CURSOR_GLYPH_HEIGHT = 40.0
CURSOR_HOTSPOT_X = 5.0
CURSOR_HOTSPOT_Y = 5.0
# Screen fallback when no display can be measured. Used only to clamp a point
# into something finite; the overlay is cosmetic so an approximate clamp on an
# exotic display setup is strictly better than refusing to draw.
FALLBACK_SCREEN_WIDTH = 1920.0
FALLBACK_SCREEN_HEIGHT = 1080.0


# ── Exceptions ──


class PolicyStateError(ValueError):
    """A present policy field is malformed in a way that would FAIL OPEN.

    Raised only for fields whose empty value means "unrestricted", so a typo cannot
    quietly convert a restriction into a grant. The dispatcher turns this into a
    refusal naming the file, which is actionable for the operator and discloses
    nothing to the model.
    """


class ComputerUseError(Exception):
    """Base class for every computer-use failure.

    Platform drivers raise these; the backend seam converts them into a
    ``DriverResult(ok=False, ...)`` so no exception crosses the MCP dispatch
    boundary and a driver failure can never kill the sidecar's worker thread.
    """


class ComputerUseUnsupported(ComputerUseError):
    """The current platform (or process) cannot drive computer use at all."""


class ComputerUseDenied(ComputerUseError):
    """A policy, governance ceiling, or the primary enable refused the call."""


class KeyParseError(ComputerUseError):
    """A ``press_key`` spec named an unknown key or modifier."""


class StaleIndex(ComputerUseError):
    """A cached snapshot is missing, expired, or its element indices drifted."""


# ── Frozen data containers ──


@dataclass(frozen=True)
class AppRef:
    """An application resolved from the on-screen window list.

    Resolution is ALWAYS via the window list, never ``pgrep``: a ``pgrep -n``
    match returns short-lived helper processes (a Chrome/Slack helper answered
    ``kAXErrorCannotComplete`` to every attribute read while the real browser
    had a different pid). The pid that owns a visible, layer-0 window is the
    one whose accessibility tree is populated.
    """

    name: str
    pid: int
    bundle_id: str = ""
    window_id: int = 0
    window_title: str = ""

    @property
    def key(self) -> str:
        """Stable APP-IDENTITY key: bundle id when known, else process name.

        Deliberately window-agnostic — it answers "which application is this?", which
        is the right granularity for the denylist and the operator's allow/deny
        patterns (an operator who blocks Terminal means every Terminal window). It is
        NOT the snapshot cache key: see :attr:`window_key`.
        """
        return (self.bundle_id or self.name).strip().lower()

    @property
    def window_key(self) -> str:
        """Snapshot cache key: the app identity PLUS the specific window.

        Element indices address one WINDOW's accessibility tree, so keying the cache
        by application alone aliased distinct windows of the same app: snapshot
        document A, focus document B, and the follow-up action —
        which re-resolves to B — retrieved A's cached tree. The fingerprint check
        cannot catch that, because two documents of the same app routinely have
        identically-shaped toolbars, so ``role|subrole|title`` at a given index
        matches and the action mutates the wrong document.

        ``pid`` is included as well as ``window_id``: the window id is only unique
        within a session, and a relaunched app can reuse one. Together they cannot
        alias two live windows.
        """
        return f"{self.key}#{self.pid}#{self.window_id}"

    @property
    def label(self) -> str:
        """Human/model-facing identity, e.g. ``com.apple.finder (pid 1041)``."""
        return f"{self.bundle_id or self.name} (pid {self.pid})"


@dataclass(frozen=True)
class ElementRec:
    """One node of a rendered accessibility tree, addressed by ``index``.

    ``subrole`` and ``secure`` are MANDATORY fields, not conveniences: macOS
    reports a password box as ``role='AXTextField'`` with
    ``subrole='AXSecureTextField'`` and a readable value, so ``secure`` is the
    only reliable signal that a value must never be rendered and that input
    must be refused. ``secure`` is set by the driver from BOTH attributes.
    """

    index: int
    role: str
    subrole: str = ""
    title: str = ""
    value: str = ""
    actions: tuple[str, ...] = ()
    depth: int = 0
    secure: bool = False
    enabled: bool = True
    #: ``(x, y, width, height)`` in WINDOW-LOCAL coordinates, or ``None`` when the
    #: element exposes no geometry (ordinary — an off-screen row, a background app's
    #: menu item) or the window's own rect was unavailable.
    #:
    #: Window-LOCAL, not screen: the screenshot the model may also be looking at is
    #: a crop of this window, so a screen-absolute rect could not be related to
    #: anything the model can see. It also makes the number stable when the user
    #: drags the window between two turns.
    frame: "tuple[float, float, float, float] | None" = None
    #: Rendered trait words (``selected``, ``expanded``, ``editable``). Derived at
    #: walk time from tri-state AX reads; an empty tuple means "no traits observed",
    #: never "traits unknown".
    traits: tuple[str, ...] = ()
    #: True when this record is the app's focused element (``AXFocusedUIElement``).
    #: At most one record per snapshot carries it.
    focused: bool = False

    @property
    def short_role(self) -> str:
        """Compact role for rendering: ``AXButton`` -> ``button``."""
        role = self.role[2:] if self.role.startswith("AX") else self.role
        return role.lower() if role else "?"


@dataclass(frozen=True)
class Snapshot:
    """One accessibility walk of one application window, plus optional pixels.

    ``captured_at`` is a ``time.monotonic()`` reading — never wall clock, so a
    clock adjustment cannot make a stale snapshot look fresh (or a fresh one
    look expired).

    ``image_jpeg`` holds the encoded bytes exactly as the backend produced
    them; ``image_path`` is set once those bytes have been persisted to the
    screenshot dir. A backend MAY return bytes without a path (the persisting
    layer then fills it in via ``dataclasses.replace``), and only ``image_path``
    is ever relayed to the model — the bytes themselves never reach a result.
    """

    app: AppRef
    elements: tuple[ElementRec, ...] = ()
    window_title: str = ""
    captured_at: float = 0.0
    truncated: bool = False
    depth_truncated: bool = False
    has_secure: bool = False
    image_jpeg: bytes = b""
    image_path: str = ""
    image_width: int = 0
    image_height: int = 0
    #: The target window's screen rect ``(x, y, width, height)``, top-left origin —
    #: the origin every :attr:`ElementRec.frame` is expressed relative to. ``None``
    #: when the window list carried no usable bounds, in which case element frames
    #: are ``None`` too rather than silently screen-absolute.
    window_bounds: "tuple[float, float, float, float] | None" = None
    #: The user's current text selection inside the focused element, already
    #: sanitized and clipped. Empty when there is no selection, when the focused
    #: element is secure, or when nothing is focused.
    selected_text: str = ""
    #: The tree budget this snapshot was actually WALKED at, stamped by
    #: ``service.snapshot``. The drift-verification re-walk reads it so it re-walks
    #: the same tree the model was shown: verifying a snapshot taken at
    #: ``max_tree_nodes=2001`` against the 1200 config default made every element
    #: above 1200 permanently un-actionable ("changed since the last
    #: computer_get_state … now no element at that index"), and re-snapshotting
    #: reproduced the same refusal — a loop with no way out on a documented happy
    #: path (the schema advertises up to 5000 and the Settings copy says to raise it
    #: for dense apps). ``None`` for a snapshot a backend built directly, in which
    #: case the caller's own request is used.
    walk_budget: "SnapshotRequest | None" = None

    @property
    def key(self) -> str:
        """Index key — the WINDOW this snapshot's element indices belong to.

        ``AppRef.window_key``, not ``AppRef.key``: the indices are meaningless
        against a different window of the same application.
        """
        return self.app.window_key


@dataclass(frozen=True)
class DriverResult:
    """The uniform return of every :class:`~backend.ComputerUseBackend` method.

    A single result type (rather than per-method returns plus exceptions) is
    what lets ``UnsupportedBackend`` answer every method with the same typed
    refusal, and what keeps driver failures from propagating as exceptions into
    the MCP stdio loop. ``text`` is already-final prose: on ``ok=False`` it is
    the refusal/error reason WITHOUT the ``Error: `` prefix (the dispatch layer
    adds it once, so the prefix can never be doubled or forgotten).
    """

    ok: bool
    text: str = ""
    apps: tuple[AppRef, ...] = ()
    app: AppRef | None = None
    snapshot: Snapshot | None = None


@dataclass(frozen=True)
class BackendStatus:
    """Whether this backend can drive computer use here, and why not if it can't."""

    supported: bool
    platform_id: str
    reason: str = ""


@dataclass(frozen=True)
class PermissionProbe:
    """ADVISORY macOS permission hints for the Settings UI.

    Never a gate. ``responsible_hint`` names the process a user should actually
    grant, because macOS attributes a TCC grant to the responsible parent of
    the process tree (the packaged app, or the terminal that launched a dev
    gateway) rather than to the sidecar that asks.
    """

    accessibility: str = PERMISSION_UNKNOWN
    screen_recording: str = PERMISSION_UNKNOWN
    responsible_hint: str = ""


@dataclass(frozen=True)
class PolicyConfig:
    """The operator-controlled target policy, read from the keystone state file.

    ``allowed_apps`` is an optional allow-list (empty = every app that is not
    denied). ``extra_denied_apps`` can only ADD to the built-in denylist —
    there is deliberately no mechanism to remove a built-in entry, so the
    floor in ``policy.py`` cannot be edited away from the dashboard.
    """

    allowed_apps: tuple[str, ...] = ()
    extra_denied_apps: tuple[str, ...] = ()

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> "PolicyConfig":
        """Build from a (possibly hand-edited) state mapping — never raises."""
        return cls(
            allowed_apps=_str_tuple(state.get(STATE_KEY_ALLOWED_APPS), strict=True),
            extra_denied_apps=_str_tuple(state.get(STATE_KEY_EXTRA_DENIED_APPS)),
        )


@dataclass(frozen=True)
class SnapshotRequest:
    """The budgets one ``computer_get_state`` walk must respect."""

    max_nodes: int = DEFAULT_MAX_TREE_NODES
    max_depth: int = DEFAULT_MAX_TREE_DEPTH
    text_limit: int = DEFAULT_TEXT_LIMIT
    want_image: bool = DEFAULT_ATTACH_SCREENSHOT
    image_max_px: int = DEFAULT_SCREENSHOT_MAX_PX
    image_quality: int = DEFAULT_SCREENSHOT_JPEG_QUALITY


@dataclass(frozen=True)
class ClickRequest:
    """One resolved click: HOW, WHERE, which button, how many times.

    A single frozen record rather than five positional driver arguments, for two
    reasons that both bit during design:

    * the method is RESOLVED before the driver is called (``auto`` never reaches
      a backend — :func:`resolve_click_method` turns it into a concrete method at
      the dispatch chokepoint), so the driver cannot accidentally re-decide it and
      pick the pointer-warping path;
    * ``point`` is ``None`` for the element-addressed form, which is what makes
      "exactly one of (element_index | x+y)" a shape the type system carries
      rather than a convention every backend re-checks.

    ``point`` is in TOP-LEFT screen coordinates, the convention the whole package
    uses. The bottom-left flip AppKit needs happens only in ``overlay_proc``.
    """

    method: str = CLICK_METHOD_ACCESSIBILITY
    point: "tuple[float, float] | None" = None
    button: str = DEFAULT_MOUSE_BUTTON
    count: int = DEFAULT_CLICK_COUNT

    @property
    def moves_pointer(self) -> bool:
        """True when this click warps the operator's physical cursor.

        The single predicate every gate, audit and refusal site reads, so "which
        method moves the pointer" is decided in exactly one place.
        """
        return self.method in POINTER_MOVING_METHODS


@dataclass(frozen=True)
class DragRequest:
    """One drag: a start point, an end point and a button, all resolved.

    Coordinate-only by construction — there is no element form of a drag, because
    a drag's meaning IS the path between two points (a canvas stroke, a slider
    sweep, a range selection) and no accessibility action expresses it.

    Both points are TOP-LEFT screen coordinates. ``moves_pointer`` mirrors
    :attr:`ClickRequest.moves_pointer` so the gate can treat the two uniformly.
    """

    start: tuple[float, float]
    end: tuple[float, float]
    method: str = CLICK_METHOD_APP_POST
    button: str = DEFAULT_MOUSE_BUTTON

    @property
    def moves_pointer(self) -> bool:
        """True when this drag warps the operator's physical cursor."""
        return self.method in POINTER_MOVING_METHODS


@dataclass(frozen=True)
class DeniedApp:
    """One entry of the built-in target denylist, with its category + rationale.

    The rationale travels with the rule so the Settings panel and the refusal
    text can both explain WHY a target is blocked without duplicating prose.
    """

    category: str
    reason: str
    bundle_prefixes: tuple[str, ...] = field(default_factory=tuple)
    name_substrings: tuple[str, ...] = field(default_factory=tuple)
    # Matched against the resolved WINDOW TITLE, not the app identity. Needed
    # because a host application can display someone else's UI: KiroCrew's own
    # dashboard served in a browser tab has Chrome's bundle id and Chrome's process
    # name, so a bundle/name rule cannot see it. Only ever used
    # to ADD a refusal — a title match can never lift a bundle match.
    title_substrings: tuple[str, ...] = field(default_factory=tuple)


def _str_tuple(raw: Any, *, strict: bool = False) -> tuple[str, ...]:
    """Coerce hand-edited JSON into a tuple of non-empty lowercase strings.

    Defensive on purpose: the state file is operator-edited, so a scalar, a
    ``null``, or a list containing dicts must degrade to a usable value rather
    than raise inside a security check.

    ``strict`` inverts the disposition for a field where the empty tuple MEANS
    "unrestricted". Returning ``()`` for a malformed ``allowed_apps`` (say the
    operator wrote ``"Preview"`` instead of ``["Preview"]``) silently converts a
    restriction into no restriction at all — a typo that widens the ceiling. In
    strict mode a present-but-malformed value raises :class:`PolicyStateError`
    instead, which the caller turns into a refusal: an operator who tried to
    restrict something gets an error, never an accidental grant. An ABSENT value is
    still fine in both modes — that is the documented "no allow-list" case.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        if strict:
            raise PolicyStateError(
                "allowed_apps must be a JSON list of strings; a malformed value "
                "would silently remove the restriction it was meant to express"
            )
        return ()
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip().lower())
        elif strict:
            # A LIST whose items are malformed is the same widening bug as a
            # non-list, and the list check above does not catch it:
            # ``[{"name": "Preview"}]`` is a list, every item is dropped, and the
            # result is the empty tuple that MEANS "unrestricted". So strict mode
            # has to refuse per ITEM, not just per container.
            raise PolicyStateError(
                "every allowed_apps entry must be a non-empty string; a malformed "
                "entry would silently remove the restriction it was meant to express"
            )
    return tuple(out)
