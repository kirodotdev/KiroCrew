"""One rewrite for the outbound-URL pass and the credential pass together.

Running the two passes one after the other cannot be right in either order. The
URL pass replaces a whole URL, so a credential pass run first can leave a tag
inside a URL the URL pass then fails to recognise. A credential pass run second
sees text the URL pass already rewrote, so a credential whose label sits inside
a removed URL and whose value sits after it loses its anchor and stays.

So the composed pass removes every character that EITHER order removes. It
runs both orders as span plans, maps each second-pass span back onto the
original text, merges all the spans, and rewrites the original once. Each span
writes its own tag, so the result reads like either order's. The credential
pass then runs once more over the result, because a URL tag repeats the URL's
host and that host can itself carry a credential.

:func:`kiro_crew.security.text_redaction.redact_with_findings` is the caller.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right

from kiro_crew.security import exfil, redaction

_Span = tuple[int, int, str]


class _Claimed:
    """Disjoint ``[start, end)`` ranges, kept sorted, with an O(log n) overlap test."""

    def __init__(self) -> None:
        self._starts: list[int] = []
        self._ends: list[int] = []

    def overlaps(self, start: int, end: int) -> bool:
        i = bisect_right(self._starts, start)
        if i > 0 and self._ends[i - 1] > start:
            return True
        return i < len(self._starts) and self._starts[i] < end

    def add(self, start: int, end: int) -> None:
        i = bisect_left(self._starts, start)
        self._starts.insert(i, start)
        self._ends.insert(i, end)


def _url_spans(text: str) -> tuple[list[_Span], list[str]]:
    """Every span the URL pass rewrites in *text*, with its warnings.

    The URL pass replaces every occurrence of each address it flags, longest
    first, so the spans are found the same way.
    """
    removed: list[tuple[str, str]] = []
    _cleaned, warnings, _records = exfil.redact_exfiltration_urls_with_records(
        text, removed_out=removed
    )
    if not removed:
        return [], list(warnings)
    placeholders = dict(removed)
    spans: list[_Span] = []
    claimed = _Claimed()
    for address in sorted(placeholders, key=len, reverse=True):
        found: list[tuple[int, int]] = []
        start = text.find(address)
        while start != -1:
            end = start + len(address)
            if claimed.overlaps(start, end):
                start = text.find(address, start + 1)
                continue
            found.append((start, end))
            spans.append((start, end, placeholders[address]))
            start = text.find(address, end)
        # One address's own matches never overlap, so they join the claimed set
        # only after the whole address has been placed.
        for start, end in found:
            claimed.add(start, end)
    spans.sort()
    return spans, list(warnings)


def _credential_spans(text: str) -> tuple[list[_Span], list[str]]:
    spans, warnings, _rules = redaction._credential_redaction_plan(text)
    return list(spans), list(warnings)


def _splice(text: str, spans: list[_Span]) -> tuple[str, list[tuple[int, int, int, int]]]:
    """*text* with disjoint *spans* replaced, and a map from output to input.

    The map holds one ``(out_start, out_end, in_start, in_end)`` row per output
    piece; a copied piece has equal lengths, a tag piece maps to its whole span.
    """
    parts: list[str] = []
    rows: list[tuple[int, int, int, int]] = []
    cursor = out = 0
    for start, end, tag in spans:
        if start > cursor:
            parts.append(text[cursor:start])
            rows.append((out, out + start - cursor, cursor, start))
            out += start - cursor
        parts.append(tag)
        rows.append((out, out + len(tag), start, end))
        out += len(tag)
        cursor = end
    if cursor < len(text):
        parts.append(text[cursor:])
        rows.append((out, out + len(text) - cursor, cursor, len(text)))
    return "".join(parts), rows


def _map_back(spans: list[_Span], rows: list[tuple[int, int, int, int]]) -> list[_Span]:
    """Spans found on a spliced text, moved onto the text it was spliced from.

    An edge inside a copied piece moves by that piece's offset; an edge inside a
    tag widens to the whole span the tag replaced, so nothing it touched stays.
    """
    starts = [row[0] for row in rows]

    def point(x: int, *, left: bool) -> int:
        i = max(bisect_right(starts, x) - 1, 0)
        if not left and i > 0 and x == rows[i][0]:
            i -= 1  # an end edge belongs to the piece before it
        o_start, o_end, i_start, i_end = rows[i]
        if o_end - o_start == i_end - i_start:
            return i_start + min(max(x - o_start, 0), i_end - i_start)
        return i_start if left else i_end

    return [(point(s, left=True), point(e, left=False), tag) for s, e, tag in spans]


def _splice_union(text: str, spans: list[_Span]) -> str:
    """Remove every character any span covers; write each span's tag once.

    Spans are walked by start, the longer first at a tie. A span already wholly
    covered writes nothing. A span that starts inside the covered run writes its
    tag and extends the run, so no character of any span survives.
    """
    parts: list[str] = []
    cursor = 0
    for start, end, tag in sorted(spans, key=lambda s: (s[0], -s[1])):
        if end <= cursor:
            continue
        if start >= cursor:
            parts.append(text[cursor:start])
        parts.append(tag)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _hide_hosts_with_credentials(text: str, spans: list[_Span]) -> list[_Span]:
    """Drop the host from a URL tag when a credential span overlaps that host.

    A URL tag repeats the URL's host, and a credential can run through the host
    and on into the path, leaving a piece in the host that no later scan would
    recognise. A credential only in the path or query leaves the host as it is.
    """
    prefix = exfil.EXFILTRATION_REDACTION_TAG_PREFIX
    creds = sorted((s, e) for s, e, tag in spans if not tag.startswith(prefix))
    cred_starts = [s for s, _ in creds]
    reach: list[int] = []  # reach[i] = furthest end among creds[: i + 1]
    for _, e in creds:
        reach.append(max(e, reach[-1]) if reach else e)
    out: list[_Span] = []
    for start, end, tag in spans:
        if tag.startswith(prefix):
            scheme = text.find("://", start, end)
            host_start = start if scheme == -1 else scheme + 3
            host_end = host_start
            while host_end < end and text[host_end] not in "/?#:":
                host_end += 1
            i = bisect_left(cred_starts, host_end)
            if i > 0 and reach[i - 1] > host_start:
                tag = f"{prefix}{redaction._REDACTED_CREDENTIAL_TAG}]"
        out.append((start, end, tag))
    return out


def _merge_warnings(first: list[str], second: list[str]) -> list[str]:
    return first + [w for w in second if w not in first]


def compose(text: str) -> tuple[str, list[str], list[str]]:
    """``(text, credential_warnings, url_warnings)`` with both passes applied once."""
    url_spans, url_warnings = _url_spans(text)
    cred_spans, cred_warnings = _credential_spans(text)
    if not url_spans and not cred_spans:
        # Either order is then a no-op on the first pass, so the second pass sees
        # the same text and finds nothing either.
        return text, cred_warnings, url_warnings

    spans = url_spans + cred_spans
    if url_spans:  # URL pass first, then the credential pass on what is left
        after_urls, rows = _splice(text, url_spans)
        found, warnings = _credential_spans(after_urls)
        spans += _map_back(found, rows)
        cred_warnings = _merge_warnings(cred_warnings, warnings)
    if cred_spans:  # credential pass first, then the URL pass on what is left
        after_creds, rows = _splice(text, cred_spans)
        found, warnings = _url_spans(after_creds)
        spans += _map_back(found, rows)
        url_warnings = _merge_warnings(url_warnings, warnings)

    merged = _splice_union(text, _hide_hosts_with_credentials(text, spans))
    merged, again = redaction.redact_credentials(merged)
    return merged, _merge_warnings(cred_warnings, list(again)), url_warnings
