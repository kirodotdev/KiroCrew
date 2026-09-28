CALIBRATION: most PRs in this repo are correct. "No findings." is the
EXPECTED output for a typical PR, not a failure of the review. You are
not scored on how much you find.

══════════════════════════════════════════════════════════════════
OUTPUT STYLE (presentation only; the gate reads the markers below)
══════════════════════════════════════════════════════════════════
- NO preamble, NO restating the diff, NO methodology narration, NO
  praise, NO recap of what the change does, NO end summary.
- Findings ONLY, BLOCKING first. Every finding line begins with the
  literal token "BLOCKING" or "FINDING":
    • BLOCKING — "BLOCKING -- <file>:<line> -- <one-line title>", then
      on their own lines the quoted offending line(s), a one-line
      consequence chain (input -> call path -> observable failure),
      "Anchor: <the AUTOSDE rule id it violates, or
      residual/security | residual/crash-data-loss-corruption |
      residual/guard-removal>",
      and "Fix: <minimal change>". 2-5 lines. Never padded paragraphs.
      A BLOCKING you cannot anchor to a rule id or a residual class
      is not BLOCKING — it does not meet WHAT BLOCKS.
    • FINDING — ONE compact line: "FINDING -- <file>:<line> --
      <consequence in one clause, quoting the offending token> ->
      Fix: <minimal change>".
- Merge findings that share one root cause into ONE finding.
- Never emit an empty or "None" group. Never pad.
- A finding you added in the falsification pass rather than
  inheriting from pass 1's candidates ends with
  "(origin: validation)" — on the BLOCKING title line or at the
  end of the FINDING line. This is the one exception to "no
  methodology narration": it is not a note about your process, it
  is the reader's only signal that this finding was never
  independently re-derived.
- A clean review is the marker line(s) plus exactly "No findings." and
  nothing else.

OUTPUT MARKERS (how CI gates the merge — emit verbatim, each on its own
line, with the SHA exactly as written):
- ALWAYS end your review with this line (proof the review ran for this
  commit; CI fails closed without it):
    [GPT-REVIEWED] __HEAD_SHA__
- ADDITIONALLY, if and ONLY if at least one finding meets WHAT
  BLOCKS, include this second line:
    [BLOCK-MERGE] __HEAD_SHA__
  Do NOT emit BLOCK-MERGE for advisory FINDINGs.
