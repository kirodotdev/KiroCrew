"""Who owns a ``config.local.json`` key: the person, or a policy an edition enforces.

``config.local.json`` deep-merges over ``config.json``, so a Settings switch whose key
the overlay sets is held (:func:`~kiro_crew.config.loader_locations.overlay_pins`). The
core cannot tell who wrote that entry: a person's own ``kirocrew config set --local``
and an edition's boot-time seed go through the same writer. A surface that knows only
"pinned" tells the person to remove the entry, and where an edition writes the key back
on every boot, following that advice changes nothing.

So a downstream edition that enforces an overlay key registers an owner for it here, at
boot. When the overlay sets the key, the owner is asked with the value it holds and
answers with the policy that keeps it there (a ``label`` and a ``detail`` line), or
``None`` when the entry is not its policy. A surface then names the policy instead of
advising removal. The public core registers none, so every pin reads as the person's own.

An owner changes what a surface says, never what the setting does: the overlay is read
and merged exactly as before, and nothing an owner returns can set, clear or change a
value. An owner that raises, or answers with anything but a usable label, counts as no
policy, so the surface falls back to the person's-own-entry wording.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from kiro_crew.config import loader_locations

logger = logging.getLogger("kiro_crew.config.loader")

#: ``overlay_source`` when the overlay sets the key and no owner claims the entry.
SOURCE_USER = "user"
#: ``overlay_source`` when a registered owner claims the entry as its policy.
SOURCE_POLICY = "policy"

#: Longest label and detail a surface shows; longer text is cut so an owner cannot
#: crowd the row it is explaining.
MAX_LABEL_CHARS = 120
MAX_DETAIL_CHARS = 400

#: Asked with the value ``config.local.json`` holds for the key. Returns a mapping
#: with a non-empty ``label`` and an optional ``detail`` when the entry is the policy
#: the owner enforces, else ``None``. Called off the event loop, once per read of the
#: pin, so it may read a file but must not block for long.
OverlayOwner = Callable[[object], "Mapping[str, object] | None"]

_OWNERS: dict[tuple[str, ...], OverlayOwner] = {}
_LOCK = threading.Lock()


@dataclass(frozen=True)
class OverlayPolicy:
    """The policy an owner says keeps an overlay key where it is."""

    label: str
    detail: str = ""


@dataclass(frozen=True)
class OverlayPin:
    """Whether ``config.local.json`` sets a key, and whose entry it is."""

    pinned: bool
    #: :data:`SOURCE_USER` or :data:`SOURCE_POLICY` when pinned, else ``None``.
    source: str | None = None
    #: Set only when :attr:`source` is :data:`SOURCE_POLICY`.
    policy: OverlayPolicy | None = None

    def fields(self) -> dict[str, object]:
        """The wire fields a surface reports the pin with."""
        return {
            "overlay_override": self.pinned,
            "overlay_source": self.source,
            "overlay_policy": (
                None
                if self.policy is None
                else {"label": self.policy.label, "detail": self.policy.detail}
            ),
        }


def _key_path(key_path: str | Iterable[str]) -> tuple[str, ...]:
    path = (key_path,) if isinstance(key_path, str) else tuple(key_path)
    if not path or not all(isinstance(part, str) and part for part in path):
        raise ValueError("an overlay owner needs a key path of non-empty strings")
    return path


def register_overlay_owner(key_path: str | Iterable[str], owner: OverlayOwner) -> None:
    """Register *owner* for the overlay key at *key_path*; a second call replaces it.

    *key_path* is a top-level key (``"auto_update"``) or its segments
    (``("telemetry", "enabled")``).
    """
    path = _key_path(key_path)
    if not callable(owner):
        raise TypeError(f"overlay owner for {'.'.join(path)!r} is not callable")
    with _LOCK:
        _OWNERS[path] = owner


def unregister_overlay_owner(key_path: str | Iterable[str]) -> None:
    """Remove the owner registered for *key_path*, if any."""
    path = _key_path(key_path)
    with _LOCK:
        _OWNERS.pop(path, None)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _policy_for(path: tuple[str, ...], value: object) -> OverlayPolicy | None:
    with _LOCK:
        owner = _OWNERS.get(path)
    if owner is None:
        return None
    dotted = ".".join(path)
    try:
        answer = owner(value)
    except Exception:
        logger.warning(
            "overlay owner for %s raised; its pin reads as a config.local.json entry",
            dotted,
            exc_info=True,
        )
        return None
    if answer is None:
        return None
    label = answer.get("label") if isinstance(answer, Mapping) else None
    detail = (answer.get("detail") or "") if isinstance(answer, Mapping) else None
    if not isinstance(label, str) or not label.strip() or not isinstance(detail, str):
        logger.warning(
            "overlay owner for %s answered without a usable label; its pin reads as a "
            "config.local.json entry",
            dotted,
        )
        return None
    return OverlayPolicy(_clip(label, MAX_LABEL_CHARS), _clip(detail, MAX_DETAIL_CHARS))


def overlay_pin(*key_path: str) -> OverlayPin:
    """Whether ``config.local.json`` sets the key at *key_path*, and whose entry it is.

    Never raises. The overlay is read as :func:`overlay_pins` reads it; only a set
    key asks its owner, with the value the overlay holds there.
    """
    pinned, value = loader_locations.overlay_entry(*key_path)
    if not pinned:
        return OverlayPin(False)
    policy = _policy_for(key_path, value)
    if policy is None:
        return OverlayPin(True, SOURCE_USER)
    return OverlayPin(True, SOURCE_POLICY, policy)
