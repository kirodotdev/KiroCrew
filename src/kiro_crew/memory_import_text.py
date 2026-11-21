"""``kirocrew memory import-text``: save facts from a plain text or Markdown file.

Reads one fact per line or bullet, lists the facts the default store does not hold and
the owner has not forgotten, and writes only the confirmed ones through
``VectorMemoryStore.set_semantic_if_absent``. The key is a digest of the fact, so a second
run is a no-op. No network call, no model call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.terminal_safe import safe_terminal_line

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

#: Largest file read. A list of facts is a few kilobytes; a bigger file is not one.
MAX_FILE_BYTES = 256 * 1024
#: Most facts proposed in one run, and the longest one fact may be.
MAX_FACTS = 200
MAX_FACT_CHARS = 500
#: A built-in ``user.*`` key; the source marks the row as memory from elsewhere.
KEY_PREFIX = "user.imported_"
SOURCE = "import"

_BULLETS = ("- ", "* ", "+ ", "\u2022 ")


def _fact_from_line(raw: str) -> str:
    """The fact a line states, or ``""`` for a blank, heading, fence or label line."""
    line = raw.strip()
    if line.startswith(("#", "```", "---")) or line.endswith(":"):
        return ""
    for mark in _BULLETS:
        if line.startswith(mark):
            line = line[len(mark) :].strip()
            break
    else:
        # "1. fact" and "2) fact"
        head, _, rest = line.partition(" ")
        if head[:-1].isdigit() and head[-1:] in (".", ")"):
            line = rest.strip()
    return line


def normalize(fact: str) -> str:
    """The form two facts are compared in: whitespace collapsed, case folded."""
    return " ".join(fact.split()).casefold()


def fact_key(fact: str) -> str:
    """The semantic key a fact is stored under."""
    return KEY_PREFIX + hashlib.sha256(normalize(fact).encode("utf-8")).hexdigest()[:16]


def parse_facts(text: str) -> tuple[list[str], int]:
    """``(facts, skipped)``: every distinct fact, and lines over ``MAX_FACT_CHARS``."""
    facts: list[str] = []
    seen: set[str] = set()
    skipped = 0
    for raw in text.splitlines():
        fact = " ".join(_fact_from_line(raw).split())
        if len(fact) < 3 or normalize(fact) in seen:
            continue
        if len(fact) > MAX_FACT_CHARS:
            skipped += 1
            continue
        seen.add(normalize(fact))
        facts.append(fact)
    return facts, skipped


def _held_facts(rows: list[Any]) -> set[str]:
    """Every fact or lesson text in semantic *rows*, in compared form."""
    held: set[str] = set()
    for row in rows:
        try:
            value = json.loads(row["value_json"])
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            value = value.get("rule")
        if isinstance(value, str):
            held.add(normalize(value))
    return held


def _forgotten_keys(store: VectorMemoryStore) -> set[str]:
    """Keys the owner forgot, plus the key each forgotten text would be imported under."""
    # A forget keeps the row, and no public reader returns it; this is get_semantic's read.
    sql = "SELECT key, value_json FROM semantic_memory WHERE is_deleted = 1"
    rows = store._fetch_all_locked(sql)
    return {str(row["key"]) for row in rows} | {fact_key(text) for text in _held_facts(rows)}


def _choose(count: int, args: argparse.Namespace) -> list[int] | None:
    """The 1-based numbers the owner keeps, or ``None`` when nothing is to be saved."""
    from kiro_crew.cli_commands_memory import _interactive_terminal

    if args.dry_run:
        return None
    if args.yes:
        return list(range(1, count + 1))
    if not _interactive_terminal():
        print("No terminal to confirm at; re-run with --yes to save all.")
        return None
    try:
        answer = input("Save which? (numbers such as 1,3 | all | Enter for none): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if answer.casefold() == "all":
        return list(range(1, count + 1))
    chosen: list[int] = []
    for token in answer.replace(",", " ").split():
        if not token.isdigit() or not 1 <= int(token) <= count:
            raise ValueError(
                f"{safe_terminal_line(token)!r} is not one of 1-{count}; nothing saved."
            )
        if int(token) not in chosen:
            chosen.append(int(token))
    return chosen or None


def run(args: argparse.Namespace) -> None:
    """Run ``kirocrew memory import-text`` for *args*; a refusal exits 1 on stderr."""
    from kiro_crew.cli_commands_memory import _admitted_store_path
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.hooks import safe_read_file
    from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE
    from kiro_crew.user_json import strip_utf8_bom
    from kiro_crew.vector_memory import declared_store

    path = Path(args.file)
    name = safe_terminal_line(path.name)
    try:
        if not path.is_file():
            raise FileNotFoundError
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"{name} is larger than {MAX_FILE_BYTES // 1024} KiB; nothing saved.")
        text = safe_read_file(str(path))
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except (OSError, UnicodeError) as exc:
        # Only the class: the exception text quotes the resolved target.
        print(f"Error: {name} could not be read ({type(exc).__name__}).", file=sys.stderr)
        sys.exit(1)
    facts, skipped = parse_facts(strip_utf8_bom(text))
    if not facts:
        print(f"Error: no facts found in {name}; nothing saved.", file=sys.stderr)
        sys.exit(1)
    cfg = KiroCrewConfig.load()
    db_path = _admitted_store_path(DEFAULT_MEMORY_STORE, cfg, may_create=True)
    if db_path is None:
        sys.exit(1)
    store = declared_store(
        db_path, store_id=DEFAULT_MEMORY_STORE, config=cfg, embedding_dim=cfg.memory.embedding_dim
    )
    outcomes: list[str] = []
    try:
        # Only an existing file is opened: opening creates it, and a preview leaves none.
        opened = db_path.exists()
        held: set[str] = set()
        forgotten: set[str] = set()
        if opened:
            store.init()
            held, forgotten = _held_facts(store.get_all_semantic()), _forgotten_keys(store)
        new = [fact for fact in facts if normalize(fact) not in held]
        fresh = [fact for fact in new if fact_key(fact) not in forgotten]
        proposed, over = fresh[:MAX_FACTS], len(fresh) - MAX_FACTS
        more = f" {over} more over the {MAX_FACTS} limit; run again." if over > 0 else ""
        print(
            f"{len(proposed)} new fact(s) in {name}; {len(facts) - len(new)} already known, "
            f"{len(new) - len(fresh)} previously forgotten, {skipped} skipped.{more}"
        )
        for number, fact in enumerate(proposed, 1):
            print(f"  {number}. {safe_terminal_line(fact)}")
        try:
            chosen = _choose(len(proposed), args) if proposed else None
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        if not chosen:
            print("Nothing saved.")
            return
        if not opened:
            store.init()
        for number in chosen:
            fact = proposed[number - 1]
            outcomes.append(store.set_semantic_if_absent(fact_key(fact), fact, 1.0, SOURCE))
    finally:
        store.close()
    saved = outcomes.count("imported")
    print(f"Saved {saved} fact(s); {outcomes.count('existing')} already present.")
    if saved:
        from kiro_crew.cli_commands_memory import _IMPORT_EMBED_NOTE

        print(_IMPORT_EMBED_NOTE)
    refused = outcomes.count("rejected")
    if refused:
        print(f"Error: the store refused {refused} fact(s).", file=sys.stderr)
        sys.exit(1)
