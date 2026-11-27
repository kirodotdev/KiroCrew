"""The ``crewmate.memory`` change card: a bot asks to delete or edit its own memories.

Raised only by the bot itself (``propose_memory_changes`` on ``kirocrew-mate``,
:mod:`kiro_crew.dashboard.handlers.member_memory_self`), never through
``propose_change``: the gateway fills ``member`` and ``store`` from the calling bot's
own thread, so a card can only ever name that bot's own memory store.

One card carries a batch of changes, each a fact or an experience (an episode) by id,
with ``delete`` or ``edit`` and the bot's one-line reason. The user may untick rows
before Apply; ``skip`` holds the indices of the unticked rows and is the card's only
editable field, so a re-preview can drop a row but never change what a row does.

Apply reuses the memory page's own routes, so the card writes exactly what that page
would, with its audit: per group a ``POST /api/memory/bulk/preview`` that signs the
selection at the revision the card showed, then ``POST /api/memory/bulk/apply`` with
the ``preview_id`` that step returned. Every edit is its own group, since the memory
editor replaces one record at a time, and the edits run first; all deletes then share
one last group, so an edit refused at apply time stops the plan before any delete.
A record that changed since the card was shown fails its preview
(``stale_memory_preview``) and nothing after it runs. The snapshot reader already
refuses a card whose edit the memory editor would refuse (``memory_not_editable``).

No Undo (``change_card_catalog_plans.offers_no_undo``): the memory page keeps each
record's history and restores a saved version from there.

Pure: no I/O. The snapshot comes from :mod:`kiro_crew.dashboard.member_memory_records`.
"""

from __future__ import annotations

from typing import Any

KIND_CREWMATE_MEMORY = "crewmate.memory"

ACTION_DELETE = "delete"
ACTION_EDIT = "edit"
ACTIONS = (ACTION_DELETE, ACTION_EDIT)

#: The record kinds a bot may change here: its facts and its experiences. Lessons
#: (``directive``) have their own tools (``learn_update`` / ``learn_remove``).
MEMORY_KINDS = ("fact", "episode")

#: The most changes one card carries: a card the user reads row by row.
MAX_CHANGES = 20
REASON_MAX = 200
#: The memory editor's own bound on an experience's text (``memory_edit._after``).
TEXT_MAX = 2000
ID_MAX = 512

#: The orders ``memory_list`` offers, as ``memory_record_facets.SORTS`` names them.
LIST_SORTS = ("updated", "created", "confidence", "importance")

#: The card's one editable field: indices of the rows the user unticked.
FIELD_SKIP = "skip"
EDITABLE = (FIELD_SKIP,)

#: The ``field`` of each change row, so the dashboard draws it as a ticked row.
ROW_FIELD = "memory_change"

PREVIEW_ROUTE = "/api/memory/bulk/preview"
APPLY_ROUTE = "/api/memory/bulk/apply"


def record_key(kind: str, record_id: str) -> str:
    """The snapshot key of one record."""
    return f"{kind}:{record_id}"


