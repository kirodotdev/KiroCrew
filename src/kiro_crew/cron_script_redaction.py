"""Bounded redaction of a cron child's captured output before a slice of it is reported.

A diagnostic keeps only a head or a tail of what a child printed, and the pattern
redactor reads a fixed window that overshoots that slice instead of the whole
capture. ``_safe_tail_redaction_window`` builds the window for a tail cut, masking a
private-key block or a line the cut severed, which no pattern can recognise once its
anchor is gone.

Part of :mod:`kiro_crew.cron_script`, the import path callers, tests and docs use: it
reaches every name here, and a patch through it reaches this module too
(:mod:`kiro_crew._facade_mirror`).
"""

from __future__ import annotations

import re
from typing import Any

from kiro_crew.security import _REDACTED_CREDENTIAL_TAG, _STREAM_HOLDBACK_JWT_MAX

# How far BEYOND its kept slice each diagnostic-site pattern redaction reads. A
# credential straddling the slice boundary is only detectable while the bytes
# on the far side are still present -- but redacting the WHOLE capture to get
# them costs a multiple of an unbounded string (``proc.communicate`` caps
# neither stream, and ``redact_credentials`` materialises its base64 runs), so
# a script streaming gigabytes would OOM the gateway in redaction that survived
# capture. Redacting a fixed window that overshoots the slice by the streaming
# redactor's credential-holdback ceiling keeps the footprint constant. Two
# credential classes are exempted from the margin because a fixed window
# cannot cover them: granted vault values (shapeless, unbounded -- so
# ``_scrub_grant_values`` runs over the whole capture BEFORE the window is
# cut; exact-substring replacement carries none of the amplification this
# window exists to bound) and severed credentials on the tail-keeping
# window, where the cut removes the ANCHOR a pattern needs --
# ``_safe_tail_redaction_window`` masks both severed shapes as classes
# (label-paired open private-key blocks, and the severed line's leading
# non-whitespace run) rather than per pattern.
_REDACT_STRADDLE_MARGIN = _STREAM_HOLDBACK_JWT_MAX

# How much of a failed script's stderr is reported, taken from the END.
_MAX_SCRIPT_STDERR_TAIL = 500

# How much of an unparsable script stdout is reported, taken from the START.
_MAX_BAD_OUTPUT_HEAD = 200

#: Private-key PEM block markers, with the SAME label class the batch
#: redactor's PEM alternative anchors on (``[A-Z ]*PRIVATE KEY``). Only
#: private-key blocks are tracked: they are the one secret PEM class the
#: redactor masks, and they have no length ceiling, so they are the one class
#: the straddle margin cannot cover on a tail-keeping window. The label is
#: captured so an END can only close a block whose label it MATCHES -- a
#: certificate footer (or any foreign END line) interleaved inside an open
#: private-key block is body text, not a close.
_PEM_KEY_MARKER_RE = re.compile(r"-----(BEGIN|END) ([A-Z ]*PRIVATE KEY)-----")

#: Everything a complete private-key BEGIN marker could start with. Recognises
#: the cut landing INSIDE a marker, where neither the prefix scan (marker
#: incomplete before the cut) nor the window's own pattern pass (anchor
#: destroyed) can see the block that just opened.
_PEM_BEGIN_LITERAL = "-----BEGIN "

_PEM_SEVERED_LABEL_RE = re.compile(r"[A-Z ]*-{0,4}\Z")


def _may_end_inside_begin_marker(pre_cut_line: str) -> bool:
    """True when ``pre_cut_line`` could end with a BEGIN marker cut mid-marker.

    ``pre_cut_line`` is the severed line's content BEFORE the cut (bounded by
    the caller). A marker severed by the cut leaves a nonempty PREFIX of
    ``-----BEGIN <label>-----`` at the line's end -- possibly after arbitrary
    inline prose (``Error: dumping -----BEG``), so the check is on the line's
    SUFFIX, not its start. Two forms: the suffix is a proper prefix of the
    ``-----BEGIN `` literal itself, or the literal is complete and everything
    after it to the cut is label characters plus at most four closing dashes.
    Either way the label (and whether it names a private key) is unknowable
    from this side of the cut alone, so the caller fails closed. A trailing
    dash of ordinary prose also matches the first form; that costs a
    tag-only report for one rare line shape, the safe direction.
    """
    for k in range(1, len(_PEM_BEGIN_LITERAL)):
        if pre_cut_line.endswith(_PEM_BEGIN_LITERAL[:k]):
            return True
    idx = pre_cut_line.rfind(_PEM_BEGIN_LITERAL)
    if idx == -1:
        return False
    return (
        _PEM_SEVERED_LABEL_RE.fullmatch(pre_cut_line[idx + len(_PEM_BEGIN_LITERAL) :]) is not None
    )


