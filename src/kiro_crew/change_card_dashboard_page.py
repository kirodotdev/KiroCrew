"""The ``crewmate.dashboard_page`` change card: a crewmate on a template offers its own page.

The card asks; it writes nothing. Its one apply step is the yes
(``POST /api/members/{slug}/dashboard/page-offer``), which wakes the crewmate to
write, save and apply the page (:mod:`kiro_crew.dashboard.page_offer`). Declining or
leaving it to expire writes nothing. Raised only by the crewmate itself, in its own
thread (``offer_dashboard_page``), once ever.

The wording is an offer of what the page is for -- never what it costs to write.
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import quote

#: The card's title: the question the person answers with its button.
TITLE = "Want me to build a Dashboard page around your goal?"


def page_offer_path(slug: str) -> str:
    return f"/api/members/{quote(slug, safe='')}/dashboard/page-offer"


def preview(
    params: dict[str, Any],
    snapshot: dict[str, Any],
    context: dict[str, Any],
    out: dict[str, Any],
    *,
    fail: Callable[[str, str], Exception],
    step: Callable[..., dict[str, Any]],
) -> None:
    """Fill *out* (title, changes, scope, apply) for one page-offer card.

    *context* carries the goal the page is built around (``goal``) and the template it
    would replace (``replaces``), both read by ``change_cards_readers.read_context``,
    which refuses the card when the crewmate was already asked or is not on a
    template page.
    """
    if not snapshot.get("exists") or not snapshot.get("slug"):
        raise fail("not_found", f"no crewmate named '{params['member']}'")
    label = str(context.get("label") or params["member"])
    replaces = str(context.get("replaces") or "")
    goal = str(context.get("goal") or "")
    out["title"] = TITLE
    out["changes"] = [
        {
            "label": "Dashboard page",
            "before": f"{replaces} (template)",
            "after": f"A page {label} writes around “{goal}”" if goal else f"A page {label} writes",
        }
    ]
    out["scope"] = {"members": [label], "count": 1, "only_this_member": True}
    out["apply"] = [
        step("POST", page_offer_path(str(snapshot["slug"])), {"member": params["member"]})
    ]
