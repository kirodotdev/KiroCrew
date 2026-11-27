"""Which of a Bot chat's app offers are held back for now (``bot_chat_app_tools``).

An offer is held while something is stopping or confirming the app's code: a
pending close for this Bot's (slug, app) pair that is not confirmed yet, or a
revocation fence (a disable, uninstall or trust withdrawal under way). A held offer
is not delivered to a chat that starts meanwhile, and a reconcile must not read
that gap as a change, or it reloads the chat into the same hold again and again.

:func:`fence_holds` is the one test both sides use: ``record_mounted`` voids the
app for a starting chat, and :func:`reconcile_view` keeps the reconcile from
counting it. Holds no state: the caller passes its tables, under its own lock.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping


def fence_holds(
    app: str,
    slug: str,
    *,
    fence: Collection[str],
    hold_counts: Mapping[str, int],
    pending_close: Collection[tuple[str, str]],
) -> bool:
    """Whether the revocation fence holds *app* back from Bot *slug*'s chat.

    An open ``app_stop_fence`` span, or a fence raised for a revoke with no
    pending close behind it, holds every Bot. A fence kept up only by pending
    closes holds just the Bots whose own (slug, app) close is unconfirmed, so one
    Bot's unconfirmed close does not take the app from another.
    """
    if app not in fence:
        return False
    return (
        hold_counts.get(app, 0) > 0
        or not any(a == app for _, a in pending_close)
        or (slug, app) in pending_close
    )


def reconcile_view(
    slug: str,
    now: Mapping[str, str],
    was: Mapping[str, str],
    *,
    unmounted: Mapping[str, str],
    pending_close_withheld: Mapping[str, str],
    fence: Collection[str],
    hold_counts: Mapping[str, int],
    pending_close: Collection[tuple[str, str]],
) -> dict[str, str]:
    """The offers *now* as a reconcile compares them with *was*, the chat's record.

    An offer the harness withheld (*unmounted*) counts only once its entry changes,
    since that change may make it mountable. A held offer, one whose pending close
    is unconfirmed under an unchanged entry or whose app the fence holds, counts as
    unchanged: a chat that already runs it keeps ``was[a]``, a chat that does not
    leaves it out. Once the hold lifts, the next reconcile sees it as it is.
    """
    out: dict[str, str] = {}
    for a, f in now.items():
        if unmounted.get(a) == f:
            continue
        app = a.split(":")[0] if ":" in a else ""
        held = pending_close_withheld.get(a) == f or (
            app != ""
            and fence_holds(
                app, slug, fence=fence, hold_counts=hold_counts, pending_close=pending_close
            )
        )
        if not held:
            out[a] = f
        elif a in was:
            out[a] = was[a]
    return out
