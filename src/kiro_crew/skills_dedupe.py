"""Metadata-only skill de-duplication.

When an auto-skill candidate is generated we must decide whether it duplicates
an existing generated skill *before* it is queued/created. The generated set is
bounded (``skills.max_auto_skills``, ~100) and each skill's identity is captured
by one short metadata line (name + description + triggers), so we can compare the
candidate against **all** existing skills in a single cheap judge call — no
embeddings, no vector index, no top-K pre-filter.

This module is pure orchestration: it builds the judge prompt, parses the judge
reply, and drives an injected ``judge_fn`` (so it is fully unit-testable without
a live model). The caller supplies ``judge_fn`` (typically a Haiku background
call); when unavailable, the caller falls back to the lexical ``find_similar``.
"""

from __future__ import annotations

import re
from typing import Callable, Optional, Sequence

# Sentinel the judge is instructed to return when nothing matches.
_NONE_TOKEN = "NONE"


def _meta_line(entry: dict) -> str:
    """One compact ``key | description | triggers`` line for the judge."""
    key = str(entry.get("key") or entry.get("name") or entry.get("slug") or "").strip()
    desc = re.sub(r"\s+", " ", str(entry.get("description", ""))).strip()
    trig = re.sub(r"\s+", " ", str(entry.get("triggers", ""))).strip()
    line = f"- {key}: {desc}"
    if trig:
        line += f" [triggers: {trig}]"
    return line


def build_dedupe_prompt(candidate: dict, existing: Sequence[dict]) -> str:
    """Build the judge prompt comparing *candidate* against all *existing* skills."""
    existing_block = "\n".join(_meta_line(e) for e in existing) or "(none)"
    cand = _meta_line(candidate)
    return (
        "You are de-duplicating a library of auto-generated agent skills. "
        "A NEW skill candidate was just produced. Decide whether it is "
        "essentially the SAME skill as one that already exists — i.e. it "
        "captures the same procedure / would be triggered by the same "
        "situations, even if the wording differs. Minor scope differences are "
        "NOT duplicates; near-identical purpose IS a duplicate.\n\n"
        f"NEW candidate:\n{cand}\n\n"
        f"EXISTING skills:\n{existing_block}\n\n"
        "Reply with ONLY the exact key of the existing skill it duplicates "
        f"(e.g. `auto/deploy-timeout`), or the single word {_NONE_TOKEN} if it "
        "is genuinely new. Output just the key or "
        f"{_NONE_TOKEN} — no explanation."
    )


def parse_dedupe_response(text: str, valid_keys: Sequence[str]) -> Optional[str]:
    """Extract a matched existing-skill key from the judge reply.

    Returns the matched key iff the reply names one of *valid_keys*; otherwise
    (including an explicit ``NONE``) returns ``None``. Robust to extra prose,
    code fences, and quoting.
    """
    if not text:
        return None
    cleaned = text.strip().strip("`").strip()
    if cleaned.upper() == _NONE_TOKEN or cleaned.upper().startswith(_NONE_TOKEN):
        # Only treat as NONE when no valid key also appears (guards a reply like
        # "NONE of these except auto/foo").
        if not any(k in text for k in valid_keys):
            return None
    # Match ONLY on a whole extracted token. A substring test would let a
    # longer, distinct key (``auto/deploy-helper-v2``) be mis-resolved to a
    # shorter existing one (``auto/deploy-helper``), wrongly rejecting a real
    # new candidate and marking its session consolidated. The tokenizer splits
    # on any char outside ``[A-Za-z0-9._/\-]``, so prose/quoting/fences are
    # still handled.
    tokens = set(re.findall(r"[A-Za-z0-9._/\-]+", text))
    for k in valid_keys:
        if k in tokens:
            return k
    return None


def metadata_dedupe(
    candidate: dict,
    existing: Sequence[dict],
    judge_fn: Optional[Callable[[str], str]],
) -> Optional[str]:
    """Return the key of an existing skill *candidate* duplicates, else ``None``.

    - No existing skills, or no ``judge_fn`` supplied → ``None`` (caller falls
      back to lexical dedupe).
    - Any judge error → ``None`` (fail open; never raise into the caller).
    """
    if not existing or judge_fn is None:
        return None
    valid_keys = [
        str(e.get("key") or e.get("name") or e.get("slug") or "").strip()
        for e in existing
    ]
    valid_keys = [k for k in valid_keys if k]
    if not valid_keys:
        return None
    prompt = build_dedupe_prompt(candidate, existing)
    try:
        reply = judge_fn(prompt)
    except Exception:
        return None
    return parse_dedupe_response(reply or "", valid_keys)
