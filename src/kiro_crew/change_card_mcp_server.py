"""The ``mcp.server`` change card: add, change or remove one server on the MCP page.

Raised only by ``request_server`` (``dashboard.handlers.request_server_page``), never
by ``propose_change``: an agent cannot write the MCP registries
(the "agents ask before changing gateway-trusted files" entry in docs/decisions/),
so this card is how it asks. The card shows the server's name and the real
difference of its entry; the person's Apply is the only thing that writes it.

The apply reuses the MCP page's own routes, so the card writes exactly what the
page would: ``POST /api/mcp/custom`` to add, ``PUT /api/mcp/custom/{name}`` to
change, ``POST /api/mcp/apply`` with ``uninstall`` to remove. With **Use in open chats now** checked
(``use_now``, the default), ``POST /api/sessions/restart`` follows, so open chats
load the change now; unchecked, new chats get it and open chats keep what they
started with unless Apply restarts the broker or removes a served target. Those
changes reset open chats when idle because subscribed stubs cannot reconnect.

This module is pure: it validates the card's parameters and builds its preview
from the snapshot the dashboard reader takes. It has no Undo
(``change_card_catalog_plans.offers_no_undo``): the store keeps no revision a
reverse write could bind to.
"""

from __future__ import annotations

import copy
import shlex
import urllib.parse
from typing import Any

# The catalog's helpers are imported where they are used: ``change_card_catalog_kinds``
# imports this module to build its kind table, so a module-level import back would
# close a cycle.

KIND_MCP_SERVER = "mcp.server"

ACTION_ADD = "add"
ACTION_CHANGE = "change"
ACTION_REMOVE = "remove"
ACTIONS = (ACTION_ADD, ACTION_CHANGE, ACTION_REMOVE)

#: The card's options the person can set before Apply (``KindDef.editable``):
#: restart open chats, and share this server with other chats and Bots.
OPTION_USE_NOW = "use_now"
OPTION_SHARE = "share"
EDITABLE = (OPTION_USE_NOW, OPTION_SHARE)
#: The asking agent's claim: does the server read or write the chat's folder?
#: Required on an add with an entry and on a change. Not editable on the card.
PARAM_USES_CHAT_FOLDER = "uses_chat_folder"
#: ``mcp_gateway.cwd_use`` answers (``config.integration_sections``).
CWD_USES = "uses_cwd"
CWD_NOT_USED = "not_used"
#: The change row that carries the sharing offer to the card's option, not drawn
#: as a change line: ``after`` is whether sharing is on for the server after Apply.
#: Present only for a server that is safe to share (:func:`safe_to_share`).
SHARE_OFFER_FIELD = "share_offer"

#: The change row that shows an Apply stops sharing a server. Drawn as an ordinary
#: change line, unlike the offer row, so no unshare step is hidden from the person.
SHARING_OFF_ROW = {"label": "Sharing", "before": "On", "after": "Off"}
#: Placeholders that stand for the chat's folder in an arg or env value.
_FOLDER_PLACEHOLDERS = (
    "${workspacefolder}",
    "${workspaceroot}",
    "${pwd}",
    "$pwd",
    "${cwd}",
    "${projectdir}",
    "${project_dir}",
)


def _names_the_chat_folder(value: str) -> bool:
    """Whether one arg or env value points at the folder the server runs in."""
    text = value.strip()
    if text.startswith("-") and "=" in text:
        text = text.split("=", 1)[1].strip()
    low = text.lower()
    if text in (".", "..", "~") or text.startswith(("./", "../", ".\\", "..\\")):
        return True
    if any(p in low for p in _FOLDER_PLACEHOLDERS):
        return True
    # A bare relative path (``src``, ``data/db.sqlite``) resolves against the folder.
    # A package spec (``@scope/pkg``, ``pkg@1.2``), a flag, a URL or an absolute path
    # does not.
    if not text or text.startswith(("-", "@", "/", "~/", "$")) or "://" in text:
        return False
    if len(text) > 2 and text[1] == ":":  # a Windows drive path
        return False
    return "@" not in text and ("/" in text or low.endswith((".js", ".mjs", ".cjs", ".ts", ".py")))


