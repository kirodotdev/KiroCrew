"""Browser sites: the pages an app's OWN agent may drive Crew's browser on, unasked.

An app whose agent works a website for the owner (searches it, opens a result,
plays it) would otherwise meet an approval card on every ``browser`` call, and the
card's "Always allow" is refused for Kiro Crew's own tools. An app names the pages
it needs in ``permissions.browserSites``, each an exact ``https://<hostname>``
origin followed by a path pattern: ``https://www.youtube.com/watch``,
``https://www.bilibili.com/video/*``. ``*`` matches any run of characters,
``/`` included; everything else matches itself. The query and the fragment are
never compared, so ``/results`` admits ``/results?search_query=...``.

Each entry is an entry of the :data:`GRANT_FAMILY` family in the app's
approved-grants set. Unlike most families, an install never approves it
(``manager_records._OWNER_APPROVED_FAMILIES``): the owner approves the sites on
the app's page, and an update that adds one waits there too. The family is
explicit-only, so a set stored before this host knew the key approves none.

:func:`app_browser_sites` is the one reader both enforcement points use, read
fresh on every call so a disable, an uninstall, a withdrawn trust grant or an
update that drops a site ends it at the next call:

* the tool gate's ``app-browser-sites`` row (``hooks._tier_app_browser_sites``, which calls :func:`gate_grants`)
  approves the ``browser`` tool for the app's own sessions while it is non-empty;
* the browser op route (``dashboard/handlers/browser_ops.py``) holds those
  sessions to the approved pages, so a page off the list is refused and blanked.

Nothing here names an app.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any
from urllib.parse import urlsplit

from kiro_crew.apps.app_hosts import frame_origin_refusal

logger = logging.getLogger(__name__)

#: The manifest key under ``permissions``, and the grant family that stores it.
GRANT_FAMILY = "browserSites"
BROWSER_SITE_GRANT_FAMILIES = (GRANT_FAMILY,)
#: Most entries one manifest may declare.
MAX_BROWSER_SITES = 16
#: Longest entry, origin and path pattern together.
MAX_ENTRY_LENGTH = 256
#: A path pattern: ``/`` then RFC 3986 path characters and ``*``. No ``%``, ``?``
#: or ``#``, so an entry means one thing and is compared against a raw path.
_PATH_RE = re.compile(r"/[A-Za-z0-9._~!$&'()+,;=:@/*-]*")
#: Escapes that would let a raw path say ``.``, ``/`` or ``\\`` without spelling it.
_ENCODED_SEPARATORS = ("%2e", "%2f", "%5c")


def declared_sites(value: Any) -> list[str]:
    """The string entries of a raw ``permissions.browserSites`` value, in order."""
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(e for e in value if isinstance(e, str) and e))


def _split(entry: str) -> tuple[str, str]:
    """``(origin, path pattern)`` of an entry; the path is ``""`` when there is none."""
    rest = entry[len("https://") :] if entry.startswith("https://") else ""
    host, slash, path = rest.partition("/")
    return f"https://{host}", f"{slash}{path}"


#: The separator of a URL path: never a filesystem one.
_URL_SEP = "/"


def _dot_segment(path: str) -> bool:
    return any(seg in (".", "..") for seg in path.split(_URL_SEP))


def entry_refusal(entry: str) -> str | None:
    """Why *entry* cannot be a ``browserSites`` entry, or None when it can."""
    if len(entry) > MAX_ENTRY_LENGTH:
        return f"longer than {MAX_ENTRY_LENGTH} characters"
    origin, path = _split(entry)
    refusal = frame_origin_refusal(origin)
    if refusal is not None:
        return refusal
    if not path or not _PATH_RE.fullmatch(path):
        return "not an origin followed by a path pattern starting with / (no query or fragment)"
    if "//" in path or _dot_segment(path):
        return "a path pattern with an empty, . or .. segment"
    return None


def browser_site_errors(value: Any) -> list[str]:
    """Validation errors of ``permissions.browserSites``; empty when valid or absent."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(e, str) for e in value):
        return ["permissions.browserSites must be an array of strings"]
    errors: list[str] = []
    if len(value) > MAX_BROWSER_SITES:
        errors.append(
            f"permissions.browserSites: {len(value)} entries exceeds the limit of "
            f"{MAX_BROWSER_SITES}"
        )
    for entry in value:
        refusal = entry_refusal(entry)
        if refusal is not None:
            errors.append(f"permissions.browserSites entry is {refusal}: {entry!r}")
    if len(set(value)) != len(value):
        errors.append("permissions.browserSites repeats an entry")
    return errors


