"""Who may Edit & Resend a user message, or rewind to it.

Both rewrite the session for everyone in it, so on a Shared Crew only the
message's author may do either:

* A row by a person (``human`` or ``machine``) is that person's: the editor's
  :func:`~kiro_crew.shared_crew.owner.writes_as_ids` must hold its id, so the
  owner writes as each of their ids and an Unknown caller only as Unknown.
* A row with no author (written before authors), a damaged or Unknown author
  (a channel row, a merged queue row) or an automation author is the owner's;
  an app may also edit a row its own principal wrote.
* With the Shared Crew preview off only a pair where the editor or the author is
  a relay teammate is judged; every other edit is allowed as before, so the
  owner's own paths (this machine, Tailscale, the owner over the relay) are
  never refused. A row is a relay teammate's only when its stamp names the
  ``kiro`` provider (``attribution.author_stamp``); a row stamped before the
  provider was recorded never is.

The stored author is read ONLY to refuse: every outcome here is one main
allowed before this rule, so a forged stamp can at most hand the edit back to
someone main already let edit. ``test/fixtures/shared_crew_edit_rule.json``
drives this module's tests and the dashboard's ``website/src/api/editRule.ts``,
which hides the edit controls by the same rule.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from kiro_crew.shared_crew import owner, preview
from kiro_crew.shared_crew.attribution import message_author
from kiro_crew.shared_crew.contracts import MESSAGE_AUTHOR_META_KEY, PrincipalKind
from kiro_crew.shared_crew.identity import PROVIDER_KIRO, viewer_principal

#: The refusal's machine-readable code; the dashboard shows it on the refused-press notice.
NOT_MESSAGE_AUTHOR = "not_message_author"

_PERSON_KINDS = (PrincipalKind.HUMAN, PrincipalKind.MACHINE)


@dataclass(frozen=True)
class Editor:
    """What the rule needs to know about the caller, derived from the published actor."""

    #: Every principal id the caller writes as (``owner.writes_as_ids``).
    ids: frozenset[str]
    is_owner: bool
    #: A teammate the Kiro relay verified (``routes_identity._relay_teammate``).
    relay_teammate: bool


def may_edit(editor: Editor, row_meta: Mapping[str, Any] | None, *, preview_on: bool) -> bool:
    """Whether *editor* may edit or rewind to the user row whose ``meta`` is *row_meta*."""
    raw = row_meta.get(MESSAGE_AUTHOR_META_KEY) if isinstance(row_meta, Mapping) else None
    author = message_author(row_meta)
    person = author.kind in _PERSON_KINDS
    author_relay = person and isinstance(raw, Mapping) and raw.get("provider") == PROVIDER_KIRO
    if not preview_on and not (editor.relay_teammate or author_relay):
        return True
    if person:
        return author.id in editor.ids
    return editor.is_owner or (author.kind is PrincipalKind.SYSTEM and author.id in editor.ids)


def editor_of(request: web.Request) -> Editor:
    """The :class:`Editor` for *request*'s published actor (Unknown when none was published)."""
    from kiro_crew.shared_crew.routes_identity import _owner_id, _relay_teammate

    principal = viewer_principal(request)
    owner_id = _owner_id(request)
    return Editor(
        ids=owner.writes_as_ids(principal, owner_id),
        is_owner=owner.is_owner_principal(principal, owner_id),
        relay_teammate=_relay_teammate(request),
    )


def edit_rule_view(request: web.Request) -> dict[str, bool]:
    """The rule's inputs about the viewer, for the mode route's ``edit_rule``.

    The dashboard pairs it with ``viewer_ids`` to hide the edit controls. Display
    only: the gateway decides with :func:`refuse_unless_author`.
    """
    editor = editor_of(request)
    return {
        "preview": preview.preview_enabled(),
        "is_owner": editor.is_owner,
        "relay_teammate": editor.relay_teammate,
    }


def refuse_unless_author(request: web.Request, row: Mapping[str, Any]) -> web.Response | None:
    """The 403 for an edit or rewind of *row* by someone else, else ``None``."""
    meta = row.get("meta")
    if may_edit(
        editor_of(request),
        meta if isinstance(meta, Mapping) else None,
        preview_on=preview.preview_enabled(),
    ):
        return None
    return web.json_response(
        {
            "error": "only the message's author can edit it or rewind to it",
            "code": NOT_MESSAGE_AUTHOR,
        },
        status=403,
    )


__all__ = [
    "NOT_MESSAGE_AUTHOR",
    "Editor",
    "edit_rule_view",
    "editor_of",
    "may_edit",
    "refuse_unless_author",
]
