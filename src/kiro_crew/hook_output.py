"""Shared bound-and-report helper for a hook's ``tool_response.output``.

Every call site that fires ``PostToolUse`` on a tool's terminal result bounds
the output text it hands the hook to the same 2000 characters, so a hook
script (or its own ``max_output_size``/STDOUT cap — see ``Output size`` in
``docs/reference/kiro-cli/hooks.md``) never receives an unbounded string. That
cut used to be silent: a plain ``text[:2000]`` slice with nothing in the
payload saying a cut happened. :func:`bounded_hook_output` makes the cut
visible to the hook instead, via two sibling keys alongside ``output``.

Kept here rather than in ``kiro_crew.hooks`` (whose own "owners read these
globals" pattern couples it tightly to the hook *engine*) or
``kiro_crew.security`` (redaction, a different concern — redaction must still
run on the FULL text before this bound is applied) so the four call sites
(ACP, dashboard, subagent, messaging) can import one small, dependency-free
helper without adding an edge to either of those larger modules.
"""

from __future__ import annotations

#: The character cap on text handed to a hook as ``tool_response.output``,
#: shared by the ACP, dashboard, subagent and messaging PostToolUse call
#: sites. This bound itself must never change independently of all four.
HOOK_OUTPUT_CHAR_CAP = 2000


def bounded_hook_output(text: str, limit: int = HOOK_OUTPUT_CHAR_CAP) -> dict[str, object]:
    """Bound ``text`` to ``limit`` characters and report whether it was cut.

    Returns the payload a ``tool_response``/``output`` dict should carry:

    * ``output`` — ``text`` cut to ``limit`` characters (unchanged if shorter).
    * ``truncated`` — whether ``text`` was longer than ``limit``.
    * ``output_length`` — ``len(text)`` before any cut.

    Callers redact ``text`` first; this only bounds and reports, it does not
    redact.
    """
    return {
        "output": text[:limit],
        "truncated": len(text) > limit,
        "output_length": len(text),
    }