def _glob_matches(pattern: str, path: str) -> bool:
    """Whether *path* is *pattern* with each ``*`` standing for any run of characters.

    Linear, with no backtracking: the literal pieces are found left to right, each at
    its first place after the one before, which is the leftmost match a glob of ``*``
    alone needs. A landing URL is page-chosen, so the matcher's cost must not grow
    with how its stars line up against it.
    """
    pieces = pattern.split("*")
    if len(pieces) == 1:
        return path == pattern
    head, tail = pieces[0], pieces[-1]
    if len(path) < len(head) + len(tail) or not path.startswith(head) or not path.endswith(tail):
        return False
    pos, end = len(head), len(path) - len(tail)
    for piece in pieces[1:-1]:
        found = path.find(piece, pos, end)
        if found < 0:
            return False
        pos = found + len(piece)
    return True


def url_matches(sites: tuple[str, ...] | list[str], url: str) -> bool:
    """Whether *url* is a page one of *sites* names. Fails closed on anything odd.

    https only, no user info, no port, the exact host, and a raw path with no dot
    segment and no escaped separator, so the path compared is the path the browser
    loads.
    """
    if not isinstance(url, str) or not sites:
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or "@" in parts.netloc or port is not None:
        return False
    host = (parts.hostname or "").rstrip(".")
    path = parts.path or "/"
    lowered = path.lower()
    if "\\" in path or _dot_segment(path) or any(e in lowered for e in _ENCODED_SEPARATORS):
        return False
    for entry in sites:
        origin, pattern = _split(entry)
        if origin[len("https://") :] == host and _glob_matches(pattern, path):
            return True
    return False


#: The server and tool this row approves, and nothing else.
BROWSER_SERVER = "kirocrew-core"
BROWSER_TOOL = "browser"


def browser_call_url(params: Any) -> tuple[bool, str | None]:
    """``(readable, url)``: the URL the call would open, ``None`` when it opens none.

    The same reading as the shim's navigate gate (``mcp_tools/browser._gated_url``).
    Unreadable params are ``(False, None)``: the row cannot say what the call does.
    """
    if not isinstance(params, dict):
        return False, None
    op = params.get("op")
    args = params.get("args") or {}
    if not isinstance(op, str) or not isinstance(args, dict):
        return False, None
    if op == "navigate":
        url = args.get("url")
        return isinstance(url, str), url if isinstance(url, str) else None
    if op == "tabs" and args.get("action") == "new":
        url = args.get("url", "about:blank")
        if not isinstance(url, str):
            return False, None
        return True, None if url == "about:blank" else url
    return True, None


#: Slot keys the tool gate approved a ``browser`` call for, in this process. The op
#: route holds such a slot to its sites even once they are gone (no page at all), so a
#: call approved on a grant that was revoked, or on a manifest edited, before the call
#: reached the route never runs unconfined.
_granted_lock = threading.Lock()
_granted: dict[str, None] = {}
#: Most slot keys remembered; the oldest go first.
_GRANTED_CAP = 4096
#: What a held session's sites are once none is approved: an entry no URL matches.
NO_PAGES: tuple[str, ...] = ("",)


def note_granted_session(slot_key: str) -> None:
    """Remember that the gate approved a ``browser`` call for dashboard slot *slot_key*."""
    if not slot_key:
        return
    with _granted_lock:
        _granted.pop(slot_key, None)
        _granted[slot_key] = None
        while len(_granted) > _GRANTED_CAP:
            del _granted[next(iter(_granted))]


def session_was_granted(*slot_keys: str) -> bool:
    """Whether the gate approved a ``browser`` call for any of *slot_keys*."""
    with _granted_lock:
        return any(key in _granted for key in slot_keys if key)


