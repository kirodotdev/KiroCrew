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

The owner may also edit the list (:func:`site_change`, through the owner-only route
in ``browser_sites_routes``): add a site the app never declared, checked by the rule
a declared entry meets; remove one; or turn one off. Those edits live in the
gateway-only ``app-browser-sites`` record (``protected_app_state``), never in the
app's tree, and :func:`app_browser_sites` reads them with the approvals.

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

# The calls the row never approves (each asks the person on the card) follow the
# rule a person's site grant follows too.
from kiro_crew.browser_harness.op_classes import card_only

logger = logging.getLogger(__name__)

#: The manifest key under ``permissions``, and the grant family that stores it.
GRANT_FAMILY = "browserSites"
BROWSER_SITE_GRANT_FAMILIES = (GRANT_FAMILY,)
#: Most entries one manifest may declare.
MAX_BROWSER_SITES = 16
#: Most sites the owner may add to one app (``site_change``), approvals of declared
#: sites included.
MAX_OWNER_SITES = 2 * MAX_BROWSER_SITES
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
    if op in ("navigate", "http_get"):
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
      other URL falls through to the card, and the op route refuses it anyway;
    * the call is not :func:`card_only` (``js``, ``cdp``, ``http_get``, ``tabs
      close``), which always asks, nor ``upload``, which only a person's own site
      grant covers.

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
    op_args = call.raw_params.get("args")
    op = str(call.raw_params.get("op"))
    if (
        not readable
        or op == "upload"
        or card_only(op, op_args if isinstance(op_args, dict) else {})
    ):
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


def _owner_record(app: str) -> dict[str, list[str]]:
    """The owner's edits of *app*'s current install (``protected_app_state``).

    Raises ``ValueError`` when no install record reads back: the app's sandbox can
    move ``installed.json`` aside, and an empty record would undo every removal and
    turn-off the owner made.
    """
    from kiro_crew.apps import protected_app_state  # noqa: PLC0415
    from kiro_crew.apps.manager import _read_installed  # noqa: PLC0415

    meta = _read_installed(app)
    if meta is None:
        raise ValueError("the app's install record could not be read")
    return protected_app_state.read_owner_browser_sites(app, meta.installedAt)


def _owner_added(record: dict[str, list[str]]) -> list[str]:
    """The record's added entries that pass :func:`entry_refusal`, at most the cap."""
    valid = [e for e in record["added"] if entry_refusal(e) is None]
    return valid[:MAX_OWNER_SITES]


def _site_state(app: str, declared: list[str]) -> tuple[list[str], list[str], dict[str, list[str]]]:
    """``(valid declared, approved declared, owner record)`` for *app*, read fresh."""
    return _site_state_from(app, declared, _owner_record(app))


def effective_sites(
    declared: list[str], approved: list[str], record: dict[str, list[str]]
) -> list[str]:
    """The pages in force: approved declared ones the owner kept, plus the owner's own.

    A site the owner removed or turned off is never in force, whoever listed it.
    """
    removed, disabled = set(record["removed"]), set(record["disabled"])
    kept = [e for e in approved if e not in removed]
    out = list(dict.fromkeys([*kept, *_owner_added(record)]))
    return [e for e in out if e not in disabled]


def app_browser_sites(app: str) -> tuple[str, ...]:
    """The pages *app*'s own agent may browse unasked right now. Blocking; fails closed.

    The declared entries the owner approved and did not remove or turn off, and the
    entries the owner added (``site_change``) and did not turn off. Empty unless the
    app is enabled and trusted to execute, and empty when the owner's record cannot
    be read.
    """
    if not isinstance(app, str) or not app:
        return ()
    try:
        from kiro_crew.apps.execution import third_party_ceiling_closed  # noqa: PLC0415
        from kiro_crew.apps.manager import get_app_manifest, is_app_enabled  # noqa: PLC0415

        manifest = get_app_manifest(app)
        if manifest is None:
            return ()
        sites = effective_sites(*_site_state(app, _declared(manifest.permissions)))
        if not sites or not is_app_enabled(app):
            return ()
        if third_party_ceiling_closed(app) is not None:
            return ()
        return tuple(sites)
    except Exception:  # noqa: BLE001 - an unreadable grant grants nothing
        logger.warning("could not resolve the browser sites of an app", exc_info=True)
        return ()


#: A row's ``source``: the app's manifest declares it, or the owner added it.
SOURCE_APP = "app"
SOURCE_OWNER = "owner"
#: A row's ``status``. ``pending`` is a declared site the owner has not approved.
STATUS_APPROVED = "approved"
STATUS_PENDING = "pending"
STATUS_DISABLED = "disabled"
#: A declared site the owner removed: listed so the owner can add it back, never in force.
STATUS_REMOVED = "removed"


def site_rows(
    declared: list[str], approved: list[str], record: dict[str, list[str]]
) -> list[dict[str, str]]:
    """Every site the owner can see for an app: ``{site, source, status}``, declared first.

    A declared site the owner removed is listed in its declared place as ``removed``.
    """
    removed, disabled = set(record["removed"]), set(record["disabled"])
    added = _owner_added(record)
    granted = set(approved) | set(added)
    rows: list[dict[str, str]] = []
    for site in dict.fromkeys([*declared, *added]):
        if site in removed and site not in added:
            status = STATUS_REMOVED
        elif site in disabled:
            status = STATUS_DISABLED
        else:
            status = STATUS_APPROVED if site in granted else STATUS_PENDING
        source = SOURCE_APP if site in declared else SOURCE_OWNER
        rows.append({"site": site, "source": source, "status": status})
    return rows


