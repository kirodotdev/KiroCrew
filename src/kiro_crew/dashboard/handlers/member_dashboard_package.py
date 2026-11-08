"""What one crewmate's dashboard route reads to name the page in force.

The package bound to a crewmate, the record's pinned page and older-package rule, and
the apply counter a page is named by. Split out of :mod:`member_dashboard`, which
imports them back under the same names.
"""

from __future__ import annotations

import logging

from kiro_crew import dashboard_agentic
from kiro_crew import members as members_mod
from kiro_crew.dashboard_templates import instance

logger = logging.getLogger(__name__)


def _bound_package_slug(member: str) -> str:
    """The ``kind="dashboard"`` artifact bound to *member*, or ``""``.

    :func:`~kiro_crew.artifact_store.dashboard_package.resolve_bound_slug` with this
    route's own answer for the cases it raises on. It raises because its other caller
    must tell "no package" from "cannot prove there is none" -- a write validated
    against the wrong Model replaces a cell with a value of the wrong shape. THIS
    caller is a read that draws a page, and the page it draws when the answer is empty
    is the template instance every crewmate already has. So an unprovable answer is
    answered the same way as an absent one: the crewmate keeps the page it has, and a
    failed lookup costs a package page rather than showing the wrong one.

    ``BindingAmbiguous`` arrives through the same ``except`` by being a subclass of
    ``BindingLookupIncomplete``, and wants the same answer here for the same reason.

    Blocking file IO: it lists the artifact library and reads each candidate. Callers
    run it off the event loop.
    """
    try:
        # ONE lookup, shared with the crewmate's `[DASHBOARD]` turn block, so the
        # page the person sees and the page the crewmate is told about agree.
        return dashboard_agentic.bound_package(members_mod.member_slug(member))
    except Exception:
        logger.debug("dashboard: %r's package binding could not be resolved", member, exc_info=True)
        return ""


def _package_in_force(slug: str) -> tuple[instance.PinnedPackage | None, bool]:
    """``(pinned page, older rule)`` for *slug*, from its instance record.

    The write path asks the same two questions (``dashboard_package.read_package_model``),
    so the page and the write check name one Model. Blocking file IO.
    """
    pinned, older, _counter = _package_read(slug)
    return pinned, older


def _package_read(slug: str) -> tuple[instance.PinnedPackage | None, bool, int]:
    """:func:`_package_in_force` plus the record's apply counter, from ONE record read.

    The counter names the page a correction (Dashboard edit mode) was typed on. Read
    with the page, so an apply landing while the page renders cannot pair the page
    on screen with its replacement's counter. Blocking file IO.
    """
    record = instance.read(slug)
    counter = int(getattr(record, "instance_version", 0) or 0)
    return instance.pinned_package(record), instance.keeps_older_package_rule(record), counter


def _page_revision(counter: int, version: int) -> int:
    """The apply counter a package page is named by: the record's, else *version*.

    The number ``dashboard_package._package_model`` stamps the Model with. It only goes
    up, unlike a package version: a slug deleted and saved again starts at 1 again.
    """
    return counter or version
