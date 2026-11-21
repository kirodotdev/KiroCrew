"""Stored chat rows read as the dashboard receives them.

The Regenerate scan judges rows the way the dashboard does (``hidden_reply``), and
the dashboard judges what ``_prepare_messages`` sends, not the stored bytes. This
view applies the same per-row projection (``chat_utils_display.display_row_content``,
``display_meta``, ``display_variant_content``) lazily, per row and per key, so a
scan pays only for the text it reads.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import overload

from kiro_crew.dashboard.chat_utils_display import (
    display_meta,
    display_row_content,
    display_variant_content,
)


class _WireRow(Mapping[str, object]):
    """One stored row as the client receives it, projected per key on first read.

    ``role`` is the stored role; ``content`` is :func:`display_row_content` (the key
    is absent when that is ``None``); ``meta`` is :func:`display_meta`;
    ``variants`` holds each dict variant's :func:`display_variant_content`. A key
    nobody reads is never projected, so a scan that only looks at a row's role
    pays for no redaction."""

    __slots__ = ("_row", "_cache")
    _KEYS = ("role", "content", "meta", "variants")

    def __init__(self, row: dict) -> None:
        self._row = row
        self._cache: dict[str, object] = {}

    def _project(self, key: str) -> object:
        if key in self._cache:
            return self._cache[key]
        row = self._row
        value: object
        if key == "role":
            value = row.get("role")
        elif key == "content":
            value = display_row_content(row)
        elif key == "meta":
            value = display_meta(row)
        else:
            raw = row.get("variants")
            value = (
                [
                    {"content": display_variant_content(v.get("content", ""), v)}
                    for v in list(raw)
                    if isinstance(v, dict)
                ]
                if isinstance(raw, list)
                else None
            )
        self._cache[key] = value
        return value

    def __getitem__(self, key: str) -> object:
        if key not in self._KEYS:
            raise KeyError(key)
        value = self._project(key)
        if value is None:
            raise KeyError(key)
        return value

    def __iter__(self) -> Iterator[str]:
        return (k for k in self._KEYS if k in self)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key in self._KEYS and self._project(key) is not None


class WireRows(Sequence[Mapping[str, object]]):
    """A list of stored rows read as the client receives them (:class:`_WireRow`),
    each projected on first access and kept. The Regenerate scan hands this to
    ``hidden_reply.is_hidden_reply_at``, so it judges the rows the dashboard
    judges (``_prepare_messages``), not the stored bytes."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self._views: dict[int, _WireRow] = {}

    def __len__(self) -> int:
        return len(self._rows)

    @overload
    def __getitem__(self, index: int) -> Mapping[str, object]: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[Mapping[str, object]]: ...

    def __getitem__(
        self, index: int | slice
    ) -> Mapping[str, object] | Sequence[Mapping[str, object]]:
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self._rows)
        view = self._views.get(index)
        if view is None:
            view = _WireRow(self._rows[index])
            self._views[index] = view
        return view