def app_site_rows(app: str) -> list[dict[str, str]] | None:
    """:func:`site_rows` for installed *app*, or None when it is not installed. Raises on a bad record."""
    from kiro_crew.apps.manager import get_app_manifest  # noqa: PLC0415

    manifest = get_app_manifest(app)
    if manifest is None:
        return None
    return site_rows(*_site_state(app, _declared(manifest.permissions)))


#: What the owner's route may do to one site. Only ``add`` widens what the app's agent
#: may open; the dashboard asks the owner before it sends one.
SITE_ACTIONS = ("add", "remove", "disable")


class SiteChangeError(Exception):
    """A refused :func:`site_change`: ``code`` for the client, the message for the person."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def site_change(app: str, action: str, site: str) -> list[dict[str, str]]:
    """Apply the owner's *action* to *site* for installed *app*; the rows after it.

    ``add`` puts *site* in force: a new pattern is checked by :func:`entry_refusal`,
    the rule a manifest entry meets, and a declared one is approved by it. It also
    turns a turned-off or removed site back on. ``remove`` takes a site off the list (a
    declared one stays off across updates, listed as ``removed``); ``disable`` keeps it
    listed but out of force. Neither acts on a removed row (``not_listed``). Both
    end at the next browser op, the way a withdrawn approval does. Raises
    :class:`SiteChangeError`. Callers authorize the owner first: nothing here checks
    who asks. The app's record lock spans the install check and the write, so an
    uninstall cannot land between them, and the record is stored for that install.
    """
    from kiro_crew.apps.manager import (  # noqa: PLC0415
        _read_installed,
        app_record_lock,
        get_app_manifest,
    )

    if action not in SITE_ACTIONS:
        raise SiteChangeError("bad_action", f"action must be one of {', '.join(SITE_ACTIONS)}")
    if not isinstance(site, str) or not site.strip():
        raise SiteChangeError("bad_site", "site is required")
    site = site.strip()
    with app_record_lock(app):
        meta = _read_installed(app)
        manifest = get_app_manifest(app) if meta is not None else None
        if meta is None or manifest is None:
            raise SiteChangeError("not_found", "app is not installed")
        return _edit_record(app, meta.installedAt, _declared(manifest.permissions), action, site)


def _edit_record(
    app: str, installed_at: str, declared: list[str], action: str, site: str
) -> list[dict[str, str]]:
    """:func:`site_change`'s edit of the owner record, under the app's record lock."""
    from kiro_crew.apps import protected_app_state  # noqa: PLC0415

    with protected_app_state.owner_browser_sites_update(app, installed_at) as record:
        listed = {
            row["site"]
            for row in site_rows(*_site_state_from(app, declared, record))
            if row["status"] != STATUS_REMOVED
        }
        if action == "add":
            refusal = entry_refusal(site)
            if refusal is not None:
                raise SiteChangeError("bad_site", f"site is {refusal}")
            if site not in record["added"] and len(record["added"]) >= MAX_OWNER_SITES:
                raise SiteChangeError("too_many", f"at most {MAX_OWNER_SITES} added sites per app")
            if site not in record["added"]:
                record["added"].append(site)
            record["removed"] = [e for e in record["removed"] if e != site]
            record["disabled"] = [e for e in record["disabled"] if e != site]
        elif site not in listed:
            raise SiteChangeError("not_listed", "that site is not on this app's list")
        elif action == "remove":
            record["added"] = [e for e in record["added"] if e != site]
            record["disabled"] = [e for e in record["disabled"] if e != site]
            if site in declared and site not in record["removed"]:
                record["removed"].append(site)
        elif site not in record["disabled"]:
            record["disabled"].append(site)
        rows = site_rows(*_site_state_from(app, declared, record))
    return rows


def _site_state_from(
    app: str, declared: list[str], record: dict[str, list[str]]
) -> tuple[list[str], list[str], dict[str, list[str]]]:
    """``(valid declared, approved declared, record)`` with the owner record given.

    A declaration that fails validation counts as none: ``app.json`` is in the app's
    own writable tree, so it is re-checked on every read.
    """
    if not declared or browser_site_errors(declared):
        return [], [], record
    return declared, _approved(app, declared), record


def browser_sites_grant_row(name: str, manifest: Any) -> dict[str, Any]:
    """The ``browserSitesGrant`` key of an app row, absent when nothing is declared.

    ``approved`` lists the declared entries the owner's approved set holds;
    ``staged`` the declared ones it does not, which the detail page offers for
    approval. Nothing is staged while the declaration is invalid, since the readers
    then admit none of it, nor a site the owner has already ruled on: removed,
    turned off or added through :func:`site_change`.
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
    try:
        record = _owner_record(name)
        held = {*record["removed"], *record["disabled"], *record["added"]}
    except (OSError, ValueError):
        # The owner's rulings are unknown, so nothing is offered for approval.
        logger.warning("could not read the owner's browser sites of an app", exc_info=True)
        valid = False
        held = set()
    staged = [e for e in declared if e not in approved and e not in held] if valid else []
    return {"browserSitesGrant": {"approved": approved, "staged": staged}}


__all__ = [
    "BROWSER_SITE_GRANT_FAMILIES",
    "GRANT_FAMILY",
    "MAX_BROWSER_SITES",
    "MAX_OWNER_SITES",
    "NO_PAGES",
    "SITE_ACTIONS",
    "SiteChangeError",
    "app_site_rows",
    "effective_sites",
    "site_change",
    "site_rows",
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