def gate_grants(facts: Any) -> bool:
    """Whether the tool gate's ``app-browser-sites`` row approves this call.

    The row (``hooks._tier_app_browser_sites``) imports this module only when a call
    reaches it, so nothing here loads before an MCP server lists its tools. Every
    condition fails toward the card:

    * the caller is the app's own dashboard slot: ``facts.app`` is the slot's ``_app``,
      which only a request with an authenticated app scope sets, and the session key
      is a ``dashboard:`` slot key, the kind the op route resolves and confines. A
      person's chat, a cron, another app, and a subagent the app spawned (it carries
      the app but no slot the route can find) keep their card;
    * the call is ``@kirocrew-core/browser`` by its VERIFIED identity, never by title;
    * no operator ``confirm_tools`` pattern names it;
    * the app holds approved sites right now (read fresh);
    * a call that names a URL (``navigate``, ``tabs`` ``new``) names one of them; any
      other URL falls through to the card, and the op route refuses it anyway.

    A granted slot is noted (:func:`note_granted_session`) before the approval leaves,
    so the op route holds it to its sites even if they are revoked before the call
    reaches it. ``classifier_only`` is the row's own check.
    """
    call = facts.call
    if not facts.app or not facts.session_key.startswith("dashboard:"):
        return False
    if not (
        call.identity_trusted
        and call.mcp_server == BROWSER_SERVER
        and call.mcp_tool == BROWSER_TOOL
        and facts.canonical_mcp_name
    ):
        return False
    readable, url = browser_call_url(call.raw_params)
    if not readable:
        return False
    # Call time: the facade imports nothing from here at module level.
    from kiro_crew import hooks  # noqa: PLC0415

    ref = hooks.mcp_identity_ref(call.mcp_server, call.mcp_tool)
    if hooks._operator_confirms(facts, (f"Running: {ref}", ref)):
        return False
    sites = app_browser_sites(facts.app)
    if not sites or (url is not None and not url_matches(sites, url)):
        return False
    note_granted_session(facts.session_key[len("dashboard:") :])
    return True


def _declared(permissions: Any) -> list[str]:
    """The entries a manifest's ``permissions`` (parsed or a raw dict) declare."""
    if isinstance(permissions, dict):
        return declared_sites(permissions.get(GRANT_FAMILY))
    return declared_sites(getattr(permissions, GRANT_FAMILY, None))


def browser_site_grant_entries(permissions: Any) -> dict[str, list[str]]:
    """The :data:`GRANT_FAMILY` entries a manifest's ``permissions`` declare."""
    return {GRANT_FAMILY: _declared(permissions)}


def _approved(app: str, declared: list[str]) -> list[str]:
    """The *declared* entries in *app*'s approved-grants set, read fresh."""
    if not declared:
        return []
    # Deferred: the facade's family imports ``point_grants``, which imports this module.
    from kiro_crew.apps.manager import staged_app_grants  # noqa: PLC0415

    approved = set(staged_app_grants(app, GRANT_FAMILY, lambda: declared))
    return [entry for entry in declared if entry in approved]


def app_browser_sites(app: str) -> tuple[str, ...]:
    """The pages *app*'s own agent may browse unasked right now. Blocking; fails closed.

    Empty unless the live manifest declares a valid ``browserSites``, the owner
    approved the entries returned, the app is enabled, and it is trusted to execute.
    """
    if not isinstance(app, str) or not app:
        return ()
    try:
        from kiro_crew.apps.execution import third_party_ceiling_closed  # noqa: PLC0415
        from kiro_crew.apps.manager import get_app_manifest, is_app_enabled  # noqa: PLC0415

        manifest = get_app_manifest(app)
        if manifest is None:
            return ()
        declared = _declared(manifest.permissions)
        # Re-validated: app.json is in the app's own writable tree.
        if not declared or browser_site_errors(declared):
            return ()
        approved = _approved(app, declared)
        if not approved or not is_app_enabled(app):
            return ()
        if third_party_ceiling_closed(app) is not None:
            return ()
        return tuple(approved)
    except Exception:  # noqa: BLE001 - an unreadable grant grants nothing
        logger.warning("could not resolve the browser sites of an app", exc_info=True)
        return ()


def browser_sites_grant_row(name: str, manifest: Any) -> dict[str, Any]:
    """The ``browserSitesGrant`` key of an app row, absent when nothing is declared.

    ``approved`` lists the declared entries the owner's approved set holds;
    ``staged`` the declared ones it does not, which the detail page offers for
    approval. Nothing is staged while the declaration is invalid, since the readers
    then admit none of it.
    """
    permissions = (
        manifest.get("permissions")
        if isinstance(manifest, dict)
        else getattr(manifest, "permissions", None)
    )
    declared = _declared(permissions)
    if not declared:
        return {}
    approved = _approved(name, declared)
    valid = not browser_site_errors(declared)
    staged = [e for e in declared if e not in approved] if valid else []
    return {"browserSitesGrant": {"approved": approved, "staged": staged}}


__all__ = [
    "BROWSER_SITE_GRANT_FAMILIES",
    "GRANT_FAMILY",
    "MAX_BROWSER_SITES",
    "NO_PAGES",
    "note_granted_session",
    "session_was_granted",
    "BROWSER_SERVER",
    "BROWSER_TOOL",
    "app_browser_sites",
    "gate_grants",
    "browser_call_url",
    "browser_site_errors",
    "browser_site_grant_entries",
    "browser_sites_grant_row",
    "declared_sites",
    "entry_refusal",
    "url_matches",
]
