"""Auto-register chat-emitted ``<mcwidget>`` bodies as artifacts.

Every widget the agent emits becomes an artifact the moment its message is
finalized — unpinned, so it is a *record*, not a library entry. Starring it in
chat is then a pure metadata flip (``pinned=True``) rather than a create, which
is what lets the in-session Artifacts tab list widgets at all: a widget's HTML
lives inline in the message and is never written to disk, so there is nothing
for the file-backed session-docs scan to find.

Why register on the backend rather than in ``WidgetFrame`` on mount: a chat
message that is never scrolled into view never mounts its widgets (the chat list
virtualizes), so frontend registration would make an artifact's existence depend
on whether a human happened to look at it. Registering where the message is
finalized covers every emitted widget exactly once, and gets the originating
session key for free.

Identity comes from :func:`kiro_crew.widget_slug.derive_widget_slug` over
``(message_ts, widget_index)`` — the same function the frontend uses, so a
``WidgetFrame`` impression resolves the artifact this module wrote without the
two sides exchanging an id. See that module's docstring for the parity contract.

Retention: auto-registered, still-unpinned widget artifacts are pruned oldest-
first past :data:`MAX_AUTO_WIDGET_ARTIFACTS`. Without a sweep, a chat-heavy user
accumulates one three-file artifact directory per throwaway widget forever, and
every library listing is an O(N) scan over them. Pinning one takes it out of the
sweep permanently — the star is the "keep this" signal.

All filesystem work here is blocking, so callers MUST invoke
:func:`register_widgets_off_loop` (never :func:`register_widgets` directly) from
async code — see AGENTS.md and the ``no-blocking-call-on-event-loop`` rule.
"""

from __future__ import annotations

import asyncio
import logging

from kiro_crew.artifacts import (
    MAX_AUTO_WIDGET_ARTIFACTS,
    ArtifactAlreadyExistsError,
    ArtifactError,
    ArtifactValidationError,
    get_default_store,
)
from kiro_crew.executors import subprocess_executor
from kiro_crew.widget_parse import parse_widgets
from kiro_crew.widget_slug import derive_widget_slug

logger = logging.getLogger(__name__)

#: Fallback display name when the agent emitted ``<mcwidget>`` with no title.
_DEFAULT_WIDGET_NAME = "Widget"


def register_widgets(text: str, message_ts: str, session_key: str) -> list[str]:
    """Register every complete widget in ``text``; return the slugs touched.

    Blocking (filesystem). Idempotent: a slug that already exists is left
    untouched, so a replayed/rehydrated message never duplicates or clobbers an
    artifact the user has since edited. An explicit ``slug=`` attribute means the
    agent re-emitted a KNOWN artifact — skipped entirely, since creating it here
    would race the real one and re-emission is not authorship.

    Never raises: a failure to register is a lost convenience, not a reason to
    fail the chat turn that produced the widget.
    """
    if not message_ts:
        # No stable identity to derive a slug from — registering under a random
        # slug would make the frontend probe miss and strand the artifact.
        return []
    try:
        widgets = parse_widgets(text)
    except Exception:  # pragma: no cover — parser must never break a turn
        logger.warning("widget parse failed for message %s", message_ts, exc_info=True)
        return []
    if not widgets:
        return []

    store = get_default_store()
    registered: list[str] = []
    for w in widgets:
        if w.slug:
            # Re-emission of an existing artifact — not a new one.
            continue
        if not w.content.strip():
            # An empty widget body has nothing to persist and would fail
            # content validation; skip rather than log a failure per turn.
            continue
        slug = derive_widget_slug(message_ts, w.index)
        try:
            store.create(
                name=w.title or _DEFAULT_WIDGET_NAME,
                content=w.content,
                slug=slug,
                kind="widget",
                source="chat",
                session_key=session_key,
                auto_registered=True,
            )
        except ArtifactAlreadyExistsError:
            # Already registered (message re-finalized, or the user starred it
            # from a tab before this ran). Nothing to do — do NOT overwrite.
            continue
        except (ArtifactValidationError, ArtifactError, OSError) as exc:
            logger.warning("auto-register failed for widget %s: %s", slug, exc)
            continue
        registered.append(slug)

    if registered:
        try:
            pruned = store.prune_auto_widgets(keep=MAX_AUTO_WIDGET_ARTIFACTS)
            if pruned:
                logger.info("pruned %d unpinned auto-registered widget artifacts", pruned)
        except (ArtifactError, OSError) as exc:
            logger.warning("auto-widget prune failed: %s", exc)
    return registered


async def register_widgets_off_loop(text: str, message_ts: str, session_key: str) -> list[str]:
    """Async wrapper: run :func:`register_widgets` in the shared executor.

    Registration walks and writes the artifact store, so it must never run on
    the gateway's event loop (see the module docstring).
    """
    return await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), register_widgets, text, message_ts, session_key
    )
