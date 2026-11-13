"""Disposition outcomes and the per-head rounds that ``--rounds`` and the push check read.

``_review_contract.py`` re-exports every name here, and callers import them from
there. ``_review_pushes.py`` loads this module, and the contract takes it from that
load. The fence readers come from ``_review_fences.py``.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import re


class _NoBytecodeSourceLoader(importlib.machinery.SourceFileLoader):
    """Load shipped source normally while suppressing cache writes."""

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)

    def set_data(self, path, data, *, _mode=0o666):
        return None


def _load_sibling(filename, name):
    """Load a sibling script without cwd, sys.path, or bytecode side effects."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    loader = _NoBytecodeSourceLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:  # pragma: no cover - defensive
        raise RuntimeError("cannot import kirocrew-prepare-pr sibling script: " + path)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _load_owner(stem):
    """Load ``<stem>.py`` beside this script, named under this module's own package."""
    package = __name__.rpartition(".")[0]
    return _load_sibling(stem + ".py", package + "." + stem if package else stem)


_fences = _load_owner("_review_fences")
unfenced_lines = _fences.unfenced_lines


# --- Disposition outcomes and rounds: the one reader for --rounds and the push check -----
#
# The outcome is read from exactly two places, never from prose:
#   (a) the FIRST quoted line of the disposition (its rationale lead):
#       `> fixed in <sha>`  `> Fixed: ...`  `> removed in <sha>`  `> **fixed** in <sha>`
#       `> Rebutted: ...`  `> deferred #N`  `> accepted: ...`  `> Agreed, no change.`
#       `> Outcome: fixed in <sha>`  `> Outcome: checked, no change needed`
#   (b) an explicit label line: `Outcome: <word>`, `Disposition: <word>` /
#       `**Disposition: <word>**`, or, when it starts the record, a bold token that is
#       exactly an outcome word: `- **fixed** span=<id>`, `**rebutted** -- span=<id>`.
# A finding's title, a bold title with other words, and the plain metadata lines
# (`class:`, `mechanism:`, `self-added:`, `resolution:`, `[operator: ...]`) never declare
# an outcome. A record that declares none is `undeclared` in the view, never counted as
# a fix and never silently nothing (sample and counts: code-quality/impl/D5-FORMAT-SURVEY.md).
FIX_OUTCOMES = frozenset({"fixed", "removed", "reverted"})
OTHER_OUTCOMES = frozenset(
    {
        "rebutted",
        "deferred",
        "acknowledged",
        "accepted",
        "agreed",
        "intended",
        "checked",
        "kept",
        "declined",
    }
)
OUTCOME_WORDS = FIX_OUTCOMES | OTHER_OUTCOMES

_METADATA_RE = re.compile(r"^(?:self-added|mechanism|class|resolution)\s*:", re.IGNORECASE)
_OPERATOR_RE = re.compile(r"^\[operator:[^\]]*\]\s*$", re.IGNORECASE)
_OUTCOME_LABEL_RE = re.compile(r"^\**Outcome\**\s*:\**\s*([A-Za-z]+)", re.IGNORECASE)
_DISPOSITION_LINE_RE = re.compile(r"^\**Disposition\**\s*:\**\s*\**([A-Za-z]+)", re.IGNORECASE)
_BOLD_TOKEN_RE = re.compile(r"^(?:[-*]\s+)?\*\*([A-Za-z]+)\*\*(?=\s|[.:,;!]|$)")
_QUOTED_OUTCOME_RE = re.compile(r"^>\s*\*{0,2}Outcome\*{0,2}:\*{0,2}\s*([A-Za-z]+)", re.IGNORECASE)
_QUOTED_LEAD_RE = re.compile(r"^>\s*(\*{0,2})([A-Za-z]+)(\*{0,2})(.*)$")
_IN_SHA_RE = re.compile(r"^\s+in\s+[0-9a-f]{7,40}\b", re.IGNORECASE)
_RESOLUTION_LINE_RE = re.compile(r"^resolution:\s*(removed|narrowed|deferred)\b", re.IGNORECASE)


def _quoted_lead_word(line):
    """The outcome word that leads a quoted rationale line, or "".

    `> **fixed** in <sha>` (bold) and `> fixed: ...` / `> fixed.` accept any
    follow-on. A bare fix word needs `in <sha>` ("> Fixed point iteration" and
    "> removed sessions still listed" are not outcomes); the other words accept a
    following blank ("> Agreed, no change."). `> Outcome: <word>` names its word.
    """
    m = _QUOTED_OUTCOME_RE.match(line)
    if m:
        return m.group(1).lower()
    m = _QUOTED_LEAD_RE.match(line)
    if not m:
        return ""
    open_bold, word, close_bold, rest = m.groups()
    word = word.lower()
    if word not in OUTCOME_WORDS:
        return ""
    if open_bold and close_bold:
        return word
    if not rest or rest[0] in ":.,;!":
        return word
    if word in FIX_OUTCOMES:
        return word if _IN_SHA_RE.match(rest) else ""
    return word if rest[0].isspace() else ""