def _pem_open_label(text: str, pos: int, endpos: int, open_label: str | None = None) -> str | None:
    """Walk PEM key markers in ``text[pos:endpos]``; return the open label.

    Label-paired: an END closes only the block whose label it matches, so a
    foreign END line (a certificate footer) inside an open key block is body
    text. A BEGIN inside an open block cannot nest (PEM has no nesting): the
    outer block stays open -- fail closed.
    """
    for m in _PEM_KEY_MARKER_RE.finditer(text, pos, endpos):
        kind, label = m.group(1), m.group(2)
        if open_label is None:
            if kind == "BEGIN":
                open_label = label
        elif kind == "END" and label == open_label:
            open_label = None
    return open_label


#: Chunk size and marker carry for ``_pem_state_before``'s streaming scan.
_PEM_SCAN_CHUNK = 64 * 1024

_PEM_SCAN_CARRY = 256


def _pem_state_before(fh: Any, end: int) -> tuple[str | None, str]:
    """Return the private-key block open at byte ``end`` of ``fh``, and a carry.

    Streams ``fh[0:end]`` in fixed chunks, so memory stays bounded however
    large the file is. Markers are ASCII, so latin-1 decoding keeps byte
    offsets and cannot fail. The carry is the unscanned tail (at most
    ``_PEM_SCAN_CARRY`` chars past the last marker); the caller prepends it
    to the text after ``end`` so a marker split by ``end`` is still seen.
    """
    open_label: str | None = None
    carry = ""
    fh.seek(0)
    pos = 0
    while pos < end:
        chunk = fh.read(min(_PEM_SCAN_CHUNK, end - pos))
        if not chunk:
            break
        pos += len(chunk)
        buf = carry + chunk.decode("latin-1")
        last_end = 0
        for m in _PEM_KEY_MARKER_RE.finditer(buf):
            kind, label = m.group(1), m.group(2)
            if open_label is None:
                if kind == "BEGIN":
                    open_label = label
            elif kind == "END" and label == open_label:
                open_label = None
            last_end = m.end()
        carry = buf[max(last_end, len(buf) - _PEM_SCAN_CARRY) :]
    return open_label, carry


def _mask_from_open_block(window: str, label: str) -> str:
    """Mask ``window`` through the labelled END line of an open PEM block."""
    close = window.find(f"-----END {label}-----")
    if close == -1:
        return _REDACTED_CREDENTIAL_TAG
    close_nl = window.find("\n", close)
    kept_after = window[close_nl + 1 :] if close_nl != -1 else ""
    return _REDACTED_CREDENTIAL_TAG + "\n" + kept_after


def _safe_tail_redaction_window(text: str, keep: int) -> str:
    """Return the pattern-redaction input for a TAIL-keeping ``keep`` slice.

    The window is the last ``keep + _REDACT_STRADDLE_MARGIN`` chars of
    ``text``. Cutting there can sever a credential's ANCHOR from the body the
    pattern would mask, so fail-closed rules cover the shapes a severed
    credential can take, without enumerating credential patterns:

    - MULTI-LINE (private-key PEM, unbounded): walk the discarded prefix's
      PEM markers (bounded ``finditer`` state machine, no copies) pairing
      each END with its matching BEGIN label; when the window starts inside a
      block that never closed, mask the retained bytes through that block's
      OWN labelled END line -- or the whole window when it never closes. The
      SEVERED LINE gets the same walk: a BEGIN marker sitting after the cut
      on that line opens a block whose body follows it, so masking the line
      alone would delete the anchor and hand the body to the pattern pass
      unanchored -- the walk continues through the severed segment and an
      open block at its end is masked through its END like any other.

    - SINGLE-LINE (JWT, Bearer, token URL, base64 run): when the window
      starts mid-line, a broken single-line credential can sit anywhere on
      the severed line -- directly at the cut, after whitespace, or stranded
      from an anchor word (``Bearer``) the cut left in the prefix -- so the
      severed line's whole in-window remainder is masked as a unit. The cost
      is one partial line of diagnostics that was already cut anyway. When
      the cut lands inside a BEGIN marker itself -- with or without inline
      prose before the marker on that line -- the block's label is split
      across the cut and unknowable, so the whole window fails closed to the
      tag.
    """
    start = len(text) - (keep + _REDACT_STRADDLE_MARGIN)
    if start <= 0:
        return text
    window = text[start:]
    open_label = _pem_open_label(text, 0, start)
    if open_label is not None:
        return _mask_from_open_block(window, open_label)
    if text[start - 1] not in "\r\n":
        line_start = text.rfind("\n", 0, start) + 1
        pre_cut_line = text[max(line_start, start - 256) : start]
        if _may_end_inside_begin_marker(pre_cut_line):
            return _REDACTED_CREDENTIAL_TAG
        line_end = window.find("\n")
        if line_end == -1:
            return _REDACTED_CREDENTIAL_TAG
        severed_open = _pem_open_label(window, 0, line_end)
        if severed_open is not None:
            # A BEGIN marker after the cut on the severed line: its block's
            # body follows in the remainder, and the line mask below would
            # delete the anchor -- mask through the labelled END instead.
            return _mask_from_open_block(window[line_end:], severed_open)
        return _REDACTED_CREDENTIAL_TAG + window[line_end:]
    return window
