"""What the warm task brief reads before it writes (RFC section 3.9.1): which
pull requests to read live, the numbered facts the model's snapshot carries as
context, and the digest that stops a re-send when nothing changed. The model
states none of these facts; the gateway renders them (section 3.10.4).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from kiro_crew.dashboard import warm_task_brief_inputs as inputs

# The most facts one brief carries. The prompt's own budgets keep it well
# below (five PR reads; open items fit 9,000 characters, so at most ~150).
MAX_FACTS = 200


def _ask_texts(
    waiting: Sequence[Mapping[str, Any]], asks: Sequence[Mapping[str, Any]]
) -> list[str]:
    """The text of every open ask, where the live read looks for full PR URLs."""
    texts = [
        part
        for item in waiting
        for part in (str(item.get("text") or ""), *(str(o) for o in item.get("options") or []))
        if part
    ]
    for item in asks:
        values = (item.get("title"), item.get("summary"), item.get("decision"))
        texts.extend(str(v) for v in (*values, *(item.get("artifacts") or {}).values()) if v)
    return texts


def live_refs(
    work_items: Sequence[Mapping[str, Any]],
    waiting: Sequence[Mapping[str, Any]],
    terminal: Callable[[object], bool],
) -> list[inputs.PrRef]:
    """The pull requests to read before the brief may name them (M3): each
    open ask item's ledger PR, then full PR URLs in the asks' text, then
    every other open item's ledger PR. A bare ``#N`` is never guessed."""
    asks = [item for item in work_items if inputs.is_open_ask(item, terminal)]
    rest = [item for item in work_items if not terminal(item.get("state")) and item not in asks]
    ledger = [ref for item in asks if (ref := inputs.ledger_ref(item)) is not None]
    refs = inputs.pr_refs(_ask_texts(waiting, asks), ledger)
    for item in rest:
        ref = inputs.ledger_ref(item)
        if ref is not None and ref not in refs and len(refs) < inputs.MAX_LIVE_PRS:
            refs.append(ref)
    return refs


def checked_facts(
    live: Sequence[Mapping[str, Any]], open_items: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The facts the gateway read for this brief, numbered ``f1``…: each PR
    the live read covered (one it could not read states only "not checked
    since HH:MM"), then every work item the prompt shows, as the ledger holds
    them now."""
    facts: list[dict[str, Any]] = []
    for entry in live:
        # Machine values, as read: the card puts them in the owner's language.
        if entry.get("checked") is not True:
            values = {"not_checked_since": entry.get("not_checked_since") or "never"}
        else:
            keys = ("state", "review", "mergeable", "checks")
            values = {k: entry[k] for k in keys if entry.get(k)}
        facts.append(
            {
                "kind": "pr",
                "pr": entry.get("pr"),
                "repo": entry.get("repo"),
                "label": f"{entry.get('repo')}{entry.get('pr')}",
                "values": values,
                "source": "github",
                "read_at": entry.get("read_at", ""),
            }
        )
    for item in open_items:
        if not (item.get("title") or item.get("item_id")):
            continue
        state = item.get("state")
        values = {"state": state if state != "open" else "", "status": item.get("status")}
        facts.append(
            {
                "kind": "task",
                "item_id": item.get("item_id"),
                "label": str(item.get("title") or item.get("item_id") or ""),
                "values": {k: v for k, v in values.items() if v},
                "source": "tasks",
            }
        )
    return [{"id": f"f{n}", **fact} for n, fact in enumerate(facts[:MAX_FACTS], 1)]


def facts_digest(ledger_digest: str, waiting: object, live: Sequence[Mapping[str, Any]]) -> str:
    """What a brief stood on: the stored ledgers, what waited and each PR
    read, minus read times. Equal digests mean nothing changed since."""
    reads = [
        {k: v for k, v in entry.items() if k not in ("read_at", "not_checked_since", "_read_epoch")}
        for entry in live
    ]
    blob = json.dumps(
        [ledger_digest, waiting, reads], ensure_ascii=False, sort_keys=True, default=str
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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
