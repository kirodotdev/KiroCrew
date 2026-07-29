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

The judge returns a **tri-state** verdict: a candidate can be genuinely NEW,
a plain DUP (same skill, nothing to add), or an UPDATE (same skill but the
candidate carries meaningful new requirements/steps worth folding into the
existing one). ``metadata_dedupe_verdict`` exposes the full tri-state; the
legacy ``metadata_dedupe`` is a thin wrapper that returns the matched key for
either DUP or UPDATE (else ``None``) so existing callers keep working.
"""

from __future__ import annotations

import re
from typing import Callable, Optional, Sequence, Tuple

# Sentinel the judge is instructed to return when nothing matches.
_NONE_TOKEN = "NONE"
_DUP_TOKEN = "DUP"
_UPDATE_TOKEN = "UPDATE"

# Tri-state verdicts.
VERDICT_NEW = "new"
VERDICT_DUP = "dup"
VERDICT_UPDATE = "update"


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
        "Reply with EXACTLY ONE of the following forms — nothing else, no "
        "explanation:\n"
        f"- `{_NONE_TOKEN}` — the candidate is genuinely new; no existing skill "
        "covers it.\n"
        f"- `{_DUP_TOKEN} <key>` — the candidate is the same skill as <key> and "
        "adds nothing new (a plain duplicate).\n"
        f"- `{_UPDATE_TOKEN} <key>` — the candidate is the same skill as <key> "
        "but carries meaningful NEW requirements or steps worth folding into "
        "the existing skill.\n\n"
        "<key> must be the exact key of an existing skill above (e.g. "
        f"`{_DUP_TOKEN} auto/deploy-timeout`)."
    )


def _tokens(text: str) -> set:
    """Whole-token set from *text* (split on any char outside key chars).

    Matching ONLY on whole tokens avoids a substring test resolving a longer,
    distinct key (``auto/deploy-helper-v2``) to a shorter existing one
    (``auto/deploy-helper``). Prose, quoting, and fences are still handled.
    """
    return set(re.findall(r"[A-Za-z0-9._/\-]+", text))


def _match_key(text: str, valid_keys: Sequence[str]) -> Optional[str]:
    """Return the first *valid_keys* entry that appears as a whole token."""
    toks = _tokens(text)
    for k in valid_keys:
        if k in toks:
            return k
    return None


def parse_dedupe_response(text: str, valid_keys: Sequence[str]) -> Optional[str]:
    """Extract a matched existing-skill key from the judge reply.

    Returns the matched key iff the reply names one of *valid_keys*; otherwise
    (including an explicit ``NONE``) returns ``None``. Robust to extra prose,
    code fences, and quoting.

    This is the legacy (binary) parser. It is retained for backward
    compatibility and treats any reply that names a valid key — with or without
    a ``DUP``/``UPDATE`` prefix — as a match.
    """
    if not text:
        return None
    cleaned = text.strip().strip("`").strip()
    if cleaned.upper() == _NONE_TOKEN or cleaned.upper().startswith(_NONE_TOKEN):
        # Only treat as NONE when no valid key also appears (guards a reply like
        # "NONE of these except auto/foo").
        if not any(k in text for k in valid_keys):
            return None
    return _match_key(text, valid_keys)


def parse_dedupe_verdict(
    text: str, valid_keys: Sequence[str]
) -> Tuple[str, Optional[str]]:
    """Parse a tri-state verdict from the judge reply.

    Returns ``(verdict, key)`` where *verdict* is one of ``VERDICT_NEW``,
    ``VERDICT_DUP``, ``VERDICT_UPDATE``:

    - empty / ``NONE`` / unparseable → ``(VERDICT_NEW, None)``.
    - ``DUP <key>`` naming a valid key → ``(VERDICT_DUP, key)``.
    - ``UPDATE <key>`` naming a valid key → ``(VERDICT_UPDATE, key)``.
    - a bare ``<key>`` with no prefix (backward compat) → ``(VERDICT_DUP, key)``.
    - a ``DUP``/``UPDATE`` whose key is not valid, or ``NONE`` → ``(VERDICT_NEW,
      None)``.

    Robust to code fences, quoting, and surrounding prose.
    """
    if not text:
        return (VERDICT_NEW, None)
    cleaned = text.strip().strip("`").strip().strip("`").strip()
    upper = cleaned.upper()

    # Explicit NONE (and no valid key smuggled alongside) → new.
    if upper == _NONE_TOKEN or upper.startswith(_NONE_TOKEN):
        if not _match_key(text, valid_keys):
            return (VERDICT_NEW, None)

    toks = _tokens(text)
    upper_toks = {t.upper() for t in toks}
    key = _match_key(text, valid_keys)

    has_update = _UPDATE_TOKEN in upper_toks
    has_dup = _DUP_TOKEN in upper_toks

    if has_update:
        return (VERDICT_UPDATE, key) if key else (VERDICT_NEW, None)
    if has_dup:
        return (VERDICT_DUP, key) if key else (VERDICT_NEW, None)

    # No prefix keyword: a bare key is treated as a plain duplicate (backward
    # compatible with the old binary judge contract).
    if key:
        return (VERDICT_DUP, key)
    return (VERDICT_NEW, None)


def _valid_keys(existing: Sequence[dict]) -> list:
    keys = [
        str(e.get("key") or e.get("name") or e.get("slug") or "").strip()
        for e in existing
    ]
    return [k for k in keys if k]


def metadata_dedupe_verdict(
    candidate: dict,
    existing: Sequence[dict],
    judge_fn: Optional[Callable[[str], str]],
) -> Tuple[str, Optional[str]]:
    """Return a tri-state ``(verdict, key)`` for *candidate* vs *existing*.

    - No existing skills, or no ``judge_fn`` supplied → ``(VERDICT_NEW, None)``
      (caller falls back to lexical dedupe).
    - Any judge error → ``(VERDICT_NEW, None)`` (fail open; never raise).
    - ``NONE`` → ``(VERDICT_NEW, None)``.
    - ``DUP <key>`` → ``(VERDICT_DUP, key)``.
    - ``UPDATE <key>`` → ``(VERDICT_UPDATE, key)``.
    - bare ``<key>`` → ``(VERDICT_DUP, key)`` (backward compat).
    """
    if not existing or judge_fn is None:
        return (VERDICT_NEW, None)
    valid_keys = _valid_keys(existing)
    if not valid_keys:
        return (VERDICT_NEW, None)
    prompt = build_dedupe_prompt(candidate, existing)
    try:
        reply = judge_fn(prompt)
    except Exception:
        return (VERDICT_NEW, None)
    return parse_dedupe_verdict(reply or "", valid_keys)


def metadata_dedupe(
    candidate: dict,
    existing: Sequence[dict],
    judge_fn: Optional[Callable[[str], str]],
) -> Optional[str]:
    """Return the key of an existing skill *candidate* duplicates, else ``None``.

    Thin wrapper over :func:`metadata_dedupe_verdict`: returns the matched key
    for either a ``DUP`` or an ``UPDATE`` verdict, else ``None``. Preserves the
    original binary semantics for existing callers.

    - No existing skills, or no ``judge_fn`` supplied → ``None``.
    - Any judge error → ``None`` (fail open; never raise into the caller).
    """
    verdict, key = metadata_dedupe_verdict(candidate, existing, judge_fn)
    if verdict in (VERDICT_DUP, VERDICT_UPDATE):
        return key
    return None
