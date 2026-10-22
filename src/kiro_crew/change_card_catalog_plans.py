"""What a change card shows and what its Apply runs: the plan a proposal becomes.

The step vocabulary a plan is written in (:func:`step`, the ``{{...}}`` fills and
:func:`placeholder`), the stores and dashboard setting tables a card writes, the risk
a setting change carries, the display helpers (``_display`` passes each value a card
shows in full through the credential redactor), the plan pieces Apply and Undo share,
and :func:`build_preview`, which turns a kind's parameters plus the current-state
snapshot into the card's title, ``changes`` rows, ``risk`` and ordered ``apply``
plan. A new kind's preview, or a new step shape, goes here.

Served through :mod:`kiro_crew.change_card_catalog`, which callers import and tests
patch; its mirror (:mod:`kiro_crew._facade_mirror`) carries such a patch to this
module, and this module never imports it.
"""

from __future__ import annotations

import copy
import json
import re
import shlex
import urllib.parse
from typing import Any

from kiro_crew import guide_catalog
from kiro_crew import mate_capability_catalog as _mate_catalog
from kiro_crew import member_goals as _goals
from kiro_crew.change_card_catalog_kinds import (
    _CODE_EXEC_SETTINGS,
    _PLACEHOLDER,
    _SETTING_ORDER,
    CREWMATE_CREATE_SCHEDULE_STEP,
    DEFAULT_CREWMATE_TEMPLATE,
    KIND_CONNECTION_CONNECT,
    KIND_CREWMATE_CAPABILITIES,
    KIND_CREWMATE_CHECK_INS,
    KIND_CREWMATE_CREATE,
    KIND_CREWMATE_DASHBOARD_PAGE,
    KIND_CREWMATE_GOAL,
    KIND_CREWMATE_IMPORT,
    KIND_CREWMATE_KEY_RESULTS,
    KIND_CREWMATE_UPDATE,
    KIND_DENIED_COMMAND,
    KIND_FOLDER_STEERING,
    KIND_MCP_ADD_CUSTOM,
    KIND_MCP_INSTALL,
    KIND_MCP_TOGGLE,
    KIND_SCHEDULE_CREATE,
    KIND_SCHEDULE_UPDATE,
    KIND_SECRET_SAVE,
    KIND_SETTING_CHANGE,
    KIND_TEMPLATE_UPDATE,
    KIND_TRUST_APP,
    LIST_OP_ADD,
    RISK_CODE_EXEC,
    RISK_NORMAL,
    RISK_TIGHTEN,
    RISK_WIDEN,
    _fail,
)
from kiro_crew.config.sections import (
    DEFAULT_MODEL,
    MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE,
    MEMBER_APPROVAL_MODES,
    effective_pin_scope,
    normalize_agent_model,
)

# ── plans ──


