"""Composing a crewmate's dashboard page.

Split out of ``member_dashboard`` to keep that module under the file-line cap, not
because the render is a separate concern: ``member_dashboard`` imports :func:`_render`
back and calls it from ``_serve``, and re-exports it so ``member_dashboard._render``
stays the import path callers and tests already use.

The dependency runs ONE way at module load: this module imports nothing from
``member_dashboard`` at the top level, so there is no import cycle. The handler helpers
it needs to fill a page -- ``_trusted_page``, ``read_fields``, ``_page_safe``,
``_template_text_safe`` -- are imported lazily inside :func:`_render`, which also keeps
the heavy ``dashboard_frame`` import off this module's load path for the same reason the
handler's own render did.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["_render"]


def _render(
    slug: str,
    member: str,
    record: Any,
    locale: str = "",
    *,
    slot: str | None = None,
    last_user_at: str = "",
) -> str | None:
    """The live page with its values filled in -- the frame's half of contract v3 part 5.

    Fold values come through :class:`~kiro_crew.dashboard_feed.DashboardFeed`, which
    subscribes with a baseline -- the bus hands the CURRENT fold value through the
    projection read path, so nothing refolds a log here. Agentic values come from the
    slot's ``agentic`` fold. The result is :func:`dashboard_frame.compose_body`, the one
    builder that also composes a refill, so the page's ``window.kirocrew`` is the same
    shape on first paint and after.

    *last_user_at* is ``_person_last_spoke`` for this page's chat, handed to the page
    beside the write times it is compared with.

    DEMO SCOPE: the feed is opened and closed per request. The contract wants one
    long-lived feed per open dashboard pushing refills over the WS exporter; until
    that lands, a re-read (focus, the tab's own refetch) is how the page moves. The
    handler ETags the WHOLE serialized response so an unchanged page skips the download
    -- there is no compose cache here, because the compose itself is cheap (the field
    read is memoized by the fold layer) and the download is the cost worth skipping.
    ``None`` on any failure, so the raw page still renders under the frame's own stale
    band rather than the tab erroring.
    """
    try:
        from kiro_crew import dashboard_frame
        from kiro_crew.dashboard.handlers.member_dashboard import (
            _page_safe,
            _template_text_safe,
            _trusted_page,
            read_fields,
        )
        from kiro_crew.dashboard_templates.manifest import parse_manifest

        manifest = parse_manifest(dict(record.manifest))
        # THE EXECUTABLE PAGE COMES FROM THE CATALOG, NEVER FROM THE RECORD.
        #
        # `record.html` and `record.manifest` both come out of
        # ``members/<slug>/dashboard/instance.json``, which is WRITABLE. Gating the
        # render on `manifest.source` asked that file to vouch for itself: anything
        # that could append a `<script>` to the stored page could leave the
        # `builtin` label in place beside it, and this step is the one that hands
        # the crewmate's task titles and summaries to whatever runs. The label is
        # not evidence, so it cannot be the gate.
        #
        # What IS trusted is the repository. The record's ``template_id`` names a
        # directory in it, so the id is read from the record and the BYTES are read
        # from the catalog. A stored page may stay on disk -- a rollback reads it,
        # and it is what the crewmate copied -- but nothing executes it.
        page = _trusted_page(slug, record)
        if page is None:
            return None
        read = read_fields(slug, member, manifest, slot=slot)
        payload = dashboard_frame.read_payload(
            # MASKED AND REDACTED on the way out, at the one step that hands fold
            # values to a page's own script. A snapshot is taken from what the page
            # holds, so it inherits this rather than needing its own pass.
            {name: _page_safe(value) for name, value in read.fields.items()},
            agentic=[name for name, spec in manifest.fields.items() if spec.agentic],
            seq=read.seq,
            stale=read.stale,
            missing=read.missing,
            # Masked like the values beside them: a stamp is not a secret, but this is
            # the one chokepoint and a field added here later would otherwise skip it.
            written_at={name: str(_page_safe(at)) for name, at in read.written_at.items()},
            locale=locale,
            # A root session's page is read by its slot; a crewmate's never is.
            subject="crewmate" if slot is None else "session",
            # Through the same mask as the write times it is compared with.
            last_user_at=str(_page_safe(last_user_at)),
        )
        # The catalog's page, redacted like the values that go into it. The scrub is
        # kept because this is the one step producing both the raw body and the
        # composed document, and it guards a hand-edited checkout rather than a page
        # an attacker chose: the bytes here came out of the repository.
        return dashboard_frame.compose_body(_template_text_safe(page), payload)
    except Exception:
        logger.warning("dashboard: could not fill %r's page", slug, exc_info=True)
        return None
