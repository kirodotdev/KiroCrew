"""The ``kirocrew-computer`` server's tool schemas and its registry, ``MCP_COMPUTER_SCHEMAS``.

Callers import these names from :mod:`kiro_crew.validation`, which serves them; this
module never imports that facade.
"""

from __future__ import annotations

import re

# Computer-use tool names and their argument bounds. Safe to import at module
# scope: ``computer_use.types`` is deliberately dependency-free (it imports
# nothing from ``kiro_crew`` and never touches ctypes), so there is no cycle and
# no native library is loaded on any platform. Aliased so the schema block below
# reads as "the computer-use vocabulary" rather than bare names.
from kiro_crew.computer_use import types as _cu_types
from kiro_crew.constants import MAX_SHORT_STRING
from kiro_crew.validation_parts.field_engine import FieldSpec, ToolSchema

# ── Tool Schemas (MCP Computer Use — server ``kirocrew-computer``) ──
#
# EVERY computer-use tool MUST have an entry here, including the ones with no
# arguments. Two independent reasons, both load-bearing:
#
#   1. A tool absent from its schema map passes its arguments RAW through the
#      server's ``_validate_args`` (the ``if schema:`` fallback the cron/core
#      servers use), so an unvalidated dict would reach a handler that can
#      synthesize keystrokes into another application's window.
#   2. The dispatcher refuses any tool name not present here, which makes this
#      dict the registration surface: a tenth tool added without a row is
#      rejected rather than silently ungoverned.
#
# Bounds come from ``computer_use.types`` rather than being re-spelled, so the
# schema ceiling and the driver's own budgets cannot drift. ``element_index`` is
# an ``int`` FieldSpec, which ``validate_field`` guards against a bool
# masquerading as an int (``True == 1`` would otherwise pass a range check and
# address element 1).
_CU_SCROLL_DIRECTIONS = frozenset(_cu_types.SCROLL_DIRECTIONS)
# Key specs and accessibility action names are opaque single tokens. Anchored and
# whitespace-free so a spec cannot smuggle a newline into a rendered result, and
# deliberately NOT an enum: ``keymap.parse_key`` owns the key vocabulary (a
# regex enum here would have to be kept in sync with it and would fail closed on
# a legitimate alias), and accessibility action names are app-defined.
_CU_KEY_SPEC_RE = re.compile(r"^[A-Za-z0-9+_.,;:'\"`~!@#$%^&*()\[\]{}<>?/\\|=-]+$")
_CU_AX_ACTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# ``app`` is the agent's REQUEST for a target, matched case-insensitively against
# the on-screen window list. It is never the authorization identity — the gate is
# queried with what the driver RESOLVED — so it only needs to be a bounded,
# single-line string.
_CU_APP_FIELD = FieldSpec("app", str, required=True, max_len=MAX_SHORT_STRING)
_CU_ELEMENT_FIELD = FieldSpec(
    "element_index", int, required=True, min_val=0, max_val=_cu_types.MAX_ELEMENT_INDEX
)
# ``computer_click``'s element index is OPTIONAL because the tool also accepts a
# coordinate target. "Exactly one of (element_index | x+y)" is a CROSS-FIELD rule
# and ``validate_tool_args`` validates fields independently, so it is enforced at
# the dispatch chokepoint by ``policy.check_click_target`` — which the in-process
# entry point also traverses. Keeping it out of the schema is deliberate: a
# ``custom_validator`` here would duplicate the rule in a second place that the
# gateway-side caller could still bypass.
_CU_OPTIONAL_ELEMENT_FIELD = FieldSpec(
    "element_index", int, min_val=0, max_val=_cu_types.MAX_ELEMENT_INDEX
)
_CU_CLICK_METHODS = frozenset(_cu_types.CLICK_METHODS)
_CU_MOUSE_BUTTONS = frozenset(_cu_types.MOUSE_BUTTONS)
_CU_DRAG_PATHS = frozenset(_cu_types.DRAG_PATHS)
# ``computer_launch_app``'s target is a NAME, and this field is where that is
# enforced on the wire. Deliberately the same bounded single-line string shape as
# ``app`` — there is no path field and no argument field, because a launch that
# accepted either would be "run an arbitrary program with attacker-chosen input"
# rather than "open an application". The drivers narrow it much further (a resolved
# executable must sit under a protected install root); this is only the outer bound.
_CU_LAUNCH_APP_FIELD = FieldSpec("app", str, required=True, max_len=_cu_types.MAX_LAUNCH_QUERY_LEN)


