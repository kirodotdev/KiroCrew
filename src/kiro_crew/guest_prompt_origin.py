"""Who a prompt's text came from, as far as the image builder is concerned.

``acp.prompt_blocks.build_prompt_blocks`` reads any readable image path in the user's
text and sends the picture as an image block. For a Shared Crew guest that read must
stay inside the attachment stores, so the code that starts a guest's turn binds
``guest_scope.attachment_read`` here and the builder consults it. A leaf module with no
application imports at load: the ACP package cannot import the dashboard, and the
dashboard cannot import the ACP package.

The binding is a context variable, so it follows the turn into ``asyncio.to_thread``
and into tasks the turn creates. A guest's request binds it (``slot_ownership``); a
message that waits in the queue or is steered into a running turn carries
:data:`GUEST_ORIGIN_META` in its entry meta, and the drain binds from the entries it
consumes, so a turn never inherits the mark of whichever request created its task.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterable
from typing import Any

#: ``(raw path, max bytes)`` -> ``(path to attach, bytes)`` or ``None`` when the name is
#: not an attachment-store file. Reads nothing the guest named except through that check.
GuestImageSource = Callable[[str, int], "tuple[str, bytes] | None"]

#: The queue-entry meta key that says the entry's words are a guest's. A restriction
#: only (it narrows which images are read), so it rides the durable entry meta.
GUEST_ORIGIN_META = "guest_origin"

_source: contextvars.ContextVar[GuestImageSource | None] = contextvars.ContextVar(
    "guest_prompt_image_source", default=None
)


def bind(guest: bool) -> contextvars.Token[GuestImageSource | None]:
    """Mark prompts built from here on as a guest's (*guest*) or not; ``reset`` undoes it."""
    if not guest:
        return _source.set(None)
    from kiro_crew.shared_crew.guest_scope import attachment_read

    return _source.set(attachment_read)


def reset(token: contextvars.Token[GuestImageSource | None]) -> None:
    _source.reset(token)


def current() -> GuestImageSource | None:
    """The attachment-name source a guest's prompt must read images through, else ``None``."""
    return _source.get()


def mark(meta: dict[str, Any]) -> None:
    """Stamp *meta* when the code now running was started by a guest's request."""
    if _source.get() is not None:
        meta[GUEST_ORIGIN_META] = True


def marked(items: Iterable[dict[str, Any]]) -> bool:
    """Whether any queue entry in *items* carries a guest's words."""
    return any(
        isinstance(item.get("meta"), dict) and item["meta"].get(GUEST_ORIGIN_META) is True
        for item in items
    )
