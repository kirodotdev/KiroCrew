"""Adopt a template onto a dashboard instance only while nothing is adopted on it.

A sibling of :mod:`kiro_crew.dashboard_templates.instance`, which owns the store, its
lock and its commit; this module adds the one conditional form of ``adopt`` that an
AUTOMATIC adoption needs (Crew Mode's board on the switch to the conductor), kept
out of the store module for its size alone.
"""

from __future__ import annotations

import json

from kiro_crew.dashboard_templates import catalog
from kiro_crew.dashboard_templates import instance as _store
from kiro_crew.dashboard_templates.instance import STATE_EMPTY, Instance, InstanceRefused

__all__ = ["adopt_if_empty"]


def adopt_if_empty(slug: str, template_id: str, *, session_id: str = "") -> Instance | None:
    """:func:`instance.adopt`, only onto a page nothing was adopted on yet; ``None`` otherwise.

    The emptiness check and the commit run under the instance's one write lock, so a
    page another writer adopted between a caller's own read and this call is kept: an
    automatic adoption must never replace a page a person or an agent chose at the
    same moment. The template is loaded and checked the way ``adopt`` does, before
    the lock is taken.
    """
    try:
        entry = catalog.load_one(template_id)
    except catalog.UnknownTemplate as exc:
        raise InstanceRefused(str(exc)) from None
    manifest = json.loads(json.dumps(_store._manifest_json(entry)))
    html = entry.html
    _store._check(manifest, html)
    with _store._locked(_store.instance_dir(slug)):
        if _store.read(slug).state != STATE_EMPTY:
            return None
        return _store._commit(
            slug,
            action="adopted",
            template_id=entry.id,
            template_version=entry.version,
            html=html,
            manifest=manifest,
            from_version=0,
            session_id=session_id,
        )
