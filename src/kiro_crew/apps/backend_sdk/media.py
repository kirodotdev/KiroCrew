"""Media type sniffing: what a file is, decided by its leading bytes (seam level 14).

``kiro_crew.messaging.raster``, the table both directions of the gateway's own file
path ask. The answer comes from the content, never from a filename or a declared
type: both of those are attacker-controlled and the leading bytes are not, so a
script named ``chart.png`` and declared ``image/png`` is still a script (CWE-434).
SVG is never an answer, because it is scriptable markup with no fixed signature, and
a RIFF container is ``image/webp`` only when its form tag says so, so a RIFF/WAVE
file is refused.

The answer is the set the gateway can sniff, not a list of what to accept. Keep your
own allowlist of the types you take on top of it, so a type the gateway learns to
sniff in a later release does not widen what your app accepts.
"""

from __future__ import annotations

from kiro_crew.messaging import raster

#: Leading bytes :func:`sniff_raster_mime` needs to decide every type it knows. The
#: gateway's own ``messaging.raster.SNIFF_BYTES``, so a read sliced to it holds what
#: the gateway's own sniff reads.
RASTER_SNIFF_BYTES: int = raster.SNIFF_BYTES


def sniff_raster_mime(head: bytes) -> str | None:
    """The raster type *head* starts with, such as ``image/png``, or ``None``.

    ``kiro_crew.messaging.raster.sniff_raster_mime``, looked up at call time. *head*
    is the file's leading bytes: at least :data:`RASTER_SNIFF_BYTES` of them, and a
    longer buffer answers the same. A shorter one matches fewer types: a header cut
    before its signature ends is ``None``. Check the answer against your own
    allowlist; an answer that is not ``None`` only says what the bytes are.
    """
    return raster.sniff_raster_mime(head)


__all__ = ["RASTER_SNIFF_BYTES", "sniff_raster_mime"]
