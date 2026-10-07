"""Cut an agent cron's answer down to the block its author marked as the deliverable.

A cron agent's answer text carries everything the model wrote during the turn,
including narration such as "I'll run the checks... now checking..." ahead of
the digest the job exists to produce. That narration is ordinary answer text,
not reasoning, so no renderer flag can tell it apart. The job's author
can: wrapping the digest in ``<deliverable>...</deliverable>`` in the agent's
answer makes the cron deliver only what is inside.

The marker is the opt-in, so there is no per-job or global setting:

* no complete ``<deliverable>`` / ``</deliverable>`` pair -> the text is
  returned unchanged (today's behaviour, and the fail-open case: a model that
  forgets the marker still delivers its whole answer, never an empty one);
* one or more pairs -> their contents, stripped and joined by a blank line;
* every pair empty -> unchanged, for the same reason a missing marker is.

Everything outside the pairs is dropped, an ``[OPTIONS: ...]`` line included,
so a job that wants buttons puts that line inside its block.
"""

from __future__ import annotations

import re

#: Matched case-insensitively and non-greedily, so two blocks stay two blocks.
_DELIVERABLE_RE = re.compile(r"<deliverable>(.*?)</deliverable>", re.IGNORECASE | re.DOTALL)


def extract_cron_deliverable(text: str) -> str:
    """Return only the marked deliverable block(s) of ``text``, or ``text`` unchanged."""
    if not text:
        return text
    blocks = [m.group(1).strip() for m in _DELIVERABLE_RE.finditer(text)]
    blocks = [b for b in blocks if b]
    if not blocks:
        return text
    return "\n\n".join(blocks)
