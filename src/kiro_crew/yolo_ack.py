"""The owner's acknowledgement of the YOLO confirmation, kept on the gateway.

The dashboard's approval-mode picker asks for a confirmation the first time the
owner switches to YOLO, and its *Don't show again* box skips it later. The
browser keeps that choice per origin (``mc-yolo-ack``), and the UI-preferences
backup leaves it out on purpose because that backup is agent-writable. This
record keeps the owner's copy where an agent cannot write it, so a fresh browser
profile still skips the confirmation for the machine owner.

``yolo_ack.json`` in the data home holds ``{"acknowledged": bool}``. Only a JSON
object whose ``acknowledged`` is exactly ``true`` counts; a missing, empty,
malformed or unreadable record, or any other value, reads as not acknowledged,
so the confirmation shows. The owner-only ``/api/chat/yolo-ack`` route
(:mod:`kiro_crew.dashboard.handlers.yolo_ack`) is the only writer. It rewrites the
record in place (:func:`kiro_crew.atomic_write.rewrite_in_place`), keeping its
inode, so a running Linux sandbox's read-only bind of the leaf stays sealed.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from kiro_crew.atomic_write import rewrite_in_place

logger = logging.getLogger(__name__)

FILE_NAME = "yolo_ack.json"

#: Serializes the owner's writes; the route is the only writer.
_WRITE_LOCK = threading.Lock()


def _path() -> Path:
    # Deferred, as ``security.mcp_body_fields`` does: keep ``config`` off the import path.
    from kiro_crew.config.loader import config_dir

    return config_dir() / FILE_NAME


def read_acknowledged() -> bool:
    """Whether the owner acknowledged the YOLO confirmation. Only a JSON ``true`` is."""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except Exception:  # noqa: BLE001 -- an unreadable record reads as not acknowledged
        logger.debug("%s unreadable; reading as not acknowledged", FILE_NAME, exc_info=True)
        return False
    return isinstance(raw, dict) and raw.get("acknowledged") is True


def write_acknowledged(value: bool) -> None:
    """Record the owner's choice; ``False`` re-arms the confirmation.

    A browser holding its own ``mc-yolo-ack`` copy still skips it. Rewritten
    in place, so a torn read during the rewrite reads as not acknowledged, the
    narrow answer.
    """
    data = json.dumps({"acknowledged": bool(value)}).encode("utf-8")
    with _WRITE_LOCK:
        rewrite_in_place(_path(), data)


__all__ = ["FILE_NAME", "read_acknowledged", "write_acknowledged"]
