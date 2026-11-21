"""What an app token is told when the API scope check refuses it.

The scope check (``token_auth_app_scope._enforce_app_scope``) refuses an app token
on a path its approved ``permissions.api`` entries do not cover. The cause decides
who can clear it, so the refusal names the cause and the steps:

- ``app_permission_not_approved``: an entry the installed manifest declares
  covers the path, but the owner has not approved it yet, which is how an update
  that adds an entry leaves it. The owner approves it on the app's detail page.
- ``app_permission_not_declared``: no declared entry covers the path. The app's
  developer adds one to ``app.json``, the installed app is updated, and the owner
  approves the new entry.
- ``app_permission_unavailable``: the grant could not be read, or an update of the
  app is swapping its files. Nothing to change; a retry clears it.

Pure: the caller reads the app's grant beforehand, together with its allowlist, so
building a refusal never touches the disk on the request path.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Sequence

NOT_APPROVED = "app_permission_not_approved"

NOT_DECLARED = "app_permission_not_declared"

UNAVAILABLE = "app_permission_unavailable"

# The button on the app detail page that approves held-back entries
# (``pages.appDetailPage.approve_new_permissions``).
_APPROVE_BUTTON = "Approve new permissions"

# A display name comes from the app's own manifest, so it is reduced to one line
# of visible text before it is quoted. These Unicode categories are dropped: Cc
# (control), Cf (format: bidi overrides, zero-width marks, BOM, soft hyphen, tag
# characters) and Zl/Zp (line and paragraph separators). Whitespace runs collapse.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})

_LABEL_MAX_CHARS = 80


@dataclass(frozen=True)
class GrantFacts:
    """An app's ``permissions.api`` grant as read beside its allowlist.

    ``declared`` is ``None`` when the manifest could not be read. ``held_back``
    lists the declared entries the owner has not approved yet.
    """

    label: str
    declared: tuple[str, ...] | None
    held_back: tuple[str, ...]


@dataclass(frozen=True)
class ScopeRefusal:
    """The body fields of one scope refusal."""

    code: str
    message: str
    pending_approval: tuple[str, ...]


def app_label(app_name: str, display_name: object) -> str:
    """*display_name* as one short line of visible text, or *app_name* when none is left."""
    text = display_name if isinstance(display_name, str) else ""
    visible = "".join(ch for ch in text if unicodedata.category(ch) not in _INVISIBLE_CATEGORIES)
    cleaned = " ".join(visible.split())
    if len(cleaned) > _LABEL_MAX_CHARS:
        cleaned = cleaned[: _LABEL_MAX_CHARS - 1].rstrip() + "…"
    return cleaned or app_name


def _quoted(entries: Sequence[str]) -> str:
    return ", ".join(f'"{entry}"' for entry in entries)


def scope_refusal(
    app_name: str,
    label: str,
    path: str,
    *,
    pending_approval: Sequence[str],
    declared: bool | None,
    approval_delay_secs: int,
) -> ScopeRefusal:
    """Word the refusal of *app_name*'s token on *path*.

    *pending_approval* holds the held-back entries that cover *path*. *declared*
    says whether any declared entry covers it, ``None`` when the manifest could
    not be read. *approval_delay_secs* is how long an approval can take to reach
    the scope check, which caches each app's allowlist.
    """
    app_page = f"/apps/detail/{app_name}"
    pending = tuple(pending_approval)
    if pending:
        noun = "permission" if len(pending) == 1 else "permissions"
        remedy = (
            f"To fix this, the dashboard owner opens the app's page (Apps, Library, then "
            f'Details on {label}, or {app_page}) and clicks "{_APPROVE_BUTTON}". '
            f"If the app is off, turn it on there first. The approval takes effect "
            f"within {approval_delay_secs} seconds."
        )
        message = (
            f'App "{label}" cannot call {path} yet. Its installed version asks for the '
            f"new API {noun} {_quoted(pending)}, which the dashboard owner has not "
            f"approved. {remedy}"
        )
        return ScopeRefusal(NOT_APPROVED, message, pending)
    if declared is False:
        remedy = (
            f'To fix this, add "{path}", or a pattern that covers it, to permissions.api '
            f"in the app's app.json, and install that version with kirocrew app update "
            f"{app_name}. An app that registers itself registers its new manifest again "
            f"instead. The dashboard owner then approves the new permission on the app's "
            f'page ({app_page}) with "{_APPROVE_BUTTON}".'
        )
        message = (
            f'App "{label}" cannot call {path}: no entry in the permissions.api list of '
            f"its installed app.json covers this path. {remedy}"
        )
        return ScopeRefusal(NOT_DECLARED, message, ())
    remedy = (
        f"Retry in a moment. If this keeps happening, check the app with: "
        f"kirocrew app info {app_name}"
    )
    message = (
        f'App "{label}" cannot call {path} right now: its permissions could not be '
        f"read, or an update of the app is in progress. {remedy}"
    )
    return ScopeRefusal(UNAVAILABLE, message, ())