def params_schema() -> dict[str, Any]:
    """The card's ``params`` schema, as ``list_change_kinds`` would show it."""
    change = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(MEMORY_KINDS)},
            "id": {"type": "string", "maxLength": ID_MAX},
            "action": {"type": "string", "enum": list(ACTIONS)},
            "text": {"type": "string", "maxLength": TEXT_MAX},
            "value": {},
            "reason": {"type": "string", "maxLength": REASON_MAX},
        },
        "required": ["kind", "id", "action", "reason"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "member": {"type": "string"},
            "store": {"type": "string"},
            "changes": {"type": "array", "items": change, "maxItems": MAX_CHANGES},
            FIELD_SKIP: {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["member", "store", "changes"],
        "additionalProperties": False,
    }


def _clean_change(raw: Any, at: int) -> dict[str, Any]:
    from kiro_crew.change_card_catalog_kinds import _fail, _only

    where = f"change {at + 1}"
    if not isinstance(raw, dict):
        raise _fail("invalid_param", f"{where} must be an object")
    _only(raw, {"kind", "id", "action", "text", "value", "reason"}, where)
    kind, record_id, action = raw.get("kind"), raw.get("id"), raw.get("action")
    if kind not in MEMORY_KINDS:
        raise _fail("invalid_param", f"{where}: 'kind' must be fact or episode")
    if not isinstance(record_id, str) or not 1 <= len(record_id) <= ID_MAX:
        raise _fail("invalid_param", f"{where}: 'id' must be the id memory_list gave")
    if action not in ACTIONS:
        raise _fail("invalid_param", f"{where}: 'action' must be delete or edit")
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > REASON_MAX:
        raise _fail("invalid_param", f"{where}: 'reason' is one line of at most {REASON_MAX}")
    out: dict[str, Any] = {
        "kind": kind,
        "id": record_id,
        "action": action,
        "reason": " ".join(reason.split()),
    }
    if action == ACTION_DELETE:
        if "text" in raw or "value" in raw:
            raise _fail("invalid_param", f"{where}: a delete takes no text or value")
        return out
    if kind == "episode":
        text = raw.get("text")
        if "value" in raw or not isinstance(text, str) or not 10 <= len(text.strip()) <= TEXT_MAX:
            raise _fail(
                "invalid_param", f"{where}: an experience edit takes 'text' of 10-{TEXT_MAX} chars"
            )
        out["text"] = text.strip()
    else:
        if "text" in raw or "value" not in raw:
            raise _fail("invalid_param", f"{where}: a fact edit takes the new 'value'")
        out["value"] = raw["value"]
    return out


def clean_params(p: dict[str, Any]) -> dict[str, Any]:
    """Normalize the card's parameters. Raises ``CardCatalogError``."""
    from kiro_crew.change_card_catalog_kinds import _fail, _member_name, _only
    from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, memory_store_name_defect

    _only(p, {"member", "store", "changes", FIELD_SKIP})
    member = _member_name(p, "member")
    store = p.get("store")
    # The Global store is everyone's, never one bot's own.
    if (
        not isinstance(store, str)
        or store == DEFAULT_MEMORY_STORE
        or memory_store_name_defect(store) is not None
    ):
        raise _fail("invalid_param", "'store' must be the bot's own memory store")
    raw = p.get("changes")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_CHANGES:
        raise _fail("invalid_param", f"'changes' holds 1-{MAX_CHANGES} changes")
    changes = [_clean_change(c, i) for i, c in enumerate(raw)]
    keys = [record_key(c["kind"], c["id"]) for c in changes]
    if len(set(keys)) != len(keys):
        raise _fail("invalid_param", "each memory may appear in only one change")
    skip_raw = p.get(FIELD_SKIP, [])
    if not isinstance(skip_raw, list) or not all(
        isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(changes) for i in skip_raw
    ):
        raise _fail("invalid_param", "'skip' lists row numbers of this card")
    out: dict[str, Any] = {"member": member, "store": store, "changes": changes}
    skip = sorted(set(skip_raw))
    if skip:
        out[FIELD_SKIP] = skip
    return out


def _shown(record: dict[str, Any]) -> str:
    return str(record.get("shown") or "")


def _after_text(change: dict[str, Any]) -> str:
    if change["kind"] == "episode":
        return str(change["text"])
    from kiro_crew.change_card_catalog_plans import _plain

    return _plain(change["value"])


def _group_steps(store: str, items: list[dict[str, Any]], operation: dict[str, Any], at: int):
    """The preview step and the apply step for one group, the first at plan index *at*."""
    from kiro_crew.change_card_catalog_plans import _step_fill, _with_fills, step

    preview = step(
        "POST",
        PREVIEW_ROUTE,
        {"store": store, "selection": {"items": items}, "operation": operation},
    )
    fills = [_step_fill("preview_id", at, "preview_id")]
    apply = step("POST", APPLY_ROUTE, _with_fills({"store": store}, fills), fill=fills)
    return [preview, apply]


def build_preview(
    p: dict[str, Any], snapshot: dict[str, Any], out: dict[str, Any], label: str
) -> None:
    """Fill *out* (title, scope, changes, apply) for one ``crewmate.memory`` card.

    *label* is the bot's name as the dashboard shows it.

    *snapshot* is ``{"store", "records": {kind:id -> {revision, shown}}}`` as the
    reader took it. A change whose record is not there refuses the card: the bot
    lists its memory again and proposes what is still true.
    """
    from kiro_crew.change_card_catalog_kinds import _fail

    if snapshot.get("store") != p["store"]:
        raise _fail("not_own_store", "a bot can change only its own memory")
    records = snapshot.get("records") or {}
    skip = set(p.get(FIELD_SKIP) or [])
    rows: list[dict[str, Any]] = []
    deletes: list[dict[str, Any]] = []
    edits: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for at, change in enumerate(p["changes"]):
        rec = records.get(record_key(change["kind"], change["id"]))
        if not isinstance(rec, dict) and at in skip:
            # Gone since the card was shown, and unticked: the rest can still apply.
            rec = {}
        elif not isinstance(rec, dict):
            raise _fail(
                "not_found",
                f"memory {change['id']} is gone or changed kind; call memory_list again",
            )
        row: dict[str, Any] = {
            "field": ROW_FIELD,
            "index": at,
            "memory_kind": change["kind"],
            "action": change["action"],
            "label": change["reason"],
            "before": _shown(rec),
            "included": at not in skip,
        }
        if change["action"] == ACTION_EDIT:
            row["after"] = _after_text(change)
        rows.append(row)
        if at in skip:
            continue
        item = {"kind": change["kind"], "id": change["id"], "revision": rec["revision"]}
        if change["action"] == ACTION_DELETE:
            deletes.append(item)
        else:
            edits.append((item, change))
    if not deletes and not edits:
        raise _fail("no_change", "every row is unticked; decline the card instead")
    # Edits first: deletes have no Undo, so an edit refused at apply time must stop
    # the plan before any delete has run.
    apply: list[dict[str, Any]] = []
    for item, change in edits:
        field = "text" if change["kind"] == "episode" else "value"
        operation = {"type": "set", field: change[field]}
        apply += _group_steps(p["store"], [item], operation, len(apply))
    if deletes:
        apply += _group_steps(p["store"], deletes, {"type": "forget"}, len(apply))
    deleted, edited = len(deletes), len(edits)
    if deleted and edited:
        title = f"Delete {deleted} and edit {edited} of {label}'s memories"
    elif deleted:
        title = f"Delete {deleted} of {label}'s memories"
    else:
        title = f"Edit {edited} of {label}'s memories"
    out["title"] = title
    out["scope"] = {"members": [label], "count": 1, "only_this_member": True}
    out["changes"] = rows
    out["apply"] = apply