def declared_outcome(body):
    """The lower-case outcome word a disposition declares, or "" when it declares none.

    Only the two places named above are read, outside fenced code, and the first
    declaration in document order wins. Nothing else counts: not a finding title,
    not a bold title with other words ("**Fixed point iteration is slow**"), not a
    `class:` / `mechanism:` / `self-added:` / `resolution:` line, not any unquoted
    sentence, and not a later quoted line (a rebuttal quoting "- **fixed** ..." stays
    a rebuttal).
    """
    first_quote = True
    started = False
    for raw in unfenced_lines(body, quoted=True):
        line = raw.strip()
        if not line or line.startswith("<!--"):
            continue
        if line.startswith(">"):
            if first_quote:
                first_quote = False
                word = _quoted_lead_word(line)
                if word:
                    return word
            continue
        if _METADATA_RE.match(line) or _OPERATOR_RE.match(line):
            continue
        for pattern in (_OUTCOME_LABEL_RE, _DISPOSITION_LINE_RE):
            m = pattern.match(line)
            if m and m.group(1).lower() in OUTCOME_WORDS:
                return m.group(1).lower()
        if not started:
            m = _BOLD_TOKEN_RE.match(line)
            if m and m.group(1).lower() in OUTCOME_WORDS:
                return m.group(1).lower()
        started = True
    return ""


def declared_resolution(body):
    """`removed`, `narrowed` or `deferred` from a plain `resolution:` line, else "".

    The line sits outside the quoted block, like `self-added:`; the sweep file
    holds the same line for the push check.
    """
    for raw in unfenced_lines(body, quoted=True):
        line = raw.strip()
        if line.startswith(">"):
            continue
        m = _RESOLUTION_LINE_RE.match(line)
        if m:
            return m.group(1).lower()
    return ""


def same_head(a, b):
    """True when two hex spellings (7 to 40 characters) can name one commit."""
    a, b = a.lower(), b.lower()
    return a.startswith(b) or b.startswith(a)


def disposition_rounds(records, bodies):
    """(order, rounds) from writer disposition records: one round per judged head.

    `order` lists the round keys in first-disposed order; `rounds[key]` holds the
    full `head`, the `fixed` span set, the `resolved` {span: word} map, the round's
    `resolution` word (set by any record that declares one, spans or not), an
    `outcomes` count by word (`undeclared` for a record that declares none) and the
    `undeclared` [(comment id, spans)] list. A 7-character and a 40-character
    spelling of one head are one round.
    """
    order: list = []
    rounds: dict = {}
    for rec in records:
        head = rec.get("head") or ""
        if rec.get("malformed") or not head:
            continue
        key = next((k for k in order if same_head(rounds[k]["head"], head)), None)
        if key is None:
            key = head[:12]
            rounds[key] = {
                "head": head,
                "fixed": set(),
                "resolved": {},
                "resolution": "",
                "outcomes": {},
                "undeclared": [],
            }
            order.append(key)
        r = rounds[key]
        if len(head) > len(r["head"]):
            r["head"] = head
        body = bodies.get(rec.get("comment_id"), "")
        spans = list(rec.get("spans") or [])
        word = declared_outcome(body)
        name = word or "undeclared"
        r["outcomes"][name] = r["outcomes"].get(name, 0) + 1
        if not word:
            r["undeclared"].append((rec.get("comment_id"), spans))
        if word in FIX_OUTCOMES:
            r["fixed"].update(spans)
        resolution = declared_resolution(body)
        if resolution:
            r["resolution"] = resolution
            for span in spans:
                r["resolved"][span] = resolution
    return order, rounds


def fix_rounds(order, rounds):
    """{span: [round index, ...]}: the distinct judged heads that fixed each span.

    Fixes at or before the round that recorded a `resolution:` for the span do not
    count, so a span with a recorded narrow, removal or deferral starts again.
    """
    fixed: dict = {}
    resolved_at: dict = {}
    for idx, key in enumerate(order):
        r = rounds[key]
        for span in r["resolved"]:
            resolved_at[span] = idx
        for span in sorted(r["fixed"]):
            fixed.setdefault(span, []).append(idx)
    out = {}
    for span, rs in fixed.items():
        after = [i for i in rs if i > resolved_at.get(span, -1)]
        if after:
            out[span] = after
    return out