def detects_chat_folder(name: str, entry: dict[str, Any] | None) -> bool:
    """Kiro Crew's own reading: does this server read or write the chat's folder?

    True when an arg or env value is ``.``, a relative path or a folder placeholder
    (``${workspaceFolder}``, ``${PWD}`` and the like). The server's name and package
    are not read: a name says nothing certain about the folder. A remote server
    (``url``) runs no process here, so it has no folder to read. Anything this
    cannot read is treated as using the folder.
    """
    if not isinstance(entry, dict):
        return True
    if entry.get("url"):
        return False
    args = entry.get("args") or []
    env = entry.get("env") or {}
    if not isinstance(args, list) or not isinstance(env, dict):
        return True
    values = [str(a) for a in args] + [str(v) for v in env.values()]
    return any(_names_the_chat_folder(v) for v in values)


#: Why :func:`share_refusal` keeps a server per-chat. Only
#: :data:`REFUSED_USES_FOLDER` says anything about the chat's folder.
REFUSED_NOT_LOCAL = "not_local"
REFUSED_USES_FOLDER = "uses_folder"
REFUSED_NO_CLAIM = "no_claim"
REFUSED_DECLARED_ELSEWHERE = "declared_elsewhere"


def share_refusal(
    p: dict[str, Any], entry: dict[str, Any] | None, snapshot: dict[str, Any]
) -> str | None:
    """Why this server may not run as one copy for all chats, or None when it may.

    It may only when nothing says it uses the chat's folder: the asking agent says
    it does not, Kiro Crew's own reading (:func:`detects_chat_folder`) finds no
    sign, no earlier answer recorded that it does, and it runs a local process (a
    remote server has no launch to share). Another declaration of the name also
    keeps it per-chat: this card approves only the entry it writes. The folder
    signals are checked before that one, so :data:`REFUSED_USES_FOLDER` is returned
    whenever any of them is present.
    """
    if not isinstance(entry, dict) or not entry.get("command") or entry.get("url"):
        return REFUSED_NOT_LOCAL
    claim = p.get(PARAM_USES_CHAT_FOLDER)
    if claim is True or snapshot.get("cwd_use") == CWD_USES:
        return REFUSED_USES_FOLDER
    if detects_chat_folder(p["server"], entry):
        return REFUSED_USES_FOLDER
    if claim is not False:
        return REFUSED_NO_CLAIM
    if snapshot.get("share_exclusive") is not True:
        return REFUSED_DECLARED_ELSEWHERE
    return None


def safe_to_share(
    p: dict[str, Any], entry: dict[str, Any] | None, snapshot: dict[str, Any]
) -> bool:
    """Whether this server may run as one copy for all chats (:func:`share_refusal`)."""
    return share_refusal(p, entry, snapshot) is None


_TITLES = {
    ACTION_ADD: "Add server: {name}",
    ACTION_CHANGE: "Change server: {name}",
    ACTION_REMOVE: "Remove server: {name}",
}


def params_schema() -> dict[str, Any]:
    """The card's ``params`` schema, as ``list_change_kinds`` would show it."""
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "server": {"type": "string"},
            "entry": {"type": "object"},
            OPTION_USE_NOW: {"type": "boolean"},
            OPTION_SHARE: {"type": "boolean"},
            PARAM_USES_CHAT_FOLDER: {"type": "boolean"},
        },
        "required": ["action", "server"],
        "additionalProperties": False,
    }


