"""What the warm welcome reads before it posts (RFC section 3.9.1): which pull
requests to read live, and the digest that stops a re-send when nothing changed.
The gateway renders every fact (section 3.11).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from kiro_crew.dashboard import warm_task_brief_inputs as inputs


def live_refs(
    work_items: Sequence[Mapping[str, Any]],
    terminal: Callable[[object], bool],
) -> list[inputs.PrRef]:
    """The pull requests to read before the welcome shows their state (M3): each
    open ask item's ledger PR, then every other open item's, at most
    :data:`inputs.MAX_LIVE_PRS`. Only a ledger PR is read: the welcome shows PR
    state only on its item's line, so a URL in an item's text is never read."""
    asks = [item for item in work_items if inputs.is_open_ask(item, terminal)]
    rest = [item for item in work_items if not terminal(item.get("state")) and item not in asks]
    return inputs.pr_refs((), [r for i in (*asks, *rest) if (r := inputs.ledger_ref(i))])


#: A PR read's time-only fields: they change as time passes, never with the facts.
_READ_TIME_KEYS = ("read_at", "not_checked_since", "_read_epoch")


def timeless(live: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """*live* with every read-time field emptied, for a render of the facts alone."""
    return tuple({**entry, **{k: "" for k in _READ_TIME_KEYS if k in entry}} for entry in live)


def facts_digest(content: str) -> str:
    """What a welcome said, rendered from :func:`timeless` reads at a fixed time,
    so equal digests mean nothing it would say changed since, whatever input
    moved. Every input the renderer reads is covered, none named here."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


FACTS_DIGEST_KEY = "facts_digest"


def previous_facts_digest(
    rows: Sequence[Mapping[str, Any]], result_kind: str, miss_key: str
) -> str:
    """The facts digest of the newest delivered brief, or ""."""
    for row in reversed(rows):
        meta = row.get("meta")
        if isinstance(meta, Mapping) and meta.get("kind") == result_kind:
            digest = meta.get(FACTS_DIGEST_KEY)
            return digest if isinstance(digest, str) and not meta.get(miss_key) else ""
    return ""
