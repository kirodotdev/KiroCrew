"""What each ``browser`` op does to a page: only reads it, or acts on it.

One classifier, read by two consumers that must agree on what "reads only" means:

* the tool's hand-back wait (``mcp_tools/browser._HandbackWait``) runs a
  read-only op after the person handed the browser back with a note, and holds
  any other op back so the agent can read the note first;
* a host trust grant (``kiro_crew.browser_host_trust``) covers an op on one
  site by its class: a "read" grant covers opening and reading that site's
  pages, an "act" grant covers clicking, typing and uploading files there too.

Stdlib only, so the MCP shim can import it before it lists its tools.
"""

from __future__ import annotations

from typing import Any

#: Ops that only read the page: safe to run after a hand-back whatever the person said.
READ_ONLY_OPS = frozenset(
    {
        "snapshot",
        "read",
        "screenshot",
        "console",
        "wait_for",
        "wait_for_element",
        "wait_for_network_idle",
        "iframe_target",
    }
)

#: Browser Harness's own helpers served as ops (``browser_harness.raw_ops``). They
#: act whatever they are asked to do, so they are acting ops and never in a read class.
HARNESS_HELPER_OPS: tuple[str, ...] = ("js", "cdp", "http_get")

#: Ops no standing grant covers, a person's site grant or an app's ``browserSites``:
#: each asks on the card. A site grant names one host and covers pressing, typing
#: and picking on its pages; these reach past that. ``js`` runs any script; ``cdp``
#: can reach the whole browser (every tab, every cookie); ``http_get`` fetches
#: outside the browser. ``upload`` is not one: a person's "act" grant on a site
#: covers it there, though an app's ``browserSites`` does not
#: (``apps.browser_sites.gate_grants``).
CARD_ONLY_OPS = frozenset(HARNESS_HELPER_OPS)
#: Ops no host grant covers: the card-only ones, ``ask`` (the person's own
#: question), ``reflex`` (a whole loop of ops) and ``iframe_target``, which answers
#: from every tab the agent owns, since one host's grant does not cover another
#: site's tab.
NO_HOST_TRUST_OPS = CARD_ONLY_OPS | {"ask", "reflex", "iframe_target"}


def card_only(op: str, op_args: dict[str, Any]) -> bool:
    """No standing grant covers this call: a :data:`CARD_ONLY_OPS` op, or ``tabs
    close``, which closes a tab whose host no grant names."""
    return op in CARD_ONLY_OPS or (op == "tabs" and op_args.get("action") == "close")


#: Ops that open or move around a page without pressing anything on it. A host
#: "read" grant covers them on that host, beside :data:`READ_ONLY_OPS`.
_OPEN_OPS = frozenset({"navigate", "scroll"})


#: Ops that act on the page: a host "act" grant covers them.
_ACT_OPS = frozenset(
    {
        "back",
        "click",
        "dialog",
        "dispatch_key",
        "drag",
        "fill",
        "hover",
        "paste",
        "press_key",
        "select_option",
        "type",
        "upload",
        *HARNESS_HELPER_OPS,
    }
)

#: The two classes of :func:`host_trust_class`.
CLASS_READ = "read"
CLASS_ACT = "act"


def reads_only(op: str, op_args: dict[str, Any]) -> bool:
    """The op only reads the page (``tabs`` only when it lists)."""
    if op == "tabs":
        return str(op_args.get("action") or "list") == "list"
    return op in READ_ONLY_OPS


def host_trust_class(op: str, op_args: dict[str, Any]) -> str:
    """``"read"`` or ``"act"`` for an op a host grant may cover, ``""`` for one it never does.

    Read: :func:`reads_only`, plus opening a page (``navigate``, ``tabs new``) and
    scrolling. Act: every op that presses, types, picks or uploads files on the
    page, plus going back and switching or closing tabs. An op this module does not name gets
    ``""``, so a new op asks until someone classes it here.
    """
    if op in NO_HOST_TRUST_OPS or card_only(op, op_args):
        return ""
    if op == "tabs":
        action = str(op_args.get("action") or "list")
        if action == "new":
            return CLASS_READ
        return CLASS_ACT if action == "switch" else ""
    if reads_only(op, op_args) or op in _OPEN_OPS:
        return CLASS_READ
    return CLASS_ACT if op in _ACT_OPS else ""


__all__ = [
    "CLASS_ACT",
    "CARD_ONLY_OPS",
    "CLASS_READ",
    "HARNESS_HELPER_OPS",
    "NO_HOST_TRUST_OPS",
    "card_only",
    "READ_ONLY_OPS",
    "host_trust_class",
    "reads_only",
]
