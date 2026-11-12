"""One gate call's shared reads of its own text.

A deny judgement asks the same question of the same text many times: each floor
tokenizes the command again and peels the same word again. :func:`sharing_views`
opens a memo for the length of ONE call and a reader marked :func:`once_per_call`
keeps its answers there, so a distinct text is read once inside that call. The memo
is dropped when the call returns: nothing is kept between calls, judgements or
sessions, and outside a call every reader computes afresh.

Only a PURE reader may be marked: its answer is decided by its one argument and by
module constants, never by the filesystem, the clock or configuration. That is what
makes one read and many reads the same verdict; ``test_deny_gate_call_sharing.py``
judges a corpus with the memo and without. A leaf, so every shell reader can import it.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Hashable
from contextvars import ContextVar
from typing import Any, TypeVar

_T = TypeVar("_T")

#: One gate call's answers; unset outside a call. The redirect views key on the plain
#: text, the walks on ``(walk name, text)`` and a marked reader on ``(reader, text)``.
_CALL_VIEWS: ContextVar[dict[Hashable, Any] | None] = ContextVar("call_views", default=None)


def sharing_views(fn):  # type: ignore[no-untyped-def]  # one view cache per call of fn
    @functools.wraps(fn)
    def run(*args, **kwargs):  # type: ignore[no-untyped-def]
        scope = _CALL_VIEWS.set({})
        try:
            return fn(*args, **kwargs)
        finally:
            _CALL_VIEWS.reset(scope)

    return run


def once_per_call(
    *, fresh: Callable[[Any], Any] | None = None
) -> Callable[[Callable[[str], _T]], Callable[[str], _T]]:
    """Mark a pure one-argument reader: inside a call, each distinct text is read once.

    *fresh* copies a mutable answer on EVERY return, the first included, so a caller
    that edits what it was handed cannot change what the next one reads. An answer
    that raised is not kept.
    """

    def mark(fn: Callable[[str], _T]) -> Callable[[str], _T]:
        @functools.wraps(fn)
        def read(text: str) -> _T:
            views = _CALL_VIEWS.get()
            if views is None:
                return fn(text)
            key = (fn, text)
            try:
                answer = views[key]
            except KeyError:
                answer = views[key] = fn(text)
            return answer if fresh is None else fresh(answer)

        return read

    return mark
