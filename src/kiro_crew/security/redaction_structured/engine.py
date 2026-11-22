"""Compose the detectors, resolve overlaps and replace Secret spans.

    text --> classify tokens (B) --+
         \\-> armour markers (C) ---+--> resolve --> replace --> spans
          \\-> known-key inputs (A) +
           \\-> structural Identifiers

Resolve keeps one hit per character, in this order:

1. A structural Identifier (ARN, URL scheme/host/path, commit permalink, public
   OAuth parameter, git object name) that fully contains a label or pairing Secret
   hit wins; the Secret hit is dropped. It never overrides a checksum,
   fixed-format, armour, JOSE or URL-parameter hit. Exception (1a): the git
   object-name row does not override an exact AWS label hit (Detector A); it still
   overrides key-ID pairing.
2. Higher tier (Secret over Identifier) for any other overlap.
3. Overlapping Secret hits are replaced as one span (their union), named by the
   longest member, then the more certain detector, then the lower rule id.

Cost: the text is lowered once per scan and shared by every case-insensitive reader.
Each detector first tests its literal anchors (a two-character prefix head for format
rows, a key word for known keys, a row's own literals for Identifier values) and does
no other work when none is present; every lookup derived from the rows is built once
per rows tuple (``charsets.cached``).
"""

from __future__ import annotations

import bisect
from collections.abc import Callable
from dataclasses import dataclass, replace

from .armour import detect_armour
from .charsets import ascii_lower, cached
from .encoded import detect_encoded
from .headers import detect_headers
from .identifier_values import detect_identifier_values
from .identifiers import (
    ObjectNamePlan,
    detect_arns,
    detect_object_names,
    detect_urls,
    object_name_plan,
)
from .known_key import detect_known_keys
from .model import (
    CONTAINABLE_DETECTORS,
    IDENTIFIER,
    LABEL_BEATS_RULES,
    PLAN_TAGS,
    SECRET,
    TAG,
    Hit,
    Span,
)
from .table import TABLE, StructureRow, Table
from .tokens import detect_formats, detect_jose, detect_pairs

_NONE: tuple[list[Hit], list[int]] = ([], [])


@dataclass(frozen=True, slots=True)
class ScanResult:
    hits: list[Hit]
    open_from: int | None
    # The earliest undecided unit that can change what is replaced: ``open_from``
    # without the units only an Identifier-value row reads (a data URI payload, a
    # request id), whose hits replace nothing and override no Secret hit.
    replace_open_from: int | None = None


def scan(
    text: str, final: bool = True, origin: int = 0, table: Table = TABLE, depth: int = 0
) -> ScanResult:
    """Every detector's raw hits, and the earliest undecided unit at or after ``origin``.

    ``depth`` counts the decodes the encoded detector went through to reach ``text``.
    """
    hits: list[Hit] = []
    opens: list[int] = []
    # One lowered copy, shared by every case-insensitive reader.
    low = ascii_lower(text)
    value_hits, value_opens = detect_identifier_values(text, table.structures, final, low)
    hits += value_hits

    found, op = detect_formats(text, table.formats, final)
    hits += found
    opens += op
    key_ids = [(h.start, h.end) for h in found if h.rule_id == "aws.access_key_id"]

    for detected in (
        detect_pairs(text, table.pairs, key_ids, final) if key_ids or not final else _NONE,
        detect_jose(text, table.jose, final),
        detect_armour(text, table.armour, final),
        detect_known_keys(text, table.known_keys, final, low),
        detect_urls(
            text, table.structures, final, table.url_params, table.userinfo, table.path_secrets
        ),
        detect_headers(text, table.headers, final, low),
        detect_encoded(text, table.encoded, final, depth, _holds_secret(table), origin),
    ):
        if detected[0]:
            hits += detected[0]
        if detected[1]:
            opens += detected[1]
    for row in cached(table.structures, _arn_rows):
        found, op = detect_arns(text, row, final)
        hits += found
        opens += op
    for plan in cached(table.structures, _object_name_plans):
        found, op = detect_object_names(text, plan, final)
        hits += found
        opens += op

    # An open unit runs to the end of the text, so one that started before
    # ``origin`` (its label already streamed) still holds the text after it.
    live = [max(o, origin) for o in opens]
    replace_from = min(live) if live else None
    live += [max(o, origin) for o in value_opens]
    return ScanResult(_outside_tags(text, hits), min(live) if live else None, replace_from)


def _outside_tags(text: str, hits: list[Hit]) -> list[Hit]:
    """*hits* without each Secret hit that reads only the text of a placeholder.

    A placeholder holds no value, so a hit inside one is a reader taking the tag
    for a value; replacing it would write a second tag into the first. Dropping it
    keeps masked text a fixed point: redacting it again is a byte-for-byte no-op.
    """
    if "[REDACTED" not in text or not hits:
        return hits
    tags: list[tuple[int, int]] = []
    for tag in PLAN_TAGS:
        at = text.find(tag)
        while at != -1:
            tags.append((at, at + len(tag)))
            at = text.find(tag, at + len(tag))
    if not tags:
        return hits
    tags.sort()
    starts = [start for start, _end in tags]

    def inside(h: Hit) -> bool:
        k = bisect.bisect_right(starts, h.start) - 1
        return k >= 0 and h.end <= tags[k][1]

    return [h for h in hits if h.tier != SECRET or not inside(h)]


