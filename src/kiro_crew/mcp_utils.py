"""Pure helpers for MCP server-key handling, shared without circular imports.

Lives in its own module (rather than ``agent.py``) so both ``agent.py`` and
``dashboard/handlers/mcp.py`` can import it at the top level -- ``agent`` imports
the handlers module, so a helper defined in ``agent`` could only be reached from
the handlers via an in-function import.  Keeping it dependency-free here removes
that workaround.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

#: Kiro Crew's internal names for the two OAuth hints on a remote MCP entry.
#: These are the names used in ``mcp.json``, the custom-server API and the UI
#: types.  They are NOT the names kiro-cli parses -- see
#: :func:`kiro_oauth_wire_entry` for the translation and why it matters.
INTERNAL_SCOPES_KEY = "scopes"
INTERNAL_CLIENT_ID_KEY = "clientId"

#: The names kiro-cli's ``RemoteMcpServerConfig`` / ``OAuthConfig`` actually
#: deserialize.  Anything else in a remote entry is accepted and dropped on the
#: floor, which is a SILENT failure: an entry carrying ``scopes`` authorizes
#: with the provider's default grant instead of the scopes the card promised.
KIRO_SCOPES_KEY = "oauthScopes"
KIRO_OAUTH_KEY = "oauth"


def _scopes_shape(raw: object) -> str:
    """A scope value's SHAPE for a log line, carrying none of its members.

    The field is hand-editable, so it can hold arbitrary JSON -- describing the
    shape keeps an accidentally-pasted token out of the log entirely instead of
    trusting the value to be harmless.
    """
    if not isinstance(raw, list):
        return type(raw).__name__
    bad = [
        f"[{i}]:{type(scope).__name__}"
        for i, scope in enumerate(raw)
        if not isinstance(scope, str) or not scope.strip()
    ]
    return f"list[{len(raw)}] with {', '.join(bad)}" if bad else f"list[{len(raw)}]"


def _wire_scopes(raw: object, *, server: str = "") -> list[str] | None:
    """The ``oauthScopes`` value for an internal ``scopes``, or None to omit it.

    Returns None -- meaning "emit no scope request" -- for anything that is not a
    non-empty list of non-empty strings. Partial forwarding is not an option: a
    hand-edited ``scopes: ["read", 7]`` emitted as-is makes kiro-cli reject the
    WHOLE agent spec ("expected a sequence" of strings), which drops every one of
    Kiro Crew's MCP tools, not just this server's. Omitting the field instead
    costs this entry the provider's default grant and leaves the rest working.

    All-or-nothing on purpose, matching the custom-server API's validation
    contract: a list with one bad member is a malformed request, not a request
    for the members that happen to be well-formed. Emitting the good subset
    would silently narrow (or broaden) the grant relative to what was written.

    Degrading is deliberate, but it is not free: the entry ends up authorizing
    with the provider's default grant, which can be WIDER than the list on disk.
    A malformed value therefore warns, so the swap is diagnosable from the log
    rather than only from a provider's consent screen. Absent and empty stay
    silent -- neither is a mistake, they are "no request" and "stop requesting".

    ``server`` names the entry in that warning, and an empty one suppresses it:
    the callers that pass a name are the ones writing a spec a provider will be
    asked with, while a comparison or UI read of the same value changes no grant
    and would only duplicate the line.
    """
    if raw is None or raw == []:
        return None
    if isinstance(raw, list) and all(
        isinstance(scope, str) and scope.strip() for scope in raw
    ):
        return list(raw)
    if server:
        logger.warning(
            "MCP server %s declares a malformed OAuth scope list (%s); requesting "
            "the provider's default grant instead, which may be broader than the "
            "scopes written on disk. Fix the value to request specific scopes.",
            server,
            _scopes_shape(raw),
        )
    return None


#: Sentinel for "no internal source spoke about this hint" -- deliberately
#: distinct from ``[]`` / ``""``, which mean "a source spoke, and it says none".
#: Collapsing the two loses data in one direction or leaks stale permissions in
#: the other, so :func:`apply_kiro_oauth_hints` keeps them apart.
_NO_SOURCE: Any = object()


def apply_kiro_oauth_hints(
    entry: dict[str, Any],
    *,
    scopes: Any = _NO_SOURCE,
    client_id: Any = _NO_SOURCE,
    server: str = "",
) -> dict[str, Any]:
    """Reconcile kiro-cli's OAuth wire keys on ``entry`` against a source value.

    The single merge rule shared by every writer of a remote MCP entry -- the
    agent-spec emit path and the ``mcp.json`` sync path -- so the semantics
    cannot drift between them.

    Each hint is in one of THREE states, and conflating any two of them either
    destroys the user's configuration or keeps requesting access they removed:

    * a VALID value -> write the wire key from it.
    * an EMPTY or malformed value (``[]``, ``""``, a non-string member) -> the
      source is speaking, and it says "none", so DELETE the wire key. Without
      this, a ``dict.update()``-based merge could never narrow a grant, because
      ``update`` cannot remove a key -- a widened-then-narrowed scope list would
      keep authorizing the scopes the user took away.
    * ``_NO_SOURCE`` (argument omitted) -> nothing authoritative spoke, so
      PRESERVE whatever wire value is already there, verbatim. An entry can be
      hand-authored directly in wire form with no internal spelling anywhere, and
      for that entry the wire value IS the source; rebuilding it from a source
      that does not exist would delete the only copy.

    The preserved value is NOT re-validated. A wire-only entry is the user's own
    text in the file kiro-cli reads, so the choice is between leaving it exactly
    as written and silently deleting configuration we did not author -- the same
    class of loss this three-state split exists to prevent.

    ``oauth`` is always edited surgically: only ``clientId`` belongs to us, so
    any other sub-key (``issuer``, ...) survives, and the mapping is dropped only
    once nothing is left in it.
    """
    out = dict(entry)

    if scopes is not _NO_SOURCE:
        wire_scopes = _wire_scopes(scopes, server=server)
        if wire_scopes is None:
            out.pop(KIRO_SCOPES_KEY, None)
        else:
            out[KIRO_SCOPES_KEY] = wire_scopes

    if client_id is not _NO_SOURCE:
        raw_oauth = out.get(KIRO_OAUTH_KEY)
        oauth = dict(raw_oauth) if isinstance(raw_oauth, dict) else {}
        oauth.pop("clientId", None)
        if isinstance(client_id, str) and client_id.strip():
            oauth["clientId"] = client_id
        if oauth:
            out[KIRO_OAUTH_KEY] = oauth
        else:
            out.pop(KIRO_OAUTH_KEY, None)

    return out


def kiro_oauth_wire_entry(
    entry: dict[str, Any], *, store_entry: dict[str, Any] | None, server: str = ""
) -> dict[str, Any]:
    """Translate a remote MCP entry into the OAuth shape kiro-cli parses.

    ``scopes`` -> ``oauthScopes``, and ``clientId`` -> ``oauth.clientId``. Both
    internal names are dropped from the result: kiro-cli ignores unknown keys
    silently, so leaving them would keep two spellings of one fact around with
    only one of them load-bearing.

    ``store_entry`` is the dashboard store's own entry for this server
    (``<data home>/mcp.json``), and it answers BOTH questions at once -- who owns
    the entry, and what the owner says. A usable dict means the store owns this
    name and IS the source; ``None`` (or any non-dict, which the merge skipped
    and which therefore supplied nothing) means we own nothing here:

    ======================  ==================  ============================
    store_entry             states              result
    ======================  ==================  ============================
    dict                    valid hint          wire key rebuilt from it
    dict                    empty hint          wire key DELETED
    dict                    no hint at all      wire key DELETED
    None / non-dict         --                  wire value PRESERVED verbatim
    ======================  ==================  ============================

    The store is read through :func:`kiro_entry_scopes` /
    :func:`kiro_entry_client_id`, so it is authoritative in EITHER spelling. That
    matters because the store does not only receive internal-form writes: the
    scope-toggle preservation rule copies a global server's spec into the store
    verbatim to keep it configured, and a global entry can be hand-authored in
    wire form. Such a copy is ours to manage and states its hints in the wire
    spelling; reading only the internal one would see "no hints" and delete the
    very configuration the copy exists to preserve.

    The source is read from the STORE, never from ``entry``, because ``entry`` is
    the merged spec ``rebuild_agent_config`` assembles -- the previously-rendered
    wire keys folded together with the store via ``dict.update()``. Reading hints
    off that would let a stale render outvote the store and pin every future
    session to the grant the entry was first rendered with.

    For an unmanaged entry an absent hint is silence, not removal, so the
    existing wire value stands -- deleting it would destroy the only copy of
    configuration written in a file we do not own. An internal spelling found
    there is still translated, since kiro-cli would otherwise ignore it.

    ``oauth`` is edited surgically in both regimes: only ``clientId`` is ours, so
    sibling sub-keys (``issuer``, ...) always survive. Safe on a stdio entry or
    one with neither spelling.
    """
    out = dict(entry)
    entry_scopes = out.pop(INTERNAL_SCOPES_KEY, _NO_SOURCE)
    entry_client_id = out.pop(INTERNAL_CLIENT_ID_KEY, _NO_SOURCE)

    if isinstance(store_entry, dict):
        # Owned: the store speaks, in whichever spelling it holds. "No hint" and
        # "empty hint" are the same answer here -- both mean stop requesting it --
        # so the readers' absent-or-empty -> [] / "" collapse is exactly right.
        return apply_kiro_oauth_hints(
            out,
            scopes=kiro_entry_scopes(store_entry, server=server),
            client_id=kiro_entry_client_id(store_entry),
            server=server,
        )

    return apply_kiro_oauth_hints(
        out, scopes=entry_scopes, client_id=entry_client_id, server=server
    )


def kiro_entry_scopes(entry: dict[str, Any], *, server: str = "") -> list[str]:
    """The OAuth scopes a remote MCP entry requests, either spelling.

    Reads an already-translated entry as well as an internal one so a spec
    round-tripped through disk still reports the access it asks for. That
    round-trip is also what makes an unmanaged entry a fixed point on the sync
    path: its hand-authored wire values read back unchanged, so a re-sync
    rewrites the same request instead of narrowing it.

    Validates through :func:`_wire_scopes`, the SAME contract the emit path uses,
    so a malformed list is omitted here too. Reading it as the well-formed subset
    while the emitted spec omits the field entirely would make discovery report
    an access level no file asks for and no session receives -- and a sync acting
    on that would propagate the truncated grant as if it were complete.

    Precedence keys on the internal key's PRESENCE, not its value. When
    ``scopes`` exists its validated value is FINAL -- including ``[]``, which is
    the custom-server API's explicit clear. An entry can hold both spellings (the
    scope-toggle preservation rule copies a global spec into the store in wire
    form, and the API then edits it in internal form), and falling through on an
    empty internal value would resurrect the stale wire sibling and keep
    requesting access the user just cleared. Wire spellings are consulted only
    when the internal key is absent entirely.
    """
    if INTERNAL_SCOPES_KEY in entry:
        validated = _wire_scopes(entry[INTERNAL_SCOPES_KEY], server=server)
        return validated if validated is not None else []

    # Between the two WIRE spellings a fall-through is correct: neither is a
    # deliberate clear, so the first one that validates is the request.
    wire_candidates: list[object] = [entry.get(KIRO_SCOPES_KEY)]
    oauth = entry.get(KIRO_OAUTH_KEY)
    if isinstance(oauth, dict):
        wire_candidates.append(oauth.get(KIRO_SCOPES_KEY))
    for raw in wire_candidates:
        validated = _wire_scopes(raw, server=server)
        if validated is not None:
            return validated
    return []


def kiro_entry_client_id(entry: dict[str, Any]) -> str:
    """The public OAuth client id on a remote MCP entry, either spelling.

    Same presence rule as :func:`kiro_entry_scopes`: when ``clientId`` exists its
    value is final, so an explicit empty (or an explicit ``null``) clears rather
    than falling through to a stale ``oauth.clientId`` sibling.
    """
    if INTERNAL_CLIENT_ID_KEY in entry:
        raw = entry[INTERNAL_CLIENT_ID_KEY]
        return raw if isinstance(raw, str) and raw.strip() else ""
    oauth = entry.get(KIRO_OAUTH_KEY)
    if isinstance(oauth, dict):
        raw = oauth.get("clientId")
        if isinstance(raw, str) and raw.strip():
            return raw
    return ""


def mcp_server_alias(name: str) -> str:
    """Return a kiro-safe (slash-free) alias for an MCP server key.

    kiro-cli resolves agent ``tools``/``allowedTools`` entries of the form
    ``@server`` by splitting on ``/`` (``@server/tool``).  A server key that
    contains ``/`` -- e.g. the npm-scoped ``npm:@playwright/mcp`` or the MCP
    registry ``namespace/name`` form -- can therefore never be referenced as
    ``@key``: kiro reads the trailing path segment as a (non-existent) tool
    name and exposes none of the server's tools.

    Map such keys to a stable, descriptive, slash-free slug
    (``npm:@playwright/mcp`` -> ``playwright-mcp``).  Slash-free names are
    returned unchanged so existing well-formed configs are untouched.
    """
    if not name or "/" not in name:
        return name
    slug = name.split(":", 1)[1] if ":" in name else name
    slug = slug.lstrip("@").replace("/", "-").replace("@", "-")
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", slug).strip("-")
    return slug or "mcp-server"
