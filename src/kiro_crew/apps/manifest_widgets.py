"""The manifest ``contributes.widgets`` block: views an app draws inline in any chat.

An agent draws an app widget with ``<mcwidget use="<app>:<id>">{json}</mcwidget>``.
The dashboard runs the widget's ``entry`` in the shared sandboxed app frame
(``apps/widget_frame.py``) and hands the module only the parsed JSON payload of
that one tag. The frame gets no capability, so the module reaches no backend.

What a declaration can and cannot do is decided here, before anything reads it:

- ``id`` is the frame point id (``^[a-z][a-z0-9_-]{0,63}$``) and unique in the app;
- ``title`` is 1 to ``MAX_WIDGET_TITLE`` UTF-16 units and ``description`` at most
  ``MAX_WIDGET_DESCRIPTION``; an agent reads the description to fill the payload;
- ``entry`` is an ESM module relative to the app's ``ui/`` directory; its
  containment is checked in ``AppManifest.validate``, which knows the app root;
- an app declares at most ``MAX_WIDGETS_PER_APP`` widgets;
- a field present with the wrong JSON type, or an unknown field, is refused.

A malformed block is kept with its errors, so ``AppManifest.validate`` refuses the
install instead of the block vanishing from the serialized manifest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Mirrored in ``website/src/appWidgets/appWidgetRegistry.ts``.
MAX_WIDGETS_PER_APP = 16
MAX_WIDGET_TITLE = 40
MAX_WIDGET_DESCRIPTION = 200
#: The longest ``entry`` kept. Checked before path normalization, which would
#: otherwise accept a padded path such as ``"./" * 500000 + "a.mjs"``.
MAX_WIDGET_ENTRY = 512
#: The frame point id rule (``POINT_ID_RE`` in ``apps/app_frame.py``): the id is a
#: segment of the frame routes.
WIDGET_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_WIDGET_KEYS = frozenset({"id", "title", "description", "entry"})


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units, the unit the dashboard's ``.length`` counts.

    The same count as ``_mirrored_len`` in ``manifest_contributes.py``, with
    ``surrogatepass`` so a lone surrogate from a JSON escape is counted, not raised.
    """
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


@dataclass
class WidgetConfig:
    """One widget an app offers to every chat."""

    id: str = ""
    title: str = ""
    description: str = ""
    #: ESM module, relative to the app's ``ui`` directory (same base as ``ui.entry``).
    entry: str = ""
    #: Field names that are unknown or present with the wrong JSON type. Not serialized.
    bad_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "title": self.title, "entry": self.entry}
        if self.description:
            d["description"] = self.description
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WidgetConfig:
        bad = sorted(k for k in data if k not in _WIDGET_KEYS)
        values: dict[str, str] = {}
        for key in ("id", "title", "description", "entry"):
            value = data.get(key, "")
            if isinstance(value, str):
                values[key] = value
            else:
                # Refused, never coerced: ``str(5)`` would install as a working id.
                bad.append(key)
                values[key] = ""
        return cls(bad_fields=bad, **values)

    def validate(self) -> list[str]:
        where = f"contributes.widgets[{self.id or '?'}]"
        errors = [
            f"{where}: {name} is not a known field of the right type" for name in self.bad_fields
        ]
        if not self.id:
            errors.append("contributes.widgets: entry missing id")
        elif not WIDGET_ID_RE.fullmatch(self.id):
            errors.append(f"{where}: id must be a storage-safe slug ({self.id!r})")
        if not self.title:
            errors.append(f"{where}: missing title")
        elif _utf16_len(self.title) > MAX_WIDGET_TITLE:
            errors.append(f"{where}: title exceeds {MAX_WIDGET_TITLE} characters")
        if _utf16_len(self.description) > MAX_WIDGET_DESCRIPTION:
            errors.append(f"{where}: description exceeds {MAX_WIDGET_DESCRIPTION} characters")
        if not self.entry:
            errors.append(f"{where}: missing entry")
        elif len(self.entry) > MAX_WIDGET_ENTRY:
            errors.append(f"{where}: entry exceeds {MAX_WIDGET_ENTRY} characters")
        return errors


@dataclass
class WidgetsConfig:
    """The ``contributes.widgets`` list, with what was malformed about it."""

    widgets: list[WidgetConfig] = field(default_factory=list)
    #: ``widgets`` present but not a list. Not serialized.
    bad_block: bool = False
    #: Entries of a well-formed list that were not objects. Not serialized.
    dropped: int = 0

    @classmethod
    def from_value(cls, raw: Any, *, present: bool) -> WidgetsConfig:
        if not present:
            return cls()
        if not isinstance(raw, list):
            return cls(bad_block=True)
        return cls(
            widgets=[WidgetConfig.from_dict(w) for w in raw if isinstance(w, dict)],
            dropped=sum(1 for w in raw if not isinstance(w, dict)),
        )

    def to_list(self) -> list[dict[str, Any]]:
        return [w.to_dict() for w in self.widgets]

    def by_id(self, widget_id: str) -> WidgetConfig | None:
        return next((w for w in self.widgets if w.id == widget_id), None)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.bad_block:
            errors.append(
                "contributes.widgets must be an array -- a non-array value passes as "
                "empty and then disappears from the serialized manifest"
            )
        if self.dropped:
            errors.append(
                f"contributes.widgets: {self.dropped} entr"
                f"{'y' if self.dropped == 1 else 'ies'} must be an object"
            )
        if len(self.widgets) > MAX_WIDGETS_PER_APP:
            errors.append(
                f"contributes.widgets: {len(self.widgets)} widgets exceeds the limit of "
                f"{MAX_WIDGETS_PER_APP}"
            )
        seen: set[str] = set()
        for widget in self.widgets:
            errors.extend(widget.validate())
            if widget.id:
                if widget.id in seen:
                    # One id is one frame route: the second would answer for the first.
                    errors.append(f"contributes.widgets: duplicate id {widget.id!r}")
                seen.add(widget.id)
        return errors