def clean_params(p: dict[str, Any]) -> dict[str, Any]:
    """Normalize the card's parameters. Raises ``CardCatalogError``."""
    from kiro_crew.change_card_catalog_kinds import (
        _check_mcp_spec_shapes,
        _clean_bool,
        _fail,
        _only,
        valid_mcp_name,
    )

    _only(p, {"action", "server", "entry", OPTION_USE_NOW, OPTION_SHARE, PARAM_USES_CHAT_FOLDER})
    action = p.get("action")
    if action not in ACTIONS:
        raise _fail("invalid_param", "'action' must be add, change or remove")
    server = p.get("server")
    if not isinstance(server, str) or not valid_mcp_name(server):
        raise _fail("invalid_param", "invalid MCP server name")
    out: dict[str, Any] = {"action": action, "server": server}
    entry = p.get("entry")
    if action == ACTION_REMOVE:
        if entry is not None:
            raise _fail("invalid_param", "'entry' is not used to remove a server")
    else:
        if not isinstance(entry, dict) or not (
            isinstance(entry.get("command"), str) or isinstance(entry.get("url"), str)
        ):
            raise _fail("invalid_param", f"server '{server}' needs a command or a url")
        _check_mcp_spec_shapes(server, entry)
        from kiro_crew.dashboard.handlers.mcp_custom import _clean_spec, _validate_spec

        if action == ACTION_ADD:
            if "disabled" in entry:
                raise _fail("invalid_param", "'disabled' is not supported when adding a server")
            # The add route's own check, so a card the person approves does not
            # fail on Apply. A change is checked against the stored entry in
            # build_preview, the first place that has it.
            err = _validate_spec(entry)
            if err:
                raise _fail("invalid_param", f"server '{server}': {err}")
        out["entry"] = copy.deepcopy(entry)
        # Keep carried fields for the stored-entry check in build_preview. Only
        # normalize valid shapes; invalid changes still reach that check intact.
        if _validate_spec(entry, frozenset(entry)) is None:
            out["entry"].update(_clean_spec(entry))
    use_now = _clean_bool(p, OPTION_USE_NOW, required=False)
    out[OPTION_USE_NOW] = True if use_now is None else use_now
    share = _clean_bool(p, OPTION_SHARE, required=False)
    if share is not None:
        out[OPTION_SHARE] = share
    if action != ACTION_REMOVE:
        uses_folder = p.get(PARAM_USES_CHAT_FOLDER)
        if not isinstance(uses_folder, bool):
            raise _fail(
                "invalid_param",
                f"'{PARAM_USES_CHAT_FOLDER}' is required: true if the server reads or "
                "writes files in the chat's folder, false if it does not",
            )
        out[PARAM_USES_CHAT_FOLDER] = uses_folder
    return out


