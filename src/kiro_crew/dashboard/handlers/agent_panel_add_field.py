"""``POST /api/agent-panel/dashboard/add-field`` -- one more field on the caller's own page.

A Bot whose page is live and that needs one more hand-written field (the XR room's
``xr_layout``, say) would otherwise send its whole page again through
``dashboard_save``. This route takes the field alone and builds the page around it.

One source
----------
The field is added to ONE whole page, its model, view, theme and widgets together, and
never to a mix of two. Which page (:func:`_compose`):

* his TRUE DRAFT, when there is one: the stored page, if it was never put in force. The
  record says so: no version it keeps pins the page's bytes (``instance_applied.applied_pages``),
  or the page was written after the last version that does;
* else HIS OWN PAGE IN FORCE, whole: the stored page when it is that page, and otherwise
  the gateway's pinned snapshot (``instance.pinned_package``), written as the new stored
  page. A stored page he LEFT (by a rollback, or by applying a template after it) is
  not the source: one add and one ``dashboard_apply`` must not bring it back. That
  does not hold when its artifact was renamed, described, tagged or saved again
  since the page was last put live: the record's time moves, and the page reads as a
  true draft (an open hole);
* with a shipped template in force and no true draft there is no page of his to add to.

Where the record cannot tell a true draft from a page he left, the add is refused and
writes nothing: a record from before pins whose stored version a person cut, and a stored
page older than the versions the record still keeps. It never guesses. The reply says
which page the field went onto, in one of the two sentences of :data:`ONTO`.

One writer
----------
The page is written by ``agent_panel_dashboard._store_bound_package``, the helper
``dashboard_save`` ends in: the same write gate, the same save lock, the same fenced
update, the same page check. The read of the source page happens INSIDE that lock
(:func:`_compose`), so two adds at once, or an add beside a save, each see the other's
write or wait for it.

What the caller chooses
-----------------------
``name``, ``type``, ``label`` and ``description``, under a closed schema. The field's
``source`` is always ``{"agentic": true}``; the caller, the binding and the page come
from the session's own binding (``_resolve_dashboard_caller``), never from the body.

What comes back
---------------
A saved DRAFT. Nothing is live until ``dashboard_apply``. Every refusal is a fixed
sentence of :data:`REFUSALS`. The reply and the log carry the gateway's own words, the
field's name once it has passed the name rule, the type once it is one of the catalog's,
and numbers; nothing else the caller sent travels back.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final

from aiohttp import web

from kiro_crew.artifact_store import dashboard_package as _dashboard
from kiro_crew.artifact_store.model import ArtifactConflictError, ArtifactValidationError
from kiro_crew.dashboard.handlers.agent_panel_dashboard import (
    _SESSION_PACKAGE_REFUSAL,
    Composed,
    PageNotStored,
    _credential_shaped_names,
    _instance_body,
    _page_binding,
    _resolve_dashboard_caller,
    _store_bound_package,
)
from kiro_crew.validation import DASHBOARD_ADD_FIELD_SCHEMA, ValidationError, validate_tool_args

logger = logging.getLogger("kiro_crew.dashboard.handlers.agent_panel")

#: The tool and the audit operation: its own name, apart from the save's.
OPERATION: Final[str] = "dashboard_add_field"

#: Where a field this route adds gets its value: from the Bot, always.
AGENTIC_SOURCE: Final[dict[str, bool]] = {"agentic": True}

_NOTHING: Final[str] = "Nothing was written"

#: Where the record cannot tell a true draft from a page he left: no guess, and one way
#: on that is the Bot's own act (a save writes a page the record CAN answer for).
_HISTORY_CANNOT_SAY: Final[str] = (
    "the page's history cannot say whether this stored page was ever live. Save the "
    "page you want once with dashboard_save, then add the field"
)

#: Every refusal of this route: ``code -> (status, sentence)``. A sentence is fixed
#: words; ``{name}``, ``{types}``, ``{count}``, ``{most}`` and ``{limit}`` are filled by
#: :func:`_refusal` with a vetted name, the catalog's own names and numbers.
REFUSALS: Final[dict[str, tuple[int, str]]] = {
    "validation_error": (
        400,
        "dashboard_add_field takes `name` and `type`, and may take `label` and "
        f"`description`: four strings and no other key. {_NOTHING}",
    ),
    "session_package_unsupported": (400, _SESSION_PACKAGE_REFUSAL),
    "bad_field_name": (
        400,
        "`name` is not a field name: lowercase letters, digits and '_', starting with "
        f"a letter, at most 64 characters. {_NOTHING}",
    ),
    "unsafe_declared_name": (
        400,
        "`name` looks like a credential, and a page cannot declare a name a redactor "
        f"would mask. Name the field for a reader, like 'open_prs'. {_NOTHING}",
    ),
    "unknown_type": (
        400,
        f"`type` is not a data type dashboard_add_field takes. It takes: {{types}}. {_NOTHING}",
    ),
    "type_needs_more": (
        400,
        "a field of this type needs keys dashboard_add_field does not take (an enum "
        "needs its choices). Declare it in the model you send to dashboard_save. "
        f"{_NOTHING}",
    ),
    "bad_label": (
        400,
        f"`label` must be one line of 1 to {{limit}} characters, not blank. {_NOTHING}",
    ),
    "bad_description": (
        400,
        f"`description` must be one line of 1 to {{limit}} characters, not blank. {_NOTHING}",
    ),
    "no_page": (
        409,
        f"you have no page of your own to add a field to. {_NOTHING}",
    ),
    "template_live": (
        409,
        "a shipped template is the page in force, and your stored page is one you "
        f"left, so there is no page of your own to add a field to. {_NOTHING}",
    ),
    "history_before_pins": (
        409,
        "this page's record is from before pages were pinned, so "
        f"{_HISTORY_CANNOT_SAY}. {_NOTHING}",
    ),
    "history_not_kept": (
        409,
        "this stored page is older than the versions the record still keeps, so "
        f"{_HISTORY_CANNOT_SAY}. {_NOTHING}",
    ),
    "page_unreadable": (
        503,
        f"your page could not be read just now. {_NOTHING}; call again",
    ),
    "stored_page_invalid": (
        409,
        "the page this field would be added to does not pass the page gate as the "
        "gate stands today. That is not your doing: the gate changed after the page "
        "was stored. dashboard_save the whole page again with the new field in its "
        f"model. {_NOTHING}",
    ),
    "stored_widgets_preview_off": (
        403,
        "your page holds phone widgets and the 'Phone widgets' feature preview is off "
        "on this Crew, so the page is not saved again with them. Ask the person to "
        f"turn the preview on. {_NOTHING}",
    ),
    "field_declared": (409, f"your page already declares `{{name}}`. {_NOTHING}"),
    "model_full": (
        409,
        f"your page declares {{count}} fields and {{most}} is the most a page holds. {_NOTHING}",
    ),
    "page_too_large": (
        409,
        "with this field the page would be over the {limit}-byte limit for a stored "
        f"page. {_NOTHING}",
    ),
    "add_refused": (
        409,
        f"the page with this field did not pass the page gate. {_NOTHING}; "
        "dashboard_save the whole page to read the gate's own reason",
    ),
    "page_changed": (
        409,
        f"your page changed while the field was being added. {_NOTHING}; ask again",
    ),
    "save_failed": (503, f"the page could not be saved just now. {_NOTHING}; call again"),
}

#: Which page the field went onto: ``reply["onto"]`` -> the sentence ``reply["note"]``.
#: Both say what was true AT THE READ the page was built from, and no more: an apply or
#: a rollback takes the record's lock and not the save lock, so by the reply another
#: page can be the live one.
ONTO: Final[dict[str, str]] = {
    "draft": (
        "added to your saved page, which was not live when it was read; "
        "dashboard_apply puts that whole page live"
    ),
    "live": (
        "added to a copy of the page that was live when it was read; "
        "dashboard_apply puts it live"
    ),
}

#: After the draft is saved: the two calls that make the field show a value.
NEXT: Final[str] = (
    "dashboard_apply puts this page live as a new version; then dashboard_write "
    "gives the field its value"
)


class AddRefused(Exception):
    """This add is refused: ``code`` is a key of :data:`REFUSALS`, ``fill`` its numbers."""

    def __init__(self, code: str, **fill: Any) -> None:
        super().__init__(code)
        self.code = code
        self.fill = fill


def addable_types() -> list[str]:
    """The catalog's types a field can have with the universal keys alone, sorted.

    A type that REQUIRES a key of its own (``enum`` and its ``choices``) is left out:
    this route takes no such key, so the field could not pass the gate.
    """
    return sorted(
        name for name, entry in _dashboard.data_type_catalog().items() if not entry.required
    )


def _refusal(code: str, **fill: Any) -> web.Response:
    """The response for *code*: its fixed sentence, filled with gateway-made values only."""
    status, sentence = REFUSALS[code]
    if "{" in sentence:
        sentence = sentence.format(**fill)
    return web.json_response({"error": sentence, "code": code}, status=status)


def _one_line(value: Any) -> bool:
    """Whether *value* is a label the gate takes: the gate's own rule, plus encodable."""
    if not isinstance(value, str) or not value.strip() or len(value) > _dashboard.MAX_LABEL_LEN:
        return False
    if "\n" in value or "\r" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate: the gate refuses the whole package for one.
        return False
    return True