def _cu_coord_field(name: str, *, required: bool = False) -> FieldSpec:
    """A screen-coordinate field, bounded so nonsense never reaches the FFI.

    ``(int, float)`` rather than ``float``: JSON has one number type and a model
    writing ``x: 400`` produces an ``int``, which a ``float``-only spec would
    reject for no reason. ``bool`` is still refused — it is an ``int`` subclass, and
    ``validate_field`` rejects it unless ``bool`` is explicitly in the tuple.

    The bound is a fixed generous range rather than a display-derived one: a
    multi-display desktop legitimately extends far past one screen, and the OS
    clamps a delivered event anyway.
    """
    return FieldSpec(
        name,
        (int, float),
        required=required,
        min_val=_cu_types.MIN_SCREEN_COORD,
        max_val=_cu_types.MAX_SCREEN_COORD,
    )


MCP_COMPUTER_SCHEMAS: dict[str, ToolSchema] = {
    _cu_types.TOOL_LIST_APPS: ToolSchema(tool_name=_cu_types.TOOL_LIST_APPS, fields=[]),
    _cu_types.TOOL_LAUNCH_APP: ToolSchema(
        tool_name=_cu_types.TOOL_LAUNCH_APP,
        # ONE field, and the absence of the others is the point. No ``path``, no
        # ``args``, no ``url``: the target is a name resolved against the OS's own
        # catalog of installed applications, and each of those fields would turn this
        # into a way to run an arbitrary program with attacker-chosen input — which,
        # because computer use is deliberately not governance-gated, would also skip
        # the ``BUILTIN_DENIED_RULES`` floor every ``bash`` call passes.
        fields=[_CU_LAUNCH_APP_FIELD],
    ),
    _cu_types.TOOL_GET_STATE: ToolSchema(
        tool_name=_cu_types.TOOL_GET_STATE,
        fields=[
            _CU_APP_FIELD,
            FieldSpec("text_limit", int, min_val=1, max_val=_cu_types.MAX_TEXT_LIMIT),
            FieldSpec("max_tree_nodes", int, min_val=1, max_val=_cu_types.MAX_TREE_NODES_LIMIT),
            FieldSpec("max_tree_depth", int, min_val=1, max_val=_cu_types.MAX_TREE_DEPTH_LIMIT),
            # No default: absent means "use the operator's config", which is
            # resolved in ``service.snapshot_request``. A default here would
            # override the operator's ``attach_screenshot: false``.
            FieldSpec("screenshot", bool),
        ],
    ),
    _cu_types.TOOL_CLICK: ToolSchema(
        tool_name=_cu_types.TOOL_CLICK,
        fields=[
            _CU_APP_FIELD,
            _CU_OPTIONAL_ELEMENT_FIELD,
            _cu_coord_field("x"),
            _cu_coord_field("y"),
            # No default on ``click_count``: absent means "one click", which the
            # dispatcher supplies from ``DEFAULT_CLICK_COUNT``. Bounded at 3 because
            # macOS itself only reports up to a triple click as a distinct gesture.
            FieldSpec(
                "click_count",
                int,
                min_val=_cu_types.MIN_CLICK_COUNT,
                max_val=_cu_types.MAX_CLICK_COUNT,
            ),
            # Closed enums, not free strings: an unknown value must be refused
            # before any event is synthesized, because a substituted button or
            # method performs a DIFFERENT gesture in a live application.
            FieldSpec("mouse_button", str, max_len=8, allowed=_CU_MOUSE_BUTTONS),
            FieldSpec("click_method", str, max_len=16, allowed=_CU_CLICK_METHODS),
        ],
    ),
    _cu_types.TOOL_DRAG: ToolSchema(
        tool_name=_cu_types.TOOL_DRAG,
        fields=[
            _CU_APP_FIELD,
            # All four coordinates REQUIRED: a drag has no element form (no
            # accessibility action expresses a sweep between two points), so a
            # partial pair has no meaning to fall back on.
            _cu_coord_field("from_x", required=True),
            _cu_coord_field("from_y", required=True),
            _cu_coord_field("to_x", required=True),
            _cu_coord_field("to_y", required=True),
            FieldSpec("mouse_button", str, max_len=8, allowed=_CU_MOUSE_BUTTONS),
            # ``accessibility`` is accepted by the enum but refused by the driver
            # for a drag — enumerating a smaller set here would put the same rule
            # in two places and let them drift.
            FieldSpec("click_method", str, max_len=16, allowed=_CU_CLICK_METHODS),
            # The PATH shape. ``steps`` has no default here: absent means the
            # dispatcher's ``DEFAULT_DRAG_STEPS`` (the two-point form), and putting a
            # default in the schema as well would make one of the two authoritative
            # by accident.
            FieldSpec(
                "steps",
                int,
                min_val=_cu_types.MIN_DRAG_STEPS,
                max_val=_cu_types.MAX_DRAG_STEPS,
            ),
            FieldSpec("path", str, max_len=16, allowed=_CU_DRAG_PATHS),
        ],
    ),
    _cu_types.TOOL_TYPE_TEXT: ToolSchema(
        tool_name=_cu_types.TOOL_TYPE_TEXT,
        fields=[
            _CU_APP_FIELD,
            FieldSpec("text", str, required=True, max_len=_cu_types.MAX_TYPE_TEXT_LEN),
            # REQUIRED, and a security control rather than an ergonomic choice: an
            # unnamed target has no role or subrole, so ``policy.check_input_target``
            # has nothing to inspect and the keystrokes would land in whatever the
            # app happened to focus — possibly a password field. Enforced again at
            # the chokepoint by ``tools._ELEMENT_REQUIRED_TOOLS``; both layers say
            # required so a conforming call is never refused on arrival.
            _CU_ELEMENT_FIELD,
        ],
    ),
    _cu_types.TOOL_PRESS_KEY: ToolSchema(
        tool_name=_cu_types.TOOL_PRESS_KEY,
        fields=[
            _CU_APP_FIELD,
            # REQUIRED for the same reason as ``computer_type_text``, plus one of its
            # own: ``press_key('tab')`` can MOVE focus onto a password box, so the
            # keystroke after an indexless call would land there. The driver focuses
            # the named element first, which is what makes the secure-field refusal
            # meaningful.
            _CU_ELEMENT_FIELD,
            FieldSpec(
                "key",
                str,
                required=True,
                max_len=_cu_types.MAX_KEY_LEN,
                pattern=_CU_KEY_SPEC_RE,
            ),
        ],
    ),
    _cu_types.TOOL_SET_VALUE: ToolSchema(
        tool_name=_cu_types.TOOL_SET_VALUE,
        fields=[
            _CU_APP_FIELD,
            _CU_ELEMENT_FIELD,
            FieldSpec("value", str, required=True, max_len=_cu_types.MAX_TYPE_TEXT_LEN),
        ],
    ),
    _cu_types.TOOL_SCROLL: ToolSchema(
        tool_name=_cu_types.TOOL_SCROLL,
        fields=[
            _CU_APP_FIELD,
            _CU_ELEMENT_FIELD,
            FieldSpec("direction", str, required=True, max_len=8, allowed=_CU_SCROLL_DIRECTIONS),
            FieldSpec(
                "pages",
                (int, float),
                min_val=_cu_types.MIN_SCROLL_PAGES,
                max_val=_cu_types.MAX_SCROLL_PAGES,
                default=_cu_types.DEFAULT_SCROLL_PAGES,
            ),
        ],
    ),
    _cu_types.TOOL_PERFORM_ACTION: ToolSchema(
        tool_name=_cu_types.TOOL_PERFORM_ACTION,
        fields=[
            _CU_APP_FIELD,
            _CU_ELEMENT_FIELD,
            FieldSpec(
                "action",
                str,
                required=True,
                max_len=_cu_types.MAX_ACTION_LEN,
                pattern=_CU_AX_ACTION_RE,
            ),
        ],
    ),
    _cu_types.TOOL_END_TURN: ToolSchema(tool_name=_cu_types.TOOL_END_TURN, fields=[]),
}
