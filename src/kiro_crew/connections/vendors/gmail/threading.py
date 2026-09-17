"""Reply-threading header semantics: ``In-Reply-To`` / ``References`` / ``Subject``.

Threading a reply correctly is three separate rules, each implemented here as a
pure function over the parent message's own header values:

* **``In-Reply-To``** is the parent's own ``Message-ID``, verbatim, angle
  brackets included.
* **``References``** is the parent's ``References`` chain with the parent's
  ``Message-ID`` appended (RFC 5322 §3.6.4). If the parent had no
  ``References``, the chain is just the parent's ``Message-ID``. Duplicates are
  not introduced: a ``Message-ID`` already ending the chain is not appended
  twice.
* **``Subject``** gets a single ``Re:`` prefix. A subject that already begins
  with ``Re:`` (case-insensitive, optionally followed by ``[n]`` count syntax
  some clients emit) is left un-doubled — ``Re: Re: x`` is a bug this function
  exists to avoid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Matches a leading reply prefix: ``Re:``, ``RE:``, ``re:``, and the
# ``Re[2]:`` count form. Only ONE leading occurrence is stripped for the
# has-prefix check; we never strip a run, because "Re: Fwd: x" is meaningful.
_RE_PREFIX = re.compile(r"^\s*re(\[\d+\])?\s*:\s*", re.IGNORECASE)

# A Message-ID token: ``<...>``. Splitting a References chain on whitespace and
# keeping the angle-bracketed tokens is the robust way to normalize it.
_MSGID_TOKEN = re.compile(r"<[^<>]+>")


@dataclass(frozen=True)
class ThreadHeaders:
    """The three computed header values for a reply. Empty string = omit."""

    in_reply_to: str
    references: str
    subject: str


def add_reply_prefix(subject: str) -> str:
    """Return ``subject`` with exactly one leading ``Re:``.

    Idempotent: a subject already carrying a reply prefix is returned with its
    existing prefix intact (not doubled). A blank subject becomes ``Re:``.
    """
    stripped = subject.strip()
    if _RE_PREFIX.match(stripped):
        return stripped
    if not stripped:
        return "Re:"
    return f"Re: {stripped}"


def build_references_chain(parent_references: str, parent_message_id: str) -> str:
    """Append the parent's Message-ID to its References chain.

    ``parent_references`` is the parent message's own ``References`` header
    value (may be empty). ``parent_message_id`` is the parent's ``Message-ID``.
    Returns a whitespace-joined chain of angle-bracketed IDs, with the parent's
    own ID last and never duplicated.
    """
    chain = _MSGID_TOKEN.findall(parent_references or "")
    pid = parent_message_id.strip()
    if pid and not pid.startswith("<"):
        pid = f"<{pid}>"
    if pid:
        # Drop any prior occurrence of the parent id so it lands exactly once,
        # at the end, per RFC 5322 §3.6.4.
        chain = [c for c in chain if c != pid]
        chain.append(pid)
    return " ".join(chain)


def reply_headers(
    parent_message_id: str,
    parent_references: str,
    parent_subject: str,
) -> ThreadHeaders:
    """Compute all three reply-threading headers from the parent's headers.

    ``parent_message_id`` should be the parent's ``Message-ID`` (angle brackets
    optional on input; normalized on output). A blank ``parent_message_id``
    yields empty ``in_reply_to``/``references`` (there is nothing to thread to)
    but the subject is still prefixed.
    """
    pid = parent_message_id.strip()
    if pid and not pid.startswith("<"):
        pid = f"<{pid}>"
    in_reply_to = pid
    references = build_references_chain(parent_references, pid) if pid else ""
    subject = add_reply_prefix(parent_subject)
    return ThreadHeaders(
        in_reply_to=in_reply_to,
        references=references,
        subject=subject,
    )