def _share_steps(
    p: dict[str, Any], entry: dict[str, Any] | None, snapshot: dict[str, Any], at: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The sharing rows and the steps that carry them out after the write.

    *entry* is the server's entry after Apply. A server that is safe to share
    (:func:`safe_to_share`) gets the offer. It starts checked for a new server,
    since sharing is an optimization people would not find otherwise (the owner's
    decision), and for one already shared. Checked, Apply records that the server does not use the
    chat's folder, turns MCP server sharing on for this Kiro Crew when it is off
    (``POST /api/mcp-gateway/enable``, a broker restart, never a gateway restart),
    reads the launch the write produced and shares it with that launch pinned.
    Unchecked on a shared server, Apply stops sharing it. A server that is not safe
    to share gets no offer, and if it was shared, Apply stops sharing it; only when
    a chat-folder signal is the reason (:data:`REFUSED_USES_FOLDER`) and no answer is
    recorded yet does it also record that the server uses the folder; a recorded
    answer is never rewritten. Every unshare step comes with the
    :data:`SHARING_OFF_ROW` change line, so the card shows all that Apply does. Any
    unshare step requires a session restart even when ``use_now`` is false, since
    the old target is gone.
    *at* is the plan index of the first step.
    """
    from kiro_crew.change_card_catalog_plans import _step_fill, _with_fills, step

    name = p["server"]
    shared = bool(snapshot.get("shared"))
    stub_path = "/api/mcp-gateway/servers/stub"
    unshare = [step("POST", stub_path, {"name": name, "stub": False})]
    if p["action"] == ACTION_REMOVE:
        return ([dict(SHARING_OFF_ROW)], unshare) if shared else ([], [])
    refusal = share_refusal(p, entry, snapshot)
    if refusal is not None:
        if not shared:
            return [], []
        # The operator's recorded answer is never rewritten; only a server with no
        # answer yet gets one.
        if refusal == REFUSED_USES_FOLDER and snapshot.get("cwd_use") is None:
            stop = {"name": name, "stub": False, "cwd_use": CWD_USES}
            unshare = [step("POST", stub_path, stop)]
        return [dict(SHARING_OFF_ROW)], unshare
    share = p.get(OPTION_SHARE, True if p["action"] == ACTION_ADD else shared)
    if snapshot.get("disabled"):
        # Muted entries have no launch to approve. Preserve any visible unshare.
        return ([dict(SHARING_OFF_ROW)], unshare) if shared and not share else ([], [])
    row = {"field": SHARE_OFFER_FIELD, "label": "Sharing", "after": share}
    if not share:
        return ([dict(SHARING_OFF_ROW), row], unshare) if shared else ([row], [])
    launch = "/api/mcp-gateway/servers/launch?" + urllib.parse.urlencode({"name": name})
    steps = []
    if not snapshot.get("sharing_on"):
        steps.append(step("POST", "/api/mcp-gateway/enable", {"enabled": True}))
    fills = [_step_fill("expected_launch", at + len(steps), "expected_launch")]
    assert entry is not None  # safe_to_share requires a local entry
    body = _with_fills(
        {
            "name": name,
            "stub": True,
            "cwd_use": CWD_NOT_USED,
            "expected_entry": {k: entry[k] for k in ("command", "args", "env") if k in entry},
        },
        fills,
    )
    return [row], [*steps, step("GET", launch), step("POST", stub_path, body, fill=fills)]


def _launch(entry: dict[str, Any] | None) -> str | None:
    if not entry:
        return None
    if entry.get("url"):
        return str(entry["url"])
    command = entry.get("command")
    if not isinstance(command, str):
        return None
    return shlex.join([command] + [str(a) for a in entry.get("args") or []])


def _env_changed(before: dict[str, Any] | None, after: dict[str, Any] | None) -> bool:
    old = (before or {}).get("env") or {}
    new = (after or {}).get("env") or {}
    return (old if isinstance(old, dict) else {}) != (new if isinstance(new, dict) else {})


def entry_rows(before: dict[str, Any] | None, after: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The rows that show how one server entry changes, field by field.

    The launch line is one before/after row. Env and headers are add/remove lists
    of ``key=value`` pairs, and a pair the change keeps is not listed. Each pair,
    old and new, on add, change and remove alike, goes through the credential
    redactor the ``mcp.add_custom`` card uses (``_display``): a secret-looking value
    is redacted, a plain value shows in full. A stored value the redactor does not
    recognise shows in full too; the agent can already read ``mcp.json`` itself.
    Any other field is a before/after row. Unchanged fields are left out.
    """
    from kiro_crew.change_card_catalog_plans import _display, _plain

    rows: list[dict[str, Any]] = []
    old_launch, new_launch = _launch(before), _launch(after)
    if old_launch != new_launch:
        is_url = bool((after or before or {}).get("url"))
        row: dict[str, Any] = {"label": "URL" if is_url else "Command"}
        if new_launch is None:
            # A removal: the line that goes away, not "becomes" nothing.
            row["remove"] = [_display(str(old_launch))]
        else:
            if old_launch is not None:
                row["before"] = _display(old_launch)
            row["after"] = _display(new_launch)
        rows.append(row)
    for field, label, sep in (("env", "Environment", "="), ("headers", "Headers", ": ")):
        old = (before or {}).get(field) or {}
        new = (after or {}).get(field) or {}
        old = old if isinstance(old, dict) else {}
        new = new if isinstance(new, dict) else {}
        removed = [k for k in old if k not in new or new[k] != old[k]]
        added = [k for k in new if k not in old or new[k] != old[k]]
        if not removed and not added:
            continue
        diff: dict[str, Any] = {"label": label}
        if added:
            diff["add"] = [_display(f"{k}{sep}{_plain(new[k])}") for k in sorted(added)]
        if removed:
            diff["remove"] = [_display(f"{k}{sep}{_plain(old[k])}") for k in sorted(removed)]
        rows.append(diff)
    skip = {"command", "args", "url", "env", "headers"}
    for field in sorted((set(before or {}) | set(after or {})) - skip):
        old_v, new_v = (before or {}).get(field), (after or {}).get(field)
        if old_v == new_v:
            continue
        other: dict[str, Any] = {"label": field}
        if new_v is None:
            other["remove"] = [_display(_plain(old_v))]
        else:
            if old_v is not None:
                other["before"] = _display(_plain(old_v))
            other["after"] = _display(_plain(new_v))
        rows.append(other)
    return rows


def build_preview(p: dict[str, Any], snapshot: dict[str, Any], out: dict[str, Any]) -> None:
    """Fill *out* (title, changes, risk, apply) for one ``mcp.server`` card.

    *snapshot* is :func:`dashboard.handlers.request_server_page.server_state`:
    ``exists`` (the name is on the MCP page anywhere), ``editable`` (it is in the
    Kiro Crew store, the one store a change writes) and ``entry`` (that stored
    entry). Raises ``CardCatalogError`` for a card that cannot apply.
    """
    from kiro_crew.change_card_catalog_kinds import RISK_CODE_EXEC, RISK_TIGHTEN, _fail
    from kiro_crew.change_card_catalog_plans import step

    action, name = p["action"], p["server"]
    stored = snapshot.get("entry") if isinstance(snapshot.get("entry"), dict) else None
    if action == ACTION_ADD:
        if snapshot.get("exists"):
            raise _fail("already_exists", f"an MCP server '{name}' already exists")
        from kiro_crew.dashboard.handlers.mcp_custom import _clean_spec

        after: dict[str, Any] | None = _clean_spec(p["entry"])
        rows = entry_rows(None, after)
        apply = [step("POST", "/api/mcp/custom", {"servers": {name: p["entry"]}, "enable": True})]
        out["risk"] = RISK_CODE_EXEC
    elif action == ACTION_CHANGE:
        if not snapshot.get("editable") or stored is None:
            raise _fail(
                "not_found" if not snapshot.get("exists") else "not_editable",
                f"'{name}' is not a server this card can change; change it on the MCP page",
            )
        # Build the same spec the PUT stores, including carried fields and OAuth hints.
        from kiro_crew.dashboard.handlers.mcp_custom import _build_update_spec

        after, refusal = _build_update_spec(p["entry"], stored)
        if refusal:
            raise _fail("invalid_param", f"server '{name}': {refusal[0]}")
        carried = [str(k) for k in snapshot.get("carried") or [] if k in stored]
        if "headers" in carried and "headers" in p["entry"]:
            raise _fail(
                "not_editable",
                f"'{name}' has stored header values; change them on the MCP page",
            )
        rows = entry_rows(stored, after)
        if not rows:
            raise _fail("no_change", f"'{name}' already has that entry")
        path = "/api/mcp/custom/" + urllib.parse.quote(name, safe="")
        apply = [step("PUT", path, {"spec": p["entry"]})]
        # New third-party code can run when the launch line or the env changes
        # (an env such as NODE_OPTIONS loads code). Headers reach a remote server
        # and do not.
        if _launch(stored) != _launch(p["entry"]) or _env_changed(stored, after):
            out["risk"] = RISK_CODE_EXEC
    else:
        if not snapshot.get("exists"):
            raise _fail("not_found", f"there is no MCP server '{name}'")
        after = None
        rows = entry_rows(stored, None) if stored is not None else []
        apply = [step("POST", "/api/mcp/apply", {"changes": [{"name": name, "uninstall": True}]})]
        out["risk"] = RISK_TIGHTEN
    share_rows, share_steps = _share_steps(p, after, snapshot, len(apply))
    apply += share_steps
    rows += share_rows
    # The launch read is a GET whose body is None.
    unshares = any((s.get("body") or {}).get("stub") is False for s in share_steps)
    if p.get(OPTION_USE_NOW, True) or unshares:
        apply.append(step("POST", "/api/sessions/restart", {}))
    out["title"] = _TITLES[action].format(name=name)
    out["changes"] = rows
    out["apply"] = apply
