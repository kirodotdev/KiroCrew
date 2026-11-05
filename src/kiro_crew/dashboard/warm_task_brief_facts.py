"""Checked facts for the warm task brief (RFC section 3.9.1, "Checked facts").

A checked fact is something the gateway itself read for this brief (a PR read
live from GitHub, a work item from the ledger), with an id ``fN``. The model
writes typed lines, each listing the ids it states; a line stays only when
every id is in the fact table and matches its kind (exact lookup, no text
scanning). File, deploy and ticket lines always go: no fact covers them yet.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from kiro_crew.dashboard import warm_task_brief_inputs as inputs

# The most facts one brief carries. The prompt's own budgets keep it well
# below (five PR reads; open items fit 9,000 characters, so at most ~150).
MAX_FACTS = 200
# The most typed lines one brief keeps.
MAX_LINES = 12
LINE_KINDS = ("pr", "task", "file", "deploy", "ticket", "none")
# Kinds no checked fact covers yet: a line of one is never stated.
_UNCHECKED_KINDS = frozenset({"file", "deploy", "ticket"})
_SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


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


def live_links(live: Iterable[Mapping[str, Any]]) -> tuple[inputs.PrRef, ...]:
    """The link row (section 3.9.3): each pull request read live, once, its
    URL built from the read's own repo. Model text never adds to it."""
    refs: list[inputs.PrRef] = []
    for entry in live:
        repo, number = entry.get("repo"), str(entry.get("pr", "")).lstrip("#")
        if entry.get("checked") is True and isinstance(repo, str) and number.isdigit():
            ref = inputs.PrRef(repo, int(number))
            if ref not in refs:
                refs.append(ref)
    return tuple(refs)


# Machine values in the words the card shows; any other is lower-cased.
_WORDS = {
    "MERGEABLE": "no conflicts",
    "CLEAN": "ready to merge",
    "CONFLICTING": "has conflicts",
    "DIRTY": "has conflicts",
    "UNKNOWN": "",
    "progress": "ongoing",
    "question": "has a question",
}


def _word(value: object) -> str:
    text = str(value or "")
    return _WORDS.get(text, text.lower().replace("_", " "))


def _checks_text(checks: object) -> str:
    """Every count the read kept, an unknown one included: an unresolved
    check is never shown as all clear. English, for the plain-text copy; the
    card translates the same values."""
    if isinstance(checks, Mapping):
        parts = [f"{checks.get(k, 0)} {w}" for k, w in (("passed", "passed"), ("failed", "failed"))]
        parts += [
            f"{checks[k]} {w}"
            for k, w in (("pending", "running"), ("unknown", "unknown"))
            if checks.get(k)
        ]
        return "checks " + ", ".join(parts)
    return f"checks {checks}" if isinstance(checks, str) else ""


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


def card_facts(facts: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """The facts as ``meta.brief.checked`` stores them for the card."""
    keys = ("id", "kind", "label", "values", "source", "read_at")
    return tuple({key: fact[key] for key in keys if fact.get(key)} for fact in facts)


def raw_lines(value: object, clean: Callable[[object], str]) -> tuple[dict[str, Any], ...]:
    """The model's ``lines`` as typed records, text cleaned, at most
    ``MAX_LINES``. Not yet checked against the facts (``typed_lines``)."""
    rows: list[dict[str, Any]] = []
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, Mapping):
            continue
        text = clean(raw.get("text"))
        kind = raw.get("kind") if raw.get("kind") in LINE_KINDS else "none"
        ids = raw.get("facts")
        refs = [str(r) for r in ids] if isinstance(ids, list) else []
        if text:
            rows.append({"text": text, "kind": kind, "facts": refs})
    return tuple(rows[:MAX_LINES])


def typed_lines(
    lines: Sequence[Mapping[str, Any]], facts: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], ...]:
    """The lines M11 lets stay, by exact lookup in the fact table: every id a
    line lists is a fact; a ``pr`` or ``task`` line lists a fact of its own
    kind; a file, deploy or ticket line goes (no fact covers one yet)."""
    by_id = {fact["id"]: fact for fact in facts}
    kept = []
    for line in lines:
        refs = list(dict.fromkeys(line["facts"]))
        if line["kind"] in _UNCHECKED_KINDS or any(ref not in by_id for ref in refs):
            continue
        if line["kind"] in ("pr", "task") and not any(
            by_id[ref]["kind"] == line["kind"] for ref in refs
        ):
            continue
        kept.append({"text": line["text"], "kind": line["kind"], "facts": refs})
    return tuple(kept)


def marks(refs: Sequence[str]) -> str:
    """Fact ids as the superscript marks the card's Checked rows start with."""
    return ",".join(str(ref)[1:] for ref in refs).translate(_SUPERSCRIPT)


_SOURCES = {"github": "GitHub", "tasks": "task list"}


def fact_words(values: Mapping[str, Any]) -> list[str]:
    """One fact's values as English words, for the plain-text copy."""
    if "not_checked_since" in values:
        return [f"not checked since {values['not_checked_since']}"]
    words = [_word(values.get(k)) for k in ("state", "status", "review", "mergeable")]
    return [w for w in (*words, _checks_text(values.get("checks"))) if w]


def lines_text(
    lines: Sequence[Mapping[str, Any]], checked: Sequence[Mapping[str, Any]], label: str
) -> str:
    """The typed lines as plain text, each with its marks, then the Checked
    rows the marks point to, as the card draws them."""
    rows = [f"- {line['text']}{marks(line['facts'])}" for line in lines]
    if checked:
        rows.append(f"{label}:")
    for fact in checked:
        words = fact_words(fact.get("values") or {})
        values = f": {' · '.join(words)}" if words else ""
        source = _SOURCES.get(str(fact.get("source")), "")
        where = " ".join(str(v) for v in (source, fact.get("read_at")) if v)
        rows.append(
            f"{marks([fact['id']])} {fact['label']}{values}{f' ({where})' if where else ''}"
        )
    return "\n".join(rows)


def checked_brief(brief: Any, facts: Sequence[Mapping[str, Any]], links: tuple[Any, ...]) -> Any:
    """*brief* with M11 applied: the typed lines the fact table backs, the
    link row and the Checked rows."""
    return replace(
        brief,
        lines=typed_lines(brief.lines, facts),
        links=links,
        checked=card_facts(facts),
    )


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
