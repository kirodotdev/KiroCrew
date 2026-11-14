"""One session start's reuse of a read every sub-agent target repeats.

A KAS session start projects its own agent and then each sub-agent target its
``availableAgents`` names (``acp.kas_subagent_targets``). Each target asks the same
questions of the same files: which installed app declares an agent, and what each
skill file in scope holds. Inside :func:`one_build` the first answer is kept and
handed to every later asker of the same key, so a start with eight targets reads
each file once instead of eight times.

Nothing outlives the ``with`` block: the next session start reads everything again,
so a file changed between two starts is always read fresh. Outside a block
:func:`reuse` computes every time, exactly as before. A computation that raises
stores nothing, so the next asker tries again, as it did before. The scope is a
``ContextVar``, so a thread or task that did not enter the block sees none.

What one block keeps is bounded: at most :data:`MAX_KEPT` answers whose weights
(each caller's measure of what its answer and key hold, in characters) add up to at
most :data:`MAX_KEPT_WEIGHT`. An answer past either bound is computed and handed
back without being kept, so a large tree costs the old repeated reads, never memory.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypeVar

_T = TypeVar("_T")

#: The most answers one block keeps.
MAX_KEPT = 8192
#: The most weight, in characters, one block keeps.
MAX_KEPT_WEIGHT = 8 * 1024 * 1024


@dataclass
class _Kept:
    answers: dict[Hashable, Any] = field(default_factory=dict)
    weight: int = 0


#: The answers kept for the block in progress, or ``None`` outside one.
_KEPT: ContextVar[_Kept | None] = ContextVar("build_reuse_kept", default=None)


@contextmanager
def one_build() -> Iterator[None]:
    """Keep the answers :func:`reuse` computes until the block ends.

    A nested block keeps using the outer block's answers.
    """
    if _KEPT.get() is not None:
        yield
        return
    token = _KEPT.set(_Kept())
    try:
        yield
    finally:
        _KEPT.reset(token)


def reuse(key: Hashable, compute: Callable[[], _T], *, weight: Callable[[_T], int]) -> _T:
    """*compute*'s answer, kept under *key* for the rest of the block.

    *key* must name every input the answer depends on; the answer must not be
    changed by whoever receives it. *weight* measures, in characters, what keeping
    the answer and *key* holds; an answer that does not fit the block's budget is
    not kept.
    """
    kept = _KEPT.get()
    if kept is None:
        return compute()
    if key in kept.answers:
        return kept.answers[key]  # type: ignore[no-any-return]
    value = compute()
    cost = max(0, weight(value))
    if len(kept.answers) < MAX_KEPT and kept.weight + cost <= MAX_KEPT_WEIGHT:
        kept.answers[key] = value
        kept.weight += cost
    return value


def in_one_build(compute: Callable[[], _T]) -> _T:
    """*compute*'s answer, computed inside :func:`one_build`."""
    with one_build():
        return compute()