def _written_ms(record: Any) -> int:
    """When the stored record was last written, in ms since the epoch; 0 when it does not say."""
    from datetime import datetime, timezone

    try:
        at = datetime.fromisoformat(
            str(getattr(record, "updated_at", "") or "").replace("Z", "+00:00")
        )
    except ValueError:
        return 0
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return int(at.timestamp() * 1000)


def _vetted(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """``(name, field spec)`` from the validated arguments, or :class:`AddRefused`.

    Everything that can be judged without the stored page, judged before the lock.
    """
    name, type_name = args.get("name"), args.get("type")
    if not isinstance(name, str) or _dashboard._FIELD_NAME_RE.fullmatch(name) is None:
        raise AddRefused("bad_field_name")
    if _credential_shaped_names({"model": {"types": {name: {}}}}):
        raise AddRefused("unsafe_declared_name")
    catalog = _dashboard.data_type_catalog()
    if not isinstance(type_name, str) or type_name not in catalog:
        raise AddRefused("unknown_type", types=", ".join(addable_types()))
    if catalog[type_name].required:
        raise AddRefused("type_needs_more")
    spec: dict[str, Any] = {"type": type_name, "source": dict(AGENTIC_SOURCE)}
    for key in ("label", "description"):
        if key in args and args[key] is not None:
            if not _one_line(args[key]):
                raise AddRefused(f"bad_{key}", limit=_dashboard.MAX_LABEL_LEN)
            spec[key] = args[key]
    return name, spec


def _compose(store: Any, slug: str, name: str, spec: dict[str, Any]) -> tuple[Composed, str]:
    """``(the source page with the field added and the read it was built from, which
    page that was)``, or :class:`AddRefused`. Under the save lock.

    Called by ``_store_bound_package`` inside ``instance.package_save_lock``, so the page
    read here is the page the write replaces. The second value is a key of :data:`ONTO`.
    """
    from kiro_crew import widgets_preview
    from kiro_crew.dashboard_templates import instance as instance_store
    from kiro_crew.dashboard_templates import instance_applied

    binding = _page_binding(slug)
    try:
        record = instance_store.read(slug)
        pinned = instance_store.pinned_package(record)
        applied = instance_applied.applied_pages(slug)
        existing = _dashboard.resolve_bound_slug(binding, store=store)
        # THE ONE READ: the page is built from it and the write is fenced on its token.
        current = store.get(existing, assign_token=True) if existing else None
    except Exception:
        raise AddRefused("page_unreadable") from None
    if pinned is None and record.package_slug:
        # A pin whose snapshot is missing or does not hash to its digest: which page
        # is in force cannot be said.
        raise AddRefused("page_unreadable")
    # ONE SOURCE, WHOLE: his true draft, else his own page in force. Never the model
    # of one with the view of the other, and not a page he left (unless its artifact
    # was renamed, described, tagged or saved again since it was last put live: it then
    # reads as a draft).
    if current is None:
        if pinned is None:
            raise AddRefused("no_page")
        # His page is in force and its artifact is gone: the pinned page is written anew.
        source, onto = pinned.content, "live"
    else:
        try:
            stored = _dashboard.canonical_package_content(str(current.content or ""))
        except ArtifactValidationError:
            raise AddRefused("stored_page_invalid") from None
        digest = instance_applied.page_digest(stored)
        if pinned is not None and digest == record.package_digest:
            # The stored page IS the page in force.
            source, onto = stored, "live"
        elif digest in applied.pins and _written_ms(current) <= applied.pins[digest]:
            # A page he left: it was in force (a kept version pins it, and was written
            # after the page was) and is not now. The same bytes written again since
            # are a new page, and fall through to the draft's rules.
            if pinned is None:
                raise AddRefused("template_live")
            source, onto = pinned.content, "live"
        elif not applied.keyed:
            # A record from before pins says nothing of what was in force. The older
            # rule's own marker does: a version a person cut was (``has_person_cut_version``).
            from kiro_crew.dashboard.handlers.member_dashboard import applied_package_version

            if applied_package_version(current) >= int(current.version):
                raise AddRefused("history_before_pins")
            source, onto = stored, "draft"
        elif _written_ms(current) < applied.tellable_since_ms:
            # Written before the oldest version the record can still answer for.
            raise AddRefused("history_not_kept")
        else:
            # Pinned nowhere and written since: a true draft. It was never in force.
            source, onto = stored, "draft"
    try:
        package = _dashboard.parse_package(source)
    except ArtifactValidationError:
        raise AddRefused("stored_page_invalid") from None
    if package.get("bound_to") != binding:
        raise AddRefused("page_unreadable")
    if package.get("widgets") and not widgets_preview.widgets_enabled():
        raise AddRefused("stored_widgets_preview_off")
    types = dict((package.get("model") or {}).get("types") or {})
    if name in types:
        raise AddRefused("field_declared", name=name)
    if len(types) >= _dashboard.MAX_MODEL_FIELDS:
        raise AddRefused("model_full", count=len(types), most=_dashboard.MAX_MODEL_FIELDS)
    types[name] = spec
    package = {**package, "model": {"types": types}}
    try:
        _dashboard.canonical_package_content(json.dumps(package, ensure_ascii=False))
    except ArtifactValidationError:
        # The page alone passed and the field's own keys were vetted: what is left is
        # the size of the stored form, which is measured here to say so.
        written = json.dumps(package, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if len(written.encode("utf-8", "replace")) > _dashboard.MAX_PACKAGE_BYTES:
            raise AddRefused("page_too_large", limit=_dashboard.MAX_PACKAGE_BYTES) from None
        raise AddRefused("add_refused") from None
    return (
        Composed(package, existing or "", current.content_token if current is not None else None),
        onto,
    )


async def api_dashboard_add_field(request: web.Request) -> web.Response:
    """POST /api/agent-panel/dashboard/add-field -- add one hand-written field, as a draft.

    See the module docstring. Answers ``{"ok", "added": {"field", "type"}, "onto",
    "note", "saved": {"package", "package_version", "fields"}, "verification", "next"}``,
    or a refusal of :data:`REFUSALS`.
    """
    resolved, refusal = await _resolve_dashboard_caller(request, OPERATION)
    if refusal is not None:
        return refusal
    assert resolved is not None
    slug, crew_name, slot = resolved
    body = await _instance_body(request)
    if isinstance(body, web.Response):
        return body
    try:
        args = validate_tool_args(body, DASHBOARD_ADD_FIELD_SCHEMA)
    except ValidationError:
        # A fixed sentence: the schema's own names the key it refused, and an
        # unknown key is the caller's text.
        return _refusal("validation_error")
    if not crew_name:
        # What ``dashboard_save`` answers a root session, word for word.
        return _refusal("session_package_unsupported")
    try:
        name, spec = _vetted(args)
    except AddRefused as refused:
        logger.info("%s for %s refused: %s", OPERATION, slug, refused.code)
        return _refusal(refused.code, **refused.fill)
    went_onto: list[str] = []

    def _composed(store: Any) -> Composed:
        composed, onto = _compose(store, slug, name, spec)
        went_onto.append(onto)
        return composed

    try:
        stored = await _store_bound_package(
            slug, crew_name, slot, widgets_sent=True, compose=_composed
        )
    except PageNotStored as failed:
        cause = failed.cause
        if isinstance(cause, AddRefused):
            logger.info("%s for %s refused: %s", OPERATION, slug, cause.code)
            return _refusal(cause.code, **cause.fill)
        # NO RETRY. The other writer takes no save lock, so a second try under this
        # lock could meet a third write; one refusal that says "ask again" cannot loop.
        code = (
            "page_changed"
            if isinstance(cause, ArtifactConflictError)
            else "add_refused" if isinstance(cause, ArtifactValidationError) else "save_failed"
        )
        # The kind of failure only: a store's message can quote what it was writing.
        logger.warning("%s for %s failed: %s (%s)", OPERATION, slug, code, type(cause).__name__)
        return _refusal(code)

    status = (stored.verification or {}).get("status")
    # NO BROADCAST: the page in force has not changed. `dashboard_apply` puts it live.
    return web.json_response(
        {
            "ok": True,
            "added": {"field": name, "type": spec["type"]},
            # Which whole page the field went onto, and so what an apply puts live.
            "onto": went_onto[-1],
            "note": ONTO[went_onto[-1]],
            "saved": {
                "package": stored.art.slug,
                "package_version": int(stored.art.version),
                **stored.counts,
            },
            # The page check's verdict alone: what a page raised is the page's text.
            "verification": status if isinstance(status, str) else None,
            "next": NEXT,
        }
    )