def _q(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")


def step(
    method: str,
    path: str,
    body: Any = None,
    *,
    fill: list[dict[str, Any]] | None = None,
    repeat: bool = False,
) -> dict[str, Any]:
    s: dict[str, Any] = {"method": method, "path": path, "body": body}
    if fill:
        s["fill"] = fill
    if repeat:
        s["repeat"] = True
    return s


def _user_fill(field: str) -> dict[str, Any]:
    return {"field": field, "source": "user"}


def _step_fill(field: str, step_index: int, key: str) -> dict[str, Any]:
    return {"field": field, "source": "step", "step": step_index, "key": key}


def _step_path_fill(field: str, step_index: int, key: str) -> dict[str, Any]:
    """A ``step`` fill of one path segment instead of a body field.

    The step's path carries :func:`placeholder` where the segment goes, and the
    value must be a plain identifier (:data:`PATH_FILL_VALUE`), so an earlier
    response can never add a segment or a query to the path.
    """
    return {**_step_fill(field, step_index, key), "in": "path"}


def is_path_fill(fill: dict[str, Any]) -> bool:
    return fill.get("in") == "path"


#: What a path fill may stand for: one segment of identifier characters.
PATH_FILL_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def placeholder(fill: dict[str, Any]) -> str:
    if fill["source"] == "user":
        return _PLACEHOLDER % f"user:{fill['field']}"
    return _PLACEHOLDER % f"step{fill['step']}.{fill['key']}"


def _with_fills(body: dict[str, Any], fills: list[dict[str, Any]]) -> dict[str, Any]:
    out = dict(body)
    for f in fills:
        out[f["field"]] = placeholder(f)
    return out


def _cron_body(fields: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for key in ("name", "message", "timezone"):
        if key in fields:
            body[key] = fields[key]
    if "cron_expr" in fields:
        body["cron"] = fields["cron_expr"]
    return body


def _list_diff(before: list[str], after: list[str]) -> tuple[list[str], list[str]]:
    return [x for x in after if x not in before], [x for x in before if x not in after]


def _order_risk(order: tuple[Any, ...], before: Any, after: Any) -> str:
    """Rank a move along *order* (tightest first): toward the end widens."""
    if before not in order or after not in order:
        return RISK_NORMAL
    b, a = order.index(before), order.index(after)
    if a > b:
        return RISK_WIDEN
    if a < b:
        return RISK_TIGHTEN
    return RISK_NORMAL


def _setting_risk(path: str, before: Any, after: Any) -> str:
    if path in _CODE_EXEC_SETTINGS:
        return RISK_CODE_EXEC
    order = _SETTING_ORDER.get(path)
    if path == "dashboard.tailscale.pin_scope":
        # The route takes any short string and the loader applies its
        # normalised form, so rank the values the gateway will apply.
        before, after = effective_pin_scope(before), effective_pin_scope(after)
    if order is not None and before in order and after in order:
        return _order_risk(order, before, after)
    segments = set(guide_catalog._SEGMENT_SPLIT.split(path.lower()))
    if segments & guide_catalog._EXCLUDED_SETTING_SEGMENTS:
        return RISK_WIDEN
    return RISK_NORMAL


STORE_KIROCREW = "kirocrew"
STORE_DASHBOARD = "dashboard"

#: Settings whose Settings-page control writes the DASHBOARD config
#: (``PUT /api/dashboard/config``, partial body) rather than a config path: each
#: registry id maps to the body key ``ChatPanel.tsx`` / ``BrowserPanel.tsx`` send
#: and the values that control offers. A registry id absent here, with no
#: editable ``configKey`` either, has no write path a card may use.
_BOOL_VALUES: tuple[Any, ...] = (True, False)
DASHBOARD_SETTINGS: dict[str, tuple[str, tuple[Any, ...]]] = {
    "chat.response-verbosity": ("verbosity", ("default", "concise", "ultra", "answer_only")),
    "chat.widget-density": ("widget_density", ("more", "less")),
    "chat.quick-send": ("quick_send", _BOOL_VALUES),
    "chat.merge-queued-messages": ("merge_queued_messages", _BOOL_VALUES),
    "chat.link-previews": ("link_previews", _BOOL_VALUES),
    "chat.mcp-apps-in-side-panel": ("mcp_app_panel", _BOOL_VALUES),
    "chat.auto-open-git-in-side-panel": ("auto_open_git_panel", _BOOL_VALUES),
    "chat.split-view-session-grid": ("session_grid", _BOOL_VALUES),
    "chat.tail-only-fork": ("tail_fork_enabled", _BOOL_VALUES),
    "chat.restore-sessions": ("restore_sessions", _BOOL_VALUES),
    "chat.restore-window": ("restore_window_minutes", (15, 30, 60, 120, 360, 720, 1440, 0)),
    "chat.pr-and-issue-chips-on-session-cards": ("session_card_source_links", _BOOL_VALUES),
    "chat.folder-suggestions": ("folder_suggestions_enabled", _BOOL_VALUES),
    "chat.default-memory-mode": ("default_memory_mode", ()),
    "browser.use-the-built-in-browser": ("use_builtin_browser", _BOOL_VALUES),
}
#: Body keys of ``PUT /api/dashboard/config`` a card may write, by key.
DASHBOARD_KEYS = {key: values for key, values in DASHBOARD_SETTINGS.values()}

#: Dashboard settings whose value is a STRING LIST the Settings page edits one
#: item at a time: registry id -> (stored key, add body key, remove body key),
#: exactly the delta keys ``ChatPanel.tsx``'s Selectable Models control sends to
#: ``PUT /api/dashboard/config``. A ``setting.change`` card on one of these takes
#: ``op``/``item`` instead of ``value``.
DASHBOARD_LIST_SETTINGS: dict[str, tuple[str, str, str]] = {
    "chat.selectable-models": (
        "model_picker_hidden_models",
        "model_picker_hidden_models_add",
        "model_picker_hidden_models_remove",
    ),
}
#: The same, by stored key.
DASHBOARD_LIST_KEYS = {v[0]: v for v in DASHBOARD_LIST_SETTINGS.values()}
#: How a one-item edit of a list setting reads, by stored key: (row label, add
#: title, remove title). The registry label names the Settings control, which
#: can say the opposite of the stored list ("Selectable Models" edits the HIDDEN
#: list), so the card names the list it really edits and says what the person
#: will see. A list without an entry reads "Add X to <label>" / "Remove X from".
LIST_SETTING_WORDING: dict[str, tuple[str, str, str]] = {
    "model_picker_hidden_models": (
        "Hidden models",
        "Hide “{item}” from the model picker",
        "Show “{item}” in the model picker",
    ),
}
#: Delta body keys of those lists -> the stored key they edit.
DASHBOARD_LIST_OP_KEYS = {
    op_key: v[0] for v in DASHBOARD_LIST_SETTINGS.values() for op_key in (v[1], v[2])
}


def value_allowed(value: Any, allowed: tuple[Any, ...] | list[Any]) -> bool:
    """Membership that does not let ``1`` stand for ``True`` (or the reverse)."""
    return any(type(value) is type(a) and value == a for a in allowed)


def _setting_risk_key(p: dict[str, Any], snapshot: dict[str, Any]) -> str:
    key = str(snapshot.get("key") or p.get("path") or "")
    if snapshot.get("store") == STORE_DASHBOARD:
        return f"dashboard.{key}"
    return key


def string_items(value: Any) -> list[str]:
    """*value*'s string entries, in order; anything else reads as an empty list."""
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def list_after(before: Any, op: str, item: str) -> list[str]:
    """The string list *before* with *item* added (appended) or removed."""
    items = string_items(before)
    if op == LIST_OP_ADD:
        return items if item in items else [*items, item]
    return [x for x in items if x != item]


def list_op_step(target: dict[str, Any], op: str, item: str) -> dict[str, Any]:
    """The Settings page's own one-item delta request for a string-list setting."""
    _key, add_key, remove_key = DASHBOARD_LIST_KEYS[str(target.get("key"))]
    return step(
        "PUT", "/api/dashboard/config", {add_key if op == LIST_OP_ADD else remove_key: [item]}
    )


def setting_write_step(p: dict[str, Any], target: dict[str, Any], value: Any) -> dict[str, Any]:
    """The one existing request that writes *value* to the setting's own store."""
    key = target.get("key") or p.get("path")
    if target.get("store") == STORE_DASHBOARD:
        return step("PUT", "/api/dashboard/config", {key: value})
    return step("PATCH", "/api/config/kirocrew", {"path": key, "value": value})


def _short(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _plain(value: Any) -> str:
    """A spec value as one display string: a string as itself, anything else as JSON."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _display(text: str) -> str:
    """*text* through the credential redactor, for a value a card shows in full."""
    from kiro_crew.platform import redact_via_context

    return redact_via_context(text)


def _server_spec_rows(name: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """The rows that show one MCP server spec in full, as Apply will send it.

    Apply sends the whole spec, so the card shows the whole spec: the full
    launch line (the row wraps) and every env and header value. Agent-proposed
    values were already refused if any output redactor would change them
    (``change_cards.check_param_text``); the display pass is defence in depth.
    """
    if spec.get("url"):
        launch = str(spec["url"])
    else:
        launch = shlex.join([str(spec.get("command"))] + [str(a) for a in spec.get("args") or []])
    rows: list[dict[str, Any]] = [{"label": name, "after": _display(launch)}]
    for field, sep in (("env", "="), ("headers", ": ")):
        values = spec.get(field)
        if isinstance(values, dict) and values:
            rows.append(
                {
                    "label": f"{name} {field}",
                    "add": [f"{k}{sep}{_display(_plain(v))}" for k, v in sorted(values.items())],
                }
            )
    for field in sorted(set(spec) - {"command", "args", "url", "env", "headers"}):
        rows.append({"label": f"{name} {field}", "after": _display(_plain(spec[field]))})
    return rows


def _launched_servers(draft: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The MCP server specs a capabilities draft sets with a launch line or URL."""
    out = []
    for op in draft.get("operations") or []:
        value = op.get("value")
        if (
            op.get("section") == "mcpServers"
            and op.get("action") == "set"
            and isinstance(value, dict)
            and ("command" in value or "url" in value)
        ):
            out.append((str(op.get("id") or ""), value))
    return out


def _crewmate_put_body(fields: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    """The crewmate update route's body for ``fields``, armed against a newer permission.

    An approval mode is a permission, so its write is compare-and-set: the body
    names the mode this plan read (``expected``'s, ``""`` for unset) and the
    route refuses with ``409 approval_mode_conflict``, under its config lock,
    when the stored mode has moved since. Without it a profile save made in the
    window after the card's own snapshot check and before its PUT would be
    silently replaced, possibly by a wider mode than the user last chose. The
    other fields are presentation and carry no such guard.
    """
    body = dict(fields)
    if "approval_mode" in body:
        body["expected_approval_mode"] = str(expected.get("approval_mode") or "")
    return body


def build_preview(
    kind: str, params: dict[str, Any], snapshot: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Title, changes, risk, scope and the apply plan for one proposal.

    *snapshot* is the gateway's read of the state the change replaces; *context*
    carries read-only facts used only for display (references, install details,
    a next run time). Raises :class:`CardCatalogError` for a proposal that would
    change nothing or cannot apply to the current state.
    """
    p = params
    # ``widen`` carries "this grants something the member runs without asking"
    # as its own flag, alongside the single ``risk`` label the badge shows. Only
    # the capabilities preview sets it today (an ``approval_expanded`` draft that
    # also launches code reads ``code_exec`` but must still require the widen
    # acknowledgement); every other kind leaves it False, so the acknowledgement
    # gate stays driven by ``risk == "widen"`` for them.
    out: dict[str, Any] = {"risk": RISK_NORMAL, "widen": False, "changes": []}
    rows: list[dict[str, Any]]
    changed: dict[str, Any]
    body: Any
    if kind == KIND_SETTING_CHANGE and "op" in p:
        if (
            snapshot.get("store") != STORE_DASHBOARD
            or snapshot.get("key") not in DASHBOARD_LIST_KEYS
        ):
            raise _fail("not_a_list", "'op'/'item' apply only to a string-list setting")
        before = snapshot.get("value")
        wording = LIST_SETTING_WORDING.get(str(snapshot.get("key")))
        label = (
            wording[0] if wording else snapshot.get("label") or p.get("setting_id") or p.get("path")
        )
        prior = string_items(before)
        after = list_after(before, p["op"], p["item"])
        if after == prior:
            state = "already lists" if p["op"] == LIST_OP_ADD else "does not list"
            raise _fail("no_change", f"'{label}' {state} '{p['item']}'")
        adding = p["op"] == LIST_OP_ADD
        if wording:
            out["title"] = (wording[1] if adding else wording[2]).format(item=p["item"])
        elif adding:
            out["title"] = f"Add “{p['item']}” to {label}"
        else:
            out["title"] = f"Remove “{p['item']}” from {label}"
        add, remove = _list_diff(prior, after)
        out["changes"] = [
            {"label": label, "before": prior, "after": after, "add": add, "remove": remove}
        ]
        # A one-item edit of a display list never widens what the agent may do.
        out["apply"] = [list_op_step(snapshot, p["op"], p["item"])]
    elif kind == KIND_SETTING_CHANGE:
        before = snapshot.get("value")
        label = snapshot.get("label") or p.get("setting_id") or p.get("path")
        if snapshot.get("store") == STORE_DASHBOARD and snapshot.get("key") in DASHBOARD_LIST_KEYS:
            raise _fail("list_setting", f"'{label}' is a list; pass 'op' and one 'item'")
        if before == p["value"]:
            raise _fail("no_change", f"'{label}' already has that value")
        out["title"] = f"Change {label}"
        out["changes"] = [{"label": label, "before": before, "after": p["value"]}]
        out["risk"] = _setting_risk(_setting_risk_key(p, snapshot), before, p["value"])
        out["apply"] = [setting_write_step(p, snapshot, p["value"])]
    elif kind == KIND_SCHEDULE_CREATE and "at" in p:
        # One-shot: the instant was resolved by the gateway's read of the zone
        # (``context["at_ts"]``) and is sent as epoch ``at``, which
        # ``POST /api/crons`` turns into a single-fire job deleted after its run.
        at_ts = context.get("at_ts")
        if isinstance(at_ts, bool) or not isinstance(at_ts, (int, float)):
            raise _fail("invalid_at", "the run time could not be resolved")
        tz = context.get("timezone") or p.get("timezone") or "UTC"
        out["title"] = f"Create one-time reminder “{p['name']}”"
        out["once"] = True
        out["changes"] = [
            {"label": "Name", "after": p["name"]},
            {"field": "at", "label": "When", "after": p["at"], "once": True, "timezone": tz},
            {"label": "Message", "after": _display(p["message"])},
        ]
        body = {"name": p["name"], "message": p["message"], "timezone": tz, "at": int(at_ts)}
        out["apply"] = [step("POST", "/api/crons", body)]
    elif kind == KIND_SCHEDULE_CREATE:
        out["title"] = f"Create schedule “{p['name']}”"
        out["changes"] = [
            {"label": "Name", "after": p["name"]},
            {"field": "cron_expr", "label": "When", "after": p["cron_expr"]},
            {"label": "Message", "after": _display(p["message"])},
        ]
        out["apply"] = [step("POST", "/api/crons", _cron_body(p))]
    elif kind == KIND_SCHEDULE_UPDATE:
        if not snapshot.get("exists"):
            raise _fail("not_found", "that schedule does not exist")
        before = snapshot.get("fields") or {}
        changed = {k: v for k, v in p["fields"].items() if before.get(k) != v}
        if not changed:
            raise _fail("no_change", "the schedule already has those values")
        out["title"] = f"Update schedule “{snapshot.get('name') or p['id']}”"
        out["changes"] = [
            {"field": k, "label": k, "before": before.get(k), "after": v}
            for k, v in changed.items()
        ]
        out["apply"] = [step("PATCH", f"/api/crons/{_q(p['id'])}", _cron_body(p["fields"]))]
    elif kind == KIND_CREWMATE_CREATE:
        if snapshot.get("exists"):
            raise _fail("already_exists", f"a mate named '{p['name']}' already exists")
        out["title"] = f"Create mate “{p['name']}”"
        out["changes"] = [
            {"label": "Name", "after": p["name"]},
            {"label": "Goal", "after": p["goal"]},
        ]
        inherited = [s for s in snapshot.get("inherited_auto_approve") or () if isinstance(s, str)]
        if inherited:
            # The crewmate runs these without asking from its first turn; the
            # click grants that, so the card says so and asks for the same
            # acknowledgement any other widening change does.
            out["changes"].append(
                {
                    "field": "auto_approve",
                    "label": "Runs without asking",
                    "after": ", ".join(inherited),
                }
            )
            out["risk"] = RISK_WIDEN
        # The goal is the crewmate's first goal in its goal record, never its
        # description. The record is addressed by the member_id the create
        # returns, so the goal is written only once the crewmate exists.
        goal_fills = [
            _step_path_fill("slug", 0, "member_id"),
            _step_fill("member", 0, "name"),
        ]
        apply = [
            step(
                "POST",
                "/api/agents",
                {
                    "name": p["name"],
                    "kiro_agent": DEFAULT_CREWMATE_TEMPLATE,
                    "source": "kirocrew",
                    # Its first chat opens with the crewmate asking about this
                    # goal, as one made on the Crewmates page does.
                    "first_greeting": True,
                },
            ),
            step(
                "PUT",
                f"/api/members/{placeholder(goal_fills[0])}/goals/long-term",
                _with_fills({"long_term": p["goal"], "expected": ""}, goal_fills[1:]),
                fill=goal_fills,
            ),
        ]
        schedule = p.get("schedule")
        if schedule:
            out["changes"].append(
                {"field": "cron_expr", "label": "When", "after": schedule["cron_expr"]}
            )
            apply.append(_create_schedule_step(p))
        out["apply"] = apply
    elif kind == KIND_CREWMATE_IMPORT:
        out.update(_import_preview(p, snapshot))
    elif kind == KIND_CREWMATE_UPDATE:
        if not snapshot.get("exists"):
            raise _fail("not_found", f"no mate named '{p['name']}'")
        stored = snapshot.get("fields") or {}
        before = dict(stored)
        # An unset approval mode is not "nothing": the thread opens in the
        # unset-effective mode, which is also what the profile's picker shows.
        # The card compares against that, so asking for the mode already in
        # force is refused as no change rather than drawn as "(empty) -> Trust".
        if "approval_mode" in p["fields"] and not before.get("approval_mode"):
            before["approval_mode"] = MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE
        # A model pin of "" and of DEFAULT_MODEL are one value (inherit the next
        # tier); the record stores "", the picker shows DEFAULT_MODEL. The card
        # shows the picker's spelling and compares the normalized pair, so asking
        # for the default while unpinned is no change. Reasoning effort keeps ""
        # (the dashboard labels it as inherited).
        if "model" in p["fields"]:
            before["model"] = before.get("model") or DEFAULT_MODEL
        changed = {
            k: v
            for k, v in p["fields"].items()
            if (
                normalize_agent_model(before.get(k)) != normalize_agent_model(v)
                if k == "model"
                else before.get(k) != v
            )
        }
        if not changed:
            raise _fail("no_change", "the mate already has those values")
        if "approval_mode" in changed:
            # A permission move is ranked like ``agent.approval_mode`` is: toward
            # ``trust`` the card is a widening (badge and acknowledgement), toward
            # ``normal`` a tightening. MEMBER_APPROVAL_MODES is ordered tightest
            # to widest, which is what makes the index comparison the ranking.
            out["risk"] = _order_risk(
                MEMBER_APPROVAL_MODES, before["approval_mode"], changed["approval_mode"]
            )
        # The title and the scope line name the crewmate the way the user knows
        # it (its display name, from the context); the key is the identity the
        # plan addresses and stays in the params and the path.
        label = str(context.get("label") or p["name"])
        out["title"] = (
            # The Preview opt-in a crewmate's first ``request_capabilities`` raises
            # while its flag is off (``kiro_crew.mate_tool_access``): a question
            # in the user's words, with no preview internals.
            f"Allow {label} to ask you for tools its worker needs? "
            "Each one still needs your approval."
            if changed == {"dynamic_capabilities": True}
            else f"Update mate “{label}”"
        )
        # ``field`` lets the card draw the user's word for the field and its
        # values (the dashboard's FIELD_LABELS); ``label`` stays the fallback.
        out["changes"] = [
            {"label": k, "field": k, "before": before.get(k), "after": v}
            for k, v in changed.items()
        ]
        out["scope"] = {"members": [label], "count": 1, "only_this_member": True}
        out["apply"] = [
            step("PUT", f"/api/agents/{_q(p['name'])}", _crewmate_put_body(changed, stored))
        ]
    elif kind == KIND_CREWMATE_GOAL:
        if not snapshot.get("exists") or not snapshot.get("slug"):
            raise _fail("not_found", f"no mate named '{p['member']}'")
        goals = [g for g in snapshot.get("goals") or [] if isinstance(g, dict)]
        label = str(context.get("label") or p["member"])
        gid = p.get("goal_id")
        goal_body: dict[str, Any] = {"member": p["member"], "long_term": p["long_term"]}
        if gid is None:
            if any(
                p["long_term"] in (g.get("text"), _goals.scrub_text(g.get("text"))) for g in goals
            ):
                raise _fail("no_change", "the mate already has that goal")
            goal_shown: str | None = None
            out["title"] = f"Add a goal for “{label}”"
            goal_number = len(goals) + 1
        else:
            goal_numbers = {g.get("id"): n for n, g in enumerate(goals, start=1)}
            if gid not in goal_numbers:
                raise _fail("not_found", "the mate has no goal with that id")
            goal_before = str(goals[goal_numbers[gid] - 1].get("text") or "")
            if goal_before == p["long_term"]:
                raise _fail("no_change", "the mate already has that goal")
            # The change line and the plan are public (CardStore.public): the goal
            # they replace is shown as the Goals tab shows it, scrubbed, and the
            # route takes the scrubbed text as ``expected``. The raw goal stays in
            # the gateway-private snapshot.
            goal_shown = _goals.scrub_text(goal_before)
            out["title"] = f"Set goal {goal_numbers[gid]} for “{label}”"
            goal_number = goal_numbers[gid]
            goal_body.update({"goal_id": gid, "expected": goal_shown})
        out["changes"] = [
            {
                "label": f"Goal {goal_number}",
                "number": goal_number,
                "field": "goal",
                "before": goal_shown or None,
                "after": p["long_term"],
            }
        ]
        out["scope"] = {"members": [label], "count": 1, "only_this_member": True}
        out["apply"] = [step("PUT", _goal_path(str(snapshot["slug"])), goal_body)]
    elif kind == KIND_CREWMATE_KEY_RESULTS:
        if not snapshot.get("exists") or not snapshot.get("slug"):
            raise _fail("not_found", f"no mate named '{p['member']}'")
        if not snapshot.get("has_goal"):
            raise _fail("no_goal", "the mate has no goal to measure yet")
        numbers = {
            g.get("id"): n
            for n, g in enumerate(snapshot.get("goals") or [], start=1)
            if isinstance(g, dict)
        }
        if any(g not in numbers for i in p["items"] for g in i.get("goal_ids", [])):
            raise _fail("not_found", "a key result names a goal the mate does not have")
        before_set = list(snapshot.get("key_results") or [])
        before_links = [
            list(r.get("goal_ids") or [])
            for r in snapshot.get("key_results_records") or []
            if isinstance(r, dict)
        ]
        same_links = all(
            "goal_ids" not in item
            or item["goal_ids"] == (before_links[n] if n < len(before_links) else [])
            for n, item in enumerate(p["items"])
        )
        if (
            _goals.key_result_shape(before_set) == _goals.key_result_shape(p["items"])
            and same_links
        ):
            raise _fail("no_change", "the mate already has those key results")
        label = str(context.get("label") or p["member"])
        # The plan and the change lines are public (CardStore.public); the set
        # they replace is shown as the Goals tab shows it, scrubbed. The route
        # takes the scrubbed set as ``expected``.
        before_set = _scrub_kr_shape(before_set)
        old_lines = [
            _kr_line(k, before_links[n] if n < len(before_links) else [], numbers)
            for n, k in enumerate(before_set)
        ]
        new_lines = [_kr_line(k, k.get("goal_ids", []), numbers) for k in p["items"]]
        out["title"] = f"Set the milestones for “{label}”"
        out["changes"] = [
            {
                "label": "Key results",
                "field": "key_results",
                "add": [ln for ln in new_lines if ln not in old_lines],
                "remove": [ln for ln in old_lines if ln not in new_lines],
            }
        ]
        out["scope"] = {"members": [label], "count": 1, "only_this_member": True}
        out["apply"] = [
            step(
                "PUT",
                _key_results_path(str(snapshot["slug"])),
                {
                    "member": p["member"],
                    "items": p["items"],
                    "expected": before_set,
                    "expected_ids": [r["id"] for r in snapshot.get("key_results_records") or []],
                    "expected_links": {
                        r["id"]: sorted(r.get("goal_ids") or [])
                        for r in snapshot.get("key_results_records") or []
                    },
                },
            )
        ]
    elif kind == KIND_CREWMATE_CHECK_INS:
        if not snapshot.get("exists") or not snapshot.get("slug"):
            raise _fail("not_found", f"no mate named '{p['member']}'")
        before_ci = snapshot.get("schedules") or {}
        changed = {k: v for k, v in p.items() if k != "member" and before_ci.get(k) != v}
        if not changed:
            raise _fail("no_change", "the check-ins are already set that way")
        if not snapshot.get("has_goal") and any(p.get(c) is True for c in _goals.CADENCES):
            raise _fail("no_goal", "the mate has no goal to check in on yet")
        label = str(context.get("label") or p["member"])
        out["title"] = f"Set the check-ins for “{label}”"
        out["changes"] = [
            {
                "label": CHECK_IN_LABELS[k],
                "field": k,
                "before": _check_in_value(k, before_ci.get(k)),
                "after": _check_in_value(k, v),
            }
            for k, v in changed.items()
        ]
        if changed.get(_goals.CADENCE_HEARTBEAT) is True:
            # Consent to a recurring spend names how often it recurs, the default too.
            every = p.get("interval", before_ci.get("interval"))
            for row in out["changes"]:
                if row["field"] == _goals.CADENCE_HEARTBEAT:
                    row["after"] = (
                        f"On, every {_goals.interval_label(every)} "
                        f"(up to {_goals.wakes_per_day(every)} runs a day)"
                    )
        out["scope"] = {"members": [label], "count": 1, "only_this_member": True}
        out["apply"] = [
            step(
                "PUT",
                _check_ins_path(str(snapshot["slug"])),
                {
                    "member": p["member"],
                    **changed,
                    "expected": {k: before_ci[k] for k in changed},
                },
            )
        ]
    elif kind == KIND_CREWMATE_DASHBOARD_PAGE:
        from kiro_crew import change_card_dashboard_page

        change_card_dashboard_page.preview(p, snapshot, context, out, fail=_fail, step=step)
    elif kind == KIND_CREWMATE_CAPABILITIES:
        revision = snapshot.get("revision")
        if not revision:
            raise _fail("not_found", f"no capabilities to edit for '{p['member']}'")
        impact = context.get("impact") or []
        if not impact:
            raise _fail("no_change", "these operations change nothing")
        label = str(context.get("label") or p["member"])
        out["title"] = f"Change tools for “{label}”"
        out["changes"] = _impact_rows(impact)
        launched = _launched_servers(p["draft"])
        for server, spec in launched:
            # A server this card defines runs its launch line in the member's
            # next session: shown in full, and marked as running code.
            out["changes"] += _server_spec_rows(server, spec)
        origins = p.get("origins") or {}
        gateway = set(context.get("gateway") or ())
        out["changes"] += _mate_catalog.origin_rows(p["draft"], origins, gateway, impact, _display)
        # Named additions read as one plain line each; the raw rows (sections,
        # launch lines, origins) stay on the card behind its Details.
        summary = _mate_catalog.addition_summary(p["draft"], origins, gateway, impact, _display)
        if summary:
            # An approval row that widens stays in the main view: a grant (a
            # whole server's included) is never hidden behind Details.
            out["changes"] = summary + [
                row if _widening_row(row, impact) else {**row, "detail": True}
                for row in out["changes"]
            ]
        out["scope"] = {"members": [label], "count": 1, "only_this_member": True}
        # An auto-approval this draft grants widens what the member runs without
        # asking, and a launch line runs third-party code. Both can be true at
        # once: a draft that sets an MCP server AND auto-approves one of its
        # tools. ``risk`` is the single worst label the badge shows, so
        # ``code_exec`` wins it; but the acknowledgement gate keys on
        # ``approval_expanded`` DIRECTLY, so carry it as its own ``widen`` flag
        # rather than folding it into ``risk`` -- otherwise a draft that does
        # both would surface as ``code_exec`` and silently drop the "runs
        # without asking" acknowledgement the plain-widen draft requires.
        approval_expanded = any(i.get("approval_expanded") for i in impact)
        # Named additions that are all servers this gateway already runs bring
        # no new outside code, so only the third-party warning goes. An approval
        # the draft widens keeps its acknowledgement, whatever the server.
        known_code = bool(summary) and _mate_catalog.only_gateway_servers(p["draft"], gateway)
        if known_code:
            launched = []
        out["widen"] = approval_expanded
        if launched:
            out["risk"] = RISK_CODE_EXEC
        elif approval_expanded:
            out["risk"] = RISK_WIDEN
        elif all(i.get("change") == "removed" for i in impact):
            out["risk"] = RISK_TIGHTEN
        out["apply"] = _capability_plan(p["member"], revision, p["draft"])
    elif kind == KIND_TEMPLATE_UPDATE:
        if not snapshot.get("exists"):
            raise _fail("not_found", f"no template named '{p['template']}'")
        before = snapshot.get("fields") or {}
        rows = []
        changed = {}
        widen = tighten = False
        for key, value in p["fields"].items():
            old = before.get(key)
            if old == value:
                continue
            changed[key] = value
            if isinstance(value, list):
                add, remove = _list_diff(old if isinstance(old, list) else [], value)
                rows.append({"label": key, "add": add, "remove": remove})
                if key == "allowedTools":
                    widen = widen or bool(add)
                    tighten = tighten or bool(remove)
            else:
                rows.append({"label": key, "before": old, "after": value})
        if not changed:
            raise _fail("no_change", "the template already has those values")
        out["title"] = f"Change template “{p['template']}”"
        out["changes"] = rows
        members = list(context.get("members") or [])
        out["scope"] = {"members": members, "count": len(members)}
        if widen:
            out["risk"] = RISK_WIDEN
        elif tighten:
            out["risk"] = RISK_TIGHTEN
        out["apply"] = [step("PATCH", f"/api/agents/detail/{_q(p['template'])}", changed)]
    elif kind == KIND_MCP_INSTALL:
        if snapshot.get("exists"):
            raise _fail("already_exists", f"an MCP server '{snapshot.get('name')}' already exists")
        detail = context.get("detail") or {}
        out["title"] = f"Install MCP server “{snapshot.get('name') or p['id']}”"
        out["changes"] = [
            {"label": "Source", "after": f"{p['provider']}: {p['id']}"},
            *(
                [{"label": "Command", "after": _short(detail["command"], 300)}]
                if detail.get("command")
                else []
            ),
            *([{"label": "Version", "after": detail["version"]}] if detail.get("version") else []),
            *(
                [{"label": "Repository", "after": detail["repo_url"]}]
                if detail.get("repo_url")
                else []
            ),
        ]
        out["risk"] = RISK_CODE_EXEC
        out["apply"] = [step("POST", "/api/mcp/discover/install", dict(p))]
    elif kind == KIND_MCP_ADD_CUSTOM:
        existing = [n for n, present in (snapshot.get("exists") or {}).items() if present]
        if existing:
            raise _fail("already_exists", f"an MCP server '{existing[0]}' already exists")
        out["title"] = "Add custom MCP server " + ", ".join(f"“{n}”" for n in p["servers"])
        rows = []
        for name, spec in p["servers"].items():
            rows += _server_spec_rows(name, spec)
        rows.append({"label": "Enabled", "after": p["enable"]})
        out["changes"] = rows
        out["risk"] = RISK_CODE_EXEC
        out["apply"] = [
            step("POST", "/api/mcp/custom", {"servers": p["servers"], "enable": p["enable"]})
        ]
    elif kind == KIND_MCP_TOGGLE:
        if not snapshot.get("exists"):
            raise _fail("not_found", "that MCP server is not configured")
        if snapshot.get("enabled") == p["enabled"]:
            raise _fail("no_change", "it is already in that state")
        label = f"{p['server']} / {p['tool']}" if "tool" in p else p["name"]
        verb = "Enable" if p["enabled"] else "Disable"
        out["title"] = f"{verb} MCP {'tool' if 'tool' in p else 'server'} {label}"
        out["changes"] = [
            {"label": label, "before": snapshot.get("enabled"), "after": p["enabled"]}
        ]
        out["risk"] = RISK_NORMAL if p["enabled"] else RISK_TIGHTEN
        out["apply"] = [_toggle_step(p, p["enabled"])]
    elif kind == KIND_CONNECTION_CONNECT:
        if not snapshot.get("known"):
            raise _fail("not_found", f"no connection provider '{p['slug']}'")
        if snapshot.get("granted"):
            raise _fail("no_change", f"'{p['slug']}' is already connected")
        out["title"] = f"Connect {p['slug']}"
        out["changes"] = [{"label": "Account", "before": "not connected", "after": "connected"}]
        out["apply"] = [
            step("POST", "/api/connections/mint", {"slug": p["slug"]}),
            step(
                "GET",
                "/api/connections/mint?" + urllib.parse.urlencode({"slug": p["slug"]}),
                repeat=True,
            ),
        ]
    elif kind == KIND_SECRET_SAVE:
        out["title"] = f"Save secret {p['name']}"
        out["changes"] = [
            {
                "label": p["name"],
                "before": "stored" if snapshot.get("exists") else "not stored",
                "after": "stored (value you type)",
            }
        ]
        fills = [_user_fill("value")]
        out["apply"] = [
            step("POST", "/api/secrets", _with_fills({"name": p["name"]}, fills), fill=fills)
        ]
        if snapshot.get("exists"):
            out["undo_unavailable_reason"] = "overwrites_existing"
    elif kind == KIND_TRUST_APP:
        if snapshot.get("trusted"):
            raise _fail("no_change", f"'{p['name']}' is already trusted")
        out["title"] = f"Trust app {p['name']}"
        out["changes"] = [{"label": p["name"], "before": "not trusted", "after": "trusted"}]
        if p.get("repository"):
            # Apply sends this repository as the one the person saw, so it is shown.
            out["changes"].append({"label": "Repository", "after": _display(p["repository"])})
        out["risk"] = RISK_WIDEN
        body = {"repository": p["repository"]} if p.get("repository") else None
        out["apply"] = [step("POST", f"/api/security/trusted-apps/{_q(p['name'])}", body)]
    elif kind == KIND_FOLDER_STEERING:
        if not snapshot.get("exists"):
            raise _fail("not_found", f"no folder with id '{p['folder']}'")
        current = string_items(snapshot.get("steering_dirs"))
        new = p["steering_dirs"]
        if new == current:
            raise _fail("no_change", "the folder already has that steering list")
        label = str(context.get("label") or p["folder"])
        out["title"] = f"Change steering for folder “{label}”"
        add, remove = _list_diff(current, new)
        out["changes"] = [{"label": "Folder", "after": label}]
        if add or remove:
            # The entries that differ, marked, so a dropped one is not found by eye.
            out["changes"].append({"label": "Change", "add": add, "remove": remove})
        out["changes"] += [
            {"label": "Current", "after": current},
            {"label": "New", "after": new},
        ]
        # A directory added is a host-file read the gateway now makes into every
        # chat in the folder; only removing directories narrows that.
        if add:
            out["risk"] = RISK_WIDEN
        elif remove:
            out["risk"] = RISK_TIGHTEN
        out["apply"] = [_folder_steering_step(p["folder"], new)]
    else:  # KIND_DENIED_COMMAND
        if p["action"] == "add":
            if snapshot.get("exists"):
                raise _fail("already_exists", "that denied-command pattern already exists")
            out["title"] = "Add denied-command rule"
            out["changes"] = [{"label": "Pattern", "add": [p["pattern"]]}]
            out["risk"] = RISK_TIGHTEN
            out["apply"] = [
                step(
                    "POST",
                    "/api/security/denied-commands/user",
                    {"pattern": p["pattern"], "note": p["note"]},
                )
            ]
        else:
            if not snapshot.get("exists"):
                raise _fail("not_found", "no user denied-command rule with that id")
            if snapshot.get("enabled") == p["enabled"]:
                raise _fail("no_change", "the rule is already in that state")
            verb = "Enable" if p["enabled"] else "Disable"
            out["title"] = f"{verb} denied-command rule"
            out["changes"] = [
                {
                    "label": _short(snapshot.get("pattern") or p["id"]),
                    "before": snapshot.get("enabled"),
                    "after": p["enabled"],
                }
            ]
            out["risk"] = RISK_TIGHTEN if p["enabled"] else RISK_WIDEN
            out["apply"] = [
                step(
                    "PATCH",
                    f"/api/security/denied-commands/user/{_q(p['id'])}",
                    {"enabled": p["enabled"]},
                )
            ]
    if offers_no_undo(kind, p):
        out["undo_unavailable_reason"] = UNDO_NO_IDENTITY_CHECK
    return out


#: Undo reason for kinds whose reverse is a delete their store cannot bind to the
#: entry this card wrote.
UNDO_NO_IDENTITY_CHECK = "no_identity_check"


def offers_no_undo(kind: str, params: dict[str, Any]) -> bool:
    """True for kinds whose Undo could not bind its write to the entry this card wrote.

    Uninstalling an MCP server, disconnecting an account, removing a trusted app
    and deleting a denied-command rule all go through stores that take no
    revision or fingerprint, so an Undo could not tell the entry this card wrote
    from one the user put there since under the same name. A schedule update is
    reversed by a cron PATCH, which writes unconditionally, so an Undo could
    overwrite an edit saved from another tab. These cards apply and are reversed
    from their settings page; the card offers no Undo.
    """
    if kind in (
        KIND_MCP_INSTALL,
        KIND_MCP_ADD_CUSTOM,
        KIND_CONNECTION_CONNECT,
        KIND_TRUST_APP,
        KIND_SCHEDULE_UPDATE,
        # The yes only starts a wake; the page it leads to goes back with
        # ``dashboard_rollback``, a version like any other.
        KIND_CREWMATE_DASHBOARD_PAGE,
    ):
        return True
    return kind == KIND_DENIED_COMMAND and params.get("action") == "add"


def _folder_steering_step(folder: str, steering_dirs: list[str]) -> dict[str, Any]:
    return step("PATCH", f"/api/chat/folders/{_q(folder)}", {"steering_dirs": steering_dirs})


def _toggle_step(p: dict[str, Any], enabled: bool) -> dict[str, Any]:
    if "tool" in p:
        return step(
            "POST",
            "/api/mcp/toggle-tool",
            {"server": p["server"], "tool": p["tool"], "enabled": enabled},
        )
    return step("POST", "/api/mcp/toggle", {"name": p["name"], "enabled": enabled})


def _create_schedule_step(p: dict[str, Any]) -> dict[str, Any]:
    """A ``crewmate.create`` card's work-schedule step, filled from step 0."""
    fills = [_step_fill("member_id", 0, "member_id")]
    body = {
        "name": f"{p['name']} schedule",
        "message": p["goal"],
        "agent": DEFAULT_CREWMATE_TEMPLATE,
        "silent": False,
        "hide_in_chat": False,
        "strict_schedule": True,
        **_cron_body(p["schedule"]),
    }
    return step("POST", "/api/crons", _with_fills(body, fills), fill=fills)


#: The roles of a ``crewmate.import`` card's apply steps, in plan order
#: (rfc-crewmate-share.md §5.4). Step 0 is always the create.
IMPORT_STEP_CREATE = "create"
IMPORT_STEP_GOAL = "goal"
IMPORT_STEP_RULES = "rules"
IMPORT_STEP_FILES = "files"
IMPORT_STEP_CRON = "cron"


def _import_message_crons(p: dict[str, Any]) -> list[dict[str, Any]]:
    """The crons a plain cron step creates; a ``script`` cron is made with its script."""
    return [job for job in p.get("crons") or () if "script" not in job]


def import_step_roles(p: dict[str, Any]) -> list[str]:
    """What each apply step of a ``crewmate.import`` card writes, by index."""
    roles = [IMPORT_STEP_CREATE]
    if p.get("goal"):
        roles.append(IMPORT_STEP_GOAL)
    if p.get("rules"):
        roles.append(IMPORT_STEP_RULES)
    if p.get("carried"):
        roles.append(IMPORT_STEP_FILES)
    roles += [IMPORT_STEP_CRON] * len(_import_message_crons(p))
    return roles


def import_files_path(slug: str) -> str:
    """The owner route that writes a new crewmate's carried files (and takes them back)."""
    return f"/api/members/{slug}/import-files"


def _import_member_fills() -> list[dict[str, Any]]:
    return [_step_path_fill("slug", 0, "member_id"), _step_fill("member", 0, "name")]


def _import_rules_step(rules: str) -> dict[str, Any]:
    fills = _import_member_fills()
    path = f"/api/members/{placeholder(fills[0])}/rules"
    return step("PUT", path, _with_fills({"rules": rules}, fills[1:]), fill=fills)


def _import_files_step(sha: str) -> dict[str, Any]:
    """Write the carried files from the staged copy named *sha*, into the new crewmate's own space."""
    fills = _import_member_fills()
    body = _with_fills({"sha256": sha}, fills[1:])
    return step("POST", import_files_path(placeholder(fills[0])), body, fill=fills)


def _import_cron_step(job: dict[str, Any]) -> dict[str, Any]:
    """One imported cron, bound to the new crewmate and created paused in the same write."""
    fills = [_step_fill("member_id", 0, "member_id")]
    body = {
        **_cron_body(job),
        "agent": DEFAULT_CREWMATE_TEMPLATE,
        "enabled": False,
    }
    return step("POST", "/api/crons", _with_fills(body, fills), fill=fills)


def _import_preview(p: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """The ``crewmate.import`` card: every part shown, then the §5.4 ordered plan."""
    from kiro_crew import crewmate_carried

    if snapshot.get("exists"):
        raise _fail("already_exists", f"a mate named '{p['name']}' already exists")
    carried = p.get("carried") or {}
    scripts = {s["file"] for s in carried.get("scripts") or ()}
    named = {job["script"].partition(":")[0] for job in p.get("crons") or () if "script" in job}
    if named != scripts:
        raise _fail("invalid_param", "each script cron needs its script, and each script a cron")
    bundled = {r["name"] for r in p.get("skills") or () if r["source"] == "bundled"}
    if bundled != {s["name"] for s in carried.get("skills") or ()}:
        raise _fail("invalid_param", "each bundled skill needs its files")
    out: dict[str, Any] = {"risk": RISK_NORMAL, "title": f"Import mate “{p['name']}”"}
    changes: list[dict[str, Any]] = [
        {"field": "name", "label": "Name", "after": p["name"]},
        {"field": "sender", "label": "From", "after": p.get("sender", "")},
    ]
    for key, label in (("description", "Description"), ("model_hint", "Model")):
        if p.get(key):
            changes.append({"field": key, "label": label, "after": p[key]})
    refs = [r for r in p.get("skills") or () if r["source"] != "bundled"]
    if refs:
        changes.append({"field": "import_skills", "label": "Skills", "after": refs})
    if carried.get("skills"):
        row = {"field": "carried_skills", "label": "Skills it brings"}
        changes.append({**row, "after": carried["skills"]})
    if p.get("rules"):
        changes.append({"field": "rules", "label": "Rules", "after": p["rules"]})
    if p.get("goal"):
        changes.append({"field": "goal", "label": "Goal", "after": p["goal"]})
    if p.get("crons"):
        changes.append({"field": "import_crons", "label": "Crons", "after": p["crons"]})
    if carried.get("scripts"):
        row = {"field": "carried_scripts", "label": "Cron scripts"}
        changes.append({**row, "after": carried["scripts"]})
    if carried.get("avatar"):
        changes.append({"field": "carried_avatar", "label": "Picture", "after": carried["avatar"]})
    if p.get("dashboard_template"):
        row = {"field": "dashboard_template", "label": "Dashboard"}
        changes.append({**row, "after": p["dashboard_template"]})
    # Trust is the receiver's to grant: the mode rides on the create itself,
    # because an unset mode opens in MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE.
    inherited = [s for s in snapshot.get("inherited_auto_approve") or () if isinstance(s, str)]
    # ``import_approval`` only when a row of inherited grants follows: its
    # hint says the crewmate asks like the owner's other crewmates do.
    mode = "import_approval" if inherited else "approval_mode"
    changes.append({"field": mode, "label": "Approval", "after": "normal"})
    if inherited:  # granted by the template, so the press widens (as crewmate.create)
        reach = [r for r in snapshot.get("inherited_reach") or () if isinstance(r, dict)]
        row = {"field": "template_auto_approve", "label": "Your mates use these without asking"}
        changes.append({**row, "after": reach or ", ".join(inherited)})
        out["risk"] = RISK_WIDEN
    if crewmate_carried.code_exec(carried):
        # Each runs on this machine; a widening press still asks for its acknowledgement.
        out["widen"] = out["risk"] == RISK_WIDEN
        out["risk"] = RISK_CODE_EXEC
    changes.append({"field": "not_included", "label": "Stays with the sender", "after": "private"})
    if p.get("not_imported"):
        row = {"field": "not_imported", "label": "Not brought over"}
        changes.append({**row, "after": p["not_imported"]})
    out["changes"] = changes

    create: dict[str, Any] = {
        "name": p["name"],
        "kiro_agent": DEFAULT_CREWMATE_TEMPLATE,
        "source": "kirocrew",
        "description": p.get("description", ""),
        "approval_mode": "normal",
        "imported": {
            "format_version": p["format_version"],
            "sender_label": p.get("sender", ""),
            "sha256": p.get("bundle_sha256", ""),
        },
    }
    if p.get("avatar_traits") and not carried.get("avatar"):
        create["avatar"] = {"kind": "ghost", "traits": p["avatar_traits"]}
    apply = [step("POST", "/api/agents", create)]
    if p.get("goal"):  # crewmate.create's goal step, addressed by step 0's member_id
        fills = _import_member_fills()
        body = _with_fills({"long_term": p["goal"], "expected": ""}, fills[1:])
        path = f"/api/members/{placeholder(fills[0])}/goals/long-term"
        apply.append(step("PUT", path, body, fill=fills))
    if p.get("rules"):
        apply.append(_import_rules_step(p["rules"]))
    if carried:
        apply.append(_import_files_step(p["bundle_sha256"]))
    apply += [_import_cron_step(job) for job in _import_message_crons(p)]
    out["apply"] = apply
    return out


def import_undo(
    p: dict[str, Any], evidence: list[dict[str, Any]], applied_steps: int
) -> list[dict[str, Any]]:
    """Take back what an applied (or ``partial``) import wrote, newest part first.

    Its plain crons are deleted by the ids their creates answered, its carried
    files (and the script crons made with them) by the file step's own record,
    its rules are cleared (a rules file outlives the crewmate it belongs to),
    and the crewmate is deleted last, so every earlier step still finds it.
    """
    roles = import_step_roles(p)[:applied_steps]
    plan = []
    for index, role in enumerate(roles):
        job_id = evidence[index].get("id") if index < len(evidence) else None
        if role == IMPORT_STEP_CRON and job_id:
            plan.append(step("DELETE", f"/api/crons/{_q(str(job_id))}"))
    name = (evidence[0].get("name") if evidence else None) or p["name"]
    slug = str((evidence[0].get("member_id") if evidence else "") or "")
    if IMPORT_STEP_FILES in roles:
        plan.append(step("DELETE", import_files_path(_q(slug)), {"member": name}))
    if IMPORT_STEP_RULES in roles:
        body = {"member": name, "rules": ""}
        plan.append(step("PUT", f"/api/members/{_q(slug)}/rules", body))
    plan.append(step("DELETE", f"/api/agents/{_q(str(name))}"))
    return plan


def legacy_create_apply(
    params: dict[str, Any], *, first_greeting: bool = True
) -> list[dict[str, Any]]:
    """The apply plan a ``crewmate.create`` card was stored with before goal records.

    The goal went into ``description`` and the schedule was step 1. A card
    persisted in that shape is still a valid record (``rebuild_record``): its
    applied steps and evidence keep their indexes, so its Undo still finds the
    schedule it made (:func:`_create_schedule_index`). Cards from builds whose
    create did not yet ask for a first greeting carry no ``first_greeting``
    key; ``first_greeting=False`` rebuilds that shape.
    """
    p = params
    body: dict[str, Any] = {
        "name": p["name"],
        "kiro_agent": DEFAULT_CREWMATE_TEMPLATE,
        "description": p["goal"],
        "source": "kirocrew",
    }
    if first_greeting:
        body["first_greeting"] = True
    apply = [step("POST", "/api/agents", body)]
    if p.get("schedule"):
        apply.append(_create_schedule_step(p))
    return apply


#: Where the schedule step of a legacy-shaped ``crewmate.create`` card sits.
LEGACY_CREWMATE_CREATE_SCHEDULE_STEP = 1


def _create_schedule_index(evidence: list[dict[str, Any]]) -> int:
    """The step index of a ``crewmate.create`` card's schedule, by its evidence.

    Step 1 of the current plan is the goal write, whose evidence never holds an
    ``id``; a legacy card's step 1 was the schedule, whose evidence is its job id.
    """
    legacy = len(evidence) > 1 and isinstance(evidence[1], dict) and "id" in evidence[1]
    return LEGACY_CREWMATE_CREATE_SCHEDULE_STEP if legacy else CREWMATE_CREATE_SCHEDULE_STEP


def _goal_path(slug: str) -> str:
    return f"/api/members/{_q(slug)}/goals/long-term"


def _key_results_path(slug: str) -> str:
    return f"/api/members/{_q(slug)}/goals/key-results"


def _check_ins_path(slug: str) -> str:
    return f"/api/members/{_q(slug)}/goals/schedules"


CHECK_IN_LABELS = {
    "daily": "Daily Goal Reminder",
    "weekly": "Weekly report",
    "monthly": "Monthly review",
    "heartbeat": "Heartbeat (each run uses credits)",
    "weekday": "Day of the week",
    "month_day": "Day of the month",
    "time": "Check-in time",
    "interval": "Heartbeat interval",
}
_WEEKDAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def _check_in_value(key: str, value: Any) -> str | None:
    """How one check-in field reads on the card."""
    if value is None:
        return None
    if key in _goals.CADENCES:
        return "On" if value else "Off"
    if key == "weekday" and isinstance(value, int) and 0 <= value <= 6:
        return _WEEKDAY_NAMES[value]
    if key == "month_day":
        return f"Day {value}"
    if key == "interval" and isinstance(value, int):
        return (
            f"Every {_goals.interval_label(value)} "
            f"(up to {_goals.wakes_per_day(value)} runs a day)"
        )
    return str(value)


def _scrub_kr_shape(items: list[Any]) -> list[dict[str, str]]:
    from kiro_crew.member_goals import key_result_shape, scrub_text

    return [{k: scrub_text(v) for k, v in i.items()} for i in key_result_shape(items)]


def _scrub_kr_records(records: list[Any]) -> list[dict[str, Any]]:
    """Key-result records with every string through the goal scrubber."""
    from kiro_crew.member_goals import scrub_text

    out = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        clean = {k: scrub_text(v) if isinstance(v, str) else v for k, v in rec.items()}
        clean["id"] = rec.get("id")
        clean["readings"] = [
            {**r, "value": scrub_text(r.get("value", ""))}
            for r in rec.get("readings") or []
            if isinstance(r, dict)
        ]
        out.append(clean)
    return out


def _kr_line(
    item: dict[str, Any], links: list[Any] | None = None, numbers: dict[Any, int] | None = None
) -> str:
    """``text → target``, then the numbers of the goals it serves (``· #1 #3``)."""
    line = f"{item.get('text', '')} → {item.get('target', '')}"
    served = [numbers[g] for g in links or [] if numbers and g in numbers]
    if served:
        line += " · " + " ".join(f"#{n}" for n in sorted(served))
    return line


def _capability_plan(member: str, revision: str, draft: dict[str, Any]) -> list[dict[str, Any]]:
    base = {"revision": revision, **copy.deepcopy(draft)}
    fills = [_step_fill("preview_token", 0, "preview_token")]
    path = f"/api/agents/{_q(member)}/capabilities"
    return [
        step("POST", path + "/preview", base),
        step("PUT", path, _with_fills(base, fills), fill=fills),
    ]


def _widening_row(row: dict[str, Any], impact: list[dict[str, Any]]) -> bool:
    """Whether *row* (an :func:`_impact_rows` row) adds an approval the preview counts as wider."""
    section = row.get("label")
    if section not in ("allowedTools", "autoApprove"):
        return False
    added = set(row.get("add") or [])
    return any(
        i.get("approval_expanded") and i.get("section") == section and i.get("id") in added
        for i in impact
    )


def _impact_rows(impact: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, list[str]]] = {}
    for item in impact:
        section = str(item.get("section") or "")
        bucket = grouped.setdefault(section, {"add": [], "remove": [], "changed": []})
        change = item.get("change")
        if change == "added":
            key = "add"
        elif change == "removed":
            key = "remove"
        else:
            key = "changed"
        bucket[key].append(str(item.get("id") or ""))
    rows = []
    for section, bucket in grouped.items():
        row: dict[str, Any] = {"label": section, "add": bucket["add"], "remove": bucket["remove"]}
        if bucket["changed"]:
            row["after"] = "changed: " + ", ".join(bucket["changed"])
        rows.append(row)
    return rows


def capability_inverse(
    draft: dict[str, Any], rows: list[dict[str, Any]], *, severed: bool = False
) -> list[dict[str, Any]] | None:
    """Operations that restore each row *draft* touches to its state in *rows*.

    ``None`` when a row cannot be restored exactly: an MCP server definition the
    mate set locally is shown redacted, so writing it back would not restore it.
    *severed* is a crewmate's own file (view ``mode="mate"``): with no parent, a
    row that was absent or off (``present``) is restored with ``remove``, and an
    on approval or switch with ``set True``.
    """
    by_key = {(r.get("section"), r.get("id")): r for r in rows if isinstance(r, dict)}
    ops: list[dict[str, Any]] = []
    for op in draft.get("operations") or []:
        section, rid = op.get("section"), op.get("id")
        row = by_key.get((section, rid)) or {}
        state = row.get("state") or "inherited"
        if severed and not row.get("present"):
            ops.append({"section": section, "id": rid, "action": "remove"})
        elif severed and section in ("tools", "allowedTools", "autoApprove", "skills"):
            ops.append({"section": section, "id": rid, "action": "set", "value": True})
        elif state == "removed":
            ops.append({"section": section, "id": rid, "action": "remove"})
        elif state == "local":
            if section == "mcpServers":
                return None
            ops.append({"section": section, "id": rid, "action": "set", "value": row.get("value")})
        else:
            ops.append({"section": section, "id": rid, "action": "inherit"})
    return ops
