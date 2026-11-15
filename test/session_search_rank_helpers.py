"""A corpus where how often a word repeats and how rare it is point opposite ways."""

from __future__ import annotations

import json
import os
from pathlib import Path

RARE_WORD_QUERY = "tabs kirocrew"


def write_session(base: Path, key: str, content: str, *, mtime: int, memory_mode: str = "") -> None:
    """One transcript titled ``notes`` holding *content*, modified at *mtime*."""
    metadata = {"_type": "metadata", "title": "notes"}
    if memory_mode:
        metadata["memory_mode"] = memory_mode
    path = base / f"{key}.jsonl"
    path.write_text(
        json.dumps(metadata) + "\n" + json.dumps({"role": "user", "content": content}) + "\n",
        encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))


def write_rare_word_corpus(base: Path, *, private_sessions: int = 0) -> None:
    """Every session says ``kirocrew``; only ``chatty`` and ``focused`` say ``tabs``.

    ``chatty`` repeats the common word twenty times and holds the rare one once;
    ``focused`` holds the common word once and the rare one three times. Every
    file has one age, so recency cannot decide the order. :data:`RARE_WORD_QUERY`
    puts the two words in the order neither session uses, so no phrase bonus
    applies. Each private session holds only the rare word, so it never matches
    but would make the rare word common if it were counted.
    """
    mtime = 1_700_000_000
    write_session(base, "chatty", "kirocrew " * 20 + "tabs", mtime=mtime)
    write_session(base, "focused", "kirocrew tabs tabs tabs", mtime=mtime)
    for i in range(4):
        write_session(base, f"filler{i}", "kirocrew notes", mtime=mtime)
    for i in range(private_sessions):
        write_session(base, f"private{i}", "tabs", mtime=mtime, memory_mode="incognito")