def _holds_secret(table: Table) -> Callable[[str, int], bool]:
    """A reader for the encoded detector: does *decoded*, read at *depth*, hold a Secret?

    It reads the decoded text with the table, as the plan reads plain text, so an
    encoded copy of any credential a row reads is masked as its plain form is.
    """

    def holds(decoded: str, depth: int) -> bool:
        hits = scan(decoded, final=True, table=table, depth=depth).hits
        return any(h.tier == SECRET for h in resolve(hits))

    return holds


def _arn_rows(rows: tuple[StructureRow, ...]) -> tuple[StructureRow, ...]:
    return tuple(row for row in rows if row.kind == "arn")


def _object_name_plans(rows: tuple[StructureRow, ...]) -> tuple[ObjectNamePlan, ...]:
    return tuple(object_name_plan(row) for row in rows if row.kind == "object_name")


def resolve(hits: list[Hit]) -> list[Hit]:
    """Resolve overlaps; returns the kept hits in text order."""
    if len(hits) < 2:
        return list(hits)
    structural = sorted(
        (h for h in hits if h.tier == IDENTIFIER and h.structural), key=lambda h: h.start
    )
    # Rule 1a: an exact AWS label (Detector A) beats the git object-name row; only the
    # other structural rows contain a label hit. Pairing still loses to every row.
    container = _containment(structural)
    label_container: list[Callable[[Hit], bool]] = []  # built on the first label hit

    def dropped(h: Hit) -> bool:
        if h.detector not in CONTAINABLE_DETECTORS:
            return False
        if h.detector != "known_key":
            return container(h)
        if not label_container:
            others = [s for s in structural if s.rule_id not in LABEL_BEATS_RULES]
            label_container.append(
                container if len(others) == len(structural) else _containment(others)
            )
        return label_container[0](h)

    secrets = sorted(
        (h for h in hits if h.tier == SECRET and not dropped(h)),
        key=lambda h: (h.start, -h.end),
    )
    merged: list[Hit] = []
    cluster: list[Hit] = []
    end = -1
    for h in secrets:
        if cluster and h.start < end:
            cluster.append(h)
            end = max(end, h.end)
            continue
        if cluster:
            merged.append(_merge(cluster, end))
        cluster, end = [h], h.end
    if cluster:
        merged.append(_merge(cluster, end))

    covered_starts = [h.start for h in merged]

    def overlaps_secret(h: Hit) -> bool:
        i = bisect.bisect_right(covered_starts, h.end - 1) - 1
        return i >= 0 and merged[i].end > h.start

    kept_ids: list[Hit] = []
    last_end = -1
    for h in sorted((h for h in hits if h.tier == IDENTIFIER), key=lambda h: (h.start, -h.end)):
        if h.start < last_end or overlaps_secret(h):
            continue
        kept_ids.append(h)
        last_end = h.end
    return sorted(merged + kept_ids, key=lambda h: (h.start, h.end))


def _containment(structural: list[Hit]) -> Callable[[Hit], bool]:
    """A test: is a hit fully inside one of ``structural`` (sorted by start)?"""
    if not structural:
        return lambda h: False
    starts = [h.start for h in structural]
    reach: list[int] = []
    for h in structural:
        reach.append(max(reach[-1], h.end) if reach else h.end)

    def contained(h: Hit) -> bool:
        i = bisect.bisect_right(starts, h.start) - 1
        return i >= 0 and reach[i] >= h.end

    return contained


def _merge(cluster: list[Hit], end: int) -> Hit:
    if len(cluster) == 1:
        return cluster[0]
    # The member covering the most characters names the span; on a tie the more
    # certain detector, then the lower rule id.
    best = min(cluster, key=lambda h: (-(h.end - h.start), h.rank_key()))
    start = min(h.start for h in cluster)
    tail = next((h.tail for h in cluster if h.tail and h.end == end), "")
    anchors = [h.anchor for h in cluster if h.anchor >= 0]
    return replace(best, start=start, end=end, tail=tail, anchor=min(anchors, default=-1))


def render(text: str, resolved: list[Hit], lo: int, hi: int) -> str:
    """``text[lo:hi]`` with every Secret hit inside it replaced by the tag."""
    out: list[str] = []
    pos = lo
    for h in resolved:
        if h.tier != SECRET or h.start < lo or h.end > hi:
            continue
        out.append(text[pos : h.start])
        out.append(TAG)
        pos = h.end
    out.append(text[pos:hi])
    return "".join(out)


def to_spans(resolved: list[Hit], offset: int = 0) -> list[Span]:
    return [(h.start + offset, h.end + offset, h.rule_id, h.tier) for h in resolved]


def redact_hits(text: str) -> tuple[str, list[Hit]]:
    """Whole-text redaction, returning the resolved hits (with detector)."""
    hits = scan(text, final=True).hits
    if not hits:
        return text, []
    resolved = resolve(hits)
    return render(text, resolved, 0, len(text)), resolved


def redact(text: str) -> tuple[str, list[Span]]:
    """Redact ``text``; spans are ``(start, end, rule_id, tier)`` over the INPUT text.

    Only ``tier == "secret"`` spans are replaced; Identifier spans are reported so a
    caller can check what was kept on purpose.
    """
    out, resolved = redact_hits(text)
    return out, to_spans(resolved)
